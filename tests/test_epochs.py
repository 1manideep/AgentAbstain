"""Scheduled and operator epochs (DESIGN §11.4, §14.1): apply on their day, expire, spawn arrivals."""

from __future__ import annotations

import pytest
from conftest import NO_PROVIDER_TIERS, SimRun, deep_merge

from void.config import EpochConfig, load_config
from void.db import Database
from void.events import EventBus
from void.world.epochs import Epochs
from void.world.weather import Weather

DAYS = 3
TPD = 24
ENQUEUE_TICK = 2 * TPD + 2  # day 3, second tick: the operator boom applies at the next tick (51)

EPOCHS = deep_merge(NO_PROVIDER_TIERS, {
    "run": {"days": DAYS},
    "epochs": [{"day": 2, "kind": "drought", "scarcity": 0.4, "weather_baseline": -0.6, "duration_days": 1},
               {"day": 2, "kind": "arrival", "arrival": {"name": "Zed", "tier": "scripted", "balance_usd": 1.0}}],
})


@pytest.fixture(scope="module")
def world(run_sim) -> tuple[SimRun, dict[int, tuple[int, float, float]], dict[str, int]]:
    trace: dict[int, tuple[int, float, float]] = {}
    cmd: dict[str, int] = {}

    def hook(sim, report):
        trace[report.tick] = (report.day, float(sim.db.kv_get("scarcity")), float(sim.weather.baseline))
        if report.tick == ENQUEUE_TICK:
            cmd["id"] = sim.enqueue("epoch", {"day": report.day, "kind": "boom", "scarcity": 1.5, "duration_days": 1})

    run = run_sim("epochs", EPOCHS, on_tick=hook)
    return run, trace, cmd


def test_drought_applies_on_day_two_and_expires_on_day_three(world):
    run, trace, _ = world
    assert run.status == "completed" and len(trace) == DAYS * TPD
    for t in range(1, TPD + 1):
        assert trace[t] == (1, 1.0, 0.0)
    for t in range(TPD + 1, 2 * TPD + 1):
        assert trace[t] == (2, 0.4, -0.6), (t, trace[t])
    for t in range(2 * TPD + 1, ENQUEUE_TICK + 1):  # until the operator epoch lands at ENQUEUE_TICK + 1
        assert trace[t] == (3, 1.0, 0.0), (t, trace[t])
    started = [e for e in run.events("epoch") if e["payload"]["phase"] == "started"]
    ended = [e for e in run.events("epoch") if e["payload"]["phase"] == "ended"]
    assert [(e["tick"], e["payload"]["kind"]) for e in started][:2] == [(TPD + 1, "drought"), (TPD + 1, "arrival")]
    assert started[0]["payload"]["ends_day"] == 3 and started[0]["payload"]["scarcity"] == 0.4
    assert [(e["tick"], e["payload"]["kind"]) for e in ended][:1] == [(2 * TPD + 1, "drought")]
    # the weather itself was pulled toward the storm baseline on day 2 and recovered on day 3
    weather = {e["tick"]: e["payload"]["value"] for e in run.events("weather")}
    assert weather[2 * TPD] < -0.3 and weather[DAYS * TPD] > weather[2 * TPD]
    assert run.kv("epoch_stack")[-1]["id"] == "op1" and run.kv("epochs_applied") == ["cfg0", "cfg1", "op1"]


def test_arrival_spawns_a_generation_zero_agent(world):
    run, _, _ = world
    zeds = [a for a in run.sim.registry.all() if a.name == "Zed"]
    assert len(zeds) == 1
    zed = zeds[0]
    assert zed.generation == 0 and zed.parent_id is None and zed.born_tick == TPD + 1 and zed.model_tier == "scripted"
    births = [b for b in run.events("birth") if b["payload"]["kind"] == "arrival"]
    assert len(births) == 1 and births[0]["payload"]["agent_id"] == zed.agent_id and births[0]["payload"]["endowment_usd"] == 1.0
    assert births[0]["tick"] == TPD + 1 and births[0]["day"] == 2
    grant = run.db.fetchone("SELECT delta, kind FROM wallet_ledger WHERE agent_id=? ORDER BY entry_id LIMIT 1", (zed.agent_id,))
    assert int(grant["delta"]) == 1_000_000 and grant["kind"] == "arrival_grant"
    assert (run.run_dir / "vaults" / zed.agent_id / "self.md").is_file() or (run.run_dir / "graveyard" / zed.agent_id / "self.md").is_file()
    started = [e for e in run.events("epoch") if e["payload"]["kind"] == "arrival" and e["payload"]["phase"] == "started"]
    assert started[0]["payload"]["arrival_agent_id"] == zed.agent_id
    assert any(r["population"] == 7 for r in run.tick_rows())


def test_operator_epoch_applies_at_the_next_tick(world):
    run, trace, cmd = world
    assert trace[ENQUEUE_TICK] == (3, 1.0, 0.0)
    for t in range(ENQUEUE_TICK + 1, DAYS * TPD + 1):
        assert trace[t] == (3, 1.5, 0.0), (t, trace[t])
    applied = run.sim.control.result(cmd["id"])
    assert applied == {"applied_tick": ENQUEUE_TICK + 1, "result": {"ok": True, "epoch_id": "op1"}}
    cmds = run.events("operator_command")
    assert len(cmds) == 1 and cmds[0]["visibility"] == "operator" and cmds[0]["tick"] == ENQUEUE_TICK + 1
    assert cmds[0]["payload"] == {"cmd_id": cmd["id"], "kind": "epoch", "ok": True, "epoch_id": "op1"}
    boom = [e for e in run.events("epoch") if e["payload"].get("id") == "op1"]
    assert [(e["tick"], e["payload"]["phase"]) for e in boom] == [(ENQUEUE_TICK + 1, "started")]
    assert boom[0]["payload"]["scarcity"] == 1.5 and boom[0]["payload"]["ends_day"] == 4
    assert run.sim.epochs.snapshot(3)["active"]["id"] == "op1"


def _bench(tmp_path):
    cfg = load_config("configs/scripted_smoke.yaml", {"epochs": [{"day": 2, "kind": "drought", "scarcity": 0.4, "duration_days": 1}]})
    db = Database(tmp_path / "epochs.db")
    return cfg, db, Epochs(cfg, db, Weather(cfg.world.weather), EventBus(persist=db.persist_event), None)


def test_epochs_unit_apply_expire_and_snapshot(tmp_path):
    cfg, db, ep = _bench(tmp_path)
    ep.apply_and_expire(1, 1)
    assert db.kv_get("scarcity") is None and ep.scheduled(1) and ep.active() == []
    ep.apply_and_expire(2, 25)
    assert db.kv_get("scarcity") == 0.4 and ep.active()[0]["prev_scarcity"] == 1.0 and ep.scheduled(2) == []
    ep.apply_and_expire(2, 26)  # idempotent within the day
    assert db.kv_get("scarcity") == 0.4 and len(ep.active()) == 1
    ep.apply_and_expire(3, 49)
    assert db.kv_get("scarcity") == 1.0 and ep.active() == [] and ep.snapshot(3) == {"active": None, "scheduled": []}
    assert ep.schedule(EpochConfig(day=3, kind="boom", scarcity=1.5), 50) == "op1" and ep.scheduled(3)[0]["id"] == "op1"


def test_overlapping_epochs_restore_the_original_scarcity(tmp_path):
    cfg, db, ep = _bench(tmp_path)
    ep.apply_and_expire(2, 25)  # drought: 1.0 -> 0.4
    ep.schedule(EpochConfig(day=2, kind="boom", scarcity=1.5, duration_days=1), 30)
    ep.apply_and_expire(2, 31)  # boom on top: 0.4 -> 1.5
    assert db.kv_get("scarcity") == 1.5
    ep.apply_and_expire(3, 49)  # both end on day 3
    assert ep.active() == []
    assert db.kv_get("scarcity") == 1.0
