"""S9: real historical catch-up 2024-12-03 to 2026-10-31 (the S8.2 rehearsal procedure on the real database).

Run ONLY after explicit authorization, in a local terminal (repository root):
    .\\.venv\\Scripts\\python.exe -B python\\s9_catch_up.py

1. preflight (read-only, as nordiccart_loader via config/.env): the real target, cluster 7663257437785384692; either the
   verified S7 baseline (0 runs) or a resumable S9 ledger (every run verified); no other session
2. the postgres password is asked once (getpass: not shown, not stored); it is used only for pg_dump recovery points and
   the final read-only verification scripts
3. per month: python/run_activity.py --apply --windows <days left in the month> --yes --profile full as
   nordiccart_loader with NORDICCART_S9_REAL_AUTHORIZED=<cluster>/<database> (the engine's month-end verification
   runs after the month's last window and stops on any failure); then a pg_dump -Fc recovery point to
   database/backups/s9/, its SHA-256 recorded on the month's last run row; a progress line in logs/s9/progress.jsonl
4. stops at the first non-zero exit (nothing is retried automatically). Restartable: the next window comes from the ledger
5. after the last month: scripts 01-18 read-only as postgres, saved to logs/s9/scripts_final.txt
"""
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
import psycopg  # noqa: E402

from nordiccart.settings import load_env_file  # noqa: E402

BIN = Path(r"C:\Program Files\PostgreSQL\18\bin")
REAL_CLUSTER, REAL_DB, LAST_DAY = "7663257437785384692", "nordiccart_oltp", date(2026, 10, 31)
OUT, REC = ROOT / "logs" / "s9", ROOT / "database" / "backups" / "s9"
OUT.mkdir(parents=True, exist_ok=True)
REC.mkdir(parents=True, exist_ok=True)
BASELINE = {"orders": 20, "movements": 6134, "change_log": 43310, "products": 500, "customers": 20000}

for k in [k for k in os.environ if k.startswith("PG") or k.startswith("NORDICCART_S6")]:
    del os.environ[k]
load_env_file()                                            # nordiccart_loader, config/.env (values never printed)
assert os.environ.get("PGDATABASE") == REAL_DB and os.environ.get("PGPORT", "5432") == "5432"
LOADER_ENV = dict(os.environ)


def say(text: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {text}", flush=True)
    with (OUT / "driver.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")


def loader(read_only: bool = True):
    return psycopg.connect(options="-c default_transaction_read_only=on" if read_only else None, autocommit=not read_only)


# ----------------------------------------------------------------------------- 1. preflight
with loader() as c:
    cluster, port, db = c.execute("SELECT system_identifier::text, inet_server_port(), current_database() "
                                  "FROM pg_control_system()").fetchone()
    assert (cluster, port, db) == (REAL_CLUSTER, 5432, REAL_DB), (cluster, port, db)
    runs, bad = c.execute("SELECT count(*), count(*) FILTER (WHERE status <> 'verified') FROM meta.activity_runs").fetchone()
    others = c.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                       "AND pid <> pg_backend_pid()").fetchone()[0]
    counts = dict(zip(BASELINE, c.execute("SELECT (SELECT count(*) FROM orders), (SELECT count(*) FROM inventory_movements), "
                                          "(SELECT count(*) FROM change_log), (SELECT count(*) FROM products), "
                                          "(SELECT count(*) FROM customers)").fetchone()))
if runs == 0 and counts != BASELINE:
    sys.exit(f"STOP: no runs yet but the database is not the verified S7 baseline: {counts}")
if bad:
    sys.exit(f"STOP: {bad} run(s) not verified: resume or inspect them with python/run_activity.py first")
if others:
    sys.exit(f"STOP: {others} other session(s) on {REAL_DB}")
say(f"preflight OK: cluster {cluster}, {db}:{port}, {runs} verified runs, counts {counts}")

password = getpass.getpass("postgres password (for recovery points and the final checks; not shown, not stored): ")
ADMIN_ENV = {**{k: v for k, v in LOADER_ENV.items() if not k.startswith("PG")}, "PGPASSWORD": password}
probe = subprocess.run([str(BIN / "psql.exe"), "-h", "localhost", "-p", "5432", "-U", "postgres", "-d", REAL_DB, "-Atc",
                        "SELECT 1"], env=ADMIN_ENV, capture_output=True, text=True)
if probe.returncode != 0:
    sys.exit("STOP: the postgres password was not accepted (nothing was written)")

ENGINE_ENV = {**LOADER_ENV, "NORDICCART_S9_REAL_AUTHORIZED": f"{REAL_CLUSTER}/{REAL_DB}"}


def next_start() -> date:
    with loader() as c:
        row = c.execute("SELECT max(window_end), bool_and(status = 'verified') FROM meta.activity_runs").fetchone()
    if row[0] is None:
        return date(2024, 12, 3)
    if not row[1]:
        sys.exit("STOP: an open or failed run exists (see logs/s9); nothing is retried automatically")
    return row[0].astimezone(timezone.utc).date()


def stats() -> dict:
    with loader() as c:
        r = c.execute("SELECT (SELECT count(*) FROM orders), (SELECT count(*) FROM inventory_movements), "
                      "(SELECT count(*) FROM meta.activity_runs), pg_database_size(current_database())").fetchone()
    return dict(zip(("orders", "movements", "runs", "db_bytes"), r))


# ----------------------------------------------------------------------------- 3. months
while True:
    start = next_start()
    if start > LAST_DAY:
        say(f"catch-up complete: next window {start}")
        break
    first_next = (start.replace(day=1) + timedelta(days=32)).replace(day=1)
    windows = (min(first_next, LAST_DAY + timedelta(days=1)) - start).days
    month = f"{start:%Y-%m}"
    say(f"{month}: {windows} window(s) from {start}")
    t0 = time.perf_counter()
    proc = subprocess.run([sys.executable, "-B", str(ROOT / "python" / "run_activity.py"), "--apply", "--windows", str(windows),
                           "--yes", "--profile", "full"], cwd=ROOT, env=ENGINE_ENV, capture_output=True, text=True)
    engine_s = time.perf_counter() - t0
    (OUT / f"engine_{month}.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    text = proc.stderr
    month_line = re.findall(r"month-end verification (\S+): (\d+) of (\d+) checks passed \| (.*)", text)
    lost = re.findall(r"lost demand so far: (\d+) orders, (\d+) units", text)
    units = sum(int(x) for x in re.findall(r"run \d+ verified: (\d+) units", text))
    rec = {"month": month, "start": start.isoformat(), "windows": windows, "exit": proc.returncode, "engine_s": round(engine_s, 1),
           "units": units, "month_check": month_line[-1][:3] if month_line else None,
           "lost_demand_cumulative": [int(x) for x in lost[-1]] if lost else None}
    if proc.returncode != 0:
        rec["error"] = [l for l in text.splitlines() if "ERROR" in l or "REFUSED" in l or "BLOCKED" in l][-3:]
        with (OUT / "progress.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        say(f"STOPPED at {month} (exit {proc.returncode}): {rec['error']}")
        sys.exit(1)
    if start + timedelta(days=windows) == first_next:      # recovery point after a completed month (no writer runs now)
        name = f"nordiccart_oltp_s9_{start:%Y%m}_activity_recovery_{datetime.now():%Y%m%d_%H%M%S}.dump"
        path = REC / name
        t1 = time.perf_counter()
        dump = subprocess.run([str(BIN / "pg_dump.exe"), "-h", "localhost", "-p", "5432", "-U", "postgres", "-d", REAL_DB,
                               "-Fc", "-f", str(path)], env=ADMIN_ENV, capture_output=True, text=True)
        rec["dump_s"] = round(time.perf_counter() - t1, 1)
        if dump.returncode != 0:
            rec["error"] = dump.stderr[-500:]
            with (OUT / "progress.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            say(f"STOPPED: recovery point for {month} failed")
            sys.exit(1)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        with loader(read_only=False) as c:                 # once, on the month's last (verified) run
            n = c.execute("UPDATE meta.activity_runs SET recovery_point = %s, recovery_point_sha256 = %s "
                          "WHERE window_end = %s AND status = 'verified' AND recovery_point IS NULL",
                          (name, sha, datetime.combine(first_next, datetime.min.time(), timezone.utc))).rowcount
        rec.update(recovery_point=name, sha256=sha, dump_bytes=path.stat().st_size, recorded=n == 1)
    rec.update(stats())
    with (OUT / "progress.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    say(f"{month}: exit 0, {units} units, month check {rec['month_check']}, orders {rec['orders']}, "
        f"engine {engine_s:.0f} s" + (f", recovery point {rec['dump_bytes']:,} bytes" if "dump_bytes" in rec else ""))

# ----------------------------------------------------------------------------- 5. final scripts (read-only, postgres)
total, lines = Counter(), []
with psycopg.connect(host="localhost", port=5432, dbname=REAL_DB, user="postgres", password=password,
                     options="-c default_transaction_read_only=on", autocommit=True) as conn:
    for path in sorted((ROOT / "sql" / "verification").glob("[01][0-9]_*.sql")):
        c, t0 = Counter(), time.perf_counter()
        with conn.transaction():
            cur = conn.cursor()
            cur.execute(path.read_text(encoding="utf-8-sig"))
            while True:
                if cur.description:
                    names = [d.name for d in cur.description]
                    rows = cur.fetchall()
                    if "status" in names:
                        for r in rows:
                            c[r[names.index("status")]] += 1
                            if r[names.index("status")] == "FAIL":
                                lines.append(f"    FAIL {path.name} {r[0]} {r[2] if len(r) > 2 else ''}")
                if not cur.nextset():
                    break
        lines.append(f"  {path.name:<46} {sum(c.values()):>3} rows {c['PASS']:>3} PASS {c['INFO']:>3} INFO {c['FAIL']:>2} FAIL"
                     f" {c['WARN']:>2} WARN  {time.perf_counter() - t0:.1f} s")
        total.update(c)
lines.append(f"  total {sum(total.values())} rows: {total['PASS']} PASS, {total['INFO']} INFO, {total['FAIL']} FAIL, "
             f"{total['WARN']} WARN")
(OUT / "scripts_final.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
say("final scripts: " + lines[-1].strip())
del password
say("S9 driver finished.")
