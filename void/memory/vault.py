"""Per-agent markdown vault: ``<root>/<agent_id>/self.md`` and ``<root>/<agent_id>/notes/*.md``.

The vault is what an operator can open in Obsidian and what ``reindex`` rebuilds the
``notes`` table from. File names are ``<slug>-<id tail>.md``; a name that already belongs
to a *different* note is never overwritten (a numeric suffix is added instead). Moving the
whole directory is how the graveyard archives a bankrupt agent.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

from void.ids import slugify
from void.memory.notes import Note, parse_note, render_frontmatter, render_note, split_frontmatter

__all__ = ["Vault"]

_NOTE_ID_TAIL = 6


class Vault:
    def __init__(self, root: Path, agent_id: str) -> None:
        self.root = Path(root)
        self.agent_id = agent_id

    # --- layout -----------------------------------------------------------------------
    @property
    def dir(self) -> Path:
        return self.root / self.agent_id

    @property
    def notes_dir(self) -> Path:
        return self.dir / "notes"

    @property
    def self_path(self) -> Path:
        return self.dir / "self.md"

    def ensure(self) -> None:
        self.notes_dir.mkdir(parents=True, exist_ok=True)

    def note_path(self, note: Note) -> Path:
        """Canonical file for ``note`` before collision handling."""
        return self.notes_dir / f"{slugify(note.title)}-{note.note_id[-_NOTE_ID_TAIL:]}.md"

    # --- notes ------------------------------------------------------------------------
    def write_note(self, note: Note) -> Path:
        """Write (or rewrite) ``note``; sets ``note.path`` and returns it.

        If a file with the canonical name holds another note, ``-2``, ``-3``, ... is appended
        so no note ever overwrites a different one.
        """
        self.ensure()
        if (
            note.path
            and Path(note.path).parent == self.notes_dir
            and _file_note_id(Path(note.path)) == note.note_id
        ):
            path = Path(note.path)
        else:
            path = self.note_path(note)
            suffix = 2
            while path.exists() and _file_note_id(path) != note.note_id:
                path = path.with_name(f"{self.note_path(note).stem}-{suffix}.md")
                suffix += 1
        path.write_text(render_note(note), encoding="utf-8")
        note.path = str(path)
        return path

    def read_note(self, path: Path | str) -> Note:
        p = Path(path)
        return parse_note(p.read_text(encoding="utf-8"), agent_id=self.agent_id, path=str(p))

    def iter_notes(self) -> Iterator[Note]:
        """Every note file in the vault, in a stable (name-sorted) order."""
        if not self.notes_dir.is_dir():
            return
        for p in sorted(self.notes_dir.glob("*.md")):
            if p.is_file():
                yield self.read_note(p)

    # --- self node --------------------------------------------------------------------
    def write_self(self, summary: str, version: int, tick: int) -> Path:
        self.ensure()
        text = render_frontmatter({"version": int(version), "tick": int(tick)}) + summary + "\n"
        self.self_path.write_text(text, encoding="utf-8")
        return self.self_path

    def read_self(self) -> tuple[str, int]:
        """``(summary, version)``; ``("", 0)`` when no self.md exists."""
        if not self.self_path.is_file():
            return "", 0
        data, rest = split_frontmatter(self.self_path.read_text(encoding="utf-8"))
        summary = rest[:-1] if rest.endswith("\n") else rest
        try:
            version = int(data.get("version", 0))
        except (TypeError, ValueError):
            version = 0
        return summary, version

    # --- graveyard --------------------------------------------------------------------
    def move_to(self, dest_root: Path) -> Path:
        """Move the whole vault directory under ``dest_root``; the vault then lives there."""
        dest_root = Path(dest_root)
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / self.agent_id
        if dest.exists():
            raise FileExistsError(f"vault destination already exists: {dest}")
        if self.dir.exists():
            shutil.move(str(self.dir), str(dest))
        else:
            (dest / "notes").mkdir(parents=True)
        self.root = dest_root
        return dest


def _file_note_id(path: Path) -> str | None:
    try:
        data, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    value = data.get("id")
    return None if value is None else str(value)
