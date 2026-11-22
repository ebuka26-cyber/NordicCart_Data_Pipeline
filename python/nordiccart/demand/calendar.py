"""The demand calendar: normalized seasonality, day and hour probabilities (design 5.3).

Simulation years run from calendar.simulation_start (1 November) for 12 months.
For every channel (online, in_store) and every day d of month m in simulation year y:

    S*_m       = S_m x D_y / sum over the year's months of S_m x d_m       (normalized index)
                 so that sum of S*_m x d_m = D_y exactly (D_y = days in the year, 365)
    w_ch(d)    = weekday weight x Black Week weight (November days of the Monday-to-Sunday
                 week of Black Friday) x 0 on store-closed days (Sundays and the configured
                 holidays, in_store only)
    p_ch(d|m)  = w_ch(d) / sum of w_ch over the days of m                   (sums to 1 per month)
    u_ch(d)    = S*_m x d_m / D_y x p_ch(d|m)                             (daily exposure unit)

A customer active through the whole year has sum of u_ch(d) = 1 for either channel, so the
expected number of orders a year is exactly the personal rate. Within a month, closed days
and Black Week only move orders between days; the month total is unchanged.
Hours: h_ch(h) = hour weight / sum over the channel's open hours (local time; the open
hours 07-23 and 10-19 avoid the daylight-saving gaps at 02:00-03:00).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext

from .config import CHANNELS, DemandSettings
from .numeric import PREC, cumulative, to_ints

ZERO = Decimal(0)


def black_friday(year: int) -> date:
    """The day after the fourth Thursday of November."""
    first = date(year, 11, 1)
    first_thursday = first + timedelta(days=(3 - first.weekday()) % 7)
    return first_thursday + timedelta(days=22)


def black_week(year: int) -> tuple[date, date]:
    """Monday to Sunday of the Black Friday week, limited to November."""
    friday = black_friday(year)
    return friday - timedelta(days=4), min(friday + timedelta(days=2), date(year, 11, 30))


def _month_end(d: date) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


@dataclass(frozen=True)
class MonthSpan:
    year: int
    month: int
    first: int                 # calendar day index of the first day
    last: int                  # calendar day index of the last day
    days: int
    sim_year: int              # index of the simulation year
    share: Decimal             # S*_m x d_m / D_y: share of a full year's exposure


class DemandCalendar:
    def __init__(self, ds: DemandSettings):
        self.start, self.end = ds.calendar_start, ds.calendar_end
        if self.start.day != 1 or _month_end(self.end) != self.end:
            raise ValueError("the demand calendar needs whole months (simulation_start on a 1st, "
                             "simulation_end on a month end)")
        n = (self.end - self.start).days + 1
        self.days: list[date] = [self.start + timedelta(days=i) for i in range(n)]
        self.index: dict[date, int] = {d: i for i, d in enumerate(self.days)}
        planning_end = min(ds.planning_end or self.end, self.end)
        self.split = self.index[planning_end] + 1      # first day after the original calendar (n without an extension)
        self.black_weeks = {y: black_week(y) for y in range(self.start.year, self.end.year + 1)}

        with localcontext() as ctx:
            ctx.prec = PREC
            # Simulation years and the normalized monthly index ------------------------------
            self.months: list[MonthSpan] = []
            self.normalized_index: dict[tuple[int, int], Decimal] = {}
            self.year_bounds: list[tuple[date, date]] = []
            year_start, y = self.start, 0
            while year_start <= self.end:
                year_end = min(date(year_start.year + 1, year_start.month, 1) - timedelta(days=1), self.end)
                self.year_bounds.append((year_start, year_end))
                spans = []
                d = year_start
                while d <= year_end:
                    m_end = _month_end(d)
                    spans.append((d, m_end))
                    d = m_end + timedelta(days=1)
                year_days = Decimal((year_end - year_start).days + 1)
                raw_total = sum(ds.monthly_index[a.month] * ((b - a).days + 1) for a, b in spans)
                for a, b in spans:
                    days = (b - a).days + 1
                    s_star = ds.monthly_index[a.month] * year_days / raw_total
                    self.normalized_index[(a.year, a.month)] = s_star
                    self.months.append(MonthSpan(a.year, a.month, self.index[a], self.index[b], days, y,
                                                 s_star * days / year_days))
                year_start, y = year_end + timedelta(days=1), y + 1
            self.month_of: list[MonthSpan] = [None] * n            # type: ignore[list-item]
            for span in self.months:
                for i in range(span.first, span.last + 1):
                    self.month_of[i] = span

            # Day weights, probabilities and daily exposure units per channel ---------------------
            closed = set(ds.store_closed_days)
            self.weight: dict[str, list[Decimal]] = {}
            self.probability: dict[str, list[Decimal]] = {}
            self.unit: dict[str, list[Decimal]] = {}
            self.suffix: dict[str, list[Decimal]] = {}
            self.month_cum: dict[str, dict[tuple[int, int], list[int]]] = {}
            for ch in CHANNELS:
                weights = []
                for d in self.days:
                    w = ds.weekday_weights[ch][d.isoweekday()]
                    bw_first, bw_last = self.black_weeks[d.year]
                    if bw_first <= d <= bw_last:
                        w *= ds.black_week_weight
                    if ch == "in_store" and (d.month, d.day) in closed:
                        w = ZERO
                    weights.append(w)
                probs, units = [ZERO] * n, [ZERO] * n
                cums = {}
                for span in self.months:
                    month_weights = weights[span.first:span.last + 1]
                    total = sum(month_weights)
                    for i, w in enumerate(month_weights, start=span.first):
                        probs[i] = w / total
                        units[i] = span.share * probs[i]
                    cums[(span.year, span.month)] = cumulative(to_ints(month_weights))
                # S10: two segments, the original calendar [0, split) and the extension [split, n): a sum inside
                # the original calendar uses exactly the terms it always used (Decimal rounding unchanged)
                suffix = [ZERO] * (n + 1)
                for i in range(n - 1, -1, -1):
                    suffix[i] = (ZERO if i + 1 == self.split else suffix[i + 1]) + units[i]
                self.weight[ch], self.probability[ch], self.unit[ch], self.suffix[ch] = weights, probs, units, suffix
                self.month_cum[ch] = cums

            # Hours ---------------------------------------------------------------------------
            self.hours: dict[str, list[int]] = {}
            self.hour_probability: dict[str, dict[int, Decimal]] = {}
            self.hour_cum: dict[str, list[int]] = {}
            for ch in CHANNELS:
                hw = ds.hour_weights[ch]
                total = sum(hw.values())
                hours = sorted(hw)
                self.hours[ch] = hours
                self.hour_probability[ch] = {h: hw[h] / total for h in hours}
                self.hour_cum[ch] = cumulative(to_ints([hw[h] for h in hours]))

    # ------------------------------------------------------------------ lookups
    def last_hour(self, channel: str) -> int:
        return self.hours[channel][-1]

    def span(self, year: int, month: int) -> MonthSpan:
        return self.month_of[self.index[date(year, month, 1)]]

    def units(self, channel: str, first: date, last: date) -> Decimal:
        """Sum of u_ch(d) over first..last (clamped to the calendar)."""
        first, last = max(first, self.start), min(last, self.end)
        if first > last:
            return ZERO
        a, b, s, split = self.index[first], self.index[last], self.suffix[channel], self.split
        if b < split or a >= split:                    # inside one segment
            return s[a] - (ZERO if b + 1 == split else s[b + 1])
        return s[a] + (s[split] - s[b + 1])            # original part [a, split) + extension part [split, b]

    def months_between(self, first: date, last: date) -> list[MonthSpan]:
        first, last = max(first, self.start), min(last, self.end)
        return [s for s in self.months if s.first <= self.index[last] and s.last >= self.index[first]] if first <= last else []
