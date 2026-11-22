-- =============================================================================
-- NordicCart OLTP | Rollback of migration V008 (security)
-- -----------------------------------------------------------------------------
-- Use         : when a committed V008 must be withdrawn. Recommended rollback:
--               in place, one transaction, no data row touched, rows written
--               after V008 kept. Fallback if it fails: restore the pre-V008
--               backup. A V008 that failed has already rolled itself back.
--               See docs/phase3_v008_deployment_and_rollback_guide.md, section 7.
-- Connect to  : the database V008 was applied to, as a SUPERUSER (postgres).
-- Transaction : one transaction. Roles are dropped at the end only when no
--               other database still uses them (they are cluster-wide).
-- Effect      : returns the catalog to its pre-V008 state. Tested: owners,
--               SECURITY INVOKER without fixed search_path, PUBLIC privileges
--               (CONNECT and TEMP on the database, USAGE on public, EXECUTE on
--               the functions) and the effective privileges of every object equal
--               those before V008. Data rows are not touched, except V008's row
--               in meta.schema_migrations.
-- Not undone  : nothing else; V009 and later must be rolled back first (guard).
-- =============================================================================

BEGIN;

SET LOCAL search_path TO public;
SET LOCAL lock_timeout = '10s';

DO $guard$
BEGIN
    IF NOT (SELECT r.rolsuper FROM pg_roles AS r WHERE r.rolname = current_user) THEN
        RAISE EXCEPTION 'R008 must run as a superuser (for example postgres), not as %.', current_user;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM meta.schema_migrations WHERE version = '008') THEN
        RAISE EXCEPTION 'V008 is not applied to database %; nothing to roll back.', current_database();
    END IF;
    IF EXISTS (SELECT 1 FROM meta.schema_migrations WHERE version > '008') THEN
        RAISE EXCEPTION 'Later migrations are applied (%); roll them back first.',
            (SELECT string_agg(version, ', ' ORDER BY version) FROM meta.schema_migrations WHERE version > '008');
    END IF;
END
$guard$;

-- 1. The three trigger functions run with the caller's rights again, without a fixed search_path
ALTER FUNCTION public.fn_inventory_apply_movement()    SECURITY INVOKER RESET search_path;
ALTER FUNCTION public.fn_inventory_apply_reservation() SECURITY INVOKER RESET search_path;
ALTER FUNCTION public.fn_capture_change()              SECURITY INVOKER RESET search_path;

COMMENT ON FUNCTION public.fn_inventory_apply_movement() IS
    'AFTER INSERT on inventory_movements: adds quantity_change to inventory_balances.quantity_on_hand.';
COMMENT ON FUNCTION public.fn_inventory_apply_reservation() IS
    'AFTER INSERT OR UPDATE OF status on inventory_reservations: keeps inventory_balances.quantity_reserved = SUM(quantity) of active reservations.';
COMMENT ON FUNCTION public.fn_capture_change() IS
    'AFTER INSERT OR UPDATE OR DELETE row trigger. Argument: primary key column name. Writes the row image to change_log in the same transaction.';

-- 2. Ownership back to the database owner (who ran V001 to V007)
DO $ownership$
DECLARE
    v_owner name := (SELECT pg_get_userbyid(d.datdba) FROM pg_database AS d WHERE d.datname = current_database());
BEGIN
    EXECUTE format('REASSIGN OWNED BY nordiccart_owner TO %I', v_owner);
END
$ownership$;

-- 3. Every privilege V008 granted to the NordicCart roles in this database, and the
--    default privileges of nordiccart_owner (they own nothing any more)
DROP OWNED BY nordiccart_loader, nordiccart_scheduler, nordiccart_analyst, nordiccart_extractor,
              nordiccart_writer, nordiccart_reader, nordiccart_owner;

-- 4. PUBLIC privileges as before V008 (PostgreSQL defaults)
GRANT USAGE ON SCHEMA public TO PUBLIC;
DO $public$
DECLARE
    r record;
BEGIN
    EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO PUBLIC', current_database());
    FOR r IN
        SELECT p.oid::regprocedure AS obj
          FROM pg_proc AS p
          JOIN pg_namespace AS n ON n.oid = p.pronamespace
         WHERE n.nspname IN ('public', 'meta')
           AND NOT EXISTS (SELECT 1 FROM pg_depend AS d
                            WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO PUBLIC', r.obj);
    END LOOP;
END
$public$;

-- 5. Forget V008
DELETE FROM meta.schema_migrations WHERE version = '008';

-- 6. Drop the roles that no other database still uses (cluster-wide objects)
DO $roles$
DECLARE
    v_role text;
BEGIN
    FOREACH v_role IN ARRAY ARRAY['nordiccart_loader', 'nordiccart_scheduler', 'nordiccart_analyst', 'nordiccart_extractor',
                                  'nordiccart_writer', 'nordiccart_reader', 'nordiccart_owner']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
            BEGIN
                EXECUTE format('DROP ROLE %I', v_role);
                RAISE NOTICE 'role % dropped', v_role;
            EXCEPTION WHEN dependent_objects_still_exist OR object_in_use THEN
                RAISE NOTICE 'role % kept: another database still uses it (%)', v_role, SQLERRM;
            END;
        END IF;
    END LOOP;
END
$roles$;

COMMIT;
