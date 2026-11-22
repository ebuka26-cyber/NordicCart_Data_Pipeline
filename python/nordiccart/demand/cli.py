"""Command line: calibrate the demand model (no database).

    python python/run_demand_calibration.py                 full profile, demand seeds 1 to 5
    python python/run_demand_calibration.py --seeds 1 2     other seeds
    python python/run_demand_calibration.py --profile pilot

Builds the master data of the profile in memory, the demand model, its exact figures, the
comparison with the opening inventory model, and one seeded simulation of the generation
window per seed with the statistical checks of design 11.1. Writes
output/demand/<profile>/calibration.json and a log file. Exit codes: 0 every check passed,
1 a check failed, 3 configuration problem.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from ..inventory.generate import generate_opening_inventory
from ..logging_setup import setup_logging
from ..master_data.generate import generate_master_data
from ..settings import DEFAULT_CONFIG, SettingsError, load_settings
from .calibration import exact_figures, simulated_metrics, statistical_checks
from .config import load_demand_settings
from .inventory import opening_comparison, period_weekly_units, rcal_reorder_points, recalibrated_opening
from .model import DemandError, DemandModel

log = logging.getLogger("nordiccart.demand")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="run_demand_calibration",
                                description="Calibrate the NordicCart demand model in memory (no database).")
    p.add_argument("--profile", default="full", help="dataset profile (default: full)")
    p.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5], help="demand seeds (default: 1 to 5)")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="generator settings (default: config/generator.toml)")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"], help="console detail")
    return p.parse_args(argv)


def _plain(value):
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def inventory_figures(model: DemandModel, settings, master) -> dict:
    """Opening model versus demand model, recalibrated opening stock and R-cal reorder points."""
    ds = model.ds
    inventory = generate_opening_inventory(settings, ds.inventory, master)
    first_week = model.weekly_units(ds.generation_start)
    weeks = {"first combined week": first_week,
             "average week, growth year": period_weekly_units(model, *ds.growth_year),
             "average week, primary year": period_weekly_units(model, *ds.primary_year)}
    comparison = opening_comparison(model, inventory, weeks)
    recalibrated = recalibrated_opening(model, inventory, first_week)
    stocked = {(x.location_code, x.sku): x for x in inventory.listings if x.occurred_at is not None}
    by_type: dict = {}
    for key, (quantity, reorder) in recalibrated.items():
        x = stocked[key]
        row = by_type.setdefault(x.location_type, {"listings": 0, "old_opening": 0, "new_opening": 0,
                                                    "old_reorder_points": 0, "new_reorder_points": 0})
        row["listings"] += 1
        row["old_opening"] += x.opening_quantity
        row["new_opening"] += quantity
        row["old_reorder_points"] += x.reorder_point
        row["new_reorder_points"] += reorder
    pilot_skus = sorted({p.sku for p in master.products[:20]})
    pilot = [x for x in inventory.listings if x.sku in pilot_skus and x.occurred_at is not None]
    pilot_units = sum(x.opening_quantity for x in pilot)
    pilot_weekly = sum((first_week.get((x.location_code, x.sku), Decimal(0)) for x in pilot), Decimal(0))
    review = ds.generation_start
    rcal = rcal_reorder_points(model, review)
    rcal_by_type: dict = {}
    for (loc, sku), rop in rcal.items():
        rcal_by_type.setdefault(model.location_type[loc], 0)
        rcal_by_type[model.location_type[loc]] += rop
    old_rop_by_type: dict = {}
    for x in inventory.listings:
        if model.products[x.sku].on_sale(review):
            old_rop_by_type[x.location_type] = old_rop_by_type.get(x.location_type, 0) + x.reorder_point
    return {"comparison": comparison,
            "recalibrated_opening_by_location_type": by_type,
            "pilot_products": {"skus": pilot_skus, "stocked_listings": len(pilot), "opening_units": pilot_units,
                               "weekly_units_first_combined_week": pilot_weekly,
                               "weeks_of_cover": pilot_units / pilot_weekly if pilot_weekly else None},
            "rcal_review": {"day": review, "reorder_points_by_location_type": rcal_by_type,
                            "opening_model_reorder_points_by_location_type": old_rop_by_type}}


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings(args.config, args.profile)
        ds = load_demand_settings(settings)
    except SettingsError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 3
    log_path = setup_logging(settings.log_dir, f"demand_{settings.profile}_calibration", args.log_level)
    started = time.perf_counter()
    log.info("NordicCart demand calibration | profile=%s | catalogue seed=%s | demand seeds %s | no database",
             settings.profile, settings.seed, " ".join(str(s) for s in args.seeds))
    log.info("config: %s | log file: %s", settings.config_path, log_path)
    master = generate_master_data(settings)
    try:
        model = DemandModel(ds, settings, master)
    except DemandError as exc:
        log.error("%s", exc)
        return 3
    figures = exact_figures(model)
    for name, v in figures["volume"].items():
        log.info("expected %-18s %s to %s: %10.1f orders, %9.1f customer-years, online share %.4f",
                 name, v["first"], v["last"], v["expected_orders"], v["customer_years"], v["expected_online_share"])
    nb = figures["negative_binomial_full_year"]
    log.info("full year: P(0) %.4f, P(1) %.4f, variance/mean %s, top 20%% share %.4f", nb["P0"], nb["P1"],
             nb["variance_over_mean"], nb["top_20_percent_share"])
    log.info("units per order %.4f (generation window); online sign-up propensity %.4f",
             figures["units_per_order_generation_window"], figures["online_signup_propensity"])
    inventory = inventory_figures(model, settings, master)
    for name, w in inventory["comparison"]["weeks"].items():
        log.info("opening model %.1f weekly units vs demand model %.1f (%s): %.2f times; opening stock covers %.1f weeks",
                 inventory["comparison"]["old_weekly_units"], w["new_weekly_units"], name, w["old_over_new"],
                 w["opening_weeks_of_cover"])
    seeds = []
    failed = 0
    for seed in args.seeds:
        t0 = time.perf_counter()
        metrics = simulated_metrics(model, seed)
        checks = statistical_checks(model, figures, metrics)
        bad = [c for c in checks if not c.passed]
        failed += len(bad)
        log.info("seed %d: %d orders (primary year %d, growth year %d), %d of %d checks passed (%.1f s)", seed,
                 metrics["orders"], metrics["orders_primary_year"], metrics["orders_growth_year"],
                 len(checks) - len(bad), len(checks), time.perf_counter() - t0)
        for c in bad:
            log.error("  FAIL %s: %.5f not in [%.5f, %.5f] (expected %.5f)", c.name, c.actual, c.low, c.high, c.expected)
        seeds.append({"metrics": metrics, "checks": [c.as_dict() for c in checks]})
    out = settings.csv_dir.parent / "demand" / settings.profile
    out.mkdir(parents=True, exist_ok=True)
    payload = {"profile": settings.profile, "catalogue_seed": settings.seed, "demand_seeds": args.seeds,
               "written_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "exact": figures, "inventory": inventory, "seeds": seeds}
    path = out / "calibration.json"
    path.write_text(json.dumps(_plain(payload), indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("calibration figures: %s", path)
    log.info("finished in %.1f s: %s", time.perf_counter() - started,
             "every check passed" if not failed else f"{failed} checks failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
