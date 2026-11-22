# NordicCart V008: security privilege matrix

Phase 3, step 5, stage S3. Migration `database/migrations/V008__security_roles_and_privileges.sql`.
Design source: `docs/phase3_combined_activity_design.md`, revision 2, section 4.1 (limitation L3).
Status 2026-11-18: implemented and tested in the disposable PostgreSQL 18.4 sandbox. **Not applied to the real `nordiccart_oltp` database.**

This document is the reference for what each role may do after V008. Three independent mechanisms enforce it:

| Mechanism | Where | What it checks |
|---|---|---|
| V008 self-check | last `DO` block of V008 | Re-reads the catalog before `COMMIT`. Any deviation raises an exception and the whole migration rolls back. |
| Automated matrix test | `tests/python/test_v008_security_db.py::test_privilege_matrix_is_complete` | Compares 360 entries (8 grantees × 19 tables with every column, 24 functions, 2 schemas, the database) with the expected matrix built from this document. |
| Verification script | `sql/verification/17_verify_v008_security.sql` | Read-only. 27 PASS checks and 4 INFO rows after V008; runs as `nordiccart_analyst`. |

Anything not listed as allowed in this document is denied.

---

## 1. Roles

Roles are cluster-wide. V008 creates each one only if it is missing, then re-asserts its attributes and settings. No role is SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS.

| Role | Kind | Member of | Connection limit | Purpose |
|---|---|---|---|---|
| `nordiccart_owner` | NOLOGIN | none (and nobody is a member of it) | 0 | Owns every object of schemas `public` and `meta`, including the three SECURITY DEFINER functions |
| `nordiccart_writer` | NOLOGIN group | none | 0 | Holds the loaders' privileges |
| `nordiccart_reader` | NOLOGIN group | none | 0 | Holds read-only privileges |
| `nordiccart_loader` | LOGIN | `nordiccart_writer` | 30 | Manual, approved loads: pilot loaders, scale-up, historical catch-up |
| `nordiccart_scheduler` | LOGIN | `nordiccart_writer` | 3 | Automated hourly ticks (stage S10) |
| `nordiccart_analyst` | LOGIN | `nordiccart_reader` | 10 | Verification scripts, Power BI, ad-hoc analysis |
| `nordiccart_extractor` | LOGIN | none | 3 | Phase 4: change_log extraction, checkpoints and purge |

**Memberships** are granted `WITH INHERIT TRUE, SET FALSE` (PostgreSQL 16 syntax):
- the login role inherits the group's privileges;
- it cannot `SET ROLE` to the group, so `current_user` is always the login role. Audit columns and `pg_stat_activity` show who did what.

**Passwords.** The four LOGIN roles are created without a password. An administrator sets them at deployment, outside the repository (see the deployment guide).

### 1.1 Role settings

Applied when the role logs in. V008 first runs `ALTER ROLE … RESET ALL` on all seven roles, so the result is exactly this table.

| Role | lock_timeout | idle_in_transaction_session_timeout | statement_timeout | default_transaction_read_only |
|---|---|---|---|---|
| `nordiccart_loader` | 10s | 60s | 30min | off |
| `nordiccart_scheduler` | 10s | 60s | 5min | off |
| `nordiccart_analyst` | (server default) | 5min | 15min | **on** |
| `nordiccart_extractor` | 10s | 60s | 30min | off |
| owner, writer, reader | none | none | none | none |

These settings guard against mistakes such as runaway queries, forgotten open transactions and lock queues. **They are not a security control.** Any role can change its own session settings with `SET`, and its own defaults with `ALTER ROLE <self> SET`. This is a PostgreSQL behaviour, and a test documents it. The analyst's read-only default is a convenience. The real control is that the reader group has no write privilege.

---

## 2. Database and schemas

| Privilege | owner | writer, loader, scheduler | reader, analyst | extractor | PUBLIC |
|---|---|---|---|---|---|
| Database: CONNECT | no (NOLOGIN; migrations connect as `postgres`) | **yes** (granted to the writer group) | **yes** (granted to the reader group) | **yes** | **no** (revoked) |
| Database: TEMPORARY | no | no | no | no | **no** (revoked) |
| Database: CREATE | no | no | no | no | no |
| Schema `public`: USAGE | yes | yes | yes | yes | **no** (revoked) |
| Schema `public`: CREATE | yes | no | no | no | no |
| Schema `meta`: USAGE | yes (owner) | yes | yes | no | no |
| Schema `meta`: CREATE | yes (owner) | no | no | no | no |

V008 revokes everything PUBLIC held on the database (CONNECT, TEMPORARY), on schemas `public` and `meta`, on all tables and sequences, and EXECUTE on every NordicCart function. A role outside the NordicCart roles cannot connect to `nordiccart_oltp` unless it is a superuser or the database owner. An administrator can grant CONNECT explicitly.

---

## 3. Tables

Legend:
- **S** = SELECT
- **I** = INSERT
- **U(cols)** = UPDATE of the listed columns only
- **D** = DELETE
- **all** = owner (implicit)
- **—** = nothing

TRUNCATE, TRIGGER and REFERENCES are held by no application role.

| Table | owner | writer, loader, scheduler | reader, analyst | extractor | PUBLIC |
|---|---|---|---|---|---|
| `public.currencies` | all | S, I | S | — | — |
| `public.countries` | all | S, I | S | — | — |
| `public.exchange_rates` | all | S, I | S | — | — |
| `public.product_categories` | all | S, I | S | — | — |
| `public.products` | all | S, I, U(discontinued_on, is_active) | S | — | — |
| `public.product_prices` | all | S, I, U(valid_to) | S | — | — |
| `public.customers` | all | S, I, U(email, phone, address_line1, postal_code, city, is_active) | S | — | — |
| `public.stock_locations` | all | S, I | S | — | — |
| `public.stores` | all | S, I | S | — | — |
| `public.warehouses` | all | S, I | S | — | — |
| `public.orders` | all | S, I, U(status, cancellation_reason, confirmed_at, fulfilled_at, cancelled_at) | S | — | — |
| `public.order_items` | all | S, I | S | — | — |
| `public.payment_attempts` | all | S, I, U(status, authorized_at, captured_at, failed_at, failure_reason, provider_reference) | S | — | — |
| `public.inventory_balances` | all | S, I, U(reorder_point) | S | — | — |
| `public.inventory_reservations` | all | S, I, U(status, closed_at) | S | — | — |
| `public.inventory_movements` | all | S, I | S | — | — |
| `public.change_log` | all | S(change_id, table_name, operation, transaction_id, changed_at) | S | S, D | — |
| `public.change_log_checkpoints` | all | — | S | S, I, U(last_snapshot) | — |
| `meta.schema_migrations` | all | S | S | — | — |

### 3.1 How the column privileges combine with the existing guards

Column-level UPDATE only decides which columns a statement may name. The V006 and V007 guard triggers still decide which values and transitions are allowed, and they bind the owner as well. For example:

| Column privilege | Still enforced by V006/V007 (unchanged) |
|---|---|
| `orders.status` and event timestamps | Allowed status transitions; timestamps written once, in time order; final orders immutable; `cancellation_reason` exactly when cancelled |
| `payment_attempts.status` and outcome columns | Allowed payment transitions; outcome fields only by their own transition; `provider_reference` written once; capture only while stock is reserved |
| `inventory_reservations.status`, `closed_at` | active → consumed, released or expired only; closed rows immutable |
| `inventory_balances.reorder_point` | New balances start at zero; id, location, product and created_at never change; the quantity columns change only from the ledger triggers |
| `updated_at` (every table that has one) | Set by `fn_set_updated_at`; no role may name it in an UPDATE |

What the writer can never change:
- stock quantities. `quantity_on_hand` and `quantity_reserved` have no UPDATE privilege. Only the SECURITY DEFINER triggers change them, from movements and reservations;
- identities, keys, foreign keys, amounts and totals, order lines, movements, exchange rates, migration history.

Nobody except the owner may DELETE or TRUNCATE, apart from the extractor's DELETE on `change_log`. The owner is still stopped by the V006/V007 guards, which raise `orders_no_delete` and the `inventory_no_truncate` rules.

### 3.2 Why the writer reads change_log metadata

The pilot loaders count the change_log rows each unit of work writes. They report this count and compare it with the plan. These counts need SELECT on `change_id`, `table_name`, `operation`, `transaction_id` and `changed_at`. The writer cannot read `record_key` or `row_data` (the row images), and cannot write the table. This is a deviation from the design's "no direct access to change_log" (section 7).

### 3.3 Sequences

Every NordicCart table uses `GENERATED ALWAYS AS IDENTITY`. INSERT on such a table needs no privilege on its sequence, so no role holds a sequence privilege, and PUBLIC holds none either. Identity sequences belong to their table's owner.

---

## 4. Functions

All 24 NordicCart functions are owned by `nordiccart_owner`. PUBLIC has EXECUTE on none of them.

| Function | Kind | SECURITY | EXECUTE granted to |
|---|---|---|---|
| `fn_reserve_order_stock(bigint, timestamptz, timestamptz)` | unit of work U2 | INVOKER | writer |
| `fn_close_order_reservations(bigint, reservation_status, timestamptz)` | unit of work U3 release / U4 expire | INVOKER | writer |
| `fn_fulfil_order_stock(bigint, timestamptz)` | unit of work U5 | INVOKER | writer |
| `fn_lock_order_balances(bigint)` | called by the three units of work (with the caller's rights) | INVOKER | writer |
| `fn_assert_order_consistent(bigint)` | called by the deferred commit-time order check | INVOKER | writer |
| `fn_assert_order_inventory_consistent(bigint)` | called by the deferred commit-time inventory check | INVOKER | writer |
| `fn_inventory_apply_movement()` | trigger: movement → on-hand quantity | **DEFINER** | nobody |
| `fn_inventory_apply_reservation()` | trigger: reservation → reserved quantity | **DEFINER** | nobody |
| `fn_capture_change()` | trigger: row image → change_log | **DEFINER** | nobody |
| `fn_set_updated_at()` | trigger | INVOKER | nobody |
| `fn_reject_modification()` | trigger (insert-only tables) | INVOKER | nobody |
| `fn_check_stock_location_subtype()` | trigger | INVOKER | nobody |
| `fn_orders_guard()` | trigger | INVOKER | nobody |
| `fn_order_items_guard()` | trigger | INVOKER | nobody |
| `fn_payment_attempts_guard()` | trigger | INVOKER | nobody |
| `fn_reject_history_truncate()` | trigger | INVOKER | nobody |
| `fn_check_order_consistency()` | deferred trigger | INVOKER | nobody |
| `fn_inventory_balances_guard()` | trigger | INVOKER | nobody |
| `fn_inventory_reservations_guard()` | trigger | INVOKER | nobody |
| `fn_inventory_movements_guard()` | trigger | INVOKER | nobody |
| `fn_payment_attempts_inventory_guard()` | trigger | INVOKER | nobody |
| `fn_reject_inventory_truncate()` | trigger | INVOKER | nobody |
| `fn_check_order_inventory()` | deferred trigger | INVOKER | nobody |
| `meta.assert_migration_can_run(text, text)` | migration guard | INVOKER | nobody (migrations run as a superuser) |

Why trigger functions need no EXECUTE grant:
- PostgreSQL checks EXECUTE on a trigger function only at `CREATE TRIGGER`, not when the trigger fires.
- Functions that a trigger or a unit of work calls in turn are checked against the current user. That is why the writer needs EXECUTE on `fn_lock_order_balances` and the two `fn_assert_*` functions.
- A trigger function cannot be called directly in any case: PostgreSQL raises "trigger functions can only be called as triggers".

### 4.1 SECURITY DEFINER functions

| Function | Runs as | search_path | Writes |
|---|---|---|---|
| `fn_inventory_apply_movement()` | `nordiccart_owner` | `pg_catalog, public, pg_temp` | `inventory_balances.quantity_on_hand` |
| `fn_inventory_apply_reservation()` | `nordiccart_owner` | `pg_catalog, public, pg_temp` | `inventory_balances.quantity_reserved` |
| `fn_capture_change()` | `nordiccart_owner` | `pg_catalog, public, pg_temp` | INSERT into `change_log` |

The search path is fixed, with `pg_temp` last:
- the caller's `search_path` has no effect, so a schema, temporary table or function the caller creates cannot be picked up;
- application roles cannot create objects anyway: no CREATE on any schema and no TEMPORARY on the database;
- the function bodies already schema-qualify their tables (V002/V007).

Exactly these three functions are SECURITY DEFINER. The V008 self-check and script 17 (checks 202, 203) fail if any other function in `public` or `meta` is SECURITY DEFINER, or if any of the three loses its search path.

---

## 5. Default privileges (objects created later by `nordiccart_owner`)

| Object type | Schemas | Rule |
|---|---|---|
| Functions | all | No EXECUTE for PUBLIC |
| Tables | `public`, `meta` | SELECT for `nordiccart_reader` |

Writer and extractor privileges on new tables are granted explicitly by the migration that creates them (V009: purchase orders, `meta.activity_*`).

---

## 6. Explicitly refused (security tests)

Each item below is tested with a real login. The expected result is SQLSTATE `42501` (insufficient privilege) unless stated otherwise.

Loader: 34 statements (`REFUSED_FOR_THE_LOADER` in the Python tests, one test each; similar cases in `tests/sql/test_v008_roles.sql`, tests 40–80):
- UPDATE of on-hand or reserved quantity, order total, order lines, the movement ledger, payment amount, customer registration time, VAT rate;
- DELETE of orders, movements, customers; TRUNCATE of the ledger;
- forging a change_log row, reading `change_log.row_data`, purging change_log, writing `meta.schema_migrations`;
- CREATE TABLE, CREATE TEMP TABLE, `CREATE FUNCTION pg_temp.lower` (search path trap), CREATE SCHEMA, CREATE TRIGGER, CREATE ROLE;
- `ALTER TABLE … DISABLE TRIGGER ALL`, `ALTER TABLE … OWNER TO` itself, `DROP TABLE change_log`;
- `SET ROLE nordiccart_owner`, `SET ROLE nordiccart_writer`, `SET session_replication_role = replica`;
- calling `fn_capture_change()`, `fn_inventory_apply_reservation()`, `meta.assert_migration_can_run(…)`;
- `LOCK TABLE inventory_balances IN ACCESS EXCLUSIVE MODE`;
- `pg_read_file(…)`, `COPY … TO '<server file>'`.

The loader also cannot end another role's session (`pg_terminate_backend`).

Other roles:
- **Scheduler:** exactly the loader's privileges (matrix test). A direct quantity UPDATE is refused, and so is a 4th concurrent connection (connection limit 3).
- **Analyst:**
  - a write fails first with `25006` (read-only transaction);
  - after `SET default_transaction_read_only = off` the same write fails with `42501`.
- **Extractor:** cannot read business tables, and cannot UPDATE or TRUNCATE change_log. It has no USAGE on schema `meta`, as the matrix test verifies.
- **PUBLIC** (tested through a role with no grants):
  - cannot connect to the database;
  - cannot use the schemas;
  - cannot execute any NordicCart function.

Other checks:
- **Hostile search path.** A schema `nc_trap` holds look-alike functions and tables, and a temporary function `pg_temp.lower` is attempted (refused). The three SECURITY DEFINER triggers still write to the real tables and use `pg_catalog`.
- **Owner bound by guards.** `nordiccart_owner` (through `postgres`) still hits:
  - `inventory_balances_quantities_by_ledger` (23514);
  - `orders_no_delete` (23514);
  - `inventory_no_truncate` (23000);
  - the V002 insert-only guard on change_log (`fn_reject_modification`, 23000).

---

## 7. Deviations from design section 4.1

| # | Design | V008 | Reason |
|---|---|---|---|
| 1 | Writer: "no direct access to change_log" | SELECT on 5 metadata columns (no `record_key` or `row_data`), no write | The pilot loaders count the change_log rows they write; those counts are part of their verification |
| 2 | `SET search_path = pg_catalog, public` | `pg_catalog, public, pg_temp` | Puts `pg_temp` explicitly last. If it is left out, PostgreSQL searches it **first** for relations |
| 3 | Settings: `lock_timeout = '10s'`, `idle_in_transaction_session_timeout = '60s'` | Same for loader, scheduler, extractor. Analyst: idle 5min, no lock_timeout, read-only default. `statement_timeout` for all four login roles | The analyst needs longer idle time for interactive tools. statement_timeout was listed only for the scheduler and is added as a guard for all of them |
| 4 | "low connection limit" (scheduler) | Explicit limits: loader 30, scheduler 3, analyst 10, extractor 3, NOLOGIN roles 0 | Concrete values; owner, writer and reader cannot log in at all |
| 5 | Writer: INSERT/UPDATE on `meta.activity_*` | Not in V008 | Those tables are created by V009, which grants them |
| 6 | "Migrations run as `postgres` with `SET ROLE nordiccart_owner`" | V008 itself runs as a superuser (it creates roles and changes ownership) | Applies from V009 on |
| 7 | PostgreSQL 13+ (V001 check) | V008 requires PostgreSQL 16+ | Membership options `INHERIT` / `SET`. The real server runs 18.4 |
