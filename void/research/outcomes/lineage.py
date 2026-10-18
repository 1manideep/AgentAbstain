"""Lineage outcomes (MEMORY_EVOLUTION 4, 6.3, 7.1): lineage fitness ``F_L``, net worth, cohort-by-generation fitness
and audit-exam accuracy, and the deepest generation reached.

``mean_balance`` averages the living only, so an arm that bankrupts its poor agents scores higher on it. The outcomes
here count every agent the run ever held, through the parent links of the ``agents`` table.

Founders and lineages
    A *founder* is an agent without a parent: the generation-0 roster, arrivals, and the fresh replacements the
    kernel spawns into an empty world (generation 0, no parent), each of which counts as its own founder. Every other
    agent descends from exactly one founder by following ``agents.parent_id`` upwards; a founder's *lineage* is that
    founder and all its descendants.

Agent-days
    An agent born at tick ``b`` is alive over ticks ``b+1 .. e``, where ``e`` is its ``died_tick``, or the run's last
    tick (``world_kv.tick``, the last committed tick) while it lives; its agent-days are ``(e - b) / ticks_per_day``.
    ``end = last tick + 1`` for the living would be right if ticks counted from 0, but the first simulated tick is 1
    and the roster is born at tick 0 (setup), so it would credit every founder with a tick it never lived (a founder
    alive through a D-day run would score ``D + 1/ticks_per_day``). With the half-open span ``(b, e]`` it scores
    exactly ``D``.

Registered outcomes (``end_of_run_outcome(run, "<name>[.<arg>]")``)
    ``lineage_fitness``       ``F_L``: agent-days summed over each founder's lineage, averaged over founders.
                              ``lineage_fitness.founder.<name or agent id>`` is one founder's lineage sum (the mean
                              over the founders of that name, if a fresh founder reused a name).
    ``net_worth``             per lineage, the living members' balances plus the estates its dead members passed on
                              (their ``estate_out`` ledger amounts), averaged over founders, in USD; the same
                              ``founder.<name or agent id>`` argument.
    ``cohort_fitness.<g>``    mean agent-days of the generation-``g`` agents (bare: every agent).
    ``cohort_exam_accuracy.<g>`` audit-exam accuracy of the generation-``g`` agents, graded items only (the filter of
                              :func:`void.research.analysis.exam_counts`; bare: every agent, which is ``exam_accuracy``).
    ``max_generation``        the deepest generation of any agent the run held.

Each but ``cohort_exam_accuracy`` also has a per-day version for the daily curves of ``compare``: the same quantity as
it stood at the end of day ``d`` (tick ``d * ticks_per_day``, or the last tick if the run ended earlier), over the
agents born by then. A balance at that tick is the end balance minus the ledger deltas booked after it (every balance
change goes through the wallet ledger); on the last day each equals its end-of-run value.

Caveat for cohort slopes (H3, H9): a later generation is born later and is censored at the run end, so its mean
agent-days is lower for that reason alone. Compare slopes between arms of the same length, not raw levels.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from void.config import micro_to_usd
from void.research.analysis import _graded_audit_sql, _has_tables, _last_tick, connect, run_config
from void.research.outcomes import register

__all__ = [
    "cohort_exam_accuracy",
    "cohort_fitness",
    "cohort_table",
    "lineage_fitness",
    "lineage_table",
    "max_generation",
    "net_worth",
]

FOUNDER_ARG = "founder"


@dataclass(frozen=True)
class _Member:
    agent_id: str
    name: str
    parent_id: str | None
    generation: int
    born_tick: int
    died_tick: int | None  # None while alive at the cutoff
    balance: int           # micro-dollars at the cutoff
    estate: int            # micro-dollars passed on at death, by the cutoff


@dataclass(frozen=True)
class _World:
    ticks_per_day: int
    cutoff: int
    members: dict[str, _Member]
    root: dict[str, str]   # agent_id -> founder agent_id

    def agent_days(self, m: _Member) -> float:
        end = self.cutoff if m.died_tick is None else m.died_tick
        return max(0, end - m.born_tick) / self.ticks_per_day

    @property
    def founders(self) -> list[_Member]:
        return [m for aid, m in self.members.items() if self.root[aid] == aid]


def _load(run_dir: str | Path, day: int | None = None) -> _World:
    """Every agent born by the cutoff (the run's last tick, or the end of ``day``), as it stood then."""
    with connect(run_dir) as db:
        rows = db.execute("SELECT agent_id, name, parent_id, generation, born_tick, died_tick, balance FROM agents "
                          "ORDER BY born_tick, agent_id").fetchall()
        if not rows:
            return _World(ticks_per_day=1, cutoff=0, members={}, root={})
        tpd = int(run_config(run_dir).run.ticks_per_day)
        last = max([_last_tick(db)] + [int(r["born_tick"]) for r in rows]
                   + [int(r["died_tick"]) for r in rows if r["died_tick"] is not None])
        cutoff = last if day is None else min(int(day) * tpd, last)
        later = {str(r["agent_id"]): int(r["s"]) for r in db.execute(
            "SELECT agent_id, SUM(delta) AS s FROM wallet_ledger WHERE tick > ? GROUP BY agent_id", (cutoff,))}
        estates = {str(r["agent_id"]): -int(r["s"]) for r in db.execute(
            "SELECT agent_id, SUM(delta) AS s FROM wallet_ledger WHERE kind='estate_out' AND tick <= ? GROUP BY agent_id",
            (cutoff,))}
    members: dict[str, _Member] = {}
    for r in rows:
        aid = str(r["agent_id"])
        if int(r["born_tick"]) > cutoff:
            continue
        died = None if r["died_tick"] is None or int(r["died_tick"]) > cutoff else int(r["died_tick"])
        members[aid] = _Member(
            agent_id=aid, name=str(r["name"]), parent_id=None if r["parent_id"] is None else str(r["parent_id"]),
            generation=int(r["generation"]), born_tick=int(r["born_tick"]), died_tick=died,
            balance=int(r["balance"]) - later.get(aid, 0), estate=estates.get(aid, 0))
    return _World(ticks_per_day=tpd, cutoff=cutoff, members=members, root=_roots(members))


def _roots(members: dict[str, _Member]) -> dict[str, str]:
    """The founder of every member: the top of its parent chain (a parent the table does not hold ends the chain)."""
    root: dict[str, str] = {}
    for aid in members:
        chain: list[str] = []
        cur = aid
        while cur not in root:
            chain.append(cur)
            parent = members[cur].parent_id
            if parent is None or parent not in members or parent in chain:
                root[cur] = cur
                break
            cur = parent
        top = root[cur]
        for a in chain:
            root.setdefault(a, top)
    return root


def _founder_ids(world: _World, arg: str | None) -> list[str]:
    """Every founder, or those a ``founder.<name or agent id>`` argument names (none when it names no founder)."""
    founders = world.founders
    if arg is None:
        return [f.agent_id for f in founders]
    head, dot, key = arg.partition(".")
    if head != FOUNDER_ARG or not dot or not key:
        raise ValueError(f"lineage argument {arg!r} must be 'founder.<name or agent id>'")
    return [f.agent_id for f in founders if key in (f.agent_id, f.name)]


def _per_founder(world: _World, arg: str | None, value: Callable[[_World, _Member], float]) -> float | None:
    """``value`` summed over each selected founder's lineage, averaged over those founders (None when there are none)."""
    ids = _founder_ids(world, arg)
    if not ids:
        return None
    totals = dict.fromkeys(ids, 0.0)
    for aid, m in world.members.items():
        if world.root[aid] in totals:
            totals[world.root[aid]] += value(world, m)
    return sum(totals.values()) / len(ids)


def _worth_usd(world: _World, m: _Member) -> float:
    """What a member adds to its lineage's net worth: its balance while it lives, plus any estate it passed on."""
    return micro_to_usd((m.balance if m.died_tick is None else 0) + m.estate)


def _generation(arg: str | None) -> int | None:
    if arg is None:
        return None
    try:
        return int(arg)
    except ValueError:
        raise ValueError(f"cohort argument {arg!r} must be a generation number") from None


# --- the outcomes ------------------------------------------------------------------------------------------------
def _lineage_fitness_on_day(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    return _per_founder(_load(run_dir, day), arg, _World.agent_days)


def _net_worth_on_day(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    return _per_founder(_load(run_dir, day), arg, _worth_usd)


def _cohort_fitness_on_day(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    g = _generation(arg)
    world = _load(run_dir, day)
    days = [world.agent_days(m) for m in world.members.values() if g is None or m.generation == g]
    return sum(days) / len(days) if days else None


def _max_generation_on_day(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    gens = [m.generation for m in _load(run_dir, day).members.values()]
    return float(max(gens)) if gens else None


@register("lineage_fitness", daily=_lineage_fitness_on_day)
def lineage_fitness(run_dir: str | Path, arg: str | None = None) -> float | None:
    """``F_L``: agent-days summed over each founder's lineage, averaged over founders (None without agents)."""
    return _lineage_fitness_on_day(run_dir, arg, None)


@register("net_worth", daily=_net_worth_on_day)
def net_worth(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Per lineage, living balances plus estates passed on, averaged over founders, in USD (None without agents)."""
    return _net_worth_on_day(run_dir, arg, None)


@register("cohort_fitness", daily=_cohort_fitness_on_day)
def cohort_fitness(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean agent-days of the generation-``arg`` agents (every agent when ``arg`` is None); None without any."""
    return _cohort_fitness_on_day(run_dir, arg, None)


@register("cohort_exam_accuracy")
def cohort_exam_accuracy(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Audit-exam accuracy (``correct / asked`` over graded items) of the generation-``arg`` agents; None without
    graded items. ``arg`` None pools every agent, which is :func:`void.research.analysis.exam_accuracy`."""
    g = _generation(arg)
    sql, params = _graded_audit_sql("COUNT(*) AS n, COALESCE(SUM(a.correct), 0) AS c", None)
    if g is not None:
        sql += " AND i.agent_id IN (SELECT agent_id FROM agents WHERE generation=?)"
        params += (g,)
    with connect(run_dir) as db:
        if not _has_tables(db, "exam_items", "exam_answers"):
            return None
        row = db.execute(sql, params).fetchone()
    asked = int(row["n"]) if row is not None else 0
    return int(row["c"]) / asked if asked else None


@register("max_generation", daily=_max_generation_on_day)
def max_generation(run_dir: str | Path, arg: str | None = None) -> float | None:
    """The deepest generation of any agent the run held (the founders are generation 0); None without agents."""
    return _max_generation_on_day(run_dir, arg, None)


# --- tables for inspection and cohort slopes ----------------------------------------------------------------------
def lineage_table(run_dir: str | Path, day: int | None = None) -> dict[str, dict[str, Any]]:
    """Per founder (by agent id): ``name``, ``members``, ``living``, ``agent_days`` and ``net_worth_usd`` of its
    lineage, at the run end or at the end of ``day``; the outcomes above are means over this table."""
    world = _load(run_dir, day)
    out: dict[str, dict[str, Any]] = {f.agent_id: {"name": f.name, "members": 0, "living": 0, "agent_days": 0.0,
                                                   "net_worth_usd": 0.0} for f in world.founders}
    for aid, m in world.members.items():
        row = out[world.root[aid]]
        row["members"] += 1
        row["living"] += int(m.died_tick is None)
        row["agent_days"] += world.agent_days(m)
        row["net_worth_usd"] += _worth_usd(world, m)
    return out


def cohort_table(run_dir: str | Path) -> dict[int, dict[str, Any]]:
    """Per generation: ``agents``, ``fitness`` (mean agent-days) and ``exam_accuracy`` (None without graded items),
    the rows a cohort slope on generation is fitted to."""
    world = _load(run_dir)
    days: dict[int, list[float]] = {}
    for m in world.members.values():
        days.setdefault(m.generation, []).append(world.agent_days(m))
    return {g: {"agents": len(d), "fitness": sum(d) / len(d), "exam_accuracy": cohort_exam_accuracy(run_dir, str(g))}
            for g, d in sorted(days.items())}
