-- =============================================================================
-- NordicCart OLTP | Behaviour tests for V007: inventory_balances,
--                  inventory_reservations, inventory_movements
-- -----------------------------------------------------------------------------
-- Purpose     : prove the inventory rules accept good data and reject bad data
--               with the expected SQLSTATE and rule, including the order rules
--               checked at COMMIT, the unit-of-work functions (U2, U3/U4 close,
--               U5), idempotent retries and failure recovery.
-- Connect to  : nordiccart_oltp, as the database owner
-- Run how     : run the whole file. Result: one table with status per test.
-- Expected    : every row has status PASS, and failed_tests = 0.
-- Concurrency : true multi-session races are in tests/python/test_v007_concurrency.py
--               (this file runs in one session).
-- -----------------------------------------------------------------------------
-- Safety (same harness as the V006 tests)
--   * All test rows are written inside one PL/pgSQL block that always ends by
--     raising an internal exception, which rolls the whole block back. Nothing
--     is committed. Test 199 checks this.
--   * Test codes cannot collide with real data: currency XTS, country XA,
--     order numbers NC-TEST-*, SKUs NC-TEST-SKU-*, locations XA-*, documents NC-TEST-*.
--   * The two TRUNCATE tests (41, 72) run inside the sandbox and are refused
--     by their guards; even without the guards they would be rolled back.
--   * Session-only objects: temp table nc_test_results and pg_temp helper
--     functions, gone when the connection closes. Test 143 also creates a temp
--     table with a pg_temp trigger function inside the sandbox; they are
--     rolled back with it.
--   * Side effect: identity sequences are not transactional, so each run uses
--     up some surrogate key numbers (gaps in IDs).
-- -----------------------------------------------------------------------------
-- Stock in the scenario (test location XA-WH-T1 unless stated)
--   P1 NC-TEST-SKU-1: opening 10, receipt +5, transfer in +2, adjustment -1 = 16
--   P2 NC-TEST-SKU-2: opening 5        P3 NC-TEST-SKU-3: not listed at XA-WH-T1
--   XA-WH-T2 P1: opening 4, transfer out -2 = 2      XA-STO-T1 P2: opening 3
-- Orders: O1 online P1 x2 + P2 x1 (reserve, pay, fulfil); O2 in store P2 x1
--   (full flow); O3 online P1 x1 (payment fails, release); O4 online P3 x1
--   (not listed: out of stock); O5 online P1 x1 (expiry); O7 online P2 x6
--   (oversell: out of stock); O8 online P1 x1 + P2 x10 (failure recovery).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Session-only scaffolding
-- -----------------------------------------------------------------------------
CREATE TEMP TABLE IF NOT EXISTS nc_test_results (
    test_no    integer,
    area       text,
    test_name  text,
    expected   text,
    actual     text
);

TRUNCATE nc_test_results;

-- Runs one or more SQL statements; returns '00000' on success, otherwise the
-- SQLSTATE of the error followed by the name of the constraint or rule that
-- raised it. Each call is a subtransaction, so a failed step leaves nothing behind.
CREATE OR REPLACE FUNCTION pg_temp.nc_try(p_sql text)
RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_state       text;
    v_constraint  text;
BEGIN
    EXECUTE p_sql;
    RETURN '00000';
EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS v_state = RETURNED_SQLSTATE,
                            v_constraint = CONSTRAINT_NAME;
    RETURN v_state || coalesce(' ' || nullif(v_constraint, ''), '');
END
$fn$;

-- Evaluates a boolean query; returns 'true', 'false' or the SQLSTATE of an error.
-- Side effects of the query (for example a unit-of-work function) are kept.
CREATE OR REPLACE FUNCTION pg_temp.nc_check(p_sql text)
RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_result      boolean;
    v_state       text;
    v_constraint  text;
BEGIN
    EXECUTE p_sql INTO v_result;
    RETURN coalesce(v_result::text, 'null');
EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS v_state = RETURNED_SQLSTATE,
                            v_constraint = CONSTRAINT_NAME;
    RETURN v_state || coalesce(' ' || nullif(v_constraint, ''), '');
END
$fn$;

-- Lookups by business key, to keep the test steps readable.
CREATE OR REPLACE FUNCTION pg_temp.nc_order(p_order_number text) RETURNS bigint
LANGUAGE sql STABLE AS $fn$ SELECT order_id FROM public.orders WHERE order_number = p_order_number $fn$;

CREATE OR REPLACE FUNCTION pg_temp.nc_item(p_order_number text, p_line integer) RETURNS bigint
LANGUAGE sql STABLE AS $fn$
    SELECT i.order_item_id FROM public.order_items AS i JOIN public.orders AS o USING (order_id)
     WHERE o.order_number = p_order_number AND i.line_number = p_line $fn$;

CREATE OR REPLACE FUNCTION pg_temp.nc_loc(p_code text) RETURNS bigint
LANGUAGE sql STABLE AS $fn$ SELECT stock_location_id FROM public.stock_locations WHERE location_code = p_code $fn$;

CREATE OR REPLACE FUNCTION pg_temp.nc_prod(p_sku text) RETURNS bigint
LANGUAGE sql STABLE AS $fn$ SELECT product_id FROM public.products WHERE sku = p_sku $fn$;

CREATE OR REPLACE FUNCTION pg_temp.nc_stock(p_code text, p_sku text) RETURNS text
LANGUAGE sql STABLE AS $fn$
    SELECT b.quantity_on_hand || '/' || b.quantity_reserved
      FROM public.inventory_balances AS b
     WHERE b.stock_location_id = pg_temp.nc_loc(p_code) AND b.product_id = pg_temp.nc_prod(p_sku) $fn$;

-- -----------------------------------------------------------------------------
-- Tests (run in order inside a rolled-back sandbox)
-- expected: '00000' = step succeeds; '<SQLSTATE> <rule>' = step is rejected
--           with that error by that constraint, guard or commit-time rule;
--           'true' = the check query returns true.
-- nc_stock(location, sku) returns 'on_hand/reserved'.
-- -----------------------------------------------------------------------------
DO $tests$
DECLARE
    r        record;
    v_actual text;
    v_log    jsonb := '[]'::jsonb;
BEGIN
    PERFORM set_config('search_path', 'public', true);

    BEGIN  -- sandbox: everything in here is rolled back at the end
        FOR r IN
            SELECT *
              FROM (VALUES
    -- ===================================================================== setup
    (1, 'setup', 'Reference data, catalogue (P1, P2, P3) and customer C1', '00000',
     $q$INSERT INTO currencies (currency_code, currency_name) VALUES ('XTS', 'Test currency');
        INSERT INTO countries (country_code, country_name, currency_code, vat_rate, time_zone)
        VALUES ('XA', 'Testland A', 'XTS', 0.2500, 'Europe/Stockholm');
        INSERT INTO product_categories (category_code, category_name) VALUES ('NC_TEST_CAT', 'NordicCart test category');
        INSERT INTO products (sku, product_name, brand, category_id)
        SELECT v.sku, v.name, 'TestBrand', c.category_id
          FROM product_categories AS c
         CROSS JOIN (VALUES ('NC-TEST-SKU-1', 'Test laptop'), ('NC-TEST-SKU-2', 'Test headphones'),
                            ('NC-TEST-SKU-3', 'Test cable')) AS v(sku, name)
         WHERE c.category_code = 'NC_TEST_CAT';
        INSERT INTO customers (customer_number, first_name, last_name, email, country_code, signup_channel, registered_at)
        VALUES ('NC-TEST-C1', 'Åsa', 'Testsson', 'nc.test.c1@example.invalid', 'XA', 'online', '2025-01-15 10:00+00')$q$),
    (2, 'setup', 'Locations XA-WH-T1, XA-WH-T2 (warehouses) and XA-STO-T1 (store)', '00000',
     $q$INSERT INTO stock_locations (location_code, location_name, location_type, country_code,
                                     address_line1, postal_code, city, opened_on)
        VALUES ('XA-WH-T1',  'Test warehouse 1', 'warehouse', 'XA', 'Testvägen 1', '12345', 'Testby', '2024-01-01'),
               ('XA-WH-T2',  'Test warehouse 2', 'warehouse', 'XA', 'Testvägen 2', '12345', 'Testby', '2024-01-01'),
               ('XA-STO-T1', 'Test store',       'store',     'XA', 'Testgatan 1', '12345', 'Testby', '2024-01-01');
        INSERT INTO warehouses (stock_location_id)
        SELECT stock_location_id FROM stock_locations WHERE location_code IN ('XA-WH-T1', 'XA-WH-T2');
        INSERT INTO stores (stock_location_id)
        SELECT stock_location_id FROM stock_locations WHERE location_code = 'XA-STO-T1';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (3, 'setup', 'List products: balances WH1/P1, WH1/P2, WH2/P1, STO/P2 (P3 not listed at WH1)', '00000',
     $q$INSERT INTO inventory_balances (stock_location_id, product_id, reorder_point)
        SELECT pg_temp.nc_loc(v.loc), pg_temp.nc_prod(v.sku), 2
          FROM (VALUES ('XA-WH-T1', 'NC-TEST-SKU-1'), ('XA-WH-T1', 'NC-TEST-SKU-2'),
                       ('XA-WH-T2', 'NC-TEST-SKU-1'), ('XA-STO-T1', 'NC-TEST-SKU-2')) AS v(loc, sku)$q$),

    -- ===================================================================== balances and the movement ledger
    (10, 'balances', 'New balance with stock already on it is rejected', '23514 inventory_balances_start_empty',
     $q$INSERT INTO inventory_balances (stock_location_id, product_id, quantity_on_hand)
        VALUES (pg_temp.nc_loc('XA-WH-T2'), pg_temp.nc_prod('NC-TEST-SKU-2'), 10)$q$),
    (11, 'balances', 'Second balance for the same location and product is rejected', '23505 uq_inventory_balances_location_product',
     $q$INSERT INTO inventory_balances (stock_location_id, product_id)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'))$q$),
    (12, 'balances', 'Negative reorder point is rejected', '23514 ck_inventory_balances_reorder_point',
     $q$UPDATE inventory_balances SET reorder_point = -1
         WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1') AND product_id = pg_temp.nc_prod('NC-TEST-SKU-1')$q$),
    (13, 'ledger', 'Opening balance +10 for WH1/P1 is accepted', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'opening_balance', 10, '2025-01-01 00:00+00')$q$),
    (14, 'ledger', 'The movement moved on-hand stock: WH1/P1 is 10 on hand, 0 reserved', 'true',
     $q$SELECT pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '10/0'$q$),
    (15, 'ledger', 'A second opening balance for the same balance is rejected (idempotent load)', '23505 uq_inventory_movements_one_opening_balance',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'opening_balance', 10, '2025-01-01 00:00+00')$q$),
    (16, 'ledger', 'Opening balances WH1/P2 +5, WH2/P1 +4, STO/P2 +3 are accepted', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at)
        SELECT pg_temp.nc_loc(v.loc), pg_temp.nc_prod(v.sku), 'opening_balance', v.qty, '2025-01-01 00:00+00'
          FROM (VALUES ('XA-WH-T1', 'NC-TEST-SKU-2', 5), ('XA-WH-T2', 'NC-TEST-SKU-1', 4),
                       ('XA-STO-T1', 'NC-TEST-SKU-2', 3)) AS v(loc, sku, qty)$q$),
    (17, 'balances', 'Writing quantity_on_hand directly is rejected', '23514 inventory_balances_quantities_by_ledger',
     $q$UPDATE inventory_balances SET quantity_on_hand = 999
         WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1') AND product_id = pg_temp.nc_prod('NC-TEST-SKU-1')$q$),
    (18, 'balances', 'Writing quantity_reserved directly is rejected', '23514 inventory_balances_quantities_by_ledger',
     $q$UPDATE inventory_balances SET quantity_reserved = 1
         WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1') AND product_id = pg_temp.nc_prod('NC-TEST-SKU-1')$q$),
    (19, 'balances', 'Changing the reorder point is allowed', '00000',
     $q$UPDATE inventory_balances SET reorder_point = 3
         WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1') AND product_id = pg_temp.nc_prod('NC-TEST-SKU-1')$q$),
    (20, 'balances', 'Moving a balance to another product is rejected', '23514 inventory_balances_identity_immutable',
     $q$UPDATE inventory_balances SET product_id = pg_temp.nc_prod('NC-TEST-SKU-3')
         WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T2') AND product_id = pg_temp.nc_prod('NC-TEST-SKU-1')$q$),
    (21, 'ledger', 'Purchase receipt without a delivery note is rejected', '23514 inventory_movements_document_reference_required',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 5, '2025-02-01 08:00+00')$q$),
    (22, 'ledger', 'Purchase receipt +5 with delivery note NC-TEST-DN-1 is accepted', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 5, '2025-02-01 08:00+00', 'NC-TEST-DN-1')$q$),
    (23, 'ledger', 'Retrying the same receipt (same delivery note) is rejected: stock is not counted twice', '23505 uq_inventory_movements_document_line',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 5, '2025-02-01 08:00+00', 'NC-TEST-DN-1')$q$),
    (24, 'ledger', 'Inbound movement with a negative quantity is rejected', '23514 ck_inventory_movements_inbound_positive',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', -1, now(), 'NC-TEST-DN-2')$q$),
    (25, 'ledger', 'Outbound movement with a positive quantity is rejected', '23514 ck_inventory_movements_outbound_negative',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'write_off', 1, now(), 'NC-TEST-WO-1')$q$),
    (26, 'ledger', 'Zero-quantity movement is rejected', '23514 ck_inventory_movements_nonzero',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'stock_adjustment', 0, now(), 'NC-TEST-SC-0')$q$),
    (27, 'ledger', 'Writing off more than is on hand is rejected: stock never goes negative', '23514 ck_inventory_balances_no_oversell',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T2'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'write_off', -5, now(), 'NC-TEST-WO-2')$q$),
    (28, 'ledger', 'Movement for a product not listed at the location is rejected', '23503 fk_inventory_movements_balance',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-3'), 'purchase_receipt', 5, now(), 'NC-TEST-DN-3')$q$),
    (29, 'ledger', 'Sale without an order line is rejected', '23514 ck_inventory_movements_sale_ref',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'sale', -1, now())$q$),
    (30, 'ledger', 'Return restock before returns exist is rejected (MVP)', '23514 inventory_movements_return_restock_mvp',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, return_item_id)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'return_restock', 1, now(), 1)$q$),
    (31, 'ledger', 'Return restock without a return item is rejected', '23514 ck_inventory_movements_restock_ref',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'return_restock', 1, now())$q$),
    (32, 'ledger', 'Transfer out WH2/P1 -2 (document NC-TEST-TR-1) is accepted', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T2'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'transfer_out', -2, '2025-02-02 08:00+00', 'NC-TEST-TR-1')$q$),
    (33, 'ledger', 'Transfer in without its transfer out is rejected', '23514 ck_inventory_movements_transfer_ref',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'transfer_in', 2, '2025-02-02 12:00+00')$q$),
    (34, 'ledger', 'Transfer in with a different quantity is rejected', '23514 inventory_movements_transfer_matches_transfer_out',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, related_movement_id)
        SELECT pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'transfer_in', 3, '2025-02-02 12:00+00', movement_id
          FROM inventory_movements WHERE external_reference = 'NC-TEST-TR-1'$q$),
    (35, 'ledger', 'Transfer in at the location it left is rejected', '23514 inventory_movements_transfer_matches_transfer_out',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, related_movement_id)
        SELECT pg_temp.nc_loc('XA-WH-T2'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'transfer_in', 2, '2025-02-02 12:00+00', movement_id
          FROM inventory_movements WHERE external_reference = 'NC-TEST-TR-1'$q$),
    (36, 'ledger', 'Transfer in dated before its transfer out is rejected', '23514 inventory_movements_timestamp_order',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, related_movement_id)
        SELECT pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'transfer_in', 2, '2025-02-01 12:00+00', movement_id
          FROM inventory_movements WHERE external_reference = 'NC-TEST-TR-1'$q$),
    (37, 'ledger', 'Transfer in WH1/P1 +2 matching NC-TEST-TR-1 is accepted', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, related_movement_id)
        SELECT pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'transfer_in', 2, '2025-02-02 12:00+00', movement_id
          FROM inventory_movements WHERE external_reference = 'NC-TEST-TR-1'$q$),
    (38, 'ledger', 'Receiving the same transfer twice is rejected', '23505 uq_inventory_movements_one_transfer_in_per_out',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, related_movement_id)
        SELECT pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'transfer_in', 2, '2025-02-02 12:00+00', movement_id
          FROM inventory_movements WHERE external_reference = 'NC-TEST-TR-1'$q$),
    (39, 'ledger', 'Updating a ledger row is rejected (insert-only)', '23000',
     $q$UPDATE inventory_movements SET quantity_change = 20 WHERE external_reference = 'NC-TEST-DN-1'$q$),
    (40, 'ledger', 'Deleting a ledger row is rejected (insert-only)', '23000',
     $q$DELETE FROM inventory_movements WHERE external_reference = 'NC-TEST-DN-1'$q$),
    (41, 'ledger', 'TRUNCATE of the ledger is rejected', '23000 inventory_no_truncate',
     $q$TRUNCATE inventory_movements$q$),
    (42, 'ledger', 'Stock count correction -1 (NC-TEST-SC-1) is accepted', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'stock_adjustment', -1, '2025-02-03 08:00+00', 'NC-TEST-SC-1')$q$),
    (43, 'ledger', 'Stock after the ledger steps: WH1/P1 16, WH1/P2 5, WH2/P1 2, STO/P2 3, nothing reserved', 'true',
     $q$SELECT pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '16/0' AND pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-2') = '5/0'
           AND pg_temp.nc_stock('XA-WH-T2', 'NC-TEST-SKU-1') = '2/0' AND pg_temp.nc_stock('XA-STO-T1', 'NC-TEST-SKU-2') = '3/0'$q$),

    -- ===================================================================== U1 place, U2 reserve
    (50, 'orders', 'U1: place orders O1, O2, O3, O4, O5, O7, O8', '00000',
     $q$INSERT INTO orders (order_number, customer_id, channel, store_id, fulfilment_location_id, country_code, currency_code,
                            items_total, shipping_fee, shipping_vat_amount, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT v.order_number,
               CASE WHEN v.channel = 'online' THEN (SELECT customer_id FROM customers WHERE customer_number = 'NC-TEST-C1') END,
               v.channel::sales_channel,
               CASE WHEN v.channel = 'in_store' THEN (SELECT store_id FROM stores WHERE stock_location_id = pg_temp.nc_loc('XA-STO-T1')) END,
               CASE WHEN v.channel = 'in_store' THEN pg_temp.nc_loc('XA-STO-T1') ELSE pg_temp.nc_loc('XA-WH-T1') END,
               'XA', 'XTS', v.items, v.ship, v.ship_vat, v.items + v.ship,
               CASE WHEN v.channel = 'online' THEN 'Testgatan 5' END,
               CASE WHEN v.channel = 'online' THEN '12345' END,
               CASE WHEN v.channel = 'online' THEN 'Testby' END,
               v.placed_at::timestamptz
          FROM (VALUES ('NC-TEST-O1', 'online',   2499.00, 49.00, 9.80, '2025-03-01 10:00+00'),
                       ('NC-TEST-O2', 'in_store',  499.00,  0.00, 0.00, '2025-03-01 11:00+00'),
                       ('NC-TEST-O3', 'online',   1000.00,  0.00, 0.00, '2025-03-02 10:00+00'),
                       ('NC-TEST-O4', 'online',     19.90,  0.00, 0.00, '2025-03-02 10:00+00'),
                       ('NC-TEST-O5', 'online',   1000.00,  0.00, 0.00, '2025-03-02 10:00+00'),
                       ('NC-TEST-O7', 'online',   2994.00,  0.00, 0.00, '2025-03-02 10:00+00'),
                       ('NC-TEST-O8', 'online',   5990.00,  0.00, 0.00, '2025-03-02 10:00+00'))
               AS v(order_number, channel, items, ship, ship_vat, placed_at);
        INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost, line_total, vat_rate, vat_amount)
        SELECT pg_temp.nc_order(v.order_number), v.line_number, pg_temp.nc_prod(v.sku), v.quantity,
               v.unit_price, v.unit_cost, v.line_total, 0.2500, v.vat_amount
          FROM (VALUES ('NC-TEST-O1', 1, 'NC-TEST-SKU-1',  2, 1000.00, 600.00, 2000.00, 400.00),
                       ('NC-TEST-O1', 2, 'NC-TEST-SKU-2',  1,  499.00, 300.00,  499.00,  99.80),
                       ('NC-TEST-O2', 1, 'NC-TEST-SKU-2',  1,  499.00, 300.00,  499.00,  99.80),
                       ('NC-TEST-O3', 1, 'NC-TEST-SKU-1',  1, 1000.00, 600.00, 1000.00, 200.00),
                       ('NC-TEST-O4', 1, 'NC-TEST-SKU-3',  1,   19.90,   5.00,   19.90,   3.98),
                       ('NC-TEST-O5', 1, 'NC-TEST-SKU-1',  1, 1000.00, 600.00, 1000.00, 200.00),
                       ('NC-TEST-O7', 1, 'NC-TEST-SKU-2',  6,  499.00, 300.00, 2994.00, 598.80),
                       ('NC-TEST-O8', 1, 'NC-TEST-SKU-1',  1, 1000.00, 600.00, 1000.00, 200.00),
                       ('NC-TEST-O8', 2, 'NC-TEST-SKU-2', 10,  499.00, 300.00, 4990.00, 998.00))
               AS v(order_number, line_number, sku, quantity, unit_price, unit_cost, line_total, vat_amount);
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (51, 'reservations', 'Reservation at another location than the fulfilment location is rejected', '23514 inventory_reservations_match_order_line',
     $q$INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
        VALUES (pg_temp.nc_item('NC-TEST-O3', 1), pg_temp.nc_loc('XA-WH-T2'), pg_temp.nc_prod('NC-TEST-SKU-1'), 1,
                '2025-03-02 10:01+00', '2025-03-02 10:16+00')$q$),
    (52, 'reservations', 'Reservation for part of the line quantity is rejected', '23514 inventory_reservations_match_order_line',
     $q$INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
        VALUES (pg_temp.nc_item('NC-TEST-O1', 1), pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 1,
                '2025-03-01 10:01+00', '2025-03-01 10:16+00')$q$),
    (53, 'reservations', 'Reservation inserted as consumed is rejected', '23514 inventory_reservations_starts_active',
     $q$INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, status, reserved_at, expires_at, closed_at)
        VALUES (pg_temp.nc_item('NC-TEST-O3', 1), pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 1, 'consumed',
                '2025-03-02 10:01+00', '2025-03-02 10:16+00', '2025-03-02 10:02+00')$q$),
    (54, 'reservations', 'Reservation dated before the order was placed is rejected', '23514 inventory_reservations_timestamp_order',
     $q$INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
        VALUES (pg_temp.nc_item('NC-TEST-O3', 1), pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 1,
                '2025-03-01 09:00+00', '2025-03-01 09:15+00')$q$),
    (55, 'reservations', 'Reservation that expires before it starts is rejected', '23514 ck_inventory_reservations_expiry',
     $q$INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
        VALUES (pg_temp.nc_item('NC-TEST-O3', 1), pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 1,
                '2025-03-02 10:01+00', '2025-03-02 10:01+00')$q$),
    (56, 'reservations', 'Reserving the same line twice is rejected (retried U2)', '23505 uq_inventory_reservations_one_active_per_line',
     $q$INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
        VALUES (pg_temp.nc_item('NC-TEST-O3', 1), pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 1,
                '2025-03-02 10:01+00', '2025-03-02 10:16+00');
        INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
        VALUES (pg_temp.nc_item('NC-TEST-O3', 1), pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 1,
                '2025-03-02 10:01+00', '2025-03-02 10:16+00')$q$),
    (57, 'reservations', 'Reservations left on an order that stays placed fail at commit', '23514 orders_placed_no_reservations',
     $q$INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
        VALUES (pg_temp.nc_item('NC-TEST-O3', 1), pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 1,
                '2025-03-02 10:01+00', '2025-03-02 10:16+00');
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (58, 'reservations', 'stock_reserved without any reservation fails at commit', '23514 orders_reservations_per_line',
     $q$UPDATE orders SET status = 'stock_reserved' WHERE order_number = 'NC-TEST-O3';
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (59, 'reservations', 'stock_reserved with only one of two lines reserved fails at commit', '23514 orders_reservations_per_line',
     $q$INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
        VALUES (pg_temp.nc_item('NC-TEST-O1', 1), pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 2,
                '2025-03-01 10:01+00', '2025-03-01 10:16+00');
        UPDATE orders SET status = 'stock_reserved' WHERE order_number = 'NC-TEST-O1';
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (60, 'reservations', 'Reserving more than is available is refused by the balance (no oversell)', '23514 ck_inventory_balances_no_oversell',
     $q$INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
        VALUES (pg_temp.nc_item('NC-TEST-O7', 1), pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 6,
                '2025-03-02 10:01+00', '2025-03-02 10:16+00')$q$),
    (61, 'U2 reserve', 'fn_reserve_order_stock reserves both lines of O1 and returns reserved', 'true',
     $q$SELECT fn_reserve_order_stock(pg_temp.nc_order('NC-TEST-O1'), '2025-03-01 10:01+00', '2025-03-01 10:16+00') = 'reserved'$q$),
    (62, 'U2 reserve', 'O1 passes the commit-time checks (stock_reserved, one active reservation per line)', '00000',
     $q$SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (63, 'U2 reserve', 'Reserved stock is counted: WH1/P1 16/2, WH1/P2 5/1, O1 is stock_reserved', 'true',
     $q$SELECT pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '16/2' AND pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-2') = '5/1'
           AND (SELECT status FROM orders WHERE order_number = 'NC-TEST-O1') = 'stock_reserved'$q$),
    (64, 'idempotency', 'Retrying U2 on O1 returns already_reserved and changes nothing', 'true',
     $q$SELECT fn_reserve_order_stock(pg_temp.nc_order('NC-TEST-O1'), '2025-03-01 10:01+00', '2025-03-01 10:16+00') = 'already_reserved'
           AND pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '16/2'
           AND (SELECT count(*) FROM inventory_reservations WHERE order_item_id IN (pg_temp.nc_item('NC-TEST-O1', 1), pg_temp.nc_item('NC-TEST-O1', 2))) = 2$q$),
    (65, 'U2 reserve', 'O7 (6 x P2, 4 available) returns out_of_stock and reserves nothing', 'true',
     $q$SELECT fn_reserve_order_stock(pg_temp.nc_order('NC-TEST-O7'), '2025-03-02 10:01+00', '2025-03-02 10:16+00') = 'out_of_stock'
           AND pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-2') = '5/1'
           AND (SELECT status FROM orders WHERE order_number = 'NC-TEST-O7') = 'placed'
           AND NOT EXISTS (SELECT 1 FROM inventory_reservations WHERE order_item_id = pg_temp.nc_item('NC-TEST-O7', 1))$q$),
    (66, 'U2 reserve', 'O4 (P3 not listed at WH1) returns out_of_stock', 'true',
     $q$SELECT fn_reserve_order_stock(pg_temp.nc_order('NC-TEST-O4'), '2025-03-02 10:01+00', '2025-03-02 10:16+00') = 'out_of_stock'$q$),
    (67, 'U2 reserve', 'Then O7 is cancelled (out_of_stock) in a new transaction', '00000',
     $q$UPDATE orders SET status = 'cancelled', cancelled_at = '2025-03-02 10:01+00', cancellation_reason = 'out_of_stock'
         WHERE order_number = 'NC-TEST-O7' AND status = 'placed';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (68, 'reservations', 'Reserving for an order that has left placed is rejected', '23514 inventory_reservations_order_placed',
     $q$INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
        VALUES (pg_temp.nc_item('NC-TEST-O1', 1), pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 2,
                '2025-03-01 10:02+00', '2025-03-01 10:17+00')$q$),
    (69, 'U2 reserve', 'fn_reserve_order_stock on a cancelled order is rejected', '23514 inventory_reservations_order_placed',
     $q$SELECT fn_reserve_order_stock(pg_temp.nc_order('NC-TEST-O7'), '2025-03-02 10:05+00', '2025-03-02 10:20+00')$q$),
    (70, 'reservations', 'Changing the quantity of a reservation is rejected', '23514 inventory_reservations_terms_immutable',
     $q$UPDATE inventory_reservations SET quantity = 1 WHERE order_item_id = pg_temp.nc_item('NC-TEST-O1', 1)$q$),
    (71, 'reservations', 'Deleting a reservation is rejected', '23514 inventory_reservations_no_delete',
     $q$DELETE FROM inventory_reservations WHERE order_item_id = pg_temp.nc_item('NC-TEST-O1', 1)$q$),
    (72, 'reservations', 'TRUNCATE of reservations is rejected', '23000 inventory_no_truncate',
     $q$TRUNCATE inventory_reservations$q$),
    (73, 'reservations', 'Consuming a reservation before the order is confirmed is rejected', '23514 inventory_reservations_order_state',
     $q$UPDATE inventory_reservations SET status = 'consumed', closed_at = '2025-03-01 10:05+00'
         WHERE order_item_id = pg_temp.nc_item('NC-TEST-O1', 1)$q$),
    (74, 'reservations', 'Writing off reserved units is refused (no oversell)', '23514 ck_inventory_balances_no_oversell',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'write_off', -5, now(), 'NC-TEST-WO-3')$q$),
    (75, 'failure recovery', 'A U2 that fails on its second line (oversold) is refused as a whole', '23514 ck_inventory_balances_no_oversell',
     $q$INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
        VALUES (pg_temp.nc_item('NC-TEST-O8', 1), pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 1,
                '2025-03-02 10:01+00', '2025-03-02 10:16+00');
        INSERT INTO inventory_reservations (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
        VALUES (pg_temp.nc_item('NC-TEST-O8', 2), pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 10,
                '2025-03-02 10:01+00', '2025-03-02 10:16+00')$q$),
    (76, 'failure recovery', 'After the failed U2 nothing is left: O8 holds no reservation, WH1/P1 still 16/2', 'true',
     $q$SELECT NOT EXISTS (SELECT 1 FROM inventory_reservations
                            WHERE order_item_id IN (pg_temp.nc_item('NC-TEST-O8', 1), pg_temp.nc_item('NC-TEST-O8', 2)))
           AND pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '16/2'$q$),

    -- ===================================================================== U3 payment and capture
    (80, 'payments', 'O1 attempt 1 pending, then authorized', '00000',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        VALUES (pg_temp.nc_order('NC-TEST-O1'), 1, 'card', 'TestPSP', 2548.00, '2025-03-01 10:05+00');
        UPDATE payment_attempts SET status = 'authorized', authorized_at = '2025-03-01 10:08+00'
         WHERE order_id = pg_temp.nc_order('NC-TEST-O1') AND attempt_number = 1;
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (81, 'payments', 'Capture after a reservation was released is rejected', '23514 payment_attempts_capture_requires_active_reservations',
     $q$UPDATE inventory_reservations SET status = 'released', closed_at = '2025-03-01 10:08+00'
         WHERE order_item_id = pg_temp.nc_item('NC-TEST-O1', 2);
        UPDATE payment_attempts SET status = 'captured', captured_at = '2025-03-01 10:09+00'
         WHERE order_id = pg_temp.nc_order('NC-TEST-O1') AND attempt_number = 1$q$),
    (82, 'payments', 'U3c: capture with all reservations active, then confirm: accepted', '00000',
     $q$SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1' FOR UPDATE;
        UPDATE payment_attempts SET status = 'captured', captured_at = '2025-03-01 10:09+00'
         WHERE order_id = pg_temp.nc_order('NC-TEST-O1') AND attempt_number = 1;
        UPDATE orders SET status = 'confirmed', confirmed_at = '2025-03-01 10:09+00'
         WHERE order_number = 'NC-TEST-O1' AND status = 'stock_reserved';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (83, 'reservations', 'Releasing a reservation of a confirmed order is rejected', '23514 inventory_reservations_order_state',
     $q$UPDATE inventory_reservations SET status = 'released', closed_at = '2025-03-01 10:10+00'
         WHERE order_item_id = pg_temp.nc_item('NC-TEST-O1', 1)$q$),
    (84, 'reservations', 'Expiring a reservation of a confirmed order is rejected', '23514 inventory_reservations_order_state',
     $q$UPDATE inventory_reservations SET status = 'expired', closed_at = '2025-03-01 10:20+00'
         WHERE order_item_id = pg_temp.nc_item('NC-TEST-O1', 1)$q$),

    -- ===================================================================== U5 fulfil
    (90, 'U5 fulfil', 'Sale before the reservation is consumed is rejected', '23514 inventory_movements_sale_matches_consumed_reservation',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, order_item_id)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'sale', -2, '2025-03-01 14:00+00', pg_temp.nc_item('NC-TEST-O1', 1))$q$),
    (91, 'U5 fulfil', 'fulfilled without stock issued fails at commit', '23514 orders_fulfilled_stock_issued',
     $q$UPDATE orders SET status = 'fulfilled', fulfilled_at = '2025-03-01 14:00+00' WHERE order_number = 'NC-TEST-O1';
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (92, 'U5 fulfil', 'Sale of a different quantity than the consumed reservation is rejected', '23514 inventory_movements_sale_matches_consumed_reservation',
     $q$UPDATE inventory_reservations SET status = 'consumed', closed_at = '2025-03-01 14:00+00'
         WHERE order_item_id = pg_temp.nc_item('NC-TEST-O1', 1);
        INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, order_item_id)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'sale', -1, '2025-03-01 14:00+00', pg_temp.nc_item('NC-TEST-O1', 1))$q$),
    (93, 'U5 fulfil', 'Sale dated before the order was confirmed is rejected', '23514 inventory_movements_timestamp_order',
     $q$UPDATE inventory_reservations SET status = 'consumed', closed_at = '2025-03-01 14:00+00'
         WHERE order_item_id = pg_temp.nc_item('NC-TEST-O1', 1);
        INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, order_item_id)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'sale', -2, '2025-03-01 09:00+00', pg_temp.nc_item('NC-TEST-O1', 1))$q$),
    (94, 'U5 fulfil', 'Consuming with a time before the order was confirmed is rejected', '23514 inventory_reservations_timestamp_order',
     $q$UPDATE inventory_reservations SET status = 'consumed', closed_at = '2025-03-01 10:05+00'
         WHERE order_item_id = pg_temp.nc_item('NC-TEST-O1', 1)$q$),
    (95, 'idempotency', 'A second sale for the same line in one U5 is rejected: stock is issued once', '23505 uq_inventory_movements_one_sale_per_line',
     $q$UPDATE inventory_reservations SET status = 'consumed', closed_at = '2025-03-01 14:00+00'
         WHERE order_item_id = pg_temp.nc_item('NC-TEST-O1', 1);
        INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, order_item_id)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'sale', -2, '2025-03-01 14:00+00', pg_temp.nc_item('NC-TEST-O1', 1));
        INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, order_item_id)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'sale', -2, '2025-03-01 14:00+00', pg_temp.nc_item('NC-TEST-O1', 1))$q$),
    (96, 'U5 fulfil', 'fn_fulfil_order_stock consumes, issues stock and fulfils O1', 'true',
     $q$SELECT fn_fulfil_order_stock(pg_temp.nc_order('NC-TEST-O1'), '2025-03-01 14:00+00') = 'fulfilled'$q$),
    (97, 'U5 fulfil', 'O1 passes the commit-time checks (fulfilled, every line consumed and sold once)', '00000',
     $q$SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (98, 'U5 fulfil', 'Stock left on hand once: WH1/P1 14/0, WH1/P2 4/0', 'true',
     $q$SELECT pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '14/0' AND pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-2') = '4/0'$q$),
    (99, 'idempotency', 'Retrying U5 returns already_fulfilled; still one sale per line', 'true',
     $q$SELECT fn_fulfil_order_stock(pg_temp.nc_order('NC-TEST-O1'), '2025-03-01 15:00+00') = 'already_fulfilled'
           AND (SELECT count(*) FROM inventory_movements
                 WHERE movement_type = 'sale'
                   AND order_item_id IN (pg_temp.nc_item('NC-TEST-O1', 1), pg_temp.nc_item('NC-TEST-O1', 2))) = 2
           AND pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '14/0'$q$),
    (100, 'reservations', 'Changing a consumed reservation is rejected', '23514 inventory_reservations_final_immutable',
     $q$UPDATE inventory_reservations SET closed_at = '2025-03-01 15:00+00'
         WHERE order_item_id = pg_temp.nc_item('NC-TEST-O1', 1)$q$),
    (101, 'U5 fulfil', 'Issuing stock for an order that is already fulfilled is rejected', '23514 inventory_movements_sale_order_state',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, order_item_id)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'sale', -2, '2025-03-01 15:00+00', pg_temp.nc_item('NC-TEST-O1', 1))$q$),
    (102, 'U5 fulfil', 'fn_fulfil_order_stock on an order that is not confirmed is rejected', '23514 orders_status_transition',
     $q$SELECT fn_fulfil_order_stock(pg_temp.nc_order('NC-TEST-O3'), '2025-03-02 14:00+00')$q$),
    (103, 'reservations', 'Closing a reservation without closed_at is rejected (O3 reserved, then released with no time; undone)', '23514 ck_inventory_reservations_closed_at',
     $q$SELECT fn_reserve_order_stock(pg_temp.nc_order('NC-TEST-O3'), '2025-03-02 10:01+00', '2025-03-02 10:16+00');
        UPDATE inventory_reservations SET status = 'released' WHERE order_item_id = pg_temp.nc_item('NC-TEST-O3', 1)$q$),

    -- ===================================================================== release (U3) and expiry (U4)
    (110, 'release', 'O3 reserved by U2', 'true',
     $q$SELECT fn_reserve_order_stock(pg_temp.nc_order('NC-TEST-O3'), '2025-03-02 10:01+00', '2025-03-02 10:16+00') = 'reserved'$q$),
    (111, 'release', 'O3 attempt 1 fails', '00000',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        VALUES (pg_temp.nc_order('NC-TEST-O3'), 1, 'card', 'TestPSP', 1000.00, '2025-03-02 10:02+00');
        UPDATE payment_attempts SET status = 'failed', failed_at = '2025-03-02 10:03+00', failure_reason = 'card_declined'
         WHERE order_id = pg_temp.nc_order('NC-TEST-O3') AND attempt_number = 1;
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (112, 'release', 'payment_failed while reservations are still active fails at commit', '23514 orders_closed_reservations_released',
     $q$UPDATE orders SET status = 'payment_failed', cancelled_at = '2025-03-02 10:04+00' WHERE order_number = 'NC-TEST-O3';
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (113, 'release', 'U3 final failure: fn_close_order_reservations releases 1 reservation', 'true',
     $q$SELECT fn_close_order_reservations(pg_temp.nc_order('NC-TEST-O3'), 'released', '2025-03-02 10:04+00') = 1$q$),
    (114, 'release', 'U3 final failure: order payment_failed in the same transaction passes the commit-time checks', '00000',
     $q$UPDATE orders SET status = 'payment_failed', cancelled_at = '2025-03-02 10:04+00' WHERE order_number = 'NC-TEST-O3';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (115, 'release', 'Released stock is available again: WH1/P1 14/0', 'true',
     $q$SELECT pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '14/0'$q$),
    (116, 'idempotency', 'Repeating the release closes 0 reservations and changes nothing', 'true',
     $q$SELECT fn_close_order_reservations(pg_temp.nc_order('NC-TEST-O3'), 'released', '2025-03-02 10:05+00') = 0
           AND pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '14/0'$q$),
    (117, 'release', 'Reopening a released reservation is rejected', '23514 inventory_reservations_status_transition',
     $q$UPDATE inventory_reservations SET status = 'active', closed_at = NULL
         WHERE order_item_id = pg_temp.nc_item('NC-TEST-O3', 1)$q$),
    (118, 'expiry', 'O5 reserved by U2 (expires 10:16)', 'true',
     $q$SELECT fn_reserve_order_stock(pg_temp.nc_order('NC-TEST-O5'), '2025-03-02 10:01+00', '2025-03-02 10:16+00') = 'reserved'$q$),
    (119, 'expiry', 'O5 attempt 1 authorized', '00000',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        VALUES (pg_temp.nc_order('NC-TEST-O5'), 1, 'card', 'TestPSP', 1000.00, '2025-03-02 10:02+00');
        UPDATE payment_attempts SET status = 'authorized', authorized_at = '2025-03-02 10:03+00'
         WHERE order_id = pg_temp.nc_order('NC-TEST-O5') AND attempt_number = 1;
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (120, 'expiry', 'Expiring before expires_at is rejected', '23514 inventory_reservations_expire_after_due',
     $q$SELECT fn_close_order_reservations(pg_temp.nc_order('NC-TEST-O5'), 'expired', '2025-03-02 10:10+00')$q$),
    (121, 'expiry', 'Cancelling without closing the reservations fails at commit', '23514 orders_closed_reservations_released',
     $q$UPDATE payment_attempts SET status = 'voided' WHERE order_id = pg_temp.nc_order('NC-TEST-O5') AND attempt_number = 1;
        UPDATE orders SET status = 'cancelled', cancelled_at = '2025-03-02 10:16+00', cancellation_reason = 'reservation_expired'
         WHERE order_number = 'NC-TEST-O5';
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (122, 'expiry', 'U4: expire reservations, void the authorization and cancel the order in one transaction', '00000',
     $q$SELECT fn_close_order_reservations(pg_temp.nc_order('NC-TEST-O5'), 'expired', '2025-03-02 10:16+00');
        UPDATE payment_attempts SET status = 'voided' WHERE order_id = pg_temp.nc_order('NC-TEST-O5') AND status = 'authorized';
        UPDATE orders SET status = 'cancelled', cancelled_at = '2025-03-02 10:16+00', cancellation_reason = 'reservation_expired'
         WHERE order_number = 'NC-TEST-O5' AND status = 'stock_reserved';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (123, 'expiry', 'Expired stock is available again and the reservation is expired: WH1/P1 14/0', 'true',
     $q$SELECT pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '14/0'
           AND (SELECT status FROM inventory_reservations WHERE order_item_id = pg_temp.nc_item('NC-TEST-O5', 1)) = 'expired'$q$),
    (124, 'release', 'fn_close_order_reservations accepts only released or expired', '22023',
     $q$SELECT fn_close_order_reservations(pg_temp.nc_order('NC-TEST-O5'), 'consumed', now())$q$),

    -- ===================================================================== in-store order, end to end
    (130, 'in store', 'O2 end to end: U2 at the store, cash payment, capture, confirm, U5', '00000',
     $q$DO $d$
        BEGIN
            IF fn_reserve_order_stock(pg_temp.nc_order('NC-TEST-O2'), '2025-03-01 11:01+00', '2025-03-01 11:06+00') <> 'reserved' THEN
                RAISE EXCEPTION 'reserve did not return reserved';
            END IF;
        END $d$;
        INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        VALUES (pg_temp.nc_order('NC-TEST-O2'), 1, 'cash', 'TestTill', 499.00, '2025-03-01 11:02+00');
        UPDATE payment_attempts SET status = 'authorized', authorized_at = '2025-03-01 11:02+00'
         WHERE order_id = pg_temp.nc_order('NC-TEST-O2') AND attempt_number = 1;
        UPDATE payment_attempts SET status = 'captured', captured_at = '2025-03-01 11:02+00'
         WHERE order_id = pg_temp.nc_order('NC-TEST-O2') AND attempt_number = 1;
        UPDATE orders SET status = 'confirmed', confirmed_at = '2025-03-01 11:02+00' WHERE order_number = 'NC-TEST-O2';
        DO $d$
        BEGIN
            IF fn_fulfil_order_stock(pg_temp.nc_order('NC-TEST-O2'), '2025-03-01 11:03+00') <> 'fulfilled' THEN
                RAISE EXCEPTION 'fulfil did not return fulfilled';
            END IF;
        END $d$;
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (131, 'in store', 'Store stock after the sale: STO/P2 2/0 (warehouse untouched)', 'true',
     $q$SELECT pg_temp.nc_stock('XA-STO-T1', 'NC-TEST-SKU-2') = '2/0' AND pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-2') = '4/0'$q$),

    -- ===================================================================== invariants and change capture
    (140, 'invariants', 'Every test balance: on_hand = SUM(movements) and reserved = SUM(active reservations)', 'true',
     $q$SELECT bool_and(b.quantity_on_hand = coalesce((SELECT sum(m.quantity_change) FROM inventory_movements AS m
                                                        WHERE m.stock_location_id = b.stock_location_id
                                                          AND m.product_id = b.product_id), 0)
                    AND b.quantity_reserved = coalesce((SELECT sum(r.quantity) FROM inventory_reservations AS r
                                                         WHERE r.stock_location_id = b.stock_location_id
                                                           AND r.product_id = b.product_id
                                                           AND r.status = 'active'), 0))
          FROM inventory_balances AS b
         WHERE b.stock_location_id IN (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_loc('XA-WH-T2'), pg_temp.nc_loc('XA-STO-T1'))$q$),
    (141, 'invariants', 'Every test order passes all commit-time checks together', '00000',
     $q$SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (142, 'change_log', 'Reservations, movements and balance changes were captured for the pipeline', 'true',
     $q$SELECT EXISTS (SELECT 1 FROM change_log WHERE table_name = 'inventory_reservations' AND operation = 'insert'
                         AND (row_data ->> 'order_item_id')::bigint = pg_temp.nc_item('NC-TEST-O1', 1))
           AND EXISTS (SELECT 1 FROM change_log WHERE table_name = 'inventory_movements' AND operation = 'insert'
                         AND row_data ->> 'movement_type' = 'sale'
                         AND (row_data ->> 'order_item_id')::bigint = pg_temp.nc_item('NC-TEST-O1', 1))
           AND EXISTS (SELECT 1 FROM change_log WHERE table_name = 'inventory_balances' AND operation = 'update'
                         AND (row_data ->> 'quantity_on_hand')::int = 14
                         AND (row_data ->> 'stock_location_id')::bigint = pg_temp.nc_loc('XA-WH-T1'))$q$),

    -- ===================================================================== defence in depth
    -- Balance quantities are written only by the ledger triggers. The guard
    -- recognises them by trigger depth, so an UPDATE issued from inside some
    -- other trigger also passes it (closed by V008 privileges; see the README).
    -- Even then the CHECK constraints on inventory_balances still hold.
    (143, 'defence in depth', 'A write from inside another trigger passes the depth guard but cannot make reserved stock negative', '23514 ck_inventory_balances_reserved',
     $q$CREATE TEMP TABLE nc_bypass (stock_location_id bigint, product_id bigint);
        CREATE FUNCTION pg_temp.nc_bypass_write() RETURNS trigger LANGUAGE plpgsql AS $f$
        BEGIN
            UPDATE inventory_balances SET quantity_reserved = -1
             WHERE stock_location_id = NEW.stock_location_id AND product_id = NEW.product_id;
            RETURN NULL;
        END $f$;
        CREATE TRIGGER nc_bypass_write AFTER INSERT ON pg_temp.nc_bypass FOR EACH ROW EXECUTE FUNCTION pg_temp.nc_bypass_write();
        INSERT INTO nc_bypass VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'))$q$)

                   ) AS t(test_no, area, test_name, expected, sql_text)
             ORDER BY test_no
        LOOP
            IF r.expected IN ('true', 'false') THEN
                v_actual := pg_temp.nc_check(r.sql_text);
            ELSE
                v_actual := pg_temp.nc_try(r.sql_text);
            END IF;

            v_log := v_log || jsonb_build_object(
                'test_no', r.test_no, 'area', r.area, 'test_name', r.test_name,
                'expected', r.expected, 'actual', v_actual);
        END LOOP;

        RAISE EXCEPTION 'nc_rollback_sandbox';
    EXCEPTION
        WHEN raise_exception THEN
            IF SQLERRM <> 'nc_rollback_sandbox' THEN
                RAISE;
            END IF;
    END;

    -- After the rollback: nothing from the sandbox may remain
    v_log := v_log || jsonb_build_object(
        'test_no', 199, 'area', 'safety',
        'test_name', 'Sandbox rolled back: no test orders, locations, stock, movements or change_log rows remain',
        'expected', 'true',
        'actual', pg_temp.nc_check($q$
            SELECT NOT EXISTS (SELECT 1 FROM orders          WHERE order_number LIKE 'NC-TEST-%')
               AND NOT EXISTS (SELECT 1 FROM stock_locations WHERE location_code LIKE 'XA-%')
               AND NOT EXISTS (SELECT 1 FROM products        WHERE sku LIKE 'NC-TEST-SKU-%')
               AND NOT EXISTS (SELECT 1 FROM currencies      WHERE currency_code = 'XTS')
               AND NOT EXISTS (SELECT 1 FROM inventory_movements WHERE external_reference LIKE 'NC-TEST-%')
               AND NOT EXISTS (SELECT 1 FROM change_log
                                WHERE (table_name = 'orders' AND row_data ->> 'order_number' LIKE 'NC-TEST-%')
                                   OR (table_name = 'inventory_movements' AND row_data ->> 'external_reference' LIKE 'NC-TEST-%')
                                   OR (table_name = 'currencies' AND record_key = 'XTS'))$q$));

    INSERT INTO nc_test_results (test_no, area, test_name, expected, actual)
    SELECT (e ->> 'test_no')::integer, e ->> 'area', e ->> 'test_name', e ->> 'expected', e ->> 'actual'
      FROM jsonb_array_elements(v_log) AS e;
END
$tests$;

-- -----------------------------------------------------------------------------
-- Result
-- -----------------------------------------------------------------------------
SELECT test_no,
       area,
       test_name,
       expected,
       actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS status,
       count(*) FILTER (WHERE expected IS DISTINCT FROM actual) OVER () AS failed_tests,
       count(*) OVER () AS total_tests
  FROM nc_test_results
 ORDER BY CASE WHEN expected = actual THEN 1 ELSE 0 END, test_no;

-- Reading the expected column: SQLSTATE, then the constraint or rule name.
--   00000 success        23000 insert-only or TRUNCATE guard     22023 invalid parameter
--   23503 foreign_key_violation   23505 unique_violation (idempotency keys)
--   23514 check_violation: ck_* are CHECK constraints (ck_inventory_balances_no_oversell
--   is the no-oversell and no-negative-stock guard); inventory_*, orders_* and
--   payment_attempts_* are the rule names raised by the V007 guards, the
--   commit-time checks and the unit-of-work functions (see the migration file).
