-- =============================================================================
-- NordicCart OLTP | Verification 15: global inventory and activity (initial version)
-- -----------------------------------------------------------------------------
-- Scope       : the WHOLE database: every listing, movement, reservation, order and
--               payment attempt, whoever wrote it (the pilot loaders, later activity
--               and, from stage S6, the combined activity engine).
-- Connect to  : nordiccart_oltp (any role that can read these tables)
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS or INFO, and failed_checks = 0
--               (43 rows: 35 PASS, 8 INFO).
-- When        : after every load. In the historical catch-up after every month
--               (design 7.4: scripts 11 to 16), with params.range_start / range_end set
--               to the month: the range only changes the INFO rows (group 6); every
--               integrity check always covers the whole database.
-- Created     : 2026-11-18, Phase 3 step 5, stage S1 (initial version):
--               * takes over the global stock checks of scripts 13 and 14, which are
--                 now scoped to their pilots;
--               * applies the identity and integrity rules that the pilot loaders
--                 apply to later activity (python/nordiccart/activity/scope.py) to ALL
--                 records, pilot records included.
--               Later stages add: purchase orders and the run ledger (V009, S4/S6),
--               exchange rates (script 16, S5), price changes and reorder point reviews.
-- Parameters  : params CTE: opening count date and reference (config/generator.toml
--               [inventory]), reservation lifetimes and store hours rules shared with
--               script 13, the pre-launch windows of script 14, the longest time a
--               transfer may stay in transit, and the date range of the INFO rows.
-- Performance : (S8.1, 2026-11-20) res, sales and pay are AS NOT MATERIALIZED. Used more than
--               once, PostgreSQL would materialize them, and the correlated look-ups of checks
--               503 and 505 would scan the whole materialized set once per order (quadratic:
--               101 minutes on the 23-month S8 history). Inlined, they use the order_id and
--               order_item_id indexes. Same checks, expressions and coverage; same results.
-- Not checked : rules that need history the database does not keep: write order of
--               backdated records (exceptions E1 to E3, design 8.3) and reorder point
--               revisions (no business time stored). See the S1 test report.
-- -----------------------------------------------------------------------------
-- Groups: 1 ledger and conservation | 2 documents and transfers | 3 order identity
--         4 order integrity | 5 payments, reservations and sales | 6 INFO (range)
-- Integrity checks count the rows that break a rule: expected 0.
-- =============================================================================

WITH params AS (
    SELECT DATE '2024-11-01'      AS opening_date,             -- opening stock count (local midnight)
           'OB-20241101'::text    AS opening_reference,
           interval '15 minutes'  AS online_reservation,
           interval '5 minutes'   AS in_store_reservation,
           7                      AS warehouse_pre_launch_days,
           1                      AS store_pre_launch_days,
           interval '1 day'       AS max_in_transit,           -- dispatched, not arrived, this long before the latest event
           DATE '2024-11-01'      AS range_start,              -- INFO rows only (UTC dates, inclusive)
           DATE '2099-12-31'      AS range_end
),
loc AS (
    SELECT l.stock_location_id, l.location_code, l.location_type::text AS location_type, l.country_code,
           l.opened_on, c.time_zone
      FROM public.stock_locations l JOIN public.countries c USING (country_code)
),
mv AS (
    SELECT m.*, m.movement_type::text AS kind, l.location_code, l.location_type, l.country_code, l.time_zone,
           l.opened_on, (m.occurred_at AT TIME ZONE l.time_zone) AS local_at,
           CASE WHEN m.movement_type = 'purchase_receipt' THEN split_part(m.external_reference, '/', 1)
                WHEN m.movement_type IN ('transfer_out', 'transfer_in')
                THEN substring(m.external_reference FROM '^TR-[A-Z]{2}-[0-9]{4}-[0-9]{6}') END AS document_number
      FROM public.inventory_movements m JOIN loc l USING (stock_location_id)
),
pairs AS (
    SELECT o.movement_id AS out_id, o.stock_location_id AS out_location_id, o.location_type AS out_type,
           o.country_code AS out_country, o.product_id, o.quantity_change AS out_quantity, o.occurred_at AS out_at,
           o.external_reference AS reference, substr(o.external_reference, 19) AS destination,
           i.movement_id AS in_id, i.stock_location_id AS in_location_id, i.location_code AS in_location,
           i.location_type AS in_type, i.country_code AS in_country, i.product_id AS in_product_id,
           i.quantity_change AS in_quantity, i.occurred_at AS in_at, i.external_reference AS in_reference
      FROM mv o LEFT JOIN mv i ON i.kind = 'transfer_in' AND i.related_movement_id = o.movement_id
     WHERE o.kind = 'transfer_out'
),
running AS (
    SELECT m.stock_location_id, m.product_id,
           sum(m.quantity_change) OVER (PARTITION BY m.stock_location_id, m.product_id
                                        ORDER BY m.occurred_at, m.movement_id) AS on_hand_after
      FROM public.inventory_movements m
),
o AS (
    SELECT o.*, c.time_zone, c.vat_rate AS country_vat, c.currency_code AS country_currency,
           (o.placed_at AT TIME ZONE c.time_zone) AS placed_local,
           CASE WHEN o.order_number ~ '^[A-Z]{2}-[0-9]{4}-[0-9]{8}$' THEN substr(o.order_number, 9)::int END AS seq
      FROM public.orders o JOIN public.countries c USING (country_code)
),
lines AS (
    SELECT i.*, o.order_number, o.country_code, o.country_vat, o.placed_at, o.placed_local, o.status AS order_status,
           o.fulfilment_location_id, o.fulfilled_at
      FROM public.order_items i JOIN o USING (order_id)
),
res AS NOT MATERIALIZED (                -- S8.1: inlined, see Performance in the header
    SELECT r.*, i.order_id, i.quantity AS line_quantity, i.product_id AS line_product_id
      FROM public.inventory_reservations r JOIN public.order_items i USING (order_item_id)
),
sales AS NOT MATERIALIZED (
    SELECT m.*, i.order_id, i.quantity AS line_quantity
      FROM public.inventory_movements m JOIN public.order_items i USING (order_item_id)
     WHERE m.movement_type = 'sale'
),
pay AS NOT MATERIALIZED (
    SELECT p.*, max(p.attempt_number) OVER (PARTITION BY p.order_id) AS last_attempt,
           count(*) OVER (PARTITION BY p.order_id) AS attempts,
           lag(coalesce(p.captured_at, p.failed_at)) OVER (PARTITION BY p.order_id ORDER BY p.attempt_number) AS previous_result
      FROM public.payment_attempts p
),
latest AS (
    SELECT greatest((SELECT max(occurred_at) FROM public.inventory_movements),
                    (SELECT max(greatest(placed_at, confirmed_at, fulfilled_at, cancelled_at)) FROM public.orders),
                    (SELECT max(greatest(attempted_at, authorized_at, captured_at, failed_at)) FROM public.payment_attempts),
                    (SELECT max(greatest(reserved_at, closed_at)) FROM public.inventory_reservations)) AS at
),
checks (check_no, check_type, object_name, expected, actual) AS (
    -- 1. Ledger and conservation (every listing) ------------------------------------------
    SELECT 101, 'ledger', 'balances where on hand <> sum of movements', '0', count(*)::text
      FROM public.inventory_balances b
     WHERE b.quantity_on_hand <> coalesce((SELECT sum(m.quantity_change) FROM public.inventory_movements m
                                            WHERE m.stock_location_id = b.stock_location_id AND m.product_id = b.product_id), 0)
    UNION ALL
    SELECT 102, 'ledger', 'balances where reserved <> active reservations', '0', count(*)::text
      FROM public.inventory_balances b
     WHERE b.quantity_reserved <> coalesce((SELECT sum(r.quantity) FROM public.inventory_reservations r
                                             WHERE r.stock_location_id = b.stock_location_id AND r.product_id = b.product_id
                                               AND r.status = 'active'), 0)
    UNION ALL
    SELECT 103, 'ledger', 'negative stock or more reserved than on hand', '0', count(*)::text
      FROM public.inventory_balances WHERE quantity_on_hand < 0 OR quantity_reserved > quantity_on_hand
    UNION ALL
    SELECT 104, 'ledger', 'listings whose stock goes negative at some business time (movements in occurred_at order)', '0',
           count(DISTINCT (stock_location_id, product_id))::text
      FROM running WHERE on_hand_after < 0
    UNION ALL
    SELECT 105, 'ledger', 'conservation: on hand + in transit = every movement except transfers (signed)',
           (SELECT coalesce(sum(quantity_change), 0) FROM public.inventory_movements
             WHERE movement_type NOT IN ('transfer_out', 'transfer_in'))::text,
           ((SELECT coalesce(sum(quantity_on_hand), 0) FROM public.inventory_balances)
            + (SELECT coalesce(-sum(out_quantity), 0) FROM pairs WHERE in_id IS NULL))::text
    UNION ALL
    SELECT 106, 'ledger', 'opening balances not at the opening count (local midnight of the opening date) or with another reference', '0', count(*)::text
      FROM mv, params p
     WHERE kind = 'opening_balance'
       AND (occurred_at <> (p.opening_date::timestamp AT TIME ZONE time_zone)
            OR external_reference IS DISTINCT FROM p.opening_reference)
    UNION ALL
    SELECT 107, 'ledger', 'movements dated before the opening count of their location', '0', count(*)::text
      FROM mv, params p
     WHERE occurred_at < (p.opening_date::timestamp AT TIME ZONE time_zone)
    UNION ALL
    SELECT 108, 'ledger', 'business events dated in the future (orders, payments, reservations, movements)', '0',
           ((SELECT count(*) FROM public.orders WHERE greatest(placed_at, confirmed_at, fulfilled_at, cancelled_at) > now())
            + (SELECT count(*) FROM public.payment_attempts
                WHERE greatest(attempted_at, authorized_at, captured_at, failed_at) > now())
            + (SELECT count(*) FROM public.inventory_reservations WHERE greatest(reserved_at, closed_at) > now())
            + (SELECT count(*) FROM public.inventory_movements WHERE occurred_at > now()))::text
    UNION ALL
    SELECT 109, 'ledger', 'warehouses above their storage capacity now', '0', count(*)::text
      FROM public.warehouses wh
     WHERE wh.storage_capacity_units < (SELECT coalesce(sum(b.quantity_on_hand), 0) FROM public.inventory_balances b
                                         WHERE b.stock_location_id = wh.stock_location_id)
    UNION ALL
    SELECT 110, 'ledger', 'movement types not simulated yet (stock adjustments, write-offs, return restocks)', '0', count(*)::text
      FROM public.inventory_movements WHERE movement_type IN ('stock_adjustment', 'write_off', 'return_restock')

    -- 2. Documents and transfers ------------------------------------------------------------
    UNION ALL
    SELECT 201, 'documents', 'purchase receipts not at a warehouse, or reference not PO-<market>-<year>-<6 digits>/DN<n> of its market', '0', count(*)::text
      FROM mv
     WHERE kind = 'purchase_receipt'
       AND (location_type <> 'warehouse' OR external_reference IS NULL
            OR external_reference !~ '^PO-[A-Z]{2}-[0-9]{4}-[0-9]{6}/DN[0-9]+$'
            OR substr(external_reference, 4, 2) <> country_code)
    UNION ALL
    SELECT 202, 'documents', 'transfers out not from a warehouse, or reference not TR-<market>-<local year>-<6 digits>-<store of the market>', '0', count(*)::text
      FROM mv
     WHERE kind = 'transfer_out'
       AND (location_type <> 'warehouse' OR external_reference IS NULL
            OR external_reference !~ '^TR-[A-Z]{2}-[0-9]{4}-[0-9]{6}-.+$'
            OR substr(external_reference, 4, 2) <> country_code
            OR substr(external_reference, 7, 4) <> to_char(local_at, 'YYYY')
            OR NOT EXISTS (SELECT 1 FROM loc d JOIN public.stores s USING (stock_location_id)
                            WHERE d.location_code = substr(mv.external_reference, 19) AND d.country_code = mv.country_code))
    UNION ALL
    SELECT 203, 'documents', 'arrivals not matching their dispatch (product, quantity, reference, destination store of the market, not earlier)', '0', count(*)::text
      FROM pairs
     WHERE in_id IS NOT NULL
       AND (in_product_id <> product_id OR in_quantity <> -out_quantity OR in_reference IS DISTINCT FROM reference
            OR in_location <> destination OR in_type <> 'store' OR in_country <> out_country
            OR in_location_id = out_location_id OR in_at < out_at)
    UNION ALL
    SELECT 204, 'documents', 'document numbers used for more than one location, time or destination (PO, DN, TR)', '0',
           ((SELECT count(*) FROM (SELECT document_number FROM mv WHERE kind = 'purchase_receipt' AND document_number IS NOT NULL
                                    GROUP BY 1 HAVING count(DISTINCT stock_location_id) > 1) x)
            + (SELECT count(*) FROM (SELECT external_reference FROM mv WHERE kind = 'purchase_receipt'
                                       AND external_reference IS NOT NULL
                                     GROUP BY 1 HAVING count(DISTINCT occurred_at) > 1) x)
            + (SELECT count(*) FROM (SELECT document_number FROM mv WHERE kind = 'transfer_out' AND document_number IS NOT NULL
                                    GROUP BY 1 HAVING count(DISTINCT stock_location_id) > 1 OR count(DISTINCT occurred_at) > 1
                                        OR count(DISTINCT external_reference) > 1) x))::text
    UNION ALL
    SELECT 205, 'documents', 'receipts or transfers of a product not launched (pre-launch window) or discontinued', '0', count(*)::text
      FROM mv JOIN public.products p USING (product_id), params pr
     WHERE kind IN ('purchase_receipt', 'transfer_out', 'transfer_in')
       AND (p.launched_on IS NULL
            OR local_at::date < p.launched_on - CASE WHEN location_type = 'store' THEN pr.store_pre_launch_days
                                                     ELSE pr.warehouse_pre_launch_days END
            OR (p.discontinued_on IS NOT NULL AND p.discontinued_on <= local_at::date))
    UNION ALL
    SELECT 206, 'documents', 'transfers still in transit longer than the limit before the latest business event (lost)', '0', count(*)::text
      FROM pairs, params p, latest
     WHERE in_id IS NULL AND out_at < latest.at - p.max_in_transit
    UNION ALL
    SELECT 207, 'documents', 'movements at a location not open yet on the local day', '0', count(*)::text
      FROM mv WHERE opened_on > local_at::date

    -- 3. Order identity (every order) ---------------------------------------------------------
    UNION ALL
    SELECT 301, 'identity', 'order numbers not <market>-<local year of placed_at>-<8-digit global sequence>', '0', count(*)::text
      FROM o
     WHERE seq IS NULL OR substr(order_number, 1, 2) <> country_code
        OR substr(order_number, 4, 4) <> to_char(placed_local, 'YYYY')
    UNION ALL
    SELECT 302, 'identity', 'global order sequence numbers used by more than one order', '0', count(*)::text
      FROM (SELECT seq FROM o WHERE seq IS NOT NULL GROUP BY seq HAVING count(*) > 1) x
    UNION ALL
    SELECT 303, 'identity', 'gaps in the global order sequence (highest number - numbers used)', '0',
           coalesce((SELECT max(seq) - count(DISTINCT seq) FROM o WHERE seq IS NOT NULL), 0)::text
    UNION ALL
    SELECT 304, 'identity', 'orders not numbered in placed_at order', '0', count(*)::text
      FROM (SELECT placed_at, lag(placed_at) OVER (ORDER BY seq, order_number) AS previous FROM o WHERE seq IS NOT NULL) x
     WHERE previous > placed_at
    UNION ALL
    SELECT 305, 'identity', 'orders without a customer of their market registered at placed_at', '0', count(*)::text
      FROM o LEFT JOIN public.customers cu USING (customer_id)
     WHERE cu.customer_id IS NULL OR cu.country_code <> o.country_code OR o.placed_at < cu.registered_at
    UNION ALL
    SELECT 306, 'identity', 'orders not in the currency of their market', '0', count(*)::text
      FROM o WHERE currency_code <> country_currency

    -- 4. Order integrity -----------------------------------------------------------------------
    UNION ALL
    SELECT 401, 'money', 'orders without lines or with line numbers other than 1..n', '0', count(*)::text
      FROM o
     WHERE NOT EXISTS (SELECT 1 FROM public.order_items i WHERE i.order_id = o.order_id)
        OR (SELECT min(i.line_number) <> 1 OR max(i.line_number) <> count(*) FROM public.order_items i
             WHERE i.order_id = o.order_id)
    UNION ALL
    SELECT 402, 'money', 'lines whose total or VAT does not follow the formulas', '0', count(*)::text
      FROM lines l
     WHERE l.line_total <> l.quantity * l.unit_price - l.discount_amount
        OR l.vat_amount <> round(l.line_total * l.vat_rate / (1 + l.vat_rate), 2)
    UNION ALL
    SELECT 403, 'money', 'orders whose items total, shipping VAT or order total is wrong', '0', count(*)::text
      FROM o
     WHERE o.items_total <> (SELECT sum(line_total) FROM public.order_items i WHERE i.order_id = o.order_id)
        OR o.shipping_vat_amount <> round(o.shipping_fee * o.country_vat / (1 + o.country_vat), 2)
        OR o.order_total <> o.items_total + o.shipping_fee
    UNION ALL
    SELECT 404, 'money', 'lines whose price or cost is not the product_prices row valid at placed_at', '0', count(*)::text
      FROM lines l
     WHERE NOT EXISTS (SELECT 1 FROM public.product_prices pp
                        WHERE pp.product_id = l.product_id AND pp.country_code = l.country_code
                          AND pp.valid_from <= l.placed_at AND (pp.valid_to IS NULL OR l.placed_at < pp.valid_to)
                          AND pp.list_price = l.unit_price AND pp.unit_cost = l.unit_cost)
    UNION ALL
    SELECT 405, 'money', 'lines with another VAT rate than the market, or with a discount or promotion (none in the MVP)', '0', count(*)::text
      FROM lines l
     WHERE l.vat_rate <> l.country_vat OR l.discount_percent <> 0 OR l.discount_amount <> 0 OR l.promotion_id IS NOT NULL
    UNION ALL
    SELECT 406, 'catalogue', 'lines for a product not on sale on the local order day (not launched or discontinued)', '0', count(*)::text
      FROM lines l JOIN public.products p USING (product_id)
     WHERE p.launched_on IS NULL OR p.launched_on > l.placed_local::date
        OR (p.discontinued_on IS NOT NULL AND p.discontinued_on <= l.placed_local::date)

    -- 5. Payments, reservations and sales against the order status --------------------------------
    UNION ALL
    SELECT 501, 'lifecycle', 'payment attempts not numbered 1..n, for another amount than the order total, or cash online', '0', count(*)::text
      FROM pay JOIN o USING (order_id)
     WHERE pay.last_attempt <> pay.attempts OR pay.amount <> o.order_total
        OR (pay.payment_method = 'cash' AND o.channel <> 'in_store')
    UNION ALL
    SELECT 502, 'lifecycle', 'orders without exactly one capture when confirmed or fulfilled (at confirmed_at), or with a capture otherwise', '0', count(*)::text
      FROM o
     WHERE (SELECT count(*) FROM public.payment_attempts p WHERE p.order_id = o.order_id AND p.status = 'captured')
           <> CASE WHEN o.status IN ('confirmed', 'fulfilled') THEN 1 ELSE 0 END
        OR (o.status IN ('confirmed', 'fulfilled')
            AND o.confirmed_at IS DISTINCT FROM (SELECT max(p.captured_at) FROM public.payment_attempts p
                                                  WHERE p.order_id = o.order_id AND p.status = 'captured'))
    UNION ALL
    SELECT 503, 'lifecycle', 'payment, reservation, sale or status times before placed_at, or payment steps out of order', '0', count(*)::text
      FROM o
     WHERE least(o.confirmed_at, o.fulfilled_at, o.cancelled_at) < o.placed_at
        OR EXISTS (SELECT 1 FROM pay p WHERE p.order_id = o.order_id
                    AND (p.attempted_at < o.placed_at OR p.authorized_at < p.attempted_at OR p.captured_at < p.authorized_at
                         OR p.failed_at < p.attempted_at OR p.attempted_at < p.previous_result))
        OR EXISTS (SELECT 1 FROM res r WHERE r.order_id = o.order_id AND (r.reserved_at < o.placed_at OR r.closed_at < r.reserved_at))
        OR EXISTS (SELECT 1 FROM sales s WHERE s.order_id = o.order_id AND s.occurred_at < o.placed_at)
    UNION ALL
    SELECT 504, 'lifecycle', 'reservations not at the fulfilment location for the line''s product and quantity, or with the wrong lifetime', '0', count(*)::text
      FROM res JOIN o USING (order_id), params p
     WHERE res.stock_location_id <> o.fulfilment_location_id OR res.product_id <> res.line_product_id
        OR res.quantity <> res.line_quantity
        OR res.expires_at - res.reserved_at <> CASE o.channel WHEN 'online' THEN p.online_reservation
                                                              ELSE p.in_store_reservation END
    UNION ALL
    SELECT 505, 'lifecycle', 'orders whose reservations and sales do not match their status', '0', count(*)::text
      FROM o
     WHERE EXISTS (
             SELECT 1 FROM public.order_items i
              WHERE i.order_id = o.order_id
                AND ((o.status = 'fulfilled'
                      AND ((SELECT count(*) FROM sales s
                             WHERE s.order_item_id = i.order_item_id AND s.quantity_change = -i.quantity
                               AND s.stock_location_id = o.fulfilment_location_id AND s.occurred_at = o.fulfilled_at) <> 1
                           OR NOT EXISTS (SELECT 1 FROM res r WHERE r.order_item_id = i.order_item_id
                                            AND r.status = 'consumed' AND r.closed_at = o.fulfilled_at)))
                     OR (o.status IN ('stock_reserved', 'confirmed')
                         AND NOT EXISTS (SELECT 1 FROM res r WHERE r.order_item_id = i.order_item_id AND r.status = 'active'))))
        OR (o.status <> 'fulfilled' AND (EXISTS (SELECT 1 FROM sales s WHERE s.order_id = o.order_id)
                                         OR EXISTS (SELECT 1 FROM res r WHERE r.order_id = o.order_id AND r.status = 'consumed')))
        OR (o.status NOT IN ('stock_reserved', 'confirmed')
            AND EXISTS (SELECT 1 FROM res r WHERE r.order_id = o.order_id AND r.status = 'active'))
    UNION ALL
    SELECT 506, 'lifecycle', 'sales before the order was confirmed', '0', count(*)::text
      FROM sales s JOIN o USING (order_id)
     WHERE s.occurred_at < o.confirmed_at OR o.confirmed_at IS NULL

    -- 6. INFO for the date range (UTC dates of the business events) --------------------------------
    UNION ALL
    SELECT 601, 'info', 'orders placed in the range by market', NULL,
           (SELECT coalesce(string_agg(country_code || ' ' || n, ', ' ORDER BY country_code), 'none')
              FROM (SELECT o.country_code, count(*) AS n FROM o, params p
                     WHERE (o.placed_at AT TIME ZONE 'UTC')::date BETWEEN p.range_start AND p.range_end GROUP BY 1) x)
    UNION ALL
    SELECT 602, 'info', 'orders placed in the range by status now', NULL,
           (SELECT coalesce(string_agg(status || ' ' || n, ', ' ORDER BY status), 'none')
              FROM (SELECT o.status::text, count(*) AS n FROM o, params p
                     WHERE (o.placed_at AT TIME ZONE 'UTC')::date BETWEEN p.range_start AND p.range_end GROUP BY 1) x)
    UNION ALL
    SELECT 603, 'info', 'movements in the range by type (lines / units)', NULL,
           (SELECT coalesce(string_agg(kind || ' ' || n || ' / ' || units, ', ' ORDER BY kind), 'none')
              FROM (SELECT kind, count(*) AS n, sum(quantity_change) AS units FROM mv, params p
                     WHERE (occurred_at AT TIME ZONE 'UTC')::date BETWEEN p.range_start AND p.range_end GROUP BY 1) x)
    UNION ALL
    SELECT 604, 'info', 'listings by location type (all / with stock now)', NULL,
           (SELECT string_agg(location_type || ' ' || n || ' / ' || stocked, ', ' ORDER BY location_type)
              FROM (SELECT l.location_type, count(*) AS n, count(*) FILTER (WHERE b.quantity_on_hand > 0) AS stocked
                      FROM public.inventory_balances b JOIN loc l USING (stock_location_id) GROUP BY 1) x)
    UNION ALL
    SELECT 605, 'info', 'units now: on hand / reserved / in transit', NULL,
           ((SELECT coalesce(sum(quantity_on_hand), 0) || ' / ' || coalesce(sum(quantity_reserved), 0)
               FROM public.inventory_balances)
            || ' / ' || (SELECT coalesce(-sum(out_quantity), 0) FROM pairs WHERE in_id IS NULL))
    UNION ALL
    SELECT 606, 'info', 'open orders now (placed, stock_reserved, confirmed)', NULL,
           (SELECT count(*) FROM o WHERE status IN ('placed', 'stock_reserved', 'confirmed'))::text
    UNION ALL
    SELECT 607, 'info', 'latest business event (UTC)', NULL,
           (SELECT coalesce(to_char(at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'), 'none') FROM latest)
    UNION ALL
    SELECT 608, 'info', 'change_log rows (whole database, not yet extracted)', NULL,
           (SELECT count(*) FROM public.change_log)::text
)
SELECT check_no,
       check_type,
       object_name,
       expected,
       actual,
       CASE WHEN expected IS NULL THEN 'INFO'
            WHEN expected = actual THEN 'PASS'
            ELSE 'FAIL' END AS status,
       count(*) FILTER (WHERE expected IS NOT NULL AND expected IS DISTINCT FROM actual) OVER () AS failed_checks
  FROM checks
 ORDER BY check_no;
