"""Daily synthetic EUR reference rates and transactional X1 loading."""
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext

from .demand.numeric import normal
from .master_data.rules import stream

D = Decimal
TICK = D("0.0001")
DAY = timedelta(days=1)
E1_END = date(2024, 12, 2)
SIGMA = {"SEK": D(".004"), "NOK": D(".005"), "DKK": D(".0004")}
# TARGET Good Friday and Easter Monday within the approved simulation calendar (2027: S10 generation horizon).
EASTER_CLOSURES = {date(2025, 4, 18), date(2025, 4, 21), date(2026, 4, 3), date(2026, 4, 6),
                   date(2027, 3, 26), date(2027, 3, 29)}


def working_day(day):
    return (day.weekday() < 5 and day not in EASTER_CLOSURES
            and (day.month, day.day) not in {(1, 1), (5, 1), (12, 25), (12, 26)})


def daily_rates(settings, day, previous):
    """Produce one X1 triplet from the preceding day's four-decimal state."""
    if not settings.simulation_start <= day <= settings.activity_end:
        raise ValueError("Rate date outside simulation calendar")
    rates = {}
    with localcontext() as ctx:
        ctx.prec = 34
        for quote, volatility in SIGMA.items():
            anchor = settings.pricing.reference_rates[quote]
            prior = previous[quote]
            if not working_day(day):
                rates[quote] = prior
                continue
            shock = normal(stream(settings.seed, "exchange_rates", quote, day.isoformat()))
            displacement = (prior / anchor).ln()
            rate = anchor * (D(".97") * displacement + volatility * shock).exp()
            band = D(".003") if quote == "DKK" else D(".15")
            lo = (anchor * (1 - band)).quantize(TICK, rounding=ROUND_CEILING)
            hi = (anchor * (1 + band)).quantize(TICK, rounding=ROUND_FLOOR)
            rates[quote] = min(hi, max(lo, rate.quantize(TICK)))
    return rates


def reconcile(conn, settings, start, end, *, apply=False, confirmed=None):
    """Verify stored history, then plan or atomically insert missing requested rows."""
    if not settings.simulation_start <= start <= end <= settings.activity_end:
        raise ValueError("Invalid exchange-rate window")
    with conn.transaction():
        if not apply:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        else:
            conn.execute("SET LOCAL lock_timeout = '10s'")
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('nordiccart:X1', 0))")
        if not conn.execute("SELECT EXISTS (SELECT FROM meta.schema_migrations WHERE version='009')").fetchone()[0]:
            raise ValueError("S5 requires V009")
        stored = {}
        latest = None
        for day, base, quote, rate, source in conn.execute(
                "SELECT rate_date,base_currency_code,quote_currency_code,rate,source FROM public.exchange_rates"):
            if base != "EUR" or quote not in SIGMA or source != "synthetic_ecb" or not settings.simulation_start <= day <= settings.activity_end:
                raise ValueError(f"Unexpected exchange-rate row: {day} {base}/{quote}")
            stored[day, quote] = rate
            latest = day if latest is None else max(latest, day)
        previous = settings.pricing.reference_rates
        missing = []
        day = settings.simulation_start
        while day <= end:
            expected = daily_rates(settings, day, previous)
            for quote, rate in expected.items():
                actual = stored.get((day, quote))
                if actual is None:
                    if day < start or (latest is not None and day < latest):
                        raise ValueError(f"Gap in stored history: {day} {quote}")
                    missing.append((day, quote, rate))
                elif actual != rate:
                    raise ValueError(f"Conflicting stored rate: {day} {quote}")
            previous = {q: stored.get((day, q), rate) for q, rate in expected.items()}
            day += DAY
        if apply:
            if confirmed is not None and missing != confirmed:
                raise ValueError("Rate plan changed after confirmation")
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO public.exchange_rates "
                                "(rate_date,quote_currency_code,rate) VALUES (%s,%s,%s)", missing)
        return missing
