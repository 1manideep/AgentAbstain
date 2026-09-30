"""The HTTP API (DESIGN §15). All JSON. GETs are open; every POST needs the operator token.

POSTs that map to operator commands only enqueue (DESIGN §14.1) and answer
``{cmd_id, will_apply_at_tick}``; the tick loop applies them in ``cmd_id`` order. ``pause``,
``resume``, ``speed`` and ``step`` act on loop-level flags immediately.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from void.config import micro_to_usd
from void.events import OPERATOR, Event, Kind
from void.server.auth import is_same_origin_fetch, require_operator
from void.server.driver import RunEnded

__all__ = ["router", "MAX_LIMIT"]

router = APIRouter(prefix="/api")
MAX_LIMIT = 5000
CALL_HISTORY = 240
BALANCE_HISTORY = 240


def _sim(request: Request) -> Any:
    return request.app.state.sim


def _hub(request: Request) -> Any:
    return request.app.state.hub


def _ack(sim: Any, cmd_id: int) -> dict[str, Any]:
    return {"cmd_id": cmd_id, "will_apply_at_tick": int(sim.clock.tick) + 1}


def _require_running(sim: Any) -> None:
    if sim.status != "running":
        raise HTTPException(status_code=409, detail=f"run is {sim.status}")


def _parse_action(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def _agent_record(rec: Any) -> dict[str, Any]:
    try:
        personality = json.loads(rec.seed.to_json())
    except Exception:
        personality = None
    return {
        "id": rec.agent_id, "name": rec.name, "tier": rec.model_tier, "generation": rec.generation,
        "parent_id": rec.parent_id, "born_tick": rec.born_tick, "died_tick": rec.died_tick, "status": rec.status.value,
        "balance_usd": round(micro_to_usd(rec.balance), 6), "x": rec.pos.x, "y": rec.pos.y, "heading": rec.heading,
        "stress": rec.stress, "asleep": rec.asleep, "entropy_budget": rec.entropy_budget,
        "last_action": _parse_action(rec.last_action), "last_action_ok": rec.last_action_ok,
        "last_forage_tick": rec.last_forage_tick, "effects": dict(rec.effects), "personality": personality,
        "memory_path": rec.memory_path,
    }


def _note_summary(note: Any) -> dict[str, Any]:
    prov = note.provenance
    return {"note_id": note.note_id, "title": note.title, "tags": list(note.tags), "created_tick": note.created_tick,
            "channel": prov.channel, "hop": prov.hop, "importance": note.importance, "archived": bool(note.archived),
            "body": note.body}


# --- session / state -----------------------------------------------------------------------------------------
@router.get("/session")
async def session(request: Request) -> dict[str, str]:
    """The operator token, only for same-origin fetches (no CORS headers are ever sent)."""
    if not is_same_origin_fetch(request):
        raise HTTPException(status_code=403, detail="same-origin only")
    return {"token": _sim(request).operator_token}


@router.get("/state")
async def state(request: Request) -> dict[str, Any]:
    snap = _hub(request).latest_snapshot()
    if snap is None:
        raise HTTPException(status_code=404, detail="no snapshot yet")
    return snap


# --- agents --------------------------------------------------------------------------------------------------
def build_agent_detail(sim: Any, agent_id: str) -> dict[str, Any] | None:
    """The /api/agents/{id} payload; shared by the route and the fixture recorder."""
    rec = sim.registry.get(agent_id)
    if rec is None:
        return None
    db = sim.db
    calls_rows = db.fetchall(
        "SELECT tick, purpose, effective_temperature, text_coherence, world_cost, real_cost, degenerate_induced, "
        "corruption_mode, stop_reason, action_type, latency_ms, invalid_action, stale_action, stress, error "
        "FROM llm_calls WHERE agent_id=? ORDER BY tick DESC, call_id DESC LIMIT ?", (agent_id, CALL_HISTORY))
    calls = [{
        "tick": int(r["tick"]), "purpose": r["purpose"], "t_eff": r["effective_temperature"],
        "coherence": r["text_coherence"], "text_coherence": r["text_coherence"],
        "cost_usd": round(int(r["world_cost"]) / 1e6, 6), "world_cost_usd": round(int(r["world_cost"]) / 1e6, 6),
        "real_cost_usd": round(int(r["real_cost"]) / 1e6, 6), "degenerate": bool(r["degenerate_induced"]),
        "degenerate_induced": int(r["degenerate_induced"] or 0), "corruption_mode": r["corruption_mode"],
        "stop_reason": r["stop_reason"], "action_type": r["action_type"], "latency_ms": r["latency_ms"],
        "invalid_action": int(r["invalid_action"] or 0), "stale_action": int(r["stale_action"] or 0),
        "stress": r["stress"], "error": r["error"],
    } for r in reversed(calls_rows)]
    ledger = db.fetchall("SELECT tick, balance_after FROM wallet_ledger WHERE agent_id=? ORDER BY tick DESC, entry_id DESC LIMIT ?",
                         (agent_id, BALANCE_HISTORY))
    balance_series = [{"tick": int(r["tick"]), "balance_usd": round(micro_to_usd(int(r["balance_after"])), 6)} for r in reversed(ledger)]
    children = [r["agent_id"] for r in db.fetchall("SELECT agent_id FROM agents WHERE parent_id=? ORDER BY born_tick, agent_id", (agent_id,))]
    try:
        self_summary = sim.memory.get_self(agent_id)
    except Exception:
        self_summary = None
    notes = [_note_summary(n) for n in sim.memory.list_notes(agent_id, include_archived=True)]
    return {"agent": _agent_record(rec), "self_summary": self_summary, "self_versions": sim.memory.self_history(agent_id),
            "notes": notes, "calls": calls, "balance_series": balance_series, "children": children}


@router.get("/agents/{agent_id}")
async def agent_detail(agent_id: str, request: Request) -> dict[str, Any]:
    detail = build_agent_detail(_sim(request), agent_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    return detail


@router.get("/agents/{agent_id}/notes/{note_id}")
async def agent_note(agent_id: str, note_id: str, request: Request) -> dict[str, Any]:
    sim = _sim(request)
    if sim.registry.get(agent_id) is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    note = sim.memory.get_note(note_id)
    if note is None or note.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="no such note for this agent")
    prov = note.provenance
    out = _note_summary(note)
    out.update({"agent_id": note.agent_id, "links": list(note.links), "path": note.path,
                "source_agent_id": prov.source_agent_id, "origin_note_id": prov.origin_note_id,
                "origin_generation": prov.origin_generation, "transfer_tick": prov.transfer_tick,
                "path_agents": list(prov.path_agents)})
    return out


# --- chronicle / metrics / events / snapshots ----------------------------------------------------------------
def _headline_from_markdown(markdown: str) -> str:
    for line in markdown.splitlines():
        s = line.strip()
        if s.startswith("**") and s.endswith("**") and len(s) > 4:
            return s[2:-2]
    return ""


@router.get("/chronicle")
async def chronicle(request: Request, day: int | None = Query(default=None, ge=0)) -> dict[str, Any]:
    sim = _sim(request)
    if sim.chronicle is None:
        raise HTTPException(status_code=404, detail="chronicle disabled")
    rev = int(sim.db.kv_get("chronicle_rev", 0) or 0)
    if day is None:
        doc = sim.chronicle.latest()
        if doc is None:
            raise HTTPException(status_code=404, detail="no chronicle yet")
        return {"day": int(doc.day), "headline": doc.headline, "markdown": doc.markdown, "rev": rev}
    markdown = sim.chronicle.read(day)
    if markdown is None:
        raise HTTPException(status_code=404, detail=f"no chronicle for day {day}")
    latest_day = sim.db.kv_get("chronicle_day")
    headline = str(sim.db.kv_get("chronicle_headline", "") or "") if latest_day == day else _headline_from_markdown(markdown)
    return {"day": day, "headline": headline, "markdown": markdown, "rev": rev}


@router.get("/metrics")
async def metrics(request: Request, since_tick: int = Query(default=0, ge=-1),
                  limit: int = Query(default=MAX_LIMIT, ge=1, le=MAX_LIMIT), days: bool = False) -> dict[str, Any]:
    db = _sim(request).db
    if days:
        rows = db.fetchall("SELECT tick, day, json FROM metrics WHERE tick < 0 ORDER BY day LIMIT ?", (limit,))
        return {"days": [{"type": "day", "day": int(r["day"]), "row": json.loads(r["json"])} for r in rows]}
    rows = db.fetchall("SELECT tick, day, json FROM metrics WHERE tick > 0 AND tick > ? ORDER BY tick LIMIT ?", (since_tick, limit))
    return {"metrics": [{"type": "metrics", "tick": int(r["tick"]), "day": int(r["day"]), "row": json.loads(r["json"])} for r in rows]}


@router.get("/events")
async def events(request: Request, since_seq: int = Query(default=0, ge=0),
                 limit: int = Query(default=500, ge=1, le=MAX_LIMIT)) -> dict[str, Any]:
    db = _sim(request).db
    rows = db.fetchall("SELECT seq, tick, day, kind, agent_id, payload, visibility FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                       (since_seq, limit))
    last = db.fetchone("SELECT COALESCE(MAX(seq), 0) AS s FROM events")
    out = [{"type": "event", "seq": int(r["seq"]), "tick": int(r["tick"]), "day": int(r["day"]), "kind": r["kind"],
            "agent_id": r["agent_id"], "visibility": r["visibility"], "payload": json.loads(r["payload"])} for r in rows]
    return {"events": out, "last_seq": int(last["s"]) if last else 0}


@router.get("/snapshots")
async def snapshots(request: Request, since_tick: int = Query(default=0, ge=-1),
                    limit: int = Query(default=60, ge=1, le=MAX_LIMIT)) -> dict[str, Any]:
    sim, hub = _sim(request), _hub(request)
    rows = sim.db.fetchall("SELECT tick, json FROM snapshots WHERE tick > ? ORDER BY tick LIMIT ?", (since_tick, limit))
    newest = int(sim.clock.tick)
    out = []
    for r in rows:
        snap = json.loads(r["json"])
        snap["type"] = "snapshot"
        snap["ts_ms"] = hub.snapshot_ts(int(r["tick"]), newest)
        out.append(snap)
    return {"snapshots": out}


# --- lineage / graveyard / boards --------------------------------------------------------------------------
def build_tree(sim: Any) -> dict[str, Any]:
    rows = sim.registry.family_tree()
    nodes: dict[str, dict[str, Any]] = {}
    for r in rows:
        nodes[str(r["agent_id"])] = {
            "id": r["agent_id"], "name": r["name"], "tier": r["model_tier"], "generation": r["generation"],
            "parent_id": r["parent_id"], "status": r["status"], "born_tick": r["born_tick"], "died_tick": r["died_tick"],
            "balance_usd": round(micro_to_usd(int(r["balance"] or 0)), 6), "children": [],
        }
    roots: list[dict[str, Any]] = []
    for r in rows:
        node = nodes[str(r["agent_id"])]
        parent = nodes.get(str(r["parent_id"])) if r["parent_id"] else None
        (parent["children"] if parent is not None else roots).append(node)
    return {"roots": roots, "count": len(rows)}


@router.get("/tree")
async def tree(request: Request) -> dict[str, Any]:
    return build_tree(_sim(request))


def build_graveyard(sim: Any) -> dict[str, Any]:
    db = sim.db
    counts = {r["parent_id"]: int(r["n"]) for r in db.fetchall(
        "SELECT parent_id, COUNT(*) AS n FROM agents WHERE parent_id IS NOT NULL GROUP BY parent_id")}
    causes: dict[str, str | None] = {}
    for r in db.fetchall("SELECT agent_id, payload FROM events WHERE kind='death' ORDER BY seq"):
        try:
            causes[str(r["agent_id"])] = json.loads(r["payload"]).get("cause")
        except ValueError:
            pass
    rows = db.fetchall("SELECT agent_id, name, model_tier, generation, parent_id, born_tick, died_tick, status, balance "
                       "FROM agents WHERE status='archived' ORDER BY died_tick, agent_id")
    return {"agents": [{
        "id": r["agent_id"], "name": r["name"], "tier": r["model_tier"], "generation": r["generation"],
        "parent_id": r["parent_id"], "born_tick": r["born_tick"], "died_tick": r["died_tick"], "status": r["status"],
        "balance_usd": round(micro_to_usd(int(r["balance"] or 0)), 6), "children": counts.get(r["agent_id"], 0),
        "cause": causes.get(str(r["agent_id"])),
    } for r in rows]}


@router.get("/graveyard")
async def graveyard(request: Request) -> dict[str, Any]:
    return build_graveyard(_sim(request))


@router.get("/gadgets")
async def gadgets(request: Request) -> dict[str, Any]:
    return _hub(request).gadgets_message()


@router.get("/tasks")
async def tasks(request: Request) -> dict[str, Any]:
    return _hub(request).tasks_message()


# --- operator POSTs ------------------------------------------------------------------------------------------
class PostTaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=600)
    reward_usd: float = Field(ge=0.0, le=1000.0)


class ArrivalBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    tier: str = Field(min_length=1)
    balance_usd: float = Field(default=1.0, ge=0.0, le=1000.0)


class EpochBody(BaseModel):
    day: int | None = Field(default=None, ge=0, description="Defaults to the current day (applied next tick)")
    kind: Literal["drought", "storm", "boom", "arrival", "custom"] = "custom"
    scarcity: float | None = Field(default=None, ge=0.0, le=10.0)
    weather_baseline: float | None = Field(default=None, ge=-1.0, le=1.0)
    duration_days: int = Field(default=1, ge=1, le=365)
    arrival: ArrivalBody | None = None

    @model_validator(mode="after")
    def _arrival_needs_block(self) -> EpochBody:
        if self.kind == "arrival" and self.arrival is None:
            raise ValueError("arrival epochs need an `arrival` block")
        return self


class BenefactorBody(BaseModel):
    agent_id: str | None = None
    amount_usd: float | None = Field(default=None, gt=0.0, le=100.0)
    enabled: bool | None = None

    @model_validator(mode="after")
    def _one_form(self) -> BenefactorBody:
        if self.enabled is None and self.amount_usd is None:
            raise ValueError("send {agent_id?, amount_usd} for a grant or {enabled} to toggle the benefactor")
        return self


class SpeedBody(BaseModel):
    tick_seconds: float = Field(ge=0.0, le=3600.0)


operator = [Depends(require_operator)]


@router.post("/tasks", dependencies=operator, status_code=202)
async def post_task(body: PostTaskBody, request: Request) -> dict[str, Any]:
    sim = _sim(request)
    _require_running(sim)
    cmd_id = sim.enqueue("post_task", {"title": body.title, "description": body.description, "reward_usd": body.reward_usd})
    return _ack(sim, cmd_id)


@router.post("/tasks/{task_id}/approve/{application_id}", dependencies=operator, status_code=202)
async def approve_task(task_id: str, application_id: str, request: Request) -> dict[str, Any]:
    sim = _sim(request)
    _require_running(sim)
    task = sim.db.fetchone("SELECT status FROM tasks WHERE task_id=?", (task_id,))
    app = sim.db.fetchone("SELECT status FROM task_applications WHERE application_id=? AND task_id=?", (application_id, task_id))
    if task is None or app is None:
        raise HTTPException(status_code=404, detail="unknown task or application")
    if task["status"] != "open" or app["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"task is {task['status']}, application is {app['status']}")
    return _ack(sim, sim.enqueue("approve_task", {"task_id": task_id, "application_id": application_id}))


@router.post("/tasks/{task_id}/complete", dependencies=operator, status_code=202)
async def complete_task(task_id: str, request: Request) -> dict[str, Any]:
    sim = _sim(request)
    _require_running(sim)
    task = sim.db.fetchone("SELECT status FROM tasks WHERE task_id=?", (task_id,))
    if task is None:
        raise HTTPException(status_code=404, detail="unknown task")
    if task["status"] != "assigned":
        raise HTTPException(status_code=409, detail=f"task is {task['status']}, not assigned")
    return _ack(sim, sim.enqueue("complete_task", {"task_id": task_id}))


@router.post("/control/epoch", dependencies=operator, status_code=202)
async def control_epoch(body: EpochBody, request: Request) -> dict[str, Any]:
    sim = _sim(request)
    _require_running(sim)
    if body.arrival is not None and body.arrival.tier not in sim.cfg.tiers:
        raise HTTPException(status_code=422, detail=f"unknown tier {body.arrival.tier!r}")
    payload = body.model_dump(exclude_none=True)
    payload.setdefault("day", int(sim.clock.day))
    return _ack(sim, sim.enqueue("epoch", payload))


@router.post("/control/benefactor", dependencies=operator, status_code=202)
async def control_benefactor(body: BenefactorBody, request: Request) -> dict[str, Any]:
    sim = _sim(request)
    _require_running(sim)
    if body.enabled is not None:
        return _ack(sim, sim.enqueue("benefactor", {"enabled": body.enabled}))
    if body.agent_id is not None and sim.registry.get(body.agent_id) is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    payload: dict[str, Any] = {"amount_usd": body.amount_usd}
    if body.agent_id is not None:
        payload["agent_id"] = body.agent_id
    return _ack(sim, sim.enqueue("benefactor", payload))


def _emit_control(sim: Any, kind: str, payload: dict[str, Any]) -> None:
    try:
        sim.bus.emit(Event(int(sim.clock.tick), int(sim.clock.day), kind, payload, visibility=OPERATOR))
    except Exception:  # the event log is a convenience here; never fail the control call over it
        pass


@router.post("/control/pause", dependencies=operator)
async def control_pause(request: Request) -> dict[str, Any]:
    sim, hub = _sim(request), _hub(request)
    _require_running(sim)
    if not sim.is_paused:
        sim.pause()
        _emit_control(sim, Kind.PAUSE, {})
        hub.broadcast_status()
    return hub.status_message()


@router.post("/control/resume", dependencies=operator)
async def control_resume(request: Request) -> dict[str, Any]:
    sim, hub = _sim(request), _hub(request)
    _require_running(sim)
    if sim.is_paused:
        sim.resume()
        _emit_control(sim, Kind.RESUME, {})
        hub.broadcast_status()
    return hub.status_message()


@router.post("/control/step", dependencies=operator)
async def control_step(request: Request) -> dict[str, Any]:
    sim = _sim(request)
    _require_running(sim)
    if not sim.is_paused:
        raise HTTPException(status_code=409, detail="step is only valid while paused")
    driver = request.app.state.driver
    if not await driver.wait_ready(timeout=30.0):
        raise HTTPException(status_code=503, detail="simulation is still starting")
    try:
        report = await driver.step()
    except RunEnded as e:
        raise HTTPException(status_code=409, detail=f"run is {e}") from None
    return {"tick": int(report.tick), "day": int(report.day), "status": report.status, "calls": report.calls,
            "deaths": list(report.deaths), "births": list(report.births)}


@router.post("/control/speed", dependencies=operator)
async def control_speed(body: SpeedBody, request: Request) -> dict[str, Any]:
    sim, hub = _sim(request), _hub(request)
    sim.tick_seconds = float(body.tick_seconds)
    hub.broadcast_status()
    return hub.status_message()
