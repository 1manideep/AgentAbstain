"""Pydantic models for every server -> client message (DESIGN §15) and their JSON Schema export.

The models are the wire contract: ``void schema`` writes ``schemas/<name>.schema.json`` (committed; there is no
type generator, ``web/src/protocol.ts`` mirrors them by hand) and ``void mock-feed`` validates every line it writes
against :func:`parse_message`. Extra keys are ignored on validation (and therefore permitted by the exported
schemas), so the simulation may add fields without breaking older clients.

HTTP response bodies with a wire model (:data:`RESPONSE_MODELS`, so far ``GET /api/events/aggregates``) are exported
next to them as ``schemas/http/<name>.schema.json``; they are bodies, not messages, so they carry no ``type``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

__all__ = [
    "RunStatus", "AgentStatusStr", "Visibility",
    "TierPublic", "MemoryLayerPublic", "IntelligencePublic", "HelloConfig", "Hello",
    "RosterAgent", "Roster",
    "LastAction", "AgentState", "NodeState", "EpochInfo", "EpochsState", "Snapshot",
    "GadgetRender", "GadgetItem", "GadgetsMsg",
    "TaskApplication", "TaskItem", "TasksMsg",
    "ChronicleMsg", "EventMsg", "MetricsMsg", "DayMsg", "StatusMsg",
    "GATE_EVENTS_LIMIT", "EPOCH_EVENTS_LIMIT", "GenerationCount", "TierCollapse", "DegenerationAggregate", "ClaimTally", "GossipEdgeTally",
    "MemoryAgentTally", "DayCount", "ExamDayTally", "MemoryAggregate", "EventAggregates",
    "ServerMessage", "MESSAGE_MODELS", "SCHEMA_NAMES", "RESPONSE_MODELS", "RESPONSE_SCHEMA_DIR",
    "parse_message", "export_schemas",
]

# ``failed``: a tick raised; the state was re-synced with the last committed tick and no further tick runs (DESIGN §14).
RunStatus = Literal["running", "completed", "capped_total", "extinct", "inconsistent", "failed"]
AgentStatusStr = Literal["alive", "bankrupt", "archived"]
Visibility = Literal["public", "operator"]


# --- hello -----------------------------------------------------------------------------------------------
class TierPublic(BaseModel):
    color: str
    model: str
    provider: str
    collapse_temperature: float | None = Field(default=None, description="T_c, so the T_eff histogram can draw it from the start")
    max_temperature: float | None = None
    rank: int | None = Field(default=None, description="Rung on intelligence.ladder (0 = smartest); null off the ladder")


class MemoryLayerPublic(BaseModel):
    """Which parts of the evolving-memory layer are on (MEMORY_EVOLUTION §5), so memory-only views can hide."""

    enabled: bool
    policy: bool
    maintenance: bool
    exam: bool


class IntelligencePublic(BaseModel):
    ladder: list[str] = Field(default_factory=list, description="Tier names from smartest to dumbest; empty without a ladder")


class HelloConfig(BaseModel):
    """``VoidConfig.public_subset()``: what the renderer is told (no prices, no thresholds)."""

    model_config = ConfigDict(extra="allow")

    name: str
    seed: int
    days: int
    ticks_per_day: int
    tick_seconds: float
    render_delay_ticks: int
    world_size: float
    population_cap: int
    daily_cap_usd: float
    total_cap_usd: float
    degeneration_mode: Literal["induce", "observe", "both"] = "both"
    view_radius: float | None = Field(default=None, description="Fog radius (world.view_radius); null without fog")
    memory_layer: MemoryLayerPublic | None = None
    tiers: dict[str, TierPublic]
    intelligence: IntelligencePublic | None = None


class Hello(BaseModel):
    type: Literal["hello"] = "hello"
    run_id: str
    config: HelloConfig
    tick: int
    day: int
    last_seq: int = Field(description="Highest events.seq at connect time; catch up with GET /api/events?since_seq=")
    paused: bool
    tick_seconds: float
    server_ts_ms: float
    status: RunStatus


# --- roster ----------------------------------------------------------------------------------------------
class RosterAgent(BaseModel):
    id: str
    name: str
    tier: str
    generation: int
    parent_id: str | None = None
    born_tick: int
    died_tick: int | None = None
    status: AgentStatusStr
    traits: dict[str, float] = Field(default_factory=dict)  # the six personality traits, for behaviour in the renderer
    motto: str = ""


class Roster(BaseModel):
    type: Literal["roster"] = "roster"
    agents: list[RosterAgent]


# --- snapshot --------------------------------------------------------------------------------------------
class LastAction(BaseModel):
    type: str | None = None
    target: str | None = None
    ok: bool = True


class AgentState(BaseModel):
    id: str
    x: float
    y: float
    heading: float
    stress: float
    t_eff: float | None = Field(default=None, description="Effective temperature of this tick's call; null when the agent did not call")
    degenerate: bool = False
    asleep: bool = False
    status: AgentStatusStr
    balance_usd: float
    anim: str = "idle"
    last_action: LastAction | None = None
    effects: list[str] = Field(default_factory=list)


class NodeState(BaseModel):
    id: str
    x: float
    y: float
    stock: float
    capacity: float
    stock_delta: float = 0.0


class EpochInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: str = "custom"
    day: int
    duration_days: int = 1
    scarcity: float | None = None
    weather_baseline: float | None = None
    arrival: dict[str, Any] | None = None


class EpochsState(BaseModel):
    active: EpochInfo | None = None
    scheduled: list[EpochInfo] = Field(default_factory=list)


class Snapshot(BaseModel):
    type: Literal["snapshot"] = "snapshot"
    tick: int
    day: int
    tick_of_day: int
    ts_ms: float = Field(description="Wall-clock send time in ms; the renderer's virtual timeline is built from these")
    paused: bool = False
    status: RunStatus = "running"
    weather: float
    scarcity: float
    spend_today_usd: float
    spend_total_usd: float
    spawn_pool_usd: float
    house_usd: float = 0.0
    population: int
    gadgets_rev: int = 0
    tasks_rev: int = 0
    chronicle_rev: int = 0
    epochs: EpochsState = Field(default_factory=EpochsState)
    agents: list[AgentState]
    nodes: list[NodeState]


# --- gadgets ---------------------------------------------------------------------------------------------
class GadgetRender(BaseModel):
    model_config = ConfigDict(extra="allow")

    shape: str
    color: str
    scale: float = 1.0
    label: str = ""
    rotation_deg: float = 0.0
    height_offset: float = 0.0


class GadgetItem(BaseModel):
    id: str
    name: str
    owner: str
    x: float
    y: float
    render: GadgetRender
    uses: int = 0
    effect_kind: str | None = Field(default=None, description="One of world.effect_caps' kinds (e.g. forage_bonus)")
    effect_value: float | None = Field(default=None, description="The verified effect size, capped by world.effect_caps")
    purpose: str | None = None
    verified_without_tests: bool = False
    failures: int = 0
    created_tick: int | None = None


class GadgetsMsg(BaseModel):
    type: Literal["gadgets"] = "gadgets"
    rev: int
    items: list[GadgetItem]


# --- tasks -----------------------------------------------------------------------------------------------
class TaskApplication(BaseModel):
    id: str
    agent_id: str
    pitch: str
    fee_usd: float
    status: Literal["pending", "approved", "rejected"]
    tick: int


class TaskItem(BaseModel):
    id: str
    title: str
    description: str = ""
    reward_usd: float
    status: Literal["open", "assigned", "completed", "cancelled"]
    assigned_agent_id: str | None = None
    posted_tick: int
    completed_tick: int | None = None
    applications: list[TaskApplication] = Field(default_factory=list)


class TasksMsg(BaseModel):
    type: Literal["tasks"] = "tasks"
    rev: int
    items: list[TaskItem]


# --- chronicle, events, metrics, status ------------------------------------------------------------------
class ChronicleMsg(BaseModel):
    type: Literal["chronicle"] = "chronicle"
    rev: int
    day: int | None = Field(default=None, description="null until the first day has been written")
    headline: str = ""
    markdown: str = Field(default="", description="Rendered as plain text by the client; never converted to HTML")


class EventMsg(BaseModel):
    type: Literal["event"] = "event"
    seq: int
    tick: int
    day: int
    kind: str
    agent_id: str | None = None
    visibility: Visibility = "public"
    payload: dict[str, Any] = Field(default_factory=dict)


class MetricsMsg(BaseModel):
    type: Literal["metrics"] = "metrics"
    tick: int
    day: int
    row: dict[str, Any] = Field(description="One per-tick metrics row (DESIGN §17)")


class DayMsg(BaseModel):
    type: Literal["day"] = "day"
    day: int
    row: dict[str, Any] = Field(description="The daily aggregate row (DESIGN §17)")


class StatusMsg(BaseModel):
    type: Literal["status"] = "status"
    paused: bool
    tick_seconds: float
    status: RunStatus


# --- HTTP: GET /api/events/aggregates ----------------------------------------------------------------------
# The whole-run totals of the web's event-derived charts (void/server/api.py build_event_aggregates); a body, not a
# message, so it has no ``type``. Groups come in order of their first event, the order a live client meets them in.
#: Gate events in the aggregates: the web's proposals list keeps this many (MAX_GATE_EVENTS in web/src/state/series.ts).
GATE_EVENTS_LIMIT = 200
#: Epoch events in the aggregates (a run schedules a handful; operators add a few more).
EPOCH_EVENTS_LIMIT = 1000


class GenerationCount(BaseModel):
    generation: int | None = Field(description="payload.generation; null when it was not an integral number (total only)")
    count: int


class TierCollapse(BaseModel):
    tier: str
    t_c: float = Field(description="The newest finite payload.t_c of the tier's degeneration events")


class DegenerationAggregate(BaseModel):
    by_generation: list[GenerationCount] = Field(description="degeneration events per payload.generation")
    t_c_by_tier: list[TierCollapse] = Field(description="Tiers in order of their first degeneration event with a t_c")


class ClaimTally(BaseModel):
    agent_id: str | None = Field(description="payload.agent_id (the speaker); null when it was not a string")
    tier: str | None = Field(description="payload.tier; the client files the group under the roster's tier for the speaker")
    made: int
    falsy: int = Field(description="Claims whose payload.truthful is not truthy")


class GossipEdgeTally(BaseModel):
    speaker_id: str = Field(description="payload.speaker_id; '' when it was not a string")
    listener_id: str = Field(description="payload.listener_id; '' when it was not a string")
    count: int
    last_tick: int = Field(description="Tick of the edge's newest transfer")


class MemoryAgentTally(BaseModel):
    """One agent's maintenance, maintenance_skipped and exam_answered events (agent: payload.agent_id, else the event's)."""

    agent_id: str
    maintenance_real_micro: float = Field(description="Sum of maintenance payload.real_cost (micro-dollars)")
    maintenance_world_micro: float = Field(description="Sum of maintenance payload.world_cost (micro-dollars)")
    maintenance_world_self_micro: float = Field(description="World cost of the maintenance calls the agent paid itself")
    maintenance_calls: int
    maintenance_skipped: int
    asked: int = Field(description="exam_answered events without payload.failed (graded items)")
    correct: int
    abstained: int
    failed: int


class DayCount(BaseModel):
    day: int
    count: int


class ExamDayTally(BaseModel):
    day: int
    kind: str = Field(description="payload.kind (the question kind); 'unknown' when absent")
    asked: int
    correct: int
    abstained: int
    failed: int


class MemoryAggregate(BaseModel):
    agents: list[MemoryAgentTally]
    skipped_by_day: list[DayCount] = Field(description="maintenance_skipped events per day, with or without an agent")
    exams: list[ExamDayTally] = Field(description="exam_answered events per day and question kind")


class EventAggregates(BaseModel):
    """Whole-run totals of the event-derived charts over every event with ``seq <= as_of_seq``.

    A client seeds its aggregates with these and then counts only events with a larger seq, so the backfill and the
    live stream meet without overlap. Epoch events (few) and the newest gadget gate events (the proposals list) come
    as event messages, for the reducers that keep the events themselves.
    """

    as_of_seq: int = Field(description="Every event with seq <= as_of_seq is counted; nothing after it")
    degeneration: DegenerationAggregate
    claims: list[ClaimTally] = Field(description="Per speaker and payload tier")
    gossip: list[GossipEdgeTally] = Field(description="Per speaker -> listener edge")
    memory: MemoryAggregate
    epochs: list[EventMsg] = Field(description=f"The epoch events (the newest {EPOCH_EVENTS_LIMIT}), oldest first")
    gate: list[EventMsg] = Field(
        description=f"The newest {GATE_EVENTS_LIMIT} gadget_verified / gadget_rejected events, oldest first")


ServerMessage = Annotated[
    Hello | Roster | Snapshot | GadgetsMsg | TasksMsg | ChronicleMsg | EventMsg | MetricsMsg | DayMsg | StatusMsg,
    Field(discriminator="type"),
]

MESSAGE_MODELS: dict[str, type[BaseModel]] = {
    "hello": Hello, "roster": Roster, "snapshot": Snapshot, "gadgets": GadgetsMsg, "tasks": TasksMsg,
    "chronicle": ChronicleMsg, "event": EventMsg, "metrics": MetricsMsg, "day": DayMsg, "status": StatusMsg,
}

#: The schemas ``void schema`` exports (DESIGN §2: ``schemas/`` is committed and consumed by ``web/``).
SCHEMA_NAMES: tuple[str, ...] = ("hello", "snapshot", "event", "metrics", "day", "roster", "gadgets", "tasks", "chronicle",
                                 "status")

#: HTTP response bodies with a wire model, exported to ``<out>/http/<name>.schema.json`` (DESIGN §15).
RESPONSE_MODELS: dict[str, type[BaseModel]] = {"event_aggregates": EventAggregates}
RESPONSE_SCHEMA_DIR = "http"

_adapter: TypeAdapter[Any] = TypeAdapter(ServerMessage)


def parse_message(data: dict[str, Any]) -> BaseModel:
    """Validate one server -> client message by its ``type`` discriminator (raises ``ValidationError``)."""
    return _adapter.validate_python(data)


def _write_schema(model: type[BaseModel], out_dir: Path, rel: str) -> Path:
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://void.local/schemas/{rel}"
    schema.setdefault("title", model.__name__)
    path = out_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def export_schemas(out_dir: Path) -> list[Path]:
    """Write ``<name>.schema.json`` for every name in :data:`SCHEMA_NAMES` and ``http/<name>.schema.json`` for every
    response model in :data:`RESPONSE_MODELS`; returns the paths written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = [_write_schema(MESSAGE_MODELS[name], out_dir, f"{name}.schema.json") for name in SCHEMA_NAMES]
    written += [_write_schema(model, out_dir, f"{RESPONSE_SCHEMA_DIR}/{name}.schema.json")
                for name, model in RESPONSE_MODELS.items()]
    return written
