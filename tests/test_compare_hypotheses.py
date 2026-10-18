"""Pre-registered decisions in the comparison (MEMORY_EVOLUTION 5.2, 6.2, 6.11; PLAN section 8 Q2; DESIGN section 17).

Synthetic experiment directories, no simulation: every run is a ``config.resolved.yaml`` written from ``base.yaml``
with overrides, a ``world.db`` holding a completed run row (and, for the per-tier analysis, decide calls with claims)
and a ``metrics.jsonl`` whose tick rows carry ``mean_balance``. Effects are planted as exact per-seed differences, so
every verdict below is known in advance. Covered: superiority pass and fail, the ``min_power`` failure,
non-superiority, equivalence pass and fail (absolute and SD margins), the Holm adjustment (identical data passes as an
exploratory hypothesis and fails as one of three primaries), arms that disagree on the hypotheses, a hypothesis naming
an arm without runs, a recovered 2x2 interaction, the per-tier difference-in-differences, defaults filled in before
configs are compared, the bootstrap-versus-t boundary of ``stats.superiority``, the scarcity configs (two scripted tiers,
arms that differ only in their declared variables) and ``scripts/compare.py --hypotheses-only``'s exit codes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import random
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from void.config import VoidConfig, load_config
from void.db import Database
from void.research import stats as st
from void.research.analysis import COMPUTED
from void.research.compare import (
    TIER_INVARIANTS,
    TIER_OUTCOMES,
    IncomparableRuns,
    compare,
    config_diff,
    covered_by,
    evaluate_hypothesis,
    factorial_design,
    render,
    render_hypotheses,
)

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"
DAYS, TPD = 1, 2
SEEDS = tuple(range(1, 9))

# planted per-seed differences (arm minus base), one entry per seed in SEEDS
UP = [0.9, 1.1, 1.0, 1.2, 0.8, 1.05, 0.95, 1.0]           # mean 1.0, d_z about 8: clear
MID = [0.0, 1.0, 0.4, 0.1, 1.2, 0.3, 0.8, 0.2]            # mean 0.5, d_z about 1.13: p 0.015, power 0.89
HOLM = [-0.1, 1.1, 0.5, 0.1, 1.2, 0.1, 0.9, 0.2]          # mean 0.5, d_z about 0.99: p 0.026 (fails once doubled)
FLAT = [0.04, -0.03, 0.02, -0.05, 0.01, 0.03, -0.02, -0.01]  # mean about 0
NOISY = [0.9, -1.1, 0.4, -0.6, 1.2, -0.9, 0.3, 0.6]       # mean 0.1, sd 0.86: nothing to detect
BOUNDARY = [0.59, 1.75, -0.43, 1.49, 0.24, 0.24]          # bootstrap CI clears 0, t p = 0.114


# --- synthetic runs ----------------------------------------------------------------------------------------------
def _nest(dotted: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, value in dotted.items():
        *parents, key = path.split(".")
        node = out
        for p in parents:
            node = node.setdefault(p, {})
        node[key] = value
    return out


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


_ARM_CONFIGS: dict[str, VoidConfig] = {}
_DUMPER = getattr(yaml, "CSafeDumper", yaml.SafeDumper)


def _config(exp: str, arm: str, seed: int, ivs: list[str], sets: dict[str, Any], hypotheses: list[dict[str, Any]],
            primary: str = "mean_balance", agents: list[dict[str, Any]] | None = None) -> VoidConfig:
    """``base.yaml`` with the experiment block, ``sets`` (dotted paths) and ``agents``; loaded once per arm."""
    over: dict[str, Any] = {
        "run": {"name": f"{exp}_{arm}", "days": DAYS, "ticks_per_day": TPD},
        "entropy": {"degeneration": {"mode": "observe"}},
        "experiment": {"name": exp, "arm": arm, "independent_variables": ivs, "primary_outcome": primary,
                       "hypotheses": hypotheses},
    }
    if agents is not None:
        over["agents"] = agents
    over = _merge(over, _nest(sets))
    key = json.dumps(over, sort_keys=True, default=str)
    if key not in _ARM_CONFIGS:
        _ARM_CONFIGS[key] = load_config(CONFIGS / "base.yaml", over)
    cfg = _ARM_CONFIGS[key].model_copy(deep=True)
    cfg.run.seed = seed
    return cfg


def _write_run(run_dir: Path, cfg: VoidConfig, *, mean_balance: float,
               calls: Sequence[tuple[str, int, int]] = (), config_text: str | None = None) -> None:
    """A completed run: the resolved config, a run row with status ``completed``, ``calls`` as decide calls
    ``(tier, claims_made, claims_false)`` at tick 1, and tick rows up to the last tick carrying ``mean_balance``."""
    run_dir.mkdir(parents=True)
    text = yaml.dump(cfg.canonical_dict(), Dumper=_DUMPER, sort_keys=True)  # cfg.to_yaml(), through libyaml
    (run_dir / "config.resolved.yaml").write_text(config_text if config_text is not None else text)
    last = cfg.run.days * cfg.run.ticks_per_day
    db = Database(run_dir / "world.db")
    try:
        db.execute("INSERT INTO run(run_id, config_hash, config_yaml, seed, created_at, schema_version, status, ended_tick) "
                   "VALUES(?,?,?,?,?,?,?,?)", (f"r{cfg.run.seed}", "synthetic", text, cfg.run.seed,
                                               "2026-01-01T00:00:00", 2, "completed", last))
        for i, (tier, made, false) in enumerate(calls):
            db.execute("INSERT INTO llm_calls(call_id, tick, agent_id, purpose, tier, model, provider, real_cost, world_cost, "
                       "claims_made, claims_false) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (f"c{i}", 1, f"a{i}", "decide", tier, "scripted-v1", "scripted", 0, 0, made, false))
    finally:
        db.close()
    rows = [{"tick": t, "day": (t - 1) // cfg.run.ticks_per_day + 1, "mean_balance": mean_balance} for t in range(1, last + 1)]
    rows += [{"kind": "day", "day": d, "mean_balance": mean_balance} for d in range(1, cfg.run.days + 1)]
    (run_dir / "metrics.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def _base_value(seed: int) -> float:
    return 2.0 + 0.1 * seed + 0.05 * math.sin(seed)


def _planted(root: Path, diffs: dict[str, list[float] | None], hypotheses: list[dict[str, Any]],
             hyps_by_arm: dict[str, list[dict[str, Any]]] | None = None) -> Path:
    """Arms named by ``diffs``: ``None`` is the baseline, a list the per-seed difference from it (seeds 1..n). The
    arms differ in ``world.scarcity`` (declared)."""
    n = max(len(d) for d in diffs.values() if d)
    for i, (arm, d) in enumerate(diffs.items()):
        for j, seed in enumerate(range(1, n + 1)):
            cfg = _config("exp_synth", arm, seed, ["world.scarcity"], {"world.scarcity": round(1.0 - 0.1 * i, 2)},
                          (hyps_by_arm or {}).get(arm, hypotheses))
            _write_run(root / arm / f"seed_{seed}", cfg, mean_balance=_base_value(seed) + (d[j] if d else 0.0))
    return root


def _hyp(name: str, arm_b: str, kind: str = "superiority", **kw: Any) -> dict[str, Any]:
    return {"name": name, "arm_a": "base", "arm_b": arm_b, "outcome": "mean_balance", "kind": kind, **kw}


MAIN_HYPS = [
    _hyp("sup_pass", "up", margin=0.2),
    _hyp("sup_margin_not_cleared", "up", margin=1.5, primary=False),
    _hyp("sup_wrong_direction", "up", direction="less", primary=False),
    _hyp("sup_noise", "noisy", primary=False),
    _hyp("power_enough", "mid", min_power=0.8, primary=False),
    _hyp("power_short", "mid", min_power=0.95, primary=False),
    _hyp("nonsup_flat", "flat", kind="non_superiority", primary=False),
    _hyp("nonsup_up", "up", kind="non_superiority", primary=False),
    _hyp("equiv_flat", "flat", kind="equivalence", margin=0.5, primary=False),
    _hyp("equiv_up", "up", kind="equivalence", margin=0.5, primary=False),
    _hyp("equiv_flat_sd", "flat", kind="equivalence", margin=1.0, margin_unit="sd", primary=False),
    _hyp("equiv_noisy_quarter_sd", "noisy", kind="equivalence", margin=0.25, margin_unit="sd", primary=False),
]


@pytest.fixture(scope="module")
def main_exp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _planted(tmp_path_factory.mktemp("main") / "exp_synth",
                    {"base": None, "up": UP, "mid": MID, "flat": FLAT, "noisy": NOISY}, MAIN_HYPS)


@pytest.fixture(scope="module")
def main_result(main_exp: Path) -> dict[str, Any]:
    return compare(main_exp)


def _rows(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {h["name"]: h for h in result["hypotheses"]}


def _diffs(h: dict[str, Any]) -> list[float]:
    """The per-seed differences a hypothesis row was decided on (``arm_b - arm_a`` as stored, so float-exact)."""
    return [b - a for a, b in h["per_seed"].values()]


# --- superiority -------------------------------------------------------------------------------------------------
def test_superiority_passes_when_the_interval_clears_the_margin(main_result: dict[str, Any]) -> None:
    h = _rows(main_result)["sup_pass"]
    assert h["passed"] is True and h["kind"] == "superiority" and h["outcome"] == "mean_balance"
    assert h["n"] == len(SEEDS) and h["dropped"] == 0
    assert h["delta"] == pytest.approx(sum(UP) / len(UP))
    d = _diffs(h)
    assert d == pytest.approx(UP)
    assert h["ci"] == st.bootstrap_ci(d) and h["ci"][0] > 0.2
    assert h["p"] == st.superiority(d, 0.2, "greater")["p"] and h["p"] < 0.05
    assert h["p_holm"] == h["p"]  # the only primary hypothesis
    assert h["d_z"] == pytest.approx(st.paired_t(UP)["mean"] / st.paired_t(UP)["sd"])
    assert "clears > 0.2" in h["reason"]
    assert h["per_seed"]["1"] == [pytest.approx(_base_value(1)), pytest.approx(_base_value(1) + UP[0])]


def test_superiority_fails_on_a_margin_not_cleared_the_wrong_side_or_noise(main_result: dict[str, Any]) -> None:
    rows = _rows(main_result)
    margin = rows["sup_margin_not_cleared"]
    assert margin["passed"] is False and "does not clear > 1.5" in margin["reason"]
    assert margin["p_holm"] is None  # exploratory
    wrong = rows["sup_wrong_direction"]
    assert wrong["passed"] is False and "upper bound" in wrong["reason"] and wrong["p"] == 1.0
    noise = rows["sup_noise"]
    assert noise["passed"] is False and noise["ci"][0] < 0 < noise["ci"][1]


def test_min_power_failure(main_result: dict[str, Any]) -> None:
    rows = _rows(main_result)
    enough, short = rows["power_enough"], rows["power_short"]
    d_z = st.paired_t(MID)["mean"] / st.paired_t(MID)["sd"]
    expected = st.power_paired(d_z, len(MID))  # margin 0: the power at the observed d_z
    assert enough["power"] == pytest.approx(expected) == short["power"]
    assert 0.8 <= expected < 0.95
    assert enough["passed"] is True and short["passed"] is False
    assert short["ci"][0] > 0 and short["p"] < 0.05  # it fails only on power
    assert "power" in short["reason"] and "< min_power 0.95" in short["reason"]


def test_power_beyond_a_margin_is_lower_than_at_d_z() -> None:
    h = {"name": "h", "kind": "superiority", "direction": "greater", "margin": 0.3, "margin_unit": "abs"}
    r = evaluate_hypothesis(h, MID)
    sd = st.paired_t(MID)["sd"]
    assert r["power"] == pytest.approx(st.power_paired((0.5 - 0.3) / sd, len(MID)))
    assert r["power"] < st.power_paired(0.5 / sd, len(MID))
    assert evaluate_hypothesis({**h, "margin": 0.0}, MID)["power"] == pytest.approx(st.power_paired(0.5 / sd, len(MID)))


# --- non-superiority and equivalence ---------------------------------------------------------------------------------
def test_non_superiority(main_result: dict[str, Any]) -> None:
    rows = _rows(main_result)
    flat, up = rows["nonsup_flat"], rows["nonsup_up"]
    assert flat["passed"] is True and "does not hold" in flat["reason"]
    assert flat["p"] == st.non_superiority(_diffs(flat), 0.0, "greater")["p"]
    assert up["passed"] is False and "holds" in up["reason"]
    assert st.non_superiority(UP, 0.0, "greater")["passed"] is False


def test_equivalence_pass_and_fail(main_result: dict[str, Any]) -> None:
    rows = _rows(main_result)
    flat, up = rows["equiv_flat"], rows["equiv_up"]
    tost = st.tost_paired(_diffs(flat), 0.5)
    assert flat["passed"] is True and flat["p"] == tost["p"] and flat["ci"] == tost["ci90"]
    assert flat["ci_level"] == pytest.approx(0.90) and "TOST p" in flat["reason"]
    assert flat["power"] == pytest.approx(st.power_tost(0.5 / st.paired_t(FLAT)["sd"], len(FLAT)))
    assert up["passed"] is False and up["p"] >= 0.05


def test_equivalence_margin_in_sd_units_of_the_paired_differences(main_result: dict[str, Any]) -> None:
    rows = _rows(main_result)
    sd_flat, sd_noisy = st.paired_t(FLAT)["sd"], st.paired_t(NOISY)["sd"]
    flat = rows["equiv_flat_sd"]
    assert flat["margin_abs"] == pytest.approx(sd_flat) and flat["passed"] is True
    quarter = rows["equiv_noisy_quarter_sd"]
    assert quarter["margin_abs"] == pytest.approx(0.25 * sd_noisy)
    # 0.25 SD of the data at 8 pairs: 0.25 * sqrt(8) < t(0.95, 7), so equivalence cannot be shown
    assert quarter["passed"] is False and quarter["p"] >= 0.05


# --- Holm ------------------------------------------------------------------------------------------------------------
def test_holm_adjusts_across_the_primary_hypotheses(tmp_path: Path) -> None:
    hyps = [_hyp("up", "up"), _hyp("mid_a", "mid"), _hyp("mid_b", "mid"), _hyp("mid_exploratory", "mid", primary=False)]
    result = compare(_planted(tmp_path / "exp_synth", {"base": None, "up": UP, "mid": HOLM}, hyps))
    rows = _rows(result)
    primaries = {n: rows[n]["p"] for n in ("up", "mid_a", "mid_b")}
    assert {n: rows[n]["p_holm"] for n in primaries} == pytest.approx(st.holm(primaries))
    p_mid = rows["mid_a"]["p"]
    assert p_mid < 0.05 <= 2 * p_mid
    assert rows["mid_a"]["p_holm"] == pytest.approx(2 * p_mid)  # second smallest of three
    assert rows["mid_b"]["p_holm"] == pytest.approx(2 * p_mid)  # the running maximum
    assert rows["up"]["passed"] is True
    for name in ("mid_a", "mid_b"):  # the same data that passes unadjusted fails after the correction
        assert rows[name]["passed"] is False and rows[name]["ci"][0] > 0
        assert f"p_holm {rows[name]['p_holm']:.4g} >= 0.05" in rows[name]["reason"]
    explore = rows["mid_exploratory"]
    assert explore["passed"] is True and explore["p_holm"] is None and explore["p"] == p_mid
    text = render_hypotheses(result)
    assert "Holm across 3 primary" in text
    assert "  FAIL  mid_a [superiority greater, margin 0, primary] mid - base on mean_balance" in text
    assert "  PASS  mid_exploratory [superiority greater, margin 0, exploratory]" in text


# --- reading the hypotheses ---------------------------------------------------------------------------------------
def test_arms_that_disagree_on_the_hypotheses_are_refused(tmp_path: Path) -> None:
    hyps = [_hyp("h", "up", margin=0.2)]
    other = [_hyp("h", "up", margin=0.3)]
    exp = _planted(tmp_path / "exp_synth", {"base": None, "up": UP}, hyps, hyps_by_arm={"up": other})
    with pytest.raises(IncomparableRuns, match=r"disagree on experiment\.hypotheses.*experiment\.hypotheses\.0\.margin"):
        compare(exp)
    script = _load_script()
    assert script.main([str(exp), "--hypotheses-only"]) == 2


def test_a_hypothesis_naming_an_arm_without_runs_fails(tmp_path: Path) -> None:
    hyps = [_hyp("h", "up"), _hyp("ghost", "missing", primary=False)]
    rows = _rows(compare(_planted(tmp_path / "exp_synth", {"base": None, "up": UP}, hyps)))
    assert rows["h"]["passed"] is True
    assert rows["ghost"]["passed"] is False and rows["ghost"]["n"] == 0 and "no runs" in rows["ghost"]["reason"]


def test_without_hypotheses_the_result_holds_an_empty_list(tmp_path: Path) -> None:
    result = compare(_planted(tmp_path / "exp_synth", {"base": None, "up": UP}, []))
    assert result["hypotheses"] == [] and result["factorial"] is None and result["tier_did"] is None
    assert "PASS" not in render(result) and "FAIL" not in render(result)
    assert render_hypotheses(result).startswith("pre-registered hypotheses: none")


def test_render_prints_one_verdict_line_per_hypothesis(main_result: dict[str, Any]) -> None:
    text = render(main_result)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(("PASS ", "FAIL "))]
    assert len(lines) == len(MAIN_HYPS)
    for h in main_result["hypotheses"]:
        assert any(ln.startswith(f"{'PASS' if h['passed'] else 'FAIL'}  {h['name']} [") for ln in lines), h["name"]
    json.dumps(main_result)  # the result serializes as it is


# --- the boundary between the bootstrap interval and the t-test ---------------------------------------------------------
def test_superiority_verdict_never_disagrees_with_its_p() -> None:
    r = st.superiority(BOUNDARY, 0.0, "greater")
    assert r["ci_clears"] is True and r["ci"][0] > 0  # the percentile bootstrap alone would pass
    assert r["p"] >= 0.05 and r["p_clears"] is False
    assert r["passed"] is False
    non = st.non_superiority(BOUNDARY, 0.0, "greater")
    assert non["passed"] is True and non["p"] == r["p"]
    for seed in range(60):  # a PASS always carries p < alpha
        rng = random.Random(seed)
        d = [rng.gauss(0.5, 1.0) for _ in range(6)]
        for direction in ("greater", "less"):
            s = st.superiority(d if direction == "greater" else [-x for x in d], 0.0, direction)
            assert s["passed"] == (s["ci_clears"] and s["p"] < 0.05)


def test_a_boundary_hypothesis_fails_on_its_p(tmp_path: Path) -> None:
    rows = _rows(compare(_planted(tmp_path / "exp_synth", {"base": None, "b": BOUNDARY}, [_hyp("edge", "b")])))
    h = rows["edge"]
    assert h["n"] == len(BOUNDARY) and h["ci"][0] > 0 and h["ci_clears"] is True
    assert h["passed"] is False and h["p"] >= 0.05 and h["p_holm"] == h["p"]
    assert "clears > 0" in h["reason"] and "p_holm" in h["reason"] and ">= 0.05" in h["reason"]


def test_power_tost() -> None:
    assert st.power_tost(0.0, 20) == 0.0
    assert st.power_tost(0.5, 10) < st.power_tost(0.5, 40) < st.power_tost(1.0, 40) <= 1.0
    with pytest.raises(ValueError):
        st.power_tost(0.5, 0)


# --- factorial ------------------------------------------------------------------------------------------------------
PRESS = {"none": (False, False), "gossip": (False, True), "chronicle": (True, False), "both": (True, True)}
MAIN_A, MAIN_B_ONLY, INTER = 0.5, 0.25, 0.75   # y = mu + MAIN_A*A + MAIN_B_ONLY*B + INTER*A*B + noise


@pytest.fixture(scope="module")
def press_exp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("press") / "exp_press"
    rng = random.Random(7)
    for arm, (a, b) in PRESS.items():
        for seed in SEEDS:
            cfg = _config("exp_press", arm, seed, ["chronicle.enabled", "gossip.enabled"],
                          {"chronicle.enabled": a, "gossip.enabled": b}, [])
            y = _base_value(seed) + MAIN_A * a + MAIN_B_ONLY * b + INTER * a * b + rng.gauss(0.0, 0.05)
            _write_run(root / arm / f"seed_{seed}", cfg, mean_balance=y)
    return root


def test_a_planted_2x2_interaction_is_recovered(press_exp: Path) -> None:
    result = compare(press_exp)
    f = result["factorial"]
    assert f is not None and f["outcome"] == "mean_balance" and f["n"] == len(SEEDS) and f["dropped"] == 0
    assert f["factor_a"] == {"path": "chronicle.enabled", "levels": [False, True]}
    assert f["factor_b"] == {"path": "gossip.enabled", "levels": [False, True]}
    assert f["cells"] == {"0,0": "none", "0,1": "gossip", "1,0": "chronicle", "1,1": "both"}
    # main effects average over the other factor: A = MAIN_A + INTER/2, B = MAIN_B_ONLY + INTER/2
    assert f["main_a"]["mean"] == pytest.approx(MAIN_A + INTER / 2, abs=0.06)
    assert f["main_b"]["mean"] == pytest.approx(MAIN_B_ONLY + INTER / 2, abs=0.06)
    assert f["interaction"]["mean"] == pytest.approx(INTER, abs=0.1)
    lo, hi = f["interaction"]["ci"]
    assert 0 < lo < INTER < hi
    text = render(result)
    assert "factorial 2x2 on mean_balance" in text and "interaction A x B" in text


def test_factorial_design_needs_four_crossed_arms() -> None:
    def rep(a: Any, b: Any, c: Any = 1) -> dict[str, Any]:
        return {"x": {"a": a, "b": b, "c": c}}

    ivs = ["x.a", "x.b", "x.c"]
    crossed = {"p": rep(0, 0), "q": rep(0, 1), "r": rep(1, 0), "s": rep(1, 1)}
    design = factorial_design(crossed, ivs)
    assert design is not None and design["cells"] == {(0, 0): "p", (0, 1): "q", (1, 0): "r", (1, 1): "s"}
    assert factorial_design({k: crossed[k] for k in "pqr"}, ivs) is None                       # three arms
    assert factorial_design({**crossed, "s": rep(0, 1)}, ivs) is None                           # a cell twice
    assert factorial_design({**crossed, "s": rep(1, 2)}, ivs) is None                           # three levels
    assert factorial_design({**crossed, "s": rep(1, 1, 2)}, ivs) is None                        # three IVs vary
    numeric = factorial_design({"p": rep(10, 0), "q": rep(10, 1), "r": rep(9, 0), "s": rep(9, 1)}, ivs)
    assert numeric is not None and numeric["factors"][0]["levels"] == [9, 10]                  # numeric order


# --- per-tier difference-in-differences ----------------------------------------------------------------------------------
TIER_RATES = {  # arm -> tier -> false claim rate (claims_false of 200)
    "high": {"scripted": 0.30, "scripted_b": 0.50},
    "low": {"scripted": 0.20, "scripted_b": 0.20},
}
TWO_TIERS = [{"name": "Ada", "tier": "scripted"}, {"name": "Bao", "tier": "scripted_b"}]


def _scripted_b() -> dict[str, Any]:
    """A second scripted tier: base.yaml's ``scripted`` with a sharper hidden softmax."""
    return {**load_config(CONFIGS / "base.yaml").tiers["scripted"].model_dump(mode="json"), "scripted_beta": 2.0, "color": "#f687b3"}


def test_per_tier_difference_in_differences(tmp_path: Path) -> None:
    root = tmp_path / "exp_scarcity_synth"
    for i, (arm, rates) in enumerate(TIER_RATES.items()):
        for seed in SEEDS:
            cfg = _config("exp_scarcity_synth", arm, seed, ["world.scarcity"],
                          {"world.scarcity": 0.5 + 0.7 * i, "tiers.scripted_b": _scripted_b(), "economy.world_pricing": "equalized"},
                          [], primary="false_claim_rate",
                          agents=TWO_TIERS)
            jitter = (seed % 3) * 2  # the same shift in both tiers: it cancels in the per-tier deltas
            calls = [(t, 200, round(r * 200) + jitter) for t, r in rates.items()]
            _write_run(root / arm / f"seed_{seed}", cfg, mean_balance=1.0, calls=calls)
    result = compare(root)
    did = result["tier_did"]
    assert did is not None and did["tiers"] == ["scripted", "scripted_b"]
    (row,) = did["rows"]
    assert (row["outcome"], row["arm_a"], row["arm_b"]) == ("false_claim_rate", "high", "low")
    assert row["per_tier"]["scripted"]["delta"] == pytest.approx(-0.10)
    assert row["per_tier"]["scripted_b"]["delta"] == pytest.approx(-0.30)
    assert row["per_tier"]["scripted"]["path"] == "false_claim_rate.scripted"
    (diff,) = row["differences"]
    assert (diff["tier_a"], diff["tier_b"], diff["n"]) == ("scripted", "scripted_b", len(SEEDS))
    assert diff["did"] == pytest.approx(-0.20)
    assert diff["ci95"] == [pytest.approx(-0.20), pytest.approx(-0.20)]
    assert all(v == pytest.approx(-0.20) for v in diff["per_seed"].values())
    assert "scripted_b - scripted: did=-0.2000" in render(result)


def test_tier_outcomes_are_computed_outcomes() -> None:
    assert set(TIER_OUTCOMES) <= set(COMPUTED) and "notes_by_channel" not in TIER_OUTCOMES


# --- configs are compared with their defaults filled in --------------------------------------------------------------
def test_a_block_left_at_its_defaults_compares_with_one_turned_on(tmp_path: Path) -> None:
    root = tmp_path / "exp_veins"
    for arm, on in (("off", False), ("on", True)):
        for seed in (1, 2):
            cfg = _config("exp_veins", arm, seed, ["world.veins.enabled"], {"world.veins.enabled": on}, [])
            _write_run(root / arm / f"seed_{seed}", cfg, mean_balance=1.0 + 0.1 * seed + 0.2 * on)
    stored = yaml.safe_load((root / "off" / "seed_1" / "config.resolved.yaml").read_text())
    assert "veins" not in stored["world"]  # the stored file omits the block at its defaults
    assert "veins" in yaml.safe_load((root / "on" / "seed_1" / "config.resolved.yaml").read_text())["world"]
    result = compare(root)
    assert result["comparisons"][0]["primary"]["n"] == 2


def test_runs_stored_before_and_after_a_late_field_compare(tmp_path: Path) -> None:
    root = tmp_path / "exp_synth"
    for arm, scarcity in (("a", 1.0), ("b", 0.8)):
        for seed in (1, 2):
            cfg = _config("exp_synth", arm, seed, ["world.scarcity"], {"world.scarcity": scarcity}, [])
            text = None
            if seed == 2:  # written before the canonical form dropped late fields: world.fallow_ticks spelled out
                data = yaml.safe_load(cfg.to_yaml())
                data["world"]["fallow_ticks"] = 0
                text = yaml.safe_dump(data, sort_keys=True)
            _write_run(root / arm / f"seed_{seed}", cfg, mean_balance=1.0 + seed + scarcity, config_text=text)
    assert "fallow_ticks" in (root / "a" / "seed_2" / "config.resolved.yaml").read_text()
    assert "fallow_ticks" not in (root / "a" / "seed_1" / "config.resolved.yaml").read_text()
    assert compare(root)["comparisons"][0]["primary"]["n"] == 2


def test_configs_are_compared_as_load_config_returns_them(main_exp: Path) -> None:
    from void.research.compare import _load_cfg

    run = main_exp / "up" / "seed_3"
    loaded = _load_cfg(run)
    assert loaded == load_config(run / "config.resolved.yaml").model_dump(mode="json")
    assert loaded["world"]["veins"]["enabled"] is False and loaded["world"]["fallow_ticks"] == 0  # absent from the file
    assert [h["name"] for h in loaded["experiment"]["hypotheses"]] == [h["name"] for h in MAIN_HYPS]


def test_a_resolved_config_that_does_not_load_is_refused(tmp_path: Path) -> None:
    exp = _planted(tmp_path / "exp_synth", {"base": None, "up": UP}, [])
    path = exp / "up" / "seed_1" / "config.resolved.yaml"
    path.write_text(path.read_text() + "not_a_section: 1\n")
    with pytest.raises(IncomparableRuns, match="does not load as a config"):
        compare(exp)


# --- the scarcity configs ------------------------------------------------------------------------------------------
def test_scarcity_configs_roster_two_scripted_tiers_and_differ_only_in_declared_ivs() -> None:
    high, low = load_config(CONFIGS / "exp_scarcity_high.yaml"), load_config(CONFIGS / "exp_scarcity_low.yaml")
    for cfg in (high, low):
        assert cfg.experiment.name == "exp_scarcity" and cfg.warnings() == []
        assert cfg.economy.world_pricing == "equalized" and cfg.entropy.degeneration.mode == "observe"
        assert cfg.population.tier_mutation_prob == 0
        rostered = sorted({a.tier for a in cfg.agents})
        assert rostered == ["scripted", "scripted_b"]
        assert all(cfg.tiers[t].provider == "scripted" for t in rostered)
        assert sum(a.tier == "scripted" for a in cfg.agents) == sum(a.tier == "scripted_b" for a in cfg.agents)
        a, b = cfg.tiers["scripted"].model_dump(), cfg.tiers["scripted_b"].model_dump()
        assert {k for k in a if a[k] != b[k]} == {"scripted_beta", "color"}
        assert {k: a[k] for k in TIER_INVARIANTS} == {k: b[k] for k in TIER_INVARIANTS}
    assert high.tiers == low.tiers and high.agents == low.agents
    ivs = sorted(set(high.experiment.independent_variables) | set(low.experiment.independent_variables))
    diff = config_diff(high.model_dump(mode="json"), low.model_dump(mode="json"))
    assert sorted(d for d in diff if d not in ("run.name", "experiment.arm")) == ["population.starting_balance_usd", "world.scarcity"]
    assert all(covered_by(d, ivs) for d in diff if d not in ("run.name", "experiment.arm"))


def test_scarcity_runs_estimate_the_scarcity_by_tier_interaction(tmp_path: Path) -> None:
    from void.sim.loop import Simulation

    out = tmp_path / "exp_scarcity"
    for arm in ("high", "low"):
        for seed in (2, 3):  # one 12-tick day; on these seeds both tiers make claims in both arms
            cfg = load_config(CONFIGS / f"exp_scarcity_{arm}.yaml", {"run": {"days": 1, "ticks_per_day": 12, "seed": seed}})
            sim = Simulation(cfg, out / arm / f"seed_{seed}")
            try:
                asyncio.run(sim.run())
            finally:
                sim.db.close()
    result = compare(out)
    assert result["factorial"] is None
    did = result["tier_did"]
    assert did is not None and did["tiers"] == ["scripted", "scripted_b"]
    row = next(r for r in did["rows"] if r["outcome"] == "false_claim_rate")
    assert all(row["per_tier"][t]["n"] == 2 for t in did["tiers"])
    (diff,) = row["differences"]
    assert diff["n"] == 2 and diff["did"] is not None and diff["ci95"][0] is not None


# --- scripts/compare.py --hypotheses-only --------------------------------------------------------------------------------
def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("compare_script", REPO / "scripts" / "compare.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hypotheses_only_exit_codes(main_exp: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _load_script()
    # main_exp: the one primary hypothesis passes, exploratory failures do not count
    assert script.main([str(main_exp), "--hypotheses-only"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("pre-registered hypotheses") and "  PASS  sup_pass" in out and "  FAIL  power_short" in out
    assert "primary:" not in out  # only the hypotheses
    failing = _planted(tmp_path / "exp_synth", {"base": None, "up": UP, "noisy": NOISY},
                       [_hyp("up", "up"), _hyp("noise", "noisy")])
    assert script.main([str(failing), "--hypotheses-only"]) == 1
    capsys.readouterr()
    assert script.main([str(failing), "--hypotheses-only", "--json"]) == 1
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["up", "noise"] and [r["passed"] for r in rows] == [True, False]
    assert script.main([str(failing)]) == 0  # without the flag the verdicts do not set the exit code
    assert script.main([str(tmp_path / "nowhere"), "--hypotheses-only"]) == 2


def test_hypotheses_only_as_a_command(main_exp: Path, tmp_path: Path) -> None:
    failing = _planted(tmp_path / "exp_synth", {"base": None, "noisy": NOISY}, [_hyp("noise", "noisy")])
    cmd = [sys.executable, str(REPO / "scripts" / "compare.py")]
    ok = subprocess.run([*cmd, str(main_exp), "--hypotheses-only"], capture_output=True, text=True, check=False)
    assert ok.returncode == 0, ok.stderr
    bad = subprocess.run([*cmd, str(failing), "--hypotheses-only"], capture_output=True, text=True, check=False)
    assert bad.returncode == 1 and "FAIL  noise" in bad.stdout
