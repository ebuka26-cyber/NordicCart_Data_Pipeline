# Phase 2: running the migrations in Valentina Studio

This guide creates the `nordiccart_oltp` database, applies the migrations, and checks the result. Nothing in it generates business data.

| Part | Migrations | Status | Time |
|---|---|---|---|
| [Part A](#part-a-foundational-tables-v001-to-v005) | V001 to V005, foundational tables | done on your database | about 15 minutes |
| [Part B](#part-b-orders-and-payments-v006) | V006, orders and payments | done on your database | about 10 minutes |
| [Part C](#part-c-inventory-v007) | V007, inventory | done on your database | about 10 minutes, plus 5 for the optional concurrency test |

A later migration is always applied the same way as Parts B and C: on the existing database, one new file, then its own checks. Already-applied files are never run or edited again.

## What you need

| Requirement | How to check |
|---|---|
| PostgreSQL 13 or later, running | In any Valentina SQL Editor connected to the server: `SELECT version();` |
| A login role that can create databases and extensions (the `postgres` superuser is simplest) | You connect with it in Valentina |
| Valentina Studio (the free edition is enough) with a saved connection to that PostgreSQL server | The server appears in Valentina's Schema Editor |
| This project folder | `<project folder>` |

## How to run a script in Valentina Studio (used in every step)

1. Open the `.sql` file in a text editor (Notepad or VS Code), press `Ctrl+A`, then `Ctrl+C`.
2. In Valentina, open a SQL Editor on the right database. Either double-click the database in the Schema Editor, or right-click it and choose **Open SQL Editor**.
3. Check the **Active Database** menu on the SQL Editor toolbar shows the database the step names.
4. Clear the editor, paste with `Ctrl+V`, and press **Execute** (`Ctrl+E` on Windows).
5. Read the console pane at the bottom. Any line starting with `ERROR` means the script failed; go to [Troubleshooting](#troubleshooting).

Each migration is a single transaction. If anything fails, nothing from that file is kept, so a failed run is always safe to fix and repeat.

## Part A: foundational tables (V001 to V005)

### Step 1. Check the PostgreSQL version

Connected to any database on the server, run:

```sql
SELECT version();
```

The version must be 13 or later. If it is older, stop here: the change capture design needs `xid8` and `pg_snapshot`, which arrived in PostgreSQL 13.

### Step 2. Create the database (once)

File: `database/setup/00_create_database.sql`. Active database: **postgres**.

`CREATE DATABASE` must run on its own, so run the two statements separately:

1. Paste only statement 1 and execute it:
   ```sql
   CREATE DATABASE nordiccart_oltp
       WITH ENCODING 'UTF8'
            TEMPLATE template0;
   ```
2. Replace the editor contents with statement 2 and execute it:
   ```sql
   ALTER DATABASE nordiccart_oltp SET timezone TO 'UTC';
   ```
3. Press **Refresh** on the main toolbar. `nordiccart_oltp` now appears under the server.

If statement 1 says the database already exists, skip to step 3.

### Step 3. Open a SQL Editor on the new database

Open a **new** SQL Editor on `nordiccart_oltp` (double-click it in the Schema Editor). A new connection is needed so the UTC setting from step 2 applies. Confirm the **Active Database** menu shows `nordiccart_oltp`.

### Step 4. Apply the migrations, one file at a time, in this order

Active database for all five: **nordiccart_oltp**.

| Order | File in `database/migrations/` | What success looks like |
|---|---|---|
| 1 | `V001__migration_history_and_extensions.sql` | no ERROR in the console |
| 2 | `V002__change_capture_and_shared_trigger_functions.sql` | no ERROR; a result tab with an empty `assert_migration_can_run` value (normal) |
| 3 | `V003__reference_data_currencies_countries_exchange_rates.sql` | same as V002 |
| 4 | `V004__catalogue_and_customers.sql` | same as V002 |
| 5 | `V005__stock_locations_stores_warehouses.sql` | same as V002 |

After each file, you can confirm progress with:

```sql
SELECT version, script_name, applied_at, applied_by
  FROM meta.schema_migrations
 ORDER BY version;
```

After V005 it lists five rows, `001` to `005`.

Then press **Refresh**. Under `nordiccart_oltp` you should see schema `public` with 12 tables (`change_log`, `change_log_checkpoints`, `countries`, `currencies`, `customers`, `exchange_rates`, `product_categories`, `product_prices`, `products`, `stock_locations`, `stores`, `warehouses`) and schema `meta` with `schema_migrations`.

### Step 5. Run the verification scripts

Folder: `sql/verification/`. Active database: **nordiccart_oltp**. All four are read-only.

| File | Result tabs | Pass condition |
|---|---|---|
| `01_verify_environment_and_migrations.sql` | 1 (11 rows) | `failed_checks` = 0 |
| `02_verify_tables_and_columns.sql` | 2: tables (12 rows), columns (97 rows) | `failed_checks` = 0 in both |
| `03_verify_keys_and_constraints.sql` | 1 (47 rows) | `failed_checks` = 0 |
| `04_verify_indexes_triggers_and_types.sql` | 1 (53 rows) | `failed_checks` = 0 |

How to read the results:

- Rows are sorted with problems first, so a clean run shows PASS on the first row.
- `expected` is what the design requires; `actual` is what the database has.
- `PASS`: matches. `FAIL`: missing or different, must be fixed. `WARN`: advisory (for example a session time zone other than UTC, or an extra object the script does not know about). `INFO`: objects from later migrations; ignore.

### Step 6. Run the behaviour tests

File: `tests/sql/test_foundation_constraints.sql`. Active database: **nordiccart_oltp**.

The result tab has 44 rows. Pass condition: every `status` is PASS and `failed_tests` = 0.

The tests insert sample rows (test currency `XTS`, test country `XA`) to prove that each rule rejects bad data and accepts good data, then roll everything back. Test 99 confirms that nothing was left behind. The only lasting effect is a few used-up ID numbers in the identity sequences, so later IDs will not start at 1. If that matters to you, run the tests before loading any data, or skip them.

### Step 7. Keep a record

Export or screenshot the four verification results and the test result. They are useful evidence for the portfolio write-up.

## Part B: orders and payments (V006)

Run this on your existing `nordiccart_oltp`, which already has V001 to V005. Do not run V001 to V005 again.

### Step B1. Confirm the starting point

Open a SQL Editor on **nordiccart_oltp** and run:

```sql
SELECT version, script_name, applied_at
  FROM meta.schema_migrations
 ORDER BY version;
```

It must list exactly `001` to `005`.

| What you see | What to do |
|---|---|
| `001` to `005` | Continue with step B2. |
| `001` to `006` | V006 is already applied. Skip to step B3. |
| Fewer than five rows | Finish Part A first. |

### Step B2. Apply V006

File: `database/migrations/V006__orders_order_items_payment_attempts.sql`. Active database: **nordiccart_oltp**.

Paste the whole file and press **Execute** (`Ctrl+E`), exactly as in Part A. Success looks like:

- no line starting with `ERROR` in the console;
- one result tab with an empty `assert_migration_can_run` value (normal);
- the query from step B1 now lists `001` to `006`.

Press **Refresh**. Schema `public` now has 15 tables: the 12 from Part A plus `orders`, `order_items` and `payment_attempts`.

### Step B3. Run the V006 verification scripts

Folder: `sql/verification/`. Active database: **nordiccart_oltp**. All three are read-only.

| File | Result tabs | Pass condition |
|---|---|---|
| `05_verify_v006_tables_and_columns.sql` | 2: migrations and tables (9 rows), columns (53 rows) | `failed_checks` = 0 in both |
| `06_verify_v006_keys_and_constraints.sql` | 1 (39 rows) | `failed_checks` = 0 |
| `07_verify_v006_indexes_triggers_and_types.sql` | 1 (47 rows: 39 PASS, 8 INFO) | `failed_checks` = 0 |

The 8 INFO rows in script 07 are the enums and functions from V001 to V005, which script 04 already checks.

Optional but recommended: run scripts 01 to 04 again. They still pass after V006. The new tables, enums, functions and the V006 migration row appear there as INFO, which is expected.

### Step B4. Run the V006 behaviour tests

File: `tests/sql/test_v006_orders_and_payments.sql`. Active database: **nordiccart_oltp**.

The result tab has 119 rows. Pass condition: every `status` is PASS and `failed_tests` = 0.

The tests place sample orders (order numbers `NC-TEST-*`, test currencies `XTS` and `XXX`, test countries `XA` and `XB`), take them through payment failures, retries, capture, confirmation, fulfilment, payment failure and expiry, try to rewrite history (order identity, customer, timestamps, completed payments, deletes, one TRUNCATE), try fulfilment from the wrong country or location, and try to leave cancelled orders with open authorizations. They check that every invalid step is refused by the intended rule, and then roll everything back. In the `expected` column, a rejection shows as the SQLSTATE plus the rule name (for example `23514 orders_status_transition`), so a test only passes if the right rule refused it. Test 199 confirms nothing was left behind. As in Part A, the run uses up some ID numbers.

### Step B5. Keep a record

Export or screenshot the three verification results and the test result.

## Part C: inventory (V007)

Run this on your existing `nordiccart_oltp`, which already has V001 to V006. Do not run V001 to V006 again.

### Step C1. Confirm the starting point

Open a SQL Editor on **nordiccart_oltp** and run:

```sql
SELECT version, script_name, applied_at
  FROM meta.schema_migrations
 ORDER BY version;
```

| What you see | What to do |
|---|---|
| `001` to `006` | Continue with step C2. |
| `001` to `007` | V007 is already applied. Skip to step C3. |
| Fewer than six rows | Finish Part B first. |

### Step C2. Apply V007

File: `database/migrations/V007__inventory_balances_reservations_movements.sql`. Active database: **nordiccart_oltp**.

Paste the whole file and press **Execute** (`Ctrl+E`), exactly as before. Success looks like:

- no line starting with `ERROR` in the console;
- one result tab with an empty `assert_migration_can_run` value (normal);
- the query from step C1 now lists `001` to `007`.

Press **Refresh**. Schema `public` now has 18 tables: the 15 from Parts A and B plus `inventory_balances`, `inventory_reservations` and `inventory_movements`. V007 inserts no rows.

### Step C3. Run the V007 verification scripts

Folder: `sql/verification/`. Active database: **nordiccart_oltp**. All three are read-only.

| File | Result tabs | Pass condition |
|---|---|---|
| `08_verify_v007_tables_and_columns.sql` | 2: migrations and tables (10 rows), columns (30 rows) | `failed_checks` = 0 in both |
| `09_verify_v007_keys_and_constraints.sql` | 1 (24 rows) | `failed_checks` = 0 |
| `10_verify_v007_indexes_triggers_and_types.sql` | 1 (70 rows: 53 PASS, 17 INFO) | `failed_checks` = 0 |

The 17 INFO rows in script 10 are the enums and functions from V001 to V006, which scripts 04 and 07 already check.

Recommended: run scripts 01 to 07 again. All still have `failed_checks` = 0. Two things are expected and need no action:

- INFO rows for V007 objects in 01, 02 and 04;
- **2 WARN rows in script 07**: `trg_orders_inventory_check` on `orders` and `trg_payment_attempts_inventory_guard` on `payment_attempts`. V007 adds these two triggers to V006 tables, script 07 does not know them, and script 10 checks them.

### Step C4. Run the behaviour tests

Active database: **nordiccart_oltp**. Run both files.

| File | Rows | Pass condition |
|---|---|---|
| `tests/sql/test_v007_inventory.sql` | 105 | every `status` is PASS and `failed_tests` = 0 |
| `tests/sql/test_v006_orders_and_payments.sql` | 119 | every `status` is PASS and `failed_tests` = 0 |

Use the V006 test file that is in the folder now (its header says revision 3). From V007 an order can only be `stock_reserved` with a reservation for every line, so revision 3 reserves, releases and fulfils through the V007 functions when it finds the inventory tables. An older copy (revision 2) fails from test 57 onward after V007; that is the new rule working, not a fault.

The V007 tests list test products at test locations, receive, transfer, adjust and write off stock, reserve orders, try to oversell, pay, fulfil, release, expire, retry every step, and check that `on_hand` and `reserved` always equal the ledger. Every rejection is checked by SQLSTATE and rule name (for example `23514 ck_inventory_balances_no_oversell`). Test 143 creates a temporary table with a trigger inside the test block to show that the stock CHECKs hold even when the quantity guard is bypassed; it is rolled back with everything else. Test 199 confirms nothing was left behind. As before, the run uses up some ID numbers.

### Step C5 (optional). Run the concurrency test

The SQL tests run in one session. `tests/python/test_v007_concurrency.py` opens several sessions at once to prove the parts one session cannot: racing for the last units, no deadlocks, duplicate retries, a session killed half-way, lost updates and serialization retries.

It creates its **own** database `nordiccart_concurrency_test` on the same server, applies V001 to V007 to it, runs 11 scenarios and drops it. It never connects to `nordiccart_oltp`; it refuses any other database name.

Needs Python 3.9 or later and a role that can create databases (`postgres`). In PowerShell:

```powershell
cd "<project folder>"
python -m pip install -r tests\python\requirements.txt
$env:PGHOST = "localhost"; $env:PGPORT = "5432"; $env:PGUSER = "postgres"
$env:PGPASSWORD = Read-Host "PostgreSQL password"
python tests\python\test_v007_concurrency.py
```

Pass condition: the last line reads `Scenarios: 11  PASS: 11  FAIL: 0`. It takes about 10 seconds. The password stays in that PowerShell window only; close the window afterwards.

### Step C6. Keep a record

Export or screenshot the three V007 verification results, both test results and, if you ran it, the concurrency output.

## Troubleshooting

Whenever a script fails, first run this in the same SQL Editor, so the next script starts cleanly:

```sql
ROLLBACK;
```

(If nothing was open, PostgreSQL only replies with a warning; that is fine.)

| Message in the console | Cause | What to do |
|---|---|---|
| `unterminated dollar-quoted string` | Valentina split the file at a semicolon inside a function body. Nothing was committed. | Run `ROLLBACK;`, then run that file with psql (next section). Continue with the next file in Valentina. |
| `current transaction is aborted, commands ignored until end of transaction block` | An earlier statement in the same file failed; every later statement is skipped. | Scroll up to the **first** ERROR; that is the real cause. Run `ROLLBACK;`, fix it, run the file again. |
| `Migration V00X is already applied` | The file ran successfully before. | Nothing to do. Run `ROLLBACK;` and go to the next file. |
| `Migration V00X requires V00Y, which is not applied yet` | Files were run out of order. | Run `ROLLBACK;`, run V00Y, then V00X. |
| `Connected to database postgres, expected nordiccart_oltp` | Wrong active database. | Run `ROLLBACK;`, switch the Active Database to `nordiccart_oltp`, run again. |
| `function meta.assert_migration_can_run(...) does not exist` | V001 has not been applied to this database (or the wrong database is active). | Run `ROLLBACK;`, check the active database, apply V001 first. |
| `NordicCart requires PostgreSQL 13 or later` | Server too old. | Upgrade PostgreSQL. |
| `Database nordiccart_oltp must use UTF8 encoding` | Database was created without step 2. | Recreate the database with step 2 (see "Starting over"). |
| `permission denied to create extension "btree_gist"` or `permission denied for schema public` | The login role is not the database owner or superuser. | Connect as `postgres` (or the owner of `nordiccart_oltp`) and run the file again. |
| `could not open extension control file ... btree_gist.control` | PostgreSQL contrib modules are missing. | Windows installers include them. On Linux install the `postgresql-contrib` package for your version. |
| `database "nordiccart_oltp" already exists` (step 2) | Created earlier. | Continue with step 3. |
| `CREATE DATABASE cannot run inside a transaction block` (step 2) | The client wrapped the statement in a transaction. | Make sure only statement 1 is in the editor. If it still fails, run it with psql: `psql -U postgres -d postgres -c "CREATE DATABASE nordiccart_oltp WITH ENCODING 'UTF8' TEMPLATE template0;"` |
| Verification 01 shows WARN for the session time zone | The SQL Editor connection was opened before step 2, or step 2's second statement was skipped. | Open a new SQL Editor, or run step 2's statement 2. It is advisory only. |
| Verification shows `relation "meta.schema_migrations" does not exist` | V001 is not applied, or the wrong database is active. | Check the Active Database, then apply V001. |
| A verification row shows FAIL | An object is missing or differs from the design. | Read `expected` vs `actual` on that row. If the migrations ran without error this should not happen; send me the row. |
| A test row shows FAIL | A rule did not behave as designed. | The `test_name` and `expected` name the rule involved. Run the matching verification scripts (03 and 04 for Part A, 06 and 07 for V006) to see whether it exists; send me the row. |
| V006 or V007: `relation "orders" already exists`, `type "order_status" already exists`, `relation "inventory_balances" already exists` or similar | Objects with these names were created outside the migrations. Nothing from that file was kept. | Run `ROLLBACK;`. Check `meta.schema_migrations`: if the version is missing, the objects came from elsewhere; tell me before changing anything. |
| A test row is FAIL with `actual` showing a different rule than `expected` | The step was refused, but by another rule than the one under test. | Usually a sign of a changed object. Run the verification scripts for that migration (05 to 07 for V006, 08 to 10 for V007) and send me the FAIL rows. |
| Script 07 shows 2 WARN rows after V007 (`trg_orders_inventory_check`, `trg_payment_attempts_inventory_guard`) | Expected: V007 adds these triggers to V006 tables. | Nothing to do. Script 10 checks them. |
| V006 test rows FAIL from test 57 onward after V007 | An old copy of `test_v006_orders_and_payments.sql` (revision 2) was used. | Use the file in the folder now (revision 3 in its header). |
| V007 test 143 FAIL with `actual` = `23514 inventory_balances_quantities_by_ledger` | The connection is not allowed to create the temporary trigger table, or the guard was changed. | Send me the row. |
| Python: `nordiccart_concurrency_test already exists` | An earlier run was interrupted before it could drop its database. | Run `python tests\python\test_v007_concurrency.py --recreate`. |
| Python: `No module named 'psycopg'` | The requirements are not installed. | Run `python -m pip install -r tests\python\requirements.txt`. |
| Python: `permission denied to create database` or `password authentication failed` | Wrong role or password in the PowerShell variables. | Use `postgres` (or a role with CREATEDB) and set `$env:PGPASSWORD` again. |
| A test row about TRUNCATE (129) shows `00000` | The TRUNCATE guard is missing, and the rest of the run then works on emptied tables. Everything is still rolled back at the end. | Run 07 to see which trigger is missing; send me the FAIL rows. |
| Several test rows FAIL one after another | An earlier test succeeded when it should have failed and left its rows in place for the rest of the run (they are still rolled back at the end). | Fix the first FAIL row; the rest usually follow from it. |
| Scripts 01 to 04 show INFO rows after V006 or V007 | Expected: they list objects from later migrations as INFO. | Nothing to do. |

## Fallback: running a file with psql

`psql` ships with PostgreSQL. It never splits function bodies incorrectly, and `ON_ERROR_STOP` makes it stop at the first error. In PowerShell (replace `<version>` with the folder name under `C:\Program Files\PostgreSQL\`, for example `16`):

```powershell
cd "<project folder>"
$psql = "C:\Program Files\PostgreSQL\<version>\bin\psql.exe"

& $psql -h localhost -p 5432 -U postgres -d nordiccart_oltp -v ON_ERROR_STOP=1 -f "database\migrations\V001__migration_history_and_extensions.sql"
```

Use the same command with any other file name from the tables above, for example `database\migrations\V007__inventory_balances_reservations_movements.sql`. If PostgreSQL runs somewhere else (Docker, another machine), change `-h` and `-p`. psql asks for the password; nothing is stored in the scripts.

## Starting over (development only)

Never needed after a failed migration, because a failed file leaves nothing behind. If you still want a clean database, this permanently deletes `nordiccart_oltp` and everything in it. Close every SQL Editor connected to it, open one on **postgres**, run:

```sql
DROP DATABASE nordiccart_oltp;
```

Then repeat from step 2.

## What comes next

Phase 3 has started: the master data generator and its VS Code and Valentina steps are in `docs/phase3_master_data_generator_guide.md`. V008 (roles, column privileges including `SECURITY DEFINER` on the two balance-maintenance functions, session settings) follows before the full-scale simulation. See `database/README.md` for the full list.
