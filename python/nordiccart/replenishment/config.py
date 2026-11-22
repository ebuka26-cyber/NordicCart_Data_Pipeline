"""Replenishment settings: the [replenishment] section of config/generator.toml."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ..settings import Settings, SettingsError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

APPROVED_PROFILES = ("pilot",)
MARKETS = ("SE", "NO", "DK", "FI")
LOCATION_TYPES = ("warehouse", "store")


@dataclass(frozen=True)
class ReplenishmentSettings:
    first_review_date: date
    end_date: date
    first_document_sequence: int
    revision_hour: int                       # local hour of R0 (warehouse reorder point revision)
    store_review_hour: int                   # local, Monday to Saturday
    warehouse_review_weekday: int            # ISO weekday
    warehouse_review_hour: int
    warehouse_days: tuple[int, ...]          # ISO weekdays when warehouses dispatch and receive
    dispatch_hours: tuple[int, int]          # local [from, to)
    transit_minutes: tuple[int, int]
    receiving_hours: tuple[int, int]         # local [from, to)
    supplier_lead_days: tuple[int, int]
    split_delivery_gap_days: tuple[int, int] # working days between the two notes
    pre_launch_days: dict[str, int]          # warehouse / store
    echelon_reorder_points: bool
    protect_warehouse_reorder_point: bool
    case_packs: dict[str, int]               # per category_code
    forced_reorders: dict[str, int]          # per market, at the first warehouse review
    split_deliveries: tuple[str, ...]        # markets whose forced purchase order arrives in two notes
    store_opening_hour: int                  # from [transactions].store_hours

    @property
    def end_exclusive(self) -> date:
        return self.end_date + timedelta(days=1)


def _need(table: dict, key: str, where: str = "replenishment"):
    if key not in table:
        raise SettingsError(f"Missing '{key}' in [{where}] of the generator config.")
    return table[key]


def _pair(raw, name: str, low: int, high: int) -> tuple[int, int]:
    if not isinstance(raw, list) or len(raw) != 2 or not all(isinstance(x, int) for x in raw):
        raise SettingsError(f"replenishment.{name} must be two whole numbers, e.g. [6, 8].")
    a, b = raw
    if not low <= a <= b <= high:
        raise SettingsError(f"replenishment.{name} must satisfy {low} <= from <= to <= {high}.")
    return a, b


def _hour(raw, name: str) -> int:
    if not isinstance(raw, int) or not 0 <= raw <= 23:
        raise SettingsError(f"replenishment.{name} must be a whole hour 0 to 23.")
    return raw


def load_replenishment_settings(settings: Settings, store_hours: tuple[int, int],
                                category_codes: set[str], config_path: Path | str | None = None, *,
                                engine: bool = False) -> ReplenishmentSettings:
    """Read [replenishment] from the same file the other settings came from.

    The replenishment pilot is approved for the pilot profile only; the activity engine (S6/S7,
    engine=True) uses the same policy settings for the full profile.
    """
    if settings.profile not in APPROVED_PROFILES and not engine:
        raise SettingsError(
            f"The replenishment simulation is approved for the pilot profile only (got '{settings.profile}'). "
            "Larger volumes wait for the combined activity simulation.")
    path = Path(config_path) if config_path else settings.config_path
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    if "replenishment" not in raw:
        raise SettingsError(f"{path} has no [replenishment] section.")
    rp = raw["replenishment"]

    first = _need(rp, "first_review_date")
    end = _need(rp, "end_date")
    for name, value in (("first_review_date", first), ("end_date", end)):
        if not isinstance(value, date):
            raise SettingsError(f"replenishment.{name} must be a date (YYYY-MM-DD without quotes).")
    if not first < end:
        raise SettingsError("replenishment.end_date must be after first_review_date.")
    if first < settings.simulation_start or end > settings.simulation_end:
        raise SettingsError("replenishment dates must stay within the simulation calendar.")

    seq = _need(rp, "first_document_sequence")
    if not isinstance(seq, int) or seq < 1:
        raise SettingsError("replenishment.first_document_sequence must be a whole number >= 1.")

    weekday = _need(rp, "warehouse_review_weekday")
    if not isinstance(weekday, int) or not 1 <= weekday <= 7:
        raise SettingsError("replenishment.warehouse_review_weekday must be an ISO weekday 1 (Monday) to 7 (Sunday).")
    days = _need(rp, "warehouse_days")
    if not isinstance(days, list) or not days or any(not isinstance(d, int) or not 1 <= d <= 7 for d in days):
        raise SettingsError("replenishment.warehouse_days must list ISO weekdays 1 to 7.")
    if weekday not in days:
        raise SettingsError("replenishment.warehouse_review_weekday must be one of warehouse_days.")

    dispatch = _pair(_need(rp, "dispatch_hours"), "dispatch_hours", 0, 24)
    transit = _pair(_need(rp, "transit_minutes"), "transit_minutes", 1, 24 * 60)
    receiving = _pair(_need(rp, "receiving_hours"), "receiving_hours", 0, 24)
    lead = _pair(_need(rp, "supplier_lead_days"), "supplier_lead_days", 1, 120)
    gap = _pair(_need(rp, "split_delivery_gap_days"), "split_delivery_gap_days", 1, 30)
    if dispatch[0] == dispatch[1] or receiving[0] == receiving[1]:
        raise SettingsError("replenishment.dispatch_hours and receiving_hours need from < to.")
    opening_hour = store_hours[0]
    if dispatch[1] * 60 + transit[1] > opening_hour * 60:
        raise SettingsError("replenishment: the last dispatch plus the longest transit must arrive before the stores "
                            f"open at {opening_hour}:00 (transactions.store_hours), so transit stays within one day.")

    pre = _need(rp, "pre_launch_days")
    if any(not isinstance(pre.get(t), int) or pre[t] < 0 for t in LOCATION_TYPES):
        raise SettingsError("replenishment.pre_launch_days needs whole days >= 0 for warehouse and store.")

    packs_raw = _need(rp, "case_packs")
    missing = sorted(category_codes - set(packs_raw))
    if missing:
        raise SettingsError(f"replenishment.case_packs needs a case pack for {', '.join(missing)}.")
    if any(not isinstance(v, int) or v < 1 for v in packs_raw.values()):
        raise SettingsError("replenishment.case_packs must be whole numbers >= 1.")

    echelon = _need(rp, "echelon_reorder_points")
    protection = _need(rp, "warehouse_protection")
    if not isinstance(echelon, bool):
        raise SettingsError("replenishment.echelon_reorder_points must be true or false.")
    if protection not in ("reorder_point", "none"):
        raise SettingsError('replenishment.warehouse_protection must be "reorder_point" or "none".')

    revision_hour = _hour(_need(rp, "revision_hour"), "revision_hour")
    store_review_hour = _hour(_need(rp, "store_review_hour"), "store_review_hour")
    if store_review_hour < store_hours[1]:
        raise SettingsError(f"replenishment.store_review_hour must be after the stores close at {store_hours[1]}:00.")
    if revision_hour >= store_review_hour:
        raise SettingsError("replenishment.revision_hour must be before store_review_hour (R0 runs before the first review).")

    scen = _need(rp, "scenarios")
    forced = _need(scen, "forced_reorders", "replenishment.scenarios")
    if any(m not in MARKETS for m in forced) or any(not isinstance(v, int) or v < 0 for v in forced.values()):
        raise SettingsError(f"replenishment.scenarios.forced_reorders needs whole numbers >= 0 for markets {', '.join(MARKETS)}.")
    splits = tuple(_need(scen, "split_deliveries", "replenishment.scenarios"))
    if any(m not in MARKETS for m in splits) or len(set(splits)) != len(splits):
        raise SettingsError("replenishment.scenarios.split_deliveries must list distinct market codes.")
    if any(not forced.get(m) for m in splits):
        raise SettingsError("replenishment.scenarios.split_deliveries: each market needs a forced reorder to split.")

    return ReplenishmentSettings(
        first_review_date=first,
        end_date=end,
        first_document_sequence=seq,
        revision_hour=revision_hour,
        store_review_hour=store_review_hour,
        warehouse_review_weekday=weekday,
        warehouse_review_hour=_hour(_need(rp, "warehouse_review_hour"), "warehouse_review_hour"),
        warehouse_days=tuple(sorted(set(days))),
        dispatch_hours=dispatch,
        transit_minutes=transit,
        receiving_hours=receiving,
        supplier_lead_days=lead,
        split_delivery_gap_days=gap,
        pre_launch_days={t: int(pre[t]) for t in LOCATION_TYPES},
        echelon_reorder_points=echelon,
        protect_warehouse_reorder_point=protection == "reorder_point",
        case_packs={str(k): int(v) for k, v in packs_raw.items()},
        forced_reorders={m: int(forced.get(m, 0)) for m in MARKETS},
        split_deliveries=splits,
        store_opening_hour=opening_hour,
    )
