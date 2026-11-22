-- =============================================================================
-- NordicCart OLTP | Verification 18: V009 activity control and purchase orders
-- -----------------------------------------------------------------------------
-- Scope       : V009 (Phase 3 step 5, stage S4):
--               1 structure    the five tables, enums, constraints, triggers
--               2 security     owners, SECURITY DEFINER functions, privileges of the
--                              new tables and functions (V008 roles)
--               3 receipts     every purchase receipt maps to an order line; received
--                              quantities and statuses equal the ledger
--               4 pilot (E3)   the 4 pilot purchase orders, their lines and receipts
--               5 R0 (C18)     the 69 pilot_r0 revisions of the replenishment pilot
--               6 history      reorder points equal their latest revision; chains intact
--               7 run ledger   verified runs have their checkpoint; windows contiguous
--               8 INFO
-- Connect to  : nordiccart_oltp (nordiccart_analyst or postgres). Read-only: one
--               SELECT statement, no writes, no temp objects.
-- Expected    : after V009 AND the pilot document backfill (run_replenishment.py
--               --apply): every row PASS or INFO, failed_checks = 0 (44 rows: 38 PASS,
--               6 INFO). Right after V009, before the backfill, groups 3 to 5 FAIL by
--               design (the documents are not written yet). Before V009 the script
--               stops with "relation does not exist": run it only after V009.
-- Parameters  : the params CTE holds the pilot documents (replenishment pilot plan,
--               fingerprint 858bbfd0..., output/replenishment/pilot/summary.json).
-- =============================================================================

WITH params AS (
    SELECT ARRAY['PO-DK-2024-000001', 'PO-FI-2024-000001', 'PO-NO-2024-000001', 'PO-SE-2024-000001']::text[] AS pilot_orders,
           4    AS expected_pilot_lines,
           26   AS expected_pilot_units,
           5    AS expected_pilot_receipts,
           69   AS expected_r0_rows,
           'R0-DK-WH-01-20241115 17, R0-FI-WH-01-20241115 17, R0-NO-WH-01-20241115 17, R0-SE-WH-01-20241115 18'::text
                AS expected_r0_documents,
           '301 -> 511'::text AS expected_r0_totals,                -- sum of old -> sum of new reorder points
           TIMESTAMPTZ '2024-11-15 00:00+00' AS r0_day_start,
           TIMESTAMPTZ '2024-11-16 00:00+00' AS r0_day_end,
           ARRAY['public.purchase_orders', 'public.purchase_order_lines', 'public.reorder_point_revisions',
                 'meta.activity_runs', 'meta.activity_checkpoints']::text[] AS v009_tables,
           ARRAY['nordiccart_writer', 'nordiccart_loader', 'nordiccart_scheduler', 'nordiccart_reader',
                 'nordiccart_analyst', 'nordiccart_extractor', 'public']::text[] AS app_roles,
           'search_path=pg_catalog, public, pg_temp'::text AS definer_path
),
new_tables AS (
    SELECT c.oid, n.nspname || '.' || c.relname AS tbl, c.relname, c.relowner
      FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace, params AS p
     WHERE c.relkind = 'r' AND n.nspname || '.' || c.relname = ANY (p.v009_tables)
),
ours_proc AS (
    SELECT pr.oid, pr.oid::regprocedure::text AS signature, pr.proname, pr.prosecdef, pr.proconfig, pr.proowner
      FROM pg_proc AS pr JOIN pg_namespace AS n ON n.oid = pr.pronamespace
     WHERE n.nspname IN ('public', 'meta')
       AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_proc'::regclass AND d.objid = pr.oid AND d.deptype = 'e')
),
v009_proc AS (
    SELECT * FROM ours_proc
     WHERE proname IN ('fn_purchase_order_line_received', 'fn_purchase_order_state', 'fn_purchase_order_refresh',
                       'fn_purchase_orders_guard', 'fn_purchase_order_lines_guard', 'fn_purchase_order_lines_refresh_order',
                       'fn_purchase_receipt_apply', 'fn_check_purchase_order_complete', 'fn_reorder_point_revisions_guard',
                       'fn_check_reorder_point_revision_applied', 'fn_activity_runs_guard', 'fn_check_activity_run_verified',
                       'fn_activity_checkpoints_guard', 'fn_check_activity_checkpoint_verified')
),
-- purchase receipts with the order they name (NULL when the reference is not a PO reference)
receipts AS (
    SELECT m.movement_id, m.stock_location_id, m.product_id, m.quantity_change, m.occurred_at, m.external_reference,
           CASE WHEN m.external_reference ~ '^PO-[A-Z]{2}-[0-9]{4}-[0-9]{6}/DN[1-9][0-9]*$'
                THEN split_part(m.external_reference, '/', 1) END AS order_number
      FROM public.inventory_movements AS m
     WHERE m.movement_type = 'purchase_receipt'
),
lines AS (
    SELECT o.purchase_order_id, o.purchase_order_number, o.stock_location_id AS order_location, o.ordered_at, o.status AS order_status,
           o.received_at, l.purchase_order_line_id, l.line_number, l.stock_location_id, l.product_id, l.order_reason,
           l.quantity_ordered, l.quantity_received, l.quantity_outstanding, l.status
      FROM public.purchase_orders AS o JOIN public.purchase_order_lines AS l USING (purchase_order_id)
),
latest_revision AS (
    SELECT DISTINCT ON (stock_location_id, product_id) stock_location_id, product_id, new_reorder_point
      FROM public.reorder_point_revisions ORDER BY stock_location_id, product_id, effective_at DESC
),
chained AS (
    SELECT r.*, lag(r.new_reorder_point) OVER (PARTITION BY r.stock_location_id, r.product_id ORDER BY r.effective_at) AS previous_new
      FROM public.reorder_point_revisions AS r
),
runs AS (
    SELECT r.*, lag(r.window_end) OVER (ORDER BY r.window_start) AS previous_end,
           lag(r.activity_run_id) OVER (ORDER BY r.window_start) AS previous_id
      FROM meta.activity_runs AS r
),
checks (check_no, check_type, object_name, expected, actual) AS (
    -- 1. Structure -----------------------------------------------------------------------------
    SELECT 101, 'structure', 'Migration V009 recorded', 'V009__activity_control_and_purchase_orders.sql',
           (SELECT script_name FROM meta.schema_migrations WHERE version = '009')::text
    UNION ALL
    SELECT 102, 'structure', 'Columns of the five V009 tables',
           'activity_checkpoints 6, activity_runs 20, purchase_order_lines 12, purchase_orders 9, reorder_point_revisions 9',
           (SELECT string_agg(relname || ' ' || n, ', ' ORDER BY relname)
              FROM (SELECT t.relname, count(*) AS n FROM new_tables AS t
                      JOIN pg_attribute AS a ON a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped GROUP BY 1) x)
    UNION ALL
    SELECT 103, 'structure', 'Enum types and labels',
           'activity_run_mode(catch_up,live); activity_run_status(running,verified,failed); purchase_order_reason(reorder_point,launch,seasonal,forced_reorder); '
           || 'purchase_order_status(open,partially_received,received); reorder_point_revision_kind(pilot_r0,monthly_review)',
           (SELECT string_agg(typname || '(' || labels || ')', '; ' ORDER BY typname)
              FROM (SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) AS labels
                      FROM pg_type AS t JOIN pg_enum AS e ON e.enumtypid = t.oid
                     WHERE t.typname IN ('activity_run_mode', 'activity_run_status', 'purchase_order_reason',
                                         'purchase_order_status', 'reorder_point_revision_kind') GROUP BY 1) x)
    UNION ALL
    SELECT 104, 'structure', 'Key, unique, foreign key, check and exclusion constraints per table',
           'activity_checkpoints 6, activity_runs 15, purchase_order_lines 9, purchase_orders 8, reorder_point_revisions 8',
           (SELECT string_agg(relname || ' ' || n, ', ' ORDER BY relname)
              FROM (SELECT t.relname, count(*) AS n FROM new_tables AS t
                      JOIN pg_constraint AS c ON c.conrelid = t.oid AND c.contype IN ('p', 'u', 'f', 'c', 'x') GROUP BY 1) x)
    UNION ALL
    SELECT 105, 'structure', 'Triggers per table (V009 tables, plus the receipt trigger on inventory_movements)',
           'activity_checkpoints 4, activity_runs 5, inventory_movements 1, purchase_order_lines 6, purchase_orders 6, reorder_point_revisions 5',
           (SELECT string_agg(relname || ' ' || n, ', ' ORDER BY relname)
              FROM (SELECT c.relname, count(*) AS n FROM pg_trigger AS tg JOIN pg_class AS c ON c.oid = tg.tgrelid
                     WHERE NOT tg.tgisinternal
                       AND (c.oid IN (SELECT oid FROM new_tables) OR tg.tgname = 'trg_inventory_movements_purchase_order_receipt')
                     GROUP BY 1) x)
    UNION ALL
    SELECT 106, 'structure', 'Deferred (commit-time) constraint triggers',
           'trg_activity_checkpoints_run_verified, trg_activity_runs_verified_with_checkpoint, trg_purchase_orders_complete, trg_reorder_point_revisions_applied',
           (SELECT string_agg(tg.tgname::text, ', ' ORDER BY tg.tgname) FROM pg_trigger AS tg
             WHERE tg.tgrelid IN (SELECT oid FROM new_tables) AND tg.tgdeferrable AND tg.tginitdeferred)
    UNION ALL
    SELECT 107, 'structure', 'Tables captured in change_log (business tables only; meta tables are engine metadata)',
           'purchase_order_lines, purchase_orders, reorder_point_revisions',
           (SELECT string_agg(DISTINCT c.relname::text, ', ' ORDER BY c.relname::text) FROM pg_trigger AS tg
              JOIN pg_class AS c ON c.oid = tg.tgrelid JOIN pg_proc AS pr ON pr.oid = tg.tgfoid
             WHERE pr.proname = 'fn_capture_change' AND c.oid IN (SELECT oid FROM new_tables))

    -- 2. Security ----------------------------------------------------------------------------
    UNION ALL
    SELECT 201, 'security', 'V009 tables, enums and functions not owned by nordiccart_owner', '0',
           ((SELECT count(*) FROM new_tables WHERE relowner <> (SELECT oid FROM pg_roles WHERE rolname = 'nordiccart_owner'))
            + (SELECT count(*) FROM v009_proc WHERE proowner <> (SELECT oid FROM pg_roles WHERE rolname = 'nordiccart_owner'))
            + (SELECT count(*) FROM pg_type WHERE typname IN ('activity_run_mode', 'activity_run_status', 'purchase_order_reason',
                                                               'purchase_order_status', 'reorder_point_revision_kind')
                  AND typowner <> (SELECT oid FROM pg_roles WHERE rolname = 'nordiccart_owner')))::text
    UNION ALL
    SELECT 202, 'security', 'SECURITY DEFINER functions (V008 three, V009 two)',
           'fn_capture_change(), fn_inventory_apply_movement(), fn_inventory_apply_reservation(), fn_purchase_order_lines_refresh_order(), fn_purchase_receipt_apply()',
           (SELECT string_agg(signature, ', ' ORDER BY signature) FROM ours_proc WHERE prosecdef)
    UNION ALL
    SELECT 203, 'security', 'SECURITY DEFINER functions without search_path = pg_catalog, public, pg_temp', '0',
           (SELECT count(*) FROM ours_proc, params WHERE prosecdef AND proconfig IS DISTINCT FROM ARRAY[params.definer_path])::text
    UNION ALL
    SELECT 204, 'security', 'PUBLIC privileges on the V009 tables and functions', '0',
           ((SELECT count(*) FROM new_tables WHERE has_any_column_privilege('public', oid, 'SELECT,INSERT,UPDATE,REFERENCES'))
            + (SELECT count(*) FROM v009_proc WHERE has_function_privilege('public', oid, 'EXECUTE')))::text
    UNION ALL
    SELECT 205, 'security', 'Writer table privileges on the V009 tables',
           'activity_checkpoints INSERT,SELECT; activity_runs INSERT,SELECT; purchase_order_lines INSERT,SELECT; purchase_orders INSERT,SELECT; reorder_point_revisions INSERT,SELECT',
           (SELECT string_agg(relname || ' ' || privs, '; ' ORDER BY relname)
              FROM (SELECT t.relname, string_agg(pr.p, ',' ORDER BY pr.p) AS privs
                      FROM new_tables AS t
                      CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) AS pr(p)
                     WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nordiccart_writer')
                       AND has_table_privilege('nordiccart_writer', t.oid, pr.p)
                     GROUP BY 1) x)
    UNION ALL
    SELECT 206, 'security', 'Columns of the V009 tables the writer may UPDATE',
           'activity_runs(completed_units, end_state_hash, failure_reason, finished_at, recovery_point, recovery_point_sha256, status, unit_counts)',
           (SELECT string_agg(relname || '(' || cols || ')', '; ' ORDER BY relname)
              FROM (SELECT t.relname, string_agg(a.attname, ', ' ORDER BY a.attname) AS cols
                      FROM new_tables AS t JOIN pg_attribute AS a ON a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped
                     WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nordiccart_writer')
                       AND has_column_privilege('nordiccart_writer', t.oid, a.attname, 'UPDATE')
                     GROUP BY 1) x)
    UNION ALL
    SELECT 207, 'security', 'V009 functions the writer may execute (read-only helpers of the guards)',
           'fn_purchase_order_line_received(character varying,bigint,bigint), fn_purchase_order_state(bigint)',
           (SELECT string_agg(signature, ', ' ORDER BY signature) FROM v009_proc
             WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nordiccart_writer')
               AND has_function_privilege('nordiccart_writer', oid, 'EXECUTE'))
    UNION ALL
    SELECT 208, 'security', 'V009 tables the reader may SELECT (and nothing else)', '5 / 0',
           (SELECT count(*) FILTER (WHERE has_table_privilege('nordiccart_reader', oid, 'SELECT')) || ' / '
                   || count(*) FILTER (WHERE has_any_column_privilege('nordiccart_reader', oid, 'INSERT,UPDATE,REFERENCES'))
              FROM new_tables WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nordiccart_reader'))
    UNION ALL
    SELECT 209, 'security', 'Extractor privileges on the V009 tables', '0',
           (SELECT count(*) FROM new_tables
             WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nordiccart_extractor')
               AND (has_any_column_privilege('nordiccart_extractor', oid, 'SELECT,INSERT,UPDATE,REFERENCES')
                    OR has_table_privilege('nordiccart_extractor', oid, 'DELETE')))::text
    UNION ALL
    SELECT 210, 'security', 'DELETE, TRUNCATE, TRIGGER or REFERENCES on the V009 tables held by an application role', '0',
           (SELECT count(*) FROM new_tables AS t, params AS p, unnest(p.app_roles) AS a(r),
                   unnest(ARRAY['DELETE', 'TRUNCATE', 'TRIGGER', 'REFERENCES']) AS pr(priv)
             WHERE (a.r = 'public' OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname = a.r))
               AND has_table_privilege(a.r, t.oid, pr.priv))::text

    -- 3. Purchase orders and receipts (global) --------------------------------------------------
    UNION ALL
    SELECT 301, 'receipts', 'purchase receipts that do not map to a purchase order line (warehouse, product, PO reference)', '0',
           (SELECT count(*) FROM receipts AS r
             WHERE NOT EXISTS (SELECT 1 FROM lines AS l
                                WHERE l.purchase_order_number = r.order_number
                                  AND l.stock_location_id = r.stock_location_id AND l.product_id = r.product_id))::text
    UNION ALL
    SELECT 302, 'receipts', 'order lines whose quantity received differs from their receipts', '0',
           (SELECT count(*) FROM lines AS l
             WHERE l.quantity_received <> coalesce((SELECT sum(r.quantity_change) FROM receipts AS r
                                                    WHERE r.order_number = l.purchase_order_number
                                                      AND r.stock_location_id = l.stock_location_id
                                                      AND r.product_id = l.product_id), 0))::text
    UNION ALL
    SELECT 303, 'receipts', 'orders whose status or received_at differs from their lines and receipts', '0',
           (SELECT count(*) FROM public.purchase_orders AS o,
                   LATERAL (SELECT CASE WHEN count(*) = 0 OR sum(l.quantity_received) = 0 THEN 'open'
                                        WHEN bool_and(l.quantity_received = l.quantity_ordered) THEN 'received'
                                        ELSE 'partially_received' END AS status,
                                   CASE WHEN count(*) > 0 AND bool_and(l.quantity_received = l.quantity_ordered)
                                        THEN (SELECT max(r.occurred_at) FROM receipts AS r
                                               WHERE r.order_number = o.purchase_order_number
                                                 AND r.stock_location_id = o.stock_location_id) END AS received_at
                              FROM public.purchase_order_lines AS l WHERE l.purchase_order_id = o.purchase_order_id) AS s
             WHERE (o.status::text, o.received_at) IS DISTINCT FROM (s.status, s.received_at))::text
    UNION ALL
    SELECT 304, 'receipts', 'orders without lines', '0',
           (SELECT count(*) FROM public.purchase_orders AS o
             WHERE NOT EXISTS (SELECT 1 FROM public.purchase_order_lines AS l WHERE l.purchase_order_id = o.purchase_order_id))::text
    UNION ALL
    SELECT 305, 'receipts', 'receipts dated before their order', '0',
           (SELECT count(*) FROM receipts AS r JOIN public.purchase_orders AS o ON o.purchase_order_number = r.order_number
             WHERE r.occurred_at < o.ordered_at)::text

    -- 4. Pilot purchase orders (exception E3) -----------------------------------------------------
    UNION ALL
    SELECT 401, 'pilot E3', 'pilot purchase orders recorded', (SELECT cardinality(pilot_orders) FROM params)::text,
           (SELECT count(*) FROM public.purchase_orders, params WHERE purchase_order_number = ANY (params.pilot_orders))::text
    UNION ALL
    SELECT 402, 'pilot E3', 'pilot order lines / units ordered',
           (SELECT expected_pilot_lines || ' / ' || expected_pilot_units FROM params),
           (SELECT count(*) || ' / ' || coalesce(sum(quantity_ordered), 0) FROM lines, params
             WHERE purchase_order_number = ANY (params.pilot_orders))
    UNION ALL
    SELECT 403, 'pilot E3', 'pilot orders completely received, received_at = their last delivery note',
           (SELECT cardinality(pilot_orders) FROM params)::text,
           (SELECT count(*) FROM public.purchase_orders AS o, params
             WHERE o.purchase_order_number = ANY (params.pilot_orders) AND o.status = 'received'
               AND o.received_at = (SELECT max(r.occurred_at) FROM receipts AS r WHERE r.order_number = o.purchase_order_number))::text
    UNION ALL
    SELECT 404, 'pilot E3', 'pilot lines with received = ordered and nothing outstanding',
           (SELECT expected_pilot_lines FROM params)::text,
           (SELECT count(*) FROM lines, params
             WHERE purchase_order_number = ANY (params.pilot_orders)
               AND quantity_received = quantity_ordered AND quantity_outstanding = 0 AND status = 'received')::text
    UNION ALL
    SELECT 405, 'pilot E3', 'pilot lines by order reason', 'forced_reorder 4',
           (SELECT string_agg(order_reason || ' ' || n, ', ' ORDER BY order_reason)
              FROM (SELECT order_reason::text, count(*) AS n FROM lines, params
                     WHERE purchase_order_number = ANY (params.pilot_orders) GROUP BY 1) x)
    UNION ALL
    SELECT 406, 'pilot E3', 'pilot receipt lines mapped to the pilot order lines',
           (SELECT expected_pilot_receipts FROM params)::text,
           (SELECT count(*) FROM receipts AS r JOIN lines AS l
                ON l.purchase_order_number = r.order_number AND l.stock_location_id = r.stock_location_id AND l.product_id = r.product_id,
                   params
             WHERE r.order_number = ANY (params.pilot_orders))::text

    -- 5. R0 history (decision C18) ------------------------------------------------------------------
    UNION ALL
    SELECT 501, 'R0 history', 'pilot_r0 revision rows', (SELECT expected_r0_rows FROM params)::text,
           (SELECT count(*) FROM public.reorder_point_revisions WHERE revision_kind = 'pilot_r0')::text
    UNION ALL
    SELECT 502, 'R0 history', 'pilot_r0 rows by document', (SELECT expected_r0_documents FROM params),
           (SELECT string_agg(document_reference || ' ' || n, ', ' ORDER BY document_reference)
              FROM (SELECT document_reference, count(*) AS n FROM public.reorder_point_revisions
                     WHERE revision_kind = 'pilot_r0' GROUP BY 1) x)
    UNION ALL
    SELECT 503, 'R0 history', 'pilot_r0 reorder points, sum of old -> sum of new', (SELECT expected_r0_totals FROM params),
           (SELECT sum(old_reorder_point) || ' -> ' || sum(new_reorder_point) FROM public.reorder_point_revisions
             WHERE revision_kind = 'pilot_r0')
    UNION ALL
    SELECT 504, 'R0 history', 'pilot_r0 rows not dated on 2024-11-15 (UTC) or not at a warehouse', '0',
           (SELECT count(*) FROM public.reorder_point_revisions AS r
              JOIN public.stock_locations AS l USING (stock_location_id), params AS p
             WHERE r.revision_kind = 'pilot_r0'
               AND (r.effective_at < p.r0_day_start OR r.effective_at >= p.r0_day_end OR l.location_type <> 'warehouse'))::text

    -- 6. Revision history integrity (global) --------------------------------------------------------
    UNION ALL
    SELECT 601, 'history', 'listings whose reorder point differs from their latest revision', '0',
           (SELECT count(*) FROM latest_revision AS r JOIN public.inventory_balances AS b USING (stock_location_id, product_id)
             WHERE b.reorder_point <> r.new_reorder_point)::text
    UNION ALL
    SELECT 602, 'history', 'revisions whose old value is not the previous revision''s new value', '0',
           (SELECT count(*) FROM chained WHERE previous_new IS NOT NULL AND old_reorder_point <> previous_new)::text

    -- 7. Run ledger (global) ---------------------------------------------------------------------------
    UNION ALL
    SELECT 701, 'run ledger', 'verified runs without their checkpoint (same run, hash and window end)', '0',
           (SELECT count(*) FROM meta.activity_runs AS r
             WHERE r.status = 'verified'
               AND NOT EXISTS (SELECT 1 FROM meta.activity_checkpoints AS c
                                WHERE c.activity_run_id = r.activity_run_id AND c.state_hash = r.end_state_hash
                                  AND c.state_as_of = r.window_end))::text
    UNION ALL
    SELECT 702, 'run ledger', 'checkpoints whose run is not verified with the same hash', '0',
           (SELECT count(*) FROM meta.activity_checkpoints AS c JOIN meta.activity_runs AS r USING (activity_run_id)
             WHERE r.status <> 'verified' OR r.end_state_hash <> c.state_hash)::text
    UNION ALL
    SELECT 703, 'run ledger', 'gaps or overlaps between consecutive windows, or a predecessor link that is not the previous window', '0',
           (SELECT count(*) FROM runs
             WHERE (previous_end IS NOT NULL AND window_start <> previous_end)
                OR previous_run_id IS DISTINCT FROM previous_id)::text
    UNION ALL
    SELECT 704, 'run ledger', 'runs that are not verified, other than the latest', '0',
           (SELECT count(*) FROM meta.activity_runs
             WHERE status <> 'verified' AND window_end < (SELECT max(window_end) FROM meta.activity_runs))::text

    -- 8. INFO ------------------------------------------------------------------------------------------
    UNION ALL
    SELECT 801, 'info', 'purchase orders by status', NULL,
           (SELECT coalesce(string_agg(status || ' ' || n, ', ' ORDER BY status), 'none')
              FROM (SELECT status::text, count(*) AS n FROM public.purchase_orders GROUP BY 1) x)
    UNION ALL
    SELECT 802, 'info', 'open purchase order lines / units outstanding', NULL,
           (SELECT count(*) || ' / ' || coalesce(sum(quantity_outstanding), 0) FROM public.purchase_order_lines
             WHERE status <> 'received')
    UNION ALL
    SELECT 803, 'info', 'reorder point revisions by kind', NULL,
           (SELECT coalesce(string_agg(revision_kind || ' ' || n, ', ' ORDER BY revision_kind), 'none')
              FROM (SELECT revision_kind::text, count(*) AS n FROM public.reorder_point_revisions GROUP BY 1) x)
    UNION ALL
    SELECT 804, 'info', 'activity runs by status / last window end (UTC)', NULL,
           (SELECT coalesce(string_agg(status || ' ' || n, ', ' ORDER BY status), 'none') || ' / '
                   || coalesce((SELECT to_char(max(window_end) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') FROM meta.activity_runs), 'none')
              FROM (SELECT status::text, count(*) AS n FROM meta.activity_runs GROUP BY 1) x)
    UNION ALL
    SELECT 805, 'info', 'purchase receipts with a non-PO reference (allowed by V007; counted in 301)', NULL,
           (SELECT count(*) FROM receipts WHERE order_number IS NULL)::text
    UNION ALL
    SELECT 806, 'info', 'change_log rows of the V009 business tables', NULL,
           (SELECT count(*) FROM public.change_log
             WHERE table_name IN ('purchase_orders', 'purchase_order_lines', 'reorder_point_revisions'))::text
)
SELECT check_no,
       check_type,
       object_name,
       expected,
       actual,
       CASE WHEN expected IS NULL THEN 'INFO'
            WHEN expected = actual THEN 'PASS'
            ELSE 'FAIL' END AS status,
       count(*) FILTER (WHERE expected IS NOT NULL AND expected IS DISTINCT FROM actual) OVER () AS failed_checks
  FROM checks
 ORDER BY check_no;
