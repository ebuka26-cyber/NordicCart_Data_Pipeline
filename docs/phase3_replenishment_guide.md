# Phase 3, step 4: replenishment pilot (VS Code and Valentina Studio)

The replenishment pilot restocks NordicCart after the 20 pilot orders. It writes into `nordiccart_oltp`:

1. **Reorder points (R0).** The 4 warehouses get new reorder points that cover their online orders **and** the stores they resupply (69 listings; total 323 -> 533 units). Each one stays below the listing's opening stock.
2. **Store transfers (T1, T2).** Every store listing at or below its reorder point after the pilot is refilled from its market warehouse:
   - 4 transfer documents, 8 lines, 10 units;
   - the transfer_out leaves the warehouse on Monday 18 November 2024 between 06:00 and 08:00;
   - the transfer_in reaches the store the same morning, before it opens at 10:00.
3. **Supplier deliveries (R1).** 4 purchase orders, one per market, 26 units:
   - they are delivered 10 to 14 days later as 5 delivery notes (the Swedish order arrives in two parts);
   - each note is one purchase_receipt per product at the warehouse.

In total: **17 units of work, each one transaction**. Business dates run from 15 November to 6 December 2024, all after the last pilot event (2024-11-15 08:44:22 UTC). Stock goes from 1,441 to 1,467 units, and nothing is left in transit. No orders are added.

| Document | Natural or forced | Why |
|---|---|---|
| Transfers to stores | **Natural** (`reorder_point`) | The store's stock was at or below its reorder point at the evening review |
| Purchase orders `PO-SE/NO/DK/FI-2024-000001` | **Forced** (`forced_reorder`) | Pilot scenario. No warehouse reaches its reorder point in three weeks at pilot demand (about 6% of the modelled demand), so one order per market is placed on purpose to test the receipt path. The CSV preview and the log mark them FORCED. |

The design and the decisions behind it are in `docs\phase3_replenishment_feasibility.md` (D1 to D7 and D9 to D11 adopted; D8, demand recalibration, waits for the full-scale simulation). All settings are in `config\generator.toml`, section `[replenishment]`.

## Before you start

- The master data, the opening inventory and **all 20 pilot orders** are loaded with the same `config\generator.toml`. The program checks this and stops if anything is missing or different. Your database meets this (checked read-only on 2026-11-18: 17 units of work to write, 0 conflicts).
- **Make a backup first.** Receipts and transfers cannot be deleted afterwards (V007 refuses DELETE and TRUNCATE on the stock ledger). From the project folder in the VS Code terminal:

  ```powershell
  & "C:\Program Files\PostgreSQL\18\bin\pg_dump.exe" --format=custom --dbname=nordiccart_oltp --file="database\backups\nordiccart_oltp_<yyyymmdd_hhmmss>_pre_replenishment.dump"
  ```

  It asks for the password, or reads it from your environment.

## Part 1. The Run buttons

`.vscode\launch.json` has three new entries after the existing seven. Nothing to install.

| Configuration | What it does |
|---|---|
| NordicCart - Replenishment: Dry Run | Generates and validates the plan and writes the CSV preview. No database. |
| NordicCart - Replenishment: Check | Compares the plan with `nordiccart_oltp`, read-only. |
| NordicCart - Replenishment: Load | Writes the 17 units of work after you type the database name. |

## Part 2. Run it in VS Code

1. Open the Run and Debug view (`Ctrl+Shift+D`).
2. Pick the configuration in the drop-down at the top.
3. Press the green Run button (or `F5`). The output appears in the terminal panel.

Run them in this order.

**1. Dry Run.** Expected lines:
- `transfer lines / units: 8 / 10`
- `purchase orders (forced / natural): 4 (4 / 0)`
- `delivery notes (split purchase orders): 5 (1)`
- `units of work (one transaction each): 17`
- `stock on hand after the pilot -> after replenishment: 1441 -> 1467`
- `replenishment plan fingerprint (SHA-256): 858bbfd09597030b...`
- `validation: 0 issues`

The preview is in `output\replenishment\pilot`:
- `reorder_points.csv`, `transfers.csv`, `purchase_orders.csv` (column `forced`), `receipts.csv`;
- `units_of_work.csv` (every step in the order it will run), `reviews.csv`, `summary.json`.

**2. Check.** Expected:
- `pilot transactions complete (100 units of work verified against their plan)`;
- a table showing 17 units of work to write, by kind: `R0 reorder points 4, T1 dispatch 4, T2 arrival 4, R1 receipt 5`;
- `conflicts: 0   blocking: 0`;
- `plan finished: 17 units of work would be written, nothing was written.`

**3. Load.** The terminal asks `Type the database name (nordiccart_oltp) to write 17 replenishment units of work ...`.
- Click into the terminal, type `nordiccart_oltp` and press Enter. Pressing Enter without typing cancels; nothing is written.
- Each step is logged as it runs, for example `2024-11-18 05:56:10 UTC T1 dispatch TR-SE-2024-000001-SE-STO-03 dispatched 3 units, 2 lines SE-WH-01 -> SE-STO-03`.
- Expected at the end: `committed: 17 units of work in 17 transactions (revise 4, dispatch 4, arrive 4, receive 5), 17 verified, 111 change_log rows written`.

**4. Load again** (proves repeats are safe). Expected: `nothing to write: the database already holds every replenishment unit of work (17, verified).`

**5. Transactions: Check** (the pilot still verifies). Expected:
- `orders 20 20 0`, `units of work 100 100 0`;
- `later replenishment (after the plan, counted in the stock check): 21 movements, net +26 units`;
- `conflicts: 0   blocking: 0`.

The same from the VS Code terminal:

```powershell
python python\run_replenishment.py            # dry run
python python\run_replenishment.py --plan     # check, read-only
python python\run_replenishment.py --apply    # load (asks to confirm)
python python\run_transactions.py --plan      # the pilot still verifies
```

Each run writes a log to `logs\replenishment_pilot_<mode>_<time>.log`. Exit codes:
- 0: success;
- 1: validation failed;
- 2: conflict with the database, or a step gave an unplanned result;
- 3: settings, connection or database problem (including missing pilot orders);
- 4: cancelled.

## Part 3. Verify in Valentina Studio

Open a SQL Editor on **nordiccart_oltp** and run:

| Script | Expected | Checks |
|---|---|---|
| `sql\verification\14_verify_replenishment.sql` (new) | 43 rows: 36 PASS, 7 INFO, `failed_checks` = 0 | Counts (5 receipt lines, 26 units, 5 delivery notes, 4 purchase orders, 1 split, 4 transfer documents, 8 + 8 lines, 10 units, 0 in transit, warehouse reorder points 533). Placement: receipts at warehouses, transfers warehouse -> store of the same market, arriving at the store named in the reference. Launch and opening dates; reference formats; working days and hours; nothing dated inside the pilot. Policy: store at or below its reorder point at dispatch, warehouse kept at its reorder point. Ledger, business-time running balance, conservation (1,467), capacity. |
| `sql\verification\13_verify_pilot_transactions.sql` (revision 2) | 50 rows: 44 PASS, 6 INFO | Unchanged result; check 505 now expects 1,441 + 26 received = 1,467 units |
| `sql\verification\11_verify_master_data.sql` (revision 4) | 43 rows: 33 PASS, 10 INFO | New INFO row 208: `5 receipts, 8 transfers out, 8 transfers in` |
| `sql\verification\12_verify_opening_inventory.sql` (revision 3) | 24 rows: 17 PASS, 7 INFO | New INFO row 204; the opening checks are unaffected |

Before the Load, scripts 11, 12 and 13 already give these results (the revisions are valid before and after). Script 14 is for after the Load.

To look at the documents:

```sql
SELECT m.occurred_at, m.movement_type, l.location_code, p.sku, m.quantity_change, m.external_reference
  FROM inventory_movements m
  JOIN stock_locations l USING (stock_location_id)
  JOIN products p USING (product_id)
 WHERE m.movement_type IN ('purchase_receipt', 'transfer_out', 'transfer_in')
 ORDER BY m.occurred_at, m.movement_id;

SELECT l.location_code, sum(b.reorder_point) AS reorder_points, sum(b.quantity_on_hand) AS on_hand
  FROM inventory_balances b JOIN stock_locations l USING (stock_location_id)
 GROUP BY 1 ORDER BY 1;
```

## Part 4 (optional). Tests

```powershell
python -m pytest tests\python\test_replenishment.py -q        # 61 tests, no database
```

The database tests create and drop their own databases (`nordiccart_repl_test_*`) and never connect to `nordiccart_oltp`. They need a role that can create databases. They read the server from the PG variables and fill any missing ones from `config\.env`. To keep them away from your own server, run them against a throwaway PostgreSQL 18 instance, as was done for the test report:

```powershell
$bin = "C:\Program Files\PostgreSQL\18\bin"
& "$bin\initdb.exe" -D "$env:TEMP\nc_sandbox" -U postgres -A trust -E UTF8 --locale=C
& "$bin\pg_ctl.exe" -D "$env:TEMP\nc_sandbox" -o "-p 55432" -l "$env:TEMP\nc_sandbox.log" -w start
$env:PGHOST = "localhost"; $env:PGPORT = "55432"; $env:PGUSER = "postgres"; $env:PGPASSWORD = "unused"; $env:PGDATABASE = "postgres"
$env:NORDICCART_DB_TESTS = "1"
python -m pytest tests\python\test_replenishment_db.py -v     # expected: 22 passed
python tests\python\test_v007_concurrency.py --recreate       # expected: 14 PASS
& "$bin\pg_ctl.exe" -D "$env:TEMP\nc_sandbox" stop             # then delete the folder
```

## Why repeating or interrupting a load is safe

1. **One transaction per step, in business-time order.** A crash or error loses at most the step that was running, which is rolled back.
2. **It continues where it stopped.**
   - Every planned movement is matched on its business key: receipts on (warehouse, product, delivery note), dispatches on (warehouse, product, transfer reference), arrivals through the transfer_out they point to, reorder points on their old and new values.
   - Done steps must be a prefix of the plan.
   - The stock must equal the after-pilot stock plus exactly those steps.
3. **The database refuses doubles.**
   - A delivery note or transfer reference is unique per type, location and product (`uq_inventory_movements_document_line`).
   - A transfer can arrive only once (`uq_inventory_movements_one_transfer_in_per_out`).
   - Reserved units can never be transferred (`ck_inventory_balances_no_oversell`).
4. **It refuses a database that does not match the plan.** Receipts or transfers with other values, an arrival at another store, movements dated inside the pilot, movements outside this plan, stock adjustments or write-offs, or pilot orders not complete: the run stops before writing anything.
5. **One loader at a time.** An advisory lock refuses a second loader.

## If something has to be undone

There is no SQL undo: V007 keeps the stock ledger insert-only. The only way back is the backup taken under "Before you start". Restoring it replaces the whole database and needs your decision.

## Troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `The pilot transactions are not complete` | Pilot orders missing | Run Transactions: Check; it must show `100 100 0`. |
| `The pilot transactions in the database do not match their plan` | Pilot orders changed, or other orders exist | Nothing was written. Send me the lines. |
| `CONFLICT ... dated at or before the pilot's last event` | A receipt or transfer dated inside the pilot | Nothing was written. Send me the lines. |
| `CONFLICT ... not part of this plan` | Receipts or transfers from elsewhere | Nothing was written. Send me the lines. |
| `CONFLICT ... receipt ... plan ...` or `transfer_in ... plan ...` | Loaded with other settings or a changed config | Nothing was written. Send me the lines. |
| `... has N <sku> available, the plan dispatches M` | Stock differs from the plan at that moment | That step was rolled back; the steps before it are kept. Send me the log. |
| `Another replenishment load is running` | A second Load is still open | Wait for it to finish, or close the other terminal. |
| `No keyboard input: add --yes` / `Not an interactive terminal` | Started without keyboard input | Use the Load configuration and type the name in the terminal. |
| `The replenishment simulation is approved for the pilot profile only` | `--profile full` was used | Larger volumes wait for the combined activity simulation. |
| `test_same_seed_gives_the_same_plan_and_the_recorded_fingerprint` fails | `[replenishment]` or the seed changed | Check `config\generator.toml`; send me the fingerprint from the Dry Run. |
