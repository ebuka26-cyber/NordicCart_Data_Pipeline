-- =============================================================================
-- NordicCart OLTP | Migration V001
-- Migration history table, environment checks and required extensions
-- -----------------------------------------------------------------------------
-- Design source : database/design/nordiccart_oltp_rev3.dbml (revision 3, approved)
-- Depends on    : database/setup/00_create_database.sql
-- Connect to    : nordiccart_oltp, as the database owner (for example "postgres")
-- Transaction   : the whole file is one transaction. Any error rolls back
--                 everything; run ROLLBACK; in the same editor if the client
--                 stops at the error, then fix the cause and run the file again.
-- Re-runnable   : no. A second run stops at the guard below.
-- -----------------------------------------------------------------------------
-- Creates
--   * extension btree_gist          needed by the EXCLUDE constraint on
--                                   product_prices (V004), design item [PG]
--   * schema meta                   deployment metadata only, kept out of the
--                                   OLTP model so the public schema matches the DBML
--   * meta.schema_migrations        one row per applied migration
--   * meta.assert_migration_can_run guard used at the top of every migration
-- =============================================================================

BEGIN;

SET LOCAL lock_timeout = '10s';

-- -----------------------------------------------------------------------------
-- Environment checks
-- -----------------------------------------------------------------------------
DO $guard$
BEGIN
    -- xid8, pg_snapshot, pg_current_xact_id() and pg_current_snapshot() are
    -- required by the change capture design and exist from PostgreSQL 13.
    IF current_setting('server_version_num')::integer < 130000 THEN
        RAISE EXCEPTION 'NordicCart requires PostgreSQL 13 or later; this server runs %.',
            current_setting('server_version');
    END IF;

    IF (SELECT pg_encoding_to_char(d.encoding)
          FROM pg_database AS d
         WHERE d.datname = current_database()) <> 'UTF8' THEN
        RAISE EXCEPTION 'Database % must use UTF8 encoding.', current_database()
            USING HINT = 'Recreate it with database/setup/00_create_database.sql.';
    END IF;

    -- Protects against running into "postgres" or another project's database.
    -- Any name starting with nordiccart is accepted (nordiccart_oltp, nordiccart_oltp_test).
    IF current_database() NOT LIKE 'nordiccart%' THEN
        RAISE EXCEPTION 'Connected to database %, expected nordiccart_oltp.', current_database()
            USING HINT = 'Select nordiccart_oltp as the active database and run the file again.';
    END IF;

    IF to_regclass('meta.schema_migrations') IS NOT NULL THEN
        RAISE EXCEPTION 'Migration V001 is already applied (meta.schema_migrations exists).';
    END IF;
END
$guard$;

-- -----------------------------------------------------------------------------
-- Extensions
-- -----------------------------------------------------------------------------
-- btree_gist lets a GiST index combine plain equality (product_id, country_code)
-- with range overlap (validity period). It is a trusted extension in PostgreSQL 13+.
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;

-- -----------------------------------------------------------------------------
-- Migration history
-- -----------------------------------------------------------------------------
CREATE SCHEMA meta;

COMMENT ON SCHEMA meta IS
    'Deployment metadata for the NordicCart OLTP database. Not part of the operational data model and not tracked in change_log.';

CREATE TABLE meta.schema_migrations (
    version      varchar(10)  NOT NULL,
    description  varchar(200) NOT NULL,
    script_name  varchar(200) NOT NULL,
    applied_at   timestamptz  NOT NULL DEFAULT now(),
    applied_by   varchar(63)  NOT NULL DEFAULT current_user,
    CONSTRAINT pk_schema_migrations PRIMARY KEY (version),
    CONSTRAINT uq_schema_migrations_script_name UNIQUE (script_name)
);

COMMENT ON TABLE meta.schema_migrations IS
    'One row per applied migration script in database/migrations. Written by the last statement of each migration, inside its transaction.';

CREATE FUNCTION meta.assert_migration_can_run(p_version varchar, p_requires varchar)
RETURNS void
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF EXISTS (SELECT 1 FROM meta.schema_migrations WHERE version = p_version) THEN
        RAISE EXCEPTION 'Migration V% is already applied to database %.', p_version, current_database()
            USING HINT = 'Nothing to do. SELECT * FROM meta.schema_migrations ORDER BY version; shows what is applied.';
    END IF;

    IF p_requires IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM meta.schema_migrations WHERE version = p_requires) THEN
        RAISE EXCEPTION 'Migration V% requires V%, which is not applied yet.', p_version, p_requires
            USING HINT = 'Run the migrations in version order.';
    END IF;
END
$fn$;

COMMENT ON FUNCTION meta.assert_migration_can_run(varchar, varchar) IS
    'Stops a migration that was already applied or whose predecessor is missing. Called first in every migration.';

-- -----------------------------------------------------------------------------
-- Record this migration
-- -----------------------------------------------------------------------------
INSERT INTO meta.schema_migrations (version, description, script_name)
VALUES ('001', 'Migration history, environment checks and btree_gist',
        'V001__migration_history_and_extensions.sql');

COMMIT;
