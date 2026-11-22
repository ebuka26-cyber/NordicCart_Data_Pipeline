"""Hourly ETL from the change_log outbox (stage S10): raw -> cleaned -> analytics, as files under output/etl.

raw        extract: as nordiccart_extractor, in one REPEATABLE READ snapshot, every change_log row whose
           transaction is visible in the current snapshot and was not visible in the consumer's last snapshot
           (change_log_checkpoints, design V002: never change_id ranges or timestamps). The rows go to one
           gzip JSONL file (written to a temporary name, then renamed); only after that file exists is the
           consumer's checkpoint advanced. A crash between the two re-extracts the same changes next time:
           harmless, because the cleaned layer applies changes idempotently. Nothing is purged from change_log.
cleaned    the current state of every tracked table (one CSV per table, keyed by record_key), built by applying
           raw files in change_id order: a change older than or equal to the record's last applied change_id is
           skipped, so re-applying a raw file changes nothing. Deletes keep a tombstone (_deleted = 1).
           Data-quality checks run on the result (DQ_CHECKS); analytics are published only if all pass.
analytics  rebuilt from cleaned on every run (Power BI reporting rules, database/design rev3 note):
           fact_sales_daily   fulfilled orders by local fulfilment date, market and channel: orders, lines, units,
                              gross, VAT and net revenue in local currency and EUR (rate of the fulfilment date)
           orders_monthly     orders by placement month (local), market, channel and status
           inventory_current  on hand, reserved and reorder point by location and product
"""

from __future__ import annotations

import csv
import gzip
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

log = logging.getLogger("nordiccart.etl")
CONSUMER = "nordiccart_etl"
META = ("_change_id", "_deleted")
CENT = Decimal("0.01")


@dataclass
class EtlResult:
    raw_file: str | None = None
    extracted: int = 0
    applied: int = 0
    skipped: int = 0
    tables: dict = field(default_factory=dict)
    dq: list = field(default_factory=list)            # (check, violations)
    published: bool = False


# ----------------------------------------------------------------------------- paths and state
def _paths(root: Path) -> dict[str, Path]:
    p = {k: root / k for k in ("raw", "cleaned", "analytics")}
    for d in p.values():
        d.mkdir(parents=True, exist_ok=True)
    p["state"] = root / "state.json"
    return p


REPLACE_ATTEMPTS, REPLACE_WAIT = 20, 0.5


def _atomic_write(path: Path, write) -> None:
    tmp = path.with_name(path.name + ".tmp")
    write(tmp)
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:      # Windows: a reader (the API, a Power BI refresh) has the file open at this instant
            if attempt == REPLACE_ATTEMPTS:
                raise
            log.warning("%s is in use by a reader; replace retried in %.1f s (attempt %d)", path.name, REPLACE_WAIT,
                        attempt)
            time.sleep(REPLACE_WAIT)


def _load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"applied_raw": []}


# ----------------------------------------------------------------------------- raw
def extract(conn: psycopg.Connection, raw_dir: Path) -> tuple[Path | None, int]:
    """New change_log rows since the consumer's last snapshot -> one raw file; then advance the checkpoint."""
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        snap = conn.execute("SELECT pg_current_snapshot()::text").fetchone()[0]
        last = conn.execute("SELECT last_snapshot::text FROM change_log_checkpoints WHERE consumer_name = %s",
                            (CONSUMER,)).fetchone()
        last = last[0] if last else None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        tmp = raw_dir / f"change_log_{stamp}.jsonl.gz.tmp"
        n, first_id, last_id = 0, None, None
        with gzip.open(tmp, "wt", encoding="utf-8") as fh, conn.cursor(name="etl_extract") as cur:
            cur.itersize = 5000                            # server-side cursor: streamed, not held in memory
            cur.execute("SELECT change_id, table_name, record_key, operation::text, row_data, changed_at FROM change_log "
                        "WHERE pg_visible_in_snapshot(transaction_id, %s::pg_snapshot) "
                        "AND (%s::pg_snapshot IS NULL OR NOT pg_visible_in_snapshot(transaction_id, %s::pg_snapshot)) "
                        "ORDER BY change_id", (snap, last, last))
            for cid, table, key, op, data, at in cur:
                fh.write(json.dumps({"change_id": cid, "table": table, "key": key, "op": op, "row": data,
                                     "changed_at": at.isoformat()}, separators=(",", ":")) + "\n")
                n += 1
                first_id = cid if first_id is None else first_id
                last_id = cid
    path = None
    if n:
        path = raw_dir / f"change_log_{stamp}_{first_id}_{last_id}.jsonl.gz"
        os.replace(tmp, path)
    else:
        tmp.unlink()
    with conn.transaction():                              # the destination file exists: advance the checkpoint
        if last is None:
            conn.execute("INSERT INTO change_log_checkpoints (consumer_name, last_snapshot) VALUES (%s, %s::pg_snapshot)",
                         (CONSUMER, snap))
        else:
            conn.execute("UPDATE change_log_checkpoints SET last_snapshot = %s::pg_snapshot WHERE consumer_name = %s",
                         (snap, CONSUMER))
    return path, n


# ----------------------------------------------------------------------------- cleaned
def _read_table(path: Path) -> tuple[list[str], dict[str, dict]]:
    if not path.exists():
        return [], {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), {r["record_key"]: r for r in reader}


def _write_table(path: Path, columns: list[str], rows: dict[str, dict]) -> None:
    def write(tmp: Path) -> None:
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            for key in sorted(rows, key=lambda k: (len(k), k)):
                w.writerow(rows[key])
    _atomic_write(path, write)


def _cell(v) -> str:
    if v is None:
        return ""
    return json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else str(v)


def apply_raw(paths: dict[str, Path], state: dict) -> tuple[int, int, dict]:
    """Apply every raw file not applied yet (oldest first), idempotently by change_id."""
    pending = sorted(p for p in paths["raw"].glob("change_log_*.jsonl.gz") if p.name not in state["applied_raw"])
    applied = skipped = 0
    tables: dict[str, tuple[list[str], dict]] = {}
    for path in pending:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                ch = json.loads(line)
                table = ch["table"]
                if table not in tables:
                    tables[table] = _read_table(paths["cleaned"] / f"{table}.csv")
                columns, rows = tables[table]
                current = rows.get(ch["key"])
                if current is not None and int(current["_change_id"]) >= ch["change_id"]:
                    skipped += 1                                   # already applied (rerun or duplicate extract)
                    continue
                row = {"record_key": ch["key"], **{k: _cell(v) for k, v in ch["row"].items()},
                       "_change_id": str(ch["change_id"]), "_deleted": "1" if ch["op"] == "delete" else "0"}
                for k in row:
                    if k not in columns:
                        columns.append(k)
                rows[ch["key"]] = row
                applied += 1
    for table, (columns, rows) in tables.items():
        ordered = ["record_key"] + [c for c in columns if c not in ("record_key", *META)] + list(META)
        _write_table(paths["cleaned"] / f"{table}.csv", ordered, rows)
    state["applied_raw"] = sorted(set(state["applied_raw"]) | {p.name for p in pending})
    _atomic_write(paths["state"], lambda tmp: tmp.write_text(json.dumps(state, indent=1), encoding="utf-8"))
    counts = {t: sum(1 for r in _read_table(paths["cleaned"] / f"{t}.csv")[1].values() if r["_deleted"] == "0")
              for t in sorted(p.stem for p in paths["cleaned"].glob("*.csv"))}
    return applied, skipped, counts


def _live(paths: dict[str, Path], table: str) -> list[dict]:
    return [r for r in _read_table(paths["cleaned"] / f"{table}.csv")[1].values() if r["_deleted"] == "0"]


# ----------------------------------------------------------------------------- data quality
def dq_checks(paths: dict[str, Path], source_counts: dict[str, int] | None = None) -> list[tuple[str, int]]:
    """Essential checks on the cleaned layer; each returns its number of violations (0 = pass)."""
    t = {name: _live(paths, name) for name in ("orders", "order_items", "payment_attempts", "inventory_movements",
                                               "inventory_balances", "customers", "products", "stock_locations")}
    ids = {name: {r[f"{key}_id"] for r in t[name]} for name, key in
           (("orders", "order"), ("customers", "customer"), ("products", "product"), ("stock_locations", "stock_location"))}
    out = []
    if source_counts is not None:                          # ingestion completeness: cleaned = database, per table
        out.append(("tables whose cleaned row count differs from the database",
                    sum(1 for k, v in source_counts.items() if len(_live(paths, k)) != v)))
    out.append(("order lines without their order", sum(1 for r in t["order_items"] if r["order_id"] not in ids["orders"])))
    out.append(("payment attempts without their order",
                sum(1 for r in t["payment_attempts"] if r["order_id"] not in ids["orders"])))
    out.append(("orders without their customer", sum(1 for r in t["orders"] if r["customer_id"] not in ids["customers"])))
    out.append(("movements with an unknown product or location",
                sum(1 for r in t["inventory_movements"]
                    if r["product_id"] not in ids["products"] or r["stock_location_id"] not in ids["stock_locations"])))
    lines = defaultdict(Decimal)
    for r in t["order_items"]:
        lines[r["order_id"]] += Decimal(r["line_total"])
    out.append(("orders whose items total or order total does not add up",
                sum(1 for r in t["orders"] if Decimal(r["items_total"]) != lines[r["order_id"]]
                    or Decimal(r["order_total"]) != Decimal(r["items_total"]) + Decimal(r["shipping_fee"]))))
    out.append(("negative or oversold balances",
                sum(1 for r in t["inventory_balances"]
                    if int(r["quantity_on_hand"]) < 0 or int(r["quantity_reserved"]) > int(r["quantity_on_hand"]))))
    out.append(("orders with an unknown status or channel",
                sum(1 for r in t["orders"] if r["status"] not in ("placed", "stock_reserved", "confirmed", "fulfilled",
                                                                  "cancelled", "payment_failed")
                    or r["channel"] not in ("online", "in_store"))))
    return out


# ----------------------------------------------------------------------------- analytics
def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    def write(tmp: Path) -> None:
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
    _atomic_write(path, write)


def build_analytics(paths: dict[str, Path]) -> dict[str, int]:
    countries = {r["country_code"]: r for r in _live(paths, "countries")}
    tz = {c: ZoneInfo(r["time_zone"]) for c, r in countries.items()}
    rates = {(r["quote_currency_code"], r["rate_date"]): Decimal(r["rate"]) for r in _live(paths, "exchange_rates")
             if r["base_currency_code"] == "EUR"}
    orders = {r["order_id"]: r for r in _live(paths, "orders")}
    lines = defaultdict(list)
    for r in _live(paths, "order_items"):
        lines[r["order_id"]].append(r)

    def to_eur(amount: Decimal, currency: str, day: str) -> Decimal | None:
        if currency == "EUR":
            return amount
        rate = rates.get((currency, day))
        return (amount / rate).quantize(CENT, ROUND_HALF_UP) if rate else None

    sales = defaultdict(lambda: [0, 0, 0, Decimal(0), Decimal(0), Decimal(0), Decimal(0), 0])
    monthly = defaultdict(int)
    missing_rate = 0
    for o in orders.values():
        placed = datetime.fromisoformat(o["placed_at"]).astimezone(tz[o["country_code"]])
        monthly[(placed.strftime("%Y-%m"), o["country_code"], o["channel"], o["status"])] += 1
        if o["status"] != "fulfilled" or not o["fulfilled_at"]:
            continue
        day = datetime.fromisoformat(o["fulfilled_at"]).astimezone(tz[o["country_code"]]).date().isoformat()
        s = sales[(day, o["country_code"], o["channel"], o["currency_code"])]
        gross = sum((Decimal(x["line_total"]) for x in lines[o["order_id"]]), Decimal(0))
        vat = sum((Decimal(x["vat_amount"]) for x in lines[o["order_id"]]), Decimal(0))
        s[0] += 1
        s[1] += len(lines[o["order_id"]])
        s[2] += sum(int(x["quantity"]) for x in lines[o["order_id"]])
        s[3] += gross
        s[4] += vat
        eur_gross, eur_net = to_eur(gross, o["currency_code"], day), to_eur(gross - vat, o["currency_code"], day)
        if eur_gross is None:
            s[7] += 1
            missing_rate += 1
        else:
            s[5] += eur_gross
            s[6] += eur_net
    _write_csv(paths["analytics"] / "fact_sales_daily.csv",
               ["fulfilment_date_local", "market", "channel", "currency", "orders", "order_lines", "units",
                "revenue_gross_local", "vat_local", "revenue_net_local", "revenue_gross_eur", "revenue_net_eur",
                "orders_without_eur_rate"],
               [[d, m, ch, cur, s[0], s[1], s[2], s[3], s[4], s[3] - s[4], s[5], s[6], s[7]]
                for (d, m, ch, cur), s in sorted(sales.items())])
    _write_csv(paths["analytics"] / "orders_monthly.csv", ["month_local", "market", "channel", "status", "orders"],
               [[*k, v] for k, v in sorted(monthly.items())])
    locations = {r["stock_location_id"]: r for r in _live(paths, "stock_locations")}
    products = {r["product_id"]: r for r in _live(paths, "products")}
    categories = {r["category_id"]: r["category_code"] for r in _live(paths, "product_categories")}
    inv = [[locations[b["stock_location_id"]]["location_code"], locations[b["stock_location_id"]]["location_type"],
            locations[b["stock_location_id"]]["country_code"], products[b["product_id"]]["sku"],
            categories.get(products[b["product_id"]]["category_id"], ""), b["quantity_on_hand"], b["quantity_reserved"],
            b["reorder_point"]] for b in _live(paths, "inventory_balances")]
    _write_csv(paths["analytics"] / "inventory_current.csv",
               ["location_code", "location_type", "market", "sku", "category", "on_hand", "reserved", "reorder_point"],
               sorted(inv))
    return {"fact_sales_daily": len(sales), "orders_monthly": len(monthly), "inventory_current": len(inv),
            "orders_without_eur_rate": missing_rate}


# ----------------------------------------------------------------------------- run
def run_etl(conn: psycopg.Connection, root: Path, source_counts: dict[str, int] | None = None) -> EtlResult:
    """One ETL pass: extract (raw), apply (cleaned), data-quality checks, analytics (only if every check passes)."""
    paths = _paths(root)
    result = EtlResult()
    raw, result.extracted = extract(conn, paths["raw"])
    result.raw_file = raw.name if raw else None
    log.info("raw: %d change(s) extracted%s", result.extracted, f" to {raw.name}" if raw else " (nothing new)")
    state = _load_state(paths["state"])
    result.applied, result.skipped, result.tables = apply_raw(paths, state)
    log.info("cleaned: %d change(s) applied, %d already applied (skipped); live rows: %s", result.applied,
             result.skipped, ", ".join(f"{k} {v}" for k, v in result.tables.items()))
    result.dq = dq_checks(paths, source_counts)
    failed = [(c, n) for c, n in result.dq if n]
    for c, n in result.dq:
        log.info("DQ %-4s %s: %d", "PASS" if n == 0 else "FAIL", c, n)
    if failed:
        log.error("data-quality checks failed: analytics not published")
        return result
    marts = build_analytics(paths)
    result.published = True
    log.info("analytics published: %s", ", ".join(f"{k} {v}" for k, v in marts.items()))
    return result
