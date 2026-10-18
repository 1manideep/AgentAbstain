"""Retrieval outcomes and memory-command rates (void/research/outcomes/retrieval.py; MEMORY_EVOLUTION §7.2, §10.2 G1).

A hand-built ``world.db`` pins every figure, and its daily version, to values worked out by hand below. Scripted runs
only: two days of each exp_mem_dep arm (the amnesic arm retrieves nothing, the memory arm does) and two days of E0
arms (the maintenance commands).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from void.config import load_config
from void.db import Database
from void.research.analysis import DAILY, computed_outcome_names, end_of_run_outcome
from void.research.outcomes import OUTCOMES, load_all
from void.research.outcomes import retrieval as rt
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"
TPD = 4  # day 1 is ticks 1-4, day 2 ticks 5-8, day 3 ticks 9-12
NAMES = ("empty_retrieval_rate", "mean_retrieved_age_ticks", "retrieved_depth_share", "retrieved_cited_rate",
         "maintenance_valid_command_rate", "tick_command_refusal_rate")
RETRIEVAL = NAMES[:4]
NODES = ("n1", "n2", "n12")


def _decision(action: dict[str, object], thought: str = "t") -> str:
    return json.dumps({"thought": thought, "memory_ops": [], "action": action})


def _extras(titles: list[str], hist: dict[str, int] | None = None, age: float | None = None) -> str:
    """Decide extras as the loop writes them (keys left out when ``hist`` or ``age`` is None)."""
    d: dict[str, object] = {"retrieved_titles": titles, "retrieved_hops": [0] * len(titles), "p_chosen": 0.5}
    if hist is not None:
        d["retrieved_hop_hist"] = hist
    if age is not None:
        d["retrieved_mean_age"] = age
    return json.dumps(d, sort_keys=True)


# call_id: (tick, agent, purpose, thought, raw_text, extras)
CALLS: dict[str, tuple[int, str, str, str, str | None, str | None]] = {
    # cited: the thought names n12, a node id named in its second title (n1 is not found inside "n12")
    "c1": (1, "ag_a", "decide", "Heading back to n12 now",
           _decision({"type": "forage"}), _extras(["Node n1", "Foraging at node n12 on day 1"], {"0": 1, "1": 1}, 2.0)),
    # not cited: "n12" does not name "Node n1"; ag_b's talk on this tick is not ag_a's
    "c2": (2, "ag_a", "decide", "going to n12",
           _decision({"type": "move", "x": 1.0, "y": 2.0}), _extras(["Node n1"], {"0": 1, "1": 0, "2": 0}, 4.0)),
    # cited: the action text names the title (case ignored)
    "c3": (2, "ag_b", "decide", "thanks",
           _decision({"type": "talk", "target": "ag_a", "text": "Remember: bao GAVE me money!"}),
           _extras(["Bao gave me money"], {"0": 0, "1": 1}, 1.0)),
    # an empty retrieval
    "c4": (3, "ag_b", "decide", "idle", _decision({"type": "idle"}), _extras([], {"0": 0}, 0.0)),
    # cited through its talk event; the response is prose with no JSON, so it gives no action
    "c5": (5, "ag_a", "decide", "idle", "I will talk to Bao now.",
           _extras(["Rich spot east", "Node n2"], {"0": 1, "1": 0, "2": 1}, 10.0)),
    # no retrieval logged: left out of every retrieval figure
    "c6": (6, "ag_b", "decide", "Node n2", _decision({"type": "idle"}), None),
    "c6b": (6, "ag_a", "decide", "Node n2", _decision({"type": "idle"}), json.dumps({"p_chosen": 0.2})),
    # not a decide call: ignored
    "c7": (6, "ag_a", "maintain", "Node n2", None, _extras(["Node n2"], {"0": 1}, 50.0)),
    # cited: the action target names node n2 (upper case); an older row shape without retrieved_mean_age
    "c8": (7, "ag_b", "decide", "", _decision({"type": "move", "target": "N2"}), _extras(["Node n2"], {"0": 1})),
}
# talk events: (tick, speaker, text)
TALKS = [
    (2, "ag_b", "Node n1 is rich"),               # ag_b at tick 2: c3 is cited anyway; c2 is ag_a's call
    (3, "ag_a", "Bao gave me money"),             # ag_a has no call at tick 3
    (5, "ag_a", "Meet me at the rich spot EAST"),  # c5's citation
]
# memory_commands: (tick, purpose, ok)
COMMANDS = [
    (5, "maintain", 1), (5, "maintain", 0), (5, "maintain", 1), (9, "maintain", 1),
    (1, "tick", 1), (2, "tick", 1), (2, "tick", 0), (6, "tick", 1), (7, "tick", 0),
    (3, "kernel", 0), (4, "recall", 0),
]
EXPECTED = {
    # retrieval logged on c1 c2 c3 c4 c5 c8; c4 retrieved nothing
    "empty_retrieval_rate": 1 / 6,
    # pooled over notes; c8 has no age: (2*2 + 1*4 + 1*1 + 2*10) / (2 + 1 + 1 + 2)
    "mean_retrieved_age_ticks": 29 / 6,
    # notes at depth >= 1 over notes, from the hop histograms of c1 c2 c3 c4 c5 c8: (1+0+1+0+1+0) / (2+1+1+0+2+1)
    "retrieved_depth_share": 3 / 7,
    # c1 c3 c5 c8 cite, c2 does not; c4 retrieved nothing
    "retrieved_cited_rate": 4 / 5,
    "maintenance_valid_command_rate": 3 / 4,
    "tick_command_refusal_rate": 2 / 5,
}
EXPECTED_DAILY = {  # day -> figures
    1: {"empty_retrieval_rate": 1 / 4, "mean_retrieved_age_ticks": 9 / 4, "retrieved_depth_share": 2 / 4,
        "retrieved_cited_rate": 2 / 3, "maintenance_valid_command_rate": None, "tick_command_refusal_rate": 1 / 3},
    2: {"empty_retrieval_rate": 0.0, "mean_retrieved_age_ticks": 10.0, "retrieved_depth_share": 1 / 3,
        "retrieved_cited_rate": 1.0, "maintenance_valid_command_rate": 2 / 3, "tick_command_refusal_rate": 1 / 2},
    3: {"empty_retrieval_rate": None, "mean_retrieved_age_ticks": None, "retrieved_depth_share": None,
        "retrieved_cited_rate": None, "maintenance_valid_command_rate": 1.0, "tick_command_refusal_rate": None},
}


@pytest.fixture(autouse=True)
def _registered() -> None:
    load_all()  # copies the registered daily versions into DAILY, as end_of_run_outcome and compare do


def _synthetic(run_dir: Path, *, nodes: tuple[str, ...] = NODES) -> Path:
    cfg = load_config(CONFIGS / "base.yaml", {"run": {"days": 3, "ticks_per_day": TPD}})
    (run_dir / "config.resolved.yaml").write_text(cfg.to_yaml())
    db = Database(run_dir / "world.db")
    try:
        for nid in nodes:
            db.execute("INSERT INTO resource_nodes(node_id, x, y, stock, capacity, regen_per_tick) VALUES(?,?,?,?,?,?)",
                       (nid, 0.0, 0.0, 1.0, 1.0, 0.1))
        for cid, (tick, aid, purpose, thought, raw, extras) in CALLS.items():
            db.execute("INSERT INTO llm_calls(call_id, tick, agent_id, purpose, tier, model, provider, real_cost, world_cost, "
                       "thought, raw_text, extras) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (cid, tick, aid, purpose, "scripted", "scripted", "scripted", 0, 0, thought, raw, extras))
        for tick, speaker, text in TALKS:
            db.execute("INSERT INTO events(tick, day, kind, agent_id, payload) VALUES(?,?,?,?,?)",
                       (tick, (tick - 1) // TPD + 1, "talk", speaker,
                        json.dumps({"speaker_id": speaker, "listener_id": "ag_x", "text": text})))
        for i, (tick, purpose, ok) in enumerate(COMMANDS):
            db.execute("INSERT INTO memory_commands(cmd_id, tick, agent_id, purpose, command, args, ok) VALUES(?,?,?,?,?,?,?)",
                       (f"mc{i}", tick, "ag_a", purpose, "create", "{}", ok))
    finally:
        db.close()
    return run_dir


# --- registration ----------------------------------------------------------------------------------------------
def test_every_outcome_is_registered_with_a_daily_version() -> None:
    listed = computed_outcome_names()
    for name in NAMES:
        assert name in listed, name
        assert OUTCOMES[name] is getattr(rt, name), name
        assert name in DAILY, name


# --- matching helpers ------------------------------------------------------------------------------------------
def test_mentions_needs_a_word_boundary_and_ignores_case() -> None:
    assert rt.mentions("going to N12 now", "n12")
    assert not rt.mentions("going to n12 now", "n1")
    assert not rt.mentions("xn1", "n1")
    assert rt.mentions("(n1)", "n1") and rt.mentions("at n1.", "n1")
    assert rt.mentions("Remember: bao GAVE me money!", "Bao gave me money")
    assert rt.mentions("he said: what? then left", "what?")  # no boundary needed after a non-alphanumeric end
    assert not rt.mentions("anything", "") and not rt.mentions("anything", "  ")


def test_node_ids_in_a_title() -> None:
    assert rt.node_ids_in("Foraging at node n12 on day 1", frozenset(NODES)) == ["n12"]
    assert rt.node_ids_in("Node n1 and n2", frozenset(NODES)) == ["n1", "n2"]
    assert rt.node_ids_in("Node n7 seen", frozenset(NODES)) == []  # not a node of this run
    assert rt.node_ids_in("Node n7 seen on day 1, near N3") == ["n3", "n7"]  # no node table: every n<digits> token
    assert rt.node_ids_in("Bao gave me money") == []


def test_response_action_reads_the_logged_json() -> None:
    assert rt.response_action(_decision({"type": "talk", "target": "ag_a", "text": "hi"})) == ("ag_a", "hi")
    assert rt.response_action(_decision({"type": "forage"})) == (None, None)
    fenced = "Here it is:\n```json\n" + _decision({"type": "move", "target": "n2"}) + "\n```"
    assert rt.response_action(fenced) == ("n2", None)
    assert rt.response_action("I will talk to Bao now.") == (None, None)
    assert rt.response_action('{"action": "talk"}') == (None, None)
    assert rt.response_action(_decision({"type": "talk", "target": 3, "text": ["x"]})) == (None, None)
    assert rt.response_action(None) == (None, None) and rt.response_action("") == (None, None)


def test_cited_looks_at_thought_action_and_talk() -> None:
    base = rt.CallRetrieval(call_id="c", agent_id="a", tick=1, titles=("Node n1",), hop_hist=None, mean_age=None,
                            thought="", action_target=None, action_text=None, talk=())
    nodes = frozenset(NODES)
    assert not rt.cited(base, nodes)
    for field, value in (("thought", "back to n1"), ("action_target", "N1"), ("action_text", "node n1 is rich"),
                         ("talk", ("I saw Node n1",))):
        assert rt.cited(replace(base, **{field: value}), nodes), field
    assert not rt.cited(replace(base, thought="off to n12"), nodes)
    assert not rt.cited(replace(base, titles=(), thought="Node n1"), nodes)


# --- a hand-built run --------------------------------------------------------------------------------------------
def test_synthetic_rows_give_exact_values(tmp_path: Path) -> None:
    run = _synthetic(tmp_path)
    for name, want in EXPECTED.items():
        assert getattr(rt, name)(run) == pytest.approx(want), name
        assert end_of_run_outcome(run, name) == pytest.approx(want), name


def test_synthetic_daily_values(tmp_path: Path) -> None:
    run = _synthetic(tmp_path)
    for day, figures in EXPECTED_DAILY.items():
        for name, want in figures.items():
            got = DAILY[name](run, None, day)
            assert got == (None if want is None else pytest.approx(want)), (day, name)


def test_decide_retrievals_reads_what_each_call_was_shown(tmp_path: Path) -> None:
    calls = {c.call_id: c for c in rt.decide_retrievals(_synthetic(tmp_path))}
    assert list(calls) == ["c1", "c2", "c3", "c4", "c5", "c8"]  # by tick, then call id; c6/c6b log no retrieval
    c1, c3, c5, c8 = calls["c1"], calls["c3"], calls["c5"], calls["c8"]
    assert c1.titles == ("Node n1", "Foraging at node n12 on day 1") and c1.hop_hist == {0: 1, 1: 1} and c1.mean_age == 2.0
    assert (c3.action_target, c3.action_text) == ("ag_a", "Remember: bao GAVE me money!")
    assert (c5.action_target, c5.action_text) == (None, None) and c5.talk == ("Meet me at the rich spot EAST",)
    assert calls["c2"].talk == () and c8.mean_age is None and c8.action_target == "N2"
    assert [c.call_id for c in rt.decide_retrievals(tmp_path, day=2)] == ["c5", "c8"]


def test_a_tier_argument_filters_calls_and_command_rates_take_none(tmp_path: Path) -> None:
    run = _synthetic(tmp_path)
    for name in RETRIEVAL:
        assert end_of_run_outcome(run, f"{name}.scripted") == pytest.approx(EXPECTED[name]), name
        assert end_of_run_outcome(run, f"{name}.sharp") is None, name  # no call of that tier
    for name in ("maintenance_valid_command_rate", "tick_command_refusal_rate"):
        assert end_of_run_outcome(run, f"{name}.scripted") is None, name


def test_without_a_node_table_node_ids_come_from_the_titles(tmp_path: Path) -> None:
    run = _synthetic(tmp_path, nodes=())
    assert rt.retrieved_cited_rate(run) == pytest.approx(EXPECTED["retrieved_cited_rate"])


def test_nothing_to_measure_gives_none(tmp_path: Path) -> None:
    run = _synthetic(tmp_path)
    conn = sqlite3.connect(str(run / "world.db"))
    try:
        conn.execute("DELETE FROM llm_calls WHERE call_id NOT IN ('c4', 'c6', 'c6b')")  # only an empty retrieval left
        conn.execute("DELETE FROM memory_commands WHERE purpose IN ('maintain', 'tick')")
        conn.commit()
    finally:
        conn.close()
    assert rt.empty_retrieval_rate(run) == 1.0
    for name in NAMES[1:]:
        assert getattr(rt, name)(run) is None, name
    conn = sqlite3.connect(str(run / "world.db"))
    try:
        conn.execute("DROP TABLE memory_commands")  # a run made before the memory layer
        conn.execute("UPDATE llm_calls SET extras=NULL")
        conn.commit()
    finally:
        conn.close()
    for name in NAMES:
        assert getattr(rt, name)(run) is None, name


def test_missing_age_and_histogram_keys_give_none(tmp_path: Path) -> None:
    run = _synthetic(tmp_path)
    conn = sqlite3.connect(str(run / "world.db"))
    try:
        for cid, extras in conn.execute("SELECT call_id, extras FROM llm_calls WHERE extras IS NOT NULL").fetchall():
            d = json.loads(extras)
            d.pop("retrieved_mean_age", None)
            d.pop("retrieved_hop_hist", None)
            conn.execute("UPDATE llm_calls SET extras=? WHERE call_id=?", (json.dumps(d), cid))
        conn.commit()
    finally:
        conn.close()
    assert rt.mean_retrieved_age_ticks(run) is None
    assert rt.retrieved_depth_share(run) is None
    assert rt.empty_retrieval_rate(run) == pytest.approx(EXPECTED["empty_retrieval_rate"])  # titles are still there


# --- short scripted runs -----------------------------------------------------------------------------------------
def _run(cfg_file: str, out: Path) -> Path:
    stem = Path(cfg_file).stem
    cfg_path = out / f"{stem}.yaml"
    cfg_path.write_text(f"extends: {CONFIGS / cfg_file}\nrun:\n  days: 2\n")
    sim = Simulation(load_config(cfg_path), out / stem)
    try:
        asyncio.run(sim.run())
    finally:
        sim.db.close()
    return out / stem


@pytest.fixture(scope="module")
def dependence(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Each exp_mem_dep arm for two days on its configured seed, from a temporary config extending the arm's file."""
    out = tmp_path_factory.mktemp("mem_dep_retrieval")
    return {arm: _run(f"exp_mem_dep_{arm}.yaml", out) for arm in ("mem", "amnesic")}


def test_amnesic_retrieves_nothing_and_memory_does(dependence: dict[str, Path]) -> None:
    amnesic = {name: end_of_run_outcome(dependence["amnesic"], name) for name in NAMES}
    mem = {name: end_of_run_outcome(dependence["mem"], name) for name in NAMES}
    assert amnesic["empty_retrieval_rate"] == 1.0
    assert mem["empty_retrieval_rate"] is not None and mem["empty_retrieval_rate"] < 1.0
    # nothing retrieved, no notes written: nothing else to measure in the amnesic arm
    assert all(amnesic[n] is None for n in NAMES[1:]), amnesic
    assert mem["mean_retrieved_age_ticks"] is not None and mem["mean_retrieved_age_ticks"] >= 0.0
    for name in ("retrieved_depth_share", "retrieved_cited_rate", "tick_command_refusal_rate"):
        assert mem[name] is not None and 0.0 <= mem[name] <= 1.0, (name, mem)
    assert mem["maintenance_valid_command_rate"] is None  # the dependence check runs no maintenance
    for day in (1, 2):
        assert DAILY["empty_retrieval_rate"](dependence["amnesic"], None, day) == 1.0, day


def test_retrieval_figures_agree_with_the_call_rows(dependence: dict[str, Path]) -> None:
    run = dependence["mem"]
    conn = sqlite3.connect(str(run / "world.db"))
    try:
        rows = [json.loads(e) for (e,) in conn.execute("SELECT extras FROM llm_calls WHERE purpose='decide'")]
    finally:
        conn.close()
    assert rows and all("retrieved_titles" in d and "retrieved_hop_hist" in d for d in rows)
    shown = [d for d in rows if d["retrieved_titles"]]
    assert rt.empty_retrieval_rate(run) == pytest.approx(1 - len(shown) / len(rows))
    notes = sum(len(d["retrieved_titles"]) for d in shown)
    assert sum(sum(d["retrieved_hop_hist"].values()) for d in rows) == notes  # one histogram entry per note shown
    assert rt.mean_retrieved_age_ticks(run) == pytest.approx(
        sum(d["retrieved_mean_age"] * len(d["retrieved_titles"]) for d in shown) / notes)


@pytest.fixture(scope="module")
def e0(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Two E0 arms for two days: the hoarder issues maintenance commands every night, the no-op none."""
    out = tmp_path_factory.mktemp("e0_retrieval")
    return {arm: _run(f"exp_mem_e0_{arm}.yaml", out) for arm in ("hoarder", "noop")}


def test_maintenance_valid_command_rate_on_e0(e0: dict[str, Path]) -> None:
    run = e0["hoarder"]
    rate = end_of_run_outcome(run, "maintenance_valid_command_rate")
    assert rate is not None and 0.0 <= rate <= 1.0
    conn = sqlite3.connect(str(run / "world.db"))
    try:
        n, ok = conn.execute("SELECT COUNT(*), SUM(ok) FROM memory_commands WHERE purpose='maintain'").fetchone()
    finally:
        conn.close()
    assert n > 0 and rate == pytest.approx(ok / n)
    assert DAILY["maintenance_valid_command_rate"](run, None, 1) is None  # the first nightly step opens day 2
    assert 0.0 <= DAILY["maintenance_valid_command_rate"](run, None, 2) <= 1.0
    assert end_of_run_outcome(e0["noop"], "maintenance_valid_command_rate") is None  # the no-op issues no command
    refusal = end_of_run_outcome(run, "tick_command_refusal_rate")
    assert refusal is not None and 0.0 <= refusal <= 1.0
