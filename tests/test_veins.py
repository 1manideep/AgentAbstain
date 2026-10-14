"""Veins and fallow nodes (MEMORY_EVOLUTION §5.2, §10.4): the world features meant to make memory worth having.

Kernel invariants are checked on short scripted runs: veins open only where the configured chances allow, pay the
configured multiplier, are shown to an agent only within ``world.veins.visible_radius`` of their node, end on time,
and replay identically for the same seed; a node foraged to nothing lies fallow and is not foraged again before it
recovers. Brain tests pin how scripted agents notice, write down and walk to veins, and two review fixes: a
correction note no longer suppresses the heard-message note, and the daily self-revision keeps its slot. That
worlds without these features are byte-identical to before is pinned separately by tests/test_world_goldens.py.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, REPO, deep_merge

from void.agents.models import PersonalitySeed
from void.brain.scripted import ScriptedBrain, VeinMemory, node_memory, vein_memory
from void.config import VeinsConfig, WorldConfig, load_config
from void.events import Kind
from void.rng import RNG
from void.types import HeardMessage, NodeView, Observation, RetrievedNote, Vec2

TPD = 12
FOG_VEINS: dict[str, Any] = deep_merge(NO_PROVIDER_TIERS, {
    "run": {"days": 2, "ticks_per_day": TPD},
    "world": {"view_radius": 12.0, "veins": {"enabled": True, "prone_nodes": 2, "p_start_prone": 0.5, "p_start_other": 0.0,
                                             "duration_ticks": 4, "yield_mult": 4.0, "visible_radius": 4.0}},
})


def _payloads(run, kind: str) -> list[dict[str, Any]]:
    return [{**json.loads(r["payload"]), "tick": int(r["tick"])}
            for r in run.db.fetchall("SELECT tick, payload FROM events WHERE kind=? ORDER BY seq", (kind,))]


# --- config -------------------------------------------------------------------------------------------------
def test_veins_and_fallow_are_off_by_default_and_validated() -> None:
    w = WorldConfig()
    assert w.veins.enabled is False and w.fallow_ticks == 0
    with pytest.raises(ValueError):
        WorldConfig(view_radius=6.0, veins=VeinsConfig(enabled=True, visible_radius=6.0))  # must be seen only up close
    with pytest.raises(ValueError):
        WorldConfig(resource_nodes=2, veins=VeinsConfig(enabled=True, prone_nodes=3))
    with pytest.raises(ValueError):
        VeinsConfig(yield_mult=0.5)
    with pytest.raises(ValueError):
        WorldConfig(fallow_ticks=-1)


# --- kernel invariants on a short run ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def vein_run(run_sim):
    return run_sim("veins_fog", FOG_VEINS)


def test_veins_open_only_at_prone_nodes_and_end_on_time(vein_run) -> None:
    starts = _payloads(vein_run, Kind.VEIN_START)
    ends = _payloads(vein_run, Kind.VEIN_END)
    assert starts, "with p_start_prone 0.5 over 24 ticks some vein must open"
    prone = set(vein_run.sim.kernel.vein_prone_nodes())
    assert len(prone) == 2 and all(s["prone"] and s["node_id"] in prone for s in starts)  # p_start_other is 0
    for s in starts:
        assert s["until_tick"] == s["tick"] + 4 - 1 and s["yield_mult"] == 4.0
    for e in ends:  # a vein ends the tick after its last paying tick
        assert e["tick"] == e["last_tick"] + 1


def test_foraging_in_a_vein_pays_the_multiplier(vein_run) -> None:
    forages = _payloads(vein_run, Kind.FORAGE)
    in_vein = [f for f in forages if "vein" in f]
    starts = _payloads(vein_run, Kind.VEIN_START)
    for f in in_vein:
        assert f["vein"] == 4.0
        assert any(s["node_id"] == f["node_id"] and s["tick"] <= f["tick"] <= s["until_tick"] for s in starts)
    for f in forages:  # every forage outside a vein window carries no vein mark
        if "vein" not in f:
            assert not any(s["node_id"] == f["node_id"] and s["tick"] <= f["tick"] <= s["until_tick"] for s in starts)


def test_a_vein_is_shown_only_within_its_visible_radius(vein_run) -> None:
    shown = 0
    for r in vein_run.db.fetchall("SELECT observation FROM llm_calls WHERE purpose='decide' AND observation IS NOT NULL"):
        for n in json.loads(r["observation"]).get("nodes", []):
            if n.get("vein_mult") is not None:
                shown += 1
                assert n["distance"] <= 4.0 + 1e-9
                assert n["vein_ticks_left"] >= 1
    assert shown > 0, "agents stand at nodes, so some vein must be seen up close"


def test_veins_replay_identically_for_the_same_seed(run_sim) -> None:
    a = run_sim("veins_det_a", FOG_VEINS)
    b = run_sim("veins_det_b", FOG_VEINS)
    key = lambda run: [(p["tick"], p["node_id"], p["until_tick"]) for p in _payloads(run, Kind.VEIN_START)]  # noqa: E731
    assert key(a) == key(b) and key(a)


def test_a_node_foraged_to_nothing_lies_fallow_until_it_recovers(run_sim) -> None:
    run = run_sim("fallow", deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 2, "ticks_per_day": TPD},
                                                            "world": {"fallow_ticks": 6, "node_capacity": 3.0}}))
    fallows = _payloads(run, Kind.NODE_FALLOW)
    assert fallows, "nodes of capacity 3 must be foraged out within two days"
    forages = _payloads(run, Kind.FORAGE)
    for f in fallows:
        assert f["until_tick"] == f["tick"] + 6 + 1
        # nothing is foraged at that node while it lies fallow (the depleting forage itself is at f["tick"])
        assert not any(g["node_id"] == f["node_id"] and f["tick"] < g["tick"] < f["until_tick"] and g["units"] > 0 for g in forages)


# --- the scripted brain and veins -------------------------------------------------------------------------
def _note(i: int, title: str, body: str, tick: int) -> RetrievedNote:
    return RetrievedNote(f"note_{i:02d}", title, body, 0.5, 0, None, tick)


def _obs(memories: list[RetrievedNote], *, tick: int = 30, nodes: list[NodeView] | None = None,
         actions: tuple[str, ...] = ("move", "idle", "forage"), heard: list[HeardMessage] | None = None) -> Observation:
    tod = (tick - 1) % TPD
    return Observation(tick=tick, day=(tick - 1) // TPD + 1, tick_of_day=tod, ticks_left_today=TPD - tod - 1, agent_id="ag_1",
                       name="Ada", tier="scripted", generation=0, position=Vec2(0.0, 0.0), balance_usd=1.0,
                       burn_rate_usd_per_tick=0.004, stress=0.1, stress_label="calm", weather=0.0, weather_label="mild",
                       scarcity_label="normal", nodes=list(nodes or []), memories=memories, available_actions=list(actions),
                       world_size=60.0, nodes_in_view=len(nodes or []), heard=list(heard or []))


def _brain() -> ScriptedBrain:
    return ScriptedBrain("scripted", load_config(REPO / "configs" / "exp_mem_dep_mem.yaml"), RNG(7), {"ag_1": PersonalitySeed()})


def _vein_view(node_id: str = "n3", dist: float = 1.0, left: int = 5, x: float = 12.0, y: float = -5.0) -> NodeView:
    v = NodeView(node_id, dist, "rich", x, y)
    v.vein_mult, v.vein_ticks_left = 4.0, left
    return v


def test_a_vein_in_sight_is_written_down_once() -> None:
    obs = _obs([], tick=30, nodes=[_vein_view()])
    op = ScriptedBrain._vein_note(obs)
    assert op is not None and op.title == "Vein at node n3 on day 3"
    assert op.text == "Saw a vein at [[Node n3]] at (12, -5) on day 3, tick 30; it pays x4 for 5 more ticks."
    written = _note(1, op.title, op.text, 30)
    assert vein_memory([written]) == {"n3": VeinMemory("n3", 12.0, -5.0, 1, 34)}
    # the vein note also places the node on the map (it names no stock, so plain parsing says thin) and
    # navigation ranks a remembered vein site as rich
    assert node_memory([written], TPD)["n3"].bucket == "thin"
    assert ScriptedBrain._navigable_nodes(_obs([written], tick=40)) == [("n3", 12.0, -5.0, "rich")]
    # once remembered and still paying, no second note
    assert ScriptedBrain._vein_note(_obs([written], tick=31, nodes=[_vein_view(left=4)])) is None
    # no vein in sight, no note
    assert ScriptedBrain._vein_note(_obs([], nodes=[NodeView("n3", 1.0, "rich", 12.0, -5.0)])) is None


def test_a_remembered_open_vein_out_of_sight_is_worth_a_walk() -> None:
    brain = _brain()
    vein = _note(1, "Vein at node n3 on day 3", "Saw a vein at [[Node n3]] at (12, -5) on day 3, tick 28; it pays x4 for 6 more ticks.", 28)
    open_now = _obs([vein], tick=30, nodes=[NodeView("n1", 1.0, "thin", 1.0, 0.0)])
    assert ScriptedBrain._open_vein_target(open_now) == ("n3", 12.0, -5.0)
    for t in range(10):
        _, action = brain._utilities(open_now, PersonalitySeed(), RNG(t).stream("u"))["move"]
        assert math.hypot(action.x - 12.0, action.y + 5.0) < 1.0
    # after the vein's window the walk is no longer special, though the site still ranks as rich
    closed = _obs([vein], tick=40)
    assert ScriptedBrain._open_vein_target(closed) is None
    assert ScriptedBrain._navigable_nodes(closed) == [("n3", 12.0, -5.0, "rich")]
    # an amnesic agent has nothing to walk to
    assert ScriptedBrain._open_vein_target(_obs([], tick=30)) is None


def test_a_vein_in_view_is_the_move_target_and_foraging_at_it_is_preferred() -> None:
    brain = _brain()
    far_vein = _vein_view(dist=6.0, x=6.0, y=0.0)
    obs = _obs([], nodes=[NodeView("n1", 2.0, "rich", 2.0, 0.0), far_vein])
    _, action = brain._utilities(obs, PersonalitySeed(), RNG(0).stream("u"))["move"]
    assert math.hypot(action.x - 6.0, action.y) < 1.0
    at_vein = _obs([], nodes=[_vein_view(dist=0.5)])
    plain = _obs([], nodes=[NodeView("n3", 0.5, "rich", 12.0, -5.0)])
    u = lambda o: brain._utilities(o, PersonalitySeed(), RNG(0).stream("u"))["forage"][0]  # noqa: E731
    assert u(at_vein) > u(plain)


# --- review fixes -----------------------------------------------------------------------------------------
FORAGE_N1 = _note(1, "Foraging at node n1 on day 2", "Foraged at [[Node n1]] at (15, 4) on day 2; it was rich.", 14)


def test_a_correction_note_no_longer_suppresses_the_heard_note() -> None:
    brain = _brain()
    heard = [HeardMessage("transfer", "ag_bao", "Bao", "Bao gave you $0.10", 29)]
    obs = _obs([FORAGE_N1], tick=30, nodes=[NodeView("n1", 0.8, "empty", 15.2, 3.7)], heard=heard)
    act = brain._utilities(obs, PersonalitySeed(), RNG(0).stream("u"))["idle"][1]
    ops = brain._memory_ops(obs, PersonalitySeed(), act, RNG(0).stream("m"))
    assert [o.title for o in ops] == ["Node n1 seen empty on day 3", "Bao gave me money"]


def test_the_daily_self_revision_keeps_its_slot() -> None:
    brain = _brain()
    heard = [HeardMessage("transfer", "ag_bao", "Bao", "Bao gave you $0.10", 29)]
    late = 36  # tick_of_day 11: the last tick of day 3
    obs = _obs([FORAGE_N1], tick=late, nodes=[NodeView("n1", 0.8, "empty", 15.2, 3.7)], heard=heard)
    act = brain._utilities(obs, PersonalitySeed(), RNG(0).stream("u"))["idle"][1]
    ops = brain._memory_ops(obs, PersonalitySeed(), act, RNG(0).stream("m"))
    assert len(ops) == 2 and ops[0].title == "Node n1 seen empty on day 3" and ops[1].op == "revise_self"


def test_an_agent_avoids_a_node_its_own_correction_says_is_spent() -> None:
    """End to end over two ticks: sight contradicts memory, the agent writes it down, and the note steers it."""
    brain = _brain()
    seen_n2 = _note(2, "Node n2 seen on day 2", "Saw [[Node n2]] at (-12, 9) on day 2; it was rich.", 16)
    near_rich = _note(3, "Foraging at node n1 on day 3", "Foraged at [[Node n1]] at (3, 1) on day 3; it was rich.", 26)
    t1 = _obs([near_rich, seen_n2], tick=29, nodes=[NodeView("n1", 3.2, "empty", 3.0, 1.0)])
    act = brain._utilities(t1, PersonalitySeed(), RNG(0).stream("u"))["idle"][1]
    ops = brain._memory_ops(t1, PersonalitySeed(), act, RNG(0).stream("m"))
    assert ops and ops[0].title == "Node n1 seen empty on day 3"
    written = _note(4, ops[0].title, ops[0].text or "", 29)
    t2 = _obs([near_rich, seen_n2, written], tick=30)  # n1 now out of view
    _, move = brain._utilities(t2, PersonalitySeed(), RNG(1).stream("u"))["move"]
    assert math.hypot(move.x + 12.0, move.y - 9.0) < 1.0  # to n2, not back to the spent n1
