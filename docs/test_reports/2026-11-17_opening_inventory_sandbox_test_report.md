# Test report: opening inventory generator, disposable sandbox

Date: 2026-11-17.

| Item | Version |
|---|---|
| PostgreSQL | 16.15, throwaway sandbox server, deleted afterwards (your server runs 18.4) |
| Python | 3.14.6 and 3.13.18, Linux |
| Packages | psycopg 3.3.9, pytest 9.2.0 |

Nothing was run against your `nordiccart_oltp`. No migration was changed. No orders, reservations or other movements were created by the program.

## What was created

| File | Purpose |
|---|---|
| `python/nordiccart/inventory/generate.py` | Opening inventory generator: demand model, quantities, reorder points |
| `python/nordiccart/inventory/load.py` | Loader through V007: listings, then opening_balance movements in lock order; plan, conflicts, verification, advisory lock, one transaction |
| `python/nordiccart/inventory/validate.py` | Validation before any write (18 rules) |
| `python/nordiccart/inventory/config.py` | Reads and checks the `[inventory]` settings |
| `python/nordiccart/inventory/model.py` | Listing rows, fingerprint, summary |
| `python/nordiccart/inventory/cli.py`, `__init__.py`, `__main__.py`; `python/run_opening_inventory.py` | Command line (dry run, `--plan`, `--apply`), logging, CSV preview, runner |
| `tests/python/test_opening_inventory.py` | 38 unit tests, no database |
| `tests/python/test_opening_inventory_db.py` | 18 database tests in their own databases (opt-in) |
| `sql/verification/12_verify_opening_inventory.sql` | Read-only check for Valentina Studio |
| `docs/phase3_opening_inventory_guide.md` | Execution guide |

Changed:

| File | Change |
|---|---|
| `config/generator.toml` | New `[inventory]` section; existing values unchanged (the master data fingerprint is the same) |
| `.vscode/launch.json` | 4 Run configurations added after your 2 existing ones, which are unchanged |
| `.gitignore` | `.vscode/` became `.vscode/*` plus `!.vscode/launch.json`, so the Run configurations are shared through Git; other editor files and `config/.env` stay ignored (checked with git) |
| `sql/verification/11_verify_master_data.sql` | Revision 2. Checks 204 and 206 now allow opening stock: any other movement, reservation, order or exchange rate still fails. It now gives 37 PASS and 5 INFO both before and after the opening load. Without this change it would show 2 false FAILs after the load. |
| `tests/python/test_master_data_seed_db.py` | One assertion updated to 37 PASS / 5 INFO for that revision |

## Run and results

| Step | Result |
|---|---|
| All 30 Python files compiled by Python 3.14 with warnings as errors | 0 problems |
| Fresh `nordiccart_oltp` (V001 to V007), master data pilot applied as on your machine | 250 rows, fingerprint `c46181a25bd6fdc1...` (same as your load) |
| Dry run | 320 listings, 288 opening movements, 32 listings at 0, 1,460 units; validation 0 issues; fingerprint `5c7a4eba5057caab...`; 0.4 s |
| `--plan` | would insert 320 listings and 288 opening balances; nothing written |
| `--apply` without `--yes` from a non-interactive shell | refused, exit code 4, 0 listings written |
| `--apply --yes` | **320 listings and 288 opening balances (1,460 units) inserted, 320 verified, 896 change_log rows** |
| `--apply --yes` again, and a third time | nothing to insert (verified); exit code 0 |
| Script 12 (default and `search_path=pg_catalog`) | **19 PASS, 4 INFO, 0 FAIL** |
| Script 11 revision 2 (both settings) | 37 PASS, 5 INFO, 0 FAIL |
| Phase 2 verification 01 to 10 with master data and opening stock present | 477 PASS, 0 FAIL, 2 WARN (the expected V007 triggers in script 07), 72 INFO; unchanged |
| Phase 2 SQL tests with the stock present | foundation 44/44, V006 119/119, V007 105/105; stock unchanged afterwards (320 listings, 288 movements, 1,460 units, 0 orders); script 12 still 19 PASS |
| Full profile on a copy: 500 products x 16 locations | 8,000 listings, 6,096 opening balances, 28,109 units; added 7,680 listings and 5,808 openings on top of the pilot in 5.5 s; script 12 with full counts 19 PASS |

## Python tests (exact counts)

| Test file | Python 3.14.6 | Python 3.13.18 |
|---|---|---|
| `test_opening_inventory.py` (unit) | **38 passed, 0 failed** | 38 passed, 0 failed |
| `test_opening_inventory_db.py` (database) | **18 passed, 0 failed** | 18 passed, 0 failed |
| `test_master_data_generator.py` (regression) | 48 passed, 0 failed | 48 passed, 0 failed |
| `test_master_data_seed_db.py` (regression) | 13 passed, 0 failed | 13 passed, 0 failed |
| `test_v007_concurrency.py` (regression) | 11 of 11 scenarios | 11 of 11 scenarios |

On 3.14 the unit and database tests ran with deprecation warnings treated as errors.

### Unit tests, `test_opening_inventory.py` (38)

**Consistency**
- every product listed once at every location (320);
- pilot counts (288 stocked, 1,460 units, 32 at zero for CAM-000007 and SMH-000011);
- only products on sale on the opening day get stock;
- opening time is 00:00 local time (SE, NO, DK 23:00 UTC; FI 22:00 UTC) with reference `OB-20241101`;
- reorder points valid and below the opening stock;
- pilot and full profile pass validation.

**Reproducibility**
- same seed, same inventory;
- the recorded fingerprint matches;
- another seed gives other quantities for the same listings;
- the pilot listings are the first 320 of the full profile.

**Realistic variation**
- at least 20 different quantities; warehouses range from 4 to 50 or more;
- warehouse stock correlates with expected demand (correlation above 0.9);
- a warehouse holds at least as much as any store in its market;
- a flagship holds at least as much as the standard stores;
- Sweden holds the most and Finland less than Denmark;
- cheaper products sell more units within every category;
- warehouse capacity is respected at full scale.

**Validator catches faults (15)**
- duplicate listing; missing listing;
- negative quantity; fractional quantity; negative reorder point;
- stock for a product not yet launched; missing stock for a product on sale;
- reorder point not below stock; store below display units; warehouse below a store;
- wrong opening time; reference over 64 characters;
- capacity exceeded; unknown location; zero listing with a time.

**Settings and command line**
- opening date after the simulation start is refused;
- a share above 1 is refused;
- a missing category demand stops the run (exit 3);
- the dry run writes the preview without a database;
- the password never reaches the log;
- the opening date is the first simulated day.

### Database tests, `test_opening_inventory_db.py` (18)

**Loading through V007**
- plan is read-only;
- apply loads every listing, each with stock on hand = opening quantity, reserved 0 and the generated reorder point; 896 change_log rows; a rerun inserts nothing;
- movements are written in the V007 lock order;
- the ledger identity holds;
- script 12 passes from Python.

**Duplicate prevention**
- the database itself refuses a second opening balance (`23505 uq_inventory_movements_one_opening_balance`);
- the database refuses direct stock writes (`23514 inventory_balances_quantities_by_ledger`);
- two loaders at once: one inserts 608 rows, the other 0.

**Conflicts write nothing**
- a different opening quantity already in the database;
- opening stock after other activity;
- opening stock for a product not on sale;
- later activity (a receipt, a changed reorder point) is drift, not a conflict.

**Failures roll back**
- a failure on the last movement leaves all inventory tables empty;
- a movement changed by the database is caught before COMMIT.

**Preconditions**
- a database without master data is refused;
- changed master data (a launch date) is refused.

**Scale and command line**
- scaling to 30 products only adds 160 listings and their openings; pilot ids are unchanged;
- `--plan`, `--apply --yes` twice from the command line.

## Mutation checks (do the tests catch defects?)

One defect at a time, in a copy of the code, then both inventory test files run on Python 3.14:

| Defect | Caught by |
|---|---|
| advisory lock removed | two loaders at once |
| a different opening quantity not detected | different opening quantity is a conflict |
| opening stock allowed after later activity | opening stock not added after activity |
| opening stock for a product not on sale not detected | conflict test for that case |
| inserts outside one transaction | rerun, two loaders, failure rollback, and more |
| verification before COMMIT skipped | changed row caught before commit |
| master data precondition skipped | both precondition tests |
| movements not written in the V007 lock order | lock order test (added after the first round missed this defect) |
| products not yet launched get stock | counts, launch rule, validation, and more |
| the same variation at every location | counts, fingerprint, and more |
| opening at UTC midnight instead of local midnight | opening time, validation, and more |
| demand ignores price | counts, fingerprint, cheaper-sells-more |
| validator: launch rule removed | validator test |
| validator: warehouse-covers-stores rule removed | validator test |
| validator: capacity rule removed | validator test |

15 of 15 caught.

## Stock loaded (pilot)

| Market | Warehouse units | Store units | Value at unit cost |
|---|---|---|---|
| SE | 353 | 200 | 897,407.45 SEK |
| NO | 180 | 141 | 627,841.19 NOK |
| DK | 192 | 139 | 388,172.60 DKK |
| FI | 154 | 101 | 43,079.84 EUR |

- Warehouses hold 4 to 50 units per product (average 12.2).
- Stores hold 1 to 8 (average 2.7).

## Outstanding issues

1. **Demand is a planning assumption.** It is set to about 6.7 units per product per week across the chain at full scale (20,000 customers buying about 4.4 orders of 1.5 units a year). The order simulation is not designed yet. When its volume is set, revisit `[inventory.category_demand]`, or stock will run out too fast or never.
2. **Pilot stock is sized for the full business.** With only 100 pilot customers, pilot orders will hardly draw the stock down, so stockouts will not occur naturally in a pilot simulation. The tests can force them. Sizing stock to the pilot instead would break the rule that the pilot is the start of the full dataset.
3. **Two pilot products start at zero.** CAM-000007 and SMH-000011 launch after the opening day. Their first stock must come from a `purchase_receipt` with a document number at launch, in the activity simulation.
4. **Reorder points are stored but not acted on yet.** Replenishment (warehouse to store transfers, supplier receipts) belongs to the activity simulation. 39 store listings of expensive single-unit items have a reorder point of 0, meaning reorder once sold out.
5. **The database does not enforce every opening rule.** V007 does not stop an opening balance that is dated before a product launch, at an inactive location, or after other movements. The generator, validator and loader enforce these, and so does script 12; the database alone would not. A V008 rule could add it if wanted.
6. **Not run on your exact setup.** Tested on Linux with PostgreSQL 16.15 and Python 3.14.6; your laptop runs Windows, PostgreSQL 18.4 and Python 3.14.7. Nothing used is specific to a PostgreSQL version after 13, and the unit tests' fingerprint check confirms identical data on your machine.
7. **Scripts 11 and 12 assume "before any orders".** Their "nothing else yet" checks are meant for this stage. Once the activity simulation starts, use them only for their other checks.
