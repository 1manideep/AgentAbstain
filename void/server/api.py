"""The HTTP API (DESIGN §15). All JSON. GETs are open except ``/api/exams``; every POST needs the operator token.

POSTs that map to operator commands only enqueue (DESIGN §14.1) and answer
``{cmd_id, will_apply_at_tick}``; the tick loop applies them in ``cmd_id`` order. ``pause``,
``resume``, ``speed`` and ``step`` act on loop-level flags immediately.

The memory-layer reads (MEMORY_EVOLUTION §5.10) serve the tables the layer writes: ``policy_versions``
(``/api/agents/{id}/policy``), ``memory_commands`` (``/api/agents/{id}/memory_commands``), ``practice_events``
(``/api/practices``), a per-agent roll-up (``/api/memory/summary``) and the exam items with their truths
(``/api/exams``, operator only: truths are operator visibility, like the ``exam_asked`` events). Agent-authored
text (policy texts, command arguments and messages, practice excerpts, exam answers) is returned as plain
strings; the client renders it as text, never as HTML. The open routes' shapes are mirrored in ``web/src/protocol.ts``
(``PolicyResponse``, ``MemoryCommandsResponse``, ``MemorySummaryResponse``, ``PracticesResponse``) and held to them
by tests/test_server_memory_api.py.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from void.config import micro_to_usd
from void.events import OPERATOR, Event, Kind
from void.memory.vault_stats import agent_vault_stats
from void.server.auth import is_same_origin_fetch, require_operator
from void.server.driver import RunEnded

__all__ = ["router", "MAX_LIMIT", "MEMORY_COMMANDS_LIMIT", "build_memory_summary"]

log = logging.getLogger("void.server.api")

router = APIRouter(prefix="/api")
MAX_LIMIT = 5000
MEMORY_COMMANDS_LIMIT = 500
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
    """The latest snapshot with the run's current status: the last snapshot is published before the end-of-run
    check (and a failed tick publishes none), so its own ``status`` still says ``running`` once the run is over."""
    snap = _hub(request).latest_snapshot()
    if snap is None:
        raise HTTPException(status_code=404, detail="no snapshot yet")
    snap["status"] = _sim(request).status
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


# --- memory layer (MEMORY_EVOLUTION §5.10) ---------------------------------------------------------------------
def _known_agent(sim: Any, agent_id: str) -> None:
    if sim.registry.get(agent_id) is None:
        raise HTTPException(status_code=404, detail="unknown agent")


def _usd(micro: Any) -> float:
    return round(micro_to_usd(int(micro or 0)), 6)


def _json_or_raw(raw: Any) -> Any:
    """A JSON column decoded, or the stored text as is when it does not parse (None stays None)."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return str(raw)


@router.get("/agents/{agent_id}/policy")
async def agent_policy(agent_id: str, request: Request) -> dict[str, Any]:
    """Every ``policy_versions`` row of the agent, newest first (empty when the policy layer is off)."""
    sim = _sim(request)
    _known_agent(sim, agent_id)
    versions = [{"version": int(v.version), "tick": int(v.tick), "source": v.source, "source_agent_id": v.source_agent_id,
                 "parent_version": v.parent_version, "similarity_to_parent": v.similarity_to_parent, "text": v.text}
                for v in reversed(sim.policy.history(agent_id))]
    return {"agent_id": agent_id, "versions": versions}


@router.get("/agents/{agent_id}/memory_commands")
async def agent_memory_commands(agent_id: str, request: Request,
                                limit: int = Query(default=50, ge=1, le=MEMORY_COMMANDS_LIMIT)) -> dict[str, Any]:
    """The agent's last ``limit`` memory-command audit rows, newest first; ``args`` is the stored JSON text."""
    sim = _sim(request)
    _known_agent(sim, agent_id)
    rows = sim.db.fetchall("SELECT cmd_id, tick, purpose, command, args, ok, message, bytes_delta FROM memory_commands "
                           "WHERE agent_id=? ORDER BY tick DESC, rowid DESC LIMIT ?", (agent_id, limit))
    return {"commands": [{
        "cmd_id": r["cmd_id"], "tick": int(r["tick"]), "purpose": r["purpose"], "command": r["command"],
        "args": str(r["args"]), "ok": bool(r["ok"]), "message": r["message"], "bytes_delta": int(r["bytes_delta"] or 0),
    } for r in rows]}


def build_memory_summary(sim: Any) -> dict[str, Any]:
    """The /api/memory/summary payload: one row per agent (living or not), in ``agent_id`` order.

    * ``notes``, ``bytes``: :func:`void.memory.vault_stats.agent_vault_stats` (live notes, entity stubs left out).
      ``links`` is that module's resolved link count, recovered from ``link_density`` (rounded to four places, so
      ``density * notes`` rounds back to the exact count for any vault under 10,000 notes).
    * ``policy_source``, ``policy_version``: the current ``policy_versions`` row (None without a policy).
    * ``maintenance_*_usd``: the agent's ``llm_calls`` with purpose ``maintain``; ``decide_real_usd``: purpose
      ``decide``. Exam calls (purpose ``exam``) are paid by the research pool and not counted here.
    * ``exam_*``: the graded audit-exam answers, as :func:`void.research.analysis.exam_counts` counts them: a paid
      call that returned no usable answer (``call_id`` set, ``answer`` NULL) measured nothing and is left out.
    """
    db = sim.db
    agents = db.fetchall("SELECT agent_id, name, status FROM agents ORDER BY agent_id")
    ids = [str(r["agent_id"]) for r in agents]
    vault = agent_vault_stats(db, ids, sim.run_dir) if ids else {}
    costs: dict[tuple[str, str], tuple[int, int]] = {
        (str(r["agent_id"]), str(r["purpose"])): (int(r["r"]), int(r["w"])) for r in db.fetchall(
            "SELECT agent_id, purpose, COALESCE(SUM(real_cost), 0) AS r, COALESCE(SUM(world_cost), 0) AS w FROM llm_calls "
            "WHERE purpose IN ('maintain', 'decide') GROUP BY agent_id, purpose")}
    graded = {str(r["agent_id"]): (int(r["n"]), int(r["c"]), int(r["ab"])) for r in db.fetchall(
        "SELECT i.agent_id AS agent_id, COUNT(*) AS n, COALESCE(SUM(a.correct), 0) AS c, COALESCE(SUM(a.abstained), 0) AS ab "
        "FROM exam_answers a JOIN exam_items i ON i.item_id = a.item_id "
        "WHERE i.exam='audit' AND NOT (a.call_id IS NOT NULL AND a.answer IS NULL) GROUP BY i.agent_id")}
    out: list[dict[str, Any]] = []
    for r in agents:
        aid = str(r["agent_id"])
        v = vault.get(aid, {})
        notes = int(v.get("notes") or 0)
        density = v.get("link_density")
        policy = sim.policy.current(aid)
        m_real, m_world = costs.get((aid, "maintain"), (0, 0))
        asked, correct, abstained = graded.get(aid, (0, 0, 0))
        out.append({
            "agent_id": aid, "name": r["name"], "alive": r["status"] == "alive",
            "notes": notes, "links": int(round(float(density) * notes)) if density is not None else 0,
            "bytes": int(v.get("bytes") or 0),
            "policy_source": policy.source if policy is not None else None,
            "policy_version": int(policy.version) if policy is not None else None,
            "maintenance_real_usd": _usd(m_real), "maintenance_world_usd": _usd(m_world),
            "decide_real_usd": _usd(costs.get((aid, "decide"), (0, 0))[0]),
            "exam_asked": asked, "exam_correct": correct, "exam_abstained": abstained,
        })
    return {"agents": out}


@router.get("/memory/summary")
async def memory_summary(request: Request) -> dict[str, Any]:
    return build_memory_summary(_sim(request))


@router.get("/practices")
async def practices(request: Request, limit: int = Query(default=500, ge=1, le=MAX_LIMIT)) -> dict[str, Any]:
    """The last ``limit`` ``practice_events`` rows, newest first (empty until practice transmission writes any)."""
    rows = _sim(request).db.fetchall(
        "SELECT event_id, tick, kind, source_agent_id, target_agent_id, excerpt, similarity FROM practice_events "
        "ORDER BY tick DESC, rowid DESC LIMIT ?", (limit,))
    return {"events": [{
        "event_id": r["event_id"], "tick": int(r["tick"]), "kind": r["kind"], "source_agent_id": r["source_agent_id"],
        "target_agent_id": r["target_agent_id"], "excerpt": str(r["excerpt"]),
        "similarity": None if r["similarity"] is None else float(r["similarity"]),
    } for r in rows]}


operator = [Depends(require_operator)]


@router.get("/exams", dependencies=operator)
async def exams(request: Request, agent_id: str | None = None,
                limit: int = Query(default=500, ge=1, le=MAX_LIMIT)) -> dict[str, Any]:
    """Exam items with their truths and answers, newest first (operator only: the truths are operator visibility).

    ``answered`` is False while an item has no answer row; ``failed`` marks a paid call that returned no usable
    answer (not graded, see :func:`build_memory_summary`). ``options`` and ``retrieved`` are decoded JSON lists.
    """
    sim = _sim(request)
    sql = ("SELECT i.item_id, i.exam, i.agent_id, i.tick_asked, i.tick_about, i.kind, i.question, i.truth, i.options, "
           "i.source_ref, a.item_id AS answered, a.call_id, a.answer, a.correct, a.abstained, a.retrieved "
           "FROM exam_items i LEFT JOIN exam_answers a ON a.item_id = i.item_id")
    params: tuple[Any, ...] = ()
    if agent_id is not None:
        _known_agent(sim, agent_id)
        sql += " WHERE i.agent_id=?"
        params = (agent_id,)
    rows = sim.db.fetchall(sql + " ORDER BY i.tick_asked DESC, i.rowid DESC LIMIT ?", (*params, limit))
    items = []
    for r in rows:
        answered = r["answered"] is not None
        items.append({
            "item_id": r["item_id"], "exam": r["exam"], "agent_id": r["agent_id"], "tick_asked": int(r["tick_asked"]),
            "tick_about": int(r["tick_about"]), "kind": r["kind"], "question": str(r["question"]), "truth": str(r["truth"]),
            "options": _json_or_raw(r["options"]), "source_ref": str(r["source_ref"]),
            "answered": answered, "call_id": r["call_id"], "answer": r["answer"],
            "correct": bool(r["correct"]) if answered else None, "abstained": bool(r["abstained"]) if answered else None,
            "failed": answered and r["call_id"] is not None and r["answer"] is None,
            "retrieved": _json_or_raw(r["retrieved"]),
        })
    return {"items": items}


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
    sim, hub = _sim(request), _hub(request)
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
    except Exception as e:
        if sim.status == "running":
            raise
        # the tick raised and marked the run failed (DESIGN §14): tell every socket, as the loop's own end does
        log.exception("step: tick failed")
        try:
            await hub.on_run_end()
        except Exception:
            log.exception("hub.on_run_end failed")
        raise HTTPException(status_code=500, detail=f"tick failed: {type(e).__name__}; run is {sim.status}") from None
    return {"tick": int(report.tick), "day": int(report.day), "status": report.status, "calls": report.calls,
            "deaths": list(report.deaths), "births": list(report.births)}


@router.post("/control/speed", dependencies=operator)
async def control_speed(body: SpeedBody, request: Request) -> dict[str, Any]:
    sim, hub = _sim(request), _hub(request)
    sim.tick_seconds = float(body.tick_seconds)
    hub.broadcast_status()
    return hub.status_message()
