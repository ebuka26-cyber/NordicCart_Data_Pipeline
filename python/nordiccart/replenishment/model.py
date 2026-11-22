"""Planned replenishment documents and the units of work that load them.

Documents (business keys only; the loader resolves surrogate ids)
  ReorderPointRevision  one per warehouse: new reorder points for its listings (decision D2a)
  TransferDocument      one warehouse -> one store of the same market; one line per product.
                        external_reference = TR-<market>-<year>-<seq>-<destination> on both the
                        transfer_out and the transfer_in (decision D6)
  PurchaseOrder         one warehouse; its lines say WHY they were ordered:
                          reorder_point   inventory position <= reorder point (natural)
                          launch          first stock of a product in its pre-launch window (natural)
                          forced_reorder  pilot scenario: ordered on purpose although the warehouse
                                          is above its reorder point (decision D1)
  Delivery              one supplier delivery note of a purchase order:
                        external_reference = <PO number>/DN<n>; a split delivery has DN1 and DN2

Units of work (each one PostgreSQL transaction, run in business-time order)
  revise    R0  guarded UPDATE of inventory_balances.reorder_point for one warehouse
  receive   R1  purchase_receipt movements of one delivery note
  dispatch  T1  transfer_out movements of one transfer document (stock goes in transit)
  arrive    T2  transfer_in movements of one transfer document (related_movement_id = its transfer_out)
Purchase orders and reviews were plan-only in DBML revision 3. From V009 (stage S4, DBML revision 4)
purchase orders are documents in purchase_orders / purchase_order_lines and R0 is recorded in
reorder_point_revisions: the units of work write them when V009 is present (nordiccart.units.stock),
and the loader backfills them for a pilot loaded before V009 (exception E3, decision C18).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ..master_data.model import canonical
from ..stock.ledger import StockLedger

Key = tuple[str, str]

KIND_RANK = {"revise": 0, "receive": 1, "dispatch": 2, "arrive": 3}
UNIT_OF_WORK = {"revise": "R0 reorder points", "receive": "R1 receipt", "dispatch": "T1 dispatch",
                "arrive": "T2 arrival"}
TRIGGERS = ("reorder_point", "launch", "forced_reorder")


@dataclass(frozen=True)
class ReorderPointChange:
    sku: str
    old: int
    new: int
    echelon_weekly_units: Decimal           # online + the market's stores (planning figure)


@dataclass(frozen=True)
class ReorderPointRevision:
    market: str
    warehouse: str
    at: datetime
    changes: tuple[ReorderPointChange, ...]

    @property
    def reference(self) -> str:
        """Document reference in reorder_point_revisions (V009, decision C18): R0-<warehouse>-<UTC date>.
        A property, not a field: the plan fingerprint is unchanged."""
        return f"R0-{self.warehouse}-{self.at:%Y%m%d}"


@dataclass(frozen=True)
class TransferLine:
    line_number: int
    sku: str
    requested: int                          # S - IP at the review
    quantity: int                           # dispatched (less than requested only when the warehouse is short)
    store_available: int                    # on hand - reserved at the review
    store_position: int                     # inventory position at the review
    reorder_point: int
    order_up_to: int


@dataclass(frozen=True)
class TransferDocument:
    reference: str
    market: str
    source: str                             # warehouse location_code
    destination: str                        # store location_code
    requested_at: datetime                  # review that found the need
    dispatched_at: datetime                 # transfer_out.occurred_at
    arrived_at: datetime                    # transfer_in.occurred_at
    trigger: str                            # reorder_point (stores are never force-filled)
    lines: tuple[TransferLine, ...]

    @property
    def units(self) -> int:
        return sum(x.quantity for x in self.lines)


@dataclass(frozen=True)
class PurchaseOrderLine:
    line_number: int
    sku: str
    quantity: int                           # multiple of the case pack
    trigger: str                            # reorder_point | launch | forced_reorder
    position: int                           # inventory position at the review
    reorder_point: int
    order_up_to: int
    case_pack: int


@dataclass(frozen=True)
class DeliveryLine:
    sku: str
    quantity: int


@dataclass(frozen=True)
class Delivery:
    reference: str                          # PO-SE-2024-000001/DN1
    note: int
    received_at: datetime                   # purchase_receipt.occurred_at
    lines: tuple[DeliveryLine, ...]

    @property
    def units(self) -> int:
        return sum(x.quantity for x in self.lines)


@dataclass(frozen=True)
class PurchaseOrder:
    number: str
    market: str
    warehouse: str
    ordered_at: datetime                    # warehouse review time
    forced: bool                            # pilot scenario, not triggered by the reorder point
    lines: tuple[PurchaseOrderLine, ...]
    deliveries: tuple[Delivery, ...]

    @property
    def units(self) -> int:
        return sum(x.quantity for x in self.lines)


@dataclass(frozen=True)
class Event:
    at: datetime
    kind: str                               # revise | receive | dispatch | arrive
    key: str                                # warehouse code | delivery reference | transfer reference

    @property
    def sort_key(self) -> tuple:
        return self.at, KIND_RANK[self.kind], self.key

    def label(self) -> str:
        return UNIT_OF_WORK[self.kind]


@dataclass(frozen=True)
class ReviewRecord:
    """Plan-only trace of a review (CSV preview and log), never loaded."""
    at: datetime
    kind: str                               # store | warehouse
    market: str
    triggered: int
    note: str


@dataclass
class ReplenishmentPlan:
    profile: str
    seed: int
    start_after: datetime                   # last event of the pilot transactions
    first_review_date: date
    end_date: date
    revisions: list[ReorderPointRevision]
    transfers: list[TransferDocument]
    purchase_orders: list[PurchaseOrder]
    base_stock: dict[Key, tuple[int, int]] = field(repr=False, default_factory=dict)   # after the pilot
    base_reorder_points: dict[Key, int] = field(repr=False, default_factory=dict)      # before R0
    reviews: list[ReviewRecord] = field(repr=False, default_factory=list)
    backorders: list[str] = field(repr=False, default_factory=list)
    # Identity of the pilot transactions this plan is built on (S1: the loader tells the pilot's
    # orders from later orders). Not part of the fingerprint, which covers the documents only.
    pilot_order_numbers: tuple[str, ...] = field(repr=False, default=())
    pilot_last_sequence: int = field(repr=False, default=0)

    def last_event_at(self) -> datetime:
        """Business time of the plan's last unit of work (start_after when there is none)."""
        events = self.events()
        return events[-1].at if events else self.start_after

    def document_numbers(self) -> set[str]:
        """PO-<market>-<year>-<seq> and TR-<market>-<year>-<seq> numbers of this plan's documents."""
        return {p.number for p in self.purchase_orders} | {t.reference[:17] for t in self.transfers}

    # ------------------------------------------------------------- lookups
    def revision(self, warehouse: str) -> ReorderPointRevision:
        return next(r for r in self.revisions if r.warehouse == warehouse)

    def transfer(self, reference: str) -> TransferDocument:
        return next(t for t in self.transfers if t.reference == reference)

    def deliveries(self) -> list[tuple[PurchaseOrder, Delivery]]:
        return [(po, d) for po in self.purchase_orders for d in po.deliveries]

    def delivery(self, reference: str) -> tuple[PurchaseOrder, Delivery]:
        return next((po, d) for po, d in self.deliveries() if d.reference == reference)

    # ------------------------------------------------------------- units of work
    def events(self) -> list[Event]:
        out = [Event(r.at, "revise", r.warehouse) for r in self.revisions]
        out += [Event(d.received_at, "receive", d.reference) for _, d in self.deliveries()]
        for t in self.transfers:
            out.append(Event(t.dispatched_at, "dispatch", t.reference))
            out.append(Event(t.arrived_at, "arrive", t.reference))
        return sorted(out, key=lambda e: e.sort_key)

    def apply(self, ledger: StockLedger, reorder_points: dict[Key, int], event: Event) -> None:
        """Apply one unit of work to a stock ledger and a reorder point map (as V007 would)."""
        if event.kind == "revise":
            r = self.revision(event.key)
            for c in r.changes:
                reorder_points[(r.warehouse, c.sku)] = c.new
        elif event.kind == "receive":
            po, d = self.delivery(event.key)
            for x in d.lines:
                ledger.receive(po.warehouse, x.sku, x.quantity)
        elif event.kind == "dispatch":
            t = self.transfer(event.key)
            for x in t.lines:
                ledger.dispatch(t.reference, t.source, t.destination, x.sku, x.quantity)
        elif event.kind == "arrive":
            t = self.transfer(event.key)
            for x in t.lines:
                ledger.arrive(t.reference, x.sku)
        else:
            raise ValueError(f"unknown unit of work {event.kind}")

    def replay(self, events: list[Event] | None = None) -> tuple[StockLedger, dict[Key, int]]:
        """Stock and reorder points after the given events (default: all), from the state after the pilot."""
        ledger = StockLedger.from_snapshot(self.base_stock)
        reorder_points = dict(self.base_reorder_points)
        for e in self.events() if events is None else events:
            self.apply(ledger, reorder_points, e)
        return ledger, reorder_points

    # ------------------------------------------------------------- identity and summary
    def fingerprint(self) -> str:
        """SHA-256 of every planned document in canonical form. Same seed and settings, same value."""
        def plain(value):
            if isinstance(value, dict):
                return {k: plain(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [plain(v) for v in value]
            return canonical(value)
        payload = {"revisions": [plain(asdict(r)) for r in self.revisions],
                   "transfers": [plain(asdict(t)) for t in self.transfers],
                   "purchase_orders": [plain(asdict(p)) for p in self.purchase_orders]}
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def summary(self) -> dict:
        def count(values) -> dict:
            out: dict[str, int] = {}
            for v in values:
                out[v] = out.get(v, 0) + 1
            return dict(sorted(out.items()))
        events = self.events()
        lines = [x for t in self.transfers for x in t.lines]
        po_lines = [x for p in self.purchase_orders for x in p.lines]
        ledger, rops = self.replay()
        warehouses = {r.warehouse for r in self.revisions}
        return {
            "reorder point revisions (warehouses / listings changed)":
                f"{len(self.revisions)} / {sum(len(r.changes) for r in self.revisions)}",
            "warehouse reorder points, total before -> after":
                f"{sum(v for (loc, _), v in self.base_reorder_points.items() if loc in warehouses)} -> "
                f"{sum(v for (loc, _), v in rops.items() if loc in warehouses)}",
            "transfer documents": len(self.transfers),
            "transfer lines / units": f"{len(lines)} / {sum(x.quantity for x in lines)}",
            "transfers by market (documents)": count(t.market for t in self.transfers),
            "purchase orders (forced / natural)":
                f"{len(self.purchase_orders)} ({sum(p.forced for p in self.purchase_orders)} / "
                f"{sum(not p.forced for p in self.purchase_orders)})",
            "purchase order lines by trigger": count(x.trigger for x in po_lines),
            "purchase order units": sum(x.quantity for x in po_lines),
            "delivery notes (split purchase orders)":
                f"{sum(len(p.deliveries) for p in self.purchase_orders)} "
                f"({sum(len(p.deliveries) > 1 for p in self.purchase_orders)})",
            "units received": sum(d.units for _, d in self.deliveries()),
            "store requests not served (backorders)": len(self.backorders),
            "units of work (one transaction each)": len(events),
            "units of work by kind": count(e.kind for e in events),
            "stock on hand after the pilot -> after replenishment":
                f"{sum(v[0] for v in self.base_stock.values())} -> {ledger.total_on_hand()}",
            "units in transit at the end": ledger.units_in_transit(),
            "first event (UTC)": events[0].at.isoformat() if events else None,
            "last event (UTC)": events[-1].at.isoformat() if events else None,
        }
