-- =============================================================================
-- NordicCart OLTP | Migration V002
-- Change data capture tables and shared trigger functions
-- -----------------------------------------------------------------------------
-- Design source : database/design/nordiccart_oltp_rev3.dbml (revision 3, approved)
-- Depends on    : V001
-- Connect to    : nordiccart_oltp, as the database owner
-- Transaction   : the whole file is one transaction (see V001 header).
-- Re-runnable   : no. A second run stops at the guard below.
-- -----------------------------------------------------------------------------
-- Why this comes before the business tables
--   The design note says "Build change capture first so the pipeline can start
--   from the bootstrap snapshot". Every table created from V003 onwards gets its
--   capture trigger in the same migration that creates it, so no change is ever
--   written without a change_log row.
--
-- Creates
--   * enum change_operation
--   * table change_log               [DBML] + [PG] identity CACHE 1, insert-only guard
--   * table change_log_checkpoints   [DBML] + [PG] updated_at maintenance
--   * fn_set_updated_at()            [PG] updated_at maintenance (BEFORE UPDATE)
--   * fn_reject_modification()      [PG] insert-only guard
--   * fn_capture_change(pk_column)   [PG] generic AFTER row trigger writing change_log
--
-- Not tracked in change_log (design): change_log, change_log_checkpoints.
-- =============================================================================

BEGIN;

SET LOCAL search_path TO public;
SET LOCAL lock_timeout = '10s';

SELECT meta.assert_migration_can_run('002', '001');

-- -----------------------------------------------------------------------------
-- Enum
-- -----------------------------------------------------------------------------
CREATE TYPE change_operation AS ENUM (
    'insert',
    'update',
    'delete'
);

-- -----------------------------------------------------------------------------
-- change_log: transactional outbox written by triggers in the writer's transaction
-- -----------------------------------------------------------------------------
CREATE TABLE change_log (
    change_id       bigint       GENERATED ALWAYS AS IDENTITY (CACHE 1),
    table_name      varchar(63)  NOT NULL,
    record_key      varchar(64)  NOT NULL,
    operation       change_operation NOT NULL,
    row_data        jsonb        NOT NULL,
    transaction_id  xid8         NOT NULL DEFAULT pg_current_xact_id(),
    changed_at      timestamptz  NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT pk_change_log PRIMARY KEY (change_id)
);

CREATE INDEX ix_change_log_transaction_id ON change_log (transaction_id);
CREATE INDEX ix_change_log_table_change_id ON change_log (table_name, change_id);

COMMENT ON TABLE change_log IS
    'Transactional change log (outbox pattern). One generic AFTER INSERT OR UPDATE OR DELETE row trigger on every operational table writes here in the same transaction. Extraction uses a snapshot checkpoint (pg_current_snapshot / pg_visible_in_snapshot), never change_id ranges or timestamps. Insert-only; old rows are purged with DELETE by the extractor role. Not tracked itself.';
COMMENT ON COLUMN change_log.change_id IS
    'PG: identity with CACHE 1. Orders changes to the same record; not a commit-order watermark.';
COMMENT ON COLUMN change_log.record_key IS
    'Primary key value of the changed row, as text';
COMMENT ON COLUMN change_log.row_data IS
    'Row image after the change, or the deleted row for delete';
COMMENT ON COLUMN change_log.transaction_id IS
    'Top-level transaction id of the writer; drives the snapshot checkpoint';
COMMENT ON COLUMN change_log.changed_at IS
    'Informational only';

-- -----------------------------------------------------------------------------
-- change_log_checkpoints: one row per extraction consumer
-- -----------------------------------------------------------------------------
CREATE TABLE change_log_checkpoints (
    consumer_name  varchar(50)  NOT NULL,
    last_snapshot  pg_snapshot  NOT NULL,
    created_at     timestamptz  NOT NULL DEFAULT now(),
    updated_at     timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT pk_change_log_checkpoints PRIMARY KEY (consumer_name)
);

COMMENT ON TABLE change_log_checkpoints IS
    'One row per extraction consumer. Written only by the extractor role after its destination load has committed. pg_snapshot_xmin(last_snapshot) is the lower bound for the next run and for the change_log purge. Not tracked in change_log.';
COMMENT ON COLUMN change_log_checkpoints.consumer_name IS
    'Extraction consumer, e.g. nordiccart_elt';
COMMENT ON COLUMN change_log_checkpoints.last_snapshot IS
    'Snapshot of the last batch committed in the destination';
COMMENT ON COLUMN change_log_checkpoints.updated_at IS
    'Time of the last checkpoint';

-- -----------------------------------------------------------------------------
-- Shared trigger functions
-- Table names inside function bodies are schema-qualified so the functions do
-- not depend on the caller's search_path.
-- -----------------------------------------------------------------------------

-- [PG] updated_at maintenance. now() is the transaction start time, as the
-- design note states: updated_at is for audit, never for incremental extraction.
CREATE FUNCTION fn_set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION fn_set_updated_at() IS
    'BEFORE UPDATE row trigger: sets updated_at to now() (transaction start time).';

-- [PG] insert-only guard. Used as a BEFORE UPDATE/DELETE row trigger and as a
-- BEFORE TRUNCATE statement trigger. SQLSTATE 23000 (integrity class) is never
-- retried: the design retries only 40001, 40P01 and 55P03.
CREATE FUNCTION fn_reject_modification()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    RAISE EXCEPTION '% on %.% is not allowed: the table is insert-only.',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = '23000',
              HINT = 'Write a new row instead of changing or removing an existing one.';
END
$fn$;

COMMENT ON FUNCTION fn_reject_modification() IS
    'Insert-only guard: raises SQLSTATE 23000 for UPDATE, DELETE or TRUNCATE on the table it is attached to.';

-- [PG] generic change capture. TG_ARGV[0] names the primary key column, whose
-- value becomes change_log.record_key. A wrong column name yields NULL and the
-- NOT NULL constraint on record_key stops the write, so a misconfigured trigger
-- can never silently lose a change.
CREATE FUNCTION fn_capture_change()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_row jsonb;
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_row := to_jsonb(OLD);
    ELSE
        v_row := to_jsonb(NEW);
    END IF;

    INSERT INTO public.change_log (table_name, record_key, operation, row_data)
    VALUES (TG_TABLE_NAME,
            v_row ->> TG_ARGV[0],
            lower(TG_OP)::public.change_operation,
            v_row);

    RETURN NULL;  -- AFTER trigger: return value is ignored
END
$fn$;

COMMENT ON FUNCTION fn_capture_change() IS
    'AFTER INSERT OR UPDATE OR DELETE row trigger. Argument: primary key column name. Writes the row image to change_log in the same transaction.';

-- -----------------------------------------------------------------------------
-- Triggers on the change capture tables
-- -----------------------------------------------------------------------------
-- change_log is insert-only. DELETE stays possible for the purge step in the
-- design; it will be limited to the extractor role by privileges in a later
-- migration. TRUNCATE is blocked because it would drop unextracted changes.
CREATE TRIGGER trg_change_log_insert_only
    BEFORE UPDATE ON change_log
    FOR EACH ROW EXECUTE FUNCTION fn_reject_modification();

CREATE TRIGGER trg_change_log_no_truncate
    BEFORE TRUNCATE ON change_log
    FOR EACH STATEMENT EXECUTE FUNCTION fn_reject_modification();

CREATE TRIGGER trg_change_log_checkpoints_set_updated_at
    BEFORE UPDATE ON change_log_checkpoints
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

-- -----------------------------------------------------------------------------
-- Record this migration
-- -----------------------------------------------------------------------------
INSERT INTO meta.schema_migrations (version, description, script_name)
VALUES ('002', 'Change capture tables and shared trigger functions',
        'V002__change_capture_and_shared_trigger_functions.sql');

COMMIT;
