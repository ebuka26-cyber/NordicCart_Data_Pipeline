"""Row types for the master data tables and the generated dataset.

Rows carry business keys only (currency_code, sku, location_code, customer_number, ...),
never surrogate ids: the database assigns ids, and the loader resolves foreign keys
through the business keys. That is what makes a rerun safe.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timezone
from decimal import Decimal


@dataclass(frozen=True)
class Currency:
    currency_code: str
    currency_name: str
    minor_units: int


@dataclass(frozen=True)
class Country:
    country_code: str
    country_name: str
    currency_code: str
    vat_rate: Decimal
    time_zone: str


@dataclass(frozen=True)
class ProductCategory:
    category_code: str
    category_name: str
    is_active: bool


@dataclass(frozen=True)
class Product:
    sku: str
    product_name: str
    brand: str
    category_code: str
    launched_on: date | None
    discontinued_on: date | None
    is_active: bool


@dataclass(frozen=True)
class ProductPrice:
    sku: str
    country_code: str
    currency_code: str
    list_price: Decimal
    unit_cost: Decimal
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True)
class StockLocation:
    location_code: str
    location_name: str
    location_type: str
    country_code: str
    address_line1: str
    postal_code: str
    city: str
    opened_on: date
    is_active: bool


@dataclass(frozen=True)
class Store:
    location_code: str
    store_format: str


@dataclass(frozen=True)
class Warehouse:
    location_code: str
    ships_online_orders: bool
    storage_capacity_units: int | None


@dataclass(frozen=True)
class Customer:
    customer_number: str
    first_name: str
    last_name: str
    email: str
    phone: str | None
    birth_date: date | None
    address_line1: str | None
    postal_code: str | None
    city: str | None
    country_code: str
    signup_channel: str
    registered_at: datetime
    is_active: bool


# Load order follows the foreign keys.
TABLE_ORDER = (
    "currencies", "countries", "product_categories", "products", "product_prices",
    "stock_locations", "stores", "warehouses", "customers",
)


def column_names(row_type) -> list[str]:
    return [f.name for f in fields(row_type)]


def canonical(value):
    """Stable text form used for fingerprints, CSV files and comparisons."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


@dataclass
class MasterDataset:
    profile: str
    seed: int
    currencies: list[Currency]
    countries: list[Country]
    product_categories: list[ProductCategory]
    products: list[Product]
    product_prices: list[ProductPrice]
    stock_locations: list[StockLocation]
    stores: list[Store]
    warehouses: list[Warehouse]
    customers: list[Customer]

    def tables(self) -> dict[str, list]:
        return {name: getattr(self, name) for name in TABLE_ORDER}

    def row_counts(self) -> dict[str, int]:
        return {name: len(rows) for name, rows in self.tables().items()}

    def total_rows(self) -> int:
        return sum(self.row_counts().values())

    def fingerprint(self) -> str:
        """SHA-256 of every row in canonical form. Same seed and settings, same value."""
        payload = {
            name: [{k: canonical(v) for k, v in asdict(row).items()} for row in rows]
            for name, rows in self.tables().items()
        }
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
