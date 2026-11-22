# Phase 3, step 5, stage S4: V009 activity control and purchase orders (sandbox test report)

Date: 2026-11-18. Updated 2026-11-19 after the S4 review (section 10).

**Review update.**
- ChatGPT's concurrency finding (two concurrent first windows) was **reproduced and fixed** in V009.
- The figures in this report are those of the fixed V009.
- The reviewed V009's results are kept in section 10.

Design: `docs/phase3_combined_activity_design.md`, revision 2 (approved):
- limitations L1 (purchase orders), L2 (run ledger and checkpoints, sections 7.2 and 7.3) and L10;
- decisions C15 and C18;
- exception E3;
- stage S4 in section 12;
- decision C17 still holds (script 14 scope).

Scope: S4 only.
- V009 is implemented and tested in a disposable PostgreSQL 18.4 sandbox. **V009 was not applied to the real database**, and S4 did not connect to the real database.
- V001–V008 are unchanged (SHA-256, section 2).
- No pilot record was altered; credentials and authentication are unchanged; nothing of S5 or later was started.

## 1. Result

| Item | Result |
|---|---|
| V009 migration | Implemented: one transaction, run as postgres with `SET ROLE nordiccart_owner`, additive, with a self-check. Applies to the real state in about 0.2 s and writes no business row |
| Purchase orders (requirement 1) | Orders and lines record ordered, received and outstanding quantities, the reason and the status. Received quantities and statuses are derived from the ledger's `<PO>/DN<n>` receipts by a SECURITY DEFINER trigger |
| Run ledger and checkpoints (requirement 2) | Contiguous, non-overlapping windows, chained by a unique predecessor link: one first run, at most one successor per run, race-free in every isolation level (section 10). Immutable fingerprints; running → verified/failed, failed → running; verified only with its checkpoint in the same transaction; insert-only checkpoints |
| Reorder point revision history (requirement 3) | Insert-only and chained in business time; must match the balance at commit. The 69 R0 pilot revisions are represented exactly: old and new values, business time, reference `R0-<warehouse>-20241115` |
| Constraints, indexes, FKs, triggers, change capture (requirement 4) | 46 named constraints, 10 indexes, 26 triggers on the new tables (4 deferred) and 1 receipt trigger on `inventory_movements`, change capture on the 3 business tables. Structure verified by script 18 group 1 and the catalog test |
| Security (requirement 5) | Privilege matrix **1,616 / 1,616** entries as designed. Loader: INSERT only, no change of documents or history (42501). Guards also bind the owner |
| Pilot documents without duplicating movements (requirement 6) | The loader backfills the 4 purchase orders, their lines and the 69 R0 rows **in one transaction with no movement**: business data byte-identical, +81 change_log rows. A rerun writes nothing. A fresh load writes the same documents inside its units |
| Automated tests (requirement 7) | New: SQL suite **96/96**, Python database suite **68/68** (including 3 concurrent-start regression tests). Full Python suite **491 passed, 0 failed, 0 skipped**. SQL suites on V009: 44/44, 119/119, 105/105, 87/87, 96/96. Concurrency 14/14 on V009 as the loader |
| C18 demonstrated | After a later monthly review of 18 warehouse reorder points, the loader and script 14 still verify R0 on V009. The same change on V008 breaks both |
| Mutation checks | **15 of 15 planted defects caught**: 2 by V009's own self-check (the migration refuses to commit), 13 by the tests. M15 removes the run-chain fix, and the new regression tests catch it |
| Real-state rehearsal | On a copy of the real state, following the guide (rerun with the fixed V009):<br>- V009 in **195 ms**, business data unchanged;<br>- loader `--plan` shows 8 documents, `--apply` writes them in 1.2 s (81 change_log rows, no movement), a rerun has nothing to write;<br>- scripts 01–15, 17 and 18 as the analyst: **883 rows, 0 FAIL**;<br>- R009 restores the V008 catalog exactly (963 entries) and V009 re-applies |
| Defects found and fixed during S4 | 2 in V009:<br>- guard order (section 7);<br>- review finding: concurrent first windows (section 10).<br>Plus test-side corrections |
| Open points | Section 9 |

## 2. Files

| File | Status | Content |
|---|---|---|
| `database/migrations/V009__activity_control_and_purchase_orders.sql` | new | The migration after the review fix: 60,545 bytes, SHA-256 `65A4EA6D878A87168BEBE9C146A0AE6C101D18582727A5317AB5D33216EF5B17`. The reviewed version was `EE9490509B7D8BA3…`, 59,098 bytes |
| `database/rollback/R009__undo_activity_control_and_purchase_orders.sql` | new | In-place rollback (refuses once an activity run exists) |
| `database/design/nordiccart_oltp_rev4.dbml` | new | DBML revision 4 = revision 3 + the V009 objects (revision 3 unchanged) |
| `sql/verification/18_verify_v009_activity_control.sql` | new | V009 verification: 44 rows (check 703 also verifies the predecessor links since the fix) |
| `tests/sql/test_v009_activity_control.sql` | new | 96 behaviour tests, rolled back. 126 and 127 were added with the fix |
| `tests/python/test_v009_db.py` | new | 68 database tests (opt-in, own databases, disposable server only), including the 3 concurrent-start regression tests added with the fix |
| `docs/phase3_v009_deployment_and_rollback_guide.md` | new | Deployment, pilot document backfill, verification, rollback |
| `python/nordiccart/units/base.py` | changed | `has_activity_control(conn)` |
| `python/nordiccart/units/stock.py` | changed | With V009: R0 writes its revision rows; a receipt first ensures its purchase order document; the document writers are also the E3 backfill |
| `python/nordiccart/replenishment/model.py` | changed | Property `ReorderPointRevision.reference`. It is not a field, so the plan fingerprint is unchanged (`858bbfd0…`) |
| `python/nordiccart/replenishment/load.py`, `cli.py` | changed | With V009: R0 verified from the history; later revisions accepted when chained; order documents verified; missing documents reported and written by `--apply` in one transaction |
| `sql/verification/14_verify_replenishment.sql` | revision 3 | C18: reorder points as of the pilot's last event (history when present); same 44 rows |
| `sql/verification/17_verify_v008_security.sql` | revision 2 | V008 exact lists limited to V001–V008 objects; same results on V008 |
| `tests/sql/test_v008_roles.sql` | revision 2 | Test 2's exact SECURITY DEFINER list limited to V001–V008; same 87 tests |
| `tests/python/test_v007_concurrency.py` | changed | `--through 9` |
| status document, `tests/README.md`, `sql/README.md`, `database/README.md` | changed | S4 recorded |

Unchanged:
- **V001–V008:** SHA-256 identical to the recorded values. First 16 hex digits:

  | Migration | SHA-256 (first 16 hex) |
  |---|---|
  | V001 | `D3349651A5CAF2CF` |
  | V002 | `14BBCD1F4EC19447` |
  | V003 | `ED76162656C04E92` |
  | V004 | `88D0A52D422405BB` |
  | V005 | `02320CF0FB4507AF` |
  | V006 | `A8225D72CF6B4151` |
  | V007 | `DCD79FF76BABDF36` |
  | V008 | `444F116FEFF9A37D` |

- **Other S3 files:** R008, the reapply script.
- **Configuration:** `config/.env` (last modified 18:25:58 by you); `config/generator.toml`.

## 3. Environment and starting state

- **Sandbox:** PostgreSQL 18.4 on port 55432, separate from the real server; Python 3.14.7.
- **Starting state "the latest verified database state"** (`nordiccart_oltp_s4_base`):
  - the S3 readiness backup (`nordiccart_oltp_20261118_163550_pre_v008.dump`, SHA-256 `090AA80A…`, restored byte-identical);
  - plus the approved V008, applied exactly as on the real server.
- **Compared with the real database's post-deployment snapshot:**
  - 18 of 19 tables byte-identical; `meta.schema_migrations` differs only by V008's `applied_at`;
  - privileges, owners and database settings identical (only the listing order differs).
- **Why not a fresh dump of the real database:** `config/.env` now holds only the loader, which cannot dump. A fresh dump would need the `postgres` password, which was not used in S4.
- **Python tests:** they build their own templates with the existing loaders:
  - V001–V008 with both pilots;
  - the same plus V009;
  - an empty V001–V009.

## 4. Test results

### 4.1 Full Python suite

`NORDICCART_DB_TESTS=1 python -m pytest tests/python` gives **491 passed, 0 failed, 0 skipped in 405.1 s**:
- 423 before S4;
- 68 V009 tests.

The run before the fix gave 488 passed, with the reviewed V009 and 65 V009 tests.

Every earlier suite still passes, including the V008 suite on V008 databases and the pilot loaders on V007 databases.

### 4.2 V009 database tests: 68 passed

| Area | Tests | What is proven |
|---|---|---|
| Migration | 3 | Every row unchanged and `applied_by` = postgres; refuses a second run, a database without V008 and a non-superuser session; a planted forbidden privilege makes the self-check roll back everything (catalog identical) |
| Privilege matrix | 1 | 8 grantees × 5 tables × every column × SELECT/INSERT/UPDATE, table DELETE/TRUNCATE/REFERENCES/TRIGGER, and 14 functions: **1,616 entries, 0 differences** (56 columns since the fix) |
| E3 / C18 backfill | 1 | Plan: 8 documents to write. Apply as the loader: 4 orders, 4 lines, 69 R0 rows equal to the plan; 81 change_log rows; **business data byte-identical** (no movement). Rerun: 0 written. Transaction pilot still verifies |
| Fresh load on V009 | 1 | All pilots loaded as the loader on an empty V009: 17 replenishment units, 0 documents left to backfill; data **and** documents identical to the backfilled database |
| C18 | 1 | Monthly review of 18 SE warehouse listings (revision + update): the loader shows 0 conflicts and "later reorder point revisions 18"; script 14 44/37/7/0; script 18 44/38/6/0. On V008 the same change makes the loader conflict and script 14 FAIL 111 |
| Conflicting documents | 1 | A pilot order with other lines is a conflict; nothing is written |
| Interrupted runs | 6 | Crash inside the backfill: all 8 documents rolled back, rerun completes. A fresh load stopped after 2, 9, 13 and 15 units resumes with no duplicate documents (4/4/69). Run protocol: a crash during step 7 leaves the run running with no checkpoint; a changed fingerprint is refused (loader 42501, owner 23514); the rerun completes; gaps refused, the next window accepted |
| Duplicates and concurrency | 4 | Duplicate order (23505), delivery note line (23505), over-receipt (23514), revision document (23505). Two concurrent receipts of 2 against a line of 3: the second waits, then is refused (23514); received 2, outstanding 1. Two backfills: the advisory lock refuses the second (LoaderBusyError); without the lock the second insert of a pilot order fails with 23505 and the loader completes the other 7 documents. Two starts of one window: the second waits, then 23P01 |
| Concurrent starts (review finding, section 10) | 3 | Disjoint first windows started concurrently, empty ledger, READ COMMITTED: the second waits, then 23505 `uq_activity_runs_previous`. The same with the second session in REPEATABLE READ and an older snapshot: 23505. Next windows after a verified run: the second waits, then is refused (23505 or 23P01). Each time one first run and a linear chain remain, and script 18 shows no run-ledger FAIL |
| Unauthorized changes | 20 | 19 loader statements refused with 42501 (documents, history, run ledger, checkpoints, protected functions, disabling the receipt trigger, DROP); the guards bind the owner (23514 / 23000) |
| Rollback | 2 | V009 + backfill + R009 gives a catalog identical to V008 (tables, columns, constraints, triggers, indexes, functions with source hashes, enums, ACLs, default ACLs, migrations) and identical business data; V009 and the backfill re-apply. R009 refuses without V009 or once a run exists |
| Regression on V009 | 25 | SQL suites 5; scripts 01–15, 17, 18 as the analyst 17; script 18 before the backfill fails exactly 301, 401–406, 501–503; both command lines as the loader (plan 8 documents → apply → nothing to write); 14 concurrency scenarios as the loader |

### 4.3 SQL suite `tests/sql/test_v009_activity_control.sql`: 96 tests, 0 failed

| Group | Tests |
|---|---|
| Catalog | 3 |
| Setup | 1 |
| Purchase orders and receipts | 16 |
| Order rules | 10 |
| E3 backfill | 7 |
| Loader refusals | 9 |
| Owner guards | 6 |
| Revision history | 10 |
| Run ledger | 28 (126: the chain set by the guard; 127: a predecessor link cannot be changed, owner included) |
| Roles and change capture | 5 |
| Safety (everything rolled back) | 1 |

### 4.4 SQL suites on V009

All on the backfilled V009 database:

| Suite | Result |
|---|---|
| Foundation | **44/44** |
| V006 | **119/119** |
| V007 | **105/105** |
| V008 (revision 2) | **87/87**. Revision 2 also gives 87/87 on V008 |
| V009 | **96/96** |

V007's receipts with non-PO references (`NC-TEST-DN-…`) and its direct reorder point updates still pass: V009 is additive.

### 4.5 Concurrency

`test_v007_concurrency.py --recreate --through 9 --role nordiccart_loader` gives **14 PASS, 0 FAIL**. The receipt trigger is in place, and the scenarios' receipts keep V007 behaviour.

### 4.6 Verification scripts

| Scripts | V008 base | V009 right after the migration | V009 after the backfill |
|---|---|---|---|
| 01–10 (as analyst) | 552 rows, 0 FAIL, 2 WARN (script 07: the V007 triggers on V006 tables) | 0 FAIL; the new V009 objects appear as INFO; **1 new expected WARN** in script 10: V009's receipt trigger on `inventory_movements` (a V007 table), verified by script 18 check 105 | same |
| 11–15 (as analyst) | 0 FAIL | unchanged | unchanged |
| 14 | 44: 37 PASS, 7 INFO | same | same |
| 17 (revision 2) | 31: 27 PASS, 4 INFO | same | same |
| 18 | n/a | 44: 28 PASS, 6 INFO, **10 FAIL** (301, 401–406, 501–503: documents not written yet, by design) | **44: 38 PASS, 6 INFO, 0 FAIL** |

## 5. Real-state rehearsal (deployment guide sections 1–6)

Script `s4_rehearsal.py` (scratchpad), on a fresh copy of the rebuilt real state (`nordiccart_oltp_s4_rehearsal`, kept for inspection). Commands as in the guide: psql with `ON_ERROR_STOP=1`; the loaders as `nordiccart_loader`; the scripts as `nordiccart_analyst`.

| Step | Result |
|---|---|
| Preflight | Migrations 001–008, 7 roles, no V009 table, change_log 1,537. Both pilot `--plan` checks: 0 to write, 0 conflicts |
| V009 (psql) | Exit 0, **195 ms** with the fixed V009 (197 ms with the reviewed version); output ends `INSERT 0 1`, `COMMIT`; stderr empty |
| Right after V009 | Script 17: 31/27/4/0. Script 18: 44 rows, 28 PASS, 6 INFO, **10 FAIL** (301, 401–406, 501–503, as expected). Business data unchanged; change_log 1,537 |
| `run_replenishment.py --plan` | `to write 8` (4 purchase orders, 4 R0 revision sets); 0 conflicts |
| `run_replenishment.py --apply --yes` | Exit 0, 1.2 s: "4 purchase orders with their lines, 69 R0 revision rows (no movement written)", 81 change_log rows, 17 units verified |
| Rerun `--plan` | purchase orders 4/4, R0 revisions 4/4, to write 0. Transactions `--plan`: 0 to write |
| Scripts 01–15, 17, 18 (analyst) | **883 rows: 709 PASS, 171 INFO, 0 FAIL, 3 WARN** (script 07: 2 known; script 10: 1 expected, see 4.6). Script 18: **44/38/6/0** |
| Business data | Orders, lines, payments, reservations, movements, balances and master data **byte-identical**; change_log 1,537 → 1,618; documents 4 orders, 4 lines, 69 revisions, all orders `received` |
| Rollback rehearsal (second copy) | V009 (191 ms) + backfill, then **R009 (132 ms)**. NOTICE: "drops 4 purchase orders, 4 order lines and 69 reorder point revisions". **Catalog identical to V008 (963 entries)**, business data identical. V009 (189 ms) and the backfill re-apply cleanly |

The rehearsal was run again in full with the fixed V009 on 2026-11-19. Every result above is the same as with the reviewed version; only the timings changed.

## 6. Mutation checks

Each defect was planted in a temporary copy of V009, R009 or script 14, or as an in-process patch of the loader code. Project files were not changed. The V009 suite ran against each one, without the concurrency subprocess.

| Mutation | Caught by |
|---|---|
| M1 receipt trigger function not SECURITY DEFINER | **V009 self-check** ("SECURITY DEFINER or search_path not as designed: fn_purchase_receipt_apply()") |
| M2 writer may UPDATE `quantity_received` | **V009 self-check** ("privileges on the new tables not as designed") |
| M3 receipt may exceed the ordered quantity | SQL suite (test 26: rule `purchase_receipt_within_ordered`) |
| M4 revision chain not checked | SQL suite (test 82) |
| M5 revision not checked at commit (deferred trigger removed) | SQL suite (84); script 18 structure (C18 test, scripts test, script 18 before-backfill test) |
| M6 run verified without checkpoint (deferred trigger removed) | SQL suite (106); script 18 structure |
| M7 windows need not be contiguous | Run protocol test; SQL suite (115) |
| M8 backfilled line ignores its receipts | Backfill test; conflicting-documents test |
| M9 run fingerprint may change | Run protocol test (owner 23514); SQL suite (103) |
| M10 order number of any market | SQL suite (41) |
| M11 R009 keeps the receipt trigger | Rollback test (R009 fails: the trigger still depends on its function) |
| M12 script 14 reads the current reorder points (C18 undone) | C18 test (script 14 FAILs after a monthly review) |
| M13 loader ignores the R0 history (C18 undone) | Backfill test; interrupted-backfill test |
| M14 receipt unit does not write its purchase order (E3 undone) | Backfill test; interrupted-backfill test |
| M15 run chain not unique (the review fix removed: `uq_activity_runs_previous`) | The new concurrent-start tests. Both empty-ledger cases fail with "the second start must wait": session B commits a disjoint first window at once, which is the reproduced race. The non-empty case is still refused by the exclusion constraint and fails only on script 18's structure count |

## 7. Defects found and fixed during S4

| # | Found by | Defect | Fix |
|---|---|---|---|
| 1 | V009 database test | On a database without V008, V009 switched to the owner role before checking its prerequisites, and failed with "permission denied for schema meta" instead of a clear message | The guard checks V008 (owner role and migration 008) before `SET ROLE`; the message is now "Migration V009 requires V008" |
| 2 | First sandbox runs (test side) | `regclass::text` drops `public.` in script 18's table filter; a `"char"` concatenation in the catalog query; a duplicate-revision probe refused by the chain guard before the unique key; test 55 needed splitting, because complete and partial backfills are refused by different rules (both correct) | Corrected; each now checks the intended rule |
| 3 | Scope review | Script 17 and the V008 SQL suite pinned V008's exact lists ("exactly three SECURITY DEFINER functions", "16 writer tables") and would fail after any later migration | Revision 2: those lists are limited to V001–V008 objects; the global rules still cover everything; results on V008 unchanged |
| 4 | **ChatGPT S4 review** | Concurrent first windows could fork the run chain (section 10) | `previous_run_id` + `UNIQUE NULLS NOT DISTINCT` (section 10) |

## 8. Design decisions taken in S4 (for review)

1. **Additive enforcement.** V009 fully checks every write that refers to its own documents: receipts with `PO-…/DN<n>` references, revision rows and run rows. It does not refuse writes V007 accepted:
   - receipts with other references;
   - reorder point updates without a revision.

   Those are reported instead by script 18 (301, 601) and by the replenishment loader. The reason is that a strict rule would change V007 behaviour and its approved regression suites. The engine (S6) always writes `<PO>/DN<n>` receipts and revision rows. A strict rule can be added in a later migration once the engine is the only writer.
2. **Received quantities and statuses are derived, not written.** Lines added after their receipts (E3) take the received quantity from the ledger; supplied values are replaced. Writers have no UPDATE privilege; owner updates must equal the ledger.
3. **Run windows are contiguous.** A new run must start where the last verified run ended. That run must be verified, so a failed run must be resumed first. An exclusion constraint prevents concurrent overlaps. Since the review fix, a unique predecessor link also prevents concurrent disjoint first windows (section 10). The window length (one UTC day for catch-up, one hour live) is left to S6/S10 and not fixed in the database.
4. **Meta tables are not captured in change_log.** Runs and checkpoints are engine metadata; the business documents are captured.
5. **The R0 document reference** is `R0-<warehouse>-<UTC date>`, for example `R0-SE-WH-01-20241115`. It is derived from the plan, so the plan fingerprint is unchanged.
6. **The pilot orders' document time.** In a fresh load the order document is written by the first receipt unit; its `ordered_at` stays the plan's 2024-11-18. On the real database it is written by the backfill (E3). In both cases it is a document, not a stock event.

## 9. Open points

1. **Deployment** needs your explicit authorization. It follows `docs/phase3_v009_deployment_and_rollback_guide.md`:
   1. preflight;
   2. pre-V009 backup as `postgres`;
   3. V009;
   4. script 18 (10 expected FAILs);
   5. `run_replenishment.py --apply` as the loader for the 8 pilot documents;
   6. script 18 at 38/6/0.
2. **The sandbox starting state was rebuilt**, not dumped from the real database: the pre-V008 backup plus the approved V008 (section 3). A fresh pre-V009 backup at deployment time must be taken as `postgres`, with its password typed at the prompt.
3. **The strict "every receipt has a purchase order" rule** is deferred (section 8.1). Script 18 check 301 enforces it as verification.
4. **Run window length and hash canonicalization** (how the engine computes `state_hash`) are S6 decisions. V009 stores and compares the hashes but does not compute them.
5. **R009 cannot remove the change_log rows** captured for the V009 documents. change_log is insert-only, so they remain after a rollback.

## 10. Review finding (2026-11-19): concurrent first windows in the run ledger

**Finding (ChatGPT).** `meta.fn_activity_runs_guard()` reads the latest run without serialization. With an empty ledger, two concurrent sessions could each insert a disjoint first window. The overlap constraint does not stop them, so the contiguity rule is bypassed.

### 10.1 Reproduced on the reviewed V009 (SHA-256 `EE9490509B7D8BA3…`)

Script `s4_race_repro.py` (scratchpad), on fresh sandbox copies of the real state with the reviewed V009 and an empty ledger. Both sessions ran as `nordiccart_loader`:
- session A inserts the first window 2024-12-03 and keeps its transaction open;
- session B inserts the disjoint first window 2024-12-10.

| Session B isolation | Result |
|---|---|
| READ COMMITTED | B was **not blocked** ("still waiting after 1.5 s: False") and **committed in 0.01 s**. Ledger: 2 running runs (2024-12-03 and 2024-12-10), **contiguity broken (1 gap)** |
| REPEATABLE READ (B's snapshot taken before A commits) | B **committed** in 0.52 s. Same result: 2 runs, 1 gap |

**Conclusion: confirmed.**
- The race exists only for the **first** window.
- With at least one verified run, every new window must start at that run's end, so two concurrent starts overlap and the exclusion constraint already refuses the second. The regression test confirms this (non-empty case).
- The REPEATABLE READ case also shows that a lock taken inside the guard would not be enough: B's snapshot predates A's commit, so B's read still finds an empty ledger.

### 10.2 Fix (smallest safe change, V009 only)

| Change | Effect |
|---|---|
| New column `meta.activity_runs.previous_run_id`, set by the guard on insert to the latest run (NULL for the first), immutable (added to the guard's identity check), with FK `fk_activity_runs_previous` | Each run names its predecessor; a caller's value is replaced |
| `CONSTRAINT uq_activity_runs_previous UNIQUE NULLS NOT DISTINCT (previous_run_id)` (PostgreSQL 15+; V009 requires 16+ through V008) | **At most one run without a predecessor, and at most one successor per run.** An index decides, not the guard's read. A concurrent second start waits on the first one's index entry and then fails with 23505, in any isolation level |

Everything else is unchanged: no new function, privilege, role or table. The writer already had INSERT on the table, and `previous_run_id` is not updatable. The fix also touched:
- **script 18:** the column and constraint counts of checks 102 and 104 updated (activity_runs 20 columns, 15 constraints); check 703 also verifies that every predecessor link names the previous window. Still 44 rows.
- **DBML revision 4.**
- **tests:** SQL suite tests 126 and 127; Python matrix 1,592 → 1,616 entries; 3 new concurrent-start regression tests.

New V009: 60,545 bytes, SHA-256 `65A4EA6D878A8716…`.

### 10.3 Verification of the fix

| Check | Result |
|---|---|
| Same reproduction on the fixed V009, READ COMMITTED | B **waits** ("still waiting after 1.5 s: True"), then is refused with **23505 `uq_activity_runs_previous`** after A commits. Ledger: 1 run, no gap |
| Same reproduction, REPEATABLE READ (stale snapshot) | B refused with **23505 `uq_activity_runs_previous`**. Ledger: 1 run, no gap |
| New regression test `test_concurrent_disjoint_starts_cannot_fork_the_run_chain` | 3 cases pass: empty ledger in READ COMMITTED, empty ledger in REPEATABLE READ, next windows after a verified run. Each requires that the second session waits, is refused, and leaves one first run, a linear chain and no run-ledger FAIL in script 18 |
| The regression test catches the original defect (mutation M15: unique constraint removed) | Caught: both empty-ledger cases fail ("the second start must wait for the first one's uncommitted row") |
| V009 database suite | **68 passed** |
| SQL suites on V009 | 44/44, 119/119, 105/105, 87/87, **96/96** |
| Full Python suite (all opt-in database tests) | **491 passed, 0 failed, 0 skipped** (405.1 s) |
| Concurrency scenarios as the loader on V009 | 14/14 (part of the full suite) |
| Real-state rehearsal, rerun | Same results; V009 195 ms; script 18 38/6/0 after the backfill; R009 catalog identical (963 entries) |

**Stopped as instructed:**
- V009 has not been applied to the real database;
- the real database was not touched;
- the scope is unchanged.
