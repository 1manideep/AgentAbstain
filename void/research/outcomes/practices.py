"""Transmission outcomes (MEMORY_EVOLUTION §7.1, H3, H7, H8) and the practice-coding tooling of §7.3 (WP8): a written
codebook (``docs/research/codebook.md``), a blind sampler, a deterministic keyword coder and Cohen's kappa. The LLM
coder of §7.3 step (2) is paid and is not built here.

The data are two tables of the evolving-memory layer:

* ``practice_events`` (``kind`` ``suggested``, ``adopted``, ``persisted`` or ``dropped``; ``source_agent_id``,
  ``target_agent_id``, ``excerpt``, ``similarity``, ``tick``): a ``suggested`` row is a ``share_practice`` the
  kernel delivered; the other kinds are the kernel's measurement of what the target did with it (never self-report).
* ``policy_versions`` (``agent_id``, ``version``, ``tick``, ``text``, ``parent_version``, ``source``,
  ``source_agent_id``): every version of every agent's practices file; an agent's current policy is its highest
  version.

Practice-event outcomes (each also has a daily version for ``compare``'s curves: the same figure over the rows written
by the end of day ``d``, tick ``d * ticks_per_day``):

``adoption_rate``
    ``adopted`` rows over ``suggested`` rows (raw counts). None without a suggestion (or without the table).
``adoption_latency_ticks``
    Mean ticks from a suggestion to its adoption, over the adoptions that pair with a suggestion. An ``adopted`` row
    pairs with the latest ``suggested`` row from the same source to the same target at or before its tick whose
    excerpt is the same up to :func:`normalize_line`; failing that, with the latest suggestion from that source to
    that target at or before its tick (the kernel's row names the source, so that suggestion is the one taken up);
    failing that it is unpaired and left out. None without a paired adoption.
``persistence_rate[.resolved]``
    ``persisted`` rows over ``adopted`` rows; None without an adoption. An adoption made within
    ``memory.practices.persist_days`` of the end of the run has had no time to persist, so the bare rate is biased
    low in short runs; ``persistence_rate.resolved`` is ``persisted / (persisted + dropped)``, over the adoptions
    whose fate the kernel has recorded (None when it has recorded none).
``first_adoption_source_richer_share``
    For every agent that adopted at least once, its *first* adoption (its earliest ``adopted`` row as target, the
    first written on a tied tick): whether the source was richer than the adopter at that tick. A balance at tick
    ``t`` is the agent's balance after every ledger entry of ticks ``<= t``: its end balance minus the wallet-ledger
    deltas booked after ``t`` (every balance change goes through the wallet ledger; the reconstruction of
    :mod:`void.research.outcomes.lineage`). Richer is strictly greater (a tie is not richer). The outcome is the share
    of those agents whose first adoption came from a richer source: near 1 means agents first copy the successful
    (payoff-biased transmission, after SwarmWorld), near the share expected by chance means wealth does not steer who
    is copied. An agent whose source the ``agents`` table does not hold is left out. None without a first adoption.
    MEMORY_EVOLUTION §7.1 also asks by which channel an agent first took a practice (observing, suggestion,
    inheritance); this world has no observation channel for practices, so the outcome measures the richer-source
    share of the suggestion channel instead.

Policy outcomes, over the agents alive at the end of the run (``agents.status = 'alive'``, the population of
``monoculture_index``) that have a policy; end of run only. They read the *policy graph*: one node per policy
version, with an edge from each version to what it was made from:

* to the agent's previous version (weight 0), for every version but an ``inherited`` one (an inherited text replaces
  the previous one wholesale: a child's first version, or a transplant);
* to the source agent's version current at the row's tick (weight 1: a *hop*), for an ``inherited`` or ``adopted``
  row with a ``source_agent_id``;
* for every ``adopted`` practice event, from the target's version current at the event's tick to the source's
  version current at the tick of the paired suggestion (the adoption tick when unpaired), weight 1. A maintenance
  edit the kernel measures as an adoption is a ``self_edit`` row, so the hop is recorded by the event.

A hop whose source has no version at that tick (a transplant donor from another run, a suggestion from an agent
without a policy) still counts as a hop, to a text the run does not hold. Edges point back in time (to versions
current at or before the row's tick); two agents adopting from each other on the same tick could close a cycle, and a
walk ignores an edge back into a version it is still expanding.

``lineage_depth``
    Mean over the policies of the number of hops on the longest path of the graph behind the current policy: a seed
    that was only self-edited is 0, a child's inherited copy of it 1, a grandchild's 2; an adoption from a depth-``k``
    policy makes the adopter's depth at least ``k + 1``. With inheritance only it is the generation of the agent (when
    every ancestor had a policy): "generations a practice has survived" (§7.1). None without a policy.
``novelty_share``
    Share of the *bullets* of the current policies that appear in no seed of the run and in no inherited or adopted
    ancestor text. A bullet is a practice line (:func:`void.culture.practices.policy_lines`: a non-empty line that is
    not a markdown heading) under :func:`normalize_line` (lower case, words only, so list markers and punctuation do
    not matter), counted once per policy. The texts a bullet is checked against are: the text of every ``seed`` row of
    the run; the text of every version reached through at least one hop (the source's whole policy at the time, and
    everything behind it); the text of every ``inherited`` version on the walk (a verbatim copy of its source, also
    when the source is outside the run); and every adopted excerpt on the walk. The agent's own earlier versions are
    not checked: a bullet the agent wrote itself and kept is novel. Pooled over the policies (a share of bullets, not
    a mean of per-agent shares). None without a bullet.
``cluster_entropy[.dominant][.keyword]``
    H8's cluster entropy ``H``, in bits. Each bullet of each current policy gets a practice category (T1 to T9 of
    the codebook): from ``practice_codes.csv`` in the run directory when there is one (columns ``text`` and ``code``;
    a bullet is looked up by its normalized text, the majority code wins when one text was coded twice, and a bullet
    the file does not cover falls back to the keyword coder), else from :func:`keyword_code`. Bare: each agent's
    category shares (its bullets per category over its bullets) averaged over the agents with a bullet, so every
    agent weighs the same however long its policy, and ``H = -sum p_c log2 p_c`` over that population mix. ``.dominant``:
    each agent is put in the cluster of its most frequent category (a tie goes to the lower code number) and ``H`` is
    the entropy of the cluster sizes; it is 0 when every agent leans the same way however varied each policy is, the
    monoculture H8 asks about. ``.keyword`` ignores ``practice_codes.csv``. T9 (other) counts as a category. None
    without a coded bullet.
``practice_share.<T>[.dominant][.keyword]``
    The share of category ``<T>`` (``T1`` to ``T9``) in the distribution whose entropy ``cluster_entropy`` is, with
    the same coder, the same population (living agents whose current policy has a bullet) and the same optional
    arguments after the category (``dominant`` and ``keyword``, in any order, each as ``cluster_entropy`` reads
    them). Bare: the share of an agent's current-policy bullets coded ``<T>``, averaged over the agents (each agent
    weighs the same, as in ``cluster_entropy``). ``.dominant``: the share of those agents whose dominant category is
    ``<T>``. So the nine shares sum to 1, and ``-sum p log2 p`` over them is ``cluster_entropy`` with the same
    arguments, whenever ``cluster_entropy`` is defined; None exactly when it is None.

Any other argument raises ``ValueError`` (a typo in a config's outcome list fails loudly).

Caveats. Excerpt matching and the bullet match are textual: a practice reworded on the way is a new bullet, so
``novelty_share`` counts rewording as novelty and ``lineage_depth`` follows recorded hops, not meaning. The keyword
coder is a deterministic baseline for the codebook, not a validated instrument: the categories of real policies need
the human or LLM coding of §7.3, validated with :func:`cohens_kappa`.

Coding tooling (driven by ``scripts/code_practices.py``):

* :func:`sample_policy_lines`: a blind sample of policy versions from every run under a root, one item per bullet,
  with no arm, experiment, run or agent column, ids hashed with a salt, shuffled with a seeded stream; the mapping
  back to runs goes to a separate key file.
* :func:`keyword_code`: the codebook's keyword rules (:data:`KEYWORDS`, tie order :data:`TIE_ORDER`).
* :func:`cohens_kappa` and :func:`agreement`: Cohen's kappa between two coders.
"""

from __future__ import annotations

import csv
import hashlib
import math
import re
import sqlite3
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from void.config import micro_to_usd
from void.culture.practices import policy_lines
from void.research.analysis import _alive_ids, _has_tables, _mean, connect, run_config
from void.research.outcomes import register
from void.rng import RNG

__all__ = [
    "CODES",
    "CODE_NAMES",
    "CODES_FILE",
    "KEYWORDS",
    "KEY_COLUMNS",
    "OTHER",
    "SAMPLE_COLUMNS",
    "TIE_ORDER",
    "SampleItem",
    "adoption_latency_ticks",
    "adoption_rate",
    "agreement",
    "bullets",
    "cluster_entropy",
    "code_profile",
    "code_table",
    "cohens_kappa",
    "dominant_code",
    "first_adoption_source_richer_share",
    "first_adoptions",
    "keyword_code",
    "keyword_scores",
    "lineage_depth",
    "normalize_code",
    "normalize_line",
    "novelty_share",
    "pair_adoptions",
    "persistence_rate",
    "practice_share",
    "read_codes",
    "sample_policy_lines",
    "write_csv",
]

# --- the codebook ------------------------------------------------------------------------------------------------
# The categories of MEMORY_EVOLUTION §7.3, verbatim; docs/research/codebook.md defines each (a test keeps the three
# in step).
CODE_NAMES: dict[str, str] = {
    "T1": "Indexing: maps of content, hub notes",
    "T2": "Compression: summaries, merging notes",
    "T3": "Typing and tagging: fact, claim, plan",
    "T4": "Pruning: deleting, expiring, correcting",
    "T5": "Source tracking: who said it, and whether it proved true",
    "T6": "Question anticipation: writing notes as answers to future needs",
    "T7": "Heuristics: rules of thumb for action",
    "T8": "Social: whom to trust, what to share",
    "T9": "Other",
}
CODES: tuple[str, ...] = tuple(CODE_NAMES)
OTHER = "T9"

# Keyword rules: a rule matches when its words start a word of the normalized line, in order (``delet`` matches
# "deleted", ``map of`` matches "map of nodes"), so a rule is a word prefix, never a fragment inside a word.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "T1": ("index", "hub", "map of", "maps of", "table of contents", "link", "backlink", "overview", "catalog",
           "directory"),
    "T2": ("summar", "merg", "compress", "condens", "consolidat", "combin", "digest", "duplicat", "dedup", "distil"),
    "T3": ("tag", "type", "label", "categor", "classif", "fact", "claim", "plan", "frontmatter", "front matter"),
    "T4": ("delet", "remov", "prun", "expir", "forget", "discard", "drop", "archiv", "older than", "outdat", "stale",
           "obsolet", "wrong", "correct", "fix", "clean up", "cleanup"),
    "T5": ("source", "who said", "who told", "told me", "came from", "heard from", "provenance", "rumour", "rumor",
           "verif", "turned out true", "turned out to be true", "proved true", "confirm", "cite", "attribut"),
    "T6": ("question", "wish i had known", "wish i knew", "need to know", "will need", "might need", "future",
           "anticipat", "answer", "look up", "lookup"),
    "T7": ("rule", "heuristic", "lesson", "strategy", "tactic", "before moving", "go to", "move to", "route",
           "forag", "richest", "best", "avoid", "prefer", "repeat", "income", "spend", "earn", "pay", "paid"),
    "T8": ("trust", "distrust", "mistrust", "share", "liar", "honest", "gossip", "neighbour", "neighbor", "ally",
           "allies", "friend", "deceiv", "decept", "betray", "cooperat", "whom", "people", "person"),
}
# The most keyword matches wins; a tie goes to the earlier code here (the narrower practice before the broader one).
TIE_ORDER: tuple[str, ...] = ("T5", "T8", "T6", "T4", "T1", "T2", "T3", "T7")

CODES_FILE = "practice_codes.csv"
SAMPLE_COLUMNS: tuple[str, ...] = ("item_id", "policy_id", "line_no", "text", "code")
KEY_COLUMNS: tuple[str, ...] = ("item_id", "policy_id", "run", "agent_id", "version", "tick", "source", "line_no")

_WORD_RE = re.compile(r"[^\W_]+")


def normalize_line(line: str) -> str:
    """A practice line reduced to its words: lower case, every run of non-word characters (list markers, punctuation,
    link brackets) a single space, trimmed. ``"- Delete notes that turned out to be wrong."`` and ``"* delete notes
    that turned out to be wrong"`` are the same bullet."""
    return " ".join(_WORD_RE.findall(str(line).lower()))


def bullets(text: str | None) -> list[str]:
    """The distinct normalized practice lines of a policy text, in order (empty ones dropped)."""
    return list(dict.fromkeys(n for n in (normalize_line(line) for line in policy_lines(text)) if n))


def keyword_scores(line: str) -> dict[str, int]:
    """``{code: number of that code's keyword rules the line matches}`` for the codes with a match."""
    padded = f" {normalize_line(line)}"
    scores: dict[str, int] = {}
    for code, rules in KEYWORDS.items():
        hits = sum(1 for rule in rules if f" {normalize_line(rule)}" in padded)
        if hits:
            scores[code] = hits
    return scores


def keyword_code(line: str) -> str:
    """The keyword coder: the code with the most keyword matches, a tie broken by :data:`TIE_ORDER`, ``T9`` (other)
    when no rule matches. Deterministic: a pure function of the line."""
    scores = keyword_scores(line)
    if not scores:
        return OTHER
    best = max(scores.values())
    return next(code for code in TIE_ORDER if scores.get(code) == best)


def normalize_code(raw: object) -> str:
    """A coder's label as compared: stripped and upper-cased; a bare digit ``n`` from 1 to 9 is ``Tn``. Empty means
    not coded."""
    label = str(raw or "").strip().upper()
    if len(label) == 1 and label in "123456789":
        return f"T{label}"
    return label


# --- agreement ------------------------------------------------------------------------------------------------------
def agreement(a: Sequence[str], b: Sequence[str]) -> dict[str, Any]:
    """Cohen's kappa between two coders who labelled the same items in the same order, with its parts: ``n``,
    ``observed`` (share of items labelled the same), ``expected`` (chance agreement from each coder's label shares),
    ``kappa = (observed - expected) / (1 - expected)``, ``labels`` and the ``confusion`` counts ``{(a, b): n}``.

    When agreement is perfect ``kappa`` is 1.0, also in the degenerate case of both coders using one label throughout
    (where the formula is 0/0). Raises ``ValueError`` for no items or sequences of different lengths.
    """
    if len(a) != len(b):
        raise ValueError(f"the two codings have {len(a)} and {len(b)} items")
    n = len(a)
    if n == 0:
        raise ValueError("no items to compare")
    observed = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    labels = sorted(set(ca) | set(cb))
    expected = sum((ca[label] / n) * (cb[label] / n) for label in labels)
    if observed == 1.0:
        kappa = 1.0
    else:
        kappa = (observed - expected) / (1.0 - expected)
    return {"n": n, "observed": observed, "expected": expected, "kappa": kappa, "labels": labels,
            "confusion": dict(sorted(Counter(zip(a, b, strict=True)).items()))}


def cohens_kappa(a: Sequence[str], b: Sequence[str]) -> float:
    """Cohen's kappa between two codings of the same items (see :func:`agreement`)."""
    return float(agreement(a, b)["kappa"])


# --- practice events -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Event:
    event_id: str
    tick: int
    kind: str
    source: str
    target: str
    excerpt: str


def _no_arg(name: str, arg: str | None) -> None:
    if arg is not None:
        raise ValueError(f"outcome {name!r} takes no argument (got {arg!r})")


def _cutoff(run_dir: str | Path, day: int | None) -> int | None:
    return None if day is None else int(day) * int(run_config(run_dir).run.ticks_per_day)


def _read_events(db: sqlite3.Connection, cutoff: int | None) -> list[_Event] | None:
    """Every practice event written by ``cutoff`` (all of them when None), in write order; None without the table."""
    if not _has_tables(db, "practice_events"):
        return None
    sql = "SELECT event_id, tick, kind, source_agent_id, target_agent_id, excerpt FROM practice_events"
    params: tuple[Any, ...] = ()
    if cutoff is not None:
        sql += " WHERE tick <= ?"
        params = (cutoff,)
    rows = db.execute(sql + " ORDER BY tick, event_id", params).fetchall()
    return [_Event(str(r["event_id"]), int(r["tick"]), str(r["kind"]), str(r["source_agent_id"]),
                   str(r["target_agent_id"]), str(r["excerpt"] or "")) for r in rows]


def _events(run_dir: str | Path, day: int | None) -> list[_Event] | None:
    cutoff = _cutoff(run_dir, day)
    with connect(run_dir) as db:
        return _read_events(db, cutoff)


def pair_adoptions(events: Sequence[Any]) -> list[tuple[Any, Any | None]]:
    """``(adopted, suggestion or None)`` for every ``adopted`` event, in order (the rule is in the module docstring).

    Works on any objects with ``kind``, ``tick``, ``source``, ``target`` and ``excerpt`` in write order."""
    suggestions: dict[tuple[str, str], list[Any]] = {}
    for e in events:
        if e.kind == "suggested":
            suggestions.setdefault((e.source, e.target), []).append(e)
    out: list[tuple[Any, Any | None]] = []
    for e in events:
        if e.kind != "adopted":
            continue
        before = [s for s in suggestions.get((e.source, e.target), []) if s.tick <= e.tick]
        same = [s for s in before if normalize_line(s.excerpt) == normalize_line(e.excerpt)]
        pick = same[-1] if same else (before[-1] if before else None)
        out.append((e, pick))
    return out


def _count(events: Iterable[_Event], kind: str) -> int:
    return sum(1 for e in events if e.kind == kind)


def _adoption_rate_on_day(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    _no_arg("adoption_rate", arg)
    events = _events(run_dir, day)
    if events is None:
        return None
    suggested = _count(events, "suggested")
    return _count(events, "adopted") / suggested if suggested else None


def _latency_on_day(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    _no_arg("adoption_latency_ticks", arg)
    events = _events(run_dir, day)
    if events is None:
        return None
    return _mean(float(a.tick - s.tick) for a, s in pair_adoptions(events) if s is not None)


def _persistence_on_day(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    if arg not in (None, "resolved"):
        raise ValueError(f"persistence_rate argument {arg!r} must be 'resolved' or nothing")
    events = _events(run_dir, day)
    if events is None:
        return None
    persisted = _count(events, "persisted")
    denominator = persisted + _count(events, "dropped") if arg == "resolved" else _count(events, "adopted")
    return persisted / denominator if denominator else None


def _balances_at(db: sqlite3.Connection, ids: Iterable[str], tick: int) -> dict[str, int]:
    """Micro-dollar balance of each agent the ``agents`` table holds, after every ledger entry of ticks <= ``tick``."""
    out: dict[str, int] = {}
    for aid in sorted(set(ids)):
        row = db.execute("SELECT balance FROM agents WHERE agent_id=?", (aid,)).fetchone()
        if row is None:
            continue
        later = db.execute("SELECT COALESCE(SUM(delta), 0) AS s FROM wallet_ledger WHERE agent_id=? AND tick > ?",
                           (aid, int(tick))).fetchone()
        out[aid] = int(row["balance"]) - int(later["s"])
    return out


def first_adoptions(run_dir: str | Path, day: int | None = None) -> list[dict[str, Any]]:
    """One dict per agent that adopted (by the end of ``day``), sorted by agent id: ``agent_id``, ``tick``,
    ``source_agent_id``, ``adopter_balance_usd`` and ``source_balance_usd`` at that tick (None when the ``agents``
    table does not hold the agent), and ``richer`` (None when a balance is unknown).
    ``first_adoption_source_richer_share`` is the share of ``richer`` among the rows where it is known."""
    cutoff = _cutoff(run_dir, day)
    out: list[dict[str, Any]] = []
    with connect(run_dir) as db:
        events = _read_events(db, cutoff)
        if events is None:
            return []
        first: dict[str, _Event] = {}
        for e in events:
            if e.kind == "adopted":
                first.setdefault(e.target, e)
        for aid in sorted(first):
            e = first[aid]
            bal = _balances_at(db, (e.target, e.source), e.tick)
            mine, theirs = bal.get(e.target), bal.get(e.source)
            out.append({
                "agent_id": aid, "tick": e.tick, "source_agent_id": e.source,
                "adopter_balance_usd": None if mine is None else micro_to_usd(mine),
                "source_balance_usd": None if theirs is None else micro_to_usd(theirs),
                "richer": None if mine is None or theirs is None else theirs > mine,
            })
    return out


def _richer_on_day(run_dir: str | Path, arg: str | None, day: int | None) -> float | None:
    _no_arg("first_adoption_source_richer_share", arg)
    known = [row["richer"] for row in first_adoptions(run_dir, day) if row["richer"] is not None]
    return sum(known) / len(known) if known else None


@register("adoption_rate", daily=_adoption_rate_on_day)
def adoption_rate(run_dir: str | Path, arg: str | None = None) -> float | None:
    """``adopted`` practice events over ``suggested`` ones; None without a suggestion."""
    return _adoption_rate_on_day(run_dir, arg, None)


@register("adoption_latency_ticks", daily=_latency_on_day)
def adoption_latency_ticks(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean ticks from a suggestion to its adoption, over the adoptions paired with a suggestion; None without one."""
    return _latency_on_day(run_dir, arg, None)


@register("persistence_rate", daily=_persistence_on_day)
def persistence_rate(run_dir: str | Path, arg: str | None = None) -> float | None:
    """``persisted`` over ``adopted`` practice events (``.resolved``: over ``persisted + dropped``); None without a
    denominator."""
    return _persistence_on_day(run_dir, arg, None)


@register("first_adoption_source_richer_share", daily=_richer_on_day)
def first_adoption_source_richer_share(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Share of the agents that adopted whose first adoption came from a source richer than them at that tick."""
    return _richer_on_day(run_dir, arg, None)


# --- the policy graph ------------------------------------------------------------------------------------------------
Node = tuple[str, int]  # (agent_id, version)


@dataclass(frozen=True)
class _Version:
    agent_id: str
    version: int
    tick: int
    text: str
    source: str
    source_agent_id: str | None


@dataclass
class _Graph:
    versions: dict[Node, _Version]
    current: dict[str, Node]                                    # agent_id -> its highest version
    preds: dict[Node, list[tuple[Node, int]]] = field(default_factory=dict)
    loose_hops: dict[Node, int] = field(default_factory=dict)   # 1 where a hop's source text is not in the run
    received: dict[Node, list[str]] = field(default_factory=dict)
    seeds: list[str] = field(default_factory=list)


def _load_graph(db: sqlite3.Connection) -> _Graph | None:
    """The policy graph of the run (see the module docstring); None without a policy table."""
    if not _has_tables(db, "policy_versions"):
        return None
    rows = db.execute("SELECT agent_id, version, tick, text, source, source_agent_id FROM policy_versions "
                      "ORDER BY agent_id, version").fetchall()
    versions: dict[Node, _Version] = {}
    by_agent: dict[str, list[_Version]] = {}
    for r in rows:
        v = _Version(str(r["agent_id"]), int(r["version"]), int(r["tick"]), str(r["text"] or ""), str(r["source"]),
                     None if r["source_agent_id"] is None else str(r["source_agent_id"]))
        versions[(v.agent_id, v.version)] = v
        by_agent.setdefault(v.agent_id, []).append(v)
    ticks = {aid: [v.tick for v in vs] for aid, vs in by_agent.items()}

    def at(agent_id: str | None, tick: int) -> Node | None:
        """The version of ``agent_id`` current at ``tick`` (its last version written at or before it)."""
        if agent_id is None or agent_id not in by_agent:
            return None
        i = bisect_right(ticks[agent_id], tick)
        return None if i == 0 else (agent_id, by_agent[agent_id][i - 1].version)

    graph = _Graph(versions=versions, current={aid: (aid, vs[-1].version) for aid, vs in by_agent.items()})
    for aid, vs in by_agent.items():
        for i, v in enumerate(vs):
            node = (aid, v.version)
            edges: list[tuple[Node, int]] = []
            if v.source != "inherited" and i > 0:
                edges.append(((aid, vs[i - 1].version), 0))
            if v.source in ("inherited", "adopted"):
                src = at(v.source_agent_id, v.tick) if v.source_agent_id != aid else None
                if src is not None:
                    edges.append((src, 1))
                else:
                    graph.loose_hops[node] = 1
            if v.source == "inherited":
                graph.received.setdefault(node, []).append(v.text)
            if v.source == "seed":
                graph.seeds.append(v.text)
            graph.preds[node] = edges
    events = _read_events(db, None) or []
    for adopted, suggestion in pair_adoptions(events):
        node = at(adopted.target, adopted.tick)
        if node is None:
            continue  # the target had no policy yet: nothing to attach the hop to
        src_tick = suggestion.tick if suggestion is not None else adopted.tick
        src = at(adopted.source, src_tick) if adopted.source != adopted.target else None
        if src is not None:
            graph.preds[node].append((src, 1))
        else:
            graph.loose_hops[node] = 1
        graph.received.setdefault(node, []).append(adopted.excerpt)
    return graph


def _depths(graph: _Graph, starts: Iterable[Node]) -> dict[Node, int]:
    """Longest-path hop count behind each node reachable from ``starts`` (iterative, cycle-safe: an edge back into a
    node still being expanded is ignored)."""
    memo: dict[Node, int] = {}
    for start in starts:
        if start in memo:
            continue
        stack: list[tuple[Node, int]] = [(start, 0)]
        open_: set[Node] = {start}
        while stack:
            node, i = stack[-1]
            preds = graph.preds.get(node, [])
            if i < len(preds):
                stack[-1] = (node, i + 1)
                pred = preds[i][0]
                if pred not in memo and pred not in open_:
                    stack.append((pred, 0))
                    open_.add(pred)
                continue
            stack.pop()
            open_.discard(node)
            memo[node] = max([graph.loose_hops.get(node, 0)]
                             + [w + memo[p] for p, w in preds if p in memo])
    return memo


def _ancestor_bullets(graph: _Graph, start: Node) -> set[str]:
    """Every bullet ``start``'s novelty is checked against (the module docstring lists the texts)."""
    seen: set[tuple[Node, bool]] = set()
    out: set[str] = set()
    for text in graph.seeds:
        out.update(bullets(text))
    todo: list[tuple[Node, bool]] = [(start, False)]
    while todo:
        state = todo.pop()
        if state in seen:
            continue
        seen.add(state)
        node, crossed = state
        if crossed:
            out.update(bullets(graph.versions[node].text))
        for text in graph.received.get(node, []):
            out.update(bullets(text))
        for pred, w in graph.preds.get(node, []):
            todo.append((pred, crossed or w > 0))
    return out


def _living_graph(run_dir: str | Path) -> tuple[_Graph, list[str]] | None:
    """The policy graph and the living agents that have a policy (sorted); None without a policy table."""
    with connect(run_dir) as db:
        graph = _load_graph(db)
        if graph is None:
            return None
        alive = [aid for aid in _alive_ids(db) if aid in graph.current]
    return graph, alive


@register("lineage_depth")
def lineage_depth(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Mean over the living agents' current policies of the hops (inherited or adopted) on the longest path behind
    each; None without a living agent that has a policy."""
    _no_arg("lineage_depth", arg)
    loaded = _living_graph(run_dir)
    if loaded is None:
        return None
    graph, alive = loaded
    starts = [graph.current[aid] for aid in alive]
    depth = _depths(graph, starts)
    return _mean(float(depth[n]) for n in starts)


@register("novelty_share")
def novelty_share(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Share of the bullets of the living agents' current policies found in no seed and no inherited or adopted
    ancestor text (normalized line match); None without a bullet."""
    _no_arg("novelty_share", arg)
    loaded = _living_graph(run_dir)
    if loaded is None:
        return None
    graph, alive = loaded
    total = novel = 0
    for aid in alive:
        node = graph.current[aid]
        mine = bullets(graph.versions[node].text)
        if not mine:
            continue
        known = _ancestor_bullets(graph, node)
        total += len(mine)
        novel += sum(1 for b in mine if b not in known)
    return novel / total if total else None


# --- coding and cluster entropy ------------------------------------------------------------------------------------
def read_codes(path: str | Path) -> dict[str, str]:
    """``{normalized line: code}`` from a codes CSV with ``text`` and ``code`` columns (rows without a code are
    skipped). A text coded more than once takes its most frequent code, a tie the lower code number. Raises
    ``ValueError`` for missing columns or a code outside T1 to T9."""
    votes: dict[str, Counter[str]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or not {"text", "code"} <= set(reader.fieldnames):
            raise ValueError(f"{path} needs 'text' and 'code' columns (has {reader.fieldnames})")
        for row in reader:
            code = normalize_code(row.get("code"))
            if not code:
                continue
            if code not in CODE_NAMES:
                raise ValueError(f"{path}: code {row.get('code')!r} is not one of {', '.join(CODES)}")
            key = normalize_line(row.get("text") or "")
            if key:
                votes.setdefault(key, Counter())[code] += 1
    return {key: min(c, key=lambda code: (-c[code], CODES.index(code))) for key, c in votes.items()}


def code_table(run_dir: str | Path, *, keyword: bool = False) -> dict[str, str]:
    """The codes a run's bullets are looked up in first: :func:`read_codes` of ``practice_codes.csv`` in the run
    directory, or ``{}`` (the keyword coder for every bullet) without the file or with ``keyword``."""
    path = Path(run_dir) / CODES_FILE
    return read_codes(path) if path.is_file() and not keyword else {}


def code_profile(text: str | None, table: Mapping[str, str]) -> Counter[str]:
    """``{code: bullets}`` of a policy text: each of its :func:`bullets` coded by ``table`` (normalized text to code),
    falling back to :func:`keyword_code`. Empty for a text without a bullet."""
    return Counter(table.get(b) or keyword_code(b) for b in bullets(text))


def dominant_code(profile: Mapping[str, int]) -> str:
    """The most frequent code of a non-empty profile; a tie goes to the lower code number."""
    return min(profile, key=lambda code: (-profile[code], CODES.index(code)))


def _entropy_bits(shares: Iterable[float]) -> float:
    return -sum(p * math.log2(p) for p in shares if p > 0.0) + 0.0


def _coding_flags(name: str, arg: str | None) -> list[str]:
    """The ``dominant`` / ``keyword`` flags of a coding outcome's argument (``cluster_entropy``'s grammar)."""
    flags = [] if arg is None else arg.split(".")
    if any(f not in ("dominant", "keyword") for f in flags):
        raise ValueError(f"{name} argument {arg!r} must be made of 'dominant' and 'keyword'")
    return flags


def _category_shares(run_dir: str | Path, flags: Sequence[str]) -> dict[str, float] | None:
    """The category distribution of the living agents' current policies whose entropy is ``cluster_entropy`` (every
    code of :data:`CODES` a key); None without a living agent whose policy has a bullet."""
    loaded = _living_graph(run_dir)
    if loaded is None:
        return None
    graph, alive = loaded
    table = code_table(run_dir, keyword="keyword" in flags)
    profiles = [p for p in (code_profile(graph.versions[graph.current[aid]].text, table) for aid in alive) if p]
    if not profiles:
        return None
    if "dominant" in flags:
        clusters = Counter(dominant_code(p) for p in profiles)
        return {c: clusters[c] / len(profiles) for c in CODES}
    return {c: sum(p[c] / sum(p.values()) for p in profiles) / len(profiles) for c in CODES}


@register("cluster_entropy")
def cluster_entropy(run_dir: str | Path, arg: str | None = None) -> float | None:
    """H8's cluster entropy in bits over the living agents' current policies (see the module docstring)."""
    shares = _category_shares(run_dir, _coding_flags("cluster_entropy", arg))
    return None if shares is None else _entropy_bits(shares[c] for c in CODES)


@register("practice_share")
def practice_share(run_dir: str | Path, arg: str | None = None) -> float | None:
    """The share of one practice category in ``cluster_entropy``'s distribution:
    ``practice_share.<T>[.dominant][.keyword]`` (see the module docstring); None when ``cluster_entropy`` is."""
    code, dot, rest = (arg or "").partition(".")
    if code not in CODE_NAMES:
        raise ValueError(f"practice_share argument {arg!r} must start with a category {CODES[0]} to {CODES[-1]}: "
                         "practice_share.<T>[.dominant][.keyword]")
    shares = _category_shares(run_dir, _coding_flags("practice_share", rest if dot else None))
    return None if shares is None else shares[code]


# --- the blind sampler -----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SampleItem:
    """One bullet of a sampled policy version: the blind columns (``item_id``, ``policy_id``, ``line_no``, ``text``)
    and the key back to its run (``run``, ``agent_id``, ``version``, ``tick``, ``source``)."""

    item_id: str
    policy_id: str
    line_no: int
    text: str
    run: str
    agent_id: str
    version: int
    tick: int
    source: str

    def blind_row(self) -> dict[str, Any]:
        return {"item_id": self.item_id, "policy_id": self.policy_id, "line_no": self.line_no, "text": self.text,
                "code": ""}

    def key_row(self) -> dict[str, Any]:
        return {"item_id": self.item_id, "policy_id": self.policy_id, "run": self.run, "agent_id": self.agent_id,
                "version": self.version, "tick": self.tick, "source": self.source, "line_no": self.line_no}


def _hashed(prefix: str, salt: str, *parts: object) -> str:
    h = hashlib.blake2b(digest_size=6, key=salt.encode("utf-8")[:64])
    h.update("\x1f".join(str(p) for p in parts).encode("utf-8"))
    return f"{prefix}_{h.hexdigest()}"


def sample_policy_lines(runs_root: str | Path, *, n: int = 200, seed: int = 0, salt: str | None = None,
                        distinct: bool = True) -> list[SampleItem]:
    """A blind sample of ``n`` policy versions from every run under ``runs_root`` (each directory holding a
    ``world.db`` with a policy table), one :class:`SampleItem` per practice line.

    The pool is every version with at least one practice line, in sorted order (run path, agent, version); with
    ``distinct`` only the first version of each text is kept, so the seed text and verbatim inherited copies come in
    once. ``n`` versions are drawn without replacement by ``RNG(seed).stream("practice_sample")`` and listed in the
    order drawn, each version's lines together and in order (the coder reads a line in its policy). Ids are salted
    hashes of the run path, agent, version and line (``salt`` defaults to one derived from ``seed``; pass a private
    one when the coder could know the layout of the runs), so neither ids nor order reveal the arm.
    """
    root = Path(runs_root)
    if n < 1:
        raise ValueError("n must be >= 1")
    salt = f"practice-sample:{seed}" if salt is None else salt
    pool: list[tuple[str, _Version]] = []
    seen_texts: set[str] = set()
    for db_path in sorted(root.rglob("world.db"), key=lambda p: p.relative_to(root).as_posix()):
        run_dir = db_path.parent
        rel = run_dir.relative_to(root).as_posix()
        with connect(run_dir) as db:
            if not _has_tables(db, "policy_versions"):
                continue
            rows = db.execute("SELECT agent_id, version, tick, text, source, source_agent_id FROM policy_versions "
                              "ORDER BY agent_id, version").fetchall()
        for r in rows:
            text = str(r["text"] or "")
            if not bullets(text):
                continue
            if distinct:
                if text.strip() in seen_texts:
                    continue
                seen_texts.add(text.strip())
            pool.append((rel, _Version(str(r["agent_id"]), int(r["version"]), int(r["tick"]), text, str(r["source"]),
                                       None if r["source_agent_id"] is None else str(r["source_agent_id"]))))
    picks = RNG(int(seed)).stream("practice_sample").sample(pool, min(int(n), len(pool)))
    items: list[SampleItem] = []
    for rel, v in picks:
        policy_id = _hashed("pv", salt, rel, v.agent_id, v.version)
        for line_no, line in enumerate((ln for ln in policy_lines(v.text) if normalize_line(ln)), start=1):
            items.append(SampleItem(item_id=_hashed("it", salt, rel, v.agent_id, v.version, line_no),
                                    policy_id=policy_id, line_no=line_no, text=line, run=rel, agent_id=v.agent_id,
                                    version=v.version, tick=v.tick, source=v.source))
    return items


def write_csv(path: str | Path, columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> int:
    """Write ``rows`` (only ``columns``, in that order) as CSV with a header; returns the row count."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
            count += 1
    return count
