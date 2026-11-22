"""Opening inventory settings: the [inventory] section of config/generator.toml."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..settings import Settings, SettingsError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib


@dataclass(frozen=True)
class CategoryDemand:
    weekly_units: Decimal          # chain-wide weekly units of one typical product
    reference_price_eur: Decimal   # price at which a product sells weekly_units


@dataclass(frozen=True)
class InventorySettings:
    opening_date: date
    external_reference: str
    online_share: Decimal
    store_format_weight: dict[str, Decimal]
    weeks_of_cover: dict[str, Decimal]
    lead_time_weeks: dict[str, Decimal]
    safety_factor: Decimal
    price_elasticity: Decimal
    price_factor_range: tuple[Decimal, Decimal]
    popularity_range: tuple[Decimal, Decimal]
    location_noise_range: tuple[Decimal, Decimal]
    store_display_units: dict[str, int]
    flagship_extra_units: int
    warehouse_minimum_units: int
    category_demand: dict[str, CategoryDemand]


def _dec(value) -> Decimal:
    return Decimal(str(value))


def _range(raw, name: str) -> tuple[Decimal, Decimal]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise SettingsError(f"inventory.{name} must be a list of two numbers, e.g. [0.6, 1.5].")
    low, high = _dec(raw[0]), _dec(raw[1])
    if not Decimal(0) < low <= high:
        raise SettingsError(f"inventory.{name} must satisfy 0 < low <= high.")
    return low, high


def _need(table: dict, key: str):
    if key not in table:
        raise SettingsError(f"Missing '{key}' in [inventory] of the generator config.")
    return table[key]


def load_inventory_settings(settings: Settings, config_path: Path | str | None = None) -> InventorySettings:
    """Read [inventory] from the same file the master data settings came from."""
    path = Path(config_path) if config_path else settings.config_path
    with path.open("rb") as fh:
        raw = tomllib.load(fh, parse_float=Decimal)
    if "inventory" not in raw:
        raise SettingsError(f"{path} has no [inventory] section.")
    inv = raw["inventory"]

    opening_date = _need(inv, "opening_date")
    if not isinstance(opening_date, date):
        raise SettingsError("inventory.opening_date must be a date (YYYY-MM-DD without quotes).")
    if opening_date > settings.simulation_start:
        raise SettingsError("inventory.opening_date must be on or before calendar.simulation_start: "
                            "stock has to exist when the simulation begins.")
    reference = str(_need(inv, "external_reference"))
    if not reference or len(reference) > 64 or reference != reference.strip():
        raise SettingsError("inventory.external_reference must be 1 to 64 characters without outer spaces.")

    online_share = _dec(_need(inv, "online_share"))
    if not Decimal(0) <= online_share <= Decimal(1):
        raise SettingsError("inventory.online_share must be between 0 and 1.")

    def positive_map(key: str, keys: tuple[str, ...]) -> dict[str, Decimal]:
        table = _need(inv, key)
        missing = [k for k in keys if k not in table]
        if missing:
            raise SettingsError(f"inventory.{key} needs {', '.join(missing)}.")
        values = {k: _dec(table[k]) for k in keys}
        if any(v <= 0 for v in values.values()):
            raise SettingsError(f"inventory.{key} values must be positive.")
        return values

    display = _need(inv, "store_display_units")
    display_keys = ("under_100_eur", "under_500_eur", "from_500_eur")
    if any(k not in display or not isinstance(display[k], int) or display[k] < 1 for k in display_keys):
        raise SettingsError("inventory.store_display_units needs whole numbers >= 1 for "
                            "under_100_eur, under_500_eur and from_500_eur.")

    demand_raw = _need(inv, "category_demand")
    category_demand = {}
    for code, item in demand_raw.items():
        weekly = _dec(item.get("weekly_units", 0))
        ref_price = _dec(item.get("reference_price_eur", 0))
        if weekly <= 0 or ref_price <= 0:
            raise SettingsError(f"inventory.category_demand.{code} needs positive weekly_units and reference_price_eur.")
        category_demand[str(code)] = CategoryDemand(weekly, ref_price)

    for key in ("flagship_extra_units", "warehouse_minimum_units"):
        if not isinstance(_need(inv, key), int) or inv[key] < 0:
            raise SettingsError(f"inventory.{key} must be a whole number >= 0.")
    if inv["warehouse_minimum_units"] < 1:
        raise SettingsError("inventory.warehouse_minimum_units must be at least 1.")

    safety = _dec(_need(inv, "safety_factor"))
    elasticity = _dec(_need(inv, "price_elasticity"))
    if safety <= 0 or elasticity < 0:
        raise SettingsError("inventory.safety_factor must be positive and price_elasticity not negative.")

    return InventorySettings(
        opening_date=opening_date,
        external_reference=reference,
        online_share=online_share,
        store_format_weight=positive_map("store_format_weight", ("flagship", "standard")),
        weeks_of_cover=positive_map("weeks_of_cover", ("store", "warehouse")),
        lead_time_weeks=positive_map("lead_time_weeks", ("store", "warehouse")),
        safety_factor=safety,
        price_elasticity=elasticity,
        price_factor_range=_range(_need(inv, "price_factor_range"), "price_factor_range"),
        popularity_range=_range(_need(inv, "popularity_range"), "popularity_range"),
        location_noise_range=_range(_need(inv, "location_noise_range"), "location_noise_range"),
        store_display_units={k: int(display[k]) for k in display_keys},
        flagship_extra_units=int(inv["flagship_extra_units"]),
        warehouse_minimum_units=int(inv["warehouse_minimum_units"]),
        category_demand=category_demand,
    )
