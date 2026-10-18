"""Degradation ~ stress for every measured degradation, per tier and per architecture (exp_tiers robustness).

DESIGN §17 defines the ``exp_tiers`` outcome as "per-tier fit of degradation ~ stress, identical thresholds, observe,
equalized", and §8 stores the measured degradation on every ``llm_calls`` row. The built-in collapse fit
(:func:`void.research.analysis.recovered_collapse_stress`) uses ``action_regret`` only, which the scripted brain alone
records (a provider tier's rows hold NULL there), so a live tier never gets a collapse point. This module fits every
measure the loop records for every tier, and pools tiers by the ``tiers.<name>.architecture`` of the run's resolved
config (PLAN §4.2, architecture-dependent collapse: dense against mixture-of-experts).

Measures (:data:`MEASURES`), each oriented so that a higher value is worse:

* ``incoherence``: ``1 - text_coherence`` (only calls whose free text was long enough to be scored);
* ``invalid_action``: 1 when the kernel refused the action or the call returned no decision, else 0;
* ``perseveration``: 1 when the action repeats the previous one (type and target), else 0;
* ``action_entropy_w8``: Shannon entropy in bits of the agent's last eight action types (a flattening policy
  spreads over more action types);
* ``action_regret``: utility lost against the best available action (scripted tiers only).

Every fit is over decide calls (``purpose='decide'``) that recorded both the measure and the call's own
``llm_calls.stress``; a call is never placed at a group's mean stress. Bins are 0.1 wide (0.0, 0.1, ..., 1.0), the
same bins as :func:`void.research.analysis.collapse_curve`.

Registered outcomes (resolved by :func:`void.research.analysis.end_of_run_outcome`):

* ``degradation_onset.<measure>.<tier>``: the onset bin (see :func:`onset_from_curve`). Bins holding fewer than
  ``analysis.MIN_BIN_COUNT`` calls take no part; the baseline is the mean of the lowest-stress bin that qualifies;
  the onset is the first higher bin whose mean exceeds the baseline by at least ``max(0.1, baseline)`` (a rise of
  0.1 in the measure's own units, or a doubling once the baseline is above 0.1). None when no bin qualifies or the
  rise never happens within the observed stress range (right-censored).
* ``degradation_slope.<measure>.<tier>`` (or ``.<tier>``, measure ``incoherence``): the ordinary least-squares slope
  of the measure on stress over the individual calls, in measure units per unit of stress. None with fewer than
  two calls or when every call ran at the same stress.
* ``degradation_slope_by_architecture.<measure>.<dense|moe>`` (or ``.<dense|moe>``, measure ``incoherence``): the
  same slope over the calls of every tier whose resolved ``architecture`` matches, pooled call by call (not an
  average of per-tier slopes). A tier without the key in the resolved config counts as ``dense`` (the config default).

Argument parsing: a first segment naming a measure selects it and the rest is the tier (absent: every tier pooled);
any other argument is a tier name with the default measure ``incoherence``. ``degradation_onset`` parses its argument
the same way. An unknown tier, measure or architecture gives None, like the other registered outcomes.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from void.research import analysis
from void.research.analysis import _stress_bin, connect, run_config_dict
from void.research.outcomes import register

__all__ = [
    "ARCHITECTURES",
    "DEFAULT_MEASURE",
    "MEASURES",
    "ONSET_MIN_RISE",
    "degradation_curve",
    "degradation_onset",
    "degradation_points",
    "degradation_slope",
    "degradation_slope_by_architecture",
    "ols_slope",
    "onset_from_curve",
    "parse_measure_arg",
    "tier_architectures",
    "tiers_of_architecture",
]

# measure name -> the llm_calls column it is read from; ``incoherence`` is stored inverted (see _value)
MEASURES: dict[str, str] = {
    "incoherence": "text_coherence",
    "invalid_action": "invalid_action",
    "perseveration": "perseveration",
    "action_entropy_w8": "action_entropy_w8",
    "action_regret": "action_regret",
}
DEFAULT_MEASURE = "incoherence"
ARCHITECTURES = ("dense", "moe")
DEFAULT_ARCHITECTURE = "dense"  # TierConfig.architecture's default
ONSET_MIN_RISE = 0.1            # the onset rise is at least this far above the baseline, in the measure's units
_EPS = 1e-9                     # tolerance for "at least" on float means, and the stress span below which no slope


def _value(measure: str, raw: Any) -> float:
    return 1.0 - float(raw) if measure == "incoherence" else float(raw)


def parse_measure_arg(arg: str | None) -> tuple[str, str | None]:
    """``(measure, rest)`` from an outcome argument: ``"perseveration.alpha"`` gives ``("perseveration", "alpha")``,
    ``"perseveration"`` gives ``("perseveration", None)`` and ``"alpha"`` (not a measure) gives
    ``("incoherence", "alpha")``. ``None`` or ``""`` gives ``("incoherence", None)``."""
    if not arg:
        return DEFAULT_MEASURE, None
    head, _, rest = arg.partition(".")
    if head in MEASURES:
        return head, rest or None
    return DEFAULT_MEASURE, arg


# --- the data -------------------------------------------------------------------------------------------------
def degradation_points(run_dir: str | Path, tier: str | Sequence[str] | None = None,
                       measure: str = DEFAULT_MEASURE) -> list[tuple[float, float]]:
    """``(stress, value)`` of every decide call of ``tier`` (one name, several, or None for every tier) that recorded
    both its stress and ``measure``, in tick then call-id order. An empty sequence of tiers selects no call."""
    if measure not in MEASURES:
        raise ValueError(f"unknown degradation measure {measure!r}; one of {', '.join(MEASURES)}")
    column = MEASURES[measure]
    sql = (f"SELECT stress, {column} AS v FROM llm_calls WHERE purpose='decide' "
           f"AND stress IS NOT NULL AND {column} IS NOT NULL")
    params: tuple[Any, ...] = ()
    if isinstance(tier, str):
        sql += " AND tier=?"
        params = (tier,)
    elif tier is not None:
        tiers = tuple(tier)
        if not tiers:
            return []
        sql += f" AND tier IN ({','.join('?' * len(tiers))})"
        params = tiers
    with connect(run_dir) as db:
        rows = db.execute(sql + " ORDER BY tick, call_id", params).fetchall()
    return [(float(r["stress"]), _value(measure, r["v"])) for r in rows]


def _curve(points: Iterable[tuple[float, float]]) -> dict[float, dict[str, float]]:
    acc: dict[float, list[float]] = {}
    for stress, value in points:
        acc.setdefault(_stress_bin(stress), []).append(value)
    return {b: {"n": float(len(v)), "mean": sum(v) / len(v)} for b, v in sorted(acc.items())}


def degradation_curve(run_dir: str | Path, tier: str | Sequence[str] | None = None,
                      measure: str = DEFAULT_MEASURE) -> dict[float, dict[str, float]]:
    """Per 0.1-wide stress bin (``analysis._stress_bin`` of each call's own stress): ``{"n", "mean"}`` of ``measure``
    over the decide calls of ``tier`` (see :func:`degradation_points`), bins in ascending order, empty bins absent."""
    return _curve(degradation_points(run_dir, tier, measure))


# --- the fits -------------------------------------------------------------------------------------------------
def onset_from_curve(curve: dict[float, dict[str, float]], *, min_bin_count: int | None = None,
                     min_rise: float = ONSET_MIN_RISE) -> float | None:
    """The first bin, above the baseline bin, whose mean is at least ``baseline + max(min_rise, baseline)``.

    Only bins with ``n >= min_bin_count`` (default ``analysis.MIN_BIN_COUNT``) take part; the baseline is the mean of
    the lowest-stress such bin. None when no bin qualifies or no higher bin rises that far."""
    need = analysis.MIN_BIN_COUNT if min_bin_count is None else min_bin_count
    bins = [b for b, c in sorted(curve.items()) if c["n"] >= need]
    if not bins:
        return None
    base = float(curve[bins[0]]["mean"])
    rise = max(min_rise, base)
    for b in bins[1:]:
        if float(curve[b]["mean"]) - base >= rise - _EPS:
            return b
    return None


def ols_slope(points: Sequence[tuple[float, float]]) -> float | None:
    """Least-squares slope of ``y`` on ``x`` over ``(x, y)`` points; None with fewer than two points or when every
    ``x`` is the same (to within 1e-9)."""
    if len(points) < 2:
        return None
    xs = [p[0] for p in points]
    if max(xs) - min(xs) <= _EPS:
        return None
    n = len(points)
    mx = sum(xs) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in points)
    slope = sxy / sxx
    return slope if math.isfinite(slope) else None


def degradation_onset(run_dir: str | Path, tier: str | Sequence[str] | None = None, measure: str = DEFAULT_MEASURE, *,
                      min_bin_count: int | None = None, min_rise: float = ONSET_MIN_RISE) -> float | None:
    """:func:`onset_from_curve` of :func:`degradation_curve` for ``tier`` and ``measure``."""
    return onset_from_curve(degradation_curve(run_dir, tier, measure), min_bin_count=min_bin_count, min_rise=min_rise)


def degradation_slope(run_dir: str | Path, tier: str | Sequence[str] | None = None,
                      measure: str = DEFAULT_MEASURE) -> float | None:
    """:func:`ols_slope` of ``measure`` on stress over the decide calls of ``tier`` (every tier when None)."""
    return ols_slope(degradation_points(run_dir, tier, measure))


# --- architecture -------------------------------------------------------------------------------------------------
def tier_architectures(run_dir: str | Path) -> dict[str, str]:
    """``{tier: architecture}`` from the run's resolved config; a tier without the key is ``dense``."""
    tiers = run_config_dict(run_dir).get("tiers") or {}
    out: dict[str, str] = {}
    for name, spec in tiers.items():
        arch = spec.get("architecture") if isinstance(spec, dict) else None
        out[str(name)] = str(arch) if arch is not None else DEFAULT_ARCHITECTURE
    return out


def tiers_of_architecture(run_dir: str | Path, architecture: str) -> list[str]:
    """The tiers of the resolved config whose architecture is ``architecture``, sorted."""
    return sorted(t for t, a in tier_architectures(run_dir).items() if a == architecture)


def degradation_slope_by_architecture(run_dir: str | Path, architecture: str,
                                      measure: str = DEFAULT_MEASURE) -> float | None:
    """:func:`degradation_slope` over the pooled calls of every tier of ``architecture``; None when no tier has it."""
    tiers = tiers_of_architecture(run_dir, architecture)
    return degradation_slope(run_dir, tiers, measure) if tiers else None


# --- registered outcomes ------------------------------------------------------------------------------------------
@register("degradation_onset")
def _onset_outcome(run_dir: str | Path, arg: str | None) -> float | None:
    measure, tier = parse_measure_arg(arg)
    return degradation_onset(run_dir, tier, measure)


@register("degradation_slope")
def _slope_outcome(run_dir: str | Path, arg: str | None) -> float | None:
    measure, tier = parse_measure_arg(arg)
    return degradation_slope(run_dir, tier, measure)


@register("degradation_slope_by_architecture")
def _slope_by_architecture_outcome(run_dir: str | Path, arg: str | None) -> float | None:
    measure, architecture = parse_measure_arg(arg)
    if architecture not in ARCHITECTURES:
        return None
    return degradation_slope_by_architecture(run_dir, architecture, measure)
