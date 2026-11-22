"""Database behaviour tests for the pilot transaction loader (opt-in).

They prove against real PostgreSQL that every order runs through the V006/V007 rules and
functions, that repeated and interrupted runs never duplicate anything, and that a
database that differs from the plan is refused before anything is written.

Safety
  * Runs only when NORDICCART_DB_TESTS=1 is set; otherwise every test is skipped.
  * Creates its own databases (nordiccart_tx_test_*) on the server named by the standard
    PG variables, applies V001 to V007, the master data pilot and the opening inventory to
    them, and drops them at the end. It never connects to nordiccart_oltp.
  * The role must be allowed to create databases.

Run from the repository folder (PowerShell):
    $env:NORDICCART_DB_TESTS = "1"
    python -m pytest tests/python/test_transactions_db.py -v
"""

from __future__ import annotations

import dataclasses
import os
from datetime import timedelta
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from nordiccart.inventory.config import load_inventory_settings  # noqa: E402
from nordiccart.inventory.generate import generate_opening_inventory  # noqa: E402
from nordiccart.inventory.load import apply_inventory  # noqa: E402
from nordiccart.master_data.load import PreconditionError, apply_load  # noqa: E402
from nordiccart.settings import load_env_file  # noqa: E402
from nordiccart.transactions import cli  # noqa: E402
from nordiccart.transactions.config import load_transaction_settings  # noqa: E402
from nordiccart.transactions.generate import generate_transactions  # noqa: E402
from nordiccart.transactions.load import (  # noqa: E402
    ADVISORY_LOCK_KEY, LoaderBusyError, LoadInterrupted, OutcomeMismatchError, TransactionConflictError,
    VerificationError, apply_transactions, check_transaction_database, plan_transactions, verify_transactions,
)
from nordiccart.transactions import load as tx_load  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("NORDICCART_DB_TESTS") != "1",
                                reason="set NORDICCART_DB_TESTS=1 to run the database tests")

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "nordiccart_tx_test"
TEMPLATE_MASTER = f"{PREFIX}_template_master"      # migrations + master data pilot
TEMPLATE_STOCK = f"{PREFIX}_template_stock"        # + opening inventory
SCRIPT_13 = (52, 45, 7, 0)      # rows, PASS, INFO, FAIL of script 13 revision 3 (S1: scoped; was 50, 44, 6, 0)


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
def plan(pilot_settings, pilot_dataset, inventory):
    inv = load_inventory_settings(pilot_settings)
    tx = load_transaction_settings(pilot_settings, inv.opening_date)
    return generate_transactions(pilot_settings, tx, pilot_dataset, inventory, inv.store_format_weight)


@pytest.fixture(scope="module")
def templates(pilot_dataset, inventory):
    load_env_file()
    with _admin() as admin:
        for name in (TEMPLATE_MASTER, TEMPLATE_STOCK):
            _drop(admin, name)
        admin.execute(f'CREATE DATABASE "{TEMPLATE_MASTER}" WITH ENCODING \'UTF8\' TEMPLATE template0')
        admin.execute(f'ALTER DATABASE "{TEMPLATE_MASTER}" SET timezone TO \'UTC\'')
    with _connect(TEMPLATE_MASTER) as conn:
        for path in sorted((REPO_ROOT / "database" / "migrations").glob("V00[1-7]__*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
        apply_load(conn, pilot_dataset)
    with _admin() as admin:
        admin.execute(f'CREATE DATABASE "{TEMPLATE_STOCK}" TEMPLATE "{TEMPLATE_MASTER}"')
    with _connect(TEMPLATE_STOCK) as conn:
        apply_inventory(conn, inventory)
    yield
    with _admin() as admin:
        for name in (TEMPLATE_STOCK, TEMPLATE_MASTER):
            _drop(admin, name)


@pytest.fixture
def fresh_db(templates, request):
    name = f"{PREFIX}_{request.node.name}"[:63].lower().replace("[", "_").replace("]", "").replace("-", "_")
    with _admin() as admin:
        _drop(admin, name)
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_STOCK}"')
    yield name
    with _admin() as admin:
        _drop(admin, name)


def _counts(conn) -> dict:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("orders", "order_items", "payment_attempts", "inventory_reservations", "inventory_movements",
                      "change_log")}


def _with(plan, number: str, **changes):
    return dataclasses.replace(plan, orders=[dataclasses.replace(o, **changes) if o.order_number == number else o
                                             for o in plan.orders])


def _sql(conn, script: str):
    rows = conn.execute((REPO_ROOT / "sql" / "verification" / script).read_text(encoding="utf-8")).fetchall()
    statuses = [r[5] for r in rows]
    return len(rows), statuses.count("PASS"), statuses.count("INFO"), statuses.count("FAIL")


# --------------------------------------------------------------------------- loading
def test_plan_is_read_only(fresh_db, plan, pilot_dataset, inventory):
    with _connect(fresh_db) as conn:
        check_transaction_database(conn, pilot_dataset, inventory)
        before = _counts(conn)
        progress = plan_transactions(conn, plan)
        assert progress.ok and progress.counts() == (20, 100) and not progress.done
        assert _counts(conn) == before


def test_apply_runs_every_unit_of_work_and_a_rerun_does_nothing(fresh_db, plan):
    with _connect(fresh_db) as conn:
        result = apply_transactions(conn, plan)
        assert (result.events_run, result.orders_placed, result.verified_orders) == (100, 20, 20)
        assert result.by_kind == {"place": 20, "reserve": 20, "payment_start": 21, "payment_result": 21, "fulfil": 14,
                                  "customer_cancel": 1, "expire": 1, "cancel_out_of_stock": 2}
        counts = _counts(conn)
        assert {k: counts[k] for k in ("orders", "order_items", "payment_attempts", "inventory_reservations")} == \
            {"orders": 20, "order_items": 26, "payment_attempts": 21, "inventory_reservations": 23}
        assert counts["inventory_movements"] == 288 + 17
        again = apply_transactions(conn, plan)
        assert again.events_run == 0 and again.change_log_rows == 0 and _counts(conn) == counts


def test_final_state_matches_the_plan_and_the_ledger(fresh_db, plan):
    with _connect(fresh_db) as conn:
        apply_transactions(conn, plan)
        assert verify_transactions(conn, plan) == (20, [])
        statuses = dict(conn.execute("SELECT status::text || coalesce(' ' || cancellation_reason, ''), count(*) "
                                     "FROM orders GROUP BY 1").fetchall())
        assert statuses == {"fulfilled": 14, "payment_failed": 2, "cancelled customer_request": 1,
                            "cancelled out_of_stock": 2, "cancelled reservation_expired": 1}
        on_hand, reserved = conn.execute("SELECT sum(quantity_on_hand), sum(quantity_reserved) FROM inventory_balances").fetchone()
        assert (on_hand, reserved) == (1441, 0)
        assert plan.replay().snapshot() == {
            (loc, sku): (oh, rs) for loc, sku, oh, rs in conn.execute(
                "SELECT l.location_code, p.sku, b.quantity_on_hand, b.quantity_reserved FROM inventory_balances b "
                "JOIN stock_locations l USING (stock_location_id) JOIN products p USING (product_id)").fetchall()}


def test_sql_verification_13_passes_and_scripts_11_and_12_stay_valid(fresh_db, plan):
    with _connect(fresh_db) as conn:
        assert _sql(conn, "11_verify_master_data.sql") == (44, 33, 11, 0)
        assert _sql(conn, "12_verify_opening_inventory.sql") == (24, 17, 7, 0)
        apply_transactions(conn, plan)
        assert _sql(conn, "13_verify_pilot_transactions.sql") == SCRIPT_13
        assert _sql(conn, "11_verify_master_data.sql") == (44, 33, 11, 0)
        assert _sql(conn, "12_verify_opening_inventory.sql") == (24, 17, 7, 0)


def test_out_of_stock_orders_leave_no_reservation_or_payment(fresh_db, plan):
    with _connect(fresh_db) as conn:
        apply_transactions(conn, plan)
        rows = conn.execute("""
            SELECT o.order_number,
                   (SELECT count(*) FROM inventory_reservations r JOIN order_items i USING (order_item_id) WHERE i.order_id = o.order_id),
                   (SELECT count(*) FROM payment_attempts p WHERE p.order_id = o.order_id)
              FROM orders o WHERE o.cancellation_reason = 'out_of_stock' ORDER BY 1""").fetchall()
    expected = sorted(o.order_number for o in plan.orders if o.scenario.startswith("out_of_stock"))
    assert [r[0] for r in rows] == expected and all(r[1:] == (0, 0) for r in rows)


def test_reserved_stock_is_not_sold_to_the_next_customer(fresh_db, plan):
    """The holder's reservation keeps the last unit: on hand 1, reserved 1, so U2 for the next order is out_of_stock."""
    oos = next(o for o in plan.orders if o.scenario == "out_of_stock_reserved")
    holder = plan.order(oos.holder_order_number)
    events = plan.events()
    stop = next(i for i, e in enumerate(events) if e.order_number == oos.order_number and e.kind == "reserve")
    with _connect(fresh_db) as conn:
        with pytest.raises(LoadInterrupted):
            apply_transactions(conn, plan, stop_after=stop)
        row = conn.execute("""
            SELECT b.quantity_on_hand, b.quantity_reserved FROM inventory_balances b
              JOIN stock_locations l USING (stock_location_id) JOIN products p USING (product_id)
             WHERE l.location_code = %s AND p.sku = %s""", (oos.store_code, oos.lines[0].sku)).fetchone()
        assert row == (1, 1)
        order_id = conn.execute("SELECT order_id FROM orders WHERE order_number = %s", (oos.order_number,)).fetchone()[0]
        assert conn.execute("SELECT fn_reserve_order_stock(%s, %s, %s)",
                            (order_id, oos.reserve_at, oos.reservation_expires_at)).fetchone()[0] == "out_of_stock"
        apply_transactions(conn, plan)
        assert conn.execute("SELECT status::text FROM orders WHERE order_number = %s",
                            (holder.order_number,)).fetchone()[0] == "fulfilled"


# --------------------------------------------------------------------------- idempotency and recovery
@pytest.mark.parametrize("stop_after", [1, 2, 13, 37, 58, 99])
def test_an_interrupted_run_continues_where_it_stopped(fresh_db, plan, stop_after):
    with _connect(fresh_db) as conn:
        with pytest.raises(LoadInterrupted):
            apply_transactions(conn, plan, stop_after=stop_after)
        progress = plan_transactions(conn, plan)
        assert progress.ok and len(progress.done) == stop_after and len(progress.remaining) == 100 - stop_after
        result = apply_transactions(conn, plan)
        assert result.events_run == 100 - stop_after
        assert verify_transactions(conn, plan) == (20, [])
        assert _sql(conn, "13_verify_pilot_transactions.sql") == SCRIPT_13


def test_two_loaders_at_once_are_refused(fresh_db, plan):
    with _connect(fresh_db) as holder, _connect(fresh_db) as conn:
        holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (ADVISORY_LOCK_KEY,))
        before = _counts(conn)
        with pytest.raises(LoaderBusyError):
            apply_transactions(conn, plan)
        assert _counts(conn) == before
        holder.execute("SELECT pg_advisory_unlock_all()")
        assert apply_transactions(conn, plan).events_run == 100


def test_an_order_with_other_values_is_a_conflict(fresh_db, plan, pilot_dataset):
    first = plan.orders[0]
    other = next(c.customer_number for c in pilot_dataset.customers
                 if c.country_code == first.country_code and c.customer_number != first.customer_number
                 and c.registered_at < first.placed_at)
    with _connect(fresh_db) as conn:
        with pytest.raises(LoadInterrupted):
            apply_transactions(conn, _with(plan, first.order_number, customer_number=other), stop_after=1)
        before = _counts(conn)
        progress = plan_transactions(conn, plan)
        assert any("exists with other values" in c for c in progress.conflicts)
        with pytest.raises(TransactionConflictError):
            apply_transactions(conn, plan)
        assert _counts(conn) == before


def test_orders_outside_the_plan_block_the_run(fresh_db, plan):
    smaller = dataclasses.replace(plan, orders=plan.orders[:-1])
    with _connect(fresh_db) as conn:
        apply_transactions(conn, plan)
        progress = plan_transactions(conn, smaller)
        assert any("not part of this plan" in b for b in progress.blocking)
        with pytest.raises(TransactionConflictError):
            apply_transactions(conn, smaller)


def test_other_stock_movements_block_the_run(fresh_db, plan):
    with _connect(fresh_db) as conn:
        conn.execute("""
            INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at,
                                             external_reference)
            SELECT l.stock_location_id, p.product_id, 'purchase_receipt', 5, '2024-11-01 06:00+00', 'PO-TEST-1'
              FROM stock_locations l, products p WHERE l.location_code = 'SE-WH-01' AND p.sku = 'ACC-000010'""")
        before = _counts(conn)
        progress = plan_transactions(conn, plan)
        assert any("other than opening balances and sales" in b for b in progress.blocking)
        assert any("stock balances differ" in b for b in progress.blocking)
        with pytest.raises(TransactionConflictError):
            apply_transactions(conn, plan)
        assert _counts(conn) == before


def _movement(conn, location, sku, kind, quantity, at, reference, related=None):
    return conn.execute(
        "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, "
        "external_reference, related_movement_id) SELECT l.stock_location_id, p.product_id, %s::movement_type, %s, %s, %s, %s "
        "FROM stock_locations l, products p WHERE l.location_code = %s AND p.sku = %s RETURNING movement_id",
        (kind, quantity, at, reference, related, location, sku)).fetchone()[0]


def test_replenishment_after_the_pilot_is_accepted_and_the_pilot_still_verifies(fresh_db, plan):
    """Receipts and transfers dated after the pilot's last event are later activity, counted in the stock check."""
    later = plan.last_event_at() + timedelta(days=5)
    with _connect(fresh_db) as conn:
        apply_transactions(conn, plan)
        _movement(conn, "SE-WH-01", "ACC-000010", "purchase_receipt", 5, later, "PO-SE-2024-000901/DN1")
        out = _movement(conn, "SE-WH-01", "ACC-000010", "transfer_out", -1, later + timedelta(hours=1), "TR-SE-2024-000901-SE-STO-01")
        _movement(conn, "SE-STO-01", "ACC-000010", "transfer_in", 1, later + timedelta(hours=2), "TR-SE-2024-000901-SE-STO-01", out)
        _movement(conn, "DK-WH-01", "ACC-000010", "transfer_out", -1, later + timedelta(hours=3), "TR-DK-2024-000901-DK-STO-01")
        progress = plan_transactions(conn, plan)                        # one transfer is still in transit
        assert progress.ok and progress.counts() == (0, 0) and len(progress.done) == 100
        assert (progress.later_movements, progress.later_units) == (4, 4)
        assert verify_transactions(conn, plan) == (20, [])
        assert apply_transactions(conn, plan).events_run == 0
        assert _sql(conn, "13_verify_pilot_transactions.sql") == SCRIPT_13


def test_replenishment_before_the_pilot_is_complete_blocks(fresh_db, plan):
    """Writing the remaining pilot steps after later replenishment would break business-time order."""
    with _connect(fresh_db) as conn:
        with pytest.raises(LoadInterrupted):
            apply_transactions(conn, plan, stop_after=50)
        _movement(conn, "SE-WH-01", "ACC-000010", "purchase_receipt", 5, plan.last_event_at() + timedelta(days=5),
                  "PO-SE-2024-000901/DN1")
        before = _counts(conn)
        progress = plan_transactions(conn, plan)
        assert any("business-time order" in b for b in progress.blocking)
        with pytest.raises(TransactionConflictError):
            apply_transactions(conn, plan)
        assert _counts(conn) == before


def test_an_unplanned_database_result_rolls_back_that_step_and_stops(fresh_db, plan):
    oos = next(o for o in plan.orders if o.scenario == "out_of_stock_quantity")
    wrong = _with(plan, oos.order_number, reserve_outcome="reserved")    # the plan now (wrongly) expects stock
    with _connect(fresh_db) as conn:
        with pytest.raises(OutcomeMismatchError, match="out_of_stock"):
            apply_transactions(conn, wrong)
        status, reservations = conn.execute("""
            SELECT o.status::text, (SELECT count(*) FROM inventory_reservations r JOIN order_items i USING (order_item_id)
                                     WHERE i.order_id = o.order_id)
              FROM orders o WHERE o.order_number = %s""", (oos.order_number,)).fetchone()
        assert (status, reservations) == ("placed", 0)
        events = plan.events()
        position = next(i for i, e in enumerate(events) if e.order_number == oos.order_number and e.kind == "reserve")
        assert len(plan_transactions(conn, plan).done) == position     # nothing after the failing step ran
        assert apply_transactions(conn, plan).events_run == 100 - position


def test_verification_catches_a_step_that_did_not_happen(fresh_db, plan, monkeypatch):
    skipped = next(o for o in plan.orders if o.final_status == "fulfilled" and o.channel == "online")
    real = tx_load._fulfil

    def fulfil_all_but_one(conn, ids, o):
        return "skipped (test)" if o.order_number == skipped.order_number else real(conn, ids, o)

    monkeypatch.setattr(tx_load, "_fulfil", fulfil_all_but_one)
    with _connect(fresh_db) as conn:
        with pytest.raises(VerificationError, match="not in the database"):
            apply_transactions(conn, plan)


def test_database_safeguards_stay_active_after_the_load(fresh_db, plan):
    fulfilled = next(o for o in plan.orders if o.final_status == "fulfilled")
    with _connect(fresh_db) as conn:
        apply_transactions(conn, plan)
        order_id = conn.execute("SELECT order_id FROM orders WHERE order_number = %s", (fulfilled.order_number,)).fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            conn.execute("UPDATE inventory_balances SET quantity_on_hand = quantity_on_hand + 1 WHERE inventory_balance_id = 1")
        assert exc.value.diag.constraint_name == "inventory_balances_quantities_by_ledger"
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            conn.execute("UPDATE orders SET status = 'cancelled', cancellation_reason = 'customer_request', "
                         "cancelled_at = now() WHERE order_id = %s", (order_id,))
        assert exc.value.diag.constraint_name == "orders_status_transition"
        assert conn.execute("SELECT fn_fulfil_order_stock(%s, now())", (order_id,)).fetchone()[0] == "already_fulfilled"
        with pytest.raises(psycopg.errors.CheckViolation) as exc:      # a second sale of a fulfilled line
            conn.execute("""INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change,
                            occurred_at, order_item_id)
                            SELECT m.stock_location_id, m.product_id, 'sale', m.quantity_change, m.occurred_at, m.order_item_id
                              FROM inventory_movements m JOIN order_items i USING (order_item_id)
                             WHERE i.order_id = %s LIMIT 1""", (order_id,))
        assert exc.value.diag.constraint_name == "inventory_movements_sale_order_state"


def test_missing_opening_inventory_is_refused(templates, plan, pilot_dataset, inventory):
    name = f"{PREFIX}_no_stock"
    with _admin() as admin:
        _drop(admin, name)
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_MASTER}"')
    try:
        with _connect(name) as conn:
            with pytest.raises(PreconditionError, match="opening inventory"):
                check_transaction_database(conn, pilot_dataset, inventory)
    finally:
        with _admin() as admin:
            _drop(admin, name)


def test_command_line_plan_apply_and_rerun(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setenv("PGDATABASE", fresh_db)
    config = tmp_path / "generator.toml"
    config.write_text((REPO_ROOT / "config" / "generator.toml").read_text(encoding="utf-8")
                      .replace('log_dir = "logs"', f'log_dir = "{(tmp_path / "logs").as_posix()}"'), encoding="utf-8")
    base = ["--config", str(config), "--no-csv", "--env-file", str(tmp_path / "none.env")]
    assert cli.main(base + ["--plan"]) == 0
    assert cli.main(base + ["--apply"]) == 4             # not an interactive terminal and no --yes: refused
    with _connect(fresh_db) as conn:
        assert _counts(conn)["orders"] == 0
    assert cli.main(base + ["--apply", "--yes"]) == 0
    assert cli.main(base + ["--apply", "--yes"]) == 0
    with _connect(fresh_db) as conn:
        assert _counts(conn)["orders"] == 20
    log = max((tmp_path / "logs").glob("transactions_pilot_apply_*.log")).read_text(encoding="utf-8")
    assert "nothing to write" in log
