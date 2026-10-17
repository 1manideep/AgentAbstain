"""Press outcomes for ``exp_press``: reach and fidelity of chronicle items against gossip (DESIGN §17, "2x2 chronicle
x gossip; reach and fidelity of chronicle items vs gossip origins"; PLAN §4.7, "does information spread differently
through press than through word of mouth").

The loop gives a note written within a tick of reading the chronicle the channel ``chronicle`` and the synthetic
origin ``chronicle:day_<n>`` (``n``: the day whose paper the agent read), but no item id. The chronicle stores each
line of day ``n``'s paper as an item ``d<n>-<k>`` in ``chronicle_items`` (§11.2). :func:`chronicle_matches` joins the
two offline, without writing to the run: every live chronicle-channel note is matched against the items of its origin
day by cosine on :mod:`void.memory.embed` embeddings built with the run's embedder config
(``HashingEmbedder(memory.embedding_dim)`` from the resolved config). The note side is the embedding the memory store
indexed for it (title plus body without its ``## Related`` section), the item side embeds the item text. The best
item is the one with the highest similarity (the earliest item of the day on a tie); similarities are clamped to
[0, 1] like the ``similarity`` of a ``gossip_transfer`` event, so the two fidelities share a scale.

Registered outcomes (resolved by :func:`void.research.analysis.end_of_run_outcome`; an argument they do not take, or
one that does not parse, gives None):

* ``chronicle_reach[.<channel>]``: living agents holding at least one live chronicle-derived note, over living agents.
  A note is chronicle-derived when its channel is ``chronicle`` or its origin is ``chronicle:day_<n>`` (a gossip or
  inherited copy of a chronicle note). ``.<channel>`` counts only derived notes that arrived through that channel:
  ``chronicle_reach.chronicle`` is the direct readers, ``chronicle_reach.gossip`` those who heard the paper by word
  of mouth.
* ``chronicle_fidelity``: mean best similarity over the live chronicle-channel notes that matched an item.
* ``gossip_fidelity[.<hop>]``: mean ``similarity`` of the ``gossip_transfer`` events (copy against origin);
  ``.<hop>`` restricts to transfers of that hop.
* ``gossip_reach[.<hop>]``: living agents holding at least one live gossip-channel note (of that hop), over living
  agents.

Reaches are None without living agents and 0.0 when nobody holds such a note; fidelities are None without a matched
note or a transfer. Daily versions (``compare``'s curves): a reach on day ``d`` is the state at the last tick of day
``d`` (agents born by then and not yet dead; notes that had arrived by then). The index keeps no deletion tick, so a
note its living owner deleted counts as gone on every day, and a dead agent's notes count until its death. A fidelity
on day ``d`` is over that day's arrivals: chronicle-channel notes created on day ``d``, transfers made on day ``d``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from void.memory.embed import HashingEmbedder, cosine, unpack
from void.memory.notes import CHANNELS
from void.research.analysis import _alive_ids, _mean, connect, run_config
from void.research.outcomes import register

__all__ = [
    "CHRONICLE_CHANNEL",
    "CHRONICLE_ORIGIN_PREFIX",
    "GOSSIP_CHANNEL",
    "ChronicleMatch",
    "best_item",
    "chronicle_fidelity",
    "chronicle_matches",
    "chronicle_reach",
    "gossip_fidelity",
    "gossip_reach",
    "origin_day",
]

CHRONICLE_CHANNEL = "chronicle"
GOSSIP_CHANNEL = "gossip"
CHRONICLE_ORIGIN_PREFIX = "chronicle:"
_ORIGIN_RE = re.compile(r"^chronicle:day_(\d+)$")
_ITEM_NUM_RE = re.compile(r"-(\d+)$")


@dataclass(frozen=True)
class ChronicleMatch:
    """One live chronicle-channel note and the item of its origin day it is closest to (None: the origin does not
    name a day, or that day has no items)."""

    note_id: str
    agent_id: str
    created_tick: int
    day: int | None            # the origin day: whose paper the note's author read
    item_id: str | None        # the best item of that day, ``d<day>-<n>``
    similarity: float | None   # its clamped cosine to the note, in [0, 1]


def origin_day(origin_note_id: str | None) -> int | None:
    """The day ``n`` of a ``chronicle:day_<n>`` origin; None for any other origin."""
    m = _ORIGIN_RE.match(str(origin_note_id or ""))
    return int(m.group(1)) if m else None


def _item_order(item_id: str) -> tuple[int, str]:
    m = _ITEM_NUM_RE.search(item_id)
    return (int(m.group(1)) if m else 0, item_id)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def best_item(vec: Sequence[float], items: Sequence[tuple[str, Sequence[float]]]) -> tuple[str, float] | None:
    """``(item_id, similarity)`` of the item in ``items`` (in day order) with the highest clamped cosine to ``vec``;
    the earliest one on a tie, None for no items."""
    best: tuple[str, float] | None = None
    for item_id, item_vec in items:
        sim = _clamp(cosine(list(vec), list(item_vec)))
        if best is None or sim > best[1]:
            best = (item_id, sim)
    return best


def _day_ticks(run_dir: str | Path, day: int) -> tuple[int, int]:
    """First and last tick of ``day`` (ticks ``(day - 1) * tpd + 1`` .. ``day * tpd``)."""
    tpd = run_config(run_dir).run.ticks_per_day
    return (int(day) - 1) * tpd + 1, int(day) * tpd


def chronicle_matches(run_dir: str | Path, *, day: int | None = None) -> list[ChronicleMatch]:
    """Every live chronicle-channel note (``day``: only those created on that day), ordered by note id, with the best
    item of its origin day and the similarity (see the module docstring)."""
    cfg = run_config(run_dir)
    embedder = HashingEmbedder(cfg.memory.embedding_dim)
    sql = ("SELECT note_id, agent_id, created_tick, origin_note_id, embedding FROM notes "
           "WHERE archived=0 AND channel=?")
    params: tuple[object, ...] = (CHRONICLE_CHANNEL,)
    if day is not None:
        sql += " AND created_tick BETWEEN ? AND ?"
        params += _day_ticks(run_dir, day)
    with connect(run_dir) as db:
        notes = db.execute(sql + " ORDER BY note_id", params).fetchall()
        items: dict[int, list[tuple[str, list[float]]]] = {}
        for d in sorted({d for d in (origin_day(r["origin_note_id"]) for r in notes) if d is not None}):
            rows = db.execute("SELECT item_id, text FROM chronicle_items WHERE day=?", (d,)).fetchall()
            ordered = sorted(rows, key=lambda r: _item_order(str(r["item_id"])))
            items[d] = [(str(r["item_id"]), embedder.embed(str(r["text"]))) for r in ordered]
    out: list[ChronicleMatch] = []
    for r in notes:
        vec = unpack(r["embedding"])
        if len(vec) != embedder.dim:
            raise ValueError(f"note {r['note_id']} has a {len(vec)}-dim embedding but the run's "
                             f"memory.embedding_dim is {embedder.dim}")
        d = origin_day(r["origin_note_id"])
        best = best_item(vec, items.get(d, [])) if d is not None else None
        out.append(ChronicleMatch(note_id=str(r["note_id"]), agent_id=str(r["agent_id"]), created_tick=int(r["created_tick"]),
                                  day=d, item_id=best[0] if best else None, similarity=best[1] if best else None))
    return out


# --- reach ---------------------------------------------------------------------------------------------------
NoteFilter = Callable[[sqlite3.Row], bool]


def _reach(run_dir: str | Path, keep: NoteFilter, day: int | None) -> float | None:
    """Living agents holding a note ``keep`` accepts, over living agents: at the end of the run, or at the last tick
    of ``day`` (see the module docstring)."""
    cols = "n.agent_id AS agent_id, n.channel AS channel, n.hop AS hop, n.origin_note_id AS origin_note_id"
    with connect(run_dir) as db:
        if day is None:
            alive = set(_alive_ids(db))
            rows = db.execute(f"SELECT {cols} FROM notes n WHERE n.archived=0").fetchall()
        else:
            end = _day_ticks(run_dir, day)[1]
            alive = {str(r["agent_id"]) for r in db.execute(
                "SELECT agent_id FROM agents WHERE born_tick <= ? AND (died_tick IS NULL OR died_tick > ?)", (end, end))}
            rows = db.execute(
                f"SELECT {cols} FROM notes n JOIN agents a ON a.agent_id = n.agent_id WHERE n.created_tick <= ? "
                "AND (n.archived=0 OR (a.died_tick IS NOT NULL AND a.died_tick > ?))", (end, end)).fetchall()
    if not alive:
        return None
    holders = {str(r["agent_id"]) for r in rows if keep(r)} & alive
    return len(holders) / len(alive)


def _chronicle_filter(channel: str | None) -> NoteFilter | None:
    if channel is not None and channel not in CHANNELS:
        return None

    def keep(r: sqlite3.Row) -> bool:
        derived = r["channel"] == CHRONICLE_CHANNEL or str(r["origin_note_id"] or "").startswith(CHRONICLE_ORIGIN_PREFIX)
        return derived and (channel is None or r["channel"] == channel)

    return keep


def _hop(arg: str | None) -> tuple[bool, int | None]:
    """``(ok, hop)``: the hop an argument names (None without an argument); not ok unless it is a non-negative
    integer."""
    if arg is None:
        return True, None
    return (True, int(arg)) if arg.isascii() and arg.isdigit() else (False, None)


def _gossip_filter(arg: str | None) -> NoteFilter | None:
    ok, hop = _hop(arg)
    if not ok:
        return None
    return lambda r: r["channel"] == GOSSIP_CHANNEL and (hop is None or int(r["hop"]) == hop)


def _chronicle_reach_on_day(run_dir: str | Path, arg: str | None, day: int) -> float | None:
    keep = _chronicle_filter(arg)
    return None if keep is None else _reach(run_dir, keep, day)


@register("chronicle_reach", daily=_chronicle_reach_on_day)
def chronicle_reach(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Living agents holding a live chronicle-derived note (arrived through channel ``arg``), over living agents."""
    keep = _chronicle_filter(arg)
    return None if keep is None else _reach(run_dir, keep, None)


def _gossip_reach_on_day(run_dir: str | Path, arg: str | None, day: int) -> float | None:
    keep = _gossip_filter(arg)
    return None if keep is None else _reach(run_dir, keep, day)


@register("gossip_reach", daily=_gossip_reach_on_day)
def gossip_reach(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Living agents holding a live gossip-channel note (of hop ``arg``), over living agents."""
    keep = _gossip_filter(arg)
    return None if keep is None else _reach(run_dir, keep, None)


# --- fidelity ------------------------------------------------------------------------------------------------
def _chronicle_fidelity(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    if arg is not None:
        return None
    return _mean(m.similarity for m in chronicle_matches(run_dir, day=day) if m.similarity is not None)


@register("chronicle_fidelity", daily=_chronicle_fidelity)
def chronicle_fidelity(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean best similarity of the live chronicle-channel notes to the items of their origin day."""
    return _chronicle_fidelity(run_dir, arg, None)


def _gossip_fidelity(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    ok, hop = _hop(arg)
    if not ok:
        return None
    sql, params = "SELECT payload FROM events WHERE kind='gossip_transfer'", ()
    if day is not None:
        sql, params = sql + " AND day=?", (int(day),)
    sims: list[float] = []
    with connect(run_dir) as db:
        for r in db.execute(sql + " ORDER BY seq", params):
            p = json.loads(r["payload"] or "{}")
            if p.get("similarity") is None or (hop is not None and int(p.get("hop", 0)) != hop):
                continue
            sims.append(float(p["similarity"]))
    return _mean(sims)


@register("gossip_fidelity", daily=_gossip_fidelity)
def gossip_fidelity(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean ``similarity`` of the run's ``gossip_transfer`` events (of hop ``arg``)."""
    return _gossip_fidelity(run_dir, arg, None)
