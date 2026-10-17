"""Metrics rows (DESIGN §17, §16, §10; MEMORY_EVOLUTION §5.10): the full by_tier / by_generation fields, the tick
gadget fields, per-call stress bins and the day vault structure from :mod:`void.memory.vault_stats`."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, REPO, SMOKE, deep_merge

from void.agents.models import PersonalitySeed
from void.agents.registry import AgentRegistry
from void.config import MemoryConfig, load_config
from void.db import Database
from void.ids import IdFactory
from void.memory.notes import CHANNELS, render_note
from void.memory.store import MemoryStore
from void.memory.vault_stats import (
    INDEX_TAG,
    VAULT_KEYS,
    agent_vault_stats,
    resolve_note_path,
    vault_report,
    vault_stats,
)
from void.research.analysis import end_of_run_outcome
from void.sim.metrics import MetricsRecorder, stress_bin

GROUP_KEYS = {"gossip_transfers", "notes_by_channel", "tracer_reach_by_channel", "tracer_adopters", "gadget_proposals",
              "gadgets_verified", "gadget_use_failure_rate", "probe_recall", "stress_bins"}
BIN_KEYS = {"lo", "calls", "coherence_sum", "coherence_n", "invalid_sum"}
TICK = 5


# --- a hand-built world database ----------------------------------------------------------------------------------
def _agent(db: Database, agent_id: str, tier: str, generation: int = 0, status: str = "alive") -> None:
    db.execute(
        "INSERT INTO agents(agent_id, name, model_tier, balance, personality_seed, memory_path, generation, status, x, y, "
        "entropy_budget, born_tick, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, agent_id.upper(), tier, 500_000, PersonalitySeed().to_json(), f"vaults/{agent_id}", generation, status,
         0.0, 0.0, 1.0, 0, "tick"),
    )


def _call(db: Database, call_id: str, agent_id: str, tier: str, stress: float | None, coherence: float | None,
          invalid: int, *, tick: int = TICK, purpose: str = "decide") -> None:
    db.execute(
        "INSERT INTO llm_calls(call_id, tick, agent_id, purpose, tier, model, provider, real_cost, world_cost, stress, "
        "text_coherence, invalid_action) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (call_id, tick, agent_id, purpose, tier, "scripted", "scripted", 0, 0, stress, coherence, invalid),
    )


def _gadget(db: Database, gadget_id: str, owner: str, status: str, created_tick: int) -> None:
    db.execute(
        "INSERT INTO gadgets(gadget_id, name, owner_agent_id, purpose, code_path, test_path, code_hash, test_hash, status, "
        "render_spec, effect_spec, verification, created_tick) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (gadget_id, gadget_id, owner, "test", "c.py", "t.py", "h", "h", status, "{}", "{}", "{}", created_tick),
    )


def _note_row(db: Database, note_id: str, agent_id: str, channel: str, archived: int = 0) -> None:
    db.execute("INSERT INTO notes(note_id, agent_id, path, title, created_tick, embedding, channel, archived) "
               "VALUES(?,?,?,?,?,?,?,?)", (note_id, agent_id, f"/nowhere/{note_id}.md", note_id, 1, b"", channel, archived))


def _record(tmp_path: Path, build, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = load_config(SMOKE, overrides or {})
    db = Database(tmp_path / "world.db")
    build(db)
    rec = MetricsRecorder(cfg, db, AgentRegistry(db), "run_test", tmp_path / "metrics.jsonl")
    try:
        return rec.record_tick(TICK, 1)
    finally:
        rec.close()
        db.close()


def _empty_bins(row_bins: list[dict[str, Any]], skip: set[int]) -> bool:
    return all(b["calls"] == 0 and b["coherence_n"] == 0 and b["invalid_sum"] == 0 and b["coherence_sum"] == 0.0
               for i, b in enumerate(row_bins) if i not in skip)


# --- stress bins --------------------------------------------------------------------------------------------------
def test_stress_bin_formula() -> None:
    assert [stress_bin(s) for s in (-0.5, 0.0, 0.0999, 0.1, 0.45, 0.9, 0.999, 1.0, 3.0)] == [0, 0, 0, 1, 4, 9, 9, 9, 9]


def test_stress_bins_bin_each_call_by_its_own_stress(tmp_path: Path) -> None:
    def build(db: Database) -> None:
        for aid in ("a1", "a2", "a3"):
            _agent(db, aid, "alpha")
        _call(db, "c1", "a1", "alpha", 0.1, 0.9, 0)    # coherent, low stress
        _call(db, "c2", "a2", "alpha", 0.9, 0.2, 1)    # incoherent and invalid, high stress
        _call(db, "c3", "a3", "alpha", 0.95, None, 1)  # no coherence measured: counted, not in the coherence sums
        _call(db, "m1", "a1", "alpha", 0.5, 0.5, 1, purpose="maintain")  # not a decide call
        _call(db, "c0", "a1", "alpha", 0.5, 0.5, 1, tick=TICK - 1)       # another tick

    row = _record(tmp_path, build)
    bins = row["by_tier"]["alpha"]["stress_bins"]
    assert len(bins) == 10 and [b["lo"] for b in bins] == [round(i / 10, 1) for i in range(10)]
    assert all(set(b) == BIN_KEYS for b in bins)
    assert bins[1] == {"lo": 0.1, "calls": 1, "coherence_sum": 0.9, "coherence_n": 1, "invalid_sum": 0}
    assert bins[9] == {"lo": 0.9, "calls": 2, "coherence_sum": 0.2, "coherence_n": 1, "invalid_sum": 2}
    assert _empty_bins(bins, {1, 9})
    # the agents' current mean stress (0 here) plays no part: only each call's own stress does
    assert row["by_tier"]["alpha"]["mean_stress"] == 0.0
    assert row["by_generation"]["0"]["stress_bins"] == bins


# --- the other group fields and the tick gadget fields -----------------------------------------------------------
def _world(db: Database) -> None:
    _agent(db, "a1", "alpha", generation=0)
    _agent(db, "a2", "beta", generation=1)
    _agent(db, "a3", "beta", generation=1, status="bankrupt")
    _call(db, "c1", "a1", "alpha", 0.2, 0.8, 0)
    _call(db, "c2", "a2", "beta", 0.3, 0.7, 0)
    # gadgets: one proposed now by a1, one verified earlier by a2, one verified by the dead a3 (still beta)
    _gadget(db, "g1", "a1", "proposed", TICK)
    _gadget(db, "g2", "a2", "verified", TICK - 3)
    _gadget(db, "g3", "a3", "verified", TICK - 4)
    for gid, user, ok, tick in (("g2", "a1", 1, 2), ("g2", "a1", 0, 3), ("g2", "a2", 1, 4)):
        db.execute("INSERT INTO gadget_uses(gadget_id, agent_id, tick, ok) VALUES(?,?,?,?)", (gid, user, tick, ok))
    # gossip: a2 receives twice this tick, a1 once on an earlier tick
    for tick, speaker, listener in ((TICK, "a1", "a2"), (TICK, "a1", "a2"), (TICK - 1, "a2", "a1")):
        db.execute("INSERT INTO events(tick, day, kind, agent_id, payload) VALUES(?,?,?,?,?)",
                   (tick, 1, "gossip_transfer", listener, json.dumps({"speaker_id": speaker, "listener_id": listener})))
    # notes: live ones count for living agents only
    _note_row(db, "n1", "a1", "observed")
    _note_row(db, "n2", "a1", "gossip")
    _note_row(db, "n3", "a1", "observed", archived=1)
    _note_row(db, "n4", "a2", "chronicle")
    _note_row(db, "n5", "a3", "observed")
    for aid, ch in (("a1", "tracer"), ("a2", "gossip"), ("a3", "gossip")):
        db.execute("INSERT INTO tracer_arrivals(agent_id, tick, channel, hop) VALUES(?,?,?,?)", (aid, 1, ch, 0))


def test_group_fields_without_the_tracer(tmp_path: Path) -> None:
    row = _record(tmp_path, _world)
    assert row["gadget_proposals"] == 1 and row["gadgets_verified"] == 2
    assert row["gadget_use_failure_rate"] == round(1 / 3, 4)
    alpha, beta = row["by_tier"]["alpha"], row["by_tier"]["beta"]
    assert set(row["by_tier"]) == {"alpha", "beta"} and GROUP_KEYS <= set(alpha) and GROUP_KEYS <= set(beta)
    assert (alpha["gossip_transfers"], beta["gossip_transfers"]) == (0, 2)
    assert alpha["notes_by_channel"] == {**{c: 0 for c in CHANNELS}, "observed": 1, "gossip": 1}
    assert beta["notes_by_channel"] == {**{c: 0 for c in CHANNELS}, "chronicle": 1}
    assert alpha["tracer_reach_by_channel"] is None and beta["tracer_reach_by_channel"] is None
    assert (alpha["gadget_proposals"], alpha["gadgets_verified"], alpha["gadget_use_failure_rate"]) == (1, 0, 0.5)
    assert (beta["gadget_proposals"], beta["gadgets_verified"], beta["gadget_use_failure_rate"]) == (0, 2, 0.0)
    for agg in (alpha, beta):
        assert agg["tracer_adopters"] is None and agg["probe_recall"] is None
    gen = row["by_generation"]
    assert set(gen) == {"0", "1"}
    assert (gen["0"]["gossip_transfers"], gen["1"]["gossip_transfers"]) == (0, 2)
    assert (gen["1"]["gadgets_verified"], gen["1"]["gadget_use_failure_rate"]) == (2, 0.0)


def test_group_fields_with_the_tracer(tmp_path: Path) -> None:
    row = _record(tmp_path, _world, {"experiment": {"tracer": {"enabled": True}}})
    assert row["by_tier"]["alpha"]["tracer_reach_by_channel"] == {"tracer": 1}
    assert row["by_tier"]["beta"]["tracer_reach_by_channel"] == {"gossip": 1}  # the dead a3 is not reached any more
    assert row["by_generation"]["0"]["tracer_reach_by_channel"] == {"tracer": 1}


def test_a_tick_without_gadget_uses_has_no_failure_rate(tmp_path: Path) -> None:
    row = _record(tmp_path, lambda db: _agent(db, "a1", "alpha"))
    assert (row["gadget_proposals"], row["gadgets_verified"], row["gadget_use_failure_rate"]) == (0, 0, None)
    agg = row["by_tier"]["alpha"]
    assert agg["gadget_use_failure_rate"] is None and agg["calls"] == 0 and _empty_bins(agg["stress_bins"], set())


# --- vault_stats on hand-built vaults ----------------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path: Path):
    db = Database(tmp_path / "run" / "world.db")
    mem = MemoryStore(db, MemoryConfig(), IdFactory("vault_stats_test"), tmp_path / "run" / "vaults")
    yield mem
    db.close()


def _write(store: MemoryStore, agent_id: str, title: str, text: str, tags: tuple[str, ...] = ()) -> Any:
    return store.remember(agent_id, 1, text, title=title, tags=list(tags), auto_link=False)


def _bytes(notes: list[Any]) -> int:
    return sum(len(render_note(n).encode("utf-8")) for n in notes)


def test_vault_stats_index_two_notes_and_an_orphan(store: MemoryStore) -> None:
    notes = [
        _write(store, "a1", "Alpha", "alpha body"),
        _write(store, "a1", "Beta", "beta body"),
        _write(store, "a1", "Orphan", "lonely"),
        _write(store, "a1", "Index", "[[Alpha]] and [[Beta]]", (INDEX_TAG,)),
    ]
    stats = vault_stats(store.db, ["a1"])
    assert stats == {
        "notes": 4,
        "bytes": _bytes(notes),
        "mean_note_chars": round((10 + 9 + 6 + 22) / 4, 4),
        "link_density": 0.5,     # two resolved links over four notes
        "orphan_rate": 0.25,     # Orphan has no link either way
        "index_coverage": 0.6667,  # Alpha and Beta of the three non-index notes
    }
    assert tuple(sorted(stats)) == tuple(sorted(VAULT_KEYS))
    assert agent_vault_stats(store.db, ["a1"]) == {"a1": stats} == agent_vault_stats(store.db, "a1")


def test_vault_stats_two_hops_either_direction_stubs_and_dangling_links(store: MemoryStore) -> None:
    store.ensure_entity("a1", "Bao", 1, "An inhabitant.")  # an entity stub: not counted, links to it do not resolve
    _write(store, "a1", "Index", "start at [[Alpha]]", (INDEX_TAG,))
    _write(store, "a1", "Alpha", "alpha")
    _write(store, "a1", "Beta", "see [[alpha]] and [[Beta]]")          # case-insensitive; a self-link does not count
    _write(store, "a1", "Gamma", "see [[Beta]], [[Bao]] and [[Nowhere]]")  # three hops from the index
    stats = vault_stats(store.db, ["a1"])
    assert stats["notes"] == 4
    assert stats["link_density"] == 0.75  # Index->Alpha, Beta->Alpha, Gamma->Beta
    assert stats["orphan_rate"] == 0.0
    assert stats["index_coverage"] == 0.6667  # Alpha (1 hop) and Beta (2 hops, against the link) but not Gamma


def test_vault_stats_pool_agents_and_handle_missing_vaults(store: MemoryStore) -> None:
    _write(store, "a1", "Index", "[[Alpha]]", (INDEX_TAG,))
    _write(store, "a1", "Alpha", "alpha")
    _write(store, "a2", "One", "one [[Two]]")
    _write(store, "a2", "Two", "two")
    _write(store, "a2", "Three", "three")
    population, by_agent = vault_report(store.db, ["a2", "a1", "a3"])
    assert list(by_agent) == ["a1", "a2", "a3"]
    assert by_agent["a1"]["index_coverage"] == 1.0
    assert by_agent["a2"]["index_coverage"] is None and by_agent["a2"]["orphan_rate"] == round(1 / 3, 4)
    assert by_agent["a3"] == {"notes": 0, "bytes": 0, "mean_note_chars": None, "link_density": None,
                              "orphan_rate": None, "index_coverage": None}
    assert population["notes"] == 5 and population["link_density"] == 0.4
    assert population["bytes"] == by_agent["a1"]["bytes"] + by_agent["a2"]["bytes"]
    assert population["orphan_rate"] == 0.2
    assert population["index_coverage"] == 0.25  # one of the four non-index notes sits under an index note
    assert vault_stats(store.db, ["a2"])["index_coverage"] is None


def test_vault_stats_read_a_plain_connection_and_a_moved_run(store: MemoryStore, tmp_path: Path) -> None:
    _write(store, "a1", "Index", "[[Alpha]]", (INDEX_TAG,))
    _write(store, "a1", "Alpha", "alpha")
    want = vault_stats(store.db, ["a1"])
    moved = tmp_path / "moved"
    shutil.copytree(tmp_path / "run" / "vaults", moved / "vaults")
    shutil.rmtree(tmp_path / "run" / "vaults")
    conn = sqlite3.connect(str(tmp_path / "run" / "world.db"))  # no row factory: rows index by position
    try:
        assert vault_stats(conn, ["a1"], run_dir=moved) == want
        gone = vault_stats(conn, ["a1"])
        assert gone["bytes"] == 0 and gone["notes"] == 2 and gone["link_density"] == want["link_density"]
    finally:
        conn.close()
    path = str(store.db.fetchone("SELECT path FROM notes WHERE title='Alpha'")["path"])
    assert resolve_note_path(path, moved) == moved / "vaults" / Path(*Path(path).parts[-3:])
    assert resolve_note_path(path) == Path(path)


def test_vault_stats_import_nothing_from_the_simulation() -> None:
    # the command layer is imported only after the sim-free check, to confirm the local INDEX_TAG matches it
    code = ("import sys, void.memory.vault_stats as v; "
            "bad = sorted(m for m in sys.modules if m.startswith(('void.sim', 'void.world', 'void.brain'))); "
            "assert not bad, bad; from void.memory.commands import INDEX_TAG; assert v.INDEX_TAG == INDEX_TAG")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]


# --- a scripted run -----------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def scripted(run_sim):
    over = deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 2}, "experiment": {"tracer": {"enabled": True, "day": 1}}})
    return run_sim("metrics_rows", over)


def test_scripted_tick_rows_carry_every_group_field(scripted) -> None:
    rows = scripted.tick_rows()
    assert rows and scripted.status == "completed"
    for row in rows:
        assert {"gadget_proposals", "gadgets_verified", "gadget_use_failure_rate"} <= set(row)
        for key in ("by_tier", "by_generation"):
            assert row[key]
            for agg in row[key].values():
                assert GROUP_KEYS <= set(agg), sorted(GROUP_KEYS - set(agg))
                assert set(agg["notes_by_channel"]) >= set(CHANNELS)
                assert isinstance(agg["tracer_reach_by_channel"], dict)
                assert agg["tracer_adopters"] is None and agg["probe_recall"] is None
                assert len(agg["stress_bins"]) == 10
                assert sum(b["calls"] for b in agg["stress_bins"]) == agg["calls"]  # every decide call has a stress
            assert sum(a["gossip_transfers"] for a in row[key].values()) == row["gossip_transfers"]
    assert sum(r["gossip_transfers"] for r in rows) > 0
    assert sum(b["coherence_n"] for r in rows for a in r["by_tier"].values() for b in a["stress_bins"]) > 0
    last = rows[-1]
    alive = {a.agent_id for a in scripted.sim.registry.alive()}
    arrivals = [r for r in scripted.db.fetchall("SELECT agent_id FROM tracer_arrivals") if r["agent_id"] in alive]
    assert sum(sum(a["tracer_reach_by_channel"].values()) for a in last["by_tier"].values()) == len(arrivals) > 0
    live = sum(1 for r in scripted.db.fetchall("SELECT agent_id FROM notes WHERE archived=0") if r["agent_id"] in alive)
    assert sum(sum(a["notes_by_channel"].values()) for a in last["by_tier"].values()) == live


def test_scripted_day_rows_carry_the_vault(scripted) -> None:
    days = scripted.day_rows()
    assert [d["day"] for d in days] == [1, 2]
    for d in days:
        assert set(d["vault"]) == set(VAULT_KEYS) and d["vault"]["notes"] > 0
        assert isinstance(d["link_density"], float)
        assert all(set(v) == set(VAULT_KEYS) for v in d["vault_by_agent"].values())
        assert sum(v["notes"] for v in d["vault_by_agent"].values()) == d["vault"]["notes"]
    last = days[-1]
    alive = sorted(a.agent_id for a in scripted.sim.registry.alive())
    assert sorted(last["vault_by_agent"]) == alive
    population, by_agent = vault_report(scripted.db, alive)
    assert last["vault"] == population and last["vault_by_agent"] == by_agent
    # DESIGN §10's link density is MemoryStore.link_density averaged over the living agents
    assert last["link_density"] == round(sum(scripted.sim.memory.link_density(a) for a in alive) / len(alive), 4)


def test_new_paths_resolve_as_outcomes(scripted) -> None:
    assert end_of_run_outcome(scripted.run_dir, "by_generation.0.mean_balance") is not None
    assert end_of_run_outcome(scripted.run_dir, "day.vault.notes") == float(scripted.day_rows()[-1]["vault"]["notes"])
    assert end_of_run_outcome(scripted.run_dir, "day.link_density") is not None
    assert end_of_run_outcome(scripted.run_dir, "by_tier.scripted.stress_bins.0.calls") is not None
