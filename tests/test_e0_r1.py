"""E0 under R1 (MEMORY_EVOLUTION §5.3, §6.2): the positive control's mechanism is switched on, the same rule in every arm.

The index builder "maintains an index note per node, which R1 boosts". ``configs/exp_mem_e0_base.yaml`` sets
``memory.index_boost`` once (its derivation is in the comment there), and every E0 arm inherits it, so compare's config
diff still sees only the maintenance strategy. The rule acts only on notes tagged ``index``, and in E0 only the index
builder's nightly batch writes them, so the arms differ in its effect and nowhere else. Short scripted runs check both
halves: an index note reaches a decide observation once the first index exists, more often under R1 than under R0, and
the hoarder (the other arm that writes files) never holds an index note, so R1 ranks its retrieval exactly as R0.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from void.config import load_config
from void.memory.store import INDEX_TAG
from void.research.compare import config_diff
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"
E0_ARMS = ("noop", "index_builder", "hoarder", "decoy")
TICKS_PER_DAY = 12
RUN = {"days": 2, "ticks_per_day": TICKS_PER_DAY}   # the first index files are written at the start of day 2


def _run(tmp: Path, arm: str, **memory: object) -> Path:
    over: dict[str, object] = {"run": dict(RUN)}
    if memory:
        over["memory"] = dict(memory)
    sim = Simulation(load_config(CONFIGS / f"exp_mem_e0_{arm}.yaml", over), tmp)
    try:
        asyncio.run(sim.run())
    finally:
        sim.db.close()
    return tmp


def _db(run_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(run_dir / "world.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _index_titles(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Per agent, the titles of every note that ever carried the ``index`` tag (archived ones included)."""
    out: dict[str, set[str]] = {}
    for r in conn.execute("SELECT agent_id, title, tags FROM notes"):
        if INDEX_TAG in json.loads(r["tags"] or "[]"):
            out.setdefault(str(r["agent_id"]), set()).add(str(r["title"]))
    return out


def _decides_with_index(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """``(tick, agent_id)`` of every decide call whose observation retrieved at least one index note."""
    titles = _index_titles(conn)
    hits: list[tuple[int, str]] = []
    for r in conn.execute("SELECT tick, agent_id, extras FROM llm_calls WHERE purpose='decide' ORDER BY tick, agent_id"):
        got = set(json.loads(r["extras"] or "{}").get("retrieved_titles") or [])
        if got & titles.get(str(r["agent_id"]), set()):
            hits.append((int(r["tick"]), str(r["agent_id"])))
    return hits


@pytest.fixture(scope="module")
def r1_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _run(tmp_path_factory.mktemp("e0_r1_index_builder"), "index_builder")


@pytest.fixture(scope="module")
def r0_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _run(tmp_path_factory.mktemp("e0_r0_index_builder"), "index_builder", index_boost=0.0)


# --- configs -----------------------------------------------------------------------------------------------------
def test_the_e0_base_config_turns_r1_on() -> None:
    base = load_config(CONFIGS / "exp_mem_e0_base.yaml")
    assert base.memory.index_boost > 0.0
    assert base.memory.max_retrieved == 4  # the boost was derived for a top-4 retrieval; it is re-derived if this moves


def test_every_e0_arm_resolves_the_same_index_boost() -> None:
    base = load_config(CONFIGS / "exp_mem_e0_base.yaml").memory.index_boost
    cfgs = {arm: load_config(CONFIGS / f"exp_mem_e0_{arm}.yaml") for arm in E0_ARMS}
    assert {arm: c.memory.index_boost for arm, c in cfgs.items()} == {arm: base for arm in E0_ARMS}
    dumped = {arm: c.model_dump(mode="json") for arm, c in cfgs.items()}
    for i, a in enumerate(E0_ARMS):
        for b in E0_ARMS[i + 1:]:
            diff = config_diff(dumped[a], dumped[b])
            assert not [d for d in diff if d.startswith("memory.")], (a, b, diff)
            assert "tiers.scripted.maintenance_strategy" in diff, (a, b, diff)


def test_the_dependence_arms_inherit_the_value_and_run_no_maintenance() -> None:
    base = load_config(CONFIGS / "exp_mem_e0_base.yaml").memory.index_boost
    for arm in ("mem", "amnesic"):
        cfg = load_config(CONFIGS / f"exp_mem_dep_{arm}.yaml")
        assert cfg.memory.index_boost == base
        assert not cfg.memory.maintenance.enabled  # no nightly batch, so no file under /memories/index/: R1 is R0 there


# --- the index builder under R1 ----------------------------------------------------------------------------------
def test_an_index_note_reaches_a_decide_observation_once_an_index_exists(r1_dir: Path) -> None:
    conn = _db(r1_dir)
    first_index = conn.execute("SELECT MIN(created_tick) AS t FROM notes WHERE tags LIKE '%\"index\"%'").fetchone()["t"]
    assert first_index is not None and int(first_index) > TICKS_PER_DAY, "the first index files are written on day 2"
    hits = _decides_with_index(conn)
    assert hits, "no decide observation retrieved an index note"
    assert all(tick >= int(first_index) for tick, _ in hits)
    agents_with_index = set(_index_titles(conn))
    assert {aid for _, aid in hits} == agents_with_index, "every agent that keeps an index retrieves it at least once"


def test_the_boost_brings_index_notes_into_view_more_often_than_r0(r1_dir: Path, r0_dir: Path) -> None:
    r1, r0 = _db(r1_dir), _db(r0_dir)
    day2 = "SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='decide' AND tick > ?"
    n1, n0 = (int(c.execute(day2, (TICKS_PER_DAY,)).fetchone()["n"]) for c in (r1, r0))
    assert n1 > 0 and n0 > 0
    h1, h0 = len(_decides_with_index(r1)), len(_decides_with_index(r0))
    # the two worlds are identical until the first day-2 retrieval, the only place the boost can act
    first = TICKS_PER_DAY + 1
    q = "SELECT agent_id, extras FROM llm_calls WHERE purpose='decide' AND tick < ? ORDER BY call_id"
    assert [tuple(r) for r in r1.execute(q, (first,))] == [tuple(r) for r in r0.execute(q, (first,))]
    assert h1 / n1 > h0 / n0, (h1, n1, h0, n0)


# --- the other arms: the rule is there, its object is not ----------------------------------------------------------
def test_the_hoarder_holds_no_index_note_so_r1_ranks_as_r0(tmp_path: Path) -> None:
    sim = Simulation(load_config(CONFIGS / "exp_mem_e0_hoarder.yaml", {"run": dict(RUN)}), tmp_path / "hoarder")
    try:
        asyncio.run(sim.run())
        assert sim.cfg.memory.index_boost > 0.0
        conn = _db(tmp_path / "hoarder")
        assert conn.execute("SELECT COUNT(*) AS n FROM memory_commands WHERE purpose='maintain' AND ok=1").fetchone()["n"] > 0
        assert _index_titles(conn) == {}
        assert _decides_with_index(conn) == []
        store, tick = sim.memory, sim.clock.tick
        r1_cfg, r0_cfg = store.cfg, store.cfg.model_copy(update={"index_boost": 0.0})
        agents = [a.agent_id for a in sim.registry.alive()]
        assert agents
        for aid in agents:
            for query in (store.get_self(aid) or "", "Node n1 rich Node n2 thin", "where is food"):
                store.cfg = r1_cfg
                notes1, stats1 = store.retrieve_with_stats(aid, query, tick)
                store.cfg = r0_cfg
                notes0, stats0 = store.retrieve_with_stats(aid, query, tick)
                store.cfg = r1_cfg
                assert [(n.note_id, n.score) for n in notes1] == [(n.note_id, n.score) for n in notes0], (aid, query)
                assert stats1 == stats0 and stats1.get("index_entries", 0) == 0
    finally:
        sim.db.close()
