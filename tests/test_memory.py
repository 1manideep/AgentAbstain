"""Tests for the memory subsystem: note format, vault, wikilink graph and MemoryStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from void.config import MemoryConfig
from void.db import Database
from void.ids import IdFactory
from void.memory.embed import tokenize
from void.memory.graph import NoteGraph
from void.memory.notes import (
    Note,
    Provenance,
    add_related_links,
    content_body,
    normalise_title,
    parse_note,
    parse_wikilinks,
    render_note,
)
from void.memory.store import MemoryStore
from void.memory.vault import Vault


def make_store(tmp_path: Path, name: str = "run", run_id: str = "run", **cfg: object) -> MemoryStore:
    root = tmp_path / name
    db = Database(root / "world.db")
    return MemoryStore(db, MemoryConfig(**cfg), IdFactory(run_id), root / "vaults")


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return make_store(tmp_path)


# --- notes.py -------------------------------------------------------------------------------


def test_render_note_matches_design_format() -> None:
    note = Note(
        note_id="note_00000001abcdef",
        agent_id="ag_1",
        title="The eastern node is thin",
        body="Foraged at [[Node east]] and got little. [[Bao]] said the north node is better.",
        created_tick=137,
        importance=0.7,
        tags=["resources"],
        provenance=Provenance(origin_generation=0),
    )
    assert render_note(note) == (
        "---\n"
        "id: note_00000001abcdef\n"
        "title: The eastern node is thin\n"
        "created_tick: 137\n"
        "importance: 0.7\n"
        "tags: [resources]\n"
        "provenance: {source_agent: null, hop: 0, origin_note: null, origin_generation: 0, "
        "channel: observed, transfer_tick: null, path: []}\n"
        "---\n"
        "Foraged at [[Node east]] and got little. [[Bao]] said the north node is better.\n"
    )


@pytest.mark.parametrize(
    "body",
    ["", "one line", "trailing newline\n", "two trailing\n\n", "a\n---\nb", "---\nstarts like frontmatter", "  spaced  "],
)
def test_parse_render_round_trip_is_exact(body: str) -> None:
    note = Note(
        note_id="note_1",
        agent_id="ag_1",
        title="yes: [[odd]] 'title' #1",
        body=body,
        created_tick=5,
        importance=0.25,
        tags=["a", "b c"],
        provenance=Provenance(source_agent_id="ag_9", hop=2, origin_note_id="note_0", origin_generation=3),
    )
    back = parse_note(render_note(note), agent_id="ag_1", path="/x/y.md")
    assert back.body == body
    assert (back.note_id, back.title, back.created_tick, back.importance, back.tags) == (
        "note_1",
        "yes: [[odd]] 'title' #1",
        5,
        0.25,
        ["a", "b c"],
    )
    assert back.provenance == note.provenance
    assert back.path == "/x/y.md" and back.agent_id == "ag_1"
    assert render_note(back) == render_note(note)


def test_parse_note_tolerates_missing_keys() -> None:
    note = parse_note("---\ntitle: Bare\n---\nbody text", agent_id="ag_1")
    assert note.title == "Bare" and note.body == "body text"
    assert note.created_tick == 0 and note.importance == 0.5 and note.tags == []
    assert note.provenance == Provenance()
    assert note.note_id.startswith("note_")
    plain = parse_note("no frontmatter at all")
    assert plain.body == "no frontmatter at all" and plain.title == "untitled"


def test_parse_wikilinks_handles_aliases_and_dedupes() -> None:
    body = "See [[Alpha]] and [[Beta|the second]], then [[Alpha]] again, [[ Gamma ]] and [[]]."
    assert parse_wikilinks(body) == ["Alpha", "Beta", "Gamma"]
    assert parse_wikilinks("no links here") == []


# --- vault.py -------------------------------------------------------------------------------


def test_vault_write_read_self_and_no_overwrite(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vaults", "ag_1")
    assert vault.read_self() == ("", 0)
    vault.write_self("I forage east.", 3, 40)
    assert vault.read_self() == ("I forage east.", 3)

    first = Note("note_00000001aaaaaa", "ag_1", "Same title", "one", 1)
    second = Note("note_00000002aaaaaa", "ag_1", "Same title", "two", 2)
    p1 = vault.write_note(first)
    p2 = vault.write_note(second)
    assert p1.name == "same-title-aaaaaa.md" and p2.name == "same-title-aaaaaa-2.md"
    assert vault.read_note(p1).body == "one" and vault.read_note(p2).body == "two"
    # Rewriting the same note reuses its file.
    first.body = "one, revised"
    assert vault.write_note(first) == p1 and vault.read_note(p1).body == "one, revised"
    # Iteration order is by file name (stable across runs), not by note id.
    assert [n.note_id for n in vault.iter_notes()] == [second.note_id, first.note_id]


# --- graph.py -------------------------------------------------------------------------------


def _note(nid: str, title: str, body: str) -> Note:
    return Note(nid, "ag", title, body, 0, links=parse_wikilinks(body))


def test_graph_resolution_backlinks_and_walk() -> None:
    notes = [
        _note("a", "Alpha", "goes to [[beta]] and [[Missing]]"),
        _note("b", "Beta", "goes to [[  GAMMA ]] and back to [[Alpha|first]]"),
        _note("c", "Gamma", "dead end"),
        _note("d", "Delta", "links [[Gamma]]"),
    ]
    g = NoteGraph(notes)
    assert g.links_out("a") == ["b"] and g.dangling("a") == ["Missing"]
    assert g.links_out("b") == ["c", "a"]
    assert sorted(g.backlinks("c")) == ["b", "d"]
    assert g.resolve("gamma") == "c" and g.resolve("nope") is None

    scores = g.walk({"a": 1.0}, hops=2, decay=0.5)
    assert scores == {"a": 1.0, "b": 0.5, "c": 0.25}
    assert g.walk({"a": 1.0}, hops=0, decay=0.5) == {"a": 1.0}
    # The best score per note wins when two entry points reach it.
    merged = g.walk({"a": 1.0, "d": 0.2}, hops=2, decay=0.5)
    assert merged["c"] == 0.25 and merged["d"] == 0.2
    assert g.walk({"unknown": 1.0}, hops=2, decay=0.5) == {}


# --- store.py -------------------------------------------------------------------------------


def test_remember_writes_file_and_rows(store: MemoryStore) -> None:
    note = store.remember(
        "ag_1",
        10,
        "Foraged at the eastern node and got little food",
        links_to=["Bao advice"],
        tags=["resources", "resources", " food "],
    )
    assert note.title == "Foraged at the eastern node and"
    assert note.tags == ["resources", "food"]
    assert note.links == ["Bao advice"]
    path = Path(note.path)
    assert path == store.vault_root / "ag_1" / "notes" / f"foraged-at-the-eastern-node-and-{note.note_id[-6:]}.md"
    on_disk = parse_note(path.read_text(), agent_id="ag_1")
    assert on_disk.note_id == note.note_id
    assert on_disk.body.endswith("\n\n## Related\n- [[Bao advice]]")
    assert on_disk.links == ["Bao advice"]

    row = store.db.fetchone("SELECT * FROM notes WHERE note_id=?", (note.note_id,))
    assert row is not None
    assert row["agent_id"] == "ag_1" and row["path"] == str(path) and row["created_tick"] == 10
    assert row["tags"] == '["resources", "food"]' and row["hop"] == 0 and row["archived"] == 0
    assert len(row["embedding"]) == store.cfg.embedding_dim * 4
    links = store.db.fetchall("SELECT to_title FROM note_links WHERE from_note_id=?", (note.note_id,))
    assert [r["to_title"] for r in links] == ["Bao advice"]

    long = store.remember("ag_1", 11, "x" * 2000)
    assert len(long.body) == store.cfg.note_max_chars
    # A link already in the body is not repeated in a Related line.
    inline = store.remember("ag_1", 12, "Met [[Bao]] today", links_to=["bao"])
    assert "## Related" not in inline.body and inline.links == ["Bao"]


def _chain(store: MemoryStore, agent_id: str = "ag_1") -> tuple[Note, Note, Note]:
    a = store.remember(agent_id, 10, "Foraged at the eastern node and got little food", links_to=["Bao advice"])
    b = store.remember(agent_id, 11, "Bao said the north node is better than the east", title="Bao advice",
                       links_to=["North node"])
    c = store.remember(agent_id, 12, "Rich stock by the river, plentiful water and shade", title="North node")
    return a, b, c


def test_graph_mode_reaches_two_hop_neighbour_vector_mode_does_not(tmp_path: Path) -> None:
    graph_store = make_store(tmp_path, "graph", entry_k=1, hops=2, mode="graph")
    vector_store = make_store(tmp_path, "vector", entry_k=1, hops=2, mode="vector")
    query = "eastern node foraging little food"
    a, b, c = _chain(graph_store)
    got = graph_store.retrieve("ag_1", query, 20)
    assert [r.note_id for r in got] == [a.note_id, b.note_id, c.note_id]
    assert got[0].score > got[1].score > got[2].score > 0
    assert all(r.hop == 0 and r.source_agent_id is None for r in got)
    assert got[2].body == c.body and got[2].title == "North node"

    va, _, _ = _chain(vector_store)
    vgot = vector_store.retrieve("ag_1", query, 20)
    assert [r.note_id for r in vgot] == [va.note_id]


def test_retrieve_respects_k_and_ignores_other_agents(store: MemoryStore) -> None:
    _chain(store, "ag_1")
    store.remember("ag_2", 10, "Foraged at the eastern node and got little food")
    got = store.retrieve("ag_1", "eastern node foraging", 20, k=2)
    assert len(got) == 2
    assert store.retrieve("ag_3", "eastern node foraging", 20) == []
    assert all(store.get_note(r.note_id).agent_id == "ag_1" for r in got)


def test_recency_and_importance_affect_ranking(tmp_path: Path) -> None:
    text = "the eastern node is thin today"
    s = make_store(tmp_path, "recency", mode="vector")
    old = s.remember("ag_1", 0, text, title="old")
    new = s.remember("ag_1", 200, text, title="new")
    got = s.retrieve("ag_1", text, 200)
    assert [r.note_id for r in got] == [new.note_id, old.note_id]
    assert got[0].score > got[1].score
    # Even a very old note keeps at least the recency floor.
    ancient = s.retrieve("ag_1", text, 100_000)
    assert ancient[1].score >= 0.05 * 0.5 * 0.5 * 0.9

    s2 = make_store(tmp_path, "importance", mode="vector")
    dull = s2.remember("ag_1", 5, text, title="dull", importance=0.1)
    vital = s2.remember("ag_1", 5, text, title="vital", importance=0.9)
    assert [r.note_id for r in s2.retrieve("ag_1", text, 5)] == [vital.note_id, dull.note_id]


def test_revise_self_versioning_and_word_cap(store: MemoryStore) -> None:
    long_text = " ".join(f"w{i}" for i in range(100))
    version, summary = store.revise_self("ag_1", 3, long_text)
    assert version == 1 and summary.split() == [f"w{i}" for i in range(store.cfg.self_max_words)]
    assert store.get_self("ag_1") == summary
    version2, summary2 = store.revise_self("ag_1", 9, "I trade with Bao.")
    assert version2 == 2 and summary2 == "I trade with Bao."
    assert store.vault_for("ag_1").read_self() == ("I trade with Bao.", 2)
    assert store.self_history("ag_1") == [
        {"version": 1, "tick": 3, "summary": summary},
        {"version": 2, "tick": 9, "summary": "I trade with Bao."},
    ]
    assert store.get_self("ag_nobody") == ""


def test_copy_note_keeps_provenance_and_changes_nothing_else(store: MemoryStore) -> None:
    src = store.remember("ag_1", 4, "Foraged at [[Node east]] and got little", tags=["resources"], importance=0.8)
    store.revise_self("ag_1", 4, "me")
    before_src = store.db.fetchone("SELECT * FROM notes WHERE note_id=?", (src.note_id,))
    prov = Provenance(source_agent_id="ag_1", hop=1, origin_note_id=src.note_id, origin_generation=2)
    copy = store.copy_note(src, "ag_2", 7, body="East node gives little, says [[Node east]] visitor", provenance=prov)
    assert copy.note_id != src.note_id and copy.agent_id == "ag_2"
    assert copy.provenance == prov and copy.title == src.title and copy.tags == ["resources"]
    assert copy.importance == 0.8 and copy.created_tick == 7
    assert Path(copy.path).is_relative_to(store.vault_root / "ag_2")
    row = store.db.fetchone("SELECT * FROM notes WHERE note_id=?", (copy.note_id,))
    assert (row["source_agent_id"], row["hop"], row["origin_note_id"], row["origin_generation"]) == (
        "ag_1", 1, src.note_id, 2,
    )
    # The source row, the source file and the self version history are untouched.
    assert tuple(store.db.fetchone("SELECT * FROM notes WHERE note_id=?", (src.note_id,))) == tuple(before_src)
    assert store.get_note(src.note_id).body == src.body
    assert [h["version"] for h in store.self_history("ag_1")] == [1]
    assert store.self_history("ag_2") == []
    # Inheritance-style copy: importance override and an extra tag, provenance stored verbatim.
    inherited = store.copy_note(src, "ag_3", 8, provenance=Provenance(origin_generation=0), importance=0.3,
                                tags=[*src.tags, "inherited"])
    assert inherited.provenance.hop == 0 and inherited.importance == 0.3 and inherited.tags == ["resources", "inherited"]
    assert inherited.body == src.body


def test_archive_agent_moves_files_and_hides_notes(store: MemoryStore, tmp_path: Path) -> None:
    a, b, c = _chain(store)
    store.revise_self("ag_1", 12, "I was here.")
    old_dir = store.vault_root / "ag_1"
    graveyard = tmp_path / "graveyard"
    new_dir = store.archive_agent("ag_1", graveyard)
    assert new_dir == graveyard / "ag_1" and new_dir.is_dir() and not old_dir.exists()
    assert (new_dir / "self.md").is_file()
    assert len(list((new_dir / "notes").glob("*.md"))) == 3

    assert store.retrieve("ag_1", "eastern node foraging", 20) == []
    assert store.list_notes("ag_1") == []
    archived = store.list_notes("ag_1", include_archived=True)
    assert [n.note_id for n in archived] == [a.note_id, b.note_id, c.note_id]
    assert all(n.archived and Path(n.path).is_relative_to(new_dir) for n in archived)
    assert store.get_note(a.note_id).body == a.body
    assert store.top_notes("ag_1", 3) == []
    assert store.get_self("ag_1") == "I was here."


def test_top_notes_orders_by_importance_then_recency(store: MemoryStore) -> None:
    low = store.remember("ag_1", 1, "low", importance=0.2)
    mid_old = store.remember("ag_1", 1, "mid old", importance=0.5)
    mid_new = store.remember("ag_1", 9, "mid new", importance=0.5)
    high = store.remember("ag_1", 2, "high", importance=0.9)
    assert [n.note_id for n in store.top_notes("ag_1", 3)] == [high.note_id, mid_new.note_id, mid_old.note_id]
    assert [n.note_id for n in store.top_notes("ag_1", 10)][-1] == low.note_id


def test_reindex_reproduces_rows(store: MemoryStore) -> None:
    _chain(store)
    store.copy_note(store.list_notes("ag_1")[0], "ag_1", 13,
                    provenance=Provenance("ag_2", 1, "note_x", 4), importance=0.7)
    store.remember("ag_2", 1, "untouched other agent")

    def snapshot() -> tuple[list[tuple], list[tuple]]:
        notes = [tuple(r) for r in store.db.fetchall("SELECT * FROM notes ORDER BY note_id")]
        links = [tuple(r) for r in store.db.fetchall("SELECT * FROM note_links ORDER BY from_note_id, to_title")]
        return notes, links

    before = snapshot()
    store.db.execute("DELETE FROM note_links")
    store.db.execute("UPDATE notes SET importance=0, embedding=x'00' WHERE agent_id='ag_1'")
    assert snapshot() != before
    assert store.reindex("ag_1") == 4
    assert snapshot() == before


def test_reindex_indexes_hand_written_files(store: MemoryStore) -> None:
    vault = store.vault_for("ag_1")
    vault.ensure()
    (vault.notes_dir / "manual.md").write_text("---\ntitle: Manual\n---\nWritten by hand, see [[Other]].\n")
    assert store.reindex("ag_1") == 1
    notes = store.list_notes("ag_1")
    assert len(notes) == 1 and notes[0].title == "Manual" and notes[0].links == ["Other"]
    assert store.retrieve("ag_1", "written by hand", 5)[0].note_id == notes[0].note_id


def test_similarity_is_cosine_of_embeddings(store: MemoryStore) -> None:
    assert store.similarity("the eastern node is thin", "the eastern node is thin") == pytest.approx(1.0)
    assert store.similarity("eastern node thin", "eastern node thin food") > store.similarity(
        "eastern node thin", "Bao proposed a beacon gadget"
    )


def test_determinism_same_inputs_same_output(tmp_path: Path) -> None:
    def build(name: str) -> list[tuple[str, float, str]]:
        s = make_store(tmp_path, name, run_id="same-run")
        _chain(s)
        s.remember("ag_1", 13, "Bao proposed a beacon gadget near the river", links_to=["North node"], importance=0.7)
        s.remember("ag_1", 14, "Weather turned foul, foraging yields dropped", tags=["weather"])
        s.copy_note(s.list_notes("ag_1")[0], "ag_1", 15, provenance=Provenance("ag_2", 1, "note_z", 1))
        return [(r.note_id, r.score, r.title) for r in s.retrieve("ag_1", "river node foraging food", 20)]

    first, second = build("one"), build("two")
    assert first == second and len(first) >= 4


# --- v2: provenance channels --------------------------------------------------------------


def test_provenance_v2_round_trip_and_tolerant_parse() -> None:
    prov = Provenance("ag_1", 2, "tracer", 3, "gossip", 40, ["ag_1", "ag_2"])
    text = render_note(Note("note_1", "ag_2", "T", "b", 1, provenance=prov))
    assert (
        "provenance: {source_agent: ag_1, hop: 2, origin_note: tracer, origin_generation: 3, "
        "channel: gossip, transfer_tick: 40, path: [ag_1, ag_2]}\n"
    ) in text
    assert parse_note(text).provenance == prov
    old = parse_note("---\ntitle: T\nprovenance: {source_agent_id: ag_9, hop: 1}\n---\nb")
    assert old.provenance == Provenance(source_agent_id="ag_9", hop=1)
    alt = parse_note("---\ntitle: T\nprovenance: {channel: bogus, path_agents: [a, b], transfer_tick: 7}\n---\nb")
    assert (alt.provenance.channel, alt.provenance.path_agents, alt.provenance.transfer_tick) == ("observed", ["a", "b"], 7)


def test_channel_columns_written_by_remember_copy_and_reindex(store: MemoryStore) -> None:
    def cols(note_id: str) -> tuple:
        r = store.db.fetchone("SELECT channel, transfer_tick, path_agents FROM notes WHERE note_id=?", (note_id,))
        return (r["channel"], r["transfer_tick"], r["path_agents"])

    chron = store.remember("ag_1", 5, "The chronicle said Bao got rich", channel="chronicle")
    assert chron.provenance.channel == "chronicle" and cols(chron.note_id) == ("chronicle", None, "[]")
    over = store.remember("ag_1", 6, "override", channel="chronicle",
                          provenance=Provenance(channel="inherited", path_agents=["ag_0"]))
    assert over.provenance.channel == "inherited" and cols(over.note_id) == ("inherited", None, '["ag_0"]')
    prov = Provenance("ag_1", 1, chron.note_id, None, "gossip", 9, ["ag_1"])
    copy = store.copy_note(chron, "ag_2", 9, provenance=prov)
    assert copy.provenance == prov and cols(copy.note_id) == ("gossip", 9, '["ag_1"]')
    assert parse_note(Path(copy.path).read_text()).provenance == prov
    with pytest.raises(ValueError):
        store.remember("ag_1", 7, "bad", channel="telepathy")
    before = [tuple(r) for r in store.db.fetchall("SELECT * FROM notes WHERE agent_id='ag_2'")]
    assert store.reindex("ag_2") == 1
    assert [tuple(r) for r in store.db.fetchall("SELECT * FROM notes WHERE agent_id='ag_2'")] == before


# --- v2: related sections, unique titles, auto-linking -----------------------------------


def test_add_related_links_and_content_body() -> None:
    body, added = add_related_links("Body text.", ["A", "B"])
    assert body == "Body text.\n\n## Related\n- [[A]]\n- [[B]]" and added == ["A", "B"]
    body2, added2 = add_related_links(body, ["a", "C"])
    assert added2 == ["C"] and body2 == body + "\n- [[C]]"
    assert content_body(body2) == "Body text."
    mid = "Text\n\n## Related\n- [[A]]\n\n## Notes\nmore"
    out, added3 = add_related_links(mid, ["B"])
    assert added3 == ["B"] and out == "Text\n\n## Related\n- [[A]]\n- [[B]]\n\n## Notes\nmore"
    assert content_body(out) == "Text\n\n## Notes\nmore"
    assert add_related_links("", ["X"]) == ("## Related\n- [[X]]", ["X"])
    assert add_related_links("See [[X]]", ["x"]) == ("See [[X]]", [])


def test_titles_are_unique_per_vault(store: MemoryStore) -> None:
    a = store.remember("ag_1", 1, "same", title="Same Title")
    b = store.remember("ag_1", 2, "same again", title="same title")
    c = store.remember("ag_1", 3, "third", title="SAME TITLE")
    assert (a.title, b.title, c.title) == ("Same Title", "same title-2", "SAME TITLE-3")
    assert store.remember("ag_2", 1, "x", title="Same Title").title == "Same Title"


_TOPICS = [
    "eastern node thin little food",
    "north node rich stock river",
    "Bao trades food for water at the river",
    "weather storm reduced forage yield at eastern node",
    "eastern node recovering food stock",
]


def test_auto_link_builds_bidirectional_related_sections(tmp_path: Path) -> None:
    s = make_store(tmp_path, "auto")
    for i in range(20):
        s.remember("ag_1", i, f"{_TOPICS[i % 5]} variation {i}")
    rows = [(str(r["from_note_id"]), str(r["to_title"])) for r in s.db.fetchall("SELECT * FROM note_links")]
    assert len(rows) >= 20
    notes = s.list_notes("ag_1")
    by_id = {n.note_id: n for n in notes}
    by_title = {normalise_title(n.title): n for n in notes}
    assert len(by_title) == 20
    for from_id, to_title in rows:
        target = by_title[normalise_title(to_title)]
        assert (target.note_id, by_id[from_id].title) in rows  # bidirectional
    for n in notes:
        text = Path(n.path).read_text()
        bullets = [ln for ln in text.splitlines() if ln.startswith("- [[")]
        assert len(bullets) == len(set(bullets))
        assert sorted(parse_wikilinks(text)) == sorted(t for f, t in rows if f == n.note_id)
        assert "## Related" in text or not bullets
    assert s.link_density("ag_1") == pytest.approx(len(rows) / 20) and s.link_density("ag_1") >= 1.0
    assert s.link_density("ag_none") == 0.0
    # Links change files but not embeddings, so the index is still reproducible from the vault.
    before = [tuple(r) for r in s.db.fetchall("SELECT * FROM notes ORDER BY note_id")]
    assert s.reindex("ag_1") == 20
    assert [tuple(r) for r in s.db.fetchall("SELECT * FROM notes ORDER BY note_id")] == before
    assert sorted(rows) == sorted((str(r[0]), str(r[1])) for r in s.db.fetchall("SELECT * FROM note_links"))


def test_auto_link_reaches_note_in_graph_mode_only(tmp_path: Path) -> None:
    query = "Foraged at the eastern node and got little food"
    results: dict[str, list[str]] = {}
    for mode in ("graph", "vector"):
        s = make_store(tmp_path, mode, entry_k=1, mode=mode)
        a = s.remember("ag_1", 1, query)
        b = s.remember("ag_1", 2, "The eastern node gave little food again today")
        assert b.links == [a.title] and s.get_note(a.note_id).links == [b.title]  # auto-linked both ways
        results[mode] = [r.note_id for r in s.retrieve("ag_1", query, 3)]
        results[mode + "_ids"] = [a.note_id, b.note_id]
    assert results["graph"] == results["graph_ids"]
    assert results["vector"] == results["vector_ids"][:1]


def test_auto_link_can_be_disabled(tmp_path: Path) -> None:
    s = make_store(tmp_path, "off", auto_link_k=0)
    a = s.remember("ag_1", 1, "eastern node thin little food")
    b = s.remember("ag_1", 2, "eastern node thin little food again")
    assert a.links == [] and b.links == [] and s.link_density("ag_1") == 0.0


# --- v2: entity stubs ---------------------------------------------------------------------


def test_ensure_entity_is_anchor_only(tmp_path: Path) -> None:
    s = make_store(tmp_path, "entity", entry_k=1)
    e = s.ensure_entity("ag_1", "Node East", 1, "The eastern resource node.")
    assert e.tags == ["entity"] and e.title == "Node East"
    assert s.ensure_entity("ag_1", "  node   EAST ", 2, "other").note_id == e.note_id
    assert len(s.list_notes("ag_1")) == 1
    m = s.remember("ag_1", 3, "Foraged at [[Node East]] and got little food")
    assert m.links == ["Node East"]
    g = NoteGraph(s.list_notes("ag_1"), hub_tags=("entity",))
    assert g.links_out(m.note_id) == [e.note_id] and g.backlinks(e.note_id) == [m.note_id]
    # Never returned, even as the best match; never an auto-link candidate.
    got = [r.note_id for r in s.retrieve("ag_1", "Node East eastern resource node", 5)]
    assert e.note_id not in got
    twin = s.remember("ag_1", 4, "The eastern resource node.")
    assert twin.links == [] and "## Related" not in Path(e.path).read_text()
    # Walked through: two notes that mention the same entity reach each other.
    other = s.remember("ag_1", 5, "Bao camps beside [[Node East]] selling water", auto_link=False)
    got, stats = s.retrieve_with_stats("ag_1", "Foraged at Node East and got little food", 6)
    assert stats["entry_ids"] == [m.note_id]
    assert [r.note_id for r in got] == [m.note_id, other.note_id] and stats["hop_hist"] == {0: 1, 1: 0, 2: 1}


# --- v2: tracer lineage -------------------------------------------------------------------


def test_tracer_lineage(store: MemoryStore, tmp_path: Path) -> None:
    t = store.plant_tracer("ag_1", 5, "The richest node", "Prefer node 3; it is the richest.")
    assert t.tags == ["strategy", "tracer"] and t.importance == 0.95
    assert t.provenance == Provenance(channel="tracer", origin_note_id="tracer")
    row = store.db.fetchone("SELECT channel, origin_note_id, hop FROM notes WHERE note_id=?", (t.note_id,))
    assert (row["channel"], row["origin_note_id"], row["hop"]) == ("tracer", "tracer", 0)
    c2 = store.copy_note(t, "ag_2", 9, body="Node 3 is the richest, go there.",
                         provenance=Provenance("ag_1", 1, "tracer", None, "gossip", 9, ["ag_1"]))
    c3 = store.copy_note(c2, "ag_3", 12, provenance=Provenance("ag_2", 2, "tracer", None, "gossip", 12, ["ag_1", "ag_2"]))
    late = store.copy_note(t, "ag_3", 14, provenance=Provenance("ag_1", 0, "tracer", 1, "inherited", 14, ["ag_1"]))
    assert late.title == "The richest node-2" and c3.provenance.path_agents == ["ag_1", "ag_2"]
    assert store.holders_of("tracer") == {"ag_1", "ag_2", "ag_3"}
    assert store.first_arrival("ag_1", "tracer") == (5, "tracer", 0)
    assert store.first_arrival("ag_3", "tracer") == (12, "gossip", 2)
    assert store.first_arrival("ag_9", "tracer") is None
    assert [n.note_id for n in store.descendants_of("tracer")] == [t.note_id, c2.note_id, c3.note_id, late.note_id]
    direct = store.copy_note(t, "ag_4", 20, provenance=Provenance("ag_1", 1, t.note_id, None, "gossip", 20, ["ag_1"]))
    assert store.holders_of(t.note_id) == {"ag_1", "ag_4"} and store.descendants_of(t.note_id)[-1].note_id == direct.note_id
    store.archive_agent("ag_2", tmp_path / "graveyard")
    assert store.holders_of("tracer") == {"ag_1", "ag_3"}
    assert store.holders_of("tracer", include_archived=True) == {"ag_1", "ag_2", "ag_3"}
    assert store.first_arrival("ag_2", "tracer") == (9, "gossip", 1)
    assert store.descendants_of("tracer")[1].archived is True


# --- v2: retrieval stats and metrics ------------------------------------------------------


def test_retrieve_with_stats_and_notes_by_channel(tmp_path: Path) -> None:
    s = make_store(tmp_path, "stats", entry_k=1)
    a, b, c = _chain(s)
    s.copy_note(c, "ag_1", 20, body="Water and shade by the river, stock is rich",
                provenance=Provenance("ag_2", 1, c.note_id, None, "gossip", 20, ["ag_2"]))
    query = "eastern node foraging little food"
    got, stats = s.retrieve_with_stats("ag_1", query, 30)
    assert [r.note_id for r in got] == [a.note_id, b.note_id, c.note_id]
    assert stats["entry_ids"] == [a.note_id]
    assert stats["hop_hist"] == {0: 1, 1: 1, 2: 1}
    assert stats["channel_mix"] == {"observed": 3}
    assert stats["mean_age_ticks"] == pytest.approx((20 + 19 + 18) / 3)
    assert s.retrieve("ag_1", query, 30) == got
    assert s.notes_by_channel(["ag_1"]) == {"observed": 3, "gossip": 1, "inherited": 0, "chronicle": 0, "tracer": 0}
    assert s.notes_by_channel([]) == {c: 0 for c in ("observed", "gossip", "inherited", "chronicle", "tracer")}
    empty, estats = s.retrieve_with_stats("ag_none", query, 30)
    assert empty == [] and estats == {"hop_hist": {0: 0, 1: 0, 2: 0}, "mean_age_ticks": 0.0, "channel_mix": {}, "entry_ids": []}
    s.cfg = MemoryConfig(entry_k=2, mode="vector")
    vgot, vstats = s.retrieve_with_stats("ag_1", query, 30)
    assert vstats["hop_hist"] == {0: len(vgot)} and len(vgot) == 2 and vstats["entry_ids"] == [r.note_id for r in vgot]


def test_graph_hub_traversal_and_hops() -> None:
    notes = [
        _note("a", "Alpha", "mentions [[Bao]]"),
        _note("b", "Beta", "also [[Bao]]"),
        Note("e", "ag", "Bao", "entity", 0, tags=["entity"]),
    ]
    plain = NoteGraph(notes)
    assert plain.walk({"a": 1.0}, 2, 0.5) == {"a": 1.0, "e": 0.5}
    hub = NoteGraph(notes, hub_tags=("entity",))
    assert hub.walk_with_hops({"a": 1.0}, 2, 0.5) == {"a": (1.0, 0), "e": (0.5, 1), "b": (0.25, 2)}
    assert hub.links_out("e") == [] and sorted(hub.backlinks("e")) == ["a", "b"] and hub.is_hub("e")


# --- v2: probe ----------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "expected"), [("graph", True), ("vector", False)])
def test_probe_recall_depends_on_mode(tmp_path: Path, mode: str, expected: bool) -> None:
    s = make_store(tmp_path, "probe_" + mode, entry_k=1, mode=mode)
    assert s.plant_probe("ag_1", 1) is None
    a = s.remember("ag_1", 1, "Foraged at the eastern node and got little food")
    planted = s.plant_probe("ag_1", 2)
    assert planted is not None
    pid, query = planted
    assert query == a.title
    probe = s.get_note(pid)
    assert probe is not None and probe.tags == ["probe"] and probe.provenance.channel == "observed"
    bridge_title = probe.links[0]
    bridge = s._find_by_title("ag_1", bridge_title)
    assert bridge is not None and bridge.links == [a.title, probe.title]
    assert s.get_note(a.note_id).links == [bridge_title]  # backlink written into A's file
    assert not (set(tokenize(content_body(probe.body))) & set(tokenize(a.body)))
    assert s.check_probe("ag_1", pid, query, 3) is expected
    assert s.check_probe("ag_1", pid, query, 3, k=1) is False
    # Probes and bridges are never anchors: a later probe still hangs off A.
    pid2, query2 = s.plant_probe("ag_1", 10)
    assert query2 == a.title and pid2 != pid and s.get_note(a.note_id).links == [bridge_title, "Bridge 10"]
