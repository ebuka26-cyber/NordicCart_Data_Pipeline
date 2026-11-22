# Pilot transaction load: execution report

| Item | Value |
|---|---|
| Date | 2026-11-18. Run times are local time (UTC+1); business event times are UTC and marked as such. |
| Phase | Phase 3, step 3: customer transaction simulation (pilot) |
| Database | `nordiccart_oltp` on localhost:5432, PostgreSQL 18.4, migrations 001 to 007 |
| Tool | `python/run_transactions.py` (package `python/nordiccart/transactions`), Python 3.14.7, psycopg 3.3.9, libpq 18.4 |
| Settings | `config/generator.toml`, profile `pilot`, seed 20261117, orders placed 2024-11-01 to 2024-11-14 |
| Fingerprints (SHA-256) | transaction plan `fabcd571a3b9f66827f20e54d0ebc421be5f68937af558383ee4b84f7c466264`; master data `c46181a25bd6fdc1…`; opening inventory `5c7a4eba5057caab…` |
| Authorization | Written authorization from the database owner on 2026-11-18 to load the 20 pilot transactions, after a verified backup |
| Executed by | Claude Code (AI coding agent) in the project folder, on the owner's instruction |
| **Result** | **SUCCESS: 20 of 20 orders processed, 100 of 100 units of work executed, 0 errors, 0 warnings, 0 retries. Stock on hand 1,460 → 1,441 units.** |

## 1. Pre-flight checks (before any write)

| Check | Result |
|---|---|
| Backup | `database/backups/nordiccart_oltp_20261118_012230_pre_pilot_transactions.dump` present, 241,404 bytes, SHA-256 `86EAEA1E4860C47792D406060C02F4DDE80032399F073F76BCA58B5572E7D7CB` (same as when it was created and verified at 01:22). It holds the pre-load state: 0 orders, 320 listings, 288 opening balances, 1,146 change_log rows. |
| Verification scripts | `sql/verification/13_verify_pilot_transactions.sql` (new in step 3), `11_verify_master_data.sql` (revision 3), `12_verify_opening_inventory.sql` (revision 2) present. All three are single read-only SELECT statements. |
| Migration files | SHA-256 of V001 to V007 recorded as a baseline (compared again after the load, section 7). |
| Read-only Check (`--plan`), 01:32:44 | Plan fingerprint `fabcd571…`, validation 0 issues; "master data and opening inventory present and unchanged"; orders 20 planned, 0 present, 20 to write; units of work 100 planned, 0 present, 100 to write; conflicts 0, blocking 0. Log: `logs/transactions_pilot_plan_20261118_013244.log`. |

## 2. Execution

```powershell
.venv\Scripts\python.exe python\run_transactions.py --apply --yes
```

| Item | Value |
|---|---|
| Started / finished | 01:32:50 / 01:32:51 (0.9 s) |
| Exit code | 0 |
| Log | `logs/transactions_pilot_apply_20261118_013250.log` (149 lines: 0 ERROR, 0 WARNING) |
| Final loader line | `committed: 100 units of work in 100 transactions (place 20, reserve 20, payment_start 21, payment_result 21, fulfil 14, customer_cancel 1, expire 1, cancel_out_of_stock 2), 20 orders placed, 20 orders verified, 280 change_log rows written` |

How the loader ran (unchanged code, standard `--apply` workflow):

1. It took the session advisory lock, read the database again and rebuilt the plan under the lock: 0 conflicts. The pending work matched the Check shown before (20 orders, 100 units of work).
2. It ran each unit of work in its own transaction, in business-time order across all orders, through the V006/V007 functions and guarded status updates. Stock quantities were never written directly.
3. It compared each result with the plan. Every result matched, so no step was stopped or rolled back unexpectedly.
4. After the last step it verified every order, payment attempt, reservation, sale movement and stock balance against the plan, plus the ledger identity for all 320 balances. All 20 orders were verified.

### Units of work

| Unit of work | Design step | Planned | Executed |
|---|---|---|---|
| place | U1: insert the order (placed) and its lines | 20 | 20 |
| reserve | U2: `fn_reserve_order_stock` | 20 | 20 (18 reserved; 2 returned `out_of_stock` and were rolled back, as the design requires) |
| cancel_out_of_stock | U2 follow-up in a new transaction: order cancelled (`out_of_stock`) | 2 | 2 |
| payment_start | U3a: pending payment attempt | 21 | 21 |
| payment_result | U3c: captured (order confirmed) or failed; last failure releases the stock | 21 | 21 |
| customer_cancel | `fn_close_order_reservations` (released), order cancelled (`customer_request`) | 1 | 1 |
| expire | U4 sweeper: `fn_close_order_reservations` (expired), order cancelled (`reservation_expired`) | 1 | 1 |
| fulfil | U5: `fn_fulfil_order_stock` (reservations consumed, sale movements) | 14 | 14 |
| **Total** | | **100** | **100** |

The two rolled-back U2 transactions are part of the plan: an `out_of_stock` result writes nothing, and a separate transaction cancels the order. Every other unit of work committed. The full sequence is in Appendix A.

### Scenario outcomes

| Scenario | Orders | Expected outcome | Actual |
|---|---|---|---|
| fulfilled | 12 | reserved, paid on attempt 1, confirmed, fulfilled | 12 fulfilled |
| payment_retry_fulfilled | 2 | attempt 1 fails, attempt 2 captured, fulfilled | 2 fulfilled (SE-2024-00000007, DK-2024-00000014) |
| payment_failed | 2 | all attempts fail (3 online, 2 in store); stock released | 2 payment_failed (SE-2024-00000010: 3 attempts; NO-2024-00000003: 2 attempts) |
| customer_cancelled | 1 | reserved, cancelled before paying; stock released | SE-2024-00000004 cancelled (customer_request), 2 reservations released |
| reservation_expired | 1 | reserved, never paid; U4 expires the reservation | NO-2024-00000009 cancelled (reservation_expired), 2 reservations expired |
| out_of_stock_quantity | 1 | asks for 5 × TAB-000003 when DK-WH-01 holds 4; nothing reserved | DK-2024-00000020 cancelled (out_of_stock), 0 reservations, 0 payments |
| out_of_stock_reserved | 1 | the last SPH-000014 at SE-STO-03 is held by SE-2024-00000011 | SE-2024-00000012 cancelled (out_of_stock), 0 reservations, 0 payments |

## 3. Orders processed

Read from the database after the load. "Scenario" comes from the plan (`output/transactions/pilot/orders.csv`). "Matches plan" compares the database status, cancellation reason, order total, channel and fulfilment location with the plan.

| # | Order | Scenario (plan) | Channel | Location | Final status | Cancellation reason | Order total | Lines / units | Payment attempts | Reservations | Units sold | Matches plan |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | NO-2024-00000001 | fulfilled | in_store | NO-STO-03 | fulfilled | - | 3999.00 NOK | 1 / 1 | 1 mobile_payment captured | consumed 1 | 1 | yes |
| 2 | FI-2024-00000002 | fulfilled | online | FI-WH-01 | fulfilled | - | 83.80 EUR | 1 / 2 | 1 mobile_payment captured | consumed 1 | 2 | yes |
| 3 | NO-2024-00000003 | payment_failed | in_store | NO-STO-01 | payment_failed | - | 339.00 NOK | 1 / 1 | 1 card failed (insufficient_funds); 2 card failed (card_declined) | released 1 | 0 | yes |
| 4 | SE-2024-00000004 | customer_cancelled | online | SE-WH-01 | cancelled | customer_request | 758.00 SEK | 2 / 2 | none | released 2 | 0 | yes |
| 5 | SE-2024-00000005 | fulfilled | online | SE-WH-01 | fulfilled | - | 9990.00 SEK | 1 / 1 | 1 card captured | consumed 1 | 1 | yes |
| 6 | FI-2024-00000006 | fulfilled | in_store | FI-STO-02 | fulfilled | - | 109.00 EUR | 1 / 1 | 1 card captured | consumed 1 | 1 | yes |
| 7 | SE-2024-00000007 | payment_retry_fulfilled | in_store | SE-STO-03 | fulfilled | - | 2918.00 SEK | 2 / 2 | 1 card failed (insufficient_funds); 2 card captured | consumed 2 | 2 | yes |
| 8 | DK-2024-00000008 | fulfilled | in_store | DK-STO-01 | fulfilled | - | 837.00 DKK | 2 / 3 | 1 card captured | consumed 2 | 3 | yes |
| 9 | NO-2024-00000009 | reservation_expired | online | NO-WH-01 | cancelled | reservation_expired | 658.00 NOK | 2 / 2 | none | expired 2 | 0 | yes |
| 10 | SE-2024-00000010 | payment_failed | online | SE-WH-01 | payment_failed | - | 1199.00 SEK | 1 / 1 | 1 card failed (card_declined); 2 card failed (insufficient_funds); 3 card failed (insufficient_funds) | released 1 | 0 | yes |
| 11 | SE-2024-00000011 | fulfilled | in_store | SE-STO-03 | fulfilled | - | 1999.00 SEK | 1 / 1 | 1 card captured | consumed 1 | 1 | yes |
| 12 | SE-2024-00000012 | out_of_stock_reserved | in_store | SE-STO-03 | cancelled | out_of_stock | 1999.00 SEK | 1 / 1 | none | none | 0 | yes |
| 13 | DK-2024-00000013 | fulfilled | online | DK-WH-01 | fulfilled | - | 409.00 DKK | 1 / 1 | 1 card captured | consumed 1 | 1 | yes |
| 14 | DK-2024-00000014 | payment_retry_fulfilled | in_store | DK-STO-01 | fulfilled | - | 2499.00 DKK | 1 / 1 | 1 card failed (insufficient_funds); 2 card captured | consumed 1 | 1 | yes |
| 15 | SE-2024-00000015 | fulfilled | online | SE-WH-01 | fulfilled | - | 8299.00 SEK | 1 / 1 | 1 card captured | consumed 1 | 1 | yes |
| 16 | NO-2024-00000016 | fulfilled | online | NO-WH-01 | fulfilled | - | 1188.00 NOK | 2 / 2 | 1 buy_now_pay_later captured | consumed 2 | 2 | yes |
| 17 | SE-2024-00000017 | fulfilled | in_store | SE-STO-01 | fulfilled | - | 659.00 SEK | 1 / 1 | 1 cash captured | consumed 1 | 1 | yes |
| 18 | NO-2024-00000018 | fulfilled | in_store | NO-STO-03 | fulfilled | - | 2199.00 NOK | 1 / 1 | 1 card captured | consumed 1 | 1 | yes |
| 19 | SE-2024-00000019 | fulfilled | online | SE-WH-01 | fulfilled | - | 338.00 SEK | 1 / 1 | 1 mobile_payment captured | consumed 1 | 1 | yes |
| 20 | DK-2024-00000020 | out_of_stock_quantity | online | DK-WH-01 | cancelled | out_of_stock | 29324.00 DKK | 2 / 6 | none | none | 0 | yes |

**20 of 20 orders match the plan; 0 mismatches.**

## 4. Final database counts

"Before" is the state at 01:23 (backup verification), confirmed unchanged by the Check at 01:32:44. "After" is exact `count(*)` per table at 01:34, in a read-only session.

| Table | Before load | After load | Change |
|---|---|---|---|
| `meta.schema_migrations` | 7 | 7 | 0 |
| `public.change_log` | 1146 | 1426 | +280 |
| `public.change_log_checkpoints` | 0 | 0 | 0 |
| `public.countries` | 4 | 4 | 0 |
| `public.currencies` | 4 | 4 | 0 |
| `public.customers` | 100 | 100 | 0 |
| `public.exchange_rates` | 0 | 0 | 0 |
| `public.inventory_balances` | 320 | 320 | 0 |
| `public.inventory_movements` | 288 | 305 | +17 |
| `public.inventory_reservations` | 0 | 23 | +23 |
| `public.order_items` | 0 | 26 | +26 |
| `public.orders` | 0 | 20 | +20 |
| `public.payment_attempts` | 0 | 21 | +21 |
| `public.product_categories` | 10 | 10 | 0 |
| `public.product_prices` | 80 | 80 | 0 |
| `public.products` | 20 | 20 | 0 |
| `public.stock_locations` | 16 | 16 | 0 |
| `public.stores` | 12 | 12 | 0 |
| `public.warehouses` | 4 | 4 | 0 |

The 280 new change_log rows match the loader's count ("280 change_log rows written"). Master data tables, `exchange_rates` and `change_log_checkpoints` are unchanged.

### State summary after the load

| Metric | Value |
|---|---|
| orders by status | cancelled 4, fulfilled 14, payment_failed 2 |
| cancelled orders by reason | customer_request 1, out_of_stock 2, reservation_expired 1 |
| orders by channel | in_store 10, online 10 |
| payment attempts by status | captured 14, failed 7 |
| inventory reservations by status | consumed 17, expired 2, released 4 |
| inventory movements by type (rows / units) | opening_balance 288 rows / 1460 units, sale 17 rows / -19 units |
| units on hand (sum of inventory_balances.quantity_on_hand) | 1441 |
| units reserved (sum of inventory_balances.quantity_reserved) | 0 |
| opening units + sale movements = ledger total | 1460 + (-19) = 1441 |
| listings with 0 on hand | 33 |
| SPH-000014 at SE-STO-03 (on hand / reserved) | 0 / 0 |
| TAB-000003 at DK-WH-01 (on hand / reserved) | 4 / 0 |
| change_log rows (total) | 1426 |
| meta.schema_migrations rows | 7 |

## 5. Inventory

**Units on hand: 1,441** (expected 1,441 = 1,460 opening units − 19 units sold). Units reserved: 0. Ledger: 1,460 opening + (−19) sales = 1,441, equal to the sum of `inventory_balances.quantity_on_hand`.

| Market | Location type | Opening units | After load | Units sold | Sold by |
|---|---|---|---|---|---|
| DK | store | 139 | 135 | 4 | DK-2024-00000008 (3), DK-2024-00000014 (1) |
| DK | warehouse | 192 | 191 | 1 | DK-2024-00000013 |
| FI | store | 101 | 100 | 1 | FI-2024-00000006 |
| FI | warehouse | 154 | 152 | 2 | FI-2024-00000002 |
| NO | store | 141 | 139 | 2 | NO-2024-00000001, NO-2024-00000018 |
| NO | warehouse | 180 | 178 | 2 | NO-2024-00000016 |
| SE | store | 200 | 196 | 4 | SE-2024-00000007 (2), SE-2024-00000011, SE-2024-00000017 |
| SE | warehouse | 353 | 350 | 3 | SE-2024-00000005, SE-2024-00000015, SE-2024-00000019 |
| **Total** | | **1,460** | **1,441** | **19** | 14 fulfilled orders |

Opening units come from the opening inventory load log (2026-11-17 18:16). "After load" comes from script 12, check 601.

- **SPH-000014 at SE-STO-03:** 0 on hand, 0 reserved. SE-2024-00000011 bought the last unit, so SE-2024-00000012 got `out_of_stock`. It stays at 0 until restocking is simulated.
- **TAB-000003 at DK-WH-01:** 4 on hand, 0 reserved, unchanged. DK-2024-00000020 asked for 5, and V007's all-or-nothing rule reserved neither of its two lines.
- **Listings at 0 on hand:** 33 = 32 listings of the two products not launched yet (CAM-000007, SMH-000011; 16 locations each) + SPH-000014 at SE-STO-03.

## 6. Post-load verification

| Check | Result |
|---|---|
| Loader's own verification (inside `--apply`) | 20 orders verified against the plan; ledger identity holds for all balances |
| SQL script 13 (pilot transactions) | 50 rows: 44 PASS, 6 INFO, 0 FAIL, `failed_checks` = 0 (expected 44 / 6 / 0) |
| SQL script 11 (master data, revision 3) | 42 rows: 33 PASS, 9 INFO, 0 FAIL, `failed_checks` = 0 (expected 33 / 9 / 0) |
| SQL script 12 (opening inventory, revision 2) | 23 rows: 17 PASS, 6 INFO, 0 FAIL, `failed_checks` = 0 (expected 17 / 6 / 0) |
| Orders compared with the plan CSV | 20 of 20 match (status, reason, total, channel, location) |
| Read-only Check (`--plan`) again, 01:34:37 | orders 20 planned, 20 present, 0 to write; units of work 100 planned, 100 present, 0 to write; conflicts 0, blocking 0; "master data and opening inventory present and unchanged". A repeated Load would write nothing. Log: `logs/transactions_pilot_plan_20261118_013437.log` |

`--apply` was not run a second time. The read-only Check proves the same thing without another run in write mode.

Script results in full: `2026-11-18_transactions_pilot_sql_verification_report.md`.

## 7. Changes to files and rules

| Item | Result |
|---|---|
| Migrations V001 to V007 | Not modified. SHA-256 identical before and after the load (first 16 hex digits: V001 `D3349651A5CAF2CF`, V002 `14BBCD1F4EC19447`, V003 `ED76162656C04E92`, V004 `88D0A52D422405BB`, V005 `02320CF0FB4507AF`, V006 `A8225D72CF6B4151`, V007 `DCD79FF76BABDF36`). |
| Business rules, Python code, config, SQL scripts | Not modified. No project file outside `logs/`, `output/` and the new backup changed after 01:22 (checked at 01:34, before these reports were written). |
| Database | Changed only by the loader's 100 units of work. No manual SQL writes; all other database sessions in this work were read-only. |
| Generated by the runs | `output/transactions/pilot/` (CSV preview and `summary.json`). Rewritten by every run with identical content; only `written_at_utc` changes. |

New files:

| File | Content |
|---|---|
| `logs/transactions_pilot_plan_20261118_013244.log` | Check before the load |
| `logs/transactions_pilot_apply_20261118_013250.log` | The load |
| `logs/transactions_pilot_plan_20261118_013437.log` | Check after the load |
| `logs/sql_verification_13_verify_pilot_transactions_20261118_013322.csv` | Raw output of script 13 |
| `logs/sql_verification_11_verify_master_data_20261118_013322.csv` | Raw output of script 11 |
| `logs/sql_verification_12_verify_opening_inventory_20261118_013322.csv` | Raw output of script 12 |
| `docs/test_reports/2026-11-18_transactions_pilot_execution_report.md` | This report (copy at the project root) |
| `docs/test_reports/2026-11-18_transactions_pilot_sql_verification_report.md` | SQL verification report (copy at the project root) |

Earlier the same day (backup step): `database/backups/nordiccart_oltp_20261118_012230_pre_pilot_transactions.dump` and `logs/pg_dump_nordiccart_oltp_20261118_012230.log`.

## 8. Errors and deviations

**Errors: none.** The load log has 0 ERROR and 0 WARNING lines and no SQLSTATE retries. The three log lines containing "retry" are the scenario name `payment_retry_fulfilled`. No unit of work gave an unplanned result.

**Deviation: `--yes` instead of typing the database name.** The agent's shell has no keyboard input. Without `--yes`, the command line refuses the load (exit code 4, nothing written). The owner gave written authorization beforehand. Every other safeguard stayed active: the re-check under the advisory lock, refusal on conflicts, the plan-changed check, checking each step's result, and the final verification. The command is the same as the VS Code "NordicCart - Transactions: Load" configuration plus `--yes`.

## 9. Rollback position

The pilot rows cannot be removed with SQL. V006 and V007 refuse DELETE and TRUNCATE on `orders`, `order_items`, `payment_attempts`, `inventory_reservations` and `inventory_movements`. The only way back to the pre-load state is the backup `database/backups/nordiccart_oltp_20261118_012230_pre_pilot_transactions.dump`: drop the database, then run `pg_restore --create`. That needs the owner's approval and has not been done.

## 10. Open items for review

1. `docs/phase2_database_implementation_status.md` still describes step 3 as "not yet run against the real database". It was not edited in this run, which allowed only necessary changes, and should be updated.
2. SPH-000014 is sold out at SE-STO-03 (Malmö). Restocking (purchase receipts, transfers) is not simulated yet.
3. Before scaling up, from the status document: decide the realistic scenario mix and order volume, restocking and daily `exchange_rates`. The next orders must continue at sequence 21, after 2024-11-15 08:44:22 UTC.
4. Script 13 is pinned to the pilot counts. Once further activity is loaded (receipts, later orders), it will report FAIL by design until its params block is updated.

## Appendix A. Units of work as executed

From `logs/transactions_pilot_apply_20261118_013250.log`, in run order. Each line is one transaction: business time (UTC), order, unit of work, result.

```text
  1  2024-11-01 12:08:41 UTC NO-2024-00000001 U1 place                   placed, 1 lines, total 3999.00 NOK
  2  2024-11-01 12:08:44 UTC NO-2024-00000001 U2 reserve                 reserved
  3  2024-11-01 12:09:17 UTC NO-2024-00000001 U3a payment attempt 1      pending (mobile_payment, 3999.00 NOK)
  4  2024-11-01 12:09:21 UTC NO-2024-00000001 U3c payment result 1       captured, order confirmed
  5  2024-11-01 12:11:19 UTC NO-2024-00000001 U5 fulfil                  fulfilled, 1 units issued from NO-STO-03
  6  2024-11-02 10:55:31 UTC FI-2024-00000002 U1 place                   placed, 1 lines, total 83.80 EUR
  7  2024-11-02 10:55:34 UTC FI-2024-00000002 U2 reserve                 reserved
  8  2024-11-02 10:59:02 UTC FI-2024-00000002 U3a payment attempt 1      pending (mobile_payment, 83.80 EUR)
  9  2024-11-02 10:59:06 UTC FI-2024-00000002 U3c payment result 1       captured, order confirmed
 10  2024-11-02 12:40:46 UTC FI-2024-00000002 U5 fulfil                  fulfilled, 2 units issued from FI-WH-01
 11  2024-11-02 14:20:07 UTC NO-2024-00000003 U1 place                   placed, 1 lines, total 339.00 NOK
 12  2024-11-02 14:20:12 UTC NO-2024-00000003 U2 reserve                 reserved
 13  2024-11-02 14:20:54 UTC NO-2024-00000003 U3a payment attempt 1      pending (card, 339.00 NOK)
 14  2024-11-02 14:21:00 UTC NO-2024-00000003 U3c payment result 1       failed (insufficient_funds)
 15  2024-11-02 14:21:26 UTC NO-2024-00000003 U3a payment attempt 2      pending (card, 339.00 NOK)
 16  2024-11-02 14:21:35 UTC NO-2024-00000003 U3c payment result 2       failed (card_declined), last attempt: order payment_failed, 1 reservations released
 17  2024-11-03 15:32:15 UTC SE-2024-00000004 U1 place                   placed, 2 lines, total 758.00 SEK
 18  2024-11-03 15:32:20 UTC SE-2024-00000004 U2 reserve                 reserved
 19  2024-11-03 15:36:33 UTC SE-2024-00000004 cancel (customer_request)  cancelled (customer_request), 2 reservations released
 20  2024-11-03 19:09:22 UTC SE-2024-00000005 U1 place                   placed, 1 lines, total 9990.00 SEK
 21  2024-11-03 19:09:25 UTC SE-2024-00000005 U2 reserve                 reserved
 22  2024-11-03 19:10:07 UTC SE-2024-00000005 U3a payment attempt 1      pending (card, 9990.00 SEK)
 23  2024-11-03 19:10:11 UTC SE-2024-00000005 U3c payment result 1       captured, order confirmed
 24  2024-11-04 10:13:49 UTC SE-2024-00000005 U5 fulfil                  fulfilled, 1 units issued from SE-WH-01
 25  2024-11-04 12:14:33 UTC FI-2024-00000006 U1 place                   placed, 1 lines, total 109.00 EUR
 26  2024-11-04 12:14:40 UTC FI-2024-00000006 U2 reserve                 reserved
 27  2024-11-04 12:15:13 UTC FI-2024-00000006 U3a payment attempt 1      pending (card, 109.00 EUR)
 28  2024-11-04 12:15:18 UTC FI-2024-00000006 U3c payment result 1       captured, order confirmed
 29  2024-11-04 12:16:07 UTC FI-2024-00000006 U5 fulfil                  fulfilled, 1 units issued from FI-STO-02
 30  2024-11-05 13:58:51 UTC SE-2024-00000007 U1 place                   placed, 2 lines, total 2918.00 SEK
 31  2024-11-05 13:58:54 UTC SE-2024-00000007 U2 reserve                 reserved
 32  2024-11-05 13:59:11 UTC SE-2024-00000007 U3a payment attempt 1      pending (card, 2918.00 SEK)
 33  2024-11-05 13:59:19 UTC SE-2024-00000007 U3c payment result 1       failed (insufficient_funds)
 34  2024-11-05 14:00:32 UTC SE-2024-00000007 U3a payment attempt 2      pending (card, 2918.00 SEK)
 35  2024-11-05 14:00:39 UTC SE-2024-00000007 U3c payment result 2       captured, order confirmed
 36  2024-11-05 14:01:26 UTC SE-2024-00000007 U5 fulfil                  fulfilled, 2 units issued from SE-STO-03
 37  2024-11-06 16:57:43 UTC DK-2024-00000008 U1 place                   placed, 2 lines, total 837.00 DKK
 38  2024-11-06 16:57:50 UTC DK-2024-00000008 U2 reserve                 reserved
 39  2024-11-06 16:58:20 UTC DK-2024-00000008 U3a payment attempt 1      pending (card, 837.00 DKK)
 40  2024-11-06 16:58:25 UTC DK-2024-00000008 U3c payment result 1       captured, order confirmed
 41  2024-11-06 17:00:08 UTC DK-2024-00000008 U5 fulfil                  fulfilled, 3 units issued from DK-STO-01
 42  2024-11-07 15:03:29 UTC NO-2024-00000009 U1 place                   placed, 2 lines, total 658.00 NOK
 43  2024-11-07 15:03:36 UTC NO-2024-00000009 U2 reserve                 reserved
 44  2024-11-07 15:19:05 UTC NO-2024-00000009 U4 expire                  cancelled (reservation_expired), 2 reservations expired, 0 authorizations voided
 45  2024-11-07 18:31:48 UTC SE-2024-00000010 U1 place                   placed, 1 lines, total 1199.00 SEK
 46  2024-11-07 18:31:50 UTC SE-2024-00000010 U2 reserve                 reserved
 47  2024-11-07 18:33:41 UTC SE-2024-00000010 U3a payment attempt 1      pending (card, 1199.00 SEK)
 48  2024-11-07 18:33:50 UTC SE-2024-00000010 U3c payment result 1       failed (card_declined)
 49  2024-11-07 18:34:19 UTC SE-2024-00000010 U3a payment attempt 2      pending (card, 1199.00 SEK)
 50  2024-11-07 18:34:29 UTC SE-2024-00000010 U3c payment result 2       failed (insufficient_funds)
 51  2024-11-07 18:35:29 UTC SE-2024-00000010 U3a payment attempt 3      pending (card, 1199.00 SEK)
 52  2024-11-07 18:35:37 UTC SE-2024-00000010 U3c payment result 3       failed (insufficient_funds), last attempt: order payment_failed, 1 reservations released
 53  2024-11-09 14:34:47 UTC SE-2024-00000011 U1 place                   placed, 1 lines, total 1999.00 SEK
 54  2024-11-09 14:34:52 UTC SE-2024-00000011 U2 reserve                 reserved
 55  2024-11-09 14:35:16 UTC SE-2024-00000012 U1 place                   placed, 1 lines, total 1999.00 SEK
 56  2024-11-09 14:35:23 UTC SE-2024-00000012 U2 reserve                 rolled back (out_of_stock)
 57  2024-11-09 14:35:24 UTC SE-2024-00000012 U2 cancel (out_of_stock)   cancelled (out_of_stock)
 58  2024-11-09 14:35:37 UTC SE-2024-00000011 U3a payment attempt 1      pending (card, 1999.00 SEK)
 59  2024-11-09 14:35:43 UTC SE-2024-00000011 U3c payment result 1       captured, order confirmed
 60  2024-11-09 14:37:13 UTC SE-2024-00000011 U5 fulfil                  fulfilled, 1 units issued from SE-STO-03
 61  2024-11-10 16:55:15 UTC DK-2024-00000013 U1 place                   placed, 1 lines, total 409.00 DKK
 62  2024-11-10 16:55:22 UTC DK-2024-00000013 U2 reserve                 reserved
 63  2024-11-10 16:59:12 UTC DK-2024-00000013 U3a payment attempt 1      pending (card, 409.00 DKK)
 64  2024-11-10 16:59:18 UTC DK-2024-00000013 U3c payment result 1       captured, order confirmed
 65  2024-11-11 08:40:56 UTC DK-2024-00000013 U5 fulfil                  fulfilled, 1 units issued from DK-WH-01
 66  2024-11-12 11:23:44 UTC DK-2024-00000014 U1 place                   placed, 1 lines, total 2499.00 DKK
 67  2024-11-12 11:23:47 UTC DK-2024-00000014 U2 reserve                 reserved
 68  2024-11-12 11:24:04 UTC DK-2024-00000014 U3a payment attempt 1      pending (card, 2499.00 DKK)
 69  2024-11-12 11:24:11 UTC DK-2024-00000014 U3c payment result 1       failed (insufficient_funds)
 70  2024-11-12 11:24:51 UTC DK-2024-00000014 U3a payment attempt 2      pending (card, 2499.00 DKK)
 71  2024-11-12 11:24:54 UTC DK-2024-00000014 U3c payment result 2       captured, order confirmed
 72  2024-11-12 11:25:42 UTC DK-2024-00000014 U5 fulfil                  fulfilled, 1 units issued from DK-STO-01
 73  2024-11-12 20:19:22 UTC SE-2024-00000015 U1 place                   placed, 1 lines, total 8299.00 SEK
 74  2024-11-12 20:19:27 UTC SE-2024-00000015 U2 reserve                 reserved
 75  2024-11-12 20:21:43 UTC SE-2024-00000015 U3a payment attempt 1      pending (card, 8299.00 SEK)
 76  2024-11-12 20:21:49 UTC SE-2024-00000015 U3c payment result 1       captured, order confirmed
 77  2024-11-13 10:34:46 UTC SE-2024-00000015 U5 fulfil                  fulfilled, 1 units issued from SE-WH-01
 78  2024-11-13 14:38:04 UTC NO-2024-00000016 U1 place                   placed, 2 lines, total 1188.00 NOK
 79  2024-11-13 14:38:09 UTC NO-2024-00000016 U2 reserve                 reserved
 80  2024-11-13 14:40:40 UTC NO-2024-00000016 U3a payment attempt 1      pending (buy_now_pay_later, 1188.00 NOK)
 81  2024-11-13 14:40:44 UTC NO-2024-00000016 U3c payment result 1       captured, order confirmed
 82  2024-11-13 15:45:14 UTC SE-2024-00000017 U1 place                   placed, 1 lines, total 659.00 SEK
 83  2024-11-13 15:45:22 UTC SE-2024-00000017 U2 reserve                 reserved
 84  2024-11-13 15:46:14 UTC SE-2024-00000017 U3a payment attempt 1      pending (cash, 659.00 SEK)
 85  2024-11-13 15:46:18 UTC SE-2024-00000017 U3c payment result 1       captured, order confirmed
 86  2024-11-13 15:48:14 UTC SE-2024-00000017 U5 fulfil                  fulfilled, 1 units issued from SE-STO-01
 87  2024-11-13 16:15:11 UTC NO-2024-00000018 U1 place                   placed, 1 lines, total 2199.00 NOK
 88  2024-11-13 16:15:18 UTC NO-2024-00000018 U2 reserve                 reserved
 89  2024-11-13 16:15:49 UTC NO-2024-00000018 U3a payment attempt 1      pending (card, 2199.00 NOK)
 90  2024-11-13 16:15:54 UTC NO-2024-00000018 U3c payment result 1       captured, order confirmed
 91  2024-11-13 16:16:26 UTC NO-2024-00000018 U5 fulfil                  fulfilled, 1 units issued from NO-STO-03
 92  2024-11-14 09:54:30 UTC NO-2024-00000016 U5 fulfil                  fulfilled, 2 units issued from NO-WH-01
 93  2024-11-14 16:00:20 UTC SE-2024-00000019 U1 place                   placed, 1 lines, total 338.00 SEK
 94  2024-11-14 16:00:24 UTC SE-2024-00000019 U2 reserve                 reserved
 95  2024-11-14 16:04:11 UTC SE-2024-00000019 U3a payment attempt 1      pending (mobile_payment, 338.00 SEK)
 96  2024-11-14 16:04:14 UTC SE-2024-00000019 U3c payment result 1       captured, order confirmed
 97  2024-11-14 18:42:54 UTC DK-2024-00000020 U1 place                   placed, 2 lines, total 29324.00 DKK
 98  2024-11-14 18:42:56 UTC DK-2024-00000020 U2 reserve                 rolled back (out_of_stock)
 99  2024-11-14 18:42:57 UTC DK-2024-00000020 U2 cancel (out_of_stock)   cancelled (out_of_stock)
100  2024-11-15 08:44:22 UTC SE-2024-00000019 U5 fulfil                  fulfilled, 1 units issued from SE-WH-01
```
