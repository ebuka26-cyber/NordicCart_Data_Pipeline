-- =============================================================================
-- NordicCart OLTP | Verification 07: V006 enum types, indexes, triggers,
--                                    functions and identity settings
-- -----------------------------------------------------------------------------
-- Scope       : objects created by migration V006
-- Connect to  : nordiccart_oltp (any role that can read the system catalogs)
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS, and failed_checks = 0.
--               Enums and functions from V001 to V005 show as INFO (checked by 04).
--               An object on these tables that is not in the lists shows as WARN.
-- -----------------------------------------------------------------------------
-- Indexes behind PRIMARY KEY and UNIQUE constraints are checked in
-- 06_verify_v006_keys_and_constraints.sql. This script checks the plain
-- indexes from the DBML index blocks and the [PG] partial unique index
-- uq_payment_attempts_one_live_per_order (an index, not a constraint).
-- Revision 2 (2026-11-16): guards also cover DELETE; TRUNCATE guards and
-- fn_reject_history_truncate added.
-- =============================================================================

WITH
-- -----------------------------------------------------------------------------
-- Enum types: labels in their defined order
-- -----------------------------------------------------------------------------
expected_enums (object_name, definition) AS (
    VALUES ('order_status',   'placed, stock_reserved, confirmed, fulfilled, payment_failed, cancelled'),
           ('payment_method', 'card, mobile_payment, buy_now_pay_later, cash'),
           ('payment_status', 'pending, authorized, captured, failed, voided')
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
-- Indexes from the DBML index blocks, plus the [PG] partial unique index
-- -----------------------------------------------------------------------------
expected_indexes (table_name, object_name, definition) AS (
    VALUES ('orders',           'ix_orders_customer_id',                  'btree (customer_id)'),
           ('orders',           'ix_orders_store_id',                     'btree (store_id)'),
           ('orders',           'ix_orders_fulfilment_location_id',       'btree (fulfilment_location_id)'),
           ('orders',           'ix_orders_status_placed_at',             'btree (status, placed_at)'),
           ('orders',           'ix_orders_placed_at',                    'btree (placed_at)'),
           ('orders',           'ix_orders_updated_at',                   'btree (updated_at)'),
           ('order_items',      'ix_order_items_product_id',              'btree (product_id)'),
           ('order_items',      'ix_order_items_promotion_id',            'btree (promotion_id)'),
           ('order_items',      'ix_order_items_updated_at',              'btree (updated_at)'),
           ('payment_attempts', 'ix_payment_attempts_status_attempted_at', 'btree (status, attempted_at)'),
           ('payment_attempts', 'ix_payment_attempts_updated_at',          'btree (updated_at)'),
           ('payment_attempts', 'uq_payment_attempts_one_live_per_order',
            'UNIQUE btree (order_id) WHERE (status = ANY (ARRAY[''pending''::payment_status, ''authorized''::payment_status, ''captured''::payment_status]))')
),
scope_tables (table_name) AS (
    VALUES ('orders'), ('order_items'), ('payment_attempts')
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
    -- orders
    ('orders', 'trg_orders_guard',             'BEFORE ROW INSERT OR DELETE OR UPDATE -> fn_orders_guard()'),
    ('orders', 'trg_orders_no_truncate',       'BEFORE STATEMENT TRUNCATE -> fn_reject_history_truncate()'),
    ('orders', 'trg_orders_consistency_check', 'AFTER ROW INSERT OR UPDATE OF status, items_total, shipping_fee, shipping_vat_amount -> fn_check_order_consistency() [constraint, initially deferred]'),
    ('orders', 'trg_orders_set_updated_at',    'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('orders', 'trg_orders_capture_change',    'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''order_id'')'),
    -- order_items
    ('order_items', 'trg_order_items_guard',             'BEFORE ROW INSERT OR DELETE OR UPDATE -> fn_order_items_guard()'),
    ('order_items', 'trg_order_items_no_truncate',       'BEFORE STATEMENT TRUNCATE -> fn_reject_history_truncate()'),
    ('order_items', 'trg_order_items_consistency_check', 'AFTER ROW INSERT OR DELETE OR UPDATE OF line_total, vat_rate -> fn_check_order_consistency() [constraint, initially deferred]'),
    ('order_items', 'trg_order_items_set_updated_at',    'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('order_items', 'trg_order_items_capture_change',    'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''order_item_id'')'),
    -- payment_attempts
    ('payment_attempts', 'trg_payment_attempts_guard',             'BEFORE ROW INSERT OR DELETE OR UPDATE -> fn_payment_attempts_guard()'),
    ('payment_attempts', 'trg_payment_attempts_no_truncate',       'BEFORE STATEMENT TRUNCATE -> fn_reject_history_truncate()'),
    ('payment_attempts', 'trg_payment_attempts_consistency_check', 'AFTER ROW INSERT OR UPDATE OF status -> fn_check_order_consistency() [constraint, initially deferred]'),
    ('payment_attempts', 'trg_payment_attempts_set_updated_at',    'BEFORE ROW UPDATE -> fn_set_updated_at()'),
    ('payment_attempts', 'trg_payment_attempts_capture_change',    'AFTER ROW INSERT OR DELETE OR UPDATE -> fn_capture_change(''payment_attempt_id'')')
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
    VALUES ('public.fn_orders_guard()',                   'plpgsql, returns trigger'),
           ('public.fn_order_items_guard()',              'plpgsql, returns trigger'),
           ('public.fn_payment_attempts_guard()',         'plpgsql, returns trigger'),
           ('public.fn_assert_order_consistent(bigint)',  'plpgsql, returns void'),
           ('public.fn_check_order_consistency()',        'plpgsql, returns trigger'),
           ('public.fn_reject_history_truncate()',        'plpgsql, returns trigger')
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
-- Identity settings: ALWAYS on every surrogate key
-- -----------------------------------------------------------------------------
expected_identities (table_name, object_name, definition) AS (
    VALUES ('orders',           'order_id',           'GENERATED ALWAYS'),
           ('order_items',      'order_item_id',      'GENERATED ALWAYS'),
           ('payment_attempts', 'payment_attempt_id', 'GENERATED ALWAYS')
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
