"""Database tests for V008 security (Phase 3, step 5, stage S3) (opt-in).

They prove against real PostgreSQL that:
  * V008 applies to the existing state (pilots loaded) without changing a row, refuses a second
    run, and rolls back as a whole when its self-check fails;
  * roles, memberships, settings, ownership and SECURITY DEFINER functions are as designed, and
    the complete privilege matrix (docs/phase3_v008_security_privilege_matrix.md) holds for every
    role, table, column, function, schema and the database;
  * every pilot loader runs end to end as nordiccart_loader and produces exactly the data the
    superuser produced; customer transactions, reservations, fulfilment, receipts and transfers
    work as the loader; both pilots verify and scripts 01 to 15 and 17 pass as nordiccart_analyst;
  * privilege escalation, protected functions, unsafe search paths and direct changes of
    protected records are refused for real logins (42501), while the V002, V006 and V007 guards
    still bind even the owner;
  * the rollback script returns the catalog to its exact pre-V008 state, and V008 re-applies;
  * the SQL behaviour suites (foundation, V006, V007, V008) and the 14 concurrency scenarios
    (as nordiccart_loader) pass on V008.

Safety
  * Runs only when NORDICCART_DB_TESTS=1 is set; otherwise every test is skipped.
  * Creates its own databases (nordiccart_v008_test_*) and drops them. Never connects to
    nordiccart_oltp. V008 creates CLUSTER roles (nordiccart_*): run this only against a disposable
    server (the S3 sandbox), never against the real one. The role must be a superuser.
  * Login as the NordicCart roles needs an authentication method that lets them in without a
    password (the sandbox uses trust); the roles have no password.

Run from the repository folder (PowerShell), with all PG variables pointing to the sandbox:
    $env:NORDICCART_DB_TESTS = "1"
    python -m pytest tests/python/test_v008_security_db.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from nordiccart.inventory.config import load_inventory_settings  # noqa: E402
from nordiccart.inventory.generate import generate_opening_inventory  # noqa: E402
from nordiccart.inventory.load import apply_inventory, plan_inventory  # noqa: E402
from nordiccart.master_data.load import apply_load, plan_load  # noqa: E402
from nordiccart.replenishment import cli as repl_cli  # noqa: E402
from nordiccart.replenishment.config import load_replenishment_settings  # noqa: E402
from nordiccart.replenishment.generate import generate_replenishment  # noqa: E402
from nordiccart.replenishment.load import apply_replenishment, plan_replenishment  # noqa: E402
from nordiccart.settings import load_env_file  # noqa: E402
from nordiccart.transactions import cli as tx_cli  # noqa: E402
from nordiccart.transactions.config import load_transaction_settings  # noqa: E402
from nordiccart.transactions.generate import generate_transactions  # noqa: E402
from nordiccart.transactions.load import apply_transactions, plan_transactions  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("NORDICCART_DB_TESTS") != "1",
                                reason="set NORDICCART_DB_TESTS=1 to run the database tests")

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "database" / "migrations"
V008 = MIGRATIONS / "V008__security_roles_and_privileges.sql"
R008 = REPO_ROOT / "database" / "rollback" / "R008__undo_security_roles_and_privileges.sql"
REAPPLY = REPO_ROOT / "database" / "security" / "V008_reapply_database_privileges.sql"
PREFIX = "nordiccart_v008_test"
TEMPLATE_V007 = f"{PREFIX}_template_v007"      # V001-V007, master data, opening inventory, both pilots
TEMPLATE_V008 = f"{PREFIX}_template_v008"      # + V008: the real database's state after S3
TEMPLATE_EMPTY = f"{PREFIX}_template_empty"    # V001-V008, no rows
LOADER, SCHEDULER, ANALYST, EXTRACTOR = ("nordiccart_loader", "nordiccart_scheduler", "nordiccart_analyst",
                                         "nordiccart_extractor")
ROLES = ("nordiccart_owner", "nordiccart_writer", "nordiccart_reader", LOADER, SCHEDULER, ANALYST, EXTRACTOR)

# --------------------------------------------------------------------------- the expected privilege matrix
BUSINESS = ("public.currencies", "public.countries", "public.exchange_rates", "public.product_categories",
            "public.products", "public.product_prices", "public.customers", "public.stock_locations", "public.stores",
            "public.warehouses", "public.orders", "public.order_items", "public.payment_attempts",
            "public.inventory_balances", "public.inventory_reservations", "public.inventory_movements")
TABLES = BUSINESS + ("public.change_log", "public.change_log_checkpoints", "meta.schema_migrations")
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
WRITER_UPDATE = {
    "public.orders": {"status", "cancellation_reason", "confirmed_at", "fulfilled_at", "cancelled_at"},
    "public.payment_attempts": {"status", "authorized_at", "captured_at", "failed_at", "failure_reason",
                                "provider_reference"},
    "public.inventory_reservations": {"status", "closed_at"},
    "public.inventory_balances": {"reorder_point"},
    "public.product_prices": {"valid_to"},
    "public.products": {"discontinued_on", "is_active"},
    "public.customers": {"email", "phone", "address_line1", "postal_code", "city", "is_active"},
}
CHANGE_LOG_METADATA = {"change_id", "table_name", "operation", "transaction_id", "changed_at"}
WRITER_FUNCTIONS = {
    "fn_reserve_order_stock(bigint,timestamp with time zone,timestamp with time zone)",
    "fn_close_order_reservations(bigint,reservation_status,timestamp with time zone)",
    "fn_fulfil_order_stock(bigint,timestamp with time zone)",
    "fn_lock_order_balances(bigint)", "fn_assert_order_consistent(bigint)",
    "fn_assert_order_inventory_consistent(bigint)",
}
DEFINER_FUNCTIONS = {"fn_capture_change()", "fn_inventory_apply_movement()", "fn_inventory_apply_reservation()"}


def expected_matrix(columns: dict[str, list[str]], functions: list[str]) -> dict:
    """(role, object) -> privileges, from the design (section 4.1) and V008."""
    out: dict = {}
    for role in ("nordiccart_owner", "nordiccart_writer", LOADER, SCHEDULER, "nordiccart_reader", ANALYST,
                 EXTRACTOR, "public"):
        writer = role in ("nordiccart_writer", LOADER, SCHEDULER)
        reader = role in ("nordiccart_reader", ANALYST)
        for t in TABLES:
            cols = set(columns[t])
            if role == "nordiccart_owner":
                table, select, update = set(TABLE_PRIVILEGES), cols, cols
            elif writer and t in BUSINESS:
                table, select, update = {"SELECT", "INSERT"}, cols, WRITER_UPDATE.get(t, set())
            elif writer and t == "meta.schema_migrations":
                table, select, update = {"SELECT"}, cols, set()
            elif writer and t == "public.change_log":
                table, select, update = set(), CHANGE_LOG_METADATA, set()
            elif reader:
                table, select, update = {"SELECT"}, cols, set()
            elif role == EXTRACTOR and t == "public.change_log":
                table, select, update = {"SELECT", "DELETE"}, cols, set()
            elif role == EXTRACTOR and t == "public.change_log_checkpoints":
                table, select, update = {"SELECT", "INSERT"}, cols, {"last_snapshot"}
            else:
                table, select, update = set(), set(), set()
            out[(role, t)] = (frozenset(table), frozenset(select), frozenset(update))
        for f in functions:
            out[(role, f)] = role == "nordiccart_owner" or (writer and f in WRITER_FUNCTIONS)
        schema = {"public": set(), "meta": set()}
        if role == "nordiccart_owner":
            schema = {"public": {"USAGE", "CREATE"}, "meta": {"USAGE", "CREATE"}}
        elif writer or reader:
            schema = {"public": {"USAGE"}, "meta": {"USAGE"}}
        elif role == EXTRACTOR:
            schema = {"public": {"USAGE"}, "meta": set()}
        out[(role, "schemas")] = {k: frozenset(v) for k, v in schema.items()}
        out[(role, "database")] = frozenset({"CONNECT"} if (writer or reader or role == EXTRACTOR) else set())
    return out


def actual_matrix(conn, columns: dict[str, list[str]], functions: list[str]) -> dict:
    out: dict = {}
    roles = ("nordiccart_owner", "nordiccart_writer", LOADER, SCHEDULER, "nordiccart_reader", ANALYST, EXTRACTOR, "public")
    for role in roles:
        for t in TABLES:
            table = {p for p in TABLE_PRIVILEGES
                     if conn.execute("SELECT has_table_privilege(%s, %s, %s)", (role, t, p)).fetchone()[0]}
            select = {c for c in columns[t]
                      if conn.execute("SELECT has_column_privilege(%s, %s, %s, 'SELECT')", (role, t, c)).fetchone()[0]}
            update = {c for c in columns[t]
                      if conn.execute("SELECT has_column_privilege(%s, %s, %s, 'UPDATE')", (role, t, c)).fetchone()[0]}
            out[(role, t)] = (frozenset(table), frozenset(select), frozenset(update))
        for f in functions:
            out[(role, f)] = conn.execute("SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
                                          (role, f"public.{f}" if not f.startswith("assert_") else f"meta.{f}")).fetchone()[0]
        out[(role, "schemas")] = {s: frozenset(p for p in ("USAGE", "CREATE")
                                               if conn.execute("SELECT has_schema_privilege(%s, %s, %s)", (role, s, p)).fetchone()[0])
                                  for s in ("public", "meta")}
        out[(role, "database")] = frozenset(p for p in ("CONNECT", "TEMPORARY", "CREATE")
                                            if conn.execute("SELECT has_database_privilege(%s, current_database(), %s)",
                                                            (role, p)).fetchone()[0])
    return out


# --------------------------------------------------------------------------- databases
def _admin():
    return psycopg.connect(dbname="postgres", autocommit=True)


def _connect(dbname: str, user: str | None = None):
    assert dbname.startswith(PREFIX), "tests only touch their own databases"
    conn = psycopg.connect(dbname=dbname, autocommit=True, **({"user": user} if user else {}))
    conn.execute("SET search_path TO public")
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
    """A copy of a template database; database-level privileges are re-applied (not copied by PostgreSQL)."""
    with _admin() as admin:
        _create(admin, name, template)
    if template != TEMPLATE_V007:
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
            "the V008 tests need a superuser on a disposable server"
        for name in (TEMPLATE_V008, TEMPLATE_V007, TEMPLATE_EMPTY):
            _drop(admin, name)
        _create(admin, TEMPLATE_V007)
        _create(admin, TEMPLATE_EMPTY)
    with _connect(TEMPLATE_V007) as conn:
        _migrate(conn, 7)
        apply_load(conn, pilot_dataset)
        apply_inventory(conn, inventory)
        assert apply_transactions(conn, pilot).events_run == 100
        assert apply_replenishment(conn, plan).events_run == 17
    with _admin() as admin:
        _create(admin, TEMPLATE_V008, TEMPLATE_V007)
    with _connect(TEMPLATE_V008) as conn:
        conn.execute(V008.read_text(encoding="utf-8"))
    with _connect(TEMPLATE_EMPTY) as conn:
        _migrate(conn, 8)
    yield
    with _admin() as admin:
        for name in (TEMPLATE_V008, TEMPLATE_V007, TEMPLATE_EMPTY):
            _drop(admin, name)


def _name(request, suffix: str = "") -> str:
    """A database name per test and fixture (a test may use two fixtures at once)."""
    base = f"{PREFIX}_{request.node.name}".lower().replace("[", "_").replace("]", "").replace("-", "_")
    return base[:60 - len(suffix)] + suffix


@pytest.fixture
def v007_db(templates, request):
    """The real database's state before V008."""
    name = _copy(TEMPLATE_V007, _name(request, "_v7"))
    yield name
    with _admin() as admin:
        _drop(admin, name)


@pytest.fixture
def v008_db(templates, request):
    """The real database's state after V008."""
    name = _copy(TEMPLATE_V008, _name(request, "_v8"))
    yield name
    with _admin() as admin:
        _drop(admin, name)


@pytest.fixture
def empty_v008_db(templates, request):
    name = _copy(TEMPLATE_EMPTY, _name(request, "_e8"))
    yield name
    with _admin() as admin:
        _drop(admin, name)


# --------------------------------------------------------------------------- reading
CONTENT = {
    "orders": """SELECT o.order_number, c.customer_number, o.channel, o.status, o.cancellation_reason, o.items_total,
                        o.order_total, o.placed_at, o.confirmed_at, o.fulfilled_at, o.cancelled_at
                   FROM orders o JOIN customers c USING (customer_id)""",
    "order_items": """SELECT o.order_number, i.line_number, p.sku, i.quantity, i.unit_price, i.line_total, i.vat_amount
                        FROM order_items i JOIN orders o USING (order_id) JOIN products p USING (product_id)""",
    "payment_attempts": """SELECT o.order_number, a.attempt_number, a.payment_method, a.amount, a.status, a.provider_reference,
                                  a.attempted_at, a.captured_at, a.failed_at FROM payment_attempts a JOIN orders o USING (order_id)""",
    "inventory_reservations": """SELECT o.order_number, i.line_number, r.quantity, r.status, r.reserved_at, r.closed_at
                                   FROM inventory_reservations r JOIN order_items i USING (order_item_id)
                                   JOIN orders o USING (order_id)""",
    "inventory_movements": """SELECT m.movement_type, l.location_code, p.sku, m.quantity_change, m.occurred_at, m.external_reference
                                FROM inventory_movements m JOIN stock_locations l USING (stock_location_id)
                                JOIN products p USING (product_id)""",
    "inventory_balances": """SELECT l.location_code, p.sku, b.quantity_on_hand, b.quantity_reserved, b.reorder_point
                               FROM inventory_balances b JOIN stock_locations l USING (stock_location_id)
                               JOIN products p USING (product_id)""",
    "master": """SELECT 'p', sku, product_name, launched_on::text FROM products UNION ALL
                 SELECT 'c', customer_number, email, registered_at::text FROM customers UNION ALL
                 SELECT 'l', location_code, location_name, opened_on::text FROM stock_locations""",
}


def _content(conn) -> dict:
    """Business content per table (no surrogate ids, no load times), timestamps in UTC."""
    out = {}
    with conn.transaction():
        conn.execute("SET LOCAL TimeZone = 'UTC'")
        for name, sql in CONTENT.items():
            out[name] = conn.execute(
                f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM ({sql}) t").fetchone()
        out["change_log rows"] = conn.execute("SELECT count(*) FROM change_log").fetchone()[0]
    return out


def _sql_result(conn, path: Path) -> tuple[int, int, int, int]:
    """(rows, PASS, INFO, FAIL) of a verification script, or (rows, PASS, 0, FAIL) of a SQL test file."""
    rows = conn.execute(path.read_text(encoding="utf-8")).fetchall()
    statuses = [r[5] for r in rows]
    return len(rows), statuses.count("PASS"), statuses.count("INFO"), statuses.count("FAIL")


def _columns(conn) -> dict[str, list[str]]:
    out = {}
    for t in TABLES:
        out[t] = [r[0] for r in conn.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped "
            "ORDER BY attnum", (t,)).fetchall()]
    return out


def _functions(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass "
        "AND d.objid = p.oid AND d.deptype = 'e') ORDER BY 1").fetchall()]


CATALOG = """
SELECT 'class ' || n.nspname || '.' || c.relname, pg_get_userbyid(c.relowner),
       (SELECT array_agg(x::text ORDER BY x::text)
          FROM unnest(coalesce(c.relacl, acldefault(CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END::"char", c.relowner))) x)::text
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('public', 'meta') AND c.relkind IN ('r', 'S')
UNION ALL
SELECT 'column ' || c.relname || '.' || a.attname, NULL,
       (SELECT array_agg(x::text ORDER BY x::text) FROM unnest(a.attacl) x)::text
  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('public', 'meta') AND c.relkind = 'r' AND a.attnum > 0 AND a.attacl IS NOT NULL
   AND cardinality(a.attacl) > 0
UNION ALL
SELECT 'function ' || p.oid::regprocedure::text, pg_get_userbyid(p.proowner),
       p.prosecdef::text || ' ' || coalesce(p.proconfig::text, '-') || ' '
       || (SELECT array_agg(x::text ORDER BY x::text) FROM unnest(coalesce(p.proacl, acldefault('f', p.proowner))) x)::text
       || ' ' || coalesce(obj_description(p.oid, 'pg_proc'), '')
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname IN ('public', 'meta')
UNION ALL
SELECT 'type ' || t.typname, pg_get_userbyid(t.typowner),
       (SELECT array_agg(x::text ORDER BY x::text) FROM unnest(coalesce(t.typacl, acldefault('T', t.typowner))) x)::text
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


def _sqlstate(conn, sql: str, params=None) -> str:
    """'00000' or the SQLSTATE of the error; always rolled back."""
    try:
        with conn.transaction():
            conn.execute(sql, params)
            raise psycopg.Rollback()
    except psycopg.Error as exc:
        return exc.sqlstate or "?"
    return "00000"


# --------------------------------------------------------------------------- the migration
def test_v008_keeps_every_row_and_records_itself(v007_db):
    with _connect(v007_db) as conn:
        before = _content(conn)
        conn.execute(V008.read_text(encoding="utf-8"))
        assert _content(conn) == before                                  # not one business row or change_log row
        assert conn.execute("SELECT applied_by FROM meta.schema_migrations WHERE version = '008'").fetchone()[0] == "postgres"


def test_v008_refuses_a_second_run_and_a_missing_v007(v008_db, templates):
    with _connect(v008_db) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="already applied"):
            conn.execute(V008.read_text(encoding="utf-8"))
    name = f"{PREFIX}_only_v006"
    with _admin() as admin:
        _create(admin, name)
    try:
        with _connect(name) as conn:
            _migrate(conn, 6)
            with pytest.raises(psycopg.errors.RaiseException, match="requires V007"):
                conn.execute(V008.read_text(encoding="utf-8"))
    finally:
        with _admin() as admin:
            _drop(admin, name)


def test_a_failing_self_check_rolls_back_the_whole_migration(v007_db):
    """A stray SECURITY DEFINER function is caught by the self-check; nothing of V008 stays."""
    with _connect(v007_db) as conn:
        conn.execute("CREATE FUNCTION public.nc_stray() RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'")
        before = _catalog(conn)
        with pytest.raises(psycopg.errors.RaiseException, match="SECURITY DEFINER or search_path not as designed"):
            conn.execute(V008.read_text(encoding="utf-8"))
        conn.execute("ROLLBACK")                                          # the failed script's open transaction
        assert _catalog(conn) == before
        assert conn.execute("SELECT count(*) FROM meta.schema_migrations WHERE version = '008'").fetchone()[0] == 0


# --------------------------------------------------------------------------- the catalog after V008
def test_roles_have_least_attributes_memberships_and_settings(v008_db):
    with _connect(v008_db) as conn:
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT rolname, rolcanlogin, rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls, "
            "rolconnlimit, coalesce(array_to_string(array(SELECT unnest(rolconfig) ORDER BY 1), ','), '') "
            "FROM pg_roles WHERE rolname LIKE 'nordiccart%'").fetchall()}
        assert set(rows) == set(ROLES)
        assert {r for r, v in rows.items() if v[0]} == {LOADER, SCHEDULER, ANALYST, EXTRACTOR}
        assert not any(v[1] for v in rows.values())
        assert {r: v[2] for r, v in rows.items()} == {"nordiccart_owner": 0, "nordiccart_writer": 0, "nordiccart_reader": 0,
                                                       LOADER: 30, SCHEDULER: 3, ANALYST: 10, EXTRACTOR: 3}
        assert rows[LOADER][3] == "idle_in_transaction_session_timeout=60s,lock_timeout=10s,statement_timeout=30min"
        assert rows[SCHEDULER][3] == "idle_in_transaction_session_timeout=60s,lock_timeout=10s,statement_timeout=5min"
        assert rows[ANALYST][3] == "default_transaction_read_only=on,idle_in_transaction_session_timeout=5min,statement_timeout=15min"
        assert rows[EXTRACTOR][3] == "idle_in_transaction_session_timeout=60s,lock_timeout=10s,statement_timeout=30min"
        members = set(conn.execute(
            "SELECT r.rolname, g.rolname, m.inherit_option, m.set_option, m.admin_option FROM pg_auth_members m "
            "JOIN pg_roles r ON r.oid = m.member JOIN pg_roles g ON g.oid = m.roleid "
            "WHERE r.rolname LIKE 'nordiccart%' OR g.rolname LIKE 'nordiccart%'").fetchall())
        assert members == {(LOADER, "nordiccart_writer", True, False, False),
                           (SCHEDULER, "nordiccart_writer", True, False, False),
                           (ANALYST, "nordiccart_reader", True, False, False)}


def test_ownership_and_security_definer_functions(v008_db):
    with _connect(v008_db) as conn:
        owners = conn.execute("""
            SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname IN ('public', 'meta') AND c.relkind IN ('r', 'S', 'i')
               AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
            UNION SELECT DISTINCT pg_get_userbyid(p.proowner) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname IN ('public', 'meta')
               AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
            UNION SELECT DISTINCT pg_get_userbyid(t.typowner) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
             WHERE n.nspname IN ('public', 'meta') AND t.typtype = 'e'
            UNION SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'meta'""").fetchall()
        assert owners == [("nordiccart_owner",)]
        definers = conn.execute(
            "SELECT p.oid::regprocedure::text, p.proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname IN ('public', 'meta') AND p.prosecdef ORDER BY 1").fetchall()
        assert {d[0] for d in definers} == DEFINER_FUNCTIONS
        assert all(d[1] == ["search_path=pg_catalog, public, pg_temp"] for d in definers)
        assert conn.execute("SELECT extowner::regrole::text FROM pg_extension WHERE extname = 'btree_gist'").fetchone()[0] == "postgres"


def test_the_complete_privilege_matrix(v008_db):
    with _connect(v008_db) as conn:
        columns, functions = _columns(conn), _functions(conn)
        assert len(functions) == 23 and len(TABLES) == 19
        functions.append("meta.assert_migration_can_run(character varying,character varying)")
        expected = expected_matrix(columns, functions[:-1])
        expected.update({(r, "assert") : r == "nordiccart_owner" for r in
                         ("nordiccart_owner", "nordiccart_writer", LOADER, SCHEDULER, "nordiccart_reader", ANALYST, EXTRACTOR, "public")})
        actual = actual_matrix(conn, columns, functions[:-1])
        for r in ("nordiccart_owner", "nordiccart_writer", LOADER, SCHEDULER, "nordiccart_reader", ANALYST, EXTRACTOR, "public"):
            actual[(r, "assert")] = conn.execute(
                "SELECT has_function_privilege(%s, 'meta.assert_migration_can_run(varchar, varchar)', 'EXECUTE')", (r,)).fetchone()[0]
        differences = sorted(f"{k}: expected {expected[k]} actual {actual[k]}" for k in expected if expected[k] != actual[k])
        assert not differences, differences[:10]
        assert len(expected) == 8 * (19 + 23 + 3)


def test_verification_script_17_passes_after_v008_and_fails_before(v008_db, v007_db):
    script = REPO_ROOT / "sql" / "verification" / "17_verify_v008_security.sql"
    with _connect(v008_db, ANALYST) as conn:
        assert _sql_result(conn, script) == (31, 27, 4, 0)
    with _connect(v007_db) as conn:
        assert _sql_result(conn, script)[3] >= 10                         # V008 missing: many FAILs


def test_verification_script_17_reports_missing_roles_instead_of_failing(v007_db):
    """On a server without the NordicCart roles (the real one before V008, or after R008 dropped them) script 17 must
    still run and report FAIL rows. Found in the S3 rehearsal. The roles are renamed inside a transaction that is rolled
    back, so no other database or session sees it."""
    script = REPO_ROOT / "sql" / "verification" / "17_verify_v008_security.sql"
    with _connect(v007_db) as conn:
        with conn.transaction(force_rollback=True):
            for role in ROLES:
                conn.execute(f"ALTER ROLE {role} RENAME TO nc_hidden_{role}")
            assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'nordiccart%'").fetchone()[0] == 0
            assert _sql_result(conn, script) == (31, 7, 4, 20)
        assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'nordiccart%'").fetchone()[0] == 7


def test_a_copy_or_restore_needs_the_database_privileges_reapplied(templates, request):
    """CREATE DATABASE ... TEMPLATE (and pg_restore without --create) do not carry database-level ACLs."""
    name = _name(request)
    with _admin() as admin:
        _create(admin, name, TEMPLATE_V008)
    try:
        with _connect(name) as conn:
            assert conn.execute("SELECT has_database_privilege('public', current_database(), 'TEMPORARY')").fetchone()[0]
            assert 301 in {r[0] for r in conn.execute(
                (REPO_ROOT / "sql" / "verification" / "17_verify_v008_security.sql").read_text(encoding="utf-8")).fetchall()
                           if r[5] == "FAIL"}
            conn.execute(REAPPLY.read_text(encoding="utf-8"))
            conn.execute(REAPPLY.read_text(encoding="utf-8"))              # idempotent
            assert not conn.execute("SELECT has_database_privilege('public', current_database(), 'CONNECT')").fetchone()[0]
            assert _sql_result(conn, REPO_ROOT / "sql" / "verification" / "17_verify_v008_security.sql")[3] == 0
    finally:
        with _admin() as admin:
            _drop(admin, name)


# --------------------------------------------------------------------------- compatibility under the loader role
def test_every_pilot_loader_runs_as_the_loader_and_produces_the_same_data(empty_v008_db, pilot_dataset, inventory,
                                                                         pilot, plan):
    """Master data, opening inventory, 100 order units of work and 17 replenishment units, all as nordiccart_loader."""
    with _connect(empty_v008_db, LOADER) as conn:
        assert conn.execute("SELECT current_user").fetchone()[0] == LOADER
        master = apply_load(conn, pilot_dataset)
        stock = apply_inventory(conn, inventory)
        tx = apply_transactions(conn, pilot)
        repl = apply_replenishment(conn, plan)
        assert sum(master.inserted.values()) == 250 and master.change_log_rows > 0
        assert (stock.balances_inserted, stock.movements_inserted, stock.units_inserted) == (320, 288, 1460)
        assert tx.events_run == 100 and tx.by_kind["cancel_out_of_stock"] == 2 and tx.by_kind["expire"] == 1
        assert repl.events_run == 17 and repl.change_log_rows == 111
        # reruns write nothing, read-only plans see everything verified
        assert apply_transactions(conn, pilot).events_run == 0 and apply_replenishment(conn, plan).events_run == 0
        assert plan_load(conn, pilot_dataset).total_to_insert == 0 and plan_inventory(conn, inventory).total_to_insert == 0
        assert plan_transactions(conn, pilot).ok and plan_replenishment(conn, plan).ok
        loaded_as_loader = _content(conn)
    with _connect(TEMPLATE_V008) as conn:
        loaded_as_postgres = _content(conn)
    assert loaded_as_loader == loaded_as_postgres                         # identical business data and change_log rows


@pytest.mark.parametrize("script, expected", [
    ("01_verify_environment_and_migrations.sql", None), ("02_verify_tables_and_columns.sql", None),
    ("03_verify_keys_and_constraints.sql", None), ("04_verify_indexes_triggers_and_types.sql", None),
    ("05_verify_v006_tables_and_columns.sql", None), ("06_verify_v006_keys_and_constraints.sql", None),
    ("07_verify_v006_indexes_triggers_and_types.sql", None), ("08_verify_v007_tables_and_columns.sql", None),
    ("09_verify_v007_keys_and_constraints.sql", None), ("10_verify_v007_indexes_triggers_and_types.sql", None),
    ("11_verify_master_data.sql", (44, 33, 11, 0)), ("12_verify_opening_inventory.sql", (24, 17, 7, 0)),
    ("13_verify_pilot_transactions.sql", (52, 45, 7, 0)), ("14_verify_replenishment.sql", (44, 37, 7, 0)),
    ("15_verify_inventory_and_activity.sql", (43, 35, 8, 0)),
])
def test_verification_scripts_pass_as_the_analyst(v008_db, script, expected):
    with _connect(v008_db, ANALYST) as conn:
        result = _sql_result(conn, REPO_ROOT / "sql" / "verification" / script)
    assert result[3] == 0, f"{script}: {result}"
    if expected:
        assert result == expected


def test_customer_and_stock_activity_as_the_loader_on_the_real_state(v008_db, pilot, plan, inventory):
    """Later receipts, transfers and orders (reserve, pay, fulfil, open) written as nordiccart_loader."""
    from test_pilot_scope_db import _valid_later_activity
    with _connect(v008_db, LOADER) as conn:
        made = _valid_later_activity(conn, pilot, inventory)
        assert len(made["orders"]) == 4
        tx, repl = plan_transactions(conn, pilot), plan_replenishment(conn, plan)
        assert tx.ok and repl.ok and tx.later.orders == 4 and tx.later.open_orders == 1
    with _connect(v008_db, ANALYST) as conn:
        for script, expected in (("13_verify_pilot_transactions.sql", (52, 45, 7, 0)),
                                 ("14_verify_replenishment.sql", (44, 37, 7, 0)),
                                 ("15_verify_inventory_and_activity.sql", (43, 35, 8, 0))):
            assert _sql_result(conn, REPO_ROOT / "sql" / "verification" / script) == expected


def test_the_command_lines_run_as_the_loader(v008_db, monkeypatch, tmp_path):
    monkeypatch.setenv("PGDATABASE", v008_db)
    monkeypatch.setenv("PGUSER", LOADER)
    config = tmp_path / "generator.toml"
    config.write_text((REPO_ROOT / "config" / "generator.toml").read_text(encoding="utf-8")
                      .replace('log_dir = "logs"', f'log_dir = "{(tmp_path / "logs").as_posix()}"'), encoding="utf-8")
    base = ["--config", str(config), "--no-csv", "--env-file", str(tmp_path / "none.env")]
    for cli in (tx_cli, repl_cli):
        assert cli.main(base + ["--plan"]) == 0
        assert cli.main(base + ["--apply", "--yes"]) == 0                 # nothing to write, verified
    log = max((tmp_path / "logs").glob("transactions_pilot_plan_*.log")).read_text(encoding="utf-8")
    assert f"as {LOADER}" in log and "008" in log


def test_the_scheduler_has_the_writer_rights_and_its_own_limits(v008_db):
    with _connect(v008_db, SCHEDULER) as conn:
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "5min"
        assert _sqlstate(conn, "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, "
                               "occurred_at, external_reference) SELECT l.stock_location_id, p.product_id, 'purchase_receipt', 1, "
                               "'2024-12-09 08:00+00', 'PO-SE-2024-000002/DN1' FROM stock_locations l, products p "
                               "WHERE l.location_code = 'SE-WH-01' AND p.sku = 'ACC-000010'") == "00000"
        assert _sqlstate(conn, "UPDATE inventory_balances SET quantity_on_hand = 0") == "42501"
    conns = [_connect(v008_db, SCHEDULER) for _ in range(3)]
    try:
        with pytest.raises(psycopg.OperationalError, match="too many connections for role"):
            _connect(v008_db, SCHEDULER)
    finally:
        for c in conns:
            c.close()


def test_the_analyst_reads_and_is_read_only(v008_db):
    with _connect(v008_db, ANALYST) as conn:
        assert conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 20
        assert _sqlstate(conn, "INSERT INTO currencies (currency_code, currency_name) VALUES ('XTU', 'x')") == "25006"
        conn.execute("SET default_transaction_read_only = off")          # the default is a convenience...
        assert _sqlstate(conn, "INSERT INTO currencies (currency_code, currency_name) VALUES ('XTU', 'x')") == "42501"
        assert _sqlstate(conn, "SELECT fn_lock_order_balances(1)") == "42501"     # ...the privileges are the control


def test_the_extractor_reads_purges_and_checkpoints_change_log_only(v008_db):
    with _connect(v008_db, EXTRACTOR) as conn:
        assert conn.execute("SELECT count(*) FROM change_log").fetchone()[0] == 1537
        assert _sqlstate(conn, "DELETE FROM change_log WHERE change_id = (SELECT min(change_id) FROM change_log)") == "00000"
        assert _sqlstate(conn, "UPDATE change_log SET table_name = table_name WHERE false") == "42501"
        assert _sqlstate(conn, "TRUNCATE change_log") == "42501"
        assert _sqlstate(conn, "INSERT INTO change_log_checkpoints (consumer_name, last_snapshot) "
                               "VALUES ('nc_test', pg_current_snapshot())") == "00000"
        assert _sqlstate(conn, "SELECT count(*) FROM orders") == "42501"
        assert conn.execute("SELECT count(*) FROM change_log").fetchone()[0] == 1537       # the purge was rolled back


# --------------------------------------------------------------------------- security (real logins)
REFUSED_FOR_THE_LOADER = {
    "update on-hand quantity": "UPDATE inventory_balances SET quantity_on_hand = quantity_on_hand + 1",
    "update reserved quantity": "UPDATE inventory_balances SET quantity_reserved = 0",
    "update order snapshot": "UPDATE orders SET order_total = 1",
    "update order lines": "UPDATE order_items SET quantity = 1",
    "update ledger": "UPDATE inventory_movements SET quantity_change = 0",
    "update payment amount": "UPDATE payment_attempts SET amount = 0",
    "update customer registration": "UPDATE customers SET registered_at = now()",
    "update VAT rate": "UPDATE countries SET vat_rate = 0",
    "delete orders": "DELETE FROM orders",
    "delete movements": "DELETE FROM inventory_movements",
    "delete customers": "DELETE FROM customers",
    "truncate ledger": "TRUNCATE inventory_movements",
    "forge change_log": "INSERT INTO change_log (table_name, record_key, operation, row_data) VALUES ('orders','1','insert','{}')",
    "read change contents": "SELECT row_data FROM change_log LIMIT 1",
    "purge change_log": "DELETE FROM change_log",
    "write migrations": "INSERT INTO meta.schema_migrations (version, description, script_name) VALUES ('999','x','x')",
    "create table": "CREATE TABLE public.nc_x (x integer)",
    "create temp table": "CREATE TEMP TABLE nc_x (x integer)",
    "create temp function (search_path trap)": "CREATE FUNCTION pg_temp.lower(text) RETURNS text LANGUAGE sql AS 'SELECT 1::text'",
    "create schema": "CREATE SCHEMA nc_x",
    "disable triggers": "ALTER TABLE inventory_balances DISABLE TRIGGER ALL",
    "create trigger": "CREATE TRIGGER nc_x BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at()",
    "take ownership": "ALTER TABLE orders OWNER TO nordiccart_loader",
    "drop table": "DROP TABLE change_log",
    "become the owner": "SET ROLE nordiccart_owner",
    "become the writer group": "SET ROLE nordiccart_writer",
    "skip all triggers": "SET session_replication_role = replica",
    "call capture trigger": "SELECT fn_capture_change()",
    "call balance trigger": "SELECT fn_inventory_apply_reservation()",
    "call migration guard": "SELECT meta.assert_migration_can_run('999', NULL)",
    "exclusive lock": "LOCK TABLE inventory_balances IN ACCESS EXCLUSIVE MODE",
    "read server file": "SELECT pg_read_file('postgresql.conf')",
    "write server file": "COPY (SELECT 1) TO '/nc_never_written.txt'",
    "create role": "CREATE ROLE nc_x",
}


@pytest.mark.parametrize("attempt", sorted(REFUSED_FOR_THE_LOADER))
def test_the_loader_is_refused(v008_db, attempt):
    with _connect(v008_db, LOADER) as conn:
        state = _sqlstate(conn, REFUSED_FOR_THE_LOADER[attempt])
    assert state == "42501", f"{attempt}: {state}"


def test_the_loader_cannot_end_another_roles_session(v008_db):
    with _connect(v008_db) as admin, _connect(v008_db, LOADER) as conn:
        pid = admin.execute("SELECT pg_backend_pid()").fetchone()[0]
        assert _sqlstate(conn, "SELECT pg_terminate_backend(%s)", (pid,)) == "42501"
        assert admin.execute("SELECT 1").fetchone()[0] == 1               # still alive


def test_a_hostile_search_path_does_not_divert_the_security_definer_functions(v008_db):
    """A trap schema first in the caller's search_path: the SECURITY DEFINER triggers ignore it."""
    with _connect(v008_db) as admin:                     # an object the loader could only see, never create
        admin.execute("CREATE SCHEMA nc_trap")
        admin.execute("CREATE TABLE nc_trap.inventory_balances (x integer)")
        admin.execute("CREATE TABLE nc_trap.change_log (x integer)")
        admin.execute("GRANT USAGE ON SCHEMA nc_trap TO nordiccart_loader")
        admin.execute("GRANT SELECT, INSERT, UPDATE ON nc_trap.inventory_balances, nc_trap.change_log TO nordiccart_loader")
        before = admin.execute("SELECT b.quantity_on_hand FROM inventory_balances b JOIN stock_locations l USING (stock_location_id) "
                               "JOIN products p USING (product_id) WHERE l.location_code = 'SE-WH-01' AND p.sku = 'ACC-000010'"
                               ).fetchone()[0]
        changes = admin.execute("SELECT count(*) FROM public.change_log").fetchone()[0]
    with _connect(v008_db, LOADER) as conn:
        conn.execute("SET search_path TO nc_trap, pg_temp, public")
        conn.execute("INSERT INTO public.inventory_movements (stock_location_id, product_id, movement_type, quantity_change, "
                     "occurred_at, external_reference) SELECT l.stock_location_id, p.product_id, 'purchase_receipt', 4, "
                     "'2024-12-09 08:00+00', 'PO-SE-2024-000002/DN1' FROM public.stock_locations l, public.products p "
                     "WHERE l.location_code = 'SE-WH-01' AND p.sku = 'ACC-000010'")
    with _connect(v008_db) as admin:
        after = admin.execute("SELECT b.quantity_on_hand FROM inventory_balances b JOIN stock_locations l USING (stock_location_id) "
                              "JOIN products p USING (product_id) WHERE l.location_code = 'SE-WH-01' AND p.sku = 'ACC-000010'"
                              ).fetchone()[0]
        assert after == before + 4
        assert admin.execute("SELECT count(*) FROM public.change_log").fetchone()[0] == changes + 2
        assert admin.execute("SELECT count(*) FROM nc_trap.inventory_balances").fetchone()[0] == 0
        assert admin.execute("SELECT count(*) FROM nc_trap.change_log").fetchone()[0] == 0


def test_the_existing_guards_still_bind_the_owner(v008_db):
    """V008 weakens no constraint: even the owner role meets the V002, V006 and V007 guards."""
    with _connect(v008_db) as conn:
        conn.execute("SET ROLE nordiccart_owner")
        assert _sqlstate(conn, "UPDATE inventory_balances SET quantity_on_hand = quantity_on_hand + 1") == "23514"
        assert _sqlstate(conn, "DELETE FROM orders") == "23514"
        assert _sqlstate(conn, "TRUNCATE inventory_movements") == "23000"
        assert _sqlstate(conn, "UPDATE change_log SET table_name = table_name") == "23000"
        assert _sqlstate(conn, "UPDATE order_items SET quantity = quantity + 1") == "23514"


def test_known_limit_a_role_can_change_its_own_timeout_defaults(v008_db):
    """PostgreSQL lets a role change its own session settings and defaults: timeouts guard against
    accidents, not against a misbehaving role (reported as an open item)."""
    with _connect(v008_db, LOADER) as conn:
        conn.execute("SET statement_timeout = 0")
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "0"
        conn.execute("ALTER ROLE nordiccart_loader SET statement_timeout = '31min'")
    with _connect(v008_db) as admin:
        assert "statement_timeout=31min" in admin.execute(
            "SELECT array_to_string(rolconfig, ',') FROM pg_roles WHERE rolname = 'nordiccart_loader'").fetchone()[0]
        admin.execute("ALTER ROLE nordiccart_loader SET statement_timeout = '30min'")       # restore V008's value


# --------------------------------------------------------------------------- rollback
def test_rollback_restores_the_pre_v008_catalog_and_v008_reapplies(v007_db):
    with _connect(v007_db) as conn:
        before_catalog, before_content = _catalog(conn), _content(conn)
        conn.execute(V008.read_text(encoding="utf-8"))
        assert _catalog(conn) != before_catalog
        conn.execute(R008.read_text(encoding="utf-8"))
        assert _catalog(conn) == before_catalog                           # owners, ACLs, functions, migrations
        assert _content(conn) == before_content
        conn.execute(V008.read_text(encoding="utf-8"))                    # can be applied again
        assert conn.execute("SELECT count(*) FROM meta.schema_migrations WHERE version = '008'").fetchone()[0] == 1


def test_rollback_refuses_when_v008_is_not_applied(v007_db):
    with _connect(v007_db) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="not applied"):
            conn.execute(R008.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- SQL suites and concurrency
@pytest.mark.parametrize("suite, tests", [("test_foundation_constraints.sql", 44), ("test_v006_orders_and_payments.sql", 119),
                                          ("test_v007_inventory.sql", 105), ("test_v008_roles.sql", 87)])
def test_the_sql_behaviour_suites_pass_on_v008(v008_db, suite, tests):
    with _connect(v008_db) as conn:
        cur = conn.execute((REPO_ROOT / "tests" / "sql" / suite).read_text(encoding="utf-8"))
        rows = None
        while True:                                     # the result table is the script's last result set
            if cur.description is not None:
                rows = cur.fetchall()
            if not cur.nextset():
                break
    failed = [r[:5] for r in rows if r[5] != "PASS"]
    assert len(rows) == tests and not failed, failed[:5]


def test_the_concurrency_scenarios_pass_as_the_loader(templates):
    """The 14 multi-session scenarios of test_v007_concurrency.py on V001-V008, sessions as nordiccart_loader."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" / "python" / "test_v007_concurrency.py"), "--recreate", "--through", "8",
         "--role", LOADER], capture_output=True, text=True, timeout=600, env=os.environ.copy())
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "Scenarios: 14  PASS: 14  FAIL: 0" in result.stdout
    assert f"run as {LOADER}" in result.stdout
