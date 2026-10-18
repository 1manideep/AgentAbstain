"""Scheduled and operator-triggered epochs (DESIGN §11.4): bounded pulls on kernel-owned levers.

An epoch moves up to two levers: ``scarcity`` (the multiplier on forage value) and ``weather_baseline`` (the
value the weather decays toward). Applying one pushes the previous values on ``world_kv.epoch_stack``; expiry at
``day + duration_days`` restores whatever the newest still-active epoch set, else the configured baseline.

Presets. The kinds ``drought``, ``storm`` and ``boom`` carry default levers, filled in for any lever the epoch
leaves ``None``. An explicit value always wins, and a lever without a preset ("-") stays as the epoch gives it:

    =========  ===============  =======================
    kind       scarcity preset  weather_baseline preset
    =========  ===============  =======================
    drought    0.5              -
    storm      -                -0.6
    boom       1.5              -
    arrival    -                -
    custom     -                -
    =========  ===============  =======================

So a bare ``POST /api/control/epoch {"kind": "storm"}`` pulls the weather baseline to -0.6 for one day.
Presets are resolved whenever an epoch is read (the scheduled list, apply) and never written back: the stored
epoch stays as configured or posted, while the stack entry records the effective levers, so expiry and resume
restore through the stack exactly as they do for explicit values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from void.config import EpochConfig, VoidConfig
from void.db import Database
from void.events import Event, EventBus, Kind
from void.world.weather import Weather

if TYPE_CHECKING:
    from void.agents.lifecycle import Lifecycle

__all__ = ["PRESETS", "Epochs", "with_presets"]

# default levers per kind (table in the module docstring); arrival and custom have none
PRESETS: dict[str, dict[str, float]] = {
    "drought": {"scarcity": 0.5},
    "storm": {"weather_baseline": -0.6},
    "boom": {"scarcity": 1.5},
}


def with_presets(epoch: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``epoch`` with its kind's preset filled into every lever it leaves ``None``; explicit values win."""
    out = dict(epoch)
    for lever, value in PRESETS.get(str(out.get("kind", "custom")), {}).items():
        if out.get(lever) is None:
            out[lever] = value
    return out


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
        return [with_presets(e) for e in static + dynamic]

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
