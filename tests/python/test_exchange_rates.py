"""S5 deterministic series and opt-in integration on the restored local sandbox."""
from datetime import date
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from nordiccart.exchange_rates import DAY, E1_END, SIGMA, daily_rates, reconcile, working_day

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "sql/verification/16_verify_exchange_rates.sql"


def test_series(pilot_settings):
    day = pilot_settings.simulation_start
    prior = pilot_settings.pricing.reference_rates
    rows = 0
    while day <= pilot_settings.simulation_end:
        rates = daily_rates(pilot_settings, day, prior)
        assert rates == daily_rates(pilot_settings, day, prior)
        assert set(rates) == set(SIGMA)
        for quote, rate in rates.items():
            assert rate > 0 and rate == round(rate, 4)
            if not working_day(day):
                assert rate == prior[quote]
        assert abs(rates["DKK"] / Decimal("7.4634") - 1) <= Decimal(".003")
        prior = rates
        day += DAY
        rows += 3
    assert rows == 2190


@pytest.mark.parametrize("day", [date(2024,11,2), date(2024,12,25), date(2025,4,18),
                                 date(2025,4,21), date(2026,4,3), date(2026,5,1)])
def test_closures(day):
    assert not working_day(day)


@pytest.fixture
def sandbox():
    if os.getenv("NORDICCART_S5_SANDBOX") != "1":
        pytest.skip("requires restored V009 sandbox on 127.0.0.1:55432")
    name = "nordiccart_s5_test_" + uuid4().hex
    kwargs = dict(host="127.0.0.1", hostaddr="127.0.0.1", port=55432, connect_timeout=5, autocommit=True)
    with psycopg.connect(**kwargs, dbname="postgres", user="postgres") as admin:
        assert admin.execute("SELECT inet_server_port()").fetchone() == (55432,)
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE nordiccart_oltp_v9r_rehearsal").format(sql.Identifier(name)))
        try:
            admin.execute(sql.SQL("ALTER DATABASE {} SET timezone = 'UTC'").format(sql.Identifier(name)))
            with psycopg.connect(**kwargs, dbname=name, user="postgres") as setup:
                acl = ROOT / "database/security/V008_reapply_database_privileges.sql"
                setup.execute(acl.read_text(encoding="utf-8-sig"))
            with psycopg.connect(**kwargs, dbname=name, user="nordiccart_loader") as conn:
                yield conn
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def test_e1_cli_and_next_x1(sandbox, pilot_settings):
    conn = sandbox
    env = dict(os.environ, PGHOST="127.0.0.1", PGHOSTADDR="127.0.0.1", PGPORT="55432",
               PGDATABASE=conn.info.dbname, PGUSER="nordiccart_loader")
    def run(mode, expected):
        proc = subprocess.run([sys.executable, str(ROOT / "python/run_exchange_rates.py"), mode],
                              cwd=ROOT, env=env, input=conn.info.dbname+"\n", capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert f"{expected} rows pending" in proc.stderr
    counts = """SELECT (SELECT count(*) FROM exchange_rates), (SELECT count(*) FROM change_log),
                (SELECT count(*) FROM orders),(SELECT count(*) FROM inventory_movements),
                (SELECT sum(quantity_on_hand) FROM inventory_balances)"""
    assert conn.execute(counts).fetchone() == (0,1618,20,326,1467)
    verify = VERIFY.read_text(encoding="utf-8")
    assert conn.execute(verify).fetchall()[0][2] == 96
    run("--plan", 96)
    assert conn.execute(counts).fetchone() == (0,1618,20,326,1467)
    run("--apply", 96)
    run("--apply", 0)
    run("--plan", 0)
    assert conn.execute(counts).fetchone() == (96,1714,20,326,1467)
    assert [r[-1] for r in conn.execute(verify)] == ["PASS"] * 4
    # Verify all earlier catalog, business and security checks after E1, as the reader.
    with psycopg.connect(host="127.0.0.1", hostaddr="127.0.0.1", port=55432, dbname=conn.info.dbname,
                         user="nordiccart_analyst", options="-c default_transaction_read_only=on") as reader:
        for script in sorted(VERIFY.parent.glob("*.sql")):
            with reader.execute(script.read_text(encoding="utf-8-sig")) as cur:
                while True:
                    columns = [c.name for c in cur.description]
                    for row in cur.fetchall():
                        assert row[columns.index("status")] != "FAIL", (script.name, row)
                    if not cur.nextset():
                        break
    next_day = E1_END + DAY
    pending = reconcile(conn, pilot_settings, next_day, next_day, apply=True)
    assert len(pending) == 3
    assert conn.execute(counts).fetchone()[:2] == (99,1717)
    assert [r[-1] for r in conn.execute(verify)] == ["PASS"] * 4


def test_conflicts_and_stale_confirmation(sandbox, pilot_settings):
    first = pilot_settings.simulation_start
    pending = reconcile(sandbox, pilot_settings, first, E1_END)
    day, quote, rate = pending[0]
    insert = "INSERT INTO exchange_rates(rate_date,quote_currency_code,rate) VALUES (%s,%s,%s)"
    sandbox.execute(insert, (day, quote, rate))
    with pytest.raises(ValueError, match="changed"):
        reconcile(sandbox, pilot_settings, first, E1_END, apply=True, confirmed=pending)
    sandbox.execute(insert, (day, "DKK", 99))
    with pytest.raises(ValueError, match="Conflicting"):
        reconcile(sandbox, pilot_settings, first, E1_END, apply=True)
    assert sandbox.execute("SELECT count(*) FROM exchange_rates").fetchone() == (2,)
