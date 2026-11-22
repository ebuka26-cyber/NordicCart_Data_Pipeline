"""Loading the opening inventory through the V007 mechanisms.

What is written, in one transaction
  1. inventory_balances: one row per location and product (the listing), inserted at
     quantity_on_hand = 0 and quantity_reserved = 0 with its reorder_point. V007 refuses
     any other starting quantities (inventory_balances_start_empty).
  2. inventory_movements: one opening_balance row per stocked listing, in the V007 lock
     order (stock_location_id, product_id). The V007 trigger raises quantity_on_hand.
     The quantity columns are never written directly.

Why a repeat run cannot duplicate stock
  * The database itself allows one opening_balance per location and product
    (V007 index uq_inventory_movements_one_opening_balance). A second insert fails.
  * The loader matches every listing on (location_code, sku) and compares the existing
    opening movement, which is insert-only and never changes. Present and equal: left
    alone. Missing: inserted. Different: CONFLICT, nothing is written.
  * Opening stock is never added to a listing that already has later activity (other
    movements or reservations): that would rewrite history. CONFLICT instead.
  * One transaction under an advisory lock; plan again inside it; every row and the
    ledger identity (on_hand = sum of movements) verified before COMMIT.
Current stock levels and reorder points change with later activity. They are not
compared on a rerun (reorder point differences are reported as drift).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row

from ..master_data.load import PreconditionError, build_plan, check_database
from ..master_data.model import MasterDataset
from .model import OpeningInventory, OpeningListing

log = logging.getLogger("nordiccart.inventory.load")

ADVISORY_LOCK_KEY = "nordiccart.opening_inventory"

INVENTORY_COLUMNS = {
    ("inventory_balances", "quantity_on_hand"): "integer",
    ("inventory_balances", "quantity_reserved"): "integer",
    ("inventory_balances", "reorder_point"): "integer",
    ("inventory_movements", "movement_type"): "USER-DEFINED",
    ("inventory_movements", "quantity_change"): "integer",
    ("inventory_movements", "occurred_at"): "timestamp with time zone",
    ("inventory_movements", "external_reference"): "character varying",
}
REQUIRED_INDEX = "uq_inventory_movements_one_opening_balance"


class InventoryConflictError(RuntimeError):
    def __init__(self, plan: "InventoryPlan"):
        super().__init__(f"{len(plan.conflicts)} conflicting listings; nothing was written.")
        self.plan = plan


class PlanChangedError(RuntimeError):
    """The database changed between the confirmed plan and the write."""


class VerificationError(RuntimeError):
    """After the inserts, the database did not hold exactly the opening inventory (rolled back)."""


@dataclass
class InventoryPlan:
    listings: int = 0
    stocked: int = 0
    balances_present: int = 0
    balances_to_insert: list[OpeningListing] = field(default_factory=list)
    movements_present: int = 0
    movements_to_insert: list[OpeningListing] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)
    extra_balances: int = 0
    extra_opening_movements: int = 0

    @property
    def total_to_insert(self) -> int:
        return len(self.balances_to_insert) + len(self.movements_to_insert)

    def counts(self) -> tuple[int, int]:
        return len(self.balances_to_insert), len(self.movements_to_insert)

    def lines(self) -> list[str]:
        head = f"{'table':<38}{'dataset':>9}{'present':>9}{'insert':>8}{'extra':>7}"
        return [
            head, "-" * len(head),
            f"{'inventory_balances (listings)':<38}{self.listings:>9}{self.balances_present:>9}"
            f"{len(self.balances_to_insert):>8}{self.extra_balances:>7}",
            f"{'inventory_movements (opening_balance)':<38}{self.stocked:>9}{self.movements_present:>9}"
            f"{len(self.movements_to_insert):>8}{self.extra_opening_movements:>7}",
            f"conflicts: {len(self.conflicts)}   reorder point drift (allowed): {len(self.drift)}",
        ]


@dataclass
class InventoryResult:
    balances_inserted: int
    movements_inserted: int
    units_inserted: int
    verified_listings: int
    change_log_rows: int


# --------------------------------------------------------------------------- checks before anything else
def check_inventory_database(conn: psycopg.Connection, master: MasterDataset) -> dict:
    """Migrations 001-007, master tables as in V003-V005, inventory tables as in V007, master data loaded."""
    info = check_database(conn)
    rows = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name IN ('inventory_balances', 'inventory_movements')").fetchall()
    actual = {(r[0], r[1]): r[2] for r in rows}
    problems = [f"{t}.{c} is {actual.get((t, c), 'missing')}, expected {d}"
                for (t, c), d in INVENTORY_COLUMNS.items() if actual.get((t, c)) != d]
    if conn.execute("SELECT to_regclass(%s)", (f"public.{REQUIRED_INDEX}",)).fetchone()[0] is None:
        problems.append(f"index {REQUIRED_INDEX} (one opening balance per location and product) is missing")
    if problems:
        raise PreconditionError("The inventory tables differ from migration V007: " + "; ".join(problems))

    master_plan = build_plan(conn, master)
    if master_plan.conflict_count or master_plan.total_to_insert:
        missing = {name: len(t.to_insert) for name, t in master_plan.tables.items() if t.to_insert}
        raise PreconditionError(
            "The master data in the database is not the dataset this inventory is built from "
            f"(missing rows: {missing or 'none'}, differing rows: {master_plan.conflict_count}). "
            "Run python/run_master_data_seed.py --apply with the same config first.")
    return info


# --------------------------------------------------------------------------- planning
def _existing(conn: psycopg.Connection) -> tuple[dict, dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT l.location_code, p.sku, b.quantity_on_hand, b.quantity_reserved, b.reorder_point,
                   (SELECT count(*) FROM inventory_movements m
                     WHERE m.stock_location_id = b.stock_location_id AND m.product_id = b.product_id
                       AND m.movement_type <> 'opening_balance') AS other_movements,
                   (SELECT count(*) FROM inventory_reservations r
                     WHERE r.stock_location_id = b.stock_location_id AND r.product_id = b.product_id) AS reservations
              FROM inventory_balances b
              JOIN stock_locations l ON l.stock_location_id = b.stock_location_id
              JOIN products p ON p.product_id = b.product_id""")
        balances = {(r["location_code"], r["sku"]): r for r in cur.fetchall()}
        cur.execute("""
            SELECT l.location_code, p.sku, m.quantity_change, m.occurred_at, m.external_reference
              FROM inventory_movements m
              JOIN stock_locations l ON l.stock_location_id = m.stock_location_id
              JOIN products p ON p.product_id = m.product_id
             WHERE m.movement_type = 'opening_balance'""")
        openings = {(r["location_code"], r["sku"]): r for r in cur.fetchall()}
    return balances, openings


def build_inventory_plan(conn: psycopg.Connection, inventory: OpeningInventory) -> InventoryPlan:
    balances, openings = _existing(conn)
    plan = InventoryPlan(listings=len(inventory.listings), stocked=len(inventory.movements()))
    keys = set()
    for x in inventory.listings:
        keys.add(x.key)
        label = f"{x.location_code}/{x.sku}"
        bal = balances.get(x.key)
        if bal is None:
            plan.balances_to_insert.append(x)
        else:
            plan.balances_present += 1
            if bal["reorder_point"] != x.reorder_point:
                plan.drift.append(f"{label}: reorder_point database={bal['reorder_point']} generated={x.reorder_point}")
        opening = openings.get(x.key)
        if x.stocked:
            if opening is not None:
                diff = [f"{col}: database={opening[col]!r} generated={want!r}"
                        for col, want in (("quantity_change", x.opening_quantity), ("occurred_at", x.occurred_at),
                                          ("external_reference", x.external_reference))
                        if opening[col] != want]
                if diff:
                    plan.conflicts.append(f"{label}: opening balance differs: " + "; ".join(diff))
                else:
                    plan.movements_present += 1
            elif bal is not None and (bal["other_movements"] or bal["reservations"]):
                plan.conflicts.append(
                    f"{label}: stock activity is already recorded ({bal['other_movements']} movements, "
                    f"{bal['reservations']} reservations); opening stock cannot be added afterwards")
            else:
                plan.movements_to_insert.append(x)
        elif opening is not None:
            plan.conflicts.append(f"{label}: the database has an opening balance of {opening['quantity_change']}, "
                                  "but this product is not on sale on the opening day")
    plan.extra_balances = sum(1 for k in balances if k not in keys)
    plan.extra_opening_movements = sum(1 for k in openings if k not in keys)
    return plan


def plan_inventory(conn: psycopg.Connection, inventory: OpeningInventory) -> InventoryPlan:
    """Read-only comparison with the database."""
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return build_inventory_plan(conn, inventory)


# --------------------------------------------------------------------------- verification
def _verify(conn: psycopg.Connection, inventory: OpeningInventory,
            new_balances: set, new_movements: set) -> tuple[int, list[str]]:
    balances, openings = _existing(conn)
    problems: list[str] = []
    for x in inventory.listings:
        label = f"{x.location_code}/{x.sku}"
        bal = balances.get(x.key)
        if bal is None:
            problems.append(f"{label}: balance missing")
            continue
        if x.key in new_balances and bal["reorder_point"] != x.reorder_point:
            problems.append(f"{label}: reorder_point {bal['reorder_point']} != {x.reorder_point}")
        opening = openings.get(x.key)
        if x.stocked:
            if opening is None or (opening["quantity_change"], opening["occurred_at"], opening["external_reference"]) != \
                    (x.opening_quantity, x.occurred_at, x.external_reference):
                problems.append(f"{label}: opening balance {opening and opening['quantity_change']} != {x.opening_quantity}")
            elif x.key in new_movements and not bal["other_movements"] and \
                    (bal["quantity_on_hand"], bal["quantity_reserved"]) != (x.opening_quantity, 0):
                problems.append(f"{label}: on hand {bal['quantity_on_hand']}/{bal['quantity_reserved']} "
                                f"after the opening balance of {x.opening_quantity}")
        elif opening is not None:
            problems.append(f"{label}: unexpected opening balance")
    # Ledger identity for every listed balance (V007 maintains it; checked, never assumed).
    bad = conn.execute("""
        SELECT count(*) FROM inventory_balances b
         WHERE b.quantity_on_hand <> coalesce((SELECT sum(m.quantity_change) FROM inventory_movements m
                                                WHERE m.stock_location_id = b.stock_location_id
                                                  AND m.product_id = b.product_id), 0)
            OR b.quantity_reserved <> coalesce((SELECT sum(r.quantity) FROM inventory_reservations r
                                                 WHERE r.stock_location_id = b.stock_location_id
                                                   AND r.product_id = b.product_id AND r.status = 'active'), 0)""").fetchone()[0]
    if bad:
        problems.append(f"{bad} balances break the ledger identity (on hand = sum of movements, reserved = active reservations)")
    return len(inventory.listings), problems


def verify_inventory(conn: psycopg.Connection, inventory: OpeningInventory) -> tuple[int, list[str]]:
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return _verify(conn, inventory, set(), set())


# --------------------------------------------------------------------------- writing
def apply_inventory(conn: psycopg.Connection, inventory: OpeningInventory,
                    confirmed: InventoryPlan | None = None) -> InventoryResult:
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout = '10s'")
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (ADVISORY_LOCK_KEY,))
        plan = build_inventory_plan(conn, inventory)          # again, now under the lock
        if plan.conflicts:
            raise InventoryConflictError(plan)
        if confirmed is not None and plan.counts() != confirmed.counts():
            raise PlanChangedError("The database changed after the plan was shown; run again to see the new plan.")

        with conn.cursor() as cur:
            if plan.balances_to_insert:
                cur.executemany(
                    "INSERT INTO inventory_balances (stock_location_id, product_id, reorder_point) "
                    "SELECT l.stock_location_id, p.product_id, %s FROM stock_locations l, products p "
                    "WHERE l.location_code = %s AND p.sku = %s",
                    [(x.reorder_point, x.location_code, x.sku) for x in plan.balances_to_insert])
                log.info("inserted %5d inventory_balances rows (listings at 0 on hand)", len(plan.balances_to_insert))
            if plan.movements_to_insert:
                loc_ids = dict(conn.execute("SELECT location_code, stock_location_id FROM stock_locations").fetchall())
                prod_ids = dict(conn.execute("SELECT sku, product_id FROM products").fetchall())
                rows = sorted(plan.movements_to_insert, key=lambda x: (loc_ids[x.location_code], prod_ids[x.sku]))  # V007 lock order
                cur.executemany(
                    "INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, "
                    "occurred_at, external_reference) VALUES (%s, %s, 'opening_balance', %s, %s, %s)",
                    [(loc_ids[x.location_code], prod_ids[x.sku], x.opening_quantity, x.occurred_at, x.external_reference)
                     for x in rows])
                log.info("inserted %5d opening_balance movements (%d units)", len(rows), sum(x.opening_quantity for x in rows))

        new_balances = {x.key for x in plan.balances_to_insert}
        new_movements = {x.key for x in plan.movements_to_insert}
        checked, problems = _verify(conn, inventory, new_balances, new_movements)
        if problems:
            raise VerificationError(f"{len(problems)} problems after the insert: " + "; ".join(problems[:10]))
        change_log_rows = conn.execute(
            "SELECT count(*) FROM change_log WHERE transaction_id = pg_current_xact_id()").fetchone()[0]
    return InventoryResult(
        balances_inserted=len(new_balances),
        movements_inserted=len(new_movements),
        units_inserted=sum(x.opening_quantity for x in plan.movements_to_insert),
        verified_listings=checked,
        change_log_rows=change_log_rows,
    )
