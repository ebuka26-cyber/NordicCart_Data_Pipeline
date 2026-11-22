# S5 exchange rates · project checkpoint

**SUCCESS: S5 deployed to the real database on November 19, 2026.**
- Implementation and sandbox validation are complete.
- The approved E1 backfill committed 96 exchange rates at 22:35:00 as `nordiccart_loader`.
- Every requested post-deployment check passed: change_log 1,714; pilot 20 / 326 / 1,467 unchanged; plan rerun 0 pending; script 16 4 PASS; scripts 01–13 and 15–18 0 FAIL.
- **Script 14 was not rerun on the real database after E1.** The loader may not read `change_log` row contents. It passed after E1 in the sandbox.

- **Completed:** Reviewed the approved design and S4 closeout. Implemented the synthetic daily series, X1 loader, E1 CLI, script 16, script 11 compatibility and focused tests.
- **Results:** 9 tests passed. The sandbox committed exactly 96 E1 rates and 96 audit rows; repeat apply and plan both found zero pending. All 18 verification scripts had zero FAIL. A single-day resume added the expected three rows. The full-calendar in-memory check produced 2,190 rates.
- **Errors:** The earlier master-data check expected an empty exchange-rate table. Its scope was updated for S5 and the complete SQL suite passed. No unresolved sandbox failures.
- **Last operation:** Final sandbox verification passed, disposable databases were removed, and the sandbox was stopped. The application and database migrations remain at the S4 schema.
- **Remaining:** ChatGPT review of S5, then separately authorized live preflight, backup verification, E1 plan, apply and post-load checks. No S6 work started.

- **Pre-apply recheck (22:11, read-only, as `nordiccart_loader`):**
  - Pre-S5 backup unchanged: 373,690 bytes, SHA-256 `DF2CE951…DD0E7`.
  - Real database matches the readiness state: migrations 001–009; 20 orders, 326 movements, 1,467 units; change_log 1,618 rows (max id 1836); 0 exchange rates; 4 purchase orders and 69 revisions; 0 activity runs; no other sessions or prepared transactions.
  - E1 `--plan`: exit 0, **96 pending**.
  - Next: `run_exchange_rates.py --apply` once the user explicitly approves it.
- **E1 deployed on the real database: SUCCESS (22:35:00, as `nordiccart_loader`).**
  - The first `--apply` was refused at its confirmation prompt, because PowerShell had added a byte-order mark to the piped answer; nothing was written. The user approved one rerun, which committed 96 rows.
  - After: 96 rates; change_log 1,714; 20 orders, 326 movements, 1,467 units unchanged; plan rerun 0 pending.
  - Script 16: 4 PASS, 0 FAIL. Scripts 01–13 and 15–18: 0 FAIL. Script 14 needs postgres or the analyst.
  - Details: `S5_real_deployment_readiness_report.md`, section "E1 real deployment".
- **Closed:** S5 deployed. Next stage: S6 (see `S6_ACTIVITY_ENGINE_CHECKPOINT.md`).

Evidence and source inventory: `docs/test_reports/S5_exchange_rates_implementation_report.md`.
