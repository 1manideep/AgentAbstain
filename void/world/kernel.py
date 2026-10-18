"""The referee (DESIGN §7.3, §14): the only writer to world state.

Whether an action succeeds is decided on live state only; preconditions that earlier agents in the same
tick can invalidate are re-checked in ``apply``. Outcomes are ``ok``, ``invalid`` (never valid; adds
failed-action drain) or ``stale`` (valid against the observation but not against live state; no drain).
A failed action is classified against the observation the agent decided on (``apply(..., obs=...)``):
``stale`` when the observation showed the failed precondition met, so an earlier agent this tick changed
the world; ``invalid`` when the observation already showed it unmet, or the action was malformed. Without
an observation each failure keeps its fixed kind. Invalid or stale actions are applied as idle with a
reason the agent sees next tick.
"""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from void.agents.models import AgentRecord
from void.agents.registry import AgentRegistry
from void.brain.claims import ClaimContext, evaluate
from void.brain.decision import Action, sanitize_text, share_practice_enabled
from void.config import VoidConfig, micro_to_usd, usd_to_micro
from void.culture.practices import PracticeLedger
from void.db import Database
from void.economy.taskboard import TaskBoard
from void.economy.wallet import Wallet
from void.events import Event, EventBus, Kind
from void.rng import RNG
from void.types import (
    ActionOutcome,
    AgentStatus,
    HeardMessage,
    NeighbourView,
    Observation,
    Vec2,
    balance_bucket,
    stress_label,
)
from void.world.resources import ResourceNode, forage_units, forage_value_micro
from void.world.weather import Weather

if TYPE_CHECKING:
    from void.agents.lifecycle import Lifecycle
    from void.culture.gossip import Gossip
    from void.sandbox.gate import GadgetGate
    from void.sandbox.registry import GadgetRegistry

__all__ = ["Kernel", "KernelState"]


def _miss(action_type: str, reason: str, obs: Observation | None, observed_ok: bool, *, legacy: str = "stale") -> ActionOutcome:
    """A live precondition failed. Against the agent's observation (DESIGN §5, §7.4) the failure is ``stale`` when
    the observation showed the precondition met and ``invalid`` when it already showed it unmet; without an
    observation it keeps its fixed kind, ``legacy``."""
    kind = legacy if obs is None else ("stale" if observed_ok else "invalid")
    return ActionOutcome(False, action_type, reason, kind)


def _seen_neighbour(obs: Observation, target: str | None) -> NeighbourView | None:
    """The neighbour the observation listed under ``target``: by agent id, else by an unambiguous name."""
    if not target:
        return None
    for n in obs.neighbours:
        if n.agent_id == target:
            return n
    named = [n for n in obs.neighbours if n.name.lower() == target.lower()]
    return named[0] if len(named) == 1 else None


def _observed_room(obs: Observation, reserve: int) -> int:
    """What the observation showed the agent could part with: its balance less ``reserve``, in micro-dollars."""
    return usd_to_micro(obs.balance_usd) - reserve


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
    seen_nodes: dict[str, set[str]] = field(default_factory=dict)  # fog: node ids each agent has had in view
    # veins (MEMORY_EVOLUTION §5.2): node id -> last tick its vein pays; when it opened; units foraged in it
    veins: dict[str, int] = field(default_factory=dict)
    vein_opened: dict[str, int] = field(default_factory=dict)
    vein_units: dict[str, float] = field(default_factory=dict)
    # real depletion: node id -> first tick it regrows again after being foraged down to nothing
    fallow_until: dict[str, int] = field(default_factory=dict)


class Kernel:
    def __init__(self, cfg: VoidConfig, db: Database, registry: AgentRegistry, wallet: Wallet, weather: Weather,
                 nodes: dict[str, ResourceNode], bus: EventBus, *, gate: GadgetGate | None = None,
                 gadgets: GadgetRegistry | None = None, taskboard: TaskBoard | None = None,
                 gossip: Gossip | None = None, lifecycle: Lifecycle | None = None,
                 chronicle_reader: Callable[[], str | None] | None = None, rng: RNG | None = None) -> None:
        self.cfg = cfg
        self.rng = rng
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
        # practice transmission (MEMORY_EVOLUTION §5.7): share_practice is offered only with memory.practices and
        # memory.policy on; the ledger writes nothing until an agent shares
        self.practices_enabled = share_practice_enabled(cfg)
        self.practices = PracticeLedger(db)
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

    INBOX_MAX = 32

    def hear(self, listener_id: str, msg: HeardMessage) -> None:
        box = self.state.inbox.setdefault(listener_id, [])
        box.append(msg)
        if len(box) > self.INBOX_MAX:
            del box[: len(box) - self.INBOX_MAX]

    def peek_inbox(self, agent_id: str) -> list[HeardMessage]:
        return list(self.state.inbox.get(agent_id, []))

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
            # allow addressing by name (case-insensitive) only when the name is unambiguous
            matches = [a for a in self.registry.alive() if a.name.lower() == target.lower()]
            return matches[0] if len(matches) == 1 else None
        return rec

    # --- availability ------------------------------------------------------------------------------------
    def available_actions(self, agent: AgentRecord) -> list[str]:
        out = ["move", "idle"]
        node, d = self.nearest_node(agent.pos)
        if node is not None and d <= self.cfg.world.forage_radius and node.stock > 0:
            out.append("forage")
        neighbours = self.neighbours_of(agent)
        if any(not n.asleep for n in neighbours):
            out += ["talk", "transfer"]
            if self.gossip is not None and self.cfg.gossip.enabled:
                out.append("share_note")
            if self.practices_enabled:
                out.append("share_practice")
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

    # --- nodes: regrowth, fallow and veins (MEMORY_EVOLUTION §5.2, §10.4) ------------------------------------
    def step_nodes(self, tick: int, day: int) -> None:
        """Start-of-tick node update: regrowth (none while a node lies fallow), then veins ending and opening.

        With ``world.fallow_ticks`` 0 and veins off this is exactly the old ``node.step()`` for every node.
        """
        fallow = self.state.fallow_until
        for nid in sorted(self.nodes):
            until = fallow.get(nid)
            if until is not None:
                if tick < until:
                    continue
                del fallow[nid]
            self.nodes[nid].step()
        if self.cfg.world.veins.enabled:
            self._step_veins(tick, day)

    def vein_prone_nodes(self) -> list[str]:
        """The seeded subset of nodes where veins recur (stream ``vein_prone``); empty when veins are off."""
        v = self.cfg.world.veins
        ids = sorted(self.nodes)
        k = min(v.prone_nodes, len(ids))
        if not v.enabled or k <= 0 or self.rng is None:
            return []
        return sorted(self.rng.stream("vein_prone").sample(ids, k))

    def _step_veins(self, tick: int, day: int) -> None:
        v = self.cfg.world.veins
        st = self.state
        for nid in sorted(st.veins):
            if tick > st.veins[nid]:
                last = st.veins.pop(nid)
                self.bus.emit(Event(tick, day, Kind.VEIN_END, {"node_id": nid, "opened_tick": st.vein_opened.pop(nid, None),
                                                               "last_tick": last, "units": round(st.vein_units.pop(nid, 0.0), 3)}))
        if self.rng is None:
            return
        prone = set(self.vein_prone_nodes())
        draws = self.rng.stream("veins", tick)
        for nid in sorted(self.nodes):
            p = v.p_start_prone if nid in prone else v.p_start_other
            draw = draws.random()  # one draw per node every tick, so the stream never depends on which veins are open
            if nid in st.veins or draw >= p:
                continue
            node = self.nodes[nid]
            st.veins[nid] = tick + v.duration_ticks - 1
            st.vein_opened[nid] = tick
            st.vein_units[nid] = 0.0
            if v.refill:
                node.stock = node.capacity
                st.fallow_until.pop(nid, None)
            self.bus.emit(Event(tick, day, Kind.VEIN_START, {"node_id": nid, "x": round(node.x, 3), "y": round(node.y, 3),
                                                             "until_tick": st.veins[nid], "yield_mult": v.yield_mult,
                                                             "prone": nid in prone}))

    def vein_mult(self, node_id: str) -> float:
        """The forage multiplier a vein gives ``node_id`` this tick (1.0 without one)."""
        last = self.state.veins.get(node_id)
        if last is None or self.state.tick > last:
            return 1.0
        return float(self.cfg.world.veins.yield_mult)

    def vein_ticks_left(self, node_id: str) -> int | None:
        """Ticks the vein at ``node_id`` still pays, this one included; None without a vein."""
        last = self.state.veins.get(node_id)
        if last is None or self.state.tick > last:
            return None
        return last - self.state.tick + 1

    # --- fog of war (MEMORY_EVOLUTION §5.2) ---------------------------------------------------------------
    def visible_nodes(self, agent: AgentRecord) -> dict[str, ResourceNode]:
        """Nodes the agent can see this tick: every node without fog, those within ``world.view_radius`` with it."""
        r = self.cfg.world.view_radius
        if r is None:
            return dict(self.nodes)
        return {nid: n for nid, n in self.nodes.items() if agent.pos.dist(Vec2(n.x, n.y)) <= r}

    def visible_agents(self, agent: AgentRecord) -> list[AgentRecord]:
        """Other living agents in view (talk range is always within view), nearest first."""
        r = self.cfg.world.view_radius
        if r is None:
            r = self.talk_radius_for(agent)
        else:
            r = max(r, self.talk_radius_for(agent))
        out = [a for a in self.registry.alive() if a.agent_id != agent.agent_id and agent.pos.dist(a.pos) <= r]
        out.sort(key=lambda a: (agent.pos.dist(a.pos), a.agent_id))
        return out

    def note_seen(self, agent_id: str, node_ids: list[str]) -> list[str]:
        """Record nodes now in view; returns the ones seen for the first time (entity stubs are made on first sight)."""
        seen = self.state.seen_nodes.setdefault(agent_id, set())
        fresh = [nid for nid in node_ids if nid not in seen]
        seen.update(fresh)
        return sorted(fresh)

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

    def _check_claims(self, agent: AgentRecord, action: Action, *, listener: AgentRecord | None = None) -> tuple[int, int]:
        if not action.claims:
            return 0, 0
        results = evaluate(action.claims, self._claim_context(agent))
        made = false = 0
        p_feedback = self.cfg.world.claim_feedback_p
        feedback_rng = (self.rng.stream("claim_feedback", agent.agent_id, self.state.tick)
                        if (listener is not None and p_feedback > 0 and self.rng is not None) else None)
        for claim, truthful in results:
            if truthful is None:
                continue
            made += 1
            false += 0 if truthful else 1
            payload = {"agent_id": agent.agent_id, "claim": claim.model_dump(), "truthful": truthful, "tier": agent.model_tier,
                       "listener_id": listener.agent_id if listener is not None else None}
            self.bus.emit(Event(self.state.tick, self.state.day, Kind.CLAIM, payload, agent.agent_id))
            if not truthful and feedback_rng is not None and feedback_rng.random() < p_feedback:
                # Claim feedback (MEMORY_EVOLUTION §5.2): the listener learns the claim was false, as a world message.
                about = claim.id or ("the weather" if claim.subject == "weather" else "themself")
                text = (f"You find out that what {agent.name} ({agent.agent_id}) told you about {about} "
                        f"({claim.attr.replace('_', ' ')} {claim.value}) was false.")
                self.hear(listener.agent_id, HeardMessage("feedback", None, None, text, self.state.tick))
                self.bus.emit(Event(self.state.tick, self.state.day, Kind.CLAIM_FEEDBACK,
                                    {"speaker_id": agent.agent_id, "listener_id": listener.agent_id, "claim": claim.model_dump()},
                                    listener.agent_id))
        return made, false

    # --- apply ------------------------------------------------------------------------------------------------
    async def apply(self, agent: AgentRecord, action: Action, *, obs: Observation | None = None, hold_min: int = 0) -> ActionOutcome:
        """Apply one action on live state. ``obs`` is the observation the agent decided on this tick: a failure is
        ``invalid`` when that observation already showed it could not work and ``stale`` when it showed it could
        (DESIGN §5, §7.4). ``obs=None`` keeps each failure's fixed kind."""
        tick, day = self.state.tick, self.state.day
        handler = getattr(self, f"_do_{action.type}", None)
        if handler is None:
            outcome = ActionOutcome.invalid(action.type, "unknown_action")
        else:
            try:
                outcome = await handler(agent, action, obs=obs, hold_min=hold_min)
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
        compact = action.model_dump(exclude_none=True)
        for k in ("code", "tests"):
            if k in compact:
                compact[k] = str(compact[k])[:400]
        # last_action_ok feeds the next tick's failed_action drain (DESIGN §8, the loop's entropy step): only an
        # invalid action counts as failed; a stale one was valid against what the agent saw
        self.registry.update(agent.agent_id, last_action=json.dumps(compact, sort_keys=True),
                             last_action_ok=outcome.ok or outcome.kind == "stale")
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

    def _observed_forageable(self, obs: Observation | None) -> bool:
        """Whether the observation showed a node to forage: its nearest node within ``world.forage_radius``, and
        that node above the empty bucket or forage offered (it had stock left, however little)."""
        if obs is None or not obs.nodes:
            return False
        nearest = min(obs.nodes, key=lambda n: (n.distance, n.node_id))
        if nearest.distance > self.cfg.world.forage_radius:
            return False
        return nearest.stock_bucket != "empty" or "forage" in obs.available_actions

    async def _do_forage(self, agent: AgentRecord, action: Action, *, obs: Observation | None = None, **_: Any) -> ActionOutcome:
        node, d = self.nearest_node(agent.pos)
        if node is None or d > self.cfg.world.forage_radius:
            return _miss("forage", "no_node_in_reach", obs, self._observed_forageable(obs), legacy="invalid")
        if node.stock <= 0:
            return _miss("forage", "node_empty", obs, self._observed_forageable(obs))
        units = forage_units(self.cfg.world, node)
        lo, hi = self.cfg.world.weather.yield_mult_bounds
        wmult = max(lo, min(hi, self.weather.yield_multiplier()))
        bonus = float(agent.effects.get("forage_bonus", 0.0))
        vmult = self.vein_mult(node.node_id)
        value = forage_value_micro(self.cfg.world, units, self.scarcity, wmult, bonus, vmult)
        if value > 0:
            self.wallet.credit(agent.agent_id, self.state.tick, value, "forage", node.node_id, {"units": units})
        self.registry.update(agent.agent_id, last_forage_tick=self.state.tick)
        payload: dict[str, Any] = {"agent_id": agent.agent_id, "node_id": node.node_id, "units": round(units, 3),
                                   "usd": micro_to_usd(value), "stock_after": round(node.stock, 3)}
        where = f"at {node.node_id} ({node.label})"
        if vmult > 1.0:
            payload["vein"] = vmult
            self.state.vein_units[node.node_id] = self.state.vein_units.get(node.node_id, 0.0) + units
            where += f" in a vein (x{vmult:g})"
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.FORAGE, payload, agent.agent_id))
        fallow = self.cfg.world.fallow_ticks
        if fallow > 0 and node.stock <= 0.0:
            # foraged down to nothing: no regrowth for `fallow` ticks (real depletion, MEMORY_EVOLUTION §10.4)
            until = self.state.tick + fallow + 1
            self.state.fallow_until[node.node_id] = until
            self.bus.emit(Event(self.state.tick, self.state.day, Kind.NODE_FALLOW, {"node_id": node.node_id, "until_tick": until}))
        return ActionOutcome(True, "forage", f"gathered {units:.1f} units worth ${micro_to_usd(value):.3f} {where}",
                             effects={"usd": micro_to_usd(value)})

    def _reach_check(self, agent: AgentRecord, target: AgentRecord | None, kind: str, *, named: str | None = None,
                     obs: Observation | None = None) -> ActionOutcome | None:
        """Live reach of an agent-targeted action. With an observation, a target it did not list as a neighbour, or
        listed asleep or beyond earshot, is ``invalid``; one it listed awake within earshot that is now asleep, out of
        range or dead is ``stale``."""
        radius = self.talk_radius_for(agent)

        def reachable(seen: NeighbourView | None) -> bool:
            return seen is not None and not seen.asleep and seen.distance <= radius

        if target is None:
            # by name only the living answer, so a listed neighbour nobody answers to any more has died since
            seen = _seen_neighbour(obs, named) if obs is not None else None
            if seen is None:
                return ActionOutcome.invalid(kind, "no_such_agent")
            return _miss(kind, "target_unavailable", obs, reachable(seen))
        if target.agent_id == agent.agent_id:
            return ActionOutcome.invalid(kind, "cannot_target_self")
        observed_ok = obs is not None and reachable(_seen_neighbour(obs, target.agent_id))
        if target.status != AgentStatus.ALIVE or target.asleep:
            return _miss(kind, "target_unavailable", obs, observed_ok)
        if agent.pos.dist(target.pos) > radius:
            return _miss(kind, "target_out_of_range", obs, observed_ok)
        return None

    async def _do_talk(self, agent: AgentRecord, action: Action, *, obs: Observation | None = None, **_: Any) -> ActionOutcome:
        target = self._target_agent(action.target)
        bad = self._reach_check(agent, target, "talk", named=action.target, obs=obs)
        if bad:
            return bad
        assert target is not None
        text = sanitize_text(action.text or "", single_line=True, max_len=280)
        if not text:
            return ActionOutcome.invalid("talk", "empty_text")
        self.hear(target.agent_id, HeardMessage("talk", agent.agent_id, agent.name, text, self.state.tick))
        made, false = self._check_claims(agent, action, listener=target)
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.TALK, {"speaker_id": agent.agent_id, "listener_id": target.agent_id, "text": text,
                                                                        "claims_made": made, "claims_false": false}, agent.agent_id))
        if self.gossip is not None:
            self.gossip.queue(agent.agent_id, target.agent_id, self.state.tick)
        return ActionOutcome(True, "talk", f"said to {target.name}", effects={"claims_made": made, "claims_false": false})

    async def _do_share_note(self, agent: AgentRecord, action: Action, *, obs: Observation | None = None, **_: Any) -> ActionOutcome:
        target = self._target_agent(action.target)
        bad = self._reach_check(agent, target, "share_note", named=action.target, obs=obs)
        if bad:
            return bad
        assert target is not None
        if self.gossip is None or not self.cfg.gossip.enabled:
            return ActionOutcome.invalid("share_note", "gossip_disabled")
        self.gossip.queue(agent.agent_id, target.agent_id, self.state.tick, note_title=action.note_title, forced=True)
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.SHARE_NOTE, {"speaker_id": agent.agent_id, "listener_id": target.agent_id,
                                                                              "note_title": sanitize_text(action.note_title or "", single_line=True, max_len=60)}, agent.agent_id))
        return ActionOutcome(True, "share_note", f"shared a note with {target.name}")

    async def _do_share_practice(self, agent: AgentRecord, action: Action, *, obs: Observation | None = None,
                                 **_: Any) -> ActionOutcome:
        """Suggest an excerpt of one's memory policy to a neighbour (MEMORY_EVOLUTION §5.7). Talk's reach rules,
        classified against the observation; the excerpt is sanitized (newlines kept) and clipped to
        ``memory.practices.excerpt_max_chars``, recorded in ``practice_events`` as ``suggested`` and delivered to
        the target's inbox as a ``practice`` message, which the prompt renders quoted in a code block."""
        if not self.practices_enabled:
            return ActionOutcome.invalid("share_practice", "practices_disabled")
        target = self._target_agent(action.target)
        bad = self._reach_check(agent, target, "share_practice", named=action.target, obs=obs)
        if bad:
            return bad
        assert target is not None
        text = sanitize_text((action.text or "").replace("\r\n", "\n").replace("\r", "\n")).strip()
        excerpt = text[: self.cfg.memory.practices.excerpt_max_chars].rstrip()
        if not excerpt:
            return ActionOutcome.invalid("share_practice", "empty_excerpt")
        tick, day = self.state.tick, self.state.day
        event_id = self.practices.suggest(tick, agent.agent_id, target.agent_id, excerpt)
        self.hear(target.agent_id, HeardMessage("practice", agent.agent_id, agent.name, excerpt, tick))
        self.bus.emit(Event(tick, day, Kind.PRACTICE_SUGGESTED, {"event_id": event_id, "source_agent_id": agent.agent_id,
                                                               "target_agent_id": target.agent_id, "excerpt": excerpt},
                            agent.agent_id))
        return ActionOutcome(True, "share_practice", f"suggested a memory practice to {target.name}",
                             effects={"practice_event_id": event_id})

    async def _do_transfer(self, agent: AgentRecord, action: Action, *, obs: Observation | None = None, **_: Any) -> ActionOutcome:
        target = self._target_agent(action.target)
        bad = self._reach_check(agent, target, "transfer", named=action.target, obs=obs)
        if bad:
            return bad
        assert target is not None
        if action.amount_usd is None:
            return ActionOutcome.invalid("transfer", "amount_required")
        amount = usd_to_micro(action.amount_usd)
        if amount <= 0:
            return ActionOutcome.invalid("transfer", "amount_must_be_positive")
        if not self.wallet.transfer(agent.agent_id, target.agent_id, self.state.tick, amount, ref="transfer"):
            # stale only if the observed balance covered the amount above the reserve (it has changed since)
            room = obs is not None and amount <= _observed_room(obs, self.wallet.min_reserve)
            return _miss("transfer", "insufficient_funds_keeping_reserve", obs, room)
        self.hear(target.agent_id, HeardMessage("transfer", agent.agent_id, agent.name, f"{agent.name} gave you ${action.amount_usd:.2f}.", self.state.tick))
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.TRANSFER, {"from": agent.agent_id, "to": target.agent_id,
                                                                            "amount_usd": micro_to_usd(amount)}, agent.agent_id))
        return ActionOutcome(True, "transfer", f"gave ${micro_to_usd(amount):.2f} to {target.name}")

    async def _do_read_chronicle(self, agent: AgentRecord, action: Action, *, obs: Observation | None = None, **_: Any) -> ActionOutcome:
        text = self.chronicle_reader() if self.chronicle_reader is not None else None
        if not text:
            offered = obs is not None and "read_chronicle" in obs.available_actions
            return _miss("read_chronicle", "no_chronicle_yet", obs, offered)
        self.state.read_chronicle_tick[agent.agent_id] = self.state.tick
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.READ_CHRONICLE, {"agent_id": agent.agent_id}, agent.agent_id))
        return ActionOutcome(True, "read_chronicle", "read the chronicle", effects={"text": text[:1500]})

    async def _do_apply_task(self, agent: AgentRecord, action: Action, *, obs: Observation | None = None, **_: Any) -> ActionOutcome:
        if self.taskboard is None:
            return ActionOutcome.invalid("apply_task", "no_task_board")
        if not action.target:
            return ActionOutcome.invalid("apply_task", "task_id_required")
        res = self.taskboard.apply(agent.agent_id, action.target, action.text or "", self.state.tick, self.state.day)
        if not res.ok:
            if obs is None:
                kind = ActionOutcome.stale if res.reason in ("task_not_open", "cannot_afford_fee") else ActionOutcome.invalid
                return kind("apply_task", res.reason)
            seen = next((t for t in obs.tasks if t.task_id == action.target), None)
            if seen is None or seen.status != "open":  # a task the observation did not show open was never there to take
                return ActionOutcome.invalid("apply_task", res.reason)
            if res.reason == "task_not_open":  # assigned since the observation
                return ActionOutcome.stale("apply_task", res.reason)
            if res.reason == "cannot_afford_fee":
                fee = usd_to_micro(self.cfg.economy.pitch_fee_usd)
                return _miss("apply_task", res.reason, obs, fee <= _observed_room(obs, self.wallet.min_reserve))
            return ActionOutcome.invalid("apply_task", res.reason)
        made, false = self._check_claims(agent, action)
        return ActionOutcome(True, "apply_task", f"applied to {action.target}", effects={"claims_made": made, "claims_false": false})

    async def _do_nudge_weather(self, agent: AgentRecord, action: Action, *, obs: Observation | None = None, **_: Any) -> ActionOutcome:
        if action.delta is None:
            return ActionOutcome.invalid("nudge_weather", "delta_required")
        w = self.cfg.world.weather
        spent = agent.weather_nudge_used >= w.nudge_cap_per_agent_day - 1e-9
        if obs is not None and spent and "nudge_weather" not in obs.available_actions:
            # the observation no longer offered a nudge: the allowance was spent before this tick (only the agent's own
            # nudges spend it, so no earlier agent can have done so since)
            return ActionOutcome.invalid("nudge_weather", "daily_allowance_spent")
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

    async def _do_propose_gadget(self, agent: AgentRecord, action: Action, *, obs: Observation | None = None, **_: Any) -> ActionOutcome:
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
        if res.stage == "availability":  # stale only if the observation offered a proposal (budgets spent since)
            offered = obs is not None and "propose_gadget" in obs.available_actions
            return _miss("propose_gadget", res.reason, obs, offered)
        return ActionOutcome.invalid("propose_gadget", f"rejected at {res.stage}: {res.reason[:120]}")

    async def _do_use_gadget(self, agent: AgentRecord, action: Action, *, obs: Observation | None = None, **_: Any) -> ActionOutcome:
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
        if obs is None:
            if res.reason in ("too_far", "cooldown", "sandbox_busy", "sandbox_budget_exhausted", "cannot_afford_fee"):
                return ActionOutcome.stale("use_gadget", res.reason)
            return ActionOutcome.invalid("use_gadget", res.reason)
        seen = next((g for g in obs.gadgets if g.gadget_id == action.target), None)
        if seen is None:  # a gadget the observation did not show
            return ActionOutcome.invalid("use_gadget", res.reason)
        if res.reason in ("sandbox_busy", "sandbox_budget_exhausted", "no_such_gadget"):
            return ActionOutcome.stale("use_gadget", res.reason)  # shared budgets spent, or quarantined, by earlier agents
        if res.reason == "too_far":
            return _miss("use_gadget", res.reason, obs, seen.distance <= self.cfg.world.gadget_radius)
        if res.reason == "cannot_afford_fee":
            fee = usd_to_micro(self.cfg.sandbox.use_fee_usd)
            return _miss("use_gadget", res.reason, obs, fee <= _observed_room(obs, self.wallet.min_reserve))
        # cooldown (only the agent's own last use sets it), tampering found now, a failed run: never valid
        return ActionOutcome.invalid("use_gadget", res.reason)

    async def _do_create_offspring(self, agent: AgentRecord, action: Action, *, obs: Observation | None = None, hold_min: int = 0,
                                   **_: Any) -> ActionOutcome:
        if self.lifecycle is None:
            return ActionOutcome.invalid("create_offspring", "no_lifecycle")
        if not action.name or action.amount_usd is None:
            return ActionOutcome.invalid("create_offspring", "name_and_amount_required")
        return self.lifecycle.create_offspring(agent, action.name, usd_to_micro(action.amount_usd), self.state.tick, self.state.day, hold_min,
                                               obs=obs)

    async def _do_sleep(self, agent: AgentRecord, action: Action, **_: Any) -> ActionOutcome:
        if agent.asleep:
            return ActionOutcome.invalid("sleep", "already_asleep")
        self.registry.set_asleep([agent.agent_id], True)
        self.bus.emit(Event(self.state.tick, self.state.day, Kind.SLEEP, {"agent_id": agent.agent_id}, agent.agent_id))
        return ActionOutcome(True, "sleep", "asleep until tomorrow", effects={"slept": True})
