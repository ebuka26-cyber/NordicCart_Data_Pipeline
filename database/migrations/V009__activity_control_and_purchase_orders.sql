-- =============================================================================
-- NordicCart OLTP | Migration V009
-- Activity control and purchase orders: purchase_orders, purchase_order_lines,
-- reorder_point_revisions, meta.activity_runs, meta.activity_checkpoints
-- -----------------------------------------------------------------------------
-- Design source : docs/phase3_combined_activity_design.md, revision 2: limitations
--                 L1 (purchase orders), L2 (run ledger and checkpoints, sections 7.2
--                 and 7.3), L10 and decision C18 (reorder point revision history),
--                 exception E3 and decision C15 (pilot purchase order documents),
--                 stage S4. DBML: database/design/nordiccart_oltp_rev4.dbml
-- Depends on    : V008 (roles nordiccart_owner, _writer, _reader)
-- Connect to    : nordiccart_oltp, as a SUPERUSER session (for example "postgres").
--                 The file switches to SET ROLE nordiccart_owner (design 4.1: from
--                 V009 on, migrations run as postgres with SET ROLE nordiccart_owner),
--                 so every new object is owned by nordiccart_owner and V008's default
--                 privileges apply (SELECT for nordiccart_reader, no EXECUTE for PUBLIC).
-- Transaction   : the whole file is one transaction. The last block re-checks the
--                 security rules; any failure rolls back everything.
-- Re-runnable   : no. A second run stops at the guard.
-- Rollback      : database/rollback/R009__undo_activity_control_and_purchase_orders.sql
-- -----------------------------------------------------------------------------
-- What it adds (no existing table, column, constraint or function is changed;
-- V001 to V008 files are unchanged; no row is written except V009's own
-- migration row: the pilot documents are loaded afterwards by the replenishment
-- loader, exception E3)
--   1. purchase_orders / purchase_order_lines (L1)
--        * one order per warehouse, number PO-<market>-<year>-<seq> of the
--          warehouse's market and the local year of ordered_at;
--        * lines: product listed at that warehouse, reason (reorder_point, launch,
--          seasonal, forced_reorder), quantity ordered, quantity received,
--          quantity outstanding (generated), status;
--        * received quantities and statuses are DERIVED from the stock ledger:
--          a purchase_receipt whose external_reference is <PO number>/DN<n> is
--          checked against the order (same warehouse, a line for the product,
--          not before the order, never more than ordered) and moves the line and
--          order status (SECURITY DEFINER trigger; writers have no UPDATE privilege
--          on these tables). Lines added after their receipts (the E3 backfill)
--          take the received quantity from the ledger. No movement is ever
--          written or changed here.
--        * receipts with other references (allowed by V007, used by its tests)
--          are left as V007 accepts them; verification script 18 reports any
--          purchase receipt that does not map to an order line.
--   2. reorder_point_revisions (L10, C18): durable, timestamped, insert-only
--      history of reorder point changes (business time, old and new value, kind
--      pilot_r0 or monthly_review, document reference). Revisions of a listing
--      form a chain in business-time order (old = previous new) and, at commit,
--      the listing's latest revision equals its current reorder point.
--   3. meta.activity_runs / meta.activity_checkpoints (L2, run protocol 7.3):
--      one run row per window (no overlaps, each window starts where the last
--      verified one ended, each run names its predecessor and only the first run
--      has none: enforced by a unique index, so concurrent starts cannot fork
--      the chain in any isolation level), immutable window, fingerprints and start state hash;
--      running -> verified | failed, failed -> running (resume); a run becomes
--      verified only together with its checkpoint (same transaction, same hash).
--      Checkpoints are insert-only. Engine metadata: not captured in change_log.
--   4. Privileges (V008 roles): writer SELECT and INSERT on the five tables and
--      UPDATE of the run status columns only; reader SELECT; nothing for PUBLIC
--      or the extractor. No DELETE or TRUNCATE for anyone (guards also bind the
--      owner).
-- =============================================================================

BEGIN;

SET LOCAL lock_timeout = '10s';

DO $guard$
BEGIN
    IF NOT (SELECT r.rolsuper FROM pg_roles AS r WHERE r.rolname = session_user) THEN
        RAISE EXCEPTION 'V009 runs as a superuser session with SET ROLE nordiccart_owner; connect as postgres, not as %.', session_user;
    END IF;
    -- checked before SET ROLE: without V008 the owner role has no rights in this database yet
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nordiccart_owner')
       OR NOT EXISTS (SELECT 1 FROM meta.schema_migrations WHERE version = '008') THEN
        RAISE EXCEPTION 'Migration V009 requires V008, which is not applied yet.'
            USING HINT = 'Run the migrations in version order.';
    END IF;
END
$guard$;

SET LOCAL ROLE nordiccart_owner;
SET LOCAL search_path TO public;

SELECT meta.assert_migration_can_run('009', '008');

-- -----------------------------------------------------------------------------
-- Enums
-- -----------------------------------------------------------------------------
CREATE TYPE purchase_order_status AS ENUM ('open', 'partially_received', 'received');
COMMENT ON TYPE purchase_order_status IS
    'Derived from received quantities: open (nothing received), partially_received, received (every line complete).';

CREATE TYPE purchase_order_reason AS ENUM ('reorder_point', 'launch', 'seasonal', 'forced_reorder');
COMMENT ON TYPE purchase_order_reason IS
    'Why a line was ordered (design L1): reorder_point (inventory position at or below it), launch (first stock of a new product), seasonal (pre-season build-up), forced_reorder (pilot scenario, decision D1).';

CREATE TYPE reorder_point_revision_kind AS ENUM ('pilot_r0', 'monthly_review');
COMMENT ON TYPE reorder_point_revision_kind IS
    'pilot_r0: the replenishment pilot revision R0 (echelon demand, 2024-11-15). monthly_review: the monthly R-cal review of the combined activity (stage S6).';

CREATE TYPE meta.activity_run_mode AS ENUM ('catch_up', 'live');
CREATE TYPE meta.activity_run_status AS ENUM ('running', 'verified', 'failed');
COMMENT ON TYPE meta.activity_run_status IS
    'running -> verified (with its checkpoint) or failed; failed -> running (resume after a fix). verified is final.';

-- -----------------------------------------------------------------------------
-- purchase_orders
-- -----------------------------------------------------------------------------
CREATE TABLE purchase_orders (
    purchase_order_id      bigint                 GENERATED ALWAYS AS IDENTITY,
    purchase_order_number  varchar(17)            NOT NULL,
    stock_location_id      bigint                 NOT NULL,
    location_type          stock_location_type    NOT NULL DEFAULT 'warehouse',
    ordered_at             timestamptz            NOT NULL,
    status                 purchase_order_status  NOT NULL DEFAULT 'open',
    received_at            timestamptz,
    created_at             timestamptz            NOT NULL DEFAULT now(),
    updated_at             timestamptz            NOT NULL DEFAULT now(),
    CONSTRAINT pk_purchase_orders PRIMARY KEY (purchase_order_id),
    CONSTRAINT uq_purchase_orders_number UNIQUE (purchase_order_number),
    -- target of the line FK: a line is always at its order's warehouse
    CONSTRAINT uq_purchase_orders_id_location UNIQUE (purchase_order_id, stock_location_id),
    -- type-checked FK (V005 pattern): orders are placed by warehouses only
    CONSTRAINT fk_purchase_orders_warehouse
        FOREIGN KEY (stock_location_id, location_type) REFERENCES warehouses (stock_location_id, location_type),
    CONSTRAINT ck_purchase_orders_location_type CHECK (location_type = 'warehouse'),
    CONSTRAINT ck_purchase_orders_number_format CHECK (purchase_order_number ~ '^PO-[A-Z]{2}-[0-9]{4}-[0-9]{6}$'),
    CONSTRAINT ck_purchase_orders_received_at CHECK ((status = 'received') = (received_at IS NOT NULL)),
    CONSTRAINT ck_purchase_orders_received_after_order CHECK (received_at IS NULL OR received_at >= ordered_at)
);

CREATE INDEX ix_purchase_orders_location_ordered_at ON purchase_orders (stock_location_id, ordered_at);
CREATE INDEX ix_purchase_orders_open ON purchase_orders (status) WHERE status <> 'received';
CREATE INDEX ix_purchase_orders_updated_at ON purchase_orders (updated_at);

COMMENT ON TABLE purchase_orders IS
    'Supplier purchase orders of the warehouses (design L1). Receipts are purchase_receipt movements with external_reference <purchase_order_number>/DN<n>. status and received_at are derived from the lines and maintained by triggers only.';
COMMENT ON COLUMN purchase_orders.ordered_at IS 'Business time of the warehouse review that placed the order';
COMMENT ON COLUMN purchase_orders.received_at IS 'Business time of the last receipt, once every line is complete';

-- -----------------------------------------------------------------------------
-- purchase_order_lines
-- -----------------------------------------------------------------------------
CREATE TABLE purchase_order_lines (
    purchase_order_line_id  bigint                 GENERATED ALWAYS AS IDENTITY,
    purchase_order_id       bigint                 NOT NULL,
    line_number             smallint               NOT NULL,
    stock_location_id       bigint                 NOT NULL,
    product_id              bigint                 NOT NULL,
    order_reason            purchase_order_reason  NOT NULL,
    quantity_ordered        integer                NOT NULL,
    quantity_received       integer                NOT NULL DEFAULT 0,
    quantity_outstanding    integer                GENERATED ALWAYS AS (quantity_ordered - quantity_received) STORED,
    status                  purchase_order_status  NOT NULL DEFAULT 'open',
    created_at              timestamptz            NOT NULL DEFAULT now(),
    updated_at              timestamptz            NOT NULL DEFAULT now(),
    CONSTRAINT pk_purchase_order_lines PRIMARY KEY (purchase_order_line_id),
    CONSTRAINT uq_purchase_order_lines_line UNIQUE (purchase_order_id, line_number),
    CONSTRAINT uq_purchase_order_lines_product UNIQUE (purchase_order_id, product_id),
    CONSTRAINT fk_purchase_order_lines_order
        FOREIGN KEY (purchase_order_id, stock_location_id) REFERENCES purchase_orders (purchase_order_id, stock_location_id),
    CONSTRAINT fk_purchase_order_lines_balance
        FOREIGN KEY (stock_location_id, product_id) REFERENCES inventory_balances (stock_location_id, product_id),
    CONSTRAINT ck_purchase_order_lines_line_number CHECK (line_number > 0),
    CONSTRAINT ck_purchase_order_lines_ordered CHECK (quantity_ordered > 0),
    CONSTRAINT ck_purchase_order_lines_received CHECK (quantity_received >= 0 AND quantity_received <= quantity_ordered),
    CONSTRAINT ck_purchase_order_lines_status CHECK (status = CASE
        WHEN quantity_received = 0 THEN 'open'::purchase_order_status
        WHEN quantity_received < quantity_ordered THEN 'partially_received'::purchase_order_status
        ELSE 'received'::purchase_order_status END)
);

CREATE INDEX ix_purchase_order_lines_location_product ON purchase_order_lines (stock_location_id, product_id);
-- open supply per listing (engine state at a window start, design 7.2)
CREATE INDEX ix_purchase_order_lines_open ON purchase_order_lines (stock_location_id, product_id) WHERE status <> 'received';
CREATE INDEX ix_purchase_order_lines_updated_at ON purchase_order_lines (updated_at);

COMMENT ON TABLE purchase_order_lines IS
    'One line per product of a purchase order. quantity_received = SUM of the purchase_receipt movements of the order (<number>/DN<n>) for the line''s warehouse and product, maintained by triggers only; quantity_outstanding = ordered - received.';

-- -----------------------------------------------------------------------------
-- reorder_point_revisions (decision C18)
-- -----------------------------------------------------------------------------
CREATE TABLE reorder_point_revisions (
    reorder_point_revision_id  bigint                       GENERATED ALWAYS AS IDENTITY,
    stock_location_id          bigint                       NOT NULL,
    product_id                 bigint                       NOT NULL,
    revision_kind              reorder_point_revision_kind  NOT NULL,
    effective_at               timestamptz                  NOT NULL,
    old_reorder_point          integer                      NOT NULL,
    new_reorder_point          integer                      NOT NULL,
    document_reference         varchar(40)                  NOT NULL,
    created_at                 timestamptz                  NOT NULL DEFAULT now(),
    CONSTRAINT pk_reorder_point_revisions PRIMARY KEY (reorder_point_revision_id),
    CONSTRAINT fk_reorder_point_revisions_balance
        FOREIGN KEY (stock_location_id, product_id) REFERENCES inventory_balances (stock_location_id, product_id),
    CONSTRAINT uq_reorder_point_revisions_listing_time UNIQUE (stock_location_id, product_id, effective_at),
    -- idempotency key: a repeated revision document fails with 23505
    CONSTRAINT uq_reorder_point_revisions_document UNIQUE (document_reference, stock_location_id, product_id),
    CONSTRAINT ck_reorder_point_revisions_old CHECK (old_reorder_point >= 0),
    CONSTRAINT ck_reorder_point_revisions_new CHECK (new_reorder_point >= 0),
    CONSTRAINT ck_reorder_point_revisions_changed CHECK (new_reorder_point <> old_reorder_point),
    CONSTRAINT ck_reorder_point_revisions_reference CHECK (document_reference ~ '^[A-Za-z0-9][A-Za-z0-9/_.-]*$')
);

CREATE INDEX ix_reorder_point_revisions_effective_at ON reorder_point_revisions (effective_at);
CREATE INDEX ix_reorder_point_revisions_document ON reorder_point_revisions (document_reference);

COMMENT ON TABLE reorder_point_revisions IS
    'Durable, insert-only history of reorder point changes in business time (decision C18). Per listing the rows form a chain: old_reorder_point = the previous revision''s new value, effective_at increasing; at commit the latest revision equals inventory_balances.reorder_point. The first revision''s old value is the value before it (for R0: the opening inventory model).';

-- -----------------------------------------------------------------------------
-- meta.activity_runs and meta.activity_checkpoints (run protocol, design 7.3)
-- -----------------------------------------------------------------------------
CREATE TABLE meta.activity_runs (
    activity_run_id        bigint                    GENERATED ALWAYS AS IDENTITY,
    previous_run_id        bigint,
    run_mode               meta.activity_run_mode    NOT NULL,
    window_start           timestamptz               NOT NULL,
    window_end             timestamptz               NOT NULL,
    status                 meta.activity_run_status  NOT NULL DEFAULT 'running',
    plan_fingerprint       char(64)                  NOT NULL,
    config_fingerprint     char(64)                  NOT NULL,
    start_state_hash       char(64)                  NOT NULL,
    end_state_hash         char(64),
    planned_units          integer                   NOT NULL,
    completed_units        integer                   NOT NULL DEFAULT 0,
    unit_counts            jsonb                     NOT NULL DEFAULT '{}'::jsonb,
    failure_reason         text,
    started_at             timestamptz               NOT NULL DEFAULT now(),
    finished_at            timestamptz,
    recovery_point         varchar(255),
    recovery_point_sha256  char(64),
    created_at             timestamptz               NOT NULL DEFAULT now(),
    updated_at             timestamptz               NOT NULL DEFAULT now(),
    CONSTRAINT pk_activity_runs PRIMARY KEY (activity_run_id),
    -- no two runs for overlapping business time (a repeated start fails with 23P01)
    CONSTRAINT ex_activity_runs_no_overlap EXCLUDE USING gist (tstzrange(window_start, window_end, '[)') WITH &&),
    -- the run chain: one first run (no predecessor) and at most one successor per run. An index, not the
    -- guard's read, decides: a concurrent second start (even a disjoint first window, even from an older
    -- REPEATABLE READ snapshot) waits for the first and then fails with 23505.
    CONSTRAINT uq_activity_runs_previous UNIQUE NULLS NOT DISTINCT (previous_run_id),
    CONSTRAINT fk_activity_runs_previous FOREIGN KEY (previous_run_id) REFERENCES meta.activity_runs (activity_run_id),
    CONSTRAINT ck_activity_runs_window CHECK (window_end > window_start),
    CONSTRAINT ck_activity_runs_plan_fingerprint CHECK (plan_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_activity_runs_config_fingerprint CHECK (config_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_activity_runs_start_state_hash CHECK (start_state_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_activity_runs_end_state_hash CHECK (end_state_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_activity_runs_units CHECK (planned_units >= 0 AND completed_units >= 0 AND completed_units <= planned_units),
    CONSTRAINT ck_activity_runs_unit_counts CHECK (jsonb_typeof(unit_counts) = 'object'),
    CONSTRAINT ck_activity_runs_verified CHECK ((status = 'verified') = (end_state_hash IS NOT NULL)
                                                AND (status <> 'verified' OR completed_units = planned_units)),
    CONSTRAINT ck_activity_runs_failed CHECK ((status = 'failed') = (failure_reason IS NOT NULL)),
    CONSTRAINT ck_activity_runs_finished CHECK ((status = 'running') = (finished_at IS NULL)
                                                AND (finished_at IS NULL OR finished_at >= started_at)),
    CONSTRAINT ck_activity_runs_recovery_point CHECK ((recovery_point IS NULL) = (recovery_point_sha256 IS NULL)
                                                      AND (recovery_point IS NULL OR status = 'verified')
                                                      AND (recovery_point_sha256 IS NULL OR recovery_point_sha256 ~ '^[0-9a-f]{64}$'))
);

CREATE INDEX ix_activity_runs_window_end ON meta.activity_runs (window_end);
CREATE INDEX ix_activity_runs_status ON meta.activity_runs (status) WHERE status <> 'verified';

COMMENT ON TABLE meta.activity_runs IS
    'Run ledger of the activity engine (design 7.3): one row per window, bounds, plan and config fingerprints (SHA-256 hex), state hashes, planned and completed units of work, status, timings, monthly recovery point. Windows never overlap and each starts where the last verified one ended; previous_run_id chains the runs (one first run, one successor per run, unique index). Engine metadata, not business data: not captured in change_log.';
COMMENT ON COLUMN meta.activity_runs.previous_run_id IS
    'The run this window follows (NULL only for the first run). Set by the guard on insert, never changed; uq_activity_runs_previous serializes concurrent starts.';

CREATE TABLE meta.activity_checkpoints (
    activity_checkpoint_id  bigint       GENERATED ALWAYS AS IDENTITY,
    activity_run_id         bigint       NOT NULL,
    state_as_of             timestamptz  NOT NULL,
    state_hash              char(64)     NOT NULL,
    state_snapshot          jsonb        NOT NULL,
    created_at              timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT pk_activity_checkpoints PRIMARY KEY (activity_checkpoint_id),
    CONSTRAINT uq_activity_checkpoints_run UNIQUE (activity_run_id),
    CONSTRAINT uq_activity_checkpoints_state_as_of UNIQUE (state_as_of),
    CONSTRAINT fk_activity_checkpoints_run FOREIGN KEY (activity_run_id) REFERENCES meta.activity_runs (activity_run_id),
    CONSTRAINT ck_activity_checkpoints_state_hash CHECK (state_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_activity_checkpoints_snapshot CHECK (jsonb_typeof(state_snapshot) = 'object')
);

COMMENT ON TABLE meta.activity_checkpoints IS
    'State snapshot (JSON) and its SHA-256 at the end of each verified window (design 7.2). Insert-only; written in the same transaction that marks the run verified. The next window recomputes the state from the database and must find the same hash.';

-- -----------------------------------------------------------------------------
-- Purchase order functions
-- -----------------------------------------------------------------------------
-- Received units of one order line, from the ledger: purchase receipts of <number>/DN<n>.
CREATE FUNCTION fn_purchase_order_line_received(p_purchase_order_number varchar, p_stock_location_id bigint, p_product_id bigint)
RETURNS integer
LANGUAGE sql
AS $fn$
    SELECT coalesce(sum(m.quantity_change), 0)::integer
      FROM public.inventory_movements AS m
     WHERE m.movement_type = 'purchase_receipt'
       AND m.stock_location_id = p_stock_location_id
       AND m.product_id = p_product_id
       AND m.external_reference ~ '^PO-[A-Z]{2}-[0-9]{4}-[0-9]{6}/DN[1-9][0-9]*$'
       AND split_part(m.external_reference, '/', 1) = p_purchase_order_number
$fn$;

COMMENT ON FUNCTION fn_purchase_order_line_received(varchar, bigint, bigint) IS
    'Units received for a purchase order line = SUM of the purchase_receipt movements <number>/DN<n> at the warehouse for the product.';

-- Order status and received_at derived from the lines and the ledger.
CREATE FUNCTION fn_purchase_order_state(p_purchase_order_id bigint,
                                        OUT o_status public.purchase_order_status, OUT o_received_at timestamptz)
LANGUAGE sql
AS $fn$
    SELECT CASE WHEN count(*) = 0 OR sum(l.quantity_received) = 0 THEN 'open'::public.purchase_order_status
                WHEN bool_and(l.quantity_received = l.quantity_ordered) THEN 'received'::public.purchase_order_status
                ELSE 'partially_received'::public.purchase_order_status END,
           CASE WHEN count(*) > 0 AND bool_and(l.quantity_received = l.quantity_ordered) THEN
               (SELECT max(m.occurred_at)
                  FROM public.inventory_movements AS m
                  JOIN public.purchase_orders AS o ON o.purchase_order_id = p_purchase_order_id
                 WHERE m.movement_type = 'purchase_receipt'
                   AND m.stock_location_id = o.stock_location_id
                   AND m.external_reference ~ '^PO-[A-Z]{2}-[0-9]{4}-[0-9]{6}/DN[1-9][0-9]*$'
                   AND split_part(m.external_reference, '/', 1) = o.purchase_order_number)
           END
      FROM public.purchase_order_lines AS l
     WHERE l.purchase_order_id = p_purchase_order_id
$fn$;

COMMENT ON FUNCTION fn_purchase_order_state(bigint) IS
    'Derived order status (open, partially_received, received) and received_at (last receipt, once every line is complete).';

-- Writes the derived status on the order row. Called only from the SECURITY DEFINER triggers below.
CREATE FUNCTION fn_purchase_order_refresh(p_purchase_order_id bigint)
RETURNS void
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_state record;
BEGIN
    SELECT * INTO v_state FROM public.fn_purchase_order_state(p_purchase_order_id);
    UPDATE public.purchase_orders
       SET status = v_state.o_status, received_at = v_state.o_received_at
     WHERE purchase_order_id = p_purchase_order_id
       AND (status, received_at) IS DISTINCT FROM (v_state.o_status, v_state.o_received_at);
END
$fn$;

COMMENT ON FUNCTION fn_purchase_order_refresh(bigint) IS
    'Sets purchase_orders.status and received_at to their derived values. Internal: called by the SECURITY DEFINER triggers.';

-- BEFORE INSERT OR UPDATE on purchase_orders: number of the warehouse market and local year;
-- inserted open; identity never changes; status and received_at always the derived values.
CREATE FUNCTION fn_purchase_orders_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_country  char(2);
    v_tz       text;
    v_state    record;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'open' OR NEW.received_at IS NOT NULL THEN
            RAISE EXCEPTION 'Purchase order %: a new order starts open; received quantities come from receipts.', NEW.purchase_order_number
                USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_orders_insert_open';
        END IF;
        SELECT sl.country_code, c.time_zone INTO v_country, v_tz
          FROM public.stock_locations AS sl JOIN public.countries AS c ON c.country_code = sl.country_code
         WHERE sl.stock_location_id = NEW.stock_location_id;
        IF substring(NEW.purchase_order_number FROM 4 FOR 2) IS DISTINCT FROM v_country THEN
            RAISE EXCEPTION 'Purchase order %: the number must carry the warehouse market %.', NEW.purchase_order_number, v_country
                USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_orders_number_market';
        END IF;
        IF substring(NEW.purchase_order_number FROM 7 FOR 4)::integer
           <> extract(year FROM (NEW.ordered_at AT TIME ZONE v_tz))::integer THEN
            RAISE EXCEPTION 'Purchase order %: the number must carry the local year of ordered_at (%).',
                NEW.purchase_order_number, extract(year FROM (NEW.ordered_at AT TIME ZONE v_tz))
                USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_orders_number_year';
        END IF;
        RETURN NEW;
    END IF;

    IF (NEW.purchase_order_id, NEW.purchase_order_number, NEW.stock_location_id, NEW.location_type, NEW.ordered_at, NEW.created_at)
       IS DISTINCT FROM
       (OLD.purchase_order_id, OLD.purchase_order_number, OLD.stock_location_id, OLD.location_type, OLD.ordered_at, OLD.created_at) THEN
        RAISE EXCEPTION 'Purchase order %: number, warehouse and ordered_at never change.', OLD.purchase_order_number
            USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_orders_identity_immutable';
    END IF;
    SELECT * INTO v_state FROM public.fn_purchase_order_state(NEW.purchase_order_id);
    IF (NEW.status, NEW.received_at) IS DISTINCT FROM (v_state.o_status, v_state.o_received_at) THEN
        RAISE EXCEPTION 'Purchase order %: status % / received_at % differ from the receipts (% / %).',
            NEW.purchase_order_number, NEW.status, NEW.received_at, v_state.o_status, v_state.o_received_at
            USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_orders_status_from_receipts';
    END IF;
    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION fn_purchase_orders_guard() IS
    'BEFORE INSERT OR UPDATE on purchase_orders: inserted open; number of the warehouse market and the local year of ordered_at; number, warehouse and ordered_at immutable; status and received_at equal to the values derived from the receipts.';

-- BEFORE INSERT OR UPDATE on purchase_order_lines: received quantity and status come from the ledger.
CREATE FUNCTION fn_purchase_order_lines_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_number   varchar;
    v_status   public.purchase_order_status;
    v_received integer;
BEGIN
    SELECT o.purchase_order_number, o.status INTO v_number, v_status
      FROM public.purchase_orders AS o WHERE o.purchase_order_id = NEW.purchase_order_id;
    v_received := public.fn_purchase_order_line_received(v_number, NEW.stock_location_id, NEW.product_id);

    IF TG_OP = 'INSERT' THEN
        IF v_status = 'received' THEN
            RAISE EXCEPTION 'Purchase order %: completely received; no line can be added.', v_number
                USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_order_lines_order_received';
        END IF;
        IF v_received > NEW.quantity_ordered THEN
            RAISE EXCEPTION 'Purchase order % line %: % units already received, % ordered.',
                v_number, NEW.line_number, v_received, NEW.quantity_ordered
                USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_order_lines_received_exceeds_ordered';
        END IF;
        -- derived values; anything supplied is replaced (E3: a backfilled line takes its receipts)
        NEW.quantity_received := v_received;
        NEW.status := CASE WHEN v_received = 0 THEN 'open'::public.purchase_order_status
                           WHEN v_received < NEW.quantity_ordered THEN 'partially_received'::public.purchase_order_status
                           ELSE 'received'::public.purchase_order_status END;
        RETURN NEW;
    END IF;

    IF (NEW.purchase_order_line_id, NEW.purchase_order_id, NEW.line_number, NEW.stock_location_id, NEW.product_id,
        NEW.order_reason, NEW.quantity_ordered, NEW.created_at)
       IS DISTINCT FROM
       (OLD.purchase_order_line_id, OLD.purchase_order_id, OLD.line_number, OLD.stock_location_id, OLD.product_id,
        OLD.order_reason, OLD.quantity_ordered, OLD.created_at) THEN
        RAISE EXCEPTION 'Purchase order % line %: product, reason and quantity ordered never change.', v_number, OLD.line_number
            USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_order_lines_terms_immutable';
    END IF;
    IF NEW.quantity_received <> v_received THEN
        RAISE EXCEPTION 'Purchase order % line %: quantity_received % differs from the receipts (%).',
            v_number, OLD.line_number, NEW.quantity_received, v_received
            USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_order_lines_received_from_receipts';
    END IF;
    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION fn_purchase_order_lines_guard() IS
    'BEFORE INSERT OR UPDATE on purchase_order_lines: no line for a completely received order; quantity_received and status derived from the purchase receipts <number>/DN<n> (never more than ordered); terms immutable.';

-- AFTER INSERT on purchase_order_lines (SECURITY DEFINER): the order status follows its lines.
CREATE FUNCTION fn_purchase_order_lines_refresh_order()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $fn$
BEGIN
    PERFORM public.fn_purchase_order_refresh(NEW.purchase_order_id);
    RETURN NULL;
END
$fn$;

COMMENT ON FUNCTION fn_purchase_order_lines_refresh_order() IS
    'AFTER INSERT on purchase_order_lines: sets the order status from its lines. V009: SECURITY DEFINER (nordiccart_owner), fixed search_path; writers have no UPDATE privilege on purchase_orders.';

-- AFTER INSERT on inventory_movements (SECURITY DEFINER): a purchase receipt of <number>/DN<n>
-- must fit its order and moves the line and order status. Other references: V007 behaviour.
CREATE FUNCTION fn_purchase_receipt_apply()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $fn$
DECLARE
    v_number  varchar;
    v_order   public.purchase_orders%ROWTYPE;
    v_line    public.purchase_order_lines%ROWTYPE;
BEGIN
    IF NEW.external_reference IS NULL OR NEW.external_reference NOT LIKE 'PO-%' THEN
        RETURN NULL;
    END IF;
    IF NEW.external_reference !~ '^PO-[A-Z]{2}-[0-9]{4}-[0-9]{6}/DN[1-9][0-9]*$' THEN
        RAISE EXCEPTION 'Purchase receipt %: a purchase order reference has the form PO-<market>-<year>-<seq>/DN<n>.', NEW.external_reference
            USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_receipt_reference_format';
    END IF;
    v_number := split_part(NEW.external_reference, '/', 1);

    -- lock order: balance rows (the movement insert) -> order row -> order line
    SELECT * INTO v_order FROM public.purchase_orders WHERE purchase_order_number = v_number FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Purchase receipt %: purchase order % does not exist.', NEW.external_reference, v_number
            USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_receipt_requires_purchase_order';
    END IF;
    IF v_order.stock_location_id <> NEW.stock_location_id THEN
        RAISE EXCEPTION 'Purchase receipt %: received at another location than the ordering warehouse.', NEW.external_reference
            USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_receipt_ordering_warehouse';
    END IF;
    IF NEW.occurred_at < v_order.ordered_at THEN
        RAISE EXCEPTION 'Purchase receipt %: received at % before the order (%).', NEW.external_reference, NEW.occurred_at, v_order.ordered_at
            USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_receipt_after_order';
    END IF;
    SELECT * INTO v_line FROM public.purchase_order_lines
     WHERE purchase_order_id = v_order.purchase_order_id AND product_id = NEW.product_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Purchase receipt %: the product is not on purchase order %.', NEW.external_reference, v_number
            USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_receipt_product_ordered';
    END IF;
    IF v_line.quantity_received + NEW.quantity_change > v_line.quantity_ordered THEN
        RAISE EXCEPTION 'Purchase receipt %: % + % units would exceed the % ordered.',
            NEW.external_reference, v_line.quantity_received, NEW.quantity_change, v_line.quantity_ordered
            USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_receipt_within_ordered';
    END IF;

    UPDATE public.purchase_order_lines
       SET quantity_received = quantity_received + NEW.quantity_change,
           status = CASE WHEN quantity_received + NEW.quantity_change < quantity_ordered
                         THEN 'partially_received'::public.purchase_order_status
                         ELSE 'received'::public.purchase_order_status END
     WHERE purchase_order_line_id = v_line.purchase_order_line_id;
    PERFORM public.fn_purchase_order_refresh(v_order.purchase_order_id);
    RETURN NULL;
END
$fn$;

COMMENT ON FUNCTION fn_purchase_receipt_apply() IS
    'AFTER INSERT on inventory_movements (purchase_receipt): a reference PO-<market>-<year>-<seq>/DN<n> must name an existing order of the receiving warehouse, a line for the product, a time not before the order and at most the outstanding quantity; then the line and order statuses move. V009: SECURITY DEFINER (nordiccart_owner), fixed search_path.';

-- Deferred (commit time) on purchase_orders: at least one line; every receipt of the order fits a line.
CREATE FUNCTION fn_check_purchase_order_complete()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_order  public.purchase_orders%ROWTYPE;
    v_bad    integer;
BEGIN
    SELECT * INTO v_order FROM public.purchase_orders WHERE purchase_order_id = NEW.purchase_order_id;
    IF NOT EXISTS (SELECT 1 FROM public.purchase_order_lines WHERE purchase_order_id = v_order.purchase_order_id) THEN
        RAISE EXCEPTION 'Purchase order %: an order needs at least one line (insert the order and its lines in one transaction).',
            v_order.purchase_order_number
            USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_orders_have_lines';
    END IF;
    SELECT count(*) INTO v_bad
      FROM public.inventory_movements AS m
     WHERE m.movement_type = 'purchase_receipt'
       AND m.external_reference ~ '^PO-[A-Z]{2}-[0-9]{4}-[0-9]{6}/DN[1-9][0-9]*$'
       AND split_part(m.external_reference, '/', 1) = v_order.purchase_order_number
       AND (m.stock_location_id <> v_order.stock_location_id
            OR m.occurred_at < v_order.ordered_at
            OR NOT EXISTS (SELECT 1 FROM public.purchase_order_lines AS l
                            WHERE l.purchase_order_id = v_order.purchase_order_id AND l.product_id = m.product_id));
    IF v_bad > 0 THEN
        RAISE EXCEPTION 'Purchase order %: % existing receipts do not fit its lines (warehouse, product or time).',
            v_order.purchase_order_number, v_bad
            USING ERRCODE = 'check_violation', CONSTRAINT = 'purchase_orders_receipts_fit_lines';
    END IF;
    RETURN NULL;
END
$fn$;

COMMENT ON FUNCTION fn_check_purchase_order_complete() IS
    'Deferred constraint trigger on purchase_orders (commit time): at least one line, and every receipt <number>/DN<n> already in the ledger is at the warehouse, not before the order, for a product on a line (exception E3).';

-- -----------------------------------------------------------------------------
-- Reorder point revision functions
-- -----------------------------------------------------------------------------
CREATE FUNCTION fn_reorder_point_revisions_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_current  integer;
    v_previous public.reorder_point_revisions%ROWTYPE;
BEGIN
    SELECT b.reorder_point INTO v_current
      FROM public.inventory_balances AS b
     WHERE b.stock_location_id = NEW.stock_location_id AND b.product_id = NEW.product_id
       FOR UPDATE;
    SELECT * INTO v_previous FROM public.reorder_point_revisions
     WHERE stock_location_id = NEW.stock_location_id AND product_id = NEW.product_id
     ORDER BY effective_at DESC LIMIT 1;
    IF FOUND THEN
        IF NEW.effective_at <= v_previous.effective_at THEN
            RAISE EXCEPTION 'Reorder point revision %: effective_at % is not after the previous revision (%).',
                NEW.document_reference, NEW.effective_at, v_previous.effective_at
                USING ERRCODE = 'check_violation', CONSTRAINT = 'reorder_point_revisions_business_time_order';
        END IF;
        IF NEW.old_reorder_point <> v_previous.new_reorder_point THEN
            RAISE EXCEPTION 'Reorder point revision %: old value % is not the previous revision''s new value %.',
                NEW.document_reference, NEW.old_reorder_point, v_previous.new_reorder_point
                USING ERRCODE = 'check_violation', CONSTRAINT = 'reorder_point_revisions_chain';
        END IF;
    END IF;
    -- recorded just before the balance update (current = old) or just after it / as a backfill (current = new)
    IF v_current NOT IN (NEW.old_reorder_point, NEW.new_reorder_point) THEN
        RAISE EXCEPTION 'Reorder point revision %: the listing''s reorder point is %, neither the old (%) nor the new (%) value.',
            NEW.document_reference, v_current, NEW.old_reorder_point, NEW.new_reorder_point
            USING ERRCODE = 'check_violation', CONSTRAINT = 'reorder_point_revisions_match_balance';
    END IF;
    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION fn_reorder_point_revisions_guard() IS
    'BEFORE INSERT on reorder_point_revisions: locks the balance row; per listing revisions are in business-time order and chained (old = previous new); the balance holds the old or the new value.';

CREATE FUNCTION fn_check_reorder_point_revision_applied()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_latest  integer;
    v_current integer;
BEGIN
    SELECT r.new_reorder_point INTO v_latest FROM public.reorder_point_revisions AS r
     WHERE r.stock_location_id = NEW.stock_location_id AND r.product_id = NEW.product_id
     ORDER BY r.effective_at DESC LIMIT 1;
    SELECT b.reorder_point INTO v_current FROM public.inventory_balances AS b
     WHERE b.stock_location_id = NEW.stock_location_id AND b.product_id = NEW.product_id;
    IF v_latest IS DISTINCT FROM v_current THEN
        RAISE EXCEPTION 'Reorder point revision %: at commit the listing''s reorder point is %, its latest revision says %.',
            NEW.document_reference, v_current, v_latest
            USING ERRCODE = 'check_violation', CONSTRAINT = 'reorder_point_revisions_applied';
    END IF;
    RETURN NULL;
END
$fn$;

COMMENT ON FUNCTION fn_check_reorder_point_revision_applied() IS
    'Deferred constraint trigger on reorder_point_revisions (commit time): the listing''s latest revision equals inventory_balances.reorder_point.';

-- -----------------------------------------------------------------------------
-- Run ledger functions
-- -----------------------------------------------------------------------------
CREATE FUNCTION meta.fn_activity_runs_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_last meta.activity_runs%ROWTYPE;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'running' OR NEW.completed_units <> 0 OR NEW.end_state_hash IS NOT NULL
           OR NEW.finished_at IS NOT NULL OR NEW.recovery_point IS NOT NULL THEN
            RAISE EXCEPTION 'Activity run %: a run starts as running, with no completed units, end hash or recovery point.', NEW.window_start
                USING ERRCODE = 'check_violation', CONSTRAINT = 'activity_runs_start_running';
        END IF;
        SELECT * INTO v_last FROM meta.activity_runs ORDER BY window_end DESC LIMIT 1;
        IF FOUND THEN
            IF v_last.status <> 'verified' THEN
                RAISE EXCEPTION 'Activity run %: the run for % to % is %; resume it before starting another window.',
                    NEW.window_start, v_last.window_start, v_last.window_end, v_last.status
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'activity_runs_previous_verified';
            END IF;
            IF NEW.window_start <> v_last.window_end THEN
                RAISE EXCEPTION 'Activity run %: windows are contiguous; the next window starts at %.', NEW.window_start, v_last.window_end
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'activity_runs_contiguous';
            END IF;
        END IF;
        -- the chain link, always from the ledger (NULL = first run); a caller's value is replaced.
        -- This read may miss a concurrent or newer run: uq_activity_runs_previous then refuses the insert.
        NEW.previous_run_id := v_last.activity_run_id;
        RETURN NEW;
    END IF;

    IF (NEW.activity_run_id, NEW.previous_run_id, NEW.run_mode, NEW.window_start, NEW.window_end, NEW.plan_fingerprint,
        NEW.config_fingerprint, NEW.start_state_hash, NEW.planned_units, NEW.started_at, NEW.created_at)
       IS DISTINCT FROM
       (OLD.activity_run_id, OLD.previous_run_id, OLD.run_mode, OLD.window_start, OLD.window_end, OLD.plan_fingerprint,
        OLD.config_fingerprint, OLD.start_state_hash, OLD.planned_units, OLD.started_at, OLD.created_at) THEN
        RAISE EXCEPTION 'Activity run %: window, mode, fingerprints, start state and planned units never change (a different plan is a new decision, not an update).',
            OLD.activity_run_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'activity_runs_identity_immutable';
    END IF;
    IF NEW.completed_units < OLD.completed_units THEN
        RAISE EXCEPTION 'Activity run %: completed units cannot decrease (% -> %).', OLD.activity_run_id, OLD.completed_units, NEW.completed_units
            USING ERRCODE = 'check_violation', CONSTRAINT = 'activity_runs_progress_forward';
    END IF;
    IF OLD.status = 'verified' THEN
        -- final: only the monthly recovery point can still be recorded, once
        IF (NEW.status, NEW.completed_units, NEW.unit_counts, NEW.end_state_hash, NEW.failure_reason, NEW.finished_at)
           IS DISTINCT FROM (OLD.status, OLD.completed_units, OLD.unit_counts, OLD.end_state_hash, OLD.failure_reason, OLD.finished_at)
           OR (OLD.recovery_point IS NOT NULL
               AND (NEW.recovery_point, NEW.recovery_point_sha256) IS DISTINCT FROM (OLD.recovery_point, OLD.recovery_point_sha256)) THEN
            RAISE EXCEPTION 'Activity run %: a verified run is final (only its recovery point can be recorded, once).', OLD.activity_run_id
                USING ERRCODE = 'check_violation', CONSTRAINT = 'activity_runs_verified_final';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT ((OLD.status = 'running' AND NEW.status IN ('running', 'verified', 'failed'))
            OR (OLD.status = 'failed' AND NEW.status IN ('failed', 'running'))) THEN
        RAISE EXCEPTION 'Activity run %: status % -> % is not allowed.', OLD.activity_run_id, OLD.status, NEW.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'activity_runs_status_transition';
    END IF;
    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION meta.fn_activity_runs_guard() IS
    'BEFORE INSERT OR UPDATE on meta.activity_runs: a new run starts running, after a verified run, at its window end, and names it as previous_run_id (NULL for the first run; uq_activity_runs_previous makes concurrent starts safe); predecessor, window, mode, fingerprints, start state and planned units immutable; completed units never decrease; running -> verified | failed, failed -> running; a verified run is final except for recording its recovery point once.';

CREATE FUNCTION meta.fn_check_activity_run_verified()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF NEW.status = 'verified' AND NOT EXISTS (
           SELECT 1 FROM meta.activity_checkpoints AS c
            WHERE c.activity_run_id = NEW.activity_run_id
              AND c.state_hash = NEW.end_state_hash AND c.state_as_of = NEW.window_end) THEN
        RAISE EXCEPTION 'Activity run %: verified without its checkpoint (same transaction, same state hash, state as of the window end).',
            NEW.activity_run_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'activity_runs_verified_with_checkpoint';
    END IF;
    RETURN NULL;
END
$fn$;

COMMENT ON FUNCTION meta.fn_check_activity_run_verified() IS
    'Deferred constraint trigger on meta.activity_runs (commit time): a verified run has its checkpoint with the same state hash, as of the window end.';

CREATE FUNCTION meta.fn_activity_checkpoints_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_run meta.activity_runs%ROWTYPE;
BEGIN
    SELECT * INTO v_run FROM meta.activity_runs WHERE activity_run_id = NEW.activity_run_id;
    IF FOUND AND v_run.status = 'failed' THEN
        RAISE EXCEPTION 'Activity checkpoint: run % has failed; resume it first.', NEW.activity_run_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'activity_checkpoints_run_not_failed';
    END IF;
    IF FOUND AND NEW.state_as_of <> v_run.window_end THEN
        RAISE EXCEPTION 'Activity checkpoint: state_as_of % is not the end of run % (%).', NEW.state_as_of, NEW.activity_run_id, v_run.window_end
            USING ERRCODE = 'check_violation', CONSTRAINT = 'activity_checkpoints_window_end';
    END IF;
    RETURN NEW;
END
$fn$;

COMMENT ON FUNCTION meta.fn_activity_checkpoints_guard() IS
    'BEFORE INSERT on meta.activity_checkpoints: the run is not failed and the state is as of its window end.';

CREATE FUNCTION meta.fn_check_activity_checkpoint_verified()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM meta.activity_runs AS r
                    WHERE r.activity_run_id = NEW.activity_run_id
                      AND r.status = 'verified' AND r.end_state_hash = NEW.state_hash) THEN
        RAISE EXCEPTION 'Activity checkpoint of run %: the run must be verified in the same transaction with end_state_hash = state_hash.',
            NEW.activity_run_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'activity_checkpoints_run_verified';
    END IF;
    RETURN NULL;
END
$fn$;

COMMENT ON FUNCTION meta.fn_check_activity_checkpoint_verified() IS
    'Deferred constraint trigger on meta.activity_checkpoints (commit time): its run is verified with the same state hash.';

-- -----------------------------------------------------------------------------
-- Triggers
-- -----------------------------------------------------------------------------
-- purchase_orders
CREATE TRIGGER trg_purchase_orders_guard
    BEFORE INSERT OR UPDATE ON purchase_orders
    FOR EACH ROW EXECUTE FUNCTION fn_purchase_orders_guard();
CREATE TRIGGER trg_purchase_orders_set_updated_at
    BEFORE UPDATE ON purchase_orders
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();
CREATE TRIGGER trg_purchase_orders_no_delete
    BEFORE DELETE ON purchase_orders
    FOR EACH ROW EXECUTE FUNCTION fn_reject_modification();
CREATE TRIGGER trg_purchase_orders_no_truncate
    BEFORE TRUNCATE ON purchase_orders
    FOR EACH STATEMENT EXECUTE FUNCTION fn_reject_modification();
CREATE CONSTRAINT TRIGGER trg_purchase_orders_complete
    AFTER INSERT OR UPDATE ON purchase_orders
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_check_purchase_order_complete();
CREATE TRIGGER trg_purchase_orders_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON purchase_orders
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('purchase_order_id');

-- purchase_order_lines
CREATE TRIGGER trg_purchase_order_lines_guard
    BEFORE INSERT OR UPDATE ON purchase_order_lines
    FOR EACH ROW EXECUTE FUNCTION fn_purchase_order_lines_guard();
CREATE TRIGGER trg_purchase_order_lines_set_updated_at
    BEFORE UPDATE ON purchase_order_lines
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();
CREATE TRIGGER trg_purchase_order_lines_no_delete
    BEFORE DELETE ON purchase_order_lines
    FOR EACH ROW EXECUTE FUNCTION fn_reject_modification();
CREATE TRIGGER trg_purchase_order_lines_no_truncate
    BEFORE TRUNCATE ON purchase_order_lines
    FOR EACH STATEMENT EXECUTE FUNCTION fn_reject_modification();
CREATE TRIGGER trg_purchase_order_lines_refresh_order
    AFTER INSERT ON purchase_order_lines
    FOR EACH ROW EXECUTE FUNCTION fn_purchase_order_lines_refresh_order();
CREATE TRIGGER trg_purchase_order_lines_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON purchase_order_lines
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('purchase_order_line_id');

-- inventory_movements (V007 table): purchase receipts against their orders
CREATE TRIGGER trg_inventory_movements_purchase_order_receipt
    AFTER INSERT ON inventory_movements
    FOR EACH ROW WHEN (NEW.movement_type = 'purchase_receipt')
    EXECUTE FUNCTION fn_purchase_receipt_apply();

-- reorder_point_revisions (insert-only)
CREATE TRIGGER trg_reorder_point_revisions_guard
    BEFORE INSERT ON reorder_point_revisions
    FOR EACH ROW EXECUTE FUNCTION fn_reorder_point_revisions_guard();
CREATE TRIGGER trg_reorder_point_revisions_insert_only
    BEFORE UPDATE OR DELETE ON reorder_point_revisions
    FOR EACH ROW EXECUTE FUNCTION fn_reject_modification();
CREATE TRIGGER trg_reorder_point_revisions_no_truncate
    BEFORE TRUNCATE ON reorder_point_revisions
    FOR EACH STATEMENT EXECUTE FUNCTION fn_reject_modification();
CREATE CONSTRAINT TRIGGER trg_reorder_point_revisions_applied
    AFTER INSERT ON reorder_point_revisions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_check_reorder_point_revision_applied();
CREATE TRIGGER trg_reorder_point_revisions_capture_change
    AFTER INSERT OR UPDATE OR DELETE ON reorder_point_revisions
    FOR EACH ROW EXECUTE FUNCTION fn_capture_change('reorder_point_revision_id');

-- meta.activity_runs
CREATE TRIGGER trg_activity_runs_guard
    BEFORE INSERT OR UPDATE ON meta.activity_runs
    FOR EACH ROW EXECUTE FUNCTION meta.fn_activity_runs_guard();
CREATE TRIGGER trg_activity_runs_set_updated_at
    BEFORE UPDATE ON meta.activity_runs
    FOR EACH ROW EXECUTE FUNCTION public.fn_set_updated_at();
CREATE TRIGGER trg_activity_runs_no_delete
    BEFORE DELETE ON meta.activity_runs
    FOR EACH ROW EXECUTE FUNCTION public.fn_reject_modification();
CREATE TRIGGER trg_activity_runs_no_truncate
    BEFORE TRUNCATE ON meta.activity_runs
    FOR EACH STATEMENT EXECUTE FUNCTION public.fn_reject_modification();
CREATE CONSTRAINT TRIGGER trg_activity_runs_verified_with_checkpoint
    AFTER INSERT OR UPDATE ON meta.activity_runs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION meta.fn_check_activity_run_verified();

-- meta.activity_checkpoints (insert-only)
CREATE TRIGGER trg_activity_checkpoints_guard
    BEFORE INSERT ON meta.activity_checkpoints
    FOR EACH ROW EXECUTE FUNCTION meta.fn_activity_checkpoints_guard();
CREATE TRIGGER trg_activity_checkpoints_insert_only
    BEFORE UPDATE OR DELETE ON meta.activity_checkpoints
    FOR EACH ROW EXECUTE FUNCTION public.fn_reject_modification();
CREATE TRIGGER trg_activity_checkpoints_no_truncate
    BEFORE TRUNCATE ON meta.activity_checkpoints
    FOR EACH STATEMENT EXECUTE FUNCTION public.fn_reject_modification();
CREATE CONSTRAINT TRIGGER trg_activity_checkpoints_run_verified
    AFTER INSERT ON meta.activity_checkpoints
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION meta.fn_check_activity_checkpoint_verified();

-- -----------------------------------------------------------------------------
-- Privileges (V008 roles). Default privileges of nordiccart_owner (V008) already
-- give SELECT to nordiccart_reader and no EXECUTE to PUBLIC; stated explicitly here.
-- -----------------------------------------------------------------------------
REVOKE ALL ON FUNCTION
      public.fn_purchase_order_line_received(varchar, bigint, bigint), public.fn_purchase_order_state(bigint),
      public.fn_purchase_order_refresh(bigint), public.fn_purchase_orders_guard(), public.fn_purchase_order_lines_guard(),
      public.fn_purchase_order_lines_refresh_order(), public.fn_purchase_receipt_apply(),
      public.fn_check_purchase_order_complete(), public.fn_reorder_point_revisions_guard(),
      public.fn_check_reorder_point_revision_applied(), meta.fn_activity_runs_guard(),
      meta.fn_check_activity_run_verified(), meta.fn_activity_checkpoints_guard(),
      meta.fn_check_activity_checkpoint_verified()
   FROM PUBLIC;
REVOKE ALL ON public.purchase_orders, public.purchase_order_lines, public.reorder_point_revisions,
              meta.activity_runs, meta.activity_checkpoints FROM PUBLIC;

GRANT SELECT, INSERT ON public.purchase_orders, public.purchase_order_lines, public.reorder_point_revisions
   TO nordiccart_writer;
-- read-only helpers called by the line and order guards, which run with the writer's rights
GRANT EXECUTE ON FUNCTION public.fn_purchase_order_line_received(varchar, bigint, bigint),
                          public.fn_purchase_order_state(bigint)
   TO nordiccart_writer;
GRANT SELECT, INSERT ON meta.activity_runs, meta.activity_checkpoints TO nordiccart_writer;
GRANT UPDATE (status, completed_units, unit_counts, end_state_hash, failure_reason, finished_at,
              recovery_point, recovery_point_sha256)
   ON meta.activity_runs TO nordiccart_writer;
GRANT SELECT ON public.purchase_orders, public.purchase_order_lines, public.reorder_point_revisions,
                meta.activity_runs, meta.activity_checkpoints
   TO nordiccart_reader;

-- -----------------------------------------------------------------------------
-- Self-check: the security rules, re-read from the catalog. Any failure rolls back.
-- -----------------------------------------------------------------------------
DO $check$
DECLARE
    v_bad     text;
    v_new     text[] := ARRAY['public.purchase_orders', 'public.purchase_order_lines', 'public.reorder_point_revisions',
                              'meta.activity_runs', 'meta.activity_checkpoints'];
    v_app     text[] := ARRAY['nordiccart_writer', 'nordiccart_loader', 'nordiccart_scheduler', 'nordiccart_reader',
                              'nordiccart_analyst', 'nordiccart_extractor', 'public'];
    v_definer text[] := ARRAY['fn_capture_change()', 'fn_inventory_apply_movement()', 'fn_inventory_apply_reservation()',
                              'fn_purchase_order_lines_refresh_order()', 'fn_purchase_receipt_apply()'];
BEGIN
    -- every new object owned by nordiccart_owner
    SELECT string_agg(obj, ', ') INTO v_bad FROM (
        SELECT c.oid::regclass::text AS obj FROM pg_class AS c
         WHERE c.oid::regclass::text = ANY (v_new) AND c.relowner <> 'nordiccart_owner'::regrole
        UNION ALL
        SELECT p.oid::regprocedure::text FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
         WHERE n.nspname IN ('public', 'meta') AND p.proowner <> 'nordiccart_owner'::regrole
           AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
        UNION ALL
        SELECT t.oid::regtype::text FROM pg_type AS t JOIN pg_namespace AS n ON n.oid = t.typnamespace
         WHERE n.nspname IN ('public', 'meta') AND t.typtype = 'e' AND t.typowner <> 'nordiccart_owner'::regrole
    ) AS x;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V009 self-check: not owned by nordiccart_owner: %', v_bad;
    END IF;

    -- SECURITY DEFINER: exactly the V008 three and the V009 two, each with the fixed search_path
    SELECT string_agg(p.oid::regprocedure::text, ', ') INTO v_bad
      FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
     WHERE n.nspname IN ('public', 'meta')
       AND ((p.prosecdef AND NOT p.oid::regprocedure::text = ANY (v_definer))
         OR (p.oid::regprocedure::text = ANY (v_definer)
             AND NOT (p.prosecdef AND p.proconfig = ARRAY['search_path=pg_catalog, public, pg_temp'])));
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V009 self-check: SECURITY DEFINER or search_path not as designed: %', v_bad;
    END IF;

    -- nothing for PUBLIC on NordicCart functions and the new tables
    SELECT string_agg(p.oid::regprocedure::text, ', ') INTO v_bad
      FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
     WHERE n.nspname IN ('public', 'meta') AND has_function_privilege('public', p.oid, 'EXECUTE')
       AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e');
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V009 self-check: PUBLIC may execute: %', v_bad;
    END IF;

    -- no application role (nor PUBLIC) holds DELETE, TRUNCATE, TRIGGER or REFERENCES anywhere
    -- (V008 rule; the extractor's change_log purge excepted)
    SELECT string_agg(a.role_name || ' ' || pr.privilege || ' ' || c.oid::regclass::text, ', ') INTO v_bad
      FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
      CROSS JOIN unnest(v_app) AS a(role_name)
      CROSS JOIN unnest(ARRAY['DELETE', 'TRUNCATE', 'TRIGGER', 'REFERENCES']) AS pr(privilege)
     WHERE n.nspname IN ('public', 'meta') AND c.relkind = 'r'
       AND has_table_privilege(a.role_name, c.oid, pr.privilege)
       AND NOT (a.role_name = 'nordiccart_extractor' AND pr.privilege = 'DELETE' AND c.oid = 'public.change_log'::regclass);
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V009 self-check: forbidden table privileges: %', v_bad;
    END IF;

    -- the new tables: writer SELECT + INSERT, UPDATE only of the run status columns; reader SELECT only;
    -- extractor and PUBLIC nothing
    SELECT string_agg(x, ', ') INTO v_bad FROM (
        SELECT a.role_name || ' ' || pr.privilege || ' ' || t.tbl AS x
          FROM unnest(v_new) AS t(tbl)
          CROSS JOIN unnest(v_app) AS a(role_name)
          CROSS JOIN unnest(ARRAY['SELECT', 'INSERT']) AS pr(privilege)
         WHERE has_table_privilege(a.role_name, t.tbl, pr.privilege)
               <> (a.role_name IN ('nordiccart_writer', 'nordiccart_loader', 'nordiccart_scheduler')
                   OR (pr.privilege = 'SELECT' AND a.role_name IN ('nordiccart_reader', 'nordiccart_analyst')))
        UNION ALL
        SELECT a.role_name || ' UPDATE(' || att.attname || ') ' || t.tbl
          FROM unnest(v_new) AS t(tbl)
          JOIN pg_attribute AS att ON att.attrelid = t.tbl::regclass AND att.attnum > 0 AND NOT att.attisdropped
          CROSS JOIN unnest(v_app) AS a(role_name)
         WHERE has_column_privilege(a.role_name, t.tbl, att.attname, 'UPDATE')
               <> (t.tbl = 'meta.activity_runs'
                   AND a.role_name IN ('nordiccart_writer', 'nordiccart_loader', 'nordiccart_scheduler')
                   AND att.attname IN ('status', 'completed_units', 'unit_counts', 'end_state_hash', 'failure_reason',
                                       'finished_at', 'recovery_point', 'recovery_point_sha256'))
    ) AS y;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V009 self-check: privileges on the new tables not as designed: %', v_bad;
    END IF;
END
$check$;

-- -----------------------------------------------------------------------------
-- Record this migration (applied_by: the administrator's login, not the owner role)
-- -----------------------------------------------------------------------------
INSERT INTO meta.schema_migrations (version, description, script_name, applied_by)
VALUES ('009', 'Activity control and purchase orders: purchase orders, run ledger and checkpoints, reorder point revisions',
        'V009__activity_control_and_purchase_orders.sql', session_user);

COMMIT;
