"""Deterministic randomness, money rounding and other shared rules.

Reproducibility across machines:
  * Every entity gets its own random stream, seeded with a text key such as
    "nordiccart|20261117|customer|42". Python seeds text keys through SHA-512, which
    does not depend on PYTHONHASHSEED, the platform or the Python version.
  * Only stable random.Random methods are used (random, randrange, randint, choice,
    choices).
  * Prices, margins and the customer growth curve use Decimal arithmetic, never
    float exp/log/pow, whose last digit can differ between C libraries.
  * Money is rounded with ROUND_HALF_UP to 2 decimals, as the design requires.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext
from zoneinfo import ZoneInfo

CENT = Decimal("0.01")
ONE = Decimal(1)
UTC = timezone.utc


def stream(seed: int, *labels) -> random.Random:
    """An independent random stream for one entity, e.g. stream(seed, 'customer', 42)."""
    return random.Random("nordiccart|" + str(seed) + "|" + "|".join(str(x) for x in labels))


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def uniform_decimal(rng: random.Random, low: Decimal, high: Decimal, steps: int = 1_000_000) -> Decimal:
    """Uniform value in [low, high] on a grid of `steps` points, without floats."""
    return low + (high - low) * Decimal(rng.randrange(steps + 1)) / Decimal(steps)


def log_uniform_decimal(rng: random.Random, low: Decimal, high: Decimal) -> Decimal:
    """Log-uniform value in [low, high]: as many cheap as expensive orders of magnitude."""
    with localcontext() as ctx:
        ctx.prec = 28
        u = Decimal(rng.randrange(1_000_001)) / Decimal(1_000_000)
        return (low.ln() + u * (high.ln() - low.ln())).exp()


def power_fraction(value: Decimal, exponent: Decimal) -> Decimal:
    """value ** exponent for 0 < value <= 1 with Decimal (deterministic everywhere)."""
    with localcontext() as ctx:
        ctx.prec = 28
        return value ** exponent


def _round_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).quantize(ONE, rounding=ROUND_HALF_UP) * step


def price_point(amount: Decimal, currency_code: str) -> Decimal:
    """Turn a converted amount into a typical shelf price in that currency.

    EUR: under 100 ends in .90 (29.90), under 1000 ends in 9 (349.00), above in 49/99 (1 299.00).
    SEK, NOK, DKK: under 1 000 ends in 9 (399), under 10 000 ends in 99 (4 999),
    above ends in 990 (12 990).
    """
    if currency_code == "EUR":
        if amount < 100:
            price = _round_to(amount, ONE) - Decimal("0.10")
        elif amount < 1000:
            price = _round_to(amount, Decimal(10)) - ONE
        else:
            price = _round_to(amount, Decimal(50)) - ONE
        floor = Decimal("4.90")
    else:
        if amount < 1000:
            price = _round_to(amount, Decimal(10)) - ONE
        elif amount < 10000:
            price = _round_to(amount, Decimal(100)) - ONE
        else:
            price = _round_to(amount, Decimal(1000)) - Decimal(10)
        floor = Decimal(49)
    return money(max(price, floor))


def net_of_vat(gross: Decimal, vat_rate: Decimal) -> Decimal:
    """Price excl. VAT, unrounded."""
    return gross / (ONE + vat_rate)


def local_midnight_utc(day: date, time_zone: str) -> datetime:
    """00:00 local time on `day` in the market time zone, as a UTC timestamp."""
    return datetime.combine(day, time(0, 0), tzinfo=ZoneInfo(time_zone)).astimezone(UTC)


def local_to_utc(day: date, hour: int, minute: int, second: int, time_zone: str) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=ZoneInfo(time_zone)).astimezone(UTC)


def add_days(day: date, days: int) -> date:
    return day + timedelta(days=days)


def years_before(day: date, years: int) -> date:
    """Same calendar day `years` earlier; 29 February becomes 28 February."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def age_on(birth: date, day: date) -> int:
    return day.year - birth.year - ((day.month, day.day) < (birth.month, birth.day))


def floor_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


ASCII_FOLD = str.maketrans({
    "å": "a", "ä": "a", "á": "a", "à": "a", "ö": "o", "ø": "o", "ó": "o", "æ": "ae",
    "é": "e", "è": "e", "ë": "e", "ü": "u", "ú": "u", "í": "i", "ý": "y", "ÿ": "y", "ß": "ss",
})


def email_part(text: str) -> str:
    """Lower-case ASCII form of a name for an email address (Jönsson -> jonsson)."""
    folded = text.lower().translate(ASCII_FOLD)
    return "".join(ch for ch in folded if ch.isascii() and (ch.isalnum() or ch in "-"))
