"""Planned customer orders, their payment attempts and the business events that load them.

A PlannedOrder is the complete, expected life of one order: its lines and totals (the
purchase-time snapshot), its payment attempts with their outcomes, and the timestamps of
every step. Rows carry business keys only (order_number, customer_number, sku,
location_code); the loader resolves surrogate ids.

Each order expands into Events, one per unit of work of the design (DBML project note):
    place                U1   insert the order (placed) and its lines
    reserve              U2   fn_reserve_order_stock: all lines or none
    cancel_out_of_stock  U2   new transaction after an out_of_stock result: order cancelled
    payment_start        U3a  insert a pending payment attempt
    payment_result       U3c  captured (order confirmed) or failed; after the last failure
                              the order becomes payment_failed and its stock is released
    customer_cancel           reservations released, order cancelled (customer_request)
    expire               U4   reservations expired, order cancelled (reservation_expired)
    fulfil               U5   fn_fulfil_order_stock: reservations consumed, sale movements
The loader runs the events of all orders in business-time order, each in its own
transaction, which is how the database rules expect the application to write.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ..master_data.model import canonical
from ..stock.ledger import StockLedger as SharedStockLedger

EVENT_KINDS = ("place", "reserve", "cancel_out_of_stock", "payment_start", "payment_result",
               "customer_cancel", "expire", "fulfil")
UNIT_OF_WORK = {
    "place": "U1 place", "reserve": "U2 reserve", "cancel_out_of_stock": "U2 cancel (out_of_stock)",
    "payment_start": "U3a payment attempt", "payment_result": "U3c payment result",
    "customer_cancel": "cancel (customer_request)", "expire": "U4 expire", "fulfil": "U5 fulfil",
}


@dataclass(frozen=True)
class PlannedLine:
    line_number: int
    sku: str
    quantity: int
    unit_price: Decimal          # list price incl. VAT valid at placed_at (product_prices)
    unit_cost: Decimal           # unit cost excl. VAT valid at placed_at
    discount_percent: Decimal    # 0: no promotions in the MVP
    discount_amount: Decimal
    line_total: Decimal
    vat_rate: Decimal
    vat_amount: Decimal


@dataclass(frozen=True)
class PlannedAttempt:
    attempt_number: int
    payment_method: str
    payment_provider: str
    amount: Decimal
    outcome: str                 # captured or failed
    attempted_at: datetime
    authorized_at: datetime | None
    captured_at: datetime | None
    failed_at: datetime | None
    failure_reason: str | None
    provider_reference: str

    @property
    def result_at(self) -> datetime:
        return self.captured_at if self.outcome == "captured" else self.failed_at


@dataclass(frozen=True)
class PlannedOrder:
    sequence: int                # position in placed_at order, from first_order_sequence
    order_number: str
    scenario: str
    customer_number: str
    channel: str                 # online or in_store
    store_code: str | None       # selling store (in_store only)
    fulfilment_location_code: str
    country_code: str
    currency_code: str
    lines: tuple[PlannedLine, ...]
    items_total: Decimal
    shipping_fee: Decimal
    shipping_vat_amount: Decimal
    order_total: Decimal
    shipping_address_line1: str | None
    shipping_postal_code: str | None
    shipping_city: str | None
    placed_at: datetime
    reserve_at: datetime                     # when U2 runs
    reserve_outcome: str                     # reserved or out_of_stock
    reservation_expires_at: datetime         # expiry requested in U2 (no reservation when out_of_stock)
    attempts: tuple[PlannedAttempt, ...]
    final_status: str                        # fulfilled, payment_failed or cancelled
    cancellation_reason: str | None
    confirmed_at: datetime | None
    fulfilled_at: datetime | None
    cancelled_at: datetime | None
    reservation_close_status: str | None     # consumed, released, expired or None
    reservation_closed_at: datetime | None
    holder_order_number: str | None = None   # out_of_stock_reserved: the order holding the last unit

    def events(self) -> list["Event"]:
        out: list[Event] = []

        def add(at: datetime, kind: str, attempt: int | None = None) -> None:
            out.append(Event(at=at, sequence=self.sequence, step=len(out), order_number=self.order_number,
                             kind=kind, attempt_number=attempt))

        add(self.placed_at, "place")
        add(self.reserve_at, "reserve")
        if self.reserve_outcome == "out_of_stock":
            add(self.cancelled_at, "cancel_out_of_stock")
            return out
        for a in self.attempts:
            add(a.attempted_at, "payment_start", a.attempt_number)
            add(a.result_at, "payment_result", a.attempt_number)
        if self.final_status == "fulfilled":
            add(self.fulfilled_at, "fulfil")
        elif self.cancellation_reason == "customer_request":
            add(self.cancelled_at, "customer_cancel")
        elif self.cancellation_reason == "reservation_expired":
            add(self.reservation_closed_at, "expire")
        return out

    @property
    def captured_attempt(self) -> PlannedAttempt | None:
        return next((a for a in self.attempts if a.outcome == "captured"), None)

    @property
    def units(self) -> int:
        return sum(x.quantity for x in self.lines)


@dataclass(frozen=True)
class Event:
    at: datetime
    sequence: int
    step: int
    order_number: str
    kind: str
    attempt_number: int | None = None

    @property
    def sort_key(self) -> tuple:
        return self.at, self.sequence, self.step

    def label(self) -> str:
        text = UNIT_OF_WORK[self.kind]
        return f"{text} {self.attempt_number}" if self.attempt_number else text


class StockLedger(SharedStockLedger):
    """The shared stock ledger (nordiccart.stock.ledger) plus the order units of work.

    Mirrors what V007 does for each unit of work: reserve all lines or none, release or
    expire (reserved down), fulfil (reserved and on hand down). The generator uses it to
    decide availability, the loader to know the expected stock at any point of the plan.
    """

    def apply(self, order: PlannedOrder, event: Event) -> str | None:
        """Apply one event of `order`. Returns the U2 outcome for reserve events."""
        loc = order.fulfilment_location_code
        if event.kind == "reserve":
            return self.reserve(loc, order.lines)
        if event.kind in ("customer_cancel", "expire"):
            self.release(loc, order.lines)
        elif event.kind == "payment_result" and order.final_status == "payment_failed" \
                and event.attempt_number == order.attempts[-1].attempt_number:
            self.release(loc, order.lines)
        elif event.kind == "fulfil":
            self.issue(loc, order.lines)
        return None


@dataclass
class TransactionPlan:
    profile: str
    seed: int
    start_date: date
    end_date: date
    orders: list[PlannedOrder]
    opening_stock: dict[tuple[str, str], int] = field(repr=False, default_factory=dict)

    def events(self) -> list[Event]:
        return sorted((e for o in self.orders for e in o.events()), key=lambda e: e.sort_key)

    def last_event_at(self) -> datetime | None:
        """Business time of the plan's last unit of work. Later activity (replenishment) starts after it."""
        events = self.events()
        return events[-1].at if events else None

    def order(self, order_number: str) -> PlannedOrder:
        return self._by_number()[order_number]

    def _by_number(self) -> dict[str, PlannedOrder]:
        return {o.order_number: o for o in self.orders}

    def replay(self, events: list[Event] | None = None) -> StockLedger:
        """Stock after the given events (default: all of them), from the opening stock."""
        ledger = StockLedger(self.opening_stock)
        orders = self._by_number()
        for e in self.events() if events is None else events:
            ledger.apply(orders[e.order_number], e)
        return ledger

    def fingerprint(self) -> str:
        """SHA-256 of every planned order in canonical form. Same seed and settings, same value."""
        def plain(value):
            if isinstance(value, dict):
                return {k: plain(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [plain(v) for v in value]
            return canonical(value)
        payload = [plain(asdict(o)) for o in self.orders]
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def summary(self) -> dict:
        def count(values) -> dict:
            out: dict[str, int] = {}
            for v in values:
                out[v] = out.get(v, 0) + 1
            return dict(sorted(out.items()))
        events = self.events()
        fulfilled = [o for o in self.orders if o.final_status == "fulfilled"]
        return {
            "orders": len(self.orders),
            "orders by scenario": count(o.scenario for o in self.orders),
            "orders by final status": count(
                o.final_status + (f" ({o.cancellation_reason})" if o.cancellation_reason else "") for o in self.orders),
            "orders by channel": count(o.channel for o in self.orders),
            "orders by market": count(o.country_code for o in self.orders),
            "order lines": sum(len(o.lines) for o in self.orders),
            "payment attempts by outcome": count(a.outcome for o in self.orders for a in o.attempts),
            "payment attempts by method": count(a.payment_method for o in self.orders for a in o.attempts),
            "units sold (fulfilled)": sum(o.units for o in fulfilled),
            "units of work (one transaction each)": len(events),
            "units of work by kind": count(e.kind for e in events),
            "first order placed (UTC)": min(o.placed_at for o in self.orders).isoformat(),
            "last event (UTC)": events[-1].at.isoformat() if events else None,
        }
