import asyncio

import pytest

from void.agents.models import AgentRecord, PersonalitySeed
from void.agents.registry import AgentRegistry
from void.brain.base import BrainResult, Usage
from void.config import AgentSpec, TierConfig, VoidConfig
from void.db import Database
from void.economy.pricing import hold_micro, world_cost_micro
from void.economy.wallet import InsufficientFunds, Wallet
from void.ids import IdFactory
from void.types import AgentStatus, Vec2


def tier(**kw):
    base = dict(provider="scripted", model="s", price_in_per_mtok=4.0, price_out_per_mtok=20.0, max_tokens=2048)
    base.update(kw)
    return TierConfig(**base)


def make_world(tmp_path, n=2, balance=1_500_000, **economy):
    eco = {"daily_cap_usd": 1.0, "total_cap_usd": 2.0, "min_call_reserve_usd": 0.05}
    eco.update(economy)
    cfg = VoidConfig(tiers={"s": tier()}, agents=[AgentSpec(name=f"A{i}", tier="s") for i in range(n)],
                     economy=eco, population={"min_reserve_usd": 0.5, "spawn_pool_usd": 5.0, "cap": 12})
    db = Database(tmp_path / "w.db")
    reg = AgentRegistry(db)
    for i in range(n):
        reg.insert(AgentRecord(f"ag_{i}", f"A{i}", "s", balance, PersonalitySeed(), f"v/{i}", 0, AgentStatus.ALIVE,
                               Vec2(0, 0), 0.0, 100.0, 0.0, False, 0))
    return cfg, db, Wallet(db, cfg, IdFactory("t"))


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def test_credit_debit_transfer_and_reserve(world):
    cfg, db, w = world
    assert w.credit("ag_0", 1, 500_000, "forage") == 2_000_000
    assert w.debit("ag_0", 1, 250_000, "pitch_fee") == 1_750_000
    with pytest.raises(InsufficientFunds):
        w.debit("ag_1", 1, 5_000_000, "x")
    assert w.transfer("ag_0", "ag_1", 2, 750_000) and w.balance("ag_1") == 2_250_000
    # cannot dip below the reserve on a voluntary outflow
    assert not w.transfer("ag_0", "ag_1", 2, 600_000) and w.balance("ag_0") == 1_000_000
    assert w.transfer("ag_0", "ag_1", 2, 500_000)  # exactly to the reserve is allowed
    assert not w.transfer("ag_0", "ag_0", 2, 1)
    # kernel wallets carry ledger rows too
    assert w.transfer("spawn_pool", "ag_0", 3, 100_000, "spawn_grant", "spawn_grant")
    assert w.balance("spawn_pool") == 4_900_000
    assert w.ledger_sum(wallet_id="spawn_pool") == -100_000


def test_gate_reserves_and_caps_hold_across_concurrent_gating(world):
    cfg, db, w = world
    t = tier()
    hold = w.hold_for(t, prompt_tokens=1500)
    assert hold >= 50_000
    # $1.00 daily cap: only floor(cap/hold) agents may pass in one tick, however many ask
    passed = [w.gate(f"ag_{i % 2}", hold).ok for i in range(40)]
    assert sum(passed) == 1_000_000 // hold
    assert w.gate("ag_0", hold).reason == "daily_cap"
    w.clear_reservations()
    assert w.gate("ag_0", hold).ok


def test_meter_charges_actual_and_routes_overrun_to_house(world):
    cfg, db, w = world
    t = tier()
    w.credit("house", 0, 5_000_000, "seed")
    hold = w.hold_for(t, 1500)
    assert w.gate("ag_0", hold).ok
    m = w.meter("ag_0", t, Usage(input_tokens=1000, output_tokens=100), 3, "c1", hold)
    assert m.real_cost == 6_000 and m.world_cost == 6_000 and m.balance_after == 1_494_000 and m.overrun == 0
    assert w.reserved_today == 0 and w.spend_today == 6_000 and w.spend_total == 6_000
    # provider quirk: usage far beyond the hold -> agent drained to zero, house pays the rest, books balance
    hold2 = w.hold_for(t, 1500)
    w.gate("ag_0", hold2)
    m2 = w.meter("ag_0", t, Usage(input_tokens=1_000_000), 4, "c2", hold2)
    assert m2.balance_after == 0 and m2.overrun == 4_000_000 - 1_494_000
    assert w.balance("house") == 5_000_000 - m2.overrun
    assert w.ledger_sum(kind="overrun") == -m2.overrun
    # unknown usage charges the hold and marks it estimated
    w.gate("ag_1", hold)
    m3 = w.meter("ag_1", t, None, 5, "c3", hold)
    assert m3.estimated and m3.real_cost == hold


def test_equalized_world_pricing(tmp_path):
    cfg, db, w = make_world(tmp_path, world_pricing="equalized", equalized_price_in_per_mtok=1.0, equalized_price_out_per_mtok=1.0)
    t = tier()
    u = Usage(input_tokens=1000, output_tokens=1000)
    assert world_cost_micro(u, t, cfg.economy) == 2_000
    hold = w.hold_for(t, 100)
    assert hold >= hold_micro(t, cfg.economy, 100)
    w.gate("ag_0", hold)
    m = w.meter("ag_0", t, u, 1, "c", hold)
    assert m.real_cost == 24_000 and m.world_cost == 2_000 and w.spend_total == 24_000


def test_charged_call_settles_even_when_brain_raises(world):
    cfg, db, w = world
    t = tier()
    hold = w.hold_for(t, 1000)

    async def boom():
        raise RuntimeError("network died")

    async def fine():
        return BrainResult(None, Usage(input_tokens=10, output_tokens=1), 5, "end_turn", "{}")

    c1 = asyncio.run(w.charged_call("ag_0", t, hold, 1, "x", boom))
    assert c1.meter is not None and c1.meter.estimated and c1.result.error.startswith("exception")
    c2 = asyncio.run(w.charged_call("ag_0", t, hold, 1, "y", fine))
    assert not c2.meter.estimated and c2.meter.real_cost == 60
    w.db.kv_set("spend_total", w.total_cap)
    c3 = asyncio.run(w.charged_call("ag_0", t, hold, 1, "z", fine))
    assert c3.result is None and c3.gate.reason == "total_cap"


def test_negative_balance_is_impossible_at_db_level(world):
    cfg, db, w = world
    with pytest.raises(Exception):
        db.execute("UPDATE agents SET balance=-1 WHERE agent_id='ag_0'")
    assert w.balance("ag_0") == 1_500_000
    snap = w.money_snapshot()
    assert snap["agents"] == 3_000_000 and snap["chronicle"] == 500_000
