# S10 activation status (real database)

Date: 2026-11-20. Target: the real `nordiccart_oltp` (localhost:5432, cluster 7663257437785384692), under your authorization.

**Result: S10 ACTIVATED.**
- The supervised run succeeded: 20 windows verified, the ETL complete, **8/8 data-quality checks PASS**.
- The hourly task "NordicCart Hourly Pipeline" is **enabled (Ready)**.
- S10 is closed.

## 1. Steps completed before resumption (Codex; evidence `logs/pipeline/s10_activation_local_status.txt`)

| Step | Evidence |
|---|---|
| Pre-S10 backup | `database/backups/nordiccart_oltp_20261120_203554_pre_s10.dump`, 110,523,954 bytes, SHA-256 `56543C75F677879B47A5FA9DA36405BB1571400F4D423CE4FCED7A0F0731E6F2`, verified |
| Credentials | `setup_pipeline_credentials.ps1` exit 0. `config/.env.scheduler` and `config/.env.extractor` exist; both roles' logins were checked by the script; files restricted to your account. Not opened or changed here |
| Authorization | `python/run_pipeline_hourly.cmd` has `set NORDICCART_S9_REAL_AUTHORIZED=7663257437785384692/nordiccart_oltp` active |

## 2. Supervised run (found running at resumption; not duplicated)

- **Process:** `run_pipeline.py` (PID 59604) started at 20:37:15 local through the launcher.
- **When I resumed:** generation had finished and the ETL was running. I waited for it; nothing was started again.
- **Result:** exit 0 in 292.6 s (`logs/pipeline/runs.jsonl`, `logs/pipeline/pipeline_2026-11-20.log`).

### 2.1 Generation (as `nordiccart_scheduler`)

- **History check:** the engine replayed the full history from 2024-12-03, because the configuration fingerprint changed to `8672aa88…`. The drift check passed: start state `a57f6ed8…` equals the S9 final checkpoint. History unchanged.
- **Windows:** **20 verified, runs 699–718**:
  - 19 day windows, 2026-11-01 to 2026-11-20 00:00 UTC;
  - one window to **2026-11-20 19:00 UTC**, the hour floor at run time.
- **Time:** 195.7 s, exit 0.
- **Activity generated:** **2,502 orders** (first 2026-11-01 06:37 UTC, last 2026-11-20 18:50 UTC), plus:
  - daily store transfers and supplier receipts;
  - weekly purchase orders;
  - the November monthly review;
  - P1 markdowns;
  - exchange rates through 2026-11-20 (2,250 rows).

**Real database after the run (read-only check):**

| Measure | Value |
|---|---|
| Run ledger | **718 runs, 718 verified, 718 checkpoints**, contiguous to 2026-11-20 19:00 UTC |
| Orders | 73,920 (71,418 at S9 + 2,502) |
| Inventory movements | 207,291 |
| change_log | 1,510,626 |
| Other sessions | 0 |

### 2.2 ETL (as `nordiccart_extractor`)

| Layer | Result |
|---|---|
| Raw | Initial extract **1,510,626 changes**, equal to `change_log`, in `output/etl/raw/change_log_20261120T194033112380Z_219_1510844.jsonl.gz` (53.4 MB). Consumer checkpoint `nordiccart_etl` created |
| Cleaned | 1,510,626 applied, 0 skipped. 19 tables, e.g. orders 73,920, order_items 99,836, payment_attempts 74,086, inventory_movements 207,291, reservations 99,833, customers 20,000, exchange_rates 2,250, inventory_balances 8,000 |
| Analytics | Published in `output/etl/analytics/`: `fact_sales_daily.csv` (5,326 rows), `orders_monthly.csv` (536), `inventory_current.csv` (8,000); 0 orders without an EUR rate |

### 2.3 Data-quality checks: 8 of 8 PASS

| # | Check | Violations |
|---|---|---|
| 1 | Tables whose cleaned row count differs from the database | 0 |
| 2 | Order lines without their order | 0 |
| 3 | Payment attempts without their order | 0 |
| 4 | Orders without their customer | 0 |
| 5 | Movements with an unknown product or location | 0 |
| 6 | Orders whose items total or order total does not add up | 0 |
| 7 | Negative or oversold balances | 0 |
| 8 | Orders with an unknown status or channel | 0 |

## 3. Windows Task Scheduler

Enabled only after the supervised run succeeded and all 8 checks passed.

| Item | Value |
|---|---|
| Task | "NordicCart Hourly Pipeline" |
| **State** | **Ready (enabled)** |
| Action | `cmd.exe /c "…\python\run_pipeline_hourly.cmd"` |
| Trigger | Every hour (PT1H) at minute 5; next run 2026-11-20 21:05 local |
| Settings | No overlapping instances (IgnoreNew); 50-minute limit; runs as the current Windows user (interactive: while you are logged on) |

Each run executes the engine tick (the windows up to the current hour; an hour already verified writes nothing) and then the incremental ETL, data-quality checks and analytics. Logs: `logs/pipeline/`.

**First automatic run (verified):** the task started at 2026-11-20 21:05:01 local, with result **0**; next run 22:05.

| Step | Result |
|---|---|
| Generation | **1 hourly window, [19:00, 20:00) UTC, run 719 verified**: 4 orders placed, 3 payments, 1 expiry. Catch-up stops at the current hour |
| ETL raw | **35 changes** extracted incrementally (`change_log_20261120T200528448375Z_1510845_1510879.jsonl.gz`), continuing from the activation run's checkpoint |
| ETL cleaned | 35 applied, 0 skipped |
| Data quality | **8 of 8 PASS** |
| Analytics | Published (`fact_sales_daily` 5,326, `orders_monthly` 536, `inventory_current` 8,000) |
| Pipeline | Exit 0 in 44.4 s |

## 4. Remaining blockers

None for S10.

Operational notes:
- The task runs only while you are logged on (interactive principal). Missed hours are caught up by the next run.
- Synthetic generation is configured through `[demand] generation_end = 2027-10-31`.
- `change_log` is not purged by the ETL.
- **Rollback:**
  - `Disable-ScheduledTask -TaskName "NordicCart Hourly Pipeline"`;
  - comment the authorization line in the launcher;
  - data changes can be reverted only from the pre-S10 backup, with your approval.

## 5. Stage closed

S10 is complete. Next priorities: **FastAPI, Power BI dashboards, GitHub publication.**
