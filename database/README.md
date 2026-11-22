# NordicCart OLTP database

PostgreSQL source database for the NordicCart pipeline. This folder holds the approved design, the one-time setup script and the versioned migrations.

```
database/
├── design/
│   ├── nordiccart_oltp_rev4.dbml      revision 4 = revision 3 + the V009 objects (stage S4)
│   ├── nordiccart_oltp_rev3.dbml      approved design (revision 3), unchanged
│   └── archive/
│       └── nordiccart_oltp.dbml       earlier revision, kept for history only
├── setup/
│   └── 00_create_database.sql         creates nordiccart_oltp (run once, outside a transaction)
├── migrations/
│   ├── V001__migration_history_and_extensions.sql
│   ├── V002__change_capture_and_shared_trigger_functions.sql
│   ├── V003__reference_data_currencies_countries_exchange_rates.sql
│   ├── V004__catalogue_and_customers.sql
│   ├── V005__stock_locations_stores_warehouses.sql
│   ├── V006__orders_order_items_payment_attempts.sql
│   ├── V007__inventory_balances_reservations_movements.sql
│   ├── V008__security_roles_and_privileges.sql
│   └── V009__activity_control_and_purchase_orders.sql
├── rollback/
│   ├── R008__undo_security_roles_and_privileges.sql   in-place rollback of V008
│   └── R009__undo_activity_control_and_purchase_orders.sql   in-place rollback of V009
└── security/
    └── V008_reapply_database_privileges.sql           after a restore or template copy of a V008 database
```

How to run everything in Valentina Studio: [docs/phase2_valentina_studio_execution_guide.md](../docs/phase2_valentina_studio_execution_guide.md).

## A note on the word "phase"

The project calls this work **Phase 2** (database implementation). The DBML uses its own build phases for the schema: DBML Phase 1 is the MVP table set (orders, reservations, payments, fulfilment), DBML Phase 2 adds promotions, Phase 3 shipments, Phase 4 returns and refunds. To avoid confusion, these files call the DBML ones "DBML Phase 1" and so on.

## Migration conventions and history

| Rule | Why |
|---|---|
| File name `V<3 digits>__<description>.sql`, applied in version order | Sorts correctly and matches the Flyway convention, so a migration tool can take over later |
| Each file is one transaction (`BEGIN` ... `COMMIT`) | A failure anywhere rolls back the whole file; the database is never half-migrated |
| Each file starts with `meta.assert_migration_can_run('<version>', '<previous>')` | Stops a second run and stops a run out of order |
| Each file ends by inserting its row into `meta.schema_migrations` | The database records what has been applied, by whom and when |
| Forward-only; an applied file is never edited | A change to an applied migration becomes a new migration. Functions are changed with `CREATE OR REPLACE FUNCTION` in a new file |
| No data in migrations | Reference data and synthetic data are loaded in separate, later steps |

Check what a database has applied:

```sql
SELECT version, script_name, applied_at, applied_by
  FROM meta.schema_migrations
 ORDER BY version;
```

| Version | File | Status in the project |
|---|---|---|
| 001 to 005 | foundational tables | applied to your database and verified |
| 006 | `V006__orders_order_items_payment_attempts.sql` | revision 2, applied to your database and verified |
| 007 | `V007__inventory_balances_reservations_movements.sql` | applied to your database and verified |
| 008 | `V008__security_roles_and_privileges.sql` | applied to your database and verified on 2026-11-18 (Phase 3 step 5, S3): `docs/test_reports/2026-11-18_s3_v008_real_deployment_report.md`. Rollback: `docs/phase3_v008_deployment_and_rollback_guide.md` |
| 009 | `V009__activity_control_and_purchase_orders.sql` | implemented and tested in the sandbox (Phase 3 step 5, S4); **not applied** to your database. Deployment, pilot document backfill and rollback: `docs/phase3_v009_deployment_and_rollback_guide.md` |

## What each migration builds

| Migration | Objects | Depends on |
|---|---|---|
| V001 | `btree_gist` extension, schema `meta`, `meta.schema_migrations`, `meta.assert_migration_can_run()`; checks PostgreSQL 13+, UTF8 and database name | setup script |
| V002 | enum `change_operation`; tables `change_log`, `change_log_checkpoints`; functions `fn_set_updated_at()`, `fn_reject_modification()`, `fn_capture_change()` | V001 |
| V003 | tables `currencies`, `countries`, `exchange_rates` | V002 |
| V004 | enum `sales_channel`; tables `product_categories`, `products`, `product_prices`, `customers` | V003 |
| V005 | enum `stock_location_type`; tables `stock_locations`, `stores`, `warehouses`; function `fn_check_stock_location_subtype()` | V004 |
| V006 | enums `order_status`, `payment_method`, `payment_status`; tables `orders`, `order_items`, `payment_attempts`; functions `fn_orders_guard()`, `fn_order_items_guard()`, `fn_payment_attempts_guard()`, `fn_reject_history_truncate()`, `fn_assert_order_consistent()`, `fn_check_order_consistency()` | V005 |
| V007 | enums `reservation_status`, `movement_type`; tables `inventory_balances`, `inventory_reservations`, `inventory_movements`; 13 functions (balance maintenance, guards, commit-time order check, unit-of-work functions `fn_reserve_order_stock()`, `fn_close_order_reservations()`, `fn_fulfil_order_stock()`); one new trigger each on `orders` and `payment_attempts` | V006 |
| V008 | no table, column, constraint, trigger or function body; 7 cluster roles (`nordiccart_owner`, `_writer`, `_reader`, `_loader`, `_scheduler`, `_analyst`, `_extractor`) with settings and connection limits; ownership of every object to `nordiccart_owner`; `fn_inventory_apply_movement()`, `fn_inventory_apply_reservation()` and `fn_capture_change()` SECURITY DEFINER with a fixed search path; least privilege including column-level UPDATE; nothing for PUBLIC. Must run as a superuser; PostgreSQL 16+. Matrix: `docs/phase3_v008_security_privilege_matrix.md` | V007 |
| V009 | Run as postgres with `SET ROLE nordiccart_owner`.<br>- **Enums:** `purchase_order_status`, `purchase_order_reason`, `reorder_point_revision_kind`, `meta.activity_run_mode`, `meta.activity_run_status`.<br>- **Tables:** `purchase_orders`, `purchase_order_lines` (received quantities and statuses derived from the ledger's `<PO>/DN<n>` receipts), `reorder_point_revisions` (insert-only, chained, applied at commit), `meta.activity_runs` (contiguous non-overlapping windows chained by a unique predecessor link, immutable fingerprints, verified only with a checkpoint), `meta.activity_checkpoints` (insert-only).<br>- **Functions:** 14 (two SECURITY DEFINER).<br>- **Trigger on `inventory_movements`:** checks purchase receipts against their order.<br>- **Privileges:** grants on the V008 roles.<br>No V001–V008 object or row changed. DBML revision 4 | V008 |

Foreign key dependency order (parent before child):

```
V003 to V005
currencies ──┬─> countries ──┬─> customers
             │               ├─> stock_locations ──┬─> stores
             │               │                     └─> warehouses
             │               └─> product_prices
             ├─> exchange_rates (base and quote)
             └─> product_prices
product_categories ─> products ─> product_prices

V006
customers, stores, stock_locations, countries, currencies ─> orders
orders, products ─> order_items
orders ─> payment_attempts

V007
stock_locations, products ─> inventory_balances
inventory_balances (location, product), order_items ─> inventory_reservations
inventory_balances (location, product), order_items ─> inventory_movements
inventory_movements (transfer_out) ─> inventory_movements (transfer_in)

change_log, change_log_checkpoints: no foreign keys
```

## V006: orders and payments

### How the units of work write

The database checks; the application computes. Every amount (line totals, VAT, `items_total`, `shipping_vat_amount`, `order_total`) is computed by the application with `Decimal` and `ROUND_HALF_UP` and written once. The database rejects anything that does not add up.

| Unit of work | What the application does | What the database enforces |
|---|---|---|
| U1 Place | INSERT `orders` (status `placed`) and its `order_items` in one transaction | row CHECKs at once; currency of the country; fulfilment location in the order country (online: a warehouse that ships online orders); at COMMIT: at least one line, `items_total` = sum of lines, one VAT rate per order, shipping VAT by formula |
| U2 Reserve | UPDATE the order to `stock_reserved` (from V007: through `fn_reserve_order_stock`, see the V007 section) | allowed status move; lines and amounts frozen from now on |
| U3a Pay | INSERT `payment_attempts` as `pending` | order must be `stock_reserved`; amount = `order_total`; cash only in store; at most one live attempt |
| U3 retry | mark the attempt `failed`, INSERT the next attempt number as `pending` | `pending -> failed` needs `failed_at` and `failure_reason`; the next live attempt is allowed only after the failure |
| U3c Confirm | lock the order, set the attempt `authorized` then `captured`, then set the order `confirmed` | capture only while the order is `stock_reserved`; at COMMIT: confirmed or fulfilled has exactly one captured attempt, and a captured attempt means the order is confirmed |
| U3 final failure | after the last failed attempt set the order `payment_failed` | at COMMIT: at least one failed attempt and none pending or authorized |
| U4 Expire | in one transaction: set the order `cancelled` (reservation_expired) and void every authorized attempt | at COMMIT: a cancelled order has no authorized attempt; a pending one may remain, and when it later authorizes, that transaction must void it; capture on a cancelled order refused |
| U5 Fulfil | set the order `fulfilled` | `confirmed -> fulfilled` only; `fulfilled_at` not before `confirmed_at`; the order is then final |

Order of statements in U3c matters: **capture first, then confirm**. The capture guard requires the order to still be `stock_reserved`, and the commit-time check requires the confirm before COMMIT. Lock order everywhere: the orders row first. The guard triggers also lock the parent order row (`FOR UPDATE`), so concurrent writers to the same order queue behind each other.

### Design items implemented in V006

**[DBML], all of them for the three tables:** columns, types, NOT NULL, defaults, primary keys, the UNIQUE rules, every named FK including the composite in-store fulfilment FK (`orders.(store_id, fulfilment_location_id) -> stores.(store_id, stock_location_id)`, MATCH SIMPLE so online orders skip it), every named CHECK and every index. Checked against the `dbml2sql` output: identical apart from the implementation choices below and `fk_order_items_promotion`, which waits for the promotions table (DBML Phase 2).

**[PG] items from the design:**

| Item from the design | How |
|---|---|
| Partial unique index: one live attempt per order | `uq_payment_attempts_one_live_per_order` on `(order_id) WHERE status IN ('pending', 'authorized', 'captured')` |
| Order status transition matrix | `fn_orders_guard()`: placed -> stock_reserved or cancelled; stock_reserved -> confirmed, payment_failed or cancelled; confirmed -> fulfilled; everything else refused |
| Deferred order total check | `fn_assert_order_consistent()` via three deferred constraint triggers: `items_total` = SUM(`line_total`) at COMMIT |
| Order lines immutable after placed | `fn_order_items_guard()` locks the order and refuses INSERT, UPDATE and DELETE unless it is `placed` |
| Line `vat_rate` equals the order country rate | `fn_order_items_guard()`, against `countries.vat_rate` |
| Payment `amount` = `order_total`; cash only in store; transitions pending -> authorized or failed, authorized -> captured or voided; capture only while stock_reserved with the order row locked | `fn_payment_attempts_guard()` |
| `updated_at` maintenance and change_log capture | as on every other business table |

### V006 additions: rules from the business rules, DBML notes and pre-deployment review

The [PG] list in the DBML does not name these one by one. Each one follows from an approved rule or from the pre-deployment review (revision 2), and none of them changes a table or column. Remove any you disagree with by asking for a later migration that replaces the function.

**Lifecycle, totals and payment validation**

| Rule | Source | Rule name raised |
|---|---|---|
| An order is inserted as `placed`; an attempt is inserted as `pending` | DBML lifecycle (U1, U3a) | `orders_starts_placed`, `payment_attempts_starts_pending` |
| An order has at least one line at COMMIT | Business Rules 1.1 "selects one or more products" | `orders_has_lines` |
| `orders.currency_code` is the currency of the order country | Business Rules 2 "currency of the country in which the sale occurs" | `orders_currency_matches_country` |
| One VAT rate per order and `shipping_vat_amount = round(shipping_fee * rate / (1 + rate), 2)` at COMMIT | DBML note on `shipping_vat_amount` ("at the line VAT rate") | `orders_single_vat_rate`, `orders_shipping_vat` |
| Amounts (`items_total`, `shipping_fee`, `shipping_vat_amount`, `order_total`) frozen once the order leaves `placed` | Lines are frozen (DBML), so the amount owed must be too, otherwise a payment could stop matching | `orders_terms_frozen` |
| A new attempt needs a `stock_reserved` order | U3: retries happen "while reservations are active" | `payment_attempts_order_stock_reserved` |
| `confirmed` or `fulfilled` if and only if exactly one captured attempt (COMMIT) | Business Rules 1.6 "successful payment confirms the order" | `orders_confirmed_has_capture`, `orders_capture_confirms_order` |
| `payment_failed` needs a failed attempt and none pending or authorized (COMMIT) | Business Rules 1.5, U3 "after the last failure" | `orders_payment_failed_state` |
| `order_items.promotion_id` must be NULL | DBML MVP note "stays NULL"; DBML Phase 2 replaces this with "discount equals the promotion percentage" | `order_items_promotion_mvp` |

**Historical integrity (revision 2)**

| Rule | Why | Rule name raised |
|---|---|---|
| Orders are never deleted; cancel instead | an order is a business record | `orders_no_delete` |
| `order_id`, `order_number`, `customer_id`, `placed_at`, `created_at` never change | identity of the purchase: who bought, when, under which number | `orders_identity_immutable` |
| `channel`, `store_id`, `fulfilment_location_id`, `country_code`, `currency_code` never change, even while placed | VAT, currency and stock all depend on them; a changed country would leave line VAT rates wrong | `orders_context_immutable` |
| `confirmed_at`, `fulfilled_at`, `cancelled_at` are NULL until the order reaches their status | the CHECKs require them there; this is the reverse direction | `orders_event_timestamp_state` |
| ... and written once | an event happened at one time | `orders_event_timestamp_changed` |
| ... and in time order: `confirmed_at` and `cancelled_at` not before `placed_at`, `fulfilled_at` not before `confirmed_at` | business events are chronological | `orders_event_timestamp_order` |
| `fulfilled`, `payment_failed` and `cancelled` orders are final: no column changes at all | completed history | `orders_final_immutable` |
| `order_item_id`, `order_id`, `created_at` of a line never change (a line cannot move to another order) | line identity | `order_items_identity_immutable` |
| Payment attempts are never deleted | payment records are history | `payment_attempts_no_delete` |
| `order_id`, `attempt_number`, `payment_method`, `payment_provider`, `amount`, `attempted_at`, `created_at` of an attempt never change | the terms the customer paid on | `payment_attempts_terms_immutable` |
| `authorized_at`, `captured_at`, `failed_at`, `failure_reason` are written only by their own status change (and are empty on insert) | the outcome comes from the provider, once | `payment_attempts_outcome_by_transition` |
| `provider_reference` is written once | provider transaction id | `payment_attempts_provider_reference_write_once` |
| `captured`, `failed` and `voided` attempts are final: no column changes at all | completed payment records | `payment_attempts_final_immutable` |
| Payment times in order: `placed_at <= attempted_at <= authorized_at <= captured_at`, `failed_at >= attempted_at` | business events are chronological | `payment_attempts_timestamp_order` |
| `TRUNCATE` of `orders`, `order_items` and `payment_attempts` refused (SQLSTATE 23000) | TRUNCATE skips row triggers, so it gets its own guard | `history_no_truncate` |

**Fulfilment location (revision 2)**

| Rule | Why | Rule name raised |
|---|---|---|
| The fulfilment location is in the order country (online and in store) | stock, VAT and currency belong to one market | `orders_fulfilment_country` |
| An online order is fulfilled from a warehouse with `ships_online_orders = true` | DBML: "Online orders are fulfilled from the warehouse in their country" (was [APP]) | `orders_online_fulfilment_warehouse` |

In-store orders were already tied to the selling store's own location by `fk_orders_store_fulfilment`. Both checks run at insert; the context-immutable rule keeps them true afterwards.

**Cancellation and payment state (revision 2, checked at COMMIT)**

| Rule | Why | Rule name raised |
|---|---|---|
| A cancelled order has no `authorized` attempt | U4: authorizations are voided in the same transaction as the cancellation. Before revision 2 a cancelled order could keep an open authorization | `orders_cancelled_open_authorization` |
| A `pending` attempt may remain on a cancelled order | the provider outcome is unknown (timeout); it is reconciled later, and an authorization that arrives late must be voided in that same transaction | (allowed) |
| `cancellation_reason` is `out_of_stock`, `reservation_expired` or `customer_request` when cancelled, and empty otherwise (also for `payment_failed`) | DBML note lists the three reasons | `orders_cancellation_reason` |

Together with the existing rules, the commit-time states are: `confirmed` or `fulfilled` has exactly one captured attempt; `payment_failed` has at least one failed attempt and none pending or authorized; `cancelled` has no captured and no authorized attempt; `placed` and `stock_reserved` have no captured attempt.

Every guard raises SQLSTATE `23514` (check_violation) with the rule name in the error's constraint field (the TRUNCATE guard raises `23000`), so the application can tell rules apart and, per the design's retry rules, never retries them.

### Rules that waited for the inventory tables

The inventory half of the order and payment rules (one active reservation per line for `stock_reserved` and `confirmed`, capture only while every reservation is active, release or expiry on `payment_failed` and `cancelled`, consumed reservations and one sale movement per line for `fulfilled`) is implemented in V007. See the V007 section. No V006 table or function was changed for it.

### Rules that wait for later DBML phases or stay in the application

| Rule | When |
|---|---|
| `confirmed -> cancelled` (cancellation after payment) needs refunds | DBML Phase 4; the transition is refused until then, as the design says |
| `fk_order_items_promotion`, discount equals the promotion percentage, promotion eligibility | DBML Phase 2 (eligibility stays [APP]) |
| Maximum 3 payment attempts per order | [APP] in the design; the database allows more |
| Provider calls outside transactions, idempotency keys, timeout reconciliation | [APP] |
| Choosing which warehouse fulfils an online order (the database only checks that it is a shipping warehouse in the right country) | [APP] in the design |
| Delivering online orders within their country | [APP]; there is no shipping country column |

## V007: inventory

### How stock works

| Quantity | Definition | Written by |
|---|---|---|
| `quantity_on_hand` | SUM(`inventory_movements.quantity_change`) for the location and product | AFTER INSERT trigger on `inventory_movements` |
| `quantity_reserved` | SUM(`quantity`) of `active` reservations for the location and product | AFTER INSERT or UPDATE OF status trigger on `inventory_reservations` |
| available | `quantity_on_hand - quantity_reserved` | derived, not stored |

- A balance row means "this product is listed at this location". It is inserted with both quantities at 0 (`inventory_balances_start_empty`); stock enters through an `opening_balance` or `purchase_receipt` movement. A product without a balance row at the fulfilment location counts as not listed, and U2 reports it out of stock.
- Nobody writes the two quantity columns directly (`inventory_balances_quantities_by_ledger`), so on_hand = SUM(movements) and reserved = SUM(active reservations) always hold. To correct a count, insert a `stock_adjustment` movement.
- The CHECKs `on_hand >= 0`, `reserved >= 0` and `reserved <= on_hand` are the final guard against negative stock and overselling. Concurrent writers to one balance row queue on its row lock and the CHECK is evaluated on the latest version, so the loser gets 23514 instead of overselling.
- Reservations never move stock. Stock leaves on hand exactly once, through the `sale` movement in U5.
- PostgreSQL evaluates CHECKs in name order. A negative on-hand therefore reports `ck_inventory_balances_no_oversell`, not `ck_inventory_balances_on_hand`. Treat both names as "not enough stock".

### How the units of work write

The three functions are reference implementations of the design's units of work. They run inside the caller's transaction, take every lock in the design order and are idempotent. The application may also write the rows itself: the triggers and commit-time checks enforce the same rules either way.

| Unit of work | Call (inside one transaction) | Result |
|---|---|---|
| U2 Reserve | `SELECT fn_reserve_order_stock(order_id, reserved_at, expires_at)`: locks the order (must be `placed`), locks its balance rows in lock order, checks every line, inserts one `active` reservation per line, sets the order `stock_reserved` | `reserved`; `out_of_stock` (nothing changed: per the design, roll back, then cancel the order with reason `out_of_stock` in a new transaction); `already_reserved` (a retry after success; nothing changed) |
| U3c Capture and confirm | as in V006: lock the order, capture, then confirm | V007 refuses the capture unless every line holds an active reservation (`payment_attempts_capture_requires_active_reservations`). The design's answer is to void the attempt. |
| U3 final failure | `SELECT fn_close_order_reservations(order_id, 'released', closed_at)`, then set the order `payment_failed` | number of reservations closed (0 when repeated) |
| U4 Expire | sweeper: lock one due order `FOR UPDATE SKIP LOCKED`; `fn_close_order_reservations(order_id, 'expired', now)`; void authorized attempts; cancel the order (`reservation_expired`) | expiring before `expires_at` is refused (`inventory_reservations_expire_after_due`) |
| Cancellation (`customer_request`) before payment | `fn_close_order_reservations(order_id, 'released', closed_at)`, then cancel | |
| U5 Fulfil | `SELECT fn_fulfil_order_stock(order_id, fulfilled_at)`: locks the order (must be `confirmed`) and its balance rows; per line FIRST consumes the reservation, THEN inserts the `sale` movement (minus the line quantity); sets the order `fulfilled` | `fulfilled`; `already_fulfilled` (a retry after success). If a reservation changed underneath it, SQLSTATE 40001 (retry the unit of work). |
| U7 Receipts, transfers, adjustments, write-offs | INSERT into `inventory_movements` only | a retried insert fails with 23505 on its idempotency index: the step already happened |

U7 idempotency keys: `opening_balance` once per balance; `purchase_receipt`, `stock_adjustment`, `write_off` and `transfer_out` carry the document number in `external_reference`, unique per movement type, location and product; `transfer_in` once per `transfer_out` (`related_movement_id`); `sale` once per order line. Reserved units cannot be transferred out or written off, because `reserved <= on_hand` would fail.

Lock order everywhere (DBML): orders row, then `inventory_balances` rows sorted by `(stock_location_id, product_id)`, then reservation and movement writes, then the `payment_attempts` row. Retry the whole unit of work on 40001, 40P01 and 55P03; never on 23514 or 23503.

### Design items implemented in V007

**[DBML], all of them for the three tables:** both enums, columns, types, NOT NULL, defaults, primary keys, `uq_inventory_balances_location_product`, every named FK including the composite balance FKs `(stock_location_id, product_id)` and the transfer self-reference, all 13 named CHECKs and all 11 indexes. Checked against the `dbml2sql` output: columns identical; differences only in the implementation choices below and `fk_inventory_movements_return_item`, which waits for `return_items` (DBML Phase 4).

**[PG] items from the design:**

| Item from the design | How |
|---|---|
| Partial uniques: one active reservation per line; one sale per line; one return_restock per return item; one transfer_in per transfer_out | `uq_inventory_reservations_one_active_per_line`, `uq_inventory_movements_one_sale_per_line`, `uq_inventory_movements_one_restock_per_return_item`, `uq_inventory_movements_one_transfer_in_per_out` |
| Balance maintenance from movements and reservations | `fn_inventory_apply_movement()`, `fn_inventory_apply_reservation()` |
| Quantities change only through the triggers | `fn_inventory_balances_guard()` until V008 adds the column privileges the design names |
| Reservation consistency: location = order fulfilment location, product and quantity = order line | `fn_inventory_reservations_guard()` (`inventory_reservations_match_order_line`) |
| Reservation transitions: only `active -> consumed, released, expired`; closed rows immutable | same guard (`inventory_reservations_status_transition`, `inventory_reservations_final_immutable`) |
| Insert-only movement ledger | V002 `fn_reject_modification()` on UPDATE and DELETE |
| A sale needs a consumed reservation of the same line, location and quantity | `fn_inventory_movements_guard()` (`inventory_movements_sale_matches_consumed_reservation`) |
| A transfer_in needs a transfer_out of the same product at another location with the opposite quantity | same guard (`inventory_movements_transfer_matches_transfer_out`) |
| Capture only while all reservations are active (the inventory half; V006 has the status half) | new trigger `trg_payment_attempts_inventory_guard` |
| Order states and reservations agree (lifecycle notes and U2 to U5), checked at COMMIT | `fn_assert_order_inventory_consistent()` through three deferred constraint triggers: `placed` holds no reservation (`orders_placed_no_reservations`); `stock_reserved` and `confirmed` hold one active reservation per line (`orders_reservations_per_line`); `fulfilled` has every line consumed and sold once (`orders_fulfilled_stock_issued`); `payment_failed` and `cancelled` hold none (`orders_closed_reservations_released`) |
| Lock order | the unit-of-work functions; the guards also lock the parent order row `FOR UPDATE` |
| `updated_at` maintenance and change_log capture | as on every other business table |

### V007 additions: rules from the business rules, DBML notes and the V007 brief

None of them changes a table or column. Each can be removed by a later migration that replaces the function or drops the index.

| Rule | Why | Rule name raised |
|---|---|---|
| A new balance starts at 0 on hand and 0 reserved | quantities come only from the ledger | `inventory_balances_start_empty` |
| Balance id, location, product and `created_at` never change | a balance row is the listing of one product at one location | `inventory_balances_identity_immutable` |
| Reservations are never deleted; reservation terms (line, location, product, quantity, `reserved_at`, `expires_at`) never change | reservation history | `inventory_reservations_no_delete`, `inventory_reservations_terms_immutable` |
| A reservation is inserted as `active`, only while its order is `placed` | U2 | `inventory_reservations_starts_active`, `inventory_reservations_order_placed` |
| `consumed` needs a `confirmed` order; `released` needs `stock_reserved`, `payment_failed` or `cancelled`; `expired` needs `stock_reserved` or `cancelled` | U5, U3, U4 and cancellation | `inventory_reservations_order_state` |
| Expire only when due (`closed_at >= expires_at`) | U4 sweeper | `inventory_reservations_expire_after_due` |
| Times in order: reservation not before the order; closed not before reserved (consumed not before confirmation); sale not before confirmation; transfer_in not before its transfer_out | business events are chronological | `inventory_reservations_timestamp_order`, `inventory_movements_timestamp_order` |
| A sale only for a `confirmed` order | U5 | `inventory_movements_sale_order_state` |
| `opening_balance` once per balance | a second opening balance would double the stock | index `uq_inventory_movements_one_opening_balance` (23505) |
| `purchase_receipt`, `stock_adjustment`, `write_off` and `transfer_out` need `external_reference`, unique per type, location and product | the design gives these movements no idempotency key; the document number becomes it, so a retried receipt cannot add stock twice | `inventory_movements_document_reference_required`; index `uq_inventory_movements_document_line` (23505) |
| `return_item_id` stays NULL | DBML MVP note; return_restock waits for DBML Phase 4 | `inventory_movements_return_restock_mvp` |
| `TRUNCATE` of the three tables refused (SQLSTATE 23000) | TRUNCATE skips row triggers | `inventory_no_truncate` |
| Unit-of-work functions U2, U3/U4 close, U5 | atomic, lock order, idempotent retries | `fn_fulfil_order_stock` on an order that is not confirmed: `orders_status_transition`; `fn_reserve_order_stock` on an order that is not placed (and not already reserved): `inventory_reservations_order_placed` |

Every rule raises SQLSTATE `23514` with the rule name in the constraint field, as in V005 and V006, except the TRUNCATE guard (`23000`), the idempotency indexes (`23505`) and a wrong status argument to `fn_close_order_reservations` (`22023`).

### Triggers V007 adds to V006 tables

| Table | Trigger | What it does |
|---|---|---|
| `orders` | `trg_orders_inventory_check` (deferred constraint trigger, AFTER UPDATE OF status) | runs the commit-time order-inventory check |
| `payment_attempts` | `trg_payment_attempts_inventory_guard` (BEFORE UPDATE OF status, when captured) | capture needs every line to hold an active reservation |

No V006 table or function changes. Verification script 07 does not know these two triggers and lists them as **WARN** (extra object on a V006 table). That is expected; script 10 checks them.

### V006 behaviour tests after V007 (test file revision 3)

The V006 tests set orders to `stock_reserved` with a plain UPDATE. From V007 that is refused at COMMIT unless every line holds an active reservation, which is exactly the design rule. Revision 3 of `tests/sql/test_v006_orders_and_payments.sql` therefore detects V007: when the inventory tables exist, it gives the test products 100 units of opening stock and reserves, releases, expires and fulfils through the V007 functions; on a V006-only database it behaves as before. Same 119 tests, same expected results. It passes 119/119 at V006 and at V007. Revision 2 of the file fails from test 57 onward at V007, for the reason above.

### Rules that wait for V008, later DBML phases or the application

| Rule | When |
|---|---|
| No UPDATE privilege on the two quantity columns for the application role | V008. Until then the guard recognises the ledger triggers by trigger depth; an UPDATE issued from inside some other trigger would also pass it (the CHECKs still hold, test 143). |
| `return_restock` movements and the `return_item_id` FK | DBML Phase 4 |
| Reservation lifetime (`reserved_at` + 15 minutes online, + 5 minutes in store) | [APP]; the database only requires `expires_at > reserved_at` |
| Scheduling the expiry sweeper | [APP] |
| Retry limits and backoff | [APP] |

## Design items implemented in V001 to V005

**[DBML], all of them for these 12 tables:** columns, types, NOT NULL, defaults, primary keys, every UNIQUE (column and index-block), every named foreign key including the two composite type-checked keys to `stock_locations`, every named CHECK, and every index. Checked against the DDL that the official DBML tool (`@dbml/cli`, `dbml2sql`) generates from `nordiccart_oltp_rev3.dbml`: identical apart from the implementation choices listed below.

**[PG], the ones that belong to these tables:**

| Item from the design | Where |
|---|---|
| `updated_at` maintenance trigger | every table with `updated_at` (V002 to V007) |
| change_log capture on every tracked table | every business table (V003 to V007); `change_log` and `change_log_checkpoints` are not tracked, as designed |
| `change_log` identity with CACHE 1 | V002 |
| insert-only guard on `change_log` (UPDATE and TRUNCATE blocked, DELETE left for the purge step) | V002 |
| insert-only guard on `exchange_rates` (UPDATE, DELETE and TRUNCATE blocked) | V003 |
| EXCLUDE on `product_prices` validity periods (btree_gist) | V004 |
| deferred stock location subtype check | V005 |

## Remaining [PG] items and their planned migrations

| Item | Planned migration |
|---|---|
| column privileges (no UPDATE on balance quantities, no UPDATE or DELETE on ledgers), application and extractor roles, `lock_timeout` and `idle_in_transaction_session_timeout`. The two balance-maintenance functions must then become `SECURITY DEFINER` (see design observation 11) | V008 (next), before any data is generated |
| `promotions` and the `order_items.promotion_id` FK | DBML Phase 2 |
| `shipments` | DBML Phase 3 |
| `returns`, `return_items`, `refunds`, the `inventory_movements.return_item_id` FK, their partial uniques and guards, `confirmed -> cancelled` | DBML Phase 4 |

## Implementation choices (no change to the data model)

These are choices DBML leaves open or that the generator makes by convention. None of them changes a table, column, key or rule in the design.

| Choice | DBML generator output | What the migrations do | Reason |
|---|---|---|---|
| Surrogate keys | `GENERATED BY DEFAULT AS IDENTITY` | `GENERATED ALWAYS AS IDENTITY` | The design says surrogate keys are "never reused". ALWAYS rejects a hand-typed id that could later collide with the sequence. Explicit ids are still possible with `OVERRIDING SYSTEM VALUE` if ever needed. |
| Foreign keys | `DEFERRABLE INITIALLY IMMEDIATE` | not deferrable (PostgreSQL default) | Behaviour is identical unless a session runs `SET CONSTRAINTS ... DEFERRED`. The design never defers a foreign key; its only deferred rules are constraint triggers. |
| Unique rules | inline `UNIQUE` plus `CREATE UNIQUE INDEX` | named `UNIQUE` constraints | Same enforcement and same index. Named constraints are explicit FK targets and give readable error messages. DBML names are kept; unnamed DBML uniques get `uq_<table>_<column>`. |
| Constraint names | auto-generated for PKs | `pk_<table>` | Predictable names for verification and error handling. |
| Migration history | none | schema `meta` | Kept out of `public` so the operational schema contains exactly the DBML tables. |
| Insert-only guard error | not specified | SQLSTATE `23000` | An integrity-class error. The design's retry rules retry only 40001, 40P01 and 55P03, so it is never retried. |
| Business rule errors (V005 to V007 guards and commit-time checks) | not specified | SQLSTATE `23514` with a rule name in the constraint field | The design names 23514 as a business rejection that is never retried; the rule name tells the application which rule failed. |
| Unit-of-work functions (V007) | not specified ([APP] lock order) | `fn_reserve_order_stock`, `fn_close_order_reservations`, `fn_fulfil_order_stock` | One tested implementation of the lock order and the idempotent retries; the application can call them or write the same rows itself. |
| Totals | not specified | the database verifies, the application computes | Matches the design: CHECKs "make the formulas hold row by row" and the deferred trigger checks `items_total` at commit. One writer per value, no hidden recalculation. |
| Database time zone | not specified | `ALTER DATABASE ... SET timezone TO 'UTC'` in the setup script | Matches the design convention that timestamps are UTC. Storage is unaffected either way. |

## Design observations (not changed; for your decision)

Noticed while implementing. The approved design is left as it is.

1. `customers.email` is unique case-sensitively, so `Anna@x.se` and `anna@x.se` can both exist. A unique index on `lower(email)` would close this if wanted.
2. `stores.store_format` is documented as "flagship or standard" but has no CHECK, so any text up to 20 characters is accepted.
3. `product_prices.currency_code` "equals the market currency when the price was set", but nothing enforces it. It is application logic today.
4. `docs/Business_Rules.md.txt` section 4 still describes the earlier seven-table design (`inventory`, `payments`); revision 3 replaced it.
5. `docs/Database_Schema.md.txt` is empty.
6. **VAT and currency "at placed_at".** The design keeps only the current rate in `countries.vat_rate` and the current currency in `countries.currency_code`, with no history table. The V006 guards therefore compare with the values at the moment the line or order is written. The synthetic generator must run in date order and update `countries.vat_rate` on the date a rate changes (for example Finland: 24 % to 25.5 % on 1 September 2024) before it writes later orders. A VAT history table would remove this dependency; that would be a design change.
7. `order_items.unit_price` and `unit_cost` are "copied from product_prices", but no rule checks that they match the price valid at `placed_at`. It is application logic in the design.
8. Resolved in V006 revision 2: `cancellation_reason` is required and limited to the three documented values when an order is cancelled (guard trigger, not a CHECK, so the table is unchanged).
9. Resolved in V006 revision 2: business timestamps on orders and payment attempts must be in time order.
10. `payment_attempts.amount > 0` with `amount = order_total` means an order whose total is 0 (all free items, no shipping) can never be paid or confirmed. Harmless for a retailer that never sells at 0, but worth knowing.
11. **V008 privileges and the balance triggers.** The design removes UPDATE on the quantity columns from the application role. Trigger functions run with the privileges of the role that fired them, so once that privilege is gone, `fn_inventory_apply_movement()` and `fn_inventory_apply_reservation()` must be `SECURITY DEFINER` (owned by the schema owner, with a fixed `search_path`), or every movement and reservation by the application role will fail. V008 has to include this.
12. Until V008, the quantity guard recognises the ledger triggers by trigger depth. An UPDATE issued from inside some other trigger also passes it. The CHECKs still stop negative stock and overselling (test 143 proves it), but the ledger identity on_hand = SUM(movements) could be broken that way by the database owner.
13. A `transfer_out` without its `transfer_in` is allowed and means stock in transit. Nothing checks that it arrives, or by when.
14. The `external_reference` requirement and `opening_balance` once per balance are V007 additions (see the additions table). The synthetic generator must give every receipt, adjustment, write-off and transfer_out a document number, unique per type, location and product.
15. The reservation status and the order's `cancellation_reason` are not coupled: an order cancelled as `reservation_expired` may have released (not expired) reservations, and the reverse. The design does not say they must match.
16. `ck_inventory_balances_on_hand` can never be the reported error while `reserved >= 0`, because `ck_inventory_balances_no_oversell` covers the same case and sorts first (see "How stock works"). It stays because the design names it.
17. Every reservation and movement also updates its balance row, so `change_log` receives two rows per stock event (the event and the balance). Expected by the capture design; relevant for pipeline volume.
18. Rows loaded by the Phase 3 seeder get `created_at` = load time. Business dates are `registered_at`, `launched_on`, `opened_on` and `valid_from`; reports must use those, not `created_at`.
19. `stock_locations` has `opened_on` and `is_active` but no closing date, so a store closure cannot be dated. Market launch dates exist only in the generator's reference data.

## Checking a database

Run, in order, after the migrations:

| Order | File | Covers |
|---|---|---|
| 1 | `sql/verification/01_verify_environment_and_migrations.sql` | environment, V001 to V005 history (V006 shows as INFO) |
| 2 | `sql/verification/02_verify_tables_and_columns.sql` | V001 to V005 tables and columns |
| 3 | `sql/verification/03_verify_keys_and_constraints.sql` | V001 to V005 constraints |
| 4 | `sql/verification/04_verify_indexes_triggers_and_types.sql` | V001 to V005 enums, indexes, triggers, functions |
| 5 | `sql/verification/05_verify_v006_tables_and_columns.sql` | V006 history, tables and columns |
| 6 | `sql/verification/06_verify_v006_keys_and_constraints.sql` | V006 constraints |
| 7 | `sql/verification/07_verify_v006_indexes_triggers_and_types.sql` | V006 enums, indexes (including the partial unique), 15 triggers, 6 functions. After V007: 2 expected WARN rows for the V007 triggers on `orders` and `payment_attempts` |
| 8 | `sql/verification/08_verify_v007_tables_and_columns.sql` | V007 history, tables and columns |
| 9 | `sql/verification/09_verify_v007_keys_and_constraints.sql` | V007 constraints |
| 10 | `sql/verification/10_verify_v007_indexes_triggers_and_types.sql` | V007 enums, indexes (including the six partial uniques), 18 triggers, 13 functions, identity |
| 11 | `tests/sql/test_foundation_constraints.sql` | 44 behaviour tests, V002 to V005 |
| 12 | `tests/sql/test_v006_orders_and_payments.sql` | 119 behaviour tests, V006 (revision 3 works at V006 and V007) |
| 13 | `tests/sql/test_v007_inventory.sql` | 105 behaviour tests, V007 |
| optional | `tests/python/test_v007_concurrency.py` | 11 multi-session scenarios in its own temporary database (see `tests/README.md`) |

All verification scripts are read-only. The SQL test scripts write only inside a block that they always roll back. The Python test never connects to `nordiccart_oltp`.

Latest sandbox result: `docs/test_reports/2026-11-16_v007_sandbox_test_report.md`.
