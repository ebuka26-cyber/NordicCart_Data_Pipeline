-- =============================================================================
-- NordicCart OLTP | Verification 05: V006 migration history, tables and columns
-- -----------------------------------------------------------------------------
-- Scope       : migration V006 (orders, order_items, payment_attempts)
-- Connect to  : nordiccart_oltp (any role that can read the system catalogs)
-- Safe to run : yes. Read-only: two SELECT statements, no writes, no temp objects.
-- Expected    : every row has status PASS, and failed_checks = 0, in both results.
-- Run after   : 01 to 04 (they still cover V001 to V005; V006 objects appear
--               there as INFO, which is expected).
-- -----------------------------------------------------------------------------
-- The expected column list is the column specification of
-- database/design/nordiccart_oltp_rev3.dbml for these tables: name, position,
-- PostgreSQL type, NOT NULL, default expression and identity kind.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Result 1: migration history and tables
-- -----------------------------------------------------------------------------
WITH expected_migrations (version, script_name) AS (
    VALUES ('001', 'V001__migration_history_and_extensions.sql'),
           ('002', 'V002__change_capture_and_shared_trigger_functions.sql'),
           ('003', 'V003__reference_data_currencies_countries_exchange_rates.sql'),
           ('004', 'V004__catalogue_and_customers.sql'),
           ('005', 'V005__stock_locations_stores_warehouses.sql'),
           ('006', 'V006__orders_order_items_payment_attempts.sql')
),
migration_checks AS (
    SELECT 'migration' AS check_type,
           'V' || e.version AS object_name,
           e.script_name AS expected,
           coalesce(m.script_name, 'not applied') AS actual
      FROM expected_migrations AS e
      LEFT JOIN meta.schema_migrations AS m ON m.version = e.version
),
expected_tables (table_name) AS (
    VALUES ('orders'), ('order_items'), ('payment_attempts')
),
table_checks AS (
    SELECT 'table' AS check_type,
           e.table_name AS object_name,
           'exists' AS expected,
           CASE WHEN EXISTS (SELECT 1
                               FROM pg_class AS c
                               JOIN pg_namespace AS n ON n.oid = c.relnamespace
                              WHERE n.nspname = 'public'
                                AND c.relkind IN ('r', 'p')
                                AND c.relname = e.table_name)
                THEN 'exists' ELSE 'missing' END AS actual
      FROM expected_tables AS e
),
checks AS (
    SELECT * FROM migration_checks
    UNION ALL
    SELECT * FROM table_checks
)
SELECT check_type,
       object_name,
       expected,
       actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS status,
       count(*) FILTER (WHERE expected <> actual) OVER () AS failed_checks,
       count(*) OVER () AS total_checks
  FROM checks
 ORDER BY CASE WHEN expected = actual THEN 1 ELSE 0 END, check_type, object_name;

-- -----------------------------------------------------------------------------
-- Result 2: columns (position, type, NOT NULL, default, identity)
-- -----------------------------------------------------------------------------
WITH expected_columns (table_name, ordinal, column_name, data_type, not_null, default_expr, identity_kind) AS (
    VALUES
    ('orders', 1, 'order_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('orders', 2, 'order_number', 'character varying(30)', true,  NULL, ''),
    ('orders', 3, 'customer_id', 'bigint', false, NULL, ''),
    ('orders', 4, 'channel', 'sales_channel', true,  NULL, ''),
    ('orders', 5, 'store_id', 'bigint', false, NULL, ''),
    ('orders', 6, 'fulfilment_location_id', 'bigint', true,  NULL, ''),
    ('orders', 7, 'country_code', 'character(2)', true,  NULL, ''),
    ('orders', 8, 'currency_code', 'character(3)', true,  NULL, ''),
    ('orders', 9, 'status', 'order_status', true,  '''placed''::order_status', ''),
    ('orders', 10, 'cancellation_reason', 'character varying(50)', false, NULL, ''),
    ('orders', 11, 'items_total', 'numeric(14,2)', true,  '0', ''),
    ('orders', 12, 'shipping_fee', 'numeric(12,2)', true,  '0', ''),
    ('orders', 13, 'shipping_vat_amount', 'numeric(12,2)', true,  '0', ''),
    ('orders', 14, 'order_total', 'numeric(14,2)', true,  '0', ''),
    ('orders', 15, 'shipping_address_line1', 'character varying(200)', false, NULL, ''),
    ('orders', 16, 'shipping_postal_code', 'character varying(10)', false, NULL, ''),
    ('orders', 17, 'shipping_city', 'character varying(100)', false, NULL, ''),
    ('orders', 18, 'placed_at', 'timestamp with time zone', true,  NULL, ''),
    ('orders', 19, 'confirmed_at', 'timestamp with time zone', false, NULL, ''),
    ('orders', 20, 'fulfilled_at', 'timestamp with time zone', false, NULL, ''),
    ('orders', 21, 'cancelled_at', 'timestamp with time zone', false, NULL, ''),
    ('orders', 22, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('orders', 23, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('order_items', 1, 'order_item_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('order_items', 2, 'order_id', 'bigint', true,  NULL, ''),
    ('order_items', 3, 'line_number', 'smallint', true,  NULL, ''),
    ('order_items', 4, 'product_id', 'bigint', true,  NULL, ''),
    ('order_items', 5, 'promotion_id', 'bigint', false, NULL, ''),
    ('order_items', 6, 'quantity', 'integer', true,  NULL, ''),
    ('order_items', 7, 'unit_price', 'numeric(12,2)', true,  NULL, ''),
    ('order_items', 8, 'unit_cost', 'numeric(12,2)', true,  NULL, ''),
    ('order_items', 9, 'discount_percent', 'numeric(5,2)', true,  '0', ''),
    ('order_items', 10, 'discount_amount', 'numeric(12,2)', true,  '0', ''),
    ('order_items', 11, 'line_total', 'numeric(14,2)', true,  NULL, ''),
    ('order_items', 12, 'vat_rate', 'numeric(5,4)', true,  NULL, ''),
    ('order_items', 13, 'vat_amount', 'numeric(12,2)', true,  NULL, ''),
    ('order_items', 14, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('order_items', 15, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('payment_attempts', 1, 'payment_attempt_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('payment_attempts', 2, 'order_id', 'bigint', true,  NULL, ''),
    ('payment_attempts', 3, 'attempt_number', 'smallint', true,  NULL, ''),
    ('payment_attempts', 4, 'payment_method', 'payment_method', true,  NULL, ''),
    ('payment_attempts', 5, 'payment_provider', 'character varying(50)', true,  NULL, ''),
    ('payment_attempts', 6, 'amount', 'numeric(14,2)', true,  NULL, ''),
    ('payment_attempts', 7, 'status', 'payment_status', true,  '''pending''::payment_status', ''),
    ('payment_attempts', 8, 'failure_reason', 'character varying(50)', false, NULL, ''),
    ('payment_attempts', 9, 'provider_reference', 'character varying(64)', false, NULL, ''),
    ('payment_attempts', 10, 'attempted_at', 'timestamp with time zone', true,  NULL, ''),
    ('payment_attempts', 11, 'authorized_at', 'timestamp with time zone', false, NULL, ''),
    ('payment_attempts', 12, 'captured_at', 'timestamp with time zone', false, NULL, ''),
    ('payment_attempts', 13, 'failed_at', 'timestamp with time zone', false, NULL, ''),
    ('payment_attempts', 14, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('payment_attempts', 15, 'updated_at', 'timestamp with time zone', true,  'now()', '')
),
actual_columns AS (
    SELECT c.relname::text                                  AS table_name,
           a.attnum::integer                                AS ordinal,
           a.attname::text                                  AS column_name,
           -- strip the "public." prefix the server adds to enum type names when
           -- public is not on the search_path, so results do not depend on session settings
           regexp_replace(format_type(a.atttypid, a.atttypmod), '^public\.', '') AS data_type,
           a.attnotnull                                     AS not_null,
           regexp_replace(pg_get_expr(d.adbin, d.adrelid), 'public\.', '', 'g') AS default_expr,
           CASE a.attidentity WHEN 'a' THEN 'ALWAYS' WHEN 'd' THEN 'BY DEFAULT' ELSE '' END AS identity_kind
      FROM pg_attribute AS a
      JOIN pg_class     AS c ON c.oid = a.attrelid
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
      LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE n.nspname = 'public'
       AND c.relkind IN ('r', 'p')
       AND a.attnum > 0
       AND NOT a.attisdropped
       AND c.relname IN (SELECT DISTINCT table_name FROM expected_columns)
),
checks AS (
    SELECT coalesce(e.table_name, a.table_name)   AS table_name,
           coalesce(e.column_name, a.column_name) AS column_name,
           CASE WHEN e.column_name IS NULL THEN 'not expected'
                ELSE concat_ws(' | ', '#' || e.ordinal, e.data_type,
                               CASE WHEN e.not_null THEN 'NOT NULL' ELSE 'NULL' END,
                               'default ' || coalesce(e.default_expr, '-'),
                               nullif(e.identity_kind, ''))
           END AS expected,
           CASE WHEN a.column_name IS NULL THEN 'missing'
                ELSE concat_ws(' | ', '#' || a.ordinal, a.data_type,
                               CASE WHEN a.not_null THEN 'NOT NULL' ELSE 'NULL' END,
                               'default ' || coalesce(a.default_expr, '-'),
                               nullif(a.identity_kind, ''))
           END AS actual
      FROM expected_columns AS e
      FULL JOIN actual_columns AS a
        ON a.table_name = e.table_name
       AND a.column_name = e.column_name
)
SELECT 'column' AS check_type,
       table_name,
       column_name,
       expected,
       actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS status,
       count(*) FILTER (WHERE expected IS DISTINCT FROM actual) OVER () AS failed_checks,
       count(*) OVER () AS total_checks
  FROM checks
 ORDER BY CASE WHEN expected = actual THEN 1 ELSE 0 END, table_name, column_name;
