"""Validation of a generated master dataset before anything is written.

Three groups of rules:
  1. Schema rules: NOT NULL, text lengths, numeric precision, enum values, every
     named CHECK of the master tables, unique keys and foreign keys (V003 to V005).
  2. Rules later migrations rely on (V006, V007): every market has a warehouse that
     ships online orders, prices are in the market currency, VAT rates are valid.
  3. Data quality rules the schema cannot express: lower-case unique emails, one open
     price per product and market, positive margins, adult customers, in-store
     sign-ups only where a store was already open, consistent formats.
Each problem becomes an Issue; an empty list means the dataset is safe to load.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..settings import Settings
from .model import MasterDataset
from .reference import Reference, load_reference
from .rules import ONE, age_on, local_midnight_utc, net_of_vat
from .schema import COLUMNS, ENUM_VALUES, NUMERIC, STORE_FORMATS


@dataclass(frozen=True)
class Issue:
    table: str
    key: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.table} [{self.key}] {self.rule}: {self.detail}"


KEYS = {
    "currencies": ("currency_code",),
    "countries": ("country_code",),
    "product_categories": ("category_code",),
    "products": ("sku",),
    "product_prices": ("sku", "country_code", "valid_from"),
    "stock_locations": ("location_code",),
    "stores": ("location_code",),
    "warehouses": ("location_code",),
    "customers": ("customer_number",),
}

PATTERNS = {
    "customer_number": re.compile(r"^C\d{8}$"),
    "sku": re.compile(r"^[A-Z]{3}-\d{6}$"),
    "location_code": re.compile(r"^(SE|NO|DK|FI)-(STO|WH)-\d{2}$"),
    "email": re.compile(r"^[a-z0-9][a-z0-9._%+-]*@[a-z0-9-]+(\.[a-z0-9-]+)+$"),
}
POSTAL_PATTERNS = {"NNN NN": re.compile(r"^\d{3} \d{2}$"), "NNNN": re.compile(r"^\d{4}$"), "NNNNN": re.compile(r"^\d{5}$")}


def _key(table: str, row) -> str:
    values = asdict(row)
    return "/".join(str(values[k]) for k in KEYS[table])


def validate_master_data(dataset: MasterDataset, settings: Settings, reference: Reference | None = None) -> list[Issue]:
    ref = reference or load_reference()
    issues: list[Issue] = []

    def add(table: str, row, rule: str, detail: str) -> None:
        issues.append(Issue(table, _key(table, row) if row is not None else "-", rule, detail))

    tables = dataset.tables()

    # ---------------------------------------------------------------- 1. counts
    expected = {
        "currencies": len(ref.currencies), "countries": len(ref.markets),
        "product_categories": len(ref.categories), "products": settings.products,
        "product_prices": settings.products * len(ref.markets),
        "stock_locations": len(ref.stores) + len(ref.warehouses),
        "stores": len(ref.stores), "warehouses": len(ref.warehouses), "customers": settings.customers,
    }
    for table, count in expected.items():
        if len(tables[table]) != count:
            add(table, None, "row_count", f"expected {count} rows, generated {len(tables[table])}")

    # ---------------------------------------------------------------- 2. columns
    for table, rows in tables.items():
        spec = COLUMNS[table]
        for row in rows:
            for column, value in asdict(row).items():
                if column not in spec:
                    continue                       # business-key helper columns (sku, location_code, ...)
                data_type, max_len, nullable = spec[column]
                if value is None:
                    if not nullable:
                        add(table, row, "not_null", f"{column} is NULL")
                    continue
                if max_len is not None and len(str(value)) > max_len:
                    add(table, row, "length", f"{column} has {len(str(value))} characters, limit {max_len}")
                if data_type == "character" and len(str(value)) != max_len:
                    add(table, row, "fixed_length", f"{column}={value!r} must have exactly {max_len} characters")
                if isinstance(value, str) and value != value.strip():
                    add(table, row, "whitespace", f"{column} has leading or trailing spaces")
                if (table, column) in NUMERIC:
                    precision, scale = NUMERIC[(table, column)]
                    if not isinstance(value, Decimal) or value.as_tuple().exponent < -scale or \
                            abs(value) >= Decimal(10) ** (precision - scale):
                        add(table, row, "numeric", f"{column}={value} does not fit numeric({precision},{scale})")
                if (table, column) in ENUM_VALUES and value not in ENUM_VALUES[(table, column)]:
                    add(table, row, "enum", f"{column}={value!r} is not one of {ENUM_VALUES[(table, column)]}")
                if data_type == "timestamp with time zone" and value.tzinfo is None:
                    add(table, row, "timezone", f"{column} has no time zone")

    # ---------------------------------------------------------------- 3. unique keys
    def unique(table: str, label: str, getter) -> None:
        seen: dict = {}
        for row in tables[table]:
            value = getter(row)
            if value in seen:
                add(table, row, f"unique_{label}", f"{label}={value!r} repeats")
            seen[value] = True

    for table, cols in KEYS.items():
        unique(table, "+".join(cols), lambda r, cols=cols: tuple(asdict(r)[c] for c in cols))
    unique("countries", "country_name", lambda r: r.country_name)
    unique("product_categories", "category_name", lambda r: r.category_name)
    unique("customers", "email_case_insensitive", lambda r: r.email.lower())

    # ---------------------------------------------------------------- 4. foreign keys
    currency_codes = {c.currency_code for c in dataset.currencies}
    countries = {c.country_code: c for c in dataset.countries}
    categories = {c.category_code for c in dataset.product_categories}
    products = {p.sku: p for p in dataset.products}
    locations = {l.location_code: l for l in dataset.stock_locations}
    for c in dataset.countries:
        if c.currency_code not in currency_codes:
            add("countries", c, "fk_countries_currency", f"currency {c.currency_code} missing")
    for p in dataset.products:
        if p.category_code not in categories:
            add("products", p, "fk_products_category", f"category {p.category_code} missing")
    for pp in dataset.product_prices:
        if pp.sku not in products:
            add("product_prices", pp, "fk_product_prices_product", f"product {pp.sku} missing")
        if pp.country_code not in countries:
            add("product_prices", pp, "fk_product_prices_country", f"country {pp.country_code} missing")
        if pp.currency_code not in currency_codes:
            add("product_prices", pp, "fk_product_prices_currency", f"currency {pp.currency_code} missing")
    for l in dataset.stock_locations:
        if l.country_code not in countries:
            add("stock_locations", l, "fk_stock_locations_country", f"country {l.country_code} missing")
    for c in dataset.customers:
        if c.country_code not in countries:
            add("customers", c, "fk_customers_country", f"country {c.country_code} missing")

    # ---------------------------------------------------------------- 5. named CHECKs and subtypes
    for c in dataset.currencies:
        if c.minor_units < 0:
            add("currencies", c, "minor_units", "minor_units must not be negative")
    valid_zone: set[str] = set()
    for c in dataset.countries:
        if not (Decimal(0) <= c.vat_rate < ONE):
            add("countries", c, "ck_countries_vat_rate", f"vat_rate {c.vat_rate} outside [0, 1)")
        try:
            ZoneInfo(c.time_zone)
            valid_zone.add(c.country_code)
        except Exception:  # noqa: BLE001
            add("countries", c, "time_zone", f"{c.time_zone!r} is not an IANA time zone")
    for p in dataset.products:
        if p.discontinued_on and p.launched_on and p.discontinued_on < p.launched_on:
            add("products", p, "ck_products_dates", "discontinued before launch")
        if not PATTERNS["sku"].match(p.sku):
            add("products", p, "sku_format", f"{p.sku!r} is not like SPH-000001")
        if p.launched_on and p.launched_on > settings.simulation_end:
            add("products", p, "launch_date", "launched after the last simulated day")
    for pp in dataset.product_prices:
        if pp.list_price <= 0:
            add("product_prices", pp, "ck_product_prices_list_price", "list_price must be > 0")
        if pp.unit_cost < 0:
            add("product_prices", pp, "ck_product_prices_unit_cost", "unit_cost must be >= 0")
        if pp.valid_to is not None and pp.valid_to <= pp.valid_from:
            add("product_prices", pp, "ck_product_prices_validity", "valid_to must be after valid_from")
    store_codes = {s.location_code for s in dataset.stores}
    warehouse_codes = {w.location_code for w in dataset.warehouses}
    for l in dataset.stock_locations:
        subtypes = (l.location_code in store_codes) + (l.location_code in warehouse_codes)
        expected_type = "store" if l.location_code in store_codes else "warehouse"
        if subtypes != 1 or l.location_type != expected_type:
            add("stock_locations", l, "stock_locations_subtype", "needs exactly one matching store or warehouse row")
        m = PATTERNS["location_code"].match(l.location_code)
        if not m or m.group(1) != l.country_code or (m.group(2) == "STO") != (l.location_type == "store"):
            add("stock_locations", l, "location_code_format", "code must be <country>-STO-nn or <country>-WH-nn")
        if l.opened_on > settings.simulation_end:
            add("stock_locations", l, "opened_on", "opens after the last simulated day")
    for s in dataset.stores:
        if s.location_code not in locations:
            add("stores", s, "fk_stores_stock_location", "stock location missing")
        if s.store_format not in STORE_FORMATS:
            add("stores", s, "store_format", f"{s.store_format!r} is not flagship or standard")
    for w in dataset.warehouses:
        if w.location_code not in locations:
            add("warehouses", w, "fk_warehouses_stock_location", "stock location missing")
        if w.storage_capacity_units is not None and w.storage_capacity_units <= 0:
            add("warehouses", w, "ck_warehouses_capacity", "capacity must be > 0")

    # ---------------------------------------------------------------- 6. rules later migrations rely on
    for code, market in ((m.country_code, m) for m in ref.markets):
        online = [w for w in dataset.warehouses
                  if w.ships_online_orders and locations.get(w.location_code) and locations[w.location_code].country_code == code]
        if not online:
            add("countries", countries.get(code), "online_fulfilment", "no warehouse ships online orders in this market (V006)")
        else:
            first_open = min(locations[w.location_code].opened_on for w in online)
            if first_open > market.market_launched_on:
                add("countries", countries.get(code), "online_fulfilment",
                    f"market launches {market.market_launched_on} but its warehouse opens {first_open}")
        if not any(l.country_code == code and l.location_type == "store" for l in dataset.stock_locations):
            add("countries", countries.get(code), "stores", "no store in this market")

    open_prices: dict[tuple[str, str], int] = {}
    for pp in dataset.product_prices:
        country = countries.get(pp.country_code)
        product = products.get(pp.sku)
        if country is None or product is None:
            continue
        if pp.currency_code != country.currency_code:
            add("product_prices", pp, "price_currency", f"{pp.currency_code} is not the {pp.country_code} currency")
        if pp.unit_cost >= net_of_vat(pp.list_price, country.vat_rate):
            add("product_prices", pp, "positive_margin", "unit_cost (excl. VAT) must be below the net price")
        if product.launched_on and pp.country_code in valid_zone and \
                pp.valid_from != local_midnight_utc(product.launched_on, country.time_zone):
            add("product_prices", pp, "price_from_launch", "the opening price must start at local midnight of the launch day")
        if pp.valid_to is None:
            open_prices[(pp.sku, pp.country_code)] = open_prices.get((pp.sku, pp.country_code), 0) + 1
    for sku in products:
        for code in countries:
            if open_prices.get((sku, code), 0) != 1:
                add("product_prices", None, "one_current_price", f"{sku} in {code} has {open_prices.get((sku, code), 0)} current prices")

    # ---------------------------------------------------------------- 7. customers
    allowed_domains = set(settings.customer.email_domains)
    stores_by_country: dict[str, list] = {}
    for l in dataset.stock_locations:
        if l.location_type == "store":
            stores_by_country.setdefault(l.country_code, []).append(l)
    for c in dataset.customers:
        market = ref.market(c.country_code) if c.country_code in countries else None
        if not PATTERNS["customer_number"].match(c.customer_number):
            add("customers", c, "customer_number_format", "must be C followed by 8 digits")
        if c.email != c.email.lower() or not PATTERNS["email"].match(c.email):
            add("customers", c, "email_format", f"{c.email!r} must be a lower-case address")
        elif c.email.split("@", 1)[1] not in allowed_domains:
            add("customers", c, "email_domain", f"{c.email!r} must use a reserved example domain")
        if (c.address_line1 is None) != (c.postal_code is None) or (c.address_line1 is None) != (c.city is None):
            add("customers", c, "address_complete", "address_line1, postal_code and city are all set or all empty")
        if market is None:
            continue
        tz = ZoneInfo(market.time_zone)
        local_day = c.registered_at.astimezone(tz).date()
        if local_day < market.market_launched_on or local_day > settings.simulation_end:
            add("customers", c, "registration_window", f"registered {local_day}, outside {market.market_launched_on}..{settings.simulation_end}")
        if c.postal_code is not None and not POSTAL_PATTERNS[market.postal_code_format].match(c.postal_code):
            add("customers", c, "postal_code_format", f"{c.postal_code!r} is not {market.postal_code_format}")
        if c.city is not None and c.city not in {x.city for x in market.cities}:
            add("customers", c, "city", f"{c.city!r} is not a {c.country_code} city in the reference list")
        if c.phone is not None and not c.phone.startswith(market.phone_prefix + " "):
            add("customers", c, "phone_format", f"{c.phone!r} must start with {market.phone_prefix}")
        if c.birth_date is not None:
            if age_on(c.birth_date, local_day) < 18:
                add("customers", c, "adult_at_signup", "younger than 18 at registration")
            if c.birth_date.year < 1900:
                add("customers", c, "birth_date", "born before 1900")
        if c.signup_channel == "in_store":
            open_by = [s for s in stores_by_country.get(c.country_code, []) if s.opened_on <= local_day]
            if not open_by:
                add("customers", c, "in_store_signup", "no store in the market was open on the registration day")
            else:
                local_hour = c.registered_at.astimezone(tz).hour
                if not 10 <= local_hour < 20:
                    add("customers", c, "in_store_hours", f"in-store sign-up at {local_hour}:00 local time")
    return issues
