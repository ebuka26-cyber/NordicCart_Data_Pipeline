"""NordicCart read-only analytics API (FastAPI) over the hourly ETL's published analytics files.

Source: output/etl/analytics/ (fact_sales_daily.csv, orders_monthly.csv, inventory_current.csv), written atomically by
nordiccart.etl.build_analytics after all 8 data-quality checks pass. The API never connects to PostgreSQL.

Freshness: every request stats the file; when its modification time or size changed (a new hourly publication) the file
is re-read, otherwise the parsed copy is reused. No restart is needed after an ETL run.

Pipeline safety: on Windows, os.replace fails (PermissionError) while any process holds the target file open, even with
FILE_SHARE_DELETE. The API therefore opens a file only when it has changed, reads it whole in one call (milliseconds)
and closes it at once; it never keeps a handle open between requests.

Start:  python python/run_api.py   (Swagger UI at http://127.0.0.1:8000/docs)
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ANALYTICS_DIR = REPO_ROOT / "output" / "etl" / "analytics"
DEFAULT_RUNS_LOG = REPO_ROOT / "logs" / "pipeline" / "runs.jsonl"
CENT = Decimal("0.01")
MAX_LIMIT = 10000

Channel = Literal["online", "in_store"]
LocationType = Literal["store", "warehouse"]
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


class AnalyticsUnavailable(Exception):
    """An analytics file is missing or unreadable (HTTP 503)."""


# ----------------------------------------------------------------------------- file access
def _read_whole(path: Path) -> tuple[bytes, os.stat_result]:
    """Read a whole file in one call and stat the same handle, so the handle is held only for that read (see module doc)."""
    with path.open("rb") as fh:
        return fh.read(), os.fstat(fh.fileno())


@dataclass(frozen=True)
class TableSpec:
    file: str
    ints: tuple[str, ...] = ()
    decimals: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()

    @property
    def columns(self) -> set[str]:
        return {*self.ints, *self.decimals, *self.texts}


TABLES = {
    "sales": TableSpec("fact_sales_daily.csv",
                       ints=("orders", "order_lines", "units", "orders_without_eur_rate"),
                       decimals=("revenue_gross_local", "vat_local", "revenue_net_local", "revenue_gross_eur",
                                 "revenue_net_eur"),
                       texts=("fulfilment_date_local", "market", "channel", "currency")),
    "orders": TableSpec("orders_monthly.csv", ints=("orders",), texts=("month_local", "market", "channel", "status")),
    "inventory": TableSpec("inventory_current.csv", ints=("on_hand", "reserved", "reorder_point"),
                           texts=("location_code", "location_type", "market", "sku", "category")),
}


@dataclass(frozen=True)
class Table:
    key: tuple[int, int]
    rows: list[dict]
    modified: datetime

    def values(self, column: str) -> list[str]:
        return sorted({r[column] for r in self.rows})


class AnalyticsStore:
    """Parsed analytics files, re-read whenever a file's (mtime, size) changes."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._cache: dict[str, Table] = {}
        self._lock = threading.Lock()

    def table(self, name: str) -> Table:
        spec = TABLES[name]
        path = self.directory / spec.file
        try:
            st = path.stat()
            with self._lock:
                cached = self._cache.get(name)
                if cached and cached.key == (st.st_mtime_ns, st.st_size):
                    return cached
                data, st = _read_whole(path)
                table = Table((st.st_mtime_ns, st.st_size), self._parse(spec, data),
                              datetime.fromtimestamp(st.st_mtime, timezone.utc))
                self._cache[name] = table
                return table
        except FileNotFoundError:
            raise AnalyticsUnavailable(f"{spec.file} not found in {self.directory}: run the ETL first") from None
        except OSError as exc:
            raise AnalyticsUnavailable(f"{spec.file} could not be read: {exc}") from None

    @staticmethod
    def _parse(spec: TableSpec, data: bytes) -> list[dict]:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
        if set(reader.fieldnames or ()) != spec.columns:
            raise AnalyticsUnavailable(f"{spec.file} has unexpected columns {reader.fieldnames}")
        rows = []
        for n, r in enumerate(reader, start=2):
            try:
                row = {c: r[c] for c in spec.texts}
                row.update({c: int(r[c]) for c in spec.ints})
                row.update({c: Decimal(r[c]) for c in spec.decimals})
            except (ValueError, ArithmeticError, TypeError):
                raise AnalyticsUnavailable(f"{spec.file} line {n} is malformed") from None
            rows.append(row)
        return rows


def _money(x: Decimal) -> float:
    return float(x.quantize(CENT, ROUND_HALF_UP))


def _ratio(num: int | Decimal, den: int | Decimal, places: str = "0.0001") -> float | None:
    return float((Decimal(num) / Decimal(den)).quantize(Decimal(places), ROUND_HALF_UP)) if den else None


def _check_value(name: str, value: str | None, known: list[str]) -> str | None:
    """Normalise an optional filter value; 400 if it is not present in the data."""
    if value is None:
        return None
    if value not in known:
        raise HTTPException(400, f"unknown {name} '{value}'; known values: {', '.join(known)}")
    return value


def _check_range(lo, hi, what: str) -> None:
    if lo is not None and hi is not None and lo > hi:
        raise HTTPException(400, f"{what}_from ({lo}) is after {what}_to ({hi})")


# ----------------------------------------------------------------------------- response models
class SalesDailyRow(BaseModel):
    fulfilment_date_local: date
    country: str = Field(description="market country code")
    channel: str
    currency: str
    orders: int
    order_lines: int
    units: int
    revenue_gross_local: float
    vat_local: float
    revenue_net_local: float
    revenue_gross_eur: float
    revenue_net_eur: float
    orders_without_eur_rate: int


class OrdersMonthlyRow(BaseModel):
    month_local: str
    country: str
    channel: str
    status: str
    orders: int


class InventoryRow(BaseModel):
    location_code: str
    location_type: str
    country: str
    sku: str
    category: str
    on_hand: int
    reserved: int
    available: int = Field(description="on_hand - reserved")
    reorder_point: int
    low_stock: bool = Field(description="available <= reorder_point")


class Page(BaseModel):
    source_file: str
    data_as_of_utc: datetime = Field(description="publication time of the analytics file (last ETL that changed it)")
    total: int = Field(description="rows matching the filters")
    limit: int
    offset: int


class SalesPage(Page):
    rows: list[SalesDailyRow]


class OrdersPage(Page):
    rows: list[OrdersMonthlyRow]


class InventoryPage(Page):
    rows: list[InventoryRow]


class SalesKpis(BaseModel):
    fulfilled_orders: int
    order_lines: int
    units: int
    revenue_gross_eur: float
    revenue_net_eur: float
    vat_eur: float
    average_order_value_gross_eur: float | None
    units_per_order: float | None
    orders_without_eur_rate: int


class CountrySales(BaseModel):
    country: str
    fulfilled_orders: int
    revenue_gross_eur: float
    revenue_net_eur: float
    share_of_revenue_gross: float | None


class OrderKpis(BaseModel):
    months: list[str] = Field(description="placement months covered (months overlapping the date range)")
    placed_orders: int
    by_status: dict[str, int]
    fulfilment_rate: float | None
    cancellation_rate: float | None
    payment_failure_rate: float | None


class InventoryKpis(BaseModel):
    sku_locations: int
    units_on_hand: int
    units_reserved: int
    units_available: int
    low_stock_positions: int
    out_of_stock_positions: int = Field(description="positions with available <= 0")


class Kpis(BaseModel):
    filters: dict[str, str | None]
    period: dict[str, date | None] = Field(description="fulfilment dates actually covered by the sales data")
    sales: SalesKpis
    sales_by_country: list[CountrySales]
    orders: OrderKpis
    inventory: InventoryKpis = Field(description="current snapshot; the channel and date filters do not apply")
    data_as_of_utc: dict[str, datetime]


class FileStatus(BaseModel):
    file: str
    available: bool
    rows: int | None = None
    data_as_of_utc: datetime | None = None
    error: str | None = None


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    analytics_dir: str
    files: list[FileStatus]
    last_pipeline_run: dict | None = Field(description="last record of logs/pipeline/runs.jsonl")


# ----------------------------------------------------------------------------- application
def create_app(analytics_dir: Path | None = None, runs_log: Path | None = None) -> FastAPI:
    analytics_dir = Path(analytics_dir or os.environ.get("NORDICCART_ANALYTICS_DIR") or DEFAULT_ANALYTICS_DIR)
    runs_log = Path(runs_log or DEFAULT_RUNS_LOG)
    store = AnalyticsStore(analytics_dir)
    app = FastAPI(
        title="NordicCart Analytics API",
        version="1.0.0",
        description="Read-only access to the analytics published by the hourly NordicCart ETL "
                    "(`output/etl/analytics`). Data refreshes automatically after each hourly run. "
                    "EUR amounts are converted at the EUR exchange rate of the fulfilment date (exchange_rates); "
                    "local amounts are in the row's currency. Sales count fulfilled orders by local fulfilment "
                    "date; monthly orders count all orders by local placement month and status.",
    )
    app.state.store = store

    @app.exception_handler(AnalyticsUnavailable)
    async def _unavailable(_: Request, exc: AnalyticsUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    def page(table: Table, file: str, rows: list[dict], limit: int, offset: int) -> dict:
        return {"source_file": file, "data_as_of_utc": table.modified, "total": len(rows), "limit": limit,
                "offset": offset, "rows": rows[offset:offset + limit]}

    def country_param(value: str | None) -> str | None:
        return value.upper() if value else None

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/health", response_model=Health, tags=["status"], summary="Analytics availability and freshness")
    def health() -> dict:
        files = []
        for name, spec in TABLES.items():
            try:
                t = store.table(name)
                files.append({"file": spec.file, "available": True, "rows": len(t.rows), "data_as_of_utc": t.modified})
            except AnalyticsUnavailable as exc:
                files.append({"file": spec.file, "available": False, "error": str(exc)})
        last = None
        try:
            with runs_log.open(encoding="utf-8") as fh:
                lines = [x for x in fh.read().splitlines() if x.strip()]
            last = json.loads(lines[-1]) if lines else None
        except (OSError, ValueError):
            pass
        return {"status": "ok" if all(f["available"] for f in files) else "degraded",
                "analytics_dir": str(analytics_dir), "files": files, "last_pipeline_run": last}

    @app.get("/sales/daily", response_model=SalesPage, tags=["sales"],
             summary="Daily fulfilled sales by country, channel and currency")
    def sales_daily(
        country: str | None = Query(None, description="market country code, e.g. SE"),
        channel: Channel | None = None,
        date_from: date | None = Query(None, description="first fulfilment date (local), inclusive"),
        date_to: date | None = Query(None, description="last fulfilment date (local), inclusive"),
        limit: int = Query(1000, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> dict:
        _check_range(date_from, date_to, "date")
        t = store.table("sales")
        c = _check_value("country", country_param(country), t.values("market"))
        rows = [_sales_row(r) for r in _filter_sales(t.rows, c, channel, date_from, date_to)]
        return page(t, TABLES["sales"].file, rows, limit, offset)

    @app.get("/orders/monthly", response_model=OrdersPage, tags=["orders"],
             summary="Orders placed per month by country, channel and status")
    def orders_monthly(
        country: str | None = Query(None, description="market country code, e.g. SE"),
        channel: Channel | None = None,
        status: str | None = Query(None, description="order status, e.g. fulfilled"),
        month_from: str | None = Query(None, pattern=MONTH_PATTERN, description="first placement month YYYY-MM"),
        month_to: str | None = Query(None, pattern=MONTH_PATTERN, description="last placement month YYYY-MM"),
        limit: int = Query(1000, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> dict:
        _check_range(month_from, month_to, "month")
        t = store.table("orders")
        c = _check_value("country", country_param(country), t.values("market"))
        s = _check_value("status", status, t.values("status"))
        rows = [{"month_local": r["month_local"], "country": r["market"], "channel": r["channel"],
                 "status": r["status"], "orders": r["orders"]}
                for r in _filter_orders(t.rows, c, channel, month_from, month_to)
                if s is None or r["status"] == s]
        return page(t, TABLES["orders"].file, rows, limit, offset)

    @app.get("/inventory/current", response_model=InventoryPage, tags=["inventory"],
             summary="Current stock per location and SKU")
    def inventory_current(
        country: str | None = Query(None, description="market country code, e.g. SE"),
        location_type: LocationType | None = None,
        location_code: str | None = Query(None, description="e.g. SE-STO-01"),
        category: str | None = Query(None, description="e.g. ACCESSORIES"),
        sku: str | None = Query(None, description="e.g. ACC-000010"),
        low_stock: bool | None = Query(None, description="true: only positions with available <= reorder_point"),
        limit: int = Query(1000, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> dict:
        t = store.table("inventory")
        c = _check_value("country", country_param(country), t.values("market"))
        loc = _check_value("location_code", location_code.upper() if location_code else None,
                           t.values("location_code"))
        cat = _check_value("category", category.upper() if category else None, t.values("category"))
        rows = []
        for r in t.rows:
            if (c and r["market"] != c) or (location_type and r["location_type"] != location_type) \
                    or (loc and r["location_code"] != loc) or (cat and r["category"] != cat) \
                    or (sku and r["sku"] != sku.upper()):
                continue
            row = _inventory_row(r)
            if low_stock is None or row["low_stock"] == low_stock:
                rows.append(row)
        return page(t, TABLES["inventory"].file, rows, limit, offset)

    @app.get("/kpis", response_model=Kpis, tags=["kpis"], summary="Business KPIs for a period")
    def kpis(
        country: str | None = Query(None, description="market country code, e.g. SE"),
        channel: Channel | None = None,
        date_from: date | None = Query(None, description="first fulfilment date (local), inclusive"),
        date_to: date | None = Query(None, description="last fulfilment date (local), inclusive"),
    ) -> dict:
        _check_range(date_from, date_to, "date")
        sales_t, orders_t, inv_t = store.table("sales"), store.table("orders"), store.table("inventory")
        c = _check_value("country", country_param(country), sales_t.values("market"))
        sales = _filter_sales(sales_t.rows, c, channel, date_from, date_to)
        tot = defaultdict(Decimal)
        per_country: dict[str, dict] = defaultdict(lambda: defaultdict(Decimal))
        for r in sales:
            for k in ("orders", "order_lines", "units", "orders_without_eur_rate", "revenue_gross_eur",
                      "revenue_net_eur"):
                tot[k] += r[k]
            pc = per_country[r["market"]]
            for k in ("orders", "revenue_gross_eur", "revenue_net_eur"):
                pc[k] += r[k]
        days = [r["fulfilment_date_local"] for r in sales]
        m_from = date_from.strftime("%Y-%m") if date_from else None
        m_to = date_to.strftime("%Y-%m") if date_to else None
        placed = _filter_orders(orders_t.rows, c, channel, m_from, m_to)
        by_status: dict[str, int] = defaultdict(int)
        for r in placed:
            by_status[r["status"]] += r["orders"]
        n_placed = sum(by_status.values())
        inv = [_inventory_row(r) for r in inv_t.rows if not c or r["market"] == c]
        gross = tot["revenue_gross_eur"]
        return {
            "filters": {"country": c, "channel": channel,
                        "date_from": date_from.isoformat() if date_from else None,
                        "date_to": date_to.isoformat() if date_to else None},
            "period": {"first_fulfilment_date": min(days) if days else None,
                       "last_fulfilment_date": max(days) if days else None},
            "sales": {
                "fulfilled_orders": int(tot["orders"]), "order_lines": int(tot["order_lines"]),
                "units": int(tot["units"]), "revenue_gross_eur": _money(gross),
                "revenue_net_eur": _money(tot["revenue_net_eur"]),
                "vat_eur": _money(gross - tot["revenue_net_eur"]),
                "average_order_value_gross_eur": _money(gross / tot["orders"]) if tot["orders"] else None,
                "units_per_order": _ratio(tot["units"], tot["orders"], "0.01"),
                "orders_without_eur_rate": int(tot["orders_without_eur_rate"]),
            },
            "sales_by_country": [
                {"country": k, "fulfilled_orders": int(v["orders"]), "revenue_gross_eur": _money(v["revenue_gross_eur"]),
                 "revenue_net_eur": _money(v["revenue_net_eur"]),
                 "share_of_revenue_gross": _ratio(v["revenue_gross_eur"], gross)}
                for k, v in sorted(per_country.items())],
            "orders": {
                "months": sorted({r["month_local"] for r in placed}), "placed_orders": n_placed,
                "by_status": dict(sorted(by_status.items())),
                "fulfilment_rate": _ratio(by_status.get("fulfilled", 0), n_placed),
                "cancellation_rate": _ratio(by_status.get("cancelled", 0), n_placed),
                "payment_failure_rate": _ratio(by_status.get("payment_failed", 0), n_placed),
            },
            "inventory": {
                "sku_locations": len(inv), "units_on_hand": sum(r["on_hand"] for r in inv),
                "units_reserved": sum(r["reserved"] for r in inv),
                "units_available": sum(r["available"] for r in inv),
                "low_stock_positions": sum(r["low_stock"] for r in inv),
                "out_of_stock_positions": sum(r["available"] <= 0 for r in inv),
            },
            "data_as_of_utc": {"sales": sales_t.modified, "orders": orders_t.modified, "inventory": inv_t.modified},
        }

    return app


def _filter_sales(rows: list[dict], country, channel, date_from: date | None, date_to: date | None) -> list[dict]:
    lo, hi = (date_from.isoformat() if date_from else None), (date_to.isoformat() if date_to else None)
    return [r for r in rows
            if (not country or r["market"] == country) and (not channel or r["channel"] == channel)
            and (not lo or r["fulfilment_date_local"] >= lo) and (not hi or r["fulfilment_date_local"] <= hi)]


def _filter_orders(rows: list[dict], country, channel, month_from: str | None, month_to: str | None) -> list[dict]:
    return [r for r in rows
            if (not country or r["market"] == country) and (not channel or r["channel"] == channel)
            and (not month_from or r["month_local"] >= month_from) and (not month_to or r["month_local"] <= month_to)]


def _sales_row(r: dict) -> dict:
    out = {k: v for k, v in r.items() if k != "market"}
    out["country"] = r["market"]
    for k in TABLES["sales"].decimals:
        out[k] = _money(r[k])
    return out


def _inventory_row(r: dict) -> dict:
    available = r["on_hand"] - r["reserved"]
    return {"location_code": r["location_code"], "location_type": r["location_type"], "country": r["market"],
            "sku": r["sku"], "category": r["category"], "on_hand": r["on_hand"], "reserved": r["reserved"],
            "available": available, "reorder_point": r["reorder_point"], "low_stock": available <= r["reorder_point"]}


app = create_app()
