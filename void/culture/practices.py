"""Practice transmission between agents (MEMORY_EVOLUTION §5.7): the ``practice_events`` ledger.

An agent suggests an excerpt of its memory policy to a neighbour with ``share_practice``; the kernel delivers it
fenced and records it here as ``suggested``. Adoption is measured later by the kernel (an excerpt counts as adopted
when the receiver's new policy contains it at ``memory.practices.adopt_similarity``), never self-reported; those
rows (``adopted``, ``persisted``, ``dropped``) go through :meth:`PracticeLedger.record`, so every kind shares one
id sequence.

The measure is :func:`excerpt_similarity`: the best :func:`void.memory.policy.similarity` (token-set Jaccard, the
placeholder until a real embedder exists) between the excerpt and a window of consecutive non-empty lines of the
policy, the window one line shorter, as long as, or one line longer than the excerpt. The nightly step
(:mod:`void.sim.maintenance`) records ``adopted`` when an agent's maintenance leaves a pending excerpt in its policy
that was not there before; at each later day start it settles every adoption ``memory.practices.persist_days`` old:
``persisted`` when the receiver is alive and its policy still holds the excerpt, ``dropped`` otherwise. Each row
carries the similarity measured when it was written.

Event ids are deterministic: ``pe_<n:08d><hash6>`` where ``n`` counts the rows already in the table, so a resumed
run continues the sequence and two runs of the same config write the same ids. The ledger draws no randomness and
emits no events; the caller (the kernel) emits the matching ``Kind.PRACTICE_*`` event.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from void.db import Database
from void.memory.policy import similarity

__all__ = ["PracticeKind", "PRACTICE_KINDS", "PracticeLedger", "policy_lines", "excerpt_similarity"]

PracticeKind = Literal["suggested", "adopted", "persisted", "dropped"]
PRACTICE_KINDS: tuple[PracticeKind, ...] = ("suggested", "adopted", "persisted", "dropped")

_COLUMNS = "event_id, tick, kind, source_agent_id, target_agent_id, excerpt, similarity"
_WORD_RE = re.compile(r"[a-z0-9]+")  # the word tokens of void.memory.policy.similarity


def policy_lines(text: str | None) -> list[str]:
    """The practice lines of a policy text, in order: non-empty lines that are not markdown headings, stripped.

    These are what an excerpt is cut from: a heading (``# How I remember (v1)``) names the file, it is no practice.
    """
    if not text:
        return []
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _content_lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def excerpt_similarity(excerpt: str, text: str | None) -> float:
    """How closely ``text`` (a policy) holds ``excerpt``: the best token-set similarity between the excerpt and a window
    of consecutive non-empty lines of the text, windows of the excerpt's line count and one line either side (so a
    practice split over two lines, or two merged into one, still matches). 0.0 when either side has no words."""
    lines = _content_lines(text)
    ex = _content_lines(excerpt)
    joined = " ".join(ex)
    if not lines or not _WORD_RE.search(joined.lower()):  # the tokens similarity() counts; none: nothing to hold
        return 0.0
    best = 0.0
    n = len(ex)
    for size in sorted({max(1, n - 1), n, n + 1}):
        if size > len(lines):
            continue
        for i in range(len(lines) - size + 1):
            best = max(best, similarity(joined, " ".join(lines[i:i + size])))
    return best


class PracticeLedger:
    """Reads and writes ``practice_events``. Writes join the caller's transaction (the kernel applies actions inside
    the tick's)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def _next_id(self, tick: int, kind: str, source_id: str, target_id: str) -> str:
        n = int(self.db.fetchone("SELECT COUNT(*) AS n FROM practice_events")["n"]) + 1
        digest = hashlib.blake2b(f"{tick}:{kind}:{source_id}:{target_id}:{n}".encode(), digest_size=3).hexdigest()
        return f"pe_{n:08d}{digest}"

    def record(self, kind: PracticeKind, tick: int, source_id: str, target_id: str, excerpt: str,
               similarity: float | None = None) -> str:
        """Insert one row of ``kind``; returns its event id."""
        if kind not in PRACTICE_KINDS:
            raise ValueError(f"unknown practice event kind {kind!r}")
        event_id = self._next_id(tick, kind, source_id, target_id)
        self.db.execute(f"INSERT INTO practice_events({_COLUMNS}) VALUES(?,?,?,?,?,?,?)",
                        (event_id, int(tick), kind, source_id, target_id, excerpt, similarity))
        return event_id

    def suggest(self, tick: int, source_id: str, target_id: str, excerpt: str) -> str:
        """Record that ``source_id`` suggested ``excerpt`` (already sanitized and clipped) to ``target_id``."""
        return self.record("suggested", tick, source_id, target_id, excerpt)

    def adopt(self, tick: int, source_id: str, target_id: str, excerpt: str, similarity: float) -> str:
        """Record that the kernel measured ``excerpt`` from ``source_id`` in ``target_id``'s new policy."""
        return self.record("adopted", tick, source_id, target_id, excerpt, similarity)

    def settle(self, kept: bool, tick: int, source_id: str, target_id: str, excerpt: str, similarity: float) -> str:
        """Record how an adoption ended once it was ``persist_days`` old: ``persisted`` when ``kept``, else ``dropped``."""
        if kept:
            return self.record("persisted", tick, source_id, target_id, excerpt, similarity)
        return self.record("dropped", tick, source_id, target_id, excerpt, similarity)

    def adopted_between(self, after_tick: int, through_tick: int) -> list[dict[str, Any]]:
        """The ``adopted`` rows with ``after_tick < tick <= through_tick``, in the order written (to settle)."""
        rows = self.db.fetchall(f"SELECT {_COLUMNS} FROM practice_events WHERE kind='adopted' AND tick>? AND tick<=? "
                                "ORDER BY tick, event_id", (int(after_tick), int(through_tick)))
        return [dict(r) for r in rows]

    def pending_for(self, agent_id: str, since_tick: int) -> list[dict[str, Any]]:
        """The suggestions ``agent_id`` received at or after ``since_tick``, oldest first (for the maintenance prompt:
        "suggestions received from neighbours, fenced, with their sources", MEMORY_EVOLUTION Appendix C)."""
        rows = self.db.fetchall(f"SELECT {_COLUMNS} FROM practice_events WHERE kind='suggested' AND target_agent_id=? AND tick>=? "
                                "ORDER BY tick, event_id", (agent_id, int(since_tick)))
        return [dict(r) for r in rows]

    def rows(self, kind: PracticeKind | None = None) -> list[dict[str, Any]]:
        """Every row (of ``kind`` when given), in the order written."""
        if kind is None:
            rows = self.db.fetchall(f"SELECT {_COLUMNS} FROM practice_events ORDER BY tick, event_id")
        else:
            rows = self.db.fetchall(f"SELECT {_COLUMNS} FROM practice_events WHERE kind=? ORDER BY tick, event_id", (kind,))
        return [dict(r) for r in rows]
