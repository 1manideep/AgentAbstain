"""Statistics for pre-registered decisions (MEMORY_EVOLUTION 6.2, 6.11; H1, H3, H4, H9; PLAN section 8 Q2).

Standard library only: numpy and scipy are not runtime dependencies of the package, so the Student t
distribution is computed here from the regularized incomplete beta function (the Numerical Recipes
``betacf`` continued fraction) and its quantile by bisection.

What is here, and which promise each piece keeps:

* :func:`paired_t` -- the paired t-test on per-seed differences.
* :func:`bootstrap_ci` -- the paired percentile bootstrap. With the same inputs it reproduces
  :func:`void.research.compare.paired_stats`'s ``ci95`` exactly (same RNG, draw order and percentile rule).
* :func:`superiority` / :func:`non_superiority` -- the pre-registered "the interval clears the margin"
  rules; a PASS also needs the margin-shifted t p-value (the one :func:`holm` adjusts) below alpha, so the
  verdict and the reported p never disagree.
* :func:`contrast_test` -- one interaction contrast (a per-seed difference of differences) against a margin in
  ``greater``, ``less`` or ``two_sided`` direction: superiority's interval and t rules, on the observed side for
  ``two_sided``.
* :func:`tost_paired` -- two one-sided tests: a claim of *no effect* needs equivalence within a stated
  margin, never a non-significant p-value (6.11; H1 uses a margin of 0.25 SD).
* :func:`holm` -- Holm step-down adjustment across the pre-registered primary endpoints.
* :func:`power_paired` / :func:`n_for_power` -- approximate power of the paired design (6.2, E0 gate);
  :func:`power_tost` -- approximate power of the equivalence test at a zero effect.
* :func:`sign_test` -- exact two-sided sign test.
* :func:`interaction_2x2` -- per-seed main effects and interaction of a 2x2 factorial, descriptive (Q2:
  scarcity x capability tier); a declared interaction (H4: rung x self-edit) is decided with :func:`contrast_test`.
* :func:`slope_by_seed` -- the slope of a generation analysis with a random intercept per seed (6.11),
  estimated within seeds with a cluster bootstrap by seed.

Every function is deterministic: resampling uses ``random.Random(seed)``. Intervals are returned as
two-element lists (``[lo, hi]``, ``[None, None]`` when undefined) so results serialize to JSON as they are.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Hashable, Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "betai",
    "bootstrap_ci",
    "contrast_test",
    "holm",
    "interaction_2x2",
    "n_for_power",
    "non_superiority",
    "paired_t",
    "power_paired",
    "power_tost",
    "sign_test",
    "slope_by_seed",
    "superiority",
    "t_cdf",
    "t_ppf",
    "t_sf",
    "tost_paired",
]

N_BOOT = 2000
BOOT_SEED = 0
DIRECTIONS = ("greater", "less")
CONTRAST_DIRECTIONS = ("greater", "less", "two_sided")
_NORMAL = statistics.NormalDist()
_BETACF_MAXIT = 500
_BETACF_EPS = 1e-15
_FPMIN = 1e-300


# --- the t distribution ---------------------------------------------------------------------------------
def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz; Numerical Recipes ``betacf``)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _BETACF_MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _BETACF_EPS:
            return h
    raise ArithmeticError(f"betacf did not converge for a={a}, b={b}, x={x}")


def betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function ``I_x(a, b)`` for ``a, b > 0`` and ``0 <= x <= 1``."""
    if a <= 0 or b <= 0:
        raise ValueError("betai needs a > 0 and b > 0")
    if not 0.0 <= x <= 1.0:
        raise ValueError("betai needs 0 <= x <= 1")
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0
    log_bt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    bt = math.exp(log_bt)
    # The continued fraction converges fast for x < (a + 1) / (a + b + 2); otherwise use the symmetry
    # I_x(a, b) = 1 - I_{1-x}(b, a).
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _check_df(df: float) -> float:
    df = float(df)
    if math.isnan(df) or df <= 0:
        raise ValueError("degrees of freedom must be positive")
    return df


def _t_tail(t: float, df: float) -> float:
    """``P(T > |t|)``: half the two-sided tail, computed directly so small tails keep their precision."""
    if math.isinf(df):
        return _NORMAL.cdf(-abs(t))
    if math.isinf(t):
        return 0.0
    return 0.5 * betai(df / 2.0, 0.5, df / (df + t * t))


def t_sf(t: float, df: float) -> float:
    """Upper tail ``P(T > t)`` of Student's t with ``df`` degrees of freedom (``df`` may be ``math.inf``)."""
    df = _check_df(df)
    if math.isnan(t):
        raise ValueError("t is NaN")
    tail = _t_tail(t, df)
    return tail if t > 0 else 1.0 - tail


def t_cdf(t: float, df: float) -> float:
    """``P(T <= t)`` of Student's t with ``df`` degrees of freedom, via the regularized incomplete beta:
    ``P(T > |t|) = I_x(df/2, 1/2) / 2`` with ``x = df / (df + t^2)``."""
    df = _check_df(df)
    if math.isnan(t):
        raise ValueError("t is NaN")
    tail = _t_tail(t, df)
    return 1.0 - tail if t > 0 else tail


def t_ppf(q: float, df: float) -> float:
    """Quantile of Student's t: the ``t`` with ``t_cdf(t, df) == q``, found by bisection."""
    df = _check_df(df)
    if not 0.0 < q < 1.0:
        raise ValueError("t_ppf needs 0 < q < 1")
    if q == 0.5:
        return 0.0
    # Bisect for the positive t whose upper tail is p; the lower tail uses q itself (not 1 - q) so small
    # q keep their precision.
    return -_upper_quantile(q, df) if q < 0.5 else _upper_quantile(1.0 - q, df)


def _upper_quantile(p: float, df: float) -> float:
    """The ``t > 0`` with ``t_sf(t, df) == p`` for ``0 < p < 0.5``, by bisection."""
    lo, hi = 0.0, 1.0
    while t_sf(hi, df) > p:
        lo, hi = hi, hi * 2.0
        if math.isinf(hi):  # pragma: no cover - only for q within rounding of 1
            raise ArithmeticError("t_ppf: quantile overflow")
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if mid in (lo, hi):
            break
        if t_sf(mid, df) > p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --- helpers -------------------------------------------------------------------------------------------
def _clean(values: Iterable[float]) -> list[float]:
    out = [float(v) for v in values]
    if any(not math.isfinite(v) for v in out):
        raise ValueError("values must be finite numbers")
    return out


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear interpolation between order statistics; the same rule as ``compare._percentile``."""
    if not sorted_vals:
        return math.nan
    pos = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _tails(level: float) -> tuple[float, float]:
    """Lower and upper quantiles of a two-sided ``level`` interval. Rounded so that 0.95 gives exactly
    0.025 and 0.975, the literals ``compare.paired_stats`` uses (``(1 - 0.95) / 2`` is not 0.025 in floats)."""
    if not 0.0 < level < 1.0:
        raise ValueError("level must lie strictly between 0 and 1")
    lo_q = round((1.0 - level) / 2.0, 12)
    return lo_q, round(1.0 - lo_q, 12)


def _check_direction(direction: str) -> None:
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, not {direction!r}")


def _check_margin(margin: float) -> float:
    margin = float(margin)
    if not math.isfinite(margin) or margin < 0:
        raise ValueError("margin must be a finite number >= 0")
    return margin


def _check_alpha(alpha: float) -> float:
    alpha = float(alpha)
    if not 0.0 < alpha < 0.5:
        raise ValueError("alpha must lie strictly between 0 and 0.5")
    return alpha


def _sorted_keys(keys: Iterable[Hashable]) -> list[Hashable]:
    keys = list(keys)
    try:
        return sorted(keys)  # type: ignore[type-var]
    except TypeError:
        return sorted(keys, key=repr)


# --- paired inference ------------------------------------------------------------------------------------
def paired_t(diffs: Sequence[float]) -> dict[str, Any]:
    """Paired (one-sample) t-test of ``mean(diffs) == 0``.

    Returns ``{n, mean, sd, se, t, df, p_two_sided}``; fields that need two or more differences are None
    below that. When every difference is identical (``sd == 0``) the statistic is ``+-inf`` with p = 0 for
    a non-zero mean, and undefined (None) for a zero mean.
    """
    d = _clean(diffs)
    n = len(d)
    out: dict[str, Any] = {"n": n, "mean": None, "sd": None, "se": None, "t": None, "df": None, "p_two_sided": None}
    if n == 0:
        return out
    mean = sum(d) / n
    out["mean"] = mean
    if n < 2:
        return out
    sd = statistics.stdev(d)
    se = sd / math.sqrt(n)
    out.update(sd=sd, se=se, df=n - 1)
    if se == 0:
        if mean != 0:
            out.update(t=math.copysign(math.inf, mean), p_two_sided=0.0)
        return out
    t = mean / se
    out.update(t=t, p_two_sided=min(1.0, 2.0 * _t_tail(t, n - 1)))
    return out


def bootstrap_ci(diffs: Sequence[float], n_boot: int = N_BOOT, seed: int = BOOT_SEED,
                 level: float = 0.95) -> list[float | None]:
    """Percentile bootstrap interval for ``mean(diffs)``: ``n_boot`` resamples of the differences with
    replacement drawn from ``random.Random(seed)``. Draws, order and percentile rule match
    ``compare.paired_stats``, so for ``level=0.95`` the result equals its ``ci95`` on the same data.
    ``[None, None]`` with fewer than two differences."""
    d = _clean(diffs)
    lo_q, hi_q = _tails(level)
    n = len(d)
    if n < 2:
        return [None, None]
    if n_boot < 1:
        raise ValueError("n_boot must be at least 1")
    rng = random.Random(seed)
    means = sorted(sum(d[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return [_percentile(means, lo_q), _percentile(means, hi_q)]


def _shifted_p(d: list[float], shift: float, direction: str) -> float | None:
    """Twice the one-sided p-value of the paired t-test of ``mean(d) - shift`` in ``direction`` (capped at 1)."""
    st = paired_t(d)
    if st["se"] is None:
        return None
    mean, se, df = st["mean"], st["se"], st["df"]
    diff = mean - shift
    if se == 0:
        beyond = diff > 0 if direction == "greater" else diff < 0
        return 0.0 if beyond else 1.0
    t = diff / se
    one_sided = t_sf(t, df) if direction == "greater" else t_cdf(t, df)
    return min(1.0, 2.0 * one_sided)


def superiority(diffs: Sequence[float], margin: float, direction: str, alpha: float = 0.05, *,
                n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> dict[str, Any]:
    """Is the effect beyond ``margin`` in ``direction``?

    Two rules, both required:

    * the pre-registered interval rule on the two-sided ``1 - alpha`` paired bootstrap interval:
      ``greater`` clears when ``lo > margin``, ``less`` when ``hi < -margin`` (margin 0 is plain
      superiority) -- ``ci_clears``;
    * the margin-shifted paired t-test: ``p`` is twice its one-sided p-value (capped at 1), the p that
      :func:`holm` adjusts, and ``p_clears`` is ``p < alpha`` (the ``1 - alpha`` t interval clearing the margin).

    The percentile bootstrap and the t interval disagree near the boundary (with few pairs the bootstrap
    interval is the narrower one), so ``passed`` is ``ci_clears and p_clears``: a PASS always carries
    ``p < alpha``, and a reported p at or above alpha is always a FAIL. ``passed`` is None (undetermined)
    with fewer than two differences.

    Returns ``{passed, ci, p, n, mean, ci_clears, p_clears}``.
    """
    _check_direction(direction)
    margin = _check_margin(margin)
    alpha = _check_alpha(alpha)
    d = _clean(diffs)
    ci = bootstrap_ci(d, n_boot=n_boot, seed=seed, level=1.0 - alpha)
    lo, hi = ci
    if lo is None or hi is None:
        ci_clears: bool | None = None
    elif direction == "greater":
        ci_clears = lo > margin
    else:
        ci_clears = hi < -margin
    shift = margin if direction == "greater" else -margin
    p = _shifted_p(d, shift, direction)
    p_clears = None if p is None else p < alpha
    passed = None if ci_clears is None or p_clears is None else (ci_clears and p_clears)
    return {"passed": passed, "ci": ci, "p": p, "n": len(d), "mean": (sum(d) / len(d)) if d else None,
            "ci_clears": ci_clears, "p_clears": p_clears}


def non_superiority(diffs: Sequence[float], margin: float, direction: str, alpha: float = 0.05, *,
                    n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> dict[str, Any]:
    """The complement of :func:`superiority`: passes when the effect is *not* significantly beyond
    ``margin`` in ``direction`` -- superiority's interval rule (``greater``: ``lo > margin``; ``less``:
    ``hi < -margin`` on the two-sided ``1 - alpha`` paired bootstrap interval) or its shifted t-test
    (``p < alpha``) does not hold. ``p`` is superiority's p. ``passed`` is None with fewer than two differences.

    This is a "does not clear the bar" check, not a claim of no effect; that needs :func:`tost_paired`.
    Returns ``{passed, ci, p, n, mean}``.
    """
    sup = superiority(diffs, margin, direction, alpha, n_boot=n_boot, seed=seed)
    passed = None if sup["passed"] is None else not sup["passed"]
    return {"passed": passed, "ci": sup["ci"], "p": sup["p"], "n": sup["n"], "mean": sup["mean"]}


def contrast_test(values: Sequence[float], margin: float, direction: str, alpha: float = 0.05, *,
                  n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> dict[str, Any]:
    """Does one interaction contrast clear ``margin`` in ``direction``? ``values`` are its per-seed estimates (a
    difference of paired differences: one value per seed, used like a paired difference).

    ``greater`` and ``less`` are :func:`superiority` exactly. ``two_sided`` asks whether the contrast lies beyond
    ``margin`` on either side: it is :func:`superiority` on the side of the observed mean (``greater`` when the mean
    is at or above 0). That side has the smaller margin-shifted one-sided t p-value, so its ``p`` (twice that, capped
    at 1) is the two-sided p; at margin 0 it is the ordinary two-sided paired t p-value. The interval is the two-sided
    ``1 - alpha`` paired percentile bootstrap in every direction. ``passed`` is ``ci_clears and p < alpha`` before any
    multiplicity adjustment (None below two values).

    Returns ``{passed, ci, p, n, mean, side, ci_clears, p_clears}``; ``side`` is the side tested (None without values).
    """
    if direction not in CONTRAST_DIRECTIONS:
        raise ValueError(f"direction must be one of {CONTRAST_DIRECTIONS}, not {direction!r}")
    d = _clean(values)
    side: str | None = direction
    if direction == "two_sided":
        side = ("greater" if sum(d) / len(d) >= 0 else "less") if d else None
    sup = superiority(d, margin, side or "greater", alpha, n_boot=n_boot, seed=seed)
    return {**sup, "side": side}


def tost_paired(diffs: Sequence[float], margin: float, alpha: float = 0.05) -> dict[str, Any]:
    """Two one-sided paired t-tests of equivalence within ``(-margin, margin)``.

    ``p_lower`` tests ``H0: mean <= -margin``, ``p_upper`` tests ``H0: mean >= margin``; ``p`` is the larger
    and ``equivalent`` is ``p < alpha``. ``ci90`` is the t interval at ``1 - 2 * alpha`` (90% for the default
    alpha); ``equivalent`` holds exactly when it lies inside ``(-margin, margin)``.

    ``margin`` is in the outcome's units. A standardized margin such as H1's 0.25 SD is converted with an
    SD fixed in advance (a pilot or the pre-registration), not with the SD of the data being tested.
    All fields are None with fewer than two differences.

    Returns ``{p_lower, p_upper, p, equivalent, ci90, n, mean}``.
    """
    margin = _check_margin(margin)
    alpha = _check_alpha(alpha)
    st = paired_t(diffs)
    out: dict[str, Any] = {"p_lower": None, "p_upper": None, "p": None, "equivalent": None,
                           "ci90": [None, None], "n": st["n"], "mean": st["mean"]}
    if st["se"] is None:
        return out
    mean, se, df = st["mean"], st["se"], st["df"]
    if se == 0:
        p_lower = 0.0 if mean > -margin else 1.0
        p_upper = 0.0 if mean < margin else 1.0
        ci90 = [mean, mean]
    else:
        p_lower = t_sf((mean + margin) / se, df)
        p_upper = t_cdf((mean - margin) / se, df)
        half = t_ppf(1.0 - alpha, df) * se
        ci90 = [mean - half, mean + half]
    p = max(p_lower, p_upper)
    out.update(p_lower=p_lower, p_upper=p_upper, p=p, equivalent=p < alpha, ci90=ci90)
    return out


def holm(pvals: Mapping[str, float | None]) -> dict[str, float | None]:
    """Holm step-down adjusted p-values: sorted ascending, the i-th smallest of m becomes
    ``(m - i + 1) * p``, made monotone by a running maximum and capped at 1. None (an endpoint that could
    not be tested) is passed through and does not count towards m. Keys keep their input order."""
    tested: list[tuple[float, str]] = []
    for key, p in pvals.items():
        if p is None:
            continue
        p = float(p)
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"p-value for {key!r} is not in [0, 1]: {p}")
        tested.append((p, key))
    tested.sort(key=lambda kv: kv[0])
    m = len(tested)
    adjusted: dict[str, float] = {}
    running = 0.0
    for i, (p, key) in enumerate(tested):
        running = max(running, min(1.0, (m - i) * p))
        adjusted[key] = running
    return {key: adjusted.get(key) for key in pvals}


# --- design ----------------------------------------------------------------------------------------------
def power_paired(d_z: float, n: int, alpha: float = 0.05, method: str = "normal") -> float:
    """Approximate power of the two-sided paired test at standardized effect ``d_z`` with ``n`` pairs.

    ``method="normal"`` (default) is the normal approximation ``Phi(|d_z| sqrt(n) - z_{1-alpha/2})``. It
    ignores the opposite tail and the t distribution's heavier tails, so it overstates power at small n
    (0.84 where the t-test has about 0.80 at d_z = 0.66, n = 20). ``method="t"`` uses the shifted central t,
    ``P(T_{n-1} > t_{1-alpha/2, n-1} - |d_z| sqrt(n))``, a closer approximation to the noncentral t power.
    Both are approximations; neither is the exact noncentral t.
    """
    if method not in ("normal", "t"):
        raise ValueError("method must be 'normal' or 't'")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must lie strictly between 0 and 1")
    if n < 1 or (method == "t" and n < 2):
        raise ValueError("n is too small for this method")
    delta = abs(float(d_z)) * math.sqrt(n)
    if method == "normal":
        return _NORMAL.cdf(delta - _NORMAL.inv_cdf(1.0 - alpha / 2.0))
    df = n - 1
    return t_sf(t_ppf(1.0 - alpha / 2.0, df) - delta, df)


def power_tost(margin_z: float, n: int, alpha: float = 0.05) -> float:
    """Approximate power of :func:`tost_paired` to declare equivalence when the true effect is zero, for a
    margin of ``margin_z`` standard deviations of the differences and ``n`` pairs: the normal approximation
    ``max(0, 2 Phi(margin_z sqrt(n) - z_{1-alpha}) - 1)``. Like :func:`power_paired`'s default it ignores the
    t distribution's heavier tails, so it overstates power at small n."""
    if not 0.0 < alpha < 0.5:
        raise ValueError("alpha must lie strictly between 0 and 0.5")
    if n < 1:
        raise ValueError("n is too small")
    margin_z = abs(float(margin_z))
    return max(0.0, 2.0 * _NORMAL.cdf(margin_z * math.sqrt(n) - _NORMAL.inv_cdf(1.0 - alpha)) - 1.0)


def n_for_power(d_z: float, power: float = 0.8, alpha: float = 0.05, method: str = "normal") -> int:
    """Smallest number of pairs (at least 2) whose :func:`power_paired` reaches ``power``."""
    if d_z == 0 or not math.isfinite(d_z):
        raise ValueError("d_z must be finite and non-zero")
    if not 0.0 < power < 1.0:
        raise ValueError("power must lie strictly between 0 and 1")
    z = _NORMAL.inv_cdf(1.0 - alpha / 2.0) + _NORMAL.inv_cdf(power)
    n = max(2, math.ceil((z / abs(d_z)) ** 2))
    while n > 2 and power_paired(d_z, n - 1, alpha, method) >= power:
        n -= 1
    while power_paired(d_z, n, alpha, method) < power:
        n += 1
    return n


def sign_test(pos: int, neg: int) -> float | None:
    """Exact two-sided sign test (binomial with p = 1/2, ties excluded); None without informative pairs."""
    if pos < 0 or neg < 0:
        raise ValueError("counts must be non-negative")
    m = pos + neg
    if m == 0:
        return None
    k = min(pos, neg)
    tail = sum(math.comb(m, i) for i in range(k + 1)) / 2**m
    return min(1.0, 2.0 * tail)


# --- factorial and generation analyses ---------------------------------------------------------------------
def _effect_summary(values: list[float], n_boot: int, seed: int, level: float) -> dict[str, Any]:
    pos = sum(1 for v in values if v > 0)
    neg = sum(1 for v in values if v < 0)
    return {"mean": (sum(values) / len(values)) if values else None,
            "ci": bootstrap_ci(values, n_boot=n_boot, seed=seed, level=level),
            "sign_test_p": sign_test(pos, neg), "positive": pos, "negative": neg, "ties": len(values) - pos - neg}


_CELLS = ((0, 0), (0, 1), (1, 0), (1, 1))


def interaction_2x2(cells: Mapping[tuple[int, int], Mapping[Any, float | None]], *, n_boot: int = N_BOOT,
                    seed: int = BOOT_SEED, level: float = 0.95) -> dict[str, Any]:
    """Main effects and interaction of a 2x2 factorial with seeds shared across the four cells.

    ``cells[(a, b)]`` maps seed -> outcome for factor levels ``a, b`` in {0, 1}. For each seed present
    (with a non-None value) in all four cells:

    * ``main_a = ((y10 + y11) - (y00 + y01)) / 2``
    * ``main_b = ((y01 + y11) - (y00 + y10)) / 2``
    * ``interaction = (y11 - y10) - (y01 - y00)`` (the effect of b at a = 1 minus its effect at a = 0)

    Each gets its mean over seeds, a paired percentile bootstrap interval (:func:`bootstrap_ci`) and an
    exact sign-test p. Seeds missing from any cell are dropped and counted.

    Returns ``{n, seeds, dropped, main_a, main_b, interaction, per_seed}``.
    """
    missing = [c for c in _CELLS if c not in cells]
    if missing:
        raise ValueError(f"interaction_2x2 needs all four cells (0,0), (0,1), (1,0), (1,1); missing {missing}")
    extra = [c for c in cells if c not in _CELLS]
    if extra:
        raise ValueError(f"interaction_2x2 cells must be (a, b) with a, b in {{0, 1}}; got {extra}")
    present = [{s for s, v in cells[c].items() if v is not None} for c in _CELLS]
    common = set.intersection(*present)
    seen = set.union(*present)
    seeds = _sorted_keys(common)
    effects: dict[str, list[float]] = {"main_a": [], "main_b": [], "interaction": []}
    per_seed: dict[str, dict[str, float]] = {}
    for s in seeds:
        y00, y01, y10, y11 = (float(cells[c][s]) for c in _CELLS)  # type: ignore[arg-type]
        row = {"main_a": ((y10 + y11) - (y00 + y01)) / 2.0,
               "main_b": ((y01 + y11) - (y00 + y10)) / 2.0,
               "interaction": (y11 - y10) - (y01 - y00)}
        for k, v in row.items():
            effects[k].append(v)
        per_seed[str(s)] = row
    out: dict[str, Any] = {"n": len(seeds), "seeds": seeds, "dropped": len(seen) - len(common)}
    for k, vals in effects.items():
        out[k] = _effect_summary(_clean(vals), n_boot, seed, level)
    out["per_seed"] = per_seed
    return out


SLOPE_METHOD = "within-seed OLS (seed-demeaned), cluster bootstrap by seed"


def _pooled_slope(sxy: float, sxx: float) -> float | None:
    return sxy / sxx if sxx > 0 else None


def slope_by_seed(rows: Iterable[tuple[Any, float | None, float | None]], *, n_boot: int = N_BOOT,
                  seed: int = BOOT_SEED, level: float = 0.95) -> dict[str, Any]:
    """Slope of ``y`` on ``x`` with a separate intercept per seed, for generation analyses (6.11).

    ``rows`` are ``(seed, x, y)``; rows with a None ``x`` or ``y`` are skipped. Within each seed ``x`` and
    ``y`` are demeaned and the slope is the pooled OLS slope of the demeaned values,
    ``sum_s Sxy_s / sum_s Sxx_s`` (the fixed-effects, or within, estimator). Seed-level shifts in the
    outcome therefore cannot leak into the slope.

    For a balanced design -- every seed observed at the same ``x`` values, as when every seed runs the same
    generations -- the seed means of ``x`` are all equal, so there is no between-seed information about the
    slope and the random-intercept (mixed model) estimate equals this one. For unbalanced designs it is the
    within estimator, which stays unbiased when the seed intercepts correlate with ``x``.

    Uncertainty: a cluster bootstrap that resamples whole seeds with replacement (``random.Random(seed)``,
    ``n_boot`` draws); ``ci`` is the percentile interval at ``level`` and ``se`` the bootstrap standard
    deviation. Resamples in which ``x`` never varies within a seed are skipped. ``slope`` is None when ``x``
    never varies within any seed; ``ci`` and ``se`` are None with fewer than two seeds.

    Returns ``{slope, ci, se, n_seeds, n_obs, method}``.
    """
    lo_q, hi_q = _tails(level)
    groups: dict[Any, list[tuple[float, float]]] = {}
    for s, x, y in rows:
        if x is None or y is None:
            continue
        x, y = float(x), float(y)
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("x and y must be finite numbers")
        groups.setdefault(s, []).append((x, y))
    seeds = _sorted_keys(groups)
    sums: list[tuple[float, float]] = []
    for s in seeds:
        pts = groups[s]
        mx = sum(x for x, _ in pts) / len(pts)
        my = sum(y for _, y in pts) / len(pts)
        sums.append((sum((x - mx) * (y - my) for x, y in pts), sum((x - mx) ** 2 for x, _ in pts)))
    k = len(sums)
    slope = _pooled_slope(sum(a for a, _ in sums), sum(b for _, b in sums))
    out: dict[str, Any] = {"slope": slope, "ci": [None, None], "se": None, "n_seeds": k,
                           "n_obs": sum(len(g) for g in groups.values()), "method": SLOPE_METHOD}
    if slope is None or k < 2:
        return out
    if n_boot < 1:
        raise ValueError("n_boot must be at least 1")
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(n_boot):
        sxy = sxx = 0.0
        for _ in range(k):
            a, b = sums[rng.randrange(k)]
            sxy += a
            sxx += b
        b_hat = _pooled_slope(sxy, sxx)
        if b_hat is not None:
            draws.append(b_hat)
    if len(draws) >= 2:
        draws.sort()
        out["ci"] = [_percentile(draws, lo_q), _percentile(draws, hi_q)]
        out["se"] = statistics.stdev(draws)
    return out
