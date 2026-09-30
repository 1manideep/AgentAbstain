"""Turn metered token usage into micro-dollars. Always after the call, never estimated.

Two prices exist (DESIGN §0, §9.2): the *real* cost charged by the provider, which is what
the daily and total caps protect, and the *world* cost debited from the agent's wallet,
which equals the real cost under ``world_pricing: real`` and a single configured price
under ``equalized`` (so model tier is not confounded with economic stress).
"""

from __future__ import annotations

import math

from void.brain.base import Usage
from void.config import MICRO, EconomyConfig, TierConfig

__all__ = ["cost_micro", "real_cost_micro", "world_cost_micro", "hold_micro"]


def _cost(usage: Usage, p_in: float, p_out: float, p_cr: float, p_cw: float) -> int:
    usd = (usage.input_tokens * p_in + usage.output_tokens * p_out
           + usage.cache_read_tokens * p_cr + usage.cache_write_tokens * p_cw) / 1_000_000
    return int(math.ceil(usd * MICRO))


def real_cost_micro(usage: Usage, tier: TierConfig) -> int:
    return _cost(usage, tier.price_in_per_mtok, tier.price_out_per_mtok,
                 tier.price_cache_read_per_mtok, tier.price_cache_write_per_mtok)


cost_micro = real_cost_micro


def world_cost_micro(usage: Usage, tier: TierConfig, economy: EconomyConfig) -> int:
    if economy.world_pricing == "equalized":
        return _cost(usage, economy.equalized_price_in_per_mtok, economy.equalized_price_out_per_mtok,
                     economy.equalized_price_cache_read_per_mtok, economy.equalized_price_cache_write_per_mtok)
    return real_cost_micro(usage, tier)


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
