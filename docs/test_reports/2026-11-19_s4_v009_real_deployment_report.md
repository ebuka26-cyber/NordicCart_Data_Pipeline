# NordicCart S4 · V009 real deployment report

**Phase 4 SUCCESS. Overall S4 deployment SUCCESS.** V009 and the historical pilot-document backfill are deployed and verified on `localhost:5432/nordiccart_oltp`. Final verification across scripts 01–15, 17 and 18 returned **709 PASS, 171 INFO, 3 expected WARN, 0 FAIL** (883 rows).

## Phase 4 · Real post-backfill results

The backfill log `logs/replenishment_pilot_apply_20261119_153355.log` records eight documents committed in one transaction, 81 change-log entries and zero new movements. The real read-only snapshot confirmed migrations 001–009, four received purchase orders, four lines, 69 pilot R0 revisions, 1,618 change-log rows, zero activity runs and zero checkpoints. All 16 original business tables matched their pre-V009 row counts and full-row digests, including 20 orders, 326 movements and balances totaling 1,467 stock units.

Both pilot loaders ran with `--plan --no-csv` as `nordiccart_loader` with read-only connections enforced. Both exited 0 with zero pending work, conflicts or blocking issues. Replenishment found all four purchase orders and four warehouse revision sets. Logs: `transactions_pilot_plan_20261119_153705.log` and `replenishment_pilot_plan_20261119_153706.log` in `logs/`.

Scripts 01–13, 15, 17 and 18 returned **672 PASS, 164 INFO, 3 known WARN, 0 FAIL** (839 rows). Script 18 returned 38 PASS, 6 INFO and 0 FAIL. The two script 07 warnings and the script 10 receipt-trigger warning matched the rehearsed objects by name. Per-script results and live counts: `logs/s4_v009_final_loader_checks.txt`.

The final script 14 transcript records 37 PASS, 7 INFO, 0 FAIL and ROLLBACK of the read-only transaction. The operator confirmed psql exit 0. Evidence: `logs/s4_v009_final_14_manual.txt`. All S4 verification is complete.

The next planned stage is S5: the daily exchange-rate generator, X1 loader, verification script 16 and 96-row E1 historical backfill. S5 awaits separate authorization.

## Historical deployment record

The following entries preserve the commands, results and interim decisions recorded during deployment.

## Phase 2 closeout and Phase 3 preparation

The real script 14 transcript (`logs/s4_v009_post_14_manual.txt`) contains 37 PASS, 7 INFO, 0 FAIL and psql exit 0. With scripts 01–13, 15, 17 and 18, the final Phase 2 tally was 883 checks: 699 PASS, 171 INFO, 3 expected WARN and exactly 10 expected pre-backfill FAIL (script 18 checks 301, 401–406, 501–503). Zero unexpected failures.

At 14:37 on November 19, the real loader ran `python/run_replenishment.py --plan --no-csv` with read-only connections enforced, exit 0. It identified V001–V009 as `nordiccart_loader`. The plan found four missing purchase orders (DK, FI, NO, SE), four missing warehouse R0 revision sets, zero pending inventory operations, zero conflicts and zero blocking issues. The plan covers four order lines and 69 historical revision rows. Its fingerprint is `858bbfd09597030b573273c9885ffae0c0299e4ed146e8aca7c98f0e05531236`. Evidence: `logs/replenishment_pilot_plan_20261119_143705.log`.

The restored-backup rehearsal measured +81 change-log entries for the same backfill. The expected real total after apply is 1,618 from 1,537. This plan wrote zero database rows.

**Command for a later authorized backfill** (PowerShell):

```powershell
Set-Location -LiteralPath '<project folder>'
$env:PGHOST = 'localhost'
$env:PGPORT = '5432'
$env:PGDATABASE = 'nordiccart_oltp'
$env:PGUSER = 'nordiccart_loader'
& '.\.venv\Scripts\python.exe' -B '.\python\run_replenishment.py' --apply --no-csv
```

Review the target and pending counts before typing `nordiccart_oltp` at the confirmation prompt. The historical entries below record earlier checkpoints.

## Phase 1 · Final preflight

| Operation | Exit | Observed result |
|---|---:|---|
| `Get-FileHash -Algorithm SHA256` on V009, the pre-V009 dump and R009 | 0 | All three matched their readiness hashes, listed below. |
| PostgreSQL 18 `pg_restore.exe --list database/backups/nordiccart_oltp_20261119_041244_pre_v009.dump` | 0 | 286,354 bytes, custom gzip archive from PostgreSQL 18.4, 461 TOC entries, 455 listed, 19 table-data entries, zero V009 objects. The readiness report records the successful full restore. |
| Read-only loader connection attempts inside the command sandbox (`psql -X -w`, then application/libpq connection) | 2 each | Server requested authentication. No SQL ran. No credential was inspected or changed. |
| Read-only loader comparison with `loader_snapshot_v9final.json` using the project's Python environment in the Windows user context | 0 | Loopback address ::1, port 5432, database `nordiccart_oltp`, session user `nordiccart_loader`, PostgreSQL 18.4 primary, read-only transaction. Migrations exactly 001–008, seven NordicCart roles, zero V009 tables. 20 orders, 326 movements, 1,467 units on hand, zero reserved, 1,537 change-log rows, maximum change ID 1,755. Zero other client sessions or prepared transactions. All 17 table counts and full-row digests matched, as did the change-log metadata digest. The existing backup is current at this check. |
| `.\.venv\Scripts\python.exe -B python/run_transactions.py --plan --no-csv` as loader with `default_transaction_read_only=on` | 0 | Zero orders and zero units of work pending, zero conflicts and zero blocking issues. Log: `logs/transactions_pilot_plan_20261119_051542.log`. |
| `.\.venv\Scripts\python.exe -B python/run_replenishment.py --plan --no-csv` in the same read-only context | 0 | Zero units of work pending, zero conflicts and zero blocking issues. Log: `logs/replenishment_pilot_plan_20261119_051608.log`. |
| `psql -X -w -h localhost -p 5432 -U postgres -d nordiccart_oltp -v ON_ERROR_STOP=1 -f logs/s4_v009_real_phase1_readonly.sql` in the Windows user context | 2 | The server required password authentication. No administrator session opened and no SQL ran. Output: `logs/s4_v009_real_phase1_auto.txt`. |

The loader comparison used a repeatable-read, read-only transaction and the readiness snapshot's table digest query, with the same date, timezone, interval and floating-point settings. Only aggregates and comparison results were reported.

| Artifact | SHA-256 |
|---|---|
| `database/migrations/V009__activity_control_and_purchase_orders.sql` | `65A4EA6D878A87168BEBE9C146A0AE6C101D18582727A5317AB5D33216EF5B17` |
| `database/backups/nordiccart_oltp_20261119_041244_pre_v009.dump` | `03B689488F3957F220F00CBD381295003B415A9D9020DE92D7C2CCF266660825` |
| `database/rollback/R009__undo_activity_control_and_purchase_orders.sql` | `7E6984A54D11C0A2BD2B46658155CA001DDFA2904F663726945F464704AAA227` |

## Deployment checkpoint

The last successful database operation was the read-only replenishment plan at 05:16:08. Both pilot plans passed. The command wrapper initially returned 74 because its text matcher omitted the transaction plan's words “0 orders and”. The loader itself exited 0. I verified its saved log and ran only the remaining replenishment plan.

**Pending operator action.** The guide requires an administrator session for preflight. A local interactive prompt is needed. Run the following in PowerShell, and enter the administrator password only at the PostgreSQL prompt. The prepared input runs the identity, baseline and session checks, then scripts 01–15 and 17 under `SET LOCAL ROLE nordiccart_analyst`, inside a single read-only transaction. It stops at the first discrepancy. A final `ROLLBACK` closes that read-only transaction.

```powershell
Set-Location -LiteralPath '<project folder>'
$env:PGCLIENTENCODING = 'UTF8'
& 'C:\Program Files\PostgreSQL\18\bin\psql.exe' -X -W -h localhost -p 5432 -U postgres -d nordiccart_oltp -v ON_ERROR_STOP=1 -f '.\logs\s4_v009_real_phase1_readonly.sql' 2>&1 | Tee-Object -FilePath '.\logs\s4_v009_real_phase1_manual.txt'
"psql exit $LASTEXITCODE" | Tee-Object -FilePath '.\logs\s4_v009_real_phase1_manual.txt' -Append
```

The output to inspect is `logs/s4_v009_real_phase1_manual.txt`. Require `PHASE1_SQL_PASS`, 789 rows (671 PASS, 116 INFO, zero FAIL, two known WARN from script 07), and psql exit 0. Inspect the saved output before proceeding to Phase 2. No migration command has been issued.

V009, the pilot backfill, R009 and restore have not run. No recovery action is indicated. Any future rollback or restore requires separate authorization.

## Latest checkpoint · Phase 1 stopped for review

**PARTIAL.** The manual preflight transcript confirms one other client, zero prepared transactions and a match on every other reported baseline value. The session guard deliberately raised an error at line 36 before scripts 01–15 and 17 ran. The transcript contains no numeric psql exit code.

The subsequent read-only `pg_stat_activity` query ran as `nordiccart_loader` on localhost port 5432 and exited 0. It selected PID, role, application, state, transaction start and connection metadata for other client backends on `nordiccart_oltp`, excluding its own PID. It returned **zero rows**. The earlier transcript captured only a count. The earlier client's role, application, state and transaction status cannot be recovered from these observations. Zero prepared transactions does not establish whether it had an ordinary open transaction.

After review, rerun the unchanged Phase 1 preflight in a quiet window. If the client recurs, inspect its session metadata while it is connected. No session was terminated, no preflight script was changed, and deployment remains paused before V009.

## Current checkpoint · Phase 1 harness corrected and sandbox-validated

**Deployment verdict: PARTIAL.** The latest real preflight passed its baseline and script 01 (11 PASS, 3 INFO). The generated harness enclosed two statements from script 02 in one subquery. PostgreSQL reported a syntax error at the first statement terminator. Phase 1 stopped and its read-only transaction closed. No numeric exit code appears in that transcript.

All 16 prescribed scripts were inspected. Scripts 02, 05 and 08 each return two result sets. The corrected wrapper evaluates each SELECT independently and combines their status rows with UNION ALL before applying the existing per-script count and failure checks. All 17 original verification SQL files retained their SHA-256 hashes. The repeatable-read, read-only transaction, identity and session checks, analyst role, ON_ERROR_STOP and existing warning checks remain in place.

Validation used PostgreSQL 18.4 and the restored V008 sandbox on 127.0.0.1:55432. The positive test copy differed from the real harness only in its expected port. The three repaired scripts returned 115, 62 and 40 rows respectively. Across 19 result sets, psql returned 0 with 789 rows, 671 PASS, 116 INFO, zero FAIL and two known script 07 WARN. A separate negative test copy expected 110 PASS for script 02 instead of 109. It stopped at script 02 with exit 3, before script 03. Logs are in `logs/s4_v009_phase1_harness_validation_55432.txt` and `logs/s4_v009_phase1_harness_rejection_55432.txt`. The sandbox was stopped. The real database was not accessed in this repair.

Next safe action after review is the corrected Phase 1 rerun below. It writes a new transcript to preserve the failed run.

```powershell
Set-Location -LiteralPath '<project folder>'
$env:PGCLIENTENCODING = 'UTF8'
& 'C:\Program Files\PostgreSQL\18\bin\psql.exe' -X -W -h localhost -p 5432 -U postgres -d nordiccart_oltp -v ON_ERROR_STOP=1 -f '.\logs\s4_v009_real_phase1_readonly.sql' 2>&1 | Tee-Object -FilePath '.\logs\s4_v009_real_phase1_corrected.txt'
"psql exit $LASTEXITCODE" | Tee-Object -FilePath '.\logs\s4_v009_real_phase1_corrected.txt' -Append
```

## Phase 2 · Awaiting manual deployment

**Phase 2 verdict: PARTIAL.** Phase 1 passed on the real database. I inspected `logs/s4_v009_real_phase1_manual_v2.txt`: 789 rows, 671 PASS, 116 INFO, zero FAIL, two expected WARN, `PHASE1_SQL_PASS` and psql exit 0.

`Get-FileHash -Algorithm SHA256` confirmed the approved V009 digest `65A4EA6D878A87168BEBE9C146A0AE6C101D18582727A5317AB5D33216EF5B17`. An immediate read-only psql query as `nordiccart_loader` exited 0. It confirmed PostgreSQL 18.4 primary at ::1:5432, database `nordiccart_oltp`, migrations exactly 001–008, seven NordicCart roles, zero V009 tables, 20 orders, 326 movements, 1,467 units, zero reserved, 1,537 change-log rows (max ID 1,755), zero other clients and zero prepared transactions.

The following operator command repeats the verified identity, baseline and session guard before V009 in the same psql session. The guard closes its read-only transaction with ROLLBACK. V009 then manages its own transaction. The final SELECT reads registration. Enter the administrator password only at the PostgreSQL prompt.

```powershell
Set-Location -LiteralPath '<project folder>'
& {
    $migration = '.\database\migrations\V009__activity_control_and_purchase_orders.sql'
    $approved = '65A4EA6D878A87168BEBE9C146A0AE6C101D18582727A5317AB5D33216EF5B17'
    if ((Get-FileHash -LiteralPath $migration -Algorithm SHA256).Hash -ne $approved) { throw 'STOP: V009 hash mismatch' }
    $env:PGCLIENTENCODING = 'UTF8'
    $log = '.\logs\s4_v009_phase2_deploy_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.txt'
    & 'C:\Program Files\PostgreSQL\18\bin\psql.exe' -X -W -h localhost -p 5432 -U postgres -d nordiccart_oltp -v ON_ERROR_STOP=1 -P pager=off -f '.\logs\s4_v009_phase2_immediate_check.sql' -f $migration -c "SELECT version, script_name, applied_by FROM meta.schema_migrations WHERE version = '009';" 2>&1 | Tee-Object -FilePath $log
    "psql exit $LASTEXITCODE" | Tee-Object -FilePath $log -Append
}
```

**Pending:** inspect the real transcript for successful guard, migration INSERT and COMMIT, registration as postgres and exit 0. Then run read-only scripts 01–15, 17 and 18 and check existing business data. Require script 17 at 27 PASS / 4 INFO; script 18 at 28 PASS / 6 INFO / exactly ten FAIL (301, 401–406, 501–503); only expected warnings in 07 and 10. Stop after Phase 2. No database write was issued by this handoff. No automatic retry, rollback or restore is authorized.

## Phase 2 · Real migration committed, verification awaiting script 14

**Migration: SUCCESS. Full verification: FAILED to complete.** The real deployment transcript `logs/s4_v009_phase2_deploy_20261119_142429.txt` confirms PHASE2_PRE_DEPLOY_CHECK_PASS, COMMIT, registration of the approved filename as postgres and psql exit 0. Live read-only SQL independently confirmed migrations 001–009, 20 orders, 326 inventory movements, 1,467 units on hand and 1,537 change-log rows. All five V009 tables were empty.

I executed the original scripts 01–13, 15, 17 and 18 using psycopg as the loader in one repeatable-read, read-only transaction. All result sets were consumed. Exit 0. Per-script counts and warning names are in `logs/s4_v009_post_loader.txt`. Combined: 839 rows, 662 PASS, 164 INFO, 10 expected FAIL, 3 expected WARN, zero unexpected FAIL. Script 17: 27 PASS, 4 INFO. Script 18: 28 PASS, 6 INFO and precisely 301, 401–406 and 501–503 failed. Its structure, ownership and privilege checks passed. The two known warnings in 07 and the V009 receipt trigger warning in 10 matched by name.

Script 14 reads change_log.row_data, beyond the loader grant. A direct psql analyst connection with -w exited 2 before executing SQL because no noninteractive password was available. Script 14's post-deployment outcome remains unverified. No backfill, database changes or recovery operations were performed during verification.

Run only script 14 locally with interactive authentication. Then inspect the saved rows for 37 PASS, 7 INFO, zero FAIL and exit 0 before closing Phase 2.

```powershell
Set-Location -LiteralPath '<project folder>'
$env:PGCLIENTENCODING = 'UTF8'
& 'C:\Program Files\PostgreSQL\18\bin\psql.exe' -X -W -h localhost -p 5432 -U postgres -d nordiccart_oltp -v ON_ERROR_STOP=1 -P pager=off -c 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL ROLE nordiccart_analyst;' -f '.\sql\verification\14_verify_replenishment.sql' -c 'ROLLBACK;' 2>&1 | Tee-Object -FilePath '.\logs\s4_v009_post_14_manual.txt'
"psql exit $LASTEXITCODE" | Tee-Object -FilePath '.\logs\s4_v009_post_14_manual.txt' -Append
```
