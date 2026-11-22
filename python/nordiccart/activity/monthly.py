"""Month-end verification (design 7.4, step 2; S6 milestone 3). Read-only; runs as the loader.

After the last window of a month (and again as a gate before the first window of the next month)
the engine checks the month [first day 00:00 UTC, next month 00:00 UTC), from the origin on:

    orders        lines and totals add up; one gap-free global order sequence; every order placed in
                  the month has the stock records its status requires; the open orders at the month
                  end are exactly the orders carried by the month-end checkpoint
    payments      every attempt is for the order total; exactly one capture when confirmed or
                  fulfilled (none otherwise); no pending attempt on a closed order
    inventory     ledger identity; no negative or oversold balance; network conservation (on hand +
                  in transit changes by receipts - sales); every receipt maps to a purchase order line,
                  never above the ordered quantity
    rules         every P1 price change due in the month (price timeline) is recorded, nothing else;
                  R-cal revisions recorded as many as the runs' R-cal units
    rates         three rates (SEK, NOK, DKK) for every day; a rate for every non-EUR fulfilment day
    checkpoints   the month is covered by contiguous verified runs chained to their predecessor, each
                  with its checkpoint (same hash, as of its window end); the month-end checkpoint equals
                  the database state at the month end
Each check counts the rows that break its rule (expected 0). Scripts 11 to 18 remain the full check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import psycopg

from ..master_data.rules import UTC
from ..units.base import LEDGER_IDENTITY_SQL
from .engine import EngineContext, EngineError
from .state import state_as_of, state_hash


class MonthVerificationError(EngineError):
    """A completed month breaks a month-end rule (blocking)."""


@dataclass
class MonthResult:
    month: str
    first: datetime
    end: datetime
    checks: list[tuple[str, int]] = field(default_factory=list)      # (rule, violations)
    summary: dict = field(default_factory=dict)

    @property
    def problems(self) -> list[str]:
        return [f"{n} {rule}" for rule, n in self.checks if n]


def month_bounds(day: date) -> tuple[datetime, datetime]:
    first = datetime(day.year, day.month, 1, tzinfo=UTC)
    nxt = datetime(day.year + (day.month == 12), day.month % 12 + 1, 1, tzinfo=UTC)
    return first, nxt


def verify_month(conn: psycopg.Connection, ctx: EngineContext, day: date) -> MonthResult:
    """Month-end checks of the month containing `day` (from the origin on)."""
    first, end = month_bounds(day)
    lo = max(first, ctx.origin)
    result = MonthResult(f"{first:%Y-%m}", lo, end)
    p = {"lo": lo, "hi": end}
    q = lambda sql, params=p: conn.execute(sql, params).fetchone()[0]   # noqa: E731

    def check(rule: str, violations: int) -> None:
        result.checks.append((rule, int(violations)))

    month_orders = "SELECT * FROM orders WHERE placed_at >= %(lo)s AND placed_at < %(hi)s"
    # ---------------------------------------------------------------- orders
    check("orders whose lines or totals do not add up", q(f"""
        SELECT count(*) FROM ({month_orders}) o
         WHERE NOT EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id)
            OR o.items_total <> (SELECT sum(i.line_total) FROM order_items i WHERE i.order_id = o.order_id)
            OR o.order_total <> o.items_total + o.shipping_fee
            OR EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id
                        AND i.line_total <> i.quantity * i.unit_price - i.discount_amount)"""))
    check("order numbers not on one gap-free global sequence", q("""
        SELECT count(*) - count(DISTINCT substr(order_number, 9)::int)
             + abs(coalesce(max(substr(order_number, 9)::int), 0) - count(*)) FROM orders"""))
    check("orders whose stock records do not match their status", q(f"""
        SELECT count(*) FROM ({month_orders}) o
         WHERE EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id AND (
                 (o.status = 'fulfilled' AND ((SELECT count(*) FROM inventory_movements m WHERE m.order_item_id = i.order_item_id
                                               AND m.movement_type = 'sale' AND m.quantity_change = -i.quantity) <> 1
                                              OR NOT EXISTS (SELECT 1 FROM inventory_reservations r
                                                              WHERE r.order_item_id = i.order_item_id AND r.status = 'consumed')))
              OR (o.status IN ('stock_reserved', 'confirmed') AND NOT EXISTS (
                      SELECT 1 FROM inventory_reservations r WHERE r.order_item_id = i.order_item_id AND r.status = 'active'))
              OR (o.status IN ('cancelled', 'payment_failed') AND (
                      EXISTS (SELECT 1 FROM inventory_reservations r WHERE r.order_item_id = i.order_item_id AND r.status = 'active')
                      OR EXISTS (SELECT 1 FROM inventory_movements m WHERE m.order_item_id = i.order_item_id)))))"""))
    state_end = state_as_of(conn, end)
    checkpoint = conn.execute("SELECT state_hash, state_snapshot FROM meta.activity_checkpoints WHERE state_as_of = %s",
                              (end,)).fetchone()
    carried = set((checkpoint[1].get("engine_carry") or {}).get("keys", {})) if checkpoint else set()
    check("open orders at the month end that the month-end checkpoint does not carry (or the reverse)",
          len(set(state_end["open_orders"]) ^ carried))
    # ---------------------------------------------------------------- payments
    check("payment attempts not for the order total", q(f"""
        SELECT count(*) FROM payment_attempts a JOIN ({month_orders}) o ON o.order_id = a.order_id
         WHERE a.amount <> o.order_total"""))
    check("orders without exactly one capture when confirmed or fulfilled (none otherwise)", q(f"""
        SELECT count(*) FROM ({month_orders}) o
         WHERE (SELECT count(*) FROM payment_attempts a WHERE a.order_id = o.order_id AND a.status = 'captured')
               <> CASE WHEN o.status IN ('confirmed', 'fulfilled') THEN 1 ELSE 0 END"""))
    check("closed orders with a pending payment attempt", q(f"""
        SELECT count(*) FROM ({month_orders}) o JOIN payment_attempts a ON a.order_id = o.order_id
         WHERE o.status IN ('fulfilled', 'cancelled', 'payment_failed') AND a.status = 'pending'"""))
    # ---------------------------------------------------------------- inventory
    check("balances that break the ledger identity", q(LEDGER_IDENTITY_SQL, None))
    check("negative or oversold balances", q("SELECT count(*) FROM inventory_balances "
                                             "WHERE quantity_on_hand < 0 OR quantity_reserved > quantity_on_hand", None))
    state_lo = state_as_of(conn, lo)
    stock = lambda s: sum(v[0] for v in s["balances"].values()) + sum(s["in_transit"].values())   # noqa: E731
    flows = q("SELECT coalesce(sum(quantity_change), 0) FROM inventory_movements WHERE occurred_at >= %(lo)s "
              "AND occurred_at < %(hi)s AND movement_type IN ('purchase_receipt', 'sale')")
    check("units by which on hand + in transit does not change by receipts - sales", abs(stock(state_end) - stock(state_lo) - flows))
    check("purchase receipts without their purchase order line, or above the ordered quantity", q("""
        SELECT count(*) FROM inventory_movements m
         WHERE m.movement_type = 'purchase_receipt' AND m.occurred_at >= %(lo)s AND m.occurred_at < %(hi)s
           AND NOT EXISTS (SELECT 1 FROM purchase_orders po JOIN purchase_order_lines pl ON pl.purchase_order_id = po.purchase_order_id
                            WHERE po.purchase_order_number = split_part(m.external_reference, '/', 1)
                              AND pl.stock_location_id = m.stock_location_id AND pl.product_id = m.product_id
                              AND pl.quantity_received <= pl.quantity_ordered)"""))
    # ---------------------------------------------------------------- monthly rules
    due = {(c.sku, c.market, c.at, c.new_price) for c in ctx.price_changes if lo <= c.at < end}
    recorded = {(sku, market, at, price) for sku, market, at, price in conn.execute(
        "SELECT p.sku, pp.country_code, pp.valid_from, pp.list_price FROM product_prices pp JOIN products p USING (product_id) "
        "WHERE pp.valid_from >= %(lo)s AND pp.valid_from < %(hi)s AND EXISTS (SELECT 1 FROM product_prices e "   # not a launch price
        "WHERE e.product_id = pp.product_id AND e.country_code = pp.country_code AND e.valid_from < pp.valid_from)",
        p).fetchall()}
    check("P1 price changes due in the month and not recorded, or recorded and not due", len(due ^ recorded))
    runs = conn.execute("SELECT activity_run_id, previous_run_id, window_start, window_end, status::text, end_state_hash, "
                        "unit_counts FROM meta.activity_runs WHERE window_start >= %(lo)s AND window_start < %(hi)s "
                        "ORDER BY window_start", p).fetchall()
    rcal_units = sum(int(r[6].get("RCAL", 0)) for r in runs)
    check("R-cal reviews recorded unlike the runs' R-cal units", abs(rcal_units - q(
        "SELECT count(DISTINCT document_reference) FROM reorder_point_revisions WHERE revision_kind = 'monthly_review' "
        "AND effective_at >= %(lo)s AND effective_at < %(hi)s")))
    # ---------------------------------------------------------------- exchange rates
    days = (end.date() - lo.date()).days
    check("days without exactly three exchange rates", days - q("""
        SELECT count(*) FROM (SELECT rate_date FROM exchange_rates WHERE rate_date >= %(lo)s::date AND rate_date < %(hi)s::date
                               GROUP BY rate_date HAVING count(*) = 3) d"""))
    check("non-EUR fulfilled orders without a rate on their local fulfilment day", q("""
        SELECT count(*) FROM orders o JOIN countries c ON c.country_code = o.country_code
         WHERE o.fulfilled_at >= %(lo)s AND o.fulfilled_at < %(hi)s AND o.currency_code <> 'EUR'
           AND NOT EXISTS (SELECT 1 FROM exchange_rates x WHERE x.quote_currency_code = o.currency_code
                            AND x.rate_date = (o.fulfilled_at AT TIME ZONE c.time_zone)::date)"""))
    # ---------------------------------------------------------------- activity runs and checkpoints
    covered, gaps, chain = timedelta(0), 0, 0
    expected_start = lo
    for run_id, previous, start, stop, status, end_hash, _ in runs:
        if status != "verified":
            gaps += 1
            continue
        covered += stop - start
        gaps += start != expected_start
        expected_start = stop
        if previous is not None and q("SELECT window_end FROM meta.activity_runs WHERE activity_run_id = %s", (previous,)) != start:
            chain += 1
    check("parts of the month not covered by contiguous verified runs", gaps + (covered != end - lo))
    check("runs not chained to the run before them", chain)
    check("verified runs without their checkpoint (same hash, as of the window end)", q("""
        SELECT count(*) FROM meta.activity_runs r
         WHERE r.window_start >= %(lo)s AND r.window_start < %(hi)s AND r.status = 'verified'
           AND NOT EXISTS (SELECT 1 FROM meta.activity_checkpoints c WHERE c.activity_run_id = r.activity_run_id
                            AND c.state_hash = r.end_state_hash AND c.state_as_of = r.window_end)"""))
    check("month-end checkpoint missing or unlike the database state at the month end",
          int(checkpoint is None or checkpoint[0] != state_hash(state_end)))

    result.summary = {
        "orders placed": q("SELECT count(*) FROM orders WHERE placed_at >= %(lo)s AND placed_at < %(hi)s"),
        "orders fulfilled": q("SELECT count(*) FROM orders WHERE fulfilled_at >= %(lo)s AND fulfilled_at < %(hi)s"),
        "payment attempts": q("SELECT count(*) FROM payment_attempts WHERE attempted_at >= %(lo)s AND attempted_at < %(hi)s"),
        "movements": dict(conn.execute("SELECT movement_type::text, count(*) FROM inventory_movements WHERE occurred_at >= %(lo)s "
                                       "AND occurred_at < %(hi)s GROUP BY 1 ORDER BY 1", p).fetchall()),
        "purchase orders": q("SELECT count(*) FROM purchase_orders WHERE ordered_at >= %(lo)s AND ordered_at < %(hi)s"),
        "P1 price changes": len(recorded),
        "R-cal revisions": q("SELECT count(*) FROM reorder_point_revisions WHERE revision_kind = 'monthly_review' "
                             "AND effective_at >= %(lo)s AND effective_at < %(hi)s"),
        "exchange rate days": days,
        "verified runs": sum(1 for r in runs if r[4] == "verified"),
        "open orders at the month end": len(state_end["open_orders"]),
    }
    return result
