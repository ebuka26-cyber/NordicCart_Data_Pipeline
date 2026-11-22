"""Combined activity engine, stage S6 (design 7.1 to 7.3): plan or run UTC day windows.

    python python/run_activity.py --plan                     next window: plan and checks, writes nothing
    python python/run_activity.py --apply --windows 3 --yes  run (or resume) the next 3 windows
    python python/run_activity.py --tick --yes               S10 hourly tick: every window from the last verified end
                                                             to floor(now, hour), at most to the next UTC midnight each
                                                             (missed hours and days are caught up in order); an hour
                                                             already verified writes nothing. --now <ISO UTC> sets the
                                                             clock (tests). Stops at the generation horizon
                                                             ([demand] generation_end, S10): beyond it the demand model
                                                             has no calendar, so the tick logs it and writes nothing
    --replay auto|full|incremental                           auto (default): incremental from the last
                                                             checkpoint when it has a carry, else full

S6 is sandbox-only. --apply requires all of:
- NORDICCART_S6_SANDBOX=1;
- NORDICCART_S6_SANDBOX_CLUSTER set to the sandbox cluster's system identifier;
- a connected server that is that cluster, is not a protected (real) cluster, and is not the
  port and database configured in config/.env.

Exit codes:
- 0: done.
- 1: window or month-end verification failed (a failed window is marked failed; a failed month
  also blocks the next month's first window).
- 2: drift or conflict (blocking, nothing written).
- 3: configuration, connection or sandbox guard problem.
- 4: another engine or a pilot load holds the activity lock.
"""

import argparse
import logging
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import psycopg

from nordiccart.activity.engine import DAY, EngineContext, EngineError
from nordiccart.activity.monthly import MonthVerificationError, verify_month
from nordiccart.activity.run import (
    ACTIVITY_LOCK, DriftError, SandboxGuardError, WindowVerificationError, database_identity, is_month_start, plan_window,
    require_sandbox, run_window,
)
from nordiccart.logging_setup import setup_logging
from nordiccart.master_data.load import connect
from nordiccart.settings import DEFAULT_CONFIG, SettingsError, describe_connection_target, load_env_file, load_settings
from nordiccart.units.base import release_session_lock, try_session_lock

log = logging.getLogger("nordiccart.activity")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="plan the next window and run its checks (default)")
    mode.add_argument("--apply", action="store_true", help="run the next windows (sandbox only in S6)")
    mode.add_argument("--tick", action="store_true", help="hourly tick: windows up to floor(now, hour) (S10)")
    p.add_argument("--now", default=None, help="tick clock as an ISO UTC timestamp (default: the system clock)")
    p.add_argument("--windows", type=int, default=1, help="number of UTC day windows to run (default 1)")
    p.add_argument("--replay", choices=["auto", "full", "incremental"], default="auto")
    p.add_argument("--yes", action="store_true", help="do not ask for the database name")
    p.add_argument("--profile", default=None)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--env-file", type=Path, default=None)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    return p.parse_args(argv)


def _tick_end(conn, ctx: EngineContext, target: datetime) -> datetime | None:
    """End of the tick's next window: the target hour, at most the next UTC midnight; None when up to date.

    An open (running or failed) run is resumed with its stored end, whatever the clock says.
    """
    row = conn.execute("SELECT window_end, status::text FROM meta.activity_runs ORDER BY window_start DESC LIMIT 1").fetchone()
    if row is not None and row[1] in ("running", "failed"):
        return row[0]
    start = row[0] if row is not None else ctx.origin
    if start >= target:
        return None
    midnight = datetime.combine(start.astimezone(timezone.utc).date() + timedelta(days=1), dtime(0), timezone.utc)
    return min(target, midnight)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings(args.config, args.profile)
    except SettingsError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 3
    mode = "tick" if args.tick else "apply" if args.apply else "plan"
    if args.tick:
        args.apply = True                                     # a tick writes like --apply
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        horizon = datetime.combine(settings.activity_end + timedelta(days=1), dtime(0), timezone.utc)
        target = min(now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0), horizon)
    setup_logging(settings.log_dir, f"activity_{settings.profile}_{mode}", args.log_level)
    started = time.perf_counter()
    try:
        ctx = EngineContext.load(settings)
    except (SettingsError, EngineError, ValueError) as exc:
        log.error("Configuration error: %s", exc)
        return 3
    load_env_file(args.env_file)
    try:
        conn = connect()
    except psycopg.OperationalError as exc:
        log.error("could not connect: %s", str(exc).strip().splitlines()[0] if str(exc).strip() else exc)
        return 3
    with conn:
        ident = database_identity(conn)
        log.info("NordicCart activity engine | mode=%s | replay=%s | profile=%s | target %s | cluster %s | origin %s | "
                 "config %s...", mode, args.replay, settings.profile, describe_connection_target(), ident["cluster"],
                 ctx.origin.isoformat(), ctx.config_fingerprint[:16])
        if args.apply:
            try:
                require_sandbox(conn)
            except SandboxGuardError as exc:
                log.error("REFUSED (sandbox guard): %s. Nothing was written.", exc)
                return 3
        if conn.execute("SELECT NOT EXISTS (SELECT 1 FROM meta.schema_migrations WHERE version = '009')").fetchone()[0]:
            log.error("the engine needs V009 (run ledger and checkpoints)")
            return 3
        key = try_session_lock(conn, ACTIVITY_LOCK)
        if key is None:
            log.error("another activity engine or a pilot load holds the lock %s", ACTIVITY_LOCK)
            return 4
        try:
            n = 0
            while True:
                end_at = None
                if args.tick:
                    end_at = _tick_end(conn, ctx, target)
                    if end_at is None:
                        reached = "generation horizon (calendar.simulation_end) reached" if target == horizon else "up to date"
                        log.info("tick: nothing to do up to %s (%s); %d window(s) run", target.isoformat(), reached, n)
                        break
                if not args.apply:
                    with conn.transaction():
                        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                        plan = plan_window(conn, ctx, replay=args.replay)
                else:
                    plan = plan_window(conn, ctx, replay=args.replay, end_at=end_at)
                counts = {}
                for u in plan.units:
                    counts[u.kind] = counts.get(u.kind, 0) + 1
                log.info("window %s to %s%s: %d units (%s), %d done; plan %s...; start state %s...; %s replay from %s "
                         "in %.3f s; lost demand so far: %d orders, %d units", plan.start.isoformat(), plan.end.isoformat(),
                         " (resume)" if plan.resume else "", len(plan.units),
                         ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), plan.done, plan.fingerprint[:16],
                         plan.start_hash[:16], plan.replay, plan.sim.start.date().isoformat(), plan.seconds,
                         plan.sim.lost_orders, plan.sim.lost_units)
                if not args.apply:
                    log.info("plan finished: nothing was written.")
                    return 0
                if n == 0 and not args.yes:
                    database = conn.execute("SELECT current_database()").fetchone()[0]
                    if input(f"Type the database name ({database}) to run {args.windows} window(s): ").strip() != database:
                        log.error("Confirmation declined; nothing was written.")
                        return 3
                run_window(conn, ctx, plan)
                n += 1
                if is_month_start(plan.end):                            # the window completed a month
                    month = verify_month(conn, ctx, (plan.end - DAY).date())
                    log.info("month-end verification %s: %d of %d checks passed | %s", month.month,
                             len(month.checks) - len(month.problems), len(month.checks),
                             ", ".join(f"{k} {v}" for k, v in month.summary.items()))
                    if month.problems:
                        raise MonthVerificationError(f"month {month.month}: " + "; ".join(month.problems))
                if not args.tick and n >= args.windows:
                    break
        except MonthVerificationError as exc:
            log.error("MONTH VERIFICATION FAILED: %s", exc)
            return 1
        except SandboxGuardError as exc:
            log.error("REFUSED (sandbox guard): %s. Nothing was written.", exc)
            return 3
        except DriftError as exc:
            log.error("BLOCKED (drift): %s. Nothing was written for this window.", exc)
            return 2
        except WindowVerificationError as exc:
            log.error("VERIFICATION FAILED: %s", exc)
            return 1
        except (EngineError, psycopg.Error, ValueError) as exc:
            log.error("FAILED: %s", exc)
            return 1
        finally:
            release_session_lock(conn, key)
    log.info("finished in %.1f s", time.perf_counter() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
