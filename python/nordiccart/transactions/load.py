"""Loading the pilot transactions through the V006/V007 units of work.

How it writes
  * Every event of every order runs as its own transaction, in business-time order across
    all orders, exactly as the design's units of work (DBML project note):
        U1  INSERT orders (placed) and order_items, with the totals already computed
        U2  SELECT fn_reserve_order_stock(order, reserved_at, expires_at); when it returns
            out_of_stock the transaction is rolled back and a NEW transaction cancels the
            order (out_of_stock)
        U3a INSERT payment_attempts (pending), committed before the provider is "called"
        U3c lock the order, then: pending -> authorized -> captured and the order confirmed;
            or pending -> failed; after the last failure fn_close_order_reservations
            (released) and the order payment_failed, in the same transaction
        U4  lock the due order (FOR UPDATE SKIP LOCKED), fn_close_order_reservations
            (expired), void any authorization, order cancelled (reservation_expired)
        U5  SELECT fn_fulfil_order_stock(order, fulfilled_at): reservations consumed, then
            sale movements; order fulfilled
    Customer cancellation: fn_close_order_reservations (released), order cancelled
    (customer_request). Stock quantities are never written directly; every rule and
    deferred check of V006 and V007 runs on every commit.
  * Each step checks the database result against the plan (for example that U2 returns
    reserved, or out_of_stock for the out-of-stock scenarios). A different result rolls that
    transaction back and stops the run.
  * Retries: SQLSTATE 40001, 40P01 and 55P03 are retried up to 3 times with jittered
    backoff; business rejections (23514, 23503) are never retried (design retry rules).

Why a repeat or resumed run cannot duplicate anything
  * Each order is matched on order_number. From what the database holds (order status,
    payment attempts, reservations, sale movements) the loader works out how many of the
    order's steps are done, and compares every stored value with the plan. A difference is
    a CONFLICT and nothing is written.
  * The steps already done must be a prefix of the plan in business-time order, and the
    stock of all 320 listings must equal the opening stock replayed through exactly those
    steps. Then only the remaining steps run. An interrupted run therefore continues where
    it stopped, and a complete one does nothing.
  * The database itself refuses doubles: order_number is unique, one live payment attempt
    per order, one active reservation and one sale movement per order line, guarded
    status transitions.
  * A session advisory lock stops two loaders from running at the same time.

Records outside this pilot (Phase 3, step 5, stage S1)
  * The loader reads only the pilot's own 20 orders and their rows. Every other order and
    every movement other than opening balances and the pilot's sales is foreign to it.
  * A foreign record is accepted as LATER ACTIVITY only when it passes the identity and
    integrity rules of nordiccart.activity.scope: dated strictly after the plan's last
    event, order numbers that continue the pilot's sequence, valid document references,
    consistent lines, payments, reservations, sales and transfer pairs, simulated movement
    types only (exception E2: opening balances at listings outside the pilot). A date after
    the pilot alone is never enough. Anything else blocks the run.
  * Later activity is accepted only when the plan is complete (otherwise the remaining steps
    would be written out of business-time order). Its movements and active reservations at
    the pilot's listings are added to the expected stock, so the pilot keeps verifying.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from ..activity.scope import LaterActivity, PilotScope, expected_stock, read_later_activity
from ..inventory.load import build_inventory_plan, check_inventory_database
from ..inventory.model import OpeningInventory
from ..master_data.load import PreconditionError
from ..master_data.model import MasterDataset
from ..units import orders as order_units
from ..units.base import (
    ACTIVITY_BUSY, ACTIVITY_LOCK, LEDGER_IDENTITY_SQL, Ids, OutcomeMismatchError, release_session_lock, run_unit,
    try_session_lock,
)
from .model import Event, PlannedOrder, TransactionPlan

log = logging.getLogger("nordiccart.transactions.load")

ADVISORY_LOCK_KEY = "nordiccart.transactions"
REQUIRED_FUNCTIONS = (
    "fn_reserve_order_stock(bigint,timestamp with time zone,timestamp with time zone)",
    "fn_close_order_reservations(bigint,reservation_status,timestamp with time zone)",
    "fn_fulfil_order_stock(bigint,timestamp with time zone)",
)
TRANSACTION_COLUMNS = {
    ("orders", "order_number"): "character varying",
    ("orders", "status"): "USER-DEFINED",
    ("orders", "placed_at"): "timestamp with time zone",
    ("orders", "order_total"): "numeric",
    ("order_items", "line_total"): "numeric",
    ("order_items", "vat_amount"): "numeric",
    ("payment_attempts", "attempt_number"): "smallint",
    ("payment_attempts", "status"): "USER-DEFINED",
    ("payment_attempts", "provider_reference"): "character varying",
    ("inventory_reservations", "expires_at"): "timestamp with time zone",
    ("inventory_reservations", "closed_at"): "timestamp with time zone",
}


class TransactionConflictError(RuntimeError):
    def __init__(self, progress: "TransactionProgress"):
        super().__init__(f"{len(progress.conflicts) + len(progress.blocking)} conflicts with the database; "
                         "nothing was written.")
        self.progress = progress


class PlanChangedError(RuntimeError):
    """The database changed between the confirmed plan and the write."""


class LoaderBusyError(PreconditionError):
    """Another transaction load holds the advisory lock."""


# OutcomeMismatchError (a unit of work gave a different result than planned; that transaction
# was rolled back) is shared by all units of work: nordiccart.units.base, imported above.


class VerificationError(RuntimeError):
    """After the run, the database does not hold exactly the planned transactions."""


class LoadInterrupted(RuntimeError):
    """Raised by the stop_after test hook after the given number of committed units of work."""


@dataclass
class OrderState:
    order: dict
    items: list[dict] = field(default_factory=list)
    attempts: dict[int, dict] = field(default_factory=dict)
    reservations: list[dict] = field(default_factory=list)
    sales: list[dict] = field(default_factory=list)


@dataclass
class TransactionProgress:
    orders: int = 0
    orders_present: int = 0
    events_total: int = 0
    done: list[Event] = field(default_factory=list)
    remaining: list[Event] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    later_movements: int = 0          # later movements (receipts, transfers, sales) dated after the plan's last event
    later_units: int = 0              # their net quantity change
    later: LaterActivity = field(default_factory=LaterActivity)   # everything outside the pilot (S1)

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.blocking

    def counts(self) -> tuple[int, int]:
        return self.orders - self.orders_present, len(self.remaining)

    def remaining_by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.remaining:
            out[e.kind] = out.get(e.kind, 0) + 1
        return out

    def lines(self) -> list[str]:
        head = f"{'':<30}{'planned':>9}{'present':>9}{'to write':>10}"
        return [
            head, "-" * len(head),
            f"{'orders':<30}{self.orders:>9}{self.orders_present:>9}{self.orders - self.orders_present:>10}",
            f"{'units of work (transactions)':<30}{self.events_total:>9}{len(self.done):>9}{len(self.remaining):>10}",
            "to run by kind: " + (", ".join(f"{k} {v}" for k, v in self.remaining_by_kind().items()) or "nothing"),
            "later activity outside the pilot (identity and integrity checked, counted in the stock check): "
            + self.later.summary(),
            f"conflicts: {len(self.conflicts)}   blocking: {len(self.blocking)}",
        ]


@dataclass
class TransactionResult:
    events_run: int
    orders_placed: int
    by_kind: dict[str, int]
    verified_orders: int
    change_log_rows: int


# --------------------------------------------------------------------------- preconditions
def check_transaction_database(conn: psycopg.Connection, master: MasterDataset, inventory: OpeningInventory) -> dict:
    """Migrations 001-007, master data and opening inventory exactly as generated, V006/V007 objects present."""
    info = check_inventory_database(conn, master)
    inv_plan = build_inventory_plan(conn, inventory)
    if inv_plan.conflicts or inv_plan.total_to_insert:
        raise PreconditionError(
            "The opening inventory in the database is not the one this pilot is built from "
            f"({len(inv_plan.balances_to_insert)} listings and {len(inv_plan.movements_to_insert)} opening balances "
            f"missing, {len(inv_plan.conflicts)} conflicts). Run python/run_opening_inventory.py --apply first.")
    rows = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name IN ('orders', 'order_items', 'payment_attempts', "
        "'inventory_reservations')").fetchall()
    actual = {(r[0], r[1]): r[2] for r in rows}
    problems = [f"{t}.{c} is {actual.get((t, c), 'missing')}, expected {d}"
                for (t, c), d in TRANSACTION_COLUMNS.items() if actual.get((t, c)) != d]
    for signature in REQUIRED_FUNCTIONS:
        if conn.execute("SELECT to_regprocedure(%s)", (f"public.{signature}",)).fetchone()[0] is None:
            problems.append(f"function {signature} is missing")
    if problems:
        raise PreconditionError("The order and inventory objects differ from migrations V006 and V007: " + "; ".join(problems))
    return info


# --------------------------------------------------------------------------- reading the database
def pilot_scope(plan: TransactionPlan) -> PilotScope:
    """What this loader verifies itself: the plan's orders, at the plan's 320 listings, up to its last event."""
    return PilotScope(
        name="the pilot transactions",
        boundary=plan.last_event_at() or datetime(1, 1, 1, tzinfo=timezone.utc),
        order_numbers=frozenset(o.order_number for o in plan.orders),
        last_order_sequence=max((o.sequence for o in plan.orders), default=0),
        listings=frozenset(plan.opening_stock),
    )


def _fetch_state(conn: psycopg.Connection, numbers: list[str]) -> tuple[dict[str, OrderState], dict]:
    """The pilot's own orders with all their rows (scoped by order number), and the stock balances."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT o.order_id, o.order_number, c.customer_number, o.channel::text AS channel,
                   sl.location_code AS store_code, fl.location_code AS fulfilment_location_code,
                   o.country_code, o.currency_code, o.status::text AS status, o.cancellation_reason,
                   o.items_total, o.shipping_fee, o.shipping_vat_amount, o.order_total,
                   o.shipping_address_line1, o.shipping_postal_code, o.shipping_city,
                   o.placed_at, o.confirmed_at, o.fulfilled_at, o.cancelled_at
              FROM orders o
              LEFT JOIN customers c ON c.customer_id = o.customer_id
              LEFT JOIN stores s ON s.store_id = o.store_id
              LEFT JOIN stock_locations sl ON sl.stock_location_id = s.stock_location_id
              JOIN stock_locations fl ON fl.stock_location_id = o.fulfilment_location_id
             WHERE o.order_number = ANY(%s::text[])""", (numbers,))
        states = {r["order_number"]: OrderState(order=r) for r in cur.fetchall()}
        by_id = {s.order["order_id"]: s for s in states.values()}
        ids = list(by_id)
        cur.execute("""
            SELECT i.order_id, i.line_number, p.sku, i.quantity, i.unit_price, i.unit_cost, i.discount_percent,
                   i.discount_amount, i.line_total, i.vat_rate, i.vat_amount, i.promotion_id
              FROM order_items i JOIN products p ON p.product_id = i.product_id
             WHERE i.order_id = ANY(%s::bigint[])
             ORDER BY i.order_id, i.line_number""", (ids,))
        for r in cur.fetchall():
            by_id[r["order_id"]].items.append(r)
        cur.execute("""
            SELECT order_id, attempt_number, payment_method::text AS payment_method, payment_provider, amount,
                   status::text AS status, failure_reason, provider_reference, attempted_at, authorized_at,
                   captured_at, failed_at
              FROM payment_attempts
             WHERE order_id = ANY(%s::bigint[])""", (ids,))
        for r in cur.fetchall():
            by_id[r["order_id"]].attempts[r["attempt_number"]] = r
        cur.execute("""
            SELECT i.order_id, i.line_number, l.location_code, p.sku, r.quantity, r.status::text AS status,
                   r.reserved_at, r.expires_at, r.closed_at
              FROM inventory_reservations r
              JOIN order_items i ON i.order_item_id = r.order_item_id
              JOIN stock_locations l ON l.stock_location_id = r.stock_location_id
              JOIN products p ON p.product_id = r.product_id
             WHERE i.order_id = ANY(%s::bigint[])""", (ids,))
        for r in cur.fetchall():
            by_id[r["order_id"]].reservations.append(r)
        cur.execute("""
            SELECT i.order_id, i.line_number, l.location_code, p.sku, m.quantity_change, m.occurred_at
              FROM inventory_movements m
              JOIN order_items i ON i.order_item_id = m.order_item_id
              JOIN stock_locations l ON l.stock_location_id = m.stock_location_id
              JOIN products p ON p.product_id = m.product_id
             WHERE m.movement_type = 'sale' AND i.order_id = ANY(%s::bigint[])""", (ids,))
        for r in cur.fetchall():
            by_id[r["order_id"]].sales.append(r)
        cur.execute("""
            SELECT l.location_code, p.sku, b.quantity_on_hand, b.quantity_reserved
              FROM inventory_balances b
              JOIN stock_locations l ON l.stock_location_id = b.stock_location_id
              JOIN products p ON p.product_id = b.product_id""")
        balances = {(r["location_code"], r["sku"]): (r["quantity_on_hand"], r["quantity_reserved"]) for r in cur.fetchall()}
    return states, balances


# --------------------------------------------------------------------------- comparing one order
HEADER = ("customer_number", "channel", "store_code", "fulfilment_location_code", "country_code", "currency_code",
          "items_total", "shipping_fee", "shipping_vat_amount", "order_total", "shipping_address_line1",
          "shipping_postal_code", "shipping_city", "placed_at")
LINE = ("line_number", "sku", "quantity", "unit_price", "unit_cost", "discount_percent", "discount_amount",
        "line_total", "vat_rate", "vat_amount")


def _done_flags(order: PlannedOrder, events: list[Event], s: OrderState) -> list[bool]:
    status = s.order["status"]
    flags = []
    for e in events:
        if e.kind == "place":
            flags.append(True)
        elif e.kind == "reserve":
            flags.append(bool(s.reservations) if order.reserve_outcome == "reserved" else status == "cancelled")
        elif e.kind in ("cancel_out_of_stock", "customer_cancel", "expire"):
            flags.append(status == "cancelled")
        elif e.kind == "payment_start":
            flags.append(e.attempt_number in s.attempts)
        elif e.kind == "payment_result":
            a = s.attempts.get(e.attempt_number)
            flags.append(a is not None and a["status"] != "pending")
        elif e.kind == "fulfil":
            flags.append(status == "fulfilled")
    return flags


def _expected_after(order: PlannedOrder, events: list[Event], k: int) -> dict:
    """Order status, timestamps and reservation status after the first k events of the order."""
    done = events[:k]
    kinds = [e.kind for e in done]
    status, reservation = "placed", None
    if "reserve" in kinds and order.reserve_outcome == "reserved":
        status, reservation = "stock_reserved", "active"
    results = [e.attempt_number for e in done if e.kind == "payment_result"]
    captured = order.captured_attempt
    if captured and captured.attempt_number in results:
        status = "confirmed"
    if order.final_status == "payment_failed" and order.attempts and order.attempts[-1].attempt_number in results:
        status, reservation = "payment_failed", "released"
    if "cancel_out_of_stock" in kinds:
        status = "cancelled"
    if "customer_cancel" in kinds:
        status, reservation = "cancelled", "released"
    if "expire" in kinds:
        status, reservation = "cancelled", "expired"
    if "fulfil" in kinds:
        status, reservation = "fulfilled", "consumed"
    return {
        "status": status,
        "reservation": reservation,
        "cancellation_reason": order.cancellation_reason if status == "cancelled" else None,
        "confirmed_at": order.confirmed_at if status in ("confirmed", "fulfilled") else None,
        "fulfilled_at": order.fulfilled_at if status == "fulfilled" else None,
        "cancelled_at": order.cancelled_at if status in ("cancelled", "payment_failed") else None,
        "starts": {e.attempt_number for e in done if e.kind == "payment_start"},
        "results": set(results),
    }


def _order_progress(order: PlannedOrder, s: OrderState | None) -> tuple[int, list[str]]:
    """How many of the order's events the database already holds, and any differences from the plan."""
    events = order.events()
    if s is None:
        return 0, []
    label = order.order_number
    planned = {k: getattr(order, k) for k in HEADER}
    diff = [f"{k}: database={s.order[k]!r} plan={planned[k]!r}" for k in HEADER if s.order[k] != planned[k]]
    lines_db = [{k: r[k] for k in LINE} for r in s.items]
    lines_plan = [{k: getattr(x, k) for k in LINE} for x in order.lines]
    if lines_db != lines_plan or any(r["promotion_id"] is not None for r in s.items):
        diff.append("order lines differ")
    if diff:
        return 0, [f"{label}: the order exists with other values: " + "; ".join(diff[:4])]

    flags = _done_flags(order, events, s)
    k = flags.index(False) if False in flags else len(flags)
    if any(flags[k:]):
        return k, [f"{label}: steps are recorded out of order ({', '.join(e.label() for e, f in zip(events, flags) if f)})"]

    want = _expected_after(order, events, k)
    problems = [f"{c}: database={s.order[c]!r} plan={want[c]!r}"
                for c in ("status", "cancellation_reason", "confirmed_at", "fulfilled_at", "cancelled_at")
                if s.order[c] != want[c]]
    if set(s.attempts) != want["starts"]:
        problems.append(f"payment attempts {sorted(s.attempts)} expected {sorted(want['starts'])}")
    for a in order.attempts:
        db = s.attempts.get(a.attempt_number)
        if db is None:
            continue
        terms = {"payment_method": a.payment_method, "payment_provider": a.payment_provider, "amount": a.amount,
                 "attempted_at": a.attempted_at}
        if a.attempt_number in want["results"]:
            terms.update(status=a.outcome, authorized_at=a.authorized_at, captured_at=a.captured_at,
                         failed_at=a.failed_at, failure_reason=a.failure_reason, provider_reference=a.provider_reference)
        else:
            terms.update(status="pending", provider_reference=None)
        problems += [f"attempt {a.attempt_number} {c}: database={db[c]!r} plan={v!r}" for c, v in terms.items() if db[c] != v]
    if want["reservation"] is None:
        if s.reservations:
            problems.append(f"{len(s.reservations)} reservations, none expected yet")
    else:
        closed = order.reservation_closed_at if want["reservation"] != "active" else None
        expected = sorted((x.line_number, order.fulfilment_location_code, x.sku, x.quantity, want["reservation"],
                           order.reserve_at, order.reservation_expires_at, closed) for x in order.lines)
        found = sorted((r["line_number"], r["location_code"], r["sku"], r["quantity"], r["status"], r["reserved_at"],
                        r["expires_at"], r["closed_at"]) for r in s.reservations)
        if found != expected:
            problems.append("reservations differ from the plan")
    sales_expected = sorted((x.line_number, order.fulfilment_location_code, x.sku, -x.quantity, order.fulfilled_at)
                            for x in order.lines) if want["status"] == "fulfilled" else []
    sales_found = sorted((r["line_number"], r["location_code"], r["sku"], r["quantity_change"], r["occurred_at"])
                         for r in s.sales)
    if sales_found != sales_expected:
        problems.append("sale movements differ from the plan")
    if problems:
        return k, [f"{label}: " + "; ".join(problems[:4])]
    return k, []


# --------------------------------------------------------------------------- planning
def build_progress(conn: psycopg.Connection, plan: TransactionPlan) -> TransactionProgress:
    scope = pilot_scope(plan)
    states, balances = _fetch_state(conn, sorted(scope.order_numbers))
    later = read_later_activity(conn, scope)            # every record outside the pilot, checked (S1)
    progress = TransactionProgress(orders=len(plan.orders), later=later)
    progress.later_movements, progress.later_units = later.movements, later.net_units
    progress.blocking += later.problems

    done_count: dict[str, int] = {}
    for order in plan.orders:
        state = states.get(order.order_number)
        if state is not None:
            progress.orders_present += 1
        k, conflicts = _order_progress(order, state)
        done_count[order.order_number] = k
        progress.conflicts += conflicts

    events = plan.events()
    progress.events_total = len(events)
    position = {o.order_number: 0 for o in plan.orders}
    seen_open = None
    for e in events:
        order = plan.order(e.order_number)
        index = position[e.order_number]
        position[e.order_number] += 1
        is_done = index < done_count[e.order_number]
        (progress.done if is_done else progress.remaining).append(e)
        no_effect = e.kind == "reserve" and order.reserve_outcome == "out_of_stock"
        if not is_done and not no_effect and seen_open is None:
            seen_open = e
        elif is_done and not no_effect and seen_open is not None and not progress.conflicts:
            progress.conflicts.append(
                f"{e.order_number}: {e.label()} is recorded, but the earlier step {seen_open.order_number} "
                f"{seen_open.label()} is not; the database does not hold a prefix of this plan")

    if later.exists and progress.remaining:
        progress.blocking.append(
            f"later activity exists ({later.orders} orders, {later.movements} movements after the plan's last "
            f"event), but {len(progress.remaining)} planned steps are not in the database; writing them now would "
            "break business-time order")

    if not progress.conflicts:
        # The pilot's replay at its 320 listings, plus the accepted later activity at those listings.
        expected = expected_stock(plan.replay(progress.done).snapshot(), scope, later)
        wrong = [f"{loc}/{sku}: database {balances.get((loc, sku))} plan {want}"
                 for (loc, sku), want in expected.items() if balances.get((loc, sku), (0, 0)) != want]
        if wrong:
            progress.blocking.append(f"{len(wrong)} stock balances differ from the opening stock plus the recorded "
                                     f"steps: {'; '.join(wrong[:3])}")
    return progress


def plan_transactions(conn: psycopg.Connection, plan: TransactionPlan) -> TransactionProgress:
    """Read-only comparison with the database."""
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return build_progress(conn, plan)


# --------------------------------------------------------------------------- verification
def _verify(conn: psycopg.Connection, plan: TransactionPlan) -> tuple[int, list[str]]:
    progress = build_progress(conn, plan)
    problems = progress.conflicts + progress.blocking
    if progress.remaining:
        problems.append(f"{len(progress.remaining)} planned steps are not in the database")
    bad = conn.execute(LEDGER_IDENTITY_SQL).fetchone()[0]          # every listing, not only the pilot's
    if bad:
        problems.append(f"{bad} balances break the ledger identity")
    return len(plan.orders), problems


def verify_transactions(conn: psycopg.Connection, plan: TransactionPlan) -> tuple[int, list[str]]:
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return _verify(conn, plan)


# --------------------------------------------------------------------------- the units of work
# The units themselves live in nordiccart.units (shared with the combined engine, S6). The names
# below are the loader's entry points; _run_event looks them up at call time.
_Ids = Ids
_place = order_units.place
_reserve = order_units.reserve
_cancel_out_of_stock = order_units.cancel_out_of_stock
_payment_start = order_units.payment_start
_lock_order = order_units.lock_order
_payment_result = order_units.payment_result
_customer_cancel = order_units.customer_cancel
_expire = order_units.expire
_fulfil = order_units.fulfil


def _run_event(conn: psycopg.Connection, ids: Ids, plan: TransactionPlan, e: Event) -> str:
    o: PlannedOrder = plan.order(e.order_number)

    def work() -> str:
        if e.kind == "place":
            return _place(conn, ids, o)
        if e.kind == "reserve":
            return _reserve(conn, ids, o)
        if e.kind == "cancel_out_of_stock":
            return _cancel_out_of_stock(conn, ids, o)
        if e.kind == "payment_start":
            return _payment_start(conn, ids, o, e.attempt_number)
        if e.kind == "payment_result":
            return _payment_result(conn, ids, o, e.attempt_number)
        if e.kind == "customer_cancel":
            return _customer_cancel(conn, ids, o)
        if e.kind == "expire":
            return _expire(conn, ids, o)
        if e.kind == "fulfil":
            return _fulfil(conn, ids, o)
        raise ValueError(f"unknown event kind {e.kind}")

    return run_unit(conn, work, f"{e.order_number} {e.label()}", log, rolled_back="rolled back (out_of_stock)")


def apply_transactions(conn: psycopg.Connection, plan: TransactionPlan,
                       confirmed: TransactionProgress | None = None, stop_after: int | None = None) -> TransactionResult:
    """Run every remaining unit of work in business-time order, then verify the whole pilot."""
    key = try_session_lock(conn, ADVISORY_LOCK_KEY)
    if key is None:
        raise LoaderBusyError("Another transaction load is running against this database (advisory lock held).")
    activity = try_session_lock(conn, ACTIVITY_LOCK)           # S6: never write while the activity engine runs
    if activity is None:
        release_session_lock(conn, key)
        raise LoaderBusyError(ACTIVITY_BUSY)
    try:
        progress = build_progress(conn, plan)                 # again, now under the lock
        if not progress.ok:
            raise TransactionConflictError(progress)
        if confirmed is not None and progress.counts() != confirmed.counts():
            raise PlanChangedError("The database changed after the plan was shown; run again to see the new plan.")
        changes_before = conn.execute("SELECT count(*) FROM change_log").fetchone()[0]
        ids = Ids(conn)
        by_kind: dict[str, int] = {}
        for count, e in enumerate(progress.remaining, start=1):
            if stop_after is not None and count > stop_after:
                raise LoadInterrupted(f"stopped after {stop_after} units of work (test hook)")
            result = _run_event(conn, ids, plan, e)
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
            log.info("%s UTC %-16s %-26s %s", e.at.strftime("%Y-%m-%d %H:%M:%S"), e.order_number, e.label(), result)
        checked, problems = _verify(conn, plan)
        if problems:
            raise VerificationError(f"{len(problems)} problems after the run: " + "; ".join(problems[:5]))
        changes = conn.execute("SELECT count(*) FROM change_log").fetchone()[0] - changes_before
        return TransactionResult(
            events_run=len(progress.remaining),
            orders_placed=by_kind.get("place", 0),
            by_kind=by_kind,
            verified_orders=checked,
            change_log_rows=changes,
        )
    finally:
        release_session_lock(conn, activity)
        release_session_lock(conn, key)
