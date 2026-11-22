"""NordicCart hourly pipeline (stage S10): generation tick, then ETL raw -> cleaned -> analytics.

    python python/run_pipeline.py --env-file <writer env> --etl-env-file <extractor env> [--now <ISO UTC>]

1. lock        one pipeline at a time (an OS lock on logs/pipeline/pipeline.lock, released when the process ends)
2. generation  python/run_activity.py --tick --yes --profile full (the S8.2/S9 engine): every window from the last
               verified end to floor(now, hour); an hour already verified writes nothing. Retried (3 attempts, 60 s
               apart) only for exit 4 (lock busy) and exit 1 (a failed window, which the engine resumes); drift,
               guard or configuration errors (exit 2, 3) stop the pipeline. --skip-generation omits the step.
3. ETL         nordiccart.etl.run_etl as the extractor (snapshot checkpoint, idempotent cleaned layer, DQ checks,
               analytics only if every check passes); connection errors retried (3 attempts)
4. evidence    logs/pipeline/pipeline_<YYYY-MM-DD>.log and one JSON line per run in logs/pipeline/runs.jsonl
Exit codes: 0 success, 1 a step failed, 4 another pipeline is running.
Env files: KEY=VALUE lines (PGHOST, PGPORT, PGDATABASE, PGUSER, optional PGPASSWORD); the generation file is passed to
the engine CLI, which applies its write guard (sandbox, or the explicitly authorized real target).
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
import psycopg  # noqa: E402

from nordiccart.etl import run_etl  # noqa: E402

LOGS = ROOT / "logs" / "pipeline"
SOURCE_TABLES = ("orders", "order_items", "payment_attempts", "inventory_movements", "inventory_balances", "customers",
                 "products")
log = logging.getLogger("nordiccart.pipeline")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", type=Path, required=True, help="writer connection (loader or scheduler) for the tick")
    p.add_argument("--etl-env-file", type=Path, required=True, help="extractor connection for the ETL")
    p.add_argument("--out", type=Path, default=ROOT / "output" / "etl")
    p.add_argument("--now", default=None, help="clock for the tick (ISO UTC; default: system clock)")
    p.add_argument("--skip-generation", action="store_true")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--retry-wait", type=int, default=60, help="seconds between attempts")
    return p.parse_args(argv)


def read_env(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            out[key.strip()] = value.strip().strip("'\"")
    return out


def connect(env: dict, read_only: bool = False) -> psycopg.Connection:
    kw = {"host": env.get("PGHOST"), "port": env.get("PGPORT", "5432"), "dbname": env.get("PGDATABASE"),
          "user": env.get("PGUSER"), "password": env.get("PGPASSWORD"), "connect_timeout": 10}
    if read_only:
        kw["options"] = "-c default_transaction_read_only=on"
    return psycopg.connect(**{k: v for k, v in kw.items() if v})


def acquire_lock():
    """An OS byte-range lock on logs/pipeline/pipeline.lock, held while the process lives (released on exit or crash)."""
    import msvcrt
    fh = open(LOGS / "pipeline.lock", "a+")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    return fh


def generation(args) -> int:
    cmd = [sys.executable, "-B", str(ROOT / "python" / "run_activity.py"), "--tick", "--yes", "--profile", "full",
           "--env-file", str(args.env_file)] + (["--now", args.now] if args.now else [])
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    for attempt in range(1, args.retries + 1):
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
        for line in proc.stderr.splitlines():
            if any(s in line for s in ("tick:", "verified:", "month-end", "ERROR", "REFUSED", "BLOCKED", "FAILED")):
                log.info("engine | %s", line.split("  ", 1)[-1].strip())
        log.info("generation attempt %d: exit %d", attempt, proc.returncode)
        if proc.returncode == 0 or proc.returncode not in (1, 4) or attempt == args.retries:
            return proc.returncode
        time.sleep(args.retry_wait)
    return 1


def main(argv=None) -> int:
    args = parse_args(argv)
    LOGS.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        handlers=[logging.FileHandler(LOGS / f"pipeline_{started:%Y-%m-%d}.log", encoding="utf-8"),
                                  logging.StreamHandler()])
    record = {"started": started.isoformat(), "now": args.now, "generation_exit": None, "etl": None, "exit": None}
    lock = acquire_lock()
    if lock is None:
        log.error("another pipeline run holds %s", LOGS / "pipeline.lock")
        return 4
    try:
        log.info("pipeline start | generation %s | ETL output %s", "skipped" if args.skip_generation else "tick", args.out)
        if not args.skip_generation:
            record["generation_exit"] = generation(args)
            if record["generation_exit"] != 0:
                log.error("generation failed (exit %d): ETL not run", record["generation_exit"])
                record["exit"] = 1
                return 1
        writer, extractor = read_env(args.env_file), read_env(args.etl_env_file)
        for attempt in range(1, args.retries + 1):
            try:
                with connect(writer, read_only=True) as c:     # completeness reference (read-only)
                    counts = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in SOURCE_TABLES}
                with connect(extractor) as c:
                    result = run_etl(c, args.out, counts)
                break
            except psycopg.OperationalError as exc:
                log.warning("ETL attempt %d: connection problem: %s", attempt, str(exc).strip().splitlines()[0])
                if attempt == args.retries:
                    raise
                time.sleep(args.retry_wait)
        record["etl"] = {"raw_file": result.raw_file, "extracted": result.extracted, "applied": result.applied,
                         "skipped": result.skipped, "dq_failed": [c for c, n in result.dq if n],
                         "published": result.published}
        record["exit"] = 0 if result.published else 1
        log.info("pipeline end | exit %d | %.1f s", record["exit"], (datetime.now(timezone.utc) - started).total_seconds())
        return record["exit"]
    except Exception as exc:                                   # logged and reported; the next hourly run retries
        log.exception("pipeline failed: %s", exc)
        record["exit"] = 1
        return 1
    finally:
        record["finished"] = datetime.now(timezone.utc).isoformat()
        with (LOGS / "runs.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        lock.close()                                           # releases the OS lock


if __name__ == "__main__":
    sys.exit(main())
