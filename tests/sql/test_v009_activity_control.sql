-- =============================================================================
-- NordicCart OLTP | Behaviour tests for V009: purchase orders, reorder point
-- revisions, run ledger and checkpoints
-- -----------------------------------------------------------------------------
-- Purpose     : prove the V009 rules by doing, as nordiccart_loader (the role the
--               engine and loaders use) and, for the guards, as nordiccart_owner:
--               purchase order lifecycle (open, partially received, received),
--               receipts checked against their order (reference format, existing
--               order, ordering warehouse, not before the order, product on the
--               order, never more than ordered, duplicates), the E3 backfill (an
--               order written after its receipts takes them from the ledger and
--               must fit them), the revision chain (business-time order, chain,
--               balance match, applied at commit, idempotent reference), the run
--               protocol (start, progress, verified only with its checkpoint,
--               failed and resumed, contiguous windows, final verified runs,
--               insert-only checkpoints), refusals (42501) for every change of
--               documents, history or ledger outside the protocol, and the guards
--               that also bind the owner.
-- Connect to  : a database with V009 applied and an EMPTY run ledger (before stage
--               S6) or a test copy, as a SUPERUSER (postgres).
-- Run how     : run the whole file. Result: one table with status per test.
-- Expected    : every row has status PASS, and failed_tests = 0.
-- Safety      : the harness of the V006 to V008 tests. Everything happens inside one
--               PL/pgSQL block that always ends by raising an internal exception, so
--               nothing is committed (test 299 checks it). Test keys cannot collide
--               with real data (currency XTS, country XA, NC-TEST-*, PO-XA-*).
--               Identity sequences are not transactional: each run uses up ids.
-- Design      : docs/phase3_combined_activity_design.md 4 (L1, L2, L10), 7.2, 7.3,
--               8.3 (E3), 11.2 ("V009: behaviour tests").
-- =============================================================================

CREATE TEMP TABLE IF NOT EXISTS nc_test_results (
    test_no    integer,
    area       text,
    test_name  text,
    expected   text,
    actual     text
);

TRUNCATE nc_test_results;

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
CREATE OR REPLACE FUNCTION pg_temp.nc_po(p_number text) RETURNS bigint
LANGUAGE sql STABLE AS $fn$ SELECT purchase_order_id FROM public.purchase_orders WHERE purchase_order_number = p_number $fn$;

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
    -- ===================================================================== V009 is applied
    (1, 'catalog', 'V009 is recorded in meta.schema_migrations and the run ledger is empty', 'postgres', 'true',
     $q$SELECT EXISTS (SELECT 1 FROM meta.schema_migrations WHERE version = '009')
               AND NOT EXISTS (SELECT 1 FROM meta.activity_runs)$q$),
    (2, 'catalog', 'Five SECURITY DEFINER functions (V008 three, V009 two), owned by nordiccart_owner, fixed search_path', 'postgres', 'true',
     $q$SELECT array_agg(p.proname::text ORDER BY p.proname) = ARRAY['fn_capture_change', 'fn_inventory_apply_movement',
                    'fn_inventory_apply_reservation', 'fn_purchase_order_lines_refresh_order', 'fn_purchase_receipt_apply']
               AND bool_and(p.proowner = 'nordiccart_owner'::regrole)
               AND bool_and(p.proconfig = ARRAY['search_path=pg_catalog, public, pg_temp'])
          FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
         WHERE n.nspname IN ('public', 'meta') AND p.prosecdef$q$),
    (3, 'catalog', 'Loader: INSERT on the documents, no UPDATE except the run status columns', 'postgres', 'true',
     $q$SELECT has_table_privilege('nordiccart_loader', 'public.purchase_orders', 'INSERT')
               AND has_table_privilege('nordiccart_loader', 'public.reorder_point_revisions', 'INSERT')
               AND has_table_privilege('nordiccart_loader', 'meta.activity_checkpoints', 'INSERT')
               AND NOT has_any_column_privilege('nordiccart_loader', 'public.purchase_orders', 'UPDATE')
               AND NOT has_any_column_privilege('nordiccart_loader', 'public.purchase_order_lines', 'UPDATE')
               AND NOT has_any_column_privilege('nordiccart_loader', 'public.reorder_point_revisions', 'UPDATE')
               AND NOT has_any_column_privilege('nordiccart_loader', 'meta.activity_checkpoints', 'UPDATE')
               AND has_column_privilege('nordiccart_loader', 'meta.activity_runs', 'status', 'UPDATE')
               AND NOT has_column_privilege('nordiccart_loader', 'meta.activity_runs', 'plan_fingerprint', 'UPDATE')$q$),

    -- ===================================================================== setup (as postgres)
    (10, 'setup', 'Reference data, three products, a warehouse and a store with stock', 'postgres', '00000',
     $q$INSERT INTO currencies (currency_code, currency_name) VALUES ('XTS', 'Test currency');
        INSERT INTO countries (country_code, country_name, currency_code, vat_rate, time_zone)
        VALUES ('XA', 'Testland A', 'XTS', 0.2500, 'Europe/Stockholm');
        INSERT INTO product_categories (category_code, category_name) VALUES ('NC_TEST_CAT', 'NordicCart test category');
        INSERT INTO products (sku, product_name, brand, category_id)
        SELECT v.sku, v.name, 'TestBrand', c.category_id FROM product_categories AS c
         CROSS JOIN (VALUES ('NC-TEST-SKU-1', 'Test laptop'), ('NC-TEST-SKU-2', 'Test headphones'),
                            ('NC-TEST-SKU-3', 'Test cable (not listed)')) AS v(sku, name)
         WHERE c.category_code = 'NC_TEST_CAT';
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
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'opening_balance', 10, '2025-01-01 00:00+00');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),

    -- ===================================================================== purchase orders and receipts (loader)
    (20, 'purchase orders', 'Order PO-XA-2025-000001 with two lines (reorder_point, launch)', 'nordiccart_loader', '00000',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-XA-2025-000001', pg_temp.nc_loc('XA-WH-T1'), '2025-02-03 08:00+00');
        INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, order_reason, quantity_ordered)
        VALUES (pg_temp.nc_po('PO-XA-2025-000001'), 1, pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'reorder_point', 6),
               (pg_temp.nc_po('PO-XA-2025-000001'), 2, pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'launch', 4);
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (21, 'purchase orders', 'New order: open, lines open, outstanding 6 and 4', 'nordiccart_loader', 'true',
     $q$SELECT (SELECT status = 'open' AND received_at IS NULL FROM purchase_orders WHERE purchase_order_number = 'PO-XA-2025-000001')
               AND (SELECT array_agg(status::text || ':' || quantity_received || ':' || quantity_outstanding ORDER BY line_number)
                      FROM purchase_order_lines WHERE purchase_order_id = pg_temp.nc_po('PO-XA-2025-000001'))
                   = ARRAY['open:0:6', 'open:0:4']$q$),
    (22, 'purchase orders', 'Delivery note DN1: 4 of line 1', 'nordiccart_loader', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 4, '2025-02-10 08:00+00', 'PO-XA-2025-000001/DN1')$q$),
    (23, 'purchase orders', 'Line 1 partially received (4, outstanding 2); order partially_received; stock +4', 'nordiccart_loader', 'true',
     $q$SELECT (SELECT status = 'partially_received' FROM purchase_orders WHERE purchase_order_number = 'PO-XA-2025-000001')
               AND (SELECT status::text || ':' || quantity_received || ':' || quantity_outstanding FROM purchase_order_lines
                     WHERE purchase_order_id = pg_temp.nc_po('PO-XA-2025-000001') AND line_number = 1) = 'partially_received:4:2'
               AND (SELECT quantity_on_hand FROM inventory_balances
                     WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1') AND product_id = pg_temp.nc_prod('NC-TEST-SKU-1')) = 14$q$),
    (24, 'purchase orders', 'DN1 for line 2 (4) and DN2 for the rest of line 1 (2)', 'nordiccart_loader', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'purchase_receipt', 4, '2025-02-10 08:00+00', 'PO-XA-2025-000001/DN1'),
               (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 2, '2025-02-12 09:30+00', 'PO-XA-2025-000001/DN2')$q$),
    (25, 'purchase orders', 'Order received; received_at = the last delivery note; nothing outstanding', 'nordiccart_loader', 'true',
     $q$SELECT (SELECT status = 'received' AND received_at = '2025-02-12 09:30+00'
                  FROM purchase_orders WHERE purchase_order_number = 'PO-XA-2025-000001')
               AND (SELECT bool_and(status = 'received' AND quantity_outstanding = 0) FROM purchase_order_lines
                     WHERE purchase_order_id = pg_temp.nc_po('PO-XA-2025-000001'))$q$),
    (26, 'receipts', 'A third delivery note would exceed the ordered quantity', 'nordiccart_loader', '23514 purchase_receipt_within_ordered',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 1, '2025-02-13 08:00+00', 'PO-XA-2025-000001/DN3')$q$),
    (27, 'receipts', 'Receipt for an order that does not exist', 'nordiccart_loader', '23514 purchase_receipt_requires_purchase_order',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 1, '2025-02-13 08:00+00', 'PO-XA-2025-000099/DN1')$q$),
    (28, 'receipts', 'Malformed purchase order reference', 'nordiccart_loader', '23514 purchase_receipt_reference_format',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 1, '2025-02-13 08:00+00', 'PO-XA-2025-1/DN1')$q$),
    (29, 'purchase orders', 'Second order PO-XA-2025-000002 (5 of product 1), ordered 2025-03-03', 'nordiccart_loader', '00000',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-XA-2025-000002', pg_temp.nc_loc('XA-WH-T1'), '2025-03-03 08:00+00');
        INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, order_reason, quantity_ordered)
        VALUES (pg_temp.nc_po('PO-XA-2025-000002'), 1, pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'seasonal', 5)$q$),
    (30, 'receipts', 'Receipt at another location than the ordering warehouse', 'nordiccart_loader', '23514 purchase_receipt_ordering_warehouse',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-STO-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 1, '2025-03-10 08:00+00', 'PO-XA-2025-000002/DN1')$q$),
    (31, 'receipts', 'Receipt dated before the order', 'nordiccart_loader', '23514 purchase_receipt_after_order',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 1, '2025-03-01 08:00+00', 'PO-XA-2025-000002/DN1')$q$),
    (32, 'receipts', 'Receipt of a product that is not on the order', 'nordiccart_loader', '23514 purchase_receipt_product_ordered',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'purchase_receipt', 1, '2025-03-10 08:00+00', 'PO-XA-2025-000002/DN1')$q$),
    (33, 'receipts', 'Delivery note DN1 of order 2: 2 units', 'nordiccart_loader', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 2, '2025-03-10 08:00+00', 'PO-XA-2025-000002/DN1')$q$),
    (34, 'receipts', 'The same delivery note line again is refused (V007 idempotency key)', 'nordiccart_loader', '23505 uq_inventory_movements_document_line',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 2, '2025-03-10 08:00+00', 'PO-XA-2025-000002/DN1')$q$),
    (35, 'receipts', 'A receipt with a non-PO document reference is still accepted (V007 behaviour; script 18 reports it)', 'nordiccart_loader', '00000',
     $q$INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'purchase_receipt', 1, '2025-03-11 08:00+00', 'NC-TEST-DN-9')$q$),

    (40, 'order rules', 'Duplicate order number', 'nordiccart_loader', '23505 uq_purchase_orders_number',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-XA-2025-000001', pg_temp.nc_loc('XA-WH-T1'), '2025-04-01 08:00+00')$q$),
    (41, 'order rules', 'Number of another market', 'nordiccart_loader', '23514 purchase_orders_number_market',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-SE-2025-900003', pg_temp.nc_loc('XA-WH-T1'), '2025-04-01 08:00+00')$q$),
    (42, 'order rules', 'Number of another year than the local order year', 'nordiccart_loader', '23514 purchase_orders_number_year',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-XA-2024-000003', pg_temp.nc_loc('XA-WH-T1'), '2025-04-01 08:00+00')$q$),
    (43, 'order rules', 'Order placed by a store (only warehouses order)', 'nordiccart_loader', '23503 fk_purchase_orders_warehouse',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-XA-2025-000003', pg_temp.nc_loc('XA-STO-T1'), '2025-04-01 08:00+00')$q$),
    (44, 'order rules', 'An order inserted as received', 'nordiccart_loader', '23514 purchase_orders_insert_open',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at, status, received_at)
        VALUES ('PO-XA-2025-000003', pg_temp.nc_loc('XA-WH-T1'), '2025-04-01 08:00+00', 'received', '2025-04-02 08:00+00')$q$),
    (45, 'order rules', 'An order without lines (commit time)', 'nordiccart_loader', '23514 purchase_orders_have_lines',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-XA-2025-000003', pg_temp.nc_loc('XA-WH-T1'), '2025-04-01 08:00+00');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (46, 'order rules', 'Line with quantity 0', 'nordiccart_loader', '23514 ck_purchase_order_lines_ordered',
     $q$INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, order_reason, quantity_ordered)
        VALUES (pg_temp.nc_po('PO-XA-2025-000002'), 2, pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'reorder_point', 0)$q$),
    (47, 'order rules', 'Line for a product not listed at the warehouse', 'nordiccart_loader', '23503 fk_purchase_order_lines_balance',
     $q$INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, order_reason, quantity_ordered)
        VALUES (pg_temp.nc_po('PO-XA-2025-000002'), 2, pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-3'), 'launch', 2)$q$),
    (48, 'order rules', 'Line at another location than its order', 'nordiccart_loader', '23503 fk_purchase_order_lines_order',
     $q$INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, order_reason, quantity_ordered)
        VALUES (pg_temp.nc_po('PO-XA-2025-000002'), 2, pg_temp.nc_loc('XA-STO-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'reorder_point', 2)$q$),
    (49, 'order rules', 'Line added to a completely received order', 'nordiccart_loader', '23514 purchase_order_lines_order_received',
     $q$INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, order_reason, quantity_ordered)
        VALUES (pg_temp.nc_po('PO-XA-2025-000001'), 3, pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'reorder_point', 1)$q$),

    -- ===================================================================== E3: documents after their receipts
    (50, 'backfill E3', 'Receipt recorded before V009 knew its order (simulated: receipt trigger disabled)', 'postgres', '00000',
     $q$ALTER TABLE inventory_movements DISABLE TRIGGER trg_inventory_movements_purchase_order_receipt;
        INSERT INTO inventory_movements (stock_location_id, product_id, movement_type, quantity_change, occurred_at, external_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'purchase_receipt', 3, '2025-04-02 08:00+00', 'PO-XA-2025-000004/DN1'),
               (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'purchase_receipt', 5, '2025-04-02 08:00+00', 'PO-XA-2025-000005/DN1'),
               (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'purchase_receipt', 1, '2025-04-02 08:00+00', 'PO-XA-2025-000006/DN1'),
               (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'purchase_receipt', 1, '2025-04-02 08:00+00', 'PO-XA-2025-000006/DN1');
        ALTER TABLE inventory_movements ENABLE TRIGGER trg_inventory_movements_purchase_order_receipt$q$),
    (51, 'backfill E3', 'Backfill PO-XA-2025-000004 (3 ordered): the line takes its receipt from the ledger', 'nordiccart_loader', '00000',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-XA-2025-000004', pg_temp.nc_loc('XA-WH-T1'), '2025-04-01 08:00+00');
        INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, order_reason, quantity_ordered)
        VALUES (pg_temp.nc_po('PO-XA-2025-000004'), 1, pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'forced_reorder', 3);
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (52, 'backfill E3', 'Backfilled order received at its receipt time; no movement added', 'nordiccart_loader', 'true',
     $q$SELECT (SELECT status = 'received' AND received_at = '2025-04-02 08:00+00' FROM purchase_orders
                 WHERE purchase_order_number = 'PO-XA-2025-000004')
               AND (SELECT quantity_received = 3 AND status = 'received' FROM purchase_order_lines
                     WHERE purchase_order_id = pg_temp.nc_po('PO-XA-2025-000004'))
               AND (SELECT count(*) FROM inventory_movements WHERE external_reference LIKE 'PO-XA-2025-000004/%') = 1$q$),
    (53, 'backfill E3', 'Backfill with fewer units ordered than already received', 'nordiccart_loader', '23514 purchase_order_lines_received_exceeds_ordered',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-XA-2025-000005', pg_temp.nc_loc('XA-WH-T1'), '2025-04-01 08:00+00');
        INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, order_reason, quantity_ordered)
        VALUES (pg_temp.nc_po('PO-XA-2025-000005'), 1, pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'forced_reorder', 4)$q$),
    (54, 'backfill E3', 'Backfill that leaves a received product without a line (commit time)', 'nordiccart_loader', '23514 purchase_orders_receipts_fit_lines',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-XA-2025-000006', pg_temp.nc_loc('XA-WH-T1'), '2025-04-01 08:00+00');
        INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, order_reason, quantity_ordered)
        VALUES (pg_temp.nc_po('PO-XA-2025-000006'), 1, pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'forced_reorder', 1);
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (55, 'backfill E3', 'Backfill dated after its receipts, order partly received (commit time)', 'nordiccart_loader', '23514 purchase_orders_receipts_fit_lines',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-XA-2025-000005', pg_temp.nc_loc('XA-WH-T1'), '2025-04-05 08:00+00');
        INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, order_reason, quantity_ordered)
        VALUES (pg_temp.nc_po('PO-XA-2025-000005'), 1, pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'forced_reorder', 6);
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (56, 'backfill E3', 'Backfill dated after its receipts, order complete (received before ordered)', 'nordiccart_loader', '23514 ck_purchase_orders_received_after_order',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-XA-2025-000005', pg_temp.nc_loc('XA-WH-T1'), '2025-04-05 08:00+00');
        INSERT INTO purchase_order_lines (purchase_order_id, line_number, stock_location_id, product_id, order_reason, quantity_ordered)
        VALUES (pg_temp.nc_po('PO-XA-2025-000005'), 1, pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'forced_reorder', 5)$q$),

    -- ===================================================================== unauthorized changes (loader) and guards (owner)
    (60, 'refused', 'Loader sets an order status', 'nordiccart_loader', '42501',
     $q$UPDATE purchase_orders SET status = 'open' WHERE purchase_order_number = 'PO-XA-2025-000001'$q$),
    (61, 'refused', 'Loader changes a received quantity', 'nordiccart_loader', '42501',
     $q$UPDATE purchase_order_lines SET quantity_received = 0 WHERE purchase_order_id = pg_temp.nc_po('PO-XA-2025-000001')$q$),
    (62, 'refused', 'Loader changes an ordered quantity', 'nordiccart_loader', '42501',
     $q$UPDATE purchase_order_lines SET quantity_ordered = 99 WHERE purchase_order_id = pg_temp.nc_po('PO-XA-2025-000001')$q$),
    (63, 'refused', 'Loader deletes an order', 'nordiccart_loader', '42501',
     $q$DELETE FROM purchase_orders WHERE purchase_order_number = 'PO-XA-2025-000001'$q$),
    (64, 'refused', 'Loader truncates order lines', 'nordiccart_loader', '42501',
     $q$TRUNCATE purchase_order_lines$q$),
    (65, 'refused', 'Loader changes a reorder point revision', 'nordiccart_loader', '42501',
     $q$UPDATE reorder_point_revisions SET new_reorder_point = new_reorder_point + 1$q$),
    (66, 'refused', 'Loader deletes checkpoints', 'nordiccart_loader', '42501',
     $q$DELETE FROM meta.activity_checkpoints$q$),
    (67, 'refused', 'Loader calls the order refresh function', 'nordiccart_loader', '42501',
     $q$SELECT public.fn_purchase_order_refresh(pg_temp.nc_po('PO-XA-2025-000001'))$q$),
    (68, 'refused', 'Loader calls the receipt trigger function', 'nordiccart_loader', '42501',
     $q$SELECT public.fn_purchase_receipt_apply()$q$),
    (70, 'owner guards', 'Owner sets a received quantity other than the receipts', 'nordiccart_owner', '23514 purchase_order_lines_received_from_receipts',
     $q$UPDATE purchase_order_lines SET quantity_received = 0, status = 'open'
         WHERE purchase_order_id = pg_temp.nc_po('PO-XA-2025-000001') AND line_number = 1$q$),
    (71, 'owner guards', 'Owner sets an order status other than its receipts', 'nordiccart_owner', '23514 purchase_orders_status_from_receipts',
     $q$UPDATE purchase_orders SET status = 'open', received_at = NULL WHERE purchase_order_number = 'PO-XA-2025-000001'$q$),
    (72, 'owner guards', 'Owner changes the order time', 'nordiccart_owner', '23514 purchase_orders_identity_immutable',
     $q$UPDATE purchase_orders SET ordered_at = ordered_at - interval '1 day' WHERE purchase_order_number = 'PO-XA-2025-000001'$q$),
    (73, 'owner guards', 'Owner changes an ordered quantity', 'nordiccart_owner', '23514 purchase_order_lines_terms_immutable',
     $q$UPDATE purchase_order_lines SET quantity_ordered = 99 WHERE purchase_order_id = pg_temp.nc_po('PO-XA-2025-000002')$q$),
    (74, 'owner guards', 'Owner deletes an order', 'nordiccart_owner', '23000',
     $q$DELETE FROM purchase_orders WHERE purchase_order_number = 'PO-XA-2025-000002'$q$),
    (75, 'owner guards', 'Owner truncates the revision history', 'nordiccart_owner', '23000',
     $q$TRUNCATE reorder_point_revisions$q$),

    -- ===================================================================== reorder point revisions (C18)
    (80, 'revisions', 'Monthly review: balance 2 -> 5 and its revision in one transaction (commit check)', 'nordiccart_loader', '00000',
     $q$UPDATE inventory_balances SET reorder_point = 5
         WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1') AND product_id = pg_temp.nc_prod('NC-TEST-SKU-1');
        INSERT INTO reorder_point_revisions (stock_location_id, product_id, revision_kind, effective_at, old_reorder_point,
                                             new_reorder_point, document_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'monthly_review', '2025-02-28 06:00+00', 2, 5,
                'RCAL-XA-WH-T1-202502');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (81, 'revisions', 'Revision dated before the previous one', 'nordiccart_loader', '23514 reorder_point_revisions_business_time_order',
     $q$INSERT INTO reorder_point_revisions (stock_location_id, product_id, revision_kind, effective_at, old_reorder_point,
                                             new_reorder_point, document_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'monthly_review', '2025-02-01 06:00+00', 5, 6,
                'RCAL-XA-WH-T1-202501')$q$),
    (82, 'revisions', 'Revision whose old value is not the previous new value', 'nordiccart_loader', '23514 reorder_point_revisions_chain',
     $q$INSERT INTO reorder_point_revisions (stock_location_id, product_id, revision_kind, effective_at, old_reorder_point,
                                             new_reorder_point, document_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'monthly_review', '2025-03-31 06:00+00', 4, 6,
                'RCAL-XA-WH-T1-202503')$q$),
    (83, 'revisions', 'Revision of values the balance never held', 'nordiccart_loader', '23514 reorder_point_revisions_match_balance',
     $q$INSERT INTO reorder_point_revisions (stock_location_id, product_id, revision_kind, effective_at, old_reorder_point,
                                             new_reorder_point, document_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-2'), 'monthly_review', '2025-02-28 06:00+00', 3, 4,
                'RCAL-XA-WH-T1-202502')$q$),
    (84, 'revisions', 'Revision recorded but the balance not updated (commit time)', 'nordiccart_loader', '23514 reorder_point_revisions_applied',
     $q$INSERT INTO reorder_point_revisions (stock_location_id, product_id, revision_kind, effective_at, old_reorder_point,
                                             new_reorder_point, document_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'monthly_review', '2025-03-31 06:00+00', 5, 8,
                'RCAL-XA-WH-T1-202503');
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (85, 'revisions', 'The same revision document twice for a listing', 'nordiccart_loader', '23505 uq_reorder_point_revisions_document',
     $q$INSERT INTO reorder_point_revisions (stock_location_id, product_id, revision_kind, effective_at, old_reorder_point,
                                             new_reorder_point, document_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'monthly_review', '2025-04-30 06:00+00', 5, 6,
                'RCAL-XA-WH-T1-202502')$q$),
    (86, 'revisions', 'A revision that changes nothing', 'nordiccart_loader', '23514 ck_reorder_point_revisions_changed',
     $q$INSERT INTO reorder_point_revisions (stock_location_id, product_id, revision_kind, effective_at, old_reorder_point,
                                             new_reorder_point, document_reference)
        VALUES (pg_temp.nc_loc('XA-WH-T1'), pg_temp.nc_prod('NC-TEST-SKU-1'), 'monthly_review', '2025-04-30 06:00+00', 5, 5,
                'RCAL-XA-WH-T1-202504')$q$),
    (87, 'revisions', 'Owner changes a revision (insert-only)', 'nordiccart_owner', '23000',
     $q$UPDATE reorder_point_revisions SET effective_at = effective_at WHERE document_reference = 'RCAL-XA-WH-T1-202502'$q$),
    (88, 'revisions', 'A reorder point change without a revision is still accepted (V007 behaviour) and shows as drift', 'nordiccart_loader', 'true',
     $q$WITH upd AS (UPDATE inventory_balances SET reorder_point = 9
                       WHERE stock_location_id = pg_temp.nc_loc('XA-WH-T1') AND product_id = pg_temp.nc_prod('NC-TEST-SKU-1')
                     RETURNING reorder_point)
        SELECT (SELECT reorder_point FROM upd) = 9$q$),
    (89, 'revisions', 'Drift detection (script 18 check 601): the listing differs from its latest revision', 'nordiccart_loader', 'true',
     $q$SELECT (SELECT b.reorder_point FROM inventory_balances b
                 WHERE b.stock_location_id = pg_temp.nc_loc('XA-WH-T1') AND b.product_id = pg_temp.nc_prod('NC-TEST-SKU-1'))
               <> (SELECT r.new_reorder_point FROM reorder_point_revisions r
                    WHERE r.stock_location_id = pg_temp.nc_loc('XA-WH-T1') AND r.product_id = pg_temp.nc_prod('NC-TEST-SKU-1')
                    ORDER BY r.effective_at DESC LIMIT 1)$q$),

    -- ===================================================================== run ledger and checkpoints (design 7.3)
    (100, 'runs', 'Start window 2025-01-01 (running)', 'nordiccart_loader', '00000',
     $q$INSERT INTO meta.activity_runs (run_mode, window_start, window_end, plan_fingerprint, config_fingerprint, start_state_hash, planned_units)
        VALUES ('catch_up', '2025-01-01 00:00+00', '2025-01-02 00:00+00', repeat('a', 64), repeat('b', 64), repeat('c', 64), 10)$q$),
    (101, 'runs', 'A second window while the first is running', 'nordiccart_loader', '23514 activity_runs_previous_verified',
     $q$INSERT INTO meta.activity_runs (run_mode, window_start, window_end, plan_fingerprint, config_fingerprint, start_state_hash, planned_units)
        VALUES ('catch_up', '2025-01-02 00:00+00', '2025-01-03 00:00+00', repeat('a', 64), repeat('b', 64), repeat('c', 64), 10)$q$),
    (102, 'runs', 'Loader changes the plan fingerprint of a running window', 'nordiccart_loader', '42501',
     $q$UPDATE meta.activity_runs SET plan_fingerprint = repeat('d', 64) WHERE window_start = '2025-01-01 00:00+00'$q$),
    (103, 'runs', 'Owner changes the plan fingerprint (a changed plan stops the run)', 'nordiccart_owner', '23514 activity_runs_identity_immutable',
     $q$UPDATE meta.activity_runs SET plan_fingerprint = repeat('d', 64) WHERE window_start = '2025-01-01 00:00+00'$q$),
    (104, 'runs', 'Progress: 5 of 10 units done', 'nordiccart_loader', '00000',
     $q$UPDATE meta.activity_runs SET completed_units = 5, unit_counts = '{"place": 5}' WHERE window_start = '2025-01-01 00:00+00'$q$),
    (105, 'runs', 'Completed units decrease', 'nordiccart_loader', '23514 activity_runs_progress_forward',
     $q$UPDATE meta.activity_runs SET completed_units = 3 WHERE window_start = '2025-01-01 00:00+00'$q$),
    (106, 'runs', 'Verified without a checkpoint (commit time)', 'nordiccart_loader', '23514 activity_runs_verified_with_checkpoint',
     $q$UPDATE meta.activity_runs SET status = 'verified', completed_units = 10, end_state_hash = repeat('e', 64), finished_at = now()
         WHERE window_start = '2025-01-01 00:00+00';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (107, 'runs', 'Verified with incomplete units', 'nordiccart_loader', '23514 ck_activity_runs_verified',
     $q$UPDATE meta.activity_runs SET status = 'verified', end_state_hash = repeat('e', 64), finished_at = now()
         WHERE window_start = '2025-01-01 00:00+00'$q$),
    (108, 'runs', 'Verified with its checkpoint in one transaction (protocol step 7)', 'nordiccart_loader', '00000',
     $q$UPDATE meta.activity_runs SET status = 'verified', completed_units = 10, end_state_hash = repeat('e', 64), finished_at = now()
         WHERE window_start = '2025-01-01 00:00+00';
        INSERT INTO meta.activity_checkpoints (activity_run_id, state_as_of, state_hash, state_snapshot)
        SELECT activity_run_id, window_end, repeat('e', 64), '{"balances": []}' FROM meta.activity_runs
         WHERE window_start = '2025-01-01 00:00+00';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (109, 'runs', 'A second checkpoint for the same run', 'nordiccart_loader', '23505 uq_activity_checkpoints_run',
     $q$INSERT INTO meta.activity_checkpoints (activity_run_id, state_as_of, state_hash, state_snapshot)
        SELECT activity_run_id, window_end, repeat('e', 64), '{}' FROM meta.activity_runs WHERE window_start = '2025-01-01 00:00+00'$q$),
    (110, 'runs', 'A verified run is final', 'nordiccart_loader', '23514 activity_runs_verified_final',
     $q$UPDATE meta.activity_runs SET unit_counts = '{"place": 9}' WHERE window_start = '2025-01-01 00:00+00'$q$),
    (111, 'runs', 'Its monthly recovery point is recorded once', 'nordiccart_loader', '00000',
     $q$UPDATE meta.activity_runs SET recovery_point = 'nordiccart_oltp_202501_activity_recovery_test.dump', recovery_point_sha256 = repeat('f', 64)
         WHERE window_start = '2025-01-01 00:00+00'$q$),
    (112, 'runs', 'The recovery point cannot be changed afterwards', 'nordiccart_loader', '23514 activity_runs_verified_final',
     $q$UPDATE meta.activity_runs SET recovery_point = 'other.dump' WHERE window_start = '2025-01-01 00:00+00'$q$),
    (113, 'runs', 'Checkpoints are insert-only (loader has no UPDATE)', 'nordiccart_loader', '42501',
     $q$UPDATE meta.activity_checkpoints SET state_hash = repeat('0', 64)$q$),
    (114, 'runs', 'Checkpoints are insert-only (owner: guard)', 'nordiccart_owner', '23000',
     $q$UPDATE meta.activity_checkpoints SET state_hash = repeat('0', 64)$q$),
    (115, 'runs', 'Next window with a gap', 'nordiccart_loader', '23514 activity_runs_contiguous',
     $q$INSERT INTO meta.activity_runs (run_mode, window_start, window_end, plan_fingerprint, config_fingerprint, start_state_hash, planned_units)
        VALUES ('catch_up', '2025-01-03 00:00+00', '2025-01-04 00:00+00', repeat('a', 64), repeat('b', 64), repeat('e', 64), 4)$q$),
    (116, 'runs', 'Invalid fingerprint format', 'nordiccart_loader', '23514 ck_activity_runs_plan_fingerprint',
     $q$INSERT INTO meta.activity_runs (run_mode, window_start, window_end, plan_fingerprint, config_fingerprint, start_state_hash, planned_units)
        VALUES ('catch_up', '2025-01-02 00:00+00', '2025-01-03 00:00+00', 'not-a-hash', repeat('b', 64), repeat('e', 64), 4)$q$),
    (117, 'runs', 'Next window, contiguous', 'nordiccart_loader', '00000',
     $q$INSERT INTO meta.activity_runs (run_mode, window_start, window_end, plan_fingerprint, config_fingerprint, start_state_hash, planned_units)
        VALUES ('catch_up', '2025-01-02 00:00+00', '2025-01-03 00:00+00', repeat('a', 64), repeat('b', 64), repeat('e', 64), 4)$q$),
    (118, 'runs', 'Checkpoint not as of the window end', 'nordiccart_loader', '23514 activity_checkpoints_window_end',
     $q$INSERT INTO meta.activity_checkpoints (activity_run_id, state_as_of, state_hash, state_snapshot)
        SELECT activity_run_id, window_end - interval '1 hour', repeat('1', 64), '{}' FROM meta.activity_runs
         WHERE window_start = '2025-01-02 00:00+00'$q$),
    (119, 'runs', 'Checkpoint while the run is not verified (commit time)', 'nordiccart_loader', '23514 activity_checkpoints_run_verified',
     $q$INSERT INTO meta.activity_checkpoints (activity_run_id, state_as_of, state_hash, state_snapshot)
        SELECT activity_run_id, window_end, repeat('1', 64), '{}' FROM meta.activity_runs WHERE window_start = '2025-01-02 00:00+00';
        SET CONSTRAINTS ALL IMMEDIATE;
        SET CONSTRAINTS ALL DEFERRED$q$),
    (120, 'runs', 'The run fails (verification FAIL)', 'nordiccart_loader', '00000',
     $q$UPDATE meta.activity_runs SET status = 'failed', failure_reason = 'test: verification failed', finished_at = now()
         WHERE window_start = '2025-01-02 00:00+00'$q$),
    (121, 'runs', 'No checkpoint for a failed run', 'nordiccart_loader', '23514 activity_checkpoints_run_not_failed',
     $q$INSERT INTO meta.activity_checkpoints (activity_run_id, state_as_of, state_hash, state_snapshot)
        SELECT activity_run_id, window_end, repeat('1', 64), '{}' FROM meta.activity_runs WHERE window_start = '2025-01-02 00:00+00'$q$),
    (122, 'runs', 'A failed run cannot jump to verified', 'nordiccart_loader', '23514 activity_runs_status_transition',
     $q$UPDATE meta.activity_runs SET status = 'verified' WHERE window_start = '2025-01-02 00:00+00'$q$),
    (123, 'runs', 'No new window while a run has failed', 'nordiccart_loader', '23514 activity_runs_previous_verified',
     $q$INSERT INTO meta.activity_runs (run_mode, window_start, window_end, plan_fingerprint, config_fingerprint, start_state_hash, planned_units)
        VALUES ('catch_up', '2025-01-03 00:00+00', '2025-01-04 00:00+00', repeat('a', 64), repeat('b', 64), repeat('1', 64), 4)$q$),
    (124, 'runs', 'Resume after a fix: failed -> running', 'nordiccart_loader', '00000',
     $q$UPDATE meta.activity_runs SET status = 'running', failure_reason = NULL, finished_at = NULL
         WHERE window_start = '2025-01-02 00:00+00'$q$),
    (125, 'runs', 'Owner deletes a run', 'nordiccart_owner', '23000',
     $q$DELETE FROM meta.activity_runs WHERE window_start = '2025-01-02 00:00+00'$q$),
    (126, 'runs', 'Run chain: the first run has no predecessor, the next run names it (set by the guard)', 'nordiccart_loader', 'true',
     $q$SELECT (SELECT previous_run_id IS NULL FROM meta.activity_runs WHERE window_start = '2025-01-01 00:00+00')
               AND (SELECT previous_run_id FROM meta.activity_runs WHERE window_start = '2025-01-02 00:00+00')
                   = (SELECT activity_run_id FROM meta.activity_runs WHERE window_start = '2025-01-01 00:00+00')$q$),
    (127, 'runs', 'Owner changes a predecessor link', 'nordiccart_owner', '23514 activity_runs_identity_immutable',
     $q$UPDATE meta.activity_runs SET previous_run_id = NULL WHERE window_start = '2025-01-02 00:00+00'$q$),

    -- ===================================================================== other roles and change capture
    (130, 'roles', 'Analyst reads the five tables', 'nordiccart_analyst', 'true',
     $q$SELECT (SELECT count(*) FROM purchase_orders) >= 0 AND (SELECT count(*) FROM purchase_order_lines) >= 0
               AND (SELECT count(*) FROM reorder_point_revisions) >= 0 AND (SELECT count(*) FROM meta.activity_runs) >= 0
               AND (SELECT count(*) FROM meta.activity_checkpoints) >= 0$q$),
    (131, 'roles', 'Reader cannot write an order', 'nordiccart_reader', '42501',
     $q$INSERT INTO purchase_orders (purchase_order_number, stock_location_id, ordered_at)
        VALUES ('PO-XA-2025-000009', pg_temp.nc_loc('XA-WH-T1'), '2025-05-01 08:00+00')$q$),
    (132, 'roles', 'Extractor cannot read purchase orders', 'nordiccart_extractor', '42501',
     $q$SELECT count(*) FROM purchase_orders$q$),
    (133, 'roles', 'PUBLIC cannot read the run ledger', 'nc_test_nobody', '42501',
     $q$SELECT count(*) FROM meta.activity_runs$q$),
    (134, 'capture', 'Order, line and revision writes are captured in change_log; runs and checkpoints are not', 'postgres', 'true',
     $q$SELECT EXISTS (SELECT 1 FROM change_log WHERE table_name = 'purchase_orders' AND row_data ->> 'purchase_order_number' = 'PO-XA-2025-000001'
                         AND operation = 'update' AND row_data ->> 'status' = 'received')
               AND EXISTS (SELECT 1 FROM change_log WHERE table_name = 'purchase_order_lines'
                            AND (row_data ->> 'purchase_order_id')::bigint = pg_temp.nc_po('PO-XA-2025-000001'))
               AND EXISTS (SELECT 1 FROM change_log WHERE table_name = 'reorder_point_revisions'
                            AND row_data ->> 'document_reference' = 'RCAL-XA-WH-T1-202502')
               AND NOT EXISTS (SELECT 1 FROM change_log WHERE table_name IN ('activity_runs', 'activity_checkpoints'))$q$)

                   ) AS t(test_no, area, test_name, run_as, expected, sql_text)
             ORDER BY test_no
        LOOP
            IF r.expected IN ('true', 'false') THEN
                v_actual := pg_temp.nc_check_as(CASE WHEN r.run_as = 'postgres' THEN 'none' ELSE r.run_as END, r.sql_text);
            ELSE
                v_actual := pg_temp.nc_as(CASE WHEN r.run_as = 'postgres' THEN 'none' ELSE r.run_as END, r.sql_text);
            END IF;
            IF r.expected IN ('42501', '23000') AND v_actual LIKE r.expected || '%' THEN
                v_actual := r.expected;
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
        'test_name', 'Sandbox rolled back: no test orders, revisions, runs, movements or test role remain',
        'expected', 'true',
        'actual', pg_temp.nc_check_as('none', $q$
            SELECT NOT EXISTS (SELECT 1 FROM purchase_orders WHERE purchase_order_number LIKE 'PO-XA-%')
               AND NOT EXISTS (SELECT 1 FROM reorder_point_revisions WHERE document_reference LIKE 'RCAL-XA-%')
               AND NOT EXISTS (SELECT 1 FROM meta.activity_runs WHERE window_start = '2025-01-01 00:00+00')
               AND NOT EXISTS (SELECT 1 FROM inventory_movements WHERE external_reference LIKE 'PO-XA-%')
               AND NOT EXISTS (SELECT 1 FROM stock_locations WHERE location_code LIKE 'XA-%')
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

-- Reading the expected column: 00000 allowed; 42501 insufficient_privilege (refused by
-- the V008 roles); 23514 / 23503 / 23505 with a rule or constraint name: refused by that
-- V009 (or V007) rule; 23000: an insert-only / no-delete guard, which binds the owner too;
-- true / false: the check query's result.
