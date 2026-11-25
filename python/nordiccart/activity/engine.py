"""Chronological activity engine (design 7.1 to 7.3; stage S6).

One discrete-event simulation combines customer orders (demand model, S2), their payments,
reservations and fulfilment (units U1 to U5), daily exchange rates (X1, S5), store transfers
(T1/T2), warehouse purchase orders and supplier receipts (V009 documents, R1). It runs in UTC
day windows; every write is one existing unit of work (nordiccart.units, exchange_rates).

Replay (milestone 2: incremental)
    A replay starts from a state S at an instant T and runs every action in (business time,
    rank, key) order up to the window end. Two starting points give the same result:
      full         T = the engine origin (demand.generation_start 00:00 UTC), S = state_as_of(origin);
                   the origin must be a closed history (no open order, transfer or purchase order)
      incremental  T = the end of the last verified window, S = its checkpoint state (the database
                   must still hold it: hash comparison) plus the CARRY stored with the checkpoint:
                   the actions already scheduled for T or later (the remaining steps of open orders,
                   transfer arrivals, supplier receipts) and the open orders' planned life
    Everything else is regenerated for [T, end): exchange rates and reviews by calendar, orders
    by the seeded demand draws placed in [T, end). Every random choice has its own stream keyed
    by business keys, so the replay is the same from any start: plan(day 1) + plan(day 2) =
    plan(days 1 to 2), and incremental = full (tested over December and January).
    The carry is plain data (dataclasses, Decimal, datetime) encoded losslessly to JSON, with its
    own SHA-256. Milestone 3: every carried future action is re-derived from its seeded stream
    when the carry is restored (open orders rebuilt from their demand draw, transfer times, receipt
    times); an altered or missing action blocks the window.

Ranks at equal business time (design 7.1)
    0 X1 exchange rates (00:00 UTC)  1 P1 price change (00:00 local)  2 R-cal monthly review
    3 R1 receipt  4 T2 arrival  5 T1 dispatch  6 order units (U1..U5)  7 PO of a warehouse review

Monthly business rules (milestone 3, [activity] in the generator config)
    P1     lifecycle markdowns (decision C8): when a product is markdown_after_months old (6, 12),
           at 00:00 local in every market, its list price falls by a seeded 3% to 8% (the same step
           in every market), rounded to the market's shelf price point; the unit cost is unchanged.
           A markdown that would not lower the price, or would leave no margin, is skipped. Only
           steps on or after the origin happen (no backdated price). The price timeline is a pure
           function of master data and config, so full and incremental replay see the same prices.
    R-cal  the monthly reorder point review (design 6), on the first working day of each month at
           rcal_local_hour local: demand.inventory.rcal_reorder_points for every listing of a product
           on sale; one unit per location with changes (guarded UPDATE + reorder_point_revisions
           kind monthly_review, reference RCAL-<location>-<YYYYMMDD>); a warehouse whose new reorder
           points would sum above its storage capacity keeps its current ones (capacity guard).

Policies
    orders     demand.simulate draws (customer, month, i); availability-aware customers take
               only what is free at the fulfilment location when they order (C9): a short
               line is reduced or dropped, an order with no line left is lost demand
    stores     review 05:30 local on warehouse working days (C10); (s, S) on the inventory
               position; the same morning dispatch 06:00 to 07:00 local, arrival 45 to 105
               minutes later, before the stores open
    warehouses review Mondays at warehouse_review_hour local; natural purchase orders
               (reorder_point or launch), rounded up to the case pack, within storage
               capacity; one delivery note after the supplier lead time on a working day
    s and S    s = the listing's reorder point in the database. S (S8.1) follows the R-cal in force
               on the review's local day: max(display or warehouse minimum, forecast units over the
               weeks of cover from that R-cal day (demand.inventory.rcal_cover_units), s + 1). Before
               the first R-cal after the origin, S comes from the planning figures of the opening
               inventory, as in the replenishment pilot. S is a pure function of the calendar, so
               full and incremental replay agree; it is not stored (transfer and PO lines record it)
"""

from __future__ import annotations

import calendar
import hashlib
import heapq
import json
import tomllib
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..demand.config import load_demand_settings
from ..demand.inventory import opening_inventory_for, rcal_cover_units, rcal_reorder_points
from ..demand.model import DemandModel
from ..exchange_rates import working_day
from ..demand.simulate import SimulatedOrder, customer_rate, month_counts, month_orders
from ..inventory.config import load_inventory_settings
from ..inventory.generate import display_minimum, generate_opening_inventory
from ..master_data.generate import generate_master_data
from ..master_data.model import ProductPrice, canonical
from ..master_data.rules import UTC, local_midnight_utc, money, net_of_vat, price_point, stream
from ..replenishment import policy
from ..replenishment.config import load_replenishment_settings
from ..replenishment.generate import local_time, on_or_next_working_day
from ..replenishment.model import Delivery, DeliveryLine, PurchaseOrder, PurchaseOrderLine, TransferDocument, TransferLine
from ..settings import Settings
from ..stock.ledger import InTransit, StockLedger
from ..transactions.config import load_transaction_settings
from ..transactions.generate import FAILING_METHODS, PROVIDER_PREFIX, REASONS_BY_METHOD, ZERO, chance, pick, price_at
from ..transactions.model import Event, PlannedAttempt, PlannedLine, PlannedOrder
from .state import COUNT_NAMES, build_snapshot

ENGINE_VERSION = "s8-1"                    # s6-m3 + S8.1 (S after R-cal): a run of another version is not continued
CARRY_VERSION = 2                          # 2: demand keys of the open orders and a SHA-256 of the carry
RANK = {"X1": 0, "P1": 1, "RCAL": 2, "R1": 3, "T2": 4, "T1": 5, "order": 6, "PO": 7}
STORE_REVIEW = (5, 30)                     # local, design C10
DISPATCH_MINUTES = (0, 59)                 # after dispatch_hours[0] local, before online ordering opens
DAY = timedelta(days=1)
CARRIED_ACTIONS = ("order_event", "dispatch", "arrive", "receive")


class EngineError(RuntimeError):
    """The engine cannot plan this window (settings, start state or database history)."""


class CarryError(EngineError):
    """The checkpoint's carry is not what the engine derives (altered, incomplete or foreign): blocking."""


# ----------------------------------------------------------------------------- monthly business rules
@dataclass(frozen=True)
class ActivitySettings:
    markdown_after_months: tuple[int, ...]
    markdown_range: tuple[Decimal, Decimal]
    rcal_local_hour: int


def load_activity_settings(settings: Settings) -> ActivitySettings:
    with settings.config_path.open("rb") as fh:
        raw = tomllib.load(fh, parse_float=Decimal).get("activity")
    if raw is None:
        raise EngineError(f"{settings.config_path} has no [activity] section")
    months = tuple(int(x) for x in raw["markdown_after_months"])
    low, high = (Decimal(str(x)) for x in raw["markdown_range"])
    hour = int(raw["rcal_local_hour"])
    if not months or any(m <= 0 for m in months) or not Decimal(0) < low <= high < Decimal(1) \
            or not 0 <= hour * 60 < STORE_REVIEW[0] * 60 + STORE_REVIEW[1]:
        raise EngineError("[activity]: markdown_after_months > 0, 0 < markdown_range low <= high < 1, "
                          "rcal_local_hour before the 05:30 store review")
    return ActivitySettings(months, (low, high), hour)


@dataclass(frozen=True)
class PriceChange:
    """P1: one product's markdown in one market (closes the current price, opens the new one)."""
    sku: str
    market: str
    currency: str
    at: datetime                           # 00:00 local of the markdown day
    step: int                              # 1, 2: the n-th markdown (markdown_after_months)
    markdown: Decimal                      # fraction, e.g. 0.054
    old_price: Decimal
    new_price: Decimal
    unit_cost: Decimal


@dataclass(frozen=True)
class ReorderPointReview:
    """R-cal: the changed reorder points of one location at one monthly review."""
    location: str
    at: datetime
    reference: str                         # RCAL-<location>-<YYYYMMDD>
    changes: tuple[tuple[str, int, int], ...]   # (sku, old, new), old != new


def add_months(day: date, months: int) -> date:
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def rcal_day(year: int, month: int) -> date:
    """First working day of a month (weekday, not a closure of exchange_rates.working_day)."""
    day = date(year, month, 1)
    while not working_day(day):
        day += DAY
    return day


def price_timeline(settings: Settings, master, activity: ActivitySettings, origin: datetime):
    """Every price row per (sku, market), markdowns included, and the P1 changes at or after the origin."""
    countries = {c.country_code: c for c in master.countries}
    products = {p.sku: p for p in master.products}
    timeline: dict[tuple[str, str], list[ProductPrice]] = {}
    changes: list[PriceChange] = []
    low, high = (int(x * 1000) for x in activity.markdown_range)
    for base in sorted(master.product_prices, key=lambda p: (p.sku, p.country_code, p.valid_from)):
        rows = timeline.setdefault((base.sku, base.country_code), [])
        rows.append(base)
        product = products[base.sku]
        if base.valid_to is not None or product.launched_on is None:
            continue
        country = countries[base.country_code]
        for step, months in enumerate(activity.markdown_after_months, start=1):
            day = add_months(product.launched_on, months)
            at = local_midnight_utc(day, country.time_zone)
            if at < origin or (product.discontinued_on is not None and day >= product.discontinued_on):
                continue                                         # no backdated or post-discontinuation price
            markdown = Decimal(stream(settings.seed, "activity", "markdown", base.sku, step).randint(low, high)) / 1000
            current = rows[-1]
            new = price_point(current.list_price * (1 - markdown), base.currency_code)
            if new >= current.list_price or current.unit_cost >= net_of_vat(new, country.vat_rate):
                continue                                         # would not lower the price, or no margin left
            rows[-1] = replace(current, valid_to=at)
            rows.append(ProductPrice(base.sku, base.country_code, base.currency_code, new, current.unit_cost, at, None))
            changes.append(PriceChange(base.sku, base.country_code, base.currency_code, at, step, markdown,
                                       current.list_price, new, current.unit_cost))
    return timeline, sorted(changes, key=lambda c: (c.at, c.sku, c.market))


# ----------------------------------------------------------------------------- context
@dataclass
class EngineContext:
    """Everything the simulation needs that does not come from the database (plus pure caches)."""
    settings: Settings
    master: object
    model: DemandModel
    tx: object
    inv: object
    rp: object
    origin: datetime
    listings: dict                         # (loc, sku) -> OpeningListing
    echelon: dict                          # (warehouse, sku) -> weekly units served
    config_fingerprint: str
    activity: ActivitySettings = None
    price_rows: dict = field(default_factory=dict, repr=False)      # (sku, market) -> [ProductPrice], markdowns included
    price_changes: list = field(default_factory=list, repr=False)   # [PriceChange] at or after the origin
    _month_orders: dict = field(default_factory=dict, repr=False)   # (year, month) -> [(key, order, placed_at)]
    _rcal: dict = field(default_factory=dict, repr=False)           # review day -> {(loc, sku): reorder point}
    _cover: dict = field(default_factory=dict, repr=False)          # review day -> {(loc, sku): units over the cover}

    @classmethod
    def load(cls, settings: Settings) -> "EngineContext":
        inv = load_inventory_settings(settings)
        tx = load_transaction_settings(settings, inv.opening_date, engine=True)
        master = generate_master_data(settings)
        rp = load_replenishment_settings(settings, tx.store_hours, {c.category_code for c in master.product_categories},
                                         engine=True)
        ds = load_demand_settings(settings)
        activity = load_activity_settings(settings)
        model = DemandModel(ds, settings, master)
        inventory = opening_inventory_for(settings, inv, master, model)     # pilot: the opening model; full: S7 calibrated
        raw = settings.config_path.read_bytes()
        fingerprint = hashlib.sha256(b"|".join([ENGINE_VERSION.encode(), settings.profile.encode(), raw])).hexdigest()
        origin = datetime.combine(ds.generation_start, dtime(0), UTC)
        rows, changes = price_timeline(settings, master, activity, origin)
        return cls(settings=settings, master=master, model=model, tx=tx, inv=inv, rp=rp, origin=origin,
                   listings={(x.location_code, x.sku): x for x in inventory.listings},
                   echelon=policy.echelon_weekly(inventory.listings), config_fingerprint=fingerprint,
                   activity=activity, price_rows=rows, price_changes=changes)

    def rcal(self, day: date) -> dict:
        """R-cal reorder points of a review day (pure; cached)."""
        if day not in self._rcal:
            self._rcal[day] = rcal_reorder_points(self.model, day)
        return self._rcal[day]

    def cover_units(self, day: date) -> dict | None:
        """S8.1: forecast units over the weeks of cover of the R-cal in force on local `day` (pure; cached).

        R-cal runs before the first review of its day (rcal_local_hour < 05:30), so a review on the
        R-cal day already uses it. None before the first R-cal after the origin.
        """
        review = rcal_day(day.year, day.month)
        if day < review:
            previous = day.replace(day=1) - DAY
            review = rcal_day(previous.year, previous.month)
        if review < self.origin.date():
            return None
        if review not in self._cover:
            self._cover[review] = rcal_cover_units(self.model, review)
        return self._cover[review]

    def demand_order(self, key: str) -> tuple[SimulatedOrder, datetime]:
        """The demand draw behind an order key <customer>/<YYYY-MM>/<i>, with its placed_at."""
        year, month = (int(x) for x in key.split("/")[1].split("-"))
        index = self.__dict__.setdefault("_key_index", {})
        if (year, month) not in index:
            index[(year, month)] = {k: (o, placed) for k, o, placed in self.orders_of_month(year, month)}
        found = index[(year, month)].get(key)
        if found is None:
            raise CarryError(f"no demand draw {key}")
        return found

    def orders_of_month(self, year: int, month: int) -> list[tuple[str, SimulatedOrder, datetime]]:
        """Every demand draw of one calendar month (local days), with its key and placed_at. Cached: pure."""
        cached = self._month_orders.get((year, month))
        if cached is None:
            model = self.model
            cached = []
            for c in model.customers:
                starts = [d for d in c.start.values() if d is not None]
                if not starts:
                    continue
                counts = _counts(self, c)
                if (year, month) not in counts:          # S8 fix: a neighbour month outside the calendar
                    continue                             # (2026-11 for the last window) has no demand
                n, units = counts[(year, month)]
                for i, o in enumerate(month_orders(model, c, year, month, n, units) if n else ()):
                    key = f"{c.number}/{year:04d}-{month:02d}/{i}"
                    cached.append((key, o, _placed_at(self, key, o)))
            self._month_orders[(year, month)] = cached
        return cached


def _counts(ctx: EngineContext, c) -> dict:
    cache = ctx.__dict__.setdefault("_counts", {})
    if c.number not in cache:
        cache[c.number] = month_counts(ctx.model, c, customer_rate(ctx.model, c.number))
    return cache[c.number]


def _placed_at(ctx: EngineContext, key: str, o: SimulatedOrder) -> datetime:
    r = stream(ctx.settings.seed, "activity", "placed", key)
    tz = ZoneInfo(next(c.time_zone for c in ctx.master.countries if c.country_code == o.market))
    local = datetime.combine(o.local_day, dtime(o.hour), tz) + timedelta(seconds=r.randrange(3300))
    return local.astimezone(UTC)


# ----------------------------------------------------------------------------- units of work
@dataclass(frozen=True)
class Unit:
    at: datetime
    rank: int
    key: str                               # order: <sequence>.<step>; documents: their reference
    kind: str                              # X1, PO, R1, T1, T2, or an order event kind
    label: str
    payload: object = field(compare=False, repr=False, default=None)
    attempt: int | None = None

    @property
    def sort_key(self) -> tuple:
        return self.at, self.rank, self.key

    def descriptor(self) -> dict:
        """What the plan fingerprint covers: when, what, and the document or order it writes."""
        return {"at": self.at.isoformat(), "rank": self.rank, "key": self.key, "kind": self.kind,
                "attempt": self.attempt, "payload": _plain(self.payload)}


def _plain(value):
    if hasattr(value, "__dataclass_fields__"):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return canonical(value)


def plan_fingerprint(units: list[Unit]) -> str:
    text = json.dumps([u.descriptor() for u in units], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- carry: lossless JSON
_TYPES = {cls.__name__: cls for cls in (PlannedOrder, PlannedLine, PlannedAttempt, Event, TransferDocument, TransferLine,
                                        PurchaseOrder, PurchaseOrderLine, Delivery, DeliveryLine, PriceChange,
                                        ReorderPointReview)}


def carry_digest(carry: dict) -> str:
    body = {k: v for k, v in carry.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def encode(value):
    """Typed JSON for the carry: dataclasses, tuples, Decimal, datetime and date survive exactly."""
    if hasattr(value, "__dataclass_fields__"):
        name = type(value).__name__
        if name not in _TYPES:
            raise EngineError(f"cannot carry a {name}")
        return {"$dc": name, "f": {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, tuple):
        return {"$tu": [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, Decimal):
        return {"$d": str(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise EngineError("naive datetime in the carry")
        return {"$t": value.astimezone(UTC).isoformat()}
    if isinstance(value, date):
        return {"$da": value.isoformat()}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise EngineError(f"cannot carry a {type(value).__name__}")


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        if "$dc" in value:
            return _TYPES[value["$dc"]](**{k: decode(v) for k, v in value["f"].items()})
        if "$tu" in value:
            return tuple(decode(v) for v in value["$tu"])
        if "$d" in value:
            return Decimal(value["$d"])
        if "$t" in value:
            return datetime.fromisoformat(value["$t"]).astimezone(UTC)
        if "$da" in value:
            return date.fromisoformat(value["$da"])
        raise EngineError(f"unknown carry value {sorted(value)}")
    return value


# ----------------------------------------------------------------------------- simulation
@dataclass
class Simulation:
    start: datetime
    units: list[Unit]                      # every unit of work from `start` to `until`, in order
    snapshots: dict                        # requested instant -> canonical state snapshot
    carry: dict                            # what continues after `until` (stored with its checkpoint)
    lost_orders: int = 0
    lost_units: int = 0
    short_lines: int = 0

    def window(self, start: datetime, end: datetime) -> list[Unit]:
        return [u for u in self.units if start <= u.at < end]


class _Sim:
    """The replay. Mirrors exactly what the units of work do to the database state."""

    def __init__(self, ctx: EngineContext, state: dict, start: datetime, carry: dict | None):
        self.ctx = ctx
        self.start = start
        self.seed = ctx.settings.seed
        m = ctx.master
        self.countries = {c.country_code: c for c in m.countries}
        self.locations = {l.location_code: l for l in m.stock_locations}
        self.formats = {s.location_code: s.store_format for s in m.stores}
        self.capacity = {w.location_code: w.storage_capacity_units for w in m.warehouses}
        self.products = {p.sku: p for p in m.products}
        self.customers = {c.customer_number: c for c in m.customers}
        self.prices = ctx.price_rows                    # static timeline: original prices and P1 markdowns
        self.warehouse_of = {code: wh for code, wh in ctx.model.warehouse.items()}
        self.stores_of = defaultdict(list)
        for l in sorted(m.stock_locations, key=lambda l: l.location_code):
            if l.location_type == "store":
                self.stores_of[l.country_code].append(l.location_code)
        self.markets = sorted(self.warehouse_of)

        if datetime.fromisoformat(state["as_of"]) != start:
            raise EngineError(f"the start state is as of {state['as_of']}, not {start.isoformat()}")
        self.ledger = StockLedger({})
        self.rop: dict[tuple[str, str], int] = {}
        for key, (on_hand, reserved, rop) in state["balances"].items():
            loc, sku = key.split("|")
            self.ledger.on_hand[(loc, sku)] = on_hand
            if reserved:
                self.ledger.reserved[(loc, sku)] = reserved
            self.rop[(loc, sku)] = rop
        self.listed = set(self.rop)
        self.skus_at = defaultdict(list)
        for loc, sku in sorted(self.listed):
            self.skus_at[loc].append(sku)
        self.sequences = dict(state["sequences"])
        self.counts = dict(state["counts"])
        through = state["exchange_rates_through"]
        self.rates_through = date.fromisoformat(through) if through else None
        self.open_orders: dict[str, list] = {
            n: [status, {int(a): s for a, s in (x.split(":") for x in attempts.split(",") if x)}]
            for n, (status, attempts) in state["open_orders"].items()}
        self.open_po: dict[str, list] = {k: list(v) for k, v in state["open_purchase_order_lines"].items()}
        self.on_order: dict[tuple[str, str], int] = defaultdict(int)
        for loc, sku, ordered, received in self.open_po.values():
            self.on_order[(loc, sku)] += ordered - received
        self.orders: dict[str, PlannedOrder] = {}
        self.order_keys: dict[str, str] = {}             # order number -> demand key (open orders)
        self.pending_reserve: dict[tuple[str, str], int] = defaultdict(int)   # placed, not yet reserved
        self.pending_out: dict[tuple[str, str], int] = defaultdict(int)       # allocated, not yet dispatched
        self.units: list[Unit] = []
        self.lost_orders = self.lost_units = self.short_lines = 0
        self.queue: list = []
        self.counter = 0
        if carry is None:
            if self.open_orders or state["in_transit"] or self.open_po:
                raise EngineError("the start state holds open orders, transfers in transit or open purchase orders "
                                  "but no carry: a full replay needs a closed history at the origin")
        else:
            self._restore(state, carry)

    # ------------------------------------------------------------------ carry
    def _restore(self, state: dict, carry: dict) -> None:
        """Rebuild the queue and the in-flight bookkeeping from the carry; check it against the state."""
        if carry.get("version") != CARRY_VERSION or datetime.fromisoformat(carry.get("as_of", "1970-01-01T00:00:00+00:00")) \
                != self.start or carry.get("config_fingerprint") != self.ctx.config_fingerprint:
            raise CarryError("the carry belongs to another instant, engine version or configuration")
        if carry.get("sha256") != carry_digest(carry):
            raise CarryError("the carry does not match its SHA-256 (changed after it was written)")
        self.orders = {o.order_number: o for o in (decode(x) for x in carry["orders"])}
        self.order_keys = dict(carry["keys"])
        self.lost_orders, self.lost_units, self.short_lines = carry["lost"]
        entries = [decode(x) for x in carry["queue"]]
        self._check_derivation(entries)
        transit, receipts = {}, defaultdict(int)
        for at, rank, key, action, payload in entries:
            if action not in CARRIED_ACTIONS or at < self.start:
                raise CarryError(f"carry entry {action} {key} at {at} cannot continue at {self.start}")
            self.push(at, rank, key, action, payload)
            if action == "order_event":
                number, e = payload
                if number not in self.orders:
                    raise EngineError(f"carry: an event of {number} without the order")
                if e.kind == "reserve":
                    o = self.orders[number]
                    for x in o.lines:
                        self.pending_reserve[(o.fulfilment_location_code, x.sku)] += x.quantity
            elif action == "dispatch":
                for x in payload.lines:
                    self.pending_out[(payload.source, x.sku)] += x.quantity
                # numbered at a review before the window start, not dispatched yet, so not in the start state (S10):
                # the next review must continue after it
                prefix, number = payload.reference[:10], int(payload.reference[11:17])
                self.sequences[prefix] = max(self.sequences.get(prefix, 0), number)
            elif action == "arrive":
                for x in payload.lines:
                    transit[f"{payload.reference}|{x.sku}"] = (payload, x)
            elif action == "receive":
                po, d = payload
                for x in d.lines:
                    receipts[f"{po.number}|{next(l.line_number for l in po.lines if l.sku == x.sku)}"] += x.quantity
        if set(self.orders) != set(self.open_orders) or set(self.order_keys) != set(self.open_orders):
            raise CarryError(f"carry: open orders {sorted(set(self.orders) ^ set(self.open_orders))[:5]} differ "
                             "from the state")
        dispatched = {k for k, (t, _) in transit.items() if t.dispatched_at < self.start}
        if dispatched != set(state["in_transit"]):
            raise CarryError("carry: transfers in transit differ from the state")
        for k in dispatched:
            t, x = transit[k]
            if state["in_transit"][k] != x.quantity:
                raise CarryError(f"carry: {k} carries {x.quantity} units, the state {state['in_transit'][k]}")
            self.ledger.in_transit[(t.reference, x.sku)] = InTransit(t.reference, t.source, t.destination, x.sku,
                                                                     x.quantity)
        for k, (loc, sku, ordered, received) in self.open_po.items():
            if receipts.get(k, 0) != ordered - received:
                raise CarryError(f"carry: open purchase order line {k} has {ordered - received} units to receive, "
                                 f"the carried deliveries {receipts.get(k, 0)}")

    def _check_derivation(self, entries: list) -> None:
        """Every carried future action must be the one its seeded stream gives (milestone 3).

        Open orders are rebuilt from their demand draw and the lines they hold; their remaining
        events must be exactly the carried order events. Transfer and receipt times are recomputed.
        """
        expected_events = set()
        for number, o in self.orders.items():
            key = self.order_keys.get(number)
            if key is None:
                raise CarryError(f"carry: open order {number} without its demand key")
            d, placed = self.ctx.demand_order(key)
            if placed != o.placed_at or d.customer_number != o.customer_number:
                raise CarryError(f"carry: open order {number} is not the demand draw {key}")
            rebuilt = self.build_order(key, d, placed, o.sequence, [(x.sku, x.quantity) for x in o.lines])
            if rebuilt != o:
                raise CarryError(f"carry: open order {number} differs from its derivation (its planned life was changed)")
            expected_events |= {(e.at, RANK["order"], f"{o.sequence:08d}.{e.step:02d}", number, e)
                                for e in o.events() if e.at >= self.start}
        carried_events = {(at, rank, key, payload[0], payload[1])
                          for at, rank, key, action, payload in entries if action == "order_event"}
        if carried_events != expected_events:
            raise CarryError(f"carry: the open orders' remaining steps differ from their derivation "
                             f"({len(carried_events ^ expected_events)} differences)")
        for at, rank, key, action, payload in entries:
            if action in ("dispatch", "arrive"):
                t = payload
                day = t.dispatched_at.astimezone(self.tz(t.market)).date()
                times = self.transfer_times(t.market, t.destination, day)
                expected_at = times[0] if action == "dispatch" else times[1]
                if (t.dispatched_at, t.arrived_at) != times or at != expected_at or rank != RANK["T1" if action == "dispatch" else "T2"]:
                    raise CarryError(f"carry: transfer {t.reference} times differ from their derivation")
            elif action == "receive":
                po, d = payload
                day = po.ordered_at.astimezone(self.tz(po.market)).date()
                if d.received_at != self.receipt_time(po.market, po.number, day) or at != d.received_at \
                        or rank != RANK["R1"] or sorted((x.sku, x.quantity) for x in d.lines) != \
                        sorted((x.sku, x.quantity) for x in po.lines):
                    raise CarryError(f"carry: delivery {d.reference} differs from its derivation")

    def carry(self, until: datetime) -> dict:
        pending = sorted(e for e in self.queue if e[4] in CARRIED_ACTIONS)
        if any(e[0] < until for e in pending) or any(e[4] not in CARRIED_ACTIONS and e[0] >= until for e in self.queue):
            raise EngineError("internal: the queue does not end cleanly at the window end")
        open_numbers = set(self.open_orders)
        carry = {"version": CARRY_VERSION, "as_of": until.astimezone(UTC).isoformat(),
                 "config_fingerprint": self.ctx.config_fingerprint,
                 "orders": [encode(self.orders[n]) for n in sorted(open_numbers)],
                 "keys": {n: self.order_keys[n] for n in sorted(open_numbers)},
                 "queue": [encode([at, rank, key, action, payload]) for at, rank, key, _, action, payload in pending],
                 "lost": [self.lost_orders, self.lost_units, self.short_lines]}
        carry["sha256"] = carry_digest(carry)
        return carry

    # ------------------------------------------------------------------ derivations shared by the plan and the carry check
    def transfer_times(self, market: str, store: str, day: date) -> tuple[datetime, datetime]:
        rp = self.ctx.rp
        r = stream(self.seed, "activity", "dispatch", store, day.isoformat())
        dispatched = local_time(day, rp.dispatch_hours[0], self.countries[market].time_zone,
                                60 * r.randint(*DISPATCH_MINUTES) + r.randrange(60))
        return dispatched, dispatched + timedelta(seconds=r.randint(rp.transit_minutes[0] * 60, rp.transit_minutes[1] * 60))

    def receipt_time(self, market: str, number: str, review_day: date) -> datetime:
        rp = self.ctx.rp
        r = stream(self.seed, "activity", "purchase_order", number)
        first_day = on_or_next_working_day(review_day + timedelta(days=r.randint(*rp.supplier_lead_days)), rp.warehouse_days)
        window = (rp.receiving_hours[1] - rp.receiving_hours[0]) * 3600
        return local_time(first_day, rp.receiving_hours[0], self.countries[market].time_zone, r.randrange(window))

    # ------------------------------------------------------------------ helpers
    def tz(self, market: str) -> ZoneInfo:
        return ZoneInfo(self.countries[market].time_zone)

    def push(self, at: datetime, rank: int, key: str, action: str, payload) -> None:
        self.counter += 1
        heapq.heappush(self.queue, (at, rank, key, self.counter, action, payload))

    def free(self, loc: str, sku: str) -> int:
        return self.ledger.available(loc, sku) - self.pending_reserve[(loc, sku)] - self.pending_out[(loc, sku)]

    def next_number(self, prefix: str) -> int:
        self.sequences[prefix] = self.sequences.get(prefix, 0) + 1
        return self.sequences[prefix]

    def emit(self, unit: Unit) -> None:
        self.units.append(unit)

    def snapshot(self, t: datetime) -> dict:
        balances = {f"{loc}|{sku}": [self.ledger.on_hand.get((loc, sku), 0), self.ledger.reserved.get((loc, sku), 0),
                                     self.rop[(loc, sku)]] for loc, sku in self.listed}
        in_transit = {f"{x.reference}|{x.sku}": x.quantity for x in self.ledger.in_transit.values()}
        open_orders = {n: [s[0], ",".join(f"{a}:{st}" for a, st in sorted(s[1].items()))]
                       for n, s in self.open_orders.items()}
        open_po = {k: list(v) for k, v in self.open_po.items() if v[3] < v[2]}
        prices = {f"{sku}|{market}": str(p.list_price) for (sku, market), rows in self.prices.items() for p in rows
                  if p.valid_from < t and (p.valid_to is None or p.valid_to >= t)}
        counts = {k: self.counts[k] for k in COUNT_NAMES}
        # price rows dated before t: launch prices loaded with the master data (also of later launches) and P1 rows
        counts["product_prices"] = sum(1 for rows in self.prices.values() for p in rows if p.valid_from < t)
        return build_snapshot(t, balances, in_transit, open_po, open_orders, self._dispatched_sequences(), counts,
                              self.rates_through, prices)

    def _dispatched_sequences(self) -> dict:
        """The document sequences as the database holds them (state._SEQUENCES): transfer numbers count once dispatched.

        The store review numbers its transfers at 05:30 local and dispatches them from 06:00 local, so an hourly window
        can end in between (S10). Numbers still waiting in the queue for their dispatch are not reported; with none
        pending (every midnight boundary) this is self.sequences unchanged.
        """
        pending = defaultdict(set)
        for entry in self.queue:
            if entry[4] == "dispatch":
                reference = entry[5].reference
                pending[reference[:10]].add(int(reference[11:17]))
        sequences = dict(self.sequences)
        for prefix, numbers in pending.items():
            n = sequences.get(prefix, 0)
            while n in numbers:
                n -= 1
            if n > 0:
                sequences[prefix] = n
            else:
                sequences.pop(prefix, None)       # nothing of this prefix dispatched yet: absent, as in the database
        return sequences

    # ------------------------------------------------------------------ schedule
    def schedule(self, until: datetime) -> None:
        """Calendar actions and demand draws in [start, until); carried actions are already queued."""
        first_day, last_day = self.start.date(), (until - timedelta(microseconds=1)).date()
        rp = self.ctx.rp
        day = first_day
        while day <= last_day:
            entries = [(datetime.combine(day, dtime(0), UTC), RANK["X1"], day.isoformat(), "x1", day)]
            for market in self.markets:
                if day.isoweekday() in rp.warehouse_days:
                    at = datetime.combine(day, dtime(*STORE_REVIEW), self.tz(market)).astimezone(UTC)
                    entries.append((at, RANK["T1"], f"review-{market}", "store_review", (market, day)))
                if day.isoweekday() == rp.warehouse_review_weekday:
                    at = local_time(day, rp.warehouse_review_hour, self.countries[market].time_zone)
                    entries.append((at, RANK["PO"], f"review-{market}", "warehouse_review", (market, day)))
            if day == rcal_day(day.year, day.month):
                for market in self.markets:
                    at = local_time(day, self.ctx.activity.rcal_local_hour, self.countries[market].time_zone)
                    entries.append((at, RANK["RCAL"], f"rcal-{market}", "rcal", (market, day)))
            for entry in entries:
                if self.start <= entry[0] < until:
                    self.push(*entry)
            day += DAY
        for change in self.ctx.price_changes:
            if self.start <= change.at < until:
                self.push(change.at, RANK["P1"], f"{change.sku}|{change.market}", "p1", change)
        months = sorted({(d.year, d.month) for d in (first_day - DAY, last_day + DAY)})
        y, mth = months[0]
        while (y, mth) <= months[-1]:
            for key, o, placed in self.ctx.orders_of_month(y, mth):
                if self.start <= placed < until:
                    self.push(placed, RANK["order"], key, "place", (key, o))
            y, mth = (y + 1, 1) if mth == 12 else (y, mth + 1)

    # ------------------------------------------------------------------ X1
    def do_x1(self, at: datetime, day: date) -> None:
        self.emit(Unit(at, RANK["X1"], day.isoformat(), "X1", "X1 exchange rates", day))
        self.counts["exchange_rates"] += 3
        self.rates_through = day

    # ------------------------------------------------------------------ P1 and R-cal
    def do_p1(self, at: datetime, c: PriceChange) -> None:
        """The price timeline already holds the change (orders see it from `at`); the unit writes it."""
        self.emit(Unit(at, RANK["P1"], f"{c.sku}|{c.market}", "P1",
                       f"P1 {c.sku} {c.market} markdown {c.step} ({c.markdown * 100:.1f}%)", c))

    def do_rcal(self, at: datetime, payload) -> None:
        market, day = payload
        new = self.ctx.rcal(day)
        wh = self.warehouse_of[market]
        for loc in [wh] + self.stores_of[market]:
            changes = tuple((sku, self.rop[(loc, sku)], new[(loc, sku)]) for sku in self.skus_at[loc]
                            if (loc, sku) in new and new[(loc, sku)] != self.rop[(loc, sku)])
            if not changes:
                continue
            cap = self.capacity.get(loc)
            if loc == wh and cap is not None and sum(new.get((loc, s), self.rop[(loc, s)]) for s in self.skus_at[loc]) > cap:
                continue                                             # capacity guard: keep the current reorder points
            reference = f"RCAL-{loc}-{day:%Y%m%d}"
            self.emit(Unit(at, RANK["RCAL"], reference, "RCAL", f"{reference} R-cal ({len(changes)} listings)",
                           ReorderPointReview(loc, at, reference, changes)))
            for sku, _, value in changes:
                self.rop[(loc, sku)] = value
            self.counts["reorder_point_revisions"] += len(changes)

    # ------------------------------------------------------------------ orders
    def do_place(self, at: datetime, payload) -> None:
        key, d = payload
        loc = d.location_code
        lines = []
        for sku, quantity in d.lines:
            take = min(quantity, max(0, self.free(loc, sku)))
            if take < quantity:
                self.short_lines += 1
                self.lost_units += quantity - take
            if take > 0:
                lines.append((sku, take))
        if not lines:
            self.lost_orders += 1
            return
        sequence = self.sequences.get("order", 0) + 1
        self.sequences["order"] = sequence
        order = self.build_order(key, d, at, sequence, lines)
        self.orders[order.order_number] = order
        self.order_keys[order.order_number] = key
        for x in order.lines:
            self.pending_reserve[(loc, x.sku)] += x.quantity
        for e in order.events():
            if e.kind == "place":
                self.apply_order_event(order, e)
            else:
                self.push(e.at, RANK["order"], f"{sequence:08d}.{e.step:02d}", "order_event", (order.order_number, e))

    def do_order_event(self, at: datetime, payload) -> None:
        number, e = payload
        self.apply_order_event(self.orders[number], e)

    def apply_order_event(self, o: PlannedOrder, e) -> None:
        loc = o.fulfilment_location_code
        self.emit(Unit(e.at, RANK["order"], f"{o.sequence:08d}.{e.step:02d}", e.kind,
                       f"{o.order_number} {e.label()}", o, e.attempt_number))
        state = self.open_orders.get(o.order_number)
        closed = False
        if e.kind == "place":
            self.open_orders[o.order_number] = ["placed", {}]
            self.counts["orders"] += 1
            self.counts["order_items"] += len(o.lines)
        elif e.kind == "reserve":
            for x in o.lines:
                self.pending_reserve[(loc, x.sku)] -= x.quantity
            if self.ledger.reserve(loc, o.lines) != "reserved":
                raise EngineError(f"{o.order_number}: simulated U2 found no stock although the order was sized to it")
            state[0] = "stock_reserved"
            self.counts["reservations"] += len(o.lines)
        elif e.kind == "payment_start":
            state[1][e.attempt_number] = "pending"
            self.counts["payment_attempts"] += 1
        elif e.kind == "payment_result":
            a = o.attempts[e.attempt_number - 1]
            state[1][e.attempt_number] = a.outcome
            if a.outcome == "captured":
                state[0] = "confirmed"
            elif o.final_status == "payment_failed" and e.attempt_number == o.attempts[-1].attempt_number:
                self.ledger.release(loc, o.lines)
                closed = True
        elif e.kind in ("customer_cancel", "expire"):
            self.ledger.release(loc, o.lines)
            closed = True
        elif e.kind == "fulfil":
            self.ledger.issue(loc, o.lines)
            self.counts["movements"] += len(o.lines)
            closed = True
        else:
            raise EngineError(f"unexpected order event {e.kind}")
        if closed:
            del self.open_orders[o.order_number]
            del self.orders[o.order_number]                 # only open orders are kept (and carried)
            del self.order_keys[o.order_number]

    def build_order(self, key: str, d, placed: datetime, sequence: int, chosen) -> PlannedOrder:
        tx, seed = self.ctx.tx, self.seed
        market, channel = d.market, d.channel
        country = self.countries[market]
        tz = self.tz(market)
        number = f"{market}-{placed.astimezone(tz).year:04d}-{sequence:08d}"
        r = stream(seed, "activity", "timeline", key)
        rate = country.vat_rate
        lines = []
        for n, (sku, quantity) in enumerate(chosen, start=1):
            p = price_at(self.prices, sku, market, placed)
            if p is None:
                raise EngineError(f"{sku} has no {market} price at {placed.isoformat()}")
            total = money(p.list_price * quantity)
            lines.append(PlannedLine(n, sku, quantity, p.list_price, p.unit_cost, ZERO, ZERO, total, rate,
                                     money(total * rate / (1 + rate))))
        items_total = money(sum((x.line_total for x in lines), ZERO))
        fee = money(tx.shipping_fee[market]) if channel == "online" and items_total < tx.free_shipping_from[market] else ZERO
        order_total = items_total + fee

        reserve_at = placed + timedelta(seconds=r.randint(2, 8))
        lifetime = tx.reservation_minutes[channel]
        expires = reserve_at + timedelta(minutes=lifetime)
        attempts: list[PlannedAttempt] = []
        outcomes = []
        if d.outcome == "fulfilled":
            outcomes = ["failed"] * d.failed_attempts + ["captured"]
        elif d.outcome == "payment_failed":
            outcomes = ["failed"] * d.attempts
        at = reserve_at + timedelta(seconds=r.randint(30, 240) if channel == "online" else r.randint(15, 60))
        methods = tx.payment_methods[channel]
        failing = {m: w for m, w in methods.items() if m in FAILING_METHODS}
        method = None
        for n, outcome in enumerate(outcomes, start=1):
            if outcome == "captured":
                method = pick(r, list(methods), list(methods.values()))
            elif method is None or method not in failing or not chance(r, Decimal("0.5")):
                method = pick(r, list(failing), list(failing.values()))
            reference = f"{PROVIDER_PREFIX[method]}-{hashlib.sha256(f'{seed}|{number}|{n}'.encode()).hexdigest()[:20].upper()}"
            if outcome == "captured":
                authorized = at + timedelta(seconds=r.randint(2, 6))
                captured = authorized + timedelta(seconds=r.randint(1, 2))
                attempts.append(PlannedAttempt(n, method, tx.providers[method], order_total, "captured", at, authorized,
                                               captured, None, None, reference))
            else:
                reasons = [x for x in REASONS_BY_METHOD[method] if x in tx.failure_reasons] or list(tx.failure_reasons)
                reason = reasons[r.randrange(len(reasons))]
                failed = at + timedelta(seconds=r.randint(20, 30) if reason == "timeout" else r.randint(3, 10))
                attempts.append(PlannedAttempt(n, method, tx.providers[method], order_total, "failed", at, None, None,
                                               failed, reason, reference))
                at = failed + timedelta(seconds=r.randint(20, 90) if channel == "online" else r.randint(20, 45))

        confirmed = fulfilled = cancelled = closed = None
        status, reason, close_status = "cancelled", None, None
        if d.outcome == "fulfilled":
            status, close_status = "fulfilled", "consumed"
            confirmed = attempts[-1].captured_at
            if channel == "in_store":
                fulfilled = confirmed + timedelta(seconds=r.randint(30, 120))
            else:
                local = confirmed.astimezone(tz)
                if local.hour < tx.fulfilment_cutoff_hour:
                    fulfilled = confirmed + timedelta(seconds=r.randint(3600, 4 * 3600))
                else:
                    nxt = local.date() + DAY
                    fulfilled = (datetime.combine(nxt, dtime(9), tz) + timedelta(seconds=r.randint(0, 3 * 3600))
                                 ).astimezone(UTC)
            closed = fulfilled
        elif d.outcome == "payment_failed":
            status, close_status = "payment_failed", "released"
            cancelled = closed = attempts[-1].failed_at
        elif d.outcome == "cancelled":
            reason, close_status = "customer_request", "released"
            cancelled = closed = reserve_at + timedelta(seconds=r.randint(60, min(600, lifetime * 60 - 60)))
        elif d.outcome == "expired":
            reason, close_status = "reservation_expired", "expired"
            cancelled = closed = expires + timedelta(seconds=r.randint(5, 55))
        else:
            raise EngineError(f"unknown demand outcome {d.outcome}")

        customer = self.customers[d.customer_number]
        online = channel == "online"
        return PlannedOrder(
            sequence=sequence, order_number=number, scenario=f"demand:{d.outcome}", customer_number=d.customer_number,
            channel=channel, store_code=None if online else d.location_code, fulfilment_location_code=d.location_code,
            country_code=market, currency_code=country.currency_code, lines=tuple(lines), items_total=items_total,
            shipping_fee=fee, shipping_vat_amount=money(fee * rate / (1 + rate)), order_total=order_total,
            shipping_address_line1=customer.address_line1 if online else None,
            shipping_postal_code=customer.postal_code if online else None,
            shipping_city=customer.city if online else None,
            placed_at=placed, reserve_at=reserve_at, reserve_outcome="reserved", reservation_expires_at=expires,
            attempts=tuple(attempts), final_status=status, cancellation_reason=reason, confirmed_at=confirmed,
            fulfilled_at=fulfilled, cancelled_at=cancelled, reservation_close_status=close_status,
            reservation_closed_at=closed)

    # ------------------------------------------------------------------ store replenishment
    def order_up_to(self, loc: str, sku: str, day: date) -> int:
        """S of a listing at a review on local `day`: from the R-cal in force (S8.1), else the planning figures."""
        x = self.ctx.listings[(loc, sku)]
        inv = self.ctx.inv
        cover = self.ctx.cover_units(day)
        kind = x.location_type
        if cover is None:                                    # before the first R-cal: planning weekly units x weeks
            weekly, weeks = (self.ctx.echelon[(loc, sku)] if kind == "warehouse" else x.expected_weekly_units,
                             inv.weeks_of_cover[kind])
        else:                                                # forecast units over the whole cover, x 1
            weekly, weeks = cover.get((loc, sku), ZERO), Decimal(1)
        if kind == "warehouse":
            return policy.warehouse_order_up_to(weekly, weeks, inv.warehouse_minimum_units, self.rop[(loc, sku)])
        return policy.store_order_up_to(weekly, weeks, display_minimum(inv, x.price_eur, self.formats[loc]),
                                        self.rop[(loc, sku)])

    def do_store_review(self, at: datetime, payload) -> None:
        market, day = payload
        rp = self.ctx.rp
        wh = self.warehouse_of[market]
        requests = []
        for store in self.stores_of[market]:
            for sku in self.skus_at[store]:
                if not policy.eligible(self.products[sku], self.locations[store], day, rp.pre_launch_days["store"]):
                    continue
                position = self.ledger.available(store, sku) + self.ledger.in_transit_to(store, sku)
                if not policy.needs_replenishment(position, self.rop[(store, sku)]):
                    continue
                big_s = self.order_up_to(store, sku, day)
                quantity = big_s - position
                if quantity > 0:
                    requests.append(policy.StoreRequest(store, sku, quantity, at, self.ledger.available(store, sku),
                                                        position, self.rop[(store, sku)], big_s,
                                                        self.formats[store] == "flagship"))
        given = defaultdict(list)
        for sku in sorted({q.sku for q in requests}):
            protected = self.rop[(wh, sku)] if rp.protect_warehouse_reorder_point else 0
            for request, quantity in policy.allocate([q for q in requests if q.sku == sku], self.free(wh, sku), protected):
                if quantity > 0:
                    given[request.store].append((request, quantity))
        tz = self.countries[market].time_zone
        timed = []
        for store in sorted(given):
            dispatched, arrived = self.transfer_times(market, store, day)
            timed.append((dispatched, store, arrived))
        for dispatched, store, arrived in sorted(timed):
            year = dispatched.astimezone(ZoneInfo(tz)).year
            prefix = f"TR-{market}-{year:04d}"
            reference = f"{prefix}-{self.next_number(prefix):06d}-{store}"
            lines = tuple(TransferLine(n, q.sku, q.quantity, quantity, q.available, q.position, q.reorder_point,
                                       q.order_up_to)
                          for n, (q, quantity) in enumerate(sorted(given[store], key=lambda g: g[0].sku), start=1))
            t = TransferDocument(reference, market, wh, store, at, dispatched, arrived, "reorder_point", lines)
            for x in lines:
                self.pending_out[(wh, x.sku)] += x.quantity
            self.push(dispatched, RANK["T1"], reference, "dispatch", t)
            self.push(arrived, RANK["T2"], reference, "arrive", t)

    def do_dispatch(self, at: datetime, t: TransferDocument) -> None:
        self.emit(Unit(at, RANK["T1"], t.reference, "T1", f"{t.reference} T1 dispatch", t))
        for x in t.lines:
            self.pending_out[(t.source, x.sku)] -= x.quantity
            self.ledger.dispatch(t.reference, t.source, t.destination, x.sku, x.quantity)
            self.counts["movements"] += 1

    def do_arrive(self, at: datetime, t: TransferDocument) -> None:
        self.emit(Unit(at, RANK["T2"], t.reference, "T2", f"{t.reference} T2 arrival", t))
        for x in t.lines:
            self.ledger.arrive(t.reference, x.sku)
            self.counts["movements"] += 1

    # ------------------------------------------------------------------ warehouse replenishment
    def do_warehouse_review(self, at: datetime, payload) -> None:
        market, day = payload
        rp = self.ctx.rp
        wh = self.warehouse_of[market]
        earliest = day + timedelta(days=rp.supplier_lead_days[0])
        lines = []
        for sku in self.skus_at[wh]:
            product = self.products[sku]
            if not policy.eligible(product, self.locations[wh], earliest, rp.pre_launch_days["warehouse"]):
                continue
            position = self.ledger.available(wh, sku) + self.on_order[(wh, sku)]
            s, big_s = self.rop[(wh, sku)], self.order_up_to(wh, sku, day)
            if policy.needs_replenishment(position, s) and big_s > position:
                pack = rp.case_packs[product.category_code]
                lines.append(PurchaseOrderLine(0, sku, policy.round_up_to_case(big_s - position, pack),
                                               "launch" if product.launched_on > day else "reorder_point",
                                               position, s, big_s, pack))
        if not lines:
            return
        cap = self.capacity.get(wh)
        if cap is not None:
            held = self.ledger.total_on_hand(wh) + sum(self.on_order[(wh, s)] for s in self.skus_at[wh])
            kept = []
            for x in lines:
                if held + x.quantity <= cap:
                    kept.append(x)
                    held += x.quantity
            lines = kept
            if not lines:
                return
        tz = self.countries[market].time_zone
        year = at.astimezone(ZoneInfo(tz)).year
        prefix = f"PO-{market}-{year:04d}"
        number = f"{prefix}-{self.next_number(prefix):06d}"
        numbered = tuple(replace(x, line_number=n) for n, x in enumerate(lines, start=1))
        received = self.receipt_time(market, number, day)
        delivery = Delivery(f"{number}/DN1", 1, received, tuple(DeliveryLine(x.sku, x.quantity) for x in numbered))
        po = PurchaseOrder(number, market, wh, at, False, numbered, (delivery,))
        self.emit(Unit(at, RANK["PO"], number, "PO", f"{number} purchase order ({len(numbered)} lines)", po))
        self.counts["purchase_orders"] += 1
        self.counts["purchase_order_lines"] += len(numbered)
        for x in numbered:
            self.on_order[(wh, x.sku)] += x.quantity
            self.open_po[f"{number}|{x.line_number}"] = [wh, x.sku, x.quantity, 0]
        self.push(received, RANK["R1"], delivery.reference, "receive", (po, delivery))

    def do_receive(self, at: datetime, payload) -> None:
        po, d = payload
        self.emit(Unit(at, RANK["R1"], d.reference, "R1", f"{d.reference} R1 receipt", (po, d)))
        by_sku = {x.sku: x.line_number for x in po.lines}
        for x in d.lines:
            self.ledger.receive(po.warehouse, x.sku, x.quantity)
            self.on_order[(po.warehouse, x.sku)] -= x.quantity
            line = self.open_po[f"{po.number}|{by_sku[x.sku]}"]
            line[3] += x.quantity
            if line[3] >= line[2]:
                del self.open_po[f"{po.number}|{by_sku[x.sku]}"]
            self.counts["movements"] += 1


def simulate(ctx: EngineContext, state: dict, until: datetime, instants=(), carry: dict | None = None) -> Simulation:
    """Replay from the instant of `state` (the origin, or a checkpoint with its carry) to `until` (exclusive).

    Snapshots are taken at the requested instants; the carry at `until` is returned for its checkpoint.
    """
    start = datetime.fromisoformat(state["as_of"])
    if until <= start:
        raise EngineError(f"nothing to replay: {until.isoformat()} is not after {start.isoformat()}")
    sim = _Sim(ctx, state, start, carry)
    sim.schedule(until)
    wanted = sorted(set(t for t in instants if start <= t <= until))
    snapshots = {}
    while sim.queue and sim.queue[0][0] < until:
        at = sim.queue[0][0]
        while wanted and wanted[0] <= at:
            t = wanted.pop(0)
            snapshots[t] = sim.snapshot(t)
        at, rank, key, _, action, payload = heapq.heappop(sim.queue)
        getattr(sim, "do_" + action)(at, payload)
    for t in wanted:
        snapshots[t] = sim.snapshot(t)
    problems = sim.ledger.problems()
    if problems:
        raise EngineError("simulated stock breaks the V007 rules: " + "; ".join(problems[:5]))
    return Simulation(start=start, units=sorted(sim.units, key=lambda u: u.sort_key), snapshots=snapshots,
                      carry=sim.carry(until), lost_orders=sim.lost_orders, lost_units=sim.lost_units,
                      short_lines=sim.short_lines)


def unit_counts(units: list[Unit]) -> dict[str, int]:
    out: dict[str, int] = {}
    for u in units:
        out[u.kind] = out.get(u.kind, 0) + 1
    return dict(sorted(out.items()))
