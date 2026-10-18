"""Build a brain per tier from config.

Scripted brains are free to build and are built for every scripted tier (their state is persisted across a
resume). A provider brain imports its SDK, so it is built up front only for a tier the run can reach from its
config: a tier on the roster, a tier a configured arrival epoch brings in, and, when ``population.tier_mutation_prob``
is above zero, every tier a child can mutate onto from those (repeatedly). When an LLM writer is on
(``gossip.paraphrase: llm`` or ``chronicle.writer: llm``) every tier is built, since the writers choose among all
configured provider tiers. Any other configured tier is built on first lookup, so an arrival an operator schedules
at runtime onto a tier nobody started on still gets its brain; a purely scripted run never imports a provider SDK.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any

from void.agents.models import PersonalitySeed
from void.brain.base import Brain
from void.config import VoidConfig
from void.rng import RNG

__all__ = ["BrainTable", "build_brains", "reachable_tiers"]


class BrainTable(dict[str, Brain]):
    """The run's brains by tier name. A configured tier that was not built up front is built on first lookup
    (``table[tier]`` or ``table.get(tier)``); iteration and ``in`` see only the brains built so far."""

    def __init__(self, build: Callable[[str], Brain], tiers: Iterable[str]) -> None:
        super().__init__()
        self._build = build
        self._configured = frozenset(tiers)

    def __missing__(self, tier: str) -> Brain:
        if tier not in self._configured:
            raise KeyError(tier)
        brain = self._build(tier)
        self[tier] = brain
        return brain

    def get(self, tier: str, default: Any = None) -> Any:  # type: ignore[override]
        if tier in self or tier in self._configured:
            return self[tier]
        return default


def reachable_tiers(cfg: VoidConfig) -> set[str]:
    """Tiers whose agents can exist in this run as configured: the roster, configured arrival epochs and, when
    tier mutation is on, every tier reachable from those by mutation (a child of a mutated child mutates again)."""
    from void.agents.lifecycle import mutation_candidates

    seen = {a.tier for a in cfg.agents if a.tier is not None}
    seen |= {e.arrival.tier for e in cfg.epochs if e.kind == "arrival" and e.arrival is not None}
    seen &= set(cfg.tiers)
    if cfg.population.tier_mutation_prob > 0:
        frontier = sorted(seen)
        while frontier:
            nxt = [t for t in mutation_candidates(cfg, frontier.pop()) if t not in seen]
            seen.update(nxt)
            frontier.extend(nxt)
    return seen


def build_brains(cfg: VoidConfig, rng: RNG, seed_of: dict[str, PersonalitySeed], client: Any | None = None,
                 gemini_client: Any | None = None) -> dict[str, Brain]:
    """One brain per tier (see the module docstring for which are built up front); every provider brain shares one
    semaphore (``server.max_concurrent_calls`` is global)."""
    semaphore = asyncio.Semaphore(cfg.server.max_concurrent_calls)

    def build(name: str) -> Brain:
        tier = cfg.tiers[name]
        if tier.provider == "scripted":
            from void.brain.scripted import ScriptedBrain
            return ScriptedBrain(name, cfg, rng, seed_of)
        if tier.provider == "anthropic":
            from void.brain.anthropic_brain import AnthropicBrain
            return AnthropicBrain(name, cfg, client=client, semaphore=semaphore)
        if tier.provider == "gemini":
            from void.brain.gemini_brain import GeminiBrain
            return GeminiBrain(name, cfg, client=gemini_client, semaphore=semaphore)
        raise ValueError(f"unknown provider {tier.provider!r} for tier {name!r}")

    llm_writers = cfg.gossip.paraphrase == "llm" or cfg.chronicle.writer == "llm"
    needed = set(cfg.tiers) if llm_writers else reachable_tiers(cfg)
    brains = BrainTable(build, cfg.tiers)
    for name, tier in cfg.tiers.items():  # config order, as before
        if tier.provider == "scripted" or name in needed:
            brains[name] = build(name)
    return brains
