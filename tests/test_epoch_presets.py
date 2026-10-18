"""Epoch presets (DESIGN §11.4): drought, storm and boom move their default lever when the epoch leaves it None."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from void.config import EpochConfig, load_config
from void.db import Database
from void.events import Event, EventBus
from void.server.app import create_app
from void.sim.loop import Simulation
from void.world.epochs import PRESETS, Epochs, with_presets
from void.world.weather import Weather

REPO = Path(__file__).resolve().parents[1]
SMOKE = REPO / "configs" / "scripted_smoke.yaml"
TPD = 24


def _bench(tmp_path: Path, epochs: list[dict[str, Any]]) -> tuple[Database, Weather, Epochs, list[Event]]:
    cfg = load_config(SMOKE, {"epochs": epochs})
    db = Database(tmp_path / "presets.db")
    weather = Weather(cfg.world.weather)
    seen: list[Event] = []
    bus = EventBus(persist=db.persist_event)
    bus.subscribe(seen.append)
    return db, weather, Epochs(cfg, db, weather, bus, None), seen


def _payloads(seen: list[Event], phase: str) -> list[dict[str, Any]]:
    return [dict(e.payload) for e in seen if e.payload.get("phase") == phase]


# --- the table ---------------------------------------------------------------------------------------------------
def test_preset_table_and_resolution():
    assert PRESETS == {"drought": {"scarcity": 0.5}, "storm": {"weather_baseline": -0.6}, "boom": {"scarcity": 1.5}}
    bare = {"kind": "storm", "scarcity": None, "weather_baseline": None}
    assert with_presets(bare) == {"kind": "storm", "scarcity": None, "weather_baseline": -0.6}
    assert bare["weather_baseline"] is None  # the input is not mutated
    assert with_presets({"kind": "drought"})["scarcity"] == 0.5 and with_presets({"kind": "boom"})["scarcity"] == 1.5
    assert with_presets({"kind": "drought", "scarcity": 0.8})["scarcity"] == 0.8
    assert with_presets({"kind": "boom", "scarcity": 0.0})["scarcity"] == 0.0  # zero is explicit, not missing
    for kind in ("arrival", "custom"):
        e = {"kind": kind, "scarcity": None, "weather_baseline": None}
        assert with_presets(e) == e


# --- a bare storm -----------------------------------------------------------------------------------------------
def test_bare_storm_sets_weather_baseline_and_restores_it_on_expiry(tmp_path):
    db, weather, ep, seen = _bench(tmp_path, [{"day": 2, "kind": "storm"}])
    ep.apply_and_expire(1, 1)
    assert weather.baseline == 0.0 and ep.active() == []
    sched = ep.scheduled(1)
    assert len(sched) == 1 and sched[0]["weather_baseline"] == -0.6 and sched[0]["scarcity"] is None
    ep.apply_and_expire(2, TPD + 1)
    assert weather.baseline == -0.6
    assert db.kv_get("scarcity") is None  # the storm preset leaves scarcity alone
    top = ep.active()[-1]
    assert top["kind"] == "storm" and top["weather_baseline"] == -0.6 and top["prev_weather_baseline"] == 0.0
    assert top["scarcity"] is None and top["prev_scarcity"] is None
    started = _payloads(seen, "started")
    assert len(started) == 1 and started[0]["weather_baseline"] == -0.6 and started[0]["scarcity"] is None
    ep.apply_and_expire(3, 2 * TPD + 1)
    assert weather.baseline == 0.0 and ep.active() == []
    assert [p["kind"] for p in _payloads(seen, "ended")] == ["storm"]
    # the stored configuration was not rewritten: the preset is resolved on read
    assert ep.cfg.epochs[0].weather_baseline is None


def test_bare_storm_posted_to_the_api_moves_the_world(tmp_path):
    cfg = load_config(SMOKE, {"run": {"name": "preset_api", "days": 2, "tick_seconds": 0.0}})
    sim = Simulation(cfg, tmp_path / "run")
    sim.pause()
    auth = {"Authorization": f"Bearer {sim.operator_token}"}
    with TestClient(create_app(sim, static_dir=None, host="127.0.0.1", port=8000)) as client:
        r = client.post("/api/control/epoch", json={"kind": "storm"}, headers=auth)
        assert r.status_code == 202, r.text
        assert client.post("/api/control/step", headers=auth).status_code == 200
        assert sim.control.result(r.json()["cmd_id"])["result"]["ok"] is True
        assert sim.weather.baseline == -0.6 and sim.db.kv_get("weather_baseline") == -0.6
        snap = client.get("/api/state").json()
        assert snap["epochs"]["active"]["kind"] == "storm" and snap["epochs"]["active"]["weather_baseline"] == -0.6
        assert snap["scarcity"] == cfg.world.scarcity
        for _ in range(TPD):  # through the end of day 1 and into day 2, when the one-day storm expires
            assert client.post("/api/control/step", headers=auth).status_code == 200
        assert sim.clock.day == 2 and sim.weather.baseline == 0.0
        assert client.get("/api/state").json()["epochs"]["active"] is None
    sim.db.close()


# --- explicit levers win -----------------------------------------------------------------------------------------
def test_explicit_scarcity_overrides_the_drought_preset(tmp_path):
    db, weather, ep, seen = _bench(tmp_path, [{"day": 2, "kind": "drought", "scarcity": 0.8, "duration_days": 1}])
    ep.apply_and_expire(2, TPD + 1)
    assert db.kv_get("scarcity") == 0.8 and ep.active()[-1]["scarcity"] == 0.8
    assert _payloads(seen, "started")[0]["scarcity"] == 0.8
    assert weather.baseline == 0.0
    ep.apply_and_expire(3, 2 * TPD + 1)
    assert db.kv_get("scarcity") == 1.0


def test_preset_fills_only_the_levers_left_none(tmp_path):
    db, weather, ep, _ = _bench(tmp_path, [{"day": 2, "kind": "drought", "weather_baseline": -0.3},
                                           {"day": 2, "kind": "storm", "weather_baseline": -0.9}])
    ep.apply_and_expire(2, TPD + 1)
    # drought: explicit weather kept, scarcity from the preset; storm: explicit weather wins over its preset
    assert db.kv_get("scarcity") == 0.5 and weather.baseline == -0.9
    assert [(e["kind"], e["scarcity"], e["weather_baseline"]) for e in ep.active()] == [("drought", 0.5, -0.3), ("storm", None, -0.9)]
    ep.apply_and_expire(3, 2 * TPD + 1)
    assert db.kv_get("scarcity") == 1.0 and weather.baseline == 0.0


def test_boom_preset_and_operator_scheduled_epochs(tmp_path):
    db, weather, ep, _ = _bench(tmp_path, [])
    assert ep.schedule(EpochConfig(day=2, kind="boom"), 10) == "op1"
    assert db.kv_get("epochs_dynamic")[0]["scarcity"] is None  # stored as posted
    assert ep.scheduled(1)[0]["scarcity"] == 1.5
    ep.apply_and_expire(2, TPD + 1)
    assert db.kv_get("scarcity") == 1.5 and ep.active()[-1]["prev_scarcity"] == 1.0
    ep.apply_and_expire(3, 2 * TPD + 1)
    assert db.kv_get("scarcity") == 1.0 and weather.baseline == 0.0


def test_arrival_and_custom_have_no_preset(tmp_path):
    db, weather, ep, seen = _bench(tmp_path, [{"day": 2, "kind": "custom"},
                                              {"day": 2, "kind": "arrival", "arrival": {"name": "Zed", "tier": "scripted"}}])
    ep.apply_and_expire(2, TPD + 1)
    assert db.kv_get("scarcity") is None and weather.baseline == 0.0
    assert [(e["kind"], e["scarcity"], e["weather_baseline"]) for e in ep.active()] == [("custom", None, None), ("arrival", None, None)]
    assert all(p["scarcity"] is None and p["weather_baseline"] is None for p in _payloads(seen, "started"))


# --- resume ---------------------------------------------------------------------------------------------------------
RESUME_TICKS = 36  # a day and a half: the storm is active at the split (tick 12) and expires at tick 25
SPLIT = 12
OP_TICK = 6  # a bare operator boom enqueued here applies at tick 7


def _resume_cfg():
    return load_config(SMOKE, {"run": {"days": 2, "name": "preset_resume"}, "memory": {"probe_every_ticks": 6},
                               "epochs": [{"day": 1, "kind": "storm", "duration_days": 1}]})


def _events(sim: Simulation) -> list[tuple]:
    # the sandbox self-test is re-run by every process that opens the run; it is not world state
    return [(r["tick"], r["kind"], r["agent_id"], r["payload"])
            for r in sim.db.fetchall("SELECT tick, kind, agent_id, payload FROM events WHERE kind != 'sandbox_self_test' ORDER BY seq")]


async def _ticks(sim: Simulation, n: int, trace: dict[int, tuple[float, float]]) -> None:
    for _ in range(n):
        if sim.clock.tick == OP_TICK:
            sim.enqueue("epoch", {"day": sim.clock.day, "kind": "boom"})
        rep = await sim.tick()
        trace[rep.tick] = (float(sim.db.kv_get("scarcity")), float(sim.weather.baseline))


def test_resume_preserves_an_active_preset_epoch(tmp_path):
    async def straight() -> tuple[list[tuple], list[Any], dict[int, tuple[float, float]]]:
        trace: dict[int, tuple[float, float]] = {}
        sim = Simulation(_resume_cfg(), tmp_path / "straight")
        await sim.setup()
        await _ticks(sim, RESUME_TICKS, trace)
        await sim.finish()
        rows = [json.loads(r) for r in (tmp_path / "straight" / "metrics.jsonl").read_text().splitlines()]
        ev = _events(sim)
        sim.db.close()
        return ev, rows, trace

    async def interrupted() -> tuple[list[tuple], list[Any], dict[int, tuple[float, float]]]:
        trace: dict[int, tuple[float, float]] = {}
        sim = Simulation(_resume_cfg(), tmp_path / "split")
        await sim.setup()
        await _ticks(sim, SPLIT, trace)
        sim.metrics.close()
        sim.db.close()
        sim2 = Simulation(_resume_cfg(), tmp_path / "split", resume=True)
        assert sim2.status == "running"
        # the reopened world still stands under the storm and the boom, with the stack that restores them
        assert sim2.weather.baseline == -0.6 and float(sim2.db.kv_get("scarcity")) == 1.5
        assert [(e["id"], e["kind"], e["scarcity"], e["weather_baseline"]) for e in sim2.epochs.active()] == [
            ("cfg0", "storm", None, -0.6), ("op1", "boom", 1.5, None)]
        assert sim2.epochs.snapshot(1)["active"]["id"] == "op1"
        await sim2.setup()
        await _ticks(sim2, RESUME_TICKS - SPLIT, trace)
        await sim2.finish()
        rows = [json.loads(r) for r in (tmp_path / "split" / "metrics.jsonl").read_text().splitlines()]
        ev = _events(sim2)
        sim2.db.close()
        return ev, rows, trace

    ev_a, rows_a, trace_a = asyncio.run(straight())
    ev_b, rows_b, trace_b = asyncio.run(interrupted())
    assert trace_a == trace_b
    for t in range(1, OP_TICK + 1):
        assert trace_a[t] == (1.0, -0.6), (t, trace_a[t])
    for t in range(OP_TICK + 1, TPD + 1):
        assert trace_a[t] == (1.5, -0.6), (t, trace_a[t])
    for t in range(TPD + 1, RESUME_TICKS + 1):  # both one-day epochs expire on day 2
        assert trace_a[t] == (1.0, 0.0), (t, trace_a[t])
    assert len(ev_a) == len(ev_b)
    first_diff = next((i for i, (x, y) in enumerate(zip(ev_a, ev_b, strict=True)) if x != y), None)
    assert first_diff is None, f"event streams diverge at #{first_diff}: {ev_a[first_diff]} vs {ev_b[first_diff]}"
    assert rows_a == rows_b
    epochs = [(t, json.loads(p)["id"], json.loads(p)["phase"]) for t, k, _, p in ev_a if k == "epoch"]
    assert epochs == [(1, "cfg0", "started"), (OP_TICK + 1, "op1", "started"), (TPD + 1, "cfg0", "ended"), (TPD + 1, "op1", "ended")]
