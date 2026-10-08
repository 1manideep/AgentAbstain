"""Build an agent's observation from live state (DESIGN §5)."""

from __future__ import annotations

from dataclasses import dataclass

from void.agents.models import AgentRecord
from void.agents.registry import AgentRegistry
from void.brain.prompt import estimate_tokens, render_observation
from void.config import VoidConfig, micro_to_usd
from void.db import Database
from void.economy.scheduler import Clock
from void.economy.taskboard import TaskBoard
from void.memory.policy import PolicyStore
from void.memory.store import MemoryStore
from void.sandbox.registry import GadgetRegistry
from void.types import (
    GadgetView,
    HeardMessage,
    NeighbourView,
    NodeView,
    Observation,
    Vec2,
    balance_bucket,
    stress_label,
)
from void.world.kernel import Kernel
from void.world.resources import ResourceNode

__all__ = ["ObservationBuilder"]

HEARD_MAX = 8
HEARD_CHARS = 1500


@dataclass
class ObservationBuilder:
    cfg: VoidConfig
    db: Database
    registry: AgentRegistry
    kernel: Kernel
    memory: MemoryStore
    nodes: dict[str, ResourceNode]
    gadgets: GadgetRegistry | None = None
    taskboard: TaskBoard | None = None
    policy: PolicyStore | None = None

    def burn_rate(self, agent_id: str) -> float:
        rows = self.db.fetchall("SELECT world_cost FROM llm_calls WHERE agent_id=? AND purpose='decide' ORDER BY tick DESC LIMIT 6", (agent_id,))
        if not rows:
            return 0.0
        return micro_to_usd(sum(int(r["world_cost"]) for r in rows) / len(rows))

    def _policy_text(self, agent_id: str) -> str | None:
        if self.policy is None or not self.cfg.memory.policy.enabled or not self.cfg.memory.policy.show_in_prompt:
            return None
        text = self.policy.text(agent_id)
        return "" if text is None else text

    def _heard(self, agent_id: str) -> list[HeardMessage]:
        msgs = self.kernel.peek_inbox(agent_id)
        msgs = list(reversed(msgs))[:HEARD_MAX]  # newest first
        out, used = [], 0
        for m in msgs:
            text = m.text[: max(0, HEARD_CHARS - used)]
            if not text:
                break
            used += len(text)
            out.append(HeardMessage(m.kind, m.from_agent_id, m.from_name, text, m.tick))
        return out

    def build(self, agent: AgentRecord, clock: Clock, chronicle_headline: str | None, stress: float, *, retrieve: bool = True) -> Observation:
        pos = agent.pos
        fog = self.cfg.world.view_radius is not None
        neighbours = [NeighbourView(a.agent_id, a.name, a.model_tier, round(pos.dist(a.pos), 2), stress_label(a.stress), a.asleep)
                      for a in (self.kernel.visible_agents(agent) if fog else self.kernel.neighbours_of(agent))]
        visible = self.kernel.visible_nodes(agent) if fog else self.nodes
        nodes = sorted((NodeView(nid, round(pos.dist(Vec2(n.x, n.y)), 2), n.label, round(n.x, 1), round(n.y, 1)) for nid, n in visible.items()),
                       key=lambda v: (v.distance, v.node_id))
        gadgets: list[GadgetView] = []
        if self.gadgets is not None:
            for g in self.gadgets.verified():
                if g.x is None or g.y is None:
                    continue
                d = pos.dist(Vec2(float(g.x), float(g.y)))
                if d <= self.cfg.world.gadget_radius * 4:
                    gadgets.append(GadgetView(g.gadget_id, g.name, g.owner_agent_id, g.purpose, round(d, 2)))
            gadgets.sort(key=lambda v: (v.distance, v.gadget_id))
        tasks = self.taskboard.view_for(agent.agent_id) if self.taskboard is not None else []
        heard = self._heard(agent.agent_id)
        self_summary = self.memory.get_self(agent.agent_id)
        memories = []
        if retrieve and self.cfg.memory.enabled:  # the amnesic control retrieves nothing (MEMORY_EVOLUTION §5.2)
            query_bits = [self_summary] + [h.text for h in heard[:3]] + [f"Node {n.node_id} {n.stock_bucket}" for n in nodes[:2]]
            query = " ".join(b for b in query_bits if b)
            memories = self.memory.retrieve(agent.agent_id, query, clock.tick) if query.strip() else []
        obs = Observation(
            tick=clock.tick, day=clock.day, tick_of_day=clock.tick_of_day, ticks_left_today=clock.ticks_left_today,
            agent_id=agent.agent_id, name=agent.name, tier=agent.model_tier, generation=agent.generation, position=pos,
            balance_usd=micro_to_usd(agent.balance), burn_rate_usd_per_tick=self.burn_rate(agent.agent_id),
            stress=stress, stress_label=stress_label(stress), weather=self.kernel.weather.value, weather_label=self.kernel.weather.label,
            scarcity_label=self.kernel.scarcity_label(), neighbours=neighbours, nodes=nodes, gadgets=gadgets, tasks=tasks, heard=heard,
            self_summary=self_summary, memories=memories, chronicle_headline=chronicle_headline,
            last_action_result=self.kernel.state.last_result.get(agent.agent_id), available_actions=self.kernel.available_actions(agent),
            world_size=self.cfg.world.size, population=self.registry.count_alive(), population_cap=self.cfg.population.cap,
            active_effects=dict(agent.effects), balance_bucket=balance_bucket(micro_to_usd(agent.balance), self.cfg.population.starting_balance_usd),
            policy_text=self._policy_text(agent.agent_id), nodes_in_view=len(nodes) if fog else None,
        )
        obs.prompt_tokens = estimate_tokens(render_observation(obs))
        return obs
