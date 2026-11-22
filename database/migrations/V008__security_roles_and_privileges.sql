-- =============================================================================
-- NordicCart OLTP | Migration V008
-- Security: roles, object ownership, least privilege, SECURITY DEFINER, timeouts
-- -----------------------------------------------------------------------------
-- Design source : docs/phase3_combined_activity_design.md, revision 2, section 4.1
--                 (limitation L3, stage S3); privilege matrix:
--                 docs/phase3_v008_security_privilege_matrix.md
-- Depends on    : V007
-- Connect to    : nordiccart_oltp, as a SUPERUSER (for example "postgres"): V008
--                 creates cluster roles and moves object ownership.
-- Transaction   : the whole file is one transaction (see V001 header). The last
--                 block re-checks every rule below; any failure rolls back all.
-- Re-runnable   : no. A second run stops at the guard below.
-- Requires      : PostgreSQL 16 or later (role membership options INHERIT / SET).
-- Rollback      : database/rollback/R008__undo_security_roles_and_privileges.sql
-- -----------------------------------------------------------------------------
-- What it does
--   1. Roles (cluster-wide; created only if missing, attributes re-asserted):
--        nordiccart_owner      NOLOGIN  owns every object of public and meta
--        nordiccart_writer     NOLOGIN  group: what the loaders may write
--        nordiccart_reader     NOLOGIN  group: read everything
--        nordiccart_loader     LOGIN    member of writer (manual, approved loads)
--        nordiccart_scheduler  LOGIN    member of writer (hourly ticks, stage S10)
--        nordiccart_analyst    LOGIN    member of reader (scripts, Power BI)
--        nordiccart_extractor  LOGIN    change_log extraction and purge (Phase 4)
--      No role is superuser, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS.
--      LOGIN roles are created WITHOUT a password: an administrator sets one
--      (psql \password) before first use. No secret is stored in this file.
--   2. Ownership of every table (with its identity sequence), enum type and
--      function of schemas public and meta, and of schema meta itself, moves
--      from the migration user to nordiccart_owner. Objects of the btree_gist
--      extension stay with their owner. Owners bypass privileges, so no
--      application role owns anything.
--   3. fn_inventory_apply_movement, fn_inventory_apply_reservation and
--      fn_capture_change become SECURITY DEFINER (they run as nordiccart_owner)
--      with SET search_path = pg_catalog, public, pg_temp (pg_temp last, so
--      temporary objects can never shadow what they use). Writers change stock
--      quantities and change_log only through these triggers.
--   4. Privileges (everything else is denied):
--        PUBLIC      nothing: no CONNECT or TEMP on the database, no USAGE or
--                    CREATE on public or meta, no EXECUTE on NordicCart functions
--        writer      CONNECT; USAGE on public and meta; SELECT on every business
--                    table and meta.schema_migrations; INSERT on the 16 business
--                    tables; UPDATE only on lifecycle and status columns (list
--                    below); SELECT on the metadata columns of change_log (no
--                    row_data: the loaders count the rows they write);
--                    EXECUTE on the unit-of-work functions and the functions
--                    they and the commit-time checks call. No UPDATE of
--                    quantities, of order lines, movements or history, no DELETE,
--                    TRUNCATE, TRIGGER, REFERENCES or CREATE anywhere.
--        reader      CONNECT; USAGE on public and meta; SELECT on every table,
--                    change_log included (verification scripts)
--        extractor   CONNECT; USAGE on public; SELECT and DELETE on change_log
--                    (purge, V002 note); SELECT, INSERT and UPDATE(last_snapshot)
--                    on change_log_checkpoints
--        owner       ownership; USAGE and CREATE on public (later migrations run
--                    as postgres with SET ROLE nordiccart_owner)
--      Default privileges for objects nordiccart_owner creates later: no
--      EXECUTE for PUBLIC on functions; SELECT for reader on tables. Writer
--      privileges on later tables are granted by their migration (V009).
--   5. Per-role settings (lock_timeout, statement_timeout,
--      idle_in_transaction_session_timeout, connection limits, analysts read-only
--      by default). Earlier settings of the seven roles are reset first, so the
--      result is exactly this list; the self-check verifies it. A role can still
--      change its own session settings: timeouts protect against mistakes, they
--      are not a security control.
-- Unchanged: every table, column, constraint, index, trigger and function body
-- of V001 to V007; every row (V008 writes only its meta.schema_migrations row).
-- =============================================================================

BEGIN;

SET LOCAL search_path TO public;
SET LOCAL lock_timeout = '10s';

SELECT meta.assert_migration_can_run('008', '007');

DO $guard$
BEGIN
    IF NOT (SELECT r.rolsuper FROM pg_roles AS r WHERE r.rolname = current_user) THEN
        RAISE EXCEPTION 'V008 creates cluster roles and moves object ownership: run it as a superuser (for example postgres), not as %.', current_user;
    END IF;
    IF current_setting('server_version_num')::integer < 160000 THEN
        RAISE EXCEPTION 'V008 needs PostgreSQL 16 or later (role membership options INHERIT and SET); this server runs %.',
            current_setting('server_version');
    END IF;
END
$guard$;

-- -----------------------------------------------------------------------------
-- 1. Roles
-- -----------------------------------------------------------------------------
DO $roles$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('nordiccart_owner',     false,  0),
            ('nordiccart_writer',    false,  0),
            ('nordiccart_reader',    false,  0),
            ('nordiccart_loader',    true,  30),
            ('nordiccart_scheduler', true,   3),
            ('nordiccart_analyst',   true,  10),
            ('nordiccart_extractor', true,   3)) AS t(role_name, can_login, connection_limit)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r.role_name) THEN
            EXECUTE format('CREATE ROLE %I', r.role_name);
        END IF;
        EXECUTE format('ALTER ROLE %I WITH %s INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT %s',
                       r.role_name, CASE WHEN r.can_login THEN 'LOGIN' ELSE 'NOLOGIN' END, r.connection_limit);
        -- Settings are exactly the ones below, whatever an earlier run or an administrator left
        EXECUTE format('ALTER ROLE %I RESET ALL', r.role_name);
    END LOOP;
END
$roles$;

COMMENT ON ROLE nordiccart_owner IS 'NordicCart: owns every object of schemas public and meta and the SECURITY DEFINER functions (V008). NOLOGIN.';
COMMENT ON ROLE nordiccart_writer IS 'NordicCart: privileges of the loaders (V008). NOLOGIN group.';
COMMENT ON ROLE nordiccart_reader IS 'NordicCart: read-only access to every table (V008). NOLOGIN group.';
COMMENT ON ROLE nordiccart_loader IS 'NordicCart: manual, approved loads (pilot loaders, scale-up, historical catch-up). Member of nordiccart_writer.';
COMMENT ON ROLE nordiccart_scheduler IS 'NordicCart: automated hourly ticks (stage S10). Member of nordiccart_writer.';
COMMENT ON ROLE nordiccart_analyst IS 'NordicCart: verification scripts, Power BI, ad-hoc analysis. Member of nordiccart_reader; read-only by default.';
COMMENT ON ROLE nordiccart_extractor IS 'NordicCart: change_log extraction with snapshot checkpoints and purge (Phase 4).';

-- Memberships: privileges are inherited; SET FALSE keeps current_user = the login role.
GRANT nordiccart_writer TO nordiccart_loader    WITH INHERIT TRUE, SET FALSE;
GRANT nordiccart_writer TO nordiccart_scheduler WITH INHERIT TRUE, SET FALSE;
GRANT nordiccart_reader TO nordiccart_analyst   WITH INHERIT TRUE, SET FALSE;

-- Per-role settings (applied when the role logs in)
ALTER ROLE nordiccart_loader    SET lock_timeout = '10s';
ALTER ROLE nordiccart_loader    SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE nordiccart_loader    SET statement_timeout = '30min';
ALTER ROLE nordiccart_scheduler SET lock_timeout = '10s';
ALTER ROLE nordiccart_scheduler SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE nordiccart_scheduler SET statement_timeout = '5min';
ALTER ROLE nordiccart_analyst   SET statement_timeout = '15min';
ALTER ROLE nordiccart_analyst   SET idle_in_transaction_session_timeout = '5min';
ALTER ROLE nordiccart_analyst   SET default_transaction_read_only = on;
ALTER ROLE nordiccart_extractor SET lock_timeout = '10s';
ALTER ROLE nordiccart_extractor SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE nordiccart_extractor SET statement_timeout = '30min';

-- -----------------------------------------------------------------------------
-- 2. Ownership: every NordicCart object of public and meta to nordiccart_owner
-- -----------------------------------------------------------------------------
ALTER SCHEMA meta OWNER TO nordiccart_owner;

DO $ownership$
DECLARE
    r record;
BEGIN
    -- Tables; their identity sequences and indexes follow the table.
    FOR r IN
        SELECT c.oid::regclass AS obj
          FROM pg_class AS c
          JOIN pg_namespace AS n ON n.oid = c.relnamespace
         WHERE n.nspname IN ('public', 'meta')
           AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND NOT EXISTS (SELECT 1 FROM pg_depend AS d
                            WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
         ORDER BY 1::text
    LOOP
        EXECUTE format('ALTER TABLE %s OWNER TO nordiccart_owner', r.obj);
    END LOOP;

    -- Sequences not owned by a table column (none in V001 to V007; kept for completeness)
    FOR r IN
        SELECT c.oid::regclass AS obj
          FROM pg_class AS c
          JOIN pg_namespace AS n ON n.oid = c.relnamespace
         WHERE n.nspname IN ('public', 'meta') AND c.relkind = 'S'
           AND NOT EXISTS (SELECT 1 FROM pg_depend AS d
                            WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype IN ('a', 'i', 'e'))
    LOOP
        EXECUTE format('ALTER SEQUENCE %s OWNER TO nordiccart_owner', r.obj);
    END LOOP;

    -- Enum and domain types (array types follow their element type)
    FOR r IN
        SELECT t.oid::regtype AS obj
          FROM pg_type AS t
          JOIN pg_namespace AS n ON n.oid = t.typnamespace
         WHERE n.nspname IN ('public', 'meta') AND t.typtype IN ('e', 'd')
           AND NOT EXISTS (SELECT 1 FROM pg_depend AS d
                            WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e')
         ORDER BY 1::text
    LOOP
        EXECUTE format('ALTER TYPE %s OWNER TO nordiccart_owner', r.obj);
    END LOOP;

    -- Functions
    FOR r IN
        SELECT p.oid::regprocedure AS obj
          FROM pg_proc AS p
          JOIN pg_namespace AS n ON n.oid = p.pronamespace
         WHERE n.nspname IN ('public', 'meta')
           AND NOT EXISTS (SELECT 1 FROM pg_depend AS d
                            WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
         ORDER BY 1::text
    LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO nordiccart_owner', r.obj);
    END LOOP;
END
$ownership$;

-- -----------------------------------------------------------------------------
-- 3. SECURITY DEFINER for balance maintenance and change capture
-- -----------------------------------------------------------------------------
ALTER FUNCTION public.fn_inventory_apply_movement()    SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.fn_inventory_apply_reservation() SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.fn_capture_change()              SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp;

COMMENT ON FUNCTION public.fn_inventory_apply_movement() IS
    'AFTER INSERT on inventory_movements: adds quantity_change to inventory_balances.quantity_on_hand. V008: SECURITY DEFINER (nordiccart_owner), fixed search_path; writers have no UPDATE privilege on the quantity columns.';
COMMENT ON FUNCTION public.fn_inventory_apply_reservation() IS
    'AFTER INSERT OR UPDATE OF status on inventory_reservations: keeps inventory_balances.quantity_reserved = SUM(quantity) of active reservations. V008: SECURITY DEFINER (nordiccart_owner), fixed search_path.';
COMMENT ON FUNCTION public.fn_capture_change() IS
    'AFTER INSERT OR UPDATE OR DELETE row trigger. Argument: primary key column name. Writes the row image to change_log in the same transaction. V008: SECURITY DEFINER (nordiccart_owner), fixed search_path; no application role can write change_log directly.';

-- -----------------------------------------------------------------------------
-- 4. Privileges
-- -----------------------------------------------------------------------------
-- 4a. PUBLIC keeps nothing
DO $public$
DECLARE
    r record;
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    FOR r IN
        SELECT p.oid::regprocedure AS obj
          FROM pg_proc AS p
          JOIN pg_namespace AS n ON n.oid = p.pronamespace
         WHERE n.nspname IN ('public', 'meta')
           AND NOT EXISTS (SELECT 1 FROM pg_depend AS d
                            WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', r.obj);
    END LOOP;
END
$public$;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA meta FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public, meta FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public, meta FROM PUBLIC;

-- 4b. Database and schemas
DO $database$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO nordiccart_writer, nordiccart_reader, nordiccart_extractor',
                   current_database());
END
$database$;

GRANT USAGE, CREATE ON SCHEMA public TO nordiccart_owner;
GRANT USAGE ON SCHEMA public TO nordiccart_writer, nordiccart_reader, nordiccart_extractor;
GRANT USAGE ON SCHEMA meta TO nordiccart_writer, nordiccart_reader;

-- 4c. Writer: read and insert business rows
GRANT SELECT, INSERT ON
      public.currencies, public.countries, public.exchange_rates,
      public.product_categories, public.products, public.product_prices, public.customers,
      public.stock_locations, public.stores, public.warehouses,
      public.orders, public.order_items, public.payment_attempts,
      public.inventory_balances, public.inventory_reservations, public.inventory_movements
   TO nordiccart_writer;
GRANT SELECT ON meta.schema_migrations TO nordiccart_writer;
GRANT SELECT (change_id, table_name, operation, transaction_id, changed_at) ON public.change_log TO nordiccart_writer;

-- 4d. Writer: UPDATE only on lifecycle and status columns (V006 and V007 guards still
--     decide which transitions are allowed; updated_at is set by its trigger)
GRANT UPDATE (status, cancellation_reason, confirmed_at, fulfilled_at, cancelled_at)
   ON public.orders TO nordiccart_writer;
GRANT UPDATE (status, authorized_at, captured_at, failed_at, failure_reason, provider_reference)
   ON public.payment_attempts TO nordiccart_writer;
GRANT UPDATE (status, closed_at) ON public.inventory_reservations TO nordiccart_writer;
GRANT UPDATE (reorder_point) ON public.inventory_balances TO nordiccart_writer;
GRANT UPDATE (valid_to) ON public.product_prices TO nordiccart_writer;
GRANT UPDATE (discontinued_on, is_active) ON public.products TO nordiccart_writer;
GRANT UPDATE (email, phone, address_line1, postal_code, city, is_active) ON public.customers TO nordiccart_writer;

-- 4e. Writer: the units of work (U2, U3/U4 close, U5) and the functions they and the
--     commit-time order checks call with the caller's rights
GRANT EXECUTE ON FUNCTION
      public.fn_reserve_order_stock(bigint, timestamptz, timestamptz),
      public.fn_close_order_reservations(bigint, public.reservation_status, timestamptz),
      public.fn_fulfil_order_stock(bigint, timestamptz),
      public.fn_lock_order_balances(bigint),
      public.fn_assert_order_consistent(bigint),
      public.fn_assert_order_inventory_consistent(bigint)
   TO nordiccart_writer;

-- 4f. Reader: everything readable, nothing else
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nordiccart_reader;
GRANT SELECT ON meta.schema_migrations TO nordiccart_reader;

-- 4g. Extractor: change_log only
GRANT SELECT, DELETE ON public.change_log TO nordiccart_extractor;
GRANT SELECT, INSERT ON public.change_log_checkpoints TO nordiccart_extractor;
GRANT UPDATE (last_snapshot) ON public.change_log_checkpoints TO nordiccart_extractor;

-- 4h. Defaults for objects nordiccart_owner creates in later migrations
ALTER DEFAULT PRIVILEGES FOR ROLE nordiccart_owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE nordiccart_owner IN SCHEMA public, meta GRANT SELECT ON TABLES TO nordiccart_reader;

-- -----------------------------------------------------------------------------
-- 5. Self-check: every rule above, re-read from the catalog. Any failure rolls back.
-- -----------------------------------------------------------------------------
DO $check$
DECLARE
    v_bad      text;
    v_roles    text[] := ARRAY['nordiccart_owner', 'nordiccart_writer', 'nordiccart_reader', 'nordiccart_loader',
                               'nordiccart_scheduler', 'nordiccart_analyst', 'nordiccart_extractor'];
    v_definer  text[] := ARRAY['fn_capture_change()', 'fn_inventory_apply_movement()', 'fn_inventory_apply_reservation()'];
    v_app      text[] := ARRAY['nordiccart_writer', 'nordiccart_loader', 'nordiccart_scheduler', 'nordiccart_reader',
                               'nordiccart_analyst', 'nordiccart_extractor', 'public'];
BEGIN
    -- Roles: least attributes, no unexpected memberships
    SELECT string_agg(rolname, ', ') INTO v_bad
      FROM pg_roles
     WHERE rolname = ANY (v_roles)
       AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V008 self-check: roles with elevated attributes: %', v_bad;
    END IF;
    SELECT string_agg(r.rolname || ' in ' || g.rolname, ', ') INTO v_bad
      FROM pg_auth_members AS m
      JOIN pg_roles AS r ON r.oid = m.member
      JOIN pg_roles AS g ON g.oid = m.roleid
     WHERE (r.rolname = ANY (v_roles) OR g.rolname = 'nordiccart_owner')
       AND (r.rolname, g.rolname) NOT IN (('nordiccart_loader', 'nordiccart_writer'),
                                          ('nordiccart_scheduler', 'nordiccart_writer'),
                                          ('nordiccart_analyst', 'nordiccart_reader'));
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V008 self-check: unexpected role memberships: %', v_bad;
    END IF;

    -- Login, connection limits and role settings exactly as designed
    SELECT string_agg(e.role_name, ', ') INTO v_bad
      FROM (VALUES ('nordiccart_owner',     false,  0, ARRAY[]::text[]),
                   ('nordiccart_writer',    false,  0, ARRAY[]::text[]),
                   ('nordiccart_reader',    false,  0, ARRAY[]::text[]),
                   ('nordiccart_loader',    true,  30, ARRAY['idle_in_transaction_session_timeout=60s', 'lock_timeout=10s', 'statement_timeout=30min']),
                   ('nordiccart_scheduler', true,   3, ARRAY['idle_in_transaction_session_timeout=60s', 'lock_timeout=10s', 'statement_timeout=5min']),
                   ('nordiccart_analyst',   true,  10, ARRAY['default_transaction_read_only=on', 'idle_in_transaction_session_timeout=5min', 'statement_timeout=15min']),
                   ('nordiccart_extractor', true,   3, ARRAY['idle_in_transaction_session_timeout=60s', 'lock_timeout=10s', 'statement_timeout=30min']))
           AS e(role_name, can_login, connection_limit, settings)
      LEFT JOIN pg_roles AS r ON r.rolname = e.role_name
     WHERE r.oid IS NULL
        OR r.rolcanlogin IS DISTINCT FROM e.can_login
        OR r.rolconnlimit IS DISTINCT FROM e.connection_limit
        OR NOT r.rolinherit
        OR ARRAY(SELECT s FROM unnest(coalesce(r.rolconfig, ARRAY[]::text[])) AS s ORDER BY s) <> e.settings;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V008 self-check: login, connection limit or settings not as designed: %', v_bad;
    END IF;

    -- Ownership
    SELECT string_agg(obj, ', ') INTO v_bad FROM (
        SELECT c.oid::regclass::text AS obj
          FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
         WHERE n.nspname IN ('public', 'meta') AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
           AND c.relowner <> 'nordiccart_owner'::regrole
           AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
        UNION ALL
        SELECT p.oid::regprocedure::text
          FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
         WHERE n.nspname IN ('public', 'meta') AND p.proowner <> 'nordiccart_owner'::regrole
           AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
        UNION ALL
        SELECT t.oid::regtype::text
          FROM pg_type AS t JOIN pg_namespace AS n ON n.oid = t.typnamespace
         WHERE n.nspname IN ('public', 'meta') AND t.typtype IN ('e', 'd') AND t.typowner <> 'nordiccart_owner'::regrole
           AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e')
        UNION ALL
        SELECT 'schema meta' FROM pg_namespace WHERE nspname = 'meta' AND nspowner <> 'nordiccart_owner'::regrole
    ) AS x;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V008 self-check: not owned by nordiccart_owner: %', v_bad;
    END IF;

    -- SECURITY DEFINER: exactly the three functions, each with the fixed search_path
    SELECT string_agg(p.oid::regprocedure::text, ', ') INTO v_bad
      FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
     WHERE n.nspname IN ('public', 'meta')
       AND ((p.prosecdef AND NOT p.oid::regprocedure::text = ANY (v_definer))
         OR (p.oid::regprocedure::text = ANY (v_definer)
             AND NOT (p.prosecdef AND p.proconfig = ARRAY['search_path=pg_catalog, public, pg_temp'])));
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V008 self-check: SECURITY DEFINER or search_path not as designed: %', v_bad;
    END IF;

    -- No application role (nor PUBLIC) may change quantities, delete, truncate,
    -- create triggers or references, or write change_log
    SELECT string_agg(a.role_name || ' ' || a2.privilege || ' ' || c.oid::regclass::text, ', ') INTO v_bad
      FROM pg_class AS c
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
      CROSS JOIN unnest(v_app) AS a(role_name)
      CROSS JOIN unnest(ARRAY['DELETE', 'TRUNCATE', 'TRIGGER', 'REFERENCES']) AS a2(privilege)
     WHERE n.nspname IN ('public', 'meta') AND c.relkind = 'r'
       AND has_table_privilege(a.role_name, c.oid, a2.privilege)
       AND NOT (a.role_name = 'nordiccart_extractor' AND a2.privilege = 'DELETE' AND c.oid = 'public.change_log'::regclass);
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V008 self-check: forbidden table privileges: %', v_bad;
    END IF;
    SELECT string_agg(a.role_name || ' ' || col, ', ') INTO v_bad
      FROM unnest(v_app) AS a(role_name)
      CROSS JOIN unnest(ARRAY['quantity_on_hand', 'quantity_reserved', 'stock_location_id', 'product_id']) AS q(col)
     WHERE has_column_privilege(a.role_name, 'public.inventory_balances', q.col, 'UPDATE');
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V008 self-check: UPDATE of inventory balance quantities or identity granted: %', v_bad;
    END IF;
    SELECT string_agg(a.role_name || ' ' || t.tbl, ', ') INTO v_bad
      FROM unnest(v_app) AS a(role_name)
      CROSS JOIN unnest(ARRAY['public.order_items', 'public.inventory_movements', 'public.exchange_rates',
                              'public.change_log', 'meta.schema_migrations']) AS t(tbl)
     WHERE has_any_column_privilege(a.role_name, t.tbl, 'UPDATE');
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V008 self-check: UPDATE granted on insert-only history: %', v_bad;
    END IF;
    IF has_table_privilege('nordiccart_writer', 'public.change_log', 'INSERT')
       OR has_column_privilege('nordiccart_writer', 'public.change_log', 'row_data', 'SELECT')
       OR has_table_privilege('nordiccart_writer', 'public.change_log_checkpoints', 'SELECT') THEN
        RAISE EXCEPTION 'V008 self-check: the writer may write change_log or read its contents.';
    END IF;

    -- PUBLIC: nothing on the database, the schemas or NordicCart functions
    IF has_database_privilege('public', current_database(), 'CONNECT')
       OR has_database_privilege('public', current_database(), 'TEMPORARY')
       OR has_schema_privilege('public', 'public', 'CREATE') OR has_schema_privilege('public', 'public', 'USAGE')
       OR has_schema_privilege('public', 'meta', 'USAGE') THEN
        RAISE EXCEPTION 'V008 self-check: PUBLIC still has database or schema privileges.';
    END IF;
    SELECT string_agg(p.oid::regprocedure::text, ', ') INTO v_bad
      FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
     WHERE n.nspname IN ('public', 'meta') AND has_function_privilege('public', p.oid, 'EXECUTE')
       AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e');
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V008 self-check: PUBLIC may execute: %', v_bad;
    END IF;

    -- No application role may create objects or temporary tables
    SELECT string_agg(a.role_name, ', ') INTO v_bad
      FROM unnest(v_app) AS a(role_name)
     WHERE has_schema_privilege(a.role_name, 'public', 'CREATE') OR has_schema_privilege(a.role_name, 'meta', 'CREATE')
        OR has_database_privilege(a.role_name, current_database(), 'CREATE')
        OR has_database_privilege(a.role_name, current_database(), 'TEMPORARY');
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V008 self-check: application roles may create objects: %', v_bad;
    END IF;
END
$check$;

-- -----------------------------------------------------------------------------
-- Record this migration
-- -----------------------------------------------------------------------------
INSERT INTO meta.schema_migrations (version, description, script_name)
VALUES ('008', 'Security: roles, ownership, least privilege, SECURITY DEFINER, timeouts',
        'V008__security_roles_and_privileges.sql');

COMMIT;
