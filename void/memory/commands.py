"""The six memory commands an agent may issue over its own vault (MEMORY_EVOLUTION §5.4).

The command surface deliberately matches Anthropic's memory tool (``view``, ``create``, ``str_replace``,
``insert``, ``delete``, ``rename``) so a vault or a policy developed here is usable behind that product
surface and results are comparable to the default baseline anyone can run.

Design rules this module keeps (§5.4, §5.9):

* **Paths are virtual.** Agents see ``/memories/notes/<file>.md``, ``/memories/index/<file>.md``,
  ``/memories/memory_policy.md`` and ``/memories/self.md``. :func:`resolve_path` canonicalizes and
  confines every path to the agent's own vault; ``..``, URL-encoded traversal, absolute host paths,
  symlinks and non-``.md`` names are refused before anything touches the disk. A note's virtual path is
  its real file name under ``notes/`` or, when it carries the ``index`` tag, under ``index/``: both areas
  share one physical directory and the tag is the mapping, so ``reindex`` needs no second folder.
* **Writes go through the store.** A note created or edited by a command is written with
  :class:`~void.memory.store.MemoryStore` so the database row stays authoritative for provenance,
  importance, created tick and kernel tags. Agent text never carries frontmatter: it is stripped, and
  only the body region of a file is ever edited. The vault chooses file names (``<slug>-<id tail>.md``),
  so a success message names the file actually written when it differs from the one requested.
* **Rights gate commands.** ``notes`` allows ``create`` under ``/memories/notes/``; ``policy`` allows
  edits of the policy file (delegated to :mod:`void.memory.policy`); ``structure`` allows
  ``str_replace`` / ``insert`` / ``delete`` / ``rename`` on notes and the index directory. ``view`` is
  always allowed. ``self.md`` is read-only here (``revise_self`` stays the way to change it). Entity
  stubs and probe notes are kernel anchors: their bodies may be edited, never deleted or renamed.
* **Every command is audited** in ``memory_commands`` with its arguments, outcome, result hash and byte
  delta, and every file the kernel writes is recorded in ``note_manifest`` with a content hash (the
  store does that itself; :meth:`MemoryCommands.record_manifest` is a thin wrapper).
* **Limits** come from :class:`~void.config.MaintenanceConfig` (files per vault, bytes per file and per
  vault) and are enforced here, not by callers; the policy's own limits (characters, edits per day) come
  from the policy store's config.

Nothing here draws randomness or reads the clock; the only hash is blake2b over file text, so a run is
reproducible command for command. Agent-authored text is sanitized where it is stored (control characters
and angle brackets neutralised); whoever renders a result for a model fences it.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

from pydantic import BaseModel, Field, model_validator

from void.brain.decision import sanitize_text
from void.config import MaintenanceConfig, MaintenanceRight
from void.db import Database
from void.ids import IdFactory
from void.memory.notes import DEFAULT_TITLE, Note, normalise_title, split_frontmatter
from void.memory.policy import PolicyStore
from void.memory.store import ENTITY_TAG, PROBE_TAG, MemoryStore, clean_title, content_hash

__all__ = [
    "ROOT", "NOTES_DIR", "INDEX_DIR", "POLICY_PATH", "SELF_PATH", "AGENT_TAG", "INDEX_TAG", "CommandName", "MemoryCommand",
    "CommandResult", "VaultPath", "PathError", "resolve_path", "MAX_FILE_TEXT", "content_hash", "title_from_name", "virtual_path",
    "MemoryCommands",
]

ROOT = "/memories"
NOTES_DIR = f"{ROOT}/notes"
INDEX_DIR = f"{ROOT}/index"
POLICY_PATH = f"{ROOT}/memory_policy.md"
SELF_PATH = f"{ROOT}/self.md"

AGENT_TAG = "agent"   # every note an agent creates through a command
INDEX_TAG = "index"   # a note listed under /memories/index/ (and boosted by R1 retrieval, §5.3)
_PROTECTED: tuple[tuple[str, str], ...] = ((ENTITY_TAG, "a kernel entity anchor"), (PROBE_TAG, "a kernel probe"))

CommandName = Literal["view", "create", "str_replace", "insert", "delete", "rename"]
_RIGHT_FOR: dict[str, MaintenanceRight | None] = {
    "view": None, "create": "notes", "str_replace": "structure", "insert": "structure", "delete": "structure", "rename": "structure",
}
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.'-]{0,78}\.md$")
MAX_FILE_TEXT = 8000
MAX_PATH_CHARS = 200

SUCCESS_EDITED = "The memory file has been edited."


class MemoryCommand(BaseModel):
    """One command, in the flat shape the structured-output schema wants (every field present, unused ones null)."""

    model_config = {"extra": "forbid"}
    command: CommandName
    path: str | None = Field(default=None, max_length=MAX_PATH_CHARS)
    file_text: str | None = Field(default=None, max_length=MAX_FILE_TEXT)
    old_str: str | None = Field(default=None, max_length=MAX_FILE_TEXT)
    new_str: str | None = Field(default=None, max_length=MAX_FILE_TEXT)
    insert_line: int | None = Field(default=None, ge=0, le=10_000)
    insert_text: str | None = Field(default=None, max_length=MAX_FILE_TEXT)
    old_path: str | None = Field(default=None, max_length=MAX_PATH_CHARS)
    new_path: str | None = Field(default=None, max_length=MAX_PATH_CHARS)
    view_range: list[int] | None = Field(default=None, max_length=2)

    @model_validator(mode="after")
    def _required_fields(self) -> MemoryCommand:
        need = {
            "view": ("path",), "create": ("path", "file_text"), "str_replace": ("path", "old_str"),
            "insert": ("path", "insert_line", "insert_text"), "delete": ("path",), "rename": ("old_path", "new_path"),
        }[self.command]
        missing = [f for f in need if getattr(self, f) is None]
        if missing:
            raise ValueError(f"{self.command} requires {', '.join(missing)}")
        return self

    def args(self) -> dict[str, Any]:
        """The arguments as stored in ``memory_commands.args`` (nulls dropped, long text truncated)."""
        out = self.model_dump(exclude_none=True)
        out.pop("command", None)
        for k in ("file_text", "old_str", "new_str", "insert_text"):
            if k in out and len(out[k]) > 400:
                out[k] = out[k][:400] + "…"
        return out


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    message: str                      # the success string from §5.4, or the error text the agent sees
    bytes_delta: int = 0
    result_hash: str | None = None    # content hash of the file after the command, when one exists
    note_id: str | None = None        # the note touched, when the path is a note
    effects: dict[str, Any] = field(default_factory=dict)


class PathError(ValueError):
    """A path that is not a well-formed location inside the agent's own vault."""


class _Refusal(Exception):
    """An agent-readable refusal raised inside a handler; :meth:`MemoryCommands.apply` turns it into a result."""


@dataclass(frozen=True)
class VaultPath:
    """A validated virtual path: which area of the vault it names, and the bare file name (None for a directory)."""

    virtual: str                      # canonical form, e.g. ``/memories/notes/thin-node-3f2a1b.md``
    area: Literal["root", "notes", "index", "policy", "self"]
    name: str | None                  # file name within the area, None for the area directory itself

    @property
    def is_dir(self) -> bool:
        return self.name is None

    @property
    def is_note(self) -> bool:
        return self.name is not None and self.area in ("notes", "index")


def resolve_path(raw: str) -> VaultPath:
    """Canonicalize an agent-supplied path and confine it to the virtual vault layout.

    Rejects: anything outside ``/memories``, ``..`` or ``.`` segments (also URL-encoded), backslashes, NUL,
    hidden names, non-``.md`` files, names longer than 80 characters, nested directories beyond one level.
    Never touches the filesystem, so it is safe to call on untrusted text and cheap to property-test.
    """
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_CHARS:
        raise PathError("path must be a non-empty string")
    text = unquote(unquote(raw)).replace("\\", "/")
    if "\x00" in text or "%" in text:
        raise PathError("path contains forbidden characters")
    if not text.startswith("/"):
        text = "/" + text
    parts = [p for p in text.split("/") if p != ""]
    if any(p in (".", "..") or p.startswith(".") for p in parts):
        raise PathError("path may not contain '.', '..' or hidden names")
    canon = "/" + "/".join(parts)
    if canon == ROOT:
        return VaultPath(ROOT, "root", None)
    if canon == POLICY_PATH:
        return VaultPath(POLICY_PATH, "policy", "memory_policy.md")
    if canon == SELF_PATH:
        return VaultPath(SELF_PATH, "self", "self.md")
    for area, prefix in (("notes", NOTES_DIR), ("index", INDEX_DIR)):
        if canon == prefix:
            return VaultPath(prefix, area, None)  # type: ignore[arg-type]
        if canon.startswith(prefix + "/"):
            name = canon[len(prefix) + 1:]
            if "/" in name:
                raise PathError("only one level of directories is allowed")
            if not _FILE_RE.match(name):
                raise PathError("file names must be .md, start with a letter or digit and be at most 80 characters")
            return VaultPath(posixpath.join(prefix, name), area, name)  # type: ignore[arg-type]
    raise PathError(f"path must be under {ROOT} (notes/, index/, memory_policy.md or self.md)")


def title_from_name(name: str) -> str:
    """The note title a file name stands for: ``.md`` dropped, ``-`` and ``_`` become spaces, whitespace collapsed."""
    stem = name[:-3] if name.lower().endswith(".md") else name
    return " ".join(stem.replace("-", " ").replace("_", " ").split()) or DEFAULT_TITLE


def virtual_path(note: Note) -> str:
    """Where the agent sees ``note``: under ``index/`` when it carries the ``index`` tag, else under ``notes/``."""
    return posixpath.join(INDEX_DIR if INDEX_TAG in note.tags else NOTES_DIR, _file_name(note.path))


@dataclass(frozen=True)
class _Entry:
    """One live note as the listing and the limits see it (body bytes, last kernel write)."""

    note_id: str
    area: str
    name: str
    title: str
    size: int
    tick: int
    channel: str
    tags: tuple[str, ...]

    @property
    def virtual(self) -> str:
        return posixpath.join(INDEX_DIR if self.area == "index" else NOTES_DIR, self.name)


class MemoryCommands:
    """Apply validated commands to one agent's vault through the store, audit each one, keep the manifest.

    ``rights`` is the set of :data:`~void.config.MaintenanceRight` values the caller holds for this batch;
    ``purpose`` labels the audit row (``maintain`` for the nightly step, ``tick`` for per-tick ops routed
    here later, ``recall`` for the C3 tool, ``kernel`` for kernel-initiated writes).
    """

    def __init__(self, db: Database, store: MemoryStore, cfg: MaintenanceConfig, ids: IdFactory,
                 policy: PolicyStore | None = None) -> None:
        self.db = db
        self.store = store
        self.cfg = cfg
        self.ids = ids
        self.policy = policy  # edits of /memories/memory_policy.md are delegated to PolicyStore.set (source self_edit)

    # --- read side ---------------------------------------------------------------------------------
    def listing(self, agent_id: str, *, tick: int | None = None, area: str = "root") -> str:
        """The two-level directory listing the maintenance prompt and ``view`` show: sizes in bytes (of the
        agent-visible body) and the tick of the last kernel write. ``area`` narrows it to ``notes`` or ``index``;
        ``tick`` is accepted for prompt renderers but the listing is a function of the index alone."""
        entries = self._entries(agent_id)
        by_area = {"notes": [e for e in entries if e.area == "notes"], "index": [e for e in entries if e.area == "index"]}

        def files(name: str, indent: str) -> list[str]:
            rows = [f"{indent}{e.name}  ({e.size} bytes, tick {e.tick}, {e.channel})" for e in by_area[name]]
            return rows or [f"{indent}(empty)"]

        if area in ("notes", "index"):
            return "\n".join([f"{NOTES_DIR if area == 'notes' else INDEX_DIR}/", *files(area, "  ")])
        lines = [f"{ROOT}/"]
        self_text = self._self_text(agent_id)
        if self_text is not None:
            row = self.db.fetchone("SELECT MAX(tick) AS t FROM self_versions WHERE agent_id=?", (agent_id,))
            lines.append(f"  self.md  ({len(self_text.encode('utf-8'))} bytes, tick {_tick_of(row)})")
        policy_text = self._policy_text(agent_id)
        if policy_text is not None:
            row = self.db.fetchone("SELECT MAX(tick) AS t FROM policy_versions WHERE agent_id=?", (agent_id,))
            lines.append(f"  memory_policy.md  ({len(policy_text.encode('utf-8'))} bytes, tick {_tick_of(row)})")
        lines += ["  notes/", *files("notes", "    "), "  index/", *files("index", "    ")]
        return "\n".join(lines)

    def allowed(self, cmd: MemoryCommand, rights: list[MaintenanceRight]) -> str | None:
        """None when ``cmd`` is permitted under ``rights``, else the refusal message the agent will see."""
        need = _RIGHT_FOR[cmd.command]
        if need is None:
            return None
        try:
            target = resolve_path(cmd.path or cmd.old_path or "")
        except PathError as e:
            return str(e)
        if target.area == "self":
            return "self.md is edited with revise_self, not with memory commands"
        if target.area == "policy":
            return None if "policy" in rights else "you do not have the right to edit your memory policy"
        if cmd.command == "create":
            if target.area == "notes":
                return None if "notes" in rights else "you do not have the right to create notes"
            return None if "structure" in rights else "creating index files needs the structure right"
        return None if "structure" in rights else "you do not have the right to restructure your memory"

    # --- write side ----------------------------------------------------------------------------------
    def apply(self, agent_id: str, cmd: MemoryCommand, tick: int, *, purpose: str, rights: list[MaintenanceRight],
              call_id: str | None = None) -> CommandResult:
        """Validate, apply and audit one command. Never raises on agent error: the result carries the message.

        * ``view``: directory listing (two levels, sizes, last-changed tick) or the file with 1-based line
          numbers, honouring ``view_range`` (inclusive, clamped to the file). Viewing never writes.
        * ``create``: refuse if the path exists (by file name, or by the title the name stands for); notes go
          through ``store.remember`` with the title derived from the file name, a stripped and sanitized body,
          tags ``agent`` (plus ``index`` under ``/memories/index/``), channel ``observed`` and auto-linking.
        * ``str_replace``: exactly one occurrence or refuse (listing the line numbers of all matches).
        * ``insert``: ``insert_line`` within ``0..len(lines)`` or refuse.
        * ``delete``: notes are archived in the index, the file removed, the manifest row dropped; the policy,
          ``self.md``, directories and kernel anchors (entity stubs, probes) cannot be deleted.
        * ``rename``: destination must not exist; the note's title follows the new file name and the vault
          picks the canonical file name for it; moving between ``notes/`` and ``index/`` toggles the ``index``
          tag; links other notes hold to the old title are left alone.
        * Limits from ``cfg`` (files, bytes per file, bytes per vault) apply to every write.
        * Every outcome, ok or not, is one ``memory_commands`` row; the caller emits the ``MEMORY_COMMAND``
          event from the returned result.
        """
        refusal = self.allowed(cmd, rights)
        if refusal is not None:
            res = CommandResult(False, refusal, result_hash=self._hash_at(agent_id, cmd.path or cmd.old_path))
        else:
            try:
                res = self._dispatch(agent_id, cmd, tick)
            except (PathError, _Refusal) as e:
                res = CommandResult(False, str(e), result_hash=self._hash_at(agent_id, cmd.path or cmd.old_path))
        self._audit(agent_id, cmd, tick, purpose, res, call_id)
        return res

    def _dispatch(self, agent_id: str, cmd: MemoryCommand, tick: int) -> CommandResult:
        handler = {
            "view": self._view, "create": self._create, "str_replace": self._str_replace,
            "insert": self._insert, "delete": self._delete, "rename": self._rename,
        }[cmd.command]
        return handler(agent_id, cmd, tick)

    # --- the six commands ---------------------------------------------------------------------------
    def _view(self, agent_id: str, cmd: MemoryCommand, tick: int) -> CommandResult:
        target = resolve_path(cmd.path or "")
        if target.is_dir:
            return CommandResult(True, self.listing(agent_id, tick=tick, area=target.area))
        text, note = self._read(agent_id, target)
        lines = _lines(text)
        start, end = 1, len(lines)
        if cmd.view_range is not None:
            if len(cmd.view_range) != 2:
                raise _Refusal("view_range must be [start, end]")
            a, b = int(cmd.view_range[0]), int(cmd.view_range[1])
            if a < 1 or b < a:
                raise _Refusal("view_range must satisfy 1 <= start <= end")
            if a > len(lines):
                raise _Refusal(f"view_range starts after the last line of {target.virtual} ({len(lines)} lines)")
            start, end = a, min(b, len(lines))
        numbered = "\n".join(f"{i:>6}\t{lines[i - 1]}" for i in range(start, end + 1))
        message = f"Here's the content of {target.virtual} with line numbers:" + (f"\n{numbered}" if numbered else "")
        return CommandResult(True, message, result_hash=self._hash_of(agent_id, target, note),
                             note_id=None if note is None else note.note_id, effects={"lines": len(lines)})

    def _create(self, agent_id: str, cmd: MemoryCommand, tick: int) -> CommandResult:
        target = resolve_path(cmd.path or "")
        body = _agent_body(cmd.file_text or "")
        if target.area == "policy":
            if self._policy_text(agent_id) is not None:
                raise _Refusal(f"{POLICY_PATH} already exists; edit it with str_replace or insert")
            self._set_policy(agent_id, body, tick)
            return CommandResult(True, f"File created successfully at: {POLICY_PATH}", bytes_delta=_nbytes(body),
                                 result_hash=content_hash(body), effects={"path": POLICY_PATH})
        if not target.is_note:
            raise _Refusal(f"create needs a .md file path under {NOTES_DIR}/ or {INDEX_DIR}/")
        if not body.strip():
            raise _Refusal("file_text is empty")
        entries = self._entries(agent_id)
        title = clean_title(title_from_name(target.name or ""))
        clash = _clash(entries, target.name or "", title)
        if clash is not None:
            raise _Refusal(_exists_message(target.virtual, clash))
        self._check_limits(entries, new_bytes=_nbytes(body), new_file=True)
        tags = [AGENT_TAG] + ([INDEX_TAG] if target.area == "index" else [])
        # the body passed the byte limit, so a character cap at the same number never cuts it; the Related
        # section auto-linking appends is kernel text and may add a few lines on top
        note = self.store.remember(agent_id, tick, body, title=title, tags=tags, channel="observed", auto_link=True,
                                   max_chars=self.cfg.max_file_bytes)
        actual = virtual_path(note)
        message = f"File created successfully at: {actual}"
        if actual != target.virtual:
            message += f" (requested {target.virtual})"
        return CommandResult(True, message, bytes_delta=_nbytes(note.body), result_hash=self._note_hash(note),
                             note_id=note.note_id, effects={"path": actual, "requested": target.virtual, "title": note.title})

    def _str_replace(self, agent_id: str, cmd: MemoryCommand, tick: int) -> CommandResult:
        target = self._editable(cmd.path, "str_replace")
        old = _clean(cmd.old_str or "")
        new = _clean(cmd.new_str or "")
        if not old:
            raise _Refusal("old_str is empty")
        text, note = self._read(agent_id, target)
        positions = [m.start() for m in re.finditer(f"(?={re.escape(old)})", text)]
        if not positions:
            raise _Refusal(f"old_str was not found in {target.virtual}")
        if len(positions) > 1:
            lines = ", ".join(str(n) for n in _match_lines(text, positions))
            raise _Refusal(f"old_str occurs {len(positions)} times in {target.virtual} (lines {lines}); "
                           "include more context so it matches exactly once")
        new_text = text[: positions[0]] + new + text[positions[0] + len(old):]
        return self._write(agent_id, target, note, text, new_text, tick, SUCCESS_EDITED)

    def _insert(self, agent_id: str, cmd: MemoryCommand, tick: int) -> CommandResult:
        target = self._editable(cmd.path, "insert")
        text, note = self._read(agent_id, target)
        lines = _lines(text)
        at = int(cmd.insert_line or 0)
        if at > len(lines):
            raise _Refusal(f"insert_line must be between 0 and {len(lines)} for {target.virtual}")
        inserted = _clean(cmd.insert_text or "")
        if inserted.endswith("\n"):
            inserted = inserted[:-1]
        new_text = "\n".join(lines[:at] + inserted.split("\n") + lines[at:])
        return self._write(agent_id, target, note, text, new_text, tick, f"The file {target.virtual} has been edited.")

    def _delete(self, agent_id: str, cmd: MemoryCommand, tick: int) -> CommandResult:
        target = resolve_path(cmd.path or "")
        if target.area == "root":
            raise _Refusal("the root cannot be deleted")
        if target.is_dir:
            raise _Refusal(f"{target.virtual} is a directory and cannot be deleted")
        if target.area == "policy":
            raise _Refusal("the memory policy cannot be deleted")
        if target.area == "self":
            raise _Refusal("self.md cannot be deleted")
        note = self._note_at(agent_id, target)
        self._guard_anchor(note, target, "deleted")
        old_bytes = _nbytes(note.body)
        self.store.delete_note(note.note_id)
        return CommandResult(True, f"Successfully deleted {target.virtual}", bytes_delta=-old_bytes, note_id=note.note_id,
                             effects={"path": target.virtual, "title": note.title})

    def _rename(self, agent_id: str, cmd: MemoryCommand, tick: int) -> CommandResult:
        old = resolve_path(cmd.old_path or "")
        if old.area == "policy":
            raise _Refusal("the memory policy cannot be renamed")
        if not old.is_note:
            raise _Refusal(f"rename needs a note under {NOTES_DIR}/ or {INDEX_DIR}/ as old_path")
        new = resolve_path(cmd.new_path or "")
        if not new.is_note:
            raise _Refusal(f"new_path must be a .md file under {NOTES_DIR}/ or {INDEX_DIR}/")
        if old.virtual == new.virtual:
            raise _Refusal("old_path and new_path are the same")
        note = self._note_at(agent_id, old)
        self._guard_anchor(note, old, "renamed")
        title = clean_title(title_from_name(new.name or ""))
        clash = _clash([e for e in self._entries(agent_id) if e.note_id != note.note_id], new.name or "", title)
        if clash is not None:
            raise _Refusal(_exists_message(new.virtual, clash))
        tags = [t for t in note.tags if t != INDEX_TAG] + ([INDEX_TAG] if new.area == "index" else [])
        renamed = self.store.rename_note(note.note_id, title, tick, tags=tags)
        actual = virtual_path(renamed)
        message = f"Successfully renamed {old.virtual} to {actual}"
        if actual != new.virtual:
            message += f" (requested {new.virtual})"
        return CommandResult(True, message, result_hash=self._note_hash(renamed), note_id=renamed.note_id,
                             effects={"old": old.virtual, "path": actual, "requested": new.virtual, "title": renamed.title})

    # --- shared pieces ------------------------------------------------------------------------------
    def _editable(self, raw: str | None, command: str) -> VaultPath:
        target = resolve_path(raw or "")
        if target.is_dir:
            raise _Refusal(f"{command} needs a file path, not a directory")
        if target.area == "self":
            raise _Refusal("self.md is edited with revise_self, not with memory commands")
        return target

    def _read(self, agent_id: str, target: VaultPath) -> tuple[str, Note | None]:
        """The agent-visible text behind a file path and, for a note, the note; refuses when it does not exist."""
        if target.is_note:
            note = self._note_at(agent_id, target)
            return note.body, note
        if target.area == "policy":
            text = self._policy_text(agent_id)
            if text is None:
                raise _Refusal(f"{POLICY_PATH} does not exist")
            return text, None
        if target.area == "self":
            text = self._self_text(agent_id)
            if text is None:
                raise _Refusal(f"{SELF_PATH} does not exist")
            return text, None
        raise _Refusal(f"{target.virtual} is a directory")

    def _write(self, agent_id: str, target: VaultPath, note: Note | None, old_text: str, new_text: str, tick: int,
               message: str) -> CommandResult:
        """Store an edited body (note or policy) after the limits, returning the success result."""
        if target.area == "policy":
            self._set_policy(agent_id, new_text, tick)
            return CommandResult(True, message, bytes_delta=_nbytes(new_text) - _nbytes(old_text),
                                 result_hash=content_hash(new_text), effects={"path": POLICY_PATH})
        if note is None:
            raise _Refusal(f"{target.virtual} does not exist")
        self._check_limits(self._entries(agent_id), new_bytes=_nbytes(new_text), old_bytes=_nbytes(old_text))
        updated = self.store.update_body(note.note_id, new_text, tick)
        return CommandResult(True, message, bytes_delta=_nbytes(updated.body) - _nbytes(old_text),
                             result_hash=self._note_hash(updated), note_id=updated.note_id,
                             effects={"path": virtual_path(updated), "title": updated.title})

    def _set_policy(self, agent_id: str, text: str, tick: int) -> None:
        if self.policy is None:
            raise _Refusal("there is no memory policy in this world")
        pcfg = self.policy.cfg
        if len(text) > int(pcfg.max_chars):
            raise _Refusal(f"the policy would be {len(text)} characters; the limit is {pcfg.max_chars}")
        used = int(self.policy.edits_today(agent_id, tick))
        if used >= int(pcfg.max_edits_per_day):
            raise _Refusal(f"you have used today's {pcfg.max_edits_per_day} policy edits")
        try:
            self.policy.set(agent_id, text, tick, source="self_edit")
        except ValueError as e:
            raise _Refusal(str(e)) from e

    def _check_limits(self, entries: list[_Entry], *, new_bytes: int, old_bytes: int = 0, new_file: bool = False) -> None:
        if new_file and len(entries) >= self.cfg.max_files:
            raise _Refusal(f"your vault already holds {self.cfg.max_files} files; delete one first")
        if new_bytes > self.cfg.max_file_bytes:
            raise _Refusal(f"the file would be {new_bytes} bytes; the limit is {self.cfg.max_file_bytes}")
        total = sum(e.size for e in entries) - old_bytes + new_bytes
        if total > self.cfg.max_vault_bytes:
            raise _Refusal(f"your vault would be {total} bytes; the limit is {self.cfg.max_vault_bytes}")

    def _guard_anchor(self, note: Note, target: VaultPath, verb: str) -> None:
        for tag, what in _PROTECTED:
            if tag in note.tags:
                raise _Refusal(f"{target.virtual} is {what} and cannot be {verb}")

    def _rows(self, agent_id: str) -> list[Any]:
        """Index rows of the agent's live notes joined with their manifest tick (no file reads)."""
        return self.db.fetchall(
            "SELECT n.note_id, n.path, n.title, n.tags, n.created_tick, n.channel, m.tick AS written_tick "
            "FROM notes n LEFT JOIN note_manifest m ON m.path = n.path WHERE n.agent_id=? AND n.archived=0 ORDER BY n.path",
            (agent_id,),
        )

    def _entries(self, agent_id: str) -> list[_Entry]:
        """Every live note of the agent as the listing and the limits see it (body sizes read from disk), by name."""
        out: list[_Entry] = []
        for r in self._rows(agent_id):
            tags = tuple(_tags_of(r["tags"]))
            written = r["written_tick"]
            out.append(_Entry(
                note_id=str(r["note_id"]), area=_area_of(tags), name=_file_name(str(r["path"])),
                title=str(r["title"]), size=_body_bytes(Path(str(r["path"]))),
                tick=int(r["created_tick"]) if written is None else int(written), channel=str(r["channel"]), tags=tags,
            ))
        return sorted(out, key=lambda e: (e.area, e.name))

    def _note_at(self, agent_id: str, target: VaultPath) -> Note:
        """The live note a virtual path names (file name and area must both match) or a refusal."""
        for r in self._rows(agent_id):
            if _file_name(str(r["path"])) == target.name and _area_of(_tags_of(r["tags"])) == target.area:
                note = self.store.get_note(str(r["note_id"]))
                if note is not None and not note.archived:
                    return note
        raise _Refusal(f"{target.virtual} does not exist")

    def _policy_text(self, agent_id: str) -> str | None:
        return None if self.policy is None else self.policy.text(agent_id)

    def _self_text(self, agent_id: str) -> str | None:
        text = self.store.get_self(agent_id)
        if text or self.store.vault_for(agent_id).self_path.is_file():
            return text
        return None

    def _hash_of(self, agent_id: str, target: VaultPath, note: Note | None) -> str | None:
        """Content hash of the file a path names right now, None when no file exists (directories, missing)."""
        if target.is_note:
            return None if note is None else self._note_hash(note)
        if target.area == "policy":
            text = self._policy_text(agent_id)
            return None if text is None else content_hash(text)
        if target.area == "self":
            path = self.store.vault_for(agent_id).self_path
            return content_hash(path.read_text(encoding="utf-8")) if path.is_file() else None
        return None

    def _hash_at(self, agent_id: str, raw: str | None) -> str | None:
        """:meth:`_hash_of` for a raw agent path that may be malformed or name nothing (never raises)."""
        try:
            target = resolve_path(raw or "")
            note = self._note_at(agent_id, target) if target.is_note else None
            return self._hash_of(agent_id, target, note)
        except (PathError, _Refusal, OSError):
            return None

    @staticmethod
    def _note_hash(note: Note) -> str | None:
        path = Path(str(note.path))
        try:
            return content_hash(path.read_text(encoding="utf-8"))
        except OSError:
            return None

    # --- audit and manifest -------------------------------------------------------------------------
    def _audit(self, agent_id: str, cmd: MemoryCommand, tick: int, purpose: str, res: CommandResult, call_id: str | None) -> str:
        cmd_id = self.ids.new("mc")
        self.db.execute(
            "INSERT INTO memory_commands(cmd_id, tick, agent_id, purpose, command, args, ok, message, result_hash, bytes_delta, call_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cmd_id, int(tick), agent_id, purpose, cmd.command, json.dumps(cmd.args(), sort_keys=True), int(res.ok),
             res.message[:400], res.result_hash, int(res.bytes_delta), call_id),
        )
        return cmd_id

    def record_manifest(self, agent_id: str, path: str, text: str, tick: int, *, written_by: str = "kernel",
                        note_id: str | None = None) -> str:
        """Upsert the manifest row for a file the kernel just wrote (the store's helper); returns the content hash."""
        return self.store.record_manifest(path, text, tick, agent_id=agent_id, note_id=note_id, written_by=written_by)


# --- helpers -----------------------------------------------------------------------------------------
def _file_name(path: str | None) -> str:
    return (path or "").replace("\\", "/").rsplit("/", 1)[-1]


def _clean(text: str) -> str:
    """Agent text as stored: control characters and angle brackets neutralised, line endings normalised."""
    return sanitize_text(text).replace("\r\n", "\n").replace("\r", "\n")


def _agent_body(text: str) -> str:
    """A whole file an agent wrote: cleaned, any leading frontmatter block dropped (§5.9), trailing newlines trimmed."""
    _, body = split_frontmatter(_clean(text))
    return body.rstrip("\n")


def _lines(text: str) -> list[str]:
    return text.split("\n") if text else []


def _nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _body_bytes(path: Path) -> int:
    try:
        _, rest = split_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return 0
    return _nbytes(rest[:-1] if rest.endswith("\n") else rest)


def _area_of(tags: Sequence[str]) -> str:
    return "index" if INDEX_TAG in tags else "notes"


def _tags_of(raw: object) -> list[str]:
    try:
        tags = json.loads(str(raw) if raw is not None else "[]")
    except (TypeError, ValueError):
        return []
    return [str(t) for t in tags] if isinstance(tags, list) else []


def _tick_of(row: Any) -> str:
    return "-" if row is None or row["t"] is None else str(int(row["t"]))


def _match_lines(text: str, positions: list[int]) -> list[int]:
    out: list[int] = []
    for pos in positions:
        line = text.count("\n", 0, pos) + 1
        if line not in out:
            out.append(line)
    return out


def _clash(entries: list[_Entry], name: str, title: str) -> _Entry | None:
    """The live note a requested file name collides with: same file name in either area, or the same title."""
    key = normalise_title(title)
    for e in entries:
        if e.name == name or normalise_title(e.title) == key:
            return e
    return None


def _exists_message(requested: str, clash: _Entry) -> str:
    if clash.virtual == requested:
        return f"{requested} already exists"
    return f"{requested} already exists as {clash.virtual} (titled '{clash.title}')"
