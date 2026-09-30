"""Build a brain per tier from config."""

from __future__ import annotations

import asyncio
from typing import Any

from void.agents.models import PersonalitySeed
from void.brain.base import Brain
from void.config import VoidConfig
from void.rng import RNG

__all__ = ["build_brains"]


def build_brains(cfg: VoidConfig, rng: RNG, seed_of: dict[str, PersonalitySeed], client: Any | None = None) -> dict[str, Brain]:
    brains: dict[str, Brain] = {}
    semaphore: asyncio.Semaphore | None = None
    for name, tier in cfg.tiers.items():
        if tier.provider == "scripted":
            from void.brain.scripted import ScriptedBrain
            brains[name] = ScriptedBrain(name, cfg, rng, seed_of)
        elif tier.provider == "anthropic":
            from void.brain.anthropic_brain import AnthropicBrain
            if semaphore is None:
                semaphore = asyncio.Semaphore(cfg.server.max_concurrent_calls)
            brains[name] = AnthropicBrain(name, cfg, client=client, semaphore=semaphore)
        else:
            raise ValueError(f"unknown provider {tier.provider!r} for tier {name!r}")
    return brains
