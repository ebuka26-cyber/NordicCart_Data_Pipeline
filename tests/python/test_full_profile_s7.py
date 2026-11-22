"""S7: full-profile data expansion (500 products, 20 000 customers) on top of the loaded pilots.

In memory (always): the full-profile opening inventory keeps the pilot listings exactly as the
opening model gives them (what the pilots loaded), gives every other product on sale at the opening
the demand-calibrated opening stock and reorder point (decision C3), lists later launches at 0, and
passes the opening inventory validation. The pilot profile is unchanged.

Sandbox (NORDICCART_S6_SANDBOX=1; base NORDICCART_S6_BASE = the real database's state after S5):
the existing seeder and opening inventory loader expand a clone as nordiccart_loader (exception E2
for the new opening balances); a rerun finds nothing to insert; pilot records and exchange rates are
byte-identical; both pilot --plan checks report 0 conflicts; scripts 01 to 18 have 0 FAIL; the
activity engine runs and verifies two full-profile windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from nordiccart.demand.config import load_demand_settings
from nordiccart.demand.inventory import opening_inventory_for, recalibrated_opening
from nordiccart.demand.model import DemandModel
from nordiccart.inventory.config import load_inventory_settings
from nordiccart.inventory.generate import generate_opening_inventory
from nordiccart.inventory.validate import validate_opening_inventory
from nordiccart.master_data.generate import generate_master_data
from nordiccart.settings import load_settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def full():
    settings = load_settings(None, "full")
    inv = load_inventory_settings(settings)
    master = generate_master_data(settings)
    model = DemandModel(load_demand_settings(settings), settings, master)
    return settings, inv, master, model, opening_inventory_for(settings, inv, master, model)


def test_full_profile_opening_inventory(full):
    settings, inv, master, model, inventory = full
    pilot_settings = load_settings(None, "pilot")
    pilot_master = generate_master_data(pilot_settings)
    pilot = generate_opening_inventory(pilot_settings, load_inventory_settings(pilot_settings), pilot_master)
    pilot_skus = {p.sku for p in pilot_master.products}
    assert {x.key: x for x in inventory.listings if x.sku in pilot_skus} == {x.key: x for x in pilot.listings}
    assert len(inventory.listings) == 8000 and len(inventory.movements()) == 6096
    assert sum(x.opening_quantity for x in inventory.listings) == 19866
    assert sum(1 for x in inventory.listings if not x.stocked) == 1904
    base = generate_opening_inventory(settings, inv, master)
    recalibrated = recalibrated_opening(model, base, model.weekly_units(model.ds.generation_start))
    for x in inventory.listings:
        if x.sku not in pilot_skus and x.stocked:
            assert (x.opening_quantity, x.reorder_point) == recalibrated[x.key]
            assert x.reorder_point < x.opening_quantity
    assert validate_opening_inventory(inventory, master, settings, inv) == []
    assert opening_inventory_for(pilot_settings, load_inventory_settings(pilot_settings), pilot_master).fingerprint() \
        == pilot.fingerprint()                                          # the pilot profile is the opening model


# ----------------------------------------------------------------------------- sandbox
sandbox = pytest.mark.skipif(os.getenv("NORDICCART_S6_SANDBOX") != "1",
                             reason="requires the S6/S7 sandbox on 127.0.0.1:55432 (NORDICCART_S6_SANDBOX=1)")


def _cli(name: str, runner: str, *args: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env_file = tmp_path / f"{name}.env"
    env_file.write_text(f"PGHOST=127.0.0.1\nPGPORT=55432\nPGDATABASE={name}\nPGUSER=nordiccart_loader\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    return subprocess.run([sys.executable, "-B", str(ROOT / "python" / runner), *args, "--env-file", str(env_file)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=1200)


@sandbox
def test_full_profile_expansion_in_the_sandbox(tmp_path):
    import test_activity_engine as s6
    with psycopg.connect(**s6.KW, dbname="postgres", user="postgres") as c:
        os.environ["NORDICCART_S6_SANDBOX_CLUSTER"] = c.execute(
            "SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
    name = s6.clone("s7")
    try:
        pilot_before = s6.digest(name, pilot_only=True)
        for runner in ("run_master_data_seed.py", "run_opening_inventory.py"):
            applied = _cli(name, runner, "--profile", "full", "--apply", "--yes", *(["--no-csv"] if "inventory" in runner else []),
                           tmp_path=tmp_path)
            assert applied.returncode == 0, applied.stderr[-3000:]
            again = _cli(name, runner, "--profile", "full", "--plan", tmp_path=tmp_path)
            assert again.returncode == 0 and "conflict" in again.stderr.lower(), again.stderr[-2000:]
        assert "inserted  7680 inventory_balances rows" not in again.stderr                 # the rerun inserts nothing
        assert "0 listings and 0 opening balances would be inserted" in again.stderr
        with s6.reader(name) as conn:
            counts = conn.execute(
                "SELECT (SELECT count(*) FROM products), (SELECT count(*) FROM product_prices), (SELECT count(*) FROM customers), "
                "(SELECT count(*) FROM stores), (SELECT count(*) FROM warehouses), (SELECT count(*) FROM inventory_balances), "
                "(SELECT count(*) FROM inventory_movements WHERE movement_type = 'opening_balance'), "
                "(SELECT sum(quantity_change) FROM inventory_movements WHERE movement_type = 'opening_balance'), "
                "(SELECT count(*) FROM exchange_rates), (SELECT count(*) FROM orders)").fetchone()
            assert counts == (500, 2000, 20000, 12, 4, 8000, 6096, 19866, 96, 20)
        after = s6.digest(name, pilot_only=True)
        # pilot records and rates unchanged; "movements before the origin" now also holds the E2 opening balances
        # of the new products (dated at the opening count), so the movements are compared for the pilot products
        assert {k: v for k, v in after.items() if k != "movements"} == {k: v for k, v in pilot_before.items() if k != "movements"}
        pilot_skus = [p.sku for p in generate_master_data(load_settings(None, "pilot")).products]
        movements = s6.BUSINESS["movements"] + " WHERE m.occurred_at < '2024-12-03 00:00:00+00' AND p.sku = ANY(%s)"
        digests = []
        for db in (s6.BASE, name):
            with psycopg.connect(**s6.KW, dbname=db, user="postgres") as conn, conn.transaction():
                conn.execute("SET LOCAL TimeZone = 'UTC'")
                digests.append(conn.execute(f"SELECT count(*), md5(string_agg(t::text, '|' ORDER BY t::text COLLATE \"C\")) "
                                            f"FROM ({movements}) t", (pilot_skus,)).fetchone())
        assert digests[0] == digests[1] and digests[0][0] == 326
        assert after["movements"][0] == 326 + 5808
        for runner in ("run_transactions.py", "run_replenishment.py"):
            check = _cli(name, runner, "--plan", tmp_path=tmp_path)
            assert check.returncode == 0 and "conflicts: 0   blocking: 0" in check.stderr, check.stderr[-2000:]
        results = s6.scripts(name)
        assert all(fail == 0 for _, _, fail, _ in results.values()), results
        run = s6.engine(name, ROOT / "config" / "generator.toml", "--apply", "--windows", "2", "--yes", "--profile", "full")
        assert run.returncode == 0, run.stderr[-3000:]
        runs = s6.ledger(name)
        assert len(runs) == 2 and all(r[2] == "verified" and r[6] == r[11] for r in runs)
    finally:
        s6.drop(name)
