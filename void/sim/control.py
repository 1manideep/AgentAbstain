"""Operator command queue (DESIGN §14.1): HTTP handlers enqueue; the tick loop applies.

Every command is a ``control_commands`` row: ``received_tick`` when it was enqueued, ``applied_tick`` and ``result``
once the tick loop applied it. Those rows make a run replayable (DESIGN §0 "Single writer; replayable runs"):
:class:`CommandReplay` reads the applied rows of a finished (or stopped) run and hands each one back to a new run at
the tick it was applied there, through the same queue and the same handler (``void run --replay-commands RUN_DIR``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from void.db import Database

__all__ = ["ControlQueue", "Command", "RecordedCommand", "CommandReplay", "HEADLESS_SKIP", "recorded_commands"]

# Loop-level flags, not world changes: a headless replay runs at its own pace and never pauses, so these are skipped.
HEADLESS_SKIP: frozenset[str] = frozenset({"pause", "resume", "speed", "step"})


@dataclass
class Command:
    cmd_id: int
    kind: str
    payload: dict[str, Any]
    received_tick: int


@dataclass(frozen=True)
class RecordedCommand:
    """One applied ``control_commands`` row of a source run."""

    cmd_id: int
    kind: str
    payload: dict[str, Any]
    received_tick: int
    applied_tick: int


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


def _connect(run_dir: Path) -> sqlite3.Connection:
    """A plain connection to ``run_dir/world.db`` (only read; closing it removes the WAL side files again)."""
    path = Path(run_dir) / "world.db"
    if not path.is_file():
        raise FileNotFoundError(f"no world.db in {run_dir}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def recorded_commands(run_dir: str | Path) -> tuple[list[RecordedCommand], dict[str, Any]]:
    """The applied control commands of the run in ``run_dir`` in application order (``applied_tick``, then
    ``cmd_id``, the order the drain applied them), and that run's ``run`` row (``run_id``, ``config_hash``, ``seed``;
    empty when the table holds no row). Commands never applied (still pending when the run stopped) are left out:
    they changed nothing."""
    conn = _connect(Path(run_dir))
    try:
        try:
            rows = conn.execute("SELECT cmd_id, kind, payload, received_tick, applied_tick FROM control_commands "
                                "WHERE applied_tick IS NOT NULL ORDER BY applied_tick, cmd_id").fetchall()
        except sqlite3.Error as e:
            raise ValueError(f"cannot read control_commands in {run_dir}: {e}") from e
        try:
            run = conn.execute("SELECT run_id, config_hash, seed FROM run LIMIT 1").fetchone()
        except sqlite3.Error:
            run = None
    finally:
        conn.close()
    out = [RecordedCommand(int(r["cmd_id"]), str(r["kind"]), json.loads(r["payload"]), int(r["received_tick"]),
                           int(r["applied_tick"])) for r in rows]
    return out, (dict(run) if run is not None else {})


class CommandReplay:
    """The operator commands of a source run, handed back tick by tick (``void run --replay-commands``).

    :meth:`due` returns the commands the source run applied at ``tick``, in the order it applied them; the loop
    enqueues each with its recorded ``received_tick`` just before it drains its queue, so they go through the same
    ``_apply_command`` path at the same point of the same tick. Kinds in :data:`HEADLESS_SKIP` are dropped.
    """

    def __init__(self, commands: list[RecordedCommand], source: dict[str, Any] | None = None, *,
                 origin: str | None = None) -> None:
        self.source = dict(source or {})
        self.origin = origin
        self.skipped = [c for c in commands if c.kind in HEADLESS_SKIP]
        self.commands = [c for c in commands if c.kind not in HEADLESS_SKIP]
        self._by_tick: dict[int, list[RecordedCommand]] = {}
        for c in self.commands:
            self._by_tick.setdefault(c.applied_tick, []).append(c)

    @classmethod
    def load(cls, run_dir: str | Path) -> CommandReplay:
        commands, source = recorded_commands(run_dir)
        return cls(commands, source, origin=str(run_dir))

    def due(self, tick: int) -> list[RecordedCommand]:
        """The commands to apply at ``tick`` (each is handed out once)."""
        return self._by_tick.pop(int(tick), [])

    def drop_through(self, tick: int) -> int:
        """Forget the commands due at or before ``tick`` (a resumed replay already applied them); returns how many."""
        due = [t for t in self._by_tick if t <= int(tick)]
        return sum(len(self._by_tick.pop(t)) for t in due)

    def remaining(self) -> int:
        """Commands not handed out yet (their tick has not come, or the replaying run ended before it)."""
        return sum(len(v) for v in self._by_tick.values())

    def summary(self) -> dict[str, Any]:
        """Provenance for the replaying run's ``world_kv`` (``replay_commands``): no path, so it stays comparable."""
        return {"source_run_id": self.source.get("run_id"), "source_config_hash": self.source.get("config_hash"),
                "commands": len(self.commands), "skipped": sorted(c.kind for c in self.skipped)}
