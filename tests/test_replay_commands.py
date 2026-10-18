"""Operator-command replay (DESIGN §0 "Operator writes ... Single writer; replayable runs"; DESIGN §14.1).

A scripted run is driven by operator commands enqueued between ticks (``Simulation.enqueue``, as the HTTP handlers
do): a task posted, approved and completed, a command that fails, two epochs (one applied at once, one an arrival a
day later), a manual grant to a picked target and one to a named agent, and the benefactor switched on and off.
A second run of the same config replays them headless through the CLI (``void run --replay-commands RUN_DIR``):
every ``control_commands`` row of the source run is applied at its recorded ``applied_tick`` through the same
``_apply_command`` path. ``metrics.jsonl`` carries no wall-clock field, so the two must match byte for byte, and so
must the event stream and the command rows.

Each run happens in its own working directory with a relative run dir (as in tests/test_world_goldens.py), so no
absolute path reaches a payload.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, SMOKE, deep_merge

from void.cli import main
from void.config import load_config
from void.db import Database
from void.sim.control import HEADLESS_SKIP, CommandReplay, recorded_commands
from void.sim.loop import Simulation, TickReport

DAYS = 2
NAME = "replay"


def _write_config(root: Path) -> Path:
    cfg = load_config(SMOKE, deep_merge(NO_PROVIDER_TIERS, {"run": {"name": NAME, "days": DAYS},
                                                            "benefactor": {"mean_interval_ticks": 4}}))
    path = root / "replay.yaml"
    path.write_text(cfg.to_yaml(), encoding="utf-8")
    assert load_config(path).hash() == cfg.hash()
    return path


def _drive(sim: Simulation, report: TickReport, ops: dict[str, Any]) -> None:
    """Commands enqueued after ``report.tick``; each is applied at the next tick boundary."""
    t = report.tick
    if t == 2:
        sim.enqueue("post_task", {"title": "Map the east", "description": "Walk the east edge and report back.", "reward_usd": 0.3})
    elif t == 3:
        sim.enqueue("epoch", {"day": 1, "kind": "drought", "scarcity": 0.7})
    elif t == 5:
        sim.enqueue("benefactor", {"amount_usd": 0.4})  # target picked by the benefactor's seeded stream
    elif t == 7:
        sim.enqueue("benefactor", {"enabled": True})
    elif t == 9:
        sim.enqueue("epoch", {"day": 2, "kind": "arrival", "arrival": {"name": "Zed", "tier": "scripted", "balance_usd": 0.8}})
    elif t == 12:
        sim.enqueue("complete_task", {"task_id": "tk_missing"})  # fails: replayed as it failed
        alive = sorted(a.agent_id for a in sim.registry.alive())
        sim.enqueue("benefactor", {"agent_id": alive[-1], "amount_usd": 0.25})
    elif t == 30:
        sim.enqueue("benefactor", {"enabled": False})
    if "approve_tick" not in ops and t >= 4:
        app = sim.db.fetchone("SELECT application_id, task_id FROM task_applications WHERE status='pending' "
                              "ORDER BY tick, application_id LIMIT 1")
        if app is not None:
            ops["approve_tick"], ops["task_id"] = t, str(app["task_id"])
            sim.enqueue("approve_task", {"task_id": app["task_id"], "application_id": app["application_id"]})
    elif "approve_tick" in ops and "complete_tick" not in ops and t == int(ops["approve_tick"]) + 3:
        ops["complete_tick"] = t
        sim.enqueue("complete_task", {"task_id": ops["task_id"]})


def _record(db: Database) -> dict[str, Any]:
    events = [[r["tick"], r["kind"], r["agent_id"], json.loads(r["payload"]), r["visibility"]]
              for r in db.fetchall("SELECT tick, kind, agent_id, payload, visibility FROM events ORDER BY seq")]
    commands = [dict(r) for r in db.fetchall(
        "SELECT cmd_id, received_tick, kind, payload, applied_tick, result FROM control_commands ORDER BY cmd_id")]
    tasks = [dict(r) for r in db.fetchall("SELECT * FROM tasks ORDER BY task_id")]
    return {"events": events, "commands": commands, "tasks": tasks}


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("replay")
    config = _write_config(root)
    src_wd, rep_wd = root / "source", root / "replayed"
    src_wd.mkdir()
    rep_wd.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(src_wd)
        sim = Simulation(load_config(config), Path("run"))
        ops: dict[str, Any] = {}
        status = asyncio.run(sim.run(on_tick=lambda r: _drive(sim, r, ops)))
        source = _record(sim.db)
        sim.db.close()
        mp.chdir(rep_wd)
        rc = main(["run", "--config", str(config), "--out", ".", "--replay-commands", str(src_wd / "run")])
    db = Database(rep_wd / NAME / "world.db")
    try:
        replayed = _record(db)
        summary = db.kv_get("replay_commands")
        replay_status = db.fetchone("SELECT status FROM run")["status"]
    finally:
        db.close()
    return {"status": status, "ops": ops, "source": source, "replayed": replayed, "rc": rc, "summary": summary,
            "replay_status": replay_status, "source_dir": src_wd / "run", "replay_dir": rep_wd / NAME}


def test_the_source_run_was_really_driven(runs):
    assert runs["status"] == "completed"
    src = runs["source"]
    kinds = [c["kind"] for c in src["commands"]]
    assert {"post_task", "approve_task", "complete_task", "epoch", "benefactor"} <= set(kinds)
    assert all(c["applied_tick"] is not None for c in src["commands"])
    results = [json.loads(c["result"]) for c in src["commands"]]
    assert any(not r.get("ok", True) for r in results)  # the failing command is part of what is replayed
    assert "complete_tick" in runs["ops"] and any(t["status"] == "completed" for t in src["tasks"])
    events = src["events"]
    assert any(e[1] == "epoch" and e[3].get("kind") == "drought" and e[3].get("phase") == "started" for e in events)
    assert any(e[1] == "birth" and e[3].get("name") == "Zed" for e in events)
    manual = [e for e in events if e[1] == "benefactor_grant" and e[3].get("manual")]
    seeded = [e for e in events if e[1] == "benefactor_grant" and not e[3].get("manual")]
    assert len(manual) == 2 and seeded  # two manual grants, and automatic ones while it was switched on
    assert sum(1 for e in events if e[1] == "operator_command") == len(src["commands"])


def test_the_replay_gives_a_byte_identical_metrics_jsonl(runs):
    assert runs["rc"] == 0 and runs["replay_status"] == "completed"
    a = (runs["source_dir"] / "metrics.jsonl").read_bytes()
    b = (runs["replay_dir"] / "metrics.jsonl").read_bytes()
    rows = [json.loads(line) for line in a.decode().splitlines() if line.strip()]
    assert len(rows) == DAYS * 24 + DAYS
    assert a == b


def test_the_replay_applies_every_command_at_its_recorded_tick(runs):
    src, rep = runs["source"], runs["replayed"]
    assert rep["commands"] == src["commands"]  # same ids, received and applied ticks, payloads and results
    assert rep["tasks"] == src["tasks"]
    first = next((i for i, (x, y) in enumerate(zip(src["events"], rep["events"], strict=False)) if x != y), None)
    assert first is None and len(src["events"]) == len(rep["events"]), f"event #{first} differs"


def test_the_replay_records_its_source(runs):
    cfg = load_config(runs["source_dir"] / "config.resolved.yaml")
    assert runs["summary"] == {"source_run_id": f"{NAME}-s{cfg.run.seed}", "source_config_hash": cfg.hash(),
                               "commands": len(runs["source"]["commands"]), "skipped": []}


def test_a_replay_stopped_and_resumed_matches_the_source(runs, tmp_path, monkeypatch):
    """A resumed replay applies only the commands still to come: nothing twice, nothing lost."""
    monkeypatch.chdir(tmp_path)
    cfg = load_config(runs["source_dir"] / "config.resolved.yaml")
    stop = 10  # commands are due both before and after it

    async def first_half() -> None:
        sim = Simulation(cfg, Path("run"), replay_commands=runs["source_dir"])
        await sim.setup()
        for _ in range(stop):
            await sim.tick()
        sim.metrics.close()
        sim.db.close()

    async def second_half() -> tuple[str, int, dict[str, Any]]:
        sim = Simulation(cfg, Path("run"), resume=True, replay_commands=runs["source_dir"])
        assert sim.replay is not None and sim.replay.remaining() > 0
        status = await sim.run()
        left = sim.replay.remaining()
        rec = _record(sim.db)
        sim.db.close()
        return status, left, rec

    asyncio.run(first_half())
    status, left, rec = asyncio.run(second_half())
    assert status == "completed" and left == 0
    assert rec["commands"] == runs["source"]["commands"]
    assert (tmp_path / "run" / "metrics.jsonl").read_bytes() == (runs["source_dir"] / "metrics.jsonl").read_bytes()


def test_a_run_without_operator_commands_differs_from_the_driven_one(runs, tmp_path, monkeypatch):
    """The comparison has teeth: the same config without the commands does not give the driven run's metrics."""
    monkeypatch.chdir(tmp_path)
    cfg = load_config(runs["source_dir"] / "config.resolved.yaml")
    sim = Simulation(cfg, Path("run"))
    asyncio.run(sim.run(max_ticks=14))
    sim.db.close()
    plain = (tmp_path / "run" / "metrics.jsonl").read_bytes().splitlines()
    driven = (runs["source_dir"] / "metrics.jsonl").read_bytes().splitlines()
    assert plain[:2] == driven[:2]  # ticks 1 and 2: nothing applied yet (the first command lands at tick 3)
    assert plain[:14] != driven[:14]


# --- CommandReplay ---------------------------------------------------------------------------------------------------------
def _source_db(path: Path) -> None:
    db = Database(path / "world.db")
    rows = [  # (received_tick, kind, payload, applied_tick)
        (0, "pause", {}, 1),
        (2, "post_task", {"title": "a", "description": "b", "reward_usd": 0.1}, 3),
        (2, "speed", {"tick_seconds": 0.5}, 3),
        (2, "benefactor", {"amount_usd": 0.2}, 3),
        (3, "resume", {}, 4),
        (4, "epoch", {"day": 1, "kind": "storm"}, 5),
        (6, "complete_task", {"task_id": "tk_x"}, None),  # still pending when the run stopped: never applied
    ]
    for received, kind, payload, applied in rows:
        db.execute("INSERT INTO control_commands(received_tick, kind, payload, applied_tick, result) VALUES(?,?,?,?,?)",
                   (received, kind, json.dumps(payload, sort_keys=True), applied, None if applied is None else "{}"))
    db.close()


def test_command_replay_hands_back_applied_commands_by_tick_and_skips_loop_flags(tmp_path):
    _source_db(tmp_path)
    commands, source = recorded_commands(tmp_path)
    assert [c.kind for c in commands] == ["pause", "post_task", "speed", "benefactor", "resume", "epoch"]
    assert source == {}
    replay = CommandReplay.load(tmp_path)
    assert {"pause", "resume", "speed"} <= HEADLESS_SKIP
    assert sorted(c.kind for c in replay.skipped) == ["pause", "resume", "speed"]
    assert replay.due(1) == []
    due = replay.due(3)
    assert [(c.kind, c.received_tick, c.applied_tick) for c in due] == [("post_task", 2, 3), ("benefactor", 2, 3)]
    assert due[0].payload == {"title": "a", "description": "b", "reward_usd": 0.1}
    assert replay.due(3) == []  # handed out once
    assert replay.remaining() == 1 and [c.kind for c in replay.due(5)] == ["epoch"] and replay.remaining() == 0
    assert replay.summary() == {"source_run_id": None, "source_config_hash": None, "commands": 3,
                                "skipped": ["pause", "resume", "speed"]}
    resumed = CommandReplay.load(tmp_path)
    assert resumed.drop_through(3) == 2 and resumed.due(3) == [] and [c.kind for c in resumed.due(5)] == ["epoch"]


def test_a_replay_source_without_a_database_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError):
        CommandReplay.load(tmp_path)


# --- the CLI ------------------------------------------------------------------------------------------------------------------
def test_cli_refuses_a_replay_with_a_seed_sweep(tmp_path, capsys):
    rc = main(["run", "--config", str(SMOKE), "--seeds", "1-2", "--out", str(tmp_path), "--replay-commands", str(tmp_path)])
    assert rc == 2 and "--seeds" in capsys.readouterr().err
    assert not any(tmp_path.iterdir())


def test_cli_refuses_a_missing_replay_source(tmp_path, capsys):
    rc = main(["run", "--config", str(SMOKE), "--out", str(tmp_path / "out"), "--replay-commands", str(tmp_path / "nowhere")])
    assert rc == 2 and "world.db" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_cli_refuses_to_replay_a_run_into_its_own_directory(tmp_path, capsys):
    _source_db(tmp_path / "scripted_smoke")
    rc = main(["run", "--config", str(SMOKE), "--out", str(tmp_path), "--resume", "--replay-commands", str(tmp_path / "scripted_smoke")])
    assert rc == 2 and "itself" in capsys.readouterr().err
