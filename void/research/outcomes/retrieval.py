"""Retrieval outcomes (MEMORY_EVOLUTION §7.2, "Retrieval") and the memory-command rates (§10.2, G1 "valid-command
rate at least 90%").

Every decide call row (``llm_calls``, purpose ``decide``) carries in its JSON ``extras`` what retrieval put in front of
the agent for that call: ``retrieved_titles`` (one per note shown), and from the loop memory audit on also
``retrieved_hop_hist`` (graph depth at which the walk reached each note -> notes) and ``retrieved_mean_age`` (mean
ticks since those notes were written; 0.0 when nothing was retrieved). A row whose extras carry no
``retrieved_titles`` (no extras at all, or a run older than the retrieval log) says nothing about retrieval and is left
out of every retrieval figure. The amnesic control logs an empty list on every call.

Registered outcomes (resolved by :func:`void.research.analysis.end_of_run_outcome`):

* ``empty_retrieval_rate[.<tier>]``: decide calls that retrieved no note, over decide calls. 1.0 in the amnesic arm.
* ``mean_retrieved_age_ticks[.<tier>]``: mean age in ticks of the retrieved notes, pooled over notes (each call's
  ``retrieved_mean_age`` weighted by how many notes it retrieved). None when no call retrieved a note, or when the
  calls that did carry no ``retrieved_mean_age`` (a run older than the loop memory audit).
* ``retrieved_depth_share[.<tier>]``: share of the retrieved notes the graph walk reached at depth 1 or more (not
  the entry points), pooled over notes from ``retrieved_hop_hist``. None without that key or without a retrieved
  note. In ``vector`` retrieval mode every note is an entry point, so the share is 0.0.
* ``retrieved_cited_rate[.<tier>]``: decide calls that retrieved a note and *cited* one, over decide calls that
  retrieved a note (None without such a call). A call cites a retrieved note when the note's title, or a resource
  node id named in that title (``n3`` in ``Node n3`` or ``Foraging at node n3 on day 2``), appears in the call's
  thought, in the target or text of the action the brain returned, or in the text of a ``talk`` event the agent
  emitted on that tick. Matching ignores case and needs a word boundary on each alphanumeric end, so ``n1`` is not
  found inside ``n12``. The node ids are the run's ``resource_nodes`` (any ``n<digits>`` token when that table is
  empty). Per call rather than per note (§7.2 words it per note), so a call that names two of its notes counts once.
* ``maintenance_valid_command_rate``: ``memory_commands`` rows of purpose ``maintain`` that succeeded (``ok``), over
  all such rows: the G1 valid-command rate. None when there are none (maintenance off, or a strategy that issues no
  command, like the scripted ``noop``).
* ``tick_command_refusal_rate``: ``memory_commands`` rows of purpose ``tick`` (a decision's ``remember`` and
  ``revise_self``, which the vault limits can refuse) that failed, over all such rows. None when there are none (the
  amnesic control writes no tick rows).

Caveats. ``retrieved_cited_rate`` is a weak proxy: an agent can act on a note without naming it, and the scripted
brain's thought never names one, so on scripted runs it mostly measures talk that quotes a remembered node. The
action is read from the logged response text (the JSON the brain returned, before any induced degeneration rewrote
the action; the thought column is the logged thought after it); when prompts are not logged that text is cut at 4000
characters, and a response that no longer parses as JSON contributes no action, only its thought and talk.

An argument on the four retrieval outcomes is a tier name (calls of that tier only; a tier without calls gives None);
the two command rates take no argument (any argument gives None). Daily versions (``compare``'s curves): the same
figure over the calls, or the commands, of day ``d``'s ticks. The nightly maintenance at the start of day ``d`` (about
day ``d - 1``) runs on a tick of day ``d`` and so counts on day ``d``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from void.research.analysis import _call_filter, _has_tables, connect
from void.research.outcomes import register

__all__ = [
    "CallRetrieval",
    "cited",
    "decide_retrievals",
    "empty_retrieval_rate",
    "maintenance_valid_command_rate",
    "mean_retrieved_age_ticks",
    "mentions",
    "node_ids_in",
    "response_action",
    "retrieved_cited_rate",
    "retrieved_depth_share",
    "tick_command_refusal_rate",
]

_NODE_TOKEN_RE = re.compile(r"(?<![a-z0-9])n\d+(?![a-z0-9])")


@dataclass(frozen=True)
class CallRetrieval:
    """One decide call that logged its retrieval: what it was shown and what it said and did."""

    call_id: str
    agent_id: str
    tick: int
    titles: tuple[str, ...]           # ``retrieved_titles``, in retrieval order
    hop_hist: dict[int, int] | None   # ``retrieved_hop_hist`` (graph depth -> notes); None when not logged
    mean_age: float | None            # ``retrieved_mean_age``; None when not logged
    thought: str
    action_target: str | None         # from the logged response; None when it names none or does not parse
    action_text: str | None
    talk: tuple[str, ...]             # texts of the agent's ``talk`` events on this tick


# --- text matching ------------------------------------------------------------------------------------------
@lru_cache(maxsize=4096)
def _pattern(needle: str) -> re.Pattern[str]:
    """``needle`` (lower case) with a word boundary required on each end that is a letter or digit."""
    head = r"(?<![a-z0-9])" if needle[:1].isalnum() else ""
    tail = r"(?![a-z0-9])" if needle[-1:].isalnum() else ""
    return re.compile(head + re.escape(needle) + tail)


def _found(lower_hay: str, needle: str) -> bool:
    n = needle.strip().lower()
    return bool(n) and _pattern(n).search(lower_hay) is not None


def mentions(haystack: str, needle: str) -> bool:
    """Whether ``needle`` appears in ``haystack`` ignoring case, not run into a letter or digit on either side; an
    empty or blank needle is never found."""
    return _found(haystack.lower(), needle)


def node_ids_in(title: str, node_ids: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """The resource node ids ``title`` names (sorted): those of ``node_ids`` that :func:`mentions` finds in it, or,
    with no ``node_ids``, every ``n<digits>`` token in it (lower-cased)."""
    if node_ids:
        return sorted(nid for nid in node_ids if mentions(title, nid))
    return sorted(set(_NODE_TOKEN_RE.findall(title.lower())))


def cited(call: CallRetrieval, node_ids: frozenset[str] | set[str] = frozenset()) -> bool:
    """Whether ``call`` names one of its retrieved notes (its title, or a node id named in its title) in its thought,
    its action's target or text, or its talk on that tick (see the module docstring)."""
    hay = "\n".join(x for x in (call.thought, call.action_target or "", call.action_text or "", *call.talk) if x).lower()
    if not hay:
        return False
    for title in call.titles:
        if _found(hay, title) or any(_found(hay, nid) for nid in node_ids_in(title, node_ids)):
            return True
    return False


# --- reading the run ----------------------------------------------------------------------------------------
def _json_object(text: str) -> dict[str, Any] | None:
    """``text`` as a JSON object, or the span from its first ``{`` to its last ``}`` as one; None otherwise."""
    candidates = [text]
    lo, hi = text.find("{"), text.rfind("}")
    if 0 <= lo < hi and (lo, hi) != (0, len(text) - 1):
        candidates.append(text[lo:hi + 1])
    for c in candidates:
        try:
            data = json.loads(c)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def response_action(raw_text: str | None) -> tuple[str | None, str | None]:
    """``(target, text)`` of the action in a decide call's logged response, each None when absent or not a string,
    both None when the response does not parse as a JSON object with an ``action`` object."""
    data = _json_object((raw_text or "").strip())
    action = data.get("action") if data is not None else None
    if not isinstance(action, dict):
        return None, None
    target, text = action.get("target"), action.get("text")
    return (target if isinstance(target, str) else None), (text if isinstance(text, str) else None)


def _hop_hist(value: Any) -> dict[int, int] | None:
    if not isinstance(value, dict):
        return None
    out: dict[int, int] = {}
    for k, v in value.items():
        try:
            depth, n = int(k), int(v)
        except (TypeError, ValueError):
            continue
        out[depth] = out.get(depth, 0) + n
    return out


def _talk_by_tick(db: sqlite3.Connection, ticks: tuple[int, int] | None) -> dict[tuple[str, int], list[str]]:
    """``(speaker, tick) -> [text, ...]`` over the run's ``talk`` events (in ``ticks``, inclusive), in event order."""
    sql, params = "SELECT tick, agent_id, payload FROM events WHERE kind='talk'", ()
    if ticks is not None:
        sql, params = sql + " AND tick BETWEEN ? AND ?", ticks
    out: dict[tuple[str, int], list[str]] = {}
    for r in db.execute(sql + " ORDER BY seq", params):
        try:
            p = json.loads(r["payload"] or "{}")
        except ValueError:
            continue
        if not isinstance(p, dict) or not isinstance(p.get("text"), str):
            continue
        speaker = str(p.get("speaker_id") or r["agent_id"] or "")
        out.setdefault((speaker, int(r["tick"])), []).append(p["text"])
    return out


def _run_node_ids(db: sqlite3.Connection) -> frozenset[str]:
    if not _has_tables(db, "resource_nodes"):
        return frozenset()
    return frozenset(str(r["node_id"]) for r in db.execute("SELECT node_id FROM resource_nodes"))


def _read(run_dir: str | Path, tier: str | None, day: int | None) -> tuple[list[CallRetrieval], frozenset[str]]:
    extra, params = _call_filter(run_dir, tier, day)
    with connect(run_dir) as db:
        rows = db.execute("SELECT call_id, agent_id, tick, thought, raw_text, extras FROM llm_calls "
                          "WHERE purpose='decide'" + extra + " ORDER BY tick, call_id", params).fetchall()
        ticks = (int(params[-2]), int(params[-1])) if day is not None else None
        talk = _talk_by_tick(db, ticks)
        node_ids = _run_node_ids(db)
    out: list[CallRetrieval] = []
    for r in rows:
        try:
            extras = json.loads(r["extras"] or "{}")
        except ValueError:
            extras = {}
        titles = extras.get("retrieved_titles") if isinstance(extras, dict) else None
        if not isinstance(titles, list):
            continue
        age = extras.get("retrieved_mean_age")
        target, text = response_action(r["raw_text"])
        aid, tick = str(r["agent_id"]), int(r["tick"])
        out.append(CallRetrieval(
            call_id=str(r["call_id"]), agent_id=aid, tick=tick, titles=tuple(str(t) for t in titles),
            hop_hist=_hop_hist(extras.get("retrieved_hop_hist")),
            mean_age=float(age) if isinstance(age, int | float) and not isinstance(age, bool) else None,
            thought=str(r["thought"] or ""), action_target=target, action_text=text,
            talk=tuple(talk.get((aid, tick), ())),
        ))
    return out, node_ids


def decide_retrievals(run_dir: str | Path, tier: str | None = None, day: int | None = None) -> list[CallRetrieval]:
    """Every decide call (of ``tier``, on ``day``) whose extras log its retrieval, ordered by tick and call id."""
    return _read(run_dir, tier, day)[0]


# --- the four retrieval figures -------------------------------------------------------------------------------
def _empty_rate(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    calls = decide_retrievals(run_dir, arg, day)
    return sum(1 for c in calls if not c.titles) / len(calls) if calls else None


def _mean_age(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    weighted = [(c.mean_age, len(c.titles)) for c in decide_retrievals(run_dir, arg, day)
                if c.titles and c.mean_age is not None]
    n = sum(k for _, k in weighted)
    return sum(age * k for age, k in weighted) / n if n else None


def _depth_share(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    total = deep = 0
    for c in decide_retrievals(run_dir, arg, day):
        if c.hop_hist is None:
            continue
        total += sum(c.hop_hist.values())
        deep += sum(n for depth, n in c.hop_hist.items() if depth >= 1)
    return deep / total if total else None


def _cited_rate(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    calls, node_ids = _read(run_dir, arg, day)
    shown = [c for c in calls if c.titles]
    return sum(1 for c in shown if cited(c, node_ids)) / len(shown) if shown else None


@register("empty_retrieval_rate", daily=_empty_rate)
def empty_retrieval_rate(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Decide calls (of tier ``arg``) that retrieved no note, over decide calls that logged their retrieval."""
    return _empty_rate(run_dir, arg, None)


@register("mean_retrieved_age_ticks", daily=_mean_age)
def mean_retrieved_age_ticks(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean age in ticks of the retrieved notes, pooled over notes; None when the extras key is missing."""
    return _mean_age(run_dir, arg, None)


@register("retrieved_depth_share", daily=_depth_share)
def retrieved_depth_share(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Share of the retrieved notes reached at graph depth 1 or more, pooled over notes."""
    return _depth_share(run_dir, arg, None)


@register("retrieved_cited_rate", daily=_cited_rate)
def retrieved_cited_rate(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Decide calls that cite a retrieved note, over decide calls that retrieved one."""
    return _cited_rate(run_dir, arg, None)


# --- memory-command rates -------------------------------------------------------------------------------------
def _command_rate(run_dir: str | Path, purpose: str, *, failed: bool, arg: str | None, day: int | None) -> float | None:
    """Share of the ``memory_commands`` rows of ``purpose`` (on ``day``) that succeeded, or with ``failed`` that
    failed; None without such rows, without the table, or with an argument."""
    if arg is not None:
        return None
    extra, params = _call_filter(run_dir, None, day)
    with connect(run_dir) as db:
        if not _has_tables(db, "memory_commands"):
            return None
        row = db.execute("SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN ok THEN 1 ELSE 0 END), 0) AS k "
                         "FROM memory_commands WHERE purpose=?" + extra, (purpose, *params)).fetchone()
    n, k = int(row["n"]), int(row["k"])
    if not n:
        return None
    return (n - k) / n if failed else k / n


def _maintenance_valid(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    return _command_rate(run_dir, "maintain", failed=False, arg=arg, day=day)


def _tick_refusal(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    return _command_rate(run_dir, "tick", failed=True, arg=arg, day=day)


@register("maintenance_valid_command_rate", daily=_maintenance_valid)
def maintenance_valid_command_rate(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Maintenance commands that succeeded, over maintenance commands (G1's valid-command rate)."""
    return _maintenance_valid(run_dir, arg, None)


@register("tick_command_refusal_rate", daily=_tick_refusal)
def tick_command_refusal_rate(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Per-tick memory commands (``remember``, ``revise_self``) the vault limits refused, over per-tick commands."""
    return _tick_refusal(run_dir, arg, None)
