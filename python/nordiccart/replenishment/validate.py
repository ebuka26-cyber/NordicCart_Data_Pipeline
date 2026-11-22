"""Validation of the planned replenishment before anything is written.

Checked independently of the generator (an empty list means safe to load):
  base        the plan starts from the stock and reorder points left by the pilot transactions
  window      every unit of work after the pilot's last event, inside the window, not in the future
  revision    R0 recomputed here: echelon reorder point per warehouse listing (online + the market's
              stores, lead time x safety factor, floor 2), below the opening quantity of stocked
              listings, old value = the opening reorder point, run before the first store review
  documents   unique references in the agreed formats (TR-<market>-<year>-<seq>-<destination>,
              <PO number>/DN<n>), consecutive numbering per market and year, at most 64 characters
  routes      receipts at the market warehouse; transfers from the market warehouse to a store of
              the same market; destination in the reference = arrival store
  calendar    locations open and active; products launched (or in the pre-launch window) and not
              discontinued; reviews, dispatches and receipts on working days in their hours;
              arrivals Monday to Saturday, the same morning, before the store opens
  policy      transfer lines: position <= reorder point, requested = S - position, S recomputed;
              purchase order lines: natural ones at or below the reorder point, forced ones above it
              and flagged forced_reorder, quantity = S - position rounded up to the case pack;
              forced purchase orders and split deliveries exactly as configured
  stock       an independent replay in business-time order: never negative, reserved units never
              moved, warehouses keep their reorder point, capacity respected, every arrival matches
              its dispatch, conservation (on hand + in transit = after-pilot stock + received) after
              every unit of work, nothing in transit or on order at the end
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from ..inventory.config import InventorySettings
from ..inventory.generate import display_minimum
from ..inventory.model import OpeningInventory
from ..master_data.model import MasterDataset
from ..master_data.validate import Issue
from ..transactions.model import TransactionPlan
from .config import ReplenishmentSettings
from .model import ReplenishmentPlan

TRANSFER_REF = re.compile(r"^TR-(SE|NO|DK|FI)-(\d{4})-(\d{6})-(.+)$")
PO_NUMBER = re.compile(r"^PO-(SE|NO|DK|FI)-(\d{4})-(\d{6})$")


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _half_up(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def validate_replenishment(plan: ReplenishmentPlan, master: MasterDataset, inventory: OpeningInventory,
                           inv: InventorySettings, rp: ReplenishmentSettings, pilot: TransactionPlan,
                           now: datetime | None = None) -> list[Issue]:
    issues: list[Issue] = []

    def add(key: str, rule: str, detail: str) -> None:
        issues.append(Issue("replenishment", key, rule, detail))

    now = now or datetime.now(timezone.utc)
    countries = {c.country_code: c for c in master.countries}
    products = {p.sku: p for p in master.products}
    locations = {l.location_code: l for l in master.stock_locations}
    formats = {s.location_code: s.store_format for s in master.stores}
    capacity = {w.location_code: w.storage_capacity_units for w in master.warehouses}
    listings = {(x.location_code, x.sku): x for x in inventory.listings}
    warehouse_of = {l.country_code: l.location_code for l in master.stock_locations if l.location_type == "warehouse"}

    def zone(market: str) -> ZoneInfo:
        return ZoneInfo(countries[market].time_zone)

    def local(at: datetime, market: str) -> datetime:
        return at.astimezone(zone(market))

    def on_sale(sku: str, location: str, day, kind: str) -> bool:
        p, loc = products.get(sku), locations.get(location)
        if p is None or loc is None or not p.is_active or p.launched_on is None or not loc.is_active:
            return False
        if loc.opened_on > day or (p.discontinued_on is not None and p.discontinued_on <= day):
            return False
        return day >= p.launched_on - timedelta(days=rp.pre_launch_days[kind])

    # --- base ------------------------------------------------------------------------------
    if plan.start_after != pilot.last_event_at():
        add("-", "start_after", f"plan starts after {plan.start_after}, the pilot ends at {pilot.last_event_at()}")
    if plan.base_stock != pilot.replay().snapshot():
        add("-", "base_stock", "the starting stock is not the opening inventory replayed through the pilot")
    if plan.base_reorder_points != {k: x.reorder_point for k, x in listings.items()}:
        add("-", "base_reorder_points", "the starting reorder points are not the opening inventory's")

    # --- independent policy figures ------------------------------------------------------------
    store_weekly: dict[tuple[str, str], Decimal] = {}
    for x in inventory.listings:
        if x.location_type == "store":
            store_weekly[(x.country_code, x.sku)] = store_weekly.get((x.country_code, x.sku), Decimal(0)) + x.expected_weekly_units
    expected_rop = dict(plan.base_reorder_points)
    warehouse_demand: dict[tuple[str, str], Decimal] = {}
    for (loc, sku), x in listings.items():
        if x.location_type != "warehouse":
            continue
        demand = x.expected_weekly_units + (store_weekly.get((x.country_code, sku), Decimal(0))
                                            if rp.echelon_reorder_points else Decimal(0))
        warehouse_demand[(loc, sku)] = demand
        if rp.echelon_reorder_points:
            new = max(2, _ceil(demand * inv.lead_time_weeks["warehouse"] * inv.safety_factor))
            if x.opening_quantity > 0:
                new = min(new, x.opening_quantity - 1)
            expected_rop[(loc, sku)] = max(new, 0)

    def store_s(loc: str, sku: str) -> int:
        x = listings[(loc, sku)]
        minimum = display_minimum(inv, x.price_eur, formats[loc])
        return max(minimum, _half_up(x.expected_weekly_units * inv.weeks_of_cover["store"]), x.reorder_point + 1)

    def warehouse_s(loc: str, sku: str) -> int:
        return max(inv.warehouse_minimum_units, _half_up(warehouse_demand[(loc, sku)] * inv.weeks_of_cover["warehouse"]),
                   expected_rop[(loc, sku)] + 1)

    # --- window ---------------------------------------------------------------------------------
    market_of: dict[str, str] = {r.warehouse: r.market for r in plan.revisions}
    market_of.update({t.reference: t.market for t in plan.transfers})
    market_of.update({d.reference: po.market for po, d in plan.deliveries()})
    for e in plan.events():
        day = local(e.at, market_of[e.key]).date()
        if e.at <= plan.start_after:
            add(e.key, "after_pilot", f"{e.label()} at {e.at.isoformat()} is not after the pilot ({plan.start_after.isoformat()})")
        if not rp.first_review_date <= day <= rp.end_date:
            add(e.key, "window", f"{e.label()} on {day}, outside {rp.first_review_date} to {rp.end_date}")
        if e.at >= now:
            add(e.key, "not_in_future", f"{e.label()} at {e.at.isoformat()} is in the future")

    # --- R0 ---------------------------------------------------------------------------------------
    seen = set()
    for r in plan.revisions:
        if r.warehouse in seen:
            add(r.warehouse, "revision_unique", "more than one revision for the warehouse")
        seen.add(r.warehouse)
        if warehouse_of.get(r.market) != r.warehouse:
            add(r.warehouse, "revision_location", f"{r.warehouse} is not the warehouse of {r.market}")
            continue
        lt = local(r.at, r.market)
        if lt.date() != rp.first_review_date or (lt.hour, lt.minute, lt.second) != (rp.revision_hour, 0, 0):
            add(r.warehouse, "revision_time", f"R0 at {lt.isoformat()}, expected {rp.revision_hour}:00 on {rp.first_review_date}")
        got = {c.sku: c for c in r.changes}
        want = {sku: expected_rop[(loc, sku)] for (loc, sku) in listings
                if loc == r.warehouse and expected_rop[(loc, sku)] != plan.base_reorder_points[(loc, sku)]}
        if set(got) != set(want):
            add(r.warehouse, "revised_reorder_point", f"changes for {sorted(set(got) ^ set(want))} differ from the echelon rule")
        for sku, c in got.items():
            x = listings.get((r.warehouse, sku))
            if x is None:
                add(r.warehouse, "listing_missing", f"{sku} is not listed at {r.warehouse}")
                continue
            if c.old != x.reorder_point:
                add(r.warehouse, "revision_old_value", f"{sku}: old {c.old}, opening reorder point {x.reorder_point}")
            if sku in want and c.new != want[sku]:
                add(r.warehouse, "revised_reorder_point", f"{sku}: new {c.new}, echelon rule gives {want[sku]}")
            if c.new < 0 or (x.opening_quantity > 0 and c.new >= x.opening_quantity):
                add(r.warehouse, "reorder_point_below_opening", f"{sku}: {c.new} must be >= 0 and below the opening "
                                                                f"quantity {x.opening_quantity} (script 12, check 501)")
    if rp.echelon_reorder_points:
        for market, wh in warehouse_of.items():
            if wh not in seen and any(expected_rop[k] != plan.base_reorder_points[k] for k in listings if k[0] == wh):
                add(wh, "revision_missing", "echelon reorder points differ but the warehouse has no revision")

    # --- references and numbering -------------------------------------------------------------------
    def duplicates(values):
        return sorted({v for v in values if values.count(v) > 1})

    for ref in duplicates([t.reference for t in plan.transfers]):
        add(ref, "unique_reference", "transfer reference used twice")
    for ref in duplicates([p.number for p in plan.purchase_orders]):
        add(ref, "unique_reference", "purchase order number used twice")
    for ref in duplicates([d.reference for _, d in plan.deliveries()]):
        add(ref, "unique_reference", "delivery note reference used twice")

    numbering: dict[tuple[str, str, int], list[tuple[datetime, int]]] = {}
    for t in plan.transfers:
        m = TRANSFER_REF.match(t.reference)
        lt = local(t.dispatched_at, t.market) if t.market in countries else t.dispatched_at
        if not m or m.group(1) != t.market or int(m.group(2)) != lt.year or m.group(4) != t.destination \
                or len(t.reference) > 64:
            add(t.reference, "reference_format", "expected TR-<market>-<local year>-<NNNNNN>-<destination>, at most 64 characters")
        else:
            numbering.setdefault(("TR", t.market, lt.year), []).append((t.dispatched_at, int(m.group(3))))
    for p in plan.purchase_orders:
        m = PO_NUMBER.match(p.number)
        lt = local(p.ordered_at, p.market) if p.market in countries else p.ordered_at
        if not m or m.group(1) != p.market or int(m.group(2)) != lt.year:
            add(p.number, "reference_format", "expected PO-<market>-<local year>-<NNNNNN>")
        else:
            numbering.setdefault(("PO", p.market, lt.year), []).append((p.ordered_at, int(m.group(3))))
        if [d.note for d in p.deliveries] != list(range(1, len(p.deliveries) + 1)):
            add(p.number, "delivery_notes", "delivery notes must be numbered DN1..DNn")
        for d in p.deliveries:
            if d.reference != f"{p.number}/DN{d.note}" or len(d.reference) > 64:
                add(d.reference, "reference_format", f"expected {p.number}/DN{d.note}, at most 64 characters")
    for (prefix, market, year), items in numbering.items():
        numbers = [n for _, n in sorted(items)]
        if numbers != list(range(rp.first_document_sequence, rp.first_document_sequence + len(numbers))):
            add(f"{prefix}-{market}-{year}", "document_sequence", f"numbers {numbers} are not consecutive in time order "
                                                                  f"from {rp.first_document_sequence}")

    # --- transfers ------------------------------------------------------------------------------------
    for t in plan.transfers:
        src, dst = locations.get(t.source), locations.get(t.destination)
        if t.market not in countries or src is None or dst is None:
            add(t.reference, "fk_location", f"unknown market or location ({t.market}, {t.source}, {t.destination})")
            continue
        if t.source != warehouse_of.get(t.market) or src.location_type != "warehouse":
            add(t.reference, "transfer_route", f"source {t.source} is not the {t.market} warehouse")
        if dst.location_type != "store" or dst.country_code != t.market:
            add(t.reference, "transfer_route", f"destination {t.destination} is not a store of {t.market}")
        if t.trigger != "reorder_point":
            add(t.reference, "transfer_trigger", f"stores are replenished by reorder point only, not {t.trigger}")
        req, disp, arr = local(t.requested_at, t.market), local(t.dispatched_at, t.market), local(t.arrived_at, t.market)
        if req.isoweekday() == 7 or (req.hour, req.minute, req.second) != (rp.store_review_hour, 0, 0) \
                or not t.requested_at < t.dispatched_at:
            add(t.reference, "review_time", f"store review at {req.isoformat()} (Monday to Saturday, "
                                            f"{rp.store_review_hour}:00, before the dispatch)")
        if disp.isoweekday() not in rp.warehouse_days or not rp.dispatch_hours[0] <= disp.hour < rp.dispatch_hours[1]:
            add(t.reference, "dispatch_time", f"dispatched {disp:%a %H:%M} local, outside warehouse days and dispatch hours")
        transit = t.arrived_at - t.dispatched_at
        if not timedelta(minutes=rp.transit_minutes[0]) <= transit <= timedelta(minutes=rp.transit_minutes[1]):
            add(t.reference, "transit_time", f"transit {transit}, expected {rp.transit_minutes[0]} to {rp.transit_minutes[1]} minutes")
        if arr.date() != disp.date() or arr.isoweekday() == 7 or arr.hour >= rp.store_opening_hour:
            add(t.reference, "arrival_time", f"arrived {arr:%a %H:%M} local: same day as the dispatch, Monday to "
                                             f"Saturday, before the store opens at {rp.store_opening_hour}:00")
        if [x.line_number for x in t.lines] != list(range(1, len(t.lines) + 1)) or not t.lines:
            add(t.reference, "line_numbers", "lines must be numbered 1..n")
        if len({x.sku for x in t.lines}) != len(t.lines):
            add(t.reference, "unique_product", "a product appears twice (one transfer_out per product and document)")
        for x in t.lines:
            if (t.source, x.sku) not in listings or (t.destination, x.sku) not in listings:
                add(t.reference, "listing_missing", f"{x.sku} is not listed at both locations")
                continue
            if x.quantity <= 0 or x.quantity > x.requested:
                add(t.reference, "quantity", f"{x.sku}: dispatched {x.quantity}, requested {x.requested}")
            if not on_sale(x.sku, t.destination, arr.date(), "store") or not on_sale(x.sku, t.source, disp.date(), "warehouse"):
                add(t.reference, "product_on_sale", f"{x.sku} may not move on {disp.date()} (launch, discontinuation or location)")
            base_rop = plan.base_reorder_points[(t.destination, x.sku)]
            if x.reorder_point != base_rop or x.order_up_to != store_s(t.destination, x.sku):
                add(t.reference, "transfer_policy", f"{x.sku}: reorder point {x.reorder_point}/{base_rop}, "
                                                    f"order-up-to {x.order_up_to}/{store_s(t.destination, x.sku)}")
            if x.store_position > x.reorder_point:
                add(t.reference, "transfer_trigger", f"{x.sku}: position {x.store_position} is above the reorder point "
                                                     f"{x.reorder_point}")
            if x.requested != x.order_up_to - x.store_position:
                add(t.reference, "transfer_quantity", f"{x.sku}: requested {x.requested}, S - position = "
                                                      f"{x.order_up_to - x.store_position}")

    # --- purchase orders ----------------------------------------------------------------------------
    forced_by_market: dict[str, int] = {}
    for p in plan.purchase_orders:
        if p.market not in countries or p.warehouse != warehouse_of.get(p.market):
            add(p.number, "receipt_location", f"{p.warehouse} is not the warehouse of {p.market}")
            continue
        forced_by_market[p.market] = forced_by_market.get(p.market, 0) + (1 if p.forced else 0)
        od = local(p.ordered_at, p.market)
        if od.isoweekday() != rp.warehouse_review_weekday or (od.hour, od.minute, od.second) != (rp.warehouse_review_hour, 0, 0):
            add(p.number, "review_time", f"ordered {od:%a %H:%M} local, expected the warehouse review")
        if [x.line_number for x in p.lines] != list(range(1, len(p.lines) + 1)) or not p.lines:
            add(p.number, "line_numbers", "lines must be numbered 1..n")
        if len({x.sku for x in p.lines}) != len(p.lines):
            add(p.number, "unique_product", "a product appears twice")
        for x in p.lines:
            product = products.get(x.sku)
            if product is None or (p.warehouse, x.sku) not in listings:
                add(p.number, "listing_missing", f"{x.sku} is not listed at {p.warehouse}")
                continue
            pack = rp.case_packs.get(product.category_code)
            if x.case_pack != pack or x.quantity <= 0 or x.quantity % x.case_pack:
                add(p.number, "case_pack", f"{x.sku}: {x.quantity} is not a positive multiple of the case pack {pack}")
            if x.reorder_point != expected_rop[(p.warehouse, x.sku)] or x.order_up_to != warehouse_s(p.warehouse, x.sku):
                add(p.number, "po_policy", f"{x.sku}: reorder point {x.reorder_point}/{expected_rop[(p.warehouse, x.sku)]}, "
                                           f"order-up-to {x.order_up_to}/{warehouse_s(p.warehouse, x.sku)}")
            if p.forced != (x.trigger == "forced_reorder"):
                add(p.number, "forced_flag", f"{x.sku}: trigger {x.trigger} on a {'forced' if p.forced else 'natural'} order")
            if x.trigger in ("reorder_point", "launch") and x.position > x.reorder_point:
                add(p.number, "po_trigger", f"{x.sku}: position {x.position} above the reorder point {x.reorder_point}")
            if x.trigger == "forced_reorder" and x.position <= x.reorder_point:
                add(p.number, "forced_flag", f"{x.sku}: position {x.position} is at or below the reorder point, so the "
                                             "order is natural, not forced")
            if x.trigger not in ("reorder_point", "launch", "forced_reorder"):
                add(p.number, "po_trigger", f"{x.sku}: unknown trigger {x.trigger}")
            if pack and x.quantity != -(-max(x.order_up_to - x.position, 1) // pack) * pack:
                add(p.number, "po_quantity", f"{x.sku}: {x.quantity}, expected S - position rounded up to the case pack")
            delivered = sum(dl.quantity for d in p.deliveries for dl in d.lines if dl.sku == x.sku)
            if delivered != x.quantity:
                add(p.number, "delivery_total", f"{x.sku}: delivered {delivered}, ordered {x.quantity}")
        if any(dl.sku not in {x.sku for x in p.lines} or dl.quantity <= 0 for d in p.deliveries for dl in d.lines):
            add(p.number, "delivery_total", "a delivery line is not on the order or not positive")
        split = p.forced and p.market in rp.split_deliveries
        if len(p.deliveries) != (2 if split else 1):
            add(p.number, "split_count", f"{len(p.deliveries)} delivery notes, expected {2 if split else 1}")
        previous = None
        for d in p.deliveries:
            ld = local(d.received_at, p.market)
            if ld.isoweekday() not in rp.warehouse_days or not rp.receiving_hours[0] <= ld.hour < rp.receiving_hours[1]:
                add(d.reference, "receipt_time", f"received {ld:%a %H:%M} local, outside warehouse days and receiving hours")
            for dl in d.lines:
                if not on_sale(dl.sku, p.warehouse, ld.date(), "warehouse"):
                    add(d.reference, "product_on_sale", f"{dl.sku} may not be received on {ld.date()}")
            if d.note == 1:
                lead = (ld.date() - od.date()).days
                if not rp.supplier_lead_days[0] <= lead <= rp.supplier_lead_days[1] + 2:
                    add(d.reference, "lead_time", f"{lead} days after the order, expected {rp.supplier_lead_days[0]} "
                                                  f"to {rp.supplier_lead_days[1]} (+2 for a weekend)")
            elif previous is not None:
                gap, cursor = 0, previous.date()
                while cursor < ld.date():
                    cursor += timedelta(days=1)
                    gap += cursor.isoweekday() in rp.warehouse_days
                if not rp.split_delivery_gap_days[0] <= gap <= rp.split_delivery_gap_days[1]:
                    add(d.reference, "split_gap", f"{gap} working days after the previous note")
            previous = ld
    for market in sorted(set(rp.forced_reorders) | set(forced_by_market)):
        if forced_by_market.get(market, 0) != rp.forced_reorders.get(market, 0):
            add(market, "forced_count", f"{forced_by_market.get(market, 0)} forced purchase orders, configured "
                                        f"{rp.forced_reorders.get(market, 0)}")

    # --- independent stock replay ---------------------------------------------------------------------
    on_hand = {k: v[0] for k, v in plan.base_stock.items()}
    reserved = {k: v[1] for k, v in plan.base_stock.items()}
    rops = dict(plan.base_reorder_points)
    in_transit: dict[tuple[str, str], tuple[str, int]] = {}
    on_order: dict[tuple[str, str], int] = {}
    base_total, received = sum(on_hand.values()), 0
    steps = [(r.at, 0, r.warehouse, "revise", r) for r in plan.revisions]
    steps += [(d.received_at, 1, d.reference, "receive", (p, d)) for p, d in plan.deliveries()]
    steps += [(t.dispatched_at, 2, t.reference, "dispatch", t) for t in plan.transfers]
    steps += [(t.arrived_at, 3, t.reference, "arrive", t) for t in plan.transfers]
    steps += [(p.ordered_at, 4, p.number, "order", p) for p in plan.purchase_orders]
    for at, _, key, kind, item in sorted(steps, key=lambda s: s[:3]):
        if kind == "revise":
            for c in item.changes:
                rops[(item.warehouse, c.sku)] = c.new
        elif kind == "order":
            for x in item.lines:
                k = (item.warehouse, x.sku)
                position = on_hand.get(k, 0) - reserved.get(k, 0) + on_order.get(k, 0)
                if x.position != position:
                    add(key, "po_position", f"{x.sku}: recorded position {x.position}, replay gives {position}")
                on_order[k] = on_order.get(k, 0) + x.quantity
        elif kind == "receive":
            po, d = item
            for dl in d.lines:
                k = (po.warehouse, dl.sku)
                on_hand[k] = on_hand.get(k, 0) + dl.quantity
                on_order[k] = on_order.get(k, 0) - dl.quantity
                received += dl.quantity
            cap = capacity.get(po.warehouse)
            if cap is not None and sum(v for (loc, _), v in on_hand.items() if loc == po.warehouse) > cap:
                add(key, "warehouse_capacity", f"{po.warehouse} above its capacity of {cap} units")
        elif kind == "dispatch":
            for x in item.lines:
                src, dst = (item.source, x.sku), (item.destination, x.sku)
                position = on_hand.get(dst, 0) - reserved.get(dst, 0) + sum(
                    q for (ref, sku), (dest, q) in in_transit.items() if dest == item.destination and sku == x.sku)
                if x.store_position != position:
                    add(key, "transfer_position", f"{x.sku}: recorded position {x.store_position}, replay gives {position}")
                if on_hand.get(src, 0) - reserved.get(src, 0) < x.quantity:
                    add(key, "stock_negative", f"{x.sku}: {item.source} has {on_hand.get(src, 0) - reserved.get(src, 0)} "
                                               f"available, {x.quantity} dispatched")
                on_hand[src] = on_hand.get(src, 0) - x.quantity
                in_transit[(item.reference, x.sku)] = (item.destination, x.quantity)
                if rp.protect_warehouse_reorder_point and on_hand[src] - reserved.get(src, 0) < rops.get(src, 0):
                    add(key, "warehouse_protection", f"{x.sku}: {item.source} left with {on_hand[src]}, below its "
                                                     f"reorder point {rops.get(src, 0)}")
        elif kind == "arrive":
            for x in item.lines:
                entry = in_transit.pop((item.reference, x.sku), None)
                if entry is None or entry != (item.destination, x.quantity):
                    add(key, "transfer_pair", f"{x.sku}: arrival without a matching dispatch of {x.quantity} units")
                    continue
                on_hand[(item.destination, x.sku)] = on_hand.get((item.destination, x.sku), 0) + x.quantity
        if any(v < 0 for v in on_hand.values()) or any(reserved.get(k, 0) > on_hand.get(k, 0) for k in reserved):
            add(key, "stock_negative", f"negative stock or more reserved than on hand after {kind}")
        if sum(on_hand.values()) + sum(q for _, q in in_transit.values()) != base_total + received:
            add(key, "conservation", "on hand + in transit differs from the stock after the pilot + units received")
    if in_transit:
        add("-", "in_transit_end", f"{len(in_transit)} transfer lines are still in transit at the end of the window")
    if any(on_order.values()):
        add("-", "on_order_end", "purchase order lines are not fully received at the end of the window")
    return issues
