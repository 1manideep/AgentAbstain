"""Agent records and personality seeds (the heritable part of an agent's strategy)."""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field

from void.types import AgentStatus, Vec2

__all__ = ["PersonalitySeed", "AgentRecord", "TRAITS", "MOTTOS"]

TRAITS = ("greed", "sociability", "curiosity", "caution", "industriousness", "honesty")
MOTTOS = (
    "Keep moving.", "Trust the north node.", "Share what you find.", "Spend only when it earns.",
    "Talk first, dig second.", "Nobody remembers the quiet ones.", "The weather is a rumour.",
    "A full purse sleeps early.", "Build something that outlives you.", "Watch who gets lucky.",
)


@dataclass
class PersonalitySeed:
    greed: float = 0.5
    sociability: float = 0.5
    curiosity: float = 0.5
    caution: float = 0.5
    industriousness: float = 0.5
    honesty: float = 0.8
    motto: str = "Keep moving."

    @classmethod
    def generate(cls, rng: random.Random, overrides: dict[str, float] | None = None) -> PersonalitySeed:
        vals = {t: round(rng.random(), 3) for t in TRAITS}
        if overrides:
            for k, v in overrides.items():
                if k in TRAITS:
                    vals[k] = max(0.0, min(1.0, float(v)))
        return cls(**vals, motto=rng.choice(MOTTOS))

    def mutate(self, rng: random.Random, sigma: float = 0.1, motto_from: str | None = None) -> PersonalitySeed:
        vals = {t: round(max(0.0, min(1.0, getattr(self, t) + rng.gauss(0.0, sigma))), 3) for t in TRAITS}
        motto = self.motto
        if motto_from:
            first = motto_from.strip().split(".")[0].strip()
            if first:
                motto = (first[:60] + ".") if not first.endswith(".") else first[:61]
        elif rng.random() < 0.15:
            motto = rng.choice(MOTTOS)
        return PersonalitySeed(**vals, motto=motto)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> PersonalitySeed:
        d = json.loads(s)
        return cls(**{k: d.get(k, 0.5) for k in TRAITS}, motto=d.get("motto", "Keep moving."))

    def describe(self) -> str:
        def lvl(v: float) -> str:
            return "low" if v < 0.34 else ("moderate" if v < 0.67 else "high")
        return ", ".join(f"{t} {lvl(getattr(self, t))}" for t in TRAITS) + f'. Motto: "{self.motto}"'


@dataclass
class AgentRecord:
    agent_id: str
    name: str
    model_tier: str
    balance: int
    seed: PersonalitySeed
    memory_path: str
    generation: int
    status: AgentStatus
    pos: Vec2
    heading: float
    entropy_budget: float
    stress: float
    asleep: bool
    born_tick: int
    parent_id: str | None = None
    last_forage_tick: int | None = None
    last_action: str | None = None
    last_action_ok: bool = True
    weather_nudge_used: float = 0.0
    died_tick: int | None = None
    effects: dict[str, float] = field(default_factory=dict)

    @property
    def alive(self) -> bool:
        return self.status == AgentStatus.ALIVE
