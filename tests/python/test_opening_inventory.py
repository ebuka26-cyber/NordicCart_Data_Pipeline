"""Unit tests for the opening inventory generator and its validation (no database).

Run from the repository folder:
    python -m pytest tests/python/test_opening_inventory.py -v
"""

from __future__ import annotations

import copy
import dataclasses
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from nordiccart.inventory import cli
from nordiccart.inventory.config import load_inventory_settings
from nordiccart.inventory.generate import generate_opening_inventory
from nordiccart.inventory.validate import validate_opening_inventory
from nordiccart.master_data.generate import generate_master_data
from nordiccart.settings import SettingsError, load_settings

# Fingerprint of the pilot opening inventory (seed 20261117, shipped config). If you change
# the seed or the [inventory] settings on purpose, copy the new value from the dry run log.
PILOT_INVENTORY_FINGERPRINT = "5c7a4eba5057caabaa0d18b6fed95d14ba2a11523a5fd0bf39f99d9e3df38d35"
NOT_ON_SALE_AT_OPENING = {"CAM-000007", "SMH-000011"}       # launched 2025-03-19 and 2025-10-19


@pytest.fixture(scope="module")
def pilot(pilot_settings, pilot_dataset):
    inv = load_inventory_settings(pilot_settings)
    return pilot_settings, inv, pilot_dataset, generate_opening_inventory(pilot_settings, inv, pilot_dataset)


@pytest.fixture(scope="module")
def full(full_settings, full_dataset):
    inv = load_inventory_settings(full_settings)
    return full_settings, inv, full_dataset, generate_opening_inventory(full_settings, inv, full_dataset)


def _issues(pilot, inventory):
    settings, inv, master, _ = pilot
    return validate_opening_inventory(inventory, master, settings, inv)


def _with(inventory, index, **changes):
    clone = copy.copy(inventory)
    clone.listings = list(inventory.listings)
    clone.listings[index] = dataclasses.replace(clone.listings[index], **changes)
    return clone


def _index(inventory, location_code, sku):
    return next(i for i, x in enumerate(inventory.listings) if x.key == (location_code, sku))


# --------------------------------------------------------------------------- consistency
def test_every_product_is_listed_once_at_every_location(pilot):
    _, _, master, inventory = pilot
    keys = [x.key for x in inventory.listings]
    assert len(keys) == len(set(keys)) == 320
    assert set(keys) == {(l.location_code, p.sku) for l in master.stock_locations for p in master.products}


def test_pilot_counts(pilot):
    inventory = pilot[3]
    stocked = inventory.movements()
    assert len(stocked) == 288 and sum(x.opening_quantity for x in inventory.listings) == 1460
    assert {x.sku for x in inventory.listings if not x.stocked} == NOT_ON_SALE_AT_OPENING
    assert len([x for x in inventory.listings if not x.stocked]) == 32


def test_only_products_on_sale_get_opening_stock(pilot):
    _, inv, master, inventory = pilot
    launched = {p.sku: p.launched_on for p in master.products}
    for x in inventory.listings:
        assert x.stocked == (launched[x.sku] <= inv.opening_date)


def test_opening_time_is_local_midnight_of_the_opening_day(pilot):
    inventory = pilot[3]
    expected = {"SE": datetime(2024, 10, 31, 23, tzinfo=timezone.utc), "NO": datetime(2024, 10, 31, 23, tzinfo=timezone.utc),
                "DK": datetime(2024, 10, 31, 23, tzinfo=timezone.utc), "FI": datetime(2024, 10, 31, 22, tzinfo=timezone.utc)}
    for x in inventory.listings:
        if x.stocked:
            assert x.occurred_at == expected[x.country_code] and x.external_reference == "OB-20241101"
        else:
            assert x.occurred_at is None and x.external_reference is None and x.opening_quantity == 0


def test_reorder_points_are_valid(pilot):
    for x in pilot[3].listings:
        assert isinstance(x.reorder_point, int) and x.reorder_point >= 0
        if x.stocked:
            assert x.reorder_point < x.opening_quantity


def test_pilot_passes_validation(pilot):
    assert _issues(pilot, pilot[3]) == []


def test_full_profile_passes_validation(full):
    settings, inv, master, inventory = full
    assert len(inventory.listings) == 8000 and len(inventory.movements()) == 6096
    assert validate_opening_inventory(inventory, master, settings, inv) == []


# --------------------------------------------------------------------------- reproducibility
def test_same_seed_gives_identical_inventory(pilot):
    settings, inv, master, inventory = pilot
    assert generate_opening_inventory(settings, inv, master).fingerprint() == inventory.fingerprint()


def test_pilot_fingerprint_is_stable(pilot):
    assert pilot[3].fingerprint() == PILOT_INVENTORY_FINGERPRINT


def test_other_seed_gives_other_quantities(pilot):
    settings, inv, master, inventory = pilot
    other = generate_opening_inventory(dataclasses.replace(settings, seed=settings.seed + 1), inv, master)
    assert other.fingerprint() != inventory.fingerprint()
    assert [x.key for x in other.listings] == [x.key for x in inventory.listings]   # same listings, other quantities


def test_pilot_listings_are_the_first_listings_of_the_full_profile(pilot, full):
    assert full[3].listings[:320] == pilot[3].listings


# --------------------------------------------------------------------------- realistic variation
def test_quantities_vary_with_demand(pilot):
    inventory = pilot[3]
    stocked = inventory.movements()
    assert len({x.opening_quantity for x in stocked}) >= 20
    warehouse = [x for x in stocked if x.location_type == "warehouse"]
    assert min(x.opening_quantity for x in warehouse) == 4 and max(x.opening_quantity for x in warehouse) >= 40
    # Pearson correlation between expected weekly demand and opening stock in the warehouses
    xs = [float(x.expected_weekly_units) for x in warehouse]
    ys = [float(x.opening_quantity) for x in warehouse]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    corr = cov / ((sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5)
    assert corr > 0.9


def test_warehouse_holds_at_least_as_much_as_any_store_in_its_market(full):
    stores, warehouses = defaultdict(int), {}
    for x in full[3].movements():
        key = (x.country_code, x.sku)
        if x.location_type == "store":
            stores[key] = max(stores[key], x.opening_quantity)
        else:
            warehouses[key] = x.opening_quantity
    assert all(warehouses[k] >= v for k, v in stores.items())


def test_flagship_holds_at_least_as_much_as_standard_stores(full):
    _, _, master, inventory = full
    fmt = {s.location_code: s.store_format for s in master.stores}
    best_standard, flagship = defaultdict(int), {}
    for x in inventory.movements():
        if x.location_type != "store":
            continue
        key = (x.country_code, x.sku)
        if fmt[x.location_code] == "flagship":
            flagship[key] = x.opening_quantity
        else:
            best_standard[key] = max(best_standard[key], x.opening_quantity)
    assert all(flagship[k] >= v for k, v in best_standard.items())


def test_bigger_market_holds_more_stock(full):
    units = defaultdict(int)
    for x in full[3].listings:
        units[x.country_code] += x.opening_quantity
    assert units["SE"] > max(units["NO"], units["DK"], units["FI"])        # 40% of customers
    assert units["FI"] < units["DK"]                                       # 18% vs 22%


def test_cheaper_products_sell_more_units_within_a_category(full):
    # Rank correlation between price and expected chain demand, per category: negative.
    by_cat = defaultdict(dict)
    for x in full[3].listings:
        entry = by_cat[x.category_code].setdefault(x.sku, [x.price_eur, Decimal(0)])
        entry[1] += x.expected_weekly_units
    for cat, items in by_cat.items():
        prices = sorted(items.values(), key=lambda v: v[0])
        cheap = [v[1] for v in prices[: len(prices) // 3]]
        dear = [v[1] for v in prices[-(len(prices) // 3):]]
        assert sum(cheap) / len(cheap) > sum(dear) / len(dear), cat


def test_warehouse_capacity_is_respected_at_full_scale(full):
    _, _, master, inventory = full
    capacity = {w.location_code: w.storage_capacity_units for w in master.warehouses}
    units = defaultdict(int)
    for x in inventory.listings:
        if x.location_type == "warehouse":
            units[x.location_code] += x.opening_quantity
    assert all(units[code] <= capacity[code] for code in capacity)


# --------------------------------------------------------------------------- validator catches faults
def _rules(pilot, inventory):
    return {i.rule for i in _issues(pilot, inventory)}


def test_validator_detects_a_duplicate_listing(pilot):
    inventory = copy.copy(pilot[3])
    inventory.listings = pilot[3].listings + [pilot[3].listings[0]]
    assert {"unique_listing", "listing_count"} <= _rules(pilot, inventory)


def test_validator_detects_a_missing_listing(pilot):
    inventory = copy.copy(pilot[3])
    inventory.listings = pilot[3].listings[1:]
    assert "listing_count" in _rules(pilot, inventory)


FAULTS = [
    ("SE-WH-01", "SPH-000001", {"opening_quantity": -3}, "opening_quantity_range"),
    ("SE-WH-01", "SPH-000001", {"opening_quantity": Decimal("7.5")}, "whole_number"),
    ("SE-STO-02", "SPH-000001", {"reorder_point": -1}, "reorder_point_range"),
    ("SE-WH-01", "CAM-000007", {"opening_quantity": 5, "occurred_at": datetime(2024, 10, 31, 23, tzinfo=timezone.utc),
                                "external_reference": "OB-20241101"}, "opening_before_launch"),
    ("SE-WH-01", "SPH-000001", {"opening_quantity": 0, "occurred_at": None, "external_reference": None}, "opening_stock_missing"),
    ("SE-WH-01", "HPH-000005", {"reorder_point": 500}, "reorder_below_opening"),
    ("SE-STO-02", "HPH-000005", {"opening_quantity": 1, "reorder_point": 0}, "store_display_minimum"),
    ("SE-WH-01", "HPH-000005", {"opening_quantity": 5, "reorder_point": 1}, "warehouse_covers_stores"),
    ("SE-WH-01", "SPH-000001", {"occurred_at": datetime(2024, 11, 1, 12, tzinfo=timezone.utc)}, "opening_time"),
    ("SE-WH-01", "SPH-000001", {"external_reference": "X" * 65}, "external_reference"),
    ("SE-WH-01", "ACC-000015", {"opening_quantity": 200_000}, "warehouse_capacity"),
    ("SE-WH-01", "SPH-000001", {"location_code": "XX-WH-99"}, "fk_location"),
    ("SE-WH-01", "CAM-000007", {"occurred_at": datetime(2024, 10, 31, 23, tzinfo=timezone.utc)}, "zero_listing"),
]


@pytest.mark.parametrize("location_code, sku, changes, rule", FAULTS, ids=[f[3] for f in FAULTS])
def test_validator_detects(pilot, location_code, sku, changes, rule):
    inventory = _with(pilot[3], _index(pilot[3], location_code, sku), **changes)
    assert rule in _rules(pilot, inventory)


# --------------------------------------------------------------------------- settings and command line
def _write_config(tmp_path, **replacements):
    text = load_settings().config_path.read_text(encoding="utf-8")
    for old, new in replacements.items():
        assert old in text, old
        text = text.replace(old, new)
    path = tmp_path / "generator.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_opening_after_simulation_start_is_rejected(tmp_path):
    path = _write_config(tmp_path, **{"opening_date = 2024-11-01": "opening_date = 2024-12-01"})
    with pytest.raises(SettingsError, match="on or before calendar.simulation_start"):
        load_inventory_settings(load_settings(path), path)


def test_online_share_must_be_a_share(tmp_path):
    path = _write_config(tmp_path, **{"online_share = 0.55": "online_share = 1.55"})
    with pytest.raises(SettingsError, match="between 0 and 1"):
        load_inventory_settings(load_settings(path), path)


def test_missing_category_demand_stops_the_run(tmp_path):
    path = _write_config(tmp_path, **{
        "ACCESSORIES = { weekly_units = 14.0, reference_price_eur = 30 }": "",
        'log_dir = "logs"': f'log_dir = "{(tmp_path / "logs").as_posix()}"'})
    assert cli.main(["--config", str(path), "--no-csv"]) == 3


def test_dry_run_writes_the_preview_without_a_database(tmp_path, monkeypatch):
    path = _write_config(tmp_path, **{'csv_dir = "output/master_data"': f'csv_dir = "{(tmp_path / "out" / "master_data").as_posix()}"',
                                      'log_dir = "logs"': f'log_dir = "{(tmp_path / "logs").as_posix()}"'})
    monkeypatch.setenv("PGHOST", "host.invalid")             # would fail if contacted
    assert cli.main(["--config", str(path)]) == 0
    out = tmp_path / "out" / "opening_inventory" / "pilot"
    lines = (out / "opening_inventory.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 321 and lines[0].startswith("location_code,location_type")
    assert PILOT_INVENTORY_FINGERPRINT in (out / "summary.json").read_text(encoding="utf-8")


def test_password_never_reaches_the_log(tmp_path, monkeypatch, capsys):
    path = _write_config(tmp_path, **{'log_dir = "logs"': f'log_dir = "{(tmp_path / "logs").as_posix()}"'})
    for key in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGPASSFILE"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("PGHOST=127.0.0.1\nPGPORT=1\nPGDATABASE=nowhere\nPGUSER=nobody\nPGPASSWORD=Inventory-Secret-99\n",
                   encoding="utf-8")
    assert cli.main(["--config", str(path), "--env-file", str(env), "--plan", "--no-csv"]) == 3
    text = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "logs").glob("*.log"))
    out = capsys.readouterr()
    assert "Inventory-Secret-99" not in text + out.out + out.err


def test_opening_date_is_the_first_simulated_day(pilot):
    settings, inv, _, _ = pilot
    assert inv.opening_date == settings.simulation_start == date(2024, 11, 1)
