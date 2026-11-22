"""Calibration tests of the demand model on the full profile (Phase 3, step 5, stage S2). No database.

Design 11.1:
  * exact figures of the full profile (20,000 customers, 500 products, built in memory),
    compared with the design's stated values;
  * seeded statistical criteria: one simulation of the generation window (2024-12-03 to
    2026-10-31) for each demand seed 1 to 5; every check of calibration.statistical_checks
    must pass for every seed (about 100 checks a seed: volumes, purchase frequency, zero-order
    shares, concentration, months, weekdays, Black Week, hours, channel, markets, locations,
    lifecycle rates, units per order, category mix, personal rates).
The draws are deterministic, so these tests give the same result on every run.
Run time: about one minute (five simulations of about 72,000 orders).
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest

from nordiccart.demand.calibration import exact_figures, simulated_metrics, statistical_checks
from nordiccart.demand.config import load_demand_settings
from nordiccart.demand.model import DemandModel

# Demand seeds 1 to 5 (design 11.1); NORDICCART_CALIBRATION_SEEDS="7 8" runs others.
SEEDS = tuple(int(s) for s in os.environ.get("NORDICCART_CALIBRATION_SEEDS", "1 2 3 4 5").split())
D = Decimal


@pytest.fixture(scope="module")
def model(full_settings, full_dataset):
    return DemandModel(load_demand_settings(full_settings), full_settings, full_dataset)


@pytest.fixture(scope="module")
def figures(model):
    return exact_figures(model)


@pytest.fixture(scope="module")
def runs(model, figures):
    cache: dict = {}

    def run(seed: int):
        if seed not in cache:
            metrics = simulated_metrics(model, seed)
            cache[seed] = (metrics, statistical_checks(model, figures, metrics))
        return cache[seed]
    return run


def _close(actual, expected, relative):
    return abs(float(actual) / float(expected) - 1) <= relative


# --------------------------------------------------------------------------- exact (full profile)
def test_full_profile_volumes_match_the_design(figures):
    v = figures["volume"]
    assert _close(v["primary year"]["customer_years"], 17_821, 0.001)
    assert _close(v["growth year"]["customer_years"], 12_424, 0.002)
    assert _close(v["primary year"]["expected_orders"], 42_772, 0.001)
    assert _close(v["growth year"]["expected_orders"], 29_817, 0.002)
    assert _close(v["generation window"]["expected_orders"], 72_589, 0.001)
    for period in v.values():                          # expected volume = λ̄ x seasonal customer-years, exactly
        assert abs(period["expected_orders"] - period["lambda_x_customer_years"]) < D("1e-15")
    assert abs(v["generation window"]["expected_online_share"] - D("0.55")) < D("1e-20")


def test_full_profile_customer_groups_match_the_design(figures):
    g = figures["primary_year_groups"]
    assert (g["full_year_customers"], g["partial_customers"]) == (16_014, 3_986)
    assert abs(g["full_year_exposure_min"] - 1) < D("1e-20") and abs(g["full_year_exposure_max"] - 1) < D("1e-20")
    assert abs(float(g["partial_mean_exposure"]) - 0.453) < 0.002
    assert abs(float(g["partial_expected_zero_share"]) - 0.553) < 0.001
    assert abs(float(g["all_expected_zero_share"]) - 0.374) < 0.001


def test_units_per_order_and_outcomes(figures):
    assert 1.40 <= float(figures["units_per_order_generation_window"]) <= 1.50     # design target 1.45 +- 0.05
    assert figures["mean_lines_per_order"] == D("1.35")
    for outcomes in figures["outcomes"].values():
        assert abs(sum(outcomes.values()) - 1) < D("1e-20")


def test_november_2025_and_black_week(model):
    per_day = model.expected_orders_by(date(2025, 11, 1), date(2025, 11, 30), lambda loc, d: d)
    black = [v for d, v in per_day.items() if date(2025, 11, 24) <= d <= date(2025, 11, 30)]
    other = [v for d, v in per_day.items() if not date(2025, 11, 24) <= d <= date(2025, 11, 30)]
    assert _close(sum(per_day.values()), 4_613, 0.01)                    # design: about 4,613
    assert _close(sum(black) / 7, 315, 0.03) and _close(sum(other) / 23, 105, 0.03)


# --------------------------------------------------------------------------- statistical (seeds 1 to 5)
@pytest.mark.parametrize("seed", SEEDS)
def test_statistical_criteria_hold_for_every_seed(runs, seed):
    metrics, checks = runs(seed)
    failed = [f"{c.name}: {c.actual:.5f} not in [{c.low:.5f}, {c.high:.5f}]" for c in checks if not c.passed]
    assert not failed, failed
    assert len(checks) >= 100
    assert metrics["full_year_customers"] == 16_014 and metrics["partial_customers"] == 3_986


def test_the_seeds_spread_around_the_expectation(runs, figures):
    primary = [runs(seed)[0]["orders_primary_year"] for seed in SEEDS]
    expected = float(figures["volume"]["primary year"]["expected_orders"])
    mean = sum(primary) / len(primary)
    assert abs(mean / expected - 1) < 0.015                              # 5 seeds, each about +-1%
    assert len(set(primary)) == len(primary)                             # the seeds really differ


def test_black_week_and_hours_are_in_the_draws(runs, model):
    metrics, checks = runs(SEEDS[0])
    names = {c.name for c in checks}
    assert "Black Week share of November 2025 orders" in names
    assert sum(1 for n in names if n.startswith("hour share ")) == len(model.calendar.hours["online"]) + \
        len(model.calendar.hours["in_store"])
    assert metrics["black_week_orders"] / metrics["november_orders"] > 0.4      # 7 days at x3 of 30
