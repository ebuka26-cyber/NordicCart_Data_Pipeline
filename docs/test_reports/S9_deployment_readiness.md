# S9: Real historical deployment readiness

Date: 2026-11-20. **Status: S9 READY FOR EXECUTION. Stopped before any write; awaiting explicit authorization.** Nothing has been written to the real database. ChatGPT approved S9 in principle; the final prerequisite, the backup restore, passed (section 2.1).

## 1. Real database at the verified S7 baseline (read-only, as `nordiccart_loader`, `default_transaction_read_only on`)

| Check | Result |
|---|---|
| Target | `nordiccart_oltp` on localhost:5432, PostgreSQL 18.4, cluster 7663257437785384692, TimeZone UTC |
| Migrations | 001–009 |
| Content | 500 products, 2,000 prices, 20,000 customers, 8,000 listings, 20 orders, 6,134 movements, 4 POs, 69 reorder point revisions, 96 rates, change_log 43,310; latest business event 2024-12-02 |
| Engine ledger | 0 runs, 0 checkpoints |
| Sessions | no other session, no prepared transaction |
| **Engine plan on the real database** (`run_activity.py --plan`, read-only) | First window 2024-12-03: 626 units. Plan fingerprint `68af2515…`, start state `47d91ee1…` and config `678452ac…` are **identical to S8.2's first run**. The real database, engine and configuration are exactly what was rehearsed |

## 2. Fresh backup (completed)

| Item | Result |
|---|---|
| File | `database/backups/nordiccart_oltp_20261120_143552_pre_s9.dump` |
| Size and SHA-256 | 3,396,430 bytes, `F50E821962762396F44219BD0D1EB01CD0F41325B10A4C833EA9523DAE619DE5` |
| Method | Established procedure: `pg_dump -Fc` as `postgres` in a local window; the password was typed by you, never shown or stored |
| Integrity | `pg_restore --list` exit 0: custom format, gzip, dumped from and by 18.4, 611 TOC entries, 24 TABLE DATA entries (the same structure as the verified pre-S7 backup) |

### 2.1 Restore of the pre-S9 backup (final prerequisite): PASS

The backup was restored into the disposable sandbox database `nordiccart_s9_prerestore_f997c3f9` (127.0.0.1:55432, sandbox cluster 7709316403725191040). Evidence: scratchpad `s9/prerestore_check.json`.

| Check | Result |
|---|---|
| Archive | SHA-256 equals the documented value |
| Restore | `pg_restore --exit-on-error` exit 0 in 1.9 s, no errors |
| Migrations | 001–009 |
| Documented S7 baseline | All 18 counts equal: 500 products, 2,000 prices, 20,000 customers, 8,000 listings, 6,096 opening balances (19,866 units), 20 orders, 26 lines, 21 payments, 23 reservations, 6,134 movements, 4 POs and lines, 69 revisions, 96 rates, change_log 43,310 |
| **Activity ledger** | **empty**: 0 runs, 0 checkpoints; latest business event 2024-12-02 12:46 UTC |
| **Original records** against the verified S7 baseline in the sandbox (`nordiccart_s7_full`) | Orders, lines, payments, reservations, POs, PO lines and reorder point revisions are identical in every column, IDs included. Movements (6,134) and exchange rates (96): every ID and business value is identical; only `created_at` differs, on the 5,808 E2 opening balances and the 96 E1 rates. Those rows were loaded separately in the sandbox and in the real deployment, so their load timestamps differ (expected) |
| Engine `--plan` on the restore (read-only) | First window 626 units; plan `68af2515…`, start state `47d91ee1…` and config `678452ac…` **equal to the real database and to S8.2's first run** |

## 3. Blockers found, and the minimal preparation done (no database writes)

### 3.1 The engine refused the real database by design

The S6 write guard (`require_sandbox`) accepted only the sandbox cluster.

**Change:** one explicit authorization path in `python/nordiccart/activity/run.py`. The engine may write to the real database only if `NORDICCART_S9_REAL_AUTHORIZED` equals exactly `7663257437785384692/nordiccart_oltp`, and all of these hold:
- the connected server is that protected cluster and database;
- it is the `config/.env` target;
- the sandbox switch is not set, so fault injection is off.

**Unchanged:** sandbox behaviour.

**Test:** new unit test `tests/python/test_activity_real_guard.py`, 7 of 7 passed. Without the token the guard still refuses; a wrong cluster, database, port or a non-protected cluster is refused.

### 3.2 Monthly recovery points need postgres

`nordiccart_loader` cannot read all of `change_log` or the sequences (V008 privileges), so it cannot run `pg_dump`.

**Change:** `python/s9_catch_up.py` is the S8.2 driver adapted to the real target.
- **Run:** you start it in a local terminal and type the postgres password **once**. It is used only for the 23 `pg_dump` recovery points and the final read-only scripts, and is never shown or stored.
- **Engine:** writes as `nordiccart_loader` through `config/.env`, with the authorization token.
- **Per month:**
  - run `run_activity.py --apply`, which includes the month-end verification;
  - take a `pg_dump -Fc` recovery point to `database/backups/s9/` (git-ignored);
  - record its SHA-256 in the run ledger;
  - write a progress line to `logs/s9/progress.jsonl`.
- **Safety:**
  - preflight checks the exact target, the baseline and that no other session is open;
  - the driver stops at the first error and never retries automatically;
  - it is restartable from the ledger.
- **At the end:** scripts 01–18, read-only, to `logs/s9/scripts_final.txt`.

No migration, schema, policy or engine-logic change; engine `s8-1` and the configuration are the S8.2 versions.

### 3.3 Other prerequisites: no blocker

- 612 GB free on disk (about 1.3 GB of recovery points expected).
- The period ends on 2026-10-31, before today (no future-dated events).
- Disk, privileges for the ledger update (`recovery_point`: loader has UPDATE) and the advisory lock are all checked.

## 4. Execution once authorized (projection from S8.2, not measured)

- **Time:** about 55 minutes of engine time and about 5 minutes of recovery points.
- **Result:** 698 windows, 23 month-end verifications; about 71,400 orders and €25.1M fulfilled revenue; database about 1.24 GB.
- **After the run:**
  - scripts 01–18;
  - pilot `--plan` checks;
  - original-record comparison against the pre-S9 state;
  - recovery-point retention (quarter ends and the final point);
  - the S9 report.

**Recovery if a month fails:**
- the driver stops;
- nothing is retried or restored without your approval;
- the last verified month's recovery point and the pre-S9 backup are available.

## 5. Authorization requested

Please confirm explicitly that I may run `python/s9_catch_up.py` against the real `nordiccart_oltp`. It writes the historical activity 2024-12-03 to 2026-10-31 as `nordiccart_loader`, plus 23 recovery points, and you type the postgres password once in the local window.
