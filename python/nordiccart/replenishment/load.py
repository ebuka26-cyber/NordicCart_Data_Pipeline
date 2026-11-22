"""Loading the replenishment pilot through V007 (DBML unit of work U7, plus the R0 revision).

How it writes
  * Every unit of work runs as its own transaction, in business-time order:
        R0  lock the warehouse's balance rows FOR UPDATE in (stock_location_id, product_id)
            order, then a guarded UPDATE ... SET reorder_point = new WHERE reorder_point = old
        R1  lock the warehouse's balance rows in lock order, check storage capacity, insert one
            purchase_receipt per line (external_reference = <PO number>/DN<n>)
        T1  lock the source warehouse's balance rows in lock order, check on hand - reserved
            for every line, insert one transfer_out per line (external_reference = TR-...)
        T2  lock the destination store's balance rows in lock order, find each transfer_out by
            its business key, insert the transfer_in with related_movement_id and the same reference
    Stock quantities are never written directly: the V007 triggers move them, and the V007
    CHECKs and guards run on every insert (no negative stock, no reserved units moved,
    arrival = dispatch quantity at another location, one arrival per dispatch).
  * Each result is compared with the plan; a different result rolls that transaction back
    and stops the run (OutcomeMismatchError).
  * Retries: SQLSTATE 40001, 40P01 and 55P03 up to 3 times with jittered backoff; 23514 and
    23503 never (business rejections). 23505 on an idempotency index means the step already
    happened: the state is read again and the step counts as done when it matches the plan.

Why a repeat or resumed run cannot duplicate receipts or transfers
  * Every planned movement is matched on its business key: (purchase_receipt, warehouse, sku,
    delivery reference), (transfer_out, warehouse, sku, transfer reference) and, for an
    arrival, the transfer_in pointing at that transfer_out. Reorder points are matched on
    (warehouse, sku) against their old and new values.
  * From that the loader works out which units of work are done, compares every stored value
    with the plan (a difference is a CONFLICT and nothing is written), requires the done ones
    to be a prefix of the plan in business-time order, and requires every balance to equal the
    stock after the pilot replayed through exactly those units of work. Only the rest runs.
  * The database refuses doubles by itself: uq_inventory_movements_document_line (receipts and
    dispatches) and uq_inventory_movements_one_transfer_in_per_out (arrivals).
  * Preconditions: master data, opening inventory and ALL 100 pilot units of work present and
    equal to their plans; no adjustment, write-off or return movement; no receipt or transfer
    dated at or before the pilot transactions' last event.
  * A session advisory lock stops two replenishment loaders from running at the same time.

Records outside this pilot (Phase 3, step 5, stage S1)
  * Own records: the 20 pilot orders (verified by the transaction loader, a precondition),
    the receipts and transfers matched to this plan, and the reorder points of R0.
  * Every other order or movement is foreign. It is accepted as LATER ACTIVITY only when it
    is dated strictly after this plan's last event (2024-12-02 12:46:04 UTC for the pilot)
    and passes the identity and integrity rules of nordiccart.activity.scope (order
    numbering, document references that do not reuse a pilot PO or TR number, consistent
    orders and transfer pairs, simulated movement types; exception E2 for opening balances
    at listings outside the pilot). Orders, receipts or transfers inside this pilot's window
    that are not its own block the run, as before.
  * Later activity is accepted only when this plan is complete; its movements and active
    reservations at the pilot's listings are added to the expected stock.
  * Without V009, later reorder point changes are not covered: R0 is compared with the current
    values, so a later change is a conflict.

V009 (stage S4: decision C18 and exception E3), only when the migration is applied
  * R0 is verified from the durable revision history (reorder_point_revisions, kind pilot_r0,
    reference R0-<warehouse>-<date>): old value, new value and business time per listing. The
    current reorder point may then differ through LATER revisions (monthly review, S6): they are
    accepted when dated after this plan's last event and chained from the pilot's value, and the
    current value must equal the listing's latest revision.
  * The pilot's purchase orders are documents (purchase_orders / purchase_order_lines): every
    order with a recorded delivery must exist with the planned warehouse, order time and lines;
    the received quantities are derived by the database from the receipts.
  * A pilot loaded before V009 has neither: they are DOCUMENTS TO WRITE. --apply writes them in
    one transaction after any remaining units of work (backfill): the 4 purchase orders with their
    lines and the 69 R0 revision rows. No movement is written or changed; the V009 triggers link
    the existing receipts. A fresh load on V009 writes the same documents inside its units of work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row

from ..activity.scope import LaterActivity, PilotScope, expected_stock, read_later_activity
from ..inventory.model import OpeningInventory
from ..master_data.load import PreconditionError
from ..master_data.model import MasterDataset
from ..transactions import load as tx_load
from ..transactions.model import TransactionPlan
from ..units import stock as stock_units
from ..units.base import (
    ACTIVITY_BUSY, ACTIVITY_LOCK, LEDGER_IDENTITY_SQL, Ids, OutcomeMismatchError, has_activity_control,
    release_session_lock, run_unit, try_session_lock,
)
from .model import Event, ReplenishmentPlan

log = logging.getLogger("nordiccart.replenishment.load")

ADVISORY_LOCK_KEY = "nordiccart.replenishment"
REPLENISHMENT_TYPES = ("purchase_receipt", "transfer_out", "transfer_in")
REQUIRED_INDEXES = ("uq_inventory_movements_document_line", "uq_inventory_movements_one_transfer_in_per_out")


class ReplenishmentConflictError(RuntimeError):
    def __init__(self, progress: "ReplenishmentProgress"):
        super().__init__(f"{len(progress.conflicts) + len(progress.blocking)} conflicts with the database; "
                         "nothing was written.")
        self.progress = progress


class PlanChangedError(RuntimeError):
    """The database changed between the confirmed plan and the write."""


class LoaderBusyError(PreconditionError):
    """Another replenishment load holds the advisory lock."""


# OutcomeMismatchError (a unit of work gave a different result than planned; that transaction
# was rolled back) is shared by all units of work: nordiccart.units.base, imported above.


class VerificationError(RuntimeError):
    """After the run, the database does not hold exactly the planned replenishment."""


class LoadInterrupted(RuntimeError):
    """Raised by the stop_after test hook after the given number of committed units of work."""


@dataclass
class ReplenishmentProgress:
    events_total: int = 0
    done: list[Event] = field(default_factory=list)
    remaining: list[Event] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    later: LaterActivity = field(default_factory=LaterActivity)   # everything outside this pilot (S1)
    # V009 (S4): None before V009; otherwise the documents of done units still to write, as
    # ("purchase_order", number) or ("reorder_point_revision", warehouse)
    documents_missing: list[tuple[str, str]] | None = None
    documents_summary: str = ""
    later_revisions: int = 0

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.blocking

    @property
    def documents_to_write(self) -> int:
        return len(self.documents_missing or [])

    def counts(self) -> tuple[int, int]:
        return len(self.done), len(self.remaining)

    def remaining_by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.remaining:
            out[e.label()] = out.get(e.label(), 0) + 1
        return out

    def lines(self) -> list[str]:
        head = f"{'':<30}{'planned':>9}{'present':>9}{'to write':>10}"
        out = [
            head, "-" * len(head),
            f"{'units of work (transactions)':<30}{self.events_total:>9}{len(self.done):>9}{len(self.remaining):>10}",
            "to run by kind: " + (", ".join(f"{k} {v}" for k, v in self.remaining_by_kind().items()) or "nothing"),
            "later activity outside this pilot (identity and integrity checked, counted in the stock check): "
            + self.later.summary(),
        ]
        if self.documents_missing is not None:
            out.append(f"documents (V009): {self.documents_summary}; later reorder point revisions "
                       f"{self.later_revisions}; to write {self.documents_to_write}"
                       + (" (" + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in self.documents_missing) + ")"
                          if self.documents_missing else ""))
        out.append(f"conflicts: {len(self.conflicts)}   blocking: {len(self.blocking)}")
        return out


@dataclass
class ReplenishmentResult:
    events_run: int
    by_kind: dict[str, int]
    verified_events: int
    change_log_rows: int
    documents_written: int = 0              # V009 backfill (purchase orders and R0 revision sets)


# --------------------------------------------------------------------------- preconditions
def check_replenishment_database(conn: psycopg.Connection, master: MasterDataset, inventory: OpeningInventory,
                                 pilot: TransactionPlan) -> dict:
    """V001-V007, master data and opening inventory as generated, and the pilot transactions complete."""
    info = tx_load.check_transaction_database(conn, master, inventory)
    missing = [name for name in REQUIRED_INDEXES
               if conn.execute("SELECT to_regclass(%s)", (f"public.{name}",)).fetchone()[0] is None]
    if missing:
        raise PreconditionError(f"V007 idempotency indexes are missing: {', '.join(missing)}")
    progress = tx_load.plan_transactions(conn, pilot)
    if not progress.ok:
        raise PreconditionError("The pilot transactions in the database do not match their plan ("
                                + "; ".join((progress.conflicts + progress.blocking)[:3])
                                + "). Check them with python/run_transactions.py --plan.")
    if progress.remaining:
        raise PreconditionError(f"The pilot transactions are not complete: {len(progress.remaining)} of "
                                f"{progress.events_total} units of work are missing. Replenishment starts after "
                                "the pilot; run python/run_transactions.py --apply first.")
    info["pilot_units_of_work"] = progress.events_total
    return info


# --------------------------------------------------------------------------- reading the database
def pilot_scope(plan: ReplenishmentPlan, own_movement_ids) -> PilotScope:
    """What this loader verifies itself (with the pilot transactions it is built on), up to its last event."""
    if not plan.pilot_order_numbers:
        raise ValueError("the replenishment plan does not name the pilot orders it is built on; "
                         "generate it with generate_replenishment")
    return PilotScope(
        name="the replenishment pilot",
        boundary=plan.last_event_at(),
        order_numbers=frozenset(plan.pilot_order_numbers),
        last_order_sequence=plan.pilot_last_sequence,
        listings=frozenset(plan.base_stock),
        own_movement_ids=frozenset(own_movement_ids),
        own_documents=frozenset(plan.document_numbers()),
    )


def _fetch_state(conn: psycopg.Connection) -> tuple[list[dict], dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT m.movement_id, m.movement_type::text AS movement_type, l.location_code, p.sku,
                   m.quantity_change, m.occurred_at, m.external_reference, m.related_movement_id
              FROM inventory_movements m
              JOIN stock_locations l ON l.stock_location_id = m.stock_location_id
              JOIN products p ON p.product_id = m.product_id
             WHERE m.movement_type::text = ANY(%s)
             ORDER BY m.movement_id""", (list(REPLENISHMENT_TYPES),))
        rows = cur.fetchall()
        cur.execute("""
            SELECT l.location_code, p.sku, b.quantity_on_hand, b.quantity_reserved, b.reorder_point
              FROM inventory_balances b
              JOIN stock_locations l ON l.stock_location_id = b.stock_location_id
              JOIN products p ON p.product_id = b.product_id""")
        balances = {(r["location_code"], r["sku"]): (r["quantity_on_hand"], r["quantity_reserved"], r["reorder_point"])
                    for r in cur.fetchall()}
    return rows, balances


@dataclass
class _Documents:
    """V009 documents: purchase orders with their lines, and the reorder point revision history."""
    orders: dict        # number -> {"warehouse", "ordered_at", "status", "received_at", "lines": {n: (sku, reason, ordered, received)}}
    revisions: dict     # (location_code, sku) -> revision rows in business-time order


def _fetch_documents(conn: psycopg.Connection) -> _Documents | None:
    """None before V009."""
    if not has_activity_control(conn):
        return None
    orders: dict = {}
    revisions: dict = {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT o.purchase_order_number, l.location_code, o.ordered_at, o.status::text AS status, o.received_at,
                   pl.line_number, p.sku, pl.order_reason::text AS order_reason, pl.quantity_ordered, pl.quantity_received
              FROM purchase_orders o
              JOIN stock_locations l ON l.stock_location_id = o.stock_location_id
              LEFT JOIN purchase_order_lines pl ON pl.purchase_order_id = o.purchase_order_id
              LEFT JOIN products p ON p.product_id = pl.product_id
             ORDER BY o.purchase_order_number, pl.line_number""")
        for r in cur.fetchall():
            o = orders.setdefault(r["purchase_order_number"], {
                "warehouse": r["location_code"], "ordered_at": r["ordered_at"], "status": r["status"],
                "received_at": r["received_at"], "lines": {}})
            if r["line_number"] is not None:
                o["lines"][r["line_number"]] = (r["sku"], r["order_reason"], r["quantity_ordered"], r["quantity_received"])
        cur.execute("""
            SELECT l.location_code, p.sku, r.revision_kind::text AS revision_kind, r.effective_at,
                   r.old_reorder_point, r.new_reorder_point, r.document_reference
              FROM reorder_point_revisions r
              JOIN stock_locations l ON l.stock_location_id = r.stock_location_id
              JOIN products p ON p.product_id = r.product_id
             ORDER BY r.effective_at, r.reorder_point_revision_id""")
        for r in cur.fetchall():
            revisions.setdefault((r["location_code"], r["sku"]), []).append(r)
    return _Documents(orders, revisions)


@dataclass
class _Index:
    receipts: dict
    outs: dict
    ins_by_out: dict
    balances: dict
    matched: set = field(default_factory=set)
    docs: _Documents | None = None

    @classmethod
    def build(cls, rows: list[dict], balances: dict, docs: _Documents | None = None) -> "_Index":
        receipts, outs, ins = {}, {}, {}
        for r in rows:
            key = (r["location_code"], r["sku"], r["external_reference"])
            if r["movement_type"] == "purchase_receipt":
                receipts[key] = r
            elif r["movement_type"] == "transfer_out":
                outs[key] = r
            else:
                ins[r["related_movement_id"]] = r
        return cls(receipts, outs, ins, balances, docs=docs)

    def history(self, key: tuple[str, str]) -> list[dict]:
        return self.docs.revisions.get(key, []) if self.docs is not None else []

    def r0_row(self, warehouse: str, sku: str, reference: str) -> dict | None:
        return next((h for h in self.history((warehouse, sku)) if h["document_reference"] == reference), None)


def _event_state(plan: ReplenishmentPlan, e: Event, ix: _Index) -> tuple[bool, list[str]]:
    """Is the unit of work in the database (True/False), and the differences from the plan."""
    flags: list[bool] = []
    problems: list[str] = []
    if e.kind == "revise":
        r = plan.revision(e.key)
        for c in r.changes:
            own = ix.r0_row(r.warehouse, c.sku, r.reference)
            if own is not None:                                    # V009: R0 read from the history (C18)
                flags.append(True)
                found = (own["revision_kind"], own["effective_at"], own["old_reorder_point"], own["new_reorder_point"])
                if found != ("pilot_r0", r.at, c.old, c.new):
                    problems.append(f"{r.warehouse}/{c.sku}: revision {r.reference} is {found}, plan "
                                    f"pilot_r0 at {r.at.isoformat()} {c.old} -> {c.new}")
                continue
            bal = ix.balances.get((r.warehouse, c.sku))
            history = ix.history((r.warehouse, c.sku))
            # the listing's value before any (later) recorded revision, else its current value
            value = history[0]["old_reorder_point"] if history else (bal[2] if bal is not None else None)
            if bal is None:
                problems.append(f"{r.warehouse}/{c.sku}: not listed")
            elif value == c.new:
                flags.append(True)
            elif value == c.old:
                flags.append(False)
            else:
                problems.append(f"{r.warehouse}/{c.sku}: reorder point {value}, plan {c.old} -> {c.new}")
    elif e.kind == "receive":
        po, d = plan.delivery(e.key)
        for x in d.lines:
            row = ix.receipts.get((po.warehouse, x.sku, d.reference))
            if row is None:
                flags.append(False)
                continue
            ix.matched.add(row["movement_id"])
            flags.append(True)
            if (row["quantity_change"], row["occurred_at"]) != (x.quantity, d.received_at):
                problems.append(f"{d.reference} {x.sku}: receipt {row['quantity_change']} at {row['occurred_at']}, "
                                f"plan {x.quantity} at {d.received_at}")
    elif e.kind in ("dispatch", "arrive"):
        t = plan.transfer(e.key)
        for x in t.lines:
            out = ix.outs.get((t.source, x.sku, t.reference))
            if e.kind == "dispatch":
                if out is None:
                    flags.append(False)
                    continue
                ix.matched.add(out["movement_id"])
                flags.append(True)
                if (out["quantity_change"], out["occurred_at"]) != (-x.quantity, t.dispatched_at):
                    problems.append(f"{t.reference} {x.sku}: transfer_out {out['quantity_change']} at {out['occurred_at']}, "
                                    f"plan {-x.quantity} at {t.dispatched_at}")
                continue
            arrival = ix.ins_by_out.get(out["movement_id"]) if out is not None else None
            if arrival is None:
                flags.append(False)
                continue
            ix.matched.add(arrival["movement_id"])
            flags.append(True)
            found = (arrival["location_code"], arrival["quantity_change"], arrival["occurred_at"], arrival["external_reference"])
            if found != (t.destination, x.quantity, t.arrived_at, t.reference):
                problems.append(f"{t.reference} {x.sku}: transfer_in {found}, plan "
                                f"{(t.destination, x.quantity, t.arrived_at, t.reference)}")
    if flags and any(flags) and not all(flags):
        problems.append(f"{e.label()} {e.key} is only partly recorded")
    return bool(flags) and all(flags) and not problems, problems


def _check_documents(plan: ReplenishmentPlan, progress: ReplenishmentProgress, ix: _Index) -> None:
    """V009: the pilot's purchase order documents and R0 history against the plan (E3, C18)."""
    docs = ix.docs
    done = {(e.kind, e.key) for e in progress.done}
    missing: list[tuple[str, str]] = []
    present_orders = 0
    for po in plan.purchase_orders:
        delivered = [d for d in po.deliveries if ("receive", d.reference) in done]
        found = docs.orders.get(po.number)
        if found is None:
            if delivered:
                missing.append(("purchase_order", po.number))
            continue
        present_orders += 1
        if (found["warehouse"], found["ordered_at"]) != (po.warehouse, po.ordered_at):
            progress.conflicts.append(f"purchase order {po.number}: {found['warehouse']} ordered at "
                                      f"{found['ordered_at']}, plan {po.warehouse} at {po.ordered_at.isoformat()}")
        want = {x.line_number: (x.sku, x.trigger, x.quantity) for x in po.lines}
        got = {n: v[:3] for n, v in found["lines"].items()}
        if got != want:
            progress.conflicts.append(f"purchase order {po.number}: lines {got}, plan {want}")
            continue
        received: dict[str, int] = {}
        for d in delivered:
            for x in d.lines:
                received[x.sku] = received.get(x.sku, 0) + x.quantity
        got_received = {v[0]: v[3] for v in found["lines"].values()}
        if any(got_received[x.sku] != received.get(x.sku, 0) for x in po.lines):
            progress.conflicts.append(f"purchase order {po.number}: received {got_received}, recorded deliveries {received}")
        complete = len(delivered) == len(po.deliveries)
        want_status = ("received", max(d.received_at for d in po.deliveries)) if complete else None
        if complete and (found["status"], found["received_at"]) != want_status:
            progress.conflicts.append(f"purchase order {po.number}: status {found['status']} at {found['received_at']}, "
                                      f"expected received at {want_status[1].isoformat()}")
    present_revisions = 0
    for r in plan.revisions:
        rows = [c for c in r.changes if ix.r0_row(r.warehouse, c.sku, r.reference) is not None]
        if rows:
            present_revisions += 1
        if ("revise", r.warehouse) not in done:
            if rows:
                progress.conflicts.append(f"{r.reference}: recorded in the revision history but R0 is not applied")
            continue
        if not rows:
            later = [c.sku for c in r.changes if ix.history((r.warehouse, c.sku))]
            if later:
                progress.blocking.append(f"{r.reference}: the R0 history is missing but later revisions exist "
                                         f"({', '.join(later[:3])}); R0 can only be recorded before them")
            else:
                missing.append(("reorder_point_revision", r.warehouse))
        elif len(rows) < len(r.changes):
            progress.conflicts.append(f"{r.reference}: only {len(rows)} of {len(r.changes)} listings are in the "
                                      "revision history")
    progress.documents_missing = missing
    progress.documents_summary = (f"purchase orders {present_orders}/{len(plan.purchase_orders)}, "
                                  f"R0 revisions {present_revisions}/{len(plan.revisions)} warehouses")


def _expected_reorder_point(plan: ReplenishmentPlan, key: tuple[str, str], pilot_value: int, ix: _Index,
                            progress: ReplenishmentProgress) -> int:
    """The listing's reorder point now: the pilot's value, or (V009) its latest LATER revision."""
    own = {r.reference for r in plan.revisions if r.warehouse == key[0]}
    later = [h for h in ix.history(key) if h["document_reference"] not in own]
    if not later:
        return pilot_value
    progress.later_revisions += len(later)
    early = [h for h in later if h["effective_at"] <= plan.last_event_at()]
    if early:
        progress.blocking.append(f"{key[0]}/{key[1]}: reorder point revision {early[0]['document_reference']} is dated "
                                 f"at or before the pilot's last event ({plan.last_event_at().isoformat()})")
    if later[0]["old_reorder_point"] != pilot_value:
        progress.conflicts.append(f"{key[0]}/{key[1]}: the first later revision starts from "
                                  f"{later[0]['old_reorder_point']}, the pilot left {pilot_value}")
    return later[-1]["new_reorder_point"]


# --------------------------------------------------------------------------- planning
def build_progress(conn: psycopg.Connection, plan: ReplenishmentPlan) -> ReplenishmentProgress:
    rows, balances = _fetch_state(conn)
    ix = _Index.build(rows, balances, _fetch_documents(conn))
    progress = ReplenishmentProgress()
    early = [r for r in rows if r["occurred_at"] <= plan.start_after]
    if early:
        progress.blocking.append(
            f"{len(early)} receipts or transfers are dated at or before the pilot's last event "
            f"({plan.start_after.isoformat()}), e.g. {early[0]['external_reference']}; replenishment must come after it")

    events = plan.events()
    progress.events_total = len(events)
    flags = []
    for e in events:
        done, problems = _event_state(plan, e, ix)
        progress.conflicts += problems
        flags.append(done)
    k = flags.index(False) if False in flags else len(flags)
    if any(flags[k:]):
        later = [f"{e.label()} {e.key}" for e, f in zip(events[k:], flags[k:]) if f]
        progress.conflicts.append(f"units of work are recorded out of business-time order: {events[k].label()} "
                                  f"{events[k].key} is missing, but {', '.join(later[:3])} exist")
    progress.done, progress.remaining = events[:k], events[k:]

    # Everything not matched to this plan (and not the pilot transactions) is foreign: accepted only
    # as valid later activity after this plan's last event (S1). Replaces the former blanket refusal.
    scope = pilot_scope(plan, ix.matched)
    later = read_later_activity(conn, scope)
    progress.later = later
    progress.blocking += later.problems
    if later.exists and progress.remaining:
        progress.blocking.append(
            f"later activity exists ({later.orders} orders, {later.movements} movements after the plan's last "
            f"event), but {len(progress.remaining)} planned units of work are not in the database; writing them "
            "now would break business-time order")

    if ix.docs is not None:
        _check_documents(plan, progress, ix)

    if not progress.conflicts:
        ledger, reorder_points = plan.replay(progress.done)
        expected = expected_stock(ledger.snapshot(), scope, later)
        wrong = [f"{loc}/{sku}: database {balances.get((loc, sku), (None, None))[:2]} plan {want}"
                 for (loc, sku), want in expected.items() if balances.get((loc, sku), (0, 0, 0))[:2] != want]
        if wrong:
            progress.blocking.append(f"{len(wrong)} stock balances differ from the stock after the pilot plus the "
                                     f"recorded units of work: {'; '.join(wrong[:3])}")
        wrong = []
        for key, rop in sorted(reorder_points.items()):
            if key not in balances:
                continue
            want = _expected_reorder_point(plan, key, rop, ix, progress) if ix.docs is not None else rop
            if balances[key][2] != want:
                wrong.append(f"{key[0]}/{key[1]}: database {balances[key][2]} plan {want}")
        if progress.later_revisions and progress.remaining:
            progress.blocking.append(f"{progress.later_revisions} later reorder point revisions exist, but "
                                     f"{len(progress.remaining)} planned units of work are not in the database")
        if wrong:
            progress.blocking.append(f"{len(wrong)} reorder points differ from the plan: {'; '.join(wrong[:3])}")
    return progress


def plan_replenishment(conn: psycopg.Connection, plan: ReplenishmentPlan) -> ReplenishmentProgress:
    """Read-only comparison with the database."""
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return build_progress(conn, plan)


# --------------------------------------------------------------------------- verification
def _verify(conn: psycopg.Connection, plan: ReplenishmentPlan) -> tuple[int, list[str]]:
    progress = build_progress(conn, plan)
    problems = progress.conflicts + progress.blocking
    if progress.remaining:
        problems.append(f"{len(progress.remaining)} planned units of work are not in the database")
    if progress.documents_to_write:
        problems.append(f"{progress.documents_to_write} V009 documents are not in the database: {progress.documents_missing}")
    bad = conn.execute(LEDGER_IDENTITY_SQL).fetchone()[0]          # every listing, not only the pilot's
    if bad:
        problems.append(f"{bad} balances break the ledger identity")
    # Conservation of the pilot's own network (S1: scoped; the global identity is script 15):
    # on hand at the pilot's listings + this plan's transfers in transit
    #   = the plan's replay + the accepted later activity at those listings.
    keys = sorted(plan.base_stock)
    on_hand, in_transit = conn.execute("""
        SELECT (SELECT coalesce(sum(b.quantity_on_hand), 0)
                  FROM inventory_balances b
                  JOIN stock_locations l ON l.stock_location_id = b.stock_location_id
                  JOIN products p ON p.product_id = b.product_id
                  JOIN unnest(%s::text[], %s::text[]) AS k(location_code, sku)
                    ON k.location_code = l.location_code AND k.sku = p.sku),
               (SELECT coalesce(-sum(o.quantity_change), 0) FROM inventory_movements o
                 WHERE o.movement_type = 'transfer_out' AND o.external_reference = ANY(%s::text[])
                   AND NOT EXISTS (SELECT 1 FROM inventory_movements i
                                    WHERE i.movement_type = 'transfer_in' AND i.related_movement_id = o.movement_id))
    """, ([k[0] for k in keys], [k[1] for k in keys], [t.reference for t in plan.transfers])).fetchone()
    ledger, _ = plan.replay(progress.done)
    later_units = sum(progress.later.on_hand_change.values())
    if (on_hand, in_transit) != (ledger.total_on_hand() + later_units, ledger.units_in_transit()):
        problems.append(f"conservation (pilot listings): database on hand {on_hand} + in transit {in_transit}, plan "
                        f"{ledger.total_on_hand()} + later activity {later_units:+d} + {ledger.units_in_transit()}")
    return progress.events_total, problems


def verify_replenishment(conn: psycopg.Connection, plan: ReplenishmentPlan) -> tuple[int, list[str]]:
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return _verify(conn, plan)


# --------------------------------------------------------------------------- the units of work
# The units themselves live in nordiccart.units.stock (shared with the combined engine, S6). These
# wrappers resolve this plan's document for a unit-of-work key; messages keep the key as label.
_Ids = Ids
_lock_balances = stock_units.lock_balances


def _revise(conn, ids: Ids, plan: ReplenishmentPlan, key: str) -> str:
    return stock_units.revise_reorder_points(conn, ids, plan.revision(key), key)


def _receive(conn, ids: Ids, plan: ReplenishmentPlan, key: str) -> str:
    po, d = plan.delivery(key)
    return stock_units.receive(conn, ids, po, d, key)


def _dispatch(conn, ids: Ids, plan: ReplenishmentPlan, key: str) -> str:
    return stock_units.dispatch(conn, ids, plan.transfer(key), key)


def _arrive(conn, ids: Ids, plan: ReplenishmentPlan, key: str) -> str:
    return stock_units.arrive(conn, ids, plan.transfer(key), key)


UNITS = {"revise": _revise, "receive": _receive, "dispatch": _dispatch, "arrive": _arrive}


def _already_done(conn: psycopg.Connection, plan: ReplenishmentPlan, e: Event) -> bool:
    rows, balances = _fetch_state(conn)
    done, problems = _event_state(plan, e, _Index.build(rows, balances, _fetch_documents(conn)))
    return done and not problems


def _write_documents(conn, ids: Ids, plan: ReplenishmentPlan, missing: list[tuple[str, str]]) -> str:
    """V009 backfill (E3, C18) in ONE transaction: documents only, no movement."""
    orders = revisions = 0
    for kind, key in missing:
        if kind == "purchase_order":
            po = next(p for p in plan.purchase_orders if p.number == key)
            orders += stock_units.ensure_purchase_order(conn, ids, po, f"backfill {key}")
        else:
            revisions += stock_units.record_reorder_point_revisions(conn, ids, plan.revision(key))
    return f"{orders} purchase orders with their lines, {revisions} R0 revision rows (no movement written)"


def _run_event(conn: psycopg.Connection, ids: Ids, plan: ReplenishmentPlan, e: Event) -> str:
    # 23505 on an idempotency index means the step happened (design retry rule): reload and check.
    return run_unit(conn, lambda: UNITS[e.kind](conn, ids, plan, e.key), f"{e.label()} {e.key}", log,
                    already_done=lambda: _already_done(conn, plan, e))


def apply_replenishment(conn: psycopg.Connection, plan: ReplenishmentPlan,
                        confirmed: ReplenishmentProgress | None = None,
                        stop_after: int | None = None) -> ReplenishmentResult:
    """Run every remaining unit of work in business-time order, then verify the whole replenishment."""
    key = try_session_lock(conn, ADVISORY_LOCK_KEY)
    if key is None:
        raise LoaderBusyError("Another replenishment load is running against this database (advisory lock held).")
    activity = try_session_lock(conn, ACTIVITY_LOCK)           # S6: never write while the activity engine runs
    if activity is None:
        release_session_lock(conn, key)
        raise LoaderBusyError(ACTIVITY_BUSY)
    try:
        progress = build_progress(conn, plan)                 # again, now under the lock
        if not progress.ok:
            raise ReplenishmentConflictError(progress)
        if confirmed is not None and (progress.counts(), progress.documents_to_write) != \
                (confirmed.counts(), confirmed.documents_to_write):
            raise PlanChangedError("The database changed after the plan was shown; run again to see the new plan.")
        changes_before = conn.execute("SELECT count(*) FROM change_log").fetchone()[0]
        ids = Ids(conn)
        by_kind: dict[str, int] = {}
        for count, e in enumerate(progress.remaining, start=1):
            if stop_after is not None and count > stop_after:
                raise LoadInterrupted(f"stopped after {stop_after} units of work (test hook)")
            result = _run_event(conn, ids, plan, e)
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
            log.info("%s UTC %-19s %-34s %s", e.at.strftime("%Y-%m-%d %H:%M:%S"), e.label(), e.key, result)
        documents = 0
        if progress.documents_missing is not None:
            # V009: documents of units done before V009 (the units just run wrote their own)
            backlog = build_progress(conn, plan).documents_missing or []
            if backlog:
                result = run_unit(conn, lambda: _write_documents(conn, ids, plan, backlog), "V009 document backfill", log)
                log.info("%-23s %-19s %-34s %s", "", "V009 backfill (E3)", f"{len(backlog)} documents", result)
                documents = len(backlog)
        checked, problems = _verify(conn, plan)
        if problems:
            raise VerificationError(f"{len(problems)} problems after the run: " + "; ".join(problems[:5]))
        changes = conn.execute("SELECT count(*) FROM change_log").fetchone()[0] - changes_before
        return ReplenishmentResult(events_run=len(progress.remaining), by_kind=by_kind, verified_events=checked,
                                   change_log_rows=changes, documents_written=documents)
    finally:
        release_session_lock(conn, activity)
        release_session_lock(conn, key)
