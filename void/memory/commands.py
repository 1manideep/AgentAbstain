"""The six memory commands an agent may issue over its own vault (MEMORY_EVOLUTION §5.4) `[scaffold]`.

The command surface deliberately matches Anthropic's memory tool (``view``, ``create``, ``str_replace``,
``insert``, ``delete``, ``rename``) so a vault or a policy developed here is usable behind that product
surface and results are comparable to the default baseline anyone can run.

Design rules this module must keep (§5.4, §5.9):

* **Paths are virtual.** Agents see ``/memories/notes/<file>.md``, ``/memories/index/<file>.md``,
  ``/memories/memory_policy.md`` and ``/memories/self.md``. :func:`resolve_path` canonicalizes and
  confines every path to the agent's own vault; ``..``, URL-encoded traversal, absolute host paths,
  symlinks and non-``.md`` names are refused before anything touches the disk.
* **Writes go through the store.** A note created or edited by a command is written with
  :class:`~void.memory.store.MemoryStore` so the database row stays authoritative for provenance,
  importance, created tick and kernel tags. Agent text never carries frontmatter: it is stripped.
* **Rights gate commands.** ``notes`` allows ``create`` under ``/memories/notes/``; ``policy`` allows
  edits of the policy file (delegated to :mod:`void.memory.policy`); ``structure`` allows
  ``str_replace`` / ``insert`` / ``delete`` / ``rename`` on notes and the index directory. ``view`` is
  always allowed. ``self.md`` is read-only here (``revise_self`` stays the way to change it).
* **Every command is audited** in ``memory_commands`` with its arguments, outcome, result hash and byte
  delta, and every file the kernel writes is recorded in ``note_manifest`` with a content hash.
* **Limits** come from :class:`~void.config.MaintenanceConfig` (files per vault, bytes per file and per
  vault, commands per step) and are enforced here, not by callers.

Implementation status: the data model and the path rules are final; :meth:`MemoryCommands.apply` is the
work package WP3 and raises :class:`NotImplementedError` until it lands.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import unquote

from pydantic import BaseModel, Field, model_validator

from void.config import MaintenanceConfig, MaintenanceRight
from void.db import Database
from void.ids import IdFactory
from void.memory.store import MemoryStore

__all__ = [
    "ROOT", "NOTES_DIR", "INDEX_DIR", "POLICY_PATH", "SELF_PATH", "CommandName", "MemoryCommand", "CommandResult",
    "VaultPath", "PathError", "resolve_path", "content_hash", "MemoryCommands",
]

ROOT = "/memories"
NOTES_DIR = f"{ROOT}/notes"
INDEX_DIR = f"{ROOT}/index"
POLICY_PATH = f"{ROOT}/memory_policy.md"
SELF_PATH = f"{ROOT}/self.md"

CommandName = Literal["view", "create", "str_replace", "insert", "delete", "rename"]
_RIGHT_FOR: dict[str, MaintenanceRight | None] = {
    "view": None, "create": "notes", "str_replace": "structure", "insert": "structure", "delete": "structure", "rename": "structure",
}
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.'-]{0,78}\.md$")
MAX_FILE_TEXT = 8000
MAX_PATH_CHARS = 200


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


@dataclass(frozen=True)
class VaultPath:
    """A validated virtual path: which area of the vault it names, and the bare file name (None for a directory)."""

    virtual: str                      # canonical form, e.g. ``/memories/notes/thin-node-3f2a1b.md``
    area: Literal["root", "notes", "index", "policy", "self"]
    name: str | None                  # file name within the area, None for the area directory itself

    @property
    def is_dir(self) -> bool:
        return self.name is None


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


def content_hash(text: str) -> str:
    return "blake2b:" + hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


class MemoryCommands:
    """Apply validated commands to one agent's vault through the store, audit each one, keep the manifest.

    ``rights`` is the set of :data:`~void.config.MaintenanceRight` values the caller holds for this batch;
    ``purpose`` labels the audit row (``maintain`` for the nightly step, ``tick`` for per-tick ops routed
    here later, ``recall`` for the C3 tool, ``kernel`` for kernel-initiated writes).
    """

    def __init__(self, db: Database, store: MemoryStore, cfg: MaintenanceConfig, ids: IdFactory) -> None:
        self.db = db
        self.store = store
        self.cfg = cfg
        self.ids = ids

    # --- read side ---------------------------------------------------------------------------------
    def listing(self, agent_id: str, *, tick: int | None = None) -> str:
        """The two-level directory listing the maintenance prompt shows (paths, sizes, last-changed tick).

        WP3 implements this from the ``notes`` index plus the policy and self files; a scaffold-stage
        caller gets a listing built from the store so prompts can already be rendered.
        """
        lines = [f"{ROOT}/", "  self.md", "  memory_policy.md", "  notes/"]
        for n in self.store.list_notes(agent_id):
            size = len(n.body.encode("utf-8"))
            lines.append(f"    {_file_name(n.path)}  ({size} bytes, tick {n.created_tick}, {n.provenance.channel})")
        lines.append("  index/")
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

        WP3 implements the six commands here. The contract:

        * ``view``: directory listing (two levels, sizes) or the file with line numbers, honouring ``view_range``.
        * ``create``: refuse if the path exists; notes go through ``store.remember`` with the agent's title
          derived from the file name and a stripped body; index files are notes tagged ``index``.
        * ``str_replace``: exactly one occurrence or refuse (listing the line numbers of all matches).
        * ``insert``: ``insert_line`` within ``0..len(lines)`` or refuse.
        * ``delete``: notes are archived in the index, the file removed, the manifest row updated; the policy
          file and the root cannot be deleted.
        * ``rename``: destination must not exist; the note's title follows the new file name.
        * Limits from ``cfg`` (files, bytes per file, bytes per vault) apply to every write.
        * Every outcome, ok or not, is one ``memory_commands`` row and one ``MEMORY_COMMAND`` event emitted
          by the caller from the returned result.
        """
        refusal = self.allowed(cmd, rights)
        if refusal is not None:
            res = CommandResult(False, refusal)
            self._audit(agent_id, cmd, tick, purpose, res, call_id)
            return res
        raise NotImplementedError("MemoryCommands.apply is work package WP3 (MEMORY_EVOLUTION §10.1)")

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
        """Upsert the manifest row for a file the kernel just wrote; returns the content hash."""
        h = content_hash(text)
        self.db.execute(
            "INSERT INTO note_manifest(path, note_id, agent_id, content_hash, written_by, tick) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET note_id=excluded.note_id, content_hash=excluded.content_hash, "
            "written_by=excluded.written_by, tick=excluded.tick",
            (str(path), note_id, agent_id, h, written_by, int(tick)),
        )
        return h


def _file_name(path: str | None) -> str:
    return (path or "").replace("\\", "/").rsplit("/", 1)[-1]
