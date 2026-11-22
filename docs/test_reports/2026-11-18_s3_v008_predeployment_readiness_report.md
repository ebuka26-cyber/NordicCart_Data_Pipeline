# S3: V008 pre-deployment readiness report

Date: 2026-11-18. Times are local (CET) unless marked UTC.

Scope: V008 pre-deployment readiness only, after ChatGPT approved the V008 implementation and sandbox results.

What was not done:
- **V008 was not applied.** No role was created and no privilege changed on the real server.
- `config/.env`, passwords and the PostgreSQL authentication configuration were not modified.
- V009 was not touched; no V009 file exists.

How the real database was accessed:
- **Read-only only.** Every query session was forced read-only (`PGOPTIONS=-c default_transaction_read_only=on`, and every check ran in a `READ ONLY` transaction).
- The backup used `pg_dump`, which only reads.
- Credentials came from `config/.env` and were never printed.

## 1. Verdict

**Deployment is safe to proceed once you authorize it.** No preflight check found an unexpected database state, so there was no reason to stop.

| Area | Result |
|---|---|
| Database state (migrations, roles, owners, privileges, content) | Exactly as expected for the pre-V008 state. No blocker |
| Authentication (`pg_hba.conf`) | scram-sha-256 on every rule, localhost only. **The new login roles need passwords**; without one they cannot log in |
| Pre-V008 backup | Created, SHA-256 verified twice, archive readable |
| Full test restore in the sandbox | **All 19 tables byte-identical** to the real database; sequences, owners, privileges and database settings equal; every verification script gives the same result |
| V008 and R008 on the exact backup (sandbox) | Both applied cleanly. R008 restored the exact catalog |
| V008, R008, reapply script | Present and unchanged since the S3 delivery (SHA-256) |
| Loader switch procedure | Prepared (section 8). The password never enters the repository, a terminal command or a log |
| Git protection of backups | **Blocker found and resolved.** The dumps would have been committed. `.gitignore` now excludes them, verified with git (section 9) |

Conditions at deployment time (section 10):
1. Repeat the preflight and the content fingerprint. If the fingerprint differs, take a new backup first.
2. Close the idle client session.
3. Set the loader password right after V008.
4. The `config/.env` switch is a separate decision.

## 2. Preflight on the real database (deployment guide, section 1)

Connection `postgres@localhost:5432/nordiccart_oltp`, superuser, session read-only. One `REPEATABLE READ, READ ONLY` snapshot.

| Check | Expected | Actual | Status |
|---|---|---|---|
| 1.1 Migrations | 001–007, nothing later | 001, 002, 003, 004, 005, 006, 007 | OK |
| 1.2 Server | PostgreSQL 16 or later | 18.4, not in recovery | OK |
| 1.3 NordicCart roles | 0 | 0 | OK |
| Roles on the server | — | Only `postgres`: superuser, LOGIN, scram-sha-256 password, no role settings, no memberships | OK |
| 1.4 Database owner | postgres | postgres | OK |
| 1.4 Object owners (public, meta) | postgres only | 19 tables, 15 sequences, 236 functions (24 NordicCart + 212 of the extension `btree_gist`) and 8 enum types: all `postgres`. Schema `meta`: `postgres`. Schema `public`: `pg_database_owner` (PostgreSQL 15+ default). Extensions `btree_gist` 1.8 and `plpgsql`: `postgres` | OK |
| 1.5 Other sessions on `nordiccart_oltp` | none, or none that blocks | 1 idle client session (`postgres` from `::1`, no application name, probably Valentina Studio or VS Code). Holds **0 locks**; not in a transaction | OK, but close it at deployment (section 10) |
| Prepared transactions | 0 | 0 | OK |
| 1.6 Other non-superuser login roles | none | none | OK |
| Database ACL | PostgreSQL defaults | `datacl` NULL: PUBLIC has CONNECT and TEMPORARY, no CREATE | OK (V008 revokes these) |
| Schema privileges | defaults | PUBLIC: USAGE on `public` (no CREATE); nothing on `meta` | OK |
| Object ACLs | none set | 0 tables, 0 columns, 0 functions, 0 types with explicit ACLs; 0 default ACLs | OK |
| SECURITY DEFINER / function settings | none | none / none | OK |
| PUBLIC EXECUTE on functions | default (all) | 236 of 236 | OK (V008 revokes it on the 24 NordicCart functions; extension functions keep the default) |
| Triggers | 60 | 60 | OK |
| Database-level settings | — | `TimeZone=UTC` for all roles (from `00_create_database.sql`) | Note: see section 6.3 |
| Other databases on the server | — | 7, all owned by `postgres` with default ACLs: `Employees`, `France`, `Store`, `World`, `postgres`, `sql_practice`, `swedish_electricity` | Not touched by V008 (note R3, section 10) |

### 2.1 Content (requirement 6)

| Fact | Expected | Real database | Restored backup |
|---|---|---|---|
| Orders | 20 | **20** (fulfilled 14, cancelled 4, payment_failed 2) | 20 |
| Order lines / payment attempts / reservations | 26 / 21 / 23 | 26 / 21 / 23 | 26 / 21 / 23 |
| Inventory movements | 326 | **326**: opening_balance 288 (+1,460), purchase_receipt 5 (+26), sale 17 (−19), transfer_out 8 (−10), transfer_in 8 (+10) | 326, same breakdown |
| Stock units on hand | 1,467 | **1,467** (0 reserved, 320 balances) | 1,467 |
| change_log rows | 1,537 | **1,537** | 1,537 |
| Ledger identity (on hand = sum of movements) | 0 breaks | 0 | 0 |
| Last business event | 2024-12-02 12:46:04 UTC | 2024-12-02 12:46:04 UTC | same |

**Content fingerprint** `b6317bea941fae97fe89d9a749d58843`. It is an md5 over the per-table exact md5s of all 19 tables: every column including ids, UTC, C collation.
- It is identical for the real database before the backup, for the real database at the end of this work, and for the restored backup.
- The real database therefore did not change during readiness.

### 2.2 Verification scripts on the real database (read-only)

| Scripts | Result |
|---|---|
| 01–10 | 551 rows: 477 PASS, 72 INFO, 0 FAIL, 2 WARN. The WARNs are script 07's two known V007 triggers. Script 01 has no time zone WARN here: the server session is UTC |
| 11 | 43 rows: 33 PASS, 10 INFO |
| 12 | 24 rows: 17 PASS, 7 INFO |
| 13 | 52 rows: 45 PASS, 7 INFO |
| 14 | 44 rows: 37 PASS, 7 INFO |
| 15 | 43 rows: 35 PASS, 8 INFO |
| 17 (V008 security) | 31 rows: **7 PASS, 4 INFO, 20 FAIL**. This is the expected pre-V008 result: no NordicCart roles, default privileges |
| **Total** | 788 rows: 651 PASS, 115 INFO, 20 FAIL (all script 17, expected), 2 WARN |

### 2.3 Pilot loaders, read-only `--plan` on the real database

Logs went to `logs/predeploy_readonly_20261118/`.

| Command line | Exit | Result |
|---|---|---|
| `run_master_data_seed.py --plan` | 0 | 0 rows to insert |
| `run_opening_inventory.py --plan` | 0 | 0 listings and 0 opening balances to insert; 69 allowed reorder point drifts (the R0 revision) |
| `run_transactions.py --plan` | 0 | 20/20 orders, 100/100 units of work, 0 conflicts, 0 blocking |
| `run_replenishment.py --plan` | 0 | 17/17 units of work, 0 conflicts, 0 blocking |

## 3. Authentication: `pg_hba.conf` (inspected, not modified)

Read through the server's `pg_hba_file_rules` view.
- File: `C:/Program Files/PostgreSQL/18/data/pg_hba.conf`, last modified 2026-07-17 00:32.
- The server last loaded its configuration on 2026-11-11 04:43 (the server start), after that modification, so these rules are active.
- 0 parse errors; no ident mappings.

| # | Line | Type | Database | User | Address | Method |
|---|---|---|---|---|---|---|
| 1 | 113 | local | all | all | — | scram-sha-256 |
| 2 | 115 | host | all | all | 127.0.0.1/32 | scram-sha-256 |
| 3 | 117 | host | all | all | ::1/128 | scram-sha-256 |
| 4 | 120 | local | replication | all | — | scram-sha-256 |
| 5 | 121 | host | replication | all | 127.0.0.1/32 | scram-sha-256 |
| 6 | 122 | host | replication | all | ::1/128 | scram-sha-256 |

Related settings:

| Setting | Value | Meaning |
|---|---|---|
| `password_encryption` | scram-sha-256 | `\password` stores a SCRAM verifier |
| `listen_addresses` | `*` | The server listens on all interfaces, but no rule matches a remote address, so remote connections are refused |
| `ssl` | off | Acceptable while only localhost is allowed |
| `log_statement` | none | Password changes are not written to the server log |

Conclusions:
- **Every connection needs a password (SCRAM).** There is no `trust` or `peer` rule. A NordicCart login role without a password cannot log in at all.
- After V008, `nordiccart_loader`, `nordiccart_scheduler`, `nordiccart_analyst` and `nordiccart_extractor` therefore stay unusable until you set a password. That is the safe default.
- Set passwords only for the roles you will use: the loader now, the analyst when needed. Leave the scheduler (S10) and extractor (Phase 4) without one.
- The sandbox tests used `trust`. On the real server the privileges are the same; only the login method differs.

## 4. Backup before V008 (requirement 4)

| Item | Value |
|---|---|
| File | `database/backups/nordiccart_oltp_20261118_163550_pre_v008.dump` |
| Created | 2026-11-18 16:35:50 by `pg_dump` 18.4 (server 18.4). Custom format, gzip. 0.4 s; exit 0; no warnings |
| Size | 260,854 bytes |
| **SHA-256** | **`090AA80ABF2DA64708450D8FCC8600A5B41CA73CBFECCA4D3EA8296DD56A9D70`** (Python hashlib and PowerShell Get-FileHash agree) |
| Archive | `pg_restore --list` exit 0; 383 TOC entries |
| Archive contents | Data for all 19 tables. Also 19 tables, 15 sequences with their positions, 24 functions, 8 types, 60 triggers, 40 constraints, 26 foreign keys, 38 indexes, schema `meta`, extension `btree_gist`, 112 comments. The `DATABASE` and `DATABASE PROPERTIES` entries are restored with `--create` |
| Consistency | `pg_dump` takes one snapshot. The content fingerprint before the dump, after the dump and of the restored copy is identical |

Earlier backups stay in place: `…_012230_pre_pilot_transactions.dump` and `…_032247_pre_replenishment.dump`.

## 5. Full test restore in the disposable sandbox (requirement 5)

**Sandbox:** PostgreSQL 18.4 on port 55432, separate from the real server.

**Preparation:**
- The sandbox's own older `nordiccart_oltp`, the S3 rehearsal database and every `nordiccart_*` role were dropped first, so the sandbox matched the real server: no NordicCart role.
- Command: `pg_restore --create --exit-on-error -d postgres <backup>`, exit 0, **2.2 s**.

**Restored database:** owner `postgres`, UTF8, `English_United States.1252` collation and ctype (same as the real database), database setting `TimeZone=UTC` restored.

| Comparison (real vs restored) | Result |
|---|---|
| Exact md5 of every row of all 19 tables (all columns, ids included) | **19 of 19 identical** (table below) |
| Identity sequence positions (15) | identical (for example `orders` 52, `inventory_movements` 360, `change_log` 1755) |
| Migrations, content facts, order statuses, movement types, ledger identity | identical |
| Owners (database, schemas, tables, sequences, functions, types, extensions) | identical |
| Privileges (database, schemas, object ACLs, SECURITY DEFINER, PUBLIC EXECUTE) | identical |
| Database settings | identical (`TimeZone=UTC`) |
| Verification scripts 01–15 and 17 | identical: 788 rows, 651 PASS, 115 INFO, 20 FAIL (script 17, expected), 2 WARN |

| Table | Rows | md5 (first 12) |
|---|---|---|
| meta.schema_migrations | 7 | c8ff68f6968e |
| public.change_log | 1,537 | 2c3ec519b11a |
| public.change_log_checkpoints | 0 | d41d8cd98f00 |
| public.countries | 4 | d8c18dfeb61f |
| public.currencies | 4 | ca70e58a623e |
| public.customers | 100 | 8488eeea6b5a |
| public.exchange_rates | 0 | d41d8cd98f00 |
| public.inventory_balances | 320 | 2b50c9c0e3de |
| public.inventory_movements | 326 | ee235ba7d99f |
| public.inventory_reservations | 23 | f12c9b7d25cc |
| public.order_items | 26 | 9af8a5a7c0a8 |
| public.orders | 20 | 67141de3df08 |
| public.payment_attempts | 21 | 8928de9af012 |
| public.product_categories | 10 | 6052f901d0d5 |
| public.product_prices | 80 | f980748dd231 |
| public.products | 20 | 674e15e84a9e |
| public.stock_locations | 16 | d8c1221a0395 |
| public.stores | 12 | 133855c17dc3 |
| public.warehouses | 4 | 268d2c88bb81 |

### 5.1 V008 and R008 on copies of this exact backup (sandbox only)

Both runs used psql with `ON_ERROR_STOP=1`, as in the guide, starting with no NordicCart role on the server.

**Rollback dry run:**
1. V008: exit 0, 208 ms. Script 17: 27 PASS, 4 INFO, 0 FAIL.
2. R008: exit 0, 116 ms, all 7 roles dropped.
3. The catalog snapshot (282 entries) is **identical** to before V008; the business content is identical; migrations are 001–007. Script 17 is back to 7/4/20.
4. V008 again: 147 ms, 27/4/0. R008 again: 196 ms.

**Deployment dry run:**
- Preflight identical to section 2.
- V008: exit 0, **152 ms**, output ending `INSERT 0 1`, `COMMIT`.
- Business checksums and change_log (1,537) unchanged. The only difference is V008's own migration row (7 → 8).
- Scripts 01–15 and 17 as `nordiccart_analyst`: 789 rows, 670 PASS, 116 INFO, **0 FAIL**, 3 WARN (the extra WARN is the sandbox client's time zone; template copies do not carry the database's `TimeZone` setting).
- All four pilot command lines `--plan` as `nordiccart_loader`: exit 0, nothing to write, 0 conflicts.

## 6. Notes for the deployment and rollback guide

The approved guide is unchanged. These points add to it.

### 6.1 Section 5.2 (smoke test as the loader)

Use the password file of section 8 below instead of a `PGPASSWORD` line in an env file. The test env file then contains no secret.

### 6.2 After the `config/.env` switch, administrative work must name `postgres` explicitly

This covers backups, V009 and R008.
- `pg_dump` as `nordiccart_loader` fails: it cannot read `change_log.row_data` or the checkpoints.
- `pg_dump` as the analyst also fails: it has no sequence privileges.
- Command: `pg_dump -h localhost -p 5432 -U postgres -d nordiccart_oltp -Fc -f <file>`. psql and pg_dump prompt for the password.

### 6.3 Restores, extending guide section 6

- `pg_restore --create` (into a server without that database) restores the database-level `TimeZone=UTC` setting and the database ACL.
- `pg_restore` into an existing database, or a template copy, carries neither. After such a restore:
  - run `ALTER DATABASE <name> SET timezone = 'UTC';` as `postgres`;
  - for a post-V008 backup, also run `database/security/V008_reapply_database_privileges.sql`.

## 7. V008 migration and rollback files (requirement 7)

Present and unchanged. The SHA-256 of each file equals the value recorded at the S3 delivery, and every last-modified time is before that delivery.

| File | Bytes | Last modified | SHA-256 |
|---|---|---|---|
| `database/migrations/V008__security_roles_and_privileges.sql` | 27,101 | 2026-11-18 15:09:51 | `444F116FEFF9A37D9CC06BCCCC8A86A23C1AD382C96D103F43D1D5A14812D7A1` |
| `database/rollback/R008__undo_security_roles_and_privileges.sql` | 5,390 | 2026-11-18 15:14:08 | `AD1607FFD20229B7FAF18248DAA45F85ED0F322EB3D9DAAA5E20372B94853C63` |
| `database/security/V008_reapply_database_privileges.sql` | 1,707 | 2026-11-18 14:46:37 | `EAFE7635C5B9C528967210D407C7D17CCB42CB6A511A5DF6AF212669258F2E50` |
| `sql/verification/17_verify_v008_security.sql` | 18,617 | 2026-11-18 15:18:35 | `609583D7FF6B32D5FFB60550AD52F1581E181DF7D1EA0CD371A4EB64E8E3E6E6` |

V001–V007 still match the hashes recorded earlier (first 16 hex digits):

| Migration | SHA-256 (first 16 hex) |
|---|---|
| V001 | `D3349651A5CAF2CF` |
| V002 | `14BBCD1F4EC19447` |
| V003 | `ED76162656C04E92` |
| V004 | `88D0A52D422405BB` |
| V005 | `02320CF0FB4507AF` |
| V006 | `A8225D72CF6B4151` |
| V007 | `DCD79FF76BABDF36` |

`database/migrations/` holds V001–V008 only; there is no V009 file anywhere in the project.

## 8. Switching the application to the restricted loader account (requirement 8)

These are all your actions, after V008 is deployed and verified (script 17: 27 PASS, 4 INFO, 0 FAIL).

**Principles:**
- The password exists only in your password manager, as a SCRAM verifier on the server, and in one per-user password file outside the repository.
- It is never typed into a terminal command: PowerShell keeps a plain-text history.
- It is never typed into a SQL editor, a document, a chat or `config/.env`.

1. **Generate** a long random password (24+ characters) in your password manager and keep it there.
2. **Set it on the server** in psql as `postgres`. psql prompts twice without echo, computes the SCRAM-SHA-256 verifier locally and sends only that (`password_encryption = scram-sha-256`, `log_statement = none`).
   ```
   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h localhost -p 5432 -U postgres -d nordiccart_oltp
   \password nordiccart_loader
   SELECT rolname FROM pg_authid WHERE rolname = 'nordiccart_loader' AND rolpassword LIKE 'SCRAM-SHA-256$%';   -- 1 row
   ```
   Do not use `ALTER ROLE … PASSWORD '…'` in Valentina Studio, because the plain text would sit in its query history.
3. **Store it for libpq**, outside the repository, in the standard per-user password file `%APPDATA%\postgresql\pgpass.conf`:
   - create the folder if it is missing;
   - open the file in Notepad: `notepad "$env:APPDATA\postgresql\pgpass.conf"`;
   - add the line `localhost:5432:nordiccart_oltp:nordiccart_loader:<password>` (escape any `:` or `\` in the password with `\`);
   - save, then restrict the file to your Windows account:
     ```
     icacls "$env:APPDATA\postgresql\pgpass.conf" /inheritance:r /grant:r "${env:USERNAME}:(R,W)"
     ```
4. **Test without touching `config/.env`:**
   1. Create `%USERPROFILE%\nordiccart_loader.env` with four lines and no password: `PGHOST=localhost`, `PGPORT=5432`, `PGDATABASE=nordiccart_oltp`, `PGUSER=nordiccart_loader`.
   2. Open a new terminal and check that `Get-ChildItem env:PG*` shows nothing.
   3. Run:
      ```
      & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h localhost -U nordiccart_loader -d nordiccart_oltp -w -c "SELECT current_user"
      .\.venv\Scripts\python.exe python\run_transactions.py --plan --env-file "$env:USERPROFILE\nordiccart_loader.env"
      .\.venv\Scripts\python.exe python\run_replenishment.py --plan --env-file "$env:USERPROFILE\nordiccart_loader.env"
      ```
   Expected:
   - psql (`-w`: never prompt) prints `nordiccart_loader`;
   - the logs say `connected to nordiccart_oltp as nordiccart_loader … migrations 001,…,008`;
   - 0 to write and 0 conflicts. The sandbox gave exactly this.
5. **Switch** (separate decision). Edit `config/.env` in an editor:
   - set `PGUSER=nordiccart_loader`;
   - **delete the `PGPASSWORD` line**. That line is the `postgres` password, and it would override the password file. Keep it in your password manager as the admin credential.
   - Do not save copies of `config/.env` in the project.
6. **Verify:**
   - run both `--plan` commands without `--env-file`; the logs name `nordiccart_loader`;
   - the VS Code Run configurations use `config/.env`, so they now run as the loader too.
7. **After the switch:**
   - Backups, migrations (V009) and R008 run as `postgres` explicitly, through `psql -U postgres` or `pg_dump -U postgres`, which prompt (section 6.2).
   - The database tests (pytest) must keep pointing at the sandbox with every PG variable set. Otherwise they would fill missing values from `config/.env`.
8. **Back-out.** Restore `PGUSER=postgres` and the `PGPASSWORD` line from your password manager. Do this **before** any R008: R008 drops the loader role, and the loader's open sessions would keep the role from being dropped.

## 9. Backups and GitHub (requirement 9)

**Finding.**
- The project folder is not a git repository: there is no `.git` here or in any parent folder, and git is not on PATH. GitHub Desktop 3.6.6 is installed.
- With the previous `.gitignore`, a git repository of this folder would **commit all three backup dumps**, as well as any roles export, `config/.env` copy or loader env file.
- Verified with GitHub Desktop's git in a throwaway repository mirroring all 302 project paths as empty files.

**Fix (the only project file changed apart from the report).** `.gitignore` now also ignores:

| Pattern | Covers |
|---|---|
| `database/backups/`, `*.dump`, `*.backup` | Backups, dumps and role exports |
| `*.env`, `.env.*` (except `.env.example`), `pgpass.conf`, `.pgpass` | Copies of connection settings and password files |

**Verification with git** (`git status --ignored` on the mirror, old vs new `.gitignore`):
- exactly the three `.dump` files change from "would commit" to ignored; nothing else in the project changes status;
- `config/.env` stays ignored;
- `config/.env.example` and `.vscode/launch.json` stay committable;
- a roles export in `database/backups/`, a `config/.env.bak` and a `nordiccart_loader.env` would also be ignored.

**Remaining advice:**
- Commit through GitHub Desktop, which honours `.gitignore`. A drag-and-drop upload on github.com does not.
- If the project already exists in a GitHub repository elsewhere, check its history for `.dump` files. An ignore rule does not remove files committed earlier.

## 10. Blockers, risks and conditions

**Blockers:**
- None in the database.
- One repository blocker (committable backups), resolved in section 9.

**Conditions at deployment (with your authorization):**
1. **Repeat the read-only preflight and the content fingerprint** (`b6317bea941fae97fe89d9a749d58843`) immediately before V008.
   - If anything was written after 16:35:50 the fingerprint differs: take a new backup first.
   - If it is equal, this backup is the pre-V008 restore point.
2. **Close the idle client session**, for example Valentina Studio. It holds no locks now. If it started a transaction during V008, V008's `lock_timeout` (10 s) would stop V008 safely: rollback, nothing changed.
3. Apply V008 with psql as `postgres` (guide section 3); about 0.15–0.25 s expected.
4. Verify:
   - script 17: 27 PASS, 4 INFO, 0 FAIL;
   - scripts 01–15 unchanged: 0 FAIL, script 01 +1 INFO;
   - change_log 1,537.
5. Set the loader password (section 8, step 2). The scheduler and extractor stay without one.
6. Loader smoke test without the switch (section 8, step 4). The `config/.env` switch is a separate decision.

**Residual risks (not blockers):**
- **R1. Timeouts are not a security control.** A role can change its own (S3 report, issue 1).
- **R2. Database ACL and `TimeZone` setting after restores.** They are lost when restoring into an existing database or making a template copy (section 6.3).
- **R3. The new login roles can connect to the 7 other databases on this server.** PUBLIC holds CONNECT on them by default (`Employees`, `France`, `Store`, `World`, `postgres`, `sql_practice`, `swedish_electricity`).
  - They cannot read tables there unless PUBLIC was granted SELECT. They can create temporary tables and run functions there.
  - Optional hardening, outside V008, your decision:
    - `pg_hba.conf` rules that limit the `nordiccart_*` users to `nordiccart_oltp`; or
    - `REVOKE CONNECT ON DATABASE … FROM PUBLIC` on the other databases.
- **R4. The extractor can delete any change_log row,** not just extracted ones (Phase 4 purge function).
- **R5. `ssl = off`.** Acceptable while `pg_hba.conf` admits only localhost. Revisit before any remote access.

## 11. Evidence

Read-only scripts and outputs are in the session scratchpad. They are not part of the project:
- preflight: `pd_preflight.py`, `pd_preflight_real.txt`, `pd_preflight_real_first.json`, `pd_preflight_real.json`;
- restored copy: `pd_preflight_nordiccart_oltp.json`;
- comparisons: `pd_compare.py`, `pd_sequences.py`, `pd_fingerprint.py`;
- scripts: `pd_run_scripts.py`, `pd_scripts_real.txt`, `pd_scripts_restored.txt`;
- backup: `pd_backup.py`, `pd_backup.txt`;
- dry runs: `pd_dryrun_rollback.txt`, `pd_dryrun_deploy.txt`;
- git checks: `gitignore_check/`, `gitignore_mirror/`.

Loader plan logs: `logs/predeploy_readonly_20261118/` (git-ignored).

The sandbox is stopped. It holds the restored backup (`nordiccart_oltp`) and the V008 dry-run copy (`nordiccart_oltp_s3`).

**Stopped as instructed.** Nothing has been deployed.
