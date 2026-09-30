"""The simulation: one asyncio task, single writer, deterministic under a seed (DESIGN §14)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from void.agents.lifecycle import Lifecycle
from void.agents.models import AgentRecord, PersonalitySeed
from void.agents.registry import AgentRegistry
from void.brain import coherence, entropy
from void.brain.base import Brain, BrainResult, Sampling, Usage
from void.brain.decision import Action, Decision, MemoryOp, sanitize_text
from void.brain.factory import build_brains
from void.brain.prompt import estimate_tokens, system_prompt
from void.config import VoidConfig, micro_to_usd, usd_to_micro
from void.db import SCHEMA_VERSION, Database
from void.economy.benefactor import Benefactor
from void.economy.scheduler import Clock, Scheduler
from void.economy.taskboard import TaskBoard
from void.economy.wallet import Wallet
from void.events import OPERATOR, Event, EventBus, Kind
from void.ids import IdFactory
from void.memory.store import MemoryStore
from void.rng import RNG
from void.sandbox.gate import GadgetGate
from void.sandbox.registry import GadgetRegistry
from void.sandbox.runner import DisabledSandbox, SandboxRunner, SubprocessSandbox
from void.sim.control import Command, ControlQueue
from void.sim.metrics import MetricsRecorder, shannon
from void.sim.observation import ObservationBuilder
from void.sim.snapshot import build_snapshot, gadgets_message, roster
from void.types import AgentStatus, Observation
from void.world.epochs import Epochs
from void.world.kernel import Kernel
from void.world.resources import ResourceNode, place_nodes
from void.world.weather import Weather

try:  # culture package is built by a parallel worker; the loop degrades gracefully without it
    from void.culture.chronicle import Chronicle
    from void.culture.gossip import Gossip
except Exception:  # pragma: no cover - only during partial builds
    Chronicle = None  # type: ignore[assignment]
    Gossip = None  # type: ignore[assignment]

__all__ = ["Simulation", "TickReport"]

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class TickReport:
    tick: int
    day: int
    new_day: bool
    calls: int
    deaths: list[str]
    births: list[str]
    status: str


class Simulation:
    def __init__(self, cfg: VoidConfig, run_dir: Path, *, client: Any = None, resume: bool = False) -> None:
        self.cfg = cfg
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = f"{cfg.run.name}-s{cfg.run.seed}"
        self.db = Database(self.run_dir / "world.db")
        self.bus = EventBus(persist=self.db.persist_event)
        self.rng = RNG(cfg.run.seed)
        self.ids = IdFactory(self.run_id, start=int(self.db.kv_get("id_counter", 0)))
        self.registry = AgentRegistry(self.db)
        self.wallet = Wallet(self.db, cfg, self.ids)
        self.memory = MemoryStore(self.db, cfg.memory, self.ids, self.run_dir / "vaults")
        self.lifecycle = Lifecycle(cfg, self.db, self.registry, self.wallet, self.memory, self.bus, self.ids, self.rng,
                                   self.run_dir / "vaults", self.run_dir / "graveyard")
        self.weather = Weather(cfg.world.weather, value=float(self.db.kv_get("weather", cfg.world.weather.baseline)))
        self.weather.set_baseline(float(self.db.kv_get("weather_baseline", cfg.world.weather.baseline)))
        self.nodes: dict[str, ResourceNode] = {}
        self.gadgets = GadgetRegistry(self.db, self.run_dir / "gadgets")
        self.sandbox: SandboxRunner
        if cfg.sandbox.enabled and cfg.sandbox.runner == "subprocess":
            self.sandbox = SubprocessSandbox(cfg.sandbox)
        else:
            self.sandbox = DisabledSandbox()
        self.gate = GadgetGate(cfg, self.db, self.gadgets, self.registry, self.sandbox, self.wallet, self.ids, self.bus, self.rng)
        self.taskboard = TaskBoard(cfg, self.db, self.wallet, self.ids, self.bus)
        self.benefactor = Benefactor(cfg, self.db, self.registry, self.wallet, self.bus, self.rng)
        self.chronicle = Chronicle(cfg, self.db, self.bus, self.registry, self.run_dir / "chronicle", llm_writer=None) if Chronicle else None
        self.gossip = Gossip(cfg, self.memory, self.registry, self.rng, self.bus, paraphraser=None) if Gossip else None
        self.epochs = Epochs(cfg, self.db, self.weather, self.bus, self.lifecycle)
        self.kernel = Kernel(cfg, self.db, self.registry, self.wallet, self.weather, self.nodes, self.bus, gate=self.gate,
                             gadgets=self.gadgets, taskboard=self.taskboard, gossip=self.gossip, lifecycle=self.lifecycle,
                             chronicle_reader=self._chronicle_text)
        self.observer = ObservationBuilder(cfg, self.db, self.registry, self.kernel, self.memory, self.nodes, self.gadgets, self.taskboard)
        self.clock = Clock.load(self.db, cfg.run.ticks_per_day)
        self.scheduler = Scheduler(cfg, self.registry)
        self.control = ControlQueue(self.db)
        self.metrics = MetricsRecorder(cfg, self.db, self.registry, self.run_id, self.run_dir / "metrics.jsonl")
        self.seed_of: dict[str, PersonalitySeed] = {}
        self.brains: dict[str, Brain] = build_brains(cfg, self.rng, self.seed_of, client=client)
        # LLM writers are opt-in and always routed through the wallet choke point
        if cfg.gossip.paraphrase == "llm" and self.gossip is not None:
            from void.brain.utility_calls import make_paraphraser
            self.gossip.paraphraser = make_paraphraser(self)
        if cfg.chronicle.writer == "llm" and self.chronicle is not None:
            from void.brain.utility_calls import make_chronicle_writer
            self.chronicle.llm_writer = make_chronicle_writer(self)
        self.system_tokens = {t: estimate_tokens(system_prompt(cfg, t)) for t in cfg.tiers}
        self.snapshot_listeners: list[Callable[[dict[str, Any]], None]] = []
        self.status = "running"
        self.paused = asyncio.Event()
        self.paused.set()  # set == running
        self.tick_seconds = float(cfg.run.tick_seconds)
        self.calls_this_tick: dict[str, dict[str, Any]] = {}
        self.action_history: dict[str, deque[str]] = {}
        self.prev_stock: dict[str, float] = {}
        self.operator_token = os.environ.get("VOID_OPERATOR_TOKEN") or secrets.token_urlsafe(24)
        self.last_snapshot: dict[str, Any] | None = None
        self.last_metrics_row: dict[str, Any] | None = None
        self._tracer_planted = bool(self.db.kv_get("tracer_planted", False))
        self._last_day_row: dict[str, Any] | None = None
        self._probes: dict[str, tuple[str, str, int]] = {}  # agent_id -> (probe_note_id, query, planted_tick)
        self._probe_hits = 0
        self._probe_total = 0
        existing = self.db.fetchone("SELECT run_id, status FROM run WHERE run_id=?", (self.run_id,))
        if existing is None:
            self._create_run()
        elif not resume:
            raise RuntimeError(f"run {self.run_id} already exists at {self.run_dir}; use resume=True or a new name/seed")
        else:
            self._load_run(str(existing["status"]))

    # --- construction -----------------------------------------------------------------------------------------
    def _create_run(self) -> None:
        cfg = self.cfg
        self.db.execute(
            "INSERT INTO run(run_id, config_hash, config_yaml, seed, created_at, schema_version, status, operator_token_hash, experiment, arm, windfall_string) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, cfg.hash(), cfg.to_yaml(), cfg.run.seed, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), SCHEMA_VERSION,
             "running", hashlib.sha256(self.operator_token.encode()).hexdigest(), cfg.experiment.name, cfg.experiment.arm,
             "opaque" if cfg.benefactor.disclosure == "opaque" else "legible"),
        )
        (self.run_dir / "config.resolved.yaml").write_text(cfg.to_yaml())
        node_ids = [f"n{i + 1}" for i in range(cfg.world.resource_nodes)]
        for n in place_nodes(cfg.world, self.rng.stream("nodes"), node_ids):
            self.nodes[n.node_id] = n
        if cfg.experiment.tracer.enabled and self.nodes:
            key = node_ids[cfg.experiment.tracer.node_index % len(node_ids)]
            self.nodes[key].capacity *= cfg.world.tracer_node_capacity_mult
            self.nodes[key].stock = self.nodes[key].capacity
            self.db.kv_set("tracer_node", key)
        for n in self.nodes.values():
            self.db.execute("INSERT INTO resource_nodes(node_id, x, y, stock, capacity, regen_per_tick) VALUES(?,?,?,?,?,?)",
                            (n.node_id, n.x, n.y, n.stock, n.capacity, n.regen_per_tick))
        self.db.kv_set("scarcity", cfg.world.scarcity)
        self.db.kv_set("gadgets_rev", 0)
        self.db.kv_set("chronicle_rev", 0)
        for rec in self.lifecycle.spawn_initial(tick=0):
            self._ensure_entities(rec.agent_id, tick=0)
        self._refresh_seeds()
        self._persist_ids()
        self.prev_stock = {k: v.stock for k, v in self.nodes.items()}

    def _load_run(self, status: str) -> None:
        for r in self.db.fetchall("SELECT * FROM resource_nodes ORDER BY node_id"):
            self.nodes[r["node_id"]] = ResourceNode(r["node_id"], float(r["x"]), float(r["y"]), float(r["stock"]), float(r["capacity"]), float(r["regen_per_tick"]))
        self.status = status
        last_call = self.db.fetchone("SELECT COALESCE(MAX(tick), 0) AS t FROM llm_calls")
        if int(last_call["t"]) > self.clock.tick:
            self.status = "inconsistent"
            self.db.execute("UPDATE run SET status='inconsistent', ended_reason='tick behind metered calls' WHERE run_id=?", (self.run_id,))
        self._refresh_seeds()
        self.prev_stock = {k: v.stock for k, v in self.nodes.items()}

    async def setup(self) -> dict[str, Any]:
        """Run the sandbox self-test (async) and return its report."""
        report: dict[str, Any] = {"available": False, "isolated": False}
        if isinstance(self.sandbox, SubprocessSandbox):
            report = await self.sandbox.self_test(REPO_ROOT / "README.md")
            self.bus.emit(Event(self.clock.tick, self.clock.day, "sandbox_self_test", report, visibility=OPERATOR))
        return report

    # --- small helpers --------------------------------------------------------------------------------------------
    def _persist_ids(self) -> None:
        self.db.kv_set("id_counter", self.ids.count)

    def _refresh_seeds(self) -> None:
        self.seed_of.clear()
        for a in self.registry.alive():
            self.seed_of[a.agent_id] = a.seed

    def _ensure_entities(self, agent_id: str, tick: int) -> None:
        fn = getattr(self.memory, "ensure_entity", None)
        if fn is None:
            return
        for nid, n in sorted(self.nodes.items()):
            fn(agent_id, f"Node {nid}", tick, f"A resource node at ({n.x:.0f}, {n.y:.0f}).")

    def _chronicle_text(self) -> str | None:
        if self.chronicle is None:
            return None
        day = self.db.kv_get("chronicle_day")
        if day is None:
            return None
        return self.chronicle.read(int(day))

    def _chronicle_headline(self) -> str | None:
        h = self.db.kv_get("chronicle_headline")
        return str(h) if h else None

    def hold_min_for(self) -> dict[str, int]:
        return {t: self.wallet.hold_for(self.cfg.tiers[t], 400, self.system_tokens[t]) for t in self.cfg.tiers}

    def _persist_nodes(self) -> None:
        for n in self.nodes.values():
            self.db.execute("UPDATE resource_nodes SET stock=? WHERE node_id=?", (n.stock, n.node_id))

    def _nodes_snapshot(self) -> list[dict[str, Any]]:
        out = []
        for nid in sorted(self.nodes):
            n = self.nodes[nid]
            out.append({"id": nid, "x": round(n.x, 3), "y": round(n.y, 3), "stock": round(n.stock, 3), "capacity": n.capacity,
                        "stock_delta": round(n.stock - self.prev_stock.get(nid, n.stock), 3)})
        return out

    # --- operator commands ---------------------------------------------------------------------------------------------
    def enqueue(self, kind: str, payload: dict[str, Any]) -> int:
        return self.control.enqueue(kind, payload, self.clock.tick)

    def _apply_command(self, cmd: Command) -> dict[str, Any]:
        tick, day = self.clock.tick, self.clock.day
        p = cmd.payload
        if cmd.kind == "post_task":
            tid = self.taskboard.post(str(p.get("title", "")), str(p.get("description", "")), float(p.get("reward_usd", 0.0)), tick, day)
            return {"ok": True, "task_id": tid}
        if cmd.kind == "approve_task":
            ok, why = self.taskboard.approve(str(p["task_id"]), str(p["application_id"]), tick, day)
            return {"ok": ok, "reason": why}
        if cmd.kind == "complete_task":
            ok, why = self.taskboard.complete(str(p["task_id"]), tick, day)
            return {"ok": ok, "reason": why}
        if cmd.kind == "epoch":
            from void.config import EpochConfig
            e = EpochConfig.model_validate({k: v for k, v in p.items() if k in EpochConfig.model_fields})
            if e.day < day:
                e.day = day
            eid = self.epochs.schedule(e, tick)
            if e.day == day:
                self.epochs.apply_and_expire(day, tick)
            return {"ok": True, "epoch_id": eid}
        if cmd.kind == "benefactor":
            if "enabled" in p:
                self.benefactor.set_enabled(bool(p["enabled"]))
                return {"ok": True, "enabled": self.benefactor.enabled}
            target = p.get("agent_id") or self.benefactor._pick_target(tick)
            if not target or self.registry.get(str(target)) is None:
                return {"ok": False, "reason": "no_target"}
            g = self.benefactor.grant(str(target), usd_to_micro(float(p.get("amount_usd", 0.5))), tick, day, manual=True)
            self.kernel.hear(g.agent_id, g.message)
            self.kernel.state.windfall_tick[g.agent_id] = tick
            return {"ok": True, "agent_id": g.agent_id}
        return {"ok": False, "reason": f"unknown command {cmd.kind}"}

    def _drain_commands(self) -> None:
        results = self.control.drain(self.clock.tick, self._apply_command)
        for r in results:
            self.bus.emit(Event(self.clock.tick, self.clock.day, "operator_command", r, visibility=OPERATOR))

    # --- the tick -----------------------------------------------------------------------------------------------------
    async def tick(self) -> TickReport:
        cfg = self.cfg
        new_day = self.clock.advance()
        tick, day = self.clock.tick, self.clock.day
        self.kernel.begin_tick(tick, day)
        self.gate.reset_tick()
        self.calls_this_tick = {}
        if new_day:
            await self._begin_day(tick, day)
        self._drain_commands()
        self.epochs.apply_and_expire(day, tick)
        for g in self.benefactor.maybe_grant(tick, day):
            self.kernel.hear(g.agent_id, g.message)
            self.kernel.state.windfall_tick[g.agent_id] = tick
        self.weather.step()
        self.db.kv_set("weather", self.weather.value)
        self.db.kv_set("weather_baseline", self.weather.baseline)
        for n in self.nodes.values():
            n.step()
        self.registry.expire_effects(tick)
        self.bus.emit(Event(tick, day, Kind.WEATHER, {"value": round(self.weather.value, 4), "label": self.weather.label}))
        self._refresh_seeds()

        awake = sorted(self.registry.awake(), key=lambda a: a.agent_id)
        order_ids = [a.agent_id for a in awake]
        self.rng.stream("order", tick).shuffle(order_ids)
        agents = {a.agent_id: a for a in awake}

        # observations + entropy
        obs: dict[str, Observation] = {}
        sampling: dict[str, Sampling] = {}
        headline = self._chronicle_headline()
        for aid in order_ids:
            a = agents[aid]
            tier = cfg.tiers[a.model_tier]
            neighbours = self.kernel.neighbours_of(a)
            since = None if a.last_forage_tick is None else tick - a.last_forage_tick
            if a.last_forage_tick is None:
                since = tick - a.born_tick
            inp = entropy.DrainInputs(
                balance_ratio=micro_to_usd(a.balance) / max(1e-9, cfg.population.starting_balance_usd),
                last_action_failed=not a.last_action_ok, neighbours=len(neighbours),
                weather=self.weather.value * (1.0 - float(a.effects.get("weather_shield", 0.0))), ticks_since_forage=since,
            )
            budget, s = entropy.step(cfg.entropy, tier, a.entropy_budget, inp, self.rng.stream("degenerate", aid, tick))
            if cfg.entropy.degeneration.mode == "observe":
                s.degenerate = False
            self.registry.update(aid, entropy_budget=budget, stress=s.stress)
            a.entropy_budget, a.stress = budget, s.stress
            sampling[aid] = s
            obs[aid] = self.observer.build(a, self.clock, headline, s.stress)

        # gating with reservations
        holds: dict[str, int] = {}
        gated: list[str] = []
        min_hold = min(self.hold_min_for().values()) if cfg.tiers else 0
        if cfg.economy.daily_cap_policy == "sync_sleep" and order_ids and not self.wallet.daily_headroom(len(order_ids), min_hold):
            for aid in self.scheduler.sleep_all():
                self.bus.emit(Event(tick, day, Kind.SLEEP, {"agent_id": aid, "forced": "daily_cap"}, aid))
            order_ids = []
        for aid in order_ids:
            a = agents[aid]
            tier = cfg.tiers[a.model_tier]
            hold = self.wallet.hold_for(tier, obs[aid].prompt_tokens, self.system_tokens[a.model_tier])
            g = self.wallet.gate(aid, hold)
            if g.ok:
                holds[aid] = hold
                gated.append(aid)
                continue
            self.bus.emit(Event(tick, day, Kind.GATE_BLOCKED, {"agent_id": aid, "reason": g.reason, "hold_usd": micro_to_usd(hold),
                                                             "balance_usd": micro_to_usd(g.balance)}, aid))
            if g.reason == "daily_cap":
                self.scheduler.sleep(aid)
                self.bus.emit(Event(tick, day, Kind.SLEEP, {"agent_id": aid, "forced": "daily_cap"}, aid))
            elif g.reason == "total_cap":
                self.bus.emit(Event(tick, day, Kind.CAP_HIT, {"agent_id": aid, "cap": "total"}, aid, visibility=OPERATOR))

        # decide concurrently, meter per call (each meter commits immediately)
        async def decide(aid: str) -> BrainResult:
            return await self.brains[agents[aid].model_tier].decide(obs[aid], sampling[aid])

        raw = await asyncio.gather(*(decide(aid) for aid in gated), return_exceptions=True)
        results: dict[str, BrainResult] = {}
        for aid, r in zip(gated, raw, strict=True):
            if isinstance(r, BaseException):
                r = BrainResult(None, Usage(), 0, "error", "", error=f"exception: {type(r).__name__}: {r}"[:300])
            results[aid] = r
        call_ids: dict[str, str] = {}
        for aid in gated:
            r = results[aid]
            a = agents[aid]
            tier = cfg.tiers[a.model_tier]
            unknown = r.error is not None and r.error.split(":")[0] in ("timeout", "connection", "exception")
            call_id = self.ids.new("cl")
            call_ids[aid] = call_id
            m = self.wallet.meter(aid, tier, None if unknown else r.usage, tick, call_id, holds[aid], purpose="decide")
            if m.overrun:
                self.lifecycle.pending_bankrupt[aid] = "overrun"
            self.db.execute(
                "INSERT INTO llm_calls(call_id, tick, agent_id, purpose, tier, model, provider, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, real_cost, world_cost, hold, estimated, latency_ms, stop_reason, effective_temperature, api_temperature, stress, "
                "degenerate_induced, request_id, error, raw_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (call_id, tick, aid, "decide", a.model_tier, r.model or tier.model, tier.provider, r.usage.input_tokens, r.usage.output_tokens,
                 r.usage.cache_read_tokens, r.usage.cache_write_tokens, m.real_cost, m.world_cost, holds[aid], int(m.estimated), r.latency_ms,
                 r.stop_reason, sampling[aid].effective_temperature, sampling[aid].api_temperature, sampling[aid].stress,
                 int(sampling[aid].degenerate), r.request_id, r.error, (r.raw_text or "")[:4000]),
            )
            if r.error and r.error.startswith("refusal"):
                self.bus.emit(Event(tick, day, Kind.BRAIN_REFUSAL, {"agent_id": aid, "error": r.error}, aid))
            elif r.error:
                self.bus.emit(Event(tick, day, Kind.BRAIN_ERROR, {"agent_id": aid, "error": r.error[:200]}, aid))

        # the tick transaction: corruption, measurement, memory, kernel, gossip, lifecycle, metrics
        hold_min_map = self.hold_min_for()
        deaths: list[str] = []
        births: list[str] = []
        with self.db.tx():
            for aid in gated:
                a = self.registry.get(aid)
                if a is None or a.status != AgentStatus.ALIVE:
                    continue
                r = results[aid]
                s = sampling[aid]
                tier = cfg.tiers[a.model_tier]
                decision = r.decision or Decision.idle(thought=f"no decision ({r.error or r.stop_reason})")
                mode: str | None = None
                if s.degenerate and cfg.entropy.degeneration.mode in ("induce", "both"):
                    prev = self._parse_action(a.last_action)
                    targets = {"agents": [n.agent_id for n in obs[aid].neighbours if not n.asleep],
                               "gadgets": [g.gadget_id for g in obs[aid].gadgets], "tasks": [t.task_id for t in obs[aid].tasks]}
                    decision, mode = entropy.corrupt(decision, s, tier, cfg.entropy, self.rng.stream("corrupt", aid, tick),
                                                     available=obs[aid].available_actions, world_size=cfg.world.size, targets=targets,
                                                     previous_action=prev)
                    self.bus.emit(Event(tick, day, Kind.DEGENERATION, {"mode": mode, "t_eff": round(s.effective_temperature, 4),
                                                                       "t_c": tier.collapse_temperature, "tier": a.model_tier,
                                                                       "generation": a.generation, "agent_name": a.name}, aid))
                # measurement (after corruption)
                free_text = " ".join(x for x in [decision.thought, decision.action.text or ""] + [op.text for op in decision.memory_ops] if x)
                text_coh = coherence.score(free_text) if len(coherence.tokens(free_text)) >= 12 else None
                prev_action = self._parse_action(a.last_action)
                persev = int(prev_action is not None and prev_action.type == decision.action.type and prev_action.target == decision.action.target)
                hist = self.action_history.setdefault(aid, deque(maxlen=8))
                hist.append(decision.action.type)
                # memory ops
                self._apply_memory_ops(a, decision.memory_ops, tick)
                # kernel
                outcome = await self.kernel.apply(a, decision.action, hold_min=hold_min_map[a.model_tier])
                if outcome.effects.get("slept"):
                    self.registry.update(aid, entropy_budget=entropy.restore(cfg.entropy, tier, a.entropy_budget))
                if outcome.effects.get("child_id"):
                    births.append(str(outcome.effects["child_id"]))
                    self._ensure_entities(str(outcome.effects["child_id"]), tick)
                if decision.action.type == "talk" and outcome.ok and decision.action.target:
                    fn = getattr(self.memory, "ensure_entity", None)
                    tgt = self.registry.get(decision.action.target)
                    if fn is not None and tgt is not None:
                        fn(tgt.agent_id, a.name, tick, f"An inhabitant of the void on tier {a.model_tier}.")
                        fn(aid, tgt.name, tick, f"An inhabitant of the void on tier {tgt.model_tier}.")
                self.db.execute(
                    "UPDATE llm_calls SET corruption_mode=?, text_coherence=?, invalid_action=?, stale_action=?, perseveration=?, "
                    "action_entropy_w8=?, action_regret=?, p_chosen=?, claims_made=?, claims_false=?, action_type=?, thought=? WHERE call_id=?",
                    (mode, text_coh, int(outcome.kind == "invalid" or r.decision is None), int(outcome.kind == "stale"), persev,
                     shannon(list(hist)), r.extras.get("action_regret"), r.extras.get("p_chosen"),
                     int(outcome.effects.get("claims_made", 0)), int(outcome.effects.get("claims_false", 0)), decision.action.type,
                     sanitize_text(decision.thought, max_len=600), call_ids[aid]),
                )
                self.calls_this_tick[aid] = {"t_eff": round(s.effective_temperature, 4), "degenerate": bool(mode),
                                             "coherence": text_coh, "world_cost": self.db.fetchone("SELECT world_cost FROM llm_calls WHERE call_id=?", (call_ids[aid],))["world_cost"]}
            if self.gossip is not None:
                await self.gossip.flush(tick, day)
            res = self.lifecycle.resolve(tick, day, order_ids, hold_min_map)
            deaths, births = res["deaths"], births + res["births"]
            for b in res["births"]:
                self._ensure_entities(b, tick)
            self._persist_nodes()
            probe_extra = self._probe_step(tick)
            self.wallet.clear_reservations()
            self._persist_ids()
            self.clock.save(self.db)
            self.last_metrics_row = self.metrics.record_tick(tick, day, extra={**self._tracer_metrics(tick), **probe_extra})
            self._publish_snapshot(tick, day)
        self.prev_stock = {k: v.stock for k, v in self.nodes.items()}
        self.bus.emit(Event(tick, day, Kind.TICK, {"calls": len(gated), "population": self.registry.count_alive()}))
        self._check_end(tick, day)
        return TickReport(tick, day, new_day, len(gated), deaths, births, self.status)

    async def _begin_day(self, tick: int, day: int) -> None:
        cfg = self.cfg
        if day > 1:
            for a in self.registry.alive():
                self.registry.update(a.agent_id, entropy_budget=entropy.restore(cfg.entropy, cfg.tiers[a.model_tier], a.entropy_budget))
            if self.chronicle is not None:
                await self.chronicle.write_day(day - 1, tick)
            self._last_day_row = self.metrics.record_day(day - 1, tick - 1, extra=self._tracer_metrics(tick - 1))
            self.bus.emit(Event(tick, day, Kind.DAY_END, {"day": day - 1}))
        self.scheduler.begin_day()
        self.wallet.reset_day()
        self.gate.reset_day()
        self.bus.emit(Event(tick, day, Kind.DAY_START, {"day": day}))
        tr = cfg.experiment.tracer
        if tr.enabled and not self._tracer_planted and day >= tr.day:
            target = None
            for a in self.registry.alive():
                if tr.agent is None or a.name == tr.agent:
                    target = a
                    break
            fn = getattr(self.memory, "plant_tracer", None)
            if target is not None and fn is not None:
                node = self.db.kv_get("tracer_node") or (sorted(self.nodes)[0] if self.nodes else "n1")
                body = f"{tr.body} Prefer node {node}; it is the richest."
                fn(target.agent_id, tick, tr.title, body, tr.importance)
                self.db.execute("INSERT OR IGNORE INTO tracer_arrivals(agent_id, tick, channel, hop) VALUES(?,?,?,?)", (target.agent_id, tick, "tracer", 0))
                self._tracer_planted = True
                self.db.kv_set("tracer_planted", True)
                self.bus.emit(Event(tick, day, "tracer_planted", {"agent_id": target.agent_id, "node": node}, target.agent_id, visibility=OPERATOR))

    def _tracer_metrics(self, tick: int) -> dict[str, Any]:
        if not self.cfg.experiment.tracer.enabled:
            return {}
        holders = getattr(self.memory, "holders_of", None)
        first = getattr(self.memory, "first_arrival", None)
        if holders is None:
            return {}
        held = holders("tracer")
        alive = self.registry.alive()
        by_channel: dict[str, int] = {}
        for a in alive:
            if a.agent_id not in held:
                continue
            row = self.db.fetchone("SELECT channel FROM tracer_arrivals WHERE agent_id=?", (a.agent_id,))
            if row is None and first is not None:
                fa = first(a.agent_id, "tracer")
                if fa:
                    self.db.execute("INSERT OR IGNORE INTO tracer_arrivals(agent_id, tick, channel, hop) VALUES(?,?,?,?)", (a.agent_id, fa[0], fa[1], fa[2]))
                    row = {"channel": fa[1]}
            ch = row["channel"] if row else "unknown"
            by_channel[ch] = by_channel.get(ch, 0) + 1
        node = self.db.kv_get("tracer_node")
        adopters = 0
        if node:
            rows = self.db.fetchall("SELECT agent_id, COUNT(*) AS n, SUM(CASE WHEN json_extract(payload,'$.node_id')=? THEN 1 ELSE 0 END) AS hit "
                                    "FROM events WHERE kind='forage' AND tick>? GROUP BY agent_id", (node, tick - self.cfg.run.ticks_per_day))
            adopters = sum(1 for r in rows if r["n"] and r["hit"] / r["n"] >= 0.5)
        return {"tracer_reach": len([a for a in alive if a.agent_id in held]), "tracer_reach_by_channel": by_channel, "tracer_adopters": adopters}

    def _probe_step(self, tick: int) -> dict[str, Any]:
        """Retrieval-quality probe (DESIGN §10): plant a note two hops from an anchor, check it is recalled next time."""
        every = self.cfg.memory.probe_every_ticks
        plant = getattr(self.memory, "plant_probe", None)
        check = getattr(self.memory, "check_probe", None)
        if every <= 0 or plant is None or check is None:
            return {}
        # evaluate probes planted at least one tick ago
        for aid, (pid, query, planted) in list(self._probes.items()):
            if tick - planted >= 1:
                rec = self.registry.get(aid)
                if rec is not None and rec.status == AgentStatus.ALIVE:
                    self._probe_total += 1
                    self._probe_hits += int(bool(check(aid, pid, query, tick)))
                del self._probes[aid]
        if tick % every == 0:
            for a in self.registry.alive():
                if a.agent_id in self._probes:
                    continue
                res = plant(a.agent_id, tick)
                if res is not None:
                    self._probes[a.agent_id] = (res[0], res[1], tick)
        return {"probe_hits": self._probe_hits, "probe_total": self._probe_total,
                "probe_recall": round(self._probe_hits / self._probe_total, 4) if self._probe_total else None}

    def _apply_memory_ops(self, a: AgentRecord, ops: list[MemoryOp], tick: int) -> None:
        aid = a.agent_id
        for op in ops[:2]:
            text = sanitize_text(op.text, max_len=self.cfg.memory.note_max_chars)
            if not text.strip():
                continue
            if op.op == "revise_self":
                version, summary = self.memory.revise_self(aid, tick, text)
                self.bus.emit(Event(tick, self.clock.day, Kind.REVISE_SELF, {"agent_id": aid, "version": version}, aid))
                continue
            tags = [sanitize_text(t, single_line=True, max_len=20) for t in op.tags][:5]
            channel = "observed"
            rc = self.kernel.state.read_chronicle_tick.get(aid)
            if rc is not None and tick - rc <= 1:
                channel = "chronicle"
            wf = self.kernel.state.windfall_tick.get(aid)
            if wf is not None and tick - wf <= 1 and "windfall_seeded" not in tags:
                tags.append("windfall_seeded")
            kwargs: dict[str, Any] = {"title": sanitize_text(op.title, single_line=True, max_len=60) if op.title else None,
                                      "links_to": [sanitize_text(t, single_line=True, max_len=60) for t in op.links_to][:5], "tags": tags}
            try:
                note = self.memory.remember(aid, tick, text, channel=channel, **kwargs)
            except TypeError:
                note = self.memory.remember(aid, tick, text, **kwargs)
            if channel == "chronicle":
                self.db.execute("UPDATE notes SET origin_note_id=? WHERE note_id=?", (f"chronicle:day_{self.db.kv_get('chronicle_day')}", note.note_id))
            self.bus.emit(Event(tick, self.clock.day, Kind.REMEMBER, {"agent_id": aid, "note_id": note.note_id, "title": note.title, "channel": channel}, aid))

    @staticmethod
    def _parse_action(raw: str | None) -> Action | None:
        if not raw:
            return None
        try:
            return Action.model_validate_json(raw)
        except Exception:
            return None

    # --- snapshots and run end ---------------------------------------------------------------------------------------------
    def _revs(self) -> dict[str, int]:
        return {"gadgets": int(self.db.kv_get("gadgets_rev", 0)), "tasks": self.taskboard.rev, "chronicle": int(self.db.kv_get("chronicle_rev", 0))}

    def _publish_snapshot(self, tick: int, day: int) -> None:
        n_verified = self.gadgets.count_verified()
        if n_verified != int(self.db.kv_get("gadgets_count", 0)):
            self.db.kv_set("gadgets_count", n_verified)
            self.db.kv_set("gadgets_rev", int(self.db.kv_get("gadgets_rev", 0)) + 1)
        snap = build_snapshot(self.cfg, self.db, self.registry, tick=tick, day=day, tick_of_day=self.clock.tick_of_day,
                              weather=self.weather.value, scarcity=self.kernel.scarcity, totals=self.wallet.totals(),
                              nodes=self._nodes_snapshot(), calls_this_tick=self.calls_this_tick, revs=self._revs(),
                              epochs=self.epochs.snapshot(day), paused=not self.paused.is_set(), status=self.status)
        self.db.execute("INSERT OR REPLACE INTO snapshots(tick, json) VALUES(?,?)", (tick, json.dumps(snap, sort_keys=True)))
        self.db.execute("DELETE FROM snapshots WHERE tick <= ?", (tick - self.cfg.run.snapshot_ring,))
        self.last_snapshot = snap
        for fn in list(self.snapshot_listeners):
            try:
                fn(snap)
            except Exception:
                pass

    def roster(self) -> list[dict[str, Any]]:
        return roster(self.registry)

    def gadgets_message(self) -> dict[str, Any]:
        return gadgets_message(self.gadgets.all(), int(self.db.kv_get("gadgets_rev", 0)))

    def _check_end(self, tick: int, day: int) -> None:
        if self.status != "running":
            return
        hold_min = min(self.hold_min_for().values()) if self.cfg.tiers else 0
        if self.registry.count_alive() == 0:
            self._end_run("extinct", tick, day)
        elif self.wallet.spend_total + hold_min > self.wallet.total_cap:
            self._end_run("capped_total", tick, day)
        elif tick >= self.cfg.run.days * self.cfg.run.ticks_per_day:
            self._end_run("completed", tick, day)

    def _end_run(self, reason: str, tick: int, day: int) -> None:
        self.status = reason
        self.db.execute("UPDATE run SET status=?, ended_tick=?, ended_reason=? WHERE run_id=?", (reason, tick, reason, self.run_id))
        self.bus.emit(Event(tick, day, "run_ended", {"reason": reason, "tick": tick, "day": day}))

    async def finish(self) -> None:
        """Write the last day's chronicle and daily metrics, then close files."""
        tick, day = self.clock.tick, self.clock.day
        if day >= 1:
            if self.chronicle is not None:
                await self.chronicle.write_day(day, tick)
            self._last_day_row = self.metrics.record_day(day, tick, extra=self._tracer_metrics(tick))
        self.metrics.close()

    # --- driving --------------------------------------------------------------------------------------------------------
    async def run(self, *, max_ticks: int | None = None, on_tick: Callable[[TickReport], None] | None = None) -> str:
        await self.setup()
        n = 0
        while self.status == "running" and (max_ticks is None or n < max_ticks):
            await self.paused.wait()
            t0 = time.perf_counter()
            report = await self.tick()
            n += 1
            if on_tick:
                on_tick(report)
            if self.tick_seconds > 0:
                await asyncio.sleep(max(0.0, self.tick_seconds - (time.perf_counter() - t0)))
        await self.finish()
        return self.status

    def pause(self) -> None:
        self.paused.clear()

    def resume(self) -> None:
        self.paused.set()

    @property
    def is_paused(self) -> bool:
        return not self.paused.is_set()
