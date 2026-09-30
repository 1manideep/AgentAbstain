"""The verification gate: propose -> fee -> size -> static -> sandbox -> spec -> register (DESIGN §12).

Every stage records why it stopped, so a rejected proposal is as informative as a verified
one. Usage re-hashes the stored code before every run and applies capped, non-stacking,
expiring effects through the agent registry.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from void.agents.registry import AgentRegistry
from void.brain.decision import sanitize_text
from void.config import VoidConfig, usd_to_micro
from void.db import Database
from void.economy.wallet import Wallet
from void.events import Event, EventBus, Kind
from void.ids import IdFactory
from void.rng import RNG
from void.sandbox.registry import GadgetRecord, GadgetRegistry, TamperedGadget, sha256, validate_describe
from void.sandbox.runner import SandboxRunner
from void.sandbox.static_check import check

__all__ = ["GadgetGate", "GateOutcome", "UseOutcome"]


@dataclass
class GateOutcome:
    ok: bool
    stage: str
    reason: str = ""
    gadget_id: str | None = None


@dataclass
class UseOutcome:
    ok: bool
    reason: str = ""
    kind: str | None = None
    value: float | None = None
    expires_tick: int | None = None


class GadgetGate:
    def __init__(self, cfg: VoidConfig, db: Database, registry: GadgetRegistry, agents: AgentRegistry,
                 sandbox: SandboxRunner, wallet: Wallet, ids: IdFactory, bus: EventBus, rng: RNG) -> None:
        self.cfg = cfg
        self.db = db
        self.registry = registry
        self.agents = agents
        self.sandbox = sandbox
        self.wallet = wallet
        self.ids = ids
        self.bus = bus
        self.rng = rng
        self.runs_this_tick = 0
        for key, initial in (("sandbox_runs_today", 0), ("sandbox_cpu_today", 0.0), ("sandbox_proposals_today", {})):
            if db.kv_get(key) is None:
                db.kv_set(key, initial)

    # --- budgets ------------------------------------------------------------------------------
    def reset_day(self) -> None:
        self.db.kv_set("sandbox_runs_today", 0)
        self.db.kv_set("sandbox_cpu_today", 0.0)
        self.db.kv_set("sandbox_proposals_today", {})

    def reset_tick(self) -> None:
        self.runs_this_tick = 0

    def _budget_ok(self) -> tuple[bool, str]:
        s = self.cfg.sandbox
        if not s.enabled or not self.sandbox.available:
            return False, "sandbox_unavailable"
        if self.runs_this_tick >= s.max_runs_per_tick:
            return False, "sandbox_busy"
        if int(self.db.kv_get("sandbox_runs_today", 0)) >= s.max_runs_per_day:
            return False, "sandbox_budget_exhausted"
        if float(self.db.kv_get("sandbox_cpu_today", 0.0)) >= s.cpu_seconds_per_day:
            return False, "sandbox_budget_exhausted"
        return True, "ok"

    def _account_run(self, cpu_seconds: float) -> None:
        self.runs_this_tick += 1
        self.db.kv_set("sandbox_runs_today", int(self.db.kv_get("sandbox_runs_today", 0)) + 1)
        self.db.kv_set("sandbox_cpu_today", float(self.db.kv_get("sandbox_cpu_today", 0.0)) + max(0.0, cpu_seconds))

    def can_propose(self, agent_id: str, *, recent_failure: bool) -> tuple[bool, str]:
        """Availability of `propose_gadget` for the observation (Voyager trigger + budgets)."""
        s = self.cfg.sandbox
        ok, why = self._budget_ok()
        if not ok:
            return False, why
        holds = self.registry.count_verified(agent_id)
        if not recent_failure and holds > 0:
            return False, "no_recent_failure"
        if holds >= s.max_gadgets_per_agent:
            return False, "max_gadgets_per_agent"
        if self.registry.count_verified() >= s.max_gadgets_total:
            return False, "max_gadgets_total"
        proposals = self.db.kv_get("sandbox_proposals_today", {}) or {}
        if int(proposals.get(agent_id, 0)) >= s.proposals_per_agent_per_day:
            return False, "proposals_per_day"
        return True, "ok"

    def can_use(self) -> tuple[bool, str]:
        return self._budget_ok()

    # --- propose --------------------------------------------------------------------------------
    async def propose(self, agent_id: str, tick: int, day: int, name: str, purpose: str, code: str, tests: str,
                      *, position: tuple[float, float], template_label: str | None = None,
                      recent_failure: bool = True) -> GateOutcome:
        ok, why = self.can_propose(agent_id, recent_failure=recent_failure)
        if not ok:
            return GateOutcome(False, "availability", why)
        fee = usd_to_micro(self.cfg.sandbox.gadget_fee_usd)
        if not self.wallet.transfer(agent_id, "house", tick, fee, "gadget_fee", "gadget_fee", ref=f"propose:{name}"):
            return GateOutcome(False, "fee", "cannot_afford_fee")
        proposals = self.db.kv_get("sandbox_proposals_today", {}) or {}
        proposals[agent_id] = int(proposals.get(agent_id, 0)) + 1
        self.db.kv_set("sandbox_proposals_today", proposals)
        gid = self.ids.new("gd")
        name = sanitize_text(name, single_line=True, max_len=20)
        purpose = sanitize_text(purpose, single_line=True, max_len=120)
        verification: dict[str, object] = {"template_label": template_label, "isolated": getattr(self.sandbox, "isolated", False)}

        def reject(stage: str, reason: str) -> GateOutcome:
            verification.update({"stage": stage, "reason": reason[:300]})
            cp, tp = self.registry.store_files(gid, code, tests)
            self.registry.insert(GadgetRecord(gid, name, agent_id, purpose, str(cp), str(tp), sha256(code), sha256(tests),
                                              "rejected", {}, {}, verification, False, template_label, None, None, tick))
            self.bus.emit(Event(tick, day, Kind.GADGET_REJECTED, {"gadget_id": gid, "name": name, "owner": agent_id,
                                                                  "stage": stage, "reason": reason[:200]}, agent_id))
            return GateOutcome(False, stage, reason, gid)

        max_bytes = self.cfg.sandbox.max_code_bytes
        if len(code.encode()) > max_bytes or len(tests.encode()) > max_bytes:
            return reject("size", f"source exceeds {max_bytes} bytes")
        sc = check(code, max_bytes=max_bytes)
        if not sc.ok:
            return reject("static", "; ".join(sc.errors[:4]))
        st = check(tests, max_bytes=max_bytes, allow_gadget=True)
        if not st.ok:
            return reject("static", "tests: " + "; ".join(st.errors[:4]))
        seed = self.rng.stream("gadget", gid).randrange(1 << 30)
        run_tests = self.cfg.sandbox.gate_enabled
        result = await self.sandbox.verify(code, tests, seed, run_tests=run_tests)
        self._account_run(float(self.cfg.sandbox.cpu_seconds))  # a fixed charge per run keeps budgets machine-independent
        verification.update({"sandbox_stage": result.stage, "run_tests": run_tests})
        if not result.ok:
            return reject(result.stage if result.stage in ("tests", "timeout", "killed", "exception", "contract", "protocol", "unavailable") else "sandbox",
                          result.error or result.stage)
        spec, err = validate_describe(result.describe, self.cfg.world.effect_caps)
        if spec is None:
            return reject("spec", err or "invalid describe()")
        ring = self.rng.stream("gadget_place", gid)
        theta = ring.uniform(0, 2 * math.pi)
        half = self.cfg.world.size / 2.0
        x = max(-half, min(half, position[0] + 0.8 * math.cos(theta)))
        y = max(-half, min(half, position[1] + 0.8 * math.sin(theta)))
        verification.update({"stage": "verified", "reason": ""})
        cp, tp = self.registry.store_files(gid, code, tests)
        rec = GadgetRecord(gid, name, agent_id, purpose, str(cp), str(tp), sha256(code), sha256(tests), "verified",
                           spec["render"], spec["effect"], verification, not run_tests, template_label, x, y, tick)
        self.registry.insert(rec)
        self.bus.emit(Event(tick, day, Kind.GADGET_VERIFIED, {"gadget_id": gid, "name": name, "owner": agent_id, "stage": "verified",
                                                              "reason": "", "x": x, "y": y, "render": spec["render"],
                                                              "effect": spec["effect"], "verified_without_tests": not run_tests}, agent_id))
        return GateOutcome(True, "verified", "", gid)

    # --- use ----------------------------------------------------------------------------------------
    async def use(self, agent_id: str, tick: int, day: int, gadget_id: str, params: dict[str, float], distance: float) -> UseOutcome:
        rec = self.registry.get(gadget_id)
        if rec is None or rec.status != "verified":
            return UseOutcome(False, "no_such_gadget")
        if distance > self.cfg.world.gadget_radius:
            return UseOutcome(False, "too_far")
        ok, why = self.can_use()
        if not ok:
            return UseOutcome(False, why)
        last = self.registry.last_use_tick(gadget_id, agent_id)
        if last is not None and tick - last < self.cfg.world.effect_ticks:
            return UseOutcome(False, "cooldown")
        try:
            code, _ = self.registry.load_code(rec)
        except (TamperedGadget, OSError):
            # quarantine before any fee: a tampered gadget is never used or charged for again
            self.registry.record_use(gadget_id, agent_id, tick, False, None, rec.effect.get("value"), "tampered")
            self.db.execute("UPDATE gadgets SET status='tampered' WHERE gadget_id=?", (gadget_id,))
            self.bus.emit(Event(tick, day, "gadget_tampered", {"gadget_id": gadget_id, "agent_id": agent_id}, agent_id, visibility="operator"))
            return UseOutcome(False, "gadget_tampered")
        fee = usd_to_micro(self.cfg.sandbox.use_fee_usd)
        if fee and not self.wallet.transfer(agent_id, "house", tick, fee, "gadget_use_fee", "gadget_use_fee", ref=f"use:{gadget_id}"):
            return UseOutcome(False, "cannot_afford_fee")
        seed = self.rng.stream("gadget", gadget_id, tick, agent_id).randrange(1 << 30)
        result = await self.sandbox.run(code, params, seed)
        self._account_run(float(self.cfg.sandbox.cpu_seconds))
        expected = float(rec.effect.get("value", 0.0))
        kind = str(rec.effect.get("kind"))
        cap = self.cfg.world.effect_caps.get(kind, 0.0)
        if not result.ok or not isinstance(result.run_value, dict) or "value" not in result.run_value:
            self.registry.record_use(gadget_id, agent_id, tick, False, None, expected, result.error or result.stage)
            self.bus.emit(Event(tick, day, "gadget_use_failed", {"gadget_id": gadget_id, "agent_id": agent_id,
                                                                  "stage": result.stage, "reason": (result.error or "")[:200]}, agent_id))
            return UseOutcome(False, f"run_failed:{result.stage}")
        try:
            raw = float(result.run_value["value"])
        except (TypeError, ValueError):
            raw = 0.0
        if raw != raw:
            raw = 0.0
        value = max(0.0, min(raw, expected, cap))
        expires = tick + self.cfg.world.effect_ticks
        current = self.agents.get(agent_id)
        if current is not None and current.effects.get(kind, 0.0) >= value:
            value_applied = current.effects[kind]  # non-stacking, max wins
        else:
            self.agents.set_effect(agent_id, kind, value, expires)
            value_applied = value
        self.registry.record_use(gadget_id, agent_id, tick, True, value, expected, None)
        self.bus.emit(Event(tick, day, Kind.USE_GADGET, {"gadget_id": gadget_id, "agent_id": agent_id, "kind": kind,
                                                       "value": value, "expected": expected, "expires_tick": expires,
                                                       "params": json.loads(json.dumps(params))}, agent_id))
        return UseOutcome(True, "", kind, value_applied, expires)
