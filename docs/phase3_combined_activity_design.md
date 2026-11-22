# Phase 3, step 5: combined customer activity and replenishment simulation. Technical design

| Item | Value |
|---|---|
| Revision | **2** (2026-11-18), after design review. Revision 1, same day: architecture approved in principle with three issues to resolve. |
| Status | Revision 2 **approved**. Stage S1 implemented and reviewed; stage S2 (demand module) implemented, awaiting review. Decisions recorded after the S1 review (2026-11-18): C17 to C19 (section 13), applied in sections 4, 8.1 and 12. No migration written yet. |
| Based on | Migrations V001 to V007; DBML revision 3; packages `master_data`, `inventory`, `transactions`, `replenishment`, `stock`; `config/generator.toml`; the loaded real database (pilot transactions and replenishment pilot, verified 2026-11-18); full-profile facts and calibration figures from running the existing generators in memory (no database) |
| Audience | Project owner and design reviewer |

## 0. Revision 2: corrections

| # | Review point | Correction | Sections |
|---|---|---|---|
| R1 | Persistent state and recovery: revision 1 used `meta.activity_state` before V009 existed | All engine state is **derived from the database**. The two things the database cannot hold today get tables. **V009 is brought forward to before the historical catch-up:** purchase orders, plus a run ledger and checkpoints in `meta`. Each window has a crash-safe run protocol, and monthly recovery points are pg_dump files taken while no writer runs. File-based state is rejected: a database restore would leave it out of step. | 4, 7.2, 7.4, 12 |
| R2 | Demand calibration | 1. Zero-purchase share corrected: λ̄ = 2.4, k = 0.8 gives **33.0%** of full-year customers with no order (revision 1 said "about 45%", which would need k ≈ 0.42). 2. Full-year and partially registered customers separated through **seasonal exposure**. 3. Seasonal, Black Friday, weekday and hour factors **normalized**, so expected annual volume equals λ̄ × exposure exactly. 4. Volumes recomputed: **72,589** orders for 2024-12-03 to 2026-10-31 (revision 1: ~74,400) and **42,772** in the primary analytics year (revision 1: ~43,000). Black Friday week about 315 orders a day (revision 1: "about 400"). 5. Testable criteria: exact checks and seeded statistical checks. | 5, 6, 11 |
| R3 | Migration order: security before automated writes | **V008 = security** (roles, privileges, SECURITY DEFINER, timeouts), as V007's own comments already assume. **V009 = activity control and purchase orders**, with its grants based on V008's roles. Both run and are tested **before** the full-scale catch-up; hourly automation only afterwards, under its own login role. | 4, 12 |
| R4 | Additional requirements | 24 months kept, the latest 12 months as primary analytics; pilot records and V001 to V007 preserved (section 8.1); **exceptions to business-time write order listed explicitly**, including the exchange rate backfill (8.3); November 2025 is not comparable year over year with the pilot-only November 2024 (10.3); monthly verification and recovery points (7.4) | 7.4, 8, 10 |

## 1. Summary

The pilots proved each mechanism separately. This step builds **one chronological engine** that generates and loads all of the following together, in business-time order, at the scale of 500 products and 20,000 customers:

- customer orders, payments, reservations and fulfilment;
- store transfers and supplier receipts;
- reorder point reviews, price changes and daily exchange rates.

It continues after the loaded history without touching it. It runs in time windows (one UTC day for the historical catch-up, one hour later), and every window is crash-safe and reproducible.

1. **Volume from customers.** Each active customer orders at a personal rate drawn from a gamma distribution (mean λ̄ = 2.4 a year, shape k = 0.8), scaled by normalized seasonality and limited to the days the customer is registered. Expected volume: 72,589 orders from 2024-12-03 to 2026-10-31, of which 42,772 fall in the primary analytics year 2025-11-01 to 2026-10-31.
2. **One demand model** drives orders, the opening stock of the 480 new products and monthly reorder point reviews. This fixes the 2.2 to 3.4 times overstated demand in the current opening model.
3. **Continuation.** The loaded history (20 orders, replenishment pilot, last event 2024-12-02 12:46:04 UTC) stays unchanged. Full-scale activity starts on 2024-12-03; order numbers continue at 21, transfer and purchase order numbers at 000002.
4. **State lives in the database.** Business tables hold almost everything. V009 adds purchase orders, a run ledger and checkpoints. Monthly pg_dump recovery points capture data and engine state together.
5. **Security first:**
   - V008 (roles and privileges) before V009;
   - both before the full-scale catch-up, which runs under a least-privilege loader role;
   - hourly automation last, under its own scheduler role.
6. **24 months generated, 12 months for analytics.** Primary year 2025-11-01 to 2026-10-31. December 2024 to October 2025 is the growth year. November 2024 is the pilot month and is not a year-over-year baseline.

## 2. Starting point (facts)

### 2.1 Loaded real database (2026-11-18)

| Item | Value |
|---|---|
| Profile | pilot: 20 products, 100 customers, 16 locations, 320 listings |
| History | 20 orders (100 units of work, last event 2024-11-15 08:44:22 UTC); replenishment pilot (17 units of work, last event **2024-12-02 12:46:04 UTC**) |
| Stock | 1,467 units, 0 reserved, 0 in transit; 326 movements; warehouse reorder points 533; change_log 1,537; exchange_rates 0 rows |
| Numbering used | orders sequence 1 to 20; `TR-<market>-2024-000001-…` (4); `PO-<market>-2024-000001` (4) with 5 delivery notes |
| Restore point | `database/backups/nordiccart_oltp_20261118_032247_pre_replenishment.dump` (before the replenishment load) |
| Migrations | V001 to V007; connections as `postgres` (no application roles yet) |

### 2.2 Full profile (existing generators, in memory)

| Item | Value |
|---|---|
| Customers | 20,000 (SE 8,031, DK 4,471, NO 3,952, FI 3,546), all active |
| Registered (active) by | 2024-11-01: 12,384; 2024-12-03: 12,690; 2025-05-01: 14,119; 2025-11-01: 16,014; 2026-05-01: 17,918; 2026-10-31: 19,990 (about 290 to 350 a month in the calendar) |
| Products | 500; 381 on sale on 2024-11-01; 119 launched in the calendar; none discontinued |
| Prices | 2,000 rows: one price per product and market, never changing |
| Opening inventory (current model) | 8,000 listings, 6,096 opening balances, 28,109 units; modelled demand 2,928 units a week |
| Stores | 84% of stocked store listings at display minimums (3 units or fewer) |
| Generation time | master data 4.0 s, opening inventory 0.3 s |

## 3. What exists and what blocks the combined engine

| Area | In place | Blocks or limits |
|---|---|---|
| Units of work | U1 to U5, R0, R1, T1, T2, one transaction each, verified against the plan | Implemented as private functions inside two pilot loaders |
| Stock simulation | Shared `stock/ledger.py` (on hand, reserved, in transit) | No boundary state from the database |
| Idempotency | Business keys, prefix rule, database idempotency indexes | Full-plan prefix comparison does not scale to windows |
| Pilot verification | Two pilot `--plan` checks; scripts 13 and 14 | Both loaders block on any order, receipt or transfer outside their plan; scripts 13 and 14 count globally |
| Demand | Opening-model demand per product | No order-volume model; disagrees with inventory demand |
| Prices | `product_prices` effective-dated, no overlaps (EXCLUDE), `valid_to` closable (no update guard) | One price per product and market for the whole calendar |
| Exchange rates | `exchange_rates` insert-only, one row per pair per calendar day, EUR base | Empty |
| Security | Guards and CHECKs; trigger-depth quantity guard | No roles or privileges; everything runs as `postgres` (V008 pending) |

## 4. Database limitations and migration plan (V008, V009)

| # | Limitation | Effect | Resolution | Migration and stage |
|---|---|---|---|---|
| L1 | No purchase order tables | Open supplier orders are invisible; a window cannot rebuild them from the database | `purchase_orders`, `purchase_order_lines` with trigger `reorder_point`, `launch`, `seasonal` or `forced_reorder`, status and change capture; receipts linked by the existing reference `<PO>/DN<n>` | **V009, stage S4, before the catch-up** |
| L2 | No run ledger or checkpoint | No durable record of completed windows; no drift detection between windows | `meta.activity_runs` (one row per window: bounds, plan and config fingerprints, state hashes, counts, status, timings, recovery point) and `meta.activity_checkpoints` (state snapshot and hash at each verified window end) | **V009, S4** |
| L3 | No roles or privileges (V008 pending); balance triggers rely on trigger depth | Any writer could change quantities or history; hourly automation would run as superuser | Owner, writer, login, reader and extractor roles; column privileges; SECURITY DEFINER for balance maintenance and change capture; timeouts per role (section 4.1) | **V008, stage S3, before V009 and before any large-scale or automated write** |
| L4 | transfer_out stores no destination | In-transit destination only in the reference `TR-…-<destination>` | Keep the reference (already verified by script 14). Optional destination column later. | none (optional) |
| L5 | `countries.vat_rate` holds the current rate only | A VAT change inside the calendar cannot be represented | No standard-rate change in the four markets from 2024-11-01 to 2026-10-31. Rule: never change `vat_rate` in historical runs. | none |
| L6 | No promotions, shipments, returns or refunds | Peaks without discounts; no delivery or return data | Out of scope (DBML phases 2 to 4) | later |
| L7 | Orders need a customer | No anonymous walk-in sales | Keep ("loyalty-identified purchases") | none |
| L8 | The database does not reject future or backdated business timestamps | Wrong history possible | Window end = min(target, now); nothing written at or before the last loaded event, apart from the listed exceptions (8.3) | design rule |
| L9 | change_log about 14 rows per order | About 1.0 to 1.3 million rows for the history | Expected; purged by extraction later (Phase 4) | none |
| L10 | Reorder points carry no history and no business time (only the current value; change_log is purged in Phase 4) | After the first monthly R-cal update, the pilot's R0 revision can no longer be verified (replenishment loader and script 14 compare current values) | **Decision C18:** V009 provides a durable, timestamped reorder point revision history (one row per listing and revision: business time, old and new value, revision kind R0 / R-cal, document reference). The R0 pilot revision is backfilled into it like E3, and the loader and script 14 verify R0 from it. | **V009, S4** |

V001 to V007 are not modified (files and table definitions). V008 adds roles and grants and changes object ownership; V009 adds tables, triggers and grants. Verification scripts 01 to 10 must still pass after each migration (tested).

### 4.1 V008 security model (proposal)

| Role | Kind | Purpose and privileges |
|---|---|---|
| `nordiccart_owner` | NOLOGIN | Owns all objects in `public` and `meta` (ownership moved from `postgres`). Owns the SECURITY DEFINER functions. Migrations run as `postgres` with `SET ROLE nordiccart_owner`. |
| `nordiccart_writer` | NOLOGIN group | **SELECT** on all tables. **INSERT** on business tables and `meta.activity_*`. **UPDATE** only on lifecycle and status columns: order status and timestamps, payment status and outcome columns, reservation status and closed_at, `inventory_balances.reorder_point`, `product_prices.valid_to`, product discontinuation and active flags, customer contact and active columns, `meta.activity_runs` status columns. **No** UPDATE of balance quantities, **no** DELETE or TRUNCATE anywhere, **no** direct access to `change_log`, no CREATE. EXECUTE on the unit-of-work functions. |
| `nordiccart_loader` | LOGIN, member of writer | Manual, approved runs: pilot loaders, scale-up, historical catch-up |
| `nordiccart_scheduler` | LOGIN, member of writer | Automated hourly ticks only; own credentials; low connection limit; `statement_timeout` |
| `nordiccart_reader` / `nordiccart_analyst` | NOLOGIN / LOGIN | SELECT for verification scripts, Power BI and ad-hoc analysis |
| `nordiccart_extractor` | LOGIN | Phase 4: read change_log with snapshot checkpoints, manage `change_log_checkpoints`, purge consumed change_log rows |

Functions and settings:
- `fn_inventory_apply_movement`, `fn_inventory_apply_reservation` and `fn_capture_change` become **SECURITY DEFINER**, owned by `nordiccart_owner`, with `SET search_path = pg_catalog, public`. Writers then change quantities and change_log only through the triggers.
- Per-role settings: `lock_timeout = '10s'`, `idle_in_transaction_session_timeout = '60s'`.
- `config/.env` switches to `nordiccart_loader` after V008. A separate scheduler credential is created in stage S10.

## 5. Order volume across Sweden, Norway, Denmark and Finland

### 5.1 Customer purchase model

**Personal rate.** Each customer *c* gets a rate λ_c ~ Gamma(shape k, mean λ̄), drawn once from a seeded stream keyed by the customer number and kept for the whole calendar. Recommended λ̄ = 2.4 orders per year and k = 0.8.

**Seasonal exposure.** For any period P (a month, the primary year, the window), customer *c*'s exposure is

    E_c(P) = Σ over days d in P on or after the registration day of  S*(month(d)) / D

- D = 365 days in a Nov-to-Oct simulation year;
- S* = normalized monthly index (5.3).

A customer registered all year has E = 1. A customer who registers during the year has E < 1, weighted by the season of the remaining days.

**Orders.** Given λ_c, customer *c* places N_c,m ~ Poisson(λ_c × E_c(month m)) orders in month m. Over a period, the number of orders of a customer with exposure E follows a negative binomial distribution (gamma-Poisson) with mean λ̄E, and:

    P(N = 0) = (1 + λ̄E / k)^(−k)
    Var(N)   = λ̄E + (λ̄E)² / k

**Full year (E = 1), λ̄ = 2.4, k = 0.8 (corrected):**

| Orders in the year | 0 | 1 | 2 | 3 | 4 or more |
|---|---|---|---|---|---|
| Share of customers | **32.99%** | 19.79% | 13.36% | 9.35% | 24.51% |

Mean 2.4, variance 9.6, variance/mean 4.0. In seeded simulations of 16,014 customers, the top 20% of customers place about 61% to 62% of orders.

Revision 1 stated "about 45% with no order", which is wrong for k = 0.8: (1 + 2.4/0.8)^(−0.8) = 4^(−0.8) = 0.3299. A 45% zero share would need k ≈ 0.42 (0.4494).

**Partially registered customers.** A customer registered for part of the period has a lower expected count (λ̄E) and a higher chance of no order:

| Seasonal exposure E | 0.25 | 0.50 | 0.75 | 1.00 |
|---|---|---|---|---|
| Expected orders | 0.60 | 1.20 | 1.80 | 2.40 |
| P(no order) | 63.9% | 48.0% | 39.0% | 33.0% |

In the primary analytics year (2025-11-01 to 2026-10-31):

| Group | Customers | Seasonal customer-years | Expected zero-order share |
|---|---|---|---|
| Full-year customers (registered before 2025-11-01) | 16,014 | 16,014 | 33.0% |
| Partially registered (registered during the year) | 3,986 | 1,807 (mean E = 0.453) | 55.3% |
| All customers active at year end | 20,000 | 17,821 | 37.4% |

**Purchase frequency KPIs** (Power BI, Phase 4) must divide by exposure (customer-years), not by the number of customers. Otherwise customers who joined late look like weak buyers.

**Each order:**
- **Channel:** online or in store by customer propensity. In-store sign-ups buy in store 70% of the time; the online-sign-up propensity is solved so that the expected online share is 0.55 (existing `online_share`).
- **Day and hour:** from the channel's normalized day and hour weights (5.3).
- **Store:** home-city preference and flagship weight (existing rules).
- **Lines and quantities:** 1 / 2 / 3 lines at 0.70 / 0.25 / 0.05; items under 50 EUR two at a time 20%. Expected units per order are computed from the catalogue mix (target about 1.45) and stated in the calibration report.
- **Products:** category share × attractiveness (price elasticity, seeded popularity, launch ramp over 6 weeks, decline after 18 months), only products in stock at the fulfilment location. Demand for out-of-stock items is reported as lost demand in the plan summary.
- **Lifecycle rates:** online first-attempt failure 6% (store 2%); retry after failure 60% (store 50%), at most 3 (store 2) attempts; abandoned checkout leading to expiry 3% (online); customer cancellation 1% (online), 0.2% (store).

**Determinism (project rule: no floats for generated values).** Gamma draws use the Marsaglia-Tsang method and Poisson draws use inversion, both with Decimal `ln`, `exp` and `sqrt` and `random.Random.random()` / `randrange`. The calibration script above used floats only to check design figures.

### 5.2 Volumes (seasonal exposure of the full-profile customers)

| λ̄ | 2024-12-03 to 2026-10-31 | of which growth year (to 2025-10-31) | Primary year 2025-11-01 to 2026-10-31 | Orders a day, primary year |
|---|---|---|---|---|
| 2.0 | ~60,500 | ~24,800 | ~35,600 | ~98 |
| **2.4 (recommended)** | **72,589** | **29,817** | **42,772** | **~117** |
| 3.0 | ~90,700 | ~37,300 | ~53,500 | ~147 |
| 4.4 (current inventory assumption) | ~133,100 | ~54,700 | ~78,400 | ~215 |

These come from seasonal customer-years: 12,424 in the growth year and 17,821 in the primary year. Revision 1 used calendar-day exposure and gave ~74,400 and ~43,000. The share per market follows the customer mix (SE about 40%, DK 22%, NO 20%, FI 18%) and is checked in the calibration tests.

### 5.3 Normalized seasonality, Black Friday, weekday and hour factors

1. **Monthly index.** Raw values: Jan 0.95, Feb 0.80, Mar 0.85, Apr 0.85, May 0.90, Jun 0.90, Jul 0.80, Aug 0.95, Sep 0.95, Oct 1.00, Nov 1.45, Dec 1.60. Their *day-weighted* mean is Σ S_m d_m / 365 = 365.45 / 365 = 1.001233. **Normalized:** S*_m = S_m / 1.001233, so that Σ S*_m d_m = 365 exactly. Revision 1 assumed the raw values averaged 1.00; the result would have been 0.12% too many orders a year.
2. **Within a month (day weights).** A customer's monthly count is fixed by step 1. Its orders are spread over the month's **open days for the channel** with weights normalized inside the month:

       P(day d | month m, channel ch) = w_ch(d) / Σ over open days d' in m of w_ch(d')

   - Online weekday weights: Mon 1.10, Tue to Thu 1.00, Fri 0.95, Sat 0.85, Sun 1.10.
   - Store weekday weights: Mon 0.85, Tue 0.90, Wed 0.95, Thu 1.05, Fri 1.15, Sat 1.60; Sunday and the store holidays 25 and 26 December and 1 January closed, weight 0.
   - **Black Friday week** (the 7 days around Black Friday, 28 November 2025; 29 November 2024 falls in the pilot month) gets weight ×3 within November.

   Because the weights are normalized within each month, closed days and Black Friday move orders *between days* and never change the month's total. Store orders lost to a closed day move to the open days.
3. **Registration month.** For the month a customer registers, the exposure counts only the days from registration on, and those days' weights are normalized against the whole month, so partial months stay exact.
4. **Hours.** Hour weights per channel are normalized to sum 1 over the open hours of the day: online 07:00 to 23:00 with an evening peak 19:00 to 22:00; stores 10:00 to 19:00 with a peak 12:00 to 17:00.

Consequence: the **expected** annual volume equals λ̄ × Σ_c E_c(year) exactly; only the random draws differ. Expected November 2025: about 4,613 orders, about 315 a day in Black Friday week and about 105 on other November days. Revision 1's "about 400 on Black Friday" is withdrawn.

## 6. Recalibrating demand, warehouse inventory and reorder points

**Problem.** The opening model's chain demand (2,928 units a week) is 2.2 to 3.4 times the demand of the recommended volume: about 850 units a week at 12,690 customers and about 1,340 at 19,990, at 1.45 units per order. Opening stock would cover 2.2 to 3.4 times the intended weeks. Step 4 saw the same effect at pilot scale.

**One model, three uses.** The `demand` module computes *expected* units per (location, sku, week) analytically from section 5: customers' seasonal exposure, channel propensities, store choice, category shares, attractiveness and lifecycle. The same figures drive:

| Use | Rule |
|---|---|
| Order generation | Section 5 (random draws around the expectation) |
| Opening stock of the 480 new products (stage S7) | Weeks of cover (stores 6, warehouses 10) × expected weekly units in the opening week, with the existing minimum and noise rules. Products launched later start at 0 and get launch purchase orders. **The 20 pilot products keep their loaded opening balances** (flagged as overstocked in the calibration report). |
| Reorder points and order-up-to levels | Monthly review **R-cal** (generalizes R0) on the first working day of each month, for every listing. Stores: forecast demand over 1 week × safety 1.5, at least display minimum − 1. Warehouses: echelon demand over 2 weeks × 1.5, plus a pre-build for the season index of the lead-time window, below a capacity guard. It is a guarded `UPDATE reorder_point WHERE reorder_point = old`; order-up-to levels stay in the engine. |

The forced purchase orders of the replenishment pilot are off; all purchase orders are natural (`reorder_point`, `launch`, `seasonal`).

## 7. Combined chronological processing

### 7.1 Engine

A discrete-event simulation per window, with one priority queue ordered by (business time UTC, rank, entity key):

| Rank | Unit of work | Notes |
|---|---|---|
| 0 | X1 exchange rates for the day (00:00 UTC) | Section 9.1 |
| 1 | P1 price changes (00:00 local) | Close `valid_to` and insert the new row in one transaction |
| 2 | R-cal reorder point review (monthly) | Section 6 |
| 3 | R1 supplier receipts | Purchase orders from Monday warehouse reviews |
| 4 | T2 transfer arrivals | Same morning as dispatch |
| 5 | T1 transfer dispatches | From the morning store review |
| 6 | Order units of work: U1 place, U2 reserve or out-of-stock cancel, U3a/U3c payments, customer cancel, U4 expire, U5 fulfil | Each order's timeline (attempts, outcomes, cancellation or expiry, fulfilment time) is drawn at placement from a stream keyed by the order number. Stock outcomes are decided by the shared ledger at the event's time. |
| 7 | Reviews: store reviews 05:30 local; warehouse review Mondays 09:00 local | Produce T1 and purchase orders |

- Store reviews move from 20:00 (step 4) to **05:30**, so no store request is pending across a day boundary.
- Fulfilment: online the same afternoon (confirmed before 14:00) or the next morning; in store within minutes.
- Replenishment: the step 4 (s, S) policy with recalibrated s and S.

### 7.2 Windows and state (crash-safe, database-derived)

| Item | Rule |
|---|---|
| Window | Historical catch-up: one **UTC calendar day**. Live mode: one hour. |
| State at a window start | **Derived from the database:** orders and statuses, pending payment attempts, active reservations, balances, reorder points, prices, transfers in transit (unmatched transfer_out plus the destination in the reference), **open purchase order lines** (V009: ordered minus received through their `<PO>/DN<n>` receipts), last numbers used, the last exchange rate. Future events of open orders are rebuilt from order state and the order's seeded stream. |
| Checkpoint | At each verified window end, `meta.activity_checkpoints` stores the state snapshot (JSON) and its SHA-256. The next window recomputes the state from the database and must get the **same hash**; otherwise it stops (drift). |
| Composition rule | plan(day 1) + plan(day 2) = plan(days 1 to 2); tested |
| Identity | Plan fingerprint and config/engine fingerprint stored in `meta.activity_runs` |

### 7.3 Window run protocol

1. Take advisory lock `nordiccart.activity`. While it is held, the pilot loaders refuse `--apply`.
2. Read the last verified checkpoint. If a run row for the next window has status `running`, resume it (step 5).
3. Derive the state at the window start; compare its hash with the checkpoint. A mismatch blocks the run.
4. Generate the window plan (deterministic) and upsert its `meta.activity_runs` row (`running`, fingerprints, planned units) in its own transaction. A different fingerprint on an existing `running` row stops the run: code or config changed inside a window.
5. Run the window's remaining units of work, each in its own transaction, using the pilot loaders' prefix rule limited to the window.
6. Verify the window: counts equal to the plan, ledger identity for touched balances, conservation for the network, documents complete, no event after the window end.
7. **In one transaction:** insert the checkpoint (state at the window end, hash) and set the run row to `verified` (counts, timings).

| Failure | Recovery |
|---|---|
| Crash inside a unit of work | That transaction rolls back. The rerun regenerates the same window plan and the prefix rule skips the done units. |
| Crash between step 5 and 7 | The run row stays `running`. The rerun finds all units done, verifies, then writes the checkpoint. |
| Crash during step 7 | Rolled back as a whole; same as above |
| Verification FAIL | Stop. Owner decision: fix and resume, or restore the last monthly recovery point. |
| Database restored from a recovery point | Data, purchase orders, run ledger and checkpoints come back together (one pg_dump), so the engine resumes from the restored state with no outside file to reconcile |

**Why not files.** A JSON state file written next to the database cannot be committed atomically with the units of work. pg_dump recovery points would not contain it, so after a restore the file would describe a different state, and it can be deleted or corrupted on its own. Files are acceptable only in the sandbox before V009 exists (stage S6 can run against a V009 sandbox instead). Revision 1's `meta.activity_state` is replaced by the V009 design above, and V009 moves before the catch-up.

### 7.4 Monthly verification and recovery points (full-scale loading)

The historical catch-up runs in **23 monthly batches**, December 2024 to October 2026 (December 2024 from the 3rd). Each batch:

1. runs its day windows (7.3);
2. after the month's last window, runs the **monthly verification**: scripts 11 to 16 with the month as the date range for 15. Any FAIL stops the catch-up;
3. takes a **recovery point**:
   - pg_dump custom format while the activity lock is held, so no writer runs;
   - file `database/backups/nordiccart_oltp_<yyyymm>_activity_recovery_<timestamp>.dump`;
   - SHA-256 recorded in the month's last run row and in a short monthly verification note;
4. continues with the next month only after steps 2 and 3 succeed.

Retention: all monthly recovery points until the catch-up is complete, then quarter ends and the final one (owner decision). Disk estimate: compressed dumps grow to about 150 to 250 MB, about 2 to 3 GB in total.

## 8. Continuation, preserved records and write-order exceptions

### 8.1 Preserved

- **Pilot records:** 20 orders, 26 lines, 21 payment attempts, 23 reservations, 17 sales; the replenishment pilot's 21 movements and 69 reorder point revisions. **Never regenerated, updated or deleted.** They are verified before the first combined window and after every month by the pilot checks and scripts 13 and 14.
- **Migrations V001 to V007:** files unchanged; table definitions unchanged; V008 changes object ownership and grants only.
- **Numbering:** orders continue the global sequence at **21** in `placed_at` order (`CC-YYYY-NNNNNNNN`, local year); `TR` and `PO` numbers continue per market and year at **000002** in 2024 and restart at 000001 in 2025 and 2026. Next numbers are always read from the database.
- **Pilot verification** (stage S1, before any combined write):
  - both pilot loaders accept *any* activity dated after their own last event and outside their own documents, and add it to their expected stock (on hand from movements, reserved from active reservations);
  - script 13 is limited to the 20 pilot order numbers, and script 14 to the pilot's own document numbers (4 PO and 4 TR numbers) and the stock up to its last movement, 2024-12-02 12:46:04 UTC (decision C17: replaces "dates up to 2024-12-06", because the combined activity starts on 2024-12-03; implemented in S1, approved);
  - their global stock checks move to script 15.

### 8.2 Start

The first combined window is the UTC day **2024-12-03**, after the last loaded event (2024-12-02 12:46:04 UTC). The engine refuses to start if the database holds any business timestamp at or after the window start. November 2024 stays the **pilot month**.

### 8.3 Exceptions to business-time write order (explicit)

Business events are written in business-time order: no business event is written at or before an already loaded later event. Three exceptions are allowed; they are explicit, limited, and checked:

| # | Exception | Scope | Why it is safe | Check |
|---|---|---|---|---|
| **E1** | **Historical exchange rate backfill** | `exchange_rates` for rate_date 2024-11-01 to 2024-12-02: 32 days × 3 pairs = **96 rows**, loaded in stage S5 after events of those dates already exist. From 2024-12-03 the engine writes each day's rates at the start of its window (rank 0), in order. | Exchange rates are reference data keyed by date, not business events. They change no order, payment or stock row; the table is insert-only. `created_at` shows the load time. | Script 16: one row per pair per calendar day, no gaps, from 2024-11-01; every fulfilled order has a rate for its fulfilment date |
| E2 | Opening balances of the 480 new products | Listings and opening balances dated 2024-11-01 for products that have **no** movement or reservation yet (stage S7) | Opening balances define the stock at 2024-11-01. A listing without activity has no history to break. Existing listings are compared, never changed. | Script 12 (full-profile revision): one opening balance per stocked listing, none for listings with earlier activity; script 13/14 unchanged |
| E3 | Pilot purchase order documents (if decision C15 is yes) | The 4 pilot purchase orders and their lines, ordered 2024-11-18, inserted into the V009 tables in stage S4 | Documents describing receipts already recorded; no stock change | Every purchase_receipt maps to an existing purchase order line, and received equals ordered |

Any other write before the latest loaded business timestamp is refused by the engine (blocking) and caught by scripts 13 to 16.

## 9. Daily exchange rates and hourly generation

### 9.1 Exchange rates (unit of work X1)

| Item | Design |
|---|---|
| Rows | EUR->SEK, EUR->NOK, EUR->DKK, one per calendar day: 730 days from 2024-11-01 to 2026-10-31 = **2,190 rows** (96 by backfill E1, 2,094 by the engine). EUR->EUR is forbidden by V003 and not needed. |
| Model | Synthetic, `source = 'synthetic_ecb'`. Mean-reverting log-rate walk around the 2025 averages in config (SEK 11.0671, NOK 11.7182, DKK 7.4634). Daily volatility SEK about 0.4%, NOK about 0.5%; DKK kept within ±0.3% (ERM II). Decimal arithmetic, 4 decimals, seeded per currency. The walk resumes from the last stored rate (state from the database). |
| Weekends and holidays | The last working day's rate is repeated |
| Verification | Script 16 (bands, no gaps, peg, coverage of every fulfilment date) |

### 9.2 Hourly generation (prepared; built in stage S10)

| Topic | Design |
|---|---|
| Modes | `--until <timestamp>`: catch-up, day windows. `--tick`: one hour `[last verified end, floor(now, hour))`, covering missed hours. Same engine, protocol and tables. |
| After 2026-10-31 | Catch-up from 2026-11-01 to the present, then ticks. New customer registrations become events (unit of work C1); launches and price changes come from a rolling catalogue plan. |
| Prerequisites | **V008 deployed and tested** (scheduler runs as `nordiccart_scheduler`, never `postgres`); V009; monitoring of exit codes and run rows; owner approval |
| Safety | Business time never after `now`; one activity lock (overlapping ticks exit 3); daylight-saving hours tested; an hour already verified writes nothing |
| Scheduling | Windows Task Scheduler, hourly, `--tick --yes` with the scheduler credential; alert on any non-zero exit |

## 10. Scale and the 12-month versus 24-month calendar

### 10.1 Scale-up to 500 products and 20,000 customers (stage S7)

1. **Master data:** `run_master_data_seed.py --profile full --apply`, run as `nordiccart_loader`. It adds 480 products, 1,920 prices and 19,900 customers; pilot rows are compared and stay unchanged (tested behaviour).
2. **Opening inventory extension** (new loader mode): 7,680 new listings; opening balances for new products on sale on 2024-11-01 from the recalibrated model (E2); zero listings for later launches.
3. **Verification:** scripts 11 and 12 with full-profile parameters; pilot checks still green.

### 10.2 Volumes and run time (λ̄ = 2.4; to be measured in the rehearsal)

| Item | 2024-12-03 to 2026-10-31 |
|---|---|
| Orders | 72,589 expected (~5.1 units of work each) -> ~370,000 units of work |
| Store transfers | ~6,000 documents (~12,000 units of work) |
| Purchase orders | ~400 (~450 delivery notes) |
| R-cal reviews | 23 monthly reviews of up to 8,000 listings |
| Exchange rates | 2,094 rows (engine) + 96 (E1) |
| change_log | ~1.0 to 1.3 million rows |
| Run time | ~1 to 2 hours of units of work at the pilots' 5 to 10 ms each, plus monthly verification and dumps |

### 10.3 Reconciling the 12-month objective with the 24-month calendar

The repository does not record the 12-month objective; it comes from the original brief. `config/generator.toml` fixes 24 months, 2024-11-01 to 2026-10-31, and the loaded history is dated in November 2024.

**Decision (kept): generate all 24 months; the primary analytics period is the latest 12 complete months, 2025-11-01 to 2026-10-31.**

| Period | Role | Scale |
|---|---|---|
| 2024-11-01 to 2024-12-02 | **Pilot month:** 20 orders, 100 customers, 20 products, first replenishment | Pilot only |
| 2024-12-03 to 2025-10-31 | **Growth year:** full engine and catalogue, 12,690 -> 16,014 active customers | ~29,800 orders |
| 2025-11-01 to 2026-10-31 | **Primary analytics year:** both seasonal peaks, 16,014 -> 19,990 active customers | ~42,800 orders |

**Year-over-year limits:**
- **November 2025 cannot be compared with November 2024.** November 2024 holds only the 20 pilot orders of 100 customers on 20 products, and its Black Friday (29 November 2024) was never simulated at scale. A November YoY figure would show a meaningless growth of many thousand per cent. Reports show it as *not comparable*.
- **December 2025 vs December 2024:** December 2024 is at full scale only from the 3rd. Compare 3 to 31 December like for like, or per day.
- **January to October 2026 vs 2025:** representative. Growth includes the customer base growing by about a quarter, so per-customer measures (orders per customer-year) are the fair comparison.
- **The first full-year comparison** is the primary year against the growth year with the same exclusions: November not comparable, December like for like.

Options B (generate only the last 12 months) and C (rebuild a 12-month database) from revision 1 stay rejected: B leaves an 11-month hole; C discards the pilot history.

## 11. Testing requirements

### 11.1 Calibration criteria (testable)

**Exact (deterministic, no random draws), tolerance 1e-9:**

| Criterion | Target |
|---|---|
| Normalized monthly index | Σ S*_m × d_m = 365 for each Nov-to-Oct year |
| Day weights | Inside every month, channel day probabilities sum to 1; closed days 0; Black Friday week weight 3 relative |
| Hour weights | Sum to 1 over open hours per channel and day |
| Expected volume | Σ_c λ̄ × E_c(year) = λ̄ × seasonal customer-years (17,821 primary, 12,424 growth with the full profile) |
| Expected channel share | 0.55 online |
| Expected units per order | Computed from the line mix and catalogue; reported (target 1.45 ± 0.05 checked as a design figure) |
| Negative binomial checks | P(0) = 0.3299 and variance/mean = 4.0 for λ̄ = 2.4, k = 0.8, E = 1; partial-exposure P(0) table of 5.1 |

**Statistical (simulated, seeds 1 to 5, full profile):**

| Criterion | Tolerance |
|---|---|
| Orders in the primary year | 42,772 ± 3% (expected spread about ±1% per seed) |
| Orders in the growth year | 29,817 ± 3% |
| Mean orders per full-year customer | 2.40 ± 0.08 |
| Zero-order share, full-year customers | 33.0% ± 1.5 percentage points |
| Zero-order share, partially registered | 55.3% ± 3 points |
| Share with exactly 1 order (full-year) | 19.8% ± 1.5 points |
| Variance/mean (full-year customers) | 4.0 ± 0.5 |
| Top 20% of full-year customers' share of orders | 0.58 to 0.65 |
| Monthly share of annual orders | S*_m d_m / 365 ± 10% relative (months with ≥ 2,000 orders) |
| Weekday share per channel | normalized weight ± 5% relative |
| Online share | 55% ± 1.5 points |
| Market share of orders | exposure-weighted customer share ± 1.5 points |
| Lifecycle rates (failure, retry, expiry, cancellation) | configured rate ± 20% relative |
| Inventory | warehouse weeks of cover 8 to 12 after the first 3 months; store order-line fill rate ≥ 97%; lost demand ≤ 3%; chain stock turns 4 to 6 a year |

### 11.2 Other tests

| Level | Tests |
|---|---|
| Engine properties (many seeds) | Conservation after every event; no negative stock; reserved ≤ on hand; allowed final statuses; business-time order; **window composition**; determinism; daylight-saving days; numbering at year ends |
| Crash and recovery (sandbox) | Kill the process at random points (inside a unit, between units, during verification, during the checkpoint transaction): the rerun ends identical to an uninterrupted run (same fingerprints, same checkpoint hashes). Restore a monthly recovery point into a second sandbox database and resume. Detect state drift (manual row change -> hash mismatch -> blocking). |
| V008 security | New `tests/sql/test_v008_roles.sql`: the full role × object × privilege matrix, with denials checked by SQLSTATE 42501. All Python database suites and loaders re-run as `nordiccart_loader`. Verification scripts as `nordiccart_analyst`. SECURITY DEFINER functions maintain balances and change_log without direct privileges. Writers cannot update quantities, delete, truncate, write change_log or run DDL. Scripts 01 to 10 still pass. |
| V009 | Structure verification script; behaviour tests (purchase order statuses, receipts within ordered quantity, run and checkpoint constraints); pilot purchase order backfill (E3) checks |
| Pilot regression | After every combined month: both pilot `--plan` checks show 0 to write; scripts 13 and 14 (scoped) pass |
| SQL verification | Script 15 (combined activity per date range), script 16 (exchange rates), scripts 11 and 12 full-profile revisions |
| Concurrency | Activity lock against pilot `--apply`; overlapping ticks; expiry sweeper against payments during ticks |
| Performance | One month at full scale, then the full 24-month rehearsal: units of work per second, change_log growth, dump times and sizes, query plans of the state reads |
| Mutation checks | Planted defects in composition, numbering, state derivation, checkpoint hashing, lock and scoped acceptance must be caught |

## 12. Implementation stages (revised)

Every stage ends with tests and a sandbox run. Stages marked **DB** write to the real database and need a fresh backup, a readiness report and explicit authorization.

| Stage | Content | Real database |
|---|---|---|
| S1 | Pilot loaders accept later activity (scoped); scripts 13 and 14 scoped revisions; script 15 skeleton; shared unit-of-work module extracted (pilot fingerprints and tests unchanged) | none |
| S2 | `demand` module, normalization, calibration report and the calibration tests of 11.1 (decisions C1 to C3, C11) | none |
| **S3** | **V008 security:** migration, `test_v008_roles.sql`, all suites as `nordiccart_loader`, scripts 01 to 14 green; rehearsal on a restored copy of the real database | **DB:** V008; `config/.env` switches to `nordiccart_loader` |
| **S4** | **V009 activity control and purchase orders:** migration with grants on V008 roles, DBML revision 4, structure and behaviour tests, pilot purchase order backfill (E3); **reorder point revision history** with the R0 pilot backfill, and the replenishment loader and script 14 verifying R0 from it (C18, L10) | **DB:** V009 (+ E3, R0 history) |
| S5 | Exchange rates: generator, X1 loader, script 16; backfill E1 | **DB:** 96 rows (E1) |
| S6 | Window engine and run protocol at **pilot scale** in a V009 sandbox (December 2024): composition, crash and recovery, drift, monthly verification and recovery points | none (sandbox) |
| S7 | Scale-up: master data full profile; opening inventory extension (E2); scripts 11 and 12 full-profile revisions (C19: they count globally until then and fail once products are added); R-cal and price changes | **DB:** master data and opening stock |
| S8 | Full 24-month **rehearsal** in the sandbox from a restored copy of the real database after S7: 23 monthly batches with verification, recovery points, timings and a restore drill | none (sandbox) |
| S9 | **Real historical catch-up** 2024-12-03 to 2026-10-31: 23 monthly batches, each verified and followed by a recovery point; stop on any FAIL | **DB:** ~390,000 units of work |
| S10 | Live mode: catch-up from 2026-11-01 to the present (loader role), then **hourly automation** as `nordiccart_scheduler`. Only after S3 is deployed and tested and monitoring is in place. | **DB:** ongoing |
| S11 | Phase 4: extraction (extractor role), Power BI (primary analytics year, YoY rules of 10.3) | **DB:** extractor reads and purges |

Order rationale:
- Security (S3) comes before any large or automated write.
- V009 (S4) comes after V008 so its grants use the new roles, and before S6 to S9 so all engine state is durable and included in recovery points.
- Hourly automation (S10) comes last.

## 13. Decisions needed

| # | Decision | Recommendation |
|---|---|---|
| C1 | Order volume λ̄, heterogeneity k, market factors | λ̄ 2.4, k 0.8 (33% zero buyers a full year), all markets 1.0 |
| C2 | Demand calibration | One demand model for orders, opening stock and reorder points |
| C3 | Opening stock of the 480 new products | From the recalibrated model; pilot listings unchanged |
| C4 | Exception E2 | Accept, limited to listings without activity |
| C5 | History period | Continue after 2024-12-02 12:46:04 UTC; no rebuild |
| C6 | 12 versus 24 months | 24 months generated; primary analytics year 2025-11-01 to 2026-10-31; YoY rules of 10.3 |
| C7 | Exchange rates and exception E1 | Synthetic, all calendar days; E1 backfill of 96 rows |
| C8 | Price evolution | Modest lifecycle markdowns (−3% to −8% after 6 and 12 months), or constant prices |
| C9 | Out-of-stock behaviour | Availability-aware customers; lost demand in the plan summary |
| C10 | Store review time | 05:30 local |
| C11 | Lifecycle rates | Table in 5.1 |
| C12 | Migration order | V008 security (S3), then V009 activity control and purchase orders (S4), both before the catch-up |
| C13 | V008 ownership model | Move object ownership to `nordiccart_owner` with SECURITY DEFINER functions owned by it. The alternative (keep `postgres` as owner) is not recommended: superuser-owned SECURITY DEFINER functions. |
| C14 | Catch-up batching and retention | 23 monthly batches; verification and a recovery point after each; keep all points until completion |
| C15 | Exception E3 (pilot purchase order backfill) | Yes, so every receipt maps to a purchase order |
| C16 | Customer churn | None for now |
| C17 | Scope of script 14 (decided after the S1 review, 2026-11-18) | **Approved:** the pilot's own document numbers and its last movement (section 8.1) |
| C18 | Verifying R0 after monthly R-cal updates (decided after the S1 review) | **Required:** V009 provides a durable, timestamped reorder point revision history; R0 is backfilled (L10, stage S4) |
| C19 | Scripts 11 and 12 with the full profile (decided after the S1 review) | **Kept in S7:** full-profile revisions of both scripts |

## 14. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| V008 breaks existing loaders (privileges, SECURITY DEFINER) | Loads fail | All suites and loaders re-run as `nordiccart_loader` in the sandbox; rehearsal on a restored copy; backup before deployment |
| State drift between windows | Wrong continuation | Database-derived state plus checkpoint hash comparison; blocking on mismatch |
| Pilot checks turn red after combined activity | Loss of regression evidence | S1 first; checked every month |
| Calibration off | Unrealistic KPIs | Exact and statistical criteria (11.1) before S7 |
| Run time and change_log growth | Long loads, large database | Monthly batches, rehearsal timings, extraction purge later |
| Exceptions E1 to E3 misused | Broken history | Limited scopes, scripts 12 and 14 to 16, engine refuses any other backdated write |
| YoY misread (November 2024) | Wrong conclusions | YoY rules of 10.3 in the report design; November 2024 marked not comparable |
| Hourly automation with excessive rights | Security exposure | Scheduler role, least privilege, V008 tested before S10 |

## 15. Out of scope

Implementation of any stage; promotions, shipments, returns and refunds; FastAPI; the scheduler setup itself; Power BI models; writing the V008 and V009 migrations (designed only).
