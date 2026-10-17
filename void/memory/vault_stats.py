"""Vault growth and structure statistics (MEMORY_EVOLUTION §5.10 and the vault metrics of §9).

Free of simulation imports: the metrics recorder writes these into every day row, and research outcomes and the
server reuse them on a finished run. ``db`` is anything with ``execute(sql, params)`` returning rows that index by
position: a :class:`void.db.Database` or a plain ``sqlite3.Connection`` with or without ``sqlite3.Row``.

Definitions, per agent (every figure iterates in sorted order, so the result is a pure function of the run):

* **notes**: the agent's live notes (``archived = 0``) except entity stubs (the ``entity`` tag MemoryStore puts on
  the link anchors it creates for other inhabitants). Stubs are kernel scaffolding, not content.
* **links**: a ``note_links`` row counts when its ``to_title`` resolves, case-insensitively with whitespace
  collapsed, to the title of one of the agent's counted notes; the oldest note (``created_tick``, then
  ``note_id``) owns a title, as in :class:`void.memory.graph.NoteGraph`. A note's links are its distinct resolved
  targets other than itself; links to stubs, archived notes, other vaults or unknown titles do not count.
* **bytes**: the size of each note's file, the path resolved as MemoryStore resolves it (the ``notes.path`` column
  as stored; with ``run_dir`` the path is first rebased onto ``<run_dir>/vaults/<agent>/notes/<file>`` so a moved
  or copied run still reads). A missing file counts 0.
* **mean_note_chars**: mean characters of a note's body, which is the file after its frontmatter block with the
  final newline dropped (what MemoryStore loads as the body).
* **link_density**: resolved links per note.
* **orphan_rate**: share of notes with no resolved outgoing link and no resolved incoming link.
* **index_coverage**: share of the non-index notes reachable within two link hops, following links in either
  direction, from a note tagged ``index``. ``None`` without an index note (or when every note is an index note).

Population figures pool the agents' counts: ``mean_note_chars``, ``link_density`` and ``orphan_rate`` divide the
summed numerators by the summed note counts, and ``index_coverage`` is the share of all non-index notes covered by
an index note of their own vault (``None`` when no agent has an index note). Rates are ``None`` without notes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from void.memory.notes import normalise_title
from void.memory.store import ENTITY_TAG

__all__ = [
    "INDEX_HOPS",
    "INDEX_TAG",
    "VAULT_KEYS",
    "agent_vault_stats",
    "resolve_note_path",
    "vault_report",
    "vault_stats",
]

INDEX_TAG = "index"  # the tag of a note under /memories/index/ (void.memory.commands.INDEX_TAG)
INDEX_HOPS = 2
VAULT_KEYS: tuple[str, ...] = ("notes", "bytes", "mean_note_chars", "link_density", "orphan_rate", "index_coverage")
_ROUND = 4
# a note file is <root>/<agent_id>/notes/<file>.md (void.memory.vault.Vault): the last four parts below a run dir
_VAULT_PARTS = 4
_VAULTS_DIR = "vaults"


@dataclass
class _Tally:
    """Raw counts for one vault (or the sum of several)."""

    notes: int = 0
    bytes: int = 0
    chars: int = 0
    links: int = 0
    orphans: int = 0
    index_notes: int = 0
    non_index: int = 0
    covered: int = 0

    def add(self, other: _Tally) -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    def finish(self) -> dict[str, Any]:
        n = self.notes
        return {
            "notes": n,
            "bytes": self.bytes,
            "mean_note_chars": _ratio(self.chars, n),
            "link_density": _ratio(self.links, n),
            "orphan_rate": _ratio(self.orphans, n),
            "index_coverage": _ratio(self.covered, self.non_index) if self.index_notes else None,
        }


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, _ROUND) if den else None


def resolve_note_path(path: str, run_dir: str | Path | None = None) -> Path:
    """The file a ``notes.path`` value names: as stored (what MemoryStore reads), or rebased onto ``run_dir``.

    With ``run_dir`` the last four parts (``<agent>/notes/<file>`` under the vault root) are joined onto
    ``<run_dir>/vaults`` and used when that file exists; otherwise the stored path is returned unchanged.
    """
    p = Path(path)
    if run_dir is not None and len(p.parts) >= _VAULT_PARTS:
        rebased = Path(run_dir) / _VAULTS_DIR / Path(*p.parts[-(_VAULT_PARTS - 1):])
        if rebased.is_file():
            return rebased
    return p


def _body_chars(data: bytes) -> int:
    """Characters of the body: the text after a leading ``---`` frontmatter block, final newline dropped."""
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    rest = text
    if lines[0].rstrip("\r") == "---":
        for i in range(1, len(lines)):
            if lines[i].rstrip("\r") == "---":
                rest = "\n".join(lines[i + 1:])
                break
    if rest.endswith("\n"):
        rest = rest[:-1]
    return len(rest)


def _tags(raw: object) -> set[str]:
    try:
        items = json.loads(str(raw) if raw is not None else "[]")
    except (TypeError, ValueError):
        return set()
    return {str(t) for t in items} if isinstance(items, list) else set()


def _tally(db: Any, agent_id: str, run_dir: str | Path | None) -> _Tally:
    rows = db.execute(
        "SELECT note_id, path, title, tags FROM notes WHERE agent_id=? AND archived=0 ORDER BY created_tick, note_id",
        (agent_id,),
    ).fetchall()
    notes: list[tuple[str, str, bool]] = []  # (note_id, path, is_index), oldest first
    owner: dict[str, str] = {}
    for r in rows:
        tags = _tags(r[3])
        if ENTITY_TAG in tags:
            continue
        nid = str(r[0])
        notes.append((nid, str(r[1]), INDEX_TAG in tags))
        owner.setdefault(normalise_title(str(r[2])), nid)
    t = _Tally()
    if not notes:
        return t
    out: dict[str, set[str]] = {nid: set() for nid, _, _ in notes}
    inc: dict[str, set[str]] = {nid: set() for nid, _, _ in notes}
    link_rows = db.execute(
        "SELECT l.from_note_id, l.to_title FROM note_links l JOIN notes n ON n.note_id = l.from_note_id "
        "WHERE n.agent_id=? AND n.archived=0 ORDER BY l.from_note_id, l.to_title",
        (agent_id,),
    ).fetchall()
    for lr in link_rows:
        src = str(lr[0])
        target = owner.get(normalise_title(str(lr[1])))
        if src not in out or target is None or target == src:
            continue
        out[src].add(target)
        inc[target].add(src)

    t.notes = len(notes)
    for nid, path, is_index in notes:
        try:
            data = resolve_note_path(path, run_dir).read_bytes()
        except OSError:
            data = b""
        t.bytes += len(data)
        t.chars += _body_chars(data) if data else 0
        t.links += len(out[nid])
        if not out[nid] and not inc[nid]:
            t.orphans += 1
        if is_index:
            t.index_notes += 1
        else:
            t.non_index += 1

    if t.index_notes:
        frontier = sorted(nid for nid, _, is_index in notes if is_index)
        reached = set(frontier)
        for _ in range(INDEX_HOPS):
            nxt: list[str] = []
            for nid in frontier:
                for other in sorted(out[nid] | inc[nid]):
                    if other not in reached:
                        reached.add(other)
                        nxt.append(other)
            frontier = nxt
        t.covered = sum(1 for nid, _, is_index in notes if not is_index and nid in reached)
    return t


def vault_report(
    db: Any, agent_ids: Iterable[str], run_dir: str | Path | None = None
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """``(population stats, {agent_id: stats})`` over ``agent_ids`` in one pass; both carry :data:`VAULT_KEYS`."""
    total = _Tally()
    by_agent: dict[str, dict[str, Any]] = {}
    for aid in sorted({str(a) for a in agent_ids}):
        t = _tally(db, aid, run_dir)
        total.add(t)
        by_agent[aid] = t.finish()
    return total.finish(), by_agent


def vault_stats(db: Any, agent_ids: Iterable[str], run_dir: str | Path | None = None) -> dict[str, Any]:
    """Population vault stats pooled over ``agent_ids`` (see the module docstring for the definitions)."""
    return vault_report(db, agent_ids, run_dir)[0]


def agent_vault_stats(
    db: Any, agent_ids: Iterable[str] | str, run_dir: str | Path | None = None
) -> dict[str, dict[str, Any]]:
    """Per-agent vault stats ``{agent_id: stats}``; a single id may be passed as a plain string."""
    ids = [agent_ids] if isinstance(agent_ids, str) else list(agent_ids)
    return vault_report(db, ids, run_dir)[1]
