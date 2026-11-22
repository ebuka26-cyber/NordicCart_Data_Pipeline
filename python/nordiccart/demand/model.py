"""The demand model of the combined activity (design 5 and 6): one set of assumptions for
order volume, channel, location and product mix, used by order generation (S6), the opening
stock of products added later (S7) and the monthly reorder point review R-cal (S6).

Customers (full profile, in memory)
  * Personal rate λ_c ~ Gamma(k, λ̄ / k), drawn once per customer (simulate.customer_rate).
  * Orders can start on the local registration day, for a channel only if the customer
    registered before the channel's last open hour (otherwise from the next day); on the
    registration day orders are placed in later hours. start[channel] holds that first day.
  * Exposure over a period P:  E_c(P) = sum over channels of π_c(ch) x sum of u_ch(d) over
    the days of P from start[ch] (calendar.py). A customer registered all year has E = 1.
    Expected orders = λ̄ x market factor x E_c(P); the negative binomial gives the spread.
  * Channel propensity π_c(online): 0 without a delivery address; 1 - 0.70 for in-store
    sign-ups; for online sign-ups the value x solved so that the expected online share of
    orders over demand.channel_period equals [transactions] online_share (0.55) exactly.
  * In store: P(store | customer) proportional to the format weight ([inventory]
    store_format_weight) x home_city_weight if the store is in the customer's city.
    Online: the market's warehouse.

Products
  * Base weight = category weekly_units x clip((reference price / price) ** elasticity) x
    popularity: the same figures and seeded stream as the opening inventory model.
  * Attractiveness on day d = base weight x life cycle (ramp over the first weeks, decline
    after 18 months, floor). A product is on sale from its launch day while active.
  * Lines per order and units per line: [transactions] lines_per_order and two_units_share
    (products under 50 EUR are bought two at a time with that share).

Expected values are exact sums over days (Decimal, 28 digits); simulate.py draws around them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from ..inventory.generate import _power, _price_eur
from ..master_data.model import MasterDataset
from ..master_data.rules import stream, uniform_decimal
from ..settings import Settings
from ..transactions.generate import CHEAP_EUR
from .calendar import DemandCalendar
from .config import CHANNELS, DemandSettings
from .numeric import PREC, cumulative, to_ints

ZERO, ONE = Decimal(0), Decimal(1)


class DemandError(ValueError):
    """The master data or settings do not fit the demand model."""


@dataclass(frozen=True)
class CustomerDemand:
    number: str
    market: str
    city: str | None
    signup_channel: str
    has_address: bool
    registered_local: datetime
    start: dict                        # channel -> first local day it can order (None: after the calendar)
    online: Decimal                    # π_c(online)
    warehouse: str
    stores: tuple[str, ...]
    store_probability: tuple[Decimal, ...]
    store_cum: tuple[int, ...]
    market_factor: Decimal

    def propensity(self, channel: str) -> Decimal:
        return self.online if channel == "online" else ONE - self.online


@dataclass(frozen=True)
class ProductDemand:
    sku: str
    category_code: str
    price_eur: Decimal
    base_weight: Decimal
    units_per_line: Decimal            # expected quantity of a line of this product
    cheap: bool
    launched_on: date | None
    discontinued_on: date | None
    is_active: bool

    def on_sale(self, day: date) -> bool:
        return (self.is_active and self.launched_on is not None and self.launched_on <= day
                and (self.discontinued_on is None or self.discontinued_on > day))


@dataclass(frozen=True)
class Mix:
    """Products on sale on one day: attractiveness, its total and cumulative integer weights."""
    skus: tuple[str, ...]
    weights: tuple[Decimal, ...]
    total: Decimal
    cum: list[int]
    units_per_line: Decimal            # attractiveness-weighted mean quantity of a line


class DemandModel:
    def __init__(self, ds: DemandSettings, settings: Settings, master: MasterDataset):
        self.ds = ds
        self.profile = settings.profile
        self.catalogue_seed = settings.seed
        self.calendar = cal = DemandCalendar(ds)
        countries = {c.country_code: c for c in master.countries}
        self.time_zone = {code: ZoneInfo(c.time_zone) for code, c in countries.items()}
        store_format = {s.location_code: s.store_format for s in master.stores}
        ships_online = {w.location_code for w in master.warehouses if w.ships_online_orders}
        self.location_market = {l.location_code: l.country_code for l in master.stock_locations}
        self.location_type = {l.location_code: l.location_type for l in master.stock_locations}
        self.location_channel = {code: "online" if kind == "warehouse" else "in_store"
                                 for code, kind in self.location_type.items()}
        markets = sorted(c for c in countries if c in ds.market_factor)
        if sorted(countries) != markets:
            raise DemandError(f"demand.market_factor needs every market: {', '.join(sorted(countries))}")
        self.markets = markets

        # Locations: one online warehouse per market; stores open for the whole calendar
        self.warehouse: dict[str, str] = {}
        self.stores: dict[str, list] = {}
        for code in markets:
            whs = [l.location_code for l in master.stock_locations
                   if l.country_code == code and l.location_code in ships_online and l.is_active]
            if len(whs) != 1:
                raise DemandError(f"{code}: the demand model expects exactly one warehouse shipping online orders")
            self.warehouse[code] = whs[0]
            stores = [l for l in master.stock_locations if l.country_code == code and l.location_type == "store"
                      and l.is_active]
            late = [l.location_code for l in stores if l.opened_on > ds.calendar_start]
            if late or not stores:
                raise DemandError(f"{code}: every store must be open on {ds.calendar_start} (opening later: {late})")
            self.stores[code] = stores
        self.store_format = store_format

        with localcontext() as ctx:
            ctx.prec = PREC
            self._build_products(ds, settings, master)
            self._build_customers(ds, master, store_format)
            self._build_location_mass()

    # ------------------------------------------------------------------ products
    def _build_products(self, ds: DemandSettings, settings: Settings, master: MasterDataset) -> None:
        inv = ds.inventory
        prices = _price_eur(master, settings)
        low, high = inv.price_factor_range
        markets = set(self.markets)
        priced: dict[str, dict[str, datetime]] = defaultdict(dict)
        for p in master.product_prices:
            if p.valid_to is None:
                priced[p.sku][p.country_code] = p.valid_from
        self.products: dict[str, ProductDemand] = {}
        for product in master.products:
            if set(priced[product.sku]) != markets:
                raise DemandError(f"{product.sku}: needs one open-ended price in every market")
            if product.launched_on is not None:
                late = [m for m, f in priced[product.sku].items()
                        if f.astimezone(self.time_zone[m]).date() > product.launched_on]
                if late:
                    raise DemandError(f"{product.sku}: priced only after its launch day in {late}")
            demand = inv.category_demand[product.category_code]
            price = prices[product.sku]
            factor = min(max(_power(demand.reference_price_eur / price, inv.price_elasticity), low), high)
            popularity = uniform_decimal(stream(settings.seed, "inventory", "demand", product.sku),
                                         *inv.popularity_range, steps=10_000)
            cheap = price < CHEAP_EUR
            self.products[product.sku] = ProductDemand(
                sku=product.sku, category_code=product.category_code, price_eur=price,
                base_weight=demand.weekly_units * factor * popularity,
                units_per_line=ONE + ds.two_units_share if cheap else ONE, cheap=cheap,
                launched_on=product.launched_on, discontinued_on=product.discontinued_on, is_active=product.is_active)
        total = sum(ds.lines_per_order.values())
        self.mean_lines = sum(Decimal(n) * w for n, w in ds.lines_per_order.items()) / total
        self.lines_cum = cumulative(to_ints([ds.lines_per_order[n] for n in sorted(ds.lines_per_order)]))
        self.line_counts = sorted(ds.lines_per_order)
        self._mix_cache: dict[date, Mix] = {}

    def life_cycle(self, p: ProductDemand, day: date) -> Decimal:
        """Ramp after launch (linear from launch_start_factor to 1), decline after decline_after_months."""
        at = self.ds.attractiveness
        age = (day - p.launched_on).days
        ramp_days = at.launch_ramp_weeks * 7
        ramp = ONE if age >= ramp_days else at.launch_start_factor + (ONE - at.launch_start_factor) * age / ramp_days
        months = (day.year - p.launched_on.year) * 12 + day.month - p.launched_on.month - (day.day < p.launched_on.day)
        decline = ONE
        if months > at.decline_after_months:
            decline = max(at.decline_floor, ONE - at.decline_per_month * (months - at.decline_after_months))
        return min(ramp, decline)

    def attractiveness(self, sku: str, day: date) -> Decimal:
        p = self.products[sku]
        return p.base_weight * self.life_cycle(p, day) if p.on_sale(day) else ZERO

    def mix(self, day: date) -> Mix:
        """Product mix of one day (the same in every market: one price per product and market from launch)."""
        cached = self._mix_cache.get(day)
        if cached is None:
            with localcontext() as ctx:
                ctx.prec = PREC
                skus = tuple(s for s, p in sorted(self.products.items()) if p.on_sale(day))
                weights = tuple(self.products[s].base_weight * self.life_cycle(self.products[s], day) for s in skus)
                total = sum(weights)
                units = sum(w * self.products[s].units_per_line for s, w in zip(skus, weights)) / total
                cached = Mix(skus, weights, total, cumulative(to_ints(list(weights))), units)
            self._mix_cache[day] = cached
        return cached

    # ------------------------------------------------------------------ customers
    def _build_customers(self, ds: DemandSettings, master: MasterDataset, store_format: dict) -> None:
        cal = self.calendar
        first, last = ds.channel_period
        a = ONE - ds.in_store_signup_store_share
        raw = []
        sums = defaultdict(lambda: ZERO)                     # (group, channel) -> sum of mf x units
        for c in master.customers:
            if not c.is_active:
                continue
            local = c.registered_at.astimezone(self.time_zone[c.country_code])
            start = {}
            for ch in CHANNELS:
                day = local.date() if local.hour < cal.last_hour(ch) else local.date() + timedelta(days=1)
                start[ch] = day if day <= cal.end else None
            has_address = bool(c.address_line1 and c.postal_code and c.city)
            group = "none" if not has_address else "store_signup" if c.signup_channel == "in_store" else "online_signup"
            mf = ds.market_factor[c.country_code]
            for ch in CHANNELS:
                if start[ch] is not None:
                    sums[(group, ch)] += mf * cal.units(ch, max(start[ch], first), last)
            raw.append((c, local, start, has_address, group, mf))

        s = ds.online_share
        o1, s1 = sums[("store_signup", "online")], sums[("store_signup", "in_store")]
        o2, s2 = sums[("online_signup", "online")], sums[("online_signup", "in_store")]
        s3 = sums[("none", "in_store")]
        denominator = (ONE - s) * o2 + s * s2
        if denominator <= 0:
            raise DemandError("no online sign-ups with an address: the online share cannot be reached")
        x = (s * (a * o1 + (ONE - a) * s1 + s2 + s3) - a * o1) / denominator
        if not ZERO <= x <= ONE:
            raise DemandError(f"online share {s} is not reachable (online sign-up propensity would be {x})")
        self.online_signup_propensity = x
        self.propensity_of_group = {"store_signup": a, "online_signup": x, "none": ZERO}

        home = ds.home_city_weight
        fmt = ds.inventory.store_format_weight
        self.customers: list[CustomerDemand] = []
        for c, local, start, has_address, group, mf in raw:
            stores = self.stores[c.country_code]
            weights = [fmt[store_format[l.location_code]] * (home if c.city and l.city == c.city else ONE)
                       for l in stores]
            total = sum(weights)
            self.customers.append(CustomerDemand(
                number=c.customer_number, market=c.country_code, city=c.city, signup_channel=c.signup_channel,
                has_address=has_address, registered_local=local, start=start,
                online=self.propensity_of_group[group], warehouse=self.warehouse[c.country_code],
                stores=tuple(l.location_code for l in stores), store_probability=tuple(w / total for w in weights),
                store_cum=tuple(cumulative(to_ints(weights))), market_factor=mf))
        self.customer = {c.number: c for c in self.customers}

    def exposure(self, c: CustomerDemand, first: date, last: date, channel: str | None = None) -> Decimal:
        """Seasonal exposure E_c over first..last (one channel's part when `channel` is given)."""
        total = ZERO
        for ch in CHANNELS if channel is None else (channel,):
            p = c.propensity(ch)
            if p and c.start[ch] is not None:
                total += p * self.calendar.units(ch, max(c.start[ch], first), last)
        return total

    def customer_years(self, first: date, last: date) -> Decimal:
        """Sum of the exposures of all customers (seasonal customer-years)."""
        return sum((self.exposure(c, first, last) for c in self.customers), ZERO)

    # ------------------------------------------------------------------ locations and expected orders
    def _build_location_mass(self) -> None:
        """M_loc(d) = sum over customers already started of mf x π(ch) x P(loc | customer, ch)."""
        n = len(self.calendar.days)
        increments: dict[str, list[Decimal]] = defaultdict(lambda: [ZERO] * n)
        for c in self.customers:
            for ch in CHANNELS:
                p = c.propensity(ch)
                if not p or c.start[ch] is None:
                    continue
                i = self.calendar.index.get(max(c.start[ch], self.calendar.start))
                if ch == "online":
                    increments[c.warehouse][i] += c.market_factor * p
                else:
                    for store, prob in zip(c.stores, c.store_probability):
                        increments[store][i] += c.market_factor * p * prob
        self.location_mass: dict[str, list[Decimal]] = {}
        for loc in sorted(increments):
            mass, running = [ZERO] * n, ZERO
            for i, inc in enumerate(increments[loc]):
                running += inc
                mass[i] = running
            self.location_mass[loc] = mass
        self.locations = sorted(self.location_mass)

    def daily_orders(self, location: str, first: date, last: date) -> list[tuple[date, Decimal]]:
        """Expected orders placed at (online: fulfilled from) a location on each day."""
        cal = self.calendar
        ch = self.location_channel[location]
        unit, mass = cal.unit[ch], self.location_mass[location]
        rate = self.ds.orders_per_customer_year
        first, last = max(first, cal.start), min(last, cal.end)
        return [(cal.days[i], rate * mass[i] * unit[i]) for i in range(cal.index[first], cal.index[last] + 1)]

    def expected_orders(self, first: date, last: date, market: str | None = None, channel: str | None = None,
                        location: str | None = None) -> Decimal:
        total = ZERO
        with localcontext() as ctx:
            ctx.prec = PREC
            for loc in self.locations:
                if (market and self.location_market[loc] != market) or (channel and self.location_channel[loc] != channel) \
                        or (location and loc != location):
                    continue
                total += sum((v for _, v in self.daily_orders(loc, first, last)), ZERO)
        return total

    def expected_orders_by(self, first: date, last: date, key) -> dict:
        """Expected orders grouped by key(location, day), e.g. lambda loc, d: (d.year, d.month)."""
        out: dict = defaultdict(lambda: ZERO)
        with localcontext() as ctx:
            ctx.prec = PREC
            for loc in self.locations:
                for d, v in self.daily_orders(loc, first, last):
                    out[key(loc, d)] += v
        return dict(out)

    # ------------------------------------------------------------------ lifecycle and units
    def outcome_probabilities(self, channel: str) -> dict[str, Decimal]:
        """Exact outcome probabilities of an order (stock available): design C11."""
        lc = self.ds.lifecycle[channel]
        with localcontext() as ctx:
            ctx.prec = PREC
            pay = ONE - lc.abandoned_checkout - lc.customer_cancel
            f, r = lc.first_attempt_failure, lc.retry_after_failure
            captured = pay * (ONE - f) * sum((f * r) ** j for j in range(lc.max_attempts))
            return {"fulfilled": captured, "payment_failed": pay - captured, "expired": lc.abandoned_checkout,
                    "cancelled": lc.customer_cancel}

    def units_per_order(self, first: date, last: date) -> Decimal:
        """Expected units per placed order over a period (orders-weighted product mix)."""
        with localcontext() as ctx:
            ctx.prec = PREC
            per_day = self.expected_orders_by(first, last, lambda loc, d: d)
            total = sum(per_day.values())
            return self.mean_lines * sum(v * self.mix(d).units_per_line for d, v in per_day.items()) / total

    def expected_units(self, first: date, last: date, locations=None, fulfilled: bool = True) -> dict[tuple[str, str], Decimal]:
        """Expected units per (location, sku) over first..last: orders x lines x product share x units per line.

        fulfilled=True counts only orders expected to be fulfilled (stock leaves the location);
        lines of one order are distinct products, which the expectation ignores (shares are small).
        """
        out: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        locations = self.locations if locations is None else locations
        with localcontext() as ctx:
            ctx.prec = PREC
            share = {ch: self.outcome_probabilities(ch)["fulfilled"] if fulfilled else ONE for ch in CHANNELS}
            series = {loc: self.daily_orders(loc, first, last) for loc in locations}
            days = [d for d, _ in series[locations[0]]] if locations else []
            for i, d in enumerate(days):
                m = self.mix(d)
                q = [w * self.products[s].units_per_line for s, w in zip(m.skus, m.weights)]
                for loc in locations:
                    orders = series[loc][i][1] * share[self.location_channel[loc]]
                    if not orders:
                        continue
                    factor = orders * self.mean_lines / m.total
                    for s, wq in zip(m.skus, q):
                        out[(loc, s)] += factor * wq
        return dict(out)

    def weekly_units(self, week_first: date, locations=None, fulfilled: bool = True) -> dict[tuple[str, str], Decimal]:
        return self.expected_units(week_first, week_first + timedelta(days=6), locations, fulfilled)
