"""Loads the fixed reference data in python/nordiccart/reference_data/."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "reference_data"


@dataclass(frozen=True)
class City:
    city: str
    weight: int
    postal_min: int
    postal_max: int


@dataclass(frozen=True)
class Market:
    country_code: str
    country_name: str
    currency_code: str
    vat_rate: Decimal
    time_zone: str
    market_launched_on: date
    postal_code_format: str
    phone_prefix: str
    mobile_prefixes: tuple[str, ...]
    phone_layout: str
    address_suffixes: tuple[str, ...]
    streets: tuple[str, ...]
    cities: tuple[City, ...]

    def city(self, name: str) -> City:
        for c in self.cities:
            if c.city == name:
                return c
        raise KeyError(f"City {name!r} is not in the {self.country_code} city list.")


@dataclass(frozen=True)
class ProductType:
    pattern: str
    price_eur: tuple[Decimal, Decimal]
    parts: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Category:
    category_code: str
    category_name: str
    sku_prefix: str
    weight: int
    brands: tuple[str, ...]
    margin: tuple[Decimal, Decimal]
    types: tuple[ProductType, ...]


@dataclass(frozen=True)
class LocationSpec:
    location_code: str
    location_name: str
    location_type: str
    country_code: str
    address_line1: str
    postal_code: str
    city: str
    opened_on: date
    store_format: str | None = None
    ships_online_orders: bool | None = None
    storage_capacity_units: int | None = None


@dataclass(frozen=True)
class Reference:
    currencies: tuple[dict, ...]
    markets: tuple[Market, ...]
    categories: tuple[Category, ...]
    warehouses: tuple[LocationSpec, ...]
    stores: tuple[LocationSpec, ...]
    names: dict[str, dict[str, tuple[str, ...]]]

    def market(self, country_code: str) -> Market:
        for m in self.markets:
            if m.country_code == country_code:
                return m
        raise KeyError(country_code)


def _read(name: str, directory: Path) -> dict:
    with (directory / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def load_reference(directory: Path | None = None) -> Reference:
    directory = directory or REFERENCE_DIR
    markets_raw = _read("markets.json", directory)
    catalogue_raw = _read("catalogue.json", directory)
    locations_raw = _read("locations.json", directory)
    names_raw = _read("names.json", directory)

    markets = tuple(
        Market(
            country_code=m["country_code"],
            country_name=m["country_name"],
            currency_code=m["currency_code"],
            vat_rate=Decimal(m["vat_rate"]),
            time_zone=m["time_zone"],
            market_launched_on=date.fromisoformat(m["market_launched_on"]),
            postal_code_format=m["postal_code_format"],
            phone_prefix=m["phone"]["country_prefix"],
            mobile_prefixes=tuple(m["phone"]["mobile_prefixes"]),
            phone_layout=m["phone"]["layout"],
            address_suffixes=tuple(m["address_suffixes"]),
            streets=tuple(m["streets"]),
            cities=tuple(City(**c) for c in m["cities"]),
        )
        for m in markets_raw["countries"]
    )
    categories = tuple(
        Category(
            category_code=c["category_code"],
            category_name=c["category_name"],
            sku_prefix=c["sku_prefix"],
            weight=int(c["weight"]),
            brands=tuple(c["brands"]),
            margin=(Decimal(c["margin"][0]), Decimal(c["margin"][1])),
            types=tuple(
                ProductType(
                    pattern=t["pattern"],
                    price_eur=(Decimal(t["price_eur"][0]), Decimal(t["price_eur"][1])),
                    parts={k: tuple(v) for k, v in t["parts"].items()},
                )
                for t in c["types"]
            ),
        )
        for c in catalogue_raw["categories"]
    )
    warehouses = tuple(
        LocationSpec(location_type="warehouse", opened_on=date.fromisoformat(w.pop("opened_on")), **w)
        for w in (dict(x) for x in locations_raw["warehouses"])
    )
    stores = tuple(
        LocationSpec(location_type="store", opened_on=date.fromisoformat(s.pop("opened_on")), **s)
        for s in (dict(x) for x in locations_raw["stores"])
    )
    names = {
        cc: {k: tuple(v) for k, v in lists.items()}
        for cc, lists in names_raw.items() if not cc.startswith("_")
    }
    return Reference(
        currencies=tuple(markets_raw["currencies"]),
        markets=markets,
        categories=categories,
        warehouses=warehouses,
        stores=stores,
        names=names,
    )
