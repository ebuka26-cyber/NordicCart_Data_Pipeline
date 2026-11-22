"""Unit tests for stage S1 (no database): pilot scopes, shared units of work, unchanged plans.

The database behaviour of S1 is tested in test_pilot_scope_db.py. These tests check the pieces
that need no PostgreSQL: what each pilot loader treats as its own records, that the units of work
the loaders run are the shared ones, that the rule texts and the SQL rules agree, and that the
replenishment plan's new identity fields do not change its fingerprint.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone

import pytest

from nordiccart.activity import scope
from nordiccart.activity.scope import LaterActivity, PilotScope, expected_stock
from nordiccart.inventory.config import load_inventory_settings
from nordiccart.inventory.generate import generate_opening_inventory
from nordiccart.replenishment import load as repl_load
from nordiccart.replenishment.config import load_replenishment_settings
from nordiccart.replenishment.generate import generate_replenishment
from nordiccart.transactions import load as tx_load
from nordiccart.transactions.config import load_transaction_settings
from nordiccart.transactions.generate import generate_transactions
from nordiccart.units import base, orders, stock

PILOT_TRANSACTIONS_FINGERPRINT = "fabcd571a3b9f66827f20e54d0ebc421be5f68937af558383ee4b84f7c466264"
PILOT_REPLENISHMENT_FINGERPRINT = "858bbfd09597030b573273c9885ffae0c0299e4ed146e8aca7c98f0e05531236"


@pytest.fixture(scope="module")
def pilot_plans(pilot_settings, pilot_dataset):
    inv = load_inventory_settings(pilot_settings)
    inventory = generate_opening_inventory(pilot_settings, inv, pilot_dataset)
    tx = load_transaction_settings(pilot_settings, inv.opening_date)
    pilot = generate_transactions(pilot_settings, tx, pilot_dataset, inventory, inv.store_format_weight)
    rp = load_replenishment_settings(pilot_settings, tx.store_hours, {c.category_code for c in pilot_dataset.product_categories})
    return pilot, generate_replenishment(pilot_settings, inv, rp, pilot_dataset, inventory, pilot)


def test_the_pilot_plans_are_unchanged(pilot_plans):
    pilot, plan = pilot_plans
    assert pilot.fingerprint() == PILOT_TRANSACTIONS_FINGERPRINT
    assert plan.fingerprint() == PILOT_REPLENISHMENT_FINGERPRINT


def test_the_replenishment_plan_names_the_pilot_it_is_built_on_outside_its_fingerprint(pilot_plans):
    pilot, plan = pilot_plans
    assert plan.pilot_order_numbers == tuple(o.order_number for o in pilot.orders)
    assert plan.pilot_last_sequence == 20
    assert plan.last_event_at() == datetime(2024, 12, 2, 12, 46, 4, tzinfo=timezone.utc)
    assert plan.document_numbers() == {f"{kind}-{m}-2024-000001" for kind in ("PO", "TR") for m in ("DK", "FI", "NO", "SE")}
    anonymous = dataclasses.replace(plan, pilot_order_numbers=(), pilot_last_sequence=0)
    assert anonymous.fingerprint() == plan.fingerprint()            # identity fields are not part of the documents
    with pytest.raises(ValueError, match="pilot orders"):
        repl_load.pilot_scope(anonymous, set())


def test_each_loader_scopes_its_own_records(pilot_plans):
    pilot, plan = pilot_plans
    tx_scope = tx_load.pilot_scope(pilot)
    assert tx_scope.boundary == datetime(2024, 11, 15, 8, 44, 22, tzinfo=timezone.utc)
    assert (len(tx_scope.order_numbers), tx_scope.last_order_sequence, len(tx_scope.listings)) == (20, 20, 320)
    assert not tx_scope.own_movement_ids and not tx_scope.own_documents
    repl_scope = repl_load.pilot_scope(plan, {7, 8})
    assert repl_scope.boundary == datetime(2024, 12, 2, 12, 46, 4, tzinfo=timezone.utc)
    assert repl_scope.order_numbers == tx_scope.order_numbers and repl_scope.listings == tx_scope.listings
    assert repl_scope.own_movement_ids == {7, 8} and len(repl_scope.own_documents) == 8


def test_expected_stock_adds_later_activity_only_at_the_pilot_listings():
    s = PilotScope(name="p", boundary=datetime(2024, 1, 1, tzinfo=timezone.utc), order_numbers=frozenset(),
                   last_order_sequence=0, listings=frozenset({("A", "x"), ("B", "x")}))
    later = LaterActivity(on_hand_change={("A", "x"): -2, ("C", "x"): 5}, reserved={("B", "x"): 1})
    assert expected_stock({("A", "x"): (10, 0)}, s, later) == {("A", "x"): (8, 0), ("B", "x"): (0, 1)}


def test_every_rule_has_a_text_and_every_text_a_rule():
    in_sql = set(re.findall(r"SELECT (?:DISTINCT )?'([a-z_]+)'", scope._VIOLATIONS))
    assert in_sql == set(scope.RULES)
    words = {"name": "the pilot", "boundary": "2024-11-15T08:44:22+00:00", "last_seq": 20}
    assert all(text.format(**words) for text in scope.RULES.values())


def test_the_loaders_run_the_shared_units_of_work():
    assert tx_load.OutcomeMismatchError is repl_load.OutcomeMismatchError is base.OutcomeMismatchError
    assert tx_load._Ids is repl_load._Ids is base.Ids
    assert (tx_load._place, tx_load._reserve, tx_load._payment_start, tx_load._payment_result, tx_load._fulfil,
            tx_load._customer_cancel, tx_load._expire, tx_load._cancel_out_of_stock) == \
        (orders.place, orders.reserve, orders.payment_start, orders.payment_result, orders.fulfil,
         orders.customer_cancel, orders.expire, orders.cancel_out_of_stock)
    assert repl_load._lock_balances is stock.lock_balances
    assert set(repl_load.UNITS) == {"revise", "receive", "dispatch", "arrive"}


def test_the_later_activity_summary_names_every_count():
    text = LaterActivity(orders=4, open_orders=1, movements=3, net_units=-2, by_type={"sale": 3},
                         extra_listings=2, extra_opening_balances=1, extra_opening_units=12).summary()
    assert text == ("4 orders (1 open), 3 movements (sale 3), net -2 units; listings outside the pilot 2 "
                    "(1 opening balances, 12 units)")
