"""Read-only analytics API (nordiccart.api) on small fixture files: filters, KPIs, errors, reload after an ETL run."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from nordiccart.api import create_app  # noqa: E402

SALES = """fulfilment_date_local,market,channel,currency,orders,order_lines,units,revenue_gross_local,vat_local,revenue_net_local,revenue_gross_eur,revenue_net_eur,orders_without_eur_rate
2026-10-01,SE,online,SEK,2,3,4,1000.0,200.0,800.0,90.00,72.00,0
2026-10-01,FI,in_store,EUR,1,1,1,124.0,24.0,100.0,124.00,100.00,0
2026-10-02,SE,in_store,SEK,1,2,2,500.0,100.0,400.0,45.00,36.00,0
2026-11-01,SE,online,SEK,3,3,3,300.0,60.0,240.0,27.00,21.60,0
"""
ORDERS = """month_local,market,channel,status,orders
2026-10,FI,in_store,fulfilled,1
2026-10,SE,in_store,fulfilled,1
2026-10,SE,online,cancelled,1
2026-10,SE,online,fulfilled,2
2026-11,SE,online,fulfilled,3
2026-11,SE,online,payment_failed,1
"""
INVENTORY = """location_code,location_type,market,sku,category,on_hand,reserved,reorder_point
FI-STO-01,store,FI,ACC-000010,ACCESSORIES,5,0,3
SE-STO-01,store,SE,ACC-000010,ACCESSORIES,4,2,3
SE-WH-01,warehouse,SE,BAG-000001,BAGS,0,0,10
"""


@pytest.fixture()
def env(tmp_path):
    d = tmp_path / "analytics"
    d.mkdir()
    for name, text in (("fact_sales_daily.csv", SALES), ("orders_monthly.csv", ORDERS),
                       ("inventory_current.csv", INVENTORY)):
        (d / name).write_text(text, encoding="utf-8")
    runs = tmp_path / "runs.jsonl"
    runs.write_text('{"exit": 0, "etl": {"published": true}}\n', encoding="utf-8")
    return d, TestClient(create_app(d, runs))


def test_sales_filters_and_paging(env):
    _, client = env
    body = client.get("/sales/daily", params={"country": "se", "date_from": "2026-10-01", "date_to": "2026-10-31"}).json()
    assert body["total"] == 2
    assert [(r["fulfilment_date_local"], r["channel"]) for r in body["rows"]] == [("2026-10-01", "online"),
                                                                                  ("2026-10-02", "in_store")]
    assert body["rows"][0]["revenue_gross_eur"] == 90.0 and body["rows"][0]["country"] == "SE"
    assert client.get("/sales/daily", params={"channel": "in_store"}).json()["total"] == 2
    paged = client.get("/sales/daily", params={"limit": 1, "offset": 3}).json()
    assert paged["total"] == 4 and len(paged["rows"]) == 1 and paged["rows"][0]["fulfilment_date_local"] == "2026-11-01"


def test_orders_and_inventory_filters(env):
    _, client = env
    rows = client.get("/orders/monthly", params={"country": "SE", "status": "fulfilled", "month_to": "2026-10"}).json()
    assert [(r["channel"], r["orders"]) for r in rows["rows"]] == [("in_store", 1), ("online", 2)]
    low = client.get("/inventory/current", params={"low_stock": "true"}).json()["rows"]
    assert [(r["location_code"], r["available"]) for r in low] == [("SE-STO-01", 2), ("SE-WH-01", 0)]
    assert client.get("/inventory/current", params={"location_type": "warehouse"}).json()["total"] == 1
    assert client.get("/inventory/current", params={"category": "accessories", "country": "FI"}).json()["total"] == 1


def test_kpis(env):
    _, client = env
    k = client.get("/kpis", params={"country": "SE", "date_from": "2026-10-01", "date_to": "2026-10-31"}).json()
    assert k["sales"] == {"fulfilled_orders": 3, "order_lines": 5, "units": 6, "revenue_gross_eur": 135.0,
                          "revenue_net_eur": 108.0, "vat_eur": 27.0, "average_order_value_gross_eur": 45.0,
                          "units_per_order": 2.0, "orders_without_eur_rate": 0}
    assert k["orders"]["months"] == ["2026-10"] and k["orders"]["by_status"] == {"cancelled": 1, "fulfilled": 3}
    assert k["orders"]["cancellation_rate"] == 0.25
    assert k["inventory"]["sku_locations"] == 2 and k["inventory"]["out_of_stock_positions"] == 1
    total = client.get("/kpis").json()
    assert total["sales"]["revenue_gross_eur"] == 286.0
    assert {c["country"]: c["share_of_revenue_gross"] for c in total["sales_by_country"]} == {"FI": 0.4336,
                                                                                            "SE": 0.5664}


@pytest.mark.parametrize(("path", "params", "status"), [
    ("/sales/daily", {"country": "XX"}, 400),
    ("/sales/daily", {"date_from": "2026-11-01", "date_to": "2026-10-01"}, 400),
    ("/sales/daily", {"date_from": "2026-13-01"}, 422),
    ("/sales/daily", {"channel": "phone"}, 422),
    ("/sales/daily", {"limit": 0}, 422),
    ("/orders/monthly", {"month_from": "2026-1"}, 422),
    ("/orders/monthly", {"status": "lost"}, 400),
    ("/inventory/current", {"location_code": "XX-STO-09"}, 400),
    ("/kpis", {"country": "US"}, 400),
])
def test_invalid_requests(env, path, params, status):
    _, client = env
    r = client.get(path, params=params)
    assert r.status_code == status and "detail" in r.json()


def test_missing_file_is_503_and_health_degraded(env):
    d, client = env
    assert client.get("/health").json()["status"] == "ok"
    (d / "inventory_current.csv").unlink()
    r = client.get("/inventory/current")
    assert r.status_code == 503 and "run the ETL first" in r.json()["detail"]
    h = client.get("/health").json()
    assert h["status"] == "degraded" and h["last_pipeline_run"]["exit"] == 0


def test_reload_after_etl_publication(env):
    """A new file published with os.replace (as nordiccart.etl does) is served without a restart."""
    d, client = env
    assert client.get("/kpis").json()["sales"]["fulfilled_orders"] == 7
    tmp = d / "fact_sales_daily.csv.tmp"
    tmp.write_text(SALES + "2026-11-02,FI,online,EUR,5,5,5,50.0,10.0,40.0,50.00,40.00,0\n", encoding="utf-8")
    os.replace(tmp, d / "fact_sales_daily.csv")
    assert client.get("/kpis").json()["sales"]["fulfilled_orders"] == 12
    assert client.get("/sales/daily", params={"date_from": "2026-11-02"}).json()["total"] == 1
