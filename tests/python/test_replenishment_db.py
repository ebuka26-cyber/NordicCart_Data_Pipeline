"""Database behaviour tests for the replenishment pilot loader (opt-in).

They prove against real PostgreSQL that receipts, transfers and the reorder point revision
run through the V007 rules, that repeated and interrupted runs never duplicate a receipt or
a transfer, that a database that differs from the plan is refused before anything is
written, that the pilot transactions still verify afterwards, and which rules only the
verification script (14) enforces because V007 accepts the movement.

Safety
  * Runs only when NORDICCART_DB_TESTS=1 is set; otherwise every test is skipped.
  * Creates its own databases (nordiccart_repl_test_*) on the server named by the standard PG
    variables, applies V001 to V007, the master data pilot, the opening inventory and the 20
    pilot orders to them, and drops them at the end. It never connects to nordiccart_oltp.
  * The role must be allowed to create databases.

Run from the repository folder (PowerShell):
    $env:NORDICCART_DB_TESTS = "1"
    python -m pytest tests/python/test_replenishment_db.py -v
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from nordiccart.inventory.config import load_inventory_settings  # noqa: E402
from nordiccart.inventory.generate import generate_opening_inventory  # noqa: E402
from nordiccart.inventory.load import apply_inventory, plan_inventory  # noqa: E402
from nordiccart.master_data.load import PreconditionError, apply_load  # noqa: E402
from nordiccart.replenishment import cli  # noqa: E402
from nordiccart.replenishment.config import load_replenishment_settings  # noqa: E402
from nordiccart.replenishment.generate import generate_replenishment  # noqa: E402
from nordiccart.replenishment.load import (  # noqa: E402
    ADVISORY_LOCK_KEY, LoaderBusyError, LoadInterrupted, OutcomeMismatchError, ReplenishmentConflictError,
    apply_replenishment, check_replenishment_database, plan_replenishment, verify_replenishment,
)
from nordiccart.settings import load_env_file  # noqa: E402
from nordiccart.transactions import cli as tx_cli  # noqa: E402
from nordiccart.transactions import load as tx_load  # noqa: E402
from nordiccart.transactions.config import load_transaction_settings  # noqa: E402
from nordiccart.transactions.generate import generate_transactions  # noqa: E402
from nordiccart.transactions.load import apply_transactions, plan_transactions, verify_transactions  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("NORDICCART_DB_TESTS") != "1",
                                reason="set NORDICCART_DB_TESTS=1 to run the database tests")

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "nordiccart_repl_test"
TEMPLATE_STOCK = f"{PREFIX}_template_stock"        # migrations + master data + opening inventory
TEMPLATE_PILOT = f"{PREFIX}_template_pilot"        # + the 20 pilot orders (100 units of work)
UNITS_OF_WORK = 17
SCRIPT_13 = (52, 45, 7, 0)      # rows, PASS, INFO, FAIL of script 13 revision 3 (S1: scoped; was 50, 44, 6, 0)
SCRIPT_14 = (44, 37, 7, 0)      # script 14 revision 2 (S1: scoped; was 43, 36, 7, 0)


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
def inv(pilot_settings):
    return load_inventory_settings(pilot_settings)


@pytest.fixture(scope="module")
def inventory(pilot_settings, inv, pilot_dataset):
    return generate_opening_inventory(pilot_settings, inv, pilot_dataset)


@pytest.fixture(scope="module")
def pilot(pilot_settings, inv, pilot_dataset, inventory):
    tx = load_transaction_settings(pilot_settings, inv.opening_date)
    return generate_transactions(pilot_settings, tx, pilot_dataset, inventory, inv.store_format_weight)


@pytest.fixture(scope="module")
def plan(pilot_settings, inv, pilot_dataset, inventory, pilot):
    tx = load_transaction_settings(pilot_settings, inv.opening_date)
    rp = load_replenishment_settings(pilot_settings, tx.store_hours, {c.category_code for c in pilot_dataset.product_categories})
    return generate_replenishment(pilot_settings, inv, rp, pilot_dataset, inventory, pilot)


@pytest.fixture(scope="module")
def templates(pilot_dataset, inventory, pilot):
    load_env_file()
    with _admin() as admin:
        for name in (TEMPLATE_PILOT, TEMPLATE_STOCK):
            _drop(admin, name)
        admin.execute(f'CREATE DATABASE "{TEMPLATE_STOCK}" WITH ENCODING \'UTF8\' TEMPLATE template0')
        admin.execute(f'ALTER DATABASE "{TEMPLATE_STOCK}" SET timezone TO \'UTC\'')
    with _connect(TEMPLATE_STOCK) as conn:
        for path in sorted((REPO_ROOT / "database" / "migrations").glob("V00[1-7]__*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
        apply_load(conn, pilot_dataset)
        apply_inventory(conn, inventory)
    with _admin() as admin:
        admin.execute(f'CREATE DATABASE "{TEMPLATE_PILOT}" TEMPLATE "{TEMPLATE_STOCK}"')
    with _connect(TEMPLATE_PILOT) as conn:
        assert apply_transactions(conn, pilot).events_run == 100
    yield
    with _admin() as admin:
        for name in (TEMPLATE_PILOT, TEMPLATE_STOCK):
            _drop(admin, name)


@pytest.fixture
def fresh_db(templates, request):
    name = f"{PREFIX}_{request.node.name}"[:63].lower().replace("[", "_").replace("]", "").replace("-", "_")
    with _admin() as admin:
        _drop(admin, name)
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_PILOT}"')
    yield name
    with _admin() as admin:
        _drop(admin, name)


def _counts(conn) -> dict:
    out = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
           for t in ("orders", "inventory_movements", "inventory_reservations", "change_log")}
    out["reorder_points"] = conn.execute("SELECT sum(reorder_point) FROM inventory_balances").fetchone()[0]
    return out


def _sql(conn, script: str):
    rows = conn.execute((REPO_ROOT / "sql" / "verification" / script).read_text(encoding="utf-8")).fetchall()
    statuses = [r[5] for r in rows]
    return len(rows), statuses.count("PASS"), statuses.count("INFO"), statuses.count("FAIL")


def _failing(conn, script: str) -> set[int]:
    rows = conn.execute((REPO_ROOT / "sql" / "verification" / script).read_text(encoding="utf-8")).fetchall()
    return {r[0] for r in rows if r[5] == "FAIL"}


def _position(plan, kind: str, key_start: str) -> int:
    """Number of units of work before the first event of this kind whose key starts with key_start."""
    return next(i for i, e in enumerate(plan.events()) if e.kind == kind and e.key.startswith(key_start))


def _insert(conn, location: str, sku: str, kind: str, quantity: int, at, reference, related=None):
    return conn.execute(
        "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, "
        "external_reference, related_movement_id) SELECT l.stock_location_id, p.product_id, %s::movement_type, %s, %s, %s, %s "
        "FROM stock_locations l, products p WHERE l.location_code = %s AND p.sku = %s RETURNING movement_id",
        (kind, quantity, at, reference, related, location, sku)).fetchone()[0]


# --------------------------------------------------------------------------- loading
def test_plan_is_read_only(fresh_db, plan, pilot_dataset, inventory, pilot):
    with _connect(fresh_db) as conn:
        check_replenishment_database(conn, pilot_dataset, inventory, pilot)
        before = _counts(conn)
        progress = plan_replenishment(conn, plan)
        assert progress.ok and progress.counts() == (0, UNITS_OF_WORK)
        assert _counts(conn) == before


def test_apply_runs_every_unit_of_work_and_a_rerun_does_nothing(fresh_db, plan):
    with _connect(fresh_db) as conn:
        result = apply_replenishment(conn, plan)
        assert (result.events_run, result.verified_events) == (UNITS_OF_WORK, UNITS_OF_WORK)
        assert result.by_kind == {"revise": 4, "dispatch": 4, "arrive": 4, "receive": 5}
        assert result.change_log_rows == 69 + 2 * (5 + 8 + 8)        # 69 reorder points, 21 movements + their balances
        counts = _counts(conn)
        assert counts["inventory_movements"] == 288 + 17 + 21 and counts["orders"] == 20
        again = apply_replenishment(conn, plan)
        assert again.events_run == 0 and again.change_log_rows == 0 and _counts(conn) == counts


def test_final_state_matches_the_plan_the_ledger_and_the_reorder_points(fresh_db, plan):
    with _connect(fresh_db) as conn:
        apply_replenishment(conn, plan)
        assert verify_replenishment(conn, plan) == (UNITS_OF_WORK, [])
        ledger, rops = plan.replay()
        rows = conn.execute("SELECT l.location_code, p.sku, b.quantity_on_hand, b.quantity_reserved, b.reorder_point "
                            "FROM inventory_balances b JOIN stock_locations l USING (stock_location_id) "
                            "JOIN products p USING (product_id)").fetchall()
        assert {(r[0], r[1]): (r[2], r[3]) for r in rows} == ledger.snapshot()
        assert {(r[0], r[1]): r[4] for r in rows} == rops
        assert conn.execute("SELECT sum(quantity_on_hand), sum(quantity_reserved) FROM inventory_balances").fetchone() == (1467, 0)
        in_transit = conn.execute("SELECT count(*) FROM inventory_movements o WHERE o.movement_type = 'transfer_out' AND NOT "
                                  "EXISTS (SELECT 1 FROM inventory_movements i WHERE i.related_movement_id = o.movement_id)"
                                  ).fetchone()[0]
        assert in_transit == 0


def test_sql_verification_scripts_pass_before_and_after(fresh_db, plan):
    with _connect(fresh_db) as conn:
        for _ in range(2):                                   # before, then after the replenishment
            assert _sql(conn, "11_verify_master_data.sql") == (44, 33, 11, 0)
            assert _sql(conn, "12_verify_opening_inventory.sql") == (24, 17, 7, 0)
            assert _sql(conn, "13_verify_pilot_transactions.sql") == SCRIPT_13
            apply_replenishment(conn, plan)
        assert _sql(conn, "14_verify_replenishment.sql") == SCRIPT_14


def test_the_transaction_loader_still_verifies_the_pilot_after_replenishment(fresh_db, plan, pilot):
    with _connect(fresh_db) as conn:
        apply_replenishment(conn, plan)
        progress = plan_transactions(conn, pilot)
        assert progress.ok and progress.counts() == (0, 0) and len(progress.done) == 100
        assert (progress.later_movements, progress.later_units) == (21, 26)
        assert verify_transactions(conn, pilot) == (20, [])
        assert apply_transactions(conn, pilot).events_run == 0


def test_the_reorder_point_revision_is_drift_for_the_opening_inventory_loader(fresh_db, plan, inventory):
    with _connect(fresh_db) as conn:
        apply_replenishment(conn, plan)
        inv_plan = plan_inventory(conn, inventory)
        assert not inv_plan.conflicts and inv_plan.total_to_insert == 0 and len(inv_plan.drift) == 69


# --------------------------------------------------------------------------- idempotency and recovery
@pytest.mark.parametrize("stop_after", [1, 4, 6, 9, 13, 16])
def test_an_interrupted_run_continues_where_it_stopped(fresh_db, plan, pilot, stop_after):
    with _connect(fresh_db) as conn:
        with pytest.raises(LoadInterrupted):
            apply_replenishment(conn, plan, stop_after=stop_after)
        progress = plan_replenishment(conn, plan)
        assert progress.ok and progress.counts() == (stop_after, UNITS_OF_WORK - stop_after)
        tx_progress = plan_transactions(conn, pilot)            # the pilot verifies even with stock in transit
        assert tx_progress.ok and not tx_progress.remaining
        result = apply_replenishment(conn, plan)
        assert result.events_run == UNITS_OF_WORK - stop_after
        assert verify_replenishment(conn, plan) == (UNITS_OF_WORK, [])
        assert _sql(conn, "14_verify_replenishment.sql") == SCRIPT_14


def test_two_loaders_at_once_are_refused(fresh_db, plan):
    with _connect(fresh_db) as holder, _connect(fresh_db) as conn:
        holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (ADVISORY_LOCK_KEY,))
        before = _counts(conn)
        with pytest.raises(LoaderBusyError):
            apply_replenishment(conn, plan)
        assert _counts(conn) == before
        holder.execute("SELECT pg_advisory_unlock_all()")
        assert apply_replenishment(conn, plan).events_run == UNITS_OF_WORK


def test_a_receipt_with_another_quantity_is_a_conflict(fresh_db, plan):
    stop = _position(plan, "receive", "")
    po, delivery = plan.delivery(plan.events()[stop].key)
    with _connect(fresh_db) as conn:
        with pytest.raises(LoadInterrupted):
            apply_replenishment(conn, plan, stop_after=stop)
        line = delivery.lines[0]
        _insert(conn, po.warehouse, line.sku, "purchase_receipt", line.quantity + 1, delivery.received_at, delivery.reference)
        before = _counts(conn)
        progress = plan_replenishment(conn, plan)
        assert any(delivery.reference in c and "receipt" in c for c in progress.conflicts)
        with pytest.raises(ReplenishmentConflictError):
            apply_replenishment(conn, plan)
        assert _counts(conn) == before


def test_an_arrival_at_another_store_is_a_conflict(fresh_db, plan):
    stop = _position(plan, "arrive", "TR-FI")
    t = plan.transfer(plan.events()[stop].key)
    with _connect(fresh_db) as conn:
        with pytest.raises(LoadInterrupted):
            apply_replenishment(conn, plan, stop_after=stop)
        out_id = conn.execute("SELECT movement_id FROM inventory_movements WHERE external_reference = %s "
                              "AND movement_type = 'transfer_out'", (t.reference,)).fetchone()[0]
        other_store = "FI-STO-01" if t.destination != "FI-STO-01" else "FI-STO-02"
        _insert(conn, other_store, t.lines[0].sku, "transfer_in", t.lines[0].quantity, t.arrived_at, t.reference, out_id)
        progress = plan_replenishment(conn, plan)                 # V007 accepted it; the loader does not
        assert any("transfer_in" in c and other_store in c for c in progress.conflicts)
        with pytest.raises(ReplenishmentConflictError):
            apply_replenishment(conn, plan)


def test_movements_inside_the_pilot_or_outside_the_plan_block_the_run(fresh_db, plan, pilot):
    with _connect(fresh_db) as conn:
        _insert(conn, "SE-WH-01", "ACC-000010", "purchase_receipt", 5, datetime(2024, 11, 10, 9, tzinfo=timezone.utc),
                "PO-SE-2024-999999/DN1")
        progress = plan_replenishment(conn, plan)
        assert any("at or before the pilot's last event" in b for b in progress.blocking)
        assert any("not part of this plan" in b for b in progress.blocking)
        assert not plan_transactions(conn, pilot).ok              # inside its window: the pilot loader blocks too
        with pytest.raises(ReplenishmentConflictError):
            apply_replenishment(conn, plan)


def test_a_stock_adjustment_blocks_the_run(fresh_db, plan):
    with _connect(fresh_db) as conn:
        _insert(conn, "SE-WH-01", "ACC-000010", "stock_adjustment", -1, datetime(2024, 11, 20, 9, tzinfo=timezone.utc),
                "SC-TEST-1")
        progress = plan_replenishment(conn, plan)
        assert any("adjustments" in b for b in progress.blocking)


def test_an_unplanned_shortage_rolls_back_that_step_and_stops(fresh_db, plan):
    first = plan.transfers[0]
    too_much = dataclasses.replace(plan, transfers=[dataclasses.replace(first, lines=(dataclasses.replace(
        first.lines[0], quantity=500, requested=500),) + first.lines[1:])] + plan.transfers[1:])
    position = _position(plan, "dispatch", first.reference)
    with _connect(fresh_db) as conn:
        with pytest.raises(OutcomeMismatchError, match="available"):
            apply_replenishment(conn, too_much)
        assert conn.execute("SELECT count(*) FROM inventory_movements WHERE movement_type = 'transfer_out'").fetchone()[0] == 0
        assert len(plan_replenishment(conn, plan).done) == position           # the steps before it are committed
        assert apply_replenishment(conn, plan).events_run == UNITS_OF_WORK - position


# --------------------------------------------------------------------------- database integrity
def test_the_database_refuses_duplicate_documents_and_moving_reserved_stock(fresh_db, plan, pilot):
    po, delivery = plan.delivery(plan.events()[_position(plan, "receive", "")].key)
    with _connect(fresh_db) as conn:
        apply_replenishment(conn, plan)
        line = delivery.lines[0]
        with pytest.raises(psycopg.errors.UniqueViolation) as exc:          # the same delivery note twice
            _insert(conn, po.warehouse, line.sku, "purchase_receipt", line.quantity, delivery.received_at, delivery.reference)
        assert exc.value.diag.constraint_name == "uq_inventory_movements_document_line"
        t = plan.transfers[0]
        out_id = conn.execute("SELECT movement_id FROM inventory_movements WHERE external_reference = %s AND "
                              "movement_type = 'transfer_out'", (t.reference,)).fetchone()[0]
        with pytest.raises(psycopg.errors.UniqueViolation) as exc:          # the same transfer arriving twice
            _insert(conn, t.destination, t.lines[0].sku, "transfer_in", t.lines[0].quantity, t.arrived_at, t.reference, out_id)
        assert exc.value.diag.constraint_name == "uq_inventory_movements_one_transfer_in_per_out"

        # Reserved units cannot be transferred: an online order reserves 1 unit at SE-WH-01, then a
        # transfer_out of the whole on-hand quantity is refused. Everything is rolled back.
        model = next(o for o in pilot.orders if o.fulfilment_location_code == "SE-WH-01" and o.final_status == "fulfilled")
        placed = datetime(2024, 12, 10, 10, tzinfo=timezone.utc)
        order = dataclasses.replace(model, order_number="SE-2024-99999999", placed_at=placed)
        sku = order.lines[0].sku
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            with conn.transaction():
                tx_load._place(conn, tx_load._Ids(conn), order)
                order_id = conn.execute("SELECT order_id FROM orders WHERE order_number = %s", (order.order_number,)).fetchone()[0]
                assert conn.execute("SELECT fn_reserve_order_stock(%s, %s, %s)",
                                    (order_id, placed + timedelta(seconds=5), placed + timedelta(minutes=15))).fetchone()[0] == "reserved"
                on_hand = conn.execute("SELECT b.quantity_on_hand FROM inventory_balances b JOIN stock_locations l USING "
                                       "(stock_location_id) JOIN products p USING (product_id) WHERE l.location_code = "
                                       "'SE-WH-01' AND p.sku = %s", (sku,)).fetchone()[0]
                _insert(conn, "SE-WH-01", sku, "transfer_out", -on_hand, placed + timedelta(minutes=1), "TR-SE-2024-999999-SE-STO-01")
        assert exc.value.diag.constraint_name == "ck_inventory_balances_no_oversell"
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 20


def test_v007_accepts_what_only_the_application_and_script_14_refuse(fresh_db, plan):
    """Receipt at a store, transfer across a border, backdated transfer: V007 stores them; the scripts fail.

    S1: scripts 13 and 14 are scoped to their pilots. The backdated transfer lies inside the pilots and
    fails both of them; the store receipt and the cross-border transfer are later activity (after the
    pilot) and fail the global script 15. The loader refuses all three.
    """
    with _connect(fresh_db) as conn:
        apply_replenishment(conn, plan)
        assert _failing(conn, "14_verify_replenishment.sql") == set()
        assert _failing(conn, "15_verify_inventory_and_activity.sql") == set()
        _insert(conn, "SE-STO-01", "ACC-000010", "purchase_receipt", 1, datetime(2024, 12, 3, 9, tzinfo=timezone.utc),
                "PO-SE-2024-000099/DN1")
        out_id = _insert(conn, "SE-WH-01", "ACC-000010", "transfer_out", -1, datetime(2024, 12, 4, 6, 10, tzinfo=timezone.utc),
                         "TR-SE-2024-000099-NO-STO-01")
        _insert(conn, "NO-STO-01", "ACC-000010", "transfer_in", 1, datetime(2024, 12, 4, 7, 30, tzinfo=timezone.utc),
                "TR-SE-2024-000099-NO-STO-01", out_id)
        _insert(conn, "DK-WH-01", "ACC-000010", "transfer_out", -1, datetime(2024, 11, 10, 7, tzinfo=timezone.utc),
                "TR-DK-2024-000099-DK-STO-02")
        # backdated into the pilot: not a pilot document, and the pilot's stock at its last movement changes
        assert {309, 505, 506} <= _failing(conn, "14_verify_replenishment.sql")
        assert {504, 505} <= _failing(conn, "13_verify_pilot_transactions.sql")
        # store receipt, cross-border transfer (out and arrival), transfer left in transit (lost)
        assert {201, 202, 203, 206} <= _failing(conn, "15_verify_inventory_and_activity.sql")
        progress = plan_replenishment(conn, plan)
        assert not progress.ok
        assert any("purchase receipts are not at a warehouse" in b for b in progress.blocking)
        assert any("transfers out are not from a warehouse" in b for b in progress.blocking)
        assert any("not part of this plan" in b for b in progress.blocking)


def test_a_database_without_the_complete_pilot_is_refused(templates, plan, pilot_dataset, inventory, pilot):
    name = f"{PREFIX}_no_pilot"
    with _admin() as admin:
        _drop(admin, name)
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_STOCK}"')
    try:
        with _connect(name) as conn:
            with pytest.raises(PreconditionError, match="pilot transactions are not complete"):
                check_replenishment_database(conn, pilot_dataset, inventory, pilot)
    finally:
        with _admin() as admin:
            _drop(admin, name)


# --------------------------------------------------------------------------- command line
def test_command_line_plan_apply_and_rerun(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setenv("PGDATABASE", fresh_db)
    config = tmp_path / "generator.toml"
    config.write_text((REPO_ROOT / "config" / "generator.toml").read_text(encoding="utf-8")
                      .replace('log_dir = "logs"', f'log_dir = "{(tmp_path / "logs").as_posix()}"'), encoding="utf-8")
    base = ["--config", str(config), "--no-csv", "--env-file", str(tmp_path / "none.env")]
    assert cli.main(base + ["--plan"]) == 0
    assert cli.main(base + ["--apply"]) == 4             # not an interactive terminal and no --yes: refused
    with _connect(fresh_db) as conn:
        assert conn.execute("SELECT count(*) FROM inventory_movements WHERE movement_type = 'purchase_receipt'").fetchone()[0] == 0
    assert cli.main(base + ["--apply", "--yes"]) == 0
    assert cli.main(base + ["--apply", "--yes"]) == 0
    log = max((tmp_path / "logs").glob("replenishment_pilot_apply_*.log")).read_text(encoding="utf-8")
    assert "nothing to write" in log
    assert tx_cli.main(base + ["--plan"]) == 0           # the pilot transaction check still passes
    with _connect(fresh_db) as conn:
        assert _sql(conn, "14_verify_replenishment.sql") == SCRIPT_14
