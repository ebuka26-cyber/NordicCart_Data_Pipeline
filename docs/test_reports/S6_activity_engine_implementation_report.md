# S6 activity engine, milestone 1 · implementation report

November 19, 2026. **Ready for review: sandbox-verified, 14 of 14 tests passed.**

Scope:
- Built: the chronological engine and run protocol of design sections 7.1–7.3, at pilot scale, in the sandbox.
- Not done:
  - no migration;
  - nothing written to the real database (it was read once, read-only, as the loader, to confirm that the sandbox base equals it);
  - no full historical generation.

## 1. What the engine does

One discrete-event simulation combines the following, in business-time order:
- customer orders from the S2 demand model, with their payments, reservations and fulfilment;
- daily exchange rates (X1, from S5);
- store transfers (T1/T2);
- warehouse purchase orders (V009 documents);
- supplier receipts (R1).

It runs in UTC day windows. **Every write is an existing unit of work** (`nordiccart.units` U1–U5, T1, T2, R1, `ensure_purchase_order`, and `exchange_rates.reconcile`), so every V006–V009 rule and trigger runs on every commit.

| Design point | Implementation |
|---|---|
| State lives in the database (7.2) | `state_as_of(conn, t)` reads only rows dated before *t*. It covers:<ul><li>balances (on hand, reserved, and the reorder point via V009 revisions);</li><li>transfers in transit;</li><li>open purchase order lines (ordered minus `<PO>/DN<n>` receipts);</li><li>open orders and their payment attempt states;</li><li>document numbering;</li><li>row counts;</li><li>exchange rates.</li></ul>It uses business keys only, and is hashed with SHA-256. Every business time is the time of the unit of work that wrote the row, so any past instant can be recomputed later. |
| Deterministic, composable plan (7.2) | The window plan is the replay from the engine origin (2024-12-03 00:00 UTC), starting from `state_as_of(origin)`, cut to [start, end).<br>Each random choice has its own seeded stream, keyed by business keys (customer/month/order index, store/day, PO number).<br>So plan(day 1) + plan(day 2) = plan(days 1–2), which is tested both in memory and in the database. |
| Run protocol (7.3) | 1. Session advisory lock `nordiccart.activity`.<br>2. The last run decides the next window: resume a `running` or `failed` run, or start the next day.<br>3. Drift checks: the history before the origin still has the first run's start hash; the state at the window start equals the previous checkpoint and the replay.<br>4. Run row `running`, with plan and config fingerprints; on resume, the fingerprints must be identical.<br>5. Units of work, each in its own transaction that also advances `completed_units` (the prefix rule).<br>6. Verification: all units done; the state at the window end equals the replay; ledger identity; no business time at or after the end.<br>7. One transaction: the checkpoint snapshot and hash, and the run `verified`. |
| Ranks (7.1) | X1 0, R1 3, T2 4, T1 5, order units 6, purchase order 7. Ties are broken by key. |
| CLI | `python/run_activity.py --plan` (read-only) or `--apply --windows N [--yes]`.<br>Exit codes: 0 done, 1 verification failed, 2 drift (blocking), 3 configuration, 4 lock busy.<br>**`--apply` refuses to run unless `NORDICCART_S6_SANDBOX=1` is set** (milestone 1 is sandbox-only). |

## 2. Sandbox base

- `nordiccart_s6_base`, on port 55432, is the verified pre-S5 backup (`…_214751_pre_s5.dump`, SHA-256 `DF2CE951…DD0E7`), restored, with E1 then applied as `nordiccart_loader`.
- Compared with the real database: **22 of 22 tables identical on business columns, plus identical change_log metadata** (1,714 rows, max id 1932).

## 3. Test results

Command: `python -B -m pytest tests/python/test_activity_engine.py -v` with `NORDICCART_S6_SANDBOX=1`.
- **Result: 14 passed, 0 failed, in 69 s.**
- Each test runs on its own clone of the base. The CLI runs as `nordiccart_loader`; the checks run as `nordiccart_analyst`.

| Test | What it proves | Result |
|---|---|---|
| `test_plan_is_deterministic` | Same seed and config give the same plan fingerprint | PASS |
| `test_window_composition_in_memory` (×2 volumes) | Per-day plans joined = the 7-day plan; the boundary state does not depend on the horizon; 2-day plan = day 1 + day 2 | PASS |
| `test_december_windows_execute_verify_and_keep_the_pilot` (×2 volumes) | **Execution**: 29 windows (Dec 3–31) are all `verified`. Each run's plan fingerprint and end hash equals the replay, and equals its checkpoint.<br>**Pilot intact**: pilot records (orders 1–20 and their lines, payments and reservations; every movement, purchase order, revision and rate before the origin) are byte-identical; both pilot `--plan` checks show 0 conflicts and 0 blocking.<br>**Scripts 01–18 as the analyst**: 0 FAIL.<br>**Stress volume**: every unit kind occurs, and orders end fulfilled, cancelled, payment_failed and confirmed (still open) | PASS |
| `test_plan_writes_nothing_and_finished_windows_never_rerun` | **Idempotency**: `--plan` changes neither business data nor the ledger and offers the next day; the run ledger refuses a repeat of a finished window | PASS |
| `test_crash_and_resume_end_identical` (×4) | **Interruption and recovery**: the process ends abruptly (`os._exit`) in window 4 (75 units) at four points:<ul><li>inside unit 30 (rolled back; 29 done);</li><li>after unit 30;</li><li>before verification;</li><li>inside the checkpoint transaction.</li></ul>The rerun resumes with the exact number of done units, executes no unit twice, and finishes the 7 windows. Run ledger (fingerprints, hashes, snapshots), business records and change_log count are **identical to the uninterrupted reference** | PASS |
| `test_failed_run_is_resumed` | A unit failing with SQL errors (privilege revoked temporarily) marks the run `failed` with its reason; the next start resumes it; the result is identical to the reference | PASS |
| `test_drift_blocks_the_next_window` | A manual reorder-point change, and a stray future exchange rate, each block the next window with exit 2. The message names the changed listing. No run row and nothing is written | PASS |
| `test_one_two_day_window_equals_two_one_day_windows` | One 2-day window gives the same database state hash, business records and checkpoint hash as two 1-day windows | PASS |
| `test_second_engine_is_refused` | While the activity lock is held, a second engine exits 4 and writes nothing | PASS |

Regression: the existing Python suite (no database flags) gives 258 passed and 256 skipped.

## 4. December 2024 demonstration

Persistent sandbox databases `nordiccart_s6_demo_representative` and `nordiccart_s6_demo_stress`.

| | Representative (config, λ̄ = 2.4) | Stress (sandbox config, λ̄ = 36) |
|---|---:|---:|
| Windows run (all `verified`) / units of work / time | 29 / 94 / 3.3 s | 29 / 1,332 / 9.2 s |
| Orders placed (from order 21) | 11 | 232 |
| Order outcomes | 11 fulfilled | 215 fulfilled, 9 payment_failed, 7 cancelled (3 customer, 4 expired), 1 confirmed (ships 2025-01-01) |
| Payment attempts (failed) | 11 (0) | 231 (15) |
| Transfers (T1 + T2) | 5 | 68 |
| Purchase orders and receipts | 0 POs, 0 receipts | 13 POs (6 received, 7 open), 6 receipts |
| Exchange rates | 96 → 183 | 96 → 183 |
| change_log | 1,714 → 2,014 | 1,714 → 6,151 |
| Stock (units on hand / reserved) | 1,451 / 0 | 1,437 / 1 |
| Lost demand (availability-aware customers) | 0 orders | 1 order, 1 unit |
| Ledger identity breaks; negative or oversold balances | 0; 0 | 0; 0 |
| Pilot records; pilot `--plan` checks | identical; 0 conflicts | identical; 0 conflicts |
| Scripts 01–18 (analyst) | 712 PASS, 172 INFO, **0 FAIL**, 3 known WARN | 712 PASS, 172 INFO, **0 FAIL**, 3 known WARN |

At representative volume (100 pilot customers), December has only 11 orders. The stress config exists only to exercise every path, and only in the sandbox.

## 5. Implementation choices for review

1. **Replay from the origin** for every window. It is simple and exactly composable, but its cost grows with the history (December: 0.1–0.3 s per window). The 23-month catch-up (S8/S9) will need incremental replay from the last checkpoint.
2. **Stores** are reviewed at 05:30 local on warehouse working days (Mon–Fri), and dispatched the same morning (06:00–07:00 local, before online ordering opens). So no request is pending across a window. Units allocated to a transfer are held until dispatch.
3. **Warehouses** are reviewed on Mondays: natural purchase orders (reorder_point or launch), rounded up to the case pack, within storage capacity, with one delivery note after the supplier lead time. The PO document is written at the review (status open); R1 later checks the receipt against it.
4. **(s, S) parameters:**
   - s is the listing's reorder point in the database;
   - S comes from the opening model, as in the replenishment pilot.
   - The monthly R-cal review and P1 price changes are not in this milestone (the first R-cal day is 2025-01-02).
5. **Availability-aware customers (C9):** a short line is reduced or dropped when the order is placed; an order with no line left is lost demand. The engine therefore never plans a U2 out_of_stock.
6. **Demand before the origin:** demand draws on local days 1–2 December fall before the origin and are not placed.
7. **Origin conditions:** the origin must have no open orders, transfers or purchase orders (true for the pilot history).
8. **Payment state at a boundary** uses the unit time (captured_at or failed_at). An authorization 1–2 seconds before a midnight capture therefore counts as pending.

## 6. Open items

Not started: S7 and later, and any real-database run of the engine.

S6 items still open:
- monthly verification and recovery points (7.4);
- the R-cal monthly review;
- pilot loaders refusing `--apply` while the activity lock is held (7.3 step 1; the pilot loaders are unchanged);
- incremental replay for full scale.

## 7. Files

| File | Change |
|---|---|
| `python/nordiccart/activity/state.py` | New: database state as of a business time, snapshot, hash, differences |
| `python/nordiccart/activity/engine.py` | New: context, deterministic replay, order lifecycle, store and warehouse replenishment, plan fingerprint |
| `python/nordiccart/activity/run.py` | New: run protocol, prefix rule, verification, checkpoint, sandbox fault injection |
| `python/run_activity.py` | New: CLI |
| `tests/python/test_activity_engine.py` | New: 14 sandbox tests |
| `docs/test_reports/S6_ACTIVITY_ENGINE_CHECKPOINT.md`, this report, status document S6 entry | Documentation |

No existing module, migration, verification script or configuration file was changed. S5 closeout: the S5 checkpoint now opens with SUCCESS and records that real-database script 14 was not rerun after E1.
