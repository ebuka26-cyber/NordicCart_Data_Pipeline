"""Calibration of the demand model: exact figures and seeded statistical checks (design 11.1).

    exact_figures(model)                  deterministic figures (no random draws)
    simulated_metrics(model, seed)        one seeded simulation of the generation window
    statistical_checks(model, metrics)    the 11.1 statistical criteria with their tolerances
    category_unit_shares(model, ...)      expected unit share per category (product mix)

Statistical tolerances follow design 11.1. Two refinements, stated in the S2 report:
  * monthly shares are compared with the model's expected monthly share, which includes the
    growth of the customer base during the year (11.1 wrote S*_m d_m / 365, the share of a
    constant customer base);
  * for rare outcomes the tolerance is the larger of the relative tolerance and 4 standard
    errors of the observed count, so a check does not hinge on a few dozen events;
  * location shares (not a design criterion; added) use the standard error of a count made of
    negative binomial customers (location_count_variance), not a binomial one.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext

from .config import CHANNELS
from .model import DemandModel
from .numeric import PREC, nb_pmf, nb_zero
from .simulate import simulate

ZERO, ONE = Decimal(0), Decimal(1)


@dataclass(frozen=True)
class Check:
    name: str
    expected: float
    actual: float
    low: float
    high: float

    @property
    def passed(self) -> bool:
        return self.low <= self.actual <= self.high

    def as_dict(self) -> dict:
        return {**asdict(self), "passed": self.passed}


def _f(x) -> float:
    """Decimal to float for reporting and comparisons of statistics (never for generated values)."""
    return float(x)


def generation_window(model: DemandModel) -> tuple[date, date]:
    return model.ds.generation_start, model.ds.planning_end or model.calendar.end   # the historical window (S10)


def top_share_expected(mean: Decimal, shape: Decimal, top: Decimal = Decimal("0.2")) -> Decimal:
    """Share of all orders placed by the top `top` of customers, for a negative binomial population."""
    with localcontext() as ctx:
        ctx.prec = PREC
        pmf = nb_pmf(mean, shape, 400)
        remaining, orders = top, ZERO
        for n in range(len(pmf) - 1, -1, -1):
            take = min(pmf[n], remaining)
            orders += take * n
            remaining -= take
            if remaining <= 0:
                break
        return orders / mean


def location_count_variance(model: DemandModel, first: date, last: date) -> dict[str, Decimal]:
    """Variance of the order count per location: customers' counts are negative binomial.

    For customer c and location l, N | λ_c ~ Poisson(λ_c x mf x E_c,ch x P(l | c)), so
    Var(N) = μ + μ² / k with μ = λ̄ x mf x E_c,ch x P(l | c). A location's orders come from a
    limited set of heavy-tailed customers, so its count varies far more than a binomial count.
    """
    ds = model.ds
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    with localcontext() as ctx:
        ctx.prec = PREC
        for c in model.customers:
            for ch in CHANNELS:
                e = model.exposure(c, first, last, channel=ch)
                if not e:
                    continue
                base = ds.orders_per_customer_year * c.market_factor * e
                pairs = [(c.warehouse, ONE)] if ch == "online" else zip(c.stores, c.store_probability)
                for loc, p in pairs:
                    mu = base * p
                    out[loc] += mu + mu * mu / ds.rate_shape
    return dict(out)


def category_unit_shares(model: DemandModel, first: date, last: date) -> dict[str, Decimal]:
    """Expected share of placed units per category (orders-weighted product mix over the days)."""
    with localcontext() as ctx:
        ctx.prec = PREC
        per_day = model.expected_orders_by(first, last, lambda loc, d: d)
        units: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for d, orders in per_day.items():
            m = model.mix(d)
            for s, w in zip(m.skus, m.weights):
                p = model.products[s]
                units[p.category_code] += orders * w * p.units_per_line / m.total
        total = sum(units.values())
        return {k: v / total for k, v in sorted(units.items())}


def exact_figures(model: DemandModel) -> dict:
    """Deterministic calibration figures (design 11.1, exact part)."""
    ds, cal = model.ds, model.calendar
    lam, k = ds.orders_per_customer_year, ds.rate_shape
    gen_first, gen_last = generation_window(model)
    with localcontext() as ctx:
        ctx.prec = PREC
        years = []
        for first, last in cal.year_bounds:
            spans = cal.months_between(first, last)
            years.append({"first": first.isoformat(), "last": last.isoformat(),
                          "days": (last - first).days + 1,
                          "sum_normalized_index_x_days": sum(cal.normalized_index[(s.year, s.month)] * s.days
                                                             for s in spans),
                          "sum_month_shares": sum(s.share for s in spans)})
        periods = {"generation window": (gen_first, gen_last), "growth year": ds.growth_year,
                   "primary year": ds.primary_year}
        volume = {}
        for name, (first, last) in periods.items():
            cy = model.customer_years(first, last)
            volume[name] = {"first": first.isoformat(), "last": last.isoformat(), "customer_years": cy,
                            "expected_orders": model.expected_orders(first, last),
                            "lambda_x_customer_years": lam * cy,
                            "expected_online_share": model.expected_orders(first, last, channel="online")
                            / model.expected_orders(first, last)}
        pf, pl = ds.primary_year
        full = [c for c in model.customers if c.registered_local.date() < pf]
        partial = [c for c in model.customers if pf <= c.registered_local.date() <= pl]
        partial_e = [model.exposure(c, pf, pl) for c in partial]
        pmf = nb_pmf(lam, k, 3)
        figures = {
            "simulation_years": years,
            "volume": volume,
            "negative_binomial_full_year": {"P0": pmf[0], "P1": pmf[1], "P2": pmf[2], "P3": pmf[3],
                                            "P4_or_more": ONE - sum(pmf), "variance": lam + lam * lam / k,
                                            "variance_over_mean": ONE + lam / k,
                                            "top_20_percent_share": top_share_expected(lam, k)},
            "partial_exposure_P0": {str(e): nb_zero(lam * Decimal(e), k) for e in ("0.25", "0.5", "0.75", "1")},
            "primary_year_groups": {
                "full_year_customers": len(full),
                "full_year_exposure_min": min((model.exposure(c, pf, pl) for c in full), default=ZERO),
                "full_year_exposure_max": max((model.exposure(c, pf, pl) for c in full), default=ZERO),
                "partial_customers": len(partial),
                "partial_customer_years": sum(partial_e, ZERO),
                "partial_mean_exposure": sum(partial_e, ZERO) / len(partial) if partial else ZERO,
                "partial_expected_zero_share": sum((nb_zero(lam * c.market_factor * e, k)
                                                    for c, e in zip(partial, partial_e)), ZERO) / len(partial)
                if partial else ZERO,
                "all_expected_zero_share": (sum((nb_zero(lam * c.market_factor * model.exposure(c, pf, pl), k)
                                                 for c in full), ZERO)
                                            + sum((nb_zero(lam * c.market_factor * e, k)
                                                   for c, e in zip(partial, partial_e)), ZERO))
                / (len(full) + len(partial)) if full or partial else ZERO,
            },
            "online_signup_propensity": model.online_signup_propensity,
            "mean_lines_per_order": model.mean_lines,
            "units_per_order_generation_window": model.units_per_order(gen_first, gen_last),
            "units_per_order_primary_year": model.units_per_order(pf, pl),
            "outcomes": {ch: model.outcome_probabilities(ch) for ch in CHANNELS},
            "market_share_generation_window": {m: model.expected_orders(gen_first, gen_last, market=m)
                                               / volume["generation window"]["expected_orders"]
                                               for m in model.markets},
            "hour_probability_sums": {ch: sum(cal.hour_probability[ch].values()) for ch in CHANNELS},
        }
    return figures


def simulated_metrics(model: DemandModel, seed: int) -> dict:
    """Simulate the generation window with one demand seed and measure it (seed replaces ds.seed)."""
    from dataclasses import replace
    original = model.ds
    model.ds = replace(original, seed=seed)
    try:
        first, last = generation_window(model)
        orders, rates = simulate(model, first, last)
    finally:
        model.ds = original
    ds = model.ds
    pf, pl = ds.primary_year
    gf, gl = ds.growth_year
    cf, cl = ds.channel_period
    in_primary = Counter(o.customer_number for o in orders if pf <= o.local_day <= pl)
    full = [c.number for c in model.customers if c.registered_local.date() < pf]
    partial = [c.number for c in model.customers if pf <= c.registered_local.date() <= pl]
    counts = [in_primary.get(n, 0) for n in full]
    n_full = len(counts)
    mean = sum(counts) / n_full
    variance = sum((x - mean) ** 2 for x in counts) / (n_full - 1)
    ranked = sorted(counts, reverse=True)
    top = sum(ranked[: n_full // 5]) / sum(counts)

    by_month = Counter((o.local_day.year, o.local_day.month) for o in orders if pf <= o.local_day <= pl)
    by_weekday = Counter((o.channel, o.local_day.isoweekday()) for o in orders)
    by_hour = Counter((o.channel, o.hour) for o in orders)
    bw_first, bw_last = model.calendar.black_weeks[pf.year]
    november = [o for o in orders if o.local_day.year == pf.year and o.local_day.month == 11]
    black_week = sum(1 for o in november if bw_first <= o.local_day <= bw_last)
    by_channel = Counter(o.channel for o in orders if cf <= o.local_day <= cl)
    by_market = Counter(o.market for o in orders)
    by_location = Counter(o.location_code for o in orders)
    units_by_category: Counter = Counter()
    for o in orders:
        for sku, q in o.lines:
            units_by_category[model.products[sku].category_code] += q
    lifecycle = {}
    for ch in CHANNELS:
        group = [o for o in orders if o.channel == ch]
        paying = [o for o in group if o.attempts]
        attempts = sum(o.attempts for o in paying)
        failures = sum(o.failed_attempts for o in paying)
        retry_possible = sum(min(o.failed_attempts, ds.lifecycle[ch].max_attempts - 1) for o in paying)
        retries = sum(o.attempts - 1 for o in paying)
        lifecycle[ch] = {"orders": len(group), "expired": sum(o.outcome == "expired" for o in group),
                         "cancelled": sum(o.outcome == "cancelled" for o in group),
                         "payment_failed": sum(o.outcome == "payment_failed" for o in group),
                         "fulfilled": sum(o.outcome == "fulfilled" for o in group),
                         "attempts": attempts, "failed_attempts": failures, "retry_possible": retry_possible,
                         "retries": retries}
    rate_values = list(rates.values())
    rate_mean = sum(rate_values) / len(rate_values)
    rate_var = sum((r - rate_mean) ** 2 for r in rate_values) / (len(rate_values) - 1)
    return {
        "seed": seed, "orders": len(orders),
        "orders_primary_year": sum(1 for o in orders if pf <= o.local_day <= pl),
        "orders_growth_year": sum(1 for o in orders if gf <= o.local_day <= gl),
        "full_year_customers": n_full, "full_year_mean": mean,
        "full_year_zero_share": counts.count(0) / n_full, "full_year_one_share": counts.count(1) / n_full,
        "full_year_variance_over_mean": variance / mean, "full_year_top_20_share": top,
        "partial_customers": len(partial),
        "partial_zero_share": sum(1 for n in partial if in_primary.get(n, 0) == 0) / len(partial) if partial else 0.0,
        "by_month": {f"{y:04d}-{m:02d}": v for (y, m), v in sorted(by_month.items())},
        "by_weekday": {f"{ch}|{wd}": v for (ch, wd), v in sorted(by_weekday.items())},
        "by_hour": {f"{ch}|{h}": v for (ch, h), v in sorted(by_hour.items())},
        "november_orders": len(november), "black_week_orders": black_week,
        "by_channel": dict(by_channel), "by_market": dict(sorted(by_market.items())),
        "by_location": dict(sorted(by_location.items())),
        "units": sum(o.units for o in orders), "lines": sum(len(o.lines) for o in orders),
        "units_by_category": dict(sorted(units_by_category.items())),
        "lifecycle": lifecycle,
        "rates": {"customers": len(rate_values), "mean": _f(rate_mean), "variance": _f(rate_var)},
    }


def _relative(name: str, expected: float, actual: float, tolerance: float, count: float | None = None,
              share: float | None = None) -> Check:
    """expected x (1 +- tolerance), widened to 4 standard errors when `count` events are observed."""
    width = abs(expected) * tolerance
    if count is not None and share is not None and count > 0:
        width = max(width, 4 * math.sqrt(share * (1 - share) / count))
    return Check(name, expected, actual, expected - width, expected + width)


def statistical_checks(model: DemandModel, figures: dict, m: dict) -> list[Check]:
    """Design 11.1, statistical part, for one simulated seed."""
    ds = model.ds
    lam, k = _f(ds.orders_per_customer_year), _f(ds.rate_shape)
    gen_first, gen_last = generation_window(model)
    vol = figures["volume"]
    out = [
        _relative("orders in the primary year", _f(vol["primary year"]["expected_orders"]), m["orders_primary_year"], 0.03),
        _relative("orders in the growth year", _f(vol["growth year"]["expected_orders"]), m["orders_growth_year"], 0.03),
        Check("mean orders per full-year customer", lam, m["full_year_mean"], lam - 0.08, lam + 0.08),
        Check("zero-order share, full-year customers", _f(figures["negative_binomial_full_year"]["P0"]),
              m["full_year_zero_share"], _f(figures["negative_binomial_full_year"]["P0"]) - 0.015,
              _f(figures["negative_binomial_full_year"]["P0"]) + 0.015),
        Check("zero-order share, partially registered", _f(figures["primary_year_groups"]["partial_expected_zero_share"]),
              m["partial_zero_share"], _f(figures["primary_year_groups"]["partial_expected_zero_share"]) - 0.03,
              _f(figures["primary_year_groups"]["partial_expected_zero_share"]) + 0.03),
        Check("share with exactly 1 order, full-year", _f(figures["negative_binomial_full_year"]["P1"]),
              m["full_year_one_share"], _f(figures["negative_binomial_full_year"]["P1"]) - 0.015,
              _f(figures["negative_binomial_full_year"]["P1"]) + 0.015),
        Check("variance / mean, full-year customers", 1 + lam / k, m["full_year_variance_over_mean"],
              1 + lam / k - 0.5, 1 + lam / k + 0.5),
        Check("top 20% of full-year customers' share of orders", _f(figures["negative_binomial_full_year"]
                                                                    ["top_20_percent_share"]),
              m["full_year_top_20_share"], 0.58, 0.65),
        Check("mean personal rate", lam, m["rates"]["mean"], lam * 0.97, lam * 1.03),
        Check("variance of personal rates", lam * lam / k, m["rates"]["variance"], lam * lam / k * 0.85,
              lam * lam / k * 1.15),
    ]
    # Monthly shares of the primary year (months with >= 2,000 simulated orders)
    pf, pl = ds.primary_year
    expected_month = model.expected_orders_by(pf, pl, lambda loc, d: f"{d.year:04d}-{d.month:02d}")
    total_expected = sum(expected_month.values())
    total_sim = sum(m["by_month"].values())
    for key, expected in sorted(expected_month.items()):
        sim = m["by_month"].get(key, 0)
        if sim >= 2000:
            out.append(_relative(f"monthly share {key}", _f(expected / total_expected), sim / total_sim, 0.10))
    # Weekday share per channel over the generation window
    expected_wd = model.expected_orders_by(gen_first, gen_last,
                                           lambda loc, d: (model.location_channel[loc], d.isoweekday()))
    for ch in CHANNELS:
        ch_expected = sum(v for (c, _), v in expected_wd.items() if c == ch)
        ch_sim = sum(v for key, v in m["by_weekday"].items() if key.startswith(ch + "|"))
        for wd in range(1, 8):
            share = _f(expected_wd.get((ch, wd), ZERO) / ch_expected)
            sim = m["by_weekday"].get(f"{ch}|{wd}", 0) / ch_sim
            if share == 0:
                out.append(Check(f"weekday share {ch} {wd} (closed)", 0.0, sim, 0.0, 0.0))
            else:
                out.append(_relative(f"weekday share {ch} {wd}", share, sim, 0.05))
    # Black Week (design 5.3: weight x 3 inside November) and hours per channel (normalized hour weights)
    bw_first, bw_last = model.calendar.black_weeks[pf.year]
    november = model.expected_orders_by(date(pf.year, 11, 1), date(pf.year, 11, 30), lambda loc, d: d)
    bw_share = sum(v for d, v in november.items() if bw_first <= d <= bw_last) / sum(november.values())
    out.append(_relative(f"Black Week share of November {pf.year} orders", _f(bw_share),
                         m["black_week_orders"] / m["november_orders"], 0.10))
    for ch in CHANNELS:
        ch_total = sum(v for key, v in m["by_hour"].items() if key.startswith(ch + "|"))
        for hour, p in model.calendar.hour_probability[ch].items():
            out.append(_relative(f"hour share {ch} {hour:02d}", _f(p), m["by_hour"].get(f"{ch}|{hour}", 0) / ch_total,
                                 0.10, count=ch_total, share=_f(p)))
    # Channel and geography
    online = m["by_channel"].get("online", 0) / sum(m["by_channel"].values())
    target = _f(ds.online_share)
    out.append(Check("online share of orders (channel period)", target, online, target - 0.015, target + 0.015))
    total_orders = m["orders"]
    for market, share in figures["market_share_generation_window"].items():
        out.append(Check(f"market share {market}", _f(share), m["by_market"].get(market, 0) / total_orders,
                         _f(share) - 0.015, _f(share) + 0.015))
    expected_loc = model.expected_orders_by(gen_first, gen_last, lambda loc, d: loc)
    total_loc = sum(expected_loc.values())
    variance = location_count_variance(model, gen_first, gen_last)
    for loc, expected in sorted(expected_loc.items()):
        share = _f(expected / total_loc)
        width = max(share * 0.10, 4 * math.sqrt(_f(variance[loc])) / _f(total_loc))     # clustered by customer
        out.append(Check(f"location share {loc}", share, m["by_location"].get(loc, 0) / total_orders,
                         share - width, share + width))
    # Lifecycle (configured rates, design C11)
    for ch in CHANNELS:
        lc, sim = ds.lifecycle[ch], m["lifecycle"][ch]
        n = sim["orders"]
        out.append(_relative(f"{ch} abandoned checkouts (expired)", _f(lc.abandoned_checkout), sim["expired"] / n, 0.20,
                             count=n, share=_f(lc.abandoned_checkout)))
        out.append(_relative(f"{ch} customer cancellations", _f(lc.customer_cancel), sim["cancelled"] / n, 0.20,
                             count=n, share=_f(lc.customer_cancel)))
        out.append(_relative(f"{ch} payment attempt failures", _f(lc.first_attempt_failure),
                             sim["failed_attempts"] / sim["attempts"], 0.20, count=sim["attempts"],
                             share=_f(lc.first_attempt_failure)))
        out.append(_relative(f"{ch} retries after a failure", _f(lc.retry_after_failure),
                             sim["retries"] / sim["retry_possible"], 0.20, count=sim["retry_possible"],
                             share=_f(lc.retry_after_failure)))
        outcomes = figures["outcomes"][ch]
        out.append(_relative(f"{ch} fulfilled orders", _f(outcomes["fulfilled"]), sim["fulfilled"] / n, 0.02,
                             count=n, share=_f(outcomes["fulfilled"])))
    # Units and the product mix
    units = _f(figures["units_per_order_generation_window"])
    out.append(_relative("units per order", units, m["units"] / m["orders"], 0.02))
    shares = category_unit_shares(model, gen_first, gen_last)
    for cat, share in shares.items():
        out.append(_relative(f"unit share {cat}", _f(share), m["units_by_category"].get(cat, 0) / m["units"], 0.10,
                             count=m["units"], share=_f(share)))
    return out
