"""``void mcp``: the six memory commands served over the Model Context Protocol (MEMORY_EVOLUTION §5.11, WP12).

A thin stdio server over one vault directory, so any harness or model can keep a memory with the framework's
command semantics without the simulator::

    void mcp --vault DIR [--max-files N] [--max-file-bytes N] [--max-vault-bytes N]

**Transport.** JSON-RPC 2.0, one message per line on stdin and stdout (UTF-8, ASCII-escaped, no embedded
newlines) and nothing else on stdout; diagnostics go to stderr. The server answers ``initialize``, ``ping``,
``tools/list`` and ``tools/call`` and accepts the ``notifications/initialized`` and ``notifications/cancelled``
notifications; any other method is ``-32601``. A request other than ``initialize`` or ``ping`` before
``initialize`` gets ``-32002``. A batch (a JSON array) is answered with an array. There is no network listener
and no dependency beyond the standard library and this package; the protocol is implemented here.

**Tools.** ``view``, ``create``, ``str_replace``, ``insert``, ``delete`` and ``rename``, each with a JSON input
schema mirroring :class:`~void.memory.commands.MemoryCommand`. A call is validated into a ``MemoryCommand`` and
applied by :class:`~void.memory.commands.MemoryCommands`, so the success strings, the refusals, the path
resolution (virtual ``/memories/...`` paths; ``..``, encoded traversal, host paths, hidden and non-``.md`` names
refused before anything touches the disk) and the limits are exactly the simulator's. A refused command is a
tool result with ``isError`` true carrying the command's own message, so a model can correct itself; only
malformed protocol traffic and unknown tool names are JSON-RPC errors.

**The vault.** DIR is one agent's vault as the simulator lays it out (``notes/*.md``, ``self.md``,
``memory_policy.md``), created when missing; its directory name is the agent id. The directory is the source of
truth: the store's index lives in an in-memory database, built from the files at start and rebuilt whenever
something else changed the directory between two calls (a stat fingerprint decides). Each file's frontmatter is
believed as found (the manifest is seeded from the files), so editing a note through the server never rewrites
its provenance as operator-made. The policy file is loaded into the policy store the same way and edited through
it (``self_edit`` versions under the default character limit and daily edit budget). ``self.md`` is read-only,
as in the simulator. The audit rows the command layer writes live as long as the process.

**Clock.** Every ``tools/call`` that reaches the vault is one tick, counting on from the newest tick the vault
already records, so created ticks keep increasing across sessions and a day of the policy edit budget is
``TICKS_PER_DAY`` calls. Nothing reads the wall clock or draws randomness; note ids come from a counter keyed on
the vault name that skips any id the vault already holds, so the same calls on the same vault write the same
files.

**Confinement.** On top of the command layer's path resolution the server never follows a link. A symbolic link,
or a hard-linked file (a second name for a file that may live outside the vault), is left out of the index; a
command naming one, or naming anything under a linked ``notes/`` directory, ``self.md`` or the policy file, is
refused before it touches the disk; and a write whose file name would land on one is refused as well.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import traceback
from collections.abc import Callable, Iterator
from importlib import metadata
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import ValidationError

from void.config import MaintenanceConfig, MaintenanceRight, MemoryConfig, MemoryPolicyConfig
from void.db import Database
from void.ids import IdFactory
from void.memory.commands import (
    INDEX_DIR,
    MAX_FILE_TEXT,
    MAX_PATH_CHARS,
    NOTES_DIR,
    POLICY_PATH,
    ROOT,
    SELF_PATH,
    MemoryCommand,
    MemoryCommands,
    PathError,
    resolve_path,
    title_from_name,
)
from void.memory.notes import Note, parse_note, split_frontmatter
from void.memory.policy import FILE_NAME as POLICY_FILE
from void.memory.policy import SOURCES, PolicySource, PolicyStore, PolicyVersion, clean_policy_text
from void.memory.store import MemoryStore, clean_title
from void.memory.vault import Vault

__all__ = [
    "PROTOCOL_VERSIONS", "SERVER_NAME", "TICKS_PER_DAY", "MAX_MESSAGE_BYTES", "TOOLS", "TOOL_NAMES",
    "PARSE_ERROR", "INVALID_REQUEST", "METHOD_NOT_FOUND", "INVALID_PARAMS", "INTERNAL_ERROR", "NOT_INITIALIZED",
    "VaultError", "VaultSession", "McpServer", "serve", "build_parser", "cli_main",
]

# Newest first: a client asking for one of these gets it echoed back, any other gets the newest.
PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "void-memory"
TICKS_PER_DAY = 24                 # calls per "day" of the policy edit budget
MAX_MESSAGE_BYTES = 1 << 20        # one line of input; a longer one is drained and answered with -32600
RIGHTS: list[MaintenanceRight] = ["notes", "policy", "structure"]   # the vault's owner holds every right
AUDIT_PURPOSE = "tick"             # each call is one tick of the server's clock

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
NOT_INITIALIZED = -32002           # implementation-defined: a request before initialize

_NOTES_SUBDIR = "notes"
_SELF_FILE = "self.md"


# --- the tools ------------------------------------------------------------------------------------------------
def _path(description: str) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": MAX_PATH_CHARS, "description": description}


def _text(description: str) -> dict[str, Any]:
    return {"type": "string", "maxLength": MAX_FILE_TEXT, "description": description}


def _tool(name: str, title: str, description: str, properties: dict[str, Any], required: list[str], *,
          read_only: bool = False, destructive: bool = False, idempotent: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        "annotations": {"title": title, "readOnlyHint": read_only, "destructiveHint": destructive,
                        "idempotentHint": idempotent, "openWorldHint": False},
    }


_FILE_PATH = f"a file under {ROOT}, e.g. {NOTES_DIR}/node-n3.md, {INDEX_DIR}/places.md or {POLICY_PATH}"
_NOTE_PATH = f"a note: {NOTES_DIR}/<name>.md or {INDEX_DIR}/<name>.md"

TOOLS: tuple[dict[str, Any], ...] = (
    _tool(
        "view", "View memory",
        f"List {ROOT} (or {NOTES_DIR}, {INDEX_DIR}) with each file's size in bytes and the tick of its last write, "
        "or show a file with 1-based line numbers. view_range [start, end] limits the lines shown.",
        {"path": _path(f"{ROOT}, {NOTES_DIR}, {INDEX_DIR} or {_FILE_PATH}"),
         "view_range": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 2, "maxItems": 2,
                        "description": "[start, end] of the lines to show, 1-based and inclusive"}},
        ["path"], read_only=True, idempotent=True,
    ),
    _tool(
        "create", "Create a memory file",
        f"Create a note under {NOTES_DIR}/ or {INDEX_DIR}/ (the file name becomes its title), or {POLICY_PATH} when "
        "there is none. Refused when the file, or a note with the same title, exists. The vault picks the final "
        "file name and the result names it.",
        {"path": _path(f"{_NOTE_PATH}, or {POLICY_PATH}"),
         "file_text": _text("the whole file; a leading frontmatter block is dropped")},
        ["path", "file_text"],
    ),
    _tool(
        "str_replace", "Replace text in a memory file",
        "Replace the one exact occurrence of old_str in a file with new_str (empty or absent deletes it). Refused "
        "when old_str occurs zero times or more than once.",
        {"path": _path(_FILE_PATH), "old_str": _text("the exact text to replace"),
         "new_str": _text("the replacement text (default empty)")},
        ["path", "old_str"], destructive=True,
    ),
    _tool(
        "insert", "Insert lines into a memory file",
        "Insert insert_text after line insert_line of a file (0 inserts before the first line).",
        {"path": _path(_FILE_PATH),
         "insert_line": {"type": "integer", "minimum": 0, "maximum": 10_000,
                         "description": "the line to insert after, 0 for the top"},
         "insert_text": _text("the text to insert")},
        ["path", "insert_line", "insert_text"],
    ),
    _tool(
        "delete", "Delete a memory note",
        f"Delete a note. {POLICY_PATH}, {SELF_PATH} and the directories cannot be deleted.",
        {"path": _path(_NOTE_PATH)},
        ["path"], destructive=True,
    ),
    _tool(
        "rename", "Rename a memory note",
        f"Rename a note, or move it between {NOTES_DIR}/ and {INDEX_DIR}/. The destination must not exist; the "
        "note's title follows the new name.",
        {"old_path": _path(f"the note to rename: {_NOTE_PATH}"), "new_path": _path(f"its new path: {_NOTE_PATH}")},
        ["old_path", "new_path"],
    ),
)
TOOL_NAMES: tuple[str, ...] = tuple(t["name"] for t in TOOLS)
_FIELDS: dict[str, tuple[str, ...]] = {t["name"]: tuple(t["inputSchema"]["properties"]) for t in TOOLS}


# --- confinement ------------------------------------------------------------------------------------------------
class VaultError(ValueError):
    """The vault directory cannot be served (not a directory, the filesystem root, a bad limit)."""


def _problem(path: Path, *, directory: bool = False) -> str | None:
    """Why the server will not read or write ``path``, or None when it may (nothing there counts as fine).

    A symbolic link is never followed, wherever it points; a regular file with a second name (a hard link) may
    live outside the vault, so it is treated the same; anything else that is not a regular file (or, with
    ``directory``, not a directory) is left alone too.
    """
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        return f"unreadable ({e.strerror})"
    if stat.S_ISLNK(st.st_mode):
        return "a symbolic link"
    if directory:
        return None if stat.S_ISDIR(st.st_mode) else "not a directory"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if st.st_nlink > 1:
        return "a hard link (a file with more than one name)"
    return None


def _stat_key(path: Path) -> tuple[int, ...] | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_mode, st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns, st.st_nlink)


def _refusal(shown: str, problem: str) -> str:
    return f"{shown} is {problem} on disk; the server only reads and writes regular files inside the vault"


class _ConfinedVault(Vault):
    """A :class:`Vault` that never follows a link: linked entries are not listed, read or written through."""

    def iter_note_files(self) -> Iterator[tuple[Path, str]]:
        """The regular note files, name-sorted; a copy carrying an id an earlier file already has is skipped
        (the index keys notes by id) and so is a file that is not UTF-8 text."""
        if _problem(self.notes_dir, directory=True) is not None or not self.notes_dir.is_dir():
            return
        seen: set[str] = set()
        for p in sorted(self.notes_dir.glob("*.md")):
            if _problem(p) is not None or not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            note_id = parse_note(text, agent_id=self.agent_id, path=str(p)).note_id
            if note_id in seen:
                continue
            seen.add(note_id)
            yield p, text

    def write_note(self, note: Note) -> Path:
        """Refuse (``PathError``, which the command layer turns into a refusal) when the file the vault would
        write, or any of its numbered alternatives, is a link; otherwise write as :class:`Vault` does."""
        problem = _problem(self.notes_dir, directory=True)
        if problem is not None:
            raise PathError(_refusal(f"the directory behind {NOTES_DIR}/ and {INDEX_DIR}/", problem))
        if self.notes_dir.is_dir():
            stem = self.note_path(note).stem
            current = Path(note.path).name if note.path else None
            for entry in self.notes_dir.iterdir():
                name = entry.name
                numbered = name.startswith(stem + "-") and name.endswith(".md") and name[len(stem) + 1:-3].isdigit()
                if name == current or name == f"{stem}.md" or numbered:
                    problem = _problem(entry)
                    if problem is not None:
                        raise PathError(_refusal(f"{NOTES_DIR}/{name}", problem))
        return super().write_note(note)

    def read_self(self) -> tuple[str, int]:
        if _problem(self.self_path) is not None:
            return "", 0
        try:
            return super().read_self()
        except (OSError, UnicodeDecodeError):
            return "", 0


class _ConfinedStore(MemoryStore):
    def vault_for(self, agent_id: str) -> Vault:
        return _ConfinedVault(self.vault_root, agent_id)


class _ConfinedPolicy(PolicyStore):
    def set(self, agent_id: str, text: str, tick: int, *, source: PolicySource,
            source_agent_id: str | None = None) -> PolicyVersion:
        problem = _problem(self.path_for(agent_id))
        if problem is not None:  # the command layer turns a ValueError from the policy store into a refusal
            raise ValueError(_refusal(POLICY_PATH, problem))
        return super().set(agent_id, text, tick, source=source, source_agent_id=source_agent_id)


class _VaultIds(IdFactory):
    """Ids from a counter keyed on the vault name; a note id the vault already holds is skipped."""

    def __init__(self, run_id: str, taken: Callable[[str], bool]) -> None:
        super().__init__(run_id)
        self._taken = taken

    def new(self, prefix: str) -> str:
        while True:
            out = super().new(prefix)
            if prefix != "note" or not self._taken(out):
                return out


# --- the vault behind the tools ---------------------------------------------------------------------------------
_UNSEEN = object()


class VaultSession:
    """One vault directory behind the six commands: the in-memory index, the clock and the confinement checks."""

    def __init__(self, vault: str | Path, *, max_files: int | None = None, max_file_bytes: int | None = None,
                 max_vault_bytes: int | None = None) -> None:
        limits = {k: v for k, v in (("max_files", max_files), ("max_file_bytes", max_file_bytes),
                                    ("max_vault_bytes", max_vault_bytes)) if v is not None}
        try:
            self.cfg = MaintenanceConfig(**limits)
        except ValidationError as e:
            raise VaultError(_validation_text(e).replace("memory.maintenance.", "")) from None
        path = Path(vault).expanduser()
        if path.exists() and not path.is_dir():
            raise VaultError(f"{path} is not a directory")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise VaultError(f"cannot create {path}: {e.strerror}") from None
        self.dir = path.resolve()
        if self.dir.parent == self.dir:
            raise VaultError("the filesystem root cannot be a vault")
        self.agent_id = self.dir.name
        self.db = Database(":memory:")
        self.ids = _VaultIds(f"mcp:{self.agent_id}", self._note_id_taken)
        self.store = _ConfinedStore(self.db, MemoryConfig(), self.ids, self.dir.parent)
        self.policy = _ConfinedPolicy(self.db, MemoryPolicyConfig(enabled=True), self.dir.parent,
                                      ticks_per_day=TICKS_PER_DAY)
        self.commands = MemoryCommands(self.db, self.store, self.cfg, self.ids, policy=self.policy)
        self._seen: dict[str, object] = {}
        self.sync()
        self.tick = self._newest_tick()

    # --- tool calls -------------------------------------------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Apply one tool call; ``(ok, text)``, the text being the command's success string or its refusal."""
        fields = _FIELDS[name]  # the server answers an unknown tool name before calling this
        unknown = sorted(set(arguments) - set(fields))
        if unknown:
            return False, f"{name} does not take {', '.join(unknown)} (it takes {', '.join(fields)})"
        if not all(_encodable(v) for v in arguments.values()):
            return False, "arguments must be valid Unicode text"
        try:
            cmd = MemoryCommand(command=name, **arguments)  # type: ignore[arg-type]
        except ValidationError as e:
            return False, f"invalid {name} arguments: {_validation_text(e)}"
        if _problem(self.dir, directory=True) is not None or not self.dir.is_dir():
            return False, "the vault directory is gone or was replaced; restart the server"
        self.sync()
        for raw in (cmd.path, cmd.old_path, cmd.new_path):
            refusal = self._link_refusal(raw)
            if refusal is not None:
                return False, refusal
        self.tick += 1
        try:
            res = self.commands.apply(self.agent_id, cmd, self.tick, purpose=AUDIT_PURPOSE, rights=RIGHTS)
        except BaseException:
            self._seen = {}  # the index may now disagree with the files: rebuild it before the next call
            raise
        self._seen = self._fingerprint()
        return res.ok, res.message

    def instructions(self) -> str:
        """What ``initialize`` tells the client about this vault."""
        c = self.cfg
        return (
            "A memory vault behind six commands: view, create, str_replace, insert, delete and rename. Paths are "
            f"virtual and start with {ROOT}: notes in {NOTES_DIR}/<name>.md, index notes in {INDEX_DIR}/<name>.md, "
            f"your memory practices in {POLICY_PATH}, and {SELF_PATH} (read-only). Start with view {ROOT}. "
            f"Limits: {c.max_files} files, {c.max_file_bytes} bytes per file, {c.max_vault_bytes} bytes in all."
        )

    # --- keeping the index in step with the directory ---------------------------------------------------------
    def sync(self) -> bool:
        """Rebuild whatever changed on disk since the last call (notes, policy); True when anything was rebuilt.
        ``self.md`` needs nothing: it is read from its file whenever it is shown."""
        seen = self._fingerprint()
        changed = False
        if seen["notes"] != self._seen.get("notes", _UNSEEN):
            self._index_notes()
            changed = True
        if seen["policy"] != self._seen.get("policy", _UNSEEN):
            self._load_policy()
            changed = True
        self._seen = seen
        return changed

    def _fingerprint(self) -> dict[str, object]:
        notes = self.dir / _NOTES_SUBDIR
        entries: list[tuple[str, tuple[int, ...] | None]] = []
        if _problem(notes, directory=True) is None and notes.is_dir():
            try:
                with os.scandir(notes) as it:
                    entries = sorted((e.name, _stat_key(Path(e.path))) for e in it)
            except OSError:
                entries = []
        return {"notes": (_stat_key(notes), tuple(entries)), "policy": _stat_key(self.dir / POLICY_FILE)}

    def _index_notes(self) -> None:
        """Index every regular note file, believing its frontmatter: the manifest is seeded with each file as it
        is, so the store's reindex keeps the file's provenance instead of marking it operator-edited. A file
        without a frontmatter title (a markdown file dropped in by hand) is titled after its file name, as a note
        created at that path would be."""
        vault = self.store.vault_for(self.agent_id)
        untitled: list[tuple[str, str]] = []
        with self.db.tx():
            self.db.execute("DELETE FROM note_manifest WHERE agent_id=?", (self.agent_id,))
            for path, text in vault.iter_note_files():
                note = parse_note(text, agent_id=self.agent_id, path=str(path))
                self.store.record_manifest(path, text, note.created_tick, agent_id=self.agent_id, note_id=note.note_id,
                                           written_by="operator")
                title = split_frontmatter(text)[0].get("title")
                if not (isinstance(title, str) and title.strip()):
                    untitled.append((note.note_id, clean_title(title_from_name(path.name))))
            self.store.reindex(self.agent_id)
            for note_id, title in untitled:
                self.db.execute("UPDATE notes SET title=? WHERE note_id=?", (title, note_id))

    def _load_policy(self) -> None:
        """The policy file as the current version (its header's version, tick and source when they parse)."""
        path = self.dir / POLICY_FILE
        with self.db.tx():
            self.db.execute("DELETE FROM policy_versions WHERE agent_id=?", (self.agent_id,))
            if _problem(path) is not None or not path.is_file():
                return
            try:
                header, text = PolicyStore.parse_file(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                return
            source = header.get("source")
            self.db.execute(
                "INSERT INTO policy_versions(agent_id, version, tick, text, parent_version, source, source_agent_id, "
                "similarity_to_parent) VALUES(?,?,?,?,NULL,?,NULL,NULL)",
                (self.agent_id, max(1, _as_int(header.get("version"), 1)), max(0, _as_int(header.get("tick"), 0)),
                 clean_policy_text(text), source if source in SOURCES else "seed"),
            )

    def _newest_tick(self) -> int:
        """The newest tick the vault records (notes, the policy, ``self.md``); the clock counts on from it."""
        ticks = [0]
        for sql in ("SELECT MAX(created_tick) AS t FROM notes WHERE agent_id=?",
                    "SELECT MAX(tick) AS t FROM note_manifest WHERE agent_id=?",
                    "SELECT MAX(tick) AS t FROM policy_versions WHERE agent_id=?"):
            row = self.db.fetchone(sql, (self.agent_id,))
            if row is not None and row["t"] is not None:
                ticks.append(int(row["t"]))
        self_path = self.dir / _SELF_FILE
        if _problem(self_path) is None and self_path.is_file():
            try:
                header, _ = split_frontmatter(self_path.read_text(encoding="utf-8"))
                ticks.append(_as_int(header.get("tick"), 0))
            except (OSError, UnicodeDecodeError):
                pass
        return max(ticks)

    def _note_id_taken(self, note_id: str) -> bool:
        return self.db.fetchone("SELECT 1 FROM notes WHERE note_id=?", (note_id,)) is not None

    def _link_refusal(self, raw: str | None) -> str | None:
        """The refusal for a path whose file on disk is a link (or behind a linked directory), else None. A
        malformed path is left to the command layer, which refuses it with its own message."""
        if raw is None:
            return None
        try:
            target = resolve_path(raw)
        except PathError:
            return None
        checks: list[tuple[str, Path, bool]] = []
        if target.area in ("notes", "index"):
            checks.append((f"the directory behind {NOTES_DIR}/ and {INDEX_DIR}/", self.dir / _NOTES_SUBDIR, True))
            if target.name is not None:
                checks.append((target.virtual, self.dir / _NOTES_SUBDIR / target.name, False))
        elif target.area == "self":
            checks.append((SELF_PATH, self.dir / _SELF_FILE, False))
        elif target.area == "policy":
            checks.append((POLICY_PATH, self.dir / POLICY_FILE, False))
        for shown, host, directory in checks:
            problem = _problem(host, directory=directory)
            if problem is not None:
                return _refusal(shown, problem)
        return None


# --- JSON-RPC 2.0 over stdio -----------------------------------------------------------------------------------
class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class McpServer:
    """JSON-RPC 2.0, newline-delimited, in front of a :class:`VaultSession` (see the module docstring)."""

    def __init__(self, session: VaultSession) -> None:
        self.session = session
        self.initialized = False      # initialize answered
        self.ready = False            # notifications/initialized received
        self.protocol_version: str | None = None

    def serve(self, reader: BinaryIO, writer: BinaryIO) -> int:
        """Answer messages from ``reader`` on ``writer`` until end of input; returns the exit status (0)."""
        while True:
            line = reader.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                return 0
            if len(line) > MAX_MESSAGE_BYTES and not line.endswith(b"\n"):
                while True:  # drop the rest of the oversized line
                    more = reader.readline(MAX_MESSAGE_BYTES)
                    if not more or more.endswith(b"\n"):
                        break
                out: bytes | None = _encode(_error(None, INVALID_REQUEST, f"message longer than {MAX_MESSAGE_BYTES} bytes"))
            else:
                out = self.handle_line(line)
            if out is not None:
                writer.write(out + b"\n")
                writer.flush()

    def handle_line(self, line: bytes | str) -> bytes | None:
        """One line of input to one line of output (without the newline), or None when nothing is owed."""
        try:
            text = line.decode("utf-8") if isinstance(line, bytes) else line
        except UnicodeDecodeError:
            return _encode(_error(None, PARSE_ERROR, "parse error: input is not UTF-8"))
        if not text.strip():
            return None
        try:
            message = json.loads(text)
        except (ValueError, RecursionError):
            return _encode(_error(None, PARSE_ERROR, "parse error: not valid JSON"))
        out = self.handle(message)
        return None if out is None else _encode(out)

    def handle(self, message: Any) -> Any:
        """One parsed message (or batch) to its response (or list of responses), None for notifications only."""
        if isinstance(message, list):
            if not message:
                return _error(None, INVALID_REQUEST, "empty batch")
            replies = [r for r in (self._handle_one(m) for m in message) if r is not None]
            return replies or None
        return self._handle_one(message)

    def _handle_one(self, msg: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(_valid_id(msg), INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        if "method" not in msg:
            if "id" in msg and ("result" in msg or "error" in msg):
                return None  # a response to a request this server never sent: nothing to answer
            return _error(_valid_id(msg), INVALID_REQUEST, "a request needs a method")
        method = msg["method"]
        is_request = "id" in msg
        if is_request and _valid_id(msg) is None:
            return _error(None, INVALID_REQUEST, "id must be a string or an integer")
        if not isinstance(method, str):
            return _error(msg.get("id"), INVALID_REQUEST, "method must be a string") if is_request else None
        params = msg.get("params")
        if params is None:
            params = {}
        if not is_request:
            if isinstance(params, dict):
                self._notify(method, params)
            return None
        req_id = msg["id"]
        if not isinstance(params, dict):
            return _error(req_id, INVALID_PARAMS, "params must be an object")
        try:
            result = self._dispatch(method, params)
        except _RpcError as e:
            return _error(req_id, e.code, e.message)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return _error(req_id, INTERNAL_ERROR, "internal error (the server's stderr has the details)")
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/initialized":
            self.ready = True
        # notifications/cancelled and anything else: every request is answered before the next is read

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if not self.initialized:
            raise _RpcError(NOT_INITIALIZED, f"the server is not initialized; send initialize before {method}")
        if method == "tools/list":
            return {"tools": list(TOOLS)}
        if method == "tools/call":
            return self._call(params)
        raise _RpcError(METHOD_NOT_FOUND, f"method not found: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        self.protocol_version = asked if isinstance(asked, str) and asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        self.initialized = True
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": "The Void memory vault", "version": _version()},
            "instructions": self.session.instructions(),
        }

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOL_NAMES:
            raise _RpcError(INVALID_PARAMS, f"unknown tool: {name}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "arguments must be an object")
        ok, text = self.session.call(name, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": not ok}


def serve(session: VaultSession, reader: BinaryIO, writer: BinaryIO) -> int:
    """Run an :class:`McpServer` for ``session`` over the given byte streams until end of input."""
    return McpServer(session).serve(reader, writer)


# --- helpers -----------------------------------------------------------------------------------------------------
def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _encode(obj: Any) -> bytes:
    # ASCII-escaped: no raw newline can appear inside a message and a lone surrogate cannot break the encoding
    return json.dumps(obj, separators=(",", ":")).encode("ascii")


def _valid_id(msg: Any) -> str | int | None:
    """The request id when it is a string or an integer (MCP forbids null), else None."""
    if not isinstance(msg, dict):
        return None
    value = msg.get("id")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return value


def _encodable(value: Any) -> bool:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
    return True


def _validation_text(e: ValidationError) -> str:
    parts: list[str] = []
    for err in e.errors():
        msg = str(err.get("msg", "invalid"))
        msg = msg[len("Value error, "):] if msg.startswith("Value error, ") else msg
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _version() -> str:
    try:
        return metadata.version("void")
    except metadata.PackageNotFoundError:
        return "0.0.0"


# --- command line ------------------------------------------------------------------------------------------------
def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0: {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    d = MaintenanceConfig()
    p = argparse.ArgumentParser(
        prog="void mcp",
        description="Serve the six memory commands (view, create, str_replace, insert, delete, rename) over the "
                    "Model Context Protocol for one vault directory: JSON-RPC 2.0, one message per line on stdin "
                    "and stdout. Diagnostics go to stderr.",
    )
    p.add_argument("--vault", required=True, metavar="DIR",
                   help="the vault directory (notes/, self.md, memory_policy.md); created when missing")
    p.add_argument("--max-files", type=_positive, metavar="N", help=f"notes per vault (default {d.max_files})")
    p.add_argument("--max-file-bytes", type=_positive, metavar="N", help=f"bytes per file (default {d.max_file_bytes})")
    p.add_argument("--max-vault-bytes", type=_positive, metavar="N",
                   help=f"bytes of notes in the whole vault (default {d.max_vault_bytes})")
    return p


def cli_main(argv: list[str]) -> int:
    """``void mcp ARGS`` (the CLI's lazy-subcommand contract): serve until stdin closes; 2 on a bad argument."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # --help (0) or a usage error (2): argparse already printed why
        return e.code if isinstance(e.code, int) else 2
    try:
        session = VaultSession(args.vault, max_files=args.max_files, max_file_bytes=args.max_file_bytes,
                               max_vault_bytes=args.max_vault_bytes)
    except VaultError as e:
        print(f"void mcp: {e}", file=sys.stderr)
        return 2
    print(f"void mcp: serving {session.dir} on stdin/stdout", file=sys.stderr)
    try:
        return serve(session, sys.stdin.buffer, sys.stdout.buffer)
    except BrokenPipeError:  # the client went away: stop quietly (and keep the interpreter's exit flush quiet)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        session.db.close()
