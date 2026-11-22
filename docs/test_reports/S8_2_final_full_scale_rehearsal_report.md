# S8.2: Final full-scale sandbox rehearsal

Date: 2026-11-20. Sandbox only (PostgreSQL 18.4, 127.0.0.1:55432, cluster 7709316403725191040).
- **The real database (port 5432) was not modified**: read-only check after the run, section 8.
- **S9 has not started.**
- Scope, as authorized: the S8.1 engine unchanged (`ENGINE_VERSION s8-1`), the existing 500-product assortment and display minimums, and no calibration, policy or feature changes.

## Verdict

**S8.2 PASS. No new errors and no material regressions.** The corrected engine ran the whole history, 2024-12-03 to 2026-10-31, on a fresh clone of the verified S7 baseline:
- 698 of 698 windows verified;
- 23 of 23 month-end checks 19/19;
- scripts 01–18 0 FAIL, with the optimized script 15 running as `nordiccart_analyst`;
- original records identical;
- 23 recovery points recorded; the final one restored and verified.

Against S8 (primary year):
- inventory turnover improved from 2.21 to **2.45** unit turns, and from 1.56 to 1.64 at cost;
- availability is unchanged.

**Known limitation (accepted in the S8.1 review):** turnover stays below the 4–6 target. The cause is the business rules, not a defect: every store displays all 500 products, and the display minimums hold most store stock (section 6).

## 1. Execution (completed)

| Item | Result |
|---|---|
| Database | `nordiccart_s82`, `CREATE DATABASE … TEMPLATE nordiccart_s7_full` (the verified S7 baseline, equal to the real database before S8), timezone UTC, V008 database privileges re-applied |
| Driver | The S8 driver unchanged except for the database name, output folder and the one-time clone: one `run_activity.py --apply --profile full` process per month as `nordiccart_loader`, then a `pg_dump -Fc` recovery point recorded with its SHA-256 on the month's last run row |
| Window protocol | **698 of 698 runs `verified`**, 698 checkpoints, 2024-12-03 00:00 to 2026-11-01 00:00 UTC; incremental replay from each previous checkpoint |
| Month-end verification | **23 of 23 months 19/19** |
| Units of work | 370,035 |
| Engine time | 3,057 s (**51 min**); per month 70 s (February 2025) to 235 s (August 2026). S8: 47 min |
| Recovery points | 23 taken and recorded (313.7 s in total) |
| Errors | None. No failed, open or repeated window |
| Final state | 71,418 orders (71,398 simulated + 20 pilot), 200,336 movements, change_log 1,460,385, **database 1,181 MB** (1.24 GB) |

**Determinism check:** over the 394 windows S8.1 also ran (2024-12-03 to 2025-12-31), S8.2 and S8.1 are **identical**: every plan fingerprint, end-state hash and checkpoint hash. The same engine on the same baseline gives the same history.

## 2. Verification (completed)

| Check | Result |
|---|---|
| Scripts 01–18 at 2026-10-31, as `nordiccart_analyst` | **888 rows: 712 PASS, 173 INFO, 0 FAIL**, 3 known WARN (07: 2, 10: 1). The whole suite took **48.5 s**. Script 15 (optimized) ran within the analyst's 15-minute timeout; in S8 it took 101 min and could not run as the analyst |
| Pilot `--plan` (as the loader) | Transactions: **0 conflicts, 0 blocking** (71,398 later orders, 42 open). Replenishment: **0 conflicts, 0 blocking**; PO documents 4/4, R0 revisions 4/4, 329 later revisions, 0 to write |
| Original records | **Byte-identical to S7**, full rows including IDs: 20 orders, 26 lines, 21 payments, 23 reservations, 6,134 movements, 4 purchase orders and lines, 69 R0 revisions, 96 exchange rates |

## 3. Recovery points (completed)

| Item | Result |
|---|---|
| Taken | 23, one after each month; all recorded in `meta.activity_runs.recovery_point` with SHA-256 (23 distinct) |
| **Final point verified** | `nordiccart_s82_202610_activity_recovery_20261120_142115.dump`, 110,510,490 bytes. SHA-256 `a1d7c3ee…a50ed7` **matches the ledger**. Restored into the new database `nordiccart_s82_restore_3f6181ba` in **58 s**, exit 0 |
| Restored content | **All 24 tables** in `public` and `meta` (2,056,036 rows) match the source, compared by row count and a digest of every full row. `meta.activity_runs` was compared without `recovery_point`, `recovery_point_sha256` and `updated_at`, which the driver writes on the final row after the dump (by design) |
| State | Final checkpoint at 2026-11-01 00:00 UTC. The state hash **recomputed from the restored database** (`state_as_of`) equals the ledger and the restored checkpoint: `a57f6ed8…773994` |
| Retention | As in S8 (design C14): every point kept until the catch-up completed and the final point was verified; then the **quarter ends from March 2025 plus the final point** were kept. **8 files, 540,002,144 bytes**: 2025-03, 06, 09, 12, 2026-03, 06, 09 and 10. Before removal, each of the other 15 files (767,856,611 bytes) was checked against its ledger SHA-256; names and checksums stay in the ledger. Log: `s82/retention.json` |

The restored database is kept for review. The S8 database, the eight S8 recovery points, `nordiccart_s81` and `nordiccart_s7_full` were not changed.

## 4. Comparison with S8 and S8.1 (completed)

Same queries on all three databases (the S8.1 comparison method). Turns = units (or EUR cost) sold ÷ average end-of-day network stock, in transit included, annualized. Warehouse "network" cover = warehouse units ÷ weekly sales of its whole market. S8.1 covers 2025 only.

### 4.1 Volume and revenue

| Measure | S8 | S8.2 | Change |
|---|---:|---:|---:|
| Demand (placed + lost orders) | 71,742 | 71,742 | 0 (same demand draws) |
| Orders placed (simulated) | 71,409 | 71,398 | −11 |
| Against 72,635 expected (demand) | −1.2% | −1.2% | 0 |
| Growth year / primary year placed | 29,332 / 42,077 | 29,310 / 42,088 | −22 / +11 |
| Lost orders (share) | 333 (0.46%) | 344 (0.48%) | +11 |
| Markets SE / DK / NO / FI | 41.9 / 21.6 / 19.6 / 16.8% | 42.0 / 21.6 / 19.6 / 16.8% | ≤ 0.1 pt |
| Online share | 54.8% | 54.8% | 0 |
| First-attempt payment failure, online / in store | 6.2% / 2.1% | 6.2% / 2.1% | 0 |
| Statuses: fulfilled / payment failed / expired / customer-cancelled / confirmed | 68,447 / 1,332 / 1,140 / 448 / 42 | 68,439 / 1,335 / 1,135 / 447 / 42 | — |
| Fulfilled revenue (EUR) | 25.15 M (10.55 + 14.60) | **25.13 M** (10.53 + 14.60) | −0.09% |
| P1 markdowns / R-cal revisions / exchange rates | 1,353 / 5,798 / 2,190 | 1,353 / 5,798 / 2,190 | 0 |

### 4.2 Inventory

| Measure | S8 2025 | S8.1 2025 | S8.2 2025 | S8 primary year | **S8.2 primary year** | S8 whole | S8.2 whole |
|---|---:|---:|---:|---:|---:|---:|---:|
| Units sold | 49,172 | 49,173 | 49,173 | 58,592 | 58,611 | 99,404 | 99,378 |
| Average network stock (units) | 23,911 | 21,814 | 21,814 | 26,515 | **23,941** (−9.7%) | 24,952 | 22,618 |
| Average network stock (EUR at cost) | 5.55 M | 5.28 M | 5.28 M | 6.21 M | **5.91 M** (−4.9%) | 5.81 M | 5.52 M |
| **Turns (units)** | 2.06 | 2.25 | 2.25 | 2.21 | **2.45** | 2.08 | 2.30 |
| Turns (cost) | 1.49 | 1.56 | 1.56 | 1.56 | **1.64** | 1.49 | 1.56 |
| Warehouse month-end stock (average) | 11,008 | 8,982 | 8,982 | 11,987 | **9,460** (−21%) | 11,352 | 9,072 |
| Store month-end stock (average) | 13,036 | 12,929 | 12,929 | 14,465 | 14,365 (−0.7%) | 13,651 | 13,541 |

Weeks of cover at month ends, primary year:

| Month end | Warehouse network weeks S8 → S8.2 | Store weeks S8 → S8.2 | Warehouse units S8 → S8.2 |
|---|---:|---:|---:|
| 2025-11 | 7.0 → 6.5 | 18.7 → 18.6 | 10,216 → 9,581 |
| 2025-12 | 6.8 → 5.8 | 17.9 → 17.7 | 11,299 → 9,719 |
| 2026-01 | 11.6 → 9.7 | 31.5 → 31.4 | 11,514 → 9,706 |
| 2026-02 | 13.1 → 10.2 | 35.2 → 35.0 | 11,472 → 8,985 |
| 2026-03 | 13.0 → 9.6 | 33.5 → 33.3 | 12,189 → 9,007 |
| 2026-04 | 13.2 → 10.0 | 33.1 → 32.8 | 12,599 → 9,525 |
| 2026-05 | 11.5 → 8.6 | 30.5 → 30.2 | 11,891 → 8,876 |
| 2026-06 | 12.2 → 9.2 | 32.6 → 32.3 | 12,563 → 9,435 |
| 2026-07 | 13.5 → 10.3 | 33.8 → 33.5 | 12,565 → 9,585 |
| 2026-08 | 11.1 → 9.1 | 26.7 → 26.4 | 12,874 → 10,516 |
| 2026-09 | 10.8 → 8.5 | 28.5 → 28.3 | 12,490 → 9,833 |
| 2026-10 | 9.6 → 6.9 | 25.1 → 24.8 | 12,167 → 8,749 |

- **Warehouses:** January–September 2026 cover is 8.5–10.3 weeks in S8.2, **inside the 8–12 target**; in S8 it was 10.8–13.5 weeks, above target in five of those nine months.
- **Peak month-ends:** November, December and October 2026 sit below 8 weeks because they are measured against peak or pre-peak sales. They cause no availability loss (section 4.3).
- **Stores:** store cover is unchanged (about 25–35 weeks outside the peak), because stores sit on the display floor (section 6).

### 4.3 Availability

| Measure | S8 primary | S8.2 primary | S8 whole | S8.2 whole |
|---|---:|---:|---:|---:|
| Lost orders / lost units | 194 / 496 | **183 / 474** | 333 / 795 | 344 / 825 |
| Unit fill (sold ÷ sold + lost) | 99.16% | **99.20%** | 99.21% | 99.18% |
| Store listings at 0 at month end (average) | 0.28% | 0.23% | 0.20% | 0.20% |
| Warehouse listings at 0 at month end (average) | 0.63% | 0.67% | 0.58% | 0.67% |

The established availability requirement holds: unit fill stays above 99%, and lost demand is about 0.5% of orders. In the primary year S8.2 is slightly better; over the whole period it loses 30 more units out of about 100,000 sold (−0.03 points).

### 4.4 Transfers and purchase orders

| Measure | S8 primary | S8.2 primary | S8 whole | S8.2 whole |
|---|---:|---:|---:|---:|
| Store transfers (documents) | 2,967 | 2,965 | 5,616 | 5,583 |
| Transfer units | 28,459 | 28,495 | 50,495 | 50,327 |
| Purchase orders | 208 | 208 | 396 | 396 |
| PO lines | 8,125 | 9,978 (+23%) | 14,303 | 17,691 (+24%) |
| PO units | 60,689 | 58,726 (−3.2%) | 108,653 | 104,825 (−3.5%) |
| PO lines by reason (whole, incl. pilot) | reorder point 13,840, launch 463 | reorder point 17,223, launch 468 | | |

- **Purchase orders:** still one per warehouse per week. The lower S gives smaller, more frequent orders per product: more lines and fewer units.
- **Transfers:** unchanged, about one per store per working day. As decided in S8.1, this was left as is.

## 5. Performance and size (completed)

| Measure | S8 | S8.2 |
|---|---:|---:|
| Engine time, whole period | 47 min | 51 min (+8%) |
| Recovery points | 284 s | 314 s |
| Database size | 1,169 MB (1.23 GB) | 1,181 MB (1.24 GB, +1%) |
| Movements / change_log | 195,177 / 1,443,636 | 200,336 / 1,460,385 (+2.6% / +1.2%, from the extra PO and transfer lines) |
| Scripts 01–18 (analyst) | scripts 15–18 had to run as the superuser (script 15 alone took 101 min) | 48.5 s in total, all as the analyst |
| Final restore | 55 s | 58 s |

**Projection, not measured:** S9 on the real database has the same volume, so about 50–55 minutes of engine time plus about 5 minutes of recovery points, giving a database of about 1.24 GB.

## 6. Known limitation: inventory turnover (accepted)

Turnover reaches 2.45 unit turns (1.64 at cost) in the primary year, below the original 4–6 target. **This is a consequence of the accepted business rules, not a defect:**
- Every one of the 12 stores lists and displays all 500 products, with display minimums of 1–3 units (+1 in flagship stores).
- That floor is about 13,000–14,500 units, 60% of network stock.
- At the end of S8, the display minimums made up 94% of store stock.
- With primary-year sales of 58,611 units, even empty warehouses would allow at most about 58,611 ÷ 14,365 ≈ **4.1 unit turns**.

The correctable part, the warehouse overstock, has been corrected. Per the S8.1 decision, the assortment, display minimums and corrected policy are retained, and **no further calibration change was made**. Store cover of about 25–35 weeks outside the peak is therefore expected behaviour.

## 7. New errors and material regressions

- **New errors: none.** No engine, verification, protocol or data-integrity failure; no conflict; no FAIL.
- **Material regressions: none.** Minor differences against S8, all expected from the corrected S:
  - +11 lost orders over 23 months (0.46% → 0.48%);
  - warehouse listings at 0 at month end +0.09 points on average;
  - October 2026 warehouse month-end cover of 6.9 weeks, against pre-season sales;
  - 23–24% more PO lines;
  - engine time +8%.

## 8. Real database and environment

- **Real database, read-only check after S8.2:** `nordiccart_oltp` on port 5432, `default_transaction_read_only on`: 20 orders, 500 products, change_log 43,310, 0 activity runs. Unchanged.
- **Sandbox:** stopped after the run (smart shutdown). Databases:
  - `nordiccart_s82` (final rehearsal, 1.24 GB);
  - `nordiccart_s82_restore_3f6181ba` (verified final restore);
  - `nordiccart_s81`, `nordiccart_s8`, `nordiccart_s7_full` and earlier test databases, unchanged.

## 9. Evidence

Session scratchpad (session-specific):
- Tools:
  - `s82_driver.py` (generated from `s8_driver.py` by `make_s82_driver.py`)
  - `s82_final_restore.py`, `s82_retention.py`
  - `s82_analysis.py` (from `s8_analysis.py`), `s82_compare.py` (uses the S8.1 comparison queries)
- Run evidence:
  - `s82/progress.jsonl`, `s82/logs/engine_<YYYY-MM>.log`, `s82/driver.out`
  - `s82/final_restore.json`, `s82/retention.json`
- Results:
  - `s82/scripts_s82.txt`, `s82/pilot_plans.txt`, `s82/original_rows.txt`
  - `s82/analysis.json`, `s82/compare.json`
- `s82/recovery/` (8 dumps)

## 10. Next step

**Stopped for ChatGPT review.** S8.2 shows the corrected engine is ready for S9. S9, the real historical catch-up on the real database, requires:
- this review;
- a fresh verified backup of the real database;
- **your explicit authorization**.

It has not started.
