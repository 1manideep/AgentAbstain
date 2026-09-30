"""Shared fixtures for the backend suite: deterministic scripted runs, no network, tmp run dirs.

``make_sim`` builds a :class:`Simulation` from ``configs/scripted_smoke.yaml`` plus deep-merged
overrides in a fresh temporary run directory; ``run_sim`` drives one to completion and wraps the
result in :class:`SimRun` with small query helpers. Both are session scoped so module fixtures can
share a single run per file (DESIGN §19 asks for 6 scripted agents over 3 days, about 3 s each).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from void.config import VoidConfig, load_config
from void.sim.loop import Simulation, TickReport

REPO = Path(__file__).resolve().parents[1]
SMOKE = REPO / "configs" / "scripted_smoke.yaml"

# Children never mutate onto a provider tier: the suite runs without credentials and must never call out.
NO_PROVIDER_TIERS: dict[str, Any] = {"population": {"tier_mutation_prob": 0.0}}


@pytest.fixture(scope="session", autouse=True)
def _no_provider_credentials():
    """Strip provider credentials for the whole session so no test can reach a model API."""
    with pytest.MonkeyPatch.context() as mp:
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            mp.delenv(key, raising=False)
        yield


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class SimRun:
    cfg: VoidConfig
    sim: Simulation
    run_dir: Path
    status: str
    reports: list[TickReport] = field(default_factory=list)

    @property
    def db(self):
        return self.sim.db

    def events(self, kind: str | None = None, *, visibility: str | None = None) -> list[dict[str, Any]]:
        """Events in seq order with the payload parsed; optionally filtered by kind and visibility."""
        sql = "SELECT seq, tick, day, kind, agent_id, payload, visibility FROM events WHERE 1=1"
        params: list[Any] = []
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        if visibility is not None:
            sql += " AND visibility=?"
            params.append(visibility)
        rows = self.db.fetchall(sql + " ORDER BY seq", tuple(params))
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def event_kinds(self) -> dict[str, int]:
        return {r["kind"]: int(r["n"]) for r in self.db.fetchall("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind")}

    def metrics_rows(self) -> list[dict[str, Any]]:
        with (self.run_dir / "metrics.jsonl").open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def tick_rows(self) -> list[dict[str, Any]]:
        return [r for r in self.metrics_rows() if r.get("kind") != "day"]

    def day_rows(self) -> list[dict[str, Any]]:
        return [r for r in self.metrics_rows() if r.get("kind") == "day"]

    def action_types(self) -> dict[str, int]:
        rows = self.db.fetchall("SELECT action_type, COUNT(*) AS n FROM llm_calls WHERE action_type IS NOT NULL GROUP BY action_type")
        return {r["action_type"]: int(r["n"]) for r in rows}

    def kv(self, key: str, default: Any = None) -> Any:
        return self.db.kv_get(key, default)


@pytest.fixture(scope="session")
def make_sim(tmp_path_factory) -> Callable[..., Simulation]:
    """Build a Simulation in a fresh temp run dir from the smoke config plus deep-merged overrides."""

    def build(name: str, overrides: dict[str, Any] | None = None, *, base: Path = SMOKE) -> Simulation:
        run_dir = tmp_path_factory.mktemp(name)
        cfg = load_config(base, deep_merge({"run": {"name": name}}, overrides or {}))
        return Simulation(cfg, run_dir)

    return build


@pytest.fixture(scope="session")
def run_sim(make_sim) -> Callable[..., SimRun]:
    """Run a Simulation to its end state; ``on_tick(sim, report)`` may enqueue operator commands."""

    def run(name: str, overrides: dict[str, Any] | None = None, *,
            on_tick: Callable[[Simulation, TickReport], None] | None = None, max_ticks: int | None = None) -> SimRun:
        sim = make_sim(name, overrides)
        reports: list[TickReport] = []

        def hook(report: TickReport) -> None:
            reports.append(report)
            if on_tick is not None:
                on_tick(sim, report)

        status = asyncio.run(sim.run(max_ticks=max_ticks, on_tick=hook))
        return SimRun(sim.cfg, sim, sim.run_dir, status, reports)

    return run
