# S6 activity engine: checkpoint and handoff

Last updated: 2026-11-20. **Milestone 3 complete and sandbox-verified; stopped for ChatGPT review.**

## Completed tasks
- **Milestone 1** (reviewed): engine, run protocol, CLI, 14 sandbox tests. Report: `S6_activity_engine_implementation_report.md`.
- **Milestone 2** (reviewed): incremental replay with carry, pilot loaders honour the activity lock, identity-based sandbox guard. Report: `S6_M2_incremental_replay_report.md`. It now has a clarification of the configuration-fingerprint comparison.
- **Milestone 3:**
  1. R-cal monthly review: first working day, 05:00 local, `rcal_reorder_points`, capacity guard; new unit `units.stock.review_reorder_points` (kind `monthly_review`).
  2. P1 lifecycle markdowns: 6 and 12 months after launch, 3–8%, shelf price points, margin kept; new unit `units.prices.change_price`.
  3. Both rules are part of the deterministic replay. State version 2 adds prices.
  4. Month-end verification (`activity/monthly.py`, 19 checks). It runs after a month's last window and as a gate before the next month.
  5. Four months (Dec–Mar, 119 windows): incremental = full; 4 × 19/19 month checks; scripts 01–18 0 FAIL; pilot intact.
  6. Crash inside R-cal and P1 units resumes identically; month gate blocks a broken month.
  7. Carry v2: SHA-256 plus re-derivation of every future action. Three tampering cases are blocked.
  8. Fingerprint clarification (M2 report and M3 report section 3).
  - Script revisions: 11 (105 and INFO 507) and 12 (501). Five suites' script 11 expectations 43 → 44.
  - Tests:
    - S6 suite: 25 of 25 passed.
    - Full regression, all database suites in the sandbox: 525 passed.
  - Report: `docs/test_reports/S6_M3_implementation_report.md`.

## Remaining tasks
- ChatGPT review of milestone 3.
- Open for later:
  - monthly recovery points (7.4);
  - state reads at full scale;
  - S recalibration;
  - price elasticity of markdowns.

## Errors or blockers
- None open.
- Fixed during M3 testing:
  - price-row count for pre-loaded launch prices;
  - the P1 month check ignoring launch rows;
  - script 12 check 501;
  - a test filter for R-cal revisions;
  - a missed script 11 INFO expectation in `test_master_data_seed_db`;
  - the S6 fixture reading the base when the V008 rollback test has removed CONNECT. Privileges on the base and `nordiccart_s6_m3try` were re-applied afterwards.

## Files and outputs
- Code:
  - `python/nordiccart/activity/{engine,run,state,monthly}.py`
  - `python/nordiccart/units/{stock,prices}.py`
  - `python/run_activity.py`
- Configuration: `config/generator.toml` `[activity]`.
- Scripts: `sql/verification/11_*.sql`, `12_*.sql`.
- Tests: `tests/python/test_activity_engine.py`, plus five existing database suites (script 11 counts).
- Scratchpad (session-specific; may be cleared):
  - `s6m3_probe.py`
  - `s6m3_try.log` (four-month manual run)
  - `s6m3_full_regression.txt`
  - `sandbox_s6_stress.toml` (regenerated from the updated config)
- Test commands (sandbox, all PG variables set to the sandbox):
  - `NORDICCART_S6_SANDBOX=1 python -B -m pytest tests/python/test_activity_engine.py -v`
  - Full regression: add `NORDICCART_DB_TESTS=1` and `NORDICCART_S5_SANDBOX=1`.

## Current environment
- Real database: unchanged. Migrations 001–009, 96 exchange rates, change_log 1,714, no activity runs.
- Sandbox: PostgreSQL 18.4, port 55432, cluster 7709316403725191040. Databases:
  - `nordiccart_s6_base` (= real state);
  - `nordiccart_s6_m3try` (four-month run);
  - earlier demo and test databases (M1/M2 state version; not continued by M3).

## Next action
Wait for ChatGPT's review of milestone 3. No engine run on the real database, no full historical generation, no S7.
