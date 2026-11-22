-- =============================================================================
-- NordicCart OLTP | Behaviour tests for the foundational tables (V002-V005)
-- -----------------------------------------------------------------------------
-- Purpose     : prove that the keys, constraints and triggers REJECT bad data
--               and ACCEPT good data, not only that they exist.
-- Connect to  : nordiccart_oltp, as the database owner
-- Run how     : run the whole file. Result: one table with status per test.
-- Expected    : every row has status PASS, and failed_tests = 0.
-- -----------------------------------------------------------------------------
-- Safety
--   * Every test row is written inside one PL/pgSQL block that always ends by
--     raising an internal exception, which rolls the whole block back. Nothing
--     is committed to any business table or to change_log. The last test checks
--     this after the rollback.
--   * Test rows use codes reserved for testing (currency XTS, user-assigned
--     country code XA), so they cannot collide with real reference data.
--   * DML in the tests only targets these test rows.
--   * Session-only objects: temp table nc_test_results and two pg_temp helper
--     functions. They disappear when the connection closes.
--   * Side effect: identity sequences are not transactional, so each run uses
--     up a few surrogate key numbers (gaps in IDs). Harmless, but run the tests
--     before loading data if you want IDs to start at 1.
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
-- SQLSTATE of the error. The exception block makes each call a subtransaction,
-- so a failed statement leaves nothing behind.
CREATE OR REPLACE FUNCTION pg_temp.nc_try(p_sql text)
RETURNS text
LANGUAGE plpgsql
AS $fn$
BEGIN
    EXECUTE p_sql;
    RETURN '00000';
EXCEPTION WHEN OTHERS THEN
    RETURN SQLSTATE;
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

-- -----------------------------------------------------------------------------
-- Tests (run in order inside a rolled-back sandbox)
-- expected: '00000' = statement succeeds; a SQLSTATE = statement is rejected
--           with that error; 'true' = the check query returns true.
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
    -- ---------------------------------------------------------------- setup
    (1, 'setup', 'Insert test currencies XTS and XXX', '00000',
     $q$INSERT INTO currencies (currency_code, currency_name)
        VALUES ('XTS', 'Test currency'), ('XXX', 'No currency')$q$),
    (2, 'setup', 'Insert test country XA', '00000',
     $q$INSERT INTO countries (country_code, country_name, currency_code, vat_rate, time_zone)
        VALUES ('XA', 'Testland A', 'XTS', 0.2500, 'Europe/Stockholm')$q$),
    (3, 'setup', 'Insert test category and product', '00000',
     $q$INSERT INTO product_categories (category_code, category_name)
        VALUES ('NC_TEST_CAT', 'NordicCart test category');
        INSERT INTO products (sku, product_name, brand, category_id)
        SELECT 'NC-TEST-SKU-1', 'Test laptop', 'TestBrand', category_id
          FROM product_categories WHERE category_code = 'NC_TEST_CAT'$q$),
    (4, 'setup', 'Insert test customer', '00000',
     $q$INSERT INTO customers (customer_number, first_name, last_name, email,
                               country_code, signup_channel, registered_at)
        VALUES ('NC-TEST-C1', 'Åsa', 'Testsson', 'nc.test.customer@example.invalid',
                'XA', 'online', '2025-01-15 10:00+00')$q$),
    (5, 'setup', 'Insert warehouse location with its warehouses row', '00000',
     $q$INSERT INTO stock_locations (location_code, location_name, location_type, country_code,
                                     address_line1, postal_code, city, opened_on)
        VALUES ('XA-WH-T1', 'Test warehouse', 'warehouse', 'XA', 'Testvägen 1', '12345', 'Testby', '2024-01-01');
        INSERT INTO warehouses (stock_location_id)
        SELECT stock_location_id FROM stock_locations WHERE location_code = 'XA-WH-T1'$q$),

    -- ---------------------------------------------------------------- currencies and countries
    (10, 'currencies', 'Missing currency_name is rejected (NOT NULL)', '23502',
     $q$INSERT INTO currencies (currency_code, currency_name) VALUES ('XTT', NULL)$q$),
    (11, 'countries', 'VAT rate of 1.0000 is rejected (ck_countries_vat_rate)', '23514',
     $q$INSERT INTO countries VALUES ('XB', 'Testland B', 'XTS', 1.0000, 'Europe/Oslo')$q$),
    (12, 'countries', 'Unknown currency is rejected (fk_countries_currency)', '23503',
     $q$INSERT INTO countries VALUES ('XB', 'Testland B', 'QQQ', 0.2500, 'Europe/Oslo')$q$),
    (13, 'countries', 'Duplicate country_name is rejected (uq_countries_country_name)', '23505',
     $q$INSERT INTO countries VALUES ('XB', 'Testland A', 'XTS', 0.2500, 'Europe/Oslo')$q$),

    -- ---------------------------------------------------------------- exchange_rates
    (20, 'exchange_rates', 'Valid daily rate is accepted', '00000',
     $q$INSERT INTO exchange_rates (rate_date, base_currency_code, quote_currency_code, rate)
        VALUES ('2025-01-02', 'XTS', 'XXX', 11.50000000)$q$),
    (21, 'exchange_rates', 'Second rate for the same pair and day is rejected (uq_exchange_rates_pair_date)', '23505',
     $q$INSERT INTO exchange_rates (rate_date, base_currency_code, quote_currency_code, rate)
        VALUES ('2025-01-02', 'XTS', 'XXX', 11.60000000)$q$),
    (22, 'exchange_rates', 'Base equal to quote is rejected (ck_exchange_rates_distinct_pair)', '23514',
     $q$INSERT INTO exchange_rates (rate_date, base_currency_code, quote_currency_code, rate)
        VALUES ('2025-01-03', 'XTS', 'XTS', 1)$q$),
    (23, 'exchange_rates', 'Zero rate is rejected (ck_exchange_rates_rate_positive)', '23514',
     $q$INSERT INTO exchange_rates (rate_date, base_currency_code, quote_currency_code, rate)
        VALUES ('2025-01-03', 'XTS', 'XXX', 0)$q$),
    (24, 'exchange_rates', 'UPDATE is rejected (insert-only guard)', '23000',
     $q$UPDATE exchange_rates SET rate = 99 WHERE base_currency_code = 'XTS'$q$),
    (25, 'exchange_rates', 'DELETE is rejected (insert-only guard)', '23000',
     $q$DELETE FROM exchange_rates WHERE base_currency_code = 'XTS'$q$),

    -- ---------------------------------------------------------------- catalogue
    (30, 'product_categories', 'Explicit surrogate key is rejected (GENERATED ALWAYS)', '428C9',
     $q$INSERT INTO product_categories (category_id, category_code, category_name)
        VALUES (999999, 'NC_TEST_CAT2', 'Second test category')$q$),
    (31, 'product_categories', 'Duplicate category_code is rejected (uq_product_categories_category_code)', '23505',
     $q$INSERT INTO product_categories (category_code, category_name) VALUES ('NC_TEST_CAT', 'Other name')$q$),
    (32, 'products', 'Discontinued before launch is rejected (ck_products_dates)', '23514',
     $q$INSERT INTO products (sku, product_name, brand, category_id, launched_on, discontinued_on)
        SELECT 'NC-TEST-SKU-2', 'Test phone', 'TestBrand', category_id, '2025-06-01', '2025-05-31'
          FROM product_categories WHERE category_code = 'NC_TEST_CAT'$q$),
    (33, 'products', 'Unknown category is rejected (fk_products_category)', '23503',
     $q$INSERT INTO products (sku, product_name, brand, category_id)
        VALUES ('NC-TEST-SKU-3', 'Test tablet', 'TestBrand', -1)$q$),
    (34, 'products', 'Duplicate SKU is rejected (uq_products_sku)', '23505',
     $q$INSERT INTO products (sku, product_name, brand, category_id)
        SELECT 'NC-TEST-SKU-1', 'Copy', 'TestBrand', category_id
          FROM product_categories WHERE category_code = 'NC_TEST_CAT'$q$),

    -- ---------------------------------------------------------------- product_prices
    (40, 'product_prices', 'First validity period [2025-01-01, 2025-07-01) is accepted', '00000',
     $q$INSERT INTO product_prices (product_id, country_code, currency_code, list_price, unit_cost, valid_from, valid_to)
        SELECT product_id, 'XA', 'XTS', 9999.00, 6000.00, '2025-01-01 00:00+00', '2025-07-01 00:00+00'
          FROM products WHERE sku = 'NC-TEST-SKU-1'$q$),
    (41, 'product_prices', 'Back-to-back open-ended period from 2025-07-01 is accepted (valid_to is exclusive)', '00000',
     $q$INSERT INTO product_prices (product_id, country_code, currency_code, list_price, unit_cost, valid_from, valid_to)
        SELECT product_id, 'XA', 'XTS', 8999.00, 5800.00, '2025-07-01 00:00+00', NULL
          FROM products WHERE sku = 'NC-TEST-SKU-1'$q$),
    (42, 'product_prices', 'Overlapping period is rejected (ex_product_prices_no_overlap)', '23P01',
     $q$INSERT INTO product_prices (product_id, country_code, currency_code, list_price, unit_cost, valid_from, valid_to)
        SELECT product_id, 'XA', 'XTS', 9499.00, 5900.00, '2025-06-01 00:00+00', '2025-08-01 00:00+00'
          FROM products WHERE sku = 'NC-TEST-SKU-1'$q$),
    (43, 'product_prices', 'Empty validity (valid_to = valid_from) is rejected (ck_product_prices_validity)', '23514',
     $q$INSERT INTO product_prices (product_id, country_code, currency_code, list_price, unit_cost, valid_from, valid_to)
        SELECT product_id, 'XA', 'XTS', 9499.00, 5900.00, '2024-01-01 00:00+00', '2024-01-01 00:00+00'
          FROM products WHERE sku = 'NC-TEST-SKU-1'$q$),
    (44, 'product_prices', 'Zero list price is rejected (ck_product_prices_list_price)', '23514',
     $q$INSERT INTO product_prices (product_id, country_code, currency_code, list_price, unit_cost, valid_from, valid_to)
        SELECT product_id, 'XA', 'XTS', 0, 5900.00, '2024-01-01 00:00+00', '2024-06-01 00:00+00'
          FROM products WHERE sku = 'NC-TEST-SKU-1'$q$),
    (45, 'product_prices', 'Negative unit cost is rejected (ck_product_prices_unit_cost)', '23514',
     $q$INSERT INTO product_prices (product_id, country_code, currency_code, list_price, unit_cost, valid_from, valid_to)
        SELECT product_id, 'XA', 'XTS', 100.00, -1.00, '2024-01-01 00:00+00', '2024-06-01 00:00+00'
          FROM products WHERE sku = 'NC-TEST-SKU-1'$q$),

    -- ---------------------------------------------------------------- customers
    (50, 'customers', 'Duplicate email is rejected (uq_customers_email)', '23505',
     $q$INSERT INTO customers (customer_number, first_name, last_name, email, country_code, signup_channel, registered_at)
        VALUES ('NC-TEST-C2', 'Erik', 'Testsson', 'nc.test.customer@example.invalid', 'XA', 'in_store', now())$q$),
    (51, 'customers', 'Unknown signup channel is rejected (enum sales_channel)', '22P02',
     $q$INSERT INTO customers (customer_number, first_name, last_name, email, country_code, signup_channel, registered_at)
        VALUES ('NC-TEST-C3', 'Erik', 'Testsson', 'nc.test.c3@example.invalid', 'XA', 'phone', now())$q$),
    (52, 'customers', 'Unknown country is rejected (fk_customers_country)', '23503',
     $q$INSERT INTO customers (customer_number, first_name, last_name, email, country_code, signup_channel, registered_at)
        VALUES ('NC-TEST-C4', 'Erik', 'Testsson', 'nc.test.c4@example.invalid', 'QQ', 'online', now())$q$),

    -- ---------------------------------------------------------------- stock locations
    (60, 'stock_locations', 'Store location plus its stores row passes the commit-time subtype check', '00000',
     $q$INSERT INTO stock_locations (location_code, location_name, location_type, country_code,
                                     address_line1, postal_code, city, opened_on)
        VALUES ('XA-STO-T1', 'Test store', 'store', 'XA', 'Testgatan 1', '12345', 'Testby', '2024-01-01');
        INSERT INTO stores (stock_location_id)
        SELECT stock_location_id FROM stock_locations WHERE location_code = 'XA-STO-T1';
        SET CONSTRAINTS public.trg_stock_locations_subtype_required IMMEDIATE;
        SET CONSTRAINTS public.trg_stock_locations_subtype_required DEFERRED$q$),
    (61, 'stock_locations', 'Location without its subtype row fails the commit-time check', '23514',
     $q$INSERT INTO stock_locations (location_code, location_name, location_type, country_code,
                                     address_line1, postal_code, city, opened_on)
        VALUES ('XA-WH-T9', 'Orphan warehouse', 'warehouse', 'XA', 'Testvägen 9', '12345', 'Testby', '2024-01-01');
        SET CONSTRAINTS public.trg_stock_locations_subtype_required IMMEDIATE$q$),
    (62, 'stores', 'Store row pointing at a warehouse location is rejected (fk_stores_stock_location)', '23503',
     $q$INSERT INTO stores (stock_location_id)
        SELECT stock_location_id FROM stock_locations WHERE location_code = 'XA-WH-T1'$q$),
    (63, 'stores', 'Store row typed as warehouse is rejected (ck_stores_location_type)', '23514',
     $q$INSERT INTO stores (stock_location_id, location_type)
        SELECT stock_location_id, 'warehouse' FROM stock_locations WHERE location_code = 'XA-WH-T1'$q$),
    (64, 'stores', 'Second store for the same location is rejected (uq_stores_stock_location)', '23505',
     $q$INSERT INTO stores (stock_location_id)
        SELECT stock_location_id FROM stock_locations WHERE location_code = 'XA-STO-T1'$q$),
    (65, 'stores', 'Deleting a store row but keeping its location fails the commit-time check', '23514',
     $q$DELETE FROM stores
         WHERE stock_location_id = (SELECT stock_location_id FROM stock_locations WHERE location_code = 'XA-STO-T1');
        SET CONSTRAINTS public.trg_stores_subtype_required IMMEDIATE$q$),
    (66, 'warehouses', 'Zero storage capacity is rejected (ck_warehouses_capacity)', '23514',
     $q$UPDATE warehouses SET storage_capacity_units = 0
         WHERE stock_location_id = (SELECT stock_location_id FROM stock_locations WHERE location_code = 'XA-WH-T1')$q$),

    -- ---------------------------------------------------------------- change capture and updated_at
    (70, 'change_log', 'Insert of currency XTS was captured with record_key XTS', 'true',
     $q$SELECT EXISTS (SELECT 1 FROM change_log
                        WHERE table_name = 'currencies' AND record_key = 'XTS'
                          AND operation = 'insert' AND row_data ->> 'currency_name' = 'Test currency')$q$),
    (71, 'change_log', 'Rows are captured under the writing transaction id', 'true',
     $q$SELECT bool_and(transaction_id = pg_current_xact_id())
          FROM change_log WHERE table_name = 'currencies' AND record_key = 'XTS'$q$),
    (72, 'updated_at', 'UPDATE sets updated_at to now(), overriding the value supplied', '00000',
     $q$UPDATE currencies SET currency_name = 'Test currency renamed', updated_at = '2000-01-01 00:00+00'
         WHERE currency_code = 'XTS'$q$),
    (73, 'updated_at', 'updated_at of XTS equals now() after the update', 'true',
     $q$SELECT updated_at = now() FROM currencies WHERE currency_code = 'XTS'$q$),
    (74, 'change_log', 'Update of currency XTS was captured with the new row image', 'true',
     $q$SELECT EXISTS (SELECT 1 FROM change_log
                        WHERE table_name = 'currencies' AND record_key = 'XTS' AND operation = 'update'
                          AND row_data ->> 'currency_name' = 'Test currency renamed')$q$),
    (75, 'change_log', 'Every test table insert was captured (customers, products, locations)', 'true',
     $q$SELECT count(DISTINCT table_name) = 6
          FROM change_log
         WHERE transaction_id = pg_current_xact_id()
           AND table_name IN ('countries', 'product_categories', 'products', 'customers', 'stock_locations', 'warehouses')$q$),
    (76, 'change_log', 'UPDATE of change_log is rejected (insert-only guard)', '23000',
     $q$UPDATE change_log SET record_key = record_key
         WHERE table_name = 'currencies' AND record_key = 'XTS'$q$)
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
        'test_no', 99, 'area', 'safety',
        'test_name', 'Sandbox rolled back: no test rows remain in business tables or change_log',
        'expected', 'true',
        'actual', pg_temp.nc_check($q$
            SELECT NOT EXISTS (SELECT 1 FROM currencies       WHERE currency_code IN ('XTS', 'XXX'))
               AND NOT EXISTS (SELECT 1 FROM countries        WHERE country_code = 'XA')
               AND NOT EXISTS (SELECT 1 FROM stock_locations  WHERE location_code LIKE 'XA-%')
               AND NOT EXISTS (SELECT 1 FROM customers        WHERE customer_number LIKE 'NC-TEST-%')
               AND NOT EXISTS (SELECT 1 FROM change_log
                                WHERE (table_name = 'currencies' AND record_key IN ('XTS', 'XXX'))
                                   OR (table_name = 'countries'  AND record_key = 'XA'))$q$));

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

-- SQLSTATE reference for the expected column:
--   00000 success              23502 not_null_violation    23503 foreign_key_violation
--   23505 unique_violation     23514 check_violation       23P01 exclusion_violation
--   23000 insert-only guard    22P02 invalid enum value    428C9 generated_always identity
