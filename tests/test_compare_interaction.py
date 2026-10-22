"""Pre-registered interaction hypotheses (``kind: interaction``; MEMORY_EVOLUTION H4, 6.6; DESIGN sections 4 and 17).

Covered: the config rules of both layouts (four arms, a between-arm 2x2; arm x within-run levels) and their errors;
the serialized form of a hypothesis that does not use the new fields (unchanged, so stored configs hash as before);
:func:`void.research.stats.contrast_test` in each direction; :func:`void.research.compare.evaluate_interaction` (Holm
across a hypothesis's own contrasts, ``two_sided``, margins, ``min_power``); the per-rung lineage fitness
``tier_lineage_fitness.<tier>`` on a hand-built world; ``compare`` on synthetic experiment directories with planted
per-seed values (a known interaction passes, none fails, in both layouts; a primary interaction joins the Holm family
once; incomplete runs are dropped); H4 as declared in ``configs/live_mem_e4_base.yaml`` and inherited by the live arms
and the scripted mirrors; and ``void compare`` end to end on tiny scripted runs of each layout (the E4 mirror's H4 over
the five rungs, and a four-arm interaction over the ``exp_press`` arms).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import random
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from void.cli import main as cli_main
from void.config import HypothesisConfig, VoidConfig, load_config
from void.db import Database
from void.research import stats as st
from void.research.analysis import DAILY, end_of_run_outcome
from void.research.compare import compare, evaluate_interaction, render, render_hypotheses
from void.research.outcomes import OUTCOMES, load_all
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"
SEEDS = tuple(range(1, 9))
RUNGS = ["genius", "sharp", "average", "slow", "dim"]

# planted per-seed contrast values, one per seed in SEEDS (the same shapes test_compare_hypotheses.py uses)
UP = [0.9, 1.1, 1.0, 1.2, 0.8, 1.05, 0.95, 1.0]           # mean 1.0, d_z about 8: clear
HOLM = [-0.1, 1.1, 0.5, 0.1, 1.2, 0.1, 0.9, 0.2]          # mean 0.5: p about 0.026, fails once multiplied by 2 or 3
NOISY = [0.9, -1.1, 0.4, -0.6, 1.2, -0.9, 0.3, 0.6]       # mean 0.1, sd 0.86: nothing to detect
FLAT = [0.04, -0.03, 0.02, -0.05, 0.01, 0.03, -0.02, -0.01]  # mean about 0


def _neg(xs: Sequence[float]) -> list[float]:
    return [-x for x in xs]


# --- config ----------------------------------------------------------------------------------------------------
def _four(**kw: Any) -> dict[str, Any]:
    return {"name": "i4", "kind": "interaction", "arm_a": "none", "arm_b": "gossip", "arm_c": "chronicle",
            "arm_d": "both", "outcome": "mean_balance", **kw}


def _levels(**kw: Any) -> dict[str, Any]:
    return {"name": "il", "kind": "interaction", "arm_a": "default", "arm_b": "selfedit",
            "outcome_template": "tier_lineage_fitness.{level}", "levels": ["hi", "mid", "lo"], **kw}


def test_both_layouts_validate() -> None:
    four = HypothesisConfig.model_validate(_four(direction="two_sided"))
    assert (four.layout, four.reference_level, four.level_outcomes(), four.outcome_paths()) == (
        "four_arms", None, {}, ["mean_balance"])
    by_level = HypothesisConfig.model_validate(_levels())
    assert by_level.layout == "levels" and by_level.reference_level == "hi"  # the first level unless set
    assert by_level.level_outcomes() == {"hi": "tier_lineage_fitness.hi", "mid": "tier_lineage_fitness.mid",
                                         "lo": "tier_lineage_fitness.lo"}
    assert by_level.outcome_paths() == list(by_level.level_outcomes().values()) and by_level.outcome is None
    assert HypothesisConfig.model_validate(_levels(reference="mid")).reference_level == "mid"
    assert HypothesisConfig.model_validate(_levels(levels=["a", "b"])).levels == ["a", "b"]  # two levels suffice
    superiority = HypothesisConfig(name="s", arm_a="a", arm_b="b", outcome="mean_balance")
    assert superiority.layout is None and superiority.outcome_paths() == ["mean_balance"]
    for direction in ("greater", "less", "two_sided"):
        assert HypothesisConfig.model_validate(_levels(direction=direction, margin=0.1, margin_unit="sd",
                                                       primary=False, min_power=0.8)).direction == direction


@pytest.mark.parametrize("bad, message", [
    (_four(arm_d=None), "needs both arm_c and arm_d"),
    (_four(arm_c=None), "needs both arm_c and arm_d"),
    (_four(arm_d="none"), "four different arms"),
    (_four(arm_c="gossip"), "four different arms"),
    (_four(outcome=None), "four-arm interaction needs outcome"),
    (_four(levels=["a", "b"]), "not both"),
    (_four(outcome_template="x.{level}"), "not both"),
    (_four(reference="a"), "not both"),
    ({**_four(), "arm_c": None, "arm_d": None}, "needs arm_c and arm_d .* or outcome_template and levels"),
    (_levels(outcome="mean_balance"), "reads outcome_template"),
    (_levels(outcome_template=None), "needs outcome_template holding"),
    (_levels(outcome_template="tier_lineage_fitness.genius"), "needs outcome_template holding"),
    (_levels(outcome_template="{level}"), "only placeholder"),
    (_levels(outcome_template="by_tier.{level}.{other}"), "only placeholder"),
    (_levels(levels=None), "at least two levels"),
    (_levels(levels=["hi"]), "at least two levels"),
    (_levels(levels=["hi", "hi"]), "levels must be unique"),
    (_levels(levels=["hi", "a.b"]), "without dots or braces"),
    (_levels(levels=["hi", " "]), "without dots or braces"),
    (_levels(reference="nowhere"), "is not one of the levels"),
    (_levels(arm_b="default"), "arm_a and arm_b must differ"),
    (_levels(margin=-0.1), "margin must be >= 0"),
    (_levels(direction="sideways"), "direction"),
    ({"name": "s", "arm_a": "a", "arm_b": "b", "outcome": "mean_balance", "direction": "two_sided"},
     "two_sided only applies to kind: interaction"),
    ({"name": "e", "arm_a": "a", "arm_b": "b", "outcome": "mean_balance", "kind": "equivalence", "margin": 0.1,
      "direction": "two_sided"}, "two_sided only applies to kind: interaction"),
    ({"name": "s", "arm_a": "a", "arm_b": "b", "outcome": "mean_balance", "arm_c": "c", "arm_d": "d"},
     "arm_c, arm_d only apply to kind: interaction"),
    ({"name": "s", "arm_a": "a", "arm_b": "b", "outcome": "mean_balance", "levels": ["x", "y"]},
     "levels only apply to kind: interaction"),
    ({"name": "s", "arm_a": "a", "arm_b": "b", "kind": "non_superiority"}, "outcome is required"),
    ({"name": "s", "arm_a": "a", "arm_b": "b", "outcome": "by_tier.{level}.calls"}, "outcome holds {level}"),
])
def test_interaction_validation_errors(bad: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        HypothesisConfig.model_validate(bad)


def test_a_hypothesis_without_the_new_fields_serializes_as_before() -> None:
    """The interaction fields are left out of the dump while unset, so every stored config that declares only the
    older kinds keeps its exact form and hash; an interaction hypothesis round-trips through the stored YAML."""
    old = HypothesisConfig(name="s", arm_a="a", arm_b="b", outcome="mean_balance")
    assert list(old.model_dump(mode="json")) == ["name", "arm_a", "arm_b", "outcome", "kind", "direction", "margin",
                                                  "margin_unit", "primary", "min_power"]
    four = HypothesisConfig.model_validate(_four())
    assert "levels" not in four.model_dump() and four.model_dump()["arm_d"] == "both"
    for hyps in ([_four()], [_levels(reference="mid")]):
        cfg = load_config(CONFIGS / "base.yaml", {"experiment": {"hypotheses": hyps}})
        stored = yaml.safe_load(cfg.to_yaml())
        assert VoidConfig.model_validate(stored).hash() == cfg.hash()
        assert VoidConfig.model_validate(stored).experiment.hypotheses == cfg.experiment.hypotheses


# --- stats.contrast_test ------------------------------------------------------------------------------------------
def test_contrast_test_directions() -> None:
    up = st.contrast_test(UP, 0.0, "two_sided")
    assert up["side"] == "greater" and up["passed"] and up["ci_clears"]
    assert up["p"] == pytest.approx(st.paired_t(UP)["p_two_sided"], rel=1e-12)  # margin 0: the two-sided t p
    assert up == {**st.superiority(UP, 0.0, "greater"), "side": "greater"}
    down = st.contrast_test(_neg(UP), 0.0, "two_sided")
    assert down["side"] == "less" and down["passed"] and down["p"] == pytest.approx(up["p"], rel=1e-12)
    assert down["ci"] == pytest.approx([-up["ci"][1], -up["ci"][0]])
    assert not st.contrast_test(_neg(UP), 0.0, "greater")["passed"]
    assert st.contrast_test(_neg(UP), 0.0, "less")["passed"]
    assert not st.contrast_test(NOISY, 0.0, "two_sided")["passed"]
    # a margin on either side: |mean| about 1 clears 0.5, not 1.5
    assert st.contrast_test(_neg(UP), 0.5, "two_sided")["passed"]
    assert not st.contrast_test(_neg(UP), 1.5, "two_sided")["passed"]
    assert st.contrast_test([], 0.0, "two_sided")["side"] is None
    assert st.contrast_test([1.0], 0.0, "two_sided")["passed"] is None
    with pytest.raises(ValueError, match="direction"):
        st.contrast_test(UP, 0.0, "up")


# --- evaluate_interaction -------------------------------------------------------------------------------------------
def _h(**kw: Any) -> dict[str, Any]:
    return {"name": "h", "kind": "interaction", "direction": "two_sided", "margin": 0.0, "margin_unit": "abs",
            "primary": True, "min_power": None, **kw}


def test_holm_within_a_multi_contrast_hypothesis() -> None:
    alone = evaluate_interaction(_h(), {"x": HOLM})
    (c,) = alone["contrasts"]
    assert c["p"] < 0.05 and c["p_holm"] == c["p"] and c["clears"] and alone["holds"]
    assert alone["k"] == 1 and alone["p"] == c["p"] and alone["deciding"] == "x"
    # the same contrast among two null ones: its p is multiplied by 3 and nothing clears
    three = evaluate_interaction(_h(), {"x": HOLM, "y": NOISY, "z": FLAT})
    by = {r["label"]: r for r in three["contrasts"]}
    assert three["k"] == 3 and by["x"]["p"] == pytest.approx(c["p"])
    assert by["x"]["p_holm"] == pytest.approx(min(1.0, 3 * c["p"])) and by["x"]["ci_clears"] and not by["x"]["clears"]
    assert three["holds"] is False and not any(r["clears"] for r in three["contrasts"])
    # the hypothesis's own p, the one that enters the family, is min(1, k * smallest contrast p)
    assert three["p"] == pytest.approx(min(1.0, 3 * min(r["p"] for r in three["contrasts"])))
    assert three["p"] == min(r["p_holm"] for r in three["contrasts"])
    # a strong contrast survives the multiplication and decides the row
    strong = evaluate_interaction(_h(), {"y": NOISY, "x": UP, "z": FLAT})
    assert strong["holds"] and strong["deciding"] == "x" and strong["delta"] == pytest.approx(1.0)
    assert [r["clears"] for r in strong["contrasts"]] == [False, True, False]


def test_two_sided_greater_and_less_decisions() -> None:
    values = {"x": _neg(UP), "y": FLAT}
    two = evaluate_interaction(_h(), values)
    assert two["holds"] and two["deciding"] == "x" and two["contrasts"][0]["side"] == "less"
    assert two["ci"][1] < 0
    assert not evaluate_interaction(_h(direction="greater"), values)["holds"]
    assert evaluate_interaction(_h(direction="less"), values)["holds"]
    assert evaluate_interaction(_h(direction="greater"), {"x": UP, "y": FLAT})["holds"]
    assert evaluate_interaction(_h(margin=0.5), values)["holds"]
    assert not evaluate_interaction(_h(margin=1.5), values)["holds"]
    sd = evaluate_interaction(_h(margin=2.0, margin_unit="sd"), values)  # d_z about -8 clears 2 SD
    assert sd["holds"] and sd["margin_abs"] == pytest.approx(2.0 * sd["sd"])


def test_min_power_and_untestable_contrasts() -> None:
    mid = [0.0, 1.0, 0.4, 0.1, 1.2, 0.3, 0.8, 0.2]  # p about 0.015, power about 0.89
    assert evaluate_interaction(_h(min_power=0.8), {"x": mid})["holds"]
    short = evaluate_interaction(_h(min_power=0.95), {"x": mid})
    assert not short["holds"] and short["contrasts"][0]["ci_clears"]
    # a contrast with fewer than two values is not tested and does not count towards k
    some = evaluate_interaction(_h(), {"x": UP, "y": [0.3]})
    assert some["k"] == 1 and some["contrasts"][1]["p"] is None and some["contrasts"][1]["clears"] is None
    none = evaluate_interaction(_h(), {"x": [1.0], "y": []})
    assert none["holds"] is None and none["k"] == 0 and none["p"] is None


# --- the per-rung outcome: tier_lineage_fitness ----------------------------------------------------------------
# (agent_id, name, tier, parent_id, born_tick, died_tick); 4 ticks a day, last tick 8
TIER_AGENTS = (
    ("g1", "Ga", "genius", None, 0, None),     # 2 days, with a child born at 4 (1 day): lineage 3
    ("g2", "Ga-1", "genius", "g1", 4, None),
    ("d1", "Da", "dim", None, 0, 4),           # dies at 4: 1 day
    ("d2", "Db", "dim", None, 0, None),        # 2 days, with a child of another tier born at 6 (0.5): lineage 2.5
    ("d3", "Db-1", "sharp", "d2", 6, None),
    ("s1", "Sa", "sharp", None, 2, None),      # a parentless arrival is its own founder: 1.5 days
)


@pytest.fixture
def tier_world(tmp_path: Path) -> Path:
    cfg = load_config(CONFIGS / "base.yaml", {"run": {"days": 2, "ticks_per_day": 4}})
    (tmp_path / "config.resolved.yaml").write_text(cfg.to_yaml())
    db = Database(tmp_path / "world.db")
    try:
        for aid, name, tier, parent, born, died in TIER_AGENTS:
            db.execute("INSERT INTO agents(agent_id, name, parent_id, model_tier, balance, personality_seed, memory_path, "
                       "generation, status, x, y, entropy_budget, born_tick, died_tick, created_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, name, parent, tier, 0, "{}", "", 0 if parent is None else 1,
                        "alive" if died is None else "archived", 0, 0, 1, born, died, f"tick:{born}"))
        db.kv_set("tick", 8)
    finally:
        db.close()
    return tmp_path


def test_tier_lineage_fitness_on_a_hand_built_world(tier_world: Path) -> None:
    load_all()
    assert "tier_lineage_fitness" in OUTCOMES and "tier_lineage_fitness" in DAILY
    assert end_of_run_outcome(tier_world, "tier_lineage_fitness.genius") == pytest.approx(3.0, abs=1e-12)
    # each founder's lineage whole, whatever its members' own tiers: Db-1 counts for dim, not sharp
    assert end_of_run_outcome(tier_world, "tier_lineage_fitness.dim") == pytest.approx((1.0 + 2.5) / 2, abs=1e-12)
    assert end_of_run_outcome(tier_world, "tier_lineage_fitness.sharp") == pytest.approx(1.5, abs=1e-12)
    assert end_of_run_outcome(tier_world, "tier_lineage_fitness.average") is None  # no founder on that rung
    # the founders of every rung together are lineage_fitness itself
    assert end_of_run_outcome(tier_world, "lineage_fitness") == pytest.approx((3.0 + 1.0 + 2.5 + 1.5) / 4, abs=1e-12)
    # the end of day 1 (tick 4): Ga 1 (its child born that tick, 0), Da 1 and Db 1, Sa 0.5
    assert DAILY["tier_lineage_fitness"](tier_world, "genius", 1) == pytest.approx(1.0, abs=1e-12)
    assert DAILY["tier_lineage_fitness"](tier_world, "dim", 1) == pytest.approx(1.0, abs=1e-12)
    assert DAILY["tier_lineage_fitness"](tier_world, "sharp", 1) == pytest.approx(0.5, abs=1e-12)
    for bad in ("tier_lineage_fitness", "tier_lineage_fitness.dim.x"):
        with pytest.raises(ValueError, match="one tier name"):
            end_of_run_outcome(tier_world, bad)


# --- synthetic experiment directories ------------------------------------------------------------------------------
_CFGS: dict[str, VoidConfig] = {}


def _cfg(exp: str, arm: str, seed: int, ivs: list[str], sets: dict[str, Any], hypotheses: list[dict[str, Any]]) -> VoidConfig:
    over: dict[str, Any] = {
        "run": {"name": f"{exp}_{arm}", "days": 1, "ticks_per_day": 2},
        "entropy": {"degeneration": {"mode": "observe"}},
        "experiment": {"name": exp, "arm": arm, "independent_variables": ivs, "primary_outcome": "mean_balance",
                       "hypotheses": hypotheses},
    }
    for path, value in sets.items():
        head, key = path.split(".")
        over.setdefault(head, {})[key] = value
    k = json.dumps(over, sort_keys=True)
    if k not in _CFGS:
        _CFGS[k] = load_config(CONFIGS / "base.yaml", over)
    cfg = _CFGS[k].model_copy(deep=True)
    cfg.run.seed = seed
    return cfg


def _write(run_dir: Path, cfg: VoidConfig, row: dict[str, Any], status: str = "completed") -> None:
    """A run with the resolved config, a run row of ``status`` and tick rows up to the last tick carrying ``row``."""
    run_dir.mkdir(parents=True)
    text = cfg.to_yaml()
    (run_dir / "config.resolved.yaml").write_text(text)
    last = cfg.run.days * cfg.run.ticks_per_day
    db = Database(run_dir / "world.db")
    try:
        db.execute("INSERT INTO run(run_id, config_hash, config_yaml, seed, created_at, schema_version, status, ended_tick) "
                   "VALUES(?,?,?,?,?,?,?,?)", (f"r{cfg.run.seed}", "synthetic", text, cfg.run.seed,
                                               "2026-01-01T00:00:00", 2, status, last))
    finally:
        db.close()
    rows = [{"tick": t, "day": 1, **row} for t in range(1, last + 1)]
    (run_dir / "metrics.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def _base(seed: int) -> float:
    return 2.0 + 0.1 * seed + 0.05 * math.sin(seed)


LEVELS = ["hi", "mid", "lo"]


def _by_level_exp(root: Path, planted: dict[str, list[float]], hypotheses: list[dict[str, Any]], *,
                  main_effect: float = 0.0, failed: tuple[str, int] | None = None) -> Path:
    """Arms ``base`` and ``treat`` (they differ in ``world.scarcity``, declared). Each run's tick rows carry
    ``by_tier.<level>.mean_balance`` for the three levels; ``treat`` adds ``main_effect`` at every level plus
    ``planted[level][seed - 1]``, so the contrast at a level against ``mid`` is exactly the planted value there minus
    the planted value at ``mid``. ``failed`` marks one run as failed."""
    for arm, scarcity in (("base", 1.0), ("treat", 0.9)):
        for seed in SEEDS:
            cfg = _cfg("exp_lvl", arm, seed, ["world.scarcity"], {"world.scarcity": scarcity}, hypotheses)
            by_tier: dict[str, Any] = {}
            for i, lv in enumerate(LEVELS):
                y = _base(seed) + 0.3 * i
                if arm == "treat":
                    y += main_effect + planted.get(lv, [0.0] * len(SEEDS))[seed - 1]
                by_tier[lv] = {"mean_balance": y}
            status = "failed" if failed == (arm, seed) else "completed"
            _write(root / arm / f"seed_{seed}", cfg, {"mean_balance": _base(seed), "by_tier": by_tier}, status)
    return root


def _lvl_hyp(**kw: Any) -> dict[str, Any]:
    return {"name": "rung_x_arm", "kind": "interaction", "arm_a": "base", "arm_b": "treat",
            "outcome_template": "by_tier.{level}.mean_balance", "levels": LEVELS, "reference": "mid",
            "direction": "two_sided", **kw}


def test_by_level_a_planted_interaction_passes_and_a_main_effect_alone_fails(tmp_path: Path) -> None:
    known = compare(_by_level_exp(tmp_path / "a" / "exp_lvl", {"hi": UP, "lo": FLAT}, [_lvl_hyp()]))
    (h,) = known["hypotheses"]
    assert h["kind"] == "interaction" and h["outcome"] == "by_tier.{level}.mean_balance" and h["reference"] == "mid"
    assert h["passed"] and h["k"] == 2 and h["deciding"] == "hi" and h["delta"] == pytest.approx(1.0)
    hi, lo = h["contrasts"]
    assert (hi["label"], hi["vs"], lo["label"]) == ("hi", "mid", "lo")
    assert [hi["per_seed"][str(s)] for s in SEEDS] == pytest.approx(UP)
    assert [lo["per_seed"][str(s)] for s in SEEDS] == pytest.approx(FLAT)
    assert hi["clears"] and not lo["clears"] and hi["n"] == len(SEEDS) and hi["dropped"] == 0
    assert h["p"] == pytest.approx(min(1.0, 2 * min(hi["p"], lo["p"]))) and h["p_holm"] == h["p"]  # one primary
    text = render_hypotheses(known)
    assert "  PASS  rung_x_arm [interaction two_sided, margin 0, primary] (treat - base) at each level - at mid" in text
    assert "      PASS  contrast hi - mid: n=8 (dropped 0)" in text and "      FAIL  contrast lo - mid: n=8" in text
    # a large main effect of the arm with no interaction: every contrast is about 0, so H fails
    none = compare(_by_level_exp(tmp_path / "b" / "exp_lvl", {"hi": FLAT, "lo": _neg(FLAT)}, [_lvl_hyp()],
                                 main_effect=2.0))
    (h0,) = none["hypotheses"]
    assert not h0["passed"] and h0["k"] == 2 and not any(c["clears"] for c in h0["contrasts"])
    assert none["comparisons"][0]["primary"]["delta"] == pytest.approx(0.0)
    assert "FAIL  rung_x_arm" in render(none) and "no contrast clears" in h0["reason"]
    # the reference defaults to the first level: contrasts mid - hi and lo - hi
    first = compare(_by_level_exp(tmp_path / "c" / "exp_lvl", {"hi": UP}, [_lvl_hyp(reference=None)]))
    (hf,) = first["hypotheses"]
    assert hf["reference"] == "hi" and [c["label"] for c in hf["contrasts"]] == ["mid", "lo"]
    assert hf["passed"] and hf["contrasts"][0]["estimate"] == pytest.approx(-1.0)


def test_by_level_holm_within_and_two_sided_through_compare(tmp_path: Path) -> None:
    # HOLM at one level of three: p about 0.026 doubled across the two contrasts fails; with two levels (k = 1) it passes
    three = compare(_by_level_exp(tmp_path / "a" / "exp_lvl", {"hi": HOLM, "lo": NOISY}, [_lvl_hyp()]))
    (h3,) = three["hypotheses"]
    hi = h3["contrasts"][0]
    assert hi["p"] < 0.05 <= hi["p_holm"] and hi["ci_clears"] and not h3["passed"]
    two = compare(_by_level_exp(tmp_path / "b" / "exp_lvl", {"hi": HOLM},
                                [_lvl_hyp(levels=["hi", "mid"], reference="mid")]))
    (h2,) = two["hypotheses"]
    assert h2["k"] == 1 and h2["passed"] and h2["p"] == pytest.approx(hi["p"])
    # a negative interaction: two_sided and less pass, greater fails
    hyps = [_lvl_hyp(name="two"), _lvl_hyp(name="less", direction="less", primary=False),
            _lvl_hyp(name="greater", direction="greater", primary=False)]
    rows = {h["name"]: h for h in compare(_by_level_exp(tmp_path / "c" / "exp_lvl", {"lo": _neg(UP)}, hyps))["hypotheses"]}
    assert rows["two"]["passed"] and rows["less"]["passed"] and not rows["greater"]["passed"]
    assert rows["two"]["deciding"] == "lo" and rows["two"]["contrasts"][1]["side"] == "less"
    assert rows["less"]["p_holm"] is None and rows["greater"]["p_holm"] is None  # exploratory


def test_a_primary_interaction_joins_the_holm_family_once(tmp_path: Path) -> None:
    """Exploratory, the HOLM contrast passes; as one of two primaries with the same p it fails, and its p_holm is
    Holm's adjustment of the family of the two hypotheses' own p values."""
    sup = {"name": "sup", "arm_a": "base", "arm_b": "treat", "outcome": "by_tier.hi.mean_balance"}  # diffs = HOLM
    inter = _lvl_hyp(name="inter", levels=["hi", "mid"], reference="mid")
    alone = compare(_by_level_exp(tmp_path / "a" / "exp_lvl", {"hi": HOLM}, [{**inter, "primary": False}]))
    assert alone["hypotheses"][0]["passed"]
    both = compare(_by_level_exp(tmp_path / "b" / "exp_lvl", {"hi": HOLM}, [sup, inter]))
    rows = {h["name"]: h for h in both["hypotheses"]}
    assert rows["sup"]["p"] == pytest.approx(rows["inter"]["p"])  # the same per-seed values, the same t-test
    family = st.holm({"sup": rows["sup"]["p"], "inter": rows["inter"]["p"]})
    assert rows["inter"]["p_holm"] == pytest.approx(family["inter"]) and rows["inter"]["p_holm"] >= 0.05
    assert not rows["inter"]["passed"] and not rows["sup"]["passed"]
    assert rows["inter"]["contrasts"][0]["clears"]  # it clears within the hypothesis; the family adjustment fails it
    assert "in the Holm family of primary hypotheses" in rows["inter"]["reason"]


def test_by_level_drops_incomplete_runs(tmp_path: Path) -> None:
    exp = _by_level_exp(tmp_path / "exp_lvl", {"hi": UP}, [_lvl_hyp()], failed=("treat", 3))
    result = compare(exp, allow_incomplete=True)
    (h,) = result["hypotheses"]
    assert h["n_seeds"] == len(SEEDS) - 1 and all(c["n"] == len(SEEDS) - 1 for c in h["contrasts"])
    assert "3" not in h["contrasts"][0]["per_seed"] and h["passed"]


# four arms crossed: chronicle x gossip, as exp_press
PRESS = {"none": (False, False), "gossip": (False, True), "chronicle": (True, False), "both": (True, True)}


def _four_exp(root: Path, inter: float, hypotheses: list[dict[str, Any]]) -> Path:
    rng = random.Random(7)
    for arm, (a, b) in PRESS.items():
        for seed in SEEDS:
            cfg = _cfg("exp_four", arm, seed, ["chronicle.enabled", "gossip.enabled"],
                       {"chronicle.enabled": a, "gossip.enabled": b}, hypotheses)
            y = _base(seed) + 0.4 * a + 0.2 * b + inter * a * b + rng.gauss(0.0, 0.05)
            _write(root / arm / f"seed_{seed}", cfg, {"mean_balance": y})
    return root


def test_four_arms_a_planted_interaction_passes_and_none_fails(tmp_path: Path) -> None:
    hyp = {**_four(name="press_x"), "direction": "greater"}
    known = compare(_four_exp(tmp_path / "a" / "exp_four", 0.6, [hyp]))
    (h,) = known["hypotheses"]
    assert h["passed"] and h["k"] == 1 and h["deciding"] == "interaction" and h["delta"] == pytest.approx(0.6, abs=0.1)
    # the same contrast as the descriptive 2x2 block's interaction, which is still reported (no verdict)
    f = known["factorial"]
    assert f is not None and h["delta"] == pytest.approx(f["interaction"]["mean"], abs=1e-12)
    (c,) = h["contrasts"]
    assert len(c["per_seed"]) == len(SEEDS) and c["vs"] is None
    text = render(known)
    assert "  PASS  press_x [interaction greater, margin 0, primary] (both - chronicle) - (gossip - none) on mean_balance: n=8" in text
    assert "factorial 2x2 on mean_balance (descriptive, no verdict" in text and "contrast interaction" not in text
    none = compare(_four_exp(tmp_path / "b" / "exp_four", 0.0, [{**hyp, "direction": "two_sided"}]))
    assert not none["hypotheses"][0]["passed"]
    neg = compare(_four_exp(tmp_path / "c" / "exp_four", -0.6, [{**hyp, "direction": "two_sided"}]))
    assert neg["hypotheses"][0]["passed"] and neg["hypotheses"][0]["ci"][1] < 0


def test_an_interaction_naming_an_arm_without_runs_fails(tmp_path: Path) -> None:
    result = compare(_four_exp(tmp_path / "exp_four", 0.6, [_four(name="ghost", arm_d="ghost")]))
    (h,) = result["hypotheses"]
    assert not h["passed"] and h["missing_arms"] == ["ghost"] and "no runs" in h["reason"]


# --- H4 as declared ---------------------------------------------------------------------------------------------
E4_CONFIGS = sorted([*CONFIGS.glob("live_mem_e4_*.yaml"), *CONFIGS.glob("exp_mem_e4s_*.yaml")])


def test_h4_is_declared_in_the_e4_base_and_inherited_by_every_e4_config() -> None:
    raw = yaml.safe_load((CONFIGS / "live_mem_e4_base.yaml").read_text())
    (declared,) = raw["experiment"]["hypotheses"]
    assert declared["name"] == "H4"
    assert len(E4_CONFIGS) == 8
    for path in E4_CONFIGS:
        cfg = load_config(path)
        (h4,) = cfg.experiment.hypotheses
        assert (h4.name, h4.kind, h4.layout, h4.arm_a, h4.arm_b) == ("H4", "interaction", "levels", "default", "selfedit")
        assert (h4.direction, h4.margin, h4.margin_unit, h4.primary, h4.min_power) == ("two_sided", 0.0, "abs", True, None)
        assert h4.outcome_template == "tier_lineage_fitness.{level}" and h4.reference_level == "average"
        # the ladder's rungs, every one of them rostered (1/2/4/2/1), in the ladder's order
        assert h4.levels == list(cfg.intelligence.ladder) == RUNGS
        assert {a.tier for a in cfg.agents} == set(RUNGS)
        if cfg.experiment.arm is not None:
            assert cfg.experiment.arm in ("default", "selfedit", "flat_tariff")
        if path.name.startswith("exp_mem_e4s"):
            assert all(cfg.tiers[t].provider == "scripted" for t in RUNGS)


# --- end to end on tiny scripted runs -------------------------------------------------------------------------------
E2E_SEEDS = (1, 2, 3)


def _run(cfg: VoidConfig, run_dir: Path) -> None:
    sim = Simulation(cfg, run_dir)
    try:
        assert asyncio.run(sim.run()) == "completed"
    finally:
        sim.db.close()


@pytest.fixture(scope="module")
def e4s_exp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The E4 mirror's default and selfedit arms, three seeds of one six-tick day each."""
    root = tmp_path_factory.mktemp("e4s") / "exp_mem_e4s"
    for arm in ("default", "selfedit"):
        for seed in E2E_SEEDS:
            cfg = load_config(CONFIGS / f"exp_mem_e4s_{arm}.yaml", {"run": {"days": 1, "ticks_per_day": 6, "seed": seed}})
            _run(cfg, root / arm / f"seed_{seed}")
    return root


def test_e2e_h4_on_the_scripted_e4_mirror(e4s_exp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = compare(e4s_exp)
    (h4,) = result["hypotheses"]
    assert h4["name"] == "H4" and h4["levels"] == RUNGS and h4["reference"] == "average"
    assert [c["label"] for c in h4["contrasts"]] == ["genius", "sharp", "slow", "dim"]
    # each contrast's per-seed values are the per-rung outcomes of the runs, (selfedit - default at L) - (... at average)
    for c in h4["contrasts"]:
        assert c["n"] == len(E2E_SEEDS)
        for s in E2E_SEEDS:
            y = {(arm, lv): end_of_run_outcome(e4s_exp / arm / f"seed_{s}", f"tier_lineage_fitness.{lv}")
                 for arm in ("default", "selfedit") for lv in (c["label"], "average")}
            expected = (y["selfedit", c["label"]] - y["default", c["label"]]) - (y["selfedit", "average"] - y["default", "average"])
            assert c["per_seed"][str(s)] == pytest.approx(expected, abs=1e-12)
    assert isinstance(h4["passed"], bool) and h4["k"] == 4
    assert h4["passed"] == any(c["clears"] for c in h4["contrasts"])  # the only primary: family p_holm is its own p
    verdict = "PASS" if h4["passed"] else "FAIL"
    assert cli_main(["compare", str(e4s_exp)]) == 0
    out = capsys.readouterr().out
    assert f"  {verdict}  H4 [interaction two_sided, margin 0, primary] (selfedit - default) at each level - at average " \
           "on tier_lineage_fitness.{level}: 4 contrast(s) of 4" in out
    for rung in ("genius", "sharp", "slow", "dim"):
        assert f"contrast {rung} - average: n=3 (dropped 0)" in out
    spec = importlib.util.spec_from_file_location("compare_script", REPO / "scripts" / "compare.py")
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.main([str(e4s_exp), "--hypotheses-only"]) == (0 if h4["passed"] else 1)
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith(f"  {verdict}  H4") and len(lines) == 6  # header, H4, four contrasts


def test_h4_outcomes_resolve_after_one_day_in_every_scripted_e4_arm(e4s_exp: Path, tmp_path: Path) -> None:
    """tests/test_outcomes_resolve.py resolves each hypothesis's ``outcome``; an arm x levels interaction reads one path
    per level instead (``outcome_paths``), so H4's five are resolved here, in a one-day run of each scripted E4 arm: every
    rung has founders at any length (1/2/4/2/1), so none needs an exemption."""
    flat = tmp_path / "flat_tariff"
    _run(load_config(CONFIGS / "exp_mem_e4s_flat_tariff.yaml", {"run": {"days": 1, "ticks_per_day": 6, "seed": 1}}), flat)
    (h4,) = load_config(CONFIGS / "exp_mem_e4s_flat_tariff.yaml").experiment.hypotheses
    assert h4.outcome_paths() == [f"tier_lineage_fitness.{r}" for r in RUNGS]
    for run_dir in (e4s_exp / "default" / "seed_1", e4s_exp / "selfedit" / "seed_1", flat):
        for path in h4.outcome_paths():
            value = end_of_run_outcome(run_dir, path)
            assert isinstance(value, float) and value > 0, (run_dir.name, path, value)


def test_e2e_rung_placement_gives_every_rung_its_founders(e4s_exp: Path) -> None:
    import sqlite3

    for s in E2E_SEEDS:
        with sqlite3.connect(e4s_exp / "default" / f"seed_{s}" / "world.db") as db:
            rows = db.execute("SELECT model_tier, COUNT(*) FROM agents WHERE parent_id IS NULL AND born_tick = 0 "
                              "GROUP BY model_tier").fetchall()
        assert dict(rows) == {"genius": 1, "sharp": 2, "average": 4, "slow": 2, "dim": 1}


def test_e2e_four_arm_interaction_on_scripted_press_runs(tmp_path: Path) -> None:
    hyp = {**_four(name="press_x"), "direction": "two_sided", "primary": True}
    root = tmp_path / "exp_press"
    for arm in PRESS:
        for seed in E2E_SEEDS:
            cfg = load_config(CONFIGS / f"exp_press_{arm}.yaml", {"run": {"days": 1, "ticks_per_day": 6, "seed": seed},
                                                                  "experiment": {"hypotheses": [hyp]}})
            _run(cfg, root / arm / f"seed_{seed}")
    result = compare(root)
    (h,) = result["hypotheses"]
    (c,) = h["contrasts"]
    assert c["label"] == "interaction" and c["n"] == len(E2E_SEEDS)
    for s in E2E_SEEDS:
        y = {arm: end_of_run_outcome(root / arm / f"seed_{s}", "mean_balance") for arm in PRESS}
        assert c["per_seed"][str(s)] == pytest.approx((y["both"] - y["chronicle"]) - (y["gossip"] - y["none"]), abs=1e-12)
    assert isinstance(h["passed"], bool) and h["passed"] == bool(c["clears"])
    text = render(result)
    verdict = "PASS" if h["passed"] else "FAIL"
    assert f"  {verdict}  press_x [interaction two_sided, margin 0, primary] (both - chronicle) - (gossip - none) " \
           "on mean_balance: n=3" in text
    assert result["factorial"] is not None and "(descriptive, no verdict" in text
