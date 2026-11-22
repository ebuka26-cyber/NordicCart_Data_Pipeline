# NordicCart Data Pipeline: project completion report

Date: 2026-11-22. **Status: COMPLETE.** Every planned component is built, verified and running. GitHub publication is prepared and awaits the owner's approval (`GitHub_publication_readiness_report.md`).

## 1. What was built

An end-to-end pipeline for a fictional Nordic omnichannel retailer, covering four markets (SE, NO, DK, FI), 12 stores, 4 warehouses and an online shop:

**Python simulation → PostgreSQL OLTP → hourly automated ETL → FastAPI and Power BI**

| Component | Delivered |
|---|---|
| **OLTP database** | PostgreSQL 18 `nordiccart_oltp`, built by 9 forward-only migrations (V001–V009). Business rules are enforced by constraints, triggers and functions: the order life cycle, the stock ledger, purchase orders and replenishment. Every change is captured in `change_log`. 7 least-privilege roles |
| **Synthetic data** | Seeded, deterministic generators for master data (500 products, 20,000 customers, 16 locations), opening inventory, pilot orders, replenishment and exchange rates. Then a combined activity engine that writes orders, payments, stock movements, replenishment, price changes and rates one verified transaction at a time |
| **Historical load** | 2024-12-03 to 2026-10-31: 698 daily windows and 23 month-end verifications on the real database. The result is identical to the sandbox rehearsal in every fingerprint |
| **Hourly pipeline** | Windows Task Scheduler at minute 5 of every hour runs the engine tick (catching up missed hours), then the ETL: raw → cleaned → 8 data-quality checks → analytics CSVs, published only when every check passes |
| **FastAPI** | Read-only service over the analytics: `/health`, `/sales/daily`, `/orders/monthly`, `/inventory/current`, `/kpis`, with filters, paging and OpenAPI documentation |
| **Power BI** | A Power BI project stored as text, with Sales, Orders and Inventory pages. Star schema with 7 tables and 7 relationships, 35 DAX measures and 46 visuals, generated from one specification |
| **Documentation** | Portfolio `README.md` with an architecture diagram, plus guides, design documents and a report for every stage in `docs/` |

## 2. Stage history

| Stage | Result |
|---|---|
| Phase 2: database (V001–V007) | Applied and verified |
| Phase 3: generators and pilots (master data, opening inventory, 20 pilot orders, replenishment pilot) | Loaded and verified |
| S3/S4: security roles (V008), activity control and purchase orders (V009) | Deployed to the real database with backups and tested rollbacks |
| S5: exchange rates | 96 E1 rates |
| S6: activity engine | Sandbox; 525 tests |
| S7: full business scale | 500 products, 20,000 customers, demand-calibrated stock. Deployed |
| S8: full-scale rehearsal | Sandbox: 698 windows |
| S9: real historical catch-up | 698 of 698 windows verified, 23 of 23 months passed 19/19 checks, scripts 01–18 0 FAIL |
| S10: hourly pipeline and ETL | Activated 2026-11-20. Every scheduled run since has exited 0, with 8/8 data-quality checks |
| FastAPI | 14 tests; 27/27 reconciliation checks against the database. Passed review |
| Power BI | 77/77 reconciliation checks against FastAPI and an independent CSV recalculation, before and after an hourly refresh. Passed review |
| GitHub preparation | README, security scan and clean-copy reproducibility check complete. Push awaits approval |

## 3. Current figures (all data to 2026-11-22 04:06 UTC)

| Measure | Value |
|---|---|
| Orders placed / fulfilled | 74,090 / 71,035 |
| Fulfilment / cancellation rate | 95.9 % / 2.2 % |
| Fulfilled revenue | €25,996,511 gross; €20,783,463 net; €5,213,049 VAT |
| Average order value | €365.97 gross; 1.45 units per order |
| Stock positions | 8,000: 26,734 units available, 893 at or below the reorder point, 42 out of stock |
| Orders without an EUR rate | 0 |

## 4. Quality evidence

- **Tests:** 554 pytest tests collected. The database suites run only on a disposable sandbox server, never on the real database. There are also SQL behaviour suites for V006–V009.
- **Verification scripts:** 18 read-only SQL scripts, 0 FAIL on the real database.
- **Reconciliation:** the API matches the database to the cent, and Power BI matches the API and an independent recalculation.
- **Hourly operations:** 10 real runs since activation, including an automatic catch-up after the computer was off for about a day (2026-11-21 01:05 to 2026-11-22 02:15 UTC). Every run exited 0 with 8/8 data-quality checks.
- **Database safety:**
  - Backups were taken before every real-database change.
  - Every real write needed explicit authorization.
  - No failure was retried or restored without approval.

## 5. Known limitations

1. **Manual Power BI refresh.** Desktop refresh is manual. Scheduled refresh would need the Power BI Service and an on-premises gateway.
2. **Local Windows automation.** The pipeline runs as the logged-on user on a local Windows machine, with no high availability.
3. **Pinned write guard.** The engine's write guard is pinned to the original server, by design. A new server uses sandbox mode or registers its own identifier (README, "Historical simulation").
4. **`change_log` is not purged.** It grows by about 65,000 rows a month at the current volume (1.46 million over the first 23 months).
5. **Analytics files.** They are CSV files, which is enough at this volume; a larger scale would suggest a columnar format or a warehouse.

## 6. Remaining action

Publication to GitHub, after the owner's approval: `GitHub_publication_readiness_report.md`, section 5.
