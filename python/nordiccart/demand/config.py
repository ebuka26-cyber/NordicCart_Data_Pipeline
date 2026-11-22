"""Demand settings: [demand] of config/generator.toml plus the shared pilot assumptions.

The demand model reuses, unchanged, what the pilots already assume:
  [transactions]  online_share, lines_per_order, two_units_share, online_hours, store_hours
  [inventory]     category_demand, price_elasticity, price_factor_range, popularity_range,
                  store_format_weight, weeks_of_cover, lead_time_weeks, safety_factor, ...
The [transactions] values are read here directly because load_transaction_settings is
approved for the pilot profile only and must stay so; they are validated the same way.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..inventory.config import InventorySettings, load_inventory_settings
from ..settings import Settings, SettingsError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

CHANNELS = ("online", "in_store")
LIFECYCLE_KEYS = ("first_attempt_failure", "retry_after_failure", "max_attempts", "abandoned_checkout", "customer_cancel")


@dataclass(frozen=True)
class Lifecycle:
    first_attempt_failure: Decimal       # each payment attempt fails with this share
    retry_after_failure: Decimal         # a failed attempt is retried with this share (attempts left)
    max_attempts: int
    abandoned_checkout: Decimal          # never paid: the reservation expires (U4)
    customer_cancel: Decimal             # cancelled by the customer before paying


@dataclass(frozen=True)
class Attractiveness:
    launch_ramp_weeks: int
    launch_start_factor: Decimal
    decline_after_months: int
    decline_per_month: Decimal
    decline_floor: Decimal


@dataclass(frozen=True)
class DemandSettings:
    seed: int                                    # demand streams; the engine uses the generator seed
    calendar_start: date                         # calendar.simulation_start
    calendar_end: date                           # S10: [demand] generation_end (default calendar.simulation_end)
    orders_per_customer_year: Decimal            # λ̄
    rate_shape: Decimal                          # k
    market_factor: dict[str, Decimal]
    generation_start: date
    growth_year: tuple[date, date]
    primary_year: tuple[date, date]
    channel_period: tuple[date, date]
    online_share: Decimal                        # [transactions]
    in_store_signup_store_share: Decimal
    home_city_weight: Decimal
    monthly_index: dict[int, Decimal]            # raw
    weekday_weights: dict[str, dict[int, Decimal]]
    store_closed_days: tuple[tuple[int, int], ...]
    black_week_weight: Decimal
    hour_weights: dict[str, dict[int, Decimal]]
    lines_per_order: dict[int, Decimal]          # [transactions]
    two_units_share: Decimal                     # [transactions]
    cheap_eur: Decimal                           # price below which two units are possible (pilot rule)
    attractiveness: Attractiveness
    lifecycle: dict[str, Lifecycle]
    inventory: InventorySettings                 # [inventory]
    planning_end: date | None = None             # S10: calendar.simulation_end, the end of the original calendar:
                                                 # sums and forecasts made up to it stay exactly as before

    @property
    def rate_scale(self) -> Decimal:
        """Gamma scale θ = λ̄ / k, so the mean rate is λ̄."""
        return self.orders_per_customer_year / self.rate_shape


def _dec(value) -> Decimal:
    return Decimal(str(value))


def _need(table: dict, key: str, where: str):
    if key not in table:
        raise SettingsError(f"Missing '{key}' in [{where}] of the generator config.")
    return table[key]


def _share(value, name: str) -> Decimal:
    v = _dec(value)
    if not Decimal(0) <= v <= Decimal(1):
        raise SettingsError(f"{name} must be between 0 and 1.")
    return v


def _period(raw, name: str) -> tuple[date, date]:
    if not isinstance(raw, list) or len(raw) != 2 or not all(isinstance(d, date) for d in raw) or raw[0] > raw[1]:
        raise SettingsError(f"demand.{name} must be [first day, last day] (dates without quotes).")
    return raw[0], raw[1]


def _int_weights(raw, name: str, keys: range | tuple, allow_zero: bool = True) -> dict[int, Decimal]:
    if not isinstance(raw, dict):
        raise SettingsError(f"demand.{name} must be a table.")
    values = {int(k): _dec(v) for k, v in raw.items()}
    if sorted(values) != sorted(keys):
        raise SettingsError(f"demand.{name} needs exactly the keys {', '.join(str(k) for k in keys)}.")
    if any(v < 0 or (not allow_zero and v == 0) for v in values.values()) or sum(values.values()) <= 0:
        raise SettingsError(f"demand.{name} weights must be {'non-negative' if allow_zero else 'positive'}, not all zero.")
    return values


def _hours(raw, name: str) -> tuple[int, int]:
    if not isinstance(raw, list) or len(raw) != 2 or not all(isinstance(x, int) for x in raw) \
            or not 0 <= raw[0] < raw[1] <= 24:
        raise SettingsError(f"transactions.{name} must be two whole hours with 0 <= from < to <= 24.")
    return raw[0], raw[1]


def load_demand_settings(settings: Settings, seed: int | None = None,
                         config_path: Path | str | None = None) -> DemandSettings:
    """Read [demand] (and the shared [transactions] / [inventory] values) from the generator config."""
    path = Path(config_path) if config_path else settings.config_path
    with path.open("rb") as fh:
        raw = tomllib.load(fh, parse_float=Decimal)
    if "demand" not in raw:
        raise SettingsError(f"{path} has no [demand] section.")
    dm, tx = raw["demand"], raw.get("transactions", {})

    rate = _dec(_need(dm, "orders_per_customer_year", "demand"))
    shape = _dec(_need(dm, "rate_shape", "demand"))
    if rate <= 0 or shape <= 0:
        raise SettingsError("demand.orders_per_customer_year and demand.rate_shape must be positive.")
    factors = {str(k): _dec(v) for k, v in _need(dm, "market_factor", "demand").items()}
    if any(v <= 0 for v in factors.values()):
        raise SettingsError("demand.market_factor values must be positive.")

    generation_start = _need(dm, "generation_start", "demand")
    if not isinstance(generation_start, date):
        raise SettingsError("demand.generation_start must be a date.")
    periods = {name: _period(_need(dm, name, "demand"), name) for name in ("growth_year", "primary_year", "channel_period")}
    for name, (first, last) in periods.items():
        if first < settings.simulation_start or last > settings.simulation_end:
            raise SettingsError(f"demand.{name} must lie inside the calendar {settings.simulation_start} to "
                                f"{settings.simulation_end}.")
    if not settings.simulation_start <= generation_start <= settings.simulation_end:
        raise SettingsError("demand.generation_start must lie inside the calendar.")

    online_hours = _hours(_need(tx, "online_hours", "transactions"), "online_hours")
    store_hours = _hours(_need(tx, "store_hours", "transactions"), "store_hours")
    weekday = {"online": _int_weights(_need(dm, "online_weekday_weights", "demand"), "online_weekday_weights",
                                      range(1, 8), allow_zero=False),
               "in_store": _int_weights(_need(dm, "store_weekday_weights", "demand"), "store_weekday_weights",
                                        range(1, 8))}
    hours = {"online": _int_weights(_need(dm, "online_hour_weights", "demand"), "online_hour_weights",
                                    range(*online_hours), allow_zero=False),
             "in_store": _int_weights(_need(dm, "store_hour_weights", "demand"), "store_hour_weights",
                                      range(*store_hours), allow_zero=False)}

    closed = []
    for text in _need(dm, "store_closed_days", "demand"):
        try:
            month, day = (int(x) for x in str(text).split("-"))
            date(2024, month, day)
        except ValueError as exc:
            raise SettingsError(f"demand.store_closed_days: '{text}' is not MM-DD.") from exc
        closed.append((month, day))

    black = _dec(_need(dm, "black_week_weight", "demand"))
    if black < 1:
        raise SettingsError("demand.black_week_weight must be at least 1.")

    lines_raw = _need(tx, "lines_per_order", "transactions")
    lines = {int(k): _dec(v) for k, v in lines_raw.items()}
    if not lines or any(k < 1 for k in lines) or any(v < 0 for v in lines.values()) or sum(lines.values()) <= 0:
        raise SettingsError("transactions.lines_per_order needs line counts >= 1 with non-negative weights.")

    at = _need(dm, "attractiveness", "demand")
    attractiveness = Attractiveness(
        launch_ramp_weeks=int(_need(at, "launch_ramp_weeks", "demand.attractiveness")),
        launch_start_factor=_share(_need(at, "launch_start_factor", "demand.attractiveness"),
                                   "demand.attractiveness.launch_start_factor"),
        decline_after_months=int(_need(at, "decline_after_months", "demand.attractiveness")),
        decline_per_month=_share(_need(at, "decline_per_month", "demand.attractiveness"),
                                 "demand.attractiveness.decline_per_month"),
        decline_floor=_share(_need(at, "decline_floor", "demand.attractiveness"), "demand.attractiveness.decline_floor"))
    if attractiveness.launch_ramp_weeks < 0 or attractiveness.decline_after_months < 0 or attractiveness.decline_floor <= 0:
        raise SettingsError("demand.attractiveness: weeks and months must be >= 0 and decline_floor positive.")

    lc_raw = _need(dm, "lifecycle", "demand")
    lifecycle = {}
    for channel in CHANNELS:
        item = _need(lc_raw, channel, "demand.lifecycle")
        missing = [k for k in LIFECYCLE_KEYS if k not in item]
        if missing:
            raise SettingsError(f"demand.lifecycle.{channel} needs {', '.join(missing)}.")
        lc = Lifecycle(
            first_attempt_failure=_share(item["first_attempt_failure"], f"demand.lifecycle.{channel}.first_attempt_failure"),
            retry_after_failure=_share(item["retry_after_failure"], f"demand.lifecycle.{channel}.retry_after_failure"),
            max_attempts=int(item["max_attempts"]),
            abandoned_checkout=_share(item["abandoned_checkout"], f"demand.lifecycle.{channel}.abandoned_checkout"),
            customer_cancel=_share(item["customer_cancel"], f"demand.lifecycle.{channel}.customer_cancel"))
        if lc.max_attempts < 1 or lc.abandoned_checkout + lc.customer_cancel >= 1:
            raise SettingsError(f"demand.lifecycle.{channel}: max_attempts >= 1 and abandoned + cancel < 1.")
        lifecycle[channel] = lc

    return DemandSettings(
        seed=settings.seed if seed is None else seed,
        calendar_start=settings.simulation_start,
        calendar_end=settings.activity_end,
        planning_end=settings.simulation_end,
        orders_per_customer_year=rate,
        rate_shape=shape,
        market_factor=factors,
        generation_start=generation_start,
        growth_year=periods["growth_year"],
        primary_year=periods["primary_year"],
        channel_period=periods["channel_period"],
        online_share=_share(_need(tx, "online_share", "transactions"), "transactions.online_share"),
        in_store_signup_store_share=_share(_need(dm, "in_store_signup_store_share", "demand"),
                                           "demand.in_store_signup_store_share"),
        home_city_weight=_dec(_need(dm, "home_city_weight", "demand")),
        monthly_index=_int_weights(_need(dm, "monthly_index", "demand"), "monthly_index", range(1, 13), allow_zero=False),
        weekday_weights=weekday,
        store_closed_days=tuple(closed),
        black_week_weight=black,
        hour_weights=hours,
        lines_per_order=lines,
        two_units_share=_share(_need(tx, "two_units_share", "transactions"), "transactions.two_units_share"),
        cheap_eur=Decimal(50),
        attractiveness=attractiveness,
        lifecycle=lifecycle,
        inventory=load_inventory_settings(settings, path),
    )
