-- =============================================================================
-- NordicCart OLTP | Verification 03: primary keys, unique, foreign key,
--                                    check and exclusion constraints
-- -----------------------------------------------------------------------------
-- Scope       : the 12 tables created by migrations V002-V005
-- Connect to  : nordiccart_oltp (any role that can read the system catalogs)
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS, and failed_checks = 0.
--               A constraint that exists but is not in the list shows as WARN.
-- -----------------------------------------------------------------------------
-- What is compared for each constraint (by name)
--   * table and constraint type
--   * key columns in order (PK, UNIQUE, FK); referenced columns for CHECKs,
--     sorted by name; the full definition for the EXCLUDE constraint
--   * FKs: referenced table and columns, ON UPDATE / ON DELETE NO ACTION,
--     MATCH SIMPLE (the design relies on MATCH SIMPLE for nullable composite FKs)
--   * not deferrable, and validated
-- CHECK expressions themselves are proven by tests/sql/test_foundation_constraints.sql.
-- =============================================================================

WITH expected_constraints (table_name, constraint_name, constraint_type, key_columns, ref_table, ref_columns) AS (
    VALUES
    -- Primary keys
    ('currencies',             'pk_currencies',             'PRIMARY KEY', 'currency_code',     NULL, NULL),
    ('countries',              'pk_countries',              'PRIMARY KEY', 'country_code',      NULL, NULL),
    ('exchange_rates',         'pk_exchange_rates',         'PRIMARY KEY', 'exchange_rate_id',  NULL, NULL),
    ('product_categories',     'pk_product_categories',     'PRIMARY KEY', 'category_id',       NULL, NULL),
    ('products',               'pk_products',               'PRIMARY KEY', 'product_id',        NULL, NULL),
    ('product_prices',         'pk_product_prices',         'PRIMARY KEY', 'product_price_id',  NULL, NULL),
    ('customers',              'pk_customers',              'PRIMARY KEY', 'customer_id',       NULL, NULL),
    ('stock_locations',        'pk_stock_locations',        'PRIMARY KEY', 'stock_location_id', NULL, NULL),
    ('stores',                 'pk_stores',                 'PRIMARY KEY', 'store_id',          NULL, NULL),
    ('warehouses',             'pk_warehouses',             'PRIMARY KEY', 'warehouse_id',      NULL, NULL),
    ('change_log',             'pk_change_log',             'PRIMARY KEY', 'change_id',         NULL, NULL),
    ('change_log_checkpoints', 'pk_change_log_checkpoints', 'PRIMARY KEY', 'consumer_name',     NULL, NULL),

    -- Unique constraints (business keys and composite FK targets)
    ('countries',          'uq_countries_country_name',              'UNIQUE', 'country_name',                                  NULL, NULL),
    ('exchange_rates',     'uq_exchange_rates_pair_date',            'UNIQUE', 'base_currency_code, quote_currency_code, rate_date', NULL, NULL),
    ('product_categories', 'uq_product_categories_category_code',    'UNIQUE', 'category_code',                                 NULL, NULL),
    ('product_categories', 'uq_product_categories_category_name',    'UNIQUE', 'category_name',                                 NULL, NULL),
    ('products',           'uq_products_sku',                        'UNIQUE', 'sku',                                           NULL, NULL),
    ('product_prices',     'uq_product_prices_product_country_from', 'UNIQUE', 'product_id, country_code, valid_from',          NULL, NULL),
    ('customers',          'uq_customers_customer_number',           'UNIQUE', 'customer_number',                               NULL, NULL),
    ('customers',          'uq_customers_email',                     'UNIQUE', 'email',                                         NULL, NULL),
    ('stock_locations',    'uq_stock_locations_location_code',       'UNIQUE', 'location_code',                                 NULL, NULL),
    ('stock_locations',    'uq_stock_locations_id_type',             'UNIQUE', 'stock_location_id, location_type',              NULL, NULL),
    ('stores',             'uq_stores_stock_location',               'UNIQUE', 'stock_location_id, location_type',              NULL, NULL),
    ('stores',             'uq_stores_store_location',               'UNIQUE', 'store_id, stock_location_id',                   NULL, NULL),
    ('warehouses',         'uq_warehouses_stock_location',           'UNIQUE', 'stock_location_id, location_type',              NULL, NULL),

    -- Foreign keys (names from the DBML Ref definitions)
    ('countries',       'fk_countries_currency',            'FOREIGN KEY', 'currency_code',                    'currencies',         'currency_code'),
    ('exchange_rates',  'fk_exchange_rates_base_currency',  'FOREIGN KEY', 'base_currency_code',               'currencies',         'currency_code'),
    ('exchange_rates',  'fk_exchange_rates_quote_currency', 'FOREIGN KEY', 'quote_currency_code',              'currencies',         'currency_code'),
    ('customers',       'fk_customers_country',             'FOREIGN KEY', 'country_code',                     'countries',          'country_code'),
    ('products',        'fk_products_category',             'FOREIGN KEY', 'category_id',                      'product_categories', 'category_id'),
    ('product_prices',  'fk_product_prices_product',        'FOREIGN KEY', 'product_id',                       'products',           'product_id'),
    ('product_prices',  'fk_product_prices_country',        'FOREIGN KEY', 'country_code',                     'countries',          'country_code'),
    ('product_prices',  'fk_product_prices_currency',       'FOREIGN KEY', 'currency_code',                    'currencies',         'currency_code'),
    ('stock_locations', 'fk_stock_locations_country',       'FOREIGN KEY', 'country_code',                     'countries',          'country_code'),
    ('stores',          'fk_stores_stock_location',         'FOREIGN KEY', 'stock_location_id, location_type', 'stock_locations',    'stock_location_id, location_type'),
    ('warehouses',      'fk_warehouses_stock_location',     'FOREIGN KEY', 'stock_location_id, location_type', 'stock_locations',    'stock_location_id, location_type'),

    -- Check constraints (columns listed alphabetically)
    ('countries',       'ck_countries_vat_rate',           'CHECK', 'vat_rate',                                NULL, NULL),
    ('exchange_rates',  'ck_exchange_rates_rate_positive', 'CHECK', 'rate',                                    NULL, NULL),
    ('exchange_rates',  'ck_exchange_rates_distinct_pair', 'CHECK', 'base_currency_code, quote_currency_code', NULL, NULL),
    ('products',        'ck_products_dates',               'CHECK', 'discontinued_on, launched_on',            NULL, NULL),
    ('product_prices',  'ck_product_prices_list_price',    'CHECK', 'list_price',                              NULL, NULL),
    ('product_prices',  'ck_product_prices_unit_cost',     'CHECK', 'unit_cost',                               NULL, NULL),
    ('product_prices',  'ck_product_prices_validity',      'CHECK', 'valid_from, valid_to',                    NULL, NULL),
    ('stores',          'ck_stores_location_type',         'CHECK', 'location_type',                           NULL, NULL),
    ('warehouses',      'ck_warehouses_location_type',     'CHECK', 'location_type',                           NULL, NULL),
    ('warehouses',      'ck_warehouses_capacity',          'CHECK', 'storage_capacity_units',                  NULL, NULL),

    -- Exclusion constraint ([PG] item: no overlapping price validity per product and market)
    ('product_prices', 'ex_product_prices_no_overlap', 'EXCLUDE',
     'USING gist (product_id WITH =, country_code WITH =, tstzrange(valid_from, valid_to) WITH &&)', NULL, NULL)
),
scope AS (
    SELECT DISTINCT table_name FROM expected_constraints
),
actual_constraints AS (
    SELECT rel.relname::text AS table_name,
           con.conname::text AS constraint_name,
           CASE con.contype
               WHEN 'p' THEN 'PRIMARY KEY'
               WHEN 'u' THEN 'UNIQUE'
               WHEN 'f' THEN 'FOREIGN KEY'
               WHEN 'c' THEN 'CHECK'
               WHEN 'x' THEN 'EXCLUDE'
           END AS constraint_type,
           CASE con.contype
               WHEN 'x' THEN regexp_replace(pg_get_constraintdef(con.oid), '^EXCLUDE ', '')
               WHEN 'c' THEN (SELECT string_agg(a.attname, ', ' ORDER BY a.attname)
                                FROM unnest(con.conkey) AS k(attnum)
                                JOIN pg_attribute AS a ON a.attrelid = con.conrelid AND a.attnum = k.attnum)
               ELSE (SELECT string_agg(a.attname, ', ' ORDER BY k.ord)
                       FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                       JOIN pg_attribute AS a ON a.attrelid = con.conrelid AND a.attnum = k.attnum)
           END AS key_columns,
           ref.relname::text AS ref_table,
           (SELECT string_agg(a.attname, ', ' ORDER BY k.ord)
              FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
              JOIN pg_attribute AS a ON a.attrelid = con.confrelid AND a.attnum = k.attnum) AS ref_columns,
           con.confupdtype,
           con.confdeltype,
           con.confmatchtype,
           con.condeferrable,
           con.convalidated
      FROM pg_constraint AS con
      JOIN pg_class      AS rel ON rel.oid = con.conrelid
      JOIN pg_namespace  AS nsp ON nsp.oid = rel.relnamespace
      LEFT JOIN pg_class AS ref ON ref.oid = con.confrelid
     WHERE nsp.nspname = 'public'
       AND con.contype IN ('p', 'u', 'f', 'c', 'x')
       AND rel.relname IN (SELECT table_name FROM scope)
),
described AS (
    SELECT coalesce(e.table_name, a.table_name)           AS table_name,
           coalesce(e.constraint_name, a.constraint_name) AS constraint_name,
           coalesce(e.constraint_type, a.constraint_type) AS constraint_type,
           CASE WHEN e.constraint_name IS NULL THEN 'not expected'
                ELSE concat_ws(' ',
                        e.constraint_type,
                        CASE WHEN e.constraint_type = 'EXCLUDE' THEN e.key_columns
                             ELSE '(' || e.key_columns || ')' END,
                        CASE WHEN e.ref_table IS NOT NULL
                             THEN 'REFERENCES ' || e.ref_table || ' (' || e.ref_columns || ')'
                                  || ' ON UPDATE NO ACTION ON DELETE NO ACTION MATCH SIMPLE' END,
                        'NOT DEFERRABLE VALIDATED')
           END AS expected,
           CASE WHEN a.constraint_name IS NULL THEN 'missing'
                ELSE concat_ws(' ',
                        a.constraint_type,
                        CASE WHEN a.constraint_type = 'EXCLUDE' THEN a.key_columns
                             ELSE '(' || a.key_columns || ')' END,
                        CASE WHEN a.ref_table IS NOT NULL
                             THEN 'REFERENCES ' || a.ref_table || ' (' || a.ref_columns || ')'
                                  || ' ON UPDATE ' || CASE a.confupdtype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT'
                                                       WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL' ELSE 'SET DEFAULT' END
                                  || ' ON DELETE ' || CASE a.confdeltype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT'
                                                       WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL' ELSE 'SET DEFAULT' END
                                  || ' MATCH ' || CASE a.confmatchtype WHEN 's' THEN 'SIMPLE' WHEN 'f' THEN 'FULL' ELSE 'PARTIAL' END
                        END,
                        CASE WHEN a.condeferrable THEN 'DEFERRABLE' ELSE 'NOT DEFERRABLE' END
                          || CASE WHEN a.convalidated THEN ' VALIDATED' ELSE ' NOT VALIDATED' END)
           END AS actual,
           e.constraint_name IS NULL AS is_unexpected
      FROM expected_constraints AS e
      FULL JOIN actual_constraints AS a
        ON a.table_name = e.table_name
       AND a.constraint_name = e.constraint_name
)
SELECT table_name,
       constraint_type,
       constraint_name,
       expected,
       actual,
       CASE WHEN is_unexpected THEN 'WARN'
            WHEN expected = actual THEN 'PASS'
            ELSE 'FAIL' END AS status,
       count(*) FILTER (WHERE NOT is_unexpected AND expected IS DISTINCT FROM actual) OVER () AS failed_checks,
       count(*) OVER () AS total_checks
  FROM described
 ORDER BY CASE WHEN is_unexpected THEN 1 WHEN expected = actual THEN 2 ELSE 0 END,
          table_name,
          CASE constraint_type WHEN 'PRIMARY KEY' THEN 1 WHEN 'UNIQUE' THEN 2 WHEN 'FOREIGN KEY' THEN 3
                               WHEN 'CHECK' THEN 4 ELSE 5 END,
          constraint_name;
