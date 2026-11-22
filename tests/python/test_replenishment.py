"""Unit tests for the shared stock ledger and the replenishment pilot (no database needed).

Run from the repository folder:
    python -m pytest tests/python/test_replenishment.py -v
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from nordiccart.inventory.config import load_inventory_settings
from nordiccart.inventory.generate import display_minimum, generate_opening_inventory
from nordiccart.master_data.generate import generate_master_data
from nordiccart.replenishment import cli as rp_cli
from nordiccart.replenishment import policy
from nordiccart.replenishment.config import load_replenishment_settings
from nordiccart.replenishment.generate import GenerationError, generate_replenishment
from nordiccart.replenishment.model import ReplenishmentPlan
from nordiccart.replenishment.validate import validate_replenishment
from nordiccart.settings import SettingsError, load_settings
from nordiccart.stock.ledger import StockError, StockLedger
from nordiccart.transactions import cli as tx_cli
from nordiccart.transactions.config import load_transaction_settings
from nordiccart.transactions.generate import generate_transactions
from nordiccart.transactions.model import StockLedger as TransactionLedger

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "config" / "generator.toml"

# Recorded with seed 20261117 and the committed config/generator.toml. If a deliberate
# change to the generator or the [replenishment] settings changes it, update it here.
PILOT_REPLENISHMENT_FINGERPRINT = "858bbfd09597030b573273c9885ffae0c0299e4ed146e8aca7c98f0e05531236"
PILOT_LAST_EVENT = datetime(2024, 11, 15, 8, 44, 22, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def inv(pilot_settings):
    return load_inventory_settings(pilot_settings)


@pytest.fixture(scope="module")
def inventory(pilot_settings, inv, pilot_dataset):
    return generate_opening_inventory(pilot_settings, inv, pilot_dataset)


@pytest.fixture(scope="module")
def tx(pilot_settings, inv):
    return load_transaction_settings(pilot_settings, inv.opening_date)


@pytest.fixture(scope="module")
def pilot(pilot_settings, tx, pilot_dataset, inventory, inv):
    return generate_transactions(pilot_settings, tx, pilot_dataset, inventory, inv.store_format_weight)


@pytest.fixture(scope="module")
def rp(pilot_settings, tx, pilot_dataset):
    return load_replenishment_settings(pilot_settings, tx.store_hours, {c.category_code for c in pilot_dataset.product_categories})


@pytest.fixture(scope="module")
def plan(pilot_settings, inv, rp, pilot_dataset, inventory, pilot):
    return generate_replenishment(pilot_settings, inv, rp, pilot_dataset, inventory, pilot)


def _validate(plan, pilot_dataset, inventory, inv, rp, pilot):
    return validate_replenishment(plan, pilot_dataset, inventory, inv, rp, pilot)


def _rules(issues) -> set[str]:
    return {i.rule for i in issues}


def _tz(master, market):
    return ZoneInfo(next(c.time_zone for c in master.countries if c.country_code == market))


def _store_triggers(pilot, inventory):
    """Store listings on sale with available <= reorder point after the pilot (independent of the generator)."""
    snapshot = pilot.replay().snapshot()
    out = {}
    for x in inventory.listings:
        if x.location_type == "store" and x.opening_quantity > 0:
            on_hand, reserved = snapshot[(x.location_code, x.sku)]
            if on_hand - reserved <= x.reorder_point:
                out[(x.location_code, x.sku)] = on_hand - reserved
    return out


# --------------------------------------------------------------------------- shared stock ledger
def test_ledger_receives_dispatches_and_arrives_with_conservation():
    ledger = StockLedger({("WH", "A"): 5, ("ST", "A"): 0})
    ledger.receive("WH", "A", 3)
    ledger.dispatch("TR-1", "WH", "ST", "A", 4)
    assert ledger.available("WH", "A") == 4 and ledger.units_in_transit() == 4 and ledger.in_transit_to("ST", "A") == 4
    assert ledger.total_on_hand() + ledger.units_in_transit() == 5 + 3
    line = ledger.arrive("TR-1", "A")
    assert (line.destination, line.quantity) == ("ST", 4)
    assert ledger.snapshot() == {("ST", "A"): (4, 0), ("WH", "A"): (4, 0)}
    assert ledger.units_in_transit() == 0 and ledger.received == 3 and ledger.problems() == []


def test_ledger_refuses_what_v007_refuses():
    ledger = StockLedger({("WH", "A"): 3, ("ST", "A"): 0})
    ledger.reserve("WH", [SimpleNamespace(sku="A", quantity=2)])
    with pytest.raises(StockError, match="no_oversell"):                 # reserved units never move
        ledger.dispatch("TR-1", "WH", "ST", "A", 2)
    with pytest.raises(StockError):
        ledger.receive("WH", "A", 0)
    with pytest.raises(StockError):
        ledger.dispatch("TR-2", "WH", "WH", "A", 1)                      # same location
    ledger.dispatch("TR-3", "WH", "ST", "A", 1)
    with pytest.raises(StockError, match="already dispatched"):
        ledger.dispatch("TR-3", "WH", "ST", "A", 1)
    with pytest.raises(StockError, match="nothing in transit"):
        ledger.arrive("TR-9", "A")


def test_ledger_snapshot_round_trip_and_the_transaction_ledger_is_the_shared_one(pilot):
    ledger = StockLedger.from_snapshot({("WH", "A"): (5, 2), ("ST", "A"): (1, 0)})
    assert ledger.snapshot() == {("ST", "A"): (1, 0), ("WH", "A"): (5, 2)} and ledger.available("WH", "A") == 3
    assert issubclass(TransactionLedger, StockLedger)
    after_pilot = pilot.replay()
    assert isinstance(after_pilot, StockLedger) and after_pilot.total_on_hand() == 1441
    assert pilot.last_event_at() == PILOT_LAST_EVENT


# --------------------------------------------------------------------------- policy
def test_round_up_to_case():
    assert [policy.round_up_to_case(q, 5) for q in (1, 5, 6, 10, 11)] == [5, 5, 10, 10, 15]
    assert policy.round_up_to_case(0, 2) == 2 and policy.round_up_to_case(3, 1) == 3


def test_echelon_demand_adds_the_stores_of_the_market(inventory):
    echelon = policy.echelon_weekly(inventory.listings)
    x = next(l for l in inventory.listings if (l.location_code, l.sku) == ("SE-WH-01", "SPH-000014"))
    stores = sum(l.expected_weekly_units for l in inventory.listings
                 if l.location_type == "store" and l.country_code == "SE" and l.sku == "SPH-000014")
    assert echelon[("SE-WH-01", "SPH-000014")] == x.expected_weekly_units + stores > x.expected_weekly_units


def test_echelon_reorder_point_rounds_up_and_stays_below_the_opening_quantity():
    assert policy.echelon_reorder_point(Decimal("3.76"), Decimal(2), Decimal("1.5"), 2, 19) == 12   # ceil(11.28)
    assert policy.echelon_reorder_point(Decimal("3.76"), Decimal(2), Decimal("1.5"), 2, 10) == 9    # capped below 10
    assert policy.echelon_reorder_point(Decimal("0.10"), Decimal(2), Decimal("1.5"), 2, 0) == 2     # floor, not stocked


def test_order_up_to_levels():
    assert policy.store_order_up_to(Decimal("0.3385"), Decimal(6), 1, 1) == 2       # round(2.03) = 2, above ROP 1
    assert policy.store_order_up_to(Decimal("0.10"), Decimal(6), 3, 2) == 3         # display minimum wins
    assert policy.store_order_up_to(Decimal("0.10"), Decimal(6), 1, 3) == 4         # always above the reorder point
    assert policy.warehouse_order_up_to(Decimal("1.1"), Decimal(10), 4, 4) == 11
    assert policy.needs_replenishment(3, 3) and policy.needs_replenishment(0, 0) and not policy.needs_replenishment(4, 3)


def test_eligibility_knows_launches_discontinuation_and_locations(pilot_dataset):
    camera = next(p for p in pilot_dataset.products if p.sku == "CAM-000007")
    store = next(l for l in pilot_dataset.stock_locations if l.location_code == "SE-STO-01")
    assert camera.launched_on == date(2025, 3, 19)
    assert not policy.eligible(camera, store, date(2024, 11, 18), 7)                 # months before launch
    assert not policy.eligible(camera, store, date(2025, 3, 11), 7)
    assert policy.eligible(camera, store, date(2025, 3, 12), 7)                      # inside the pre-launch window
    assert not policy.eligible(dataclasses.replace(camera, discontinued_on=date(2025, 4, 1)), store, date(2025, 4, 1), 7)
    assert not policy.eligible(camera, dataclasses.replace(store, is_active=False), date(2025, 3, 20), 7)
    assert not policy.eligible(camera, dataclasses.replace(store, opened_on=date(2025, 6, 1)), date(2025, 3, 20), 7)


def test_allocation_order_and_warehouse_protection():
    def request(store, available, rop, flagship=False, quantity=2):
        return policy.StoreRequest(store, "A", quantity, None, available, available, rop, rop + 2, flagship)
    requests = [request("S1", 1, 2), request("S2", 0, 1), request("S3", 1, 1, flagship=True), request("S4", 1, 3)]
    result = policy.allocate(requests, warehouse_available=8, protected=3)          # 5 units above the protection
    assert [(r.store, q) for r, q in result] == [("S2", 2), ("S3", 2), ("S4", 1), ("S1", 0)]
    assert sum(q for _, q in policy.allocate(requests, 3, 3)) == 0                    # nothing above the reorder point


# --------------------------------------------------------------------------- the pilot as configured
def test_pilot_has_the_planned_documents_and_units_of_work(plan):
    summary = plan.summary()
    assert summary["reorder point revisions (warehouses / listings changed)"] == "4 / 69"
    assert summary["warehouse reorder points, total before -> after"] == "323 -> 533"
    assert summary["transfer documents"] == 4 and summary["transfer lines / units"] == "8 / 10"
    assert summary["purchase orders (forced / natural)"] == "4 (4 / 0)"
    assert summary["delivery notes (split purchase orders)"] == "5 (1)"
    assert summary["units received"] == 26 and summary["units in transit at the end"] == 0
    assert summary["units of work by kind"] == {"arrive": 4, "dispatch": 4, "receive": 5, "revise": 4}
    assert summary["stock on hand after the pilot -> after replenishment"] == "1441 -> 1467"
    assert plan.backorders == []


def test_transfers_serve_exactly_the_store_listings_at_or_below_their_reorder_point(plan, pilot, inventory, inv,
                                                                                    pilot_dataset):
    triggers = _store_triggers(pilot, inventory)
    lines = {(t.destination, x.sku): x for t in plan.transfers for x in t.lines}
    assert set(lines) == set(triggers) and len(lines) == 8
    formats = {s.location_code: s.store_format for s in pilot_dataset.stores}
    listing = {(x.location_code, x.sku): x for x in inventory.listings}
    for key, x in lines.items():
        l = listing[key]
        s = max(display_minimum(inv, l.price_eur, formats[key[0]]),
                int((l.expected_weekly_units * inv.weeks_of_cover["store"]).to_integral_value(rounding="ROUND_HALF_UP")),
                l.reorder_point + 1)
        assert x.store_position == triggers[key] <= x.reorder_point == l.reorder_point
        assert x.quantity == x.requested == s - triggers[key] > 0
    assert {t.trigger for t in plan.transfers} == {"reorder_point"}


def test_forced_purchase_orders_are_flagged_and_kept_apart_from_natural_replenishment(plan):
    assert {p.market for p in plan.purchase_orders} == {"SE", "NO", "DK", "FI"}
    for p in plan.purchase_orders:
        assert p.forced and [x.trigger for x in p.lines] == ["forced_reorder"]
        assert all(x.position > x.reorder_point for x in p.lines)          # not triggered by the reorder point
        assert all(x.quantity % x.case_pack == 0 for x in p.lines)


def test_forced_order_takes_the_least_headroom_listing_of_each_market(plan, inventory):
    ledger, rops = plan.replay([e for e in plan.events() if e.kind in ("revise", "dispatch", "arrive")])
    for p in plan.purchase_orders:
        candidates = sorted((ledger.available(p.warehouse, x.sku) - rops[(p.warehouse, x.sku)], x.sku)
                            for x in inventory.listings if x.location_code == p.warehouse and x.opening_quantity > 0)
        assert p.lines[0].sku == candidates[0][1] or p.market == "SE"     # SE may skip a 1-unit split candidate
        assert p.lines[0].position == ledger.available(p.warehouse, p.lines[0].sku)


def test_the_se_forced_order_is_split_into_two_delivery_notes(plan):
    for p in plan.purchase_orders:
        if p.market == "SE":
            dn1, dn2 = p.deliveries
            assert (dn1.reference, dn2.reference) == (f"{p.number}/DN1", f"{p.number}/DN2")
            assert dn1.received_at < dn2.received_at and dn1.units + dn2.units == p.units
        else:
            assert len(p.deliveries) == 1 and p.deliveries[0].units == p.units


def test_warehouse_reorder_points_follow_echelon_demand_below_the_opening_quantity(plan, inventory, inv):
    listing = {(x.location_code, x.sku): x for x in inventory.listings}
    on_sale_before = on_sale_after = 0
    for r in plan.revisions:
        for c in r.changes:
            x = listing[(r.warehouse, c.sku)]
            assert c.old == x.reorder_point and c.new > c.old
            assert x.opening_quantity == 0 or c.new < x.opening_quantity          # script 12, check 501 stays PASS
            if x.opening_quantity:
                on_sale_before += c.old
                on_sale_after += c.new
    unchanged = sum(x.reorder_point for x in inventory.listings if x.location_type == "warehouse" and x.opening_quantity
                    and not any(c.sku == x.sku for r in plan.revisions if r.warehouse == x.location_code for c in r.changes))
    assert on_sale_before + unchanged == 302 and on_sale_after + unchanged == 504     # feasibility study figures


def test_products_not_launched_yet_are_never_replenished(plan):
    skus = {x.sku for t in plan.transfers for x in t.lines} | {x.sku for p in plan.purchase_orders for x in p.lines}
    assert not skus & {"CAM-000007", "SMH-000011"}            # listed at 0 with reorder points > 0, launched 2025


def test_every_unit_of_work_is_after_the_pilot_in_business_time_order_and_hours(plan, pilot_dataset):
    events = plan.events()
    assert all(e.at > PILOT_LAST_EVENT for e in events) and events == sorted(events, key=lambda e: e.sort_key)
    for t in plan.transfers:
        tz = _tz(pilot_dataset, t.market)
        disp, arr = t.dispatched_at.astimezone(tz), t.arrived_at.astimezone(tz)
        assert t.requested_at < t.dispatched_at < t.arrived_at and disp.date() == arr.date()
        assert disp.isoweekday() <= 5 and 6 <= disp.hour < 8 and arr.hour < 10
    for p in plan.purchase_orders:
        tz = _tz(pilot_dataset, p.market)
        for d in p.deliveries:
            local = d.received_at.astimezone(tz)
            assert local.isoweekday() <= 5 and 7 <= local.hour < 15 and local.date() <= date(2024, 12, 6)


def test_document_references_follow_the_agreed_formats(plan):
    for t in plan.transfers:
        assert t.reference == f"TR-{t.market}-2024-000001-{t.destination}" and len(t.reference) <= 64
    for p in plan.purchase_orders:
        assert p.number == f"PO-{p.market}-2024-000001"


def test_conservation_and_nothing_left_in_transit_or_on_order(plan):
    ledger = StockLedger.from_snapshot(plan.base_stock)
    rops = dict(plan.base_reorder_points)
    for e in plan.events():
        plan.apply(ledger, rops, e)
        assert ledger.problems() == []
        assert ledger.total_on_hand() + ledger.units_in_transit() == 1441 + ledger.received
    assert ledger.units_in_transit() == 0 and ledger.total_on_hand() == 1467


def test_reviews_whose_follow_ups_leave_the_window_are_skipped(plan):
    skipped = [r for r in plan.reviews if r.note.startswith("skipped")]
    assert {(r.kind, r.at.date()) for r in skipped if r.kind == "warehouse"} == {
        ("warehouse", date(2024, 11, 25)), ("warehouse", date(2024, 12, 2))}
    assert {r.at.date() for r in skipped if r.kind == "store"} == {date(2024, 12, 6)}


def test_validation_passes(plan, pilot_dataset, inventory, inv, rp, pilot):
    assert _validate(plan, pilot_dataset, inventory, inv, rp, pilot) == []


def test_same_seed_gives_the_same_plan_and_the_recorded_fingerprint(plan, pilot_settings, inv, rp, pilot_dataset,
                                                                     inventory, pilot):
    again = generate_replenishment(pilot_settings, inv, rp, pilot_dataset, inventory, pilot)
    assert again.fingerprint() == plan.fingerprint() == PILOT_REPLENISHMENT_FINGERPRINT


def test_another_seed_gives_another_plan_that_still_validates(pilot_settings, inv, tx, rp, plan):
    other = dataclasses.replace(pilot_settings, seed=7)
    master = generate_master_data(other)
    inventory = generate_opening_inventory(other, inv, master)
    other_pilot = generate_transactions(other, tx, master, inventory, inv.store_format_weight)
    other_plan = generate_replenishment(other, inv, rp, master, inventory, other_pilot)
    assert other_plan.fingerprint() != plan.fingerprint()
    assert validate_replenishment(other_plan, master, inventory, inv, rp, other_pilot) == []


def test_a_short_warehouse_allocates_partly_protects_itself_and_orders_naturally(pilot_settings, inv, rp, pilot_dataset,
                                                                                inventory, pilot):
    """SE-WH-01 left with 13 x SPH-000014 (reorder point 12): the store asks for 2, gets 1, and SE orders naturally."""
    opening = dict(pilot.opening_stock)
    opening[("SE-WH-01", "SPH-000014")] = 13
    short = dataclasses.replace(pilot, opening_stock=opening)
    plan = generate_replenishment(pilot_settings, inv, rp, pilot_dataset, inventory, short)
    line = next(x for t in plan.transfers if t.destination == "SE-STO-03" for x in t.lines if x.sku == "SPH-000014")
    assert (line.requested, line.quantity) == (2, 1)
    assert any(b.startswith("SE-STO-03/SPH-000014") for b in plan.backorders)
    natural = [p for p in plan.purchase_orders if not p.forced]
    assert [(p.market, x.sku, x.trigger) for p in natural for x in p.lines] == [("SE", "SPH-000014", "reorder_point")]
    assert natural[0].lines[0].position <= natural[0].lines[0].reorder_point
    assert validate_replenishment(plan, pilot_dataset, inventory, inv, rp, short) == []


def test_a_window_without_room_for_the_forced_orders_is_refused(pilot_settings, inv, rp, pilot_dataset, inventory, pilot):
    with pytest.raises(GenerationError, match="forced purchase orders"):
        generate_replenishment(pilot_settings, inv, dataclasses.replace(rp, end_date=date(2024, 11, 25)),
                               pilot_dataset, inventory, pilot)


# --------------------------------------------------------------------------- the validator catches faults
def _with_transfer(plan, n, **changes):
    transfers = list(plan.transfers)
    transfers[n] = dataclasses.replace(transfers[n], **changes)
    return dataclasses.replace(plan, transfers=transfers)


def _with_line(plan, n, **changes):
    t = plan.transfers[n]
    return _with_transfer(plan, n, lines=(dataclasses.replace(t.lines[0], **changes),) + t.lines[1:])


def _with_po(plan, market, **changes):
    return dataclasses.replace(plan, purchase_orders=[dataclasses.replace(p, **changes) if p.market == market else p
                                                      for p in plan.purchase_orders])


def _po(plan, market):
    return next(p for p in plan.purchase_orders if p.market == market)


def _with_delivery(plan, market, note, **changes):
    p = _po(plan, market)
    return _with_po(plan, market, deliveries=tuple(dataclasses.replace(d, **changes) if d.note == note else d
                                                   for d in p.deliveries))


def _shift_transfer(plan, n, delta):
    t = plan.transfers[n]
    return _with_transfer(plan, n, dispatched_at=t.dispatched_at + delta, arrived_at=t.arrived_at + delta,
                          requested_at=t.requested_at + delta)


FAULTS = {
    "receipt at a store": (lambda p: _with_po(p, "SE", warehouse="SE-STO-01"), "receipt_location"),
    "transfer to another market": (lambda p: _with_transfer(p, 0, destination="NO-STO-01"), "transfer_route"),
    "reference names another store": (lambda p: _with_transfer(p, 0, reference=p.transfers[0].reference[:-1] + "9"),
                                      "reference_format"),
    "arrival before dispatch": (lambda p: _with_transfer(p, 0, arrived_at=p.transfers[0].dispatched_at - timedelta(minutes=1)),
                                "transit_time"),
    "arrival after the store opens": (lambda p: _with_transfer(p, 0, arrived_at=p.transfers[0].arrived_at + timedelta(hours=3)),
                                      "arrival_time"),
    "dispatch on a Sunday": (lambda p: _shift_transfer(p, 0, timedelta(days=-1)), "dispatch_time"),
    "receipt on a Saturday": (lambda p: _with_delivery(p, "DK", 1, received_at=_po(p, "DK").deliveries[0].received_at
                                                       + timedelta(days=1)), "receipt_time"),
    "more dispatched than available": (lambda p: _with_line(p, 0, quantity=500, requested=500), "stock_negative"),
    "warehouse drawn below its reorder point": (lambda p: _with_line(p, 0, quantity=3), "warehouse_protection"),
    "R0 before the pilot ends": (lambda p: dataclasses.replace(p, revisions=[dataclasses.replace(
        p.revisions[0], at=PILOT_LAST_EVENT - timedelta(hours=1))] + p.revisions[1:]), "after_pilot"),
    "delivery after the window": (lambda p: _with_delivery(p, "NO", 1, received_at=datetime(2024, 12, 9, 8, tzinfo=timezone.utc)),
                                  "window"),
    "duplicate transfer reference": (lambda p: _with_transfer(p, 1, reference=p.transfers[0].reference), "unique_reference"),
    "split notes do not add up": (lambda p: _with_delivery(p, "SE", 2, lines=(dataclasses.replace(
        _po(p, "SE").deliveries[1].lines[0], quantity=5),)), "delivery_total"),
    "order not in case packs": (lambda p: _with_po(p, "DK", lines=(dataclasses.replace(_po(p, "DK").lines[0], quantity=7),)),
                                "case_pack"),
    "forced order missing": (lambda p: dataclasses.replace(p, purchase_orders=[o for o in p.purchase_orders if o.market != "FI"]),
                             "forced_count"),
    "store above its reorder point refilled": (lambda p: _with_line(p, 0, store_position=5), "transfer_trigger"),
    "forced order that the reorder point would have triggered": (
        lambda p: _with_po(p, "NO", lines=(dataclasses.replace(_po(p, "NO").lines[0], position=1),)), "forced_flag"),
    "revised reorder point at the opening quantity": (lambda p: dataclasses.replace(p, revisions=[dataclasses.replace(
        p.revisions[0], changes=(dataclasses.replace(p.revisions[0].changes[0], new=500),) + p.revisions[0].changes[1:])]
        + p.revisions[1:]), "reorder_point_below_opening"),
    "product not launched yet": (lambda p: _with_line(p, 0, sku="CAM-000007"), "product_on_sale"),
    "R0 at the wrong time": (lambda p: dataclasses.replace(p, revisions=[dataclasses.replace(
        p.revisions[0], at=p.revisions[0].at + timedelta(hours=1))] + p.revisions[1:]), "revision_time"),
    "purchase order numbering gap": (lambda p: _with_po(p, "FI", number="PO-FI-2024-000002", deliveries=tuple(
        dataclasses.replace(d, reference=f"PO-FI-2024-000002/DN{d.note}") for d in _po(p, "FI").deliveries)),
        "document_sequence"),
    "supplier lead time too short": (lambda p: _with_delivery(p, "FI", 1, received_at=_po(p, "FI").ordered_at
                                                              + timedelta(days=3, hours=1)), "lead_time"),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_the_validator_catches(fault, plan, pilot_dataset, inventory, inv, rp, pilot):
    mutate, rule = FAULTS[fault]
    assert rule in _rules(_validate(mutate(plan), pilot_dataset, inventory, inv, rp, pilot))


def test_the_validator_catches_a_warehouse_over_capacity(plan, pilot_dataset, inventory, inv, rp, pilot):
    tiny = dataclasses.replace(pilot_dataset, warehouses=[
        dataclasses.replace(w, storage_capacity_units=150) if w.location_code == "FI-WH-01" else w
        for w in pilot_dataset.warehouses])
    assert "warehouse_capacity" in _rules(validate_replenishment(plan, tiny, inventory, inv, rp, pilot))


def test_the_validator_catches_a_plan_built_on_other_stock(plan, pilot_dataset, inventory, inv, rp, pilot):
    other = dataclasses.replace(plan, base_stock={**plan.base_stock, ("SE-WH-01", "ACC-000010"): (99, 0)})
    assert "base_stock" in _rules(_validate(other, pilot_dataset, inventory, inv, rp, pilot))


# --------------------------------------------------------------------------- settings and command line
def _config_with(tmp_path: Path, old: str, new: str) -> Path:
    text = CONFIG.read_text(encoding="utf-8")
    assert old in text, old
    path = tmp_path / "generator.toml"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


INVALID = {
    "end before start": ("end_date = 2024-12-06", "end_date = 2024-11-10"),
    "transit past store opening": ("dispatch_hours = [6, 8]", "dispatch_hours = [8, 9]"),
    "missing case pack": (", ACCESSORIES = 10 }", " }"),
    "split without a forced order": ('split_deliveries = ["SE"]', 'split_deliveries = ["XX"]'),
    "unknown market": ("forced_reorders = { SE = 1,", "forced_reorders = { XX = 1, SE = 1,"),
    "review day not a working day": ("warehouse_review_weekday = 1", "warehouse_review_weekday = 7"),
    "store review before closing": ("store_review_hour = 20", "store_review_hour = 18"),
    "unknown protection": ('warehouse_protection = "reorder_point"', 'warehouse_protection = "half"'),
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_invalid_settings_are_refused(case, tmp_path, tx, pilot_dataset):
    path = _config_with(tmp_path, *INVALID[case])
    settings = load_settings(path, "pilot")
    with pytest.raises(SettingsError):
        load_replenishment_settings(settings, tx.store_hours, {c.category_code for c in pilot_dataset.product_categories})


def test_only_the_pilot_profile_is_approved(full_settings, tx, pilot_dataset):
    with pytest.raises(SettingsError, match="pilot profile only"):
        load_replenishment_settings(full_settings, tx.store_hours, {c.category_code for c in pilot_dataset.product_categories})


def test_dry_run_writes_the_preview_without_a_database_and_never_logs_the_password(tmp_path, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "never-in-the-log-42")
    config = tmp_path / "generator.toml"
    config.write_text(CONFIG.read_text(encoding="utf-8")
                      .replace('log_dir = "logs"', f'log_dir = "{(tmp_path / "logs").as_posix()}"')
                      .replace('csv_dir = "output/master_data"', f'csv_dir = "{(tmp_path / "out" / "master_data").as_posix()}"'),
                      encoding="utf-8")
    assert rp_cli.main(["--config", str(config), "--env-file", str(tmp_path / "none.env")]) == 0
    out = tmp_path / "out" / "replenishment" / "pilot"
    assert {p.name for p in out.iterdir()} == {"reorder_points.csv", "transfers.csv", "purchase_orders.csv", "receipts.csv",
                                               "units_of_work.csv", "reviews.csv", "summary.json"}
    assert "forced_reorder" in (out / "purchase_orders.csv").read_text(encoding="utf-8-sig")
    log = next((tmp_path / "logs").glob("replenishment_pilot_dry_run_*.log")).read_text(encoding="utf-8")
    assert "no database was contacted" in log and "never-in-the-log-42" not in log


@pytest.mark.parametrize("module", [rp_cli, tx_cli], ids=["replenishment", "transactions"])
def test_typed_confirmation_without_keyboard_input_cancels(module, monkeypatch):
    """Windows reports the NUL device as a terminal; reading it gives EOF. That must cancel, not crash."""
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))

    def no_input(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", no_input)
    args = ("nordiccart_oltp", 17) if module is rp_cli else ("nordiccart_oltp", 20, 100)
    assert module._confirm(*args) is False
