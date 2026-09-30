"""Entropy budget, stress, effective temperature and the degeneration operator (DESIGN §8)."""

from __future__ import annotations

import random
from dataclasses import dataclass

from void.brain import coherence
from void.brain.base import Sampling
from void.brain.decision import ACTION_TYPES, Action, Decision
from void.config import EntropyConfig, TierConfig

__all__ = ["DrainInputs", "drain", "step", "degeneration_probability", "corrupt", "restore"]


@dataclass
class DrainInputs:
    balance_ratio: float          # balance / starting_balance
    last_action_failed: bool
    neighbours: int
    weather: float
    ticks_since_forage: int | None  # None = never foraged


def drain(cfg: EntropyConfig, inp: DrainInputs) -> float:
    d = cfg.drain
    total = d.baseline
    total += d.low_balance * max(0.0, 1.0 - inp.balance_ratio)
    total += d.failed_action * (1.0 if inp.last_action_failed else 0.0)
    total += d.crowding * inp.neighbours
    total += d.weather * abs(inp.weather)
    hungry = inp.ticks_since_forage is None or inp.ticks_since_forage > d.hunger_ticks
    total += d.hunger * (1.0 if hungry else 0.0)
    return max(0.0, total)


def degeneration_probability(cfg: EntropyConfig, tier: TierConfig, t_eff: float) -> float:
    if t_eff < tier.collapse_temperature:
        return 0.0
    span = tier.max_temperature - tier.collapse_temperature
    frac = 1.0 if span <= 0 else min(1.0, (t_eff - tier.collapse_temperature) / span)
    p0, p1 = cfg.degeneration.p_at_collapse, cfg.degeneration.p_at_max
    return p0 + (p1 - p0) * frac


def step(cfg: EntropyConfig, tier: TierConfig, budget: float, inp: DrainInputs, rng: random.Random) -> tuple[float, Sampling]:
    """Apply one tick of drain; return (new_budget, sampling decision)."""
    new_budget = max(0.0, budget - drain(cfg, inp))
    stress = 1.0 - (new_budget / tier.entropy_budget_max if tier.entropy_budget_max > 0 else 0.0)
    stress = max(0.0, min(1.0, stress))
    t_eff = cfg.base_temperature + (tier.max_temperature - cfg.base_temperature) * stress
    p = degeneration_probability(cfg, tier, t_eff)
    degenerate = rng.random() < p
    api_t = min(1.0, t_eff / tier.max_temperature) if tier.supports_temperature else None
    return new_budget, Sampling(effective_temperature=t_eff, stress=stress, degenerate=degenerate, api_temperature=api_t)


def restore(cfg: EntropyConfig, tier: TierConfig, budget: float) -> float:
    return min(tier.entropy_budget_max, budget + cfg.restore_on_sleep * tier.entropy_budget_max)


NOISE_ACTIONS = ("move", "forage", "talk", "idle", "sleep", "read_chronicle", "nudge_weather")


def _random_action(available: list[str], obs_world_size: float, rng: random.Random, targets: dict[str, list[str]]) -> Action:
    """Noise never moves money and never touches the sandbox (DESIGN §8)."""
    kinds = [a for a in available if a in ACTION_TYPES and a in NOISE_ACTIONS] or ["idle"]
    kind = rng.choice(kinds)
    half = obs_world_size / 2.0
    if kind == "move":
        return Action(type="move", x=rng.uniform(-half, half), y=rng.uniform(-half, half))
    if kind == "talk":
        agents = targets.get("agents", [])
        if not agents:
            return Action.idle()
        t = rng.choice(agents)
        return Action(type="talk", target=t, text=" ".join(rng.choice(["the", "void", "again", "again", "hungry", "node", "why"]) for _ in range(8)))
    if kind == "nudge_weather":
        return Action(type="nudge_weather", delta=rng.uniform(-0.1, 0.1))
    return Action(type=kind)  # forage, read_chronicle, sleep, idle


def corrupt(
    decision: Decision,
    sampling: Sampling,
    tier: TierConfig,
    cfg: EntropyConfig,
    rng: random.Random,
    *,
    available: list[str],
    world_size: float,
    targets: dict[str, list[str]],
    previous_action: Action | None,
) -> tuple[Decision, str]:
    """Apply the degeneration operator; returns (corrupted decision, mode)."""
    w = cfg.degeneration.mode_weights
    modes = list(w.keys())
    weights = [max(0.0, w[m]) for m in modes]
    mode = rng.choices(modes, weights=weights, k=1)[0] if sum(weights) > 0 else "garble"
    span = max(1e-9, tier.max_temperature - tier.collapse_temperature)
    severity = max(0.0, min(1.0, (sampling.effective_temperature - tier.collapse_temperature) / span))
    if mode == "random_action":
        action = _random_action(available, world_size, rng, targets)
        return Decision(thought=decision.thought, memory_ops=decision.memory_ops, action=action), mode
    if mode == "perseverate":
        action = previous_action.model_copy() if previous_action is not None else decision.action
        return Decision(thought=decision.thought, memory_ops=decision.memory_ops, action=action), mode
    action = decision.action.model_copy()
    if action.text:
        action.text = coherence.degrade(action.text, 0.4 + 0.6 * severity, rng)[:400]
    ops = []
    for op in decision.memory_ops:
        op2 = op.model_copy()
        op2.text = coherence.degrade(op2.text, 0.4 + 0.6 * severity, rng)[:600]
        ops.append(op2)
    thought = coherence.degrade(decision.thought, 0.4 + 0.6 * severity, rng)[:600]
    return Decision(thought=thought, memory_ops=ops, action=action), "garble"
