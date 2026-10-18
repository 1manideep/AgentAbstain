"""Spread and gate outcomes (DESIGN §17): the censored time to 50% tracer reach for ``exp_spread``, and the mean
balance at generation >= G for ``exp_gate``.

Censored time to 50% reach
    The built-in ``time_to_50pct_tracer_reach`` (:func:`void.research.analysis.time_to_50pct_tracer_reach`) is the
    first tick whose row has ``tracer_reach / population >= 0.5``, and None when no row gets there. In a short
    ``exp_spread`` run most seeds never get there, and ``compare`` drops every pair with a None, so the arms would be
    compared on the few seeds that spread fastest. Here a run that never reaches half the population is *right
    censored* instead: its value is the run's last tick + 1, the earliest tick at which 50% could still have been
    reached, so it is a lower bound on the true time. The comparison then keeps every seed; a seed censored in both
    arms is a tie, and an arm that never spreads is scored no slower than "just after the end", which understates its
    delay rather than inventing one. ``tracer_reach_censored`` says which runs were censored, so the share of
    censored seeds per arm is reported next to the times.

    The rows are the tick rows of ``metrics.jsonl`` (the ``metrics`` table without one); the run's last tick is the
    tick of the last row, which is ``run.days * run.ticks_per_day`` for a run that completed (``compare`` refuses
    runs that did not). The reach test is exactly the built-in's, so a reached run has the same value under both
    names. Without a row carrying ``tracer_reach`` (``experiment.tracer`` disabled) both outcomes are None.

Balance at generation >= G
    DESIGN §17 asks ``exp_gate`` for the balance at generation G >= 4: the gate's effect should show in the lineages
    that went through several rounds of selection, not only in the founders. ``balance_gen_ge.<G>`` is the mean
    balance in USD of the living agents of generation G or deeper (the founders are generation 0); ``mean_balance``
    is the same mean over every living agent, so ``balance_gen_ge.0`` equals it (unrounded; the metrics row rounds
    it). Balances are exact micro-dollar values from the ``agents`` table. None when no living agent is that
    deep: always in a run too short for G generations (a one-day run, for G = 4), and in a run whose deep lineages
    died out. ``compare`` drops those pairs.

Registered outcomes (``end_of_run_outcome(run, "<name>[.<arg>]")``)
    ``time_to_50pct_tracer_reach_censored``  first tick with reach / population >= 0.5, else the last tick + 1.
    ``tracer_reach_censored``                1.0 if 50% was never reached (the time above is censored), else 0.0.
    ``balance_gen_ge[.<G>]``                 mean balance (USD) of the living agents with generation >= G (default
                                             :data:`DEFAULT_GENERATION`); None without any.

The first two take no argument and ``balance_gen_ge`` takes an integer: anything else raises ``ValueError`` (a typo
in a config's outcome list fails loudly instead of giving None in every run).

Daily versions (``compare``'s curves): the two tracer outcomes on day ``d`` are computed over the tick rows of days
``1 .. d``, so the time is censored at the last tick of day ``d`` + 1 until 50% is reached and then stays at the tick
it was reached; ``balance_gen_ge`` on day ``d`` is the mean over the agents alive at the end of day ``d``, with their
balances as they stood then (:func:`void.research.outcomes.lineage`'s reconstruction from the wallet ledger). On the
last day each equals its end-of-run value.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from void.config import micro_to_usd
from void.research.analysis import load_metrics
from void.research.outcomes import register
from void.research.outcomes.lineage import _load

__all__ = [
    "DEFAULT_GENERATION",
    "HALF_REACH",
    "balance_gen_ge",
    "half_reach",
    "time_to_50pct_tracer_reach_censored",
    "tracer_reach_censored",
]

HALF_REACH = 0.5
DEFAULT_GENERATION = 4  # DESIGN §17: balance at generation G >= 4


def half_reach(tick_rows: Sequence[dict[str, Any]]) -> tuple[float, bool] | None:
    """``(tick, censored)`` over tick rows in tick order: the first tick with ``tracer_reach / population >= 0.5`` and
    False, else the last row's tick + 1 and True. None when no row carries ``tracer_reach`` (the tracer is off)."""
    if not any(r.get("tracer_reach") is not None for r in tick_rows):
        return None
    for row in tick_rows:
        pop = row.get("population") or 0
        reach = row.get("tracer_reach")
        if pop and reach is not None and reach / pop >= HALF_REACH:
            return float(row["tick"]), False
    return float(int(tick_rows[-1]["tick"]) + 1), True


def _no_arg(name: str, arg: str | None) -> None:
    if arg is not None:
        raise ValueError(f"outcome {name!r} takes no argument (got {arg!r})")


def _half_reach_on_day(run_dir: str | Path, day: int | None) -> tuple[float, bool] | None:
    tick_rows, _ = load_metrics(run_dir)
    if day is not None:
        tick_rows = [r for r in tick_rows if int(r.get("day", 0)) <= int(day)]
    return half_reach(tick_rows)


def _time_to_half_on_day(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    _no_arg("time_to_50pct_tracer_reach_censored", arg)
    hit = _half_reach_on_day(run_dir, day)
    return None if hit is None else hit[0]


def _censored_on_day(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    _no_arg("tracer_reach_censored", arg)
    hit = _half_reach_on_day(run_dir, day)
    return None if hit is None else float(hit[1])


def _generation_arg(arg: str | None) -> int:
    if arg is None:
        return DEFAULT_GENERATION
    try:
        return int(arg)
    except ValueError:
        raise ValueError(f"balance_gen_ge argument {arg!r} must be a generation number") from None


def _balance_gen_ge_on_day(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    g = _generation_arg(arg)
    world = _load(run_dir, day)
    balances = [m.balance for m in world.members.values() if m.died_tick is None and m.generation >= g]
    return micro_to_usd(sum(balances)) / len(balances) if balances else None


@register("time_to_50pct_tracer_reach_censored", daily=_time_to_half_on_day)
def time_to_50pct_tracer_reach_censored(run_dir: str | Path, arg: str | None = None) -> float | None:
    """First tick with ``tracer_reach / population >= 0.5``, else the run's last tick + 1 (right censored); None
    without the tracer."""
    return _time_to_half_on_day(run_dir, arg, None)


@register("tracer_reach_censored", daily=_censored_on_day)
def tracer_reach_censored(run_dir: str | Path, arg: str | None = None) -> float | None:
    """1.0 if the tracer never reached half the population (the time is censored), else 0.0; None without the
    tracer."""
    return _censored_on_day(run_dir, arg, None)


@register("balance_gen_ge", daily=_balance_gen_ge_on_day)
def balance_gen_ge(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean balance (USD) of the living agents with generation >= ``arg`` (default 4); None without any."""
    return _balance_gen_ge_on_day(run_dir, arg, None)
