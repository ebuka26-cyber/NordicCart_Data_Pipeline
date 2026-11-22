# S5 real deployment readiness

**Verdict: READY for controlled S5 E1 deployment review.** November 19, 2026. All preparation checks passed. No outstanding blocker.

- Real read-only preflight as `nordiccart_loader`: localhost (::1), port 5432, database `nordiccart_oltp`, migrations exactly 001–009. There were 20 orders, 326 movements, 1,467 units on hand, 1,618 change-log rows, four received purchase orders, four lines and 69 pilot R0 revisions. Activity runs, checkpoints, exchange rates, other client sessions and prepared transactions were all zero.
- The E1 CLI ran with `--plan` and `default_transaction_read_only=on`. It exited 0 with exactly 96 rates pending. Reconciliation raised zero conflicts. Log: `logs/exchange_rates_e1_20261119_214704.log`.
- Verified backup: `database/backups/nordiccart_oltp_20261119_214751_pre_s5.dump`, **373,690 bytes**, SHA-256 `DF2CE951BEC7ACD00BAA94B38EC5F4272CFDCF3CABC4C2030A635D5F6A4DD0E7`.
- The original process left an empty file that pg_restore rejected. The local retry as postgres exited 0. Its archive passed listing before replacing the empty file. The archive header records creation at 21:56:49, database nordiccart_oltp, PostgreSQL 18.4, custom format, gzip, 611 TOC entries and 24 table-data entries.
- Full restore: `pg_restore --exit-on-error` exited 0 on a fresh template0 database at 127.0.0.1:55432. Restored counts matched the recorded live S4 baseline: migrations 001–009, 20 orders, 326 movements, 1,467 units, 1,618 change-log rows, four received purchase orders, four lines, 69 reorder-point revisions, zero activity runs, zero checkpoints and zero exchange rates. The disposable database was removed and the sandbox stopped.
- No real database writes or E1 apply were issued. S6 was not started.

## E1 real deployment: SUCCESS (November 19, 2026)

The user approved exactly 96 exchange-rate records into `localhost:5432/nordiccart_oltp`. All connections used `config/.env` as `nordiccart_loader`. No password was shown, no migration was run, and S6 was not started.

**Pre-apply checks:**
- 22:11 recheck: the pre-S5 backup is unchanged (373,690 bytes, SHA-256 `DF2CE951…DD0E7`). The state matches the readiness record: migrations 001–009; change_log 1,618 rows (max id 1836); 0 exchange rates; no other sessions or prepared transactions.
- 22:32:31 read-only `--plan`: same identity (`nordiccart_loader@localhost:5432/nordiccart_oltp`), **96 pending, exit 0**.
- Exit 0 also means 0 conflicts. The plan raises an error on any conflicting stored rate, unexpected row, stored-history gap or changed plan.

**Execution log:**

| Time | Command | Result |
|---|---|---|
| 22:32:39 | `--apply`, answer piped from PowerShell | **Refused at the confirmation prompt; nothing written.** PowerShell had put a UTF-8 byte-order mark in front of the piped answer (`'﻿nordiccart_oltp'`), so the safeguard rejected it. A read-only check confirmed: 0 rates, change_log 1,618 |
| — | — | Reported to the user, who **approved one more run** with clean input |
| 22:34:51 | read-only `--plan` | 96 pending, exit 0 |
| 22:35:00 | `cmd /c "echo nordiccart_oltp\| python -B python\run_exchange_rates.py --apply"` | Confirmation accepted. **`Committed 96 E1 rows`, exit 0.** One transaction |
| 22:35:19 | read-only `--plan` rerun | **0 pending**, exit 0 |

The answer was checked as plain `'nordiccart_oltp\n'` with a harmless probe before the run. Logs: `logs/exchange_rates_e1_20261119_221157.log`, `…_223231.log`, `…_223239.log`, `…_223451.log`, `…_223500.log` and `…_223518.log`.

**Post-deployment verification** (read-only, as `nordiccart_loader`):

| Check | Expected | Actual |
|---|---:|---:|
| Exchange-rate records | 96 | **96**: EUR/SEK, EUR/NOK and EUR/DKK; 32 days, 2024-11-01 to 2024-12-02; source `synthetic_ecb` |
| Change-log rows | 1,714 | **1,714**: +96, all `exchange_rates`; max id 1836 → 1932, contiguous |
| Orders, movements, units on hand | 20 / 326 / 1,467 | **20 / 326 / 1,467**. Reserved still 0 |
| Migrations | 001–009 | 001–009, unchanged |
| Read-only plan rerun | 0 pending | **0** |
| Script 16 | 4 PASS, 0 FAIL | **4 PASS, 0 FAIL** |
| Scripts 01–13 and 15–18 | 0 FAIL | 843 rows: 675 PASS, 165 INFO, **0 FAIL**. 3 known WARN: 2 in script 07, 1 in script 10 |
| Script 14 | — | Not run: the loader may not read `change_log` row contents (V008 design). It passed after E1 in the sandbox. To run it on the real database, use `postgres` or `nordiccart_analyst` |

Every requested check matches. **Verdict: SUCCESS.** The pre-S5 backup was not needed or used.
