-- S5. Check the complete E1 window and every subsequent loaded day.
-- S10: rates may run to [demand] generation_end 2027-10-31 (hourly generation after calendar.simulation_end).
WITH bounds AS (
    SELECT DATE '2024-11-01' AS first_day,
           least(DATE '2027-10-31', greatest(DATE '2024-12-02', max(rate_date))) AS last_day
    FROM public.exchange_rates
), anchors(quote, centre, band) AS (
    VALUES ('SEK',11.0671,0.15), ('NOK',11.7182,0.15), ('DKK',7.4634,0.003)
), expected AS (
    SELECT first_day + i AS day, a.* FROM bounds
    CROSS JOIN LATERAL generate_series(0, last_day-first_day) i CROSS JOIN anchors a
), checks(check_no, description, violations) AS (
    SELECT 101, 'Missing EUR rates per calendar date and pair', count(*)
    FROM expected e LEFT JOIN public.exchange_rates r ON r.rate_date=e.day
        AND r.base_currency_code='EUR' AND r.quote_currency_code=e.quote
    WHERE r.exchange_rate_id IS NULL
    UNION ALL
    SELECT 102, 'Pair, date, source, precision or band violations', count(*)
    FROM public.exchange_rates r LEFT JOIN anchors a ON a.quote=r.quote_currency_code
    WHERE a.quote IS NULL OR r.base_currency_code <> 'EUR' OR r.source <> 'synthetic_ecb'
       OR r.rate_date NOT BETWEEN DATE '2024-11-01' AND DATE '2027-10-31'
       OR r.rate <> round(r.rate,4) OR abs(r.rate-a.centre)>a.band*a.centre
    UNION ALL
    SELECT 103, 'Closed days differing from previous day', count(*)
    FROM public.exchange_rates r LEFT JOIN public.exchange_rates p
      ON p.rate_date=r.rate_date-1 AND p.base_currency_code=r.base_currency_code
      AND p.quote_currency_code=r.quote_currency_code
    WHERE (extract(isodow from r.rate_date)>=6 OR to_char(r.rate_date,'MM-DD') IN ('01-01','05-01','12-25','12-26')
      OR r.rate_date IN (DATE '2025-04-18',DATE '2025-04-21',DATE '2026-04-03',DATE '2026-04-06',
                         DATE '2027-03-26',DATE '2027-03-29'))
      AND r.rate IS DISTINCT FROM p.rate
    UNION ALL
    SELECT 104, 'Fulfilled non-EUR orders missing local fulfilment-date rate', count(*)
    FROM public.orders o JOIN public.countries c USING (country_code)
    WHERE o.status='fulfilled' AND o.currency_code <> 'EUR' AND NOT EXISTS (
      SELECT FROM public.exchange_rates r WHERE r.base_currency_code='EUR' AND r.quote_currency_code=o.currency_code
      AND r.rate_date=(o.fulfilled_at AT TIME ZONE c.time_zone)::date)
)
SELECT check_no, description, violations, CASE WHEN violations=0 THEN 'PASS' ELSE 'FAIL' END AS status
FROM checks ORDER BY check_no;
