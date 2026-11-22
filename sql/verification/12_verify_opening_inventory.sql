-- =============================================================================
-- NordicCart OLTP | Verification 12: opening inventory
-- -----------------------------------------------------------------------------
-- Scope       : rows written by python/run_opening_inventory.py --apply
--               (inventory_balances listings, opening_balance movements)
-- Connect to  : nordiccart_oltp (any role that can read these tables)
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS or INFO, and failed_checks = 0
--               (24 rows: 17 PASS, 7 INFO).
-- When        : after loading the opening inventory, and still after the pilot
--               transactions and the replenishment pilot (sales, receipts and
--               transfers change the stock; the opening checks read the
--               opening_balance movements, which never change).
-- Revision 2 (2026-11-17): 201 accepts sale movements; 202 and 203 are INFO
--               counts (orders are checked by script 13); 501, 502 and 602 use the
--               opening quantities instead of the current stock; 603 lists the
--               listings without opening stock.
-- Revision 4 (2026-11-19, S6 M3): 501 compares with the reorder point IN FORCE AT
--               THE OPENING: the first revision's old value (V009 history) or, for a
--               listing never revised, the current one. The monthly R-cal review
--               (S6) may later set reorder points above an opening quantity. Before
--               V009 (no history table) 501 compares with the current value, as in
--               revision 3; the history is read through query_to_xml so the script
--               still parses there. Still one read-only SELECT.
-- Revision 3 (2026-11-18): 201 accepts purchase receipts and transfers (checked by
--               script 14) and fails only on stock_adjustment, write_off and
--               return_restock; new INFO row 204 counts receipts and transfers.
--               501 compares with the CURRENT reorder point: the replenishment
--               pilot revises warehouse reorder points (R0) and keeps each one
--               below the listing's opening quantity, so 501 stays PASS.
-- Profile     : the expected counts are in the params CTE: 320 listings, 288
--               opening balances and 1460 units for the pilot; 8000, 6096 and
--               19866 for the full profile (the 20 pilot products as loaded plus the
--               demand-calibrated opening stock of the others, decision C3; the old
--               opening model's 28109 is not used). Chosen from the data (revision 5).
-- Revision 5 (2026-11-20, S7): full-profile revision (decision C19).
-- -----------------------------------------------------------------------------
-- Groups: 1 counts | 2 other stock activity | 3 ledger identity | 4 opening rules
--         5 stock levels | 6 INFO summaries
-- Integrity checks count the rows that break a rule: expected 0.
-- =============================================================================

WITH profile AS (                            -- S7: pilot (20 products) or full profile (any other count)
    SELECT (SELECT count(*) FROM public.products) <= 20 AS is_pilot
),
params AS (
    SELECT CASE WHEN is_pilot THEN 320 ELSE 8000 END     AS expected_listings,
           CASE WHEN is_pilot THEN 288 ELSE 6096 END     AS expected_openings,
           CASE WHEN is_pilot THEN 1460 ELSE 19866 END   AS expected_units,   -- full: 1460 pilot + 18406 calibrated (C3)
           DATE '2024-11-01' AS opening_date,
           'OB-20241101'::text AS opening_reference
      FROM profile
),
loc AS (
    SELECT l.stock_location_id, l.location_code, l.location_type, l.country_code, l.opened_on, l.is_active, c.time_zone
      FROM public.stock_locations l JOIN public.countries c USING (country_code)
),
opening AS (
    SELECT m.* FROM public.inventory_movements m WHERE m.movement_type = 'opening_balance'
),
checks (check_no, check_type, object_name, expected, actual) AS (
    -- 1. Counts ------------------------------------------------------------------
    SELECT 101, 'count', 'inventory_balances (one listing per product and location)',
           (SELECT expected_listings FROM params)::text, (SELECT count(*) FROM public.inventory_balances)::text
    UNION ALL
    SELECT 102, 'count', 'opening_balance movements', (SELECT expected_openings FROM params)::text,
           (SELECT count(*) FROM opening)::text
    UNION ALL
    SELECT 103, 'count', 'opening units', (SELECT expected_units FROM params)::text,
           (SELECT coalesce(sum(quantity_change), 0) FROM opening)::text
    UNION ALL
    SELECT 104, 'count', 'product and location pairs without a listing', '0', count(*)::text
      FROM public.products p CROSS JOIN public.stock_locations l
     WHERE NOT EXISTS (SELECT 1 FROM public.inventory_balances b
                        WHERE b.product_id = p.product_id AND b.stock_location_id = l.stock_location_id)
    UNION ALL
    SELECT 105, 'count', 'listings with more than one opening balance', '0', count(*)::text
      FROM (SELECT 1 FROM opening GROUP BY stock_location_id, product_id HAVING count(*) > 1) x

    -- 2. Other stock activity (orders: script 13; receipts and transfers: script 14) --------
    UNION ALL SELECT 201, 'nothing else yet', 'stock adjustments, write-offs and return restocks (not simulated yet)', '0',
                     (SELECT count(*) FROM public.inventory_movements
                       WHERE movement_type IN ('stock_adjustment', 'write_off', 'return_restock'))::text
    UNION ALL SELECT 202, 'info', 'inventory_reservations, if orders are loaded (checked by script 13)', NULL,
                     (SELECT count(*) FROM public.inventory_reservations)::text
    UNION ALL SELECT 203, 'info', 'orders, if loaded (checked by script 13)', NULL, (SELECT count(*) FROM public.orders)::text
    UNION ALL SELECT 204, 'info', 'purchase receipts and transfers, if loaded (checked by script 14)', NULL,
                     (SELECT count(*) FILTER (WHERE movement_type = 'purchase_receipt') || ' receipts, '
                             || count(*) FILTER (WHERE movement_type = 'transfer_out') || ' transfers out, '
                             || count(*) FILTER (WHERE movement_type = 'transfer_in') || ' transfers in'
                        FROM public.inventory_movements)

    -- 3. Ledger identity (V007 keeps the balance equal to its ledger) -------------------------
    UNION ALL
    SELECT 301, 'ledger', 'balances where on hand <> sum of movements', '0', count(*)::text
      FROM public.inventory_balances b
     WHERE b.quantity_on_hand <> coalesce((SELECT sum(m.quantity_change) FROM public.inventory_movements m
                                            WHERE m.stock_location_id = b.stock_location_id AND m.product_id = b.product_id), 0)
    UNION ALL
    SELECT 302, 'ledger', 'balances where reserved <> active reservations', '0', count(*)::text
      FROM public.inventory_balances b
     WHERE b.quantity_reserved <> coalesce((SELECT sum(r.quantity) FROM public.inventory_reservations r
                                             WHERE r.stock_location_id = b.stock_location_id AND r.product_id = b.product_id
                                               AND r.status = 'active'), 0)
    UNION ALL
    SELECT 303, 'ledger', 'negative stock or more reserved than on hand', '0', count(*)::text
      FROM public.inventory_balances WHERE quantity_on_hand < 0 OR quantity_reserved > quantity_on_hand

    -- 4. Opening rules ------------------------------------------------------------------------
    UNION ALL
    SELECT 401, 'opening', 'opening balances not dated 00:00 local time on the opening day', '0', count(*)::text
      FROM opening o JOIN loc l USING (stock_location_id)
     WHERE (o.occurred_at AT TIME ZONE l.time_zone) <> (SELECT opening_date FROM params)::timestamp
    UNION ALL
    SELECT 402, 'opening', 'opening balances without the stock count reference', '0', count(*)::text
      FROM opening WHERE external_reference IS DISTINCT FROM (SELECT opening_reference FROM params)
    UNION ALL
    SELECT 403, 'opening', 'opening stock for products launched after the opening day', '0', count(*)::text
      FROM opening o JOIN public.products p USING (product_id)
     WHERE p.launched_on > (SELECT opening_date FROM params)
    UNION ALL
    SELECT 404, 'opening', 'products on sale on the opening day without opening stock at an open location', '0', count(*)::text
      FROM public.inventory_balances b
      JOIN public.products p USING (product_id)
      JOIN loc l USING (stock_location_id)
     WHERE p.launched_on <= (SELECT opening_date FROM params) AND p.is_active
       AND l.opened_on <= (SELECT opening_date FROM params) AND l.is_active
       AND NOT EXISTS (SELECT 1 FROM opening o WHERE o.stock_location_id = b.stock_location_id AND o.product_id = b.product_id)
    UNION ALL
    SELECT 405, 'opening', 'opening balances at a location that had not opened yet', '0', count(*)::text
      FROM opening o JOIN loc l USING (stock_location_id)
     WHERE l.opened_on > (SELECT opening_date FROM params)

    -- 5. Stock levels (opening quantities, which later sales do not change) ----------------------
    UNION ALL
    SELECT 501, 'stock', 'opening balances not above the reorder point in force at the opening', '0',
           CASE WHEN to_regclass('public.reorder_point_revisions') IS NULL       -- before V009: no revision history
                THEN (SELECT count(*) FROM opening o JOIN public.inventory_balances b USING (stock_location_id, product_id)
                       WHERE b.reorder_point >= o.quantity_change)::text
                ELSE (xpath('/row/n/text()', query_to_xml(
                        'SELECT count(*) AS n FROM public.inventory_movements o '
                        'JOIN public.inventory_balances b USING (stock_location_id, product_id) '
                        'WHERE o.movement_type = ''opening_balance'' AND coalesce((SELECT v.old_reorder_point '
                        'FROM public.reorder_point_revisions v WHERE v.stock_location_id = b.stock_location_id '
                        'AND v.product_id = b.product_id ORDER BY v.effective_at, v.reorder_point_revision_id LIMIT 1), '
                        'b.reorder_point) >= o.quantity_change', false, true, '')))[1]::text
           END
    UNION ALL
    SELECT 502, 'stock', 'warehouses opening with less of a product than a store of the same market', '0', count(*)::text
      FROM opening w
      JOIN loc wl ON wl.stock_location_id = w.stock_location_id AND wl.location_type = 'warehouse'
     WHERE w.quantity_change < (SELECT max(s.quantity_change) FROM opening s
                                  JOIN loc sl ON sl.stock_location_id = s.stock_location_id AND sl.location_type = 'store'
                                 WHERE sl.country_code = wl.country_code AND s.product_id = w.product_id)
    UNION ALL
    SELECT 503, 'stock', 'warehouses above their storage capacity', '0', count(*)::text
      FROM public.warehouses wh
     WHERE wh.storage_capacity_units < (SELECT coalesce(sum(b.quantity_on_hand), 0) FROM public.inventory_balances b
                                         WHERE b.stock_location_id = wh.stock_location_id)

    -- 6. INFO summaries --------------------------------------------------------------------------
    UNION ALL
    SELECT 601, 'info', 'units on hand now, by market and location type', NULL,
           (SELECT string_agg(country_code || ' ' || location_type || ' ' || units, ', ' ORDER BY country_code, location_type)
              FROM (SELECT l.country_code, l.location_type, sum(b.quantity_on_hand) AS units
                      FROM public.inventory_balances b JOIN loc l USING (stock_location_id)
                     GROUP BY l.country_code, l.location_type) x)
    UNION ALL
    SELECT 602, 'info', 'opening stock value at unit cost, by market (local currency)', NULL,
           (SELECT string_agg(country_code || ' ' || value || ' ' || currency_code, ', ' ORDER BY country_code)
              FROM (SELECT l.country_code, pp.currency_code, sum(b.quantity_change * pp.unit_cost) AS value
                      FROM opening b
                      JOIN loc l USING (stock_location_id)
                      JOIN public.product_prices pp ON pp.product_id = b.product_id AND pp.country_code = l.country_code
                       AND pp.valid_from <= (SELECT opening_date FROM params)::timestamp AT TIME ZONE l.time_zone
                       AND (pp.valid_to IS NULL OR pp.valid_to > (SELECT opening_date FROM params)::timestamp AT TIME ZONE l.time_zone)
                     GROUP BY l.country_code, pp.currency_code) x)
    UNION ALL
    SELECT 603, 'info', 'listings without opening stock (product not on sale yet)', NULL,
           (SELECT count(*) || ' (' || coalesce(string_agg(DISTINCT p.sku, ', '), '') || ')'
              FROM public.inventory_balances b JOIN public.products p USING (product_id)
             WHERE NOT EXISTS (SELECT 1 FROM opening o
                                WHERE o.stock_location_id = b.stock_location_id AND o.product_id = b.product_id))
    UNION ALL
    SELECT 604, 'info', 'change_log rows for the inventory tables (captured for the pipeline)', NULL,
           (SELECT count(*) FROM public.change_log WHERE table_name IN ('inventory_balances', 'inventory_movements'))::text
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
