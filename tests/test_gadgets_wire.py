"""The gadgets wire message carries the registry's fields (DESIGN §12, §15; GadgetItem in void/server/protocol.py).

``snapshot.gadgets_message`` fills ``effect_kind``/``effect_value`` from the verified effect spec, ``purpose``,
``verified_without_tests`` (the gate's flag, set when ``sandbox.gate_enabled`` is false), the ``uses`` and
``failures`` counters and ``created_tick``, all read from the ``gadgets`` table. Two one-day scripted runs (gate on,
gate off) must each verify at least one gadget; their messages validate against :class:`GadgetsMsg` and agree with
the table column by column.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, SimRun, deep_merge

from void.sandbox.registry import GadgetRecord
from void.server.protocol import GadgetItem, GadgetsMsg, parse_message
from void.sim.snapshot import gadget_item, gadgets_message

NEW_FIELDS = {"effect_kind", "effect_value", "purpose", "verified_without_tests", "failures", "created_tick"}


@pytest.fixture(scope="module", params=[True, False], ids=["gate_on", "gate_off"])
def gadget_run(request, run_sim) -> SimRun:
    gate = bool(request.param)
    run = run_sim(f"gadgets_wire_{'on' if gate else 'off'}",
                  deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 1}, "sandbox": {"gate_enabled": gate}}))
    if not getattr(run.sim.sandbox, "available", False):
        pytest.skip("sandbox unavailable: gadget proposals fail closed")
    return run


def _columns(run: SimRun, gadget_id: str) -> dict[str, Any]:
    return dict(run.db.fetchone("SELECT * FROM gadgets WHERE gadget_id=?", (gadget_id,)))


def test_the_message_validates_with_the_new_fields(gadget_run: SimRun) -> None:
    msg = gadget_run.sim.gadgets_message()
    parsed = parse_message(msg)
    assert isinstance(parsed, GadgetsMsg)
    assert parsed.rev == int(gadget_run.kv("gadgets_rev", 0))
    assert parsed.items, "the scripted run verified no gadget"
    for item in msg["items"]:
        # every key the server sends is a GadgetItem field (extra keys would be silently ignored on validation)
        assert set(item) == set(GadgetItem.model_fields)
        assert NEW_FIELDS <= set(item)
        GadgetItem.model_validate(item)
    for g in parsed.items:
        assert g.effect_kind in gadget_run.cfg.world.effect_caps
        assert g.effect_value is not None and 0 <= g.effect_value <= gadget_run.cfg.world.effect_caps[g.effect_kind]
        assert g.purpose and g.created_tick is not None and g.uses >= 0 and g.failures >= 0


def test_the_items_agree_with_the_gadget_columns(gadget_run: SimRun) -> None:
    gate_on = gadget_run.cfg.sandbox.gate_enabled
    msg = gadget_run.sim.gadgets_message()
    rows = gadget_run.db.fetchall("SELECT gadget_id FROM gadgets WHERE status='verified' AND x IS NOT NULL ORDER BY gadget_id")
    assert [i["id"] for i in msg["items"]] == [r["gadget_id"] for r in rows]
    for item in msg["items"]:
        col = _columns(gadget_run, item["id"])
        effect = json.loads(col["effect_spec"])
        assert (item["name"], item["owner"], item["x"], item["y"]) == (col["name"], col["owner_agent_id"], col["x"], col["y"])
        assert item["render"] == json.loads(col["render_spec"])
        assert (item["effect_kind"], item["effect_value"]) == (effect["kind"], effect["value"])
        assert item["purpose"] == col["purpose"]
        assert item["verified_without_tests"] is bool(col["verified_without_tests"])
        assert item["verified_without_tests"] is (not gate_on)
        assert (item["uses"], item["failures"], item["created_tick"]) == (col["uses"], col["failures"], col["created_tick"])


def test_rejected_proposals_stay_off_the_wire(gadget_run: SimRun) -> None:
    ids = {i["id"] for i in gadget_run.sim.gadgets_message()["items"]}
    others = gadget_run.db.fetchall("SELECT gadget_id FROM gadgets WHERE status!='verified' OR x IS NULL")
    assert not ids & {r["gadget_id"] for r in others}


def test_uses_and_failures_follow_the_use_ledger(gadget_run: SimRun) -> None:
    sim = gadget_run.sim
    gid = sim.gadgets_message()["items"][0]["id"]
    user = sim.registry.alive()[0].agent_id
    end = sim.clock.tick
    for k, ok in enumerate((True, False, False)):
        sim.gadgets.record_use(gid, user, end + 100 + k, ok, 0.1 if ok else None, 0.1, None if ok else "test")
    item = next(i for i in sim.gadgets_message()["items"] if i["id"] == gid)
    ledger = gadget_run.db.fetchone("SELECT COALESCE(SUM(ok),0) AS ok, COUNT(*) AS n FROM gadget_uses WHERE gadget_id=?", (gid,))
    assert item["uses"] == int(ledger["ok"]) >= 1
    assert item["failures"] == int(ledger["n"]) - int(ledger["ok"]) >= 2
    GadgetItem.model_validate(item)


# --- record conversion without a run ----------------------------------------------------------------------
def _record(**kw: Any) -> GadgetRecord:
    base: dict[str, Any] = {
        "gadget_id": "gd_1", "name": "lamp", "owner_agent_id": "ag_1", "purpose": "find food", "code_path": "c.py",
        "test_path": "t.py", "code_hash": "h", "test_hash": "h", "status": "verified",
        "render": {"shape": "cube", "color": "#ffcc33", "scale": 1.0, "label": "lamp", "rotation_deg": 0.0, "height_offset": 0.0},
        "effect": {"kind": "forage_bonus", "value": 0.2}, "verification": {"stage": "verified"},
        "verified_without_tests": True, "template_label": None, "x": 1.5, "y": -2.0, "created_tick": 7, "uses": 3, "failures": 2,
    }
    base.update(kw)
    return GadgetRecord(**base)


def test_a_record_maps_onto_every_new_field() -> None:
    item = gadget_item(_record())
    assert {k: item[k] for k in NEW_FIELDS} == {"effect_kind": "forage_bonus", "effect_value": 0.2, "purpose": "find food",
                                                "verified_without_tests": True, "failures": 2, "created_tick": 7}
    assert item["uses"] == 3
    GadgetItem.model_validate(item)


@pytest.mark.parametrize("effect", [{}, {"kind": "", "value": "x"}, {"kind": 3, "value": float("nan")}, {"value": True},
                                    {"value": float("inf")}, []])
def test_a_malformed_effect_spec_reads_as_unknown(effect: dict[str, Any]) -> None:
    item = gadget_item(_record(effect=effect, purpose=""))
    assert (item["effect_kind"], item["effect_value"], item["purpose"]) == (None, None, None)
    GadgetItem.model_validate(item)


def test_only_placed_verified_gadgets_are_listed() -> None:
    recs = [_record(gadget_id="gd_a"), _record(gadget_id="gd_b", status="rejected", x=None, y=None),
            _record(gadget_id="gd_c", status="tampered"), _record(gadget_id="gd_d", x=None, y=None)]
    msg = gadgets_message(recs, 4)
    assert [i["id"] for i in msg["items"]] == ["gd_a"]
    assert GadgetsMsg.model_validate(msg).rev == 4
