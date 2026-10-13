"""The cross-run live budget: how much real provider money the runs under a directory have already spent.

Each run caps its own spend (``economy.total_cap_usd``), but nothing inside a run knows about the others, so a
sweep of seeds or a second stage could add up past what the owner authorised. This module reads every run
directory under the given roots, read-only, and adds up the real cost of the calls made on the providers being
budgeted (Anthropic by default; scripted tiers cost nothing real and are never counted). ``scripts/live_budget.py``
and ``void run --budget-usd`` both refuse to start a run whose cap would push the total over the budget.

Per run the spend is the larger of ``SUM(llm_calls.real_cost)`` and the wallet's ``spend_total`` (when every
rostered tier is on a budgeted provider; ``spend_total`` also counts a call that was metered but whose row was
never written), plus any hold still reserved when the process died (``reserved_inflight``: a call that may have
been billed but was never metered). All amounts are micro-dollars.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = ["RunSpend", "run_spend", "find_runs", "spend_under", "rostered_providers", "BUDGETED_PROVIDERS", "DB_FILE"]

DB_FILE = "world.db"
CONFIG_FILE = "config.resolved.yaml"
BUDGETED_PROVIDERS: tuple[str, ...] = ("anthropic",)


@dataclass(frozen=True)
class RunSpend:
    run_dir: Path
    real_micro: int          # what this run has spent (or may have spent) on the budgeted providers
    status: str | None       # the run's status (running, completed, capped_total, failed, inconsistent, ...)
    calls: int               # llm_calls rows on the budgeted providers
    errors: int              # of which returned an error
    inflight_micro: int      # holds still reserved when the process stopped (already inside real_micro)


def rostered_providers(cfg: dict[str, Any]) -> set[str]:
    """Providers of the tiers the run's agents start on (children never cross a provider boundary)."""
    tiers = cfg.get("tiers") or {}
    out: set[str] = set()
    for a in cfg.get("agents") or []:
        t = tiers.get(a.get("tier")) if isinstance(a, dict) else None
        if isinstance(t, dict) and t.get("provider"):
            out.add(str(t["provider"]))
    return out


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.Error:
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _kv(conn: sqlite3.Connection, key: str) -> Any:
    try:
        row = conn.execute("SELECT value FROM world_kv WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return json.loads(row["value"]) if row else None


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return 0
    return int((row[0] if row is not None else 0) or 0)


def run_spend(run_dir: str | Path, providers: Iterable[str] = BUDGETED_PROVIDERS) -> RunSpend:
    """The real spend of one run directory on ``providers`` (see the module docstring)."""
    run_dir = Path(run_dir)
    providers = tuple(providers)
    cfg: dict[str, Any] = {}
    cfg_path = run_dir / CONFIG_FILE
    if cfg_path.is_file():
        loaded = yaml.safe_load(cfg_path.read_text()) or {}
        cfg = loaded if isinstance(loaded, dict) else {}
    rostered = rostered_providers(cfg)
    marks = ",".join("?" for _ in providers) or "''"
    conn = _connect_ro(run_dir / DB_FILE)
    try:
        calls_cost = _scalar(conn, f"SELECT COALESCE(SUM(real_cost), 0) FROM llm_calls WHERE provider IN ({marks})", providers)
        calls = _scalar(conn, f"SELECT COUNT(*) FROM llm_calls WHERE provider IN ({marks})", providers)
        errors = _scalar(conn, f"SELECT COUNT(*) FROM llm_calls WHERE provider IN ({marks}) AND error IS NOT NULL", providers)
        lost = _scalar(conn, "SELECT COALESCE(SUM(CAST(json_extract(payload, '$.real_cost') AS INTEGER)), 0) "
                             "FROM wallet_ledger WHERE kind='inflight_lost'")
        spend_total = int(_kv(conn, "spend_total") or 0)
        inflight = int(_kv(conn, "reserved_inflight") or 0)
        try:
            row = conn.execute("SELECT status FROM run LIMIT 1").fetchone()
            status = str(row["status"]) if row is not None else None
        except sqlite3.Error:
            status = None
    finally:
        conn.close()
    budgeted = bool(rostered & set(providers))
    if not budgeted and not rostered:  # no readable config: trust the call rows alone
        real = calls_cost
    elif not budgeted:
        real, inflight = calls_cost, 0
    elif rostered <= set(providers):
        real = max(spend_total, calls_cost + lost) + inflight
    else:  # a mixed roster: spend_total also holds the scripted or other-provider tiers' cost
        real = calls_cost + lost + inflight
    return RunSpend(run_dir, real, status, calls, errors, inflight)


def find_runs(roots: Iterable[str | Path]) -> list[Path]:
    """Every run directory (one holding a ``world.db``) under the roots, each once."""
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        candidates = [root] if (root / DB_FILE).is_file() else []
        candidates += [p.parent for p in sorted(root.rglob(DB_FILE))]
        for d in candidates:
            key = d.resolve()
            if key not in seen:
                seen.add(key)
                out.append(d)
    return out


def spend_under(roots: Iterable[str | Path], providers: Iterable[str] = BUDGETED_PROVIDERS) -> tuple[int, list[RunSpend]]:
    """``(total micro-dollars, per-run rows)`` over every run directory under ``roots``."""
    rows = [run_spend(d, providers) for d in find_runs(roots)]
    return sum(r.real_micro for r in rows), rows
