# SQL verification report: pilot transactions

| Item | Value |
|---|---|
| Date | 2026-11-18, 01:33:22 local time (UTC+1) |
| Database | `nordiccart_oltp`, PostgreSQL 18.4 |
| When | Right after the pilot load committed (01:32:51), with no other activity in between |
| Client | `psql` 18.4: `psql -X --csv -v ON_ERROR_STOP=1 -d nordiccart_oltp -f <script>` |
| Session | Forced read-only (`default_transaction_read_only=on`). Each script is one SELECT statement with no temporary objects. |
| Order | Script 13, then 11, then 12, as instructed |
| Raw output | `logs/sql_verification_13_verify_pilot_transactions_20261118_013322.csv`, `logs/sql_verification_11_verify_master_data_20261118_013322.csv`, `logs/sql_verification_12_verify_opening_inventory_20261118_013322.csv` |
| Expected values | Script headers and `docs/phase3_transactions_guide.md`, Part 3 |
| **Result** | **All three scripts PASS: 115 rows, 94 PASS, 21 INFO, 0 FAIL. `failed_checks` = 0 in every script.** |

## Summary

| Script | Revision | Checks | Rows (expected) | PASS (expected) | INFO (expected) | FAIL | `failed_checks` | psql exit | Result |
|---|---|---|---|---|---|---|---|---|---|
| `sql/verification/13_verify_pilot_transactions.sql` | 1 (new in step 3) | Orders, lines, payments, reservations, sale movements and the stock they change | 50 (50) | 44 (44) | 6 (6) | 0 | 0 | 0 | **PASS** |
| `sql/verification/11_verify_master_data.sql` | 3 | Master data (currencies to customers) | 42 (42) | 33 (33) | 9 (9) | 0 | 0 | 0 | **PASS** |
| `sql/verification/12_verify_opening_inventory.sql` | 2 | Opening inventory listings, opening balances and the stock ledger | 23 (23) | 17 (17) | 6 (6) | 0 | 0 | 0 | **PASS** |

How to read the results: a row is PASS when `actual` equals `expected` and FAIL otherwise. INFO rows have no expected value and only report a figure. `failed_checks` is the number of FAIL rows and is repeated on every row. Integrity checks count the rows that break a rule, so they expect 0.

## Requested checks at a glance

| Requirement | Script / check | Expected | Actual | Status |
|---|---|---|---|---|
| Orders processed | 13 / 101 | 20 | 20 | PASS |
| Order statuses | 13 / 102 | cancelled 4, fulfilled 14, payment_failed 2 | cancelled 4, fulfilled 14, payment_failed 2 | PASS |
| Cancellation reasons | 13 / 103 | customer_request 1, out_of_stock 2, reservation_expired 1 | customer_request 1, out_of_stock 2, reservation_expired 1 | PASS |
| No order left open | 13 / 108 | 0 | 0 | PASS |
| Payment attempts | 13 / 105 | captured 14, failed 7 | captured 14, failed 7 | PASS |
| Payment rules (amount, cash, capture on the last attempt, time order) | 13 / 307, 401 to 403, 406, 407 | 0 each | 0 each | PASS |
| Stock reservations | 13 / 106 | consumed 17, expired 2, released 4 | consumed 17, expired 2, released 4 | PASS |
| No active reservation left | 13 / 109 | 0 | 0 | PASS |
| Inventory movements (sales) | 13 / 107 | sale lines 17, units 19 | sale lines 17, units 19 | PASS |
| Only opening balances and sales | 13 / 504, 11 / 204, 12 / 201 | 0 | 0 | PASS |
| Stock ledger identity | 13 / 501 to 503, 12 / 301 to 303 | 0 each | 0 each | PASS |
| **Remaining inventory** | **13 / 505** | **1441** | **1441** | **PASS** |
| Opening stock unchanged | 12 / 101 to 103 | 320 listings, 288 opening balances, 1460 units | 320, 288, 1460 | PASS |
| Master data unchanged | 11 / 101 to 109 | 4, 4, 10, 20, 80, 16, 12, 4, 100 | 4, 4, 10, 20, 80, 16, 12, 4, 100 | PASS |

## Script 13: pilot transactions (50 rows)

Groups: 1xx counts, 2xx customers, products, locations and dates, 3xx money, 4xx order lifecycle, 5xx stock, 6xx INFO summaries.

| Check | Type | Object | Expected | Actual | Status |
|---|---|---|---|---|---|
| 101 | count | orders | 20 | 20 | PASS |
| 102 | count | orders by final status | cancelled 4, fulfilled 14, payment_failed 2 | cancelled 4, fulfilled 14, payment_failed 2 | PASS |
| 103 | count | cancelled orders by reason | customer_request 1, out_of_stock 2, reservation_expired 1 | customer_request 1, out_of_stock 2, reservation_expired 1 | PASS |
| 104 | count | order lines | 26 | 26 | PASS |
| 105 | count | payment attempts by status | captured 14, failed 7 | captured 14, failed 7 | PASS |
| 106 | count | reservations by status | consumed 17, expired 2, released 4 | consumed 17, expired 2, released 4 | PASS |
| 107 | count | sale movements and units sold | sale lines 17, units 19 | sale lines 17, units 19 | PASS |
| 108 | count | orders still open (placed, stock_reserved or confirmed) | 0 | 0 | PASS |
| 109 | count | active reservations | 0 | 0 | PASS |
| 201 | consistency | orders without a customer or by a customer of another market | 0 | 0 | PASS |
| 202 | consistency | orders placed before the customer registered, or by an inactive customer | 0 | 0 | PASS |
| 203 | consistency | order lines for a product not on sale on the local order day | 0 | 0 | PASS |
| 204 | consistency | orders at a location that was not open on the local order day | 0 | 0 | PASS |
| 205 | consistency | orders placed outside the pilot window or before the opening stock count | 0 | 0 | PASS |
| 206 | consistency | online orders not shipped to the customer's address | 0 | 0 | PASS |
| 207 | consistency | in-store orders on a Sunday or outside opening hours | 0 | 0 | PASS |
| 208 | consistency | business events dated in the future | 0 | 0 | PASS |
| 301 | money | orders not in the currency of their market | 0 | 0 | PASS |
| 302 | money | lines whose price or cost is not the product_prices row valid at placed_at | 0 | 0 | PASS |
| 303 | money | lines with another VAT rate than the market, or with a discount | 0 | 0 | PASS |
| 304 | money | lines whose total or VAT does not follow the formulas | 0 | 0 | PASS |
| 305 | money | orders whose items total, shipping VAT or order total is wrong | 0 | 0 | PASS |
| 306 | money | orders with the wrong shipping fee (online below the free threshold pays the fee) | 0 | 0 | PASS |
| 307 | money | payment attempts whose amount is not the order total, or cash online | 0 | 0 | PASS |
| 401 | lifecycle | fulfilled orders without exactly one capture, confirmed at the capture time | 0 | 0 | PASS |
| 402 | lifecycle | captures that are not the last attempt, or more attempts than allowed | 0 | 0 | PASS |
| 403 | lifecycle | payment attempts out of time order (placed, attempted, authorized, captured or failed, next attempt) | 0 | 0 | PASS |
| 404 | lifecycle | reservations with the wrong lifetime (15 min online, 5 min in store) | 0 | 0 | PASS |
| 405 | lifecycle | reservations not matching the order line and fulfilment location, or reserved before placed_at | 0 | 0 | PASS |
| 406 | lifecycle | payment results after the reservation expired | 0 | 0 | PASS |
| 407 | lifecycle | payment_failed orders: an attempt not failed, or not closed at the last failure | 0 | 0 | PASS |
| 408 | lifecycle | payment_failed and customer_request orders whose reservations are not released at cancelled_at | 0 | 0 | PASS |
| 409 | lifecycle | reservation_expired orders not expired by the sweeper after expires_at, or with a payment | 0 | 0 | PASS |
| 410 | lifecycle | out_of_stock orders with a reservation or a payment attempt | 0 | 0 | PASS |
| 411 | lifecycle | out_of_stock orders where every line was available when they were cancelled | 0 | 0 | PASS |
| 412 | lifecycle | fulfilled orders without a consumed reservation and a sale of each line at fulfilled_at | 0 | 0 | PASS |
| 413 | lifecycle | fulfilment delays: in store over 5 minutes, online over 48 hours | 0 | 0 | PASS |
| 414 | lifecycle | sale movements of orders that are not fulfilled | 0 | 0 | PASS |
| 501 | stock | balances where on hand \<\> sum of movements | 0 | 0 | PASS |
| 502 | stock | balances where reserved \<\> active reservations | 0 | 0 | PASS |
| 503 | stock | negative stock or more reserved than on hand | 0 | 0 | PASS |
| 504 | stock | stock movements other than opening_balance and sale | 0 | 0 | PASS |
| 505 | stock | units on hand (opening units minus units sold) | 1441 | 1441 | PASS |
| 506 | stock | sales dated before the order was confirmed or at another location | 0 | 0 | PASS |
| 601 | info | orders by market and channel |  | DK in_store 2, DK online 2, FI in_store 1, FI online 1, NO in_store 3, NO online 2, SE in_store 4, SE online 5 | INFO |
| 602 | info | fulfilled revenue excl. VAT, cost and gross profit by market (local currency) |  | DK 2996.00 / 2215.45 / 780.55 DKK, FI 153.62 / 89.42 / 64.20 EUR, NO 5908.80 / 4528.37 / 1380.43 NOK, SE 19323.20 / 16042.04 / 3281.16 SEK | INFO |
| 603 | info | shipping fees charged on fulfilled orders (incl. VAT) |  | SE 49.00 SEK | INFO |
| 604 | info | payment attempts by method and status |  | buy_now_pay_later captured 1, card captured 9, card failed 7, cash captured 1, mobile_payment captured 3 | INFO |
| 605 | info | first order placed and last order fulfilled (UTC) |  | 2024-11-01 12:08:41 / 2024-11-15 08:44:22 | INFO |
| 606 | info | change_log rows for the order and inventory tables |  | inventory_balances 671, inventory_movements 305, inventory_reservations 46, order_items 26, orders 72, payment_attempts 56 | INFO |

## Script 11: master data, revision 3 (42 rows)

Revision 3 turned checks 201, 202, 203 and 205 (orders, lines, payment attempts, reservations) into INFO counts, because script 13 checks those rows. They now show the pilot (20, 26, 21, 23). Check 204 accepts sale movements. The master data checks give the same results as before the load.

| Check | Type | Object | Expected | Actual | Status |
|---|---|---|---|---|---|
| 101 | row count | currencies | 4 | 4 | PASS |
| 102 | row count | countries | 4 | 4 | PASS |
| 103 | row count | product_categories | 10 | 10 | PASS |
| 104 | row count | products | 20 | 20 | PASS |
| 105 | row count | product_prices (one per product and market) | 80 | 80 | PASS |
| 106 | row count | stock_locations | 16 | 16 | PASS |
| 107 | row count | stores | 12 | 12 | PASS |
| 108 | row count | warehouses | 4 | 4 | PASS |
| 109 | row count | customers | 100 | 100 | PASS |
| 201 | info | orders, if loaded (checked by script 13) |  | 20 | INFO |
| 202 | info | order_items, if loaded (checked by script 13) |  | 26 | INFO |
| 203 | info | payment_attempts, if loaded (checked by script 13) |  | 21 | INFO |
| 204 | no activity yet | stock movements other than opening_balance and sale | 0 | 0 | PASS |
| 205 | info | inventory_reservations, if loaded (checked by script 13) |  | 23 | INFO |
| 206 | info | opening inventory, if loaded (checked by script 12) |  | 320 listings, 288 opening balances | INFO |
| 207 | no activity yet | exchange_rates | 0 | 0 | PASS |
| 301 | market | SE | Sweden \| SEK \| 0.2500 \| Europe/Stockholm | Sweden \| SEK \| 0.2500 \| Europe/Stockholm | PASS |
| 302 | market | NO | Norway \| NOK \| 0.2500 \| Europe/Oslo | Norway \| NOK \| 0.2500 \| Europe/Oslo | PASS |
| 303 | market | DK | Denmark \| DKK \| 0.2500 \| Europe/Copenhagen | Denmark \| DKK \| 0.2500 \| Europe/Copenhagen | PASS |
| 304 | market | FI | Finland \| EUR \| 0.2550 \| Europe/Helsinki | Finland \| EUR \| 0.2550 \| Europe/Helsinki | PASS |
| 401 | locations | locations without exactly one matching store or warehouse row | 0 | 0 | PASS |
| 402 | locations | location codes not \<country\>-STO-nn / \<country\>-WH-nn | 0 | 0 | PASS |
| 403 | locations | markets without a warehouse that ships online orders | 0 | 0 | PASS |
| 404 | locations | markets without a store | 0 | 0 | PASS |
| 405 | locations | stores with a format other than flagship or standard | 0 | 0 | PASS |
| 501 | prices | product and market pairs without exactly one current price | 0 | 0 | PASS |
| 502 | prices | prices not in the market currency | 0 | 0 | PASS |
| 503 | prices | prices whose unit cost is not below the net price (no margin) | 0 | 0 | PASS |
| 504 | prices | prices valid before the product launch day (market time) | 0 | 0 | PASS |
| 505 | products | categories without products | 0 | 0 | PASS |
| 506 | products | products with a sku not like SPH-000001 | 0 | 0 | PASS |
| 601 | customers | emails not lower case or repeated ignoring case | 0 | 0 | PASS |
| 602 | customers | customer numbers not like C00000001 | 0 | 0 | PASS |
| 603 | customers | partial addresses (address, postal code and city go together) | 0 | 0 | PASS |
| 604 | customers | registered before the first NordicCart location in their market opened | 0 | 0 | PASS |
| 605 | customers | registered in the future | 0 | 0 | PASS |
| 606 | customers | younger than 18 when they registered | 0 | 0 | PASS |
| 607 | customers | in-store sign-ups with no store open in their market that day | 0 | 0 | PASS |
| 701 | info | customers per market (in-store sign-ups) |  | DK 27 (7), FI 24 (4), NO 15 (5), SE 34 (11) | INFO |
| 702 | info | products per category |  | ACCESSORIES 5, CAMERAS 1, GAMING 1, HEADPHONES 1, LAPTOPS 2, SMART_HOME 2, SMARTPHONES 3, TABLETS 1, TV_AUDIO 1, WEARABLES 3 | INFO |
| 703 | info | customer registrations, first and last |  | 2018-06-25 .. 2026-10-31 | INFO |
| 704 | info | change_log rows for these tables (captured for the pipeline) |  | 250 | INFO |

## Script 12: opening inventory, revision 2 (23 rows)

The opening checks (1xx, 4xx, 5xx) read the `opening_balance` movements, which sales never change, so they pass before and after the load. Checks 202 and 203 are INFO counts of the pilot reservations and orders. Check 601 shows the stock on hand after the sales: 1,441 units in total.

| Check | Type | Object | Expected | Actual | Status |
|---|---|---|---|---|---|
| 101 | count | inventory_balances (one listing per product and location) | 320 | 320 | PASS |
| 102 | count | opening_balance movements | 288 | 288 | PASS |
| 103 | count | opening units | 1460 | 1460 | PASS |
| 104 | count | product and location pairs without a listing | 0 | 0 | PASS |
| 105 | count | listings with more than one opening balance | 0 | 0 | PASS |
| 201 | nothing else yet | stock movements other than opening_balance and sale | 0 | 0 | PASS |
| 202 | info | inventory_reservations, if orders are loaded (checked by script 13) |  | 23 | INFO |
| 203 | info | orders, if loaded (checked by script 13) |  | 20 | INFO |
| 301 | ledger | balances where on hand \<\> sum of movements | 0 | 0 | PASS |
| 302 | ledger | balances where reserved \<\> active reservations | 0 | 0 | PASS |
| 303 | ledger | negative stock or more reserved than on hand | 0 | 0 | PASS |
| 401 | opening | opening balances not dated 00:00 local time on the opening day | 0 | 0 | PASS |
| 402 | opening | opening balances without the stock count reference | 0 | 0 | PASS |
| 403 | opening | opening stock for products launched after the opening day | 0 | 0 | PASS |
| 404 | opening | products on sale on the opening day without opening stock at an open location | 0 | 0 | PASS |
| 405 | opening | opening balances at a location that had not opened yet | 0 | 0 | PASS |
| 501 | stock | opening balances not above the listing reorder point | 0 | 0 | PASS |
| 502 | stock | warehouses opening with less of a product than a store of the same market | 0 | 0 | PASS |
| 503 | stock | warehouses above their storage capacity | 0 | 0 | PASS |
| 601 | info | units on hand now, by market and location type |  | DK store 135, DK warehouse 191, FI store 100, FI warehouse 152, NO store 139, NO warehouse 178, SE store 196, SE warehouse 350 | INFO |
| 602 | info | opening stock value at unit cost, by market (local currency) |  | DK 388172.60 DKK, FI 43079.84 EUR, NO 627841.19 NOK, SE 897407.45 SEK | INFO |
| 603 | info | listings without opening stock (product not on sale yet) |  | 32 (CAM-000007, SMH-000011) | INFO |
| 604 | info | change_log rows for the inventory tables (captured for the pipeline) |  | 976 | INFO |

## Additional read-only cross-checks (not part of the scripts)

Run in the same read-only way at 01:34, after the scripts:

| Check | Result |
|---|---|
| Orders in the database compared with the plan (`output/transactions/pilot/orders.csv`) | 20 of 20 match on status, cancellation reason, order total, channel and fulfilment location |
| `python/run_transactions.py --plan` (read-only) | 20 orders and 100 units of work present, 0 to write, conflicts 0, blocking 0 |
| Exact row counts | orders 20, order_items 26, payment_attempts 21, inventory_reservations 23, inventory_movements 305 (288 opening + 17 sale), inventory_balances 320, change_log 1,426 (1,146 + 280) |

Final state summary:

| Metric | Value |
|---|---|
| orders by status | cancelled 4, fulfilled 14, payment_failed 2 |
| cancelled orders by reason | customer_request 1, out_of_stock 2, reservation_expired 1 |
| orders by channel | in_store 10, online 10 |
| payment attempts by status | captured 14, failed 7 |
| inventory reservations by status | consumed 17, expired 2, released 4 |
| inventory movements by type (rows / units) | opening_balance 288 rows / 1460 units, sale 17 rows / -19 units |
| units on hand (sum of inventory_balances.quantity_on_hand) | 1441 |
| units reserved (sum of inventory_balances.quantity_reserved) | 0 |
| opening units + sale movements = ledger total | 1460 + (-19) = 1441 |
| listings with 0 on hand | 33 |
| SPH-000014 at SE-STO-03 (on hand / reserved) | 0 / 0 |
| TAB-000003 at DK-WH-01 (on hand / reserved) | 4 / 0 |
| change_log rows (total) | 1426 |
| meta.schema_migrations rows | 7 |

## Errors

None. Every psql run exited with code 0, with no error output, and no check returned FAIL.
