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
pairs it spans little more than the two differences), and the live stages run 16 and 8 pairs by default.

Live mode (``live_root``): C1 is evaluated instead on each live paired experiment of ``scripts/live_claude.sh``
(:data:`LIVE_DEPENDENCE`: the original world and the vein world), by this same code; every one of them must pass.
The E0 controls (C2 to C4) are scripted by design and are still read from the runs root.

Nothing here writes to a run, calls a provider or draws from a shared random stream.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    "LIVE_DEPENDENCE",
    "MIN_POWER",
    "PRIMARY_PAIRS",
    "N_BOOT",
    "OUTCOME",
    "POWER_METHOD",
    "PROPOSAL_MARGIN",
    "Paired",
    "condition_decoy",
    "condition_dependence",
    "condition_hoarder",
    "condition_index_builder",
    "dependence_margin",
    "evaluate_g0",
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
EXIT_CODES = {"PASS": 0, "FAIL": 1, "MISSING": 2}
PASS, FAIL, MISSING = "PASS", "FAIL", "MISSING"


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
    the runs left out under ``allow_incomplete``. ``hypotheses`` is ``experiment.hypotheses`` of the configs."""

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


def paired_outcomes(exp_dir: str | Path, arm_a: str, arm_b: str, outcomes: Sequence[str], *,
                    allow_incomplete: bool = False) -> Paired:
    """Load the runs of ``arm_a`` and ``arm_b`` under ``exp_dir`` (``<arm>/seed_<n>/``) and their ``outcomes``, paired
    by seed. Never raises for a missing, refused or incomplete experiment: :attr:`Paired.missing` says why instead."""
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
    for path in outcomes:
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


def dependence_margin(hypotheses: Iterable[dict[str, Any]], arm_a: str, arm_b: str, outcome: str = OUTCOME
                      ) -> tuple[float, str, str]:
    """``(margin, unit, source)`` for the ``arm_b - arm_a`` dependence contrast: the first hypothesis declared for it
    (``arm_a -> arm_b`` with direction ``greater``, or ``arm_b -> arm_a`` with ``less``; kind ``superiority``), else
    the proposal's value (:data:`PROPOSAL_MARGIN`)."""
    for h in hypotheses:
        if h.get("outcome") != outcome or h.get("kind", "superiority") != "superiority":
            continue
        pair, direction = (h.get("arm_a"), h.get("arm_b")), h.get("direction", "greater")
        if (pair == (arm_a, arm_b) and direction == "greater") or (pair == (arm_b, arm_a) and direction == "less"):
            return float(h.get("margin", 0.0) or 0.0), str(h.get("margin_unit", "abs")), \
                f"experiment.hypotheses {h.get('name')!r}"
    return PROPOSAL_MARGIN, "abs", PROPOSAL_MARGIN_SOURCE


def condition_dependence(paired: Paired, *, cid: str = "C1") -> dict[str, Any]:
    """C1: ``mem - amnesic`` above the preset margin, 95% interval above 0 (module docstring)."""
    margin, unit, source = dependence_margin(paired.hypotheses, paired.arm_a, paired.arm_b)
    res = _base(cid, "memory dependence", paired,
                f"{paired.arm_b} - {paired.arm_a} on {OUTCOME} > margin, and the 95% CI lies above 0")
    res.update(margin=margin, margin_unit=unit, margin_source=source, margin_abs=None if unit == "sd" else margin)
    s = _stats(paired, OUTCOME)
    res.update(s)
    why = _unusable(paired, s)
    if why:
        res["reason"] = why
        return res
    if unit == "sd":
        diffs = [b - a for a, b in paired.pairs(OUTCOME)]
        res["margin_abs"] = margin * statistics.stdev(diffs)
    margin_abs = float(res["margin_abs"])
    clears, above = s["estimate"] > margin_abs, _above(s)
    res["verdict"] = PASS if clears and above else FAIL
    res["reason"] = "; ".join([
        f"estimate {'is' if clears else 'is not'} above the margin {margin_abs:.4f}",
        f"the CI {'lies' if above else 'does not lie'} above 0",
    ])
    return res


def condition_index_builder(paired: Paired, *, cid: str = "C2") -> dict[str, Any]:
    """C2: ``index_builder - noop`` above 0, 95% interval above 0, power at least :data:`MIN_POWER`."""
    res = _base(cid, "positive control", paired,
                f"{paired.arm_b} - {paired.arm_a} on {OUTCOME} > 0, the 95% CI lies above 0, and power >= {MIN_POWER}")
    s = _stats(paired, OUTCOME)
    res.update(s, power=None, power_method=POWER_METHOD, min_power=MIN_POWER)
    why = _unusable(paired, s)
    if why:
        res["reason"] = why
        return res
    if s["d_z"] is not None:
        res["power"] = power_paired(s["d_z"], s["n"], ALPHA, POWER_METHOD)
    elif s["estimate"] != 0:  # every paired difference is the same non-zero number: the effect cannot be missed
        res["power"] = 1.0
    positive, above = s["estimate"] > 0, _above(s)
    powered = res["power"] is not None and res["power"] >= MIN_POWER
    res["verdict"] = PASS if positive and above and powered else FAIL
    power_text = "undefined (no variation and no effect)" if res["power"] is None else f"{res['power']:.3f}"
    res["reason"] = "; ".join([
        f"estimate {'is' if positive else 'is not'} above 0",
        f"the CI {'lies' if above else 'does not lie'} above 0",
        f"power {power_text} {'reaches' if powered else 'is below'} {MIN_POWER} at n={s['n']}",
    ])
    return res


def condition_hoarder(paired: Paired, *, cid: str = "C3") -> dict[str, Any]:
    """C3: ``hoarder - noop`` below 0 with the 95% interval below 0."""
    res = _base(cid, "negative control", paired, f"{paired.arm_b} - {paired.arm_a} on {OUTCOME} < 0, and the 95% CI lies below 0")
    s = _stats(paired, OUTCOME)
    res.update(s)
    why = _unusable(paired, s)
    if why:
        res["reason"] = why
        return res
    negative, below = s["estimate"] < 0, _below(s)
    res["verdict"] = PASS if negative and below else FAIL
    res["reason"] = "; ".join([f"estimate {'is' if negative else 'is not'} below 0",
                               f"the CI {'lies' if below else 'does not lie'} below 0"])
    return res


def condition_decoy(paired: Paired, *, cid: str = "C4") -> dict[str, Any]:
    """C4: the decoy is not significantly above the no-op on ``mean_balance`` and significantly above it on
    ``policy_chars``."""
    res = _base(cid, "metric check", paired,
                f"{paired.arm_b} - {paired.arm_a}: on {OUTCOME} the 95% CI lower bound is <= 0 (not significantly above), "
                f"and on {TEXT_METRIC} the 95% CI lies above 0")
    bal, chars = _stats(paired, OUTCOME), _stats(paired, TEXT_METRIC)
    res.update(bal)
    res["text_metric"] = chars
    why = _unusable(paired, bal, chars)
    if why:
        res["reason"] = why
        return res
    not_above, text_above = not _above(bal), _above(chars)
    res["verdict"] = PASS if not_above and text_above else FAIL
    res["reason"] = "; ".join([
        f"{OUTCOME}: {'not ' if not_above else ''}significantly above",
        f"{TEXT_METRIC}: {'' if text_above else 'not '}significantly above",
    ])
    return res


# --- G0 -------------------------------------------------------------------------------------------------------
def overall(verdicts: Iterable[str]) -> str:
    """``FAIL`` when any condition fails, else ``MISSING`` when any is missing, else ``PASS`` (also for none)."""
    vs = list(verdicts)
    if FAIL in vs:
        return FAIL
    if MISSING in vs:
        return MISSING
    return PASS


def evaluate_g0(runs_root: str | Path, *, experiments: Sequence[str] = (DEPENDENCE, E0), live_root: str | Path | None = None,
                allow_incomplete: bool = False) -> dict[str, Any]:
    """Evaluate the G0 conditions from ``runs_root/<experiment>/<arm>/seed_<n>/``.

    ``experiments`` names the dependence experiment and the E0 experiment. With ``live_root``, C1 is evaluated on each
    of :data:`LIVE_DEPENDENCE` under it instead of on ``runs_root/<experiments[0]>``."""
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
    verdict = overall(c["verdict"] for c in conditions)
    return {
        "mode": "scripted" if live_root is None else "live",
        "runs_root": str(runs_root), "live_root": None if live_root is None else str(live_root),
        "experiments": {"dependence": [str(d) for d in dep_dirs], "e0": str(e0_dir)},
        "allow_incomplete": allow_incomplete, "n_boot": N_BOOT, "boot_seed": BOOT_SEED, "alpha": ALPHA,
        "power_method": POWER_METHOD, "min_power": MIN_POWER, "primary_pairs": PRIMARY_PAIRS,
        "conditions": conditions,
        "g0": {"verdict": verdict, "exit_code": EXIT_CODES[verdict],
               "by_condition": {c["label"]: c["verdict"] for c in conditions}},
    }


# --- rendering ---------------------------------------------------------------------------------------------------
def _f(v: float | None, nd: int = 4) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{nd}f}"


def _ci(s: dict[str, Any]) -> str:
    lo, hi = s["ci95"]
    return f"[{_f(lo)}, {_f(hi)}]"


def _line(c: dict[str, Any]) -> str:
    head = f"{c['label']} {c['verdict']:<7} {c['name']} ({c['experiment']}): {c['arm_b']} - {c['arm_a']}"
    if c["verdict"] == MISSING:
        return f"{head}: {c['reason']}"
    body = f"{OUTCOME} {_f(c['estimate'])} CI95 {_ci(c)} n={c['n']} (+{c['positive']}/-{c['negative']}/={c['ties']})"
    if c["id"] == "C1":
        unit = f" SD = {_f(c['margin_abs'])}" if c["margin_unit"] == "sd" else ""
        body += f"; margin {_f(c['margin'])}{unit} from {c['margin_source']}"
    elif c["id"] == "C2":
        body += f"; power {_f(c['power'], 3)} ({c['power_method']} approximation, d_z {_f(c['d_z'], 3)})"
    elif c["id"] == "C4":
        t = c["text_metric"]
        body += f"; {TEXT_METRIC} {_f(t['estimate'], 1)} CI95 [{_f(t['ci95'][0], 1)}, {_f(t['ci95'][1], 1)}] n={t['n']}"
    if c["dropped"]:
        body += f"; {len(c['dropped'])} incomplete or unpaired run(s) dropped"
    if c["below_primary_pairs"]:
        body += f"; note: fewer than the {PRIMARY_PAIRS} pairs 6.11 asks for, so the interval is rough"
    return f"{head}: {body} -> {c['reason']}"


def render(result: dict[str, Any]) -> str:
    """One line per condition, then the G0 line."""
    lines = [_line(c) for c in result["conditions"]]
    g0 = result["g0"]
    parts = ", ".join(f"{k} {v}" for k, v in g0["by_condition"].items())
    scope = " (live dependence; E0 controls from the scripted runs)" if result["mode"] == "live" else ""
    lines.append(f"G0 {g0['verdict']}{scope}: {parts} (exit {g0['exit_code']})")
    return "\n".join(lines)


def notes(result: dict[str, Any]) -> list[str]:
    """The runs each condition dropped under ``allow_incomplete``, for stderr."""
    return [f"{c['label']} dropped {d}" for c in result["conditions"] for d in c["dropped"]]
