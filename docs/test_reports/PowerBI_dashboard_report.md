# Power BI dashboards: implementation report

Date: 2026-11-20.

**Status: COMPLETE. A working Power BI Project, built, opened and refreshed in Power BI Desktop 2.159 on this machine.**
- **Contents:** three report pages with 46 visuals, on a semantic model with 7 tables, 7 relationships and 35 DAX measures.
- **Reconciliation:** all 77 checks pass, both before and after an hourly pipeline run and refresh. The model's figures match the FastAPI endpoints and an independent recomputation from the analytics files; FastAPI itself matches the real database to the cent for closed periods.
- **Pipeline:** one ETL change, the minimal retry you allowed for Windows file-replace conflicts. The database, the historical simulation and hourly generation were not touched.

## 1. ETL retry for Windows file-replace conflicts (the only pipeline change)

**Problem:** on Windows, `os.replace` fails with `PermissionError` while any reader has the target file open. The FastAPI report showed this, and a Power BI refresh is such a reader too.

**Change** (`python/nordiccart/etl.py`, `_atomic_write`):
- On `PermissionError` only, the replace is retried every 0.5 s, up to 20 attempts (10 s). Each retry is logged as a warning.
- After the last attempt the error is raised exactly as before, so the run fails visibly, never silently.
- Nothing else changed: same files, same order, same content.

**Tests** (`tests/python/test_etl_replace_retry.py`, 2 tests):
- a reader holding the file for 0.35 s delays publication but doesn't fail it, and leaves no `.tmp` file;
- a reader that never lets go still raises `PermissionError` after the attempts, with the old file intact.

These and the 14 API tests pass: **16 of 16**. The first scheduled run on the new code (00:05, exit 0, 8/8 DQ PASS, no retry needed) is in section 6.2.

## 2. Files

| File | Purpose |
|---|---|
| `powerbi/NordicCart.pbip` | **Open this in Power BI Desktop** |
| `powerbi/NordicCart.SemanticModel/` | TMDL model: `model.tmdl`, `database.tmdl` (compatibility level 1606), `expressions.tmdl` (parameter), `relationships.tmdl`, `tables/*.tmdl` (7) |
| `powerbi/NordicCart.Report/` | PBIR report: `report.json`, 3 pages, 46 `visual.json` files, base theme Fluent2-CY26SU10 and a custom NordicCart theme |
| `powerbi/MEASURES.md` | All 35 measures: DAX, format, meaning (generated from the same specification) |
| `powerbi/build_pbip.py` | Generator for the model, report and `MEASURES.md` from one readable specification. Deterministic: regenerating gives byte-identical files. With no option it writes the portable GitHub version; `--local` writes this machine's data folder |
| `powerbi/screenshots/*.png` | The three pages after refresh |
| `powerbi/README.md` | How to open, refresh and change the data folder (replaces the placeholder) |
| `powerbi/.gitignore` | Excludes Desktop's local `.pbi/cache.abf` and `localSettings.json` |
| `python/nordiccart/etl.py` | Retry (section 1) |
| `tests/python/test_etl_replace_retry.py` | New, 2 tests |

## 3. Data model

**Source:** the hourly ETL's analytics CSVs only, through the Power Query text parameter **`AnalyticsFolder`**. The committed default is the neutral placeholder `C:\NordicCart_Data_Pipeline\output\etl\analytics` (section 8). Columns are typed with the `en-US` culture; money columns are Fixed Decimal (`Currency.Type`), so sums are exact to the cent.

| Table | Source | Grain / content |
|---|---|---|
| `FactSales` | `fact_sales_daily.csv` | Fulfilment date (local) × country × channel × currency: orders, lines, units, gross / VAT / net in local currency and EUR |
| `FactOrdersMonthly` | `orders_monthly.csv`, plus `month_start` (the first day of `month_local`) | Placement month (local) × country × channel × status: orders |
| `FactInventory` | `inventory_current.csv`, plus `available` = on hand − reserved; `low_stock` = available ≤ reorder point; `stock_status` (In stock, Low stock or Out of stock) | Location × SKU, the current snapshot |
| `DimDate` | DAX calendar from 1 January of the first data year to 31 December of the last; **marked as the date table** | Date, Year, Quarter, Month, Year Month (sorted by a key), Month Start |
| `DimCountry` | Static: DK, FI, NO, SE with name and currency | Country |
| `DimChannel` | Static: `online` (Online), `in_store` (In store) | Channel |
| `DataFreshness` | `Folder.Files` metadata of the three CSVs | When each file was published (its modification time) |

**Relationships:** all 7 are many-to-one with single-direction filtering.

| From | To |
|---|---|
| `FactSales[fulfilment_date_local]` | `DimDate[Date]` |
| `FactOrdersMonthly[month_start]` | `DimDate[Date]` |
| `FactSales[market]`, `FactOrdersMonthly[market]`, `FactInventory[market]` | `DimCountry[country_code]` |
| `FactSales[channel]`, `FactOrdersMonthly[channel]` | `DimChannel[channel_code]` |

**Model choices:**
- Numeric fact columns are hidden, and implicit measures are discouraged, so every visual uses the defined measures.
- Inventory has no date, because it is a snapshot.
- Channel doesn't apply to inventory.

## 4. DAX measures (35; full list with formulas in `powerbi/MEASURES.md`)

**Sales (13):**
- **Totals:** Gross / Net Revenue EUR, VAT EUR, Fulfilled Orders, Order Lines, Units Sold.
- **Ratios:** Average Order Value EUR, Units per Order, Share of Gross Revenue (against all countries and channels in the period).
- **Year over year:** Gross Revenue EUR PY, Gross Revenue YoY %.
- **Gross Revenue Local:** blank whenever more than one currency is in context, per the reporting rule.
- **Orders Without EUR Rate:** expected 0.

**Orders (10):**
- **Counts:** Orders Placed, Orders Fulfilled, Orders Cancelled, Orders Payment Failed, Orders In Progress.
- **Rates:** Fulfilment Rate, Cancellation Rate, Payment Failure Rate.
- **Year over year:** Orders Placed PY, Orders Placed YoY %.

**Inventory (11):**
- **Units:** On Hand, Reserved, Available.
- **Positions:** Stock Positions, Low Stock Positions, Out of Stock Positions, Low Stock Rate, SKUs with Low Stock.
- **Low-stock table:** Low Stock Available, Low Stock Reorder Point, Reorder Shortfall Units. They are blank outside low-stock positions, so the table lists only low-stock rows.

**Status (1):** Data As Of.

**Year-over-year design:**
- **Like for like:** the comparison covers the selected dates only up to the last date with data. So 2026 (1 January to 20 November) is compared with 1 January to 20 November 2025, not with all of 2025.
- **Blank when the prior year is incomplete:** YoY is blank when the prior-year period starts before the data, which starts in November 2024. So 2025 against 2024 and the "all dates" view show blank, not a misleading +798%.
- **Orders:** orders are monthly, so Orders Placed YoY compares whole months, and the current month is partial.

## 5. Dashboard pages and visuals

Each page has the same layout:
- **header:** title, a one-line definition of the data, and a "Data As Of" card;
- **slicers;**
- **six KPI cards;**
- **two charts on the middle row, then a chart and a table or matrix below.**

| Page | Slicers | KPI cards | Visuals |
|---|---|---|---|
| **Sales performance** | Fulfilment date (between), Country, Channel | Gross Revenue EUR, Net Revenue EUR, Fulfilled Orders, Average Order Value EUR, Units Sold, Units per Order | Line: gross revenue by month and country. Bar: gross revenue by country. Clustered column: average order value by country and channel. Table: country × channel with orders, units, gross, net, average order value, units per order and share |
| **Order performance** | Placement month, Country, Channel | Orders Placed, Fulfilment Rate, Cancellation Rate, Payment Failure Rate, Orders Cancelled, Orders Payment Failed | Stacked column: orders by month and status. Line: cancellation and payment-failure rate by month. Matrix: country × status. Bar: fulfilment rate by country and channel |
| **Inventory management** | Country, Location type, Category, Stock status | Units On Hand, Units Available, Units Reserved, Stock Positions, Low Stock Positions, Out of Stock Positions | Bar: on hand and available by country. Bar: available by location. Donut: positions by stock status. Table: low-stock positions (country, location, SKU, category, available, reorder point, shortfall), largest shortfall first |

**Styling:** the custom theme has a muted Nordic palette, white cards with rounded borders on a light-grey page, and consistent title styles. The auto-generated chart subtitles are turned off.

Screenshots: `powerbi/screenshots/1_sales_performance.png`, `2_order_performance.png`, `3_inventory_management.png`.

## 6. Validation and refresh results

**Method:**
1. The project was opened in Power BI Desktop 2.159.
2. The model was refreshed through Desktop's own local engine with a TMSL `refresh full` command; all 7 partitions reached state Ready with no errors in about 6 seconds.
3. DAX queries ran against the loaded model, using the ADOMD client that ships with Desktop.
4. Each result was compared with FastAPI, reading the same publication, and with an independent recomputation from the CSV files.

### 6.1 Reconciliation (publication 2026-11-20 22:08:26 UTC): **77 of 77 PASS**

| Area | Checks | Examples (Power BI = reference) |
|---|---|---|
| All data vs `/kpis` | 16 | Fulfilled orders 70,880. Units 102,944. Gross €25,945,058.40. Net €20,742,344.47. VAT €5,202,713.93. Orders placed 73,934. On hand 26,994. Reserved 51. Available 26,943. 8,000 positions; 774 low stock, 40 out of stock. Publication time to the second |
| SE · online · October 2026 vs `/kpis` | 9 | 818 fulfilled. Net €232,106.43. Gross €290,133.13. Average order value €354.69. 881 placed. Fulfilment 92.74%, cancellation 4.88%, payment failure 2.38% |
| Each country, fulfilment dates up to 2026-10-31 vs `/kpis` | 16 | DK 14,802 / €5,389,066.94 / €4,311,253.92 … SE 28,754 / €10,405,523.82 / €8,324,417.26; shares 21.48 / 16.54 / 20.50 / 41.48%. **These API figures equal the real database to the cent (FastAPI report §4.2)** |
| Orders placed 2026-10 vs `/kpis` | 5 | Fulfilled 3,891, cancelled 97, payment failed 69, placed 4,057. Rates 95.91 / 2.39 / 1.70% |
| Inventory by country vs `/kpis` | 16 | e.g. DK on hand 6,121, available 6,106, low stock 229, out of stock 18 |
| Low-stock table vs `/inventory/current?low_stock=true` | 3 | 774 rows. Largest shortfall 6. Total shortfall 355 |
| Time intelligence and local currency vs CSV | 12 | **Revenue:** gross January–October 2026 €11,002,113.98. **Prior year:** gross PY = January–October 2025, €9,092,668.10. **Like-for-like YoY 2026:** PY €9,842,540.24 (1 January to 20 November 2025), YoY +20.51%. **Blanks:** 2025 YoY and all-dates YoY blank. **Local currency:** gross revenue per currency (e.g. SEK 119,971,745) and blank across currencies |

Also checked: Orders Placed YoY for 2026 is +15.55%, with 34,722 orders against 30,049. Both totals match a recomputation from `orders_monthly.csv`.

### 6.2 Refresh after an hourly pipeline run

**The hourly run:** the scheduled run at 00:05 local (started 23:05:02 UTC) was the **first run on the ETL code with the new retry**.

| Step | Result |
|---|---|
| Generation | Exit 0 |
| ETL extract | **0 new changes.** The hour [22:00, 23:00) UTC had no new changes to extract |
| Data-quality checks | **8 of 8 PASS** |
| Analytics | Republished, as on every run, at **2026-11-20 23:06:10 UTC**: `fact_sales_daily` 5,326, `orders_monthly` 536, `inventory_current` 8,000 |
| Pipeline | **Exit 0** in 68.0 s |
| Retry warnings ("in use by a reader") | **0**. No file-replace conflict occurred |

**First refresh attempt (not used as evidence):**
- My first attempt attached to the Desktop instance used for section 6.1. The connection was lost after 624 s, with nothing returned.
- That instance had stopped responding and was ended from Task Manager.
- I didn't use or retry anything from that attempt.

**Verified refresh:**
1. **A separate Desktop instance:** a scratch copy of the project, byte-identical to the GitHub version in `powerbi/` except for the `AnalyticsFolder` parameter, opened in its own Power BI Desktop instance.
2. **Engine and refresh:** the script found that instance's engine through its process ID, then ran a TMSL `refresh full`. All 7 partitions reached Ready, with no errors, in 9.1 s.
3. **Reconciliation:** the full check from section 6.1 against FastAPI, which serves the same new publication.
4. **Cleanup:** the instance was then closed; it was the only process stopped.

**Before and after the run:**

| | Before the run (section 6.1) | After the run and refresh |
|---|---|---|
| Publication loaded ("Data As Of", UTC) | 22:08:26 | **23:06:10**, equal to FastAPI `/health` to the second |
| Reconciliation | 77 of 77 PASS | **77 of 77 PASS** |
| Fulfilled orders / gross EUR / orders placed / units on hand | 70,880 / €25,945,058.40 / 73,934 / 26,994 | 70,880 / €25,945,058.40 / 73,934 / 26,994 |

**Results:**
- **Unchanged figures:** the business figures are the same, which is correct, because the hour had no new changes.
- **The refresh picked up the new publication:** it loaded the newly published files, as the publication time shows.
- **No conflict:** the pipeline published normally, with no file-replace conflict.

## 7. Problems found and fixed during the build

1. **Desktop hung on "Working on it":**
   - **Cause:** two columns in `DimChannel`, `channel` and `Channel`. The tabular engine treats column names case-insensitively, and Desktop showed no error.
   - **How I found it:** I ran the TMDL through Desktop's own `TmdlSerializer`, which reported "Item 'Channel' already exists".
   - **Fix:** the key is now `channel_code`. The build now validates the TMDL with that serializer before opening.
2. **Report rejected:** `layoutOptimization` is not allowed by report schema 3.3.0. I removed it. All schema versions now match what Desktop 2.159 writes.
3. **YoY design:** a naive `SAMEPERIODLASTYEAR` compared partial years and gave +798% for 2025. I replaced it with the like-for-like and blank rules in section 4.
4. **A mistake in my own check:** I first labelled "Data As Of" as UTC. The model value is local time; the one-hour gap came from PowerShell's JSON date conversion in my check script. The label and the check are now corrected, and the publication time matches FastAPI to the second.

## 8. Steps that need you in Power BI Desktop

1. **Open and refresh:** open `powerbi/NordicCart.pbip` and click **Refresh**. A project stored as text holds no data until it is refreshed. Then click **Save**.
   - **What Save does:** it stores a local data cache in `.pbi/`, which `.gitignore` excludes, and Desktop may reformat the files it rewrites.
2. **If Desktop shows "Apply external changes":** this happens when the project files change on disk while the project is open (for example after `build_pbip.py`). Apply them, or close and reopen the project, before refreshing.
3. **Refresh after hourly runs:** Desktop doesn't refresh by itself. Click **Refresh** after an hourly run; the "Data As Of" card shows which publication is loaded.
4. **The data folder in the GitHub version (done):**
   - **Placeholder path:** the committed project contains no machine-specific path. `AnalyticsFolder` defaults to `C:\NordicCart_Data_Pipeline\output\etl\analytics`.
   - **Verified:** a search of every text file under `powerbi/` finds no user name or `C:\Users` path.
   - **Only one file differs:** the GitHub version and the `--local` version (the one opened and refreshed in Desktop above) differ only in `expressions.tmdl`, the parameter's default value. Every other model and report file is byte-identical.
   - **Screenshots:** the images show only the report canvas, with no account details.
   - **Your step:** on this machine, before the first refresh, either set **Transform data → Edit parameters → AnalyticsFolder** to `<project>\output\etl\analytics`, or run `python powerbi/build_pbip.py --local`. If you use `--local`, run `python powerbi/build_pbip.py` again before committing.
5. **Optional, out of scope:** publishing to the Power BI Service and scheduling refresh there needs a Power BI account and an on-premises data gateway for the local CSV folder.

## 9. Limitations

- **Refresh is manual** in Desktop.
- **Order dates:** orders are monthly, so the Orders page filters by placement month. A day-level date filter would select whole months by their first day.
- **Publication is per file:** the three files are replaced one after another within about 0.1 s. A refresh that starts in that instant can combine two publications; refreshing again resolves it. "Data As Of" shows the newest file.
- **Inventory is the current snapshot,** with no history: the ETL publishes no inventory history.

## 10. Next

GitHub publication and portfolio documentation.
