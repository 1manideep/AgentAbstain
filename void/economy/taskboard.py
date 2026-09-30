"""Craigslist-style task board with a human judge (DESIGN §9.4)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from void.brain.decision import sanitize_text
from void.config import VoidConfig, micro_to_usd, usd_to_micro
from void.db import Database
from void.economy.wallet import Wallet
from void.events import Event, EventBus, Kind
from void.ids import IdFactory
from void.types import TaskView

__all__ = ["TaskBoard"]


@dataclass
class ApplyResult:
    ok: bool
    reason: str = ""
    application_id: str | None = None


class TaskBoard:
    def __init__(self, cfg: VoidConfig, db: Database, wallet: Wallet, ids: IdFactory, bus: EventBus) -> None:
        self.cfg = cfg
        self.db = db
        self.wallet = wallet
        self.ids = ids
        self.bus = bus
        if db.kv_get("tasks_rev") is None:
            db.kv_set("tasks_rev", 0)

    def _bump(self) -> None:
        self.db.kv_set("tasks_rev", int(self.db.kv_get("tasks_rev", 0)) + 1)

    @property
    def rev(self) -> int:
        return int(self.db.kv_get("tasks_rev", 0))

    def _is_alive(self, agent_id: str) -> bool:
        return self.db.fetchone("SELECT 1 FROM agents WHERE agent_id=? AND status='alive'", (agent_id,)) is not None

    def drop_agent(self, agent_id: str) -> None:
        """A dead agent's pending applications are rejected and its assignments reopened."""
        changed = self.db.execute("UPDATE task_applications SET status='rejected' WHERE agent_id=? AND status='pending'", (agent_id,)).rowcount
        changed += self.db.execute("UPDATE tasks SET status='open', assigned_agent_id=NULL WHERE assigned_agent_id=? AND status='assigned'", (agent_id,)).rowcount
        if changed:
            self._bump()

    def post(self, title: str, description: str, reward_usd: float, tick: int, day: int) -> str:
        tid = self.ids.new("tk")
        title = sanitize_text(title, single_line=True, max_len=80)
        description = sanitize_text(description, max_len=600)
        reward = max(0, usd_to_micro(reward_usd))
        self.db.execute("INSERT INTO tasks(task_id, title, description, reward, posted_tick, status) VALUES(?,?,?,?,?,'open')",
                        (tid, title, description, reward, tick))
        self._bump()
        self.bus.emit(Event(tick, day, Kind.TASK_POSTED, {"task_id": tid, "title": title, "reward_usd": micro_to_usd(reward)}))
        return tid

    def apply(self, agent_id: str, task_id: str, pitch: str, tick: int, day: int) -> ApplyResult:
        row = self.db.fetchone("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if row is None:
            return ApplyResult(False, "no_such_task")
        if row["status"] != "open":
            return ApplyResult(False, "task_not_open")
        dup = self.db.fetchone("SELECT 1 FROM task_applications WHERE task_id=? AND agent_id=? AND status='pending'", (task_id, agent_id))
        if dup:
            return ApplyResult(False, "already_applied")
        fee = usd_to_micro(self.cfg.economy.pitch_fee_usd)
        if fee and not self.wallet.transfer(agent_id, "house", tick, fee, "pitch_fee", "pitch_fee", ref=task_id):
            return ApplyResult(False, "cannot_afford_fee")
        aid = self.ids.new("ap")
        pitch = sanitize_text(pitch, max_len=280)
        self.db.execute("INSERT INTO task_applications(application_id, task_id, agent_id, pitch, fee, tick, status) VALUES(?,?,?,?,?,?,'pending')",
                        (aid, task_id, agent_id, pitch, fee, tick))
        self._bump()
        self.bus.emit(Event(tick, day, Kind.APPLY_TASK, {"task_id": task_id, "application_id": aid, "agent_id": agent_id, "fee_usd": micro_to_usd(fee)}, agent_id))
        return ApplyResult(True, "", aid)

    def approve(self, task_id: str, application_id: str, tick: int, day: int) -> tuple[bool, str]:
        app = self.db.fetchone("SELECT * FROM task_applications WHERE application_id=? AND task_id=?", (application_id, task_id))
        task = self.db.fetchone("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if app is None or task is None:
            return False, "not_found"
        if task["status"] != "open" or app["status"] != "pending":
            return False, "not_open"
        if not self._is_alive(app["agent_id"]):
            return False, "agent_dead"
        with self.db.tx():
            self.db.execute("UPDATE task_applications SET status='approved' WHERE application_id=?", (application_id,))
            self.db.execute("UPDATE task_applications SET status='rejected' WHERE task_id=? AND application_id<>? AND status='pending'", (task_id, application_id))
            self.db.execute("UPDATE tasks SET status='assigned', assigned_agent_id=? WHERE task_id=?", (app["agent_id"], task_id))
        self._bump()
        self.bus.emit(Event(tick, day, Kind.TASK_ASSIGNED, {"task_id": task_id, "agent_id": app["agent_id"], "application_id": application_id}, app["agent_id"]))
        return True, "ok"

    def complete(self, task_id: str, tick: int, day: int) -> tuple[bool, str]:
        task = self.db.fetchone("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if task is None:
            return False, "not_found"
        if task["status"] != "assigned" or not task["assigned_agent_id"]:
            return False, "not_assigned"
        agent = task["assigned_agent_id"]
        if not self._is_alive(agent):
            return False, "agent_dead"
        with self.db.tx():
            self.db.execute("UPDATE tasks SET status='completed', completed_tick=? WHERE task_id=?", (tick, task_id))
            self.wallet.credit(agent, tick, int(task["reward"]), "task_reward", task_id)
        self._bump()
        self.bus.emit(Event(tick, day, Kind.TASK_COMPLETED, {"task_id": task_id, "agent_id": agent, "reward_usd": micro_to_usd(int(task["reward"]))}, agent))
        return True, "ok"

    def cancel(self, task_id: str, tick: int, day: int) -> tuple[bool, str]:
        task = self.db.fetchone("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if task is None or task["status"] in ("completed", "cancelled"):
            return False, "not_cancellable"
        with self.db.tx():
            self.db.execute("UPDATE tasks SET status='cancelled' WHERE task_id=?", (task_id,))
            self.db.execute("UPDATE task_applications SET status='rejected' WHERE task_id=? AND status='pending'", (task_id,))
        self._bump()
        return True, "ok"

    def view_for(self, agent_id: str) -> list[TaskView]:
        rows = self.db.fetchall("SELECT * FROM tasks WHERE status IN ('open','assigned') ORDER BY posted_tick, task_id")
        out: list[TaskView] = []
        for r in rows:
            app = self.db.fetchone("SELECT status FROM task_applications WHERE task_id=? AND agent_id=? ORDER BY tick DESC LIMIT 1", (r["task_id"], agent_id))
            out.append(TaskView(r["task_id"], r["title"], micro_to_usd(int(r["reward"])), r["status"], app["status"] if app else None))
        return out

    def snapshot(self) -> list[dict]:
        tasks = self.db.fetchall("SELECT * FROM tasks ORDER BY posted_tick, task_id")
        apps = self.db.fetchall("SELECT * FROM task_applications ORDER BY tick, application_id")
        by_task: dict[str, list[dict]] = {}
        for a in apps:
            by_task.setdefault(a["task_id"], []).append({"id": a["application_id"], "agent_id": a["agent_id"], "pitch": a["pitch"],
                                                        "fee_usd": micro_to_usd(int(a["fee"])), "status": a["status"], "tick": int(a["tick"])})
        return [{"id": t["task_id"], "title": t["title"], "description": t["description"], "reward_usd": micro_to_usd(int(t["reward"])),
                 "status": t["status"], "assigned_agent_id": t["assigned_agent_id"], "posted_tick": int(t["posted_tick"]),
                 "completed_tick": t["completed_tick"], "applications": by_task.get(t["task_id"], [])} for t in tasks]

    def open_ids(self) -> list[str]:
        return [r["task_id"] for r in self.db.fetchall("SELECT task_id FROM tasks WHERE status='open' ORDER BY task_id")]

    def dump(self) -> str:
        return json.dumps(self.snapshot(), sort_keys=True)
