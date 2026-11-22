"""Replenishment generator: a business-time simulation of reviews, transfers and supplier deliveries.

It starts from the stock left by the pilot transactions (opening inventory replayed through
the 100 pilot units of work) and runs, in UTC order, the actions of every market:

  R0 revision        revision_hour local on first_review_date: warehouse reorder points become
                     echelon reorder points (online + the market's stores), below the opening quantity
  store review       store_review_hour local, Monday to Saturday: every listing whose inventory
                     position <= reorder point asks its market warehouse for S - IP units
  dispatch (T1)      next warehouse working day, dispatch_hours local: requests are allocated
                     (stores at 0 first, then flagships, then lowest ratio; the warehouse keeps
                     its own reorder point) and leave as one transfer document per store
  arrival (T2)       the same morning, transit_minutes later, before the store opens
  warehouse review   Monday warehouse_review_hour local: natural purchase orders (IP <= reorder
                     point), plus the pilot's forced purchase orders at the first review
  receipt (R1)       supplier_lead_days later on a working day in receiving_hours; a split
                     delivery arrives as DN1 and, split_delivery_gap_days working days later, DN2

A review is skipped when its follow-ups could not finish inside the window, so nothing is
in transit or on order at the end. Every random choice has its own seeded stream.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..inventory.config import InventorySettings
from ..inventory.generate import display_minimum
from ..inventory.model import OpeningInventory
from ..master_data.model import MasterDataset
from ..master_data.rules import UTC, stream
from ..settings import Settings
from ..stock.ledger import StockError, StockLedger
from ..transactions.model import TransactionPlan
from . import policy
from .config import ReplenishmentSettings
from .model import (
    Delivery, DeliveryLine, PurchaseOrder, PurchaseOrderLine, ReorderPointChange, ReorderPointRevision,
    ReplenishmentPlan, ReviewRecord, TransferDocument, TransferLine,
)

ACTION_RANK = {"revise": 0, "receive": 1, "dispatch": 2, "arrive": 3, "warehouse_review": 4, "store_review": 5}


class GenerationError(RuntimeError):
    """The settings and the stock after the pilot do not allow the requested replenishment pilot."""


def local_time(day: date, hour: int, time_zone: str, seconds: int = 0) -> datetime:
    return (datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(time_zone)) + timedelta(hours=hour, seconds=seconds)
            ).astimezone(UTC)


def next_working_day(day: date, working: tuple[int, ...]) -> date:
    """First working day strictly after `day`."""
    d = day + timedelta(days=1)
    while d.isoweekday() not in working:
        d += timedelta(days=1)
    return d


def on_or_next_working_day(day: date, working: tuple[int, ...]) -> date:
    return day if day.isoweekday() in working else next_working_day(day, working)


def add_working_days(day: date, n: int, working: tuple[int, ...]) -> date:
    d = day
    for _ in range(n):
        d = next_working_day(d, working)
    return d


def generate_replenishment(settings: Settings, inv: InventorySettings, rp: ReplenishmentSettings,
                           master: MasterDataset, inventory: OpeningInventory,
                           pilot: TransactionPlan) -> ReplenishmentPlan:
    seed = settings.seed
    countries = {c.country_code: c for c in master.countries}
    products = {p.sku: p for p in master.products}
    locations = {l.location_code: l for l in master.stock_locations}
    formats = {s.location_code: s.store_format for s in master.stores}
    capacity = {w.location_code: w.storage_capacity_units for w in master.warehouses}
    listings = {(x.location_code, x.sku): x for x in inventory.listings}

    start_after = pilot.last_event_at()
    if start_after is None:
        raise GenerationError("the pilot transaction plan has no events; replenishment starts after them")
    base_snapshot = pilot.replay().snapshot()
    ledger = StockLedger.from_snapshot(base_snapshot)
    if any(reserved for _, reserved in base_snapshot.values()):
        raise GenerationError("the pilot leaves active reservations; replenishment needs a closed pilot")

    warehouse_of: dict[str, str] = {}
    stores_of: dict[str, list[str]] = defaultdict(list)
    for loc in sorted(master.stock_locations, key=lambda l: l.location_code):
        if loc.location_type == "warehouse":
            if loc.country_code in warehouse_of:
                raise GenerationError(f"{loc.country_code} has more than one warehouse; the pilot expects one")
            warehouse_of[loc.country_code] = loc.location_code
        else:
            stores_of[loc.country_code].append(loc.location_code)
    markets = sorted(m for m in warehouse_of if m in countries)
    skus_at: dict[str, list[str]] = defaultdict(list)
    for (loc, sku) in sorted(listings):
        skus_at[loc].append(sku)

    def tz(market: str) -> str:
        return countries[market].time_zone

    # --- policy parameters -----------------------------------------------------------------
    base_rop = {k: x.reorder_point for k, x in listings.items()}
    rop = dict(base_rop)
    order_up_to: dict[tuple[str, str], int] = {}
    echelon = policy.echelon_weekly(inventory.listings)
    revisions: list[ReorderPointRevision] = []
    for market in markets:
        wh = warehouse_of[market]
        changes = []
        for sku in skus_at[wh]:
            x = listings[(wh, sku)]
            demand = echelon[(wh, sku)] if rp.echelon_reorder_points else x.expected_weekly_units
            new = (policy.echelon_reorder_point(demand, inv.lead_time_weeks["warehouse"], inv.safety_factor, 2,
                                                x.opening_quantity)
                   if rp.echelon_reorder_points else x.reorder_point)
            order_up_to[(wh, sku)] = policy.warehouse_order_up_to(
                demand, inv.weeks_of_cover["warehouse"], inv.warehouse_minimum_units, new)
            if new != x.reorder_point:
                changes.append(ReorderPointChange(sku, x.reorder_point, new, demand))
        if changes:
            at = local_time(rp.first_review_date, rp.revision_hour, tz(market))
            if at <= start_after:
                raise GenerationError(f"R0 for {wh} at {at.isoformat()} is not after the pilot ({start_after.isoformat()})")
            revisions.append(ReorderPointRevision(market, wh, at, tuple(changes)))
    for (loc, sku), x in listings.items():
        if x.location_type == "store":
            minimum = display_minimum(inv, x.price_eur, formats[loc])
            order_up_to[(loc, sku)] = policy.store_order_up_to(x.expected_weekly_units, inv.weeks_of_cover["store"],
                                                               minimum, rop[(loc, sku)])

    # --- action queue ------------------------------------------------------------------------
    queue: list[tuple] = []
    counter = [0]

    def push(at: datetime, action: str, payload) -> None:
        counter[0] += 1
        heapq.heappush(queue, (at, ACTION_RANK[action], counter[0], action, payload))

    for r in revisions:
        push(r.at, "revise", r)
    day = rp.first_review_date
    while day <= rp.end_date:
        for market in markets:
            if day.isoweekday() != 7:                                   # stores are closed on Sundays
                push(local_time(day, rp.store_review_hour, tz(market)), "store_review", (market, day))
            if day.isoweekday() == rp.warehouse_review_weekday:
                push(local_time(day, rp.warehouse_review_hour, tz(market)), "warehouse_review", (market, day))
        day += timedelta(days=1)

    pending: dict[tuple[str, date], list[policy.StoreRequest]] = defaultdict(list)
    pending_qty: dict[tuple[str, str], int] = defaultdict(int)
    dispatch_scheduled: set[tuple[str, date]] = set()
    on_order: dict[tuple[str, str], int] = defaultdict(int)
    sequences: dict[tuple[str, str, int], int] = {}
    forced_done: set[str] = set()
    transfers: list[TransferDocument] = []
    purchase_orders: list[PurchaseOrder] = []
    reviews: list[ReviewRecord] = []
    backorders: list[str] = []

    def next_number(prefix: str, market: str, year: int) -> int:
        key = (prefix, market, year)
        sequences[key] = sequences.get(key, rp.first_document_sequence - 1) + 1
        return sequences[key]

    def stock_call(fn, *args):
        try:
            return fn(*args)
        except StockError as exc:
            raise GenerationError(str(exc)) from None

    # --- actions -------------------------------------------------------------------------------
    def store_review(at: datetime, market: str, day: date) -> None:
        dispatch_day = next_working_day(day, rp.warehouse_days)
        if dispatch_day > rp.end_date:
            reviews.append(ReviewRecord(at, "store", market, 0, f"skipped: next dispatch {dispatch_day} is after the window"))
            return
        wh = warehouse_of[market]
        found = 0
        for store in stores_of[market]:
            for sku in skus_at[store]:
                key = (store, sku)
                if not policy.eligible(products[sku], locations[store], dispatch_day, rp.pre_launch_days["store"]):
                    continue
                available = ledger.available(store, sku)
                position = available + pending_qty[key] + ledger.in_transit_to(store, sku)
                if not policy.needs_replenishment(position, rop[key]):
                    continue
                quantity = order_up_to[key] - position
                if quantity <= 0:
                    continue
                pending[(wh, dispatch_day)].append(policy.StoreRequest(
                    store, sku, quantity, at, available, position, rop[key], order_up_to[key],
                    formats[store] == "flagship"))
                pending_qty[key] += quantity
                found += 1
        reviews.append(ReviewRecord(at, "store", market, found, f"dispatch {dispatch_day}" if found else ""))
        if pending.get((wh, dispatch_day)) and (wh, dispatch_day) not in dispatch_scheduled:
            dispatch_scheduled.add((wh, dispatch_day))
            push(local_time(dispatch_day, rp.dispatch_hours[0], tz(market)), "dispatch", (market, wh, dispatch_day))

    def dispatch(market: str, wh: str, day: date) -> None:
        requests = pending.pop((wh, day), [])
        given: dict[str, list[tuple[policy.StoreRequest, int]]] = defaultdict(list)
        for sku in sorted({r.sku for r in requests}):
            protected = rop[(wh, sku)] if rp.protect_warehouse_reorder_point else 0
            for request, quantity in policy.allocate([r for r in requests if r.sku == sku],
                                                     ledger.available(wh, sku), protected):
                pending_qty[(request.store, sku)] -= request.quantity
                if quantity > 0:
                    given[request.store].append((request, quantity))
                else:
                    backorders.append(f"{request.store}/{sku}: {request.quantity} requested at "
                                      f"{request.requested_at.isoformat()}, nothing above {wh}'s protected stock")
        timed = []
        for store in sorted(given):
            r = stream(seed, "replenishment", "dispatch", wh, day.isoformat(), store)
            window = (rp.dispatch_hours[1] - rp.dispatch_hours[0]) * 3600
            dispatched = local_time(day, rp.dispatch_hours[0], tz(market), r.randrange(window))
            arrived = dispatched + timedelta(seconds=r.randint(rp.transit_minutes[0] * 60, rp.transit_minutes[1] * 60))
            timed.append((dispatched, store, arrived))
        for dispatched, store, arrived in sorted(timed):
            year = dispatched.astimezone(ZoneInfo(tz(market))).year
            reference = f"TR-{market}-{year:04d}-{next_number('TR', market, year):06d}-{store}"
            lines = tuple(
                TransferLine(n, request.sku, request.quantity, quantity, request.available, request.position,
                             request.reorder_point, request.order_up_to)
                for n, (request, quantity) in enumerate(sorted(given[store], key=lambda g: g[0].sku), start=1))
            document = TransferDocument(reference, market, wh, store, min(g[0].requested_at for g in given[store]),
                                        dispatched, arrived, "reorder_point", lines)
            for x in lines:
                stock_call(ledger.dispatch, reference, wh, store, x.sku, x.quantity)
            transfers.append(document)
            push(arrived, "arrive", document)

    def warehouse_review(at: datetime, market: str, day: date) -> None:
        wh = warehouse_of[market]
        split = market in rp.split_deliveries
        latest = add_working_days(on_or_next_working_day(day + timedelta(days=rp.supplier_lead_days[1]), rp.warehouse_days),
                                  rp.split_delivery_gap_days[1] if split else 0, rp.warehouse_days)
        if latest > rp.end_date:
            reviews.append(ReviewRecord(at, "warehouse", market, 0,
                                        f"skipped: a delivery could arrive on {latest}, after the window"))
            return
        earliest_arrival = day + timedelta(days=rp.supplier_lead_days[0])
        natural, candidates = [], []
        for sku in skus_at[wh]:
            key = (wh, sku)
            product = products[sku]
            if not policy.eligible(product, locations[wh], earliest_arrival, rp.pre_launch_days["warehouse"]):
                continue
            position = ledger.available(wh, sku) + on_order[key]
            pack = rp.case_packs[product.category_code]
            if policy.needs_replenishment(position, rop[key]) and order_up_to[key] > position:
                trigger = "launch" if product.launched_on > day else "reorder_point"
                natural.append(PurchaseOrderLine(0, sku, policy.round_up_to_case(order_up_to[key] - position, pack),
                                                 trigger, position, rop[key], order_up_to[key], pack))
            else:
                candidates.append((position - rop[key], sku, position, pack))
        forced = []
        if market not in forced_done:
            forced_done.add(market)
            picks = []
            for _, sku, position, pack in sorted(candidates):
                quantity = policy.round_up_to_case(max(order_up_to[(wh, sku)] - position, 1), pack)
                if split and quantity < 2:
                    continue                    # a split delivery needs at least two units
                picks.append(PurchaseOrderLine(0, sku, quantity, "forced_reorder", position, rop[(wh, sku)],
                                               order_up_to[(wh, sku)], pack))
            forced = picks[:rp.forced_reorders.get(market, 0)]
        made = 0
        for lines, is_forced in ((natural, False), (forced, True)):
            if not lines:
                continue
            year = at.astimezone(ZoneInfo(tz(market))).year
            number = f"PO-{market}-{year:04d}-{next_number('PO', market, year):06d}"
            numbered = tuple(PurchaseOrderLine(n, x.sku, x.quantity, x.trigger, x.position, x.reorder_point,
                                               x.order_up_to, x.case_pack) for n, x in enumerate(lines, start=1))
            if capacity.get(wh) is not None and \
                    ledger.total_on_hand(wh) + sum(on_order[(wh, s)] for s in skus_at[wh]) + sum(x.quantity for x in numbered) > capacity[wh]:
                raise GenerationError(f"{number}: {wh} would exceed its storage capacity of {capacity[wh]} units")
            r = stream(seed, "replenishment", "purchase_order", number)
            first_day = on_or_next_working_day(day + timedelta(days=r.randint(*rp.supplier_lead_days)), rp.warehouse_days)
            window = (rp.receiving_hours[1] - rp.receiving_hours[0]) * 3600
            first_at = local_time(first_day, rp.receiving_hours[0], tz(market), r.randrange(window))
            if is_forced and split:
                if sum(x.quantity for x in numbered) < 2:
                    raise GenerationError(f"{number}: a delivery of one unit cannot be split")
                part1 = tuple(DeliveryLine(x.sku, -(-x.quantity // 2)) for x in numbered)
                part2 = tuple(DeliveryLine(x.sku, x.quantity - -(-x.quantity // 2)) for x in numbered
                              if x.quantity - -(-x.quantity // 2) > 0)
                second_day = add_working_days(first_day, r.randint(*rp.split_delivery_gap_days), rp.warehouse_days)
                second_at = local_time(second_day, rp.receiving_hours[0], tz(market), r.randrange(window))
                deliveries = (Delivery(f"{number}/DN1", 1, first_at, part1), Delivery(f"{number}/DN2", 2, second_at, part2))
            else:
                deliveries = (Delivery(f"{number}/DN1", 1, first_at,
                                       tuple(DeliveryLine(x.sku, x.quantity) for x in numbered)),)
            po = PurchaseOrder(number, market, wh, at, is_forced, numbered, deliveries)
            purchase_orders.append(po)
            for x in numbered:
                on_order[(wh, x.sku)] += x.quantity
            for d in deliveries:
                push(d.received_at, "receive", (po, d))
            made += len(numbered)
        reviews.append(ReviewRecord(at, "warehouse", market, made,
                                    f"natural {len(natural)}, forced {len(forced)}" if made else ""))

    def receive(po: PurchaseOrder, delivery: Delivery) -> None:
        for x in delivery.lines:
            stock_call(ledger.receive, po.warehouse, x.sku, x.quantity)
            on_order[(po.warehouse, x.sku)] -= x.quantity
        if capacity.get(po.warehouse) is not None and ledger.total_on_hand(po.warehouse) > capacity[po.warehouse]:
            raise GenerationError(f"{delivery.reference}: {po.warehouse} exceeds its storage capacity")

    while queue:
        at, _, _, action, payload = heapq.heappop(queue)
        if action == "revise":
            for c in payload.changes:
                rop[(payload.warehouse, c.sku)] = c.new
        elif action == "store_review":
            store_review(at, *payload)
        elif action == "dispatch":
            dispatch(*payload)
        elif action == "arrive":
            for x in payload.lines:
                stock_call(ledger.arrive, payload.reference, x.sku)
        elif action == "warehouse_review":
            warehouse_review(at, *payload)
        elif action == "receive":
            receive(*payload)

    if ledger.units_in_transit() or any(on_order.values()) or any(pending.values()):
        raise GenerationError("the window ends with stock in transit, on order or requested; "
                              "move end_date or the review schedule")
    for market in markets:
        if rp.forced_reorders.get(market) and market not in forced_done:
            raise GenerationError(f"{market}: no warehouse review inside the window for the forced purchase orders")

    transfers.sort(key=lambda t: (t.dispatched_at, t.reference))
    purchase_orders.sort(key=lambda p: (p.ordered_at, p.number))
    return ReplenishmentPlan(
        profile=settings.profile, seed=seed, start_after=start_after, first_review_date=rp.first_review_date,
        end_date=rp.end_date, revisions=revisions, transfers=transfers, purchase_orders=purchase_orders,
        base_stock=base_snapshot, base_reorder_points=base_rop, reviews=reviews, backorders=backorders,
        pilot_order_numbers=tuple(o.order_number for o in pilot.orders),
        pilot_last_sequence=max((o.sequence for o in pilot.orders), default=0))
