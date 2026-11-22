"""Validation of the opening inventory before anything is written.

Rules checked (each failure becomes an Issue; an empty list means safe to load):
  completeness  every product listed exactly once at every store and warehouse
  references    every location and product exists in the master data
  schema        whole numbers, reorder_point >= 0 (V007 CHECK), quantities fit integer,
                external_reference at most 64 characters
  V007 rules    opening quantity > 0 when stocked (inbound movements are positive);
                one opening movement per listing
  business      stocked exactly when the product is on sale and the location open on the
                opening day; opening time = 00:00 local time on the opening day, not
                before the location opened or the product launched, not in the future;
                reorder point below the opening quantity; stores hold at least their
                display units; a warehouse holds at least as much as any store of its
                market; warehouse capacity not exceeded
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..master_data.model import MasterDataset
from ..master_data.rules import local_midnight_utc
from ..master_data.validate import Issue
from ..settings import Settings
from .config import InventorySettings
from .generate import display_minimum
from .model import OpeningInventory

INT4_MAX = 2_147_483_647


def validate_opening_inventory(inventory: OpeningInventory, master: MasterDataset, settings: Settings,
                               inv: InventorySettings) -> list[Issue]:
    issues: list[Issue] = []

    def add(x, rule: str, detail: str) -> None:
        issues.append(Issue("opening_inventory", f"{x.location_code}/{x.sku}" if x is not None else "-", rule, detail))

    locations = {l.location_code: l for l in master.stock_locations}
    products = {p.sku: p for p in master.products}
    countries = {c.country_code: c for c in master.countries}
    store_format = {s.location_code: s.store_format for s in master.stores}
    capacity = {w.location_code: w.storage_capacity_units for w in master.warehouses}

    # completeness and uniqueness
    expected = len(locations) * len(products)
    if len(inventory.listings) != expected:
        add(None, "listing_count", f"expected {expected} listings ({len(products)} products x {len(locations)} locations), "
                                   f"got {len(inventory.listings)}")
    seen: set[tuple[str, str]] = set()
    for x in inventory.listings:
        if x.key in seen:
            add(x, "unique_listing", "listed twice")
        seen.add(x.key)

    now = datetime.now(timezone.utc)
    store_max: dict[tuple[str, str], int] = {}
    warehouse_qty: dict[tuple[str, str], list[int]] = {}
    units_per_warehouse: dict[str, int] = {}

    for x in inventory.listings:
        loc = locations.get(x.location_code)
        product = products.get(x.sku)
        if loc is None:
            add(x, "fk_location", "location is not in the master data")
            continue
        if product is None:
            add(x, "fk_product", "product is not in the master data")
            continue
        if x.location_type != loc.location_type or x.country_code != loc.country_code or x.category_code != product.category_code:
            add(x, "master_attributes", "location type, country or category differ from the master data")

        for name, value in (("opening_quantity", x.opening_quantity), ("reorder_point", x.reorder_point)):
            if not isinstance(value, int) or isinstance(value, bool):
                add(x, "whole_number", f"{name} must be a whole number, got {value!r}")
            elif not 0 <= value <= INT4_MAX:
                add(x, f"{name}_range", f"{name}={value} must be between 0 and {INT4_MAX}")
        if not isinstance(x.opening_quantity, int) or not isinstance(x.reorder_point, int):
            continue

        on_sale = product.is_active and product.launched_on is not None and product.launched_on <= inv.opening_date
        open_location = loc.is_active and loc.opened_on <= inv.opening_date
        should_stock = on_sale and open_location
        if should_stock and x.opening_quantity <= 0:
            add(x, "opening_stock_missing", "product on sale at an open location needs opening stock (> 0)")
        if not should_stock and x.opening_quantity != 0:
            add(x, "opening_before_launch", f"no opening stock allowed: product launches {product.launched_on}, "
                                            f"location opened {loc.opened_on}, opening day {inv.opening_date}")

        if x.opening_quantity > 0:
            expected_at = local_midnight_utc(inv.opening_date, countries[loc.country_code].time_zone)
            if x.occurred_at != expected_at:
                add(x, "opening_time", f"occurred_at {x.occurred_at} must be 00:00 local time on {inv.opening_date}")
            elif x.occurred_at > now:
                add(x, "opening_time", "opening time is in the future")
            if x.external_reference != inv.external_reference or len(x.external_reference or "") > 64:
                add(x, "external_reference", f"must be {inv.external_reference!r} (at most 64 characters)")
            if x.reorder_point >= x.opening_quantity:
                add(x, "reorder_below_opening", f"reorder_point {x.reorder_point} must be below the opening quantity "
                                                f"{x.opening_quantity}")
            if loc.location_type == "store":
                minimum = display_minimum(inv, x.price_eur, store_format.get(x.location_code, "standard"))
                if x.opening_quantity < minimum:
                    add(x, "store_display_minimum", f"{x.opening_quantity} units, display minimum {minimum}")
                key = (loc.country_code, x.sku)
                store_max[key] = max(store_max.get(key, 0), x.opening_quantity)
            else:
                if x.opening_quantity < inv.warehouse_minimum_units:
                    add(x, "warehouse_minimum", f"{x.opening_quantity} units, minimum {inv.warehouse_minimum_units}")
                warehouse_qty.setdefault((loc.country_code, x.sku), []).append(x.opening_quantity)
                units_per_warehouse[x.location_code] = units_per_warehouse.get(x.location_code, 0) + x.opening_quantity
        else:
            if x.occurred_at is not None or x.external_reference is not None:
                add(x, "zero_listing", "a listing without opening stock has no opening time or reference")

    for key, biggest_store in store_max.items():
        for qty in warehouse_qty.get(key, []):
            if qty < biggest_store:
                add(None, "warehouse_covers_stores", f"{key[1]} in {key[0]}: warehouse {qty} units < a store with {biggest_store}")
    for code, units in units_per_warehouse.items():
        if capacity.get(code) is not None and units > capacity[code]:
            add(None, "warehouse_capacity", f"{code} holds {units} units, capacity {capacity[code]}")
    return issues
