"""Deterministic random draws and distribution formulas, in Decimal (no floats).

Project rule: generated values never depend on float arithmetic, whose last digit can
differ between C libraries. Every draw below uses only random.Random.getrandbits and
randrange (stable since Python 3.2) and Decimal arithmetic (ln, exp, sqrt), whose results
are correctly rounded at the stated precision (28 significant digits; 20 inside the Poisson
draw, where they are compared with a 53-bit uniform).

    uniform     (0, 1) on a 2**53 grid, never 0
    normal      standard normal, Marsaglia polar method (ln and sqrt only, no trigonometry)
    gamma       Marsaglia-Tsang (2000) for shape >= 1; shape < 1 by the boost
                Gamma(a) = Gamma(a + 1) x U ** (1 / a)
    poisson     inversion: the smallest n with U <= P(N <= n)
    nb_pmf      negative binomial (gamma-Poisson) probabilities, mean m and shape k:
                P(0) = (k / (k + m)) ** k, P(n) = P(n - 1) x (k + n - 1) / n x m / (k + m)
    to_ints / pick_index   weighted choice with integer weights and bisect (O(log n))
"""

from __future__ import annotations

import random
from bisect import bisect_right
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from itertools import accumulate

PREC = 28
POISSON_PREC = 20
ONE = Decimal(1)
TWO = Decimal(2)
GRID = 2 ** 53
WEIGHT_SCALE = Decimal(10) ** 15
_THIRD = ONE / 3
_TSANG = Decimal("0.0331")


def _bits(rng: random.Random) -> int:
    """A uniform integer in 1 .. 2**53 - 1."""
    while True:
        k = rng.getrandbits(53)
        if k:
            return k


def uniform(rng: random.Random) -> Decimal:
    """A uniform number in the open interval (0, 1)."""
    with localcontext() as ctx:
        ctx.prec = PREC
        return Decimal(_bits(rng)) / GRID


def normal(rng: random.Random) -> Decimal:
    """A standard normal number (Marsaglia polar method; the second deviate is not used)."""
    with localcontext() as ctx:
        ctx.prec = PREC
        while True:
            u = TWO * uniform(rng) - ONE
            v = TWO * uniform(rng) - ONE
            s = u * u + v * v
            if 0 < s < 1:
                return u * (-TWO * s.ln() / s).sqrt()


def gamma(rng: random.Random, shape: Decimal, scale: Decimal = ONE) -> Decimal:
    """A gamma number with the given shape and scale (mean shape x scale)."""
    if shape <= 0 or scale <= 0:
        raise ValueError("gamma needs a positive shape and scale")
    with localcontext() as ctx:
        ctx.prec = PREC
        if shape < 1:
            boosted = gamma(rng, shape + ONE)
            return boosted * (uniform(rng).ln() / shape).exp() * scale
        d = shape - _THIRD
        c = ONE / (9 * d).sqrt()
        while True:
            x = normal(rng)
            v = ONE + c * x
            if v <= 0:
                continue
            v = v * v * v
            u = uniform(rng)
            x2 = x * x
            if u < ONE - _TSANG * x2 * x2 or u.ln() < x2 / 2 + d * (ONE - v + v.ln()):
                return d * v * scale


def poisson(rng: random.Random, mean: Decimal) -> int:
    """A Poisson count with the given mean (inversion; one uniform per draw)."""
    if mean <= 0:
        return 0
    with localcontext() as ctx:
        ctx.prec = POISSON_PREC
        u = Decimal(_bits(rng)) / GRID
        p = (-mean).exp()
        cumulative = p
        n = 0
        limit = int(mean * 20) + 100
        while u > cumulative and n < limit:
            n += 1
            p = p * mean / n
            cumulative += p
        return n


def nb_pmf(mean: Decimal, shape: Decimal, up_to: int) -> list[Decimal]:
    """P(N = 0..up_to) of the negative binomial with this mean and gamma shape."""
    with localcontext() as ctx:
        ctx.prec = PREC
        if mean <= 0:
            return [ONE] + [Decimal(0)] * up_to
        q = mean / (shape + mean)
        out = [(shape / (shape + mean)) ** shape]
        for n in range(1, up_to + 1):
            out.append(out[-1] * (shape + n - 1) / n * q)
        return out


def nb_zero(mean: Decimal, shape: Decimal) -> Decimal:
    """P(N = 0) = (1 + mean / shape) ** -shape."""
    with localcontext() as ctx:
        ctx.prec = PREC
        return (ONE + mean / shape) ** (-shape)


def to_ints(weights: list[Decimal]) -> list[int]:
    """Integer weights proportional to `weights` (relative resolution 1e-15); positive stays positive."""
    total = sum(weights)
    if total <= 0:
        raise ValueError("weights must not all be zero")
    out = []
    for w in weights:
        if w < 0:
            raise ValueError("weights must not be negative")
        n = int((w / total * WEIGHT_SCALE).to_integral_value(ROUND_HALF_EVEN)) if w > 0 else 0
        out.append(max(n, 1) if w > 0 else 0)
    return out


def cumulative(ints: list[int]) -> list[int]:
    return list(accumulate(ints))


def pick_index(rng: random.Random, cum: list[int], low: int = 0) -> int:
    """Index i >= low drawn with probability proportional to its integer weight.

    `cum` holds cumulative integer weights; `low` restricts the draw to the items from
    `low` on (for example the days on or after a customer's registration).
    """
    base = cum[low - 1] if low > 0 else 0
    total = cum[-1] - base
    if total <= 0:
        raise ValueError("nothing to choose from")
    r = base + rng.randrange(total)
    return bisect_right(cum, r, lo=low)


def chance(rng: random.Random, share: Decimal) -> bool:
    """True with probability `share` (resolution 1e-9), without floats."""
    return rng.randrange(1_000_000_000) < int(share * 1_000_000_000)
