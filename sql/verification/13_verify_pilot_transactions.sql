-- =============================================================================
-- NordicCart OLTP | Verification 13: pilot transactions
-- -----------------------------------------------------------------------------
-- Scope       : rows written by python/run_transactions.py --apply (orders,
--               order_items, payment_attempts, inventory_reservations, sale
--               movements and the stock they change)
-- Connect to  : nordiccart_oltp (any role that can read these tables)
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS or INFO, and failed_checks = 0
--               (52 rows: 45 PASS, 7 INFO).
-- When        : after the pilot transactions are loaded, after the replenishment
--               pilot, and after any later activity (combined activity, stage S9).
-- Revision 2 (2026-11-18): 504 accepts purchase receipts and transfers dated after
--               the pilot's last event (params.pilot_last_event); 505 expects the
--               pilot's 1441 units plus the net change of those later movements.
-- Revision 3 (2026-11-18, Phase 3 step 5 stage S1): scoped to the pilot's own
--               records, so later activity cannot turn it red and cannot hide in it:
--               * orders, lines, payments, reservations and sales: the 20 pilot order
--                 numbers only (params.pilot_orders);
--               * stock: the 320 listings of the 20 pilot products (params.pilot_skus),
--                 reconstructed in business time at the pilot's last event;
--               * 209 (new): no other order is dated inside the pilot;
--               * 504: no movement dated inside the pilot other than opening balances
--                 and the pilot orders' sales (later replenishment is after it);
--               * 505: units on hand at the pilot listings AT the pilot's last event
--                 (from the ledger in business time) = 1441, whatever happens later;
--               * 607 (new, INFO): activity outside the pilot.
--               The global ledger, conservation and activity checks are in script 15.
-- Parameters  : the params CTE holds the pilot counts, the pilot's order numbers and
--               products, and the business settings of config/generator.toml
--               [transactions]. If the config changes, the dry run's summary.json
--               gives the new counts and output/transactions/pilot/orders.csv the numbers.
-- -----------------------------------------------------------------------------
-- Groups: 1 counts | 2 customers, products, locations and dates | 3 money
--         4 order lifecycle | 5 stock | 6 INFO summaries
-- Integrity checks count the rows that break a rule: expected 0.
-- =============================================================================

WITH params AS (
    SELECT 20                AS expected_orders,
           'cancelled 4, fulfilled 14, payment_failed 2'::text            AS expected_statuses,
           'customer_request 1, out_of_stock 2, reservation_expired 1'::text AS expected_reasons,
           26                AS expected_lines,
           'captured 14, failed 7'::text                                  AS expected_attempts,
           'consumed 17, expired 2, released 4'::text                     AS expected_reservations,
           'sale lines 17, units 19'::text                                AS expected_sales,
           1441              AS expected_units_on_hand,       -- 1460 opening units - 19 sold
           TIMESTAMPTZ '2024-11-15 08:44:22+00' AS pilot_last_event,   -- later records are outside the pilot
           ARRAY['NO-2024-00000001', 'FI-2024-00000002', 'NO-2024-00000003', 'SE-2024-00000004',
                 'SE-2024-00000005', 'FI-2024-00000006', 'SE-2024-00000007', 'DK-2024-00000008',
                 'NO-2024-00000009', 'SE-2024-00000010', 'SE-2024-00000011', 'SE-2024-00000012',
                 'DK-2024-00000013', 'DK-2024-00000014', 'SE-2024-00000015', 'NO-2024-00000016',
                 'SE-2024-00000017', 'NO-2024-00000018', 'SE-2024-00000019', 'DK-2024-00000020']::text[] AS pilot_orders,
           ARRAY['ACC-000010', 'ACC-000013', 'ACC-000015', 'ACC-000016', 'ACC-000017', 'CAM-000007',
                 'GAM-000008', 'HPH-000005', 'LAP-000002', 'LAP-000018', 'SMH-000009', 'SMH-000011',
                 'SPH-000001', 'SPH-000014', 'SPH-000019', 'TAB-000003', 'TVA-000004', 'WEA-000006',
                 'WEA-000012', 'WEA-000020']::text[] AS pilot_skus,
           DATE '2024-11-01' AS window_start,
           DATE '2024-11-14' AS window_end,
           DATE '2024-11-01' AS opening_date,
           3                 AS max_attempts,
           interval '15 minutes' AS online_reservation,
           interval '5 minutes'  AS in_store_reservation,
           10                AS store_opens,                  -- local hour
           19                AS store_closes,
           interval '5 minutes'  AS in_store_handover,
           interval '48 hours'   AS online_dispatch
),
shipping (country_code, fee, free_from) AS (
    VALUES ('SE', 49.00, 499.00), ('NO', 59.00, 599.00), ('DK', 39.00, 399.00), ('FI', 4.90, 49.00)
),
-- The pilot's own orders (by order number) and every row that belongs to them.
o AS (
    SELECT o.*, c.time_zone, c.vat_rate AS country_vat, c.currency_code AS country_currency,
           (o.placed_at AT TIME ZONE c.time_zone) AS placed_local
      FROM public.orders o JOIN public.countries c USING (country_code)
     WHERE o.order_number = ANY ((SELECT pilot_orders FROM params)::text[])
),
lines AS (
    SELECT i.*, o.order_number, o.country_code, o.placed_at, o.placed_local, o.status AS order_status,
           o.fulfilment_location_id, o.fulfilled_at
      FROM public.order_items i JOIN o USING (order_id)
),
res AS (
    SELECT r.*, i.order_id, i.quantity AS line_quantity, i.product_id AS line_product_id
      FROM public.inventory_reservations r JOIN public.order_items i USING (order_item_id)
     WHERE i.order_id IN (SELECT order_id FROM o)
),
sales AS (
    SELECT m.*, i.order_id, i.quantity AS line_quantity
      FROM public.inventory_movements m JOIN public.order_items i USING (order_item_id)
     WHERE m.movement_type = 'sale' AND i.order_id IN (SELECT order_id FROM o)
),
pay AS (
    SELECT p.*, max(p.attempt_number) OVER (PARTITION BY p.order_id) AS last_attempt,
           lag(coalesce(p.captured_at, p.failed_at)) OVER (PARTITION BY p.order_id ORDER BY p.attempt_number) AS previous_result
      FROM public.payment_attempts p
     WHERE p.order_id IN (SELECT order_id FROM o)
),
-- The pilot's listings: the 20 pilot products at every location (320 balances).
pl AS (
    SELECT b.* FROM public.inventory_balances b JOIN public.products p USING (product_id)
     WHERE p.sku = ANY ((SELECT pilot_skus FROM params)::text[])
),
checks (check_no, check_type, object_name, expected, actual) AS (
    -- 1. Counts -------------------------------------------------------------------------
    SELECT 101, 'count', 'orders', (SELECT expected_orders FROM params)::text, (SELECT count(*) FROM o)::text
    UNION ALL
    SELECT 102, 'count', 'orders by final status', (SELECT expected_statuses FROM params),
           (SELECT string_agg(status || ' ' || n, ', ' ORDER BY status)
              FROM (SELECT status::text, count(*) AS n FROM o GROUP BY status) x)
    UNION ALL
    SELECT 103, 'count', 'cancelled orders by reason', (SELECT expected_reasons FROM params),
           (SELECT string_agg(cancellation_reason || ' ' || n, ', ' ORDER BY cancellation_reason)
              FROM (SELECT cancellation_reason, count(*) AS n FROM o WHERE status = 'cancelled' GROUP BY 1) x)
    UNION ALL
    SELECT 104, 'count', 'order lines', (SELECT expected_lines FROM params)::text, (SELECT count(*) FROM lines)::text
    UNION ALL
    SELECT 105, 'count', 'payment attempts by status', (SELECT expected_attempts FROM params),
           (SELECT string_agg(status || ' ' || n, ', ' ORDER BY status)
              FROM (SELECT status::text, count(*) AS n FROM pay GROUP BY status) x)
    UNION ALL
    SELECT 106, 'count', 'reservations by status', (SELECT expected_reservations FROM params),
           (SELECT string_agg(status || ' ' || n, ', ' ORDER BY status)
              FROM (SELECT status::text, count(*) AS n FROM res GROUP BY status) x)
    UNION ALL
    SELECT 107, 'count', 'sale movements and units sold', (SELECT expected_sales FROM params),
           (SELECT 'sale lines ' || count(*) || ', units ' || coalesce(-sum(quantity_change), 0) FROM sales)
    UNION ALL
    SELECT 108, 'count', 'orders still open (placed, stock_reserved or confirmed)', '0',
           (SELECT count(*) FROM o WHERE status IN ('placed', 'stock_reserved', 'confirmed'))::text
    UNION ALL
    SELECT 109, 'count', 'active reservations of pilot orders', '0',
           (SELECT count(*) FROM res WHERE status = 'active')::text

    -- 2. Customers, products, locations and dates -----------------------------------------
    UNION ALL
    SELECT 201, 'consistency', 'orders without a customer or by a customer of another market', '0', count(*)::text
      FROM o LEFT JOIN public.customers cu USING (customer_id)
     WHERE cu.customer_id IS NULL OR cu.country_code <> o.country_code
    UNION ALL
    SELECT 202, 'consistency', 'orders placed before the customer registered, or by an inactive customer', '0', count(*)::text
      FROM o JOIN public.customers cu USING (customer_id)
     WHERE o.placed_at < cu.registered_at OR NOT cu.is_active
    UNION ALL
    SELECT 203, 'consistency', 'order lines for a product not on sale on the local order day', '0', count(*)::text
      FROM lines l JOIN public.products p USING (product_id)
     WHERE NOT p.is_active OR p.launched_on IS NULL OR p.launched_on > l.placed_local::date
        OR (p.discontinued_on IS NOT NULL AND p.discontinued_on <= l.placed_local::date)
    UNION ALL
    SELECT 204, 'consistency', 'orders at a location that was not open on the local order day', '0', count(*)::text
      FROM o JOIN public.stock_locations sl ON sl.stock_location_id = o.fulfilment_location_id
     WHERE sl.opened_on > o.placed_local::date OR NOT sl.is_active
    UNION ALL
    SELECT 205, 'consistency', 'orders placed outside the pilot window or before the opening stock count', '0', count(*)::text
      FROM o, params p
     WHERE o.placed_local::date NOT BETWEEN p.window_start AND p.window_end
        OR o.placed_local < p.opening_date::timestamp
    UNION ALL
    SELECT 206, 'consistency', 'online orders not shipped to the customer''s address', '0', count(*)::text
      FROM o JOIN public.customers cu USING (customer_id)
     WHERE o.channel = 'online'
       AND (o.shipping_address_line1, o.shipping_postal_code, o.shipping_city)
           IS DISTINCT FROM (cu.address_line1, cu.postal_code, cu.city)
    UNION ALL
    SELECT 207, 'consistency', 'in-store orders on a Sunday or outside opening hours', '0', count(*)::text
      FROM o, params p
     WHERE o.channel = 'in_store'
       AND (extract(isodow FROM o.placed_local) = 7
            OR extract(hour FROM o.placed_local) < p.store_opens OR extract(hour FROM o.placed_local) >= p.store_closes)
    UNION ALL
    SELECT 208, 'consistency', 'business events dated in the future', '0', count(*)::text
      FROM o
     WHERE greatest(o.placed_at, o.confirmed_at, o.fulfilled_at, o.cancelled_at) > now()
    UNION ALL
    SELECT 209, 'consistency', 'other orders (not pilot orders) placed at or before the pilot''s last event', '0', count(*)::text
      FROM public.orders x, params p
     WHERE x.order_number <> ALL (p.pilot_orders) AND x.placed_at <= p.pilot_last_event

    -- 3. Money ------------------------------------------------------------------------------
    UNION ALL
    SELECT 301, 'money', 'orders not in the currency of their market', '0', count(*)::text
      FROM o WHERE o.currency_code <> o.country_currency
    UNION ALL
    SELECT 302, 'money', 'lines whose price or cost is not the product_prices row valid at placed_at', '0', count(*)::text
      FROM lines l
     WHERE NOT EXISTS (SELECT 1 FROM public.product_prices pp
                        WHERE pp.product_id = l.product_id AND pp.country_code = l.country_code
                          AND pp.valid_from <= l.placed_at AND (pp.valid_to IS NULL OR l.placed_at < pp.valid_to)
                          AND pp.list_price = l.unit_price AND pp.unit_cost = l.unit_cost)
    UNION ALL
    SELECT 303, 'money', 'lines with another VAT rate than the market, or with a discount', '0', count(*)::text
      FROM lines l JOIN public.countries c USING (country_code)
     WHERE l.vat_rate <> c.vat_rate OR l.discount_percent <> 0 OR l.promotion_id IS NOT NULL
    UNION ALL
    SELECT 304, 'money', 'lines whose total or VAT does not follow the formulas', '0', count(*)::text
      FROM lines l
     WHERE l.line_total <> l.quantity * l.unit_price - l.discount_amount
        OR l.vat_amount <> round(l.line_total * l.vat_rate / (1 + l.vat_rate), 2)
    UNION ALL
    SELECT 305, 'money', 'orders whose items total, shipping VAT or order total is wrong', '0', count(*)::text
      FROM o
     WHERE o.items_total <> (SELECT sum(line_total) FROM public.order_items i WHERE i.order_id = o.order_id)
        OR o.shipping_vat_amount <> round(o.shipping_fee * o.country_vat / (1 + o.country_vat), 2)
        OR o.order_total <> o.items_total + o.shipping_fee
    UNION ALL
    SELECT 306, 'money', 'orders with the wrong shipping fee (online below the free threshold pays the fee)', '0', count(*)::text
      FROM o JOIN shipping s USING (country_code)
     WHERE o.shipping_fee <> CASE WHEN o.channel = 'online' AND o.items_total < s.free_from THEN s.fee ELSE 0 END
    UNION ALL
    SELECT 307, 'money', 'payment attempts whose amount is not the order total, or cash online', '0', count(*)::text
      FROM public.payment_attempts p JOIN o USING (order_id)
     WHERE p.amount <> o.order_total OR (p.payment_method = 'cash' AND o.channel <> 'in_store')

    -- 4. Order lifecycle ---------------------------------------------------------------------
    UNION ALL
    SELECT 401, 'lifecycle', 'fulfilled orders without exactly one capture, confirmed at the capture time', '0', count(*)::text
      FROM o
     WHERE o.status = 'fulfilled'
       AND ((SELECT count(*) FROM public.payment_attempts p WHERE p.order_id = o.order_id AND p.status = 'captured') <> 1
            OR o.confirmed_at IS DISTINCT FROM (SELECT p.captured_at FROM public.payment_attempts p
                                                 WHERE p.order_id = o.order_id AND p.status = 'captured'))
    UNION ALL
    SELECT 402, 'lifecycle', 'captures that are not the last attempt, or more attempts than allowed', '0', count(*)::text
      FROM pay, params p
     WHERE (pay.status = 'captured' AND pay.attempt_number <> pay.last_attempt) OR pay.attempt_number > p.max_attempts
    UNION ALL
    SELECT 403, 'lifecycle', 'payment attempts out of time order (placed, attempted, authorized, captured or failed, next attempt)', '0', count(*)::text
      FROM pay JOIN o USING (order_id)
     WHERE pay.attempted_at < o.placed_at OR pay.authorized_at < pay.attempted_at OR pay.captured_at < pay.authorized_at
        OR pay.failed_at < pay.attempted_at OR pay.attempted_at < pay.previous_result
    UNION ALL
    SELECT 404, 'lifecycle', 'reservations with the wrong lifetime (15 min online, 5 min in store)', '0', count(*)::text
      FROM res JOIN o USING (order_id), params p
     WHERE res.expires_at - res.reserved_at <> CASE o.channel WHEN 'online' THEN p.online_reservation ELSE p.in_store_reservation END
    UNION ALL
    SELECT 405, 'lifecycle', 'reservations not matching the order line and fulfilment location, or reserved before placed_at', '0', count(*)::text
      FROM res JOIN o USING (order_id)
     WHERE res.stock_location_id <> o.fulfilment_location_id OR res.product_id <> res.line_product_id
        OR res.quantity <> res.line_quantity OR res.reserved_at < o.placed_at
    UNION ALL
    SELECT 406, 'lifecycle', 'payment results after the reservation expired', '0', count(*)::text
      FROM pay p
     WHERE coalesce(p.captured_at, p.failed_at) >= (SELECT min(r.expires_at) FROM res r WHERE r.order_id = p.order_id)
    UNION ALL
    SELECT 407, 'lifecycle', 'payment_failed orders: an attempt not failed, or not closed at the last failure', '0', count(*)::text
      FROM o
     WHERE o.status = 'payment_failed'
       AND (EXISTS (SELECT 1 FROM public.payment_attempts p WHERE p.order_id = o.order_id AND p.status <> 'failed')
            OR o.cancelled_at IS DISTINCT FROM (SELECT max(p.failed_at) FROM public.payment_attempts p WHERE p.order_id = o.order_id))
    UNION ALL
    SELECT 408, 'lifecycle', 'payment_failed and customer_request orders whose reservations are not released at cancelled_at', '0', count(*)::text
      FROM o
     WHERE (o.status = 'payment_failed' OR o.cancellation_reason = 'customer_request')
       AND (NOT EXISTS (SELECT 1 FROM res r WHERE r.order_id = o.order_id)
            OR EXISTS (SELECT 1 FROM res r WHERE r.order_id = o.order_id
                        AND (r.status <> 'released' OR r.closed_at <> o.cancelled_at)))
    UNION ALL
    SELECT 409, 'lifecycle', 'reservation_expired orders not expired by the sweeper after expires_at, or with a payment', '0', count(*)::text
      FROM o
     WHERE o.cancellation_reason = 'reservation_expired'
       AND (NOT EXISTS (SELECT 1 FROM res r WHERE r.order_id = o.order_id)
            OR EXISTS (SELECT 1 FROM res r WHERE r.order_id = o.order_id
                        AND (r.status <> 'expired' OR r.closed_at < r.expires_at OR r.closed_at <> o.cancelled_at))
            OR EXISTS (SELECT 1 FROM public.payment_attempts p WHERE p.order_id = o.order_id AND p.status IN ('authorized', 'captured')))
    UNION ALL
    SELECT 410, 'lifecycle', 'out_of_stock orders with a reservation or a payment attempt', '0', count(*)::text
      FROM o
     WHERE o.cancellation_reason = 'out_of_stock'
       AND (EXISTS (SELECT 1 FROM res r WHERE r.order_id = o.order_id)
            OR EXISTS (SELECT 1 FROM public.payment_attempts p WHERE p.order_id = o.order_id))
    UNION ALL
    SELECT 411, 'lifecycle', 'out_of_stock orders where every line was available when they were cancelled', '0', count(*)::text
      FROM o
     WHERE o.cancellation_reason = 'out_of_stock'
       AND NOT EXISTS (
            SELECT 1 FROM public.order_items i
             WHERE i.order_id = o.order_id
               AND coalesce((SELECT sum(m.quantity_change) FROM public.inventory_movements m
                              WHERE m.stock_location_id = o.fulfilment_location_id AND m.product_id = i.product_id
                                AND m.occurred_at <= o.cancelled_at), 0)
                 - coalesce((SELECT sum(r.quantity) FROM public.inventory_reservations r
                              WHERE r.stock_location_id = o.fulfilment_location_id AND r.product_id = i.product_id
                                AND r.reserved_at <= o.cancelled_at AND (r.closed_at IS NULL OR r.closed_at > o.cancelled_at)), 0)
                 < i.quantity)
    UNION ALL
    SELECT 412, 'lifecycle', 'fulfilled orders without a consumed reservation and a sale of each line at fulfilled_at', '0', count(*)::text
      FROM lines l
     WHERE l.order_status = 'fulfilled'
       AND (NOT EXISTS (SELECT 1 FROM res r WHERE r.order_item_id = l.order_item_id AND r.status = 'consumed'
                          AND r.closed_at = l.fulfilled_at)
            OR NOT EXISTS (SELECT 1 FROM sales s WHERE s.order_item_id = l.order_item_id
                             AND s.quantity_change = -l.quantity AND s.occurred_at = l.fulfilled_at
                             AND s.stock_location_id = l.fulfilment_location_id))
    UNION ALL
    SELECT 413, 'lifecycle', 'fulfilment delays: in store over 5 minutes, online over 48 hours', '0', count(*)::text
      FROM o, params p
     WHERE o.status = 'fulfilled'
       AND (o.fulfilled_at <= o.confirmed_at
            OR o.fulfilled_at - o.confirmed_at > CASE o.channel WHEN 'online' THEN p.online_dispatch ELSE p.in_store_handover END)
    UNION ALL
    SELECT 414, 'lifecycle', 'sale movements of orders that are not fulfilled', '0', count(*)::text
      FROM sales s JOIN o USING (order_id) WHERE o.status <> 'fulfilled'

    -- 5. Stock (the pilot's 320 listings; the global ledger checks are in script 15) -----------
    UNION ALL
    SELECT 501, 'stock', 'pilot listings where on hand <> sum of movements', '0', count(*)::text
      FROM pl b
     WHERE b.quantity_on_hand <> coalesce((SELECT sum(m.quantity_change) FROM public.inventory_movements m
                                            WHERE m.stock_location_id = b.stock_location_id AND m.product_id = b.product_id), 0)
    UNION ALL
    SELECT 502, 'stock', 'pilot listings where reserved <> active reservations', '0', count(*)::text
      FROM pl b
     WHERE b.quantity_reserved <> coalesce((SELECT sum(r.quantity) FROM public.inventory_reservations r
                                             WHERE r.stock_location_id = b.stock_location_id AND r.product_id = b.product_id
                                               AND r.status = 'active'), 0)
    UNION ALL
    SELECT 503, 'stock', 'pilot listings with negative stock or more reserved than on hand', '0', count(*)::text
      FROM pl WHERE quantity_on_hand < 0 OR quantity_reserved > quantity_on_hand
    UNION ALL
    SELECT 504, 'stock', 'movements dated at or before the pilot''s last event other than opening balances and pilot sales', '0', count(*)::text
      FROM public.inventory_movements m, params p
     WHERE m.occurred_at <= p.pilot_last_event
       AND m.movement_type <> 'opening_balance'
       AND NOT (m.movement_type = 'sale' AND m.order_item_id IN (SELECT order_item_id FROM lines))
    UNION ALL
    SELECT 505, 'stock', 'units on hand at the pilot listings at the pilot''s last event (1460 opening - 19 sold)',
           (SELECT expected_units_on_hand FROM params)::text,
           (SELECT coalesce(sum(m.quantity_change), 0)
              FROM public.inventory_movements m JOIN pl USING (stock_location_id, product_id), params p
             WHERE m.occurred_at <= p.pilot_last_event)::text
    UNION ALL
    SELECT 506, 'stock', 'sales dated before the order was confirmed or at another location', '0', count(*)::text
      FROM sales s JOIN o USING (order_id)
     WHERE s.occurred_at < o.confirmed_at OR s.stock_location_id <> o.fulfilment_location_id

    -- 6. INFO summaries --------------------------------------------------------------------------
    UNION ALL
    SELECT 601, 'info', 'orders by market and channel', NULL,
           (SELECT string_agg(country_code || ' ' || channel || ' ' || n, ', ' ORDER BY country_code, channel)
              FROM (SELECT country_code, channel::text, count(*) AS n FROM o GROUP BY 1, 2) x)
    UNION ALL
    SELECT 602, 'info', 'fulfilled revenue excl. VAT, cost and gross profit by market (local currency)', NULL,
           (SELECT string_agg(country_code || ' ' || revenue || ' / ' || cogs || ' / ' || (revenue - cogs) || ' ' || currency_code,
                              ', ' ORDER BY country_code)
              FROM (SELECT l.country_code, o.currency_code, sum(l.line_total - l.vat_amount) AS revenue,
                           sum(l.quantity * l.unit_cost) AS cogs
                      FROM lines l JOIN o USING (order_id) WHERE l.order_status = 'fulfilled' GROUP BY 1, 2) x)
    UNION ALL
    SELECT 603, 'info', 'shipping fees charged on fulfilled orders (incl. VAT)', NULL,
           (SELECT coalesce(string_agg(country_code || ' ' || fees || ' ' || currency_code, ', ' ORDER BY country_code), 'none')
              FROM (SELECT country_code, currency_code, sum(shipping_fee) AS fees FROM o
                     WHERE status = 'fulfilled' AND shipping_fee > 0 GROUP BY 1, 2) x)
    UNION ALL
    SELECT 604, 'info', 'payment attempts by method and status', NULL,
           (SELECT string_agg(payment_method || ' ' || status || ' ' || n, ', ' ORDER BY payment_method, status)
              FROM (SELECT payment_method::text, status::text, count(*) AS n FROM public.payment_attempts GROUP BY 1, 2) x)
    UNION ALL
    SELECT 605, 'info', 'first order placed and last order fulfilled (UTC)', NULL,
           (SELECT to_char(min(placed_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || ' / '
                   || to_char(max(fulfilled_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') FROM o)
    UNION ALL
    SELECT 606, 'info', 'change_log rows for the order and inventory tables (whole database)', NULL,
           (SELECT string_agg(table_name || ' ' || n, ', ' ORDER BY table_name)
              FROM (SELECT table_name, count(*) AS n FROM public.change_log
                     WHERE table_name IN ('orders', 'order_items', 'payment_attempts', 'inventory_reservations',
                                          'inventory_movements', 'inventory_balances') GROUP BY 1) x)
    UNION ALL
    SELECT 607, 'info', 'outside the pilot: other orders / movements dated after the pilot''s last event (checked by script 15)', NULL,
           (SELECT (SELECT count(*) FROM public.orders x WHERE x.order_number <> ALL (p.pilot_orders)) || ' orders / '
                   || (SELECT count(*) FROM public.inventory_movements m WHERE m.occurred_at > p.pilot_last_event) || ' movements'
              FROM params p)
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
