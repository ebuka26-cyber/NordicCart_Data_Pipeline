# NordicCart Phase 2: database implementation status

Updated 2026-11-20.

| Area | Status |
|---|---|
| Phase 3 steps 1–4 | Applied to the real database and verified; the last was the replenishment pilot |
| Combined activity simulation | Designed; revision 2 approved: docs/phase3_combined_activity_design.md |
| Step 5, stage S1 | Implemented and reviewed: docs/test_reports/2026-11-18_phase3_step5_s1_pilot_scope_test_report.md |
| Stage S2 | Demand model implemented and calibrated, no database access: docs/test_reports/2026-11-18_phase3_step5_s2_demand_calibration_report.md |
| Stage S3 | V008 security **deployed to the real database 2026-11-18 and verified**. The application connects as `nordiccart_loader`: docs/test_reports/2026-11-18_s3_v008_real_deployment_report.md |
| Stage S4 | **SUCCESS.** V009 and the historical pilot-document backfill deployed and verified on the real database on 2026-11-19. Final verification: 709 PASS, 171 INFO, 3 expected WARN, 0 FAIL. |
| Stage S5 | Exchange-rate generator, X1 loader, E1 CLI and script 16 implemented. Sandbox E1 committed 96 rates; rerun pending 0. Nine focused tests passed. **E1 deployed on the real database 2026-11-19 22:35: SUCCESS.** 96 rates; change_log 1,714; plan rerun 0 pending; script 16 4 PASS; 0 FAIL. Awaiting ChatGPT review: `docs/test_reports/S5_real_deployment_readiness_report.md`. |
| Stage S6 (milestone 1) | Activity engine at pilot scale, sandbox only: database-derived state, deterministic window replay, run protocol on the V009 run ledger and checkpoints, existing units of work. December 2024 (29 day windows) verified at representative and stress volume; crash at 4 points, failure, drift, composition and lock tests passed (14/14). No migration; real database not written. Milestone 1 review passed. **Milestone 2** (2026-11-19): incremental replay from the last checkpoint (identical to full replay over 60 windows, December–January), pilot loaders honour the activity lock, sandbox guard by cluster identity. S6 suite 20 of 20, full regression 520 passed. Milestone 2 review passed. **Milestone 3** (2026-11-20): R-cal monthly review and P1 lifecycle markdowns in the deterministic replay, month-end verification (19 checks, also a gate before the next month), carry SHA-256 and re-derivation of future actions; four months December–March verified, incremental = full. Milestone 3 review passed: **S6 COMPLETE at pilot scale** (monthly recovery points deferred to S8/S9): `docs/test_reports/S6_M3_implementation_report.md`. |
| Stage S8 | **Sandbox rehearsal COMPLETE (2026-11-20): 2024-12-03 to 2026-10-31.** 698 of 698 windows verified; 23 months 19/19; 71,409 orders (−1.2% against 72,635 expected, lost demand 0.46%); scripts 01–18 0 FAIL; pilot records identical; 23 recovery points (8 retained; 14 monthly audits through January 2026 plus a verified October restore, 15 distinct months); engine 47 minutes; database 1.2 GB. One engine defect fixed (out-of-calendar month at the last window). Operational verdict PASS. Inventory-turnover calibration FAIL (about 2.2 unit turns and 1.56 cost-based turns in the primary year against a target of 4–6). Overstock and frequent transfers and a script 15 runtime of 101 minutes need decisions before S9. Real database not modified: `docs/test_reports/S8_historical_simulation_report.md`. |
| Power BI | **COMPLETE (2026-11-20).** Power BI Project `powerbi/NordicCart.pbip` (TMDL model: 7 tables, 7 relationships, 35 DAX measures; PBIR report: Sales, Orders and Inventory pages, 46 visuals) on the ETL analytics CSVs, opened and refreshed in Power BI Desktop 2.159. Reconciliation 77/77 PASS against FastAPI (itself cent-exact with the real database) and the CSVs, including like-for-like YoY, both before and after the 00:05 hourly run and a refresh (publication 23:06:10 UTC loaded). The GitHub version has no machine-specific path (`AnalyticsFolder` placeholder; `build_pbip.py --local` for a working copy). Minimal ETL retry for Windows file-replace conflicts (`etl._atomic_write`, 20 × 0.5 s; 2 tests); nothing else in the pipeline changed. Next: GitHub publication: `docs/test_reports/PowerBI_dashboard_report.md`. |
| FastAPI | **COMPLETE (2026-11-20).** Read-only analytics API (`python/nordiccart/api.py`, start `python/run_api.py`, Swagger `/docs`) over the hourly ETL's `output/etl/analytics` files; no database access, pipeline unchanged. Endpoints `/health`, `/sales/daily`, `/orders/monthly`, `/inventory/current`, `/kpis` with country, channel and date filters; 400/422/503 error handling. Each request checks the files and re-reads any the ETL has republished. Tests 14/14; live evidence 27/27 PASS, including a cent-exact match with the real database for every country's fulfilled orders and EUR revenue up to 2026-10-31. Next: Power BI, GitHub: `docs/test_reports/FastAPI_implementation_report.md`. |
| Stage S10 | **ACTIVATED on the real database and CLOSED (2026-11-20).** Pre-S10 backup verified; scheduler and extractor credentials set. Supervised run: 20 windows (runs 699–718, 2026-11-01 to 11-20 19:00 UTC, 2,502 orders) after a full-history replay that confirmed the history unchanged (state `a57f6ed8…`); ETL 1,510,626 changes; 8/8 DQ PASS. The hourly task "NordicCart Hourly Pipeline" is enabled; first automatic run 21:05 exit 0 (run 719, 35 changes, 8/8 DQ PASS). Next: FastAPI, Power BI, GitHub: `docs/test_reports/S10_activation_status.md`. Earlier: **COMPLETE in the sandbox; READY for real activation (2026-11-20).** Post-October fix: `[demand] generation_end = 2027-10-31` extends only the demand calendar and the exchange rates. Master data and history are unchanged: the first post-October window replayed the full history and passed the drift check (state `a57f6ed8…` = S9). Sandbox runs on 2026-11-01 and 11-02 generated orders, the November monthly review, purchase orders and transfers; ETL 8/8 DQ PASS; a rerun wrote nothing (files byte-identical); scripts 15, 16 and 18 0 FAIL. Credential setup prepared, not run; task disabled. Activation steps: report section 6. Earlier S10 work: **Hourly pipeline and ETL implemented (2026-11-20); scheduled task registered DISABLED.** Real-database `ANALYZE` done (24 tables). The engine gained an `--tick` mode (hourly windows, catches up missed hours, writes nothing for an hour already verified); a new `run_pipeline.py` adds lock, retries and logging; the ETL runs raw (`change_log` snapshot checkpoint) → cleaned (idempotent) → 8 DQ checks → analytics. Sandbox evidence: hourly runs verified; rerunning the same window wrote nothing (files byte-identical); hourly windows give the same state hash as S8.2's day window. Blockers: no generation after `simulation_end` 2026-10-31 (a design gap), and scheduler and extractor credentials: `docs/test_reports/S10_hourly_pipeline_report.md`. |
| Stage S9 | **SUCCESS: real historical catch-up COMPLETE (2026-11-20, explicit authorization). Historical database implementation phase CLOSED.** 2024-12-03 to 2026-10-31 on the real `nordiccart_oltp`: 698 of 698 windows verified; 23 months 19/19; ledger **identical to S8.2** for every window. Scripts 01–18 0 FAIL; pilot plans 0 conflicts; original records byte-identical to the pre-S9 backup. 23 recovery points (SHA-256 verified), 8 retained. Final: 71,418 orders, €25.13M fulfilled revenue, 200,336 movements, change_log 1,460,385, 1,238 MB. One performance anomaly with no data effect: February 2025's month-end check took 98 min. Next: hourly automation, ETL, FastAPI, Power BI: `docs/test_reports/S9_execution_report.md`. Readiness: `docs/test_reports/S9_deployment_readiness.md`. Real database at the verified S7 baseline; the first-window plan on the real database is identical to S8.2. Fresh pre-S9 backup verified (`nordiccart_oltp_20261120_143552_pre_s9.dump`, 3,396,430 bytes). Blockers resolved without DB changes: an explicit real-target authorization in the engine guard (7/7 tests), and a local-window driver for the postgres recovery points (`python/s9_catch_up.py`): `docs/test_reports/S9_deployment_readiness.md`. |
| Stage S8.2 | **Final full-scale rehearsal COMPLETE in the sandbox (2026-11-20): PASS; final review passed. Simulation development CLOSED.** Corrected engine `s8-1` on a fresh S7 clone, 2024-12-03 to 2026-10-31: 698 of 698 windows verified, 23 months 19/19, no errors. Scripts 01–18 as analyst 0 FAIL (48.5 s); pilot plans 0 conflicts; original records identical. 23 recovery points; final restore verified (SHA-256, 24 tables, state hash); 8 retained. Primary year against S8: turns 2.21 → 2.45 (cost 1.56 → 1.64), warehouse stock −21% (cover in target), unit fill 99.20%. Orders 71,398, revenue €25.13M. No material regressions. Known accepted limitation: turnover is below 4–6 because of the full-range store display minimums. Real database not modified: `docs/test_reports/S8_2_final_full_scale_rehearsal_report.md`. |
| Stage S8.1 | **COMPLETE in the sandbox (2026-11-20); review passed** (decision: keep the assortment, display minimums and corrected policy). Inventory: S recomputed at R-cal. On the 13-month run, warehouse stock fell 19.6% (in target), turns rose 2.06 → 2.25 and availability was unchanged. Store display minimums cap turnover at about 3.8, so a target decision is needed. Script 15: 101 min → 6.2 s as analyst, with the same results. Real database not modified: `docs/test_reports/S8_1_calibration_and_performance_report.md`. |
| Stage S7 | **COMPLETE** (deployment reviewed 2026-11-20). **SUCCESS: deployed to the real database on 2026-11-20** (explicit authorization). Master data 22,300 rows and opening inventory 7,680 listings / 5,808 opening balances / 18,406 units, 0 conflicts. Final: 500 products, 2,000 prices, 20,000 customers, 8,000 listings, 6,096 opening balances, 19,866 units; 96 rates and 20 orders unchanged; change_log 43,310. Pilot plans 0 conflicts, E1 0 pending, scripts 01–18 0 FAIL (888 rows). Before deployment: full-profile expansion sandbox-verified (2026-11-20). 500 products, 2,000 prices, 20,000 customers; 12 stores and 4 warehouses kept; demand-calibrated opening inventory, 8,000 listings / 6,096 opening balances / 19,866 units (E2 through the existing loader). Pilot records and the 96 rates identical; scripts 01–18 0 FAIL (11 and 12 full-profile revisions); engine at full scale 31 windows verified, December month-end 19/19. Sandbox review passed. Full regression 527 passed, exit 0. **Deployment readiness: READY** (read-only real-database plans: 22,300 master rows, 7,680 listings, 5,808 opening balances, 0 conflicts; pre-S7 backup `nordiccart_oltp_20261120_011452_pre_s7.dump` verified and restored). Awaiting ChatGPT review and explicit deployment approval: `docs/test_reports/S7_implementation_report.md`. |

**Stage S8.2 (2026-11-20): final full-scale sandbox rehearsal COMPLETE, PASS; awaiting ChatGPT review.**
- **Run:** the corrected engine ran the whole history on a fresh S7 clone: 698 of 698 windows and 23 of 23 month-end checks passed.
- **Checks:**
  - scripts 01–18 as `nordiccart_analyst` 0 FAIL;
  - pilot records identical; pilot plans 0 conflicts.
- **Recovery:** final recovery point restored and verified; S8 retention applied.
- **Primary year against S8:**
  - turnover 2.21 → 2.45 units;
  - warehouse cover in target;
  - availability unchanged (unit fill 99.2%).
- **Errors:** none new, and no material regressions.
- **Known limitation:** the turnover gap to 4–6 comes from the accepted full-range display minimums and is documented.

S9 needs review, a fresh verified backup and explicit authorization.

**Stage S8.1 (2026-11-20): COMPLETE in the sandbox; review passed.** S8 passed the operational review.

1. **Inventory**
   - **Change:** S (order-up-to) is recomputed at every monthly R-cal from the demand forecast over the weeks of cover (engine `s8-1`). There are no configuration, schedule or pilot changes.
   - **Run:** a 13-month sandbox run (`nordiccart_s81`, 2024-12-03 to 2025-12-31) verified 394 of 394 windows, with every month 19/19. Scripts 01–18 0 FAIL; pilot plans 0 conflicts; original records identical.
   - **2025 against S8:**
     - stock −8.8%;
     - warehouse stock −19.6% (cover 9–11 weeks, in target);
     - turns 2.06 → 2.25 units and 1.49 → 1.56 at cost;
     - availability unchanged (unit fill 99.19%);
     - transfers −1.1%.
   - **Limit:** the stores' full-range display minimums cap turnover at about 3.8, so the 4–6 target needs an assortment or display decision.
2. **Script 15:** three CTEs inlined (`AS NOT MATERIALIZED`). Full history: 101 min → 6.2 s as `nordiccart_analyst`; same 43 checks and results.
3. **Tests:** three new tests, including the out-of-calendar month. Engine module 27/27, script 15 DB tests 43/43, non-database suite 260 passed.

Both corrections are ready for a final full-scale rehearsal after the turnover-target decision. The real database was not modified; S9 has not started. Report: `docs/test_reports/S8_1_calibration_and_performance_report.md`.

S8 October restore review: the uniquely named sandbox copy passed full-row comparison across all 24 tables after accounting for the final archive registration, and all original pilot rows matched S7. Final SQL checks on the restore returned **712 PASS, 173 INFO, 3 expected WARN, 0 FAIL**. Script 15 matched the original check results with in-memory planner directives. The restore was retained and the idle sandbox stopped. Inventory-turnover calibration remains open for ChatGPT review before S9.

Repository folder: NordicCart_Data_Pipeline (local).

## Approved design
database/design/nordiccart_oltp_rev3.dbml (revision 3). Earlier revision in database/design/archive/. Business rules in docs/Business_Rules.md.txt (section 4 still describes the older seven-table design).

## Step 1: foundational tables (applied to the real database and verified)
- database/setup/00_create_database.sql: nordiccart_oltp, UTF8, timezone UTC
- V001 meta.schema_migrations, guard function, btree_gist, PG 13+ and UTF8 checks
- V002 change_log, change_log_checkpoints, fn_set_updated_at, fn_reject_modification, fn_capture_change
- V003 currencies, countries, exchange_rates (insert-only)
- V004 product_categories, products, product_prices (EXCLUDE no overlap), customers
- V005 stock_locations, stores, warehouses (composite type-checked FKs, deferred subtype check)
- Checks: sql/verification 01-04, tests/sql/test_foundation_constraints.sql (44 tests)

## Step 2: orders and payments (applied to the real database and verified)
- V006 orders, order_items, payment_attempts (revision 2 after the pre-deployment review, same tables and columns)
  - all DBML columns, keys, the composite in-store fulfilment FK, 23 named CHECKs, indexes
  - partial unique index: one live payment attempt per order
  - guards: order status transitions; lines immutable after placed; line VAT = country rate; payment amount = order_total; cash only in store; payment status transitions; capture only while stock_reserved (order row locked)
  - commit-time checks: at least one line; items_total = sum of lines; shipping VAT by formula; confirmed or fulfilled <=> exactly one captured attempt; payment_failed => a failed attempt and none open; cancelled => no authorized attempt (pending allowed)
  - revision 2 historical integrity: no delete or truncate of orders and payment history; order identity, customer, placed_at and context never change; event timestamps by status, written once, in time order; final orders and final payment attempts immutable; payment terms immutable; outcome fields only by their own transition; provider_reference written once
  - revision 2 fulfilment: location in the order country; online orders from a warehouse that ships online orders
  - revision 2 cancellation_reason required (three documented values) exactly when cancelled
- Checks: sql/verification 05-07, tests/sql/test_v006_orders_and_payments.sql (119 tests, each rejection checked by rule name)
- Sandbox result 2026-11-16 (PostgreSQL 16): verification 360 PASS, 0 FAIL; behaviour tests 163 PASS, 0 FAIL; 4 of 4 deliberate rule removals detected
- Execution: docs/phase2_valentina_studio_execution_guide.md, Part B
- tests/sql/test_v006_orders_and_payments.sql is now revision 3 (inventory-aware, same 119 tests): needed because V007 allows stock_reserved only with reservations

## Step 3: inventory (applied to the real database and verified)
- V007 inventory_balances, inventory_reservations, inventory_movements; enums reservation_status, movement_type
  - all DBML columns, keys, composite balance FKs, transfer self-FK, 13 named CHECKs (no negative stock, no oversell), 11 indexes; return_items FK waits for DBML Phase 4
  - partial uniques: one active reservation per line, one sale per line, one return_restock per return item, one transfer_in per transfer_out; additions: one opening_balance per balance, document reference unique per type, location and product
  - balance quantities maintained only by triggers from movements and reservations (direct writes refused); balances start at zero
  - reservation guard: matches order line and fulfilment location; active -> consumed (order confirmed), released (stock_reserved, payment_failed, cancelled) or expired (when due); terms and closed rows immutable; no delete
  - movement guard: insert-only ledger; sale only for a confirmed order with a consumed reservation of the same line, location and quantity; transfer_in matches its transfer_out; document reference for receipts, adjustments, write-offs and transfer_out; return_restock refused until returns exist
  - capture only while every line holds an active reservation (new trigger on payment_attempts)
  - commit-time order checks (new deferred trigger on orders): placed holds no reservation; stock_reserved and confirmed hold one active reservation per line; fulfilled has every line consumed and sold once; payment_failed and cancelled hold none
  - unit-of-work functions in the design lock order, idempotent: fn_reserve_order_stock (U2), fn_close_order_reservations (U3 release, U4 expire), fn_fulfil_order_stock (U5)
  - no TRUNCATE on the three tables; no V001 to V006 file, table or function changed
- Checks: sql/verification 08-10, tests/sql/test_v007_inventory.sql (105 tests), tests/python/test_v007_concurrency.py (11 multi-session scenarios in its own temporary database)
- Sandbox result 2026-11-16 (PostgreSQL 16), fresh V001-V007: verification 477 PASS, 0 FAIL, 2 expected WARN (V007 triggers on V006 tables, shown by script 07); behaviour tests 268 PASS, 0 FAIL; concurrency 11 of 11 PASS; V006-only database still 360 + 163 PASS
- Mutation checks: 53 of 59 single-rule removals caught by the behaviour tests, 3 more by verification 09 and 10, 3 equivalent (a second layer raises the same rule); 4 of 4 concurrency defects caught by the Python test
- Report: docs/test_reports/2026-11-16_v007_sandbox_test_report.md. Execution: guide Part C

## Phase 3, step 1: master data generator (pilot applied to the real database and verified)
- Python package python/nordiccart: settings (config/generator.toml, no secrets; credentials from config/.env, never logged), reference data, generator, validation, idempotent loader, CSV preview, logging to logs/
- Pilot dataset: 4 currencies, 4 countries (2026 standard VAT: SE/NO/DK 25%, FI 25.5%), 10 electronics categories, 20 products with 80 opening prices (one per market, market currency, positive margin), 12 stores, 4 warehouses (one per country, ships online orders), 100 customers; 250 rows, 250 change_log rows
- Reproducible: seed 20261117, one random stream per entity; same fingerprint on Python 3.10 to 3.14; pilot rows are the first rows of the full profile (500 products, 20,000 customers)
- Rerun-safe: business-key matching; missing rows inserted, equal rows verified, differing rows stop the run before writing; one transaction, advisory lock, verification before COMMIT
- Modes: dry run (no database), --plan (read-only), --apply (typed confirmation)
- Checks: tests/python/test_master_data_generator.py (48), tests/python/test_master_data_seed_db.py (13, opt-in, own databases), sql/verification/11_verify_master_data.sql (revision 1: 38 PASS, 4 INFO; revision 2 from step 2: 37 PASS, 5 INFO; revision 3 from step 3: 33 PASS, 9 INFO, valid before and after the opening inventory and the pilot transactions; revision 4 from step 4: 33 PASS, 10 INFO, also valid after the replenishment pilot)
- Sandbox result 2026-11-17: all of the above pass; Phase 2 checks unchanged with the data present (477 PASS, 0 FAIL; SQL tests 268/268); full profile applied to a copy in 9.7 s; 10 of 10 deliberate defects caught
- Real database 2026-11-17 (PostgreSQL 18.4, Python 3.14.7): pilot applied, 250 rows, fingerprint c46181a2..., script 11 (revision 1) 38 PASS, 4 INFO, 0 FAIL
- Guide: docs/phase3_master_data_generator_guide.md. Report: docs/test_reports/2026-11-17_master_data_pilot_sandbox_test_report.md

## Phase 3, step 2: opening inventory generator (pilot applied to the real database and verified)
- Python package python/nordiccart/inventory: settings ([inventory] in config/generator.toml), generator, validation (18 rules), loader through the V007 ledger, CSV preview, logging; runner python/run_opening_inventory.py; 4 VS Code Run configurations in .vscode/launch.json (now tracked by git)
- Pilot: every product listed at all 12 stores and 4 warehouses (320 inventory_balances rows); 288 opening_balance movements at 00:00 local time on 2024-11-01, reference OB-20241101; 1,460 units; 32 listings at 0 for the two products launched later (CAM-000007, SMH-000011); 896 change_log rows
- Quantities from expected demand: category demand and price, seeded popularity, customer mix per market, 55% online (warehouse) and 45% stores (flagship weight 2), 6 weeks of cover in stores and 10 in warehouses with seeded variation, store display minimums, warehouse minimum 4 and at least any store in its market; reorder point = lead-time demand x 1.5, below the opening stock
- Reproducible: fingerprint 5c7a4eba...; pilot listings are the first of the full profile (8,000 listings, 6,096 opening balances, 28,109 units)
- Writes only through V007: balances inserted at 0, then opening_balance movements in the V007 lock order; quantities never written directly
- Rerun-safe: master data must be present and unchanged; listings matched on location and product; equal opening movements verified, missing ones inserted; a different opening quantity, opening stock after other activity, or opening stock for a product not on sale stops the run before writing; the V007 unique index refuses a second opening balance; one transaction, advisory lock, verification before COMMIT
- Checks: tests/python/test_opening_inventory.py (38), tests/python/test_opening_inventory_db.py (18, opt-in, own databases), sql/verification/12_verify_opening_inventory.sql (revision 1: 19 PASS, 4 INFO; revision 2 from step 3: 17 PASS, 6 INFO, valid before and after the pilot transactions; revision 3 from step 4: 17 PASS, 7 INFO, also valid after the replenishment pilot)
- Changed: sql/verification/11_verify_master_data.sql revision 2 (checks 204 and 206 allow opening stock), tests/python/test_master_data_seed_db.py (one assertion), .gitignore (.vscode/launch.json tracked)
- Sandbox result 2026-11-17 (PostgreSQL 16.15, Python 3.14.6 and 3.13.18): all Python tests pass on both (48 + 38 + 13 + 18 + 11 scenarios); scripts 11 and 12 pass; Phase 2 checks unchanged with the stock present (477 PASS, 0 FAIL, 2 expected WARN; SQL tests 268/268); full profile applied to a copy in 5.5 s; 15 of 15 deliberate defects caught
- Real database 2026-11-17 (PostgreSQL 18.4): 320 listings, 288 opening balances, 1,460 units, 896 change_log rows; verification passed
- Guide: docs/phase3_opening_inventory_guide.md. Report: docs/test_reports/2026-11-17_opening_inventory_sandbox_test_report.md

## Phase 3, step 3: pilot transactions (pilot applied to the real database and verified)
- Python package python/nordiccart/transactions: settings ([transactions] in config/generator.toml, pilot profile only), generator, validator (67 rules, independent stock replay), loader through the V006/V007 units of work, CSV preview, logging; runner python/run_transactions.py; 3 VS Code Run configurations (Transactions: Dry Run, Check, Load)
- Pilot: 20 orders placed 2024-11-01 to 2024-11-14 by existing customers in all four markets, 10 online and 10 in store; 26 lines; 21 payment attempts (14 captured, 7 failed; card, mobile payment, buy now pay later, cash); 23 reservations; 17 sale movements, 19 units sold; stock 1,460 -> 1,441 units
- Scenarios: 12 fulfilled, 2 fulfilled after a failed payment, 2 payment_failed (3 attempts online, 2 in store), 1 cancelled by the customer, 1 reservation expired (U4), 1 out of stock by quantity (all lines or none), 1 out of stock because another customer's reservation holds the last unit
- Consistency: customer of the market registered before ordering; products on sale and locations open on the local day; online from the market warehouse to the customer's address, in store from the store's own stock during opening hours; price and cost valid at placed_at, market currency and VAT, V006 line, order and shipping formulas; reservations 15 min online and 5 min in store, payments before expiry, fulfilment the same afternoon or next morning online, within two minutes in store
- Writes only through V006/V007: 100 units of work, each its own transaction in business-time order: U1 insert, U2 fn_reserve_order_stock (out_of_stock rolled back, then cancelled in a new transaction), U3a pending attempt, U3c capture and confirm or fail (last failure: fn_close_order_reservations released, payment_failed), customer cancel (released), U4 expire (expired, SKIP LOCKED), U5 fn_fulfil_order_stock; every result compared with the plan
- Rerun-safe and resumable: orders matched on order_number; done steps derived from the database and compared value by value; done steps must be a prefix of the plan and the stock must equal the opening stock replayed through them; only the remaining steps run; session advisory lock; retries on 40001, 40P01, 55P03; verification of every order, payment, reservation, sale and balance at the end
- Checks: tests/python/test_transactions.py (53), tests/python/test_transactions_db.py (21, opt-in, own databases; 23 since step 4), sql/verification/13_verify_pilot_transactions.sql (44 PASS, 6 INFO; revision 2 from step 4 keeps this after the replenishment pilot)
- Changed: sql/verification/11 revision 3 and 12 revision 2 (valid before and after the transactions), two test assertions, .vscode/launch.json (3 entries)
- Sandbox result 2026-11-17 (PostgreSQL 16.15, Python 3.14.6 and 3.13.18): all Python tests pass on both (48 + 38 + 53 + 13 + 18 + 21 + 11 scenarios); scripts 11, 12 and 13 pass; Phase 2 checks unchanged with the orders present (477 PASS, 0 FAIL, 2 expected WARN; SQL tests 268/268); 18 of 19 deliberate defects caught (the 19th is equivalent for the pilot)
- Backup before the load (2026-11-18 01:22): database/backups/nordiccart_oltp_20261118_012230_pre_pilot_transactions.dump (pg_dump 18.4, custom format, 241,404 bytes, SHA-256 86EAEA1E…D7CB); archive fully readable, row counts of all 19 tables equal to the live database; the only way back to the pre-pilot state (V006/V007 refuse DELETE and TRUNCATE on the order and inventory history)
- Real database 2026-11-18 (PostgreSQL 18.4, Python 3.14.7): check, then `run_transactions.py --apply --yes` (written authorization; `--yes` because the shell was non-interactive): 100 units of work in 100 transactions (2 out_of_stock U2 rolled back by design), 20 orders placed and verified, 280 change_log rows, 0 errors, 0 warnings, 0 retries, 0.9 s; plan fingerprint fabcd571…; a second check shows 0 to write; migrations V001 to V007 unchanged (SHA-256 before and after)
- Final state: 14 fulfilled, 2 payment_failed, 4 cancelled (customer_request 1, out_of_stock 2, reservation_expired 1); 21 payment attempts (14 captured, 7 failed); 23 reservations (17 consumed, 4 released, 2 expired, 0 active); 17 sale movements (19 units); stock 1,460 -> 1,441 units, 0 reserved; change_log 1,146 -> 1,426
- Verification on the real database: script 13 44 PASS, 6 INFO; script 11 (revision 3) 33 PASS, 9 INFO; script 12 (revision 2) 17 PASS, 6 INFO; 0 FAIL (115 rows: 94 PASS, 21 INFO); all 20 orders equal the plan
- After the pilot: SPH-000014 is sold out at SE-STO-03; 8 store listings are at or below their reorder point; no warehouse listing is
- Not written yet, by design: returns, refunds, shipments, restocking (receipts, transfers: see step 4), daily exchange_rates, larger volumes
- Guide: docs/phase3_transactions_guide.md. Reports: docs/test_reports/2026-11-17_transactions_pilot_sandbox_test_report.md (sandbox), docs/test_reports/2026-11-18_transactions_pilot_execution_report.md and docs/test_reports/2026-11-18_transactions_pilot_sql_verification_report.md (real database)

## Phase 3, step 4: inventory replenishment and stock transfers (pilot applied to the real database and verified; step completed)
- Real database 2026-11-18 (PostgreSQL 18.4, Python 3.14.7), loaded with written authorization after review of the readiness report:
  - **Backup before the load:** database/backups/nordiccart_oltp_20261118_032247_pre_replenishment.dump (custom format, 257,336 bytes, SHA-256 A4907BB9…8078). Archive readable (383 TOC entries); a full test restore in the disposable sandbox matched all 19 tables; scripts 11 to 13 passed on the restored copy. Report: docs/test_reports/2026-11-18_pre_replenishment_backup_and_readiness_report.md
  - **Check before the load (read-only):** plan fingerprint 858bbfd0…, validation 0 issues, 17 to write, 0 conflicts, 0 blocking
  - **Load:** `run_replenishment.py --apply --yes` (`--yes` because the shell was non-interactive). 17 units of work in 17 transactions (R0 4, T1 4, T2 4, R1 5), 17 verified, 111 change_log rows, 0 errors, 0 warnings, 0 retries, 0.7 s
  - **Final state:** 20 orders unchanged (their change_log rows unchanged); 326 movements (opening_balance 288, sale 17, purchase_receipt 5, transfer_out 8, transfer_in 8); 1,467 units on hand, 0 reserved, 0 in transit; warehouse reorder points 533 (DK 121, FI 99, NO 108, SE 205); change_log 1,537. All 8 store listings at or below their reorder point were refilled, including SE-STO-03 SPH-000014 (0 -> 2).
  - **Verification on the real database:** script 11 33 PASS, 10 INFO; 12 17 PASS, 7 INFO; 13 44 PASS, 6 INFO; 14 36 PASS, 7 INFO; 0 FAIL (160 rows). Network reconciliation: 1,460 opening + 26 received - 19 sold = 1,467.
  - **Afterwards:** Transactions: Check 20/20 orders and 100/100 units of work, 0 to write. Replenishment: Check 17/17, 0 to write, 0 conflicts. Both backups unchanged. Migrations, code and config unchanged.
  - Report: docs/test_reports/2026-11-18_replenishment_pilot_execution_and_verification_report.md
- Restore points: the pre-replenishment backup above (current), and the pre-pilot backup database/backups/nordiccart_oltp_20261118_012230_pre_pilot_transactions.dump. The stock ledger, orders and payments are insert-only or immutable, so rollback means restoring a backup.
- Last business event loaded: 2024-12-02 12:46:04 UTC (receipt PO-SE-2024-000001/DN2). Later activity must start after it (business-time order).
- Implementation 2026-11-18 per the feasibility document, decisions D1 to D7 and D9 to D11 adopted, D8 (demand recalibration) deferred to the combined full-scale simulation:
  - shared stock ledger python/nordiccart/stock/ledger.py (on hand, reserved, in transit);
  - package python/nordiccart/replenishment (config, policy, model, generate, validate, load, cli) with python/run_replenishment.py and 3 VS Code entries (Replenishment: Dry Run, Check, Load);
  - [replenishment] in config/generator.toml
- Transaction loader change: receipts and transfers dated after the pilot's last event are accepted and added to its expected stock, so the pilot keeps verifying. They are still refused while pilot steps are missing, and when dated inside the pilot. The typed confirmation of both command lines now cancels (exit 4) when there is no keyboard input.
- Pilot plan, 17 units of work, fingerprint 858bbfd0…, window 2024-11-15 to 2024-12-06 (no orders):
  - R0: 4 warehouses, 69 reorder points revised to echelon demand (warehouse total 323 -> 533), all below the opening quantity;
  - T1/T2: 4 transfer documents, 8 lines, 10 units, all natural (the 8 store listings at or below their reorder point after the pilot), dispatched and arrived on Monday 18 November before the stores opened;
  - R1: 4 purchase orders flagged forced_reorder (one per market, least headroom), 26 units in 5 delivery notes (SE split into DN1 and DN2);
  - stock 1,441 -> 1,467; nothing in transit; 111 change_log rows
- Writes only through V007: the R0 guarded reorder point update; purchase_receipt, transfer_out and transfer_in inserts in lock order, one transaction each, in business-time order. Every result is compared with the plan. Resumable and rerun-safe:
  - business keys (type, location, product, external_reference; arrivals through related_movement_id);
  - the prefix rule and the after-pilot stock replayed through the done steps;
  - advisory lock; 23505 counts as "already done"
- Verification: new sql/verification/14_verify_replenishment.sql (43 rows: 36 PASS, 7 INFO). Revisions keep the earlier scripts valid before and after replenishment:
  - 11 revision 4: 43 rows, 33 PASS, 10 INFO;
  - 12 revision 3: 24 rows, 17 PASS, 7 INFO;
  - 13 revision 2: 50 rows, 44 PASS, 6 INFO
- Sandbox result 2026-11-18. Disposable PostgreSQL 18.4 cluster (port 55432), restored from the pre-pilot backup, the pilot replayed (100 units of work, fingerprint fabcd571…):
  - replenishment dry run, check, apply (17 units of work) and rerun (nothing to write);
  - the transaction loader still verifies the pilot (21 later movements, +26 units);
  - scripts 01 to 10: 477 PASS, 0 FAIL, 2 expected WARN; 11, 12, 13 and 14 pass;
  - SQL behaviour tests 268/268 with all data present;
  - pytest 276 passed, 0 failed (new: 61 unit + 22 database; transaction database tests 21 -> 23);
  - concurrency 14/14 in 3 runs (3 new scenarios);
  - 11 of 11 deliberate defects caught
- Before the load (sandbox phase), the real database got read-only checks only: the modified transaction loader verified the pilot (0 to write) and the replenishment check showed 17 units of work to write, 0 conflicts
- Guide: docs/phase3_replenishment_guide.md. Report: docs/test_reports/2026-11-18_replenishment_pilot_sandbox_test_report.md
- Assessment (before implementation): docs/phase3_replenishment_feasibility.md (2026-11-18), evidence from read-only queries after the pilot
- Verdict: feasible without a migration. V007 already provides purchase_receipt, transfer_out and transfer_in with the needed ledger safety: no negative stock, reserved units cannot move, idempotent document keys, matched transfer pairs (one arrival per dispatch, same quantity, other location, not earlier), insert-only, change capture. Proven by SQL tests 21 to 43 and 74 and concurrency S8 and S10. The DBML defines these as unit of work U7, "insert movements only"
- Gaps for the application, validator and a new script 14:
  - transfer_out has no destination;
  - in-transit stock is never forced to arrive and cannot be written off;
  - receipts at stores and cross-border transfers are accepted;
  - capacity, product launch and location opening are not checked for movements;
  - occurred_at can be backdated;
  - no purchase order or supplier tables, so on-order stock is invisible to SQL
- Findings:
  - stores sit at display minimums, so they reorder after almost every sale (200 of 216 store listings within one unit of their reorder point);
  - the pilot sold 6.1% of the modelled demand, so a natural run now gives about 8 store transfers (10 units) and no purchase orders;
  - warehouse reorder points count online demand only; echelon demand (online + stores) raises them from 302 to 504 units, still below every opening quantity;
  - the two products launched later have reorder points above 0 at 0 stock, so the trigger must know launch dates
- The assessment's prerequisite (the transaction loader blocked after the first receipt or transfer) is resolved by the loader change above

## Implementation choices (data model unchanged)
Identity GENERATED ALWAYS (generator default is BY DEFAULT); FKs not deferrable (generator emits DEFERRABLE INITIALLY IMMEDIATE); named UNIQUE constraints; migration metadata in schema meta; insert-only guard raises SQLSTATE 23000; business-rule guards raise 23514 with a rule name; the database verifies totals, the application computes them.

## Open design observations (not changed)
- Case-sensitive unique email; no CHECK on stores.store_format; product_prices.currency_code not enforced to match the market currency
- VAT and currency are checked against the current countries row (no history table): the generator must run in date order and update countries.vat_rate when a rate changes
- unit_price and unit_cost are not checked against product_prices (application rule)
- maximum 3 payment attempts is an application rule; an order with total 0 cannot be paid (amount > 0)
- V009 (S4) is additive: receipts with non-PO references and reorder point changes without a revision are still accepted (V007 behaviour) and reported by script 18 (checks 301 and 601) and the replenishment loader. A strict "every receipt has a purchase order" rule is deferred to a later migration once the engine is the only writer.
- V008 must make the two balance-maintenance functions SECURITY DEFINER when it removes UPDATE on the quantity columns, or the triggers fail for the application role (designed in docs/phase3_combined_activity_design.md, section 4.1, together with SECURITY DEFINER for change capture; V008 = security, V009 = activity control and purchase orders). **Implemented in V008 (S3), sandbox-tested, not yet applied.**
- until V008 the quantity guard trusts trigger depth (a write from inside another trigger passes it; the CHECKs still hold). After V008 no application role has UPDATE on the quantity columns.
- transfer_out without transfer_in is allowed (stock in transit, no arrival check)
- V007 additions for decision: external_reference required and unique for receipts, adjustments, write-offs and transfer_out; one opening_balance per balance
- reservation status (released or expired) is not tied to the order's cancellation_reason
- reservation lifetime (15 min online, 5 min in store) is an application parameter
- a negative on-hand is reported as ck_inventory_balances_no_oversell (CHECKs run in name order)
- Phase 3: created_at on seeded rows is the load time, not business time (use registered_at, launched_on, opened_on, valid_from in reporting)
- Phase 3: no closed_on for stock locations (only is_active); market launch dates live in the generator reference data, not in the schema
- Phase 3: the database does not tie an opening_balance to the product launch, an active location, or a time before other movements; the inventory loader and script 12 enforce these
- Phase 3: opening stock is sized for the full business, so pilot orders will hardly draw it down; demand per category is a planning assumption to revisit when the order volume is set
- Phase 3: the transaction loader refuses a database with orders outside its plan or stock movements other than opening balances and sales (since step 4: except receipts and transfers after its last event); a larger run must include the pilot as the start of its plan (or start from the database state), continue the numbering at 21 after the last loaded event, and model restocking
- Phase 3: the pilot scenario mix is weighted to exercise every path (70% fulfilled); the realistic mix and volume are open decisions
- Phase 3: online orders pay the market shipping fee below the free-shipping threshold (part of the V006 order total); shipments are not created yet
- Phase 3: exchange_rates is a daily insert-only series; list prices are set with fixed 2025 ECB averages from config, and the daily rates come with the activity simulation
- Phase 3 (pilot result): the pilot sold 19 units in 14 days, 6.1% of the modelled demand (155.2 units a week for the pilot catalogue); store stock sits at display minimums, so store reorder points are crossed after almost every sale
- Phase 3 step 4: transfer_out stores no destination; a transfer_in at any other location of the same product and quantity is accepted
- Phase 3 step 4: in-transit stock (transfer_out without transfer_in) has no closure; a short or lost transfer can only be recorded as a full arrival followed by a write_off
- Phase 3 step 4: V007 accepts purchase receipts at stores, transfers between countries, movements for products not yet launched or at locations not yet open, and backdated occurred_at; warehouse capacity is stored but not enforced (script 12 check 503 detects it)
- Phase 3 step 4: warehouse reorder points come from online demand only; once stores are replenished from the warehouse, echelon demand applies (302 -> 504 units in total for the pilot)
- Phase 3 step 4: no purchase order or supplier tables in DBML revision 3; on-order stock and supplier lead times exist only in the application plan; the PO number can travel in external_reference
- Phase 3 step 4: all writes must stay in business-time order; replenishment can start only after 2024-11-15 08:44:22 UTC
- Phase 3 step 4 (implemented): the 4 purchase orders of the replenishment pilot are forced (forced_reorder), not triggered by the reorder point; natural warehouse reorders are exercised only in tests until demand and volume are calibrated (D8)
- Phase 3 step 4 (implemented): the standalone replenishment pilot leaves a period without orders (2024-11-15 to 2024-12-06); later orders must start after its last event (2024-12-02 12:46:04 UTC), or the combined simulation generates both
- Phase 3 step 4 (implemented): after R0 the opening inventory check reports 69 reorder point drifts (allowed); script 12 check 501 compares with the current reorder point, which R0 keeps below the opening quantity
- Phase 3 step 4 (implemented): script 14 is pinned to the replenishment pilot counts, and script 13 now accepts receipts and transfers after the pilot; both report FAIL after returns, adjustments or more orders until their params are updated
- Phase 3 step 4 (implemented): the validator rule transfer_position assumes no orders between a store review and its dispatch (true in the standalone pilot); revisit it for the combined simulation

## Phase 3, step 5: combined customer activity and replenishment simulation (S1–S4 complete)
- Design: docs/phase3_combined_activity_design.md, revision 2 (2026-11-18), approved. Revision 1 was approved in principle with three issues; revision 2 resolves them.
- **Stage S1 (implemented 2026-11-18, no database writes, no migration):** report docs/test_reports/2026-11-18_phase3_step5_s1_pilot_scope_test_report.md
  - Shared units of work `python/nordiccart/units/` (U1 to U5, R0, R1, T1, T2, retry wrapper, advisory locks), moved unchanged out of the two pilot loaders; the loaders call them.
  - Pilot scope `python/nordiccart/activity/scope.py`: each pilot loader compares its own records exactly. Any other order or movement is accepted only as later activity that passes identity and integrity rules: after the pilot's last event, numbering continued without reuse or gaps, valid document references, consistent orders and transfer pairs, simulated movement types, exception E2 for extra listings. A later date alone is never enough.
  - Scripts 13 (revision 3) and 14 (revision 2) scoped to their pilots' own records; new script 15: global ledger, documents, order identity and integrity.
  - Tests: 304 passed in the sandbox (276 existing, 28 new); 5 planted defects all caught. Pilot fingerprints unchanged.
  - Compatibility: a sandbox restore of the real backup, with the replenishment pilot replayed by the refactored code, has the same business content as the real database (identical checksums). Read-only checks of the real database: scripts 11 to 15 pass, both loaders 0 to write; database unchanged.
  - Review outcome (2026-11-18): S1 passed. Decisions recorded in the design (section 13): **C17** script 14's pilot-document scope approved; **C18** V009 provides a durable, timestamped reorder point revision history with the R0 backfill, so R0 stays verifiable after monthly R-cal updates (design L10, stage S4); **C19** full-profile revisions of scripts 11 and 12 stay in S7.
- **Stage S2 (implemented 2026-11-18, no database access, no migration):** report docs/test_reports/2026-11-18_phase3_step5_s2_demand_calibration_report.md
  - Package `python/nordiccart/demand/`:
    - personal rates Gamma(0.8, mean 2.4);
    - seasonal exposure from local registration dates;
    - normalized monthly index, day weights (weekdays, store closed days, Black Week ×3) and hour weights;
    - channel propensity solved to an online share of exactly 0.55;
    - store choice by format and home city;
    - product mix from the opening model's weights × product life cycle;
    - expected orders and units by market, channel, location, product and period;
    - seeded, window-composable order draws (the engine's order source in S6);
    - recalibrated opening stock (S7) and R-cal reorder points (S6).
  - Configuration: new `[demand]` section in `config/generator.toml`; no existing value changed; pilot fingerprints unchanged.
  - Calibration (full profile in memory): expected orders 42,777 in the primary year (design 42,772), 29,858 in the growth year (29,817), 72,635 for 2024-12-03 to 2026-10-31 (72,589). Seeds 1 to 5: 104 of 104 statistical checks each. Calibration command `python/run_demand_calibration.py`.
  - Inventory finding: the opening model's 2,928 units a week are 2.2 times (first combined week) to 3.8 times (average week) the demand model's expected units. Recalibrated opening stock under the same rules: 19,395 units instead of 28,109. The 20 pilot products (loaded, unchanged) cover 19.8 weeks.
  - Tests: 44 new (33 deterministic, 11 calibration); 348 passed with the sandbox; 9 planted defects caught.
  - Open for review: the implementation choices in section 9 of the S2 report; the 11.1 inventory criteria need the engine (S6/S8).
  - Review outcome: S2 passed.
- **Stage S3: V008 database security. Implemented and tested in the disposable PostgreSQL 18.4 sandbox on 2026-11-18, then DEPLOYED to the real database on 2026-11-18 and verified; the application connects as `nordiccart_loader`. S3 complete.**
  - Sandbox report: docs/test_reports/2026-11-18_phase3_step5_s3_v008_security_sandbox_test_report.md
  - Deployment report: docs/test_reports/2026-11-18_s3_v008_real_deployment_report.md
  - The sandbox phase is described below; the readiness, deployment and application-switch records follow it.
  - **Migration** `database/migrations/V008__security_roles_and_privileges.sql`, one transaction ending in a self-check that rolls everything back on any deviation:
    - 7 cluster roles:
      - `nordiccart_owner` (NOLOGIN, owns every object of public and meta);
      - `nordiccart_writer` and `nordiccart_reader` (NOLOGIN groups);
      - `nordiccart_loader` and `nordiccart_scheduler` (LOGIN, in writer);
      - `nordiccart_analyst` (LOGIN, in reader);
      - `nordiccart_extractor` (LOGIN).
    - Memberships INHERIT TRUE, SET FALSE. No elevated attribute. No passwords: set at deployment, never in the repository.
    - `fn_inventory_apply_movement`, `fn_inventory_apply_reservation` and `fn_capture_change` are SECURITY DEFINER, with `search_path = pg_catalog, public, pg_temp`.
    - PUBLIC keeps nothing on the database, the schemas, the tables or the functions.
    - Writer:
      - SELECT and INSERT on the 16 business tables;
      - column-level UPDATE on lifecycle and status columns only (no quantities, totals, lines, ledger or history);
      - SELECT on 5 change_log metadata columns;
      - EXECUTE on the 3 unit-of-work functions and the 3 functions they and the commit-time checks call.
    - Reader: SELECT on all 19 tables.
    - Extractor: change_log SELECT and DELETE; checkpoints.
    - Settings: per-role timeouts (reset first, then set) and connection limits (loader 30, scheduler 3, analyst 10, extractor 3). Analyst read-only by default.
    - No table, constraint, trigger or function body changed. No data row written except V008's own migration row.
  - **Also new:**
    - `database/rollback/R008__undo_security_roles_and_privileges.sql` (in place, tested to restore the exact pre-V008 catalog);
    - `database/security/V008_reapply_database_privileges.sql` (after restores or template copies);
    - `sql/verification/17_verify_v008_security.sql` (31 rows: 27 PASS, 4 INFO after V008);
    - `tests/sql/test_v008_roles.sql` (87 tests);
    - `tests/python/test_v008_security_db.py` (75 tests);
    - `tests/python/test_v007_concurrency.py` options `--through 8 --role`.
  - **Documents:** privilege matrix docs/phase3_v008_security_privilege_matrix.md; deployment and rollback guide docs/phase3_v008_deployment_and_rollback_guide.md.
  - **Sandbox results:**
    - V008 database tests 75/75;
    - SQL suites on V008: 44/44, 119/119, 105/105, 87/87;
    - concurrency 14/14 as superuser on V007 and 14/14 as `nordiccart_loader` on V008;
    - full Python suite 423 passed, 0 failed, 0 skipped (348 before S3 + 75 new);
    - privilege matrix 360/360 entries;
    - every pilot loader as `nordiccart_loader` writes data identical to the superuser load;
    - scripts 01–15 unchanged by V008 and passing as `nordiccart_analyst`;
    - 10 of 10 planted defects caught (4 by V008's own self-check).
  - **Real-state rehearsal** on a copy with the real database's business content, starting from a server with no NordicCart role:
    - preflight clean;
    - V008 in 139–252 ms;
    - content unchanged (checksums, change_log 1,537);
    - all 4 command lines `--plan` as the loader: 0 to write;
    - R008 restored the exact catalog and dropped the roles;
    - V008 re-applied.
  - **Fixed during S3:**
    - V008 now resets and verifies role settings (mutation M8 was not caught before);
    - script 17 reports FAIL instead of an error when the roles do not exist.
  - **Deviations from design 4.1:**
    - writer reads change_log metadata columns (loaders count their change_log rows);
    - `pg_temp` explicitly last in the definer search path;
    - statement_timeout for all login roles; analyst idle 5 min;
    - explicit connection limits;
    - `meta.activity_*` grants move to V009;
    - V008 runs as superuser;
    - PostgreSQL 16+ required.
  - **Unresolved (report section 9):**
    - a role can change its own timeouts (not a security control);
    - database ACLs are lost on pg_restore into an existing database and on template copies (reapply script, script 17 check 301);
    - revoking PUBLIC CONNECT affects other non-superuser login roles (preflight 1.6);
    - extractor DELETE not limited to consumed rows (Phase 4 purge function);
    - login roles need passwords at deployment, and password login was not tested (sandbox trust authentication);
    - `config/.env` still uses `postgres` until you switch it;
    - `pg_hba.conf` not reviewed;
    - analyst read-only default is a convenience;
    - `postgres` remains the only administrative login.
  - Review outcome: S3 approved (ChatGPT).
  - **Pre-deployment readiness (2026-11-18)**, real database read-only. Report: docs/test_reports/2026-11-18_s3_v008_predeployment_readiness_report.md
    - Preflight clean: migrations 001–007; PostgreSQL 18.4; only the `postgres` role; owners `postgres`; default privileges; 20 orders, 326 movements, 1,467 units, 1,537 change_log rows. Scripts 01–15 0 FAIL. All four `--plan` checks: 0 to write.
    - `pg_hba.conf`: scram-sha-256 on every rule, localhost only, so the new login roles need passwords.
    - Backup `database/backups/nordiccart_oltp_20261118_163550_pre_v008.dump`: 260,854 bytes, SHA-256 090AA80A…56A9D70. A full `--create` restore in the sandbox is byte-identical (19/19 tables, sequences, owners, privileges). V008 and R008 dry runs on copies of it passed.
    - Content fingerprint `b6317bea…d58843`, to recheck before deployment.
    - `.gitignore` now excludes backups, dumps and env/password-file copies. The dumps were committable before.
    - V008 not applied at that point; deployment waited for explicit authorization.
  - **Deployed to the real database 2026-11-18** (written authorization). Report: docs/test_reports/2026-11-18_s3_v008_real_deployment_report.md
    - Preflight clean; fingerprint unchanged since readiness, so the pre-V008 backup stayed the restore point. The one idle session held no locks and was left open.
    - V008 via psql as `postgres` with `ON_ERROR_STOP=1`: exit 0, `COMMIT`, 375 ms; migration 008 recorded at 16:46:06 UTC.
    - Scripts 01–15 and 17: 789 rows, 671 PASS, 116 INFO, 0 FAIL, 2 known WARN; script 17: 27 PASS, 0 FAIL.
    - Data unchanged: 18 of 19 tables byte-identical, `meta.schema_migrations` +1 row; 20 orders, 326 movements, 1,467 units, 1,537 change_log rows.
    - Privilege matrix on the real database: 360/360.
    - The login roles have no password and cannot log in (verified). `config/.env` still uses `postgres`.
    - Loader password set by you (`\password`, stored in `pgpass.conf`; never seen by the assistant).
    - Restricted connection test passed: session `nordiccart_loader`, not a superuser, role timeouts in effect, allowed privileges present and forbidden ones absent.
    - Transactions: Check and Replenishment: Check as the loader: 0 conflicts, nothing to write. No record changed.
    - **S3 complete.**
    - You switched `config/.env` to `nordiccart_loader` and removed `PGPASSWORD`. The application now connects as the loader, through `pgpass.conf`.
    - Both pilot Checks: 0 conflicts, nothing to write; no record changed. Report: docs/test_reports/2026-11-18_s3_v008_application_connection_verification_report.md
    - Administrative work (backups, migrations, R008) must now name `postgres` explicitly.
- **Stage S4: V009 activity control and purchase orders.** **Phase 4 SUCCESS. Overall S4 deployment SUCCESS.** V009 and the historical pilot-document backfill are deployed and verified on `localhost:5432/nordiccart_oltp`. Final verification across scripts 01–15, 17 and 18 returned **709 PASS, 171 INFO, 3 expected WARN, 0 FAIL** (883 rows). Report: `docs/test_reports/2026-11-19_s4_v009_real_deployment_report.md`.
  - **Readiness (2026-11-19):** docs/test_reports/2026-11-19_s4_v009_predeployment_readiness_report.md
    - Pre-V009 backup `nordiccart_oltp_20261119_041244_pre_v009.dump`, taken by you as `postgres`: verified (SHA-256 `03B68948…0825`) and fully restored in the sandbox.
    - The restored copy matches the last verified real state: 19 of 19 tables byte-identical; catalog and effective privileges as V008 designed.
    - Script 14: 37 PASS, 7 INFO, 0 FAIL.
    - V009 rehearsed on copies of the restored backup: backfill, scripts 01–18 with 0 FAIL, and R009 restoring the pre-V009 catalog exactly.
  - **Migration** `database/migrations/V009__activity_control_and_purchase_orders.sql`:
    - one transaction, run as postgres with `SET ROLE nordiccart_owner` (design 4.1);
    - a self-check (owners, SECURITY DEFINER settings, privileges) rolls everything back on any deviation;
    - additive: no V001–V008 object or row changed.
  - **Purchase orders (L1):**
    - `purchase_orders`: warehouse only; number `PO-<market>-<local year>-<seq>`.
    - `purchase_order_lines`: reason `reorder_point`, `launch`, `seasonal` or `forced_reorder`; ordered, received and generated outstanding quantities.
    - Statuses open → partially_received → received, derived from the ledger.
    - A purchase receipt with a `<PO>/DN<n>` reference must fit its order: existing order of that warehouse, a line for the product, not before the order, never more than ordered. A SECURITY DEFINER trigger on `inventory_movements` moves the received quantities and statuses; the writer has no UPDATE on these tables.
    - Receipts with other references keep V007 behaviour; script 18 reports them.
  - **Reorder point revision history (L10, C18):** `reorder_point_revisions`, insert-only.
    - Per listing, revisions are in business-time order and chained (old = previous new).
    - The balance holds the old or new value when a revision is written; at commit the latest revision equals the balance.
    - A reorder point change without a revision is still accepted (V007 behaviour) and is reported as drift.
  - **Run ledger (L2, design 7.3):**
    - `meta.activity_runs`: contiguous, non-overlapping windows (exclusion constraint), chained by `previous_run_id` with `UNIQUE NULLS NOT DISTINCT` (one first run, one successor per run); immutable window, fingerprints, start state and planned units; running → verified | failed, failed → running; verified is final.
    - **S4 review fix (2026-11-19):** ChatGPT found that two concurrent sessions could start disjoint first windows on an empty ledger. Reproduced: both committed, in READ COMMITTED and REPEATABLE READ. The unique predecessor link fixes it: the second start waits, then fails with 23505, in any isolation level. Three regression tests added; mutation M15 (fix removed) is caught.
    - `meta.activity_checkpoints`: insert-only; a run becomes verified only together with its checkpoint (deferred checks, same hash).
    - Not captured in change_log (engine metadata).
  - **Privileges:** writer SELECT and INSERT on the five tables, UPDATE only of the run status columns, EXECUTE on two read-only helpers; reader SELECT; nothing for the extractor or PUBLIC; no DELETE or TRUNCATE (guards bind the owner too).
  - **Pilot documents (E3, C18)** are written by the replenishment loader when V009 is present, as `nordiccart_loader`, with no movement:
    - real backfill: four received purchase orders, four lines and 69 R0 revisions committed in one transaction. The 81 new change-log entries brought the total to 1,618. Final loader plans found zero work pending.
    - fresh load on V009: the R0 and receipt units write the same documents inline.
    - The loader verifies R0 from the history and the order documents against the plan; later revisions after the pilot are accepted when chained from the pilot's value.
  - **Also new:**
    - rollback `database/rollback/R009__undo_activity_control_and_purchase_orders.sql`;
    - verification `sql/verification/18_verify_v009_activity_control.sql` (44 rows: 38 PASS, 6 INFO after the backfill);
    - `tests/sql/test_v009_activity_control.sql` (96 tests);
    - `tests/python/test_v009_db.py` (68 tests);
    - DBML `database/design/nordiccart_oltp_rev4.dbml`;
    - guide `docs/phase3_v009_deployment_and_rollback_guide.md`.
  - **Changed:**
    - script 14 revision 3 (reorder points as of the pilot's last event, from the history when present);
    - script 17 revision 2 and `tests/sql/test_v008_roles.sql` revision 2 (V008 exact lists scoped to V001–V008 objects; same results on V008);
    - `nordiccart/units/base.py` (`has_activity_control`), `units/stock.py` (document writers), `replenishment/model.py` (`ReorderPointRevision.reference` property, fingerprint unchanged), `replenishment/load.py` and `cli.py` (documents);
    - `tests/python/test_v007_concurrency.py` (`--through 9`).
  - **Sandbox results:**
    - full Python suite 491 passed, 0 failed, 0 skipped (423 + 68 new), after the review fix;
    - SQL suites on V009: 44/44, 119/119, 105/105, 87/87, 96/96;
    - concurrency 14/14 on V009 as the loader;
    - privilege matrix 1,616/1,616;
    - mutation checks 15/15 caught (2 by V009's own self-check).
  - **Real-state rehearsal** on a copy of the rebuilt real state (pre-V008 backup + approved V008), rerun after the review fix:
    - V009 in 195 ms, business data unchanged;
    - script 18 right after V009: 28 PASS, 6 INFO, 10 expected FAIL;
    - loader backfill: 8 documents, 81 change_log rows, no movement, 1.2 s;
    - scripts 01–15, 17 and 18 as the analyst: 883 rows, 0 FAIL, 3 expected WARN (script 10 now also lists V009's receipt trigger on a V007 table);
    - R009 restores the V008 catalog exactly (963 entries), and V009 re-applies.
  - **C18 demonstrated:** after a later monthly review of 18 warehouse reorder points, the loader and script 14 still verify R0 on V009, while the same change on V008 breaks both.
- Scope:
  - one chronological engine, in UTC-day windows (hourly later), for orders, payments, reservations, fulfilment, store transfers, supplier receipts, monthly reorder point reviews, price changes and daily exchange rates;
  - continuation after the loaded history (orders from sequence 21, transfer and purchase order numbers from 000002) without changing any pilot record;
  - scale-up to 500 products and 20,000 customers;
  - 24 months generated, primary analytics year 2025-11-01 to 2026-10-31
- Revision 2 corrections:
  - **State and recovery:** engine state is derived from the database. The V009 tables (purchase orders, `meta.activity_runs`, `meta.activity_checkpoints`) move before the historical catch-up, and each window has a crash-safe protocol (run row, units of work, verification, then checkpoint and status in one transaction, with a state hash compared at the next window). File-based state is rejected because a restore would leave it out of step.
  - **Calibration:**
    - the zero-purchase share for λ̄ 2.4, k 0.8 is 33.0% of full-year customers (revision 1 said about 45%), 55.3% of customers registered during the year, and 37.4% overall in the primary year;
    - exposure is weighted by season;
    - the monthly index is normalized by its day-weighted mean 1.001233;
    - weekday, closed-day, Black Friday and hour weights are normalized within each month and day;
    - expected orders: 72,589 (2024-12-03 to 2026-10-31), of which 42,772 in the primary year;
    - exact and statistical calibration criteria
  - **Migration order:** V008 security first (owner, writer, loader, scheduler, reader and extractor roles; column privileges; SECURITY DEFINER for balance maintenance and change capture; timeouts), then V009 with grants on those roles. Both run before the full-scale catch-up; hourly automation only after V008 is deployed and tested, as `nordiccart_scheduler`.
  - **Explicit exceptions to business-time write order:** E1 exchange rate backfill for 2024-11-01 to 2024-12-02 (96 rows); E2 opening balances of the 480 new products (listings without activity only); E3 pilot purchase order documents (decision C15).
  - **Year over year:** November 2025 cannot be compared with the pilot-only November 2024; December is compared like for like (from the 3rd); January to October 2026 are representative.
  - **Monthly batches:** the catch-up runs in 23 monthly batches, each verified (scripts 11 to 16) and followed by a pg_dump recovery point with its SHA-256.
- Facts from the full profile (generators run in memory, no database):
  - 20,000 customers (SE 8,031, DK 4,471, NO 3,952, FI 3,546), of which 12,690 registered by the end of the loaded history and 16,014 by 2025-11-01;
  - 500 products, 381 on sale on 2024-11-01 and 119 launched during the calendar;
  - opening stock 28,109 units for 2,928 modelled units a week, 2.2 to 3.4 times the demand of the recommended order volume
- Open decisions C1 to C16 (design, section 13)

## Next steps
Revised implementation sequence (design, section 12). Stages marked DB need a backup, a readiness report and explicit authorization.
1. S1: pilot loaders accept later activity; scripts 13 and 14 scoped; script 15 skeleton; shared unit-of-work module (no database writes). **Done 2026-11-18, reviewed and passed.**
2. S2: demand model, normalization, calibration report and calibration tests (no database writes). **Done 2026-11-18, reviewed and passed.**
3. S3 (DB): **V008 security** (roles, column privileges, SECURITY DEFINER, timeouts); privilege matrix tests; all suites as `nordiccart_loader`; `config/.env` switches to the loader role. **Done 2026-11-18:**
   - implemented, sandbox-tested and approved;
   - pre-deployment readiness completed;
   - deployed to the real database per docs/phase3_v008_deployment_and_rollback_guide.md and verified;
   - loader password set by you;
   - restricted loader connection verified;
   - `config/.env` switched to `nordiccart_loader` by you and verified.
4. S4 (DB): **V009** purchase orders, run ledger and checkpoints, timestamped reorder point revision history with the R0 backfill (C18), DBML revision 4; pilot purchase order backfill (E3). **Status:**
   - implemented and sandbox-tested 2026-11-18;
   - review finding (concurrent first windows) fixed 2026-11-19;
   - approved for controlled deployment;
   - pre-deployment readiness complete 2026-11-19: backup verified, test restore, rehearsal; **verdict READY**;
   - V009 and the historical-document backfill deployed and verified on 2026-11-19. Phase 4 and overall S4 deployment SUCCESS.
5. S5 (DB): implemented and sandbox-verified. E1 deployed on the real database on 2026-11-19 (96 rates, verified, SUCCESS); awaiting review. Deployment record: `docs/test_reports/S5_real_deployment_readiness_report.md`. Report: `docs/test_reports/S5_exchange_rates_implementation_report.md`.
6. S6: window engine and run protocol at pilot scale in a V009 sandbox; composition, crash and recovery, and drift tests. **Status:**
   - milestone 1 implemented, sandbox-verified and reviewed (`docs/test_reports/S6_activity_engine_implementation_report.md`);
   - milestone 2 (incremental replay, pilot loaders honour the activity lock, identity-based sandbox guard) sandbox-verified and reviewed (`docs/test_reports/S6_M2_incremental_replay_report.md`);
   - milestone 3 (R-cal, P1, month-end verification, carry integrity) sandbox-verified and reviewed (`docs/test_reports/S6_M3_implementation_report.md`);
   - **S6 COMPLETE at pilot scale (2026-11-20).** Monthly recovery points (7.4) are deferred to the historical loading (S8/S9); cheaper state reads at full scale go to S8. No further S6 infrastructure.
7. S7 (DB): scale-up to the full profile; opening inventory extension (E2); scripts 11 and 12 full-profile revisions. **Status:**
   - sandbox-verified on 2026-11-20 and awaiting review (`docs/test_reports/S7_implementation_report.md`);
   - full profile: 500 products, 2,000 prices, 20,000 customers; 12 stores and 4 warehouses unchanged;
   - demand-calibrated opening inventory: 8,000 listings, 6,096 opening balances, 19,866 units (5,808 E2 opening balances through the existing loader);
   - pilot records and exchange rates identical; scripts 01–18 0 FAIL;
   - engine at full scale: 31 windows verified;
   - deployment readiness READY on 2026-11-20: regression 527 passed; read-only real plans as expected with 0 conflicts; pre-S7 backup verified and restored in the sandbox;
   - **deployed to the real database on 2026-11-20: SUCCESS.** All counts as expected; pilot, E1 and scripts 01–18 checks pass (0 FAIL). Report `docs/test_reports/S7_implementation_report.md`, section 7.
8. S8: 24-month rehearsal in the sandbox (23 monthly batches, recovery points, restore drill, timings). **Status:** rehearsal complete on 2026-11-20 (698 windows, 23 months verified, 15 distinct recovery points independently restored, scripts 0 FAIL); operational review passed (`docs/test_reports/S8_historical_simulation_report.md`). **S8.1** (2026-11-20): S recomputed at R-cal and script 15 optimized, both verified in the sandbox; review passed (`docs/test_reports/S8_1_calibration_and_performance_report.md`). **S8.2** (2026-11-20): final full-scale rehearsal with the corrected engine, PASS, awaiting review (`docs/test_reports/S8_2_final_full_scale_rehearsal_report.md`).
9. S9 (DB): real historical catch-up 2024-12-03 to 2026-10-31 in 23 verified monthly batches with recovery points. **Status: COMPLETE (2026-11-20), SUCCESS.** 698 windows and 23 months verified on the real database, identical to S8.2. The historical phase is closed; next are hourly automation, ETL, FastAPI and Power BI
10. S10 (DB): live mode (catch-up to the present), then hourly automation as `nordiccart_scheduler`, only after V008 is deployed and tested
11. S11: Phase 4 extraction and Power BI (primary analytics year, year-over-year rules)
12. Later: returns, refunds, shipments, promotions; FastAPI; optional transfer destination column and the replenishment hardening in section 9 of the feasibility document
