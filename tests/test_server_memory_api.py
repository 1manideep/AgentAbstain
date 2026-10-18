"""Memory-layer HTTP routes, /api/state's end status and validated hub messages (DESIGN §14, §15; MEMORY_EVOLUTION §5.10).

One served scripted run of configs/demo_memory.yaml, three days so the exam (two days' delay) asks its first
questions, runs to completion inside a TestClient and is then read through the routes. Each response is held to its
interface in web/src/protocol.ts (the web panels were written against those). The failure tests force a tick to
raise, once in the background loop and once under /api/control/step, and check that ``failed`` reaches the API and
validates as a wire message.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, deep_merge
from fastapi.testclient import TestClient
from test_protocol_sync import ts_interface_fields

from void.config import load_config, micro_to_usd
from void.memory.vault_stats import _tally, agent_vault_stats
from void.research.analysis import exam_counts
from void.server.api import MEMORY_COMMANDS_LIMIT
from void.server.app import create_app
from void.server.protocol import parse_message
from void.server.ws import checked
from void.sim.loop import Simulation

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "demo_memory.yaml"
DAYS = 3
FOREIGN = {"Origin": "http://evil.example"}


def make_sim(run_dir: Path, name: str, days: int) -> Simulation:
    cfg = load_config(CONFIG, deep_merge(NO_PROVIDER_TIERS, {"run": {"name": name, "days": days, "tick_seconds": 0.0}}))
    return Simulation(cfg, run_dir)


def auth(sim: Simulation) -> dict[str, str]:
    return {"Authorization": f"Bearer {sim.operator_token}"}


def wait_finished(client: TestClient, timeout: float = 120.0) -> None:
    """Block until the background driver has run the simulation to its end and fanned out the final status."""
    driver = client.app.state.driver  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout
    while not driver.finished.is_set():
        assert time.monotonic() < deadline, "the served run did not finish"
        time.sleep(0.05)


def fields(name: str) -> set[str]:
    return set(ts_interface_fields(name))


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TestClient, Simulation]]:
    sim = make_sim(tmp_path_factory.mktemp("server_memory_api") / "run", "server_memory_api", DAYS)
    app = create_app(sim, static_dir=None, host="127.0.0.1", port=8000)
    with TestClient(app) as client:
        wait_finished(client)
        assert sim.status == "completed", sim.status
        yield client, sim


@pytest.fixture(scope="module")
def roster(served: tuple[TestClient, Simulation]) -> list[str]:
    _, sim = served
    return [str(r["agent_id"]) for r in sim.db.fetchall("SELECT agent_id FROM agents ORDER BY agent_id")]


# --- /api/agents/{id}/policy -----------------------------------------------------------------------------------
def test_policy_versions_newest_first(served: tuple[TestClient, Simulation], roster: list[str]) -> None:
    client, sim = served
    for aid in roster:
        r = client.get(f"/api/agents/{aid}/policy")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == fields("PolicyResponse") and body["agent_id"] == aid
        history = sim.policy.history(aid)
        assert [v["version"] for v in body["versions"]] == [v.version for v in reversed(history)]
        for got, want in zip(body["versions"], reversed(history), strict=True):
            assert set(got) == fields("PolicyVersion")
            assert got["text"] == want.text and isinstance(got["text"], str)
            assert (got["source"], got["tick"], got["parent_version"]) == (want.source, want.tick, want.parent_version)
    assert any(client.get(f"/api/agents/{a}/policy").json()["versions"] for a in roster), "demo_memory seeds policies"
    assert client.get("/api/agents/ag_nobody/policy").status_code == 404


def test_policy_lists_a_new_version_first(served: tuple[TestClient, Simulation], roster: list[str]) -> None:
    client, sim = served
    aid = roster[0]
    before = client.get(f"/api/agents/{aid}/policy").json()["versions"]
    new = sim.policy.set(aid, "# Mine\n\n- keep <one> note per node", sim.clock.tick, source="self_edit")
    versions = client.get(f"/api/agents/{aid}/policy").json()["versions"]
    assert len(versions) == len(before) + 1
    assert versions[0]["version"] == new.version and versions[0]["source"] == "self_edit"
    assert versions[0]["text"] == new.text  # as stored: the store neutralised the angle brackets, no HTML here
    assert versions[1:] == before


# --- /api/agents/{id}/memory_commands --------------------------------------------------------------------------
def test_memory_commands_shape_order_and_limit(served: tuple[TestClient, Simulation], roster: list[str]) -> None:
    client, sim = served
    counts = {str(r["agent_id"]): int(r["n"]) for r in sim.db.fetchall(
        "SELECT agent_id, COUNT(*) AS n FROM memory_commands GROUP BY agent_id")}
    assert counts, "the nightly maintenance step issues memory commands"
    aid = max(counts, key=lambda a: (counts[a], a))
    body = client.get(f"/api/agents/{aid}/memory_commands", params={"limit": MEMORY_COMMANDS_LIMIT}).json()
    assert set(body) == fields("MemoryCommandsResponse")
    cmds = body["commands"]
    assert len(cmds) == counts[aid]
    for c in cmds:
        assert set(c) == fields("MemoryCommandRow")
        assert isinstance(c["ok"], bool) and isinstance(c["bytes_delta"], int) and isinstance(c["args"], str)
        assert isinstance(json.loads(c["args"]), dict)
        assert c["purpose"] in ("maintain", "tick", "recall", "kernel")
    ticks = [c["tick"] for c in cmds]
    assert ticks == sorted(ticks, reverse=True)
    newest = sim.db.fetchone("SELECT cmd_id FROM memory_commands WHERE agent_id=? ORDER BY tick DESC, rowid DESC LIMIT 1", (aid,))
    one = client.get(f"/api/agents/{aid}/memory_commands", params={"limit": 1}).json()["commands"]
    assert [c["cmd_id"] for c in one] == [newest["cmd_id"]]
    assert len(client.get(f"/api/agents/{aid}/memory_commands").json()["commands"]) == min(50, counts[aid])
    assert client.get(f"/api/agents/{aid}/memory_commands", params={"limit": MEMORY_COMMANDS_LIMIT + 1}).status_code == 422
    assert client.get(f"/api/agents/{aid}/memory_commands", params={"limit": 0}).status_code == 422
    assert client.get("/api/agents/ag_nobody/memory_commands").status_code == 404


# --- /api/memory/summary ---------------------------------------------------------------------------------------
def test_memory_summary(served: tuple[TestClient, Simulation], roster: list[str]) -> None:
    client, sim = served
    body = client.get("/api/memory/summary").json()
    assert set(body) == fields("MemorySummaryResponse")
    rows = body["agents"]
    assert [a["agent_id"] for a in rows] == roster
    vault = agent_vault_stats(sim.db, roster, sim.run_dir)
    for a in rows:
        aid = a["agent_id"]
        assert set(a) == fields("MemorySummaryItem")
        rec = sim.registry.get(aid)
        assert rec is not None and a["name"] == rec.name and a["alive"] is (rec.status.value == "alive")
        assert a["notes"] == vault[aid]["notes"] and a["bytes"] == vault[aid]["bytes"]
        assert a["links"] == _tally(sim.db, aid, sim.run_dir).links  # the exact count behind link_density
        cur = sim.policy.current(aid)
        assert (a["policy_source"], a["policy_version"]) == ((cur.source, cur.version) if cur else (None, None))
        for purpose, key, col in (("maintain", "maintenance_real_usd", "real_cost"), ("maintain", "maintenance_world_usd", "world_cost"),
                                  ("decide", "decide_real_usd", "real_cost")):
            row = sim.db.fetchone(f"SELECT COALESCE(SUM({col}), 0) AS s FROM llm_calls WHERE agent_id=? AND purpose=?", (aid, purpose))
            assert a[key] == pytest.approx(micro_to_usd(int(row["s"])), abs=1e-6), (aid, key)
        assert a["exam_correct"] + a["exam_abstained"] <= a["exam_asked"]
    assert any(a["notes"] > 0 for a in rows) and any(a["links"] > 0 for a in rows)
    assert any(a["maintenance_real_usd"] > 0 for a in rows) and all(a["decide_real_usd"] >= 0 for a in rows)
    totals = exam_counts(sim.run_dir)
    assert totals is not None, "three days with a two-day delay ask exam questions"
    assert (sum(a["exam_asked"] for a in rows), sum(a["exam_correct"] for a in rows),
            sum(a["exam_abstained"] for a in rows)) == totals


# --- /api/practices --------------------------------------------------------------------------------------------
def test_practices(served: tuple[TestClient, Simulation], roster: list[str]) -> None:
    client, sim = served
    body = client.get("/api/practices").json()
    assert body == {"events": []}  # nothing writes practice_events yet (WP6)
    src, dst = roster[0], roster[1]
    with sim.db.tx():
        for i, (tick, kind, sim_) in enumerate([(5, "suggested", None), (9, "adopted", 0.75), (9, "persisted", 0.5)]):
            sim.db.execute("INSERT INTO practice_events(event_id, tick, kind, source_agent_id, target_agent_id, excerpt, similarity) "
                           "VALUES(?,?,?,?,?,?,?)", (f"pe_test_{i}", tick, kind, src, dst, f"- line {i}", sim_))
    events = client.get("/api/practices").json()["events"]
    assert [e["event_id"] for e in events] == ["pe_test_2", "pe_test_1", "pe_test_0"]
    for e in events:
        assert set(e) == fields("PracticeEventRow")
    assert events[0]["similarity"] == 0.5 and events[2]["similarity"] is None
    assert [e["event_id"] for e in client.get("/api/practices", params={"limit": 1}).json()["events"]] == ["pe_test_2"]
    assert client.get("/api/practices", params={"limit": 0}).status_code == 422


# --- /api/exams ------------------------------------------------------------------------------------------------
def test_exams_need_the_operator_token(served: tuple[TestClient, Simulation]) -> None:
    client, sim = served
    assert client.get("/api/exams").status_code == 401
    assert client.get("/api/exams", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/exams", headers={**auth(sim), **FOREIGN}).status_code == 403
    assert client.get("/api/exams", headers=auth(sim)).status_code == 200


def test_exams_items_truths_and_answers(served: tuple[TestClient, Simulation]) -> None:
    client, sim = served
    items = client.get("/api/exams", headers=auth(sim)).json()["items"]
    n = int(sim.db.fetchone("SELECT COUNT(*) AS n FROM exam_items")["n"])
    assert n > 0 and len(items) == n
    keys = {"item_id", "exam", "agent_id", "tick_asked", "tick_about", "kind", "question", "truth", "options", "source_ref",
            "answered", "call_id", "answer", "correct", "abstained", "failed", "retrieved"}
    for it in items:
        assert set(it) == keys
        assert isinstance(it["question"], str) and it["truth"] in it["options"]
        assert it["answered"] and isinstance(it["correct"], bool) and isinstance(it["abstained"], bool)
        assert it["failed"] is False and it["call_id"] is None  # scripted tiers answer from retrieval, no call
        assert it["answer"] in it["options"] and isinstance(it["retrieved"], list)
        assert it["correct"] == (it["answer"] == it["truth"])
    ticks = [it["tick_asked"] for it in items]
    assert ticks == sorted(ticks, reverse=True)
    aid = items[0]["agent_id"]
    mine = client.get("/api/exams", params={"agent_id": aid}, headers=auth(sim)).json()["items"]
    assert mine and all(it["agent_id"] == aid for it in mine)
    assert [it["item_id"] for it in mine] == [it["item_id"] for it in items if it["agent_id"] == aid]
    assert len(client.get("/api/exams", params={"limit": 1}, headers=auth(sim)).json()["items"]) == 1
    assert client.get("/api/exams", params={"agent_id": "ag_nobody"}, headers=auth(sim)).status_code == 404


def test_exam_truths_stay_off_the_open_routes(served: tuple[TestClient, Simulation]) -> None:
    client, _ = served
    for path in ("/api/memory/summary", "/api/practices"):
        assert "truth" not in client.get(path).text


# --- other routes the web panels wait on -----------------------------------------------------------------------
def test_note_detail_matches_protocol(served: tuple[TestClient, Simulation]) -> None:
    client, sim = served
    row = sim.db.fetchone("SELECT note_id, agent_id FROM notes ORDER BY created_tick, note_id LIMIT 1")
    assert row is not None
    r = client.get(f"/api/agents/{row['agent_id']}/notes/{row['note_id']}")
    assert r.status_code == 200, r.text
    assert set(r.json()) == fields("NoteDetail")


def test_snapshots_match_protocol(served: tuple[TestClient, Simulation]) -> None:
    client, _ = served
    body = client.get("/api/snapshots", params={"limit": 2}).json()
    assert set(body) == fields("SnapshotsResponse") and len(body["snapshots"]) == 2
    for s in body["snapshots"]:
        parse_message(s)


# --- run status ------------------------------------------------------------------------------------------------
def test_state_reports_the_end_status(served: tuple[TestClient, Simulation]) -> None:
    client, sim = served
    stored = json.loads(sim.db.fetchone("SELECT json FROM snapshots ORDER BY tick DESC LIMIT 1")["json"])
    assert stored["status"] == "running"  # published before the end-of-run check
    state = client.get("/api/state").json()
    assert state["tick"] == stored["tick"] == DAYS * sim.cfg.run.ticks_per_day
    assert state["status"] == "completed"
    parse_message(state)
    hub = client.app.state.hub  # type: ignore[attr-defined]
    assert parse_message(hub.hello()).status == "completed"  # type: ignore[attr-defined]
    assert parse_message(hub.status_message()).status == "completed"  # type: ignore[attr-defined]


def _fail_from(monkeypatch: pytest.MonkeyPatch, sim: Simulation, tick: int) -> None:
    """Make every tick after ``tick`` raise inside the tick body (the clock has not advanced yet)."""
    original = sim._tick_body

    async def body() -> Any:
        if sim.clock.tick >= tick:
            raise RuntimeError("forced tick failure")
        return await original()

    monkeypatch.setattr(sim, "_tick_body", body)


def _assert_failed(client: TestClient, sim: Simulation, last_tick: int) -> None:
    assert sim.status == "failed"
    hub = client.app.state.hub  # type: ignore[attr-defined]
    status, hello = hub.status_message(), hub.hello()
    assert parse_message(status).status == "failed"  # type: ignore[attr-defined]
    assert parse_message(hello).status == "failed"  # type: ignore[attr-defined]
    state = client.get("/api/state").json()
    assert state["tick"] == last_tick and state["status"] == "failed"
    row = sim.db.fetchone("SELECT status, ended_reason FROM run WHERE run_id=?", (sim.run_id,))
    assert row["status"] == "failed" and "forced tick failure" in row["ended_reason"]


def test_failed_tick_in_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    sim = make_sim(tmp_path / "run", "server_memory_failed_loop", 1)
    _fail_from(monkeypatch, sim, 2)
    app = create_app(sim, static_dir=None, host="127.0.0.1", port=8000)
    with caplog.at_level(logging.ERROR, logger="void.server.driver"), TestClient(app) as client:
        wait_finished(client)
        _assert_failed(client, sim, 2)
        assert app.state.driver.error is not None
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert parse_message(hello) and hello["status"] == "failed"
    assert "simulation task failed" in caplog.text


def test_failed_tick_under_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sim = make_sim(tmp_path / "run", "server_memory_failed_step", 1)
    sim.pause()
    app = create_app(sim, static_dir=None, host="127.0.0.1", port=8000)
    with TestClient(app) as client:
        for _ in range(2):
            assert client.post("/api/control/step", headers=auth(sim)).status_code == 200
        _fail_from(monkeypatch, sim, 2)
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "hello"
            r = client.post("/api/control/step", headers=auth(sim))
            assert r.status_code == 500 and "failed" in r.json()["detail"]
            for _ in range(500):
                m = ws.receive_json()
                parse_message(m)
                if m["type"] == "status":
                    break
            assert m["type"] == "status" and m["status"] == "failed"
        _assert_failed(client, sim, 2)
        assert client.post("/api/control/step", headers=auth(sim)).status_code == 409


def test_invalid_status_is_logged_and_still_sent(caplog: pytest.LogCaptureFixture) -> None:
    msg = {"type": "status", "paused": False, "tick_seconds": 0.0, "status": "exploded"}
    with caplog.at_level(logging.WARNING, logger="void.server.ws"):
        assert checked(msg) is msg
    assert "status message does not match the wire protocol" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="void.server.ws"):
        checked({"type": "status", "paused": False, "tick_seconds": 0.0, "status": "failed"})
    assert caplog.text == ""
