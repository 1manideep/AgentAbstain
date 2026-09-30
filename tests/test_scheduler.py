from void.agents.models import AgentRecord, PersonalitySeed
from void.agents.registry import AgentRegistry
from void.config import AgentSpec, TierConfig, VoidConfig
from void.db import Database
from void.economy.scheduler import Clock, Scheduler
from void.types import AgentStatus, Vec2


def test_clock_days_and_ticks():
    c = Clock(ticks_per_day=4)
    seq = [c.advance() for _ in range(9)]
    assert seq == [True, False, False, False, True, False, False, False, True]
    assert c.tick == 9 and c.day == 3 and c.tick_of_day == 0 and c.ticks_left_today == 3
    c2 = Clock(ticks_per_day=4, tick=4, day=1)
    assert c2.is_day_end and c2.ticks_left_today == 0


def test_scheduler_wake_sleep(tmp_path):
    cfg = VoidConfig(tiers={"s": TierConfig(provider="scripted", model="s", price_in_per_mtok=1, price_out_per_mtok=1)},
                     agents=[AgentSpec(name="A", tier="s"), AgentSpec(name="B", tier="s")])
    db = Database(tmp_path / "w.db")
    reg = AgentRegistry(db)
    for i in range(2):
        reg.insert(AgentRecord(f"ag_{i}", f"N{i}", "s", 1, PersonalitySeed(), "v", 0, AgentStatus.ALIVE, Vec2(0, 0), 0, 1, 0, False, 0))
    s = Scheduler(cfg, reg)
    s.sleep("ag_0")
    assert s.awake_ids() == ["ag_1"]
    assert s.sleep_all() == ["ag_1"] and s.awake_ids() == []
    reg.update("ag_0", weather_nudge_used=0.1)
    s.begin_day()
    assert s.awake_ids() == ["ag_0", "ag_1"] and reg.get("ag_0").weather_nudge_used == 0.0
    c = Clock(ticks_per_day=3)
    c.advance()
    c.save(db)
    assert Clock.load(db, 3).tick == 1
