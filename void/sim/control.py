"""Operator command queue (DESIGN §14.1): HTTP handlers enqueue; the tick loop applies."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from void.db import Database

__all__ = ["ControlQueue", "Command"]


@dataclass
class Command:
    cmd_id: int
    kind: str
    payload: dict[str, Any]
    received_tick: int


class ControlQueue:
    def __init__(self, db: Database) -> None:
        self.db = db

    def enqueue(self, kind: str, payload: dict[str, Any], received_tick: int) -> int:
        cur = self.db.execute("INSERT INTO control_commands(received_tick, kind, payload) VALUES(?,?,?)",
                              (received_tick, kind, json.dumps(payload, sort_keys=True, default=str)))
        return int(cur.lastrowid)

    def pending(self) -> list[Command]:
        rows = self.db.fetchall("SELECT cmd_id, kind, payload, received_tick FROM control_commands WHERE applied_tick IS NULL ORDER BY cmd_id")
        return [Command(int(r["cmd_id"]), r["kind"], json.loads(r["payload"]), int(r["received_tick"])) for r in rows]

    def drain(self, tick: int, handler: Callable[[Command], dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for cmd in self.pending():
            try:
                res = handler(cmd)
            except Exception as e:  # never let a bad command stop the tick
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            self.db.execute("UPDATE control_commands SET applied_tick=?, result=? WHERE cmd_id=?",
                            (tick, json.dumps(res, sort_keys=True, default=str), cmd.cmd_id))
            results.append({"cmd_id": cmd.cmd_id, "kind": cmd.kind, **res})
        return results

    def result(self, cmd_id: int) -> dict[str, Any] | None:
        r = self.db.fetchone("SELECT applied_tick, result FROM control_commands WHERE cmd_id=?", (cmd_id,))
        if r is None:
            return None
        return {"applied_tick": r["applied_tick"], "result": json.loads(r["result"]) if r["result"] else None}
