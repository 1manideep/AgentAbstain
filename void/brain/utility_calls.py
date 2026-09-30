"""Metered, gated utility calls: LLM gossip paraphrase and LLM chronicle writing (DESIGN §11).

Both go through ``Wallet.charged_call`` so they reserve, are metered from real usage and count
toward the caps. A gated-out call returns ``None`` and the caller falls back to the template.
Any provider brain that sets ``is_llm = True`` and implements ``complete_text`` can serve them;
scripted tiers never trigger a paid call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from void.brain.base import Brain, BrainResult
from void.brain.prompt import estimate_tokens, fence

if TYPE_CHECKING:
    from void.sim.loop import Simulation

__all__ = ["complete_text", "make_paraphraser", "make_chronicle_writer", "is_llm", "chronicle_tier"]


def is_llm(brain: Brain | None) -> bool:
    return bool(getattr(brain, "is_llm", False)) and hasattr(brain, "complete_text")


async def complete_text(brain: Any, prompt: str, *, max_tokens: int = 400) -> BrainResult:
    """One plain text completion on the brain's model; never raises."""
    return await brain.complete_text(prompt, max_tokens=max_tokens)


def _record(sim: Simulation, wallet_id: str, tier_name: str, purpose: str, cc: Any, tick: int, ref: str) -> None:
    r, m = cc.result, cc.meter
    if r is None or m is None:
        return
    tier = sim.cfg.tiers[tier_name]
    sim.db.execute(
        "INSERT INTO llm_calls(call_id, tick, agent_id, purpose, tier, model, provider, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, real_cost, world_cost, hold, estimated, latency_ms, stop_reason, request_id, error, raw_text) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ref, tick, wallet_id, purpose, tier_name, r.model or tier.model, tier.provider, r.usage.input_tokens, r.usage.output_tokens,
         r.usage.cache_read_tokens, r.usage.cache_write_tokens, m.real_cost, m.world_cost, cc.hold, int(m.estimated), r.latency_ms,
         r.stop_reason, r.request_id, r.error, (r.raw_text or "")[:4000]),
    )


def make_paraphraser(sim: Simulation) -> Callable[[str, str, int], Awaitable[str | None]] | None:
    """Listener-paid paraphrase on the listener's own tier; None when no provider tier exists."""
    if not any(is_llm(b) for b in sim.brains.values()):
        return None

    async def paraphrase(listener_id: str, text: str, tick: int) -> str | None:
        rec = sim.registry.get(listener_id)
        if rec is None:
            return None
        brain = sim.brains.get(rec.model_tier)
        if not is_llm(brain):
            return None
        prompt = ("Restate the following remembered note in one sentence, keeping every [[wikilink]] exactly as written, "
                  "as if retelling it from memory. Reply with the sentence only.\n\n" + fence(text))
        tier = sim.cfg.tiers[rec.model_tier]
        hold = sim.wallet.hold_for(tier, estimate_tokens(prompt))
        ref = sim.ids.new("cl")
        cc = await sim.wallet.charged_call(listener_id, tier, hold, tick, ref, lambda: complete_text(brain, prompt, max_tokens=200), purpose="gossip")
        _record(sim, listener_id, rec.model_tier, "gossip", cc, tick, ref)
        if cc.result is None or cc.result.error or not cc.result.raw_text.strip():
            return None
        return cc.result.raw_text.strip()[:600]

    return paraphrase


def chronicle_tier(sim: Simulation) -> str | None:
    """The provider tier that writes the chronicle: the first one the roster uses (its credentials are
    known to work), else the first provider tier configured at all."""
    rostered = list(dict.fromkeys(a.tier for a in sim.cfg.agents if a.tier))
    for name in rostered + [n for n in sim.brains if n not in rostered]:
        if is_llm(sim.brains.get(name)):
            return name
    return None


def make_chronicle_writer(sim: Simulation) -> Callable[[str], Awaitable[str | None]] | None:
    """Chronicle rewrite paid by the kernel's chronicle wallet on the roster's first provider tier."""
    tier_name = chronicle_tier(sim)
    if tier_name is None:
        return None
    brain = sim.brains[tier_name]

    async def write(markdown: str) -> str | None:
        prompt = ("You are the town crier of a small closed world. Rewrite the following factual bulletin as a short newspaper "
                  "column in plain prose. Keep every name and number exactly; add nothing that is not in the bulletin; "
                  "keep the first line as the headline.\n\n" + fence(markdown))
        tier = sim.cfg.tiers[tier_name]
        hold = sim.wallet.hold_for(tier, estimate_tokens(prompt))
        ref = sim.ids.new("cl")
        cc = await sim.wallet.charged_call("chronicle", tier, hold, sim.clock.tick, ref, lambda: complete_text(brain, prompt, max_tokens=700), purpose="chronicle")
        _record(sim, "chronicle", tier_name, "chronicle", cc, sim.clock.tick, ref)
        if cc.result is None or cc.result.error or not cc.result.raw_text.strip():
            return None
        return cc.result.raw_text.strip()

    return write
