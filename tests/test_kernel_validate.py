"""A failed action is invalid or stale against the agent's observation (DESIGN §5, §7.4).

``Kernel.apply(agent, action, obs=...)`` decides success on live state and classifies a failure against the
observation the decision was made on: ``invalid`` when that observation already showed the action could not work
(it adds the failed_action drain and counts in invalid_action_rate), ``stale`` when it showed the action valid and
an earlier agent this tick changed the world (no drain). Observations here are built by the real observer, then the
world is changed "since" by hand, or they are edited to show what a fogged or out-of-date view would. ``obs=None``
keeps each failure's fixed kind (tests/test_kernel.py).
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, deep_merge

from void.brain import entropy
from void.brain.base import BrainResult, Usage
from void.brain.decision import Action, Decision
from void.config import usd_to_micro
from void.sandbox.registry import GadgetRecord, sha256
from void.types import AgentStatus, GadgetView, NeighbourView, Observation, Vec2

FAR = Vec2(0.0, 0.0)  # nodes sit on a ring at ~0.32 x world size, so the centre is out of forage reach


@pytest.fixture(scope="module")
def sim(make_sim):
    s = make_sim("kernel_validate", NO_PROVIDER_TIERS)

    async def boot() -> None:
        await s.setup()
        await s.tick()

    asyncio.run(boot())
    return s


@pytest.fixture
def ids(sim) -> list[str]:
    """A fresh tick with everyone alive, awake, at the centre, with an unspent nudge allowance and full nodes."""
    sim.kernel.begin_tick(2, 1)
    sim.kernel.state.failure_ticks.clear()  # the failure window holds at most 8 ticks per agent
    out = sorted(a.agent_id for a in sim.registry.all())
    for aid in out:
        sim.registry.update(aid, status=AgentStatus.ALIVE, weather_nudge_used=0.0)
        sim.registry.set_position(aid, FAR, 0.0)
    sim.registry.set_asleep(out, False)
    sim.lifecycle.population = sim.registry.count_alive()
    for node in sim.nodes.values():
        node.stock = node.capacity
    for aid in out:
        set_balance(sim, aid, 1.0)
    return out


def set_balance(sim, aid: str, usd: float) -> None:
    """Move an agent's balance to ``usd`` through the ledger (credited as a forage, or paid to the house)."""
    target, bal = usd_to_micro(usd), sim.wallet.balance(aid)
    if bal < target:
        sim.wallet.credit(aid, sim.kernel.state.tick, target - bal, "forage", "test")
    elif bal > target:
        assert sim.wallet.transfer(aid, "house", sim.kernel.state.tick, bal - target, keep_reserve=False)


def look(sim, aid: str) -> Observation:
    return sim.observer.build(sim.registry.get(aid), sim.clock, None, 0.0, retrieve=False)


def act(sim, aid: str, obs: Observation | None, **fields: Any):
    return asyncio.run(sim.kernel.apply(sim.registry.get(aid), Action(**fields), obs=obs))


def failures(sim, aid: str) -> int:
    return len(sim.kernel.state.failure_ticks.get(aid, []))


def check(sim, aid: str, out, kind: str, reason: str) -> None:
    """The outcome's kind and reason, and its bookkeeping: only invalid adds to the failure window and clears
    last_action_ok (which the next tick's failed_action drain reads)."""
    assert not out.ok and (out.kind, out.reason) == (kind, reason), out.summary
    rec = sim.registry.get(aid)
    assert rec.last_action_ok is (kind == "stale")
    assert sim.kernel.state.last_result[aid].startswith(f"{out.action_type}: {kind} - {reason}")


IN_RANGE = Vec2(1.0, 0.0)


def out_of_range(sim) -> Vec2:
    return Vec2(sim.cfg.world.talk_radius + 1.0, 0.0)


def social(kind: str, target: str) -> dict[str, Any]:
    if kind == "talk":
        return {"type": "talk", "target": target, "text": "hello there"}
    if kind == "transfer":
        return {"type": "transfer", "target": target, "amount_usd": 0.05}
    return {"type": "share_note", "target": target}


SOCIAL = ["talk", "transfer", "share_note"]


# --- talk, transfer, share_note --------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", SOCIAL)
def test_an_agent_the_observation_did_not_list_is_invalid(sim, ids, kind):
    speaker, target = ids[0], ids[1]
    sim.registry.set_position(target, out_of_range(sim), 0.0)
    obs = look(sim, speaker)
    assert target not in {n.agent_id for n in obs.neighbours}
    base = failures(sim, speaker)
    check(sim, speaker, act(sim, speaker, obs, **social(kind, target)), "invalid", "target_out_of_range")
    assert failures(sim, speaker) == base + 1
    sim.registry.set_asleep([target], True)  # unlisted and asleep: still never valid against what the agent saw
    check(sim, speaker, act(sim, speaker, obs, **social(kind, target)), "invalid", "target_unavailable")
    # without the observation the old fixed kind
    assert act(sim, speaker, None, **social(kind, target)).kind == "stale"
    check(sim, speaker, act(sim, speaker, obs, **social(kind, "ag_nobody")), "invalid", "no_such_agent")


@pytest.mark.parametrize("kind", SOCIAL)
def test_a_listed_neighbour_now_asleep_out_of_range_or_dead_is_stale(sim, ids, kind):
    speaker, target = ids[0], ids[1]
    sim.registry.set_position(target, IN_RANGE, 0.0)
    obs = look(sim, speaker)
    seen = next(n for n in obs.neighbours if n.agent_id == target)
    assert not seen.asleep and seen.distance <= sim.cfg.world.talk_radius
    base = failures(sim, speaker)
    sim.registry.set_asleep([target], True)  # fell asleep since the observation
    check(sim, speaker, act(sim, speaker, obs, **social(kind, target)), "stale", "target_unavailable")
    sim.registry.set_asleep([target], False)
    sim.registry.set_position(target, out_of_range(sim), 0.0)  # walked off since
    check(sim, speaker, act(sim, speaker, obs, **social(kind, target)), "stale", "target_out_of_range")
    sim.registry.set_position(target, IN_RANGE, 0.0)
    sim.registry.update(target, status=AgentStatus.BANKRUPT)  # died since: by id, and by name (only the living answer)
    check(sim, speaker, act(sim, speaker, obs, **social(kind, target)), "stale", "target_unavailable")
    check(sim, speaker, act(sim, speaker, obs, **social(kind, seen.name)), "stale", "target_unavailable")
    assert act(sim, speaker, None, **social(kind, seen.name)).kind == "invalid"  # old: no_such_agent
    assert failures(sim, speaker) == base + 1


@pytest.mark.parametrize("kind", SOCIAL)
def test_a_neighbour_the_observation_showed_asleep_or_beyond_earshot_is_invalid(sim, ids, kind):
    speaker, target = ids[0], ids[1]
    sim.registry.set_position(target, IN_RANGE, 0.0)
    sim.registry.set_asleep([target], True)
    obs = look(sim, speaker)
    assert next(n for n in obs.neighbours if n.agent_id == target).asleep
    check(sim, speaker, act(sim, speaker, obs, **social(kind, target)), "invalid", "target_unavailable")
    sim.registry.set_asleep([target], False)
    # under fog the observation lists everyone in view, earshot or not: one shown beyond earshot was never in reach
    far = out_of_range(sim)
    sim.registry.set_position(target, far, 0.0)
    rec = sim.registry.get(target)
    fogged = dataclasses.replace(obs, neighbours=[NeighbourView(target, rec.name, rec.model_tier, far.dist(FAR), "calm", False)])
    check(sim, speaker, act(sim, speaker, fogged, **social(kind, target)), "invalid", "target_out_of_range")


def test_a_reachable_listed_neighbour_succeeds_and_success_is_decided_live(sim, ids):
    speaker, target, stranger = ids[0], ids[1], ids[2]
    sim.registry.set_position(target, IN_RANGE, 0.0)
    sim.registry.set_position(stranger, out_of_range(sim), 0.0)
    obs = look(sim, speaker)
    assert act(sim, speaker, obs, type="talk", target=target, text="hello").ok
    sim.registry.set_position(stranger, Vec2(-1.0, 0.0), 0.0)  # came into earshot after the observation
    out = act(sim, speaker, obs, type="talk", target=stranger, text="hello")
    assert out.ok and sim.registry.get(speaker).last_action_ok


# --- forage ------------------------------------------------------------------------------------------------------------
def test_forage_is_classified_against_the_observed_node(sim, ids):
    a = ids[0]
    node = sim.nodes[sorted(sim.nodes)[0]]
    obs = look(sim, a)  # at the centre: no node in reach
    check(sim, a, act(sim, a, obs, type="forage"), "invalid", "no_node_in_reach")
    sim.registry.set_position(a, Vec2(node.x, node.y), 0.0)
    obs = look(sim, a)
    assert obs.nodes[0].node_id == node.node_id and obs.nodes[0].stock_bucket == "rich" and "forage" in obs.available_actions
    node.stock = 0.0  # an earlier agent emptied it since
    check(sim, a, act(sim, a, obs, type="forage"), "stale", "node_empty")
    obs = look(sim, a)  # the observation shows it empty and offers no forage
    assert obs.nodes[0].stock_bucket == "empty" and "forage" not in obs.available_actions
    check(sim, a, act(sim, a, obs, type="forage"), "invalid", "node_empty")
    assert act(sim, a, None, type="forage").kind == "stale"  # the old fixed kind
    node.stock = 0.03 * node.capacity  # in the empty bucket but with stock left: forage is offered
    obs = look(sim, a)
    assert obs.nodes[0].stock_bucket == "empty" and "forage" in obs.available_actions
    node.stock = 0.0
    check(sim, a, act(sim, a, obs, type="forage"), "stale", "node_empty")


# --- transfer funds ------------------------------------------------------------------------------------------------------
def test_transfer_beyond_the_observed_balance_is_invalid_and_funds_changed_since_are_stale(sim, ids):
    giver, taker = ids[0], ids[1]
    sim.registry.set_position(taker, IN_RANGE, 0.0)
    reserve = sim.cfg.population.min_reserve_usd
    set_balance(sim, giver, reserve + 0.30)
    obs = look(sim, giver)
    base = failures(sim, giver)
    check(sim, giver, act(sim, giver, obs, type="transfer", target=taker, amount_usd=0.31), "invalid",
          "insufficient_funds_keeping_reserve")
    assert act(sim, giver, None, type="transfer", target=taker, amount_usd=0.31).kind == "stale"  # the old fixed kind
    set_balance(sim, giver, reserve + 0.10)  # a call's cost, say, since the observation
    check(sim, giver, act(sim, giver, obs, type="transfer", target=taker, amount_usd=0.20), "stale",
          "insufficient_funds_keeping_reserve")
    assert failures(sim, giver) == base + 1
    assert sim.wallet.balance(giver) == usd_to_micro(reserve + 0.10)


# --- apply_task ---------------------------------------------------------------------------------------------------------
def test_apply_task_is_classified_against_the_observed_board(sim, ids):
    a, rival = ids[0], ids[1]
    board = sim.kernel.taskboard
    assert board is not None
    tick = sim.kernel.state.tick
    before = look(sim, a)  # the board as it was: the task is not on it
    tid = board.post("Mend the path", "Fill the ruts.", 0.40, tick, 1)
    listed = look(sim, a)
    assert next(t for t in listed.tasks if t.task_id == tid).status == "open"
    app = board.apply(rival, tid, "me", tick, 1)
    assert app.ok and board.approve(tid, app.application_id, tick, 1)[0]  # assigned since
    check(sim, a, act(sim, a, before, type="apply_task", target=tid, text="pick me"), "invalid", "task_not_open")
    check(sim, a, act(sim, a, listed, type="apply_task", target=tid, text="pick me"), "stale", "task_not_open")
    shown_assigned = look(sim, a)
    assert next(t for t in shown_assigned.tasks if t.task_id == tid).status == "assigned"
    check(sim, a, act(sim, a, shown_assigned, type="apply_task", target=tid, text="pick me"), "invalid", "task_not_open")
    assert act(sim, a, None, type="apply_task", target=tid, text="pick me").kind == "stale"  # the old fixed kind
    # the pitch fee against the observed balance
    tid2 = board.post("Count the stones", "All of them.", 0.40, tick, 1)
    reserve, fee = sim.cfg.population.min_reserve_usd, sim.cfg.economy.pitch_fee_usd
    set_balance(sim, a, reserve + fee / 2)
    check(sim, a, act(sim, a, look(sim, a), type="apply_task", target=tid2, text="pick me"), "invalid", "cannot_afford_fee")
    set_balance(sim, a, reserve + 0.30)
    obs = look(sim, a)
    set_balance(sim, a, reserve + fee / 2)
    check(sim, a, act(sim, a, obs, type="apply_task", target=tid2, text="pick me"), "stale", "cannot_afford_fee")


# --- use_gadget ----------------------------------------------------------------------------------------------------------
def plant_gadget(sim, gid: str, owner: str, at: Vec2) -> None:
    code = "def run(params):\n    return {'value': 0.0}\n"
    cp, tp = sim.kernel.gadgets.store_files(gid, code, "")
    sim.kernel.gadgets.insert(GadgetRecord(
        gadget_id=gid, name=gid, owner_agent_id=owner, purpose="a test gadget", code_path=str(cp), test_path=str(tp),
        code_hash=sha256(code), test_hash=sha256(""), status="verified", render={}, effect={"kind": "forage_bonus", "value": 0.1},
        verification={}, verified_without_tests=False, template_label=None, x=at.x, y=at.y, created_tick=1))


def test_use_gadget_is_classified_against_the_observed_gadgets(sim, ids, monkeypatch):
    a = ids[0]
    gate, radius = sim.kernel.gate, sim.cfg.world.gadget_radius
    plant_gadget(sim, "gd_near", ids[1], Vec2(0.5, 0.0))
    plant_gadget(sim, "gd_far", ids[1], Vec2(radius + 1.0, 0.0))
    obs = look(sim, a)
    shown = {g.gadget_id: g for g in obs.gadgets}
    assert "gd_near" in shown and "gd_far" in shown and shown["gd_far"].distance > radius
    unlisted = dataclasses.replace(obs, gadgets=[])
    # out of reach: invalid when the observation showed it out of reach or did not show it, stale if it showed it close
    check(sim, a, act(sim, a, obs, type="use_gadget", target="gd_far", params={}), "invalid", "too_far")
    check(sim, a, act(sim, a, unlisted, type="use_gadget", target="gd_far", params={}), "invalid", "too_far")
    close = dataclasses.replace(obs, gadgets=[GadgetView("gd_far", "gd_far", ids[1], "p", 0.5)])
    check(sim, a, act(sim, a, close, type="use_gadget", target="gd_far", params={}), "stale", "too_far")
    assert act(sim, a, None, type="use_gadget", target="gd_far", params={}).kind == "stale"  # the old fixed kind
    # shared sandbox budgets spent by earlier agents this tick: stale for a gadget it saw, invalid for one it did not
    monkeypatch.setattr(gate, "can_use", lambda: (False, "sandbox_busy"))
    check(sim, a, act(sim, a, obs, type="use_gadget", target="gd_near", params={}), "stale", "sandbox_busy")
    check(sim, a, act(sim, a, unlisted, type="use_gadget", target="gd_near", params={}), "invalid", "sandbox_busy")
    monkeypatch.setattr(gate, "can_use", lambda: (True, "ok"))
    # a cooldown is set only by the agent's own last use: it held when the agent looked, so never valid
    sim.kernel.gadgets.record_use("gd_near", a, sim.kernel.state.tick, True, 0.1, 0.1)
    check(sim, a, act(sim, a, obs, type="use_gadget", target="gd_near", params={}), "invalid", "cooldown")
    assert act(sim, a, None, type="use_gadget", target="gd_near", params={}).kind == "stale"  # the old fixed kind
    # the use fee against the observed balance
    reserve, fee = sim.cfg.population.min_reserve_usd, sim.cfg.sandbox.use_fee_usd
    plant_gadget(sim, "gd_fee", ids[1], Vec2(-0.5, 0.0))
    set_balance(sim, a, reserve + fee / 2)
    check(sim, a, act(sim, a, look(sim, a), type="use_gadget", target="gd_fee", params={}), "invalid", "cannot_afford_fee")
    set_balance(sim, a, reserve + 0.30)
    obs = look(sim, a)
    set_balance(sim, a, reserve + fee / 2)
    check(sim, a, act(sim, a, obs, type="use_gadget", target="gd_fee", params={}), "stale", "cannot_afford_fee")


# --- create_offspring ----------------------------------------------------------------------------------------------------
def test_create_offspring_beyond_the_observed_balance_or_population_is_invalid(sim, ids):
    a = ids[0]
    reserve, cap = sim.cfg.population.min_reserve_usd, sim.cfg.population.cap
    set_balance(sim, a, reserve + 0.05)
    obs = look(sim, a)
    base = failures(sim, a)
    check(sim, a, act(sim, a, obs, type="create_offspring", name="Kidlet", amount_usd=0.10), "invalid",
          "insufficient_reserve_after_endowment")
    assert act(sim, a, None, type="create_offspring", name="Kidlet", amount_usd=0.10).kind == "stale"  # the old fixed kind
    set_balance(sim, a, reserve + 0.30)
    obs = look(sim, a)
    set_balance(sim, a, reserve + 0.05)  # spent since the observation
    check(sim, a, act(sim, a, obs, type="create_offspring", name="Kidlet", amount_usd=0.10), "stale",
          "insufficient_reserve_after_endowment")
    set_balance(sim, a, reserve + 0.30)
    obs = look(sim, a)
    assert obs.population < obs.population_cap
    sim.lifecycle.population = cap  # births by earlier agents this tick filled the world
    check(sim, a, act(sim, a, obs, type="create_offspring", name="Kidlet", amount_usd=0.10), "stale", "population_cap")
    full = dataclasses.replace(obs, population=cap)
    check(sim, a, act(sim, a, full, type="create_offspring", name="Kidlet", amount_usd=0.10), "invalid", "population_cap")
    assert failures(sim, a) == base + 2
    assert not any(r.name == "Kidlet" for r in sim.registry.all())


# --- nudge_weather, read_chronicle, propose_gadget --------------------------------------------------------------------------
def test_nudge_weather_with_the_allowance_spent_per_obs_is_invalid(sim, ids):
    a, b = ids[0], ids[1]
    w = sim.cfg.world.weather
    sim.registry.update(a, weather_nudge_used=w.nudge_cap_per_agent_day)
    obs = look(sim, a)
    assert "nudge_weather" not in obs.available_actions
    check(sim, a, act(sim, a, obs, type="nudge_weather", delta=0.05), "invalid", "daily_allowance_spent")
    assert act(sim, a, None, type="nudge_weather", delta=0.05).kind == "stale"  # the old fixed kind
    sim.kernel.state.global_nudge_this_tick = w.global_nudge_cap_per_tick  # earlier agents filled the global cap...
    check(sim, a, act(sim, a, obs, type="nudge_weather", delta=0.05), "invalid", "daily_allowance_spent")  # ...moot
    obs_b = look(sim, b)
    assert "nudge_weather" in obs_b.available_actions
    check(sim, b, act(sim, b, obs_b, type="nudge_weather", delta=0.05), "stale", "global_nudge_cap_reached")


def test_read_chronicle_or_propose_gadget_the_observation_did_not_offer_is_invalid(sim, ids, monkeypatch):
    a = ids[0]
    obs = look(sim, a)
    assert "read_chronicle" not in obs.available_actions  # no chronicle has been written yet
    check(sim, a, act(sim, a, obs, type="read_chronicle"), "invalid", "no_chronicle_yet")
    offered = dataclasses.replace(obs, available_actions=[*obs.available_actions, "read_chronicle"])
    check(sim, a, act(sim, a, offered, type="read_chronicle"), "stale", "no_chronicle_yet")
    assert act(sim, a, None, type="read_chronicle").kind == "stale"  # the old fixed kind
    gadget = {"type": "propose_gadget", "name": "lamp", "text": "a lamp", "code": "x = 1\n", "tests": "import gadget\n"}
    monkeypatch.setattr(sim.kernel.gate, "can_propose", lambda agent_id, *, recent_failure: (False, "proposals_per_day"))
    obs = look(sim, a)
    assert "propose_gadget" not in obs.available_actions
    check(sim, a, act(sim, a, obs, **gadget), "invalid", "proposals_per_day")
    offered = dataclasses.replace(obs, available_actions=[*obs.available_actions, "propose_gadget"])
    check(sim, a, act(sim, a, offered, **gadget), "stale", "proposals_per_day")


# --- through the loop: the observation is passed and only invalid drains ---------------------------------------------------
def test_the_loop_classifies_against_the_tick_observation_and_drains_only_invalid(make_sim, monkeypatch):
    sim = make_sim("kernel_validate_loop", deep_merge(NO_PROVIDER_TIERS, {"entropy": {"degeneration": {"mode": "observe"}}}))
    asyncio.run(sim.setup())
    asyncio.run(sim.tick())
    order = sorted(a.agent_id for a in sim.registry.alive())
    a, b, c = order[:3]
    node = sim.nodes[sorted(sim.nodes)[0]]
    sim.registry.set_position(a, FAR, 0.0)
    sim.registry.set_position(c, Vec2(sim.cfg.world.talk_radius + 5.0, 0.0), 0.0)
    sim.registry.set_position(b, Vec2(node.x, node.y), 0.0)
    node.stock = node.capacity
    sim.registry.set_asleep(order, False)
    sim.registry.set_asleep([x for x in order if x not in (a, b)], True)  # only a and b act
    for x in (a, b):
        set_balance(sim, x, sim.cfg.population.starting_balance_usd)

    seen: dict[str, Observation | None] = {}
    orig_apply = sim.kernel.apply

    async def spy_apply(agent, action, *, obs=None, hold_min=0):
        seen[agent.agent_id] = obs
        return await orig_apply(agent, action, obs=obs, hold_min=hold_min)

    monkeypatch.setattr(sim.kernel, "apply", spy_apply)
    plan: dict[str, Action] = {}

    async def decide(obs, sampling):
        if obs.agent_id == b and plan.get(b) is not None and plan[b].type == "forage":
            node.stock = 0.0  # an earlier forager empties b's node between b's observation and its turn
        return BrainResult(Decision(thought="planned", memory_ops=[], action=plan.get(obs.agent_id, Action.idle())), Usage(), 0, "end_turn", "{}")

    for brain in sim.brains.values():
        monkeypatch.setattr(brain, "decide", decide)
    # tick 2: a talks to c, asleep and out of earshot, so not in a's observation (invalid; the old kind was stale);
    # b forages the node its observation showed rich, emptied since (stale)
    plan.update({a: Action(type="talk", target=c, text="hello"), b: Action(type="forage")})
    report = asyncio.run(sim.tick())
    tick = report.tick
    assert seen[a] is not None and seen[a].agent_id == a and seen[a].tick == tick and c not in {n.agent_id for n in seen[a].neighbours}
    assert seen[b] is not None and seen[b].agent_id == b and seen[b].nodes[0].stock_bucket == "rich"
    failed = {r["agent_id"]: r for r in sim.db.fetchall(
        "SELECT agent_id, json_extract(payload,'$.kind') AS kind, json_extract(payload,'$.reason') AS reason FROM events "
        "WHERE kind='action_failed' AND tick=?", (tick,))}
    assert (failed[a]["kind"], failed[a]["reason"]) == ("invalid", "target_unavailable")
    assert (failed[b]["kind"], failed[b]["reason"]) == ("stale", "node_empty")
    calls = {r["agent_id"]: r for r in sim.db.fetchall(
        "SELECT agent_id, invalid_action, stale_action FROM llm_calls WHERE purpose='decide' AND tick=?", (tick,))}
    assert (calls[a]["invalid_action"], calls[a]["stale_action"]) == (1, 0)
    assert (calls[b]["invalid_action"], calls[b]["stale_action"]) == (0, 1)
    assert not sim.registry.get(a).last_action_ok and sim.registry.get(b).last_action_ok
    # tick 3: the failed_action drain reaches a (invalid) and not b (stale); balances tell their inputs apart
    start = sim.cfg.population.starting_balance_usd
    set_balance(sim, a, 2.0 * start)
    set_balance(sim, b, 0.5 * start)
    plan.clear()
    inputs: list[entropy.DrainInputs] = []
    orig_step = entropy.step

    def spy_step(cfg, tier, budget, inp, rng):
        inputs.append(inp)
        return orig_step(cfg, tier, budget, inp, rng)

    monkeypatch.setattr(entropy, "step", spy_step)
    asyncio.run(sim.tick())
    inp_a = next(i for i in inputs if i.balance_ratio > 1.5)
    inp_b = next(i for i in inputs if i.balance_ratio < 0.75)
    assert inp_a.last_action_failed and not inp_b.last_action_failed
    d = sim.cfg.entropy
    assert entropy.drain(d, inp_a) - entropy.drain(d, dataclasses.replace(inp_a, last_action_failed=False)) == pytest.approx(d.drain.failed_action)
