-- =============================================================================
-- NordicCart OLTP | Verification 11: master data after the Phase 3 seeder
-- -----------------------------------------------------------------------------
-- Scope       : rows written by python/run_master_data_seed.py --apply
--               (currencies, countries, product_categories, products,
--               product_prices, stock_locations, stores, warehouses, customers)
-- Connect to  : nordiccart_oltp (any role that can read these tables)
-- Safe to run : yes. Read-only: one SELECT statement, no writes, no temp objects.
-- Expected    : every row has status PASS or INFO, and failed_checks = 0
--               After S5 E1: 32 PASS, 11 INFO (populated rates checked by 16).
--               From S6 milestone 3: 32 PASS, 12 INFO (new INFO row 507).
--               (43 rows: 33 PASS, 10 INFO). Valid before and after the opening
--               inventory (script 12), the pilot transactions (script 13) and the
--               replenishment pilot (script 14).
-- Revision 2 (2026-11-17): checks 204 and 206 accept opening stock.
-- Revision 3 (2026-11-17): orders, order lines, payment attempts and
--               reservations (201, 202, 203, 205) are INFO counts, checked by
--               script 13; 204 accepts sale movements.
-- Revision 5 (2026-11-19, S6 M3): 105 counts the launch prices (first row per
--               product and market); P1 price changes are counted by INFO row 507
--               (501 one current price, 503 margin and 504 launch day still hold).
-- Revision 4 (2026-11-18): 204 accepts purchase receipts and transfers (checked
--               by script 14) and fails only on the movement types not simulated
--               yet (stock_adjustment, write_off, return_restock); new INFO row 208
--               counts receipts and transfers. Exchange rates still fail (207).
-- Profile     : the expected product and customer counts are in the params CTE:
--               20 and 100 for the pilot, 500 and 20000 for the full profile. The
--               profile is read from the data (20 products or fewer = pilot), so a
--               partial full-profile load fails the counts (revision 6, S7, C19).
-- Revision 6 (2026-11-20, S7): full-profile revision (decision C19): the
--               params CTE chooses the pilot or full-profile counts itself.
-- -----------------------------------------------------------------------------
-- Groups: 1 row counts | 2 business activity | 3 market reference values
--         4 locations | 5 products and prices | 6 customers | 7 INFO summaries
-- Integrity checks count the rows that break a rule: expected 0.
-- =============================================================================

WITH profile AS (                            -- S7: pilot (20 products) or full profile (any other count)
    SELECT (SELECT count(*) FROM public.products) <= 20 AS is_pilot
),
params AS (
    SELECT CASE WHEN is_pilot THEN 20 ELSE 500 END AS expected_products,
           CASE WHEN is_pilot THEN 100 ELSE 20000 END AS expected_customers
      FROM profile
),
market_tz AS (
    SELECT country_code, time_zone FROM public.countries
),
checks (check_no, check_type, object_name, expected, actual) AS (
    -- 1. Row counts ------------------------------------------------------------
    SELECT 101, 'row count', 'currencies', '4', (SELECT count(*) FROM public.currencies)::text
    UNION ALL SELECT 102, 'row count', 'countries', '4', (SELECT count(*) FROM public.countries)::text
    UNION ALL SELECT 103, 'row count', 'product_categories', '10', (SELECT count(*) FROM public.product_categories)::text
    UNION ALL SELECT 104, 'row count', 'products', (SELECT expected_products FROM params)::text,
                     (SELECT count(*) FROM public.products)::text
    UNION ALL SELECT 105, 'row count', 'product_prices: launch prices (one per product and market; later P1 rows: 507)',
                     (SELECT expected_products * 4 FROM params)::text,
                     (SELECT count(*) FROM public.product_prices pp
                       WHERE NOT EXISTS (SELECT 1 FROM public.product_prices e WHERE e.product_id = pp.product_id
                                          AND e.country_code = pp.country_code AND e.valid_from < pp.valid_from))::text
    UNION ALL SELECT 106, 'row count', 'stock_locations', '16', (SELECT count(*) FROM public.stock_locations)::text
    UNION ALL SELECT 107, 'row count', 'stores', '12', (SELECT count(*) FROM public.stores)::text
    UNION ALL SELECT 108, 'row count', 'warehouses', '4', (SELECT count(*) FROM public.warehouses)::text
    UNION ALL SELECT 109, 'row count', 'customers', (SELECT expected_customers FROM params)::text,
                     (SELECT count(*) FROM public.customers)::text

    -- 2. Business activity: opening stock (12), orders (13) and replenishment (14) allowed
    UNION ALL SELECT 201, 'info', 'orders, if loaded (checked by script 13)', NULL, (SELECT count(*) FROM public.orders)::text
    UNION ALL SELECT 202, 'info', 'order_items, if loaded (checked by script 13)', NULL, (SELECT count(*) FROM public.order_items)::text
    UNION ALL SELECT 203, 'info', 'payment_attempts, if loaded (checked by script 13)', NULL,
                     (SELECT count(*) FROM public.payment_attempts)::text
    UNION ALL SELECT 204, 'no activity yet', 'stock adjustments, write-offs and return restocks (not simulated yet)', '0',
                     (SELECT count(*) FROM public.inventory_movements
                       WHERE movement_type IN ('stock_adjustment', 'write_off', 'return_restock'))::text
    UNION ALL SELECT 205, 'info', 'inventory_reservations, if loaded (checked by script 13)', NULL,
                     (SELECT count(*) FROM public.inventory_reservations)::text
    UNION ALL SELECT 206, 'info', 'opening inventory, if loaded (checked by script 12)', NULL,
                     (SELECT count(*) || ' listings, ' || (SELECT count(*) FROM public.inventory_movements
                                                           WHERE movement_type = 'opening_balance') || ' opening balances'
                        FROM public.inventory_balances)
    -- Keep the original empty-pilot result. Once X1 runs, script 16 owns FX coverage.
    UNION ALL SELECT 207, 'row count', 'exchange_rates (checked by script 16 after E1)',
                     CASE WHEN count(*) = 0 THEN '0' END, count(*)::text FROM public.exchange_rates
    UNION ALL SELECT 208, 'info', 'purchase receipts and transfers, if loaded (checked by script 14)', NULL,
                     (SELECT count(*) FILTER (WHERE movement_type = 'purchase_receipt') || ' receipts, '
                             || count(*) FILTER (WHERE movement_type = 'transfer_out') || ' transfers out, '
                             || count(*) FILTER (WHERE movement_type = 'transfer_in') || ' transfers in'
                        FROM public.inventory_movements)

    -- 3. Market reference values (currency, standard VAT 2026, time zone) ---------
    UNION ALL
    SELECT 300 + e.n, 'market', e.country_code, e.expected,
           coalesce((SELECT c.country_name || ' | ' || c.currency_code || ' | ' || c.vat_rate || ' | ' || c.time_zone
                       FROM public.countries c WHERE c.country_code = e.country_code), 'missing')
      FROM (VALUES (1, 'SE', 'Sweden | SEK | 0.2500 | Europe/Stockholm'),
                   (2, 'NO', 'Norway | NOK | 0.2500 | Europe/Oslo'),
                   (3, 'DK', 'Denmark | DKK | 0.2500 | Europe/Copenhagen'),
                   (4, 'FI', 'Finland | EUR | 0.2550 | Europe/Helsinki')) AS e (n, country_code, expected)

    -- 4. Locations ----------------------------------------------------------------
    UNION ALL
    SELECT 401, 'locations', 'locations without exactly one matching store or warehouse row', '0',
           count(*)::text
      FROM public.stock_locations l
     WHERE (SELECT count(*) FROM public.stores s WHERE s.stock_location_id = l.stock_location_id)
         + (SELECT count(*) FROM public.warehouses w WHERE w.stock_location_id = l.stock_location_id) <> 1
        OR (l.location_type = 'store') <> EXISTS (SELECT 1 FROM public.stores s WHERE s.stock_location_id = l.stock_location_id)
    UNION ALL
    SELECT 402, 'locations', 'location codes not <country>-STO-nn / <country>-WH-nn', '0', count(*)::text
      FROM public.stock_locations l
     WHERE l.location_code !~ '^(SE|NO|DK|FI)-(STO|WH)-[0-9]{2}$'
        OR left(l.location_code, 2) <> l.country_code
        OR (split_part(l.location_code, '-', 2) = 'STO') <> (l.location_type = 'store')
    UNION ALL
    SELECT 403, 'locations', 'markets without a warehouse that ships online orders', '0', count(*)::text
      FROM public.countries c
     WHERE NOT EXISTS (SELECT 1 FROM public.warehouses w JOIN public.stock_locations l USING (stock_location_id)
                        WHERE l.country_code = c.country_code AND w.ships_online_orders)
    UNION ALL
    SELECT 404, 'locations', 'markets without a store', '0', count(*)::text
      FROM public.countries c
     WHERE NOT EXISTS (SELECT 1 FROM public.stock_locations l WHERE l.country_code = c.country_code AND l.location_type = 'store')
    UNION ALL
    SELECT 405, 'locations', 'stores with a format other than flagship or standard', '0', count(*)::text
      FROM public.stores WHERE store_format NOT IN ('flagship', 'standard')

    -- 5. Products and prices ----------------------------------------------------------
    UNION ALL
    SELECT 501, 'prices', 'product and market pairs without exactly one current price', '0', count(*)::text
      FROM public.products p CROSS JOIN public.countries c
     WHERE (SELECT count(*) FROM public.product_prices pp
             WHERE pp.product_id = p.product_id AND pp.country_code = c.country_code AND pp.valid_to IS NULL) <> 1
    UNION ALL
    SELECT 502, 'prices', 'prices not in the market currency', '0', count(*)::text
      FROM public.product_prices pp JOIN public.countries c USING (country_code)
     WHERE pp.currency_code <> c.currency_code
    UNION ALL
    SELECT 503, 'prices', 'prices whose unit cost is not below the net price (no margin)', '0', count(*)::text
      FROM public.product_prices pp JOIN public.countries c USING (country_code)
     WHERE pp.unit_cost >= pp.list_price / (1 + c.vat_rate)
    UNION ALL
    SELECT 504, 'prices', 'prices valid before the product launch day (market time)', '0', count(*)::text
      FROM public.product_prices pp
      JOIN public.products p USING (product_id)
      JOIN market_tz t USING (country_code)
     WHERE (pp.valid_from AT TIME ZONE t.time_zone)::date < p.launched_on
    UNION ALL
    SELECT 507, 'info', 'price changes after the launch price (P1 markdowns of the activity engine, S6)', NULL,
           (SELECT count(*) FROM public.product_prices pp
             WHERE EXISTS (SELECT 1 FROM public.product_prices e WHERE e.product_id = pp.product_id
                            AND e.country_code = pp.country_code AND e.valid_from < pp.valid_from))::text
    UNION ALL
    SELECT 505, 'products', 'categories without products', '0', count(*)::text
      FROM public.product_categories pc
     WHERE NOT EXISTS (SELECT 1 FROM public.products p WHERE p.category_id = pc.category_id)
    UNION ALL
    SELECT 506, 'products', 'products with a sku not like SPH-000001', '0', count(*)::text
      FROM public.products WHERE sku !~ '^[A-Z]{3}-[0-9]{6}$'

    -- 6. Customers --------------------------------------------------------------------
    UNION ALL
    SELECT 601, 'customers', 'emails not lower case or repeated ignoring case', '0',
           ((SELECT count(*) FROM public.customers WHERE email <> lower(email))
          + (SELECT count(*) - count(DISTINCT lower(email)) FROM public.customers))::text
    UNION ALL
    SELECT 602, 'customers', 'customer numbers not like C00000001', '0', count(*)::text
      FROM public.customers WHERE customer_number !~ '^C[0-9]{8}$'
    UNION ALL
    SELECT 603, 'customers', 'partial addresses (address, postal code and city go together)', '0', count(*)::text
      FROM public.customers
     WHERE (address_line1 IS NULL) <> (postal_code IS NULL) OR (address_line1 IS NULL) <> (city IS NULL)
    UNION ALL
    SELECT 604, 'customers', 'registered before the first NordicCart location in their market opened', '0', count(*)::text
      FROM public.customers cu JOIN market_tz t USING (country_code)
     WHERE (cu.registered_at AT TIME ZONE t.time_zone)::date
           < (SELECT min(l.opened_on) FROM public.stock_locations l WHERE l.country_code = cu.country_code)
    UNION ALL
    SELECT 605, 'customers', 'registered in the future', '0', count(*)::text
      FROM public.customers WHERE registered_at > now()
    UNION ALL
    SELECT 606, 'customers', 'younger than 18 when they registered', '0', count(*)::text
      FROM public.customers cu JOIN market_tz t USING (country_code)
     WHERE cu.birth_date > ((cu.registered_at AT TIME ZONE t.time_zone)::date - interval '18 years')::date
    UNION ALL
    SELECT 607, 'customers', 'in-store sign-ups with no store open in their market that day', '0', count(*)::text
      FROM public.customers cu JOIN market_tz t USING (country_code)
     WHERE cu.signup_channel = 'in_store'
       AND NOT EXISTS (SELECT 1 FROM public.stock_locations l
                        WHERE l.country_code = cu.country_code AND l.location_type = 'store'
                          AND l.opened_on <= (cu.registered_at AT TIME ZONE t.time_zone)::date)

    -- 7. INFO summaries ----------------------------------------------------------------
    UNION ALL
    SELECT 701, 'info', 'customers per market (in-store sign-ups)', NULL,
           (SELECT string_agg(country_code || ' ' || n || ' (' || s || ')', ', ' ORDER BY country_code)
              FROM (SELECT country_code, count(*) AS n, count(*) FILTER (WHERE signup_channel = 'in_store') AS s
                      FROM public.customers GROUP BY country_code) x)
    UNION ALL
    SELECT 702, 'info', 'products per category', NULL,
           (SELECT string_agg(category_code || ' ' || n, ', ' ORDER BY category_code)
              FROM (SELECT pc.category_code, count(p.product_id) AS n
                      FROM public.product_categories pc LEFT JOIN public.products p USING (category_id)
                     GROUP BY pc.category_code) x)
    UNION ALL
    SELECT 703, 'info', 'customer registrations, first and last', NULL,
           (SELECT min(registered_at)::date || ' .. ' || max(registered_at)::date FROM public.customers)
    UNION ALL
    SELECT 704, 'info', 'change_log rows for these tables (captured for the pipeline)', NULL,
           (SELECT count(*) FROM public.change_log
             WHERE table_name IN ('currencies', 'countries', 'product_categories', 'products', 'product_prices',
                                  'stock_locations', 'stores', 'warehouses', 'customers'))::text
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
