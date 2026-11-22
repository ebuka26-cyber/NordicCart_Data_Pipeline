-- =============================================================================
-- NordicCart OLTP | Re-apply the database-level privileges of V008
-- -----------------------------------------------------------------------------
-- Why         : database-level privileges (CONNECT, TEMP on the database itself)
--               are NOT copied by CREATE DATABASE ... TEMPLATE and NOT restored by
--               pg_restore into an existing database (without --create). Every
--               other V008 setting (owners, SECURITY DEFINER, table, column and
--               function privileges) is restored, but PUBLIC would get CONNECT and
--               TEMP back. Verified in the S3 sandbox; verification script 17
--               (check 301) detects it.
-- When        : after restoring a post-V008 backup or recovery point into a new
--               database, and after copying the database from a template.
-- Connect to  : the restored or copied database, as a superuser or its owner.
-- Re-runnable : yes (idempotent). Writes no rows.
-- =============================================================================

DO $reapply$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM meta.schema_migrations WHERE version = '008') THEN
        RAISE EXCEPTION 'V008 is not applied to database %; use the V008 migration, not this script.', current_database();
    END IF;
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO nordiccart_writer, nordiccart_reader, nordiccart_extractor',
                   current_database());
    RAISE NOTICE 'Database-level privileges of V008 re-applied to %.', current_database();
END
$reapply$;
