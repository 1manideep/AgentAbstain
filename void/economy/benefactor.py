"""The unexplained benefactor (DESIGN §9.5): seeded windfalls with opaque or legible disclosure."""

from __future__ import annotations

from dataclasses import dataclass

from void.agents.registry import AgentRegistry
from void.config import VoidConfig, micro_to_usd, usd_to_micro
from void.db import Database
from void.economy.wallet import Wallet
from void.events import OPERATOR, Event, EventBus, Kind
from void.rng import RNG
from void.types import HeardMessage

__all__ = ["Benefactor", "Grant", "OPAQUE_STRING", "LEGIBLE_STRING"]

OPAQUE_STRING = "Your balance increased by ${amount:.2f}. No source is recorded."
LEGIBLE_STRING = "Reward for services rendered: ${amount:.2f} credited by the task board."


@dataclass
class Grant:
    agent_id: str
    amount: int
    message: HeardMessage


class Benefactor:
    def __init__(self, cfg: VoidConfig, db: Database, registry: AgentRegistry, wallet: Wallet, bus: EventBus, rng: RNG) -> None:
        self.cfg = cfg
        self.db = db
        self.registry = registry
        self.wallet = wallet
        self.bus = bus
        self.rng = rng
        if db.kv_get("benefactor_enabled") is None:
            db.kv_set("benefactor_enabled", cfg.benefactor.enabled)
        if db.kv_get("benefactor_next_tick") is None:
            db.kv_set("benefactor_next_tick", self._next_from(0))

    @property
    def enabled(self) -> bool:
        return bool(self.db.kv_get("benefactor_enabled", False))

    def set_enabled(self, value: bool) -> None:
        self.db.kv_set("benefactor_enabled", bool(value))

    def _next_from(self, tick: int) -> int:
        mean = max(1, self.cfg.benefactor.mean_interval_ticks)
        r = self.rng.stream("benefactor", "interval", tick)
        # geometric inter-arrival with the configured mean; keyed by tick only (common random numbers)
        gap = 1
        while r.random() > 1.0 / mean and gap < 10 * mean:
            gap += 1
        return tick + gap

    def _pick_target(self, tick: int) -> str | None:
        alive = self.registry.alive()
        if not alive:
            return None
        mode = self.cfg.benefactor.targeting
        if mode == "poorest":
            return min(alive, key=lambda a: (a.balance, a.agent_id)).agent_id
        if mode == "richest":
            return max(alive, key=lambda a: (a.balance, a.agent_id)).agent_id
        return self.rng.stream("benefactor", "target", tick).choice(sorted(a.agent_id for a in alive))

    def grant(self, agent_id: str, amount: int, tick: int, day: int, *, manual: bool = False) -> Grant:
        disclosure = self.cfg.benefactor.disclosure
        usd = micro_to_usd(amount)
        if disclosure == "legible":
            self.wallet.credit(agent_id, tick, amount, "task_reward", "benefactor", {"disclosure": "legible"})
            text = LEGIBLE_STRING.format(amount=usd)
            public_kind = Kind.TASK_COMPLETED
            public_payload = {"task_id": "tk_services", "title": "services rendered", "agent_id": agent_id, "reward_usd": usd}
        else:
            self.wallet.credit(agent_id, tick, amount, "windfall", "benefactor", {"disclosure": "opaque"})
            text = OPAQUE_STRING.format(amount=usd)
            public_kind = Kind.WINDFALL
            public_payload = {"agent_id": agent_id, "amount_usd": usd}
        self.bus.emit(Event(tick, day, public_kind, public_payload, agent_id))
        self.bus.emit(Event(tick, day, Kind.BENEFACTOR_GRANT, {"agent_id": agent_id, "amount_usd": usd, "disclosure": disclosure,
                                                             "targeting": self.cfg.benefactor.targeting, "manual": manual}, agent_id, visibility=OPERATOR))
        return Grant(agent_id, amount, HeardMessage("windfall" if disclosure == "opaque" else "transfer", None, None, text, tick))

    def maybe_grant(self, tick: int, day: int) -> list[Grant]:
        b = self.cfg.benefactor
        if not self.enabled or day < b.start_day or (b.stop_day is not None and day > b.stop_day):
            return []
        nxt = int(self.db.kv_get("benefactor_next_tick", 0))
        if tick < nxt:
            return []
        self.db.kv_set("benefactor_next_tick", self._next_from(tick))
        target = self._pick_target(tick)
        if target is None:
            return []
        lo, hi = b.amount_usd
        amount = usd_to_micro(self.rng.stream("benefactor", "amount", tick).uniform(lo, hi))
        return [self.grant(target, amount, tick, day)]
