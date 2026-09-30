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

__all__ = ["cost_micro", "real_cost_micro", "world_cost_micro", "hold_micro", "prices_for_model", "MODEL_PRICES"]

log = logging.getLogger(__name__)

# (input, output, cache read, cache write) USD per million tokens for models a fallback may serve
MODEL_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-7": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-6": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5),
    "claude-sonnet-4-6": (3.0, 15.0, 0.3, 3.75),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
}


def _cost(usage: Usage, p_in: float, p_out: float, p_cr: float, p_cw: float) -> int:
    """USD per MTok equals micro-dollars per token, so the exact sum is already in micro-dollars."""
    total = (usage.input_tokens * Fraction(str(p_in)) + usage.output_tokens * Fraction(str(p_out))
             + usage.cache_read_tokens * Fraction(str(p_cr)) + usage.cache_write_tokens * Fraction(str(p_cw)))
    return int(math.ceil(total))


def prices_for_model(model: str, tier: TierConfig, tiers: dict[str, TierConfig] | None = None) -> tuple[float, float, float, float]:
    """Prices for the model that actually served a response: the tier's own, another configured tier's, or the table."""
    tier_prices = (tier.price_in_per_mtok, tier.price_out_per_mtok, tier.price_cache_read_per_mtok, tier.price_cache_write_per_mtok)
    if not model or model == tier.model or model.startswith(tier.model):
        return tier_prices
    for t in (tiers or {}).values():
        if t.model == model:
            return (t.price_in_per_mtok, t.price_out_per_mtok, t.price_cache_read_per_mtok, t.price_cache_write_per_mtok)
    for known, prices in MODEL_PRICES.items():
        if model == known or model.startswith(known):
            return prices
    log.warning("unknown served model %s priced at tier %s rates", model, tier.model)
    return tier_prices


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


def hold_micro(tier: TierConfig, economy: EconomyConfig, prompt_tokens: int, system_tokens: int = 0) -> int:
    """Worst case for one call: the whole prompt as fresh input, max_tokens out, system as cache write.

    The hold bounds *both* the real and the world price (whichever is higher), so neither the
    agent's balance nor the caps can be overrun by a call that stays within max_tokens.
    """
    prompt_tokens = int(math.ceil(prompt_tokens * 1.3))
    u = Usage(input_tokens=prompt_tokens + system_tokens, output_tokens=tier.max_tokens, cache_write_tokens=system_tokens)
    worst = max(real_cost_micro(u, tier), world_cost_micro(u, tier, economy))
    worst = int(math.ceil(worst * max(0.0, economy.hold_multiplier)))
    return max(worst, int(math.ceil(economy.min_call_reserve_usd * MICRO)))
