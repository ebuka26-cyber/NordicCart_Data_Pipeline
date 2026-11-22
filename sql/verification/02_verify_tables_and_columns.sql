-- =============================================================================
-- NordicCart OLTP | Verification 02: tables and columns
-- -----------------------------------------------------------------------------
-- Scope       : the 12 tables created by migrations V002-V005
-- Connect to  : nordiccart_oltp (any role that can read the system catalogs)
-- Safe to run : yes. Read-only: two SELECT statements, no writes, no temp objects.
-- Expected    : every row has status PASS, and failed_checks = 0.
--               Tables created by later migrations show as INFO, not FAIL.
-- -----------------------------------------------------------------------------
-- The expected column list below is the column specification of
-- database/design/nordiccart_oltp_rev3.dbml for these tables: name, position,
-- PostgreSQL type, NOT NULL, default expression and identity kind.
-- Surrogate keys are GENERATED ALWAYS AS IDENTITY (see database/README.md).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Result 1: tables
-- -----------------------------------------------------------------------------
WITH expected_tables (table_name) AS (
    VALUES ('currencies'), ('countries'), ('exchange_rates'),
           ('product_categories'), ('products'), ('product_prices'), ('customers'),
           ('stock_locations'), ('stores'), ('warehouses'),
           ('change_log'), ('change_log_checkpoints')
),
actual_tables AS (
    SELECT c.relname::text AS table_name
      FROM pg_class AS c
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
       AND c.relkind IN ('r', 'p')
),
checks AS (
    SELECT coalesce(e.table_name, a.table_name) AS table_name,
           CASE WHEN e.table_name IS NULL THEN 'not in this batch' ELSE 'exists' END AS expected,
           CASE WHEN a.table_name IS NULL THEN 'missing' ELSE 'exists' END AS actual,
           CASE
               WHEN e.table_name IS NULL THEN 'INFO'
               WHEN a.table_name IS NULL THEN 'FAIL'
               ELSE 'PASS'
           END AS status
      FROM expected_tables AS e
      FULL JOIN actual_tables AS a ON a.table_name = e.table_name
)
SELECT 'table' AS check_type,
       table_name,
       expected,
       actual,
       status,
       count(*) FILTER (WHERE status = 'FAIL') OVER () AS failed_checks,
       count(*) OVER () AS total_checks
  FROM checks
 ORDER BY CASE status WHEN 'FAIL' THEN 0 WHEN 'INFO' THEN 2 ELSE 1 END, table_name;

-- -----------------------------------------------------------------------------
-- Result 2: columns (position, type, NOT NULL, default, identity)
-- -----------------------------------------------------------------------------
WITH expected_columns (table_name, ordinal, column_name, data_type, not_null, default_expr, identity_kind) AS (
    VALUES
    ('currencies', 1, 'currency_code', 'character(3)', true,  NULL, ''),
    ('currencies', 2, 'currency_name', 'character varying(50)', true,  NULL, ''),
    ('currencies', 3, 'minor_units', 'smallint', true,  '2', ''),
    ('currencies', 4, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('currencies', 5, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('countries', 1, 'country_code', 'character(2)', true,  NULL, ''),
    ('countries', 2, 'country_name', 'character varying(100)', true,  NULL, ''),
    ('countries', 3, 'currency_code', 'character(3)', true,  NULL, ''),
    ('countries', 4, 'vat_rate', 'numeric(5,4)', true,  NULL, ''),
    ('countries', 5, 'time_zone', 'character varying(64)', true,  NULL, ''),
    ('countries', 6, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('countries', 7, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('exchange_rates', 1, 'exchange_rate_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('exchange_rates', 2, 'rate_date', 'date', true,  NULL, ''),
    ('exchange_rates', 3, 'base_currency_code', 'character(3)', true,  '''EUR''::bpchar', ''),
    ('exchange_rates', 4, 'quote_currency_code', 'character(3)', true,  NULL, ''),
    ('exchange_rates', 5, 'rate', 'numeric(18,8)', true,  NULL, ''),
    ('exchange_rates', 6, 'source', 'character varying(50)', true,  '''synthetic_ecb''::character varying', ''),
    ('exchange_rates', 7, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('product_categories', 1, 'category_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('product_categories', 2, 'category_code', 'character varying(30)', true,  NULL, ''),
    ('product_categories', 3, 'category_name', 'character varying(100)', true,  NULL, ''),
    ('product_categories', 4, 'is_active', 'boolean', true,  'true', ''),
    ('product_categories', 5, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('product_categories', 6, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('products', 1, 'product_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('products', 2, 'sku', 'character varying(32)', true,  NULL, ''),
    ('products', 3, 'product_name', 'character varying(200)', true,  NULL, ''),
    ('products', 4, 'brand', 'character varying(100)', true,  NULL, ''),
    ('products', 5, 'category_id', 'bigint', true,  NULL, ''),
    ('products', 6, 'launched_on', 'date', false, NULL, ''),
    ('products', 7, 'discontinued_on', 'date', false, NULL, ''),
    ('products', 8, 'is_active', 'boolean', true,  'true', ''),
    ('products', 9, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('products', 10, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('product_prices', 1, 'product_price_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('product_prices', 2, 'product_id', 'bigint', true,  NULL, ''),
    ('product_prices', 3, 'country_code', 'character(2)', true,  NULL, ''),
    ('product_prices', 4, 'currency_code', 'character(3)', true,  NULL, ''),
    ('product_prices', 5, 'list_price', 'numeric(12,2)', true,  NULL, ''),
    ('product_prices', 6, 'unit_cost', 'numeric(12,2)', true,  NULL, ''),
    ('product_prices', 7, 'valid_from', 'timestamp with time zone', true,  NULL, ''),
    ('product_prices', 8, 'valid_to', 'timestamp with time zone', false, NULL, ''),
    ('product_prices', 9, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('product_prices', 10, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('customers', 1, 'customer_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('customers', 2, 'customer_number', 'character varying(20)', true,  NULL, ''),
    ('customers', 3, 'first_name', 'character varying(100)', true,  NULL, ''),
    ('customers', 4, 'last_name', 'character varying(100)', true,  NULL, ''),
    ('customers', 5, 'email', 'character varying(255)', true,  NULL, ''),
    ('customers', 6, 'phone', 'character varying(30)', false, NULL, ''),
    ('customers', 7, 'birth_date', 'date', false, NULL, ''),
    ('customers', 8, 'address_line1', 'character varying(200)', false, NULL, ''),
    ('customers', 9, 'postal_code', 'character varying(10)', false, NULL, ''),
    ('customers', 10, 'city', 'character varying(100)', false, NULL, ''),
    ('customers', 11, 'country_code', 'character(2)', true,  NULL, ''),
    ('customers', 12, 'signup_channel', 'sales_channel', true,  NULL, ''),
    ('customers', 13, 'registered_at', 'timestamp with time zone', true,  NULL, ''),
    ('customers', 14, 'is_active', 'boolean', true,  'true', ''),
    ('customers', 15, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('customers', 16, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('stock_locations', 1, 'stock_location_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('stock_locations', 2, 'location_code', 'character varying(20)', true,  NULL, ''),
    ('stock_locations', 3, 'location_name', 'character varying(100)', true,  NULL, ''),
    ('stock_locations', 4, 'location_type', 'stock_location_type', true,  NULL, ''),
    ('stock_locations', 5, 'country_code', 'character(2)', true,  NULL, ''),
    ('stock_locations', 6, 'address_line1', 'character varying(200)', true,  NULL, ''),
    ('stock_locations', 7, 'postal_code', 'character varying(10)', true,  NULL, ''),
    ('stock_locations', 8, 'city', 'character varying(100)', true,  NULL, ''),
    ('stock_locations', 9, 'opened_on', 'date', true,  NULL, ''),
    ('stock_locations', 10, 'is_active', 'boolean', true,  'true', ''),
    ('stock_locations', 11, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('stock_locations', 12, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('stores', 1, 'store_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('stores', 2, 'stock_location_id', 'bigint', true,  NULL, ''),
    ('stores', 3, 'location_type', 'stock_location_type', true,  '''store''::stock_location_type', ''),
    ('stores', 4, 'store_format', 'character varying(20)', true,  '''standard''::character varying', ''),
    ('stores', 5, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('stores', 6, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('warehouses', 1, 'warehouse_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('warehouses', 2, 'stock_location_id', 'bigint', true,  NULL, ''),
    ('warehouses', 3, 'location_type', 'stock_location_type', true,  '''warehouse''::stock_location_type', ''),
    ('warehouses', 4, 'ships_online_orders', 'boolean', true,  'true', ''),
    ('warehouses', 5, 'storage_capacity_units', 'integer', false, NULL, ''),
    ('warehouses', 6, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('warehouses', 7, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('change_log', 1, 'change_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('change_log', 2, 'table_name', 'character varying(63)', true,  NULL, ''),
    ('change_log', 3, 'record_key', 'character varying(64)', true,  NULL, ''),
    ('change_log', 4, 'operation', 'change_operation', true,  NULL, ''),
    ('change_log', 5, 'row_data', 'jsonb', true,  NULL, ''),
    ('change_log', 6, 'transaction_id', 'xid8', true,  'pg_current_xact_id()', ''),
    ('change_log', 7, 'changed_at', 'timestamp with time zone', true,  'clock_timestamp()', ''),
    ('change_log_checkpoints', 1, 'consumer_name', 'character varying(50)', true,  NULL, ''),
    ('change_log_checkpoints', 2, 'last_snapshot', 'pg_snapshot', true,  NULL, ''),
    ('change_log_checkpoints', 3, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('change_log_checkpoints', 4, 'updated_at', 'timestamp with time zone', true,  'now()', '')
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
