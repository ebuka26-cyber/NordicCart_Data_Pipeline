"""Database tests for V009 activity control and purchase orders (Phase 3, step 5, stage S4) (opt-in).

They prove against real PostgreSQL that:
  * V009 applies to the real database's state (pilots loaded, V008) without changing a row, refuses a
    second run, a database without V008 and a non-superuser session, and rolls back as a whole when
    its self-check fails; its objects, owners and privileges are as designed;
  * the pilot documents (exception E3: 4 purchase orders; decision C18: 69 R0 revisions) are written by
    the replenishment loader as nordiccart_loader WITHOUT any movement, once, and a fresh load on
    V009 writes the same documents inside its units of work;
  * R0 stays verifiable after a later monthly review (C18): the loader and script 14 read R0 from the
    history; without V009 the same review breaks both;
  * interrupted runs (the backfill transaction, the replenishment units, the run protocol) leave no
    partial state and resume; duplicates are refused; concurrent receipts never exceed the ordered
    quantity; concurrent runs for one window are refused; unauthorized changes are refused (42501) and
    the guards bind the owner;
  * R009 returns the catalog exactly to its V008 state and V009 re-applies;
  * the SQL suites (foundation, V006, V007, V008, V009), scripts 01 to 15, 17 and 18 (as
    nordiccart_analyst), both pilot command lines (as nordiccart_loader) and the 14 concurrency
    scenarios (as nordiccart_loader) pass on V009.

Safety
  * Runs only when NORDICCART_DB_TESTS=1 is set; otherwise every test is skipped.
  * Creates its own databases (nordiccart_v009_test_*) and drops them; never connects to nordiccart_oltp.
    V008 and V009 touch CLUSTER roles: run this only against a disposable server (the S4 sandbox),
    never the real one. The connecting role must be a superuser; the NordicCart roles log in without
    a password there (trust).

Run from the repository folder (PowerShell), with all PG variables pointing to the sandbox:
    $env:NORDICCART_DB_TESTS = "1"
    python -m pytest tests/python/test_v009_db.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from nordiccart.inventory.config import load_inventory_settings  # noqa: E402
from nordiccart.inventory.generate import generate_opening_inventory  # noqa: E402
from nordiccart.inventory.load import apply_inventory  # noqa: E402
from nordiccart.master_data.load import apply_load  # noqa: E402
from nordiccart.replenishment import cli as repl_cli  # noqa: E402
from nordiccart.replenishment import load as repl_load  # noqa: E402
from nordiccart.replenishment.config import load_replenishment_settings  # noqa: E402
from nordiccart.replenishment.generate import generate_replenishment  # noqa: E402
from nordiccart.replenishment.load import (  # noqa: E402
    LoaderBusyError, ReplenishmentConflictError, apply_replenishment, plan_replenishment,
)
from nordiccart.settings import load_env_file  # noqa: E402
from nordiccart.transactions import cli as tx_cli  # noqa: E402
from nordiccart.transactions.config import load_transaction_settings  # noqa: E402
from nordiccart.transactions.generate import generate_transactions  # noqa: E402
from nordiccart.transactions.load import apply_transactions, plan_transactions  # noqa: E402
from nordiccart.units import stock as stock_units  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("NORDICCART_DB_TESTS") != "1",
                                reason="set NORDICCART_DB_TESTS=1 to run the database tests")

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "database" / "migrations"
V008 = MIGRATIONS / "V008__security_roles_and_privileges.sql"
V009 = MIGRATIONS / "V009__activity_control_and_purchase_orders.sql"
R009 = REPO_ROOT / "database" / "rollback" / "R009__undo_activity_control_and_purchase_orders.sql"
REAPPLY = REPO_ROOT / "database" / "security" / "V008_reapply_database_privileges.sql"
VERIFY = REPO_ROOT / "sql" / "verification"
PREFIX = "nordiccart_v009_test"
TEMPLATE_V008 = f"{PREFIX}_template_v008"      # V001-V008 + master data, opening inventory, both pilots: the real state
TEMPLATE_V009 = f"{PREFIX}_template_v009"      # + V009, before the document backfill
TEMPLATE_EMPTY = f"{PREFIX}_template_empty"    # V001-V009, no rows
LOADER, ANALYST, OWNER = "nordiccart_loader", "nordiccart_analyst", "nordiccart_owner"
GRANTEES = (OWNER, "nordiccart_writer", LOADER, "nordiccart_scheduler", "nordiccart_reader", ANALYST,
            "nordiccart_extractor", "public")
WRITERS = ("nordiccart_writer", LOADER, "nordiccart_scheduler")
READERS = ("nordiccart_reader", ANALYST)
V009_TABLES = ("public.purchase_orders", "public.purchase_order_lines", "public.reorder_point_revisions",
               "meta.activity_runs", "meta.activity_checkpoints")
RUN_STATUS_COLUMNS = {"status", "completed_units", "unit_counts", "end_state_hash", "failure_reason", "finished_at",
                      "recovery_point", "recovery_point_sha256"}
WRITER_FUNCTIONS = {"fn_purchase_order_line_received(character varying,bigint,bigint)", "fn_purchase_order_state(bigint)"}
H = {c: c * 64 for c in "abcdef0123"}             # 64-character hex values for fingerprints and hashes


# --------------------------------------------------------------------------- databases
def _admin():
    return psycopg.connect(dbname="postgres", autocommit=True)


def _connect(dbname: str, user: str | None = None, autocommit: bool = True):
    assert dbname.startswith(PREFIX), "tests only touch their own databases"
    conn = psycopg.connect(dbname=dbname, autocommit=autocommit, **({"user": user} if user else {}))
    conn.execute("SET search_path TO public")
    if not autocommit:
        conn.commit()
    return conn


def _drop(admin, name: str) -> None:
    admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                  (name,))
    admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _create(admin, name: str, template: str = "template0") -> None:
    _drop(admin, name)
    admin.execute(f'CREATE DATABASE "{name}" WITH ENCODING \'UTF8\' TEMPLATE "{template}"')
    admin.execute(f'ALTER DATABASE "{name}" SET timezone TO \'UTC\'')


def _migrate(conn, last: int) -> None:
    for path in sorted(MIGRATIONS.glob("V*.sql")):
        if int(path.name[1:4]) <= last:
            conn.execute(path.read_text(encoding="utf-8"))


def _copy(template: str, name: str) -> str:
    """A copy of a template; V008's database-level privileges are re-applied (not copied by PostgreSQL)."""
    with _admin() as admin:
        _create(admin, name, template)
    with _connect(name) as conn:
        conn.execute(REAPPLY.read_text(encoding="utf-8"))
    return name


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
        assert admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0], \
            "the V009 tests need a superuser on a disposable server"
        for name in (TEMPLATE_V009, TEMPLATE_V008, TEMPLATE_EMPTY):
            _drop(admin, name)
        _create(admin, TEMPLATE_V008)
        _create(admin, TEMPLATE_EMPTY)
    with _connect(TEMPLATE_V008) as conn:          # the pilots were loaded before V008, as postgres
        _migrate(conn, 7)
        apply_load(conn, pilot_dataset)
        apply_inventory(conn, inventory)
        assert apply_transactions(conn, pilot).events_run == 100
        assert apply_replenishment(conn, plan).events_run == 17
        conn.execute(V008.read_text(encoding="utf-8"))
    with _admin() as admin:
        _create(admin, TEMPLATE_V009, TEMPLATE_V008)
    with _connect(TEMPLATE_V009) as conn:
        conn.execute(V009.read_text(encoding="utf-8"))
    with _connect(TEMPLATE_EMPTY) as conn:
        _migrate(conn, 9)
    yield
    with _admin() as admin:
        for name in (TEMPLATE_V009, TEMPLATE_V008, TEMPLATE_EMPTY):
            _drop(admin, name)


def _name(request, suffix: str = "") -> str:
    base = f"{PREFIX}_{request.node.name}".lower().replace("[", "_").replace("]", "").replace("-", "_").replace(".", "_")
    return base[:60 - len(suffix)] + suffix


def _fixture_db(template: str, suffix: str):
    @pytest.fixture
    def fixture(templates, request):
        name = _copy(template, _name(request, suffix))
        yield name
        with _admin() as admin:
            _drop(admin, name)
    return fixture


v008_db = _fixture_db(TEMPLATE_V008, "_v8")         # the real database now (after S3)
v009_db = _fixture_db(TEMPLATE_V009, "_v9")         # right after V009, before the backfill
empty_v009_db = _fixture_db(TEMPLATE_EMPTY, "_e9")


@pytest.fixture
def backfilled_db(v009_db, plan):
    """After V009 and the pilot document backfill, as the real database will be after S4."""
    with _connect(v009_db, LOADER) as conn:
        assert apply_replenishment(conn, plan).documents_written == 8
    return v009_db


# --------------------------------------------------------------------------- reading
CONTENT = {
    "orders": "SELECT o.order_number, o.status, o.order_total, o.placed_at, o.fulfilled_at, o.cancelled_at FROM orders o",
    "order_items": "SELECT o.order_number, i.line_number, i.quantity, i.line_total FROM order_items i JOIN orders o USING (order_id)",
    "payment_attempts": "SELECT o.order_number, a.attempt_number, a.status, a.amount FROM payment_attempts a JOIN orders o USING (order_id)",
    "inventory_reservations": """SELECT o.order_number, i.line_number, r.quantity, r.status, r.closed_at FROM inventory_reservations r
                                   JOIN order_items i USING (order_item_id) JOIN orders o USING (order_id)""",
    "inventory_movements": """SELECT m.movement_type, l.location_code, p.sku, m.quantity_change, m.occurred_at, m.external_reference
                                FROM inventory_movements m JOIN stock_locations l USING (stock_location_id) JOIN products p USING (product_id)""",
    "inventory_balances": """SELECT l.location_code, p.sku, b.quantity_on_hand, b.quantity_reserved, b.reorder_point
                               FROM inventory_balances b JOIN stock_locations l USING (stock_location_id) JOIN products p USING (product_id)""",
    "master": """SELECT 'p', sku, product_name FROM products UNION ALL SELECT 'c', customer_number, email FROM customers
                 UNION ALL SELECT 'l', location_code, location_name FROM stock_locations""",
}
DOCUMENTS = {
    "purchase_orders": """SELECT o.purchase_order_number, l.location_code, o.ordered_at, o.status, o.received_at
                            FROM purchase_orders o JOIN stock_locations l USING (stock_location_id)""",
    "purchase_order_lines": """SELECT o.purchase_order_number, pl.line_number, p.sku, pl.order_reason, pl.quantity_ordered,
                                      pl.quantity_received, pl.quantity_outstanding, pl.status
                                 FROM purchase_order_lines pl JOIN purchase_orders o USING (purchase_order_id) JOIN products p USING (product_id)""",
    "reorder_point_revisions": """SELECT l.location_code, p.sku, r.revision_kind, r.effective_at, r.old_reorder_point,
                                         r.new_reorder_point, r.document_reference
                                    FROM reorder_point_revisions r JOIN stock_locations l USING (stock_location_id)
                                    JOIN products p USING (product_id)""",
}


def _digest(conn, queries: dict) -> dict:
    out = {}
    with conn.transaction():
        conn.execute("SET LOCAL TimeZone = 'UTC'")
        for name, sql in queries.items():
            out[name] = conn.execute(
                f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text COLLATE \"C\"), '')) FROM ({sql}) t").fetchone()
    return out


def _change_log_rows(conn) -> int:
    return conn.execute("SELECT count(*) FROM change_log").fetchone()[0]


def _script(conn, path: Path) -> tuple[int, int, int, int]:
    """(rows, PASS, INFO, FAIL) over every result set, by the column named status."""
    c = Counter()
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
        while True:
            if cur.description is not None:
                names = [d.name for d in cur.description]
                if "status" in names:
                    i = names.index("status")
                    c.update(r[i] for r in cur.fetchall())
            if not cur.nextset():
                break
    return sum(c.values()), c["PASS"], c["INFO"], c["FAIL"]


def _failed_checks(conn, path: Path) -> list[int]:
    return [r[0] for r in conn.execute(path.read_text(encoding="utf-8")).fetchall() if r[5] == "FAIL"]


def _suite(conn, path: Path) -> tuple[int, list]:
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
        rows = None
        while True:
            if cur.description is not None:
                rows = cur.fetchall()
            if not cur.nextset():
                break
    return len(rows), [r[:5] for r in rows if r[5] != "PASS"]


def _sqlstate(conn, sql: str, params=None) -> str:
    """'00000' or the SQLSTATE of the error; always rolled back."""
    try:
        with conn.transaction():
            conn.execute(sql, params)
            raise psycopg.Rollback()
    except psycopg.Error as exc:
        return exc.sqlstate or "?"
    return "00000"


CATALOG = """
SELECT 'class ' || n.nspname || '.' || c.relname || ' ' || c.relkind::text, pg_get_userbyid(c.relowner),
       (SELECT array_agg(x::text ORDER BY x::text)
          FROM unnest(coalesce(c.relacl, acldefault(CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END::"char", c.relowner))) x)::text
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname IN ('public', 'meta')
UNION ALL
SELECT 'column ' || c.relname || '.' || a.attname, format_type(a.atttypid, a.atttypmod),
       (SELECT array_agg(x::text ORDER BY x::text) FROM unnest(a.attacl) x)::text
  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('public', 'meta') AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
UNION ALL
SELECT 'constraint ' || c.conrelid::regclass::text || '.' || c.conname, c.contype::text, pg_get_constraintdef(c.oid)
  FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname IN ('public', 'meta')
UNION ALL
SELECT 'trigger ' || t.tgrelid::regclass::text || '.' || t.tgname, t.tgenabled::text, pg_get_triggerdef(t.oid)
  FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('public', 'meta') AND NOT t.tgisinternal
UNION ALL
SELECT 'index ' || i.indexrelid::regclass::text, NULL, pg_get_indexdef(i.indexrelid)
  FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('public', 'meta')
UNION ALL
SELECT 'function ' || p.oid::regprocedure::text, pg_get_userbyid(p.proowner),
       p.prosecdef::text || ' ' || coalesce(p.proconfig::text, '-') || ' '
       || (SELECT array_agg(x::text ORDER BY x::text) FROM unnest(coalesce(p.proacl, acldefault('f', p.proowner))) x)::text
       || ' ' || md5(p.prosrc)
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname IN ('public', 'meta')
UNION ALL
SELECT 'type ' || n.nspname || '.' || t.typname, pg_get_userbyid(t.typowner),
       (SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid = t.oid)
  FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname IN ('public', 'meta') AND t.typtype = 'e'
UNION ALL
SELECT 'schema ' || nspname, pg_get_userbyid(nspowner),
       (SELECT array_agg(x::text ORDER BY x::text) FROM unnest(coalesce(nspacl, acldefault('n', nspowner))) x)::text
  FROM pg_namespace WHERE nspname IN ('public', 'meta')
UNION ALL
SELECT 'database', pg_get_userbyid(datdba),
       (SELECT array_agg(x::text ORDER BY x::text) FROM unnest(coalesce(datacl, acldefault('d', datdba))) x)::text
  FROM pg_database WHERE datname = current_database()
UNION ALL
SELECT 'default acl', pg_get_userbyid(defaclrole), defaclobjtype::text || ' ' || defaclacl::text FROM pg_default_acl
UNION ALL
SELECT 'migrations', NULL, string_agg(version, ',' ORDER BY version) FROM meta.schema_migrations
ORDER BY 1, 2, 3"""


def _catalog(conn) -> list[tuple]:
    return conn.execute(CATALOG).fetchall()


def _ids(conn, location: str, sku: str) -> tuple[int, int]:
    return conn.execute("SELECT l.stock_location_id, p.product_id FROM stock_locations l, products p "
                        "WHERE l.location_code = %s AND p.sku = %s", (location, sku)).fetchone()


# --------------------------------------------------------------------------- the migration
def test_v009_keeps_every_row_and_records_itself(v008_db):
    with _connect(v008_db) as conn:
        before, changes = _digest(conn, CONTENT), _change_log_rows(conn)
        conn.execute(V009.read_text(encoding="utf-8"))
        assert _digest(conn, CONTENT) == before and _change_log_rows(conn) == changes   # not one row written
        assert conn.execute("SELECT applied_by FROM meta.schema_migrations WHERE version = '009'").fetchone()[0] == "postgres"
        assert conn.execute("SELECT count(*) FROM purchase_orders").fetchone()[0] == 0
        owners = {r[0] for r in conn.execute(
            "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname || '.' || c.relname = ANY(%s)", (list(V009_TABLES),)).fetchall()}
        assert owners == {OWNER}


def test_v009_refuses_a_second_run_a_missing_v008_and_a_non_superuser(v009_db, v008_db, templates):
    with _connect(v009_db) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="already applied"):
            conn.execute(V009.read_text(encoding="utf-8"))
    with _connect(v008_db, LOADER) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="superuser session"):
            conn.execute(V009.read_text(encoding="utf-8"))
    name = f"{PREFIX}_only_v007"
    with _admin() as admin:
        _create(admin, name)
    try:
        with _connect(name) as conn:
            _migrate(conn, 7)
            with pytest.raises(psycopg.errors.RaiseException, match="requires V008"):
                conn.execute(V009.read_text(encoding="utf-8"))
    finally:
        with _admin() as admin:
            _drop(admin, name)


def test_a_failing_self_check_rolls_back_the_whole_migration(v008_db):
    """A forbidden privilege planted before V009 (DELETE for the reader) stops V009; nothing of it stays."""
    with _connect(v008_db) as conn:
        conn.execute("GRANT DELETE ON public.orders TO nordiccart_reader")
        before = _catalog(conn)
        with pytest.raises(psycopg.errors.RaiseException, match="V009 self-check: forbidden table privileges"):
            conn.execute(V009.read_text(encoding="utf-8"))
        conn.execute("ROLLBACK")
        assert _catalog(conn) == before
        assert conn.execute("SELECT to_regclass('public.purchase_orders')").fetchone()[0] is None


def test_the_v009_privilege_matrix(v009_db):
    """8 grantees x the 5 new tables (every column, SELECT/INSERT/UPDATE, table DELETE/TRUNCATE/REFERENCES/TRIGGER) x V009 functions."""
    with _connect(v009_db) as conn:
        functions = [r[0] for r in conn.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname IN ('public', 'meta') AND p.proname IN ('fn_purchase_order_line_received', 'fn_purchase_order_state', "
            "'fn_purchase_order_refresh', 'fn_purchase_orders_guard', 'fn_purchase_order_lines_guard', "
            "'fn_purchase_order_lines_refresh_order', 'fn_purchase_receipt_apply', 'fn_check_purchase_order_complete', "
            "'fn_reorder_point_revisions_guard', 'fn_check_reorder_point_revision_applied', 'fn_activity_runs_guard', "
            "'fn_check_activity_run_verified', 'fn_activity_checkpoints_guard', 'fn_check_activity_checkpoint_verified')").fetchall()]
        assert len(functions) == 14
        expected, actual = {}, {}
        for role in GRANTEES:
            for t in V009_TABLES:
                cols = [r[0] for r in conn.execute("SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass "
                                                   "AND attnum > 0 AND NOT attisdropped", (t,)).fetchall()]
                for p in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                    expected[(role, t, p)] = role == OWNER
                    actual[(role, t, p)] = conn.execute("SELECT has_table_privilege(%s, %s, %s)", (role, t, p)).fetchone()[0]
                for col in cols:
                    for p in ("SELECT", "INSERT", "UPDATE"):
                        expected[(role, t, col, p)] = (
                            role == OWNER
                            or (role in WRITERS and p in ("SELECT", "INSERT"))
                            or (role in WRITERS and p == "UPDATE" and t == "meta.activity_runs" and col in RUN_STATUS_COLUMNS)
                            or (role in READERS and p == "SELECT"))
                        actual[(role, t, col, p)] = conn.execute("SELECT has_column_privilege(%s, %s, %s, %s)",
                                                                 (role, t, col, p)).fetchone()[0]
            for f in functions:
                bare = f.removeprefix("meta.")
                expected[(role, f)] = role == OWNER or (role in WRITERS and bare in WRITER_FUNCTIONS)
                actual[(role, f)] = conn.execute("SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
                                                 (role, f if f.startswith("meta.") else f"public.{f}")).fetchone()[0]
        differences = sorted(str(k) for k in expected if expected[k] != actual[k])
        assert not differences, differences[:10]
        assert len(expected) == 8 * (56 * 3 + 5 * 4 + 14) == 1616


# --------------------------------------------------------------------------- E3 and C18: the pilot documents
def test_the_backfill_writes_the_pilot_documents_once_and_no_movement(v009_db, plan, pilot):
    with _connect(v009_db, LOADER) as conn:
        before, changes = _digest(conn, CONTENT), _change_log_rows(conn)
        progress = plan_replenishment(conn, plan)
        assert progress.ok and not progress.remaining and progress.documents_to_write == 8
        assert sorted(progress.documents_missing) == sorted(
            [("purchase_order", p.number) for p in plan.purchase_orders]
            + [("reorder_point_revision", r.warehouse) for r in plan.revisions])
        result = apply_replenishment(conn, plan, confirmed=progress)
        assert (result.events_run, result.documents_written, result.change_log_rows) == (0, 8, 81)
        assert _digest(conn, CONTENT) == before                     # no movement, balance or order changed
        assert _change_log_rows(conn) == changes + 81               # 4 orders + 4 lines + 4 status updates + 69 revisions
        orders = conn.execute("SELECT o.purchase_order_number, l.location_code, o.ordered_at, o.status::text, o.received_at "
                              "FROM purchase_orders o JOIN stock_locations l USING (stock_location_id) ORDER BY 1").fetchall()
        assert orders == sorted((p.number, p.warehouse, p.ordered_at, "received", max(d.received_at for d in p.deliveries))
                                for p in plan.purchase_orders)
        lines = conn.execute("SELECT o.purchase_order_number, pl.line_number, p.sku, pl.order_reason::text, pl.quantity_ordered, "
                             "pl.quantity_received, pl.quantity_outstanding FROM purchase_order_lines pl "
                             "JOIN purchase_orders o USING (purchase_order_id) JOIN products p USING (product_id) ORDER BY 1, 2").fetchall()
        assert lines == sorted((p.number, x.line_number, x.sku, x.trigger, x.quantity, x.quantity, 0)
                               for p in plan.purchase_orders for x in p.lines)
        revisions = conn.execute("SELECT l.location_code, p.sku, r.revision_kind::text, r.effective_at, r.old_reorder_point, "
                                 "r.new_reorder_point, r.document_reference FROM reorder_point_revisions r "
                                 "JOIN stock_locations l USING (stock_location_id) JOIN products p USING (product_id) ORDER BY 1, 2").fetchall()
        assert revisions == sorted((r.warehouse, c.sku, "pilot_r0", r.at, c.old, c.new, r.reference)
                                   for r in plan.revisions for c in r.changes)
        assert len(revisions) == 69
        # rerun: nothing to write, nothing written
        again = plan_replenishment(conn, plan)
        assert again.ok and again.documents_to_write == 0
        assert apply_replenishment(conn, plan).documents_written == 0 and _change_log_rows(conn) == changes + 81
        assert plan_transactions(conn, pilot).ok                  # the transaction pilot still verifies


def test_a_fresh_load_on_v009_writes_the_same_documents_inside_its_units(empty_v009_db, backfilled_db, pilot_dataset,
                                                                          inventory, pilot, plan):
    with _connect(empty_v009_db, LOADER) as conn:
        apply_load(conn, pilot_dataset)
        apply_inventory(conn, inventory)
        assert apply_transactions(conn, pilot).events_run == 100
        result = apply_replenishment(conn, plan)
        assert (result.events_run, result.documents_written) == (17, 0)    # written inline, nothing left to backfill
        fresh = _digest(conn, CONTENT | DOCUMENTS)
    with _connect(backfilled_db) as conn:
        backfilled = _digest(conn, CONTENT | DOCUMENTS)
    assert fresh == backfilled           # same business data AND the same documents, whichever way they were written


def test_r0_stays_verifiable_after_a_later_monthly_review(backfilled_db, v008_db, plan, pilot):
    """C18: a monthly review after the pilot changes 18 warehouse reorder points; with V009 the loader and
    script 14 still verify R0 (from the history); on V008 the same change breaks both."""
    review = plan.revision("SE-WH-01")
    sql_update = ("UPDATE inventory_balances SET reorder_point = reorder_point + 1 "
                  "WHERE stock_location_id = %s AND product_id = %s")
    with _connect(backfilled_db, LOADER) as conn:
        with conn.transaction():
            for c in review.changes:
                loc, prod = _ids(conn, review.warehouse, c.sku)
                conn.execute(sql_update, (loc, prod))
                conn.execute("INSERT INTO reorder_point_revisions (stock_location_id, product_id, revision_kind, effective_at, "
                             "old_reorder_point, new_reorder_point, document_reference) "
                             "VALUES (%s, %s, 'monthly_review', '2024-12-31 06:00+00', %s, %s, 'RCAL-SE-WH-01-202412')",
                             (loc, prod, c.new, c.new + 1))
        progress = plan_replenishment(conn, plan)
        assert progress.ok, progress.conflicts + progress.blocking
        assert progress.later_revisions == 18 and progress.documents_to_write == 0
        assert plan_transactions(conn, pilot).ok
    with _connect(backfilled_db, ANALYST) as conn:
        assert _script(conn, VERIFY / "14_verify_replenishment.sql") == (44, 37, 7, 0)
        assert _script(conn, VERIFY / "18_verify_v009_activity_control.sql") == (44, 38, 6, 0)
    with _connect(v008_db, LOADER) as conn:            # the same review without the history
        for c in review.changes:
            conn.execute(sql_update, _ids(conn, review.warehouse, c.sku))
        assert not plan_replenishment(conn, plan).ok
    with _connect(v008_db, ANALYST) as conn:
        assert 111 in _failed_checks(conn, VERIFY / "14_verify_replenishment.sql")


def test_the_loader_refuses_documents_that_differ_from_the_plan(v009_db, plan):
    """An existing pilot order with other lines, or an R0 row with another value, is a conflict: nothing is written."""
    with _connect(v009_db) as conn:
        loc, prod = _ids(conn, "SE-WH-01", "TAB-000003")
        with conn.transaction():
            po = conn.execute("INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at) "
                              "VALUES ('PO-SE-2024-000001', %s, '2024-11-18 08:00+00') RETURNING purchase_order_id", (loc,)).fetchone()[0]
            conn.execute("INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, "
                         "order_reason, quantity_ordered) VALUES (%s, 1, %s, %s, 'forced_reorder', 5)", (po, loc, prod))
        changes = _change_log_rows(conn)
    with _connect(v009_db, LOADER) as conn:
        progress = plan_replenishment(conn, plan)
        assert not progress.ok and any("PO-SE-2024-000001" in c for c in progress.conflicts)
        with pytest.raises(ReplenishmentConflictError):
            apply_replenishment(conn, plan)
        assert _change_log_rows(conn) == changes
        assert conn.execute("SELECT count(*) FROM reorder_point_revisions").fetchone()[0] == 0


# --------------------------------------------------------------------------- interrupted runs
def test_an_interrupted_backfill_leaves_nothing_and_completes_on_rerun(v009_db, plan, monkeypatch):
    original = stock_units.record_reorder_point_revisions
    calls = {"n": 0}

    def crash_on_third(conn, ids, r):
        calls["n"] += 1
        if calls["n"] == 3:
            raise psycopg.OperationalError("simulated crash inside the backfill transaction")
        return original(conn, ids, r)

    monkeypatch.setattr(stock_units, "record_reorder_point_revisions", crash_on_third)
    with _connect(v009_db, LOADER) as conn:
        changes = _change_log_rows(conn)
        with pytest.raises(psycopg.OperationalError, match="simulated crash"):
            apply_replenishment(conn, plan)
        # one transaction: the 4 orders and the 2 revision sets written before the crash were rolled back too
        assert conn.execute("SELECT (SELECT count(*) FROM purchase_orders), (SELECT count(*) FROM reorder_point_revisions)"
                            ).fetchone() == (0, 0)
        assert _change_log_rows(conn) == changes
        monkeypatch.setattr(stock_units, "record_reorder_point_revisions", original)
        assert apply_replenishment(conn, plan).documents_written == 8
        assert conn.execute("SELECT (SELECT count(*) FROM purchase_orders), (SELECT count(*) FROM reorder_point_revisions)"
                            ).fetchone() == (4, 69)


@pytest.mark.parametrize("stop_after", [2, 9, 13, 15])
def test_an_interrupted_fresh_load_resumes_without_duplicate_documents(empty_v009_db, pilot_dataset, inventory, pilot,
                                                                       plan, stop_after):
    with _connect(empty_v009_db, LOADER) as conn:
        apply_load(conn, pilot_dataset)
        apply_inventory(conn, inventory)
        apply_transactions(conn, pilot)
        with pytest.raises(repl_load.LoadInterrupted):
            apply_replenishment(conn, plan, stop_after=stop_after)
        progress = plan_replenishment(conn, plan)
        assert progress.ok and progress.counts() == (stop_after, 17 - stop_after) and progress.documents_to_write == 0
        result = apply_replenishment(conn, plan)
        assert result.events_run == 17 - stop_after and result.documents_written == 0
        assert conn.execute("SELECT (SELECT count(*) FROM purchase_orders), (SELECT count(*) FROM purchase_order_lines), "
                            "(SELECT count(*) FROM reorder_point_revisions), "
                            "(SELECT count(*) FROM purchase_orders WHERE status = 'received')").fetchone() == (4, 4, 69, 4)


def test_the_run_protocol_survives_a_crash_and_resumes(v009_db):
    """Design 7.3: running row committed; a crash during step 7 (verify + checkpoint) leaves the run running
    and no checkpoint; the rerun completes it; a changed fingerprint is refused; the next window follows."""
    window = ("catch_up", "2024-12-03 00:00+00", "2024-12-04 00:00+00", H["a"], H["b"], H["c"], 12)
    insert_run = ("INSERT INTO meta.activity_runs (run_mode, window_start, window_end, plan_fingerprint, config_fingerprint, "
                  "start_state_hash, planned_units) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING activity_run_id")
    with _connect(v009_db, LOADER) as conn:
        run_id = conn.execute(insert_run, window).fetchone()[0]
        conn.execute("UPDATE meta.activity_runs SET completed_units = 12, unit_counts = '{\"place\": 12}' "
                     "WHERE activity_run_id = %s", (run_id,))
    crashing = _connect(v009_db, LOADER, autocommit=False)
    crashing.execute("UPDATE meta.activity_runs SET status = 'verified', end_state_hash = %s, finished_at = now() "
                     "WHERE activity_run_id = %s", (H["d"], run_id))
    crashing.execute("INSERT INTO meta.activity_checkpoints (activity_run_id, state_as_of, state_hash, state_snapshot) "
                     "VALUES (%s, '2024-12-04 00:00+00', %s, '{\"balances\": 1}')", (run_id, H["d"]))
    crashing.close()                                            # the process dies before COMMIT
    with _connect(v009_db, LOADER) as conn:
        assert conn.execute("SELECT status::text FROM meta.activity_runs WHERE activity_run_id = %s", (run_id,)).fetchone()[0] == "running"
        assert conn.execute("SELECT count(*) FROM meta.activity_checkpoints").fetchone()[0] == 0
        assert _sqlstate(conn, "UPDATE meta.activity_runs SET plan_fingerprint = %s WHERE activity_run_id = %s",
                         (H["e"], run_id)) == "42501"          # code or config changed inside a window: stop
        with conn.transaction():                                # the rerun: step 7 in one transaction
            conn.execute("UPDATE meta.activity_runs SET status = 'verified', end_state_hash = %s, finished_at = now() "
                         "WHERE activity_run_id = %s", (H["d"], run_id))
            conn.execute("INSERT INTO meta.activity_checkpoints (activity_run_id, state_as_of, state_hash, state_snapshot) "
                         "VALUES (%s, '2024-12-04 00:00+00', %s, '{\"balances\": 1}')", (run_id, H["d"]))
        assert conn.execute("SELECT status::text, end_state_hash FROM meta.activity_runs").fetchone() == ("verified", H["d"])
        assert _sqlstate(conn, insert_run, ("catch_up", "2024-12-05 00:00+00", "2024-12-06 00:00+00", H["a"], H["b"], H["d"], 3)) == "23514"
        assert _sqlstate(conn, insert_run, ("catch_up", "2024-12-04 00:00+00", "2024-12-05 00:00+00", H["a"], H["b"], H["d"], 3)) == "00000"
    with _connect(v009_db) as conn:
        conn.execute("SET ROLE nordiccart_owner")
        assert _sqlstate(conn, "UPDATE meta.activity_runs SET plan_fingerprint = %s WHERE activity_run_id = %s",
                         (H["e"], run_id)) == "23514"


# --------------------------------------------------------------------------- duplicates and concurrency
def _later_order(conn, number="PO-SE-2024-000002", quantity=3) -> tuple[int, int]:
    loc, prod = _ids(conn, "SE-WH-01", "ACC-000010")
    with conn.transaction():
        po = conn.execute("INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at) "
                          "VALUES (%s, %s, '2024-12-05 08:00+00') RETURNING purchase_order_id", (number, loc)).fetchone()[0]
        conn.execute("INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, "
                     "order_reason, quantity_ordered) VALUES (%s, 1, %s, %s, 'reorder_point', %s)", (po, loc, prod, quantity))
    return loc, prod


RECEIPT = ("INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, "
           "external_reference) VALUES (%s, %s, 'purchase_receipt', %s, '2024-12-09 08:00+00', %s)")


def test_duplicates_are_refused(backfilled_db):
    with _connect(backfilled_db, LOADER) as conn:
        loc, prod = _later_order(conn)
        assert _sqlstate(conn, "INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at) "
                               "VALUES ('PO-SE-2024-000002', %s, '2024-12-06 08:00+00')", (loc,)) == "23505"
        conn.execute(RECEIPT, (loc, prod, 1, "PO-SE-2024-000002/DN1"))
        assert _sqlstate(conn, RECEIPT, (loc, prod, 1, "PO-SE-2024-000002/DN1")) == "23505"        # same delivery note line
        assert _sqlstate(conn, RECEIPT, (loc, prod, 3, "PO-SE-2024-000002/DN2")) == "23514"        # more than outstanding
        # a revision that passes every guard (later, chained 13 -> 14, balance 13) but reuses the R0 document
        r_loc, r_prod = _ids(conn, "SE-WH-01", "ACC-000010")
        assert _sqlstate(conn, "INSERT INTO reorder_point_revisions (stock_location_id, product_id, revision_kind, effective_at, "
                               "old_reorder_point, new_reorder_point, document_reference) "
                               "VALUES (%s, %s, 'pilot_r0', '2024-12-20 06:00+00', 13, 14, 'R0-SE-WH-01-20241115')",
                         (r_loc, r_prod)) == "23505"
        assert conn.execute("SELECT count(*) FROM purchase_orders WHERE purchase_order_number LIKE 'PO-__-2024-000001'"
                            ).fetchone()[0] == 4                   # the pilot orders exist once


def test_concurrent_receipts_never_exceed_the_ordered_quantity(backfilled_db):
    """Two sessions receive 2 units each against a line of 3: the second waits for the first, then is refused."""
    with _connect(backfilled_db, LOADER) as conn:
        loc, prod = _later_order(conn)
    first = _connect(backfilled_db, LOADER, autocommit=False)
    first.execute(RECEIPT, (loc, prod, 2, "PO-SE-2024-000002/DN1"))          # holds the balance and order rows
    outcome = {}

    def second():
        with _connect(backfilled_db, LOADER) as conn:
            outcome["state"] = _sqlstate(conn, RECEIPT, (loc, prod, 2, "PO-SE-2024-000002/DN2"))

    t = threading.Thread(target=second)
    t.start()
    time.sleep(1.0)
    assert t.is_alive(), "the second receipt must wait for the first one's locks"
    first.commit()
    first.close()
    t.join(30)
    assert outcome["state"] == "23514"
    with _connect(backfilled_db) as conn:
        assert conn.execute("SELECT quantity_received, quantity_outstanding, status::text FROM purchase_order_lines "
                            "WHERE purchase_order_id = (SELECT purchase_order_id FROM purchase_orders "
                            "WHERE purchase_order_number = 'PO-SE-2024-000002')").fetchone() == (2, 1, "partially_received")


def test_two_backfills_at_once_write_the_documents_once(v009_db, plan):
    """Two replenishment loaders: the advisory lock lets one through; without the lock the database refuses doubles."""
    holder = _connect(v009_db, LOADER)
    key = holder.execute("SELECT hashtextextended('nordiccart.replenishment', 0)").fetchone()[0]
    holder.execute("SELECT pg_advisory_lock(%s)", (key,))
    with _connect(v009_db, LOADER) as conn:
        with pytest.raises(LoaderBusyError):
            apply_replenishment(conn, plan)
    holder.execute("SELECT pg_advisory_unlock(%s)", (key,))
    holder.close()
    # no lock: two sessions write the same pilot order at the same time
    a = _connect(v009_db, LOADER, autocommit=False)
    loc, prod = _ids(a, "SE-WH-01", "TAB-000003")
    a.execute("INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at) "
              "VALUES ('PO-SE-2024-000001', %s, '2024-11-18 08:00+00')", (loc,))
    outcome = {}

    def second():
        with _connect(v009_db, LOADER) as conn:
            outcome["state"] = _sqlstate(conn, "INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at) "
                                               "VALUES ('PO-SE-2024-000001', %s, '2024-11-18 08:00+00')", (loc,))

    t = threading.Thread(target=second)
    t.start()
    time.sleep(1.0)
    a.execute("INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, order_reason, "
              "quantity_ordered) SELECT purchase_order_id, 1, %s, %s, 'forced_reorder', 4 FROM purchase_orders "
              "WHERE purchase_order_number = 'PO-SE-2024-000001'", (loc, prod))
    a.commit()
    a.close()
    t.join(30)
    assert outcome["state"] == "23505"
    with _connect(v009_db, LOADER) as conn:
        assert apply_replenishment(conn, plan).documents_written == 7       # the other 3 orders and 4 revision sets
        assert conn.execute("SELECT (SELECT count(*) FROM purchase_orders), (SELECT count(*) FROM reorder_point_revisions)"
                            ).fetchone() == (4, 69)


def test_two_sessions_cannot_start_the_same_window(v009_db):
    insert = ("INSERT INTO meta.activity_runs (run_mode, window_start, window_end, plan_fingerprint, config_fingerprint, "
              "start_state_hash, planned_units) VALUES ('catch_up', '2024-12-03 00:00+00', '2024-12-04 00:00+00', %s, %s, %s, 5)")
    a = _connect(v009_db, LOADER, autocommit=False)
    a.execute(insert, (H["a"], H["b"], H["c"]))
    outcome = {}

    def second():
        with _connect(v009_db, LOADER) as conn:
            outcome["state"] = _sqlstate(conn, insert, (H["a"], H["b"], H["c"]))

    t = threading.Thread(target=second)
    t.start()
    time.sleep(1.0)
    assert t.is_alive(), "the second start must wait on the exclusion constraint"
    a.commit()
    a.close()
    t.join(30)
    assert outcome["state"] == "23P01"                                    # ex_activity_runs_no_overlap
    with _connect(v009_db) as conn:
        assert conn.execute("SELECT count(*) FROM meta.activity_runs").fetchone()[0] == 1


RUN_INSERT = ("INSERT INTO meta.activity_runs (run_mode, window_start, window_end, plan_fingerprint, config_fingerprint, "
              "start_state_hash, planned_units) VALUES ('catch_up', %s, %s, %s, %s, %s, 5)")


@pytest.mark.parametrize("ledger, isolation", [("empty", "read committed"), ("empty", "repeatable read"),
                                               ("one verified run", "read committed")])
def test_concurrent_disjoint_starts_cannot_fork_the_run_chain(v009_db, ledger, isolation):
    """S4 review finding: the guard reads the latest run without a lock, so with an empty ledger two sessions
    could both start a FIRST window, disjoint (no overlap to stop them): a gap in the chain. Reproduced on the
    reviewed V009 (both committed, in READ COMMITTED and REPEATABLE READ). Fix: previous_run_id with
    UNIQUE NULLS NOT DISTINCT: the second start waits for the first and fails with 23505, in either isolation.
    With a verified run already present, both next windows must start at its end, so they overlap (23P01)
    or name the same predecessor (23505)."""
    first, second = ("2024-12-03 00:00+00", "2024-12-04 00:00+00"), ("2024-12-10 00:00+00", "2024-12-11 00:00+00")
    if ledger == "one verified run":
        with _connect(v009_db, LOADER) as conn:
            with conn.transaction():
                run_id = conn.execute(RUN_INSERT + " RETURNING activity_run_id",
                                      ("2024-12-02 00:00+00", "2024-12-03 00:00+00", H["a"], H["b"], H["c"])).fetchone()[0]
                conn.execute("UPDATE meta.activity_runs SET status = 'verified', completed_units = 5, end_state_hash = %s, "
                             "finished_at = now() WHERE activity_run_id = %s", (H["d"], run_id))
                conn.execute("INSERT INTO meta.activity_checkpoints (activity_run_id, state_as_of, state_hash, state_snapshot) "
                             "VALUES (%s, '2024-12-03 00:00+00', %s, '{}')", (run_id, H["d"]))
        second = ("2024-12-03 00:00+00", "2024-12-05 00:00+00")          # contiguous too, but a different window
    b = _connect(v009_db, LOADER, autocommit=False)
    b.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation.upper()}")
    b.execute("SELECT count(*) FROM meta.activity_runs")                   # B's snapshot: before A's start
    a = _connect(v009_db, LOADER, autocommit=False)
    a.execute(RUN_INSERT, (*first, H["a"], H["b"], H["c"]))                # A starts its window, not committed
    outcome = {}

    def session_b():
        try:
            b.execute(RUN_INSERT, (*second, H["a"], H["b"], H["c"]))
            b.commit()
            outcome["state"] = "00000"
        except psycopg.Error as exc:
            b.rollback()
            outcome["state"] = f"{exc.sqlstate} {exc.diag.constraint_name}"

    t = threading.Thread(target=session_b)
    t.start()
    time.sleep(1.0)
    assert t.is_alive(), "the second start must wait for the first one's uncommitted row"
    a.commit()
    a.close()
    t.join(30)
    b.close()
    expected = {"23505 uq_activity_runs_previous"} if ledger == "empty" else \
        {"23505 uq_activity_runs_previous", "23P01 ex_activity_runs_no_overlap"}
    assert outcome["state"] in expected, outcome
    with _connect(v009_db) as conn:
        assert conn.execute("SELECT count(*) FILTER (WHERE previous_run_id IS NULL), count(*) FROM meta.activity_runs"
                            ).fetchone() == (1, 2 if ledger != "empty" else 1)
        assert _failed_checks(conn, VERIFY / "18_verify_v009_activity_control.sql") == [
            301, 401, 402, 403, 404, 405, 406, 501, 502, 503]           # no run-ledger check fails (703, 704)


# --------------------------------------------------------------------------- unauthorized changes
REFUSED_FOR_THE_LOADER = {
    "set an order status": "UPDATE purchase_orders SET status = 'open'",
    "change a received quantity": "UPDATE purchase_order_lines SET quantity_received = 0",
    "change an ordered quantity": "UPDATE purchase_order_lines SET quantity_ordered = 1",
    "delete orders": "DELETE FROM purchase_orders",
    "delete order lines": "DELETE FROM purchase_order_lines",
    "truncate orders": "TRUNCATE purchase_orders",
    "change a revision": "UPDATE reorder_point_revisions SET new_reorder_point = 0",
    "delete revisions": "DELETE FROM reorder_point_revisions",
    "truncate revisions": "TRUNCATE reorder_point_revisions",
    "move a run window": "UPDATE meta.activity_runs SET window_end = now()",
    "change a run fingerprint": "UPDATE meta.activity_runs SET plan_fingerprint = repeat('0', 64)",
    "delete runs": "DELETE FROM meta.activity_runs",
    "change a checkpoint": "UPDATE meta.activity_checkpoints SET state_hash = repeat('0', 64)",
    "delete checkpoints": "DELETE FROM meta.activity_checkpoints",
    "call the order refresh": "SELECT fn_purchase_order_refresh(1)",
    "call the receipt trigger": "SELECT fn_purchase_receipt_apply()",
    "call an order guard": "SELECT fn_purchase_orders_guard()",
    "disable the receipt trigger": "ALTER TABLE inventory_movements DISABLE TRIGGER trg_inventory_movements_purchase_order_receipt",
    "drop the history": "DROP TABLE reorder_point_revisions",
}


@pytest.mark.parametrize("attempt", sorted(REFUSED_FOR_THE_LOADER))
def test_the_loader_is_refused(backfilled_db, attempt):
    with _connect(backfilled_db, LOADER) as conn:
        assert _sqlstate(conn, REFUSED_FOR_THE_LOADER[attempt]) == "42501", attempt


def test_the_guards_bind_the_owner(backfilled_db):
    with _connect(backfilled_db) as conn:
        loc, prod = _ids(conn, "SE-WH-01", "TAB-000003")
        conn.execute("SET ROLE nordiccart_owner")
        assert _sqlstate(conn, "UPDATE purchase_order_lines SET quantity_received = 0, status = 'open'") == "23514"
        assert _sqlstate(conn, "UPDATE purchase_orders SET status = 'open', received_at = NULL") == "23514"
        assert _sqlstate(conn, "UPDATE purchase_orders SET ordered_at = ordered_at + interval '1 day'") == "23514"
        assert _sqlstate(conn, "DELETE FROM purchase_orders") == "23000"
        assert _sqlstate(conn, "TRUNCATE purchase_order_lines") == "23000"
        assert _sqlstate(conn, "UPDATE reorder_point_revisions SET new_reorder_point = 1") == "23000"
        assert _sqlstate(conn, "TRUNCATE meta.activity_checkpoints") == "23000"
        assert _sqlstate(conn, RECEIPT, (loc, prod, 1, "PO-SE-2024-000001/DN3")) == "23514"    # received already


# --------------------------------------------------------------------------- rollback
def test_r009_restores_the_v008_catalog_and_v009_reapplies(v008_db, plan):
    with _connect(v008_db) as conn:
        catalog, content = _catalog(conn), _digest(conn, CONTENT)
        conn.execute(V009.read_text(encoding="utf-8"))
    with _connect(v008_db, LOADER) as conn:
        assert apply_replenishment(conn, plan).documents_written == 8
    with _connect(v008_db) as conn:
        conn.execute(R009.read_text(encoding="utf-8"))
        assert _catalog(conn) == catalog                 # tables, columns, constraints, triggers, indexes, functions, ACLs
        assert _digest(conn, CONTENT) == content         # no business row changed
        conn.execute(V009.read_text(encoding="utf-8"))   # can be applied again
    with _connect(v008_db, LOADER) as conn:
        assert apply_replenishment(conn, plan).documents_written == 8


def test_r009_refuses_without_v009_and_once_runs_exist(v008_db, v009_db):
    with _connect(v008_db) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="not applied"):
            conn.execute(R009.read_text(encoding="utf-8"))
    with _connect(v009_db, LOADER) as conn:
        conn.execute("INSERT INTO meta.activity_runs (run_mode, window_start, window_end, plan_fingerprint, config_fingerprint, "
                     "start_state_hash, planned_units) VALUES ('catch_up', '2024-12-03', '2024-12-04', %s, %s, %s, 1)",
                     (H["a"], H["b"], H["c"]))
    with _connect(v009_db) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="activity_runs holds 1 runs"):
            conn.execute(R009.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- regression on V009
@pytest.mark.parametrize("suite, tests", [("test_foundation_constraints.sql", 44), ("test_v006_orders_and_payments.sql", 119),
                                          ("test_v007_inventory.sql", 105), ("test_v008_roles.sql", 87),
                                          ("test_v009_activity_control.sql", 96)])
def test_the_sql_behaviour_suites_pass_on_v009(backfilled_db, suite, tests):
    with _connect(backfilled_db) as conn:
        count, failed = _suite(conn, REPO_ROOT / "tests" / "sql" / suite)
    assert count == tests and not failed, failed[:5]


@pytest.mark.parametrize("script, expected", [
    ("01", None), ("02", None), ("03", None), ("04", None), ("05", None), ("06", None), ("07", None), ("08", None),
    ("09", None), ("10", None), ("11", (44, 33, 11, 0)), ("12", (24, 17, 7, 0)), ("13", (52, 45, 7, 0)),
    ("14", (44, 37, 7, 0)), ("15", (43, 35, 8, 0)), ("17", (31, 27, 4, 0)), ("18", (44, 38, 6, 0)),
])
def test_the_verification_scripts_pass_as_the_analyst_on_v009(backfilled_db, script, expected):
    with _connect(backfilled_db, ANALYST) as conn:
        result = _script(conn, next(VERIFY.glob(f"{script}_*.sql")))
    assert result[3] == 0, f"{script}: {result}"
    if expected:
        assert result == expected


def test_script_18_fails_exactly_the_pilot_document_checks_before_the_backfill(v009_db):
    with _connect(v009_db, ANALYST) as conn:
        assert _failed_checks(conn, VERIFY / "18_verify_v009_activity_control.sql") == [
            301, 401, 402, 403, 404, 405, 406, 501, 502, 503]


def test_the_command_lines_run_as_the_loader_on_v009(v009_db, monkeypatch, tmp_path):
    monkeypatch.setenv("PGDATABASE", v009_db)
    monkeypatch.setenv("PGUSER", LOADER)
    config = tmp_path / "generator.toml"
    config.write_text((REPO_ROOT / "config" / "generator.toml").read_text(encoding="utf-8")
                      .replace('log_dir = "logs"', f'log_dir = "{(tmp_path / "logs").as_posix()}"'), encoding="utf-8")
    base = ["--config", str(config), "--no-csv", "--env-file", str(tmp_path / "none.env")]
    assert repl_cli.main(base + ["--plan"]) == 0
    assert repl_cli.main(base + ["--apply", "--yes"]) == 0           # the backfill
    assert repl_cli.main(base + ["--apply", "--yes"]) == 0           # nothing to write, verified
    assert tx_cli.main(base + ["--plan"]) == 0
    text = "\n".join(p.read_text(encoding="utf-8") for p in (tmp_path / "logs").glob("*.log"))
    assert "8 V009 documents would be written" in text and "committed: 8 V009 documents" in text
    assert "nothing to write" in text and f"as {LOADER}" in text and "009" in text


def test_the_concurrency_scenarios_pass_on_v009_as_the_loader(templates):
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" / "python" / "test_v007_concurrency.py"), "--recreate", "--through", "9",
         "--role", LOADER], capture_output=True, text=True, timeout=600, env=os.environ.copy())
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "Scenarios: 14  PASS: 14  FAIL: 0" in result.stdout
