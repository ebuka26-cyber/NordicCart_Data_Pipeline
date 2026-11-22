-- =============================================================================
-- NordicCart OLTP | Migration V003
-- Reference data and currency: currencies, countries, exchange_rates
-- -----------------------------------------------------------------------------
-- Design source : database/design/nordiccart_oltp_rev3.dbml (revision 3, approved)
-- Depends on    : V002 (change_log and shared trigger functions)
-- Connect to    : nordiccart_oltp, as the database owner
-- Transaction   : the whole file is one transaction (see V001 header).
-- Re-runnable   : no. A second run stops at the guard below.
-- -----------------------------------------------------------------------------
-- Dependency order inside this file
--   currencies  ->  countries        (fk_countries_currency)
--   currencies  ->  exchange_rates   (fk_exchange_rates_base_currency,
--                                     fk_exchange_rates_quote_currency)
--
-- Design items implemented
--   [DBML] columns, defaults, PKs, UNIQUEs, FKs and named CHECKs
--   [PG]   updated_at maintenance (currencies, countries)
--   [PG]   insert-only guard on exchange_rates
--   [PG]   change_log capture on all three tables
--
-- No rows are inserted. Reference data is loaded in a separate, later step.
-- =============================================================================

BEGIN;

SET LOCAL search_path TO public;
SET LOCAL lock_timeout = '10s';

SELECT meta.assert_migration_can_run('003', '002');

-- -----------------------------------------------------------------------------
-- currencies
-- -----------------------------------------------------------------------------
CREATE TABLE currencies (
    currency_code  char(3)      NOT NULL,
    currency_name  varchar(50)  NOT NULL,
    minor_units    smallint     NOT NULL DEFAULT 2,
    created_at     timestamptz  NOT NULL DEFAULT now(),
    updated_at     timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT pk_currencies PRIMARY KEY (currency_code)
);

COMMENT ON TABLE currencies IS
    'ISO 4217 currencies used by NordicCart markets and exchange rates.';
COMMENT ON COLUMN currencies.currency_code IS
    'ISO 4217: SEK, NOK, DKK, EUR';

-- -----------------------------------------------------------------------------
-- countries
-- -----------------------------------------------------------------------------
CREATE TABLE countries (
    country_code   char(2)       NOT NULL,
    country_name   varchar(100)  NOT NULL,
    currency_code  char(3)       NOT NULL,
    vat_rate       numeric(5,4)  NOT NULL,
    time_zone      varchar(64)   NOT NULL,
    created_at     timestamptz   NOT NULL DEFAULT now(),
    updated_at     timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT pk_countries PRIMARY KEY (country_code),
    CONSTRAINT uq_countries_country_name UNIQUE (country_name),
    CONSTRAINT fk_countries_currency
        FOREIGN KEY (currency_code) REFERENCES currencies (currency_code),
    CONSTRAINT ck_countries_vat_rate CHECK (vat_rate >= 0 AND vat_rate < 1)
);

COMMENT ON TABLE countries IS
    'Sales markets: SE, NO, DK, FI. Holds the current local currency, standard VAT rate and IANA time zone.';
COMMENT ON COLUMN countries.country_code IS
    'ISO 3166-1 alpha-2: SE, NO, DK, FI';
COMMENT ON COLUMN countries.currency_code IS
    'Current local currency. Transactions keep their own currency_code, so history survives a currency change.';
COMMENT ON COLUMN countries.vat_rate IS
    'Current standard VAT rate, e.g. 0.2500. Order lines snapshot the rate used.';
COMMENT ON COLUMN countries.time_zone IS
    'IANA zone, e.g. Europe/Stockholm, used for local reporting dates';

-- -----------------------------------------------------------------------------
-- exchange_rates (insert-only daily history, EUR base)
-- -----------------------------------------------------------------------------
CREATE TABLE exchange_rates (
    exchange_rate_id     bigint         GENERATED ALWAYS AS IDENTITY,
    rate_date            date           NOT NULL,
    base_currency_code   char(3)        NOT NULL DEFAULT 'EUR',
    quote_currency_code  char(3)        NOT NULL,
    rate                 numeric(18,8)  NOT NULL,
    source               varchar(50)    NOT NULL DEFAULT 'synthetic_ecb',
    created_at           timestamptz    NOT NULL DEFAULT now(),
    CONSTRAINT pk_exchange_rates PRIMARY KEY (exchange_rate_id),
    CONSTRAINT uq_exchange_rates_pair_date
        UNIQUE (base_currency_code, quote_currency_code, rate_date),
    CONSTRAINT fk_exchange_rates_base_currency
        FOREIGN KEY (base_currency_code) REFERENCES currencies (currency_code),
    CONSTRAINT fk_exchange_rates_quote_currency
        FOREIGN KEY (quote_currency_code) REFERENCES currencies (currency_code),
    CONSTRAINT ck_exchange_rates_rate_positive CHECK (rate > 0),
    CONSTRAINT ck_exchange_rates_distinct_pair CHECK (base_currency_code <> quote_currency_code)
);

COMMENT ON TABLE exchange_rates IS
    'Insert-only daily history, one row per currency pair per calendar day (EUR base, ECB style). Rates are never updated, so every historical conversion is reproducible. amount_eur = amount / rate for the event date; EUR amounts need no conversion.';
COMMENT ON COLUMN exchange_rates.rate IS
    'Units of quote currency per 1 unit of base currency';

-- -----------------------------------------------------------------------------
-- Triggers
-- -----------------------------------------------------------------------------
-- updated_at maintenance
CREATE TRIGGER trg_currencies_set_updated_at
    BEFORE UPDATE ON currencies
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE TRIGGER trg_countries_set_updated_at
    BEFORE UPDATE ON countries
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

-- insert-only guard on exchange_rates (no updated_at column by design)
CREATE TRIGGER trg_exchange_rates_insert_only
    BEFORE UPDATE OR DELETE ON exchange_rates
    FOR EACH ROW EXECUTE FUNCTION fn_reject_modification();

CREATE TRIGGER trg_exchange_rates_no_truncate
    BEFORE TRUNCATE ON exchange_rates
    FOR EACH STATEMENT EXECUTE FUNCTION fn_reject_modification();

-- change capture (argument = primary key column)
CREATE TRIGGER trg_currencies_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON currencies
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('currency_code');

CREATE TRIGGER trg_countries_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON countries
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('country_code');

CREATE TRIGGER trg_exchange_rates_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON exchange_rates
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('exchange_rate_id');

-- -----------------------------------------------------------------------------
-- Record this migration
-- -----------------------------------------------------------------------------
INSERT INTO meta.schema_migrations (version, description, script_name)
VALUES ('003', 'Reference data: currencies, countries, exchange_rates',
        'V003__reference_data_currencies_countries_exchange_rates.sql');

COMMIT;
