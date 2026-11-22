"""Order units of work U1 to U5 (V006/V007), moved unchanged from transactions/load.py in S1.

Each function is the body of ONE transaction; the caller opens it (base.run_unit).
    place                U1   INSERT orders (placed) and order_items, totals already computed
    reserve              U2   fn_reserve_order_stock; out_of_stock rolls the transaction back
                              (psycopg.Rollback) so a NEW transaction can cancel the order
    cancel_out_of_stock  U2   order cancelled (out_of_stock)
    payment_start        U3a  INSERT payment_attempts (pending)
    payment_result       U3c  lock the order, then captured (order confirmed) or failed; after
                              the last failure the reservations are released and the order is
                              payment_failed, in the same transaction
    customer_cancel           reservations released, order cancelled (customer_request)
    expire               U4   lock FOR UPDATE SKIP LOCKED, reservations expired, authorizations
                              voided, order cancelled (reservation_expired)
    fulfil               U5   fn_fulfil_order_stock: reservations consumed, sale movements
Stock quantities are never written directly; every V006/V007 rule runs on every commit.
Every result is checked (expect); a difference raises OutcomeMismatchError and rolls back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg

from .base import Ids, expect

if TYPE_CHECKING:
    from ..transactions.model import PlannedOrder


def place(conn, ids: Ids, o: "PlannedOrder") -> str:
    if conn.execute("SELECT 1 FROM orders WHERE order_number = %s", (o.order_number,)).fetchone():
        return "already placed"
    order_id = conn.execute(
        "INSERT INTO orders (order_number, customer_id, channel, store_id, fulfilment_location_id, country_code, "
        "currency_code, items_total, shipping_fee, shipping_vat_amount, order_total, shipping_address_line1, "
        "shipping_postal_code, shipping_city, placed_at) "
        "VALUES (%s, %s, %s::sales_channel, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING order_id",
        (o.order_number, ids.customer[o.customer_number], o.channel, ids.store.get(o.store_code) if o.store_code else None,
         ids.location[o.fulfilment_location_code], o.country_code, o.currency_code, o.items_total, o.shipping_fee,
         o.shipping_vat_amount, o.order_total, o.shipping_address_line1, o.shipping_postal_code, o.shipping_city,
         o.placed_at)).fetchone()[0]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost, "
            "discount_percent, discount_amount, line_total, vat_rate, vat_amount) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(order_id, x.line_number, ids.product[x.sku], x.quantity, x.unit_price, x.unit_cost, x.discount_percent,
              x.discount_amount, x.line_total, x.vat_rate, x.vat_amount) for x in o.lines])
    return f"placed, {len(o.lines)} lines, total {o.order_total} {o.currency_code}"


def reserve(conn, ids: Ids, o: "PlannedOrder") -> str:
    order_id = ids.order(conn, o.order_number)
    result = conn.execute("SELECT fn_reserve_order_stock(%s, %s, %s)",
                          (order_id, o.reserve_at, o.reservation_expires_at)).fetchone()[0]
    if o.reserve_outcome == "out_of_stock":
        expect(result == "out_of_stock", f"{o.order_number}: U2 returned {result}, the plan expects out_of_stock")
        raise psycopg.Rollback()       # design: roll back, then cancel in a new transaction
    expect(result in ("reserved", "already_reserved"), f"{o.order_number}: U2 returned {result}, the plan expects reserved")
    return result


def cancel_out_of_stock(conn, ids: Ids, o: "PlannedOrder") -> str:
    order_id = ids.order(conn, o.order_number)
    n = conn.execute("UPDATE orders SET status = 'cancelled', cancellation_reason = 'out_of_stock', cancelled_at = %s "
                     "WHERE order_id = %s AND status = 'placed'", (o.cancelled_at, order_id)).rowcount
    expect(n == 1, f"{o.order_number}: the order was not placed when cancelling it (out_of_stock)")
    return "cancelled (out_of_stock)"


def payment_start(conn, ids: Ids, o: "PlannedOrder", number: int) -> str:
    order_id = ids.order(conn, o.order_number)
    a = o.attempts[number - 1]
    if conn.execute("SELECT 1 FROM payment_attempts WHERE order_id = %s AND attempt_number = %s",
                    (order_id, number)).fetchone():
        return "already started"
    conn.execute("INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, "
                 "attempted_at) VALUES (%s, %s, %s::payment_method, %s, %s, %s)",
                 (order_id, number, a.payment_method, a.payment_provider, a.amount, a.attempted_at))
    return f"pending ({a.payment_method}, {a.amount} {o.currency_code})"


def lock_order(conn, order_id: int, number: str, skip_locked: bool = False) -> str:
    row = conn.execute("SELECT status::text FROM orders WHERE order_id = %s FOR UPDATE" +
                       (" SKIP LOCKED" if skip_locked else ""), (order_id,)).fetchone()
    expect(row is not None, f"{number}: the order row is locked by another session")
    return row[0]


def payment_result(conn, ids: Ids, o: "PlannedOrder", number: int) -> str:
    order_id = ids.order(conn, o.order_number)
    a = o.attempts[number - 1]
    status = lock_order(conn, order_id, o.order_number)            # lock order first (design lock order)
    expect(status == "stock_reserved", f"{o.order_number}: the order is {status}, a payment result needs stock_reserved")
    if a.outcome == "captured":
        missing = conn.execute(
            "SELECT count(*) FROM order_items i WHERE i.order_id = %s AND NOT EXISTS (SELECT 1 FROM inventory_reservations r "
            "WHERE r.order_item_id = i.order_item_id AND r.status = 'active')", (order_id,)).fetchone()[0]
        expect(missing == 0, f"{o.order_number}: {missing} lines hold no active reservation; the plan expects a capture")
        n1 = conn.execute("UPDATE payment_attempts SET status = 'authorized', authorized_at = %s, provider_reference = %s "
                          "WHERE order_id = %s AND attempt_number = %s AND status = 'pending'",
                          (a.authorized_at, a.provider_reference, order_id, number)).rowcount
        n2 = conn.execute("UPDATE payment_attempts SET status = 'captured', captured_at = %s "
                          "WHERE order_id = %s AND attempt_number = %s AND status = 'authorized'",
                          (a.captured_at, order_id, number)).rowcount
        n3 = conn.execute("UPDATE orders SET status = 'confirmed', confirmed_at = %s "
                          "WHERE order_id = %s AND status = 'stock_reserved'", (o.confirmed_at, order_id)).rowcount
        expect((n1, n2, n3) == (1, 1, 1), f"{o.order_number}: capture of attempt {number} changed {(n1, n2, n3)} rows")
        return "captured, order confirmed"
    final = o.final_status == "payment_failed" and number == o.attempts[-1].attempt_number
    if final:   # balances and reservations before the payment row (design lock order)
        closed = conn.execute("SELECT fn_close_order_reservations(%s, 'released', %s)", (order_id, a.failed_at)).fetchone()[0]
        expect(closed == len(o.lines), f"{o.order_number}: released {closed} reservations, expected {len(o.lines)}")
    n = conn.execute("UPDATE payment_attempts SET status = 'failed', failed_at = %s, failure_reason = %s, "
                     "provider_reference = %s WHERE order_id = %s AND attempt_number = %s AND status = 'pending'",
                     (a.failed_at, a.failure_reason, a.provider_reference, order_id, number)).rowcount
    expect(n == 1, f"{o.order_number}: attempt {number} was not pending")
    if final:
        n = conn.execute("UPDATE orders SET status = 'payment_failed', cancelled_at = %s "
                         "WHERE order_id = %s AND status = 'stock_reserved'", (o.cancelled_at, order_id)).rowcount
        expect(n == 1, f"{o.order_number}: could not set payment_failed")
        return f"failed ({a.failure_reason}), last attempt: order payment_failed, {len(o.lines)} reservations released"
    return f"failed ({a.failure_reason})"


def customer_cancel(conn, ids: Ids, o: "PlannedOrder") -> str:
    order_id = ids.order(conn, o.order_number)
    status = lock_order(conn, order_id, o.order_number)
    expect(status == "stock_reserved", f"{o.order_number}: the order is {status}, cancelling needs stock_reserved")
    closed = conn.execute("SELECT fn_close_order_reservations(%s, 'released', %s)", (order_id, o.cancelled_at)).fetchone()[0]
    expect(closed == len(o.lines), f"{o.order_number}: released {closed} reservations, expected {len(o.lines)}")
    n = conn.execute("UPDATE orders SET status = 'cancelled', cancellation_reason = 'customer_request', cancelled_at = %s "
                     "WHERE order_id = %s AND status = 'stock_reserved'", (o.cancelled_at, order_id)).rowcount
    expect(n == 1, f"{o.order_number}: could not cancel")
    return f"cancelled (customer_request), {closed} reservations released"


def expire(conn, ids: Ids, o: "PlannedOrder") -> str:
    order_id = ids.order(conn, o.order_number)
    status = lock_order(conn, order_id, o.order_number, skip_locked=True)     # U4 sweeper
    expect(status == "stock_reserved", f"{o.order_number}: the order is {status}, expiry needs stock_reserved")
    closed = conn.execute("SELECT fn_close_order_reservations(%s, 'expired', %s)",
                          (order_id, o.reservation_closed_at)).fetchone()[0]
    expect(closed == len(o.lines), f"{o.order_number}: expired {closed} reservations, expected {len(o.lines)}")
    voided = conn.execute("UPDATE payment_attempts SET status = 'voided' WHERE order_id = %s AND status = 'authorized'",
                          (order_id,)).rowcount
    n = conn.execute("UPDATE orders SET status = 'cancelled', cancellation_reason = 'reservation_expired', cancelled_at = %s "
                     "WHERE order_id = %s AND status = 'stock_reserved'", (o.cancelled_at, order_id)).rowcount
    expect(n == 1, f"{o.order_number}: could not cancel after expiry")
    return f"cancelled (reservation_expired), {closed} reservations expired, {voided} authorizations voided"


def fulfil(conn, ids: Ids, o: "PlannedOrder") -> str:
    order_id = ids.order(conn, o.order_number)
    result = conn.execute("SELECT fn_fulfil_order_stock(%s, %s)", (order_id, o.fulfilled_at)).fetchone()[0]
    expect(result in ("fulfilled", "already_fulfilled"), f"{o.order_number}: U5 returned {result}")
    return f"{result}, {o.units} units issued from {o.fulfilment_location_code}"
