# S4 V009 readiness verification: checkpoint and handoff

**Phase 4 SUCCESS. Overall S4 deployment SUCCESS.** V009 and the historical pilot-document backfill are deployed and verified on `localhost:5432/nordiccart_oltp`. Final verification across scripts 01–15, 17 and 18 returned **709 PASS, 171 INFO, 3 expected WARN, 0 FAIL** (883 rows). Script 14 returned 37 PASS, 7 INFO and 0 FAIL. Its transcript records ROLLBACK and the operator confirmed exit 0. Both loader plans found zero pending work or conflicts. No S4 actions remain.

The next planned stage is S5: the daily exchange-rate generator, X1 loader, verification script 16 and 96-row E1 historical backfill. S5 awaits separate authorization.

The sections below preserve earlier readiness and deployment checkpoints.

## Completed tasks
- **Step 1, backup file verified.**
  - `database/backups/nordiccart_oltp_20261119_041244_pre_v009.dump`: 286,354 bytes; created 2026-11-19 04:12:44 (last write 04:12:51).
  - **SHA-256 `03B689488F3957F220F00CBD381295003B415A9D9020DE92D7C2CCF266660825`** (Get-FileHash and Python hashlib agree).
  - `pg_restore --list` exit 0: custom format, gzip, pg_dump 18.4 from server 18.4, dbname `nordiccart_oltp`.
  - 461 TOC entries (455 listed): 19 TABLE DATA, 60 TRIGGER, 77 ACL, 3 DEFAULT ACL; the DATABASE and DATABASE PROPERTIES entries are restored with `--create`.
  - No V009 object in the archive.
  - `database/backups/` is in `.gitignore`.
- **Step 2, full test restore done.**
  - The sandbox's stale `nordiccart_oltp` (pre-V008 restore) was dropped, sandbox only.
  - `pg_restore --create --exit-on-error -d postgres <backup>`: exit 0 in 1.4 s.
  - Restored `nordiccart_oltp`: owner postgres, UTF8, English_United States.1252; database setting `TimeZone=UTC`.
  - Database ACL restored as V008 set it: `{postgres=CTc, nordiccart_writer=c, nordiccart_reader=c, nordiccart_extractor=c}`.
- **Step 3, comparison with the last verified real state done** (`v9r_compare.py`, `v9r_privileges.py`, outputs `v9r_compare.txt` and `v9r_privileges.txt`).
  - **Against the last full postgres snapshot of the real database** (`loader_test_preflight.json`, 2026-11-18 18:17):
    - **19 of 19 tables byte-identical**: every column, ids, change_log row contents;
    - migrations, content, order statuses, movement types, ledger identity, object counts, owners, memberships and database settings EQUAL.
  - **Against today's real-database loader snapshot** (`loader_snapshot_v9ready.json`): 17 of 17 tables byte-identical; change_log metadata identical (1,537 rows, max id 1755).
  - **Catalog against the rebuilt real state** (`nordiccart_oltp_s4_base`): 963 entries, identical except the database ACL row. The difference is a sandbox artifact: R008 ran in other test databases, and its `DROP OWNED BY` also revokes CONNECT on shared objects, which stripped `s4_base`'s grants. The restored copy's ACL equals the real one recorded at the V008 deployment.
  - **Effective privileges on the restored copy:**
    - V008 matrix 360 of 360 as designed;
    - sequence privileges for application roles or PUBLIC: 0 of 315;
    - 3 SECURITY DEFINER functions with the fixed search_path;
    - 3 default ACLs.
  - **Explained, not a defect:**
    - The real sequences carry an explicit ACL `{nordiccart_owner=rwU}` (made explicit by V008's REVOKE FROM PUBLIC). pg_dump omits ACLs that equal the default, so the restored sequences use the default, which grants the same (owner only).
    - Role password flags differ: roles are cluster objects, not in the dump.
- **Step 4, verification scripts on the restored copy done** (as `nordiccart_analyst`, read-only; output `v9r_scripts_restored.txt`).
  - **Script 14: 44 rows, 37 PASS, 7 INFO, 0 FAIL.**
  - Scripts 01–15 and 17 in total: 789 rows, 671 PASS, 116 INFO, 0 FAIL, 2 WARN (script 07, known). This is identical to the real database after the V008 deployment.
- **Step 5, V009 rehearsal on copies of the restored backup done** (`s4_rehearsal.py` with `NC_SOURCE=nordiccart_oltp`; outputs `v9r_rehearsal.txt` and `v9r_fail_ids.txt`). V009 under test SHA-256 `65A4EA6D…5B17`.
  - Preflight on the copy: migrations 001–008, 7 roles, no `purchase_orders`, change_log 1,537. Loader `--plan` for transactions and replenishment: 0 conflicts, nothing to write.
  - **V009 via psql as postgres: exit 0, 216 ms; stderr empty; ends `COMMIT`.**
  - Before the backfill, as the analyst:
    - script 17: 27 PASS, 4 INFO, 0 FAIL;
    - script 18: 28 PASS, 6 INFO, 10 FAIL. These are exactly the expected FAILs: 301, 401–406 and 501–503, i.e. the documents not yet written.
    - Business data unchanged; change_log 1,537.
  - Backfill as `nordiccart_loader`:
    - `--plan` showed 8 documents;
    - `--apply --yes` exit 0: 4 purchase orders and lines, 69 R0 revisions, 81 change_log rows, no movement;
    - the `--plan` rerun showed 0 to write; transactions `--plan` showed 0.
  - After the backfill, scripts 01–15, 17 and 18 as the analyst: **883 rows, 709 PASS, 171 INFO, 0 FAIL, 3 WARN.**
    - Script 14: 44 rows, 37 PASS, 7 INFO, 0 FAIL.
    - Script 18: 38 PASS, 6 INFO, 0 FAIL.
    - WARNs: 2 known in script 07; 1 expected new one in script 10 (receipt trigger on `inventory_movements`).
  - Business data unchanged (orders, lines, payments, reservations, movements, balances, master data). change_log went from 1,537 to 1,618. Documents (4, 4, 69, all `received`).
  - Rollback copy:
    - V009, then the backfill, then R009 (exit 0; NOTICE "drops 4 purchase orders, 4 order lines and 69 reorder point revisions");
    - **catalog identical to the pre-V009 state (963 entries); business data identical**;
    - V009 re-applied, and the backfill again: exit 0.
  - `nordiccart_oltp_v9r_rollback` and `nordiccart_oltp_v9r_failids` dropped. `nordiccart_oltp_v9r_rehearsal` kept, sandbox only.
- **Final read-only freshness check of the real database** (04:26, as `nordiccart_loader`, `loader_snapshot.py v9final`):
  - 17 of 17 tables byte-identical to today's pre-backup snapshot; change_log metadata identical (1,537 rows, max id 1755);
  - migrations 001–008, i.e. no V009.
  - Re-hash at the end: backup `03B68948…0825`, V009 `65A4EA6D…5B17` and R009 `7E6984A5…A227`, all unchanged.
- **Step 6, report done.**
  - `docs/test_reports/2026-11-19_s4_v009_predeployment_readiness_report.md` rewritten with every finding: **verdict READY**.
  - Status document S4 entries updated: readiness complete, READY, not applied.
  - Root copies refreshed.
  - Sandbox stopped.
- From the earlier readiness run (2026-11-19, read-only, as `nordiccart_loader`):
  - migrations 001–008; no V009 objects;
  - pilot data 20 / 326 / 1,467 / 1,537;
  - 17 tables byte-identical to the last snapshot;
  - pilot `--plan` checks 0 conflicts;
  - scripts 01–13, 15, 17: 0 FAIL;
  - V009 SHA-256 `65A4EA6D…5B17` matches.

## Historical checkpoint · Remaining tasks
1. ~~Verify the backup file~~ (done)
2. ~~Full test restore in the sandbox~~ (done: sandbox database `nordiccart_oltp` = the restored backup)
3. ~~Compare the restored database with the last verified real state~~ (done: identical; differences explained)
4. ~~Script 14 on the restored copy~~ (done: 44/37/7/0)
5. ~~V009 rehearsal on a copy of the restored database~~ (done: all as expected, 0 FAIL after the backfill, R009 clean)
6. ~~Update `2026-11-19_s4_v009_predeployment_readiness_report.md` with a READY / NOT READY verdict~~ (done: READY)

Nothing remains for readiness. V009 deployment is a separate task and needs the user's explicit authorization.

## Errors or blockers
- none

## Files and outputs
- Backup (created by the user as postgres): `database/backups/nordiccart_oltp_20261119_041244_pre_v009.dump`
- Readiness report: `docs/test_reports/2026-11-19_s4_v009_predeployment_readiness_report.md`. Root copy: `2026-11-19_s4_v009_predeployment_readiness_report.md`.
- This checkpoint. Root copy: `S4_V009_VERIFICATION_CHECKPOINT.md`.
- Status document: `docs/phase2_database_implementation_status.md`. Root copy: `phase2_database_implementation_status.md`.
- Deployment guide: `docs/phase3_v009_deployment_and_rollback_guide.md`.
- Migration and rollback:
  - `database/migrations/V009__activity_control_and_purchase_orders.sql` (SHA-256 `65A4EA6D…5B17`);
  - `database/rollback/R009__undo_activity_control_and_purchase_orders.sql` (SHA-256 `7E6984A5…A227`).
- Scratchpad (session-specific, may be cleared): `<session scratchpad folder>\`.
  - Scripts:
    - `v9r_compare.py`, `v9r_privileges.py`: step 3;
    - `pd_run_scripts.py --db nordiccart_oltp --user nordiccart_analyst 01 … 17`: step 4;
    - `s4_rehearsal.py` (environment `NC_SOURCE`, `NC_DB`, `NC_RB`) and `v9r_fail_ids.py`: step 5;
    - `loader_snapshot.py`: the read-only real-database check.
  - Outputs: `v9r_compare.txt`, `v9r_privileges.txt`, `v9r_scripts_restored.txt`, `v9r_rehearsal.txt`, `v9r_fail_ids.txt`, `loader_snapshot_v9final.json`.
- Commands, all sandbox only, with PGHOST=localhost, PGPORT=55432, PGUSER=postgres:
  - `pg_restore --list <backup>`;
  - `pg_restore --create --exit-on-error -d postgres <backup>`.

## Historical checkpoint · Current environment
- Real database: localhost:5432/nordiccart_oltp. Read-only access only, through `config/.env` (`nordiccart_loader`). No admin access in this session.
- Sandbox: PostgreSQL 18.4 data directory `<scratchpad>\pg18_sandbox\data`, port 55432, trust authentication, superuser `postgres` with PGPASSWORD `sandbox` (sandbox only).
  - Start: `pg_ctl start -D <data> -o "-p 55432"`. Stop: `pg_ctl stop -D <data> -m fast`.
  - **Restore status:**
    - `nordiccart_oltp` = the restored pre-V009 backup, unchanged by the rehearsal (copies were used);
    - `nordiccart_oltp_v9r_rehearsal` = restored backup + V009 + backfill;
    - other databases are from earlier S3/S4 test runs.
  - **Server stopped** at the end of this verification.
- Real database: unchanged by this work. Migrations 001–008, no V009, change_log 1,537.

## Historical checkpoint · Next action
**None for readiness. Wait for the user's explicit authorization to deploy V009.** Do not start deployment without it.

When authorized, follow `docs/phase3_v009_deployment_and_rollback_guide.md`:
1. Section 1: read-only preflight. If migrations are still 001–008 and change_log is still 1,537 rows, backup `nordiccart_oltp_20261119_041244_pre_v009.dump` is still current. Otherwise, take a new backup as `postgres`.
2. Sections 3–5: V009 as `postgres` (the user types the password); scripts 17 and 18 (expect 10 FAIL: 301, 401–406, 501–503); the backfill as `nordiccart_loader` (`--plan` shows 8, `--apply`, `--plan` shows 0); scripts 01–18 with 0 FAIL.

## Historical checkpoint · Current deployment checkpoint · Phase 2 PARTIAL

Phase 1 passed on the real target (789 rows, 0 FAIL, 2 expected WARN, exit 0). The approved V009 hash and the immediate live V008 baseline matched. The last successful database operation was the read-only loader safety check. Awaiting manual administrator execution of V009. Inspect its timestamped transcript in `logs/s4_v009_phase2_deploy_*.txt` before post-migration verification. The deployment report contains the exact command. Backfill awaits separate review.

## Historical checkpoint · Latest Phase 2 checkpoint

V009 committed and registered as postgres. Real checks 01–13, 15, 17 and 18 complete with zero unexpected failures. Script 14 is pending interactive authentication. Use the deployment report’s read-only command and inspect its transcript. No backfill authorized.
