"""Replenishment policy (docs/phase3_replenishment_feasibility.md, section 6): (s, S) with periodic review.

  s  = inventory_balances.reorder_point (stores: from the opening model; warehouses:
       revised to echelon demand by R0 when echelon_reorder_points = true)
  S  = order-up-to level, computed here from the opening model's expected weekly demand
  IP = inventory position
         store:     on hand - reserved + requested but not dispatched + in transit to the store
         warehouse: on hand - reserved + on order (purchase order lines not yet received)
  A listing is replenished when IP <= s, by S - IP (purchase orders rounded up to the
  category case pack). Products not launched yet are eligible only when their first stock
  would arrive within the pre-launch window; discontinued products and closed locations never.

Pure functions on integers and Decimal (no floats), so every result is reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from ..master_data.model import Product, StockLocation


def round_half_up(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def ceil_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def round_up_to_case(quantity: int, case_pack: int) -> int:
    """Smallest multiple of case_pack that is >= quantity (and at least one case)."""
    if case_pack < 1:
        raise ValueError("case_pack must be >= 1")
    cases = max(1, -(-quantity // case_pack))
    return cases * case_pack


def echelon_weekly(listings) -> dict[tuple[str, str], Decimal]:
    """Weekly demand a warehouse serves: its own online demand plus every store of its market.

    `listings` are OpeningListing rows (location_code, location_type, country_code, sku,
    expected_weekly_units). One warehouse per market, as in the NordicCart network.
    """
    stores: dict[tuple[str, str], Decimal] = {}
    warehouses: dict[str, list[str]] = {}
    for x in listings:
        if x.location_type == "store":
            stores[(x.country_code, x.sku)] = stores.get((x.country_code, x.sku), Decimal(0)) + x.expected_weekly_units
        elif x.location_code not in warehouses.setdefault(x.country_code, []):
            warehouses[x.country_code].append(x.location_code)
    if any(len(codes) != 1 for codes in warehouses.values()):
        raise ValueError("echelon demand assumes exactly one warehouse per market")
    return {(x.location_code, x.sku): x.expected_weekly_units + stores.get((x.country_code, x.sku), Decimal(0))
            for x in listings if x.location_type == "warehouse"}


def echelon_reorder_point(weekly: Decimal, lead_time_weeks: Decimal, safety_factor: Decimal,
                          floor: int, opening_quantity: int) -> int:
    """ceil(weekly x lead time x safety), at least `floor`, and below the opening quantity of a stocked listing.

    Staying below the opening quantity keeps verification script 12 (check 501) valid.
    """
    rop = max(floor, ceil_int(weekly * lead_time_weeks * safety_factor))
    if opening_quantity > 0:
        rop = min(rop, opening_quantity - 1)
    return max(rop, 0)


def store_order_up_to(weekly: Decimal, weeks_of_cover: Decimal, display_minimum: int, reorder_point: int) -> int:
    """Order-up-to level of a store: the display minimum or the weeks of cover, above the reorder point."""
    return max(display_minimum, round_half_up(weekly * weeks_of_cover), reorder_point + 1)


def warehouse_order_up_to(weekly: Decimal, weeks_of_cover: Decimal, minimum: int, reorder_point: int) -> int:
    """Order-up-to level of a warehouse: weeks of cover of the demand it serves, above the reorder point."""
    return max(minimum, round_half_up(weekly * weeks_of_cover), reorder_point + 1)


def needs_replenishment(position: int, reorder_point: int) -> bool:
    return position <= reorder_point


def eligible(product: Product, location: StockLocation, arrival: date, pre_launch_days: int) -> bool:
    """May stock of `product` arrive at `location` on `arrival`?

    The location must be active and open; the product active, launched or within the
    pre-launch window, and not discontinued by the arrival day.
    """
    if not location.is_active or location.opened_on > arrival:
        return False
    if not product.is_active or product.launched_on is None:
        return False
    if product.discontinued_on is not None and product.discontinued_on <= arrival:
        return False
    return arrival >= product.launched_on - timedelta(days=pre_launch_days)


@dataclass(frozen=True)
class StoreRequest:
    """A store's need found at a review, waiting for the next dispatch from its warehouse."""
    store: str
    sku: str
    quantity: int                 # S - IP at the review
    requested_at: object          # datetime of the review (UTC)
    available: int                # store on hand - reserved at the review
    position: int                 # IP at the review
    reorder_point: int
    order_up_to: int
    flagship: bool


def priority(request: StoreRequest) -> tuple:
    """Allocation order when a warehouse cannot serve every store (decision D5).

    Stores at 0 available first, then flagships, then the lowest available / reorder point
    ratio; ties by store code and sku so the result is deterministic.
    """
    ratio = Decimal(request.available) / Decimal(max(request.reorder_point, 1))
    return (0 if request.available <= 0 else 1, 0 if request.flagship else 1, ratio, request.store, request.sku)


def allocate(requests: list[StoreRequest], warehouse_available: int, protected: int) -> list[tuple[StoreRequest, int]]:
    """Split what the warehouse can give (available above `protected`) over requests for ONE sku.

    Returns (request, quantity) in priority order; quantity 0 means nothing could be sent
    (the store asks again at its next review).
    """
    remaining = max(0, warehouse_available - protected)
    out = []
    for request in sorted(requests, key=priority):
        give = min(request.quantity, remaining)
        remaining -= give
        out.append((request, give))
    return out
