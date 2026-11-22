# S4: V009 pre-deployment readiness report

Date: 2026-11-19. Updated after you took the pre-V009 backup as `postgres` (04:12:44). The first pass had one open item, this backup; it is now closed.

Scope: readiness only, after ChatGPT approved the corrected V009 for controlled deployment.

What was not done:
- V009 not applied to the real database; backfill not run there; no real-database record changed;
- `config/.env`, passwords and `pg_hba.conf` not changed.

How the real database was accessed:
- **read-only only**, through the application's own connection (`config/.env` = `nordiccart_loader`, password from `pgpass.conf`, never seen);
- every session forced read-only (`default_transaction_read_only=on`).

How the sandbox was used:
- every restore, migration, backfill and rollback ran only in the disposable PostgreSQL 18.4 sandbox (port 55432).

Step-by-step record: `docs/test_reports/S4_V009_VERIFICATION_CHECKPOINT.md`.

## 1. Verdict

**READY for V009 deployment.**
- The backup is intact and restores completely.
- The restored copy matches the last verified real state.
- Script 14 passes.
- The corrected V009 and its backfill and rollback behave exactly as in the approved sandbox tests, now on a copy of today's real data.
- Final read-only check (04:26): the real database has not changed since the backup. Migrations 001–008, no V009, 17 of 17 tables byte-identical, change_log 1,537 rows (max id 1755).

Deployment still needs **your explicit authorization**. When you deploy, follow `docs/phase3_v009_deployment_and_rollback_guide.md`:
- Rerun section 1, the read-only preflight. If migrations are still 001–008 and change_log is still 1,537 rows, this backup is still current and section 2 need not be repeated. If anything changed, take a new backup first.
- Then follow sections 3 (apply V009 as `postgres`), 4 (verify) and 5 (pilot documents as `nordiccart_loader`).

## 2. Results of this verification

| # | Check | Result |
|---|---|---|
| 1 | Backup file | `database/backups/nordiccart_oltp_20261119_041244_pre_v009.dump`, 286,354 bytes |
| 1 | SHA-256 | **`03B689488F3957F220F00CBD381295003B415A9D9020DE92D7C2CCF266660825`**. Get-FileHash and Python hashlib agree; unchanged at the end of the verification |
| 1 | Archive integrity | `pg_restore --list` exit 0.<br>Format: custom, gzip, pg_dump 18.4 from server 18.4, database `nordiccart_oltp`.<br>461 TOC entries: 19 TABLE DATA, 60 TRIGGER, 77 ACL, 3 DEFAULT ACL, plus DATABASE and DATABASE PROPERTIES.<br>No V009 object. `database/backups/` is git-ignored |
| 2 | Full test restore | `pg_restore --create --exit-on-error -d postgres`: **exit 0 in 1.4 s**.<br>Owner postgres, UTF8, English_United States.1252, database setting `TimeZone=UTC`.<br>Database ACL as V008 set it: `{postgres=CTc, nordiccart_writer=c, nordiccart_reader=c, nordiccart_extractor=c}` |
| 3 | Records vs the last full real snapshot (as `postgres`, 2026-11-18 after V008) | **19 of 19 tables byte-identical**: every column and id, including `change_log.row_data`.<br>Migrations, content, order statuses, movement types, ledger identity, object counts, owners, role memberships and database settings: equal |
| 3 | Records vs today's real snapshot (as the loader, before the backup) | **17 of 17 tables byte-identical**; change_log metadata identical (1,537 rows, max id 1755) |
| 3 | Content | 20 orders, 26 lines, 21 payments, 23 reservations, 326 movements, 1,467 units on hand, 0 reserved, 320 balances, 533 warehouse reorder points, change_log 1,537 |
| 3 | Constraints, triggers, indexes, functions, types, owners, ACLs | Full catalog (963 entries) identical to the rebuilt real state, except one sandbox artifact (section 4) |
| 3 | Privileges (effective) | V008 privilege matrix **360 of 360** as designed.<br>Sequence privileges held by application roles or PUBLIC: 0 of 315 checks.<br>3 SECURITY DEFINER functions with `search_path=pg_catalog, public, pg_temp`.<br>3 default ACLs.<br>Database ACL equal to the real one |
| 4 | **Script 14** on the restored copy (as `nordiccart_analyst`) | **44 rows: 37 PASS, 7 INFO, 0 FAIL**. Same as on the real database after the V008 deployment |
| 4 | Scripts 01–15 and 17 on the restored copy | 789 rows: 671 PASS, 116 INFO, **0 FAIL**, 2 known WARN (script 07) |
| 5 | V009 rehearsal | All as expected. See section 3 |
| — | V009 file | SHA-256 `65A4EA6D878A87168BEBE9C146A0AE6C101D18582727A5317AB5D33216EF5B17`: matches the approved value |
| — | R009 file | SHA-256 `7E6984A54D11C0A2BD2B46658155CA001DDFA2904F663726945F464704AAA227`: unchanged since the S4 review |

## 3. V009 rehearsal on copies of the restored backup

The rehearsal followed the deployment guide, sections 3–6. Commands ran as on the real server: V009 and R009 via `psql -v ON_ERROR_STOP=1` as `postgres`; the backfill as `nordiccart_loader`; checks as `nordiccart_analyst`.

| Step | Result |
|---|---|
| Preflight on the copy | migrations 001–008; 7 roles; no `purchase_orders`; change_log 1,537.<br>Loader `--plan` for transactions and replenishment: 0 conflicts, nothing to write |
| **Apply V009** | **exit 0, 216 ms** (psql start included); stderr empty; ends with `COMMIT` |
| Right after V009 | script 17: 27 PASS, 4 INFO, 0 FAIL.<br>Script 18: 28 PASS, 6 INFO and 10 FAIL. The FAILs are **exactly the expected ones**, checks 301, 401–406 and 501–503: the pilot documents are not yet written.<br>Business data unchanged; change_log 1,537 |
| Backfill `--plan` | 8 documents to write: 4 purchase orders (DK, FI, NO, SE) and 4 R0 revision sets; 0 conflicts |
| Backfill `--apply --yes` | exit 0, in one transaction: 4 purchase orders with 4 lines and 69 R0 revision rows; **81 change_log rows; no movement written** |
| Rerun `--plan` (replenishment and transactions) | 0 to write; 0 conflicts |
| After the backfill: scripts 01–15, 17 and 18 | 883 rows: 709 PASS, 171 INFO, **0 FAIL**, 3 WARN.<br>Script 14: 37 PASS, 7 INFO, 0 FAIL. Script 18: 38 PASS, 6 INFO, 0 FAIL.<br>WARNs: 2 known in script 07; 1 expected new one in script 10 (V009's receipt trigger on `inventory_movements`) |
| Business data | Unchanged: orders, lines, payments, reservations, movements, balances, master data.<br>change_log 1,537 → 1,618 (+81). Purchase orders: 4, all `received` |
| Rollback on a second copy | V009, backfill, then **R009: exit 0** with NOTICE "drops 4 purchase orders, 4 order lines and 69 reorder point revisions".<br>**Catalog identical to the pre-V009 state (963 entries); business data identical** |
| Re-apply after rollback | V009 exit 0, then backfill exit 0 (8 documents again) |

These numbers match the approved S4 sandbox report (`2026-11-18_phase3_step5_s4_v009_sandbox_test_report.md`), now on a copy of the backup itself.

## 4. Differences found, all explained (none is a defect)

| Difference | Cause | Effect |
|---|---|---|
| Sequence ACL is explicit `{nordiccart_owner=rwU}` on the real database; NULL (default) on the restored copy | V008's `REVOKE … FROM PUBLIC` made the default ACL explicit. pg_dump omits ACLs equal to the default | None: same privileges, owner only. 0 of 315 sequence checks held by other roles |
| Role password flags | Roles are cluster-level objects, not part of a database dump | None for a restore on the same server. The roles already exist there |
| Database ACL of the comparison reference `nordiccart_oltp_s4_base` | Sandbox artifact: R008's `DROP OWNED BY`, run in other sandbox test databases, also revokes CONNECT on shared objects | None. The restored copy's ACL equals the real one recorded at the V008 deployment |

Restore note, for guide section 6.2: restore with `pg_restore --create` as `postgres`, as tested here. It restores the database ACL and the `TimeZone=UTC` setting together with the data.

## 5. Real database state (first pass, read-only, unchanged since)

| Check | Result |
|---|---|
| Migrations | 001–008, nothing later; 008 applied by `postgres` at 2026-11-18 16:46:06 UTC |
| Migration files V001–V008 | SHA-256 identical to the recorded values |
| Pilot data | 20 orders (14 fulfilled, 4 cancelled, 2 payment_failed); 326 movements; the 5 pilot receipts `PO-…-2024-000001/DN…` (26 units) |
| Sessions and server | No other session on `nordiccart_oltp`; 0 prepared transactions; PostgreSQL 18.4, not in recovery |
| Pilot `--plan` checks (as the loader) | master data, opening inventory, transactions and replenishment: 0 conflicts, nothing to write |
| Scripts 01–13, 15 and 17 (as the loader) | 745 rows, 0 FAIL, 2 known WARN |

## 6. Status document

`docs/phase2_database_implementation_status.md`:
- The outdated S3 text was corrected in the first pass: S3 deployed 2026-11-18 and verified; the application connects as `nordiccart_loader`.
- The S4 entries now read: readiness complete, verdict READY, V009 not applied, deployment awaits explicit authorization.

## 7. Security note

`%APPDATA%\postgresql\` still holds a stray `pgpass.conf.txt` (62 bytes, 2026-11-18 18:09). It may contain the loader password in plain text. I did not open it. It is safe to delete, because libpq only reads `pgpass.conf`.

**Stopped.** V009 has not been applied to the real database. Deployment needs your explicit authorization.
