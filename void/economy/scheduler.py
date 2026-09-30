"""Day/tick clock and the active-window rules (DESIGN §9.3).

A day is ``ticks_per_day`` ticks. Every alive agent wakes at the first tick of a day.
``sleep`` is a one-way decision for the rest of the day. Under the ``sync_sleep`` policy the
whole population is put to sleep when the daily cap cannot cover one more round of calls;
under ``fcfs`` agents are gated one by one and the first to hit the cap sleeps.
"""

from __future__ import annotations

from dataclasses import dataclass

from void.agents.registry import AgentRegistry
from void.config import VoidConfig
from void.db import Database

__all__ = ["Clock", "Scheduler"]


@dataclass
class Clock:
    ticks_per_day: int
    tick: int = 0  # global tick counter; tick 1 is the first simulated tick
    day: int = 0  # 1-based once running

    @property
    def tick_of_day(self) -> int:
        return 0 if self.tick == 0 else (self.tick - 1) % self.ticks_per_day

    @property
    def ticks_left_today(self) -> int:
        return self.ticks_per_day - self.tick_of_day - 1

    @property
    def is_day_end(self) -> bool:
        return self.tick > 0 and self.tick_of_day == self.ticks_per_day - 1

    def advance(self) -> bool:
        """Advance one tick; return True when a new day starts."""
        self.tick += 1
        new_day = (self.tick - 1) % self.ticks_per_day == 0
        if new_day:
            self.day += 1
        return new_day

    def save(self, db: Database) -> None:
        db.kv_set("tick", self.tick)
        db.kv_set("day", self.day)

    @classmethod
    def load(cls, db: Database, ticks_per_day: int) -> Clock:
        return cls(ticks_per_day=ticks_per_day, tick=int(db.kv_get("tick", 0)), day=int(db.kv_get("day", 0)))


class Scheduler:
    def __init__(self, cfg: VoidConfig, registry: AgentRegistry) -> None:
        self.cfg = cfg
        self.registry = registry
        self.policy = cfg.economy.daily_cap_policy

    def begin_day(self) -> None:
        self.registry.wake_all_alive()

    def sleep(self, agent_id: str) -> None:
        self.registry.set_asleep([agent_id], True)

    def sleep_all(self) -> list[str]:
        ids = [a.agent_id for a in self.registry.awake()]
        self.registry.set_asleep(ids, True)
        return ids

    def awake_ids(self) -> list[str]:
        return [a.agent_id for a in self.registry.awake()]
