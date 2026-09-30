"""Arm comparison for the research harness (DESIGN §17): ``void compare --exp <name>``.

An experiment directory holds one subdirectory per arm, each holding ``seed_<n>/`` runs (or a single
run directly). Before any statistics the arms are checked for comparability and the comparison is
REFUSED (:class:`IncomparableRuns`) when

* the resolved configs differ at any path outside the union of the arms'
  ``experiment.independent_variables`` (``run.name`` and ``experiment.arm`` are always allowed);
* the seed sets differ between arms, or runs within an arm differ outside ``run.seed``;
* ``entropy.degeneration.mode`` is not ``observe`` in every arm;
* for ``exp_tiers`` / ``exp_scarcity`` / ``exp_ladder``, ``economy.world_pricing`` is not ``equalized``;
* for ``exp_tiers`` / ``exp_ladder``, the rostered tiers differ in declared thresholds, ``max_tokens`` or ``effort``, or
  the arms differ in ``entropy.drain``.

Arms are paired by seed. For the primary outcome (``experiment.primary_outcome`` of the first arm unless
overridden) each pair of arms gets n, per-arm means, the paired mean delta (b - a), Cohen's d_z, a paired
bootstrap 95% CI (2000 resamples, ``random.Random(0)``) and an exact two-sided sign test. Secondaries get
the same statistics flagged ``exploratory``. Daily curves are reported, not tested.
"""

from __future__ import annotations

import json
import math
import random
import re
import statistics
from itertools import combinations
from pathlib import Path
from typing import Any

import yaml

from void.research.analysis import CONFIG_FILE, DAILY, connect, end_of_run_outcome, load_metrics, resolve_path

__all__ = [
    "ALWAYS_ALLOWED",
    "IncomparableRuns",
    "compare",
    "config_diff",
    "covered_by",
    "discover_runs",
    "paired_stats",
    "render",
    "sign_test_p",
]

ALWAYS_ALLOWED = ("run.name", "experiment.arm")
EQUALIZED_EXPERIMENTS = ("exp_tiers", "exp_scarcity", "exp_ladder")
TIER_INVARIANTS = ("collapse_temperature", "max_temperature", "entropy_budget_max", "max_tokens", "effort")
N_BOOT = 2000
BOOT_SEED = 0
JACCARD_DEGENERATE = 0.8
_RETRIEVED_KEYS = ("retrieved_titles", "retrieved", "retrieved_ids", "memories", "entry_ids")


class IncomparableRuns(RuntimeError):
    """The runs under an experiment directory are not a fair comparison."""


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
    data = yaml.safe_load((run_dir / CONFIG_FILE).read_text()) or {}
    if not isinstance(data, dict):
        raise IncomparableRuns(f"{run_dir / CONFIG_FILE} does not hold a mapping")
    return data


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
    stats = paired_stats(pairs, n_boot=n_boot, seed=boot_seed)
    return {"path": path, "arm_a": a, "arm_b": b, **stats, "dropped": dropped, "per_seed": per_seed}


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


def _daily_curves(arms: dict[str, dict[int, Path]], names: list[str], seeds: list[int], path: str) -> dict[str, dict[str, float | None]]:
    """Per arm and day, the mean over seeds of the primary: a per-day run-level aggregate when the path has one
    (:data:`DAILY`), else the value on the day's last tick row, else on the day row."""
    head, _, rest = path.partition(".")
    curves: dict[str, dict[str, float | None]] = {}
    for arm in names:
        per_day: dict[int, list[float]] = {}
        for seed in seeds:
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
def compare(exp_dir: str | Path, primary: str | None = None, *, n_boot: int = N_BOOT, boot_seed: int = BOOT_SEED) -> dict[str, Any]:
    """Compare the arms under ``exp_dir`` (see the module docstring); raises :class:`IncomparableRuns`."""
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
    if exp_name.startswith(EQUALIZED_EXPERIMENTS):
        for arm in names:
            wp = resolve_path(reps[arm], "economy.world_pricing")
            if wp != "equalized":
                raise IncomparableRuns(f"{exp_name}: arm {arm!r} has economy.world_pricing={wp!r}; 'equalized' is required")
    violations: list[str] = []
    for a, b in combinations(names, 2):
        for path in config_diff(reps[a], reps[b]):
            if path in ALWAYS_ALLOWED or covered_by(path, ivs):
                continue
            violations.append(f"{a} vs {b}: {path}")
    if violations:
        raise IncomparableRuns(f"configs differ outside experiment.independent_variables {ivs}: " + "; ".join(violations))
    if exp_name.startswith(("exp_tiers", "exp_ladder")):  # rungs may differ only in hidden capability
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
    values = {arm: {seed: {p: end_of_run_outcome(arms[arm][seed], p) for p in paths} for seed in seeds} for arm in names}

    comparisons: list[dict[str, Any]] = []
    for a, b in combinations(names, 2):
        comparisons.append({
            "arm_a": a, "arm_b": b,
            "primary": _outcome_stats(a, b, primary, values, seeds, n_boot=n_boot, boot_seed=boot_seed),
            "secondary": [{**_outcome_stats(a, b, s, values, seeds, n_boot=n_boot, boot_seed=boot_seed), "exploratory": True}
                          for s in secondaries],
        })
    warnings: list[str] = []
    models: dict[str, dict[int, tuple[str, ...]]] = {}
    for arm in names:
        models[arm] = {seed: _served_models(arms[arm][seed]) for seed in seeds}
        if len(set(models[arm].values())) > 1:
            warnings.append(f"arm {arm!r}: served models differ across runs: " + ", ".join(f"seed {s}: {list(m)}" for s, m in models[arm].items()))
    notes: list[str] = []
    degenerate: bool | None = None
    if exp_name.startswith("exp_memory"):
        degenerate, note = _memory_degenerate_pair(arms, names, seeds)
        notes.append(note)
    return {
        "experiment": exp_name, "exp_dir": str(exp_dir), "arms": names, "seeds": seeds, "n_seeds": len(seeds),
        "independent_variables": ivs, "primary": primary, "secondaries": secondaries,
        "values": {arm: {str(seed): values[arm][seed] for seed in seeds} for arm in names},
        "comparisons": comparisons, "warnings": warnings, "notes": notes,
        "degenerate_pair": degenerate, "served_models": {arm: {str(s): list(m) for s, m in ms.items()} for arm, ms in models.items()},
        "curves": _daily_curves(arms, names, seeds, primary),
    }


# --- rendering -------------------------------------------------------------------------------------------
def _fmt(v: float | None, nd: int = 4) -> str:
    return "n/a" if v is None else f"{v:.{nd}f}"


def _stat_line(s: dict[str, Any]) -> str:
    lo, hi = s.get("ci95", [None, None])
    return (f"n={s['n']} (dropped {s.get('dropped', 0)})  mean {s['arm_a']}={_fmt(s['mean_a'])}  {s['arm_b']}={_fmt(s['mean_b'])}  "
            f"delta={_fmt(s['delta'])}  d_z={_fmt(s['d_z'], 3)}  CI95=[{_fmt(lo)}, {_fmt(hi)}]  "
            f"sign p={_fmt(s['sign_test_p'], 3)} (+{s['positive']}/-{s['negative']}/={s['ties']})")


def render(result: dict[str, Any]) -> str:
    """A compact text rendering of :func:`compare`'s result."""
    lines = [
        f"experiment {result['experiment']}  arms: {', '.join(result['arms'])}  seeds: {result['seeds']}",
        f"independent variables: {', '.join(result['independent_variables']) or '(none)'}",
        f"primary: {result['primary']}",
    ]
    for c in result["comparisons"]:
        lines.append(f"  {c['arm_a']} vs {c['arm_b']}: {_stat_line(c['primary'])}")
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
