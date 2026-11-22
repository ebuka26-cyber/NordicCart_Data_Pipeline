"""In-memory copy of inventory_balances, shared by the order and the replenishment simulations.

Per (location_code, sku) it keeps what V007 keeps per balance row:
    on_hand   = SUM(inventory_movements.quantity_change)
    reserved  = SUM(quantity) of active reservations
    available = on_hand - reserved
plus what V007 does not store anywhere: units in transit between a transfer_out and its
transfer_in. A transfer that has left its warehouse is on no balance until it arrives, so
the conservation identity of the whole network is

    total on hand + units in transit = opening + received - sold   (+ adjustments later)

Order operations (reserve, release, issue) behave exactly as before this module existed:
the pilot transaction plan and its fingerprint depend on them. The replenishment
operations (receive, dispatch, arrive) are strict and raise StockError for anything V007
would refuse, so a plan that passes the simulation cannot fail on stock at load time.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

Key = tuple[str, str]          # (location_code, sku)


class StockError(ValueError):
    """An operation that V007 would refuse: negative stock, reserved units moved, unknown transfer."""


@dataclass(frozen=True)
class InTransit:
    reference: str             # transfer document (external_reference of both movements)
    source: str                # location_code of the transfer_out
    destination: str           # location_code of the planned transfer_in
    sku: str
    quantity: int


class StockLedger:
    """On hand, reserved and in transit per (location_code, sku)."""

    def __init__(self, opening: dict[Key, int]):
        self.on_hand: dict[Key, int] = dict(opening)
        self.reserved: dict[Key, int] = defaultdict(int)
        self.in_transit: dict[tuple[str, str], InTransit] = {}     # (reference, sku) -> transfer line
        self.received: int = 0                                      # units received since construction

    @classmethod
    def from_snapshot(cls, snapshot: dict[Key, tuple[int, int]]) -> "StockLedger":
        ledger = cls({k: v[0] for k, v in snapshot.items()})
        for k, (_, reserved) in snapshot.items():
            if reserved:
                ledger.reserved[k] = reserved
        return ledger

    # ----------------------------------------------------------------- reading
    def available(self, location: str, sku: str) -> int:
        return self.on_hand.get((location, sku), 0) - self.reserved.get((location, sku), 0)

    def in_transit_to(self, destination: str, sku: str) -> int:
        return sum(t.quantity for t in self.in_transit.values() if t.destination == destination and t.sku == sku)

    def units_in_transit(self) -> int:
        return sum(t.quantity for t in self.in_transit.values())

    def total_on_hand(self, location: str | None = None) -> int:
        return sum(v for (loc, _), v in self.on_hand.items() if location is None or loc == location)

    def snapshot(self) -> dict[Key, tuple[int, int]]:
        """(on hand, reserved) per listing, in the shape of inventory_balances."""
        keys = set(self.on_hand) | {k for k, v in self.reserved.items() if v}
        return {k: (self.on_hand.get(k, 0), self.reserved.get(k, 0)) for k in sorted(keys)}

    def problems(self) -> list[str]:
        """Balances that V007's CHECKs would refuse (empty when the ledger is valid)."""
        out = []
        for k in sorted(set(self.on_hand) | set(self.reserved)):
            on_hand, reserved = self.on_hand.get(k, 0), self.reserved.get(k, 0)
            if on_hand < 0 or reserved < 0 or reserved > on_hand:
                out.append(f"{k[0]}/{k[1]}: on hand {on_hand}, reserved {reserved}")
        return out

    # ----------------------------------------------------------------- orders (V007 U2, U3/U4, U5)
    def reserve(self, location: str, lines) -> str:
        """All lines or none, like fn_reserve_order_stock."""
        if any(self.available(location, x.sku) < x.quantity for x in lines):
            return "out_of_stock"
        for x in lines:
            self.reserved[(location, x.sku)] += x.quantity
        return "reserved"

    def release(self, location: str, lines) -> None:
        for x in lines:
            self.reserved[(location, x.sku)] -= x.quantity

    def issue(self, location: str, lines) -> None:
        for x in lines:
            self.reserved[(location, x.sku)] -= x.quantity
            self.on_hand[(location, x.sku)] -= x.quantity

    # ----------------------------------------------------------------- replenishment (U7)
    def receive(self, location: str, sku: str, quantity: int) -> None:
        """purchase_receipt: on hand up."""
        if quantity <= 0:
            raise StockError(f"{location}/{sku}: a receipt needs a positive quantity, got {quantity}")
        self.on_hand[(location, sku)] = self.on_hand.get((location, sku), 0) + quantity
        self.received += quantity

    def dispatch(self, reference: str, source: str, destination: str, sku: str, quantity: int) -> None:
        """transfer_out: on hand down at the source, never into reserved units; the units go in transit."""
        if quantity <= 0:
            raise StockError(f"{reference} {sku}: a transfer needs a positive quantity, got {quantity}")
        if source == destination:
            raise StockError(f"{reference} {sku}: source and destination are both {source}")
        if (reference, sku) in self.in_transit:
            raise StockError(f"{reference} {sku}: already dispatched")
        if self.available(source, sku) < quantity:
            raise StockError(f"{reference} {sku}: {source} has {self.available(source, sku)} available, "
                             f"{quantity} requested (V007 ck_inventory_balances_no_oversell)")
        self.on_hand[(source, sku)] -= quantity
        self.in_transit[(reference, sku)] = InTransit(reference, source, destination, sku, quantity)

    def arrive(self, reference: str, sku: str) -> InTransit:
        """transfer_in: the matching transfer_out arrives at its destination, same quantity."""
        line = self.in_transit.pop((reference, sku), None)
        if line is None:
            raise StockError(f"{reference} {sku}: nothing in transit (no matching transfer_out)")
        self.on_hand[(line.destination, sku)] = self.on_hand.get((line.destination, sku), 0) + line.quantity
        return line
