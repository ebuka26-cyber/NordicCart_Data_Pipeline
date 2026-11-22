"""Column rules of the master data tables, as created by migrations V003 to V005.

Used twice:
  * validate.py checks every generated value against it before anything is written;
  * load.py compares it with the live catalog (information_schema) before writing,
    so a schema that differs from the migrations stops the run instead of failing
    half-way.
Each entry: column -> (data_type, character_maximum_length or None, nullable).
"""

from __future__ import annotations

COLUMNS: dict[str, dict[str, tuple[str, int | None, bool]]] = {
    "currencies": {
        "currency_code": ("character", 3, False),
        "currency_name": ("character varying", 50, False),
        "minor_units": ("smallint", None, False),
    },
    "countries": {
        "country_code": ("character", 2, False),
        "country_name": ("character varying", 100, False),
        "currency_code": ("character", 3, False),
        "vat_rate": ("numeric", None, False),
        "time_zone": ("character varying", 64, False),
    },
    "product_categories": {
        "category_code": ("character varying", 30, False),
        "category_name": ("character varying", 100, False),
        "is_active": ("boolean", None, False),
    },
    "products": {
        "sku": ("character varying", 32, False),
        "product_name": ("character varying", 200, False),
        "brand": ("character varying", 100, False),
        "category_id": ("bigint", None, False),
        "launched_on": ("date", None, True),
        "discontinued_on": ("date", None, True),
        "is_active": ("boolean", None, False),
    },
    "product_prices": {
        "product_id": ("bigint", None, False),
        "country_code": ("character", 2, False),
        "currency_code": ("character", 3, False),
        "list_price": ("numeric", None, False),
        "unit_cost": ("numeric", None, False),
        "valid_from": ("timestamp with time zone", None, False),
        "valid_to": ("timestamp with time zone", None, True),
    },
    "stock_locations": {
        "location_code": ("character varying", 20, False),
        "location_name": ("character varying", 100, False),
        "location_type": ("USER-DEFINED", None, False),
        "country_code": ("character", 2, False),
        "address_line1": ("character varying", 200, False),
        "postal_code": ("character varying", 10, False),
        "city": ("character varying", 100, False),
        "opened_on": ("date", None, False),
        "is_active": ("boolean", None, False),
    },
    "stores": {
        "stock_location_id": ("bigint", None, False),
        "store_format": ("character varying", 20, False),
    },
    "warehouses": {
        "stock_location_id": ("bigint", None, False),
        "ships_online_orders": ("boolean", None, False),
        "storage_capacity_units": ("integer", None, True),
    },
    "customers": {
        "customer_number": ("character varying", 20, False),
        "first_name": ("character varying", 100, False),
        "last_name": ("character varying", 100, False),
        "email": ("character varying", 255, False),
        "phone": ("character varying", 30, True),
        "birth_date": ("date", None, True),
        "address_line1": ("character varying", 200, True),
        "postal_code": ("character varying", 10, True),
        "city": ("character varying", 100, True),
        "country_code": ("character", 2, False),
        "signup_channel": ("USER-DEFINED", None, False),
        "registered_at": ("timestamp with time zone", None, False),
        "is_active": ("boolean", None, False),
    },
}

# numeric(p, s) columns: (precision, scale)
NUMERIC = {
    ("countries", "vat_rate"): (5, 4),
    ("product_prices", "list_price"): (12, 2),
    ("product_prices", "unit_cost"): (12, 2),
}

ENUM_VALUES = {
    ("stock_locations", "location_type"): ("store", "warehouse"),
    ("customers", "signup_channel"): ("online", "in_store"),
}

# Not enforced by the schema (design observations), enforced by the generator.
STORE_FORMATS = ("flagship", "standard")

# Minimum migration history the seeder needs.
REQUIRED_MIGRATIONS = ("001", "002", "003", "004", "005", "006", "007")
