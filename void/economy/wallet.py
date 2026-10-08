"""Wallet: the one mechanism behind both the fiction and the real-dollar safety cutoff.

Rules (DESIGN §9.1, v2 reservation model):

* ``gate`` runs before a call with a *hold* (the worst case for this call). It passes only if
  the agent holds the hold and both caps have room for ``spent + reserved + hold``; on pass
  the hold is reserved. Neither ``gate`` nor ``meter`` changes agent status: bankruptcy is a
  single end-of-tick decision in ``lifecycle``.
* ``meter`` runs after the call with the provider's reported usage (or the hold when usage
  is unknown). It releases the reservation, adds the real cost to the caps and debits the
  world cost from the payer. If the world cost exceeds the balance (only possible on a
  provider quirk, since the hold bounded it) the difference is debited from the ``house``
  wallet as ``overrun`` so every dollar has a ledger row.
* Kernel wallets (``spawn_pool``, ``house``, ``chronicle``, ``research_pool``) live in ``world_kv`` and get
  ledger rows like agents do, so money is conserved across the whole run.
* ``charged_call`` is the single choke point for any LLM call.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from void.brain.base import BrainResult, Usage
from void.config import TierConfig, VoidConfig, usd_to_micro
from void.db import Database
from void.economy.pricing import hold_micro, real_cost_micro, world_cost_micro
from void.ids import IdFactory

__all__ = ["Wallet", "GateResult", "MeterResult", "InsufficientFunds", "KERNEL_WALLETS", "ChargedCall"]

KERNEL_WALLETS = ("spawn_pool", "house", "chronicle", "research_pool")


class InsufficientFunds(Exception):
    pass


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str  # ok | insufficient | daily_cap | total_cap
    hold: int
    balance: int


@dataclass(frozen=True)
class MeterResult:
    real_cost: int
    world_cost: int
    balance_after: int
    overrun: int
    estimated: bool


@dataclass
class ChargedCall:
    gate: GateResult
    result: BrainResult | None
    meter: MeterResult | None
    hold: int


class Wallet:
    def __init__(self, db: Database, cfg: VoidConfig, ids: IdFactory) -> None:
        self.db = db
        self.cfg = cfg
        self.ids = ids
        self.daily_cap = usd_to_micro(cfg.economy.daily_cap_usd)
        self.total_cap = usd_to_micro(cfg.economy.total_cap_usd)
        self.min_reserve = usd_to_micro(cfg.population.min_reserve_usd)
        self._reserved_today = 0
        self._reserved_total = 0
        for key, initial in (("spend_today", 0), ("spend_total", 0),
                             ("spawn_pool", usd_to_micro(cfg.population.spawn_pool_usd)),
                             ("house", usd_to_micro(cfg.economy.house_budget_usd)),
                             ("chronicle", usd_to_micro(cfg.economy.chronicle_budget_usd)),
                             ("research_pool", usd_to_micro(cfg.economy.research_pool_usd)), ("house_debt", 0)):
            if db.kv_get(key) is None:
                db.kv_set(key, initial)

    # --- kernel-owned totals ----------------------------------------------------------------
    @property
    def spend_today(self) -> int:
        return int(self.db.kv_get("spend_today", 0))

    @property
    def spend_total(self) -> int:
        return int(self.db.kv_get("spend_total", 0))

    @property
    def spawn_pool(self) -> int:
        return int(self.db.kv_get("spawn_pool", 0))

    @property
    def reserved_today(self) -> int:
        return self._reserved_today

    def reset_day(self) -> None:
        self.db.kv_set("spend_today", 0)
        self._reserved_today = 0

    def clear_reservations(self) -> None:
        """Called at the end of every tick: any hold not released by a meter is dropped."""
        self._reserved_today = 0
        self._reserved_total = 0

    # --- balances ---------------------------------------------------------------------------
    def balance(self, wallet_id: str) -> int:
        if wallet_id in KERNEL_WALLETS:
            return int(self.db.kv_get(wallet_id, 0))
        row = self.db.fetchone("SELECT balance FROM agents WHERE agent_id=?", (wallet_id,))
        if row is None:
            raise KeyError(wallet_id)
        return int(row["balance"])

    def _set_balance(self, wallet_id: str, value: int) -> None:
        if value < 0:
            raise ValueError("balance would go negative")
        if wallet_id in KERNEL_WALLETS:
            self.db.kv_set(wallet_id, value)
        else:
            self.db.execute("UPDATE agents SET balance=? WHERE agent_id=?", (value, wallet_id))

    def _ledger(self, wallet_id: str, tick: int, delta: int, balance_after: int, kind: str, ref: str | None, payload: dict | None) -> None:
        self.db.execute(
            "INSERT INTO wallet_ledger(entry_id, tick, agent_id, delta, balance_after, kind, ref, payload) VALUES(?,?,?,?,?,?,?,?)",
            (self.ids.new("le"), tick, wallet_id, delta, balance_after, kind, ref,
             json.dumps(payload, sort_keys=True) if payload else None),
        )

    def credit(self, wallet_id: str, tick: int, amount: int, kind: str, ref: str | None = None, payload: dict | None = None) -> int:
        if amount < 0:
            raise ValueError("credit amount must be >= 0")
        with self.db.tx():
            bal = self.balance(wallet_id) + amount
            self._set_balance(wallet_id, bal)
            self._ledger(wallet_id, tick, amount, bal, kind, ref, payload)
        return bal

    def debit(self, wallet_id: str, tick: int, amount: int, kind: str, ref: str | None = None, payload: dict | None = None) -> int:
        """Debit exactly ``amount``; raises InsufficientFunds if the wallet cannot cover it."""
        if amount < 0:
            raise ValueError("debit amount must be >= 0")
        with self.db.tx():
            bal = self.balance(wallet_id)
            if amount > bal:
                raise InsufficientFunds(f"{wallet_id} holds {bal} < {amount}")
            new_bal = bal - amount
            self._set_balance(wallet_id, new_bal)
            self._ledger(wallet_id, tick, -amount, new_bal, kind, ref, payload)
        return new_bal

    def transfer(self, from_id: str, to_id: str, tick: int, amount: int, kind_out: str = "transfer_out",
                 kind_in: str = "transfer_in", ref: str | None = None, *, keep_reserve: bool = True) -> bool:
        """Move money between wallets atomically. Agents keep ``min_reserve`` after any voluntary outflow."""
        if amount <= 0 or from_id == to_id:
            return False
        try:
            with self.db.tx():
                bal = self.balance(from_id)
                floor = self.min_reserve if (keep_reserve and from_id not in KERNEL_WALLETS) else 0
                if bal - amount < floor:
                    raise InsufficientFunds(f"{from_id} cannot part with {amount} keeping reserve {floor}")
                self.debit(from_id, tick, amount, kind_out, ref, {"to": to_id})
                self.credit(to_id, tick, amount, kind_in, ref, {"from": from_id})
        except InsufficientFunds:
            return False
        return True

    # --- gate and meter -----------------------------------------------------------------------
    def hold_for(self, tier: TierConfig, prompt_tokens: int, system_tokens: int = 0) -> int:
        return hold_micro(tier, self.cfg.economy, prompt_tokens, system_tokens)

    def gate(self, wallet_id: str, hold: int) -> GateResult:
        bal = self.balance(wallet_id)
        if bal < hold:
            return GateResult(False, "insufficient", hold, bal)
        if self.spend_total + self._reserved_total + hold > self.total_cap:
            return GateResult(False, "total_cap", hold, bal)
        if self.spend_today + self._reserved_today + hold > self.daily_cap:
            return GateResult(False, "daily_cap", hold, bal)
        self._reserved_today += hold
        self._reserved_total += hold
        return GateResult(True, "ok", hold, bal)

    def release(self, hold: int) -> None:
        self._reserved_today = max(0, self._reserved_today - hold)
        self._reserved_total = max(0, self._reserved_total - hold)

    def daily_headroom(self, n_calls: int, hold: int) -> bool:
        return self.spend_today + self._reserved_today + n_calls * hold <= self.daily_cap

    def meter(self, wallet_id: str, tier: TierConfig, usage: Usage | None, tick: int, ref: str, hold: int,
              *, purpose: str = "decide") -> MeterResult:
        """Charge a finished call. ``usage=None`` means unknown: the hold is charged as an estimate."""
        if self.db._depth != 0:
            raise RuntimeError("Wallet.meter must run outside a transaction so the real-money record commits immediately")
        self.release(hold)
        estimated = usage is None
        if usage is None:
            real = world = hold
        else:
            real = real_cost_micro(usage, tier, self.cfg.tiers)
            world = world_cost_micro(usage, tier, self.cfg.economy, self.cfg.tiers)
        kind = {"decide": "llm_call", "gossip": "gossip_call", "chronicle": "chronicle_call", "maintain": "maintenance_call",
                "exam": "exam_call"}.get(purpose, "llm_call")
        payload = {"real_cost": real, "world_cost": world, "estimated": int(estimated)}
        if usage is not None:
            payload.update({"in": usage.input_tokens, "out": usage.output_tokens,
                            "cr": usage.cache_read_tokens, "cw": usage.cache_write_tokens})
            if len(usage.attempts) > 1 or (usage.attempts and usage.attempts[0][0] not in ("", tier.model)):
                payload["attempts"] = [{"model": m, "in": a.input_tokens, "out": a.output_tokens, "cr": a.cache_read_tokens,
                                        "cw": a.cache_write_tokens} for m, a in usage.attempts]
        with self.db.tx():
            bal = self.balance(wallet_id)
            charged = min(world, bal)
            overrun = world - charged
            new_bal = bal - charged
            self._set_balance(wallet_id, new_bal)
            self._ledger(wallet_id, tick, -charged, new_bal, kind, ref, payload)
            if overrun:
                house = self.balance("house")
                if house >= overrun:
                    self._set_balance("house", house - overrun)
                    self._ledger("house", tick, -overrun, house - overrun, "overrun", ref, {"agent": wallet_id})
                else:
                    # the house cannot cover it: drain the house and book the remainder as kernel debt,
                    # a first-class ledger amount so ledger sums still equal the world cost charged
                    if house > 0:
                        self._set_balance("house", 0)
                        self._ledger("house", tick, -house, 0, "overrun", ref, {"agent": wallet_id})
                    unfunded = overrun - house
                    debt = int(self.db.kv_get("house_debt", 0)) + unfunded
                    self.db.kv_set("house_debt", debt)
                    self._ledger("house", tick, -unfunded, -debt, "overrun_unfunded", ref, {"agent": wallet_id})
            self.db.kv_set("spend_today", self.spend_today + real)
            self.db.kv_set("spend_total", self.spend_total + real)
        return MeterResult(real_cost=real, world_cost=world, balance_after=new_bal, overrun=overrun, estimated=estimated)

    async def charged_call(self, wallet_id: str, tier: TierConfig, hold: int, tick: int, ref: str,
                           call: Callable[[], Awaitable[BrainResult]], *, purpose: str = "decide") -> ChargedCall:
        """gate -> call -> meter. The only sanctioned way to spend money on a model."""
        g = self.gate(wallet_id, hold)
        if not g.ok:
            return ChargedCall(g, None, None, hold)
        try:
            result = await call()
        except Exception as e:  # a brain must not raise, but the wallet must still settle
            result = BrainResult(None, Usage(), 0, "error", "", error=f"exception: {type(e).__name__}: {e}")
        unknown = result.error is not None and (result.error.startswith("timeout") or result.error.startswith("connection")
                                                or result.error.startswith("exception"))
        usage: Usage | None = None if unknown else result.usage
        m = self.meter(wallet_id, tier, usage, tick, ref, hold, purpose=purpose)
        return ChargedCall(g, result, m, hold)

    # --- reporting ----------------------------------------------------------------------------
    def ledger_sum(self, kind: str | None = None, wallet_id: str | None = None) -> int:
        sql = "SELECT COALESCE(SUM(delta),0) AS s FROM wallet_ledger WHERE 1=1"
        params: list[Any] = []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if wallet_id:
            sql += " AND agent_id=?"
            params.append(wallet_id)
        return int(self.db.fetchone(sql, tuple(params))["s"])

    def money_snapshot(self) -> dict[str, int]:
        agents = int(self.db.fetchone("SELECT COALESCE(SUM(balance),0) AS s FROM agents")["s"])
        return {"agents": agents, "spawn_pool": self.balance("spawn_pool"), "house": self.balance("house"),
                "house_debt": int(self.db.kv_get("house_debt", 0)), "chronicle": self.balance("chronicle"),
                "research_pool": self.balance("research_pool"),
                "spend_today": self.spend_today, "spend_total": self.spend_total}

    def totals(self) -> dict[str, int]:
        return {"spend_today": self.spend_today, "spend_total": self.spend_total, "spawn_pool": self.spawn_pool,
                "house": self.balance("house"), "chronicle": self.balance("chronicle"), "research_pool": self.balance("research_pool"),
                "daily_cap": self.daily_cap, "total_cap": self.total_cap, "reserved_today": self._reserved_today}
