# S6 milestone 3: monthly business rules and verification · implementation report

November 20, 2026. **Ready for review: sandbox-verified.** S6 suite: 25 of 25 tests passed. Full Python regression: 525 passed.

Scope:
- Built: the R-cal monthly review, P1 price changes and month-end verification, in the existing engine and run protocol.
- Every write ran in the sandbox.
- Not done: no migration; no real-database write; no full historical generation; no S7 work.

## 1. What was added

| # | Item | Implementation |
|---|---|---|
| 1 | **R-cal** (design 6) | On the first working day of each month (2025-01-02, 2025-02-03, 2025-03-03), at 05:00 local (`[activity] rcal_local_hour`, before the 05:30 store review), rank 2.<br>New reorder points come from the S2 function `demand.inventory.rcal_reorder_points`:<ul><li>stores: forecast over the lead time × safety, at least display minimum − 1;</li><li>warehouses: echelon demand over the lead time × safety, at least 2.</li></ul>Capacity guard: a warehouse whose new reorder points would sum above its storage capacity keeps its current ones.<br>New unit `units.stock.review_reorder_points`: one transaction per location with changes. It takes the same locks and the same guarded `UPDATE … WHERE reorder_point = old` as R0, plus `reorder_point_revisions` rows of kind `monthly_review`, reference `RCAL-<location>-<YYYYMMDD>`. V009's chain and commit checks apply.<br>S (order-up-to) stays in the engine: max(opening-model S, s + 1). |
| 2 | **P1 price changes** (decision C8, as recommended) | Lifecycle markdowns at 6 and 12 months after launch, at 00:00 local, rank 1, in every market:<ul><li>a seeded 3–8% per product and step (`[activity] markdown_after_months`, `markdown_range`);</li><li>rounded to the market's shelf price point (`price_point`);</li><li>unit cost unchanged.</li></ul>A markdown is skipped when:<ul><li>it would not lower the price;</li><li>it would leave no margin;</li><li>it falls before the origin (no backdated price);</li><li>it falls on or after discontinuation.</li></ul>New unit `units.prices.change_price`: one transaction that closes the current row (`valid_to`, the only price column a writer may update) and inserts the new row; V004's EXCLUDE keeps the periods disjoint.<br>The price timeline is a pure function of master data and config, so orders see the new price from the change time, and full and incremental replay see the same prices. |
| 3 | **Integration** | Both rules are actions of the same deterministic replay: P1 from the price timeline, R-cal from the calendar.<br>Prices are now part of the hashed state: state version 2 adds the current price per product and market and the `product_prices` count, derived by business time. A price row closed at `valid_to = t` is still the price as of t. |
| 4 | **Month-end verification** (`activity/monthly.py`) | 19 read-only checks, each counting violations (expected 0):<ul><li>**orders**: totals; one gap-free sequence; stock records match status; open orders at month end = the month-end checkpoint's carried orders;</li><li>**payments**: amount = order total; exactly one capture when confirmed or fulfilled; no pending attempt on a closed order;</li><li>**inventory**: ledger identity; no negative or oversold balance; network conservation (on hand + in transit changes by receipts − sales); receipts map to PO lines within the ordered quantity;</li><li>**rules**: the P1 changes due by the timeline = the P1 rows recorded; R-cal reviews recorded = the runs' R-cal units;</li><li>**rates**: three per day; a rate on every non-EUR local fulfilment day;</li><li>**checkpoints**: contiguous verified runs chained to their predecessor; each run's checkpoint (same hash, as of the window end); the month-end checkpoint equals the database state.</li></ul>It runs after the window that completes a month (logged; failure → exit 1) and **again as a gate before the next month's first window**, so a broken month blocks continuation even after a restart. It writes nothing and needs no new table. |
| 7 | **Carry integrity** | Carry version 2 adds:<ul><li>the demand key of each open order;</li><li>a SHA-256 of the carry.</li></ul>On restore, **every carried future action is re-derived** from its seeded stream:<ul><li>open orders are rebuilt from their demand draw and the lines the database holds, and must be identical, including their remaining steps;</li><li>transfer dispatch and arrival times, and supplier receipt times and quantities, are recomputed.</li></ul>Any difference is `DriftError` (exit 2, nothing written). This comes on top of M2's checks against the database. |
| 8 | **Fingerprint clarification** | Added to the M2 report: M2 reproduced M1's plan fingerprints and checkpoint state hashes, not its configuration fingerprints. See section 3. |

Changed files:
- New:
  - `python/nordiccart/activity/monthly.py`
  - `python/nordiccart/units/prices.py`
- Extended:
  - `activity/engine.py`: P1, R-cal, carry v2, version `s6-m3`;
  - `activity/run.py`: P1 and R-cal units, month gate;
  - `activity/state.py`: state v2 with prices;
  - `units/stock.py`: `review_reorder_points`;
  - `python/run_activity.py`: month-end verification after a month's last window.
- Configuration: `[activity]` section in `config/generator.toml`. The pilots read only their own sections, and their fingerprints are unchanged.
- Verification scripts, narrow compatibility revisions as S5 did for 207:
  - **11**: check 105 counts the launch prices; new INFO row 507 counts P1 changes. 44 rows.
  - **12**: check 501 compares opening balances with the reorder point **in force at the opening**, from V009's history. R-cal may later raise a reorder point above an opening quantity. Before V009 it keeps the current comparison: the history is read through `query_to_xml`, so the script still parses without the table.
- Tests:
  - `tests/python/test_activity_engine.py`: 5 new tests.
  - Five existing suites' script 11 expectations: 43 → 44 rows (one INFO).

## 2. Results

**Four consecutive months** (2024-12-03 to 2025-03-31; 119 UTC-day windows; stress volume; sandbox):

| Month | Month-end checks | Orders placed / fulfilled | Payment attempts | Movements: sale / transfer in+out / receipt | POs | P1 changes | R-cal: reviews / revisions | Rate days | Runs | Open at month end |
|---|---|---|---|---|---|---|---|---|---|---|
| 2024-12 (from Dec 3) | 19/19 | 232 / 215 | 231 | 297 / 230 / 20 | 13 | 0 | 0 / 0 | 29 | 29 | 1 |
| 2025-01 | 19/19 | 150 / 146 | 155 | 203 / 250 / 21 | 6 | 20 | 12 / 104 | 31 | 31 | 1 |
| 2025-02 | 19/19 | 127 / 121 | 128 | 163 / 138 / 10 | 7 | 0 | 4 / 20 | 28 | 28 | 3 |
| 2025-03 | 19/19 | 158 / 152 | 158 | 211 / 182 / 18 | 15 | 4 | 4 / 17 | 31 | 31 | 1 |

In total: 24 P1 rows; one current price per product and market; orders after a markdown pay the new price.

| Test (new in M3) | Result |
|---|---|
| `test_price_timeline_and_rcal_in_memory` | Every markdown is 3–8%, lowers the price, keeps a margin and falls on the 6- or 12-month day; price periods are contiguous. R-cal runs in January, February and March, and each value equals `rcal_reorder_points` | PASS |
| `test_four_months_with_monthly_rules_incremental_equals_full` | **119 windows**: window 1 uses a full replay and 118 use incremental replay. Every plan fingerprint and end hash equals one in-memory full replay. A second clone forced to `--replay full` has an **identical run ledger (checkpoints and carry included) and identical business records**. Four month-end verifications, 19/19 each. P1 rows = timeline (24). R-cal revisions on exactly 2025-01-02, 02-03 and 03-03. Pilot records identical; pilot `--plan` 0 conflicts; scripts 01–18 0 FAIL | PASS |
| `test_crash_in_monthly_rule_units_resumes_identically` | The process is killed inside the first R-cal unit (Jan 2) and inside the first P1 unit (Jan 3). Each unit is rolled back, and the resume does it once. The end result is identical to an uninterrupted run (ledger and business records) | PASS |
| `test_month_end_verification_blocks_a_broken_month` | December verified 19/19. A December payment amount is then changed directly, which the state hash cannot see. The first January window is refused: exit 1, "1 payment attempts not for the order total"; nothing written | PASS |
| `test_altered_future_actions_in_the_carry_block_the_window` | The checkpoint's carry is rewritten three ways:<ul><li>an open order's fulfilment moved one hour later, with the digest recomputed: blocked ("differs from its derivation");</li><li>the same change without recomputing the digest: blocked ("does not match its SHA-256");</li><li>one future step removed, with the digest recomputed: blocked ("differ").</li></ul>Exit 2 each time, nothing written. With the original carry the window then runs | PASS |

What testing found and fixed:
- The `product_prices` count: master data pre-loads the launch prices of later launches (CAM-000007, 2025-03-19). The replay now counts price rows from the timeline.
- The P1 month check counted those launch prices as unexpected P1 rows; it now ignores launch rows.
- Script 12 check 501, as described in section 1.

## 3. Fingerprints across milestones (clarification, requirement 8)

| Value | What it covers | M1 → M2 | M2 → M3 |
|---|---|---|---|
| Plan fingerprint | The window's units of work (descriptors) | Identical for all 29 December windows | Identical for the 29 December windows (no P1 or R-cal falls in December; checked against M1's stored ledger). New units from January |
| State hash | The canonical state as of the window end | Identical | **Different by design**: state version 2 adds prices and the price-row count |
| Configuration fingerprint | SHA-256(engine version, profile, configuration file) | **Different by design** (`s6-m1` → `s6-m2`); never claimed equal | Different again (`s6-m3`, plus the new `[activity]` section) |

Consequence:
- A run started under one milestone is not resumed under another.
- An older checkpoint without a matching carry or state version is not continued silently. M1/M2 demo databases would need a fresh run from the base.

The real database has no runs, so nothing is affected there.

## 4. Regression

| Suite | Result |
|---|---|
| S6 suite (`test_activity_engine.py`) | 25 tests: 20 from M1/M2 and 5 new. All passed inside the full regression below |
| Full Python regression, every database suite in the sandbox (`NORDICCART_DB_TESTS`, `S5`, `S6`) | **525 passed, 0 failed, 0 skipped** (10 min 58 s) |

The final full run passed. Two earlier full runs did not:
1. **First run: 524 passed, 1 failed.** `test_master_data_seed_db` also pins script 11's INFO count, which I had missed when updating the counts. It was corrected (10 → 11).
2. **Second run: setup errors** in the S6 tests. The first run's V008 rollback test had removed CONNECT on `nordiccart_s6_base`; this is the known sandbox side effect of `DROP OWNED BY`. The S6 fixture now reads the base as the sandbox superuser (read-only), and test clones re-apply the database privileges as before.

## 5. Unresolved issues and choices for review

1. **Verification script revisions** (11: 105 and new INFO 507; 12: 501). They follow the S5 precedent for check 207. The full-profile revisions of scripts 11 and 12 remain planned for S7 (decision C19).
2. **R-cal values**:
   - At pilot volume, R-cal mostly lowers warehouse reorder points to the floor of 2. The demand model expects far less demand than the opening model assumed (design 6).
   - S (order-up-to) still comes from the opening model; recalibrating S is left for the S7/S8 calibration.
3. **P1 does not change demand**: the demand model's product attractiveness uses the launch price. Price elasticity of markdowns is not modelled.
4. **Month-end verification is focused**. Scripts 11–18 remain the full check: the engine runs as the loader, who may not read `change_log` contents (script 14). The tests run all scripts as the analyst.
5. **Not done (design 7.4):** monthly recovery points (pg_dump while the lock is held) were not part of this request.
6. **Still open from M2:** the state reads grow with table size (the next bottleneck at full scale).
7. **Plan-only document fields** (review positions, order-up-to levels in transfer and PO lines) cannot be re-derived from the database. A deliberate change to them with a recomputed digest would change only plan fingerprints, not business actions. A `--replay full` audit would expose it.
