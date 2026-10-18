"""Vault-structure and policy outcomes (MEMORY_EVOLUTION §7.2) and the monoculture index ``M`` (§7.1, H8).

Every outcome here is an end-of-run figure over the agents alive when the run ended (``agents.status =
'alive'``), except :func:`policy_edit_distance`, which covers every agent that ever had a policy. Each takes
``(run_dir, arg)`` and ignores ``arg``; each returns None when there is nothing to measure (no living agent
with notes, no policy table in a run made before the memory layer, ...). Every figure iterates in sorted
order, so it is a pure function of the run.

Vault (a note is a live note, ``archived = 0``, that is not an entity stub, exactly as in
:mod:`void.memory.vault_stats`; stubs are kernel link anchors, not content):

* ``vault_notes``, ``vault_bytes``, ``mean_note_chars``, ``link_density``, ``orphan_rate``: the population
  figures of :func:`void.memory.vault_stats.vault_stats` over the living agents, with ``run_dir`` given so a moved
  run still reads its files. ``vault_notes`` and ``vault_bytes`` are totals, as on the metrics day rows. A bare
  ``link_density`` now resolves here; the day rows' DESIGN §10 figure (the mean over living agents of link rows
  per live note, stubs and unresolved links included) is still reachable as ``day.link_density``.
* ``index_coverage``: the same pooled share of non-index notes within two link hops of an index note of their own
  vault. Where ``vault_stats`` says None because no living agent has an index note, this outcome says 0.0 when
  there are notes (none of them is covered), so a paired comparison against an arm that never builds an index
  still has pairs; it stays None without notes, or when every note is an index note.
* ``near_duplicate_rate``: the share of notes that have another note in the same vault at cosine >= 0.9
  (:data:`NEAR_DUPLICATE_COSINE`) on the embeddings stored in ``notes.embedding`` (unpacked with
  :func:`void.memory.embed.unpack`), pooled over the living agents' notes. Both notes of a pair must be notes in
  the sense above: a note that only resembles a stub is not a duplicate. Notes in different vaults never pair.
* ``tag_entropy``: for each living agent with at least one tag on its notes, the Shannon entropy in bits of its
  tag counts (a note with two tags counts once under each); the mean over those agents.
* ``folder_depth``: the number of directories between an agent's vault root and a note file (a note in
  ``<vault>/<agent>/notes/`` has depth 1), averaged over the agent's notes and then over the living agents with
  notes. The vault root is ``agents.memory_path``, or failing that the last path part equal to the agent id. In
  today's layout every note sits in ``notes/`` (the index area is the ``index`` tag, not a folder), so this is
  1.0 wherever there are notes; it moves only under a layout with subfolders.

Policy (``policy_versions``; the current policy is an agent's highest version):

* ``policy_chars``: mean length in characters of the living agents' current policies. A naive text metric: the
  E0 decoy (§6.2) is built to rank above the no-op on it without any gain in fitness.
* ``policy_edit_distance``: for each agent with at least two versions (alive or dead), the mean normalized
  Levenshtein distance (:func:`normalized_levenshtein`, character level) between consecutive versions; the mean
  over those agents.
* ``policy_drift_from_seed``: mean over the living agents of ``1 - similarity(current, version 1)``. Version 1
  is a founder's seed or a child's inherited copy of its parent's text; an agent that never edited drifts 0.
* ``policy_versions_per_agent``: mean number of versions per living agent that has a policy.
* ``monoculture_index``: mean pairwise ``similarity`` of the living agents' current policies, over every
  unordered pair; None with fewer than two living agents that have a policy.

``similarity`` is :func:`void.memory.policy.similarity`, token-set Jaccard, and the note embeddings are feature
hashing (:class:`void.memory.embed.HashingEmbedder`): both are placeholders until a real embedder exists, so the
"semantic" drift, the monoculture index and the near-duplicate rate measure shared words, not shared meaning.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from collections.abc import Iterable
from itertools import combinations
from operator import mul
from pathlib import Path, PurePath
from typing import Any

from void.memory.embed import unpack
from void.memory.policy import similarity
from void.memory.store import ENTITY_TAG
from void.memory.vault_stats import INDEX_TAG, vault_stats
from void.research.analysis import _alive_ids, _has_tables, _mean, connect
from void.research.outcomes import register

__all__ = [
    "NEAR_DUPLICATE_COSINE",
    "VAULT_OUTCOMES",
    "folder_depth",
    "index_coverage",
    "levenshtein",
    "link_density",
    "mean_note_chars",
    "monoculture_index",
    "near_duplicate_rate",
    "normalized_levenshtein",
    "orphan_rate",
    "policy_chars",
    "policy_drift_from_seed",
    "policy_edit_distance",
    "policy_versions_per_agent",
    "tag_entropy",
    "vault_bytes",
    "vault_notes",
]

NEAR_DUPLICATE_COSINE = 0.9
# outcome name -> key of void.memory.vault_stats.vault_stats
VAULT_OUTCOMES: dict[str, str] = {
    "vault_notes": "notes",
    "vault_bytes": "bytes",
    "mean_note_chars": "mean_note_chars",
    "link_density": "link_density",
    "orphan_rate": "orphan_rate",
    "index_coverage": "index_coverage",
}


# --- string distance -------------------------------------------------------------------------------------------
def levenshtein(a: str, b: str) -> int:
    """Character-level edit distance (insertions, deletions and substitutions all cost 1).

    Bit-parallel (Myers 1999, in Hyyro's formulation for the global distance): one pass over the longer string
    with the shorter one held as bit vectors, so two 2000-character policies cost 2000 big-integer steps rather
    than four million cell updates.
    """
    if len(a) < len(b):
        a, b = b, a
    m = len(b)
    if m == 0:
        return len(a)
    peq: dict[str, int] = {}
    for i, ch in enumerate(b):
        peq[ch] = peq.get(ch, 0) | (1 << i)
    mask = (1 << m) - 1
    top = 1 << (m - 1)
    pv, mv, score = mask, 0, m
    for ch in a:
        eq = peq.get(ch, 0)
        xv = eq | mv
        xh = ((((eq & pv) + pv) & mask) ^ pv) | eq
        ph = mv | (~(xh | pv) & mask)
        mh = pv & xh
        if ph & top:
            score += 1
        elif mh & top:
            score -= 1
        ph = ((ph << 1) | 1) & mask
        mh = (mh << 1) & mask
        pv = mh | (~(xv | ph) & mask)
        mv = ph & xv
    return score


def normalized_levenshtein(a: str, b: str) -> float:
    """:func:`levenshtein` divided by the longer length: 0.0 for equal texts, 1.0 for nothing in common."""
    longest = max(len(a), len(b))
    return levenshtein(a, b) / longest if longest else 0.0


# --- shared readers --------------------------------------------------------------------------------------------
def _tags(raw: object) -> list[str]:
    try:
        items = json.loads(str(raw) if raw is not None else "[]")
    except (TypeError, ValueError):
        return []
    return [str(t) for t in items] if isinstance(items, list) else []


def _notes(db: sqlite3.Connection, agent_id: str) -> list[sqlite3.Row]:
    """The agent's live notes other than entity stubs, oldest first (the population vault_stats counts)."""
    rows = db.execute(
        "SELECT note_id, path, tags, embedding FROM notes WHERE agent_id=? AND archived=0 ORDER BY created_tick, note_id",
        (agent_id,),
    ).fetchall()
    return [r for r in rows if ENTITY_TAG not in _tags(r["tags"])]


def _vault_figure(run_dir: str | Path, key: str) -> float | None:
    """One figure of ``vault_stats`` over the living agents (``index_coverage`` 0.0 with notes but no index)."""
    with connect(run_dir) as db:
        alive = _alive_ids(db)
        stats: dict[str, Any] = vault_stats(db, alive, run_dir)
        value = stats[key]
        if key == "index_coverage" and value is None and stats["notes"]:
            has_index = any(INDEX_TAG in _tags(r["tags"]) for aid in alive for r in _notes(db, aid))
            if not has_index:
                return 0.0  # notes but no index note: none of them is covered (see the module docstring)
    return None if value is None else float(value)


def _unit(vec: list[float]) -> list[float] | None:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm > 0.0 else None


def _entropy_bits(counts: Iterable[int]) -> float:
    vals = [c for c in counts if c > 0]
    total = sum(vals)
    return -sum((c / total) * math.log2(c / total) for c in vals) if total else 0.0


def _depth(path: str, agent_id: str, memory_path: str | None) -> int | None:
    """Directories between the agent's vault root and the file ``path``; None when the root cannot be found."""
    p = PurePath(path)
    if memory_path:
        try:
            rel = p.relative_to(PurePath(memory_path))
        except ValueError:
            rel = None
        if rel is not None and rel.parts:
            return len(rel.parts) - 1
    parts = p.parts
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == agent_id:
            return len(parts) - i - 2
    return None


def _policies(db: sqlite3.Connection) -> dict[str, list[str]]:
    """``{agent_id: [text of version 1, version 2, ...]}`` for every agent with a policy, sorted."""
    out: dict[str, list[str]] = {}
    for r in db.execute("SELECT agent_id, text FROM policy_versions ORDER BY agent_id, version"):
        out.setdefault(str(r["agent_id"]), []).append(str(r["text"]))
    return out


def _living_policies(run_dir: str | Path) -> dict[str, list[str]] | None:
    """The version histories of the living agents that have a policy; None without a policy table."""
    with connect(run_dir) as db:
        if not _has_tables(db, "policy_versions"):
            return None
        alive = set(_alive_ids(db))
        return {aid: texts for aid, texts in _policies(db).items() if aid in alive}


# --- vault outcomes --------------------------------------------------------------------------------------------
@register("vault_notes")
def vault_notes(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Total notes of the living agents (``vault_stats`` ``notes``)."""
    return _vault_figure(run_dir, "notes")


@register("vault_bytes")
def vault_bytes(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Total bytes of the living agents' note files (``vault_stats`` ``bytes``)."""
    return _vault_figure(run_dir, "bytes")


@register("mean_note_chars")
def mean_note_chars(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean body characters per note, pooled over the living agents."""
    return _vault_figure(run_dir, "mean_note_chars")


@register("link_density")
def link_density(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Resolved links per note, pooled over the living agents."""
    return _vault_figure(run_dir, "link_density")


@register("orphan_rate")
def orphan_rate(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Share of the living agents' notes with no resolved link in or out."""
    return _vault_figure(run_dir, "orphan_rate")


@register("index_coverage")
def index_coverage(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Share of non-index notes within two hops of an index note of their vault; 0.0 when no vault has an index."""
    return _vault_figure(run_dir, "index_coverage")


@register("near_duplicate_rate")
def near_duplicate_rate(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Share of the living agents' notes with another note of the same vault at cosine >= 0.9 (stored embeddings).

    The embeddings are feature hashing until a real embedder exists: a near-duplicate shares words, not meaning.
    """
    total = dup = 0
    with connect(run_dir) as db:
        for aid in _alive_ids(db):
            vecs = [_unit(unpack(bytes(r["embedding"] or b""))) for r in _notes(db, aid)]
            total += len(vecs)
            hit = [False] * len(vecs)
            for i, j in combinations(range(len(vecs)), 2):
                a, b = vecs[i], vecs[j]
                if a is None or b is None or len(a) != len(b) or (hit[i] and hit[j]):
                    continue
                if sum(map(mul, a, b)) >= NEAR_DUPLICATE_COSINE:  # unit vectors: the dot product is the cosine
                    hit[i] = hit[j] = True
            dup += sum(hit)
    return dup / total if total else None


@register("tag_entropy")
def tag_entropy(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean over living agents with tagged notes of the Shannon entropy (bits) of the agent's tag counts."""
    per_agent: list[float] = []
    with connect(run_dir) as db:
        for aid in _alive_ids(db):
            counts = Counter(t for r in _notes(db, aid) for t in dict.fromkeys(_tags(r["tags"])))
            if counts:
                per_agent.append(_entropy_bits(counts[t] for t in sorted(counts)))
    return _mean(per_agent)


@register("folder_depth")
def folder_depth(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean over living agents with notes of the mean folder depth of their note files under the vault root."""
    per_agent: list[float] = []
    with connect(run_dir) as db:
        roots = {str(r["agent_id"]): r["memory_path"] for r in db.execute("SELECT agent_id, memory_path FROM agents")}
        for aid in _alive_ids(db):
            depths = [_depth(str(r["path"]), aid, roots.get(aid)) for r in _notes(db, aid)]
            mean = _mean(d for d in depths if d is not None)
            if mean is not None:
                per_agent.append(mean)
    return _mean(per_agent)


# --- policy outcomes -------------------------------------------------------------------------------------------
@register("policy_chars")
def policy_chars(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean characters of the living agents' current policies (a naive text metric; see the module docstring)."""
    pols = _living_policies(run_dir)
    return None if pols is None else _mean(len(texts[-1]) for texts in pols.values())


@register("policy_edit_distance")
def policy_edit_distance(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean over every agent with two or more versions of its mean normalized Levenshtein distance between
    consecutive versions (dead agents included)."""
    with connect(run_dir) as db:
        if not _has_tables(db, "policy_versions"):
            return None
        pols = _policies(db)
    per_agent = [
        _mean(normalized_levenshtein(prev, cur) for prev, cur in zip(texts, texts[1:], strict=False))
        for texts in pols.values()
        if len(texts) >= 2
    ]
    return _mean(v for v in per_agent if v is not None)


@register("policy_drift_from_seed")
def policy_drift_from_seed(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean over living agents of ``1 - similarity(current policy, version 1)`` (token Jaccard until a real embedder)."""
    pols = _living_policies(run_dir)
    return None if pols is None else _mean(1.0 - similarity(texts[-1], texts[0]) for texts in pols.values())


@register("policy_versions_per_agent")
def policy_versions_per_agent(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean number of policy versions per living agent that has a policy."""
    pols = _living_policies(run_dir)
    return None if pols is None else _mean(len(texts) for texts in pols.values())


@register("monoculture_index")
def monoculture_index(run_dir: str | Path, arg: str | None = None) -> float | None:
    """``M``: mean pairwise similarity of the living agents' current policies (token Jaccard until a real
    embedder exists); None with fewer than two living agents that have a policy."""
    pols = _living_policies(run_dir)
    if pols is None or len(pols) < 2:
        return None
    current = [pols[aid][-1] for aid in sorted(pols)]
    return _mean(similarity(a, b) for a, b in combinations(current, 2))
