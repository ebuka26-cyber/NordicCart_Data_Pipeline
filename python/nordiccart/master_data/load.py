"""Idempotent loading of the master dataset into the NordicCart OLTP database.

How a run stays safe to repeat
  * Every row is matched on its business key (currency_code, country_code,
    category_code, sku, sku + market + valid_from, location_code, customer_number).
    Surrogate ids are never sent; foreign keys are resolved through business keys.
  * A row that exists with the same values is left alone. A missing row is inserted.
    A row that exists with different values is a CONFLICT: the run stops before
    writing anything and lists the differences. Nothing is ever updated or deleted.
  * Columns that later business activity may change (prices closed by valid_to,
    products discontinued, customers moving or deactivated) are written on insert
    only. Differences there are reported as drift, never as conflicts.
  * The write runs in ONE transaction under an advisory lock, re-plans inside it and
    verifies every row before COMMIT. Any error rolls everything back.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

import psycopg
from psycopg.rows import dict_row

from .model import MasterDataset
from .schema import COLUMNS, NUMERIC, REQUIRED_MIGRATIONS

log = logging.getLogger("nordiccart.master_data.load")

ADVISORY_LOCK_KEY = "nordiccart.master_data_seed"


class PreconditionError(RuntimeError):
    """The database is not ready for the seeder (wrong database, missing migration, schema drift)."""


class ConflictError(RuntimeError):
    def __init__(self, plan: "LoadPlan"):
        super().__init__(f"{plan.conflict_count} conflicting rows; nothing was written.")
        self.plan = plan


class PlanChangedError(RuntimeError):
    """The database changed between the confirmed plan and the write."""


class VerificationError(RuntimeError):
    """After the inserts, the database did not hold exactly the dataset (rolled back)."""


@dataclass(frozen=True)
class TableSpec:
    name: str
    key: tuple[str, ...]
    compare: tuple[str, ...]         # must match on a rerun, else conflict
    lifecycle: tuple[str, ...]       # written on insert; later changes are drift only
    select_sql: str
    insert_sql: str
    insert_params: tuple[str, ...]   # dataset attributes in the order of the %s placeholders


SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        "currencies", ("currency_code",), ("currency_name", "minor_units"), (),
        "SELECT currency_code, currency_name, minor_units FROM currencies",
        "INSERT INTO currencies (currency_code, currency_name, minor_units) VALUES (%s, %s, %s)",
        ("currency_code", "currency_name", "minor_units"),
    ),
    TableSpec(
        "countries", ("country_code",), ("country_name", "currency_code", "vat_rate", "time_zone"), (),
        "SELECT country_code, country_name, currency_code, vat_rate, time_zone FROM countries",
        "INSERT INTO countries (country_code, country_name, currency_code, vat_rate, time_zone) VALUES (%s, %s, %s, %s, %s)",
        ("country_code", "country_name", "currency_code", "vat_rate", "time_zone"),
    ),
    TableSpec(
        "product_categories", ("category_code",), ("category_name",), ("is_active",),
        "SELECT category_code, category_name, is_active FROM product_categories",
        "INSERT INTO product_categories (category_code, category_name, is_active) VALUES (%s, %s, %s)",
        ("category_code", "category_name", "is_active"),
    ),
    TableSpec(
        "products", ("sku",), ("product_name", "brand", "category_code", "launched_on"), ("discontinued_on", "is_active"),
        "SELECT p.sku, p.product_name, p.brand, c.category_code, p.launched_on, p.discontinued_on, p.is_active "
        "FROM products p JOIN product_categories c ON c.category_id = p.category_id",
        "INSERT INTO products (sku, product_name, brand, category_id, launched_on, discontinued_on, is_active) "
        "SELECT %s, %s, %s, c.category_id, %s, %s, %s FROM product_categories c WHERE c.category_code = %s",
        ("sku", "product_name", "brand", "launched_on", "discontinued_on", "is_active", "category_code"),
    ),
    TableSpec(
        "product_prices", ("sku", "country_code", "valid_from"), ("currency_code", "list_price", "unit_cost"), ("valid_to",),
        "SELECT p.sku, pp.country_code, pp.valid_from, pp.currency_code, pp.list_price, pp.unit_cost, pp.valid_to "
        "FROM product_prices pp JOIN products p ON p.product_id = pp.product_id",
        "INSERT INTO product_prices (product_id, country_code, currency_code, list_price, unit_cost, valid_from, valid_to) "
        "SELECT p.product_id, %s, %s, %s, %s, %s, %s FROM products p WHERE p.sku = %s",
        ("country_code", "currency_code", "list_price", "unit_cost", "valid_from", "valid_to", "sku"),
    ),
    TableSpec(
        "stock_locations", ("location_code",),
        ("location_name", "location_type", "country_code", "address_line1", "postal_code", "city", "opened_on"), ("is_active",),
        "SELECT location_code, location_name, location_type::text AS location_type, country_code, address_line1, "
        "postal_code, city, opened_on, is_active FROM stock_locations",
        "INSERT INTO stock_locations (location_code, location_name, location_type, country_code, address_line1, "
        "postal_code, city, opened_on, is_active) VALUES (%s, %s, %s::stock_location_type, %s, %s, %s, %s, %s, %s)",
        ("location_code", "location_name", "location_type", "country_code", "address_line1", "postal_code", "city",
         "opened_on", "is_active"),
    ),
    TableSpec(
        "stores", ("location_code",), ("store_format",), (),
        "SELECT l.location_code, s.store_format FROM stores s JOIN stock_locations l ON l.stock_location_id = s.stock_location_id",
        "INSERT INTO stores (stock_location_id, store_format) "
        "SELECT l.stock_location_id, %s FROM stock_locations l WHERE l.location_code = %s AND l.location_type = 'store'",
        ("store_format", "location_code"),
    ),
    TableSpec(
        "warehouses", ("location_code",), ("ships_online_orders", "storage_capacity_units"), (),
        "SELECT l.location_code, w.ships_online_orders, w.storage_capacity_units "
        "FROM warehouses w JOIN stock_locations l ON l.stock_location_id = w.stock_location_id",
        "INSERT INTO warehouses (stock_location_id, ships_online_orders, storage_capacity_units) "
        "SELECT l.stock_location_id, %s, %s FROM stock_locations l WHERE l.location_code = %s AND l.location_type = 'warehouse'",
        ("ships_online_orders", "storage_capacity_units", "location_code"),
    ),
    TableSpec(
        "customers", ("customer_number",),
        ("first_name", "last_name", "birth_date", "country_code", "signup_channel", "registered_at"),
        ("email", "phone", "address_line1", "postal_code", "city", "is_active"),
        "SELECT customer_number, first_name, last_name, email, phone, birth_date, address_line1, postal_code, city, "
        "country_code, signup_channel::text AS signup_channel, registered_at, is_active FROM customers",
        "INSERT INTO customers (customer_number, first_name, last_name, email, phone, birth_date, address_line1, "
        "postal_code, city, country_code, signup_channel, registered_at, is_active) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::sales_channel, %s, %s)",
        ("customer_number", "first_name", "last_name", "email", "phone", "birth_date", "address_line1", "postal_code",
         "city", "country_code", "signup_channel", "registered_at", "is_active"),
    ),
)
SPEC_BY_NAME = {s.name: s for s in SPECS}


@dataclass
class TablePlan:
    name: str
    in_dataset: int = 0
    matching: int = 0
    to_insert: list = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)
    extra_in_db: int = 0


@dataclass
class LoadPlan:
    tables: dict[str, TablePlan]

    @property
    def total_to_insert(self) -> int:
        return sum(len(t.to_insert) for t in self.tables.values())

    @property
    def conflict_count(self) -> int:
        return sum(len(t.conflicts) for t in self.tables.values())

    def insert_counts(self) -> dict[str, int]:
        return {name: len(t.to_insert) for name, t in self.tables.items()}

    def lines(self) -> list[str]:
        head = f"{'table':<20}{'dataset':>9}{'present':>9}{'insert':>8}{'conflict':>10}{'drift':>7}{'extra':>7}"
        out = [head, "-" * len(head)]
        for t in self.tables.values():
            out.append(f"{t.name:<20}{t.in_dataset:>9}{t.matching:>9}{len(t.to_insert):>8}{len(t.conflicts):>10}"
                       f"{len(t.drift):>7}{t.extra_in_db:>7}")
        return out


@dataclass
class ApplyResult:
    inserted: dict[str, int]
    change_log_rows: int
    verified_rows: int


# --------------------------------------------------------------------------- connection
def connect() -> psycopg.Connection:
    """Connect with the standard PostgreSQL environment variables (PGHOST, PGDATABASE, ...)."""
    conn = psycopg.connect(autocommit=True, application_name="nordiccart_master_data_seed", connect_timeout=10)
    conn.execute("SET search_path TO public")
    return conn


def check_database(conn: psycopg.Connection) -> dict:
    """Stop early when the database is not the migrated NordicCart OLTP database."""
    db, user, version, version_num = conn.execute(
        "SELECT current_database(), current_user, current_setting('server_version'), "
        "current_setting('server_version_num')::int").fetchone()
    info = {"database": db, "user": user, "server_version": version}
    if version_num < 130000:
        raise PreconditionError(f"PostgreSQL {version} is too old; NordicCart needs 13 or later.")
    if conn.execute("SELECT to_regclass('meta.schema_migrations')").fetchone()[0] is None:
        raise PreconditionError(
            f"Database {db!r} has no meta.schema_migrations table. Is PGDATABASE set to your NordicCart database?")
    versions = [r[0] for r in conn.execute("SELECT version FROM meta.schema_migrations ORDER BY version")]
    info["migrations"] = versions
    missing = [v for v in REQUIRED_MIGRATIONS if v not in versions]
    if missing:
        raise PreconditionError(f"Database {db!r} is missing migrations {', '.join(missing)}; apply V001 to V007 first.")

    rows = conn.execute(
        "SELECT table_name, column_name, data_type, character_maximum_length, is_nullable, numeric_precision, numeric_scale "
        "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (list(COLUMNS),)).fetchall()
    actual = {(r[0], r[1]): r[2:] for r in rows}
    problems = []
    for table, cols in COLUMNS.items():
        for column, (data_type, max_len, nullable) in cols.items():
            got = actual.get((table, column))
            if got is None:
                problems.append(f"{table}.{column} is missing")
                continue
            g_type, g_len, g_null, g_prec, g_scale = got
            if g_type != data_type or g_len != max_len or (g_null == "YES") != nullable:
                problems.append(f"{table}.{column} is {g_type}({g_len}) nullable={g_null}, expected "
                                f"{data_type}({max_len}) nullable={'YES' if nullable else 'NO'}")
            if (table, column) in NUMERIC and (g_prec, g_scale) != NUMERIC[(table, column)]:
                problems.append(f"{table}.{column} is numeric({g_prec},{g_scale}), expected numeric{NUMERIC[(table, column)]}")
    if problems:
        raise PreconditionError("The master data tables differ from migrations V003 to V005: " + "; ".join(problems))
    return info


# --------------------------------------------------------------------------- planning
def _row_values(row) -> dict:
    return asdict(row)


def _fetch_existing(conn: psycopg.Connection, spec: TableSpec) -> dict[tuple, dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(spec.select_sql)
        return {tuple(r[k] for k in spec.key): r for r in cur.fetchall()}


def _differences(columns: tuple[str, ...], wanted: dict, found: dict) -> list[str]:
    return [f"{c}: database={found[c]!r} generated={wanted[c]!r}" for c in columns if found[c] != wanted[c]]


def build_plan(conn: psycopg.Connection, dataset: MasterDataset) -> LoadPlan:
    tables = dataset.tables()
    plan = LoadPlan(tables={})
    existing_all: dict[str, dict] = {}
    for spec in SPECS:
        tp = TablePlan(spec.name, in_dataset=len(tables[spec.name]))
        existing = _fetch_existing(conn, spec)
        existing_all[spec.name] = existing
        keys_in_dataset = set()
        for row in tables[spec.name]:
            values = _row_values(row)
            key = tuple(values[k] for k in spec.key)
            keys_in_dataset.add(key)
            found = existing.get(key)
            if found is None:
                tp.to_insert.append(row)
                continue
            diff = _differences(spec.compare, values, found)
            if diff:
                tp.conflicts.append(f"{'/'.join(map(str, key))}: " + "; ".join(diff))
                continue
            tp.matching += 1
            drift = _differences(spec.lifecycle, values, found)
            if drift:
                tp.drift.append(f"{'/'.join(map(str, key))}: " + "; ".join(drift))
        tp.extra_in_db = sum(1 for k in existing if k not in keys_in_dataset)
        plan.tables[spec.name] = tp

    _secondary_conflicts(plan, existing_all)
    return plan


def _secondary_conflicts(plan: LoadPlan, existing: dict[str, dict]) -> None:
    """Rows to insert whose other unique values are already used by a different row."""
    def check(table: str, column: str, case_insensitive: bool = False) -> None:
        spec = SPEC_BY_NAME[table]
        norm = (lambda v: v.lower()) if case_insensitive else (lambda v: v)
        used = {norm(r[column]): k for k, r in existing[table].items()}
        for row in plan.tables[table].to_insert:
            values = _row_values(row)
            owner = used.get(norm(values[column]))
            if owner is not None:
                plan.tables[table].conflicts.append(
                    f"{'/'.join(str(values[k]) for k in spec.key)}: {column} {values[column]!r} already belongs to "
                    f"{'/'.join(map(str, owner))}")

    check("countries", "country_name")
    check("product_categories", "category_name")
    check("customers", "email", case_insensitive=True)
    # A new opening price must not overlap a price history the database already has.
    priced = {(k[0], k[1]) for k in existing["product_prices"]}
    for row in plan.tables["product_prices"].to_insert:
        if (row.sku, row.country_code) in priced:
            plan.tables["product_prices"].conflicts.append(
                f"{row.sku}/{row.country_code}: the database already has other prices for this product and market")


def plan_load(conn: psycopg.Connection, dataset: MasterDataset) -> LoadPlan:
    """Read-only comparison of the dataset with the database."""
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return build_plan(conn, dataset)


# --------------------------------------------------------------------------- writing
def _verify(conn: psycopg.Connection, dataset: MasterDataset, inserted: dict[str, set]) -> tuple[int, list[str]]:
    """Every dataset row must now exist; rows inserted in this run must match in every column."""
    problems: list[str] = []
    checked = 0
    tables = dataset.tables()
    for spec in SPECS:
        existing = _fetch_existing(conn, spec)
        for row in tables[spec.name]:
            values = _row_values(row)
            key = tuple(values[k] for k in spec.key)
            found = existing.get(key)
            if found is None:
                problems.append(f"{spec.name} {key} is missing after the insert")
                continue
            columns = spec.compare + (spec.lifecycle if key in inserted[spec.name] else ())
            diff = _differences(columns, values, found)
            if diff:
                problems.append(f"{spec.name} {key}: " + "; ".join(diff))
            checked += 1
    return checked, problems


def apply_load(conn: psycopg.Connection, dataset: MasterDataset, confirmed: LoadPlan | None = None) -> ApplyResult:
    """Insert the missing rows in one transaction, verify, then commit."""
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout = '10s'")
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (ADVISORY_LOCK_KEY,))
        plan = build_plan(conn, dataset)        # again, now under the lock
        if plan.conflict_count:
            raise ConflictError(plan)
        if confirmed is not None and plan.insert_counts() != confirmed.insert_counts():
            raise PlanChangedError("The database changed after the plan was shown; run again to see the new plan.")

        inserted: dict[str, set] = {}
        with conn.cursor() as cur:
            for spec in SPECS:
                rows = plan.tables[spec.name].to_insert
                inserted[spec.name] = {tuple(_row_values(r)[k] for k in spec.key) for r in rows}
                if rows:
                    cur.executemany(spec.insert_sql, [tuple(_row_values(r)[c] for c in spec.insert_params) for r in rows])
                    log.info("inserted %5d rows into %s", len(rows), spec.name)

        checked, problems = _verify(conn, dataset, inserted)
        if problems:
            raise VerificationError(f"{len(problems)} rows differ after the insert: " + "; ".join(problems[:10]))
        change_log_rows = conn.execute(
            "SELECT count(*) FROM change_log WHERE transaction_id = pg_current_xact_id()").fetchone()[0]
    # COMMIT happened here; the deferred stock location subtype check passed.
    return ApplyResult(inserted={k: len(v) for k, v in inserted.items()}, change_log_rows=change_log_rows,
                       verified_rows=checked)


def verify_only(conn: psycopg.Connection, dataset: MasterDataset) -> tuple[int, list[str]]:
    """Read-only check that the database holds the dataset (used when nothing is missing)."""
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return _verify(conn, dataset, {s.name: set() for s in SPECS})
