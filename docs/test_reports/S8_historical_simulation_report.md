# S8: full-scale historical simulation, sandbox rehearsal · report

November 20, 2026.

**Operational rehearsal and recovery verification: PASS. Inventory-turnover calibration: FAIL. S9 awaits review.**

**Completed: the whole period 2024-12-03 to 2026-10-31 was simulated in the sandbox.**
- 698 UTC-day windows, every one `verified`.
- 23 month-end verifications, each 19/19.
- 23 recovery points.
- Scripts 01–18: 0 FAIL.
- Pilot records unchanged.

**Last successfully simulated date: 2026-10-31.**

Not done: the real database was not modified (a working copy was compared with it read-only); no migration; S9 not started.

## 1. Setup

- **Working copy:** `nordiccart_s8` (sandbox, port 55432), a clone of the S7 database.
  - Before the run it was **identical to the deployed real database**: 22 of 22 tables (business columns) plus change_log metadata; 0 activity runs; 47 MB.
- **Engine:** the existing S6 engine (incremental replay, day windows, month-end verification), full profile, `config/generator.toml` with no changes.
- **Driver:** `<scratchpad>\s8_driver.py` runs one engine process per month (`run_activity.py --apply --windows <days> --yes --profile full`). Then:
  - a `pg_dump -Fc` **recovery point** is taken while no writer runs;
  - its SHA-256 is recorded in the month's last run row (`meta.activity_runs.recovery_point*`, V009).
- **Hand-over:** Codex observed the run while it was in progress and audited recovery points; the original driver kept control.

## 2. Execution log

| Month | Windows | Units of work | Engine time | Month-end checks | Orders (cumulative, incl. 20 pilot) | Database size | Recovery point (MB) |
|---|---:|---:|---:|---|---:|---:|---:|
| 2024-12 (from Dec 3) | 29 | 19,727 | 126 s | 19/19 | 3,868 | 106 MB | 8.2 |
| 2025-01 | 31 | 12,943 | 83 s | 19/19 | 6,317 | 151 MB | 12.3 |
| 2025-02 | 28 | 10,256 | 72 s | 19/19 | 8,253 | 183 MB | 15.4 |
| 2025-03 | 31 | 12,393 | 82 s | 19/19 | 10,623 | 221 MB | 19.0 |
| 2025-04 | 30 | 12,075 | 86 s | 19/19 | 12,911 | 257 MB | 22.6 |
| 2025-05 | 31 | 13,450 | 93 s | 19/19 | 15,486 | 297 MB | 26.4 |
| 2025-06 | 30 | 13,411 | 98 s | 19/19 | 18,037 | 338 MB | 30.2 |
| 2025-07 | 31 | 12,745 | 101 s | 19/19 | 20,460 | 377 MB | 34.0 |
| 2025-08 | 31 | 14,863 | 115 s | 19/19 | 23,318 | 422 MB | 38.1 |
| 2025-09 | 30 | 15,058 | 110 s | 19/19 | 26,205 | 467 MB | 42.2 |
| 2025-10 | 31 | 16,320 | 117 s | 19/19 | 29,352 | 516 MB | 46.7 |
| 2025-11 | 30 | 23,362 | 151 s | 19/19 | 33,938 | 583 MB | 52.3 |
| 2025-12 | 31 | 27,018 | 172 s | 19/19 | 39,219 | 665 MB | 59.1 |
| 2026-01 | 31 | 16,396 | 127 s | 19/19 | 42,374 | 715 MB | 63.6 |
| 2026-02 | 28 | 12,999 | 116 s | 19/19 | 44,859 | 755 MB | 67.3 |
| 2026-03 | 31 | 15,562 | 131 s | 19/19 | 47,867 | 803 MB | 71.7 |
| 2026-04 | 30 | 15,094 | 125 s | 19/19 | 50,778 | 848 MB | 75.9 |
| 2026-05 | 31 | 16,896 | 139 s | 19/19 | 54,050 | 898 MB | 80.4 |
| 2026-06 | 30 | 16,445 | 143 s | 19/19 | 57,229 | 949 MB | 84.9 |
| 2026-07 | 31 | 15,442 | 147 s | 19/19 | 60,202 | 995 MB | 89.2 |
| 2026-08 | 31 | 18,825 | 149 s | 19/19 | 63,864 | 1,052 MB | 94.2 |
| 2026-09 | 30 | 18,049 | 152 s | 19/19 | 67,370 | 1,107 MB | 99.0 |
| 2026-10 | 31 | 20,820 | 161 + 26 s | 19/19 | 71,429 | 1,169 MB | 104.4 |

**One defect, found and fixed.** In October 2026, after window 697 (2026-10-30) was verified, planning the **last window (2026-10-31)** stopped with `KeyError: (2026, 11)`.
- Cause: the engine also reads the neighbouring month's demand draws (local day versus UTC day), and November 2026 lies outside the simulation calendar.
- Nothing was written for that window: planning fails before the run row is inserted. The ledger stayed at 697 verified runs.
- Fix in `activity/engine.py` (`orders_of_month`): a month outside the calendar has no demand. No earlier window reads such a month, so no earlier plan changes.
- The driver was then restarted, and it ran the single missing window (776 units, verified) plus the October month-end verification and recovery point.

## 3. Business activity (completed results)

**Totals at 2026-10-31**
- Orders 71,429 (71,409 simulated plus 20 pilot); order lines 96,463; payment attempts 71,586; reservations 96,460.
- Movements 195,177; purchase orders 400 (14,307 lines: 13,840 reorder point, 463 launch).
- Prices 3,353, of which **1,353 P1 markdowns**.
- Reorder-point revisions 5,867: 69 R0 plus **5,798 R-cal** across 22 monthly reviews.
- **Exchange rates 2,190** (2024-11-01 to 2026-10-31, complete).
- Change log 1,443,636 rows; 698 runs and 698 checkpoints.

**Order volume against the demand model (expected 72,635 orders, 2024-12-03 to 2026-10-31)**

| Period | Expected | Placed | Lost (no stock) | Placed + lost vs expected |
|---|---:|---:|---:|---:|
| Growth year 2024-12-03 to 2025-10-31 | 29,858 | 29,332 | | −1.8% (placed) |
| Primary year 2025-11-01 to 2026-10-31 | 42,778 | 42,077 | | −1.6% (placed) |
| **Total** | **72,635** | **71,409** | **333** | **−1.2%** (71,742) |

Monthly deviation (placed + lost vs expected) ranges from −3.5% (2026-09) to +1.3% (2026-10). That is within the design's ±3% tolerance for the yearly totals. The overall −1.2% is about 1.7 standard deviations for one seed (gamma-Poisson variance ≈ 4 × mean). The seasonal shape follows the model:
- November 2025: 4,586 orders;
- December 2025: 5,281;
- February 2025: 1,936, the low point.

| Market | Orders | Share | Expected share |
|---|---:|---:|---:|
| SE | 29,956 | 41.9% | 41.1% |
| DK | 15,459 | 21.6% | 21.8% |
| NO | 14,030 | 19.6% | 19.8% |
| FI | 11,964 | 16.8% | 17.3% |

| Channel and outcomes | Result |
|---|---|
| Channel | Online 39,127 (**54.8%**, expected 55.0%); in store 32,282 |
| Outcomes | Fulfilled 68,447 (95.9%); payment_failed 1,332; expired 1,140; customer-cancelled 448; confirmed (shipping on Nov 1) 42 |
| First-attempt payment failure | Online 6.2% (configured 6%); in store 2.1% (configured 2%) |
| Lost demand (availability-aware customers) | 333 whole orders (**0.46%**) and 795 units. Highest in December 2024 (76) and December 2025 (72) |

Fulfilled revenue (EUR, at the rate of the local fulfilment day):

| Market | In store | Online | Total |
|---|---:|---:|---:|
| SE | 4.79 M | 5.64 M | 10.43 M |
| DK | 2.50 M | 2.90 M | 5.41 M |
| NO | 2.32 M | 2.84 M | 5.16 M |
| FI | 1.96 M | 2.20 M | 4.16 M |
| **Total** | | | **25.15 M** |

By year: growth year 10.55 M; primary year 14.60 M.

**Replenishment and stock (month ends)**
- Transfers: 217–266 a month, about 8–15 units each. That is close to **one transfer per store per working day**.
- Purchase orders: 16–20 a month, one per warehouse per week. Receipts follow the orders: 2,623 to 9,985 units a month; peaks in January 2025 and December 2025 after the Black Week and December orders.

| Month end | Store units | Store weeks of cover | Store listings out of stock | Warehouse units | Warehouse weeks of cover | Warehouse out of stock |
|---|---:|---:|---:|---:|---:|---:|
| 2024-12 | 10,694 | 18.2 | 1.5% | 6,664 | 9.7 | 3.0% |
| 2025-06 | 13,166 | 33.0 | 0.3% | 11,331 | 27.2 | 0.2% |
| 2025-12 | 13,652 | 17.9 | 1.0% | 11,299 | 12.6 | 0.9% |
| 2026-06 | 14,861 | 32.6 | 0.2% | 12,563 | 22.0 | 0.5% |
| 2026-10 | 14,949 | 25.1 | 0.4% | 12,167 | 18.0 | 0.8% |

Primary-year sales: 58,592 units against an average stock of about 27,000 units, which is **about 2.2 stock turns a year**.

The primary-year cost-based turnover was **1.56**, from EUR 9.78 million cost of sales divided by EUR 6.27 million average daily inventory at cost, including in-transit stock and using daily market FX. The whole-period annualized result was 1.49. Actual in-store demand matched to recorded order lines gave **99.27% line fill** (318 of 43,842 requested lines short), above the 97% target. The table’s warehouse weeks use direct warehouse sales; using sales across the network supplied by the warehouses gives 9.58 weeks in October 2026. The store cover and chain turnover figures remain evidence of excess inventory.

## 4. Validation (completed)

| Check | Result |
|---|---|
| Window protocol | 698 of 698 runs `verified`; 698 checkpoints, each with the same hash as its run; incremental replay from the previous checkpoint throughout |
| Month-end verification | **23 of 23 months 19/19**: orders, payments, inventory, rules, rates, checkpoint chain |
| Scripts 01–18 at 2026-10-31 | **888 rows: 712 PASS, 173 INFO, 0 FAIL**, 3 known WARN (07: 2, 10: 1). Scripts 01–14 ran as `nordiccart_analyst`. Script 15 exceeded the analyst's 15-minute statement timeout (see 6.4), so 15–18 ran read-only as the sandbox superuser |
| Pilot `--plan` (as the loader) | Transactions and replenishment: **0 conflicts, 0 blocking**. Accepted as later activity: 71,409 orders (42 open) and 189,064 movements. The 3 order lines without a reservation are the two pilot orders cancelled out of stock in November 2024 (by design) |
| Pilot records | **Byte-identical** to the S7 baseline: 20 orders, 26 lines, 21 payments, 23 reservations, every movement before the origin (6,134, including E2), 4 POs and lines, 69 R0 revisions, 96 E1 rates |
| Recovery points | All 23 recorded. The pre-existing independent restore audit completed 14 months, December 2024–January 2026. The October 2026 point also passed a full restore, giving 15 distinct audited months. February–September 2026 have no completed independent restore audit. Eight archives remain after planned retention (535,410,357 bytes). |

**October recovery evidence.** No S8 verifier or simulation process was active at takeover of this closeout. The ledger, audit journal and saved final SQL transcripts were inspected without restarting the simulation or monthly audit. The October archive was restored separately with PostgreSQL 18 on port 55432. Its 109,460,363 bytes hashed to `009bb36941ce5002a21bd99c6f0737ebe46dab45b8d26fe1fcfa60a7b6a8ec86`, matching the ledger and progress log. The restored database matched the source on 698 verified runs, 698 checkpoints, 71,429 orders, 195,177 movements and 1,443,636 change-log rows. Its final checkpoint was 2026-11-01 00:00 UTC with state hash `e93158de8d8d286df5d9ac60b57a3225a562c800c0038c029219a4147396a1c8`. The disposable copy was removed and the sandbox stopped.

The final pilot comparison checked every column, including IDs, for the original rows named in the table above. Saved SQL transcripts `s8/scripts_final.txt` and `s8/scripts_15_18_postgres.txt` together cover scripts 01–18. Fresh evidence is in `logs/s8_october_final_restore.txt` and `logs/s8_final_original_rows.txt`.

### Independent October restore and SQL review

The approved follow-up restored the 109,460,363-byte October archive into a new database, `nordiccart_s8_review_0814fb0acf0b`. Before creating it, the verifier matched the server's system identifier 7709316403725191040 to the existing sandbox data directory and confirmed 127.0.0.1:55432. The archive SHA-256 matched the October ledger entry.

Ordered full-row digests matched across all 24 tables after accounting for the final run's post-dump registration. The first comparison flagged that row. Field-by-field inspection showed exactly three expected differences: archive filename, SHA-256 and the trigger-maintained update timestamp. Every other field matched. Direct comparison with S7 confirmed all 20 pilot orders and their lines, payments, reservations, all 6,134 baseline movements (including the pilot movements), the four pilot purchase orders and lines, 69 R0 revisions and 96 E1 rates.

The restored copy returned **712 PASS, 173 INFO, 3 expected WARN and 0 FAIL** across scripts 01–18 under the analyst role. For this independent run, script 15 used in-memory PostgreSQL planner directives (`AS NOT MATERIALIZED` on `res`, `sales` and `pay`). All check expressions and date coverage were preserved. Its 35 PASS and 8 INFO matched the saved original run and took 6.1 seconds on the analyzed restore. The repository SQL file was untouched. This provides a candidate optimization for review before S9.

The new review database was retained. An idle check found zero other sessions and zero prepared transactions; smart shutdown completed. No database or backup was deleted. Evidence: `logs/s8_restored_verification_results.txt`. This final restore checks the same October point and adds no new month to the 15 distinct audited months.

## 5. Performance and size (completed)

| Measure | Value |
|---|---|
| Engine time, whole period | **47 minutes** for 370,149 units of work (131 per second overall, including process start, planning, verification and month checks). Per month: 72 s (Feb 2025) to 188 s (Oct 2026 across both invocations), growing with history and volume |
| Recovery points | 284 s in total |
| Total rehearsal wall time | about 55 minutes |
| Database size | 47 MB → **1,169 MB** (1.23 GB including the final month) |
| Change log | 1,443,636 rows (design estimate 1.0–1.3 million) |
| Script 15 on the full history | **6,055 s (101 min)** |
| Scripts 16–18 | 0.1–20 s |

**Projection, not measured:** the real-database catch-up (S9) has the same volume, so it should take a similar time, about 1 hour of engine time plus recovery points. The result would be a database of about 1.2 GB.

## 6. Calibration issues and recommended corrections

1. **Too much stock.** Weeks of cover reach 25–34 in stores and 18–27 in warehouses, against design targets of 6 and 8–12; stock turns are about 2.2 a year against 4–6. Availability is excellent (lost demand 0.46%; fill-rate target met), but capital is overstated.
   - Cause: R-cal recalibrates only s (the reorder point). S (order-up-to) still comes from planning demand that is too high: the first combined week (December peak, seasonal index 1.6) for new products, and the opening model for the 20 pilot products.
   - **Recommendation:** recompute S at each monthly R-cal from the demand model's forecast (weeks of cover × forecast weekly units), for every listing. This is an engine change and needs your approval before S9.
2. **Transfers too frequent.** About one transfer per store per working day, with small lots of about 8–15 units.
   - **Recommendation:** store reviews twice a week, or a minimum transfer size. This would come with correction 1.
3. **Order volume −1.2% against the model.** It is within tolerance and statistically plausible for one seed. No correction proposed; monitor in the S9 monthly checks.
4. **Verification bottleneck: script 15 at full history (101 minutes).** It is not a data failure (0 FAIL), but it cannot run as `nordiccart_analyst` (15-minute timeout), and the S9 monthly verification would grow with every month.
   - **Recommendation before S9:** profile the slow integrity checks and review their query plans. Script 15’s date parameters currently limit INFO rows only. Preserve global integrity coverage when evaluating any optimization.
5. **Size.** The change log reaches 1.44 million rows (above the design estimate) and the database 1.2 GB. Acceptable until the Phase 4 extraction purges consumed change_log rows.

## 7. Next step

Waiting for ChatGPT's review and your decisions on corrections 1, 2 and 4 before S9. The historical activity was not deployed to the real database, and S9 has not started.
