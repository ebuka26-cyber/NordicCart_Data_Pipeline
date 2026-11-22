-- =============================================================================
-- NordicCart OLTP | Rollback of migration V009 (activity control and purchase orders)
-- -----------------------------------------------------------------------------
-- Use         : when a committed V009 must be withdrawn. V009 is purely additive,
--               so the rollback drops what it added: the receipt trigger on
--               inventory_movements, the five tables, their functions and enums,
--               and V009's row in meta.schema_migrations. No V001 to V008 object
--               or row is changed; no inventory movement is touched.
--               A V009 that failed has already rolled itself back.
-- Loses       : the rows of the five V009 tables (pilot purchase order documents
--               and R0 history can be written again by run_replenishment.py
--               --apply after V009 is re-applied; activity runs and checkpoints
--               cannot: R009 refuses while meta.activity_runs has rows). The
--               change_log rows already captured for the V009 business tables stay
--               (change_log is insert-only); they describe documents that no
--               longer exist.
-- Connect to  : the database V009 was applied to, as a SUPERUSER session (postgres).
-- Transaction : one transaction.
-- Guide       : docs/phase3_v009_deployment_and_rollback_guide.md
-- =============================================================================

BEGIN;

SET LOCAL lock_timeout = '10s';

DO $guard$
DECLARE
    v_runs bigint;
BEGIN
    IF NOT (SELECT r.rolsuper FROM pg_roles AS r WHERE r.rolname = session_user) THEN
        RAISE EXCEPTION 'R009 must run as a superuser session (for example postgres), not as %.', session_user;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM meta.schema_migrations WHERE version = '009') THEN
        RAISE EXCEPTION 'V009 is not applied to database %; nothing to roll back.', current_database();
    END IF;
    IF EXISTS (SELECT 1 FROM meta.schema_migrations WHERE version > '009') THEN
        RAISE EXCEPTION 'Later migrations are applied (%); roll them back first.',
            (SELECT string_agg(version, ', ' ORDER BY version) FROM meta.schema_migrations WHERE version > '009');
    END IF;
    SELECT count(*) INTO v_runs FROM meta.activity_runs;
    IF v_runs > 0 THEN
        RAISE EXCEPTION 'meta.activity_runs holds % runs: the activity engine has used V009. Restore a recovery point instead of rolling back.', v_runs;
    END IF;
    RAISE NOTICE 'R009 drops % purchase orders, % order lines and % reorder point revisions (re-creatable by the replenishment loader).',
        (SELECT count(*) FROM public.purchase_orders), (SELECT count(*) FROM public.purchase_order_lines),
        (SELECT count(*) FROM public.reorder_point_revisions);
END
$guard$;

SET LOCAL ROLE nordiccart_owner;

-- 1. The trigger V009 added to a V007 table
DROP TRIGGER trg_inventory_movements_purchase_order_receipt ON public.inventory_movements;

-- 2. Tables (their triggers, constraints and indexes go with them)
DROP TABLE meta.activity_checkpoints;
DROP TABLE meta.activity_runs;
DROP TABLE public.purchase_order_lines;
DROP TABLE public.purchase_orders;
DROP TABLE public.reorder_point_revisions;

-- 3. Functions
DROP FUNCTION public.fn_purchase_receipt_apply();
DROP FUNCTION public.fn_purchase_order_lines_refresh_order();
DROP FUNCTION public.fn_purchase_order_lines_guard();
DROP FUNCTION public.fn_purchase_orders_guard();
DROP FUNCTION public.fn_check_purchase_order_complete();
DROP FUNCTION public.fn_purchase_order_refresh(bigint);
DROP FUNCTION public.fn_purchase_order_state(bigint);
DROP FUNCTION public.fn_purchase_order_line_received(varchar, bigint, bigint);
DROP FUNCTION public.fn_reorder_point_revisions_guard();
DROP FUNCTION public.fn_check_reorder_point_revision_applied();
DROP FUNCTION meta.fn_activity_runs_guard();
DROP FUNCTION meta.fn_check_activity_run_verified();
DROP FUNCTION meta.fn_activity_checkpoints_guard();
DROP FUNCTION meta.fn_check_activity_checkpoint_verified();

-- 4. Enums
DROP TYPE meta.activity_run_status;
DROP TYPE meta.activity_run_mode;
DROP TYPE public.reorder_point_revision_kind;
DROP TYPE public.purchase_order_reason;
DROP TYPE public.purchase_order_status;

-- 5. Forget V009
DELETE FROM meta.schema_migrations WHERE version = '009';

COMMIT;
