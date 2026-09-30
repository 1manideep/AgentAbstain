"""Brain protocol: the only contract between the simulation and a model provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from void.brain.decision import Decision
from void.types import Observation

__all__ = ["Usage", "Sampling", "BrainResult", "Brain"]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    attempts: list[tuple[str, Usage]] = field(default_factory=list)  # (served model, usage) per billed attempt

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class Sampling:
    """What the entropy model decided for this agent this tick."""

    effective_temperature: float
    stress: float
    degenerate: bool
    api_temperature: float | None = None  # what was actually sent to a provider, if anything


@dataclass
class BrainResult:
    decision: Decision | None
    usage: Usage
    latency_ms: int
    stop_reason: str
    raw_text: str
    request_id: str | None = None
    error: str | None = None
    model: str = ""
    extras: dict[str, float] = field(default_factory=dict)  # scripted tiers: action_regret, p_chosen

    @property
    def ok(self) -> bool:
        return self.decision is not None and self.error is None


class Brain(Protocol):
    tier: str

    async def decide(self, obs: Observation, sampling: Sampling) -> BrainResult: ...
