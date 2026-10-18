"""The loop's memory audit (DESIGN §10, MEMORY_EVOLUTION §5.4 and §7.4), end to end on scripted brains.

* every decision call records what retrieval did in its JSON extras (graph-depth histogram, mean age, channel mix,
  entry points and how many of them are ``index`` notes) next to the titles and gossip hops it always had;
* the per-tick ``remember`` / ``revise_self`` ops go through the command layer: one ``memory_commands`` row each
  (purpose ``tick``), the vault limits enforced, a refusal logged and nothing written;
* a ``[[link]]`` in a per-tick ``remember`` to another agent, a node or a written chronicle day gets its entity stub
  on first mention, so the note's links resolve; gossip copies and inherited notes are not anchored (their stubs
  would draw note ids and move the balances of worlds that do not use the feature);
* long responses and command arguments are stored whole, so the audit rows replay into an empty vault;
* the run records the code commit it ran on.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, REPO, SimRun, deep_merge

from void.brain.decision import MemoryOp
from void.brain.scripted import ScriptedBrain
from void.config import load_config
from void.db import Database
from void.events import Kind
from void.ids import IdFactory
from void.memory.commands import NOTES_DIR, SELF_PATH, MemoryCommand, MemoryCommands
from void.memory.notes import Provenance, normalise_title
from void.memory.store import ENTITY_TAG, INDEX_TAG, MemoryStore, content_hash
from void.sim import loop as loop_module
from void.sim.maintenance import MaintenanceResponse

STATS_KEYS = {"retrieved_titles", "retrieved_hops", "retrieved_hop_hist", "retrieved_mean_age", "retrieved_channel_mix",
              "retrieved_entry_ids", "retrieved_index_entries"}
ALL_RIGHTS = ["notes", "policy", "structure"]


@pytest.fixture(scope="module")
def smoke(run_sim) -> SimRun:
    """Six scripted agents over three days with gossip and the chronicle on (the smoke world's defaults)."""
    return run_sim("audit_smoke", deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 3}}))


def _tick_rows(run: SimRun, command: str | None = None) -> list[Any]:
    sql = "SELECT rowid AS rid, * FROM memory_commands WHERE purpose='tick'" + (" AND command=?" if command else "") + " ORDER BY rowid"
    return run.db.fetchall(sql, (command,) if command else ())


# --- retrieval stats in the call row ------------------------------------------------------------------------
def test_every_decision_call_records_its_retrieval_stats(smoke: SimRun) -> None:
    hops = int(smoke.cfg.memory.hops)
    owned: dict[str, set[str]] = defaultdict(set)
    for r in smoke.db.fetchall("SELECT note_id, agent_id FROM notes"):
        owned[str(r["agent_id"])].add(str(r["note_id"]))
    rows = smoke.db.fetchall("SELECT agent_id, extras FROM llm_calls WHERE purpose='decide'")
    assert rows
    retrieved = deeper = 0
    for r in rows:
        ex = json.loads(r["extras"])
        assert STATS_KEYS <= set(ex), sorted(STATS_KEYS - set(ex))
        n = len(ex["retrieved_titles"])
        hist = ex["retrieved_hop_hist"]
        assert hist == {} or set(hist) == {str(h) for h in range(hops + 1)}, hist  # graph depths 0..hops, string keys
        assert sum(hist.values()) == n == len(ex["retrieved_hops"]) == sum(ex["retrieved_channel_mix"].values())
        assert isinstance(ex["retrieved_mean_age"], int | float) and ex["retrieved_mean_age"] >= 0
        assert set(ex["retrieved_entry_ids"]) <= owned[str(r["agent_id"])]  # entry points are the agent's own notes
        assert ex["retrieved_index_entries"] == 0  # nobody in the smoke world writes an index note
        if n:
            retrieved += 1
            assert ex["retrieved_entry_ids"] and ex["retrieved_mean_age"] >= 0
            deeper += int(any(int(k) > 0 and v > 0 for k, v in hist.items()))
    assert retrieved > 0 and deeper > 0, (retrieved, deeper)


def test_index_entries_count_the_index_notes_among_the_entry_points(make_sim) -> None:
    """Under R1 (the E0 base's ``memory.index_boost``) the index builder's nightly batch writes ``index`` notes from day 2
    on; each decide call counts how many of its entry points are such notes, as ``retrieve_with_stats`` reports it."""
    sim = make_sim("audit_index_entries", deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 2, "ticks_per_day": 12}}),
                   base=REPO / "configs" / "exp_mem_e0_index_builder.yaml")
    assert sim.cfg.memory.index_boost > 0.0
    assert asyncio.run(sim.run()) == "completed"
    index_ids = {str(r["note_id"]) for r in sim.db.fetchall("SELECT note_id, tags FROM notes") if INDEX_TAG in json.loads(r["tags"])}
    assert index_ids
    counted = 0
    for r in sim.db.fetchall("SELECT extras FROM llm_calls WHERE purpose='decide'"):
        ex = json.loads(r["extras"])
        assert ex["retrieved_index_entries"] == sum(1 for nid in ex["retrieved_entry_ids"] if nid in index_ids)
        counted += ex["retrieved_index_entries"]
    assert counted > 0


# --- the per-tick path through the command layer ------------------------------------------------------------
def test_a_per_tick_remember_writes_a_tick_create_row(smoke: SimRun) -> None:
    creates = _tick_rows(smoke, "create")
    remembered = smoke.events(Kind.REMEMBER)
    ok = [r for r in creates if r["ok"]]
    assert ok and len(ok) == len(remembered)
    for row, ev in zip(ok, remembered, strict=True):  # same order: the row is written just before the event
        args = json.loads(row["args"])
        assert (row["tick"], row["agent_id"]) == (ev["tick"], ev["agent_id"])
        note = smoke.sim.memory.get_note(ev["payload"]["note_id"])
        assert note is not None and posixpath.basename(args["path"]) == Path(str(note.path)).name
        assert args["path"].startswith(NOTES_DIR + "/") and args["channel"] == ev["payload"]["channel"]
        assert note.body.startswith(args["file_text"].rstrip())  # the op's text, whole
        assert row["result_hash"].startswith("blake2b:") and row["bytes_delta"] > 0
        assert row["cmd_id"].startswith(f"mc_t{row['tick']:08d}_{row['agent_id']}_")
        call = smoke.db.fetchone("SELECT agent_id, tick, purpose FROM llm_calls WHERE call_id=?", (row["call_id"],))
        assert tuple(call) == (row["agent_id"], row["tick"], "decide")


def test_a_per_tick_revise_self_is_a_str_replace_of_self_md(smoke: SimRun) -> None:
    rows = [r for r in _tick_rows(smoke, "str_replace") if r["ok"]]
    assert rows and len(rows) == len(smoke.events(Kind.REVISE_SELF))
    last: dict[str, Any] = {}
    for r in rows:
        args = json.loads(r["args"])
        assert args["path"] == SELF_PATH and set(args) == {"path", "old_str", "new_str"}
        last[str(r["agent_id"])] = r
    for aid, r in last.items():  # the newest row's hash is the file as it stands (nothing else rewrites self.md)
        path = smoke.sim.memory.vault_for(aid).self_path
        if path.is_file():
            assert r["result_hash"] == content_hash(path.read_text(encoding="utf-8"))


def test_the_tick_audit_draws_no_id_and_twins_agree(run_sim) -> None:
    over = deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 1}})
    a, b = run_sim("audit_twin", over), run_sim("audit_twin", over)
    for sql in ("SELECT * FROM memory_commands ORDER BY rowid", "SELECT call_id, extras, raw_text FROM llm_calls ORDER BY call_id",
                "SELECT note_id, title, created_tick, tags FROM notes ORDER BY note_id"):
        ra, rb = [tuple(r) for r in a.db.fetchall(sql)], [tuple(r) for r in b.db.fetchall(sql)]
        assert ra == rb and ra, sql


def test_the_tick_paths_enforce_the_vault_limits(make_sim) -> None:
    sim = make_sim("audit_limits", deep_merge(NO_PROVIDER_TIERS, {"memory": {"maintenance": {"max_file_bytes": 1000, "max_vault_bytes": 1000}}}))
    aid = sorted(sim.registry.alive(), key=lambda r: r.agent_id)[0].agent_id
    cmds, before_notes = sim.commands, len(sim.memory.list_notes(aid))
    used = sum(e.size for e in cmds._entries(aid))  # the node stubs written at birth
    text = ("I found a quiet place to forage and will come back. " * 20)[: 1000 - used - 40]
    ids = sim.ids.count
    note, refusal = cmds.tick_remember(aid, 1, text, tags=["places"], links_to=["Node n1"])
    assert note is not None and refusal is None
    assert sim.ids.count == ids + 1  # the note's id only: the audit row is not drawn from the run's counter
    note2, refusal2 = cmds.tick_remember(aid, 1, text, title="Again")
    assert note2 is None and refusal2 is not None and "your vault would be" in refusal2
    assert len(sim.memory.list_notes(aid)) == before_notes + 1
    self_before = sim.memory.get_self(aid)
    done, why = cmds.tick_revise_self(aid, 1, "I keep a long and careful account of every node " * 6)
    assert done is None and why is not None and sim.memory.get_self(aid) == self_before
    done, why = cmds.tick_revise_self(aid, 1, "I forage.")
    assert why is None and done is not None and sim.memory.get_self(aid) == "I forage."
    rows = sim.db.fetchall("SELECT command, ok, args, bytes_delta, message FROM memory_commands WHERE agent_id=? ORDER BY rowid", (aid,))
    assert [(r["command"], r["ok"]) for r in rows] == [("create", 1), ("create", 0), ("str_replace", 0), ("str_replace", 1)]
    refused = json.loads(rows[1]["args"])
    assert "path" not in refused and refused["file_text"] == text and refused["title"] == "Again" and rows[1]["bytes_delta"] == 0
    assert rows[3]["bytes_delta"] == len(b"I forage.") - len(self_before.encode())

    full = make_sim("audit_files", deep_merge(NO_PROVIDER_TIERS, {"memory": {"maintenance": {"max_files": 7}}}))
    aid = sorted(full.registry.alive(), key=lambda r: r.agent_id)[0].agent_id
    assert len(full.commands._entries(aid)) == 6  # one stub per node
    assert full.commands.tick_remember(aid, 1, "The first note fits.")[0] is not None
    note, refusal = full.commands.tick_remember(aid, 1, "The second does not.")
    assert note is None and refusal is not None and "already holds 7 files" in refusal


def test_a_full_vault_refuses_further_per_tick_notes_and_logs_it(run_sim) -> None:
    max_files = 7  # the six node stubs and one note
    run = run_sim("audit_full_vault", deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 2}, "memory": {"maintenance": {"max_files": max_files}}}))
    rows = _tick_rows(run)
    refused = [r for r in rows if not r["ok"]]
    assert any(r["command"] == "create" for r in refused)
    assert all("files" in r["message"] for r in refused if r["command"] == "create")
    events = [e for e in run.events(Kind.MEMORY_COMMAND) if e["payload"].get("purpose") == "tick"]
    assert [(e["tick"], e["agent_id"], e["payload"]["command"], e["payload"]["ok"]) for e in events] == \
        [(r["tick"], r["agent_id"], r["command"], False) for r in refused]
    assert len(run.events(Kind.REMEMBER)) == sum(1 for r in rows if r["ok"] and r["command"] == "create")
    for r in refused:  # a refusal names no file and the vault really was full
        assert "path" not in json.loads(r["args"])
        n = run.db.fetchone("SELECT COUNT(*) AS n FROM notes WHERE agent_id=? AND created_tick<=?", (r["agent_id"], r["tick"]))["n"]
        assert n >= max_files
    for r in rows:  # every note the tick path wrote went into a vault with room
        if r["ok"] and r["command"] == "create":
            name = posixpath.basename(json.loads(r["args"])["path"])
            note = next(x for x in run.db.fetchall("SELECT note_id, path FROM notes WHERE agent_id=?", (r["agent_id"],))
                        if Path(str(x["path"])).name == name)
            n = run.db.fetchone("SELECT COUNT(*) AS n FROM notes WHERE agent_id=? AND note_id<?", (r["agent_id"], note["note_id"]))["n"]
            assert n < max_files


def test_the_amnesic_control_keeps_the_direct_path(run_sim) -> None:
    run = run_sim("audit_amnesic", deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 2}, "memory": {"enabled": False}}))
    assert run.db.fetchone("SELECT COUNT(*) AS n FROM memory_commands")["n"] == 0
    assert run.events(Kind.REVISE_SELF) and not run.events(Kind.REMEMBER)
    ex = json.loads(run.db.fetchone("SELECT extras FROM llm_calls WHERE purpose='decide' LIMIT 1")["extras"])
    assert STATS_KEYS <= set(ex) and ex["retrieved_hop_hist"] == {} and ex["retrieved_entry_ids"] == []


# --- entity stubs on first mention --------------------------------------------------------------------------
def test_a_scripted_run_leaves_no_dangling_links_to_agents_nodes_or_chronicles(smoke: SimRun) -> None:
    """A regression guard on the smoke world (whose scripted notes already leave none dangling without the mention
    stubs; the wiring itself is tested by the planted op below). An agent's link to its own name is out of scope:
    nobody keeps a stub of themselves."""
    db = smoke.db
    own = {str(r["agent_id"]): normalise_title(str(r["name"])) for r in db.fetchall("SELECT agent_id, name FROM agents")}
    names = set(own.values())
    written = int(smoke.kv("chronicle_day") or 0)
    assert written >= 2
    chronicles = {normalise_title(f"Chronicle of day {d}") for d in range(1, written + 1)}
    nodes = {normalise_title(f"Node {nid}") for nid in smoke.sim.nodes}
    titles: dict[str, set[str]] = defaultdict(set)
    for r in db.fetchall("SELECT agent_id, title FROM notes WHERE archived=0"):
        titles[str(r["agent_id"])].add(normalise_title(str(r["title"])))
    checked: Counter[str] = Counter()
    dangling: list[tuple[str, str, str]] = []
    for r in db.fetchall("SELECT n.agent_id, n.title, l.to_title FROM note_links l JOIN notes n ON n.note_id=l.from_note_id "
                         "WHERE n.archived=0 ORDER BY n.note_id, l.to_title"):
        key = normalise_title(str(r["to_title"]))
        if key == own.get(str(r["agent_id"])):
            continue
        kind = "agent" if key in names else "chronicle" if key in chronicles else "node" if key in nodes else None
        if kind is None:
            continue
        checked[kind] += 1
        if key not in titles[str(r["agent_id"])]:
            dangling.append((kind, str(r["agent_id"]), str(r["to_title"])))
    assert checked["agent"] > 0 and checked["chronicle"] > 0 and checked["node"] > 0, checked
    assert dangling == []


def test_mention_stubs_name_only_known_entities(make_sim) -> None:
    sim = make_sim("audit_mentions", deep_merge(NO_PROVIDER_TIERS, {"world": {"view_radius": 12.0}}))
    asyncio.run(sim.chronicle.write_day(1, 0))
    alive = sorted(sim.registry.alive(), key=lambda r: r.agent_id)
    me, other = alive[0], alive[1]
    seen = sim.kernel.state.seen_nodes.get(me.agent_id, set())
    unseen = sorted(set(sim.nodes) - set(seen))[0]
    before = {n.title for n in sim.memory.list_notes(me.agent_id)}
    sim._anchor_mentions(me.agent_id, [other.name.lower(), "Chronicle of day 1", "Chronicle of day 9", f"Node {unseen}", "Node n99",
                                       "Nobody", "Self", me.name], 1)
    stubs = {n.title: n for n in sim.memory.list_notes(me.agent_id) if n.title not in before}
    assert set(stubs) == {other.name, "Chronicle of day 1", f"Node {unseen}"}
    assert all(ENTITY_TAG in n.tags for n in stubs.values())
    assert stubs[other.name].body == f"An inhabitant of the void on tier {other.model_tier}."
    assert stubs[f"Node {unseen}"].body == "A resource node."  # under fog an unseen node's stub tells no position
    assert stubs["Chronicle of day 1"].body.startswith("The chronicle of day 1")
    sim._anchor_mentions(me.agent_id, [other.name, "Chronicle of day 1"], 2)  # idempotent
    assert len(sim.memory.list_notes(me.agent_id)) == len(before) + 3

    clear = make_sim("audit_mentions_clear", NO_PROVIDER_TIERS)
    aid = sorted(clear.registry.alive(), key=lambda r: r.agent_id)[0].agent_id
    stub = next(n for n in clear.memory.list_notes(aid) if n.title == "Node n1")
    n = clear.nodes["n1"]
    assert stub.body == f"A resource node at ({n.x:.0f}, {n.y:.0f})."


def test_a_planted_remember_gets_its_mention_stubs_through_the_tick(make_sim, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring end to end: on day 2 one agent's decision carries a ``remember`` op whose links name an agent it has
    never met, the chronicle of day 1 and a node it has never seen. None of the three titles is in its vault when the
    op is planted, so without the call in the loop's per-tick path every one of those links would dangle. After the
    tick each has its stub, written that very tick, and the note's links resolve; its own name and a stranger get none.
    """
    sim = make_sim("audit_planted", deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 2}, "world": {"view_radius": 8.0}}))
    original = ScriptedBrain._memory_ops
    planted: dict[str, Any] = {}

    def plant(self: ScriptedBrain, obs: Any, seed: Any, action: Any, rng: Any) -> list[MemoryOp]:
        ops = original(self, obs, seed, action, rng)
        if planted or obs.day < 2:
            return ops
        vault = {normalise_title(n.title) for n in sim.memory.list_notes(obs.agent_id)}
        strangers = [r.name for r in sorted(sim.registry.alive(), key=lambda r: r.agent_id)
                     if r.agent_id != obs.agent_id and normalise_title(r.name) not in vault]
        unseen = [nid for nid in sorted(sim.nodes) if normalise_title(f"Node {nid}") not in vault]
        if not strangers or not unseen or normalise_title("Chronicle of day 1") in vault:
            return ops
        planted.update(agent_id=obs.agent_id, name=obs.name, tick=obs.tick, stranger=strangers[0], node=unseen[0])
        text = (f"[[{strangers[0]}]] told [[{obs.name}]] what [[Chronicle of day 1]] said about [[Node {unseen[0]}]]; "
                "[[Nobody Here]] answered.")
        return [MemoryOp(op="remember", title="Planted mention", text=text, tags=["people"], links_to=[strangers[0]])] + ops[:1]

    monkeypatch.setattr(ScriptedBrain, "_memory_ops", plant)
    assert asyncio.run(sim.run()) == "completed"
    assert planted, "no agent on day 2 had a stranger, an unseen node and an unread chronicle"
    aid, tick = planted["agent_id"], planted["tick"]
    pair = {aid, next(r.agent_id for r in sim.registry.all() if r.name == planted["stranger"])}
    for e in sim.db.fetchall("SELECT payload FROM events WHERE kind=? AND tick=?", (Kind.TALK, tick)):
        p = json.loads(e["payload"])
        assert {p["speaker_id"], p["listener_id"]} != pair  # no talk stub that tick: the stranger's stub is the mention's

    notes = {normalise_title(n.title): n for n in sim.memory.list_notes(aid, include_archived=True)}
    note = notes[normalise_title("Planted mention")]
    assert note.created_tick == tick
    assert sim.db.fetchone("SELECT COUNT(*) AS n FROM memory_commands WHERE purpose='tick' AND command='create' AND agent_id=? AND tick=? "
                           "AND ok=1 AND json_extract(args, '$.title')='Planted mention'", (aid, tick))["n"] == 1
    for title in (planted["stranger"], "Chronicle of day 1", f"Node {planted['node']}"):
        stub = notes.get(normalise_title(title))
        assert stub is not None, title
        assert ENTITY_TAG in stub.tags and stub.created_tick == tick, (title, stub.tags, stub.created_tick)
    assert notes[normalise_title(f"Node {planted['node']}")].body.startswith("A resource node")
    assert normalise_title(planted["name"]) not in notes and normalise_title("Nobody Here") not in notes
    links = [str(r["to_title"]) for r in sim.db.fetchall("SELECT to_title FROM note_links WHERE from_note_id=?", (note.note_id,))]
    dangling = sorted(t for t in links if normalise_title(t) not in notes)
    assert dangling == sorted([planted["name"], "Nobody Here"]), dangling


def test_only_a_per_tick_remember_makes_mention_stubs(make_sim) -> None:
    """Every agent or chronicle stub a run makes is explained by a talk that tick, a ``remember`` the vault's owner
    wrote that tick linking the title, or an inherited copy, and no vault holds a stub of its own agent. A gossip copy
    makes none. In this world (the E0 no-op arm as the CLI runs seed 5, cut to three days) anchoring gossip copies
    made a 'Chronicle of day 1' stub and a stub of an agent in its own vault; each drew a note id from the run's
    counter, which moved later gadget ids and with them the arm's balances (the E0 rule: they must not move).
    """
    sim = make_sim("audit_e0_stubs", deep_merge(NO_PROVIDER_TIERS, {"run": {"name": "exp_mem_e0_noop", "seed": 5, "days": 3}}),
                   base=REPO / "configs" / "exp_mem_e0_noop.yaml")
    assert asyncio.run(sim.run()) == "completed"
    db = sim.db
    agents = {str(r["agent_id"]): str(r["name"]) for r in db.fetchall("SELECT agent_id, name FROM agents")}
    by_name = {normalise_title(n): aid for aid, n in agents.items()}
    talks: set[tuple[int, frozenset[str]]] = set()
    for e in db.fetchall("SELECT tick, payload FROM events WHERE kind=?", (Kind.TALK,)):
        p = json.loads(e["payload"])
        talks.add((int(e["tick"]), frozenset((p["speaker_id"], p["listener_id"]))))
    mentions: set[tuple[str, int, str]] = set()
    for e in db.fetchall("SELECT tick, payload FROM events WHERE kind=?", (Kind.REMEMBER,)):
        p = json.loads(e["payload"])
        for r in db.fetchall("SELECT to_title FROM note_links WHERE from_note_id=?", (p["note_id"],)):
            mentions.add((str(p["agent_id"]), int(e["tick"]), normalise_title(str(r["to_title"]))))
    # the world's gossip copies do link chronicle days and their listener's own name, so the case is exercised
    gossip = [(str(r["agent_id"]), normalise_title(str(r["to_title"])))
              for r in db.fetchall("SELECT n.agent_id, l.to_title FROM note_links l JOIN notes n ON n.note_id=l.from_note_id "
                                   "WHERE n.channel='gossip'")]
    assert any(key.startswith("chronicle of day ") for _, key in gossip), "no gossip copy links a chronicle day: pick another seed"
    assert any(by_name.get(key) == aid for aid, key in gossip), "no gossip copy links its listener's name: pick another seed"
    checked: Counter[str] = Counter()
    unexplained: list[tuple[str, str, int]] = []
    for r in db.fetchall("SELECT agent_id, title, created_tick, tags FROM notes ORDER BY note_id"):
        tags = json.loads(r["tags"])
        key, aid, tick = normalise_title(str(r["title"])), str(r["agent_id"]), int(r["created_tick"])
        if ENTITY_TAG not in tags or "inherited" in tags or key.startswith("node "):
            continue
        assert by_name.get(key) != aid, (agents[aid], r["title"])  # never a stub of oneself
        checked["agent" if key in by_name else "other"] += 1
        talked = key in by_name and (tick, frozenset((aid, by_name[key]))) in talks
        if not talked and (aid, tick, key) not in mentions:
            unexplained.append((agents[aid], str(r["title"]), tick))
    assert checked["agent"] > 0, checked
    assert unexplained == []


# --- whole text and replay ------------------------------------------------------------------------------------
def test_long_responses_and_arguments_are_stored_whole(run_sim, monkeypatch: pytest.MonkeyPatch) -> None:
    long_a = "Node n1 is rich in the morning and thin by night. " * 50   # 2550 characters
    long_b = "Bao trades fairly; Cyra hoards. " * 80                      # 2560 characters

    def planted(strategy: str, ctx: Any, rng: Any) -> MaintenanceResponse:
        return MaintenanceResponse(reflection="Two long notes.", commands=[
            MemoryCommand(command="create", path=f"{NOTES_DIR}/Long-a.md", file_text=long_a),
            MemoryCommand(command="create", path=f"{NOTES_DIR}/Long-b.md", file_text=long_b)])

    monkeypatch.setattr("void.sim.maintenance.plan", planted)
    run = run_sim("audit_whole", deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 2, "ticks_per_day": 6},
                                                                "memory": {"policy": {"enabled": True}, "maintenance": {"enabled": True}}}))
    calls = run.db.fetchall("SELECT raw_text FROM llm_calls WHERE purpose='maintain'")
    assert calls
    for c in calls:
        assert len(c["raw_text"]) > 4000
        assert MaintenanceResponse.model_validate_json(c["raw_text"]).commands[0].file_text == long_a
    rows = run.db.fetchall("SELECT args FROM memory_commands WHERE purpose='maintain' AND command='create'")
    texts = sorted(json.loads(r["args"])["file_text"] for r in rows)
    assert rows and set(texts) == {long_a, long_b} and all(len(t) > 400 for t in texts)


def _harness(root: Path, name: str) -> tuple[Database, MemoryCommands]:
    cfg = load_config(REPO / "configs" / "scripted_smoke.yaml")
    (root / name).mkdir(parents=True)
    db = Database(root / name / "world.db")
    ids = IdFactory("replay-run")
    store = MemoryStore(db, cfg.memory, ids, root / name / "vaults")
    return db, MemoryCommands(db, store, cfg.memory.maintenance, ids)


def _vault_hashes(cmds: MemoryCommands, agent_id: str) -> dict[str, str]:
    vdir = cmds.store.vault_for(agent_id).dir
    return {str(p.relative_to(vdir)): content_hash(p.read_text(encoding="utf-8")) for p in sorted(vdir.rglob("*.md"))}


def _replay(cmds: MemoryCommands, rows: list[Any]) -> list[str | None]:
    """Apply audit rows in order to ``cmds`` from their stored arguments alone; returns each result hash."""
    out: list[str | None] = []
    for r in rows:
        args = json.loads(r["args"])
        aid, tick = str(r["agent_id"]), int(r["tick"])
        if r["purpose"] == "tick" and r["command"] == "create":
            prov = Provenance.from_mapping(args["provenance"]) if "provenance" in args else None
            cmds.tick_remember(aid, tick, args["file_text"], title=args.get("title"), tags=args["tags"], links_to=args["links_to"],
                               channel=args["channel"], provenance=prov)
        elif r["purpose"] == "tick":
            cmds.tick_revise_self(aid, tick, args["new_str"])
        else:
            cmds.apply(aid, MemoryCommand(command=r["command"], **args), tick, purpose=str(r["purpose"]), rights=ALL_RIGHTS)  # type: ignore[arg-type]
        last = cmds.db.fetchone("SELECT result_hash FROM memory_commands ORDER BY rowid DESC LIMIT 1")
        out.append(last["result_hash"])
    return out


def test_replaying_the_audit_rows_into_an_empty_vault_reproduces_its_files(tmp_path: Path) -> None:
    db, cmds = _harness(tmp_path, "original")
    aid = "ag_00000001aaaaaa"
    long_note = "Ada told me the east field regrows fast after rain; " * 11          # about 570 characters
    big_file = "# Field log\n" + "- [[Node n1]]: rich at dawn, thin at dusk, watched by Bao.\n" * 25    # about 1500 characters
    prov = Provenance(channel="chronicle", origin_note_id="chronicle:day_1")
    assert cmds.tick_remember(aid, 3, long_note, title="East field", tags=["places"], links_to=["Node n1"], channel="chronicle",
                              provenance=prov)[0] is not None
    created = cmds.apply(aid, MemoryCommand(command="create", path=f"{NOTES_DIR}/Field-log.md", file_text=big_file), 4,
                         purpose="maintain", rights=ALL_RIGHTS)
    assert created.ok
    path = str(created.effects["path"])  # the file name the vault chose
    assert cmds.apply(aid, MemoryCommand(command="str_replace", path=path, old_str="# Field log\n",
                                         new_str="# Field log, kept with Cyra, " + "who keeps count of every unit. " * 20 + "\n"),
                      5, purpose="maintain", rights=ALL_RIGHTS).ok
    assert cmds.apply(aid, MemoryCommand(command="insert", path=path, insert_line=1, insert_text="Weather matters. " * 30), 5,
                      purpose="maintain", rights=ALL_RIGHTS).ok
    assert cmds.tick_revise_self(aid, 6, "I forage the east field at dawn and keep a log of every node I see.")[1] is None
    assert cmds.tick_remember(aid, 7, "The east field regrows fast after rain, Ada said again.", tags=["places"])[0] is not None
    assert cmds.apply(aid, MemoryCommand(command="rename", old_path=path, new_path=f"{NOTES_DIR}/Dawn-log.md"), 8,
                      purpose="maintain", rights=ALL_RIGHTS).ok
    rows = db.fetchall("SELECT * FROM memory_commands ORDER BY rowid")
    assert [r["purpose"] for r in rows] == ["tick", "maintain", "maintain", "maintain", "tick", "tick", "maintain"]
    assert all(r["ok"] for r in rows)
    assert any(len(json.loads(r["args"]).get(k, "")) > 400 for r in rows for k in ("file_text", "new_str", "insert_text"))

    _, again = _harness(tmp_path, "replayed")
    hashes = _replay(again, rows)
    assert hashes == [r["result_hash"] for r in rows]
    assert _vault_hashes(again, aid) == _vault_hashes(cmds, aid) and _vault_hashes(cmds, aid)


# --- the code commit ------------------------------------------------------------------------------------------
def test_the_run_records_its_code_commit(smoke: SimRun) -> None:
    commit, dirty = smoke.kv("code_commit"), smoke.kv("code_dirty")
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available here")
    assert commit == head and len(commit) == 40
    assert isinstance(dirty, bool)


def test_code_version_is_none_outside_a_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop_module, "REPO_ROOT", tmp_path)
    assert loop_module.code_version.__wrapped__() == (None, None)
