"""Pydantic models for every server -> client message (DESIGN §15) and their JSON Schema export.

The models are the wire contract: ``void schema`` writes ``schemas/<name>.schema.json`` for the
frontend's ``npm run gen:types`` and ``void mock-feed`` validates every line it writes against
:func:`parse_message`. Extra keys are ignored on validation (and therefore permitted by the exported
schemas), so the simulation may add fields without breaking older clients.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

__all__ = [
    "RunStatus", "AgentStatusStr", "Visibility",
    "TierPublic", "HelloConfig", "Hello",
    "RosterAgent", "Roster",
    "LastAction", "AgentState", "NodeState", "EpochInfo", "EpochsState", "Snapshot",
    "GadgetRender", "GadgetItem", "GadgetsMsg",
    "TaskApplication", "TaskItem", "TasksMsg",
    "ChronicleMsg", "EventMsg", "MetricsMsg", "DayMsg", "StatusMsg",
    "ServerMessage", "MESSAGE_MODELS", "SCHEMA_NAMES", "parse_message", "export_schemas",
]

RunStatus = Literal["running", "completed", "capped_total", "extinct", "inconsistent"]
AgentStatusStr = Literal["alive", "bankrupt", "archived"]
Visibility = Literal["public", "operator"]


# --- hello -----------------------------------------------------------------------------------------------
class TierPublic(BaseModel):
    color: str
    model: str
    provider: str


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
    tiers: dict[str, TierPublic]


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


ServerMessage = Annotated[
    Hello | Roster | Snapshot | GadgetsMsg | TasksMsg | ChronicleMsg | EventMsg | MetricsMsg | DayMsg | StatusMsg,
    Field(discriminator="type"),
]

MESSAGE_MODELS: dict[str, type[BaseModel]] = {
    "hello": Hello, "roster": Roster, "snapshot": Snapshot, "gadgets": GadgetsMsg, "tasks": TasksMsg,
    "chronicle": ChronicleMsg, "event": EventMsg, "metrics": MetricsMsg, "day": DayMsg, "status": StatusMsg,
}

#: The schemas ``void schema`` exports (DESIGN §2: ``schemas/`` is committed and consumed by ``web/``).
SCHEMA_NAMES: tuple[str, ...] = ("hello", "snapshot", "event", "metrics", "roster", "gadgets", "tasks", "chronicle", "status")

_adapter: TypeAdapter[Any] = TypeAdapter(ServerMessage)


def parse_message(data: dict[str, Any]) -> BaseModel:
    """Validate one server -> client message by its ``type`` discriminator (raises ``ValidationError``)."""
    return _adapter.validate_python(data)


def export_schemas(out_dir: Path) -> list[Path]:
    """Write ``<name>.schema.json`` for every name in :data:`SCHEMA_NAMES`; returns the paths written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in SCHEMA_NAMES:
        model = MESSAGE_MODELS[name]
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://void.local/schemas/{name}.schema.json"
        schema.setdefault("title", model.__name__)
        path = out_dir / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written
