"""Seeded order draws around the demand model (design 5.1): what the engine (S6) will place.

Streams (master_data.rules.stream, seed = DemandSettings.seed):
    ("demand", "rate", customer)                 λ_c ~ Gamma(k, λ̄ / k), once per customer
    ("demand", "orders", customer)               N_c,m ~ Poisson(λ_c x mf x E_c(month m)) for every
                                                 month of the calendar, in month order (a month
                                                 without exposure draws nothing)
    ("demand", "order", customer, "YYYY-MM", i)  the i-th order of that month: channel, day,
                                                 hour, location, lines, quantities, outcome

The month counts of a customer are always drawn for the whole calendar and each customer-month
is drawn whole; a request for some days only filters them. So any split of a period into
windows gives the same orders as the whole period (composition rule, design 7.2).
Given the month count, each order picks:
    channel   online with probability π(online) U_online / (π(online) U_online + π(store) U_store),
              U = the customer's exposure units of that channel in the month (from its start day)
    day       p_ch(d | m) over the customer's available days of the month
    hour      the channel's hour probabilities; on the registration day only later hours
    location  online: the market warehouse; in store: P(store | customer)
    lines     [transactions] lines_per_order; distinct products drawn from the day's mix
    quantity  2 for products under 50 EUR with two_units_share, else 1
    outcome   abandoned -> expired; cancelled; else payment attempts (failure, retry, limit)
Availability (out of stock, lost demand) belongs to the stock simulation of the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from ..master_data.rules import stream
from .model import CustomerDemand, DemandModel
from .numeric import PREC, chance, gamma, pick_index, poisson

ZERO, ONE = Decimal(0), Decimal(1)


@dataclass(frozen=True)
class SimulatedOrder:
    customer_number: str
    market: str
    channel: str
    location_code: str
    local_day: date
    hour: int
    lines: tuple[tuple[str, int], ...]            # (sku, quantity)
    outcome: str                                  # fulfilled, payment_failed, expired, cancelled
    attempts: int
    failed_attempts: int

    @property
    def units(self) -> int:
        return sum(q for _, q in self.lines)


def customer_rate(model: DemandModel, customer_number: str) -> Decimal:
    ds = model.ds
    return gamma(stream(ds.seed, "demand", "rate", customer_number), ds.rate_shape, ds.rate_scale)


def _outcome(model: DemandModel, rng, channel: str) -> tuple[str, int, int]:
    lc = model.ds.lifecycle[channel]
    r = rng.randrange(1_000_000_000)
    abandoned = int(lc.abandoned_checkout * 1_000_000_000)
    if r < abandoned:
        return "expired", 0, 0
    if r < abandoned + int(lc.customer_cancel * 1_000_000_000):
        return "cancelled", 0, 0
    attempts = failed = 0
    while True:
        attempts += 1
        if not chance(rng, lc.first_attempt_failure):
            return "fulfilled", attempts, failed
        failed += 1
        if attempts >= lc.max_attempts or not chance(rng, lc.retry_after_failure):
            return "payment_failed", attempts, failed


def month_units(model: DemandModel, c: CustomerDemand, span) -> dict[str, Decimal]:
    """The customer's exposure in one month per channel: π(ch) x the channel's units from its start day."""
    cal = model.calendar
    first_day, last_day = cal.days[span.first], cal.days[span.last]
    units = {}
    for ch in ("online", "in_store"):
        p, start = c.propensity(ch), c.start[ch]
        if not p or start is None or start > last_day:
            units[ch] = ZERO
        elif start <= first_day:
            units[ch] = p * span.share                     # a whole month: sum of u_ch over it = S*_m d_m / D
        else:
            units[ch] = p * cal.units(ch, start, last_day)
    return units


def month_counts(model: DemandModel, c: CustomerDemand, rate: Decimal) -> dict[tuple[int, int], tuple[int, dict]]:
    """(order count, exposure per channel) for every month of the calendar, one stream per customer."""
    rng = stream(model.ds.seed, "demand", "orders", c.number)
    out = {}
    with localcontext() as ctx:
        ctx.prec = PREC
        for span in model.calendar.months:
            units = month_units(model, c, span)
            exposure = units["online"] + units["in_store"]
            out[(span.year, span.month)] = (poisson(rng, rate * c.market_factor * exposure) if exposure else 0, units)
    return out


def month_orders(model: DemandModel, c: CustomerDemand, year: int, month: int, n: int, units: dict[str, Decimal],
                 with_lines: bool = True) -> list[SimulatedOrder]:
    """The n orders of one customer in one calendar month (local days); n from month_counts."""
    cal = model.calendar
    span = cal.span(year, month)
    first_day = cal.days[span.first]
    label = f"{year:04d}-{month:02d}"
    with localcontext() as ctx:
        ctx.prec = PREC
        out = []
        for i in range(n):
            rng = stream(model.ds.seed, "demand", "order", c.number, label, i)
            channel = "online" if chance(rng, units["online"] / (units["online"] + units["in_store"])) else "in_store"
            start = max(c.start[channel], first_day)
            low = cal.index[start] - span.first
            day = cal.days[span.first + pick_index(rng, cal.month_cum[channel][(year, month)], low)]
            hours, hour_cum = cal.hours[channel], cal.hour_cum[channel]
            hour_low = 0
            if day == c.registered_local.date():             # only hours after the sign-up
                hour_low = next(j for j, h in enumerate(hours) if h > c.registered_local.hour)
            hour = hours[pick_index(rng, hour_cum, hour_low)]
            location = c.warehouse if channel == "online" else c.stores[pick_index(rng, list(c.store_cum))]
            lines: tuple = ()
            if with_lines:
                lines = _lines(model, rng, day)
            outcome, attempts, failed = _outcome(model, rng, channel)
            out.append(SimulatedOrder(c.number, c.market, channel, location, day, hour, lines, outcome, attempts, failed))
        return out


def _lines(model: DemandModel, rng, day: date) -> tuple[tuple[str, int], ...]:
    mix = model.mix(day)
    count = min(model.line_counts[pick_index(rng, model.lines_cum)], len(mix.skus))
    chosen: list[str] = []
    while len(chosen) < count:                     # rejection of repeats = drawing without replacement
        sku = mix.skus[pick_index(rng, mix.cum)]
        if sku not in chosen:
            chosen.append(sku)
    out = []
    for sku in chosen:
        p = model.products[sku]
        out.append((sku, 2 if p.cheap and chance(rng, model.ds.two_units_share) else 1))
    return tuple(out)


def simulate(model: DemandModel, first: date, last: date, with_lines: bool = True,
             customers=None) -> tuple[list[SimulatedOrder], dict[str, Decimal]]:
    """Every order placed on local days first..last, and the personal rates used."""
    cal = model.calendar
    spans = cal.months_between(first, last)
    orders: list[SimulatedOrder] = []
    rates: dict[str, Decimal] = {}
    for c in model.customers if customers is None else customers:
        starts = [d for d in c.start.values() if d is not None]
        if not starts or min(starts) > last:
            continue
        rate = rates[c.number] = customer_rate(model, c.number)
        counts = month_counts(model, c, rate)
        for span in spans:
            n, units = counts[(span.year, span.month)]
            if not n:
                continue
            for o in month_orders(model, c, span.year, span.month, n, units, with_lines):
                if first <= o.local_day <= last:
                    orders.append(o)
    return orders, rates
