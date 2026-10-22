"""GET /api/events/aggregates (DESIGN §15): the whole-run totals of the web's event-derived charts, in SQL.

One served scripted run of configs/demo_memory.yaml (three days, with an epoch on day 1) runs to completion inside a
TestClient. The route's totals must equal direct SQL counts over the run's events table, and its groups must equal
a Python fold of the same events that follows the web reducer (web/src/state/series.ts ``Series.onEvent``) rule for
rule, so a tab seeded with the answer holds what a tab that counted every event holds. A hand-made events table
covers the payload shapes the scripted world never writes (a missing ``truthful``, a non-integral generation, an
empty tier, a speaker that is not a string). The body is held to its wire model, the committed schema and the
``protocol.ts`` interfaces.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, deep_merge
from fastapi.testclient import TestClient
from test_protocol_sync import ts_interface_fields

from void.config import load_config
from void.db import Database
from void.events import Event
from void.server.api import build_event_aggregates
from void.server.app import create_app
from void.server.protocol import (
    EPOCH_EVENTS_LIMIT,
    GATE_EVENTS_LIMIT,
    RESPONSE_MODELS,
    RESPONSE_SCHEMA_DIR,
    ClaimTally,
    DayCount,
    DegenerationAggregate,
    EventAggregates,
    ExamDayTally,
    GenerationCount,
    GossipEdgeTally,
    MemoryAgentTally,
    MemoryAggregate,
    TierCollapse,
    export_schemas,
)
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "configs" / "demo_memory.yaml"
SCHEMAS = REPO / "schemas"
DAYS = 3
FOREIGN = {"Origin": "http://evil.example"}
#: the web's MAX_GENERATIONS (series.ts): generations past it clamp into the last bar
MAX_GENERATIONS = 12


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TestClient, Simulation]]:
    overrides = {"run": {"name": "server_event_aggregates", "days": DAYS, "tick_seconds": 0.0},
                 "epochs": [{"day": 1, "kind": "drought", "scarcity": 0.5, "weather_baseline": -0.5, "duration_days": 1}]}
    cfg = load_config(CONFIG, deep_merge(NO_PROVIDER_TIERS, overrides))
    sim = Simulation(cfg, tmp_path_factory.mktemp("server_event_aggregates") / "run")
    app = create_app(sim, static_dir=None, host="127.0.0.1", port=8000)
    with TestClient(app) as client:
        driver = client.app.state.driver  # type: ignore[attr-defined]
        deadline = time.monotonic() + 120
        while not driver.finished.is_set():
            assert time.monotonic() < deadline, "the served run did not finish"
            time.sleep(0.05)
        assert sim.status == "completed", sim.status
        yield client, sim


# --- the web reducer, in Python ---------------------------------------------------------------------------------
def _num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _finite(v: Any) -> bool:
    return _num(v) and math.isfinite(v)


def _nonempty(v: Any) -> bool:
    return isinstance(v, str) and v != ""


def _truthy(v: Any) -> bool:
    """JavaScript truthiness of a JSON value."""
    if v is None or isinstance(v, bool):
        return bool(v)
    if _num(v):
        return v != 0 and not math.isnan(v)
    if isinstance(v, str):
        return v != ""
    return True  # an object or an array


def fold(rows: list[dict[str, Any]], as_of: int | None = None) -> dict[str, Any]:
    """The aggregates a client builds by passing every event (seq order) through Series.onEvent, as wire groups."""
    by_gen: dict[int | None, int] = {}
    t_c: dict[str, float] = {}
    claims: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    gossip: dict[tuple[str, str], dict[str, Any]] = {}
    agents: dict[str, dict[str, Any]] = {}
    skipped: dict[int, int] = {}
    exams: dict[tuple[int, str], dict[str, Any]] = {}
    epochs: list[int] = []
    gate: list[int] = []
    upto = 0

    def agent(aid: str) -> dict[str, Any]:
        return agents.setdefault(aid, {"agent_id": aid, "maintenance_real_micro": 0, "maintenance_world_micro": 0,
                                       "maintenance_world_self_micro": 0, "maintenance_calls": 0, "maintenance_skipped": 0,
                                       "asked": 0, "correct": 0, "abstained": 0, "failed": 0})

    for r in rows:
        if as_of is not None and r["seq"] > as_of:
            break
        upto = r["seq"]
        kind, p = r["kind"], r["payload"]
        aid = p.get("agent_id") if _nonempty(p.get("agent_id")) else r["agent_id"]
        if kind == "degeneration":
            g = p.get("generation")
            key = int(g) if _finite(g) and float(g).is_integer() else None
            by_gen[key] = by_gen.get(key, 0) + 1
            if _nonempty(p.get("tier")) and _finite(p.get("t_c")):
                t_c[p["tier"]] = float(p["t_c"])
        elif kind == "claim":
            speaker = p.get("agent_id") if isinstance(p.get("agent_id"), str) else None
            tier = p["tier"] if _nonempty(p.get("tier")) else None
            c = claims.setdefault((speaker, tier), {"agent_id": speaker, "tier": tier, "made": 0, "falsy": 0})
            c["made"] += 1
            c["falsy"] += 0 if _truthy(p.get("truthful")) else 1
        elif kind == "gossip_transfer":
            ends = tuple(p.get(k) if isinstance(p.get(k), str) else "" for k in ("speaker_id", "listener_id"))
            e = gossip.setdefault(ends, {"speaker_id": ends[0], "listener_id": ends[1], "count": 0, "last_tick": r["tick"]})
            e["count"] += 1
            e["last_tick"] = max(e["last_tick"], r["tick"])
        elif kind == "maintenance":
            if aid:
                a = agent(aid)
                world = p.get("world_cost") if _finite(p.get("world_cost")) else 0
                a["maintenance_real_micro"] += p.get("real_cost") if _finite(p.get("real_cost")) else 0
                a["maintenance_world_micro"] += world
                a["maintenance_world_self_micro"] += world if p.get("payer") == aid else 0
                a["maintenance_calls"] += 1
        elif kind == "maintenance_skipped":
            skipped[r["day"]] = skipped.get(r["day"], 0) + 1
            if aid:
                agent(aid)["maintenance_skipped"] += 1
        elif kind == "exam_answered":
            qk = p["kind"] if _nonempty(p.get("kind")) else "unknown"
            x = exams.setdefault((r["day"], qk), {"day": r["day"], "kind": qk, "asked": 0, "correct": 0, "abstained": 0, "failed": 0})
            tallies = [x, agent(aid)] if aid else [x]
            for t in tallies:
                if p.get("failed") is True:
                    t["failed"] += 1
                else:
                    t["asked"] += 1
                    t["correct"] += p.get("correct") is True
                    t["abstained"] += p.get("abstained") is True
        elif kind == "epoch":
            epochs.append(r["seq"])
        elif kind in ("gadget_verified", "gadget_rejected"):
            gate.append(r["seq"])
    return {
        "as_of_seq": upto,
        "degeneration": {"by_generation": sorted(({"generation": g, "count": n} for g, n in by_gen.items()),
                                                 key=lambda d: (d["generation"] is not None, d["generation"] or 0)),
                         "t_c_by_tier": [{"tier": k, "t_c": v} for k, v in t_c.items()]},
        "claims": list(claims.values()),
        "gossip": list(gossip.values()),
        "memory": {"agents": list(agents.values()), "skipped_by_day": [{"day": d, "count": n} for d, n in sorted(skipped.items())],
                   "exams": list(exams.values())},
        "epochs": epochs[-EPOCH_EVENTS_LIMIT:],
        "gate": gate[-GATE_EVENTS_LIMIT:],
    }


def event_rows(db: Any) -> list[dict[str, Any]]:
    return [{"seq": int(r["seq"]), "tick": int(r["tick"]), "day": int(r["day"]), "kind": r["kind"], "agent_id": r["agent_id"],
             "payload": json.loads(r["payload"])} for r in db.fetchall("SELECT * FROM events ORDER BY seq")]


def comparable(body: dict[str, Any]) -> dict[str, Any]:
    """The route's body with the event lists reduced to their seqs and the sums as plain numbers."""
    out = json.loads(json.dumps(body))
    out["epochs"] = [e["seq"] for e in out["epochs"]]
    out["gate"] = [e["seq"] for e in out["gate"]]
    for a in out["memory"]["agents"]:
        for k in ("maintenance_real_micro", "maintenance_world_micro", "maintenance_world_self_micro"):
            a[k] = int(a[k]) if float(a[k]).is_integer() else a[k]
    return out


def count(db: Any, sql: str, *params: Any) -> int:
    row = db.fetchone(sql, params)
    return int(row[0] or 0) if row else 0


# --- the served run -----------------------------------------------------------------------------------------------
def test_totals_equal_direct_sql_counts(served: tuple[TestClient, Simulation]) -> None:
    client, sim = served
    r = client.get("/api/events/aggregates")
    assert r.status_code == 200, r.text
    body = r.json()
    db = sim.db
    assert body["as_of_seq"] == count(db, "SELECT MAX(seq) FROM events")

    degen = count(db, "SELECT COUNT(*) FROM events WHERE kind = 'degeneration'")
    claims = count(db, "SELECT COUNT(*) FROM events WHERE kind = 'claim'")
    false_claims = count(db, "SELECT COUNT(*) FROM events WHERE kind = 'claim' AND json_extract(payload, '$.truthful') = 0")
    transfers = count(db, "SELECT COUNT(*) FROM events WHERE kind = 'gossip_transfer'")
    maintenance = count(db, "SELECT COUNT(*) FROM events WHERE kind = 'maintenance'")
    skipped = count(db, "SELECT COUNT(*) FROM events WHERE kind = 'maintenance_skipped'")
    answered = count(db, "SELECT COUNT(*) FROM events WHERE kind = 'exam_answered'")
    epochs = count(db, "SELECT COUNT(*) FROM events WHERE kind = 'epoch'")
    gate = count(db, "SELECT COUNT(*) FROM events WHERE kind IN ('gadget_verified', 'gadget_rejected')")
    # the run exercises every chart (skipped maintenance may legitimately be zero in three days)
    assert min(degen, claims, false_claims, transfers, maintenance, answered, epochs, gate) > 0

    assert sum(g["count"] for g in body["degeneration"]["by_generation"]) == degen
    assert sum(c["made"] for c in body["claims"]) == claims
    assert sum(c["falsy"] for c in body["claims"]) == false_claims
    assert sum(g["count"] for g in body["gossip"]) == transfers
    for g in body["gossip"]:
        assert g["count"] == count(db, "SELECT COUNT(*) FROM events WHERE kind = 'gossip_transfer' AND "
                                       "json_extract(payload, '$.speaker_id') = ? AND json_extract(payload, '$.listener_id') = ?",
                                   g["speaker_id"], g["listener_id"])
    mem = body["memory"]
    assert sum(a["maintenance_calls"] for a in mem["agents"]) == maintenance
    assert sum(a["maintenance_real_micro"] for a in mem["agents"]) == count(
        db, "SELECT SUM(json_extract(payload, '$.real_cost')) FROM events WHERE kind = 'maintenance'")
    assert sum(d["count"] for d in mem["skipped_by_day"]) == skipped
    assert sum(x["asked"] + x["failed"] for x in mem["exams"]) == answered
    assert sum(x["correct"] for x in mem["exams"]) == count(
        db, "SELECT COUNT(*) FROM events WHERE kind = 'exam_answered' AND json_extract(payload, '$.correct') = 1 "
            "AND COALESCE(json_extract(payload, '$.failed'), 0) = 0")
    assert [e["seq"] for e in body["epochs"]] == [int(r["seq"]) for r in db.fetchall(
        "SELECT seq FROM events WHERE kind = 'epoch' ORDER BY seq")]
    assert [e["seq"] for e in body["gate"]] == [int(r["seq"]) for r in db.fetchall(
        "SELECT seq FROM (SELECT seq FROM events WHERE kind IN ('gadget_verified', 'gadget_rejected') ORDER BY seq DESC LIMIT ?) "
        "ORDER BY seq", (GATE_EVENTS_LIMIT,))]
    assert all(e["type"] == "event" for e in body["epochs"] + body["gate"])


def test_groups_equal_the_web_reducer_over_every_event(served: tuple[TestClient, Simulation]) -> None:
    client, sim = served
    body = client.get("/api/events/aggregates").json()
    assert comparable(body) == fold(event_rows(sim.db))


def test_as_of_seq_counts_nothing_after_it(served: tuple[TestClient, Simulation]) -> None:
    client, sim = served
    rows = event_rows(sim.db)
    newest = rows[-1]["seq"]
    for as_of in (0, 1, newest // 3, newest // 2 + 7, newest):
        body = client.get(f"/api/events/aggregates?as_of_seq={as_of}").json()
        assert body["as_of_seq"] == as_of
        assert comparable(body) == fold(rows, as_of)
    # past the newest event: capped, so a client counting events after it misses none
    assert client.get(f"/api/events/aggregates?as_of_seq={newest + 1000}").json()["as_of_seq"] == newest
    # the seed and the events after it add up to the whole run
    half = newest // 2
    seed, rest = fold(rows, half), fold([r for r in rows if r["seq"] > half])
    whole = fold(rows)
    assert sum(c["made"] for c in seed["claims"]) + sum(c["made"] for c in rest["claims"]) == sum(c["made"] for c in whole["claims"])
    assert client.get("/api/events/aggregates?as_of_seq=-1").status_code == 422


def test_open_like_the_events_route_and_origin_guarded(served: tuple[TestClient, Simulation]) -> None:
    client, _ = served
    assert client.get("/api/events/aggregates").status_code == 200  # no token, like /api/events
    assert client.get("/api/events/aggregates", headers=FOREIGN).status_code == 403


# --- payload shapes the scripted world never writes -----------------------------------------------------------------
EDGE_EVENTS: list[tuple[str, dict[str, Any], str | None]] = [
    ("degeneration", {"generation": 2, "t_c": 1.8, "tier": "dim"}, "a"),
    ("degeneration", {"generation": 2.0, "t_c": 1.9, "tier": "dim"}, "a"),     # integral real: a bar
    ("degeneration", {"generation": 2.5, "t_c": "1.7", "tier": "sharp"}, "a"),  # total only; a string t_c is no T_c
    ("degeneration", {"generation": -1, "tier": ""}, "a"),                    # clamps to 0 in the client; no tier
    ("degeneration", {"generation": 40, "t_c": 2, "tier": "genius"}, "a"),
    ("degeneration", {"generation": "3", "t_c": 1.5, "tier": "sharp"}, "a"),  # a string generation: total only
    ("degeneration", {}, None),
    ("claim", {"agent_id": "a", "tier": "dim", "truthful": True}, "a"),
    ("claim", {"agent_id": "a", "tier": "dim"}, "a"),                        # no truthful: false
    ("claim", {"agent_id": "a", "tier": "dim", "truthful": 0}, "a"),
    ("claim", {"agent_id": "a", "tier": "dim", "truthful": "yes"}, "a"),
    ("claim", {"agent_id": "a", "tier": "dim", "truthful": ""}, "a"),
    ("claim", {"agent_id": "a", "tier": "dim", "truthful": {}}, "a"),          # an object is truthy
    ("claim", {"agent_id": "a", "tier": "", "truthful": False}, "a"),
    ("claim", {"agent_id": 7, "tier": "slow", "truthful": False}, None),
    ("gossip_transfer", {"speaker_id": "a", "listener_id": "b"}, "b"),
    ("gossip_transfer", {"speaker_id": "a", "listener_id": "b"}, "b"),
    ("gossip_transfer", {"speaker_id": None, "listener_id": "b"}, "b"),
    ("gossip_transfer", {"listener_id": "b"}, "b"),                          # merges with the null speaker: ''
    ("maintenance", {"agent_id": "a", "payer": "a", "real_cost": 120, "world_cost": 300}, "a"),
    ("maintenance", {"agent_id": "a", "payer": "pool", "real_cost": 80, "world_cost": 100}, "a"),
    ("maintenance", {"payer": "b", "real_cost": "9", "world_cost": 5}, "b"),  # agent from the event; a string cost is 0
    ("maintenance", {"real_cost": 50}, None),                                # nobody: not counted
    ("maintenance_skipped", {"agent_id": "c"}, "c"),
    ("maintenance_skipped", {}, None),                                       # counted per day, no agent
    ("exam_answered", {"agent_id": "a", "kind": "node", "correct": True}, "a"),
    ("exam_answered", {"agent_id": "a", "kind": "node", "correct": 1, "abstained": True}, "a"),  # 1 is not === true
    ("exam_answered", {"agent_id": "a", "kind": "", "failed": True, "correct": True}, "a"),
    ("exam_answered", {"kind": "balance", "correct": True}, None),
    ("epoch", {"kind": "drought", "id": "op1", "phase": "started", "ends_day": 3}, None),
    ("gadget_rejected", {"gadget_id": "g1", "stage": "tests"}, "a"),
]


@pytest.fixture
def edge_db(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "world.db")
    with db.tx():
        for i, (kind, payload, agent_id) in enumerate(EDGE_EVENTS):
            db.persist_event(Event(i + 1, (i + 1) // 10, kind, payload, agent_id))
    yield db
    db.close()


def test_edge_payloads_follow_the_web_reducer(edge_db: Database) -> None:
    body = EventAggregates.model_validate(build_event_aggregates(edge_db)).model_dump(mode="json")
    assert comparable(body) == fold(event_rows(edge_db))
    # spot checks of the rules the fold encodes
    assert body["degeneration"]["by_generation"] == [{"generation": None, "count": 3}, {"generation": -1, "count": 1},
                                                     {"generation": 2, "count": 2}, {"generation": 40, "count": 1}]
    assert body["degeneration"]["t_c_by_tier"] == [{"tier": "dim", "t_c": 1.9}, {"tier": "genius", "t_c": 2.0},
                                                   {"tier": "sharp", "t_c": 1.5}]
    dim = next(c for c in body["claims"] if c["agent_id"] == "a" and c["tier"] == "dim")
    assert (dim["made"], dim["falsy"]) == (6, 3)
    assert [g for g in body["gossip"] if g["speaker_id"] == ""][0]["count"] == 2
    agents = {a["agent_id"]: a for a in body["memory"]["agents"]}
    assert agents["a"]["maintenance_real_micro"] == 200 and agents["a"]["maintenance_world_self_micro"] == 300
    assert agents["b"]["maintenance_calls"] == 1 and agents["b"]["maintenance_real_micro"] == 0
    assert (agents["a"]["asked"], agents["a"]["correct"], agents["a"]["abstained"], agents["a"]["failed"]) == (2, 1, 1, 1)
    assert sum(d["count"] for d in body["memory"]["skipped_by_day"]) == 2
    assert {x["kind"] for x in body["memory"]["exams"]} == {"node", "unknown", "balance"}


def test_empty_events_table(tmp_path: Path) -> None:
    db = Database(tmp_path / "world.db")
    body = EventAggregates.model_validate(build_event_aggregates(db, 10)).model_dump(mode="json")
    assert body["as_of_seq"] == 0
    assert body["claims"] == body["gossip"] == body["epochs"] == body["gate"] == []
    db.close()


# --- wire model, schema, protocol.ts --------------------------------------------------------------------------------
def test_committed_schema_matches_the_model(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    for name in RESPONSE_MODELS:
        rel = Path(RESPONSE_SCHEMA_DIR) / f"{name}.schema.json"
        assert (tmp_path / rel).read_text() == (SCHEMAS / rel).read_text(), f"schemas/{rel} is stale: run `void schema --out schemas`"


@pytest.mark.parametrize(("model", "ts_name"), [
    (EventAggregates, "EventAggregatesResponse"), (DegenerationAggregate, "DegenerationAggregate"),
    (GenerationCount, "GenerationCount"), (TierCollapse, "TierCollapse"), (ClaimTally, "ClaimTally"),
    (GossipEdgeTally, "GossipEdgeTally"), (MemoryAggregate, "MemoryAggregate"), (MemoryAgentTally, "MemoryAgentTally"),
    (DayCount, "DayCount"), (ExamDayTally, "ExamDayTally"),
])
def test_protocol_ts_mirrors_the_wire_model(model: Any, ts_name: str) -> None:
    ts = ts_interface_fields(ts_name)
    assert set(ts) == set(model.model_fields), ts_name
    assert all(ts.values()), f"{ts_name}: every field is always sent"
