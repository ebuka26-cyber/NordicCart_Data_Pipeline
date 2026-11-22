"""Command line: generate, validate and (optionally) seed the NordicCart master data.

Modes
  (default)  dry run: generate, validate, write the CSV preview. No database is touched.
  --plan     also connect and compare with the database, read-only. Nothing is written.
  --apply    insert the missing rows in one transaction, after you type the database
             name to confirm (or pass --yes). Existing rows are verified, never changed.

Exit codes: 0 success, 1 validation failed, 2 conflicts with existing rows,
3 configuration, connection or database precondition problem, 4 cancelled.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from ..logging_setup import setup_logging
from ..settings import (
    DEFAULT_CONFIG, DEFAULT_ENV_FILE, SettingsError, describe_connection_target, load_env_file, load_settings,
)
from .export import export_csv
from .generate import describe, generate_master_data
from .validate import validate_master_data

log = logging.getLogger("nordiccart.master_data")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_master_data_seed",
        description="Generate, validate and seed NordicCart master data (countries, currencies, "
                    "categories, products and prices, stores, warehouses, customers).")
    p.add_argument("--profile", help="dataset size from config/generator.toml (pilot or full); default: default_profile")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="compare with the database, read-only")
    mode.add_argument("--apply", action="store_true", help="insert missing rows (asks for confirmation)")
    p.add_argument("--yes", action="store_true", help="with --apply: skip the typed confirmation")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="generator settings (default: config/generator.toml)")
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="database settings (default: config/.env)")
    p.add_argument("--no-csv", action="store_true", help="skip the CSV preview")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"], help="console detail")
    return p.parse_args(argv)


def _confirm(database: str, rows: int) -> bool:
    if not sys.stdin.isatty():
        log.error("Not an interactive terminal: add --yes to confirm the insert.")
        return False
    answer = input(f"\nType the database name ({database}) to insert {rows} rows, or press Enter to cancel: ").strip()
    return answer == database


def main(argv=None) -> int:
    args = parse_args(argv)
    mode = "apply" if args.apply else "plan" if args.plan else "dry_run"
    try:
        settings = load_settings(args.config, args.profile)
    except SettingsError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 3
    log_path = setup_logging(settings.log_dir, f"master_data_{settings.profile}_{mode}", args.log_level)
    started = time.perf_counter()
    log.info("NordicCart master data | mode=%s | profile=%s | seed=%s", mode, settings.profile, settings.seed)
    log.info("config: %s | log file: %s", settings.config_path, log_path)

    # 1. Generate --------------------------------------------------------------
    dataset = generate_master_data(settings)
    counts = dataset.row_counts()
    log.info("generated %d rows: %s", dataset.total_rows(), ", ".join(f"{k}={v}" for k, v in counts.items()))
    for key, value in describe(dataset, settings).items():
        log.info("  %s: %s", key, value)
    fingerprint = dataset.fingerprint()
    log.info("dataset fingerprint (SHA-256): %s", fingerprint)

    # 2. Validate --------------------------------------------------------------
    issues = validate_master_data(dataset, settings)
    if issues:
        log.error("validation found %d issues; nothing was written:", len(issues))
        for issue in issues[:50]:
            log.error("  %s", issue)
        return 1
    log.info("validation: 0 issues (schema, keys, foreign keys, checks, V006/V007 prerequisites, data quality)")

    if not args.no_csv:
        out = export_csv(dataset, settings.csv_dir / settings.profile)
        log.info("CSV preview: %s", out)

    if mode == "dry_run":
        log.info("dry run finished in %.1f s: no database was contacted. Use --plan or --apply next.",
                 time.perf_counter() - started)
        return 0

    # 3. Database --------------------------------------------------------------
    import psycopg  # imported here so a dry run works without a database driver installed

    from .load import (
        ConflictError, PlanChangedError, PreconditionError, VerificationError, apply_load, check_database, connect,
        plan_load, verify_only,
    )

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
            info = check_database(conn)
        except PreconditionError as exc:
            log.error("%s", exc)
            return 3
        log.info("connected to %s as %s (PostgreSQL %s), migrations %s",
                 info["database"], info["user"], info["server_version"], ",".join(info["migrations"]))

        plan = plan_load(conn, dataset)
        for line in plan.lines():
            log.info("  %s", line)
        for table in plan.tables.values():
            for item in table.conflicts[:20]:
                log.error("  CONFLICT %s %s", table.name, item)
            for item in table.drift[:5]:
                log.info("  drift (allowed) %s %s", table.name, item)
        if plan.conflict_count:
            log.error("%d rows already exist with different values. Nothing was written. "
                      "Check the seed and config, or the rows listed above.", plan.conflict_count)
            return 2
        extra = sum(t.extra_in_db for t in plan.tables.values())
        if extra:
            log.info("%d rows in the database are not part of this dataset; they are left untouched.", extra)

        if mode == "plan":
            log.info("plan finished: %d rows would be inserted, nothing was written.", plan.total_to_insert)
            return 0

        if plan.total_to_insert == 0:
            checked, problems = verify_only(conn, dataset)
            if problems:
                log.error("verification found %d differences: %s", len(problems), problems[:5])
                return 2
            log.info("nothing to insert: the database already holds all %d rows (verified).", checked)
            return 0

        if not args.yes and not _confirm(info["database"], plan.total_to_insert):
            log.warning("cancelled: nothing was written.")
            return 4
        try:
            result = apply_load(conn, dataset, confirmed=plan)
        except (ConflictError, PlanChangedError, VerificationError) as exc:
            log.error("%s The transaction was rolled back.", exc)
            return 2
        except psycopg.Error as exc:
            log.error("database error (%s): %s The transaction was rolled back.",
                      getattr(exc, "sqlstate", None), str(exc).strip().splitlines()[0])
            return 3
        log.info("committed: %d rows inserted, %d rows verified, %d change_log rows written",
                 sum(result.inserted.values()), result.verified_rows, result.change_log_rows)
        log.info("finished in %.1f s", time.perf_counter() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
