# Phase 3, step 4: inventory replenishment and stock transfers. Design and feasibility

| Item | Value |
|---|---|
| Date | 2026-11-18 |
| Status | Assessment only. Nothing implemented, no migration changed, nothing written to PostgreSQL. |
| Based on | Migrations V001 to V007, `database/design/nordiccart_oltp_rev3.dbml`, `python/nordiccart` (master_data, inventory, transactions), `config/generator.toml`, verification scripts 01 to 13, tests in `tests/python` and `tests/sql`, and read-only queries on `nordiccart_oltp` after the pilot transactions (state at 2026-11-18 01:35) |
| Audience | Project owner and design reviewer |

## 1. Summary

**Feasible without a new migration.** V007 already has the movement types (`purchase_receipt`, `transfer_out`, `transfer_in`) and the safety mechanisms replenishment needs:

- no negative stock, and no moving of reserved units;
- idempotent document keys;
- matched transfer pairs (one arrival per dispatch, same quantity, other location, not dated earlier);
- an insert-only ledger, captured in `change_log`.

These are already proven by the ledger tests 21 to 43 and test 74 in `tests/sql/test_v007_inventory.sql`, and by concurrency scenarios S8 and S10 in `tests/python/test_v007_concurrency.py`. The DBML defines receipts and transfers as unit of work U7, "insert movements only".

**Gaps.** V007 does not know *where* stock may go, *when*, or *whether it arrived*:

- a `transfer_out` has no destination;
- in-transit stock is never forced to arrive;
- receipts can be posted at stores;
- transfers can cross borders;
- warehouse capacity, product launch dates and location opening dates are not checked;
- `occurred_at` can be backdated;
- purchase orders and suppliers have no tables, so "on order" stock is invisible to the database.

All of these can be enforced by the generator, its validator and a new verification script (14). Optional V008 hardening is listed in section 9.

**Three findings shape the design:**

1. **Stores reorder after almost every sale.** Store stock sits at display minimums, and the store reorder point is display minimum − 1. After the pilot, 200 of the 216 store listings on sale are within one unit of their reorder point, and 8 are already at or below it. Every warehouse listing is at least 2 units above its reorder point (median 6).
2. **Demand and stock are on different scales.** The 19 units sold in the pilot are 6.1% of the modelled demand the opening stock was sized for (155.2 units a week). A replenishment run now would create about 8 store transfers (10 units) and **no purchase orders**, even with corrected warehouse reorder points. A pilot that must exercise purchase receipts needs a deliberate scenario mix, as the transaction pilot had.
3. **The pilot transaction loader would block.** `run_transactions.py --plan/--apply` refuses any movement other than opening balances and sales, and compares every balance with "opening stock + pilot orders". The first receipt or transfer makes it exit with code 2. It needs a small, tested change before any replenishment is loaded (section 7.2).

**Also needed:**

- **Warehouse reorder points:** the opening inventory model sizes them from online demand only, but the warehouse would also resupply its stores. Echelon demand raises the total warehouse reorder points from 302 to 504 units (decision D2).
- **Business-time order:** writes must stay in business-time order. Replenishment can start only after the last pilot event (2024-11-15 08:44:22 UTC). Backdating would break the stock history and script 13, check 411.

## 2. Evidence: current state (read-only, after the pilot)

| Measure | Stores | Warehouses |
|---|---|---|
| Listings (on sale / total) | 216 / 240 | 72 / 80 |
| Units on hand | 570 | 871 |
| Reorder point range (listings on sale) | 0 to 3 | 2 to 14 |
| On-hand range (listings on sale) | 0 to 8 | 4 to 50 |
| Listings on sale at or below reorder point | **8** | **0** |
| Listings on sale within 1 unit of reorder point | 200 | 0 (minimum headroom 2, median 6) |
| Modelled weekly demand (opening inventory model) | 69.82 units | 85.34 units (online only) |
| Capacity used | n/a (no store capacity in the schema) | 0.29% to 0.32% (50,000 to 120,000 units) |

Listings at or below their reorder point (on hand / reserved / reorder point): DK-STO-01 ACC-000013 2/0/3, SE-STO-03 SPH-000014 0/0/1, DK-STO-01 ACC-000010 3/0/3, DK-STO-01 SPH-000019 2/0/2, FI-STO-02 WEA-000020 1/0/1, NO-STO-03 GAM-000008 1/0/1, NO-STO-03 SPH-000019 1/0/1, SE-STO-03 SMH-000009 2/0/2.

If they were replenished up to their opening quantity, that would mean 8 transfer lines and 10 units: DK 4 units on 3 lines (all DK-STO-01), SE 3 units on 2 lines, NO 2 on 2, FI 1 on 1. The source warehouses would stay far above their reorder points; DK-WH-01 ACC-000013, for example, would go from 17 to 15 with a reorder point of 5.

Pitfall found: the two products launched later (CAM-000007 on 2025-03-19, SMH-000011 on 2025-10-19) are listed at 0 with reorder points that add up to 20 and 41. A plain "on hand ≤ reorder point" rule would order them **months before launch**. The trigger must know about launch dates.

The pilot catalogue has no discontinued products and no locations opening after 2024-11-01. The rules for both are still needed for the full profile.

## 3. What V007 provides and what is missing

| Need | V007 mechanism | Verdict | Existing proof |
|---|---|---|---|
| A receipt raises warehouse stock | `purchase_receipt` must be positive; AFTER trigger adds it to `quantity_on_hand` | Supported | SQL test 22 |
| A receipt is counted once | `external_reference` required (guard) + unique `(movement_type, location, product, external_reference)`; a retry gets 23505 | Supported | SQL tests 21, 23; concurrency S10 (two sessions, one 23505) |
| Many receipts at once do not lose updates | Row lock on the balance + trigger arithmetic | Supported | Concurrency S8 (20 parallel receipts, ledger = balance) |
| A transfer never takes reserved units or goes below zero | `ck_inventory_balances_no_oversell`, `ck_inventory_balances_on_hand` | Supported | SQL test 27 (write-off); test 74 (write-off of reserved units). **Not tested for `transfer_out`** (same CHECK) |
| A transfer dispatch is counted once | `external_reference` required + document unique index | Supported | Same index as receipts |
| An arrival matches its dispatch | Guard: `transfer_in` → a `transfer_out` of the same product, opposite quantity, other location, not dated earlier | Supported | SQL tests 33 to 37 |
| One arrival per dispatch | `uq_inventory_movements_one_transfer_in_per_out` | Supported | SQL test 38 |
| Ledger cannot be edited, history kept | Insert-only trigger, no TRUNCATE, `change_log` capture | Supported | SQL tests 39 to 41 |
| Destination of a transfer known at dispatch | None: `transfer_out` has no destination column | **Gap** | — |
| Every dispatch eventually arrives | None: in transit indefinitely is allowed | **Gap** | (status doc observation) |
| Lost or short transfers | Not representable: the arrival must equal the dispatch; in-transit stock cannot be written off | **Gap** | — |
| Receipts only at warehouses; transfers warehouse → store in the same market | None | **Gap** | — |
| Product launched, location open, not in the future | None for movements | **Gap** | (status doc observation for opening balances) |
| Warehouse capacity | `storage_capacity_units` stored, CHECK > 0 only | **Gap** (detect-only: script 12, check 503) | — |
| Stock never negative *at any business time* | CHECKs see the current balance only; `occurred_at` can be backdated | **Gap** | — |
| Reorder point | `inventory_balances.reorder_point` (≥ 0), settable by UPDATE, captured in `change_log` | Supported as a stored value; no history, no trigger | SQL test 19 |
| Order-up-to level, case pack, lead time, review schedule | None | Application settings | — |
| Purchase orders, suppliers, on-order stock, purchase cost | No tables (not in DBML revision 3) | **Gap**, outside the approved design | — |
| Units of work | Functions exist for U2, U3/U4 and U5 only | Application inserts under locks (optional V008 functions) | — |

Conclusion for question 4: **the ledger itself is safe**. The database cannot create units, lose them silently, double-count a document, move reserved stock or go negative. Placement, timing, completeness and policy rules are application responsibilities. Each one gets a validator rule (section 7.4) and a verification check (section 8).

## 4. Purchase receipts: how they increase warehouse stock

| Aspect | Design |
|---|---|
| Unit of work | **R1 Receive delivery**: one transaction per delivery note (all its lines). The loader locks the warehouse balance rows `FOR UPDATE` sorted by `(stock_location_id, product_id)`, checks capacity, then inserts one `purchase_receipt` per line in the same order. The V007 trigger raises `quantity_on_hand`. |
| Where | Warehouses only (`location_type = 'warehouse'`, active, `opened_on` ≤ receipt day). Direct-to-store deliveries are out of scope. |
| When | Business days (Mon to Fri) within receiving hours, local time (proposal 07:00 to 15:00). Order day + supplier lead time (`lead_time_weeks.warehouse` = 2, seeded 10 to 14 days). Never before 2024-11-15 08:44:22 UTC and never in the future. |
| Document | `external_reference` = `PO-<market>-<year>-<seq>/DN<n>`, for example `PO-SE-2024-000001/DN1`, at most 64 characters. The purchase order number is kept even though there is no PO table, and `DN2` allows a split delivery. Unique per type, location and product (V007 index), so a retry gets 23505 and counts as "already done" (DBML retry rule). |
| Quantity | Order-up-to rule (section 6), rounded up to a case pack per category (new setting), > 0. A split delivery's notes add up to the PO line. |
| Capacity | After the receipt, the warehouse total ≤ `storage_capacity_units`. Simulated in the plan and checked again under the loader's advisory lock (one loader at a time). Script 12 check 503 and script 14 detect it afterwards. |
| New products | First stock arrives as a receipt shortly before launch (proposal: 3 to 7 days before `launched_on`, at the market warehouse). Store allocation follows as transfers that arrive by launch day. This matches the opening inventory rule "first stock arrives as a purchase_receipt in the activity simulation". |
| Discontinued products | No purchase order whose receipt would fall on or after `discontinued_on`. |
| Cost | Not recorded: there is no cost column on movements. COGS keeps using the order-line `unit_cost` snapshot (DBML reporting rules), so inventory valuation stays at `product_prices.unit_cost`. Purchase price variance is out of scope. |
| change_log | 2 rows per receipt line (the movement and the balance update). |

On-order stock exists only in the plan (and its CSV preview), because the database has no purchase orders. The loader's resume logic therefore keys on delivery-note references: a receipt is either present with equal values (done), absent (to write), or different (conflict).

## 5. Stock transfers: warehouse → store without creating or losing units

| Aspect | Design |
|---|---|
| Units of work | **T1 Dispatch**: lock the source warehouse balance rows in lock order, check `on_hand − reserved ≥ quantity`, insert one `transfer_out` per line. **T2 Arrive**: lock the destination store balance rows in lock order, look up each `transfer_out` by its business key, insert `transfer_in` with `related_movement_id`. Separate transactions, so stock is in transit in between. |
| Direction | Market warehouse → store of the **same market** only (no cross-border transfers: different country, currency and VAT). No store → store or store → warehouse in this step. |
| Document and destination | `external_reference` = `TR-<market>-<year>-<seq>-<destination>`, for example `TR-SE-2024-000001-SE-STO-03`, on both the dispatch (required) and the arrival (optional in V007, required by this design). The destination becomes visible to SQL checks even though V007 has no destination column. One document per (warehouse, store, dispatch), one line per product. |
| When | The store review runs after closing (proposal 20:00 local, Mon to Sat). The dispatch goes the next warehouse business morning (06:00 to 08:00). Arrival is the same morning before store opening (10:00), or the next delivery day; transit 1 business day at most. No arrivals on Sundays (stores are closed). |
| Conservation | V007 guarantees, per pair, the same product, the opposite quantity, another location, arrival not before dispatch, and one arrival per dispatch. The design adds: every dispatch arrives within the transit limit, at the store named in its reference. The system-wide identity, checked by the validator and script 14: **Σ on hand + Σ in transit = Σ opening + Σ receipts − Σ sales** (+ adjustments − write-offs later). In transit = dispatches without an arrival. |
| Reserved stock | Never moved: V007's no-oversell CHECK refuses it with 23514. The plan computes quantities from simulated available stock, so a 23514 at load time is an unplanned result: the loader rolls back and stops, and does not retry (DBML retry rule). |
| Short or damaged arrivals | V007 cannot record a partial arrival. The design rule is to record the full arrival, then a `write_off` (or negative `stock_adjustment`) at the store with its own document. Not part of the first pilot. |
| change_log | 2 rows per movement, so 4 per transfer line. |

**Allocation when the warehouse is short** (decision D5): store requests are filled in priority order (stores at 0 first, then flagships, then the lowest available/reorder point ratio). By default the warehouse is drawn down only to its own reorder point, which protects online orders. The rest waits for the next review. The shortfall also triggers the warehouse's own purchase order.

## 6. Reorder points: how they trigger replenishment

Policy: **(s, S) with periodic review**. s = the stored `inventory_balances.reorder_point`; S = an order-up-to level computed by the application.

| | Store | Warehouse |
|---|---|---|
| Review | Daily after closing (Mon to Sat) | Weekly, Monday 07:00 local (proposal), plus a launch review for new products |
| Inventory position (IP) | on hand − reserved + in transit to the store | on hand − reserved + on order (open PO lines) |
| Trigger | IP ≤ reorder point, product on sale or within the pre-launch window, location open | Same |
| Order-up-to S | max(display minimum, ⌈weekly × `weeks_of_cover.store` (6)⌉), and S > reorder point | max(`warehouse_minimum_units`, ⌈demand × `weeks_of_cover.warehouse` (10)⌉), rounded up to the case pack, within capacity |
| Quantity | S − IP from the market warehouse (allocation rule above) | S − IP from the supplier |
| Lead time in the reorder point | `lead_time_weeks.store` = 1 (transit is ≤ 1 day, so this includes review and buffer) | `lead_time_weeks.warehouse` = 2 |

Order of a review day: (1) arrivals and receipts due, (2) orders (in later combined runs), (3) store review → transfer requests, (4) warehouse review on Mondays, using IP *after* the day's transfer requests, so the warehouse reorders what the stores pulled.

**Warehouse demand (decision D2).** The opening model sizes the warehouse from online demand only (`online_share` 0.55). Once stores are replenished from the warehouse, its real outflow is online plus store demand ("echelon demand"):

- Echelon reorder point = ⌈(online weekly + Σ weekly of the market's stores) × 2 × 1.5⌉. Across the 72 warehouse listings on sale, the total rises from 302 to 504.
- None would trigger today, and none needs capping below its opening quantity, so script 12 check 501 stays PASS.

Options:

- **D2a (recommended).** Revise the warehouse reorder points once, as an explicit and idempotent step: R0, a guarded `UPDATE … SET reorder_point = new WHERE reorder_point = old`, 72 `change_log` rows.
  - The opening inventory loader then reports "reorder point drift (allowed)".
  - Validation keeps every revised reorder point below the listing's opening quantity.
- **D2b.** Keep the stored values and use echelon numbers only inside the generator. Simpler, but then the stored reorder point is no longer the real trigger, which misleads reporting.

**Demand scale (decision D8).** S and the reorder points come from the modelled demand (155 units a week for the pilot catalogue). The pilot's 100 customers buy about 6% of that. Natural triggers are therefore:

- stores: after nearly every sale (display minimums);
- warehouses: almost never in a short window.

Realistic stock KPIs (days of cover, turns, fill rate) need the order volume and `[inventory.category_demand]` to be calibrated together. That is already an open item in the status document.

## 7. Implementation plan

### 7.1 Scope of the replenishment pilot (decision D1)

The window must start after the last pilot event (2024-11-15 08:44:22 UTC) and end before the next order window, because all writes stay in business-time order. Recommended: **a standalone pilot, Fri 2024-11-15 to Fri 2024-12-06, with no new orders**, weighted on purpose like the transaction pilot.

| Scenario | Count (planned) | Path exercised |
|---|---|---|
| Natural store transfers (first review Fri 2024-11-15 20:00 local) | the listings at or below reorder point at generation time (8 lines, 10 units today) | Trigger, T1, T2, multi-line document (DK-STO-01 has 3 lines) |
| Warehouse purchase order, one per market (pilot scenario, flagged `forced_reorder`) | 4 POs | R1 receipt, case pack, lead time, capacity |
| Split delivery | 1 of the 4 POs in two notes | Two receipts for one PO line |
| Reorder point revision R0 (if D2a) | 72 warehouse listings | Guarded UPDATE, drift handling |
| In transit at the end of the window | 0 | Conservation closes |

Alternatives:

- **natural only:** about 8 transfers, 0 receipts, so the receipt path stays untested;
- **combined orders + replenishment:** realistic, but needs the order volume decision first.

Consequence of a standalone window: the business timeline has no sales between 2024-11-15 and 2024-12-06. This is acceptable for a pilot; the full-volume run should use one combined activity simulation (step 7.6).

### 7.2 Change needed in the existing transaction loader

`transactions/load.py` (`build_progress`):

- Today it blocks when movements other than opening balances and sales exist.
- It also compares all 320 balances with "opening + pilot steps".

Proposed change:

- Accept `purchase_receipt`, `transfer_out` and `transfer_in` dated **after** the plan's last event, and add them to the expected stock.
- Keep blocking anything dated inside the pilot window.

`tests/python/test_transactions_db.py` "other stock movements block the run" inserts a receipt dated 2024-11-01 06:00 UTC, so it still blocks. A new test proves that a receipt dated after 2024-11-15 08:44:22 UTC is accepted. Without this change, `run_transactions.py --plan` exits 2 after the first receipt or transfer, and the pilot's "rerun writes nothing" guarantee can no longer be shown.

### 7.3 New and changed Python modules

| Module | Purpose |
|---|---|
| `nordiccart/stock/ledger.py` (new, shared) | One stock ledger for all movement types: on hand, reserved and in transit per (location, sku); replay of any event list; snapshot; conservation totals. `transactions.model.StockLedger` delegates to it, so orders and replenishment simulate the same stock. |
| `nordiccart/replenishment/config.py` | Reads `[replenishment]`: window, review schedule, receiving and dispatch hours, transit days, supplier lead-time range, case packs per category, order-up-to cover, echelon on/off, allocation priority, pre-launch days, pilot scenarios. Pilot profile only, like `[transactions]`. |
| `nordiccart/replenishment/policy.py` | Inventory position, trigger, order-up-to, echelon demand and reorder points, case-pack rounding, allocation when the warehouse is short. Pure functions, Decimal only. |
| `nordiccart/replenishment/model.py` | Planned purchase orders, delivery notes and transfer documents; events R0 (reorder point revision), R1 (receipt), T1 (dispatch), T2 (arrival); plan fingerprint; summary. |
| `nordiccart/replenishment/generate.py` | Business-time simulation: starts from opening stock + replay of the pilot plan (fingerprint `fabcd571…`), runs the reviews, issues documents, schedules dispatches, arrivals and receipts. Seeded streams per document, as in the other generators. |
| `nordiccart/replenishment/validate.py` | The rules in 7.4, including an independent stock replay. |
| `nordiccart/replenishment/load.py` | Read-only plan, `--apply`, verification. Business-key matching per movement; done steps must be a prefix in business-time order; stock = opening + pilot + done steps; session advisory lock; locked inserts in lock order; 23505 on a document index counts as done; retries only on 40001, 40P01, 55P03; stops on any unplanned result. |
| `nordiccart/replenishment/cli.py`, `__init__.py`, `__main__.py`, `python/run_replenishment.py` | Dry run / `--plan` / `--apply` with typed confirmation, logs, CSV preview (`output/replenishment/pilot/`), exit codes as in the other tools. |
| `.vscode/launch.json` | 3 entries: Replenishment Dry Run, Check, Load. |
| `transactions/load.py` | The change in 7.2. |
| `python/README.md` | Replace the `nordiccart/generator/` placeholder with the real package names. |

Later, for the full-volume run: `nordiccart/activity/` combines order windows and replenishment in one event stream and one loader, with the pilots as a verified prefix.

### 7.4 Validation rules (generator side, before any write)

| Group | Rules |
|---|---|
| Plan | Counts per scenario as configured; unique document numbers in the agreed format; all events after 2024-11-15 08:44:22 UTC, within the window, not in the future; numbering in business-time order; fingerprint reproducible |
| References | Every sku and location exists and is listed (`inventory_balances` row); receipts at warehouses; transfers from the market warehouse to a store of the same market; destination in the reference = arrival location |
| Calendar | Locations active and open on the movement day; products launched (or within the pre-launch window) and not discontinued; receipts on business days in receiving hours; dispatches in warehouse hours; arrivals Mon to Sat before or during store hours |
| Documents | `external_reference` 1 to 64 characters, unique per (type, location, product); one arrival per dispatch with the same quantity; arrival after dispatch within the transit limit; split-delivery notes add up to the PO line |
| Policy | Every store transfer has a review where IP ≤ reorder point (or a launch); quantity = S − IP after allocation; no second replenishment while one is open (IP includes in transit and on order); `forced_reorder` only as configured; revised reorder points < opening quantity and ≥ 0 |
| Quantities | All > 0; receipts multiple of the case pack; transfers ≤ simulated available at the source at dispatch (on hand − reserved); warehouse never drawn below its protection level |
| Stock replay (independent) | From opening + pilot + plan: no negative on hand at any business time; reserved ≤ on hand; in transit ≥ 0; capacity respected; conservation identity holds after every event; nothing in transit at the end unless configured |

### 7.5 Automated tests

| File | Kind | Content |
|---|---|---|
| `tests/python/test_replenishment.py` (new) | Unit, no database | Policy maths (IP, S, echelon, case packs, allocation); launch-aware trigger (CAM-000007 and SMH-000011 not ordered before their window); same seed → recorded fingerprint, other seed still validates; scenario coverage; one mutation per validator rule (each fault kind caught); conservation property over several seeds; settings refused when invalid; dry run without a database and without logging the password |
| `tests/python/test_replenishment_db.py` (new, opt-in, own databases) | Database behaviour | `--plan` read-only; `--apply` then rerun = nothing written (0 change_log rows); resume after k units of work; conflict (receipt with another quantity; arrival at another store); blocking (unknown movements, movements inside the pilot window); duplicate document handled as done; `transfer_out` of reserved units refused with 23514 and not retried; scripts 11 to 14 before and after; advisory lock; transaction loader still verifies the pilot after replenishment |
| `tests/python/test_transactions_db.py` (changed) | Database behaviour | Receipt after the pilot accepted; receipt inside the window still blocks |
| `tests/python/test_v007_concurrency.py` (extended) | Multi-session | `transfer_out` racing U2 on the same warehouse balance (one wins, the CHECK refuses the other); two sessions recording the same arrival (one 23505) |
| `tests/sql/test_v007_inventory.sql` (optional additions) | SQL behaviour | `transfer_out` of reserved units refused; and, documenting the gaps, receipt at a store, cross-border transfer and backdated movement **accepted** by V007 |
| `test_master_data_seed_db.py`, `test_opening_inventory_db.py` | Assertions | New PASS/INFO counts after the script revisions in section 8 |

## 8. Effect on the SQL verification scripts

| Script / check | After receipts and transfers | Proposed change |
|---|---|---|
| 01 to 10 (structure) | Unaffected | None |
| 11 r3 / 204 "movements other than opening_balance and sale" = 0 | **FAIL** | Revision 4: only the types not simulated yet (`stock_adjustment`, `write_off`, `return_restock`) must be 0; receipt and transfer counts become INFO |
| 12 r2 / 201 (same check) | **FAIL** | Revision 3: same as 11 |
| 12 / 301 to 303 ledger identity | PASS | None |
| 12 / 501 opening balance > current reorder point | PASS while revised reorder points stay below the opening quantity (all 72 do with echelon) | None; the validator guards it |
| 12 / 503 warehouse capacity (current stock) | Still meaningful | None |
| 12 / 601 to 604 INFO | Values change | None |
| 13 r1 / 504 "movements other than opening_balance and sale" = 0 | **FAIL** | Revision 2: allow `purchase_receipt`, `transfer_out`, `transfer_in` |
| 13 / 505 units on hand = 1441 | **FAIL** after any receipt; also lower while transfers are in transit | Revision 2: check "units sold = 19" and "opening − units sold − in transit + received = on hand"; the full identity moves to script 14 |
| 13 / 411 out-of-stock orders really short | PASS **only if nothing is dated before 2024-11-15 08:44:22 UTC** | None; no-backdating rule |
| 13 / 501 to 503, 1xx to 4xx | PASS | None |
| 13 / 606 INFO | Counts grow | None |
| **14 (new)** `14_verify_replenishment.sql` | — | See below |

Proposed checks for script 14:

- **Counts:** receipts (rows, units, documents) and transfers (dispatches, arrivals, in transit) by market.
- **Placement:** receipts only at warehouses; transfers warehouse → store in the same market; arrival location = destination in the reference; same reference on dispatch and arrival.
- **Calendar:** location open, product launched (or pre-launch window), not discontinued; business days and hours; nothing in the future or before the pilot cut-off.
- **Documents:** reference formats; split deliveries; arrivals within the transit limit.
- **Policy:** no store transfer while the store's available stock at dispatch was above its reorder point (except flagged scenarios); reorder points < opening quantity.
- **Ledger:**
  - identity per balance;
  - running balance by `occurred_at` never negative (window function, so backdating is caught);
  - system conservation: Σ on hand + in transit = opening + receipts − sales;
  - capacity.
- **INFO:** units received and transferred per market, listings at or below their reorder point now, days of cover per location type.

## 9. Database limitations and optional V008 hardening (not proposed for now)

| Limitation | Effect | Possible V008 item |
|---|---|---|
| `transfer_out` has no destination | Arrival can be recorded at any other location | `destination_location_id` on `transfer_out`, or a transfer header table, with the guard matching it on arrival |
| In transit has no closure | Units can stay in transit forever; loss cannot be written off | Transfer status, or allow `write_off` against an open transfer |
| No location-type or market rules for movements | Receipts at stores and cross-border transfers are accepted | Guard: receipts at warehouses; transfers within one country |
| `occurred_at` not ordered | Backdated outbound movements can make historical stock negative | Guard: `occurred_at` not before the latest movement of the balance (or a running-balance check) |
| Capacity not enforced | Over-capacity receipts accepted | Deferred check per warehouse |
| No purchase orders or suppliers | On-order stock and supplier lead time not visible to SQL or Power BI | `purchase_orders` and `purchase_order_lines` (needs a DBML revision) |
| Reorder point has no history | Reports see only the current value (`change_log` holds changes) | `valid_from` history table, if reporting needs it |
| No unit-of-work functions for U7 | Each writer must take locks correctly | `fn_receive_purchase`, `fn_dispatch_transfer`, `fn_receive_transfer` in the design lock order |
| Balance guard trusts trigger depth (existing V008 note) | A write from another trigger passes the quantity guard | Already planned: privileges and SECURITY DEFINER |

All of these can be handled by the application, validator and script 14 for a single-writer simulation. They matter more once several writers exist (FastAPI, hourly scheduling).

## 10. Implementation steps

| Step | Deliverable | Gate |
|---|---|---|
| 0 | Decisions D1 to D11 (section 12) | Owner approval |
| 1 | `stock/ledger.py`; `transactions/load.py` change (7.2) with tests | Unit and database tests pass; pilot still verifies |
| 2 | `[replenishment]` settings, `policy.py`, `model.py`, `generate.py`, `validate.py`, CSV preview, dry run | Validation 0 issues; fingerprint recorded |
| 3 | `load.py`, `cli.py`, runner, 3 VS Code entries | Read-only `--plan` works against a copy |
| 4 | Script 14; revisions 11 r4, 12 r3, 13 r2 | Pass before and after in a sandbox |
| 5 | Tests 7.5; sandbox run on PostgreSQL 18; mutation checks; test report | All pass; defects caught |
| 6 | Real database: backup, dry run, check, then load **after explicit approval**; scripts 11 to 14 | Report for review |
| 7 | `docs/phase3_replenishment_guide.md`; READMEs; status document | — |

No migration in steps 1 to 7. V008 items are a separate decision.

## 11. Unresolved risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Transaction loader blocks after the first replenishment | Certain without 7.2 | Pilot verification and rerun guarantee lost | Step 1 before any load |
| Backdated movement before 2024-11-15 08:44:22 UTC | Low (generator rule) | Script 13/411 FAIL; historical negative stock | Validator, loader prefix check, script 14 running balance |
| Demand and stock scale mismatch (6%) | Certain at pilot scale | Unrealistic KPIs; receipts never trigger naturally | Scenario-weighted pilot (D1); calibrate demand with the order volume (D8) |
| Warehouse reorder points ignore store pull | Certain until D2 | Warehouse stock-outs for online orders at scale | Echelon reorder points (D2a) |
| Pre-launch listings ordered too early | Certain with a naive trigger | Months of stock before launch | Launch-aware trigger + validator + test |
| Arrival at the wrong store or never | Low (single writer) | Units appear in the wrong place or vanish into transit | Destination in reference; script 14; optional V008 |
| `transfer_out` of reserved stock at load time | Low (simulated in advance) | 23514, run stops | Plan uses available stock; loader stops and does not retry; resume after fix |
| Timeline gap without sales (2024-11-15 to 2024-12-06) | Certain with a standalone pilot | Sales history has a hole | Accept for the pilot; combined activity simulation for the full run |
| On-order stock invisible to SQL and Power BI | Certain (no PO tables) | Lead time and open PO reports impossible from the database | PO number in the reference; plan CSV; optional DBML revision |
| `change_log` growth | Certain | 2 rows per movement (4 per transfer line, 72 for R0) | Expected; extraction unaffected |
| Concurrency once more writers exist | Not yet | Capacity race; lock-order mistakes | Single loader with advisory lock now; V008 functions later |

## 12. Decisions needed

| # | Decision | Recommendation |
|---|---|---|
| D1 | Pilot scope: natural only / scenario-weighted / combined with new orders | Scenario-weighted standalone pilot, 2024-11-15 to 2024-12-06 (7.1) |
| D2 | Warehouse reorder points: revise to echelon in the database (R0) or app-only | D2a: revise in the database (302 → 504 units), each below its opening quantity |
| D3 | Order-up-to levels | Stores: display minimum / 6 weeks; warehouses: 10 weeks of (echelon) demand |
| D4 | Transfer transit and supplier lead time | Transit ≤ 1 business day; supplier 10 to 14 days |
| D5 | Allocation when the warehouse is short | Protect the warehouse reorder point; stores at 0 first, then flagships |
| D6 | Document formats | `PO-SE-2024-000001/DN1`, `TR-SE-2024-000001-SE-STO-03` (same reference on dispatch and arrival) |
| D7 | Pre-launch receipts | Warehouse 3 to 7 days before launch; stores by launch day |
| D8 | Demand scale | Decide the order volume, then calibrate `[inventory.category_demand]` |
| D9 | Working days | Warehouses Mon to Fri; store deliveries Mon to Sat; receiving 07:00 to 15:00 local |
| D10 | V008 hardening | After the replenishment pilot, with the V008 privileges work |
| D11 | Start of replenishment | After 2024-11-15 08:44:22 UTC, no backdating. Rebuilding from the pre-pilot backup to simulate replenishment inside the pilot window would need a restore and is not recommended. |
