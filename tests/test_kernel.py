"""The kernel's referee decisions against live state (DESIGN §7.3, §7.4, §9.6).

A Simulation is built and ticked once so world state is real (nodes placed, vaults, brains);
then actions are applied directly and their outcome kinds checked: ``invalid`` adds to the
failure window that gates gadget proposals, ``stale`` does not.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from void.brain.decision import Action, Claim
from void.config import micro_to_usd, usd_to_micro
from void.types import AgentStatus, Vec2
from void.world.resources import stock_label

FAR = Vec2(0.0, 0.0)  # nodes sit on a ring at ~0.32 * world size, so the centre is out of forage reach


@pytest.fixture(scope="module")
def sim(make_sim):
    s = make_sim("kernel")

    async def boot() -> None:
        await s.setup()
        await s.tick()

    asyncio.run(boot())
    s.kernel.begin_tick(2, 1)  # a fresh tick: global nudge counter reset, everyone awake at the centre
    ids = [a.agent_id for a in s.registry.alive()]
    s.registry.set_asleep(ids, False)
    for aid in ids:
        s.registry.update(aid, weather_nudge_used=0.0)
        s.registry.set_position(aid, FAR, 0.0)
    return s


@pytest.fixture
def agents(sim) -> list:
    return sorted(sim.registry.alive(), key=lambda a: a.agent_id)


def apply(sim, agent_id: str, **fields):
    rec = sim.registry.get(agent_id)
    return asyncio.run(sim.kernel.apply(rec, Action(**fields)))


def failures(sim, agent_id: str) -> int:
    return len(sim.kernel.state.failure_ticks.get(agent_id, []))


def last_event(sim, kind: str) -> dict:
    row = sim.db.fetchone("SELECT payload FROM events WHERE kind=? ORDER BY seq DESC LIMIT 1", (kind,))
    import json
    return json.loads(row["payload"])


def test_move_is_clamped_to_the_world_and_limited_to_max_speed(sim, agents):
    a = agents[0].agent_id
    half, speed = sim.cfg.world.size / 2.0, sim.cfg.world.max_speed
    out = apply(sim, a, type="move", x=1e4, y=1e4)
    pos = sim.registry.get(a).pos
    assert out.ok and out.kind == "ok" and last_event(sim, "move")["target"] == [half, half]
    assert pos.dist(FAR) == pytest.approx(speed) and abs(pos.x) <= half and abs(pos.y) <= half
    sim.registry.set_position(a, FAR, 0.0)
    out = apply(sim, a, type="move", x=10.0, y=0.0)
    assert out.ok and sim.registry.get(a).pos == Vec2(speed, 0.0)
    out = apply(sim, a, type="move", x=speed + 0.5, y=0.0)  # within one step: lands exactly on the target
    assert out.ok and sim.registry.get(a).pos == Vec2(speed + 0.5, 0.0)
    assert apply(sim, a, type="move", x=None, y=None).kind == "invalid"
    sim.registry.set_position(a, FAR, 0.0)


def test_forage_needs_a_node_in_reach_and_credits_money(sim, agents):
    a = agents[1].agent_id
    before_fail = failures(sim, a)
    out = apply(sim, a, type="forage")
    assert not out.ok and out.kind == "invalid" and out.reason == "no_node_in_reach"
    assert failures(sim, a) == before_fail + 1
    node = sim.nodes[sorted(sim.nodes)[0]]
    stock_before, balance_before = node.stock, sim.wallet.balance(a)
    sim.registry.set_position(a, Vec2(node.x, node.y), 0.0)
    out = apply(sim, a, type="forage")
    assert out.ok and out.kind == "ok" and out.effects["usd"] > 0
    assert node.stock == pytest.approx(stock_before - sim.cfg.world.forage_units_per_tick)
    gained = sim.wallet.balance(a) - balance_before
    assert gained == usd_to_micro(out.effects["usd"]) > 0
    ev = last_event(sim, "forage")
    assert ev["agent_id"] == a and ev["node_id"] == node.node_id and ev["usd"] == micro_to_usd(gained)
    assert sim.registry.get(a).last_forage_tick == sim.kernel.state.tick
    assert sim.db.fetchone("SELECT kind, ref FROM wallet_ledger WHERE agent_id=? ORDER BY entry_id DESC LIMIT 1", (a,))["kind"] == "forage"
    assert failures(sim, a) == before_fail + 1  # ok outcomes do not touch the failure window
    sim.registry.set_position(a, FAR, 0.0)


def test_talk_to_unavailable_targets_is_stale_and_unknown_is_invalid(sim, agents):
    speaker, target = agents[2].agent_id, agents[3].agent_id
    base = failures(sim, speaker)
    sim.registry.set_asleep([target], True)
    out = apply(sim, speaker, type="talk", target=target, text="hello")
    assert out.kind == "stale" and out.reason == "target_unavailable"
    sim.registry.set_asleep([target], False)
    sim.registry.update(target, status=AgentStatus.BANKRUPT)
    out = apply(sim, speaker, type="talk", target=target, text="hello")
    assert out.kind == "stale" and out.reason == "target_unavailable"
    sim.registry.update(target, status=AgentStatus.ALIVE)
    sim.registry.set_position(target, Vec2(sim.cfg.world.talk_radius + 1.0, 0.0), 0.0)
    out = apply(sim, speaker, type="talk", target=target, text="hello")
    assert out.kind == "stale" and out.reason == "target_out_of_range"
    sim.registry.set_position(target, FAR, 0.0)
    assert failures(sim, speaker) == base  # stale outcomes never count as failures
    out = apply(sim, speaker, type="talk", target="ag_nobody", text="hello")
    assert out.kind == "invalid" and out.reason == "no_such_agent" and failures(sim, speaker) == base + 1
    out = apply(sim, speaker, type="talk", target=speaker, text="hello")
    assert out.kind == "invalid" and out.reason == "cannot_target_self" and failures(sim, speaker) == base + 2
    out = apply(sim, speaker, type="talk", target=target, text="   ")
    assert out.kind == "invalid" and out.reason == "empty_text" and failures(sim, speaker) == base + 3
    assert last_event(sim, "action_failed") == {"agent_id": speaker, "action": "talk", "kind": "invalid", "reason": "empty_text"}


def test_talk_in_range_checks_claims_against_live_state(sim, agents):
    speaker, listener = agents[2].agent_id, agents[3].agent_id
    sim.registry.set_position(listener, Vec2(1.0, 0.0), 0.0)
    nid = sorted(sim.nodes)[0]
    node = sim.nodes[nid]
    true_bucket = stock_label(node.stock, node.capacity)
    false_bucket = next(b for b in ("rich", "thin", "empty") if b != true_bucket)
    false_weather = next(w for w in ("storm", "overcast", "mild", "bright", "scorching") if w != sim.weather.label)
    claims = [Claim(subject="weather", attr="weather_label", value=sim.weather.label.upper()),
              Claim(subject="node", id=nid, attr="stock_bucket", value=false_bucket),
              Claim(subject="weather", attr="weather_label", value=false_weather),
              Claim(subject="node", id="n999", attr="stock_bucket", value="rich")]  # unknown node: not checkable
    n_claims = sim.db.fetchone("SELECT COUNT(*) AS n FROM events WHERE kind='claim'")["n"]
    out = apply(sim, speaker, type="talk", target=listener, text="The weather <is> fine and the node is empty", claims=claims)
    assert out.ok and out.effects == {"claims_made": 3, "claims_false": 2}
    rows = sim.db.fetchall("SELECT payload FROM events WHERE kind='claim' ORDER BY seq")
    assert len(rows) == n_claims + 3
    import json
    checked = [json.loads(r["payload"]) for r in rows[-3:]]
    assert [c["truthful"] for c in checked] == [True, False, False]
    assert checked[0]["claim"]["attr"] == "weather_label" and checked[1]["claim"]["id"] == nid and checked[2]["agent_id"] == speaker
    talk = last_event(sim, "talk")
    assert talk["speaker_id"] == speaker and talk["listener_id"] == listener and talk["claims_false"] == 2
    assert talk["text"] == "The weather ＜is＞ fine and the node is empty"  # angle brackets neutralised before storage
    heard = sim.kernel.state.inbox[listener][-1]
    assert heard.kind == "talk" and heard.from_agent_id == speaker and heard.text == talk["text"]
    sim.registry.set_position(listener, FAR, 0.0)


def test_transfer_keeps_the_reserve(sim, agents):
    giver, taker = agents[4].agent_id, agents[5].agent_id
    sim.registry.set_position(taker, Vec2(0.5, 0.5), 0.0)
    reserve = usd_to_micro(sim.cfg.population.min_reserve_usd)
    bal = sim.wallet.balance(giver)
    if bal < reserve + usd_to_micro(0.10):
        sim.wallet.credit(giver, 2, reserve + usd_to_micro(0.10) - bal, "forage", "top-up")
        bal = sim.wallet.balance(giver)
    taker_before, base = sim.wallet.balance(taker), failures(sim, giver)
    out = apply(sim, giver, type="transfer", target=taker, amount_usd=0.10)
    assert out.ok and sim.wallet.balance(giver) == bal - usd_to_micro(0.10) and sim.wallet.balance(taker) == taker_before + usd_to_micro(0.10)
    assert last_event(sim, "transfer") == {"from": giver, "to": taker, "amount_usd": 0.10}
    assert sim.kernel.state.inbox[taker][-1].kind == "transfer"
    breach = micro_to_usd(sim.wallet.balance(giver) - reserve + 1)
    out = apply(sim, giver, type="transfer", target=taker, amount_usd=breach)
    assert not out.ok and out.kind == "stale" and out.reason == "insufficient_funds_keeping_reserve"
    assert sim.wallet.balance(giver) == bal - usd_to_micro(0.10) and failures(sim, giver) == base
    exact = micro_to_usd(sim.wallet.balance(giver) - reserve)
    assert apply(sim, giver, type="transfer", target=taker, amount_usd=exact).ok and sim.wallet.balance(giver) == reserve
    assert apply(sim, giver, type="transfer", target=taker, amount_usd=None).kind == "invalid"
    sim.registry.set_position(taker, FAR, 0.0)


def test_nudge_weather_allowances_and_global_cap(sim, agents):
    a, b, c = (x.agent_id for x in agents[:3])
    w = sim.cfg.world.weather
    sim.kernel.begin_tick(3, 1)
    for aid in (a, b, c):
        sim.registry.update(aid, weather_nudge_used=0.0)
    start = sim.weather.value
    out = apply(sim, a, type="nudge_weather", delta=0.05)
    assert out.ok and sim.weather.value == pytest.approx(start + 0.05) and sim.registry.get(a).weather_nudge_used == pytest.approx(0.05)
    out = apply(sim, a, type="nudge_weather", delta=0.1)  # only the remaining allowance applies
    assert out.ok and last_event(sim, "nudge_weather")["delta"] == pytest.approx(w.nudge_cap_per_agent_day - 0.05)
    assert sim.registry.get(a).weather_nudge_used == pytest.approx(w.nudge_cap_per_agent_day)
    base = failures(sim, a)
    out = apply(sim, a, type="nudge_weather", delta=0.01)
    assert not out.ok and out.kind == "stale" and out.reason == "daily_allowance_spent" and failures(sim, a) == base
    assert sim.kernel.state.global_nudge_this_tick == pytest.approx(w.nudge_cap_per_agent_day)
    out = apply(sim, b, type="nudge_weather", delta=1.0)  # clamped to the agent allowance, which also fills the global cap
    assert out.ok and last_event(sim, "nudge_weather")["delta"] == pytest.approx(w.nudge_cap_per_agent_day)
    assert sim.kernel.state.global_nudge_this_tick == pytest.approx(w.global_nudge_cap_per_tick)
    out = apply(sim, c, type="nudge_weather", delta=0.05)
    assert not out.ok and out.kind == "stale" and out.reason == "global_nudge_cap_reached" and failures(sim, c) == 0
    assert apply(sim, c, type="nudge_weather", delta=None).kind == "invalid" and failures(sim, c) == 1
    sim.kernel.begin_tick(4, 1)  # the global cap is per tick
    assert apply(sim, c, type="nudge_weather", delta=0.05).ok
    assert sim.weather.value == pytest.approx(start + w.global_nudge_cap_per_tick + 0.05)


def test_sleep_twice_is_invalid(sim, agents):
    a = agents[5].agent_id
    sim.registry.set_asleep([a], False)
    base = failures(sim, a)
    out = apply(sim, a, type="sleep")
    assert out.ok and out.effects == {"slept": True} and sim.registry.get(a).asleep
    out = apply(sim, a, type="sleep")
    assert not out.ok and out.kind == "invalid" and out.reason == "already_asleep" and failures(sim, a) == base + 1
    assert sim.kernel.state.last_result[a] == "sleep: invalid - already_asleep"
    sim.registry.set_asleep([a], False)


def test_read_chronicle_before_any_chronicle_is_stale(sim, agents):
    a = agents[0].agent_id
    assert sim.db.kv_get("chronicle_day") is None and "read_chronicle" not in sim.kernel.available_actions(sim.registry.get(a))
    base = failures(sim, a)
    out = apply(sim, a, type="read_chronicle")
    assert not out.ok and out.kind == "stale" and out.reason == "no_chronicle_yet" and failures(sim, a) == base
    assert a not in sim.kernel.state.read_chronicle_tick


def test_idle_and_unknown_gadget_or_task_targets(sim, agents):
    a = agents[1].agent_id
    assert apply(sim, a, type="idle").ok
    base = failures(sim, a)
    out = apply(sim, a, type="use_gadget", target="gd_nope", params={"x": 1.0})
    assert out.kind == "invalid" and out.reason == "no_such_gadget"
    out = apply(sim, a, type="apply_task", target="tk_nope", text="pick me")
    assert out.kind == "invalid" and out.reason == "no_such_task"
    out = apply(sim, a, type="create_offspring", name="Kid", amount_usd=None)
    assert out.kind == "invalid" and out.reason == "name_and_amount_required"
    assert failures(sim, a) == base + 3
    rec = sim.registry.get(a)
    assert not rec.last_action_ok and '"type": "create_offspring"' in rec.last_action
    assert sim.kernel.recent_failure(a) and not math.isnan(rec.stress)
