"""HTTP + WebSocket server (DESIGN §15) against a 1-day scripted simulation paused at start."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from void.config import load_config
from void.server.app import create_app
from void.server.auth import CSP
from void.server.protocol import parse_message
from void.sim.loop import Simulation

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "scripted_smoke.yaml"
FOREIGN = {"Origin": "http://evil.example"}


@pytest.fixture
def sim(tmp_path: Path) -> Simulation:
    cfg = load_config(CONFIG, {"run": {"name": "server_test", "days": 1, "tick_seconds": 0.0}})
    s = Simulation(cfg, tmp_path / "run")
    s.pause()
    return s


@pytest.fixture
def client(sim: Simulation):
    app = create_app(sim, static_dir=None, host="127.0.0.1", port=8000)
    with TestClient(app) as c:
        yield c


def auth(sim: Simulation) -> dict[str, str]:
    return {"Authorization": f"Bearer {sim.operator_token}"}


def step(client: TestClient, sim: Simulation, n: int = 1) -> int:
    tick = -1
    for _ in range(n):
        r = client.post("/api/control/step", headers=auth(sim))
        assert r.status_code == 200, r.text
        tick = r.json()["tick"]
    return tick


def recv_until(ws: Any, pred, limit: int = 2000) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    for _ in range(limit):
        m = ws.receive_json()
        parse_message(m)
        seen.append(m)
        if pred(m):
            return seen
    raise AssertionError("message not received")


# --- auth / origin -----------------------------------------------------------------------------------------
def test_session_same_origin_only(client: TestClient, sim: Simulation) -> None:
    r = client.get("/api/session")
    assert r.status_code == 200 and r.json() == {"token": sim.operator_token}
    assert "access-control-allow-origin" not in r.headers
    assert client.get("/api/session", headers={"Origin": "http://127.0.0.1:8000"}).status_code == 200
    assert client.get("/api/session", headers={"Origin": "http://localhost:8000"}).status_code == 200
    assert client.get("/api/session", headers=FOREIGN).status_code == 403
    assert client.get("/api/session", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/session", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_post_needs_token_and_same_origin(client: TestClient, sim: Simulation) -> None:
    body = {"title": "Dig a well", "description": "Find water", "reward_usd": 0.25}
    assert client.post("/api/tasks", json=body).status_code == 401
    assert client.post("/api/tasks", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.post("/api/tasks", json=body, headers=auth(sim))
    assert r.status_code == 202, r.text
    ack = r.json()
    assert isinstance(ack["cmd_id"], int) and ack["will_apply_at_tick"] == sim.clock.tick + 1
    assert client.post("/api/tasks", json=body, headers={**auth(sim), **FOREIGN}).status_code == 403
    assert client.post("/api/tasks", json={"title": "", "reward_usd": 1}, headers=auth(sim)).status_code == 422


def test_csp_header(client: TestClient, sim: Simulation) -> None:
    step(client, sim)
    r = client.get("/api/state")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == CSP
    assert r.json()["type"] == "snapshot" and "ts_ms" in r.json()
    assert client.get("/").headers["content-security-policy"] == CSP


# --- websocket ---------------------------------------------------------------------------------------------
def test_ws_preamble_backlog_and_live(client: TestClient, sim: Simulation) -> None:
    step(client, sim, 3)
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        parse_message(hello)
        assert hello["type"] == "hello" and hello["run_id"] == sim.run_id and hello["paused"] is True
        assert hello["tick"] == 3 and hello["status"] == "running" and hello["last_seq"] > 0
        assert set(hello["config"]["tiers"]) == set(sim.cfg.tiers)
        for expected in ("roster", "gadgets", "tasks", "chronicle"):
            m = ws.receive_json()
            parse_message(m)
            assert m["type"] == expected, m
            if expected == "roster":
                assert len(m["agents"]) == 6
        snaps = [ws.receive_json() for _ in range(3)]
        for s in snaps:
            parse_message(s)
        assert [s["type"] for s in snaps] == ["snapshot"] * 3
        assert [s["tick"] for s in snaps] == [1, 2, 3]
        ts = [s["ts_ms"] for s in snaps]
        assert ts == sorted(ts)
        assert all(len(s["agents"]) == 6 and len(s["nodes"]) > 0 for s in snaps)

        tick = step(client, sim)
        assert tick == 4
        seen = recv_until(ws, lambda m: m["type"] == "snapshot" and m["tick"] == 4)
        assert seen[-1]["ts_ms"] >= ts[-1]
        seen += recv_until(ws, lambda m: m["type"] == "metrics" and m["tick"] == 4)
        assert seen[-1]["row"]["tick"] == 4 and "population" in seen[-1]["row"]
        events = [m for m in seen if m["type"] == "event"]
        assert events and all(e["tick"] == 4 for e in events)
        assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)

        r = client.post("/api/control/speed", json={"tick_seconds": 0.5}, headers=auth(sim))
        assert r.status_code == 200 and r.json()["tick_seconds"] == 0.5
        st = recv_until(ws, lambda m: m["type"] == "status")[-1]
        assert st["paused"] is True and st["tick_seconds"] == 0.5 and st["status"] == "running"


def test_ws_rejects_bad_token(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=wrong"):
            pass


def test_ws_accepts_good_token_and_rejects_foreign_origin(client: TestClient, sim: Simulation) -> None:
    with client.websocket_connect(f"/ws?token={sim.operator_token}") as ws:
        assert ws.receive_json()["type"] == "hello"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers=FOREIGN):
            pass


# --- catch-up and read endpoints ---------------------------------------------------------------------------
def test_events_catchup_is_ordered_and_idempotent(client: TestClient, sim: Simulation) -> None:
    step(client, sim, 2)
    a = client.get("/api/events", params={"since_seq": 0, "limit": 100}).json()
    b = client.get("/api/events", params={"since_seq": 0, "limit": 100}).json()
    assert a == b and a["events"]
    seqs = [e["seq"] for e in a["events"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    for e in a["events"]:
        parse_message(e)
    last = seqs[-1]
    tail = client.get("/api/events", params={"since_seq": last}).json()["events"]
    assert all(e["seq"] > last for e in tail)
    assert client.get("/api/events", params={"limit": 6000}).status_code == 422
    m = client.get("/api/metrics", params={"since_tick": 0}).json()["metrics"]
    assert [x["tick"] for x in m] == [1, 2]
    s = client.get("/api/snapshots", params={"since_tick": 1}).json()["snapshots"]
    assert [x["tick"] for x in s] == [2]


def test_agent_endpoints(client: TestClient, sim: Simulation) -> None:
    step(client, sim, 2)
    roster = sim.roster()
    a, b = roster[0]["id"], roster[1]["id"]
    r = client.get(f"/api/agents/{a}")
    assert r.status_code == 200
    d = r.json()
    assert d["agent"]["id"] == a and d["agent"]["status"] == "alive"
    assert d["self_versions"] and d["self_versions"][0]["version"] == 1
    assert d["notes"] and {"note_id", "title", "tags", "created_tick", "channel"} <= set(d["notes"][0])
    assert d["calls"] and {"tick", "t_eff", "coherence", "cost_usd", "degenerate", "stop_reason", "action_type"} <= set(d["calls"][0])
    assert d["balance_series"] and {"tick", "balance_usd"} <= set(d["balance_series"][0])
    assert client.get("/api/agents/ag_nope").status_code == 404
    own = d["notes"][0]["note_id"]
    n = client.get(f"/api/agents/{a}/notes/{own}")
    assert n.status_code == 200 and n.json()["note_id"] == own and isinstance(n.json()["body"], str)
    assert client.get(f"/api/agents/{b}/notes/{own}").status_code == 404
    assert client.get(f"/api/agents/{a}/notes/note_missing").status_code == 404


def test_tree_graveyard_boards(client: TestClient, sim: Simulation) -> None:
    step(client, sim)
    tree = client.get("/api/tree").json()
    assert len(tree["roots"]) == 6 and tree["count"] == 6 and tree["roots"][0]["children"] == []
    assert client.get("/api/graveyard").json() == {"agents": []}
    g = client.get("/api/gadgets").json()
    assert g["type"] == "gadgets" and "rev" in g and isinstance(g["items"], list)
    t = client.get("/api/tasks").json()
    assert t["type"] == "tasks" and t["rev"] == 0 and t["items"] == []


def test_task_commands_apply_next_tick(client: TestClient, sim: Simulation) -> None:
    r = client.post("/api/tasks", json={"title": "Dig a well", "description": "Find water", "reward_usd": 0.25}, headers=auth(sim))
    assert r.status_code == 202 and r.json()["will_apply_at_tick"] == 1
    step(client, sim)
    items = client.get("/api/tasks").json()["items"]
    assert len(items) == 1 and items[0]["status"] == "open" and items[0]["reward_usd"] == 0.25
    tid = items[0]["id"]
    assert client.post(f"/api/tasks/{tid}/approve/ap_nope", headers=auth(sim)).status_code == 404
    assert client.post(f"/api/tasks/{tid}/complete", headers=auth(sim)).status_code == 409
    assert client.post("/api/tasks/tk_nope/complete", headers=auth(sim)).status_code == 404
    assert sim.control.result(r.json()["cmd_id"])["result"]["ok"] is True


def test_control_epoch_and_benefactor(client: TestClient, sim: Simulation) -> None:
    r = client.post("/api/control/epoch", json={"kind": "drought", "scarcity": 0.5, "duration_days": 1}, headers=auth(sim))
    assert r.status_code == 202 and "cmd_id" in r.json()
    assert client.post("/api/control/epoch", json={"kind": "arrival"}, headers=auth(sim)).status_code == 422
    assert client.post("/api/control/benefactor", json={}, headers=auth(sim)).status_code == 422
    assert client.post("/api/control/benefactor", json={"agent_id": "ag_nope", "amount_usd": 0.5}, headers=auth(sim)).status_code == 404
    aid = sim.roster()[0]["id"]
    r = client.post("/api/control/benefactor", json={"agent_id": aid, "amount_usd": 0.5}, headers=auth(sim))
    assert r.status_code == 202
    step(client, sim)
    assert sim.control.result(r.json()["cmd_id"])["result"]["ok"] is True
    snap = client.get("/api/state").json()
    assert snap["scarcity"] == 0.5 and snap["epochs"]["active"] is not None


def test_step_only_while_paused(client: TestClient, sim: Simulation) -> None:
    r = client.post("/api/control/resume", headers=auth(sim))
    assert r.status_code == 200 and r.json()["paused"] is False
    assert client.post("/api/control/step", headers=auth(sim)).status_code == 409
    r = client.post("/api/control/pause", headers=auth(sim))
    assert r.status_code == 200 and r.json()["paused"] is True
    assert client.post("/api/control/step", headers=auth(sim)).status_code == 200


def test_step_to_the_end_finishes_the_run(client: TestClient, sim: Simulation) -> None:
    ticks = sim.cfg.run.days * sim.cfg.run.ticks_per_day
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, lambda m: m["type"] == "chronicle")
        last = None
        for _ in range(ticks):
            r = client.post("/api/control/step", headers=auth(sim))
            assert r.status_code == 200, r.text
            last = r.json()
        assert last is not None and last["tick"] == ticks and last["status"] == "completed"
        st = recv_until(ws, lambda m: m["type"] == "status" and m["status"] == "completed")
        assert any(m["type"] == "day" and m["day"] == 1 for m in st)
        assert st[-1]["paused"] is True
    assert sim.status == "completed" and sim.clock.tick == ticks
    assert client.post("/api/control/step", headers=auth(sim)).status_code == 409
    assert client.post("/api/control/resume", headers=auth(sim)).status_code == 409
    assert client.post("/api/tasks", json={"title": "late", "reward_usd": 0.1}, headers=auth(sim)).status_code == 409
    days = client.get("/api/metrics", params={"days": 1}).json()["days"]
    assert [d["day"] for d in days] == [1] and days[0]["row"]["kind"] == "day"
    c = client.get("/api/chronicle")
    assert c.status_code == 200 and c.json()["day"] == 1 and c.json()["markdown"].startswith("# ")
    assert client.get("/api/chronicle", params={"day": 1}).json()["headline"] == c.json()["headline"]
    assert client.get("/api/chronicle", params={"day": 7}).status_code == 404
    assert client.get("/api/state").json()["tick"] == ticks


def test_static_spa_and_traversal_guard(sim: Simulation, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>void</title>")
    (dist / "assets" / "app.js").write_text("console.log('void')")
    (tmp_path / "secret.txt").write_text("nope")
    app = create_app(sim, static_dir=dist, host="127.0.0.1", port=8000)
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200 and "<title>void</title>" in r.text and r.headers["content-security-policy"] == CSP
        assert c.get("/assets/app.js").text == "console.log('void')"
        fallback = c.get("/agents/ag_123")
        assert fallback.status_code == 200 and "<title>void</title>" in fallback.text
        assert fallback.headers["content-security-policy"] == CSP
        assert c.get("/api/nope").status_code == 404
        for probe in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
            resp = c.get(probe)
            assert "nope" not in resp.text, probe
