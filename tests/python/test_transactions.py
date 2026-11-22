"""Unit tests for the pilot transaction generator and validator (no database needed).

Run from the repository folder:
    python -m pytest tests/python/test_transactions.py -v
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nordiccart.inventory.config import load_inventory_settings
from nordiccart.inventory.generate import generate_opening_inventory
from nordiccart.master_data.generate import generate_master_data
from nordiccart.master_data.rules import local_midnight_utc, money
from nordiccart.settings import SettingsError, load_settings
from nordiccart.transactions import cli
from nordiccart.transactions.config import load_transaction_settings
from nordiccart.transactions.generate import generate_transactions
from nordiccart.transactions.model import StockLedger, TransactionPlan
from nordiccart.transactions.validate import validate_transactions

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "config" / "generator.toml"

# Recorded with seed 20261117 and the committed config/generator.toml. If a deliberate
# change to the generator or the [transactions] settings changes it, update it here.
PILOT_TRANSACTIONS_FINGERPRINT = "fabcd571a3b9f66827f20e54d0ebc421be5f68937af558383ee4b84f7c466264"


@pytest.fixture(scope="module")
def inv_settings(pilot_settings):
    return load_inventory_settings(pilot_settings)


@pytest.fixture(scope="module")
def inventory(pilot_settings, inv_settings, pilot_dataset):
    return generate_opening_inventory(pilot_settings, inv_settings, pilot_dataset)


@pytest.fixture(scope="module")
def tx(pilot_settings, inv_settings):
    return load_transaction_settings(pilot_settings, inv_settings.opening_date)


@pytest.fixture(scope="module")
def plan(pilot_settings, tx, pilot_dataset, inventory, inv_settings):
    return generate_transactions(pilot_settings, tx, pilot_dataset, inventory, inv_settings.store_format_weight)


def _tz(master, country):
    return ZoneInfo(next(c.time_zone for c in master.countries if c.country_code == country))


def _with(plan: TransactionPlan, number: str, **changes) -> TransactionPlan:
    orders = [dataclasses.replace(o, **changes) if o.order_number == number else o for o in plan.orders]
    return dataclasses.replace(plan, orders=orders)


def _first(plan, **where):
    return next(o for o in plan.orders if all(getattr(o, k) == v for k, v in where.items()))


def _rules(issues) -> set[str]:
    return {i.rule for i in issues}


# --------------------------------------------------------------------------- the pilot as configured
def test_pilot_has_the_configured_scenarios_and_outcomes(plan, tx):
    assert len(plan.orders) == 20 == tx.order_count
    summary = plan.summary()
    assert summary["orders by scenario"] == {
        "customer_cancelled": 1, "fulfilled": 12, "out_of_stock_quantity": 1, "out_of_stock_reserved": 1,
        "payment_failed": 2, "payment_retry_fulfilled": 2, "reservation_expired": 1}
    assert summary["orders by final status"] == {
        "cancelled (customer_request)": 1, "cancelled (out_of_stock)": 2, "cancelled (reservation_expired)": 1,
        "fulfilled": 14, "payment_failed": 2}
    assert summary["payment attempts by outcome"] == {"captured": 14, "failed": 7}
    assert summary["units of work (one transaction each)"] == 100


def test_pilot_uses_both_channels_all_markets_and_every_payment_method(plan):
    assert {o.channel for o in plan.orders} == {"online", "in_store"}
    assert {o.country_code for o in plan.orders} == {"SE", "NO", "DK", "FI"}
    methods = {a.payment_method for o in plan.orders for a in o.attempts if a.outcome == "captured"}
    assert methods == {"card", "mobile_payment", "buy_now_pay_later", "cash"}
    assert any(len(o.lines) > 1 for o in plan.orders) and any(x.quantity > 1 for o in plan.orders for x in o.lines)


def test_validation_passes(plan, pilot_dataset, inventory, tx):
    assert validate_transactions(plan, pilot_dataset, inventory, tx) == []


def test_same_seed_gives_the_same_plan_and_the_recorded_fingerprint(plan, pilot_settings, tx, pilot_dataset,
                                                                     inventory, inv_settings):
    again = generate_transactions(pilot_settings, tx, pilot_dataset, inventory, inv_settings.store_format_weight)
    assert again.fingerprint() == plan.fingerprint() == PILOT_TRANSACTIONS_FINGERPRINT


def test_another_seed_gives_other_orders_that_still_validate(pilot_settings, tx, inv_settings, plan):
    other = dataclasses.replace(pilot_settings, seed=7)
    master = generate_master_data(other)
    inventory = generate_opening_inventory(other, inv_settings, master)
    other_plan = generate_transactions(other, tx, master, inventory, inv_settings.store_format_weight)
    assert other_plan.fingerprint() != plan.fingerprint()
    assert validate_transactions(other_plan, master, inventory, tx) == []


# --------------------------------------------------------------------------- consistency with the master data
def test_customers_registered_before_ordering_in_their_own_market(plan, pilot_dataset):
    customers = {c.customer_number: c for c in pilot_dataset.customers}
    for o in plan.orders:
        c = customers[o.customer_number]
        assert c.country_code == o.country_code and c.is_active and c.registered_at <= o.placed_at


def test_products_on_sale_and_locations_open_on_the_local_order_day(plan, pilot_dataset):
    products = {p.sku: p for p in pilot_dataset.products}
    locations = {l.location_code: l for l in pilot_dataset.stock_locations}
    for o in plan.orders:
        day = o.placed_at.astimezone(_tz(pilot_dataset, o.country_code)).date()
        assert locations[o.fulfilment_location_code].opened_on <= day
        assert all(products[x.sku].launched_on <= day for x in o.lines)
    sold = {x.sku for o in plan.orders for x in o.lines}
    assert not sold & {"CAM-000007", "SMH-000011"}            # launched after the pilot window


def test_orders_fall_in_the_window_after_the_opening_stock_and_in_opening_hours(plan, pilot_dataset, tx, inventory):
    for o in plan.orders:
        zone = next(c.time_zone for c in pilot_dataset.countries if c.country_code == o.country_code)
        local = o.placed_at.astimezone(ZoneInfo(zone))
        assert tx.start_date <= local.date() <= tx.end_date
        assert o.placed_at > local_midnight_utc(inventory.opening_date, zone)
        if o.channel == "in_store":
            assert local.weekday() != 6 and tx.store_hours[0] <= local.hour < tx.store_hours[1]
        else:
            assert tx.online_hours[0] <= local.hour < tx.online_hours[1]


def test_market_currency_vat_and_the_price_valid_at_placed_at(plan, pilot_dataset):
    countries = {c.country_code: c for c in pilot_dataset.countries}
    for o in plan.orders:
        assert o.currency_code == countries[o.country_code].currency_code
        for x in o.lines:
            price = next(p for p in pilot_dataset.product_prices if p.sku == x.sku and p.country_code == o.country_code
                         and p.valid_from <= o.placed_at and (p.valid_to is None or o.placed_at < p.valid_to))
            assert (x.unit_price, x.unit_cost) == (price.list_price, price.unit_cost)
            assert x.vat_rate == countries[o.country_code].vat_rate


def test_line_and_order_arithmetic_with_half_up_rounding(plan, tx):
    for o in plan.orders:
        for x in o.lines:
            assert x.line_total == x.quantity * x.unit_price and x.discount_amount == 0
            assert x.vat_amount == money(x.line_total * x.vat_rate / (1 + x.vat_rate))
        assert o.items_total == sum(x.line_total for x in o.lines)
        rate = o.lines[0].vat_rate
        assert o.shipping_vat_amount == money(o.shipping_fee * rate / (1 + rate))
        assert o.order_total == o.items_total + o.shipping_fee > 0
        if o.channel == "online" and o.items_total < tx.free_shipping_from[o.country_code]:
            assert o.shipping_fee == tx.shipping_fee[o.country_code]
        else:
            assert o.shipping_fee == 0
    assert any(o.shipping_fee > 0 for o in plan.orders)          # at least one order below the threshold


def test_online_orders_ship_from_the_market_warehouse_to_the_customer(plan, pilot_dataset):
    customers = {c.customer_number: c for c in pilot_dataset.customers}
    locations = {l.location_code: l for l in pilot_dataset.stock_locations}
    for o in plan.orders:
        loc = locations[o.fulfilment_location_code]
        assert loc.country_code == o.country_code
        if o.channel == "online":
            c = customers[o.customer_number]
            assert loc.location_type == "warehouse" and o.store_code is None
            assert (o.shipping_address_line1, o.shipping_postal_code, o.shipping_city) == (c.address_line1, c.postal_code, c.city)
        else:
            assert loc.location_type == "store" and o.store_code == o.fulfilment_location_code
            assert o.shipping_address_line1 is None and o.shipping_fee == 0


# --------------------------------------------------------------------------- payments and lifecycle
def test_payment_rules(plan, tx):
    refs = [a.provider_reference for o in plan.orders for a in o.attempts]
    assert len(refs) == len(set(refs))
    for o in plan.orders:
        assert len(o.attempts) <= tx.max_payment_attempts
        assert [a.attempt_number for a in o.attempts] == list(range(1, len(o.attempts) + 1))
        assert all(a.amount == o.order_total and a.payment_provider == tx.providers[a.payment_method] for a in o.attempts)
        assert all(a.payment_method != "cash" for a in o.attempts) or o.channel == "in_store"
        captured = [a for a in o.attempts if a.outcome == "captured"]
        assert len(captured) <= 1 and (not captured or o.attempts[-1] is captured[0])
        assert all(a.payment_method in ("card", "mobile_payment") for a in o.attempts if a.outcome == "failed")


def test_each_scenario_follows_its_lifecycle(plan, tx):
    for o in plan.orders:
        lifetime = timedelta(minutes=tx.reservation_minutes[o.channel])
        assert o.placed_at < o.reserve_at <= o.placed_at + timedelta(seconds=8)
        assert o.reservation_expires_at == o.reserve_at + lifetime
        for a in o.attempts:
            assert o.reserve_at < a.attempted_at < a.result_at < o.reservation_expires_at
        if o.scenario in ("fulfilled", "payment_retry_fulfilled"):
            assert o.final_status == "fulfilled" and o.confirmed_at == o.attempts[-1].captured_at < o.fulfilled_at
            assert o.fulfilled_at - o.confirmed_at <= (timedelta(minutes=5) if o.channel == "in_store" else timedelta(hours=48))
        if o.scenario == "payment_retry_fulfilled":
            assert [a.outcome for a in o.attempts] == ["failed", "captured"]
        if o.scenario == "payment_failed":
            assert all(a.outcome == "failed" for a in o.attempts)
            assert len(o.attempts) == (tx.max_payment_attempts if o.channel == "online" else 2)
            assert o.cancelled_at == o.attempts[-1].failed_at == o.reservation_closed_at
        if o.scenario == "customer_cancelled":
            assert not o.attempts and o.reserve_at < o.cancelled_at < o.reservation_expires_at
        if o.scenario == "reservation_expired":
            assert not o.attempts and o.reservation_expires_at <= o.reservation_closed_at == o.cancelled_at


def test_events_follow_business_time_and_each_order_keeps_its_step_order(plan):
    events = plan.events()
    assert [e.sort_key for e in events] == sorted(e.sort_key for e in events)
    for o in plan.orders:
        mine = [e for e in events if e.order_number == o.order_number]
        assert [e.step for e in mine] == list(range(len(mine)))
        assert mine[0].kind == "place" and mine[1].kind == "reserve"


def test_order_numbers_follow_placed_at(plan, tx, pilot_dataset):
    ordered = sorted(plan.orders, key=lambda o: o.placed_at)
    for position, o in enumerate(ordered):
        year = o.placed_at.astimezone(_tz(pilot_dataset, o.country_code)).year
        assert o.order_number == f"{o.country_code}-{year}-{tx.first_order_sequence + position:08d}"


# --------------------------------------------------------------------------- stock
def _ledger_before(plan, number, kind):
    """Stock just before the given event of the given order."""
    events = plan.events()
    stop = next(i for i, e in enumerate(events) if e.order_number == number and e.kind == kind)
    return plan.replay(events[:stop])


def test_out_of_stock_quantity_asks_for_more_than_the_warehouse_holds_and_reserves_nothing(plan):
    o = _first(plan, scenario="out_of_stock_quantity")
    ledger = _ledger_before(plan, o.order_number, "reserve")
    short = [x for x in o.lines if ledger.available(o.fulfilment_location_code, x.sku) < x.quantity]
    fine = [x for x in o.lines if x not in short]
    assert short and fine                                        # all lines or none: the available line is not reserved
    after = plan.replay([e for e in plan.events() if e.sort_key <= next(
        e.sort_key for e in plan.events() if e.order_number == o.order_number and e.kind == "reserve")])
    assert all(after.reserved[(o.fulfilment_location_code, x.sku)] == ledger.reserved[(o.fulfilment_location_code, x.sku)]
               for x in o.lines)


def test_reserved_stock_is_not_available_to_the_next_customer(plan):
    o = _first(plan, scenario="out_of_stock_reserved")
    holder = plan.order(o.holder_order_number)
    assert holder.store_code == o.store_code and holder.lines[0].sku == o.lines[0].sku
    assert holder.reserve_at < o.reserve_at < holder.fulfilled_at
    ledger = _ledger_before(plan, o.order_number, "reserve")
    key = (o.fulfilment_location_code, o.lines[0].sku)
    assert ledger.on_hand[key] == 1 and ledger.reserved[key] == 1 and ledger.available(*key) == 0


def test_stock_never_goes_negative_and_ends_at_opening_minus_sales(plan, inventory):
    ledger = StockLedger(plan.opening_stock)
    orders = {o.order_number: o for o in plan.orders}
    for e in plan.events():
        outcome = ledger.apply(orders[e.order_number], e)
        if e.kind == "reserve":
            assert outcome == orders[e.order_number].reserve_outcome
        assert all(v >= 0 for v in ledger.on_hand.values())
        assert all(0 <= ledger.reserved[k] <= ledger.on_hand[k] for k in ledger.reserved)
    sold = sum(x.quantity for o in plan.orders if o.final_status == "fulfilled" for x in o.lines)
    assert sum(ledger.on_hand.values()) == sum(x.opening_quantity for x in inventory.listings) - sold == 1441
    assert sum(ledger.reserved.values()) == 0


# --------------------------------------------------------------------------- validator catches faults
def _later_customer(master, order, *, after):
    return next(c.customer_number for c in master.customers if c.country_code == order.country_code and c.registered_at > after)


def _swap_line(o, **changes):
    return (dataclasses.replace(o.lines[0], **changes),) + o.lines[1:]


FAULTS = {
    "customer_registered": lambda p, m: _with(p, p.orders[0].order_number,
                                               customer_number=_later_customer(m, p.orders[0], after=p.orders[0].placed_at)),
    "product_on_sale": lambda p, m: _with(p, _first(p, scenario="fulfilled").order_number,
                                           lines=_swap_line(_first(p, scenario="fulfilled"), sku="CAM-000007")),
    "currency": lambda p, m: _with(p, _first(p, country_code="SE").order_number, currency_code="EUR"),
    "vat_rate": lambda p, m: _with(p, p.orders[0].order_number, lines=_swap_line(p.orders[0], vat_rate=Decimal("0.2400"))),
    "price_snapshot": lambda p, m: _with(p, p.orders[0].order_number,
                                          lines=_swap_line(p.orders[0], unit_price=p.orders[0].lines[0].unit_price + 1)),
    "line_total": lambda p, m: _with(p, p.orders[0].order_number,
                                      lines=_swap_line(p.orders[0], line_total=p.orders[0].lines[0].line_total + 1)),
    "items_total": lambda p, m: _with(p, p.orders[0].order_number, items_total=p.orders[0].items_total + 1),
    "shipping_fee": lambda p, m: _with(p, next(o for o in p.orders if o.shipping_fee > 0).order_number,
                                        shipping_fee=Decimal("0.00")),
    "payment_amount": lambda p, m: _with(p, p.orders[0].order_number, attempts=tuple(
        dataclasses.replace(a, amount=a.amount + 1) for a in p.orders[0].attempts)),
    "cash_in_store": lambda p, m: _with(p, _first(p, channel="online", scenario="fulfilled").order_number, attempts=tuple(
        dataclasses.replace(a, payment_method="cash", payment_provider="store_cash_register")
        for a in _first(p, channel="online", scenario="fulfilled").attempts)),
    "single_capture": lambda p, m: _with(p, _first(p, scenario="payment_retry_fulfilled").order_number,
                                          attempts=tuple(reversed(_first(p, scenario="payment_retry_fulfilled").attempts))),
    "max_attempts": lambda p, m: _with(p, _first(p, scenario="payment_failed", channel="online").order_number,
                                        attempts=_first(p, scenario="payment_failed", channel="online").attempts * 2),
    "pay_before_expiry": lambda p, m: _with(p, _first(p, scenario="fulfilled").order_number,
                                             reservation_expires_at=_first(p, scenario="fulfilled").reserve_at
                                             + timedelta(seconds=1)),
    "order_hours": lambda p, m: _with(p, _first(p, channel="in_store").order_number,
                                       placed_at=_first(p, channel="in_store").placed_at.replace(hour=3)),
    "online_fulfilment": lambda p, m: _with(p, _first(p, channel="online", country_code="SE").order_number,
                                             fulfilment_location_code="SE-STO-01"),
    "unique_order_number": lambda p, m: _with(p, p.orders[1].order_number, order_number=p.orders[0].order_number),
    "order_number_format": lambda p, m: _with(p, p.orders[0].order_number, order_number="X-1"),
    "stock_reserve_outcome": lambda p, m: _with(p, _first(p, scenario="fulfilled").order_number, lines=_swap_line(
        _first(p, scenario="fulfilled"), quantity=500, line_total=_first(p, scenario="fulfilled").lines[0].unit_price * 500)),
    "holder": lambda p, m: _with(p, _first(p, scenario="out_of_stock_reserved").order_number,
                                  holder_order_number=p.orders[0].order_number),
    "reservation_lifetime": lambda p, m: _with(p, p.orders[0].order_number,
                                                reservation_expires_at=p.orders[0].reservation_expires_at + timedelta(minutes=1)),
    "expire_time": lambda p, m: _with(p, _first(p, scenario="reservation_expired").order_number,
                                       reservation_closed_at=_first(p, scenario="reservation_expired").reserve_at),
    "scenario_outcome": lambda p, m: _with(p, _first(p, scenario="fulfilled").order_number, final_status="cancelled"),
    "order_count": lambda p, m: dataclasses.replace(p, orders=p.orders[:-1]),
    "online_address": lambda p, m: _with(p, _first(p, channel="online").order_number, shipping_city="Elsewhere"),
    "not_in_future": lambda p, m: _with(p, _first(p, scenario="fulfilled", channel="online").order_number,
                                         fulfilled_at=_first(p, scenario="fulfilled", channel="online").fulfilled_at
                                         + timedelta(days=3650)),
}


@pytest.mark.parametrize("rule", sorted(FAULTS))
def test_validator_catches(rule, plan, pilot_dataset, inventory, tx):
    broken = FAULTS[rule](plan, pilot_dataset)
    assert rule in _rules(validate_transactions(broken, pilot_dataset, inventory, tx))


# --------------------------------------------------------------------------- settings and command line
def _config_with(tmp_path, old: str, new: str) -> Path:
    text = CONFIG.read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "generator.toml"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


@pytest.mark.parametrize("old,new,message", [
    ("start_date = 2024-11-01", "start_date = 2024-10-31", "opening_date"),
    ("out_of_stock_reserved = 1 ", "out_of_stock_reserved = 13 ", "fulfilled >= out_of_stock_reserved"),
    ("in_store = { card = 0.75, mobile_payment = 0.15, cash = 0.10 }", "in_store = { card = 0.75, cheque = 0.25 }", "unknown keys"),
    ('online   = { card = 0.55,', 'online   = { cash = 0.10, card = 0.55,', "unknown keys"),
    ('failure_reasons = ["card_declined", "insufficient_funds", "timeout"]', 'failure_reasons = ["fraud"]', "failure_reasons"),
    ("max_payment_attempts = 3 ", "max_payment_attempts = 1 ", "max_payment_attempts"),
    ("fulfilled = 12 ", "lost_in_post = 1\nfulfilled = 12 ", "unknown scenarios"),
])
def test_invalid_transaction_settings_are_refused(tmp_path, old, new, message):
    settings = load_settings(_config_with(tmp_path, old, new), "pilot")
    with pytest.raises(SettingsError, match=message):
        load_transaction_settings(settings, load_inventory_settings(settings).opening_date)


def test_only_the_pilot_profile_is_approved(full_settings, inv_settings):
    with pytest.raises(SettingsError, match="pilot profile only"):
        load_transaction_settings(full_settings, inv_settings.opening_date)
    assert cli.main(["--profile", "full", "--no-csv"]) == 3


def test_dry_run_writes_the_preview_without_a_database(tmp_path, monkeypatch, plan):
    config = _config_with(tmp_path, 'csv_dir = "output/master_data"', f'csv_dir = "{(tmp_path / "out" / "master_data").as_posix()}"')
    config.write_text(config.read_text(encoding="utf-8").replace('log_dir = "logs"', f'log_dir = "{(tmp_path / "logs").as_posix()}"'),
                      encoding="utf-8")
    monkeypatch.setenv("PGHOST", "host.invalid")              # a database would not be reachable
    monkeypatch.setenv("PGPASSWORD", "s3cret-never-logged")
    assert cli.main(["--config", str(config)]) == 0
    out = tmp_path / "out" / "transactions" / "pilot"
    assert len((out / "orders.csv").read_text(encoding="utf-8-sig").strip().splitlines()) == 21
    assert len((out / "order_items.csv").read_text(encoding="utf-8-sig").strip().splitlines()) == 27
    assert len((out / "payment_attempts.csv").read_text(encoding="utf-8-sig").strip().splitlines()) == 22
    assert len((out / "units_of_work.csv").read_text(encoding="utf-8-sig").strip().splitlines()) == 101
    assert plan.fingerprint() in (out / "summary.json").read_text(encoding="utf-8")
    logs = list((tmp_path / "logs").glob("transactions_pilot_dry_run_*.log"))
    assert logs and "s3cret-never-logged" not in logs[0].read_text(encoding="utf-8")


def test_validation_failure_stops_before_any_database_work(monkeypatch, tmp_path):
    from nordiccart.master_data.validate import Issue
    monkeypatch.setattr(cli, "validate_transactions", lambda *a, **k: [Issue("transactions", "X", "test", "forced")])
    config = _config_with(tmp_path, 'log_dir = "logs"', f'log_dir = "{(tmp_path / "logs").as_posix()}"')
    assert cli.main(["--config", str(config), "--apply", "--yes", "--no-csv"]) == 1
