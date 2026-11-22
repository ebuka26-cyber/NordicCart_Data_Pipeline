"""Deterministic tests of the demand model (Phase 3, step 5, stage S2). No database.

Exact criteria of design 11.1 (tolerance 1e-9 or better), the samplers, the shared
assumptions with the opening inventory model, the composition rule of the draws and the
inventory uses. The seeded statistical criteria are in test_demand_calibration.py.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nordiccart.demand import numeric
from nordiccart.demand.calendar import black_friday, black_week
from nordiccart.demand.config import load_demand_settings
from nordiccart.demand.inventory import rcal_cover_units, rcal_reorder_points, recalibrated_opening
from nordiccart.demand.model import DemandModel
from nordiccart.demand.simulate import customer_rate, simulate
from nordiccart.inventory.config import load_inventory_settings
from nordiccart.inventory.generate import display_minimum, generate_opening_inventory
from nordiccart.master_data.rules import stream
from nordiccart.replenishment.config import load_replenishment_settings
from nordiccart.replenishment.generate import generate_replenishment
from nordiccart.settings import SettingsError
from nordiccart.transactions.config import load_transaction_settings
from nordiccart.transactions.generate import generate_transactions

REPO_ROOT = Path(__file__).resolve().parents[2]
TINY = Decimal("1e-20")
D = Decimal


@pytest.fixture(scope="module")
def pilot_model(pilot_settings, pilot_dataset):
    return DemandModel(load_demand_settings(pilot_settings), pilot_settings, pilot_dataset)


@pytest.fixture(scope="module")
def full_model(full_settings, full_dataset):
    return DemandModel(load_demand_settings(full_settings), full_settings, full_dataset)


def _config(tmp_path, old: str, new: str) -> Path:
    text = (REPO_ROOT / "config" / "generator.toml").read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "generator.toml"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- pilots unchanged
def test_the_pilot_fingerprints_are_unchanged(pilot_settings, pilot_dataset):
    inv = load_inventory_settings(pilot_settings)
    inventory = generate_opening_inventory(pilot_settings, inv, pilot_dataset)
    tx = load_transaction_settings(pilot_settings, inv.opening_date)
    pilot = generate_transactions(pilot_settings, tx, pilot_dataset, inventory, inv.store_format_weight)
    rp = load_replenishment_settings(pilot_settings, tx.store_hours, {c.category_code for c in pilot_dataset.product_categories})
    plan = generate_replenishment(pilot_settings, inv, rp, pilot_dataset, inventory, pilot)
    assert pilot.fingerprint() == "fabcd571a3b9f66827f20e54d0ebc421be5f68937af558383ee4b84f7c466264"
    assert plan.fingerprint() == "858bbfd09597030b573273c9885ffae0c0299e4ed146e8aca7c98f0e05531236"


# --------------------------------------------------------------------------- settings
def test_settings_reuse_the_pilot_assumptions(pilot_settings):
    ds = load_demand_settings(pilot_settings)
    inv = load_inventory_settings(pilot_settings)
    tx = load_transaction_settings(pilot_settings, inv.opening_date)
    assert ds.online_share == tx.online_share and ds.lines_per_order == tx.lines_per_order
    assert ds.two_units_share == tx.two_units_share
    assert tuple(sorted(ds.hour_weights["online"]))[0] == tx.online_hours[0]
    assert max(ds.hour_weights["online"]) == tx.online_hours[1] - 1
    assert (min(ds.hour_weights["in_store"]), max(ds.hour_weights["in_store"]) + 1) == tx.store_hours
    assert ds.inventory == inv
    assert (ds.orders_per_customer_year, ds.rate_shape, ds.rate_scale) == (D("2.4"), D("0.8"), D(3))
    assert ds.seed == pilot_settings.seed and load_demand_settings(pilot_settings, seed=5).seed == 5


@pytest.mark.parametrize("old, new, message", [
    ("rate_shape = 0.8 ", "rate_shape = 0 ", "must be positive"),
    ("primary_year = [2025-11-01, 2026-10-31]", "primary_year = [2025-11-01, 2027-10-31]", "inside the calendar"),
    ("store_hour_weights = { 10 = 0.60,", "store_hour_weights = { 9 = 0.1, 10 = 0.60,", "exactly the keys"),
    ("in_store_signup_store_share = 0.70", "in_store_signup_store_share = 1.70", "between 0 and 1"),
    ("black_week_weight = 3 ", "black_week_weight = 0.5 ", "at least 1"),
    ('store_closed_days = ["12-25", "12-26", "01-01"]', 'store_closed_days = ["12-32"]', "MM-DD"),
    ("max_attempts = 2,", "max_attempts = 0,", "max_attempts >= 1"),
])
def test_invalid_settings_are_refused(pilot_settings, tmp_path, old, new, message):
    with pytest.raises(SettingsError, match=message):
        load_demand_settings(pilot_settings, config_path=_config(tmp_path, old, new))


# --------------------------------------------------------------------------- calendar (design 5.3)
def test_the_monthly_index_is_normalized_per_simulation_year(pilot_model):
    cal = pilot_model.calendar
    assert [(a, b) for a, b in cal.year_bounds] == [(date(2024, 11, 1), date(2025, 10, 31)),
                                                     (date(2025, 11, 1), date(2026, 10, 31)),
                                                     (date(2026, 11, 1), date(2027, 10, 31))]   # S10 extension
    raw_mean = D("365.45") / 365                                     # design: 1.001233
    assert abs(raw_mean - D("1.001233")) < D("1e-6")
    for first, last in cal.year_bounds:
        spans = cal.months_between(first, last)
        assert abs(sum(cal.normalized_index[(s.year, s.month)] * s.days for s in spans) - 365) < TINY
        assert abs(sum(s.share for s in spans) - 1) < TINY
    assert abs(cal.normalized_index[(2025, 11)] - D("1.45") / raw_mean) < TINY


def test_day_probabilities_sum_to_one_with_closed_days_and_black_week(pilot_model):
    cal = pilot_model.calendar
    for ch in ("online", "in_store"):
        for span in cal.months:
            assert abs(sum(cal.probability[ch][span.first:span.last + 1]) - 1) < TINY
        for first, last in cal.year_bounds:
            assert abs(cal.units(ch, first, last) - 1) < TINY            # a full year of exposure is 1
    p = cal.probability["in_store"]
    for d in cal.days:
        if d.isoweekday() == 7 or (d.month, d.day) in ((12, 25), (12, 26), (1, 1)):
            assert p[cal.index[d]] == 0
        else:
            assert p[cal.index[d]] > 0
    assert black_friday(2024) == date(2024, 11, 29) and black_friday(2025) == date(2025, 11, 28)
    assert black_week(2025) == (date(2025, 11, 24), date(2025, 11, 30))
    assert black_week(2024) == (date(2024, 11, 25), date(2024, 11, 30))    # its Sunday is in December
    for ch in ("online", "in_store"):
        q = cal.probability[ch]
        assert abs(q[cal.index[date(2025, 11, 24)]] / q[cal.index[date(2025, 11, 17)]] - 3) < TINY   # Mondays
        assert abs(q[cal.index[date(2025, 12, 1)]] / q[cal.index[date(2025, 12, 8)]] - 1) < TINY
    w = cal.probability["online"]                                          # weekday weights within a month
    assert abs(w[cal.index[date(2026, 3, 2)]] / w[cal.index[date(2026, 3, 7)]] - D("1.10") / D("0.85")) < TINY


def test_hour_probabilities_sum_to_one_over_open_hours(pilot_model):
    cal = pilot_model.calendar
    assert cal.hours["online"] == list(range(7, 23)) and cal.hours["in_store"] == list(range(10, 19))
    for ch in ("online", "in_store"):
        assert abs(sum(cal.hour_probability[ch].values()) - 1) < TINY
        assert 2 not in cal.hours[ch]                                       # no daylight-saving gap or repeat
    online = cal.hour_probability["online"]
    assert max(online, key=online.get) in (19, 20, 21)                      # evening peak
    store = cal.hour_probability["in_store"]
    assert max(store, key=store.get) in range(12, 17)


# --------------------------------------------------------------------------- distributions (design 5.1)
def test_negative_binomial_figures_of_the_design():
    pmf = numeric.nb_pmf(D("2.4"), D("0.8"), 3)
    assert [round(float(p), 4) for p in pmf] == [0.3299, 0.1979, 0.1336, 0.0935]
    assert round(float(1 - sum(pmf)), 4) == 0.2451
    assert abs(numeric.nb_zero(D("2.4"), D("0.8")) - D(4) ** D("-0.8")) < TINY
    table = {e: round(float(numeric.nb_zero(D("2.4") * D(e), D("0.8"))), 4) for e in ("0.25", "0.5", "0.75", "1")}
    assert table == {"0.25": 0.6391, "0.5": 0.4804, "0.75": 0.3895, "1": 0.3299}   # design 5.1: 63.9 / 48.0 / 39.0 / 33.0 %
    assert round(float(numeric.nb_zero(D("2.4"), D("0.42"))), 4) == 0.4494        # revision 1's 45% needs k 0.42


def test_samplers_are_deterministic_and_recorded():
    r1, r2 = stream(1, "s2-test", "gamma"), stream(1, "s2-test", "gamma")
    g = [numeric.gamma(r1, D("0.8"), D(3)) for _ in range(1000)]
    assert g == [numeric.gamma(r2, D("0.8"), D(3)) for _ in range(1000)]
    assert all(isinstance(x, Decimal) and x > 0 for x in g)
    assert hashlib.sha256("|".join(str(x) for x in g).encode()).hexdigest() == \
        "5d3ddf67f0bb753fa3bf5b7cda621681efd0cc00c26e62494a1a351bd604d811"
    r = stream(1, "s2-test", "poisson")
    p = [numeric.poisson(r, D("2.4")) for _ in range(1000)]
    assert p[:10] == [4, 1, 2, 2, 0, 1, 4, 5, 1, 1]
    assert hashlib.sha256("|".join(str(x) for x in p).encode()).hexdigest() == \
        "81b2f62bced2118f37e053360f46a34961168a8f5c320d3ebf8220cffa65dede"
    assert numeric.poisson(r, D(0)) == 0


@pytest.mark.parametrize("shape, scale", [("0.8", "3"), ("2.5", "1")])
def test_gamma_draws_have_the_right_moments(shape, scale):
    r = stream(7, "s2-test", "gamma-moments", shape)
    xs = [float(numeric.gamma(r, D(shape), D(scale))) for _ in range(20_000)]
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    assert abs(mean / (float(shape) * float(scale)) - 1) < 0.03
    assert abs(var / (float(shape) * float(scale) ** 2) - 1) < 0.10


@pytest.mark.parametrize("mean", ["0.05", "0.7", "6"])
def test_poisson_draws_have_the_right_moments(mean):
    r = stream(7, "s2-test", "poisson-moments", mean)
    xs = [numeric.poisson(r, D(mean)) for _ in range(20_000)]
    m = sum(xs) / len(xs)
    v = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    se = (float(mean) / len(xs)) ** 0.5
    assert abs(m - float(mean)) < 4 * se
    assert abs(v / float(mean) - 1) < 0.08


def test_weighted_picks_use_integer_weights_and_respect_the_lower_bound():
    cum = numeric.cumulative(numeric.to_ints([D(1), D(0), D(3), D(1)]))
    r = stream(1, "s2-test", "pick")
    picks = [numeric.pick_index(r, cum) for _ in range(5000)]
    assert 1 not in picks and abs(picks.count(2) / 5000 - 0.6) < 0.03
    assert all(numeric.pick_index(r, cum, low=2) >= 2 for _ in range(500))
    assert numeric.to_ints([D("1e-30"), D(1)])[0] == 1                       # positive stays positive


def test_generated_values_use_no_float_arithmetic():
    """Project rule: draws and model figures in Decimal; floats only in calibration statistics."""
    for name in ("numeric.py", "calendar.py", "model.py", "simulate.py", "inventory.py"):
        text = (REPO_ROOT / "python" / "nordiccart" / "demand" / name).read_text(encoding="utf-8")
        code = "\n".join(line.split("#")[0] for line in text.splitlines())
        assert not re.search(r"\bfloat\(|\.random\(\)|\.uniform\(|\bmath\.", code), name


# --------------------------------------------------------------------------- the model
def test_expected_volume_equals_rate_times_seasonal_customer_years(pilot_model):
    m = pilot_model
    for first, last in ((date(2024, 12, 3), date(2026, 10, 31)), (date(2025, 11, 1), date(2026, 10, 31)),
                        (date(2025, 2, 1), date(2025, 2, 28)), (date(2026, 10, 31), date(2026, 10, 31))):
        expected = m.expected_orders(first, last)
        assert abs(expected - m.ds.orders_per_customer_year * m.customer_years(first, last)) < D("1e-18")
        by_market = sum(m.expected_orders(first, last, market=k) for k in m.markets)
        by_location = sum(m.expected_orders(first, last, location=loc) for loc in m.locations)
        assert abs(by_market - expected) < D("1e-18") and abs(by_location - expected) < D("1e-18")


def test_channel_propensities_reach_the_online_share_exactly(pilot_model):
    m = pilot_model
    first, last = m.ds.channel_period
    share = m.expected_orders(first, last, channel="online") / m.expected_orders(first, last)
    assert abs(share - m.ds.online_share) < D("1e-20")
    for c in m.customers:
        if not c.has_address:
            assert c.online == 0
        elif c.signup_channel == "in_store":
            assert c.online == D("0.30")
        else:
            assert c.online == m.online_signup_propensity
    assert 0 < m.online_signup_propensity < 1


def test_registration_day_and_store_choice(pilot_model, pilot_dataset):
    m = pilot_model
    for c in m.customers:
        local = c.registered_local
        for ch in ("online", "in_store"):
            last_hour = m.calendar.last_hour(ch)
            expected = local.date() if local.hour < last_hour else local.date() + timedelta(days=1)
            assert c.start[ch] == (expected if expected <= m.calendar.end else None)
        assert abs(sum(c.store_probability) - 1) < TINY
        stores = {l.location_code: l for l in pilot_dataset.stock_locations}
        weights = [D(2) if m.store_format[s] == "flagship" else D(1) for s in c.stores]
        weights = [w * (3 if c.city and stores[s].city == c.city else 1) for w, s in zip(weights, c.stores)]
        assert all(abs(p - w / sum(weights)) < TINY for p, w in zip(c.store_probability, weights))


def test_product_weights_are_the_opening_inventory_assumptions(full_model, full_settings, full_dataset):
    """Same category weights, price factors and seeded popularity as the opening model (one model)."""
    inv = load_inventory_settings(full_settings)
    inventory = generate_opening_inventory(full_settings, inv, full_dataset)
    weights = {k: v for k, v in full_settings.customer.country_weights.items()}
    share_market = {k: v / sum(weights.values()) for k, v in weights.items()}
    checked = 0
    for x in inventory.listings:
        if x.location_type != "warehouse":
            continue
        share = share_market[x.country_code] * (inv.online_share / 1)        # as the opening model multiplies
        expected = full_model.products[x.sku].base_weight * share
        assert expected.quantize(D("0.0001"), rounding="ROUND_HALF_UP") == x.expected_weekly_units
        checked += 1
    assert checked == 2000


def test_life_cycle_ramp_decline_and_floor(pilot_model):
    p = next(iter(pilot_model.products.values()))
    launch = p.launched_on
    lc = lambda days: pilot_model.life_cycle(p, launch + timedelta(days=days))   # noqa: E731
    assert lc(0) == D("0.4") and lc(42) == 1 and abs(lc(21) - D("0.7")) < TINY
    month = lambda n: date(launch.year + (launch.month - 1 + n) // 12, (launch.month - 1 + n) % 12 + 1, min(launch.day, 28))  # noqa: E731
    if launch.day <= 28:
        assert pilot_model.life_cycle(p, month(18)) == 1
        assert pilot_model.life_cycle(p, month(19)) == D("0.97")
        assert pilot_model.life_cycle(p, month(60)) == D("0.4")


def test_the_product_mix_of_a_day(full_model):
    for day in (date(2024, 12, 3), date(2026, 6, 15)):
        mix = full_model.mix(day)
        assert set(mix.skus) == {s for s, p in full_model.products.items() if p.on_sale(day)}
        assert abs(sum(w / mix.total for w in mix.weights) - 1) < TINY
        assert 1 <= mix.units_per_line <= 1 + full_model.ds.two_units_share
    assert len(full_model.mix(date(2026, 6, 15)).skus) > len(full_model.mix(date(2024, 12, 3)).skus)  # launches


def test_outcome_probabilities_follow_the_lifecycle_rates(pilot_model):
    online = pilot_model.outcome_probabilities("online")
    assert abs(sum(online.values()) - 1) < TINY
    assert abs(online["fulfilled"] - D("0.96") * D("0.94") * (1 + D("0.036") + D("0.036") ** 2)) < TINY
    store = pilot_model.outcome_probabilities("in_store")
    assert abs(store["fulfilled"] - D("0.998") * D("0.98") * (1 + D("0.01"))) < TINY
    assert store["expired"] == 0


def test_expected_units_follow_orders_lines_and_mix(full_model):
    m = full_model
    first, last = date(2025, 3, 3), date(2025, 3, 9)
    placed = m.expected_units(first, last, fulfilled=False)
    orders = m.expected_orders(first, last)
    assert abs(sum(placed.values()) - orders * m.units_per_order(first, last)) < D("1e-15")
    fulfilled = m.expected_units(first, last)
    assert sum(fulfilled.values()) < sum(placed.values())
    for loc in ("SE-WH-01", "FI-STO-02"):
        units = sum(v for (l, _), v in placed.items() if l == loc)
        daily = sum(v * m.mean_lines * m.mix(d).units_per_line for d, v in m.daily_orders(loc, first, last))
        assert abs(units - daily) < D("1e-15")


# --------------------------------------------------------------------------- draws
def test_draws_are_deterministic_and_respect_the_rules(pilot_model):
    m = pilot_model
    first, last = date(2024, 12, 3), date(2026, 10, 31)
    orders, rates = simulate(m, first, last)
    again, _ = simulate(m, first, last)
    assert orders == again and len(orders) > 100
    other = dataclasses.replace(m.ds, seed=m.ds.seed + 1)
    m2 = DemandModel.__new__(DemandModel)
    m2.__dict__.update(m.__dict__)
    m2.ds = other
    assert simulate(m2, first, last)[0] != orders
    assert rates[m.customers[0].number] == customer_rate(m, m.customers[0].number)
    customers = m.customer
    for o in orders:
        c = customers[o.customer_number]
        assert first <= o.local_day <= last and o.local_day >= c.start[o.channel]
        if o.local_day == c.registered_local.date():
            assert o.hour > c.registered_local.hour
        assert o.hour in m.calendar.hours[o.channel]
        assert m.calendar.probability[o.channel][m.calendar.index[o.local_day]] > 0      # open day
        assert o.location_code == c.warehouse if o.channel == "online" else o.location_code in c.stores
        skus = [s for s, _ in o.lines]
        assert len(set(skus)) == len(skus) and 1 <= len(skus) <= 3
        assert all(m.products[s].on_sale(o.local_day) for s in skus)
        assert all(q == 1 or (q == 2 and m.products[s].cheap) for s, q in o.lines)
        assert o.outcome in ("fulfilled", "payment_failed", "expired", "cancelled")
        assert o.outcome != "expired" or o.channel == "online"


def test_windows_compose_to_the_whole_period(pilot_model):
    """plan(window 1) + plan(window 2) = plan(both windows): design 7.2 composition rule."""
    m = pilot_model
    whole, _ = simulate(m, date(2025, 11, 1), date(2025, 12, 31))
    parts = []
    for first, last in ((date(2025, 11, 1), date(2025, 11, 27)), (date(2025, 11, 28), date(2025, 11, 28)),
                        (date(2025, 11, 29), date(2025, 12, 31))):
        parts += simulate(m, first, last)[0]
    key = lambda o: (o.local_day, o.customer_number, o.hour, o.lines)       # noqa: E731
    assert sorted(parts, key=key) == sorted(whole, key=key)


# --------------------------------------------------------------------------- inventory uses (design 6)
def test_recalibrated_opening_applies_the_opening_rules_unchanged(full_model, full_settings, full_dataset):
    """With the old weekly units the recalibration reproduces the opening model exactly."""
    inventory = generate_opening_inventory(full_settings, load_inventory_settings(full_settings), full_dataset)
    old_weekly = {(x.location_code, x.sku): x.expected_weekly_units for x in inventory.listings}
    rebuilt = recalibrated_opening(full_model, inventory, old_weekly)
    stocked = [x for x in inventory.listings if x.occurred_at is not None]
    assert len(rebuilt) == len(stocked) == 6096
    assert all(rebuilt[(x.location_code, x.sku)] == (x.opening_quantity, x.reorder_point) for x in stocked)


def test_rcal_reorder_points_follow_the_forecast(full_model):
    m = full_model
    review = date(2025, 3, 3)
    rops = rcal_reorder_points(m, review)
    inv = m.ds.inventory
    week = m.expected_units(review, review + timedelta(days=6))
    fortnight = m.expected_units(review, review + timedelta(days=13))
    echelon: dict = {}
    for (loc, sku), v in fortnight.items():
        key = (m.location_market[loc], sku)
        echelon[key] = echelon.get(key, D(0)) + v
    assert {m.location_type[loc] for loc, _ in rops} == {"store", "warehouse"}
    for (loc, sku), rop in rops.items():
        if m.location_type[loc] == "store":
            floor = display_minimum(inv, m.products[sku].price_eur, m.store_format[loc]) - 1
            demand = week.get((loc, sku), D(0))
            assert rop == max(int((demand * inv.safety_factor).to_integral_value("ROUND_CEILING")), floor)
        else:
            total = echelon[(m.location_market[loc], sku)]
            assert rop == max(int((total * inv.safety_factor).to_integral_value("ROUND_CEILING")), 2)
            assert rop >= int((fortnight.get((loc, sku), D(0)) * inv.safety_factor).to_integral_value("ROUND_FLOOR"))


def test_the_s10_extension_leaves_the_original_calendar_exactly_unchanged(full_settings, full_dataset, full_model):
    """S10: demand.generation_end extends the demand calendar; everything up to calendar.simulation_end is identical."""
    from nordiccart.demand.simulate import month_counts
    ds = full_model.ds
    assert (ds.planning_end, ds.calendar_end) == (date(2026, 10, 31), date(2027, 10, 31))
    old = DemandModel(dataclasses.replace(ds, calendar_end=ds.planning_end), full_settings, full_dataset)
    new = full_model
    for ch in ("online", "in_store"):
        for first, last in ((date(2024, 11, 1), date(2026, 10, 31)), (date(2025, 3, 15), date(2026, 10, 31)),
                            (date(2024, 12, 3), date(2025, 1, 10))):
            assert new.calendar.units(ch, first, last) == old.calendar.units(ch, first, last)      # exact, not close
    assert new.online_signup_propensity == old.online_signup_propensity
    for c_new, c_old in list(zip(new.customers, old.customers))[:300]:
        rate = customer_rate(new, c_new.number)
        before, after = month_counts(old, c_old, rate), month_counts(new, c_new, rate)
        assert all(after[k] == v for k, v in before.items()) and (2027, 10) in after
    for review in (date(2026, 9, 1), date(2026, 10, 1)):                 # reviews that looked past 2026-10-31
        assert rcal_cover_units(new, review) == rcal_cover_units(old, review)
        assert rcal_reorder_points(new, review) == rcal_reorder_points(old, review)
    assert old.calendar.end == date(2026, 10, 31)                    # without the extension: no demand after it
    assert new.expected_orders(date(2026, 11, 1), date(2026, 11, 30)) > 0


def test_rcal_cover_units_follow_the_forecast_and_the_season(full_model):
    """S8.1: the basis of S after R-cal: stores 6 weeks of their own demand, warehouses 10 weeks of echelon demand."""
    m = full_model
    review = date(2025, 3, 3)
    cover = rcal_cover_units(m, review)
    six = m.expected_units(review, review + timedelta(days=41), [l for l in m.locations if m.location_type[l] == "store"])
    ten = m.expected_units(review, review + timedelta(days=69))
    echelon: dict = {}
    for (loc, sku), v in ten.items():
        key = (m.warehouse[m.location_market[loc]], sku)
        echelon[key] = echelon.get(key, D(0)) + v
    assert {k: v for k, v in cover.items() if m.location_type[k[0]] == "store"} == six
    assert {k: v for k, v in cover.items() if m.location_type[k[0]] == "warehouse"} == echelon
    # the window follows the season: the November review covers Black Week and December, the June review the summer
    total = lambda day: sum(rcal_cover_units(m, day).values())   # noqa: E731
    assert total(date(2025, 11, 3)) > D("1.3") * total(date(2025, 6, 2))
