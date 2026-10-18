"""Gate G0, computed from runs (docs/research/MEMORY_EVOLUTION.md sections 5.2, 6.2 and 10.2).

G0 is four pre-registered conditions on paired runs, each read off the same per-seed outcomes and the same seeded
paired bootstrap that ``scripts/compare.py`` reports (:func:`void.research.compare.paired_stats`: 2000 resamples,
``random.Random(0)``, so the output is deterministic). The effect is always ``arm_b - arm_a``, paired by seed.

* **C1, memory dependence** (5.2), ``exp_mem_dep``: ``mem - amnesic`` on ``mean_balance`` is above a preset margin
  and the 95% interval lies above 0. The margin comes from the experiment config's ``experiment.hypotheses`` when one
  is declared for this contrast (``amnesic`` -> ``mem``, ``greater``, or the reverse with ``less``; ``margin_unit: sd``
  scales it by the SD of the paired differences); otherwise from the proposal. Section 5.2 says only "a preset
  margin" and states no number, so the proposal's value is taken as 0 (plain superiority) and the output says so.
* **C2, positive control** (6.2), ``exp_mem_e0``: ``index_builder - noop`` on ``mean_balance`` is above 0, the 95%
  interval lies above 0, and the estimated power of the paired test at the observed effect (``d_z``) and the
  observed number of pairs is at least 0.8 (:func:`void.research.stats.power_paired`, ``method="t"``, the closer of
  its two approximations; the normal one overstates power at small n).
* **C3, negative control** (6.2): ``hoarder - noop`` on ``mean_balance`` is below 0 and the 95% interval lies below 0.
  "Ranks hoarder below no-op" is read as a significant ranking, not a point estimate that could be noise.
* **C4, metric check** (6.2 as revised there before any paid run): the decoy is not significantly above the no-op on
  ``mean_balance`` (the 95% interval's lower bound is at most 0) *and* it is significantly above it on
  ``policy_chars``, the naive text metric (the interval lies above 0). ``policy_chars`` is computed here: the mean
  over agents of the length of each agent's latest ``policy_versions`` text.

Verdicts: ``PASS``, ``FAIL``, or ``MISSING`` when the condition cannot be evaluated: an arm has no runs or no completed
run, fewer than two complete seed pairs carry a value (no interval), or the runs were refused. Runs are refused when
any run of the two arms is incomplete (:func:`void.research.compare.run_state`: status other than ``completed``,
metrics short of the last tick, a line cut short) or a seed has a run in one arm only, unless ``allow_incomplete``,
which drops every seed pair that touches such a run and lists it; and when the two arms are not a fair comparison
(runs of one arm differ outside ``run.seed``; the arms differ outside ``experiment.independent_variables``;
``entropy.degeneration.mode`` is not ``observe``; an ``exp_mem`` experiment not on equalized prices). G0 is ``FAIL``
when any condition fails, else ``MISSING`` when any is missing, else ``PASS``; :data:`EXIT_CODES` maps it to 1, 2, 0.
A decided condition on fewer than :data:`PRIMARY_PAIRS` pairs (6.11 asks for 20 for primary claims) keeps its verdict
and is flagged ``below_primary_pairs``: a percentile bootstrap on a handful of seeds is a rough interval (with two
pairs it spans little more than the two differences), and the live stages run 16 and 32 pairs by default. In live
mode the G0 line stays FAIL while E0 fails, and the E1 release rule is per world (MEMORY_EVOLUTION.md 10.4).

Live mode (``live_root``): C1 is evaluated instead on each live paired experiment of ``scripts/live_claude.sh``
(:data:`LIVE_DEPENDENCE`: the original world and the vein world), by this same code; every one of them must pass.
The E0 controls (C2 to C4) are scripted by design and are still read from the runs root.

**Declared hypotheses.** Each condition reads the contrast it tests from the runs' ``experiment.hypotheses`` (the
same entries ``scripts/compare.py`` decides; :func:`declared_hypothesis`): C1 its margin from the superiority
hypothesis ``amnesic -> mem``; C2 its margin and ``min_power`` from ``noop -> index_builder`` (``greater``), where the
power required is never below :data:`MIN_POWER`, the value 6.2 fixes; C3 its margin from ``noop -> hoarder``
(``less``); C4 the margin of the decoy's non-superiority on ``mean_balance`` and of its superiority on
``policy_chars``. A margin in ``sd`` units is scaled by the SD of that outcome's paired differences. Every rule keeps
its shape with the declared margin ``m``: C1 and C2 need the estimate above ``m``, C3 below ``-m``, C1 to C3 the
interval clear of 0 on their side, C4 the balance interval's lower bound at most ``m`` and the text metric's above
``m``. Each condition names its source: the hypothesis and the config file that declares it
(:func:`declaring_config`, which follows the ``extends`` chain from ``configs/<run.name>.yaml``), or the proposal's
value of 0 when nothing is declared. ``configs/exp_mem_dep_base.yaml`` and ``configs/exp_mem_e0_base.yaml`` declare
all five, every margin 0, so the verdicts are those of the rules above with margin 0.

**G1** (10.2: "memory-dependence check passes; valid-command rate at least 90%; the E1 pilot shows ``c`` over ``b`` at
``d`` of at least 0.5 with 10 seeds"), with ``g1_dir``, the E1 experiment directory (``<arm>/seed_<n>/``, arms
``a0``, ``a``, ``b``, ``c``), adds two conditions:

* **G1a, valid commands**: in every arm whose runs have maintenance on (``memory.maintenance.enabled``) or carry a
  value, the mean over its complete runs of ``maintenance_valid_command_rate`` (successful ``maintain`` commands over
  all of them, per run) is at least :data:`MIN_VALID_RATE`. Arms without maintenance (``a0``, ``a``) have no
  commands to be valid and are listed as not applicable. ``MISSING`` when no arm runs maintenance or an arm with
  maintenance on has no value in any complete run (no command was issued).
* **G1b, c over b**: ``c - b`` on the outcome of the declared superiority hypothesis ``b -> c`` (else the experiment's
  primary outcome, else ``lineage_fitness``, the ``F_L`` of 6.3) has ``d_z`` at least :data:`G1_MIN_DZ` on at least
  :data:`G1_PILOT_PAIRS` pairs. Fewer pairs leave it ``MISSING``; identical non-zero differences count as an unbounded
  ``d_z``. G1's alternative ("or a clear qualitative change in vault structure") is a judgment, not computed here.

The G1 line combines the memory-dependence lines (``C1``, or the ``C1[...]`` lines in live mode: 10.4 makes the live
check the first condition of G1, so pass ``live_root`` for it), G1a and G1b. With ``g1_dir`` the exit status covers
every condition printed, G0's and G1's.

Nothing here writes to a run, calls a provider or draws from a shared random stream.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from void.config import HypothesisConfig
from void.research.analysis import connect, end_of_run_outcome, resolve_path, run_config_dict
from void.research.compare import (
    ALWAYS_ALLOWED,
    IncomparableRuns,
    config_diff,
    covered_by,
    discover_runs,
    in_family,
    paired_stats,
    run_state,
)
from void.research.stats import power_paired

__all__ = [
    "BOOT_SEED",
    "DEPENDENCE",
    "E0",
    "EXIT_CODES",
    "G1_MIN_DZ",
    "G1_OUTCOME",
    "G1_PILOT_PAIRS",
    "LIVE_DEPENDENCE",
    "MIN_POWER",
    "MIN_VALID_RATE",
    "PRIMARY_PAIRS",
    "N_BOOT",
    "OUTCOME",
    "POWER_METHOD",
    "PROPOSAL_MARGIN",
    "Paired",
    "condition_c_over_b",
    "condition_decoy",
    "condition_dependence",
    "condition_hoarder",
    "condition_index_builder",
    "condition_valid_commands",
    "declared_hypothesis",
    "declaring_config",
    "dependence_margin",
    "evaluate_g0",
    "g1_outcome",
    "hypothesis_source",
    "notes",
    "overall",
    "paired_outcomes",
    "policy_chars",
    "render",
]

DEPENDENCE = "exp_mem_dep"
E0 = "exp_mem_e0"
LIVE_DEPENDENCE = ("exp_mem_dep_claude", "exp_mem_dep_veins_claude")
OUTCOME = "mean_balance"
TEXT_METRIC = "policy_chars"
N_BOOT = 2000
BOOT_SEED = 0
ALPHA = 0.05
MIN_POWER = 0.8
PRIMARY_PAIRS = 20  # MEMORY_EVOLUTION 6.11: "20 paired seeds per arm for primary claims"
POWER_METHOD = "t"
PROPOSAL_MARGIN = 0.0
PROPOSAL_MARGIN_SOURCE = "the proposal: MEMORY_EVOLUTION 5.2 states no number, so 0 (plain superiority) [assumption]"
PROPOSAL_E0_SOURCE = "the proposal: MEMORY_EVOLUTION 6.2 states no margin, so 0 [assumption]"
EXIT_CODES = {"PASS": 0, "FAIL": 1, "MISSING": 2}
PASS, FAIL, MISSING = "PASS", "FAIL", "MISSING"
# G1 (MEMORY_EVOLUTION 10.2)
VALID_COMMANDS = "maintenance_valid_command_rate"
MIN_VALID_RATE = 0.9      # "valid-command rate at least 90%"
G1_MIN_DZ = 0.5           # "c over b at d of at least 0.5"
G1_PILOT_PAIRS = 10       # "with 10 seeds"
G1_OUTCOME = "lineage_fitness"  # 6.3: "F_L primary"; used when the runs declare neither a b -> c hypothesis nor a primary
CONFIGS_DIR = Path(__file__).resolve().parents[2] / "configs"


# --- outcomes ---------------------------------------------------------------------------------------------
def policy_chars(run_dir: str | Path) -> float | None:
    """Mean over agents of the length, in characters, of each agent's latest ``policy_versions`` text: the naive text
    metric of the decoy check (6.2). None when the run kept no policy (the policy file was off) or has no database."""
    try:
        with connect(run_dir) as db:
            rows = db.execute(
                "SELECT p.text AS text FROM policy_versions p JOIN "
                "(SELECT agent_id, MAX(version) AS v FROM policy_versions GROUP BY agent_id) m "
                "ON p.agent_id = m.agent_id AND p.version = m.v").fetchall()
    except (FileNotFoundError, sqlite3.Error):
        return None
    if not rows:
        return None
    return sum(len(str(r["text"])) for r in rows) / len(rows)


OUTCOMES: dict[str, Callable[[str | Path], float | None]] = {TEXT_METRIC: policy_chars}


def _outcome(run_dir: Path, path: str) -> float | None:
    fn = OUTCOMES.get(path)
    return fn(run_dir) if fn is not None else end_of_run_outcome(run_dir, path)


# --- loading paired runs ---------------------------------------------------------------------------------------
@dataclass
class Paired:
    """Per-seed outcomes of two arms of one experiment, for the seeds where both runs are complete.

    ``values[outcome][seed] = (a, b)`` (either may be None when the run has no value). ``missing`` says why the pair
    cannot be evaluated (no runs, no completed run, refused); ``refused`` marks a refusal among those. ``dropped`` lists
    the runs left out under ``allow_incomplete``. ``hypotheses`` is ``experiment.hypotheses`` of the configs,
    ``primary_outcome`` their ``experiment.primary_outcome`` and ``run_name`` the ``run.name`` of ``arm_b``'s runs (the
    config file :func:`declaring_config` starts from)."""

    experiment: str
    exp_dir: str
    arm_a: str
    arm_b: str
    values: dict[str, dict[int, tuple[float | None, float | None]]] = field(default_factory=dict)
    missing: str | None = None
    refused: bool = False
    dropped: list[str] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    runs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    primary_outcome: str | None = None
    run_name: str | None = None

    def pairs(self, outcome: str) -> list[tuple[float, float]]:
        """``(a, b)`` pairs in seed order, leaving out seeds where either run has no value."""
        return [(a, b) for _, (a, b) in sorted(self.values.get(outcome, {}).items()) if a is not None and b is not None]

    def n_without_value(self, outcome: str) -> int:
        return sum(1 for a, b in self.values.get(outcome, {}).values() if a is None or b is None)


def _incomparable(cfgs: dict[str, dict[int, dict[str, Any]]], exp_dir: Path) -> str | None:
    """Why the runs of two arms are not a fair comparison, or None (the guards of :func:`compare.compare` that apply
    to one pair of arms)."""
    for arm, by_seed in cfgs.items():
        seeds = sorted(by_seed)
        base = by_seed[seeds[0]]
        for seed in seeds[1:]:
            extra = [p for p in config_diff(base, by_seed[seed]) if p != "run.seed"]
            if extra:
                return f"arm {arm!r}: runs for seeds {seeds[0]} and {seed} differ outside run.seed: {extra[:8]}"
    reps = {arm: by_seed[min(by_seed)] for arm, by_seed in cfgs.items()}
    names = {str(resolve_path(rep, "experiment.name") or exp_dir.name) for rep in reps.values()}
    if len(names) > 1:
        return f"the arms belong to different experiments: {sorted(names)}"
    ivs: list[str] = []
    for rep in reps.values():
        for iv in resolve_path(rep, "experiment.independent_variables") or []:
            if str(iv) not in ivs:
                ivs.append(str(iv))
    (a, ra), (b, rb) = reps.items()
    diffs = [p for p in config_diff(ra, rb) if p not in ALWAYS_ALLOWED and not covered_by(p, ivs)]
    if diffs:
        return f"configs of {a} and {b} differ outside experiment.independent_variables {ivs}: {diffs[:8]}"
    exp_name = names.pop()
    for arm, rep in reps.items():
        mode = resolve_path(rep, "entropy.degeneration.mode")
        if mode != "observe":
            return f"arm {arm!r}: entropy.degeneration.mode is {mode!r}; experiments must use 'observe'"
        pricing = resolve_path(rep, "economy.world_pricing")
        if in_family(exp_name, "exp_mem") and pricing != "equalized":
            return f"arm {arm!r}: economy.world_pricing is {pricing!r}; the exp_mem family requires 'equalized'"
    return None


def paired_outcomes(exp_dir: str | Path, arm_a: str, arm_b: str,
                    outcomes: Sequence[str] | Callable[[Paired], Sequence[str]], *,
                    allow_incomplete: bool = False) -> Paired:
    """Load the runs of ``arm_a`` and ``arm_b`` under ``exp_dir`` (``<arm>/seed_<n>/``) and their ``outcomes``, paired
    by seed. ``outcomes`` may be a function of the loaded :class:`Paired` (its hypotheses and primary outcome are set
    by then), for a condition whose outcome is declared in the runs' configs. Never raises for a missing, refused or
    incomplete experiment: :attr:`Paired.missing` says why instead."""
    exp_dir = Path(exp_dir)
    out = Paired(experiment=exp_dir.name, exp_dir=str(exp_dir), arm_a=arm_a, arm_b=arm_b)
    try:
        found = discover_runs(exp_dir)
    except IncomparableRuns:
        found = {}
    absent = [arm for arm in (arm_a, arm_b) if not found.get(arm)]
    if absent:
        out.missing = f"no runs for arm {' and '.join(repr(a) for a in absent)} under {exp_dir}"
        return out
    runs = {arm: found[arm] for arm in (arm_a, arm_b)}
    try:
        cfgs = {arm: {seed: run_config_dict(rd) for seed, rd in by_seed.items()} for arm, by_seed in runs.items()}
    except (OSError, ValueError, sqlite3.Error) as e:  # a config that does not parse is a run nobody can trust
        out.missing, out.refused = f"refused: a resolved config does not load ({e})", True
        return out
    rep = cfgs[arm_b][min(cfgs[arm_b])]
    out.experiment = str(resolve_path(rep, "experiment.name") or exp_dir.name)
    out.hypotheses = [h for h in (resolve_path(rep, "experiment.hypotheses") or []) if isinstance(h, dict)]
    primary, run_name = resolve_path(rep, "experiment.primary_outcome"), resolve_path(rep, "run.name")
    out.primary_outcome = str(primary) if primary else None
    out.run_name = str(run_name) if run_name else None
    why = _incomparable(cfgs, exp_dir)
    if why:
        out.missing, out.refused = f"refused: {why}", True
        return out

    problems: dict[tuple[str, int], str] = {}
    for arm, by_seed in runs.items():
        out.runs[arm] = {}
        for seed, rd in sorted(by_seed.items()):
            state, reason = run_state(rd, cfgs[arm][seed])
            out.runs[arm][str(seed)] = {**state, "complete": reason is None, "reason": reason}
            if reason:
                problems[(arm, seed)] = reason
    complete = {arm: sorted(s for s in runs[arm] if (arm, s) not in problems) for arm in runs}
    empty = [arm for arm in (arm_a, arm_b) if not complete[arm]]
    if empty:
        out.missing = "; ".join(f"arm {arm!r} has no completed runs ({len(runs[arm])} incomplete)" for arm in empty)
        return out
    listed = [f"{arm} seed {seed}: {why}" for (arm, seed), why in sorted(problems.items())]
    unpaired = [f"{arm} seed {seed}: no run in arm {other!r}" for arm, other in ((arm_a, arm_b), (arm_b, arm_a))
                for seed in sorted(set(runs[arm]) - set(runs[other]))]
    if (listed or unpaired) and not allow_incomplete:
        parts = ([f"incomplete runs: {'; '.join(listed)}"] if listed else []) + \
                ([f"seed sets differ: {'; '.join(unpaired)}"] if unpaired else [])
        out.missing = "refused: " + "; ".join(parts) + " (--allow-incomplete drops the seed pairs they touch)"
        out.refused = True
        return out
    out.dropped = listed + unpaired
    seeds = sorted(set(complete[arm_a]) & set(complete[arm_b]))
    for path in (outcomes(out) if callable(outcomes) else outcomes):
        out.values[path] = {s: (_outcome(runs[arm_a][s], path), _outcome(runs[arm_b][s], path)) for s in seeds}
    return out


# --- the conditions ----------------------------------------------------------------------------------------------
def _stats(paired: Paired, outcome: str) -> dict[str, Any]:
    s = paired_stats(paired.pairs(outcome), n_boot=N_BOOT, seed=BOOT_SEED)
    return {"outcome": outcome, "n": s["n"], "estimate": s["delta"], "ci95": list(s["ci95"]), "d_z": s["d_z"],
            "mean_a": s["mean_a"], "mean_b": s["mean_b"], "sign_test_p": s["sign_test_p"], "positive": s["positive"],
            "negative": s["negative"], "ties": s["ties"], "without_value": paired.n_without_value(outcome),
            "below_primary_pairs": s["n"] < PRIMARY_PAIRS}


def _base(cid: str, name: str, paired: Paired, rule: str) -> dict[str, Any]:
    return {"id": cid, "name": name, "experiment": paired.experiment, "exp_dir": paired.exp_dir,
            "arm_a": paired.arm_a, "arm_b": paired.arm_b, "rule": rule, "verdict": MISSING, "reason": paired.missing,
            "dropped": list(paired.dropped), "refused": paired.refused, "runs": paired.runs}


def _unusable(paired: Paired, *stats: dict[str, Any]) -> str | None:
    """Why a condition cannot be decided: the pair is missing or refused, or an outcome has fewer than two pairs."""
    if paired.missing:
        return paired.missing
    for s in stats:
        if s["n"] < 2:
            return (f"{s['outcome']}: {s['n']} complete seed pair(s) with a value, fewer than two, so there is no "
                    f"interval" + (f" ({s['without_value']} without a value)" if s["without_value"] else ""))
    return None


def _above(s: dict[str, Any], bound: float = 0.0) -> bool:
    lo = s["ci95"][0]
    return lo is not None and lo > bound


def _below(s: dict[str, Any], bound: float = 0.0) -> bool:
    hi = s["ci95"][1]
    return hi is not None and hi < bound


_FLIP = {"greater": "less", "less": "greater"}


def declared_hypothesis(hypotheses: Iterable[Mapping[str, Any]], arm_a: str, arm_b: str, outcome: str | None,
                        kind: str = "superiority", direction: str = "greater") -> dict[str, Any] | None:
    """The first ``experiment.hypotheses`` entry declared for the ``arm_b - arm_a`` contrast of ``kind`` in
    ``direction`` on ``outcome`` (any outcome when None), else None. The reverse orientation (``arm_b -> arm_a``) with
    the opposite direction is the same contrast; an equivalence hypothesis has no direction, so either orientation
    matches it."""
    for h in hypotheses:
        if outcome is not None and h.get("outcome") != outcome:
            continue
        if h.get("kind", "superiority") != kind:
            continue
        pair, d = (h.get("arm_a"), h.get("arm_b")), h.get("direction", "greater")
        if kind == "equivalence" and pair in ((arm_a, arm_b), (arm_b, arm_a)):
            return dict(h)
        if (pair == (arm_a, arm_b) and d == direction) or (pair == (arm_b, arm_a) and d == _FLIP.get(direction)):
            return dict(h)
    return None


def declaring_config(run_name: str | None, hypothesis: Mapping[str, Any], configs_dir: str | Path | None = None
                     ) -> str | None:
    """The config file that declares ``hypothesis`` for runs named ``run_name``: starting at
    ``<configs_dir>/<run_name>.yaml`` (default :data:`CONFIGS_DIR`) and following ``extends``, the nearest file that
    sets ``experiment.hypotheses`` (a list replaces, never merges, under ``extends``), when that list declares an entry
    of the same name equal to ``hypothesis`` once defaults are filled in. The path is given relative to the repository
    (``configs/<file>``) when it lies inside it. None when there is no such file, the file now declares the entry
    differently (the runs' resolved config is then the only source), or a file does not parse."""
    if not run_name:
        return None
    root = Path(configs_dir) if configs_dir is not None else CONFIGS_DIR
    path, seen = (root / f"{run_name}.yaml").resolve(), set()
    while path.is_file() and path not in seen:
        seen.add(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return None
        if not isinstance(data, dict):
            return None
        exp = data.get("experiment")
        hyps = exp.get("hypotheses") if isinstance(exp, dict) else None
        if hyps is not None:
            for h in hyps if isinstance(hyps, list) else []:
                if isinstance(h, dict) and h.get("name") == hypothesis.get("name"):
                    try:
                        declared = HypothesisConfig.model_validate(h).model_dump(mode="json")
                    except ValidationError:
                        return None
                    if any(declared[k] != hypothesis.get(k, declared[k]) for k in declared):
                        return None
                    try:
                        return str(path.relative_to(CONFIGS_DIR.parent.resolve()))
                    except ValueError:
                        return str(path)
            return None
        parent = data.get("extends")
        if not isinstance(parent, str) or not parent:
            return None
        path = (path.parent / parent).resolve()
    return None


def hypothesis_source(hypothesis: Mapping[str, Any], run_name: str | None = None) -> str:
    """``experiment.hypotheses '<name>'``, followed by `` in configs/<file>`` when :func:`declaring_config` finds the
    file that declares it."""
    text = f"experiment.hypotheses {hypothesis.get('name')!r}"
    where = declaring_config(run_name, hypothesis)
    return f"{text} in {where}" if where else text


def _declared_margin(h: Mapping[str, Any] | None, run_name: str | None, default_source: str) -> tuple[float, str, str]:
    if h is None:
        return PROPOSAL_MARGIN, "abs", default_source
    return float(h.get("margin", 0.0) or 0.0), str(h.get("margin_unit", "abs")), hypothesis_source(h, run_name)


def dependence_margin(hypotheses: Iterable[dict[str, Any]], arm_a: str, arm_b: str, outcome: str = OUTCOME, *,
                      run_name: str | None = None) -> tuple[float, str, str]:
    """``(margin, unit, source)`` for the ``arm_b - arm_a`` dependence contrast: the first hypothesis declared for it
    (``arm_a -> arm_b`` with direction ``greater``, or ``arm_b -> arm_a`` with ``less``; kind ``superiority``), else
    the proposal's value (:data:`PROPOSAL_MARGIN`). ``run_name`` lets the source name the declaring config file."""
    h = declared_hypothesis(hypotheses, arm_a, arm_b, outcome, "superiority", "greater")
    return _declared_margin(h, run_name, PROPOSAL_MARGIN_SOURCE)


def _scale(paired: Paired, outcome: str, margin: float, unit: str) -> float:
    """The margin in outcome units: as given, or times the SD of the paired differences for ``unit == "sd"``."""
    if unit != "sd":
        return margin
    diffs = [b - a for a, b in paired.pairs(outcome)]
    return margin * statistics.stdev(diffs)


def _bound(m: float, sign: str = "") -> str:
    """How a reason names a bound: ``0`` at a zero margin (the wording before margins were read), else the margin."""
    return "0" if m == 0 else f"{'minus ' if sign == '-' else ''}the margin {m:.4f}"


def _margin_fields(margin: float, unit: str, source: str, hypothesis: Mapping[str, Any] | None) -> dict[str, Any]:
    return {"margin": margin, "margin_unit": unit, "margin_source": source,
            "margin_abs": None if unit == "sd" else margin,
            "hypothesis": None if hypothesis is None else hypothesis.get("name")}


def condition_dependence(paired: Paired, *, cid: str = "C1") -> dict[str, Any]:
    """C1: ``mem - amnesic`` above the preset margin, 95% interval above 0 (module docstring)."""
    h = declared_hypothesis(paired.hypotheses, paired.arm_a, paired.arm_b, OUTCOME, "superiority", "greater")
    margin, unit, source = _declared_margin(h, paired.run_name, PROPOSAL_MARGIN_SOURCE)
    res = _base(cid, "memory dependence", paired,
                f"{paired.arm_b} - {paired.arm_a} on {OUTCOME} > margin, and the 95% CI lies above 0")
    res.update(_margin_fields(margin, unit, source, h))
    s = _stats(paired, OUTCOME)
    res.update(s)
    why = _unusable(paired, s)
    if why:
        res["reason"] = why
        return res
    res["margin_abs"] = margin_abs = _scale(paired, OUTCOME, margin, unit)
    clears, above = s["estimate"] > margin_abs, _above(s)
    res["verdict"] = PASS if clears and above else FAIL
    res["reason"] = "; ".join([
        f"estimate {'is' if clears else 'is not'} above the margin {margin_abs:.4f}",
        f"the CI {'lies' if above else 'does not lie'} above 0",
    ])
    return res


def condition_index_builder(paired: Paired, *, cid: str = "C2") -> dict[str, Any]:
    """C2: ``index_builder - noop`` above the declared margin (0 by default), 95% interval above 0, power at least the
    declared ``min_power`` and never below :data:`MIN_POWER`."""
    h = declared_hypothesis(paired.hypotheses, paired.arm_a, paired.arm_b, OUTCOME, "superiority", "greater")
    margin, unit, source = _declared_margin(h, paired.run_name, PROPOSAL_E0_SOURCE)
    min_power = max(MIN_POWER, float((h or {}).get("min_power") or 0.0))
    res = _base(cid, "positive control", paired,
                f"{paired.arm_b} - {paired.arm_a} on {OUTCOME} > {'margin' if margin else '0'}, the 95% CI lies above 0, "
                f"and power >= {min_power}")
    s = _stats(paired, OUTCOME)
    res.update(s, power=None, power_method=POWER_METHOD, min_power=min_power)
    res.update(_margin_fields(margin, unit, source, h))
    why = _unusable(paired, s)
    if why:
        res["reason"] = why
        return res
    res["margin_abs"] = margin_abs = _scale(paired, OUTCOME, margin, unit)
    if s["d_z"] is not None:
        res["power"] = power_paired(s["d_z"], s["n"], ALPHA, POWER_METHOD)
    elif s["estimate"] != 0:  # every paired difference is the same non-zero number: the effect cannot be missed
        res["power"] = 1.0
    positive, above = s["estimate"] > margin_abs, _above(s)
    powered = res["power"] is not None and res["power"] >= min_power
    res["verdict"] = PASS if positive and above and powered else FAIL
    power_text = "undefined (no variation and no effect)" if res["power"] is None else f"{res['power']:.3f}"
    res["reason"] = "; ".join([
        f"estimate {'is' if positive else 'is not'} above {_bound(margin_abs)}",
        f"the CI {'lies' if above else 'does not lie'} above 0",
        f"power {power_text} {'reaches' if powered else 'is below'} {min_power} at n={s['n']}",
    ])
    return res


def condition_hoarder(paired: Paired, *, cid: str = "C3") -> dict[str, Any]:
    """C3: ``hoarder - noop`` below minus the declared margin (0 by default) with the 95% interval below 0."""
    h = declared_hypothesis(paired.hypotheses, paired.arm_a, paired.arm_b, OUTCOME, "superiority", "less")
    margin, unit, source = _declared_margin(h, paired.run_name, PROPOSAL_E0_SOURCE)
    res = _base(cid, "negative control", paired,
                f"{paired.arm_b} - {paired.arm_a} on {OUTCOME} < {'-margin' if margin else '0'}, and the 95% CI lies below 0")
    s = _stats(paired, OUTCOME)
    res.update(s)
    res.update(_margin_fields(margin, unit, source, h))
    why = _unusable(paired, s)
    if why:
        res["reason"] = why
        return res
    res["margin_abs"] = margin_abs = _scale(paired, OUTCOME, margin, unit)
    negative, below = s["estimate"] < -margin_abs, _below(s)
    res["verdict"] = PASS if negative and below else FAIL
    res["reason"] = "; ".join([f"estimate {'is' if negative else 'is not'} below {_bound(margin_abs, '-')}",
                               f"the CI {'lies' if below else 'does not lie'} below 0"])
    return res


def condition_decoy(paired: Paired, *, cid: str = "C4") -> dict[str, Any]:
    """C4: the decoy is not significantly above the no-op on ``mean_balance`` (beyond the declared non-superiority
    margin) and significantly above it on ``policy_chars`` (beyond the declared superiority margin); both 0 by
    default."""
    hb = declared_hypothesis(paired.hypotheses, paired.arm_a, paired.arm_b, OUTCOME, "non_superiority", "greater")
    ht = declared_hypothesis(paired.hypotheses, paired.arm_a, paired.arm_b, TEXT_METRIC, "superiority", "greater")
    mb, ub, sb = _declared_margin(hb, paired.run_name, PROPOSAL_E0_SOURCE)
    mt, ut, st = _declared_margin(ht, paired.run_name, PROPOSAL_E0_SOURCE)
    res = _base(cid, "metric check", paired,
                f"{paired.arm_b} - {paired.arm_a}: on {OUTCOME} the 95% CI lower bound is <= {'margin' if mb else '0'} "
                f"(not significantly above), and on {TEXT_METRIC} the 95% CI lies above {'margin' if mt else '0'}")
    bal, chars = _stats(paired, OUTCOME), _stats(paired, TEXT_METRIC)
    res.update(bal)
    res.update(_margin_fields(mb, ub, sb, hb))
    chars.update(_margin_fields(mt, ut, st, ht))
    res["text_metric"] = chars
    why = _unusable(paired, bal, chars)
    if why:
        res["reason"] = why
        return res
    res["margin_abs"] = bal_m = _scale(paired, OUTCOME, mb, ub)
    chars["margin_abs"] = text_m = _scale(paired, TEXT_METRIC, mt, ut)
    not_above, text_above = not _above(bal, bal_m), _above(chars, text_m)
    res["verdict"] = PASS if not_above and text_above else FAIL
    res["reason"] = "; ".join([
        f"{OUTCOME}: {'not ' if not_above else ''}significantly above" + ("" if bal_m == 0 else f" the margin {bal_m:.4f}"),
        f"{TEXT_METRIC}: {'' if text_above else 'not '}significantly above" + ("" if text_m == 0 else f" the margin {text_m:.1f}"),
    ])
    return res


# --- G1 ----------------------------------------------------------------------------------------------------------
def g1_outcome(paired: Paired) -> list[str]:
    """The outcome G1b reads: that of the declared superiority hypothesis ``b -> c`` (``greater``), else the runs'
    ``experiment.primary_outcome``, else :data:`G1_OUTCOME`."""
    h = declared_hypothesis(paired.hypotheses, paired.arm_a, paired.arm_b, None, "superiority", "greater")
    if h is not None and h.get("outcome"):
        return [str(h["outcome"])]
    return [paired.primary_outcome or G1_OUTCOME]


def condition_c_over_b(paired: Paired, *, cid: str = "G1b") -> dict[str, Any]:
    """G1b: ``c - b`` with ``d_z`` at least :data:`G1_MIN_DZ` on at least :data:`G1_PILOT_PAIRS` pairs (module
    docstring)."""
    (outcome,) = g1_outcome(paired)
    h = declared_hypothesis(paired.hypotheses, paired.arm_a, paired.arm_b, outcome, "superiority", "greater")
    if h is not None:
        source = hypothesis_source(h, paired.run_name)
    elif paired.primary_outcome:
        source = "experiment.primary_outcome (no b -> c hypothesis declared)"
    else:
        source = f"the proposal: MEMORY_EVOLUTION 6.3, F_L ({G1_OUTCOME})"
    res = _base(cid, "c over b", paired,
                f"{paired.arm_b} - {paired.arm_a} on {outcome}: d_z >= {G1_MIN_DZ} on at least {G1_PILOT_PAIRS} pairs")
    res.update(min_d_z=G1_MIN_DZ, min_pairs=G1_PILOT_PAIRS, hypothesis=None if h is None else h.get("name"),
               outcome_source=source)
    s = _stats(paired, outcome)
    res.update(s)
    res["below_primary_pairs"] = False  # G1 is read on the 10-seed pilot by design
    why = _unusable(paired, s)
    if not why and s["n"] < G1_PILOT_PAIRS:
        why = f"{s['n']} complete seed pair(s) with a value; G1 reads d_z on at least {G1_PILOT_PAIRS}"
    if why:
        res["reason"] = why
        return res
    if s["d_z"] is not None:
        effect, d_text = float(s["d_z"]), f"d_z {s['d_z']:.3f}"
    else:  # no variation: every difference is the same number
        effect = math.inf if s["estimate"] > 0 else -math.inf if s["estimate"] < 0 else 0.0
        d_text = "every difference is the same " + ("positive number" if s["estimate"] > 0 else "number, not positive,")
    ok = effect >= G1_MIN_DZ
    res["verdict"] = PASS if ok else FAIL
    res["reason"] = f"{d_text} {'reaches' if ok else 'is below'} {G1_MIN_DZ} at n={s['n']}"
    return res


def condition_valid_commands(exp_dir: str | Path, *, allow_incomplete: bool = False, cid: str = "G1a") -> dict[str, Any]:
    """G1a: in every arm with maintenance (on, or a value), the mean over complete runs of
    ``maintenance_valid_command_rate`` is at least :data:`MIN_VALID_RATE` (module docstring). Incomplete runs are
    refused unless ``allow_incomplete``, which leaves them out and lists them."""
    exp_dir = Path(exp_dir)
    res: dict[str, Any] = {
        "id": cid, "name": "valid commands", "experiment": exp_dir.name, "exp_dir": str(exp_dir),
        "rule": f"in every arm with maintenance, the mean over its runs of {VALID_COMMANDS} >= {MIN_VALID_RATE}",
        "outcome": VALID_COMMANDS, "threshold": MIN_VALID_RATE, "verdict": MISSING, "reason": None, "arms": {},
        "dropped": [], "refused": False, "below_primary_pairs": False, "runs": {}}
    try:
        found = discover_runs(exp_dir)
    except IncomparableRuns:
        found = {}
    if not found:
        res["reason"] = f"no runs under {exp_dir}"
        return res
    problems: list[str] = []
    complete: dict[str, dict[int, Path]] = {}
    maintenance: dict[str, bool] = {}
    for arm, by_seed in found.items():
        res["runs"][arm], complete[arm] = {}, {}
        for seed, rd in sorted(by_seed.items()):
            try:
                cfg = run_config_dict(rd)
            except (OSError, ValueError, sqlite3.Error) as e:  # a config that does not parse is a run nobody can trust
                res["reason"], res["refused"] = f"refused: a resolved config does not load ({e})", True
                return res
            if arm not in maintenance:
                maintenance[arm] = bool(resolve_path(cfg, "memory.maintenance.enabled"))
                res["experiment"] = str(resolve_path(cfg, "experiment.name") or exp_dir.name)
            state, why = run_state(rd, cfg)
            res["runs"][arm][str(seed)] = {**state, "complete": why is None, "reason": why}
            if why:
                problems.append(f"{arm} seed {seed}: {why}")
            else:
                complete[arm][seed] = rd
    if problems and not allow_incomplete:
        res["reason"] = f"refused: incomplete runs: {'; '.join(problems)} (--allow-incomplete leaves them out)"
        res["refused"] = True
        return res
    res["dropped"] = problems
    failing: list[str] = []
    unknown: list[str] = []
    for arm in found:
        values = {seed: _outcome(rd, VALID_COMMANDS) for seed, rd in sorted(complete[arm].items())}
        got = [v for v in values.values() if v is not None]
        applicable = maintenance[arm] or bool(got)
        mean = sum(got) / len(got) if got else None
        res["arms"][arm] = {"maintenance": maintenance[arm], "applicable": applicable, "n": len(got),
                            "without_value": len(values) - len(got), "mean": mean, "min": min(got) if got else None,
                            "values": {str(s): v for s, v in values.items()}}
        if not applicable:
            continue
        if mean is None:
            unknown.append(f"arm {arm!r} runs maintenance but no complete run of it issued a maintenance command")
        elif mean < MIN_VALID_RATE:
            failing.append(f"arm {arm!r} {mean:.3f} is below {MIN_VALID_RATE}")
    if not any(v["applicable"] for v in res["arms"].values()):
        res["reason"] = "no arm runs maintenance, so there is no command to be valid"
        return res
    if failing:
        res["verdict"], res["reason"] = FAIL, "; ".join(failing + unknown)
    elif unknown:
        res["reason"] = "; ".join(unknown)
    else:
        res["verdict"], res["reason"] = PASS, f"every arm with maintenance reaches {MIN_VALID_RATE}"
    return res


# --- G0 and G1 ---------------------------------------------------------------------------------------------------
def overall(verdicts: Iterable[str]) -> str:
    """``FAIL`` when any condition fails, else ``MISSING`` when any is missing, else ``PASS`` (also for none)."""
    vs = list(verdicts)
    if FAIL in vs:
        return FAIL
    if MISSING in vs:
        return MISSING
    return PASS


def _gate(conditions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    verdict = overall(c["verdict"] for c in conditions)
    return {"verdict": verdict, "exit_code": EXIT_CODES[verdict],
            "by_condition": {c["label"]: c["verdict"] for c in conditions}}


def evaluate_g0(runs_root: str | Path, *, experiments: Sequence[str] = (DEPENDENCE, E0), live_root: str | Path | None = None,
                allow_incomplete: bool = False, g1_dir: str | Path | None = None) -> dict[str, Any]:
    """Evaluate the G0 conditions from ``runs_root/<experiment>/<arm>/seed_<n>/``.

    ``experiments`` names the dependence experiment and the E0 experiment. With ``live_root``, C1 is evaluated on each
    of :data:`LIVE_DEPENDENCE` under it instead of on ``runs_root/<experiments[0]>``. With ``g1_dir`` (the E1
    experiment directory) G1a and G1b are evaluated too, ``result["g1"]`` combines them with the C1 lines, and
    ``result["exit_code"]`` covers every condition; without it ``result["g1"]`` is None and the exit code is G0's."""
    if len(experiments) != 2:
        raise ValueError("experiments must name the dependence experiment and the E0 experiment")
    runs_root = Path(runs_root)
    dep_name, e0_name = (str(e) for e in experiments)
    conditions: list[dict[str, Any]] = []
    if live_root is None:
        dep_dirs = [runs_root / dep_name]
    else:
        dep_dirs = [Path(live_root) / name for name in LIVE_DEPENDENCE]
    for exp_dir in dep_dirs:
        p = paired_outcomes(exp_dir, "amnesic", "mem", [OUTCOME], allow_incomplete=allow_incomplete)
        res = condition_dependence(p)
        res["label"] = "C1" if live_root is None else f"C1[{exp_dir.name}]"
        conditions.append(res)
    e0_dir = runs_root / e0_name
    for fn, arm, outcomes in ((condition_index_builder, "index_builder", [OUTCOME]),
                              (condition_hoarder, "hoarder", [OUTCOME]),
                              (condition_decoy, "decoy", [OUTCOME, TEXT_METRIC])):
        res = fn(paired_outcomes(e0_dir, "noop", arm, outcomes, allow_incomplete=allow_incomplete))
        res["label"] = res["id"]
        conditions.append(res)
    g0 = _gate(conditions)
    g1: dict[str, Any] | None = None
    if g1_dir is not None:
        valid = condition_valid_commands(g1_dir, allow_incomplete=allow_incomplete)
        c_over_b = condition_c_over_b(paired_outcomes(g1_dir, "b", "c", g1_outcome, allow_incomplete=allow_incomplete))
        for res in (valid, c_over_b):
            res["label"] = res["id"]
        dependence = [c for c in conditions if c["id"] == "C1"]
        conditions += [valid, c_over_b]
        g1 = _gate(dependence + [valid, c_over_b])
    every = _gate(conditions)
    return {
        "mode": "scripted" if live_root is None else "live",
        "runs_root": str(runs_root), "live_root": None if live_root is None else str(live_root),
        "experiments": {"dependence": [str(d) for d in dep_dirs], "e0": str(e0_dir),
                        "e1": None if g1_dir is None else str(g1_dir)},
        "allow_incomplete": allow_incomplete, "n_boot": N_BOOT, "boot_seed": BOOT_SEED, "alpha": ALPHA,
        "power_method": POWER_METHOD, "min_power": MIN_POWER, "primary_pairs": PRIMARY_PAIRS,
        "min_valid_rate": MIN_VALID_RATE, "g1_min_d_z": G1_MIN_DZ, "g1_pilot_pairs": G1_PILOT_PAIRS,
        "conditions": conditions,
        "g0": g0, "g1": g1,
        "exit_code": every["exit_code"] if g1 is not None else g0["exit_code"],
        "failing": [c["label"] for c in conditions if c["verdict"] == FAIL],
        "missing": [c["label"] for c in conditions if c["verdict"] == MISSING],
    }


# --- rendering ---------------------------------------------------------------------------------------------------
def _f(v: float | None, nd: int = 4) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{nd}f}"


def _ci(s: dict[str, Any], nd: int = 4) -> str:
    lo, hi = s["ci95"]
    return f"[{_f(lo, nd)}, {_f(hi, nd)}]"


def _margin(c: dict[str, Any], nd: int = 4) -> str:
    unit = f" SD = {_f(c['margin_abs'], nd)}" if c["margin_unit"] == "sd" else ""
    return f"margin {_f(c['margin'], nd)}{unit} from {c['margin_source']}"


def _valid_line(c: dict[str, Any]) -> str:
    head = f"{c['label']} {c['verdict']:<7} {c['name']} ({c['experiment']})"
    if not c["arms"]:
        return f"{head}: {c['reason']}"
    parts = []
    for arm, a in c["arms"].items():
        if not a["applicable"]:
            parts.append(f"{arm} no maintenance")
        elif a["mean"] is None:
            parts.append(f"{arm} no command")
        else:
            parts.append(f"{arm} {_f(a['mean'], 3)} (min {_f(a['min'], 3)}, n={a['n']})")
    body = f"{c['outcome']} mean over runs: " + ", ".join(parts) + f"; needs >= {c['threshold']}"
    if c["dropped"]:
        body += f"; {len(c['dropped'])} incomplete run(s) left out"
    return f"{head}: {body} -> {c['reason']}"


def _line(c: dict[str, Any]) -> str:
    if c["id"] == "G1a":
        return _valid_line(c)
    head = f"{c['label']} {c['verdict']:<7} {c['name']} ({c['experiment']}): {c['arm_b']} - {c['arm_a']}"
    if c["verdict"] == MISSING:
        return f"{head}: {c['reason']}"
    body = f"{c['outcome']} {_f(c['estimate'])} CI95 {_ci(c)} n={c['n']} (+{c['positive']}/-{c['negative']}/={c['ties']})"
    if c["id"] == "C1":
        body += f"; {_margin(c)}"
    elif c["id"] == "C2":
        body += f"; power {_f(c['power'], 3)} ({c['power_method']} approximation, d_z {_f(c['d_z'], 3)}); {_margin(c)}"
    elif c["id"] == "C3":
        body += f"; {_margin(c)}"
    elif c["id"] == "C4":
        t = c["text_metric"]
        body += (f"; {_margin(c)}; {TEXT_METRIC} {_f(t['estimate'], 1)} CI95 {_ci(t, 1)} n={t['n']}, "
                 f"{_margin(t, 1)}")
    elif c["id"] == "G1b":
        body += f"; d_z {_f(c['d_z'], 3)}; outcome from {c['outcome_source']}"
    if c["dropped"]:
        body += f"; {len(c['dropped'])} incomplete or unpaired run(s) dropped"
    if c["below_primary_pairs"]:
        body += f"; note: fewer than the {PRIMARY_PAIRS} pairs 6.11 asks for, so the interval is rough"
    return f"{head}: {body} -> {c['reason']}"


def _gate_line(name: str, gate: dict[str, Any], scope: str = "") -> str:
    parts = ", ".join(f"{k} {v}" for k, v in gate["by_condition"].items())
    return f"{name} {gate['verdict']}{scope}: {parts}"


def render(result: dict[str, Any]) -> str:
    """One line per condition, then the G0 line; with G1, the G1 line and a last line with the exit status that
    names every condition that failed or is missing."""
    lines = [_line(c) for c in result["conditions"]]
    g0, g1 = result["g0"], result.get("g1")
    scope = " (live dependence; E0 controls from the scripted runs)" if result["mode"] == "live" else ""
    if g1 is None:
        lines.append(f"{_gate_line('G0', g0, scope)} (exit {g0['exit_code']})")
        return "\n".join(lines)
    lines.append(_gate_line("G0", g0, scope))
    dep = " (live dependence)" if result["mode"] == "live" else " (dependence from the scripted runs; 10.4 reads it live)"
    lines.append(_gate_line("G1", g1, dep))
    named = [f"{v} {', '.join(result[k])}" for k, v in (("failing", FAIL), ("missing", MISSING)) if result[k]]
    lines.append(f"exit {result['exit_code']}" + (f": {'; '.join(named)}" if named else ": every condition passes"))
    return "\n".join(lines)


def notes(result: dict[str, Any]) -> list[str]:
    """The runs each condition dropped under ``allow_incomplete``, for stderr."""
    return [f"{c['label']} dropped {d}" for c in result["conditions"] for d in c["dropped"]]
