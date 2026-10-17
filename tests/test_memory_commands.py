"""The six memory commands over an agent's vault and the kernel-authoritative index (MEMORY_EVOLUTION §5.4, §5.9).

Unit tests drive :class:`MemoryCommands` over a real :class:`MemoryStore` with a fake policy store (the policy
write path is another work package); the integration test runs a scripted simulation and applies commands to a
living agent's vault, so the per-tick ``remember`` / ``revise_self`` / gossip writes and the command writes share
one manifest. Property tests attack provenance forgery through agent-supplied text.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from void.config import MaintenanceConfig, MemoryConfig, MemoryPolicyConfig
from void.db import Database
from void.ids import IdFactory
from void.memory.commands import (
    INDEX_DIR,
    NOTES_DIR,
    POLICY_PATH,
    ROOT,
    SELF_PATH,
    CommandResult,
    MemoryCommand,
    MemoryCommands,
    content_hash,
    title_from_name,
    virtual_path,
)
from void.memory.notes import CHANNELS, OPERATOR_CHANNEL, parse_note, render_note
from void.memory.store import OPERATOR_TAG, MemoryStore

ALL_RIGHTS = ["notes", "policy", "structure"]
AGENT = "ag_1"
NO_MUTATION = {"population": {"tier_mutation_prob": 0.0}}


class FakePolicy:
    """The read/write surface :class:`MemoryCommands` needs from the policy store, kept in memory."""

    def __init__(self, text: str | None = None, **cfg: Any) -> None:
        self.cfg = MemoryPolicyConfig(enabled=True, **cfg)
        self.versions: list[tuple[str, str, int, str]] = [] if text is None else [(AGENT, text, 0, "seed")]

    def text(self, agent_id: str) -> str | None:
        mine = [v for v in self.versions if v[0] == agent_id]
        return mine[-1][1] if mine else None

    def set(self, agent_id: str, text: str, tick: int, *, source: str, source_agent_id: str | None = None) -> None:
        if len(text) > self.cfg.max_chars:
            raise ValueError("policy too long")
        self.versions.append((agent_id, text, int(tick), source))

    def edits_today(self, agent_id: str, tick: int) -> int:
        return sum(1 for v in self.versions if v[0] == agent_id and v[3] == "self_edit")


class Harness:
    def __init__(self, root: Path, *, policy: FakePolicy | None = None, maintenance: dict[str, Any] | None = None,
                 memory: dict[str, Any] | None = None) -> None:
        self.db = Database(root / "world.db")
        self.ids = IdFactory("run")
        self.store = MemoryStore(self.db, MemoryConfig(**(memory or {})), self.ids, root / "vaults")
        self.policy = policy
        self.cfg = MaintenanceConfig(**(maintenance or {}))
        self.commands = MemoryCommands(self.db, self.store, self.cfg, self.ids, policy=policy)

    def apply(self, tick: int = 5, *, rights: list[str] | None = None, agent_id: str = AGENT, **cmd: Any) -> CommandResult:
        return self.commands.apply(agent_id, MemoryCommand(**cmd), tick, purpose="maintain",
                                   rights=ALL_RIGHTS if rights is None else rights)  # type: ignore[arg-type]

    def ok(self, tick: int = 5, **cmd: Any) -> CommandResult:
        res = self.apply(tick, **cmd)
        assert res.ok, res.message
        return res

    def refused(self, tick: int = 5, *, rights: list[str] | None = None, **cmd: Any) -> str:
        res = self.apply(tick, rights=rights, **cmd)
        assert not res.ok, res.message
        return res.message

    def file_text(self, note_id: str) -> str:
        note = self.store.get_note(note_id)
        assert note is not None and note.path
        return Path(note.path).read_text(encoding="utf-8")

    def row(self, note_id: str) -> dict[str, Any]:
        r = self.db.fetchone("SELECT * FROM notes WHERE note_id=?", (note_id,))
        assert r is not None
        return dict(r)

    def manifest(self, path: str | Path) -> dict[str, Any] | None:
        r = self.store.manifest_for(path)
        return None if r is None else dict(r)

    def audits(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.fetchall("SELECT * FROM memory_commands ORDER BY cmd_id")]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path, policy=FakePolicy("# How I remember\n- keep one note per node\n"))


def _assert_manifest_matches(h: Harness) -> None:
    """Every live note file has a manifest row holding exactly its current hash (the kernel wrote it last)."""
    for n in h.store.list_notes(AGENT):
        row = h.manifest(n.path)
        assert row is not None and row["written_by"] == "kernel" and row["note_id"] == n.note_id, n.path
        assert row["content_hash"] == content_hash(Path(n.path).read_text(encoding="utf-8")), n.path


# --- helpers ----------------------------------------------------------------------------------------------
def test_title_from_name_and_virtual_path(h: Harness) -> None:
    assert title_from_name("Node_n3-plan.md") == "Node n3 plan" and title_from_name("  a   b .md") == "a b"
    assert title_from_name(".md") == "untitled"
    plain = h.store.remember(AGENT, 1, "x", title="Plain", tags=["agent"])
    index = h.store.remember(AGENT, 1, "x", title="Idx", tags=["agent", "index"])
    assert virtual_path(plain) == f"{NOTES_DIR}/plain-{plain.note_id[-6:]}.md"
    assert virtual_path(index) == f"{INDEX_DIR}/idx-{index.note_id[-6:]}.md"


# --- view -------------------------------------------------------------------------------------------------
def test_view_root_is_a_two_level_listing_with_sizes_and_ticks(h: Harness) -> None:
    a = h.store.remember(AGENT, 3, "Foraged at [[Node n3]] and got little", title="Thin node", auto_link=False)
    h.store.remember(AGENT, 4, "- [[Node n3]]: thin", title="Nodes", tags=["agent", "index"], auto_link=False)
    h.store.revise_self(AGENT, 2, "I am Ada.")
    res = h.ok(command="view", path=ROOT)
    lines = res.message.split("\n")
    assert lines[0] == f"{ROOT}/" and lines[1] == "  self.md  (9 bytes, tick 2)"
    assert lines[2].startswith("  memory_policy.md  (") and lines[3] == "  notes/"
    assert f"    thin-node-{a.note_id[-6:]}.md  ({len(a.body.encode())} bytes, tick 3, observed)" in lines
    assert "  index/" in lines and any(ln.startswith("    nodes-") and "tick 4" in ln for ln in lines)
    assert res.bytes_delta == 0 and res.result_hash is None
    sub = h.ok(command="view", path=f"{NOTES_DIR}/").message
    assert sub.split("\n")[0] == f"{NOTES_DIR}/" and "thin-node-" in sub and "nodes-" not in sub
    assert h.ok(command="view", path=INDEX_DIR).message.split("\n")[1].startswith("  nodes-")
    # an agent without self.md or notes still gets a listing (empty areas say so)
    empty = h.commands.listing("ag_nobody")
    assert empty == f"{ROOT}/\n  notes/\n    (empty)\n  index/\n    (empty)"


def test_view_file_numbers_lines_and_honours_view_range(h: Harness) -> None:
    note = h.store.remember(AGENT, 3, "one\ntwo\nthree", title="Lines", auto_link=False)
    path = virtual_path(note)
    full = h.ok(command="view", path=path)
    assert full.message == f"Here's the content of {path} with line numbers:\n     1\tone\n     2\ttwo\n     3\tthree"
    assert full.result_hash == content_hash(h.file_text(note.note_id)) and full.note_id == note.note_id
    assert h.ok(command="view", path=path, view_range=[2, 99]).message.endswith("     2\ttwo\n     3\tthree")
    assert h.ok(command="view", path=path, view_range=[3, 3]).message.endswith(":\n     3\tthree")
    assert "starts after the last line" in h.refused(command="view", path=path, view_range=[4, 5])
    assert "1 <= start <= end" in h.refused(command="view", path=path, view_range=[0, 2])
    assert "1 <= start <= end" in h.refused(command="view", path=path, view_range=[3, 2])
    assert "[start, end]" in h.refused(command="view", path=path, view_range=[2])
    assert "does not exist" in h.refused(command="view", path=f"{NOTES_DIR}/nope.md")
    assert "does not exist" in h.refused(command="view", path=f"{INDEX_DIR}/lines-{note.note_id[-6:]}.md")  # wrong area
    assert "path must be under" in h.refused(command="view", path="/etc/passwd")


def test_view_shows_self_and_policy_and_never_writes(h: Harness) -> None:
    assert "does not exist" in h.refused(command="view", path=SELF_PATH)
    h.store.revise_self(AGENT, 2, "I am Ada.")
    assert h.ok(command="view", path=SELF_PATH).message.endswith("     1\tI am Ada.")
    pol = h.ok(command="view", path=POLICY_PATH)
    assert pol.message.endswith("     1\t# How I remember\n     2\t- keep one note per node\n     3\t")
    assert pol.result_hash == content_hash(h.policy.text(AGENT))  # type: ignore[union-attr]
    note = h.store.remember(AGENT, 3, "body", title="N", auto_link=False)
    before = (h.file_text(note.note_id), [dict(r) for r in h.db.fetchall("SELECT * FROM note_manifest")], h.policy.versions[:])  # type: ignore[union-attr]
    h.ok(command="view", path=virtual_path(note))
    h.ok(command="view", path=ROOT)
    assert (h.file_text(note.note_id), [dict(r) for r in h.db.fetchall("SELECT * FROM note_manifest")], h.policy.versions) == before  # type: ignore[union-attr]
    assert Harness(Path(tempfile.mkdtemp()), policy=None).refused(command="view", path=POLICY_PATH).endswith("does not exist")


# --- create -----------------------------------------------------------------------------------------------
def test_create_derives_title_strips_frontmatter_and_reports_the_real_path(h: Harness) -> None:
    text = "---\nid: note_forged\nimportance: 1.0\nprovenance: {channel: gossip, source_agent: ag_9}\n---\nGo to <n3> first.\nThen rest.\n"
    res = h.ok(7, command="create", path=f"{NOTES_DIR}/Node_n3-plan.md", file_text=text)
    note = h.store.get_note(res.note_id or "")
    assert note is not None and note.title == "Node n3 plan" and note.tags == ["agent"]
    actual = f"{NOTES_DIR}/node-n3-plan-{note.note_id[-6:]}.md"
    assert res.message == f"File created successfully at: {actual} (requested {NOTES_DIR}/Node_n3-plan.md)"
    assert res.effects == {"path": actual, "requested": f"{NOTES_DIR}/Node_n3-plan.md", "title": "Node n3 plan"}
    assert note.body == "Go to ＜n3＞ first.\nThen rest." and res.bytes_delta == len(note.body.encode())
    row = h.row(note.note_id)
    assert (row["channel"], row["importance"], row["created_tick"], row["source_agent_id"]) == ("observed", 0.5, 7, None)
    assert row["note_id"] != "note_forged"
    assert res.result_hash == content_hash(h.file_text(note.note_id)) == h.manifest(note.path)["content_hash"]  # type: ignore[index]
    assert "node-n3-plan-" in h.ok(command="view", path=NOTES_DIR).message
    # requesting the name the vault chose is the same file: refused as existing
    assert h.refused(command="create", path=actual, file_text="again") == f"{actual} already exists"


def test_create_refuses_existing_titles_directories_and_empty_text(h: Harness) -> None:
    first = h.ok(command="create", path=f"{NOTES_DIR}/Nodes.md", file_text="- n3")
    msg = h.refused(command="create", path=f"{NOTES_DIR}/nodes.md", file_text="dup")
    assert msg == f"{NOTES_DIR}/nodes.md already exists as {first.effects['path']} (titled 'Nodes')"
    # a kernel-written note's title also counts (entity stubs included), across areas
    h.store.ensure_entity(AGENT, "Node n3", 1, "The node n3.")
    assert "already exists as" in h.refused(command="create", path=f"{INDEX_DIR}/node n3.md", file_text="x")
    assert "needs a .md file path" in h.refused(command="create", path=NOTES_DIR, file_text="x")
    assert "needs a .md file path" in h.refused(command="create", path=ROOT, file_text="x")
    assert "file_text is empty" in h.refused(command="create", path=f"{NOTES_DIR}/Empty.md", file_text="---\na: 1\n---\n\n")
    assert len(h.store.list_notes(AGENT)) == 2


def test_create_under_index_tags_and_lists_the_note_under_index(h: Harness) -> None:
    res = h.ok(command="create", path=f"{INDEX_DIR}/Nodes.md", file_text="- [[Node n3]]: thin")
    note = h.store.get_note(res.note_id or "")
    assert note is not None and note.tags == ["agent", "index"] and res.effects["path"].startswith(INDEX_DIR + "/")
    assert Path(note.path).parent == h.store.vault_for(AGENT).notes_dir  # one physical directory
    assert "nodes-" in h.ok(command="view", path=INDEX_DIR).message and "nodes-" not in h.ok(command="view", path=NOTES_DIR).message
    assert h.store.reindex(AGENT) == 1 and h.store.get_note(note.note_id).tags == ["agent", "index"]  # type: ignore[union-attr]
    assert "does not exist" in h.refused(command="view", path=f"{NOTES_DIR}/{Path(note.path).name}")


def test_limits_on_files_file_bytes_and_vault_bytes(tmp_path: Path) -> None:
    h = Harness(tmp_path, maintenance={"max_files": 2, "max_file_bytes": 40, "max_vault_bytes": 60})
    assert "would be 41 bytes; the limit is 40" in h.refused(command="create", path=f"{NOTES_DIR}/Big.md", file_text="x" * 41)
    a = h.ok(command="create", path=f"{NOTES_DIR}/A.md", file_text="x" * 30)
    assert "vault would be 61 bytes; the limit is 60" in h.refused(command="create", path=f"{NOTES_DIR}/B.md", file_text="y" * 31)
    b = h.ok(command="create", path=f"{NOTES_DIR}/B.md", file_text="y" * 20)
    assert "already holds 2 files" in h.refused(command="create", path=f"{NOTES_DIR}/C.md", file_text="z")
    # edits count the growth against both limits, and shrinking is always fine
    assert "would be 41 bytes" in h.refused(command="insert", path=a.effects["path"], insert_line=1, insert_text="x" * 10)
    assert "vault would be 61 bytes" in h.refused(command="insert", path=b.effects["path"], insert_line=1, insert_text="y" * 10)
    h.ok(command="str_replace", path=a.effects["path"], old_str="x" * 30, new_str="x")
    h.ok(command="insert", path=b.effects["path"], insert_line=1, insert_text="y" * 10)
    # entity stubs occupy file slots too (they are live notes)
    h.store.delete_note(a.note_id or "")
    h.store.ensure_entity(AGENT, "Node n1", 1, "stub")
    assert "already holds 2 files" in h.refused(command="create", path=f"{NOTES_DIR}/D.md", file_text="z")


# --- str_replace and insert ---------------------------------------------------------------------------------
def test_str_replace_edits_the_body_only_and_refreshes_index_and_manifest(h: Harness) -> None:
    other = h.store.remember(AGENT, 1, "The north node is rich", title="North node", auto_link=False)
    note = h.store.remember(AGENT, 2, "Foraged at [[Node n3]] and got little", title="Thin node", importance=0.8,
                            tags=["resources"], auto_link=False)
    before = h.row(note.note_id)
    path = virtual_path(note)
    res = h.ok(9, command="str_replace", path=path, old_str="[[Node n3]]", new_str="[[North node]] <twice>")
    assert res.message == "The memory file has been edited." and res.note_id == note.note_id
    after = h.store.get_note(note.note_id)
    assert after is not None and after.body == "Foraged at [[North node]] ＜twice＞ and got little" and after.links == ["North node"]
    assert res.bytes_delta == len(after.body.encode()) - len(note.body.encode())
    row = h.row(note.note_id)
    for col in ("title", "created_tick", "importance", "tags", "channel", "source_agent_id", "hop", "origin_note_id", "path"):
        assert row[col] == before[col], col
    assert row["embedding"] != before["embedding"]
    assert [r["to_title"] for r in h.db.fetchall("SELECT to_title FROM note_links WHERE from_note_id=?", (note.note_id,))] == ["North node"]
    text = h.file_text(note.note_id)
    parsed = parse_note(text)
    assert parsed.importance == 0.8 and parsed.tags == ["resources"] and parsed.created_tick == 2 and render_note(parsed) == text
    assert h.manifest(note.path) == {"path": note.path, "note_id": note.note_id, "agent_id": AGENT, "content_hash": content_hash(text),
                                     "written_by": "kernel", "tick": 9}
    assert res.result_hash == content_hash(text)
    assert h.store.get_note(other.note_id).body == other.body  # type: ignore[union-attr]
    # the Related section is part of the editable body
    linked = h.store.remember(AGENT, 3, "See more", title="Linked", links_to=["Thin node"], auto_link=False)
    h.ok(command="str_replace", path=virtual_path(linked), old_str="- [[Thin node]]", new_str="- [[North node]]")
    assert h.store.get_note(linked.note_id).links == ["North node"]  # type: ignore[union-attr]
    # new_str may be omitted: the match is removed
    h.ok(command="str_replace", path=path, old_str=" ＜twice＞")
    assert h.store.get_note(note.note_id).body == "Foraged at [[North node]] and got little"  # type: ignore[union-attr]
    assert h.store.reindex(AGENT) == 3 and h.store.last_reindex_report == {"indexed": 3, "operator_edited": 0}


def test_str_replace_refusals_list_every_match_line(h: Harness) -> None:
    note = h.store.remember(AGENT, 1, "aa\nbb aa\ncc\naaa", title="Dup", auto_link=False)
    path = virtual_path(note)
    msg = h.refused(command="str_replace", path=path, old_str="aa", new_str="x")
    assert msg == f"old_str occurs 4 times in {path} (lines 1, 2, 4); include more context so it matches exactly once"
    assert h.refused(command="str_replace", path=path, old_str="zz") == f"old_str was not found in {path}"
    assert "old_str is empty" in h.refused(command="str_replace", path=path, old_str="\x00")
    assert "needs a file path" in h.refused(command="str_replace", path=NOTES_DIR, old_str="aa")
    assert "does not exist" in h.refused(command="str_replace", path=f"{NOTES_DIR}/missing.md", old_str="aa")
    assert "revise_self" in h.refused(command="str_replace", path=SELF_PATH, old_str="aa")
    assert h.store.get_note(note.note_id).body == "aa\nbb aa\ncc\naaa"  # type: ignore[union-attr]


def test_insert_at_a_line_or_refuse(h: Harness) -> None:
    note = h.store.remember(AGENT, 1, "one\ntwo", title="Ins", auto_link=False)
    path = virtual_path(note)
    res = h.ok(command="insert", path=path, insert_line=1, insert_text="between\nlines\n")
    assert res.message == f"The file {path} has been edited." and res.bytes_delta == len("between\nlines\n")
    assert h.store.get_note(note.note_id).body == "one\nbetween\nlines\ntwo"  # type: ignore[union-attr]
    h.ok(command="insert", path=path, insert_line=0, insert_text="top")
    h.ok(command="insert", path=path, insert_line=5, insert_text="bottom")
    assert h.store.get_note(note.note_id).body == "top\none\nbetween\nlines\ntwo\nbottom"  # type: ignore[union-attr]
    assert h.refused(command="insert", path=path, insert_line=7, insert_text="x") == f"insert_line must be between 0 and 6 for {path}"
    with pytest.raises(ValueError):
        MemoryCommand(command="insert", path=path, insert_line=-1, insert_text="x")
    empty = h.ok(command="create", path=f"{NOTES_DIR}/Short.md", file_text="x")
    h.ok(command="str_replace", path=empty.effects["path"], old_str="x", new_str="")
    assert h.ok(command="view", path=empty.effects["path"]).message.endswith("with line numbers:")
    h.ok(command="insert", path=empty.effects["path"], insert_line=0, insert_text="first")
    assert h.store.get_note(empty.note_id or "").body == "first"  # type: ignore[union-attr]


# --- delete -----------------------------------------------------------------------------------------------
def test_delete_archives_the_note_and_removes_file_links_and_manifest(h: Harness) -> None:
    target = h.store.remember(AGENT, 1, "Gone soon [[Keeper]]", title="Doomed", auto_link=False)
    keeper = h.store.remember(AGENT, 2, "Stays, links [[Doomed]]", title="Keeper", auto_link=False)
    path, disk = virtual_path(target), Path(target.path)
    res = h.ok(command="delete", path=path)
    assert res.message == f"Successfully deleted {path}" and res.bytes_delta == -len(target.body.encode())
    assert res.result_hash is None and res.note_id == target.note_id
    assert not disk.exists() and h.manifest(disk) is None
    assert h.row(target.note_id)["archived"] == 1 and h.store.list_notes(AGENT) == [h.store.get_note(keeper.note_id)]
    assert h.db.fetchall("SELECT * FROM note_links WHERE from_note_id=?", (target.note_id,)) == []
    assert [r["to_title"] for r in h.db.fetchall("SELECT to_title FROM note_links WHERE from_note_id=?", (keeper.note_id,))] == ["Doomed"]
    archived = h.store.get_note(target.note_id)
    assert archived is not None and archived.archived and archived.body == "" and archived.title == "Doomed"
    assert [n.note_id for n in h.store.list_notes(AGENT, include_archived=True)] == [target.note_id, keeper.note_id]
    assert "does not exist" in h.refused(command="delete", path=path)
    assert "does not exist" in h.refused(command="view", path=path)
    assert h.store.reindex(AGENT) == 1 and h.store.list_notes(AGENT, include_archived=True)[0].note_id == keeper.note_id


def test_delete_refuses_policy_self_directories_root_and_kernel_anchors(h: Harness) -> None:
    h.store.remember(AGENT, 1, "Foraged at [[Node n3]]", title="Anchor me", auto_link=False)
    entity = h.store.ensure_entity(AGENT, "Node n3", 1, "stub")
    planted = h.store.plant_probe(AGENT, 2)
    assert planted is not None
    probe = h.store.get_note(planted[0])
    assert probe is not None
    assert h.refused(command="delete", path=POLICY_PATH) == "the memory policy cannot be deleted"
    assert "revise_self" in h.refused(command="delete", path=SELF_PATH)
    assert h.refused(command="delete", path=ROOT) == "the root cannot be deleted"
    assert "is a directory" in h.refused(command="delete", path=NOTES_DIR)
    assert h.refused(command="delete", path=virtual_path(entity)) == f"{virtual_path(entity)} is a kernel entity anchor and cannot be deleted"
    assert h.refused(command="delete", path=virtual_path(probe)) == f"{virtual_path(probe)} is a kernel probe and cannot be deleted"
    assert "cannot be renamed" in h.refused(command="rename", old_path=virtual_path(entity), new_path=f"{NOTES_DIR}/Other.md")
    assert len(h.store.list_notes(AGENT)) == 4
    # the body of an anchor may still be edited
    h.ok(command="insert", path=virtual_path(entity), insert_line=1, insert_text="rich on day 2")
    assert h.store.get_note(entity.note_id).body == "stub\nrich on day 2"  # type: ignore[union-attr]


# --- rename -----------------------------------------------------------------------------------------------
def test_rename_follows_the_new_name_and_leaves_other_links_alone(h: Harness) -> None:
    note = h.store.remember(AGENT, 1, "Foraged at the eastern node", title="Thin node", importance=0.7, auto_link=False)
    linker = h.store.remember(AGENT, 2, "See [[Thin node]]", title="Linker", auto_link=False)
    old_virtual, old_disk = virtual_path(note), Path(note.path)
    res = h.ok(8, command="rename", old_path=old_virtual, new_path=f"{NOTES_DIR}/Eastern_node notes.md")
    renamed = h.store.get_note(note.note_id)
    assert renamed is not None and renamed.title == "Eastern node notes" and renamed.body == note.body
    actual = f"{NOTES_DIR}/eastern-node-notes-{note.note_id[-6:]}.md"
    assert res.message == f"Successfully renamed {old_virtual} to {actual} (requested {NOTES_DIR}/Eastern_node notes.md)"
    assert res.note_id == note.note_id and res.effects["title"] == "Eastern node notes" and res.bytes_delta == 0
    assert not old_disk.exists() and h.manifest(old_disk) is None
    assert Path(renamed.path).name == f"eastern-node-notes-{note.note_id[-6:]}.md"
    assert h.manifest(renamed.path)["content_hash"] == content_hash(h.file_text(note.note_id)) == res.result_hash  # type: ignore[index]
    row = h.row(note.note_id)
    assert (row["title"], row["importance"], row["created_tick"], row["channel"]) == ("Eastern node notes", 0.7, 1, "observed")
    # links other notes hold to the old title are left as they are (they dangle now)
    assert [r["to_title"] for r in h.db.fetchall("SELECT to_title FROM note_links WHERE from_note_id=?", (linker.note_id,))] == ["Thin node"]
    assert h.store.get_note(linker.note_id).links == ["Thin node"]  # type: ignore[union-attr]
    assert "does not exist" in h.refused(command="view", path=old_virtual)
    assert h.ok(command="view", path=actual).message.endswith("     1\tForaged at the eastern node")
    assert h.store.reindex(AGENT) == 2 and h.store.last_reindex_report["operator_edited"] == 0
    assert h.store.get_note(note.note_id).title == "Eastern node notes"  # type: ignore[union-attr]


def test_rename_between_areas_toggles_the_index_tag(h: Harness) -> None:
    res = h.ok(command="create", path=f"{NOTES_DIR}/Nodes.md", file_text="- n3")
    moved = h.ok(command="rename", old_path=res.effects["path"], new_path=f"{INDEX_DIR}/Nodes.md")
    note = h.store.get_note(res.note_id or "")
    assert note is not None and note.tags == ["agent", "index"] and moved.effects["path"].startswith(INDEX_DIR + "/")
    assert note.title == "Nodes" and Path(note.path).name == f"nodes-{note.note_id[-6:]}.md"  # same title keeps the canonical name
    back = h.ok(command="rename", old_path=moved.effects["path"], new_path=f"{NOTES_DIR}/Node list.md")
    assert h.store.get_note(note.note_id).tags == ["agent"] and back.effects["path"].startswith(NOTES_DIR + "/")  # type: ignore[union-attr]


def test_rename_refusals(h: Harness) -> None:
    a = h.ok(command="create", path=f"{NOTES_DIR}/A.md", file_text="a")
    b = h.ok(command="create", path=f"{NOTES_DIR}/B.md", file_text="b")
    assert h.refused(command="rename", old_path=a.effects["path"], new_path=b.effects["path"]) == f"{b.effects['path']} already exists"
    assert "already exists as" in h.refused(command="rename", old_path=a.effects["path"], new_path=f"{INDEX_DIR}/b.md")
    assert "does not exist" in h.refused(command="rename", old_path=f"{NOTES_DIR}/missing.md", new_path=f"{NOTES_DIR}/C.md")
    assert "cannot be renamed" in h.refused(command="rename", old_path=POLICY_PATH, new_path=f"{NOTES_DIR}/C.md")
    assert "revise_self" in h.refused(command="rename", old_path=SELF_PATH, new_path=f"{NOTES_DIR}/C.md")
    assert "needs a note" in h.refused(command="rename", old_path=NOTES_DIR, new_path=f"{NOTES_DIR}/C.md")
    assert "new_path must be" in h.refused(command="rename", old_path=a.effects["path"], new_path=POLICY_PATH)
    assert "new_path must be" in h.refused(command="rename", old_path=a.effects["path"], new_path=INDEX_DIR)
    assert "may not contain" in h.refused(command="rename", old_path=a.effects["path"], new_path=f"{NOTES_DIR}/%2e%2e.md")
    assert "are the same" in h.refused(command="rename", old_path=a.effects["path"], new_path=a.effects["path"])
    assert {n.title for n in h.store.list_notes(AGENT)} == {"A", "B"}


# --- rights, policy delegation --------------------------------------------------------------------------------
def test_rights_gate_every_write_but_never_view(h: Harness) -> None:
    note = h.store.remember(AGENT, 1, "body", title="N", auto_link=False)
    path = virtual_path(note)
    for rights in ([], ["notes"], ["policy"], ["structure"]):
        assert h.apply(rights=rights, command="view", path=path).ok
        assert h.apply(rights=rights, command="view", path=ROOT).ok
    assert "right to create notes" in h.refused(rights=["structure"], command="create", path=f"{NOTES_DIR}/X.md", file_text="x")
    assert "structure right" in h.refused(rights=["notes"], command="create", path=f"{INDEX_DIR}/X.md", file_text="x")
    assert "restructure" in h.refused(rights=["notes", "policy"], command="str_replace", path=path, old_str="body", new_str="x")
    assert "restructure" in h.refused(rights=["notes"], command="insert", path=path, insert_line=0, insert_text="x")
    assert "restructure" in h.refused(rights=["notes"], command="delete", path=path)
    assert "restructure" in h.refused(rights=["notes"], command="rename", old_path=path, new_path=f"{NOTES_DIR}/Y.md")
    assert "right to edit your memory policy" in h.refused(rights=["notes", "structure"], command="str_replace", path=POLICY_PATH, old_str="keep", new_str="x")
    assert "revise_self" in h.refused(rights=ALL_RIGHTS, command="insert", path=SELF_PATH, insert_line=0, insert_text="x")
    assert "revise_self" in h.refused(rights=ALL_RIGHTS, command="create", path=SELF_PATH, file_text="x")
    assert h.apply(rights=["notes"], command="create", path=f"{NOTES_DIR}/X.md", file_text="x").ok
    assert h.apply(rights=["structure"], command="create", path=f"{INDEX_DIR}/Y.md", file_text="y").ok
    assert h.apply(rights=["policy"], command="str_replace", path=POLICY_PATH, old_str="keep", new_str="prune").ok
    assert h.store.get_note(note.note_id).body == "body"  # type: ignore[union-attr]
    # a refused command still reports the current hash of the file it named
    res = h.apply(rights=[], command="delete", path=path)
    assert not res.ok and res.result_hash == content_hash(h.file_text(note.note_id))


def test_policy_edits_delegate_to_the_policy_store(tmp_path: Path) -> None:
    pol = FakePolicy(max_chars=100, max_edits_per_day=2)
    h = Harness(tmp_path, policy=pol)
    assert "does not exist" in h.refused(command="str_replace", path=POLICY_PATH, old_str="a", new_str="b")
    assert "does not exist" in h.refused(command="insert", path=POLICY_PATH, insert_line=0, insert_text="x")
    res = h.ok(3, command="create", path=POLICY_PATH, file_text="---\nversion: 9\n---\n# Mine\n- keep one note per node\n")
    assert res.message == f"File created successfully at: {POLICY_PATH}" and res.note_id is None
    assert pol.versions[-1] == (AGENT, "# Mine\n- keep one note per node", 3, "self_edit") and res.result_hash == content_hash(pol.text(AGENT))  # type: ignore[arg-type]
    assert res.bytes_delta == len(b"# Mine\n- keep one note per node")
    assert "already exists" in h.refused(command="create", path=POLICY_PATH, file_text="again")
    h.ok(4, command="insert", path=POLICY_PATH, insert_line=2, insert_text="- delete wrong notes")
    assert pol.text(AGENT) == "# Mine\n- keep one note per node\n- delete wrong notes"
    assert "used today's 2 policy edits" in h.refused(5, command="str_replace", path=POLICY_PATH, old_str="Mine", new_str="Ours")
    pol.versions = [v for v in pol.versions if v[3] != "self_edit"] + [(AGENT, pol.text(AGENT) or "", 5, "seed")]
    assert "the limit is 100" in h.refused(6, command="insert", path=POLICY_PATH, insert_line=0, insert_text="x" * 100)
    assert "old_str occurs 2 times" in h.refused(6, command="str_replace", path=POLICY_PATH, old_str="note", new_str="x")
    assert len(pol.versions) == 1
    off = Harness(Path(tempfile.mkdtemp()), policy=None)
    assert "does not exist" in off.refused(command="str_replace", path=POLICY_PATH, old_str="a", new_str="b")
    assert "no memory policy" in off.refused(command="create", path=POLICY_PATH, file_text="x")


# --- audit and manifest ---------------------------------------------------------------------------------------
def test_every_command_ok_or_refused_is_audited(h: Harness) -> None:
    res = h.ok(3, command="create", path=f"{NOTES_DIR}/A.md", file_text="alpha")
    path = res.effects["path"]
    h.ok(4, command="view", path=path)
    h.refused(5, command="str_replace", path=path, old_str="zzz")
    h.ok(6, command="insert", path=path, insert_line=1, insert_text="beta")
    h.refused(7, rights=["notes"], command="delete", path=path)
    h.refused(8, command="create", path="/memories/../x.md", file_text="x")
    h.ok(9, command="rename", old_path=path, new_path=f"{NOTES_DIR}/B.md")
    h.ok(10, command="delete", path=f"{NOTES_DIR}/b-{res.note_id[-6:]}.md")  # type: ignore[index]
    rows = h.audits()
    assert [(r["command"], r["ok"], r["tick"]) for r in rows] == [
        ("create", 1, 3), ("view", 1, 4), ("str_replace", 0, 5), ("insert", 1, 6), ("delete", 0, 7), ("create", 0, 8),
        ("rename", 1, 9), ("delete", 1, 10),
    ]
    assert all(r["agent_id"] == AGENT and r["purpose"] == "maintain" and r["call_id"] is None for r in rows)
    assert json.loads(rows[0]["args"]) == {"path": f"{NOTES_DIR}/A.md", "file_text": "alpha"}
    assert rows[0]["bytes_delta"] == 5 and rows[3]["bytes_delta"] == 5 and rows[7]["bytes_delta"] == -10
    assert rows[0]["result_hash"] == rows[1]["result_hash"] == rows[2]["result_hash"] and rows[0]["result_hash"].startswith("blake2b:")
    assert rows[3]["result_hash"] == rows[4]["result_hash"] != rows[0]["result_hash"]
    assert rows[5]["result_hash"] is None and rows[7]["result_hash"] is None
    assert rows[2]["message"] == f"old_str was not found in {path}" and rows[5]["message"].startswith("path may not contain")
    assert all(r["cmd_id"].startswith("mc_") for r in rows)
    # call ids thread through when the caller has one
    h.commands.apply(AGENT, MemoryCommand(command="view", path=ROOT), 11, purpose="recall", rights=[], call_id="call_x")
    last = h.audits()[-1]
    assert (last["purpose"], last["call_id"], last["command"]) == ("recall", "call_x", "view")


def test_manifest_is_written_on_every_kernel_write(h: Harness) -> None:
    a = h.ok(1, command="create", path=f"{NOTES_DIR}/Eastern node thin.md", file_text="eastern node thin little food")
    b = h.ok(2, command="create", path=f"{NOTES_DIR}/Eastern again.md", file_text="eastern node thin little food again")
    assert h.store.get_note(b.note_id or "").links  # auto-linked, so A's file was rewritten with a backlink  # type: ignore[union-attr]
    _assert_manifest_matches(h)
    assert h.manifest(h.store.get_note(a.note_id or "").path)["tick"] == 2  # type: ignore[index, union-attr]
    h.ok(3, command="str_replace", path=a.effects["path"], old_str="little", new_str="no")
    _assert_manifest_matches(h)
    moved = h.ok(4, command="rename", old_path=a.effects["path"], new_path=f"{NOTES_DIR}/Renamed.md")
    _assert_manifest_matches(h)
    assert {r["path"] for r in h.db.fetchall("SELECT path FROM note_manifest")} == {n.path for n in h.store.list_notes(AGENT)}
    h.ok(5, command="delete", path=moved.effects["path"])
    _assert_manifest_matches(h)
    assert len(h.db.fetchall("SELECT path FROM note_manifest")) == 1
    h.store.revise_self(AGENT, 6, "I am Ada.")
    self_path = h.store.vault_for(AGENT).self_path
    assert h.manifest(self_path) == {"path": str(self_path), "note_id": None, "agent_id": AGENT, "written_by": "kernel", "tick": 6,
                                     "content_hash": content_hash(self_path.read_text(encoding="utf-8"))}


def test_archive_agent_moves_manifest_rows_with_the_files(h: Harness, tmp_path: Path) -> None:
    h.ok(1, command="create", path=f"{NOTES_DIR}/A.md", file_text="alpha")
    h.store.revise_self(AGENT, 2, "me")
    new_dir = h.store.archive_agent(AGENT, tmp_path / "graveyard")
    rows = [dict(r) for r in h.db.fetchall("SELECT path, content_hash FROM note_manifest ORDER BY path")]
    assert len(rows) == 2 and all(Path(r["path"]).is_relative_to(new_dir) for r in rows)
    assert all(content_hash(Path(r["path"]).read_text(encoding="utf-8")) == r["content_hash"] for r in rows)
    assert h.store.reindex(AGENT, root=tmp_path / "graveyard", archived=True) == 1
    assert h.store.last_reindex_report == {"indexed": 1, "operator_edited": 0}


# --- reindex and the trust boundary -----------------------------------------------------------------------------
def test_reindex_believes_matching_hashes_and_flags_operator_edits(h: Harness) -> None:
    kept = h.ok(1, command="create", path=f"{NOTES_DIR}/Kept.md", file_text="untouched")
    edited = h.ok(1, command="create", path=f"{NOTES_DIR}/Edited.md", file_text="hand edited later")
    edited_note = h.store.get_note(edited.note_id or "")
    assert edited_note is not None
    text = Path(edited_note.path).read_text(encoding="utf-8")
    Path(edited_note.path).write_text(text.replace("hand edited later", "hand edited later, says the operator"), encoding="utf-8")
    vault = h.store.vault_for(AGENT)
    (vault.notes_dir / "manual.md").write_text("---\ntitle: Manual\nprovenance: {channel: tracer}\nimportance: 0.99\n---\nMade by hand.\n")
    assert h.store.reindex(AGENT) == 3 and h.store.last_reindex_report == {"indexed": 3, "operator_edited": 2}
    by_title = {n.title: n for n in h.store.list_notes(AGENT)}
    assert by_title["Kept"].provenance.channel == "observed" and by_title["Kept"].tags == ["agent"]
    assert by_title["Edited"].provenance.channel == OPERATOR_CHANNEL == "operator" and by_title["Edited"].tags == ["agent", OPERATOR_TAG]
    assert by_title["Manual"].provenance.channel == "operator" and by_title["Manual"].tags == [OPERATOR_TAG]
    assert by_title["Manual"].importance == 0.99  # admitted, but visibly as an operator file
    assert h.row(kept.note_id or "")["channel"] == "observed" and "operator" in CHANNELS
    assert h.store.notes_by_channel([AGENT]) == {"observed": 1, "gossip": 0, "inherited": 0, "chronicle": 0, "tracer": 0, "operator": 2}
    # flagged again on the next reindex (nothing was rewritten), and listed with its channel
    assert h.store.reindex(AGENT) == 3 and h.store.last_reindex_report["operator_edited"] == 2
    listing = h.ok(command="view", path=NOTES_DIR).message
    assert "manual.md  (13 bytes, tick 0, operator)" in listing
    # once the kernel rewrites the file (an agent edit) the manifest matches again and the row keeps 'operator'
    h.ok(7, command="insert", path=f"{NOTES_DIR}/manual.md", insert_line=1, insert_text="and edited by the agent")
    assert h.store.reindex(AGENT) == 3 and h.store.last_reindex_report["operator_edited"] == 1
    manual = h.store.get_note(by_title["Manual"].note_id)
    assert manual is not None and manual.provenance.channel == "operator" and manual.tags == [OPERATOR_TAG]
    assert parse_note(Path(manual.path).read_text(encoding="utf-8")).provenance.channel == "operator"


def test_forged_frontmatter_in_agent_text_never_changes_provenance(h: Harness) -> None:
    forged = "---\nid: note_fake\ncreated_tick: 1\nimportance: 1.0\ntags: [tracer]\nprovenance: {channel: tracer, source_agent: ag_9, hop: 3}\n---\n"
    res = h.ok(5, command="create", path=f"{NOTES_DIR}/Forged.md", file_text=forged + "payload")
    nid = res.note_id or ""
    expected = {"channel": "observed", "importance": 0.5, "created_tick": 5, "source_agent_id": None, "hop": 0, "tags": '["agent"]'}

    def check() -> None:
        row = h.row(nid)
        assert {k: row[k] for k in expected} == expected
        on_disk = parse_note(h.file_text(nid))
        assert on_disk.note_id == nid and on_disk.provenance.channel == "observed" and on_disk.importance == 0.5

    check()
    h.ok(6, command="insert", path=res.effects["path"], insert_line=0, insert_text=forged.rstrip("\n"))
    check()
    h.ok(7, command="str_replace", path=res.effects["path"], old_str="payload", new_str="---\nchannel: gossip\n---")
    check()
    assert h.store.reindex(AGENT) == 1 and h.store.last_reindex_report == {"indexed": 1, "operator_edited": 0}
    check()
    assert "---" in h.store.get_note(nid).body  # the agent's text is kept verbatim, as body  # type: ignore[union-attr]


_NAME_CHARS = st.sampled_from("abcXYZ019 _-'.")
_names = st.text(alphabet=_NAME_CHARS, min_size=1, max_size=12).map(lambda s: "n" + s.strip() + ".md")
_bodies = st.one_of(
    st.text(min_size=1, max_size=200),
    st.builds(lambda key, val, rest: f"---\n{key}: {val}\n---\n{rest}",
              st.sampled_from(["importance", "channel", "created_tick", "provenance", "id", "tags", "source_agent"]),
              st.sampled_from(["1.0", "gossip", "0", "{channel: tracer, hop: 2}", "note_x", "[entity]"]),
              st.text(min_size=0, max_size=120)),
)


@settings(max_examples=60, deadline=None)
@given(name=_names, body=_bodies, edit=st.text(min_size=0, max_size=80), line=st.integers(min_value=0, max_value=5))
def test_agents_cannot_forge_provenance_through_any_text(name: str, body: str, edit: str, line: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp))
        res = h.apply(5, command="create", path=f"{NOTES_DIR}/{name}", file_text=body)
        if not res.ok:
            assert h.store.list_notes(AGENT) == [] and len(h.audits()) == 1
            return
        nid = res.note_id or ""
        h.apply(6, command="insert", path=res.effects["path"], insert_line=line, insert_text=edit)
        h.apply(7, command="str_replace", path=res.effects["path"], old_str=body[:3], new_str=edit)
        for _ in range(2):
            row = h.row(nid)
            assert (row["channel"], row["importance"], row["created_tick"], row["source_agent_id"], row["hop"]) == ("observed", 0.5, 5, None, 0)
            assert json.loads(row["tags"]) == ["agent"] and row["archived"] == 0
            text = h.file_text(nid)
            parsed = parse_note(text)
            assert parsed.note_id == nid and parsed.provenance.channel == "observed" and parsed.importance == 0.5
            assert render_note(parsed) == text and h.manifest(row["path"])["content_hash"] == content_hash(text)  # type: ignore[index]
            assert h.store.reindex(AGENT) == 1 and h.store.last_reindex_report == {"indexed": 1, "operator_edited": 0}


# --- integration: a scripted run, then commands over a living agent's vault -------------------------------------
@pytest.fixture(scope="module")
def run(run_sim):
    return run_sim("memory_commands_run", {**NO_MUTATION, "run": {"days": 2, "ticks_per_day": 12}})


def test_run_writes_a_manifest_row_for_every_kernel_file(run) -> None:
    rows = run.db.fetchall("SELECT note_id, agent_id, path FROM notes WHERE archived=0")
    assert rows
    for r in rows:
        m = run.sim.memory.manifest_for(r["path"])
        assert m is not None and m["written_by"] == "kernel" and m["note_id"] == r["note_id"] and m["agent_id"] == r["agent_id"]
        assert m["content_hash"] == content_hash(Path(r["path"]).read_text(encoding="utf-8")), r["path"]
    for rec in run.sim.registry.alive():
        self_path = run.sim.memory.vault_for(rec.agent_id).self_path
        m = run.sim.memory.manifest_for(self_path)
        assert m is not None and m["content_hash"] == content_hash(self_path.read_text(encoding="utf-8"))
        assert run.sim.memory.reindex(rec.agent_id) == len(run.sim.memory.list_notes(rec.agent_id))
        assert run.sim.memory.last_reindex_report["operator_edited"] == 0
    assert run.db.fetchone("SELECT COUNT(*) AS n FROM note_manifest WHERE written_by != 'kernel'")["n"] == 0


def test_commands_over_a_living_agents_vault_in_a_real_run(run) -> None:
    sim = run.sim
    agent = sorted(sim.registry.alive(), key=lambda r: r.agent_id)[0].agent_id
    tick = run.reports[-1].tick + 1
    cmds = sim.commands

    def apply(**cmd: Any) -> CommandResult:
        return cmds.apply(agent, MemoryCommand(**cmd), tick, purpose="maintain", rights=ALL_RIGHTS)  # type: ignore[arg-type]

    before = len(sim.memory.list_notes(agent))
    listing = apply(command="view", path=ROOT)
    assert listing.ok and listing.message.startswith(f"{ROOT}/\n  self.md  (") and "  notes/\n" in listing.message
    created = apply(command="create", path=f"{INDEX_DIR}/Nodes.md", file_text="---\nprovenance: {channel: gossip}\n---\n- [[Node n1]]: seen on day 1")
    assert created.ok and created.effects["path"].startswith(INDEX_DIR + "/")
    note = sim.memory.get_note(created.note_id or "")
    assert note is not None and note.tags == ["agent", "index"] and note.provenance.channel == "observed"
    edited = apply(command="str_replace", path=created.effects["path"], old_str="seen on day 1", new_str="seen on day 1, thin")
    assert edited.ok and edited.result_hash == sim.memory.manifest_for(note.path)["content_hash"]
    refused = apply(command="delete", path=POLICY_PATH)
    assert not refused.ok
    stub = next(n for n in sim.memory.list_notes(agent) if "entity" in n.tags)
    assert not apply(command="delete", path=virtual_path(stub)).ok
    assert len(sim.memory.list_notes(agent)) == before + 1
    audits = run.db.fetchall("SELECT command, ok FROM memory_commands WHERE agent_id=? AND purpose='maintain' ORDER BY cmd_id", (agent,))
    assert [(r["command"], r["ok"]) for r in audits] == [("view", 1), ("create", 1), ("str_replace", 1), ("delete", 0), ("delete", 0)]
    # every vault file still round-trips byte for byte and the manifest still matches every live note
    for n in sim.memory.list_notes(agent):
        text = Path(n.path).read_text(encoding="utf-8")
        assert render_note(parse_note(text, agent_id=agent, path=n.path)) == text
        assert sim.memory.manifest_for(n.path)["content_hash"] == content_hash(text)
    # a hand edit in the vault is admitted on reindex as an operator file, nothing else moves
    victim = next(n for n in sim.memory.list_notes(agent) if n.provenance.channel == "observed" and "entity" not in n.tags)
    Path(victim.path).write_text(Path(victim.path).read_text(encoding="utf-8").replace("channel: observed", "channel: tracer"), encoding="utf-8")
    n_live = len(sim.memory.list_notes(agent))
    assert sim.memory.reindex(agent) == n_live and sim.memory.last_reindex_report == {"indexed": n_live, "operator_edited": 1}
    flagged = sim.memory.get_note(victim.note_id)
    assert flagged is not None and flagged.provenance.channel == "operator" and OPERATOR_TAG in flagged.tags
    assert all(n.provenance.channel != "operator" for n in sim.memory.list_notes(agent) if n.note_id != victim.note_id)
