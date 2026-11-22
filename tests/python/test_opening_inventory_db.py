"""Database tests for the opening inventory loader (opt-in).

They prove against real PostgreSQL that the V007 mechanisms are used correctly and that
repeat runs cannot duplicate opening stock.

Safety
  * Runs only when NORDICCART_DB_TESTS=1 is set; otherwise every test is skipped.
  * Creates its own databases (nordiccart_inventory_test_*) on the server named by the
    standard PG variables, applies V001 to V007 and the master data pilot to them, and
    drops them at the end. It never connects to nordiccart_oltp.
  * The role must be allowed to create databases.

Run from the repository folder (PowerShell):
    $env:NORDICCART_DB_TESTS = "1"
    python -m pytest tests/python/test_opening_inventory_db.py -v
"""

from __future__ import annotations

import dataclasses
import os
import threading
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from nordiccart.inventory import cli  # noqa: E402
from nordiccart.inventory.config import load_inventory_settings  # noqa: E402
from nordiccart.inventory.generate import generate_opening_inventory  # noqa: E402
from nordiccart.inventory.load import (  # noqa: E402
    InventoryConflictError, VerificationError, apply_inventory, check_inventory_database, plan_inventory,
)
from nordiccart.master_data.generate import generate_master_data  # noqa: E402
from nordiccart.master_data.load import PreconditionError, apply_load  # noqa: E402
from nordiccart.settings import load_env_file  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("NORDICCART_DB_TESTS") != "1",
                                reason="set NORDICCART_DB_TESTS=1 to run the database tests")

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "nordiccart_inventory_test"
TEMPLATE_EMPTY = f"{PREFIX}_template_v007"        # migrations only
TEMPLATE_PILOT = f"{PREFIX}_template_pilot"       # migrations + master data pilot


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
def templates(pilot_dataset):
    load_env_file()
    with _admin() as admin:
        for name in (TEMPLATE_EMPTY, TEMPLATE_PILOT):
            _drop(admin, name)
        admin.execute(f'CREATE DATABASE "{TEMPLATE_EMPTY}" WITH ENCODING \'UTF8\' TEMPLATE template0')
        admin.execute(f'ALTER DATABASE "{TEMPLATE_EMPTY}" SET timezone TO \'UTC\'')
    with _connect(TEMPLATE_EMPTY) as conn:
        for path in sorted((REPO_ROOT / "database" / "migrations").glob("V00[1-7]__*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
    with _admin() as admin:
        admin.execute(f'CREATE DATABASE "{TEMPLATE_PILOT}" TEMPLATE "{TEMPLATE_EMPTY}"')
    with _connect(TEMPLATE_PILOT) as conn:
        apply_load(conn, pilot_dataset)
    yield
    with _admin() as admin:
        for (name,) in admin.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (PREFIX + "%",)).fetchall():
            _drop(admin, name)


@pytest.fixture
def fresh_db(templates, request):
    """A new database with V001-V007 and the master data pilot, no inventory."""
    name = f"{PREFIX}_{request.node.name[:28].lower()}".replace("[", "_").replace("]", "").replace("-", "_")
    with _admin() as admin:
        _drop(admin, name)
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_PILOT}"')
    yield name
    with _admin() as admin:
        _drop(admin, name)


@pytest.fixture(scope="module")
def inventory(pilot_settings, pilot_dataset):
    return generate_opening_inventory(pilot_settings, load_inventory_settings(pilot_settings), pilot_dataset)


def _counts(conn) -> dict:
    return {
        "balances": conn.execute("SELECT count(*) FROM inventory_balances").fetchone()[0],
        "openings": conn.execute("SELECT count(*) FROM inventory_movements WHERE movement_type = 'opening_balance'").fetchone()[0],
        "movements": conn.execute("SELECT count(*) FROM inventory_movements").fetchone()[0],
        "units": conn.execute("SELECT coalesce(sum(quantity_on_hand), 0) FROM inventory_balances").fetchone()[0],
    }


def _ids(conn, location_code: str, sku: str) -> tuple[int, int]:
    return conn.execute("SELECT l.stock_location_id, p.product_id FROM stock_locations l, products p "
                        "WHERE l.location_code = %s AND p.sku = %s", (location_code, sku)).fetchone()


def _stocked(inventory, location_code="SE-WH-01", sku="SPH-000001"):
    return next(x for x in inventory.listings if x.key == (location_code, sku))


# --------------------------------------------------------------------------- loading through V007
def test_plan_is_read_only(fresh_db, pilot_dataset, inventory):
    with _connect(fresh_db) as conn:
        check_inventory_database(conn, pilot_dataset)
        plan = plan_inventory(conn, inventory)
        assert plan.counts() == (320, 288) and not plan.conflicts
        assert _counts(conn) == {"balances": 0, "openings": 0, "movements": 0, "units": 0}


def test_apply_loads_every_listing_and_a_rerun_inserts_nothing(fresh_db, inventory):
    with _connect(fresh_db) as conn:
        result = apply_inventory(conn, inventory)
        assert (result.balances_inserted, result.movements_inserted, result.units_inserted) == (320, 288, 1460)
        assert result.change_log_rows == 320 + 288 * 2          # balance inserts, movements, balance updates
        assert _counts(conn) == {"balances": 320, "openings": 288, "movements": 288, "units": 1460}
        rows = {(r[0], r[1]): r[2:] for r in conn.execute(
            "SELECT l.location_code, p.sku, b.quantity_on_hand, b.quantity_reserved, b.reorder_point "
            "FROM inventory_balances b JOIN stock_locations l USING (stock_location_id) JOIN products p USING (product_id)")}
        for x in inventory.listings:
            assert rows[x.key] == (x.opening_quantity, 0, x.reorder_point)

        again = apply_inventory(conn, inventory)
        assert (again.balances_inserted, again.movements_inserted, again.change_log_rows) == (0, 0, 0)
        assert _counts(conn)["openings"] == 288


def test_movements_are_written_in_the_v007_lock_order(fresh_db, inventory):
    # V007 lock order: balance rows by (stock_location_id, product_id). Inserting the opening
    # movements in that order means the loader never waits on locks in a conflicting order.
    with _connect(fresh_db) as conn:
        apply_inventory(conn, inventory)
        order = conn.execute("SELECT stock_location_id, product_id FROM inventory_movements ORDER BY movement_id").fetchall()
        assert len(order) == 288 and order == sorted(order)


def test_ledger_identity_holds_after_loading(fresh_db, inventory):
    with _connect(fresh_db) as conn:
        apply_inventory(conn, inventory)
        bad = conn.execute("""
            SELECT count(*) FROM inventory_balances b
             WHERE b.quantity_on_hand <> coalesce((SELECT sum(m.quantity_change) FROM inventory_movements m
                    WHERE m.stock_location_id = b.stock_location_id AND m.product_id = b.product_id), 0)""").fetchone()[0]
        assert bad == 0


def test_sql_verification_12_passes(fresh_db, inventory):
    sql = (REPO_ROOT / "sql" / "verification" / "12_verify_opening_inventory.sql").read_text(encoding="utf-8")
    with _connect(fresh_db) as conn:
        apply_inventory(conn, inventory)
        rows = conn.execute(sql).fetchall()
    statuses = [r[5] for r in rows]
    assert len(rows) == 24 and statuses.count("PASS") == 17 and statuses.count("INFO") == 7 and rows[0][6] == 0


# --------------------------------------------------------------------------- duplicate prevention
def test_database_refuses_a_second_opening_balance(fresh_db, inventory):
    with _connect(fresh_db) as conn:
        apply_inventory(conn, inventory)
        loc_id, prod_id = _ids(conn, "SE-WH-01", "SPH-000001")
        with pytest.raises(psycopg.errors.UniqueViolation) as info:
            conn.execute("INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, "
                         "occurred_at) VALUES (%s, %s, 'opening_balance', 5, now())", (loc_id, prod_id))
        assert info.value.diag.constraint_name == "uq_inventory_movements_one_opening_balance"
        assert _counts(conn)["units"] == 1460


def test_database_refuses_direct_stock_writes(fresh_db, inventory):
    with _connect(fresh_db) as conn:
        apply_inventory(conn, inventory)
        with pytest.raises(psycopg.errors.CheckViolation) as info:
            conn.execute("UPDATE inventory_balances SET quantity_on_hand = quantity_on_hand + 10")
        assert info.value.diag.constraint_name == "inventory_balances_quantities_by_ledger"


def test_two_loaders_at_once_do_not_duplicate(fresh_db, inventory):
    results, errors = [], []
    barrier = threading.Barrier(2)

    def run():
        try:
            with _connect(fresh_db) as conn:
                barrier.wait(timeout=30)
                r = apply_inventory(conn, inventory)
                results.append(r.balances_inserted + r.movements_inserted)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors and sorted(results) == [0, 608]
    with _connect(fresh_db) as conn:
        assert _counts(conn) == {"balances": 320, "openings": 288, "movements": 288, "units": 1460}


# --------------------------------------------------------------------------- conflicts write nothing
def test_a_different_opening_quantity_is_a_conflict(fresh_db, inventory):
    x = _stocked(inventory)
    with _connect(fresh_db) as conn:
        loc_id, prod_id = _ids(conn, *x.key)
        conn.execute("INSERT INTO inventory_balances (stock_location_id, product_id) VALUES (%s, %s)", (loc_id, prod_id))
        conn.execute("INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, "
                     "occurred_at, external_reference) VALUES (%s, %s, 'opening_balance', %s, %s, %s)",
                     (loc_id, prod_id, x.opening_quantity + 1, x.occurred_at, x.external_reference))
        with pytest.raises(InventoryConflictError) as info:
            apply_inventory(conn, inventory)
        assert any("opening balance differs" in c for c in info.value.plan.conflicts)
        assert _counts(conn) == {"balances": 1, "openings": 1, "movements": 1, "units": x.opening_quantity + 1}


def test_opening_stock_is_not_added_after_activity_started(fresh_db, inventory):
    x = _stocked(inventory)
    with _connect(fresh_db) as conn:
        loc_id, prod_id = _ids(conn, *x.key)
        conn.execute("INSERT INTO inventory_balances (stock_location_id, product_id) VALUES (%s, %s)", (loc_id, prod_id))
        conn.execute("INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, "
                     "occurred_at, external_reference) VALUES (%s, %s, 'purchase_receipt', 10, now(), 'DN-TEST-1')",
                     (loc_id, prod_id))
        with pytest.raises(InventoryConflictError) as info:
            apply_inventory(conn, inventory)
        assert any("activity is already recorded" in c for c in info.value.plan.conflicts)
        assert _counts(conn)["balances"] == 1


def test_opening_stock_for_a_product_not_on_sale_is_a_conflict(fresh_db, inventory):
    with _connect(fresh_db) as conn:
        loc_id, prod_id = _ids(conn, "SE-WH-01", "CAM-000007")
        conn.execute("INSERT INTO inventory_balances (stock_location_id, product_id) VALUES (%s, %s)", (loc_id, prod_id))
        conn.execute("INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, "
                     "occurred_at) VALUES (%s, %s, 'opening_balance', 3, now())", (loc_id, prod_id))
        with pytest.raises(InventoryConflictError) as info:
            apply_inventory(conn, inventory)
        assert any("not on sale on the opening day" in c for c in info.value.plan.conflicts)


def test_later_activity_is_drift_not_a_conflict(fresh_db, inventory):
    with _connect(fresh_db) as conn:
        apply_inventory(conn, inventory)
        loc_id, prod_id = _ids(conn, "SE-WH-01", "SPH-000001")
        # what the business simulation will do later: receive stock, retune a reorder point
        conn.execute("INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, "
                     "occurred_at, external_reference) VALUES (%s, %s, 'purchase_receipt', 10, now(), 'DN-TEST-2')",
                     (loc_id, prod_id))
        conn.execute("UPDATE inventory_balances SET reorder_point = reorder_point + 1 "
                     "WHERE stock_location_id = %s AND product_id = %s", (loc_id, prod_id))
        plan = plan_inventory(conn, inventory)
        assert not plan.conflicts and plan.total_to_insert == 0 and len(plan.drift) == 1


# --------------------------------------------------------------------------- failures roll back
def test_a_failure_half_way_rolls_everything_back(fresh_db, inventory):
    last = inventory.movements()[-1]
    with _connect(fresh_db) as conn:
        loc_id, prod_id = _ids(conn, *last.key)
        conn.execute(f"""
            CREATE FUNCTION test_fail() RETURNS trigger LANGUAGE plpgsql AS $f$
            BEGIN
                IF NEW.stock_location_id = {loc_id} AND NEW.product_id = {prod_id} THEN
                    RAISE EXCEPTION 'simulated failure' USING ERRCODE = 'P0001';
                END IF;
                RETURN NEW;
            END $f$""")
        conn.execute("CREATE TRIGGER test_fail BEFORE INSERT ON inventory_movements FOR EACH ROW EXECUTE FUNCTION test_fail()")
        with pytest.raises(psycopg.errors.RaiseException):
            apply_inventory(conn, inventory)
        assert _counts(conn) == {"balances": 0, "openings": 0, "movements": 0, "units": 0}


def test_a_row_changed_by_the_database_is_caught_before_commit(fresh_db, inventory):
    with _connect(fresh_db) as conn:
        conn.execute("""
            CREATE FUNCTION test_tamper() RETURNS trigger LANGUAGE plpgsql AS $f$
            BEGIN
                IF NEW.quantity_change = 17 THEN NEW.quantity_change := 18; END IF;
                RETURN NEW;
            END $f$""")
        conn.execute("CREATE TRIGGER test_tamper BEFORE INSERT ON inventory_movements FOR EACH ROW EXECUTE FUNCTION test_tamper()")
        with pytest.raises(VerificationError):
            apply_inventory(conn, inventory)
        assert _counts(conn)["balances"] == 0


# --------------------------------------------------------------------------- preconditions
def test_database_without_master_data_is_refused(templates, pilot_dataset):
    with _connect(TEMPLATE_EMPTY) as conn:
        with pytest.raises(PreconditionError, match="master data"):
            check_inventory_database(conn, pilot_dataset)


def test_changed_master_data_is_refused(fresh_db, pilot_dataset):
    with _connect(fresh_db) as conn:
        conn.execute("UPDATE products SET launched_on = '2024-12-01' WHERE sku = 'SPH-000001'")
        with pytest.raises(PreconditionError, match="differing rows: 1"):
            check_inventory_database(conn, pilot_dataset)


# --------------------------------------------------------------------------- scale and command line
def test_scaling_up_only_adds_listings(fresh_db, pilot_settings, inventory):
    bigger = dataclasses.replace(pilot_settings, profile="scaled", products=30, customers=150)
    master = generate_master_data(bigger)
    scaled = generate_opening_inventory(bigger, load_inventory_settings(bigger), master)
    with _connect(fresh_db) as conn:
        apply_inventory(conn, inventory)
        ids_before = conn.execute("SELECT inventory_balance_id FROM inventory_balances ORDER BY 1").fetchall()
        apply_load(conn, master)
        result = apply_inventory(conn, scaled)
        new_stocked = len(scaled.movements()) - 288
        assert (result.balances_inserted, result.movements_inserted) == (160, new_stocked)
        ids_after = conn.execute("SELECT inventory_balance_id FROM inventory_balances ORDER BY 1").fetchall()
        assert ids_after[:320] == ids_before


def test_command_line_plan_apply_rerun(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setenv("PGDATABASE", fresh_db)
    text = (REPO_ROOT / "config" / "generator.toml").read_text(encoding="utf-8")
    text = text.replace('log_dir = "logs"', f'log_dir = "{(tmp_path / "logs").as_posix()}"')
    config = tmp_path / "generator.toml"
    config.write_text(text, encoding="utf-8")
    args = ["--config", str(config), "--env-file", str(tmp_path / "none.env"), "--no-csv"]
    assert cli.main(args + ["--plan"]) == 0
    assert cli.main(args + ["--apply", "--yes"]) == 0
    assert cli.main(args + ["--apply", "--yes"]) == 0
    text = "".join(p.read_text(encoding="utf-8") for p in sorted((tmp_path / "logs").glob("opening_inventory_pilot_apply_*.log")))
    assert "committed: 320 listings and 288 opening balances (1460 units) inserted" in text
    assert "nothing to insert" in text
