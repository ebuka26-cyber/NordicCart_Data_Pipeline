"""Command line: generate, validate and (optionally) load the NordicCart pilot transactions.

Modes
  (default)  dry run: generate, validate, write the CSV preview. No database is touched.
  --plan     also connect and compare with the database, read-only. Nothing is written.
  --apply    run the remaining units of work (orders, reservations, payments, fulfilment),
             each in its own transaction, after you type the database name to confirm (or
             pass --yes). Safe to repeat: an interrupted run continues, a finished one does nothing.

Needs the master data and the opening inventory loaded first, with the same config.
Exit codes: 0 success, 1 validation failed, 2 conflict with the database or a unit of work
gave an unplanned result, 3 configuration, connection or database precondition problem,
4 cancelled.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ..inventory.config import load_inventory_settings
from ..inventory.generate import generate_opening_inventory
from ..logging_setup import setup_logging
from ..master_data.generate import generate_master_data
from ..master_data.model import canonical
from ..settings import (
    DEFAULT_CONFIG, DEFAULT_ENV_FILE, SettingsError, describe_connection_target, load_env_file, load_settings,
)
from .config import load_transaction_settings
from .generate import GenerationError, generate_transactions
from .model import TransactionPlan
from .validate import validate_transactions

log = logging.getLogger("nordiccart.transactions")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_transactions",
        description="Generate, validate and load the NordicCart pilot transactions "
                    "(orders, reservations, payments and fulfilment through the V006/V007 units of work).")
    p.add_argument("--profile", help="dataset profile from config/generator.toml; only pilot is approved")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="compare with the database, read-only")
    mode.add_argument("--apply", action="store_true", help="write the remaining units of work (asks for confirmation)")
    p.add_argument("--yes", action="store_true", help="with --apply: skip the typed confirmation")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="generator settings (default: config/generator.toml)")
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="database settings (default: config/.env)")
    p.add_argument("--no-csv", action="store_true", help="skip the CSV preview")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"], help="console detail")
    return p.parse_args(argv)


def _write(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if canonical(v) is None else canonical(v) for v in row])


def export_csv(plan: TransactionPlan, directory: Path, fingerprints: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    order_cols = ["order_number", "scenario", "customer_number", "channel", "store_code", "fulfilment_location_code",
                  "country_code", "currency_code", "items_total", "shipping_fee", "shipping_vat_amount", "order_total",
                  "shipping_address_line1", "shipping_postal_code", "shipping_city", "placed_at", "reserve_at",
                  "reserve_outcome", "reservation_expires_at", "final_status", "cancellation_reason", "confirmed_at",
                  "fulfilled_at", "cancelled_at", "reservation_close_status", "reservation_closed_at", "holder_order_number"]
    _write(directory / "orders.csv", order_cols, [[getattr(o, c) for c in order_cols] for o in plan.orders])
    line_cols = ["line_number", "sku", "quantity", "unit_price", "unit_cost", "discount_percent", "discount_amount",
                 "line_total", "vat_rate", "vat_amount"]
    _write(directory / "order_items.csv", ["order_number"] + line_cols,
           [[o.order_number] + [getattr(x, c) for c in line_cols] for o in plan.orders for x in o.lines])
    attempt_cols = ["attempt_number", "payment_method", "payment_provider", "amount", "outcome", "attempted_at",
                    "authorized_at", "captured_at", "failed_at", "failure_reason", "provider_reference"]
    _write(directory / "payment_attempts.csv", ["order_number"] + attempt_cols,
           [[o.order_number] + [getattr(a, c) for c in attempt_cols] for o in plan.orders for a in o.attempts])
    _write(directory / "units_of_work.csv", ["position", "at_utc", "order_number", "unit_of_work"],
           [[i, e.at, e.order_number, e.label()] for i, e in enumerate(plan.events(), start=1)])
    summary = {
        "profile": plan.profile,
        "seed": plan.seed,
        "order_window": f"{plan.start_date.isoformat()} to {plan.end_date.isoformat()}",
        "fingerprint_sha256": plan.fingerprint(),
        **fingerprints,
        **plan.summary(),
        "written_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return directory


def _confirm(database: str, orders: int, events: int) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        log.error("Not an interactive terminal: add --yes to confirm the load.")
        return False
    try:
        answer = input(f"\nType the database name ({database}) to write {orders} orders through {events} units of "
                       "work (orders, reservations, payments, fulfilment), or press Enter to cancel: ").strip()
    except EOFError:      # Windows reports the NUL device as a terminal; reading it gives EOF
        log.error("No keyboard input: add --yes to confirm the load.")
        return False
    return answer == database


def main(argv=None) -> int:
    args = parse_args(argv)
    mode = "apply" if args.apply else "plan" if args.plan else "dry_run"
    try:
        settings = load_settings(args.config, args.profile)
        inv_settings = load_inventory_settings(settings)
        tx = load_transaction_settings(settings, inv_settings.opening_date)
    except SettingsError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 3
    log_path = setup_logging(settings.log_dir, f"transactions_{settings.profile}_{mode}", args.log_level)
    started = time.perf_counter()
    log.info("NordicCart pilot transactions | mode=%s | profile=%s | seed=%s | orders placed %s to %s",
             mode, settings.profile, settings.seed, tx.start_date, tx.end_date)
    log.info("config: %s | log file: %s", settings.config_path, log_path)

    # 1. Generate ------------------------------------------------------------------
    master = generate_master_data(settings)
    try:
        inventory = generate_opening_inventory(settings, inv_settings, master)
        plan = generate_transactions(settings, tx, master, inventory, inv_settings.store_format_weight)
    except (KeyError, GenerationError) as exc:
        log.error("could not generate the pilot transactions: %s", exc)
        return 3
    fingerprints = {"master_data_fingerprint_sha256": master.fingerprint(),
                    "opening_inventory_fingerprint_sha256": inventory.fingerprint()}
    log.info("built on master data %s... and opening inventory %s...",
             fingerprints["master_data_fingerprint_sha256"][:16], fingerprints["opening_inventory_fingerprint_sha256"][:16])
    for key, value in plan.summary().items():
        log.info("  %s: %s", key, value)
    log.info("transaction plan fingerprint (SHA-256): %s", plan.fingerprint())

    # 2. Validate ------------------------------------------------------------------
    issues = validate_transactions(plan, master, inventory, tx)
    if issues:
        log.error("validation found %d issues; nothing was written:", len(issues))
        for issue in issues[:50]:
            log.error("  %s", issue)
        return 1
    log.info("validation: 0 issues (references, customer, product, location and date consistency, prices, VAT and "
             "totals, payment rules, lifecycle timing, independent stock replay)")
    for o in plan.orders:
        log.debug("  %s %-24s %-8s %-10s %s %s %s", o.order_number, o.scenario, o.channel, o.fulfilment_location_code,
                  o.order_total, o.currency_code, o.final_status)
    if not args.no_csv:
        out = export_csv(plan, settings.csv_dir.parent / "transactions" / settings.profile, fingerprints)
        log.info("CSV preview: %s", out)
    if mode == "dry_run":
        log.info("dry run finished in %.1f s: no database was contacted. Use --plan or --apply next.",
                 time.perf_counter() - started)
        return 0

    # 3. Database ------------------------------------------------------------------
    import psycopg  # imported here so a dry run works without a database driver installed

    from ..master_data.load import connect
    from .load import (
        LoaderBusyError, LoadInterrupted, OutcomeMismatchError, PlanChangedError, PreconditionError,
        TransactionConflictError, VerificationError, apply_transactions, check_transaction_database,
        plan_transactions, verify_transactions,
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
            info = check_transaction_database(conn, master, inventory)
        except PreconditionError as exc:
            log.error("%s", exc)
            return 3
        log.info("connected to %s as %s (PostgreSQL %s), migrations %s; master data and opening inventory "
                 "present and unchanged", info["database"], info["user"], info["server_version"],
                 ",".join(info["migrations"]))

        progress = plan_transactions(conn, plan)
        for line in progress.lines():
            log.info("  %s", line)
        for item in (progress.conflicts + progress.blocking)[:20]:
            log.error("  CONFLICT %s", item)
        if not progress.ok:
            log.error("the database does not match this plan (see CONFLICT lines). Nothing was written.")
            return 2
        if mode == "plan":
            log.info("plan finished: %d orders and %d units of work would be written, nothing was written.",
                     *progress.counts())
            return 0

        if not progress.remaining:
            checked, problems = verify_transactions(conn, plan)
            if problems:
                log.error("verification found %d problems: %s", len(problems), problems[:5])
                return 2
            log.info("nothing to write: the database already holds all %d pilot orders and every unit of work (verified).",
                     checked)
            return 0

        if not args.yes and not _confirm(info["database"], *progress.counts()):
            log.warning("cancelled: nothing was written.")
            return 4
        try:
            result = apply_transactions(conn, plan, confirmed=progress)
        except LoaderBusyError as exc:
            log.error("%s", exc)
            return 3
        except (TransactionConflictError, PlanChangedError) as exc:
            log.error("%s Nothing was written.", exc)
            return 2
        except (OutcomeMismatchError, VerificationError, LoadInterrupted) as exc:
            log.error("%s The failing unit of work was rolled back; the steps before it are committed. "
                      "Fix the cause and run --apply again: it continues where it stopped.", exc)
            return 2
        except psycopg.Error as exc:
            log.error("database error (%s): %s The failing unit of work was rolled back; run --apply again to "
                      "continue after fixing the cause.", getattr(exc, "sqlstate", None),
                      str(exc).strip().splitlines()[0])
            return 3
        log.info("committed: %d units of work in %d transactions (%s), %d orders placed, %d orders verified, "
                 "%d change_log rows written", result.events_run, result.events_run,
                 ", ".join(f"{k} {v}" for k, v in result.by_kind.items()), result.orders_placed,
                 result.verified_orders, result.change_log_rows)
        log.info("finished in %.1f s", time.perf_counter() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
