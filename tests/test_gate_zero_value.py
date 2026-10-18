"""A gadget use that realizes 0 for an effect kind the agent does not hold (``GadgetGate.use``, DESIGN §12).

Effects do not stack: the larger of the held value and the new one wins. A use whose value clamps to 0 (the horn's
``run`` gives 0 for ``x <= 0``, which the DESIGN §7.2 scripted brain draws about half the time) by an agent holding no
effect of that kind passed the ``effects.get(kind, 0.0) >= value`` test and then read ``effects[kind]``, which raised
KeyError after the use fee was paid: the kernel logged ``use_gadget`` invalid ``kernel_error:KeyError`` with a
``brain_error``, and the use never reached ``gadget_uses``. It is now an ordinary recorded use that writes no effect.

The unit tests drive the gate with a stand-in sandbox that returns a fixed run value, so they need no namespace
isolation; the scripted run is a one-day gate-off smoke world (DESIGN §7.2 rules) in which one use realizes 0 for an
agent with no talk range bonus, and which logged ``kernel_error:KeyError`` for it before the fix.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, SMOKE, deep_merge

from void.agents.models import AgentRecord, PersonalitySeed
from void.agents.registry import AgentRegistry
from void.brain.gadget_templates import TEMPLATES
from void.config import VoidConfig, load_config
from void.db import Database
from void.economy.wallet import Wallet
from void.events import EventBus, Kind
from void.ids import IdFactory
from void.rng import RNG
from void.sandbox.gate import GadgetGate
from void.sandbox.registry import GadgetRecord, GadgetRegistry, sha256
from void.sandbox.runner import SandboxResult
from void.types import AgentStatus, Vec2

HORN = next(t for t in TEMPLATES if t.label == "valid_horn")


class FixedSandbox:
    """Answers every run with ``{"value": value}``; the gate never verifies anything here."""

    isolated = True
    available = True

    def __init__(self) -> None:
        self.value: Any = 0.0

    async def verify(self, code: str, tests: str, seed: int, *, run_tests: bool = True) -> SandboxResult:
        raise AssertionError("not used")

    async def run(self, code: str, params: dict[str, float], seed: int) -> SandboxResult:
        return SandboxResult(True, "run", run_value={"value": self.value})


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.cfg: VoidConfig = load_config(SMOKE)
        self.db = Database(tmp_path / "w.db")
        self.agents = AgentRegistry(self.db)
        self.agents.insert(AgentRecord("ag_0", "Ada", "scripted", 2_000_000, PersonalitySeed(), "v", 0, AgentStatus.ALIVE,
                                       Vec2(0, 0), 0, 100, 0, False, 0))
        ids = IdFactory("t")
        self.events: list[Any] = []
        bus = EventBus(persist=self.db.persist_event)
        bus.subscribe(self.events.append)
        self.sandbox = FixedSandbox()
        self.gadgets = GadgetRegistry(self.db, tmp_path / "gadgets")
        self.gate = GadgetGate(self.cfg, self.db, self.gadgets, self.agents, self.sandbox, Wallet(self.db, self.cfg, ids),
                               ids, bus, RNG(1))
        cp, tp = self.gadgets.store_files("gd_horn", HORN.code, HORN.tests)
        self.gadgets.insert(GadgetRecord("gd_horn", "horn", "ag_9", HORN.purpose, str(cp), str(tp), sha256(HORN.code),
                                         sha256(HORN.tests), "verified", {}, {"kind": "talk_range", "value": 1.5}, {}, False,
                                         HORN.label, 0.5, 0.0, 0))

    def use(self, tick: int, value: Any) -> Any:
        self.sandbox.value = value
        self.gate.reset_tick()
        return asyncio.run(self.gate.use("ag_0", tick, 1, "gd_horn", {"x": -0.5}, distance=0.5))

    def effects(self) -> dict[str, float]:
        return {r["kind"]: float(r["value"]) for r in self.db.fetchall("SELECT kind, value FROM agent_effects WHERE agent_id='ag_0'")}

    def uses(self) -> list[tuple[int, int, float | None]]:
        return [(r["tick"], r["ok"], r["realized_effect"])
                for r in self.db.fetchall("SELECT tick, ok, realized_effect FROM gadget_uses ORDER BY rowid")]


@pytest.mark.parametrize("raw", [0.0, -0.4, float("nan"), "not a number"], ids=["zero", "negative", "nan", "text"])
def test_a_zero_value_use_without_a_held_effect_is_an_ordinary_use(tmp_path: Path, raw: Any) -> None:
    rig = Rig(tmp_path)
    out = rig.use(5, raw)  # raised KeyError: 'talk_range' before the fix
    assert (out.ok, out.reason, out.kind, out.value, out.expires_tick) == (True, "", "talk_range", 0.0, 5 + rig.cfg.world.effect_ticks)
    assert rig.effects() == {}  # nothing to apply, so no effect row
    assert rig.agents.get("ag_0").effects == {}
    assert rig.uses() == [(5, 1, 0.0)]
    row = rig.db.fetchone("SELECT uses, failures FROM gadgets WHERE gadget_id='gd_horn'")
    assert (row["uses"], row["failures"]) == (1, 0)
    used = [e for e in rig.events if e.kind == Kind.USE_GADGET]
    assert len(used) == 1 and used[0].payload["value"] == 0.0 and used[0].payload["kind"] == "talk_range"
    # it counts as a use: the cooldown applies to the next one, as after any successful use
    assert rig.use(6, 1.0).reason == "cooldown"


def test_a_held_effect_still_wins_and_a_larger_use_replaces_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    gap = rig.cfg.world.effect_ticks
    rig.agents.set_effect("ag_0", "talk_range", 0.5, 1000)
    for k, raw in enumerate((0.0, 0.3)):  # a zero or smaller use reports the held value and leaves it in place
        out = rig.use(10 + k * gap, raw)
        assert out.ok and out.value == 0.5
        assert tuple(rig.db.fetchone("SELECT value, expires_tick FROM agent_effects WHERE agent_id='ag_0'")) == (0.5, 1000)
    out = rig.use(10 + 2 * gap, 1.2)
    assert out.ok and out.value == 1.2 and rig.effects() == {"talk_range": 1.2}
    out = rig.use(10 + 3 * gap, 9.0)  # capped at the verified value and the world cap
    assert out.ok and out.value == min(1.5, rig.cfg.world.effect_caps["talk_range"])
    assert [u[2] for u in rig.uses()] == [0.0, 0.3, 1.2, out.value]


def _formerly_failing(uses: list[tuple[int, str, dict[str, Any]]]) -> list[tuple[int, str]]:
    """Uses that realized 0 while the agent held no unexpired effect of that kind (the case that raised KeyError)."""
    held: dict[tuple[str, str], int] = {}
    out = []
    for tick, agent, p in uses:
        key = (agent, p["kind"])
        if float(p["value"]) == 0.0 and held.get(key, -1) <= tick:
            out.append((tick, agent))
        if float(p["value"]) > 0.0:
            held[key] = max(held.get(key, -1), int(p["expires_tick"]))
    return out


def test_a_scripted_world_records_its_zero_value_uses(run_sim) -> None:
    run = run_sim("gate_zero", deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 1}, "sandbox": {"gate_enabled": False}}))
    if not getattr(run.sim.sandbox, "available", False):
        pytest.skip("sandbox unavailable: gadget proposals fail closed")
    db = run.db
    failed = [json.loads(r["payload"]) for r in db.fetchall("SELECT payload FROM events WHERE kind='action_failed'")]
    assert not [p for p in failed if p["action"] == "use_gadget" and p["reason"].startswith("kernel_error")]
    errors = [json.loads(r["payload"])["error"] for r in db.fetchall("SELECT payload FROM events WHERE kind='brain_error'")]
    assert not [e for e in errors if e.startswith("KeyError")]
    uses = [(int(r["tick"]), r["agent_id"], json.loads(r["payload"]))
            for r in db.fetchall("SELECT tick, agent_id, payload FROM events WHERE kind='use_gadget' ORDER BY seq")]
    zero = _formerly_failing(uses)
    assert zero, "this world no longer has a zero-value use by an agent without the effect; pick one that does"
    ledger = {(int(r["tick"]), r["agent_id"]): (r["ok"], r["realized_effect"])
              for r in db.fetchall("SELECT tick, agent_id, ok, realized_effect FROM gadget_uses")}
    for key in zero:
        assert ledger[key] == (1, 0.0)
    assert not db.fetchall("SELECT * FROM agent_effects WHERE value <= 0")
