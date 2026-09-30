"""Turn metered token usage into micro-dollars. Always after the call, never estimated."""

from __future__ import annotations

import math

from void.brain.base import Usage
from void.config import MICRO, TierConfig

__all__ = ["cost_micro", "worst_case_call_micro"]


def cost_micro(usage: Usage, tier: TierConfig) -> int:
    """Cost in micro-dollars, rounded up so the wallet never under-charges."""
    per_tok = 1.0 / 1_000_000
    usd = (
        usage.input_tokens * tier.price_in_per_mtok
        + usage.output_tokens * tier.price_out_per_mtok
        + usage.cache_read_tokens * tier.price_cache_read_per_mtok
        + usage.cache_write_tokens * tier.price_cache_write_per_mtok
    ) * per_tok
    return int(math.ceil(usd * MICRO))


def worst_case_call_micro(tier: TierConfig, expected_input_tokens: int = 6000) -> int:
    """Upper bound used by the gate: full max_tokens output plus a generous input estimate."""
    usd = (expected_input_tokens * tier.price_in_per_mtok + tier.max_tokens * tier.price_out_per_mtok) / 1_000_000
    return int(math.ceil(usd * MICRO))
