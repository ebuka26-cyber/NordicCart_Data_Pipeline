"""Unit tests for the master data generator, its validation and its settings.

No database needed. Run from the repository folder:
    python -m pytest tests/python/test_master_data_generator.py -q
"""

from __future__ import annotations

import copy
import dataclasses
import os
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from nordiccart.master_data import cli
from nordiccart.master_data.generate import generate_master_data
from nordiccart.master_data.reference import load_reference
from nordiccart.master_data.rules import money, price_point
from nordiccart.master_data.validate import validate_master_data
from nordiccart.settings import SettingsError, load_env_file, load_settings

# Fingerprint of the pilot dataset for seed 20261117 and the shipped reference data.
# If you change the seed, config/generator.toml or reference_data on purpose, run the
# dry run and copy the new fingerprint here. If it changes without such a change,
# generation is no longer reproducible on this machine.
PILOT_FINGERPRINT = "c46181a25bd6fdc1ad2c8fedab185c19802041c46b4aae703e66a0f58d768f64"


# --------------------------------------------------------------------------- sizes and reference data
def test_pilot_has_the_requested_sizes(pilot_dataset):
    assert pilot_dataset.row_counts() == {
        "currencies": 4, "countries": 4, "product_categories": 10, "products": 20, "product_prices": 80,
        "stock_locations": 16, "stores": 12, "warehouses": 4, "customers": 100,
    }


def test_full_profile_sizes(full_dataset):
    counts = full_dataset.row_counts()
    assert (counts["products"], counts["product_prices"], counts["customers"]) == (500, 2000, 20000)


def test_reference_network_is_consistent():
    ref = load_reference()
    assert [m.country_code for m in ref.markets] == ["SE", "NO", "DK", "FI"]
    assert [m.currency_code for m in ref.markets] == ["SEK", "NOK", "DKK", "EUR"]
    assert {m.country_code: m.vat_rate for m in ref.markets} == {
        "SE": Decimal("0.2500"), "NO": Decimal("0.2500"), "DK": Decimal("0.2500"), "FI": Decimal("0.2550")}
    assert len(ref.stores) == 12 and len(ref.warehouses) == 4 and len(ref.categories) == 10
    for store in ref.stores:                       # in-store sign-ups use the store city
        assert store.city in {c.city for c in ref.market(store.country_code).cities}
    for market in ref.markets:
        assert sum(1 for w in ref.warehouses if w.country_code == market.country_code and w.ships_online_orders) == 1
        assert all(w.opened_on <= market.market_launched_on for w in ref.warehouses if w.country_code == market.country_code)


def test_every_category_is_in_the_pilot(pilot_dataset):
    assert {p.category_code for p in pilot_dataset.products} == {c.category_code for c in pilot_dataset.product_categories}


def test_every_market_has_pilot_customers(pilot_dataset):
    assert {c.country_code for c in pilot_dataset.customers} == {"SE", "NO", "DK", "FI"}
    assert {c.signup_channel for c in pilot_dataset.customers} == {"online", "in_store"}


# --------------------------------------------------------------------------- validation
def test_pilot_passes_validation(pilot_dataset, pilot_settings):
    assert validate_master_data(pilot_dataset, pilot_settings) == []


def test_full_passes_validation(full_dataset, full_settings):
    assert validate_master_data(full_dataset, full_settings) == []


# --------------------------------------------------------------------------- reproducibility
def test_same_seed_gives_identical_data(pilot_settings, pilot_dataset):
    assert generate_master_data(pilot_settings).fingerprint() == pilot_dataset.fingerprint()


def test_pilot_fingerprint_is_stable(pilot_dataset):
    assert pilot_dataset.fingerprint() == PILOT_FINGERPRINT


def test_other_seed_gives_other_data(pilot_settings, pilot_dataset):
    other = generate_master_data(dataclasses.replace(pilot_settings, seed=pilot_settings.seed + 1))
    assert other.fingerprint() != pilot_dataset.fingerprint()
    assert other.customers != pilot_dataset.customers
    assert other.stock_locations == pilot_dataset.stock_locations      # fixed network


def test_pilot_rows_are_the_first_rows_of_the_full_dataset(pilot_dataset, full_dataset):
    assert full_dataset.customers[:100] == pilot_dataset.customers
    assert full_dataset.products[:20] == pilot_dataset.products
    assert full_dataset.product_prices[:80] == pilot_dataset.product_prices


# --------------------------------------------------------------------------- money rules
@pytest.mark.parametrize("amount, currency, expected", [
    ("29.4", "EUR", "28.90"), ("349.2", "EUR", "349.00"), ("1290", "EUR", "1299.00"),
    ("3.1", "EUR", "4.90"), ("1234.5", "SEK", "1199.00"), ("402", "NOK", "399.00"),
    ("12345", "DKK", "11990.00"), ("12", "SEK", "49.00"),
])
def test_price_points(amount, currency, expected):
    assert price_point(Decimal(amount), currency) == Decimal(expected)


def test_money_rounds_half_up_not_to_even():
    assert money(Decimal("2.345")) == Decimal("2.35")
    assert money(Decimal("2.335")) == Decimal("2.34")
    assert money(Decimal("-2.345")) == Decimal("-2.35")


def test_prices_have_two_decimals_and_a_margin(pilot_dataset):
    vat = {c.country_code: c.vat_rate for c in pilot_dataset.countries}
    for p in pilot_dataset.product_prices:
        assert p.list_price.as_tuple().exponent == -2 and p.unit_cost.as_tuple().exponent == -2
        assert Decimal(0) < p.unit_cost < p.list_price / (1 + vat[p.country_code])


# --------------------------------------------------------------------------- validator catches faults
def _with(dataset, table, index, **changes):
    clone = copy.copy(dataset)
    rows = list(getattr(dataset, table))
    rows[index] = dataclasses.replace(rows[index], **changes)
    setattr(clone, table, rows)
    return clone


def _rules(dataset, settings):
    return {i.rule for i in validate_master_data(dataset, settings)}


FAULTS = [
    ("customers", 1, lambda d: {"email": d.customers[0].email.upper()}, {"email_format", "unique_email_case_insensitive"}),
    ("customers", 2, lambda d: {"birth_date": date(2015, 1, 1)}, {"adult_at_signup"}),
    ("customers", 3, lambda d: {"postal_code": "ABC"}, {"postal_code_format"}),
    ("customers", 4, lambda d: {"address_line1": None}, {"address_complete"}),
    ("customers", 5, lambda d: {"customer_number": "X1"}, {"customer_number_format"}),
    ("customers", 6, lambda d: {"registered_at": datetime(2030, 1, 1, tzinfo=timezone.utc)}, {"registration_window"}),
    ("customers", 7, lambda d: {"first_name": "x" * 101}, {"length"}),
    ("customers", 8, lambda d: {"signup_channel": "phone"}, {"enum"}),
    ("product_prices", 0, lambda d: {"currency_code": "EUR"}, {"price_currency"}),
    ("product_prices", 1, lambda d: {"unit_cost": Decimal("999999.00")}, {"positive_margin"}),
    ("product_prices", 2, lambda d: {"list_price": Decimal("10.001")}, {"numeric"}),
    ("product_prices", 3, lambda d: {"valid_to": d.product_prices[3].valid_from}, {"ck_product_prices_validity", "one_current_price"}),
    ("products", 0, lambda d: {"category_code": "TOASTERS"}, {"fk_products_category"}),
    ("products", 1, lambda d: {"product_name": "x" * 201}, {"length"}),
    ("stores", 0, lambda d: {"store_format": "mega"}, {"store_format"}),
    ("warehouses", 0, lambda d: {"ships_online_orders": False}, {"online_fulfilment"}),
    ("stock_locations", 0, lambda d: {"location_type": "store"}, {"stock_locations_subtype", "location_code_format"}),
    ("countries", 0, lambda d: {"vat_rate": Decimal("1.2500")}, {"ck_countries_vat_rate"}),
    ("countries", 1, lambda d: {"time_zone": "Mars/Olympus"}, {"time_zone"}),
]


@pytest.mark.parametrize("table, index, change, expected_rules", FAULTS, ids=[f"{t}-{sorted(r)[0]}" for t, _, _, r in FAULTS])
def test_validator_detects(pilot_dataset, pilot_settings, table, index, change, expected_rules):
    broken = _with(pilot_dataset, table, index, **change(pilot_dataset))
    assert expected_rules <= _rules(broken, pilot_settings)


def test_validator_detects_in_store_signup_before_any_store(pilot_dataset, pilot_settings):
    # FI-STO-01 opens 2020-03-07; a Finnish in-store sign-up on 2020-03-01 is impossible.
    idx = next(i for i, c in enumerate(pilot_dataset.customers) if c.country_code == "FI")
    broken = _with(pilot_dataset, "customers", idx, signup_channel="in_store",
                   registered_at=datetime(2020, 3, 1, 12, 0, tzinfo=timezone.utc))
    assert {"in_store_signup", "registration_window"} <= _rules(broken, pilot_settings)


def test_validator_detects_row_count_change(pilot_dataset, pilot_settings):
    clone = copy.copy(pilot_dataset)
    clone.customers = pilot_dataset.customers[:-1]
    assert "row_count" in _rules(clone, pilot_settings)


# --------------------------------------------------------------------------- settings and credentials
def _write_config(tmp_path, **overrides):
    text = (load_settings().config_path).read_text(encoding="utf-8")
    for old, new in overrides.items():
        assert old in text
        text = text.replace(old, new)
    path = tmp_path / "generator.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_unknown_profile_is_rejected():
    with pytest.raises(SettingsError, match="Unknown profile"):
        load_settings(profile="huge")


def test_future_simulation_end_is_rejected(tmp_path):
    path = _write_config(tmp_path, **{"simulation_end = 2026-10-31": "simulation_end = 2099-12-31"})
    with pytest.raises(SettingsError, match="past"):
        load_settings(path)


def test_shares_must_be_between_0_and_1(tmp_path):
    path = _write_config(tmp_path, **{"phone_share = 0.90": "phone_share = 1.5"})
    with pytest.raises(SettingsError, match="between 0 and 1"):
        load_settings(path)


def test_env_file_loads_without_overriding(tmp_path, monkeypatch):
    for key in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PGHOST", "already-set")
    env = tmp_path / ".env"
    env.write_text("# comment\nPGHOST=localhost\nPGPORT=5432\nPGPASSWORD=\"s3cret value\"\n\nexport PGUSER=postgres\n",
                   encoding="utf-8")
    loaded = load_env_file(env)
    assert sorted(loaded) == ["PGPASSWORD", "PGPORT", "PGUSER"]
    assert os.environ["PGHOST"] == "already-set"
    assert os.environ["PGPASSWORD"] == "s3cret value"


def test_dry_run_writes_preview_and_never_needs_a_database(tmp_path, monkeypatch):
    config = _write_config(tmp_path, **{'csv_dir = "output/master_data"': f'csv_dir = "{(tmp_path / "out").as_posix()}"',
                                        'log_dir = "logs"': f'log_dir = "{(tmp_path / "logs").as_posix()}"'})
    monkeypatch.setenv("PGHOST", "host.invalid")            # would fail if contacted
    assert cli.main(["--config", str(config)]) == 0
    out = tmp_path / "out" / "pilot"
    assert (out / "summary.json").read_text(encoding="utf-8").count(PILOT_FINGERPRINT) == 1
    lines = (out / "customers.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 101 and lines[0].startswith("customer_number,")
    assert list((tmp_path / "logs").glob("master_data_pilot_dry_run_*.log"))


def test_password_never_reaches_the_log(tmp_path, monkeypatch, capsys):
    config = _write_config(tmp_path, **{'log_dir = "logs"': f'log_dir = "{(tmp_path / "logs").as_posix()}"'})
    for key in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGPASSFILE"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("PGHOST=127.0.0.1\nPGPORT=1\nPGDATABASE=nowhere\nPGUSER=nobody\nPGPASSWORD=TopSecret-4711\n",
                   encoding="utf-8")
    code = cli.main(["--config", str(config), "--env-file", str(env), "--plan", "--no-csv"])
    assert code == 3                                         # nothing listens on port 1
    log_text = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "logs").glob("*.log"))
    captured = capsys.readouterr()
    assert "PGPASSWORD" in log_text                          # the name may appear ...
    assert "TopSecret-4711" not in log_text + captured.out + captured.err   # ... the value never
