-- =============================================================================
-- NordicCart OLTP | Migration V005
-- Stock locations: stock_locations (supertype), stores and warehouses (subtypes)
-- -----------------------------------------------------------------------------
-- Design source : database/design/nordiccart_oltp_rev3.dbml (revision 3, approved)
-- Depends on    : V004 (and V003 for countries)
-- Connect to    : nordiccart_oltp, as the database owner
-- Transaction   : the whole file is one transaction (see V001 header).
-- Re-runnable   : no. A second run stops at the guard below.
-- -----------------------------------------------------------------------------
-- Dependency order inside this file
--   countries       -> stock_locations  (fk_stock_locations_country)
--   stock_locations -> stores           (fk_stores_stock_location, composite)
--   stock_locations -> warehouses       (fk_warehouses_stock_location, composite)
--
-- How the type-checked supertype works (design note on stock_locations)
--   * stock_locations has UNIQUE (stock_location_id, location_type).
--   * stores and warehouses carry a fixed location_type ('store' / 'warehouse',
--     forced by a CHECK) and reference that pair with a composite FK, so a store
--     row can only point at a location of type store, and vice versa.
--   * [PG] a deferred constraint trigger checks at COMMIT that every location
--     has its matching subtype row. Insert the stock_locations row first, then
--     its stores or warehouses row, in the same transaction.
--
-- Design items implemented
--   [DBML] enum stock_location_type; columns, defaults, PKs, UNIQUEs, FKs,
--          indexes and named CHECKs of the three tables
--   [PG]   deferred stock location subtype check
--   [PG]   updated_at maintenance and change_log capture on all three tables
--
-- No rows are inserted.
-- =============================================================================

BEGIN;

SET LOCAL search_path TO public;
SET LOCAL lock_timeout = '10s';

SELECT meta.assert_migration_can_run('005', '004');

-- -----------------------------------------------------------------------------
-- Enum
-- -----------------------------------------------------------------------------
CREATE TYPE stock_location_type AS ENUM (
    'store',
    'warehouse'
);

-- -----------------------------------------------------------------------------
-- stock_locations (supertype for every place that holds stock)
-- -----------------------------------------------------------------------------
CREATE TABLE stock_locations (
    stock_location_id  bigint               GENERATED ALWAYS AS IDENTITY,
    location_code      varchar(20)          NOT NULL,
    location_name      varchar(100)         NOT NULL,
    location_type      stock_location_type  NOT NULL,
    country_code       char(2)              NOT NULL,
    address_line1      varchar(200)         NOT NULL,
    postal_code        varchar(10)          NOT NULL,
    city               varchar(100)         NOT NULL,
    opened_on          date                 NOT NULL,
    is_active          boolean              NOT NULL DEFAULT true,
    created_at         timestamptz          NOT NULL DEFAULT now(),
    updated_at         timestamptz          NOT NULL DEFAULT now(),
    CONSTRAINT pk_stock_locations PRIMARY KEY (stock_location_id),
    CONSTRAINT uq_stock_locations_location_code UNIQUE (location_code),
    -- Target of the type-checked FKs from stores and warehouses
    CONSTRAINT uq_stock_locations_id_type UNIQUE (stock_location_id, location_type),
    CONSTRAINT fk_stock_locations_country
        FOREIGN KEY (country_code) REFERENCES countries (country_code)
);

CREATE INDEX ix_stock_locations_country_code ON stock_locations (country_code);

COMMENT ON TABLE stock_locations IS
    'Supertype for every place that holds stock (12 stores and 4 warehouses). All stock tables reference it with ordinary FKs; there is no polymorphic location_id plus location_type pair. Every location must have its matching stores or warehouses row at commit (deferred constraint trigger).';
COMMENT ON COLUMN stock_locations.location_code IS
    'Stable business key, e.g. SE-STO-01, SE-WH-01';

-- -----------------------------------------------------------------------------
-- stores (subtype)
-- -----------------------------------------------------------------------------
CREATE TABLE stores (
    store_id           bigint               GENERATED ALWAYS AS IDENTITY,
    stock_location_id  bigint               NOT NULL,
    location_type      stock_location_type  NOT NULL DEFAULT 'store',
    store_format       varchar(20)          NOT NULL DEFAULT 'standard',
    created_at         timestamptz          NOT NULL DEFAULT now(),
    updated_at         timestamptz          NOT NULL DEFAULT now(),
    CONSTRAINT pk_stores PRIMARY KEY (store_id),
    -- One store per stock location (location_type is fixed)
    CONSTRAINT uq_stores_stock_location UNIQUE (stock_location_id, location_type),
    -- Target of the in-store fulfilment FK from orders (later migration)
    CONSTRAINT uq_stores_store_location UNIQUE (store_id, stock_location_id),
    CONSTRAINT fk_stores_stock_location
        FOREIGN KEY (stock_location_id, location_type)
        REFERENCES stock_locations (stock_location_id, location_type),
    CONSTRAINT ck_stores_location_type CHECK (location_type = 'store')
);

COMMENT ON TABLE stores IS
    'Retail store subtype of stock_locations. In-store orders are fulfilled from the selling store own stock location.';
COMMENT ON COLUMN stores.location_type IS
    'Fixed discriminator used by the composite FK';
COMMENT ON COLUMN stores.store_format IS
    'flagship or standard';

-- -----------------------------------------------------------------------------
-- warehouses (subtype)
-- -----------------------------------------------------------------------------
CREATE TABLE warehouses (
    warehouse_id            bigint               GENERATED ALWAYS AS IDENTITY,
    stock_location_id       bigint               NOT NULL,
    location_type           stock_location_type  NOT NULL DEFAULT 'warehouse',
    ships_online_orders     boolean              NOT NULL DEFAULT true,
    storage_capacity_units  integer,
    created_at              timestamptz          NOT NULL DEFAULT now(),
    updated_at              timestamptz          NOT NULL DEFAULT now(),
    CONSTRAINT pk_warehouses PRIMARY KEY (warehouse_id),
    -- One warehouse per stock location (location_type is fixed)
    CONSTRAINT uq_warehouses_stock_location UNIQUE (stock_location_id, location_type),
    CONSTRAINT fk_warehouses_stock_location
        FOREIGN KEY (stock_location_id, location_type)
        REFERENCES stock_locations (stock_location_id, location_type),
    CONSTRAINT ck_warehouses_location_type CHECK (location_type = 'warehouse'),
    CONSTRAINT ck_warehouses_capacity
        CHECK (storage_capacity_units IS NULL OR storage_capacity_units > 0)
);

COMMENT ON TABLE warehouses IS
    'Warehouse subtype of stock_locations. Online orders are fulfilled from the warehouse in their country.';
COMMENT ON COLUMN warehouses.location_type IS
    'Fixed discriminator used by the composite FK';

-- -----------------------------------------------------------------------------
-- [PG] Deferred subtype check: every location has its matching subtype row at commit
-- -----------------------------------------------------------------------------
-- Fires at COMMIT (DEFERRABLE INITIALLY DEFERRED) for:
--   * a new or retyped stock_locations row      -> its subtype row must exist
--   * a deleted or re-pointed stores/warehouses row -> the old location must
--     still have a subtype row (or be deleted itself in the same transaction)
-- Raises SQLSTATE 23514 (check_violation), a business rejection that the
-- design's retry rules never retry.
CREATE FUNCTION fn_check_stock_location_subtype()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_location_id  bigint;
    v_type         public.stock_location_type;
BEGIN
    IF TG_TABLE_NAME = 'stock_locations' THEN
        v_location_id := NEW.stock_location_id;
    ELSE
        v_location_id := OLD.stock_location_id;
    END IF;

    SELECT sl.location_type
      INTO v_type
      FROM public.stock_locations AS sl
     WHERE sl.stock_location_id = v_location_id;

    IF NOT FOUND THEN
        RETURN NULL;  -- the location itself was removed; nothing left to check
    END IF;

    IF (v_type = 'store'
        AND NOT EXISTS (SELECT 1 FROM public.stores AS s
                         WHERE s.stock_location_id = v_location_id))
       OR
       (v_type = 'warehouse'
        AND NOT EXISTS (SELECT 1 FROM public.warehouses AS w
                         WHERE w.stock_location_id = v_location_id)) THEN
        RAISE EXCEPTION 'Stock location % is of type % but has no matching % row.',
            v_location_id, v_type,
            CASE v_type WHEN 'store' THEN 'stores' ELSE 'warehouses' END
            USING ERRCODE = '23514',
                  HINT = 'Insert the stock_locations row and its subtype row in the same transaction.';
    END IF;

    RETURN NULL;
END
$fn$;

COMMENT ON FUNCTION fn_check_stock_location_subtype() IS
    'Deferred constraint trigger function: every stock location has its matching stores or warehouses row at commit.';

CREATE CONSTRAINT TRIGGER trg_stock_locations_subtype_required
    AFTER INSERT OR UPDATE OF location_type ON stock_locations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_check_stock_location_subtype();

CREATE CONSTRAINT TRIGGER trg_stores_subtype_required
    AFTER UPDATE OF stock_location_id OR DELETE ON stores
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_check_stock_location_subtype();

CREATE CONSTRAINT TRIGGER trg_warehouses_subtype_required
    AFTER UPDATE OF stock_location_id OR DELETE ON warehouses
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_check_stock_location_subtype();

-- -----------------------------------------------------------------------------
-- Triggers: updated_at maintenance and change capture
-- -----------------------------------------------------------------------------
CREATE TRIGGER trg_stock_locations_set_updated_at
    BEFORE UPDATE ON stock_locations
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE TRIGGER trg_stores_set_updated_at
    BEFORE UPDATE ON stores
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE TRIGGER trg_warehouses_set_updated_at
    BEFORE UPDATE ON warehouses
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE TRIGGER trg_stock_locations_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON stock_locations
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('stock_location_id');

CREATE TRIGGER trg_stores_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON stores
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('store_id');

CREATE TRIGGER trg_warehouses_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON warehouses
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('warehouse_id');

-- -----------------------------------------------------------------------------
-- Record this migration
-- -----------------------------------------------------------------------------
INSERT INTO meta.schema_migrations (version, description, script_name)
VALUES ('005', 'Stock locations: stock_locations, stores, warehouses',
        'V005__stock_locations_stores_warehouses.sql');

COMMIT;
