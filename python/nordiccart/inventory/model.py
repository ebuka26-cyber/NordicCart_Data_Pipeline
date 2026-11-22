"""Opening inventory rows.

One OpeningListing per location and product: it becomes one inventory_balances row
(the listing, created at 0/0 with its reorder point) and, when opening_quantity > 0,
one inventory_movements row of type opening_balance. The V007 trigger then raises
quantity_on_hand by that quantity. Rows carry business keys only (location_code, sku).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal

from ..master_data.model import canonical


@dataclass(frozen=True)
class OpeningListing:
    location_code: str
    location_type: str                  # store or warehouse (from master data)
    country_code: str
    sku: str
    category_code: str
    price_eur: Decimal                  # Finnish list price, used as the EUR price
    expected_weekly_units: Decimal      # planning figure for this location; not stored in the database
    reorder_point: int
    opening_quantity: int               # 0 when the product is not on sale at the opening
    occurred_at: datetime | None        # opening count time; None when opening_quantity is 0
    external_reference: str | None

    @property
    def key(self) -> tuple[str, str]:
        return self.location_code, self.sku

    @property
    def stocked(self) -> bool:
        return self.opening_quantity > 0


@dataclass
class OpeningInventory:
    profile: str
    seed: int
    opening_date: date
    listings: list[OpeningListing]

    def movements(self) -> list[OpeningListing]:
        return [x for x in self.listings if x.stocked]

    def fingerprint(self) -> str:
        """SHA-256 of every listing in canonical form. Same seed and settings, same value."""
        payload = [{k: canonical(v) for k, v in asdict(x).items()} for x in self.listings]
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def summary(self) -> dict:
        units: dict[str, int] = {}
        for x in self.listings:
            units[f"{x.country_code} {x.location_type}"] = units.get(f"{x.country_code} {x.location_type}", 0) + x.opening_quantity
        stocked = self.movements()
        return {
            "listings (inventory_balances rows)": len(self.listings),
            "opening_balance movements": len(stocked),
            "listings at zero (product not on sale yet)": len(self.listings) - len(stocked),
            "opening units": sum(x.opening_quantity for x in self.listings),
            "units by market and location type": dict(sorted(units.items())),
        }
