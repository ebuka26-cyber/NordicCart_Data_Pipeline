# Phase 3, step 5, stage S1: pilot scope, shared units of work and script 15 (test report)

Date: 2026-11-18. Design: `docs/phase3_combined_activity_design.md`, revision 2 (approved), sections 8.1 and 12 (stage S1).
Scope: S1 only. No database migration, nothing written to the real `nordiccart_oltp`, nothing of S2 or later.

## 1. Result

| Item | Result |
|---|---|
| Loaders accept independently verified later activity and still verify their own pilot records | Done. A later date is necessary, never sufficient: 23 identity and integrity rules (section 3.2) |
| Scripts 13 and 14 scoped to their pilots | Done: 13 revision 3 (52 rows), 14 revision 2 (44 rows) |
| Script 15 (global inventory and activity), initial version | Done: 43 rows (35 PASS checks, 8 INFO) |
| Units of work extracted into a shared module, behaviour unchanged | Done: `python/nordiccart/units/`. On real data the refactored replenishment units wrote business content identical to the original code (section 6.1) |
| Regression tests: later orders, receipts, transfers, extra listings, rejected conflicting data, idempotency | 28 new tests (21 database, 7 unit) |
| Full test suite in the disposable PostgreSQL 18.4 sandbox | **304 passed, 0 failed, 0 skipped** (137 s) |
| Mutation checks (planted defects in the scoped acceptance) | 5 of 5 caught |
| Pilot plan fingerprints | Unchanged: transactions `fabcd571…7c466264`, replenishment `858bbfd0…05531236` |
| Existing pilot records | Verify: sandbox restore of the real backup, and read-only checks of the real database (scripts 11 to 15: 0 FAIL; both loaders: 0 to write, 0 conflicts) |
| Migrations V001 to V007 | Unchanged (files last modified 2026-11-16; no migration added) |
| Real database | Not written. Read-only sessions only; business-content checksums identical before and after (section 6.2) |

## 2. Changed files

| File | Status | Change |
|---|---|---|
| `python/nordiccart/units/__init__.py` | new | Package description |
| `python/nordiccart/units/base.py` | new | `OutcomeMismatchError`, `expect`, `Ids`, `run_unit` (one unit = one transaction, `SET LOCAL lock_timeout = '10s'`, retries for 40001/40P01/55P03, optional 23505 "already done" check), session advisory lock helpers, `LEDGER_IDENTITY_SQL` |
| `python/nordiccart/units/orders.py` | new | U1 `place`, U2 `reserve` / `cancel_out_of_stock`, U3a `payment_start`, U3c `payment_result`, `customer_cancel`, U4 `expire`, U5 `fulfil`, `lock_order`: bodies moved verbatim from `transactions/load.py` |
| `python/nordiccart/units/stock.py` | new | R0 `revise_reorder_points`, R1 `receive`, T1 `dispatch`, T2 `arrive`, `lock_balances`: bodies moved verbatim from `replenishment/load.py`, parameterised by the document instead of plan + key |
| `python/nordiccart/activity/__init__.py` | new | Package for the combined activity (the engine follows in S6) |
| `python/nordiccart/activity/scope.py` | new | `PilotScope`, `LaterActivity`, `read_later_activity` (23 rules, set-based SQL, read-only), `expected_stock` |
| `python/nordiccart/transactions/load.py` | changed | Reads only its 20 pilot orders; foreign records through the scope rules; expected stock = pilot replay + accepted later activity; units delegated to `nordiccart.units` (old private names kept as aliases) |
| `python/nordiccart/replenishment/load.py` | changed | Foreign records through the scope rules (boundary = its own last event); conservation scoped to the pilot's listings and transfers; units delegated; guard for a plan without pilot identity |
| `python/nordiccart/replenishment/model.py` | changed | Fields `pilot_order_numbers`, `pilot_last_sequence` (not in the fingerprint); `last_event_at()`, `document_numbers()` |
| `python/nordiccart/replenishment/generate.py` | changed | Sets the two identity fields from the pilot plan |
| `sql/verification/13_verify_pilot_transactions.sql` | changed | Revision 3: scoped (section 4.1) |
| `sql/verification/14_verify_replenishment.sql` | changed | Revision 2: scoped (section 4.2) |
| `sql/verification/15_verify_inventory_and_activity.sql` | new | Initial global script (section 4.3) |
| `tests/python/test_pilot_scope_db.py` | new | 21 database tests |
| `tests/python/test_pilot_scope.py` | new | 7 unit tests |
| `tests/python/test_transactions_db.py` | changed | Expected row counts of script 13 only (section 5.3) |
| `tests/python/test_replenishment_db.py` | changed | Expected row counts of scripts 13 and 14; the "V007 accepts what only the application refuses" test follows the new script scopes (section 5.3) |
| `tests/README.md` | changed | The two new test files and how to run them |
| `docs/phase2_database_implementation_status.md` | changed | S1 recorded; next step list |

Not changed: CLIs (`transactions/cli.py`, `replenishment/cli.py`; their progress lines now include the later-activity summary through the loaders), generators other than the two identity fields, configuration, migrations, scripts 01 to 12, `.vscode/launch.json`, the design document.

## 3. What S1 does

### 3.1 Own records and later activity

| | Transaction pilot loader | Replenishment pilot loader |
|---|---|---|
| Own records, compared exactly with the plan | The 20 pilot orders and every row of them (lines, attempts, reservations, sales) | Its receipts, transfers and the R0 reorder points, matched by business key; the 20 pilot orders through the transaction loader (precondition) |
| Boundary (later activity strictly after it) | Its last event 2024-11-15 08:44:22 UTC | Its last event 2024-12-02 12:46:04 UTC |
| Own listings (stock check) | 320 (20 pilot products × 16 locations) | the same 320 |
| Own document numbers | none | `PO-{DK,FI,NO,SE}-2024-000001`, `TR-{DK,FI,NO,SE}-2024-000001` |

Every other record is foreign. It is accepted only as later activity that passes all rules of 3.2. Then:
- the plan must be complete; otherwise the loader blocks ("business-time order");
- the later movements and the active reservations of later orders at the pilot's 320 listings are added to the expected stock;
- extra listings outside the 320 are reported but do not touch the pilot's stock check.

Consequence: an order on 2024-11-25 is valid later activity for the transaction pilot. It is refused by the replenishment loader, because it lies inside the replenishment window. Script 14 catches it too: its network units at the pilot's last movement change (test `order_inside_replenishment_window`).

### 3.2 Identity and integrity rules (`nordiccart/activity/scope.py`)

| Rule | Refuses |
|---|---|
| order_not_after_boundary | foreign orders placed at or before the boundary |
| order_number_format | numbers not `<market>-<local year of placed_at>-<8-digit sequence>` |
| order_sequence_in_pilot_range | a later order using a sequence number 1 to 20 |
| order_sequence_duplicate | a sequence number used by two orders |
| order_sequence_gap | missing numbers after 20 (the sequence continues without gaps) |
| order_sequence_order | later orders not numbered in placed_at order |
| order_in_future | any order, payment, reservation or sale time after now() |
| order_event_before_placed | payment, reservation, sale or status times before placed_at |
| order_customer | no customer, a customer of another market, or one not registered at placed_at |
| order_lines_totals | no lines, line numbers not 1..n, line, items or order totals that do not add up |
| order_line_price | price or cost not from the product_prices row valid at placed_at |
| order_payments | attempts not 1..n, amount ≠ order total, not exactly one capture when confirmed or fulfilled (none otherwise) |
| order_reservation_line | reservation not at the fulfilment location, or other product or quantity than the line |
| order_stock_status | reservations and sales that do not match the status (fulfilled: one sale at fulfilled_at and a consumed reservation per line; stock_reserved/confirmed: an active reservation per line; otherwise none active and no sale) |
| movement_not_simulated | stock adjustments, write-offs, return restocks |
| movement_not_after_boundary | foreign receipts, transfers or sales dated at or before the boundary |
| movement_in_future | movements after now() |
| opening_outside_pilot | an opening balance at an extra listing (E2) not at the opening count time and reference of its market, or for a product not on sale on the opening day |
| receipt_rules | receipt not at a warehouse, or reference not `PO-<market>-<year>-<6 digits>/DN<n>` of its market |
| transfer_out_rules | not from a warehouse, or reference not `TR-<market>-<local year>-<6 digits>-<store of the same market>` |
| transfer_in_rules | arrival not matching its dispatch (product, quantity, reference, destination named in the reference, not earlier) |
| document_reuses_pilot | a later receipt or transfer with a pilot PO or TR number |
| document_identity | one PO number at two warehouses, one delivery note at two times, one TR number with two sources, times or destinations |

### 3.3 Shared units of work

The loaders keep deciding which unit runs and comparing results with their plan. The unit bodies (SQL, locks, `expect` checks) now live in `nordiccart.units`. Mechanics:
- The transaction loader's dispatcher looks the units up by the old names at call time, so `tx_load._place`, `_fulfil` and the others still exist. The existing test that replaces `_fulfil` still works unchanged.
- Both loaders share one `OutcomeMismatchError` class. The command lines catch it as before.
- The retry wrapper logs the same text as before ("<order> <unit>: SQLSTATE …, retry …").

## 4. Verification scripts

### 4.1 Script 13, revision 3 (52 rows: 45 PASS, 7 INFO; was 50: 44, 6)

- **Scope:** `params.pilot_orders` (the 20 order numbers) for orders, lines, payments, reservations and sales; `params.pilot_skus` (the 20 products) for the stock group.
- **105, 106, 109, 406:** now count the pilot orders' rows only.
- **501 to 503:** limited to the 320 pilot listings.
- **504:** no movement dated at or before the pilot's last event other than opening balances and the pilot orders' sales.
- **505:** units on hand at the pilot listings at the pilot's last event, rebuilt from the ledger in business time, = 1441. It no longer depends on later movements.
- **New 209:** no other order placed at or before the pilot's last event.
- **New 607 (INFO):** orders and movements outside the pilot.

### 4.2 Script 14, revision 2 (44 rows: 37 PASS, 7 INFO; was 43: 36, 7)

- **Scope:** the pilot's documents by number (`params.pilot_documents`: 4 PO and 4 TR numbers) and the 320 pilot listings. A later line that reuses a pilot document number is counted, so the 1xx counts fail.
- **Ledger group (501 to 507):** rebuilt in business time at the pilot's last movement (`params.replenishment_last_event` = 2024-12-02 12:46:04 UTC):
  - pilot-listing ledger identity;
  - never negative up to that time;
  - conservation;
  - network units = 1467;
  - warehouse capacity for the pilot's stock.
- **New 309:** no receipt or transfer outside the pilot's documents dated at or before that last movement.
- **INFO 603 to 605 and 607:** limited to the pilot.
- **Refinement of the design, for review:** section 8.1 says "dates up to 2024-12-06" (the review window end). The combined activity starts on 2024-12-03 (8.2), so a date scope up to 12-06 would count legitimate later documents. The scope is therefore the pilot's own document numbers plus its last movement. The design document itself is unchanged.

### 4.3 Script 15, new (43 rows: 35 PASS, 8 INFO)

Global checks over every record, pilot records included:

| Group | Checks |
|---|---|
| 1 Ledger | 101 on hand = Σ movements; 102 reserved = Σ active reservations; 103 no negative or oversold balance; 104 never negative in business time; 105 conservation (on hand + in transit = every non-transfer movement); 106 opening balances at the opening count (local midnight 2024-11-01) with `OB-20241101`; 107 nothing before the opening count; 108 nothing dated in the future; 109 warehouse capacity now; 110 no adjustment, write-off or return restock yet |
| 2 Documents | 201 receipts at warehouses with valid PO/DN references of the market; 202 transfers out from warehouses with valid TR references to a store of the market; 203 arrivals match their dispatch; 204 one PO, DN and TR number = one location, time and destination; 205 receipts and transfers inside the launch and discontinuation rules (pre-launch 7 days warehouse, 1 day store); 206 no transfer in transit more than 1 day before the latest business event; 207 no movement before its location opened |
| 3 Order identity | 301 number format; 302 sequence used once; 303 no gaps; 304 numbered in placed_at order; 305 customer of the market, registered at placed_at; 306 currency of the market |
| 4 Order integrity | 401 lines 1..n; 402 line and VAT formulas; 403 items, shipping VAT and order totals; 404 price list at placed_at; 405 market VAT, no discount (MVP); 406 product on sale on the local order day |
| 5 Lifecycle | 501 attempts 1..n, amount = total, no cash online; 502 exactly one capture when confirmed or fulfilled, at confirmed_at; 503 times not before placed_at and payment steps in order; 504 reservations at the fulfilment location for the line, lifetime 15 min online / 5 min in store; 505 reservations and sales match the status; 506 no sale before confirmation |
| 6 INFO (date range `params.range_start/range_end`) | orders by market and by status, movements by type, listings, units on hand / reserved / in transit, open orders, latest business event, change_log rows |

Deliberately unlike script 13, script 15 does not require customers or products to be active *now*. A later deactivation must not invalidate history.

## 5. Tests

### 5.1 Environment

- **Server:** disposable PostgreSQL 18.4 cluster (`initdb` in the session scratchpad, port 55432, trust authentication), started for this run.
- **Databases:** every test creates and drops its own databases (`nordiccart_tx_test_*`, `nordiccart_repl_test_*`, `nordiccart_s1_test_*`, …).
- **Connection:** all PG variables were set explicitly to the sandbox (PGHOST=localhost, PGPORT=55432, PGUSER, PGPASSWORD, PGDATABASE), so nothing fell back to `config/.env`.
- **Python:** 3.14.7 (`.venv`).
- **Command:**
  ```
  $env:NORDICCART_DB_TESTS = "1"   (and the five PG variables of the sandbox)
  .\.venv\Scripts\python.exe -m pytest tests/python -q -p no:cacheprovider
  ```
- **Baseline before any change:** 200 passed, 76 skipped (database tests not enabled).

### 5.2 Results (final run, all database tests enabled)

| File | Tests | Result |
|---|---|---|
| test_master_data_generator.py | 48 | passed |
| test_master_data_seed_db.py | 13 | passed |
| test_opening_inventory.py | 38 | passed |
| test_opening_inventory_db.py | 18 | passed |
| test_transactions.py | 53 | passed (includes the recorded transaction fingerprint) |
| test_transactions_db.py | 23 | passed |
| test_replenishment.py | 61 | passed (includes the recorded replenishment fingerprint) |
| test_replenishment_db.py | 22 | passed |
| **test_pilot_scope.py (new)** | 7 | passed |
| **test_pilot_scope_db.py (new)** | 21 | passed |
| **Total** | **304** | **304 passed, 0 failed, 0 skipped, 137 s** |

`tests/python/test_v007_concurrency.py` is a standalone script, not a pytest file. It was not run in S1, because V007 and its unit SQL are unchanged.

### 5.3 Existing tests that had to change, and why

The first run of the unchanged existing suites gave 17 failures. All of them were expected consequences of the new script scopes, and every script run in them had 0 FAIL rows:
- 16 were row-count assertions: script 13 now returns (52, 45, 7, 0) instead of (50, 44, 6, 0); script 14 returns (44, 37, 7, 0) instead of (43, 36, 7, 0).
- The 17th was the scope test below.

Changes made:
- `test_transactions_db.py`: constant `SCRIPT_13`, used by the existing script 13 assertions. No other change.
- `test_replenishment_db.py`: constants `SCRIPT_13` and `SCRIPT_14`, same use. In addition, `test_v007_accepts_what_only_the_application_and_script_14_refuse` now expects:
  - the backdated transfer (2024-11-10, inside the pilots) fails script 14 checks {309, 505, 506} (was {306, 110}) and script 13 checks {504, 505} (was {504});
  - the store receipt and the cross-border transfer, dated after the pilot, fail script 15 checks {201, 202, 203, 206}. Before S1 they failed script 14 checks {201, 204}; script 14 no longer looks at later documents;
  - script 15 passes before the inserts;
  - the replenishment loader refuses all three with the specific rule texts.

### 5.4 New database tests (`test_pilot_scope_db.py`)

All tests start from a copy of the real database's state (V001 to V007, master data, opening inventory, both pilots). Valid later records are written through the shared units of work (`nordiccart.units`).

| Test | Proves |
|---|---|
| shared units write valid later activity that both pilots accept | Later activity accepted by both loaders: 4 later orders (3 fulfilled, 1 open with reserved stock; numbers 21 to 24 in placed_at order), receipt `PO-SE-2024-000002/DN1`, transfer `TR-SE-2024-000002-SE-STO-01`. Both plans complete; both verify functions return no problems; scripts 11 to 15 pass. Later reserved units are counted in the expected stock |
| reruns and plans write nothing while later activity exists | Two plan + apply rounds of both loaders: 0 units of work, 0 change_log rows, every count unchanged (idempotency) |
| extra inventory listings of exception E2 are accepted | A new product with listings at SE-WH-01 (opening stock 12 at the opening count) and SE-STO-01 (zero), then a later transfer. Accepted by both loaders: 2 extra listings, 1 opening balance; nothing changes at the pilot listings; scripts 13 to 15 pass |
| invalid later records are refused (14 cases) | See the table below. In every case the refusing loader blocks with the rule's text, apply raises the conflict error and writes nothing, and the named script checks fail |
| the pilots' own records are still compared exactly | With valid later activity present, one changed R0 reorder point is still a conflict, and script 14 check 111 fails |
| a later sale at a pilot listing is counted | The later stock change per pilot listing equals replenishment + receipt + transfer − later sales |
| later activity before the replenishment pilot is complete blocks it | Replenishment stopped after 9 of 17 units, then a later receipt: blocked ("business-time order"), nothing written. The transaction pilot still verifies |
| command lines with later activity and with an invalid record | Both `--plan` exit 0, both `--apply --yes` say "nothing to write". After a stock adjustment: transactions `--plan` exits 2; replenishment `--plan` exits 3 (precondition: the pilot transactions refuse) |

| Refused case | Transaction loader | Replenishment loader | Script checks failing |
|---|---|---|---|
| sequence of the pilot range reused (another market prefix) | refuses | refuses | 15: 302, 304 |
| gap in the sequence (22 without 21) | refuses | refuses | 15: 303 |
| order number of another market | refuses | refuses | 15: 301 |
| order placed 2024-11-25 (inside the replenishment window) | accepts (later for this pilot) | refuses | 14: 506 |
| price not from the price list (+1.00, totals consistent) | refuses | refuses | 15: 404 |
| customer of another market | refuses | refuses | 15: 305 |
| receipt at a store | refuses | refuses | 15: 201 |
| receipt reusing pilot PO number (`PO-SE-2024-000001/DN3`) | accepts (no own documents) | refuses | 14: 101, 103, 306 |
| arrival at another store than the reference | refuses | refuses | 15: 203 |
| malformed transfer reference | refuses | refuses | 15: 202 |
| one TR number for two destinations | refuses | refuses | 15: 204 |
| stock adjustment after the pilot | refuses | refuses | 15: 110 |
| movement dated in 2027 | refuses | refuses | 15: 108 |
| E2 opening balance not at the opening count | refuses | refuses | 15: 106 |

### 5.5 New unit tests (`test_pilot_scope.py`)

- Pilot fingerprints equal the recorded values.
- The replenishment plan names its pilot (20 order numbers, last sequence 20, 8 document numbers, last event 2024-12-02 12:46:04). Clearing those fields does not change its fingerprint, and the scope then refuses to run.
- Each loader's scope: boundaries, 20 orders, 320 listings.
- `expected_stock` adds later activity only at pilot listings.
- Every SQL rule has a message and every message a rule.
- The loaders' unit names are the shared units.
- The summary line names every count.

### 5.6 Mutation checks (defects planted by monkeypatch in a separate process; project files untouched)

| Mutation | Tests failing | Caught |
|---|---|---|
| M1: accept later records by date alone (drop identity and integrity problems) | 13 refused cases | yes |
| M2: forget later activity in the pilots' stock check | 8 | yes |
| M3: replenishment scope uses the transaction pilot's boundary | order inside the replenishment window | yes |
| M4: forget the pilot's own document numbers | receipt reusing a pilot PO | yes |
| M5: accept later activity while a pilot is incomplete | business-time order test | yes |

## 6. Database compatibility

### 6.1 Sandbox restore of the real database, replenishment replayed by the refactored code

Source: `database/backups/nordiccart_oltp_20261118_032247_pre_replenishment.dump`, the newest backup of the real database. It was taken before the replenishment load and restored into the sandbox database `nordiccart_oltp_s1compat`, port 55432.

| Step | Result |
|---|---|
| Restore | 20 orders, 305 movements, 1,441 units, change_log 1,426, migrations 001 to 007, as at 03:22:47 |
| Scripts 11, 12, 13 (r3), 15 | 0 FAIL each (43/33/10, 24/17/7, 52/45/7, 43/35/8) |
| `run_transactions.py --plan` | fingerprint `fabcd571…`; 20/20 orders, 100/100 units of work, 0 to write, 0 conflicts (log `logs/sandbox_s1_20261118/transactions_pilot_plan_20261118_131651.log`) |
| `run_replenishment.py --apply --yes` (sandbox only) | fingerprint `858bbfd0…`; 17 units of work in 17 transactions (revise 4, receive 5, dispatch 4, arrive 4), 17 verified, 111 change_log rows (log `…/replenishment_pilot_apply_20261118_131703.log`) |
| Both `--plan` afterwards | 0 to write, 0 conflicts. The transaction pilot reports the 21 replenishment movements as later activity, net +26 |
| End state | 20 orders, 326 movements, 1,467 units, 0 reserved, warehouse reorder points 533, change_log 1,537, last movement 2024-12-02 12:46:04 UTC: the figures of the real load (2026-11-18 execution report) |
| Scripts 11 to 15 | 0 FAIL each |

Business-content checksums: md5 over the business columns of each table, without surrogate ids or load times, timestamps in UTC.

| Table | Rows | Sandbox replay (refactored code) | Real database (original code) |
|---|---|---|---|
| orders | 20 | 5f52439daf02cd7ad0c3fc0176850648 | 5f52439daf02cd7ad0c3fc0176850648 |
| order_items | 26 | 18e8a9b635e47921cbcee56ba0ce8d12 | 18e8a9b635e47921cbcee56ba0ce8d12 |
| payment_attempts | 21 | 5e20680e3f5f35221e7986e006d04936 | 5e20680e3f5f35221e7986e006d04936 |
| inventory_reservations | 23 | 6073d4d6c3bb3a281bdbd2dea1fe3ae5 | 6073d4d6c3bb3a281bdbd2dea1fe3ae5 |
| inventory_movements | 326 | 3619b46468ef7a46f7e921ce191604d0 | 3619b46468ef7a46f7e921ce191604d0 |
| inventory_balances | 320 | 5b04904050c1c2a0f7a6ab0ddbf46dad | 5b04904050c1c2a0f7a6ab0ddbf46dad |

The extracted replenishment units write exactly what the original code wrote to the real database.

### 6.2 Read-only checks of the real database

Connection: `config/.env`; credentials are not shown here. Every session ran with `PGOPTIONS=-c default_transaction_read_only=on`; the server confirmed `default_transaction_read_only on` and `transaction_read_only on`. Nothing was written.

| Check | Result |
|---|---|
| Scripts 11, 12, 13 (r3), 14 (r2), 15 | 0 FAIL each: 43/33/10, 24/17/7, 52/45/7, 44/37/7, 43/35/8 |
| `run_transactions.py --plan --no-csv` | fingerprint `fabcd571…`; 20/20 orders, 100/100 units of work, 0 to write, 0 conflicts; later activity 21 movements, net +26 (log `logs/s1_readonly_check_20261118/transactions_pilot_plan_20261118_131816.log`) |
| `run_replenishment.py --plan --no-csv` | fingerprint `858bbfd0…`; 17/17 units of work, 0 to write, 0 conflicts (log `…/replenishment_pilot_plan_20261118_131817.log`) |
| Content before and after | Identical: the six checksums above, change_log 1,537, 7 migrations, last movement 2024-12-02 12:46:04 UTC |

All existing pilot records verify under the new code and scripts.

## 7. Problems met during the work (all fixed before the final run)

| Problem | Fix |
|---|---|
| `x = ANY ((SELECT array FROM params))` is parsed as a subquery comparison (`varchar = text[]`, SQLSTATE 42883) in scripts 13 and 14 | `ANY ((SELECT …)::text[])` |
| `IS DISTINCT FROM ALL (…)` is not valid SQL (script 14, check 309) | `NOT coalesce(x = ANY (…), false)` |
| A sandbox config written with PowerShell 5.1 `Out-File -Encoding utf8` has a BOM that `tomllib` rejects | Written as UTF-8 without BOM (scratchpad file only) |
| First content checksums differed on timestamp tables (sandbox session in CET, real in UTC) | Session `TimeZone = 'UTC'` in the checker; then identical |

## 8. Open items for review (not resolved in S1)

1. **Later reorder point changes (R-cal, stages S2/S6).**
   - The replenishment loader compares the R0 values with the *current* reorder points, and script 14 checks 111, 401, 402 and 403 read them too.
   - The first monthly reorder point review of the catch-up will turn them into conflicts or FAILs.
   - Reorder point changes carry no business time in the database, and change_log history is purged in Phase 4.
   - **Recommendation:** decide in S4 whether V009 adds a reorder point revision document (R0 backfilled like E3), so that R0 and R-cal stay verifiable without change_log.
2. **Scripts 11 and 12 count globally.** They fail once products or listings are added (E2, S7). This was expected by the design ("full-profile revisions" in S7). The E2 test therefore does not assert them.
3. **Script 14 scope** differs from the design's wording (section 4.2). Please confirm.
4. **Write order is not visible in the data.** An order dated inside the replenishment window is accepted by the transaction loader, but the replenishment loader refuses it and script 14 fails it. Backdated writes in general are to be refused by the engine (S6), together with its run ledger (V009). Script 15 cannot see load order.
5. **Activity lock.** Design 7.3 step 1 (the pilot loaders refuse `--apply` while `nordiccart.activity` is held) belongs to the engine, S6. It is not in S1.
6. **Scale.** The scope SQL reads all movements a few times per plan; the replenishment loader reads all receipts and transfers. This is fine at pilot size; time it at full scale in S8.
7. **Business-policy checks for later activity** are not in the loaders, which check identity and integrity. Script 15 checks reservation lifetimes and launch windows, but not:
   - store, receiving or dispatch hours;
   - the (s, S) policy.

   These follow with the engine's configuration (S6).
8. **Script 15 is the initial version.** Purchase orders (V009), exchange rates (script 16), price changes and run-ledger checks come with their stages.

## 9. Restrictions honoured

- V001 to V007 not modified; no migration created.
- Nothing written to the real `nordiccart_oltp`: only read-only sessions, with content identical before and after.
- No S2 or later work: no demand model, no V008/V009, no engine.
- All pilot orders, inventory records and transaction history preserved and verified.
- Credentials not shown.
- The sandbox cluster on port 55432 is disposable. The sandbox database `nordiccart_oltp_s1compat` remains inside it for inspection.

Stopped after S1 for review.
