# Phase 3, step 5, stage S3: V008 database security (sandbox test report)

Date: 2026-11-18.

Design: `docs/phase3_combined_activity_design.md`, revision 2 (approved), section 4.1 (limitation L3, stage S3).

Scope: S3 only.
- V008 is implemented and tested in a disposable PostgreSQL 18.4 sandbox.
- **V008 was not applied to the real `nordiccart_oltp` database, and S3 did not contact the real database at all.**
- Migrations V001–V007 are unchanged. No V009 exists or was run. `config/.env` is unchanged (last modified 2026-11-17). No pilot record was changed.

## 1. Result

| Item | Result |
|---|---|
| V008 migration | Implemented, as one transaction with a self-check:<br>- 7 roles, ownership, 3 SECURITY DEFINER functions with a fixed search path;<br>- least privilege on the database, schemas, tables, columns and functions;<br>- role timeouts and connection limits;<br>- nothing for PUBLIC |
| Privilege matrix | `docs/phase3_v008_security_privilege_matrix.md`. The automated test compares all **360 entries** (8 grantees × 19 tables with every column, 24 functions, 2 schemas, the database): **all equal** |
| V008 database tests (`test_v008_security_db.py`) | **75 passed, 0 failed** |
| SQL behaviour suites on V008 | Foundation **44/44**, V006 **119/119**, V007 **105/105**, V008 (new) **87/87**; 0 failed |
| Concurrency (14 scenarios) | V001–V007 as superuser **14/14**; V001–V008 with every session as `nordiccart_loader` **14/14** |
| Full Python suite (all opt-in database tests on) | **423 passed, 0 failed, 0 skipped** (292.7 s). That is 348 before S3 plus 75 new |
| Verification scripts after V008 | 01–15 and 17 as `nordiccart_analyst`: **789 rows, 670 PASS, 116 INFO, 0 FAIL, 3 WARN** (the 3 WARN existed before V008). Script 17: **27 PASS, 4 INFO, 0 FAIL** |
| Real-state rehearsal | On a sandbox copy whose business content is identical to the real database (S1 checksums), starting with no NordicCart role on the server:<br>- V008 applied in 139–252 ms;<br>- business content unchanged;<br>- all 4 pilot command lines `--plan` as `nordiccart_loader`: 0 to write;<br>- R008 restored the exact catalog (282 entries) and dropped all 7 roles;<br>- V008 re-applied |
| Mutation checks (planted defects) | **10 of 10 caught.** 4 of them by V008's own self-check, which refuses to commit |
| Defects found and fixed during S3 | 2 that matter (section 7): role settings left over from an earlier run were not reset or checked; script 17 failed with an error on a server without the roles |
| Unresolved issues | Section 9 |

## 2. Files

| File | Status | Content |
|---|---|---|
| `database/migrations/V008__security_roles_and_privileges.sql` | new | The migration (required output 1) |
| `database/rollback/R008__undo_security_roles_and_privileges.sql` | new | In-place rollback, one transaction. Tested to restore the exact pre-V008 catalog |
| `database/security/V008_reapply_database_privileges.sql` | new | Re-applies the database-level privileges after a restore or template copy. Idempotent, writes no rows |
| `sql/verification/17_verify_v008_security.sql` | new | Read-only security verification, 31 rows. Runs with or without the roles |
| `tests/sql/test_v008_roles.sql` | new | 87 SQL tests (all rolled back) |
| `tests/python/test_v008_security_db.py` | new | 75 database tests, opt-in, own databases, disposable server only |
| `tests/python/test_v007_concurrency.py` | changed | Options `--through 8` and `--role`. Without them it behaves exactly as before |
| `docs/phase3_v008_security_privilege_matrix.md` | new | Required output 2 |
| `docs/phase3_v008_deployment_and_rollback_guide.md` | new | Required output 6 |
| `docs/phase2_database_implementation_status.md` | changed | S3 recorded (required output 5) |
| `tests/README.md`, `sql/README.md`, `database/README.md` | changed | New files listed |

V001–V007 SHA-256 are unchanged; the first 16 hex digits equal the values recorded in the transactions pilot execution report:

| Migration | SHA-256 (first 16 hex) |
|---|---|
| V001 | `D3349651A5CAF2CF` |
| V002 | `14BBCD1F4EC19447` |
| V003 | `ED76162656C04E92` |
| V004 | `88D0A52D422405BB` |
| V005 | `02320CF0FB4507AF` |
| V006 | `A8225D72CF6B4151` |
| V007 | `DCD79FF76BABDF36` |

## 3. Environment and method

**Sandbox**
- Disposable PostgreSQL **18.4** cluster (initdb) on port **55432**, trust authentication, `postgres` superuser.
- Separate from the real server on port 5432.
- Python 3.14.7 with psycopg 3 (`.venv`).

**Real-state source: `nordiccart_oltp_s1compat`**
- A restore of the real pre-replenishment backup, with the replenishment pilot replayed (stage S1).
- Its business checksums equal the real database's:

| Data | Rows | md5 |
|---|---|---|
| orders | 20 | `5f52439d…` |
| order_items | 26 | `18e8a9b6…` |
| payment_attempts | 21 | `5e20680e…` |
| inventory_reservations | 23 | `6073d4d6…` |
| inventory_movements | 326 | `3619b464…` |
| inventory_balances | 320 | `5b049040…` |

change_log has 1,537 rows; the last movement is 2024-12-02 12:46:04 UTC.

**Test databases.** The Python tests build their own templates with the existing loaders:
- V001–V007 + master data + opening inventory + 100 transaction units + 17 replenishment units, as `postgres`;
- the same + V008;
- V001–V008 with no rows.

**Logins.** Every "as role X" test logs in as that role, with a real session whose `session_user` is X. SET ROLE from a superuser is not used for the security conclusions, because PostgreSQL checks SET ROLE against the session user.

**Roles are cluster-wide.** For the rehearsal (section 5), the sandbox's earlier S3 scratch databases and all `nordiccart_*` roles were removed first, so V008 met the same situation as on the real server: no NordicCart role.

## 4. Test results

### 4.1 Full Python suite

Command, from the repository root with the sandbox PG variables:

```
NORDICCART_DB_TESTS=1 python -m pytest tests/python
```

Result: **423 passed, 0 failed, 0 skipped in 292.7 s.**

- **Before S3 (end of S2):** 348 passed, in the same sandbox.
- **Added in S3:** the 75 V008 tests.
- **Every earlier suite still passes unchanged.** These are:
  - master data, opening inventory, transactions, replenishment, pilot scope and demand (unit and calibration);
  - their database tests, which run as `postgres` on V001–V007 databases.

Re-run after the final V008 and script 17 changes.

### 4.2 V008 database tests: 75 passed, 0 failed

| Area | Tests | What is proven |
|---|---|---|
| Migration | 3 | Every business row and change_log row unchanged; `applied_by` postgres. A second run and a missing V007 are refused. A failing self-check (a stray SECURITY DEFINER function planted first) rolls back everything: the catalog snapshot is identical and there is no V008 row |
| Roles | 1 | LOGIN and NOLOGIN as designed; no elevated attribute; memberships INHERIT TRUE, SET FALSE; nobody in owner; exact settings and connection limits |
| Ownership and definer | 1 | Every table, sequence, type and function of public and meta, and schema meta, owned by `nordiccart_owner`; exactly 3 SECURITY DEFINER functions with `search_path=pg_catalog, public, pg_temp` |
| Complete matrix | 1 | 360 expected = actual entries |
| Script 17 | 2 | After V008: 31/27/4/0. Before V008: FAILs. Without the roles (renamed in a rolled-back transaction): 7 PASS, 4 INFO, 20 FAIL, no error |
| Copy or restore | 1 | `CREATE DATABASE … TEMPLATE` loses the database ACL: PUBLIC can connect and create temp tables again, and script 17 check 301 FAILs. The reapply script fixes it |
| Pilot loaders as loader | 1 | On an empty V008 database, as `nordiccart_loader`:<br>- master data 250 rows; opening inventory 320/288/1,460;<br>- 100 transaction units (2 out_of_stock cancels, 1 expiry); 17 replenishment units (111 change_log rows);<br>- reruns write 0; plans verified;<br>- **business content and change_log identical to the superuser load** |
| Scripts as analyst | 15 | Scripts 01–15 have no FAIL. Exact counts:<br>- 11: 43/33/10/0;<br>- 12: 24/17/7/0;<br>- 13: 52/45/7/0;<br>- 14: 44/37/7/0;<br>- 15: 43/35/8/0 |
| Customer and stock activity as loader | 1 | S1's valid later activity, written as the loader:<br>- 4 orders: reserve, pay, fulfil, one left open;<br>- a purchase receipt; a transfer out and in.<br>Both pilots verify; scripts 13, 14 and 15 pass |
| Command lines as loader | 2 | `run_transactions` and `run_replenishment`, `--plan` and `--apply --yes` (nothing to write, verified). The log names the loader |
| Scheduler | 1 | statement_timeout 5min; inserts a receipt; refused a quantity UPDATE (42501); a 4th connection is refused |
| Analyst | 1 | Reads; a write gives 25006 (read-only default). After switching read-only off, the same write gives 42501. Function EXECUTE gives 42501 |
| Extractor | 1 | Reads change_log (1,537 rows); purges a row (rolled back); inserts a checkpoint. Cannot UPDATE or TRUNCATE change_log; cannot read orders. The SQL suite (tests 100–105) adds moving the checkpoint (UPDATE) |
| Loader refused | 34 | One test per statement, each 42501. List in the matrix document, section 6 |
| Session control | 1 | The loader cannot terminate another role's session |
| Hostile search path | 1 | The loader sets `search_path = nc_trap, pg_temp, public`, where `nc_trap` holds look-alike `inventory_balances` and `change_log` tables it may write. A receipt still adds 4 units to the real balance and 2 rows to the real change_log; the trap tables stay empty. A `pg_temp` function cannot be created at all: refused test "create temp function" |
| Guards bind the owner | 1 | As `nordiccart_owner`:<br>- quantity UPDATE: 23514;<br>- DELETE orders: 23514;<br>- TRUNCATE movements: 23000;<br>- UPDATE change_log: 23000;<br>- UPDATE order lines: 23514 |
| Known limit | 1 | A role can `ALTER ROLE <self> SET statement_timeout`. Documented, then restored |
| Rollback | 2 | R008 gives an identical catalog snapshot, and V008 re-applies. R008 refuses when V008 is absent |
| SQL suites on V008 | 4 | Foundation 44, V006 119, V007 105, V008 87; 0 failed each |
| Concurrency on V008 | 1 | The 14 scenarios with `--through 8 --role nordiccart_loader` (subprocess) |

### 4.3 SQL suite `tests/sql/test_v008_roles.sql`: 87 tests, 0 failed

| Group | Tests |
|---|---|
| Catalog | 3 |
| Setup | 1 |
| Loader allowed | 19: receipt, transfer out and in, reorder point, product and customer updates, an order end to end (U1, U2, payment, U5), reserve, release and cancel, the change_log count, reads |
| Loader refused | 39 (42501) |
| Scheduler | 2 |
| Analyst | 4 |
| Reader | 1 |
| Extractor | 6 |
| PUBLIC | 4 (through a role with no grants) |
| search_path | 3 |
| Owner guards | 4 |
| Safety | 1 |

Run as `postgres` on a V008 database. Every role switch uses `set_config('role', …)`. Everything is rolled back, including the test role.

### 4.4 Concurrency

| Run | Command | Result |
|---|---|---|
| V001–V007, superuser (unchanged behaviour) | `test_v007_concurrency.py --recreate` | **14 PASS, 0 FAIL** (12.3 s), exit 0. Sessions run as `postgres` |
| V001–V008, all sessions as `nordiccart_loader` | `--recreate --through 8 --role nordiccart_loader` | **14 PASS, 0 FAIL** (12.8 s), exit 0. Fixtures and scenario sessions run as `nordiccart_loader` |

The scenario results are identical in both runs:
- the 10-session race reserves exactly 5;
- the balance CHECK stops an oversell (23514);
- no deadlocks (6/6 blocked rounds, 40/40 free race);
- U5 issues stock once;
- the sweeper and capture work in both orders;
- a killed session leaves no trace;
- 20 concurrent receipts lose no update;
- a 40001 conflict succeeds on retry;
- duplicate receipts and arrivals are stored once (23505);
- the transfer-versus-U2 race has one winner, and a transfer cannot take reserved units;
- the ledger invariants hold (13 balances, 0 inconsistent).

### 4.5 Verification scripts before and after V008 (rehearsal database)

| Scripts | Before V008 (as postgres) | After V008 (as `nordiccart_analyst`) |
|---|---|---|
| 01–10 | 551 rows: 476 PASS, 72 INFO, 0 FAIL, 3 WARN | 552 rows: 476 PASS, 73 INFO, 0 FAIL, 3 WARN (script 01: +1 INFO row, "V008 applied") |
| 11 | 43: 33 PASS, 10 INFO | identical |
| 12 | 24: 17 PASS, 7 INFO | identical |
| 13 | 52: 45 PASS, 7 INFO | identical |
| 14 | 44: 37 PASS, 7 INFO | identical |
| 15 | 43: 35 PASS, 8 INFO | identical |
| 17 | 31: 7 PASS, 4 INFO, 20 FAIL (no roles on the server), or 16 PASS, 11 FAIL (roles exist elsewhere on the server) | **31: 27 PASS, 4 INFO, 0 FAIL** |

The 3 WARN rows are unrelated to V008 and were already present:
- script 01: session time zone is Europe/Berlin (the Windows client), not UTC;
- script 07: the 2 known V007 triggers on V006 tables.

### 4.6 How the nine requirements are covered

| # | Requirement | Evidence |
|---|---|---|
| 1 | Owner, writer, loader, scheduler, reader, analyst, extractor | V008 section 1; tests "roles"; script 17 checks 101–109 |
| 2 | Least privilege: tables, schemas, sequences, functions | Matrix test (360 entries); script 17 checks 301–411; PUBLIC tests; no sequence grants needed (identity columns) |
| 3 | Column-level UPDATE restrictions | Matrix (UPDATE per column); refused UPDATEs of quantities, order total, lines, ledger, payment amount, registration, VAT; script 17 checks 402–404 |
| 4 | SECURITY DEFINER with secure ownership and search path | Ownership test; definer test; hostile search path test; script 17 checks 201–203; mutations M1, M2 |
| 5 | Role-specific timeouts | Settings test; scheduler and analyst sessions show their values; self-check; mutation M8; known limit documented |
| 6 | Privilege-matrix tests | `test_the_complete_privilege_matrix` |
| 7 | Compatibility as loader: transactions, reservations, fulfilment, receipts, transfers | Loader load identical to the superuser load; S1 later activity as loader; command lines as loader; SQL tests 20–38; concurrency as loader |
| 8 | Regression: pilot loaders and SQL verification scripts | Pilot loaders and `--plan` as loader; scripts 01–15 and 17 as analyst; SQL suites 44/119/105/87; full Python suite |
| 9 | Security: escalation, EXECUTE, unsafe search path, protected records | 34 + 39 refused statements, including SET ROLE, session_replication_role, trigger functions, migration guard, DDL, server files and roles; terminate test; hostile search path; owner guards |

## 5. Real-state rehearsal (deployment guide, sections 1–5 and 7)

Script `s3_rehearsal.py` (scratchpad), psql 18.4 with `ON_ERROR_STOP=1`, exactly as the guide.

### 5.1 Rollback rehearsal

Copy `nordiccart_oltp_s3_rollback` of the real-state source. No NordicCart role at the start.

1. V008: exit 0, 252 ms. 7 roles created. Script 17: 27 PASS, 4 INFO, 0 FAIL.
2. R008: exit 0, 95 ms. NOTICEs: all 7 roles dropped.
3. Checks after R008:
   - the normalized catalog snapshot (282 rows: owners, ACLs, SECURITY DEFINER, settings, default privileges) is **identical** to the pre-V008 snapshot;
   - the business content is identical;
   - migrations are 001–007.
4. Script 17: 7 PASS, 4 INFO, 20 FAIL, as expected without roles.
5. V008 again: 139 ms, script 17 27/4/0. R008 again: 90 ms. Copy dropped; no role left.

### 5.2 Deployment rehearsal

Copy `nordiccart_oltp_s3` of the real-state source.

**Preflight (guide section 1):**
- migrations 001–007;
- server 18.4;
- 0 NordicCart roles;
- database owner and object owner `postgres`;
- 0 other sessions;
- no other login roles.

The content equals the real database (S1 checksums).

**Apply.** V008 via psql: exit 0, **156 ms**. Output ends with `INSERT 0 1` and `COMMIT`.

**Content.** All 6 business checksums and change_log (1,537) are identical before and after. The only difference is the migrations count, 7 → 8 (V008's own row).

**Verification.** Scripts as in 4.5: as `postgres` 194 PASS, 43 INFO, 0 FAIL for 11–15 and 17; as `nordiccart_analyst` 01–15 and 17 with 0 FAIL.

**Pilot command lines as `nordiccart_loader`** (`--plan`, separate env file, no PG variables in the environment):

| Command line | Exit | Result |
|---|---|---|
| `run_master_data_seed.py` | 0 | 0 rows to insert |
| `run_opening_inventory.py` | 0 | 0 to insert; the known R0 reorder point drift is allowed |
| `run_transactions.py` | 0 | 20/20 orders, 100/100 units of work; later activity 21 movements, +26 units; 0 conflicts, 0 blocking |
| `run_replenishment.py` | 0 | 17/17 units of work; 0 conflicts, 0 blocking |

Content unchanged afterwards.

## 6. Mutation checks: 10 of 10 caught

How the run worked:
- each defect was planted in a temporary copy of V008 or R008; project files were not changed;
- the V008 suite (concurrency excluded) ran against each one, using the final V008 with the 74 tests that existed before the script 17 fix;
- the script copy tests ("copy or restore") also fail wherever script 17 fails.

| Mutation | Caught by |
|---|---|
| M1 `fn_capture_change` not SECURITY DEFINER | **V008 self-check** (migration refused) |
| M2 definer search path without `pg_temp` | **V008 self-check** |
| M3 PUBLIC keeps CONNECT and TEMP | **V008 self-check** |
| M4 writer may UPDATE `orders.order_total` | Matrix; script 17; refused "update order snapshot"; SQL suite |
| M5 loader membership `SET TRUE` | Roles test; script 17; refused "become the writer group" |
| M6 writer without EXECUTE on the commit-time checks | Matrix; script 17; pilot loaders as loader; later activity as loader (the deferred check fails); SQL suite |
| M7 reader may INSERT | Matrix; script 17; analyst test; SQL suite |
| M8 loader without `lock_timeout` | **V008 self-check**, after the fix in section 7. Before the fix it was NOT caught, because the cluster role still held the value from an earlier run |
| M9 extractor cannot purge | Matrix; script 17; extractor test; SQL suite |
| M10 R008 forgets PUBLIC EXECUTE | Rollback catalog test; SQL suite |

## 7. Defects found and fixed during S3

| # | Found by | Defect | Fix |
|---|---|---|---|
| 1 | Mutation M8 | V008 adopted existing roles but neither reset their earlier settings nor checked them. A missing `ALTER ROLE … SET` went unnoticed when a previous run had left the value | V008 runs `ALTER ROLE … RESET ALL` for the 7 roles before setting them. The self-check verifies LOGIN, INHERIT, connection limits and the exact settings of every role |
| 2 | Real-state rehearsal | Script 17 raised `role "nordiccart_owner" does not exist` on a server without the roles: the real server before V008, or after R008 | Role-dependent checks now report FAIL ("role … missing") instead. New regression test (roles renamed inside a rolled-back transaction) |
| 3 | Development | Smaller test-harness issues, not V008 behaviour:<br>- statement snapshot in SQL tests;<br>- SET ROLE checked against the session user, so those tests moved to real logins;<br>- `text || "char"` in script 17;<br>- fixture name collision;<br>- multi-result-set reading | Fixed before the final runs |

## 8. Deviations from design section 4.1

Details are in the matrix document, section 7.
1. The writer may SELECT 5 metadata columns of change_log (not `record_key` or `row_data`). The loaders count the change_log rows they write.
2. The definer search path is `pg_catalog, public, pg_temp`. `pg_temp` is explicitly last; when omitted, PostgreSQL searches it first for relations.
3. `statement_timeout` is set for all four login roles. The analyst has idle 5 min, no lock_timeout and read-only by default.
4. Explicit connection limits: loader 30, scheduler 3, analyst 10, extractor 3; the NOLOGIN roles 0.
5. No grants on `meta.activity_*` yet: V009 creates those tables.
6. V008 itself runs as a superuser. "SET ROLE nordiccart_owner" for migrations applies from V009.
7. V008 requires PostgreSQL 16+, for membership options. The real server runs 18.4.

## 9. Unresolved issues

1. **Timeouts are not enforceable against the role itself.** Any role can `SET` its own timeouts and `ALTER ROLE <self> SET` its defaults. This is PostgreSQL behaviour, and a test documents it. Treat the timeouts as protection against mistakes, not as a security control.
2. **Database-level privileges are not carried by `pg_restore` (into an existing database) or by template copies.** Mitigation: `database/security/V008_reapply_database_privileges.sql` after every restore or copy, and script 17 check 301 detects it. Restores of post-V008 backups also need the roles to exist first (guide section 6).
3. **Revoking PUBLIC's CONNECT on `nordiccart_oltp` affects any other non-superuser login role.** The rehearsal copy had none; the real server must be checked with preflight 1.6 before deployment.
4. **The extractor's DELETE on change_log is not limited to consumed rows.** A Phase 4 purge function (SECURITY DEFINER, bounded by the checkpoint snapshot) should replace the table privilege.
5. **The login roles have no password.**
   - Passwords must be set at deployment with `\password` (guide 5.1), never in the repository.
   - The sandbox tests used trust authentication, so password login itself was not tested. Only the privileges were.
6. **`config/.env` still connects as `postgres`.** Until you switch it to `nordiccart_loader` (guide 5.3, your decision), the loaders run without V008's protections.
7. **`pg_hba.conf` was not reviewed.** It decides which roles may log in, from where and with which method. It is server configuration outside the repository. Check it before using the new login roles.
8. **The analyst's read-only default is a convenience.** The control is that the reader group has no write privilege, which is tested.
9. **`postgres` remains the only administrative login.** Migrations and Valentina Studio keep using it. No separate migration role was created (design: from V009, `SET ROLE nordiccart_owner`).

None of these blocks V008. Items 3, 5 and 6 need action at deployment time.

## 10. Status and next step

- S3 is implemented and sandbox-tested.
- **Stopped as instructed:** V008 has not been applied to the real database.
- Deployment needs your explicit authorization. It follows `docs/phase3_v008_deployment_and_rollback_guide.md`:
  1. read-only preflight;
  2. pre-V008 backup with SHA-256;
  3. V008 via psql;
  4. script 17 and scripts 01–15;
  5. passwords;
  6. `--plan` as the loader.
- Rollback: R008 (recommended, tested) or the pre-V008 backup.
