"""Transaction simulation settings: the [transactions] section of config/generator.toml."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from ..settings import Settings, SettingsError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

# Every path of the order lifecycle the pilot exercises. Order matters only for display.
SCENARIOS = (
    "fulfilled",
    "payment_retry_fulfilled",
    "payment_failed",
    "customer_cancelled",
    "reservation_expired",
    "out_of_stock_quantity",
    "out_of_stock_reserved",
)
CHANNELS = ("online", "in_store")
METHODS = ("card", "mobile_payment", "buy_now_pay_later", "cash")
FAILURE_REASONS = ("card_declined", "insufficient_funds", "timeout")   # V006 column comment
APPROVED_PROFILES = ("pilot",)


@dataclass(frozen=True)
class TransactionSettings:
    start_date: date
    days: int
    first_order_sequence: int
    online_share: Decimal
    lines_per_order: dict[int, Decimal]
    two_units_share: Decimal
    online_hours: tuple[int, int]
    store_hours: tuple[int, int]
    reservation_minutes: dict[str, int]
    max_payment_attempts: int
    fulfilment_cutoff_hour: int
    failure_reasons: tuple[str, ...]
    payment_methods: dict[str, dict[str, Decimal]]
    providers: dict[str, str]
    shipping_fee: dict[str, Decimal]
    free_shipping_from: dict[str, Decimal]
    scenarios: dict[str, int]

    @property
    def order_count(self) -> int:
        return sum(self.scenarios.values())

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(days=self.days - 1)


def _dec(value) -> Decimal:
    return Decimal(str(value))


def _need(table: dict, key: str, where: str = "transactions"):
    if key not in table:
        raise SettingsError(f"Missing '{key}' in [{where}] of the generator config.")
    return table[key]


def _hours(raw, name: str) -> tuple[int, int]:
    if not isinstance(raw, list) or len(raw) != 2 or not all(isinstance(x, int) for x in raw):
        raise SettingsError(f"transactions.{name} must be two whole hours, e.g. [10, 19].")
    start, end = raw
    if not 0 <= start < end <= 24:
        raise SettingsError(f"transactions.{name} must satisfy 0 <= from < to <= 24.")
    return start, end


def _weights(raw: dict, name: str, allowed: tuple[str, ...]) -> dict[str, Decimal]:
    unknown = [k for k in raw if k not in allowed]
    if unknown:
        raise SettingsError(f"transactions.{name} has unknown keys: {', '.join(unknown)}.")
    values = {str(k): _dec(v) for k, v in raw.items()}
    if any(v < 0 for v in values.values()) or sum(values.values()) <= 0:
        raise SettingsError(f"transactions.{name} weights must be non-negative and not all zero.")
    return values


def load_transaction_settings(settings: Settings, opening_date: date,
                              config_path: Path | str | None = None, *, engine: bool = False) -> TransactionSettings:
    """Read [transactions] from the same file the master data settings came from.

    The pilot transaction loader is approved for the pilot profile only. The activity engine
    (S6/S7, engine=True) reads the same profile-independent order rules for the full profile.
    """
    if settings.profile not in APPROVED_PROFILES and not engine:
        raise SettingsError(
            f"The transaction simulation is approved for the pilot profile only (got '{settings.profile}'). "
            "Larger volumes wait for the pilot review.")
    path = Path(config_path) if config_path else settings.config_path
    with path.open("rb") as fh:
        raw = tomllib.load(fh, parse_float=Decimal)
    if "transactions" not in raw:
        raise SettingsError(f"{path} has no [transactions] section.")
    tx = raw["transactions"]

    start = _need(tx, "start_date")
    if not isinstance(start, date):
        raise SettingsError("transactions.start_date must be a date (YYYY-MM-DD without quotes).")
    days = _need(tx, "days")
    if not isinstance(days, int) or days < 1:
        raise SettingsError("transactions.days must be a whole number >= 1.")
    if start < opening_date:
        raise SettingsError("transactions.start_date must not be before inventory.opening_date: orders need stock.")
    if start < settings.simulation_start:
        raise SettingsError("transactions.start_date must not be before calendar.simulation_start.")
    if start + timedelta(days=days - 1) > settings.simulation_end:
        raise SettingsError("transactions.start_date + days must stay within the simulation calendar.")

    first_seq = _need(tx, "first_order_sequence")
    if not isinstance(first_seq, int) or first_seq < 1:
        raise SettingsError("transactions.first_order_sequence must be a whole number >= 1.")

    online_share = _dec(_need(tx, "online_share"))
    two_units = _dec(_need(tx, "two_units_share"))
    for name, value in (("online_share", online_share), ("two_units_share", two_units)):
        if not Decimal(0) <= value <= Decimal(1):
            raise SettingsError(f"transactions.{name} must be between 0 and 1.")

    lines_raw = _need(tx, "lines_per_order")
    try:
        lines = {int(k): _dec(v) for k, v in lines_raw.items()}
    except ValueError:
        raise SettingsError('transactions.lines_per_order keys must be line counts, e.g. { "1" = 0.7 }.') from None
    if any(k < 1 for k in lines) or any(v < 0 for v in lines.values()) or sum(lines.values()) <= 0:
        raise SettingsError("transactions.lines_per_order needs line counts >= 1 with non-negative weights.")

    minutes = _need(tx, "reservation_minutes")
    if any(not isinstance(minutes.get(c), int) or minutes[c] < 1 for c in CHANNELS):
        raise SettingsError("transactions.reservation_minutes needs whole minutes >= 1 for online and in_store.")

    max_attempts = _need(tx, "max_payment_attempts")
    if not isinstance(max_attempts, int) or not 2 <= max_attempts <= 9:
        raise SettingsError("transactions.max_payment_attempts must be between 2 and 9 (a retry needs 2).")
    cutoff = _need(tx, "fulfilment_cutoff_hour")
    if not isinstance(cutoff, int) or not 8 <= cutoff <= 20:
        raise SettingsError("transactions.fulfilment_cutoff_hour must be a whole hour between 8 and 20.")

    reasons = tuple(str(x) for x in _need(tx, "failure_reasons"))
    if not reasons or any(r not in FAILURE_REASONS for r in reasons):
        raise SettingsError(f"transactions.failure_reasons must be chosen from {', '.join(FAILURE_REASONS)}.")

    methods_raw = _need(tx, "payment_methods")
    methods = {}
    for channel in CHANNELS:
        allowed = METHODS if channel == "in_store" else tuple(m for m in METHODS if m != "cash")
        methods[channel] = _weights(_need(methods_raw, channel, "transactions.payment_methods"),
                                    f"payment_methods.{channel}", allowed)
    if "card" not in methods["online"] or "card" not in methods["in_store"]:
        raise SettingsError("transactions.payment_methods needs card for both channels (failing attempts use cards).")

    providers = {str(k): str(v) for k, v in _need(tx, "providers").items()}
    missing = [m for m in METHODS if not providers.get(m) or len(providers[m]) > 50]
    if missing:
        raise SettingsError(f"transactions.providers needs a name (1 to 50 characters) for {', '.join(missing)}.")

    shipping = _need(tx, "shipping")
    fee = {str(k): _dec(v) for k, v in _need(shipping, "fee", "transactions.shipping").items()}
    free_from = {str(k): _dec(v) for k, v in _need(shipping, "free_from", "transactions.shipping").items()}
    for market in settings.customer.country_weights:
        if market not in fee or market not in free_from:
            raise SettingsError(f"transactions.shipping needs fee and free_from for {market}.")
    if any(v < 0 or v != v.quantize(Decimal("0.01")) for v in fee.values()) or any(v < 0 for v in free_from.values()):
        raise SettingsError("transactions.shipping fees must be non-negative amounts with at most 2 decimals.")

    scen_raw = _need(tx, "scenarios")
    unknown = [k for k in scen_raw if k not in SCENARIOS]
    if unknown:
        raise SettingsError(f"transactions.scenarios has unknown scenarios: {', '.join(unknown)}.")
    scenarios = {s: scen_raw.get(s, 0) for s in SCENARIOS}
    if any(not isinstance(v, int) or v < 0 for v in scenarios.values()) or sum(scenarios.values()) < 1:
        raise SettingsError("transactions.scenarios counts must be whole numbers >= 0, at least one order in total.")
    if scenarios["out_of_stock_reserved"] > scenarios["fulfilled"]:
        raise SettingsError("transactions.scenarios: each out_of_stock_reserved order needs a fulfilled in-store "
                            "order that holds the last unit, so fulfilled >= out_of_stock_reserved.")

    return TransactionSettings(
        start_date=start,
        days=days,
        first_order_sequence=first_seq,
        online_share=online_share,
        lines_per_order=dict(sorted(lines.items())),
        two_units_share=two_units,
        online_hours=_hours(_need(tx, "online_hours"), "online_hours"),
        store_hours=_hours(_need(tx, "store_hours"), "store_hours"),
        reservation_minutes={c: int(minutes[c]) for c in CHANNELS},
        max_payment_attempts=max_attempts,
        fulfilment_cutoff_hour=cutoff,
        failure_reasons=reasons,
        payment_methods=methods,
        providers=providers,
        shipping_fee=fee,
        free_shipping_from=free_from,
        scenarios=scenarios,
    )
