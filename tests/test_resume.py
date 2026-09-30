"""A run stopped between ticks and resumed must match an uninterrupted run byte for byte (DESIGN §14.2)."""

import asyncio
import json
from pathlib import Path

import pytest

from void.config import load_config
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
TICKS = 36  # one and a half days at 24 ticks/day


def _events(sim: Simulation) -> list[tuple]:
    # the sandbox self-test is re-run (and re-reported) by every process that opens the run; it is not world state
    return [(r["tick"], r["kind"], r["agent_id"], r["payload"])
            for r in sim.db.fetchall("SELECT tick, kind, agent_id, payload FROM events WHERE kind != 'sandbox_self_test' ORDER BY seq")]


def _cfg(days: int = 2):
    return load_config(REPO / "configs/scripted_smoke.yaml", {"run": {"days": days, "name": "resume_case"}, "memory": {"probe_every_ticks": 6}})


def test_stop_and_resume_matches_straight_run(tmp_path: Path):
    async def straight() -> tuple[list[tuple], list[str]]:
        sim = Simulation(_cfg(), tmp_path / "straight")
        await sim.setup()
        for _ in range(TICKS):
            await sim.tick()
        await sim.finish()
        rows = (tmp_path / "straight" / "metrics.jsonl").read_text().splitlines()
        ev = _events(sim)
        sim.db.close()
        return ev, rows

    async def interrupted() -> tuple[list[tuple], list[str]]:
        sim = Simulation(_cfg(), tmp_path / "split")
        await sim.setup()
        for _ in range(TICKS // 2):
            await sim.tick()
        sim.metrics.close()
        sim.db.close()
        sim2 = Simulation(_cfg(), tmp_path / "split", resume=True)
        assert sim2.status == "running"
        await sim2.setup()
        for _ in range(TICKS - TICKS // 2):
            await sim2.tick()
        await sim2.finish()
        rows = (tmp_path / "split" / "metrics.jsonl").read_text().splitlines()
        ev = _events(sim2)
        sim2.db.close()
        return ev, rows

    ev_a, rows_a = asyncio.run(straight())
    ev_b, rows_b = asyncio.run(interrupted())
    assert len(ev_a) == len(ev_b)
    first_diff = next((i for i, (x, y) in enumerate(zip(ev_a, ev_b, strict=True)) if x != y), None)
    assert first_diff is None, f"event streams diverge at #{first_diff}: {ev_a[first_diff]} vs {ev_b[first_diff]}"
    assert [json.loads(r) for r in rows_a] == [json.loads(r) for r in rows_b]


def test_resume_refuses_a_different_config_or_a_partial_tick(tmp_path: Path):
    async def go() -> None:
        sim = Simulation(_cfg(), tmp_path / "r")
        await sim.setup()
        await sim.tick()
        sim.db.close()
        with pytest.raises(RuntimeError, match="config_hash"):
            Simulation(load_config(REPO / "configs/scripted_smoke.yaml", {"run": {"days": 2, "name": "resume_case"}, "world": {"scarcity": 0.7}}), tmp_path / "r", resume=True)
        sim2 = Simulation(_cfg(), tmp_path / "r", resume=True)
        sim2.db.kv_set("tick_in_progress", sim2.clock.tick + 1)  # a crash mid-tick leaves the marker behind
        sim2.db.close()
        sim3 = Simulation(_cfg(), tmp_path / "r", resume=True)
        assert sim3.status == "inconsistent"
        sim3.db.close()

    asyncio.run(go())
