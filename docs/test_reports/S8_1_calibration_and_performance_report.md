# S8.1: Inventory calibration and SQL performance correction

Date: 2026-11-20. Sandbox only (PostgreSQL 18.4, port 55432, cluster 7709316403725191040). **The real database was not modified. S9 has not started.** The completed S8 database (`nordiccart_s8`) and its eight recovery points are unchanged. The 23-month simulation was not rerun.

## Summary

| Correction | Result | Ready for the final full-scale rehearsal? |
|---|---|---|
| 1. S (order-up-to) recomputed at every monthly R-cal from the demand forecast | Implemented and tested. Warehouse stock fell by about 20% and warehouse cover reached the 8–12 week target. Availability is unchanged. Chain turnover rose only from 2.06 to 2.25 unit turns (1.49 to 1.56 at cost). | **Yes**, as a correction. **The 4–6 turnover target cannot be met** without an assortment decision (section 2.4). |
| 2. Script 15 performance | Three CTEs inlined (`AS NOT MATERIALIZED`). Full 23-month history: **101 min → 6.2 s**, now within the `nordiccart_analyst` 15-minute timeout. Same 43 checks and the same results (section 3). | **Yes** |

## 1. Inventory correction

### 1.1 Cause (inspected)

- **s (reorder point):** R-cal recomputed s every month from the demand forecast over the lead time × 1.5 (`rcal_reorder_points`). Unchanged.
- **S (order-up-to level):** S was never recomputed. The engine used fixed planning figures:
  - stores: the opening week's demand (December peak, seasonal index 1.6) × 6 weeks;
  - warehouses: the opening echelon demand × 10 weeks;
  - the 20 pilot products: the older opening model.
- **Result in S8:** warehouses held stock for peak-season demand all year.

**Diagnosis on the S8 end state (2026-10-31), read-only:**

| | Stores | Warehouses |
|---|---:|---:|
| Listings on sale | 6,000 | 2,000 |
| Units on hand | 14,949 | 12,167 |
| Sum of display minimums (business rule: every store shows every product) | **14,036 (94% of store stock)** | n/a |
| Listings where S = display minimum | 4,616 of 6,000 (77%) | n/a |
| Sum of S (old planning basis) | 15,478 | 18,773 |

**What the diagnosis shows:**
- Store stock is set by the display minimums, not by S.
- Each store sells about 44 units a week across 500 products, so the display floor alone is about 27 weeks of store cover.
- The S correction can reduce warehouse stock, but not store stock.

### 1.2 Change (minimal)

**Code:**
- `python/nordiccart/demand/inventory.py`: new pure function `rcal_cover_units(model, review_day)`. It uses R-cal's forecast over the **weeks of cover** instead of the lead time:
  - stores: their own expected fulfilled units over 6 weeks;
  - warehouses: echelon units (online plus the market's stores) over 10 weeks.
  - The forecast window follows the season, so S builds up before Black Week and December and falls back afterwards.
- `python/nordiccart/activity/engine.py`:
  - `EngineContext.cover_units(day)` returns the forecast of the R-cal in force on the review's local day. R-cal runs at 05:00, before the 05:30 store review, so a review on R-cal day already uses it.
  - `order_up_to(loc, sku, day)` = max(display or warehouse minimum, forecast units over the cover, s + 1). These are the same policy functions as before (`store_order_up_to`, `warehouse_order_up_to`).
  - Before the first R-cal after the origin (2 January 2025), S comes from the planning figures as before. **December 2024 is therefore identical to S8.**
  - `ENGINE_VERSION` is now `s8-1`. The config fingerprint changes with it, so a run ledger written by `s6-m3` (S8) is never continued with the new policy.

**Unchanged:**
- configuration (weeks of cover 6 and 10, lead times, safety factor 1.5, display minimums);
- s and R-cal, including its capacity guard;
- launch rules (eligibility and pre-launch windows; PO line reason `launch`);
- store and warehouse review schedules and transfers;
- the replenishment pilot's S (`replenishment/generate.py`) and pilot records.

**Determinism:** S is a pure function of the calendar and the demand model. It is not stored, but every transfer and PO line records it, so incremental replay equals full replay. No migration and no new infrastructure.

### 1.3 Representative sandbox run (completed)

- **Database:** `nordiccart_s81`, a new clone of `nordiccart_s7_full`. That is the S7 baseline, equal to the real database's state.
- **Period:** 2024-12-03 to **2025-12-31**: 13 months and 394 day windows, including the summer low, the November build-up, Black Week and the December peak.
- **Method:** the existing CLI (`run_activity.py --apply`) run month by month by the S8 driver, without recovery dumps.
- **Comparison:** the same queries on `nordiccart_s8` over the same months. Results below.

| Check | Result |
|---|---|
| Windows | 394 of 394 verified; 13 of 13 month-end verifications **19/19** |
| December 2024 | Identical to S8: every compared figure matches (orders, stock, transfers, purchase orders, lost demand) |
| Scripts 01–18 at 2025-12-31 (as `nordiccart_analyst`) | **888 rows: 712 PASS, 173 INFO, 0 FAIL**, 3 known WARN; 17.5 s for the whole suite |
| Pilot `--plan` (as the loader) | Transactions and replenishment: **0 conflicts, 0 blocking** |
| Original records | Full-row comparison including IDs, identical to S7: 20 orders, 26 lines, 21 payments, 23 reservations, 6,134 movements, 4 POs and lines, 69 R0 revisions, 96 rates |
| Engine time, 13 months | 1,392 s, against 1,405 s in S8 (no cost) |
| Database size at 2025-12-31 | 704 MB, against 697 MB in S8 |

### 1.4 Before and after: calendar year 2025

The corrected S applies from 2 January 2025, so the comparison covers 2025-01-01 to 2025-12-31.

**Method:**
- Turns = units (or EUR cost) sold ÷ average end-of-day network stock, in transit included.
- Stock is valued at the product's EUR unit cost.
- Warehouse "network" cover = warehouse units ÷ all weekly sales in the market (the demand the warehouse supplies).

| Measure (2025) | S8 (original) | S8.1 (corrected) | Change |
|---|---:|---:|---:|
| Units sold | 49,172 | 49,173 | 0% |
| Average network stock (units) | 23,911 | 21,814 | **−8.8%** |
| Average network stock (EUR at cost) | 5.55 M | 5.28 M | −4.8% |
| **Stock turns (units)** | 2.06 | **2.25** | +9% |
| Stock turns (cost) | 1.49 | 1.56 | +5% |
| Warehouse month-end stock, March–December average | 10,884 | 8,753 | **−19.6%** |
| Warehouse network cover, March–October | 11.8–14.1 weeks | **9.1–11.3 weeks** (target 8–12) | in target |
| Warehouse network cover, November–December (peak) | 6.8–7.0 | 5.8–6.5 | peak month-ends |
| Store month-end stock, March–December average | 13,187 | 13,078 | −0.8% (display floor) |
| Store cover (store sales) | 17.9–41.5 weeks | 17.7–41.1 weeks | unchanged |
| Lost demand (orders / units) | 154 / 401 | 161 / 403 | unit fill 99.19% in both |
| Listings at 0 at month end, average: stores / warehouses | 0.22% / 0.47% | 0.22% / 0.60% | stores equal; warehouses +0.13 points |
| Store transfers (documents / units) | 2,932 / 26,003 | 2,899 / 26,109 | −1.1% / +0.4% |
| Purchase orders (documents / lines / units) | 208 / 7,248 / 53,726 | 208 / 8,965 / 52,158 | 0 / +24% / −2.9% |

Monthly detail (month ends):

| Month | Store units S8 → S8.1 | Warehouse units S8 → S8.1 | Warehouse network weeks S8 → S8.1 | PO lines S8 → S8.1 | Lost orders, cumulative S8 → S8.1 |
|---|---:|---:|---:|---:|---:|
| 2024-12 | 10,694 → 10,694 | 6,664 → 6,664 | 5.2 → 5.2 | 912 → 912 | 76 → 76 |
| 2025-01 | 12,144 → 12,046 | 11,793 → 10,526 | 15.3 → 13.7 | 812 → 844 | 95 → 95 |
| 2025-02 | 12,416 → 12,321 | 11,464 → 9,727 | 17.1 → 14.5 | 365 → 434 | 97 → 98 |
| 2025-03 | 12,598 → 12,433 | 10,376 → 8,621 | 13.6 → 11.3 | 517 → 685 | 101 → 104 |
| 2025-04 | 12,698 → 12,593 | 10,286 → 8,078 | 13.7 → 10.8 | 415 → 566 | 105 → 112 |
| 2025-05 | 12,815 → 12,643 | 10,908 → 7,998 | 13.5 → 9.9 | 516 → 654 | 111 → 120 |
| 2025-06 | 13,166 → 13,019 | 11,331 → 8,497 | 13.9 → 10.4 | 581 → 823 | 119 → 132 |
| 2025-07 | 13,334 → 13,224 | 10,818 → 8,145 | 14.1 → 10.7 | 418 → 561 | 124 → 141 |
| 2025-08 | 13,296 → 13,157 | 10,792 → 8,228 | 11.9 → 9.1 | 472 → 674 | 128 → 146 |
| 2025-09 | 13,535 → 13,420 | 11,135 → 8,789 | 11.9 → 9.4 | 638 → 856 | 134 → 151 |
| 2025-10 | 13,697 → 13,510 | 11,683 → 9,871 | 11.8 → 10.0 | 532 → 704 | 139 → 161 |
| 2025-11 | 13,078 → 13,057 | 10,216 → 9,581 | 7.0 → 6.5 | 560 → 648 | 158 → 179 |
| 2025-12 | 13,652 → 13,726 | 11,299 → 9,719 | 6.8 → 5.8 | 1,422 → 1,516 | 230 → 237 |

**Reading:**
- **Warehouses:** the correction works as designed. January still receives the December purchase orders placed under the old S. From March, warehouse cover is inside the 8–12 week target, and S rises again ahead of the season (September–October).
- **Purchase orders:** the count is unchanged, because each warehouse places one PO a week. With a smaller gap between S and s, products are reordered in smaller lots more often: 24% more lines and 3% fewer units.
- **Availability:** preserved. Lost units are 403 against 401, unit fill is 99.19% in both runs, and store empty listings are equal. Warehouse empty listings rise slightly (0.60% against 0.47% on average) without lost sales, because stores are supplied from their own display stock.
- **Stores:** unchanged. Store stock sits on the display floor.

### 1.5 Transfer frequency: not changed

Transfers stay at about one a store per working day (about 9 units). The cause predates S8.1:
- In most store listings S equals the display minimum and s equals the display minimum − 1, so every unit sold is replaced the next morning (one-for-one replenishment of the display).
- The corrected policy neither creates nor worsens this: transfers are −1.1%.
- Fewer reviews, for example two or three a week, would roughly halve the transfers. Each store would then need a higher s to cover the longer gap between reviews, which means more store stock and lower turnover, the opposite of this stage's goal.
- Daily replenishment from a warehouse in the same market is plausible for a chain retailer.
- Under the instruction, this is **not a clear, material problem**, so the schedule was not changed. It remains an optional business decision (section 2.4).

## 2. Turnover assessment

### 2.1 Result

The correction removes the warehouse overstock, the part of the S8 finding that came from a modelling error. Chain turnover improves from 2.06 to 2.25 unit turns (2025).

### 2.2 Why 4–6 turns is not reachable with the current business rules

**Are store display minimums responsible? Yes, they are the main cause of the remaining gap.**
- After the correction, store stock (about 13,100 units, 60% of the network) sits on the display floor.
- At the end of S8, the display minimums alone made up 94% of store stock, and S equalled the display minimum in 77% of store listings.
- The warehouse part of the gap was a modelling error and has been corrected.
- The calculation:

- Every store lists and displays every product: 12 stores × 500 products, with display minimums of 1–3 units (+1 in flagship stores).
- That floor is about 13,000–14,000 units, about 60% of network stock.
- 2025 sales were 49,173 units. Even with **zero** warehouse stock, the network could reach at most about 49,173 ÷ 13,078 ≈ **3.8 unit turns**.
- Reaching 4–6 would require stores to hold less than the display rules allow. That would force the figures, which the task rules out.

### 2.3 Is 2.2–2.5 realistic?

For a specialty retailer that displays a 500-product range in every store, with most products selling about once every 10 weeks per store, about 2–3 unit turns is a plausible outcome. A 4–6 target fits a narrower or faster-moving range.

### 2.4 Decisions for ChatGPT and the owner (not implemented; outside S8.1's scope)

1. **Accept a realistic target** of about 2–3 unit turns for this assortment, and keep the corrected policy. **Recommended for S9:** no further data change is needed.
2. **Store assortment by format:** standard stores list a subset (for example their best-selling 60%). This is a master-data and opening-inventory change on the real database (listings), would come with its own sandbox stage and needs explicit authorization. Estimate, not measured: store stock about −35%, turnover about 3.
3. **Lower display minimums for slow movers:** a configuration change to the display rule (opening stock already loaded would only shrink through sales).
4. **Transfer schedule:** reviews two or three times a week (section 1.5), only with option 2 or 3.

## 3. SQL verification performance (script 15)

### 3.1 Cause and change

- **Cause:** `res`, `sales` and `pay` are each referenced more than once. PostgreSQL 12+ therefore materializes them. The correlated look-ups of checks 503 and 505 (`WHERE r.order_id = o.order_id`, `s.order_item_id = i.order_item_id`) then scan the whole unindexed materialized set once per order. That is quadratic work: 101 minutes on 71,429 orders.
- **Change:** the three CTEs are declared `AS NOT MATERIALIZED` (three keywords plus a header note in `sql/verification/15_verify_inventory_and_activity.sql`).
  - The planner inlines them, so the look-ups use the existing `order_id` and `order_item_id` indexes.
  - For `pay`, the filter on `order_id` is pushed below its window functions, which are partitioned by `order_id`.
  - No check, expression, parameter, date range or coverage changed. Every integrity check still covers the whole database.
- This is the planner directive the October restore review demonstrated (6.1 s), now in the repository file.

### 3.2 Equivalence and timing (full 23-month S8 history, `nordiccart_s8`, 71,429 orders, 195,177 movements)

| Run | Role | Time | Rows | PASS / INFO / FAIL |
|---|---|---:|---:|---|
| Original (pre-S8.1 text), saved S8 run (`s8/scripts_15_18_postgres.txt`) | sandbox superuser, no timeout | 6,055 s (101 min) | 43 | 35 / 8 / 0 |
| **Optimized (repository file)** | **`nordiccart_analyst`** (statement_timeout 15 min) | **6.2 s** | 43 | 35 / 8 / 0 |
| Optimized, on `nordiccart_s81` (13 months) | `nordiccart_analyst` | within the 17.5 s suite | 43 | 35 / 8 / 0 |

**Comparison basis.** `nordiccart_s8` has not changed since its saved S8 run, which is therefore the original's reference result. A second run of the original text was started for a row-by-row comparison, but it was stopped after 71 minutes as redundant, on the review's instruction. Before cancelling, I confirmed it was this verification: original text, script 15, sandbox port 55432, read-only session and no transaction ID (no writes). It was cancelled with `pg_cancel_backend`.

**Result:** the same 43 check numbers (101–110, 201–207, 301–306, 401–406, 501–506, 601–608) and the same statuses (35 PASS, 8 INFO, failed_checks 0).
- **PASS rows:** a PASS means the actual value equals the fixed expected value. The 35 PASS rows are therefore identical by construction (34 expect 0; check 105 compares two computed totals).
- **INFO rows:** these depend on the data, and each matches the independently recorded S8 figures:

| INFO | Optimized result | S8 record |
|---|---|---|
| 601 orders by market | DK 15,463, FI 11,966, NO 14,035, SE 29,965 = 71,429 | 71,409 simulated + 20 pilot |
| 602 orders by status | fulfilled 68,461, payment_failed 1,334, cancelled 1,592, confirmed 42 | 68,447 / 1,332 / 1,140 + 448 / 42 simulated, + pilot 14 / 2 / 4 |
| 603 movements in range | sale 92,477 lines; receipts 14,016; transfers 41,294 pairs | 195,177 movements − 6,096 opening balances (dated 31 October UTC, before the range) = 189,081 = 92,477 + 14,016 + 2 × 41,294 |
| 604 listings | stores 6,000 / warehouses 2,000 | 8,000 listings |
| 605 units now | 27,116 on hand / 62 reserved / 0 in transit | 14,949 store + 12,167 warehouse = 27,116 (S8.1 diagnosis) |
| 606 open orders | 42 | 42 |
| 607 latest event | 2026-10-31 21:38:08 UTC | last window 2026-10-31 |
| 608 change_log | 1,443,636 | 1,443,636 |

The October restore review had run the same three directives in memory on a restored copy and also matched the original: 35 PASS, 8 INFO, 6.1 s.

**Failure detection preserved:** the existing database tests pin script 15's result (43 rows: 35 PASS, 8 INFO) and inject faults that it must report as FAIL (`test_pilot_scope_db.py`, `test_replenishment_db.py`). They passed **43 of 43** with the optimized script.

## 4. Regression tests (completed)

| Suite | Result |
|---|---|
| New: `test_rcal_cover_units_follow_the_forecast_and_the_season` (demand) | passed: stores over 6 weeks, warehouses echelon over 10 weeks; the November review's S is more than 1.3 × the June review's |
| New: `test_order_up_to_follows_the_rcal_in_force` (engine, in memory, December–March) | passed: which R-cal is in force at each date; every transfer and PO line's recorded S equals the rule (planning figures before 2 January 2025, forecast after) |
| New: `test_a_month_outside_the_demand_calendar_has_no_orders` (the S8 defect) | passed: 2026-11 returns no orders without a `KeyError`; 2026-10 still has orders |
| Engine module `test_activity_engine.py` (sandbox) | **27 of 27** passed. Covers determinism, window composition, crash and resume, drift, lock, sandbox guard, the four-month December–March run (incremental = full with the new S across three R-cal boundaries), month-end gate, carry checks, pilot `--plan` 0 conflicts, scripts 01–18 0 FAIL |
| `test_pilot_scope_db.py`, `test_replenishment_db.py` (script 15 pinned and mutation tests) | **43 of 43** passed |
| Whole Python suite without databases | 260 passed, 270 skipped (the database tests, run above) |
| Policy unit tests (`test_order_up_to_levels`) | passed (unchanged functions) |

## 5. Files

- **Code:**
  - `python/nordiccart/demand/inventory.py` (`rcal_cover_units`)
  - `python/nordiccart/activity/engine.py` (S after R-cal, `ENGINE_VERSION s8-1`)
- **SQL:** `sql/verification/15_verify_inventory_and_activity.sql` (three CTEs `AS NOT MATERIALIZED`)
- **Tests:** `tests/python/test_demand.py`, `tests/python/test_activity_engine.py` (three new tests)
- **Sandbox evidence (scratchpad, session-specific):**
  - `s81_driver.py`, `s81_compare.py`, `s81_diagnose.py`, `s81_script15.py`
  - `s81/progress.jsonl`, `s81/compare.json`, `s81/scripts_s81.txt`
  - `s81/script15_*.json`
- **Sandbox databases:** `nordiccart_s81` (new, 13 months), `nordiccart_s8` (unchanged), `nordiccart_s7_full` (unchanged).

## 6. Readiness and next step

**Script 15: READY.** Same checks and results over the full history, in seconds, within the analyst timeout.

**Inventory correction: READY for the final full-scale rehearsal as a correction.**
- It fixes the modelling error behind the S8 overstock: warehouses are now in target and availability is preserved.
- It does not, and cannot honestly, reach 4–6 turns while every store displays the whole range.
- Before the final rehearsal, decide on the target (section 2.4). Option 1 needs no further change; options 2 to 4 need their own stage.

**Proposed next step after review:** a final full-scale rehearsal, 2024-12-03 to 2026-10-31 on a fresh S7 clone with the corrected engine. Then S9, only with a fresh backup and explicit authorization.

**Stopped for ChatGPT review.**
