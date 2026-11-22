"""Pilot scope: a pilot's own records versus later activity (stage S1, design 8.1).

A pilot loader (transactions or replenishment) verifies its OWN records exactly: every
stored value, the prefix rule and the stock it expects. Every other record is FOREIGN to
that pilot. Until S1 any foreign order or movement blocked the pilot loaders. Since S1 a
foreign record is accepted as LATER ACTIVITY only when it passes every rule below. Being
dated later is necessary but never sufficient:

  dating     strictly after the pilot's last event (the boundary) and not in the future;
             every payment, reservation, sale and status time at or after placed_at
  identity   order numbers <market>-<local year of placed_at>-<8-digit global sequence>,
             continuing the pilot's sequence without reuse, duplicates or gaps, numbered in
             placed_at order; receipts PO-<market>-<year>-<6 digits>/DN<n> at a warehouse of
             that market; transfers TR-<market>-<local year>-<6 digits>-<store of the same
             market> from a warehouse; no later document reuses a pilot document number; one
             document number = one location, one time, one destination
  integrity  orders: a customer of the market registered at placed_at, lines 1..n whose
             totals add up, prices and costs from product_prices valid at placed_at, payment
             attempts 1..n for the order total with exactly one capture when confirmed or
             fulfilled (none otherwise), reservations at the fulfilment location for the
             line's product and quantity, the stock records the status requires (fulfilled:
             one sale at fulfilled_at and a consumed reservation per line; stock_reserved or
             confirmed: an active reservation per line; otherwise no active reservation and
             no sale); transfer arrivals that match their dispatch (product, quantity,
             reference, destination store, not earlier)
  types      only movement types the pipeline simulates: no stock adjustments, write-offs or
             return restocks

Exception E2 (design 8.3): an opening balance at a listing OUTSIDE the pilot's opening
inventory is dated at the opening count, i.e. before the boundary. It is accepted when it
carries exactly the opening count time and reference of the pilot's opening balances in
the same market and its product was on sale on the opening day.

Accepted later activity is added to the stock the pilot expects at its own listings: on
hand from the later movements, reserved from the active reservations of later orders.
Any broken rule is returned as a problem and blocks the loader. Read-only: nothing here
writes. The rules are set-based SQL so they scale to the full-profile history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import psycopg

Key = tuple[str, str]                        # (location_code, sku)

LATER_MOVEMENT_TYPES = ("purchase_receipt", "transfer_out", "transfer_in", "sale")
NOT_SIMULATED_TYPES = ("stock_adjustment", "write_off", "return_restock")


@dataclass(frozen=True)
class PilotScope:
    """What a pilot loader verifies itself; everything else must be valid later activity."""
    name: str                                # for messages, e.g. "the pilot transactions"
    boundary: datetime                       # the pilot's last event; later activity is strictly after it
    order_numbers: frozenset[str]            # the pilot's orders (compared with the plan by the loader)
    last_order_sequence: int                 # highest global order sequence of the pilot
    listings: frozenset[Key]                 # the pilot's opening inventory listings
    own_movement_ids: frozenset[int] = frozenset()   # receipts and transfers the loader matched to its plan
    own_documents: frozenset[str] = frozenset()      # PO-/TR- document numbers of the pilot

    def params(self) -> dict:
        keys = sorted(self.listings)
        return {
            "boundary": self.boundary,
            "orders": sorted(self.order_numbers),
            "last_seq": self.last_order_sequence,
            "own_ids": sorted(self.own_movement_ids),
            "own_docs": sorted(self.own_documents),
            "loc": [k[0] for k in keys],
            "sku": [k[1] for k in keys],
        }


@dataclass
class LaterActivity:
    """Foreign records found in the database, their effect on the pilot's stock and the rules they break."""
    orders: int = 0                           # orders that are not the pilot's
    open_orders: int = 0                      # of which placed, stock_reserved or confirmed
    movements: int = 0                        # receipts, transfers and sales dated after the boundary
    net_units: int = 0                        # their net quantity change (all listings)
    by_type: dict[str, int] = field(default_factory=dict)
    on_hand_change: dict[Key, int] = field(default_factory=dict)    # per pilot listing
    reserved: dict[Key, int] = field(default_factory=dict)          # active reservations of later orders
    extra_listings: int = 0                   # listings outside the pilot's opening inventory
    extra_opening_balances: int = 0           # their opening balances (exception E2)
    extra_opening_units: int = 0
    first_at: datetime | None = None          # earliest later business time
    problems: list[str] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        """True when later business activity (orders or movements after the boundary) is present."""
        return bool(self.orders or self.movements)

    def summary(self) -> str:
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(self.by_type.items())) or "none"
        return (f"{self.orders} orders ({self.open_orders} open), {self.movements} movements ({kinds}), "
                f"net {self.net_units:+d} units; listings outside the pilot {self.extra_listings} "
                f"({self.extra_opening_balances} opening balances, {self.extra_opening_units} units)")


# Problem texts; {name}, {boundary} and {last_seq} come from the scope.
RULES: dict[str, str] = {
    "order_not_after_boundary":
        "orders that are not part of this plan are dated at or before {name}'s last event ({boundary})",
    "order_number_format":
        "orders that are not part of this plan have a number that is not <market>-<local year of placed_at>-"
        "<8-digit sequence>",
    "order_sequence_in_pilot_range":
        "orders that are not part of this plan use a sequence number of the pilot's range (1 to {last_seq})",
    "order_sequence_duplicate": "later orders share their global sequence number with another order",
    "order_sequence_gap": "order sequence numbers are missing after {name}'s last number {last_seq}",
    "order_sequence_order": "later orders are not numbered in placed_at order",
    "order_in_future": "later orders have events dated in the future",
    "order_event_before_placed":
        "later orders have payment, reservation, sale or status times before placed_at",
    "order_customer":
        "later orders have no customer, a customer of another market, or one not yet registered at placed_at",
    "order_lines_totals":
        "later orders have no lines, line numbers other than 1..n, or line, items or order totals that do not add up",
    "order_line_price":
        "later order lines have a price or cost other than the product_prices row valid at placed_at",
    "order_payments":
        "later orders have payment attempts not numbered 1..n, for another amount than the order total, or not "
        "exactly one capture when confirmed or fulfilled (none otherwise)",
    "order_reservation_line":
        "later orders have reservations at another location than the fulfilment location, or for another product "
        "or quantity than the line",
    "order_stock_status":
        "later orders have reservations or sales that do not match their status (fulfilled: one sale at "
        "fulfilled_at and a consumed reservation per line; stock_reserved or confirmed: an active reservation per "
        "line; otherwise no active reservation and no sale)",
    "movement_not_simulated":
        "stock adjustments, write-offs or return restocks exist; the pipeline does not simulate them",
    "movement_not_after_boundary":
        "stock movements other than opening balances and sales of the pilot's orders are dated at or before "
        "{name}'s last event ({boundary}); they are not part of this plan",
    "movement_in_future": "stock movements are dated in the future",
    "opening_outside_pilot":
        "opening balances at listings outside the pilot (exception E2) do not carry the opening count time and "
        "reference of their market, or their product was not on sale on the opening day",
    "receipt_rules":
        "purchase receipts are not at a warehouse or their reference is not PO-<market>-<year>-<6 digits>/DN<n> "
        "of the warehouse's market",
    "transfer_out_rules":
        "transfers out are not from a warehouse, or their reference is not TR-<market>-<local year>-<6 digits>-"
        "<a store of the same market>",
    "transfer_in_rules":
        "transfer arrivals do not match their transfer_out (product, quantity, reference, the destination store "
        "named in the reference, not before the dispatch)",
    "document_reuses_pilot": "later receipts or transfers reuse a document number of {name}",
    "document_identity":
        "later document numbers are used for more than one location, time or destination",
}

_BASE = """
WITH pl AS (
    SELECT l.stock_location_id, p.product_id
      FROM unnest(%(loc)s::text[], %(sku)s::text[]) AS k(location_code, sku)
      JOIN stock_locations l ON l.location_code = k.location_code
      JOIN products p ON p.sku = k.sku
),
fo AS (
    SELECT o.*, (o.placed_at AT TIME ZONE c.time_zone) AS placed_local
      FROM orders o JOIN countries c ON c.country_code = o.country_code
     WHERE o.order_number <> ALL (%(orders)s::text[])
),
fm AS (
    SELECT m.movement_id, m.movement_type::text AS kind, m.stock_location_id, m.product_id, m.quantity_change,
           m.occurred_at, m.related_movement_id, m.external_reference,
           l.location_code, l.location_type::text AS location_type, l.country_code,
           (m.occurred_at AT TIME ZONE c.time_zone) AS local_at, p.sku, p.launched_on,
           EXISTS (SELECT 1 FROM pl WHERE pl.stock_location_id = m.stock_location_id
                                      AND pl.product_id = m.product_id) AS at_pilot_listing,
           CASE WHEN m.movement_type = 'purchase_receipt' THEN split_part(m.external_reference, '/', 1)
                WHEN m.movement_type IN ('transfer_out', 'transfer_in')
                THEN substring(m.external_reference FROM '^TR-[A-Z]{2}-[0-9]{4}-[0-9]{6}') END AS document_number,
           m.movement_type::text || ' ' || l.location_code || '/' || p.sku || coalesce(' ' || m.external_reference, '')
               || ' at ' || to_char(m.occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS label
      FROM inventory_movements m
      JOIN stock_locations l ON l.stock_location_id = m.stock_location_id
      JOIN countries c ON c.country_code = l.country_code
      JOIN products p ON p.product_id = m.product_id
     WHERE m.movement_id <> ALL (%(own_ids)s::bigint[])
       AND NOT (m.movement_type = 'sale' AND EXISTS (
                SELECT 1 FROM order_items i JOIN orders o ON o.order_id = i.order_id
                 WHERE i.order_item_id = m.order_item_id AND o.order_number = ANY (%(orders)s::text[])))
       AND NOT (m.movement_type = 'opening_balance' AND EXISTS (
                SELECT 1 FROM pl WHERE pl.stock_location_id = m.stock_location_id AND pl.product_id = m.product_id))
)"""

_VIOLATIONS = _BASE + """,
seqs AS (
    SELECT o.order_number, o.placed_at, substr(o.order_number, 9)::int AS seq,
           o.order_number <> ALL (%(orders)s::text[]) AS is_later
      FROM orders o
     WHERE o.order_number ~ '^[A-Z]{2}-[0-9]{4}-[0-9]{8}$'
),
v (rule, item) AS (
    -- orders ----------------------------------------------------------------------------
    SELECT 'order_not_after_boundary', order_number FROM fo WHERE placed_at <= %(boundary)s
    UNION ALL
    SELECT 'order_number_format', order_number FROM fo
     WHERE order_number !~ '^[A-Z]{2}-[0-9]{4}-[0-9]{8}$' OR substr(order_number, 1, 2) <> country_code
        OR substr(order_number, 4, 4) <> to_char(placed_local, 'YYYY')
    UNION ALL
    SELECT 'order_sequence_in_pilot_range', order_number FROM seqs WHERE is_later AND seq <= %(last_seq)s::int
    UNION ALL
    SELECT 'order_sequence_duplicate', s.order_number FROM seqs s
     WHERE s.is_later AND EXISTS (SELECT 1 FROM seqs t WHERE t.seq = s.seq AND t.order_number <> s.order_number)
    UNION ALL
    SELECT 'order_sequence_gap', 'sequence ' || lpad(g::text, 8, '0') || ' missing'
      FROM generate_series(%(last_seq)s::int + 1, (SELECT max(seq) FROM seqs WHERE is_later)) AS g
     WHERE NOT EXISTS (SELECT 1 FROM seqs WHERE seqs.seq = g)
    UNION ALL
    SELECT 'order_sequence_order', order_number
      FROM (SELECT order_number, placed_at, lag(placed_at) OVER (ORDER BY seq, order_number) AS previous
              FROM seqs WHERE is_later) x
     WHERE previous > placed_at
    UNION ALL
    SELECT 'order_in_future', fo.order_number FROM fo
     WHERE greatest(fo.placed_at, fo.confirmed_at, fo.fulfilled_at, fo.cancelled_at) > now()
        OR EXISTS (SELECT 1 FROM payment_attempts a WHERE a.order_id = fo.order_id
                    AND greatest(a.attempted_at, a.authorized_at, a.captured_at, a.failed_at) > now())
        OR EXISTS (SELECT 1 FROM order_items i JOIN inventory_reservations r ON r.order_item_id = i.order_item_id
                    WHERE i.order_id = fo.order_id AND greatest(r.reserved_at, r.closed_at) > now())
        OR EXISTS (SELECT 1 FROM order_items i JOIN inventory_movements m ON m.order_item_id = i.order_item_id
                    WHERE i.order_id = fo.order_id AND m.occurred_at > now())
    UNION ALL
    SELECT 'order_event_before_placed', fo.order_number FROM fo
     WHERE least(fo.confirmed_at, fo.fulfilled_at, fo.cancelled_at) < fo.placed_at
        OR EXISTS (SELECT 1 FROM payment_attempts a WHERE a.order_id = fo.order_id
                    AND least(a.attempted_at, a.authorized_at, a.captured_at, a.failed_at) < fo.placed_at)
        OR EXISTS (SELECT 1 FROM order_items i JOIN inventory_reservations r ON r.order_item_id = i.order_item_id
                    WHERE i.order_id = fo.order_id AND r.reserved_at < fo.placed_at)
        OR EXISTS (SELECT 1 FROM order_items i JOIN inventory_movements m ON m.order_item_id = i.order_item_id
                    WHERE i.order_id = fo.order_id AND m.occurred_at < fo.placed_at)
    UNION ALL
    SELECT 'order_customer', fo.order_number FROM fo LEFT JOIN customers cu ON cu.customer_id = fo.customer_id
     WHERE cu.customer_id IS NULL OR cu.country_code <> fo.country_code OR fo.placed_at < cu.registered_at
    UNION ALL
    SELECT 'order_lines_totals', fo.order_number FROM fo
     WHERE NOT EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = fo.order_id)
        OR fo.items_total <> (SELECT sum(i.line_total) FROM order_items i WHERE i.order_id = fo.order_id)
        OR fo.order_total <> fo.items_total + fo.shipping_fee
        OR (SELECT min(i.line_number) <> 1 OR max(i.line_number) <> count(*) FROM order_items i
             WHERE i.order_id = fo.order_id)
        OR EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = fo.order_id
                    AND i.line_total <> i.quantity * i.unit_price - i.discount_amount)
    UNION ALL
    SELECT 'order_line_price', fo.order_number || ' line ' || i.line_number
      FROM fo JOIN order_items i ON i.order_id = fo.order_id
     WHERE NOT EXISTS (SELECT 1 FROM product_prices pp
                        WHERE pp.product_id = i.product_id AND pp.country_code = fo.country_code
                          AND pp.valid_from <= fo.placed_at AND (pp.valid_to IS NULL OR fo.placed_at < pp.valid_to)
                          AND pp.list_price = i.unit_price AND pp.unit_cost = i.unit_cost)
    UNION ALL
    SELECT 'order_payments', fo.order_number FROM fo
     WHERE EXISTS (SELECT 1 FROM payment_attempts a WHERE a.order_id = fo.order_id AND a.amount <> fo.order_total)
        OR (SELECT count(*) FILTER (WHERE a.status = 'captured') FROM payment_attempts a WHERE a.order_id = fo.order_id)
           <> CASE WHEN fo.status IN ('confirmed', 'fulfilled') THEN 1 ELSE 0 END
        OR (SELECT coalesce(max(a.attempt_number), 0) <> count(*) FROM payment_attempts a
             WHERE a.order_id = fo.order_id)
    UNION ALL
    SELECT DISTINCT 'order_reservation_line', fo.order_number
      FROM fo JOIN order_items i ON i.order_id = fo.order_id
      JOIN inventory_reservations r ON r.order_item_id = i.order_item_id
     WHERE r.stock_location_id <> fo.fulfilment_location_id OR r.product_id <> i.product_id OR r.quantity <> i.quantity
    UNION ALL
    SELECT 'order_stock_status', fo.order_number FROM fo
     WHERE EXISTS (
             SELECT 1 FROM order_items i
              WHERE i.order_id = fo.order_id
                AND ((fo.status = 'fulfilled'
                      AND ((SELECT count(*) FROM inventory_movements m
                             WHERE m.order_item_id = i.order_item_id AND m.movement_type = 'sale'
                               AND m.quantity_change = -i.quantity AND m.stock_location_id = fo.fulfilment_location_id
                               AND m.occurred_at = fo.fulfilled_at) <> 1
                           OR NOT EXISTS (SELECT 1 FROM inventory_reservations r
                                           WHERE r.order_item_id = i.order_item_id AND r.status = 'consumed')))
                     OR (fo.status IN ('stock_reserved', 'confirmed')
                         AND NOT EXISTS (SELECT 1 FROM inventory_reservations r
                                          WHERE r.order_item_id = i.order_item_id AND r.status = 'active'))))
        OR (fo.status <> 'fulfilled' AND EXISTS (
             SELECT 1 FROM order_items i
              WHERE i.order_id = fo.order_id
                AND (EXISTS (SELECT 1 FROM inventory_movements m WHERE m.order_item_id = i.order_item_id)
                     OR EXISTS (SELECT 1 FROM inventory_reservations r
                                 WHERE r.order_item_id = i.order_item_id AND r.status = 'consumed'))))
        OR (fo.status NOT IN ('stock_reserved', 'confirmed') AND EXISTS (
             SELECT 1 FROM order_items i JOIN inventory_reservations r ON r.order_item_id = i.order_item_id
              WHERE i.order_id = fo.order_id AND r.status = 'active'))
    -- movements ---------------------------------------------------------------------------
    UNION ALL
    SELECT 'movement_not_simulated', label FROM fm WHERE kind IN ('stock_adjustment', 'write_off', 'return_restock')
    UNION ALL
    SELECT 'movement_not_after_boundary', label FROM fm
     WHERE kind IN ('purchase_receipt', 'transfer_out', 'transfer_in', 'sale') AND occurred_at <= %(boundary)s
    UNION ALL
    SELECT 'movement_in_future', label FROM fm WHERE occurred_at > now()
    UNION ALL
    SELECT 'opening_outside_pilot', label FROM fm
     WHERE kind = 'opening_balance'
       AND (launched_on IS NULL OR launched_on > local_at::date
            OR NOT EXISTS (SELECT 1 FROM inventory_movements o
                             JOIN pl ON pl.stock_location_id = o.stock_location_id AND pl.product_id = o.product_id
                             JOIN stock_locations ol ON ol.stock_location_id = o.stock_location_id
                            WHERE o.movement_type = 'opening_balance' AND ol.country_code = fm.country_code
                              AND o.occurred_at = fm.occurred_at
                              AND o.external_reference IS NOT DISTINCT FROM fm.external_reference))
    UNION ALL
    SELECT 'receipt_rules', label FROM fm
     WHERE kind = 'purchase_receipt'
       AND (location_type <> 'warehouse' OR external_reference IS NULL
            OR external_reference !~ '^PO-[A-Z]{2}-[0-9]{4}-[0-9]{6}/DN[0-9]+$'
            OR substr(external_reference, 4, 2) <> country_code)
    UNION ALL
    SELECT 'transfer_out_rules', label FROM fm
     WHERE kind = 'transfer_out'
       AND (location_type <> 'warehouse' OR external_reference IS NULL
            OR external_reference !~ '^TR-[A-Z]{2}-[0-9]{4}-[0-9]{6}-.+$'
            OR substr(external_reference, 4, 2) <> country_code
            OR substr(external_reference, 7, 4) <> to_char(local_at, 'YYYY')
            OR NOT EXISTS (SELECT 1 FROM stock_locations d JOIN stores s ON s.stock_location_id = d.stock_location_id
                            WHERE d.location_code = substr(fm.external_reference, 19)
                              AND d.country_code = fm.country_code))
    UNION ALL
    SELECT 'transfer_in_rules', label FROM fm
     WHERE kind = 'transfer_in'
       AND NOT EXISTS (SELECT 1 FROM inventory_movements o
                        WHERE o.movement_id = fm.related_movement_id AND o.movement_type = 'transfer_out'
                          AND o.product_id = fm.product_id AND o.quantity_change = -fm.quantity_change
                          AND o.external_reference = fm.external_reference
                          AND o.stock_location_id <> fm.stock_location_id
                          AND substr(o.external_reference, 19) = fm.location_code
                          AND o.occurred_at <= fm.occurred_at)
    UNION ALL
    SELECT 'document_reuses_pilot', label FROM fm WHERE document_number = ANY (%(own_docs)s::text[])
    UNION ALL
    SELECT 'document_identity', document_number FROM fm
     WHERE kind = 'purchase_receipt' AND document_number IS NOT NULL
     GROUP BY document_number HAVING count(DISTINCT stock_location_id) > 1
    UNION ALL
    SELECT 'document_identity', external_reference FROM fm
     WHERE kind = 'purchase_receipt' AND external_reference IS NOT NULL
     GROUP BY external_reference HAVING count(DISTINCT occurred_at) > 1
    UNION ALL
    SELECT 'document_identity', document_number FROM fm
     WHERE kind = 'transfer_out' AND document_number IS NOT NULL
     GROUP BY document_number
    HAVING count(DISTINCT stock_location_id) > 1 OR count(DISTINCT occurred_at) > 1
        OR count(DISTINCT external_reference) > 1
)
SELECT rule, count(*) AS n, array_to_string((array_agg(item ORDER BY item))[1:3], ', ') AS examples
  FROM v GROUP BY rule"""

_SUMMARY = _BASE + """
SELECT (SELECT count(*) FROM fo) AS orders,
       (SELECT count(*) FROM fo WHERE status IN ('placed', 'stock_reserved', 'confirmed')) AS open_orders,
       (SELECT min(placed_at) FROM fo WHERE placed_at > %(boundary)s) AS first_order_at,
       (SELECT min(occurred_at) FROM fm
         WHERE kind IN ('purchase_receipt', 'transfer_out', 'transfer_in', 'sale') AND occurred_at > %(boundary)s)
           AS first_movement_at,
       (SELECT count(*) FROM inventory_balances b
         WHERE NOT EXISTS (SELECT 1 FROM pl WHERE pl.stock_location_id = b.stock_location_id
                                              AND pl.product_id = b.product_id)) AS extra_listings,
       (SELECT count(*) FROM fm WHERE kind = 'opening_balance') AS extra_openings,
       (SELECT coalesce(sum(quantity_change), 0) FROM fm WHERE kind = 'opening_balance') AS extra_opening_units"""

_BY_TYPE = _BASE + """
SELECT kind, count(*), coalesce(sum(quantity_change), 0) FROM fm
 WHERE kind IN ('purchase_receipt', 'transfer_out', 'transfer_in', 'sale') AND occurred_at > %(boundary)s
 GROUP BY kind"""

_ON_HAND = _BASE + """
SELECT location_code, sku, sum(quantity_change) FROM fm
 WHERE at_pilot_listing AND kind IN ('purchase_receipt', 'transfer_out', 'transfer_in', 'sale')
   AND occurred_at > %(boundary)s
 GROUP BY 1, 2"""

_RESERVED = _BASE + """
SELECT l.location_code, p.sku, sum(r.quantity)
  FROM inventory_reservations r
  JOIN order_items i ON i.order_item_id = r.order_item_id
  JOIN orders o ON o.order_id = i.order_id
  JOIN pl ON pl.stock_location_id = r.stock_location_id AND pl.product_id = r.product_id
  JOIN stock_locations l ON l.stock_location_id = r.stock_location_id
  JOIN products p ON p.product_id = r.product_id
 WHERE r.status = 'active' AND o.order_number <> ALL (%(orders)s::text[])
 GROUP BY 1, 2"""


def read_later_activity(conn: psycopg.Connection, scope: PilotScope) -> LaterActivity:
    """Classify every foreign record against the scope; read-only."""
    params = scope.params()
    out = LaterActivity()
    found = {rule: (n, examples) for rule, n, examples in conn.execute(_VIOLATIONS, params).fetchall()}
    words = {"name": scope.name, "boundary": scope.boundary.isoformat(), "last_seq": scope.last_order_sequence}
    for rule, text in RULES.items():                      # stable order, as listed above
        if rule in found:
            n, examples = found[rule]
            out.problems.append(f"{n} {text.format(**words)} (e.g. {examples})")
    (out.orders, out.open_orders, first_order, first_movement, out.extra_listings, out.extra_opening_balances,
     out.extra_opening_units) = conn.execute(_SUMMARY, params).fetchone()
    out.first_at = min((t for t in (first_order, first_movement) if t is not None), default=None)
    for kind, n, units in conn.execute(_BY_TYPE, params).fetchall():
        out.by_type[kind] = n
        out.movements += n
        out.net_units += int(units)
    out.on_hand_change = {(loc, sku): int(change) for loc, sku, change in conn.execute(_ON_HAND, params).fetchall()}
    out.reserved = {(loc, sku): int(units) for loc, sku, units in conn.execute(_RESERVED, params).fetchall()}
    return out


def expected_stock(replayed: dict[Key, tuple[int, int]], scope: PilotScope,
                   later: LaterActivity) -> dict[Key, tuple[int, int]]:
    """(on hand, reserved) per pilot listing: the pilot's own replay plus the accepted later activity."""
    out = {}
    for key in sorted(scope.listings):
        on_hand, reserved = replayed.get(key, (0, 0))
        out[key] = (on_hand + later.on_hand_change.get(key, 0), reserved + later.reserved.get(key, 0))
    return out
