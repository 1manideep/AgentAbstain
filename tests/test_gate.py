import asyncio
import shutil
from pathlib import Path

import pytest

from void.agents.models import AgentRecord, PersonalitySeed
from void.agents.registry import AgentRegistry
from void.brain.gadget_templates import TEMPLATES
from void.config import load_config
from void.db import Database
from void.economy.wallet import Wallet
from void.events import EventBus
from void.ids import IdFactory
from void.rng import RNG
from void.sandbox.gate import GadgetGate
from void.sandbox.registry import GadgetRegistry
from void.sandbox.runner import SubprocessSandbox
from void.types import AgentStatus, Vec2

REPO = Path(__file__).resolve().parents[1]


def build(tmp_path, gate_enabled=True):
    if shutil.which("unshare") is None:
        pytest.skip("unshare not available")
    cfg = load_config(REPO / "configs/scripted_smoke.yaml", {"sandbox": {"gate_enabled": gate_enabled, "proposals_per_agent_per_day": 10}})
    db = Database(tmp_path / "w.db")
    reg = AgentRegistry(db)
    for i in range(2):
        reg.insert(AgentRecord(f"ag_{i}", f"A{i}", "scripted", 2_000_000, PersonalitySeed(), "v", 0, AgentStatus.ALIVE, Vec2(0, 0), 0, 100, 0, False, 0))
    ids = IdFactory("t")
    wallet = Wallet(db, cfg, ids)
    sb = SubprocessSandbox(cfg.sandbox)
    asyncio.run(sb.self_test(REPO / "README.md"))
    if not sb.isolated:
        pytest.skip("namespace isolation unavailable")
    bus = EventBus(persist=db.persist_event)
    gate = GadgetGate(cfg, db, GadgetRegistry(db, tmp_path / "gadgets"), reg, sb, wallet, ids, bus, RNG(1))
    return cfg, db, reg, wallet, gate


def test_every_template_lands_in_its_expected_stage(tmp_path):
    cfg, db, reg, wallet, gate = build(tmp_path)
    outcomes = {}
    for i, t in enumerate(TEMPLATES):
        gate.reset_tick()  # one proposal per tick, as in the simulation
        o = asyncio.run(gate.propose("ag_0", i + 1, 1, t.name, t.purpose, t.code, t.tests, position=(0.0, 0.0), template_label=t.label))
        outcomes[t.label] = (o.ok, o.stage)
    assert outcomes["valid_beacon"] == (True, "verified")
    assert outcomes["valid_horn"] == (True, "verified")
    assert outcomes["static_fail"] == (False, "static")
    assert outcomes["tests_fail"] == (False, "tests")
    assert outcomes["spec_fail"] == (False, "spec")
    assert outcomes["oversized"] == (False, "size")
    assert outcomes["broken_run"] == (True, "verified")  # tests pass; it breaks only when used
    rows = db.fetchall("SELECT status, template_label FROM gadgets ORDER BY gadget_id")
    assert sum(1 for r in rows if r["status"] == "verified") == 3 and len(rows) == 7
    # fees went to the house, one per proposal
    assert wallet.balance("house") == 7 * 50_000


def test_use_applies_capped_effect_and_records_failures(tmp_path):
    cfg, db, reg, wallet, gate = build(tmp_path)
    beacon = next(t for t in TEMPLATES if t.label == "valid_beacon")
    lantern = next(t for t in TEMPLATES if t.label == "broken_run")
    ob = asyncio.run(gate.propose("ag_0", 1, 1, beacon.name, beacon.purpose, beacon.code, beacon.tests, position=(0.0, 0.0), template_label=beacon.label))
    ol = asyncio.run(gate.propose("ag_1", 1, 1, lantern.name, lantern.purpose, lantern.code, lantern.tests, position=(0.0, 0.0), template_label=lantern.label))
    gate.reset_tick()
    u = asyncio.run(gate.use("ag_1", 2, 1, ob.gadget_id, {"x": 5.0}, distance=0.5))
    assert u.ok and u.kind == "forage_bonus" and u.value == 0.2 and u.expires_tick == 2 + cfg.world.effect_ticks
    assert reg.get("ag_1").effects == {"forage_bonus": 0.2}
    assert asyncio.run(gate.use("ag_1", 3, 1, ob.gadget_id, {"x": 5.0}, distance=0.5)).reason == "cooldown"
    assert asyncio.run(gate.use("ag_1", 3, 1, ob.gadget_id, {"x": 5.0}, distance=9.0)).reason == "too_far"
    gate.reset_tick()
    uf = asyncio.run(gate.use("ag_0", 3, 1, ol.gadget_id, {"x": 1.0}, distance=0.5))
    assert not uf.ok and uf.reason.startswith("run_failed")
    assert db.fetchone("SELECT failures FROM gadgets WHERE gadget_id=?", (ol.gadget_id,))["failures"] == 1
    # tampering with stored code is detected on the next use
    rec = gate.registry.get(ob.gadget_id)
    Path(rec.code_path).write_text(rec.code_path and "def describe():\n    return {}\ndef run(p):\n    return {'value': 9}\n")
    gate.reset_tick()
    ut = asyncio.run(gate.use("ag_0", 30, 2, ob.gadget_id, {"x": 1.0}, distance=0.5))
    assert not ut.ok and ut.reason == "gadget_tampered"


def test_gate_off_verifies_untested_gadget_and_flags_it(tmp_path):
    cfg, db, reg, wallet, gate = build(tmp_path, gate_enabled=False)
    t = next(t for t in TEMPLATES if t.label == "tests_fail")
    o = asyncio.run(gate.propose("ag_0", 1, 1, t.name, t.purpose, t.code, t.tests, position=(0.0, 0.0), template_label=t.label))
    assert o.ok and gate.registry.get(o.gadget_id).verified_without_tests


def test_availability_rules(tmp_path):
    cfg, db, reg, wallet, gate = build(tmp_path)
    assert gate.can_propose("ag_0", recent_failure=False) == (True, "ok")  # holds nothing yet
    beacon = next(t for t in TEMPLATES if t.label == "valid_beacon")
    asyncio.run(gate.propose("ag_0", 1, 1, beacon.name, beacon.purpose, beacon.code, beacon.tests, position=(0.0, 0.0)))
    assert gate.can_propose("ag_0", recent_failure=False) == (False, "no_recent_failure")
    assert gate.can_propose("ag_0", recent_failure=True) == (True, "ok")
