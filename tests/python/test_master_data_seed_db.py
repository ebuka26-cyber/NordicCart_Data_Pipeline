"""Database tests for the master data seeder (opt-in).

They prove, against real PostgreSQL, what the unit tests cannot: rows land as
generated, a rerun inserts nothing, conflicts and failures write nothing, two
seeders at once do not duplicate rows, and scaling up only adds rows.

Safety
  * Runs only when NORDICCART_DB_TESTS=1 is set; otherwise every test is skipped.
  * Creates its own databases (nordiccart_seed_test_*) on the server named by the
    standard PG variables, applies database/migrations V001 to V007 to them and
    drops them at the end. It never connects to nordiccart_oltp.
  * The role must be allowed to create databases.

Run from the repository folder (PowerShell):
    $env:NORDICCART_DB_TESTS = "1"
    python -m pytest tests/python/test_master_data_seed_db.py -v
"""

from __future__ import annotations

import dataclasses
import os
import threading
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from nordiccart.master_data import cli  # noqa: E402
from nordiccart.master_data.generate import generate_master_data  # noqa: E402
from nordiccart.master_data.load import (  # noqa: E402
    ConflictError, PreconditionError, VerificationError, apply_load, check_database, plan_load,
)
from nordiccart.settings import load_env_file  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("NORDICCART_DB_TESTS") != "1",
                                reason="set NORDICCART_DB_TESTS=1 to run the database tests")

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "nordiccart_seed_test"
TEMPLATE_V007 = f"{PREFIX}_template_v007"
TEMPLATE_V006 = f"{PREFIX}_template_v006"
MASTER_TABLES = ("currencies", "countries", "product_categories", "products", "product_prices",
                 "stock_locations", "stores", "warehouses", "customers")


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


def _build_template(admin, name: str, last_version: int) -> None:
    _drop(admin, name)
    admin.execute(f'CREATE DATABASE "{name}" WITH ENCODING \'UTF8\' TEMPLATE template0')
    admin.execute(f'ALTER DATABASE "{name}" SET timezone TO \'UTC\'')
    with _connect(name) as conn:
        for path in sorted((REPO_ROOT / "database" / "migrations").glob("V*.sql")):
            if int(path.name[1:4]) <= last_version:
                conn.execute(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def templates():
    load_env_file()
    with _admin() as admin:
        _build_template(admin, TEMPLATE_V007, 7)
        _build_template(admin, TEMPLATE_V006, 6)
    yield
    with _admin() as admin:
        rows = admin.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (PREFIX + "%",)).fetchall()
        for (name,) in rows:
            _drop(admin, name)


@pytest.fixture
def fresh_db(templates, request):
    """A new database copied from the migrated V001-V007 template."""
    name = f"{PREFIX}_{request.node.name[:30].lower()}".replace("[", "_").replace("]", "").replace("-", "_")
    with _admin() as admin:
        _drop(admin, name)
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_V007}"')
    yield name
    with _admin() as admin:
        _drop(admin, name)


def _counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in MASTER_TABLES + ("change_log",)}


def _scaled(settings, products: int, customers: int):
    return dataclasses.replace(settings, profile="scaled", products=products, customers=customers)


# --------------------------------------------------------------------------- tests
def test_plan_is_read_only(fresh_db, pilot_dataset):
    with _connect(fresh_db) as conn:
        plan = plan_load(conn, pilot_dataset)
        assert plan.total_to_insert == 250 and plan.conflict_count == 0
        assert set(_counts(conn).values()) == {0}


def test_apply_inserts_the_pilot_and_a_rerun_inserts_nothing(fresh_db, pilot_dataset):
    with _connect(fresh_db) as conn:
        first = apply_load(conn, pilot_dataset)
        assert sum(first.inserted.values()) == 250 and first.verified_rows == 250
        assert first.change_log_rows == 250                       # every insert captured for the pipeline
        counts = _counts(conn)
        assert counts == {"currencies": 4, "countries": 4, "product_categories": 10, "products": 20,
                          "product_prices": 80, "stock_locations": 16, "stores": 12, "warehouses": 4,
                          "customers": 100, "change_log": 250}
        ids_before = conn.execute("SELECT customer_number, customer_id FROM customers ORDER BY 1").fetchall()

        second = apply_load(conn, pilot_dataset)
        assert sum(second.inserted.values()) == 0 and second.change_log_rows == 0
        assert _counts(conn) == counts
        assert conn.execute("SELECT customer_number, customer_id FROM customers ORDER BY 1").fetchall() == ids_before


def test_sql_verification_11_passes_after_the_pilot(fresh_db, pilot_dataset):
    sql = (REPO_ROOT / "sql" / "verification" / "11_verify_master_data.sql").read_text(encoding="utf-8")
    with _connect(fresh_db) as conn:
        apply_load(conn, pilot_dataset)
        rows = conn.execute(sql).fetchall()
    statuses = [r[5] for r in rows]
    assert len(rows) == 44 and statuses.count("FAIL") == 0 and rows[0][6] == 0
    assert statuses.count("PASS") == 33 and statuses.count("INFO") == 11   # S6 M3: INFO row 507


def test_scaling_up_only_adds_rows(fresh_db, pilot_settings, pilot_dataset):
    bigger = generate_master_data(_scaled(pilot_settings, products=30, customers=150))
    with _connect(fresh_db) as conn:
        apply_load(conn, pilot_dataset)
        ids_before = conn.execute("SELECT customer_number, customer_id FROM customers ORDER BY 1").fetchall()
        plan = plan_load(conn, bigger)
        assert plan.insert_counts() == {"currencies": 0, "countries": 0, "product_categories": 0, "products": 10,
                                        "product_prices": 40, "stock_locations": 0, "stores": 0, "warehouses": 0,
                                        "customers": 50}
        apply_load(conn, bigger)
        assert conn.execute("SELECT count(*) FROM customers").fetchone()[0] == 150
        ids_after = conn.execute("SELECT customer_number, customer_id FROM customers ORDER BY 1").fetchall()
        assert ids_after[:100] == ids_before
        # running the smaller pilot again afterwards changes nothing and reports the extra rows
        plan_small = plan_load(conn, pilot_dataset)
        assert plan_small.total_to_insert == 0 and plan_small.tables["customers"].extra_in_db == 50


def test_conflict_stops_the_run_and_writes_nothing(fresh_db, pilot_settings, pilot_dataset):
    bigger = generate_master_data(_scaled(pilot_settings, products=20, customers=120))
    with _connect(fresh_db) as conn:
        apply_load(conn, pilot_dataset)
        conn.execute("UPDATE customers SET first_name = 'Changed' WHERE customer_number = 'C00000007'")
        before = _counts(conn)
        with pytest.raises(ConflictError) as info:
            apply_load(conn, bigger)
        assert "C00000007" in info.value.plan.tables["customers"].conflicts[0]
        assert _counts(conn) == before                        # the 20 new customers were not inserted


def test_lifecycle_changes_are_drift_not_conflicts(fresh_db, pilot_dataset):
    with _connect(fresh_db) as conn:
        apply_load(conn, pilot_dataset)
        # what the business simulation will do later: a price is closed, a customer moves away
        conn.execute("UPDATE product_prices SET valid_to = valid_from + interval '400 days' "
                     "WHERE product_id = (SELECT product_id FROM products WHERE sku = 'SPH-000001') AND country_code = 'SE'")
        conn.execute("UPDATE customers SET city = NULL, address_line1 = NULL, postal_code = NULL, is_active = false "
                     "WHERE customer_number = 'C00000010'")
        plan = plan_load(conn, pilot_dataset)
        assert plan.conflict_count == 0 and plan.total_to_insert == 0
        assert len(plan.tables["product_prices"].drift) == 1 and len(plan.tables["customers"].drift) == 1


def test_email_taken_by_another_customer_is_a_conflict(fresh_db, pilot_settings, pilot_dataset):
    bigger = generate_master_data(_scaled(pilot_settings, products=20, customers=101))
    with _connect(fresh_db) as conn:
        apply_load(conn, pilot_dataset)
        new_email = bigger.customers[100].email
        conn.execute("UPDATE customers SET email = %s WHERE customer_number = 'C00000001'", (new_email.upper(),))
        plan = plan_load(conn, bigger)
        assert any("already belongs to C00000001" in c for c in plan.tables["customers"].conflicts)


def test_a_failure_half_way_rolls_everything_back(fresh_db, pilot_dataset):
    with _connect(fresh_db) as conn:
        # Customers are inserted last. Make one customer row fail after all other tables were written.
        victim = pilot_dataset.customers[42].customer_number
        conn.execute(f"ALTER TABLE customers ADD CONSTRAINT test_reject_one CHECK (customer_number <> '{victim}')")
        with pytest.raises(psycopg.errors.CheckViolation):
            apply_load(conn, pilot_dataset)
        assert set(_counts(conn).values()) == {0}             # currencies ... warehouses rolled back too


def test_rows_the_database_drops_or_changes_are_caught_before_commit(fresh_db, pilot_dataset):
    with _connect(fresh_db) as conn:
        # A trigger in this test database silently skips one customer and alters another.
        conn.execute("""
            CREATE FUNCTION test_tamper() RETURNS trigger LANGUAGE plpgsql AS $f$
            BEGIN
                IF NEW.customer_number = 'C00000042' THEN RETURN NULL; END IF;
                IF NEW.customer_number = 'C00000043' THEN NEW.first_name := upper(NEW.first_name); END IF;
                RETURN NEW;
            END $f$""")
        conn.execute("CREATE TRIGGER test_tamper BEFORE INSERT ON customers FOR EACH ROW EXECUTE FUNCTION test_tamper()")
        with pytest.raises(VerificationError, match="2 rows differ"):
            apply_load(conn, pilot_dataset)
        assert set(_counts(conn).values()) == {0}             # rolled back


def test_two_seeders_at_once_do_not_duplicate(fresh_db, pilot_dataset):
    results, errors = [], []
    barrier = threading.Barrier(2)

    def run():
        try:
            with _connect(fresh_db) as conn:
                barrier.wait(timeout=30)
                results.append(sum(apply_load(conn, pilot_dataset).inserted.values()))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors and sorted(results) == [0, 250]          # the advisory lock serialises them
    with _connect(fresh_db) as conn:
        assert _counts(conn)["customers"] == 100


def test_database_without_v007_is_refused(templates):
    with _connect(TEMPLATE_V006) as conn:
        with pytest.raises(PreconditionError, match="007"):
            check_database(conn)


def test_wrong_database_is_refused(templates):
    with _admin() as conn:
        with pytest.raises(PreconditionError, match="meta.schema_migrations"):
            check_database(conn)


def test_command_line_apply_then_rerun(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setenv("PGDATABASE", fresh_db)
    text = (REPO_ROOT / "config" / "generator.toml").read_text(encoding="utf-8")
    text = text.replace('log_dir = "logs"', f'log_dir = "{(tmp_path / "logs").as_posix()}"')
    config = tmp_path / "generator.toml"
    config.write_text(text, encoding="utf-8")
    args = ["--config", str(config), "--env-file", str(tmp_path / "none.env"), "--no-csv"]
    assert cli.main(args + ["--plan"]) == 0
    assert cli.main(args + ["--apply", "--yes"]) == 0
    assert cli.main(args + ["--apply", "--yes"]) == 0
    logs = sorted((tmp_path / "logs").glob("master_data_pilot_apply_*.log"))
    assert "committed: 250 rows inserted" in logs[0].read_text(encoding="utf-8")
    assert "nothing to insert" in logs[-1].read_text(encoding="utf-8")
