"""Command line: generate, validate and (optionally) load the NordicCart opening inventory.

Modes
  (default)  dry run: generate, validate, write the CSV preview. No database is touched.
  --plan     also connect and compare with the database, read-only. Nothing is written.
  --apply    insert the missing listings and opening balances in one transaction, after you
             type the database name to confirm (or pass --yes).

Needs the master data loaded first (python/run_master_data_seed.py --apply, same config).
Exit codes: 0 success, 1 validation failed, 2 conflict with existing stock records,
3 configuration, connection or database precondition problem, 4 cancelled.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from ..logging_setup import setup_logging
from ..master_data.generate import generate_master_data
from ..master_data.model import canonical
from ..settings import (
    DEFAULT_CONFIG, DEFAULT_ENV_FILE, SettingsError, describe_connection_target, load_env_file, load_settings,
)
from .config import load_inventory_settings
from .generate import generate_opening_inventory
from .model import OpeningInventory, OpeningListing
from .validate import validate_opening_inventory

log = logging.getLogger("nordiccart.inventory")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_opening_inventory",
        description="Generate, validate and load the NordicCart opening inventory "
                    "(one listing per product and location, opening_balance movements).")
    p.add_argument("--profile", help="dataset size from config/generator.toml (pilot or full); default: default_profile")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="compare with the database, read-only")
    mode.add_argument("--apply", action="store_true", help="insert missing listings and opening stock (asks for confirmation)")
    p.add_argument("--yes", action="store_true", help="with --apply: skip the typed confirmation")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="generator settings (default: config/generator.toml)")
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="database settings (default: config/.env)")
    p.add_argument("--no-csv", action="store_true", help="skip the CSV preview")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"], help="console detail")
    return p.parse_args(argv)


def export_csv(inventory: OpeningInventory, directory: Path, master_fingerprint: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    columns = [f.name for f in fields(OpeningListing)]
    with (directory / "opening_inventory.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for x in inventory.listings:
            values = asdict(x)
            writer.writerow(["" if canonical(values[c]) is None else canonical(values[c]) for c in columns])
    summary = {
        "profile": inventory.profile,
        "seed": inventory.seed,
        "opening_date": inventory.opening_date.isoformat(),
        "fingerprint_sha256": inventory.fingerprint(),
        "master_data_fingerprint_sha256": master_fingerprint,
        **{k: v for k, v in inventory.summary().items()},
        "written_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return directory


def _confirm(database: str, balances: int, movements: int, units: int) -> bool:
    if not sys.stdin.isatty():
        log.error("Not an interactive terminal: add --yes to confirm the insert.")
        return False
    answer = input(f"\nType the database name ({database}) to insert {balances} listings and {movements} "
                   f"opening balances ({units} units), or press Enter to cancel: ").strip()
    return answer == database


def main(argv=None) -> int:
    args = parse_args(argv)
    mode = "apply" if args.apply else "plan" if args.plan else "dry_run"
    try:
        settings = load_settings(args.config, args.profile)
        inv_settings = load_inventory_settings(settings)
    except SettingsError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 3
    log_path = setup_logging(settings.log_dir, f"opening_inventory_{settings.profile}_{mode}", args.log_level)
    started = time.perf_counter()
    log.info("NordicCart opening inventory | mode=%s | profile=%s | seed=%s | opening day %s",
             mode, settings.profile, settings.seed, inv_settings.opening_date)
    log.info("config: %s | log file: %s", settings.config_path, log_path)

    # 1. Generate ------------------------------------------------------------------
    master = generate_master_data(settings)
    master_fingerprint = master.fingerprint()
    log.info("master data: %d products, %d locations (fingerprint %s...)",
             len(master.products), len(master.stock_locations), master_fingerprint[:16])
    try:
        if settings.profile == "pilot":
            inventory = generate_opening_inventory(settings, inv_settings, master)
        else:                                  # S7: demand-calibrated opening stock of the non-pilot products (C3)
            from ..demand.config import load_demand_settings
            from ..demand.inventory import opening_inventory_for
            from ..demand.model import DemandModel
            model = DemandModel(load_demand_settings(settings), settings, master)
            inventory = opening_inventory_for(settings, inv_settings, master, model)
            log.info("full profile: pilot products keep the opening model; the other products use the demand-calibrated "
                     "opening stock and reorder points (design 6, decision C3)")
    except KeyError as exc:
        log.error("configuration is missing demand settings for category %s ([inventory.category_demand])", exc)
        return 3
    for key, value in inventory.summary().items():
        log.info("  %s: %s", key, value)
    not_on_sale = sorted({x.sku for x in inventory.listings if not x.stocked})
    if not_on_sale:
        log.info("  listed at 0, launched after %s: %s", inv_settings.opening_date, ", ".join(not_on_sale))
    log.info("opening inventory fingerprint (SHA-256): %s", inventory.fingerprint())

    # 2. Validate ------------------------------------------------------------------
    issues = validate_opening_inventory(inventory, master, settings, inv_settings)
    if issues:
        log.error("validation found %d issues; nothing was written:", len(issues))
        for issue in issues[:50]:
            log.error("  %s", issue)
        return 1
    log.info("validation: 0 issues (completeness, references, V007 rules, launch and opening dates, "
             "reorder points, display minimums, warehouse cover and capacity)")
    if not args.no_csv:
        out = export_csv(inventory, settings.csv_dir.parent / "opening_inventory" / settings.profile, master_fingerprint)
        log.info("CSV preview: %s", out)
    if mode == "dry_run":
        log.info("dry run finished in %.1f s: no database was contacted. Use --plan or --apply next.",
                 time.perf_counter() - started)
        return 0

    # 3. Database ------------------------------------------------------------------
    import psycopg  # imported here so a dry run works without a database driver installed

    from .load import (
        InventoryConflictError, PlanChangedError, PreconditionError, VerificationError, apply_inventory,
        check_inventory_database, plan_inventory, verify_inventory,
    )
    from ..master_data.load import connect

    loaded = load_env_file(args.env_file)
    log.info("database settings: %s (from %s)", describe_connection_target(),
             f"{args.env_file} [{', '.join(loaded)}] plus environment" if loaded else "environment")
    try:
        conn = connect()
    except psycopg.OperationalError as exc:
        log.error("could not connect: %s", str(exc).strip().splitlines()[0] if str(exc).strip() else exc)
        return 3

    with conn:
        try:
            info = check_inventory_database(conn, master)
        except PreconditionError as exc:
            log.error("%s", exc)
            return 3
        log.info("connected to %s as %s (PostgreSQL %s), migrations %s; master data present and unchanged",
                 info["database"], info["user"], info["server_version"], ",".join(info["migrations"]))

        plan = plan_inventory(conn, inventory)
        for line in plan.lines():
            log.info("  %s", line)
        for item in plan.conflicts[:20]:
            log.error("  CONFLICT %s", item)
        for item in plan.drift[:5]:
            log.info("  drift (allowed) %s", item)
        if plan.conflicts:
            log.error("%d listings conflict with stock records already in the database. Nothing was written.",
                      len(plan.conflicts))
            return 2
        if plan.extra_balances or plan.extra_opening_movements:
            log.info("%d listings and %d opening balances in the database are not part of this dataset; left untouched.",
                     plan.extra_balances, plan.extra_opening_movements)
        if mode == "plan":
            log.info("plan finished: %d listings and %d opening balances would be inserted, nothing was written.",
                     *plan.counts())
            return 0

        if plan.total_to_insert == 0:
            checked, problems = verify_inventory(conn, inventory)
            if problems:
                log.error("verification found %d problems: %s", len(problems), problems[:5])
                return 2
            log.info("nothing to insert: the database already holds the opening inventory for all %d listings (verified).",
                     checked)
            return 0

        units = sum(x.opening_quantity for x in plan.movements_to_insert)
        if not args.yes and not _confirm(info["database"], *plan.counts(), units):
            log.warning("cancelled: nothing was written.")
            return 4
        try:
            result = apply_inventory(conn, inventory, confirmed=plan)
        except (InventoryConflictError, PlanChangedError, VerificationError) as exc:
            log.error("%s The transaction was rolled back.", exc)
            return 2
        except psycopg.Error as exc:
            log.error("database error (%s): %s The transaction was rolled back.",
                      getattr(exc, "sqlstate", None), str(exc).strip().splitlines()[0])
            return 3
        log.info("committed: %d listings and %d opening balances (%d units) inserted, %d listings verified, "
                 "%d change_log rows written", result.balances_inserted, result.movements_inserted,
                 result.units_inserted, result.verified_listings, result.change_log_rows)
        log.info("finished in %.1f s", time.perf_counter() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
