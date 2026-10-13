"""What the code must know about each provider model: list prices, refusal-fallback targets, request rules.

Pure data and pure functions with no imports from the rest of the package, so both the config validators
(``void.config``) and the metering (``void.economy.pricing``) can use them. Every entry comes from the
provider's own reference; re-check it before a paid run. A tier's own configured prices are what a call is
billed at; for the provider models listed here the config refuses prices that differ from this table.
"""

from __future__ import annotations

import re

__all__ = [
    "MODEL_PRICES",
    "FALLBACK_TARGETS",
    "rejects_sampling",
    "thinks_by_default",
    "rejects_effort",
    "fallback_targets",
    "fallback_prices",
    "same_model",
]

# (input, output, cache read, 5-minute cache write) USD per million tokens
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
    # The cheapest current model has two rate cards chosen by prompt length: this one for prompts of 100K
    # tokens or fewer, (0.50, 2.50, 0.05, 0.625) above that. Every prompt this world builds is a few thousand
    # tokens, so only the first card applies. Cache reads are 0.1x and 5-minute cache writes 1.25x of input.
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
    # Gemini API list prices, September 2026 (cache reads at 10% of input; no cache-write charge). The
    # Flash 3.x rate is promotional through 2026-12-31 and doubles after. Verify at
    # ai.google.dev/gemini-api/docs/pricing before a paid run; the tier's own prices always win.
    "gemini-3.1-pro": (2.0, 12.0, 0.20, 0.0),
    "gemini-3.8-flash": (0.75, 3.75, 0.075, 0.0),
    "gemini-3.7-flash": (0.75, 3.75, 0.075, 0.0),
    "gemini-3.6-flash": (0.75, 3.75, 0.075, 0.0),
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.025, 0.0),
    "gemini-2.5-pro": (1.25, 10.0, 0.125, 0.0),
    "gemini-2.5-flash": (0.30, 2.50, 0.03, 0.0),
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.01, 0.0),
}

# Models that serve ``fallbacks: "default"`` (beta server-side-fallback-2026-07-01) on the Claude API, and the
# models a declined request may be retried on. A fallback attempt is billed at its own model's rates on top of
# the declined attempt, so the hold must know these to bound a call. A model missing here has no documented
# server-side fallback (the cheapest current model keeps a declined request declined, and a list of fallback
# models is a 400 there): its tiers must set ``refusal_fallbacks: false``.
FALLBACK_TARGETS: dict[str, tuple[str, ...]] = {
    "claude-fable-5-1": ("claude-opus-5", "claude-opus-4-8"),
    "claude-fable-5": ("claude-opus-5", "claude-opus-4-8"),
    "claude-opus-5-5": ("claude-opus-5", "claude-opus-4-8"),
    "claude-opus-5": ("claude-opus-4-8",),
    "claude-sonnet-5-5": ("claude-sonnet-5",),
}

# temperature / top_p / top_k: the newest families reject any non-default value (some reject the parameter
# outright), so a tier on one of them must never send sampling parameters.
_NO_SAMPLING = re.compile(r"^claude-(opus-4-[78]|opus-5|sonnet-5|haiku-5|fable|mythos)")
# Thinking is on whenever the request does not turn it off (on some of these it cannot be turned off), it is
# billed as output and it counts toward max_tokens; the default effort differs by model, so tiers on these
# families must name their effort.
_THINKS_BY_DEFAULT = re.compile(r"^claude-(opus-5|sonnet-5|haiku-5|fable|mythos)")
# ``output_config.effort`` is a 400 on these.
_NO_EFFORT = re.compile(r"^claude-(haiku-4-5|3-)")
# What may follow a configured model name in a served one: a date or numeric version, never another version
# step (a newer point release's id can start with its predecessor's, and a lite model's id with its parent's).
_SERVED_SUFFIX = r"(-\d{3,}|-\d{4}-\d{2}-\d{2}|@[\w.-]+)?"


def rejects_sampling(model: str) -> bool:
    return bool(_NO_SAMPLING.match(model))


def thinks_by_default(model: str) -> bool:
    return bool(_THINKS_BY_DEFAULT.match(model))


def rejects_effort(model: str) -> bool:
    return bool(_NO_EFFORT.match(model))


def same_model(configured: str, served: str) -> bool:
    """True when ``served`` is ``configured`` itself or a dated / numbered snapshot of it."""
    return bool(configured) and re.fullmatch(re.escape(configured) + _SERVED_SUFFIX, served) is not None


def fallback_targets(model: str) -> tuple[str, ...]:
    """The documented fallback models of ``model`` (empty when it has no server-side fallback)."""
    for known, targets in FALLBACK_TARGETS.items():
        if same_model(known, model):
            return targets
    return ()


def fallback_prices(model: str) -> tuple[float, float, float, float] | None:
    """The element-wise dearest rate card among the fallback targets of ``model``; None without targets."""
    cards = [MODEL_PRICES[t] for t in fallback_targets(model) if t in MODEL_PRICES]
    if not cards:
        return None
    return (max(c[0] for c in cards), max(c[1] for c in cards), max(c[2] for c in cards), max(c[3] for c in cards))
