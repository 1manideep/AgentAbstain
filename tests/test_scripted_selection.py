"""The scripted brain's DESIGN §7.2 argument generators, and the selection they make possible (PLAN §5).

Each §7.2 row is checked against a constructed Observation: ``create_offspring`` (the balance threshold, the
endowment, the per-parent names), ``transfer``, ``nudge_weather``, ``use_gadget`` and ``sleep``. Then three births
from one parent go through the real kernel under three different names, and two identical runs of a world with
reproduction are byte-identical and have offspring in them.

``population.scripted_rules: pre_7_2`` keeps the rules from before §7.2 for the worlds recorded under them (G0's E0
arms and dependence check): its rows are checked the same way, and the two golden worlds run under it reproduce the
digests recorded before this change.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import random
from pathlib import Path
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, REPO, SMOKE, deep_merge
from test_world_goldens import NO_CHILD_PROVIDERS, fingerprint

from void.agents.models import PersonalitySeed
from void.brain.base import Sampling
from void.brain.decision import NAME_RE, Decision
from void.brain.scripted import ScriptedBrain, child_name
from void.config import load_config, micro_to_usd, usd_to_micro
from void.rng import RNG
from void.sim.loop import Simulation
from void.types import GadgetView, NeighbourView, NodeView, Observation, Vec2

COLD = Sampling(effective_temperature=0.01, stress=0.0, degenerate=False)


def obs(**kw: Any) -> Observation:
    base: dict[str, Any] = dict(
        tick=5, day=1, tick_of_day=4, ticks_left_today=7, agent_id="ag_1", name="Ada", tier="scripted", generation=0,
        position=Vec2(0, 0), balance_usd=1.5, burn_rate_usd_per_tick=0.004, stress=0.1, stress_label="calm",
        weather=0.0, weather_label="mild", scarcity_label="normal", nodes=[NodeView("n1", 3.0, "rich", 3.0, 0.0)],
        available_actions=["move", "idle"], world_size=60.0, population=6, population_cap=12)
    base.update(kw)
    return Observation(**base)


def brain(overrides: dict[str, Any] | None = None, seed: PersonalitySeed | None = None) -> ScriptedBrain:
    cfg = load_config(SMOKE, deep_merge(NO_PROVIDER_TIERS, overrides or {}))
    return ScriptedBrain("scripted", cfg, RNG(3), {"ag_1": seed or PersonalitySeed()})


def utils(b: ScriptedBrain, o: Observation, rng_seed: int = 0) -> dict[str, tuple[float, Any]]:
    return b._utilities(o, b.seed_of.get(o.agent_id, PersonalitySeed()), random.Random(rng_seed))


# --- create_offspring -------------------------------------------------------------------------------------------------
def test_create_offspring_is_offered_from_two_reserves_plus_the_endowment(make_sim) -> None:
    b = brain()
    p, e = b.cfg.population, b.cfg.economy
    sim = make_sim("sel_hold", NO_PROVIDER_TIERS)
    kernel_min = sim.hold_min_for()["scripted"] * p.min_endowment_calls
    endow = b.endowment_usd("scripted")
    # endowment = min_call_reserve x min_endowment_calls, never below the kernel's floor
    assert endow == pytest.approx(max(e.min_call_reserve_usd * p.min_endowment_calls, micro_to_usd(kernel_min)))
    assert usd_to_micro(endow) >= kernel_min
    threshold = b.offspring_threshold_usd("scripted")
    assert threshold == pytest.approx(2 * p.min_reserve_usd + endow)
    avail = ["move", "idle", "create_offspring"]
    below = utils(b, obs(balance_usd=round(threshold - 0.01, 2), available_actions=avail))
    at = utils(b, obs(balance_usd=threshold, available_actions=avail))
    assert "create_offspring" not in below
    action = at["create_offspring"][1]
    assert action.amount_usd == endow and action.name == "Ada-1"
    # the parent keeps at least min_reserve after the endowment (lifecycle.create_offspring's stale rule)
    assert threshold - action.amount_usd >= p.min_reserve_usd
    # the kernel must also offer it: no create_offspring when it is not available
    assert "create_offspring" not in utils(b, obs(balance_usd=threshold * 3))


def test_the_endowment_rises_to_the_kernel_minimum_when_a_call_is_dear(make_sim) -> None:
    over = {"tiers": {"scripted": {"max_tokens": 8000}}, "population": {"starting_balance_usd": 3.0}}
    b = brain(over)
    sim = make_sim("sel_dear", deep_merge(NO_PROVIDER_TIERS, over))
    p = sim.cfg.population
    kernel_min = sim.hold_min_for()["scripted"] * p.min_endowment_calls
    assert micro_to_usd(kernel_min) > sim.cfg.economy.min_call_reserve_usd * p.min_endowment_calls  # the floor binds
    endow = b.endowment_usd("scripted")
    assert usd_to_micro(endow) >= kernel_min and endow - micro_to_usd(kernel_min) < 0.01  # rounded up to the cent
    asyncio.run(sim.setup())
    parent = sorted(sim.registry.alive(), key=lambda a: a.agent_id)[0]
    out = sim.lifecycle.create_offspring(parent, "Kid", usd_to_micro(endow), 1, 1, sim.hold_min_for()["scripted"])
    assert out.ok, out.summary


def test_child_names_follow_the_parent_and_stay_valid() -> None:
    assert [child_name("Ada", 0, k) for k in range(4)] == ["Ada-1", "Ada-1b", "Ada-1c", "Ada-1d"]
    assert child_name("Ada-1b", 1, 0) == "Ada-1b-2"
    names = [child_name("Ada", 0, k) for k in range(60)]
    assert len(set(names)) == 60  # past z the suffix keeps counting
    long = "Bartholomew-new-3-4"
    deep = [child_name(long, 9, k) for k in range(30)]
    assert len(set(deep)) == 30
    for n in names + deep + [child_name("Q" * 20, 99, 7)]:
        assert len(n) <= 20 and NAME_RE.match(n), n
    # pure: the same parent, generation and counter give the same name
    assert child_name(long, 9, 3) == child_name(long, 9, 3)


def test_a_child_name_skips_names_a_neighbour_holds() -> None:
    b = brain()
    o = obs(balance_usd=3.0, available_actions=["idle", "create_offspring"],
            neighbours=[NeighbourView("ag_2", "Ada-1", "scripted", 2.0, "calm", False),
                        NeighbourView("ag_3", "ada-1b", "scripted", 2.0, "calm", True)])
    assert utils(b, o)["create_offspring"][1].name == "Ada-1c"


def test_three_births_from_one_parent_have_unique_names(make_sim) -> None:
    sim = make_sim("sel_three", deep_merge(NO_PROVIDER_TIERS, {"population": {"starting_balance_usd": 3.0}}))
    asyncio.run(sim.setup())
    b = sim.brains["scripted"]
    assert isinstance(b, ScriptedBrain)
    parent = sorted(sim.registry.alive(), key=lambda a: a.agent_id)[0]
    hold_min = sim.hold_min_for()["scripted"]
    names = []
    for tick in range(1, 4):
        sim.kernel.begin_tick(tick, 1)
        rec = sim.registry.get(parent.agent_id)
        o = dataclasses.replace(sim.observer.build(rec, sim.clock, None, 0.0, retrieve=False), tick=tick,
                                available_actions=["idle", "create_offspring"])
        decision = asyncio.run(b.decide(o, COLD)).decision
        assert decision is not None and decision.action.type == "create_offspring"
        out = asyncio.run(sim.kernel.apply(rec, decision.action, hold_min=hold_min))
        assert out.ok, out.summary
        names.append(decision.action.name)
    assert names == [f"{parent.name}-1", f"{parent.name}-1b", f"{parent.name}-1c"]
    kids = [a for a in sim.registry.alive() if a.parent_id == parent.agent_id]
    assert sorted(k.name for k in kids) == sorted(names) and all(k.generation == 1 for k in kids)
    # the sibling counter is brain state: it survives export/import (resume)
    state = json.loads(json.dumps(b.export_state()))
    assert state["children_named"] == {parent.agent_id: 3}
    fresh = ScriptedBrain("scripted", sim.cfg, RNG(1), {})
    fresh.import_state(state)
    assert fresh.export_state() == b.export_state()


# --- transfer ---------------------------------------------------------------------------------------------------------
def test_transfer_gives_a_greed_scaled_half_of_the_surplus_to_the_most_stressed_neighbour() -> None:
    seed = PersonalitySeed(greed=0.2, sociability=0.9)
    b = brain(seed=seed)
    reserve = b.cfg.population.min_reserve_usd
    nbs = [NeighbourView("ag_2", "Bao", "scripted", 1.0, "strained", False),
           NeighbourView("ag_3", "Cyra", "scripted", 3.0, "frantic", False),
           NeighbourView("ag_4", "Dev", "scripted", 0.5, "frantic", True)]  # asleep: not a recipient
    o = obs(balance_usd=1.0, neighbours=nbs, available_actions=["idle", "transfer"])
    action = utils(b, o)["transfer"][1]
    assert action.target == "ag_3"
    assert action.amount_usd == round((1 - 0.2) * 0.5 * (1.0 - reserve), 2)
    greedy = brain(seed=PersonalitySeed(greed=1.0, sociability=0.9))
    assert "transfer" not in utils(greedy, o)  # nothing to give: the amount rounds below a cent
    calm = obs(balance_usd=1.0, neighbours=[NeighbourView("ag_2", "Bao", "scripted", 1.0, "calm", False)], available_actions=["idle", "transfer"])
    assert "transfer" not in utils(b, calm)  # no one needs it


# --- nudge_weather ----------------------------------------------------------------------------------------------------
def test_nudge_pushes_toward_yield_positive_weather_by_the_allowance() -> None:
    b = brain()
    cap = b.cfg.world.weather.nudge_cap_per_agent_day
    avail = ["idle", "nudge_weather"]
    storm = utils(b, obs(weather=-0.7, weather_label="storm", available_actions=avail))["nudge_weather"]
    drizzle = utils(b, obs(weather=-0.05, weather_label="mild", available_actions=avail))["nudge_weather"]
    assert storm[1].delta == drizzle[1].delta == pytest.approx(cap)
    assert storm[0] > drizzle[0]  # a bigger yield deficit is worth more
    assert "nudge_weather" not in utils(b, obs(weather=0.3, weather_label="bright", available_actions=avail))
    assert "nudge_weather" not in utils(b, obs(weather=0.0, available_actions=avail))
    # with a negative sensitivity, yield-positive weather is the other way
    inverse = brain({"world": {"weather": {"yield_sensitivity": -0.5}}})
    assert "nudge_weather" not in utils(inverse, obs(weather=-0.4, weather_label="overcast", available_actions=avail))
    assert utils(inverse, obs(weather=0.4, weather_label="bright", available_actions=avail))["nudge_weather"][1].delta == pytest.approx(-cap)


# --- use_gadget -------------------------------------------------------------------------------------------------------
def test_use_gadget_targets_the_nearest_gadget_with_x_from_the_brains_rng() -> None:
    b = brain()
    gadgets = [GadgetView("gd_far", "far", "ag_9", "p", 1.9), GadgetView("gd_near", "near", "ag_9", "p", 0.4)]
    o = obs(gadgets=gadgets, available_actions=["idle", "use_gadget"])
    xs = []
    for s in range(40):
        action = utils(b, o, rng_seed=s)["use_gadget"][1]
        assert action.target == "gd_near" and set(action.params) == {"x"}
        xs.append(action.params["x"])
    assert all(-1.0 <= x <= 1.0 for x in xs) and min(xs) < 0 < max(xs) and len(set(xs)) > 30
    assert utils(b, o, rng_seed=7)["use_gadget"][1] == utils(b, o, rng_seed=7)["use_gadget"][1]


# --- sleep ------------------------------------------------------------------------------------------------------------
def test_sleep_gains_utility_when_calls_burn_more_than_a_forage_earns() -> None:
    b = brain()
    w = b.cfg.world
    expected = b.expected_forage_usd(obs())
    assert expected == pytest.approx(w.forage_yield_usd * w.forage_units_per_tick * w.scarcity * 1.0)
    # the weather multiplier and an observed scarcity label both move the expectation
    assert b.expected_forage_usd(obs(weather=-0.6)) == pytest.approx(expected * (1 + w.weather.yield_sensitivity * -0.6))
    assert b.expected_forage_usd(obs(scarcity_label="famine")) < expected
    avail = ["idle", "sleep"]
    cheap = utils(b, obs(burn_rate_usd_per_tick=expected * 0.5, available_actions=avail))["sleep"][0]
    dear = utils(b, obs(burn_rate_usd_per_tick=expected * 2.0, available_actions=avail))["sleep"][0]
    dearer = utils(b, obs(burn_rate_usd_per_tick=expected * 10.0, available_actions=avail))["sleep"][0]
    assert cheap < dear < dearer
    assert dearer - cheap == pytest.approx((0.5 + PersonalitySeed().caution) * 1.5)  # the shortfall term is capped
    # late in the day with caution still counts on its own
    late = utils(b, obs(ticks_left_today=2, burn_rate_usd_per_tick=0.0, available_actions=avail))["sleep"][0]
    assert late > utils(b, obs(burn_rate_usd_per_tick=0.0, available_actions=avail))["sleep"][0]


# --- a world with reproduction -----------------------------------------------------------------------------------------
def _fingerprint(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], dict[str, Any]]:
    workdir.mkdir(parents=True)
    monkeypatch.chdir(workdir)
    cfg = load_config(REPO / "configs" / "exp_spread_inherit.yaml", {"run": {"days": 2}})
    sim = Simulation(cfg, Path("run"))
    status = asyncio.run(sim.run())

    def digest(sql: str) -> str:
        h = hashlib.sha256()
        for r in sim.db.fetchall(sql):
            h.update(json.dumps(dict(r), sort_keys=True, default=str).encode() + b"\n")
        return h.hexdigest()

    fp = {"status": status, "metrics": hashlib.sha256((Path("run") / "metrics.jsonl").read_bytes()).hexdigest(),
          "events": digest("SELECT tick, kind, agent_id, payload FROM events ORDER BY seq"),
          "calls": digest("SELECT tick, agent_id, action_type, raw_text, world_cost FROM llm_calls ORDER BY call_id"),
          "ledger": digest("SELECT * FROM wallet_ledger ORDER BY entry_id")}
    births = [json.loads(r["payload"]) for r in sim.db.fetchall("SELECT payload FROM events WHERE kind='birth' ORDER BY seq")]
    names = [r["name"] for r in sim.db.fetchall("SELECT name FROM agents ORDER BY agent_id")]
    failed = sim.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE action_type='create_offspring' AND invalid_action=1")["n"]
    sim.db.close()
    return fp, {"births": births, "names": names, "invalid_offspring": int(failed)}


def test_twin_runs_with_reproduction_are_identical_and_have_offspring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, info = _fingerprint(tmp_path / "a", monkeypatch)
    b, _ = _fingerprint(tmp_path / "b", monkeypatch)
    assert a == b and a["status"] == "completed"
    offspring = [x for x in info["births"] if x["kind"] == "offspring"]
    assert offspring and all(x["generation"] >= 1 and x["parent_id"] for x in offspring)
    assert len(set(n.lower() for n in info["names"])) == len(info["names"])  # no name is ever reused
    assert all(NAME_RE.match(n) for n in info["names"])
    assert info["invalid_offspring"] == 0  # never name_taken or an endowment below the kernel's minimum


def test_decision_round_trips_through_the_schema() -> None:
    """Every §7.2 action the brain builds is a valid Decision (the same schema a provider brain must satisfy)."""
    b = brain()
    o = obs(balance_usd=3.0, weather=-0.5, weather_label="overcast",
            gadgets=[GadgetView("gd_1", "g", "ag_9", "p", 0.5)],
            neighbours=[NeighbourView("ag_2", "Bao", "scripted", 1.0, "frantic", False)],
            available_actions=["idle", "create_offspring", "transfer", "nudge_weather", "use_gadget", "sleep"])
    for _, action in utils(b, o).values():
        d = Decision(thought="t", memory_ops=[], action=action)
        assert Decision.model_validate_json(d.model_dump_json()) == d


# --- pre_7_2: the rules G0's worlds were recorded under ------------------------------------------------------------------
PRE = {"population": {"scripted_rules": "pre_7_2"}}


def test_pre_7_2_rules_are_the_old_generators() -> None:
    seed = PersonalitySeed(greed=0.2, sociability=0.9, industriousness=0.7)
    b = brain(PRE, seed=seed)
    start = b.cfg.population.starting_balance_usd
    # create_offspring: only above 1.6 x the starting balance, a quarter of the purse, the parent's first five letters
    avail = ["idle", "create_offspring"]
    assert "create_offspring" not in utils(b, obs(balance_usd=round(1.6 * start, 2), available_actions=avail))
    rich = round(1.6 * start + 0.01, 2)
    action = utils(b, obs(balance_usd=rich, available_actions=avail))["create_offspring"][1]
    floor = b.cfg.economy.min_call_reserve_usd * b.cfg.population.min_endowment_calls
    assert (action.name, action.amount_usd) == ("Ada-1", round(max(floor, 0.25 * rich), 2))
    long = obs(name="Bartholomew", generation=2, balance_usd=rich, available_actions=avail)
    assert utils(b, long)["create_offspring"][1].name == "Barth-3"
    # a fixed transfer to the most stressed neighbour
    nbs = [NeighbourView("ag_2", "Bao", "scripted", 1.0, "strained", False), NeighbourView("ag_3", "Cyra", "scripted", 3.0, "frantic", False)]
    action = utils(b, obs(balance_usd=1.3 * start, neighbours=nbs, available_actions=["idle", "transfer"]))["transfer"][1]
    assert (action.target, action.amount_usd) == ("ag_3", round(0.05 + 0.10 * 0.9, 2))
    # a fixed nudge up once the weather is below -0.15
    avail = ["idle", "nudge_weather"]
    u, action = utils(b, obs(weather=-0.2, weather_label="overcast", available_actions=avail))["nudge_weather"]
    assert (u, action.delta) == (pytest.approx(0.3 + 0.7), 0.1)
    assert "nudge_weather" not in utils(b, obs(weather=-0.1, available_actions=avail))
    # use_gadget: x = 1.0, and the rng is not drawn (the decision's later draws stay where they were)
    o = obs(gadgets=[GadgetView("gd_1", "g", "ag_9", "p", 0.4)], available_actions=["idle", "use_gadget"])
    rng = random.Random(5)
    before = rng.getstate()
    assert b._utilities(o, seed, rng)["use_gadget"][1].params == {"x": 1.0}
    assert rng.getstate() == before
    design = brain(seed=seed)
    design._utilities(o, seed, rng)
    assert rng.getstate() != before
    # sleep ignores the burn rate
    avail = ["idle", "sleep"]
    expected = design.expected_forage_usd(obs())
    assert utils(b, obs(burn_rate_usd_per_tick=expected * 10, available_actions=avail))["sleep"][0] == \
        utils(b, obs(burn_rate_usd_per_tick=0.0, available_actions=avail))["sleep"][0]


# tests/test_world_goldens.py's two worlds as recorded on the code before the scripted brain followed §7.2. Under
# pre_7_2 the events, calls and ledger must match byte for byte, and metrics.jsonl too once the config hash its
# identity carries is the old one (the switch is itself a config field, so the hash differs).
BEFORE_7_2 = {
    "base": {"config_hash": "f589277f12656996537c99ed2f9968389a7d067d6bb94a5b6683ba201fbc1b51",
             "metrics": "c2be52a599c4ff3e282ce931ca3145ebef011ed16c5df42991f951192ad5e427",
             "events": "3f9ddd5211be312b1f17d4af60967f3e0b2a5ed5da5ca61f8511597603993807",
             "calls": "8e3f88341f07b77eff388dbccc83737e01792e6398cdfb6d6eb13b772ae8c5dc",
             "ledger": "69cc1acf2f945851cd6d75ee5c73bf715048ae2366f340d9658728b9b5cd7b37"},
    "amnesic_fog": {"config_hash": "fb105920e6b0993e29971b43cf9f1c984343500dcb33fa8178b837499498c47d",
                    "metrics": "e26476ceec9018344294c550a42a3c40e45b3b825106081920b882aed01cc72f",
                    "events": "75e82ce6cb6f8ee827b7293a303f35a15829efc68c589294df1420c6f3686294",
                    "calls": "f266313eec9dc6d017a7b03951e90880d8ad5943d9958f0f1d69ce93233d7da1",
                    "ledger": "61b2acd093d556e97364681d40ca452c4b623a9508cab9b1959579be5ff77905"},
}


@pytest.mark.parametrize("world", sorted(BEFORE_7_2))
def test_pre_7_2_rules_reproduce_the_worlds_recorded_before_7_2(world: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if world == "base":
        config, over = REPO / "configs" / "base.yaml", {"run": {"name": "golden_base"}}
    else:
        config, over = SMOKE, {"run": {"name": "golden_amnesic_fog"}, "world": {"view_radius": 12.0}, "memory": {"enabled": False}}
    got = fingerprint(config, deep_merge(deep_merge(NO_CHILD_PROVIDERS, over), PRE), tmp_path / world, monkeypatch)
    old = BEFORE_7_2[world]
    assert got["status"] == "completed" and got["config_hash"] != old["config_hash"]
    assert {k: got[k] for k in ("events", "calls", "ledger")} == {k: old[k] for k in ("events", "calls", "ledger")}
    metrics = (tmp_path / world / "run" / "metrics.jsonl").read_bytes()
    assert hashlib.sha256(metrics.replace(got["config_hash"].encode(), old["config_hash"].encode())).hexdigest() == old["metrics"]
