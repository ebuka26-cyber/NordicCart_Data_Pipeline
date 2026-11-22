# S7: full-profile data expansion · implementation report

November 20, 2026. **S7 deployed to the real database: SUCCESS (section 7).** Sandbox-verified; regression 527 passed (exit 0); readiness READY (section 6); deployed with your explicit authorization at 01:21–01:22.

Scope:
- **S6 is marked complete at pilot scale.** Monthly recovery points are deferred to the historical loading (S8/S9), and no new S6 infrastructure was added.
- **S7 expands NordicCart from the pilot to the planned business scale, reusing the existing generators and loaders.**
- Not done: no migration; no engine redesign; no 23-month simulation.

## 1. What was done

| # | Requirement | Implementation |
|---|---|---|
| 1 | Reuse the generators | `run_master_data_seed.py --profile full` and `run_opening_inventory.py --profile full` are the existing tools. The full profile's first 20 products and first 100 customers, with their prices, are byte-identical to the pilot rows, so the seeder only adds rows. |
| 2 | 500 products, 20,000 customers in SE, NO, DK, FI | Master data full profile: 480 products, 1,920 prices (one per product and market, from launch) and 19,900 customers added. Customers per market: SE 8,031, DK 4,471, NO 3,952, FI 3,546. 7,616 of the 20,000 register inside the simulation calendar. |
| 3 | Keep 12 stores and 4 warehouses | Unchanged: the seeder compared all 16 locations, with 0 inserts and 0 conflicts. |
| 4 | Prices, listings, opening stock with the demand-calibrated approach (decision C3) | New `demand.inventory.opening_inventory_for` (used by the inventory CLI and the engine):<ul><li>the **20 pilot products** keep the opening model, exactly as loaded and verified by the pilots;</li><li>the **361 other products on sale on 2024-11-01** get the demand-calibrated opening quantity and reorder point (S2 `recalibrated_opening`: weeks of cover × the demand model's expected units in the first combined week, with the opening model's minimums and noise);</li><li>the **119 later launches** are listed at 0, with a demand-model reorder point and planning demand at full attractiveness.</li></ul>The opening inventory validation gives 0 issues. Prices are the master data's launch prices; P1 markdowns follow from the engine (S6 M3). |
| 5 | Preserve pilot records and exchange rates | Pilot orders, lines, payments, reservations, their movements, purchase orders, R0 revisions and the 96 E1 rates are **byte-identical** after the expansion (tested). The E1 `--plan` shows 0 pending. |
| 6 | E2 exception within the integrity rules | The **existing** loader inserts each new listing at 0/0 (`inventory_balances_start_empty`) and its opening stock as an `opening_balance` movement. The V007 trigger raises on hand; V007 allows one opening balance per listing; the loader refuses opening stock for any listing with activity; everything is verified in one transaction.<br>The pilots' scope rule accepts these rows as E2: same opening time and reference `OB-20241101` as the pilot's, product on sale on the opening day. Both pilot `--plan` checks: **0 conflicts**. |
| 7 | Sandbox test, short engine run, performance | Section 3 and section 4. |

Code changes, kept small:
- `demand/inventory.py`: `opening_inventory_for`.
- `inventory/cli.py`: uses it for the full profile.
- `activity/engine.py`: uses it.
- `transactions/config.py`, `replenishment/config.py`: an `engine=True` keyword lets the engine read the same profile-independent order and replenishment rules for the full profile. The pilot loaders keep their pilot-only guard.
- Verification scripts **11 and 12: full-profile revisions (decision C19)**. The expected counts are chosen from the data: 20 products means pilot, otherwise full (500 / 20,000; 8,000 / 6,096 / 19,866). A partial load therefore fails.
- New tests: `tests/python/test_full_profile_s7.py`.

## 2. Row counts (sandbox, actual)

| Table | Before S7 (= real database today) | After S7 expansion | After the engine, Dec 3 – Jan 2 (31 windows) |
|---|---:|---:|---:|
| products | 20 | **500** | 500 |
| product_prices | 80 | **2,000** | 2,083 (P1 markdowns) |
| customers | 100 | **20,000** | 20,000 |
| stock_locations / stores / warehouses | 16 / 12 / 4 | 16 / 12 / 4 | 16 / 12 / 4 |
| inventory_balances (listings) | 320 | **8,000** | 8,000 |
| opening_balance movements | 288 | **6,096** (+5,808 E2) | 6,096 |
| opening units | 1,460 | **19,866** (+18,406) | 19,866 |
| listings at 0 (launch later) | 32 | 1,904 | 1,904 |
| orders | 20 | 20 | 3,988 |
| inventory_movements | 326 | 6,134 | 16,486 |
| exchange_rates | 96 | 96 | 189 |
| purchase_orders | 4 | 4 | 20 |
| reorder_point_revisions | 69 | 69 | 1,317 (69 R0 + 1,248 R-cal on Jan 2) |
| change_log | 1,714 | 43,310 | 122,210 |
| database size | 12 MB | 47 MB | 111 MB |

Opening units by market and location type:
- SE: stores 3,520, warehouse 3,269.
- DK: stores 2,664, warehouse 2,038.
- NO: stores 2,664, warehouse 1,972.
- FI: stores 1,911, warehouse 1,828.

The old opening model would have loaded 28,109 units: 2.2 to 3.4 times the calibrated demand (design 6).

## 3. Validation

| Check | Result |
|---|---|
| Master data seed `--apply` (as the loader) | 22,300 rows inserted, 22,550 verified, 0 conflicts, 22,300 change_log rows. Rerun `--plan`: nothing to insert |
| Opening inventory `--apply` (as the loader) | 7,680 listings and 5,808 opening balances (18,406 units) inserted; 8,000 listings verified; 0 conflicts; 69 allowed drifts (the pilot's R0 warehouse reorder points). Rerun `--plan`: 0 and 0 to insert |
| Pilot `--plan` (transactions, replenishment) | 0 conflicts, 0 blocking, before and after the engine run. Later activity includes 7,680 listings outside the pilot with 5,808 E2 opening balances, accepted |
| E1 `--plan` | 0 pending: the 96 rates are unchanged |
| Scripts 01–18 (as the analyst) | After the expansion: 0 FAIL. After the December engine run: **888 rows, 712 PASS, 173 INFO, 0 FAIL**, 3 known WARN |
| Before the script 11/12 revision | Only the 6 count checks failed (104, 105, 109; 101, 102, 103), as C19 expected |
| Engine at full scale | 31 windows (2024-12-03 to 2025-01-02), every one `verified`.<br>**December month-end verification 19/19**: 3,848 orders placed, 3,658 fulfilled, 3,863 payment attempts, 4,943 sales, 3,724 transfer movements, 325 receipts, 16 purchase orders, 71 P1 changes, 29 runs, 27 open orders at month end.<br>R-cal on 2025-01-02: 12 location reviews, 1,248 revisions.<br>Lost demand in December: 78 orders (about 2%; design limit 3%). |
| Tests | `test_full_profile_s7.py`: 2 of 2 passed. In memory: calibrated inventory, pilot listings identical, counts, validation. Sandbox: expansion, rerun, pilot and rates identical, pilot checks, scripts, 2 engine windows.<br>Full regression, `tests/python` with every database suite in the sandbox (`NORDICCART_DB_TESTS`, `S5`, `S6`): **527 passed, 0 failed, 0 skipped, 0 errors, exit code 0, in 13 min 50 s**. That is the 525 tests of S6 M3 plus the 2 new S7 tests. It was the single run started after the S7 changes, not rerun. |

## 4. Performance (sandbox, PostgreSQL 18.4, this PC)

| Step | Time |
|---|---|
| Master data seed, full profile (22,300 rows) | 17 s |
| Opening inventory E2 (7,680 listings, 5,808 movements) | 14 s |
| Engine context load (20,000-customer demand model, calibrated inventory, price timeline) | about 9 s per process |
| First window in a process (draws a month of demand for 20,000 customers; cached afterwards) | about 10 s planning |
| Steady window: planning | 0.55–0.7 s |
| Execution and verification throughput | 250–330 units of work per second; one December window (about 650–920 units) in 2–4 s |
| December, 29 windows | 19,727 units of work in 131 s wall time (two processes) |
| State reads (3 per window) at 15,000 movements | 0.18 s |
| Month-end verification at full scale | 0.28 s |

A rough projection for the 23-month catch-up (S8/S9): about 0.4 million units of work at 250–330 per second gives about 25–35 minutes of execution. Add planning of about 8 minutes, plus growing state reads. That makes **roughly 1 to 1.5 hours in total**, consistent with design 10.2.

## 5. Observations and unresolved items for decision

1. **Deploying S7 to the real database (decision needed).** It is the same two commands as here, as `nordiccart_loader`, after a fresh backup and a readiness check:
   ```
   python python/run_master_data_seed.py --profile full --apply
   python python/run_opening_inventory.py --profile full --apply
   ```
   It writes 22,300 master rows and 13,488 inventory rows, and about 41,600 change_log rows. Nothing was run on the real database.
2. **Customers registered in the future of business time.** 7,616 customers have `registered_at` between 2024-11-01 and 2026-10-31. They are loaded now, as master data; the engine only lets them order from their registration day. Analytics must filter by `registered_at`. Making registration an event (unit C1) is planned only after 2026-10-31.
3. **Change-log growth.** December alone added about 72,000 change_log rows. Over 23 months that projects to about 1.6–1.7 million rows (design estimate 1.0–1.3 million) and a database of about 1.5 GB until Phase 4 extraction purges it.
4. **Replenishment intensity.** At full scale every store receives a transfer almost every working day (12 transfers a day). Store order-up-to levels still come from the opening model, and the 20 pilot products remain overstocked (C3). This is calibration work for the S8 rehearsal, not a blocker.
5. **Unchanged from S6:** monthly recovery points are deferred to S8/S9; the state reads are the main cost that still grows.

## 6. Deployment readiness (2026-11-20, read-only on the real database)

All real-database access was read-only (`default_transaction_read_only=on`), as `nordiccart_loader` through `config/.env`, except for the backup.

| Check | Result |
|---|---|
| Target | `nordiccart_loader@localhost:5432/nordiccart_oltp`, PostgreSQL 18.4, not in recovery, cluster 7663257437785384692 (the protected real cluster) |
| **Pilot baseline unchanged** (post-S5 state) | Migrations 001–009:<ul><li>master data: 20 products, 80 prices, 100 customers; 16 locations (12 stores, 4 warehouses);</li><li>inventory: 320 listings, 288 opening balances, 326 movements, 1,467 units on hand, 0 reserved;</li><li>activity: 20 orders; 96 exchange rates; 4 purchase orders; 69 reorder-point revisions;</li><li>change_log 1,714 rows (max id 1932); 0 activity runs;</li><li>no other session; 0 prepared transactions.</li></ul>Equal to the recorded S5 state, and 22 of 22 tables identical to the backup restored below |
| Master data `--profile full --plan` | Exit 0: **22,300 rows to insert** (products 480, product_prices 1,920, customers 19,900); 250 present (4 currencies, 4 countries, 10 categories, 20 products, 80 prices, 16 locations, 12 stores, 4 warehouses, 100 customers); **0 conflicts, 0 drift, 0 extra**. Log `logs/master_data_full_plan_20261120_011328.log` |
| Opening inventory `--profile full --plan` (CLI) | Exit 3 **by design**: the loader requires the full master data to be loaded first ("missing rows: products 480, product_prices 1,920, customers 19,900"). It runs after the master-data `--apply` in the deployment. Log `logs/opening_inventory_full_plan_20261120_011337.log` |
| Opening inventory plan, read-only, the loader's own `plan_inventory` | **7,680 listings and 5,808 opening-balance movements (18,406 units) to insert; 320 listings and 288 opening balances present; 0 conflicts**; 69 allowed reorder-point drifts (pilot R0). Inventory fingerprint `aa97c9b0…dc8b6`, the same as the sandbox load |
| **Fresh pre-S7 backup** | `database/backups/nordiccart_oltp_20261120_011452_pre_s7.dump`, **377,087 bytes, SHA-256 `A1D9DF29A1535728CC23FFE68D294934B943E6A92BE02ECED9E907DD0722C460`** (Get-FileHash and Python hashlib agree). Taken as `postgres` with `pg_dump -Fc`; the password was typed by you in a local terminal window and never seen. `database/backups/` is git-ignored |
| Archive integrity | `pg_restore --list` exit 0: custom format, gzip, pg_dump 18.4 from server 18.4, database `nordiccart_oltp`, created 2026-11-20 01:14:53. 611 TOC entries: 24 TABLE DATA, 87 TRIGGER, 104 ACL |
| Restore test (sandbox) | `pg_restore --exit-on-error` into a fresh database `nordiccart_s7_prerestore`: **exit 0 in 0.9 s**. Database privileges re-applied, TimeZone UTC. **22 of 22 tables (business columns) plus the change_log metadata identical to the real database** |

### Verdict: **READY** for S7 deployment, after your explicit approval

Deployment order, as `nordiccart_loader` through `config/.env`:
```
python python/run_master_data_seed.py --profile full --apply      # 22,300 rows
python python/run_opening_inventory.py --profile full --plan      # now expected: 7,680 + 5,808, 0 conflicts
python python/run_opening_inventory.py --profile full --apply     # 7,680 listings, 5,808 opening balances, 18,406 units
```
Afterwards:
- run the pilot `--plan` checks and scripts 01–18;
- check the counts: 500 products, 2,000 prices, 20,000 customers, 8,000 listings, 6,096 opening balances, 19,866 units.

The recovery point is the backup above. S8 has not been started.

## 7. Real deployment (2026-11-20, authorized): **SUCCESS**

Each loader ran once, as `nordiccart_loader` through `config/.env`, with the loaders' own `--yes` flag (no piped input). No retry, no restore. No migration, no engine run, no S8.

**Execution log**

| Time | Step | Result |
|---|---|---|
| 01:21 | Recheck (read-only) | Backup unchanged (SHA-256 `A1D9DF29…C460`, 377,087 bytes, `pg_restore --list` exit 0). Real baseline equal to the readiness state: migrations 001–009; 20 / 80 / 100 / 320 / 288; 20 orders; 326 movements; 1,467 units; 96 rates; change_log 1,714 (max 1932); 0 runs; no other sessions or prepared transactions |
| 01:21:30–01:21:48 | `run_master_data_seed.py --profile full --apply --yes` | **Exit 0.** Validation 0 issues. Plan: 22,300 to insert (products 480, product_prices 1,920, customers 19,900), 250 present, **0 conflicts**. **Committed 22,300 rows inserted, 22,550 verified, 22,300 change_log rows**, 18.2 s. Log `logs/master_data_full_apply_20261120_012130.log` |
| 01:21:52–01:22:03 | `run_opening_inventory.py --profile full --plan` | **Exit 0.** "Master data present and unchanged". **7,680 listings and 5,808 opening balances to insert, 0 conflicts**, 69 allowed drifts (pilot R0). Fingerprint `aa97c9b0…dc8b6`, the same as tested. Log `logs/opening_inventory_full_plan_20261120_012152.log` |
| 01:22:10–01:22:24 | `run_opening_inventory.py --profile full --apply --yes` | **Exit 0.** **Committed 7,680 listings and 5,808 opening balances (18,406 units) inserted, 8,000 listings verified, 19,296 change_log rows**, 14.1 s. Log `logs/opening_inventory_full_apply_20261120_012210.log` |

**Final counts (real database, read-only)**

| Item | Expected | Actual |
|---|---:|---:|
| products | 500 | **500** |
| product_prices | 2,000 | **2,000** |
| customers | 20,000 | **20,000** |
| inventory listings | 8,000 | **8,000** |
| opening-balance movements | 6,096 | **6,096** |
| opening inventory units | 19,866 | **19,866** |
| exchange rates (unchanged) | 96 | **96** |
| original orders (unchanged) | 20 | **20** |
| change_log rows | 43,310 | **43,310** (max id 43,528) |

Also:
- 16 locations (12 stores, 4 warehouses); 6,134 movements; 19,873 units on hand, 0 reserved;
- 4 purchase orders; 69 revisions; 0 activity runs; migrations 001–009 unchanged.

The real database is **identical to the sandbox S7 database** `nordiccart_s7_full`: 22 of 22 tables on business columns, plus change_log metadata.

**Post-deployment checks (read-only)**

| Check | Result |
|---|---|
| `run_transactions.py --plan` | Exit 0. Master data and opening inventory present and unchanged. Later activity outside the pilot: 0 orders, 21 movements; 7,680 listings outside the pilot (5,808 E2 opening balances, 18,406 units). **Conflicts 0, blocking 0**; nothing to write |
| `run_replenishment.py --plan` | Exit 0. Pilot transactions complete (100 units verified). **Conflicts 0, blocking 0**; nothing to write |
| `run_exchange_rates.py --plan` | Exit 0. **E1: 0 rows pending** |
| Scripts 01–13 and 15–18 (as `nordiccart_loader`, read-only) | 844 rows: 675 PASS, 166 INFO, **0 FAIL**, 3 known WARN (script 07: 2; script 10: 1) |
| Script 14 (as `postgres`, read-only) | The loader may not read change_log row contents (V008), so it ran in a local terminal window where you typed the password; it was never seen. **44 rows: 37 PASS, 7 INFO, 0 FAIL** |
| **Scripts 01–18 together** | **888 rows: 712 PASS, 173 INFO, 0 FAIL**, 3 known WARN, the same as the sandbox result |

### Verdict: **S7 SUCCESS**

The real database now holds the full profile and the demand-calibrated opening inventory. The pilot records and the exchange rates are unchanged.

Recovery point if ever needed (only with your approval): `database/backups/nordiccart_oltp_20261120_011452_pre_s7.dump`.

S8 (24-month rehearsal) has not been started.
