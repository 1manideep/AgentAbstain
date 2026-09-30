"""Wikilink graph over one agent's notes and the N-hop traversal used by retrieval.

Titles resolve case-insensitively with whitespace collapsed, mirroring Obsidian. Links to
titles that no note carries are remembered as *dangling* (so a later note can resolve them)
and ignored when walking. The walk is a plain BFS: a note first reached at hop ``h`` from an
entry point with score ``s`` gets ``s * decay**h``; when several entry points reach the same
note the highest score wins. Notes carrying a *hub* tag (entity stubs) are traversed in both
directions, so ``[[Bao]]`` connects every note that mentions Bao.
"""

from __future__ import annotations

from collections.abc import Iterable

from void.memory.notes import Note, normalise_title

__all__ = ["NoteGraph"]


class NoteGraph:
    def __init__(self, notes: Iterable[Note], hub_tags: Iterable[str] = ()) -> None:
        self._notes: dict[str, Note] = {}
        self._by_title: dict[str, str] = {}
        hubs = set(hub_tags)
        for note in notes:
            if note.note_id in self._notes:
                continue
            self._notes[note.note_id] = note
            # First note with a title owns it; later duplicates stay reachable by id only.
            self._by_title.setdefault(normalise_title(note.title), note.note_id)
        self._hubs: set[str] = {nid for nid, n in self._notes.items() if hubs and hubs & set(n.tags)}
        self._out: dict[str, list[str]] = {nid: [] for nid in self._notes}
        self._in: dict[str, list[str]] = {nid: [] for nid in self._notes}
        self._dangling: dict[str, list[str]] = {nid: [] for nid in self._notes}
        for nid, note in self._notes.items():
            for title in note.links:
                target = self._by_title.get(normalise_title(title))
                if target is None:
                    self._dangling[nid].append(title)
                elif target != nid and target not in self._out[nid]:
                    self._out[nid].append(target)
                    self._in[target].append(nid)

    # --- lookup -----------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._notes)

    def __contains__(self, note_id: object) -> bool:
        return note_id in self._notes

    def note(self, note_id: str) -> Note | None:
        return self._notes.get(note_id)

    def resolve(self, title: str) -> str | None:
        """The note id a ``[[title]]`` link points at, or ``None`` if it dangles."""
        return self._by_title.get(normalise_title(title))

    def links_out(self, note_id: str) -> list[str]:
        """Ids of notes this note links to (resolved links only, in link order)."""
        return list(self._out.get(note_id, ()))

    def backlinks(self, note_id: str) -> list[str]:
        """Ids of notes that link to this note."""
        return list(self._in.get(note_id, ()))

    def dangling(self, note_id: str) -> list[str]:
        """Link titles in this note that no note in the graph carries."""
        return list(self._dangling.get(note_id, ()))

    def is_hub(self, note_id: str) -> bool:
        return note_id in self._hubs

    # --- traversal --------------------------------------------------------------------
    def _neighbours(self, note_id: str) -> list[str]:
        out = self._out[note_id]
        if note_id not in self._hubs:
            return out
        return out + [nid for nid in self._in[note_id] if nid not in out]

    def walk_with_hops(
        self, entry: dict[str, float], hops: int, decay: float
    ) -> dict[str, tuple[float, int]]:
        """BFS from each entry point; per note the best ``(score * decay**hop, hop)`` is kept.

        Entry ids unknown to the graph are skipped. ``hops == 0`` returns the entry points.
        """
        best: dict[str, tuple[float, int]] = {}
        for start in sorted(entry):
            score = entry[start]
            if start not in self._notes:
                continue
            frontier = [start]
            seen = {start}
            for hop in range(max(0, hops) + 1):
                hop_score = score * (decay**hop)
                for nid in frontier:
                    cur = best.get(nid)
                    if cur is None or hop_score > cur[0] or (hop_score == cur[0] and hop < cur[1]):
                        best[nid] = (hop_score, hop)
                if hop == hops:
                    break
                nxt: list[str] = []
                for nid in frontier:
                    for target in self._neighbours(nid):
                        if target not in seen:
                            seen.add(target)
                            nxt.append(target)
                if not nxt:
                    break
                frontier = nxt
        return best

    def walk(self, entry: dict[str, float], hops: int, decay: float) -> dict[str, float]:
        """Scores only; see :meth:`walk_with_hops`."""
        return {nid: score for nid, (score, _) in self.walk_with_hops(entry, hops, decay).items()}
