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

Run directories live on one machine and ``data/`` is not committed, so a fresh clone or a replaced container would
see nothing spent. The committed spend ledger (:data:`LEDGER_FILE`, one JSON object per line, written by
``scripts/live_claude.sh`` before and after every paid run) is the record that survives: an attempt counts at its
cap while it is only ``reserved`` and at its real spend once it is ``final``. :func:`spend_under` takes, per run
id, the larger of the ledger and the run directories found on disk, so a run still counts when its directory is
gone or was moved outside the roots.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = ["RunSpend", "run_spend", "find_runs", "spend_under", "rostered_providers", "reachable_providers",
           "read_ledger", "ledger_spend", "BUDGETED_PROVIDERS", "DB_FILE", "LEDGER_FILE"]

DB_FILE = "world.db"
CONFIG_FILE = "config.resolved.yaml"
BUDGETED_PROVIDERS: tuple[str, ...] = ("anthropic",)
LEDGER_FILE = Path(__file__).resolve().parents[2] / "docs" / "research" / "live_spend.jsonl"


@dataclass(frozen=True)
class RunSpend:
    run_dir: Path
    real_micro: int          # what this run has spent (or may have spent) on the budgeted providers
    status: str | None       # the run's status (running, completed, capped_total, failed, inconsistent, ...)
    calls: int               # llm_calls rows on the budgeted providers
    errors: int              # of which returned an error
    inflight_micro: int      # holds still reserved when the process stopped (already inside real_micro)
    run_id: str | None = None  # from the run table: the key the spend ledger uses


def rostered_providers(cfg: dict[str, Any]) -> set[str]:
    """Providers of the tiers the run's agents start on (children never cross a provider boundary)."""
    tiers = cfg.get("tiers") or {}
    out: set[str] = set()
    for a in cfg.get("agents") or []:
        t = tiers.get(a.get("tier")) if isinstance(a, dict) else None
        if isinstance(t, dict) and t.get("provider"):
            out.add(str(t["provider"]))
    return out


def reachable_providers(cfg: Any) -> set[str]:
    """Paid providers a run of ``cfg`` (a VoidConfig) can call: the rostered tiers' (children keep their parent's
    provider) and, with an LLM chronicle writer, every configured provider tier (the chronicle falls back to the
    first one when no rostered tier is a provider). Scripted tiers cost nothing and are left out."""
    out = {cfg.tiers[a.tier].provider for a in cfg.agents if a.tier and a.tier in cfg.tiers}
    if getattr(cfg.chronicle, "writer", "template") == "llm":
        out |= {t.provider for t in cfg.tiers.values()}
    return out - {"scripted"}


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
            row = conn.execute("SELECT run_id, status FROM run LIMIT 1").fetchone()
            status = str(row["status"]) if row is not None else None
            run_id = str(row["run_id"]) if row is not None else None
        except sqlite3.Error:
            status = run_id = None
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
    return RunSpend(run_dir, real, status, calls, errors, inflight, run_id)


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


def read_ledger(path: str | Path) -> dict[str, dict[str, Any]]:
    """The attempts recorded in the spend ledger, by attempt id, each ``reserved`` line merged with its later
    ``final`` line. A missing file raises FileNotFoundError and a malformed line ValueError: an unreadable record
    must never read as nothing spent."""
    attempts: dict[str, dict[str, Any]] = {}
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{n}: not JSON ({e})") from e
        if not isinstance(entry, dict) or not entry.get("attempt") or not entry.get("run_id") \
                or entry.get("state") not in ("reserved", "final"):
            raise ValueError(f"{path}:{n}: a ledger line needs attempt, run_id and a state of reserved or final")
        attempts.setdefault(str(entry["attempt"]), {}).update(entry)
    return attempts


def ledger_spend(path: str | Path, exclude_attempt: str | None = None) -> dict[str, int]:
    """Micro-dollars per run id from the ledger: a ``final`` attempt at its real spend, an attempt that is still
    only ``reserved`` (running, or its process or machine died before it was recorded) at its full cap.
    ``exclude_attempt`` leaves out the reservation of the run about to start, whose cap the caller adds itself."""
    out: dict[str, int] = {}
    for attempt, e in read_ledger(path).items():
        if attempt == exclude_attempt:
            continue
        micro = int(e.get("real_micro") or 0) if e["state"] == "final" else int(e.get("cap_micro") or 0)
        out[str(e["run_id"])] = out.get(str(e["run_id"]), 0) + micro
    return out


def spend_under(roots: Iterable[str | Path], providers: Iterable[str] = BUDGETED_PROVIDERS, *,
                ledger: str | Path | None = None, exclude_attempt: str | None = None) -> tuple[int, list[RunSpend]]:
    """``(total micro-dollars, per-run rows)`` over every run directory under ``roots`` and, with ``ledger``, every
    attempt recorded there: per run id the larger of the two (see the module docstring)."""
    rows = [run_spend(d, providers) for d in find_runs(roots)]
    if ledger is None:
        return sum(r.real_micro for r in rows), rows
    on_disk: dict[str, int] = {}
    unnamed = 0
    for r in rows:
        if r.run_id is None:
            unnamed += r.real_micro
        else:
            on_disk[r.run_id] = on_disk.get(r.run_id, 0) + r.real_micro
    recorded = ledger_spend(ledger, exclude_attempt)
    return unnamed + sum(max(on_disk.get(k, 0), recorded.get(k, 0)) for k in on_disk.keys() | recorded.keys()), rows
