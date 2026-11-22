"""Validation of the planned transactions before anything is written.

Checked independently of the generator (an empty list means safe to load):
  plan          order count and scenario mix as configured; unique order numbers in the
                format CC-YYYY-NNNNNNNN, numbered in placed_at order; unique provider references
  references    customer, products, locations and stock listings exist in the master data
                and the opening inventory
  consistency   customer of the order market, active, registered before the order; product
                launched and active on the local order day; location open and active; order
                window, opening stock time, nothing in the future; online: a warehouse that
                ships online orders, during online hours, shipped to the customer's address;
                in store: the store's own stock, during opening hours, not on a Sunday
  money         market currency and VAT rate; price and cost snapshot = the product_prices row
                valid at placed_at; line and VAT arithmetic (ROUND_HALF_UP, V006 CHECKs);
                items total, shipping fee rule, shipping VAT, order total; 2 decimals
  lifecycle     U2 a few seconds after placing; reservation lifetime by channel; attempts
                numbered, at most max_payment_attempts, amount = order total, provider by
                method, cash only in store; at most one capture and only on the last attempt;
                payment results before the reservation expires; each scenario ends in its
                status, reason and timestamps; the out_of_stock_reserved order runs while its
                holder still holds the last unit
  stock         an independent replay of every event from the opening stock: each U2 outcome
                (reserved, out_of_stock) as planned, no negative stock, reserved never above
                on hand
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..inventory.model import OpeningInventory
from ..master_data.model import MasterDataset
from ..master_data.rules import local_midnight_utc, money
from ..master_data.validate import Issue
from .config import TransactionSettings
from .model import PlannedOrder, TransactionPlan

ORDER_NUMBER = re.compile(r"^(SE|NO|DK|FI)-(\d{4})-(\d{8})$")
FINAL = {
    "fulfilled": ("fulfilled", None, "consumed"),
    "payment_retry_fulfilled": ("fulfilled", None, "consumed"),
    "payment_failed": ("payment_failed", None, "released"),
    "customer_cancelled": ("cancelled", "customer_request", "released"),
    "reservation_expired": ("cancelled", "reservation_expired", "expired"),
    "out_of_stock_quantity": ("cancelled", "out_of_stock", None),
    "out_of_stock_reserved": ("cancelled", "out_of_stock", None),
}
ZERO = Decimal("0.00")
RESERVE_WITHIN = timedelta(minutes=1)
IN_STORE_HANDOVER = timedelta(minutes=5)
ONLINE_DISPATCH = timedelta(hours=48)


def _two_decimals(value: Decimal) -> bool:
    return value == value.quantize(Decimal("0.01"))


def validate_transactions(plan: TransactionPlan, master: MasterDataset, inventory: OpeningInventory,
                          tx: TransactionSettings) -> list[Issue]:
    issues: list[Issue] = []

    def add(order: PlannedOrder | None, rule: str, detail: str) -> None:
        issues.append(Issue("transactions", order.order_number if order else "-", rule, detail))

    countries = {c.country_code: c for c in master.countries}
    customers = {c.customer_number: c for c in master.customers}
    products = {p.sku: p for p in master.products}
    locations = {l.location_code: l for l in master.stock_locations}
    warehouses = {w.location_code: w for w in master.warehouses}
    listings = {(x.location_code, x.sku): x for x in inventory.listings}
    prices: dict[tuple[str, str], list] = {}
    for p in master.product_prices:
        prices.setdefault((p.sku, p.country_code), []).append(p)
    now = datetime.now(timezone.utc)

    # --- plan level -----------------------------------------------------------------------
    if len(plan.orders) != tx.order_count:
        add(None, "order_count", f"{len(plan.orders)} orders, expected {tx.order_count}")
    for scenario, expected in tx.scenarios.items():
        got = sum(1 for o in plan.orders if o.scenario == scenario)
        if got != expected:
            add(None, "scenario_count", f"{scenario}: {got} orders, expected {expected}")
    unknown = {o.scenario for o in plan.orders} - set(FINAL)
    if unknown:
        add(None, "scenario_count", f"unknown scenarios {sorted(unknown)}")
    numbers = [o.order_number for o in plan.orders]
    for n in sorted({n for n in numbers if numbers.count(n) > 1}):
        add(None, "unique_order_number", f"{n} appears {numbers.count(n)} times")
    refs = [a.provider_reference for o in plan.orders for a in o.attempts]
    for r in sorted({r for r in refs if refs.count(r) > 1}):
        add(None, "unique_provider_reference", f"{r} appears {refs.count(r)} times")
    ordered = sorted(plan.orders, key=lambda o: (o.placed_at, o.sequence))
    for position, o in enumerate(ordered):
        if o.sequence != tx.first_order_sequence + position:
            add(o, "order_sequence", f"sequence {o.sequence}, expected {tx.first_order_sequence + position} "
                                     "(numbered in placed_at order)")

    # --- per order ------------------------------------------------------------------------
    for o in plan.orders:
        country = countries.get(o.country_code)
        if country is None:
            add(o, "fk_country", f"unknown market {o.country_code}")
            continue
        zone = ZoneInfo(country.time_zone)
        local = o.placed_at.astimezone(zone)
        day = local.date()

        m = ORDER_NUMBER.match(o.order_number)
        if not m or m.group(1) != o.country_code or int(m.group(2)) != day.year or int(m.group(3)) != o.sequence \
                or len(o.order_number) > 30:
            add(o, "order_number_format", "expected CC-YYYY-NNNNNNNN with the market, local year and sequence")

        # who
        c = customers.get(o.customer_number)
        if c is None:
            add(o, "fk_customer", f"unknown customer {o.customer_number}")
        else:
            if c.country_code != o.country_code:
                add(o, "customer_market", f"customer of {c.country_code} ordering in {o.country_code}")
            if not c.is_active:
                add(o, "customer_active", "customer is not active")
            if c.registered_at > o.placed_at:
                add(o, "customer_registered", f"placed {o.placed_at.isoformat()} before registration "
                                              f"{c.registered_at.isoformat()}")
            if o.channel == "online" and (o.shipping_address_line1, o.shipping_postal_code, o.shipping_city) != \
                    (c.address_line1, c.postal_code, c.city):
                add(o, "online_address", "shipping address must be the customer's address")

        # where and when
        loc = locations.get(o.fulfilment_location_code)
        if loc is None:
            add(o, "fk_location", f"unknown location {o.fulfilment_location_code}")
            continue
        if loc.country_code != o.country_code:
            add(o, "fulfilment_country", f"{loc.location_code} is in {loc.country_code}")
        if not loc.is_active or loc.opened_on > day:
            add(o, "location_open", f"{loc.location_code} is not open on {day}")
        if o.channel == "online":
            if o.store_code is not None or loc.location_type != "warehouse" or \
                    not warehouses.get(loc.location_code) or not warehouses[loc.location_code].ships_online_orders:
                add(o, "online_fulfilment", "online orders ship from a warehouse that ships online orders, without a store")
            if not o.shipping_address_line1 or not o.shipping_postal_code or not o.shipping_city:
                add(o, "online_address", "online orders need a shipping address")
            if not tx.online_hours[0] <= local.hour < tx.online_hours[1]:
                add(o, "order_hours", f"online order at {local:%H:%M} local, outside online hours")
        elif o.channel == "in_store":
            if o.store_code != loc.location_code or loc.location_type != "store":
                add(o, "in_store_fulfilment", "in-store orders are fulfilled from the selling store")
            if o.shipping_fee != 0 or any((o.shipping_address_line1, o.shipping_postal_code, o.shipping_city)):
                add(o, "in_store_no_shipping", "in-store orders have no shipping fee or address")
            if local.weekday() == 6 or not tx.store_hours[0] <= local.hour < tx.store_hours[1]:
                add(o, "order_hours", f"in-store order at {local:%a %H:%M} local, outside opening hours")
        else:
            add(o, "channel", f"unknown channel {o.channel}")
        if not tx.start_date <= day <= tx.end_date:
            add(o, "order_window", f"placed on {day}, outside {tx.start_date} to {tx.end_date}")
        if o.placed_at < local_midnight_utc(inventory.opening_date, country.time_zone):
            add(o, "after_opening_stock", "placed before the opening stock count")
        last = max(t for t in (o.placed_at, o.reserve_at, o.confirmed_at, o.fulfilled_at, o.cancelled_at,
                               o.reservation_closed_at) if t is not None)
        if last >= now:
            add(o, "not_in_future", f"event at {last.isoformat()} is in the future")

        # money
        if o.currency_code != country.currency_code:
            add(o, "currency", f"{o.currency_code}, the currency of {o.country_code} is {country.currency_code}")
        if not o.lines:
            add(o, "has_lines", "an order needs at least one line")
        if [x.line_number for x in o.lines] != list(range(1, len(o.lines) + 1)):
            add(o, "line_numbers", "lines must be numbered 1..n")
        skus = [x.sku for x in o.lines]
        if len(set(skus)) != len(skus):
            add(o, "unique_product", "a product appears on two lines (V006 uq_order_items_order_product)")
        for x in o.lines:
            p = products.get(x.sku)
            if p is None:
                add(o, "fk_product", f"unknown product {x.sku}")
                continue
            if not p.is_active or p.launched_on is None or p.launched_on > day or \
                    (p.discontinued_on is not None and p.discontinued_on <= day):
                add(o, "product_on_sale", f"{x.sku} is not on sale on {day} (launched {p.launched_on})")
            if (o.fulfilment_location_code, x.sku) not in listings:
                add(o, "listing_missing", f"{x.sku} is not listed at {o.fulfilment_location_code}")
            price = next((pp for pp in prices.get((x.sku, o.country_code), ())
                          if pp.valid_from <= o.placed_at and (pp.valid_to is None or o.placed_at < pp.valid_to)), None)
            if price is None or (x.unit_price, x.unit_cost) != (price.list_price, price.unit_cost) \
                    or price.currency_code != o.currency_code:
                add(o, "price_snapshot", f"{x.sku}: {x.unit_price}/{x.unit_cost} is not the price valid at placed_at")
            if x.quantity < 1:
                add(o, "quantity", f"{x.sku}: quantity {x.quantity}")
            if x.vat_rate != country.vat_rate:
                add(o, "vat_rate", f"{x.sku}: {x.vat_rate}, market rate {country.vat_rate}")
            if x.discount_percent != 0 or x.discount_amount != 0:
                add(o, "no_promotions", f"{x.sku}: discounts need promotions (DBML Phase 2)")
            if x.line_total != x.quantity * x.unit_price - x.discount_amount or not _two_decimals(x.line_total):
                add(o, "line_total", f"{x.sku}: {x.line_total} != {x.quantity} x {x.unit_price}")
            if x.vat_amount != money(x.line_total * x.vat_rate / (1 + x.vat_rate)):
                add(o, "line_vat", f"{x.sku}: VAT {x.vat_amount}, expected {money(x.line_total * x.vat_rate / (1 + x.vat_rate))}")
        if o.items_total != sum((x.line_total for x in o.lines), ZERO):
            add(o, "items_total", f"{o.items_total} is not the sum of the lines")
        expected_fee = money(tx.shipping_fee[o.country_code]) if (
            o.channel == "online" and o.items_total < tx.free_shipping_from[o.country_code]) else ZERO
        if o.shipping_fee != expected_fee:
            add(o, "shipping_fee", f"{o.shipping_fee}, expected {expected_fee}")
        if o.shipping_vat_amount != money(o.shipping_fee * country.vat_rate / (1 + country.vat_rate)):
            add(o, "shipping_vat", f"{o.shipping_vat_amount} does not follow the shipping VAT formula")
        if o.order_total != o.items_total + o.shipping_fee or o.order_total <= 0:
            add(o, "order_total", f"{o.order_total} != {o.items_total} + {o.shipping_fee}")

        # lifecycle
        _lifecycle(o, tx, add)

    _holders(plan, add)
    _replay(plan, inventory, add)
    return issues


def _lifecycle(o: PlannedOrder, tx: TransactionSettings, add) -> None:
    status, reason, close = FINAL.get(o.scenario, (None, None, None))
    if (o.final_status, o.cancellation_reason, o.reservation_close_status) != (status, reason, close):
        add(o, "scenario_outcome", f"{o.scenario} must end {status}/{reason}/{close}, "
                                   f"got {o.final_status}/{o.cancellation_reason}/{o.reservation_close_status}")
    if not o.placed_at <= o.reserve_at <= o.placed_at + RESERVE_WITHIN:
        add(o, "reserve_time", "U2 must run within a minute after the order is placed")
    oos = o.scenario.startswith("out_of_stock")
    if o.reserve_outcome != ("out_of_stock" if oos else "reserved"):
        add(o, "reserve_outcome", f"{o.scenario} expects U2 {'out_of_stock' if oos else 'reserved'}")
    lifetime = timedelta(minutes=tx.reservation_minutes.get(o.channel, 0))
    if o.reservation_expires_at != o.reserve_at + lifetime:
        add(o, "reservation_lifetime", f"expires_at must be reserve time + {lifetime} for {o.channel}")
    if oos:
        if o.reservation_closed_at is not None or o.attempts:
            add(o, "out_of_stock_untouched", "an out_of_stock order has no reservation and no payment")
        if o.cancelled_at is None or not o.reserve_at <= o.cancelled_at <= o.reserve_at + RESERVE_WITHIN:
            add(o, "cancel_time", "cancelled right after the out_of_stock result")

    attempts = o.attempts
    if len(attempts) > tx.max_payment_attempts:
        add(o, "max_attempts", f"{len(attempts)} attempts, at most {tx.max_payment_attempts}")
    if [a.attempt_number for a in attempts] != list(range(1, len(attempts) + 1)):
        add(o, "attempt_numbers", "attempts must be numbered 1..n")
    captured = [a for a in attempts if a.outcome == "captured"]
    if len(captured) > 1 or (captured and attempts[-1].outcome != "captured"):
        add(o, "single_capture", "at most one capture, on the last attempt")
    previous = o.reserve_at
    for a in attempts:
        if a.amount != o.order_total:
            add(o, "payment_amount", f"attempt {a.attempt_number}: {a.amount} != order total {o.order_total}")
        if a.payment_method == "cash" and o.channel != "in_store":
            add(o, "cash_in_store", f"attempt {a.attempt_number}: cash is for in-store orders only")
        if tx.providers.get(a.payment_method) != a.payment_provider or len(a.payment_provider) > 50:
            add(o, "provider", f"attempt {a.attempt_number}: provider {a.payment_provider} for {a.payment_method}")
        if not a.provider_reference or len(a.provider_reference) > 64:
            add(o, "provider_reference", f"attempt {a.attempt_number}: 1 to 64 characters")
        if a.attempted_at < previous:
            add(o, "attempt_time", f"attempt {a.attempt_number} starts before the previous step finished")
        if a.outcome == "captured":
            if a.authorized_at is None or a.captured_at is None or a.failed_at or a.failure_reason \
                    or not a.attempted_at <= a.authorized_at <= a.captured_at:
                add(o, "capture_times", f"attempt {a.attempt_number}: attempted <= authorized <= captured")
        elif a.outcome == "failed":
            if a.failed_at is None or a.failed_at < a.attempted_at or a.authorized_at or a.captured_at \
                    or a.failure_reason not in tx.failure_reasons:
                add(o, "failure_fields", f"attempt {a.attempt_number}: failed_at after attempted_at and a known reason")
        else:
            add(o, "attempt_outcome", f"attempt {a.attempt_number}: outcome {a.outcome}")
        result = a.captured_at if a.outcome == "captured" else a.failed_at
        if result is not None and o.reservation_expires_at is not None and result >= o.reservation_expires_at:
            add(o, "pay_before_expiry", f"attempt {a.attempt_number} finishes after the reservation expires")
        previous = result or a.attempted_at

    if o.final_status == "fulfilled":
        if not captured or o.confirmed_at != captured[0].captured_at:
            add(o, "confirmed_by_capture", "confirmed_at must be the capture time")
        if o.fulfilled_at is None or o.confirmed_at is None or o.fulfilled_at <= o.confirmed_at \
                or o.fulfilled_at - o.confirmed_at > (IN_STORE_HANDOVER if o.channel == "in_store" else ONLINE_DISPATCH):
            add(o, "fulfil_time", "fulfilled after confirmation: in store within 5 minutes, online within 48 hours")
        if o.reservation_closed_at != o.fulfilled_at or o.cancelled_at is not None:
            add(o, "fulfil_fields", "reservations are consumed at fulfilment; no cancelled_at")
        if o.scenario == "payment_retry_fulfilled" and (len(attempts) < 2 or attempts[0].outcome != "failed"):
            add(o, "scenario_outcome", "a retried payment needs a failed first attempt")
    else:
        if o.confirmed_at is not None or o.fulfilled_at is not None or captured:
            add(o, "not_confirmed", f"{o.final_status} orders are never confirmed or captured")
        if o.cancelled_at is None or o.cancelled_at < o.placed_at:
            add(o, "cancel_time", "cancelled_at is required and not before placed_at")
    if o.final_status == "payment_failed":
        if not attempts or any(a.outcome != "failed" for a in attempts) \
                or o.cancelled_at != attempts[-1].failed_at or o.reservation_closed_at != o.cancelled_at:
            add(o, "payment_failed_fields", "every attempt failed; cancelled and released at the last failure")
    if o.scenario == "customer_cancelled" and (
            o.cancelled_at is None or o.reservation_expires_at is None
            or not o.reserve_at < o.cancelled_at < o.reservation_expires_at or o.reservation_closed_at != o.cancelled_at):
        add(o, "cancel_time", "a customer cancels while the reservation is still active")
    if o.scenario == "reservation_expired" and (
            o.reservation_closed_at is None or o.reservation_expires_at is None
            or o.reservation_closed_at < o.reservation_expires_at or o.cancelled_at != o.reservation_closed_at
            or o.reservation_closed_at - o.reservation_expires_at > timedelta(minutes=5)):
        add(o, "expire_time", "the sweeper expires the reservation after expires_at (within 5 minutes)")


def _holders(plan: TransactionPlan, add) -> None:
    orders = {o.order_number: o for o in plan.orders}
    for o in plan.orders:
        if o.scenario != "out_of_stock_reserved":
            if o.holder_order_number is not None:
                add(o, "holder", "only out_of_stock_reserved orders name a holder")
            continue
        h = orders.get(o.holder_order_number or "")
        if h is None:
            add(o, "holder", "the order holding the last unit is missing")
            continue
        if h.channel != "in_store" or h.final_status != "fulfilled" or h.store_code != o.store_code \
                or not {x.sku for x in o.lines} <= {x.sku for x in h.lines}:
            add(o, "holder", f"{h.order_number} must be a fulfilled in-store order of the same store and product")
        if not (h.reserve_at < o.reserve_at < (h.fulfilled_at or h.reserve_at)):
            add(o, "holder", f"U2 must run while {h.order_number} still holds its reservation")


def _replay(plan: TransactionPlan, inventory: OpeningInventory, add) -> None:
    """Replay every event from the opening stock, independently of the generator's ledger."""
    on_hand = {(x.location_code, x.sku): x.opening_quantity for x in inventory.listings}
    reserved = {k: 0 for k in on_hand}
    steps = []
    for o in plan.orders:
        seq = [(o.placed_at, "place", None), (o.reserve_at, "reserve", None)]
        for a in o.attempts:
            seq.append((a.attempted_at, "pay", a))
            seq.append((a.captured_at if a.outcome == "captured" else a.failed_at, "result", a))
        if o.reservation_close_status in ("released", "expired"):
            seq.append((o.reservation_closed_at, "release", None))
        elif o.reservation_close_status == "consumed":
            seq.append((o.fulfilled_at, "issue", None))
        for step, (at, kind, a) in enumerate(seq):
            if at is not None:
                steps.append((at, o.sequence, step, kind, o))
    steps.sort(key=lambda s: s[:3])
    for at, _, _, kind, o in steps:
        loc = o.fulfilment_location_code
        keys = [((loc, x.sku), x.quantity) for x in o.lines]
        if any(k not in on_hand for k, _ in keys):
            continue                                   # reported as listing_missing
        if kind == "reserve":
            short = [k[1] for k, q in keys if on_hand[k] - reserved[k] < q]
            outcome = "out_of_stock" if short else "reserved"
            if outcome != o.reserve_outcome:
                add(o, "stock_reserve_outcome", f"at {at.isoformat()} U2 gives {outcome} "
                                                f"(short: {', '.join(short) or 'none'}), plan says {o.reserve_outcome}")
            if outcome == "reserved" and o.reserve_outcome == "reserved":
                for k, q in keys:
                    reserved[k] += q
        elif kind in ("release", "issue") and o.reserve_outcome == "reserved":
            for k, q in keys:
                reserved[k] -= q
                if kind == "issue":
                    on_hand[k] -= q
        for k, _ in keys:
            if on_hand[k] < 0 or reserved[k] < 0 or reserved[k] > on_hand[k]:
                add(o, "stock_negative", f"{k[0]}/{k[1]}: on hand {on_hand[k]}, reserved {reserved[k]} at {at.isoformat()}")
