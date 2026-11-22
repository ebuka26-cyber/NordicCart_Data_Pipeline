# Pre-replenishment backup and readiness report

| Item | Value |
|---|---|
| Date | 2026-11-18. Times are local (CET, UTC+1) unless marked UTC. |
| Database | `nordiccart_oltp` on localhost:5432, PostgreSQL 18.4, migrations 001 to 007 |
| Purpose | Fresh backup and read-only readiness checks before the replenishment pilot load (Phase 3, step 4) |
| Database writes | **None.** Every session on the real database was forced read-only (`default_transaction_read_only=on`). Replenishment Load was **not** run. |
| **Result** | **Backup complete and verified. Dry Run and Check passed: 17 units of work planned, 0 conflicts, 0 blocking. The database is unchanged and ready for the load once it is authorized.** |

## 1. Backup

| Item | Value |
|---|---|
| Filename | `nordiccart_oltp_20261118_032247_pre_replenishment.dump` |
| Location | `<project folder>\database\backups\` |
| Size | 257,336 bytes (251.3 KB) |
| SHA-256 | `A4907BB9E9BDEBD9A5884660178F2FE2BCE69494A15677004BA43314122A8078` |
| Format | PostgreSQL custom format (`--format=custom --compress=6`, gzip) |
| Created | 2026-11-18 03:22:47 by `pg_dump` 18.4 from server 18.4, in about 1.1 s, exit code 0 |
| Session | Read-only (`PGOPTIONS=-c default_transaction_read_only=on`). Credentials came from `config/.env` and were not printed. |
| Log | `logs/pg_dump_nordiccart_oltp_20261118_032247.log` (72 lines, 0 error or warning lines) |

The earlier backup `nordiccart_oltp_20261118_012230_pre_pilot_transactions.dump` (taken before the 20 pilot orders) is kept unchanged. The new file is the restore point for the replenishment load.

## 2. Archive verification

| Check | Result |
|---|---|
| File signature | Starts with `PGDMP` (custom-format archive) |
| Table of contents (`pg_restore --list`) | Exit 0. 383 entries (378 selected), format CUSTOM, compression gzip, dumped from and by 18.4. 19 TABLE and 19 TABLE DATA entries (all tables of `public` and `meta`). |
| Database-level entries (`--list --create`) | `DATABASE nordiccart_oltp` and `DATABASE PROPERTIES` present, including `ALTER DATABASE nordiccart_oltp SET "TimeZone" TO 'UTC'` |
| **Full test restore** | Restored into a new database (`verify_pre_replenishment`) on the **disposable PostgreSQL 18.4 sandbox cluster** (port 55432, not the real server). `pg_restore --exit-on-error` exit 0 in 0.6 s. The test database was dropped afterwards and the sandbox stopped. |
| Verification scripts on the restored copy | 11: 43 rows, 33 PASS, 10 INFO, 0 FAIL. 12: 24 rows, 17 PASS, 7 INFO, 0 FAIL. 13: 50 rows, 44 PASS, 6 INFO, 0 FAIL. |

Required content, read from the restored copy and compared with the live database (read-only, 03:22:37, just before the dump):

| Requirement | Expected | Live database | Restored backup |
|---|---|---|---|
| Customer orders | 20 | 20 (fulfilled 14, payment_failed 2, cancelled 4) | 20 (same split) |
| Inventory movements | 305 | 305 (opening_balance 288, sale 17) | 305 (same split) |
| Units of stock on hand | 1,441 | 1,441 (0 reserved) | 1,441 (0 reserved) |
| change_log rows | 1,426 | 1,426 | 1,426 |
| Migrations | V001 to V007 | 7 (001, 002, 003, 004, 005, 006, 007) | 7 (001 to 007) |
| Reorder points (all / warehouses) | (before R0) | 700 / 323 | 700 / 323 |

Every table, live and restored (exact `count(*)`): all **19 of 19 equal**.

| Table | Rows | Table | Rows |
|---|---|---|---|
| meta.schema_migrations | 7 | public.order_items | 26 |
| public.change_log | 1,426 | public.orders | 20 |
| public.change_log_checkpoints | 0 | public.payment_attempts | 21 |
| public.countries | 4 | public.product_categories | 10 |
| public.currencies | 4 | public.product_prices | 80 |
| public.customers | 100 | public.products | 20 |
| public.exchange_rates | 0 | public.stock_locations | 16 |
| public.inventory_balances | 320 | public.stores | 12 |
| public.inventory_movements | 305 | public.warehouses | 4 |
| public.inventory_reservations | 23 | | |

Incident during verification (no effect on the backup or the real database): the first test-restore attempt hung because the sandbox server had been started inside a piped command. Stopping that command left the sandbox server unable to start new sessions ("could not reserve shared memory region ... error code 487" in its log). The sandbox server was stopped and started again cleanly, and the test restore then succeeded as reported above. The backup file was not touched; its checksum was recorded before the test restore.

## 3. Replenishment Dry Run (no database)

Command: `python python/run_replenishment.py` (the "NordicCart - Replenishment: Dry Run" configuration). Exit code 0. Log: `logs/replenishment_pilot_dry_run_20261118_033021.log`.

| Item | Value |
|---|---|
| **Plan fingerprint (SHA-256)** | `858bbfd09597030b573273c9885ffae0c0299e4ed146e8aca7c98f0e05531236` (the same value as in the sandbox test report and the recorded unit-test fingerprint) |
| Built on | master data `c46181a25bd6fdc1ad2c8fedab185c19802041c46b4aae703e66a0f58d768f64`; opening inventory `5c7a4eba5057caabaa0d18b6fed95d14ba2a11523a5fd0bf39f99d9e3df38d35`; pilot transactions `fabcd571a3b9f66827f20e54d0ebc421be5f68937af558383ee4b84f7c466264` (last event 2024-11-15 08:44:22 UTC) |
| Validation | **0 issues** |
| Window | 2024-11-15 to 2024-12-06; first event 2024-11-15 16:00:00 UTC, last 2024-12-02 12:46:04 UTC |
| Reorder point revisions | 4 warehouses, 69 listings; warehouse total 323 -> 533 |
| Transfers | 4 documents, 8 lines, 10 units, one per market; 0 backorders |
| Purchase orders | 4 (4 forced, 0 natural), all lines `forced_reorder`, 26 units; 5 delivery notes (1 split) |
| Stock | 1,441 -> 1,467 on hand; 0 units in transit at the end |
| Units of work | 17 (revise 4, dispatch 4, arrive 4, receive 5) |
| CSV preview | `output/replenishment/pilot/` |

## 4. Replenishment Check (real database, read-only)

Command: `python python/run_replenishment.py --plan` (the "NordicCart - Replenishment: Check" configuration), with `PGOPTIONS=-c default_transaction_read_only=on`. Exit code 0. Log: `logs/replenishment_pilot_plan_20261118_033021.log`.

| Item | Result |
|---|---|
| Connection | postgres@localhost:5432/nordiccart_oltp, PostgreSQL 18.4 |
| Preconditions | Migrations 001 to 007; master data and opening inventory present and unchanged; **pilot transactions complete (100 units of work verified against their plan)**; V007 idempotency indexes present |
| Planned / present / to write | **17 / 0 / 17** units of work |
| To run by kind | R0 reorder points 4, T1 dispatch 4, T2 arrival 4, R1 receipt 5 |
| **Conflicts** | **0** |
| **Blocking issues** | **0** |
| Outcome | `plan finished: 17 units of work would be written, nothing was written.` |

## 5. Planned steps (business time, UTC)

From `output/replenishment/pilot/units_of_work.csv`, in run order. Each step is one transaction.

| # | Business time (UTC) | Unit of work | Document / location | Content |
|---|---|---|---|---|
| 1 | 2024-11-15 16:00:00 | R0 reorder points | FI-WH-01 | 17 reorder points, 54 -> 93 |
| 2 | 2024-11-15 17:00:00 | R0 reorder points | DK-WH-01 | 17 reorder points, 67 -> 115 |
| 3 | 2024-11-15 17:00:00 | R0 reorder points | NO-WH-01 | 17 reorder points, 63 -> 102 |
| 4 | 2024-11-15 17:00:00 | R0 reorder points | SE-WH-01 | 18 reorder points, 117 -> 201 |
| 5 | 2024-11-18 04:47:06 | T1 dispatch | TR-FI-2024-000001-FI-STO-02 | WEA-000020 x1 from FI-WH-01 |
| 6 | 2024-11-18 05:13:57 | T1 dispatch | TR-NO-2024-000001-NO-STO-03 | GAM-000008 x1, SPH-000019 x1 from NO-WH-01 |
| 7 | 2024-11-18 05:56:10 | T1 dispatch | TR-SE-2024-000001-SE-STO-03 | SMH-000009 x1, SPH-000014 x2 from SE-WH-01 |
| 8 | 2024-11-18 06:06:16 | T2 arrival | TR-FI-2024-000001-FI-STO-02 | 1 unit at FI-STO-02 |
| 9 | 2024-11-18 06:43:53 | T2 arrival | TR-NO-2024-000001-NO-STO-03 | 2 units at NO-STO-03 |
| 10 | 2024-11-18 06:58:54 | T1 dispatch | TR-DK-2024-000001-DK-STO-01 | ACC-000010 x1, ACC-000013 x2, SPH-000019 x1 from DK-WH-01 |
| 11 | 2024-11-18 07:29:54 | T2 arrival | TR-SE-2024-000001-SE-STO-03 | 3 units at SE-STO-03 |
| 12 | 2024-11-18 08:27:08 | T2 arrival | TR-DK-2024-000001-DK-STO-01 | 4 units at DK-STO-01 |
| 13 | 2024-11-28 08:20:00 | R1 receipt | PO-SE-2024-000001/DN1 | TAB-000003 x2 at SE-WH-01 (forced, split 1 of 2) |
| 14 | 2024-11-29 07:11:01 | R1 receipt | PO-DK-2024-000001/DN1 | GAM-000008 x6 at DK-WH-01 (forced) |
| 15 | 2024-11-29 10:11:51 | R1 receipt | PO-FI-2024-000001/DN1 | WEA-000020 x10 at FI-WH-01 (forced) |
| 16 | 2024-12-02 06:49:39 | R1 receipt | PO-NO-2024-000001/DN1 | GAM-000008 x6 at NO-WH-01 (forced) |
| 17 | 2024-12-02 12:46:04 | R1 receipt | PO-SE-2024-000001/DN2 | TAB-000003 x2 at SE-WH-01 (forced, split 2 of 2) |

## 6. Final database counts (after all checks, read-only, 03:30:37)

| Measure | Before (03:22:37) | After (03:30:37) |
|---|---|---|
| Migrations | 7 (001 to 007) | 7 (001 to 007) |
| Orders | 20 | 20 |
| Inventory movements | 305 (opening_balance 288, sale 17) | 305 (opening_balance 288, sale 17) |
| Units on hand / reserved | 1,441 / 0 | 1,441 / 0 |
| Reorder points (all / warehouses) | 700 / 323 | 700 / 323 |
| change_log rows | 1,426 | 1,426 |
| Exact row counts of all 19 tables | (section 2) | **identical, 0 differences** |

Migration files V001 to V007: SHA-256 unchanged.

## 7. What happens when the load is authorized (for the decision)

Expected from the sandbox run of the same plan (`docs/test_reports/2026-11-18_replenishment_pilot_sandbox_test_report.md`):

- `python python/run_replenishment.py --apply` writes 17 units of work in 17 transactions.
- It writes 111 change_log rows: 69 reorder point updates, plus 21 movements and their 21 balance updates.
- Afterwards: 326 movements (288 opening, 17 sale, 5 purchase_receipt, 8 transfer_out, 8 transfer_in), 1,467 units on hand, 0 in transit, warehouse reorder points 533, change_log 1,537.
- Verification: script 14 gives 43 rows, 36 PASS, 7 INFO; scripts 11, 12 and 13 stay at 33/10, 17/7 and 44/6 with 0 FAIL.
- `run_transactions.py --plan` still verifies the pilot (21 later movements, +26 units).
- A repeated Load writes nothing.

Rollback: the stock ledger is insert-only (V007), so the only way back is restoring this backup (drop the database, then `pg_restore --create`), which needs the owner's decision.

## 8. Stop point

Nothing was loaded. The next step, **Replenishment: Load** (`--apply`), waits for the owner's explicit authorization after this report is reviewed.
