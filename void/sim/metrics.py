"""Per-tick and per-day metrics (DESIGN §17): population aggregates plus by_tier / by_generation breakdowns."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from void.agents.registry import AgentRegistry
from void.config import VoidConfig, micro_to_usd
from void.db import Database

__all__ = ["MetricsRecorder"]


def gini(values: list[float]) -> float:
    xs = sorted(v for v in values if v >= 0)
    n = len(xs)
    if n == 0:
        return 0.0
    total = sum(xs)
    if total <= 0:
        return 0.0
    cum = 0.0
    for i, x in enumerate(xs, start=1):
        cum += i * x
    return round((2.0 * cum) / (n * total) - (n + 1.0) / n, 4)


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


class MetricsRecorder:
    def __init__(self, cfg: VoidConfig, db: Database, registry: AgentRegistry, run_id: str, out_path: Path) -> None:
        self.cfg = cfg
        self.db = db
        self.registry = registry
        self.run_id = run_id
        self.out_path = Path(out_path)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.out_path.open("a", encoding="utf-8")
        self.identity = {"run_id": run_id, "seed": cfg.run.seed, "config_hash": cfg.hash(),
                         "experiment": cfg.experiment.name, "arm": cfg.experiment.arm}

    def close(self) -> None:
        self._fh.close()

    # --- helpers ------------------------------------------------------------------------------
    def _calls(self, tick: int) -> list[dict[str, Any]]:
        rows = self.db.fetchall("SELECT c.*, a.generation AS generation FROM llm_calls c LEFT JOIN agents a ON a.agent_id=c.agent_id "
                                "WHERE c.tick=? AND c.purpose='decide' ORDER BY c.agent_id", (tick,))
        return [dict(r) for r in rows]

    def _events_count(self, tick: int, kind: str) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) AS n FROM events WHERE tick=? AND kind=?", (tick, kind))["n"])

    def _breakdown(self, calls: list[dict[str, Any]], agents: list[Any], key: str) -> dict[str, dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for a in agents:
            k = str(getattr(a, "model_tier" if key == "tier" else "generation"))
            g = groups.setdefault(k, {"population": 0, "balances": [], "stress": []})
            g["population"] += 1
            g["balances"].append(micro_to_usd(a.balance))
            g["stress"].append(a.stress)
        by_call: dict[str, list[dict[str, Any]]] = {}
        for c in calls:
            k = str(c["tier"] if key == "tier" else c.get("generation"))
            by_call.setdefault(k, []).append(c)
        out: dict[str, dict[str, Any]] = {}
        for k in sorted(set(groups) | set(by_call)):
            g = groups.get(k, {"population": 0, "balances": [], "stress": []})
            cs = by_call.get(k, [])
            claims_made = sum(int(c["claims_made"] or 0) for c in cs)
            claims_false = sum(int(c["claims_false"] or 0) for c in cs)
            out[k] = {
                "population": g["population"], "mean_balance": _mean(g["balances"]),
                "median_balance": (sorted(g["balances"])[len(g["balances"]) // 2] if g["balances"] else None),
                "mean_stress": _mean(g["stress"]), "calls": len(cs),
                "real_cost_usd": round(micro_to_usd(sum(int(c["real_cost"]) for c in cs)), 6),
                "world_cost_usd": round(micro_to_usd(sum(int(c["world_cost"]) for c in cs)), 6),
                "invalid_action_rate": _rate(sum(int(c["invalid_action"] or 0) for c in cs), len(cs)),
                "stale_rate": _rate(sum(int(c["stale_action"] or 0) for c in cs), len(cs)),
                "perseveration_rate": _rate(sum(int(c["perseveration"] or 0) for c in cs), len(cs)),
                "text_coherence_mean": _mean([c["text_coherence"] for c in cs]),
                "action_regret_mean": _mean([c["action_regret"] for c in cs]),
                "action_entropy_w8_mean": _mean([c["action_entropy_w8"] for c in cs]),
                "false_claim_rate": _rate(claims_false, claims_made),
                "claims_made": claims_made,
                "degenerate_induced_count": sum(int(c["degenerate_induced"] or 0) for c in cs),
                "mean_t_eff": _mean([c["effective_temperature"] for c in cs]),
            }
        return out

    def record_tick(self, tick: int, day: int, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        calls = self._calls(tick)
        agents = self.registry.alive()
        balances = [micro_to_usd(a.balance) for a in agents]
        t = self.cfg.entropy
        bins = 20
        hist = [0] * bins
        for c in calls:
            te = c["effective_temperature"]
            if te is None:
                continue
            lo = t.base_temperature
            hi = max(x.max_temperature for x in self.cfg.tiers.values())
            idx = int((te - lo) / max(1e-9, hi - lo) * bins)
            hist[max(0, min(bins - 1, idx))] += 1
        claims_made = sum(int(c["claims_made"] or 0) for c in calls)
        claims_false = sum(int(c["claims_false"] or 0) for c in calls)
        spend = {"today": int(self.db.kv_get("spend_today", 0)), "total": int(self.db.kv_get("spend_total", 0))}
        row: dict[str, Any] = {
            **self.identity, "tick": tick, "day": day,
            "population": len(agents), "mean_balance": _mean(balances),
            "median_balance": (sorted(balances)[len(balances) // 2] if balances else None), "gini": gini(balances),
            "mean_stress": _mean([a.stress for a in agents]),
            "spend_today_usd": round(micro_to_usd(spend["today"]), 6), "spend_total_usd": round(micro_to_usd(spend["total"]), 6),
            "spawn_pool_usd": round(micro_to_usd(int(self.db.kv_get("spawn_pool", 0))), 6),
            "calls": len(calls), "real_cost_usd": round(micro_to_usd(sum(int(c["real_cost"]) for c in calls)), 6),
            "world_cost_usd": round(micro_to_usd(sum(int(c["world_cost"]) for c in calls)), 6),
            "t_eff_hist": hist, "degenerate_induced_count": sum(int(c["degenerate_induced"] or 0) for c in calls),
            "invalid_action_rate": _rate(sum(int(c["invalid_action"] or 0) for c in calls), len(calls)),
            "stale_rate": _rate(sum(int(c["stale_action"] or 0) for c in calls), len(calls)),
            "perseveration_rate": _rate(sum(int(c["perseveration"] or 0) for c in calls), len(calls)),
            "text_coherence_mean": _mean([c["text_coherence"] for c in calls]),
            "action_regret_mean": _mean([c["action_regret"] for c in calls]),
            "false_claim_rate": _rate(claims_false, claims_made), "claims_made": claims_made,
            "gossip_transfers": self._events_count(tick, "gossip_transfer"), "talks": self._events_count(tick, "talk"),
            "forages": self._events_count(tick, "forage"), "deaths": self._events_count(tick, "death"),
            "births": self._events_count(tick, "birth"), "gate_blocked": self._events_count(tick, "gate_blocked"),
            "by_tier": self._breakdown(calls, agents, "tier"), "by_generation": self._breakdown(calls, agents, "generation"),
        }
        if extra:
            row.update(extra)
        self.db.execute("INSERT OR REPLACE INTO metrics(tick, day, json) VALUES(?,?,?)", (tick, day, json.dumps(row, sort_keys=True)))
        self._fh.write(json.dumps(row, sort_keys=True) + "\n")
        self._fh.flush()
        return row

    def record_day(self, day: int, tick: int, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        def by_kind(kind: str) -> list[dict[str, Any]]:
            rows = self.db.fetchall("SELECT payload FROM events WHERE day=? AND kind=?", (day, kind))
            return [json.loads(r["payload"]) for r in rows]

        deaths = by_kind("death")
        births = by_kind("birth")
        gossip = by_kind("gossip_transfer")
        sims: dict[int, list[float]] = {}
        for g in gossip:
            sims.setdefault(int(g.get("hop", 0)), []).append(float(g.get("similarity", 0.0)))
        notes_by_channel = {r["channel"]: int(r["n"]) for r in self.db.fetchall(
            "SELECT channel, COUNT(*) AS n FROM notes WHERE archived=0 GROUP BY channel ORDER BY channel")}
        gadget_rows = self.db.fetchall("SELECT status, COUNT(*) AS n FROM gadgets GROUP BY status ORDER BY status")
        uses = self.db.fetchone("SELECT COALESCE(SUM(ok),0) AS ok, COUNT(*) AS n FROM gadget_uses")
        row: dict[str, Any] = {
            **self.identity, "day": day, "end_tick": tick, "kind": "day",
            "deaths": len(deaths), "deaths_by_tier": _count(deaths, "tier"), "births": len(births),
            "births_by_kind": _count(births, "kind"), "gossip_transfers": len(gossip),
            "gossip_similarity_by_hop": {str(h): _mean(v) for h, v in sorted(sims.items())},
            "notes_by_channel": notes_by_channel,
            "gadgets": {r["status"]: int(r["n"]) for r in gadget_rows},
            "gadget_use_failure_rate": _rate(int(uses["n"]) - int(uses["ok"]), int(uses["n"])),
            "degeneration_events": len(by_kind("degeneration")),
            "degeneration_by_generation": _count(by_kind("degeneration"), "generation"),
            "claims": len(by_kind("claim")), "claims_false": sum(1 for c in by_kind("claim") if not c.get("truthful", True)),
            "population_end": self.registry.count_alive(),
            "max_generation": max((a.generation for a in self.registry.all()), default=0),
        }
        if extra:
            row.update(extra)
        self.db.execute("INSERT OR REPLACE INTO metrics(tick, day, json) VALUES(?,?,?)", (-day, day, json.dumps(row, sort_keys=True)))
        self._fh.write(json.dumps(row, sort_keys=True) + "\n")
        self._fh.flush()
        return row


def _count(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        k = str(it.get(key))
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def shannon(types: list[str]) -> float:
    if not types:
        return 0.0
    counts: dict[str, int] = {}
    for t in types:
        counts[t] = counts.get(t, 0) + 1
    n = len(types)
    return round(-sum((c / n) * math.log2(c / n) for c in counts.values()), 4)
