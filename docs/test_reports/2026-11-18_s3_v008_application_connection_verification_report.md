# S3: application connection verification after the `config/.env` switch (report)

Date: 2026-11-18, 18:27 CET.

**Context.** You changed `config/.env` (last modified 18:25:58) to `PGUSER=nordiccart_loader` and removed `PGPASSWORD`. The application now authenticates through `%APPDATA%\postgresql\pgpass.conf`.

**Constraints observed.**
- No password was read or displayed. Only the key names of `config/.env` and the non-secret user name were inspected, and the existence of `pgpass.conf` was checked.
- No configuration was modified.
- No PG variables were set in the shell (0), so the run used the application's own configuration only.
- Every session was additionally forced read-only (`default_transaction_read_only=on`). No write was executed.

## Connection identity

| Item | Result |
|---|---|
| `config/.env` keys | PGHOST, PGPORT, PGDATABASE, PGUSER (`nordiccart_loader`). **No PGPASSWORD key** |
| Settings source in both logs | `nordiccart_loader@localhost:5432/nordiccart_oltp (from …\config\.env [PGHOST, PGPORT, PGDATABASE, PGUSER] plus environment)` |
| Session identity | `current_user` = `session_user` = **`nordiccart_loader`**, superuser **false**. The application did **not** connect as `postgres` |
| Database | `nordiccart_oltp`, PostgreSQL 18.4, port 5432, migrations 001–008 |

## Checks (exactly the VS Code configurations: `--plan`, default `config/generator.toml` and `config/.env`)

| Check | Exit | Connected as | Result |
|---|---|---|---|
| Transactions: Check (`python python/run_transactions.py --plan`) | 0 | `nordiccart_loader` | Master data and opening inventory unchanged. 20/20 orders, 100/100 units of work, to run: nothing. **Conflicts 0, blocking 0**. "0 orders and 0 units of work would be written, nothing was written." |
| Replenishment: Check (`python python/run_replenishment.py --plan`) | 0 | `nordiccart_loader` | Pilot transactions complete (100 units verified). 17/17 units of work, to run: nothing. **Conflicts 0, blocking 0**. "0 units of work would be written, nothing was written." |

Logs:
- `logs/transactions_pilot_plan_20261118_182742.log`
- `logs/replenishment_pilot_plan_20261118_182743.log`

## No database records changed

Read-only snapshots were taken through the same `nordiccart_loader` connection, before and after the two checks. Each snapshot holds:
- the exact md5 of every row of the 16 business tables and `meta.schema_migrations`;
- the change_log row count and the md5 of its metadata columns. The loader cannot read `row_data`, by design.

| Comparison | Result |
|---|---|
| Before the checks vs the last full `postgres` snapshot (after the restricted loader test, deployment report section 9.4) | **17 of 17 tables byte-identical**; change_log 1,537 → 1,537 |
| After the checks vs before the checks | **17 of 17 tables byte-identical**; change_log metadata identical (1,537 rows, max change_id 1755) |

Business facts after the checks:
- 20 orders;
- 326 inventory movements;
- 1,467 units on hand, 0 reserved;
- 1,537 change_log rows;
- migrations 001–008.

`change_log_checkpoints` cannot be read by the loader. It held 0 rows in every earlier snapshot and is written only by the extractor role.

## Verdict

**PASS.** The application now connects as the restricted `nordiccart_loader`, not as `postgres`, with no password in `config/.env`. Both pilot checks connect as the loader and report 0 conflicts and nothing to write. No database record changed.

V008 security is now in effect for the application's loader connection.

**One consequence to remember** (deployment report, section 10):
- Administrative work must now name `postgres` explicitly, because `config/.env` no longer connects as `postgres`. This covers backups (`pg_dump -U postgres`), future migrations and R008.
- The opt-in database tests must keep pointing at the sandbox with all PG variables set.
