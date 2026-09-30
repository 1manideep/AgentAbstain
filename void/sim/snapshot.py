"""World snapshots and roster messages for the renderer (DESIGN §15)."""

from __future__ import annotations

import json
from typing import Any

from void.agents.registry import AgentRegistry
from void.config import VoidConfig, micro_to_usd
from void.db import Database
from void.types import AgentStatus

__all__ = ["build_snapshot", "roster", "gadgets_message", "AGENT_ANIMS"]

AGENT_ANIMS = ("idle", "walk", "talk", "sleep", "dead")


def _anim(status: str, asleep: bool, last_action: str | None) -> str:
    if status != "alive":
        return "dead"
    if asleep:
        return "sleep"
    if last_action == "move":
        return "walk"
    if last_action in ("talk", "share_note"):
        return "talk"
    return "idle"


def roster(registry: AgentRegistry) -> list[dict[str, Any]]:
    return [
        {"id": a.agent_id, "name": a.name, "tier": a.model_tier, "generation": a.generation, "parent_id": a.parent_id,
         "born_tick": a.born_tick, "died_tick": a.died_tick, "status": a.status.value}
        for a in registry.all()
    ]


def gadgets_message(gadgets: list[Any], rev: int) -> dict[str, Any]:
    items = [{"id": g.gadget_id, "name": g.name, "owner": g.owner_agent_id, "x": g.x, "y": g.y, "render": g.render, "uses": g.uses}
             for g in gadgets if g.status == "verified" and g.x is not None]
    return {"type": "gadgets", "rev": rev, "items": items}


def build_snapshot(cfg: VoidConfig, db: Database, registry: AgentRegistry, *, tick: int, day: int, tick_of_day: int,
                   weather: float, scarcity: float, totals: dict[str, int], nodes: list[dict[str, Any]],
                   calls_this_tick: dict[str, dict[str, Any]], revs: dict[str, int], epochs: dict[str, Any],
                   paused: bool, status: str) -> dict[str, Any]:
    agents = []
    for a in registry.all():
        if a.status == AgentStatus.ARCHIVED and a.died_tick != tick:
            continue
        call = calls_this_tick.get(a.agent_id, {})
        try:
            la = json.loads(a.last_action) if a.last_action else None
        except ValueError:
            la = None
        last_type = la.get("type") if isinstance(la, dict) else None
        agents.append({
            "id": a.agent_id, "x": round(a.pos.x, 3), "y": round(a.pos.y, 3), "heading": round(a.heading, 3),
            "stress": round(a.stress, 4), "t_eff": call.get("t_eff"), "degenerate": bool(call.get("degenerate", False)),
            "asleep": a.asleep, "status": a.status.value, "balance_usd": round(micro_to_usd(a.balance), 4),
            "anim": _anim(a.status.value, a.asleep, last_type),
            "last_action": {"type": last_type, "target": la.get("target") if isinstance(la, dict) else None, "ok": a.last_action_ok},
            "effects": sorted(a.effects.keys()),
        })
    return {
        "type": "snapshot", "tick": tick, "day": day, "tick_of_day": tick_of_day, "paused": paused, "status": status,
        "weather": round(weather, 4), "scarcity": round(scarcity, 4),
        "spend_today_usd": round(micro_to_usd(totals["spend_today"]), 4), "spend_total_usd": round(micro_to_usd(totals["spend_total"]), 4),
        "spawn_pool_usd": round(micro_to_usd(totals["spawn_pool"]), 4), "house_usd": round(micro_to_usd(totals.get("house", 0)), 4),
        "population": registry.count_alive(), "gadgets_rev": revs.get("gadgets", 0), "tasks_rev": revs.get("tasks", 0),
        "chronicle_rev": revs.get("chronicle", 0), "epochs": epochs, "agents": agents, "nodes": nodes,
    }
