"""Bankruptcy, replacement, inheritance and archiving (DESIGN §13, §11.3, §19 step 6)."""

from __future__ import annotations

import json

import pytest
from conftest import NO_PROVIDER_TIERS, SimRun, deep_merge

from void.agents.models import AgentRecord
from void.config import usd_to_micro
from void.memory.notes import parse_note
from void.types import ActionOutcome, AgentStatus

SCARCE = deep_merge(NO_PROVIDER_TIERS, {
    "run": {"days": 3},
    "population": {"starting_balance_usd": 0.12, "spawn_pool_usd": 6.0, "replacement_grant_usd": 0.5, "min_reserve_usd": 0.05},
    "world": {"forage_yield_usd": 0.002},
})


@pytest.fixture(scope="module")
def scarce(run_sim) -> SimRun:
    return run_sim("lifecycle", SCARCE)


def _deaths(run: SimRun) -> list[dict]:
    return run.events("death")


def test_deaths_and_replacement_births(scarce: SimRun):
    deaths = _deaths(scarce)
    assert len(deaths) >= 1 and all(d["payload"]["cause"] in ("gate", "overrun") for d in deaths)
    births = scarce.events("birth")
    replacements = [b for b in births if b["payload"]["kind"] == "replacement"]
    assert len(replacements) >= 1
    for b in replacements:
        assert b["payload"]["generation"] >= 1 and b["payload"]["parent_id"] and b["payload"]["endowment_usd"] == 0.5
    # every replacement is announced on the death that caused it, in the same tick
    for d in deaths:
        rid = d["payload"]["replacement_id"]
        if rid:
            assert any(b["payload"]["agent_id"] == rid and b["tick"] == d["tick"] for b in replacements)
    assert scarce.sim.wallet.ledger_sum(kind="spawn_grant", wallet_id="spawn_pool") == -len(replacements) * usd_to_micro(0.5)


def test_dead_agent_is_archived_in_the_graveyard(scarce: SimRun):
    dead = _deaths(scarce)[0]["payload"]["agent_id"]
    grave = scarce.run_dir / "graveyard" / dead
    assert (grave / "self.md").is_file() and not (scarce.run_dir / "vaults" / dead).exists()
    rows = scarce.db.fetchall("SELECT note_id, path, archived FROM notes WHERE agent_id=?", (dead,))
    assert rows and all(int(r["archived"]) == 1 for r in rows)
    for r in rows:
        assert r["path"].startswith(str(grave)) and parse_note(open(r["path"], encoding="utf-8").read()).note_id == r["note_id"]
    rec = scarce.sim.registry.get(dead)
    assert rec.status == AgentStatus.ARCHIVED and rec.memory_path == str(grave) and rec.asleep
    assert scarce.sim.memory.list_notes(dead) == [] and len(scarce.sim.memory.list_notes(dead, include_archived=True)) == len(rows)


def test_family_tree_has_a_parent_chain(scarce: SimRun):
    tree = scarce.sim.registry.family_tree()
    by_id = {a["agent_id"]: a for a in tree}

    def depth(aid: str) -> int:
        n = 0
        while aid:
            n += 1
            aid = by_id[aid]["parent_id"]
        return n

    assert max(depth(a) for a in by_id) >= 2
    for a in tree:
        if a["parent_id"]:
            assert a["generation"] == by_id[a["parent_id"]]["generation"] + 1 and a["born_tick"] >= by_id[a["parent_id"]]["born_tick"]
    assert [a["agent_id"] for a in tree] == [a["agent_id"] for a in sorted(tree, key=lambda a: (a["born_tick"], a["agent_id"]))]


def test_estate_goes_to_the_spawn_pool_and_status_ends_archived(scarce: SimRun):
    for d in _deaths(scarce):
        aid, tick = d["payload"]["agent_id"], d["tick"]
        row = scarce.db.fetchone("SELECT balance, status, died_tick FROM agents WHERE agent_id=?", (aid,))
        assert int(row["balance"]) == 0 and row["status"] == "archived" and int(row["died_tick"]) == tick
        estate = usd_to_micro(d["payload"]["estate_usd"])
        out = scarce.db.fetchall("SELECT delta, balance_after FROM wallet_ledger WHERE agent_id=? AND kind='estate_out' AND tick=?", (aid, tick))
        inn = scarce.db.fetchall("SELECT delta, payload FROM wallet_ledger WHERE agent_id='spawn_pool' AND kind='estate_in' AND tick=? "
                                 "AND json_extract(payload, '$.from')=?", (tick, aid))
        if estate > 0:
            assert len(out) == 1 and int(out[0]["delta"]) == -estate and int(out[0]["balance_after"]) == 0
            assert len(inn) == 1 and int(inn[0]["delta"]) == estate
        else:
            assert not out and not inn
        # no money moved for the dead agent after its death
        assert scarce.db.fetchone("SELECT COUNT(*) AS n FROM wallet_ledger WHERE agent_id=? AND tick>?", (aid, tick))["n"] == 0
    kinds = scarce.event_kinds()
    assert kinds.get("death", 0) >= 1 and kinds.get("replacement", 0) >= 1
    assert scarce.db.fetchone("SELECT COUNT(*) AS n FROM agents WHERE status='bankrupt'")["n"] == 0  # bankrupt is transient


# --- create_offspring preconditions (unit) -----------------------------------------------------------------------------


def _parent(sim) -> AgentRecord:
    return sorted(sim.registry.alive(), key=lambda a: a.agent_id)[0]


def test_create_offspring_refuses_when_reproduction_is_disabled(make_sim):
    sim = make_sim("offspring_disabled", {"population": {"reproduction_enabled": False}})
    out = sim.lifecycle.create_offspring(_parent(sim), "Kid", usd_to_micro(0.25), 1, 1, sim.hold_min_for()["scripted"])
    assert not out.ok and out.kind == "invalid" and out.reason == "reproduction_disabled"
    assert sim.registry.count_alive() == len(sim.cfg.agents)


def test_create_offspring_is_stale_at_the_population_cap(make_sim):
    sim = make_sim("offspring_cap", {"population": {"cap": 6}})
    out = sim.lifecycle.create_offspring(_parent(sim), "Kid", usd_to_micro(0.25), 1, 1, sim.hold_min_for()["scripted"])
    assert not out.ok and out.kind == "stale" and out.reason == "population_cap"


def test_create_offspring_endowment_and_reserve_rules(make_sim):
    sim = make_sim("offspring_rules", NO_PROVIDER_TIERS)
    hold_min = sim.hold_min_for()["scripted"]
    parent = _parent(sim)
    minimum = hold_min * sim.cfg.population.min_endowment_calls
    too_small = sim.lifecycle.create_offspring(parent, "Kid", minimum - 1, 1, 1, hold_min)
    assert too_small.kind == "invalid" and too_small.reason.startswith("endowment_below_minimum")
    # a legal endowment the parent cannot afford while keeping min_reserve is stale, not invalid
    greedy = sim.lifecycle.create_offspring(parent, "Kid", parent.balance - usd_to_micro(sim.cfg.population.min_reserve_usd) + 1, 1, 1, hold_min)
    assert greedy.kind == "stale" and greedy.reason == "insufficient_reserve_after_endowment"
    assert sim.registry.count_alive() == len(sim.cfg.agents) and sim.wallet.balance(parent.agent_id) == parent.balance


def test_create_offspring_success_inherits_notes_and_debits_exactly_the_endowment(make_sim):
    sim = make_sim("offspring_ok", NO_PROVIDER_TIERS)
    hold_min = sim.hold_min_for()["scripted"]
    parent = _parent(sim)
    note = sim.memory.remember(parent.agent_id, 1, "The north node is rich in the morning. [[Node n1]]", title="North node lore",
                               tags=["resources"], importance=0.9)
    endowment = hold_min * sim.cfg.population.min_endowment_calls
    before = sim.wallet.balance(parent.agent_id)
    births: list[dict] = []
    sim.bus.subscribe(lambda e: births.append(e.payload) if e.kind == "birth" else None)
    out: ActionOutcome = sim.lifecycle.create_offspring(parent, "Kid", endowment, 3, 1, hold_min)
    assert out.ok and out.kind == "ok" and out.effects["child_id"]
    child = sim.registry.get(str(out.effects["child_id"]))
    assert child is not None and child.status == AgentStatus.ALIVE and child.name == "Kid"
    assert child.generation == parent.generation + 1 and child.parent_id == parent.agent_id and child.born_tick == 3
    assert child.balance == endowment and sim.wallet.balance(parent.agent_id) == before - endowment
    assert child.pos.dist(parent.pos) <= 1.5 + 1e-9 and child.weather_nudge_used == parent.weather_nudge_used
    assert sim.lifecycle.population == len(sim.cfg.agents) + 1 == sim.registry.count_alive()
    ledger = sim.db.fetchall("SELECT agent_id, delta, kind FROM wallet_ledger WHERE tick=3 ORDER BY entry_id")
    assert [(r["agent_id"], r["delta"], r["kind"]) for r in ledger] == [
        (parent.agent_id, -endowment, "endowment_out"), (child.agent_id, endowment, "endowment_in")]
    inherited = [n for n in sim.memory.list_notes(child.agent_id) if n.provenance.channel == "inherited"]
    assert inherited and all("inherited" in n.tags for n in inherited)
    lore = next(n for n in inherited if n.title == note.title)
    assert lore.provenance.source_agent_id == parent.agent_id and lore.provenance.origin_note_id == note.note_id
    assert "resources" in lore.tags and lore.provenance.transfer_tick == 3
    assert not any("entity" in n.tags for n in inherited)
    assert sim.memory.get_self(child.agent_id).startswith(f"Child of {parent.name}.")
    assert births[-1]["kind"] == "offspring" and births[-1]["parent_id"] == parent.agent_id and births[-1]["endowment_usd"] == endowment / 1e6
    assert json.loads(sim.db.fetchone("SELECT personality_seed FROM agents WHERE agent_id=?", (child.agent_id,))["personality_seed"])
