"""Window run protocol (design 7.3) on the V009 run ledger and checkpoints.

    1. session advisory lock nordiccart.activity (the caller takes it; one engine at a time; the
       pilot loaders take it too while they write)
    2. the last run decides the window: a running or failed run is resumed, otherwise the
       next UTC day after the last verified window (the first window starts at the origin)
    3. drift checks: the history before the origin still has the first run's start state;
       the database state at the window start equals the previous checkpoint
    4. the plan: incremental replay from the previous checkpoint (its state, which the
       database must still hold, and its carry, checked against the database), or a full
       replay from the origin (first window, an older checkpoint without carry, or on request);
       a new run row 'running' with its fingerprints, or, on resume, the same fingerprints as
       the stored row (else: code or config changed inside a window)
    5. the remaining units of work, each one transaction that also advances completed_units
       (prefix rule: the done units of the window are exactly its first completed_units units)
    6. verification: every planned unit done, the database state at the window end equals
       the replay (counts, balances, reservations, transfers, purchase orders, open orders,
       numbering, exchange rates), ledger identity, no business time at or after the end
    7. one transaction: the checkpoint (state snapshot, its SHA-256, and the carry for the next
       window) and the run 'verified'
A failure in 5 or 6 marks the run 'failed' (with the reason) when the database is reachable;
the next start resumes it. A crash leaves it 'running'; the next start resumes it too.

Sandbox guard (milestone 2): run_window refuses unless require_sandbox() accepts the connection:
NORDICCART_S6_SANDBOX=1, the server's cluster system identifier (pg_control_system) equals
NORDICCART_S6_SANDBOX_CLUSTER, it is not a protected cluster, and the server port and database
are not the target configured in config/.env (the real database).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from ..exchange_rates import reconcile
from ..settings import DEFAULT_ENV_FILE
from ..units import orders as order_units
from ..units import prices as price_units
from ..units import stock as stock_units
from ..units.base import ACTIVITY_LOCK, LEDGER_IDENTITY_SQL, Ids, expect, run_unit  # noqa: F401  (ACTIVITY_LOCK re-exported)
from .engine import (
    DAY, CarryError, EngineContext, EngineError, Simulation, Unit, decode, plan_fingerprint, simulate, unit_counts,
)
from .monthly import MonthVerificationError, verify_month
from .state import differences, latest_business_time, state_as_of, state_hash

log = logging.getLogger("nordiccart.activity")
CARRY_KEY = "engine_carry"
# Cluster system identifiers the engine must never write to, whatever the environment says: the real
# NordicCart server (pg_control_system().system_identifier, read read-only as nordiccart_loader on 2026-11-19).
PROTECTED_CLUSTERS = frozenset({"7663257437785384692"})


class DriftError(EngineError):
    """The database does not hold the state the engine expects (blocking; nothing written)."""


class WindowVerificationError(EngineError):
    """The window's result differs from the plan (run marked failed)."""


class SandboxGuardError(EngineError):
    """The connection is not an approved sandbox database (milestone 2: the engine writes only there)."""


# ----------------------------------------------------------------------------- sandbox guard
def _protected_target() -> tuple[int, str] | None:
    """(port, database) of config/.env, read from the file without loading it (the real database)."""
    if not DEFAULT_ENV_FILE.is_file():
        return None
    values = {}
    for line in DEFAULT_ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            values[key.strip().removeprefix("export ").strip()] = value.strip().strip("'\"")
    if "PGDATABASE" not in values:
        return None
    return int(values.get("PGPORT", "5432")), values["PGDATABASE"]


def is_month_start(t: datetime) -> bool:
    """00:00 UTC on the 1st: a window ending here completes a month (hourly windows end on other hours too)."""
    return t.day == 1 and t.hour == 0 and t.minute == 0 and t.second == 0 and t.microsecond == 0


def database_identity(conn: psycopg.Connection) -> dict:
    cluster, port, database = conn.execute(
        "SELECT (SELECT system_identifier FROM pg_control_system())::text, inet_server_port(), current_database()").fetchone()
    return {"cluster": cluster, "port": port, "database": database}


REAL_AUTHORIZATION = "NORDICCART_S9_REAL_AUTHORIZED"   # S9: "<cluster>/<database>", set only after explicit authorization


def require_sandbox(conn: psycopg.Connection) -> dict:
    """The identity of an approved sandbox database (or the one authorized real target, S9), or SandboxGuardError.

    S9 (real historical catch-up): with NORDICCART_S9_REAL_AUTHORIZED = "<cluster>/<database>" the engine may
    write to that target only if it names the connected server exactly, the server is a protected (real)
    cluster and it is the config/.env target. Never together with the sandbox switch (no fault injection).
    """
    ident = database_identity(conn)
    token = os.environ.get(REAL_AUTHORIZATION, "").strip()
    if token:
        if os.environ.get("NORDICCART_S6_SANDBOX") == "1":
            raise SandboxGuardError(f"{REAL_AUTHORIZATION} and NORDICCART_S6_SANDBOX=1 are mutually exclusive")
        if token != f"{ident['cluster']}/{ident['database']}" or ident["cluster"] not in PROTECTED_CLUSTERS \
                or (ident["port"], ident["database"]) != _protected_target():
            raise SandboxGuardError(f"{REAL_AUTHORIZATION}={token} does not name this server: cluster {ident['cluster']}, "
                                    f"port {ident['port']}, database {ident['database']} (the config/.env real target only)")
        return ident
    if os.environ.get("NORDICCART_S6_SANDBOX") != "1":
        raise SandboxGuardError("the activity engine writes only to the sandbox in S6: NORDICCART_S6_SANDBOX=1 is not set")
    if ident["cluster"] in PROTECTED_CLUSTERS:
        raise SandboxGuardError(f"cluster {ident['cluster']} is a protected (real) NordicCart server")
    expected = os.environ.get("NORDICCART_S6_SANDBOX_CLUSTER", "").strip()
    if not expected:
        raise SandboxGuardError("NORDICCART_S6_SANDBOX_CLUSTER (the sandbox cluster's system identifier) is not set")
    if ident["cluster"] != expected:
        raise SandboxGuardError(f"the connected cluster {ident['cluster']} is not the approved sandbox cluster {expected}")
    if (ident["port"], ident["database"]) == _protected_target():
        raise SandboxGuardError(f"port {ident['port']} and database {ident['database']} are the target of config/.env "
                                "(the real database)")
    return ident


# ----------------------------------------------------------------------------- plan
@dataclass
class WindowPlan:
    start: datetime
    end: datetime
    units: list[Unit]
    fingerprint: str
    start_state: dict
    start_hash: str
    end_state: dict
    end_hash: str
    end_carry: dict
    origin_hash: str
    done: int                                   # units already in the database (prefix)
    replay: str                                 # incremental or full
    seconds: float                              # planning time (state reads, replay, checks)
    resume: dict | None = None                  # the stored run row when resuming
    sim: Simulation | None = field(default=None, repr=False)

    @property
    def remaining(self) -> list[Unit]:
        return self.units[self.done:]


def _runs(conn) -> list[dict]:
    cur = conn.execute("SELECT activity_run_id, window_start, window_end, status::text, plan_fingerprint, "
                       "config_fingerprint, start_state_hash, end_state_hash, planned_units, completed_units "
                       "FROM meta.activity_runs ORDER BY window_end")
    names = [d.name for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _checkpoint(conn, run_id: int):
    return conn.execute("SELECT state_as_of, state_hash, state_snapshot FROM meta.activity_checkpoints "
                        "WHERE activity_run_id = %s", (run_id,)).fetchone()


def _without_carry(snapshot: dict) -> dict:
    return {k: v for k, v in snapshot.items() if k != CARRY_KEY}


def _checkpoint_differences(conn, runs: list[dict]) -> str:
    """What changed since the last checkpoint: its stored snapshot against the database now, as of the same time."""
    last = next((r for r in reversed(runs) if r["status"] == "verified"), None)
    row = _checkpoint(conn, last["activity_run_id"]) if last else None
    if row is None:
        return ""
    found = differences(state_as_of(conn, row[0]), _without_carry(row[2]))
    return f"; database vs checkpoint of run {last['activity_run_id']}: " + "; ".join(found) if found else ""


def validate_carry(conn, carry: dict) -> None:
    """The carried open orders and documents are the ones the database holds (DriftError otherwise)."""
    queue = [decode(x) for x in carry["queue"]]
    pending_starts = {(e[4][0], e[4][1].attempt_number) for e in queue
                      if e[3] == "order_event" and e[4][1].kind == "payment_start"}
    for o in (decode(x) for x in carry["orders"]):
        header = conn.execute(
            "SELECT o.order_id, c.customer_number, o.channel::text, l.location_code, o.placed_at, o.order_total "
            "FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
            "JOIN stock_locations l ON l.stock_location_id = o.fulfilment_location_id WHERE o.order_number = %s",
            (o.order_number,)).fetchone()
        same = header is not None and tuple(header[1:]) == (o.customer_number, o.channel, o.fulfilment_location_code,
                                                            o.placed_at, o.order_total)
        if same:
            lines = conn.execute("SELECT i.line_number, p.sku, i.quantity, i.unit_price FROM order_items i "
                                 "JOIN products p ON p.product_id = i.product_id WHERE i.order_id = %s ORDER BY 1",
                                 (header[0],)).fetchall()
            attempts = conn.execute("SELECT attempt_number, payment_method::text, provider_reference FROM payment_attempts "
                                    "WHERE order_id = %s ORDER BY 1", (header[0],)).fetchall()
            started = [a for a in o.attempts if (o.order_number, a.attempt_number) not in pending_starts]
            same = [tuple(r) for r in lines] == [(x.line_number, x.sku, x.quantity, x.unit_price) for x in o.lines] \
                and [tuple(r) for r in attempts] == [(a.attempt_number, a.payment_method, a.provider_reference)
                                                     for a in started]
        if not same:
            raise DriftError(f"the open order {o.order_number} in the database differs from the checkpoint's carry")
    for at, rank, key, action, payload in queue:
        if action == "arrive" and payload.dispatched_at < datetime.fromisoformat(carry["as_of"]):
            rows = conn.execute("SELECT p.sku, -m.quantity_change, l.location_code, m.occurred_at FROM inventory_movements m "
                                "JOIN products p ON p.product_id = m.product_id JOIN stock_locations l "
                                "ON l.stock_location_id = m.stock_location_id WHERE m.movement_type = 'transfer_out' "
                                "AND m.external_reference = %s ORDER BY p.sku", (payload.reference,)).fetchall()
            if [tuple(r) for r in rows] != sorted((x.sku, x.quantity, payload.source, payload.dispatched_at)
                                                  for x in payload.lines):
                raise DriftError(f"transfer {payload.reference} in the database differs from the checkpoint's carry")
        elif action == "receive":
            po, _ = payload
            rows = conn.execute("SELECT l.location_code, po.ordered_at, pl.line_number, p.sku, pl.order_reason::text, "
                                "pl.quantity_ordered FROM purchase_orders po JOIN purchase_order_lines pl "
                                "ON pl.purchase_order_id = po.purchase_order_id JOIN products p ON p.product_id = pl.product_id "
                                "JOIN stock_locations l ON l.stock_location_id = po.stock_location_id "
                                "WHERE po.purchase_order_number = %s ORDER BY pl.line_number", (po.number,)).fetchall()
            if [tuple(r) for r in rows] != [(po.warehouse, po.ordered_at, x.line_number, x.sku, x.trigger, x.quantity)
                                            for x in po.lines]:
                raise DriftError(f"purchase order {po.number} in the database differs from the checkpoint's carry")


def plan_window(conn: psycopg.Connection, ctx: EngineContext, window_days: int = 1, replay: str = "auto",
                end_at: datetime | None = None) -> WindowPlan:
    """The next (or resumed) window and its checks. Read-only.

    replay: auto (incremental when the previous checkpoint has a carry, else full), full, incremental.
    end_at: an explicit window end (S10 hourly tick: the next hour boundary, at most the next UTC midnight);
    a resumed window keeps its stored end.
    """
    started = time.perf_counter()
    runs = _runs(conn)
    last = runs[-1] if runs else None
    resume = last if last and last["status"] in ("running", "failed") else None
    if resume:
        start, end = resume["window_start"], resume["window_end"]
    else:
        start = last["window_end"] if last else ctx.origin
        end = end_at if end_at is not None else start + window_days * DAY
        if end <= start:
            raise EngineError(f"window end {end.isoformat()} is not after its start {start.isoformat()}")
    if start < ctx.origin:
        raise EngineError(f"window {start} starts before the engine origin {ctx.origin}")

    origin_state = state_as_of(conn, ctx.origin)
    origin_hash = state_hash(origin_state)
    if runs and runs[0]["window_start"] == ctx.origin and runs[0]["start_state_hash"].strip() != origin_hash:
        raise DriftError("the history before the engine origin changed since the first run "
                         f"(start state {runs[0]['start_state_hash'][:12]}..., now {origin_hash[:12]}...)"
                         + _checkpoint_differences(conn, runs))
    db_start = state_as_of(conn, start)
    previous = next((r for r in reversed(runs) if r["status"] == "verified"), None)
    carry = None
    if previous is not None:
        checkpoint = _checkpoint(conn, previous["activity_run_id"])
        if checkpoint is None or checkpoint[1] != state_hash(db_start):
            raise DriftError(f"the database state at {start.isoformat()} does not match the checkpoint of run "
                             f"{previous['activity_run_id']} (state drift)" + _checkpoint_differences(conn, runs))
        carry = checkpoint[2].get(CARRY_KEY) if previous["window_end"] == start else None
        if carry is not None and carry.get("config_fingerprint") != ctx.config_fingerprint:
            carry = None                          # another engine version or configuration: replay from the origin
    if replay == "incremental" and carry is None:
        raise EngineError("incremental replay needs the previous checkpoint's carry for this engine and configuration")
    if resume is None and is_month_start(start) and previous is not None and previous["window_end"] == start:
        month = verify_month(conn, ctx, (start - DAY).date())          # gate: the month just completed
        if month.problems:
            raise MonthVerificationError(f"month {month.month} fails its month-end verification: "
                                         + "; ".join(month.problems[:6]))
    if replay != "full" and carry is not None:
        validate_carry(conn, carry)
        try:
            sim = simulate(ctx, db_start, end, [start, end], carry=carry)
        except CarryError as exc:
            raise DriftError(f"the checkpoint's carry cannot be trusted: {exc}") from None
        mode = "incremental"
    else:
        sim = simulate(ctx, origin_state, end, [start, end])
        mode = "full"
        if state_hash(db_start) != state_hash(sim.snapshots[start]):
            raise DriftError("the database state at the window start differs from the replay: "
                             + "; ".join(differences(db_start, sim.snapshots[start])))
    expected_start, expected_end = sim.snapshots[start], sim.snapshots[end]

    units = sim.window(start, end)
    fingerprint = plan_fingerprint(units)
    latest = latest_business_time(conn)
    if resume is None and latest is not None and latest >= start:
        raise DriftError(f"the database already holds business time {latest.isoformat()} at or after the window "
                         f"start {start.isoformat()} and no run is open for it")
    if resume is not None and latest is not None and latest >= end:
        raise DriftError(f"the database holds business time {latest.isoformat()} after the resumed window")
    done = done_prefix(conn, units)
    if resume is not None:
        if (resume["plan_fingerprint"].strip(), resume["config_fingerprint"].strip(), resume["start_state_hash"].strip()) \
                != (fingerprint, ctx.config_fingerprint, state_hash(expected_start)):
            raise DriftError(f"run {resume['activity_run_id']} was started with another plan, configuration or start "
                             "state (code or config changed inside a window); restore or decide explicitly")
        if done != resume["completed_units"]:
            raise DriftError(f"run {resume['activity_run_id']} records {resume['completed_units']} completed units, "
                             f"the database holds {done}")
    elif done:
        raise DriftError(f"{done} units of a window without a run row are already in the database")
    return WindowPlan(start, end, units, fingerprint, expected_start, state_hash(expected_start), expected_end,
                      state_hash(expected_end), sim.carry, origin_hash, done, mode, time.perf_counter() - started,
                      resume, sim)


# ----------------------------------------------------------------------------- done checks (prefix rule)
def is_done(conn, u: Unit) -> bool:
    q = conn.execute
    if u.kind == "X1":
        return q("SELECT EXISTS (SELECT 1 FROM exchange_rates WHERE rate_date = %s)", (u.payload,)).fetchone()[0]
    if u.kind == "P1":
        return q("SELECT EXISTS (SELECT 1 FROM product_prices pp JOIN products p USING (product_id) WHERE p.sku = %s "
                 "AND pp.country_code = %s AND pp.valid_from = %s)", (u.payload.sku, u.payload.market, u.payload.at)).fetchone()[0]
    if u.kind == "RCAL":
        return q("SELECT EXISTS (SELECT 1 FROM reorder_point_revisions WHERE document_reference = %s)",
                 (u.payload.reference,)).fetchone()[0]
    if u.kind in ("PO", "R1", "T1", "T2"):
        if u.kind == "PO":
            return q("SELECT EXISTS (SELECT 1 FROM purchase_orders WHERE purchase_order_number = %s)",
                     (u.payload.number,)).fetchone()[0]
        reference = u.payload[1].reference if u.kind == "R1" else u.payload.reference
        kind = {"R1": "purchase_receipt", "T1": "transfer_out", "T2": "transfer_in"}[u.kind]
        return q("SELECT EXISTS (SELECT 1 FROM inventory_movements WHERE movement_type = %s::movement_type "
                 "AND external_reference = %s)", (kind, reference)).fetchone()[0]
    number = u.payload.order_number
    if u.kind == "place":
        return q("SELECT EXISTS (SELECT 1 FROM orders WHERE order_number = %s)", (number,)).fetchone()[0]
    if u.kind == "reserve":
        return q("SELECT EXISTS (SELECT 1 FROM orders o JOIN order_items i ON i.order_id = o.order_id "
                 "JOIN inventory_reservations r ON r.order_item_id = i.order_item_id WHERE o.order_number = %s)",
                 (number,)).fetchone()[0]
    if u.kind in ("payment_start", "payment_result"):
        row = q("SELECT a.status::text FROM payment_attempts a JOIN orders o ON o.order_id = a.order_id "
                "WHERE o.order_number = %s AND a.attempt_number = %s", (number, u.attempt)).fetchone()
        return row is not None and (u.kind == "payment_start" or row[0] != "pending")
    status = q("SELECT status::text FROM orders WHERE order_number = %s", (number,)).fetchone()
    if u.kind in ("customer_cancel", "expire"):
        return status is not None and status[0] == "cancelled"
    if u.kind == "fulfil":
        return status is not None and status[0] == "fulfilled"
    raise EngineError(f"unknown unit kind {u.kind}")


def done_prefix(conn, units: list[Unit]) -> int:
    """Number of leading units already in the database; a done unit after an open one is blocking."""
    flags = [is_done(conn, u) for u in units]
    k = flags.index(False) if False in flags else len(flags)
    if any(flags[k:]):
        late = next(u for u, f in zip(units[k:], flags[k:]) if f)
        raise DriftError(f"units of this window are done out of order: {late.label} is recorded although "
                         f"{units[k].label} is not")
    return k


# ----------------------------------------------------------------------------- execution
def _fault(stage: str, n: int = 0) -> None:
    """Sandbox fault injection for the crash tests (NORDICCART_ENGINE_FAULT=<stage>[:<n>]).

    Honoured only with NORDICCART_S6_SANDBOX=1 (and run_window has passed require_sandbox). The
    process ends at once (os._exit), like a killed process: an open transaction is rolled back.
    """
    spec = os.environ.get("NORDICCART_ENGINE_FAULT")
    if not spec or os.environ.get("NORDICCART_S6_SANDBOX") != "1":
        return
    name, _, number = spec.partition(":")
    if name == stage and (not number or int(number) == n):
        logging.getLogger("nordiccart.activity").warning("FAULT INJECTION: process ends at %s %s", stage, n or "")
        logging.shutdown()
        os._exit(70)


def _body(conn, ctx: EngineContext, ids: Ids, u: Unit) -> str:
    if u.kind == "X1":
        rows = reconcile(conn, ctx.settings, u.payload, u.payload, apply=True)
        expect(len(rows) == 3, f"X1 {u.payload}: {len(rows)} rates written, 3 planned")
        return "3 exchange rates"
    if u.kind == "P1":
        return price_units.change_price(conn, ids, u.payload, u.label)
    if u.kind == "RCAL":
        r = u.payload
        return stock_units.review_reorder_points(conn, ids, r.location, r.at, r.changes, r.reference, u.label)
    if u.kind == "PO":
        expect(stock_units.ensure_purchase_order(conn, ids, u.payload, u.label), f"{u.label}: already recorded")
        return f"purchase order {u.payload.number} recorded, {u.payload.units} units on order"
    if u.kind == "R1":
        po, d = u.payload
        return stock_units.receive(conn, ids, po, d, u.label)
    if u.kind == "T1":
        return stock_units.dispatch(conn, ids, u.payload, u.label)
    if u.kind == "T2":
        return stock_units.arrive(conn, ids, u.payload, u.label)
    o = u.payload
    if u.kind in ("payment_start", "payment_result"):
        return getattr(order_units, u.kind)(conn, ids, o, u.attempt)
    return getattr(order_units, u.kind)(conn, ids, o)


@dataclass
class WindowResult:
    run_id: int
    start: datetime
    end: datetime
    planned: int
    executed: int
    resumed_from: int
    end_hash: str
    counts: dict


def run_window(conn: psycopg.Connection, ctx: EngineContext, plan: WindowPlan) -> WindowResult:
    """Execute and verify one planned window (the caller holds the activity lock). Sandbox only (S6)."""
    require_sandbox(conn)
    planned = len(plan.units)
    if plan.resume is None:
        run_id = conn.execute(
            "INSERT INTO meta.activity_runs (run_mode, window_start, window_end, plan_fingerprint, config_fingerprint, "
            "start_state_hash, planned_units) VALUES ('catch_up', %s, %s, %s, %s, %s, %s) RETURNING activity_run_id",
            (plan.start, plan.end, plan.fingerprint, ctx.config_fingerprint, plan.start_hash, planned)).fetchone()[0]
        log.info("run %d started: window %s to %s, %d units of work, plan %s...", run_id, plan.start.isoformat(),
                 plan.end.isoformat(), planned, plan.fingerprint[:16])
    else:
        run_id = plan.resume["activity_run_id"]
        if plan.resume["status"] == "failed":
            conn.execute("UPDATE meta.activity_runs SET status = 'running', failure_reason = NULL, finished_at = NULL "
                         "WHERE activity_run_id = %s", (run_id,))
        log.info("run %d resumed: window %s to %s, %d of %d units already done", run_id, plan.start.isoformat(),
                 plan.end.isoformat(), plan.done, planned)
    ids = Ids(conn)
    position = plan.done
    try:
        for u in plan.remaining:
            def work(u=u, position=position):
                result = _body(conn, ctx, ids, u)
                n = conn.execute("UPDATE meta.activity_runs SET completed_units = %s "
                                 "WHERE activity_run_id = %s AND completed_units = %s",
                                 (position + 1, run_id, position)).rowcount
                expect(n == 1, f"run {run_id}: progress is not at unit {position}")
                _fault("in_unit", position + 1)
                return result
            result = run_unit(conn, work, u.label, log)
            position += 1
            log.debug("%s UTC %-40s %s", u.at.strftime("%Y-%m-%d %H:%M:%S"), u.label, result)
            _fault("after_unit", position)
        _fault("before_verify")
        problems = verify_window(conn, plan)
        if problems:
            raise WindowVerificationError(f"window {plan.start.isoformat()}: " + "; ".join(problems[:6]))
        counts = unit_counts(plan.units)
        with conn.transaction():
            conn.execute("UPDATE meta.activity_runs SET status = 'verified', end_state_hash = %s, completed_units = %s, "
                         "unit_counts = %s, finished_at = now() WHERE activity_run_id = %s",
                         (plan.end_hash, planned, Jsonb(counts), run_id))
            conn.execute("INSERT INTO meta.activity_checkpoints (activity_run_id, state_as_of, state_hash, state_snapshot) "
                         "VALUES (%s, %s, %s, %s)",
                         (run_id, plan.end, plan.end_hash, Jsonb({**plan.end_state, CARRY_KEY: plan.end_carry})))
            _fault("in_checkpoint")
    except (EngineError, psycopg.Error, ValueError, RuntimeError) as exc:
        if not conn.closed and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
            try:
                conn.execute("UPDATE meta.activity_runs SET status = 'failed', failure_reason = %s, finished_at = now() "
                             "WHERE activity_run_id = %s AND status = 'running'", (str(exc)[:1000], run_id))
            except psycopg.Error:
                pass
        raise
    log.info("run %d verified: %d units (%s), checkpoint %s... as of %s", run_id, planned,
             ", ".join(f"{k} {v}" for k, v in counts.items()), plan.end_hash[:16], plan.end.isoformat())
    return WindowResult(run_id, plan.start, plan.end, planned, planned - plan.done, plan.done, plan.end_hash, counts)


def verify_window(conn: psycopg.Connection, plan: WindowPlan) -> list[str]:
    problems = []
    done = done_prefix(conn, plan.units)
    if done != len(plan.units):
        problems.append(f"{done} of {len(plan.units)} units done")
    actual = state_as_of(conn, plan.end)
    if state_hash(actual) != plan.end_hash:
        problems.append("state at the window end differs from the replay: " + "; ".join(differences(actual, plan.end_state)))
    broken = conn.execute(LEDGER_IDENTITY_SQL).fetchone()[0]
    if broken:
        problems.append(f"{broken} balances break the ledger identity")
    latest = latest_business_time(conn)
    if latest is not None and latest >= plan.end:
        problems.append(f"business time {latest.isoformat()} at or after the window end")
    return problems
