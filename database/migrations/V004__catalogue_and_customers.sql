-- =============================================================================
-- NordicCart OLTP | Migration V004
-- Catalogue and customers: product_categories, products, product_prices, customers
-- -----------------------------------------------------------------------------
-- Design source : database/design/nordiccart_oltp_rev3.dbml (revision 3, approved)
-- Depends on    : V003 (currencies, countries)
-- Connect to    : nordiccart_oltp, as the database owner
-- Transaction   : the whole file is one transaction (see V001 header).
-- Re-runnable   : no. A second run stops at the guard below.
-- -----------------------------------------------------------------------------
-- Dependency order inside this file
--   product_categories -> products        (fk_products_category)
--   products           -> product_prices  (fk_product_prices_product)
--   countries          -> product_prices  (fk_product_prices_country)
--   currencies         -> product_prices  (fk_product_prices_currency)
--   countries          -> customers       (fk_customers_country)
--
-- Design items implemented
--   [DBML] enum sales_channel; columns, defaults, PKs, UNIQUEs, FKs, indexes and
--          named CHECKs of the four tables
--   [PG]   EXCLUDE on product_prices validity periods (btree_gist, from V001)
--   [PG]   updated_at maintenance and change_log capture on all four tables
--
-- Not in this migration: promotions (design Phase 2), which also uses sales_channel.
-- No rows are inserted.
-- =============================================================================

BEGIN;

SET LOCAL search_path TO public;
SET LOCAL lock_timeout = '10s';

SELECT meta.assert_migration_can_run('004', '003');

-- -----------------------------------------------------------------------------
-- Enum (used by customers now, by promotions and orders later)
-- -----------------------------------------------------------------------------
CREATE TYPE sales_channel AS ENUM (
    'online',
    'in_store'
);

-- -----------------------------------------------------------------------------
-- product_categories
-- -----------------------------------------------------------------------------
CREATE TABLE product_categories (
    category_id    bigint        GENERATED ALWAYS AS IDENTITY,
    category_code  varchar(30)   NOT NULL,
    category_name  varchar(100)  NOT NULL,
    is_active      boolean       NOT NULL DEFAULT true,
    created_at     timestamptz   NOT NULL DEFAULT now(),
    updated_at     timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT pk_product_categories PRIMARY KEY (category_id),
    CONSTRAINT uq_product_categories_category_code UNIQUE (category_code),
    CONSTRAINT uq_product_categories_category_name UNIQUE (category_name)
);

COMMENT ON TABLE product_categories IS
    'Consumer electronics product categories.';
COMMENT ON COLUMN product_categories.category_code IS
    'e.g. LAPTOPS, TV_AUDIO';

-- -----------------------------------------------------------------------------
-- products
-- -----------------------------------------------------------------------------
CREATE TABLE products (
    product_id       bigint        GENERATED ALWAYS AS IDENTITY,
    sku              varchar(32)   NOT NULL,
    product_name     varchar(200)  NOT NULL,
    brand            varchar(100)  NOT NULL,
    category_id      bigint        NOT NULL,
    launched_on      date,
    discontinued_on  date,
    is_active        boolean       NOT NULL DEFAULT true,
    created_at       timestamptz   NOT NULL DEFAULT now(),
    updated_at       timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT pk_products PRIMARY KEY (product_id),
    CONSTRAINT uq_products_sku UNIQUE (sku),
    CONSTRAINT fk_products_category
        FOREIGN KEY (category_id) REFERENCES product_categories (category_id),
    CONSTRAINT ck_products_dates
        CHECK (discontinued_on IS NULL OR launched_on IS NULL OR discontinued_on >= launched_on)
);

CREATE INDEX ix_products_category_id ON products (category_id);
CREATE INDEX ix_products_updated_at ON products (updated_at);

COMMENT ON TABLE products IS
    'Product master. Prices and costs live in product_prices per market and validity period.';
COMMENT ON COLUMN products.sku IS
    'Stable business key';

-- -----------------------------------------------------------------------------
-- product_prices (effective-dated price list per product and market)
-- -----------------------------------------------------------------------------
CREATE TABLE product_prices (
    product_price_id  bigint         GENERATED ALWAYS AS IDENTITY,
    product_id        bigint         NOT NULL,
    country_code      char(2)        NOT NULL,
    currency_code     char(3)        NOT NULL,
    list_price        numeric(12,2)  NOT NULL,
    unit_cost         numeric(12,2)  NOT NULL,
    valid_from        timestamptz    NOT NULL,
    valid_to          timestamptz,
    created_at        timestamptz    NOT NULL DEFAULT now(),
    updated_at        timestamptz    NOT NULL DEFAULT now(),
    CONSTRAINT pk_product_prices PRIMARY KEY (product_price_id),
    CONSTRAINT uq_product_prices_product_country_from
        UNIQUE (product_id, country_code, valid_from),
    CONSTRAINT fk_product_prices_product
        FOREIGN KEY (product_id) REFERENCES products (product_id),
    CONSTRAINT fk_product_prices_country
        FOREIGN KEY (country_code) REFERENCES countries (country_code),
    CONSTRAINT fk_product_prices_currency
        FOREIGN KEY (currency_code) REFERENCES currencies (currency_code),
    CONSTRAINT ck_product_prices_list_price CHECK (list_price > 0),
    CONSTRAINT ck_product_prices_unit_cost CHECK (unit_cost >= 0),
    CONSTRAINT ck_product_prices_validity CHECK (valid_to IS NULL OR valid_to > valid_from),
    -- [PG] No overlapping validity per product and country. tstzrange uses the
    -- default [) bounds, so valid_to is exclusive and back-to-back periods are
    -- allowed; NULL valid_to means open-ended (current price).
    CONSTRAINT ex_product_prices_no_overlap
        EXCLUDE USING gist (
            product_id WITH =,
            country_code WITH =,
            tstzrange(valid_from, valid_to) WITH &&
        )
);

CREATE INDEX ix_product_prices_country_code ON product_prices (country_code);
CREATE INDEX ix_product_prices_updated_at ON product_prices (updated_at);

COMMENT ON TABLE product_prices IS
    'Effective-dated price list per product and market. No overlapping validity per product and country (EXCLUDE constraint). Order lines copy list_price and unit_cost at purchase time, so later price changes never alter past sales.';
COMMENT ON COLUMN product_prices.country_code IS
    'Sales market of this price';
COMMENT ON COLUMN product_prices.currency_code IS
    'Equals the market currency when the price was set';
COMMENT ON COLUMN product_prices.list_price IS
    'Consumer unit price incl. VAT, local currency';
COMMENT ON COLUMN product_prices.unit_cost IS
    'Landed unit cost excl. VAT, local currency';
COMMENT ON COLUMN product_prices.valid_to IS
    'Exclusive end. NULL means current price.';

-- -----------------------------------------------------------------------------
-- customers
-- -----------------------------------------------------------------------------
CREATE TABLE customers (
    customer_id      bigint         GENERATED ALWAYS AS IDENTITY,
    customer_number  varchar(20)    NOT NULL,
    first_name       varchar(100)   NOT NULL,
    last_name        varchar(100)   NOT NULL,
    email            varchar(255)   NOT NULL,
    phone            varchar(30),
    birth_date       date,
    address_line1    varchar(200),
    postal_code      varchar(10),
    city             varchar(100),
    country_code     char(2)        NOT NULL,
    signup_channel   sales_channel  NOT NULL,
    registered_at    timestamptz    NOT NULL,
    is_active        boolean        NOT NULL DEFAULT true,
    created_at       timestamptz    NOT NULL DEFAULT now(),
    updated_at       timestamptz    NOT NULL DEFAULT now(),
    CONSTRAINT pk_customers PRIMARY KEY (customer_id),
    CONSTRAINT uq_customers_customer_number UNIQUE (customer_number),
    CONSTRAINT uq_customers_email UNIQUE (email),
    CONSTRAINT fk_customers_country
        FOREIGN KEY (country_code) REFERENCES countries (country_code)
);

CREATE INDEX ix_customers_country_code ON customers (country_code);
CREATE INDEX ix_customers_updated_at ON customers (updated_at);

COMMENT ON TABLE customers IS
    'Registered customers. Deactivated with is_active = false instead of being deleted.';
COMMENT ON COLUMN customers.customer_number IS
    'Stable business key, e.g. C00012345';
COMMENT ON COLUMN customers.is_active IS
    'Deactivation instead of hard delete';

-- -----------------------------------------------------------------------------
-- Triggers: updated_at maintenance and change capture
-- -----------------------------------------------------------------------------
CREATE TRIGGER trg_product_categories_set_updated_at
    BEFORE UPDATE ON product_categories
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE TRIGGER trg_products_set_updated_at
    BEFORE UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE TRIGGER trg_product_prices_set_updated_at
    BEFORE UPDATE ON product_prices
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE TRIGGER trg_customers_set_updated_at
    BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE TRIGGER trg_product_categories_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON product_categories
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('category_id');

CREATE TRIGGER trg_products_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON products
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('product_id');

CREATE TRIGGER trg_product_prices_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON product_prices
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('product_price_id');

CREATE TRIGGER trg_customers_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON customers
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('customer_id');

-- -----------------------------------------------------------------------------
-- Record this migration
-- -----------------------------------------------------------------------------
INSERT INTO meta.schema_migrations (version, description, script_name)
VALUES ('004', 'Catalogue and customers: product_categories, products, product_prices, customers',
        'V004__catalogue_and_customers.sql');

COMMIT;
