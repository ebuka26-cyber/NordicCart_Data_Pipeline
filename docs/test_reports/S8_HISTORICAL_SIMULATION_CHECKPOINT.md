# NordicCart project checkpoint: S8 full-scale historical rehearsal (sandbox)

Last updated: 2026-11-20. **S9 COMPLETE: the real historical catch-up succeeded, and the historical database implementation phase is CLOSED.**

## S9 (2026-11-20): real deployment

Under explicit authorization, `python/s9_catch_up.py` loaded 2024-12-03 to 2026-10-31 into the real `nordiccart_oltp`.
- **Run:** 698 of 698 windows verified; 23 of 23 months 19/19; the ledger is identical to S8.2 for every window.
- **Checks:** scripts 01–18 0 FAIL; pilot plans 0 conflicts; original records identical to the pre-S9 backup.
- **Recovery points:** 23 recorded (SHA-256 verified), 8 retained in `database/backups/s9/`.
- **Final:** 71,418 orders, €25.13M, 1,238 MB.
- **Anomaly:** February 2025's month-end check took 98 minutes, with no data effect.

Report: `docs/test_reports/S9_execution_report.md`.

**Next:** hourly automation, ETL, FastAPI, Power BI (the original project objectives).

---

S8 and S8.1 passed ChatGPT's review. S8.2 (final full-scale rehearsal) completed in the sandbox: PASS, final review passed.

## S8.2 (2026-11-20): final full-scale rehearsal

Report: `docs/test_reports/S8_2_final_full_scale_rehearsal_report.md`.

**Decision applied (S8.1 review):** keep the 500-product assortment, the display minimums and the corrected policy; no further calibration changes. Engine `s8-1`, unchanged.

**Run:** `nordiccart_s82`, a fresh clone of `nordiccart_s7_full`, 2024-12-03 to 2026-10-31.
- **698 of 698 windows verified**; 698 checkpoints; **23 of 23 months 19/19**; no errors; engine 51 min.
- S8.1 and S8.2 are identical over their 394 shared windows (plans and state hashes).

**Verification:**
- scripts 01–18 as `nordiccart_analyst`: 888 rows, **0 FAIL**, 3 known WARN, 48.5 s in total (optimized script 15);
- pilot plans 0 conflicts;
- original records byte-identical to S7.

**Recovery points:**
- 23 recorded with SHA-256;
- **final point verified**: SHA-256 matches the ledger; restore 58 s; 24 tables full-row identical; recomputed state hash equals the checkpoint;
- retention as in S8: 8 kept (quarter ends from 2025-03 plus the final point), 540 MB.

**Against S8, primary year:**
- turns 2.21 → **2.45** units, 1.56 → 1.64 at cost; average stock −9.7%;
- warehouse stock −21%, cover 8.5–10.3 weeks January–September 2026 (in target);
- unit fill 99.16% → 99.20%;
- transfers equal; PO lines +23%, PO units −3%.

**Whole period:**
- orders 71,398 against 71,409 (same demand, 71,742);
- revenue €25.13M against €25.15M;
- lost orders 344 against 333 (0.48% against 0.46%).

**New errors:** none. **Material regressions:** none.

**Known limitation (accepted):** below the 4–6 turnover target, because the display minimums of the full range in every store hold about 60% of network stock.

## S8.1 (2026-11-20): focused corrections

Report: `docs/test_reports/S8_1_calibration_and_performance_report.md`.

**1. Inventory**
- **Change:** S (order-up-to) is recomputed at every monthly R-cal from the demand forecast over the weeks of cover (stores 6, warehouse echelon 10), by `rcal_cover_units` and `order_up_to(loc, sku, day)`. `ENGINE_VERSION` is `s8-1`. Configuration, s, launch rules, review schedules and pilot records are unchanged. December 2024 is identical to S8.
- **Representative run:** new sandbox clone `nordiccart_s81` (from `nordiccart_s7_full`), 2024-12-03 to 2025-12-31.
  - 394 of 394 windows verified; 13 of 13 months 19/19.
  - Scripts 01–18 as analyst 888 rows, 0 FAIL; pilot plans 0 conflicts; original records full-row identical.
- **2025, against S8:**
  - average stock −8.8%;
  - warehouse stock −19.6%, network cover 9–11 weeks (target 8–12);
  - turns 2.06 → 2.25 units, 1.49 → 1.56 at cost;
  - lost units 401 → 403 (fill 99.19% in both);
  - transfers −1.1%; PO lines +24%, PO units −3%.
- **Limit:** stores hold the display minimum of all 500 products (about 13,000–14,000 units). The ceiling is about 3.8 turns even with empty warehouses, so **4–6 turns needs an assortment or display decision** (report 2.4). Transfer schedule left unchanged: no material problem from the new policy.

**2. Script 15:** CTEs `res`, `sales`, `pay` are `AS NOT MATERIALIZED`. Full 23-month history: 101 min → **6.2 s as `nordiccart_analyst`** (15-minute timeout). Same 43 checks, 35 PASS / 8 INFO / 0 FAIL. Equal to the saved original S8 run on the same unchanged database: 43 checks, 35 PASS and 8 INFO; the INFO values match the S8 records. A redundant rerun of the original was confirmed read-only and cancelled after 71 minutes. Mutation and pinned-result tests 43/43.

**3. Tests:**
- three new tests (forecast S, S in force per date, out-of-calendar month);
- engine module 27/27;
- script 15 DB tests 43/43;
- non-database suite 260 passed.

**Readiness:** both corrections are ready for a final full-scale rehearsal (fresh S7 clone, 2024-12-03 to 2026-10-31). First decide the turnover target (recommended: accept about 2–3 unit turns for a full-range display assortment). Preserved: `nordiccart_s8`, the eight S8 recovery points and `nordiccart_s7_full`. The 23-month simulation was not rerun.

---

S8 record (unchanged below). **S8 rehearsal COMPLETE in the sandbox: 2024-12-03 to 2026-10-31 (last simulated date 2026-10-31).**

**Operational verdict: PASS. Inventory-turnover calibration: FAIL.** Final SQL transcripts cover all 18 scripts (712 PASS, 173 INFO, 3 expected WARN, 0 FAIL). October restore and full-row pilot comparisons passed. No simulation or audit restart.

## Completed
- S1–S7 complete. S7 (full profile) is deployed to the real database and reviewed.
- **S8 rehearsal** on `nordiccart_s8` (a clone of the S7 state, identical to the real database before the run):
  - 698 of 698 day windows verified; 23 of 23 month-end verifications 19/19; 698 checkpoints.
  - 71,409 simulated orders (+20 pilot), against 72,635 expected: −1.2% (−1.7% placed only). Lost demand 333 orders (0.46%). Markets and the online share (54.8%) match the model.
  - 1,353 P1 markdowns; 5,798 R-cal revisions; 2,190 exchange rates; 400 purchase orders; 195,177 movements; change_log 1.44 million; database 1.17–1.23 GB.
  - Scripts 01–18: 888 rows, **0 FAIL**. Pilot `--plan` checks 0 conflicts. Pilot records byte-identical.
  - Recovery points:
    - 23 taken and recorded in the ledger;
    - Independent audits completed for 14 months through January 2026, plus the October 2026 final restore (15 distinct months). The remaining eight intervening months have recorded backups without completed independent restore audits. October matched the recorded archive SHA-256, source counts and final checkpoint hash.
    - retention leaves 8 files (quarter ends plus final), 511 MB.
  - Performance: engine 47 minutes for 370,149 units; rehearsal about 55 minutes in total.
  - Defect fixed: the last window's neighbour month (2026-11) was outside the calendar (`KeyError`). Nothing was written; one line in `orders_of_month`; resumed.
- Report: `docs/test_reports/S8_historical_simulation_report.md`.

## October restore review

A fresh, retained copy, `nordiccart_s8_review_0814fb0acf0b`, passed the 24-table comparison and S7 pilot comparison. The only expected post-dump metadata differences were the final recovery filename, checksum and update timestamp. SQL 01–18 on the restored copy returned 712 PASS, 173 INFO, three expected WARN and zero FAIL. Script 15 used in-memory planner directives and matched its original result in 6.1 seconds. The sandbox was confirmed idle and stopped with smart shutdown. No historical windows or monthly audits were replayed.

## Remaining tasks (decisions before S9)
1. ~~ChatGPT review of S8~~ passed (operational).
2. ~~S recomputed at R-cal~~ done in S8.1. ~~Script 15 bottleneck~~ fixed in S8.1. ~~Regression test for the out-of-calendar month~~ added in S8.1.
3. ~~ChatGPT review of S8.1 and the turnover decision~~ passed: keep the assortment, display minimums and corrected policy; document the limitation.
4. ~~Final full-scale rehearsal~~ S8.2 complete, PASS.
5. ~~ChatGPT review of S8.2~~ passed.
6. ~~S9 (real historical catch-up)~~ COMPLETE, SUCCESS (2026-11-20). Phase closed.

## Errors or blockers
- No data-integrity failure.
- One engine defect, fixed (see above).
- One verification-performance finding (script 15).

## Files
- Code change: `python/nordiccart/activity/engine.py` (`orders_of_month`: months outside the calendar).
- Scratchpad (session-specific):
  - `s8_driver.py`, `s8_analysis.py`
  - `s8\progress.jsonl`, `s8\logs\engine_<YYYY-MM>.log`
  - `s8\analysis.json`, `s8\scripts_final.txt`, `s8\scripts_15_18_postgres.txt`
  - `s8\recovery\` (8 dumps)
- Codex audit: `logs/s8_month_audit.jsonl`.

## Current environment
- Real database: **historical activity loaded through 2026-10-31 (S9)**; 698 verified runs; last checkpoint 2026-11-01 00:00 UTC.
- Sandbox: PostgreSQL 18.4, port 55432 (stopped). Databases:
  - `nordiccart_s8` (complete rehearsal, 1.2 GB, unchanged in S8.1);
  - `nordiccart_s81` (S8.1 corrected policy, 2024-12-03 to 2025-12-31, 704 MB);
  - `nordiccart_s82` (S8.2 final rehearsal, 2024-12-03 to 2026-10-31, 1.24 GB) and `nordiccart_s82_restore_3f6181ba` (its verified final restore);
  - S8.2 recovery points: scratchpad `s82\recovery\` (8 dumps, 540 MB);
  - `nordiccart_s7_full` (= real state);
  - earlier test databases.

## Next action
Historical phase closed. Next: hourly automation and the ETL pipeline. Any further write to the real database still needs explicit authorization.
