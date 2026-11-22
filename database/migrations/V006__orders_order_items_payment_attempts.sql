-- =============================================================================
-- NordicCart OLTP | Migration V006
-- Orders and payments: orders, order_items, payment_attempts
-- -----------------------------------------------------------------------------
-- Design source : database/design/nordiccart_oltp_rev3.dbml (revision 3, approved)
-- Depends on    : V005 (and V003 to V005 for every referenced table)
-- Connect to    : nordiccart_oltp, as the database owner
-- Transaction   : the whole file is one transaction (see V001 header).
-- Re-runnable   : no. A second run stops at the guard below.
-- Revision      : 2 (2026-11-16), before first deployment. Adds historical
--                 integrity, fulfilment country and cancellation/payment
--                 consistency rules. Tables and columns are unchanged.
-- -----------------------------------------------------------------------------
-- Dependency order inside this file
--   customers, stores, stock_locations, countries, currencies -> orders
--   orders, products                                          -> order_items
--   orders                                                    -> payment_attempts
--   order_items.promotion_id has no FK yet: promotions arrive in DBML Phase 2.
--
-- Design items implemented
--   [DBML] enums order_status, payment_method, payment_status; columns,
--          defaults, PKs, UNIQUEs, FKs (including the composite in-store
--          fulfilment FK), indexes and every named CHECK of the three tables
--   [PG]   partial unique index: at most one live payment attempt per order
--   [PG]   order status transition trigger (allowed moves only)
--   [PG]   deferred order check at commit: items_total = SUM(line_total)
--   [PG]   order line guard: lines immutable after the order leaves placed;
--          vat_rate equals the order country rate
--   [PG]   payment guard: amount = order_total; cash only in store; status
--          transitions; capture only while the order is stock_reserved, with
--          the order row locked
--   [PG]   updated_at maintenance and change_log capture on all three tables
--
-- Rules from the business rules, the DBML notes and the pre-deployment review
-- (see database/README.md, "V006 additions"). Every rule has a name, raised in
-- the error's constraint field.
--   Lifecycle and totals
--     * an order is inserted as placed; a payment attempt as pending
--     * an order has at least one line (Business Rules 1.1)
--     * orders.currency_code is the currency of the order country (Business Rules 2)
--     * one VAT rate per order; shipping_vat_amount follows its documented formula
--     * amounts frozen once the order leaves placed
--     * order_items.promotion_id stays NULL until promotions exist (MVP note)
--   Historical integrity
--     * orders are never deleted or truncated; payment attempts and order lines
--       are never truncated, payment attempts never deleted
--     * order identity (order_id, order_number, customer_id, placed_at,
--       created_at) never changes; neither does its context (channel, store,
--       fulfilment location, country, currency)
--     * confirmed_at, fulfilled_at, cancelled_at: NULL until the status that
--       owns them, written once, in time order after placed_at
--     * fulfilled, payment_failed and cancelled orders are final: no change at all
--     * order line identity (order_item_id, order_id, created_at) never changes
--     * payment attempt terms (order, attempt number, method, provider, amount,
--       attempted_at) never change; outcome fields (authorized_at, captured_at,
--       failed_at, failure_reason) are written only by their own transition;
--       provider_reference is written once; captured, failed and voided
--       attempts are final; payment times are in order after placed_at
--   Fulfilment location
--     * the fulfilment location is in the order country
--     * an online order is fulfilled from a warehouse that ships online orders
--       (an in-store order from its own store: fk_orders_store_fulfilment)
--   Cancellation and payment state (checked at COMMIT)
--     * confirmed or fulfilled <=> exactly one captured attempt (Business Rules 1.6)
--     * payment_failed => at least one failed attempt, none pending or authorized
--     * cancelled => no authorized attempt: U4 voids it in the same transaction.
--       A pending attempt may remain (provider outcome unknown); when it later
--       authorizes, the same transaction must void it.
--     * cancellation_reason is set (out_of_stock, reservation_expired,
--       customer_request) exactly when the order is cancelled
--
-- How the units of work must write (application rules this migration relies on)
--   U1  INSERT orders (status placed) and its order_items in one transaction,
--       with items_total, shipping_vat_amount and order_total already computed
--       (Decimal, ROUND_HALF_UP). The deferred check runs at COMMIT.
--   U3a INSERT payment_attempts (pending) while the order is stock_reserved.
--   U3c Lock the order (SELECT ... FOR UPDATE), set the attempt authorized then
--       captured, THEN set the order confirmed, then COMMIT. Capture checks the
--       order is still stock_reserved, so confirm after capture, not before.
--   U4  In one transaction: order cancelled (reservation_expired) and every
--       authorized attempt voided.
--   Lock order everywhere: orders row first. The guards below lock the parent
--   order row FOR UPDATE themselves, so writers to the same order serialize.
--
-- Rules that wait for the inventory tables (planned V007), see database/README.md:
--   stock_reserved needs one active reservation per line; capture needs all
--   reservations active; payment_failed and cancelled need reservations
--   released or expired; fulfilled needs every reservation consumed and one
--   sale movement per line.
--
-- No rows are inserted.
-- =============================================================================

BEGIN;

SET LOCAL search_path TO public;
SET LOCAL lock_timeout = '10s';

SELECT meta.assert_migration_can_run('006', '005');

-- -----------------------------------------------------------------------------
-- Enums
-- -----------------------------------------------------------------------------
CREATE TYPE order_status AS ENUM (
    'placed',
    'stock_reserved',
    'confirmed',
    'fulfilled',
    'payment_failed',
    'cancelled'
);

COMMENT ON TYPE order_status IS
    'placed: captured, stock not yet reserved. stock_reserved: all lines reserved, awaiting a captured payment. confirmed: payment captured. fulfilled: stock issued. payment_failed (terminal): last attempt failed, reservations released. cancelled (terminal): out of stock, reservation expired or customer request.';

CREATE TYPE payment_method AS ENUM (
    'card',
    'mobile_payment',
    'buy_now_pay_later',
    'cash'
);

COMMENT ON TYPE payment_method IS
    'mobile_payment: Swish, Vipps MobilePay. buy_now_pay_later: invoice or instalments. cash: in-store orders only.';

CREATE TYPE payment_status AS ENUM (
    'pending',
    'authorized',
    'captured',
    'failed',
    'voided'
);

COMMENT ON TYPE payment_status IS
    'pending: provider result not known yet. authorized: captured or voided under the order lock. voided: authorization released because the order could no longer be confirmed.';

-- -----------------------------------------------------------------------------
-- orders
-- -----------------------------------------------------------------------------
CREATE TABLE orders (
    order_id                bigint         GENERATED ALWAYS AS IDENTITY,
    order_number            varchar(30)    NOT NULL,
    customer_id             bigint,
    channel                 sales_channel  NOT NULL,
    store_id                bigint,
    fulfilment_location_id  bigint         NOT NULL,
    country_code            char(2)        NOT NULL,
    currency_code           char(3)        NOT NULL,
    status                  order_status   NOT NULL DEFAULT 'placed',
    cancellation_reason     varchar(50),
    items_total             numeric(14,2)  NOT NULL DEFAULT 0,
    shipping_fee            numeric(12,2)  NOT NULL DEFAULT 0,
    shipping_vat_amount     numeric(12,2)  NOT NULL DEFAULT 0,
    order_total             numeric(14,2)  NOT NULL DEFAULT 0,
    shipping_address_line1  varchar(200),
    shipping_postal_code    varchar(10),
    shipping_city           varchar(100),
    placed_at               timestamptz    NOT NULL,
    confirmed_at            timestamptz,
    fulfilled_at            timestamptz,
    cancelled_at            timestamptz,
    created_at              timestamptz    NOT NULL DEFAULT now(),
    updated_at              timestamptz    NOT NULL DEFAULT now(),
    CONSTRAINT pk_orders PRIMARY KEY (order_id),
    CONSTRAINT uq_orders_order_number UNIQUE (order_number),
    CONSTRAINT fk_orders_customer
        FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    -- In-store orders are fulfilled from the selling store own stock location.
    -- MATCH SIMPLE: skipped for online orders, where store_id is NULL.
    CONSTRAINT fk_orders_store_fulfilment
        FOREIGN KEY (store_id, fulfilment_location_id)
        REFERENCES stores (store_id, stock_location_id),
    CONSTRAINT fk_orders_fulfilment_location
        FOREIGN KEY (fulfilment_location_id) REFERENCES stock_locations (stock_location_id),
    CONSTRAINT fk_orders_country
        FOREIGN KEY (country_code) REFERENCES countries (country_code),
    CONSTRAINT fk_orders_currency
        FOREIGN KEY (currency_code) REFERENCES currencies (currency_code),
    CONSTRAINT ck_orders_store_matches_channel
        CHECK ((channel = 'in_store') = (store_id IS NOT NULL)),
    CONSTRAINT ck_orders_online_customer
        CHECK (channel = 'in_store' OR customer_id IS NOT NULL),
    CONSTRAINT ck_orders_online_address
        CHECK (channel = 'in_store' OR (shipping_address_line1 IS NOT NULL
                                        AND shipping_postal_code IS NOT NULL
                                        AND shipping_city IS NOT NULL)),
    CONSTRAINT ck_orders_in_store_no_shipping
        CHECK (channel = 'online' OR shipping_fee = 0),
    CONSTRAINT ck_orders_amounts_non_negative
        CHECK (items_total >= 0 AND shipping_fee >= 0),
    CONSTRAINT ck_orders_shipping_vat
        CHECK (shipping_vat_amount >= 0 AND shipping_vat_amount <= shipping_fee),
    CONSTRAINT ck_orders_total
        CHECK (order_total = items_total + shipping_fee),
    CONSTRAINT ck_orders_cancelled_at
        CHECK (status NOT IN ('cancelled', 'payment_failed') OR cancelled_at IS NOT NULL),
    CONSTRAINT ck_orders_confirmed_at
        CHECK (status NOT IN ('confirmed', 'fulfilled') OR confirmed_at IS NOT NULL),
    CONSTRAINT ck_orders_fulfilled_at
        CHECK (status <> 'fulfilled' OR fulfilled_at IS NOT NULL)
);

CREATE INDEX ix_orders_customer_id ON orders (customer_id);
CREATE INDEX ix_orders_store_id ON orders (store_id);
CREATE INDEX ix_orders_fulfilment_location_id ON orders (fulfilment_location_id);
CREATE INDEX ix_orders_status_placed_at ON orders (status, placed_at);
CREATE INDEX ix_orders_placed_at ON orders (placed_at);
CREATE INDEX ix_orders_updated_at ON orders (updated_at);

COMMENT ON TABLE orders IS
    'Customer orders, online and in store. Lifecycle: placed -> stock_reserved -> confirmed -> fulfilled, with exits to payment_failed or cancelled. Every amount on the order and its child records is in currency_code, VAT-inclusive unless stated. A sale counts as revenue only when fulfilled, dated by fulfilled_at.';
COMMENT ON COLUMN orders.order_number IS
    'Stable business key, e.g. SE-2025-00012345';
COMMENT ON COLUMN orders.customer_id IS
    'Required for online orders. NULL allowed for anonymous in-store sales.';
COMMENT ON COLUMN orders.store_id IS
    'Selling store. Required for in_store, NULL for online.';
COMMENT ON COLUMN orders.fulfilment_location_id IS
    'Stock location that reserves and issues the stock';
COMMENT ON COLUMN orders.country_code IS
    'Sales market: drives price list, VAT and currency';
COMMENT ON COLUMN orders.currency_code IS
    'Currency of every amount on this order and its child records';
COMMENT ON COLUMN orders.cancellation_reason IS
    'out_of_stock, reservation_expired or customer_request';
COMMENT ON COLUMN orders.items_total IS
    'SUM(order_items.line_total), incl. VAT. Checked at commit.';
COMMENT ON COLUMN orders.shipping_fee IS
    'Incl. VAT';
COMMENT ON COLUMN orders.shipping_vat_amount IS
    'round(shipping_fee * vat_rate / (1 + vat_rate), 2) at the line VAT rate. Checked at commit.';
COMMENT ON COLUMN orders.order_total IS
    'items_total + shipping_fee, the amount to pay';
COMMENT ON COLUMN orders.fulfilled_at IS
    'Revenue recognition time. Picked and packed (online) or handed over (in store).';
COMMENT ON COLUMN orders.cancelled_at IS
    'Also set for payment_failed';

-- -----------------------------------------------------------------------------
-- order_items (purchase-time snapshot per line)
-- -----------------------------------------------------------------------------
CREATE TABLE order_items (
    order_item_id     bigint         GENERATED ALWAYS AS IDENTITY,
    order_id          bigint         NOT NULL,
    line_number       smallint       NOT NULL,
    product_id        bigint         NOT NULL,
    promotion_id      bigint,
    quantity          integer        NOT NULL,
    unit_price        numeric(12,2)  NOT NULL,
    unit_cost         numeric(12,2)  NOT NULL,
    discount_percent  numeric(5,2)   NOT NULL DEFAULT 0,
    discount_amount   numeric(12,2)  NOT NULL DEFAULT 0,
    line_total        numeric(14,2)  NOT NULL,
    vat_rate          numeric(5,4)   NOT NULL,
    vat_amount        numeric(12,2)  NOT NULL,
    created_at        timestamptz    NOT NULL DEFAULT now(),
    updated_at        timestamptz    NOT NULL DEFAULT now(),
    CONSTRAINT pk_order_items PRIMARY KEY (order_item_id),
    CONSTRAINT uq_order_items_order_line UNIQUE (order_id, line_number),
    CONSTRAINT uq_order_items_order_product UNIQUE (order_id, product_id),
    CONSTRAINT fk_order_items_order
        FOREIGN KEY (order_id) REFERENCES orders (order_id),
    CONSTRAINT fk_order_items_product
        FOREIGN KEY (product_id) REFERENCES products (product_id),
    -- fk_order_items_promotion is added with the promotions table (DBML Phase 2).
    CONSTRAINT ck_order_items_quantity
        CHECK (quantity > 0),
    CONSTRAINT ck_order_items_price_cost
        CHECK (unit_price >= 0 AND unit_cost >= 0),
    CONSTRAINT ck_order_items_discount_percent
        CHECK (discount_percent >= 0 AND discount_percent < 100),
    CONSTRAINT ck_order_items_promotion_discount
        CHECK ((promotion_id IS NULL) = (discount_percent = 0)),
    CONSTRAINT ck_order_items_discount_rounding
        CHECK (discount_amount = round(quantity * unit_price * discount_percent / 100, 2)),
    CONSTRAINT ck_order_items_line_total
        CHECK (line_total = quantity * unit_price - discount_amount),
    CONSTRAINT ck_order_items_vat_rate
        CHECK (vat_rate >= 0 AND vat_rate < 1),
    CONSTRAINT ck_order_items_vat_rounding
        CHECK (vat_amount = round(line_total * vat_rate / (1 + vat_rate), 2))
);

CREATE INDEX ix_order_items_product_id ON order_items (product_id);
CREATE INDEX ix_order_items_promotion_id ON order_items (promotion_id);
CREATE INDEX ix_order_items_updated_at ON order_items (updated_at);

COMMENT ON TABLE order_items IS
    'Order lines. Purchase-time snapshot: price, cost, discount percentage and VAT rate are copied when the order is placed and never recalculated. Immutable once the order leaves placed. Derived per line (not stored): net_revenue_excl_vat = line_total - vat_amount; cogs = quantity * unit_cost; gross_profit = net_revenue_excl_vat - cogs.';
COMMENT ON COLUMN order_items.promotion_id IS
    'Phase 2; NULL in the MVP';
COMMENT ON COLUMN order_items.unit_price IS
    'Regular unit price incl. VAT at purchase time, copied from product_prices';
COMMENT ON COLUMN order_items.unit_cost IS
    'Unit cost excl. VAT at purchase time, copied from product_prices';
COMMENT ON COLUMN order_items.discount_percent IS
    'Percentage applied, copied from the promotion; 0 without promotion';
COMMENT ON COLUMN order_items.discount_amount IS
    'round(quantity * unit_price * discount_percent / 100, 2)';
COMMENT ON COLUMN order_items.line_total IS
    'Net sales incl. VAT = quantity * unit_price - discount_amount';
COMMENT ON COLUMN order_items.vat_amount IS
    'round(line_total * vat_rate / (1 + vat_rate), 2)';

-- -----------------------------------------------------------------------------
-- payment_attempts (one row per attempt, unit of work U3)
-- -----------------------------------------------------------------------------
CREATE TABLE payment_attempts (
    payment_attempt_id  bigint          GENERATED ALWAYS AS IDENTITY,
    order_id            bigint          NOT NULL,
    attempt_number      smallint        NOT NULL,
    payment_method      payment_method  NOT NULL,
    payment_provider    varchar(50)     NOT NULL,
    amount              numeric(14,2)   NOT NULL,
    status              payment_status  NOT NULL DEFAULT 'pending',
    failure_reason      varchar(50),
    provider_reference  varchar(64),
    attempted_at        timestamptz     NOT NULL,
    authorized_at       timestamptz,
    captured_at         timestamptz,
    failed_at           timestamptz,
    created_at          timestamptz     NOT NULL DEFAULT now(),
    updated_at          timestamptz     NOT NULL DEFAULT now(),
    CONSTRAINT pk_payment_attempts PRIMARY KEY (payment_attempt_id),
    CONSTRAINT uq_payment_attempts_order_attempt UNIQUE (order_id, attempt_number),
    CONSTRAINT uq_payment_attempts_provider_reference UNIQUE (provider_reference),
    CONSTRAINT fk_payment_attempts_order
        FOREIGN KEY (order_id) REFERENCES orders (order_id),
    CONSTRAINT ck_payment_attempts_attempt_number
        CHECK (attempt_number > 0),
    CONSTRAINT ck_payment_attempts_amount
        CHECK (amount > 0),
    CONSTRAINT ck_payment_attempts_failed
        CHECK (status <> 'failed' OR (failed_at IS NOT NULL AND failure_reason IS NOT NULL)),
    CONSTRAINT ck_payment_attempts_authorized_at
        CHECK (status NOT IN ('authorized', 'captured', 'voided') OR authorized_at IS NOT NULL),
    CONSTRAINT ck_payment_attempts_captured_at
        CHECK (status <> 'captured' OR captured_at IS NOT NULL)
);

CREATE INDEX ix_payment_attempts_status_attempted_at ON payment_attempts (status, attempted_at);
CREATE INDEX ix_payment_attempts_updated_at ON payment_attempts (updated_at);

-- [PG] At most one live attempt per order: a new attempt is possible only
-- after a failure or void, and an order can never be captured twice.
CREATE UNIQUE INDEX uq_payment_attempts_one_live_per_order
    ON payment_attempts (order_id)
    WHERE status IN ('pending', 'authorized', 'captured');

COMMENT ON TABLE payment_attempts IS
    'One row per payment attempt (unit of work U3). Idempotency key sent to the provider: order_number-attempt_number. At most one live (pending, authorized or captured) attempt per order. Maximum 3 attempts per order is an application rule.';
COMMENT ON COLUMN payment_attempts.payment_provider IS
    'Synthetic payment service provider';
COMMENT ON COLUMN payment_attempts.amount IS
    'In orders.currency_code, equals order_total';
COMMENT ON COLUMN payment_attempts.failure_reason IS
    'card_declined, insufficient_funds, timeout';
COMMENT ON COLUMN payment_attempts.provider_reference IS
    'Provider transaction id';

-- -----------------------------------------------------------------------------
-- [PG] Guard functions (BEFORE row triggers)
-- Business rules raise SQLSTATE 23514 (check_violation), a business rejection
-- that the design's retry rules never retry, and set the CONSTRAINT field to a
-- rule name the application can read.
-- -----------------------------------------------------------------------------

-- orders: no delete; insert as placed in the currency of the country, from a
-- fulfilment location in that country (a shipping warehouse for online orders);
-- identity and context never change; allowed status moves only; final states
-- frozen; amounts frozen after placed; event timestamps by status, written
-- once, in time order; cancellation_reason exactly when cancelled.
CREATE FUNCTION fn_orders_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_country_currency  char(3);
    v_location_country  char(2);
    v_location_type     public.stock_location_type;
    v_ships_online      boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Order % cannot be deleted: orders are history. Cancel it instead.', OLD.order_number
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_no_delete';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'placed' THEN
            RAISE EXCEPTION 'Order % must be inserted with status placed, not %.', NEW.order_number, NEW.status
                USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_starts_placed';
        END IF;

        -- Business Rules 2: transactions use the currency of the country of sale.
        SELECT c.currency_code INTO v_country_currency
          FROM public.countries AS c
         WHERE c.country_code = NEW.country_code;

        IF FOUND AND NEW.currency_code <> v_country_currency THEN
            RAISE EXCEPTION 'Order % is in %, but the currency of % is %.',
                NEW.order_number, NEW.currency_code, NEW.country_code, v_country_currency
                USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_currency_matches_country';
        END IF;

        -- Fulfilment location: same country; online orders ship from a
        -- warehouse that ships online orders (DBML: "Online orders are
        -- fulfilled from the warehouse in their country").
        SELECT sl.country_code, sl.location_type, w.ships_online_orders
          INTO v_location_country, v_location_type, v_ships_online
          FROM public.stock_locations AS sl
          LEFT JOIN public.warehouses AS w ON w.stock_location_id = sl.stock_location_id
         WHERE sl.stock_location_id = NEW.fulfilment_location_id;

        IF FOUND THEN
            IF v_location_country <> NEW.country_code THEN
                RAISE EXCEPTION 'Order % is sold in %, but its fulfilment location is in %.',
                    NEW.order_number, NEW.country_code, v_location_country
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_fulfilment_country';
            END IF;
            IF NEW.channel = 'online' AND (v_location_type <> 'warehouse' OR v_ships_online IS NOT TRUE) THEN
                RAISE EXCEPTION 'Online order % must be fulfilled from a warehouse that ships online orders.', NEW.order_number
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_online_fulfilment_warehouse';
            END IF;
        END IF;
    ELSE
        -- Identity of the purchase never changes.
        IF (NEW.order_id, NEW.order_number, NEW.customer_id, NEW.placed_at, NEW.created_at)
           IS DISTINCT FROM
           (OLD.order_id, OLD.order_number, OLD.customer_id, OLD.placed_at, OLD.created_at) THEN
            RAISE EXCEPTION 'Order %: order_id, order_number, customer_id, placed_at and created_at never change.', OLD.order_number
                USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_identity_immutable';
        END IF;

        -- Context of the sale never changes (VAT, currency and stock depend on it).
        IF (NEW.channel, NEW.store_id, NEW.fulfilment_location_id, NEW.country_code, NEW.currency_code)
           IS DISTINCT FROM
           (OLD.channel, OLD.store_id, OLD.fulfilment_location_id, OLD.country_code, OLD.currency_code) THEN
            RAISE EXCEPTION 'Order %: channel, store, fulfilment location, country and currency never change.', OLD.order_number
                USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_context_immutable';
        END IF;

        -- Allowed moves only (DBML). confirmed -> cancelled needs refunds (DBML Phase 4).
        IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
               (OLD.status = 'placed'         AND NEW.status IN ('stock_reserved', 'cancelled'))
            OR (OLD.status = 'stock_reserved' AND NEW.status IN ('confirmed', 'payment_failed', 'cancelled'))
            OR (OLD.status = 'confirmed'      AND NEW.status = 'fulfilled')
        ) THEN
            RAISE EXCEPTION 'Order % cannot move from % to %.', OLD.order_number, OLD.status, NEW.status
                USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_status_transition',
                      HINT = 'Allowed: placed -> stock_reserved or cancelled; stock_reserved -> confirmed, payment_failed or cancelled; confirmed -> fulfilled.';
        END IF;

        -- Final states are history: nothing on the row changes any more.
        IF OLD.status IN ('fulfilled', 'payment_failed', 'cancelled') THEN
            RAISE EXCEPTION 'Order % is % and final: it can no longer be changed.', OLD.order_number, OLD.status
                USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_final_immutable';
        END IF;

        -- Lines are immutable after placed (DBML), so the amount owed must be too.
        IF OLD.status <> 'placed' AND
           (NEW.items_total, NEW.shipping_fee, NEW.shipping_vat_amount, NEW.order_total)
           IS DISTINCT FROM
           (OLD.items_total, OLD.shipping_fee, OLD.shipping_vat_amount, OLD.order_total) THEN
            RAISE EXCEPTION 'Order % has left placed: its amounts can no longer change.', OLD.order_number
                USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_terms_frozen';
        END IF;

        -- Event timestamps are written once.
        IF    (OLD.confirmed_at IS NOT NULL AND NEW.confirmed_at IS DISTINCT FROM OLD.confirmed_at)
           OR (OLD.fulfilled_at IS NOT NULL AND NEW.fulfilled_at IS DISTINCT FROM OLD.fulfilled_at)
           OR (OLD.cancelled_at IS NOT NULL AND NEW.cancelled_at IS DISTINCT FROM OLD.cancelled_at) THEN
            RAISE EXCEPTION 'Order %: confirmed_at, fulfilled_at and cancelled_at are written once and never changed.', OLD.order_number
                USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_event_timestamp_changed';
        END IF;
    END IF;

    -- Event timestamps belong to their status (the CHECKs require them there).
    IF    (NEW.confirmed_at IS NOT NULL AND NEW.status NOT IN ('confirmed', 'fulfilled'))
       OR (NEW.fulfilled_at IS NOT NULL AND NEW.status <> 'fulfilled')
       OR (NEW.cancelled_at IS NOT NULL AND NEW.status NOT IN ('cancelled', 'payment_failed')) THEN
        RAISE EXCEPTION 'Order % is %: confirmed_at, fulfilled_at and cancelled_at are set only when the order reaches that status.',
            NEW.order_number, NEW.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_event_timestamp_state';
    END IF;

    -- Business events happen in time order.
    IF    coalesce(NEW.confirmed_at < NEW.placed_at, false)
       OR coalesce(NEW.fulfilled_at < NEW.confirmed_at, false)
       OR coalesce(NEW.cancelled_at < NEW.placed_at, false) THEN
        RAISE EXCEPTION 'Order %: confirmed_at and cancelled_at must not precede placed_at, nor fulfilled_at precede confirmed_at.', NEW.order_number
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_event_timestamp_order';
    END IF;

    -- cancellation_reason (DBML: out_of_stock, reservation_expired or
    -- customer_request) is present exactly when the order is cancelled.
    IF (NEW.status = 'cancelled'
        AND (NEW.cancellation_reason IS NULL
             OR NEW.cancellation_reason NOT IN ('out_of_stock', 'reservation_expired', 'customer_request')))
       OR (NEW.status <> 'cancelled' AND NEW.cancellation_reason IS NOT NULL) THEN
        RAISE EXCEPTION 'Order % is %: cancellation_reason must be out_of_stock, reservation_expired or customer_request when cancelled, and empty otherwise (got %).',
            NEW.order_number, NEW.status, coalesce(NEW.cancellation_reason, 'NULL')
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_cancellation_reason';
    END IF;

    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION fn_orders_guard() IS
    'BEFORE INSERT OR UPDATE OR DELETE on orders: no delete; insert as placed, in the country currency, from a fulfilment location in the order country (online: a warehouse that ships online orders); identity and context never change; allowed status moves only; final states frozen; amounts frozen after placed; event timestamps by status, written once, in time order; cancellation_reason exactly when cancelled.';

-- order_items: identity never changes; lock the parent order; lines change
-- only while it is placed; VAT rate of the order country; promotion_id NULL in the MVP.
CREATE FUNCTION fn_order_items_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_order_id      bigint;
    v_status        public.order_status;
    v_order_number  varchar(30);
    v_country_rate  numeric(5,4);
BEGIN
    IF TG_OP = 'UPDATE'
       AND (NEW.order_item_id, NEW.order_id, NEW.created_at)
           IS DISTINCT FROM (OLD.order_item_id, OLD.order_id, OLD.created_at) THEN
        RAISE EXCEPTION 'Order line %: order_item_id, order_id and created_at never change.', OLD.order_item_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'order_items_identity_immutable';
    END IF;

    IF TG_OP = 'INSERT' THEN
        v_order_id := NEW.order_id;
    ELSE
        v_order_id := OLD.order_id;
    END IF;

    -- Lock the parent order first (design lock order), then require placed.
    SELECT o.status, o.order_number INTO v_status, v_order_number
      FROM public.orders AS o
     WHERE o.order_id = v_order_id
       FOR UPDATE;

    IF FOUND AND v_status <> 'placed' THEN
        RAISE EXCEPTION 'Order % is %: its lines are immutable once the order has left placed.', v_order_number, v_status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'order_items_immutable_after_placed';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    -- MVP: promotions do not exist yet. DBML Phase 2 adds the FK and replaces
    -- this rule with "discount_percent equals the promotion percentage".
    IF NEW.promotion_id IS NOT NULL THEN
        RAISE EXCEPTION 'order_items.promotion_id must be NULL until promotions are implemented.'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'order_items_promotion_mvp';
    END IF;

    -- VAT rate snapshot = the order country standard rate. The database holds
    -- only the current rate, so the check uses countries.vat_rate when written.
    IF TG_OP = 'INSERT' OR NEW.vat_rate IS DISTINCT FROM OLD.vat_rate THEN
        SELECT c.vat_rate INTO v_country_rate
          FROM public.orders AS o
          JOIN public.countries AS c ON c.country_code = o.country_code
         WHERE o.order_id = NEW.order_id;

        IF FOUND AND NEW.vat_rate <> v_country_rate THEN
            RAISE EXCEPTION 'Line VAT rate % differs from the order country rate %.', NEW.vat_rate, v_country_rate
                USING ERRCODE = 'check_violation', CONSTRAINT = 'order_items_vat_rate_matches_country';
        END IF;
    END IF;

    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION fn_order_items_guard() IS
    'BEFORE INSERT OR UPDATE OR DELETE on order_items: order_item_id, order_id and created_at never change; locks the parent order FOR UPDATE; lines change only while the order is placed; vat_rate equals the order country rate; promotion_id NULL until promotions exist.';

-- payment_attempts: never deleted; terms never change; insert as pending for a
-- stock_reserved order; status transitions; final attempts frozen; outcome
-- fields only by their own transition; provider_reference written once;
-- capture only while stock_reserved; times in order; amount = order_total;
-- cash only in store.
CREATE FUNCTION fn_payment_attempts_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_order  record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Payment attempt % of order id % cannot be deleted: payment records are history.',
            OLD.attempt_number, OLD.order_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_no_delete';
    END IF;

    IF TG_OP = 'UPDATE' AND
       (NEW.payment_attempt_id, NEW.order_id, NEW.attempt_number, NEW.payment_method,
        NEW.payment_provider, NEW.amount, NEW.attempted_at, NEW.created_at)
       IS DISTINCT FROM
       (OLD.payment_attempt_id, OLD.order_id, OLD.attempt_number, OLD.payment_method,
        OLD.payment_provider, OLD.amount, OLD.attempted_at, OLD.created_at) THEN
        RAISE EXCEPTION 'Payment attempt % of order id %: order, attempt number, method, provider, amount and attempted_at never change.',
            OLD.attempt_number, OLD.order_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_terms_immutable';
    END IF;

    SELECT o.order_number, o.status, o.channel, o.order_total, o.placed_at
      INTO v_order
      FROM public.orders AS o
     WHERE o.order_id = NEW.order_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN NEW;  -- fk_payment_attempts_order rejects it with 23503
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' THEN
            RAISE EXCEPTION 'Payment attempt % of order % must be inserted as pending, not %.',
                NEW.attempt_number, v_order.order_number, NEW.status
                USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_starts_pending';
        END IF;
        IF v_order.status <> 'stock_reserved' THEN
            RAISE EXCEPTION 'Order % is %: payment attempts are allowed only while it is stock_reserved.',
                v_order.order_number, v_order.status
                USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_order_stock_reserved';
        END IF;
        IF NEW.authorized_at IS NOT NULL OR NEW.captured_at IS NOT NULL
           OR NEW.failed_at IS NOT NULL OR NEW.failure_reason IS NOT NULL THEN
            RAISE EXCEPTION 'Payment attempt % of order %: a new attempt has no outcome yet.',
                NEW.attempt_number, v_order.order_number
                USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_outcome_by_transition';
        END IF;
        IF NEW.amount <> v_order.order_total THEN
            RAISE EXCEPTION 'Payment amount % differs from the total % of order %.',
                NEW.amount, v_order.order_total, v_order.order_number
                USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_amount_equals_order_total';
        END IF;
        IF NEW.payment_method = 'cash' AND v_order.channel <> 'in_store' THEN
            RAISE EXCEPTION 'Order % is %: cash is accepted only for in-store orders.', v_order.order_number, v_order.channel
                USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_cash_in_store_only';
        END IF;
    ELSE
        IF NEW.status IS DISTINCT FROM OLD.status
           AND NOT (   (OLD.status = 'pending'    AND NEW.status IN ('authorized', 'failed'))
                    OR (OLD.status = 'authorized' AND NEW.status IN ('captured', 'voided'))) THEN
            RAISE EXCEPTION 'Payment attempt % of order % cannot move from % to %.',
                OLD.attempt_number, v_order.order_number, OLD.status, NEW.status
                USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_status_transition',
                      HINT = 'Allowed: pending -> authorized or failed; authorized -> captured or voided.';
        END IF;

        -- Completed payment records are history.
        IF OLD.status IN ('captured', 'failed', 'voided') THEN
            RAISE EXCEPTION 'Payment attempt % of order % is % and final: it can no longer be changed.',
                OLD.attempt_number, v_order.order_number, OLD.status
                USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_final_immutable';
        END IF;

        -- Outcome fields are written only by the transition that produces them.
        IF    (NEW.authorized_at IS DISTINCT FROM OLD.authorized_at
               AND NOT (OLD.status = 'pending' AND NEW.status = 'authorized'))
           OR (NEW.captured_at IS DISTINCT FROM OLD.captured_at
               AND NOT (OLD.status = 'authorized' AND NEW.status = 'captured'))
           OR ((NEW.failed_at, NEW.failure_reason) IS DISTINCT FROM (OLD.failed_at, OLD.failure_reason)
               AND NOT (OLD.status = 'pending' AND NEW.status = 'failed')) THEN
            RAISE EXCEPTION 'Payment attempt % of order %: authorized_at, captured_at, failed_at and failure_reason are written only by their own status change.',
                OLD.attempt_number, v_order.order_number
                USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_outcome_by_transition';
        END IF;

        IF OLD.provider_reference IS NOT NULL
           AND NEW.provider_reference IS DISTINCT FROM OLD.provider_reference THEN
            RAISE EXCEPTION 'Payment attempt % of order %: provider_reference is written once.',
                OLD.attempt_number, v_order.order_number
                USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_provider_reference_write_once';
        END IF;

        -- Capture while the order is stock_reserved (row locked above). The
        -- "all reservations active" half of this rule arrives with V007.
        IF NEW.status = 'captured' AND v_order.status <> 'stock_reserved' THEN
            RAISE EXCEPTION 'Order % is %: a payment can be captured only while the order is stock_reserved.',
                v_order.order_number, v_order.status
                USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_capture_requires_reserved_order',
                      HINT = 'Void the authorization instead. In U3(c) capture the attempt first, then confirm the order.';
        END IF;
    END IF;

    -- Payment events happen in time order, after the order was placed.
    IF    coalesce(NEW.attempted_at  < v_order.placed_at, false)
       OR coalesce(NEW.authorized_at < NEW.attempted_at, false)
       OR coalesce(NEW.captured_at   < NEW.authorized_at, false)
       OR coalesce(NEW.failed_at     < NEW.attempted_at, false) THEN
        RAISE EXCEPTION 'Payment attempt % of order %: times must follow placed_at <= attempted_at <= authorized_at <= captured_at, and failed_at >= attempted_at.',
            NEW.attempt_number, v_order.order_number
            USING ERRCODE = 'check_violation', CONSTRAINT = 'payment_attempts_timestamp_order';
    END IF;

    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION fn_payment_attempts_guard() IS
    'BEFORE INSERT OR UPDATE OR DELETE on payment_attempts: no delete; terms never change; locks the order FOR UPDATE; insert as pending, without outcome, for a stock_reserved order; pending -> authorized or failed, authorized -> captured or voided; captured, failed and voided are final; outcome fields only by their own transition; provider_reference written once; capture only while stock_reserved; times in order; amount = order_total; cash only for in-store orders.';

-- Order and payment history is never bulk-deleted. TRUNCATE skips row
-- triggers, so it gets its own statement-level guard.
CREATE FUNCTION fn_reject_history_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    RAISE EXCEPTION 'TRUNCATE on %.% is not allowed: order and payment history is never bulk-deleted.',
        TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = '23000', CONSTRAINT = 'history_no_truncate';
END
$fn$;

COMMENT ON FUNCTION fn_reject_history_truncate() IS
    'BEFORE TRUNCATE statement trigger on orders, order_items and payment_attempts: raises SQLSTATE 23000.';

-- -----------------------------------------------------------------------------
-- [PG] Deferred order consistency check (fires at COMMIT)
-- -----------------------------------------------------------------------------
CREATE FUNCTION fn_assert_order_consistent(p_order_id bigint)
RETURNS void
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_order       record;
    v_line_count  bigint;
    v_line_sum    numeric;
    v_rate_min    numeric;
    v_rate_max    numeric;
    v_captured    bigint;
    v_failed      bigint;
    v_pending     bigint;
    v_authorized  bigint;
BEGIN
    SELECT o.order_number, o.status, o.items_total, o.shipping_fee, o.shipping_vat_amount
      INTO v_order
      FROM public.orders AS o
     WHERE o.order_id = p_order_id;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT count(*), coalesce(sum(i.line_total), 0), min(i.vat_rate), max(i.vat_rate)
      INTO v_line_count, v_line_sum, v_rate_min, v_rate_max
      FROM public.order_items AS i
     WHERE i.order_id = p_order_id;

    -- Business Rules 1.1: an order contains one or more products.
    IF v_line_count = 0 THEN
        RAISE EXCEPTION 'Order % has no lines.', v_order.order_number
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_has_lines';
    END IF;

    -- DBML [PG]: at commit items_total = SUM(order_items.line_total).
    IF v_order.items_total <> v_line_sum THEN
        RAISE EXCEPTION 'Order % has items_total %, but its lines add up to %.',
            v_order.order_number, v_order.items_total, v_line_sum
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_items_total';
    END IF;

    -- Shipping VAT uses "the line VAT rate", so all lines share one rate.
    IF v_rate_min <> v_rate_max THEN
        RAISE EXCEPTION 'Order % has lines with different VAT rates (% and %).',
            v_order.order_number, v_rate_min, v_rate_max
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_single_vat_rate';
    END IF;

    IF v_order.shipping_vat_amount <> round(v_order.shipping_fee * v_rate_min / (1 + v_rate_min), 2) THEN
        RAISE EXCEPTION 'Order % has shipping_vat_amount %, expected %.',
            v_order.order_number, v_order.shipping_vat_amount,
            round(v_order.shipping_fee * v_rate_min / (1 + v_rate_min), 2)
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_shipping_vat';
    END IF;

    SELECT count(*) FILTER (WHERE p.status = 'captured'),
           count(*) FILTER (WHERE p.status = 'failed'),
           count(*) FILTER (WHERE p.status = 'pending'),
           count(*) FILTER (WHERE p.status = 'authorized')
      INTO v_captured, v_failed, v_pending, v_authorized
      FROM public.payment_attempts AS p
     WHERE p.order_id = p_order_id;

    -- Business Rules 1.6: successful payment confirms the order, and only then.
    IF v_order.status IN ('confirmed', 'fulfilled') AND v_captured <> 1 THEN
        RAISE EXCEPTION 'Order % is % but has % captured payment attempts (expected 1).',
            v_order.order_number, v_order.status, v_captured
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_confirmed_has_capture';
    END IF;

    IF v_order.status NOT IN ('confirmed', 'fulfilled') AND v_captured > 0 THEN
        RAISE EXCEPTION 'Order % has a captured payment but is %; confirm it in the same transaction.',
            v_order.order_number, v_order.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_capture_confirms_order';
    END IF;

    -- Business Rules 1.5: payment_failed follows a failed attempt, with no attempt still open.
    IF v_order.status = 'payment_failed' AND (v_failed = 0 OR v_pending + v_authorized > 0) THEN
        RAISE EXCEPTION 'Order % is payment_failed but has % failed and % pending or authorized attempts.',
            v_order.order_number, v_failed, v_pending + v_authorized
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_payment_failed_state';
    END IF;

    -- U4: a cancelled order keeps no authorization; it is voided in the same
    -- transaction. A pending attempt may remain (outcome unknown); if it later
    -- authorizes, that transaction must void it before COMMIT.
    IF v_order.status = 'cancelled' AND v_authorized > 0 THEN
        RAISE EXCEPTION 'Order % is cancelled but still has % authorized payment attempt(s); void them in the same transaction.',
            v_order.order_number, v_authorized
            USING ERRCODE = 'check_violation', CONSTRAINT = 'orders_cancelled_open_authorization';
    END IF;
END
$fn$;

COMMENT ON FUNCTION fn_assert_order_consistent(bigint) IS
    'Order consistency rules checked at commit: at least one line; items_total = SUM(line_total); one VAT rate per order and shipping VAT by formula; confirmed or fulfilled <=> exactly one captured attempt; payment_failed => a failed attempt and none pending or authorized; cancelled => no authorized attempt.';

CREATE FUNCTION fn_check_order_consistency()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM public.fn_assert_order_consistent(OLD.order_id);
    ELSE
        PERFORM public.fn_assert_order_consistent(NEW.order_id);
    END IF;
    RETURN NULL;
END
$fn$;

COMMENT ON FUNCTION fn_check_order_consistency() IS
    'Deferred constraint trigger function on orders, order_items and payment_attempts: runs fn_assert_order_consistent for the affected order at commit.';

-- -----------------------------------------------------------------------------
-- Triggers
-- -----------------------------------------------------------------------------
-- Guards. BEFORE triggers on one table fire in name order, so *_guard runs
-- before *_set_updated_at.
CREATE TRIGGER trg_orders_guard
    BEFORE INSERT OR UPDATE OR DELETE ON orders
    FOR EACH ROW EXECUTE FUNCTION fn_orders_guard();

CREATE TRIGGER trg_order_items_guard
    BEFORE INSERT OR UPDATE OR DELETE ON order_items
    FOR EACH ROW EXECUTE FUNCTION fn_order_items_guard();

CREATE TRIGGER trg_payment_attempts_guard
    BEFORE INSERT OR UPDATE OR DELETE ON payment_attempts
    FOR EACH ROW EXECUTE FUNCTION fn_payment_attempts_guard();

-- No bulk deletion of order and payment history
CREATE TRIGGER trg_orders_no_truncate
    BEFORE TRUNCATE ON orders
    FOR EACH STATEMENT EXECUTE FUNCTION fn_reject_history_truncate();

CREATE TRIGGER trg_order_items_no_truncate
    BEFORE TRUNCATE ON order_items
    FOR EACH STATEMENT EXECUTE FUNCTION fn_reject_history_truncate();

CREATE TRIGGER trg_payment_attempts_no_truncate
    BEFORE TRUNCATE ON payment_attempts
    FOR EACH STATEMENT EXECUTE FUNCTION fn_reject_history_truncate();

-- Deferred consistency check at commit
CREATE CONSTRAINT TRIGGER trg_orders_consistency_check
    AFTER INSERT OR UPDATE OF status, items_total, shipping_fee, shipping_vat_amount ON orders
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_check_order_consistency();

CREATE CONSTRAINT TRIGGER trg_order_items_consistency_check
    AFTER INSERT OR UPDATE OF line_total, vat_rate OR DELETE ON order_items
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_check_order_consistency();

CREATE CONSTRAINT TRIGGER trg_payment_attempts_consistency_check
    AFTER INSERT OR UPDATE OF status ON payment_attempts
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_check_order_consistency();

-- updated_at maintenance
CREATE TRIGGER trg_orders_set_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE TRIGGER trg_order_items_set_updated_at
    BEFORE UPDATE ON order_items
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE TRIGGER trg_payment_attempts_set_updated_at
    BEFORE UPDATE ON payment_attempts
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

-- change capture (argument = primary key column)
CREATE TRIGGER trg_orders_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON orders
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('order_id');

CREATE TRIGGER trg_order_items_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON order_items
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('order_item_id');

CREATE TRIGGER trg_payment_attempts_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON payment_attempts
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('payment_attempt_id');

-- -----------------------------------------------------------------------------
-- Record this migration
-- -----------------------------------------------------------------------------
INSERT INTO meta.schema_migrations (version, description, script_name)
VALUES ('006', 'Orders and payments: orders, order_items, payment_attempts',
        'V006__orders_order_items_payment_attempts.sql');

COMMIT;
