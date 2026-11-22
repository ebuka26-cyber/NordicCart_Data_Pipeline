# S3: V008 deployment to the real database (report)

Date: 2026-11-18. Times are local (CET) unless marked UTC.

Authorization: your written authorization of 2026-11-18 to deploy the approved V008 to the real `nordiccart_oltp`, following `docs/phase3_v008_deployment_and_rollback_guide.md`.

What was not done:
- No V009; V001–V007 unchanged; no business data changed; S4 not started.
- `config/.env`, `pg_hba.conf` and passwords not touched.
- No password created or seen.

## 1. Result

| Item | Result |
|---|---|
| Pre-deployment preflight | Clean. Content fingerprint `b6317bea941fae97fe89d9a749d58843`, unchanged since readiness |
| Pre-V008 backup | `nordiccart_oltp_20261118_163550_pre_v008.dump`. SHA-256 matches; archive readable; still the current restore point, so no new backup was needed |
| Conflicting sessions | None. One idle `postgres` session (no transaction, no locks) was left open |
| **V008** | **Applied and committed.** psql exit code 0, `ON_ERROR_STOP=1`, as `postgres`, 375 ms. The self-check passed. Migration row `008` recorded at 2026-11-18 16:46:06 UTC |
| Verification scripts 01–15 and 17 | **789 rows: 671 PASS, 116 INFO, 0 FAIL**, 2 WARN (script 07's two known V007 triggers). Script 17: 27 PASS, 4 INFO, 0 FAIL |
| Business data | **Unchanged**: 20 orders, 326 movements, 1,467 units on hand, 1,537 change_log rows. 18 of 19 tables byte-identical; `meta.schema_migrations` gained only V008's row. ID counters unchanged |
| Roles and privileges | 7 roles as designed. **Privilege matrix 360 of 360 entries as designed** on the real database |
| Pilot loaders (unchanged `config/.env`, still `postgres`) | Both `--plan` checks: nothing to write, 0 conflicts; they see migrations 001–008 |
| Authentication | `pg_hba.conf` unchanged (scram-sha-256, localhost only).<br>- Right after deployment, none of the 4 login roles had a password, so none could log in (verified).<br>- You then set the `nordiccart_loader` password privately and stored it in `pgpass.conf`.<br>- The scheduler, analyst and extractor still have none |
| Restricted loader connection test | **Passed** (section 9). It connects as `nordiccart_loader`, a non-superuser, using `pgpass.conf` with no prompt and no password in any file I wrote. The allowed privileges are present and the forbidden ones absent. "Transactions: Check" and "Replenishment: Check" as the loader: **0 conflicts, nothing to write**. No record and no `config/.env` changed |
| Rollback | Not needed and not run. R008 and the pre-V008 backup remain available |

## 2. Pre-deployment checks (guide section 1, read-only)

All checks ran in one `REPEATABLE READ, READ ONLY` snapshot, as `postgres@localhost:5432/nordiccart_oltp`.

| Check | Result |
|---|---|
| Migrations | 001–007 |
| Server | PostgreSQL 18.4, not in recovery |
| NordicCart roles / other non-superuser login roles | 0 / none |
| Owners | `postgres` for everything (`public` schema: `pg_database_owner`) |
| Privileges | PostgreSQL defaults (PUBLIC: CONNECT and TEMP on the database, USAGE on `public`, EXECUTE on functions); no explicit ACLs; no SECURITY DEFINER |
| Prepared transactions / locks of other sessions | 0 / 0 |
| Content | 20 orders, 326 movements, 1,467 units (0 reserved), 1,537 change_log rows, 0 ledger breaks |
| Content fingerprint | `b6317bea941fae97fe89d9a749d58843`, identical to the readiness report: 19 of 19 tables equal |
| Backup | 260,854 bytes; SHA-256 `090AA80ABF2DA64708450D8FCC8600A5B41CA73CBFECCA4D3EA8296DD56A9D70` matches; `pg_restore --list` exit 0, 19 TABLE DATA entries |
| V008 file | SHA-256 `444F116FEFF9A37D9CC06BCCCC8A86A23C1AD382C96D103F43D1D5A14812D7A1`, the approved file |
| R008 file | SHA-256 `AD1607FFD20229B7FAF18248DAA45F85ED0F322EB3D9DAAA5E20372B94853C63`, the approved file |

**Sessions.** Re-checked just before applying: one other session on `nordiccart_oltp`, pid 43744 (`postgres` from `::1`, no application name).
- State `idle`, no open transaction, 0 locks.
- An idle session holds no lock that V008 needs, so it was **left open, not terminated**.
- The apply script would have terminated any session with an open transaction or a lock. None existed.

## 3. Migration execution (guide section 3)

```
psql -X -h localhost -p 5432 -U postgres -d nordiccart_oltp -v ON_ERROR_STOP=1 -f database/migrations/V008__security_roles_and_privileges.sql
```

The password was supplied from `config/.env` through the environment; it was never printed.

| Item | Value |
|---|---|
| Start | 2026-11-18 17:46:05 |
| Duration | 375 ms (psql start included) |
| Exit code | 0 |
| stderr | empty: no error, no warning, no notice |
| stdout | `BEGIN`; the guard `SELECT`; the roles block (`DO`); 7 role comments; 3 `GRANT ROLE` (memberships); 12 `ALTER ROLE` (settings); `ALTER SCHEMA`; the ownership block; 3 `ALTER FUNCTION` (SECURITY DEFINER) and their comments; the PUBLIC revocations; 19 `GRANT`; 2 `ALTER DEFAULT PRIVILEGES`; the self-check (`DO`); `INSERT 0 1`; `COMMIT` |

`meta.schema_migrations` after V008:

| Version | Script | Applied (UTC) | By |
|---|---|---|---|
| 001 | V001__migration_history_and_extensions.sql | 2026-11-16 00:45:02 | postgres |
| 002 | V002__change_capture_and_shared_trigger_functions.sql | 2026-11-16 01:09:14 | postgres |
| 003 | V003__reference_data_currencies_countries_exchange_rates.sql | 2026-11-16 01:10:54 | postgres |
| 004 | V004__catalogue_and_customers.sql | 2026-11-16 01:12:28 | postgres |
| 005 | V005__stock_locations_stores_warehouses.sql | 2026-11-16 01:13:28 | postgres |
| 006 | V006__orders_order_items_payment_attempts.sql | 2026-11-16 16:02:35 | postgres |
| 007 | V007__inventory_balances_reservations_movements.sql | 2026-11-16 23:57:11 | postgres |
| **008** | **V008__security_roles_and_privileges.sql** | **2026-11-18 16:46:06** | postgres |

## 4. Verification scripts after V008 (guide section 4, read-only)

| Script | Before V008 (readiness) | After V008 |
|---|---|---|
| 01 | 13 rows: 11 PASS, 2 INFO | 14 rows: 11 PASS, 3 INFO (new INFO row: V008 applied) |
| 02 | 115: 109 PASS, 6 INFO | same |
| 03 | 47: 47 PASS | same |
| 04 | 77: 53 PASS, 24 INFO | same |
| 05 | 62: 62 PASS | same |
| 06 | 39: 39 PASS | same |
| 07 | 64: 39 PASS, 23 INFO, 2 WARN | same (the 2 known V007 triggers) |
| 08 | 40: 40 PASS | same |
| 09 | 24: 24 PASS | same |
| 10 | 70: 53 PASS, 17 INFO | same |
| 11 | 43: 33 PASS, 10 INFO | same |
| 12 | 24: 17 PASS, 7 INFO | same |
| 13 | 52: 45 PASS, 7 INFO | same |
| 14 | 44: 37 PASS, 7 INFO | same |
| 15 | 43: 35 PASS, 8 INFO | same |
| 17 | 31: 7 PASS, 4 INFO, 20 FAIL (expected before V008) | **31: 27 PASS, 4 INFO, 0 FAIL** |
| **Total** | 788: 651 PASS, 115 INFO, 20 FAIL (all expected), 2 WARN | **789: 671 PASS, 116 INFO, 0 FAIL, 2 WARN** |

Result: **0 FAIL, so 0 unexpected FAIL.**

## 5. Database checksums: data unchanged (requirement 6)

Each table's exact md5 covers every row and every column, ids included, in UTC with C collation. Before = the snapshot just before V008; after = just after.

| Table | Rows before / after | md5 (first 12) | Result |
|---|---|---|---|
| public.orders | 20 / 20 | 67141de3df08 | identical |
| public.order_items | 26 / 26 | 9af8a5a7c0a8 | identical |
| public.payment_attempts | 21 / 21 | 8928de9af012 | identical |
| public.inventory_reservations | 23 / 23 | f12c9b7d25cc | identical |
| public.inventory_movements | 326 / 326 | ee235ba7d99f | identical |
| public.inventory_balances | 320 / 320 | 2b50c9c0e3de | identical |
| public.change_log | 1,537 / 1,537 | 2c3ec519b11a | identical |
| public.change_log_checkpoints | 0 / 0 | d41d8cd98f00 | identical |
| public.customers | 100 / 100 | 8488eeea6b5a | identical |
| public.products | 20 / 20 | 674e15e84a9e | identical |
| public.product_prices | 80 / 80 | f980748dd231 | identical |
| public.product_categories | 10 / 10 | 6052f901d0d5 | identical |
| public.countries / currencies / exchange_rates | 4 / 4 / 0 (unchanged) | d8c18dfeb61f / ca70e58a623e / d41d8cd98f00 | identical |
| public.stock_locations / stores / warehouses | 16 / 12 / 4 (unchanged) | d8c1221a0395 / 133855c17dc3 / 268d2c88bb81 | identical |
| meta.schema_migrations | 7 / **8** | c8ff68f6968e → changed | V008's own row, as designed |

Business facts after V008:

| Fact | Value |
|---|---|
| Orders | **20** (fulfilled 14, cancelled 4, payment_failed 2) |
| Inventory movements | **326**:<br>- opening_balance 288 (+1,460)<br>- purchase_receipt 5 (+26)<br>- sale 17 (−19)<br>- transfer_out 8 (−10)<br>- transfer_in 8 (+10) |
| Stock units | **1,467** on hand, 0 reserved; 0 ledger identity breaks |
| change_log | **1,537** rows (V008 wrote none) |
| Last business event | 2024-12-02 12:46:04 UTC |

The 15 identity sequences have the same positions as before, for example `orders` 52, `inventory_movements` 360, `change_log` 1755.

## 6. Roles and permissions on the real database (requirement 7)

### 6.1 Roles

| Role | Login | Superuser / createdb / createrole / replication / bypassrls | Inherit | Conn. limit | Password | Settings |
|---|---|---|---|---|---|---|
| nordiccart_owner | no | no / no / no / no / no | yes | 0 | none | — |
| nordiccart_writer | no | no | yes | 0 | none | — |
| nordiccart_reader | no | no | yes | 0 | none | — |
| nordiccart_loader | yes | no | yes | 30 | **none** | lock_timeout 10s, idle_in_transaction_session_timeout 60s, statement_timeout 30min |
| nordiccart_scheduler | yes | no | yes | 3 | none | lock_timeout 10s, idle 60s, statement_timeout 5min |
| nordiccart_analyst | yes | no | yes | 10 | none | statement_timeout 15min, idle 5min, default_transaction_read_only on |
| nordiccart_extractor | yes | no | yes | 3 | none | lock_timeout 10s, idle 60s, statement_timeout 30min |

**Memberships**, each INHERIT true, SET false, ADMIN false; nobody is a member of `nordiccart_owner`:
- `nordiccart_loader` → `nordiccart_writer`;
- `nordiccart_scheduler` → `nordiccart_writer`;
- `nordiccart_analyst` → `nordiccart_reader`.

**SECURITY DEFINER functions** (exactly three), owned by `nordiccart_owner`, each with `search_path=pg_catalog, public, pg_temp`:
- `fn_capture_change()`;
- `fn_inventory_apply_movement()`;
- `fn_inventory_apply_reservation()`.

**Ownership:** `nordiccart_owner` owns 19 tables, 15 sequences, 24 NordicCart functions, 8 enum types and schema `meta`. The 212 `btree_gist` extension functions stay with `postgres`.

**Database ACL:** `{postgres=CTc/postgres, nordiccart_writer=c/postgres, nordiccart_reader=c/postgres, nordiccart_extractor=c/postgres}`.
- PUBLIC has no CONNECT and no TEMP.
- PUBLIC has no USAGE on `public` or `meta`, and no EXECUTE on the 24 NordicCart functions.
- 3 default-privilege entries.

### 6.2 Complete privilege matrix

The automated matrix of `tests/python/test_v008_security_db.py` was run read-only against the real database: `has_table_privilege`, `has_column_privilege`, `has_function_privilege`, `has_schema_privilege` and `has_database_privilege` for 8 grantees (7 roles plus PUBLIC).

Coverage:
- 19 tables, with every column checked for SELECT and UPDATE;
- 23 functions plus `meta.assert_migration_can_run`;
- 2 schemas and the database.

Result: **360 entries expected, 360 equal, 0 different.** The real database therefore matches `docs/phase3_v008_security_privilege_matrix.md`, including:
- the writer's column-level UPDATE list;
- no UPDATE on quantities;
- no DELETE or TRUNCATE (except the extractor's change_log purge);
- the reader's SELECT on all tables.

Script 17 confirms the same independently: 27 PASS, 0 FAIL.

## 7. Regression: pilot loaders under the unchanged `config/.env`

`config/.env` still connects as `postgres`. Both checks ran read-only (`default_transaction_read_only=on`), with logs in `logs/predeploy_readonly_20261118/`.

| Command line | Exit | Result |
|---|---|---|
| `run_transactions.py --plan` | 0 | Connected as `postgres`, migrations 001–008. 20/20 orders, 100/100 units of work, 0 conflicts, 0 blocking, nothing to write |
| `run_replenishment.py --plan` | 0 | 17/17 units of work, 0 conflicts, 0 blocking, nothing to write |

## 8. Authentication status, and setting the loader password privately

**Status after V008.**
- `pg_hba.conf` is unchanged: scram-sha-256 for every rule, localhost (127.0.0.1, ::1) only. `password_encryption` = scram-sha-256; `log_statement` = none.
- None of the 4 login roles has a password, so none can log in.
- Verified: `psql -w -U nordiccart_loader -d nordiccart_oltp` was refused with `fe_sendauth: no password supplied`. The server demanded a password and none exists.
- No password file `%APPDATA%\postgresql\pgpass.conf` existed at that point. Only its existence was checked.
- **Update:** you then followed the instructions below. The loader password is set and stored in `pgpass.conf`; see section 9.

**Instructions.** Only `nordiccart_loader` gets a password now. Never type the password into a PowerShell command, a SQL editor, a document, `config/.env` or the chat.

1. **Generate** a random password of 24+ characters in your password manager and keep it there.
2. **Set it in psql** (PowerShell):
   ```
   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h localhost -p 5432 -U postgres -d nordiccart_oltp
   ```
   Enter the `postgres` password at the prompt (hidden). Then, at `nordiccart_oltp=#`:
   ```
   \password nordiccart_loader
   ```
   Paste or type the new password twice; it is hidden. psql computes the SCRAM-SHA-256 verifier on your machine and sends only that. Its history file records the command `\password nordiccart_loader`, not the password.

   Check, then quit:
   ```
   SELECT rolname FROM pg_authid WHERE rolname = 'nordiccart_loader' AND rolpassword LIKE 'SCRAM-SHA-256$%';
   \q
   ```
   The query must return 1 row.
3. **Store it for libpq, outside the project**, so the restricted connection works without a password in any project file. In PowerShell:
   ```
   New-Item -ItemType Directory -Force "$env:APPDATA\postgresql" | Out-Null
   notepad "$env:APPDATA\postgresql\pgpass.conf"
   ```
   - Notepad asks whether to create the file: **Yes**.
   - Type exactly one line, with your password at the end: `localhost:5432:nordiccart_oltp:nordiccart_loader:<password>`.
   - If the password contains `:` or `\`, write them as `\:` and `\\`.
   - Save with encoding **UTF-8** (Notepad's default), not "UTF-8 with BOM", and close Notepad.
   - Then restrict the file to your Windows account:
     ```
     icacls "$env:APPDATA\postgresql\pgpass.conf" /inheritance:r /grant:r "${env:USERNAME}:(R,W)"
     ```
4. **Tell me "password set"**, without the password. I will then run the tests of section 9. They read the password from that file through libpq, so I never see it.

Leave `nordiccart_scheduler`, `nordiccart_analyst` and `nordiccart_extractor` without passwords until they are needed. `config/.env` stays unchanged until you approve the switch separately.

## 9. Restricted loader connection test (2026-11-18 18:17, after you set the password)

**Setup**
- You set the `nordiccart_loader` password with `\password` and stored it in `%APPDATA%\postgresql\pgpass.conf`. You reported that prompt-free authentication through it works.
- The password was **not read, typed, logged or stored by me**. Only the file's existence was checked.
- Connection settings came from a file containing only four keys, `PGHOST=localhost`, `PGPORT=5432`, `PGDATABASE=nordiccart_oltp` and `PGUSER=nordiccart_loader`, with **no password**. The file is in the session scratchpad, outside the project.
- The shell had **no PG variables** (0), so libpq took the password from `pgpass.conf`.
- Every session was forced read-only (`PGOPTIONS=-c default_transaction_read_only=on`). No write was attempted.
- `config/.env` was not used and not changed: last modified 2026-11-17 03:02:07.

### 9.1 Connection and identity

Command: `psql -X -w` (`-w`: never prompt; it fails if no stored password applies).

| Item | Result |
|---|---|
| psql exit code | 0 |
| `current_user` / `session_user` | `nordiccart_loader` / `nordiccart_loader` |
| Database / port | `nordiccart_oltp` / 5432 |
| Superuser | no |
| Role settings in effect | lock_timeout 10s, statement_timeout 30min, idle_in_transaction_session_timeout 1min (= 60s) |

### 9.2 Effective privileges of the logged-in loader

All checked read-only with `has_*_privilege()` and `pg_has_role()` as the loader itself.

| Allowed, as designed | Result |
|---|---|
| Read orders / movements / count change_log rows | yes: 20 / 326 / 1,537 |
| INSERT inventory_movements | yes |
| UPDATE `inventory_balances.reorder_point` | yes |
| UPDATE `orders.status` | yes |
| EXECUTE `fn_reserve_order_stock` (U2) | yes |
| Inherits the writer group's privileges | yes |

| Refused, as designed | Result |
|---|---|
| UPDATE `inventory_balances.quantity_on_hand` | no |
| UPDATE `orders.order_total` | no |
| DELETE orders | no |
| TRUNCATE inventory_movements | no |
| INSERT change_log | no |
| Read `change_log.row_data` | no; an actual `SELECT row_data FROM change_log` returned `ERROR: permission denied for table change_log` |
| CREATE in schema `public` | no |
| Temporary tables | no |
| `SET ROLE nordiccart_writer` | no |
| Owner rights | no |

### 9.3 "Transactions: Check" and "Replenishment: Check" as `nordiccart_loader`

The same commands as the VS Code configurations (`--plan`, default `config/generator.toml`), with the connection file above via `--env-file`.

| Check | Exit | Connected as | Result |
|---|---|---|---|
| Transactions: Check (`run_transactions.py --plan`) | 0 | `nordiccart_loader`, PostgreSQL 18.4, migrations 001–008 | Master data and opening inventory present and unchanged. **20/20 orders, 100/100 units of work, to run: nothing**. Later activity: 21 movements (purchase_receipt 5, transfer_in 8, transfer_out 8), net +26 units. **Conflicts 0, blocking 0**. "0 orders and 0 units of work would be written, nothing was written." |
| Replenishment: Check (`run_replenishment.py --plan`) | 0 | `nordiccart_loader`, PostgreSQL 18.4, migrations 001–008 | Pilot transactions complete (100 units of work verified against their plan). **17/17 units of work, to run: nothing**. **Conflicts 0, blocking 0**. "0 units of work would be written, nothing was written." |

Logs: `logs/transactions_pilot_plan_20261118_181728.log` and `logs/replenishment_pilot_plan_20261118_181729.log`.
- Each records the settings source: "nordiccart_loader@localhost:5432/nordiccart_oltp (from …real_loader_nopassword.env [PGHOST, PGPORT, PGDATABASE, PGUSER] plus environment)".
- The only occurrence of the word "password" in either log is in that file name.

### 9.4 Nothing changed

A read-only snapshot after the tests, compared with the one right after deployment:
- **19 of 19 tables byte-identical**, including `meta.schema_migrations` with 8 rows;
- privileges identical;
- 20 orders, 326 movements, 1,467 units on hand, 1,537 change_log rows.

**Result: the restricted loader connection works and is restricted as designed. Both pilot checks return 0 conflicts and nothing to write. S3 deployment verification is complete.**

## 10. Unresolved problems and open items

No deployment problem occurred. These items remain open:
1. **Resolved:** the loader password is set (by you, privately), and the restricted connection test passed (section 9). The scheduler, analyst and extractor intentionally still have no password.
2. **`config/.env` still connects as `postgres`.** The loaders therefore do not yet run under V008's restrictions. Switching needs your separate approval (procedure in the readiness report, section 8, steps 5–8).
3. **Timeouts are not a security control.** A role can change its own timeouts (S3 report, issue 1).
4. **The new login roles could connect to the other 7 databases on this server** once they have passwords, because PUBLIC holds CONNECT there by default (readiness report, R3). Optional hardening is your decision.
5. **The extractor's DELETE on change_log is not limited to consumed rows.** A Phase 4 purge function will replace it.
6. **Restores of post-V008 backups** need the roles first; restoring into an existing database also needs the reapply script and `ALTER DATABASE … SET timezone = 'UTC'` (readiness report, section 6.3).
7. **Backups after the `config/.env` switch must run as `postgres` explicitly** (`pg_dump -U postgres`, which prompts).
8. **The idle client session (pid 43744) was left open.** It is unaffected: superuser.

## 11. Rollback availability (not used)

- **R008** (`database/rollback/R008__undo_security_roles_and_privileges.sql`): approved and unchanged. Tested on copies of the exact pre-V008 backup; it restores the catalog exactly.
- **Pre-V008 backup:** `database/backups/nordiccart_oltp_20261118_163550_pre_v008.dump`, SHA-256 above.
- Neither will be used without your approval.

Evidence is in the session scratchpad, not in the project:
- `dep_preflight_before.json` / `.txt`, `dep_preflight_after.json` / `.txt`;
- `dep_apply.py`, `dep_apply.txt`;
- `dep_scripts_after.txt`;
- `dep_verify_roles.py`, `dep_roles_after.txt`;
- loader test: `real_loader_nopassword.env`, `loader_check_run_transactions.py.txt`, `loader_check_run_replenishment.py.txt`, `loader_test_preflight.json`.

**Stopped after deployment and verification**, as instructed. The restricted loader verification (section 9) completes S3. `config/.env` is unchanged; switching it to the loader needs your separate approval.
