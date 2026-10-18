"""Practice transmission between agents (MEMORY_EVOLUTION §5.7): the ``practice_events`` ledger.

An agent suggests an excerpt of its memory policy to a neighbour with ``share_practice``; the kernel delivers it
fenced and records it here as ``suggested``. Adoption is measured later by the kernel (an excerpt counts as adopted
when the receiver's new policy contains it at ``memory.practices.adopt_similarity``), never self-reported; those
rows (``adopted``, ``persisted``, ``dropped``) go through :meth:`PracticeLedger.record`, so every kind shares one
id sequence.

Event ids are deterministic: ``pe_<n:08d><hash6>`` where ``n`` counts the rows already in the table, so a resumed
run continues the sequence and two runs of the same config write the same ids. The ledger draws no randomness and
emits no events; the caller (the kernel) emits the matching ``Kind.PRACTICE_*`` event.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from void.db import Database

__all__ = ["PracticeKind", "PRACTICE_KINDS", "PracticeLedger", "policy_lines"]

PracticeKind = Literal["suggested", "adopted", "persisted", "dropped"]
PRACTICE_KINDS: tuple[PracticeKind, ...] = ("suggested", "adopted", "persisted", "dropped")

_COLUMNS = "event_id, tick, kind, source_agent_id, target_agent_id, excerpt, similarity"


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
