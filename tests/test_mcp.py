"""``void mcp``: the six memory commands over the Model Context Protocol (MEMORY_EVOLUTION §5.11, WP12).

The server is driven over in-memory pipes: a thread serves the near ends of two ``os.pipe`` pairs and the test
speaks JSON-RPC on the far ends, one line per message. Protocol tests cover ``initialize``, ``tools/list`` and the
JSON-RPC errors; command tests replay every call straight through :class:`MemoryCommands`, set up as the server
sets itself up, and require the same success strings, the same refusals and byte-identical files; confinement
tests try traversal, host paths, symbolic and hard links and check that nothing outside the vault is read or
written. One test runs the real ``void mcp`` subprocess.
"""

from __future__ import annotations

import io
import json
import os
import re
import select
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from void.cli import main
from void.config import MemoryConfig, MemoryPolicyConfig
from void.db import Database
from void.ids import IdFactory
from void.memory.commands import MemoryCommand, MemoryCommands
from void.memory.notes import OPERATOR_CHANNEL, Provenance, parse_note
from void.memory.policy import PolicyStore
from void.memory.store import OPERATOR_TAG, MemoryStore
from void.server.mcp import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    MAX_MESSAGE_BYTES,
    METHOD_NOT_FOUND,
    NOT_INITIALIZED,
    PARSE_ERROR,
    PROTOCOL_VERSIONS,
    SERVER_NAME,
    TICKS_PER_DAY,
    TOOL_NAMES,
    McpServer,
    VaultError,
    VaultSession,
    cli_main,
)

REPO = Path(__file__).resolve().parents[1]
ALL_RIGHTS = ["notes", "policy", "structure"]
SECRET = "TOPSECRET-7f3a"
SECRET_NOTE = f"---\nid: note_outside\ntitle: Outside\n---\n{SECRET} lives outside the vault.\n"
SIX = ("view", "create", "str_replace", "insert", "delete", "rename")


# --- a client on in-memory pipes -----------------------------------------------------------------------------------
class PipeClient:
    """Speaks newline-delimited JSON-RPC to an :class:`McpServer` running in a thread over two OS pipes."""

    def __init__(self, session: VaultSession) -> None:
        c2s_r, self._w = os.pipe()
        self._r, s2c_w = os.pipe()
        self.server = McpServer(session)
        self.session = session
        self.status: int | None = None
        self._buf = b""
        self._next_id = 0

        def run() -> None:
            with os.fdopen(c2s_r, "rb") as reader, os.fdopen(s2c_w, "wb") as writer:
                self.status = self.server.serve(reader, writer)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def send_raw(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self._w, view):]

    def send(self, message: Any) -> None:
        self.send_raw(json.dumps(message).encode("utf-8") + b"\n")

    def recv(self, timeout: float = 20.0) -> Any:
        while b"\n" not in self._buf:
            ready, _, _ = select.select([self._r], [], [], timeout)
            if not ready:
                raise TimeoutError("the server sent nothing")
            chunk = os.read(self._r, 1 << 16)
            if not chunk:
                raise EOFError("the server closed its output")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        assert line.isascii()
        return json.loads(line)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        reply = self.recv()
        assert reply["jsonrpc"] == "2.0" and reply["id"] == self._next_id, reply
        return reply

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, **({} if params is None else {"params": params})})

    def initialize(self, version: str = PROTOCOL_VERSIONS[0]) -> dict[str, Any]:
        reply = self.request("initialize", {"protocolVersion": version, "capabilities": {},
                                            "clientInfo": {"name": "test-harness", "version": "1"}})
        self.notify("notifications/initialized")
        return reply["result"]

    def call(self, name: str, **arguments: Any) -> tuple[bool, str]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})["result"]
        assert [c["type"] for c in result["content"]] == ["text"]
        return not result["isError"], result["content"][0]["text"]

    def close(self) -> int | None:
        os.close(self._w)
        self.thread.join(20)
        os.close(self._r)
        return self.status


@pytest.fixture
def client(tmp_path: Path):
    """``client(vault=None, init=True, **limits)``: a new server over ``vault`` (default ``tmp/vault/ada``)."""
    made: list[PipeClient] = []

    def make(vault: Path | None = None, *, init: bool = True, **limits: Any) -> PipeClient:
        c = PipeClient(VaultSession(vault or tmp_path / "vault" / "ada", **limits))
        made.append(c)
        if init:
            c.initialize()
        return c

    yield make
    for c in made:
        assert c.close() == 0


class Reference:
    """The same commands applied straight through :class:`MemoryCommands`, set up as the server sets itself up
    (same agent id, id stream, limits, policy store and clock), in a separate vault root."""

    def __init__(self, root: Path, session: VaultSession) -> None:
        self.db = Database(root / "ref.db")
        ids = IdFactory(session.ids.run_id)
        store = MemoryStore(self.db, MemoryConfig(), ids, root / "vaults")
        policy = PolicyStore(self.db, MemoryPolicyConfig(enabled=True), root / "vaults", ticks_per_day=TICKS_PER_DAY)
        self.commands = MemoryCommands(self.db, store, session.cfg, ids, policy=policy)
        self.agent = session.agent_id
        self.dir = root / "vaults" / session.agent_id
        self.tick = session.tick

    def apply(self, name: str, **arguments: Any) -> tuple[bool, str]:
        self.tick += 1
        res = self.commands.apply(self.agent, MemoryCommand(command=name, **arguments), self.tick, purpose="maintain",
                                  rights=ALL_RIGHTS)  # type: ignore[arg-type]
        return res.ok, res.message


def created_path(message: str) -> str:
    m = re.match(r"File created successfully at: (\S+)", message)
    assert m, message
    return m.group(1)


def tree(root: Path) -> dict[str, bytes | str]:
    """Every entry under ``root``: file bytes, or the target of a link (never followed)."""
    out: dict[str, bytes | str] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = "-> " + os.readlink(p)
        elif p.is_file():
            out[rel] = p.read_bytes()
    return out


# --- initialize -------------------------------------------------------------------------------------------------------
def test_initialize_negotiates_the_version_and_gates_the_tools(client) -> None:
    c = client(init=False)
    assert c.request("ping")["result"] == {}  # ping is allowed before initialize
    early = c.request("tools/list")["error"]
    assert early["code"] == NOT_INITIALIZED and "initialize" in early["message"]
    assert c.request("tools/call", {"name": "view", "arguments": {"path": "/memories"}})["error"]["code"] == NOT_INITIALIZED

    result = c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "1"}})["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == SERVER_NAME and result["serverInfo"]["version"]
    text = result["instructions"]
    assert text.isascii() and "/memories/notes/" in text and "200 files" in text
    c.notify("notifications/initialized")  # nothing is owed for a notification: the next line answers the ping
    assert c.request("ping")["result"] == {}
    assert c.server.initialized and c.server.ready

    for version in PROTOCOL_VERSIONS:  # every supported version is echoed; anything else gets the newest
        assert client(init=False).request("initialize", {"protocolVersion": version})["result"]["protocolVersion"] == version
    assert client(init=False).request("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]
    assert client(init=False).request("initialize")["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]


def test_jsonrpc_errors_and_batches(tmp_path: Path) -> None:
    lines = [
        b"not json",
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}',
        b'{"id":2,"method":"ping"}',
        b'{"jsonrpc":"2.0","id":3,"method":"resources/list"}',
        b'{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"memory","arguments":{}}}',
        b'{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"view","arguments":[1]}}',
        b'{"jsonrpc":"2.0","id":6,"method":"tools/call","params":[1]}',
        b'{"jsonrpc":"2.0","id":null,"method":"ping"}',
        b'{"jsonrpc":"2.0","id":true,"method":"ping"}',
        b"[]",
        b'[{"jsonrpc":"2.0","id":7,"method":"ping"},{"jsonrpc":"2.0","method":"notifications/initialized"},'
        b'{"jsonrpc":"2.0","id":8,"method":"tools/list"}]',
        b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}',
        b"   ",
        b"\xff\xfe{",
        b'{"jsonrpc":"2.0","id":9,"result":{}}',
        b'{"jsonrpc":"2.0","id":"s-10","method":"ping"}\r',
        b"x" * (MAX_MESSAGE_BYTES + 10),
        b'{"jsonrpc":"2.0","id":11,"method":"ping"}',
    ]
    out = io.BytesIO()
    assert McpServer(VaultSession(tmp_path / "v")).serve(io.BytesIO(b"\n".join(lines) + b"\n"), out) == 0
    replies = [json.loads(line) for line in out.getvalue().split(b"\n") if line]

    def err(reply: dict[str, Any]) -> tuple[Any, int]:
        return reply["id"], reply["error"]["code"]

    assert err(replies[0]) == (None, PARSE_ERROR)
    assert replies[1]["result"]["protocolVersion"] == "2024-11-05"
    assert err(replies[2]) == (2, INVALID_REQUEST)
    assert err(replies[3]) == (3, METHOD_NOT_FOUND)
    assert err(replies[4]) == (4, INVALID_PARAMS) and "unknown tool" in replies[4]["error"]["message"]
    assert err(replies[5]) == (5, INVALID_PARAMS)
    assert err(replies[6]) == (6, INVALID_PARAMS)
    assert err(replies[7]) == (None, INVALID_REQUEST)
    assert err(replies[8]) == (None, INVALID_REQUEST)
    assert err(replies[9]) == (None, INVALID_REQUEST)
    batch = replies[10]
    assert isinstance(batch, list) and [r["id"] for r in batch] == [7, 8]
    assert [t["name"] for t in batch[1]["result"]["tools"]] == list(SIX)
    # the cancellation, the blank line and the stray response are owed nothing
    assert err(replies[11]) == (None, PARSE_ERROR)
    assert replies[12] == {"jsonrpc": "2.0", "id": "s-10", "result": {}}
    assert err(replies[13]) == (None, INVALID_REQUEST) and "longer than" in replies[13]["error"]["message"]
    assert replies[14] == {"jsonrpc": "2.0", "id": 11, "result": {}}  # the oversized line was drained, not parsed
    assert len(replies) == 15


# --- tools/list -------------------------------------------------------------------------------------------------------
SAMPLE: dict[str, Any] = {
    "path": "/memories/notes/a.md", "file_text": "x", "old_str": "x", "new_str": "y", "insert_line": 0,
    "insert_text": "x", "old_path": "/memories/notes/a.md", "new_path": "/memories/index/b.md", "view_range": [1, 2],
}


def _model_bound(prop: dict[str, Any], key: str) -> Any:
    """``key`` of the non-null branch of a pydantic ``X | None`` property schema."""
    for branch in prop.get("anyOf", [prop]):
        if branch.get("type") != "null" and key in branch:
            return branch[key]
    return None


def test_tools_list_returns_the_six_tools_with_schemas(client) -> None:
    tools = client().request("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == list(SIX) == list(TOOL_NAMES)
    model = MemoryCommand.model_json_schema()["properties"]
    for tool in tools:
        name = tool["name"]
        assert tool["description"].isascii() and tool["title"]
        assert tool["annotations"]["readOnlyHint"] is (name == "view") and tool["annotations"]["openWorldHint"] is False
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"]) <= set(MemoryCommand.model_fields) - {"command"}
        for field, prop in schema["properties"].items():
            assert prop["type"] in ("string", "integer", "array") and prop["description"]
            if prop["type"] == "string":
                assert prop["maxLength"] == _model_bound(model[field], "maxLength"), (name, field)
            if prop["type"] == "integer":
                assert (prop["minimum"], prop["maximum"]) == (_model_bound(model[field], "minimum"),
                                                              _model_bound(model[field], "maximum"))
        # the schema requires exactly what MemoryCommand requires for this command
        required = {k: SAMPLE[k] for k in schema["required"]}
        MemoryCommand(command=name, **required)
        for missing in schema["required"]:
            with pytest.raises(ValidationError):
                MemoryCommand(command=name, **{k: v for k, v in required.items() if k != missing})
    assert {t["name"]: t["inputSchema"]["required"] for t in tools} == {
        "view": ["path"], "create": ["path", "file_text"], "str_replace": ["path", "old_str"],
        "insert": ["path", "insert_line", "insert_text"], "delete": ["path"], "rename": ["old_path", "new_path"],
    }


# --- the commands answer as MemoryCommands does -----------------------------------------------------------------------
def test_each_command_answers_with_the_memory_commands_string(tmp_path: Path, client) -> None:
    c = client(tmp_path / "srv" / "ada")
    ref = Reference(tmp_path / "ref", c.session)
    succeeded: set[str] = set()

    def both(name: str, **arguments: Any) -> tuple[bool, str]:
        got = c.call(name, **arguments)
        assert got == ref.apply(name, **arguments), (name, arguments)
        if got[0]:
            succeeded.add(name)
        return got

    assert both("view", path="/memories")[0]
    ok, msg = both("create", path="/memories/notes/thin-node.md",
                   file_text="Node n3 ran dry at noon.\nAda says n7 is rich.\nAda says n7 is rich.")
    assert ok and msg.startswith("File created successfully at: /memories/notes/thin-node-")
    note = created_path(msg)
    ok, msg = both("create", path="/memories/index/places.md", file_text="Places: [[thin node]]")
    index = created_path(msg)
    assert both("create", path="/memories/notes/thin-node.md", file_text="again") == (
        False, f"/memories/notes/thin-node.md already exists as {note} (titled 'thin node')")
    ok, msg = both("str_replace", path=note, old_str="Ada says n7 is rich.", new_str="x")
    assert not ok and "occurs 2 times" in msg and "(lines 2, 3)" in msg
    assert both("str_replace", path=note, old_str="ran dry at noon", new_str="ran dry at noon, refilled at dusk") == (
        True, "The memory file has been edited.")
    assert both("insert", path=note, insert_line=0, insert_text="# Node n3") == (True, f"The file {note} has been edited.")
    ok, msg = both("view", path=note, view_range=[1, 2])
    assert ok and msg.startswith(f"Here's the content of {note} with line numbers:\n     1\t# Node n3\n")
    assert both("view", path="/memories/notes")[0] and both("view", path=index)[0]
    ok, msg = both("rename", old_path=note, new_path="/memories/index/node-n3.md")
    assert ok and msg.startswith(f"Successfully renamed {note} to /memories/index/node-n3-")
    moved = msg.split(" to ", 1)[1].split(" ", 1)[0]
    assert both("delete", path=index) == (True, f"Successfully deleted {index}")
    assert both("delete", path=index) == (False, f"{index} does not exist")
    # the policy file, through the policy store
    assert both("create", path="/memories/memory_policy.md", file_text="# How I remember\n\n- One note per node.") == (
        True, "File created successfully at: /memories/memory_policy.md")
    assert both("str_replace", path="/memories/memory_policy.md", old_str="One note", new_str="One short note")[0]
    assert both("insert", path="/memories/memory_policy.md", insert_line=3, insert_text="- Link people.")[0]
    assert both("view", path="/memories/memory_policy.md")[0]
    assert both("delete", path="/memories/memory_policy.md") == (False, "the memory policy cannot be deleted")
    # refusals read the same too
    assert both("view", path="/memories/self.md") == (False, "/memories/self.md does not exist")
    assert both("create", path="/memories/self.md", file_text="me") == (
        False, "self.md is edited with revise_self, not with memory commands")
    assert not both("view", path="/memories/../etc/passwd")[0]
    assert not both("insert", path=moved, insert_line=99, insert_text="x")[0]
    assert not both("rename", old_path=moved, new_path=moved)[0]
    ok, listing = both("view", path="/memories")
    assert ok and "memory_policy.md" in listing and moved.rsplit("/", 1)[1] in listing

    assert succeeded == set(SIX)
    assert tree(c.session.dir) == tree(ref.dir)  # the server wrote exactly the files the command layer writes


def test_bad_arguments_are_tool_errors_not_protocol_errors(client) -> None:
    c = client()
    assert c.call("create", path="/memories/notes/a.md") == (False, "invalid create arguments: create requires file_text")
    assert c.call("view", path="/memories", file_text="x") == (False, "view does not take file_text (it takes path, view_range)")
    ok, msg = c.call("insert", path="/memories/notes/a.md", insert_line="top", insert_text="x")
    assert not ok and msg.startswith("invalid insert arguments: insert_line:")
    ok, msg = c.call("view", path=7)
    assert not ok and "path" in msg
    ok, msg = c.call("create", path="/memories/notes/a.md", file_text="x" * 8001)
    assert not ok and "file_text" in msg
    assert c.call("create", path="/memories/notes/a.md", file_text="half \ud800 a pair") == (
        False, "arguments must be valid Unicode text")
    assert tree(c.session.dir) == {}  # none of these touched the vault
    assert c.session.tick == 0


def test_limits_come_from_the_command_line(client) -> None:
    c = client(max_files=1, max_file_bytes=40, max_vault_bytes=60)
    assert "Limits: 1 files, 40 bytes per file, 60 bytes in all." in c.server.session.instructions()
    ok, msg = c.call("create", path="/memories/notes/a.md", file_text="x" * 30)
    assert ok
    assert c.call("create", path="/memories/notes/b.md", file_text="y") == (
        False, "your vault already holds 1 files; delete one first")
    assert c.call("str_replace", path=created_path(msg), old_str="x" * 30, new_str="z" * 41) == (
        False, "the file would be 41 bytes; the limit is 40")
    with pytest.raises(VaultError):
        VaultSession(c.session.dir, max_file_bytes=100, max_vault_bytes=50)


# --- confinement --------------------------------------------------------------------------------------------------------
TRAVERSAL = (
    "/memories/../secret.md", "/memories/notes/../../secret.md", "../secret.md", "notes/../../../secret.md",
    "/memories/%2e%2e/secret.md", "/memories/notes/%252e%252e%252fsecret.md", "/memories/notes/..%2f..%2fsecret.md",
    "/memories/notes/..\\..\\secret.md", "/etc/passwd", "/memories/notes/sub/secret.md", "/memories/notes/.secret.md",
    "/memories/notes/secret.txt", "/memories/notes/secret\x00.md", "/memories/notes/./secret.md",
)


def test_traversal_attempts_are_refused(tmp_path: Path, client) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(SECRET_NOTE, encoding="utf-8")
    (tmp_path / "vault" / "secret.md").parent.mkdir(parents=True)
    (tmp_path / "vault" / "secret.md").write_text(SECRET_NOTE, encoding="utf-8")  # a sibling of the vault
    c = client(tmp_path / "vault" / "ada")
    ref = Reference(tmp_path / "ref", c.session)
    ok, msg = c.call("create", path="/memories/notes/good.md", file_text="a fine note")
    assert ok and ref.apply("create", path="/memories/notes/good.md", file_text="a fine note") == (ok, msg)
    good = created_path(msg)
    before = tree(tmp_path / "outside"), (tmp_path / "vault" / "secret.md").read_bytes()
    host = (str(outside / "secret.md"), str(c.session.dir / "notes" / good.rsplit("/", 1)[1]), str(tmp_path / "vault" / "secret.md"))
    for path in (*TRAVERSAL, *host):
        attempts: list[tuple[str, dict[str, Any]]] = [
            ("view", {"path": path}),
            ("create", {"path": path, "file_text": "planted"}),
            ("str_replace", {"path": path, "old_str": SECRET, "new_str": "gone"}),
            ("insert", {"path": path, "insert_line": 0, "insert_text": "planted"}),
            ("delete", {"path": path}),
            ("rename", {"old_path": good, "new_path": path}),
            ("rename", {"old_path": path, "new_path": "/memories/notes/moved.md"}),
        ]
        for name, arguments in attempts:
            ok, msg = c.call(name, **arguments)
            assert not ok, (name, arguments, msg)
            assert SECRET not in msg
            assert (ok, msg) == ref.apply(name, **arguments)  # refused with the command layer's own message
    assert (tree(tmp_path / "outside"), (tmp_path / "vault" / "secret.md").read_bytes()) == before
    assert sorted(tree(c.session.dir)) == ["notes/" + good.rsplit("/", 1)[1]]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symbolic links")
def test_links_are_never_followed(tmp_path: Path, client) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in ("secret.md", "hard-target.md", "self.md", "policy.md"):
        (outside / name).write_text(SECRET_NOTE, encoding="utf-8")
    vault = tmp_path / "vault" / "ada"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "leak.md").symlink_to(outside / "secret.md")
    (vault / "notes" / "dangling.md").symlink_to(outside / "created-through-link.md")
    (vault / "notes" / "inner.md").symlink_to(vault / "notes" / "real.md")  # even a link inside the vault
    (vault / "notes" / "real.md").write_text("---\nid: note_real\ntitle: real\n---\na real note\n", encoding="utf-8")
    os.link(outside / "hard-target.md", vault / "notes" / "hard.md")
    (vault / "self.md").symlink_to(outside / "self.md")
    (vault / "memory_policy.md").symlink_to(outside / "policy.md")
    before = tree(outside)

    c = client(vault)
    ok, listing = c.call("view", path="/memories")
    assert ok and SECRET not in listing and "real.md" in listing
    for name in ("leak.md", "dangling.md", "inner.md", "hard.md"):
        assert name not in listing
    for name, what in (("leak.md", "a symbolic link"), ("dangling.md", "a symbolic link"), ("inner.md", "a symbolic link"),
                       ("hard.md", "a hard link")):
        p = f"/memories/notes/{name}"
        for tool, arguments in (
            ("view", {"path": p}), ("create", {"path": p, "file_text": "planted"}),
            ("str_replace", {"path": p, "old_str": SECRET, "new_str": "gone"}),
            ("insert", {"path": p, "insert_line": 0, "insert_text": "planted"}), ("delete", {"path": p}),
            ("rename", {"old_path": p, "new_path": "/memories/notes/moved.md"}),
            ("rename", {"old_path": "/memories/notes/real.md", "new_path": p}),
            ("view", {"path": f"/memories/index/{name}"}),
        ):
            ok, msg = c.call(tool, **arguments)
            assert not ok and what in msg and SECRET not in msg, (tool, arguments, msg)
    for tool, arguments in (
        ("view", {"path": "/memories/self.md"}), ("view", {"path": "/memories/memory_policy.md"}),
        ("create", {"path": "/memories/memory_policy.md", "file_text": "planted"}),
        ("str_replace", {"path": "/memories/memory_policy.md", "old_str": SECRET, "new_str": "gone"}),
        ("insert", {"path": "/memories/memory_policy.md", "insert_line": 0, "insert_text": "planted"}),
    ):
        ok, msg = c.call(tool, **arguments)
        assert not ok and "a symbolic link" in msg and SECRET not in msg, (tool, arguments, msg)
    # the regular parts of the vault still work, and nothing outside it changed
    assert c.call("str_replace", path="/memories/notes/real.md", old_str="a real", new_str="a still real") == (
        True, "The memory file has been edited.")
    assert c.call("create", path="/memories/notes/fine.md", file_text="fine")[0]
    assert tree(outside) == before
    assert not (outside / "created-through-link.md").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symbolic links")
def test_a_linked_notes_directory_is_refused(tmp_path: Path, client) -> None:
    outside = tmp_path / "outside" / "notes"
    outside.mkdir(parents=True)
    (outside / "inside-out.md").write_text(SECRET_NOTE, encoding="utf-8")
    vault = tmp_path / "vault" / "ada"
    vault.mkdir(parents=True)
    (vault / "notes").symlink_to(outside, target_is_directory=True)
    before = tree(outside)
    c = client(vault)
    ok, listing = c.call("view", path="/memories")
    assert ok and SECRET not in listing and "inside-out" not in listing
    for tool, arguments in (
        ("view", {"path": "/memories/notes"}), ("view", {"path": "/memories/index"}),
        ("view", {"path": "/memories/notes/inside-out.md"}), ("create", {"path": "/memories/notes/new.md", "file_text": "x"}),
        ("create", {"path": "/memories/index/new.md", "file_text": "x"}), ("delete", {"path": "/memories/notes/inside-out.md"}),
    ):
        ok, msg = c.call(tool, **arguments)
        assert not ok and "a symbolic link" in msg and SECRET not in msg, (tool, arguments, msg)
    assert tree(outside) == before


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symbolic links")
def test_a_write_never_lands_on_a_link_at_the_name_the_vault_picks(tmp_path: Path) -> None:
    session = VaultSession(tmp_path / "vault" / "ada")
    ok, msg = Reference(tmp_path / "ref", session).apply("create", path="/memories/notes/thin-node.md", file_text="n3 ran dry")
    name = created_path(msg).rsplit("/", 1)[1]  # the file name the first create in this vault will pick
    (tmp_path / "outside").mkdir()
    (session.dir / "notes").mkdir()
    (session.dir / "notes" / name).symlink_to(tmp_path / "outside" / "planted.md")
    ok, msg = session.call("create", {"path": "/memories/notes/thin-node.md", "file_text": "n3 ran dry"})
    assert not ok and msg.startswith(f"/memories/notes/{name} is a symbolic link")
    assert not (tmp_path / "outside" / "planted.md").exists()
    assert sorted(p.name for p in (session.dir / "notes").iterdir()) == [name]
    assert session.call("view", {"path": "/memories/notes"})[1] == "/memories/notes/\n  (empty)"


# --- the directory is the source of truth -------------------------------------------------------------------------------
def test_a_simulator_vault_keeps_its_provenance(tmp_path: Path, client) -> None:
    db = Database(tmp_path / "world.db")
    store = MemoryStore(db, MemoryConfig(), IdFactory("run"), tmp_path / "vaults")
    gossip = store.remember("ada", 40, "Bo says n7 pays well.", title="Bo on n7", tags=["n7"],
                            provenance=Provenance(channel="gossip", source_agent_id="ag_bo", hop=1, origin_note_id="note_o",
                                                  transfer_tick=40, path_agents=["ag_bo"]))
    store.revise_self("ada", 41, "I forage the east.")
    PolicyStore(db, MemoryPolicyConfig(enabled=True), tmp_path / "vaults").set("ada", "- One note per node.", 42, source="seed")
    db.close()

    c = client(tmp_path / "vaults" / "ada")
    assert c.session.tick == 42  # the clock counts on from the newest tick the vault records
    ok, listing = c.call("view", path="/memories")
    assert ok and "gossip" in listing and OPERATOR_CHANNEL not in listing
    assert "self.md" in listing and "memory_policy.md" in listing
    file = Path(str(gossip.path))
    ok, msg = c.call("str_replace", path=f"/memories/notes/{file.name}", old_str="pays well", new_str="paid well once")
    assert (ok, msg) == (True, "The memory file has been edited.")
    note = parse_note(file.read_text(encoding="utf-8"))
    assert note.body == "Bo says n7 paid well once."
    assert (note.note_id, note.title, note.created_tick, note.tags) == (gossip.note_id, "Bo on n7", 40, ["n7"])
    assert note.provenance == gossip.provenance and OPERATOR_TAG not in note.tags
    ok, shown = c.call("view", path="/memories/self.md")
    assert ok and "I forage the east." in shown
    ok, msg = c.call("str_replace", path="/memories/memory_policy.md", old_str="One note", new_str="One short note")
    assert ok
    header, text = PolicyStore.parse_file((c.session.dir / "memory_policy.md").read_text(encoding="utf-8"))
    assert (header["version"], header["source"], text) == (2, "self_edit", "- One short note per node.")
    ok, msg = c.call("create", path="/memories/notes/east.md", file_text="East is quiet.")
    assert ok
    new = parse_note((c.session.dir / "notes" / created_path(msg).rsplit("/", 1)[1]).read_text(encoding="utf-8"))
    assert new.created_tick == c.session.tick > 42 and new.note_id != gossip.note_id


def test_outside_edits_are_picked_up_between_calls(client) -> None:
    c = client()
    ok, msg = c.call("create", path="/memories/notes/thin-node.md", file_text="n3 ran dry")
    path = created_path(msg)
    file = c.session.dir / "notes" / path.rsplit("/", 1)[1]
    file.write_text(file.read_text(encoding="utf-8").replace("n3 ran dry", "n3 ran dry, then refilled"), encoding="utf-8")
    ok, shown = c.call("view", path=path)
    assert ok and shown.endswith("n3 ran dry, then refilled")
    ok, listing = c.call("view", path="/memories")
    assert ok and OPERATOR_CHANNEL not in listing  # an outside edit is believed, not flagged

    (c.session.dir / "notes" / "hand-note.md").write_text("Written by hand.\n", encoding="utf-8")
    ok, listing = c.call("view", path="/memories/notes")
    assert ok and "hand-note.md" in listing
    assert c.call("str_replace", path="/memories/notes/hand-note.md", old_str="by hand", new_str="by hand, then edited") == (
        True, "The memory file has been edited.")
    rewritten = [p for p in (c.session.dir / "notes").iterdir() if p.name.startswith("hand-note")]
    assert len(rewritten) == 1 and parse_note(rewritten[0].read_text(encoding="utf-8")).title == "hand note"
    assert "Written by hand, then edited." in rewritten[0].read_text(encoding="utf-8")

    file.unlink()
    ok, listing = c.call("view", path="/memories/notes")
    assert ok and file.name not in listing
    assert c.call("view", path=path) == (False, f"{path} does not exist")

    (c.session.dir / "memory_policy.md").write_text("- Written outside.\n", encoding="utf-8")
    ok, shown = c.call("view", path="/memories/memory_policy.md")
    assert ok and shown.endswith("1\t- Written outside.")


def test_a_copied_note_does_not_break_the_index(tmp_path: Path, client) -> None:
    vault = tmp_path / "vault" / "ada"
    (vault / "notes").mkdir(parents=True)
    text = "---\nid: note_same\ntitle: twin\n---\nthe same note twice\n"
    (vault / "notes" / "twin-a.md").write_text(text, encoding="utf-8")
    (vault / "notes" / "twin-b.md").write_text(text, encoding="utf-8")
    (vault / "notes" / "binary.md").write_bytes(b"\xff\xfe\x00 not text")
    ok, listing = client(vault).call("view", path="/memories/notes")
    assert ok and "twin-a.md" in listing and "twin-b.md" not in listing and "binary.md" not in listing


def test_the_same_calls_write_the_same_files(tmp_path: Path) -> None:
    def run(root: Path) -> dict[str, bytes | str]:
        s = VaultSession(root / "ada")
        for text in ("n3 ran dry", "n7 is rich", "Bo lies about n5"):
            assert s.call("create", {"path": f"/memories/notes/{text.split()[0]}.md", "file_text": text})[0]
        x = created_path(s.call("create", {"path": "/memories/notes/x.md", "file_text": "x"})[1])
        assert s.call("rename", {"old_path": x, "new_path": "/memories/index/x.md"})[0]
        return tree(s.dir)

    assert run(tmp_path / "a") == run(tmp_path / "b")


# --- the command line -----------------------------------------------------------------------------------------------------
def test_void_mcp_help_and_bad_arguments(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["mcp", "--help"]) == 0
    assert capsys.readouterr().out.startswith("usage: void mcp")
    a_file = tmp_path / "file.md"
    a_file.write_text("x", encoding="utf-8")
    assert cli_main(["--vault", str(a_file)]) == 2
    assert "is not a directory" in capsys.readouterr().err
    assert cli_main(["--vault", str(tmp_path / "v"), "--max-file-bytes", "0"]) == 2
    assert cli_main(["--vault", str(tmp_path / "v"), "--max-file-bytes", "9000", "--max-vault-bytes", "100"]) == 2
    assert "max_file_bytes" in capsys.readouterr().err
    assert cli_main([]) == 2


def test_cli_main_serves_stdin_until_it_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "create", "arguments": {"path": "/memories/notes/a.md", "file_text": "hello"}}},
    ]
    stdin = io.TextIOWrapper(io.BytesIO(b"".join(json.dumps(r).encode() + b"\n" for r in requests)))
    stdout = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert cli_main(["--vault", str(tmp_path / "v" / "ada")]) == 0
    replies = [json.loads(line) for line in stdout.buffer.getvalue().splitlines()]  # type: ignore[attr-defined]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["isError"] is False
    assert len(list((tmp_path / "v" / "ada" / "notes").glob("a-*.md"))) == 1


def test_the_void_mcp_process_speaks_only_jsonrpc_on_stdout(tmp_path: Path) -> None:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "create", "arguments": {"path": "/memories/notes/a.md", "file_text": "hello\nworld"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "view", "arguments": {"path": "/memories/../x"}}},
    ]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.run(
        [sys.executable, "-m", "void.cli", "mcp", "--vault", str(tmp_path / "vault" / "ada")],
        input=b"".join(json.dumps(r).encode() + b"\n" for r in requests), capture_output=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    replies = [json.loads(line) for line in proc.stdout.decode("ascii").splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4]
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert len(replies[1]["result"]["tools"]) == 6
    assert replies[2]["result"]["isError"] is False and replies[3]["result"]["isError"] is True
    assert b"serving" in proc.stderr
