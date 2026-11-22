# FastAPI: read-only analytics API: implementation report

Date: 2026-11-20.

**Status: COMPLETE.** The API serves the hourly ETL's analytics with filters, KPIs, error handling and Swagger documentation.
- It agrees with an independent recomputation from the files and, for closed periods, with the real database to the cent.
- It picks up each hourly publication without a restart.
- Scope held:
  - **database:** no new database, no migration and no database access by the API;
  - **pipeline:** the hourly pipeline and ETL were not modified;
  - **new packages:** only FastAPI, uvicorn (server) and httpx (test client).

## 1. Design

| Item | Choice |
|---|---|
| Source | `output/etl/analytics/`: `fact_sales_daily.csv`, `orders_monthly.csv`, `inventory_current.csv`. The ETL publishes them atomically, only after all 8 data-quality checks pass |
| Database | None. The API reads files only, so it cannot write to or load PostgreSQL |
| Freshness | Each request checks each file's modification time and size. A changed file (a new hourly publication) is re-read; otherwise the parsed copy in memory is reused. No restart is needed |
| Pipeline safety | On Windows, the ETL's `os.replace` fails while any process has the target file open. I tested this: it fails even with `FILE_SHARE_DELETE`. So the API opens a file only when it has changed, reads it whole in one call (median 0.25 ms, maximum 0.59 ms for the largest file) and closes it at once. It never keeps a file open between requests |
| Money | Totals are summed exactly (Decimal), then rounded to cents. EUR figures are the ETL's own conversions at the fulfilment-date rate |
| Network | Listens on `127.0.0.1` by default. There is no authentication, so it should not be exposed beyond localhost as is |

## 2. Endpoints

All endpoints are `GET` and read-only. Swagger UI is at `/docs`, ReDoc at `/redoc` and the schema at `/openapi.json`; `/` redirects to `/docs`.

| Endpoint | Purpose | Filters |
|---|---|---|
| `/health` | Whether each analytics file is available, with its rows and publication time (UTC); the last pipeline run from `logs/pipeline/runs.jsonl`. `status` is `ok`, or `degraded` if a file is missing | none |
| `/sales/daily` | Fulfilled sales per local fulfilment date, country, channel and currency: orders, lines, units, gross, VAT and net revenue in local currency and EUR | `country`, `channel`, `date_from`, `date_to`, `limit`, `offset` |
| `/orders/monthly` | Orders placed per local month, country, channel and status | `country`, `channel`, `status`, `month_from`, `month_to` (YYYY-MM), `limit`, `offset` |
| `/inventory/current` | Current stock per location and SKU: on hand, reserved, available (on hand − reserved), reorder point and a `low_stock` flag (available ≤ reorder point) | `country`, `location_type`, `location_code`, `category`, `sku`, `low_stock`, `limit`, `offset` |
| `/kpis` | Business KPIs (see below) | `country`, `channel`, `date_from`, `date_to` |

**List responses:** `source_file`, `data_as_of_utc`, `total` (rows matching the filters), `limit` (default 1,000, maximum 10,000), `offset` and `rows`.

**Filter rules:**
- `country` takes a market code (DK, FI, NO, SE) and is case-insensitive.
- `channel` is `online` or `in_store`.

**`/kpis` returns:**
- **sales:** fulfilled orders, lines, units, gross, net and VAT in EUR, average order value (EUR, gross), units per order, and orders without an EUR rate;
- **sales by country:** orders, gross and net EUR, and each country's share of gross revenue;
- **orders:** orders placed, counts by status, and fulfilment, cancellation and payment-failure rates. These use the placement months that overlap the date range, listed in `months`;
- **inventory:** SKU-location positions, units on hand, reserved and available, low-stock and out-of-stock positions. This is the current snapshot, so the country filter applies but the channel and date filters do not;
- **freshness:** `data_as_of_utc` for each source file.

**Error handling:**

| Case | Response |
|---|---|
| Invalid type or format: a bad date, a `channel` other than `online`/`in_store`, a malformed `month_*`, a `limit` out of range | **422**, FastAPI validation detail |
| A value that is not in the data (unknown country, status, location or category) | **400**, listing the known values |
| `*_from` after `*_to` | **400** |
| Analytics file missing, unreadable, with unexpected columns or a malformed row | **503**, naming the file |
| Unknown path | **404** |

## 3. Files created or modified

| File | Change |
|---|---|
| `python/nordiccart/api.py` | **New:** the FastAPI application (`create_app`, file store with change detection, endpoints, response models for Swagger) |
| `python/run_api.py` | **New:** launcher (uvicorn; `--host`, `--port`, `--analytics-dir`) |
| `tests/python/test_api.py` | **New:** 14 tests on small fixture files: filters, paging, KPI arithmetic, 9 error cases, missing file (503 and `degraded` health), and reload after an `os.replace` publication |
| `python/requirements.txt` | Added `fastapi`, `uvicorn` and `httpx` |

Installed into the existing `.venv`: fastapi 0.165.0, starlette 1.10.0, pydantic 2.14.4, uvicorn 0.54.0, httpx 0.28.1.

Not modified: the database, `nordiccart/etl.py`, `run_pipeline.py`, the launcher, the scheduled task and the analytics files.

## 4. Evidence

### 4.1 Tests

`tests/python/test_api.py`: **14 passed** (1.1 s). One harmless warning: Starlette deprecates using `httpx` in its test client.

### 4.2 Live API on the real analytics output (published 2026-11-20 21:06 UTC)

The script `api_evidence.py` called the running server over HTTP and compared each answer with two independent sources:
- a recomputation from the CSV files, using Python's `csv` module, not the API code;
- read-only SQL on the real `nordiccart_oltp` (session `default_transaction_read_only=on`). The SQL used only closed periods, which the hourly pipeline no longer changes.

**Overall: 27 of 27 checks PASS, 0 FAIL.**

**Health:** HTTP 200 `ok`. The files hold 5,326, 536 and 8,000 rows. The last pipeline run (21:05 UTC) has exit 0 and analytics published.

**API vs CSV recomputation:**

| Check | API | Independent |
|---|---|---|
| All-time fulfilled orders | 70,880 | 70,880 |
| All-time units | 102,944 | 102,944 |
| All-time gross revenue (EUR) | 25,945,058.40 | 25,945,058.40 |
| All-time orders placed | 73,930 | 73,930 |
| Units on hand / low-stock positions | 26,994 / 774 | 26,994 / 774 |
| SE online, October 2026: fulfilled orders / net EUR | 818 / 232,106.43 | 818 / 232,106.43 |
| `/sales/daily` SE online, October 2026: rows / local gross (SEK) | 31 / 3,248,650.00 | 31 / 3,248,650.00 |
| `/orders/monthly` 2026-10 total | 4,057 | 4,057 |
| `/inventory/current` DK stores, low stock / all rows | 85 / 8,000 | 85 / 8,000 |

**API vs the real database.**
- **Sales:** the SQL reproduces the ETL rules (local fulfilment date by country time zone, line totals, per-order EUR conversion at the fulfilment-date rate).
- **Order statuses:** placement month 2026-10 in local time.

| Country | Fulfilled orders | Gross EUR | Net EUR | Result |
|---|---|---|---|---|
| DK | 14,802 | 5,389,066.94 | 4,311,253.92 | identical |
| FI | 11,475 | 4,149,845.30 | 3,306,653.86 | identical |
| NO | 13,422 | 5,141,872.86 | 4,113,498.37 | identical |
| SE | 28,754 | 10,405,523.82 | 8,324,417.26 | identical |
| Order statuses, 2026-10 | fulfilled 3,891, cancelled 97, payment_failed 69 | | | identical |

**Error handling (live):**

| Request | Result |
|---|---|
| `country=XX` | 400 "unknown country 'XX'; known values: DK, FI, NO, SE" |
| reversed dates | 400 |
| `date_from=2026-02-30` | 422 |
| `channel=phone` | 422 |
| `month_from=202610` | 422 |
| `limit=20000` | 422 |
| `/nope` | 404 |

**Speed:** `/kpis` over all data took 25 ms once the files were loaded.

### 4.3 Live refresh across an hourly run

The API server ran without a restart while the scheduled hourly pipeline ran at 23:05 local (22:06:55 UTC).

| | Before the run (22:39 local) | After the run (23:08 local, same server process) |
|---|---|---|
| Analytics publication time (UTC) | 21:06:26 | **22:08:26** |
| Last pipeline run in `/health` | 21:05 UTC, exit 0 | **22:06 UTC, exit 0**, 39 changes, published |
| `/kpis` orders placed | 73,930 | **73,934** |
| `/kpis` units reserved | 46 | **51** |
| `/kpis` fulfilled orders / gross EUR | 70,880 / 25,945,058.40 | 70,880 / 25,945,058.40 (no fulfilment in that hour) |
| `/docs` (Swagger UI) | 200 | 200 |

**Results:**
- The API served the new hourly publication on the next request.
- The pipeline ran normally while the API was up: exit 0, 8/8 DQ PASS, analytics published.

## 5. Starting and testing

```powershell
# from the project folder; install once (already done in .venv)
.venv\Scripts\python.exe -m pip install -r python\requirements.txt

# start (Ctrl+C to stop)
.venv\Scripts\python.exe python\run_api.py              # http://127.0.0.1:8000
.venv\Scripts\python.exe python\run_api.py --port 8080  # another port

# tests (no database needed)
.venv\Scripts\python.exe -m pytest tests\python\test_api.py -q
```

**Try it:**
- **Swagger:** open http://127.0.0.1:8000/docs, choose an endpoint, then **Try it out** and **Execute**.
- **PowerShell:**
  ```powershell
  Invoke-RestMethod "http://127.0.0.1:8000/health"
  Invoke-RestMethod "http://127.0.0.1:8000/kpis?country=SE&date_from=2026-10-01&date_to=2026-10-31"
  Invoke-RestMethod "http://127.0.0.1:8000/sales/daily?country=NO&channel=online&date_from=2026-11-01"
  Invoke-RestMethod "http://127.0.0.1:8000/orders/monthly?status=cancelled&month_from=2026-01"
  Invoke-RestMethod "http://127.0.0.1:8000/inventory/current?country=DK&low_stock=true&limit=50"
  ```

## 6. Limitations and notes

- **File-replace collision (Windows):** the ETL's file replace fails if it happens during the fraction of a millisecond in which the API is reading that same file.
  - **When it can happen:** only on the first request after a publication, and only at the exact moment of the next hourly replace.
  - **If it happens:** that hourly run logs an error and exits non-zero; the next run republishes everything, since the analytics are rebuilt each run.
  - **The same applies to any reader of these files, including Power BI.**
  - **Fully removing the risk** needs a small retry around `os.replace` in `nordiccart/etl.py`. That changes the working pipeline, so it is proposed, not implemented.
- **Publication is per file:** the three files are replaced one after another within about 0.1 s. A request in that instant can combine a new sales file with the previous orders or inventory file. Each response gives `data_as_of_utc` for every source.
- **What the API serves:** the last *published* analytics. If a data-quality check fails, the ETL does not publish, and the API keeps serving the previous version. `/health` shows the last run.
- **Running it:** the API is a manual process, not a service or scheduled task, and it has no authentication.

## 7. Next

- **Power BI dashboards:** on the same `output/etl/analytics` files, or on this API.
- **GitHub publication.**
