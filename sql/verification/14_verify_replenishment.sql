-- =============================================================================
-- NordicCart OLTP | Verification 14: replenishment pilot
-- -----------------------------------------------------------------------------
-- Scope       : rows written by python/run_replenishment.py --apply (purchase
--               receipts, transfer_out / transfer_in pairs, the warehouse
--               reorder point revision R0) and the stock they change
-- Connect to  : nordiccart_oltp (any role that can read these tables)
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS or INFO, and failed_checks = 0
--               (44 rows: 37 PASS, 7 INFO).
-- When        : after the replenishment pilot is loaded (after the pilot
--               transactions, script 13), and after any later activity.
-- Revision 2 (2026-11-18, Phase 3 step 5 stage S1): scoped to the pilot's own records:
--               * receipts and transfers: the pilot's documents only, by document
--                 number (params.pilot_documents: 4 PO and 4 TR numbers); a later
--                 line added to a pilot document number is counted and fails 1xx;
--               * stock: the 320 listings of the 20 pilot products (params.pilot_skus);
--                 ledger, conservation and network units reconstructed in business
--                 time AT the pilot's last event (params.replenishment_last_event);
--               * 309 (new): no receipt or transfer outside the pilot's documents is
--                 dated at or before that last event. Later documents (combined
--                 activity from 2024-12-03) are outside this script: script 15.
--               * 507: warehouse capacity for the pilot's stock at that time.
--               Design note: the design proposed "dates up to 2024-12-06" (the review
--               window end); the combined activity starts on 2024-12-03, so the scope
--               is the pilot's documents and its last event instead.
-- Revision 3 (2026-11-18, stage S4, decision C18): 111, 401, 402, 403 and 605 read the
--               reorder points AS OF the pilot's last event (rop_end). With V009 they come
--               from the durable revision history (reorder_point_revisions: the latest
--               revision at or before that time, else the first later revision's old
--               value), so a later review (R-cal, stage S6) no longer changes them; before
--               V009 (no history table) they are the current values, as in revision 2.
--               The history is read through query_to_xml only when the table exists, so
--               the script still runs on databases without V009. Same 44 rows.
-- Parameters  : the params CTE holds the pilot counts (output/replenishment/pilot/
--               summary.json), the pilot's document numbers and products, and the
--               settings of config/generator.toml [replenishment]. If the config
--               changes, the dry run gives the new counts.
-- Why         : V007 keeps the ledger safe (no negative stock, no reserved units
--               moved, idempotent documents, matched transfer pairs) but does not
--               know where stock may go or when. This script checks the rules the
--               application enforces: receipts at warehouses, transfers warehouse ->
--               store of the same market to the store named in the reference,
--               launch and opening dates, working days and hours, nothing dated
--               inside the pilot, nothing left in transit, reorder-point triggers,
--               the running balance in business time, and conservation.
-- -----------------------------------------------------------------------------
-- Groups: 1 counts | 2 placement | 3 documents and timing | 4 policy
--         5 ledger and conservation | 6 INFO summaries
-- Integrity checks count the rows that break a rule: expected 0.
-- =============================================================================

WITH params AS (
    SELECT 5    AS expected_receipt_lines,
           26   AS expected_units_received,
           5    AS expected_delivery_notes,
           4    AS expected_purchase_orders,
           1    AS expected_split_orders,
           4    AS expected_transfer_documents,
           8    AS expected_transfer_lines,
           10   AS expected_units_transferred,
           533  AS expected_warehouse_reorder_points,       -- sum over the 80 warehouse listings after R0
           1467 AS expected_network_units,                  -- 1441 after the pilot + 26 received
           TIMESTAMPTZ '2024-11-15 08:44:22+00' AS pilot_last_event,
           TIMESTAMPTZ '2024-12-02 12:46:04+00' AS replenishment_last_event,   -- later records are outside the pilot
           ARRAY['PO-DK-2024-000001', 'PO-FI-2024-000001', 'PO-NO-2024-000001', 'PO-SE-2024-000001',
                 'TR-DK-2024-000001', 'TR-FI-2024-000001', 'TR-NO-2024-000001', 'TR-SE-2024-000001']::text[]
                AS pilot_documents,
           ARRAY['ACC-000010', 'ACC-000013', 'ACC-000015', 'ACC-000016', 'ACC-000017', 'CAM-000007',
                 'GAM-000008', 'HPH-000005', 'LAP-000002', 'LAP-000018', 'SMH-000009', 'SMH-000011',
                 'SPH-000001', 'SPH-000014', 'SPH-000019', 'TAB-000003', 'TVA-000004', 'WEA-000006',
                 'WEA-000012', 'WEA-000020']::text[] AS pilot_skus,
           DATE '2024-12-06' AS window_end,
           6    AS dispatch_from, 8 AS dispatch_to,          -- local hours [from, to)
           7    AS receiving_from, 15 AS receiving_to,
           10   AS store_opens,                             -- local hour; arrivals before it
           interval '105 minutes' AS max_transit,
           7    AS warehouse_pre_launch_days,
           1    AS store_pre_launch_days
),
loc AS (
    SELECT l.stock_location_id, l.location_code, l.location_type::text AS location_type, l.country_code,
           l.opened_on, l.is_active, c.time_zone
      FROM public.stock_locations l JOIN public.countries c USING (country_code)
),
-- Receipts and transfers with their document number (PO-<market>-<year>-<seq> or TR-<market>-<year>-<seq>).
docs AS (
    SELECT m.*, CASE WHEN m.movement_type = 'purchase_receipt' THEN split_part(m.external_reference, '/', 1)
                     ELSE substring(m.external_reference FROM '^TR-[A-Z]{2}-[0-9]{4}-[0-9]{6}') END AS document_number
      FROM public.inventory_movements m
     WHERE m.movement_type IN ('purchase_receipt', 'transfer_out', 'transfer_in')
),
-- The pilot's own receipts and transfers.
mv AS (
    SELECT m.*, m.movement_type::text AS kind, l.location_code, l.location_type, l.country_code, l.time_zone,
           (m.occurred_at AT TIME ZONE l.time_zone) AS local_at, p.sku, p.launched_on, p.discontinued_on,
           p.is_active AS product_active, l.opened_on, l.is_active AS location_active
      FROM docs m
      JOIN loc l USING (stock_location_id)
      JOIN public.products p USING (product_id)
     WHERE m.document_number = ANY ((SELECT pilot_documents FROM params)::text[])
),
-- The pilot's listings (20 products at every location) and their ledger up to the pilot's last event.
pl AS (
    SELECT b.* FROM public.inventory_balances b JOIN public.products p USING (product_id)
     WHERE p.sku = ANY ((SELECT pilot_skus FROM params)::text[])
),
ledger_at_end AS (
    SELECT m.* FROM public.inventory_movements m JOIN pl USING (stock_location_id, product_id), params p
     WHERE m.occurred_at <= p.replenishment_last_event
),
pairs AS (
    SELECT o.movement_id AS out_id, o.location_code AS out_location, o.location_type AS out_type,
           o.country_code AS out_country, o.product_id, o.quantity_change AS out_quantity, o.occurred_at AS out_at,
           o.local_at AS out_local, o.external_reference AS reference,
           regexp_replace(o.external_reference, '^TR-[A-Z]{2}-[0-9]{4}-[0-9]{6}-', '') AS destination,
           i.movement_id AS in_id, i.location_code AS in_location, i.location_type AS in_type, i.country_code AS in_country,
           i.product_id AS in_product_id, i.quantity_change AS in_quantity, i.occurred_at AS in_at, i.local_at AS in_local,
           i.external_reference AS in_reference, i.stock_location_id AS in_location_id
      FROM mv o LEFT JOIN mv i ON i.kind = 'transfer_in' AND i.related_movement_id = o.movement_id
     WHERE o.kind = 'transfer_out'
),
-- stock of one pilot listing at a business time, from the ledger (movements up to and including a given movement)
running AS (
    SELECT m.movement_id, m.stock_location_id, m.product_id, m.occurred_at,
           sum(m.quantity_change) OVER (PARTITION BY m.stock_location_id, m.product_id
                                        ORDER BY m.occurred_at, m.movement_id) AS on_hand_after
      FROM ledger_at_end m
),
reserved_at AS (
    SELECT x.movement_id,
           coalesce((SELECT sum(r.quantity) FROM public.inventory_reservations r
                      WHERE r.stock_location_id = x.stock_location_id AND r.product_id = x.product_id
                        AND r.reserved_at <= x.occurred_at AND (r.closed_at IS NULL OR r.closed_at > x.occurred_at)), 0) AS reserved
      FROM mv x
     WHERE x.kind = 'transfer_out'
),
-- C18 (V009): reorder points from the revision history, as of the pilot's last event; no rows before V009
rop_history AS (
    SELECT h.stock_location_id, h.product_id, h.reorder_point
      FROM params p,
           XMLTABLE('/table/row'
               PASSING CASE WHEN to_regclass('public.reorder_point_revisions') IS NULL THEN '<table/>'::xml
                            ELSE query_to_xml(format(
                                   'SELECT DISTINCT ON (stock_location_id, product_id) stock_location_id, product_id, '
                                || 'CASE WHEN effective_at <= %1$L THEN new_reorder_point ELSE old_reorder_point END AS reorder_point '
                                || 'FROM public.reorder_point_revisions '
                                || 'ORDER BY stock_location_id, product_id, (effective_at <= %1$L) DESC, '
                                || 'CASE WHEN effective_at <= %1$L THEN effective_at END DESC, effective_at',
                                   p.replenishment_last_event), false, false, '') END
               COLUMNS stock_location_id bigint  PATH 'stock_location_id',
                       product_id        bigint  PATH 'product_id',
                       reorder_point     integer PATH 'reorder_point') AS h
),
-- reorder point of every listing at the pilot's last event (history if any, else the current value)
rop_end AS (
    SELECT b.stock_location_id, b.product_id, coalesce(h.reorder_point, b.reorder_point) AS reorder_point
      FROM public.inventory_balances b LEFT JOIN rop_history h USING (stock_location_id, product_id)
),
-- the pilot's transfers still in transit at its last event
in_transit_at_end AS (
    SELECT coalesce(-sum(out_quantity), 0) AS units FROM pairs, params p
     WHERE out_at <= p.replenishment_last_event AND (in_id IS NULL OR in_at > p.replenishment_last_event)
),
checks (check_no, check_type, object_name, expected, actual) AS (
    -- 1. Counts -------------------------------------------------------------------------
    SELECT 101, 'count', 'purchase_receipt lines', (SELECT expected_receipt_lines FROM params)::text,
           (SELECT count(*) FROM mv WHERE kind = 'purchase_receipt')::text
    UNION ALL
    SELECT 102, 'count', 'units received', (SELECT expected_units_received FROM params)::text,
           (SELECT coalesce(sum(quantity_change), 0) FROM mv WHERE kind = 'purchase_receipt')::text
    UNION ALL
    SELECT 103, 'count', 'supplier delivery notes (distinct references)', (SELECT expected_delivery_notes FROM params)::text,
           (SELECT count(DISTINCT external_reference) FROM mv WHERE kind = 'purchase_receipt')::text
    UNION ALL
    SELECT 104, 'count', 'purchase orders (reference before /DN)', (SELECT expected_purchase_orders FROM params)::text,
           (SELECT count(DISTINCT split_part(external_reference, '/', 1)) FROM mv WHERE kind = 'purchase_receipt')::text
    UNION ALL
    SELECT 105, 'count', 'purchase orders delivered in more than one note (split)', (SELECT expected_split_orders FROM params)::text,
           (SELECT count(*) FROM (SELECT split_part(external_reference, '/', 1) FROM mv WHERE kind = 'purchase_receipt'
                                   GROUP BY 1 HAVING count(DISTINCT external_reference) > 1) x)::text
    UNION ALL
    SELECT 106, 'count', 'transfer documents (distinct references)', (SELECT expected_transfer_documents FROM params)::text,
           (SELECT count(DISTINCT external_reference) FROM mv WHERE kind = 'transfer_out')::text
    UNION ALL
    SELECT 107, 'count', 'transfer_out lines', (SELECT expected_transfer_lines FROM params)::text,
           (SELECT count(*) FROM mv WHERE kind = 'transfer_out')::text
    UNION ALL
    SELECT 108, 'count', 'transfer_in lines', (SELECT expected_transfer_lines FROM params)::text,
           (SELECT count(*) FROM mv WHERE kind = 'transfer_in')::text
    UNION ALL
    SELECT 109, 'count', 'units dispatched', (SELECT expected_units_transferred FROM params)::text,
           (SELECT coalesce(-sum(quantity_change), 0) FROM mv WHERE kind = 'transfer_out')::text
    UNION ALL
    SELECT 110, 'count', 'units in transit (dispatched, not arrived)', '0',
           (SELECT coalesce(-sum(out_quantity), 0) FROM pairs WHERE in_id IS NULL)::text
    UNION ALL
    SELECT 111, 'count', 'sum of warehouse reorder points of the pilot listings (R0: echelon demand)',
           (SELECT expected_warehouse_reorder_points FROM params)::text,
           (SELECT sum(r.reorder_point) FROM pl b JOIN rop_end r USING (stock_location_id, product_id)
              JOIN loc l USING (stock_location_id)
             WHERE l.location_type = 'warehouse')::text

    -- 2. Placement ------------------------------------------------------------------------
    UNION ALL
    SELECT 201, 'placement', 'purchase receipts not at a warehouse', '0', count(*)::text
      FROM mv WHERE kind = 'purchase_receipt' AND location_type <> 'warehouse'
    UNION ALL
    SELECT 202, 'placement', 'transfers out not from a warehouse', '0', count(*)::text
      FROM pairs WHERE out_type <> 'warehouse'
    UNION ALL
    SELECT 203, 'placement', 'transfers in not at a store', '0', count(*)::text
      FROM pairs WHERE in_id IS NOT NULL AND in_type <> 'store'
    UNION ALL
    SELECT 204, 'placement', 'transfers between different markets', '0', count(*)::text
      FROM pairs WHERE in_id IS NOT NULL AND in_country <> out_country
    UNION ALL
    SELECT 205, 'placement', 'arrivals at another location than the destination in the reference', '0', count(*)::text
      FROM pairs WHERE in_id IS NOT NULL AND in_location <> destination
    UNION ALL
    SELECT 206, 'placement', 'arrivals without the reference of their transfer_out', '0', count(*)::text
      FROM pairs WHERE in_id IS NOT NULL AND in_reference IS DISTINCT FROM reference
    UNION ALL
    SELECT 207, 'placement', 'receipts or transfers of a product not launched (pre-launch window) or discontinued', '0', count(*)::text
      FROM mv, params p
     WHERE NOT product_active OR launched_on IS NULL
        OR local_at::date < launched_on - CASE WHEN location_type = 'store' THEN p.store_pre_launch_days
                                               ELSE p.warehouse_pre_launch_days END
        OR (discontinued_on IS NOT NULL AND discontinued_on <= local_at::date)
    UNION ALL
    SELECT 208, 'placement', 'receipts or transfers at a location not open or inactive on the local day', '0', count(*)::text
      FROM mv WHERE NOT location_active OR opened_on > local_at::date

    -- 3. Documents and timing -------------------------------------------------------------
    UNION ALL
    SELECT 301, 'documents', 'receipt references not PO-<market>-<year>-<seq>/DN<n> of the warehouse''s market', '0', count(*)::text
      FROM mv WHERE kind = 'purchase_receipt'
       AND (external_reference !~ '^PO-[A-Z]{2}-[0-9]{4}-[0-9]{6}/DN[0-9]+$'
            OR substr(external_reference, 4, 2) <> country_code)
    UNION ALL
    SELECT 302, 'documents', 'transfer references not TR-<market>-<year>-<seq>-<store> of the market', '0', count(*)::text
      FROM mv WHERE kind = 'transfer_out'
       AND CASE WHEN external_reference ~ '^TR-[A-Z]{2}-[0-9]{4}-[0-9]{6}-.+$'
                THEN substr(external_reference, 4, 2) <> country_code
                     OR substr(external_reference, 7, 4)::int <> extract(year FROM local_at)::int
                ELSE true END
    UNION ALL
    SELECT 303, 'timing', 'dispatches not Monday to Friday within the dispatch hours (local)', '0', count(*)::text
      FROM mv, params p
     WHERE kind = 'transfer_out'
       AND (extract(isodow FROM local_at) > 5 OR extract(hour FROM local_at) < p.dispatch_from
            OR extract(hour FROM local_at) >= p.dispatch_to)
    UNION ALL
    SELECT 304, 'timing', 'receipts not Monday to Friday within the receiving hours (local)', '0', count(*)::text
      FROM mv, params p
     WHERE kind = 'purchase_receipt'
       AND (extract(isodow FROM local_at) > 5 OR extract(hour FROM local_at) < p.receiving_from
            OR extract(hour FROM local_at) >= p.receiving_to)
    UNION ALL
    SELECT 305, 'timing', 'arrivals not the same morning before the store opens (no Sundays, transit within the limit)', '0', count(*)::text
      FROM pairs, params p
     WHERE in_id IS NOT NULL
       AND (in_local::date <> out_local::date OR extract(isodow FROM in_local) = 7
            OR extract(hour FROM in_local) >= p.store_opens OR in_at - out_at > p.max_transit OR in_at < out_at)
    UNION ALL
    SELECT 306, 'timing', 'receipts or transfers dated at or before the pilot''s last event, after the window or in the future', '0', count(*)::text
      FROM mv, params p
     WHERE occurred_at <= p.pilot_last_event OR local_at::date > p.window_end OR occurred_at > now()
    UNION ALL
    SELECT 307, 'documents', 'transfer pairs with another product, quantity or the same location (V007 guard, checked again)', '0', count(*)::text
      FROM pairs WHERE in_id IS NOT NULL
       AND (in_product_id <> product_id OR in_quantity <> -out_quantity OR in_location = out_location)
    UNION ALL
    SELECT 309, 'timing', 'other receipts or transfers (not pilot documents) dated at or before the pilot''s last movement', '0', count(*)::text
      FROM docs d, params p
     WHERE d.occurred_at <= p.replenishment_last_event
       AND NOT coalesce(d.document_number = ANY (p.pilot_documents), false)

    -- 4. Policy ------------------------------------------------------------------------------
    UNION ALL
    SELECT 401, 'policy', 'transfers to a store whose available stock (+ stock in transit to it) was above its reorder point at dispatch', '0', count(*)::text
      FROM pairs t
      JOIN loc d ON d.location_code = t.destination
      JOIN rop_end b ON b.stock_location_id = d.stock_location_id AND b.product_id = t.product_id
     WHERE coalesce((SELECT sum(m.quantity_change) FROM public.inventory_movements m
                      WHERE m.stock_location_id = d.stock_location_id AND m.product_id = t.product_id
                        AND m.occurred_at < t.out_at), 0)
         - coalesce((SELECT sum(r.quantity) FROM public.inventory_reservations r
                      WHERE r.stock_location_id = d.stock_location_id AND r.product_id = t.product_id
                        AND r.reserved_at <= t.out_at AND (r.closed_at IS NULL OR r.closed_at > t.out_at)), 0)
         + coalesce((SELECT -sum(o2.out_quantity) FROM pairs o2
                      WHERE o2.destination = t.destination AND o2.product_id = t.product_id AND o2.out_id <> t.out_id
                        AND o2.out_at < t.out_at AND (o2.in_at IS NULL OR o2.in_at >= t.out_at)), 0)
         > b.reorder_point
    UNION ALL
    SELECT 402, 'policy', 'dispatches that left the warehouse below its reorder point', '0', count(*)::text
      FROM mv o
      JOIN running r ON r.movement_id = o.movement_id
      JOIN reserved_at ra ON ra.movement_id = o.movement_id
      JOIN rop_end b ON b.stock_location_id = o.stock_location_id AND b.product_id = o.product_id
     WHERE o.kind = 'transfer_out' AND r.on_hand_after - ra.reserved < b.reorder_point
    UNION ALL
    SELECT 403, 'policy', 'warehouse reorder points not below the opening balance (stocked pilot listings)', '0', count(*)::text
      FROM pl b
      JOIN rop_end r USING (stock_location_id, product_id)
      JOIN loc l USING (stock_location_id)
      JOIN public.inventory_movements o ON o.stock_location_id = b.stock_location_id AND o.product_id = b.product_id
                                       AND o.movement_type = 'opening_balance'
     WHERE l.location_type = 'warehouse' AND r.reorder_point >= o.quantity_change

    -- 5. Ledger and conservation (pilot listings; global versions in script 15) ---------------
    UNION ALL
    SELECT 501, 'ledger', 'pilot listings where on hand <> sum of movements', '0', count(*)::text
      FROM pl b
     WHERE b.quantity_on_hand <> coalesce((SELECT sum(m.quantity_change) FROM public.inventory_movements m
                                            WHERE m.stock_location_id = b.stock_location_id AND m.product_id = b.product_id), 0)
    UNION ALL
    SELECT 502, 'ledger', 'pilot listings where reserved <> active reservations', '0', count(*)::text
      FROM pl b
     WHERE b.quantity_reserved <> coalesce((SELECT sum(r.quantity) FROM public.inventory_reservations r
                                             WHERE r.stock_location_id = b.stock_location_id AND r.product_id = b.product_id
                                               AND r.status = 'active'), 0)
    UNION ALL
    SELECT 503, 'ledger', 'pilot listings with negative stock or more reserved than on hand', '0', count(*)::text
      FROM pl WHERE quantity_on_hand < 0 OR quantity_reserved > quantity_on_hand
    UNION ALL
    SELECT 504, 'ledger', 'pilot listings whose stock goes negative up to the pilot''s last movement (business time)', '0',
           count(DISTINCT (stock_location_id, product_id))::text
      FROM running WHERE on_hand_after < 0
    UNION ALL
    SELECT 505, 'ledger', 'conservation at the pilot''s last movement: on hand + in transit = opening + received + sold (signed)',
           (SELECT coalesce(sum(quantity_change) FILTER (WHERE movement_type IN ('opening_balance', 'purchase_receipt', 'sale')), 0)
              FROM ledger_at_end)::text,
           ((SELECT coalesce(sum(quantity_change), 0) FROM ledger_at_end) + (SELECT units FROM in_transit_at_end))::text
    UNION ALL
    SELECT 506, 'ledger', 'network units at the pilot''s last movement: on hand + in transit (1441 after the pilot + units received)',
           (SELECT expected_network_units FROM params)::text,
           ((SELECT coalesce(sum(quantity_change), 0) FROM ledger_at_end) + (SELECT units FROM in_transit_at_end))::text
    UNION ALL
    SELECT 507, 'ledger', 'warehouses whose pilot stock exceeded their storage capacity at the pilot''s last movement', '0', count(*)::text
      FROM public.warehouses wh
     WHERE wh.storage_capacity_units < (SELECT coalesce(sum(m.quantity_change), 0) FROM ledger_at_end m
                                         WHERE m.stock_location_id = wh.stock_location_id)

    -- 6. INFO summaries --------------------------------------------------------------------------
    UNION ALL
    SELECT 601, 'info', 'receipts by market (delivery notes / units)', NULL,
           (SELECT coalesce(string_agg(country_code || ' ' || notes || ' / ' || units, ', ' ORDER BY country_code), 'none')
              FROM (SELECT country_code, count(DISTINCT external_reference) AS notes, sum(quantity_change) AS units
                      FROM mv WHERE kind = 'purchase_receipt' GROUP BY 1) x)
    UNION ALL
    SELECT 602, 'info', 'transfers by market (documents / lines / units)', NULL,
           (SELECT coalesce(string_agg(out_country || ' ' || docs || ' / ' || lines || ' / ' || units, ', ' ORDER BY out_country), 'none')
              FROM (SELECT out_country, count(DISTINCT reference) AS docs, count(*) AS lines, -sum(out_quantity) AS units
                      FROM pairs GROUP BY 1) x)
    UNION ALL
    SELECT 603, 'info', 'units on hand at the pilot listings at the pilot''s last movement, by market and location type', NULL,
           (SELECT string_agg(country_code || ' ' || location_type || ' ' || units, ', ' ORDER BY country_code, location_type)
              FROM (SELECT l.country_code, l.location_type, sum(m.quantity_change) AS units
                      FROM ledger_at_end m JOIN loc l USING (stock_location_id) GROUP BY 1, 2) x)
    UNION ALL
    SELECT 604, 'info', 'pilot store listings on sale at or below their reorder point now', NULL,
           (SELECT count(*) FROM pl b JOIN loc l USING (stock_location_id)
              JOIN public.products p USING (product_id)
             WHERE l.location_type = 'store' AND p.launched_on <= (SELECT window_end FROM params)
               AND b.quantity_on_hand - b.quantity_reserved <= b.reorder_point)::text
    UNION ALL
    SELECT 605, 'info', 'warehouse reorder points of the pilot listings by market (sum, at the pilot''s last event)', NULL,
           (SELECT string_agg(country_code || ' ' || rop, ', ' ORDER BY country_code)
              FROM (SELECT l.country_code, sum(r.reorder_point) AS rop FROM pl b
                      JOIN rop_end r USING (stock_location_id, product_id)
                      JOIN loc l USING (stock_location_id) WHERE l.location_type = 'warehouse' GROUP BY 1) x)
    UNION ALL
    SELECT 606, 'info', 'first and last replenishment movement (UTC)', NULL,
           (SELECT coalesce(to_char(min(occurred_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || ' / '
                   || to_char(max(occurred_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'), 'none') FROM mv)
    UNION ALL
    SELECT 607, 'info', 'change_log rows for the pilot''s receipts and transfers (movements captured for the pipeline)', NULL,
           (SELECT count(*) FROM public.change_log
             WHERE table_name = 'inventory_movements'
               AND row_data ->> 'movement_type' IN ('purchase_receipt', 'transfer_out', 'transfer_in')
               AND (row_data ->> 'movement_id')::bigint IN (SELECT movement_id FROM mv))::text
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
