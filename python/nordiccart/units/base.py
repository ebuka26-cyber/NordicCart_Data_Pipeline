"""Building blocks shared by every unit of work.

Extracted from the pilot loaders in stage S1 without changing behaviour:
  * OutcomeMismatchError / expect    a unit gave another result than planned (rolled back)
  * Ids                              surrogate ids by business key, read once per run
  * run_unit                         one unit = one transaction, SET LOCAL lock_timeout,
                                     the design's retry rules (40001, 40P01, 55P03 up to 3
                                     times with jittered backoff; 23514/23503 never; 23505
                                     only through an optional "already done" check)
  * session advisory locks           one loader of a kind at a time
  * LEDGER_IDENTITY_SQL              balances that break on hand = sum(movements) or
                                     reserved = sum(active reservations)
  * has_activity_control             V009 (stage S4) applied: purchase order documents and the
                                     reorder point revision history exist and must be written
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable

import psycopg

RETRY_SQLSTATES = ("40001", "40P01", "55P03")
# Session advisory lock of the combined activity engine (design 7.3 step 1, stage S6). The pilot
# loaders take it too while they write, so the engine and a pilot load never write at the same time.
ACTIVITY_LOCK = "nordiccart.activity"
ACTIVITY_BUSY = ("The activity engine is running against this database (advisory lock nordiccart.activity held); "
                 "nothing was written. Run again when it has finished.")
MAX_RETRIES = 3
LOCK_TIMEOUT = "10s"

LEDGER_IDENTITY_SQL = """
    SELECT count(*) FROM inventory_balances b
     WHERE b.quantity_on_hand <> coalesce((SELECT sum(m.quantity_change) FROM inventory_movements m
                                            WHERE m.stock_location_id = b.stock_location_id
                                              AND m.product_id = b.product_id), 0)
        OR b.quantity_reserved <> coalesce((SELECT sum(r.quantity) FROM inventory_reservations r
                                             WHERE r.stock_location_id = b.stock_location_id
                                               AND r.product_id = b.product_id AND r.status = 'active'), 0)"""


class OutcomeMismatchError(RuntimeError):
    """A unit of work gave a different result than planned; that transaction was rolled back."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise OutcomeMismatchError(message)


class Ids:
    """Surrogate ids of customers, locations, stores and products by their business keys."""

    def __init__(self, conn: psycopg.Connection):
        self.customer = dict(conn.execute("SELECT customer_number, customer_id FROM customers").fetchall())
        self.location = dict(conn.execute("SELECT location_code, stock_location_id FROM stock_locations").fetchall())
        self.store = dict(conn.execute("SELECT l.location_code, s.store_id FROM stores s "
                                       "JOIN stock_locations l USING (stock_location_id)").fetchall())
        self.product = dict(conn.execute("SELECT sku, product_id FROM products").fetchall())

    @staticmethod
    def order(conn: psycopg.Connection, number: str) -> int:
        row = conn.execute("SELECT order_id FROM orders WHERE order_number = %s", (number,)).fetchone()
        if row is None:
            raise OutcomeMismatchError(f"{number}: the order does not exist")
        return row[0]


def run_unit(conn: psycopg.Connection, work: Callable[[], str], label: str, log: logging.Logger, *,
             rolled_back: str = "rolled back", already_done: Callable[[], bool] | None = None) -> str:
    """Run one unit of work in its own transaction and return its result text.

    `work` may raise psycopg.Rollback to end the transaction without writing (U2 out of
    stock); the result is then `rolled_back`. A UniqueViolation (23505) counts as done
    only when `already_done` confirms the database already holds the planned step.
    """
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            result = rolled_back
            with conn.transaction():
                conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                result = work()
            return result
        except psycopg.errors.UniqueViolation:
            if already_done is not None and already_done():
                return "already recorded (23505), matches the plan"
            raise
        except psycopg.Error as exc:
            if getattr(exc, "sqlstate", None) in RETRY_SQLSTATES and attempt <= MAX_RETRIES:
                wait = 0.05 * 2 ** attempt * (1 + random.random())          # jittered backoff (not data)
                log.warning("%s: SQLSTATE %s, retry %d of %d in %.2f s", label, exc.sqlstate, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            raise
    raise AssertionError("unreachable")


def has_activity_control(conn: psycopg.Connection) -> bool:
    """True when migration V009 (purchase orders, run ledger, reorder point revisions) is applied."""
    return conn.execute("SELECT EXISTS (SELECT 1 FROM meta.schema_migrations WHERE version = '009')").fetchone()[0]


def lock_key(conn: psycopg.Connection, name: str) -> int:
    return conn.execute("SELECT hashtextextended(%s, 0)", (name,)).fetchone()[0]


def try_session_lock(conn: psycopg.Connection, name: str) -> int | None:
    """Session advisory lock on `name`; returns its key, or None when another session holds it."""
    key = lock_key(conn, name)
    return key if conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0] else None


def release_session_lock(conn: psycopg.Connection, key: int) -> None:
    if not conn.closed and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
        conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
