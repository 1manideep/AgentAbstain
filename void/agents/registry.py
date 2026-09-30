"""CRUD over the ``agents`` table. The kernel and lifecycle are the only writers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from void.agents.models import AgentRecord, PersonalitySeed
from void.db import Database
from void.types import AgentStatus, Vec2

__all__ = ["AgentRegistry"]

_COLS = (
    "agent_id, name, parent_id, model_tier, balance, personality_seed, memory_path, generation, status, "
    "x, y, heading, entropy_budget, stress, asleep, last_forage_tick, last_action, last_action_ok, "
    "weather_nudge_used, born_tick, died_tick, created_at"
)


def _row_to_record(row: sqlite3.Row, effects: dict[str, float] | None = None) -> AgentRecord:
    return AgentRecord(
        agent_id=row["agent_id"], name=row["name"], model_tier=row["model_tier"], balance=int(row["balance"]),
        seed=PersonalitySeed.from_json(row["personality_seed"]), memory_path=row["memory_path"],
        generation=int(row["generation"]), status=AgentStatus(row["status"]),
        pos=Vec2(float(row["x"]), float(row["y"])), heading=float(row["heading"]),
        entropy_budget=float(row["entropy_budget"]), stress=float(row["stress"]), asleep=bool(row["asleep"]),
        born_tick=int(row["born_tick"]), parent_id=row["parent_id"], last_forage_tick=row["last_forage_tick"],
        last_action=row["last_action"], last_action_ok=bool(row["last_action_ok"]),
        weather_nudge_used=float(row["weather_nudge_used"]), died_tick=row["died_tick"], effects=effects or {},
    )


class AgentRegistry:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, rec: AgentRecord, created_at: str = "tick") -> None:
        self.db.execute(
            f"INSERT INTO agents({_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.agent_id, rec.name, rec.parent_id, rec.model_tier, rec.balance, rec.seed.to_json(),
                rec.memory_path, rec.generation, rec.status.value, rec.pos.x, rec.pos.y, rec.heading,
                rec.entropy_budget, rec.stress, int(rec.asleep), rec.last_forage_tick, rec.last_action,
                int(rec.last_action_ok), rec.weather_nudge_used, rec.born_tick, rec.died_tick, created_at,
            ),
        )

    def get(self, agent_id: str) -> AgentRecord | None:
        row = self.db.fetchone("SELECT * FROM agents WHERE agent_id=?", (agent_id,))
        return _row_to_record(row, self._effects(agent_id)) if row else None

    def _effects(self, agent_id: str) -> dict[str, float]:
        rows = self.db.fetchall("SELECT kind, value FROM agent_effects WHERE agent_id=?", (agent_id,))
        return {r["kind"]: float(r["value"]) for r in rows}

    def all(self, status: AgentStatus | None = None) -> list[AgentRecord]:
        if status is None:
            rows = self.db.fetchall("SELECT * FROM agents ORDER BY agent_id")
        else:
            rows = self.db.fetchall("SELECT * FROM agents WHERE status=? ORDER BY agent_id", (status.value,))
        effects: dict[str, dict[str, float]] = {}
        for r in self.db.fetchall("SELECT agent_id, kind, value FROM agent_effects"):
            effects.setdefault(r["agent_id"], {})[r["kind"]] = float(r["value"])
        return [_row_to_record(r, effects.get(r["agent_id"], {})) for r in rows]

    def alive(self) -> list[AgentRecord]:
        return self.all(AgentStatus.ALIVE)

    def awake(self) -> list[AgentRecord]:
        return [a for a in self.alive() if not a.asleep]

    def count_alive(self) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) AS n FROM agents WHERE status='alive'")["n"])

    def update(self, agent_id: str, **fields: object) -> None:
        if not fields:
            return
        allowed = {
            "balance", "generation", "status", "x", "y", "heading", "entropy_budget", "stress", "asleep",
            "last_forage_tick", "last_action", "last_action_ok", "weather_nudge_used", "died_tick",
            "model_tier", "personality_seed", "memory_path", "name",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update columns {sorted(bad)}")
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [v.value if isinstance(v, AgentStatus) else (int(v) if isinstance(v, bool) else v) for v in fields.values()]
        self.db.execute(f"UPDATE agents SET {cols} WHERE agent_id=?", (*vals, agent_id))

    def set_position(self, agent_id: str, pos: Vec2, heading: float) -> None:
        self.update(agent_id, x=pos.x, y=pos.y, heading=heading)

    def set_asleep(self, agent_ids: Iterable[str], asleep: bool) -> None:
        ids = list(agent_ids)
        if not ids:
            return
        q = ",".join("?" for _ in ids)
        self.db.execute(f"UPDATE agents SET asleep=? WHERE agent_id IN ({q})", (int(asleep), *ids))

    def wake_all_alive(self) -> None:
        self.db.execute("UPDATE agents SET asleep=0, weather_nudge_used=0 WHERE status='alive'")

    # --- effects (capped gadget effects with expiry) --------------------------------------
    def set_effect(self, agent_id: str, kind: str, value: float, expires_tick: int) -> None:
        self.db.execute(
            "INSERT INTO agent_effects(agent_id, kind, value, expires_tick) VALUES(?,?,?,?) "
            "ON CONFLICT(agent_id, kind) DO UPDATE SET value=excluded.value, expires_tick=excluded.expires_tick",
            (agent_id, kind, value, expires_tick),
        )

    def expire_effects(self, tick: int) -> int:
        cur = self.db.execute("DELETE FROM agent_effects WHERE expires_tick <= ?", (tick,))
        return int(cur.rowcount)

    def family_tree(self) -> list[dict[str, object]]:
        rows = self.db.fetchall(
            "SELECT agent_id, name, parent_id, model_tier, generation, status, born_tick, died_tick, balance FROM agents ORDER BY born_tick, agent_id"
        )
        return [dict(r) for r in rows]
