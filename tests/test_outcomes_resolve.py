"""Every declared outcome resolves (MEMORY_EVOLUTION 4 "Pre-registration", 6.11; CLAUDE.md: every claim is computed).

For every ``configs/exp_*.yaml`` arm (``experiment.arm`` set) whose roster is scripted only, a one-day run must resolve
each outcome the config declares -- ``experiment.primary_outcome``, every ``secondary_outcomes`` entry and the outcome
of every ``experiment.hypotheses`` entry -- to a number through :func:`void.research.analysis.end_of_run_outcome`.
The run is made from a temporary config that ``extends`` the arm and sets ``run.days: 1`` and ``run.seed: 1``, so the
arm's own chain of files is what is loaded. The only exceptions are :data:`ALLOWED_NONE`, one :class:`Exemption` per
config and outcome, and each must still be None in that run (no stale exemptions). Every exemption carries one
category of :data:`CATEGORIES`, read off the arm's config and the outcome's code:

* ``delay``: the mechanism is on in this arm but cannot produce a value inside one day (an exam item at least
  ``delay_days`` old, a paper printed only when day 1 ends, a fourth generation); a longer run of the arm resolves it;
* ``off by design``: the arm switches the mechanism off (no gossip, no chronicle, no reward exam), so the outcome is
  undefined in this arm by construction and None is the right value at any length. (The dependence check declares no
  outcome of this kind: its config leaves out the E0 secondaries that no exam, no policy file or the amnesic arm's
  empty vault would make None.)
* ``censored (rare event)``: the mechanism is on and can resolve inside the run, but the event it needs did not
  happen at seed 1. The reason names a seed and a length at which it does happen, and
  :func:`test_a_rare_event_exemption_resolves_at_the_seed_and_length_its_reason_names` runs the arm there and checks
  the outcome is a number.

Also: every allow-list entry names a config and an outcome that the config declares (the list cannot go stale
silently), every hypothesis names arms its experiment has (over every config with an arm, live ones included, which
are loaded and never run), and every outcome the live E1 pilot declares is a name the analysis can resolve.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

from void.config import VoidConfig, load_config
from void.research.analysis import computed_outcome_names, end_of_run_outcome, load_metrics
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"

DELAY = "delay"
OFF_BY_DESIGN = "off by design"
RARE_EVENT = "censored (rare event)"
CATEGORIES = (DELAY, OFF_BY_DESIGN, RARE_EVENT)


class Exemption(NamedTuple):
    """Why an outcome is None after the one-day seed-1 run of an arm: a category of :data:`CATEGORIES` and the reason.
    ``resolves_at`` is ``(seed, days)`` of a run of the same arm in which the outcome is a number: set for a rare
    event (and named in its reason), unset otherwise."""

    category: str
    reason: str
    resolves_at: tuple[int, int] | None = None

    def __str__(self) -> str:
        return f"{self.category}: {self.reason}"


# Each reason below was re-derived from the arm's config chain and the outcome's code (void/research/analysis.py,
# void/research/outcomes/, void/research/exams.py, void/sim/loop.py); the lengths quoted were measured on scripted runs.
_EXAM = Exemption(DELAY, "the audit exam asks only about days at least memory.exam.delay_days (2) back and asks "
                         "nothing before day delay_days + 1 (Exams.run_audit_day), so a one-day run has no graded item; "
                         "the arm's own length resolves it")
_REWARD_EXAM = Exemption(DELAY, "arm B's reward exam asks about canary facts published at least "
                                "memory.selection.reward_delay_days (2) days before, and day d's canary is published with "
                                "day d's chronicle at the start of day d + 1, so the first reward item is asked on day 4 "
                                "of the arm's 6 (goodhart_gap also needs the audit exam, itself delayed)")
_NO_REWARD_EXAM = Exemption(OFF_BY_DESIGN, "arm A (memory.selection.arm: A) asks no reward exam (Exams.reward_enabled "
                                           "is arm B only), so reward_exam_accuracy, and goodhart_gap with it, is None "
                                           "at any length")
_CHRONICLE = Exemption(DELAY, "day d's chronicle is printed at the start of day d + 1 (or when the run finishes), and "
                              "read_chronicle is offered only once a paper exists, so in a one-day run nobody reads one "
                              "and no chronicle-channel note exists to match; two days resolve it")
_NO_CHRONICLE = Exemption(OFF_BY_DESIGN, "chronicle.enabled is false in this arm: no paper is printed or read, so no "
                                         "chronicle-channel note exists to match")
_NO_GOSSIP = Exemption(OFF_BY_DESIGN, "gossip.enabled is false in this arm, so there is no gossip_transfer event to "
                                      "compare")
_GENERATION = Exemption(DELAY, "an agent of generation 4 needs four successive births, each parent born in the run "
                               "first earning the offspring price, and in this arm's lean world that takes most of the "
                               "arm's 30 days, not one (exp_gate_on at seed 1: deepest generation 1 after day 1, 2 "
                               "after day 10, 4 at day 30)")
_WINDFALL = Exemption(RARE_EVENT, "persistence needs a windfall-lineage copy (gossip or inherited) to arrive on or "
                                  "before benefactor.stop_day (2), its denominator; at seed 1 none arrives even over the "
                                  "arm's full 4 days, while at seed 3 one arrives inside the first day, so the outcome "
                                  "resolves at seed 3, 1 day", resolves_at=(3, 1))

# config stem -> outcome -> why None is right for the one-day seed-1 run
ALLOWED_NONE: dict[str, dict[str, Exemption]] = {
    "exp_benefactor_legible": {"persistence": _WINDFALL},
    "exp_gate_off": {"balance_gen_ge.4": _GENERATION},
    "exp_gate_on": {"balance_gen_ge.4": _GENERATION},
    **{f"exp_mem_e0_{arm}": {"exam_score": _EXAM, "exam_accuracy_over_baseline": _EXAM}
       for arm in ("noop", "index_builder", "hoarder", "decoy", "distill", "revert", "retire")},
    "exp_mem_e2_a": {"exam_accuracy": _EXAM, "exam_score": _EXAM,
                     "reward_exam_accuracy": _NO_REWARD_EXAM, "goodhart_gap": _NO_REWARD_EXAM},
    "exp_mem_e2_b": {"exam_accuracy": _EXAM, "exam_score": _EXAM,
                     "reward_exam_accuracy": _REWARD_EXAM, "goodhart_gap": _REWARD_EXAM},
    **{f"exp_mem_e4s_{arm}": {"exam_score": _EXAM} for arm in ("default", "flat_tariff", "selfedit")},
    "exp_mem_e5_eval": {"exam_score": _EXAM},
    "exp_mem_e3a": {"exam_score": _EXAM},  # extends exp_mem_e5_eval.yaml, same exam delay
    "exp_press_both": {"chronicle_fidelity": _CHRONICLE},
    "exp_press_chronicle": {"chronicle_fidelity": _CHRONICLE, "gossip_fidelity": _NO_GOSSIP},
    "exp_press_gossip": {"chronicle_fidelity": _NO_CHRONICLE},
    "exp_press_none": {"chronicle_fidelity": _NO_CHRONICLE, "gossip_fidelity": _NO_GOSSIP},
}

RARE_EVENTS = [(stem, name, ex) for stem, entries in ALLOWED_NONE.items() for name, ex in entries.items()
               if ex.category == RARE_EVENT]


def declared_outcomes(cfg: VoidConfig) -> list[str]:
    """The primary, the secondaries and every hypothesis outcome (one per level for a by-level interaction), in that
    order, without repeats."""
    names = [cfg.experiment.primary_outcome, *cfg.experiment.secondary_outcomes,
             *(p for h in cfg.experiment.hypotheses for p in h.outcome_paths())]
    return list(dict.fromkeys(n for n in names if n))


def _scripted_only(cfg: VoidConfig) -> bool:
    return all(cfg.tiers[a.tier].provider == "scripted" for a in cfg.agents)


def _arm_configs(pattern: str = "exp_*.yaml") -> list[Path]:
    return [p for p in sorted(CONFIGS.glob(pattern)) if load_config(p).experiment.arm is not None]


SCRIPTED_ARMS = [p for p in _arm_configs() if _scripted_only(load_config(p))]


def _run_arm(path: Path, root: Path, *, seed: int, days: int) -> tuple[VoidConfig, Path]:
    """Run the arm at ``path`` for ``days`` days at ``seed`` from a temporary config that ``extends`` it."""
    tmp_cfg = root / "configs" / f"{path.stem}_s{seed}_{days}day.yaml"
    tmp_cfg.parent.mkdir(parents=True, exist_ok=True)
    tmp_cfg.write_text(yaml.safe_dump({"extends": str(path.resolve()), "run": {"days": days, "seed": seed}}))
    cfg = load_config(tmp_cfg)
    run_dir = root / "runs" / f"{path.stem}_s{seed}_{days}day"
    sim = Simulation(cfg, run_dir)
    try:
        status = asyncio.run(sim.run())
    finally:
        sim.db.close()
    assert status == "completed", (path.name, seed, days, status)
    return cfg, run_dir


@pytest.fixture(scope="module")
def one_day_run(tmp_path_factory: pytest.TempPathFactory):
    """``run(config_path) -> (cfg, run_dir)``: a one-day seed-1 run of the arm from a temporary config, made once."""
    root = tmp_path_factory.mktemp("outcomes_resolve")
    made: dict[str, tuple[VoidConfig, Path]] = {}

    def run(path: Path) -> tuple[VoidConfig, Path]:
        if path.stem not in made:
            made[path.stem] = _run_arm(path, root, seed=1, days=1)
        return made[path.stem]

    return run


def test_there_are_scripted_arms_to_check() -> None:
    stems = {p.stem for p in SCRIPTED_ARMS}
    assert {"exp_mem_dep_mem", "exp_mem_dep_amnesic", "exp_mem_e0_noop", "exp_mem_e0_decoy"} <= stems
    assert not any(p.stem.endswith("_base") for p in SCRIPTED_ARMS)  # bases carry no arm and are not runs


@pytest.mark.parametrize("path", SCRIPTED_ARMS, ids=lambda p: p.stem)
def test_every_declared_outcome_resolves_after_one_day(path: Path, one_day_run) -> None:
    cfg, run_dir = one_day_run(path)
    assert cfg.run.days == 1 and cfg.run.seed == 1 and cfg.experiment.arm == load_config(path).experiment.arm
    values = {name: end_of_run_outcome(run_dir, name) for name in declared_outcomes(cfg)}
    allowed = ALLOWED_NONE.get(path.stem, {})
    unexpected = sorted(name for name, v in values.items() if v is None and name not in allowed)
    assert not unexpected, f"{path.name}: declared outcomes resolve to None after one day: {unexpected}"
    stale = sorted(f"{name} = {values[name]!r} ({allowed[name].category})" for name in allowed
                   if values.get(name) is not None)
    assert not stale, f"{path.name}: allow-listed outcomes resolve after one day, so their exemption is stale: {stale}"
    for name, v in values.items():
        assert v is None or isinstance(v, float), (path.name, name, v)


def test_the_allow_list_names_declared_outcomes_of_scripted_arms() -> None:
    by_stem = {p.stem: p for p in SCRIPTED_ARMS}
    for stem, entries in ALLOWED_NONE.items():
        assert stem in by_stem, f"{stem} is not a scripted exp_* arm"
        declared = declared_outcomes(load_config(by_stem[stem]))
        for name in entries:
            assert name in declared, f"{stem}: {name} is allow-listed but not declared"


def test_every_exemption_carries_a_category() -> None:
    for stem, entries in ALLOWED_NONE.items():
        for name, ex in entries.items():
            where = f"{stem}: {name}"
            assert isinstance(ex, Exemption), (where, ex)
            assert ex.category in CATEGORIES, f"{where}: category {ex.category!r} is not one of {CATEGORIES}"
            assert ex.reason.strip(), f"{where}: no reason"
            assert str(ex).startswith(tuple(f"{c}: " for c in CATEGORIES)), (where, str(ex))
            if ex.category == RARE_EVENT:
                assert ex.resolves_at is not None, f"{where}: a rare-event censoring names a seed and length that resolve it"
                seed, days = ex.resolves_at
                assert seed >= 1 and days >= 1, (where, ex.resolves_at)
                assert f"seed {seed}, {days} day" in ex.reason, f"{where}: the reason does not name seed {seed}, {days} day(s)"
            else:
                assert ex.resolves_at is None, f"{where}: only a rare-event censoring names a resolving run"


def test_the_exemption_categories_are_those_derived_from_the_arms() -> None:
    """Pins the categories re-derived from the configs: a chronicle or gossip switched off, or arm A's missing reward
    exam, is off by design; a mechanism that is on in the arm is never labelled off."""
    for stem, entries in ALLOWED_NONE.items():
        cfg = load_config(CONFIGS / f"{stem}.yaml")
        for name, ex in entries.items():
            if name == "chronicle_fidelity":
                assert (ex.category == OFF_BY_DESIGN) == (not cfg.chronicle.enabled), (stem, name, str(ex))
            if name == "gossip_fidelity":
                assert (ex.category == OFF_BY_DESIGN) == (not cfg.gossip.enabled), (stem, name, str(ex))
            if name in ("reward_exam_accuracy", "goodhart_gap"):
                assert (ex.category == OFF_BY_DESIGN) == (cfg.memory.selection.arm != "B"), (stem, name, str(ex))
            if name.startswith(("exam_", "balance_gen_ge")):
                assert ex.category == DELAY, (stem, name, str(ex))
            if ex.category == DELAY and name.startswith("exam_"):
                assert cfg.memory.exam.enabled and cfg.memory.exam.delay_days >= 1, (stem, name)
            if name == "persistence":
                assert cfg.benefactor.enabled and cfg.benefactor.stop_day is not None, (stem, name)


@pytest.mark.parametrize(("stem", "name", "ex"), RARE_EVENTS, ids=[f"{s}-{n}" for s, n, _ in RARE_EVENTS])
def test_a_rare_event_exemption_resolves_at_the_seed_and_length_its_reason_names(
        stem: str, name: str, ex: Exemption, tmp_path: Path) -> None:
    assert ex.resolves_at is not None
    seed, days = ex.resolves_at
    cfg, run_dir = _run_arm(CONFIGS / f"{stem}.yaml", tmp_path, seed=seed, days=days)
    assert (cfg.run.seed, cfg.run.days) == (seed, days)
    v = end_of_run_outcome(run_dir, name)
    assert isinstance(v, float), f"{stem}: {name} is {v!r} at seed {seed}, {days} day(s); its reason says it resolves there"


def test_every_hypothesis_names_arms_of_its_experiment() -> None:
    """Over every config with an arm (scripted and live, loaded only): a hypothesis's arms are arms of its experiment.
    This also catches a list inherited through ``extends`` from another experiment's base (exp_mem_e5_eval.yaml
    extends exp_mem_e0_base.yaml and so must clear E0's hypotheses, which name E0's arms)."""
    arms: dict[str, set[str]] = {}
    hyps: dict[str, set[tuple[str, ...]]] = {}
    for path in _arm_configs("*.yaml"):
        cfg = load_config(path)
        exp = str(cfg.experiment.name or cfg.run.name)
        arms.setdefault(exp, set()).add(str(cfg.experiment.arm))
        hyps.setdefault(exp, set()).update((h.name, *(a for a in (h.arm_a, h.arm_b, h.arm_c, h.arm_d) if a))
                                           for h in cfg.experiment.hypotheses)
    assert {"exp_mem_dep", "exp_mem_e0", "exp_mem_e1"} <= set(hyps)
    assert hyps["exp_mem_e5"] == set()
    for exp, declared in sorted(hyps.items()):
        for name, *named in sorted(declared):
            assert set(named) <= arms[exp], f"{exp}: hypothesis {name!r} names {named}, arms are {sorted(arms[exp])}"


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
