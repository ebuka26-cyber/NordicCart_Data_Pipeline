-- =============================================================================
-- NordicCart OLTP | Verification 08: V007 migration history, tables and columns
-- -----------------------------------------------------------------------------
-- Scope       : migration V007 (inventory_balances, inventory_reservations,
--               inventory_movements)
-- Connect to  : nordiccart_oltp (any role that can read the system catalogs)
-- Safe to run : yes. Read-only: two SELECT statements, no writes, no temp objects.
-- Expected    : every row has status PASS, and failed_checks = 0, in both results.
-- Run after   : 01 to 07 (they still cover V001 to V006; V007 objects appear
--               there as INFO, and in 07 as two WARN rows for the V007
--               triggers on orders and payment_attempts, which script 10 checks).
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
           ('006', 'V006__orders_order_items_payment_attempts.sql'),
           ('007', 'V007__inventory_balances_reservations_movements.sql')
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
    VALUES ('inventory_balances'), ('inventory_reservations'), ('inventory_movements')
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
    ('inventory_balances', 1, 'inventory_balance_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('inventory_balances', 2, 'stock_location_id', 'bigint', true,  NULL, ''),
    ('inventory_balances', 3, 'product_id', 'bigint', true,  NULL, ''),
    ('inventory_balances', 4, 'quantity_on_hand', 'integer', true,  '0', ''),
    ('inventory_balances', 5, 'quantity_reserved', 'integer', true,  '0', ''),
    ('inventory_balances', 6, 'reorder_point', 'integer', true,  '0', ''),
    ('inventory_balances', 7, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('inventory_balances', 8, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('inventory_reservations', 1, 'reservation_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('inventory_reservations', 2, 'order_item_id', 'bigint', true,  NULL, ''),
    ('inventory_reservations', 3, 'stock_location_id', 'bigint', true,  NULL, ''),
    ('inventory_reservations', 4, 'product_id', 'bigint', true,  NULL, ''),
    ('inventory_reservations', 5, 'quantity', 'integer', true,  NULL, ''),
    ('inventory_reservations', 6, 'status', 'reservation_status', true,  '''active''::reservation_status', ''),
    ('inventory_reservations', 7, 'reserved_at', 'timestamp with time zone', true,  NULL, ''),
    ('inventory_reservations', 8, 'expires_at', 'timestamp with time zone', true,  NULL, ''),
    ('inventory_reservations', 9, 'closed_at', 'timestamp with time zone', false, NULL, ''),
    ('inventory_reservations', 10, 'created_at', 'timestamp with time zone', true,  'now()', ''),
    ('inventory_reservations', 11, 'updated_at', 'timestamp with time zone', true,  'now()', ''),
    ('inventory_movements', 1, 'movement_id', 'bigint', true,  NULL, 'ALWAYS'),
    ('inventory_movements', 2, 'stock_location_id', 'bigint', true,  NULL, ''),
    ('inventory_movements', 3, 'product_id', 'bigint', true,  NULL, ''),
    ('inventory_movements', 4, 'movement_type', 'movement_type', true,  NULL, ''),
    ('inventory_movements', 5, 'quantity_change', 'integer', true,  NULL, ''),
    ('inventory_movements', 6, 'occurred_at', 'timestamp with time zone', true,  NULL, ''),
    ('inventory_movements', 7, 'order_item_id', 'bigint', false, NULL, ''),
    ('inventory_movements', 8, 'return_item_id', 'bigint', false, NULL, ''),
    ('inventory_movements', 9, 'related_movement_id', 'bigint', false, NULL, ''),
    ('inventory_movements', 10, 'external_reference', 'character varying(64)', false, NULL, ''),
    ('inventory_movements', 11, 'created_at', 'timestamp with time zone', true,  'now()', '')

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
