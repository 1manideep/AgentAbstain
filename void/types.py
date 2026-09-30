"""Shared value types used across subsystems. No behaviour lives here."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "AgentStatus", "Vec2", "NeighbourView", "NodeView", "GadgetView", "TaskView", "HeardMessage",
    "RetrievedNote", "Observation", "ActionOutcome", "STRESS_LABELS", "stress_label",
]


class AgentStatus(str, Enum):
    ALIVE = "alive"
    BANKRUPT = "bankrupt"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class Vec2:
    x: float
    y: float

    def dist(self, other: Vec2) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def towards(self, other: Vec2, max_step: float) -> Vec2:
        d = self.dist(other)
        if d <= max_step or d == 0.0:
            return other
        t = max_step / d
        return Vec2(self.x + (other.x - self.x) * t, self.y + (other.y - self.y) * t)


STRESS_LABELS = (("calm", 0.34), ("strained", 0.67), ("frantic", 1.01))


def stress_label(stress: float) -> str:
    for label, upper in STRESS_LABELS:
        if stress < upper:
            return label
    return "frantic"


@dataclass
class NeighbourView:
    agent_id: str
    name: str
    tier: str
    distance: float
    stress_bucket: str
    asleep: bool


@dataclass
class NodeView:
    node_id: str
    distance: float
    stock_bucket: str  # rich | thin | empty
    x: float
    y: float


@dataclass
class GadgetView:
    gadget_id: str
    name: str
    owner: str
    purpose: str
    distance: float


@dataclass
class TaskView:
    task_id: str
    title: str
    reward_usd: float
    status: str
    my_application_status: str | None


@dataclass
class HeardMessage:
    kind: str  # talk | windfall | transfer | gossip | system
    from_agent_id: str | None
    from_name: str | None
    text: str
    tick: int


@dataclass
class RetrievedNote:
    note_id: str
    title: str
    body: str
    score: float
    hop: int
    source_agent_id: str | None
    created_tick: int


@dataclass
class Observation:
    tick: int
    day: int
    tick_of_day: int
    ticks_left_today: int
    agent_id: str
    name: str
    tier: str
    generation: int
    position: Vec2
    balance_usd: float
    burn_rate_usd_per_tick: float
    stress: float
    stress_label: str
    weather: float
    weather_label: str
    scarcity_label: str
    neighbours: list[NeighbourView] = field(default_factory=list)
    nodes: list[NodeView] = field(default_factory=list)
    gadgets: list[GadgetView] = field(default_factory=list)
    tasks: list[TaskView] = field(default_factory=list)
    heard: list[HeardMessage] = field(default_factory=list)
    self_summary: str = ""
    memories: list[RetrievedNote] = field(default_factory=list)
    chronicle_headline: str | None = None
    last_action_result: str | None = None
    available_actions: list[str] = field(default_factory=list)
    world_size: float = 60.0
    population: int = 0
    population_cap: int = 0
    active_effects: dict[str, float] = field(default_factory=dict)


@dataclass
class ActionOutcome:
    ok: bool
    action_type: str
    reason: str = ""
    effects: dict[str, object] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return f"{self.action_type}: {'ok' if self.ok else 'failed'}{' - ' + self.reason if self.reason else ''}"
