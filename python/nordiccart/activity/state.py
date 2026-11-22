"""Engine state derived from the database by business time (design 7.2, stage S6).

state_as_of(conn, t) reads ONLY rows whose business time is before t, so the state of any past
instant can be recomputed later from the same database. It is what the run protocol compares:
    * at a window start with the previous checkpoint (drift detection),
    * at a window end with the engine's own simulation (verification).
The snapshot uses business keys only (location code, sku, order and document numbers), never
surrogate ids, so it is equal for any two databases that hold the same business history.

Business time of each record, i.e. the time of the unit of work that wrote it:
    orders            placed_at; status from confirmed_at, fulfilled_at, cancelled_at
    payment attempts  attempted_at; result at captured_at or failed_at (the U3c unit time)
    reservations      reserved_at; closed at closed_at
    movements         occurred_at
    purchase orders   ordered_at; received through their <PO>/DN<n> receipts
    reorder points    reorder_point_revisions.effective_at (V009)
    prices            product_prices.valid_from; a row closed at valid_to = t is still the price as of t
    exchange rates    rate_date 00:00 UTC (X1 runs at the start of its UTC day)
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import psycopg

UTC = timezone.utc

STATE_VERSION = 2          # 2 (milestone 3): current list price per product and market, product_prices count

_PRICES = """
SELECT p.sku, pp.country_code, pp.list_price FROM product_prices pp JOIN products p ON p.product_id = pp.product_id
 WHERE pp.valid_from < %(t)s AND (pp.valid_to IS NULL OR pp.valid_to >= %(t)s)"""

_BALANCES = """
WITH m AS (SELECT stock_location_id, product_id, sum(quantity_change) AS q FROM inventory_movements
            WHERE occurred_at < %(t)s GROUP BY 1, 2),
     r AS (SELECT stock_location_id, product_id, sum(quantity) AS q FROM inventory_reservations
            WHERE reserved_at < %(t)s AND (closed_at IS NULL OR closed_at >= %(t)s) GROUP BY 1, 2),
     v AS (SELECT DISTINCT ON (stock_location_id, product_id) stock_location_id, product_id, old_reorder_point
             FROM reorder_point_revisions WHERE effective_at >= %(t)s
            ORDER BY stock_location_id, product_id, effective_at, reorder_point_revision_id)
SELECT l.location_code, p.sku, coalesce(m.q, 0), coalesce(r.q, 0), coalesce(v.old_reorder_point, b.reorder_point)
  FROM inventory_balances b
  JOIN stock_locations l ON l.stock_location_id = b.stock_location_id
  JOIN products p ON p.product_id = b.product_id
  LEFT JOIN m ON m.stock_location_id = b.stock_location_id AND m.product_id = b.product_id
  LEFT JOIN r ON r.stock_location_id = b.stock_location_id AND r.product_id = b.product_id
  LEFT JOIN v ON v.stock_location_id = b.stock_location_id AND v.product_id = b.product_id"""

_IN_TRANSIT = """
SELECT o.external_reference, p.sku, -o.quantity_change
  FROM inventory_movements o JOIN products p ON p.product_id = o.product_id
 WHERE o.movement_type = 'transfer_out' AND o.occurred_at < %(t)s
   AND NOT EXISTS (SELECT 1 FROM inventory_movements i WHERE i.related_movement_id = o.movement_id
                    AND i.movement_type = 'transfer_in' AND i.occurred_at < %(t)s)"""

_OPEN_PO_LINES = """
SELECT po.purchase_order_number, pl.line_number, l.location_code, p.sku, pl.quantity_ordered,
       coalesce((SELECT sum(m.quantity_change) FROM inventory_movements m
                  WHERE m.movement_type = 'purchase_receipt' AND m.stock_location_id = pl.stock_location_id
                    AND m.product_id = pl.product_id AND m.occurred_at < %(t)s
                    AND split_part(m.external_reference, '/', 1) = po.purchase_order_number), 0)
  FROM purchase_orders po
  JOIN purchase_order_lines pl ON pl.purchase_order_id = po.purchase_order_id
  JOIN stock_locations l ON l.stock_location_id = pl.stock_location_id
  JOIN products p ON p.product_id = pl.product_id
 WHERE po.ordered_at < %(t)s"""

_OPEN_ORDERS = """
SELECT o.order_number,
       CASE WHEN o.fulfilled_at < %(t)s OR o.cancelled_at < %(t)s THEN 'closed'
            WHEN o.confirmed_at < %(t)s THEN 'confirmed'
            WHEN EXISTS (SELECT 1 FROM order_items i JOIN inventory_reservations r ON r.order_item_id = i.order_item_id
                          WHERE i.order_id = o.order_id AND r.reserved_at < %(t)s) THEN 'stock_reserved'
            ELSE 'placed' END,
       coalesce((SELECT string_agg(a.attempt_number || ':' ||
                        CASE WHEN a.captured_at < %(t)s THEN 'captured' WHEN a.failed_at < %(t)s THEN 'failed'
                             ELSE 'pending' END, ',' ORDER BY a.attempt_number)
                   FROM payment_attempts a WHERE a.order_id = o.order_id AND a.attempted_at < %(t)s), '')
  FROM orders o WHERE o.placed_at < %(t)s"""

_SEQUENCES = """
SELECT 'order', max(substr(order_number, 9)::int) FROM orders
 WHERE placed_at < %(t)s AND order_number ~ '^[A-Z]{2}-[0-9]{4}-[0-9]{8}$'
UNION ALL
SELECT substr(external_reference, 1, 10), max(substr(external_reference, 12, 6)::int) FROM inventory_movements
 WHERE movement_type = 'transfer_out' AND occurred_at < %(t)s AND external_reference ~ '^TR-[A-Z]{2}-[0-9]{4}-[0-9]{6}-'
 GROUP BY 1
UNION ALL
SELECT substr(purchase_order_number, 1, 10), max(substr(purchase_order_number, 12, 6)::int) FROM purchase_orders
 WHERE ordered_at < %(t)s GROUP BY 1"""

_COUNTS = """
SELECT (SELECT count(*) FROM orders WHERE placed_at < %(t)s),
       (SELECT count(*) FROM order_items i JOIN orders o ON o.order_id = i.order_id WHERE o.placed_at < %(t)s),
       (SELECT count(*) FROM payment_attempts WHERE attempted_at < %(t)s),
       (SELECT count(*) FROM inventory_reservations WHERE reserved_at < %(t)s),
       (SELECT count(*) FROM inventory_movements WHERE occurred_at < %(t)s),
       (SELECT count(*) FROM purchase_orders WHERE ordered_at < %(t)s),
       (SELECT count(*) FROM purchase_order_lines pl JOIN purchase_orders po
                ON po.purchase_order_id = pl.purchase_order_id WHERE po.ordered_at < %(t)s),
       (SELECT count(*) FROM reorder_point_revisions WHERE effective_at < %(t)s),
       (SELECT count(*) FROM exchange_rates WHERE (rate_date::timestamp AT TIME ZONE 'UTC') < %(t)s),
       (SELECT count(*) FROM product_prices WHERE valid_from < %(t)s),
       (SELECT max(rate_date) FROM exchange_rates WHERE (rate_date::timestamp AT TIME ZONE 'UTC') < %(t)s)"""

COUNT_NAMES = ("orders", "order_items", "payment_attempts", "reservations", "movements", "purchase_orders",
               "purchase_order_lines", "reorder_point_revisions", "exchange_rates", "product_prices")

LATEST_BUSINESS_TIME_SQL = """
SELECT greatest((SELECT max(greatest(placed_at, confirmed_at, fulfilled_at, cancelled_at)) FROM orders),
                (SELECT max(greatest(attempted_at, authorized_at, captured_at, failed_at)) FROM payment_attempts),
                (SELECT max(greatest(reserved_at, closed_at)) FROM inventory_reservations),
                (SELECT max(occurred_at) FROM inventory_movements),
                (SELECT max(ordered_at) FROM purchase_orders),
                (SELECT max(effective_at) FROM reorder_point_revisions),
                (SELECT max(rate_date)::timestamp AT TIME ZONE 'UTC' FROM exchange_rates))"""


def state_as_of(conn: psycopg.Connection, t: datetime) -> dict:
    """The engine state at business time t, from the database (read-only)."""
    p = {"t": t}
    balances = {f"{loc}|{sku}": [int(on_hand), int(reserved), int(rop)]
                for loc, sku, on_hand, reserved, rop in conn.execute(_BALANCES, p).fetchall()}
    in_transit = {f"{ref}|{sku}": int(q) for ref, sku, q in conn.execute(_IN_TRANSIT, p).fetchall()}
    open_po = {f"{number}|{line}": [loc, sku, int(ordered), int(received)]
               for number, line, loc, sku, ordered, received in conn.execute(_OPEN_PO_LINES, p).fetchall()
               if received < ordered}
    open_orders = {number: [status, attempts] for number, status, attempts in conn.execute(_OPEN_ORDERS, p).fetchall()
                   if status != "closed"}
    sequences = {key: int(n) for key, n in conn.execute(_SEQUENCES, p).fetchall() if n is not None}
    prices = {f"{sku}|{market}": str(price) for sku, market, price in conn.execute(_PRICES, p).fetchall()}
    row = conn.execute(_COUNTS, p).fetchone()
    n = len(COUNT_NAMES)
    return build_snapshot(t, balances, in_transit, open_po, open_orders, sequences,
                          dict(zip(COUNT_NAMES, (int(x) for x in row[:n]))), row[n], prices)


def build_snapshot(t: datetime, balances: dict, in_transit: dict, open_po: dict, open_orders: dict, sequences: dict,
                   counts: dict, rates_through, prices: dict) -> dict:
    """The canonical snapshot (also built by the engine's simulation, in exactly this shape)."""
    return {
        "version": STATE_VERSION,
        "as_of": t.astimezone(UTC).isoformat(),
        "balances": dict(sorted(balances.items())),
        "in_transit": dict(sorted((k, v) for k, v in in_transit.items() if v)),
        "open_purchase_order_lines": dict(sorted(open_po.items())),
        "open_orders": dict(sorted(open_orders.items())),
        "sequences": dict(sorted(sequences.items())),
        "counts": {k: counts[k] for k in COUNT_NAMES},
        "exchange_rates_through": rates_through.isoformat() if rates_through is not None else None,
        "prices": dict(sorted(prices.items())),
    }


def state_hash(snapshot: dict) -> str:
    text = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def latest_business_time(conn: psycopg.Connection) -> datetime | None:
    return conn.execute(LATEST_BUSINESS_TIME_SQL).fetchone()[0]


def differences(a: dict, b: dict, limit: int = 8) -> list[str]:
    """Readable differences between two snapshots (for drift and verification messages)."""
    out = []
    for section in sorted(set(a) | set(b)):
        x, y = a.get(section), b.get(section)
        if x == y:
            continue
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y)):
                if x.get(k) != y.get(k):
                    out.append(f"{section}[{k}]: {x.get(k)} vs {y.get(k)}")
                    if len(out) >= limit:
                        return out
        else:
            out.append(f"{section}: {x} vs {y}")
        if len(out) >= limit:
            break
    return out
