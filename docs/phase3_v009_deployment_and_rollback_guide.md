# NordicCart V009: deployment and rollback guide

Phase 3, step 5, stage S4. Applies `database/migrations/V009__activity_control_and_purchase_orders.sql` to the real `nordiccart_oltp`, then writes the pilot documents.

**Status (2026-11-18): not applied to the real database.** Tested in the disposable PostgreSQL 18.4 sandbox, on a copy reproducing the real database after S3 (V008 deployed). Each database step below needs your explicit authorization first.

Related documents:
- sandbox test report: `docs/test_reports/2026-11-18_phase3_step5_s4_v009_sandbox_test_report.md`
- design: `docs/phase3_combined_activity_design.md`, revision 2 (L1, L2, L10, C15, C18, E3)
- DBML revision 4: `database/design/nordiccart_oltp_rev4.dbml`

**Admin connection.** Since S3, `config/.env` connects as `nordiccart_loader`. Every administrative command below names `postgres` explicitly (`-U postgres`) and prompts for its password. Never put that password in a command line or a file in the project.

---

## 0. What V009 changes, and what it does not

| Changes | Does not change |
|---|---|
| New tables: `purchase_orders`, `purchase_order_lines`, `reorder_point_revisions`, `meta.activity_runs`, `meta.activity_checkpoints` (with 5 enums, 14 functions, their triggers) | Any table, column, constraint, index, function or row of V001–V008 |
| One new trigger on `inventory_movements`: a purchase receipt whose reference is `PO-<market>-<year>-<seq>/DN<n>` must fit its purchase order (existing order of that warehouse, a line for the product, not before the order, never more than ordered) and moves the order's received quantities | Receipts with other references (V007 behaviour). Reorder point updates without a revision (accepted, reported as drift by script 18) |
| Privileges for the V008 roles: writer (loader, scheduler) SELECT and INSERT on the new tables, UPDATE only of the run status columns, EXECUTE on two read-only helper functions; reader SELECT; nothing for the extractor or PUBLIC | V008 privileges, roles, passwords, `pg_hba.conf`, `config/.env` |
| Its row in `meta.schema_migrations` (`applied_by` = `postgres`) | — |

V009 runs as one transaction under `SET ROLE nordiccart_owner`, so every new object is owned by the owner role. A self-check at the end re-reads ownership, SECURITY DEFINER settings and privileges. Any failure rolls back everything.

The pilot documents are not written by the migration (no data in migrations). The replenishment loader writes them afterwards, as `nordiccart_loader`:
- the 4 pilot purchase orders with their lines (exception E3);
- the 69 R0 rows of the reorder point revision history (decision C18).

No movement is written.

---

## 1. Read-only preflight (real database)

Run as `postgres` (psql or Valentina Studio), connected to `nordiccart_oltp`:

```sql
SELECT version FROM meta.schema_migrations ORDER BY version;            -- 001 to 008, nothing later
SELECT count(*) FROM pg_roles WHERE rolname LIKE 'nordiccart%';           -- 7
SELECT to_regclass('public.purchase_orders');                             -- NULL (V009 not applied)
SELECT (SELECT count(*) FROM orders), (SELECT count(*) FROM inventory_movements),
       (SELECT sum(quantity_on_hand) FROM inventory_balances), (SELECT count(*) FROM change_log);
                                                                          -- 20, 326, 1467, 1537 (as after S3)
SELECT usename, application_name, state FROM pg_stat_activity
 WHERE datname = current_database() AND pid <> pg_backend_pid();         -- no other session in a transaction
```

Also confirm the verified state:
- scripts 01–15 and 17 have 0 FAIL; script 17 shows 27 PASS, 4 INFO;
- both pilot checks show 0 to write. They now run as the loader through `config/.env`:
  ```
  .\.venv\Scripts\python.exe python\run_transactions.py --plan
  .\.venv\Scripts\python.exe python\run_replenishment.py --plan
  ```

Stop and review before going on if:
- a check differs;
- a session is open in a transaction;
- the data has changed since S3. If it has, take the new state into account and take the backup after it.

---

## 2. Backup before V009

```powershell
$bin = "C:\Program Files\PostgreSQL\18\bin"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$dump = "database\backups\nordiccart_oltp_${stamp}_pre_v009.dump"
& "$bin\pg_dump.exe" -h localhost -p 5432 -U postgres -d nordiccart_oltp -Fc -f $dump     # prompts for the postgres password
& "$bin\pg_restore.exe" --list $dump | Measure-Object -Line
Get-FileHash $dump -Algorithm SHA256
```

- The dump must be taken as `postgres`. The loader cannot read `change_log.row_data` or the sequences.
- `database/backups/` is ignored by git, as set up in the S3 readiness work.

---

## 3. Apply V009

```powershell
& "$bin\psql.exe" -h localhost -p 5432 -U postgres -d nordiccart_oltp -v ON_ERROR_STOP=1 -f "database\migrations\V009__activity_control_and_purchase_orders.sql"
```

**Success** looks like this:
- the output ends with `INSERT 0 1` and `COMMIT`;
- no `ERROR`;
- duration in the sandbox: about 0.2 s.

| Failure | Meaning | Action |
|---|---|---|
| `V009 runs as a superuser session ...` | Not connected as `postgres` | Reconnect with `-U postgres` |
| `Migration V009 requires V008 ...` / `... already applied` | Guard | Nothing changed; re-check section 1 |
| `V009 self-check: ...` | Owners, SECURITY DEFINER settings or privileges would differ from the design (for example a privilege granted by hand since S3) | Nothing changed; report the full message |
| `canceling statement due to lock timeout` | Another session holds a lock on `inventory_movements` or `inventory_balances` | Nothing changed; close it and run again |

---

## 4. Verify V009 (read-only)

1. **Script 18** (`sql/verification/18_verify_v009_activity_control.sql`) right after V009: **44 rows, 28 PASS, 6 INFO, 10 FAIL**. The 10 FAILs are exactly checks 301, 401–406 and 501–503: the pilot documents are not written yet. This is expected.
2. **Script 17**: 31 rows, 27 PASS, 4 INFO, 0 FAIL (revision 2, still V008 scope).
3. **Scripts 01–15**: the same results as before V009, with two differences:
   - the new V009 objects appear as INFO rows in scripts 02, 04, 07 and 10;
   - script 10 shows **1 new expected WARN**: the V009 receipt trigger on `inventory_movements`, a V007 table. This is like script 07's 2 known WARN rows for V007's triggers on V006 tables; script 18 check 105 verifies the trigger.
4. **Data**: `SELECT count(*) FROM change_log;` still returns 1,537. V009 writes no business row.

---

## 5. Write the pilot documents (E3 and the R0 history)

The application connection (`config/.env`) is the loader, so no admin step is needed:

```
.\.venv\Scripts\python.exe python\run_replenishment.py --plan
```

Expected:
- `documents (V009): purchase orders 0/4, R0 revisions 0/4 warehouses; ... to write 8`;
- `plan finished: 0 units of work and 8 V009 documents would be written`.

```
.\.venv\Scripts\python.exe python\run_replenishment.py --apply
```

The command asks you to type the database name. It then writes, in **one transaction**:
- the 4 purchase orders with their lines;
- the 69 R0 revision rows.

The log shows `committed: 8 V009 documents ...; no movement written` and 81 change_log rows. A failure rolls back all 8, and a rerun writes them. A second `--plan` shows `to write 0` and `nothing was written`.

Then verify:
- script 18: **44 rows, 38 PASS, 6 INFO, 0 FAIL**. The rehearsal gave, for scripts 01–15, 17 and 18 as the analyst: 883 rows, 709 PASS, 171 INFO, 0 FAIL, 3 expected WARN;
- scripts 01–15 and 17 unchanged;
- business data unchanged, with orders, movements, balances and reorder points byte-identical. Change_log goes from 1,537 to 1,618 (+81 document rows).

---

## 6. Rollback

A V009 that failed has already rolled itself back. For a committed V009:

### 6.1 Option A (recommended while no activity run exists): R009, in place

```powershell
& "$bin\psql.exe" -h localhost -p 5432 -U postgres -d nordiccart_oltp -v ON_ERROR_STOP=1 -f "database\rollback\R009__undo_activity_control_and_purchase_orders.sql"
```

**What it does.** Drops the receipt trigger on `inventory_movements`, the five tables, the 14 functions and the 5 enums, and deletes the 009 migration row.

**Tested in the sandbox:**
- the catalog afterwards is identical to the V008 catalog: tables, columns, constraints, triggers, indexes, functions, owners and ACLs;
- business data is unchanged;
- V009 and the backfill re-apply cleanly.

**What is lost:**
- the purchase order documents and the R0 history (re-creatable with section 5);
- the 81 change_log rows already captured for them stay, because change_log is insert-only.

**When it refuses:**
- once `meta.activity_runs` holds a run, because engine state would be lost; restore a recovery point instead;
- when V009 is not applied, or a later migration is.

### 6.2 Option B: restore the pre-V009 backup

Restoring is a database rebuild; it needs your explicit authorization at that time. It also needs:
- the V008 roles to exist on the server, which they do;
- `database/security/V008_reapply_database_privileges.sql` and `ALTER DATABASE nordiccart_oltp SET timezone = 'UTC'` if you restore into an existing database. See the S3 readiness report, section 6.3.

---

## 7. After deployment

- The S1–S3 pilot checks stay as they are:
  - the transaction loader is unchanged;
  - the replenishment loader now reads R0 from the history and verifies the purchase order documents;
  - script 14 (revision 3) reads reorder points as of the pilot's last event.
- From stage S6, every reorder point change must write its revision in the same transaction (the engine's R-cal unit). A change without one is accepted by the database, but it is caught:
  - script 18 check 601 catches it for listings that already have a revision;
  - the replenishment pilot check (`--plan`) is blocked by it for the pilot's listings.
- Purchase receipts of the engine always use `<PO>/DN<n>` references, which V009 checks fully.
- The run ledger stays empty until the S6 engine runs (in a sandbox first).
