# S9: Real historical catch-up: execution report

Date: 2026-11-20. Target: the real `nordiccart_oltp` (localhost:5432, cluster 7663257437785384692), under your explicit authorization. Procedure: `python/s9_catch_up.py`.
- The engine (`s8-1`) wrote as `nordiccart_loader` through `config/.env`, with the exact real-target authorization.
- The postgres password was typed by you once, in a local window. It was used only for recovery points and the final read-only scripts, and was never shown or stored.

## Verdict

**S9 SUCCESS.** The historical activity from 2024-12-03 to 2026-10-31 is loaded and verified on the real database. There were no failures, no retries and no restarts.

**The real history is identical to the S8.2 rehearsal:** every one of the 698 windows has the same plan fingerprint, start and end state hashes and checkpoint hash. All S8.2 business results therefore describe the real database.

**The historical database implementation phase is closed.**

## 1. Execution

| Item | Result |
|---|---|
| Start / end | 15:12:51 preflight (real target, S7 baseline, 0 runs, no other session) → 18:07:35 driver finished |
| Windows | **698 of 698 runs `verified`**, 698 checkpoints, 2024-12-03 00:00 to 2026-11-01 00:00 UTC |
| Month-end verification | **23 of 23 months 19/19** |
| Units of work | 370,035 |
| Engine time | 9,921 s in total. Without February 2025: 3,922 s (65 min), against 51 min in S8.2 |
| Recovery points | 23 `pg_dump -Fc` archives in `database/backups/s9/`, each SHA-256 recorded on its month's last run; 461 s in total |
| Errors | None. Every month exit 0; nothing retried, restored or restarted |

**One anomaly (performance only, no data effect):**
- **What happened:** February 2025's 28 windows ran normally (15:17–15:18). Its **month-end verification then took 98 minutes** (15:18 → 16:56), against seconds in every other month and in S8.2. It passed 19/19, and every later month ran at normal speed (2–4.5 min a month including its check).
- **Probable cause:** a query plan made before PostgreSQL's automatic statistics refresh. Autoanalyze on `orders`, `order_items` and `inventory_movements` ran at 15:24, six minutes into the check.
- **Effect:** none on the data. Nothing was changed or restarted.
- **Recommendation for the hourly pipeline:** run `ANALYZE` on the activity tables after large batch loads. This is an operational note, not a defect.

## 2. Verification (completed)

| Check | Result |
|---|---|
| Ledger against the S8.2 rehearsal (sandbox `nordiccart_s82`) | **Identical for all 698 runs**: window, status, plan and config fingerprints, start and end state hashes, planned and completed units, checkpoint hash |
| Scripts 01–18, read-only (as `postgres`) | **888 rows: 712 PASS, 173 INFO, 0 FAIL**, 3 known WARN (07: 2, 10: 1). Script 15 took 11.9 s on the full history (S8 original: 101 min); script 18 took 59 s |
| Pilot `--plan` (read-only, loader) | Transactions: **0 conflicts, 0 blocking** (71,398 later orders, 42 open). Replenishment: **0 conflicts, 0 blocking**; POs 4/4, R0 4/4, 329 later revisions, 0 to write |
| Original records | **Byte-identical to the pre-S9 backup**: full rows including IDs and timestamps for 20 orders, 26 lines, 21 payments, 23 reservations, 6,134 movements, 4 POs and lines, 69 R0 revisions and 96 rates |
| Recovery points | 23 of 23 recorded; 23 of 23 file SHA-256 values match the ledger |

## 3. Final state of the real database

| Measure | Value |
|---|---|
| Orders | 71,418 (71,398 historical + 20 pilot), 42 open at 2026-10-31 |
| Order lines / payment attempts | 96,430 / 71,580 |
| Inventory movements | 200,336 |
| Purchase orders / lines | 400 / 17,695 |
| Prices (incl. 1,353 markdowns) / reorder point revisions | 3,353 / 5,867 |
| Exchange rates | 2,190 (2024-11-01 to 2026-10-31) |
| change_log | 1,460,385 rows (for the Phase 4 extraction) |
| Database size | 1,238 MB |

Business figures, identical to S8.2:
- 71,742 orders demanded (−1.2% against the model), 344 lost (0.48%);
- €25.13 M fulfilled revenue (€10.53 M growth year, €14.60 M primary year);
- markets SE 42.0%, DK 21.6%, NO 19.6%, FI 16.8%; online 54.8%;
- primary-year turnover 2.45 unit turns (1.64 at cost) and unit fill 99.20%;
- known, accepted limitation: turnover below 4–6 because of the full-range store display minimums.

## 4. Recovery points retained

Retention follows the S8 procedure, design C14 (the quarter ends from March 2025 and the final point):
- **8 archives kept** in `database/backups/s9/`: 2025-03, 06, 09, 12 and 2026-03, 06, 09, 10. They total 540,181,163 bytes; the final one (2026-10) is 110,523,893 bytes.
- **15 removed** after their SHA-256 matched the ledger; the ledger keeps every name and checksum. Log: `logs/s9/retention.json`.
- The pre-S9 backup `database/backups/nordiccart_oltp_20261120_143552_pre_s9.dump` is kept (SHA-256 `F50E8219…619DE5`).

## 5. Changes made for S9 (reviewed in the readiness report)

- **Write guard:** `python/nordiccart/activity/run.py` gained the explicit real-target authorization, covered by `tests/python/test_activity_real_guard.py` (7 tests).
- **Driver:** `python/s9_catch_up.py`.
- No migration, schema, policy or engine-logic change.

## 6. Evidence

- `logs/s9/`:
  - `driver.log`, `progress.jsonl` and `engine_<YYYY-MM>.log` (23 files);
  - `scripts_final.txt`, `post_verification.json`, `pilot_plans.txt`, `retention.json`.
- Readiness: `docs/test_reports/S9_deployment_readiness.md`.

## 7. Phase closure and next step

**The historical database implementation phase (S1–S9) is closed.** The real database holds verified activity through 2026-10-31.

**Next priority** (the original project objectives):
- hourly automation (incremental windows after 2026-10-31);
- the ETL pipeline from `change_log`;
- FastAPI;
- Power BI.
