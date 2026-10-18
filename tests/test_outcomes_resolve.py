"""Every declared outcome resolves (MEMORY_EVOLUTION 4 "Pre-registration", 6.11; CLAUDE.md: every claim is computed).

For every ``configs/exp_*.yaml`` arm (``experiment.arm`` set) whose roster is scripted only, a one-day run must resolve
each outcome the config declares -- ``experiment.primary_outcome``, every ``secondary_outcomes`` entry and the outcome
of every ``experiment.hypotheses`` entry -- to a number through :func:`void.research.analysis.end_of_run_outcome`.
The run is made from a temporary config that ``extends`` the arm and sets ``run.days: 1`` and ``run.seed: 1``, so the
arm's own chain of files is what is loaded. The only exceptions are :data:`ALLOWED_NONE`, one entry per config and
outcome with its reason. Two kinds of reason are accepted:

* *censored by the one-day run*: the outcome needs something a single day cannot hold (an exam item two days old, a
  fourth generation, a chronicle item matched in a vault, a windfall copy before the benefactor stops); a full-length
  run of the arm resolves it;
* *off by design in this arm*: the arm turns off what the outcome measures (no gossip in a press arm without it), so
  None is the right value. (The dependence check declares no outcome of this kind: its config leaves out the E0
  secondaries that no exam, no policy file or the amnesic arm's empty vault would make None.)

Also: every allow-list entry names a config and an outcome that the config declares (the list cannot go stale
silently), every hypothesis names arms its experiment has (over every config with an arm, live ones included, which
are loaded and never run), and every outcome the live E1 pilot declares is a name the analysis can resolve.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from void.config import VoidConfig, load_config
from void.research.analysis import computed_outcome_names, end_of_run_outcome, load_metrics
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"

_EXAM_DELAY = "censored: the audit exam asks about the world memory.exam.delay_days (2) days back, so a one-day run has no graded item"
_REWARD_DELAY = "censored: the reward exam, like the audit exam, asks two days back, so a one-day run has no graded item"
_NO_GOSSIP = "off by design: gossip is off in this arm, so there is no gossip_transfer copy to compare"
_CHRONICLE = "censored: no chronicle-channel note has been matched to a chronicle item by the end of day 1"
_GENERATION = "censored: no agent of generation 4 or later can exist after one day"
_WINDFALL = "censored: at seed 1 no windfall-lineage copy has arrived by the end of day 1, so the ratio has no denominator"

# config stem -> outcome -> why None is right for the one-day run
ALLOWED_NONE: dict[str, dict[str, str]] = {
    "exp_benefactor_legible": {"persistence": _WINDFALL},
    "exp_gate_off": {"balance_gen_ge.4": _GENERATION},
    "exp_gate_on": {"balance_gen_ge.4": _GENERATION},
    **{f"exp_mem_e0_{arm}": {"exam_score": _EXAM_DELAY, "exam_accuracy_over_baseline": _EXAM_DELAY}
       for arm in ("noop", "index_builder", "hoarder", "decoy", "distill", "revert", "retire")},
    **{f"exp_mem_e2_{arm}": {"exam_accuracy": _EXAM_DELAY, "exam_score": _EXAM_DELAY,
                             "reward_exam_accuracy": _REWARD_DELAY, "goodhart_gap": _REWARD_DELAY} for arm in ("a", "b")},
    **{f"exp_mem_e4s_{arm}": {"exam_score": _EXAM_DELAY} for arm in ("default", "flat_tariff", "selfedit")},
    "exp_mem_e5_eval": {"exam_score": _EXAM_DELAY},
    "exp_mem_e3a": {"exam_score": _EXAM_DELAY},  # extends exp_mem_e5_eval.yaml, same exam delay
    "exp_press_both": {"chronicle_fidelity": _CHRONICLE},
    "exp_press_chronicle": {"chronicle_fidelity": _CHRONICLE, "gossip_fidelity": _NO_GOSSIP},
    "exp_press_gossip": {"chronicle_fidelity": _CHRONICLE},
    "exp_press_none": {"chronicle_fidelity": _CHRONICLE, "gossip_fidelity": _NO_GOSSIP},
}


def declared_outcomes(cfg: VoidConfig) -> list[str]:
    """The primary, the secondaries and every hypothesis outcome, in that order, without repeats."""
    names = [cfg.experiment.primary_outcome, *cfg.experiment.secondary_outcomes,
             *(h.outcome for h in cfg.experiment.hypotheses)]
    return list(dict.fromkeys(n for n in names if n))


def _scripted_only(cfg: VoidConfig) -> bool:
    return all(cfg.tiers[a.tier].provider == "scripted" for a in cfg.agents)


def _arm_configs(pattern: str = "exp_*.yaml") -> list[Path]:
    return [p for p in sorted(CONFIGS.glob(pattern)) if load_config(p).experiment.arm is not None]


SCRIPTED_ARMS = [p for p in _arm_configs() if _scripted_only(load_config(p))]


@pytest.fixture(scope="module")
def one_day_run(tmp_path_factory: pytest.TempPathFactory):
    """``run(config_path) -> (cfg, run_dir)``: a one-day seed-1 run of the arm from a temporary config, made once."""
    root = tmp_path_factory.mktemp("outcomes_resolve")
    made: dict[str, tuple[VoidConfig, Path]] = {}

    def run(path: Path) -> tuple[VoidConfig, Path]:
        if path.stem not in made:
            tmp_cfg = root / "configs" / f"{path.stem}_1day.yaml"
            tmp_cfg.parent.mkdir(parents=True, exist_ok=True)
            tmp_cfg.write_text(yaml.safe_dump({"extends": str(path.resolve()), "run": {"days": 1, "seed": 1}}))
            cfg = load_config(tmp_cfg)
            run_dir = root / "runs" / path.stem
            sim = Simulation(cfg, run_dir)
            try:
                status = asyncio.run(sim.run())
            finally:
                sim.db.close()
            assert status == "completed", (path.name, status)
            made[path.stem] = (cfg, run_dir)
        return made[path.stem]

    return run


def test_there_are_scripted_arms_to_check() -> None:
    stems = {p.stem for p in SCRIPTED_ARMS}
    assert {"exp_mem_dep_mem", "exp_mem_dep_amnesic", "exp_mem_e0_noop", "exp_mem_e0_decoy"} <= stems
    assert not any(p.stem.endswith("_base") for p in SCRIPTED_ARMS)  # bases carry no arm and are not runs


@pytest.mark.parametrize("path", SCRIPTED_ARMS, ids=lambda p: p.stem)
def test_every_declared_outcome_resolves_after_one_day(path: Path, one_day_run) -> None:
    cfg, run_dir = one_day_run(path)
    assert cfg.run.days == 1 and cfg.experiment.arm == load_config(path).experiment.arm
    values = {name: end_of_run_outcome(run_dir, name) for name in declared_outcomes(cfg)}
    allowed = ALLOWED_NONE.get(path.stem, {})
    unexpected = sorted(name for name, v in values.items() if v is None and name not in allowed)
    assert not unexpected, f"{path.name}: declared outcomes resolve to None after one day: {unexpected}"
    for name, v in values.items():
        assert v is None or isinstance(v, float), (path.name, name, v)


def test_the_allow_list_names_declared_outcomes_of_scripted_arms() -> None:
    by_stem = {p.stem: p for p in SCRIPTED_ARMS}
    for stem, entries in ALLOWED_NONE.items():
        assert stem in by_stem, f"{stem} is not a scripted exp_* arm"
        declared = declared_outcomes(load_config(by_stem[stem]))
        for name, why in entries.items():
            assert name in declared, f"{stem}: {name} is allow-listed but not declared"
            assert why.startswith(("censored: ", "off by design: ")), (stem, name, why)


def test_every_hypothesis_names_arms_of_its_experiment() -> None:
    """Over every config with an arm (scripted and live, loaded only): a hypothesis's arms are arms of its experiment.
    This also catches a list inherited through ``extends`` from another experiment's base (exp_mem_e5_eval.yaml
    extends exp_mem_e0_base.yaml and so must clear E0's hypotheses, which name E0's arms)."""
    arms: dict[str, set[str]] = {}
    hyps: dict[str, set[tuple[str, str, str]]] = {}
    for path in _arm_configs("*.yaml"):
        cfg = load_config(path)
        exp = str(cfg.experiment.name or cfg.run.name)
        arms.setdefault(exp, set()).add(str(cfg.experiment.arm))
        hyps.setdefault(exp, set()).update((h.name, h.arm_a, h.arm_b) for h in cfg.experiment.hypotheses)
    assert {"exp_mem_dep", "exp_mem_e0", "exp_mem_e1"} <= set(hyps)
    assert hyps["exp_mem_e5"] == set()
    for exp, declared in sorted(hyps.items()):
        for name, a, b in sorted(declared):
            assert {a, b} <= arms[exp], f"{exp}: hypothesis {name!r} names {a!r} and {b!r}, arms are {sorted(arms[exp])}"


def test_the_live_e1_pilot_declares_outcomes_the_analysis_can_resolve(one_day_run) -> None:
    """The E1 configs are loaded, never run: each declared outcome is a computed or registered outcome, or a key of the
    metrics rows (read off a one-day scripted run of the dependence check)."""
    _, run_dir = one_day_run(CONFIGS / "exp_mem_dep_mem.yaml")
    tick_rows, day_rows = load_metrics(run_dir)
    known = set(computed_outcome_names()) | set(tick_rows[-1]) | set(day_rows[-1])
    for arm in ("a0", "a", "b", "c"):
        cfg = load_config(CONFIGS / f"live_mem_e1_{arm}.yaml")
        assert cfg.experiment.primary_outcome == "lineage_fitness"
        for name in declared_outcomes(cfg):
            assert name.split(".", 1)[0] in known, f"live_mem_e1_{arm}: {name} is not an outcome the analysis knows"
