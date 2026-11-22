"""Database tests for Phase 3, step 5, stage S1: the pilots with later activity (opt-in).

Since S1 the two pilot loaders verify their OWN records exactly and accept records outside them
only as LATER ACTIVITY that passes the identity and integrity rules of nordiccart.activity.scope
(docs/phase3_combined_activity_design.md, 8.1). Scripts 13 and 14 are scoped to their pilots and
script 15 checks the whole database. These tests prove against real PostgreSQL that:
  * valid later orders, receipts, transfers and extra inventory listings (exception E2) are
    accepted by both loaders, both pilots still verify and scripts 13, 14 and 15 pass;
  * read-only plans and reruns write nothing while later activity exists (idempotency);
  * invalid or conflicting records are refused: being dated later is never enough;
  * the pilots' own records are still compared exactly;
  * later activity before a pilot is complete blocks it (business-time order);
  * later records are written through the shared units of work (nordiccart.units), the code
    the loaders themselves run.

Safety
  * Runs only when NORDICCART_DB_TESTS=1 is set; otherwise every test is skipped.
  * Creates its own databases (nordiccart_s1_test_*) on the server named by the standard PG
    variables, loads V001 to V007, the master data pilot, the opening inventory, the pilot
    transactions and the replenishment pilot into them, and drops them at the end. It never
    connects to nordiccart_oltp. The role must be allowed to create databases.

Run from the repository folder (PowerShell):
    $env:NORDICCART_DB_TESTS = "1"
    python -m pytest tests/python/test_pilot_scope_db.py -v
"""

from __future__ import annotations

import dataclasses
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from nordiccart.inventory.config import load_inventory_settings  # noqa: E402
from nordiccart.inventory.generate import generate_opening_inventory  # noqa: E402
from nordiccart.inventory.load import apply_inventory  # noqa: E402
from nordiccart.master_data.load import apply_load  # noqa: E402
from nordiccart.replenishment import cli as repl_cli  # noqa: E402
from nordiccart.replenishment.config import load_replenishment_settings  # noqa: E402
from nordiccart.replenishment.generate import generate_replenishment  # noqa: E402
from nordiccart.replenishment.load import (  # noqa: E402
    LoadInterrupted, ReplenishmentConflictError, apply_replenishment, plan_replenishment, verify_replenishment,
)
from nordiccart.replenishment.model import (  # noqa: E402
    Delivery, DeliveryLine, PurchaseOrder, PurchaseOrderLine, TransferDocument, TransferLine,
)
from nordiccart.settings import load_env_file  # noqa: E402
from nordiccart.transactions import cli as tx_cli  # noqa: E402
from nordiccart.transactions.config import load_transaction_settings  # noqa: E402
from nordiccart.transactions.generate import generate_transactions  # noqa: E402
from nordiccart.transactions.load import (  # noqa: E402
    TransactionConflictError, apply_transactions, plan_transactions, verify_transactions,
)
from nordiccart.units import orders as order_units  # noqa: E402
from nordiccart.units import stock as stock_units  # noqa: E402
from nordiccart.units.base import Ids, run_unit  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("NORDICCART_DB_TESTS") != "1",
                                reason="set NORDICCART_DB_TESTS=1 to run the database tests")

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "nordiccart_s1_test"
TEMPLATE_PILOT = f"{PREFIX}_template_pilot"          # migrations, master data, opening inventory, 20 pilot orders
TEMPLATE_REPL = f"{PREFIX}_template_repl"            # + the replenishment pilot: the state of the real database
SCRIPT_11 = (44, 33, 11, 0)                           # rows, PASS, INFO, FAIL (S6 M3: INFO row 507)
SCRIPT_12 = (24, 17, 7, 0)
SCRIPT_13 = (52, 45, 7, 0)
SCRIPT_14 = (44, 37, 7, 0)
SCRIPT_15 = (43, 35, 8, 0)
UTC = timezone.utc
SHIFT = timedelta(days=35)        # pilot orders (1 to 14 Nov 2024) -> 6 to 19 Dec 2024, same weekday and local time
log = logging.getLogger("nordiccart.tests.s1")

ORDER_UNITS = {"place": order_units.place, "reserve": order_units.reserve,
               "cancel_out_of_stock": order_units.cancel_out_of_stock, "payment_start": order_units.payment_start,
               "payment_result": order_units.payment_result, "customer_cancel": order_units.customer_cancel,
               "expire": order_units.expire, "fulfil": order_units.fulfil}


# --------------------------------------------------------------------------- databases
def _admin():
    return psycopg.connect(dbname="postgres", autocommit=True)


def _connect(dbname: str):
    assert dbname.startswith(PREFIX), "tests only touch their own databases"
    conn = psycopg.connect(dbname=dbname, autocommit=True)
    conn.execute("SET search_path TO public")
    return conn


def _drop(admin, name: str) -> None:
    admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                  (name,))
    admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(scope="module")
def inventory(pilot_settings, pilot_dataset):
    return generate_opening_inventory(pilot_settings, load_inventory_settings(pilot_settings), pilot_dataset)


@pytest.fixture(scope="module")
def pilot(pilot_settings, pilot_dataset, inventory):
    inv = load_inventory_settings(pilot_settings)
    tx = load_transaction_settings(pilot_settings, inv.opening_date)
    return generate_transactions(pilot_settings, tx, pilot_dataset, inventory, inv.store_format_weight)


@pytest.fixture(scope="module")
def plan(pilot_settings, pilot_dataset, inventory, pilot):
    inv = load_inventory_settings(pilot_settings)
    tx = load_transaction_settings(pilot_settings, inv.opening_date)
    rp = load_replenishment_settings(pilot_settings, tx.store_hours, {c.category_code for c in pilot_dataset.product_categories})
    return generate_replenishment(pilot_settings, inv, rp, pilot_dataset, inventory, pilot)


@pytest.fixture(scope="module")
def templates(pilot_dataset, inventory, pilot, plan):
    load_env_file()
    with _admin() as admin:
        for name in (TEMPLATE_REPL, TEMPLATE_PILOT):
            _drop(admin, name)
        admin.execute(f'CREATE DATABASE "{TEMPLATE_PILOT}" WITH ENCODING \'UTF8\' TEMPLATE template0')
        admin.execute(f'ALTER DATABASE "{TEMPLATE_PILOT}" SET timezone TO \'UTC\'')
    with _connect(TEMPLATE_PILOT) as conn:
        for path in sorted((REPO_ROOT / "database" / "migrations").glob("V00[1-7]__*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
        apply_load(conn, pilot_dataset)
        apply_inventory(conn, inventory)
        assert apply_transactions(conn, pilot).events_run == 100
    with _admin() as admin:
        admin.execute(f'CREATE DATABASE "{TEMPLATE_REPL}" TEMPLATE "{TEMPLATE_PILOT}"')
    with _connect(TEMPLATE_REPL) as conn:
        assert apply_replenishment(conn, plan).events_run == 17
    yield
    with _admin() as admin:
        for name in (TEMPLATE_REPL, TEMPLATE_PILOT):
            _drop(admin, name)


def _fresh(templates, request, template: str):
    name = f"{PREFIX}_{request.node.name}"[:63].lower().replace("[", "_").replace("]", "").replace("-", "_")
    with _admin() as admin:
        _drop(admin, name)
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
    return name


@pytest.fixture
def fresh_db(templates, request):
    """A copy of the real database's state: pilot transactions and replenishment pilot loaded."""
    name = _fresh(templates, request, TEMPLATE_REPL)
    yield name
    with _admin() as admin:
        _drop(admin, name)


@pytest.fixture
def pilot_only_db(templates, request):
    """Pilot transactions loaded, replenishment pilot not yet."""
    name = _fresh(templates, request, TEMPLATE_PILOT)
    yield name
    with _admin() as admin:
        _drop(admin, name)


# --------------------------------------------------------------------------- reading
def _counts(conn) -> dict:
    out = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
           for t in ("orders", "order_items", "payment_attempts", "inventory_reservations", "inventory_movements",
                     "change_log")}
    out["on_hand"], out["reserved"], out["reorder_points"] = conn.execute(
        "SELECT sum(quantity_on_hand), sum(quantity_reserved), sum(reorder_point) FROM inventory_balances").fetchone()
    return out


def _sql(conn, script: str):
    rows = conn.execute((REPO_ROOT / "sql" / "verification" / script).read_text(encoding="utf-8")).fetchall()
    statuses = [r[5] for r in rows]
    return len(rows), statuses.count("PASS"), statuses.count("INFO"), statuses.count("FAIL")


def _failing(conn, script: str) -> set[int]:
    rows = conn.execute((REPO_ROOT / "sql" / "verification" / script).read_text(encoding="utf-8")).fetchall()
    return {r[0] for r in rows if r[5] == "FAIL"}


# --------------------------------------------------------------------------- writing later activity
def _shift_order(o, sequence: int, delta: timedelta = SHIFT, number: str | None = None, **changes):
    """A pilot order's whole timeline moved by `delta`, renumbered; payments get new provider references."""
    def sh(t):
        return None if t is None else t + delta
    attempts = tuple(dataclasses.replace(a, attempted_at=sh(a.attempted_at), authorized_at=sh(a.authorized_at),
                                         captured_at=sh(a.captured_at), failed_at=sh(a.failed_at),
                                         provider_reference=f"{a.provider_reference}-S1-{sequence}")
                     for a in o.attempts)
    placed = o.placed_at + delta
    return dataclasses.replace(
        o, sequence=sequence, order_number=number or f"{o.country_code}-{placed.year}-{sequence:08d}", placed_at=placed,
        reserve_at=sh(o.reserve_at), reservation_expires_at=sh(o.reservation_expires_at), attempts=attempts,
        confirmed_at=sh(o.confirmed_at), fulfilled_at=sh(o.fulfilled_at), cancelled_at=sh(o.cancelled_at),
        reservation_closed_at=sh(o.reservation_closed_at), holder_order_number=None, **changes)


def _templates(conn, pilot) -> list:
    """Fulfilled pilot orders whose lines can all be sold again now, one after the other."""
    available = {(loc, sku): oh - rs for loc, sku, oh, rs in conn.execute(
        "SELECT l.location_code, p.sku, b.quantity_on_hand, b.quantity_reserved FROM inventory_balances b "
        "JOIN stock_locations l USING (stock_location_id) JOIN products p USING (product_id)").fetchall()}
    out = []
    for o in pilot.orders:
        need = {(o.fulfilment_location_code, x.sku): x.quantity for x in o.lines}
        if o.final_status == "fulfilled" and all(available.get(k, 0) >= q for k, q in need.items()):
            for k, q in need.items():
                available[k] -= q
            out.append(o)
    return out


def _write_orders(conn, orders, open_orders=()) -> None:
    """Every event of the orders through the shared units of work, in business-time order.
    Orders in `open_orders` stop after U2 (stock reserved, no payment yet)."""
    ids = Ids(conn)
    by_number = {o.order_number: o for o in orders}
    events = sorted((e for o in orders for e in o.events()), key=lambda e: e.sort_key)
    for e in events:
        if e.order_number in open_orders and e.kind not in ("place", "reserve"):
            continue
        o = by_number[e.order_number]
        args = (e.attempt_number,) if e.kind in ("payment_start", "payment_result") else ()
        result = run_unit(conn, lambda: ORDER_UNITS[e.kind](conn, ids, o, *args), f"{e.order_number} {e.label()}", log,
                          rolled_back="rolled back (out_of_stock)")
        assert "rolled back" not in result, f"{e.order_number} {e.label()}: {result}"


def _receive(conn, reference: str, warehouse: str, received_at, lines) -> None:
    """A later supplier delivery through the shared unit of work R1."""
    po = PurchaseOrder(number=reference.split("/")[0], market=warehouse[:2], warehouse=warehouse,
                       ordered_at=received_at - timedelta(days=4), forced=False,
                       lines=tuple(PurchaseOrderLine(i + 1, sku, q, "reorder_point", 0, 0, 0, 1)
                                   for i, (sku, q) in enumerate(lines)),
                       deliveries=(Delivery(reference, 1, received_at, tuple(DeliveryLine(sku, q) for sku, q in lines)),))
    run_unit(conn, lambda: stock_units.receive(conn, Ids(conn), po, po.deliveries[0], reference), reference, log)


def _transfer(conn, reference: str, source: str, destination: str, dispatched_at, lines) -> None:
    """A later store transfer through the shared units of work T1 and T2 (same morning)."""
    t = TransferDocument(reference=reference, market=source[:2], source=source, destination=destination,
                         requested_at=dispatched_at - timedelta(hours=1), dispatched_at=dispatched_at,
                         arrived_at=dispatched_at + timedelta(minutes=75), trigger="reorder_point",
                         lines=tuple(TransferLine(i + 1, sku, q, q, 0, 0, 0, 0) for i, (sku, q) in enumerate(lines)))
    ids = Ids(conn)
    run_unit(conn, lambda: stock_units.dispatch(conn, ids, t, reference), reference, log)
    run_unit(conn, lambda: stock_units.arrive(conn, ids, t, reference), reference, log)


def _insert(conn, location: str, sku: str, kind: str, quantity: int, at, reference, related=None):
    """A raw movement, as a faulty or foreign writer could insert it (V007 still applies)."""
    return conn.execute(
        "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, "
        "external_reference, related_movement_id) SELECT l.stock_location_id, p.product_id, %s::movement_type, %s, %s, %s, %s "
        "FROM stock_locations l, products p WHERE l.location_code = %s AND p.sku = %s RETURNING movement_id",
        (kind, quantity, at, reference, related, location, sku)).fetchone()[0]


def _stocked_sku(inventory, location: str) -> str:
    return next(x.sku for x in inventory.listings if x.location_code == location and x.opening_quantity >= 5)


def _valid_later_activity(conn, pilot, inventory) -> dict:
    """Combined-activity-like records after the replenishment pilot: one receipt, one transfer, three
    fulfilled orders and one open order (stock reserved), numbered 21 to 24 in placed_at order."""
    sku = _stocked_sku(inventory, "SE-WH-01")
    _receive(conn, "PO-SE-2024-000002/DN1", "SE-WH-01", datetime(2024, 12, 3, 8, 0, tzinfo=UTC), [(sku, 6)])
    _transfer(conn, "TR-SE-2024-000002-SE-STO-01", "SE-WH-01", "SE-STO-01", datetime(2024, 12, 4, 5, 30, tzinfo=UTC),
              [(sku, 2)])
    chosen = _templates(conn, pilot)[:4]
    shifted = sorted(chosen, key=lambda o: o.placed_at)
    orders = [_shift_order(o, 21 + i) for i, o in enumerate(shifted)]
    open_number = orders[-1].order_number
    _write_orders(conn, orders, open_orders={open_number})
    return {"orders": orders, "open": open_number, "sku": sku}


def _extra_listing(conn, opening_at=None, sku: str = "ACC-000901") -> None:
    """Exception E2: a product added after the pilot, listed at SE-WH-01 with opening stock and at SE-STO-01 at zero."""
    conn.execute("INSERT INTO products (sku, product_name, brand, category_id, launched_on) "
                 "SELECT %s, 'S1 test cable', 'Nordic Test', category_id, DATE '2024-10-01' FROM products "
                 "WHERE sku = 'ACC-000010'", (sku,))
    conn.execute("INSERT INTO inventory_balances (stock_location_id, product_id, reorder_point) "
                 "SELECT l.stock_location_id, p.product_id, 2 FROM stock_locations l, products p "
                 "WHERE l.location_code IN ('SE-WH-01', 'SE-STO-01') AND p.sku = %s", (sku,))
    at = opening_at or datetime(2024, 10, 31, 23, 0, tzinfo=UTC)          # local midnight of 2024-11-01 in Stockholm
    _insert(conn, "SE-WH-01", sku, "opening_balance", 12, at, "OB-20241101")


# --------------------------------------------------------------------------- valid later activity
def test_the_shared_units_of_work_write_valid_later_activity_that_both_pilots_accept(fresh_db, pilot, plan, inventory):
    with _connect(fresh_db) as conn:
        assert _sql(conn, "15_verify_inventory_and_activity.sql") == SCRIPT_15     # the real database's state
        made = _valid_later_activity(conn, pilot, inventory)

        tx = plan_transactions(conn, pilot)
        assert tx.ok, tx.conflicts + tx.blocking
        assert tx.counts() == (0, 0) and len(tx.done) == 100
        later = tx.later
        assert (later.orders, later.open_orders) == (4, 1)
        assert later.by_type["purchase_receipt"] == 5 + 1 and later.by_type["transfer_out"] == 8 + 1
        assert later.by_type["sale"] == sum(len(o.lines) for o in made["orders"][:-1])
        assert sum(later.reserved.values()) == made["orders"][-1].units
        assert verify_transactions(conn, pilot) == (20, [])

        repl = plan_replenishment(conn, plan)
        assert repl.ok, repl.conflicts + repl.blocking
        assert repl.counts() == (17, 0)
        assert (repl.later.orders, repl.later.by_type["purchase_receipt"]) == (4, 1)   # its own 5 receipts are not later
        assert verify_replenishment(conn, plan) == (17, [])

        assert _sql(conn, "11_verify_master_data.sql") == SCRIPT_11
        assert _sql(conn, "12_verify_opening_inventory.sql") == SCRIPT_12
        assert _sql(conn, "13_verify_pilot_transactions.sql") == SCRIPT_13
        assert _sql(conn, "14_verify_replenishment.sql") == SCRIPT_14
        assert _sql(conn, "15_verify_inventory_and_activity.sql") == SCRIPT_15


def test_reruns_and_plans_write_nothing_while_later_activity_exists(fresh_db, pilot, plan, inventory):
    with _connect(fresh_db) as conn:
        _valid_later_activity(conn, pilot, inventory)
        _extra_listing(conn)
        before = _counts(conn)
        for _ in range(2):
            assert plan_transactions(conn, pilot).ok and plan_replenishment(conn, plan).ok
            tx = apply_transactions(conn, pilot)
            assert (tx.events_run, tx.change_log_rows, tx.verified_orders) == (0, 0, 20)
            repl = apply_replenishment(conn, plan)
            assert (repl.events_run, repl.change_log_rows, repl.verified_events) == (0, 0, 17)
        assert _counts(conn) == before


def test_extra_inventory_listings_of_exception_e2_are_accepted(fresh_db, pilot, plan):
    with _connect(fresh_db) as conn:
        _extra_listing(conn)
        _transfer(conn, "TR-SE-2024-000002-SE-STO-01", "SE-WH-01", "SE-STO-01", datetime(2024, 12, 4, 5, 30, tzinfo=UTC),
                  [("ACC-000901", 3)])
        for progress in (plan_transactions(conn, pilot), plan_replenishment(conn, plan)):
            assert progress.ok, progress.conflicts + progress.blocking
            assert (progress.later.extra_listings, progress.later.extra_opening_balances,
                    progress.later.extra_opening_units) == (2, 1, 12)
        assert not plan_replenishment(conn, plan).later.on_hand_change   # nothing changed at the pilot's listings
        assert verify_transactions(conn, pilot) == (20, []) and verify_replenishment(conn, plan) == (17, [])
        assert _sql(conn, "13_verify_pilot_transactions.sql") == SCRIPT_13
        assert _sql(conn, "14_verify_replenishment.sql") == SCRIPT_14
        assert _sql(conn, "15_verify_inventory_and_activity.sql") == SCRIPT_15


# --------------------------------------------------------------------------- refused later records
def _case_sequence_in_pilot_range(conn, ctx):
    o = ctx["templates"][0]
    # a pilot sequence number under another market prefix: the number itself is new, its sequence is not
    seq = next(p.sequence for p in ctx["pilot"].orders if p.country_code != o.country_code)
    _write_orders(conn, [_shift_order(o, 21, number=f"{o.country_code}-2024-{seq:08d}")])


def _case_sequence_gap(conn, ctx):
    _write_orders(conn, [_shift_order(ctx["templates"][0], 22)])


def _case_number_of_another_market(conn, ctx):
    o = ctx["templates"][0]
    other = "DK" if o.country_code != "DK" else "SE"
    _write_orders(conn, [_shift_order(o, 21, number=f"{other}-2024-00000021")])


def _case_order_inside_the_replenishment_window(conn, ctx):
    o = next(t for t in ctx["templates"] if t.placed_at.day >= 2)
    delta = timedelta(days=(datetime(2024, 11, 25).date() - o.placed_at.date()).days)    # placed on 25 Nov 2024
    _write_orders(conn, [_shift_order(o, 21, delta=delta)])


def _case_price_not_from_the_price_list(conn, ctx):
    o = next(t for t in ctx["templates"] if len(t.lines) == 1)
    x = o.lines[0]
    price = x.unit_price + Decimal("1.00")
    total = price * x.quantity - x.discount_amount
    vat = (total * x.vat_rate / (1 + x.vat_rate)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    line = dataclasses.replace(x, unit_price=price, line_total=total, vat_amount=vat)
    order_total = total + o.shipping_fee
    repriced = dataclasses.replace(o, lines=(line,), items_total=total, order_total=order_total,
                                   attempts=tuple(dataclasses.replace(a, amount=order_total) for a in o.attempts))
    _write_orders(conn, [_shift_order(repriced, 21)])


def _case_customer_of_another_market(conn, ctx):
    o = ctx["templates"][0]
    other = next(c for c in ctx["dataset"].customers
                 if c.country_code != o.country_code and c.registered_at < o.placed_at and c.is_active)
    _write_orders(conn, [_shift_order(o, 21, customer_number=other.customer_number)])


def _case_receipt_at_a_store(conn, ctx):
    _insert(conn, "SE-STO-01", ctx["sku"], "purchase_receipt", 2, datetime(2024, 12, 9, 8, tzinfo=UTC),
            "PO-SE-2024-000002/DN1")


def _case_receipt_reusing_a_pilot_purchase_order(conn, ctx):
    _insert(conn, "SE-WH-01", ctx["sku"], "purchase_receipt", 2, datetime(2024, 12, 9, 8, tzinfo=UTC),
            "PO-SE-2024-000001/DN3")


def _case_arrival_at_another_store(conn, ctx):
    out = _insert(conn, "SE-WH-01", ctx["sku"], "transfer_out", -1, datetime(2024, 12, 10, 5, 30, tzinfo=UTC),
                  "TR-SE-2024-000002-SE-STO-01")
    _insert(conn, "SE-STO-02", ctx["sku"], "transfer_in", 1, datetime(2024, 12, 10, 6, 45, tzinfo=UTC),
            "TR-SE-2024-000002-SE-STO-01", out)


def _case_malformed_transfer_reference(conn, ctx):
    out = _insert(conn, "SE-WH-01", ctx["sku"], "transfer_out", -1, datetime(2024, 12, 10, 5, 30, tzinfo=UTC),
                  "TR-SE-24-2-SE-STO-01")
    _insert(conn, "SE-STO-01", ctx["sku"], "transfer_in", 1, datetime(2024, 12, 10, 6, 45, tzinfo=UTC),
            "TR-SE-24-2-SE-STO-01", out)


def _case_one_document_number_two_destinations(conn, ctx):
    at = datetime(2024, 12, 10, 5, 30, tzinfo=UTC)
    for store in ("SE-STO-01", "SE-STO-02"):
        reference = f"TR-SE-2024-000002-{store}"
        out = _insert(conn, "SE-WH-01", ctx["sku"], "transfer_out", -1, at, reference)
        _insert(conn, store, ctx["sku"], "transfer_in", 1, at + timedelta(minutes=75), reference, out)


def _case_stock_adjustment(conn, ctx):
    _insert(conn, "SE-WH-01", ctx["sku"], "stock_adjustment", -1, datetime(2024, 12, 9, 8, tzinfo=UTC), "SC-2024-000001")


def _case_movement_in_the_future(conn, ctx):
    _insert(conn, "SE-WH-01", ctx["sku"], "purchase_receipt", 2, datetime(2027, 1, 5, 8, tzinfo=UTC),
            "PO-SE-2027-000001/DN1")


def _case_extra_listing_opening_not_at_the_count(conn, ctx):
    _extra_listing(conn, opening_at=datetime(2024, 11, 5, 8, tzinfo=UTC))


# name: (setup, rule text in the loader message, transaction loader refuses, replenishment loader refuses,
#        script 15 checks that fail, other scripts' checks that fail)
REFUSED = {
    "sequence_in_pilot_range": (_case_sequence_in_pilot_range, "use a sequence number of the pilot's range",
                                True, True, {302, 304}, {}),
    "sequence_gap": (_case_sequence_gap, "order sequence numbers are missing", True, True, {303}, {}),
    "number_of_another_market": (_case_number_of_another_market, "have a number that is not", True, True, {301}, {}),
    "order_inside_replenishment_window": (_case_order_inside_the_replenishment_window,
                                          "dated at or before the replenishment pilot's last event", False, True,
                                          None, {"14_verify_replenishment.sql": {506}}),
    "price_not_from_price_list": (_case_price_not_from_the_price_list, "price or cost other than the product_prices",
                                  True, True, {404}, {}),
    "customer_of_another_market": (_case_customer_of_another_market, "a customer of another market", True, True,
                                   {305}, {}),
    "receipt_at_a_store": (_case_receipt_at_a_store, "purchase receipts are not at a warehouse", True, True, {201}, {}),
    "receipt_reusing_pilot_po": (_case_receipt_reusing_a_pilot_purchase_order,
                                 "reuse a document number of the replenishment pilot", False, True, set(),
                                 {"14_verify_replenishment.sql": {101, 103, 306}}),
    "arrival_at_another_store": (_case_arrival_at_another_store, "transfer arrivals do not match", True, True,
                                 {203}, {}),
    "malformed_transfer_reference": (_case_malformed_transfer_reference, "or their reference is not TR-", True, True,
                                     {202}, {}),
    "document_number_two_destinations": (_case_one_document_number_two_destinations,
                                         "used for more than one location, time or destination", True, True,
                                         {204}, {}),
    "stock_adjustment": (_case_stock_adjustment, "stock adjustments, write-offs or return restocks", True, True,
                         {110}, {}),
    "movement_in_the_future": (_case_movement_in_the_future, "stock movements are dated in the future", True, True,
                               {108}, {}),
    "extra_listing_opening_not_at_count": (_case_extra_listing_opening_not_at_the_count,
                                           "opening balances at listings outside the pilot", True, True, {106}, {}),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_invalid_later_records_are_refused(fresh_db, pilot, plan, inventory, pilot_dataset, case):
    setup, text, tx_refuses, repl_refuses, failing_15, failing_other = REFUSED[case]
    with _connect(fresh_db) as conn:
        ctx = {"templates": _templates(conn, pilot), "dataset": pilot_dataset, "pilot": pilot,
               "sku": _stocked_sku(inventory, "SE-WH-01")}
        setup(conn, ctx)
        before = _counts(conn)
        for refuses, planner, applier, error, subject in (
                (tx_refuses, plan_transactions, apply_transactions, TransactionConflictError, pilot),
                (repl_refuses, plan_replenishment, apply_replenishment, ReplenishmentConflictError, plan)):
            progress = planner(conn, subject)
            if refuses:
                assert not progress.ok
                assert any(text in b for b in progress.blocking), progress.blocking
                with pytest.raises(error):
                    applier(conn, subject)
            else:
                assert progress.ok, progress.conflicts + progress.blocking
                assert applier(conn, subject).events_run == 0
        assert _counts(conn) == before                                   # nothing was written
        if failing_15 is not None:                                       # None: business-time dependent, not asserted
            found = _failing(conn, "15_verify_inventory_and_activity.sql")
            assert failing_15 <= found if failing_15 else found == set()
        for script, checks in failing_other.items():
            assert checks <= _failing(conn, script)


# --------------------------------------------------------------------------- the pilots' own records
def test_the_pilots_own_records_are_still_compared_exactly(fresh_db, pilot, plan, inventory):
    """Valid later activity does not loosen the comparison of the pilot's own records."""
    revision = plan.revisions[0]
    change = revision.changes[0]
    with _connect(fresh_db) as conn:
        _valid_later_activity(conn, pilot, inventory)
        conn.execute("UPDATE inventory_balances b SET reorder_point = reorder_point + 1 FROM stock_locations l, products p "
                     "WHERE l.stock_location_id = b.stock_location_id AND p.product_id = b.product_id "
                     "AND l.location_code = %s AND p.sku = %s", (revision.warehouse, change.sku))
        progress = plan_replenishment(conn, plan)
        assert any(f"{revision.warehouse}/{change.sku}: reorder point" in c for c in progress.conflicts)
        assert plan_transactions(conn, pilot).ok                         # the transaction pilot has no reorder points
        assert 111 in _failing(conn, "14_verify_replenishment.sql")


def test_a_later_sale_at_a_pilot_listing_is_counted_in_the_pilot_stock_check(fresh_db, pilot, plan, inventory):
    """The expected stock of the pilot listings includes later sales and reservations, nothing else."""
    with _connect(fresh_db) as conn:
        made = _valid_later_activity(conn, pilot, inventory)
        tx = plan_transactions(conn, pilot)
        sold = {}
        for o in made["orders"][:-1]:
            for x in o.lines:
                key = (o.fulfilment_location_code, x.sku)
                sold[key] = sold.get(key, 0) + x.quantity
        for key, units in sold.items():
            receipts = 6 if key == ("SE-WH-01", made["sku"]) else 0
            moved = -2 if key == ("SE-WH-01", made["sku"]) else 2 if key == ("SE-STO-01", made["sku"]) else 0
            replenishment = sum(change for (k, change) in _replenishment_changes(plan).items() if k == key)
            assert tx.later.on_hand_change[key] == replenishment + receipts + moved - units


def _replenishment_changes(plan) -> dict:
    out: dict = {}
    for po, d in plan.deliveries():
        for x in d.lines:
            out[(po.warehouse, x.sku)] = out.get((po.warehouse, x.sku), 0) + x.quantity
    for t in plan.transfers:
        for x in t.lines:
            out[(t.source, x.sku)] = out.get((t.source, x.sku), 0) - x.quantity
            out[(t.destination, x.sku)] = out.get((t.destination, x.sku), 0) + x.quantity
    return out


# --------------------------------------------------------------------------- business-time order
def test_later_activity_before_the_replenishment_pilot_is_complete_blocks_it(pilot_only_db, pilot, plan, inventory):
    with _connect(pilot_only_db) as conn:
        with pytest.raises(LoadInterrupted):
            apply_replenishment(conn, plan, stop_after=9)
        _receive(conn, "PO-SE-2024-000002/DN1", "SE-WH-01", datetime(2024, 12, 9, 8, tzinfo=UTC),
                 [(_stocked_sku(inventory, "SE-WH-01"), 4)])
        before = _counts(conn)
        progress = plan_replenishment(conn, plan)
        assert any("business-time order" in b for b in progress.blocking)
        with pytest.raises(ReplenishmentConflictError):
            apply_replenishment(conn, plan)
        assert _counts(conn) == before
        tx = plan_transactions(conn, pilot)                        # for the transaction pilot all of it is later
        assert tx.ok, tx.conflicts + tx.blocking
        assert tx.later.by_type["purchase_receipt"] >= 1


# --------------------------------------------------------------------------- command line
def test_the_command_line_plans_pass_with_later_activity_and_fail_on_invalid_records(fresh_db, pilot, inventory,
                                                                                      monkeypatch, tmp_path):
    monkeypatch.setenv("PGDATABASE", fresh_db)
    config = tmp_path / "generator.toml"
    config.write_text((REPO_ROOT / "config" / "generator.toml").read_text(encoding="utf-8")
                      .replace('log_dir = "logs"', f'log_dir = "{(tmp_path / "logs").as_posix()}"'), encoding="utf-8")
    base = ["--config", str(config), "--no-csv", "--env-file", str(tmp_path / "none.env")]
    with _connect(fresh_db) as conn:
        _valid_later_activity(conn, pilot, inventory)
    assert tx_cli.main(base + ["--plan"]) == 0
    assert repl_cli.main(base + ["--plan"]) == 0
    assert tx_cli.main(base + ["--apply", "--yes"]) == 0              # nothing to write, verified
    assert repl_cli.main(base + ["--apply", "--yes"]) == 0
    log_text = max((tmp_path / "logs").glob("transactions_pilot_plan_*.log")).read_text(encoding="utf-8")
    assert "later activity outside the pilot" in log_text and "4 orders (1 open)" in log_text
    with _connect(fresh_db) as conn:
        _insert(conn, "SE-WH-01", _stocked_sku(inventory, "SE-WH-01"), "stock_adjustment", -1,
                datetime(2024, 12, 20, 8, tzinfo=UTC), "SC-2024-000001")
    assert tx_cli.main(base + ["--plan"]) == 2
    assert repl_cli.main(base + ["--plan"]) == 3                      # precondition: the pilot transactions refuse
