"""Arm comparison for the research harness (DESIGN §17): ``void compare --exp <name>``.

An experiment directory holds one subdirectory per arm, each holding ``seed_<n>/`` runs (or a single
run directly). Before any statistics the arms are checked for comparability and the comparison is
REFUSED (:class:`IncomparableRuns`) when

* the seed sets differ between arms, or runs within an arm differ outside ``run.seed``;
* ``entropy.degeneration.mode`` is not ``observe`` in every arm;
* for ``exp_tiers`` / ``exp_scarcity`` / ``exp_ladder`` / ``exp_mem`` (``exp_mem_e0``, ``exp_mem_dep``, ...; not the
  ``exp_memory`` graph-vs-vector pair), ``economy.world_pricing`` is not ``equalized``;
* for ``exp_ladder`` / ``exp_gate``, ``population.tier_mutation_prob`` is not 0 in every arm;
* the resolved configs differ at any path outside the union of the arms'
  ``experiment.independent_variables`` (``run.name`` and ``experiment.arm`` are always allowed; for the ``exp_mem``
  family this is what refuses a difference in the exam or the maintenance step that is not a declared variable);
* for ``exp_tiers`` / ``exp_ladder``, the rostered tiers differ in declared thresholds, ``max_tokens`` or ``effort``, or
  the arms differ in ``entropy.drain``;
* any run is incomplete: its ``run.status`` is not ``completed`` (``capped_total``, ``extinct``, ``inconsistent``,
  ``failed``, or still ``running`` after a crash), its last tick row (``metrics.jsonl``, else the ``metrics`` table)
  is below ``run.days * run.ticks_per_day``, a metrics line is cut short, or it has no metrics at all.
  With ``allow_incomplete=True`` (``scripts/compare.py --allow-incomplete``) such runs are dropped instead: every seed
  pair that touches one is left out of the statistics, the run is listed under ``dropped_incomplete`` and in the
  warnings, and the comparison is still refused when a pair of arms has no complete seed in common.

The result always carries ``runs: {arm: {seed: {status, last_tick}}}`` for every discovered run.

A family name such as ``exp_mem`` covers an experiment name that starts with it when the next character, if any,
is not a letter, so ``exp_mem`` covers ``exp_mem_e0`` and ``exp_mem_dep_claude`` but not ``exp_memory``.

Configs are compared as loaded (``load_config(path).model_dump(mode="json")``), not as raw YAML: the stored file
leaves out fields added later while they hold their defaults (``world.veins``, ``world.fallow_ticks``,
``memory.practices``, ``memory.selection``, ``memory.policy.mutate_at_birth``, ``experiment.hypotheses``), so an arm
that turns such a block on is compared with one that leaves it at its defaults path by path, and runs stored before
and after a field existed compare. A resolved config that no longer loads is refused.

Arms are paired by seed. For the primary outcome (``experiment.primary_outcome`` of the first arm unless
overridden) each pair of arms gets n, per-arm means, the paired mean delta (b - a), Cohen's d_z, a paired
bootstrap 95% CI (2000 resamples, ``random.Random(0)``) and an exact two-sided sign test. Secondaries get
the same statistics flagged ``exploratory``. Daily curves are reported, not tested.

**Pre-registered hypotheses** (MEMORY_EVOLUTION 5.2, 6.2, 6.11; ``experiment.hypotheses``) are read from the first
arm's config; the comparison is refused when the arms disagree on them. Each is decided on its outcome's per-seed
differences ``delta = arm_b - arm_a`` over the seeds both arms completed (``alpha`` 0.05); the margin is absolute or,
with ``margin_unit: sd``, in standard deviations of those paired differences:

* ``superiority`` passes when the two-sided 95% paired bootstrap CI clears the margin on the stated side (``greater``:
  lower bound above ``margin``; ``less``: upper bound below ``-margin``) and the margin-shifted paired t-test agrees
  (:func:`void.research.stats.superiority`: ``p``, then ``p_holm`` for a primary hypothesis, below alpha); with
  ``min_power`` set, ``power`` (:func:`~void.research.stats.power_paired` at the observed standardized effect beyond
  the margin in the stated direction, which is d_z itself at margin 0) must also reach it.
* ``non_superiority`` passes when superiority does not hold (:func:`~void.research.stats.non_superiority`, decided
  without the Holm adjustment so the correction never makes a "does not clear the bar" claim easier); ``p`` is
  superiority's p and ``power`` the power to detect an effect as large as the margin.
* ``equivalence`` passes when the two one-sided t-tests (:func:`~void.research.stats.tost_paired`) give ``p`` (``p_holm``
  when primary) below alpha; ``ci`` is then the 90% t interval and ``power`` the TOST power at a zero effect
  (:func:`~void.research.stats.power_tost`).
* ``interaction`` (H4) is decided on per-seed differences of paired differences, below.
* Holm's step-down adjustment runs across the hypotheses with ``primary: true``; the others are exploratory
  (``p_holm`` None). A hypothesis with fewer than two paired seeds, or naming an arm without runs, fails.

**Interaction hypotheses** (``kind: interaction``; :class:`void.config.HypothesisConfig`) come in two layouts:

* four arms (a between-arm 2x2): one contrast per seed, ``(arm_d - arm_c) - (arm_b - arm_a)`` of ``outcome``, over
  the seeds all four arms completed;
* arm x within-run levels (H4: the rungs of one world): for each level L of ``levels`` other than ``reference`` (the
  first level unless set), one contrast per seed, ``(arm_b - arm_a)`` of ``outcome_template`` at L minus the same at
  the reference, over the seeds both arms completed.

Each contrast gets its estimate (the mean over seeds), the two-sided ``1 - alpha`` paired percentile bootstrap CI (the
one interval method of this module: 2000 resamples, ``random.Random(0)``), the margin-shifted paired t p-value of
:func:`~void.research.stats.contrast_test` (``two_sided`` tests the side of the observed mean, which gives the
two-sided p) and the power at its observed effect beyond the margin. The k contrasts with at least two seeds are
Holm-adjusted among themselves (each contrast's ``p_holm``). A contrast *clears* when its CI clears the margin on the
declared side (``two_sided``: on either side), its within-hypothesis ``p_holm`` is below alpha and, with
``min_power``, its power reaches it.

How a multi-contrast hypothesis enters the Holm family across primary hypotheses: as ONE member, with ``p`` equal to
its smallest within-hypothesis Holm p, ``min(1, k * smallest contrast p)``, the Bonferroni p of the global null "every
interaction contrast is 0" (H4 is falsified when every interaction CI includes 0). Holm across the primary hypotheses
then adjusts that p like any other hypothesis's into the row's ``p_holm``. An interaction hypothesis PASSES when at
least one contrast clears and, when it is primary, its family ``p_holm`` is below alpha. Its row's ``n, delta, ci,
d_z, power`` are the deciding contrast's (the clearing contrast with the smallest within-hypothesis ``p_holm``, else
the contrast with the smallest one); ``contrasts`` holds every contrast with its per-seed values, and :func:`render`
prints a line per contrast under the hypothesis's PASS or FAIL line when the layout is by level.

``result["hypotheses"]`` holds one row per hypothesis (``name, kind, outcome, n, delta, ci, d_z, power, p, p_holm,
passed, reason`` and the inputs); :func:`render` prints a PASS or FAIL line for each.

**2x2 factorial** (DESIGN §17 ``exp_press``): with exactly four arms whose declared independent variables include
exactly two that vary, each over exactly two values, crossed so every combination is one arm, ``result["factorial"]``
holds the main effects and the interaction of the primary outcome per seed
(:func:`~void.research.stats.interaction_2x2`; level 0 is the lower value, ``false`` before ``true``). Otherwise None.
It is descriptive (no verdict, no Holm), whether or not an interaction hypothesis is declared; only a declared
interaction hypothesis is decided.

**Per-tier difference-in-differences** (PLAN §8 Q2, scarcity x capability tier): when every arm rosters the same two
or more tiers, each bare tier-resolvable primary or secondary outcome (:data:`TIER_OUTCOMES`, e.g.
``false_claim_rate``) is resolved per tier (``false_claim_rate.<tier>``) and ``result["tier_did"]`` reports, for each
pair of arms, the per-tier paired deltas and, for each pair of tiers, the per-seed difference of those deltas
(``delta[tier_b] - delta[tier_a]``) with a paired bootstrap 95% CI. Exploratory. Otherwise None.
"""

from __future__ import annotations

import json
import math
import random
import re
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from itertools import combinations
from pathlib import Path
from typing import Any

import yaml

from void.config import HypothesisConfig, VoidConfig, load_config
from void.research import stats
from void.research.analysis import (
    CONFIG_FILE,
    DAILY,
    DB_FILE,
    METRICS_FILE,
    connect,
    end_of_run_outcome,
    load_metrics,
    resolve_path,
)

__all__ = [
    "ALPHA",
    "ALWAYS_ALLOWED",
    "TIER_OUTCOMES",
    "IncomparableRuns",
    "compare",
    "config_diff",
    "covered_by",
    "decide_hypotheses",
    "discover_runs",
    "evaluate_hypothesis",
    "evaluate_interaction",
    "factorial_design",
    "in_family",
    "paired_stats",
    "render",
    "render_hypotheses",
    "run_state",
    "sign_test_p",
]

ALWAYS_ALLOWED = ("run.name", "experiment.arm")
EQUALIZED_EXPERIMENTS = ("exp_tiers", "exp_scarcity", "exp_ladder", "exp_mem")
MUTATION_FREE_EXPERIMENTS = ("exp_ladder", "exp_gate")  # DESIGN §17: no tier mutation in these
TIER_INVARIANT_EXPERIMENTS = ("exp_tiers", "exp_ladder")
TIER_INVARIANTS = ("collapse_temperature", "max_temperature", "entropy_budget_max", "max_tokens", "effort")
N_BOOT = 2000
BOOT_SEED = 0
JACCARD_DEGENERATE = 0.8
ALPHA = 0.05
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_RETRIEVED_KEYS = ("retrieved_titles", "retrieved", "retrieved_ids", "memories", "entry_ids")
# computed outcomes (void.research.analysis.COMPUTED) whose argument is a tier name: ``false_claim_rate.<tier>``
TIER_OUTCOMES = (
    "false_claim_rate", "invalid_action_rate", "stale_rate", "perseveration_rate", "action_regret_mean",
    "text_coherence_mean", "p_chosen_mean", "action_entropy_w8_mean", "recovered_collapse_stress",
    "exam_score", "exam_accuracy", "exam_abstain_rate",
)


class IncomparableRuns(RuntimeError):
    """The runs under an experiment directory are not a fair comparison."""


def in_family(name: str, families: str | tuple[str, ...]) -> bool:
    """``name`` belongs to one of ``families`` when it starts with the family name and the next character, if any, is
    not a letter (``exp_mem`` covers ``exp_mem_e0`` but not ``exp_memory``; ``exp_tiers`` covers ``exp_tiers0``)."""
    for fam in (families,) if isinstance(families, str) else families:
        if name.startswith(fam) and not name[len(fam):len(fam) + 1].isalpha():
            return True
    return False


# --- config diff --------------------------------------------------------------------------------------
def config_diff(a: Any, b: Any, prefix: str = "") -> list[str]:
    """Dotted paths at which two resolved configs differ (lists are compared by index; a length
    difference is reported at the list's own path)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for k in sorted(set(a) | set(b), key=str):
            p = f"{prefix}.{k}" if prefix else str(k)
            if k not in a or k not in b:
                out.append(p)
            else:
                out.extend(config_diff(a[k], b[k], p))
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [prefix or "<root>"]
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out.extend(config_diff(x, y, f"{prefix}.{i}" if prefix else str(i)))
        return out
    return [] if a == b else [prefix or "<root>"]


def covered_by(path: str, independent_variables: list[str]) -> bool:
    """A differing path is allowed when it equals an independent variable or lies under one."""
    return any(path == iv or path.startswith(iv + ".") for iv in independent_variables)


_PLACED_RE = re.compile(r"^agents\.(\d+)\.tier$")


def _seed_placed(path: str, a: dict[str, Any], b: dict[str, Any]) -> bool:
    """``agents.<i>.tier`` may differ between seeds of one arm when both runs placed that agent from
    ``intelligence.ladder`` (the placement is a function of the seed, like everything else the seed drives)."""
    m = _PLACED_RE.match(path)
    if not m:
        return False
    i = int(m.group(1))
    try:
        return bool(a["agents"][i].get("placed")) and bool(b["agents"][i].get("placed"))
    except (KeyError, IndexError, TypeError, AttributeError):
        return False


# --- discovery -----------------------------------------------------------------------------------------
def _load_cfg(run_dir: Path) -> dict[str, Any]:
    """The run's resolved config as loaded, every default filled in (see the module docstring):
    ``load_config(path).model_dump(mode="json")``. A resolved config never ``extends`` another file, so it is parsed
    with libyaml when available and validated directly, which is what ``load_config`` does for such a file, about
    seven times faster on a large experiment."""
    path = run_dir / CONFIG_FILE
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_YAML_LOADER) or {}
        if not isinstance(data, dict):
            raise IncomparableRuns(f"{path} does not hold a mapping")
        cfg = load_config(path) if "extends" in data else VoidConfig.model_validate(data)
    except (ValueError, yaml.YAMLError) as e:  # pydantic's ValidationError is a ValueError
        raise IncomparableRuns(f"{path} does not load as a config: {e}") from e
    return cfg.model_dump(mode="json")


def discover_runs(exp_dir: str | Path) -> dict[str, dict[int, Path]]:
    """``{arm: {seed: run_dir}}`` from ``<exp_dir>/<arm>/seed_<n>/`` (or ``<exp_dir>/<arm>/`` single runs)."""
    exp_dir = Path(exp_dir)
    if not exp_dir.is_dir():
        raise IncomparableRuns(f"{exp_dir} is not a directory")
    arms: dict[str, dict[int, Path]] = {}
    for arm_dir in sorted(p for p in exp_dir.iterdir() if p.is_dir()):
        runs: dict[int, Path] = {}
        for sd in sorted(arm_dir.glob("seed_*")):
            if not sd.is_dir() or not (sd / CONFIG_FILE).is_file():
                continue
            try:
                runs[int(sd.name.split("_", 1)[1])] = sd
            except ValueError:
                continue
        if not runs and (arm_dir / CONFIG_FILE).is_file():
            seed = resolve_path(_load_cfg(arm_dir), "run.seed")
            runs[int(seed if seed is not None else 0)] = arm_dir
        if runs:
            arms[arm_dir.name] = runs
    return arms


# --- completeness ------------------------------------------------------------------------------------------
def _run_status(run_dir: Path) -> str | None:
    """``run.status`` from ``world.db``; None when there is no database or no run row (a run that crashed early)."""
    if not (run_dir / DB_FILE).is_file():
        return None
    try:
        with connect(run_dir) as db:
            row = db.execute("SELECT status FROM run ORDER BY created_at DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row["status"])


def _metrics_tail(run_dir: Path) -> tuple[int | None, bool]:
    """``(last tick row, intact)`` from ``metrics.jsonl``, else the ``metrics`` table: the source
    :func:`~void.research.analysis.load_metrics` reads. ``intact`` is False when a line does not parse (a write cut
    short by a crash); the tick is then the last one that parses. The tick is None without any tick row."""
    lines: list[str] = []
    path = run_dir / METRICS_FILE
    if path.is_file():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    elif (run_dir / DB_FILE).is_file():
        try:
            with connect(run_dir) as db:
                lines = [str(r["json"]) for r in db.execute("SELECT json FROM metrics")]
        except sqlite3.Error:
            return None, False
    last: int | None = None
    intact = True
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            intact = False
            continue
        if not isinstance(row, dict) or row.get("kind") == "day":
            continue
        tick = row.get("tick")
        if isinstance(tick, int) and not isinstance(tick, bool):
            last = tick if last is None else max(last, tick)
    return last, intact


def run_state(run_dir: str | Path, cfg: dict[str, Any] | None = None) -> tuple[dict[str, Any], str | None]:
    """``({status, last_tick}, reason)`` for one run; ``reason`` says why the run is incomplete, None when it ran to
    its last tick (``run.days * run.ticks_per_day`` of its resolved config) with ``run.status`` ``completed``."""
    run_dir = Path(run_dir)
    cfg = _load_cfg(run_dir) if cfg is None else cfg
    status = _run_status(run_dir)
    last_tick, intact = _metrics_tail(run_dir)
    days, tpd = resolve_path(cfg, "run.days"), resolve_path(cfg, "run.ticks_per_day")
    expected = int(days) * int(tpd) if isinstance(days, int) and isinstance(tpd, int) else None
    reasons: list[str] = []
    if status != "completed":
        reasons.append("no run row in world.db" if status is None else f"run.status {status!r}")
    if last_tick is None:
        reasons.append("no tick rows in the metrics")
    elif expected is None:
        reasons.append("the resolved config has no run.days / run.ticks_per_day to check the last tick against")
    elif last_tick < expected:
        reasons.append(f"last tick row {last_tick} of {expected}")
    if not intact:
        reasons.append("a metrics line does not parse")
    return {"status": status, "last_tick": last_tick}, ("; ".join(reasons) or None)


# --- statistics -------------------------------------------------------------------------------------------
def sign_test_p(positive: int, negative: int) -> float | None:
    """Exact two-sided sign test (binomial, ties excluded); None without informative pairs."""
    m = positive + negative
    if m == 0:
        return None
    k = min(positive, negative)
    tail = sum(math.comb(m, i) for i in range(k + 1)) / 2**m
    return min(1.0, 2.0 * tail)


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return math.nan
    pos = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def paired_stats(pairs: list[tuple[float, float]], *, n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> dict[str, Any]:
    """Paired statistics for ``(a, b)`` outcome pairs: delta is ``mean(b - a)``."""
    n = len(pairs)
    out: dict[str, Any] = {"n": n, "mean_a": None, "mean_b": None, "delta": None, "d_z": None,
                           "ci95": [None, None], "sign_test_p": None, "positive": 0, "negative": 0, "ties": 0}
    if n == 0:
        return out
    diffs = [b - a for a, b in pairs]
    out["mean_a"] = sum(a for a, _ in pairs) / n
    out["mean_b"] = sum(b for _, b in pairs) / n
    out["delta"] = sum(diffs) / n
    out["positive"] = sum(1 for d in diffs if d > 0)
    out["negative"] = sum(1 for d in diffs if d < 0)
    out["ties"] = n - out["positive"] - out["negative"]
    out["sign_test_p"] = sign_test_p(out["positive"], out["negative"])
    if n >= 2:
        sd = statistics.stdev(diffs)
        out["d_z"] = (out["delta"] / sd) if sd > 0 else None
        rng = random.Random(seed)
        means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
        out["ci95"] = [_percentile(means, 0.025), _percentile(means, 0.975)]
    return out


def _outcome_stats(a: str, b: str, path: str, values: dict[str, dict[int, dict[str, float | None]]], seeds: list[int],
                   *, n_boot: int, boot_seed: int) -> dict[str, Any]:
    pairs: list[tuple[float, float]] = []
    per_seed: dict[str, list[float | None]] = {}
    dropped = 0
    for seed in seeds:
        va, vb = values[a][seed].get(path), values[b][seed].get(path)
        per_seed[str(seed)] = [va, vb]
        if va is None or vb is None:
            dropped += 1
            continue
        pairs.append((va, vb))
    st = paired_stats(pairs, n_boot=n_boot, seed=boot_seed)
    return {"path": path, "arm_a": a, "arm_b": b, **st, "dropped": dropped, "per_seed": per_seed}


# --- pre-registered hypotheses -------------------------------------------------------------------------------
_HYP_FIELDS = ("name", "kind", "outcome", "arm_a", "arm_b", "direction", "margin", "margin_unit", "primary", "min_power",
               "arm_c", "arm_d", "outcome_template", "levels", "reference")
FOUR_ARM_CONTRAST = "interaction"  # the label of a four-arm interaction's single contrast


def _std_power(effect: float, sd: float | None, n: int, fn: Any) -> float | None:
    """``fn(effect / sd, n)`` for an effect in outcome units (negative counts as none); with ``sd == 0`` any
    positive effect is certain to show (power 1). None below two pairs."""
    if n < 2 or sd is None:
        return None
    effect = max(0.0, effect)
    if sd == 0:
        return 1.0 if effect > 0 else float(fn(0.0, n))
    return float(fn(effect / sd, n))


def evaluate_hypothesis(h: Mapping[str, Any], diffs: Sequence[float], *, alpha: float = ALPHA, n_boot: int = N_BOOT,
                        boot_seed: int = BOOT_SEED) -> dict[str, Any]:
    """The unadjusted test of one ``experiment.hypotheses`` entry (as a dict) on its per-seed differences
    ``arm_b - arm_a`` (see the module docstring for each kind's rule).

    Returns ``{n, delta, sd, d_z, margin_abs, ci, ci_level, p, power, power_at, holds, ci_clears}``: ``holds`` is the
    kind's own verdict before any Holm adjustment (None below two pairs) and ``ci_clears`` superiority's interval rule
    (None for an equivalence hypothesis). :func:`decide_hypotheses` turns rows into verdicts."""
    d = [float(x) for x in diffs]
    n = len(d)
    kind, direction = str(h.get("kind", "superiority")), str(h.get("direction", "greater"))
    mean = sum(d) / n if n else None
    sd = statistics.stdev(d) if n >= 2 else None
    d_z = mean / sd if mean is not None and sd else None
    margin = float(h.get("margin", 0.0))
    margin_abs: float | None = margin
    if h.get("margin_unit") == "sd":
        margin_abs = margin * sd if sd is not None else None
    power_at = {"superiority": "observed effect beyond the margin", "non_superiority": "an effect the size of the margin",
                "equivalence": "a zero effect (TOST)"}[kind]
    out: dict[str, Any] = {"n": n, "delta": mean, "sd": sd, "d_z": d_z, "margin_abs": margin_abs, "ci": [None, None],
                           "ci_level": 1.0 - 2.0 * alpha if kind == "equivalence" else 1.0 - alpha, "p": None,
                           "power": None, "power_at": power_at, "holds": None, "ci_clears": None}
    if n < 2 or margin_abs is None or mean is None:
        return out
    if kind == "equivalence":
        tost = stats.tost_paired(d, margin_abs, alpha)
        out.update(ci=tost["ci90"], p=tost["p"], holds=tost["equivalent"],
                   power=_std_power(margin_abs, sd, n, lambda z, k: stats.power_tost(z, k, alpha)))
        return out
    sup = stats.superiority(d, margin_abs, direction, alpha, n_boot=n_boot, seed=boot_seed)
    out.update(ci=sup["ci"], p=sup["p"], ci_clears=sup["ci_clears"])
    if kind == "superiority":
        beyond = (mean - margin_abs) if direction == "greater" else (-mean - margin_abs)
        out.update(holds=sup["passed"], power=_std_power(beyond, sd, n, lambda z, k: stats.power_paired(z, k, alpha)))
    else:
        non = stats.non_superiority(d, margin_abs, direction, alpha, n_boot=n_boot, seed=boot_seed)
        out.update(holds=non["passed"], power=_std_power(margin_abs, sd, n, lambda z, k: stats.power_paired(z, k, alpha)))
    return out


def evaluate_interaction(h: Mapping[str, Any], contrasts: Mapping[str, Sequence[float]], *, alpha: float = ALPHA,
                         n_boot: int = N_BOOT, boot_seed: int = BOOT_SEED) -> dict[str, Any]:
    """The test of one ``kind: interaction`` hypothesis (as a dict) on its contrasts' per-seed values, ``{label:
    values}`` in order (one label for a four-arm interaction, one per non-reference level otherwise); see the module
    docstring for the rule.

    Returns the fields :func:`evaluate_hypothesis` returns, with ``p`` the hypothesis's own p (the smallest
    within-hypothesis Holm p, the one that enters the Holm family when the hypothesis is primary), ``holds`` whether
    any contrast clears (None when no contrast has two values), ``n, delta, sd, d_z, margin_abs, ci, power,
    ci_clears`` those of the deciding contrast (``deciding``), ``k`` the number of contrasts tested and ``contrasts``
    one row per contrast: ``{label, n, estimate, sd, d_z, margin_abs, ci, side, p, p_holm, power, ci_clears,
    clears}``."""
    direction = str(h.get("direction", "greater"))
    margin = float(h.get("margin", 0.0))
    min_power = h.get("min_power")
    rows: list[dict[str, Any]] = []
    for label, values in contrasts.items():
        d = [float(x) for x in values]
        n = len(d)
        mean = sum(d) / n if n else None
        sd = statistics.stdev(d) if n >= 2 else None
        margin_abs: float | None = margin
        if h.get("margin_unit") == "sd":
            margin_abs = margin * sd if sd is not None else None
        row: dict[str, Any] = {"label": str(label), "n": n, "estimate": mean, "sd": sd,
                               "d_z": mean / sd if mean is not None and sd else None, "margin_abs": margin_abs,
                               "ci": [None, None], "side": None, "p": None, "p_holm": None, "power": None,
                               "ci_clears": None, "clears": None}
        if n >= 2 and margin_abs is not None and mean is not None:
            t = stats.contrast_test(d, margin_abs, direction, alpha, n_boot=n_boot, seed=boot_seed)
            beyond = (abs(mean) if direction == "two_sided" else mean if direction == "greater" else -mean) - margin_abs
            row.update(ci=t["ci"], side=t["side"], p=t["p"], ci_clears=t["ci_clears"],
                       power=_std_power(beyond, sd, n, lambda z, k: stats.power_paired(z, k, alpha)))
        rows.append(row)
    within = stats.holm({r["label"]: r["p"] for r in rows})
    tested = [r for r in rows if r["p"] is not None]
    for r in tested:
        r["p_holm"] = within[r["label"]]
        powered = min_power is None or (r["power"] is not None and r["power"] >= float(min_power))
        r["clears"] = bool(r["ci_clears"]) and r["p_holm"] < alpha and powered
    clearing = sorted((r for r in tested if r["clears"]), key=lambda r: r["p_holm"])
    by_p = sorted(tested, key=lambda r: r["p_holm"])
    deciding = clearing[0] if clearing else by_p[0] if by_p else (rows[0] if rows else None)
    out: dict[str, Any] = {"n": 0, "delta": None, "sd": None, "d_z": None, "margin_abs": None, "ci": [None, None],
                           "ci_level": 1.0 - alpha, "p": by_p[0]["p_holm"] if by_p else None, "power": None,
                           "power_at": "observed effect beyond the margin", "holds": bool(clearing) if tested else None,
                           "ci_clears": None, "k": len(tested), "deciding": None, "contrasts": rows}
    if deciding is not None:
        out.update(n=deciding["n"], delta=deciding["estimate"], sd=deciding["sd"], d_z=deciding["d_z"],
                   margin_abs=deciding["margin_abs"], ci=deciding["ci"], power=deciding["power"],
                   ci_clears=deciding["ci_clears"], deciding=deciding["label"])
    return out


def _g(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.4g}"


def _side_text(direction: str, margin: Any, unit: Any) -> str:
    m = f"{_g(float(margin or 0.0))}{' SD' if unit == 'sd' else ''}"
    if direction == "two_sided":
        return f"beyond +/-{m}" if float(margin or 0.0) > 0 else "away from 0 (either side)"
    return f"> {m}" if direction == "greater" else f"< -{m}"


def _interaction_verdict(r: Mapping[str, Any], alpha: float) -> tuple[bool, str]:
    side = _side_text(str(r["direction"]), r["margin"], r["margin_unit"])
    k = r["k"]
    ok: list[str] = []
    bad: list[str] = []
    clearing = [c["label"] for c in r["contrasts"] if c["clears"]]
    within = f"Holm across its {k} contrast{'s' if k != 1 else ''}"
    if clearing:
        ok.append(f"{', '.join(clearing)} clear{'s' if len(clearing) == 1 else ''} {side} (CI95 and p_holm < {alpha}, "
                  f"{within})")
    else:
        why: list[str] = []
        for c in r["contrasts"]:
            if c["p"] is None:
                continue
            if not c["ci_clears"]:
                why.append(f"{c['label']}: CI95 does not clear")
            elif c["p_holm"] >= alpha:
                why.append(f"{c['label']}: p_holm {_g(c['p_holm'])} >= {alpha}")
            else:
                why.append(f"{c['label']}: power {_g(c['power'])} < min_power {_g(r['min_power'])}")
        bad.append(f"no contrast clears {side} after {within} ({'; '.join(why)})")
    if r["primary"]:
        p_holm = r["p_holm"]
        (ok if p_holm < alpha else bad).append(f"p_holm {_g(p_holm)} {'<' if p_holm < alpha else '>='} {alpha} "
                                               f"(its p {_g(r['p'])} in the Holm family of primary hypotheses)")
    return not bad, "; ".join(bad + ok)


def _verdict(r: Mapping[str, Any], alpha: float) -> tuple[bool, str]:
    """``(passed, reason)`` for one evaluated row whose ``p_holm`` is set."""
    if r.get("missing_arms"):
        return False, f"arm(s) {r['missing_arms']} have no runs in this experiment"
    if r["kind"] == "interaction":
        if r["holds"] is None:
            most = max((int(c["n"]) for c in r.get("contrasts") or []), default=0)
            return False, (f"undetermined: no contrast has values for at least 2 seeds in every arm and level it reads "
                           f"(at most {most})")
        return _interaction_verdict(r, alpha)
    if r["n"] < 2 or r["holds"] is None:
        return False, f"undetermined: {r['n']} seed pair(s) with values for both arms, at least 2 needed"
    kind, greater = r["kind"], r["direction"] == "greater"
    lo, hi = r["ci"]
    m = r["margin_abs"]
    side = f"> {_g(m)}" if greater else f"< {_g(-m if m else 0.0)}"
    p_name, p_used = ("p_holm", r["p_holm"]) if r["primary"] else ("p", r["p"])
    ok: list[str] = []
    bad: list[str] = []
    if kind == "superiority":
        bound = lo if greater else hi
        (ok if r["ci_clears"] else bad).append(f"CI95 {'lower' if greater else 'upper'} bound {_g(bound)} "
                                               f"{'clears' if r['ci_clears'] else 'does not clear'} {side}")
        (ok if p_used < alpha else bad).append(f"{p_name} {_g(p_used)} {'<' if p_used < alpha else '>='} {alpha}")
    elif kind == "non_superiority":
        if r["holds"]:
            ok.append(f"superiority {side} does not hold (CI95 [{_g(lo)}, {_g(hi)}], p {_g(r['p'])})")
        else:
            bad.append(f"superiority {side} holds (CI95 [{_g(lo)}, {_g(hi)}], p {_g(r['p'])} < {alpha})")
    else:
        inside = f"CI90 [{_g(lo)}, {_g(hi)}] vs +/-{_g(m)}"
        (ok if p_used < alpha else bad).append(f"TOST {p_name} {_g(p_used)} {'<' if p_used < alpha else '>='} {alpha}; {inside}")
    if r["min_power"] is not None:
        power = r["power"]
        enough = power is not None and power >= r["min_power"]
        (ok if enough else bad).append(f"power {_g(power)} {'>=' if enough else '<'} min_power {_g(r['min_power'])} "
                                       f"(at {r['power_at']})")
    return not bad, "; ".join(bad + ok)  # what failed first, then what held


def decide_hypotheses(rows: list[dict[str, Any]], alpha: float = ALPHA) -> list[dict[str, Any]]:
    """Holm-adjust the p-values of the primary rows (``p_holm``; None for exploratory rows and untestable ones) and
    set ``passed`` / ``reason`` on every row from :func:`evaluate_hypothesis`'s fields. Mutates and returns ``rows``."""
    adjusted = stats.holm({r["name"]: r["p"] for r in rows if r["primary"]})
    for r in rows:
        r["p_holm"] = adjusted.get(r["name"]) if r["primary"] else None
        r["passed"], r["reason"] = _verdict(r, alpha)
    return rows


def _interaction_row(h: Mapping[str, Any], lookup: Mapping[str, str], seeds: list[int],
                     problems: Mapping[tuple[str, int], str], get: Any, *, alpha: float, n_boot: int,
                     boot_seed: int) -> dict[str, Any]:
    """One evaluated row of a ``kind: interaction`` hypothesis (see the module docstring): its contrasts' per-seed
    values over the seeds every arm it names completed, then :func:`evaluate_interaction`."""
    spec = HypothesisConfig.model_validate(dict(h))
    four = spec.layout == "four_arms"
    labels = [spec.arm_a, spec.arm_b] + ([str(spec.arm_c), str(spec.arm_d)] if four else [])
    dirs = [lookup.get(x) for x in labels]
    missing = [x for x, d in zip(labels, dirs, strict=True) if d is None]
    if not missing and len(set(dirs)) < len(dirs):
        missing = [f"{', '.join(labels)} name the same runs more than once"]
    row: dict[str, Any] = {**{k: h.get(k) for k in _HYP_FIELDS}, "outcome": spec.outcome or spec.outcome_template,
                           "reference": spec.reference_level, "missing_arms": missing}
    per_seed: dict[str, Any] = {}
    values: dict[str, dict[str, float]] = {}
    used = [] if missing else [s for s in seeds if all((d, s) not in problems for d in dirs)]
    run_dirs = [str(d) for d in dirs]
    if four:
        values[FOUR_ARM_CONTRAST] = {}
        for s in used:
            ya, yb, yc, yd = (get(d, s, str(spec.outcome)) for d in run_dirs)
            per_seed[str(s)] = [ya, yb, yc, yd]
            if None not in (ya, yb, yc, yd):
                values[FOUR_ARM_CONTRAST][str(s)] = (yd - yc) - (yb - ya)
    else:
        paths = spec.level_outcomes()
        ref = str(spec.reference_level)
        others = [lv for lv in paths if lv != ref]
        for lv in others:
            values[lv] = {}
        for s in used:
            cell = {lv: [get(run_dirs[0], s, p), get(run_dirs[1], s, p)] for lv, p in paths.items()}
            per_seed[str(s)] = cell
            ra, rb = cell[ref]
            for lv in others:
                va, vb = cell[lv]
                if None not in (va, vb, ra, rb):
                    values[lv][str(s)] = (vb - va) - (rb - ra)
    row.update(evaluate_interaction(row, {lb: list(v.values()) for lb, v in values.items()}, alpha=alpha,
                                    n_boot=n_boot, boot_seed=boot_seed))
    for c in row["contrasts"]:
        c["per_seed"] = values[c["label"]]
        c["dropped"] = len(used) - len(values[c["label"]])
        c["vs"] = None if four else row["reference"]
    deciding = next((c for c in row["contrasts"] if c["label"] == row["deciding"]), None)
    row.update(n_seeds=len(used), dropped=deciding["dropped"] if deciding else 0, per_seed=per_seed)
    return row


def _hypotheses(reps: Mapping[str, Mapping[str, Any]], names: list[str]) -> list[dict[str, Any]]:
    """``experiment.hypotheses`` of the first arm; refused when another arm's differ."""
    ref = names[0]
    hyps = resolve_path(reps[ref], "experiment.hypotheses") or []
    for arm in names[1:]:
        other = resolve_path(reps[arm], "experiment.hypotheses") or []
        if other != hyps:
            where = config_diff(hyps, other, "experiment.hypotheses")
            raise IncomparableRuns(f"arms disagree on experiment.hypotheses ({ref} vs {arm}: {where}); "
                                   "every arm must carry the same pre-registered hypotheses")
    return [dict(h) for h in hyps]


def _arm_lookup(reps: Mapping[str, Mapping[str, Any]], names: list[str]) -> dict[str, str]:
    """Arm directory by the names a hypothesis may use: the directory name, else the run's ``experiment.arm``."""
    out = {n: n for n in names}
    for n in names:
        label = resolve_path(reps[n], "experiment.arm")
        if isinstance(label, str) and label not in out:
            out[label] = n
    return out


# --- factorial and per-tier analyses -------------------------------------------------------------------------
def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def _level_key(v: Any) -> tuple[int, float, str]:
    if isinstance(v, bool | int | float):
        return (0, float(v), "")
    return (1, 0.0, _canon(v))


def factorial_design(reps: Mapping[str, Mapping[str, Any]], ivs: Sequence[str]) -> dict[str, Any] | None:
    """The 2x2 layout of four arms: ``{"factors": [{path, levels}, {path, levels}], "cells": {(i, j): arm}}`` when
    exactly two of the declared independent variables vary across the arms, each over exactly two values, and every
    combination is exactly one arm; None otherwise. Level 0 is the lower value (``false`` before ``true``)."""
    names = list(reps)
    if len(names) != 4:
        return None
    factors: list[tuple[str, list[Any], dict[str, int]]] = []
    for iv in ivs:
        vals = {arm: resolve_path(reps[arm], iv) for arm in names}
        distinct: dict[str, Any] = {}
        for v in vals.values():
            distinct.setdefault(_canon(v), v)
        if len(distinct) == 1:
            continue
        if len(distinct) != 2:
            return None
        levels = sorted(distinct.values(), key=_level_key)
        index = [_canon(level) for level in levels]
        factors.append((iv, levels, {arm: index.index(_canon(vals[arm])) for arm in names}))
    if len(factors) != 2:
        return None
    cells: dict[tuple[int, int], str] = {}
    for arm in names:
        key = (factors[0][2][arm], factors[1][2][arm])
        if key in cells:
            return None
        cells[key] = arm
    return {"factors": [{"path": iv, "levels": levels} for iv, levels, _ in factors], "cells": cells}


def _roster_tiers(cfg: Mapping[str, Any]) -> set[str]:
    return {str(a["tier"]) for a in cfg.get("agents") or [] if isinstance(a, dict) and a.get("tier")}


def _tier_did(names: list[str], tiers: list[str], outcomes: list[str], pair_seeds: Mapping[tuple[str, str], list[int]],
              get: Any, *, n_boot: int, boot_seed: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for outcome in outcomes:
        for a, b in combinations(names, 2):
            seeds = pair_seeds[(a, b)]
            per_tier: dict[str, Any] = {}
            deltas: dict[str, dict[int, float]] = {}
            for t in tiers:
                path = f"{outcome}.{t}"
                pairs: list[tuple[float, float]] = []
                per_seed: dict[str, list[float | None]] = {}
                deltas[t] = {}
                for s in seeds:
                    va, vb = get(a, s, path), get(b, s, path)
                    per_seed[str(s)] = [va, vb]
                    if va is not None and vb is not None:
                        pairs.append((va, vb))
                        deltas[t][s] = vb - va
                per_tier[t] = {"path": path, **paired_stats(pairs, n_boot=n_boot, seed=boot_seed),
                               "dropped": len(seeds) - len(pairs), "per_seed": per_seed}
            differences: list[dict[str, Any]] = []
            for t1, t2 in combinations(tiers, 2):
                common = [s for s in seeds if s in deltas[t1] and s in deltas[t2]]
                st = paired_stats([(deltas[t1][s], deltas[t2][s]) for s in common], n_boot=n_boot, seed=boot_seed)
                differences.append({
                    "tier_a": t1, "tier_b": t2, "n": st["n"], "did": st["delta"], "ci95": st["ci95"], "d_z": st["d_z"],
                    "sign_test_p": st["sign_test_p"], "positive": st["positive"], "negative": st["negative"], "ties": st["ties"],
                    "dropped": len(seeds) - len(common),
                    "per_seed": {str(s): deltas[t2][s] - deltas[t1][s] for s in common},
                })
            rows.append({"outcome": outcome, "arm_a": a, "arm_b": b, "per_tier": per_tier, "differences": differences,
                         "exploratory": True})
    return rows


# --- extras -----------------------------------------------------------------------------------------------
def _served_models(run_dir: Path) -> tuple[str, ...]:
    with connect(run_dir) as db:
        return tuple(sorted(str(r["model"]) for r in db.execute("SELECT DISTINCT model FROM llm_calls WHERE purpose='decide'")))


def _retrieved_sets(run_dir: Path) -> dict[tuple[str, int], set[str]]:
    """``{(agent name, tick): retrieved set}`` from ``llm_calls.extras`` when the loop recorded one."""
    out: dict[tuple[str, int], set[str]] = {}
    with connect(run_dir) as db:
        rows = db.execute("SELECT a.name AS name, c.tick AS tick, c.extras AS extras FROM llm_calls c "
                          "JOIN agents a ON a.agent_id=c.agent_id WHERE c.purpose='decide' AND c.extras IS NOT NULL").fetchall()
    for r in rows:
        try:
            extras = json.loads(r["extras"])
        except (TypeError, ValueError):
            continue
        if not isinstance(extras, dict):
            continue
        for key in _RETRIEVED_KEYS:
            val = extras.get(key)
            if isinstance(val, list):
                out[(str(r["name"]), int(r["tick"]))] = {str(v) for v in val}
                break
    return out


def _memory_degenerate_pair(arms: dict[str, dict[int, Path]], names: list[str], seeds: list[int]) -> tuple[bool | None, str]:
    a, b = names[0], names[1]
    sims: list[float] = []
    saw_extras = False
    for seed in seeds:
        sa, sb = _retrieved_sets(arms[a][seed]), _retrieved_sets(arms[b][seed])
        saw_extras = saw_extras or bool(sa) or bool(sb)
        for key in sa.keys() & sb.keys():
            union = sa[key] | sb[key]
            sims.append((len(sa[key] & sb[key]) / len(union)) if union else 1.0)
    if not saw_extras:
        return None, "llm_calls.extras holds no retrieved sets; the retrieved-set Jaccard check between memory modes was skipped"
    if not sims:
        return None, "no (agent, tick) retrieval shared between the memory arms; Jaccard check skipped"
    mean_j = sum(sims) / len(sims)
    flag = mean_j > JACCARD_DEGENERATE
    return flag, f"mean retrieved-set Jaccard between {a} and {b} = {mean_j:.3f} over {len(sims)} calls" + (" (degenerate pair)" if flag else "")


def _daily_curves(arms: dict[str, dict[int, Path]], names: list[str], seeds: dict[str, list[int]], path: str) -> dict[str, dict[str, float | None]]:
    """Per arm and day, the mean over the arm's (complete) seeds of the primary: a per-day run-level aggregate when the
    path has one (:data:`DAILY`), else the value on the day's last tick row, else on the day row."""
    head, _, rest = path.partition(".")
    curves: dict[str, dict[str, float | None]] = {}
    for arm in names:
        per_day: dict[int, list[float]] = {}
        for seed in seeds[arm]:
            tick_rows, day_rows = load_metrics(arms[arm][seed])
            days = sorted({int(r["day"]) for r in tick_rows} | {int(r["day"]) for r in day_rows})
            if head in DAILY:
                for day in days:
                    v = DAILY[head](arms[arm][seed], rest or None, day)
                    if v is not None:
                        per_day.setdefault(day, []).append(float(v))
                continue
            last_of_day: dict[int, dict[str, Any]] = {}
            for row in tick_rows:
                last_of_day[int(row["day"])] = row
            found = False
            for day, row in last_of_day.items():
                v = resolve_path(row, path)
                if isinstance(v, int | float) and not isinstance(v, bool):
                    per_day.setdefault(day, []).append(float(v))
                    found = True
            if not found:
                for row in day_rows:
                    v = resolve_path(row, path)
                    if isinstance(v, int | float) and not isinstance(v, bool):
                        per_day.setdefault(int(row["day"]), []).append(float(v))
        if per_day:
            curves[arm] = {str(d): (sum(v) / len(v)) for d, v in sorted(per_day.items())}
    return curves


# --- the comparison -------------------------------------------------------------------------------------
def compare(exp_dir: str | Path, primary: str | None = None, *, allow_incomplete: bool = False,
            n_boot: int = N_BOOT, boot_seed: int = BOOT_SEED, alpha: float = ALPHA) -> dict[str, Any]:
    """Compare the arms under ``exp_dir`` (see the module docstring); raises :class:`IncomparableRuns`.

    ``allow_incomplete`` drops every seed pair that touches an incomplete run instead of refusing. ``alpha`` is the
    level of the pre-registered hypotheses' decisions."""
    exp_dir = Path(exp_dir)
    arms = discover_runs(exp_dir)
    if len(arms) < 2:
        raise IncomparableRuns(f"{exp_dir}: need at least two arms with runs, found {sorted(arms)}")
    names = list(arms)
    cfgs = {arm: {seed: _load_cfg(rd) for seed, rd in runs.items()} for arm, runs in arms.items()}
    ref = names[0]
    seeds = sorted(arms[ref])
    for arm in names[1:]:
        if sorted(arms[arm]) != seeds:
            raise IncomparableRuns(f"seed sets differ: {ref}={seeds} vs {arm}={sorted(arms[arm])}")
    for arm in names:
        base = cfgs[arm][seeds[0]]
        for seed in seeds[1:]:
            other = cfgs[arm][seed]
            extra = [p for p in config_diff(base, other) if p != "run.seed" and not _seed_placed(p, base, other)]
            if extra:
                raise IncomparableRuns(f"arm {arm!r}: runs for seeds {seeds[0]} and {seed} differ outside run.seed: {extra}")
    reps = {arm: cfgs[arm][seeds[0]] for arm in names}
    exp_name = str(resolve_path(reps[ref], "experiment.name") or exp_dir.name)
    ivs: list[str] = []
    for arm in names:
        for iv in resolve_path(reps[arm], "experiment.independent_variables") or []:
            if iv not in ivs:
                ivs.append(str(iv))
    for arm in names:
        mode = resolve_path(reps[arm], "entropy.degeneration.mode")
        if mode != "observe":
            raise IncomparableRuns(f"arm {arm!r}: entropy.degeneration.mode is {mode!r}; experiments must use 'observe'")
    if in_family(exp_name, EQUALIZED_EXPERIMENTS):
        for arm in names:
            wp = resolve_path(reps[arm], "economy.world_pricing")
            if wp != "equalized":
                raise IncomparableRuns(f"{exp_name}: arm {arm!r} has economy.world_pricing={wp!r}; 'equalized' is required")
    if in_family(exp_name, MUTATION_FREE_EXPERIMENTS):
        for arm in names:
            prob = resolve_path(reps[arm], "population.tier_mutation_prob")
            if prob != 0:
                raise IncomparableRuns(f"{exp_name}: arm {arm!r} has population.tier_mutation_prob={prob!r}; 0 is required")
    hypotheses = _hypotheses(reps, names)
    violations: list[str] = []
    for a, b in combinations(names, 2):
        for path in config_diff(reps[a], reps[b]):
            if path in ALWAYS_ALLOWED or covered_by(path, ivs):
                continue
            violations.append(f"{a} vs {b}: {path}")
    if violations:
        raise IncomparableRuns(f"configs differ outside experiment.independent_variables {ivs}: " + "; ".join(violations))
    if in_family(exp_name, TIER_INVARIANT_EXPERIMENTS):  # rungs may differ only in hidden capability
        invariants: dict[str, dict[str, Any]] = {}
        for arm in names:
            for agent in reps[arm].get("agents", []):
                tier = reps[arm]["tiers"][agent["tier"]]
                invariants[f"{arm}:{agent['tier']}"] = {k: tier.get(k) for k in TIER_INVARIANTS}
        vals = list(invariants.values())
        if any(v != vals[0] for v in vals):
            raise IncomparableRuns(f"{exp_name}: rostered tiers differ in declared thresholds/max_tokens/effort: {invariants}")
        drains = {json.dumps(resolve_path(reps[arm], "entropy.drain"), sort_keys=True) for arm in names}
        if len(drains) > 1:
            raise IncomparableRuns(f"{exp_name}: arms differ in entropy.drain")

    states: dict[str, dict[int, dict[str, Any]]] = {arm: {} for arm in names}
    problems: dict[tuple[str, int], str] = {}
    for arm in names:
        for seed in seeds:
            states[arm][seed], why = run_state(arms[arm][seed], cfgs[arm][seed])
            if why:
                problems[(arm, seed)] = why
    listed = "; ".join(f"{arm} seed {seed}: {why}" for (arm, seed), why in problems.items())
    if problems and not allow_incomplete:
        raise IncomparableRuns(f"incomplete runs (allow_incomplete / --allow-incomplete drops the seed pairs they touch): {listed}")
    complete = {arm: [s for s in seeds if (arm, s) not in problems] for arm in names}
    pair_seeds = {(a, b): [s for s in seeds if (a, s) not in problems and (b, s) not in problems] for a, b in combinations(names, 2)}
    empty = [f"{a} vs {b}" for (a, b), ss in pair_seeds.items() if not ss]
    if empty:
        raise IncomparableRuns(f"no complete seed pair left for {', '.join(empty)} after dropping incomplete runs: {listed}")
    dropped = [{"arm": arm, "seed": seed, **states[arm][seed]} for (arm, seed) in problems]

    primary = primary or resolve_path(reps[ref], "experiment.primary_outcome")
    if not primary:
        raise IncomparableRuns("no primary outcome: set experiment.primary_outcome in the config or pass one explicitly")
    primary = str(primary)
    secondaries: list[str] = []
    for arm in names:
        for s in resolve_path(reps[arm], "experiment.secondary_outcomes") or []:
            if s != primary and s not in secondaries:
                secondaries.append(str(s))
    paths = [primary] + secondaries
    values = {arm: {seed: {p: end_of_run_outcome(arms[arm][seed], p) for p in paths} for seed in complete[arm]} for arm in names}
    cache: dict[tuple[str, int, str], float | None] = {
        (arm, seed, p): v for arm in names for seed, row in values[arm].items() for p, v in row.items()}

    def get(arm: str, seed: int, path: str) -> float | None:
        key = (arm, seed, path)
        if key not in cache:
            cache[key] = end_of_run_outcome(arms[arm][seed], path)
        return cache[key]

    comparisons: list[dict[str, Any]] = []
    for a, b in combinations(names, 2):
        ps = pair_seeds[(a, b)]
        comparisons.append({
            "arm_a": a, "arm_b": b,
            "primary": _outcome_stats(a, b, primary, values, ps, n_boot=n_boot, boot_seed=boot_seed),
            "secondary": [{**_outcome_stats(a, b, s, values, ps, n_boot=n_boot, boot_seed=boot_seed), "exploratory": True}
                          for s in secondaries],
        })

    lookup = _arm_lookup(reps, names)
    hyp_rows: list[dict[str, Any]] = []
    for h in hypotheses:
        if h.get("kind") == "interaction":
            hyp_rows.append(_interaction_row(h, lookup, seeds, problems, get, alpha=alpha, n_boot=n_boot,
                                             boot_seed=boot_seed))
            continue
        a_dir, b_dir = lookup.get(str(h["arm_a"])), lookup.get(str(h["arm_b"]))
        missing = [str(h[k]) for k, d in (("arm_a", a_dir), ("arm_b", b_dir)) if d is None]
        if a_dir is not None and a_dir == b_dir:
            missing = [f"{h['arm_a']} and {h['arm_b']} name the same runs"]
        diffs: list[float] = []
        per_seed: dict[str, list[float | None]] = {}
        if not missing and a_dir is not None and b_dir is not None:
            for s in pair_seeds[(a_dir, b_dir) if (a_dir, b_dir) in pair_seeds else (b_dir, a_dir)]:
                va, vb = get(a_dir, s, str(h["outcome"])), get(b_dir, s, str(h["outcome"]))
                per_seed[str(s)] = [va, vb]
                if va is not None and vb is not None:
                    diffs.append(vb - va)
        hyp_rows.append({**{k: h.get(k) for k in _HYP_FIELDS}, "missing_arms": missing,
                         **evaluate_hypothesis(h, diffs, alpha=alpha, n_boot=n_boot, boot_seed=boot_seed),
                         "dropped": len(per_seed) - len(diffs), "per_seed": per_seed})
    decide_hypotheses(hyp_rows, alpha)

    factorial: dict[str, Any] | None = None
    design = factorial_design(reps, ivs)
    if design is not None:
        cells = {key: {s: values[arm][s][primary] for s in complete[arm]} for key, arm in design["cells"].items()}
        factorial = {"outcome": primary, "factor_a": design["factors"][0], "factor_b": design["factors"][1],
                     "cells": {f"{i},{j}": arm for (i, j), arm in sorted(design["cells"].items())},
                     **stats.interaction_2x2(cells, n_boot=n_boot, seed=boot_seed)}

    tier_did: dict[str, Any] | None = None
    tiers = sorted(set.intersection(*(_roster_tiers(reps[arm]) for arm in names)))
    tier_outcomes = [p for p in paths if p in TIER_OUTCOMES]
    if len(tiers) >= 2 and tier_outcomes:
        tier_did = {"tiers": tiers, "rows": _tier_did(names, tiers, tier_outcomes, pair_seeds, get,
                                                       n_boot=n_boot, boot_seed=boot_seed)}
    warnings: list[str] = [f"dropped incomplete run {arm} seed {seed} ({why}); every seed pair that touches it is left out"
                           for (arm, seed), why in problems.items()]
    models: dict[str, dict[int, tuple[str, ...]]] = {}
    for arm in names:
        models[arm] = {seed: _served_models(arms[arm][seed]) for seed in complete[arm]}
        if len(set(models[arm].values())) > 1:
            warnings.append(f"arm {arm!r}: served models differ across runs: " + ", ".join(f"seed {s}: {list(m)}" for s, m in models[arm].items()))
    notes: list[str] = []
    degenerate: bool | None = None
    if exp_name.startswith("exp_memory"):
        degenerate, note = _memory_degenerate_pair(arms, names, pair_seeds[(names[0], names[1])])
        notes.append(note)
    return {
        "experiment": exp_name, "exp_dir": str(exp_dir), "arms": names, "seeds": seeds, "n_seeds": len(seeds),
        "independent_variables": ivs, "primary": primary, "secondaries": secondaries,
        "values": {arm: {str(seed): values[arm][seed] for seed in complete[arm]} for arm in names},
        "runs": {arm: {str(seed): states[arm][seed] for seed in seeds} for arm in names},
        "dropped_incomplete": dropped,
        "comparisons": comparisons, "alpha": alpha, "hypotheses": hyp_rows, "factorial": factorial, "tier_did": tier_did,
        "warnings": warnings, "notes": notes,
        "degenerate_pair": degenerate, "served_models": {arm: {str(s): list(m) for s, m in ms.items()} for arm, ms in models.items()},
        "curves": _daily_curves(arms, names, complete, primary),
    }


# --- rendering -------------------------------------------------------------------------------------------
def _fmt(v: float | None, nd: int = 4) -> str:
    return "n/a" if v is None else f"{v:.{nd}f}"


def _stat_line(s: dict[str, Any]) -> str:
    lo, hi = s.get("ci95", [None, None])
    return (f"n={s['n']} (dropped {s.get('dropped', 0)})  mean {s['arm_a']}={_fmt(s['mean_a'])}  {s['arm_b']}={_fmt(s['mean_b'])}  "
            f"delta={_fmt(s['delta'])}  d_z={_fmt(s['d_z'], 3)}  CI95=[{_fmt(lo)}, {_fmt(hi)}]  "
            f"sign p={_fmt(s['sign_test_p'], 3)} (+{s['positive']}/-{s['negative']}/={s['ties']})")


def _hypothesis_line(h: dict[str, Any]) -> str:
    lo, hi = h["ci"]
    tag = h["kind"] if h["kind"] == "equivalence" else f"{h['kind']} {h['direction']}"
    unit = " SD" if h["margin_unit"] == "sd" else ""
    tag += f", margin {_g(h['margin'])}{unit}, " + ("primary" if h["primary"] else "exploratory")
    level = round(100 * h["ci_level"])
    verdict = f"{'PASS' if h['passed'] else 'FAIL'}  {h['name']} [{tag}]"
    if h["kind"] == "interaction" and h.get("levels"):
        return (f"{verdict} ({h['arm_b']} - {h['arm_a']}) at each level - at {h['reference']} on {h['outcome']}: "
                f"{h['k']} contrast(s) of {len(h['contrasts'])}, Holm within; p={_g(h['p'])} p_holm={_g(h['p_holm'])} "
                f"-- {h['reason']}")
    if h["kind"] == "interaction":
        contrast = f"({h['arm_d']} - {h['arm_c']}) - ({h['arm_b']} - {h['arm_a']})"
        delta = "estimate"
    else:
        contrast, delta = f"{h['arm_b']} - {h['arm_a']}", "delta"
    return (f"{verdict} {contrast} on {h['outcome']}: "
            f"n={h['n']} {delta}={_fmt(h['delta'])} CI{level}=[{_fmt(lo)}, {_fmt(hi)}] d_z={_fmt(h['d_z'], 3)} "
            f"power={_fmt(h['power'], 3)} p={_g(h['p'])} p_holm={_g(h['p_holm'])} -- {h['reason']}")


def _contrast_line(h: dict[str, Any], c: dict[str, Any]) -> str:
    """One contrast of an arm x levels interaction: ``<level> - <reference>``, with its within-hypothesis Holm p."""
    lo, hi = c["ci"]
    level = round(100 * h["ci_level"])
    if c["p"] is None:
        state = "untested (fewer than 2 seeds)"
    else:
        state = "clears" if c["clears"] else "does not clear"
    return (f"contrast {c['label']} - {c['vs']}: n={c['n']} (dropped {c['dropped']}) estimate={_fmt(c['estimate'])} "
            f"CI{level}=[{_fmt(lo)}, {_fmt(hi)}] d_z={_fmt(c['d_z'], 3)} power={_fmt(c['power'], 3)} p={_g(c['p'])} "
            f"p_holm(within)={_g(c['p_holm'])} {state}")


def render_hypotheses(result: dict[str, Any]) -> str:
    """One PASS or FAIL line per pre-registered hypothesis of :func:`compare`'s result; an arm x levels interaction
    adds one indented line per contrast below its own."""
    hyps = result.get("hypotheses") or []
    if not hyps:
        return "pre-registered hypotheses: none (experiment.hypotheses is empty)"
    n_primary = sum(1 for h in hyps if h["primary"])
    note = ""
    if any(h["kind"] == "interaction" for h in hyps):
        note = "; an interaction enters it once, with its smallest within-hypothesis Holm p"
    lines = [f"pre-registered hypotheses (alpha {result.get('alpha', ALPHA)}; Holm across {n_primary} primary{note}):"]
    for h in hyps:
        lines.append(f"  {_hypothesis_line(h)}")
        if h["kind"] == "interaction" and h.get("levels"):
            lines.extend(f"      {_contrast_line(h, c)}" for c in h["contrasts"])
    return "\n".join(lines)


def _effect_line(label: str, e: dict[str, Any]) -> str:
    lo, hi = e["ci"]
    return (f"  {label}: mean={_fmt(e['mean'])} CI95=[{_fmt(lo)}, {_fmt(hi)}] sign p={_fmt(e['sign_test_p'], 3)} "
            f"(+{e['positive']}/-{e['negative']}/={e['ties']})")


def _render_factorial(f: dict[str, Any]) -> list[str]:
    fa, fb = f["factor_a"], f["factor_b"]
    lines = [f"factorial 2x2 on {f['outcome']} (descriptive, no verdict; n={f['n']}, dropped {f['dropped']}): "
             f"A={fa['path']} {_canon(fa['levels'][0])} -> {_canon(fa['levels'][1])}, "
             f"B={fb['path']} {_canon(fb['levels'][0])} -> {_canon(fb['levels'][1])}; "
             "cells " + ", ".join(f"{k}={arm}" for k, arm in f["cells"].items())]
    lines.append(_effect_line("main A", f["main_a"]))
    lines.append(_effect_line("main B", f["main_b"]))
    lines.append(_effect_line("interaction A x B", f["interaction"]))
    return lines


def _render_tier_did(t: dict[str, Any]) -> list[str]:
    lines = [f"per-tier difference-in-differences (exploratory; tiers {', '.join(t['tiers'])}):"]
    for row in t["rows"]:
        parts = []
        for tier, s in row["per_tier"].items():
            lo, hi = s["ci95"]
            parts.append(f"delta {tier}={_fmt(s['delta'])} CI95=[{_fmt(lo)}, {_fmt(hi)}] (n={s['n']})")
        lines.append(f"  {row['outcome']}  {row['arm_a']} vs {row['arm_b']}: " + "; ".join(parts))
        for d in row["differences"]:
            lo, hi = d["ci95"]
            lines.append(f"    {d['tier_b']} - {d['tier_a']}: did={_fmt(d['did'])} CI95=[{_fmt(lo)}, {_fmt(hi)}] "
                         f"(n={d['n']}, sign p={_fmt(d['sign_test_p'], 3)})")
    return lines


def render(result: dict[str, Any]) -> str:
    """A compact text rendering of :func:`compare`'s result."""
    lines = [
        f"experiment {result['experiment']}  arms: {', '.join(result['arms'])}  seeds: {result['seeds']}",
        f"independent variables: {', '.join(result['independent_variables']) or '(none)'}",
        f"primary: {result['primary']}",
    ]
    for c in result["comparisons"]:
        lines.append(f"  {c['arm_a']} vs {c['arm_b']}: {_stat_line(c['primary'])}")
    if result.get("hypotheses"):
        lines.append(render_hypotheses(result))
    if result.get("factorial"):
        lines.extend(_render_factorial(result["factorial"]))
    if result.get("tier_did"):
        lines.extend(_render_tier_did(result["tier_did"]))
    if result["secondaries"]:
        lines.append("secondary (exploratory):")
        for c in result["comparisons"]:
            for s in c["secondary"]:
                lines.append(f"  {s['path']}  {c['arm_a']} vs {c['arm_b']}: {_stat_line(s)}")
    if result.get("curves"):
        lines.append(f"daily curves of {result['primary']} (mean over seeds; shown, not tested):")
        for arm, curve in result["curves"].items():
            lines.append(f"  {arm}: " + "  ".join(f"d{d}={_fmt(v, 3)}" for d, v in curve.items()))
    if result.get("degenerate_pair") is not None:
        lines.append(f"degenerate_pair: {result['degenerate_pair']}")
    for w in result["warnings"]:
        lines.append(f"warning: {w}")
    for n in result["notes"]:
        lines.append(f"note: {n}")
    return "\n".join(lines)
