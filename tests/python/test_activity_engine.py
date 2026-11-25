"""S6 milestone 1: the activity engine at pilot scale (design 7.1 to 7.3, 11.2), sandbox only.

In memory (no database writes): determinism and window composition of the plan.
Sandbox (NORDICCART_S6_SANDBOX=1, PostgreSQL on 127.0.0.1:55432, base database
NORDICCART_S6_BASE, default nordiccart_s6_base = the pre-S5 backup restored + E1, i.e. the
real database state after S5): every test works on its own clone and drops it.
    execution     7 UTC day windows (2024-12-03 to 12-09) through the CLI as nordiccart_loader,
                  at representative pilot volume and at a stress volume (15 x the order rate)
    pilot intact  pilot records byte-identical, both pilot --plan checks 0 conflicts,
                  verification scripts 01 to 18 0 FAIL (as nordiccart_analyst)
    idempotency   --plan writes nothing; a finished window is never run again; units already
                  done are never repeated (prefix rule)
    recovery      the process is killed inside a unit, between units, before verification and
                  inside the checkpoint transaction; the rerun ends identical to the reference
    drift         a changed reorder point and a stray future record block the next window
    composition   one 2-day window = two 1-day windows (database state and business records)
    lock          a second engine exits 4 while the activity lock is held
Milestone 2 (incremental replay and execution safety):
    incremental   60 windows over the December-to-January boundary: every window after the first
                  replays incrementally from the previous checkpoint and equals the full replay
                  (plan fingerprints, state hashes, checkpoints); a run forced to --replay full ends
                  with an identical run ledger and identical business records; January numbering
    carry         lossless round trip; a carried open order changed in the database blocks the window
    pilot lock    both pilot loaders refuse to write while the engine's lock is held, and hold it
                  themselves while they write (the engine exits 4 meanwhile)
    guard         --apply and run_window refuse without the sandbox cluster identity, a wrong one, a
                  protected cluster, or the config/.env target
Milestone 3 (monthly business rules and verification):
    rules         P1 markdowns and R-cal in memory; four months (December to March) incremental = full, every
                  month-end verification passed, P1 rows and R-cal revisions as planned, scripts 01 to 18 0 FAIL
    recovery      killed inside an R-cal unit and inside a P1 unit: identical to an uninterrupted run
    month gate    a broken December (payment amount) stops January from starting
    carry         altered future actions (re-derived), a changed carry (SHA-256), a missing step: blocked
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from nordiccart.activity.engine import DAY, EngineContext, decode, plan_fingerprint, simulate
from nordiccart.activity.run import ACTIVITY_LOCK, plan_window, run_window
from nordiccart.activity.state import state_as_of, state_hash
from nordiccart.settings import load_settings

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "sql" / "verification"
BASE = os.getenv("NORDICCART_S6_BASE", "nordiccart_s6_base")
KW = dict(host="127.0.0.1", hostaddr="127.0.0.1", port=55432, connect_timeout=5, autocommit=True)
WINDOWS = 7

pytestmark = pytest.mark.skipif(os.getenv("NORDICCART_S6_SANDBOX") != "1",
                                reason="requires the S6 sandbox on 127.0.0.1:55432 (NORDICCART_S6_SANDBOX=1)")


@pytest.fixture(scope="session", autouse=True)
def sandbox_cluster():
    """The sandbox guard (milestone 2) needs the sandbox cluster's system identifier; read it from the sandbox."""
    if os.getenv("NORDICCART_S6_SANDBOX") != "1":
        yield None
        return
    with psycopg.connect(**KW, dbname="postgres", user="postgres") as c:
        assert c.execute("SELECT inet_server_port()").fetchone() == (55432,)
        cluster = c.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
    previous = os.environ.get("NORDICCART_S6_SANDBOX_CLUSTER")
    os.environ["NORDICCART_S6_SANDBOX_CLUSTER"] = cluster
    yield cluster
    if previous is None:
        os.environ.pop("NORDICCART_S6_SANDBOX_CLUSTER", None)
    else:
        os.environ["NORDICCART_S6_SANDBOX_CLUSTER"] = previous


# ----------------------------------------------------------------------------- configs and contexts
@pytest.fixture(scope="session")
def configs(tmp_path_factory) -> dict[str, Path]:
    """representative = config/generator.toml; stress = the same with orders_per_customer_year 36."""
    text = (ROOT / "config" / "generator.toml").read_text(encoding="utf-8")
    stress = re.sub(r"orders_per_customer_year = 2\.4\b", "orders_per_customer_year = 36.0", text)
    assert stress != text
    path = tmp_path_factory.mktemp("s6") / "generator_stress.toml"
    path.write_text(stress, encoding="utf-8")
    return {"representative": ROOT / "config" / "generator.toml", "stress": path}


@pytest.fixture(scope="session")
def contexts(configs) -> dict[str, EngineContext]:
    return {name: EngineContext.load(load_settings(path, "pilot")) for name, path in configs.items()}


@pytest.fixture(scope="session")
def origin_state(contexts):
    # read as the sandbox superuser: the V008 rollback test (R008 DROP OWNED BY) removes CONNECT on the other
    # databases of the cluster, the base included, when the whole regression runs first (known sandbox side effect)
    with psycopg.connect(**KW, dbname=BASE, user="postgres", options="-c default_transaction_read_only=on") as conn:
        return state_as_of(conn, contexts["stress"].origin)


# ----------------------------------------------------------------------------- databases
def _admin():
    conn = psycopg.connect(**KW, dbname="postgres", user="postgres")
    assert conn.execute("SELECT inet_server_port()").fetchone() == (55432,), "sandbox only"
    return conn


def clone(label: str) -> str:
    name = f"nordiccart_s6_{label}_{uuid4().hex[:8]}"
    with _admin() as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(name), sql.Identifier(BASE)))
        admin.execute(sql.SQL("ALTER DATABASE {} SET timezone = 'UTC'").format(sql.Identifier(name)))
    with psycopg.connect(**KW, dbname=name, user="postgres") as setup:
        setup.execute((ROOT / "database/security/V008_reapply_database_privileges.sql").read_text(encoding="utf-8-sig"))
    return name


def drop(name: str) -> None:
    with _admin() as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def db(request):
    name = clone(request.node.name[:20].replace("[", "_").replace("]", "").replace("-", "_").lower())
    yield name
    drop(name)


def engine(name: str, config: Path, *args: str, fault: str | None = None, env_overrides: dict | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG") and k != "NORDICCART_ENGINE_FAULT"}
    env.update(PGHOST="127.0.0.1", PGHOSTADDR="127.0.0.1", PGPORT="55432", PGDATABASE=name, PGUSER="nordiccart_loader",
               NORDICCART_S6_SANDBOX="1")
    if fault:
        env["NORDICCART_ENGINE_FAULT"] = fault
    for k, v in (env_overrides or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run([sys.executable, "-B", str(ROOT / "python" / "run_activity.py"), *args, "--config", str(config)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)


def loader(name: str):
    return psycopg.connect(**KW, dbname=name, user="nordiccart_loader")


def reader(name: str):
    return psycopg.connect(**KW, dbname=name, user="nordiccart_analyst", options="-c default_transaction_read_only=on")


# ----------------------------------------------------------------------------- business digests (no surrogate ids)
BUSINESS = {
    "orders": """SELECT o.order_number, c.customer_number, o.channel, sl.location_code, fl.location_code, o.country_code,
                        o.currency_code, o.status, o.cancellation_reason, o.items_total, o.shipping_fee, o.shipping_vat_amount,
                        o.order_total, o.shipping_address_line1, o.shipping_postal_code, o.shipping_city, o.placed_at,
                        o.confirmed_at, o.fulfilled_at, o.cancelled_at
                   FROM orders o JOIN customers c ON c.customer_id = o.customer_id
                   LEFT JOIN stores s ON s.store_id = o.store_id LEFT JOIN stock_locations sl ON sl.stock_location_id = s.stock_location_id
                   JOIN stock_locations fl ON fl.stock_location_id = o.fulfilment_location_id""",
    "order_items": """SELECT o.order_number, i.line_number, p.sku, i.quantity, i.unit_price, i.unit_cost, i.discount_percent,
                             i.discount_amount, i.line_total, i.vat_rate, i.vat_amount
                        FROM order_items i JOIN orders o ON o.order_id = i.order_id JOIN products p ON p.product_id = i.product_id""",
    "payment_attempts": """SELECT o.order_number, a.attempt_number, a.payment_method, a.payment_provider, a.amount, a.status,
                                  a.failure_reason, a.provider_reference, a.attempted_at, a.authorized_at, a.captured_at, a.failed_at
                             FROM payment_attempts a JOIN orders o ON o.order_id = a.order_id""",
    "reservations": """SELECT o.order_number, i.line_number, l.location_code, p.sku, r.quantity, r.status, r.reserved_at,
                              r.expires_at, r.closed_at
                         FROM inventory_reservations r JOIN order_items i ON i.order_item_id = r.order_item_id
                         JOIN orders o ON o.order_id = i.order_id JOIN stock_locations l ON l.stock_location_id = r.stock_location_id
                         JOIN products p ON p.product_id = r.product_id""",
    "movements": """SELECT l.location_code, p.sku, m.movement_type, m.quantity_change, m.occurred_at, m.external_reference,
                           o.order_number, i.line_number, rl.location_code AS related_location
                      FROM inventory_movements m JOIN stock_locations l ON l.stock_location_id = m.stock_location_id
                      JOIN products p ON p.product_id = m.product_id
                      LEFT JOIN order_items i ON i.order_item_id = m.order_item_id LEFT JOIN orders o ON o.order_id = i.order_id
                      LEFT JOIN inventory_movements rm ON rm.movement_id = m.related_movement_id
                      LEFT JOIN stock_locations rl ON rl.stock_location_id = rm.stock_location_id""",
    "balances": """SELECT l.location_code, p.sku, b.quantity_on_hand, b.quantity_reserved, b.reorder_point
                     FROM inventory_balances b JOIN stock_locations l ON l.stock_location_id = b.stock_location_id
                     JOIN products p ON p.product_id = b.product_id""",
    "purchase_orders": """SELECT po.purchase_order_number, l.location_code, po.ordered_at, po.status
                            FROM purchase_orders po JOIN stock_locations l ON l.stock_location_id = po.stock_location_id""",
    "purchase_order_lines": """SELECT po.purchase_order_number, pl.line_number, p.sku, pl.order_reason, pl.quantity_ordered,
                                      pl.quantity_received, pl.status
                                 FROM purchase_order_lines pl JOIN purchase_orders po ON po.purchase_order_id = pl.purchase_order_id
                                 JOIN products p ON p.product_id = pl.product_id""",
    "reorder_point_revisions": """SELECT l.location_code, p.sku, v.revision_kind, v.effective_at, v.old_reorder_point,
                                         v.new_reorder_point, v.document_reference
                                    FROM reorder_point_revisions v JOIN stock_locations l ON l.stock_location_id = v.stock_location_id
                                    JOIN products p ON p.product_id = v.product_id""",
    "exchange_rates": "SELECT rate_date, base_currency_code, quote_currency_code, rate, source FROM exchange_rates",
    "change_log_rows": "SELECT count(*) FROM change_log",
}
PILOT_FILTER = {     # the pilot's own records: orders 1 to 20 and everything dated before the engine origin
    "orders": "WHERE substr(o.order_number, 9)::int <= 20",
    "order_items": "WHERE substr(o.order_number, 9)::int <= 20",
    "payment_attempts": "WHERE substr(o.order_number, 9)::int <= 20",
    "reservations": "WHERE substr(o.order_number, 9)::int <= 20",
    "movements": "WHERE m.occurred_at < '2024-12-03 00:00:00+00'",
    "purchase_orders": "WHERE po.ordered_at < '2024-12-03 00:00:00+00'",
    "purchase_order_lines": "WHERE po.ordered_at < '2024-12-03 00:00:00+00'",
    "reorder_point_revisions": "WHERE v.effective_at < '2024-12-03 00:00:00+00'",     # R0; R-cal (S6 M3) comes later
    "exchange_rates": "WHERE rate_date < '2024-12-03'",
}


def digest(name: str, pilot_only: bool = False) -> dict:
    out = {}
    with reader(name) as conn, conn.transaction():
        conn.execute("SET LOCAL TimeZone = 'UTC'")
        for key, query in BUSINESS.items():
            if pilot_only:
                if key not in PILOT_FILTER:
                    continue
                query = f"{query} {PILOT_FILTER[key]}"
            out[key] = conn.execute(f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text COLLATE \"C\"), '')) "
                                    f"FROM ({query}) t").fetchone()
    return out


def ledger(name: str) -> list[tuple]:
    with reader(name) as conn:
        return conn.execute("""SELECT r.window_start, r.window_end, r.status::text, r.plan_fingerprint, r.config_fingerprint,
                                      r.start_state_hash, r.end_state_hash, r.planned_units, r.completed_units, r.unit_counts::text,
                                      c.state_as_of, c.state_hash, md5(c.state_snapshot::text)
                                 FROM meta.activity_runs r LEFT JOIN meta.activity_checkpoints c USING (activity_run_id)
                                ORDER BY r.window_start""").fetchall()


def scripts(name: str) -> dict[str, tuple[int, int, int, int]]:
    out = {}
    with reader(name) as conn:
        for path in sorted(VERIFY.glob("[0-9][0-9]_*.sql")):
            counts = {"PASS": 0, "INFO": 0, "FAIL": 0, "WARN": 0}
            with conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))
                while True:
                    if cur.description is not None and "status" in [d.name for d in cur.description]:
                        i = [d.name for d in cur.description].index("status")
                        for row in cur.fetchall():
                            counts[row[i]] = counts.get(row[i], 0) + 1
                    if not cur.nextset():
                        break
            out[path.name[:2]] = (counts["PASS"], counts["INFO"], counts["FAIL"], counts["WARN"])
    return out


# ----------------------------------------------------------------------------- reference run (stress, 7 windows)
@pytest.fixture(scope="module")
def reference(configs):
    name = clone("reference")
    proc = engine(name, configs["stress"], "--apply", "--windows", str(WINDOWS), "--yes")
    assert proc.returncode == 0, proc.stderr[-3000:]
    yield {"name": name, "ledger": ledger(name), "digest": digest(name), "log": proc.stderr}
    drop(name)


# ============================================================================= in memory
def test_plan_is_deterministic(contexts, origin_state):
    ctx = contexts["stress"]
    end = ctx.origin + WINDOWS * DAY
    a, b = simulate(ctx, origin_state, end), simulate(ctx, origin_state, end)
    assert plan_fingerprint(a.units) == plan_fingerprint(b.units)
    assert len(a.units) > 300


def test_transfers_numbered_before_dispatch_across_an_hourly_window_end(contexts, origin_state):
    """S10 regression: the store review numbers its transfers at 05:30 local, dispatch follows from 06:00 local.

    At every hour the snapshot's transfer sequences are the highest number already dispatched (what state._SEQUENCES
    reads from the database), also at an hour that ends between a review and its dispatches; a replay split there
    and continued from that snapshot and its carry equals the uninterrupted replay (no transfer number reused).
    """
    from datetime import timedelta

    ctx = contexts["stress"]
    end = ctx.origin + 3 * DAY
    hours = [ctx.origin + timedelta(hours=h) for h in range(1, 72)]
    full = simulate(ctx, origin_state, end, hours + [end])
    transfers = [u.payload for u in full.units if u.kind == "T1"]
    assert transfers, "the stress volume replenishes stores every working day"
    splits = []
    before = {k: v for k, v in origin_state["sequences"].items() if k.startswith("TR-")}   # dispatched before the origin
    for t in hours:
        dispatched = dict(before)
        for x in transfers:
            if x.dispatched_at < t:
                dispatched[x.reference[:10]] = max(dispatched.get(x.reference[:10], 0), int(x.reference[11:17]))
        assert {k: v for k, v in full.snapshots[t]["sequences"].items() if k.startswith("TR-")} == dispatched, t
        if any(x.requested_at < t <= x.dispatched_at for x in transfers):
            splits.append(t)
    assert splits, "some hour ends between a store review and its dispatches"
    split = splits[0]
    first = simulate(ctx, origin_state, split, [split])
    assert any(decode(e)[3] == "dispatch" for e in first.carry["queue"]), "the carry holds numbered, undispatched transfers"
    rest = simulate(ctx, first.snapshots[split], end, [end], carry=first.carry)
    assert plan_fingerprint(rest.units) == plan_fingerprint(full.window(split, end))
    assert state_hash(rest.snapshots[end]) == state_hash(full.snapshots[end])
    references = [u.payload.reference for u in first.units + rest.units if u.kind == "T1"]
    assert len(references) == len(set(references)) == len(transfers)


@pytest.mark.parametrize("volume", ["representative", "stress"])
def test_window_composition_in_memory(contexts, origin_state, volume):
    """plan(day 1) + plan(day 2) = plan(days 1 to 2), and the state at a boundary does not depend on the horizon."""
    ctx = contexts[volume]
    days = [ctx.origin + i * DAY for i in range(WINDOWS + 1)]
    full = simulate(ctx, origin_state, days[-1], days)
    joined = []
    for i in range(WINDOWS):
        part = simulate(ctx, origin_state, days[i + 1], [days[i], days[i + 1]])
        assert plan_fingerprint(part.window(days[i], days[i + 1])) == plan_fingerprint(full.window(days[i], days[i + 1]))
        assert state_hash(part.snapshots[days[i + 1]]) == state_hash(full.snapshots[days[i + 1]])
        joined += part.window(days[i], days[i + 1])
    assert plan_fingerprint(joined) == plan_fingerprint(full.window(days[0], days[-1]))
    two = simulate(ctx, origin_state, days[2])
    assert plan_fingerprint(two.window(days[0], days[2])) == plan_fingerprint(full.window(days[0], days[1]) +
                                                                               full.window(days[1], days[2]))


# ============================================================================= execution and pilot regression
def _pilot_plan(name: str, runner: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env_file = tmp_path / f"{name}.env"
    env_file.write_text(f"PGHOST=127.0.0.1\nPGPORT=55432\nPGDATABASE={name}\nPGUSER=nordiccart_loader\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    return subprocess.run([sys.executable, "-B", str(ROOT / "python" / runner), "--plan", "--env-file", str(env_file)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)


DECEMBER = 29                    # UTC day windows 2024-12-03 to 2024-12-31


@pytest.mark.parametrize("volume", ["representative", "stress"])
def test_december_windows_execute_verify_and_keep_the_pilot(db, configs, contexts, origin_state, volume, tmp_path):
    pilot_before = digest(db, pilot_only=True)
    proc = engine(db, configs[volume], "--apply", "--windows", str(DECEMBER), "--yes")
    assert proc.returncode == 0, proc.stderr[-3000:]
    ctx = contexts[volume]
    end = ctx.origin + DECEMBER * DAY
    sim = simulate(ctx, origin_state, end, [ctx.origin + i * DAY for i in range(DECEMBER + 1)])
    runs = ledger(db)
    assert len(runs) == DECEMBER
    for i, r in enumerate(runs):
        start, stop = ctx.origin + i * DAY, ctx.origin + (i + 1) * DAY
        assert (r[0], r[1], r[2]) == (start, stop, "verified")
        assert r[3].strip() == plan_fingerprint(sim.window(start, stop))
        assert r[6] == r[11] == state_hash(sim.snapshots[stop])         # run end hash = checkpoint = replay
        assert r[7] == r[8] == len(sim.window(start, stop))
        assert r[10] == stop
    with reader(db) as conn:
        assert state_hash(state_as_of(conn, end)) == state_hash(sim.snapshots[end])
        assert conn.execute("SELECT count(*) FROM exchange_rates").fetchone()[0] == 96 + 3 * DECEMBER
        statuses = dict(conn.execute("SELECT status::text, count(*) FROM orders GROUP BY 1").fetchall())
    assert digest(db, pilot_only=True) == pilot_before                 # pilot records untouched
    for runner in ("run_transactions.py", "run_replenishment.py"):
        check = _pilot_plan(db, runner, tmp_path)
        assert check.returncode == 0, check.stderr[-2000:]
        assert "conflicts: 0   blocking: 0" in check.stderr
    results = scripts(db)
    assert len(results) == 18
    assert all(fail == 0 for _, _, fail, _ in results.values()), results
    assert results["16"] == (4, 0, 0, 0)
    if volume == "stress":
        kinds = {k for r in runs for k in json.loads(r[9])}             # unit_counts of all runs
        assert {"X1", "place", "reserve", "payment_start", "payment_result", "fulfil", "customer_cancel", "expire",
                "T1", "T2", "PO", "R1"} <= kinds
        assert {"fulfilled", "cancelled", "payment_failed", "confirmed"} <= set(statuses)   # confirmed: ships 2025-01-01


# ============================================================================= idempotency
def test_plan_writes_nothing_and_finished_windows_never_rerun(reference, configs):
    name = reference["name"]
    before = digest(name), ledger(name)
    proc = engine(name, configs["stress"], "--plan")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "window 2024-12-10T00:00:00+00:00 to 2024-12-11T00:00:00+00:00:" in proc.stderr
    assert "(resume)" not in proc.stderr and "nothing was written" in proc.stderr
    assert (digest(name), ledger(name)) == before
    with loader(name) as conn:                                          # the run ledger refuses a repeat of a window
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO meta.activity_runs (run_mode, window_start, window_end, plan_fingerprint, "
                         "config_fingerprint, start_state_hash, planned_units) VALUES ('catch_up', '2024-12-09+00', "
                         "'2024-12-10+00', repeat('a', 64), repeat('a', 64), repeat('a', 64), 1)")


# ============================================================================= interruption and recovery
FAULTS = [("in_unit:30", "running", 29, False), ("after_unit:30", "running", 30, False),
          ("before_verify", "running", 75, False), ("in_checkpoint", "running", 75, False)]


@pytest.mark.parametrize("fault,status,completed,checkpoint", FAULTS, ids=[f[0].split(":")[0] for f in FAULTS])
def test_crash_and_resume_end_identical(db, configs, reference, fault, status, completed, checkpoint):
    cfg = configs["stress"]
    assert engine(db, cfg, "--apply", "--windows", "3", "--yes").returncode == 0
    crashed = engine(db, cfg, "--apply", "--windows", "4", "--yes", fault=fault)     # dies inside window 4 (2024-12-06)
    assert crashed.returncode == 70, crashed.stderr[-2000:]
    runs = ledger(db)
    assert len(runs) == 4 and runs[3][2] == status and runs[3][8] == completed
    assert (runs[3][11] is not None) == checkpoint
    with reader(db) as conn:                                            # nothing of the unit in flight remains
        assert conn.execute("SELECT count(*) FROM meta.activity_checkpoints").fetchone()[0] == 3
    resumed = engine(db, cfg, "--apply", "--windows", "4", "--yes")
    assert resumed.returncode == 0, resumed.stderr[-3000:]
    assert f"run 4 resumed: window 2024-12-06T00:00:00+00:00 to 2024-12-07T00:00:00+00:00, {completed} of 75 units already done" \
        in resumed.stderr
    assert ledger(db) == reference["ledger"]                            # same fingerprints, hashes and snapshots
    assert digest(db) == reference["digest"]                            # same business records and change_log rows


def test_failed_run_is_resumed(db, configs, reference):
    """A unit that fails (not a crash) marks the run failed with its reason; the next start resumes it."""
    cfg = configs["stress"]
    assert engine(db, cfg, "--apply", "--windows", "3", "--yes").returncode == 0
    with psycopg.connect(**KW, dbname=db, user="postgres") as admin:   # movements cannot be written for a moment
        admin.execute("REVOKE INSERT ON inventory_movements FROM nordiccart_writer")
    try:
        failed = engine(db, cfg, "--apply", "--windows", "1", "--yes")
        assert failed.returncode == 1
    finally:
        with psycopg.connect(**KW, dbname=db, user="postgres") as admin:
            admin.execute("GRANT INSERT ON inventory_movements TO nordiccart_writer")
    runs = ledger(db)
    assert runs[3][2] == "failed"
    with reader(db) as conn:
        assert "permission denied" in conn.execute("SELECT failure_reason FROM meta.activity_runs "
                                                   "WHERE activity_run_id = 4").fetchone()[0]
    assert engine(db, cfg, "--apply", "--windows", "4", "--yes").returncode == 0
    assert ledger(db) == reference["ledger"] and digest(db) == reference["digest"]


# ============================================================================= drift
def test_drift_blocks_the_next_window(db, configs):
    cfg = configs["stress"]
    assert engine(db, cfg, "--apply", "--windows", "2", "--yes").returncode == 0
    before = digest(db), ledger(db)
    with psycopg.connect(**KW, dbname=db, user="postgres") as admin:   # a manual change of engine-relevant state
        admin.execute("UPDATE inventory_balances SET reorder_point = reorder_point + 1 WHERE stock_location_id = "
                      "(SELECT stock_location_id FROM stock_locations WHERE location_code = 'SE-STO-01') AND product_id = "
                      "(SELECT min(product_id) FROM products)")
    blocked = engine(db, cfg, "--apply", "--windows", "1", "--yes")
    assert blocked.returncode == 2, blocked.stderr[-2000:]
    assert "BLOCKED (drift)" in blocked.stderr and "SE-STO-01" in blocked.stderr
    assert ledger(db) == before[1]                                      # no run row, nothing written
    with psycopg.connect(**KW, dbname=db, user="postgres") as admin:
        admin.execute("UPDATE inventory_balances SET reorder_point = reorder_point - 1 WHERE stock_location_id = "
                      "(SELECT stock_location_id FROM stock_locations WHERE location_code = 'SE-STO-01') AND product_id = "
                      "(SELECT min(product_id) FROM products)")
        admin.execute("INSERT INTO exchange_rates (rate_date, quote_currency_code, rate) VALUES ('2024-12-20', 'SEK', 11.5)")
    blocked = engine(db, cfg, "--apply", "--windows", "1", "--yes")
    assert blocked.returncode == 2 and "at or after the window start" in blocked.stderr
    assert ledger(db) == before[1]


# ============================================================================= composition in the database
def test_one_two_day_window_equals_two_one_day_windows(configs, contexts):
    ctx = contexts["stress"]
    a, b = clone("comp_a"), clone("comp_b")
    try:
        assert engine(a, configs["stress"], "--apply", "--windows", "2", "--yes").returncode == 0
        with loader(b) as conn:
            plan = plan_window(conn, ctx, window_days=2)
            assert (plan.start, plan.end) == (ctx.origin, ctx.origin + 2 * DAY)
            run_window(conn, ctx, plan)
        end = ctx.origin + 2 * DAY
        with reader(a) as ca, reader(b) as cb:
            assert state_hash(state_as_of(ca, end)) == state_hash(state_as_of(cb, end))
        assert digest(a) == digest(b)
        assert len(ledger(a)) == 2 and len(ledger(b)) == 1 and ledger(a)[1][11] == ledger(b)[0][11]
    finally:
        drop(a)
        drop(b)


# ============================================================================= lock
def test_second_engine_is_refused(db, configs):
    with loader(db) as holder:
        assert holder.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (ACTIVITY_LOCK,)).fetchone()[0]
        proc = engine(db, configs["stress"], "--apply", "--windows", "1", "--yes")
        assert proc.returncode == 4 and "holds the lock" in proc.stderr
    assert ledger(db) == []


# ============================================================================= milestone 2: incremental replay
JANUARY_END = 60                 # UTC day windows 2024-12-03 to 2025-01-31


def test_carry_round_trip_is_lossless(contexts, origin_state):
    ctx = contexts["stress"]
    sim = simulate(ctx, origin_state, ctx.origin + DAY)
    assert sim.carry["orders"], "orders confirmed after the online cut-off are open at midnight"
    text = json.dumps(sim.carry)
    assert json.loads(text) == sim.carry
    from nordiccart.activity.engine import decode, encode
    for item in sim.carry["queue"] + sim.carry["orders"]:
        assert encode(decode(item)) == item


def _replay_modes(log_text: str) -> list[str]:
    return re.findall(r"(incremental|full) replay from", log_text)


def test_incremental_equals_full_across_december_and_january(configs, contexts, origin_state, tmp_path):
    ctx = contexts["stress"]
    days = [ctx.origin + i * DAY for i in range(JANUARY_END + 1)]
    full = simulate(ctx, origin_state, days[-1], days)
    inc, ref = clone("inc"), clone("full")
    try:
        a = engine(inc, configs["stress"], "--apply", "--windows", str(JANUARY_END), "--yes")
        assert a.returncode == 0, a.stderr[-3000:]
        assert _replay_modes(a.stderr) == ["full"] + ["incremental"] * (JANUARY_END - 1)
        b = engine(ref, configs["stress"], "--apply", "--windows", str(JANUARY_END), "--yes", "--replay", "full")
        assert b.returncode == 0, b.stderr[-3000:]
        assert _replay_modes(b.stderr) == ["full"] * JANUARY_END
        runs = ledger(inc)
        assert len(runs) == JANUARY_END
        for i, r in enumerate(runs):
            assert r[2] == "verified"
            assert r[3].strip() == plan_fingerprint(full.window(days[i], days[i + 1]))
            assert r[6] == r[11] == state_hash(full.snapshots[days[i + 1]])
        assert runs == ledger(ref)                                       # incl. checkpoint snapshots with their carry
        assert digest(inc) == digest(ref)
        with reader(inc) as conn:                                        # the year boundary
            assert conn.execute("SELECT count(*) FROM exchange_rates").fetchone()[0] == 96 + 3 * JANUARY_END
            jan1, dec31 = (dict(conn.execute("SELECT quote_currency_code, rate FROM exchange_rates WHERE rate_date = %s",
                                             (d,)).fetchall()) for d in ("2025-01-01", "2024-12-31"))
            assert jan1 == dec31                                         # New Year's Day carries the last rate
            orders = conn.execute("SELECT count(*) FILTER (WHERE substr(order_number, 4, 4) = '2025'), max(substr(order_number, 9)::int), "
                                  "count(*) FROM orders").fetchone()
            assert orders[0] > 0 and orders[1] == orders[2]              # 2025 numbers, one global sequence without gaps
            first_2025 = conn.execute(
                "SELECT min(substr(external_reference, 12, 6)::int) FROM inventory_movements WHERE movement_type = "
                "'transfer_out' AND external_reference LIKE 'TR-__-2025-%%' GROUP BY substr(external_reference, 1, 10) "
                "UNION ALL SELECT min(substr(purchase_order_number, 12, 6)::int) FROM purchase_orders "
                "WHERE purchase_order_number LIKE 'PO-__-2025-%%' GROUP BY substr(purchase_order_number, 1, 10)").fetchall()
            assert first_2025 and {r[0] for r in first_2025} == {1}       # TR and PO numbers restart at 000001
        for runner in ("run_transactions.py", "run_replenishment.py"):
            check = _pilot_plan(inc, runner, tmp_path)
            assert check.returncode == 0 and "conflicts: 0   blocking: 0" in check.stderr, check.stderr[-2000:]
        results = scripts(inc)
        assert all(fail == 0 for _, _, fail, _ in results.values()), results
    finally:
        drop(inc)
        drop(ref)


def test_changed_carried_order_blocks_the_window(db, configs):
    cfg = configs["stress"]
    assert engine(db, cfg, "--apply", "--windows", "2", "--yes").returncode == 0
    with reader(db) as conn:
        carry = conn.execute("SELECT state_snapshot -> 'engine_carry' FROM meta.activity_checkpoints "
                             "ORDER BY state_as_of DESC LIMIT 1").fetchone()[0]
    from nordiccart.activity.engine import decode
    order = next(o for o in (decode(x) for x in carry["orders"]) if o.attempts)
    before = ledger(db)
    with psycopg.connect(**{**KW, "autocommit": False}, dbname=db, user="postgres") as admin:   # unseen by the state hash
        admin.execute("ALTER TABLE payment_attempts DISABLE TRIGGER USER")
        admin.execute("UPDATE payment_attempts SET provider_reference = provider_reference || 'X' WHERE attempt_number = 1 "
                      "AND order_id = (SELECT order_id FROM orders WHERE order_number = %s)", (order.order_number,))
        admin.execute("ALTER TABLE payment_attempts ENABLE TRIGGER USER")
        admin.commit()
    blocked = engine(db, cfg, "--apply", "--windows", "1", "--yes")
    assert blocked.returncode == 2 and f"open order {order.order_number}" in blocked.stderr, blocked.stderr[-2000:]
    assert ledger(db) == before


# ============================================================================= milestone 2: pilot loaders and the activity lock
@pytest.fixture(scope="module")
def pilot_plans():
    from nordiccart.inventory.config import load_inventory_settings
    from nordiccart.inventory.generate import generate_opening_inventory
    from nordiccart.master_data.generate import generate_master_data
    from nordiccart.replenishment.config import load_replenishment_settings
    from nordiccart.replenishment.generate import generate_replenishment
    from nordiccart.transactions.config import load_transaction_settings
    from nordiccart.transactions.generate import generate_transactions
    settings = load_settings(None, "pilot")
    inv = load_inventory_settings(settings)
    tx = load_transaction_settings(settings, inv.opening_date)
    master = generate_master_data(settings)
    rp = load_replenishment_settings(settings, tx.store_hours, {c.category_code for c in master.product_categories})
    inventory = generate_opening_inventory(settings, inv, master)
    pilot = generate_transactions(settings, tx, master, inventory, inv.store_format_weight)
    return pilot, generate_replenishment(settings, inv, rp, master, inventory, pilot)


def test_pilot_loaders_refuse_while_the_engine_runs(db, pilot_plans):
    from nordiccart.replenishment import load as rpload
    from nordiccart.transactions import load as txload
    from nordiccart.units.base import ACTIVITY_BUSY
    pilot, replenishment = pilot_plans
    before = digest(db)
    with loader(db) as holder:                                           # the engine's session lock
        assert holder.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (ACTIVITY_LOCK,)).fetchone()[0]
        with loader(db) as conn:
            with pytest.raises(txload.LoaderBusyError, match="activity engine"):
                txload.apply_transactions(conn, pilot)
            with pytest.raises(rpload.LoaderBusyError, match="activity engine"):
                rpload.apply_replenishment(conn, replenishment)
            assert str(ACTIVITY_BUSY).startswith("The activity engine is running")
            # their own locks were released again: a later load is not blocked by them
            assert conn.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
                                ).fetchone()[0] == 0
    assert digest(db) == before
    with loader(db) as conn:                                             # without the engine they run (nothing to write)
        assert txload.apply_transactions(conn, pilot).events_run == 0
        assert rpload.apply_replenishment(conn, replenishment).events_run == 0


def test_engine_refused_while_a_pilot_load_writes(db, configs, pilot_plans, monkeypatch):
    from nordiccart.transactions import load as txload
    pilot, _ = pilot_plans
    seen = {}
    original = txload._verify

    def verify_while_loading(conn, plan):                                # runs inside apply_transactions, under its locks
        seen["engine"] = engine(db, configs["stress"], "--apply", "--windows", "1", "--yes")
        return original(conn, plan)
    monkeypatch.setattr(txload, "_verify", verify_while_loading)
    with loader(db) as conn:
        txload.apply_transactions(conn, pilot)
    assert seen["engine"].returncode == 4 and "holds the lock" in seen["engine"].stderr
    assert ledger(db) == []
    assert engine(db, configs["stress"], "--apply", "--windows", "1", "--yes").returncode == 0   # free again afterwards


# ============================================================================= milestone 2: sandbox guard
def test_sandbox_guard_checks_the_database_identity(db, configs, contexts, sandbox_cluster, monkeypatch):
    from nordiccart.activity import run as run_mod
    cfg = configs["stress"]
    for overrides, text in (({"NORDICCART_S6_SANDBOX_CLUSTER": None}, "is not set"),
                            ({"NORDICCART_S6_SANDBOX_CLUSTER": "1234567890"}, "is not the approved sandbox cluster"),
                            ({"NORDICCART_S6_SANDBOX": None}, "NORDICCART_S6_SANDBOX=1 is not set")):
        proc = engine(db, cfg, "--apply", "--windows", "1", "--yes", env_overrides=overrides)
        assert proc.returncode == 3 and "REFUSED (sandbox guard)" in proc.stderr and text in proc.stderr, proc.stderr[-1500:]
    assert ledger(db) == []
    ctx = contexts["stress"]
    with loader(db) as conn:
        assert run_mod.require_sandbox(conn)["cluster"] == sandbox_cluster
        plan = plan_window(conn, ctx)
        with monkeypatch.context() as m:                                 # a protected (real) cluster identifier
            m.setattr(run_mod, "PROTECTED_CLUSTERS", frozenset({sandbox_cluster}))
            with pytest.raises(run_mod.SandboxGuardError, match="protected"):
                run_window(conn, ctx, plan)
        with monkeypatch.context() as m:                                 # the target of config/.env
            m.setattr(run_mod, "_protected_target", lambda: (55432, db))
            with pytest.raises(run_mod.SandboxGuardError, match="config/.env"):
                run_window(conn, ctx, plan)
        assert run_mod._protected_target() == (5432, "nordiccart_oltp")  # the real database, read from config/.env
    assert ledger(db) == []


# ============================================================================= milestone 3: monthly business rules
MONTHS = 119                     # UTC day windows 2024-12-03 to 2025-03-31: December, January, February, March


def test_price_timeline_and_rcal_in_memory(contexts, origin_state):
    from decimal import Decimal
    from nordiccart.activity.engine import add_months
    from nordiccart.master_data.rules import net_of_vat
    ctx = contexts["stress"]
    low, high = ctx.activity.markdown_range
    products = {p.sku: p for p in ctx.master.products}
    vat = {c.country_code: c.vat_rate for c in ctx.master.countries}
    assert ctx.price_changes, "pilot products reach 6 or 12 months after the origin"
    for c in ctx.price_changes:
        assert c.at >= ctx.origin and low <= c.markdown <= high
        assert c.new_price < c.old_price and c.unit_cost < net_of_vat(c.new_price, vat[c.market])
        assert c.at.astimezone(ctx.model.time_zone[c.market]).date() == \
            add_months(products[c.sku].launched_on, ctx.activity.markdown_after_months[c.step - 1])
        rows = ctx.price_rows[(c.sku, c.market)]                       # contiguous periods, the change in force from `at`
        assert all(a.valid_to == b.valid_from for a, b in zip(rows, rows[1:]))
        assert any(r.valid_from == c.at and r.list_price == c.new_price for r in rows)
    days = [ctx.origin + i * DAY for i in range(MONTHS + 1)]
    sim = simulate(ctx, origin_state, days[-1], days)
    rcal = [u for u in sim.units if u.kind == "RCAL"]
    assert {u.at.date().isoformat()[:7] for u in rcal} == {"2025-01", "2025-02", "2025-03"}
    for u in rcal:                                                     # R-cal sets the demand model's reorder points
        day = u.at.astimezone(ctx.model.time_zone[u.payload.location[:2]]).date()
        assert all(new == ctx.rcal(day)[(u.payload.location, sku)] for sku, _, new in u.payload.changes)
    assert [u for u in sim.units if u.kind == "P1"] and len([u for u in sim.units if u.kind == "P1"]) == \
        len([c for c in ctx.price_changes if c.at < days[-1]])


def test_order_up_to_follows_the_rcal_in_force(contexts, origin_state):
    """S8.1: S after an R-cal = max(minimum, forecast units over the weeks of cover, s + 1); before it, the planning figures."""
    from datetime import date
    from decimal import Decimal
    from nordiccart.inventory.generate import display_minimum
    from nordiccart.replenishment import policy
    ctx = contexts["stress"]
    inv = ctx.inv
    assert ctx.cover_units(date(2024, 12, 20)) is None                 # December's R-cal (12-02) is before the origin
    assert ctx.cover_units(date(2025, 1, 1)) is None                   # 01-01 is before January's R-cal (01-02)
    assert ctx.cover_units(date(2025, 1, 2)) is ctx.cover_units(date(2025, 1, 31)) is ctx.cover_units(date(2025, 2, 1))
    assert ctx.cover_units(date(2025, 2, 3)) is not ctx.cover_units(date(2025, 2, 1))   # February's R-cal is 02-03
    days = [ctx.origin + i * DAY for i in range(MONTHS + 1)]
    sim = simulate(ctx, origin_state, days[-1], days)
    checked = {"planning": 0, "rcal": 0}
    for u in sim.units:
        if u.kind == "T1":
            market, lines = u.payload.market, [(u.payload.destination, x.sku, x.reorder_point, x.order_up_to)
                                               for x in u.payload.lines]
            day = u.payload.requested_at.astimezone(ctx.model.time_zone[market]).date()
        elif u.kind == "PO":
            market, lines = u.payload.market, [(u.payload.warehouse, x.sku, x.reorder_point, x.order_up_to)
                                               for x in u.payload.lines]
            day = u.payload.ordered_at.astimezone(ctx.model.time_zone[market]).date()
        else:
            continue
        cover = ctx.cover_units(day)
        for loc, sku, rop, big_s in lines:
            x = ctx.listings[(loc, sku)]
            if cover is None:
                weekly, weeks = (ctx.echelon[(loc, sku)] if x.location_type == "warehouse" else x.expected_weekly_units,
                                 inv.weeks_of_cover[x.location_type])
            else:
                weekly, weeks = cover.get((loc, sku), Decimal(0)), Decimal(1)
            expected = (policy.warehouse_order_up_to(weekly, weeks, inv.warehouse_minimum_units, rop)
                        if x.location_type == "warehouse" else
                        policy.store_order_up_to(weekly, weeks, display_minimum(inv, x.price_eur, ctx.model.store_format[loc]), rop))
            assert big_s == expected, (u.label, sku, day)
            checked["planning" if cover is None else "rcal"] += 1
    assert checked["planning"] > 0 and checked["rcal"] > 0, checked


def test_a_month_outside_the_demand_calendar_has_no_orders(contexts):
    """S8 defect: the last window plans its neighbour month (2026-11), which is outside the calendar: no orders, no KeyError."""
    ctx = contexts["representative"]
    last = ctx.model.ds.calendar_end
    after = (last.replace(day=1) + 32 * DAY).replace(day=1)
    assert ctx.orders_of_month(after.year, after.month) == []
    assert ctx.orders_of_month(last.year, last.month)                     # the calendar's last month still has orders


def _month_lines(log_text: str) -> list[str]:
    return re.findall(r"month-end verification (\d{4}-\d{2}): (\d+) of (\d+) checks passed", log_text)


def test_four_months_with_monthly_rules_incremental_equals_full(configs, contexts, origin_state, tmp_path):
    ctx = contexts["stress"]
    days = [ctx.origin + i * DAY for i in range(MONTHS + 1)]
    full = simulate(ctx, origin_state, days[-1], days)
    inc, ref = clone("m3_inc"), clone("m3_full")
    try:
        pilot_before = digest(inc, pilot_only=True)
        a = engine(inc, configs["stress"], "--apply", "--windows", str(MONTHS), "--yes")
        assert a.returncode == 0, a.stderr[-3000:]
        assert _replay_modes(a.stderr) == ["full"] + ["incremental"] * (MONTHS - 1)
        months = _month_lines(a.stderr)
        assert [m[0] for m in months] == ["2024-12", "2025-01", "2025-02", "2025-03"]
        assert all(passed == total for _, passed, total in months), months
        b = engine(ref, configs["stress"], "--apply", "--windows", str(MONTHS), "--yes", "--replay", "full")
        assert b.returncode == 0, b.stderr[-3000:]
        runs = ledger(inc)
        assert runs == ledger(ref) and digest(inc) == digest(ref)
        for i, r in enumerate(runs):
            assert r[2] == "verified" and r[3].strip() == plan_fingerprint(full.window(days[i], days[i + 1]))
            assert r[6] == r[11] == state_hash(full.snapshots[days[i + 1]])
        with reader(inc) as conn:
            due = {(c.sku, c.market, c.at, c.new_price) for c in ctx.price_changes if c.at < days[-1]}
            recorded = set(conn.execute(
                "SELECT p.sku, pp.country_code, pp.valid_from, pp.list_price FROM product_prices pp JOIN products p "
                "USING (product_id) WHERE EXISTS (SELECT 1 FROM product_prices e WHERE e.product_id = pp.product_id "
                "AND e.country_code = pp.country_code AND e.valid_from < pp.valid_from)").fetchall())
            assert recorded == due and len(due) == 24
            assert conn.execute("SELECT count(*) FROM product_prices pp WHERE pp.valid_to IS NULL").fetchone()[0] == \
                conn.execute("SELECT count(*) * 4 FROM products").fetchone()[0]            # one current price each
            rcal = conn.execute("SELECT count(DISTINCT document_reference), count(*), "
                                "string_agg(DISTINCT to_char(effective_at AT TIME ZONE 'UTC', 'YYYY-MM-DD'), ',') "
                                "FROM reorder_point_revisions WHERE revision_kind = 'monthly_review'").fetchone()
            assert rcal[0] == sum(1 for u in full.units if u.kind == "RCAL") and rcal[2] == "2025-01-02,2025-02-03,2025-03-03"
            marked = conn.execute(                                       # orders after a markdown pay the new price
                "SELECT count(*) FROM order_items i JOIN orders o USING (order_id) JOIN products p USING (product_id) "
                "JOIN product_prices pp ON pp.product_id = i.product_id AND pp.country_code = o.country_code "
                "AND pp.valid_from <= o.placed_at AND (pp.valid_to IS NULL OR o.placed_at < pp.valid_to) "
                "WHERE i.unit_price = pp.list_price AND EXISTS (SELECT 1 FROM product_prices e WHERE e.product_id = "
                "pp.product_id AND e.country_code = pp.country_code AND e.valid_from < pp.valid_from)").fetchone()[0]
            assert marked > 0
        assert digest(inc, pilot_only=True) == pilot_before
        for runner in ("run_transactions.py", "run_replenishment.py"):
            check = _pilot_plan(inc, runner, tmp_path)
            assert check.returncode == 0 and "conflicts: 0   blocking: 0" in check.stderr, check.stderr[-2000:]
        results = scripts(inc)
        assert all(fail == 0 for _, _, fail, _ in results.values()), results
    finally:
        drop(inc)
        drop(ref)


def test_crash_in_monthly_rule_units_resumes_identically(configs, contexts, origin_state):
    """Kill the process inside the first R-cal unit (2025-01-02) and inside the first P1 unit (2025-01-03)."""
    ctx = contexts["stress"]
    jan2, jan3, jan4 = (ctx.origin + i * DAY for i in (30, 31, 32))
    sim = simulate(ctx, origin_state, jan4, [jan2, jan3, jan4])
    first_rcal = next(i for i, u in enumerate(sim.window(jan2, jan3), start=1) if u.kind == "RCAL")
    first_p1 = next(i for i, u in enumerate(sim.window(jan3, jan4), start=1) if u.kind == "P1")
    crashed, reference = clone("m3_crash"), clone("m3_ref")
    cfg = configs["stress"]
    try:
        assert engine(reference, cfg, "--apply", "--windows", "32", "--yes").returncode == 0
        assert engine(crashed, cfg, "--apply", "--windows", "30", "--yes").returncode == 0
        assert engine(crashed, cfg, "--apply", "--windows", "1", "--yes", fault=f"in_unit:{first_rcal}").returncode == 70
        assert ledger(crashed)[-1][8] == first_rcal - 1                  # the R-cal unit rolled back
        assert engine(crashed, cfg, "--apply", "--windows", "1", "--yes").returncode == 0
        assert engine(crashed, cfg, "--apply", "--windows", "1", "--yes", fault=f"in_unit:{first_p1}").returncode == 70
        assert ledger(crashed)[-1][8] == first_p1 - 1                    # the P1 unit rolled back
        assert engine(crashed, cfg, "--apply", "--windows", "1", "--yes").returncode == 0
        assert ledger(crashed) == ledger(reference) and digest(crashed) == digest(reference)
    finally:
        drop(crashed)
        drop(reference)


def test_month_end_verification_blocks_a_broken_month(db, configs):
    cfg = configs["stress"]
    proc = engine(db, cfg, "--apply", "--windows", str(DECEMBER), "--yes")
    assert proc.returncode == 0 and _month_lines(proc.stderr) == [("2024-12", "19", "19")], proc.stderr[-2000:]
    before = ledger(db)
    with psycopg.connect(**{**KW, "autocommit": False}, dbname=db, user="postgres") as admin:   # unseen by the state hash
        admin.execute("ALTER TABLE payment_attempts DISABLE TRIGGER USER")
        admin.execute("UPDATE payment_attempts SET amount = amount + 1 WHERE payment_attempt_id = (SELECT min(a.payment_attempt_id) "
                      "FROM payment_attempts a JOIN orders o USING (order_id) WHERE o.placed_at >= '2024-12-03+00')")
        admin.execute("ALTER TABLE payment_attempts ENABLE TRIGGER USER")
        admin.commit()
    blocked = engine(db, cfg, "--apply", "--windows", "1", "--yes")
    assert blocked.returncode == 1 and "MONTH VERIFICATION FAILED" in blocked.stderr \
        and "1 payment attempts not for the order total" in blocked.stderr, blocked.stderr[-2000:]
    assert ledger(db) == before                                          # January does not start


# ============================================================================= milestone 3: altered future actions in the carry
def _tamper_carry(db: str, change) -> dict:
    """Rewrite the last checkpoint's carry with `change(carry)` (sandbox, guards disabled); returns the original."""
    with psycopg.connect(**{**KW, "autocommit": False}, dbname=db, user="postgres") as admin:
        run_id, snapshot = admin.execute("SELECT activity_run_id, state_snapshot FROM meta.activity_checkpoints "
                                         "ORDER BY state_as_of DESC LIMIT 1").fetchone()
        original = json.loads(json.dumps(snapshot))
        snapshot["engine_carry"] = change(snapshot["engine_carry"])
        admin.execute("ALTER TABLE meta.activity_checkpoints DISABLE TRIGGER USER")
        admin.execute("UPDATE meta.activity_checkpoints SET state_snapshot = %s WHERE activity_run_id = %s",
                      (psycopg.types.json.Jsonb(snapshot), run_id))
        admin.execute("ALTER TABLE meta.activity_checkpoints ENABLE TRIGGER USER")
        admin.commit()
    return original


def _restore_checkpoint(db: str, original: dict) -> None:
    _tamper_carry(db, lambda carry: original["engine_carry"])


def test_altered_future_actions_in_the_carry_block_the_window(db, configs):
    from dataclasses import replace as dc_replace
    from datetime import timedelta
    from nordiccart.activity.engine import carry_digest, decode, encode
    cfg = configs["stress"]
    assert engine(db, cfg, "--apply", "--windows", "2", "--yes").returncode == 0
    before = ledger(db)

    def later_fulfilment(carry, fix_digest=True):
        """Move one open order's planned fulfilment (its order and its queued step) one hour later."""
        orders = [decode(x) for x in carry["orders"]]
        target = next(o for o in orders if o.fulfilled_at is not None)
        moved = dc_replace(target, fulfilled_at=target.fulfilled_at + timedelta(hours=1),
                           reservation_closed_at=target.reservation_closed_at + timedelta(hours=1))
        carry["orders"] = [encode(moved if o.order_number == target.order_number else o) for o in orders]
        queue = []
        for entry in (decode(x) for x in carry["queue"]):
            at, rank, key, action, payload = entry
            if action == "order_event" and payload[0] == target.order_number and payload[1].kind == "fulfil":
                event = dc_replace(payload[1], at=payload[1].at + timedelta(hours=1))
                entry = [event.at, rank, key, action, (payload[0], event)]
            queue.append(encode(entry))
        carry["queue"] = queue
        if fix_digest:
            carry["sha256"] = carry_digest(carry)
        return carry

    def drop_one_step(carry):
        carry["queue"] = carry["queue"][1:]
        carry["sha256"] = carry_digest(carry)
        return carry

    for change, text in ((later_fulfilment, "differs from its derivation"),
                         (lambda c: later_fulfilment(c, fix_digest=False), "does not match its SHA-256"),
                         (drop_one_step, "differ")):
        original = _tamper_carry(db, change)
        try:
            blocked = engine(db, cfg, "--apply", "--windows", "1", "--yes")
            assert blocked.returncode == 2 and "carry cannot be trusted" in blocked.stderr and text in blocked.stderr, \
                blocked.stderr[-2000:]
            assert ledger(db)[:-1] == before[:-1] and len(ledger(db)) == len(before)     # nothing written
        finally:
            _restore_checkpoint(db, original)
    assert engine(db, cfg, "--apply", "--windows", "1", "--yes").returncode == 0     # the original carry is accepted
