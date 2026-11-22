-- =============================================================================
-- NordicCart OLTP | Behaviour tests for V006: orders, order_items, payment_attempts
-- -----------------------------------------------------------------------------
-- Purpose     : prove that the order and payment rules accept good data and
--               reject bad data with the expected SQLSTATE and rule, including
--               the rules checked at COMMIT (deferred constraint triggers).
-- Connect to  : nordiccart_oltp, as the database owner
-- Run how     : run the whole file. Result: one table with status per test.
-- Expected    : every row has status PASS, and failed_tests = 0.
-- Revision    : 2 (2026-11-16): adds historical integrity, fulfilment country
--               and cancellation/payment consistency scenarios.
--               3 (2026-11-16): inventory-aware for V007 and later. The same 119
--               tests and expected results; where an order is reserved,
--               fulfilled, failed or cancelled, the steps now also reserve,
--               issue, release or expire stock through the V007 functions when
--               the inventory tables exist (pg_temp.nc_reserve, nc_fulfil,
--               nc_close). On a database at V006 they do exactly what they did
--               before, so the file runs unchanged at V006 and at V007.
-- -----------------------------------------------------------------------------
-- Safety (same harness as test_foundation_constraints.sql)
--   * All test rows are written inside one PL/pgSQL block that always ends by
--     raising an internal exception, which rolls the whole block back. Nothing
--     is committed to any business table or to change_log. Test 199 checks this.
--   * Test codes cannot collide with real data: currency XTS and XXX, countries
--     XA and XB, order numbers NC-TEST-*, SKUs NC-TEST-SKU-*, locations XA-*, XB-*.
--   * The only TRUNCATE (test 129) targets payment_attempts inside the sandbox
--     and is refused by its guard; even without the guard it would be rolled back.
--   * Session-only objects: temp table nc_test_results and two pg_temp helper
--     functions, gone when the connection closes.
--   * Side effect: identity sequences are not transactional, so each run uses
--     up some surrogate key numbers (gaps in IDs).
-- -----------------------------------------------------------------------------
-- How the commit-time rules are tested
--   Deferred checks normally run at COMMIT. A test forces them to run now with
--   SET CONSTRAINTS ALL IMMEDIATE (and then ALL DEFERRED again). A test that
--   expects a commit-time rejection does its writes and the SET CONSTRAINTS in
--   one step, so the rejected writes are undone with that step.
--
-- Scenario (amounts in test currency XTS, VAT 25 %)
--   O1 online : 2 x 1000.00 + 1 x 499.00 = 2499.00, shipping 49.00 (VAT 9.80),
--               total 2548.00. Payment fails once, succeeds on attempt 2,
--               order confirmed and fulfilled; then it is final.
--   O2 in store, anonymous: invalid cancellations refused, then cash payment.
--   O3 online : 1000.00 + 59.70. Three failed attempts, order payment_failed.
--   O5 online : 1000.00. Authorized; cancelling with the authorization open is
--               refused; cancel and void together is accepted (U4).
--   O6 online : 1000.00. Identity and context changes refused; cancelled with
--               a pending attempt; the late authorization must be voided.
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
-- raised it, so a rejection only passes for the intended reason. Each call is
-- a subtransaction, so a failed step leaves nothing behind.
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
CREATE OR REPLACE FUNCTION pg_temp.nc_check(p_sql text)
RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_result boolean;
BEGIN
    EXECUTE p_sql INTO v_result;
    RETURN coalesce(v_result::text, 'null');
EXCEPTION WHEN OTHERS THEN
    RETURN SQLSTATE;
END
$fn$;

-- Inventory-aware steps (revision 3). From V007 on, an order can only be
-- stock_reserved with reservations, fulfilled with sale movements, and
-- payment_failed or cancelled with its reservations closed. These helpers do
-- that through the V007 unit-of-work functions when the inventory tables
-- exist, and fall back to the plain status change on a database at V006.
CREATE OR REPLACE FUNCTION pg_temp.nc_has_inventory()
RETURNS boolean
LANGUAGE sql STABLE
AS $fn$ SELECT to_regclass('public.inventory_reservations') IS NOT NULL $fn$;

-- List every test product at XA-WH-T1 and XA-STO-T1 with 100 units opening stock.
CREATE OR REPLACE FUNCTION pg_temp.nc_list_stock()
RETURNS void
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF NOT pg_temp.nc_has_inventory() THEN
        RETURN;
    END IF;
    EXECUTE $s$
        INSERT INTO public.inventory_balances (stock_location_id, product_id)
        SELECT l.stock_location_id, p.product_id
          FROM public.stock_locations AS l
          JOIN public.products AS p ON p.sku LIKE 'NC-TEST-SKU-%'
         WHERE l.location_code IN ('XA-WH-T1', 'XA-STO-T1');
        INSERT INTO public.inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at)
        SELECT b.stock_location_id, b.product_id, 'opening_balance', 100, '2025-01-01 00:00+00'
          FROM public.inventory_balances AS b
          JOIN public.stock_locations AS l ON l.stock_location_id = b.stock_location_id
         WHERE l.location_code IN ('XA-WH-T1', 'XA-STO-T1')
    $s$;
END
$fn$;

-- U2: placed -> stock_reserved (with reservations from V007 on).
CREATE OR REPLACE FUNCTION pg_temp.nc_reserve(p_order_number text, p_reserved_at timestamptz)
RETURNS void
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_result  text;
BEGIN
    IF pg_temp.nc_has_inventory() THEN
        EXECUTE 'SELECT public.fn_reserve_order_stock(o.order_id, $1, $1 + interval ''15 minutes'')
                   FROM public.orders AS o WHERE o.order_number = $2'
           INTO v_result USING p_reserved_at, p_order_number;
        IF v_result IS DISTINCT FROM 'reserved' THEN
            RAISE EXCEPTION 'nc_reserve(%): fn_reserve_order_stock returned %', p_order_number, coalesce(v_result, 'NULL');
        END IF;
    ELSE
        UPDATE public.orders SET status = 'stock_reserved' WHERE order_number = p_order_number;
    END IF;
END
$fn$;

-- U5: confirmed -> fulfilled (consume reservations and issue stock from V007 on).
CREATE OR REPLACE FUNCTION pg_temp.nc_fulfil(p_order_number text, p_fulfilled_at timestamptz)
RETURNS void
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_result  text;
BEGIN
    IF pg_temp.nc_has_inventory() THEN
        EXECUTE 'SELECT public.fn_fulfil_order_stock(o.order_id, $1)
                   FROM public.orders AS o WHERE o.order_number = $2'
           INTO v_result USING p_fulfilled_at, p_order_number;
        IF v_result IS DISTINCT FROM 'fulfilled' THEN
            RAISE EXCEPTION 'nc_fulfil(%): fn_fulfil_order_stock returned %', p_order_number, coalesce(v_result, 'NULL');
        END IF;
    ELSE
        UPDATE public.orders SET status = 'fulfilled', fulfilled_at = p_fulfilled_at WHERE order_number = p_order_number;
    END IF;
END
$fn$;

-- U3 final failure (released) and U4 expiry (expired): close the order's
-- reservations from V007 on; nothing to do at V006.
CREATE OR REPLACE FUNCTION pg_temp.nc_close(p_order_number text, p_status text)
RETURNS void
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF pg_temp.nc_has_inventory() THEN
        EXECUTE 'SELECT public.fn_close_order_reservations(o.order_id, $1::public.reservation_status, now())
                   FROM public.orders AS o WHERE o.order_number = $2'
          USING p_status, p_order_number;
    END IF;
END
$fn$;

-- -----------------------------------------------------------------------------
-- Tests (run in order inside a rolled-back sandbox)
-- expected: '00000' = step succeeds; '<SQLSTATE> <rule>' = step is rejected
--           with that error by that constraint, guard or commit-time rule;
--           'true' = the check query returns true.
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
    (1, 'setup', 'Reference data: currencies XTS and XXX, countries XA (XTS, VAT 0.2500) and XB (XXX, VAT 0.2400)', '00000',
     $q$INSERT INTO currencies (currency_code, currency_name)
        VALUES ('XTS', 'Test currency'), ('XXX', 'No currency');
        INSERT INTO countries (country_code, country_name, currency_code, vat_rate, time_zone)
        VALUES ('XA', 'Testland A', 'XTS', 0.2500, 'Europe/Stockholm'),
               ('XB', 'Testland B', 'XXX', 0.2400, 'Europe/Oslo')$q$),
    (2, 'setup', 'Catalogue: one category, three products', '00000',
     $q$INSERT INTO product_categories (category_code, category_name)
        VALUES ('NC_TEST_CAT', 'NordicCart test category');
        INSERT INTO products (sku, product_name, brand, category_id)
        SELECT v.sku, v.name, 'TestBrand', c.category_id
          FROM product_categories AS c
         CROSS JOIN (VALUES ('NC-TEST-SKU-1', 'Test laptop'),
                            ('NC-TEST-SKU-2', 'Test headphones'),
                            ('NC-TEST-SKU-3', 'Test cable')) AS v(sku, name)
         WHERE c.category_code = 'NC_TEST_CAT'$q$),
    (3, 'setup', 'Customers NC-TEST-C1 and NC-TEST-C2', '00000',
     $q$INSERT INTO customers (customer_number, first_name, last_name, email,
                               country_code, signup_channel, registered_at)
        VALUES ('NC-TEST-C1', 'Åsa', 'Testsson', 'nc.test.c1@example.invalid', 'XA', 'online', '2025-01-15 10:00+00'),
               ('NC-TEST-C2', 'Erik', 'Testsson', 'nc.test.c2@example.invalid', 'XA', 'online', '2025-01-16 10:00+00')$q$),
    (4, 'setup', 'Locations: XA-WH-T1 (ships online), XA-WH-T2 (does not), store XA-STO-T1, warehouse XB-WH-T1', '00000',
     $q$INSERT INTO stock_locations (location_code, location_name, location_type, country_code,
                                     address_line1, postal_code, city, opened_on)
        VALUES ('XA-WH-T1',  'Test warehouse A1',  'warehouse', 'XA', 'Testvägen 1', '12345', 'Testby',  '2024-01-01'),
               ('XA-WH-T2',  'Test warehouse A2',  'warehouse', 'XA', 'Testvägen 2', '12345', 'Testby',  '2024-01-01'),
               ('XA-STO-T1', 'Test store A',       'store',     'XA', 'Testgatan 1', '12345', 'Testby',  '2024-01-01'),
               ('XB-WH-T1',  'Test warehouse B1',  'warehouse', 'XB', 'Testveien 1', '0123',  'Testvik', '2024-01-01');
        INSERT INTO warehouses (stock_location_id, ships_online_orders)
        SELECT stock_location_id, location_code <> 'XA-WH-T2'
          FROM stock_locations WHERE location_code IN ('XA-WH-T1', 'XA-WH-T2', 'XB-WH-T1');
        INSERT INTO stores (stock_location_id)
        SELECT stock_location_id FROM stock_locations WHERE location_code = 'XA-STO-T1';
        SELECT pg_temp.nc_list_stock();
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),

    -- ===================================================================== U1: place orders
    (10, 'orders', 'U1: online order O1 with two lines, from a shipping warehouse in its country, passes', '00000',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, shipping_fee, shipping_vat_amount, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O1', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               2499.00, 49.00, 9.80, 2548.00, 'Testgatan 5', '12345', 'Testby', '2025-03-01 10:00+00'
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T1';
        INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, v.line_number, p.product_id, v.quantity, v.unit_price, v.unit_cost,
               v.line_total, 0.2500, v.vat_amount
          FROM orders AS o
         CROSS JOIN (VALUES (1, 'NC-TEST-SKU-1', 2, 1000.00, 600.00, 2000.00, 400.00),
                            (2, 'NC-TEST-SKU-2', 1,  499.00, 300.00,  499.00,  99.80))
                    AS v(line_number, sku, quantity, unit_price, unit_cost, line_total, vat_amount)
          JOIN products AS p ON p.sku = v.sku
         WHERE o.order_number = 'NC-TEST-O1';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (11, 'orders', 'U1: anonymous in-store order O2 fulfilled from the selling store passes', '00000',
     $q$INSERT INTO orders (order_number, channel, store_id, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total, placed_at)
        SELECT 'NC-TEST-O2', 'in_store', s.store_id, s.stock_location_id, 'XA', 'XTS',
               499.00, 499.00, '2025-03-01 11:00+00'
          FROM stores AS s JOIN stock_locations AS l USING (stock_location_id)
         WHERE l.location_code = 'XA-STO-T1';
        INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 1, p.product_id, 1, 499.00, 300.00, 499.00, 0.2500, 99.80
          FROM orders AS o, products AS p
         WHERE o.order_number = 'NC-TEST-O2' AND p.sku = 'NC-TEST-SKU-2';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (12, 'orders', 'U1: online orders O3, O5 and O6 (one line, 1000.00) pass', '00000',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT v.order_number, c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               1000.00, 1000.00, 'Testgatan 5', '12345', 'Testby', '2025-03-02 10:00+00'
          FROM customers AS c, stock_locations AS l,
               (VALUES ('NC-TEST-O3'), ('NC-TEST-O5'), ('NC-TEST-O6')) AS v(order_number)
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T1';
        INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 1, p.product_id, 1, 1000.00, 600.00, 1000.00, 0.2500, 200.00
          FROM orders AS o, products AS p
         WHERE o.order_number IN ('NC-TEST-O3', 'NC-TEST-O5', 'NC-TEST-O6') AND p.sku = 'NC-TEST-SKU-1';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),

    -- ===================================================================== orders: row rules
    (13, 'orders', 'Insert with a status other than placed is rejected', '23514 orders_starts_placed',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            status, confirmed_at, items_total, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               'confirmed', now(), 100.00, 100.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T1'$q$),
    (14, 'orders', 'Online order without customer is rejected', '23514 ck_orders_online_customer',
     $q$INSERT INTO orders (order_number, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', 'online', l.stock_location_id, 'XA', 'XTS',
               100.00, 100.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM stock_locations AS l WHERE l.location_code = 'XA-WH-T1'$q$),
    (15, 'orders', 'Online order without shipping address is rejected', '23514 ck_orders_online_address',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS', 100.00, 100.00, now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T1'$q$),
    (16, 'orders', 'In-store order with a shipping fee is rejected', '23514 ck_orders_in_store_no_shipping',
     $q$INSERT INTO orders (order_number, channel, store_id, fulfilment_location_id, country_code, currency_code,
                            items_total, shipping_fee, shipping_vat_amount, order_total, placed_at)
        SELECT 'NC-TEST-O9', 'in_store', s.store_id, s.stock_location_id, 'XA', 'XTS',
               100.00, 49.00, 9.80, 149.00, now()
          FROM stores AS s JOIN stock_locations AS l USING (stock_location_id)
         WHERE l.location_code = 'XA-STO-T1'$q$),
    (17, 'orders', 'In-store order without store_id is rejected', '23514 ck_orders_store_matches_channel',
     $q$INSERT INTO orders (order_number, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total, placed_at)
        SELECT 'NC-TEST-O9', 'in_store', l.stock_location_id, 'XA', 'XTS', 100.00, 100.00, now()
          FROM stock_locations AS l WHERE l.location_code = 'XA-STO-T1'$q$),
    (18, 'orders', 'Online order with a store_id is rejected', '23514 ck_orders_store_matches_channel',
     $q$INSERT INTO orders (order_number, customer_id, channel, store_id, fulfilment_location_id, country_code,
                            currency_code, items_total, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', s.store_id, w.stock_location_id, 'XA',
               'XTS', 100.00, 100.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stores AS s, stock_locations AS w
         WHERE c.customer_number = 'NC-TEST-C1' AND w.location_code = 'XA-WH-T1'
           AND s.stock_location_id = (SELECT stock_location_id FROM stock_locations WHERE location_code = 'XA-STO-T1')$q$),
    (19, 'orders', 'In-store order fulfilled from a location other than its store is rejected', '23503 fk_orders_store_fulfilment',
     $q$INSERT INTO orders (order_number, channel, store_id, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total, placed_at)
        SELECT 'NC-TEST-O9', 'in_store', s.store_id, w.stock_location_id, 'XA', 'XTS', 100.00, 100.00, now()
          FROM stores AS s
          JOIN stock_locations AS l ON l.stock_location_id = s.stock_location_id AND l.location_code = 'XA-STO-T1'
         CROSS JOIN stock_locations AS w
         WHERE w.location_code = 'XA-WH-T1'$q$),
    (20, 'orders', 'order_total other than items_total + shipping_fee is rejected', '23514 ck_orders_total',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, shipping_fee, shipping_vat_amount, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               100.00, 49.00, 9.80, 100.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T1'$q$),
    (21, 'orders', 'Currency other than the country currency is rejected', '23514 orders_currency_matches_country',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', l.stock_location_id, 'XA', 'XXX',
               100.00, 100.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T1'$q$),
    (22, 'orders', 'Duplicate order_number is rejected', '23505 uq_orders_order_number',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O1', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               100.00, 100.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T1'$q$),
    (23, 'orders', 'Shipping VAT larger than the shipping fee is rejected', '23514 ck_orders_shipping_vat',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, shipping_fee, shipping_vat_amount, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               100.00, 49.00, 50.00, 149.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T1'$q$),
    (24, 'orders', 'Cancelling without cancelled_at is rejected', '23514 ck_orders_cancelled_at',
     $q$UPDATE orders SET status = 'cancelled', cancellation_reason = 'customer_request'
         WHERE order_number = 'NC-TEST-O3'$q$),

    -- ===================================================================== fulfilment location vs country
    (25, 'fulfilment', 'Online order fulfilled from a warehouse in another country is rejected', '23514 orders_fulfilment_country',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               100.00, 100.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XB-WH-T1'$q$),
    (26, 'fulfilment', 'In-store order sold in a country other than the store country is rejected', '23514 orders_fulfilment_country',
     $q$INSERT INTO orders (order_number, channel, store_id, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total, placed_at)
        SELECT 'NC-TEST-O9', 'in_store', s.store_id, s.stock_location_id, 'XB', 'XXX', 100.00, 100.00, now()
          FROM stores AS s JOIN stock_locations AS l USING (stock_location_id)
         WHERE l.location_code = 'XA-STO-T1'$q$),
    (27, 'fulfilment', 'Online order fulfilled from a store location is rejected', '23514 orders_online_fulfilment_warehouse',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               100.00, 100.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-STO-T1'$q$),
    (28, 'fulfilment', 'Online order fulfilled from a warehouse that does not ship online orders is rejected', '23514 orders_online_fulfilment_warehouse',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               100.00, 100.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T2'$q$),

    -- ===================================================================== order_items: row rules
    (30, 'order_items', 'Discount without a promotion is rejected', '23514 ck_order_items_promotion_discount',
     $q$INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 discount_percent, discount_amount, line_total, vat_rate, vat_amount)
        SELECT o.order_id, 3, p.product_id, 1, 100.00, 50.00, 10.00, 10.00, 90.00, 0.2500, 18.00
          FROM orders AS o, products AS p WHERE o.order_number = 'NC-TEST-O1' AND p.sku = 'NC-TEST-SKU-3'$q$),
    (31, 'order_items', 'Line total other than quantity x price - discount is rejected', '23514 ck_order_items_line_total',
     $q$INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 3, p.product_id, 1, 100.00, 50.00, 90.00, 0.2500, 18.00
          FROM orders AS o, products AS p WHERE o.order_number = 'NC-TEST-O1' AND p.sku = 'NC-TEST-SKU-3'$q$),
    (32, 'order_items', 'VAT amount not rounded by the formula is rejected', '23514 ck_order_items_vat_rounding',
     $q$INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 3, p.product_id, 1, 100.00, 50.00, 100.00, 0.2500, 19.99
          FROM orders AS o, products AS p WHERE o.order_number = 'NC-TEST-O1' AND p.sku = 'NC-TEST-SKU-3'$q$),
    (33, 'order_items', 'VAT rate other than the order country rate is rejected', '23514 order_items_vat_rate_matches_country',
     $q$INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 3, p.product_id, 1, 100.00, 50.00, 100.00, 0.2400, 19.35
          FROM orders AS o, products AS p WHERE o.order_number = 'NC-TEST-O1' AND p.sku = 'NC-TEST-SKU-3'$q$),
    (34, 'order_items', 'promotion_id before promotions exist is rejected', '23514 order_items_promotion_mvp',
     $q$INSERT INTO order_items (order_id, line_number, product_id, promotion_id, quantity, unit_price, unit_cost,
                                 discount_percent, discount_amount, line_total, vat_rate, vat_amount)
        SELECT o.order_id, 3, p.product_id, 1, 1, 100.00, 50.00, 10.00, 10.00, 90.00, 0.2500, 18.00
          FROM orders AS o, products AS p WHERE o.order_number = 'NC-TEST-O1' AND p.sku = 'NC-TEST-SKU-3'$q$),
    (35, 'order_items', 'Same product twice in one order is rejected', '23505 uq_order_items_order_product',
     $q$INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 3, p.product_id, 1, 1000.00, 600.00, 1000.00, 0.2500, 200.00
          FROM orders AS o, products AS p WHERE o.order_number = 'NC-TEST-O1' AND p.sku = 'NC-TEST-SKU-1'$q$),
    (36, 'order_items', 'Duplicate line_number in one order is rejected', '23505 uq_order_items_order_line',
     $q$INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 1, p.product_id, 1, 100.00, 50.00, 100.00, 0.2500, 20.00
          FROM orders AS o, products AS p WHERE o.order_number = 'NC-TEST-O1' AND p.sku = 'NC-TEST-SKU-3'$q$),
    (37, 'order_items', 'Zero quantity is rejected', '23514 ck_order_items_quantity',
     $q$INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 3, p.product_id, 0, 100.00, 50.00, 0.00, 0.2500, 0.00
          FROM orders AS o, products AS p WHERE o.order_number = 'NC-TEST-O1' AND p.sku = 'NC-TEST-SKU-3'$q$),
    (38, 'order_items', 'Unknown product is rejected', '23503 fk_order_items_product',
     $q$INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 3, -1, 1, 100.00, 50.00, 100.00, 0.2500, 20.00
          FROM orders AS o WHERE o.order_number = 'NC-TEST-O1'$q$),

    -- ===================================================================== commit-time order totals
    (40, 'order total', 'Order without lines fails at commit', '23514 orders_has_lines',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               0.00, 0.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T1';
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (41, 'order total', 'items_total different from the sum of the lines fails at commit', '23514 orders_items_total',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               900.00, 900.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T1';
        INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 1, p.product_id, 1, 1000.00, 600.00, 1000.00, 0.2500, 200.00
          FROM orders AS o, products AS p WHERE o.order_number = 'NC-TEST-O9' AND p.sku = 'NC-TEST-SKU-1';
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (42, 'order total', 'Shipping VAT not following round(fee x rate / (1 + rate), 2) fails at commit', '23514 orders_shipping_vat',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, shipping_fee, shipping_vat_amount, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               1000.00, 49.00, 9.00, 1049.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T1';
        INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 1, p.product_id, 1, 1000.00, 600.00, 1000.00, 0.2500, 200.00
          FROM orders AS o, products AS p WHERE o.order_number = 'NC-TEST-O9' AND p.sku = 'NC-TEST-SKU-1';
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (43, 'order total', 'Removing a line from a placed order without updating its totals fails at commit', '23514 orders_items_total',
     $q$DELETE FROM order_items
         WHERE line_number = 2
           AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1');
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (44, 'order total', 'Adding a line to a placed order with matching new totals passes', '00000',
     $q$INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 2, p.product_id, 3, 19.90, 5.00, 59.70, 0.2500, 11.94
          FROM orders AS o, products AS p WHERE o.order_number = 'NC-TEST-O3' AND p.sku = 'NC-TEST-SKU-3';
        UPDATE orders SET items_total = 1059.70, order_total = 1059.70 WHERE order_number = 'NC-TEST-O3';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),

    -- ===================================================================== historical integrity: identity, context, timestamps
    (45, 'integrity', 'Changing an order_number is rejected', '23514 orders_identity_immutable',
     $q$UPDATE orders SET order_number = 'NC-TEST-O2-RENAMED' WHERE order_number = 'NC-TEST-O2'$q$),
    (46, 'integrity', 'Reassigning an order to another customer is rejected', '23514 orders_identity_immutable',
     $q$UPDATE orders SET customer_id = (SELECT customer_id FROM customers WHERE customer_number = 'NC-TEST-C2')
         WHERE order_number = 'NC-TEST-O1'$q$),
    (47, 'integrity', 'Changing the purchase time placed_at is rejected', '23514 orders_identity_immutable',
     $q$UPDATE orders SET placed_at = placed_at - interval '1 day' WHERE order_number = 'NC-TEST-O1'$q$),
    (48, 'integrity', 'Changing created_at is rejected', '23514 orders_identity_immutable',
     $q$UPDATE orders SET created_at = '2000-01-01 00:00+00' WHERE order_number = 'NC-TEST-O1'$q$),
    (49, 'integrity', 'Regenerating order_id (SET order_id = DEFAULT) is rejected', '23514 orders_identity_immutable',
     $q$UPDATE orders SET order_id = DEFAULT WHERE order_number = 'NC-TEST-O6'$q$),
    (50, 'integrity', 'Changing the country and currency of a placed order is rejected', '23514 orders_context_immutable',
     $q$UPDATE orders SET country_code = 'XB', currency_code = 'XXX' WHERE order_number = 'NC-TEST-O6'$q$),
    (51, 'integrity', 'Changing the fulfilment location of a placed order is rejected', '23514 orders_context_immutable',
     $q$UPDATE orders SET fulfilment_location_id = (SELECT stock_location_id FROM stock_locations WHERE location_code = 'XA-WH-T2')
         WHERE order_number = 'NC-TEST-O6'$q$),
    (52, 'integrity', 'Moving a line to another order is rejected', '23514 order_items_identity_immutable',
     $q$UPDATE order_items SET order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O6')
         WHERE line_number = 2 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O3')$q$),
    (53, 'integrity', 'Inserting a placed order with confirmed_at already set is rejected', '23514 orders_event_timestamp_state',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            confirmed_at, items_total, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O9', c.customer_id, 'online', l.stock_location_id, 'XA', 'XTS',
               now(), 100.00, 100.00, 'Testgatan 5', '12345', 'Testby', now()
          FROM customers AS c, stock_locations AS l
         WHERE c.customer_number = 'NC-TEST-C1' AND l.location_code = 'XA-WH-T1'$q$),
    (54, 'integrity', 'Setting cancelled_at on an order that is not cancelled is rejected', '23514 orders_event_timestamp_state',
     $q$UPDATE orders SET status = 'stock_reserved', cancelled_at = now() WHERE order_number = 'NC-TEST-O6'$q$),

    -- ===================================================================== order status transitions
    (55, 'order status', 'placed -> confirmed is rejected', '23514 orders_status_transition',
     $q$UPDATE orders SET status = 'confirmed', confirmed_at = now() WHERE order_number = 'NC-TEST-O1'$q$),
    (56, 'order status', 'placed -> fulfilled is rejected', '23514 orders_status_transition',
     $q$UPDATE orders SET status = 'fulfilled', confirmed_at = now(), fulfilled_at = now()
         WHERE order_number = 'NC-TEST-O1'$q$),
    (57, 'order status', 'U2: placed -> stock_reserved is accepted', '00000',
     $q$SELECT pg_temp.nc_reserve('NC-TEST-O1', '2025-03-01 10:01+00');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (58, 'order status', 'stock_reserved -> placed is rejected', '23514 orders_status_transition',
     $q$UPDATE orders SET status = 'placed' WHERE order_number = 'NC-TEST-O1'$q$),
    (59, 'order_items', 'Changing a line of a stock_reserved order is rejected', '23514 order_items_immutable_after_placed',
     $q$UPDATE order_items SET quantity = 3, line_total = 3000.00, vat_amount = 600.00
         WHERE line_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (60, 'order_items', 'Adding a line to a stock_reserved order is rejected', '23514 order_items_immutable_after_placed',
     $q$INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost,
                                 line_total, vat_rate, vat_amount)
        SELECT o.order_id, 3, p.product_id, 1, 100.00, 50.00, 100.00, 0.2500, 20.00
          FROM orders AS o, products AS p WHERE o.order_number = 'NC-TEST-O1' AND p.sku = 'NC-TEST-SKU-3'$q$),
    (61, 'order_items', 'Deleting a line of a stock_reserved order is rejected', '23514 order_items_immutable_after_placed',
     $q$DELETE FROM order_items
         WHERE line_number = 2 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (62, 'order status', 'Changing the amount owed after placed is rejected', '23514 orders_terms_frozen',
     $q$UPDATE orders SET shipping_fee = 0.00, shipping_vat_amount = 0.00, order_total = 2499.00
         WHERE order_number = 'NC-TEST-O1'$q$),
    (63, 'order status', 'Changing the delivery address of a stock_reserved order is allowed', '00000',
     $q$UPDATE orders SET shipping_address_line1 = 'Testgatan 7' WHERE order_number = 'NC-TEST-O1'$q$),
    (64, 'integrity', 'Deleting an order is rejected (cancel instead)', '23514 orders_no_delete',
     $q$DELETE FROM orders WHERE order_number = 'NC-TEST-O1'$q$),

    -- ===================================================================== payments on O1: failure, retry, success (U3)
    (100, 'payments', 'Amount other than order_total is rejected', '23514 payment_attempts_amount_equals_order_total',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 1, 'card', 'TestPSP', 2499.00, now() FROM orders WHERE order_number = 'NC-TEST-O1'$q$),
    (101, 'payments', 'Cash for an online order is rejected', '23514 payment_attempts_cash_in_store_only',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 1, 'cash', 'TestPSP', 2548.00, now() FROM orders WHERE order_number = 'NC-TEST-O1'$q$),
    (102, 'payments', 'Attempt inserted as captured is rejected', '23514 payment_attempts_starts_pending',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, status,
                                      attempted_at, authorized_at, captured_at)
        SELECT order_id, 1, 'card', 'TestPSP', 2548.00, 'captured', now(), now(), now()
          FROM orders WHERE order_number = 'NC-TEST-O1'$q$),
    (103, 'payments', 'Attempt for an order that is not stock_reserved is rejected', '23514 payment_attempts_order_stock_reserved',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 1, 'card', 'TestPSP', order_total, now() FROM orders WHERE order_number = 'NC-TEST-O3'$q$),
    (104, 'payments', 'New pending attempt that already carries a failure is rejected', '23514 payment_attempts_outcome_by_transition',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount,
                                      attempted_at, failed_at, failure_reason)
        SELECT order_id, 1, 'card', 'TestPSP', 2548.00, now(), now(), 'card_declined'
          FROM orders WHERE order_number = 'NC-TEST-O1'$q$),
    (105, 'payments', 'Attempt dated before the order was placed is rejected', '23514 payment_attempts_timestamp_order',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 1, 'card', 'TestPSP', 2548.00, '2025-02-28 10:00+00' FROM orders WHERE order_number = 'NC-TEST-O1'$q$),
    (106, 'payments', 'U3a: attempt 1 inserted as pending is accepted', '00000',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 1, 'card', 'TestPSP', 2548.00, '2025-03-01 10:05+00' FROM orders WHERE order_number = 'NC-TEST-O1';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (107, 'payments', 'Second live attempt while attempt 1 is pending is rejected', '23505 uq_payment_attempts_one_live_per_order',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 2, 'card', 'TestPSP', 2548.00, now() FROM orders WHERE order_number = 'NC-TEST-O1'$q$),
    (108, 'payments', 'Changing the amount of a pending attempt is rejected', '23514 payment_attempts_terms_immutable',
     $q$UPDATE payment_attempts SET amount = 1.00
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (109, 'payments', 'Changing attempted_at of a pending attempt is rejected', '23514 payment_attempts_terms_immutable',
     $q$UPDATE payment_attempts SET attempted_at = attempted_at + interval '1 minute'
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (110, 'payments', 'pending -> captured is rejected', '23514 payment_attempts_status_transition',
     $q$UPDATE payment_attempts SET status = 'captured', authorized_at = now(), captured_at = now()
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (111, 'payments', 'Failed without a failure reason is rejected', '23514 ck_payment_attempts_failed',
     $q$UPDATE payment_attempts SET status = 'failed', failed_at = now()
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (112, 'payments', 'Writing failed_at without failing the attempt is rejected', '23514 payment_attempts_outcome_by_transition',
     $q$UPDATE payment_attempts SET failed_at = now()
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (113, 'payments', 'U3: pending -> failed (card_declined) is accepted', '00000',
     $q$UPDATE payment_attempts SET status = 'failed', failed_at = '2025-03-01 10:06+00', failure_reason = 'card_declined'
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (114, 'payments', 'failed -> authorized is rejected', '23514 payment_attempts_status_transition',
     $q$UPDATE payment_attempts SET status = 'authorized', authorized_at = now()
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (115, 'completed payments', 'Rewriting the failure reason of a failed attempt is rejected', '23514 payment_attempts_final_immutable',
     $q$UPDATE payment_attempts SET failure_reason = 'timeout'
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (116, 'completed payments', 'Deleting a failed attempt is rejected', '23514 payment_attempts_no_delete',
     $q$DELETE FROM payment_attempts
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (117, 'payments', 'U3: retry, attempt 2 inserted as pending after the failure is accepted', '00000',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 2, 'mobile_payment', 'TestPSP', 2548.00, '2025-03-01 10:07+00'
          FROM orders WHERE order_number = 'NC-TEST-O1';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (118, 'payments', 'Authorization dated before the attempt is rejected', '23514 payment_attempts_timestamp_order',
     $q$UPDATE payment_attempts SET status = 'authorized', authorized_at = '2025-03-01 10:00+00'
         WHERE attempt_number = 2 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (119, 'payments', 'pending -> authorized with a provider reference is accepted', '00000',
     $q$UPDATE payment_attempts SET status = 'authorized', authorized_at = '2025-03-01 10:08+00',
                                    provider_reference = 'NC-TEST-PSP-0002'
         WHERE attempt_number = 2 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (120, 'payments', 'Replacing the provider reference is rejected', '23514 payment_attempts_provider_reference_write_once',
     $q$UPDATE payment_attempts SET provider_reference = 'NC-TEST-PSP-9999'
         WHERE attempt_number = 2 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (121, 'payments', 'Writing captured_at without capturing is rejected', '23514 payment_attempts_outcome_by_transition',
     $q$UPDATE payment_attempts SET captured_at = now()
         WHERE attempt_number = 2 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (122, 'payments', 'Confirming the order without a captured payment fails at commit', '23514 orders_confirmed_has_capture',
     $q$UPDATE orders SET status = 'confirmed', confirmed_at = now() WHERE order_number = 'NC-TEST-O1';
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (123, 'payments', 'Capturing without confirming the order fails at commit', '23514 orders_capture_confirms_order',
     $q$UPDATE payment_attempts SET status = 'captured', captured_at = now()
         WHERE attempt_number = 2 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1');
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (124, 'integrity', 'Confirming with confirmed_at before placed_at is rejected', '23514 orders_event_timestamp_order',
     $q$UPDATE payment_attempts SET status = 'captured', captured_at = '2025-03-01 10:09+00'
         WHERE attempt_number = 2 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1');
        UPDATE orders SET status = 'confirmed', confirmed_at = '2025-02-28 10:00+00'
         WHERE order_number = 'NC-TEST-O1'$q$),
    (125, 'payments', 'U3c: capture attempt 2, then confirm the order, in one transaction: accepted', '00000',
     $q$SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1' FOR UPDATE;
        UPDATE payment_attempts SET status = 'captured', captured_at = '2025-03-01 10:09+00'
         WHERE attempt_number = 2 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1');
        UPDATE orders SET status = 'confirmed', confirmed_at = '2025-03-01 10:09+00'
         WHERE order_number = 'NC-TEST-O1' AND status = 'stock_reserved';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (126, 'payments', 'captured -> voided is rejected', '23514 payment_attempts_status_transition',
     $q$UPDATE payment_attempts SET status = 'voided'
         WHERE attempt_number = 2 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (127, 'completed payments', 'Changing any field of a captured attempt is rejected', '23514 payment_attempts_final_immutable',
     $q$UPDATE payment_attempts SET payment_provider = payment_provider, provider_reference = 'NC-TEST-PSP-0003'
         WHERE attempt_number = 2 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')$q$),
    (128, 'payments', 'New attempt on a confirmed order is rejected (no second payment)', '23514 payment_attempts_order_stock_reserved',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 3, 'card', 'TestPSP', 2548.00, now() FROM orders WHERE order_number = 'NC-TEST-O1'$q$),
    (129, 'completed payments', 'TRUNCATE of payment_attempts is rejected', '23000 history_no_truncate',
     $q$TRUNCATE payment_attempts$q$),
    (130, 'order status', 'confirmed -> stock_reserved is rejected', '23514 orders_status_transition',
     $q$UPDATE orders SET status = 'stock_reserved' WHERE order_number = 'NC-TEST-O1'$q$),
    (131, 'order status', 'confirmed -> cancelled is rejected until refunds exist (DBML Phase 4)', '23514 orders_status_transition',
     $q$UPDATE orders SET status = 'cancelled', cancelled_at = now(), cancellation_reason = 'customer_request'
         WHERE order_number = 'NC-TEST-O1'$q$),
    (132, 'integrity', 'Rewriting confirmed_at of a confirmed order is rejected', '23514 orders_event_timestamp_changed',
     $q$UPDATE orders SET confirmed_at = confirmed_at + interval '1 hour' WHERE order_number = 'NC-TEST-O1'$q$),
    (133, 'integrity', 'Fulfilling with fulfilled_at before confirmed_at is rejected', '23514 orders_event_timestamp_order',
     $q$UPDATE orders SET status = 'fulfilled', fulfilled_at = '2025-03-01 10:00+00' WHERE order_number = 'NC-TEST-O1'$q$),
    (134, 'order status', 'U5: confirmed -> fulfilled is accepted', '00000',
     $q$SELECT pg_temp.nc_fulfil('NC-TEST-O1', '2025-03-01 14:00+00');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (135, 'order status', 'fulfilled -> cancelled is rejected', '23514 orders_status_transition',
     $q$UPDATE orders SET status = 'cancelled', cancelled_at = now(), cancellation_reason = 'customer_request'
         WHERE order_number = 'NC-TEST-O1'$q$),
    (136, 'integrity', 'Changing a fulfilled order (delivery address) is rejected', '23514 orders_final_immutable',
     $q$UPDATE orders SET shipping_address_line1 = 'Testgatan 9' WHERE order_number = 'NC-TEST-O1'$q$),

    -- ===================================================================== payment failure on O3 (Business Rules 1.5)
    (140, 'payment failure', 'O3 reserved, then three attempts each fail (retry while reserved)', '00000',
     $q$SELECT pg_temp.nc_reserve('NC-TEST-O3', '2025-03-02 10:01+00');
        INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 1, 'card', 'TestPSP', order_total, now() FROM orders WHERE order_number = 'NC-TEST-O3';
        UPDATE payment_attempts SET status = 'failed', failed_at = now(), failure_reason = 'insufficient_funds'
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O3');
        INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 2, 'card', 'TestPSP', order_total, now() FROM orders WHERE order_number = 'NC-TEST-O3';
        UPDATE payment_attempts SET status = 'failed', failed_at = now(), failure_reason = 'card_declined'
         WHERE attempt_number = 2 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O3');
        INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 3, 'buy_now_pay_later', 'TestPSP', order_total, now() FROM orders WHERE order_number = 'NC-TEST-O3';
        UPDATE payment_attempts SET status = 'failed', failed_at = now(), failure_reason = 'timeout'
         WHERE attempt_number = 3 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O3');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (141, 'payment failure', 'Reusing attempt number 2 is rejected', '23505 uq_payment_attempts_order_attempt',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 2, 'card', 'TestPSP', order_total, now() FROM orders WHERE order_number = 'NC-TEST-O3'$q$),
    (142, 'payment failure', 'payment_failed with a cancellation_reason is rejected', '23514 orders_cancellation_reason',
     $q$UPDATE orders SET status = 'payment_failed', cancelled_at = now(), cancellation_reason = 'out_of_stock'
         WHERE order_number = 'NC-TEST-O3'$q$),
    (143, 'payment failure', 'After the last failure the order becomes payment_failed: accepted', '00000',
     $q$SELECT pg_temp.nc_close('NC-TEST-O3', 'released');
        UPDATE orders SET status = 'payment_failed', cancelled_at = now() WHERE order_number = 'NC-TEST-O3';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (144, 'payment failure', 'payment_failed -> stock_reserved is rejected', '23514 orders_status_transition',
     $q$UPDATE orders SET status = 'stock_reserved' WHERE order_number = 'NC-TEST-O3'$q$),
    (145, 'integrity', 'Changing a payment_failed order is rejected', '23514 orders_final_immutable',
     $q$UPDATE orders SET shipping_city = 'Annanstad' WHERE order_number = 'NC-TEST-O3'$q$),
    (146, 'payment failure', 'payment_failed without any failed attempt fails at commit', '23514 orders_payment_failed_state',
     $q$SELECT pg_temp.nc_reserve('NC-TEST-O5', '2025-03-02 10:01+00');
        SELECT pg_temp.nc_close('NC-TEST-O5', 'released');
        UPDATE orders SET status = 'payment_failed', cancelled_at = now() WHERE order_number = 'NC-TEST-O5';
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (147, 'payment failure', 'payment_failed while an attempt is still pending fails at commit', '23514 orders_payment_failed_state',
     $q$SELECT pg_temp.nc_reserve('NC-TEST-O5', '2025-03-02 10:01+00');
        INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 1, 'card', 'TestPSP', order_total, now() FROM orders WHERE order_number = 'NC-TEST-O5';
        SELECT pg_temp.nc_close('NC-TEST-O5', 'released');
        UPDATE orders SET status = 'payment_failed', cancelled_at = now() WHERE order_number = 'NC-TEST-O5';
        SET CONSTRAINTS ALL IMMEDIATE$q$),

    -- ===================================================================== cancellation and payment state
    (150, 'cancellation', 'Cancelling without a cancellation reason is rejected', '23514 orders_cancellation_reason',
     $q$UPDATE orders SET status = 'cancelled', cancelled_at = now() WHERE order_number = 'NC-TEST-O2'$q$),
    (151, 'cancellation', 'Cancelling with an undocumented reason is rejected', '23514 orders_cancellation_reason',
     $q$UPDATE orders SET status = 'cancelled', cancelled_at = now(), cancellation_reason = 'changed_mind'
         WHERE order_number = 'NC-TEST-O2'$q$),
    (152, 'cancellation', 'A cancellation reason on an order that is not cancelled is rejected', '23514 orders_cancellation_reason',
     $q$UPDATE orders SET cancellation_reason = 'customer_request' WHERE order_number = 'NC-TEST-O2'$q$),
    (153, 'cancellation', 'O5 reserved and its attempt authorized', '00000',
     $q$SELECT pg_temp.nc_reserve('NC-TEST-O5', '2025-03-02 10:01+00');
        INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 1, 'card', 'TestPSP', order_total, now() FROM orders WHERE order_number = 'NC-TEST-O5';
        UPDATE payment_attempts SET status = 'authorized', authorized_at = now()
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O5');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (154, 'cancellation', 'Cancelling while an authorization is still open fails at commit', '23514 orders_cancelled_open_authorization',
     $q$SELECT pg_temp.nc_close('NC-TEST-O5', 'expired');
        UPDATE orders SET status = 'cancelled', cancelled_at = now(), cancellation_reason = 'reservation_expired'
         WHERE order_number = 'NC-TEST-O5';
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (155, 'cancellation', 'U4: cancel the order and void its authorization in one transaction: accepted', '00000',
     $q$SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O5' FOR UPDATE;
        SELECT pg_temp.nc_close('NC-TEST-O5', 'expired');
        UPDATE orders SET status = 'cancelled', cancelled_at = now(), cancellation_reason = 'reservation_expired'
         WHERE order_number = 'NC-TEST-O5' AND status = 'stock_reserved';
        UPDATE payment_attempts SET status = 'voided'
         WHERE status = 'authorized' AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O5');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (156, 'integrity', 'Changing a cancelled order is rejected', '23514 orders_final_immutable',
     $q$UPDATE orders SET shipping_address_line1 = 'Testgatan 9' WHERE order_number = 'NC-TEST-O5'$q$),
    (157, 'completed payments', 'Changing a voided attempt is rejected', '23514 payment_attempts_final_immutable',
     $q$UPDATE payment_attempts SET provider_reference = 'NC-TEST-PSP-0005'
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O5')$q$),
    (158, 'cancellation', 'O6 reserved with attempt 1 pending (provider outcome unknown)', '00000',
     $q$SELECT pg_temp.nc_reserve('NC-TEST-O6', '2025-03-02 10:01+00');
        INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 1, 'card', 'TestPSP', order_total, now() FROM orders WHERE order_number = 'NC-TEST-O6';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (159, 'cancellation', 'U4: cancelling with a pending attempt is accepted (outcome still unknown)', '00000',
     $q$SELECT pg_temp.nc_close('NC-TEST-O6', 'expired');
        UPDATE orders SET status = 'cancelled', cancelled_at = now(), cancellation_reason = 'reservation_expired'
         WHERE order_number = 'NC-TEST-O6';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (160, 'cancellation', 'Late authorization on the cancelled order left open fails at commit', '23514 orders_cancelled_open_authorization',
     $q$UPDATE payment_attempts SET status = 'authorized', authorized_at = now()
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O6');
        SET CONSTRAINTS ALL IMMEDIATE$q$),
    (161, 'cancellation', 'Capturing a late authorization on the cancelled order is rejected', '23514 payment_attempts_capture_requires_reserved_order',
     $q$UPDATE payment_attempts SET status = 'authorized', authorized_at = now()
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O6');
        UPDATE payment_attempts SET status = 'captured', captured_at = now()
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O6')$q$),
    (162, 'cancellation', 'Late authorization voided in the same transaction is accepted', '00000',
     $q$UPDATE payment_attempts SET status = 'authorized', authorized_at = now()
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O6');
        UPDATE payment_attempts SET status = 'voided'
         WHERE attempt_number = 1 AND order_id = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O6');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (163, 'payments', 'Cash attempt for the in-store order O2 is accepted', '00000',
     $q$SELECT pg_temp.nc_reserve('NC-TEST-O2', '2025-03-01 11:01+00');
        INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        SELECT order_id, 1, 'cash', 'TestTill', 499.00, now() FROM orders WHERE order_number = 'NC-TEST-O2';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),

    -- ===================================================================== change capture
    (190, 'change_log', 'Inserts of O1, its two lines and its two attempts were captured', 'true',
     $q$SELECT EXISTS (SELECT 1 FROM change_log
                        WHERE table_name = 'orders' AND operation = 'insert'
                          AND row_data ->> 'order_number' = 'NC-TEST-O1')
          AND (SELECT count(*) FROM change_log
                WHERE table_name = 'order_items' AND operation = 'insert'
                  AND (row_data ->> 'order_id')::bigint = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')) = 2
          AND (SELECT count(*) FROM change_log
                WHERE table_name = 'payment_attempts' AND operation = 'insert'
                  AND (row_data ->> 'order_id')::bigint = (SELECT order_id FROM orders WHERE order_number = 'NC-TEST-O1')) = 2$q$),
    (191, 'change_log', 'Every status change of O1 was captured (stock_reserved, confirmed, fulfilled)', 'true',
     $q$SELECT array_agg(DISTINCT row_data ->> 'status' ORDER BY row_data ->> 'status')
               @> ARRAY['confirmed', 'fulfilled', 'stock_reserved']
          FROM change_log
         WHERE table_name = 'orders' AND operation = 'update'
           AND record_key = (SELECT order_id::text FROM orders WHERE order_number = 'NC-TEST-O1')$q$)
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
        'test_name', 'Sandbox rolled back: no test orders, lines, payments, reference rows or change_log rows remain',
        'expected', 'true',
        'actual', pg_temp.nc_check($q$
            SELECT NOT EXISTS (SELECT 1 FROM orders     WHERE order_number LIKE 'NC-TEST-%')
               AND NOT EXISTS (SELECT 1 FROM payment_attempts WHERE provider_reference LIKE 'NC-TEST-%')
               AND NOT EXISTS (SELECT 1 FROM products   WHERE sku LIKE 'NC-TEST-SKU-%')
               AND NOT EXISTS (SELECT 1 FROM currencies WHERE currency_code IN ('XTS', 'XXX'))
               AND NOT EXISTS (SELECT 1 FROM countries  WHERE country_code IN ('XA', 'XB'))
               AND NOT EXISTS (SELECT 1 FROM stock_locations WHERE location_code LIKE 'XA-%' OR location_code LIKE 'XB-%')
               AND NOT EXISTS (SELECT 1 FROM customers  WHERE customer_number LIKE 'NC-TEST-%')
               AND NOT EXISTS (SELECT 1 FROM change_log
                                WHERE (table_name = 'orders' AND row_data ->> 'order_number' LIKE 'NC-TEST-%')
                                   OR (table_name = 'currencies' AND record_key IN ('XTS', 'XXX')))$q$));

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
--   00000 success              23503 foreign_key_violation   23505 unique_violation
--   23514 check_violation: ck_* are CHECK constraints; orders_*, order_items_* and
--   payment_attempts_* are the rule names raised by the V006 guard triggers and
--   commit-time checks (see the migration file).
