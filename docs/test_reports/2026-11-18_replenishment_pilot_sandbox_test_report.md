# Test report: replenishment pilot, disposable PostgreSQL 18 sandbox

| Item | Value |
|---|---|
| Date | 2026-11-18 |
| Scope | Phase 3, step 4: inventory replenishment and stock transfers, implemented per `docs/phase3_replenishment_feasibility.md` (decisions D1 to D7 and D9 to D11 adopted, D8 deferred) |
| Sandbox | A **new, throwaway PostgreSQL 18.4 cluster** (`initdb`, port 55432, trust authentication, UTF8, locale C) in a temporary folder on the same Windows 11 machine. Separate from the owner's server (PostgreSQL 18.4, port 5432). |
| Sandbox data | `nordiccart_oltp` restored from `database/backups/nordiccart_oltp_20261118_012230_pre_pilot_transactions.dump` (the verified pre-pilot backup), then the 20 pilot orders replayed with `run_transactions.py --apply --yes`: 100 units of work, fingerprint `fabcd571…`, 280 change_log rows. This is the same state as the real database. |
| Software | Python 3.14.7 (project `.venv`), psycopg 3.3.9 (libpq 18.4), pytest 9.2.0 |
| Real database | **Not written.** Only two read-only checks were run against it (section 7), in sessions forced read-only (`default_transaction_read_only=on`). Row counts are identical before and after. |
| Migrations | V001 to V007 not modified (SHA-256 unchanged) |
| **Result** | **Every test passed, 0 failed.** 276 pytest tests; 14 concurrency scenarios in each of 3 runs; 268 SQL behaviour tests; verification scripts 01 to 14 with 0 FAIL; 11 of 11 deliberate defects caught. |

## 1. What was built

New:

| File | Purpose |
|---|---|
| `python/nordiccart/stock/__init__.py`, `stock/ledger.py` | Shared stock ledger: on hand, reserved and **in transit** per (location, sku). Order operations behave exactly as before (pilot fingerprint unchanged). Receipt, dispatch and arrival are strict and refuse what V007 refuses. |
| `python/nordiccart/replenishment/config.py` | Reads and checks `[replenishment]`; pilot profile only |
| `python/nordiccart/replenishment/policy.py` | (s, S) policy: inventory position, trigger at or below the reorder point, order-up-to levels, echelon demand and reorder points (below the opening quantity), case packs, allocation order and warehouse protection, launch/discontinuation/location eligibility |
| `python/nordiccart/replenishment/model.py` | Documents (reorder point revision, transfer documents, purchase orders, delivery notes); units of work R0, R1, T1, T2; replay; fingerprint; summary |
| `python/nordiccart/replenishment/generate.py` | Business-time simulation: R0, evening store reviews, morning dispatches and arrivals, Monday warehouse reviews, deliveries, forced and split scenarios |
| `python/nordiccart/replenishment/validate.py` | Independent validation (about 50 named rules) including a stock replay with conservation after every unit of work |
| `python/nordiccart/replenishment/load.py` | Loader: preconditions (pilot complete), read-only plan, resumable apply under an advisory lock, one transaction per unit of work, verification |
| `python/nordiccart/replenishment/cli.py`, `__init__.py`, `__main__.py`, `python/run_replenishment.py` | Command line: dry run, `--plan`, `--apply` (typed confirmation or `--yes`), CSV preview, logs, exit codes |
| `sql/verification/14_verify_replenishment.sql` | Read-only verification of the replenishment (43 rows) |
| `tests/python/test_replenishment.py` | 61 unit tests (no database) |
| `tests/python/test_replenishment_db.py` | 22 database integration and integrity tests (opt-in, own databases) |
| `docs/phase3_replenishment_guide.md` | VS Code execution guide |

Changed:

| File | Change |
|---|---|
| `python/nordiccart/transactions/model.py` | `StockLedger` now extends the shared ledger (same behaviour); new `TransactionPlan.last_event_at()` |
| `python/nordiccart/transactions/load.py` | Accepts purchase receipts and transfers dated **after** the plan's last event and adds them to the expected stock, so the pilot keeps verifying. Still blocks them while pilot steps are missing (business-time order), and still blocks any receipt or transfer dated inside the pilot and any adjustment, write-off or return. |
| `python/nordiccart/transactions/cli.py` | Typed confirmation: no keyboard input now cancels (exit 4) instead of crashing (see 8.1) |
| `config/generator.toml` | New `[replenishment]` and `[replenishment.scenarios]` sections at the end; existing values unchanged (all earlier fingerprints identical) |
| `.vscode/launch.json` | 3 entries added after the existing 7: Replenishment Dry Run, Check, Load |
| `sql/verification/11_verify_master_data.sql` | Revision 4: 204 fails only on adjustments, write-offs and return restocks; new INFO 208 (43 rows: 33 PASS, 10 INFO) |
| `sql/verification/12_verify_opening_inventory.sql` | Revision 3: 201 likewise; new INFO 204 (24 rows: 17 PASS, 7 INFO) |
| `sql/verification/13_verify_pilot_transactions.sql` | Revision 2: 504 accepts receipts and transfers after the pilot's last event; 505 expects 1441 + their net change (50 rows: 44 PASS, 6 INFO) |
| `tests/python/test_transactions_db.py` | Script 11/12 counts; 2 new tests (replenishment after the pilot accepted; before completion blocked) |
| `tests/python/test_master_data_seed_db.py`, `test_opening_inventory_db.py` | One assertion each: new script 11/12 counts |
| `tests/python/test_v007_concurrency.py` | 3 new scenarios (11 to 13) and their fixtures; invariants become scenario 14 |
| `python/README.md`, `tests/README.md`, `sql/README.md`, `config/README.md`, 3 earlier guides, status document | Documentation |

Not changed: migrations V001 to V007, the master data and opening inventory packages, the transaction generator and validator, verification scripts 01 to 10, the SQL behaviour tests.

## 2. The replenishment pilot plan

Dry run, fingerprint `858bbfd09597030b573273c9885ffae0c0299e4ed146e8aca7c98f0e05531236`, validation 0 issues:

| Unit of work | Count | Content |
|---|---|---|
| R0 reorder points | 4 | 69 warehouse listings revised to echelon demand; total warehouse reorder points 323 -> 533 (on-sale listings 302 -> 504, as estimated in the feasibility study); every revised value below the opening quantity |
| T1 dispatch / T2 arrival | 4 / 4 | `TR-FI-2024-000001-FI-STO-02` (1 line, 1 unit), `TR-NO-2024-000001-NO-STO-03` (2, 2), `TR-SE-2024-000001-SE-STO-03` (2, 3), `TR-DK-2024-000001-DK-STO-01` (3, 4). Dispatched Monday 18 November 2024 between 06:13 and 07:58 local time, arrived between 07:43 and 09:27 local time (79 to 94 minutes later, before the stores open at 10:00). These are exactly the 8 store listings at or below their reorder point after the pilot. |
| R1 receipt | 5 | `PO-SE-2024-000001/DN1` and `/DN2` (TAB-000003, 2 + 2), `PO-DK-2024-000001/DN1` (GAM-000008, 6), `PO-FI-2024-000001/DN1` (WEA-000020, 10), `PO-NO-2024-000001/DN1` (GAM-000008, 6). All 4 purchase orders are flagged `forced_reorder`; each is the market's warehouse listing with the least headroom above its reorder point. |
| **Total** | **17** | 10 units transferred, 26 units received, 0 in transit at the end; stock 1,441 -> 1,467; 0 backorders; first event 2024-11-15 16:00:00 UTC, last 2024-12-02 12:46:04 UTC |

Reviews whose follow-ups would end after the window were skipped as designed: warehouse reviews on 25 November and 2 December, and the store reviews on 6 December.

## 3. Sandbox workflow (logs in `logs/sandbox_final_20261118/`)

| Step | Command (sandbox config and env file) | Exit | Result |
|---|---|---|---|
| A | Restore the pre-pilot backup into a new `nordiccart_oltp` | 0 | 7 migrations, 100 customers, 320 listings, 1,460 units, 0 orders, 288 movements, 1,146 change_log rows |
| B | `run_transactions.py --apply --yes` | 0 | 100 units of work in 100 transactions, 20 orders placed and verified, 280 change_log rows |
| C | Scripts 11, 12, 13 before replenishment | 0 | 43 rows: 33 PASS, 10 INFO; 24 rows: 17 PASS, 7 INFO; 50 rows: 44 PASS, 6 INFO; 0 FAIL |
| D1 | `run_replenishment.py` (dry run) | 0 | 17 units of work, fingerprint `858bbfd0…`, validation 0 issues |
| D2 | `run_replenishment.py --plan` | 0 | 17 to write, 0 present, conflicts 0, blocking 0; nothing written |
| D3 | `run_replenishment.py --apply` without `--yes` (no keyboard input) | **4** | `No keyboard input: add --yes to confirm the load.` `cancelled: nothing was written.` |
| D4 | `run_replenishment.py --apply --yes` | 0 | `committed: 17 units of work in 17 transactions (revise 4, dispatch 4, arrive 4, receive 5), 17 verified, 111 change_log rows written` (69 reorder point updates + 21 movements + 21 balance updates), 0.7 s |
| D5 | `run_replenishment.py --apply --yes` again | 0 | `nothing to write: the database already holds every replenishment unit of work (17, verified).` |
| E1 | `run_transactions.py --plan` | 0 | orders 20/20/0, units of work 100/100/0, `later replenishment ...: 21 movements, net +26 units`, conflicts 0, blocking 0 |
| E2 | `run_transactions.py --apply --yes` | 0 | `nothing to write: the database already holds all 20 pilot orders and every unit of work (verified).` |
| F1 | `run_opening_inventory.py --plan` | 0 | 0 to insert, `conflicts: 0   reorder point drift (allowed): 69` (the R0 revision) |
| F2 | `run_master_data_seed.py --plan` | 0 | 0 rows to insert |
| G | Verification scripts 01 to 14 after replenishment | 0 | Section 4 |
| H | SQL behaviour tests with all data present | 0 | Section 5; afterwards still 20 orders, 326 movements, 1,467 units, 1,537 change_log rows (1,146 + 280 + 111), no test rows left |

## 4. SQL verification after replenishment (sandbox, read-only session)

| Script | Rows | PASS | INFO | WARN | FAIL |
|---|---|---|---|---|---|
| 01 to 10 (structure, Phase 2) | 551 | 477 | 72 | 2 | **0** |
| 11 master data (revision 4) | 43 | 33 | 10 | 0 | **0** |
| 12 opening inventory (revision 3) | 24 | 17 | 7 | 0 | **0** |
| 13 pilot transactions (revision 2) | 50 | 44 | 6 | 0 | **0** |
| 14 replenishment (new) | 43 | 36 | 7 | 0 | **0** |

The 2 WARN rows are the known V007 triggers on V006 tables (script 07), unchanged since Phase 2. Phase 2 baseline: 477 PASS, 0 FAIL, 2 WARN.

Key script 14 values: 101 receipt lines 5; 102 units received 26; 103 delivery notes 5; 104 purchase orders 4; 105 split 1; 106 transfer documents 4; 107/108 transfer lines 8/8; 109 units dispatched 10; 110 in transit 0; 111 warehouse reorder points 533; 201 to 208, 301 to 307, 401 to 403 and 501 to 507 all 0 except 505 and 506 (1,467 = 1,467). INFO 601: receipts DK 1/6, FI 1/10, NO 1/6, SE 2/4. INFO 602: transfers DK 1/3/4, FI 1/1/1, NO 1/2/2, SE 1/2/3. INFO 604: store listings at or below their reorder point now: 0.

## 5. Automated tests (exact counts)

Run on Python 3.14.7 against the sandbox, deprecation warnings treated as errors:

| Test file | Kind | Result |
|---|---|---|
| `test_replenishment.py` | Unit, no database | **61 passed, 0 failed** |
| `test_replenishment_db.py` | Integration and database integrity | **22 passed, 0 failed** |
| `test_transactions.py` | Unit (regression; pilot fingerprint unchanged) | 53 passed, 0 failed |
| `test_transactions_db.py` | Integration (2 new) | 23 passed, 0 failed |
| `test_opening_inventory.py` | Unit (regression) | 38 passed, 0 failed |
| `test_opening_inventory_db.py` | Integration (regression) | 18 passed, 0 failed |
| `test_master_data_generator.py` | Unit (regression) | 48 passed, 0 failed |
| `test_master_data_seed_db.py` | Integration (regression) | 13 passed, 0 failed |
| **pytest total** | | **276 passed, 0 failed** |
| `test_v007_concurrency.py` | Concurrency, multi-session | **14 of 14 PASS** in each of 3 runs |
| `tests/sql/test_foundation_constraints.sql` | SQL behaviour, data present | 44 of 44 PASS |
| `tests/sql/test_v006_orders_and_payments.sql` | SQL behaviour, data present | 119 of 119 PASS |
| `tests/sql/test_v007_inventory.sql` | SQL behaviour, data present | 105 of 105 PASS |

All 55 Python files compile on Python 3.14.7 with warnings as errors.

### Unit tests, `test_replenishment.py` (61)
- **Shared ledger (3):**
  - receive, dispatch and arrive keep on hand + in transit = start + received;
  - refused: moving reserved units, a zero receipt, a transfer to the same location, a second dispatch of a line, an arrival with nothing in transit;
  - snapshot round trip; the transaction ledger is the shared one; the pilot replay gives 1,441 units.
- **Policy (7):**
  - case-pack rounding; echelon demand = online + market stores;
  - echelon reorder point rounds up and stays below the opening quantity;
  - order-up-to levels (display minimum, above the reorder point); trigger at or below the reorder point;
  - eligibility: CAM-000007 from 7 days before its launch on 2025-03-19, not when discontinued, not at an inactive or unopened location;
  - allocation order (stores at 0, flagships, ratio) and warehouse protection.
- **The pilot as configured (16):**
  - planned counts; transfers serve exactly the store listings at or below their reorder point (computed independently from the pilot replay) with quantity = S - position;
  - all 4 purchase orders forced, above their reorder point and in whole case packs; each one the least-headroom listing;
  - SE split into DN1 and DN2 adding up; echelon figures 302 -> 504; nothing for unlaunched products;
  - every unit of work after the pilot, in business-time order and in the right hours; reference formats;
  - conservation after every unit of work and nothing in transit; skipped reviews;
  - validation passes; recorded fingerprint; another seed still validates;
  - a short warehouse (SE-WH-01 SPH-000014 at 13, reorder point 12) sends 1 of 2 units, records a backorder and places a **natural** purchase order;
  - a window too short for the forced orders is refused.
- **Validator fault injection (24):**
  - placement and routes: receipt at a store, transfer to another market, reference naming another store;
  - timing: arrival before dispatch, arrival after the store opens, dispatch on a Sunday, receipt on a Saturday, R0 before the pilot ends, R0 at the wrong time, delivery after the window, supplier lead time too short;
  - stock: more dispatched than available, warehouse drawn below its reorder point, warehouse over capacity;
  - documents: duplicate transfer reference, split notes not adding up, order not in case packs, numbering gap;
  - triggers and scenario counts: forced order missing, store above its reorder point refilled, forced order that the reorder point would have triggered;
  - plan data: revised reorder point at the opening quantity, product not launched yet, plan built on other stock.
- **Settings and command line (11):**
  - 8 invalid settings refused, plus the full profile refused;
  - dry run writes the 7 preview files without a database, marks forced orders, never logs the password;
  - typed confirmation without keyboard input cancels in both command lines.

### Database tests, `test_replenishment_db.py` (22)
- **Loading:**
  - plan read-only (0 present, 17 to write);
  - apply runs 17 units of work with 111 change_log rows, and a rerun runs 0 with 0 change_log rows;
  - the final balances, reorder points and in-transit state equal the plan replay;
  - scripts 11, 12 and 13 pass before and after, and script 14 after;
  - **the transaction loader still verifies the pilot afterwards**: 100/100 done, 21 later movements, +26 units, verification (20, []), apply runs 0;
  - the R0 revision is 69 allowed drifts for the opening inventory loader.
- **Idempotency and recovery:**
  - interrupted after 1, 4, 6, 9, 13 and 16 units of work, it continues and ends verified with script 14 passing; the pilot verifies at every point, including with stock in transit;
  - a second loader is refused;
  - a receipt with another quantity and an arrival at another store are conflicts (nothing written);
  - a receipt dated inside the pilot or outside the plan blocks both loaders; a stock adjustment blocks;
  - a planned dispatch beyond available stock rolls back that step only, and the next run continues.
- **Integrity:**
  - the database refuses a second identical delivery note (23505 `uq_inventory_movements_document_line`) and a second arrival (23505 `uq_inventory_movements_one_transfer_in_per_out`);
  - a transfer_out of units reserved by an order is refused (23514 `ck_inventory_balances_no_oversell`, rolled back);
  - V007 **accepts** a receipt at a store, a cross-border transfer and a backdated transfer, and script 14 fails checks 201, 204, 306 and 110 for them; script 13 fails 504.
- **Preconditions and command line:**
  - a database without the complete pilot is refused;
  - the command line: plan, apply refused without `--yes` (exit 4), apply, rerun "nothing to write", then the transaction check exits 0.

### Concurrency, `test_v007_concurrency.py` (14 scenarios, 3 runs)

Scenarios 1 to 10 and the invariants are unchanged and passed. New:

| Scenario | Result (all 3 runs) |
|---|---|
| 11. A transfer dispatch (loader style: lock, check, insert) races U2 for the same 5 units, 3 each | PASS. One wins and nothing is created or lost (on hand + in transit = 5). In all 3 runs the transfer won (U2 got `out_of_stock`, 2 left, 3 in transit). |
| 12. A transfer that skips the lock tries to take reserved units (4 on hand, 3 reserved, transfer 4) | PASS: 23514 `ck_inventory_balances_no_oversell`, stock unchanged 4/3 |
| 13. The same arrival recorded by two sessions at once | PASS: one stored, the other 23505 `uq_inventory_movements_one_transfer_in_per_out`; warehouse 4, store 2 |
| 14. Ledger invariants after all scenarios | PASS: 13 balances, 0 inconsistent |

## 6. Mutation checks (do the tests catch defects?)

Each defect was planted in a scratch copy of the project, one at a time, and the relevant test files were run against the sandbox:

| Defect | Caught by |
|---|---|
| M1 replenishment loader ignores the advisory lock | two loaders at once |
| M2 loader never recognises a recorded arrival | apply then rerun |
| M3 transaction loader does not count later replenishment in its stock check | the pilot still verifies |
| M4 transaction loader accepts replenishment while pilot steps are missing | before-completion test |
| M5 loader skips the availability check before a dispatch | unplanned shortage test (the database CHECK would still refuse the stock move) |
| M6 generator ignores launch dates | unlaunched products test |
| M7 generator ignores requests already waiting for dispatch | plan generation fails |
| M8 validator drops the store trigger rule | fault test |
| M9 validator drops the warehouse protection rule | fault test |
| M10 script 14 stops checking cross-border transfers | V007-gap test |
| M11 echelon reorder point no longer capped below the opening quantity | policy unit test |

**11 of 11 caught.**

## 7. Read-only checks against the real database

Both commands ran with the normal `config/.env` and `PGOPTIONS=-c default_transaction_read_only=on`:

| Check | Result |
|---|---|
| Row counts before | orders 20, movements 305, units 1,441, reorder points 700, change_log 1,426 (port 5432) |
| `run_transactions.py --plan` (modified loader) | exit 0; orders 20/20/0, units of work 100/100/0, later replenishment 0, conflicts 0, blocking 0 |
| `run_replenishment.py --plan` | exit 0; `pilot transactions complete (100 units of work verified)`; **17 units of work to write**, conflicts 0, blocking 0; nothing written |
| Row counts after | identical: orders 20, movements 305, units 1,441, reorder points 700, change_log 1,426 |

The real database is ready for the load. It has **not** been loaded: that needs the owner's explicit approval.

## 8. Problems found during testing (fixed)

1. **Typed confirmation on Windows.** Windows reports the NUL device as a terminal, so `--apply` without `--yes` from a shell with no keyboard input crashed with `EOFError` (exit 1) instead of cancelling (exit 4). The transaction command line had the same latent problem. Both now cancel cleanly (step D3), with a unit test for each.
2. **Split delivery of a single unit.** For a market whose forced order is split, the least-headroom listing could need only 1 unit (case pack 1), which cannot be split. The forced pick for split markets now skips 1-unit candidates. This was not triggered by the configured pilot (TAB-000003, 4 units).
3. **Script 14, check 302.** The year cast is now inside a CASE, so a malformed reference cannot abort the script.
4. A test-only mistake in one invalid-settings case (a trailing comma made the TOML unreadable) was corrected.

## 9. Unresolved issues and limits

1. **Not loaded into the real database.** By instruction. The real `--plan` shows 17 units of work and 0 conflicts. A backup before the load is recommended (guide, "Before you start").
2. **Only Python 3.14.7.** Python 3.13 is not installed on this machine, so the earlier two-version runs were not repeated.
3. **Concurrency scenario 11** passed 3 times but always with the transfer winning. The reverse order (reservation first) is covered deterministically by scenario 12 (database CHECK) and by the database test of the loader's availability check.
4. **Forced purchase orders.** The 4 receipts exist because the pilot forces them (D1). Natural warehouse reorders were exercised only in a unit test (short warehouse). At pilot demand (about 6% of modelled demand) warehouses do not reach their reorder points; D8 (demand recalibration) is deferred.
5. **Sales gap.** The standalone pilot has no orders between 2024-11-15 and 2024-12-06. Later orders must start after its last event (2024-12-02 12:46:04 UTC), or the combined simulation must generate both together.
6. **V007 limits remain**, as the database tests document: no transfer destination column, no closure for stock lost in transit, receipts at stores and cross-border transfers accepted, capacity not enforced, `occurred_at` can be backdated. The application, the validator and script 14 enforce these. Optional V008 hardening is listed in the feasibility document, section 9.
7. **Purchase orders exist only in the plan and CSV** (no PO tables in DBML revision 3). The PO number travels in each receipt's `external_reference`.
8. **Script 14 is pinned to the pilot counts** (params block). It will report FAIL after later activity until the params are updated.
9. **Validator rule `transfer_position`** assumes no orders between a store review and its dispatch, which holds in this standalone pilot. The combined simulation will need it revisited.
10. **Opening inventory check after R0** reports 69 reorder point drifts (allowed, expected), not 0.
11. **Two log folders.** Trial sandbox runs from earlier the same night are in `logs/sandbox_20261118/`; deleting them was blocked by a path protection. The evidence for this report is in `logs/sandbox_final_20261118/` (command-line logs and `before_*` / `after_*` / `sqltest_*` CSV outputs).
