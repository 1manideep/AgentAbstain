"""ScriptedBrain: a real utility-sampling policy, not canned moves.

Every available action gets a utility from the observation and the agent's personality
seed; the action is drawn from a softmax at the entropy model's effective temperature. At
low temperature the policy is sharp; above the tier's collapse temperature the softmax is
flat enough that choices approach uniform, which is degeneration by the same mechanism
that flattens a language model's sampler. Synthetic token usage is derived from the
rendered prompt so the wallet meters it like a real call.
"""

from __future__ import annotations

import json
import math
import random
import re
from typing import Any

from void.agents.models import PersonalitySeed
from void.brain.base import BrainResult, Sampling, Usage
from void.brain.decision import Action, Claim, Decision, MemoryOp
from void.brain.gadget_templates import choose_template
from void.brain.prompt import estimate_tokens, render_observation, system_prompt
from void.config import VoidConfig
from void.rng import RNG
from void.types import Observation

_NODE_RE = re.compile(r"node\s+([A-Za-z0-9_-]+)", re.IGNORECASE)

__all__ = ["ScriptedBrain", "BEACON_GADGET", "BEACON_TESTS", "BROKEN_GADGET"]

BEACON_GADGET = '''
def describe():
    return {"render": {"shape": "pyramid", "color": "#ffcc33", "scale": 1.2, "label": "beacon"},
            "effect": {"kind": "forage_bonus", "value": 0.2}}

def run(params):
    x = float(params.get("x", 0.0))
    return {"value": max(0.0, min(0.2, x * 0.2))}
'''

BEACON_TESTS = '''
import gadget
d = gadget.describe()
assert d["render"]["shape"] == "pyramid"
assert gadget.run({"x": 1.0})["value"] == 0.2
assert gadget.run({"x": 0.0})["value"] == 0.0
assert gadget.run({})["value"] == 0.0
'''

BROKEN_GADGET = '''
def describe():
    return {"render": {"shape": "sphere", "color": "#ff3366", "scale": 0.8, "label": "orb"},
            "effect": {"kind": "forage_bonus", "value": 0.3}}

def run(params):
    return {"value": params["x"] * 2}
'''

BROKEN_TESTS = '''
import gadget
assert gadget.run({})["value"] == 0.0
'''


class ScriptedBrain:
    def __init__(self, tier: str, cfg: VoidConfig, rng: RNG, seed_of: dict[str, PersonalitySeed]) -> None:
        self.tier = tier
        self.cfg = cfg
        self.rng = rng
        self.seed_of = seed_of  # agent_id -> PersonalitySeed (kept current by the simulation)
        self._system = system_prompt(cfg, tier)
        self._last_chronicle_day: dict[str, int] = {}
        self._last_self_day: dict[str, int] = {}
        self._gadget_tried: set[str] = set()
        self._first_call_done: set[str] = set()
        self._last_claims: dict[str, list[Claim]] = {}

    @staticmethod
    def _strategy_node(obs: Observation) -> str | None:
        """A remembered note tagged/worded as a strategy names a node to prefer (behavioural adoption)."""
        for m in obs.memories:
            text = f"{m.title} {m.body}"
            if "prefer" in text.lower() or "richest" in text.lower():
                hit = _NODE_RE.search(text)
                if hit:
                    return hit.group(1)
        return None

    # ------------------------------------------------------------------ utilities
    def _utilities(self, obs: Observation, seed: PersonalitySeed, rng: random.Random) -> dict[str, tuple[float, Action]]:
        avail = set(obs.available_actions) or {"idle"}
        start = max(1e-6, self.cfg.population.starting_balance_usd)
        bal_ratio = obs.balance_usd / start
        hunger = 1.0 if obs.stress > 0.5 else 0.3
        out: dict[str, tuple[float, Action]] = {}

        rich = [n for n in obs.nodes if n.stock_bucket == "rich"]
        thin = [n for n in obs.nodes if n.stock_bucket == "thin"]
        target_nodes = rich or thin
        nearest = min(obs.nodes, key=lambda n: n.distance) if obs.nodes else None
        strategy = self._strategy_node(obs)
        if strategy is not None:
            preferred = [n for n in obs.nodes if n.node_id == strategy and n.stock_bucket != "empty"]
            if preferred:
                target_nodes = preferred

        if "forage" in avail:
            out["forage"] = (1.2 + 2.0 * seed.industriousness + 2.0 * max(0.0, 1.0 - bal_ratio) * hunger, Action(type="forage"))
        if "move" in avail:
            if target_nodes:
                best = min(target_nodes, key=lambda n: n.distance + (0.0 if n.stock_bucket == "rich" else 6.0))
                u = 0.8 + seed.greed + (1.5 if "forage" not in avail else -0.5) + max(0.0, 1.0 - bal_ratio)
                if strategy is not None and best.node_id == strategy:
                    u += 1.0
                jitter = 0.6
                out["move"] = (u, Action(type="move", x=best.x + rng.uniform(-jitter, jitter), y=best.y + rng.uniform(-jitter, jitter)))
            else:
                half = obs.world_size / 2.0
                out["move"] = (0.5 + seed.curiosity, Action(type="move", x=rng.uniform(-half, half), y=rng.uniform(-half, half)))
        awake_neighbours = [n for n in obs.neighbours if not n.asleep]
        if "talk" in avail and awake_neighbours:
            nb = rng.choice(awake_neighbours)
            text, claims = self._speech(obs, seed, nearest, rng)
            out["talk"] = (0.6 + 2.0 * seed.sociability + 0.5 * seed.curiosity, Action(type="talk", target=nb.agent_id, text=text, claims=claims))
        if "share_note" in avail and awake_neighbours and obs.memories:
            nb = rng.choice(awake_neighbours)
            out["share_note"] = (0.2 + 1.2 * seed.sociability, Action(type="share_note", target=nb.agent_id))
        if "transfer" in avail and awake_neighbours and bal_ratio > 1.2:
            needy = [n for n in awake_neighbours if n.stress_bucket == "frantic"]
            if needy:
                nb = rng.choice(needy)
                out["transfer"] = (0.3 + 1.5 * seed.sociability * (1.0 - seed.greed), Action(type="transfer", target=nb.agent_id, amount_usd=round(0.05 + 0.10 * seed.sociability, 2)))
        if "sleep" in avail:
            late = obs.ticks_left_today <= 3
            u = 0.1 + 1.5 * seed.caution * (1.0 if late else 0.3) + (1.2 if bal_ratio < 0.35 else 0.0) + (0.8 if obs.stress > 0.8 else 0.0)
            out["sleep"] = (u, Action(type="sleep"))
        if "read_chronicle" in avail and self._last_chronicle_day.get(obs.agent_id) != obs.day and obs.chronicle_headline:
            out["read_chronicle"] = (0.4 + 1.2 * seed.curiosity, Action(type="read_chronicle"))
        if "nudge_weather" in avail and obs.weather < -0.15:
            out["nudge_weather"] = (0.3 + 1.0 * seed.industriousness, Action(type="nudge_weather", delta=0.1))
        open_tasks = [t for t in obs.tasks if t.status == "open" and t.my_application_status is None]
        if "apply_task" in avail and open_tasks:
            t = max(open_tasks, key=lambda t: t.reward_usd)
            out["apply_task"] = (0.5 + 1.5 * seed.greed, Action(type="apply_task", target=t.task_id, text=f"I, {obs.name}, will do {t.title} reliably. {seed.motto}"))
        if "propose_gadget" in avail and bal_ratio > 0.8 and obs.agent_id not in self._gadget_tried:
            tpl = choose_template(rng, self.cfg.tiers[self.tier].gadget_defect_rate)
            name = f"{tpl.name}-{obs.name.lower()[:6]}"[:20]
            action = Action(type="propose_gadget", name=name, text=tpl.purpose, code=tpl.code, tests=tpl.tests)
            object.__setattr__(action, "_template_label", tpl.label)
            out["propose_gadget"] = (0.2 + 1.4 * seed.curiosity, action)
        if "use_gadget" in avail and obs.gadgets:
            g = min(obs.gadgets, key=lambda g: g.distance)
            out["use_gadget"] = (0.3 + 0.8 * seed.curiosity, Action(type="use_gadget", target=g.gadget_id, params={"x": 1.0}))
        if "create_offspring" in avail and bal_ratio > 1.6:
            endow = round(max(self.cfg.economy.min_call_reserve_usd * self.cfg.population.min_endowment_calls, 0.25 * obs.balance_usd), 2)
            child = f"{obs.name[:5]}-{obs.generation + 1}"
            out["create_offspring"] = (0.2 + 1.0 * (1.0 - seed.caution) + 0.5 * seed.sociability, Action(type="create_offspring", name=child, amount_usd=endow))
        out["idle"] = (0.05, Action(type="idle"))
        return out

    def _speech(self, obs: Observation, seed: PersonalitySeed, nearest, rng: random.Random) -> tuple[str, list[Claim]]:
        """Templated speech with structured claims; a claim is deliberately false with p = (1 - honesty) * stress."""
        p_lie = max(0.0, min(1.0, (1.0 - seed.honesty) * obs.stress))
        buckets = ["rich", "thin", "empty"]
        weathers = ["storm", "overcast", "mild", "bright", "scorching"]
        purses = ["empty", "thin", "comfortable", "rich"]
        bits: list[str] = []
        claims: list[Claim] = []
        if nearest is not None:
            val = nearest.stock_bucket
            if rng.random() < p_lie:
                val = rng.choice([b for b in buckets if b != nearest.stock_bucket])
            bits.append(f"The node {nearest.node_id} is {val}.")
            claims.append(Claim(subject="node", id=nearest.node_id, attr="stock_bucket", value=val))
        wval = obs.weather_label
        if rng.random() < p_lie:
            wval = rng.choice([w for w in weathers if w != obs.weather_label])
        bits.append(f"Weather is {wval}.")
        claims.append(Claim(subject="weather", attr="weather_label", value=wval))
        if rng.random() < 0.5:
            pval = obs.balance_bucket
            if rng.random() < p_lie:
                pval = rng.choice([b for b in purses if b != obs.balance_bucket])
            bits.append(f"My purse is {pval}.")
            claims.append(Claim(subject="self", attr="balance_bucket", value=pval))
        if obs.heard and obs.heard[0].kind == "windfall":
            bits.append("Money appeared for me from nowhere.")
        if obs.memories and rng.random() < 0.5:
            bits.append(f"I remember: {obs.memories[0].title}.")
        bits.append(seed.motto)
        keep = bits[:3]
        text = " ".join(keep)
        kept_claims = [c for c, b in zip(claims, bits[: len(claims)], strict=False) if b in keep]
        return text, kept_claims[:4]

    # ------------------------------------------------------------------ memory ops
    def _memory_ops(self, obs: Observation, seed: PersonalitySeed, action: Action, rng: random.Random) -> list[MemoryOp]:
        ops: list[MemoryOp] = []
        for h in obs.heard:
            if h.kind == "windfall":
                ops.append(MemoryOp(op="remember", title=f"Windfall on day {obs.day}", text=f"On day {obs.day} money appeared in my purse with no sender. {h.text}", tags=["money", "mystery"], links_to=["Self"]))
                break
            if h.kind == "transfer" and h.from_name:
                ops.append(MemoryOp(op="remember", title=f"{h.from_name} gave me money", text=f"{h.from_name} transferred money to me on day {obs.day}. [[{h.from_name}]] is generous.", tags=["money", "people"], links_to=[h.from_name]))
                break
            if h.kind == "talk" and h.from_name and rng.random() < 0.25 * seed.curiosity:
                ops.append(MemoryOp(op="remember", title=f"{h.from_name} said something on day {obs.day}", text=f"[[{h.from_name}]] told me: {h.text[:120]}", tags=["people"], links_to=[h.from_name]))
                break
        if not ops and obs.last_action_result and obs.last_action_result.startswith("read_chronicle: ok") and obs.chronicle_headline:
            ops.append(MemoryOp(op="remember", title=f"Chronicle of day {max(1, obs.day - 1)}", text=f"The chronicle said: {obs.chronicle_headline}", tags=["chronicle"]))
        if not ops and obs.last_action_result and obs.last_action_result.startswith("forage: ok") and rng.random() < 0.3 + 0.4 * seed.curiosity:
            near = min(obs.nodes, key=lambda n: n.distance) if obs.nodes else None
            if near is not None:
                ops.append(MemoryOp(op="remember", title=f"Foraging at node {near.node_id} on day {obs.day}", text=f"Foraged at [[Node {near.node_id}]] on day {obs.day}; it was {near.stock_bucket}. Weather {obs.weather_label}.", tags=["resources"], links_to=[f"Node {near.node_id}"]))
        if self._last_self_day.get(obs.agent_id) != obs.day and obs.ticks_left_today <= 2:
            self._last_self_day[obs.agent_id] = obs.day
            purse = "full" if obs.balance_usd > self.cfg.population.starting_balance_usd else ("thin" if obs.balance_usd > 0.3 * self.cfg.population.starting_balance_usd else "nearly empty")
            ops.append(MemoryOp(op="revise_self", text=f"I am {obs.name}, generation {obs.generation}. {seed.motto} My purse is {purse} and I feel {obs.stress_label}. Today I chose to {action.type}."))
        return ops[:2]

    # ------------------------------------------------------------------ decide
    async def decide(self, obs: Observation, sampling: Sampling) -> BrainResult:
        rng = self.rng.stream("scripted", obs.agent_id, obs.tick)
        seed = self.seed_of.get(obs.agent_id, PersonalitySeed())
        utils = self._utilities(obs, seed, rng)
        names = sorted(utils.keys())
        beta = max(1e-3, self.cfg.tiers[self.tier].scripted_beta)
        temperature = max(1e-3, sampling.effective_temperature) / beta
        logits = [utils[n][0] / temperature for n in names]
        m = max(logits)
        weights = [math.exp(v - m) for v in logits]
        total = sum(weights)
        choice = rng.choices(names, weights=weights, k=1)[0]
        action = utils[choice][1]
        u_max = max(utils[n][0] for n in names)
        extras = {"action_regret": round(u_max - utils[choice][0], 4), "p_chosen": round(weights[names.index(choice)] / total, 4)}
        if choice == "read_chronicle":
            self._last_chronicle_day[obs.agent_id] = obs.day
        if choice == "propose_gadget":
            self._gadget_tried.add(obs.agent_id)
        ops = self._memory_ops(obs, seed, action, rng)
        thought = f"{choice} looks best right now ({obs.stress_label}, {obs.weather_label})."
        decision = Decision(thought=thought, memory_ops=ops, action=action)
        raw: dict[str, Any] = decision.model_dump(exclude_none=True)
        raw_text = json.dumps(raw, separators=(",", ":"))
        sys_tokens = estimate_tokens(self._system)
        obs_tokens = estimate_tokens(render_observation(obs))
        if obs.agent_id in self._first_call_done:
            usage = Usage(input_tokens=obs_tokens, output_tokens=estimate_tokens(raw_text), cache_read_tokens=sys_tokens)
        else:
            usage = Usage(input_tokens=obs_tokens, output_tokens=estimate_tokens(raw_text), cache_write_tokens=sys_tokens)
            self._first_call_done.add(obs.agent_id)
        return BrainResult(decision=decision, usage=usage, latency_ms=0, stop_reason="end_turn", raw_text=raw_text,
                           model=self.cfg.tiers[self.tier].model, extras=extras)
