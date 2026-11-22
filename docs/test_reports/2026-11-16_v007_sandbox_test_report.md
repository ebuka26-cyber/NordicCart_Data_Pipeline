# Test report: V007 inventory, disposable sandbox

Date: 2026-11-16, 20:30 UTC. Environment: PostgreSQL 16.15 in a throwaway instance in Claude's workspace, created for this work and deleted afterwards. The real NordicCart database was not touched. No business data was generated: every test row was rolled back or lived in a temporary database that was dropped.

## Run

1. `database/setup/00_create_database.sql` on a fresh database: OK
2. Migrations V001 to V007 in order, `psql -v ON_ERROR_STOP=1`: all applied, history `001,002,003,004,005,006,007`
3. Verification scripts 01 to 10, then a second pass of all of them with `search_path=pg_catalog` (session independence)
4. Behaviour tests: foundation, V006 (revision 3) and V007, then V006 and V007 a second time (repeatability)
5. After all test runs: 0 rows in every business table and in `change_log`
6. Second run of V007: refused by its guard ("Migration V007 is already applied")
7. Backwards compatibility: a separate V001 to V006 database with verification 01 to 07 and the foundation and V006 tests
8. Concurrency and failure recovery: `tests/python/test_v007_concurrency.py` in its own database, dropped afterwards
9. Mutation checks (below)

V001 to V006 and verification scripts 01 to 07 are byte-identical to the versions applied to the real database (SHA-256 checked).

## Results at V007

| Script | Rows | PASS | FAIL | WARN | INFO |
|---|---|---|---|---|---|
| 01_verify_environment_and_migrations.sql | 13 | 11 | 0 | 0 | 2 |
| 02_verify_tables_and_columns.sql | 115 | 109 | 0 | 0 | 6 |
| 03_verify_keys_and_constraints.sql | 47 | 47 | 0 | 0 | 0 |
| 04_verify_indexes_triggers_and_types.sql | 77 | 53 | 0 | 0 | 24 |
| 05_verify_v006_tables_and_columns.sql | 62 | 62 | 0 | 0 | 0 |
| 06_verify_v006_keys_and_constraints.sql | 39 | 39 | 0 | 0 | 0 |
| 07_verify_v006_indexes_triggers_and_types.sql | 64 | 39 | 0 | 2 | 23 |
| 08_verify_v007_tables_and_columns.sql | 40 | 40 | 0 | 0 | 0 |
| 09_verify_v007_keys_and_constraints.sql | 24 | 24 | 0 | 0 | 0 |
| 10_verify_v007_indexes_triggers_and_types.sql | 70 | 53 | 0 | 0 | 17 |
| **Verification total** | **551** | **477** | **0** | **2** | **72** |
| test_foundation_constraints.sql | 44 | 44 | 0 | | |
| test_v006_orders_and_payments.sql (revision 3) | 119 | 119 | 0 | | |
| test_v007_inventory.sql | 105 | 105 | 0 | | |
| **Behaviour tests total** | **268** | **268** | **0** | | |

- The `search_path=pg_catalog` pass gave the same counts.
- The second run of the V006 and V007 tests gave the same counts: 119 and 105 PASS.
- INFO rows are objects from a later migration, seen by an earlier script (expected).
- The 2 WARN rows in 07 are the V007 triggers on V006 tables (`trg_orders_inventory_check`, `trg_payment_attempts_inventory_guard`). Script 07 does not know them; script 10 checks them (PASS).

## Backwards compatibility (V001 to V006 only)

| Check | Rows | PASS | FAIL |
|---|---|---|---|
| Verification 01 to 07 | 381 | 360 (21 INFO) | 0 |
| test_foundation_constraints.sql | 44 | 44 | 0 |
| test_v006_orders_and_payments.sql (revision 3) | 119 | 119 | 0 |

Same results as the V006 report, so the revised V006 test file still works on a database without V007.

## Concurrency and failure recovery (Python, 11 scenarios)

| # | Scenario | Result |
|---|---|---|
| 1 | 10 sessions race through U2 for the last 5 units | PASS: 5 reserved, 5 out of stock and cancelled, stock 5 on hand / 5 reserved, no errors |
| 2 | Two direct reservation inserts without the U2 lock, 3 units for 2 + 2 | PASS: second fails with `23514 ck_inventory_balances_no_oversell`, stock 3 / 2 |
| 3 | Orders with lines in opposite order: held-row case (3 rounds) and free race (20 pairs) | PASS: 6/6 and 40/40 reserved, 0 deadlocks |
| 4 | Two concurrent U5 calls for one order | PASS: one `fulfilled`, one `already_fulfilled`, 1 sale movement, stock 100 / 3 to 97 / 0 |
| 5 | Expiry sweeper locks first, capture second | PASS: 0 rows captured, order cancelled, attempt voided, reservation expired |
| 6 | Capture locks first, sweeper second (`SKIP LOCKED`) | PASS: captured, sweeper skipped, order confirmed |
| 7 | Session killed (`pg_terminate_backend`) in the middle of U2 | PASS: stock unchanged, order still placed; retry reserved |
| 8 | 20 concurrent receipts on one balance | PASS: on hand 20 = ledger sum 20 (no lost update) |
| 9 | REPEATABLE READ conflict on a balance row | PASS: first try 40001, retry reserved, stock 10 / 6 |
| 10 | The same receipt (same document number) sent twice at once | PASS: one stored, other `23505 uq_inventory_movements_document_line` |
| 11 | Ledger invariants after all scenarios | PASS: 7 balances, 0 inconsistent |

The suite was run 8 times in total (including the final fresh run): 11 of 11 PASS every time.

## Mutation checks (do the tests catch defects?)

Each mutant removed one rule from a copy of V007 (CHECK set to `true`, unique index made non-unique, one guard `RAISE` removed, or one step of a function changed). The mutant was applied to a fresh V006 database, and both SQL test files were run against it.

| Mutant group | Mutants | Caught by behaviour tests | Caught only by verification | Equivalent |
|---|---|---|---|---|
| Named CHECKs | 13 | 11 | 2 (09) | 0 |
| Unique indexes (partial uniques and idempotency keys) | 6 | 5 | 1 (10) | 0 |
| Guard, commit-time and function rules (each `RAISE`) | 32 | 29 | 0 | 3 |
| Function steps and triggers (availability check, idempotent returns, consume-before-sale order, ledger maintenance, release, insert-only trigger, parameter check) | 8 | 8 | 0 | 0 |
| **Total** | **59** | **53** | **3** | **3** |

- **Caught only by verification.**
  - `ck_inventory_balances_on_hand` follows from `reserved >= 0` and `reserved <= on_hand`.
  - `ck_inventory_reservations_quantity` is shadowed by the reservation guard: the quantity must equal the line quantity, which is above 0.
  - The return_restock unique index cannot be reached while return_restock is refused (until DBML Phase 4).
  - Scripts 09 and 10 compare definitions, so they report all three as FAIL.
- **Equivalent.** These mutants are not test gaps: the observable result does not change.
  - Two `RAISE`s in the balance-maintenance functions duplicate the composite FK. The FK fires first and raises the same `23503` with the same constraint name.
  - The lines check in `fn_fulfil_order_stock` duplicates the commit-time check, which raises the same `orders_fulfilled_stock_issued`.
- **Closed by new tests.** The first mutation round found two gaps. Tests 103 (`ck_inventory_reservations_closed_at`) and 143 (`ck_inventory_balances_reserved`, reached by bypassing the quantity guard from inside another trigger) were added for them. Both mutants are now caught.

Concurrency mutants, run through the Python test:

| Defect introduced | Caught by |
|---|---|
| Balances locked in line order instead of (location, product) order | scenario 3: 3 deadlocks |
| U2 without locking the balance rows | scenario 1: oversell attempts fail with 23514 instead of a clean `out_of_stock` |
| U5 without locking the order row | scenario 4: the second call fails instead of reporting `already_fulfilled` |
| No-oversell CHECK removed | scenarios 2 and 11: reserved 4 with 3 on hand |

4 of 4 caught. Scenario 3 originally relied on timing and missed the lock-order defect, so it was rewritten with a held row that makes the interleaving deterministic.

## DBML fidelity

Compared with the DDL that `dbml2sql` generates from `nordiccart_oltp_rev3.dbml`:

- Columns of the three tables are identical (name, type, NOT NULL, default).
- Enums are identical.
- Constraints differ only by design choice:
  - `pk_*` names instead of `*_pkey`;
  - the location/product unique key is a named constraint;
  - three deferred constraint triggers are added;
  - `fk_inventory_movements_return_item` is missing because `return_items` arrives in DBML Phase 4.
- Indexes: the generator emits no partial indexes. V007 adds the four [PG] partial uniques from the design and the two V007 idempotency indexes.

## Not tested here

- Valentina Studio itself: the files were run with psql. Valentina ran the comparable V006 files without problems.
- PostgreSQL 13 to 15: the sandbox ran 16. V007 uses nothing newer than 13.
