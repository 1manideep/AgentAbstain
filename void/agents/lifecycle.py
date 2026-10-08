"""Births, deaths, replacement and inheritance (DESIGN §13). Single end-of-tick evaluation."""

from __future__ import annotations

import math
from pathlib import Path

from void.agents.models import AgentRecord, PersonalitySeed
from void.agents.registry import AgentRegistry
from void.config import VoidConfig, micro_to_usd, usd_to_micro
from void.db import Database
from void.economy.wallet import Wallet
from void.events import Event, EventBus, Kind
from void.ids import IdFactory
from void.memory.notes import Provenance
from void.memory.policy import PolicyStore
from void.memory.store import MemoryStore
from void.rng import RNG
from void.types import ActionOutcome, AgentStatus, Vec2

__all__ = ["Lifecycle"]


def _prov(**fields: object) -> Provenance:
    import dataclasses

    names = {f.name for f in dataclasses.fields(Provenance)}
    return Provenance(**{k: v for k, v in fields.items() if k in names})



def mutation_candidates(cfg: VoidConfig, tier: str) -> list[str]:
    """Tiers a child of ``tier`` may mutate onto: one rung up or down on the capability ladder when the
    parent is on it, otherwise any other tier; never across a provider boundary (credentials and the
    no-network test suite both depend on that)."""
    provider = cfg.tiers[tier].provider
    ladder = cfg.intelligence.ladder
    if tier in ladder:
        i = ladder.index(tier)
        return [ladder[j] for j in (i - 1, i + 1) if 0 <= j < len(ladder) and cfg.tiers[ladder[j]].provider == provider]
    return sorted(t for t, c in cfg.tiers.items() if t != tier and c.provider == provider)


class Lifecycle:
    def __init__(self, cfg: VoidConfig, db: Database, registry: AgentRegistry, wallet: Wallet, memory: MemoryStore,
                 bus: EventBus, ids: IdFactory, rng: RNG, vault_root: Path, graveyard_root: Path,
                 policy: PolicyStore | None = None) -> None:
        self.cfg = cfg
        self.policy = policy
        self.db = db
        self.registry = registry
        self.wallet = wallet
        self.memory = memory
        self.bus = bus
        self.ids = ids
        self.rng = rng
        self.vault_root = Path(vault_root)
        self.graveyard_root = Path(graveyard_root)
        self.population = registry.count_alive()
        self.pending_bankrupt: dict[str, str] = {}  # agent_id -> cause flagged during the tick (overrun, gate)
        self.pending_archive: list[str] = []  # bankrupt rows whose vault moves to the graveyard after the tick commits

    # --- helpers ---------------------------------------------------------------------------------
    def _spawn_position(self, near: Vec2 | None, key: str) -> Vec2:
        r = self.rng.stream("spawn_pos", key)
        half = self.cfg.world.size / 2.0
        if near is None:
            return Vec2(r.uniform(-half * 0.5, half * 0.5), r.uniform(-half * 0.5, half * 0.5))
        theta = r.uniform(0, 2 * math.pi)
        return Vec2(max(-half, min(half, near.x + 1.5 * math.cos(theta))), max(-half, min(half, near.y + 1.5 * math.sin(theta))))

    def _new_record(self, name: str, tier: str, balance: int, seed: PersonalitySeed, pos: Vec2, tick: int,
                    generation: int, parent_id: str | None) -> AgentRecord:
        aid = self.ids.new("ag")
        return AgentRecord(
            agent_id=aid, name=name, model_tier=tier, balance=0, seed=seed, memory_path=str(self.vault_root / aid),
            generation=generation, status=AgentStatus.ALIVE, pos=pos, heading=0.0,
            entropy_budget=self.cfg.tiers[tier].entropy_budget_max, stress=0.0, asleep=False, born_tick=tick,
            parent_id=parent_id, last_forage_tick=None,
        )

    def _insert(self, rec: AgentRecord, tick: int, funding: tuple[str, int, str, str] | None) -> AgentRecord:
        """Insert the row with balance 0, then fund it via the ledger so money is conserved."""
        with self.db.tx():
            self.registry.insert(rec, created_at=f"tick:{tick}")
            if funding is not None:
                source, amount, kind_out, kind_in = funding
                if source == "external":
                    self.wallet.credit(rec.agent_id, tick, amount, kind_in, "spawn")
                else:
                    ok = self.wallet.transfer(source, rec.agent_id, tick, amount, kind_out, kind_in, ref="spawn", keep_reserve=False)
                    if not ok:
                        raise RuntimeError(f"funding failed from {source}")
        self.population += 1
        return self.registry.get(rec.agent_id) or rec

    def _init_vault(self, rec: AgentRecord, tick: int, summary: str, *, roster_index: int = 0, parent_id: str | None = None) -> None:
        self.memory.vault_for(rec.agent_id).ensure()
        self.memory.revise_self(rec.agent_id, tick, summary)
        if self.policy is not None:  # seed or inherit the memory policy (MEMORY_EVOLUTION §5.5, §5.7)
            self.policy.init_for(rec.agent_id, tick, roster_index=roster_index, parent_id=parent_id)

    def _unique_name(self, base: str) -> str:
        """A name no living agent holds (case-insensitive), within the 20-character limit."""
        taken = {a.name.lower() for a in self.registry.alive()}
        if base.lower() not in taken:
            return base[:20]
        n = 2
        while True:
            suffix = f"-{n}"
            cand = base[: 20 - len(suffix)] + suffix
            if cand.lower() not in taken:
                return cand
            n += 1

    # --- spawns ------------------------------------------------------------------------------------
    def spawn_initial(self, tick: int) -> list[AgentRecord]:
        out: list[AgentRecord] = []
        for i, spec in enumerate(self.cfg.agents):
            seed = PersonalitySeed.generate(self.rng.stream("seed", "initial", i), spec.personality)
            rec = self._new_record(spec.name, spec.tier, 0, seed, self._spawn_position(None, f"initial:{i}"), tick, 0, None)
            rec = self._insert(rec, tick, ("external", usd_to_micro(self.cfg.population.starting_balance_usd), "", "starting_balance"))
            self._init_vault(rec, tick, f"I am {rec.name}. {seed.motto} I have just arrived in the void and know nothing yet.", roster_index=i)
            self.bus.emit(Event(tick, 1, Kind.BIRTH, {"agent_id": rec.agent_id, "name": rec.name, "tier": rec.model_tier, "generation": 0,
                                                    "parent_id": None, "kind": "fresh", "endowment_usd": self.cfg.population.starting_balance_usd}, rec.agent_id))
            out.append(rec)
        return out

    def spawn_arrival(self, name: str, tier: str, balance_usd: float, tick: int, day: int) -> AgentRecord | None:
        if self.population >= self.cfg.population.cap or tier not in self.cfg.tiers:
            return None
        seed = PersonalitySeed.generate(self.rng.stream("seed", "arrival", tick, name))
        rec = self._new_record(name[:20], tier, 0, seed, self._spawn_position(None, f"arrival:{tick}"), tick, 0, None)
        rec = self._insert(rec, tick, ("external", usd_to_micro(balance_usd), "", "arrival_grant"))
        self._init_vault(rec, tick, f"I am {rec.name}, newly arrived from beyond the edge. {seed.motto}")
        self.bus.emit(Event(tick, day, Kind.BIRTH, {"agent_id": rec.agent_id, "name": rec.name, "tier": rec.model_tier, "generation": 0,
                                                  "parent_id": None, "kind": "arrival", "endowment_usd": balance_usd}, rec.agent_id))
        return rec

    def _child_of(self, parent: AgentRecord, name: str, tick: int, day: int, funding: tuple[str, int, str, str], kind: str) -> AgentRecord:
        r = self.rng.stream("mutate", parent.agent_id, tick)
        tier = parent.model_tier
        others = mutation_candidates(self.cfg, tier)
        if others and r.random() < self.cfg.population.tier_mutation_prob:
            tier = r.choice(others)
        parent_self = self.memory.get_self(parent.agent_id)
        seed = parent.seed.mutate(r, motto_from=parent_self or None)
        rec = self._new_record(name, tier, 0, seed, self._spawn_position(parent.pos, f"child:{parent.agent_id}:{tick}"), tick,
                               parent.generation + 1, parent.agent_id)
        rec = self._insert(rec, tick, funding)
        first = (parent_self.strip().split(".")[0].strip() + ".") if parent_self.strip() else parent.seed.motto
        self._init_vault(rec, tick, f"Child of {parent.name}. {first}", parent_id=parent.agent_id)
        for note in self.memory.top_notes(parent.agent_id, self.cfg.memory.inherit_top_k, exclude_tags=("entity", "self", "probe")):
            origin = note.provenance.origin_note_id or note.note_id
            self.memory.copy_note(note, rec.agent_id, tick, provenance=_prov(
                channel="inherited", source_agent_id=parent.agent_id, hop=note.provenance.hop, origin_note_id=origin,
                origin_generation=note.provenance.origin_generation if note.provenance.origin_generation is not None else parent.generation,
                transfer_tick=tick, path_agents=list(getattr(note.provenance, "path_agents", []) or []) + [parent.agent_id],
            ), tags=list(note.tags) + ["inherited"])
        # remaining weather allowance is inherited (kernel-owned per-agent counter)
        self.registry.update(rec.agent_id, weather_nudge_used=parent.weather_nudge_used)
        self.bus.emit(Event(tick, day, Kind.BIRTH, {"agent_id": rec.agent_id, "name": rec.name, "tier": rec.model_tier, "generation": rec.generation,
                                                  "parent_id": parent.agent_id, "kind": kind, "endowment_usd": micro_to_usd(funding[1])}, rec.agent_id))
        return self.registry.get(rec.agent_id) or rec

    def create_offspring(self, parent: AgentRecord, name: str, endowment: int, tick: int, day: int, hold_min: int) -> ActionOutcome:
        p = self.cfg.population
        if not p.reproduction_enabled:
            return ActionOutcome.invalid("create_offspring", "reproduction_disabled")
        if any(a.name.lower() == name.lower() for a in self.registry.alive()):
            return ActionOutcome.invalid("create_offspring", "name_taken")
        if self.population >= p.cap:
            return ActionOutcome.stale("create_offspring", "population_cap")
        min_endow = hold_min * p.min_endowment_calls
        if endowment < min_endow:
            return ActionOutcome.invalid("create_offspring", f"endowment_below_minimum ${micro_to_usd(min_endow):.2f}")
        bal = self.wallet.balance(parent.agent_id)
        if bal - endowment < usd_to_micro(p.min_reserve_usd):
            return ActionOutcome.stale("create_offspring", "insufficient_reserve_after_endowment")
        child = self._child_of(parent, name, tick, day, (parent.agent_id, endowment, "endowment_out", "endowment_in"), "offspring")
        return ActionOutcome(True, "create_offspring", f"child {child.name} ({child.agent_id}) born", effects={"child_id": child.agent_id})

    def spawn_replacement(self, dead: AgentRecord, tick: int, day: int) -> AgentRecord | None:
        p = self.cfg.population
        grant = usd_to_micro(p.replacement_grant_usd)
        if self.population >= p.cap:
            self.bus.emit(Event(tick, day, Kind.REPLACEMENT, {"for": dead.agent_id, "skipped": "cap"}, dead.agent_id))
            return None
        if self.wallet.balance("spawn_pool") < grant:
            self.bus.emit(Event(tick, day, Kind.REPLACEMENT, {"for": dead.agent_id, "skipped": "pool_empty"}, dead.agent_id))
            return None
        alive = [a for a in self.registry.alive() if a.agent_id != dead.agent_id]
        if alive:
            r = self.rng.stream("replace", tick, dead.agent_id)
            weights = [a.balance + 1000 for a in alive]
            parent = r.choices(alive, weights=weights, k=1)[0]
            name = self._unique_name(f"{parent.name[:14]}-{parent.generation + 1}")
            child = self._child_of(parent, name, tick, day, ("spawn_pool", grant, "spawn_grant", "spawn_grant"), "replacement")
        else:
            spec = self.cfg.agents[self.rng.stream("replace", tick).randrange(len(self.cfg.agents))]
            seed = PersonalitySeed.generate(self.rng.stream("seed", "fresh", tick), spec.personality)
            rec = self._new_record(self._unique_name(f"{spec.name[:14]}-new"), spec.tier, 0, seed, self._spawn_position(None, f"fresh:{tick}"), tick, 0, None)
            child = self._insert(rec, tick, ("spawn_pool", grant, "spawn_grant", "spawn_grant"))
            self._init_vault(child, tick, f"I am {child.name}. {seed.motto} The void was empty when I arrived.")
            self.bus.emit(Event(tick, day, Kind.BIRTH, {"agent_id": child.agent_id, "name": child.name, "tier": child.model_tier, "generation": 0,
                                                      "parent_id": None, "kind": "fresh", "endowment_usd": micro_to_usd(grant)}, child.agent_id))
        self.bus.emit(Event(tick, day, Kind.REPLACEMENT, {"for": dead.agent_id, "replacement_id": child.agent_id}, dead.agent_id))
        return child

    # --- deaths --------------------------------------------------------------------------------------
    def bankrupt(self, agent: AgentRecord, tick: int, day: int, cause: str) -> AgentRecord | None:
        estate = self.wallet.balance(agent.agent_id)
        with self.db.tx():
            if estate > 0:
                self.wallet.transfer(agent.agent_id, "spawn_pool", tick, estate, "estate_out", "estate_in", ref="death", keep_reserve=False)
            self.registry.update(agent.agent_id, status=AgentStatus.BANKRUPT, died_tick=tick, asleep=True)
            # a dead agent's board entries are withdrawn so rewards can never reach it
            changed = self.db.execute("UPDATE task_applications SET status='rejected' WHERE agent_id=? AND status='pending'", (agent.agent_id,)).rowcount
            changed += self.db.execute("UPDATE tasks SET status='open', assigned_agent_id=NULL WHERE assigned_agent_id=? AND status='assigned'", (agent.agent_id,)).rowcount
            if changed:
                self.db.kv_set("tasks_rev", int(self.db.kv_get("tasks_rev", 0)) + 1)
        self.population -= 1
        replacement = self.spawn_replacement(agent, tick, day)
        self.bus.emit(Event(tick, day, Kind.DEATH, {"agent_id": agent.agent_id, "name": agent.name, "tier": agent.model_tier,
                                                  "generation": agent.generation, "cause": cause, "estate_usd": micro_to_usd(estate),
                                                  "replacement_id": replacement.agent_id if replacement else None}, agent.agent_id))
        self.pending_archive.append(agent.agent_id)  # filesystem move happens after the tick commits
        return replacement

    def archive_pending(self) -> list[str]:
        """Move bankrupt agents' vaults to the graveyard; called outside the tick transaction."""
        done: list[str] = []
        for aid in list(self.pending_archive):
            rec = self.registry.get(aid)
            if rec is not None and rec.status == AgentStatus.BANKRUPT:
                new_path = self.memory.archive_agent(aid, self.graveyard_root)
                self.registry.update(aid, status=AgentStatus.ARCHIVED, memory_path=str(new_path))
                done.append(aid)
            self.pending_archive.remove(aid)
        return done

    def resolve(self, tick: int, day: int, order: list[str], hold_min_for: dict[str, int]) -> dict[str, list[str]]:
        """End-of-tick: bankrupt every alive agent that cannot afford its next call, in tick order."""
        deaths: list[str] = []
        births: list[str] = []
        self.population = self.registry.count_alive()
        alive_ids = {a.agent_id for a in self.registry.alive()}
        seq = [a for a in order if a in alive_ids] + sorted(alive_ids - set(order))
        for aid in seq:
            rec = self.registry.get(aid)
            if rec is None or rec.status != AgentStatus.ALIVE:
                continue
            need = hold_min_for.get(rec.model_tier, 0)
            cause = self.pending_bankrupt.pop(aid, None)
            if cause is None and rec.balance < need:
                cause = "gate"
            if cause is None:
                continue
            rep = self.bankrupt(rec, tick, day, cause)
            deaths.append(aid)
            if rep is not None:
                births.append(rep.agent_id)
        self.pending_bankrupt.clear()
        return {"deaths": deaths, "births": births}
