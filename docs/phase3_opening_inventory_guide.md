# Phase 3, step 2: opening inventory (VS Code and Valentina Studio)

The opening inventory generator gives every NordicCart store and warehouse its stock on the first simulated day (2024-11-01). It writes through the V007 inventory mechanisms only: a listing row in `inventory_balances`, then an `opening_balance` movement that the V007 trigger adds to the stock on hand. It creates no orders, reservations or other movements.

| What is written (pilot) | Rows |
|---|---|
| `inventory_balances`: every one of the 20 products listed at all 12 stores and 4 warehouses | 320 |
| `inventory_movements` of type `opening_balance`, dated 00:00 local time on 2024-11-01, reference `OB-20241101` | 288 |
| Units on hand after loading | 1,460 |
| Listings left at 0: CAM-000007 (launches 2025-03-19) and SMH-000011 (launches 2025-10-19), 16 locations each | 32 |
| `change_log` rows written by the capture triggers | 896 |

## How the quantities are set

- **Demand per product.** Each category has a typical weekly unit sale in `config\generator.toml`, section `[inventory.category_demand]`. Within a category, cheaper products sell more units, and each product gets a seeded popularity factor.
- **Demand per location.** Demand is split across markets by the customer mix (SE 40%, DK 22%, NO 20%, FI 18%). Within a market, 55% goes to its warehouse (online orders) and 45% to its stores. Flagship stores carry twice the weight of standard stores.
- **Stock.** Stores hold 6 weeks of their demand and warehouses 10, with seeded variation of plus or minus 15%. Stores never drop below their display units: 3 for products under 100 EUR, 2 under 500 EUR and 1 above, plus 1 in a flagship. Warehouses hold at least 4 units, and always at least as much as any store in their market.
- **Reorder points.** A reorder point is the demand during the lead time (1 week for stores, 2 for warehouses) times 1.5. It always sits below the opening stock.

The result ranges from 1 unit of a TV in a standard store to 50 phone chargers in the Swedish warehouse. The same seed always gives the same quantities, and the pilot listings are the first listings of the full profile.

## Before you start

- The master data pilot must already be in `nordiccart_oltp`, loaded with the same `config\generator.toml`. The program checks this and stops if any master row is missing or different.
- The VS Code setup from the master data guide is done: `.venv` active, packages installed, `config\.env` filled in.

## Part 1. Run it with the VS Code Run button

`.vscode\launch.json` has four new entries next to your two master data entries:

| Configuration | What it does |
|---|---|
| NordicCart - Opening Inventory: Dry Run | Generates and validates, writes the CSV preview. No database. |
| NordicCart - Opening Inventory: Check | Compares with `nordiccart_oltp`, read-only. |
| NordicCart - Opening Inventory: Load | Inserts the opening inventory after you type the database name. |
| NordicCart - Opening Inventory: Unit Tests | Runs the 38 unit tests. |

To run one:
1. Open the Run and Debug view (`Ctrl+Shift+D`, or the triangle-with-bug icon on the left).
2. Pick the configuration in the drop-down at the top.
3. Press the green Run button (or `F5`). The output appears in the terminal panel.

Run them in this order:

1. **Dry Run.** Expected lines:
   - `listings (inventory_balances rows): 320`
   - `opening_balance movements: 288`
   - `opening units: 1460`
   - `listed at 0, launched after 2024-11-01: CAM-000007, SMH-000011`
   - `opening inventory fingerprint (SHA-256): 5c7a4eba5057caab...`
   - `validation: 0 issues`

   The preview is in `output\opening_inventory\pilot\opening_inventory.csv`, with one row per listing including the expected weekly demand, and in `summary.json`.
2. **Unit Tests.** Expected: `38 passed`.
3. **Check.** Expected:
   - `master data present and unchanged`;
   - a table with `insert` 320 and 288;
   - `conflicts: 0`;
   - `plan finished: 320 listings and 288 opening balances would be inserted, nothing was written.`
4. **Load.** The terminal asks `Type the database name (nordiccart_oltp) to insert 320 listings and 288 opening balances (1460 units)`. Click into the terminal, type `nordiccart_oltp` and press Enter.
   - Expected: `committed: 320 listings and 288 opening balances (1460 units) inserted, 320 listings verified, 896 change_log rows written`.
   - Pressing Enter without typing cancels; nothing is written.
5. **Load again** (proves repeats are safe). Expected: `nothing to insert: the database already holds the opening inventory for all 320 listings (verified).`

The same from the VS Code terminal, if you prefer typing:

```powershell
python python\run_opening_inventory.py            # dry run
python python\run_opening_inventory.py --plan     # check, read-only
python python\run_opening_inventory.py --apply    # load (asks to confirm)
python -m pytest tests\python\test_opening_inventory.py -v
```

Each run writes a log to `logs\opening_inventory_pilot_<mode>_<time>.log`. Exit codes:
- 0: success;
- 1: validation failed;
- 2: conflict with stock already in the database;
- 3: settings, connection or database problem (including missing master data);
- 4: cancelled.

## Part 2. Verify in Valentina Studio

1. Open a SQL Editor on **nordiccart_oltp** and run `sql\verification\12_verify_opening_inventory.sql`.
   - Expected: 24 rows, 17 PASS and 7 INFO, `failed_checks` = 0 (revision 3 of the script, which stays valid after the pilot transactions and the replenishment pilot; revision 2 gave 23 rows, 17 PASS and 6 INFO; the first version 19 PASS and 4 INFO).
   - It checks the counts; one opening balance per listing; the ledger identity (stock on hand = sum of movements); opening time and reference; no opening stock before a launch; reorder points; warehouse cover and capacity. It also shows the units and stock value at cost per market.
2. Run `sql\verification\11_verify_master_data.sql` again.
   - It was updated in this step (revision 2) so that opening stock is allowed.
   - Expected: 33 PASS, 10 INFO, `failed_checks` = 0 (revision 4; revision 3 gave 33 PASS, 9 INFO). Check 206 shows `320 listings, 288 opening balances`.
3. Look at the stock:
   ```sql
   SELECT l.location_code, p.sku, p.product_name, b.quantity_on_hand, b.reorder_point
     FROM inventory_balances b
     JOIN stock_locations l USING (stock_location_id)
     JOIN products p USING (product_id)
    ORDER BY l.location_code, p.sku;

   SELECT l.location_code, count(*) AS products, sum(b.quantity_on_hand) AS units
     FROM inventory_balances b JOIN stock_locations l USING (stock_location_id)
    GROUP BY l.location_code ORDER BY l.location_code;
   ```

## Part 3 (optional). Database tests

They use their own databases (`nordiccart_inventory_test_*`): V001 to V007 plus the master pilot, dropped afterwards. They never connect to `nordiccart_oltp`.

```powershell
$env:NORDICCART_DB_TESTS = "1"
python -m pytest tests\python\test_opening_inventory_db.py -v
```

Expected: `18 passed`. Without the variable they are skipped.

## Why a repeat run cannot duplicate stock

1. **The database refuses it.** V007 allows one `opening_balance` per location and product (index `uq_inventory_movements_one_opening_balance`). It also refuses direct writes to `quantity_on_hand`.
2. **The program checks first.** It matches each listing on location and product, and compares the existing opening movement, which never changes. Equal rows are left alone and missing rows are inserted. A different quantity, or opening stock for a product not yet on sale, is a conflict: the run stops before writing anything.
3. **No back-dating.** Opening stock is never added to a listing that already has later stock activity.
4. **All or nothing.** The load is one transaction under an advisory lock. Every row and the stock totals are checked before COMMIT.

Later activity does not cause conflicts on a rerun: sales, receipts and changed reorder points are expected (reorder point changes are reported as drift).

## Troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `The master data in the database is not the dataset this inventory is built from` | Master data not loaded, or loaded with other settings, or edited | Run the master data `--plan`: it must show `insert 0` and `conflict 0`. Use the same `config\generator.toml`. |
| `missing migrations 007` / `index uq_inventory_movements_one_opening_balance ... is missing` | V007 not applied | Apply V007 first (Phase 2 guide, Part C). |
| `CONFLICT ... opening balance differs` | Opening stock already in the database with other quantities (other seed or settings, or hand-edited) | Nothing was written. Send me the CONFLICT lines. |
| `CONFLICT ... stock activity is already recorded` | The listing already has receipts, sales or reservations | Opening stock cannot be added afterwards. Send me the lines. |
| `Not an interactive terminal: add --yes` | Started from a tool without keyboard input | Use the Load configuration (it runs in the terminal) and type the name there. |
| `test_pilot_fingerprint_is_stable` fails | Your seed or `[inventory]` settings differ from the tested ones | Check `config\generator.toml`; send me the new fingerprint from the dry run. |
| Script 12 FAIL on 201 | Stock movements other than opening balances and sales exist | Receipts, transfers, adjustments and write-offs come in a later step; send me the result. |
| Script 12 FAIL on 101 to 103 after the full profile | Expected counts are for the pilot | Set 8000, 6096 and 28109 in its `params` block. |
| `ModuleNotFoundError` in VS Code Run | The Run configuration uses another interpreter | `Ctrl+Shift+P` > "Python: Select Interpreter" > pick `.venv`. |
