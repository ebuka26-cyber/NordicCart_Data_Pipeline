# Phase 3, step 5, stage S2: demand model and calibration (report)

Date: 2026-11-18. Design: `docs/phase3_combined_activity_design.md`, revision 2 (approved): sections 5 (order volume), 6 (one demand model), 11.1 (calibration criteria), decisions C1 to C3 and C11.
Scope: S2 only. No engine, no migration, no database access (the real database was not contacted). The full profile (20,000 customers, 500 products) was built in memory only.

## 1. Result

| Item | Result |
|---|---|
| `nordiccart/demand` module | Implemented: gamma-Poisson rates, seasonal exposure with registration dates, normalized calendar, expected demand by market, channel, location, product and period, seeded draws, inventory uses |
| Exact criteria (design 11.1) | All met. Expected volume = λ̄ × seasonal customer-years exactly. Full-profile volumes within 0.15% of the design figures (section 4) |
| Statistical criteria, demand seeds 1 to 5 | **104 of 104 checks passed for every seed** (and for the production seed 20261117). Worst result: 0.83 of its tolerance |
| New automated tests | 44: `test_demand.py` (33 deterministic), `test_demand_calibration.py` (11, full profile, seeds 1 to 5) |
| Full test suite | Without database: **251 passed, 97 skipped** (database tests). With the disposable PostgreSQL 18.4 sandbox: **348 passed, 0 failed, 0 skipped** |
| Mutation checks (planted defects) | 9 of 9 caught |
| Pilot fingerprints | Unchanged: transactions `fabcd571…7c466264`, replenishment `858bbfd0…05531236`; the full S1 baseline (fingerprints, order numbers, documents, listings) is byte-identical |
| Pilot code and configuration | Unchanged: no existing configuration value, no pilot generator or loader logic. New `[demand]` section only |
| Decisions recorded | C17 (script 14 scope approved), C18 (V009 reorder point revision history), C19 (scripts 11 and 12 full-profile revisions in S7): section 10 |

## 2. Files

| File | Status | Content |
|---|---|---|
| `python/nordiccart/demand/__init__.py` | new | Package overview |
| `python/nordiccart/demand/config.py` | new | `[demand]` settings. It also reads the pilots' shared assumptions unchanged: `[transactions]` online_share, lines_per_order, two_units_share and hours; `[inventory]` category demand, elasticity, popularity, format weights, cover, lead times, safety |
| `python/nordiccart/demand/numeric.py` | new | Decimal draws without floats: uniform, polar normal, Marsaglia-Tsang gamma (boost for k < 1), inversion Poisson; negative binomial pmf; integer-weight picks |
| `python/nordiccart/demand/calendar.py` | new | Normalized monthly index per simulation year; day probabilities per channel (weekdays, store closed days, Black Week); daily exposure units with suffix sums; hour probabilities |
| `python/nordiccart/demand/model.py` | new | Customers (registration, channel propensity, store choice) and products (the opening model's weights × life cycle). Expected orders and units by market, channel, location, product and period; outcome probabilities |
| `python/nordiccart/demand/simulate.py` | new | Seeded draws: rate, monthly counts, then per order the channel, day, hour, location, lines, quantities and outcome. Window-composable. The order source of the S6 engine |
| `python/nordiccart/demand/calibration.py` | new | Exact figures, simulated metrics and the 11.1 statistical checks |
| `python/nordiccart/demand/inventory.py` | new | Opening model comparison, recalibrated opening stock (for S7), R-cal reorder points (for S6) |
| `python/nordiccart/demand/cli.py`, `python/run_demand_calibration.py` | new | Calibration command line (no database); writes `output/demand/<profile>/calibration.json` |
| `tests/python/test_demand.py` | new | 33 deterministic tests |
| `tests/python/test_demand_calibration.py` | new | 11 calibration tests (full profile, seeds 1 to 5; `NORDICCART_CALIBRATION_SEEDS` runs other seeds) |
| `config/generator.toml` | changed | New `[demand]`, `[demand.attractiveness]` and `[demand.lifecycle]` sections appended; no existing line changed |
| `.vscode/launch.json` | changed | One configuration added: "NordicCart - Demand: Calibration (no database)" |
| `docs/phase3_combined_activity_design.md` | changed | Decisions C17 to C19 recorded (status line, L10, 8.1, S4, S7, section 13) |
| `docs/phase2_database_implementation_status.md`, `tests/README.md` | changed | S2 recorded; new tests listed |
| `sql/verification/14_verify_replenishment.sql`, `python/nordiccart/replenishment/load.py` | changed | **Comments only** (C18 replaces "open decision"); no behaviour change; all their tests pass |
| `output/demand/full/calibration.json` | generated | Every figure of this report (exact, inventory, per-seed metrics and checks) |

## 3. The model, briefly

| Element | Rule (design section) |
|---|---|
| Personal rate | λ_c ~ Gamma(k = 0.8, θ = λ̄ / k = 3), mean λ̄ = 2.4, one seeded stream per customer number (5.1) |
| Exposure | E_c(P) = Σ over channels of π_c(ch) × Σ of u_ch(d) over the days of P from the customer's start. u_ch(d) = S*_m d_m / D × p_ch(d \| m). A customer registered all year has E = 1 exactly (5.1, 5.3) |
| Start day | The local registration day, for a channel only if registered before its last open hour (online 22:00, store 18:00); otherwise the next day. On the registration day only later hours are drawn |
| Monthly index | Raw index normalized per Nov-Oct year: S*_m = S_m × 365 / 365.45 (day-weighted mean 1.001233) (5.3.1) |
| Day weights | Weekday weights per channel; stores closed on Sundays, 25 and 26 December and 1 January; Black Week ×3. Normalized inside each month (5.3.2) |
| Black Week | Monday to Sunday of the Black Friday week, November days only: 24 to 30 November 2025 (25 to 30 November 2024, pilot month) |
| Hours | Online 07 to 22 with the evening peak at 19 to 21; stores 10 to 18 with the peak at 12 to 16. Normalized (5.3.4) |
| Channel | π(online) = 0 without a delivery address; 0.30 for in-store sign-ups (70% in store); **0.6810** for online sign-ups, solved so that the expected online share over 2024-12-03 to 2026-10-31 is exactly 0.55 (5.1) |
| Store | Online: the market warehouse. In store: P ∝ format weight (flagship 2, standard 1) × 3 for a store in the customer's city (5.1, existing rules) |
| Orders | N_c,m ~ Poisson(λ_c × mf × E_c(m)) per month; the market factor mf = 1.0 everywhere (C1) |
| Products | Opening-model weight (category weekly units × clipped price factor × seeded popularity, same stream) × life cycle: 0.4 on launch day rising to 1 after 6 weeks; −3 points a month after 18 months, floor 0.4 (5.1, 6) |
| Lines and units | 1 / 2 / 3 lines at 0.70 / 0.25 / 0.05 (mean 1.35), distinct products; products under 50 EUR two at a time with 20% (pilot rules) |
| Outcomes (C11) | Online: 3% abandoned (expire), 1% cancelled, attempts fail 6%, retry 60%, at most 3. In store: 0.2% cancelled, fail 2%, retry 50%, at most 2. Fulfilled: online 93.606%, store 98.782% |

**Where each requirement is met:**
- **Expected demand by country, channel, location, product and period:** `expected_orders(first, last, market=, channel=, location=)`, `expected_orders_by(first, last, key)`, `expected_units(first, last, locations, fulfilled)` and `weekly_units(week)`.
- **The same assumptions drive order generation, opening stock and replenishment:**
  - `simulate.month_counts` / `month_orders` are the engine's order source;
  - `inventory.recalibrated_opening` uses the opening rules unchanged. Fed the old weekly units, it reproduces all 6,096 opening listings exactly (tested);
  - `inventory.rcal_reorder_points` uses the model's forecasts.

## 4. Exact figures (full profile, in memory)

| Criterion | Model | Design | Note |
|---|---|---|---|
| Σ S*_m × d_m per Nov-Oct year | 365 exactly (both years) | 365 | |
| Day probabilities per month, each channel | sum 1 exactly | 1 | closed days 0; Black Week ratio 3 exactly |
| Hour probabilities per channel | sum 1 exactly | 1 | |
| Seasonal customer-years, primary year | 17,823.93 | 17,821 | +0.016% |
| Seasonal customer-years, growth year | 12,440.71 | 12,424 | +0.13% |
| Expected orders, primary year 2025-11-01 to 2026-10-31 | 42,777.4 | 42,772 | +0.013% |
| Expected orders, growth year 2024-12-03 to 2025-10-31 | 29,857.7 | 29,817 | +0.14% |
| Expected orders, 2024-12-03 to 2026-10-31 | 72,635.1 | 72,589 | +0.063% |
| Expected = λ̄ × customer-years | equal to 1e-15 | exact | |
| Expected online share (channel period) | 0.5500 exactly | 0.55 | growth year 0.5491, primary year 0.5506 |
| Full-year customers / partially registered | 16,014 / 3,986 | 16,014 / 3,986 | full-year exposure exactly 1 |
| Partially registered: mean exposure, zero-order share | 0.4541, 55.33% | 0.453, 55.3% | |
| All customers: zero-order share | 37.44% | 37.4% | |
| NB full year: P(0), P(1), P(2), P(3), P(≥4) | 32.99%, 19.79%, 13.36%, 9.35%, 24.51% | same | variance 9.6, variance/mean 4.0 |
| Top 20% share of orders (infinite population) | 61.4% | 61 to 62% | |
| P(0) at exposure 0.25 / 0.5 / 0.75 / 1 | 63.91 / 48.04 / 38.95 / 32.99% | 63.9 / 48.0 / 39.0 / 33.0% | 38.95% rounds to 38.9%; the design's 39.0% rounded an intermediate 0.3895 |
| Units per order | 1.4533 (window), 1.4526 (primary year) | 1.45 ± 0.05 | |
| November 2025 orders | 4,631 | ~4,613 | Black Week 318.6 a day (design ~315), other days 104.4 (~105) |

The small differences from the design come from three refinements in the model:
- the registration day is taken as the local date, with the hour rule;
- partial months are weighted by day weights (design 5.3.3) instead of calendar days;
- channel-specific start days.

Expected orders per month, primary year:

| Month | 2025-11 | 2025-12 | 2026-01 | 2026-02 | 2026-03 | 2026-04 | 2026-05 | 2026-06 | 2026-07 | 2026-08 | 2026-09 | 2026-10 | Year |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Expected | 4,631 | 5,373 | 3,251 | 2,520 | 3,018 | 2,972 | 3,315 | 3,266 | 3,057 | 3,696 | 3,643 | 4,035 | 42,777 |
| Simulated (mean of seeds 1 to 5) | 4,672 | 5,401 | 3,243 | 2,492 | 3,043 | 3,000 | 3,299 | 3,288 | 3,104 | 3,716 | 3,703 | 4,030 | 42,991 |

## 5. Statistical calibration (seeds 1 to 5, full profile, generation window 2024-12-03 to 2026-10-31)

### 5.1 Volumes and purchase frequency

| Seed | Orders | Primary year | Growth year | Mean, full-year | Zero share, full-year | Exactly 1 | Variance / mean | Top 20% | Zero share, partial | Online share |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 71,856 | 42,297 | 29,559 | 2.365 | 32.97% | 20.24% | 3.89 | 0.612 | 55.49% | 55.14% |
| 2 | 72,391 | 42,685 | 29,706 | 2.389 | 33.23% | 19.30% | 3.98 | 0.615 | 55.32% | 54.87% |
| 3 | 72,202 | 42,664 | 29,538 | 2.388 | 33.16% | 19.96% | 4.00 | 0.615 | 55.12% | 54.97% |
| 4 | 73,832 | 43,682 | 30,150 | 2.452 | 32.44% | 19.55% | 3.96 | 0.610 | 55.07% | 54.93% |
| 5 | 74,151 | 43,629 | 30,522 | 2.441 | 32.60% | 19.60% | 4.02 | 0.614 | 54.99% | 55.17% |
| 20261117 (info) | 71,742 | 42,271 | 29,471 | 2.368 | 33.40% | 19.41% | 3.94 | 0.614 | 55.47% | 54.82% |
| **Expected / design tolerance** | 72,635 | 42,777 ± 3% | 29,858 ± 3% | 2.40 ± 0.08 | 32.99% ± 1.5 pt | 19.79% ± 1.5 pt | 4.0 ± 0.5 | 0.58 to 0.65 | 55.33% ± 3 pt | 55% ± 1.5 pt |

Personal rates over 20,000 customers:
- **Mean:** 2.384 to 2.427 (2.4 ± 3%).
- **Variance:** 7.04 to 7.39 (7.2 ± 15%).

### 5.2 Geography, calendar and mix

Shares are in percent: expected, mean of seeds 1 to 5, range over the seeds, tolerance.

| Market | Expected | Simulated | Range | Tolerance |
|---|---|---|---|---|
| SE | 41.11 | 41.07 | 40.33 to 41.90 | ± 1.50 |
| DK | 21.79 | 21.79 | 21.37 to 22.33 | ± 1.50 |
| NO | 19.83 | 19.82 | 19.32 to 20.18 | ± 1.50 |
| FI | 17.27 | 17.32 | 16.96 to 18.12 | ± 1.50 |

| Location | Expected | Simulated | Location | Expected | Simulated |
|---|---|---|---|---|---|
| SE-WH-01 | 22.65 | 22.64 | SE-STO-01 | 8.19 | 8.13 |
| DK-WH-01 | 12.05 | 12.10 | SE-STO-02 / 03 / 04 | 3.66 / 3.44 / 3.17 | 3.63 / 3.41 / 3.26 |
| NO-WH-01 | 10.89 | 10.82 | NO-STO-01 / 02 / 03 | 4.96 / 2.05 / 1.93 | 4.96 / 2.05 / 1.99 |
| FI-WH-01 | 9.40 | 9.46 | DK-STO-01 / 02 / 03 | 5.43 / 2.28 / 2.02 | 5.41 / 2.25 / 2.03 |
| | | | FI-STO-01 / 02 | 5.71 / 2.16 | 5.69 / 2.17 |

| Weekday (share of the channel) | Mon | Tue | Wed | Thu | Fri | Sat | Sun |
|---|---|---|---|---|---|---|---|
| Online expected | 15.66 | 14.41 | 14.34 | 14.27 | 13.59 | 12.18 | 15.55 |
| Online simulated | 15.70 | 14.38 | 14.37 | 14.34 | 13.57 | 12.08 | 15.55 |
| Store expected | 13.20 | 14.17 | 14.52 | 15.60 | 17.57 | 24.94 | 0 (closed) |
| Store simulated | 13.22 | 14.30 | 14.61 | 15.52 | 17.34 | 25.01 | 0 |

| Other checks | Expected | Simulated (mean) | Range | Accepted range |
|---|---|---|---|---|
| Black Week share of November 2025 orders | 48.16% | 47.90% | 47.13 to 48.71% | ± 10% relative |
| Units per order | 1.453 | 1.453 | 1.452 to 1.455 | ± 2% |
| Online: expired / cancelled | 3.00% / 1.00% | 3.04% / 0.98% | | ± 20% |
| Online: attempt failures / retries after a failure | 6.00% / 60.0% | 5.95% / 59.9% | | ± 20% |
| Online / store fulfilled | 93.61% / 98.78% | 93.61% / 98.79% | | ± 2% |
| Store: cancelled / attempt failures / retries | 0.20% / 2.00% / 50.0% | 0.205% / 1.97% / 50.0% | | ± 20% (cancelled: 4 s.e., 0.10 to 0.30%) |

- **Monthly shares of the primary year:** all 12 within 0.4 of their ± 10% tolerance.
- **Hour shares:** 25 checks a seed (16 online, 9 store), all within ± 10% relative; worst 0.83 of the tolerance.
- **Category unit shares:** 10 checks a seed, worst 0.66 of the tolerance. For example, accessories expected 37.36%, simulated 37.42%; smartphones 17.69% and 17.69%.

### 5.3 Criteria refinements (for review)

1. **Monthly share.** Design 11.1 wrote "S*_m d_m / 365 ± 10%". That is the share of a constant customer base. The base grows by about a quarter during the primary year, so the check compares with the model's expected monthly share. The pure seasonal shares are still exact and are tested deterministically.
2. **Rare outcomes.** The tolerance is the larger of the relative tolerance and 4 standard errors of the observed proportion. This only widened the store cancellation check (0.2% of about 32,000 orders, about 65 events).
3. **Location shares.** These are my addition, not a design criterion. The first run used a binomial standard error and failed for seed 2: NO-STO-03 was +11.8%, outside ± 10%.
   - **Cause:** a store's orders come from a few hundred heavy-tailed customers, so they vary far more than a binomial count.
   - **Fix:** the check now uses the exact variance of a sum of negative binomial customers (`location_count_variance`: Σ μ + μ²/k), with tolerance max(10%, 4 s.e.). For NO-STO-03 that gives ± 14.3% relative.
   - **Disclosure:** this is the only tolerance changed after a failure. The draws were not changed.
4. **Inventory criteria of 11.1** (warehouse weeks of cover, fill rate, lost demand, stock turns) need the stock simulation of the engine. They move to S6 and S8. S2 reports the demand side of them (section 6).
5. **Added checks** beyond 11.1: Black Week share, hour shares, location shares, fulfilled shares, units per order, category unit shares, mean and variance of the personal rates. They catch defects in the draws that the design's criteria cannot see (section 8).

## 6. Inventory-demand assumptions (one model, design 6)

Opening inventory model (Phase 3 step 2, unchanged): 6,096 stocked listings, 28,109 units, **2,928.5 units a week** modelled. The demand model's expected **fulfilled** units on the same listings:

| Reference week | Demand model | Opening model ÷ demand model | Opening stock covers | Stores | Warehouses |
|---|---|---|---|---|---|
| First combined week, 2024-12-03 to 09 (12,690 customers, December season) | 1,352.3 | **2.17** | 20.8 weeks | 1.98 | 2.35 |
| Average week, growth year | 774.6 | **3.78** | 36.3 weeks | 3.66 | 3.88 |
| Average week, primary year | 805.0 | **3.64** | 34.9 weeks | 3.54 | 3.72 |

This confirms the design's finding of 2.2 to 3.4 times overstated demand; on the opening listings the range is 2.2 to 3.8. The opening listings get a falling share over time as 119 products launch during the calendar and older products decline.

| Category | Opening ÷ demand (first combined week) | (primary-year average) |
|---|---|---|
| Accessories | 2.01 | 3.30 |
| Smart home | 2.11 | 3.63 |
| Cameras, gaming, headphones, laptops, smartphones, tablets, TV and audio, wearables | 2.24 to 2.31 | 3.75 to 4.23 |

**Recalibrated opening stock (S7, exception E2).** Opening rules unchanged (6 / 10 weeks of cover, display and warehouse minimums, same seeded noise), with weekly units from the first combined week:

| | Listings | Opening model units | Recalibrated units | Opening model reorder points | Recalibrated reorder points |
|---|---|---|---|---|---|
| Warehouses | 1,524 | 16,502 | 8,662 | 5,809 | 3,639 |
| Stores | 4,572 | 11,607 | 10,733 | 6,107 | 6,048 |
| **Total** | 6,096 | 28,109 | **19,395** | 11,916 | 9,687 |

Stores change little because 84% of store listings sit at display minimums.

**The 20 pilot products** keep their loaded opening balances (design 6), which are overstocked:
- 1,460 units on 288 listings;
- expected demand of 73.6 units a week in the first combined week;
- so they cover **19.8 weeks** against 6 to 10.

**R-cal on 2024-12-03** (stores: 1 week × 1.5, at least display minimum − 1; warehouses: echelon over 2 weeks × 1.5, at least 2), on products on sale that day:

| | R-cal | Opening model |
|---|---|---|
| Store reorder points | 7,256 | 6,231 |
| Warehouse reorder points | 5,289 | 5,917 |

The capacity guard and the seasonal pre-build beyond the lead time need stock and stay in S6.

## 7. Determinism and the pilots

- **Floats:** none in generated values. A test scans the module sources for float arithmetic. Draws use `getrandbits`/`randrange` and correctly rounded Decimal `ln`/`exp`/`sqrt` (28 digits; 20 inside Poisson, compared with a 53-bit uniform).
- **Recorded draws:** SHA-256 values of 1,000 gamma and 1,000 Poisson draws are recorded in the tests.
- **Windows compose:** simulating 1 to 27 November, 28 November and 29 November to 31 December gives the same orders as 1 November to 31 December. Every customer-month is drawn whole from per-customer streams, so a window only filters (design 7.2).
- **Orders are valid against the rules** (tested on every simulated order):
  - never before registration; on the registration day, only later hours;
  - open days and hours only;
  - the right location for the channel;
  - distinct products that are on sale; two units only for products under 50 EUR.
- **Pilot fingerprints:** transactions `fabcd571a3b9f66827f20e54d0ebc421be5f68937af558383ee4b84f7c466264` and replenishment `858bbfd09597030b573273c9885ffae0c0299e4ed146e8aca7c98f0e05531236` are unchanged. They are tested in `test_demand.py`, the existing `test_transactions.py` and `test_replenishment.py`, and the S1 baseline file. The baseline was compared before and after adding `[demand]`, and at the end: byte-identical.

## 8. Mutation checks

Each defect was planted by monkeypatch in a separate process, running `test_demand.py` and `test_demand_calibration.py` with seed 1. Project files were untouched.

| Planted defect | Caught by |
|---|---|
| M1 monthly index not normalized | 4 tests (normalization, year exposure = 1, full-profile volumes, groups) |
| M2 registration dates ignored | 9 tests (channel solve, start rule, draw rules, volumes, groups, November, statistics, spread, Black Week) |
| M3 heterogeneity k = 0.42 in the draws | statistical criteria (zero share, variance) |
| M4 Poisson replaced by the rounded mean | statistical criteria, seed spread |
| M5 online propensity not solved (0.55 for all) | exact online share, full-profile online share, statistical online share |
| M6 Black Week missing in the draws | Black Week share check |
| M7 hour weights missing in the draws | hour share checks |
| M8 payment retries never happen | retry rate check |
| M9 product life cycle ignored | life cycle test |

## 9. Implementation choices for review

1. **Registration day:** orders from the local registration day if the customer registered before the channel's last open hour, only in later hours; otherwise from the next day.
2. **Black Week:** Monday to Sunday of the Black Friday week, November days only.
3. **Hour weights:** the values in `[demand]`.
4. **Product life cycle:** ramp 0.4 → 1 over 6 weeks; −3 points a month after 18 months; floor 0.4.
5. **Channel solve:** over 2024-12-03 to 2026-10-31. Customers without an address buy in store only; the per-year shares are 0.5491 and 0.5506.
6. **Monthly counts:** drawn from one stream per customer in month order, not one stream per customer-month. This cut stream creation by about 80% (19 s to 13 s a seed; Poisson at 20 digits and 53-bit uniforms brought it to 10 s); windows still compose.
7. **Lifecycle rates:** failure is a per-attempt rate, also for retries. Abandoned and cancelled are shares of all orders, before payment. Out of stock is decided by the stock simulation (S6).
8. **Reference week for the recalibrated opening stock:** the first combined week, not 1 November (no full-scale demand in November 2024). To confirm in S7.
9. **Calibration criteria refinements:** section 5.3.

## 10. Decisions recorded (from the S1 review)

| # | Decision | Recorded in |
|---|---|---|
| C17 | Script 14's revised pilot-document scope is approved | Design status line, 8.1, section 13 |
| C18 | V009 provides a durable, timestamped reorder point revision history, so the R0 pilot stays verifiable after monthly R-cal updates. Each revision records the listing, business time, old and new value, kind (R0 / R-cal) and document; R0 is backfilled like E3. The replenishment loader and script 14 will verify R0 from it. | Design L10 (section 4), S4 row (section 12), section 13; comments in script 14 and the replenishment loader |
| C19 | Full-profile revisions of scripts 11 and 12 remain in S7 | Design S7 row, section 13 |

## 11. Commands and run times

```
.\.venv\Scripts\python.exe -m pytest tests/python/test_demand.py -q              # 33 passed, 11 s
.\.venv\Scripts\python.exe -m pytest tests/python/test_demand_calibration.py -q  # 11 passed, 64 s
.\.venv\Scripts\python.exe python\run_demand_calibration.py --seeds 1 2 3 4 5 20261117
    # 104 of 104 checks per seed, 77 s, output/demand/full/calibration.json,
    # log logs/demand_full_calibration_20261118_141043.log
.\.venv\Scripts\python.exe -m pytest tests/python -q          # 251 passed, 97 skipped (no database)
(sandbox PG 18.4 on port 55432, NORDICCART_DB_TESTS=1)        # 348 passed in 206 s
```

## 12. Open items and next stage

- **Inventory criteria of 11.1** (weeks of cover 8 to 12, fill rate ≥ 97%, lost demand ≤ 3%, stock turns 4 to 6) are tested with the engine's stock simulation in S6 and S8.
- **R-cal remainder:** the capacity guard and the seasonal pre-build beyond the lead time go into S6.
- **S7:** opening stock of the 480 new products from `recalibrated_opening`; confirm the reference week (choice 8).
- **Next stage, after review:** S3, V008 security. It is the first stage that writes to the real database and needs a backup, a readiness report and your authorization.

## 13. Restrictions honoured

- No engine, no migration, no V008 or V009.
- No database access in S2 apart from the disposable sandbox for the regression run. The real `nordiccart_oltp` was not contacted.
- No existing configuration value, pilot record or pilot code behaviour changed (comment-only edits in two S1 files).
- Full profile used in memory only.

Stopped after S2 for review.
