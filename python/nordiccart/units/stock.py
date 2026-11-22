"""Replenishment units of work R0, R1, T1, T2 (V007), moved unchanged from replenishment/load.py in S1.

Each function is the body of ONE transaction; the caller opens it (base.run_unit). They take
the replenishment documents (replenishment.model) and a label for messages.
    revise_reorder_points  R0  lock the warehouse's balance rows FOR UPDATE in (stock_location_id,
                               product_id) order, then guarded UPDATE ... WHERE reorder_point = old
    receive                R1  lock in lock order, check storage capacity, one purchase_receipt per
                               line (external_reference = <PO number>/DN<n>)
    dispatch               T1  lock the source warehouse's rows, check on hand - reserved per line,
                               one transfer_out per line (external_reference = TR-...)
    arrive                 T2  lock the destination store's rows, find each transfer_out by its
                               business key, insert the transfer_in with related_movement_id
Stock quantities are never written directly: the V007 triggers move them and the V007 CHECKs
and guards run on every insert.

V009 (stage S4), only when the migration is applied (has_activity_control):
    revise_reorder_points  also records each change in reorder_point_revisions (kind pilot_r0,
                           business time = the revision time, reference R0-<warehouse>-<date>),
                           in the same transaction (decision C18)
    receive                first makes sure the purchase order document exists and equals the
                           plan (ensure_purchase_order); the V009 receipt trigger then checks the
                           receipt against it and moves the received quantities
    ensure_purchase_order / record_reorder_point_revisions are also the document backfill of a
    pilot loaded before V009 (exception E3): documents only, no movement is written.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Ids, expect, has_activity_control

if TYPE_CHECKING:
    from ..replenishment.model import Delivery, PurchaseOrder, ReorderPointRevision, TransferDocument  # noqa: F401


def lock_balances(conn, location_id: int, product_ids: list[int], label: str) -> dict[int, tuple[int, int, int]]:
    """Lock the balance rows FOR UPDATE in the design lock order (stock_location_id, product_id)."""
    rows = conn.execute(
        "SELECT product_id, quantity_on_hand, quantity_reserved, reorder_point FROM inventory_balances "
        "WHERE stock_location_id = %s AND product_id = ANY(%s) ORDER BY stock_location_id, product_id FOR UPDATE",
        (location_id, product_ids)).fetchall()
    expect(len(rows) == len(set(product_ids)), f"{label}: a product is not listed at the location")
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def revise_reorder_points(conn, ids: Ids, r: "ReorderPointRevision", label: str) -> str:
    loc = ids.location[r.warehouse]
    changes = sorted(r.changes, key=lambda c: ids.product[c.sku])
    lock_balances(conn, loc, [ids.product[c.sku] for c in changes], label)
    for c in changes:
        n = conn.execute("UPDATE inventory_balances SET reorder_point = %s "
                         "WHERE stock_location_id = %s AND product_id = %s AND reorder_point = %s",
                         (c.new, loc, ids.product[c.sku], c.old)).rowcount
        expect(n == 1, f"{r.warehouse}/{c.sku}: reorder point was not {c.old}")
    history = ""
    if has_activity_control(conn):
        record_reorder_point_revisions(conn, ids, r)
        history = f", recorded as {r.reference}"
    return (f"{len(changes)} reorder points {sum(c.old for c in changes)} -> {sum(c.new for c in changes)} "
            f"(echelon demand){history}")


def record_reorder_point_revisions(conn, ids: Ids, r: "ReorderPointRevision") -> int:
    """V009: one reorder_point_revisions row per change of the R0 revision (kind pilot_r0).

    The V009 guard locks each balance row and checks business-time order, the chain and that the
    balance holds the old or new value; at commit the latest revision must equal the balance.
    """
    loc = ids.location[r.warehouse]
    changes = sorted(r.changes, key=lambda c: ids.product[c.sku])
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO reorder_point_revisions (stock_location_id, product_id, revision_kind, effective_at, "
            "old_reorder_point, new_reorder_point, document_reference) VALUES (%s, %s, 'pilot_r0', %s, %s, %s, %s)",
            [(loc, ids.product[c.sku], r.at, c.old, c.new, r.reference) for c in changes])
    return len(changes)


def review_reorder_points(conn, ids: Ids, location: str, at, changes, reference: str, label: str) -> str:
    """R-cal (S6, design 6): one location's monthly reorder point review, kind monthly_review.

    `changes` are (sku, old, new) with old != new. Same locking and guarded UPDATE as R0, and one
    reorder_point_revisions row per change in the same transaction (V009 chain and commit checks).
    """
    loc = ids.location[location]
    ordered = sorted(changes, key=lambda c: ids.product[c[0]])
    lock_balances(conn, loc, [ids.product[sku] for sku, _, _ in ordered], label)
    for sku, old, new in ordered:
        n = conn.execute("UPDATE inventory_balances SET reorder_point = %s "
                         "WHERE stock_location_id = %s AND product_id = %s AND reorder_point = %s",
                         (new, loc, ids.product[sku], old)).rowcount
        expect(n == 1, f"{location}/{sku}: reorder point was not {old}")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO reorder_point_revisions (stock_location_id, product_id, revision_kind, effective_at, "
            "old_reorder_point, new_reorder_point, document_reference) VALUES (%s, %s, 'monthly_review', %s, %s, %s, %s)",
            [(loc, ids.product[sku], at, old, new, reference) for sku, old, new in ordered])
    return (f"{len(ordered)} reorder points {sum(c[1] for c in ordered)} -> {sum(c[2] for c in ordered)} "
            f"(R-cal), recorded as {reference}")


def ensure_purchase_order(conn, ids: Ids, po: "PurchaseOrder", label: str) -> bool:
    """V009: the purchase order document exists and equals the plan. Returns True when it was inserted.

    Inserts the header and all lines in the current transaction (the V009 commit check requires an
    order with its lines). A line inserted after its receipts takes the received quantity from the
    ledger (exception E3). An existing document with other terms is a mismatch: rolled back.
    """
    loc = ids.location[po.warehouse]
    planned = sorted((x.line_number, ids.product[x.sku], x.trigger, x.quantity) for x in po.lines)
    row = conn.execute("SELECT purchase_order_id, stock_location_id, ordered_at FROM purchase_orders "
                       "WHERE purchase_order_number = %s", (po.number,)).fetchone()
    if row is not None:
        expect((row[1], row[2]) == (loc, po.ordered_at),
               f"{label}: purchase order {po.number} exists for another warehouse or order time")
        lines = conn.execute("SELECT line_number, product_id, order_reason::text, quantity_ordered "
                             "FROM purchase_order_lines WHERE purchase_order_id = %s ORDER BY line_number",
                             (row[0],)).fetchall()
        expect(sorted(tuple(x) for x in lines) == planned,
               f"{label}: purchase order {po.number} has other lines than the plan")
        return False
    order_id = conn.execute("INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at) "
                            "VALUES (%s, %s, %s) RETURNING purchase_order_id",
                            (po.number, loc, po.ordered_at)).fetchone()[0]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, "
            "order_reason, quantity_ordered) VALUES (%s, %s, %s, %s, %s, %s)",
            [(order_id, n, loc, product, reason, quantity) for n, product, reason, quantity in planned])
    return True


def receive(conn, ids: Ids, po: "PurchaseOrder", d: "Delivery", label: str) -> str:
    loc = ids.location[po.warehouse]
    lines = sorted(d.lines, key=lambda x: ids.product[x.sku])
    lock_balances(conn, loc, [ids.product[x.sku] for x in lines], label)
    row = conn.execute("SELECT storage_capacity_units FROM warehouses WHERE stock_location_id = %s", (loc,)).fetchone()
    expect(row is not None, f"{label}: {po.warehouse} is not a warehouse")
    if row[0] is not None:
        total = conn.execute("SELECT coalesce(sum(quantity_on_hand), 0) FROM inventory_balances "
                             "WHERE stock_location_id = %s", (loc,)).fetchone()[0]
        expect(total + d.units <= row[0], f"{label}: {po.warehouse} would hold {total + d.units} units, "
                                          f"capacity {row[0]}")
    document = ""
    if has_activity_control(conn) and ensure_purchase_order(conn, ids, po, label):
        document = f", purchase order {po.number} recorded"
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, "
            "occurred_at, external_reference) VALUES (%s, %s, 'purchase_receipt', %s, %s, %s)",
            [(loc, ids.product[x.sku], x.quantity, d.received_at, d.reference) for x in lines])
    trigger = "forced_reorder" if po.forced else "reorder point"
    return f"received {d.units} units, {len(lines)} lines at {po.warehouse} ({trigger}){document}"


def dispatch(conn, ids: Ids, t: "TransferDocument", label: str) -> str:
    src = ids.location[t.source]
    lines = sorted(t.lines, key=lambda x: ids.product[x.sku])
    locked = lock_balances(conn, src, [ids.product[x.sku] for x in lines], label)
    for x in lines:
        on_hand, reserved, _ = locked[ids.product[x.sku]]
        expect(on_hand - reserved >= x.quantity, f"{label}: {t.source} has {on_hand - reserved} {x.sku} available, "
                                                 f"the plan dispatches {x.quantity}")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, "
            "occurred_at, external_reference) VALUES (%s, %s, 'transfer_out', %s, %s, %s)",
            [(src, ids.product[x.sku], -x.quantity, t.dispatched_at, t.reference) for x in lines])
    return f"dispatched {t.units} units, {len(lines)} lines {t.source} -> {t.destination}"


def arrive(conn, ids: Ids, t: "TransferDocument", label: str) -> str:
    src, dst = ids.location[t.source], ids.location[t.destination]
    lines = sorted(t.lines, key=lambda x: ids.product[x.sku])
    lock_balances(conn, dst, [ids.product[x.sku] for x in lines], label)
    rows = []
    for x in lines:
        found = conn.execute(
            "SELECT movement_id FROM inventory_movements WHERE movement_type = 'transfer_out' "
            "AND stock_location_id = %s AND product_id = %s AND external_reference = %s",
            (src, ids.product[x.sku], t.reference)).fetchone()
        expect(found is not None, f"{label}: no transfer_out for {x.sku}")
        rows.append((dst, ids.product[x.sku], x.quantity, t.arrived_at, found[0], t.reference))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, "
            "occurred_at, related_movement_id, external_reference) VALUES (%s, %s, 'transfer_in', %s, %s, %s, %s)", rows)
    return f"arrived {t.units} units at {t.destination}"
