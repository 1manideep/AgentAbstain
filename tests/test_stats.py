"""void.research.stats: the standard-library statistics behind the pre-registered decisions."""

from __future__ import annotations

import ast
import math
import random
import sys
from pathlib import Path

import pytest

from void.research import stats as st
from void.research.compare import paired_stats, sign_test_p

STATS_FILE = Path(st.__file__)


# --- the t distribution ------------------------------------------------------------------------------------
def test_t_cdf_reference_values():
    assert 1.0 - st.t_cdf(2.0, 10) == pytest.approx(0.03669, abs=1e-5)
    assert 1.0 - st.t_cdf(2.228, 10) == pytest.approx(0.025, abs=1e-5)
    assert st.t_sf(2.0, 10) == pytest.approx(0.036694017, abs=1e-9)
    assert st.t_cdf(0.0, 7) == 0.5
    assert st.t_cdf(-2.0, 10) == pytest.approx(st.t_sf(2.0, 10), abs=1e-15)


@pytest.mark.parametrize("t", [-30.0, -3.0, -1.0, -0.2, 0.0, 0.4, 1.0, 2.5, 12.0, 400.0])
def test_t_cdf_closed_forms(t):
    # df = 1 is the Cauchy distribution; df = 2 has a closed form too.
    assert st.t_cdf(t, 1) == pytest.approx(0.5 + math.atan(t) / math.pi, abs=1e-13)
    assert st.t_cdf(t, 2) == pytest.approx(0.5 + t / (2.0 * math.sqrt(2.0 + t * t)), abs=1e-13)


def _t_density(x: float, df: float) -> float:
    c = math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2)) / math.sqrt(df * math.pi)
    return c * (1 + x * x / df) ** (-(df + 1) / 2)


@pytest.mark.parametrize("df,t", [(3, 1.7), (4, 4.2426), (10, 2.0), (19, 0.8), (7.5, 3.1)])
def test_t_cdf_matches_numerical_integration(df, t):
    # Independent check: Simpson's rule on the density from 0 to t.
    n = 2000
    h = t / n
    s = _t_density(0, df) + _t_density(t, df)
    s += sum((4 if i % 2 else 2) * _t_density(i * h, df) for i in range(1, n))
    assert st.t_cdf(t, df) == pytest.approx(0.5 + s * h / 3, abs=1e-10)


def test_t_large_df_approaches_the_normal():
    assert st.t_cdf(1.959963984540054, math.inf) == pytest.approx(0.975, abs=1e-12)
    assert st.t_cdf(1.96, 1e6) == pytest.approx(st.t_cdf(1.96, math.inf), abs=1e-6)


def test_t_small_tails_keep_relative_precision():
    # sf of 40 at df 10 is about 1e-12; computing it as 1 - cdf would lose every digit.
    tail = st.t_sf(40.0, 10)
    assert 0 < tail < 1e-10
    assert st.t_cdf(-40.0, 10) == pytest.approx(tail, rel=1e-12)


@pytest.mark.parametrize("df", [1, 2, 4, 10, 19, 33, 120])
@pytest.mark.parametrize("q", [1e-9, 0.001, 0.025, 0.3, 0.5, 0.8, 0.95, 0.975, 0.999])
def test_t_ppf_inverts_t_cdf(df, q):
    t = st.t_ppf(q, df)
    assert st.t_cdf(t, df) == pytest.approx(q, rel=1e-10, abs=1e-15)
    # 1 - q itself is rounded (by about 1e-16 / q relative), so symmetry is checked to that precision.
    assert st.t_ppf(1.0 - q, df) == pytest.approx(-t, rel=1e-6, abs=1e-12)


def test_t_ppf_reference_values():
    assert st.t_ppf(0.975, 10) == pytest.approx(2.228139, abs=1e-6)
    assert st.t_ppf(0.95, 19) == pytest.approx(1.729133, abs=1e-6)
    assert st.t_ppf(0.975, 1) == pytest.approx(math.tan(math.pi * 0.475), rel=1e-10)


def test_t_input_validation():
    with pytest.raises(ValueError):
        st.t_cdf(1.0, 0)
    with pytest.raises(ValueError):
        st.t_ppf(1.0, 5)
    with pytest.raises(ValueError):
        st.t_ppf(0.0, 5)
    with pytest.raises(ValueError):
        st.t_cdf(math.nan, 5)


def test_t_matches_scipy_when_installed():
    scipy_stats = pytest.importorskip("scipy.stats")
    for df in (1, 2.5, 5, 10, 19, 60, 500):
        for t in (-8.0, -2.1, -0.4, 0.0, 0.9, 1.7, 3.3, 15.0):
            assert st.t_cdf(t, df) == pytest.approx(float(scipy_stats.t.cdf(t, df)), abs=1e-12)
        for q in (0.001, 0.05, 0.5, 0.9, 0.995):
            assert st.t_ppf(q, df) == pytest.approx(float(scipy_stats.t.ppf(q, df)), rel=1e-9, abs=1e-12)


# --- paired t ------------------------------------------------------------------------------------------------
def test_paired_t_hand_computed():
    r = st.paired_t([1.0, 2.0, 3.0, 4.0, 5.0])
    assert r["n"] == 5 and r["df"] == 4
    assert r["mean"] == 3.0
    assert r["sd"] == pytest.approx(math.sqrt(2.5))
    assert r["se"] == pytest.approx(math.sqrt(0.5))
    assert r["t"] == pytest.approx(3.0 / math.sqrt(0.5))
    assert r["p_two_sided"] == pytest.approx(2.0 * st.t_sf(r["t"], 4))
    assert r["p_two_sided"] == pytest.approx(0.013236, abs=1e-6)


def test_paired_t_degenerate_inputs():
    assert st.paired_t([])["mean"] is None
    one = st.paired_t([2.0])
    assert one["mean"] == 2.0 and one["t"] is None and one["p_two_sided"] is None
    const = st.paired_t([0.5, 0.5, 0.5])
    assert const["t"] == math.inf and const["p_two_sided"] == 0.0
    zeros = st.paired_t([0.0, 0.0])
    assert zeros["t"] is None and zeros["p_two_sided"] is None
    with pytest.raises(ValueError):
        st.paired_t([1.0, math.nan])


# --- bootstrap ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("data_seed,n", [(1, 20), (2, 7), (3, 2), (4, 45)])
def test_bootstrap_ci_equals_compare_paired_stats(data_seed, n):
    rng = random.Random(data_seed)
    pairs = [(rng.gauss(1.0, 0.7), rng.gauss(1.3, 0.9)) for _ in range(n)]
    diffs = [b - a for a, b in pairs]
    assert st.bootstrap_ci(diffs) == paired_stats(pairs)["ci95"]
    assert st.bootstrap_ci(diffs, n_boot=500, seed=9) == paired_stats(pairs, n_boot=500, seed=9)["ci95"]


def test_bootstrap_ci_shape_and_determinism():
    assert st.bootstrap_ci([]) == [None, None]
    assert st.bootstrap_ci([1.0]) == [None, None]
    d = [0.3, -0.1, 0.4, 0.2, 0.5, 0.0, 0.1]
    lo, hi = st.bootstrap_ci(d)
    assert lo <= sum(d) / len(d) <= hi
    assert st.bootstrap_ci(d) == [lo, hi]
    lo80, hi80 = st.bootstrap_ci(d, level=0.8)
    assert lo <= lo80 <= hi80 <= hi
    with pytest.raises(ValueError):
        st.bootstrap_ci(d, level=1.0)


# --- superiority and non-superiority ---------------------------------------------------------------------
def _normal_diffs(mean: float, sd: float, n: int, seed: int = 0) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


def test_superiority_greater_and_less():
    up = _normal_diffs(1.0, 0.3, 20)
    r = st.superiority(up, 0.0, "greater")
    assert r["passed"] is True and r["ci"][0] > 0 and r["p"] < 0.05
    assert st.superiority(up, 2.0, "greater")["passed"] is False  # the interval does not clear the margin
    assert st.superiority(up, 0.0, "less")["passed"] is False
    assert st.superiority(up, 0.0, "less")["p"] == 1.0
    down = [-x for x in up]
    r = st.superiority(down, 0.5, "less")
    assert r["passed"] is True and r["ci"][1] < -0.5
    assert r["ci"] == st.bootstrap_ci(down)


def test_superiority_p_agrees_with_the_interval_rule():
    for seed in range(12):
        d = _normal_diffs(0.25, 0.5, 20, seed)
        r = st.superiority(d, 0.0, "greater")
        t = st.paired_t(d)
        # p is twice the one-sided p of the margin-shifted t, so p < alpha is the t interval clearing 0.
        assert r["p"] == pytest.approx(min(1.0, 2.0 * st.t_sf(t["t"], t["df"])))
        t_lo = t["mean"] - st.t_ppf(0.975, t["df"]) * t["se"]
        assert (r["p"] < 0.05) == (t_lo > 0)


def test_non_superiority_is_the_complement():
    flat = _normal_diffs(0.0, 0.2, 20)
    r = st.non_superiority(flat, 0.0, "greater")
    assert r["passed"] is True
    assert r["ci"][0] <= 0.0
    up = _normal_diffs(1.0, 0.2, 20)
    assert st.non_superiority(up, 0.0, "greater")["passed"] is False
    assert st.non_superiority(up, 2.0, "greater")["passed"] is True
    for d in (flat, up):
        for margin in (0.0, 0.5):
            for direction in ("greater", "less"):
                assert st.non_superiority(d, margin, direction)["passed"] is (
                    not st.superiority(d, margin, direction)["passed"])


def test_superiority_undetermined_and_validation():
    assert st.superiority([0.4], 0.0, "greater")["passed"] is None
    assert st.non_superiority([], 0.0, "less")["passed"] is None
    with pytest.raises(ValueError):
        st.superiority([1.0, 2.0], 0.0, "up")
    with pytest.raises(ValueError):
        st.superiority([1.0, 2.0], -0.1, "greater")


# --- TOST --------------------------------------------------------------------------------------------------
def test_tost_equivalent_for_a_null_effect():
    d = _normal_diffs(0.0, 0.1, 20, seed=3)
    r = st.tost_paired(d, 0.5)
    assert r["equivalent"] is True
    assert r["p"] == max(r["p_lower"], r["p_upper"]) and r["p"] < 0.05
    assert -0.5 < r["ci90"][0] < r["ci90"][1] < 0.5


def test_tost_not_equivalent_for_a_real_effect():
    d = _normal_diffs(1.0, 0.1, 20, seed=3)
    r = st.tost_paired(d, 0.5)
    assert r["equivalent"] is False
    assert r["p_upper"] > 0.5 and r["p_lower"] < 1e-6
    assert r["ci90"][0] > 0.5


def test_tost_decision_matches_its_90_interval():
    for seed in range(20):
        d = _normal_diffs(0.15, 0.4, 20, seed)
        r = st.tost_paired(d, 0.3)
        lo, hi = r["ci90"]
        assert r["equivalent"] == (-0.3 < lo and hi < 0.3)
        t = st.paired_t(d)
        assert hi - lo == pytest.approx(2 * st.t_ppf(0.95, 19) * t["se"])


def test_tost_degenerate_inputs():
    assert st.tost_paired([0.1], 0.5)["equivalent"] is None
    assert st.tost_paired([0.1, 0.1, 0.1], 0.5)["equivalent"] is True
    assert st.tost_paired([0.6, 0.6], 0.5)["equivalent"] is False
    assert st.tost_paired([0.0, 0.0], 0.0)["equivalent"] is False  # a zero margin is never met


# --- Holm ---------------------------------------------------------------------------------------------------
def test_holm_reference():
    adj = st.holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert list(adj) == ["a", "b", "c"]
    assert adj == pytest.approx({"a": 0.03, "b": 0.06, "c": 0.06})


def test_holm_none_capped_and_monotone():
    adj = st.holm({"x": 0.2, "skip": None, "y": 0.6, "z": 0.001})
    assert adj["skip"] is None
    # m = 3 tested: z -> 0.003, x -> 0.4, y -> max(0.4, 0.6) = 0.6
    assert adj == pytest.approx({"x": 0.4, "skip": None, "y": 0.6, "z": 0.003})
    assert st.holm({"a": 0.5, "b": 0.6}) == {"a": 1.0, "b": 1.0}
    rng = random.Random(5)
    raw = {f"k{i}": rng.random() for i in range(9)}
    adj = st.holm(raw)
    order = sorted(raw, key=raw.get)
    vals = [adj[k] for k in order]
    assert vals == sorted(vals)
    assert all(raw[k] <= adj[k] <= 1.0 for k in raw)
    assert st.holm({}) == {}
    assert st.holm({"only": None}) == {"only": None}
    with pytest.raises(ValueError):
        st.holm({"bad": 1.5})


# --- power ----------------------------------------------------------------------------------------------------
def test_power_paired_reference():
    assert st.power_paired(0.66, 20) == pytest.approx(0.8, abs=0.05)
    # The normal approximation as specified: Phi(|d| sqrt(n) - z_{0.975}).
    expected = 0.5 * (1 + math.erf((0.66 * math.sqrt(20) - 1.959963984540054) / math.sqrt(2)))
    assert st.power_paired(0.66, 20) == pytest.approx(expected, abs=1e-12)
    assert st.power_paired(-0.66, 20) == st.power_paired(0.66, 20)
    # The shifted-t approximation sits close to the exact noncentral t power (0.7995 here).
    assert st.power_paired(0.66, 20, method="t") == pytest.approx(0.8, abs=0.01)


def test_n_for_power():
    n = st.n_for_power(0.66, 0.8)
    assert st.power_paired(0.66, n) >= 0.8 > st.power_paired(0.66, n - 1)
    assert n == 19
    # With the t approximation the seed counts in MEMORY_EVOLUTION 6.11 figure 4 come back:
    # 15 seeds for d = 0.8 and 34 for d = 0.5.
    assert st.n_for_power(0.8, 0.8, method="t") == 15
    assert st.n_for_power(0.5, 0.8, method="t") == 34
    assert st.n_for_power(5.0, 0.8) == 2
    with pytest.raises(ValueError):
        st.n_for_power(0.0, 0.8)


# --- sign test ---------------------------------------------------------------------------------------------------
def test_sign_test_exact_and_matches_compare():
    assert st.sign_test(0, 0) is None
    assert st.sign_test(10, 0) == pytest.approx(2.0 / 2**10)
    assert st.sign_test(3, 3) == 1.0
    for pos in range(0, 12):
        for neg in range(0, 12):
            assert st.sign_test(pos, neg) == sign_test_p(pos, neg)
    with pytest.raises(ValueError):
        st.sign_test(-1, 2)


# --- 2x2 interaction -----------------------------------------------------------------------------------------------
def _factorial(main_a: float, main_b: float, inter: float, *, seeds=range(20), noise=0.0, seed=0):
    rng = random.Random(seed)
    cells: dict[tuple[int, int], dict[int, float]] = {(a, b): {} for a in (0, 1) for b in (0, 1)}
    for s in seeds:
        base = rng.gauss(0.0, 5.0)  # a large per-seed shift that the paired design removes
        for a in (0, 1):
            for b in (0, 1):
                cells[(a, b)][s] = base + main_a * a + main_b * b + inter * a * b + rng.gauss(0.0, noise)
    return cells


def test_interaction_2x2_recovers_planted_effects_exactly():
    r = st.interaction_2x2(_factorial(1.0, 0.5, 2.0))
    assert r["n"] == 20 and r["dropped"] == 0
    # main_a = ((y10 + y11) - (y00 + y01)) / 2 = (1 + 3.5 - 0.5) / 2 = 2.0; main_b = (0.5 + 3.5 - 1) / 2 = 1.5
    assert r["interaction"]["mean"] == pytest.approx(2.0)
    assert r["main_a"]["mean"] == pytest.approx(2.0)
    assert r["main_b"]["mean"] == pytest.approx(1.5)
    assert r["interaction"]["positive"] == 20
    assert r["interaction"]["sign_test_p"] == pytest.approx(2.0 / 2**20)


def test_interaction_2x2_recovers_planted_interaction_under_noise():
    r = st.interaction_2x2(_factorial(0.0, 0.3, 2.0, noise=0.4, seed=7))
    lo, hi = r["interaction"]["ci"]
    assert lo < 2.0 < hi and lo > 0
    assert r["interaction"]["mean"] == pytest.approx(2.0, abs=0.3)
    assert r["interaction"]["sign_test_p"] < 0.001
    per_seed = [r["per_seed"][str(s)]["interaction"] for s in r["seeds"]]
    assert sum(per_seed) / len(per_seed) == pytest.approx(r["interaction"]["mean"], rel=1e-12)
    null = st.interaction_2x2(_factorial(1.0, 1.0, 0.0, noise=0.4, seed=7))
    lo, hi = null["interaction"]["ci"]
    assert lo < 0 < hi


def test_interaction_2x2_uses_only_seeds_in_every_cell():
    cells = _factorial(1.0, 0.5, 2.0, seeds=range(6))
    del cells[(1, 1)][5]
    cells[(0, 1)][4] = None
    cells[(0, 0)][99] = 3.0
    r = st.interaction_2x2(cells)
    assert r["seeds"] == [0, 1, 2, 3]
    assert r["n"] == 4 and r["dropped"] == 3
    assert r["interaction"]["ci"] == st.bootstrap_ci([r["per_seed"][str(s)]["interaction"] for s in r["seeds"]])


def test_interaction_2x2_validation():
    cells = _factorial(0.0, 0.0, 0.0, seeds=range(3))
    del cells[(1, 0)]
    with pytest.raises(ValueError):
        st.interaction_2x2(cells)
    cells = _factorial(0.0, 0.0, 0.0, seeds=range(3))
    cells[(2, 0)] = {0: 1.0}
    with pytest.raises(ValueError):
        st.interaction_2x2(cells)


# --- slope with a random intercept per seed ------------------------------------------------------------------------
def _generations(slope: float, *, seeds=20, gens=6, sd_intercept=10.0, noise=0.3, seed=0):
    rng = random.Random(seed)
    rows = []
    for s in range(seeds):
        intercept = rng.gauss(0.0, sd_intercept)
        for g in range(gens):
            rows.append((s, g, intercept + slope * g + rng.gauss(0.0, noise)))
    return rows


def test_slope_by_seed_recovers_planted_slope_under_random_intercepts():
    r = st.slope_by_seed(_generations(0.7))
    assert r["n_seeds"] == 20 and r["n_obs"] == 120
    assert r["slope"] == pytest.approx(0.7, abs=0.05)
    lo, hi = r["ci"]
    assert lo < 0.7 < hi
    assert hi - lo < 0.2  # the intercepts (sd 10) are removed, so the interval is tight
    assert 0 < r["se"] < 0.1
    assert "cluster bootstrap by seed" in r["method"]
    assert st.slope_by_seed(_generations(0.7)) == r  # deterministic


def test_slope_by_seed_equals_pooled_ols_in_a_balanced_design():
    rows = _generations(-0.4, seeds=8, gens=5, seed=3)
    xs = [x for _, x, _ in rows]
    ys = [y for _, _, y in rows]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    pooled = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sum((x - mx) ** 2 for x in xs)
    assert st.slope_by_seed(rows)["slope"] == pytest.approx(pooled, rel=1e-12)


def test_slope_by_seed_ignores_intercepts_that_track_x():
    # Unbalanced: seeds with high intercepts are observed at high x. Pooled OLS is biased; the within
    # estimator is not.
    rng = random.Random(11)
    rows = []
    for s in range(15):
        intercept = 3.0 * s
        for g in range(s, s + 4):
            rows.append((s, float(g), intercept + 0.5 * g + rng.gauss(0.0, 0.1)))
    r = st.slope_by_seed(rows)
    assert r["slope"] == pytest.approx(0.5, abs=0.05)
    xs = [x for _, x, _ in rows]
    ys = [y for _, _, y in rows]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    pooled = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sum((x - mx) ** 2 for x in xs)
    assert pooled > 2.0


def test_slope_by_seed_degenerate_inputs():
    empty = st.slope_by_seed([])
    assert empty["slope"] is None and empty["ci"] == [None, None] and empty["n_seeds"] == 0
    flat_x = st.slope_by_seed([(0, 1.0, 2.0), (0, 1.0, 3.0), (1, 2.0, 5.0)])
    assert flat_x["slope"] is None
    one_seed = st.slope_by_seed([(0, 0.0, 1.0), (0, 1.0, 3.0), (0, None, 9.0)])
    assert one_seed["slope"] == pytest.approx(2.0)
    assert one_seed["n_obs"] == 2 and one_seed["ci"] == [None, None] and one_seed["se"] is None
    with pytest.raises(ValueError):
        st.slope_by_seed([(0, 0.0, math.inf)])


# --- no new dependency ---------------------------------------------------------------------------------------------
def test_stats_imports_only_the_standard_library():
    tree = ast.parse(STATS_FILE.read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert roots, "expected some imports"
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}, roots - set(sys.stdlib_module_names)
