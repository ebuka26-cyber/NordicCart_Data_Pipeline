-- =============================================================================
-- NordicCart OLTP | Migration V007
-- Inventory: inventory_balances, inventory_reservations, inventory_movements
-- -----------------------------------------------------------------------------
-- Design source : database/design/nordiccart_oltp_rev3.dbml (revision 3, approved)
-- Depends on    : V006 (orders, order_items, payment_attempts) and V004/V005
-- Connect to    : nordiccart_oltp, as the database owner
-- Transaction   : the whole file is one transaction (see V001 header).
-- Re-runnable   : no. A second run stops at the guard below.
-- Changes to earlier objects: none. V001 to V006 functions and tables are not
--   replaced. V007 adds new triggers on orders and payment_attempts for the
--   inventory half of the order and payment rules.
-- -----------------------------------------------------------------------------
-- Dependency order inside this file
--   stock_locations, products        -> inventory_balances
--   inventory_balances, order_items  -> inventory_reservations
--   inventory_balances, order_items  -> inventory_movements (and itself, for transfers)
--   inventory_movements.return_item_id has no FK yet: return_items arrive in DBML Phase 4.
--
-- How stock works (DBML)
--   quantity_on_hand  = SUM(inventory_movements.quantity_change)   (ledger)
--   quantity_reserved = SUM(quantity) of active reservations
--   available         = quantity_on_hand - quantity_reserved        (derived)
--   Both quantities are maintained only by triggers on the two child tables.
--   CHECKs on inventory_balances stop negative stock and overselling
--   (quantity_reserved <= quantity_on_hand). Reservations never move stock;
--   stock leaves on hand exactly once, through the sale movement in U5.
--
-- Design items implemented
--   [DBML] enums reservation_status, movement_type; columns, defaults, PKs,
--          UNIQUE, FKs (including the composite balance FKs), indexes and
--          every named CHECK of the three tables
--   [PG]   partial uniques: one active reservation per order line; one sale per
--          order line; one return_restock per return item; one transfer_in per
--          transfer_out
--   [PG]   balance maintenance from movements and reservations; quantity
--          columns cannot be written directly
--   [PG]   reservation consistency: location = order fulfilment location,
--          product and quantity = order line; active -> consumed, released or
--          expired only; closed rows immutable
--   [PG]   insert-only movement ledger (V002 fn_reject_modification)
--   [PG]   sale needs a consumed reservation for the same line, location and
--          quantity; transfer_in needs a matching transfer_out elsewhere
--   [PG]   order rules that needed inventory (deferred from V006): stock_reserved
--          and confirmed need one active reservation per line; capture needs
--          all reservations active; payment_failed and cancelled keep no active
--          reservation; fulfilled needs every reservation consumed and one sale
--          movement per line
--   [PG]   updated_at maintenance and change_log capture on all three tables
--
-- Rules added from the business rules and the V007 brief (database/README.md, "V007 additions"):
--   * a balance row starts with zero quantities (stock enters through movements)
--   * reservations and balances are never deleted or truncated; the ledger is never truncated
--   * reservation terms never change; consume needs a confirmed order; release
--     needs stock_reserved, payment_failed or cancelled; expire needs
--     stock_reserved or cancelled and closed_at >= expires_at; times in order
--   * idempotency keys for every movement type: opening_balance once per
--     balance; purchase_receipt, stock_adjustment, write_off and transfer_out
--     carry a document reference, unique per type, location and product
--   * return_item_id stays NULL until returns exist (DBML MVP note)
--   * reference implementations of the inventory units of work, atomic and in
--     the design lock order: fn_reserve_order_stock (U2), fn_fulfil_order_stock
--     (U5), fn_close_order_reservations (release in U3, expire in U4). Each is
--     idempotent: a repeated call reports what already happened and changes nothing.
--
-- Lock order everywhere (DBML): orders row -> inventory_balances rows sorted by
-- (stock_location_id, product_id) -> reservation and movement writes in the same
-- order -> payment_attempts row. The guards lock the parent order row FOR UPDATE
-- themselves; the unit-of-work functions take every lock in this order.
--
-- No rows are inserted.
-- =============================================================================

BEGIN;

SET LOCAL search_path TO public;
SET LOCAL lock_timeout = '10s';

SELECT meta.assert_migration_can_run('007', '006');

-- -----------------------------------------------------------------------------
-- Enums
-- -----------------------------------------------------------------------------
CREATE TYPE reservation_status AS ENUM (
    'active',
    'consumed',
    'released',
    'expired'
);

COMMENT ON TYPE reservation_status IS
    'consumed: converted into a sale movement at fulfilment. released: after cancellation or payment failure. expired: released by the sweeper after expires_at.';

CREATE TYPE movement_type AS ENUM (
    'opening_balance',
    'purchase_receipt',
    'transfer_out',
    'transfer_in',
    'sale',
    'return_restock',
    'stock_adjustment',
    'write_off'
);

COMMENT ON TYPE movement_type IS
    'opening_balance +, purchase_receipt +, transfer_out -, transfer_in +, sale - (order line at fulfilment), return_restock + (resellable returns), stock_adjustment +/- (count correction), write_off - (damaged, lost or obsolete).';

-- -----------------------------------------------------------------------------
-- inventory_balances (current stock position per location and product)
-- -----------------------------------------------------------------------------
CREATE TABLE inventory_balances (
    inventory_balance_id  bigint       GENERATED ALWAYS AS IDENTITY,
    stock_location_id     bigint       NOT NULL,
    product_id            bigint       NOT NULL,
    quantity_on_hand      integer      NOT NULL DEFAULT 0,
    quantity_reserved     integer      NOT NULL DEFAULT 0,
    reorder_point         integer      NOT NULL DEFAULT 0,
    created_at            timestamptz  NOT NULL DEFAULT now(),
    updated_at            timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT pk_inventory_balances PRIMARY KEY (inventory_balance_id),
    -- One row per location and product; target of the composite balance FKs
    CONSTRAINT uq_inventory_balances_location_product UNIQUE (stock_location_id, product_id),
    CONSTRAINT fk_inventory_balances_location
        FOREIGN KEY (stock_location_id) REFERENCES stock_locations (stock_location_id),
    CONSTRAINT fk_inventory_balances_product
        FOREIGN KEY (product_id) REFERENCES products (product_id),
    CONSTRAINT ck_inventory_balances_on_hand CHECK (quantity_on_hand >= 0),
    CONSTRAINT ck_inventory_balances_reserved CHECK (quantity_reserved >= 0),
    CONSTRAINT ck_inventory_balances_no_oversell CHECK (quantity_reserved <= quantity_on_hand),
    CONSTRAINT ck_inventory_balances_reorder_point CHECK (reorder_point >= 0)
);

CREATE INDEX ix_inventory_balances_product_id ON inventory_balances (product_id);
CREATE INDEX ix_inventory_balances_updated_at ON inventory_balances (updated_at);

COMMENT ON TABLE inventory_balances IS
    'Current stock position, one row per location and product, created with zero quantities when the product is listed at the location. Available to sell = quantity_on_hand - quantity_reserved (derived, not stored). The no_oversell CHECK is the final guard: concurrent updates of the same row are serialized by its row lock and the CHECK sees the latest version, so the losing transaction fails with 23514 instead of overselling.';
COMMENT ON COLUMN inventory_balances.quantity_on_hand IS
    'Maintained only by trigger = SUM(inventory_movements.quantity_change)';
COMMENT ON COLUMN inventory_balances.quantity_reserved IS
    'Maintained only by trigger = SUM(quantity) of active reservations';
COMMENT ON COLUMN inventory_balances.reorder_point IS
    'Replenishment trigger level, set by the application';

-- -----------------------------------------------------------------------------
-- inventory_reservations (one per order line, created in U2)
-- -----------------------------------------------------------------------------
CREATE TABLE inventory_reservations (
    reservation_id     bigint              GENERATED ALWAYS AS IDENTITY,
    order_item_id      bigint              NOT NULL,
    stock_location_id  bigint              NOT NULL,
    product_id         bigint              NOT NULL,
    quantity           integer             NOT NULL,
    status             reservation_status  NOT NULL DEFAULT 'active',
    reserved_at        timestamptz         NOT NULL,
    expires_at         timestamptz         NOT NULL,
    closed_at          timestamptz,
    created_at         timestamptz         NOT NULL DEFAULT now(),
    updated_at         timestamptz         NOT NULL DEFAULT now(),
    CONSTRAINT pk_inventory_reservations PRIMARY KEY (reservation_id),
    CONSTRAINT fk_inventory_reservations_balance
        FOREIGN KEY (stock_location_id, product_id)
        REFERENCES inventory_balances (stock_location_id, product_id),
    CONSTRAINT fk_inventory_reservations_order_item
        FOREIGN KEY (order_item_id) REFERENCES order_items (order_item_id),
    CONSTRAINT ck_inventory_reservations_quantity CHECK (quantity > 0),
    CONSTRAINT ck_inventory_reservations_expiry CHECK (expires_at > reserved_at),
    CONSTRAINT ck_inventory_reservations_closed_at CHECK ((status = 'active') = (closed_at IS NULL))
);

CREATE INDEX ix_inventory_reservations_order_item_id ON inventory_reservations (order_item_id);
CREATE INDEX ix_inventory_reservations_location_product_status ON inventory_reservations (stock_location_id, product_id, status);
CREATE INDEX ix_inventory_reservations_status_expires_at ON inventory_reservations (status, expires_at);
CREATE INDEX ix_inventory_reservations_updated_at ON inventory_reservations (updated_at);

-- [PG] A retried U2 cannot reserve the same line twice.
CREATE UNIQUE INDEX uq_inventory_reservations_one_active_per_line
    ON inventory_reservations (order_item_id)
    WHERE status = 'active';

COMMENT ON TABLE inventory_reservations IS
    'One reservation per order line at the order fulfilment location, created in unit of work U2 (all lines or none). Lifecycle: consumed in U5 (fulfilment), released in U3 (final payment failure) or cancellation, expired in U4 (sweeper). Closed rows are immutable.';
COMMENT ON COLUMN inventory_reservations.expires_at IS
    'APP parameter: reserved_at + 15 min online, + 5 min in store';
COMMENT ON COLUMN inventory_reservations.closed_at IS
    'When consumed, released or expired';

-- -----------------------------------------------------------------------------
-- inventory_movements (insert-only stock ledger)
-- -----------------------------------------------------------------------------
CREATE TABLE inventory_movements (
    movement_id          bigint         GENERATED ALWAYS AS IDENTITY,
    stock_location_id    bigint         NOT NULL,
    product_id           bigint         NOT NULL,
    movement_type        movement_type  NOT NULL,
    quantity_change      integer        NOT NULL,
    occurred_at          timestamptz    NOT NULL,
    order_item_id        bigint,
    return_item_id       bigint,
    related_movement_id  bigint,
    external_reference   varchar(64),
    created_at           timestamptz    NOT NULL DEFAULT now(),
    CONSTRAINT pk_inventory_movements PRIMARY KEY (movement_id),
    CONSTRAINT fk_inventory_movements_balance
        FOREIGN KEY (stock_location_id, product_id)
        REFERENCES inventory_balances (stock_location_id, product_id),
    CONSTRAINT fk_inventory_movements_order_item
        FOREIGN KEY (order_item_id) REFERENCES order_items (order_item_id),
    -- fk_inventory_movements_return_item is added with return_items (DBML Phase 4).
    CONSTRAINT fk_inventory_movements_related
        FOREIGN KEY (related_movement_id) REFERENCES inventory_movements (movement_id),
    CONSTRAINT ck_inventory_movements_nonzero
        CHECK (quantity_change <> 0),
    CONSTRAINT ck_inventory_movements_inbound_positive
        CHECK (movement_type NOT IN ('opening_balance', 'purchase_receipt', 'transfer_in', 'return_restock')
               OR quantity_change > 0),
    CONSTRAINT ck_inventory_movements_outbound_negative
        CHECK (movement_type NOT IN ('transfer_out', 'sale', 'write_off') OR quantity_change < 0),
    CONSTRAINT ck_inventory_movements_sale_ref
        CHECK ((movement_type = 'sale') = (order_item_id IS NOT NULL)),
    CONSTRAINT ck_inventory_movements_restock_ref
        CHECK ((movement_type = 'return_restock') = (return_item_id IS NOT NULL)),
    CONSTRAINT ck_inventory_movements_transfer_ref
        CHECK ((movement_type = 'transfer_in') = (related_movement_id IS NOT NULL))
);

CREATE INDEX ix_inventory_movements_location_product_time ON inventory_movements (stock_location_id, product_id, occurred_at);
CREATE INDEX ix_inventory_movements_order_item_id ON inventory_movements (order_item_id);
CREATE INDEX ix_inventory_movements_return_item_id ON inventory_movements (return_item_id);
CREATE INDEX ix_inventory_movements_related_movement_id ON inventory_movements (related_movement_id);
CREATE INDEX ix_inventory_movements_created_at ON inventory_movements (created_at);

-- [PG] A retried step fails with 23505 instead of moving stock twice.
CREATE UNIQUE INDEX uq_inventory_movements_one_sale_per_line
    ON inventory_movements (order_item_id)
    WHERE movement_type = 'sale';
CREATE UNIQUE INDEX uq_inventory_movements_one_restock_per_return_item
    ON inventory_movements (return_item_id)
    WHERE movement_type = 'return_restock';
CREATE UNIQUE INDEX uq_inventory_movements_one_transfer_in_per_out
    ON inventory_movements (related_movement_id)
    WHERE movement_type = 'transfer_in';

-- V007 additions: idempotency keys for the movement types the DBML leaves open.
CREATE UNIQUE INDEX uq_inventory_movements_one_opening_balance
    ON inventory_movements (stock_location_id, product_id)
    WHERE movement_type = 'opening_balance';
CREATE UNIQUE INDEX uq_inventory_movements_document_line
    ON inventory_movements (movement_type, stock_location_id, product_id, external_reference)
    WHERE external_reference IS NOT NULL;

COMMENT ON TABLE inventory_movements IS
    'Insert-only stock ledger; corrections are new stock_adjustment rows. Reservations never create movements. Stock leaves on hand exactly once, through the sale movement in U5, so nothing is counted twice. Every movement type has an idempotency key (partial unique indexes).';
COMMENT ON COLUMN inventory_movements.quantity_change IS
    'Signed: positive adds stock, negative removes stock';
COMMENT ON COLUMN inventory_movements.occurred_at IS
    'Business time of the physical movement';
COMMENT ON COLUMN inventory_movements.order_item_id IS
    'Required for sale';
COMMENT ON COLUMN inventory_movements.return_item_id IS
    'Required for return_restock (Phase 4); NULL in the MVP';
COMMENT ON COLUMN inventory_movements.related_movement_id IS
    'transfer_in points to its matching transfer_out';
COMMENT ON COLUMN inventory_movements.external_reference IS
    'Supplier delivery note, stock count, write-off or transfer document. Required for purchase_receipt, stock_adjustment, write_off and transfer_out; unique per type, location and product.';

-- -----------------------------------------------------------------------------
-- [PG] Balance maintenance (AFTER triggers on the two child tables)
-- -----------------------------------------------------------------------------
-- Movement: on_hand += quantity_change. The CHECKs on inventory_balances reject
-- negative stock (ck_inventory_balances_on_hand) and moving reserved units
-- (ck_inventory_balances_no_oversell) with SQLSTATE 23514.
CREATE FUNCTION fn_inventory_apply_movement()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    UPDATE public.inventory_balances
       SET quantity_on_hand = quantity_on_hand + NEW.quantity_change
     WHERE stock_location_id = NEW.stock_location_id
       AND product_id = NEW.product_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'No inventory balance for location % and product %.', NEW.stock_location_id, NEW.product_id
            USING ERRCODE = 'foreign_key_violation', CONSTRAINT = 'fk_inventory_movements_balance';
    END IF;

    RETURN NULL;
END
$fn$;

COMMENT ON FUNCTION fn_inventory_apply_movement() IS
    'AFTER INSERT on inventory_movements: adds quantity_change to inventory_balances.quantity_on_hand.';

-- Reservation: reserved += quantity when a row becomes active, -= when it leaves active.
CREATE FUNCTION fn_inventory_apply_reservation()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_delta  integer := 0;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status = 'active' THEN
            v_delta := NEW.quantity;
        END IF;
    ELSIF OLD.status = 'active' AND NEW.status <> 'active' THEN
        v_delta := -OLD.quantity;
    ELSIF OLD.status <> 'active' AND NEW.status = 'active' THEN
        v_delta := NEW.quantity;  -- not reachable: the guard forbids reopening
    END IF;

    IF v_delta <> 0 THEN
        UPDATE public.inventory_balances
           SET quantity_reserved = quantity_reserved + v_delta
         WHERE stock_location_id = NEW.stock_location_id
           AND product_id = NEW.product_id;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'No inventory balance for location % and product %.', NEW.stock_location_id, NEW.product_id
                USING ERRCODE = 'foreign_key_violation', CONSTRAINT = 'fk_inventory_reservations_balance';
        END IF;
    END IF;

    RETURN NULL;
END
$fn$;

COMMENT ON FUNCTION fn_inventory_apply_reservation() IS
    'AFTER INSERT OR UPDATE OF status on inventory_reservations: keeps inventory_balances.quantity_reserved = SUM(quantity) of active reservations.';

-- -----------------------------------------------------------------------------
-- [PG] Guard functions (BEFORE row triggers). Business rules raise 23514 with
-- a rule name in the CONSTRAINT field, as in V005 and V006.
-- -----------------------------------------------------------------------------

-- inventory_balances: start empty; identity never changes; quantities change
-- only through the ledger triggers (trigger depth > 1). V008 also removes the
-- UPDATE privilege on the two columns from the application role.
CREATE FUNCTION fn_inventory_balances_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.quantity_on_hand <> 0 OR NEW.quantity_reserved <> 0 THEN
            RAISE EXCEPTION 'A new inventory balance starts at zero; stock enters through inventory_movements (opening_balance, purchase_receipt).'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_balances_start_empty';
        END IF;
        RETURN NEW;
    END IF;

    IF (NEW.inventory_balance_id, NEW.stock_location_id, NEW.product_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.inventory_balance_id, OLD.stock_location_id, OLD.product_id, OLD.created_at) THEN
        RAISE EXCEPTION 'Inventory balance %: id, location, product and created_at never change.', OLD.inventory_balance_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_balances_identity_immutable';
    END IF;

    IF (NEW.quantity_on_hand, NEW.quantity_reserved) IS DISTINCT FROM (OLD.quantity_on_hand, OLD.quantity_reserved)
       AND pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'Inventory balance %: quantity_on_hand and quantity_reserved change only through inventory_movements and inventory_reservations.', OLD.inventory_balance_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_balances_quantities_by_ledger',
                  HINT = 'Insert a stock_adjustment movement to correct on-hand stock.';
    END IF;

    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION fn_inventory_balances_guard() IS
    'BEFORE INSERT OR UPDATE on inventory_balances: new rows start at zero; id, location, product and created_at never change; quantity columns change only from the ledger triggers.';

-- inventory_reservations: never deleted; terms never change; lock the order;
-- insert as active, while the order is placed, matching its line; active ->
-- consumed, released or expired only; closed rows final; transition needs the
-- right order state; expiry only when due; times in order.
CREATE FUNCTION fn_inventory_reservations_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v  record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Reservation % cannot be deleted: release or expire it instead.', OLD.reservation_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_reservations_no_delete';
    END IF;

    IF TG_OP = 'UPDATE' AND
       (NEW.reservation_id, NEW.order_item_id, NEW.stock_location_id, NEW.product_id,
        NEW.quantity, NEW.reserved_at, NEW.expires_at, NEW.created_at)
       IS DISTINCT FROM
       (OLD.reservation_id, OLD.order_item_id, OLD.stock_location_id, OLD.product_id,
        OLD.quantity, OLD.reserved_at, OLD.expires_at, OLD.created_at) THEN
        RAISE EXCEPTION 'Reservation %: order line, location, product, quantity, reserved_at and expires_at never change.', OLD.reservation_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_reservations_terms_immutable';
    END IF;

    -- Lock the parent order first (design lock order).
    SELECT o.order_number, o.status, o.fulfilment_location_id, o.placed_at, o.confirmed_at,
           i.product_id AS line_product_id, i.quantity AS line_quantity
      INTO v
      FROM public.order_items AS i
      JOIN public.orders AS o ON o.order_id = i.order_id
     WHERE i.order_item_id = NEW.order_item_id
       FOR UPDATE OF o;

    IF NOT FOUND THEN
        RETURN NEW;  -- fk_inventory_reservations_order_item rejects it with 23503
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'active' THEN
            RAISE EXCEPTION 'A reservation for order % must be inserted as active, not %.', v.order_number, NEW.status
                USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_reservations_starts_active';
        END IF;
        IF v.status <> 'placed' THEN
            RAISE EXCEPTION 'Order % is %: stock is reserved only while the order is placed (U2).', v.order_number, v.status
                USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_reservations_order_placed';
        END IF;
        IF NEW.stock_location_id <> v.fulfilment_location_id
           OR NEW.product_id <> v.line_product_id
           OR NEW.quantity <> v.line_quantity THEN
            RAISE EXCEPTION 'Reservation for order % must be at its fulfilment location, for the line product and the full line quantity.', v.order_number
                USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_reservations_match_order_line';
        END IF;
        IF NEW.reserved_at < v.placed_at THEN
            RAISE EXCEPTION 'Reservation for order % is dated before the order was placed.', v.order_number
                USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_reservations_timestamp_order';
        END IF;
        RETURN NEW;
    END IF;

    -- UPDATE
    IF NEW.status IS DISTINCT FROM OLD.status
       AND NOT (OLD.status = 'active' AND NEW.status IN ('consumed', 'released', 'expired')) THEN
        RAISE EXCEPTION 'Reservation % of order % cannot move from % to %.', OLD.reservation_id, v.order_number, OLD.status, NEW.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_reservations_status_transition',
                  HINT = 'Allowed: active -> consumed, released or expired.';
    END IF;

    IF OLD.status <> 'active' THEN
        RAISE EXCEPTION 'Reservation % of order % is % and final: it can no longer be changed.', OLD.reservation_id, v.order_number, OLD.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_reservations_final_immutable';
    END IF;

    IF    (NEW.status = 'consumed' AND v.status <> 'confirmed')
       OR (NEW.status = 'released' AND v.status NOT IN ('stock_reserved', 'payment_failed', 'cancelled'))
       OR (NEW.status = 'expired'  AND v.status NOT IN ('stock_reserved', 'cancelled')) THEN
        RAISE EXCEPTION 'Reservation % cannot become % while order % is %.', OLD.reservation_id, NEW.status, v.order_number, v.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_reservations_order_state',
                  HINT = 'consumed: order confirmed (U5). released: stock_reserved, payment_failed or cancelled (U3, cancellation). expired: stock_reserved or cancelled (U4).';
    END IF;

    IF NEW.status = 'expired' AND NEW.closed_at < NEW.expires_at THEN
        RAISE EXCEPTION 'Reservation % expires at %; it cannot be expired at %.', OLD.reservation_id, NEW.expires_at, NEW.closed_at
            USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_reservations_expire_after_due';
    END IF;

    IF    coalesce(NEW.closed_at < NEW.reserved_at, false)
       OR (NEW.status = 'consumed' AND coalesce(NEW.closed_at < v.confirmed_at, false)) THEN
        RAISE EXCEPTION 'Reservation %: closed_at must not precede reserved_at, nor (when consumed) the order confirmation.', OLD.reservation_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_reservations_timestamp_order';
    END IF;

    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION fn_inventory_reservations_guard() IS
    'BEFORE INSERT OR UPDATE OR DELETE on inventory_reservations: no delete; terms never change; locks the order FOR UPDATE; insert as active while the order is placed, matching its line; active -> consumed (order confirmed), released (stock_reserved, payment_failed, cancelled) or expired (stock_reserved, cancelled, when due); closed rows final; times in order.';

-- inventory_movements (insert only; UPDATE and DELETE are refused by
-- fn_reject_modification from V002): MVP has no returns; document reference for
-- manual movements; a sale needs a consumed reservation of the same line,
-- location and quantity while the order is confirmed; a transfer_in needs the
-- matching transfer_out elsewhere; times in order.
CREATE FUNCTION fn_inventory_movements_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v  record;
    m  record;
BEGIN
    IF NEW.return_item_id IS NOT NULL THEN
        RAISE EXCEPTION 'inventory_movements.return_item_id must be NULL until returns are implemented (DBML Phase 4).'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_movements_return_restock_mvp';
    END IF;

    IF NEW.movement_type IN ('purchase_receipt', 'stock_adjustment', 'write_off', 'transfer_out')
       AND NEW.external_reference IS NULL THEN
        RAISE EXCEPTION 'A % movement needs external_reference (delivery note, stock count, write-off or transfer document): it is its idempotency key.', NEW.movement_type
            USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_movements_document_reference_required';
    END IF;

    IF NEW.movement_type = 'sale' AND NEW.order_item_id IS NOT NULL THEN
        SELECT o.order_number, o.status, o.confirmed_at
          INTO v
          FROM public.order_items AS i
          JOIN public.orders AS o ON o.order_id = i.order_id
         WHERE i.order_item_id = NEW.order_item_id
           FOR UPDATE OF o;

        IF FOUND THEN
            IF v.status <> 'confirmed' THEN
                RAISE EXCEPTION 'Order % is %: stock is issued only for a confirmed order (U5).', v.order_number, v.status
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_movements_sale_order_state';
            END IF;
            IF NOT EXISTS (SELECT 1
                             FROM public.inventory_reservations AS r
                            WHERE r.order_item_id = NEW.order_item_id
                              AND r.status = 'consumed'
                              AND r.stock_location_id = NEW.stock_location_id
                              AND r.product_id = NEW.product_id
                              AND r.quantity = -NEW.quantity_change) THEN
                RAISE EXCEPTION 'Sale for order % needs a consumed reservation of the same line, location and quantity. Consume the reservation first.', v.order_number
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_movements_sale_matches_consumed_reservation';
            END IF;
            IF NEW.occurred_at < v.confirmed_at THEN
                RAISE EXCEPTION 'Sale for order % is dated before the order was confirmed.', v.order_number
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_movements_timestamp_order';
            END IF;
        END IF;
    END IF;

    IF NEW.movement_type = 'transfer_in' AND NEW.related_movement_id IS NOT NULL THEN
        SELECT t.movement_type, t.product_id, t.stock_location_id, t.quantity_change, t.occurred_at
          INTO m
          FROM public.inventory_movements AS t
         WHERE t.movement_id = NEW.related_movement_id;

        IF FOUND THEN
            IF m.movement_type <> 'transfer_out'
               OR m.product_id <> NEW.product_id
               OR m.stock_location_id = NEW.stock_location_id
               OR m.quantity_change <> -NEW.quantity_change THEN
                RAISE EXCEPTION 'transfer_in must point to a transfer_out of the same product at another location with the opposite quantity.'
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_movements_transfer_matches_transfer_out';
            END IF;
            IF NEW.occurred_at < m.occurred_at THEN
                RAISE EXCEPTION 'transfer_in is dated before its transfer_out.'
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_movements_timestamp_order';
            END IF;
        END IF;
    END IF;

    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION fn_inventory_movements_guard() IS
    'BEFORE INSERT on inventory_movements: return_item_id NULL in the MVP; document reference for purchase_receipt, stock_adjustment, write_off and transfer_out; sale only for a confirmed order with a matching consumed reservation; transfer_in matches its transfer_out; times in order.';

-- payment_attempts (new trigger, V006 guard unchanged): capture needs every
-- line of the order to hold an active reservation. Runs after
-- trg_payment_attempts_guard (name order), which has locked the order row.
CREATE FUNCTION fn_payment_attempts_inventory_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_order_number  varchar(30);
BEGIN
    IF EXISTS (SELECT 1
                 FROM public.order_items AS i
                WHERE i.order_id = NEW.order_id
                  AND NOT EXISTS (SELECT 1
                                    FROM public.inventory_reservations AS r
                                   WHERE r.order_item_id = i.order_item_id
                                     AND r.status = 'active')) THEN
        SELECT o.order_number INTO v_order_number FROM public.orders AS o WHERE o.order_id = NEW.order_id;
        RAISE EXCEPTION 'Order %: a payment can be captured only while every line holds an active reservation.', v_order_number
            USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_capture_requires_active_reservations',
                  HINT = 'Void the authorization instead (U3c).';
    END IF;
    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION fn_payment_attempts_inventory_guard() IS
    'BEFORE UPDATE OF status on payment_attempts, when the attempt becomes captured: every order line must hold an active reservation (DBML U3c).';

-- TRUNCATE skips row triggers, so inventory history gets a statement guard.
CREATE FUNCTION fn_reject_inventory_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    RAISE EXCEPTION 'TRUNCATE on %.% is not allowed: inventory history is never bulk-deleted.',
        TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = '23000', CONSTRAINT = 'inventory_no_truncate';
END
$fn$;

COMMENT ON FUNCTION fn_reject_inventory_truncate() IS
    'BEFORE TRUNCATE statement trigger on inventory_balances, inventory_reservations and inventory_movements: raises SQLSTATE 23000.';

-- -----------------------------------------------------------------------------
-- [PG] Deferred order-inventory check (fires at COMMIT)
-- -----------------------------------------------------------------------------
CREATE FUNCTION fn_assert_order_inventory_consistent(p_order_id bigint)
RETURNS void
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_order           record;
    v_lines           bigint;
    v_lines_active    bigint;
    v_lines_consumed  bigint;
    v_lines_sold      bigint;
BEGIN
    SELECT o.order_number, o.status INTO v_order
      FROM public.orders AS o
     WHERE o.order_id = p_order_id;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT count(*),
           count(*) FILTER (WHERE EXISTS (SELECT 1 FROM public.inventory_reservations AS r
                                           WHERE r.order_item_id = i.order_item_id AND r.status = 'active')),
           count(*) FILTER (WHERE EXISTS (SELECT 1 FROM public.inventory_reservations AS r
                                           WHERE r.order_item_id = i.order_item_id AND r.status = 'consumed')),
           count(*) FILTER (WHERE EXISTS (SELECT 1 FROM public.inventory_movements AS m
                                           WHERE m.order_item_id = i.order_item_id AND m.movement_type = 'sale'))
      INTO v_lines, v_lines_active, v_lines_consumed, v_lines_sold
      FROM public.order_items AS i
     WHERE i.order_id = p_order_id;

    IF v_order.status = 'placed' AND (v_lines_active + v_lines_consumed + v_lines_sold) > 0 THEN
        RAISE EXCEPTION 'Order % is placed but holds reservations or sales; set it stock_reserved in the same transaction (U2).', v_order.order_number
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_placed_no_reservations';
    END IF;

    IF v_order.status IN ('stock_reserved', 'confirmed')
       AND (v_lines_active <> v_lines OR v_lines_consumed > 0 OR v_lines_sold > 0) THEN
        RAISE EXCEPTION 'Order % is % but only % of % lines hold an active reservation.',
            v_order.order_number, v_order.status, v_lines_active, v_lines
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_reservations_per_line';
    END IF;

    IF v_order.status = 'fulfilled'
       AND (v_lines_active > 0 OR v_lines_consumed <> v_lines OR v_lines_sold <> v_lines) THEN
        RAISE EXCEPTION 'Order % is fulfilled but % of % lines have a consumed reservation and % a sale movement.',
            v_order.order_number, v_lines_consumed, v_lines, v_lines_sold
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_fulfilled_stock_issued';
    END IF;

    IF v_order.status IN ('payment_failed', 'cancelled') AND (v_lines_active > 0 OR v_lines_sold > 0) THEN
        RAISE EXCEPTION 'Order % is % but % lines still hold an active reservation; release or expire them in the same transaction.',
            v_order.order_number, v_order.status, v_lines_active
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_closed_reservations_released';
    END IF;
END
$fn$;

COMMENT ON FUNCTION fn_assert_order_inventory_consistent(bigint) IS
    'Order-inventory rules checked at commit: placed holds no reservation; stock_reserved and confirmed hold one active reservation per line; fulfilled has every line consumed and sold once; payment_failed and cancelled hold no active reservation.';

CREATE FUNCTION fn_check_order_inventory()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_order_id  bigint;
BEGIN
    IF TG_TABLE_NAME = 'orders' THEN
        v_order_id := NEW.order_id;
    ELSE
        SELECT i.order_id INTO v_order_id
          FROM public.order_items AS i
         WHERE i.order_item_id = NEW.order_item_id;
    END IF;

    IF v_order_id IS NOT NULL THEN
        PERFORM public.fn_assert_order_inventory_consistent(v_order_id);
    END IF;
    RETURN NULL;
END
$fn$;

COMMENT ON FUNCTION fn_check_order_inventory() IS
    'Deferred constraint trigger function on orders, inventory_reservations and sale movements: runs fn_assert_order_inventory_consistent for the affected order at commit.';

-- -----------------------------------------------------------------------------
-- Units of work (reference implementations, called inside the caller's transaction)
-- Each takes its locks in the design order and is idempotent.
-- -----------------------------------------------------------------------------

-- Locks the balance rows of an order's lines at its fulfilment location, sorted
-- by (stock_location_id, product_id). Shared by the three units of work.
CREATE FUNCTION fn_lock_order_balances(p_order_id bigint)
RETURNS void
LANGUAGE plpgsql
AS $fn$
BEGIN
    PERFORM 1
       FROM public.inventory_balances AS b
       JOIN public.orders AS o
         ON o.order_id = p_order_id
        AND b.stock_location_id = o.fulfilment_location_id
      WHERE b.product_id IN (SELECT i.product_id FROM public.order_items AS i WHERE i.order_id = p_order_id)
      ORDER BY b.stock_location_id, b.product_id
        FOR UPDATE OF b;
END
$fn$;

COMMENT ON FUNCTION fn_lock_order_balances(bigint) IS
    'Locks FOR UPDATE the inventory_balances rows an order uses, in (stock_location_id, product_id) order (design lock order).';

-- U2 Reserve, all lines or none.
--   'reserved'         reservations inserted, order now stock_reserved
--   'out_of_stock'     at least one line is short; nothing changed. Per the
--                      design the caller rolls back and cancels the order
--                      (out_of_stock) in a new transaction.
--   'already_reserved' the order is already stock_reserved, confirmed or
--                      fulfilled; nothing changed (idempotent retry)
CREATE FUNCTION fn_reserve_order_stock(p_order_id bigint, p_reserved_at timestamptz, p_expires_at timestamptz)
RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_order  record;
    v_short  bigint;
BEGIN
    SELECT o.order_number, o.status, o.fulfilment_location_id
      INTO v_order
      FROM public.orders AS o
     WHERE o.order_id = p_order_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Order id % does not exist.', p_order_id USING ERRCODE = 'no_data_found';
    END IF;

    IF v_order.status IN ('stock_reserved', 'confirmed', 'fulfilled') THEN
        RETURN 'already_reserved';
    END IF;

    IF v_order.status <> 'placed' THEN
        RAISE EXCEPTION 'Order % is %: stock is reserved only while the order is placed (U2).', v_order.order_number, v_order.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'inventory_reservations_order_placed';
    END IF;

    PERFORM public.fn_lock_order_balances(p_order_id);

    -- Availability under the row locks: a missing balance row means not listed.
    SELECT count(*) INTO v_short
      FROM public.order_items AS i
      LEFT JOIN public.inventory_balances AS b
        ON b.stock_location_id = v_order.fulfilment_location_id
       AND b.product_id = i.product_id
     WHERE i.order_id = p_order_id
       AND (b.inventory_balance_id IS NULL OR b.quantity_on_hand - b.quantity_reserved < i.quantity);

    IF v_short > 0 THEN
        RETURN 'out_of_stock';
    END IF;

    INSERT INTO public.inventory_reservations
           (order_item_id, stock_location_id, product_id, quantity, reserved_at, expires_at)
    SELECT i.order_item_id, v_order.fulfilment_location_id, i.product_id, i.quantity, p_reserved_at, p_expires_at
      FROM public.order_items AS i
     WHERE i.order_id = p_order_id
     ORDER BY i.product_id;

    UPDATE public.orders SET status = 'stock_reserved'
     WHERE order_id = p_order_id AND status = 'placed';

    RETURN 'reserved';
END
$fn$;

COMMENT ON FUNCTION fn_reserve_order_stock(bigint, timestamptz, timestamptz) IS
    'U2 Reserve (all lines or none): locks the order, then its balance rows in lock order, checks availability, inserts one active reservation per line and sets the order stock_reserved. Returns reserved, out_of_stock (nothing changed) or already_reserved (idempotent).';

-- U3 final failure (released) and U4 expiry (expired): closes every active
-- reservation of the order. Returns how many were closed; 0 on a repeated
-- call. The caller changes the order status (and voids authorizations) in the
-- same transaction.
CREATE FUNCTION fn_close_order_reservations(p_order_id bigint, p_status reservation_status, p_closed_at timestamptz)
RETURNS integer
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_count  integer;
BEGIN
    IF p_status NOT IN ('released', 'expired') THEN
        RAISE EXCEPTION 'fn_close_order_reservations closes reservations as released or expired, not %.', p_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    PERFORM 1 FROM public.orders AS o WHERE o.order_id = p_order_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Order id % does not exist.', p_order_id USING ERRCODE = 'no_data_found';
    END IF;

    PERFORM public.fn_lock_order_balances(p_order_id);

    UPDATE public.inventory_reservations AS r
       SET status = p_status,
           closed_at = p_closed_at
      FROM public.order_items AS i
     WHERE i.order_item_id = r.order_item_id
       AND i.order_id = p_order_id
       AND r.status = 'active';

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END
$fn$;

COMMENT ON FUNCTION fn_close_order_reservations(bigint, reservation_status, timestamptz) IS
    'U3 (released) and U4 (expired): locks the order and its balance rows in lock order and closes every active reservation. Returns the number closed (0 when repeated).';

-- U5 Fulfil: per line in lock order FIRST consume the reservation, THEN insert
-- the sale movement; finally set the order fulfilled.
--   'fulfilled'         stock issued, order fulfilled
--   'already_fulfilled' nothing changed (idempotent retry)
CREATE FUNCTION fn_fulfil_order_stock(p_order_id bigint, p_fulfilled_at timestamptz)
RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_order  record;
    r        record;
    v_lines  bigint;
    v_done   bigint := 0;
BEGIN
    SELECT o.order_number, o.status
      INTO v_order
      FROM public.orders AS o
     WHERE o.order_id = p_order_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Order id % does not exist.', p_order_id USING ERRCODE = 'no_data_found';
    END IF;

    IF v_order.status = 'fulfilled' THEN
        RETURN 'already_fulfilled';
    END IF;

    IF v_order.status <> 'confirmed' THEN
        RAISE EXCEPTION 'Order % is %: only a confirmed order can be fulfilled (U5).', v_order.order_number, v_order.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_status_transition';
    END IF;

    PERFORM public.fn_lock_order_balances(p_order_id);

    FOR r IN
        SELECT res.reservation_id, res.order_item_id, res.stock_location_id, res.product_id, res.quantity
          FROM public.inventory_reservations AS res
          JOIN public.order_items AS i ON i.order_item_id = res.order_item_id
         WHERE i.order_id = p_order_id
           AND res.status = 'active'
         ORDER BY res.stock_location_id, res.product_id
    LOOP
        UPDATE public.inventory_reservations
           SET status = 'consumed', closed_at = p_fulfilled_at
         WHERE reservation_id = r.reservation_id
           AND status = 'active';

        IF NOT FOUND THEN
            RAISE EXCEPTION 'Reservation % changed concurrently; fulfilment of order % aborted.', r.reservation_id, v_order.order_number
                USING ERRCODE = 'serialization_failure';
        END IF;

        INSERT INTO public.inventory_movements
               (stock_location_id, product_id, movement_type, quantity_change, occurred_at, order_item_id)
        VALUES (r.stock_location_id, r.product_id, 'sale', -r.quantity, p_fulfilled_at, r.order_item_id);

        v_done := v_done + 1;
    END LOOP;

    SELECT count(*) INTO v_lines FROM public.order_items AS i WHERE i.order_id = p_order_id;
    IF v_done <> v_lines THEN
        RAISE EXCEPTION 'Order %: % of % lines held an active reservation; nothing was fulfilled.', v_order.order_number, v_done, v_lines
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_fulfilled_stock_issued';
    END IF;

    UPDATE public.orders SET status = 'fulfilled', fulfilled_at = p_fulfilled_at
     WHERE order_id = p_order_id AND status = 'confirmed';

    RETURN 'fulfilled';
END
$fn$;

COMMENT ON FUNCTION fn_fulfil_order_stock(bigint, timestamptz) IS
    'U5 Fulfil: locks the order and its balance rows in lock order; per line consumes the reservation, then inserts the sale movement; sets the order fulfilled. Returns fulfilled or already_fulfilled (idempotent).';

-- -----------------------------------------------------------------------------
-- Triggers
-- -----------------------------------------------------------------------------
-- Guards (BEFORE, fire in name order: *_guard before *_set_updated_at)
CREATE TRIGGER trg_inventory_balances_guard
    BEFORE INSERT OR UPDATE ON inventory_balances
    FOR EACH ROW EXECUTE FUNCTION fn_inventory_balances_guard();

CREATE TRIGGER trg_inventory_reservations_guard
    BEFORE INSERT OR UPDATE OR DELETE ON inventory_reservations
    FOR EACH ROW EXECUTE FUNCTION fn_inventory_reservations_guard();

CREATE TRIGGER trg_inventory_movements_guard
    BEFORE INSERT ON inventory_movements
    FOR EACH ROW EXECUTE FUNCTION fn_inventory_movements_guard();

-- Insert-only ledger (V002 function, SQLSTATE 23000)
CREATE TRIGGER trg_inventory_movements_insert_only
    BEFORE UPDATE OR DELETE ON inventory_movements
    FOR EACH ROW EXECUTE FUNCTION fn_reject_modification();

-- No bulk deletion of inventory history
CREATE TRIGGER trg_inventory_balances_no_truncate
    BEFORE TRUNCATE ON inventory_balances
    FOR EACH STATEMENT EXECUTE FUNCTION fn_reject_inventory_truncate();

CREATE TRIGGER trg_inventory_reservations_no_truncate
    BEFORE TRUNCATE ON inventory_reservations
    FOR EACH STATEMENT EXECUTE FUNCTION fn_reject_inventory_truncate();

CREATE TRIGGER trg_inventory_movements_no_truncate
    BEFORE TRUNCATE ON inventory_movements
    FOR EACH STATEMENT EXECUTE FUNCTION fn_reject_inventory_truncate();

-- Balance maintenance (AFTER; run after the FK checks, which fire first by name)
CREATE TRIGGER trg_inventory_movements_apply_to_balance
    AFTER INSERT ON inventory_movements
    FOR EACH ROW EXECUTE FUNCTION fn_inventory_apply_movement();

CREATE TRIGGER trg_inventory_reservations_apply_to_balance
    AFTER INSERT OR UPDATE OF status ON inventory_reservations
    FOR EACH ROW EXECUTE FUNCTION fn_inventory_apply_reservation();

-- Capture needs active reservations (new trigger on a V006 table)
CREATE TRIGGER trg_payment_attempts_inventory_guard
    BEFORE UPDATE OF status ON payment_attempts
    FOR EACH ROW
    WHEN (NEW.status = 'captured' AND OLD.status IS DISTINCT FROM 'captured')
    EXECUTE FUNCTION fn_payment_attempts_inventory_guard();

-- Deferred order-inventory check at commit
CREATE CONSTRAINT TRIGGER trg_orders_inventory_check
    AFTER UPDATE OF status ON orders
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_check_order_inventory();

CREATE CONSTRAINT TRIGGER trg_inventory_reservations_order_check
    AFTER INSERT OR UPDATE OF status ON inventory_reservations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_check_order_inventory();

CREATE CONSTRAINT TRIGGER trg_inventory_movements_order_check
    AFTER INSERT ON inventory_movements
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.movement_type = 'sale')
    EXECUTE FUNCTION fn_check_order_inventory();

-- updated_at maintenance (inventory_movements has no updated_at: insert-only)
CREATE TRIGGER trg_inventory_balances_set_updated_at
    BEFORE UPDATE ON inventory_balances
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE TRIGGER trg_inventory_reservations_set_updated_at
    BEFORE UPDATE ON inventory_reservations
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

-- change capture (argument = primary key column)
CREATE TRIGGER trg_inventory_balances_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON inventory_balances
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('inventory_balance_id');

CREATE TRIGGER trg_inventory_reservations_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON inventory_reservations
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('reservation_id');

CREATE TRIGGER trg_inventory_movements_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON inventory_movements
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('movement_id');

-- -----------------------------------------------------------------------------
-- Record this migration
-- -----------------------------------------------------------------------------
INSERT INTO meta.schema_migrations (version, description, script_name)
VALUES ('007', 'Inventory: inventory_balances, inventory_reservations, inventory_movements',
        'V007__inventory_balances_reservations_movements.sql');

COMMIT;
