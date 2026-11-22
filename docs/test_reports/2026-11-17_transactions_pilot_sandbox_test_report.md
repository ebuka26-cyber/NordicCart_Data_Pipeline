# Test report: pilot transaction generator, disposable sandbox

Date: 2026-11-17.

| Item | Version |
|---|---|
| PostgreSQL | 16.15, throwaway sandbox server (your server runs 18.4) |
| Python | 3.14.6 and 3.13.18, Linux |
| Packages | psycopg 3.3.9, pytest 9.2.0 (3.14) and 9.1.1 (3.13) |

Nothing was run against your `nordiccart_oltp`. No migration was changed. The 250 master data rows and the opening inventory are unchanged; their loaders still report "nothing to insert" after the transactions.

**Result: every test passed, 0 failed.**

## Files

New:

| File | Purpose |
|---|---|
| `python/nordiccart/transactions/generate.py` | Transaction generator: scenarios, customers, products, prices, totals, payments, timelines, simulated stock |
| `python/nordiccart/transactions/load.py` | Transaction loader: the V006/V007 units of work, one transaction each, in business-time order; resume, conflict detection, advisory lock, retries, verification |
| `python/nordiccart/transactions/validate.py` | Transaction validator: 67 named rules, including an independent stock replay |
| `python/nordiccart/transactions/model.py` | Planned orders, lines, payment attempts, units of work, stock ledger, fingerprint |
| `python/nordiccart/transactions/config.py` | Reads and checks `[transactions]`; only the pilot profile is approved |
| `python/nordiccart/transactions/cli.py`, `__init__.py`, `__main__.py` | Command line (dry run, `--plan`, `--apply`), logging, CSV preview |
| `python/run_transactions.py` | Runner for the VS Code Run button and the terminal |
| `tests/python/test_transactions.py` | 53 unit tests, no database |
| `tests/python/test_transactions_db.py` | 21 database behaviour tests in their own databases (opt-in) |
| `sql/verification/13_verify_pilot_transactions.sql` | SQL transaction verification for Valentina Studio (50 rows) |
| `docs/phase3_transactions_guide.md` | VS Code execution guide |

Changed:

| File | Change |
|---|---|
| `config/generator.toml` | New `[transactions]` section; existing values unchanged (master data and inventory fingerprints are the same) |
| `.vscode/launch.json` | 3 entries added after your 4 (Transactions: Dry Run, Check, Load); yours are unchanged. My tools cannot write into `.vscode`, so it is delivered as `vscode_launch.json` with copy steps in the guide |
| `sql/verification/11_verify_master_data.sql` | Revision 3: 201, 202, 203 and 205 (orders, lines, payments, reservations) became INFO counts, checked by script 13. 204 now allows sale movements. Now 33 PASS, 9 INFO, before and after the transactions (was 37 PASS, 5 INFO, which would have shown 5 false FAILs after the load) |
| `sql/verification/12_verify_opening_inventory.sql` | Revision 2: 201 allows sale movements; 202 and 203 became INFO counts; 501, 502 and 602 read the opening balances instead of the current stock; 603 lists the listings without opening stock. Now 17 PASS, 6 INFO, before and after (was 19 PASS, 4 INFO, which would have shown 4 false FAILs after the load) |
| `tests/python/test_master_data_seed_db.py`, `tests/python/test_opening_inventory_db.py` | One assertion each, for the new PASS/INFO counts of scripts 11 and 12 |
| `python/README.md`, `tests/README.md`, `sql/README.md`, `config/README.md`, `docs/phase3_master_data_generator_guide.md`, `docs/phase3_opening_inventory_guide.md`, `docs/phase2_database_implementation_status.md` | Documentation of the above |

## Run and results

| Step | Result |
|---|---|
| All 41 Python files compiled by Python 3.14 with warnings as errors | 0 problems |
| Fresh `nordiccart_oltp`: V001 to V007, master data pilot, opening inventory (as on your machine) | 250 rows; 320 listings, 288 opening balances, 1,460 units |
| Scripts 11 and 12 before the transactions | 33 PASS / 9 INFO and 17 PASS / 6 INFO, 0 FAIL |
| Dry run | 20 orders, 100 units of work, validation 0 issues, fingerprint `fabcd571a3b9f668...`, 0.5 s |
| `--plan` | 20 orders and 100 units of work would be written; nothing written |
| `--apply` without `--yes` from a non-interactive shell | refused, exit code 4, 0 orders written |
| `--apply --yes` | **100 units of work in 100 transactions, 20 orders placed and verified, 280 change_log rows** |
| `--apply --yes` again, and a third time | nothing to write (verified); exit code 0 |
| Master data and opening inventory loaders after the transactions | both "nothing to insert" |
| Script 13 (default and `search_path=pg_catalog`) | **50 rows: 44 PASS, 6 INFO, 0 FAIL** |
| Scripts 11 and 12 after the transactions (both settings) | 33 PASS / 9 INFO and 17 PASS / 6 INFO, 0 FAIL |
| Phase 2 verification 01 to 10 with the orders present | 477 PASS, 0 FAIL, 2 WARN (the expected V007 triggers in script 07); unchanged |
| Phase 2 SQL tests with the orders present | foundation 44/44, V006 119/119, V007 105/105; pilot data unchanged afterwards (20 orders, 21 payments, 305 movements, 1,441 units); script 13 still 44 PASS |

## Python tests (exact counts)

| Test file | Python 3.14.6 | Python 3.13.18 |
|---|---|---|
| `test_transactions.py` (unit) | **53 passed, 0 failed** | 53 passed, 0 failed |
| `test_transactions_db.py` (database) | **21 passed, 0 failed** | 21 passed, 0 failed |
| `test_master_data_generator.py` (regression) | 48 passed, 0 failed | 48 passed, 0 failed |
| `test_master_data_seed_db.py` (regression) | 13 passed, 0 failed | 13 passed, 0 failed |
| `test_opening_inventory.py` (regression) | 38 passed, 0 failed | 38 passed, 0 failed |
| `test_opening_inventory_db.py` (regression) | 18 passed, 0 failed | 18 passed, 0 failed |
| `test_v007_concurrency.py` (regression) | 11 of 11 scenarios | 11 of 11 scenarios |

Total: 191 pytest tests and 11 concurrency scenarios on each version, 0 failed. On 3.14 the pytest files ran with deprecation warnings treated as errors.

### Unit tests, `test_transactions.py` (53)

**The pilot as configured**
- 20 orders with the configured scenario mix, final statuses and payment outcomes, 100 units of work;
- both channels, all four markets, every payment method, multi-line and two-unit lines;
- validation passes;
- the same seed gives the recorded fingerprint;
- another seed gives other orders that still validate.

**Consistency with the master data**
- customers are active, of the order market and registered before ordering;
- products are on sale and locations open on the local order day (the two late launches are never sold);
- orders fall in the window, after the opening stock count, in online or store opening hours;
- market currency, VAT rate, and the price and cost valid at placed_at;
- line and order arithmetic with ROUND_HALF_UP, and the shipping rule;
- online orders ship from the market warehouse to the customer's address; in-store orders come from the store's own stock with no fee.

**Payments and lifecycle**
- payment rules: numbering, amounts, providers, cash in store only, one capture on the last attempt, unique references;
- every scenario follows its lifecycle and timing;
- units of work run in business-time order;
- order numbers follow placed_at.

**Stock**
- the out-of-stock order asks for more than the warehouse holds and reserves nothing, not even its available line;
- reserved stock is not available to the next customer (on hand 1, reserved 1, available 0);
- stock never goes negative and ends at opening minus sales (1,441).

**Validator catches 26 kinds of faults**
- customer registered later;
- product not launched;
- wrong currency; wrong VAT rate; wrong price snapshot;
- line total; items total; shipping fee; payment amount;
- cash online; capture not on the last attempt; too many attempts; payment after expiry;
- order outside hours; online order from a store;
- duplicate order number; bad order number;
- stock outcome; holder relation; reservation lifetime; expiry time;
- scenario outcome; order count; shipping address; event in the future.

**Settings and command line**
- 7 invalid settings are refused;
- only the pilot profile is allowed;
- the dry run writes the preview without a database and never logs the password;
- a validation failure stops before any database work.

### Database behaviour tests, `test_transactions_db.py` (21)

**Loading through the V006/V007 rules**
- plan is read-only;
- apply runs all 100 units of work and a rerun runs none (0 change_log rows);
- the final state equals the plan and the ledger;
- script 13 passes, and scripts 11 and 12 pass before and after.

**Business rules**
- out-of-stock orders leave no reservation or payment;
- reserved stock is not sold to the next customer (checked directly with `fn_reserve_order_stock`).

**Idempotency and recovery**
- an interrupted run continues where it stopped, at 6 points (after 1, 2, 13, 37, 58 and 99 units of work), each ending identical and passing script 13;
- two loaders at once: the second is refused;
- an order with other values is a conflict;
- orders outside the plan block the run;
- other stock movements block the run;
- an unplanned database result rolls back that step and stops the run; the next run continues;
- verification catches a step that did not happen.

**Safeguards and preconditions**
- the database refuses after the load: direct stock writes, changing a fulfilled order, a second sale;
- a missing opening inventory is refused.

**Command line**
- `--plan`, `--apply` refused without `--yes` (exit 4), `--apply --yes` twice.

## Mutation checks (do the tests catch defects?)

One defect at a time in a copy of the code, then both transaction test files on Python 3.14:

| Defect | Caught by |
|---|---|
| advisory lock not checked | two loaders at once |
| an order with other values not detected | conflict test |
| orders outside the plan not detected | blocking test |
| stock not compared with the plan | other stock movements test |
| units of work run order by order instead of in business time | reserved stock test |
| stock not released after the last payment failure | apply test (the V007 commit check refuses it) |
| verification after the run skipped | verification test |
| a pending payment counted as finished | interrupted run (after 13 steps) |
| U2 result not checked | unplanned result test |
| U4 releases instead of expiring | apply test (verification finds it) |
| customers chosen before they registered | validation passes test |
| shipping fee charged above the free threshold | validation passes test |
| in-store reservations live 15 minutes | validation passes test |
| store orders on Sundays | validation passes test |
| validator: stock replay removed | validator test |
| validator: registration rule removed | validator test |
| validator: price snapshot rule removed | validator test |
| validator: holder rule removed | validator test |
| stock wanted by placed but not yet reserved orders ignored | **not caught: equivalent for this pilot** (no two orders at the same location are placed within seconds, so the plan is identical, same fingerprint). It protects larger volumes. |

18 of 19 caught; the remaining one changes nothing in the pilot.

## The 20 pilot orders

Times are local.

| Order | Channel, location | Placed | Lines | Total | Payments | Result |
|---|---|---|---|---|---|---|
| NO-2024-00000001 | store NO-STO-03 | Fri 01 Nov 13:08 | SPH-000019 | 3999.00 NOK | mobile captured | fulfilled |
| FI-2024-00000002 | online FI-WH-01 | Sat 02 Nov 12:55 | ACC-000013 x2 | 83.80 EUR | mobile captured | fulfilled |
| NO-2024-00000003 | store NO-STO-01 | Sat 02 Nov 15:20 | ACC-000015 | 339.00 NOK | card failed x2 | payment_failed |
| SE-2024-00000004 | online SE-WH-01 | Sun 03 Nov 16:32 | ACC-000010, ACC-000013 | 758.00 SEK | none | cancelled (customer_request) |
| SE-2024-00000005 | online SE-WH-01 | Sun 03 Nov 20:09 | SPH-000001 | 9990.00 SEK | card captured | fulfilled |
| FI-2024-00000006 | store FI-STO-02 | Mon 04 Nov 14:14 | WEA-000020 | 109.00 EUR | card captured | fulfilled |
| SE-2024-00000007 | store SE-STO-03 | Tue 05 Nov 14:58 | SPH-000014, SMH-000009 | 2918.00 SEK | card failed, card captured | fulfilled |
| DK-2024-00000008 | store DK-STO-01 | Wed 06 Nov 17:57 | ACC-000013 x2, ACC-000010 | 837.00 DKK | card captured | fulfilled |
| NO-2024-00000009 | online NO-WH-01 | Thu 07 Nov 16:03 | ACC-000015, ACC-000010 | 658.00 NOK | none | cancelled (reservation_expired) |
| SE-2024-00000010 | online SE-WH-01 | Thu 07 Nov 19:31 | WEA-000020 | 1199.00 SEK | card failed x3 | payment_failed |
| SE-2024-00000011 | store SE-STO-03 | Sat 09 Nov 15:34 | SPH-000014 (last unit) | 1999.00 SEK | card captured | fulfilled |
| SE-2024-00000012 | store SE-STO-03 | Sat 09 Nov 15:35 | SPH-000014 | 1999.00 SEK | none | cancelled (out_of_stock) |
| DK-2024-00000013 | online DK-WH-01 | Sun 10 Nov 17:55 | HPH-000005 | 409.00 DKK | card captured | fulfilled |
| DK-2024-00000014 | store DK-STO-01 | Tue 12 Nov 12:23 | SPH-000019 | 2499.00 DKK | card failed, card captured | fulfilled |
| SE-2024-00000015 | online SE-WH-01 | Tue 12 Nov 21:19 | LAP-000018 | 8299.00 SEK | card captured | fulfilled |
| NO-2024-00000016 | online NO-WH-01 | Wed 13 Nov 15:38 | HPH-000005, ACC-000013 | 1188.00 NOK | buy-now-pay-later captured | fulfilled |
| SE-2024-00000017 | store SE-STO-01 | Wed 13 Nov 16:45 | ACC-000016 | 659.00 SEK | cash captured | fulfilled |
| NO-2024-00000018 | store NO-STO-03 | Wed 13 Nov 17:15 | GAM-000008 | 2199.00 NOK | card captured | fulfilled |
| SE-2024-00000019 | online SE-WH-01 | Thu 14 Nov 17:00 | ACC-000010 | 338.00 SEK (incl. 49.00 shipping) | mobile captured | fulfilled |
| DK-2024-00000020 | online DK-WH-01 | Thu 14 Nov 19:42 | WEA-000020, TAB-000003 x5 | 29324.00 DKK | none | cancelled (out_of_stock: warehouse holds 4) |

Fulfilled revenue excl. VAT / cost / gross profit (script 13, check 602):

| Market | Revenue excl. VAT | Cost | Gross profit |
|---|---|---|---|
| DK | 2,996.00 DKK | 2,215.45 DKK | 780.55 DKK |
| FI | 153.62 EUR | 89.42 EUR | 64.20 EUR |
| NO | 5,908.80 NOK | 4,528.37 NOK | 1,380.43 NOK |
| SE | 19,323.20 SEK | 16,042.04 SEK | 3,281.16 SEK |

## Outstanding issues

1. **launch.json is not installed yet.** My tools are not allowed to write into `.vscode`. The updated file is delivered as `vscode_launch.json`; the guide has the two copy steps.
2. **Not run on your exact setup.** Tested on PostgreSQL 16.15 and Python 3.14.6 on Linux; you have 18.4 and 3.14.7 on Windows. Nothing used is specific to a PostgreSQL version after 13, and the fingerprint test confirms identical data on your machine.
3. **The pilot mix is deliberately weighted.** 70% of orders are fulfilled so that every path runs once or twice. A realistic mix, and the order volume for the full business, are decisions for the next step.
4. **Shipping fee.** Online orders below the free-shipping threshold pay the market fee, because V006 makes the fee part of the order total. Only 1 of 10 online orders paid it (most exceed the threshold). No shipments are created. To have no fees at all, set the four fees to 0 before loading.
5. **Scaling up needs a design decision.**
   - The loader refuses a database with orders outside its plan, because they would change the stock it expects.
   - A larger run must therefore either include the pilot as the start of its plan, or start from the database state. It must continue the numbering at 21 after 2024-11-15 08:44 UTC.
   - It must also model restocking: SPH-000014 is now sold out in Malmö (SE-STO-03) and stays at 0 until purchase receipts or transfers are simulated.
6. **Not exercised yet:**
   - an authorization that arrives after the reservation expired (void path);
   - provider timeouts that stay pending;
   - anonymous in-store sales: all orders have a customer, as the business rules require;
   - promotions;
   - daily exchange rates for EUR reporting.
7. **Payments are simulated.** Provider outcomes and references are generated, not called. The design's U3 split is kept: the pending attempt is committed before the result is written in a new transaction.
8. **Repeat customers.** C00000071 places three orders in five days (online, then twice in the Malmö store where they live), and C00000063 places three. Plausible, but on the high side.
9. **Script 13 counts are for this pilot.** Its params block holds them. Scripts 11 and 12 changed their PASS/INFO counts; the guides show the new numbers.
10. **The reorder points are not acted on yet.** Restocking belongs to a later step.
