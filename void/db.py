"""SQLite storage for a run.

One database per run at ``<data_dir>/<run_id>/world.db``. WAL mode, foreign keys on.
The markdown vaults are the source of truth for memory *content*; the ``notes`` table is
a rebuildable index over them.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from void.events import Event

__all__ = ["Database", "SCHEMA_VERSION", "SCHEMA"]

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
  run_id TEXT PRIMARY KEY, config_hash TEXT NOT NULL, config_yaml TEXT NOT NULL,
  seed INTEGER NOT NULL, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'running'
    CHECK (status IN ('running','completed','capped_total','extinct','inconsistent')),
  ended_tick INTEGER, ended_reason TEXT, operator_token_hash TEXT,
  experiment TEXT, arm TEXT, windfall_string TEXT
);
CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parent_id TEXT REFERENCES agents(agent_id),
  model_tier TEXT NOT NULL,
  balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
  personality_seed TEXT NOT NULL,
  memory_path TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('alive','bankrupt','archived')),
  x REAL NOT NULL, y REAL NOT NULL,
  heading REAL NOT NULL DEFAULT 0,
  entropy_budget REAL NOT NULL,
  stress REAL NOT NULL DEFAULT 0,
  asleep INTEGER NOT NULL DEFAULT 0,
  last_forage_tick INTEGER,
  last_action TEXT,
  last_action_ok INTEGER NOT NULL DEFAULT 1,
  weather_nudge_used REAL NOT NULL DEFAULT 0,
  born_tick INTEGER NOT NULL, died_tick INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
CREATE TABLE IF NOT EXISTS wallet_ledger (
  entry_id TEXT PRIMARY KEY, tick INTEGER NOT NULL, agent_id TEXT NOT NULL,
  delta INTEGER NOT NULL, balance_after INTEGER NOT NULL,
  kind TEXT NOT NULL, ref TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_agent ON wallet_ledger(agent_id, tick);
CREATE TABLE IF NOT EXISTS llm_calls (
  call_id TEXT PRIMARY KEY, tick INTEGER NOT NULL, agent_id TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT 'decide',
  tier TEXT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  real_cost INTEGER NOT NULL, world_cost INTEGER NOT NULL, hold INTEGER NOT NULL DEFAULT 0,
  estimated INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER, stop_reason TEXT,
  effective_temperature REAL, api_temperature REAL, stress REAL,
  degenerate_induced INTEGER NOT NULL DEFAULT 0, corruption_mode TEXT,
  text_coherence REAL, invalid_action INTEGER, stale_action INTEGER, perseveration INTEGER,
  action_entropy_w8 REAL, action_regret REAL, p_chosen REAL,
  claims_made INTEGER NOT NULL DEFAULT 0, claims_false INTEGER NOT NULL DEFAULT 0,
  action_type TEXT, thought TEXT, raw_text TEXT, extras TEXT,
  request_id TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_agent ON llm_calls(agent_id, tick);
CREATE INDEX IF NOT EXISTS idx_calls_tick ON llm_calls(tick);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, tick INTEGER NOT NULL, day INTEGER NOT NULL,
  kind TEXT NOT NULL, agent_id TEXT, payload TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'public'
);
CREATE INDEX IF NOT EXISTS idx_events_tick ON events(tick);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE TABLE IF NOT EXISTS notes (
  note_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, path TEXT NOT NULL,
  title TEXT NOT NULL, created_tick INTEGER NOT NULL, importance REAL NOT NULL DEFAULT 0.5,
  tags TEXT NOT NULL DEFAULT '[]', embedding BLOB NOT NULL,
  channel TEXT NOT NULL DEFAULT 'observed',
  source_agent_id TEXT, hop INTEGER NOT NULL DEFAULT 0, origin_note_id TEXT, origin_generation INTEGER,
  transfer_tick INTEGER, path_agents TEXT NOT NULL DEFAULT '[]',
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notes_agent ON notes(agent_id, archived);
CREATE TABLE IF NOT EXISTS note_links (
  from_note_id TEXT NOT NULL, to_title TEXT NOT NULL, PRIMARY KEY (from_note_id, to_title)
);
CREATE TABLE IF NOT EXISTS self_versions (
  agent_id TEXT NOT NULL, version INTEGER NOT NULL, tick INTEGER NOT NULL, summary TEXT NOT NULL,
  PRIMARY KEY (agent_id, version)
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, reward INTEGER NOT NULL,
  posted_tick INTEGER NOT NULL, status TEXT NOT NULL CHECK (status IN ('open','assigned','completed','cancelled')),
  assigned_agent_id TEXT, completed_tick INTEGER
);
CREATE TABLE IF NOT EXISTS task_applications (
  application_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  pitch TEXT NOT NULL, fee INTEGER NOT NULL, tick INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected'))
);
CREATE TABLE IF NOT EXISTS gadgets (
  gadget_id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_agent_id TEXT NOT NULL, purpose TEXT NOT NULL,
  code_path TEXT NOT NULL, test_path TEXT NOT NULL, code_hash TEXT NOT NULL, test_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('proposed','rejected','verified')),
  render_spec TEXT NOT NULL, effect_spec TEXT NOT NULL, verification TEXT NOT NULL,
  verified_without_tests INTEGER NOT NULL DEFAULT 0, template_label TEXT,
  x REAL, y REAL, created_tick INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS gadget_uses (
  gadget_id TEXT NOT NULL, agent_id TEXT NOT NULL, tick INTEGER NOT NULL, ok INTEGER NOT NULL,
  realized_effect REAL, expected_effect REAL, reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_gadget_uses ON gadget_uses(gadget_id, agent_id, tick);
CREATE TABLE IF NOT EXISTS agent_effects (
  agent_id TEXT NOT NULL, kind TEXT NOT NULL, value REAL NOT NULL, expires_tick INTEGER NOT NULL,
  PRIMARY KEY (agent_id, kind)
);
CREATE TABLE IF NOT EXISTS resource_nodes (
  node_id TEXT PRIMARY KEY, x REAL NOT NULL, y REAL NOT NULL,
  stock REAL NOT NULL, capacity REAL NOT NULL, regen_per_tick REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS world_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS control_commands (
  cmd_id INTEGER PRIMARY KEY AUTOINCREMENT, received_tick INTEGER NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, applied_tick INTEGER, result TEXT
);
CREATE TABLE IF NOT EXISTS tracer_arrivals (
  agent_id TEXT PRIMARY KEY, tick INTEGER NOT NULL, channel TEXT NOT NULL, hop INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chronicle_items (
  day INTEGER NOT NULL, item_id TEXT NOT NULL, kind TEXT NOT NULL, event_seq INTEGER, text TEXT NOT NULL,
  PRIMARY KEY (day, item_id)
);
CREATE TABLE IF NOT EXISTS snapshots (tick INTEGER PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metrics (tick INTEGER PRIMARY KEY, day INTEGER NOT NULL, json TEXT NOT NULL);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._depth = 0

    # --- transactions -----------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Nested-safe transaction: outermost wins; any exception rolls everything back."""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self.conn
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("COMMIT")

    # --- helpers ----------------------------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.fetchone("SELECT value FROM world_kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO world_kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def persist_event(self, event: Event) -> int:
        cur = self.conn.execute(
            "INSERT INTO events(tick, day, kind, agent_id, payload, visibility) VALUES(?,?,?,?,?,?)",
            (event.tick, event.day, event.kind, event.agent_id,
             json.dumps(event.payload, sort_keys=True, default=str), event.visibility),
        )
        return int(cur.lastrowid)

    def close(self) -> None:
        self.conn.close()
