#!/usr/bin/env python3
"""NordicCart OLTP | Concurrency and failure-recovery tests for V007 (inventory).

The SQL behaviour tests run in one session. This script opens several real
PostgreSQL sessions at once to prove what one session cannot:

  1. Last units: 10 sessions race through U2 for 5 units; exactly 5 win.
  2. Without the U2 lock, the balance CHECK still stops overselling.
  3. Multi-line orders in opposite line order never deadlock (lock order),
     both when queued behind a held row and in a free race.
  4. Two concurrent U5 retries issue stock once (idempotent).
  5. Expiry sweeper first, then capture: the capture finds nothing to capture.
  6. Capture first, then sweeper: SKIP LOCKED leaves the order alone.
  7. A session killed in the middle of U2 leaves no trace; a retry succeeds.
  8. 20 concurrent receipts on one balance: no lost update.
  9. REPEATABLE READ conflict: 40001 on the first try, the retry succeeds.
 10. The same receipt sent twice at once is stored once (idempotency key).
 11. A transfer dispatch (replenishment loader style: lock, check, insert) races U2 for the
     same warehouse stock: exactly one gets it; nothing is created or lost.
 12. A transfer that ignores the lock cannot take reserved units: the balance CHECK refuses it.
 13. The same transfer arrival recorded by two sessions at once is stored once.
 14. After everything: on_hand = SUM(movements), reserved = SUM(active reservations).

Safety
  * The script creates its OWN database, nordiccart_concurrency_test, applies
    database/migrations/V001 to V007 to it, runs the scenarios and drops it.
    It never connects to nordiccart_oltp and refuses to use any other name.
  * If nordiccart_concurrency_test already exists it stops, unless --recreate.
  * --keep leaves the test database in place for inspection.

Requirements
  Python 3.9+, psycopg 3 (pip install -r tests/python/requirements.txt).
  Connection settings come from the standard libpq variables PGHOST, PGPORT,
  PGUSER, PGPASSWORD (see config/.env.example). The role must be allowed to
  create databases.

Usage
  python tests/python/test_v007_concurrency.py [--recreate] [--keep] [--through 8|9 --role nordiccart_loader]
Exit code 0 when every scenario passes, 1 otherwise.

V008 (Phase 3, step 5, stage S3): --through 8 also applies V008 (security), and --role runs the
fixtures and every scenario session as that login role (for example nordiccart_loader), so the
races run under the least-privilege loader. Migrations and the session kill of scenario 7 still
use the connecting (superuser) role. Without these options the script behaves as before.
V009 (stage S4): --through 9 also applies V009 (purchase orders, revisions, run ledger), with the
new receipt trigger on inventory_movements in place; the scenarios' receipts use non-PO references
and keep their V007 behaviour.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import psycopg
from psycopg import errors

TEST_DB = "nordiccart_concurrency_test"
ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = ROOT / "database" / "migrations"
LAST_VERSION = 7          # --through 8 applies V008 as well, --through 9 also V009
SCENARIO_ROLE = None      # --role: login role of the fixture and scenario sessions

VAT = Decimal("0.2500")
UNIT_PRICE = Decimal("100.00")
UNIT_COST = Decimal("60.00")
NOW = datetime.now(timezone.utc).replace(microsecond=0)
PLACED_AT = NOW - timedelta(hours=2)
RESERVED_AT = PLACED_AT + timedelta(minutes=1)
EXPIRES_AT = RESERVED_AT + timedelta(minutes=15)


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def connect(dbname: str, autocommit: bool = False, admin: bool = False) -> psycopg.Connection:
    if dbname not in ("postgres", TEST_DB):
        raise SystemExit(f"Refusing to connect to {dbname!r}: this script only uses {TEST_DB!r}.")
    extra = {"user": SCENARIO_ROLE} if SCENARIO_ROLE and dbname == TEST_DB and not admin else {}
    conn = psycopg.connect(dbname=dbname, autocommit=autocommit, **extra)
    conn.execute("SET lock_timeout = '20s'")
    conn.execute("SET search_path = public")
    if not autocommit:
        conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Test database lifecycle
# ---------------------------------------------------------------------------
def create_test_database(recreate: bool) -> None:
    with connect("postgres", autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone()
        if exists and not recreate:
            raise SystemExit(f"{TEST_DB} already exists. Run with --recreate to drop and rebuild it.")
        if exists:
            drop_test_database()
        admin.execute(f"CREATE DATABASE {TEST_DB} WITH ENCODING 'UTF8' TEMPLATE template0")
        admin.execute(f"ALTER DATABASE {TEST_DB} SET timezone TO 'UTC'")


def drop_test_database() -> None:
    with connect("postgres", autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (TEST_DB,),
        )
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")


def apply_migrations() -> list[str]:
    files = sorted(MIGRATIONS_DIR.glob("V*.sql"))
    selected = [f for f in files if int(f.name[1:4]) <= LAST_VERSION]
    if [int(f.name[1:4]) for f in selected] != list(range(1, LAST_VERSION + 1)):
        raise SystemExit(f"Expected V001 to V{LAST_VERSION:03d} in {MIGRATIONS_DIR}, found {[f.name for f in selected]}")
    with connect(TEST_DB, autocommit=True, admin=True) as conn:
        for f in selected:
            conn.execute(f.read_text(encoding="utf-8"))  # each file is its own BEGIN ... COMMIT
    return [f.name for f in selected]


# ---------------------------------------------------------------------------
# Fixtures (committed in the test database only)
# ---------------------------------------------------------------------------
def setup_fixtures(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("INSERT INTO currencies (currency_code, currency_name) VALUES ('XTS', 'Test currency')")
        conn.execute(
            "INSERT INTO countries (country_code, country_name, currency_code, vat_rate, time_zone) "
            "VALUES ('XA', 'Testland A', 'XTS', 0.2500, 'Europe/Stockholm')"
        )
        conn.execute("INSERT INTO product_categories (category_code, category_name) VALUES ('CT', 'Concurrency test')")
        for n in range(1, 11):
            conn.execute(
                "INSERT INTO products (sku, product_name, brand, category_id) "
                "SELECT %s, %s, 'TestBrand', category_id FROM product_categories WHERE category_code = 'CT'",
                (f"CT-P{n}", f"Concurrency product {n}"),
            )
        conn.execute(
            "INSERT INTO customers (customer_number, first_name, last_name, email, country_code, signup_channel, registered_at) "
            "VALUES ('CT-C1', 'Test', 'Customer', 'ct.c1@example.invalid', 'XA', 'online', %s)",
            (PLACED_AT - timedelta(days=30),),
        )
        conn.execute(
            "INSERT INTO stock_locations (location_code, location_name, location_type, country_code, "
            "address_line1, postal_code, city, opened_on) "
            "VALUES ('CT-WH-1', 'Concurrency warehouse', 'warehouse', 'XA', 'Testvägen 1', '12345', 'Testby', '2024-01-01')"
        )
        conn.execute("INSERT INTO warehouses (stock_location_id) SELECT stock_location_id FROM stock_locations WHERE location_code = 'CT-WH-1'")
        # A store of the same market for the transfer scenarios (11 to 13).
        conn.execute(
            "INSERT INTO stock_locations (location_code, location_name, location_type, country_code, "
            "address_line1, postal_code, city, opened_on) "
            "VALUES ('CT-STO-1', 'Concurrency store', 'store', 'XA', 'Testgatan 2', '12345', 'Testby', '2024-01-01')"
        )
        conn.execute("INSERT INTO stores (stock_location_id) SELECT stock_location_id FROM stock_locations WHERE location_code = 'CT-STO-1'")
        # P8 5 (transfer against U2), P9 4 (transfer into reserved units), P10 6 (duplicate arrival):
        # listed at the warehouse with opening stock and at the store at 0.
        for sku, qty in [("CT-P8", 5), ("CT-P9", 4), ("CT-P10", 6)]:
            for location in ("CT-WH-1", "CT-STO-1"):
                conn.execute(
                    "INSERT INTO inventory_balances (stock_location_id, product_id) "
                    "SELECT l.stock_location_id, p.product_id FROM stock_locations l, products p "
                    "WHERE l.location_code = %s AND p.sku = %s",
                    (location, sku),
                )
            conn.execute(
                "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at) "
                "SELECT l.stock_location_id, p.product_id, 'opening_balance', %s, %s FROM stock_locations l, products p "
                "WHERE l.location_code = 'CT-WH-1' AND p.sku = %s",
                (qty, PLACED_AT - timedelta(days=1), sku),
            )
        # Opening stock per product: P1 5 (last units), P2 3 (naive race), P3/P4 1000 (deadlock test),
        # P5 0 (lost update), P6 10 (serialization retry), P7 100 (fulfilment, capture races, crash)
        for sku, qty in [("CT-P1", 5), ("CT-P2", 3), ("CT-P3", 1000), ("CT-P4", 1000), ("CT-P5", 0), ("CT-P6", 10), ("CT-P7", 100)]:
            conn.execute(
                "INSERT INTO inventory_balances (stock_location_id, product_id) "
                "SELECT l.stock_location_id, p.product_id FROM stock_locations l, products p "
                "WHERE l.location_code = 'CT-WH-1' AND p.sku = %s",
                (sku,),
            )
            if qty:
                conn.execute(
                    "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at) "
                    "SELECT l.stock_location_id, p.product_id, 'opening_balance', %s, %s FROM stock_locations l, products p "
                    "WHERE l.location_code = 'CT-WH-1' AND p.sku = %s",
                    (qty, PLACED_AT - timedelta(days=1), sku),
                )


def place_order(conn: psycopg.Connection, order_number: str, lines: list[tuple[str, int]]) -> int:
    """U1: insert an online order and its lines in one transaction; totals computed with Decimal."""
    computed = []
    for sku, qty in lines:
        line_total = money(UNIT_PRICE * qty)
        vat_amount = money(line_total * VAT / (1 + VAT))
        computed.append((sku, qty, line_total, vat_amount))
    items_total = sum((c[2] for c in computed), Decimal("0.00"))
    with conn.transaction():
        order_id = conn.execute(
            "INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code, "
            "items_total, order_total, shipping_address_line1, shipping_postal_code, shipping_city, placed_at) "
            "SELECT %s, c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS', %s, %s, 'Testgatan 5', '12345', 'Testby', %s "
            "FROM customers c, stock_locations l WHERE c.customer_number = 'CT-C1' AND l.location_code = 'CT-WH-1' "
            "RETURNING order_id",
            (order_number, items_total, items_total, PLACED_AT),
        ).fetchone()[0]
        for line_number, (sku, qty, line_total, vat_amount) in enumerate(computed, start=1):
            conn.execute(
                "INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost, line_total, vat_rate, vat_amount) "
                "SELECT %s, %s, product_id, %s, %s, %s, %s, %s, %s FROM products WHERE sku = %s",
                (order_id, line_number, qty, UNIT_PRICE, UNIT_COST, line_total, VAT, vat_amount, sku),
            )
    return order_id


def reserve(conn: psycopg.Connection, order_id: int) -> str:
    """U2 in its own transaction. Out of stock: roll back, then cancel in a new transaction (design)."""
    with conn.transaction():
        result = conn.execute(
            "SELECT fn_reserve_order_stock(%s, %s, %s)", (order_id, RESERVED_AT, EXPIRES_AT)
        ).fetchone()[0]
    if result == "out_of_stock":
        with conn.transaction():
            conn.execute(
                "UPDATE orders SET status = 'cancelled', cancelled_at = %s, cancellation_reason = 'out_of_stock' "
                "WHERE order_id = %s AND status = 'placed'",
                (RESERVED_AT, order_id),
            )
    return result


def authorize(conn: psycopg.Connection, order_id: int) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at) "
            "SELECT order_id, 1, 'card', 'TestPSP', order_total, %s FROM orders WHERE order_id = %s",
            (RESERVED_AT + timedelta(minutes=1), order_id),
        )
    with conn.transaction():
        conn.execute(
            "UPDATE payment_attempts SET status = 'authorized', authorized_at = %s WHERE order_id = %s AND status = 'pending'",
            (RESERVED_AT + timedelta(minutes=2), order_id),
        )


def capture_and_confirm(conn: psycopg.Connection, order_id: int) -> int:
    """U3c: lock the order, capture (guarded), then confirm. Returns rows captured (0 or 1)."""
    with conn.transaction():
        conn.execute("SELECT 1 FROM orders WHERE order_id = %s FOR UPDATE", (order_id,))
        captured = conn.execute(
            "UPDATE payment_attempts SET status = 'captured', captured_at = %s "
            "WHERE order_id = %s AND status = 'authorized'",
            (RESERVED_AT + timedelta(minutes=3), order_id),
        ).rowcount
        if captured:
            conn.execute(
                "UPDATE orders SET status = 'confirmed', confirmed_at = %s WHERE order_id = %s AND status = 'stock_reserved'",
                (RESERVED_AT + timedelta(minutes=3), order_id),
            )
    return captured


def stock(conn: psycopg.Connection, sku: str, location: str = "CT-WH-1") -> tuple[int, int]:
    row = conn.execute(
        "SELECT b.quantity_on_hand, b.quantity_reserved FROM inventory_balances b "
        "JOIN products p ON p.product_id = b.product_id "
        "JOIN stock_locations l ON l.stock_location_id = b.stock_location_id WHERE p.sku = %s AND l.location_code = %s",
        (sku, location),
    ).fetchone()
    conn.commit()
    return row[0], row[1]


def dispatch_transfer(conn: psycopg.Connection, sku: str, quantity: int, reference: str) -> str:
    """T1 as the replenishment loader runs it: lock the balance row, check available, insert transfer_out."""
    with conn.transaction():
        on_hand, reserved = conn.execute(
            "SELECT b.quantity_on_hand, b.quantity_reserved FROM inventory_balances b "
            "JOIN stock_locations l USING (stock_location_id) JOIN products p USING (product_id) "
            "WHERE l.location_code = 'CT-WH-1' AND p.sku = %s FOR UPDATE OF b",
            (sku,),
        ).fetchone()
        if on_hand - reserved < quantity:
            return "short"
        conn.execute(
            "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference) "
            "SELECT l.stock_location_id, p.product_id, 'transfer_out', %s, %s, %s FROM stock_locations l, products p "
            "WHERE l.location_code = 'CT-WH-1' AND p.sku = %s",
            (-quantity, RESERVED_AT, reference, sku),
        )
    return "dispatched"


def units_in_transit(conn: psycopg.Connection, sku: str) -> int:
    value = conn.execute(
        "SELECT coalesce(-sum(o.quantity_change), 0) FROM inventory_movements o JOIN products p USING (product_id) "
        "WHERE p.sku = %s AND o.movement_type = 'transfer_out' "
        "AND NOT EXISTS (SELECT 1 FROM inventory_movements i WHERE i.related_movement_id = o.movement_id)",
        (sku,),
    ).fetchone()[0]
    conn.commit()
    return value


def order_status(conn: psycopg.Connection, order_id: int) -> str:
    status = conn.execute("SELECT status FROM orders WHERE order_id = %s", (order_id,)).fetchone()[0]
    conn.commit()
    return status


def run_parallel(targets: list) -> list:
    """Run callables in threads that start together; return their results (or exceptions)."""
    barrier = threading.Barrier(len(targets))
    results: list = [None] * len(targets)

    def worker(i, fn):
        try:
            barrier.wait(timeout=30)
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001 - reported as a result
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return results


def describe(exc: BaseException) -> str:
    if isinstance(exc, psycopg.Error) and exc.sqlstate:
        return f"{exc.sqlstate} {exc.diag.constraint_name or ''}".strip()
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Scenarios. Each returns (passed, detail).
# ---------------------------------------------------------------------------
def s1_last_units(setup: psycopg.Connection):
    orders = [place_order(setup, f"CT-S1-{i:02d}", [("CT-P1", 1)]) for i in range(10)]
    conns = [connect(TEST_DB) for _ in orders]
    try:
        results = run_parallel([lambda c=c, o=o: reserve(c, o) for c, o in zip(conns, orders)])
    finally:
        for c in conns:
            c.close()
    reserved = sum(1 for r in results if r == "reserved")
    out = sum(1 for r in results if r == "out_of_stock")
    errors_ = [describe(r) for r in results if isinstance(r, BaseException)]
    on_hand, held = stock(setup, "CT-P1")
    cancelled = sum(1 for o in orders if order_status(setup, o) == "cancelled")
    ok = reserved == 5 and out == 5 and not errors_ and (on_hand, held) == (5, 5) and cancelled == 5
    return ok, f"reserved={reserved}, out_of_stock={out}, cancelled={cancelled}, stock on_hand/reserved={on_hand}/{held}, errors={errors_ or 'none'}"


def s2_naive_race(setup: psycopg.Connection):
    a_order = place_order(setup, "CT-S2-A", [("CT-P2", 2)])
    b_order = place_order(setup, "CT-S2-B", [("CT-P2", 2)])
    a_inserted = threading.Event()

    def naive_reserve(conn, order_id, first):
        if not first:
            a_inserted.wait(timeout=30)
            time.sleep(0.2)  # let A hold the balance row lock
        with conn.transaction():
            conn.execute(
                "INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at) "
                "SELECT i.order_item_id, o.fulfilment_location_id, i.product_id, i.quantity, %s, %s "
                "FROM order_items i JOIN orders o USING (order_id) WHERE o.order_id = %s",
                (RESERVED_AT, EXPIRES_AT, order_id),
            )
            conn.execute("UPDATE orders SET status = 'stock_reserved' WHERE order_id = %s", (order_id,))
            if first:
                a_inserted.set()
                time.sleep(1.0)  # B is now blocked on the same balance row
        return "committed"

    ca, cb = connect(TEST_DB), connect(TEST_DB)
    try:
        ra, rb = run_parallel([lambda: naive_reserve(ca, a_order, True), lambda: naive_reserve(cb, b_order, False)])
    finally:
        ca.close()
        cb.close()
    on_hand, held = stock(setup, "CT-P2")
    rb_desc = describe(rb) if isinstance(rb, BaseException) else rb
    ok = ra == "committed" and rb_desc == "23514 ck_inventory_balances_no_oversell" and (on_hand, held) == (3, 2)
    return ok, f"A={ra}, B={rb_desc}, stock on_hand/reserved={on_hand}/{held}"


def s3_no_deadlocks(setup: psycopg.Connection):
    """Opposite line order must not deadlock, because U2 locks balances in (location, product) order.

    Part A is deterministic: an in-flight receipt holds the P3 balance row; X (lines P3, P4)
    starts U2, then Y (lines P4, P3). With the lock order both queue on P3 and finish one
    after the other once the receipt commits. Locking in line order instead would let Y take
    P4 first and the two would deadlock. Part B races 20 such pairs freely.
    """
    deadlocks = 0
    other = []
    lock = threading.Lock()

    def guarded_reserve(order_id):
        nonlocal deadlocks
        conn = connect(TEST_DB)
        try:
            return reserve(conn, order_id)
        except errors.DeadlockDetected:
            with lock:
                deadlocks += 1
            return "deadlock"
        except Exception as exc:  # noqa: BLE001
            with lock:
                other.append(describe(exc))
            return "error"
        finally:
            conn.close()

    rounds = 3
    part_a_reserved = 0
    for r in range(rounds):
        x = place_order(setup, f"CT-S3-AX{r}", [("CT-P3", 1), ("CT-P4", 1)])
        y = place_order(setup, f"CT-S3-AY{r}", [("CT-P4", 1), ("CT-P3", 1)])
        blocker = connect(TEST_DB)
        blocker.execute(
            "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference) "
            "SELECT l.stock_location_id, p.product_id, 'purchase_receipt', 1, %s, %s FROM stock_locations l, products p "
            "WHERE l.location_code = 'CT-WH-1' AND p.sku = 'CT-P3'",
            (NOW, f"CT-S3-RECEIPT-{r}"),
        )  # open transaction: the P3 balance row stays locked
        results = {}
        tx = threading.Thread(target=lambda: results.__setitem__("x", guarded_reserve(x)))
        ty = threading.Thread(target=lambda: results.__setitem__("y", guarded_reserve(y)))
        tx.start()
        time.sleep(0.3)
        ty.start()
        time.sleep(0.7)  # both U2 calls are now waiting
        blocker.commit()
        blocker.close()
        tx.join(timeout=60)
        ty.join(timeout=60)
        part_a_reserved += sum(1 for v in results.values() if v == "reserved")

    pairs = 20
    x_orders = [place_order(setup, f"CT-S3-X{i:02d}", [("CT-P3", 1), ("CT-P4", 1)]) for i in range(pairs)]
    y_orders = [place_order(setup, f"CT-S3-Y{i:02d}", [("CT-P4", 1), ("CT-P3", 1)]) for i in range(pairs)]

    def loop(orders):
        for o in orders:
            guarded_reserve(o)
        return "done"

    run_parallel([lambda: loop(x_orders), lambda: loop(y_orders)])
    part_b_reserved = sum(1 for o in x_orders + y_orders if order_status(setup, o) == "stock_reserved")
    ok = deadlocks == 0 and not other and part_a_reserved == 2 * rounds and part_b_reserved == 2 * pairs
    return ok, (f"blocked rounds: {part_a_reserved}/{2 * rounds} reserved; free race: {part_b_reserved}/{2 * pairs} reserved; "
                f"deadlocks={deadlocks}, other errors={other or 'none'}")


def s4_concurrent_fulfilment(setup: psycopg.Connection):
    order_id = place_order(setup, "CT-S4", [("CT-P7", 3)])
    reserve(setup, order_id)
    authorize(setup, order_id)
    capture_and_confirm(setup, order_id)
    before = stock(setup, "CT-P7")

    def fulfil(conn):
        with conn.transaction():
            return conn.execute("SELECT fn_fulfil_order_stock(%s, %s)", (order_id, NOW)).fetchone()[0]

    c1, c2 = connect(TEST_DB), connect(TEST_DB)
    try:
        results = run_parallel([lambda: fulfil(c1), lambda: fulfil(c2)])
    finally:
        c1.close()
        c2.close()
    sales = setup.execute(
        "SELECT count(*) FROM inventory_movements m JOIN order_items i USING (order_item_id) "
        "WHERE i.order_id = %s AND m.movement_type = 'sale'",
        (order_id,),
    ).fetchone()[0]
    setup.commit()
    after = stock(setup, "CT-P7")
    descr = [r if isinstance(r, str) else describe(r) for r in results]
    ok = sorted(descr) == ["already_fulfilled", "fulfilled"] and sales == 1 and after == (before[0] - 3, before[1] - 3)
    return ok, f"results={descr}, sale movements={sales}, stock {before[0]}/{before[1]} -> {after[0]}/{after[1]}"


def _sweeper(conn, order_id, hold_seconds, started: threading.Event | None = None):
    """U4: lock a due order with SKIP LOCKED; expire, void, cancel."""
    with conn.transaction():
        row = conn.execute(
            "SELECT order_id FROM orders WHERE order_id = %s AND status = 'stock_reserved' FOR UPDATE SKIP LOCKED",
            (order_id,),
        ).fetchone()
        if started:
            started.set()
        if row is None:
            return "skipped"
        conn.execute("SELECT fn_close_order_reservations(%s, 'expired', %s)", (order_id, NOW))
        conn.execute("UPDATE payment_attempts SET status = 'voided' WHERE order_id = %s AND status = 'authorized'", (order_id,))
        conn.execute(
            "UPDATE orders SET status = 'cancelled', cancelled_at = %s, cancellation_reason = 'reservation_expired' "
            "WHERE order_id = %s AND status = 'stock_reserved'",
            (NOW, order_id),
        )
        time.sleep(hold_seconds)
    return "expired"


def s5_sweeper_first(setup: psycopg.Connection):
    order_id = place_order(setup, "CT-S5", [("CT-P7", 2)])
    reserve(setup, order_id)
    authorize(setup, order_id)
    locked = threading.Event()
    cs, cp = connect(TEST_DB), connect(TEST_DB)

    def payer():
        locked.wait(timeout=30)
        time.sleep(0.2)
        return capture_and_confirm(cp, order_id)

    try:
        rs, rp = run_parallel([lambda: _sweeper(cs, order_id, 1.0, locked), payer])
    finally:
        cs.close()
        cp.close()
    status = order_status(setup, order_id)
    attempt = setup.execute("SELECT status FROM payment_attempts WHERE order_id = %s", (order_id,)).fetchone()[0]
    reservation = setup.execute(
        "SELECT r.status FROM inventory_reservations r JOIN order_items i USING (order_item_id) WHERE i.order_id = %s",
        (order_id,),
    ).fetchone()[0]
    setup.commit()
    ok = rs == "expired" and rp == 0 and status == "cancelled" and attempt == "voided" and reservation == "expired"
    return ok, f"sweeper={rs}, rows captured={rp if not isinstance(rp, BaseException) else describe(rp)}, order={status}, attempt={attempt}, reservation={reservation}"


def s6_payment_first(setup: psycopg.Connection):
    order_id = place_order(setup, "CT-S6", [("CT-P7", 2)])
    reserve(setup, order_id)
    authorize(setup, order_id)
    holding = threading.Event()
    cp, cs = connect(TEST_DB), connect(TEST_DB)

    def payer():
        with cp.transaction():
            cp.execute("SELECT 1 FROM orders WHERE order_id = %s FOR UPDATE", (order_id,))
            holding.set()
            captured = cp.execute(
                "UPDATE payment_attempts SET status = 'captured', captured_at = %s WHERE order_id = %s AND status = 'authorized'",
                (RESERVED_AT + timedelta(minutes=3), order_id),
            ).rowcount
            cp.execute(
                "UPDATE orders SET status = 'confirmed', confirmed_at = %s WHERE order_id = %s AND status = 'stock_reserved'",
                (RESERVED_AT + timedelta(minutes=3), order_id),
            )
            time.sleep(1.0)
        return captured

    def sweeper():
        holding.wait(timeout=30)
        return _sweeper(cs, order_id, 0)

    try:
        rp, rs = run_parallel([payer, sweeper])
    finally:
        cp.close()
        cs.close()
    status = order_status(setup, order_id)
    ok = rp == 1 and rs == "skipped" and status == "confirmed"
    return ok, f"rows captured={rp}, sweeper={rs}, order={status}"


def s7_killed_session(setup: psycopg.Connection):
    order_id = place_order(setup, "CT-S7", [("CT-P7", 4)])
    before = stock(setup, "CT-P7")
    victim = connect(TEST_DB)
    pid = victim.info.backend_pid
    victim.execute("SELECT fn_reserve_order_stock(%s, %s, %s)", (order_id, RESERVED_AT, EXPIRES_AT)).fetchone()
    # The victim holds an open transaction with 4 units reserved; kill it before COMMIT.
    with connect("postgres", autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    try:
        victim.close()
    except Exception:  # noqa: BLE001 - connection is already gone
        pass
    after_crash = stock(setup, "CT-P7")
    status_after_crash = order_status(setup, order_id)
    retry = reserve(setup, order_id)
    after_retry = stock(setup, "CT-P7")
    ok = after_crash == before and status_after_crash == "placed" and retry == "reserved" and after_retry == (before[0], before[1] + 4)
    return ok, (f"after kill: stock {after_crash[0]}/{after_crash[1]} (before {before[0]}/{before[1]}), order={status_after_crash}; "
                f"retry={retry}, stock {after_retry[0]}/{after_retry[1]}")


def s8_no_lost_updates(setup: psycopg.Connection):
    n = 20
    conns = [connect(TEST_DB) for _ in range(n)]

    def receipt(conn, i):
        with conn.transaction():
            conn.execute(
                "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference) "
                "SELECT l.stock_location_id, p.product_id, 'purchase_receipt', 1, %s, %s FROM stock_locations l, products p "
                "WHERE l.location_code = 'CT-WH-1' AND p.sku = 'CT-P5'",
                (NOW, f"CT-S8-DN-{i:02d}"),
            )
        return "ok"

    try:
        results = run_parallel([lambda c=c, i=i: receipt(c, i) for i, c in enumerate(conns)])
    finally:
        for c in conns:
            c.close()
    on_hand, _ = stock(setup, "CT-P5")
    ledger = setup.execute(
        "SELECT sum(quantity_change) FROM inventory_movements m JOIN products p USING (product_id) WHERE p.sku = 'CT-P5'"
    ).fetchone()[0]
    setup.commit()
    failures = [describe(r) for r in results if isinstance(r, BaseException)]
    ok = not failures and on_hand == n and ledger == n
    return ok, f"receipts committed={n - len(failures)}, on_hand={on_hand}, ledger sum={ledger}, errors={failures or 'none'}"


def s9_serialization_retry(setup: psycopg.Connection):
    x = place_order(setup, "CT-S9-X", [("CT-P6", 3)])
    y = place_order(setup, "CT-S9-Y", [("CT-P6", 3)])
    ca, cb = connect(TEST_DB), connect(TEST_DB)
    try:
        cb.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        cb.execute("SELECT count(*) FROM orders").fetchone()  # B takes its snapshot
        reserve(ca, x)  # A commits a change to the shared balance row
        first_try = None
        try:
            cb.execute("SELECT fn_reserve_order_stock(%s, %s, %s)", (y, RESERVED_AT, EXPIRES_AT)).fetchone()
            cb.commit()
            first_try = "no conflict"
        except errors.SerializationFailure as exc:
            cb.rollback()
            first_try = describe(exc)
        # Retry rule (design): retry the whole unit of work on 40001.
        cb.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        retry = cb.execute("SELECT fn_reserve_order_stock(%s, %s, %s)", (y, RESERVED_AT, EXPIRES_AT)).fetchone()[0]
        cb.commit()
    finally:
        ca.close()
        cb.close()
    on_hand, held = stock(setup, "CT-P6")
    ok = first_try.startswith("40001") and retry == "reserved" and (on_hand, held) == (10, 6)
    return ok, f"first try={first_try}, retry={retry}, stock on_hand/reserved={on_hand}/{held}"


def s10_duplicate_receipt(setup: psycopg.Connection):
    before, _ = stock(setup, "CT-P5")
    ca, cb = connect(TEST_DB), connect(TEST_DB)

    def receipt(conn):
        with conn.transaction():
            conn.execute(
                "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference) "
                "SELECT l.stock_location_id, p.product_id, 'purchase_receipt', 7, %s, 'CT-S10-DN-1' FROM stock_locations l, products p "
                "WHERE l.location_code = 'CT-WH-1' AND p.sku = 'CT-P5'",
                (NOW,),
            )
        return "stored"

    try:
        results = run_parallel([lambda: receipt(ca), lambda: receipt(cb)])
    finally:
        ca.close()
        cb.close()
    after, _ = stock(setup, "CT-P5")
    descr = sorted(r if isinstance(r, str) else describe(r) for r in results)
    ok = descr == ["23505 uq_inventory_movements_document_line", "stored"] and after == before + 7
    return ok, f"results={descr}, on_hand {before} -> {after}"


def s11_transfer_against_reservation(setup: psycopg.Connection):
    """5 units of P8: an order wants 3 (U2) while a transfer wants 3 (T1). Exactly one can have them."""
    order = place_order(setup, "CT-S11", [("CT-P8", 3)])
    ca, cb = connect(TEST_DB), connect(TEST_DB)
    try:
        results = run_parallel([lambda: reserve(ca, order), lambda: dispatch_transfer(cb, "CT-P8", 3, "CT-S11-TR-1")])
    finally:
        ca.close()
        cb.close()
    on_hand, held = stock(setup, "CT-P8")
    transit = units_in_transit(setup, "CT-P8")
    descr = [r if isinstance(r, str) else describe(r) for r in results]
    one_winner = sorted(descr) in (["reserved", "short"], ["dispatched", "out_of_stock"])
    ok = one_winner and on_hand + transit == 5 and held <= on_hand and (held, transit) in ((3, 0), (0, 3))
    return ok, f"U2={descr[0]}, transfer={descr[1]}, warehouse on_hand/reserved={on_hand}/{held}, in transit={transit}"


def s12_transfer_cannot_take_reserved_units(setup: psycopg.Connection):
    """4 units of P9, 3 reserved by an order: a transfer_out of 4 that skips the lock and check is refused."""
    order = place_order(setup, "CT-S12", [("CT-P9", 3)])
    first = reserve(setup, order)
    conn = connect(TEST_DB)
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference) "
                "SELECT l.stock_location_id, p.product_id, 'transfer_out', -4, %s, 'CT-S12-TR-1' FROM stock_locations l, products p "
                "WHERE l.location_code = 'CT-WH-1' AND p.sku = 'CT-P9'",
                (RESERVED_AT,),
            )
        outcome = "stored"
    except psycopg.Error as exc:
        outcome = describe(exc)
    finally:
        conn.close()
    on_hand, held = stock(setup, "CT-P9")
    ok = first == "reserved" and outcome == "23514 ck_inventory_balances_no_oversell" and (on_hand, held) == (4, 3)
    return ok, f"U2={first}, naive transfer_out of 4={outcome}, on_hand/reserved={on_hand}/{held}"


def s13_duplicate_arrival(setup: psycopg.Connection):
    """A dispatched transfer of 2 x P10 is recorded as arrived by two sessions at once: stored once."""
    assert dispatch_transfer(setup, "CT-P10", 2, "CT-S13-TR-1") == "dispatched"
    out_id = setup.execute("SELECT movement_id FROM inventory_movements WHERE external_reference = 'CT-S13-TR-1'").fetchone()[0]
    setup.commit()
    ca, cb = connect(TEST_DB), connect(TEST_DB)

    def arrive(conn):
        with conn.transaction():
            conn.execute(
                "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, "
                "related_movement_id, external_reference) SELECT l.stock_location_id, p.product_id, 'transfer_in', 2, %s, %s, "
                "'CT-S13-TR-1' FROM stock_locations l, products p WHERE l.location_code = 'CT-STO-1' AND p.sku = 'CT-P10'",
                (RESERVED_AT + timedelta(hours=1), out_id),
            )
        return "stored"

    try:
        results = run_parallel([lambda: arrive(ca), lambda: arrive(cb)])
    finally:
        ca.close()
        cb.close()
    descr = sorted(r if isinstance(r, str) else describe(r) for r in results)
    warehouse, _ = stock(setup, "CT-P10")
    store, _ = stock(setup, "CT-P10", "CT-STO-1")
    ok = descr == ["23505 uq_inventory_movements_one_transfer_in_per_out", "stored"] and (warehouse, store) == (4, 2)
    return ok, f"results={descr}, warehouse/store on hand={warehouse}/{store}"


def s11_invariants(setup: psycopg.Connection):
    bad = setup.execute(
        """
        SELECT count(*) FROM inventory_balances b
         WHERE b.quantity_on_hand <> coalesce((SELECT sum(m.quantity_change) FROM inventory_movements m
                                                WHERE m.stock_location_id = b.stock_location_id AND m.product_id = b.product_id), 0)
            OR b.quantity_reserved <> coalesce((SELECT sum(r.quantity) FROM inventory_reservations r
                                                 WHERE r.stock_location_id = b.stock_location_id AND r.product_id = b.product_id
                                                   AND r.status = 'active'), 0)
            OR b.quantity_reserved > b.quantity_on_hand OR b.quantity_on_hand < 0
        """
    ).fetchone()[0]
    balances = setup.execute("SELECT count(*) FROM inventory_balances").fetchone()[0]
    setup.commit()
    return bad == 0, f"balances checked={balances}, inconsistent={bad}"


SCENARIOS = [
    ("1. Last units: 10 sessions race through U2 for 5 units", s1_last_units),
    ("2. Without the U2 lock, the balance CHECK stops overselling", s2_naive_race),
    ("3. Opposite line order: no deadlocks (lock order)", s3_no_deadlocks),
    ("4. Two concurrent U5 calls issue stock once", s4_concurrent_fulfilment),
    ("5. Sweeper first: capture finds nothing to capture", s5_sweeper_first),
    ("6. Capture first: sweeper skips the locked order", s6_payment_first),
    ("7. Session killed mid-U2 leaves no trace; retry works", s7_killed_session),
    ("8. 20 concurrent receipts: no lost update", s8_no_lost_updates),
    ("9. REPEATABLE READ conflict: 40001, retry succeeds", s9_serialization_retry),
    ("10. Same receipt sent twice at once: stored once", s10_duplicate_receipt),
    ("11. Transfer dispatch races U2 for the same stock: one wins", s11_transfer_against_reservation),
    ("12. Transfer cannot take reserved units (balance CHECK)", s12_transfer_cannot_take_reserved_units),
    ("13. Same transfer arrival recorded twice at once: stored once", s13_duplicate_arrival),
    ("14. Ledger invariants hold after all scenarios", s11_invariants),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recreate", action="store_true", help=f"drop and rebuild {TEST_DB} if it exists")
    parser.add_argument("--keep", action="store_true", help=f"keep {TEST_DB} after the run")
    parser.add_argument("--through", type=int, choices=(7, 8, 9), default=7,
                        help="last migration to apply (8 = with V008 security, 9 = also V009 activity control)")
    parser.add_argument("--role", help="login role for the fixture and scenario sessions (needs --through 8 or 9)")
    args = parser.parse_args()
    global LAST_VERSION, SCENARIO_ROLE
    if args.role and args.through < 8:
        parser.error("--role needs --through 8 (the roles are created by V008)")
    LAST_VERSION, SCENARIO_ROLE = args.through, args.role

    started = time.time()
    create_test_database(args.recreate)
    try:
        applied = apply_migrations()
        print(f"Test database {TEST_DB}: applied {', '.join(applied)}")
        setup = connect(TEST_DB)
        user = setup.execute("SELECT current_user").fetchone()[0]
        setup.commit()
        print(f"Fixture and scenario sessions run as {user}")
        setup_fixtures(setup)
        rows = []
        for name, fn in SCENARIOS:
            try:
                passed, detail = fn(setup)
            except Exception as exc:  # noqa: BLE001 - a crash is a failed scenario
                if not setup.closed:
                    setup.rollback()
                passed, detail = False, f"crashed: {describe(exc)}"
            rows.append((name, "PASS" if passed else "FAIL", detail))
        setup.close()
    finally:
        if not args.keep:
            drop_test_database()

    width = max(len(r[0]) for r in rows)
    print()
    for name, status, detail in rows:
        print(f"{name:<{width}}  {status}  {detail}")
    failed = sum(1 for r in rows if r[1] == "FAIL")
    print(f"\nScenarios: {len(rows)}  PASS: {len(rows) - failed}  FAIL: {failed}  "
          f"({time.time() - started:.1f} s; test database {'kept' if args.keep else 'dropped'})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
