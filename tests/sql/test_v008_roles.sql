-- =============================================================================
-- NordicCart OLTP | Behaviour tests for V008: roles and privileges
-- -----------------------------------------------------------------------------
-- Purpose     : prove the V008 privilege matrix by doing: every application role
--               can do what its work needs (the units of work, receipts,
--               transfers, reorder points, the commit-time checks, extraction)
--               and is refused, with SQLSTATE 42501 (insufficient_privilege),
--               everything else: changing quantities or history, deleting,
--               truncating, writing or reading change_log contents, creating
--               objects, changing ownership or triggers, switching role or
--               trigger mode, calling protected functions, server files. The
--               existing guards still hold for the owner itself.
-- Connect to  : nordiccart_oltp (or a test copy) with V008 applied, as a
--               SUPERUSER (postgres): the tests switch role with set_config('role').
-- Run how     : run the whole file. Result: one table with status per test.
-- Expected    : every row has status PASS, and failed_tests = 0.
-- Safety      : the same harness as the V006 and V007 tests. Every test row is
--               written inside one PL/pgSQL block that always ends by raising an
--               internal exception, so nothing is committed (test 299 checks it).
--               The test role nc_test_nobody is created inside that block and
--               rolled back with it. Codes cannot collide with real data
--               (currency XTS, country XA, NC-TEST-* keys). Identity sequences
--               are not transactional: each run uses up a few surrogate ids.
-- Design      : docs/phase3_combined_activity_design.md 11.2 ("test_v008_roles.sql"),
--               matrix in docs/phase3_v008_security_privilege_matrix.md.
-- =============================================================================

CREATE TEMP TABLE IF NOT EXISTS nc_test_results (
    test_no    integer,
    area       text,
    test_name  text,
    expected   text,
    actual     text
);

TRUNCATE nc_test_results;

-- Runs SQL as a role; returns '00000' on success, otherwise the SQLSTATE and the
-- constraint or rule name. A subtransaction: a failed step leaves nothing behind,
-- and the role switch ends with the step.
CREATE OR REPLACE FUNCTION pg_temp.nc_as(p_role text, p_sql text)
RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_state       text;
    v_constraint  text;
BEGIN
    PERFORM set_config('role', p_role, true);
    EXECUTE p_sql;
    PERFORM set_config('role', 'none', true);
    RETURN '00000';
EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS v_state = RETURNED_SQLSTATE, v_constraint = CONSTRAINT_NAME;
    RETURN v_state || coalesce(' ' || nullif(v_constraint, ''), '');
END
$fn$;

-- Evaluates a boolean query as a role; returns 'true', 'false' or the SQLSTATE.
CREATE OR REPLACE FUNCTION pg_temp.nc_check_as(p_role text, p_sql text)
RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_result      boolean;
    v_state       text;
    v_constraint  text;
BEGIN
    PERFORM set_config('role', p_role, true);
    EXECUTE p_sql INTO v_result;
    PERFORM set_config('role', 'none', true);
    RETURN coalesce(v_result::text, 'null');
EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS v_state = RETURNED_SQLSTATE, v_constraint = CONSTRAINT_NAME;
    RETURN v_state || coalesce(' ' || nullif(v_constraint, ''), '');
END
$fn$;

CREATE OR REPLACE FUNCTION pg_temp.nc_loc(p_code text) RETURNS bigint
LANGUAGE sql STABLE AS $fn$ SELECT stock_location_id FROM public.stock_locations WHERE location_code = p_code $fn$;
CREATE OR REPLACE FUNCTION pg_temp.nc_prod(p_sku text) RETURNS bigint
LANGUAGE sql STABLE AS $fn$ SELECT product_id FROM public.products WHERE sku = p_sku $fn$;
CREATE OR REPLACE FUNCTION pg_temp.nc_order(p_order_number text) RETURNS bigint
LANGUAGE sql STABLE AS $fn$ SELECT order_id FROM public.orders WHERE order_number = p_order_number $fn$;
CREATE OR REPLACE FUNCTION pg_temp.nc_stock(p_code text, p_sku text) RETURNS text
LANGUAGE sql STABLE AS $fn$
    SELECT b.quantity_on_hand || '/' || b.quantity_reserved FROM public.inventory_balances AS b
     WHERE b.stock_location_id = pg_temp.nc_loc(p_code) AND b.product_id = pg_temp.nc_prod(p_sku) $fn$;

-- -----------------------------------------------------------------------------
-- Tests (run in order inside a rolled-back sandbox)
-- role 'postgres' = the superuser running the file (setup and owner checks).
-- expected: '00000' = allowed; '42501' = refused by a privilege; other codes =
--           refused by the named constraint or guard; 'true' = check holds.
-- -----------------------------------------------------------------------------
DO $tests$
DECLARE
    r        record;
    v_actual text;
    v_log    jsonb := '[]'::jsonb;
BEGIN
    PERFORM set_config('search_path', 'public', true);

    BEGIN  -- sandbox: everything in here is rolled back at the end
        CREATE ROLE nc_test_nobody NOLOGIN;      -- holds only what PUBLIC holds
        FOR r IN
            SELECT *
              FROM (VALUES
    -- ===================================================================== V008 is applied
    (1, 'catalog', 'V008 is recorded in meta.schema_migrations', 'postgres', 'true',
     $q$SELECT EXISTS (SELECT 1 FROM meta.schema_migrations WHERE version = '008')$q$),
    -- revision 2 (stage S4): V009 adds two SECURITY DEFINER functions (checked by test_v009_activity_control.sql);
    -- the exact list here is limited to V001 to V008, the owner and search_path rules still cover every one.
    (2, 'catalog', 'Exactly three SECURITY DEFINER functions of V001 to V008; every one owned by nordiccart_owner, search_path pg_catalog, public, pg_temp', 'postgres', 'true',
     $q$SELECT array_agg(p.proname::text ORDER BY p.proname)
                   FILTER (WHERE p.proname NOT IN ('fn_purchase_order_lines_refresh_order', 'fn_purchase_receipt_apply'))
                 = ARRAY['fn_capture_change', 'fn_inventory_apply_movement', 'fn_inventory_apply_reservation']
               AND bool_and(p.proowner = 'nordiccart_owner'::regrole)
               AND bool_and(p.proconfig = ARRAY['search_path=pg_catalog, public, pg_temp'])
          FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
         WHERE n.nspname IN ('public', 'meta') AND p.prosecdef$q$),
    (3, 'catalog', 'No NordicCart role is superuser, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS', 'postgres', 'true',
     $q$SELECT count(*) = 7 AND NOT bool_or(rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
          FROM pg_roles WHERE rolname LIKE 'nordiccart\_%'$q$),

    -- ===================================================================== setup (as postgres)
    (10, 'setup', 'Reference data, catalogue, customer, a warehouse and a store with stock', 'postgres', '00000',
     $q$INSERT INTO currencies (currency_code, currency_name) VALUES ('XTS', 'Test currency');
        INSERT INTO countries (country_code, country_name, currency_code, vat_rate, time_zone)
        VALUES ('XA', 'Testland A', 'XTS', 0.2500, 'Europe/Stockholm');
        INSERT INTO product_categories (category_code, category_name) VALUES ('NC_TEST_CAT', 'NordicCart test category');
        INSERT INTO products (sku, product_name, brand, category_id)
        SELECT v.sku, v.name, 'TestBrand', c.category_id FROM product_categories AS c
         CROSS JOIN (VALUES ('NC-TEST-SKU-1', 'Test laptop'), ('NC-TEST-SKU-2', 'Test headphones')) AS v(sku, name)
         WHERE c.category_code = 'NC_TEST_CAT';
        INSERT INTO customers (customer_number, first_name, last_name, email, country_code, signup_channel, registered_at)
        VALUES ('NC-TEST-C1', 'Åsa', 'Testsson', 'nc.test.c1@example.invalid', 'XA', 'online', '2025-01-15 10:00+00');
        INSERT INTO stock_locations (location_code, location_name, location_type, country_code, address_line1, postal_code, city, opened_on)
        VALUES ('XA-WH-T1', 'Test warehouse', 'warehouse', 'XA', 'Testvägen 1', '12345', 'Testby', '2024-01-01'),
               ('XA-STO-T1', 'Test store', 'store', 'XA', 'Testgatan 1', '12345', 'Testby', '2024-01-01');
        INSERT INTO warehouses (stock_location_id) SELECT pg_temp.nc_loc('XA-WH-T1');
        INSERT INTO stores (stock_location_id) SELECT pg_temp.nc_loc('XA-STO-T1');
        INSERT INTO inventory_balances (stock_location_id, product_id, reorder_point)
        SELECT pg_temp.nc_loc(v.loc), pg_temp.nc_prod(v.sku), 2
          FROM (VALUES ('XA-WH-T1', 'NC-TEST-SKU-1'), ('XA-WH-T1', 'NC-TEST-SKU-2'),
                       ('XA-STO-T1', 'NC-TEST-SKU-1'), ('XA-STO-T1', 'NC-TEST-SKU-2')) AS v(loc, sku);
        INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'opening_balance', 10, '2025-01-01 00:00+00'),
               (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'opening_balance', 5, '2025-01-01 00:00+00');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),

    -- ===================================================================== loader: the work it must be able to do
    (20, 'loader allowed', 'Insert reference and master data (currency, customer)', 'nordiccart_loader', '00000',
     $q$INSERT INTO currencies (currency_code, currency_name) VALUES ('XTT', 'Second test currency');
        INSERT INTO customers (customer_number, first_name, last_name, email, country_code, signup_channel, registered_at)
        VALUES ('NC-TEST-C2', 'Bo', 'Testsson', 'nc.test.c2@example.invalid', 'XA', 'online', '2025-01-16 10:00+00')$q$),
    (21, 'loader allowed', 'Supplier receipt +4 at the warehouse (balance moved by the SECURITY DEFINER trigger)', 'nordiccart_loader', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 4, '2025-02-03 08:00+00', 'NC-TEST-PO-1/DN1')$q$),
    (22, 'loader allowed', 'Transfer out of 2 and its arrival at the store', 'nordiccart_loader', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'transfer_out', -2, '2025-02-04 06:30+00', 'NC-TEST-TR-1');
        INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at,
                                         related_movement_id, external_reference)
        SELECT pg_temp.nc_loc('XA-STO-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'transfer_in', 2, '2025-02-04 07:45+00',
               m.movement_id, 'NC-TEST-TR-1'
          FROM inventory_movements AS m WHERE m.external_reference = 'NC-TEST-TR-1' AND m.movement_type = 'transfer_out'$q$),
    (23, 'loader allowed', 'Stock after receipt and transfer: warehouse 12/0, store 2/0', 'postgres', 'true',
     $q$SELECT pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '12/0' AND pg_temp.nc_stock('XA-STO-T1', 'NC-TEST-SKU-1') = '2/0'$q$),
    (24, 'loader allowed', 'Reorder point revision (R0 / R-cal)', 'nordiccart_loader', '00000',
     $q$UPDATE inventory_balances SET reorder_point = 3
         WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1') AND product_id = pg_temp.nc_prod('NC-TEST-SKU-1')$q$),
    (25, 'loader allowed', 'Product discontinuation and customer contact change', 'nordiccart_loader', '00000',
     $q$UPDATE products SET discontinued_on = '2026-12-31' WHERE sku = 'NC-TEST-SKU-2';
        UPDATE customers SET phone = '+46700000000', email = 'nc.test.c1b@example.invalid' WHERE customer_number = 'NC-TEST-C1'$q$),
    (26, 'loader allowed', 'U1: place online order O1 (P1 x2) with its line', 'nordiccart_loader', '00000',
     $q$INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                            items_total, shipping_fee, shipping_vat_amount, order_total,
                            shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
        SELECT 'NC-TEST-O1', customer_id, 'online', pg_temp.nc_loc('XA-WH-T1'), 'XA', 'XTS', 2000.00, 0.00, 0.00, 2000.00,
               'Testgatan 5', '12345', 'Testby', '2025-03-01 10:00+00'
          FROM customers WHERE customer_number = 'NC-TEST-C1';
        INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost, line_total, vat_rate, vat_amount)
        VALUES (pg_temp.nc_order('NC-TEST-O1'), 1, pg_temp.nc_prod('NC-TEST-SKU-1'), 2, 1000.00, 600.00, 2000.00, 0.2500, 400.00);
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (27, 'loader allowed', 'U2: fn_reserve_order_stock returns reserved', 'nordiccart_loader', 'true',
     $q$SELECT fn_reserve_order_stock(pg_temp.nc_order('NC-TEST-O1'), '2025-03-01 10:00:05+00', '2025-03-01 10:15:05+00') = 'reserved'$q$),
    (28, 'loader allowed', 'After U2 the warehouse holds 12 on hand, 2 reserved', 'postgres', 'true',
     $q$SELECT pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '12/2'$q$),
    (29, 'loader allowed', 'U3a and U3c: payment attempt, authorization, capture, order confirmed', 'nordiccart_loader', '00000',
     $q$INSERT INTO payment_attempts (order_id, attempt_number, payment_method, payment_provider, amount, attempted_at)
        VALUES (pg_temp.nc_order('NC-TEST-O1'), 1, 'card', 'TestPSP', 2000.00, '2025-03-01 10:01:00+00');
        UPDATE payment_attempts SET status = 'authorized', authorized_at = '2025-03-01 10:01:30+00', provider_reference = 'NC-TEST-PSP-1'
         WHERE order_id = pg_temp.nc_order('NC-TEST-O1') AND status = 'pending';
        UPDATE payment_attempts SET status = 'captured', captured_at = '2025-03-01 10:01:40+00'
         WHERE order_id = pg_temp.nc_order('NC-TEST-O1') AND status = 'authorized';
        UPDATE orders SET status = 'confirmed', confirmed_at = '2025-03-01 10:01:40+00'
         WHERE order_id = pg_temp.nc_order('NC-TEST-O1') AND status = 'stock_reserved'$q$),
    (30, 'loader allowed', 'U5: fn_fulfil_order_stock returns fulfilled', 'nordiccart_loader', 'true',
     $q$SELECT fn_fulfil_order_stock(pg_temp.nc_order('NC-TEST-O1'), '2025-03-01 15:00:00+00') = 'fulfilled'$q$),
    (31, 'loader allowed', 'After U5 the warehouse holds 10 on hand, 0 reserved; one sale movement', 'postgres', 'true',
     $q$SELECT pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-1') = '10/0'
               AND (SELECT count(*) FROM inventory_movements AS m JOIN order_items AS i USING (order_item_id)
                     WHERE i.order_id = pg_temp.nc_order('NC-TEST-O1') AND m.movement_type = 'sale') = 1$q$),
    (32, 'loader allowed', 'Commit-time order checks run under the loader (SET CONSTRAINTS ALL IMMEDIATE)', 'nordiccart_loader', '00000',
     $q$SET CONSTRAINTS ALL IMMEDIATE; SET CONSTRAINTS ALL DEFERRED$q$),
    (33, 'loader allowed', 'U2 then U3 release: order O2 reserved, reservations released, order cancelled (customer_request)', 'nordiccart_loader', 'true',
     $q$WITH placed AS (
            INSERT INTO orders (order_number, customer_id, channel, fulfilment_location_id, country_code, currency_code,
                                items_total, shipping_fee, shipping_vat_amount, order_total,
                                shipping_address_line1, shipping_postal_code, shipping_city, placed_at)
            SELECT 'NC-TEST-O2', customer_id, 'online', pg_temp.nc_loc('XA-WH-T1'), 'XA', 'XTS', 499.00, 0.00, 0.00, 499.00,
                   'Testgatan 5', '12345', 'Testby', '2025-03-02 10:00+00'
              FROM customers WHERE customer_number = 'NC-TEST-C1' RETURNING order_id)
        SELECT count(*) = 1 FROM placed$q$),
    (34, 'loader allowed', 'The line of O2', 'nordiccart_loader', '00000',
     $q$INSERT INTO order_items (order_id, line_number, product_id, quantity, unit_price, unit_cost, line_total, vat_rate, vat_amount)
        VALUES (pg_temp.nc_order('NC-TEST-O2'), 1, pg_temp.nc_prod('NC-TEST-SKU-2'), 1, 499.00, 300.00, 499.00, 0.2500, 99.80)$q$),
    (35, 'loader allowed', 'O2: U2 reserves, then U3 releases its one reservation', 'nordiccart_loader', 'true',
     $q$SELECT fn_reserve_order_stock(pg_temp.nc_order('NC-TEST-O2'), '2025-03-02 10:00:05+00', '2025-03-02 10:15:05+00') = 'reserved'
               AND fn_close_order_reservations(pg_temp.nc_order('NC-TEST-O2'), 'released', '2025-03-02 10:05:00+00') = 1$q$),
    (36, 'loader allowed', 'Cancel O2 (customer_request) and run the commit-time checks', 'nordiccart_loader', '00000',
     $q$UPDATE orders SET status = 'cancelled', cancellation_reason = 'customer_request', cancelled_at = '2025-03-02 10:05:00+00'
         WHERE order_id = pg_temp.nc_order('NC-TEST-O2') AND status = 'stock_reserved';
        SET CONSTRAINTS ALL IMMEDIATE; SET CONSTRAINTS ALL DEFERRED$q$),
    (37, 'loader allowed', 'Count the change_log rows of the own transaction (metadata columns)', 'nordiccart_loader', 'true',
     $q$SELECT count(*) > 0 FROM change_log WHERE transaction_id = pg_current_xact_id()$q$),
    (38, 'loader allowed', 'Read migrations, business tables and its own settings', 'nordiccart_loader', 'true',
     $q$SELECT (SELECT count(*) FROM meta.schema_migrations) >= 8 AND (SELECT count(*) FROM orders) >= 2$q$),

    -- ===================================================================== loader: refused by privileges
    (40, 'loader refused', 'UPDATE inventory_balances.quantity_on_hand', 'nordiccart_loader', '42501',
     $q$UPDATE inventory_balances SET quantity_on_hand = quantity_on_hand + 1 WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1')$q$),
    (41, 'loader refused', 'UPDATE inventory_balances.quantity_reserved', 'nordiccart_loader', '42501',
     $q$UPDATE inventory_balances SET quantity_reserved = 0 WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1')$q$),
    (42, 'loader refused', 'UPDATE inventory_balances identity (product_id)', 'nordiccart_loader', '42501',
     $q$UPDATE inventory_balances SET product_id = product_id WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1')$q$),
    (43, 'loader refused', 'UPDATE orders.order_total (purchase snapshot)', 'nordiccart_loader', '42501',
     $q$UPDATE orders SET order_total = 1 WHERE order_number = 'NC-TEST-O1'$q$),
    (44, 'loader refused', 'UPDATE orders.placed_at', 'nordiccart_loader', '42501',
     $q$UPDATE orders SET placed_at = now() WHERE order_number = 'NC-TEST-O1'$q$),
    (45, 'loader refused', 'UPDATE order_items.quantity', 'nordiccart_loader', '42501',
     $q$UPDATE order_items SET quantity = 1 WHERE order_id = pg_temp.nc_order('NC-TEST-O1')$q$),
    (46, 'loader refused', 'UPDATE payment_attempts.amount', 'nordiccart_loader', '42501',
     $q$UPDATE payment_attempts SET amount = 1 WHERE order_id = pg_temp.nc_order('NC-TEST-O1')$q$),
    (47, 'loader refused', 'UPDATE inventory_reservations.quantity', 'nordiccart_loader', '42501',
     $q$UPDATE inventory_reservations SET quantity = 1 WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1')$q$),
    (48, 'loader refused', 'UPDATE inventory_movements.quantity_change', 'nordiccart_loader', '42501',
     $q$UPDATE inventory_movements SET quantity_change = 99 WHERE external_reference = 'NC-TEST-PO-1/DN1'$q$),
    (49, 'loader refused', 'UPDATE customers.registered_at', 'nordiccart_loader', '42501',
     $q$UPDATE customers SET registered_at = now() WHERE customer_number = 'NC-TEST-C1'$q$),
    (50, 'loader refused', 'UPDATE countries.vat_rate (L5: never changed)', 'nordiccart_loader', '42501',
     $q$UPDATE countries SET vat_rate = 0.30 WHERE country_code = 'XA'$q$),
    (51, 'loader refused', 'UPDATE product_prices.list_price', 'nordiccart_loader', '42501',
     $q$UPDATE product_prices SET list_price = 1 WHERE false$q$),
    (52, 'loader refused', 'DELETE an order', 'nordiccart_loader', '42501',
     $q$DELETE FROM orders WHERE order_number = 'NC-TEST-O2'$q$),
    (53, 'loader refused', 'DELETE a movement', 'nordiccart_loader', '42501',
     $q$DELETE FROM inventory_movements WHERE external_reference = 'NC-TEST-PO-1/DN1'$q$),
    (54, 'loader refused', 'DELETE a customer', 'nordiccart_loader', '42501',
     $q$DELETE FROM customers WHERE customer_number = 'NC-TEST-C2'$q$),
    (55, 'loader refused', 'TRUNCATE inventory_movements', 'nordiccart_loader', '42501',
     $q$TRUNCATE inventory_movements$q$),
    (56, 'loader refused', 'INSERT into change_log (forged change)', 'nordiccart_loader', '42501',
     $q$INSERT INTO change_log (table_name, record_key, operation, row_data) VALUES ('orders', '1', 'insert', '{}')$q$),
    (57, 'loader refused', 'Read change_log.row_data (row contents)', 'nordiccart_loader', '42501',
     $q$SELECT row_data FROM change_log LIMIT 1$q$),
    (58, 'loader refused', 'UPDATE or DELETE change_log', 'nordiccart_loader', '42501',
     $q$DELETE FROM change_log WHERE false$q$),
    (59, 'loader refused', 'Read change_log_checkpoints', 'nordiccart_loader', '42501',
     $q$SELECT count(*) FROM change_log_checkpoints$q$),
    (60, 'loader refused', 'INSERT into meta.schema_migrations', 'nordiccart_loader', '42501',
     $q$INSERT INTO meta.schema_migrations (version, description, script_name) VALUES ('999', 'x', 'x')$q$),
    (61, 'loader refused', 'CREATE TABLE in public', 'nordiccart_loader', '42501',
     $q$CREATE TABLE public.nc_test_table (x integer)$q$),
    (62, 'loader refused', 'CREATE TABLE in meta', 'nordiccart_loader', '42501',
     $q$CREATE TABLE meta.nc_test_table (x integer)$q$),
    (63, 'loader refused', 'CREATE TEMP TABLE (no TEMP privilege)', 'nordiccart_loader', '42501',
     $q$CREATE TEMP TABLE nc_test_temp (x integer)$q$),
    (64, 'loader refused', 'CREATE FUNCTION in public', 'nordiccart_loader', '42501',
     $q$CREATE FUNCTION public.nc_test_fn() RETURNS integer LANGUAGE sql AS 'SELECT 1'$q$),
    (65, 'loader refused', 'CREATE SCHEMA', 'nordiccart_loader', '42501',
     $q$CREATE SCHEMA nc_test_schema$q$),
    (66, 'loader refused', 'ALTER TABLE orders DISABLE TRIGGER ALL', 'nordiccart_loader', '42501',
     $q$ALTER TABLE orders DISABLE TRIGGER ALL$q$),
    (67, 'loader refused', 'CREATE TRIGGER on inventory_balances', 'nordiccart_loader', '42501',
     $q$CREATE TRIGGER nc_test_trg BEFORE UPDATE ON inventory_balances FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at()$q$),
    (68, 'loader refused', 'DROP TABLE orders', 'nordiccart_loader', '42501',
     $q$DROP TABLE orders$q$),
    (69, 'loader refused', 'ALTER TABLE ... OWNER TO the loader', 'nordiccart_loader', '42501',
     $q$ALTER TABLE orders OWNER TO nordiccart_loader$q$),
    -- SET ROLE is checked against the SESSION user, which is the superuser running this
    -- file; switching role is therefore tested with real logins in test_v008_security_db.py.
    (72,'loader refused', 'session_replication_role = replica (would skip every trigger)', 'nordiccart_loader', '42501',
     $q$SELECT set_config('session_replication_role', 'replica', true)$q$),
    (73, 'loader refused', 'Call the SECURITY DEFINER trigger functions directly', 'nordiccart_loader', '42501',
     $q$SELECT fn_capture_change()$q$),
    (74, 'loader refused', 'Call fn_inventory_apply_movement directly', 'nordiccart_loader', '42501',
     $q$SELECT fn_inventory_apply_movement()$q$),
    (75, 'loader refused', 'Call the migration guard', 'nordiccart_loader', '42501',
     $q$SELECT meta.assert_migration_can_run('999', NULL)$q$),
    (76, 'loader refused', 'LOCK TABLE inventory_balances IN ACCESS EXCLUSIVE MODE', 'nordiccart_loader', '42501',
     $q$LOCK TABLE inventory_balances IN ACCESS EXCLUSIVE MODE$q$),
    (77, 'loader refused', 'Read a server file', 'nordiccart_loader', '42501',
     $q$SELECT pg_read_file('postgresql.conf')$q$),
    (78, 'loader refused', 'COPY to a server file', 'nordiccart_loader', '42501',
     $q$COPY (SELECT 1) TO '/nc_test_never_written.txt'$q$),
    (79, 'loader refused', 'CREATE ROLE', 'nordiccart_loader', '42501',
     $q$CREATE ROLE nc_test_escalation$q$),
    (80, 'loader refused', 'Onward GRANT: INSERT on orders to the analyst is not granted (warning only)', 'nordiccart_loader', 'false',
     $q$SELECT pg_temp.nc_as('nordiccart_loader', 'GRANT INSERT ON orders TO nordiccart_analyst') = '00000'
               AND has_table_privilege('nordiccart_analyst', 'orders', 'INSERT')$q$),

    -- ===================================================================== scheduler: the writer's rights, nothing more
    (85, 'scheduler', 'Scheduler inserts a receipt', 'nordiccart_scheduler', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'purchase_receipt', 1, '2025-03-05 08:00+00', 'NC-TEST-PO-2/DN1')$q$),
    (86, 'scheduler', 'Scheduler cannot change quantities', 'nordiccart_scheduler', '42501',
     $q$UPDATE inventory_balances SET quantity_on_hand = 0 WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1')$q$),

    -- ===================================================================== analyst and reader: read only
    (90, 'analyst', 'Analyst reads business tables, migrations and change_log contents', 'nordiccart_analyst', 'true',
     $q$SELECT (SELECT count(*) FROM orders) >= 0 AND (SELECT count(*) FROM meta.schema_migrations) >= 8
               AND (SELECT count(*) FROM change_log WHERE row_data IS NOT NULL) >= 0$q$),
    (91, 'analyst', 'Analyst cannot insert', 'nordiccart_analyst', '42501',
     $q$INSERT INTO currencies (currency_code, currency_name) VALUES ('XTU', 'x')$q$),
    (92, 'analyst', 'Analyst cannot update or delete', 'nordiccart_analyst', '42501',
     $q$UPDATE inventory_balances SET reorder_point = 0 WHERE false$q$),
    (93, 'analyst', 'Analyst cannot run the units of work', 'nordiccart_analyst', '42501',
     $q$SELECT fn_reserve_order_stock(pg_temp.nc_order('NC-TEST-O1'), now(), now() + interval '15 minutes')$q$),
    (94, 'reader', 'Reader (group) reads; cannot create temp tables', 'nordiccart_reader', '42501',
     $q$CREATE TEMP TABLE nc_test_temp2 (x integer)$q$),

    -- ===================================================================== extractor: change_log only
    (100, 'extractor', 'Extractor reads change_log contents', 'nordiccart_extractor', 'true',
     $q$SELECT count(*) > 0 FROM change_log WHERE row_data ->> 'order_number' = 'NC-TEST-O1'$q$),
    (101, 'extractor', 'Extractor purges change_log rows (DELETE, V002 design)', 'nordiccart_extractor', '00000',
     $q$DELETE FROM change_log WHERE table_name = 'currencies' AND record_key = 'XTT'$q$),
    (102, 'extractor', 'Extractor cannot UPDATE change_log', 'nordiccart_extractor', '42501',
     $q$UPDATE change_log SET table_name = table_name WHERE false$q$),
    (103, 'extractor', 'Extractor writes and moves its checkpoint', 'nordiccart_extractor', '00000',
     $q$INSERT INTO change_log_checkpoints (consumer_name, last_snapshot) VALUES ('nc_test_consumer', pg_current_snapshot());
        UPDATE change_log_checkpoints SET last_snapshot = pg_current_snapshot() WHERE consumer_name = 'nc_test_consumer'$q$),
    (104, 'extractor', 'Extractor cannot read business tables', 'nordiccart_extractor', '42501',
     $q$SELECT count(*) FROM orders$q$),
    (105, 'extractor', 'Extractor cannot TRUNCATE change_log', 'nordiccart_extractor', '42501',
     $q$TRUNCATE change_log$q$),

    -- ===================================================================== PUBLIC (a role with no grants)
    (110, 'public', 'A role without grants cannot use schema public', 'nc_test_nobody', '42501',
     $q$SELECT count(*) FROM public.orders$q$),
    (111, 'public', 'A role without grants cannot use schema meta', 'nc_test_nobody', '42501',
     $q$SELECT count(*) FROM meta.schema_migrations$q$),
    (112, 'public', 'A role without grants cannot run the units of work', 'nc_test_nobody', '42501',
     $q$SELECT public.fn_fulfil_order_stock(1, now())$q$),
    (113, 'public', 'PUBLIC holds no CONNECT or TEMP on this database and no EXECUTE on NordicCart functions', 'postgres', 'true',
     $q$SELECT NOT has_database_privilege('public', current_database(), 'CONNECT')
               AND NOT has_database_privilege('public', current_database(), 'TEMPORARY')
               AND NOT EXISTS (SELECT 1 FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
                                WHERE n.nspname IN ('public', 'meta') AND has_function_privilege('public', p.oid, 'EXECUTE')
                                  AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_proc'::regclass
                                                     AND d.objid = p.oid AND d.deptype = 'e'))$q$),

    -- ===================================================================== search_path and SECURITY DEFINER
    (120, 'search_path', 'The loader cannot plant a lookalike function in pg_temp (no TEMP privilege)', 'nordiccart_loader', '42501',
     $q$CREATE FUNCTION pg_temp.lower(text) RETURNS text LANGUAGE sql AS 'SELECT ''hijacked'''$q$),
    (121, 'search_path', 'With that search_path, a receipt still moves the balance and is captured', 'nordiccart_loader', '00000',
     $q$SET LOCAL search_path TO pg_temp, nc_no_such_schema, public;
        INSERT INTO public.inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'purchase_receipt', 2, '2025-03-06 08:00+00', 'NC-TEST-PO-3/DN1');
        SET LOCAL search_path TO public$q$),
    (122, 'search_path', 'Balance after both later receipts: 5 + 1 + 2 = 8 on hand', 'postgres', 'true',
     $q$SELECT pg_temp.nc_stock('XA-WH-T1', 'NC-TEST-SKU-2') = '8/0'
               AND (SELECT count(*) FROM change_log WHERE table_name = 'inventory_movements'
                       AND row_data ->> 'external_reference' = 'NC-TEST-PO-3/DN1') = 1$q$),

    -- ===================================================================== the owner: existing guards still hold
    (130, 'owner', 'Even the owner cannot change quantities directly (V007 guard)', 'nordiccart_owner', '23514 inventory_balances_quantities_by_ledger',
     $q$UPDATE inventory_balances SET quantity_on_hand = quantity_on_hand + 1
         WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1') AND product_id = pg_temp.nc_prod('NC-TEST-SKU-1')$q$),
    (131, 'owner', 'Even the owner cannot delete an order (V006 guard)', 'nordiccart_owner', '23514 orders_no_delete',
     $q$DELETE FROM orders WHERE order_number = 'NC-TEST-O2'$q$),
    (132, 'owner', 'Even the owner cannot truncate the ledger (V007 guard)', 'nordiccart_owner', '23000 inventory_no_truncate',
     $q$TRUNCATE inventory_movements$q$),
    (133, 'owner', 'Even the owner cannot update change_log (V002 guard)', 'nordiccart_owner', '23000',
     $q$UPDATE change_log SET table_name = table_name WHERE change_id = (SELECT max(change_id) FROM change_log)$q$)

                   ) AS t(test_no, area, test_name, run_as, expected, sql_text)
             ORDER BY test_no
        LOOP
            IF r.expected IN ('true', 'false') THEN
                v_actual := pg_temp.nc_check_as(CASE WHEN r.run_as = 'postgres' THEN 'none' ELSE r.run_as END, r.sql_text);
            ELSE
                v_actual := pg_temp.nc_as(CASE WHEN r.run_as = 'postgres' THEN 'none' ELSE r.run_as END, r.sql_text);
            END IF;
            -- refusals: compare the SQLSTATE only when no rule name is expected
            IF r.expected = '42501' AND v_actual LIKE '42501%' THEN
                v_actual := '42501';
            END IF;
            v_log := v_log || jsonb_build_object(
                'test_no', r.test_no, 'area', r.area, 'test_name', r.test_name || ' [' || r.run_as || ']',
                'expected', r.expected, 'actual', v_actual);
        END LOOP;

        RAISE EXCEPTION 'nc_rollback_sandbox';
    EXCEPTION
        WHEN raise_exception THEN
            IF SQLERRM <> 'nc_rollback_sandbox' THEN
                RAISE;
            END IF;
    END;

    v_log := v_log || jsonb_build_object(
        'test_no', 299, 'area', 'safety',
        'test_name', 'Sandbox rolled back: no test rows, change_log rows, checkpoint or test role remain',
        'expected', 'true',
        'actual', pg_temp.nc_check_as('none', $q$
            SELECT NOT EXISTS (SELECT 1 FROM orders          WHERE order_number LIKE 'NC-TEST-%')
               AND NOT EXISTS (SELECT 1 FROM stock_locations WHERE location_code LIKE 'XA-%')
               AND NOT EXISTS (SELECT 1 FROM currencies      WHERE currency_code IN ('XTS', 'XTT'))
               AND NOT EXISTS (SELECT 1 FROM change_log_checkpoints WHERE consumer_name = 'nc_test_consumer')
               AND NOT EXISTS (SELECT 1 FROM change_log WHERE table_name = 'currencies' AND record_key IN ('XTS', 'XTT'))
               AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nc_test_nobody')$q$));

    INSERT INTO nc_test_results (test_no, area, test_name, expected, actual)
    SELECT (e ->> 'test_no')::integer, e ->> 'area', e ->> 'test_name', e ->> 'expected', e ->> 'actual'
      FROM jsonb_array_elements(v_log) AS e;
END
$tests$;

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

-- Reading the expected column: 00000 allowed; 42501 insufficient_privilege (refused
-- by V008); 23514 / 23000 with a rule name: refused by the V006 / V007 / V002 guard,
-- which still applies to the owner; true / false: the check query's result.
