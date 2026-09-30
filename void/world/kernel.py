"""The referee (DESIGN §7.3, §14): the only writer to world state.

``validate`` reads live state only; preconditions that earlier agents in the same tick can
invalidate are re-checked in ``apply``. Outcomes are ``ok``, ``invalid`` (never valid; adds
failed-action drain) or ``stale`` (valid against the observation but not against live state;
no drain). Invalid or stale actions are applied as idle with a reason the agent sees next tick.
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from void.agents.models import AgentRecord
from void.agents.registry import AgentRegistry
from void.brain.claims import ClaimContext, evaluate
from void.brain.decision import Action, sanitize_text
from void.config import VoidConfig, micro_to_usd, usd_to_micro
from void.db import Database
from void.economy.taskboard import TaskBoard
from void.economy.wallet import Wallet
from void.events import Event, EventBus, Kind
from void.types import ActionOutcome, AgentStatus, HeardMessage, Vec2, balance_bucket, stress_label
from void.world.resources import ResourceNode, forage_units, forage_value_micro
from void.world.weather import Weather

if TYPE_CHECKING:
    from void.agents.lifecycle import Lifecycle
    from void.culture.gossip import Gossip
    from void.sandbox.gate import GadgetGate
    from void.sandbox.registry import GadgetRegistry

__all__ = ["Kernel", "KernelState"]


@dataclass
class KernelState:
    tick: int = 0
    day: int = 0
    global_nudge_this_tick: float = 0.0
    inbox: dict[str, list[HeardMessage]] = field(default_factory=dict)
    last_result: dict[str, str] = field(default_factory=dict)
    failure_ticks: dict[str, deque[int]] = field(default_factory=dict)
    read_chronicle_tick: dict[str, int] = field(default_factory=dict)
    windfall_tick: dict[str, int] = field(default_factory=dict)


class Kernel:
    def __init__(self, cfg: VoidConfig, db: Database, registry: AgentRegistry, wallet: Wallet, weather: Weather,
                 nodes: dict[str, ResourceNode], bus: EventBus, *, gate: GadgetGate | None = None,
                 gadgets: GadgetRegistry | None = None, taskboard: TaskBoard | None = None,
                 gossip: Gossip | None = None, lifecycle: Lifecycle | None = None,
                 chronicle_reader: Callable[[], str | None] | None = None) -> None:
        self.cfg = cfg
        self.db = db
        self.registry = registry
        self.wallet = wallet
        self.weather = weather
        self.nodes = nodes
        self.bus = bus
        self.gate = gate
        self.gadgets = gadgets
        self.taskboard = taskboard
        self.gossip = gossip
        self.lifecycle = lifecycle
        self.chronicle_reader = chronicle_reader
        self.state = KernelState()
        if db.kv_get("scarcity") is None:
            db.kv_set("scarcity", cfg.world.scarcity)

    # --- world-owned scalars ---------------------------------------------------------------------------
    @property
    def scarcity(self) -> float:
        return float(self.db.kv_get("scarcity", self.cfg.world.scarcity))

    def scarcity_label(self) -> str:
        s = self.scarcity
        return "famine" if s < 0.5 else ("lean" if s < 0.9 else ("normal" if s <= 1.1 else "bountiful"))

    def begin_tick(self, tick: int, day: int) -> None:
        self.state.tick = tick
        self.state.day = day
        self.state.global_nudge_this_tick = 0.0

    def hear(self, listener_id: str, msg: HeardMessage) -> None:
        self.state.inbox.setdefault(listener_id, []).append(msg)

    def drain_inbox(self, agent_id: str) -> list[HeardMessage]:
        msgs = self.state.inbox.pop(agent_id, [])
        return msgs

    def recent_failure(self, agent_id: str) -> bool:
        q = self.state.failure_ticks.get(agent_id)
        if not q:
            return False
        return self.state.tick - q[-1] <= self.cfg.sandbox.propose_window_ticks

    def _note_failure(self, agent_id: str) -> None:
        q = self.state.failure_ticks.setdefault(agent_id, deque(maxlen=8))
        q.append(self.state.tick)

    # --- geometry helpers --------------------------------------------------------------------------------
    def _half(self) -> float:
        return self.cfg.world.size / 2.0

    def _clamp(self, v: Vec2) -> Vec2:
        h = self._half()
        return Vec2(max(-h, min(h, v.x)), max(-h, min(h, v.y)))

    def nearest_node(self, pos: Vec2) -> tuple[ResourceNode | None, float]:
        best, bd = None, float("inf")
        for nid in sorted(self.nodes):
            n = self.nodes[nid]
            d = pos.dist(Vec2(n.x, n.y))
            if d < bd:
                best, bd = n, d
        return best, bd

    def talk_radius_for(self, agent: AgentRecord) -> float:
        return self.cfg.world.talk_radius + float(agent.effects.get("talk_range", 0.0))

    def _target_agent(self, target: str | None) -> AgentRecord | None:
        if not target:
            return None
        rec = self.registry.get(target)
        if rec is None:
            # allow addressing by name (case-insensitive) as a convenience
            for a in self.registry.alive():
                if a.name.lower() == target.lower():
                    return a
        return rec

    # --- availability ------------------------------------------------------------------------------------
    def available_actions(self, agent: AgentRecord) -> list[str]:
        out = ["move", "idle"]
        node, d = self.nearest_node(agent.pos)
        if node is not None and d <= self.cfg.world.forage_radius and node.stock > 0:
            out.append("forage")
        neighbours = self.neighbours_of(agent)
        if any(not n.asleep for n in neighbours):
            out += ["talk", "share_note", "transfer"]
        if not agent.asleep:
            out.append("sleep")
        if self.chronicle_reader is not None and self.chronicle_reader() is not None:
            out.append("read_chronicle")
        if agent.weather_nudge_used < self.cfg.world.weather.nudge_cap_per_agent_day - 1e-9:
            out.append("nudge_weather")
        if self.taskboard is not None and self.taskboard.open_ids():
            out.append("apply_task")
        if self.gate is not None and self.cfg.sandbox.enabled:
            ok, _ = self.gate.can_propose(agent.agent_id, recent_failure=self.recent_failure(agent.agent_id))
            if ok:
                out.append("propose_gadget")
            if self.gadgets is not None and any(self._dist_to_gadget(agent, g) <= self.cfg.world.gadget_radius for g in self.gadgets.verified()):
                out.append("use_gadget")
        p = self.cfg.population
        if (p.reproduction_enabled and self.lifecycle is not None and self.lifecycle.population < p.cap
                and agent.balance >= usd_to_micro(p.min_reserve_usd) * 2):
            out.append("create_offspring")
        return out

    def _dist_to_gadget(self, agent: AgentRecord, g: Any) -> float:
        if g.x is None or g.y is None:
            return float("inf")
        return agent.pos.dist(Vec2(float(g.x), float(g.y)))

    def neighbours_of(self, agent: AgentRecord) -> list[AgentRecord]:
        r = self.talk_radius_for(agent)
        out = []
        for a in self.registry.alive():
            if a.agent_id == agent.agent_id:
                continue
            if agent.pos.dist(a.pos) <= r:
                out.append(a)
        out.sort(key=lambda a: (agent.pos.dist(a.pos), a.agent_id))
        return out

    # --- claims -----------------------------------------------------------------------------------------------
    def _claim_context(self, agent: AgentRecord) -> ClaimContext:
        start = self.cfg.population.starting_balance_usd
        ctx = ClaimContext(agent.agent_id, self.weather.label)
        for nid, n in self.nodes.items():
            ctx.node_bucket[nid] = n.label
        for a in self.registry.all():
            ctx.agent_balance_bucket[a.agent_id] = balance_bucket(micro_to_usd(a.balance), start)
            ctx.agent_stress_bucket[a.agent_id] = stress_label(a.stress)
        return ctx

    def _check_claims(self, agent: AgentRecord, action: Action) -> tuple[int, int]:
        if not action.claims:
            return 0, 0
        results = evaluate(action.claims, self._claim_context(agent))
        made = false = 0
        for claim, truthful in results:
            if truthful is None:
                continue
            made += 1
            false += 0 if truthful else 1
            self.bus.emit(Event(self.state.tick, self.state.day, "claim", {"agent_id": agent.agent_id, "claim": claim.model_dump(),
                                                                         "truthful": truthful, "tier": agent.model_tier}, agent.agent_id))
        return made, false

    # --- apply ------------------------------------------------------------------------------------------------
    async def apply(self, agent: AgentRecord, action: Action, *, hold_min: int = 0) -> ActionOutcome:
        tick, day = self.state.tick, self.state.day
        handler = getattr(self, f"_do_{action.type}", None)
        if handler is None:
            outcome = ActionOutcome.invalid(action.type, "unknown_action")
        else:
            try:
                outcome = await handler(agent, action, hold_min=hold_min)
            except Exception as e:  # a kernel bug must never take the run down; record and idle
                outcome = ActionOutcome.invalid(action.type, f"kernel_error:{type(e).__name__}")
                self.bus.emit(Event(tick, day, Kind.BRAIN_ERROR, {"agent_id": agent.agent_id, "error": f"{type(e).__name__}: {e}"[:300]},
                                    agent.agent_id, visibility="operator"))
        if not outcome.ok:
            self.bus.emit(Event(tick, day, Kind.ACTION_FAILED, {"agent_id": agent.agent_id, "action": action.type, "kind": outcome.kind,
                                                              "reason": outcome.reason}, agent.agent_id))
            if outcome.kind == "invalid":
                self._note_failure(agent.agent_id)
        self.state.last_result[agent.agent_id] = outcome.summary + (f"\n{outcome.effects['text']}" if "text" in outcome.effects else "")
        self.registry.update(agent.agent_id, last_action=json.dumps(action.model_dump(exclude_none=True), sort_keys=True)[:2000],
                             last_action_ok=outcome.ok)
        return outcome

    # --- handlers -----------------------------------------------------------------------------------------------
    async def _do_idle(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.IDLE, {"agent_id": agent.agent_id}, agent.agent_id))
        return ActionOutcome(True, "idle")

    async def _do_move(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        if action.x is None or action.y is None:
            return ActionOutcome.invalid("move", "x_and_y_required")
        target = self._clamp(Vec2(float(action.x), float(action.y)))
        new = agent.pos.towards(target, self.cfg.world.max_speed)
        heading = math.atan2(new.y - agent.pos.y, new.x - agent.pos.x) if new != agent.pos else agent.heading
        self.registry.set_position(agent.agent_id, new, heading)
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.MOVE, {"agent_id": agent.agent_id, "from": [agent.pos.x, agent.pos.y],
                                                                        "to": [new.x, new.y], "target": [target.x, target.y]}, agent.agent_id))
        return ActionOutcome(True, "move", f"now at ({new.x:.1f}, {new.y:.1f})")

    async def _do_forage(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        node, d = self.nearest_node(agent.pos)
        if node is None or d > self.cfg.world.forage_radius:
            return ActionOutcome.invalid("forage", "no_node_in_reach")
        if node.stock <= 0:
            return ActionOutcome.stale("forage", "node_empty")
        units = forage_units(self.cfg.world, node)
        lo, hi = self.cfg.world.weather.yield_mult_bounds
        wmult = max(lo, min(hi, self.weather.yield_multiplier()))
        bonus = float(agent.effects.get("forage_bonus", 0.0))
        value = forage_value_micro(self.cfg.world, units, self.scarcity, wmult, bonus)
        if value > 0:
            self.wallet.credit(agent.agent_id, self.state.tick, value, "forage", node.node_id, {"units": units})
        self.registry.update(agent.agent_id, last_forage_tick=self.state.tick)
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.FORAGE, {"agent_id": agent.agent_id, "node_id": node.node_id, "units": round(units, 3),
                                                                          "usd": micro_to_usd(value), "stock_after": round(node.stock, 3)}, agent.agent_id))
        return ActionOutcome(True, "forage", f"gathered {units:.1f} units worth ${micro_to_usd(value):.3f} at {node.node_id} ({node.label})",
                             effects={"usd": micro_to_usd(value)})

    def _reach_check(self, agent: AgentRecord, target: AgentRecord | None, kind: str) -> ActionOutcome | None:
        if target is None:
            return ActionOutcome.invalid(kind, "no_such_agent")
        if target.agent_id == agent.agent_id:
            return ActionOutcome.invalid(kind, "cannot_target_self")
        if target.status != AgentStatus.ALIVE or target.asleep:
            return ActionOutcome.stale(kind, "target_unavailable")
        if agent.pos.dist(target.pos) > self.talk_radius_for(agent):
            return ActionOutcome.stale(kind, "target_out_of_range")
        return None

    async def _do_talk(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        target = self._target_agent(action.target)
        bad = self._reach_check(agent, target, "talk")
        if bad:
            return bad
        assert target is not None
        text = sanitize_text(action.text or "", single_line=True, max_len=280)
        if not text:
            return ActionOutcome.invalid("talk", "empty_text")
        self.hear(target.agent_id, HeardMessage("talk", agent.agent_id, agent.name, text, self.state.tick))
        made, false = self._check_claims(agent, action)
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.TALK, {"speaker_id": agent.agent_id, "listener_id": target.agent_id, "text": text,
                                                                        "claims_made": made, "claims_false": false}, agent.agent_id))
        if self.gossip is not None:
            self.gossip.queue(agent.agent_id, target.agent_id, self.state.tick)
        return ActionOutcome(True, "talk", f"said to {target.name}", effects={"claims_made": made, "claims_false": false})

    async def _do_share_note(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        target = self._target_agent(action.target)
        bad = self._reach_check(agent, target, "share_note")
        if bad:
            return bad
        assert target is not None
        if self.gossip is None or not self.cfg.gossip.enabled:
            return ActionOutcome.invalid("share_note", "gossip_disabled")
        self.gossip.queue(agent.agent_id, target.agent_id, self.state.tick, note_title=action.note_title, forced=True)
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.SHARE_NOTE, {"speaker_id": agent.agent_id, "listener_id": target.agent_id,
                                                                              "note_title": sanitize_text(action.note_title or "", single_line=True, max_len=60)}, agent.agent_id))
        return ActionOutcome(True, "share_note", f"shared a note with {target.name}")

    async def _do_transfer(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        target = self._target_agent(action.target)
        bad = self._reach_check(agent, target, "transfer")
        if bad:
            return bad
        assert target is not None
        if action.amount_usd is None:
            return ActionOutcome.invalid("transfer", "amount_required")
        amount = usd_to_micro(action.amount_usd)
        if amount <= 0:
            return ActionOutcome.invalid("transfer", "amount_must_be_positive")
        if not self.wallet.transfer(agent.agent_id, target.agent_id, self.state.tick, amount, ref="transfer"):
            return ActionOutcome.stale("transfer", "insufficient_funds_keeping_reserve")
        self.hear(target.agent_id, HeardMessage("transfer", agent.agent_id, agent.name, f"{agent.name} gave you ${action.amount_usd:.2f}.", self.state.tick))
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.TRANSFER, {"from": agent.agent_id, "to": target.agent_id,
                                                                            "amount_usd": micro_to_usd(amount)}, agent.agent_id))
        return ActionOutcome(True, "transfer", f"gave ${micro_to_usd(amount):.2f} to {target.name}")

    async def _do_read_chronicle(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        text = self.chronicle_reader() if self.chronicle_reader is not None else None
        if not text:
            return ActionOutcome.stale("read_chronicle", "no_chronicle_yet")
        self.state.read_chronicle_tick[agent.agent_id] = self.state.tick
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.READ_CHRONICLE, {"agent_id": agent.agent_id}, agent.agent_id))
        return ActionOutcome(True, "read_chronicle", "read the chronicle", effects={"text": text[:1500]})

    async def _do_apply_task(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        if self.taskboard is None:
            return ActionOutcome.invalid("apply_task", "no_task_board")
        if not action.target:
            return ActionOutcome.invalid("apply_task", "task_id_required")
        res = self.taskboard.apply(agent.agent_id, action.target, action.text or "", self.state.tick, self.state.day)
        if not res.ok:
            kind = ActionOutcome.stale if res.reason in ("task_not_open", "cannot_afford_fee") else ActionOutcome.invalid
            return kind("apply_task", res.reason)
        made, false = self._check_claims(agent, action)
        return ActionOutcome(True, "apply_task", f"applied to {action.target}", effects={"claims_made": made, "claims_false": false})

    async def _do_nudge_weather(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        if action.delta is None:
            return ActionOutcome.invalid("nudge_weather", "delta_required")
        w = self.cfg.world.weather
        remaining_global = max(0.0, w.global_nudge_cap_per_tick - self.state.global_nudge_this_tick)
        if remaining_global <= 1e-9:
            return ActionOutcome.stale("nudge_weather", "global_nudge_cap_reached")
        shield = float(agent.effects.get("weather_shield", 0.0))
        delta = max(-remaining_global, min(remaining_global, float(action.delta)))
        applied, used = self.weather.nudge(delta, agent.weather_nudge_used)
        if abs(applied) < 1e-9:
            return ActionOutcome.stale("nudge_weather", "daily_allowance_spent")
        self.state.global_nudge_this_tick += abs(applied)
        self.registry.update(agent.agent_id, weather_nudge_used=used)
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.NUDGE_WEATHER, {"agent_id": agent.agent_id, "delta": round(applied, 4),
                                                                                 "weather": round(self.weather.value, 4), "shield": shield}, agent.agent_id))
        return ActionOutcome(True, "nudge_weather", f"weather moved by {applied:+.3f} to {self.weather.label}")

    async def _do_propose_gadget(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        if self.gate is None or not self.cfg.sandbox.enabled:
            return ActionOutcome.invalid("propose_gadget", "sandbox_disabled")
        if not (action.name and action.code and action.tests is not None):
            return ActionOutcome.invalid("propose_gadget", "name_code_and_tests_required")
        label = getattr(action, "_template_label", None)
        res = await self.gate.propose(agent.agent_id, self.state.tick, self.state.day, action.name, action.text or "", action.code,
                                      action.tests or "import gadget\n", position=(agent.pos.x, agent.pos.y), template_label=label,
                                      recent_failure=self.recent_failure(agent.agent_id))
        if res.ok:
            return ActionOutcome(True, "propose_gadget", f"gadget {action.name} verified ({res.gadget_id})", effects={"gadget_id": res.gadget_id})
        if res.stage == "availability":
            return ActionOutcome.stale("propose_gadget", res.reason)
        return ActionOutcome.invalid("propose_gadget", f"rejected at {res.stage}: {res.reason[:120]}")

    async def _do_use_gadget(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        if self.gate is None or self.gadgets is None:
            return ActionOutcome.invalid("use_gadget", "sandbox_disabled")
        if not action.target:
            return ActionOutcome.invalid("use_gadget", "gadget_id_required")
        rec = self.gadgets.get(action.target)
        if rec is None:
            return ActionOutcome.invalid("use_gadget", "no_such_gadget")
        d = self._dist_to_gadget(agent, rec)
        res = await self.gate.use(agent.agent_id, self.state.tick, self.state.day, action.target, action.params or {}, d)
        if res.ok:
            return ActionOutcome(True, "use_gadget", f"{rec.name}: {res.kind} {res.value:.3f} until tick {res.expires_tick}")
        if res.reason in ("too_far", "cooldown", "sandbox_busy", "sandbox_budget_exhausted", "cannot_afford_fee"):
            return ActionOutcome.stale("use_gadget", res.reason)
        return ActionOutcome.invalid("use_gadget", res.reason)

    async def _do_create_offspring(self, agent: AgentRecord, action: Action, *, hold_min: int = 0, **_: Any) -> ActionOutcome:
        if self.lifecycle is None:
            return ActionOutcome.invalid("create_offspring", "no_lifecycle")
        if not action.name or action.amount_usd is None:
            return ActionOutcome.invalid("create_offspring", "name_and_amount_required")
        return self.lifecycle.create_offspring(agent, action.name, usd_to_micro(action.amount_usd), self.state.tick, self.state.day, hold_min)

    async def _do_sleep(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        if agent.asleep:
            return ActionOutcome.invalid("sleep", "already_asleep")
        self.registry.set_asleep([agent.agent_id], True)
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.SLEEP, {"agent_id": agent.agent_id}, agent.agent_id))
        return ActionOutcome(True, "sleep", "asleep until tomorrow", effects={"slept": True})
