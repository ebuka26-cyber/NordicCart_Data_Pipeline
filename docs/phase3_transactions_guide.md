# Phase 3, step 3: pilot transactions (VS Code and Valentina Studio)

The transaction generator writes 20 pilot orders into `nordiccart_oltp`, together with their payments and the stock movements they cause. Each order runs through its whole life:

1. The customer places the order.
2. Its lines and totals are calculated.
3. The stock is reserved.
4. The customer pays (or the payment fails).
5. The order is confirmed and fulfilled, which takes the stock out of inventory.

Every step goes through the V006/V007 database rules and functions, each in its own transaction, in business-time order. Returns, refunds, shipments, FastAPI and hourly scheduling are not part of this step.

| Outcome | Orders | What happens |
|---|---|---|
| Fulfilled | 12 | Reserved, paid on the first attempt, confirmed, fulfilled |
| Fulfilled after a retry | 2 | First payment fails, the retry is captured, then fulfilled |
| Payment failed | 2 | Every attempt fails (3 online, 2 in store); stock released |
| Cancelled by the customer | 1 | Reserved, the customer cancels before paying; stock released |
| Reservation expired | 1 | Reserved, never paid; after 15 minutes the reservation expires (U4) |
| Out of stock: quantity | 1 | 5 tablets when the Danish warehouse holds 4; nothing is reserved, not even the other line |
| Out of stock: reserved by another customer | 1 | The last phone in the Malmö store is reserved by a customer one till away |

In total:
- 20 orders with 26 lines;
- 21 payment attempts (14 captured, 7 failed);
- 23 reservations;
- 17 sale movements, 19 units sold;
- 100 units of work, each one transaction.

All orders are placed between 1 and 14 November 2024. Stock on hand goes from 1,460 to 1,441 units.

## How the orders are made realistic

- **Customers.** Each order uses an existing, active customer of its market who registered before the order. Online orders go to the customer's address. In-store customers who live in the store's city are three times as likely.
- **Products and prices.** Only products on sale on the local order day, with free stock at the fulfilling location, chosen by that location's expected demand. The price and cost are the `product_prices` row valid at the order time, in the market currency, with the market VAT rate.
- **Places and times.**
  - Online orders ship from the market's warehouse and are placed between 07:00 and 23:00 local time.
  - In-store orders are fulfilled from the store's own stock during opening hours (10:00 to 19:00, never on a Sunday).
- **Shipping fee.** Online orders pay the market's shipping fee unless they reach the free-shipping threshold. Only the fee is charged; no shipment is created.
- **Timing.**
  - Stock is reserved a few seconds after the order is placed: for 15 minutes online, 5 minutes in store.
  - Payments finish within that time.
  - Online orders are packed the same afternoon or the next morning; in-store orders are handed over within two minutes.
- **Payments.** Card, mobile payment and buy-now-pay-later online; card, mobile payment and cash in store. Cash is in-store only. At most 3 attempts per order.

All settings are in `config\generator.toml`, section `[transactions]`. Only the pilot profile is allowed: a larger volume waits for your review.

## Before you start

- The master data and the opening inventory are loaded in `nordiccart_oltp` with the same `config\generator.toml`. The program checks this and stops if anything is missing or different.
- No other orders or stock movements are in the database. The program refuses to write on top of them, because they would change the stock the plan expects.

## Part 1. Add the Run buttons

`.vscode\launch.json` needs three new entries. The file `vscode_launch.json` sent with this step is your current file with the three entries added at the end; your four existing entries are unchanged. To install it:

1. Open `vscode_launch.json` in VS Code, press `Ctrl+A`, then `Ctrl+C`.
2. Open `.vscode\launch.json`, press `Ctrl+A`, then `Ctrl+V`, then `Ctrl+S`.

| Configuration | What it does |
|---|---|
| NordicCart - Transactions: Dry Run | Generates and validates the 20 orders, writes the CSV preview. No database. |
| NordicCart - Transactions: Check | Compares with `nordiccart_oltp`, read-only. |
| NordicCart - Transactions: Load | Writes the orders after you type the database name. |

## Part 2. Run it in VS Code

1. Open the Run and Debug view (`Ctrl+Shift+D`).
2. Pick the configuration in the drop-down at the top.
3. Press the green Run button (or `F5`). The output appears in the terminal panel.

Run them in this order:

1. **Dry Run.** Expected lines:
   - `orders: 20`
   - `orders by final status: {'cancelled (customer_request)': 1, 'cancelled (out_of_stock)': 2, 'cancelled (reservation_expired)': 1, 'fulfilled': 14, 'payment_failed': 2}`
   - `units of work (one transaction each): 100`
   - `transaction plan fingerprint (SHA-256): fabcd571a3b9f668...`
   - `validation: 0 issues`

   The preview is in `output\transactions\pilot`:
   - `orders.csv`;
   - `order_items.csv`;
   - `payment_attempts.csv`;
   - `units_of_work.csv` (every step in the order it will run);
   - `summary.json`.
2. **Check.** Expected:
   - `master data and opening inventory present and unchanged`;
   - a table showing 20 orders and 100 units of work to write;
   - `conflicts: 0   blocking: 0`;
   - `plan finished: 20 orders and 100 units of work would be written, nothing was written.`
3. **Load.** The terminal asks `Type the database name (nordiccart_oltp) to write 20 orders through 100 units of work ...`.
   - Click into the terminal, type `nordiccart_oltp` and press Enter.
   - Each step is logged as it runs, for example `2024-11-09 14:35:23 UTC SE-2024-00000012 U2 reserve  rolled back (out_of_stock)`.
   - Expected at the end: `committed: 100 units of work in 100 transactions (...), 20 orders placed, 20 orders verified, 280 change_log rows written`.
   - Pressing Enter without typing cancels; nothing is written.
4. **Load again** (proves repeats are safe). Expected: `nothing to write: the database already holds all 20 pilot orders and every unit of work (verified).`

The same from the VS Code terminal:

```powershell
python python\run_transactions.py            # dry run
python python\run_transactions.py --plan     # check, read-only
python python\run_transactions.py --apply    # load (asks to confirm)
python -m pytest tests\python\test_transactions.py -v
```

Each run writes a log to `logs\transactions_pilot_<mode>_<time>.log`. Exit codes:
- 0: success;
- 1: validation failed;
- 2: conflict with the database, or a step gave an unplanned result;
- 3: settings, connection or database problem (including missing master data or opening stock);
- 4: cancelled.

## Part 3. Verify in Valentina Studio

Open a SQL Editor on **nordiccart_oltp** and run:

| Script | Expected | Checks |
|---|---|---|
| `sql\verification\13_verify_pilot_transactions.sql` (new) | 50 rows: 44 PASS, 6 INFO, `failed_checks` = 0 | Counts per status, payment and reservation; customer, product, location and date consistency; prices, VAT, totals and shipping; payment rules; every lifecycle path; that the out-of-stock orders really lacked stock; the stock ledger. The INFO rows show revenue, cost and gross profit per market, the payment mix and the dates |
| `sql\verification\11_verify_master_data.sql` (revision 4) | 43 rows: 33 PASS, 10 INFO | Orders, lines, payments and reservations are INFO counts (script 13 checks them); receipts and transfers too (script 14). Revision 3 gave 42 rows: 33 PASS, 9 INFO |
| `sql\verification\12_verify_opening_inventory.sql` (revision 3) | 24 rows: 17 PASS, 7 INFO | The opening checks read the opening balances, which sales, receipts and transfers do not change. Revision 2 gave 23 rows: 17 PASS, 6 INFO |

Script 13 is now revision 2: it stays 44 PASS, 6 INFO after the replenishment pilot (step 4), which adds receipts and transfers dated after the pilot's last event (2024-11-15 08:44:22 UTC). `python python\run_transactions.py --plan` keeps verifying the pilot after replenishment: it reports those later movements and counts them in its stock check.

Scripts 11 and 12 give the same result before and after the transactions.

To look at the orders:

```sql
SELECT o.order_number, o.channel, o.status, o.cancellation_reason, o.order_total, o.currency_code,
       o.placed_at, o.confirmed_at, o.fulfilled_at, o.cancelled_at
  FROM orders o ORDER BY o.placed_at;

SELECT o.order_number, p.attempt_number, p.payment_method, p.status, p.failure_reason, p.amount
  FROM payment_attempts p JOIN orders o USING (order_id) ORDER BY o.placed_at, p.attempt_number;
```

## Part 4 (optional). Database tests

They use their own databases (`nordiccart_tx_test_*`): V001 to V007, the master pilot and the opening inventory, dropped afterwards. They never connect to `nordiccart_oltp`.

```powershell
$env:NORDICCART_DB_TESTS = "1"
python -m pytest tests\python\test_transactions_db.py -v
```

Expected: `21 passed`. Without the variable they are skipped.

## Why repeating or interrupting a load is safe

1. **One transaction per step, in business-time order.** A crash or error loses at most the step that was running, which is rolled back.
2. **It continues where it stopped.**
   - Each order is matched on its order number.
   - From what the database holds (status, payment attempts, reservations, sales), the program works out which steps are done and checks every stored value against the plan.
   - It then runs only the remaining steps.
   - A finished load does nothing.
3. **It refuses a database that does not match the plan.**
   - An order with other values, steps recorded out of order, orders that are not in this pilot, other stock movements, or stock that differs from the opening stock plus the recorded steps: the run stops before writing anything.
4. **The database refuses doubles.** Order numbers are unique; one live payment per order; one active reservation and one sale per order line; guarded status changes.
5. **One loader at a time.** An advisory lock refuses a second loader.
6. **Each result is checked.** For example, a stock reservation must return `reserved`, or `out_of_stock` for the two out-of-stock orders. A different result rolls that step back and stops the run.

## Troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `The opening inventory in the database is not the one this pilot is built from` | Opening stock missing or different | Run Opening Inventory: Check; it must show `insert 0` and `conflicts 0`. |
| `The master data in the database is not the dataset ...` | Master data missing or changed | Run the master data Check with the same config. |
| `CONFLICT ... orders in the database are not part of this plan` | Other orders exist | The pilot needs a database without other orders. Send me the lines. |
| `CONFLICT ... stock movements other than opening balances and sales` | Receipts, adjustments or similar exist | As above. |
| `CONFLICT ... the order exists with other values` | The orders were loaded with other settings or a changed config | Nothing was written. Send me the lines. |
| `Another transaction load is running` | A second Load is still open | Wait for it to finish, or close the other terminal. |
| `U2 returned ..., the plan expects ...` | The database gave an unplanned result | That step was rolled back; the steps before it are kept. Send me the log; after the cause is fixed, Load again continues. |
| `The transaction simulation is approved for the pilot profile only` | `--profile full` was used | Larger volumes wait for the pilot review. |
| `Not an interactive terminal: add --yes` | Started without keyboard input | Use the Load configuration and type the name in the terminal. |
| `test_same_seed_gives_the_same_plan_and_the_recorded_fingerprint` fails | Seed or `[transactions]` settings differ | Check `config\generator.toml`; send me the fingerprint from the Dry Run. |
