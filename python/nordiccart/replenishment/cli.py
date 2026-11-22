"""Command line: generate, validate and (optionally) load the NordicCart replenishment pilot.

Modes
  (default)  dry run: generate, validate, write the CSV preview. No database is touched.
  --plan     also connect and compare with the database, read-only. Nothing is written.
  --apply    run the remaining units of work (reorder point revision, receipts, transfer
             dispatches and arrivals), each in its own transaction, after you type the database
             name to confirm (or pass --yes). Safe to repeat: an interrupted run continues, a
             finished one does nothing.

Needs the master data, the opening inventory and the complete pilot transactions loaded first,
with the same config. Exit codes: 0 success, 1 validation failed, 2 conflict with the database or
a unit of work gave an unplanned result, 3 configuration, connection or database precondition
problem, 4 cancelled.
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
from ..transactions.config import load_transaction_settings
from ..transactions.generate import GenerationError as TransactionGenerationError
from ..transactions.generate import generate_transactions
from ..transactions.validate import validate_transactions
from .config import load_replenishment_settings
from .generate import GenerationError, generate_replenishment
from .model import ReplenishmentPlan
from .validate import validate_replenishment

log = logging.getLogger("nordiccart.replenishment")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_replenishment",
        description="Generate, validate and load the NordicCart replenishment pilot (warehouse reorder point "
                    "revision, supplier receipts and warehouse-to-store transfers through V007).")
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


def export_csv(plan: ReplenishmentPlan, directory: Path, fingerprints: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    _write(directory / "reorder_points.csv",
           ["warehouse", "market", "revised_at", "sku", "old_reorder_point", "new_reorder_point", "echelon_weekly_units"],
           [[r.warehouse, r.market, r.at, c.sku, c.old, c.new, c.echelon_weekly_units] for r in plan.revisions for c in r.changes])
    _write(directory / "transfers.csv",
           ["reference", "market", "source", "destination", "trigger", "requested_at", "dispatched_at", "arrived_at",
            "line_number", "sku", "requested", "quantity", "store_available", "store_position", "reorder_point",
            "order_up_to"],
           [[t.reference, t.market, t.source, t.destination, t.trigger, t.requested_at, t.dispatched_at, t.arrived_at,
             x.line_number, x.sku, x.requested, x.quantity, x.store_available, x.store_position, x.reorder_point,
             x.order_up_to] for t in plan.transfers for x in t.lines])
    _write(directory / "purchase_orders.csv",
           ["po_number", "market", "warehouse", "forced", "ordered_at", "line_number", "sku", "trigger", "quantity",
            "case_pack", "position", "reorder_point", "order_up_to", "delivery_notes"],
           [[p.number, p.market, p.warehouse, p.forced, p.ordered_at, x.line_number, x.sku, x.trigger, x.quantity,
             x.case_pack, x.position, x.reorder_point, x.order_up_to, len(p.deliveries)]
            for p in plan.purchase_orders for x in p.lines])
    _write(directory / "receipts.csv",
           ["delivery_reference", "po_number", "note", "warehouse", "received_at", "sku", "quantity", "forced"],
           [[d.reference, p.number, d.note, p.warehouse, d.received_at, x.sku, x.quantity, p.forced]
            for p, d in plan.deliveries() for x in d.lines])
    _write(directory / "units_of_work.csv", ["position", "at_utc", "unit_of_work", "key"],
           [[i, e.at, e.label(), e.key] for i, e in enumerate(plan.events(), start=1)])
    _write(directory / "reviews.csv", ["at_utc", "kind", "market", "triggered", "note"],
           [[r.at, r.kind, r.market, r.triggered, r.note] for r in plan.reviews])
    summary = {
        "profile": plan.profile,
        "seed": plan.seed,
        "starts_after_utc": plan.start_after.isoformat(),
        "window": f"{plan.first_review_date.isoformat()} to {plan.end_date.isoformat()}",
        "fingerprint_sha256": plan.fingerprint(),
        **fingerprints,
        **plan.summary(),
        "backorders": plan.backorders,
        "written_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return directory


def _confirm(database: str, events: int) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        log.error("Not an interactive terminal: add --yes to confirm the load.")
        return False
    try:
        answer = input(f"\nType the database name ({database}) to write {events} replenishment units of work "
                       "(reorder points, receipts, transfers), or press Enter to cancel: ").strip()
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
    log_path = setup_logging(settings.log_dir, f"replenishment_{settings.profile}_{mode}", args.log_level)
    started = time.perf_counter()

    # 1. Generate ------------------------------------------------------------------
    master = generate_master_data(settings)
    try:
        rp = load_replenishment_settings(settings, tx.store_hours, {c.category_code for c in master.product_categories})
    except SettingsError as exc:
        log.error("Configuration error: %s", exc)
        return 3
    log.info("NordicCart replenishment pilot | mode=%s | profile=%s | seed=%s | window %s to %s",
             mode, settings.profile, settings.seed, rp.first_review_date, rp.end_date)
    log.info("config: %s | log file: %s", settings.config_path, log_path)
    try:
        inventory = generate_opening_inventory(settings, inv_settings, master)
        pilot = generate_transactions(settings, tx, master, inventory, inv_settings.store_format_weight)
        plan = generate_replenishment(settings, inv_settings, rp, master, inventory, pilot)
    except (KeyError, GenerationError, TransactionGenerationError) as exc:
        log.error("could not generate the replenishment pilot: %s", exc)
        return 3
    fingerprints = {"master_data_fingerprint_sha256": master.fingerprint(),
                    "opening_inventory_fingerprint_sha256": inventory.fingerprint(),
                    "pilot_transactions_fingerprint_sha256": pilot.fingerprint()}
    log.info("built on master data %s..., opening inventory %s... and pilot transactions %s... (last event %s UTC)",
             fingerprints["master_data_fingerprint_sha256"][:16], fingerprints["opening_inventory_fingerprint_sha256"][:16],
             fingerprints["pilot_transactions_fingerprint_sha256"][:16], plan.start_after.strftime("%Y-%m-%d %H:%M:%S"))
    for key, value in plan.summary().items():
        log.info("  %s: %s", key, value)
    log.info("replenishment plan fingerprint (SHA-256): %s", plan.fingerprint())

    # 2. Validate ------------------------------------------------------------------
    issues = validate_transactions(pilot, master, inventory, tx) + \
        validate_replenishment(plan, master, inventory, inv_settings, rp, pilot)
    if issues:
        log.error("validation found %d issues; nothing was written:", len(issues))
        for issue in issues[:50]:
            log.error("  %s", issue)
        return 1
    log.info("validation: 0 issues (pilot base, window and business-time order, echelon reorder points, document "
             "formats and numbering, routes, calendar and launch rules, reorder-point and forced triggers, case packs, "
             "split delivery, independent stock replay with conservation)")
    for t in plan.transfers:
        log.debug("  %s %s -> %s %s", t.reference, t.source, t.destination,
                  ", ".join(f"{x.sku} x{x.quantity}" for x in t.lines))
    for p in plan.purchase_orders:
        log.debug("  %s %s %s %s", p.number, "FORCED" if p.forced else "natural", p.warehouse,
                  ", ".join(f"{x.sku} x{x.quantity} ({x.trigger})" for x in p.lines))
    if not args.no_csv:
        out = export_csv(plan, settings.csv_dir.parent / "replenishment" / settings.profile, fingerprints)
        log.info("CSV preview: %s", out)
    if mode == "dry_run":
        log.info("dry run finished in %.1f s: no database was contacted. Use --plan or --apply next.",
                 time.perf_counter() - started)
        return 0

    # 3. Database ------------------------------------------------------------------
    import psycopg  # imported here so a dry run works without a database driver installed

    from ..master_data.load import PreconditionError, connect
    from .load import (
        LoaderBusyError, LoadInterrupted, OutcomeMismatchError, PlanChangedError, ReplenishmentConflictError,
        VerificationError, apply_replenishment, check_replenishment_database, plan_replenishment,
        verify_replenishment,
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
            info = check_replenishment_database(conn, master, inventory, pilot)
        except PreconditionError as exc:
            log.error("%s", exc)
            return 3
        log.info("connected to %s as %s (PostgreSQL %s), migrations %s; master data and opening inventory present and "
                 "unchanged; pilot transactions complete (%d units of work verified against their plan)",
                 info["database"], info["user"], info["server_version"], ",".join(info["migrations"]),
                 info["pilot_units_of_work"])

        progress = plan_replenishment(conn, plan)
        for line in progress.lines():
            log.info("  %s", line)
        for item in (progress.conflicts + progress.blocking)[:20]:
            log.error("  CONFLICT %s", item)
        if not progress.ok:
            log.error("the database does not match this plan (see CONFLICT lines). Nothing was written.")
            return 2
        if mode == "plan":
            if progress.documents_to_write:
                log.info("plan finished: %d units of work and %d V009 documents would be written, nothing was written.",
                         len(progress.remaining), progress.documents_to_write)
            else:
                log.info("plan finished: %d units of work would be written, nothing was written.", len(progress.remaining))
            return 0

        if not progress.remaining and not progress.documents_to_write:
            checked, problems = verify_replenishment(conn, plan)
            if problems:
                log.error("verification found %d problems: %s", len(problems), problems[:5])
                return 2
            log.info("nothing to write: the database already holds every replenishment unit of work (%d, verified).",
                     checked)
            return 0

        if not args.yes and not _confirm(info["database"], len(progress.remaining) + progress.documents_to_write):
            log.warning("cancelled: nothing was written.")
            return 4
        try:
            result = apply_replenishment(conn, plan, confirmed=progress)
        except LoaderBusyError as exc:
            log.error("%s", exc)
            return 3
        except (ReplenishmentConflictError, PlanChangedError) as exc:
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
        log.info("committed: %d units of work in %d transactions (%s), %d verified, %d change_log rows written",
                 result.events_run, result.events_run,
                 ", ".join(f"{k} {v}" for k, v in result.by_kind.items()), result.verified_events,
                 result.change_log_rows)
        if result.documents_written:
            log.info("committed: %d V009 documents (purchase orders, R0 revision sets) in one transaction; "
                     "no movement written", result.documents_written)
        log.info("finished in %.1f s", time.perf_counter() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
