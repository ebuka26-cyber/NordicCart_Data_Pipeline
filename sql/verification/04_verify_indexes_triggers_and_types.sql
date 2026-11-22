-- =============================================================================
-- NordicCart OLTP | Verification 04: enum types, secondary indexes, triggers,
--                                    trigger functions and identity settings
-- -----------------------------------------------------------------------------
-- Scope       : objects created by migrations V002-V005
-- Connect to  : nordiccart_oltp (any role that can read the system catalogs)
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS, and failed_checks = 0.
--               An object that exists but is not in the lists shows as WARN.
-- -----------------------------------------------------------------------------
-- Indexes behind PRIMARY KEY, UNIQUE and EXCLUDE constraints are checked in
-- 03_verify_keys_and_constraints.sql; this script checks the plain (non-unique)
-- indexes from the DBML index blocks.
-- =============================================================================

WITH
-- -----------------------------------------------------------------------------
-- Enum types: labels in their defined order
-- -----------------------------------------------------------------------------
expected_enums (object_name, definition) AS (
    VALUES ('change_operation',    'insert, update, delete'),
           ('sales_channel',       'online, in_store'),
           ('stock_location_type', 'store, warehouse')
),
actual_enums AS (
    SELECT t.typname::text AS object_name,
           string_agg(e.enumlabel, ', ' ORDER BY e.enumsortorder) AS definition
      FROM pg_type AS t
      JOIN pg_namespace AS n ON n.oid = t.typnamespace
      JOIN pg_enum AS e ON e.enumtypid = t.oid
     WHERE n.nspname = 'public'
     GROUP BY t.typname
),
enum_checks AS (
    SELECT 'enum type' AS check_type,
           NULL::text AS table_name,
           coalesce(e.object_name, a.object_name) AS object_name,
           coalesce(e.definition, 'not in this batch') AS expected,
           coalesce(a.definition, 'missing') AS actual,
           CASE WHEN e.object_name IS NULL THEN 'INFO'   -- enums of later migrations
                WHEN e.definition = a.definition THEN 'PASS'
                ELSE 'FAIL' END AS status
      FROM expected_enums AS e
      FULL JOIN actual_enums AS a ON a.object_name = e.object_name
),

-- -----------------------------------------------------------------------------
-- Secondary (non-unique) indexes from the DBML index blocks
-- -----------------------------------------------------------------------------
expected_indexes (table_name, object_name, definition) AS (
    VALUES ('change_log',      'ix_change_log_transaction_id',    'btree (transaction_id)'),
           ('change_log',      'ix_change_log_table_change_id',   'btree (table_name, change_id)'),
           ('customers',       'ix_customers_country_code',       'btree (country_code)'),
           ('customers',       'ix_customers_updated_at',         'btree (updated_at)'),
           ('products',        'ix_products_category_id',         'btree (category_id)'),
           ('products',        'ix_products_updated_at',          'btree (updated_at)'),
           ('product_prices',  'ix_product_prices_country_code',  'btree (country_code)'),
           ('product_prices',  'ix_product_prices_updated_at',    'btree (updated_at)'),
           ('stock_locations', 'ix_stock_locations_country_code', 'btree (country_code)')
),
scope_tables (table_name) AS (
    VALUES ('currencies'), ('countries'), ('exchange_rates'),
           ('product_categories'), ('products'), ('product_prices'), ('customers'),
           ('stock_locations'), ('stores'), ('warehouses'),
           ('change_log'), ('change_log_checkpoints')
),
actual_indexes AS (
    SELECT t.relname::text AS table_name,
           i.relname::text AS object_name,
           regexp_replace(pg_get_indexdef(x.indexrelid), '^.* USING ', '') AS definition
      FROM pg_index AS x
      JOIN pg_class AS i ON i.oid = x.indexrelid
      JOIN pg_class AS t ON t.oid = x.indrelid
      JOIN pg_namespace AS n ON n.oid = t.relnamespace
     WHERE n.nspname = 'public'
       AND t.relname IN (SELECT table_name FROM scope_tables)
       AND NOT EXISTS (SELECT 1 FROM pg_constraint AS c
                        WHERE c.conindid = x.indexrelid
                          AND c.conrelid = x.indrelid)   -- skip PK / UNIQUE / EXCLUDE indexes
),
index_checks AS (
    SELECT 'index' AS check_type,
           coalesce(e.table_name, a.table_name) AS table_name,
           coalesce(e.object_name, a.object_name) AS object_name,
           coalesce(e.definition, 'not expected') AS expected,
           coalesce(a.definition, 'missing') AS actual,
           CASE WHEN e.object_name IS NULL THEN 'WARN'
                WHEN a.object_name IS NULL OR a.table_name <> e.table_name
                     OR a.definition <> e.definition THEN 'FAIL'
                ELSE 'PASS' END AS status
      FROM expected_indexes AS e
      FULL JOIN actual_indexes AS a ON a.object_name = e.object_name
),

-- -----------------------------------------------------------------------------
-- Triggers: timing, level, events, function, argument, deferral, enabled
-- -----------------------------------------------------------------------------
expected_triggers (table_name, object_name, definition) AS (
    VALUES
    -- change capture infrastructure (V002)
    ('change_log',             'trg_change_log_insert_only',                'BEFORE ROW UPDATE -> fn_reject_modification()'),
    ('change_log',             'trg_change_log_no_truncate',                'BEFORE STATEMENT TRUNCATE -> fn_reject_modification()'),
    ('change_log_checkpoints', 'trg_change_log_checkpoints_set_updated_at', 'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    -- reference data (V003)
    ('currencies',     'trg_currencies_set_updated_at',     'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('currencies',     'trg_currencies_capture_change',     'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''currency_code'')'),
    ('countries',      'trg_countries_set_updated_at',      'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('countries',      'trg_countries_capture_change',      'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''country_code'')'),
    ('exchange_rates', 'trg_exchange_rates_insert_only',    'BEFORE ROW DELETE OR UPDATE -> fn_reject_modification()'),
    ('exchange_rates', 'trg_exchange_rates_no_truncate',    'BEFORE STATEMENT TRUNCATE -> fn_reject_modification()'),
    ('exchange_rates', 'trg_exchange_rates_capture_change', 'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''exchange_rate_id'')'),
    -- catalogue and customers (V004)
    ('product_categories', 'trg_product_categories_set_updated_at', 'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('product_categories', 'trg_product_categories_capture_change', 'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''category_id'')'),
    ('products',           'trg_products_set_updated_at',           'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('products',           'trg_products_capture_change',           'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''product_id'')'),
    ('product_prices',     'trg_product_prices_set_updated_at',     'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('product_prices',     'trg_product_prices_capture_change',     'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''product_price_id'')'),
    ('customers',          'trg_customers_set_updated_at',          'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('customers',          'trg_customers_capture_change',          'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''customer_id'')'),
    -- stock locations (V005)
    ('stock_locations', 'trg_stock_locations_set_updated_at',    'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('stock_locations', 'trg_stock_locations_capture_change',    'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''stock_location_id'')'),
    ('stock_locations', 'trg_stock_locations_subtype_required',  'AFTER ROW INSERT OR UPDATE OF location_type -> fn_check_stock_location_subtype() [constraint, initially deferred]'),
    ('stores',          'trg_stores_set_updated_at',             'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('stores',          'trg_stores_capture_change',             'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''store_id'')'),
    ('stores',          'trg_stores_subtype_required',           'AFTER ROW DELETE OR UPDATE OF stock_location_id -> fn_check_stock_location_subtype() [constraint, initially deferred]'),
    ('warehouses',      'trg_warehouses_set_updated_at',         'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('warehouses',      'trg_warehouses_capture_change',         'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''warehouse_id'')'),
    ('warehouses',      'trg_warehouses_subtype_required',       'AFTER ROW DELETE OR UPDATE OF stock_location_id -> fn_check_stock_location_subtype() [constraint, initially deferred]')
),
actual_triggers AS (
    SELECT c.relname::text AS table_name,
           t.tgname::text  AS object_name,
           -- tgtype bits: 1 ROW, 2 BEFORE, 4 INSERT, 8 DELETE, 16 UPDATE, 32 TRUNCATE, 64 INSTEAD
           concat_ws(' ',
               CASE WHEN t.tgtype & 2 > 0 THEN 'BEFORE' WHEN t.tgtype & 64 > 0 THEN 'INSTEAD OF' ELSE 'AFTER' END,
               CASE WHEN t.tgtype & 1 > 0 THEN 'ROW' ELSE 'STATEMENT' END,
               array_to_string(array_remove(ARRAY[
                   CASE WHEN t.tgtype & 4  > 0 THEN 'INSERT'   END,
                   CASE WHEN t.tgtype & 8  > 0 THEN 'DELETE'   END,
                   CASE WHEN t.tgtype & 16 > 0 THEN 'UPDATE'
                        || coalesce(' OF ' || (SELECT string_agg(a.attname, ', ' ORDER BY a.attnum)
                                                 FROM pg_attribute AS a
                                                WHERE a.attrelid = t.tgrelid
                                                  AND a.attnum = ANY (t.tgattr::int2[])), '') END,
                   CASE WHEN t.tgtype & 32 > 0 THEN 'TRUNCATE' END], NULL), ' OR '),
               '->',
               p.proname || '(' || coalesce((regexp_match(pg_get_triggerdef(t.oid),
                                                          'EXECUTE (?:FUNCTION|PROCEDURE) [^(]+\((.*)\)$'))[1], '') || ')',
               CASE WHEN t.tgconstraint <> 0 THEN
                   '[constraint, ' || CASE WHEN t.tginitdeferred THEN 'initially deferred'
                                           WHEN t.tgdeferrable  THEN 'deferrable'
                                           ELSE 'immediate' END || ']'
               END)
           || CASE WHEN t.tgenabled = 'D' THEN ' (DISABLED)' ELSE '' END AS definition
      FROM pg_trigger AS t
      JOIN pg_class     AS c ON c.oid = t.tgrelid
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
      JOIN pg_proc      AS p ON p.oid = t.tgfoid
     WHERE n.nspname = 'public'
       AND NOT t.tgisinternal                     -- skip the system triggers behind FKs
       AND c.relname IN (SELECT table_name FROM scope_tables)
),
trigger_checks AS (
    SELECT 'trigger' AS check_type,
           coalesce(e.table_name, a.table_name) AS table_name,
           coalesce(e.object_name, a.object_name) AS object_name,
           coalesce(e.definition, 'not expected') AS expected,
           coalesce(a.definition, 'missing') AS actual,
           CASE WHEN e.object_name IS NULL THEN 'WARN'
                WHEN a.object_name IS NULL OR a.table_name <> e.table_name
                     OR a.definition <> e.definition THEN 'FAIL'
                ELSE 'PASS' END AS status
      FROM expected_triggers AS e
      FULL JOIN actual_triggers AS a ON a.object_name = e.object_name AND a.table_name = e.table_name
),

-- -----------------------------------------------------------------------------
-- Functions
-- -----------------------------------------------------------------------------
expected_functions (object_name, definition) AS (
    VALUES ('public.fn_set_updated_at()',               'plpgsql, returns trigger'),
           ('public.fn_reject_modification()',          'plpgsql, returns trigger'),
           ('public.fn_capture_change()',               'plpgsql, returns trigger'),
           ('public.fn_check_stock_location_subtype()', 'plpgsql, returns trigger'),
           ('meta.assert_migration_can_run(character varying, character varying)', 'plpgsql, returns void')
),
actual_functions AS (
    SELECT n.nspname || '.' || p.proname || '(' || oidvectortypes(p.proargtypes) || ')' AS object_name,
           l.lanname || ', returns ' || format_type(p.prorettype, NULL) AS definition
      FROM pg_proc AS p
      JOIN pg_namespace AS n ON n.oid = p.pronamespace
      JOIN pg_language  AS l ON l.oid = p.prolang
     WHERE n.nspname IN ('public', 'meta')
       AND l.lanname = 'plpgsql'
),
function_checks AS (
    SELECT 'function' AS check_type,
           NULL::text AS table_name,
           coalesce(e.object_name, a.object_name) AS object_name,
           coalesce(e.definition, 'not in this batch') AS expected,
           coalesce(a.definition, 'missing') AS actual,
           CASE WHEN e.object_name IS NULL THEN 'INFO'
                WHEN e.definition = a.definition THEN 'PASS'
                ELSE 'FAIL' END AS status
      FROM expected_functions AS e
      FULL JOIN actual_functions AS a ON a.object_name = e.object_name
),

-- -----------------------------------------------------------------------------
-- Identity settings: ALWAYS on every surrogate key, CACHE 1 on change_log
-- -----------------------------------------------------------------------------
expected_identities (table_name, object_name, definition) AS (
    VALUES ('change_log',         'change_id',         'GENERATED ALWAYS, cache 1'),
           ('exchange_rates',     'exchange_rate_id',  'GENERATED ALWAYS'),
           ('product_categories', 'category_id',       'GENERATED ALWAYS'),
           ('products',           'product_id',        'GENERATED ALWAYS'),
           ('product_prices',     'product_price_id',  'GENERATED ALWAYS'),
           ('customers',          'customer_id',       'GENERATED ALWAYS'),
           ('stock_locations',    'stock_location_id', 'GENERATED ALWAYS'),
           ('stores',             'store_id',          'GENERATED ALWAYS'),
           ('warehouses',         'warehouse_id',      'GENERATED ALWAYS')
),
actual_identities AS (
    SELECT c.relname::text AS table_name,
           a.attname::text AS object_name,
           CASE a.attidentity WHEN 'a' THEN 'GENERATED ALWAYS' ELSE 'GENERATED BY DEFAULT' END
             || CASE WHEN c.relname = 'change_log' THEN ', cache ' || s.seqcache ELSE '' END AS definition
      FROM pg_attribute AS a
      JOIN pg_class     AS c ON c.oid = a.attrelid
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
      LEFT JOIN pg_sequence AS s
             ON s.seqrelid = pg_get_serial_sequence(quote_ident(n.nspname) || '.' || quote_ident(c.relname), a.attname)::regclass
     WHERE n.nspname = 'public'
       AND a.attidentity <> ''
       AND c.relname IN (SELECT table_name FROM scope_tables)
),
identity_checks AS (
    SELECT 'identity' AS check_type,
           coalesce(e.table_name, a.table_name) AS table_name,
           coalesce(e.object_name, a.object_name) AS object_name,
           coalesce(e.definition, 'not expected') AS expected,
           coalesce(a.definition, 'missing') AS actual,
           CASE WHEN e.object_name IS NULL THEN 'WARN'
                WHEN e.definition = a.definition THEN 'PASS'
                ELSE 'FAIL' END AS status
      FROM expected_identities AS e
      FULL JOIN actual_identities AS a ON a.table_name = e.table_name AND a.object_name = e.object_name
),

all_checks AS (
    SELECT * FROM enum_checks
    UNION ALL SELECT * FROM index_checks
    UNION ALL SELECT * FROM trigger_checks
    UNION ALL SELECT * FROM function_checks
    UNION ALL SELECT * FROM identity_checks
)
SELECT check_type,
       table_name,
       object_name,
       expected,
       actual,
       status,
       count(*) FILTER (WHERE status = 'FAIL') OVER () AS failed_checks,
       count(*) OVER () AS total_checks
  FROM all_checks
 ORDER BY CASE status WHEN 'FAIL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'PASS' THEN 2 ELSE 3 END,
          CASE check_type WHEN 'enum type' THEN 1 WHEN 'index' THEN 2 WHEN 'trigger' THEN 3
                          WHEN 'function' THEN 4 ELSE 5 END,
          table_name NULLS FIRST,
          object_name;
