# S10: Hourly automated pipeline and ETL: implementation report

Date: 2026-11-20.

**Status: COMPLETE in the sandbox, including generation after 2026-10-31. READY for real activation once you explicitly authorize it (section 6).**
- The scheduled task is registered and DISABLED.
- No scheduler credential exists yet.
- The only real-database operation was the requested `ANALYZE` (statistics only).
- No migration, schema change, new database, master-data rebuild, Airflow or new framework.

## 1. Summary of the fix (post-October generation)

**Problem:** `calendar.simulation_end = 2026-10-31` bounded both the master data and the demand calendar, so the engine could not generate anything after it.

**Fix:** one new setting, `[demand] generation_end = 2027-10-31`. It extends only the demand calendar and the exchange-rate generator by one more seasonal year with the same patterns. `calendar.simulation_end` keeps governing master data, so the 500 products and 20,000 customers are unchanged; there are no new registrations or launches.

**Guarantee that the history is unchanged:**
- **Cumulative demand sums:** split into the original calendar and the extension, so every sum inside the original calendar uses exactly the same Decimal terms as before.
- **Monthly review forecasts:** a review on or before 2026-10-31 forecasts only up to it (`forecast_limit`), exactly as the September and October 2026 reviews did.
- **Order hours:** the earliest order hour is 07:00 local, so November demand cannot fall before 2026-11-01 00:00 UTC.
- **Proven three ways:**
  - **Model level:** a new test compares the model with and without the extension. Cumulative sums, sign-up propensity, monthly order counts and the September and October 2026 forecasts are identical, not merely close.
  - **Engine level:** the first post-October window **replayed the full history from the origin and passed the drift check** against the database. The configuration fingerprint changed, so the old checkpoint state could not be reused.
  - **State hash:** the replayed start state equals the S9 final checkpoint hash `a57f6ed8…`.

## 2. Pipeline (built earlier in S10, unchanged)

- **Engine:** `run_activity.py --tick` processes every window from the last verified end to `floor(now, hour)`, at most one UTC day each, and catches up missed hours. An hour already verified writes nothing; an open run is resumed. The month-end check runs at 00:00 on the 1st.
- **Orchestrator:** `run_pipeline.py` adds an OS lock, retries for transient failures, a daily log and `runs.jsonl`.
- **ETL** (`nordiccart/etl.py`):
  - **raw:** a snapshot-checkpointed `change_log` extract, as the extractor role;
  - **cleaned:** the current state per table, idempotent by `change_id`;
  - **8 data-quality checks:** analytics are published only if all pass;
  - **analytics:** `fact_sales_daily` (local currency and EUR), `orders_monthly`, `inventory_current`.
- **Earlier evidence (2026-01-01, `nordiccart_s10_test`):**
  - a rerun wrote nothing, and all files were byte-identical;
  - a duplicate extract changed nothing (822,927 changes skipped);
  - hourly windows equal S8.2's day window (state hash `53174d0c…`).
- **ANALYZE** on the real database: 24 tables, exit 0, 9.4 s.

## 3. Post-October sandbox results (`nordiccart_s10_post`)

`nordiccart_s10_post` is a clone of `nordiccart_s82`, whose ledger is identical to the real database's for all 698 windows. The clock was set with `--now`.

| Run | Clock (UTC) | Generation | ETL and DQ | Exit / time |
|---|---|---|---|---|
| 1 | 2026-11-01 01:20 | **Run 699** [11-01 00:00, 01:00): X1 rates for 2026-11-01. **Full replay from 2024-12-03 in 51 s; drift check passed** (start state `a57f6ed8…` = S9; lost demand 344 / 825 as in S9) | initial extract **1,460,388** changes; 1,460,388 applied; **8/8 DQ PASS**; analytics published | 0 / 153 s |
| 2 | 2026-11-02 13:05 | **Run 700** [01:00, 11-02 00:00): 74 orders placed, 63 fulfilled (Sunday, online only). **Run 701** [11-02 00:00, 13:00): the November monthly review (4 locations), **4 purchase orders**, 12 transfers, 2 receipts, X1, 47 orders | **3,601** changes extracted and applied (incremental); **8/8 DQ PASS**; analytics published | 0 / 52 s |
| **3 (rerun of run 2)** | 2026-11-02 13:05 | "nothing to do up to 13:00 (up to date)", **0 windows** | **0 extracted, 0 applied** | 0 / 23 s |

**Totals:**
- orders 71,418 → **71,539**;
- movements 200,336 → 200,948;
- exchange rates 2,190 → 2,196;
- runs 698 → 701, contiguous (11-01 00:00 → 01:00 → 11-02 00:00 → 13:00).
- The ETL is complete: 1,460,388 + 3,601 = **1,463,989** = `change_log`.

**Rerun / idempotency (run 3):**
- every business count is unchanged (runs, orders, movements, rates, change_log);
- the raw file list is unchanged;
- **all 22 cleaned and analytics files are byte-identical** (SHA-256);
- only the ETL checkpoint's snapshot advanced (its own bookkeeping).

**Verification scripts on the post-October database (as `nordiccart_analyst`):**
- **15:** 35 PASS, 8 INFO;
- **16 (rates through 2026-11-02):** 4 PASS;
- **18:** 38 PASS, 6 INFO;
- **0 FAIL.**

**Tests:**
- non-database suite: 265 passed;
- after the two intended updates below, the demand and calibration tests pass, including the new extension test;
- the earlier S10 engine and guard tests: 9 of 9.

The two intended test updates:
- the pinned simulation years now include 2026-11 to 2027-10;
- the calibration's "generation window" stays the historical window (72,589 expected orders).

## 4. Files modified

**For the post-October extension:**

| File | Change |
|---|---|
| `config/generator.toml` | `[demand] generation_end = 2027-10-31` |
| `python/nordiccart/settings.py` | `Settings.generation_end` and `activity_end` (default `simulation_end`); validation (month end, not before `simulation_end`) |
| `python/nordiccart/demand/config.py` | `calendar_end` = `activity_end`; new `planning_end` = `simulation_end` |
| `python/nordiccart/demand/calendar.py` | Cumulative sums segmented at `planning_end` (history exact) |
| `python/nordiccart/demand/inventory.py` | `forecast_limit()`; R-cal reorder points and order-up-to forecasts capped for reviews up to `planning_end` |
| `python/nordiccart/demand/calibration.py` | The calibration "generation window" ends at `planning_end` (the historical design window) |
| `python/nordiccart/exchange_rates.py` | Bounds use `activity_end`; Easter 2027 closures |
| `python/run_activity.py` | Tick horizon = `activity_end` |
| `sql/verification/16_verify_exchange_rates.sql` | Rate horizon 2027-10-31; Easter 2027 closures |
| `tests/python/test_demand.py` | Year bounds include year 3; new test that the extension leaves the original calendar exactly unchanged |
| `python/setup_pipeline_credentials.ps1` | **New**: credential setup (not run) |

**Built earlier in S10:**
- new: `python/nordiccart/etl.py`, `python/run_pipeline.py`, `python/run_pipeline_hourly.cmd`, `python/register_hourly_task.ps1`, `tests/python/test_activity_real_guard.py`;
- changed: `python/run_activity.py` (`--tick`), `python/nordiccart/activity/run.py` (`end_at`, `is_month_start`).

## 5. Scheduler

| Item | Value |
|---|---|
| Task | "NordicCart Hourly Pipeline", current user, hourly at minute 5, no overlapping instances, 50-minute limit |
| Action | `cmd.exe /c python\run_pipeline_hourly.cmd`, which runs `run_pipeline.py` with `config/.env.scheduler` and `config/.env.extractor` |
| **State** | **Disabled.** The launcher's authorization line is commented out, so without it the engine guard refuses the real database (exit 3, nothing written) |

## 6. Readiness for real activation

**READY.** There are no remaining technical blockers. Activation needs your explicit authorization and these manual steps, in order:

1. **Fresh backup** (established procedure): a local window runs `pg_dump -Fc` as postgres to `database/backups/nordiccart_oltp_<timestamp>_pre_s10.dump`; you type the password.
2. **Credentials:** run `powershell -NoProfile -ExecutionPolicy Bypass -File python\setup_pipeline_credentials.ps1` in a local terminal and type the postgres password.
   - It sets random passwords for `nordiccart_scheduler` and `nordiccart_extractor`, checks that both can log in, and writes `config/.env.scheduler` and `config/.env.extractor` (git-ignored, user-only).
   - No password is displayed.
3. **Authorize writes:** in `python\run_pipeline_hourly.cmd`, uncomment `set NORDICCART_S9_REAL_AUTHORIZED=7663257437785384692/nordiccart_oltp`.
4. **One supervised run:** run `python\run_pipeline_hourly.cmd` once and check `logs/pipeline/` and `logs/activity_full_tick_*.log`. Expected:
   - a full replay with the drift check passing (start state `a57f6ed8…`);
   - catch-up from 2026-11-01 to the current hour, about 20 day windows plus hours, about 10 minutes on the sandbox timings;
   - the month-end check at 2026-12-01 once reached;
   - the initial ETL extract of about 1.46 M changes and 8/8 DQ PASS.
5. **Enable the schedule:** `Enable-ScheduledTask -TaskName "NordicCart Hourly Pipeline"`.

**Rollback:** `Disable-ScheduledTask -TaskName "NordicCart Hourly Pipeline"` and comment the authorization line again. Generated data can be reverted only by restoring the step 1 backup, with your approval.

**Operational notes:**
- The scheduler role has a 5-minute statement timeout (V008). After the 2026-11-20 `ANALYZE` the month-end checks are expected to run in seconds; if one ever times out, the run fails, is logged and is retried, and nothing is half-written.
- `change_log` is not purged by the ETL (no DELETE was added).

## 7. Next

FastAPI, Power BI dashboards (on `output/etl/analytics`) and GitHub publication.
