-- =============================================================================
-- NordicCart OLTP | Verification 06: V006 primary keys, unique, foreign key
--                                    and check constraints
-- -----------------------------------------------------------------------------
-- Scope       : orders, order_items, payment_attempts (migration V006)
-- Connect to  : nordiccart_oltp (any role that can read the system catalogs)
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS, and failed_checks = 0.
--               A constraint that exists but is not in the list shows as WARN.
-- -----------------------------------------------------------------------------
-- Compared for each constraint (by name): table, type, key columns in order
-- (PK, UNIQUE, FK) or referenced columns sorted by name (CHECK), FK target,
-- ON UPDATE / ON DELETE NO ACTION, MATCH SIMPLE (the in-store fulfilment FK
-- relies on MATCH SIMPLE so online orders with store_id NULL skip it), not
-- deferrable, validated.
-- Not here: the partial unique index on payment_attempts and the constraint
-- triggers (07_verify_v006_indexes_triggers_and_types.sql), and the order_items
-- promotion FK, which arrives with promotions in DBML Phase 2.
-- CHECK expressions are proven by tests/sql/test_v006_orders_and_payments.sql.
-- =============================================================================

WITH expected_constraints (table_name, constraint_name, constraint_type, key_columns, ref_table, ref_columns) AS (
    VALUES
    -- Primary keys
    ('orders',           'pk_orders',           'PRIMARY KEY', 'order_id',           NULL, NULL),
    ('order_items',      'pk_order_items',      'PRIMARY KEY', 'order_item_id',      NULL, NULL),
    ('payment_attempts', 'pk_payment_attempts', 'PRIMARY KEY', 'payment_attempt_id', NULL, NULL),

    -- Unique constraints
    ('orders',           'uq_orders_order_number',                 'UNIQUE', 'order_number',              NULL, NULL),
    ('order_items',      'uq_order_items_order_line',              'UNIQUE', 'order_id, line_number',     NULL, NULL),
    ('order_items',      'uq_order_items_order_product',           'UNIQUE', 'order_id, product_id',      NULL, NULL),
    ('payment_attempts', 'uq_payment_attempts_order_attempt',      'UNIQUE', 'order_id, attempt_number',  NULL, NULL),
    ('payment_attempts', 'uq_payment_attempts_provider_reference', 'UNIQUE', 'provider_reference',        NULL, NULL),

    -- Foreign keys (names from the DBML Ref definitions)
    ('orders',           'fk_orders_customer',            'FOREIGN KEY', 'customer_id',                      'customers',       'customer_id'),
    ('orders',           'fk_orders_store_fulfilment',    'FOREIGN KEY', 'store_id, fulfilment_location_id', 'stores',          'store_id, stock_location_id'),
    ('orders',           'fk_orders_fulfilment_location', 'FOREIGN KEY', 'fulfilment_location_id',           'stock_locations', 'stock_location_id'),
    ('orders',           'fk_orders_country',             'FOREIGN KEY', 'country_code',                     'countries',       'country_code'),
    ('orders',           'fk_orders_currency',            'FOREIGN KEY', 'currency_code',                    'currencies',      'currency_code'),
    ('order_items',      'fk_order_items_order',          'FOREIGN KEY', 'order_id',                         'orders',          'order_id'),
    ('order_items',      'fk_order_items_product',        'FOREIGN KEY', 'product_id',                       'products',        'product_id'),
    ('payment_attempts', 'fk_payment_attempts_order',     'FOREIGN KEY', 'order_id',                         'orders',          'order_id'),

    -- Check constraints on orders (columns listed alphabetically)
    ('orders', 'ck_orders_store_matches_channel', 'CHECK', 'channel, store_id',                                                    NULL, NULL),
    ('orders', 'ck_orders_online_customer',       'CHECK', 'channel, customer_id',                                                 NULL, NULL),
    ('orders', 'ck_orders_online_address',        'CHECK', 'channel, shipping_address_line1, shipping_city, shipping_postal_code', NULL, NULL),
    ('orders', 'ck_orders_in_store_no_shipping',  'CHECK', 'channel, shipping_fee',                                                NULL, NULL),
    ('orders', 'ck_orders_amounts_non_negative',  'CHECK', 'items_total, shipping_fee',                                            NULL, NULL),
    ('orders', 'ck_orders_shipping_vat',          'CHECK', 'shipping_fee, shipping_vat_amount',                                    NULL, NULL),
    ('orders', 'ck_orders_total',                 'CHECK', 'items_total, order_total, shipping_fee',                               NULL, NULL),
    ('orders', 'ck_orders_cancelled_at',          'CHECK', 'cancelled_at, status',                                                 NULL, NULL),
    ('orders', 'ck_orders_confirmed_at',          'CHECK', 'confirmed_at, status',                                                 NULL, NULL),
    ('orders', 'ck_orders_fulfilled_at',          'CHECK', 'fulfilled_at, status',                                                 NULL, NULL),

    -- Check constraints on order_items
    ('order_items', 'ck_order_items_quantity',           'CHECK', 'quantity',                                          NULL, NULL),
    ('order_items', 'ck_order_items_price_cost',         'CHECK', 'unit_cost, unit_price',                             NULL, NULL),
    ('order_items', 'ck_order_items_discount_percent',   'CHECK', 'discount_percent',                                  NULL, NULL),
    ('order_items', 'ck_order_items_promotion_discount', 'CHECK', 'discount_percent, promotion_id',                    NULL, NULL),
    ('order_items', 'ck_order_items_discount_rounding',  'CHECK', 'discount_amount, discount_percent, quantity, unit_price', NULL, NULL),
    ('order_items', 'ck_order_items_line_total',         'CHECK', 'discount_amount, line_total, quantity, unit_price', NULL, NULL),
    ('order_items', 'ck_order_items_vat_rate',           'CHECK', 'vat_rate',                                          NULL, NULL),
    ('order_items', 'ck_order_items_vat_rounding',       'CHECK', 'line_total, vat_amount, vat_rate',                  NULL, NULL),

    -- Check constraints on payment_attempts
    ('payment_attempts', 'ck_payment_attempts_attempt_number', 'CHECK', 'attempt_number',                   NULL, NULL),
    ('payment_attempts', 'ck_payment_attempts_amount',         'CHECK', 'amount',                           NULL, NULL),
    ('payment_attempts', 'ck_payment_attempts_failed',         'CHECK', 'failed_at, failure_reason, status', NULL, NULL),
    ('payment_attempts', 'ck_payment_attempts_authorized_at',  'CHECK', 'authorized_at, status',            NULL, NULL),
    ('payment_attempts', 'ck_payment_attempts_captured_at',    'CHECK', 'captured_at, status',              NULL, NULL)
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
