# NordicCart V008: deployment and rollback guide

Phase 3, step 5, stage S3. Applies `database/migrations/V008__security_roles_and_privileges.sql` to the real `nordiccart_oltp` database.

**Status (2026-11-18): not applied to the real database.** V008 was implemented and tested in a disposable PostgreSQL 18.4 sandbox, including a full rehearsal on a copy with the same content as the real database. Each database step below needs your explicit authorization first.

Related documents:
- privilege matrix: `docs/phase3_v008_security_privilege_matrix.md`
- sandbox test report: `docs/test_reports/2026-11-18_phase3_step5_s3_v008_security_sandbox_test_report.md`

---

## 0. What V008 changes, and what it does not

| Changes | Does not change |
|---|---|
| Creates 7 cluster roles (`nordiccart_owner`, `_writer`, `_reader`, `_loader`, `_scheduler`, `_analyst`, `_extractor`), without passwords | Any table, column, constraint, index, trigger or function body of V001–V007 |
| Moves ownership of every object of `public` and `meta`, and of schema `meta`, from `postgres` to `nordiccart_owner` | Any data row. The only row V008 writes is its own row in `meta.schema_migrations` |
| Makes `fn_inventory_apply_movement`, `fn_inventory_apply_reservation` and `fn_capture_change` SECURITY DEFINER with a fixed search path | `postgres` (superuser): it keeps full access, so Valentina Studio as `postgres` works as before |
| Revokes PUBLIC's CONNECT and TEMP on `nordiccart_oltp`, USAGE on `public`, EXECUTE on NordicCart functions | Other databases on the server: V008 only touches `nordiccart_oltp`, plus the cluster-wide roles |
| Grants the privileges in the matrix; sets role timeouts and connection limits | `config/.env`: the loaders keep connecting as before until you switch them (section 5) |

V008 is **one transaction**. If any statement fails, or the self-check at the end finds a deviation from the matrix, nothing is committed. That includes the roles: CREATE ROLE is transactional.

Duration in the rehearsal on a copy of the real content: see section 3.3.

---

## 1. Read-only preflight (on the real database)

Run as `postgres` in psql or Valentina Studio, connected to `nordiccart_oltp`. These queries only read.

```sql
-- 1.1 Migrations: expect 001 to 007, nothing later
SELECT version, script_name FROM meta.schema_migrations ORDER BY version;

-- 1.2 PostgreSQL version: V008 needs 16 or later (the real server runs 18.4)
SHOW server_version;

-- 1.3 NordicCart roles: expect 0 rows
SELECT rolname, rolcanlogin, rolconfig FROM pg_roles WHERE rolname LIKE 'nordiccart%' ORDER BY 1;

-- 1.4 Owners: expect a single row, the database owner (postgres)
SELECT pg_get_userbyid(d.datdba) AS database_owner FROM pg_database AS d WHERE d.datname = current_database();
SELECT DISTINCT pg_get_userbyid(c.relowner) AS object_owner
  FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('public', 'meta');

-- 1.5 Who else uses this database: expect only your own session
SELECT usename, application_name, client_addr, state
  FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid();

-- 1.6 Other login roles that rely on PUBLIC's CONNECT (they lose access after V008)
SELECT rolname FROM pg_roles
 WHERE rolcanlogin AND NOT rolsuper AND rolname NOT LIKE 'pg\_%' AND rolname NOT LIKE 'nordiccart%';
```

Stop and review before going on if:

| Check | Stop condition | What it means |
|---|---|---|
| 1.1 | Shows anything other than 001–007 | V008's guard would refuse to run anyway |
| 1.3 | Returns rows | V008 would adopt those roles. It resets their attributes and settings, and its self-check fails on unexpected memberships. Decide first whether they are yours |
| 1.4 | Shows a different or second owner | The R008 rollback would hand ownership to the database owner, which would differ from today |
| 1.5 | Shows other sessions | Close them. V008 needs short exclusive locks to change owners and grants (lock_timeout 10 s) |
| 1.6 | Lists a role that must keep access to `nordiccart_oltp` | Grant it CONNECT explicitly after V008, or give it a NordicCart role |

Also confirm the current verified state, read-only:
- scripts 11–15 pass;
- `run_transactions.py --plan` and `run_replenishment.py --plan` show 0 to write;
- script 17 before V008 shows **7 PASS, 4 INFO, 20 FAIL**. That is expected: the roles do not exist yet.

In the rehearsal on a copy of the real content, the preflight returned:
- migrations 001–007;
- server 18.4;
- 0 NordicCart roles;
- database and object owner `postgres`;
- no other sessions;
- no other login roles.

---

## 2. Backups before V008

From the repository folder in PowerShell. Replace `<version>` with the folder under `C:\Program Files\PostgreSQL\`, for example `18`. psql asks for the password; nothing is stored.

```powershell
$bin = "C:\Program Files\PostgreSQL\<version>\bin"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$dump = "database\backups\nordiccart_oltp_${stamp}_pre_v008.dump"
& "$bin\pg_dump.exe" -h localhost -p 5432 -U postgres -d nordiccart_oltp -Fc -f $dump
& "$bin\pg_restore.exe" --list $dump | Measure-Object -Line      # archive readable
Get-FileHash $dump -Algorithm SHA256                                # record the hash
```

Optional: a copy of the cluster roles as they are before V008. It is only needed to rebuild the server elsewhere.

```powershell
& "$bin\pg_dumpall.exe" -h localhost -p 5432 -U postgres --roles-only --no-role-passwords -f "database\backups\roles_${stamp}_pre_v008.sql"
```

`--no-role-passwords` keeps password hashes out of the file, because `database/backups/` is not ignored by git. Never store a password in this repository.

---

## 3. Apply V008

### 3.1 Command (recommended: psql)

V008 contains `DO` blocks. psql never splits them wrongly, and with `ON_ERROR_STOP` it stops at the first error, which rolls back the whole transaction.

```powershell
& "$bin\psql.exe" -h localhost -p 5432 -U postgres -d nordiccart_oltp -v ON_ERROR_STOP=1 -f "database\migrations\V008__security_roles_and_privileges.sql"
```

Alternative: Valentina Studio as `postgres`, pasting the whole file and pressing **Execute**, as for V001–V007. If Valentina reports `unterminated dollar-quoted string`, it split the file. Nothing was committed: run `ROLLBACK;` and use the psql command above.

### 3.2 What success looks like

- The output ends with `INSERT 0 1` and `COMMIT`, with no `ERROR`.
- There may be NOTICEs, for example from the role loop.

| Failure | Meaning | Action |
|---|---|---|
| `V008 creates cluster roles ... run it as a superuser` | Not connected as `postgres` | Reconnect as `postgres` |
| `Migration V008 is already applied to database …` / `Migration V008 requires V007, which is not applied yet.` | Guard refused | Nothing changed. Re-check 1.1 |
| `V008 self-check: ...` | The catalog would differ from the matrix | Nothing changed. Report the full message; do not edit V008 on the real server |
| `canceling statement due to lock timeout` | Another session holds a lock | Nothing changed. Close the sessions found by 1.5 and run again |

### 3.3 Rehearsal timing

The rehearsal used a sandbox copy of the real content (PostgreSQL 18.4), starting from a server with no NordicCart role, as on yours. Timings, psql start included:

| Step | Time |
|---|---|
| V008, three runs | 139–252 ms |
| R008, two runs | 90–95 ms |

The locks V008 takes on the tables are held for that time only.

---

## 4. Verify (read-only)

1. **Script 17** as `postgres`, or later as `nordiccart_analyst`: `sql/verification/17_verify_v008_security.sql`. Expected: **31 rows: 27 PASS, 4 INFO, 0 FAIL**.
2. **Scripts 01–15** as `postgres`. Expected: the same results as before V008, because V008 changes no row. Script 01 gets one more INFO row, for V008 in the migration history. Rehearsal values:
   - 01–10: 552 rows, 476 PASS, 73 INFO, 0 FAIL, 3 WARN. The WARNs are script 01's client session time zone and script 07's 2 known V007 triggers. Before V008: 551 rows, 72 INFO;
   - 11: 33 PASS, 10 INFO;
   - 12: 17 PASS, 7 INFO;
   - 13: 45 PASS, 7 INFO;
   - 14: 37 PASS, 7 INFO;
   - 15: 35 PASS, 8 INFO.
3. **Business content unchanged.** `SELECT count(*) FROM change_log;` still returns **1,537**. In the rehearsal, the content checksums of orders, lines, payments, reservations, movements and balances were identical before and after V008, and equal to those of your real database (S1).

Do **not** run the SQL behaviour suites (`tests/sql/*.sql`) on the real database:
- they insert test rows inside a transaction they then roll back;
- the rollback removes the rows, but identity sequences do not roll back, so they would leave gaps in the real IDs.

They are sandbox-only. Script 17 and the `--plan` checks below are the real-database checks.

---

## 5. After V008: passwords and the loader connection (your actions)

### 5.1 Set passwords (outside the repository)

In psql as `postgres`:

```
\password nordiccart_loader
\password nordiccart_analyst
```

`\password` prompts twice and sends only the password hash, so it does not appear in logs or history.

Leave `nordiccart_scheduler` and `nordiccart_extractor` without a password until stage S10 and Phase 4. With password authentication (`scram-sha-256` in `pg_hba.conf`), a role without a password cannot log in.

### 5.2 Smoke test as the loader (read-only)

1. Create a file **outside the repository**, for example `%USERPROFILE%\nordiccart_loader.env`, containing:
   - `PGHOST=localhost`
   - `PGPORT=5432`
   - `PGDATABASE=nordiccart_oltp`
   - `PGUSER=nordiccart_loader`
   - `PGPASSWORD=<the loader password>`
2. Make sure no `PG*` variable is set in the shell, because environment variables take precedence over the file.
3. Run both checks with that file:

```powershell
.\.venv\Scripts\python.exe python\run_transactions.py --plan --env-file "$env:USERPROFILE\nordiccart_loader.env"
.\.venv\Scripts\python.exe python\run_replenishment.py --plan --env-file "$env:USERPROFILE\nordiccart_loader.env"
```

Expected: the same result as before V008. Transactions show 20/20 orders and 100/100 units of work, 0 to write; replenishment shows 17/17, 0 to write, 0 conflicts.

### 5.3 Switch `config/.env` to the loader (separate decision)

- When you decide, edit `PGUSER` and `PGPASSWORD` in `config/.env` to `nordiccart_loader` and its password.
- Keep a `postgres` connection, for example in Valentina Studio, for migrations (V009 onwards) and administration.
- Until the switch, the loaders still connect as `postgres`. They work, but without V008's protections.

---

## 6. Restoring a backup taken after V008

PostgreSQL stores roles in the cluster, not in the database dump. A post-V008 dump references `nordiccart_*` roles, so:

1. **The roles must exist before `pg_restore`.**
   - On the same server they already exist.
   - On a new server, run V008's roles section or a `pg_dumpall --roles-only` file first, then set the passwords again.
2. Restore with `pg_restore` as usual. Owners, SECURITY DEFINER, and table, column and function privileges are restored.
3. **Database-level privileges are not restored** by `pg_restore` into an existing database, nor copied by `CREATE DATABASE … TEMPLATE`. PUBLIC would get CONNECT and TEMP back. Run, connected to the restored database as `postgres`:
   ```powershell
   & "$bin\psql.exe" -h localhost -p 5432 -U postgres -d <restored database> -v ON_ERROR_STOP=1 -f "database\security\V008_reapply_database_privileges.sql"
   ```
   The script is idempotent and writes no rows.
4. Run script 17. Check 301 fails if step 3 was skipped.

---

## 7. Rollback

Only needed if V008 committed and must be withdrawn. A failed V008 has already rolled itself back.

### 7.1 Option A (recommended): R008, in place

`database/rollback/R008__undo_security_roles_and_privileges.sql`, as `postgres`, one transaction:

```powershell
& "$bin\psql.exe" -h localhost -p 5432 -U postgres -d nordiccart_oltp -v ON_ERROR_STOP=1 -f "database\rollback\R008__undo_security_roles_and_privileges.sql"
```

What R008 does:
1. Makes the three trigger functions SECURITY INVOKER again, without the fixed search path, and restores their comments.
2. Returns ownership of every object to the database owner (REASSIGN OWNED).
3. Removes every privilege V008 granted (DROP OWNED).
4. Restores PUBLIC's CONNECT, TEMP, schema USAGE and function EXECUTE.
5. Deletes V008's `schema_migrations` row.
6. Drops the seven roles. A role still used by another database is kept, with a NOTICE.

Tested in the sandbox:
- after R008, a normalized catalog snapshot of owners, ACLs, SECURITY DEFINER and settings equals the pre-V008 snapshot exactly;
- data is untouched;
- V008 can be applied again afterwards.

R008 refuses to run if V008 is not applied, or if V009 or later is applied (roll that back first).

Rows written after V008, for example by the loader, are kept. Close the loader's sessions first: a role with open sessions cannot be dropped, and R008 then keeps it with a NOTICE.

After R008:
- script 17 shows the pre-V008 state: **7 PASS, 4 INFO, 20 FAIL** when the roles were dropped, as on a server where `nordiccart_oltp` is the only database using them;
- scripts 11–15 are unchanged.

Rehearsed on a copy of the real content: V008, then R008, then V008 again, then R008 again.

### 7.2 Option B (fallback): restore the pre-V008 backup

Use this only if R008 fails or a check after R008 differs. Restoring replaces the database, so any rows written after V008 are lost. Use the backup from section 2, with its recorded SHA-256, and the restore procedure you authorized for earlier stages.

Afterwards:
- drop the `nordiccart_*` roles if nothing else uses them: `DROP ROLE` in the order loader, scheduler, analyst, extractor, writer, reader, owner;
- verify scripts 11–15 and `--plan`.

This is a database rebuild. It needs your explicit authorization at the time.

---

## 8. Known limitations to keep in mind after deployment

- **Timeouts are not a security control.** A role can `SET` its own timeouts and `ALTER ROLE` its own defaults. A test documents this PostgreSQL behaviour.
- **Database ACLs are lost on template copies and on `pg_restore` into an existing database.** Use section 6.3.
- **The extractor's DELETE on change_log is not limited to consumed rows.** A Phase 4 purge function should replace it.
- **Revoking PUBLIC's CONNECT affects every other non-superuser login role.** See preflight 1.6.
- **The roles are cluster-wide.** They are shared with the sandbox only if the same server is used. The real server and the sandbox are separate clusters.
