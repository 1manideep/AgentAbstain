"""Turn metered token usage into micro-dollars. Always after the call, never estimated.

Two prices exist (DESIGN §0, §9.2): the *real* cost charged by the provider, which is what
the daily and total caps protect, and the *world* cost debited from the agent's wallet,
which equals the real cost under ``world_pricing: real`` and a single configured price
under ``equalized`` (so model tier is not confounded with economic stress).

Prices are decimal; the arithmetic uses exact fractions so a call is never over- or
under-charged by a micro-dollar of floating-point noise. When a refusal fallback served the
response on another model, each billed attempt is priced at that model's own rates.
"""

from __future__ import annotations

import logging
import math
from fractions import Fraction

from void.brain.base import Usage
from void.config import MICRO, EconomyConfig, TierConfig
from void.economy.catalog import MODEL_PRICES, fallback_prices, same_model

__all__ = ["cost_micro", "real_cost_micro", "world_cost_micro", "hold_micro", "cap_hold_micro", "hold_parts",
           "prices_for_model", "MODEL_PRICES", "HOLD_PROMPT_FACTOR"]

log = logging.getLogger(__name__)


def _cost(usage: Usage, p_in: float, p_out: float, p_cr: float, p_cw: float) -> int:
    """USD per MTok equals micro-dollars per token, so the exact sum is already in micro-dollars."""
    total = (usage.input_tokens * Fraction(str(p_in)) + usage.output_tokens * Fraction(str(p_out))
             + usage.cache_read_tokens * Fraction(str(p_cr)) + usage.cache_write_tokens * Fraction(str(p_cw)))
    return int(math.ceil(total))


def _tier_prices(t: TierConfig) -> tuple[float, float, float, float]:
    return (t.price_in_per_mtok, t.price_out_per_mtok, t.price_cache_read_per_mtok, t.price_cache_write_per_mtok)


def prices_for_model(model: str, tier: TierConfig, tiers: dict[str, TierConfig] | None = None) -> tuple[float, float, float, float]:
    """Prices for the model that actually served a response: the tier's own, another configured tier's, or the table.

    A configured tier matches only its own model or a dated / numbered snapshot of it (``-001``, ``-20251001``):
    a served id that merely starts with the tier's model may be a different, newer model with other prices.
    """
    if not model or same_model(tier.model, model):
        return _tier_prices(tier)
    for t in (tiers or {}).values():
        if same_model(t.model, model):
            return _tier_prices(t)
    # the table: the LONGEST known prefix wins, so a short id never captures a longer one that is also listed
    best: tuple[int, tuple[float, float, float, float]] | None = None
    for known, prices in MODEL_PRICES.items():
        if model == known or model.startswith(known):
            cand = (len(known), prices)
            best = cand if best is None or cand[0] > best[0] else best
    if best is not None:
        return best[1]
    log.warning("unknown served model %s priced at tier %s rates", model, tier.model)
    return _tier_prices(tier)


def real_cost_micro(usage: Usage, tier: TierConfig, tiers: dict[str, TierConfig] | None = None) -> int:
    if usage.attempts:
        return sum(_cost(u, *prices_for_model(model, tier, tiers)) for model, u in usage.attempts)
    return _cost(usage, tier.price_in_per_mtok, tier.price_out_per_mtok, tier.price_cache_read_per_mtok, tier.price_cache_write_per_mtok)


cost_micro = real_cost_micro


def world_cost_micro(usage: Usage, tier: TierConfig, economy: EconomyConfig, tiers: dict[str, TierConfig] | None = None) -> int:
    if economy.world_pricing == "equalized":
        return _cost(usage, economy.equalized_price_in_per_mtok, economy.equalized_price_out_per_mtok,
                     economy.equalized_price_cache_read_per_mtok, economy.equalized_price_cache_write_per_mtok)
    return real_cost_micro(usage, tier, tiers)


# Holds are estimates made before the call from chars/4 token counts; the newer tokenizers count the same text
# as up to about 30% more tokens, so every estimated input count is scaled up by this factor.
HOLD_PROMPT_FACTOR = 1.3


def _dearest(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (max(a[0], b[0]), max(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def hold_parts(tier: TierConfig, economy: EconomyConfig, prompt_tokens: int, system_tokens: int = 0, *,
               schema_tokens: int = 0) -> tuple[int, int]:
    """``(hold, cap_hold)`` for one call, from its worst case: the whole prompt as fresh input, max_tokens out
    and the system prompt as a cache write.

    ``hold`` is what the payer must hold: the worst *world* cost of the call (what its wallet is debited), never
    below ``economy.min_call_reserve_usd``, so a call that stays within max_tokens can never overrun a balance.
    ``cap_hold`` is what the call reserves against the real-dollar caps: its worst *real* cost. The two are the
    same under ``world_pricing: real``; under ``equalized`` they differ, and keeping them apart means a tier's
    real price never decides when its agents go bankrupt, and an equalized world price (which is not real
    money) never eats the caps' headroom. Scripted tiers keep the single combined hold of the original design,
    ``max(real, world, floor)`` for both, so scripted runs gate, starve and end exactly as they always did.

    For a provider tier the system prompt and the structured-output schema (``schema_tokens``, sent as input by
    the API) are scaled like the prompt. With refusal fallbacks a declined attempt and its fallback attempt are
    both billed, the fallback at its own model's rates, and a request the provider routes straight to the
    fallback model is billed at those rates alone: such a tier's worst case is two whole attempts at the
    dearer of its own and its fallbacks' rate cards.
    """
    provider = tier.provider != "scripted"
    prompt_tokens = int(math.ceil((prompt_tokens + (schema_tokens if provider else 0)) * HOLD_PROMPT_FACTOR))
    if provider:
        system_tokens = int(math.ceil(system_tokens * HOLD_PROMPT_FACTOR))
    out_tokens = tier.max_tokens + max(0, int(tier.thinking_budget or 0))  # thinking tokens are billed as output
    u = Usage(input_tokens=prompt_tokens + system_tokens, output_tokens=out_tokens, cache_write_tokens=system_tokens)
    mult = max(0.0, economy.hold_multiplier)
    floor = int(math.ceil(economy.min_call_reserve_usd * MICRO))
    if not provider:
        worst = max(real_cost_micro(u, tier), world_cost_micro(u, tier, economy))
        hold = max(int(math.ceil(worst * mult)), floor)
        return hold, hold
    attempts = 1
    real = real_cost_micro(u, tier)
    fb = fallback_prices(tier.model) if (tier.provider == "anthropic" and tier.refusal_fallbacks) else None
    if fb is not None:
        attempts = 2
        real = attempts * _cost(u, *_dearest(_tier_prices(tier), fb))
    world = attempts * world_cost_micro(u, tier, economy) if economy.world_pricing == "equalized" else real
    return max(int(math.ceil(world * mult)), floor), int(math.ceil(real * mult))


def hold_micro(tier: TierConfig, economy: EconomyConfig, prompt_tokens: int, system_tokens: int = 0, *,
               schema_tokens: int = 0) -> int:
    """The balance hold of one call (see :func:`hold_parts`)."""
    return hold_parts(tier, economy, prompt_tokens, system_tokens, schema_tokens=schema_tokens)[0]


def cap_hold_micro(tier: TierConfig, economy: EconomyConfig, prompt_tokens: int, system_tokens: int = 0, *,
                   schema_tokens: int = 0) -> int:
    """What one call reserves against the real-dollar caps (see :func:`hold_parts`)."""
    return hold_parts(tier, economy, prompt_tokens, system_tokens, schema_tokens=schema_tokens)[1]
