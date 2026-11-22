-- =============================================================================
-- NordicCart OLTP | One-time setup: create the operational database
-- -----------------------------------------------------------------------------
-- Connect to   : the maintenance database "postgres" (NOT nordiccart_oltp)
-- Run as       : a role that may create databases (for example "postgres")
-- Run how      : statement 1 on its own, then statement 2.
--                CREATE DATABASE cannot run inside a transaction block, so do
--                not wrap this file in BEGIN/COMMIT.
-- Re-runnable  : no. Statement 1 fails if the database already exists, which
--                is harmless; skip to the migrations in that case.
-- =============================================================================

-- 1. Create the database. UTF8 is required (V001 checks it) because names and
--    addresses contain Nordic characters (å, ä, ö, æ, ø).
CREATE DATABASE nordiccart_oltp
    WITH ENCODING 'UTF8'
         TEMPLATE template0;

-- 2. Show timestamps in UTC by default. The design stores every timestamp as
--    timestamptz in UTC; local reporting dates are derived later with
--    countries.time_zone. Takes effect for new connections to nordiccart_oltp.
ALTER DATABASE nordiccart_oltp SET timezone TO 'UTC';
