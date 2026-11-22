-- =============================================================================
-- NordicCart OLTP | Verification 10: V007 enum types, indexes, triggers,
--                                    functions and identity settings
-- -----------------------------------------------------------------------------
-- Scope       : objects created by migration V007, including its two triggers
--               on the V006 tables orders and payment_attempts
-- Connect to  : nordiccart_oltp (any role that can read the system catalogs)
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS, and failed_checks = 0.
--               Enums and functions from V001 to V006 show as INFO (checked by 04 and 07).
--               An object on the inventory tables that is not in the lists shows as WARN.
-- -----------------------------------------------------------------------------
-- Indexes behind PRIMARY KEY and UNIQUE constraints are checked in
-- 09_verify_v007_keys_and_constraints.sql. This script checks the plain
-- indexes from the DBML index blocks, the four [PG] partial unique indexes and
-- the two V007 idempotency indexes.
-- =============================================================================

WITH
-- -----------------------------------------------------------------------------
-- Enum types: labels in their defined order
-- -----------------------------------------------------------------------------
expected_enums (object_name, definition) AS (
    VALUES ('reservation_status', 'active, consumed, released, expired'),
           ('movement_type',      'opening_balance, purchase_receipt, transfer_out, transfer_in, sale, return_restock, stock_adjustment, write_off')
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
-- Indexes from the DBML index blocks, the [PG] partial uniques and the V007 idempotency indexes
-- -----------------------------------------------------------------------------
expected_indexes (table_name, object_name, definition) AS (
    VALUES ('inventory_balances',     'ix_inventory_balances_product_id',                  'btree (product_id)'),
           ('inventory_balances',     'ix_inventory_balances_updated_at',                  'btree (updated_at)'),
           ('inventory_reservations', 'ix_inventory_reservations_order_item_id',           'btree (order_item_id)'),
           ('inventory_reservations', 'ix_inventory_reservations_location_product_status', 'btree (stock_location_id, product_id, status)'),
           ('inventory_reservations', 'ix_inventory_reservations_status_expires_at',       'btree (status, expires_at)'),
           ('inventory_reservations', 'ix_inventory_reservations_updated_at',              'btree (updated_at)'),
           ('inventory_movements',    'ix_inventory_movements_location_product_time',      'btree (stock_location_id, product_id, occurred_at)'),
           ('inventory_movements',    'ix_inventory_movements_order_item_id',              'btree (order_item_id)'),
           ('inventory_movements',    'ix_inventory_movements_return_item_id',             'btree (return_item_id)'),
           ('inventory_movements',    'ix_inventory_movements_related_movement_id',        'btree (related_movement_id)'),
           ('inventory_movements',    'ix_inventory_movements_created_at',                 'btree (created_at)'),
           -- [PG] partial uniques from the DBML
           ('inventory_reservations', 'uq_inventory_reservations_one_active_per_line',
            'UNIQUE btree (order_item_id) WHERE (status = ''active''::reservation_status)'),
           ('inventory_movements',    'uq_inventory_movements_one_sale_per_line',
            'UNIQUE btree (order_item_id) WHERE (movement_type = ''sale''::movement_type)'),
           ('inventory_movements',    'uq_inventory_movements_one_restock_per_return_item',
            'UNIQUE btree (return_item_id) WHERE (movement_type = ''return_restock''::movement_type)'),
           ('inventory_movements',    'uq_inventory_movements_one_transfer_in_per_out',
            'UNIQUE btree (related_movement_id) WHERE (movement_type = ''transfer_in''::movement_type)'),
           -- V007 idempotency keys
           ('inventory_movements',    'uq_inventory_movements_one_opening_balance',
            'UNIQUE btree (stock_location_id, product_id) WHERE (movement_type = ''opening_balance''::movement_type)'),
           ('inventory_movements',    'uq_inventory_movements_document_line',
            'UNIQUE btree (movement_type, stock_location_id, product_id, external_reference) WHERE (external_reference IS NOT NULL)')
),
scope_tables (table_name) AS (
    VALUES ('inventory_balances'), ('inventory_reservations'), ('inventory_movements')
),
actual_indexes AS (
    SELECT t.relname::text AS table_name,
           i.relname::text AS object_name,
           CASE WHEN x.indisunique THEN 'UNIQUE ' ELSE '' END
             || regexp_replace(regexp_replace(pg_get_indexdef(x.indexrelid), '^.* USING ', ''), 'public\.', '', 'g') AS definition
      FROM pg_index AS x
      JOIN pg_class AS i ON i.oid = x.indexrelid
      JOIN pg_class AS t ON t.oid = x.indrelid
      JOIN pg_namespace AS n ON n.oid = t.relnamespace
     WHERE n.nspname = 'public'
       AND t.relname IN (SELECT table_name FROM scope_tables)
       AND NOT EXISTS (SELECT 1 FROM pg_constraint AS c
                        WHERE c.conindid = x.indexrelid
                          AND c.conrelid = x.indrelid)   -- skip PK / UNIQUE constraint indexes
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
    -- inventory_balances
    ('inventory_balances', 'trg_inventory_balances_guard',          'BEFORE ROW INSERT OR UPDATE -> fn_inventory_balances_guard()'),
    ('inventory_balances', 'trg_inventory_balances_no_truncate',    'BEFORE STATEMENT TRUNCATE -> fn_reject_inventory_truncate()'),
    ('inventory_balances', 'trg_inventory_balances_set_updated_at', 'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('inventory_balances', 'trg_inventory_balances_capture_change', 'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''inventory_balance_id'')'),
    -- inventory_reservations
    ('inventory_reservations', 'trg_inventory_reservations_guard',            'BEFORE ROW INSERT OR DELETE OR UPDATE -> fn_inventory_reservations_guard()'),
    ('inventory_reservations', 'trg_inventory_reservations_no_truncate',      'BEFORE STATEMENT TRUNCATE -> fn_reject_inventory_truncate()'),
    ('inventory_reservations', 'trg_inventory_reservations_apply_to_balance', 'AFTER ROW INSERT OR UPDATE OF status -> fn_inventory_apply_reservation()'),
    ('inventory_reservations', 'trg_inventory_reservations_order_check',      'AFTER ROW INSERT OR UPDATE OF status -> fn_check_order_inventory() [constraint, initially deferred]'),
    ('inventory_reservations', 'trg_inventory_reservations_set_updated_at',   'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('inventory_reservations', 'trg_inventory_reservations_capture_change',   'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''reservation_id'')'),
    -- inventory_movements
    ('inventory_movements', 'trg_inventory_movements_guard',            'BEFORE ROW INSERT -> fn_inventory_movements_guard()'),
    ('inventory_movements', 'trg_inventory_movements_insert_only',      'BEFORE ROW DELETE OR UPDATE -> fn_reject_modification()'),
    ('inventory_movements', 'trg_inventory_movements_no_truncate',      'BEFORE STATEMENT TRUNCATE -> fn_reject_inventory_truncate()'),
    ('inventory_movements', 'trg_inventory_movements_apply_to_balance', 'AFTER ROW INSERT -> fn_inventory_apply_movement()'),
    ('inventory_movements', 'trg_inventory_movements_order_check',      'AFTER ROW INSERT -> fn_check_order_inventory() [constraint, initially deferred]'),
    ('inventory_movements', 'trg_inventory_movements_capture_change',   'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''movement_id'')'),
    -- V007 triggers on V006 tables
    ('orders',           'trg_orders_inventory_check',           'AFTER ROW UPDATE OF status -> fn_check_order_inventory() [constraint, initially deferred]'),
    ('payment_attempts', 'trg_payment_attempts_inventory_guard', 'BEFORE ROW UPDATE OF status -> fn_payment_attempts_inventory_guard()')
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
       AND (c.relname IN (SELECT table_name FROM scope_tables)
            OR t.tgname IN (SELECT object_name FROM expected_triggers))
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
    VALUES ('public.fn_inventory_apply_movement()',            'plpgsql, returns trigger'),
           ('public.fn_inventory_apply_reservation()',         'plpgsql, returns trigger'),
           ('public.fn_inventory_balances_guard()',            'plpgsql, returns trigger'),
           ('public.fn_inventory_reservations_guard()',        'plpgsql, returns trigger'),
           ('public.fn_inventory_movements_guard()',           'plpgsql, returns trigger'),
           ('public.fn_payment_attempts_inventory_guard()',    'plpgsql, returns trigger'),
           ('public.fn_reject_inventory_truncate()',           'plpgsql, returns trigger'),
           ('public.fn_assert_order_inventory_consistent(bigint)', 'plpgsql, returns void'),
           ('public.fn_check_order_inventory()',               'plpgsql, returns trigger'),
           ('public.fn_lock_order_balances(bigint)',           'plpgsql, returns void'),
           ('public.fn_reserve_order_stock(bigint, timestamp with time zone, timestamp with time zone)', 'plpgsql, returns text'),
           ('public.fn_close_order_reservations(bigint, reservation_status, timestamp with time zone)', 'plpgsql, returns integer'),
           ('public.fn_fulfil_order_stock(bigint, timestamp with time zone)', 'plpgsql, returns text')
),
actual_functions AS (
    SELECT n.nspname || '.' || p.proname || '(' || regexp_replace(oidvectortypes(p.proargtypes), 'public\.', '', 'g') || ')' AS object_name,
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
-- Identity settings: ALWAYS on every surrogate key
-- -----------------------------------------------------------------------------
expected_identities (table_name, object_name, definition) AS (
    VALUES ('inventory_balances',     'inventory_balance_id', 'GENERATED ALWAYS'),
           ('inventory_reservations', 'reservation_id',       'GENERATED ALWAYS'),
           ('inventory_movements',    'movement_id',          'GENERATED ALWAYS')
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
