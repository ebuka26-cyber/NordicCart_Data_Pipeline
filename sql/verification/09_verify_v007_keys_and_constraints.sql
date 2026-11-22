-- =============================================================================
-- NordicCart OLTP | Verification 09: V007 primary keys, unique, foreign key
--                                    and check constraints
-- -----------------------------------------------------------------------------
-- Scope       : inventory_balances, inventory_reservations, inventory_movements
-- Connect to  : nordiccart_oltp (any role that can read the system catalogs)
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS, and failed_checks = 0.
--               A constraint that exists but is not in the list shows as WARN.
-- -----------------------------------------------------------------------------
-- Compared for each constraint (by name): table, type, key columns in order
-- (PK, UNIQUE, FK) or referenced columns sorted by name (CHECK), FK target,
-- ON UPDATE / ON DELETE NO ACTION, MATCH SIMPLE (the nullable order_item_id and
-- related_movement_id FKs rely on it), not deferrable, validated.
-- Not here: the six partial unique indexes and the triggers
-- (10_verify_v007_indexes_triggers_and_types.sql), and the return_items FK,
-- which arrives with returns in DBML Phase 4.
-- CHECK expressions are proven by tests/sql/test_v007_inventory.sql.
-- =============================================================================

WITH expected_constraints (table_name, constraint_name, constraint_type, key_columns, ref_table, ref_columns) AS (
    VALUES
    -- Primary keys
    ('inventory_balances',     'pk_inventory_balances',     'PRIMARY KEY', 'inventory_balance_id', NULL, NULL),
    ('inventory_reservations', 'pk_inventory_reservations', 'PRIMARY KEY', 'reservation_id',       NULL, NULL),
    ('inventory_movements',    'pk_inventory_movements',    'PRIMARY KEY', 'movement_id',          NULL, NULL),

    -- Unique constraint (one balance per location and product; target of the composite FKs)
    ('inventory_balances', 'uq_inventory_balances_location_product', 'UNIQUE', 'stock_location_id, product_id', NULL, NULL),

    -- Foreign keys (names from the DBML Ref definitions)
    ('inventory_balances',     'fk_inventory_balances_location',       'FOREIGN KEY', 'stock_location_id',             'stock_locations',     'stock_location_id'),
    ('inventory_balances',     'fk_inventory_balances_product',        'FOREIGN KEY', 'product_id',                    'products',            'product_id'),
    ('inventory_reservations', 'fk_inventory_reservations_balance',    'FOREIGN KEY', 'stock_location_id, product_id', 'inventory_balances',  'stock_location_id, product_id'),
    ('inventory_reservations', 'fk_inventory_reservations_order_item', 'FOREIGN KEY', 'order_item_id',                 'order_items',         'order_item_id'),
    ('inventory_movements',    'fk_inventory_movements_balance',       'FOREIGN KEY', 'stock_location_id, product_id', 'inventory_balances',  'stock_location_id, product_id'),
    ('inventory_movements',    'fk_inventory_movements_order_item',    'FOREIGN KEY', 'order_item_id',                 'order_items',         'order_item_id'),
    ('inventory_movements',    'fk_inventory_movements_related',       'FOREIGN KEY', 'related_movement_id',           'inventory_movements', 'movement_id'),

    -- Check constraints (columns listed alphabetically)
    ('inventory_balances',     'ck_inventory_balances_on_hand',           'CHECK', 'quantity_on_hand',                     NULL, NULL),
    ('inventory_balances',     'ck_inventory_balances_reserved',          'CHECK', 'quantity_reserved',                    NULL, NULL),
    ('inventory_balances',     'ck_inventory_balances_no_oversell',       'CHECK', 'quantity_on_hand, quantity_reserved',  NULL, NULL),
    ('inventory_balances',     'ck_inventory_balances_reorder_point',     'CHECK', 'reorder_point',                        NULL, NULL),
    ('inventory_reservations', 'ck_inventory_reservations_quantity',      'CHECK', 'quantity',                             NULL, NULL),
    ('inventory_reservations', 'ck_inventory_reservations_expiry',        'CHECK', 'expires_at, reserved_at',              NULL, NULL),
    ('inventory_reservations', 'ck_inventory_reservations_closed_at',     'CHECK', 'closed_at, status',                    NULL, NULL),
    ('inventory_movements',    'ck_inventory_movements_nonzero',          'CHECK', 'quantity_change',                      NULL, NULL),
    ('inventory_movements',    'ck_inventory_movements_inbound_positive', 'CHECK', 'movement_type, quantity_change',       NULL, NULL),
    ('inventory_movements',    'ck_inventory_movements_outbound_negative','CHECK', 'movement_type, quantity_change',       NULL, NULL),
    ('inventory_movements',    'ck_inventory_movements_sale_ref',         'CHECK', 'movement_type, order_item_id',         NULL, NULL),
    ('inventory_movements',    'ck_inventory_movements_restock_ref',      'CHECK', 'movement_type, return_item_id',        NULL, NULL),
    ('inventory_movements',    'ck_inventory_movements_transfer_ref',     'CHECK', 'movement_type, related_movement_id',   NULL, NULL)
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
