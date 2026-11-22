"""Pilot transaction generator: customer orders and their complete life, in business time.

How an order is built (all seeded, see rules.stream; Decimal money, ROUND_HALF_UP)
  1. Scenario: the counts in [transactions.scenarios] are shuffled over the orders. Some
     scenarios fix the channel: customer_cancelled, reservation_expired and
     out_of_stock_quantity are online checkouts; out_of_stock_reserved happens in a store,
     next to a fulfilled in-store order that takes the last unit (its "holder").
  2. When and where: a day in the order window, online between online_hours, in store
     during store_hours and never on a Sunday. The market follows the customer mix; an
     online order ships from its market's warehouse, an in-store order is fulfilled from
     the selling store (flagships weigh double).
  3. Who: an active customer of that market registered before the order (online orders
     need a delivery address, which becomes the shipping address); in-store customers
     living in the store's city are three times as likely.
  4. What: products on sale on that local day with free stock at the fulfilment location,
     weighted by the location's expected weekly demand from the opening inventory model.
     1 to 3 lines; cheap items (under 50 EUR) sometimes two at a time.
  5. Price snapshot: list price and unit cost valid at placed_at in the market currency,
     the market VAT rate, line VAT = round(line_total * rate / (1 + rate), 2). Online
     orders pay the market shipping fee unless the items reach the free-shipping threshold.
  6. Life: U2 reserve a few seconds after placing (expires after 15 min online, 5 in
     store); payment attempts within the reservation lifetime; capture confirms the order;
     online orders are picked and packed the same afternoon (confirmed before the cutoff
     hour) or next morning, in-store orders are handed over within two minutes.

Stock is simulated event by event (StockLedger) in the same order the loader writes, so
each U2 outcome is known in advance: reserved for every scenario except the two
out_of_stock ones, where it must be out_of_stock. The plan is then checked independently
by validate.py and, at load time, by the database itself.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..inventory.model import OpeningInventory
from ..master_data.model import Customer, MasterDataset, ProductPrice
from ..master_data.rules import UTC, money, stream
from ..settings import Settings
from .config import TransactionSettings
from .model import PlannedAttempt, PlannedLine, PlannedOrder, StockLedger, TransactionPlan

FORCED_CHANNEL = {
    "customer_cancelled": "online",
    "reservation_expired": "online",
    "out_of_stock_quantity": "online",
    "out_of_stock_reserved": "in_store",
}
ATTEMPT_OUTCOMES = {
    "fulfilled": ("captured",),
    "payment_retry_fulfilled": ("failed", "captured"),
}
FAILING_METHODS = ("card", "mobile_payment")
REASONS_BY_METHOD = {"card": ("card_declined", "insufficient_funds"), "mobile_payment": ("timeout", "insufficient_funds")}
PROVIDER_PREFIX = {"card": "SPC", "mobile_payment": "SPM", "buy_now_pay_later": "SPI", "cash": "TILL"}
CHEAP_EUR = Decimal(50)
ZERO = Decimal("0.00")


class GenerationError(RuntimeError):
    """The settings and master data do not allow the requested pilot (for example no customer)."""


def pick(rng, items, weights):
    """Weighted choice with integer arithmetic only (same result on every platform)."""
    pairs = [(item, Decimal(w)) for item, w in zip(items, weights) if Decimal(w) > 0]
    if not pairs:
        raise GenerationError("nothing to choose from")
    ints = [max(1, int((w * 10000).to_integral_value())) for _, w in pairs]
    r = rng.randrange(sum(ints))
    for (item, _), w in zip(pairs, ints):
        if r < w:
            return item
        r -= w
    raise AssertionError("unreachable")


def chance(rng, share: Decimal) -> bool:
    """True with probability `share`, without floats."""
    return rng.randrange(1_000_000) < int(share * 1_000_000)


def allocate(items: list[str], weights: list[Decimal], count: int, rng) -> list[str]:
    """`count` items in proportion to their weights (largest remainder), in seeded order.

    Used for the payment method of captured payments, so a small pilot still shows the
    configured mix (and every method) instead of whatever a few independent draws give.
    """
    total = sum(weights)
    exact = [w * count / total for w in weights]
    counts = [int(x) for x in exact]
    by_remainder = sorted(range(len(items)), key=lambda i: (-(exact[i] - counts[i]), i))
    for i in by_remainder[:count - sum(counts)]:
        counts[i] += 1
    out = [item for item, n in zip(items, counts) for _ in range(n)]
    rng.shuffle(out)
    return out


def seconds(rng, low: int, high: int) -> timedelta:
    return timedelta(seconds=rng.randint(low, high))


def price_at(prices: dict[tuple[str, str], list[ProductPrice]], sku: str, country: str, at: datetime) -> ProductPrice | None:
    for p in prices.get((sku, country), ()):
        if p.valid_from <= at and (p.valid_to is None or at < p.valid_to):
            return p
    return None


@dataclass
class _Slot:
    index: int
    scenario: str
    channel: str = ""
    market: str = ""
    location_code: str = ""
    store_code: str | None = None
    placed_at: datetime | None = None
    customer: Customer | None = None
    holder: "_Slot | None" = None
    is_holder: bool = False
    capture_method: str | None = None
    order: PlannedOrder | None = None
    lines: list[PlannedLine] = field(default_factory=list)


def generate_transactions(settings: Settings, tx: TransactionSettings, master: MasterDataset,
                          inventory: OpeningInventory, store_format_weight: dict[str, Decimal]) -> TransactionPlan:
    seed = settings.seed
    countries = {c.country_code: c for c in master.countries}
    locations = {l.location_code: l for l in master.stock_locations}
    store_format = {s.location_code: s.store_format for s in master.stores}
    ships_online = {w.location_code for w in master.warehouses if w.ships_online_orders}
    products = {p.sku: p for p in master.products}
    prices: dict[tuple[str, str], list[ProductPrice]] = {}
    for p in master.product_prices:
        prices.setdefault((p.sku, p.country_code), []).append(p)
    weekly = {(x.location_code, x.sku): x.expected_weekly_units for x in inventory.listings}
    price_eur = {x.sku: x.price_eur for x in inventory.listings}
    opening = {(x.location_code, x.sku): x.opening_quantity for x in inventory.listings}
    markets = [m for m in sorted(settings.customer.country_weights) if m in countries]
    market_weights = [settings.customer.country_weights[m] for m in markets]
    days = [tx.start_date + timedelta(days=d) for d in range(tx.days)]

    def tz(market: str) -> ZoneInfo:
        return ZoneInfo(countries[market].time_zone)

    def local_day(market: str, at: datetime) -> date:
        return at.astimezone(tz(market)).date()

    # 1. Scenarios -----------------------------------------------------------------------
    names = [s for s, n in tx.scenarios.items() for _ in range(n)]
    rng = stream(seed, "transactions", "scenarios")
    rng.shuffle(names)
    slots = [_Slot(index=i, scenario=s) for i, s in enumerate(names)]
    fulfilled = [s for s in slots if s.scenario == "fulfilled"]
    for oos in (s for s in slots if s.scenario == "out_of_stock_reserved"):
        candidates = [s for s in fulfilled if not s.is_holder]
        holder = candidates[rng.randrange(len(candidates))]
        holder.is_holder = True
        oos.holder = holder

    # 2. When, where and who ---------------------------------------------------------------
    def choose_customer(r, market: str, at: datetime, channel: str, store_city: str | None,
                        exclude: set[str]) -> Customer:
        eligible = [c for c in master.customers
                    if c.country_code == market and c.is_active and c.registered_at <= at
                    and c.customer_number not in exclude
                    and (channel == "in_store" or (c.address_line1 and c.postal_code and c.city))]
        if not eligible:
            raise GenerationError(f"no eligible {market} customer for an order at {at.isoformat()}")
        weights = [3 if store_city and c.city == store_city else 1 for c in eligible]
        return pick(r, eligible, weights)

    def place_slot(slot: _Slot) -> None:
        r = stream(seed, "transactions", "slot", slot.index)
        slot.channel = FORCED_CHANNEL.get(slot.scenario) or ("in_store" if slot.is_holder else
                                                              "online" if chance(r, tx.online_share)
                                                              else "in_store")
        slot.market = pick(r, markets, market_weights)
        if slot.channel == "online":
            day = days[r.randrange(len(days))]
            h0, h1 = tx.online_hours
            local = datetime(day.year, day.month, day.day, tzinfo=tz(slot.market)) + \
                timedelta(hours=h0, seconds=r.randrange((h1 - h0) * 3600 - 15 * 60))
            warehouses = sorted(l.location_code for l in master.stock_locations
                                if l.country_code == slot.market and l.location_code in ships_online
                                and l.is_active and l.opened_on <= day)
            if not warehouses:
                raise GenerationError(f"no warehouse ships online orders in {slot.market}")
            slot.location_code = warehouses[0] if len(warehouses) == 1 else warehouses[r.randrange(len(warehouses))]
            slot.store_code = None
        else:
            open_days = [d for d in days if d.weekday() != 6]
            if not open_days:
                raise GenerationError("the order window has no store day (stores are closed on Sundays)")
            day = open_days[r.randrange(len(open_days))]
            stores = [l for l in master.stock_locations
                      if l.country_code == slot.market and l.location_type == "store" and l.is_active and l.opened_on <= day]
            if slot.is_holder:
                stores = [l for l in stores if store_format[l.location_code] == "standard"] or stores
            if not stores:
                raise GenerationError(f"no store open in {slot.market} on {day}")
            store = pick(r, stores, [store_format_weight[store_format[l.location_code]] for l in stores])
            h0, h1 = tx.store_hours
            local = datetime(day.year, day.month, day.day, tzinfo=tz(slot.market)) + \
                timedelta(hours=h0, seconds=r.randrange((h1 - h0) * 3600 - 20 * 60))
            slot.location_code = slot.store_code = store.location_code
        slot.placed_at = local.astimezone(UTC)
        store_city = locations[slot.store_code].city if slot.store_code else None
        slot.customer = choose_customer(r, slot.market, slot.placed_at, slot.channel, store_city, set())

    for slot in slots:
        if slot.holder is None:
            place_slot(slot)
    for slot in slots:
        if slot.holder is not None:
            r = stream(seed, "transactions", "slot", slot.index)
            h = slot.holder
            slot.channel, slot.market = "in_store", h.market
            slot.location_code = slot.store_code = h.store_code
            slot.placed_at = h.placed_at + seconds(r, 20, 35)
            slot.customer = choose_customer(r, slot.market, slot.placed_at, "in_store",
                                            locations[h.store_code].city, {h.customer.customer_number})

    slots.sort(key=lambda s: (s.placed_at, s.index))
    for channel in ("online", "in_store"):
        paying = [s for s in slots if s.channel == channel and s.scenario in ATTEMPT_OUTCOMES]
        methods = tx.payment_methods[channel]
        for slot, method in zip(paying, allocate(list(methods), list(methods.values()), len(paying),
                                                 stream(seed, "transactions", "capture_methods", channel))):
            slot.capture_method = method

    # 3. Order numbers and timelines ---------------------------------------------------------
    for position, slot in enumerate(slots):
        sequence = tx.first_order_sequence + position
        number = f"{slot.market}-{local_day(slot.market, slot.placed_at).year:04d}-{sequence:08d}"
        slot.order = _timeline(seed, tx, slot, sequence, number, local_day, tz)

    # 4. Lines, decided at placing time against the simulated stock --------------------------
    by_number = {s.order.order_number: s for s in slots}
    ledger = StockLedger(opening)
    pending: dict[tuple[str, str], int] = {}

    def free(loc: str, sku: str) -> int:
        return ledger.available(loc, sku) - pending.get((loc, sku), 0)

    def on_sale(slot: _Slot) -> list[str]:
        day = local_day(slot.market, slot.placed_at)
        return [sku for sku, p in sorted(products.items())
                if p.is_active and p.launched_on is not None and p.launched_on <= day
                and (p.discontinued_on is None or p.discontinued_on > day)
                and price_at(prices, sku, slot.market, slot.placed_at) is not None]

    def make_line(slot: _Slot, number: int, sku: str, quantity: int) -> PlannedLine:
        p = price_at(prices, sku, slot.market, slot.placed_at)
        rate = countries[slot.market].vat_rate
        total = money(p.list_price * quantity)
        return PlannedLine(line_number=number, sku=sku, quantity=quantity, unit_price=p.list_price,
                           unit_cost=p.unit_cost, discount_percent=ZERO, discount_amount=ZERO, line_total=total,
                           vat_rate=rate, vat_amount=money(total * rate / (1 + rate)))

    def choose_lines(slot: _Slot) -> list[PlannedLine]:
        r = stream(seed, "transactions", "lines", slot.index)
        loc = slot.location_code
        skus = on_sale(slot)
        if slot.scenario == "out_of_stock_reserved":
            sku = slot.holder.lines[0].sku
            return [make_line(slot, 1, sku, 1)]
        if slot.is_holder:
            last_unit = [s for s in skus if free(loc, s) == 1]
            if not last_unit:
                raise GenerationError(f"{loc} has no product with exactly one unit left for the out_of_stock_reserved pair")
            sku = pick(r, last_unit, [weekly[(loc, s)] for s in last_unit])
            return [make_line(slot, 1, sku, 1)]
        if slot.scenario == "out_of_stock_quantity":
            stocked = [s for s in skus if free(loc, s) > 0]
            fewest = min(free(loc, s) for s in stocked)
            short = pick(r, [s for s in stocked if free(loc, s) == fewest],
                         [weekly[(loc, s)] for s in stocked if free(loc, s) == fewest])
            others = [s for s in stocked if s != short]
            first = pick(r, others, [weekly[(loc, s)] for s in others])
            return [make_line(slot, 1, first, 1), make_line(slot, 2, short, free(loc, short) + 1)]
        candidates = [s for s in skus if free(loc, s) > 0]
        count = min(pick(r, list(tx.lines_per_order), list(tx.lines_per_order.values())), len(candidates))
        lines: list[PlannedLine] = []
        for number in range(1, count + 1):
            sku = pick(r, candidates, [weekly[(loc, s)] for s in candidates])
            candidates.remove(sku)
            quantity = 2 if (price_eur[sku] < CHEAP_EUR and free(loc, sku) >= 2
                             and chance(r, tx.two_units_share)) else 1
            lines.append(make_line(slot, number, sku, quantity))
        if not lines:
            raise GenerationError(f"nothing in stock at {loc} for order {slot.order.order_number}")
        return lines

    skeletons = {s.order.order_number: s.order for s in slots}
    events = sorted((e for o in skeletons.values() for e in o.events()), key=lambda e: e.sort_key)
    for event in events:
        slot = by_number[event.order_number]
        if event.kind == "place":
            slot.lines = choose_lines(slot)
            slot.order = replace(slot.order, lines=tuple(slot.lines))
            if slot.order.reserve_outcome == "reserved":
                for x in slot.lines:
                    pending[(slot.location_code, x.sku)] = pending.get((slot.location_code, x.sku), 0) + x.quantity
            continue
        if event.kind == "reserve" and slot.order.reserve_outcome == "reserved":
            for x in slot.lines:
                pending[(slot.location_code, x.sku)] -= x.quantity
        outcome = ledger.apply(slot.order, event)
        if event.kind == "reserve" and outcome != slot.order.reserve_outcome:
            raise GenerationError(f"{slot.order.order_number}: simulated U2 gave {outcome}, "
                                  f"expected {slot.order.reserve_outcome}")

    # 5. Totals, payments and the shipping address -----------------------------------------
    orders = [_finish(slot, tx, countries) for slot in slots]
    return TransactionPlan(profile=settings.profile, seed=seed, start_date=tx.start_date, end_date=tx.end_date,
                           orders=orders, opening_stock=opening)


def _timeline(seed: int, tx: TransactionSettings, slot: _Slot, sequence: int, number: str, local_day, tz) -> PlannedOrder:
    """Every timestamp of the order's life, from the scenario (lines and amounts come later)."""
    r = stream(seed, "transactions", "timeline", slot.index)
    placed = slot.placed_at
    reserve_at = placed + seconds(r, 2, 8)
    lifetime = timedelta(minutes=tx.reservation_minutes[slot.channel])
    oos = slot.scenario in ("out_of_stock_quantity", "out_of_stock_reserved")
    expires = reserve_at + lifetime       # requested in U2; no reservation exists when it is out_of_stock

    if slot.scenario in ATTEMPT_OUTCOMES:
        outcomes = ATTEMPT_OUTCOMES[slot.scenario]
    elif slot.scenario == "payment_failed":
        # Online checkouts retry up to the limit; in a store the customer gives up after two tries.
        outcomes = ("failed",) * (tx.max_payment_attempts if slot.channel == "online" else 2)
    else:
        outcomes = ()

    attempts: list[PlannedAttempt] = []
    at = reserve_at + (seconds(r, 30, 240) if slot.channel == "online" else seconds(r, 15, 60))
    method = None
    methods = tx.payment_methods[slot.channel]
    failing = {m: w for m, w in methods.items() if m in FAILING_METHODS}
    for n, outcome in enumerate(outcomes, start=1):
        if outcome == "captured":
            method = slot.capture_method
        elif method is None or not chance(r, Decimal("0.5")):    # a retry often uses the same card
            method = pick(r, list(failing), list(failing.values()))
        digest = hashlib.sha256(f"{seed}|{number}|{n}".encode()).hexdigest()[:20].upper()
        reference = f"{PROVIDER_PREFIX[method]}-{digest}"
        if outcome == "captured":
            authorized = at + seconds(r, 2, 6)
            captured = authorized + seconds(r, 1, 2)
            attempts.append(PlannedAttempt(n, method, tx.providers[method], ZERO, "captured", at, authorized,
                                           captured, None, None, reference))
        else:
            reasons = [x for x in REASONS_BY_METHOD[method] if x in tx.failure_reasons] or list(tx.failure_reasons)
            reason = reasons[r.randrange(len(reasons))]
            failed = at + (seconds(r, 20, 30) if reason == "timeout" else seconds(r, 3, 10))
            attempts.append(PlannedAttempt(n, method, tx.providers[method], ZERO, "failed", at, None, None,
                                           failed, reason, reference))
            at = failed + seconds(r, 20, 90)

    confirmed = fulfilled = cancelled = closed = None
    status, reason, close_status = "cancelled", None, None
    if slot.scenario in ("fulfilled", "payment_retry_fulfilled"):
        status, close_status = "fulfilled", "consumed"
        confirmed = attempts[-1].captured_at
        if slot.channel == "in_store":
            fulfilled = confirmed + (seconds(r, 60, 120) if slot.is_holder else seconds(r, 30, 120))
        else:
            local = confirmed.astimezone(tz(slot.market))
            if local.hour < tx.fulfilment_cutoff_hour:
                fulfilled = confirmed + seconds(r, 3600, 4 * 3600)
            else:
                next_day = local.date() + timedelta(days=1)
                fulfilled = (datetime(next_day.year, next_day.month, next_day.day, tzinfo=tz(slot.market))
                             + timedelta(hours=9) + seconds(r, 0, 3 * 3600)).astimezone(UTC)
        closed = fulfilled
    elif slot.scenario == "payment_failed":
        status, close_status = "payment_failed", "released"
        cancelled = closed = attempts[-1].failed_at
    elif slot.scenario == "customer_cancelled":
        reason, close_status = "customer_request", "released"
        cancelled = closed = reserve_at + seconds(r, 60, 600)
    elif slot.scenario == "reservation_expired":
        reason, close_status = "reservation_expired", "expired"
        cancelled = closed = expires + seconds(r, 5, 55)
    else:  # out of stock: U2 changed nothing; a new transaction cancels the order
        reason = "out_of_stock"
        cancelled = reserve_at + timedelta(seconds=1)

    return PlannedOrder(
        sequence=sequence, order_number=number, scenario=slot.scenario,
        customer_number=slot.customer.customer_number, channel=slot.channel, store_code=slot.store_code,
        fulfilment_location_code=slot.location_code, country_code=slot.market, currency_code="",
        lines=(), items_total=ZERO, shipping_fee=ZERO, shipping_vat_amount=ZERO, order_total=ZERO,
        shipping_address_line1=None, shipping_postal_code=None, shipping_city=None,
        placed_at=placed, reserve_at=reserve_at, reserve_outcome="out_of_stock" if oos else "reserved",
        reservation_expires_at=expires, attempts=tuple(attempts), final_status=status, cancellation_reason=reason,
        confirmed_at=confirmed, fulfilled_at=fulfilled, cancelled_at=cancelled,
        reservation_close_status=close_status, reservation_closed_at=closed,
        holder_order_number=None,
    )


def _finish(slot: _Slot, tx: TransactionSettings, countries) -> PlannedOrder:
    order = slot.order
    country = countries[order.country_code]
    items_total = money(sum((x.line_total for x in order.lines), ZERO))
    fee = ZERO
    if order.channel == "online" and items_total < tx.free_shipping_from[order.country_code]:
        fee = money(tx.shipping_fee[order.country_code])
    rate = country.vat_rate
    total = items_total + fee
    customer = slot.customer
    online = order.channel == "online"
    return replace(
        order,
        currency_code=country.currency_code,
        items_total=items_total,
        shipping_fee=fee,
        shipping_vat_amount=money(fee * rate / (1 + rate)),
        order_total=total,
        shipping_address_line1=customer.address_line1 if online else None,
        shipping_postal_code=customer.postal_code if online else None,
        shipping_city=customer.city if online else None,
        attempts=tuple(replace(a, amount=total) for a in order.attempts),
        holder_order_number=slot.holder.order.order_number if slot.holder else None,
    )
