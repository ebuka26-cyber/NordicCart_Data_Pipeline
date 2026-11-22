# S6 milestone 2: incremental replay and execution safety · implementation report

November 19, 2026. **Ready for review: sandbox-verified.**
- S6 suite: 20 of 20 tests passed (14 from milestone 1 and 6 new).
- Full Python regression, including every database suite: 520 passed.

Scope:
- Every write ran in the sandbox (PostgreSQL 18.4, port 55432).
- No migration was added.
- The real database was neither written nor used for activity: it was read read-only, as the loader, for the base comparison and the cluster identifier.
- No full historical generation and no S7 work.

## 1. Changes

| Requirement | Implementation |
|---|---|
| 1. Incremental replay | Each window now replays from the last verified checkpoint:<ul><li>its state, which the database must still hold: hash comparison, as in M1;</li><li>its **carry**: the actions already scheduled at or after the checkpoint (remaining steps of open orders, transfer arrivals, supplier receipts) and the planned life of the open orders.</li></ul>Exchange rates, reviews and demand draws are regenerated for [start, end) from their seeded streams.<br>The carry is written in the checkpoint transaction under `state_snapshot.engine_carry`, as lossless typed JSON (dataclasses, Decimal, datetime). It is **not** part of the state hash.<br>Before use it is checked:<ul><li>against the checkpoint state: open orders, transfers in transit, and open purchase-order quantities;</li><li>against the database: each carried open order's customer, channel, location, time, total, lines and started payment attempts; each carried transfer's dispatch; each purchase order's lines.</li></ul>`--replay auto` (the default) replays incrementally when the previous checkpoint has a carry for this engine and configuration; otherwise it uses a full replay (the first window, or M1 checkpoints). `--replay full` forces a full replay; `--replay incremental` requires the carry.<br>Demand draws are cached per month for the life of the process. |
| 2. Preserved behaviour | Same plan fingerprints, state hashes, run protocol, prefix rule, crash resume and idempotency. **M2 reproduces M1's per-window plan fingerprints and checkpoint state hashes exactly**: all 29 December windows, at both volumes, by full replay and by an incremental chain whose carry went through a JSON round trip.<br>**Clarification (added in M3):** the comparison covered the plan fingerprint and the checkpoint state hash of every window, **not the configuration fingerprint**, and not the whole run row. The configuration fingerprint is SHA-256(engine version, profile, configuration file). M2 changed `ENGINE_VERSION` to `s6-m2`, so every M2 configuration fingerprint differs from M1's on purpose. Consequences:<ul><li>an M1 run left `running` or `failed` would not be resumed by M2 (there is none);</li><li>an M1 checkpoint has no carry, so M2 continues after it by full replay (shown in section 4).</li></ul>"Identical run ledger" in section 2 compares M2 with M2: full replay against incremental, same configuration. |
| 3. Pilot loaders and the activity lock | `apply_transactions` and `apply_replenishment` take the `nordiccart.activity` session lock after their own lock, and hold it until they finish.<br>While the engine holds it they raise `LoaderBusyError`, write nothing, and release their own lock.<br>While they hold it, the engine exits 4.<br>The lock name moved to `units/base.py` (`ACTIVITY_LOCK`). |
| 4. Sandbox guard by database identity | `require_sandbox(conn)` is called by the CLI before `--apply`, and again by `run_window` itself (defence in depth). It requires:<ul><li>`NORDICCART_S6_SANDBOX=1`;</li><li>the server's cluster `pg_control_system().system_identifier` equal to `NORDICCART_S6_SANDBOX_CLUSTER`;</li><li>the identifier not in `PROTECTED_CLUSTERS`: the real server, recorded read-only as the loader;</li><li>the server port and database not the target in `config/.env` (read from the file, never loaded).</li></ul>The log names the connected cluster. |

Files changed:
- New or rewritten:
  - `python/nordiccart/activity/engine.py`: start from any checkpoint, carry encoding, month cache;
  - `python/nordiccart/activity/run.py`: incremental plan, carry check, guard;
  - `python/run_activity.py`: `--replay`, guard, timings.
- Small changes:
  - `python/nordiccart/activity/state.py`: `as_of` normalized to UTC;
  - `python/nordiccart/units/base.py`: `ACTIVITY_LOCK`;
  - `python/nordiccart/transactions/load.py`, `python/nordiccart/replenishment/load.py`: the lock;
  - `tests/python/test_activity_engine.py`: 6 new tests and the cluster fixture.

## 2. Test results

Command: `NORDICCART_S6_SANDBOX=1 python -B -m pytest tests/python/test_activity_engine.py -v` gives **20 passed** (about 2 minutes). M1's 14 tests run unchanged. The crash, failure, drift, composition and idempotency tests now resume through incremental replay.

| New test | Result |
|---|---|
| `test_carry_round_trip_is_lossless` | Encode, decode, encode is identical; JSON round trip identical; the carry holds open orders at midnight |
| `test_incremental_equals_full_across_december_and_january` | **60 windows (2024-12-03 to 2025-01-31)**: window 1 uses a full replay and windows 2–60 incremental replay. Every plan fingerprint and end hash equals one full in-memory replay.<br>A second clone forced to `--replay full` ends with an **identical run ledger, checkpoints included** (carry included), and identical business records.<br>Year boundary:<ul><li>orders numbered `-2025-` on one gap-free global sequence;</li><li>TR and PO numbers restart at 000001 in 2025;</li><li>1 January carries the 31 December rates.</li></ul>Pilot `--plan` checks: 0 conflicts. Scripts 01–18: 0 FAIL | PASS |
| `test_changed_carried_order_blocks_the_window` | A carried open order's payment reference is changed (a change the state hash cannot see). The next window exits 2 ("the open order … differs from the checkpoint's carry"); nothing is written | PASS |
| `test_pilot_loaders_refuse_while_the_engine_runs` | With the engine's lock held, both pilot apply functions raise `LoaderBusyError` ("The activity engine is running …"), write nothing and keep no advisory lock. Without it, they run (nothing to write) | PASS |
| `test_engine_refused_while_a_pilot_load_writes` | During `apply_transactions`, the engine CLI exits 4 and writes nothing; afterwards it runs | PASS |
| `test_sandbox_guard_checks_the_database_identity` | `--apply` exits 3 ("REFUSED (sandbox guard)") with no cluster identifier, with a wrong one, or without the flag.<br>`run_window` refuses a protected cluster and the `config/.env` target.<br>`config/.env` resolves to 5432/`nordiccart_oltp`. Nothing is written | PASS |

**Regression.** All of `tests/python`, with every database suite enabled against the sandbox (`NORDICCART_DB_TESTS`, `S5`, `S6`): **520 passed, 0 skipped, 9 min 10 s**. This includes the pilot loader suites after the lock change.

Before the change, the suite without database flags gave 258 passed and 256 skipped.

## 3. Performance (stress volume, pilot profile)

| Measure | Full replay (M1 behaviour) | Incremental (M2) |
|---|---:|---:|
| CLI wall time, 60 windows (Dec 3–Jan 31), including execution and verification | 16.5 s | **14.8 s** |
| Planning time per window: window 30 / window 60 / mean | 0.077 / 0.137 / 0.088 s | **0.031 / 0.059 / 0.048 s** |
| Replay only, one more day after 30 / 60 / 120 / 210 days (in memory) | 0.050 / 0.088 / 0.199 / 0.282 s | **0.003 / 0.003 / 0.003 / 0.004 s** (17× to 76× faster) |

Planning breakdown at window 61:
- incremental: 0.176 s in total, of which state reads 0.074, replay 0.070 (mostly the one-time cache fill in a new process), carry check 0.011, prefix check 0.006;
- full: 0.191 s in total, of which replay 0.098 and state reads 0.072.

What this shows:
- The replay no longer grows with history.
- At pilot scale, wall time is dominated by the units of work and by the three database state reads per window. Those reads are now the largest cost that still grows (see 5.1).

## 4. Fallback evidence

On the M1 demo database (29 verified windows, checkpoints without a carry):
- `--plan` falls back to a full replay and plans 2025-01-01 (12 units);
- `--replay incremental` refuses: "needs the previous checkpoint's carry".

## 5. Unresolved issues and choices for review

1. **State reads grow with table size.** `state_as_of` aggregates all movements and reservations. It runs three times per window: the origin drift check, the window start and the verification. That is negligible at pilot scale, but the next bottleneck for S8. Possible remedies: indexes on the business-time columns, checking the origin less often, or deriving the end state from the start state plus the window's rows.
2. **The carry lives inside `state_snapshot`** (key `engine_carry`, not hashed, validated against the database before use). This avoids a migration. The alternative, rebuilding pending documents purely from the database, would need plan-only fields (review positions, order-up-to levels) that the database does not hold.
3. **`PROTECTED_CLUSTERS` holds the real server's system identifier.** It changes if that cluster is ever re-created with initdb; the `config/.env` target check and the sandbox allowlist still apply in that case. One guard test reads the current `config/.env` and expects 5432/`nordiccart_oltp`.
4. **Sandbox side effect (known since S3).** The V008 rollback test's `DROP OWNED BY` removed CONNECT on the other sandbox databases during the regression run. It was restored with `V008_reapply_database_privileges.sql`, and the S6 base was rechecked afterwards: 23 of 23 still identical to the real database.
5. **Still open in S6:**
   - monthly verification and recovery points (7.4);
   - the R-cal monthly review and P1;
   - the E1 CLI (`run_exchange_rates.py`) does not take the activity lock. E1 is finished, and the X1 transaction lock already serializes rate writes.
