-- =============================================================================
-- NordicCart OLTP | Verification 01: environment and migration history
-- -----------------------------------------------------------------------------
-- Connect to  : nordiccart_oltp (any role that can read the system catalogs)
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS (WARN is advisory), failed_checks = 0.
--               Migrations newer than V005 show as INFO.
-- Requires    : V001 applied (the query reads meta.schema_migrations).
-- =============================================================================

WITH expected_migrations (version, script_name) AS (
    VALUES ('001', 'V001__migration_history_and_extensions.sql'),
           ('002', 'V002__change_capture_and_shared_trigger_functions.sql'),
           ('003', 'V003__reference_data_currencies_countries_exchange_rates.sql'),
           ('004', 'V004__catalogue_and_customers.sql'),
           ('005', 'V005__stock_locations_stores_warehouses.sql')
),
environment_checks AS (
    SELECT 1 AS check_no,
           'environment' AS area,
           'PostgreSQL version' AS check_name,
           '13 or later' AS expected,
           current_setting('server_version') AS actual,
           CASE WHEN current_setting('server_version_num')::integer >= 130000
                THEN 'PASS' ELSE 'FAIL' END AS status
    UNION ALL
    SELECT 2, 'environment', 'Database name',
           'nordiccart_oltp', current_database(),
           CASE WHEN current_database() LIKE 'nordiccart%' THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 3, 'environment', 'Database encoding',
           'UTF8', pg_encoding_to_char(d.encoding),
           CASE WHEN pg_encoding_to_char(d.encoding) = 'UTF8' THEN 'PASS' ELSE 'FAIL' END
      FROM pg_database AS d
     WHERE d.datname = current_database()
    UNION ALL
    -- Advisory only: timestamps are stored correctly in any session time zone,
    -- UTC just makes what you see match the design convention.
    SELECT 4, 'environment', 'Session time zone',
           'UTC', current_setting('TimeZone'),
           CASE WHEN current_setting('TimeZone') IN ('UTC', 'Etc/UTC', 'GMT') THEN 'PASS' ELSE 'WARN' END
    UNION ALL
    SELECT 5, 'environment', 'Extension btree_gist',
           'installed',
           coalesce((SELECT 'installed, version ' || e.extversion
                       FROM pg_extension AS e WHERE e.extname = 'btree_gist'), 'missing'),
           CASE WHEN EXISTS (SELECT 1 FROM pg_extension AS e WHERE e.extname = 'btree_gist')
                THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 6, 'environment', 'Schema meta',
           'exists',
           CASE WHEN EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'meta') THEN 'exists' ELSE 'missing' END,
           CASE WHEN EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'meta') THEN 'PASS' ELSE 'FAIL' END
),
migration_checks AS (
    SELECT 100 + row_number() OVER (ORDER BY coalesce(e.version, m.version))::integer AS check_no,
           'migration' AS area,
           'V' || coalesce(e.version, m.version) AS check_name,
           coalesce(e.script_name, 'not in this batch') AS expected,
           CASE WHEN m.version IS NULL THEN 'not applied'
                ELSE m.script_name || ' (applied ' || to_char(m.applied_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') || ' UTC by ' || m.applied_by || ')'
           END AS actual,
           CASE
               WHEN e.version IS NULL THEN 'INFO'
               WHEN m.version IS NULL THEN 'FAIL'
               WHEN m.script_name <> e.script_name THEN 'FAIL'
               ELSE 'PASS'
           END AS status
      FROM expected_migrations AS e
      FULL JOIN meta.schema_migrations AS m ON m.version = e.version
),
all_checks AS (
    SELECT * FROM environment_checks
    UNION ALL
    SELECT * FROM migration_checks
)
SELECT check_no,
       area,
       check_name,
       expected,
       actual,
       status,
       count(*) FILTER (WHERE status = 'FAIL') OVER () AS failed_checks,
       count(*) OVER () AS total_checks
  FROM all_checks
 ORDER BY check_no;
