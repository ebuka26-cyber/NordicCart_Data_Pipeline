# Replenishment pilot: execution and verification report

| Item | Value |
|---|---|
| Date | 2026-11-18. Run times are local (CET, UTC+1); business event times are UTC and marked as such. |
| Database | `nordiccart_oltp` on localhost:5432, PostgreSQL 18.4, migrations 001 to 007 |
| Phase | Phase 3, step 4: inventory replenishment and stock transfers (pilot) |
| Authorization | Written authorization from the database owner on 2026-11-18 to load the approved replenishment pilot, after the readiness report (`2026-11-18_pre_replenishment_backup_and_readiness_report.md`) was reviewed |
| Executed by | Claude Code (AI coding agent) in the project folder, on the owner's instruction |
| Tool | `python/run_replenishment.py`, Python 3.14.7, psycopg 3.3.9; code, config and SQL unchanged since the sandbox-tested and reviewed version |
| **Result** | **SUCCESS. 17 of 17 units of work executed and verified, 0 errors, 0 warnings, 0 retries. All 160 verification rows of scripts 11 to 14 give 0 FAIL. All 20 orders still verify. No replenishment work remains. Every expected final value was reached.** |

## 1. Expected final state compared with the actual result

| Expected (authorization) | Actual | Met |
|---|---|---|
| 20 customer orders unchanged | 20 orders (fulfilled 14, payment_failed 2, cancelled 4). Order-side change_log rows unchanged (orders 72, order_items 26, payment_attempts 56, inventory_reservations 46), so no order row was written. | Yes |
| 326 inventory movements | 326 (opening_balance 288, sale 17, purchase_receipt 5, transfer_out 8, transfer_in 8) | Yes |
| 1,467 units of stock | 1,467 on hand, 0 reserved | Yes |
| 0 units in transit | 0 (every transfer_out has its transfer_in) | Yes |
| Warehouse reorder points totaling 533 | 533 (DK 121, FI 99, NO 108, SE 205) | Yes |
| 1,537 change_log rows | 1,537 | Yes |
| 0 failed verification checks | Scripts 11 to 14: 160 rows, 130 PASS, 30 INFO, **0 FAIL** | Yes |

## 2. Pre-load checks

| Check | Result |
|---|---|
| Pre-replenishment backup | `database/backups/nordiccart_oltp_20261118_032247_pre_replenishment.dump`, 257,336 bytes, SHA-256 `A4907BB9E9BDEBD9A5884660178F2FE2BCE69494A15677004BA43314122A8078`: **matches the readiness report** |
| Pre-pilot backup (earlier) | `database/backups/nordiccart_oltp_20261118_012230_pre_pilot_transactions.dump`, 241,404 bytes, SHA-256 `86EAEA1E4860C47792D406060C02F4DDE80032399F073F76BCA58B5572E7D7CB`: matches its recorded value |
| Code, config, SQL scripts, tests | None of the 71 `.py`, `.sql` and `.toml` files under `python/`, `tests/python/`, `sql/verification/` and `config/`, nor `.vscode/launch.json`, was modified after 02:36:58, before the readiness backup at 03:22:47 |
| Migrations V001 to V007 | SHA-256 unchanged |
| Database before the load (03:34:36, read-only) | Migrations 001 to 007; 20 orders; 305 movements (opening_balance 288, sale 17); 1,441 on hand, 0 reserved; reorder points 700 in total, 323 at warehouses; 1,426 change_log rows |
| **Replenishment: Check** (03:34:52, read-only) | Exit 0. Plan fingerprint `858bbfd09597030b573273c9885ffae0c0299e4ed146e8aca7c98f0e05531236`, validation 0 issues; pilot transactions complete (100 units of work verified); **17 planned, 0 present, 17 to write; conflicts 0, blocking 0**. Log: `logs/replenishment_pilot_plan_20261118_033452.log` |

## 3. Execution

```powershell
.venv\Scripts\python.exe python\run_replenishment.py --apply --yes
```

| Item | Value |
|---|---|
| Started / finished | 03:35:02, 0.7 s |
| Exit code | 0 |
| Log | `logs/replenishment_pilot_apply_20261118_033502.log` (57 lines: 0 ERROR, 0 WARNING, 0 SQLSTATE retries) |
| Under the advisory lock | Re-planned: 17 to write, conflicts 0, blocking 0, equal to the confirmed Check |
| Final loader line | `committed: 17 units of work in 17 transactions (revise 4, dispatch 4, arrive 4, receive 5), 17 verified, 111 change_log rows written` |

| # | Business time (UTC) | Unit of work | Document / location | Result |
|---|---|---|---|---|
| 1 | 2024-11-15 16:00:00 | R0 reorder points | FI-WH-01 | 17 reorder points 54 -> 93 |
| 2 | 2024-11-15 17:00:00 | R0 reorder points | DK-WH-01 | 17 reorder points 67 -> 115 |
| 3 | 2024-11-15 17:00:00 | R0 reorder points | NO-WH-01 | 17 reorder points 63 -> 102 |
| 4 | 2024-11-15 17:00:00 | R0 reorder points | SE-WH-01 | 18 reorder points 117 -> 201 |
| 5 | 2024-11-18 04:47:06 | T1 dispatch | TR-FI-2024-000001-FI-STO-02 | 1 unit, 1 line, FI-WH-01 -> FI-STO-02 |
| 6 | 2024-11-18 05:13:57 | T1 dispatch | TR-NO-2024-000001-NO-STO-03 | 2 units, 2 lines, NO-WH-01 -> NO-STO-03 |
| 7 | 2024-11-18 05:56:10 | T1 dispatch | TR-SE-2024-000001-SE-STO-03 | 3 units, 2 lines, SE-WH-01 -> SE-STO-03 |
| 8 | 2024-11-18 06:06:16 | T2 arrival | TR-FI-2024-000001-FI-STO-02 | 1 unit at FI-STO-02 |
| 9 | 2024-11-18 06:43:53 | T2 arrival | TR-NO-2024-000001-NO-STO-03 | 2 units at NO-STO-03 |
| 10 | 2024-11-18 06:58:54 | T1 dispatch | TR-DK-2024-000001-DK-STO-01 | 4 units, 3 lines, DK-WH-01 -> DK-STO-01 |
| 11 | 2024-11-18 07:29:54 | T2 arrival | TR-SE-2024-000001-SE-STO-03 | 3 units at SE-STO-03 |
| 12 | 2024-11-18 08:27:08 | T2 arrival | TR-DK-2024-000001-DK-STO-01 | 4 units at DK-STO-01 |
| 13 | 2024-11-28 08:20:00 | R1 receipt | PO-SE-2024-000001/DN1 | 2 units at SE-WH-01 (forced_reorder, split 1 of 2) |
| 14 | 2024-11-29 07:11:01 | R1 receipt | PO-DK-2024-000001/DN1 | 6 units at DK-WH-01 (forced_reorder) |
| 15 | 2024-11-29 10:11:51 | R1 receipt | PO-FI-2024-000001/DN1 | 10 units at FI-WH-01 (forced_reorder) |
| 16 | 2024-12-02 06:49:39 | R1 receipt | PO-NO-2024-000001/DN1 | 6 units at NO-WH-01 (forced_reorder) |
| 17 | 2024-12-02 12:46:04 | R1 receipt | PO-SE-2024-000001/DN2 | 2 units at SE-WH-01 (forced_reorder, split 2 of 2) |

Transfers are natural replenishment: each store listing was at or below its reorder point. The 4 purchase orders are the pilot's deliberately **forced** orders (decision D1), recorded with the PO number in each receipt's `external_reference`.

## 4. Database counts before and after

| Measure | Before (03:34:36) | After (03:35:58) | Change |
|---|---|---|---|
| Migrations | 7 (001 to 007) | 7 (001 to 007) | none |
| Orders (by status) | 20 (fulfilled 14, payment_failed 2, cancelled 4) | 20 (same) | none |
| Inventory movements | 305 | 326 | +21 (5 receipts, 8 transfer_out, 8 transfer_in) |
| Units on hand / reserved | 1,441 / 0 | 1,467 / 0 | +26 / 0 |
| Units in transit | 0 | 0 | none |
| Reorder points, all / warehouses | 700 / 323 | 910 / 533 | +210 (warehouses only) |
| change_log rows | 1,426 | 1,537 | +111 |

Exact row counts of all 19 tables: only `inventory_movements` (305 -> 326) and `change_log` (1,426 -> 1,537) changed. The other 17 tables are unchanged, including `inventory_balances` (320 rows; the load updates quantities and reorder points, it adds no listings).

change_log by table after the load:
- `inventory_balances` 761 (+90 = 69 reorder points + 21 balance updates from movements);
- `inventory_movements` 326 (+21);
- orders 72, order_items 26, payment_attempts 56 and inventory_reservations 46 (all unchanged);
- master data tables 250 (unchanged).

## 5. SQL verification (read-only session, 03:35:14)

| Script | Rows | PASS | INFO | FAIL | `failed_checks` | Raw output |
|---|---|---|---|---|---|---|
| 11 master data (revision 4) | 43 | 33 | 10 | **0** | 0 | `logs/sql_verification_11_verify_master_data_20261118_033514.csv` |
| 12 opening inventory (revision 3) | 24 | 17 | 7 | **0** | 0 | `logs/sql_verification_12_verify_opening_inventory_20261118_033514.csv` |
| 13 pilot transactions (revision 2) | 50 | 44 | 6 | **0** | 0 | `logs/sql_verification_13_verify_pilot_transactions_20261118_033514.csv` |
| 14 replenishment (new) | 43 | 36 | 7 | **0** | 0 | `logs/sql_verification_14_verify_replenishment_20261118_033514.csv` |
| **Total** | **160** | **130** | **30** | **0** | | |

Script 14, every PASS row (expected = actual):

| Group | Values |
|---|---|
| Counts | 101 receipt lines 5; 102 units received 26; 103 delivery notes 5; 104 purchase orders 4; 105 split orders 1; 106 transfer documents 4; 107 transfer_out lines 8; 108 transfer_in lines 8; 109 units dispatched 10; 110 units in transit 0; 111 warehouse reorder points 533 |
| Placement | 201 to 208 all 0: receipts only at warehouses; transfers warehouse -> store of the same market, at the store named in the reference, with the same reference; products launched; locations open |
| Documents and timing | 301 to 307 all 0: reference formats; dispatches Monday to Friday 06:00 to 08:00 local; receipts Monday to Friday 07:00 to 15:00 local; arrivals the same morning before 10:00; nothing dated inside the pilot, after the window or in the future; transfer pairs consistent |
| Policy | 401 to 403 all 0: every store was at or below its reorder point at dispatch; no warehouse left below its reorder point; warehouse reorder points below the opening balance |
| Ledger | 501 to 504 and 507 all 0 (ledger identity, reserved, no negative stock now or at any business time, capacity); 505 conservation 1,467 = 1,467; 506 network units 1,467 = 1,467 |

Script 14 INFO:
- 601 receipts: DK 1 / 6, FI 1 / 10, NO 1 / 6, SE 2 / 4;
- 602 transfers: DK 1 / 3 / 4, FI 1 / 1 / 1, NO 1 / 2 / 2, SE 1 / 2 / 3;
- 604 store listings at or below their reorder point now: **0**;
- 605 warehouse reorder points: DK 121, FI 99, NO 108, SE 205;
- 606 first / last replenishment movement: 2024-11-18 04:47:06 / 2024-12-02 12:46:04 UTC;
- 607 change_log rows for these movements: 21.

Script 13 revision 2: 504 = 0 (only opening balances, sales and replenishment after the pilot); 505 = 1,467 expected and actual (1,460 opening - 19 sold + 26 received).

Script 11: 204 = 0 (no adjustments, write-offs or return restocks); INFO 208 `5 receipts, 8 transfers out, 8 transfers in`.

Script 12: 201 = 0; 301 to 303 ledger = 0; 501 = 0 (every opening balance still above the current, revised reorder point); 503 capacity = 0; INFO 204 `5 receipts, 8 transfers out, 8 transfers in`.

## 6. Inventory reconciliation

Network identity:

| On hand | In transit | = | Opening | + Received | + Sold (signed) | Transfers (net) |
|---|---|---|---|---|---|---|
| 1,467 | 0 | = | 1,460 | + 26 | - 19 | 0 (-10 out, +10 in) |

Units on hand by market and location type. "Before" is script 12, check 601 on 2026-11-18 at 01:33, after the pilot; nothing changed between then and this load. "After" is script 12, check 601 at 03:35.

| Market | Location type | Before | After | Change | Explained by |
|---|---|---|---|---|---|
| DK | store | 135 | 139 | +4 | transfer in 4 (DK-STO-01) |
| DK | warehouse | 191 | 193 | +2 | transfer out 4, receipt 6 |
| FI | store | 100 | 101 | +1 | transfer in 1 (FI-STO-02) |
| FI | warehouse | 152 | 161 | +9 | transfer out 1, receipt 10 |
| NO | store | 139 | 141 | +2 | transfer in 2 (NO-STO-03) |
| NO | warehouse | 178 | 182 | +4 | transfer out 2, receipt 6 |
| SE | store | 196 | 199 | +3 | transfer in 3 (SE-STO-03) |
| SE | warehouse | 350 | 351 | +1 | transfer out 3, receipt 2 + 2 |
| **Total** | | **1,441** | **1,467** | **+26** | receipts 26; transfers net 0 |

Every listing touched by the load. "Before" = after minus the replenishment movements; reserved is 0 everywhere.

| Location | SKU | Before | Change | After | Reorder point | Movements |
|---|---|---|---|---|---|---|
| DK-STO-01 | ACC-000010 | 3 | +1 | 4 | 3 | transfer_in 1 |
| DK-STO-01 | ACC-000013 | 2 | +2 | 4 | 3 | transfer_in 2 |
| DK-STO-01 | SPH-000019 | 2 | +1 | 3 | 2 | transfer_in 1 |
| DK-WH-01 | ACC-000010 | 14 | -1 | 13 | 7 | transfer_out -1 |
| DK-WH-01 | ACC-000013 | 17 | -2 | 15 | 9 | transfer_out -2 |
| DK-WH-01 | GAM-000008 | 6 | +6 | 12 | 4 | purchase_receipt 6 |
| DK-WH-01 | SPH-000019 | 11 | -1 | 10 | 7 | transfer_out -1 |
| FI-STO-02 | WEA-000020 | 1 | +1 | 2 | 1 | transfer_in 1 |
| FI-WH-01 | WEA-000020 | 6 | +9 | 15 | 4 | transfer_out -1, purchase_receipt 10 |
| NO-STO-03 | GAM-000008 | 1 | +1 | 2 | 1 | transfer_in 1 |
| NO-STO-03 | SPH-000019 | 1 | +1 | 2 | 1 | transfer_in 1 |
| NO-WH-01 | GAM-000008 | 7 | +5 | 12 | 4 | transfer_out -1, purchase_receipt 6 |
| NO-WH-01 | SPH-000019 | 13 | -1 | 12 | 7 | transfer_out -1 |
| SE-STO-03 | SMH-000009 | 2 | +1 | 3 | 2 | transfer_in 1 |
| SE-STO-03 | SPH-000014 | 0 | +2 | 2 | 1 | transfer_in 2 |
| SE-WH-01 | SMH-000009 | 12 | -1 | 11 | 8 | transfer_out -1 |
| SE-WH-01 | SPH-000014 | 19 | -2 | 17 | 12 | transfer_out -2 |
| SE-WH-01 | TAB-000003 | 4 | +4 | 8 | 3 | purchase_receipt 2, purchase_receipt 2 |

- All 8 store listings that were at or below their reorder point after the pilot are refilled above it. This includes SE-STO-03 SPH-000014, sold out since the pilot (0 -> 2). Script 14, check 604 now counts 0 store listings at or below their reorder point.
- Every warehouse stayed above its revised reorder point after its dispatches (script 14, check 402 = 0).
- Warehouse capacity used: DK 193 / 60,000, FI 161 / 50,000, NO 182 / 60,000, SE 351 / 120,000.

## 7. Transaction-pilot regression

| Check | Result |
|---|---|
| **Transactions: Check** (`run_transactions.py --plan`, read-only, 03:35:23) | Exit 0. Plan fingerprint `fabcd571a3b9f66827f20e54d0ebc421be5f68937af558383ee4b84f7c466264`, validation 0 issues. **Orders 20 planned, 20 present, 0 to write; units of work 100 / 100 / 0**; "later replenishment (after the plan, counted in the stock check): 21 movements, net +26 units"; conflicts 0, blocking 0. Log: `logs/transactions_pilot_plan_20261118_033523.log` |
| Script 13 (pilot transactions) | 44 PASS, 6 INFO, 0 FAIL. Order, payment and reservation checks unchanged; revenue, cost and gross profit per market unchanged (DK 2996.00 / 2215.45 / 780.55 DKK, FI 153.62 / 89.42 / 64.20 EUR, NO 5908.80 / 4528.37 / 1380.43 NOK, SE 19323.20 / 16042.04 / 3281.16 SEK) |
| Order tables | orders 20, order_items 26, payment_attempts 21, inventory_reservations 23 rows; their change_log rows unchanged (72, 26, 56, 46) |

## 8. Replenishment: Check after the load

`run_replenishment.py --plan` (read-only, 03:35:24): exit 0, fingerprint `858bbfd0…`, validation 0 issues. **Units of work 17 planned, 17 present, 0 to write**; "to run by kind: nothing"; conflicts 0, blocking 0. Log: `logs/replenishment_pilot_plan_20261118_033524.log`. A repeated Load would write nothing.

## 9. Errors, warnings and deviations

- **Errors: none.** **Warnings: none.** The load log has 0 ERROR, 0 WARNING and 0 retry lines. Every command exited 0 and every psql run exited 0.
- **Deviation: `--yes` instead of typing the database name.** The agent's shell has no keyboard input, so the typed confirmation cannot be answered (it would cancel with exit 4). The owner's written authorization covered the load. Every other safeguard stayed active:
  - the re-check under the advisory lock, equal to the confirmed Check;
  - refusal on conflicts;
  - the comparison of each step's result with the plan;
  - the final verification of all 17 units of work.
- No manual SQL writes. All other sessions on the real database (counts, scripts, checks) were forced read-only.

## 10. Backups and files

| Item | State after the load |
|---|---|
| `database/backups/nordiccart_oltp_20261118_032247_pre_replenishment.dump` | **Unchanged**: 257,336 bytes, last written 03:22:47, SHA-256 `A4907BB9E9BDEBD9A5884660178F2FE2BCE69494A15677004BA43314122A8078` |
| `database/backups/nordiccart_oltp_20261118_012230_pre_pilot_transactions.dump` | **Unchanged**: 241,404 bytes, SHA-256 `86EAEA1E4860C47792D406060C02F4DDE80032399F073F76BCA58B5572E7D7CB` |
| Migrations V001 to V007 | Unchanged (SHA-256) |
| Python code, configuration, SQL scripts, tests | Unchanged (no file modified after 03:22:47) |
| New files | Logs (`replenishment_pilot_plan_20261118_033452.log`, `replenishment_pilot_apply_20261118_033502.log`, `transactions_pilot_plan_20261118_033523.log`, `replenishment_pilot_plan_20261118_033524.log`, four `sql_verification_*_20261118_033514.csv`); this report and its copy at the project root. The CSV previews in `output/replenishment/pilot/` and `output/transactions/pilot/` were rewritten by the checks with the same plan (same fingerprints). |

Rollback position: the stock ledger is insert-only (V007). The only way back to the state before this load is restoring `nordiccart_oltp_20261118_032247_pre_replenishment.dump`, which needs the owner's decision.

Documentation note: `docs/phase2_database_implementation_status.md` still describes step 4 as "awaiting approval to load". It was not edited in this run, which was limited to the load, verification and this report.

## 11. Stop point

The replenishment pilot is loaded and verified. No further work was started. The next development phase waits for the owner's instruction.
