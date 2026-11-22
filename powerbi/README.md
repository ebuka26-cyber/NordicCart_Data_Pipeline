# powerbi

The NordicCart Power BI report is a Power BI Project (`.pbip`): a TMDL semantic model and a PBIR report, all stored as text.

It reads the analytics that the hourly ETL publishes in `output/etl/analytics`; it never reads the OLTP database. Each file is published only after all 8 data-quality checks pass. The same files feed the FastAPI service (`python/run_api.py`), so the two always agree.

## Contents

| Path | What it is |
|---|---|
| `NordicCart.pbip` | Open this in Power BI Desktop |
| `NordicCart.SemanticModel/` | Model: tables, Power Query sources, relationships, DAX measures (TMDL) |
| `NordicCart.Report/` | Report: 3 pages, 46 visuals, NordicCart theme (PBIR) |
| `MEASURES.md` | Every DAX measure with its format, meaning and formula |
| `screenshots/` | The three pages after a refresh |
| `build_pbip.py` | Generates the model, the report and `MEASURES.md` from one specification (`python powerbi/build_pbip.py`) |

## Opening and refreshing

1. Open `NordicCart.pbip` in Power BI Desktop (version 2.159 or later), and set the data folder (below) if needed.
2. Choose **Refresh**. Loading the three CSV files takes about 6 seconds.
3. After any hourly ETL run, **Refresh** again to see the new publication. The "Data As Of" card shows when the files were published.

**Data folder:** before the first refresh, point the parameter `AnalyticsFolder` at your clone's `output\etl\analytics` folder. Power Query has no relative paths, so the committed project uses a neutral placeholder, `C:\NordicCart_Data_Pipeline\output\etl\analytics`, and contains no machine-specific path. There are two ways to set it:
- in Desktop: **Transform data**, then **Edit parameters**, then **AnalyticsFolder**; or
- `python powerbi/build_pbip.py --local` regenerates the project with this clone's absolute folder. That copy is for local use only: run `python powerbi/build_pbip.py` (no option) before committing to restore the placeholder.

## Reporting rules (database/design/nordiccart_oltp_rev3.dbml, "Reporting rules for Power BI")

- **Revenue:** counted when an order is fulfilled, by local fulfilment date. VAT is calculated per line.
- **Currency:** EUR uses the rate of the fulfilment date (computed in the ETL). Local-currency revenue is never added across currencies: `Gross Revenue Local` is blank when the context holds more than one currency.
- **Orders:** counted by local placement month and current status.
- **Inventory:** the current snapshot. Low stock means available (on hand − reserved) ≤ reorder point.
