-- =============================================================================
-- NordicCart OLTP | Verification 17: V008 security (roles, ownership, privileges)
-- -----------------------------------------------------------------------------
-- Scope       : the catalog state V008 creates: roles and their attributes,
--               memberships and settings; ownership; SECURITY DEFINER functions;
--               the privilege matrix (docs/phase3_v008_security_privilege_matrix.md).
-- Connect to  : nordiccart_oltp, as any role that can connect (nordiccart_analyst
--               or postgres). Reads only pg_catalog and has_*_privilege().
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS or INFO, and failed_checks = 0
--               (31 rows: 27 PASS, 4 INFO). Before V008 (or after its rollback)
--               most checks FAIL: that is expected. The script also runs when
--               the NordicCart roles do not exist: their checks FAIL, no error.
-- When        : after V008, and in every monthly verification (security drift).
-- Numbering   : 16 is reserved for the exchange rates (design, stage S5).
-- Revision 2 (2026-11-18, stage S4): checks 202, 404, 405, 407 and 408 compare V008's exact
--               privilege lists and are now limited to the 19 tables and 24 functions of
--               V001 to V008 (params), so the script stays valid after V009 adds tables and
--               functions (those are checked by script 18). Results on a V008 database are
--               unchanged. The global checks (ownership, search_path of every SECURITY
--               DEFINER function, PUBLIC, DELETE/TRUNCATE/TRIGGER/REFERENCES, CREATE) still
--               cover every object.
-- =============================================================================

WITH params AS (
    SELECT ARRAY['nordiccart_owner', 'nordiccart_writer', 'nordiccart_reader', 'nordiccart_loader',
                 'nordiccart_scheduler', 'nordiccart_analyst', 'nordiccart_extractor']::text[] AS roles,
           ARRAY['nordiccart_writer', 'nordiccart_loader', 'nordiccart_scheduler', 'nordiccart_reader',
                 'nordiccart_analyst', 'nordiccart_extractor', 'public']::text[] AS app_roles,
           'search_path=pg_catalog, public, pg_temp'::text AS definer_path,
           -- V001 to V008 objects (revision 2 scope of the V008 lists)
           ARRAY['change_log', 'change_log_checkpoints', 'schema_migrations', 'currencies', 'countries', 'exchange_rates',
                 'product_categories', 'products', 'product_prices', 'customers', 'stock_locations', 'stores',
                 'warehouses', 'orders', 'order_items', 'payment_attempts', 'inventory_balances',
                 'inventory_reservations', 'inventory_movements']::text[] AS v008_tables,
           ARRAY['assert_migration_can_run', 'fn_set_updated_at', 'fn_reject_modification', 'fn_capture_change',
                 'fn_check_stock_location_subtype', 'fn_orders_guard', 'fn_order_items_guard',
                 'fn_payment_attempts_guard', 'fn_reject_history_truncate', 'fn_assert_order_consistent',
                 'fn_check_order_consistency', 'fn_inventory_apply_movement', 'fn_inventory_apply_reservation',
                 'fn_inventory_balances_guard', 'fn_inventory_reservations_guard', 'fn_inventory_movements_guard',
                 'fn_payment_attempts_inventory_guard', 'fn_reject_inventory_truncate',
                 'fn_assert_order_inventory_consistent', 'fn_check_order_inventory', 'fn_lock_order_balances',
                 'fn_reserve_order_stock', 'fn_close_order_reservations', 'fn_fulfil_order_stock']::text[] AS v008_functions
),
existing AS (          -- the script also runs where the roles do not exist (before V008, after R008): FAIL, never an error
    SELECT (SELECT oid FROM pg_roles WHERE rolname = 'nordiccart_owner') AS owner_oid,
           EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nordiccart_writer') AS writer,
           EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nordiccart_reader') AS reader,
           EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nordiccart_extractor') AS extractor
),
app AS (               -- application roles that exist, and PUBLIC
    SELECT a.r FROM params, unnest(params.app_roles) AS a(r)
     WHERE a.r = 'public' OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname = a.r)
),
ours_class AS (        -- tables, views and sequences of public and meta, not of an extension
    SELECT c.oid, c.relname, c.relkind, n.nspname, c.relowner
      FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
     WHERE n.nspname IN ('public', 'meta') AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
       AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
),
ours_proc AS (
    SELECT p.oid, p.oid::regprocedure::text AS signature, p.proname, p.prosecdef, p.proconfig, p.proowner
      FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
     WHERE n.nspname IN ('public', 'meta')
       AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
),
ours_type AS (
    SELECT t.oid, t.typowner FROM pg_type AS t JOIN pg_namespace AS n ON n.oid = t.typnamespace
     WHERE n.nspname IN ('public', 'meta') AND t.typtype IN ('e', 'd')
       AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e')
),
tables AS (SELECT * FROM ours_class WHERE relkind = 'r'),
v008_tables AS (SELECT t.* FROM tables AS t, params WHERE t.relname = ANY (params.v008_tables)),
v008_proc AS (SELECT p.* FROM ours_proc AS p, params WHERE p.proname = ANY (params.v008_functions)),
cols AS (
    SELECT t.oid, t.relname, a.attname
      FROM v008_tables AS t JOIN pg_attribute AS a ON a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped
),
checks (check_no, check_type, object_name, expected, actual) AS (
    -- 1. Roles ----------------------------------------------------------------------------
    SELECT 101, 'roles', 'NordicCart roles and LOGIN',
           'nordiccart_analyst LOGIN, nordiccart_extractor LOGIN, nordiccart_loader LOGIN, nordiccart_owner NOLOGIN, '
           || 'nordiccart_reader NOLOGIN, nordiccart_scheduler LOGIN, nordiccart_writer NOLOGIN',
           (SELECT string_agg(rolname || CASE WHEN rolcanlogin THEN ' LOGIN' ELSE ' NOLOGIN' END, ', ' ORDER BY rolname)
              FROM pg_roles, params WHERE rolname = ANY (params.roles))
    UNION ALL
    SELECT 102, 'roles', 'NordicCart roles with SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS', '0',
           (SELECT count(*) FROM pg_roles, params WHERE rolname = ANY (params.roles)
               AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))::text
    UNION ALL
    SELECT 103, 'roles', 'Memberships of the NordicCart roles (inherit, no SET)',
           'nordiccart_analyst in nordiccart_reader, nordiccart_loader in nordiccart_writer, nordiccart_scheduler in nordiccart_writer',
           (SELECT string_agg(r.rolname || ' in ' || g.rolname || CASE WHEN m.inherit_option AND NOT m.set_option THEN '' ELSE ' (options!)' END,
                              ', ' ORDER BY r.rolname)
              FROM pg_auth_members AS m JOIN pg_roles AS r ON r.oid = m.member JOIN pg_roles AS g ON g.oid = m.roleid, params
             WHERE r.rolname = ANY (params.roles))
    UNION ALL
    SELECT 104, 'roles', 'Members of nordiccart_owner (nobody may log in as or inherit the owner)', '0',
           (SELECT count(*) FROM pg_auth_members AS m JOIN pg_roles AS g ON g.oid = m.roleid WHERE g.rolname = 'nordiccart_owner')::text
    UNION ALL
    SELECT 105, 'roles', 'Connection limits (owner, writer, reader, loader, scheduler, analyst, extractor)', '0, 0, 0, 30, 3, 10, 3',
           (SELECT string_agg(rolconnlimit::text, ', ' ORDER BY array_position(params.roles, rolname::text))
              FROM pg_roles, params WHERE rolname = ANY (params.roles))
    UNION ALL
    SELECT 106, 'roles', 'Settings of nordiccart_loader',
           'idle_in_transaction_session_timeout=60s, lock_timeout=10s, statement_timeout=30min',
           (SELECT string_agg(s, ', ' ORDER BY s) FROM pg_roles, unnest(rolconfig) AS s WHERE rolname = 'nordiccart_loader')
    UNION ALL
    SELECT 107, 'roles', 'Settings of nordiccart_scheduler',
           'idle_in_transaction_session_timeout=60s, lock_timeout=10s, statement_timeout=5min',
           (SELECT string_agg(s, ', ' ORDER BY s) FROM pg_roles, unnest(rolconfig) AS s WHERE rolname = 'nordiccart_scheduler')
    UNION ALL
    SELECT 108, 'roles', 'Settings of nordiccart_analyst',
           'default_transaction_read_only=on, idle_in_transaction_session_timeout=5min, statement_timeout=15min',
           (SELECT string_agg(s, ', ' ORDER BY s) FROM pg_roles, unnest(rolconfig) AS s WHERE rolname = 'nordiccart_analyst')
    UNION ALL
    SELECT 109, 'roles', 'Settings of nordiccart_extractor',
           'idle_in_transaction_session_timeout=60s, lock_timeout=10s, statement_timeout=30min',
           (SELECT string_agg(s, ', ' ORDER BY s) FROM pg_roles, unnest(rolconfig) AS s WHERE rolname = 'nordiccart_extractor')

    -- 2. Ownership and SECURITY DEFINER ------------------------------------------------------
    UNION ALL
    SELECT 201, 'ownership', 'Tables, sequences, types and functions of public and meta not owned by nordiccart_owner', '0',
           ((SELECT count(*) FROM ours_class, existing WHERE relowner IS DISTINCT FROM existing.owner_oid)
            + (SELECT count(*) FROM ours_proc, existing WHERE proowner IS DISTINCT FROM existing.owner_oid)
            + (SELECT count(*) FROM ours_type, existing WHERE typowner IS DISTINCT FROM existing.owner_oid)
            + (SELECT count(*) FROM pg_namespace, existing WHERE nspname = 'meta' AND nspowner IS DISTINCT FROM existing.owner_oid))::text
    UNION ALL
    SELECT 202, 'functions', 'SECURITY DEFINER functions (of V001 to V008)',
           'fn_capture_change(), fn_inventory_apply_movement(), fn_inventory_apply_reservation()',
           (SELECT string_agg(signature, ', ' ORDER BY signature) FROM v008_proc WHERE prosecdef)
    UNION ALL
    SELECT 203, 'functions', 'SECURITY DEFINER functions without search_path = pg_catalog, public, pg_temp', '0',
           (SELECT count(*) FROM ours_proc, params WHERE prosecdef AND proconfig IS DISTINCT FROM ARRAY[params.definer_path])::text

    -- 3. PUBLIC and object creation ------------------------------------------------------------
    UNION ALL
    SELECT 301, 'public', 'PUBLIC privileges: CONNECT, TEMP, CREATE on the database; USAGE, CREATE on public and meta', '0',
           ((has_database_privilege('public', current_database(), 'CONNECT'))::int
            + (has_database_privilege('public', current_database(), 'TEMPORARY'))::int
            + (has_database_privilege('public', current_database(), 'CREATE'))::int
            + (has_schema_privilege('public', 'public', 'USAGE'))::int + (has_schema_privilege('public', 'public', 'CREATE'))::int
            + (has_schema_privilege('public', 'meta', 'USAGE'))::int + (has_schema_privilege('public', 'meta', 'CREATE'))::int)::text
    UNION ALL
    SELECT 302, 'public', 'NordicCart functions PUBLIC may execute', '0',
           (SELECT count(*) FROM ours_proc WHERE has_function_privilege('public', oid, 'EXECUTE'))::text
    UNION ALL
    SELECT 303, 'public', 'Tables and sequences PUBLIC holds any privilege on', '0',
           (SELECT count(*) FROM ours_class
             WHERE (relkind = 'S' AND (has_sequence_privilege('public', oid, 'USAGE') OR has_sequence_privilege('public', oid, 'SELECT')))
                OR (relkind <> 'S' AND has_any_column_privilege('public', oid, 'SELECT,INSERT,UPDATE,REFERENCES')))::text
    UNION ALL
    SELECT 304, 'privileges', 'Application roles that may create objects or temporary tables', '0',
           (SELECT count(*) FROM app AS a
             WHERE has_schema_privilege(a.r, 'public', 'CREATE') OR has_schema_privilege(a.r, 'meta', 'CREATE')
                OR has_database_privilege(a.r, current_database(), 'CREATE')
                OR has_database_privilege(a.r, current_database(), 'TEMPORARY'))::text

    -- 4. The privilege matrix ------------------------------------------------------------------
    UNION ALL
    SELECT 401, 'privileges', 'DELETE, TRUNCATE, TRIGGER or REFERENCES held by an application role (except the extractor''s change_log purge)', '0',
           (SELECT count(*) FROM tables, app AS a,
                   unnest(ARRAY['DELETE', 'TRUNCATE', 'TRIGGER', 'REFERENCES']) AS p(priv)
             WHERE has_table_privilege(a.r, tables.oid, p.priv)
               AND NOT (a.r = 'nordiccart_extractor' AND p.priv = 'DELETE' AND tables.relname = 'change_log'))::text
    UNION ALL
    SELECT 402, 'privileges', 'UPDATE of inventory balance quantities or identity by an application role', '0',
           (SELECT count(*) FROM app AS a,
                   unnest(ARRAY['quantity_on_hand', 'quantity_reserved', 'stock_location_id', 'product_id', 'inventory_balance_id']) AS q(c)
             WHERE has_column_privilege(a.r, 'public.inventory_balances', q.c, 'UPDATE'))::text
    UNION ALL
    SELECT 403, 'privileges', 'UPDATE on insert-only history (order_items, movements, exchange_rates, change_log, migrations)', '0',
           (SELECT count(*) FROM app AS a,
                   unnest(ARRAY['public.order_items', 'public.inventory_movements', 'public.exchange_rates', 'public.change_log',
                                'meta.schema_migrations']) AS t(tbl)
             WHERE has_any_column_privilege(a.r, t.tbl, 'UPDATE'))::text
    UNION ALL
    SELECT 404, 'privileges', 'Columns the writer may UPDATE',
           'customers(address_line1, city, email, is_active, phone, postal_code); inventory_balances(reorder_point); '
           || 'inventory_reservations(closed_at, status); orders(cancellation_reason, cancelled_at, confirmed_at, fulfilled_at, status); '
           || 'payment_attempts(authorized_at, captured_at, failed_at, failure_reason, provider_reference, status); '
           || 'product_prices(valid_to); products(discontinued_on, is_active)',
           (SELECT string_agg(relname || '(' || c || ')', '; ' ORDER BY relname)
              FROM (SELECT relname, string_agg(attname, ', ' ORDER BY attname) AS c
                      FROM cols, existing
                     WHERE CASE WHEN existing.writer THEN has_column_privilege('nordiccart_writer', oid, attname, 'UPDATE') ELSE false END
                     GROUP BY relname) x)
    UNION ALL
    SELECT 405, 'privileges', 'Tables the writer may INSERT into (of V001 to V008)', '16',
           (SELECT count(*) FROM v008_tables, existing
             WHERE CASE WHEN existing.writer THEN has_table_privilege('nordiccart_writer', oid, 'INSERT') ELSE false END)::text
    UNION ALL
    SELECT 406, 'privileges', 'Writer on change_log: INSERT, or SELECT of row_data / record_key, or anything on change_log_checkpoints',
           '0',
           CASE WHEN (SELECT writer FROM existing) THEN
               ((has_table_privilege('nordiccart_writer', 'public.change_log', 'INSERT'))::int
                + (has_column_privilege('nordiccart_writer', 'public.change_log', 'row_data', 'SELECT'))::int
                + (has_column_privilege('nordiccart_writer', 'public.change_log', 'record_key', 'SELECT'))::int
                + (has_any_column_privilege('nordiccart_writer', 'public.change_log_checkpoints', 'SELECT,INSERT,UPDATE'))::int)::text
           ELSE 'role nordiccart_writer missing' END
    UNION ALL
    SELECT 407, 'privileges', 'Functions the writer may execute',
           'fn_assert_order_consistent(bigint), fn_assert_order_inventory_consistent(bigint), '
           || 'fn_close_order_reservations(bigint,reservation_status,timestamp with time zone), fn_fulfil_order_stock(bigint,timestamp with time zone), '
           || 'fn_lock_order_balances(bigint), fn_reserve_order_stock(bigint,timestamp with time zone,timestamp with time zone)',
           (SELECT string_agg(signature, ', ' ORDER BY signature) FROM v008_proc, existing
             WHERE CASE WHEN existing.writer THEN has_function_privilege('nordiccart_writer', oid, 'EXECUTE') ELSE false END)
    UNION ALL
    SELECT 408, 'privileges', 'Tables the reader may SELECT (all 19 of V001 to V008)', '19',
           (SELECT count(*) FROM v008_tables, existing
             WHERE CASE WHEN existing.reader THEN has_table_privilege('nordiccart_reader', oid, 'SELECT') ELSE false END)::text
    UNION ALL
    SELECT 409, 'privileges', 'Reader privileges other than SELECT, and functions it may execute',
           '0',
           CASE WHEN (SELECT reader FROM existing) THEN
               ((SELECT count(*) FROM tables WHERE has_any_column_privilege('nordiccart_reader', oid, 'INSERT,UPDATE,REFERENCES'))
                + (SELECT count(*) FROM ours_proc WHERE has_function_privilege('nordiccart_reader', oid, 'EXECUTE')))::text
           ELSE 'role nordiccart_reader missing' END
    UNION ALL
    SELECT 410, 'privileges', 'Extractor privileges',
           'change_log: DELETE, SELECT; change_log_checkpoints: INSERT, SELECT, UPDATE(last_snapshot)',
           (SELECT string_agg(relname || ': ' || privs, '; ' ORDER BY relname)
              FROM (SELECT relname,
                           concat_ws(', ',
                               CASE WHEN has_table_privilege('nordiccart_extractor', oid, 'DELETE') THEN 'DELETE' END,
                               CASE WHEN has_table_privilege('nordiccart_extractor', oid, 'INSERT') THEN 'INSERT' END,
                               CASE WHEN has_table_privilege('nordiccart_extractor', oid, 'SELECT') THEN 'SELECT' END,
                               CASE WHEN has_table_privilege('nordiccart_extractor', oid, 'UPDATE') THEN 'UPDATE'
                                    WHEN has_any_column_privilege('nordiccart_extractor', oid, 'UPDATE') THEN 'UPDATE(last_snapshot)' END) AS privs
                      FROM tables, existing
                     WHERE CASE WHEN existing.extractor
                                THEN has_any_column_privilege('nordiccart_extractor', oid, 'SELECT,INSERT,UPDATE')
                                     OR has_table_privilege('nordiccart_extractor', oid, 'DELETE')
                                ELSE false END) x)
    UNION ALL
    SELECT 411, 'privileges', 'Login roles that may connect to this database', 'nordiccart_analyst, nordiccart_extractor, nordiccart_loader, nordiccart_scheduler',
           (SELECT string_agg(rolname, ', ' ORDER BY rolname) FROM pg_roles, params
             WHERE rolname = ANY (params.roles) AND rolcanlogin AND has_database_privilege(rolname, current_database(), 'CONNECT'))

    -- 5. INFO ------------------------------------------------------------------------------------
    UNION ALL
    SELECT 501, 'info', 'Migrations applied', NULL,
           (SELECT string_agg(version, ', ' ORDER BY version) FROM meta.schema_migrations)
    UNION ALL
    SELECT 502, 'info', 'Every member of the writer and reader groups', NULL,
           (SELECT coalesce(string_agg(r.rolname || ' in ' || g.rolname, ', ' ORDER BY g.rolname, r.rolname), 'none')
              FROM pg_auth_members AS m JOIN pg_roles AS r ON r.oid = m.member JOIN pg_roles AS g ON g.oid = m.roleid
             WHERE g.rolname IN ('nordiccart_writer', 'nordiccart_reader'))
    UNION ALL
    SELECT 503, 'info', 'Default privileges for objects nordiccart_owner creates', NULL,
           (SELECT coalesce(string_agg(coalesce(n.nspname::text, '(all schemas)') || ' ' || d.defaclobjtype::text || ' ' || d.defaclacl::text, '; '
                                       ORDER BY n.nspname, d.defaclobjtype), 'none')
              FROM pg_default_acl AS d LEFT JOIN pg_namespace AS n ON n.oid = d.defaclnamespace
             WHERE d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = 'nordiccart_owner'))
    UNION ALL
    SELECT 504, 'info', 'Objects of public and meta (tables / sequences / functions / types)', NULL,
           (SELECT count(*) FILTER (WHERE relkind = 'r') || ' / ' || count(*) FILTER (WHERE relkind = 'S') FROM ours_class)
           || ' / ' || (SELECT count(*) FROM ours_proc) || ' / ' || (SELECT count(*) FROM ours_type)
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
