# Test report: V006 revision 2, disposable sandbox

Date: 2026-11-16, 15:04 UTC. Environment: PostgreSQL 16.15 in a throwaway instance in Claude's workspace, created for this run and deleted afterwards. The real NordicCart database was not touched.

## Run

1. `database/setup/00_create_database.sql` on a fresh instance: OK
2. Migrations V001 to V006 in order, `psql -v ON_ERROR_STOP=1`: all applied, history `001,002,003,004,005,006`
3. Every verification script and both behaviour test files, then a second pass of all of them with `search_path=pg_catalog` (session independence)
4. Second run of V006: refused by its guard ("Migration V006 is already applied")
5. After all runs: 0 rows left in `change_log`, `orders` and `payment_attempts` (test sandboxes rolled back)

## Results

| Script | Rows | PASS | FAIL | WARN | INFO |
|---|---|---|---|---|---|
| 01_verify_environment_and_migrations.sql | 12 | 11 | 0 | 0 | 1 |
| 02_verify_tables_and_columns.sql | 112 | 109 | 0 | 0 | 3 |
| 03_verify_keys_and_constraints.sql | 47 | 47 | 0 | 0 | 0 |
| 04_verify_indexes_triggers_and_types.sql | 62 | 53 | 0 | 0 | 9 |
| 05_verify_v006_tables_and_columns.sql | 62 | 62 | 0 | 0 | 0 |
| 06_verify_v006_keys_and_constraints.sql | 39 | 39 | 0 | 0 | 0 |
| 07_verify_v006_indexes_triggers_and_types.sql | 47 | 39 | 0 | 0 | 8 |
| **Verification total** | **381** | **360** | **0** | **0** | **21** |
| test_foundation_constraints.sql | 44 | 44 | 0 | | |
| test_v006_orders_and_payments.sql | 119 | 119 | 0 | | |
| **Behaviour tests total** | **163** | **163** | **0** | | |

INFO rows are objects of a later migration seen by an earlier script (expected). The `search_path=pg_catalog` pass gave the same PASS counts with 0 FAIL.

## Mutation check (do the new tests catch defects?)

Each rule below was removed in a scratch copy of the sandbox database and the V006 tests were run again.

| Rule removed | Tests that failed |
|---|---|
| cancelled order without open authorization (commit-time) | 154, 160 |
| TRUNCATE guard on payment_attempts | 129, plus later tests affected by the emptied table |
| order identity block and fulfilment-country check | 25, 26, 45 and later tests affected by the extra rows |
| final payment attempts immutable | 115, 127, 157 |

4 of 4 removals detected.
