"""Research harness (DESIGN §17): experiment configs, per-run analysis and the paired comparison.

Scripted runs only. exp_tiers is run for 3 days x 12 ticks with seeds {1, 2}: at 2 days the
low-stress bins hold too few calls for a stable per-seed collapse fit (see test docstring), so 3 days
is the documented minimum for the hidden order to be recovered on every seed.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import yaml

from void.config import load_config
from void.research.analysis import (
    SUMMARY_KEYS,
    collapse_curve,
    end_of_run_outcome,
    recovered_collapse_stress,
    summarize,
    windfall_lineage_penetration,
)
from void.research.compare import IncomparableRuns, compare, config_diff, covered_by, render
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"
TIERS_DAYS = 3


def exp_configs() -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = {}
    for p in sorted(CONFIGS.glob("exp_*.yaml")):
        groups.setdefault(str(load_config(p).experiment.name), []).append(p)
    return groups


def run_arm(config: Path, out: Path, seeds: tuple[int, ...], **overrides: object) -> Path:
    """Run ``config`` once per seed into ``out/<arm>/seed_<n>/``; returns the arm directory."""
    cfg0 = load_config(config, dict(overrides))
    arm_dir = out / str(cfg0.experiment.arm)
    for seed in seeds:
        cfg = load_config(config, {**dict(overrides), "run": {**dict(overrides.get("run", {}) or {}), "seed": seed}})  # type: ignore[arg-type]
        sim = Simulation(cfg, arm_dir / f"seed_{seed}")
        try:
            asyncio.run(sim.run())
        finally:
            sim.db.close()
    return arm_dir


# --- fixtures (module scoped: each simulation runs once) ---------------------------------------------------
@pytest.fixture(scope="module")
def tiers_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("exp_tiers")
    for arm in ("a", "b"):
        run_arm(CONFIGS / f"exp_tiers_{arm}.yaml", out, (1, 2), run={"days": TIERS_DAYS, "ticks_per_day": 12})
    return out


@pytest.fixture(scope="module")
def scarcity_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("exp_scarcity")
    for arm in ("low", "high"):
        run_arm(CONFIGS / f"exp_scarcity_{arm}.yaml", out, (1, 2), run={"days": 2, "ticks_per_day": 12})
    return out


@pytest.fixture(scope="module")
def benefactor_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("exp_benefactor")
    for arm in ("opaque", "legible"):
        run_arm(CONFIGS / f"exp_benefactor_{arm}.yaml", out, (1,), run={"days": 2, "ticks_per_day": 12})
    return out


@pytest.fixture(scope="module")
def smoke_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("smoke")
    cfg = load_config(CONFIGS / "scripted_smoke.yaml", {"run": {"days": 1, "ticks_per_day": 12}, "memory": {"probe_every_ticks": 6}})
    sim = Simulation(cfg, out / "run")
    try:
        asyncio.run(sim.run())
    finally:
        sim.db.close()
    return out / "run"


# --- (a) configs ---------------------------------------------------------------------------------------------
def test_every_experiment_config_loads_without_warnings() -> None:
    paths = sorted(CONFIGS.glob("exp_*.yaml"))
    assert len(paths) >= 17
    for p in paths:
        cfg = load_config(p)
        assert cfg.experiment.name and cfg.experiment.arm, p.name
        assert cfg.experiment.primary_outcome, p.name
        assert cfg.entropy.degeneration.mode == "observe", p.name
        assert all(cfg.tiers[a.tier].provider == "scripted" for a in cfg.agents), p.name
        assert cfg.warnings() == [], (p.name, cfg.warnings())


def test_arms_differ_only_in_declared_independent_variables() -> None:
    groups = exp_configs()
    assert set(groups) == {"exp_tiers", "exp_scarcity", "exp_memory", "exp_gate", "exp_spread", "exp_press", "exp_benefactor"}
    for name, paths in groups.items():
        assert len(paths) >= 2, name
        cfgs = [load_config(p) for p in paths]
        ivs = sorted({iv for c in cfgs for iv in c.experiment.independent_variables})
        for i in range(len(cfgs)):
            for j in range(i + 1, len(cfgs)):
                diff = config_diff(cfgs[i].model_dump(mode="json"), cfgs[j].model_dump(mode="json"))
                assert diff, (paths[i].name, paths[j].name, "arms are identical")
                stray = [d for d in diff if d not in ("run.name", "experiment.arm") and not covered_by(d, ivs)]
                assert not stray, (paths[i].name, paths[j].name, stray)
                assert any(covered_by(d, ivs) for d in diff), (paths[i].name, paths[j].name, "no IV differs")


def test_exp_tiers_hidden_beta_is_the_only_tier_difference() -> None:
    a, b = load_config(CONFIGS / "exp_tiers_a.yaml"), load_config(CONFIGS / "exp_tiers_b.yaml")
    assert a.tiers == b.tiers
    alpha, beta = a.tiers["alpha"].model_dump(), a.tiers["beta"].model_dump()
    assert {k for k in alpha if alpha[k] != beta[k]} == {"scripted_beta", "color"}
    assert alpha["scripted_beta"] > beta["scripted_beta"]
    assert {ag.tier for ag in a.agents} == {"alpha"} and {ag.tier for ag in b.agents} == {"beta"}
    assert a.economy.world_pricing == b.economy.world_pricing == "equalized"


# --- (b) the recovered collapse order ---------------------------------------------------------------------------
def test_compare_recovers_hidden_collapse_order(tiers_dir: Path) -> None:
    """Arm b (scripted_beta 0.6) must collapse at a lower stress bin than arm a (scripted_beta 2.0).

    With entropy_budget_max 30 the 12-tick day spans stress 0.0-1.0. 2 days x 2 seeds leaves the
    per-seed fit noisy (few calls per bin); at 3 days every seed recovers the order, so TIERS_DAYS = 3
    is the documented minimum for this assertion.
    """
    result = compare(tiers_dir)
    assert result["experiment"] == "exp_tiers" and result["arms"] == ["a", "b"] and result["seeds"] == [1, 2]
    assert result["primary"] == "recovered_collapse_stress"
    primary = result["comparisons"][0]["primary"]
    assert primary["n"] == 2 and primary["dropped"] == 0
    for seed in ("1", "2"):
        va, vb = primary["per_seed"][seed]
        assert va is not None and vb is not None
        assert vb < va, (seed, va, vb, collapse_curve(tiers_dir / "a" / f"seed_{seed}"), collapse_curve(tiers_dir / "b" / f"seed_{seed}"))
    assert primary["delta"] < 0 and primary["mean_b"] < primary["mean_a"]
    assert primary["sign_test_p"] is not None and primary["positive"] == 0 and primary["negative"] == 2
    lo, hi = primary["ci95"]
    assert lo is not None and hi is not None and lo <= primary["delta"] <= hi
    # the pooled fit equals the roster tier's fit for a single-tier roster
    assert recovered_collapse_stress(tiers_dir / "a" / "seed_1") == recovered_collapse_stress(tiers_dir / "a" / "seed_1", "alpha")
    assert end_of_run_outcome(tiers_dir / "b" / "seed_1", "recovered_collapse_stress.beta") == recovered_collapse_stress(tiers_dir / "b" / "seed_1")
    assert end_of_run_outcome(tiers_dir / "a" / "seed_1", "by_tier.alpha.calls") is not None
    assert end_of_run_outcome(tiers_dir / "a" / "seed_1", "by_tier.beta.calls") is None
    secondaries = result["comparisons"][0]["secondary"]
    assert [s["path"] for s in secondaries] == ["action_regret_mean", "mean_stress"]
    assert all(s["exploratory"] is True for s in secondaries)
    text = render(result)
    assert "recovered_collapse_stress" in text and "a vs b" in text and "exploratory" in text


# --- (c) refusals --------------------------------------------------------------------------------------------
def _copy_experiment(src: Path, dst: Path, edit: dict[str, dict[str, object]]) -> Path:
    """Copy an experiment directory and rewrite config.resolved.yaml of the named arms with ``edit[arm]``."""
    shutil.copytree(src, dst)
    for arm, patch in edit.items():
        for run_dir in sorted((dst / arm).glob("seed_*")):
            path = run_dir / "config.resolved.yaml"
            data = yaml.safe_load(path.read_text())
            for dotted, value in patch.items():
                cur = data
                keys = dotted.split(".")
                for k in keys[:-1]:
                    cur = cur[k]
                cur[keys[-1]] = value
            path.write_text(yaml.safe_dump(data, sort_keys=True))
    return dst


def test_compare_refuses_differences_outside_independent_variables(tiers_dir: Path, tmp_path: Path) -> None:
    bad = _copy_experiment(tiers_dir, tmp_path / "exp", {"b": {"world.scarcity": 0.7}})
    with pytest.raises(IncomparableRuns, match=r"world\.scarcity"):
        compare(bad)


def test_compare_refuses_non_observe_degeneration(tiers_dir: Path, tmp_path: Path) -> None:
    bad = _copy_experiment(tiers_dir, tmp_path / "exp", {"a": {"entropy.degeneration.mode": "both"}, "b": {"entropy.degeneration.mode": "both"}})
    with pytest.raises(IncomparableRuns, match="observe"):
        compare(bad)


def test_compare_refuses_seed_set_mismatch_and_real_pricing(tiers_dir: Path, tmp_path: Path) -> None:
    exp = _copy_experiment(tiers_dir, tmp_path / "exp", {})
    shutil.rmtree(exp / "b" / "seed_2")
    with pytest.raises(IncomparableRuns, match="seed sets differ"):
        compare(exp)
    real = _copy_experiment(tiers_dir, tmp_path / "real", {"a": {"economy.world_pricing": "real"}, "b": {"economy.world_pricing": "real"}})
    with pytest.raises(IncomparableRuns, match="equalized"):
        compare(real)


# --- (d) scarcity -> false claims ------------------------------------------------------------------------------
def test_scarcity_arms_report_false_claim_delta(scarcity_dir: Path) -> None:
    result = compare(scarcity_dir)
    assert result["primary"] == "false_claim_rate" and result["arms"] == ["high", "low"]
    primary = result["comparisons"][0]["primary"]
    high = [primary["per_seed"][s][0] for s in ("1", "2")]
    assert all(v is not None for v in high)
    assert max(high) > 0.0, ("no false claim in the high-scarcity arm", primary)
    assert primary["n"] == 2 and primary["delta"] is not None
    assert primary["ci95"][0] is not None
    secondary = {s["path"]: s for s in result["comparisons"][0]["secondary"]}
    assert secondary["invalid_action_rate"]["exploratory"] is True
    assert secondary["invalid_action_rate"]["n"] == 2


# --- (e) benefactor: common random numbers + lineage ----------------------------------------------------------------
def _grant_ticks(run_dir: Path) -> list[tuple[int, str, float]]:
    import json
    import sqlite3

    db = sqlite3.connect(str(run_dir / "world.db"))
    try:
        rows = db.execute("SELECT e.tick, a.name, e.payload FROM events e JOIN agents a ON a.agent_id=e.agent_id "
                          "WHERE e.kind='benefactor_grant' ORDER BY e.tick").fetchall()
    finally:
        db.close()
    return [(int(t), str(n), float(json.loads(p)["amount_usd"])) for t, n, p in rows]


def test_benefactor_arms_share_grants_and_lineage_is_computable(benefactor_dir: Path) -> None:
    opaque, legible = benefactor_dir / "opaque" / "seed_1", benefactor_dir / "legible" / "seed_1"
    g_opaque, g_legible = _grant_ticks(opaque), _grant_ticks(legible)
    assert g_opaque, "no grants in two days at mean_interval_ticks 6"
    assert g_opaque == g_legible
    assert all(1 <= t <= 24 for t, _, _ in g_opaque)
    pen = windfall_lineage_penetration(opaque)
    assert pen is not None and 0.0 <= pen <= 1.0
    assert end_of_run_outcome(opaque, "windfall_lineage_penetration") == pen
    result = compare(benefactor_dir)
    assert result["primary"] == "windfall_lineage_penetration"
    assert result["comparisons"][0]["primary"]["n"] == 1
    summary = summarize(opaque)
    assert summary["benefactor"]["grants"] == len(g_opaque)
    assert summary["benefactor"]["grant_ticks"] == [t for t, _, _ in g_opaque]
    assert isinstance(summary["benefactor"]["exogenous_vocab"]["words"], list)


# --- (f) summarize ----------------------------------------------------------------------------------------------
def test_summarize_returns_documented_keys(smoke_run: Path) -> None:
    s = summarize(smoke_run)
    for key in SUMMARY_KEYS:
        assert key in s, key
    assert s["status"] == "completed" and s["days"] == 1 and s["ticks_per_day"] == 12
    assert s["population"] >= 1 and s["calls"] > 0
    assert s["probe_recall"] is not None and 0.0 <= s["probe_recall"] <= 1.0
    assert isinstance(s["notes_by_channel"], dict) and isinstance(s["gadgets"], dict)
    assert s["degeneration_by_tier"] == {}  # scripted_smoke uses mode `both`, but a 1-day run never crosses T_c
    assert end_of_run_outcome(smoke_run, "mean_balance") == pytest.approx(s["mean_balance"], abs=1e-3)
    assert end_of_run_outcome(smoke_run, "tick.population") == s["population"]
    assert end_of_run_outcome(smoke_run, "day.population_end") == s["population"]
    assert end_of_run_outcome(smoke_run, "no.such.path") is None
