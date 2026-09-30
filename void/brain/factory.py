"""Build a brain per tier from config."""

from __future__ import annotations

import asyncio
from typing import Any

from void.agents.models import PersonalitySeed
from void.brain.base import Brain
from void.config import VoidConfig
from void.rng import RNG

__all__ = ["build_brains"]


def build_brains(cfg: VoidConfig, rng: RNG, seed_of: dict[str, PersonalitySeed], client: Any | None = None,
                 gemini_client: Any | None = None) -> dict[str, Brain]:
    """One brain per tier; every provider brain shares one semaphore (``server.max_concurrent_calls`` is global)."""
    brains: dict[str, Brain] = {}
    semaphore = asyncio.Semaphore(cfg.server.max_concurrent_calls)
    for name, tier in cfg.tiers.items():
        if tier.provider == "scripted":
            from void.brain.scripted import ScriptedBrain
            brains[name] = ScriptedBrain(name, cfg, rng, seed_of)
        elif tier.provider == "anthropic":
            from void.brain.anthropic_brain import AnthropicBrain
            brains[name] = AnthropicBrain(name, cfg, client=client, semaphore=semaphore)
        elif tier.provider == "gemini":
            from void.brain.gemini_brain import GeminiBrain
            brains[name] = GeminiBrain(name, cfg, client=gemini_client, semaphore=semaphore)
        else:
            raise ValueError(f"unknown provider {tier.provider!r} for tier {name!r}")
    return brains
