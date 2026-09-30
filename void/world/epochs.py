"""Scheduled and operator-triggered epochs (DESIGN §11.4): bounded pulls on kernel-owned levers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from void.config import EpochConfig, VoidConfig
from void.db import Database
from void.events import Event, EventBus, Kind
from void.world.weather import Weather

if TYPE_CHECKING:
    from void.agents.lifecycle import Lifecycle

__all__ = ["Epochs"]


class Epochs:
    def __init__(self, cfg: VoidConfig, db: Database, weather: Weather, bus: EventBus, lifecycle: Lifecycle | None = None) -> None:
        self.cfg = cfg
        self.db = db
        self.weather = weather
        self.bus = bus
        self.lifecycle = lifecycle
        if db.kv_get("epochs_dynamic") is None:
            db.kv_set("epochs_dynamic", [])
        if db.kv_get("epoch_stack") is None:
            db.kv_set("epoch_stack", [])
        if db.kv_get("epochs_applied") is None:
            db.kv_set("epochs_applied", [])

    # --- queries ---------------------------------------------------------------------------------
    def _all(self) -> list[dict[str, Any]]:
        static = [e.model_dump(mode="json") | {"id": f"cfg{i}"} for i, e in enumerate(self.cfg.epochs)]
        dynamic = list(self.db.kv_get("epochs_dynamic", []) or [])
        return static + dynamic

    def schedule(self, epoch: EpochConfig, tick: int) -> str:
        dyn = list(self.db.kv_get("epochs_dynamic", []) or [])
        eid = f"op{len(dyn) + 1}"
        dyn.append(epoch.model_dump(mode="json") | {"id": eid, "scheduled_tick": tick})
        self.db.kv_set("epochs_dynamic", dyn)
        return eid

    def active(self) -> list[dict[str, Any]]:
        return list(self.db.kv_get("epoch_stack", []) or [])

    def scheduled(self, day: int) -> list[dict[str, Any]]:
        applied = set(self.db.kv_get("epochs_applied", []) or [])
        return [e for e in self._all() if e["id"] not in applied and int(e["day"]) >= day]

    # --- apply / expire -----------------------------------------------------------------------------
    def apply_and_expire(self, day: int, tick: int) -> None:
        stack = list(self.db.kv_get("epoch_stack", []) or [])
        # expire first so a same-day replacement epoch sees the restored baseline; the value to
        # restore is whatever the newest still-active epoch set, else the configured baseline
        remaining = [e for e in stack if int(e["ends_day"]) > day]
        expired = [e for e in stack if int(e["ends_day"]) <= day]
        if any(e.get("scarcity") is not None for e in expired):
            live = next((e["scarcity"] for e in reversed(remaining) if e.get("scarcity") is not None), self.cfg.world.scarcity)
            self.db.kv_set("scarcity", float(live))
        if any(e.get("weather_baseline") is not None for e in expired):
            live_w = next((e["weather_baseline"] for e in reversed(remaining) if e.get("weather_baseline") is not None),
                          self.cfg.world.weather.baseline)
            self.weather.set_baseline(float(live_w))
        for e in expired:
            self.bus.emit(Event(tick, day, Kind.EPOCH, {"kind": e["kind"], "id": e["id"], "phase": "ended"}))
        stack = remaining
        applied = list(self.db.kv_get("epochs_applied", []) or [])
        for e in self._all():
            if e["id"] in applied or int(e["day"]) != day:
                continue
            entry: dict[str, Any] = {"id": e["id"], "kind": e["kind"], "day": day, "ends_day": day + int(e.get("duration_days", 1)),
                                     "prev_scarcity": None, "prev_weather_baseline": None,
                                     "scarcity": e.get("scarcity"), "weather_baseline": e.get("weather_baseline")}
            if e.get("scarcity") is not None:
                entry["prev_scarcity"] = float(self.db.kv_get("scarcity", self.cfg.world.scarcity))
                self.db.kv_set("scarcity", float(e["scarcity"]))
            if e.get("weather_baseline") is not None:
                entry["prev_weather_baseline"] = self.weather.baseline
                self.weather.set_baseline(float(e["weather_baseline"]))
            arrival_id = None
            if e["kind"] == "arrival" and e.get("arrival") and self.lifecycle is not None:
                a = e["arrival"]
                rec = self.lifecycle.spawn_arrival(str(a["name"]), str(a["tier"]), float(a.get("balance_usd", 1.0)), tick, day)
                arrival_id = rec.agent_id if rec else None
                entry["arrival_agent_id"] = arrival_id
            applied.append(e["id"])
            stack.append(entry)
            self.bus.emit(Event(tick, day, Kind.EPOCH, {"kind": e["kind"], "id": e["id"], "phase": "started", "ends_day": entry["ends_day"],
                                                        "scarcity": e.get("scarcity"), "weather_baseline": e.get("weather_baseline"),
                                                        "arrival_agent_id": arrival_id}))
        self.db.kv_set("epochs_applied", applied)
        self.db.kv_set("epoch_stack", stack)

    def snapshot(self, day: int) -> dict[str, Any]:
        act = self.active()
        return {"active": act[-1] if act else None, "scheduled": self.scheduled(day)}
