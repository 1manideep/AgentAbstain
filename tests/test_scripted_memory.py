"""How scripted agents use memory under fog (decision 19, MEMORY_EVOLUTION §10.4), and the retrieval-size knob.

Unit tests pin the node-status parsing (sightings, forage notes and index notes; newest wins, the index wins a
tie), the navigation filter (visible nodes left to sight, recently emptied nodes skipped, old ones demoted to
thin), the correction note (written only when sight contradicts memory, so an amnesic agent never writes one)
and that a move under fog goes to a remembered rich node rather than a remembered empty one. The knob tests check
that ``memory.max_retrieved`` caps what retrieval returns and what a decision is shown, that its default is 8,
and that only the scripted E0 and dependence-check configs lower it to 4.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, REPO, deep_merge

from void.agents.models import PersonalitySeed
from void.brain.maintenance_strategies import StrategyContext, index_body
from void.brain.scripted import NodeMemory, ScriptedBrain, node_memory
from void.config import MemoryConfig, load_config
from void.db import Database
from void.ids import IdFactory
from void.memory.store import MemoryStore
from void.rng import RNG
from void.types import NodeView, Observation, RetrievedNote, Vec2

TPD = 12  # ticks per day in the observations below (tick_of_day + ticks_left_today + 1)


def _note(i: int, title: str, body: str, tick: int) -> RetrievedNote:
    return RetrievedNote(f"note_{i:02d}", title, body, 0.5, 0, None, tick)


def _obs(memories: list[RetrievedNote], *, tick: int = 30, nodes: list[NodeView] | None = None,
         actions: tuple[str, ...] = ("move", "idle")) -> Observation:
    tod = (tick - 1) % TPD
    return Observation(tick=tick, day=(tick - 1) // TPD + 1, tick_of_day=tod, ticks_left_today=TPD - tod - 1, agent_id="ag_1",
                       name="Ada", tier="scripted", generation=0, position=Vec2(0.0, 0.0), balance_usd=1.0,
                       burn_rate_usd_per_tick=0.004, stress=0.1, stress_label="calm", weather=0.0, weather_label="mild",
                       scarcity_label="normal", nodes=list(nodes or []), memories=memories, available_actions=list(actions),
                       world_size=60.0, nodes_in_view=len(nodes or []))


FORAGE_N1 = _note(1, "Foraging at node n1 on day 2", "Foraged at [[Node n1]] at (15, 4) on day 2; it was rich. Weather mild.", 14)
SEEN_N2 = _note(2, "Node n2 seen on day 2", "Saw [[Node n2]] at (-12, 9) on day 2; it was rich.", 16)
EMPTY_N1 = _note(3, "Node n1 seen empty on day 3", "Saw [[Node n1]] at (15, 4) on day 3; it was empty.", 27)


# --- node-status parsing ------------------------------------------------------------------------------------
def test_node_memory_parses_sightings_and_forage_notes() -> None:
    known = node_memory([FORAGE_N1, SEEN_N2], TPD)
    assert known == {"n1": NodeMemory("n1", 15.0, 4.0, "rich", 14, "note"), "n2": NodeMemory("n2", -12.0, 9.0, "rich", 16, "note")}
    # a positional mention with no stock word counts as thin
    vague = node_memory([_note(4, "A place", "[[Node n7]] at (3, -2) is where Bao stands.", 5)], TPD)
    assert vague["n7"].bucket == "thin"
    # talk and people notes place nothing on the map
    assert node_memory([_note(5, "Bao said something", "[[Bao]] told me: The node n4 is rich.", 9)], TPD) == {}


def test_newest_status_wins_whatever_the_retrieval_order() -> None:
    for order in ([FORAGE_N1, EMPTY_N1], [EMPTY_N1, FORAGE_N1]):
        assert node_memory(order, TPD)["n1"] == NodeMemory("n1", 15.0, 4.0, "empty", 27, "note")


def test_index_notes_are_parsed_and_win_ties() -> None:
    ctx = StrategyContext(agent_id="ag_1", day=2, policy_text=None, listing="",
                          notes=[{"title": FORAGE_N1.title, "body": FORAGE_N1.body, "tick": 14, "tags": ["resources"]}])
    body = index_body(ctx, "n1", 15.0, 4.0, "thin")  # what the index_builder strategy writes
    index = _note(9, "Node n1 index", body, 13)
    known = node_memory([index], TPD)
    # the index summarises every note up to the end of the day it was rebuilt on (day 2 -> tick 24)
    assert known["n1"] == NodeMemory("n1", 15.0, 4.0, "thin", 2 * TPD, "index")
    # an older sighting is superseded by the index, a newer one supersedes it
    assert node_memory([FORAGE_N1, index], TPD)["n1"].source == "index"
    assert node_memory([index, EMPTY_N1], TPD)["n1"].bucket == "empty"
    # a tie goes to the index
    same_tick = _note(10, "Node n1 seen on day 2", "Saw [[Node n1]] at (15, 4) on day 2; it was rich.", 2 * TPD)
    assert node_memory([same_tick, index], TPD)["n1"].source == "index"


def test_remembered_nodes_keeps_its_shape() -> None:
    assert sorted(ScriptedBrain._remembered_nodes(_obs([FORAGE_N1, SEEN_N2, EMPTY_N1]))) == [
        ("n1", 15.0, 4.0, "empty"), ("n2", -12.0, 9.0, "rich")]


# --- skipping empty nodes -----------------------------------------------------------------------------------
def test_navigation_skips_recently_emptied_and_visible_nodes() -> None:
    obs = _obs([FORAGE_N1, SEEN_N2, EMPTY_N1], tick=30)  # n1 was seen empty 3 ticks ago
    assert ScriptedBrain._navigable_nodes(obs) == [("n2", -12.0, 9.0, "rich")]
    # a visible node is left to sight, whatever memory says
    seen = _obs([FORAGE_N1, SEEN_N2], tick=30, nodes=[NodeView("n2", 2.0, "empty", -12.0, 9.0)])
    assert ScriptedBrain._navigable_nodes(seen) == [("n1", 15.0, 4.0, "rich")]
    # an amnesic observation remembers nothing
    assert ScriptedBrain._navigable_nodes(_obs([])) == []


def test_an_old_empty_sighting_counts_as_thin() -> None:
    later = _obs([FORAGE_N1, EMPTY_N1], tick=EMPTY_N1.created_tick + TPD)  # a day on, it may have regrown
    assert ScriptedBrain._navigable_nodes(later) == [("n1", 15.0, 4.0, "thin")]
    just_before = _obs([FORAGE_N1, EMPTY_N1], tick=EMPTY_N1.created_tick + TPD - 1)
    assert ScriptedBrain._navigable_nodes(just_before) == []


def _brain() -> ScriptedBrain:
    cfg = load_config(REPO / "configs" / "exp_mem_dep_mem.yaml")
    return ScriptedBrain("scripted", cfg, RNG(7), {"ag_1": PersonalitySeed()})


def test_a_move_under_fog_goes_to_the_remembered_rich_node_not_the_emptied_one() -> None:
    brain = _brain()
    # n1 (nearer) was emptied this morning; n2 is remembered rich; nothing is in view
    near_empty = _note(11, "Node n1 seen empty on day 3", "Saw [[Node n1]] at (3, 1) on day 3; it was empty.", 28)
    near_rich = _note(12, "Foraging at node n1 on day 3", "Foraged at [[Node n1]] at (3, 1) on day 3; it was rich.", 26)
    obs = _obs([near_rich, SEEN_N2, near_empty], tick=30)
    for t in range(20):
        _, action = brain._utilities(obs, PersonalitySeed(), RNG(t).stream("u"))["move"]
        assert math.hypot(action.x - (-12.0), action.y - 9.0) < 1.0, (action.x, action.y)
    # before the empty sighting the nearer node was the target
    _, action = brain._utilities(_obs([near_rich, SEEN_N2], tick=30), PersonalitySeed(), RNG(0).stream("u"))["move"]
    assert math.hypot(action.x - 3.0, action.y - 1.0) < 1.0


# --- the correction note ------------------------------------------------------------------------------------
def test_a_node_seen_empty_against_memory_is_written_down() -> None:
    obs = _obs([FORAGE_N1], tick=30, nodes=[NodeView("n1", 0.8, "empty", 15.2, 3.7)])
    op = ScriptedBrain._correction_note(obs)
    assert op is not None and op.op == "remember"
    assert op.title == "Node n1 seen empty on day 3"
    assert op.text == "Saw [[Node n1]] at (15, 4) on day 3; it was empty."
    # the note round-trips: once retrieved, it is the newest status of n1
    written = _note(20, op.title, op.text, 30)
    assert node_memory([FORAGE_N1, written], TPD)["n1"].bucket == "empty"
    # and once memory agrees with sight, nothing more is written
    assert ScriptedBrain._correction_note(_obs([FORAGE_N1, written], tick=31, nodes=obs.nodes)) is None


def test_a_regrown_node_is_written_down_and_agreement_or_amnesia_writes_nothing() -> None:
    regrown = ScriptedBrain._correction_note(_obs([EMPTY_N1], tick=40, nodes=[NodeView("n1", 1.0, "thin", 15.0, 4.0)]))
    assert regrown is not None and regrown.text.endswith("it was thin.")
    # rich versus thin is not a contradiction worth a note
    assert ScriptedBrain._correction_note(_obs([FORAGE_N1], nodes=[NodeView("n1", 1.0, "thin", 15.0, 4.0)])) is None
    # a node memory knows nothing about, and an amnesic agent, write nothing
    assert ScriptedBrain._correction_note(_obs([SEEN_N2], nodes=[NodeView("n1", 1.0, "empty", 15.0, 4.0)])) is None
    assert ScriptedBrain._correction_note(_obs([], nodes=[NodeView("n1", 1.0, "empty", 15.0, 4.0)])) is None


def test_the_correction_note_reaches_the_decision() -> None:
    brain = _brain()
    obs = _obs([FORAGE_N1], tick=30, nodes=[NodeView("n1", 0.8, "empty", 15.2, 3.7)], actions=("move", "idle", "forage"))
    ops = brain._memory_ops(obs, PersonalitySeed(), brain._utilities(obs, PersonalitySeed(), RNG(0).stream("u"))["idle"][1],
                            RNG(0).stream("m"))
    assert ops and ops[0].title == "Node n1 seen empty on day 3"


# --- the retrieval-size knob --------------------------------------------------------------------------------
def _store(tmp_path: Path, **cfg: Any) -> MemoryStore:
    return MemoryStore(Database(tmp_path / "world.db"), MemoryConfig(**cfg), IdFactory("knob"), tmp_path / "vaults")


def test_max_retrieved_defaults_to_8_and_caps_retrieval(tmp_path: Path) -> None:
    assert MemoryConfig().max_retrieved == 8
    for k in (8, 4, 2):
        store = _store(tmp_path / f"k{k}", max_retrieved=k)
        for i in range(12):
            store.remember("ag_1", i, f"Foraged at [[Node n{i % 3}]] at ({i}, {i}) on day 1; it was rich.", title=f"Forage {i}")
        got = store.retrieve("ag_1", "Node n1 rich forage", 20)
        assert len(got) == k
        assert len(store.retrieve("ag_1", "Node n1 rich forage", 20, k=3)) == 3  # an explicit k still overrides


def test_max_retrieved_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_retrieved"):
        load_config(REPO / "configs" / "scripted_smoke.yaml", {"memory": {"max_retrieved": 0}})


def test_decisions_are_shown_at_most_max_retrieved_notes(run_sim) -> None:
    import json

    over = deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 2, "ticks_per_day": 12}, "world": {"view_radius": 12.0},
                                          "memory": {"max_retrieved": 2}})
    run = run_sim("knob_k2", over)
    shown = [len(json.loads(r["observation"])["memories"]) for r in run.db.fetchall("SELECT observation FROM llm_calls WHERE purpose='decide'")]
    assert shown and max(shown) == 2


def test_only_the_scripted_memory_checks_lower_the_knob() -> None:
    for name in ("exp_mem_e0_noop", "exp_mem_e0_index_builder", "exp_mem_e0_hoarder", "exp_mem_e0_decoy",
                 "exp_mem_dep_mem", "exp_mem_dep_amnesic"):
        assert load_config(REPO / "configs" / f"{name}.yaml").memory.max_retrieved == 4, name
    for name in ("base", "live_mem_e1_a", "live_mem_e1_a0", "live_mem_e1_b", "live_mem_e1_c", "demo_memory"):
        assert load_config(REPO / "configs" / f"{name}.yaml").memory.max_retrieved == 8, name
