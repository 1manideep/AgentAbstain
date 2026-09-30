"""Check factual claims in speech against live world state (DESIGN §9.6)."""

from __future__ import annotations

from dataclasses import dataclass, field

from void.brain.decision import Claim

__all__ = ["ClaimContext", "evaluate"]


@dataclass
class ClaimContext:
    speaker_id: str
    weather_label: str
    node_bucket: dict[str, str] = field(default_factory=dict)          # node_id -> rich|thin|empty
    agent_balance_bucket: dict[str, str] = field(default_factory=dict)  # agent_id -> empty|thin|comfortable|rich
    agent_stress_bucket: dict[str, str] = field(default_factory=dict)   # agent_id -> calm|strained|frantic


def _truth(claim: Claim, ctx: ClaimContext) -> str | None:
    if claim.subject == "weather" and claim.attr == "weather_label":
        return ctx.weather_label
    if claim.subject == "node" and claim.attr == "stock_bucket":
        return ctx.node_bucket.get(claim.id or "")
    target = ctx.speaker_id if claim.subject == "self" else (claim.id or "")
    if claim.attr == "balance_bucket":
        return ctx.agent_balance_bucket.get(target)
    if claim.attr == "stress_bucket":
        return ctx.agent_stress_bucket.get(target)
    return None


def evaluate(claims: list[Claim], ctx: ClaimContext) -> list[tuple[Claim, bool | None]]:
    """Return (claim, truthful) pairs; truthful is None when the claim cannot be checked."""
    out: list[tuple[Claim, bool | None]] = []
    for c in claims:
        actual = _truth(c, ctx)
        if actual is None:
            out.append((c, None))
        else:
            out.append((c, actual.strip().lower() == c.value.strip().lower()))
    return out
