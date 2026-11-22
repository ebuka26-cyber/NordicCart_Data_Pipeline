# Phase 3, step 1: master data generator (VS Code and Valentina Studio)

The generator creates the NordicCart master data with Python and loads it into `nordiccart_oltp`. It writes no orders, payments, stock or exchange rates.

| Table | Pilot rows | Source |
|---|---|---|
| `currencies` | 4 | SEK, NOK, DKK, EUR |
| `countries` | 4 | SE, NO, DK, FI with 2026 standard VAT (25%, FI 25.5%) and time zones |
| `product_categories` | 10 | consumer electronics (smartphones, laptops, ... accessories) |
| `products` | 20 | generated from the seed, fictional brands |
| `product_prices` | 80 | one opening list price and unit cost per product and market, in the market currency |
| `stock_locations` | 16 | 12 stores and 4 warehouses (one per country, each ships online orders) |
| `stores` / `warehouses` | 12 / 4 | subtype rows of the locations |
| `customers` | 100 | generated from the seed; reserved `example.com/.net/.org` emails |
| **Total** | **250** | plus 250 `change_log` rows written by the capture triggers |

Three modes. Only the last one writes:

| Command | What it does |
|---|---|
| `python python\run_master_data_seed.py` | **Dry run.** Generates, validates and writes a CSV preview. Contacts no database. |
| `python python\run_master_data_seed.py --plan` | **Plan.** Also connects and compares with the database, read-only. |
| `python python\run_master_data_seed.py --apply` | **Apply.** Inserts the missing rows in one transaction after you type the database name. |

Running `--apply` again inserts nothing. Rows are matched on their business keys (`sku`, `customer_number`, `location_code`, ...). If a row exists with different values, the run stops before writing anything.

---

## Part 1. One-time setup in VS Code

1. **Open the project.** In VS Code: File > Open Folder > `<project folder>`.
2. **Python extension.** Press `Ctrl+Shift+X`, search "Python" (publisher Microsoft) and install it if it is not installed yet.
3. **Open a terminal.** Terminal > New Terminal. A PowerShell prompt opens in the project folder. Run every command below from here.
4. **Check Python.** Run `python --version`. You need 3.10 or later.
   - If the result is `Python 3.10` or higher: continue.
   - If `python` is not recognised: try `py --version`. If that works, use `py` instead of `python` in every command. If neither works, install Python from python.org (tick "Add python.exe to PATH") and open a new terminal.
5. **Create a virtual environment** (once):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   The prompt now starts with `(.venv)`.
   - If PowerShell says that running scripts is disabled, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then the activate line again. This applies to this terminal only.
   - Then press `Ctrl+Shift+P`, choose "Python: Select Interpreter" and pick the one that contains `.venv`. New terminals then activate it automatically.
6. **Install the packages** (once):
   ```powershell
   python -m pip install -r python\requirements.txt
   ```
   This installs psycopg 3 (PostgreSQL driver), tzdata (time zones on Windows) and pytest.
7. **Database settings.** Only if `config\.env` does not exist yet:
   ```powershell
   Copy-Item config\.env.example config\.env
   ```
   Open `config\.env` and set `PGPASSWORD`. Change `PGHOST`, `PGPORT` or `PGUSER` only if your server differs from localhost:5432 as postgres. Keep `PGDATABASE=nordiccart_oltp`.
   - `config\.env` is listed in `.gitignore`. The password is never written to logs or output.
   - A variable set in the terminal (for example `$env:PGPASSWORD = "..."`) overrides the file.

Each later session: open the folder, open a terminal, and check that the prompt shows `(.venv)`. If not, run `.\.venv\Scripts\Activate.ps1`.

## Part 2. Generate and check, without the database

8. **Unit tests:**
   ```powershell
   python -m pytest tests\python\test_master_data_generator.py -q
   ```
   Expected: `48 passed`. One test compares the dataset fingerprint with the value recorded in the sandbox. If it passes, your machine generates exactly the same 250 rows.
9. **Dry run:**
   ```powershell
   python python\run_master_data_seed.py
   ```
   Expected lines:
   - `generated 250 rows: currencies=4, countries=4, product_categories=10, products=20, product_prices=80, stock_locations=16, stores=12, warehouses=4, customers=100`
   - `dataset fingerprint (SHA-256): c46181a25bd6fdc1ad2c8fedab185c19802041c46b4aae703e66a0f58d768f64`
   - `validation: 0 issues ...`
   - `dry run finished ... no database was contacted`

   Look at the data in `output\master_data\pilot\` (one CSV per table plus `summary.json`). Open them in VS Code or Excel.

   Alternative: open `python\run_master_data_seed.py` and click the Run button (triangle, top right). It runs the dry run only.

## Part 3. Write to nordiccart_oltp

This is the only part that changes your database. Do it once you are happy with Part 2.

10. **Plan (read-only):**
    ```powershell
    python python\run_master_data_seed.py --plan
    ```
    Expected: `connected to nordiccart_oltp ... migrations 001,...,007`, a table with `insert` 250 and `conflict` 0 in every row, then `plan finished: 250 rows would be inserted, nothing was written.`
11. **Apply:**
    ```powershell
    python python\run_master_data_seed.py --apply
    ```
    It shows the same table and asks: `Type the database name (nordiccart_oltp) to insert 250 rows`. Type `nordiccart_oltp` and press Enter.
    - Expected: `committed: 250 rows inserted, 250 rows verified, 250 change_log rows written`.
    - Pressing Enter without typing cancels; nothing is written.
12. **Run it again** (proves it is safe to repeat):
    ```powershell
    python python\run_master_data_seed.py --apply
    ```
    Expected: `nothing to insert: the database already holds all 250 rows (verified).` No question is asked.

Each run writes a log file in `logs\` (ignored by git). Exit codes:
- 0: success;
- 1: validation failed;
- 2: conflict with existing rows;
- 3: settings, connection or database problem;
- 4: cancelled.

## Part 4. Verify in Valentina Studio

13. Open a SQL Editor on **nordiccart_oltp**. Open `sql\verification\11_verify_master_data.sql` and execute it.
    - Expected: 43 rows, 33 PASS and 10 INFO, `failed_checks` = 0. (Revision 4 of the script; it gives the same result after the opening inventory, the pilot transactions and the replenishment pilot are loaded. Revision 3 gave 42 rows, 33 PASS and 9 INFO.)
    - The script checks:
      - the row counts;
      - that there are no exchange rates or stock movements other than opening balances and sales yet (orders, payments and reservations are shown as INFO counts; script 13 checks them);
      - each country's currency, VAT and time zone;
      - locations and their subtypes;
      - one current price per product and market, in the market currency, with a margin;
      - lower-case unique emails;
      - adult customers;
      - in-store sign-ups only where a store was open.
14. Press **Refresh** in the schema tree, then look at the data:
    ```sql
    SELECT * FROM countries ORDER BY country_code;

    SELECT l.location_code, l.location_name, l.city, l.opened_on, s.store_format, w.ships_online_orders
      FROM stock_locations l
      LEFT JOIN stores s USING (stock_location_id)
      LEFT JOIN warehouses w USING (stock_location_id)
     ORDER BY l.location_code;

    SELECT p.sku, p.product_name, c.category_name, pp.country_code, pp.list_price, pp.unit_cost, pp.currency_code
      FROM products p
      JOIN product_categories c USING (category_id)
      JOIN product_prices pp USING (product_id)
     ORDER BY p.sku, pp.country_code;

    SELECT customer_number, first_name, last_name, email, city, country_code, signup_channel, registered_at
      FROM customers ORDER BY customer_number;
    ```
15. Optional: run the Phase 2 checks again (verification 01 to 10, the three files in `tests\sql\`). They give the same results as before; the seeded rows do not affect them.

## Part 5 (optional). Database tests

These tests create their own databases (`nordiccart_seed_test_*`) on the same server, apply V001 to V007 to them and drop them afterwards. They never connect to `nordiccart_oltp`. They need a role that can create databases (`postgres`).

```powershell
$env:NORDICCART_DB_TESTS = "1"
python -m pytest tests\python\test_master_data_seed_db.py -v
```

Expected: `13 passed`. They prove:
- plan is read-only;
- apply then rerun inserts 250, then 0;
- script 11 passes;
- scaling up only adds rows;
- a conflict writes nothing;
- later business changes count as drift, not conflicts;
- a taken email is refused;
- a failure half-way rolls everything back;
- a row the database drops or changes is caught before COMMIT;
- two seeders at once do not duplicate;
- a database without V007, or the wrong database, is refused;
- the command line works end to end.

Without `NORDICCART_DB_TESTS=1` they are skipped.

## Scaling up later

`--profile full` generates 500 products and 20,000 customers. The pilot rows are the first rows of the full dataset, so on a database that already holds the pilot, `--apply --profile full` inserts only the missing 22,300 rows. In the sandbox this took 10 seconds. Run it only after the pilot is validated. Then change the two numbers in the `params` block of script 11 to 500 and 20000.

## Settings

All generator settings are in `config\generator.toml`:
- seed (`20261117`);
- profile sizes;
- simulation window (2024-11-01 to 2026-10-31);
- customer mix per market;
- price conversion rates.

Changing any of them changes the generated data. A database that already holds the old data then reports conflicts instead of mixing two datasets. The fixed facts are in `python\nordiccart\reference_data\`: markets, VAT, stores, warehouses, names and the catalogue rules.

## Troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `python is not recognized` | Python not on PATH | Use `py`, or install Python with "Add to PATH" (step 4). |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | PowerShell execution policy | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again. |
| `ModuleNotFoundError: No module named 'psycopg'` (or `tzdata`, `pytest`) | Packages not installed in the active environment | Check `(.venv)` in the prompt, then repeat step 6. |
| `ZoneInfoNotFoundError` | tzdata missing (Windows has no built-in time zone database) | Repeat step 6. |
| `could not connect: ... Connection refused` | PostgreSQL not running, or wrong `PGHOST`/`PGPORT` | Start the server; check `config\.env`. |
| `password authentication failed` | Wrong `PGPASSWORD` or `PGUSER` | Fix `config\.env`. |
| `has no meta.schema_migrations table` | `PGDATABASE` points to another database | Set `PGDATABASE=nordiccart_oltp`. |
| `missing migrations 007` | V007 not applied | Apply V007 first (Phase 2 guide, Part C). |
| `CONFLICT ...` lines and exit code 2 | A row exists with different values (edited by hand, or the seed or config changed) | Nothing was written. Send me the CONFLICT lines; do not edit rows to force it. |
| `Not an interactive terminal: add --yes` | Run from a tool without keyboard input | Run it in the VS Code terminal, or add `--yes` deliberately. |
| `test_pilot_fingerprint_is_stable` fails | Your files differ from the tested ones (config, reference data or code) | Check that `config\generator.toml` is unchanged; send me the new fingerprint. |
| Script 11 FAIL on 104, 105, 109 after the full profile | Expected counts are for the pilot | Set 500 and 20000 in its `params` block. |
| Strange characters (Ã¶) in a CSV in Excel | Excel guessed the encoding | Use Data > From Text/CSV and pick UTF-8 (the files carry a UTF-8 marker, so this is rare). |
