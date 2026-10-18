"""Per-tick and per-day metrics (DESIGN §17): population aggregates plus by_tier / by_generation breakdowns.

Tick rows carry, besides the population aggregates, the gadget fields ``gadget_proposals`` (gadgets created this
tick), ``gadgets_verified`` (cumulative, status verified) and ``gadget_use_failure_rate`` (cumulative failed / total
uses, ``None`` before the first use). Every ``by_tier.<t>`` and ``by_generation.<g>`` group also carries:

* ``gossip_transfers``: this tick's ``gossip_transfer`` events whose receiving agent (``listener_id``) is in the group;
* ``notes_by_channel``: live notes of the group's living agents, every channel in ``CHANNELS`` present;
* ``tracer_reach_by_channel``: ``tracer_arrivals`` rows of the group's living agents by channel, ``None`` when
  ``experiment.tracer`` is disabled;
* ``tracer_adopters`` and ``probe_recall``: always ``None``. Both are population-wide only (adoption is read from
  the forage pattern of the whole world, probes are accumulated by the loop) and live on the row itself;
* ``gadget_proposals``, ``gadgets_verified``: as on the row, for gadgets whose owner is in the group;
  ``gadget_use_failure_rate``: as on the row, for uses by an agent in the group;
* ``stress_bins``: ten bins ``{"lo", "calls", "coherence_sum", "coherence_n", "invalid_sum"}`` of this tick's decide
  calls, binned by each call's own ``llm_calls.stress`` (bin ``min(9, floor(clamp(stress, 0, 0.999) * 10))``);
  coherence sums only calls with a ``text_coherence``. The degradation-vs-stress chart pools these over ticks, so a
  group's mean stress never stands in for the stress each call ran at (DESIGN §16).

Group membership of an owner, user or receiver is read from the ``agents`` table (dead agents keep their tier and
generation). The groups present are those of the living agents, of this tick's calls and of this tick's gossip
receivers.

Day rows add ``link_density`` (DESIGN §10: the mean over living agents of outgoing links per live note, 0 for an
agent without notes, exactly what ``MemoryStore.link_density`` computes), ``vault`` (the population's vault
structure, :mod:`void.memory.vault_stats`) and ``vault_by_agent`` (the same keys for each living agent).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from void.agents.registry import AgentRegistry
from void.config import VoidConfig, micro_to_usd
from void.db import Database
from void.memory.notes import CHANNELS
from void.memory.vault_stats import vault_report

__all__ = ["MetricsRecorder", "STRESS_BINS", "stress_bin", "stress_bins"]

STRESS_BINS = 10


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


def stress_bin(stress: float) -> int:
    """The stress bin of one call: ``min(9, floor(clamp(stress, 0, 0.999) * 10))``."""
    return min(STRESS_BINS - 1, math.floor(min(max(float(stress), 0.0), 0.999) * STRESS_BINS))


def stress_bins(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ten bins of decide calls by each call's own stress; a call without a recorded stress is left out."""
    bins: list[dict[str, Any]] = [{"lo": round(i / STRESS_BINS, 1), "calls": 0, "coherence_sum": 0.0, "coherence_n": 0,
                                   "invalid_sum": 0} for i in range(STRESS_BINS)]
    for c in calls:
        if c.get("stress") is None:
            continue
        b = bins[stress_bin(c["stress"])]
        b["calls"] += 1
        b["invalid_sum"] += int(c.get("invalid_action") or 0)
        if c.get("text_coherence") is not None:
            b["coherence_sum"] += float(c["text_coherence"])
            b["coherence_n"] += 1
    for b in bins:
        b["coherence_sum"] = round(b["coherence_sum"], 6)
    return bins


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

    def _link_density(self, agent_ids: list[str]) -> float | None:
        """Mean over ``agent_ids`` of ``MemoryStore.link_density``: note_links rows from live notes per live note."""
        notes = {str(r["agent_id"]): int(r["n"]) for r in self.db.fetchall(
            "SELECT agent_id, COUNT(*) AS n FROM notes WHERE archived=0 GROUP BY agent_id ORDER BY agent_id")}
        links = {str(r["agent_id"]): int(r["n"]) for r in self.db.fetchall(
            "SELECT n.agent_id AS agent_id, COUNT(*) AS n FROM note_links l JOIN notes n ON n.note_id=l.from_note_id "
            "WHERE n.archived=0 GROUP BY n.agent_id ORDER BY n.agent_id")}
        return _mean([links.get(aid, 0) / notes[aid] if notes.get(aid) else 0.0 for aid in agent_ids])

    def _events_count(self, tick: int, kind: str) -> int:
        return int(self.db.fetchone("SELECT COUNT(*) AS n FROM events WHERE tick=? AND kind=?", (tick, kind))["n"])

    def _tick_context(self, tick: int, agents: list[Any]) -> dict[str, Any]:
        """Per-agent facts the group breakdowns share, read once per tick (every query in a fixed order)."""
        alive = {a.agent_id for a in agents}
        attrs = {str(r["agent_id"]): {"tier": str(r["model_tier"]), "generation": str(r["generation"])}
                 for r in self.db.fetchall("SELECT agent_id, model_tier, generation FROM agents ORDER BY agent_id")}
        receivers: list[str] = []
        for r in self.db.fetchall("SELECT payload FROM events WHERE tick=? AND kind='gossip_transfer' ORDER BY seq", (tick,)):
            listener = json.loads(r["payload"]).get("listener_id")
            if listener:
                receivers.append(str(listener))
        notes: dict[str, dict[str, int]] = {}
        for r in self.db.fetchall("SELECT agent_id, channel, COUNT(*) AS n FROM notes WHERE archived=0 "
                                  "GROUP BY agent_id, channel ORDER BY agent_id, channel"):
            if str(r["agent_id"]) in alive:
                notes.setdefault(str(r["agent_id"]), {})[str(r["channel"])] = int(r["n"])
        tracer: dict[str, str] | None = None
        if self.cfg.experiment.tracer.enabled:
            tracer = {str(r["agent_id"]): str(r["channel"])
                      for r in self.db.fetchall("SELECT agent_id, channel FROM tracer_arrivals ORDER BY agent_id")
                      if str(r["agent_id"]) in alive}
        proposed = [str(r["owner_agent_id"]) for r in
                    self.db.fetchall("SELECT owner_agent_id FROM gadgets WHERE created_tick=? ORDER BY gadget_id", (tick,))]
        verified = [str(r["owner_agent_id"]) for r in
                    self.db.fetchall("SELECT owner_agent_id FROM gadgets WHERE status='verified' ORDER BY gadget_id")]
        uses = {str(r["agent_id"]): (int(r["n"]), int(r["ok"])) for r in self.db.fetchall(
            "SELECT agent_id, COUNT(*) AS n, COALESCE(SUM(ok),0) AS ok FROM gadget_uses GROUP BY agent_id ORDER BY agent_id")}
        return {"attrs": attrs, "receivers": receivers, "notes": notes, "tracer": tracer,
                "proposed": proposed, "verified": verified, "uses": uses}

    def _breakdown(self, calls: list[dict[str, Any]], agents: list[Any], key: str,
                   ctx: dict[str, Any]) -> dict[str, dict[str, Any]]:
        attrs: dict[str, dict[str, str]] = ctx["attrs"]

        def group_of(agent_id: str) -> str | None:
            a = attrs.get(agent_id)
            return None if a is None else a[key]

        groups: dict[str, dict[str, Any]] = {}
        for a in agents:
            k = str(getattr(a, "model_tier" if key == "tier" else "generation"))
            g = groups.setdefault(k, {"population": 0, "balances": [], "stress": [], "members": []})
            g["population"] += 1
            g["balances"].append(micro_to_usd(a.balance))
            g["stress"].append(a.stress)
            g["members"].append(a.agent_id)
        by_call: dict[str, list[dict[str, Any]]] = {}
        for c in calls:
            k = str(c["tier"] if key == "tier" else c.get("generation"))
            by_call.setdefault(k, []).append(c)
        receivers = [k for k in (group_of(aid) for aid in ctx["receivers"]) if k is not None]
        out: dict[str, dict[str, Any]] = {}
        for k in sorted(set(groups) | set(by_call) | set(receivers)):
            g = groups.get(k, {"population": 0, "balances": [], "stress": [], "members": []})
            cs = by_call.get(k, [])
            claims_made = sum(int(c["claims_made"] or 0) for c in cs)
            claims_false = sum(int(c["claims_false"] or 0) for c in cs)
            members = sorted(g["members"])
            notes_by_channel = {c: 0 for c in CHANNELS}
            for aid in members:
                for ch, n in ctx["notes"].get(aid, {}).items():
                    notes_by_channel[ch] = notes_by_channel.get(ch, 0) + n
            tracer_reach: dict[str, int] | None = None
            if ctx["tracer"] is not None:
                tracer_reach = {}
                for aid in members:
                    ch = ctx["tracer"].get(aid)
                    if ch is not None:
                        tracer_reach[ch] = tracer_reach.get(ch, 0) + 1
            used = [ctx["uses"][aid] for aid in sorted(ctx["uses"]) if group_of(aid) == k]
            uses_n = sum(n for n, _ in used)
            uses_ok = sum(ok for _, ok in used)
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
                "gossip_transfers": sum(1 for r in receivers if r == k),
                "notes_by_channel": notes_by_channel,
                "tracer_reach_by_channel": tracer_reach,
                "tracer_adopters": None,  # population-wide only (module docstring)
                "gadget_proposals": sum(1 for aid in ctx["proposed"] if group_of(aid) == k),
                "gadgets_verified": sum(1 for aid in ctx["verified"] if group_of(aid) == k),
                "gadget_use_failure_rate": _rate(uses_n - uses_ok, uses_n),
                "probe_recall": None,  # population-wide only (module docstring)
                "stress_bins": stress_bins(cs),
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
        ctx = self._tick_context(tick, agents)
        uses = self.db.fetchone("SELECT COALESCE(SUM(ok),0) AS ok, COUNT(*) AS n FROM gadget_uses")
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
            "gadget_proposals": len(ctx["proposed"]), "gadgets_verified": len(ctx["verified"]),
            "gadget_use_failure_rate": _rate(int(uses["n"]) - int(uses["ok"]), int(uses["n"])),
            "by_tier": self._breakdown(calls, agents, "tier", ctx),
            "by_generation": self._breakdown(calls, agents, "generation", ctx),
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
        notes_by_channel = {c: 0 for c in CHANNELS}
        for r in self.db.fetchall("SELECT channel, COUNT(*) AS n FROM notes WHERE archived=0 GROUP BY channel ORDER BY channel"):
            notes_by_channel[r["channel"]] = int(r["n"])
        gadget_rows = self.db.fetchall("SELECT status, COUNT(*) AS n FROM gadgets GROUP BY status ORDER BY status")
        uses = self.db.fetchone("SELECT COALESCE(SUM(ok),0) AS ok, COUNT(*) AS n FROM gadget_uses")
        living = sorted(a.agent_id for a in self.registry.alive())
        vault, vault_by_agent = vault_report(self.db, living)
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
            "link_density": self._link_density(living),
            "vault": vault, "vault_by_agent": vault_by_agent,
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
