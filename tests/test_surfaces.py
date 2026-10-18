"""Wave-1 shared surfaces: config fields, event kinds, the canary table, wire models and the exported schemas.

Later waves build on these without editing the shared files, so this file pins what they rely on: every shipped
config still loads, the new validators refuse what they should with the stated messages, the hello config the
server sends matches the wire model key for key, ``failed`` is a wire run status, the new Kind constants exist,
and a fresh database has ``canary_facts``. Configs that use none of the new fields hash exactly as before.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO, deep_merge
from pydantic import ConfigDict, ValidationError

from void.config import (
    HypothesisConfig,
    MaintenanceConfig,
    PracticesConfig,
    SelectionArmConfig,
    VoidConfig,
    load_config,
)
from void.db import Database
from void.events import Kind
from void.server.protocol import (
    SCHEMA_NAMES,
    GadgetItem,
    Hello,
    HelloConfig,
    IntelligencePublic,
    MemoryLayerPublic,
    StatusMsg,
    TierPublic,
    export_schemas,
    parse_message,
)

CONFIGS = sorted((REPO / "configs").glob("*.yaml"))
SMOKE = REPO / "configs" / "scripted_smoke.yaml"
SCHEMAS = REPO / "schemas"


def _smoke(overrides: dict[str, Any] | None = None) -> VoidConfig:
    return load_config(SMOKE, overrides or {})


def _hyp(**kw: Any) -> dict[str, Any]:
    return {"name": "h1", "arm_a": "base", "arm_b": "mem", "outcome": "final_mean_balance", **kw}


# --- config: every shipped config -------------------------------------------------------------------------
@pytest.mark.parametrize("path", CONFIGS, ids=[p.name for p in CONFIGS])
def test_every_shipped_config_loads(path: Path) -> None:
    cfg = load_config(path)
    assert cfg.memory.maintenance.mode == "forced"
    # the new blocks default off, so no shipped config changes behaviour or hash by their arrival; the E2 configs
    # (exp_mem_e2_*) turn the selection layer on deliberately: canaries in both arms, arm B in exp_mem_e2_b
    sel = cfg.memory.selection
    if path.name.startswith("exp_mem_e2"):
        assert sel.canary.enabled and sel.arm == ("B" if path.name == "exp_mem_e2_b.yaml" else "A")
    else:
        assert sel.arm == "A" and not sel.canary.enabled
    assert not cfg.memory.practices.enabled
    assert not cfg.memory.policy.mutate_at_birth
    # pre-registered hypotheses are declared only by the experiments that pre-register them: E0 and the dependence
    # check (exp_mem_e0_base.yaml and exp_mem_dep_base.yaml, inherited by their arms and by the live dependence configs)
    # and the E1 pilot (live_mem_e1_base.yaml); every other config, E5's evaluation world included, declares none
    if path.name.startswith(("exp_mem_dep", "exp_mem_e0", "live_claude_dep", "live_mem_e1")):
        assert cfg.experiment.hypotheses
    else:
        assert cfg.experiment.hypotheses == []


def test_config_count_is_not_vacuous() -> None:
    assert len(CONFIGS) >= 40


# --- config: hypotheses -----------------------------------------------------------------------------------
def test_hypothesis_defaults_and_round_trip() -> None:
    cfg = _smoke({"experiment": {"hypotheses": [_hyp(), _hyp(name="h2", kind="equivalence", margin=0.25, margin_unit="sd",
                                                              primary=False, min_power=0.8, direction="less")]}})
    h1, h2 = cfg.experiment.hypotheses
    assert (h1.kind, h1.direction, h1.margin, h1.margin_unit, h1.primary, h1.min_power) == ("superiority", "greater", 0.0, "abs", True, None)
    assert (h2.kind, h2.margin, h2.margin_unit, h2.primary, h2.min_power) == ("equivalence", 0.25, "sd", False, 0.8)
    # they are part of the stored config (and its hash), and the stored YAML loads back to the same config
    stored = yaml.safe_load(cfg.to_yaml())
    assert [h["name"] for h in stored["experiment"]["hypotheses"]] == ["h1", "h2"]
    assert VoidConfig.model_validate(stored).hash() == cfg.hash() != _smoke().hash()


def test_duplicate_hypothesis_names_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate names"):
        _smoke({"experiment": {"hypotheses": [_hyp(), _hyp(arm_b="other")]}})


def test_an_equivalence_hypothesis_needs_a_margin() -> None:
    with pytest.raises(ValidationError, match="equivalence hypothesis needs margin > 0"):
        HypothesisConfig.model_validate(_hyp(kind="equivalence"))
    with pytest.raises(ValidationError, match="equivalence hypothesis needs margin > 0"):
        _smoke({"experiment": {"hypotheses": [_hyp(kind="equivalence", margin=0.0)]}})
    assert HypothesisConfig.model_validate(_hyp(kind="equivalence", margin=0.01)).margin == 0.01


@pytest.mark.parametrize("bad, message", [
    ({"margin": -0.1}, "margin must be >= 0"),
    ({"min_power": 0.0}, "min_power must be within"),
    ({"min_power": 1.0}, "min_power must be within"),
    ({"arm_b": "base"}, "arm_a and arm_b must differ"),
    ({"kind": "bigger"}, "kind"),
    ({"direction": "up"}, "direction"),
    ({"margin_unit": "pct"}, "margin_unit"),
    ({"surprise": 1}, "surprise"),
])
def test_hypothesis_field_rules(bad: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        HypothesisConfig.model_validate(_hyp(**bad))


# --- config: practices, selection, policy mutation, maintenance ---------------------------------------------
def test_practices_defaults_and_bounds() -> None:
    p = PracticesConfig()
    assert (p.enabled, p.excerpt_max_chars, p.adopt_similarity, p.persist_days, p.scripted_share_weight) == (False, 300, 0.6, 2, 0.3)
    for bad in ({"excerpt_max_chars": 0}, {"excerpt_max_chars": 2001}, {"adopt_similarity": -0.01}, {"adopt_similarity": 1.01},
                {"persist_days": 0}, {"scripted_share_weight": -1.0}):
        with pytest.raises(ValidationError):
            PracticesConfig.model_validate(bad)
    assert PracticesConfig.model_validate({"excerpt_max_chars": 2000, "adopt_similarity": 1.0}).excerpt_max_chars == 2000


def test_selection_defaults_and_bounds() -> None:
    s = SelectionArmConfig()
    assert (s.arm, s.canary.enabled, s.canary.per_day, s.reward_delay_days, s.reward_questions_per_agent) == ("A", False, 1, 2, 2)
    assert (s.recall_reward_usd, s.practice_royalty_usd) == (0.02, 0.02)
    for bad in ({"arm": "D"}, {"canary": {"per_day": 0}}, {"canary": {"per_day": 6}}, {"reward_delay_days": 0},
                {"reward_questions_per_agent": 0}, {"recall_reward_usd": -0.01}, {"practice_royalty_usd": -0.01}):
        with pytest.raises(ValidationError):
            SelectionArmConfig.model_validate(bad)


def test_arm_b_needs_canaries_and_the_chronicle() -> None:
    with pytest.raises(ValidationError, match="arm B needs memory.selection.canary.enabled and chronicle.enabled"):
        _smoke({"memory": {"selection": {"arm": "B"}}})
    with pytest.raises(ValidationError, match="arm B needs"):
        _smoke({"memory": {"selection": {"arm": "B", "canary": {"enabled": True}}}, "chronicle": {"enabled": False}})
    cfg = _smoke({"memory": {"selection": {"arm": "B", "canary": {"enabled": True, "per_day": 3}}}})
    assert cfg.memory.selection.arm == "B" and cfg.memory.selection.canary.per_day == 3


def test_arm_c_needs_practices() -> None:
    with pytest.raises(ValidationError, match="arm C needs memory.practices.enabled"):
        _smoke({"memory": {"selection": {"arm": "C"}}})
    assert _smoke({"memory": {"selection": {"arm": "C"}, "practices": {"enabled": True}}}).memory.selection.arm == "C"


def test_mutation_at_birth_is_for_scripted_tiers_only() -> None:
    assert _smoke({"memory": {"policy": {"enabled": True, "mutate_at_birth": True}}}).memory.policy.mutate_at_birth
    provider_roster = {"agents": [{"name": "Ada", "tier": "scripted"}, {"name": "Bao", "tier": "budget"}],
                       "memory": {"policy": {"enabled": True, "mutate_at_birth": True}}}
    with pytest.raises(ValidationError, match="kernel mutation at birth is built for scripted tiers only"):
        _smoke(provider_roster)
    # an arrival on a provider tier would be born there too
    arrival = {"memory": {"policy": {"mutate_at_birth": True}},
               "epochs": [{"day": 2, "kind": "arrival", "arrival": {"name": "Zed", "tier": "budget"}}]}
    with pytest.raises(ValidationError, match="kernel mutation at birth is built for scripted tiers only"):
        _smoke(arrival)
    # a provider tier that is defined but never rostered is fine
    assert _smoke({"memory": {"policy": {"mutate_at_birth": True}}}).tiers["budget"].provider != "scripted"


def test_maintenance_mode_optional_is_refused() -> None:
    message = "memory.maintenance.mode optional is not built (MEMORY_EVOLUTION 5.6 arm, E8); use forced"
    with pytest.raises(ValidationError, match=re.escape(message)):
        MaintenanceConfig.model_validate({"mode": "optional"})
    with pytest.raises(ValidationError, match=re.escape(message)):
        _smoke({"memory": {"maintenance": {"mode": "optional"}}})
    assert MaintenanceConfig.model_validate({"mode": "forced"}).mode == "forced"


def test_new_maintenance_strategies_are_accepted() -> None:
    for strategy in ("distill", "revert", "retire"):
        cfg = _smoke({"tiers": {"scripted": {"maintenance_strategy": strategy}}})
        assert cfg.tiers["scripted"].maintenance_strategy == strategy
    with pytest.raises(ValidationError):
        _smoke({"tiers": {"scripted": {"maintenance_strategy": "perturb"}}})


# --- config: hashes of worlds that use none of it ----------------------------------------------------------
def test_unused_new_fields_leave_the_canonical_form_alone() -> None:
    cfg = _smoke()
    data = cfg.canonical_dict()
    assert "practices" not in data["memory"] and "selection" not in data["memory"]
    assert "mutate_at_birth" not in data["memory"]["policy"] and "hypotheses" not in data["experiment"]
    assert "fallow_ticks" not in data["world"] and "veins" not in data["world"]
    # the omitted fields come back at their defaults when the stored YAML is loaded
    assert VoidConfig.model_validate(yaml.safe_load(cfg.to_yaml())).model_dump() == cfg.model_dump()


@pytest.mark.parametrize("over, path", [
    ({"memory": {"practices": {"enabled": True}}}, ("memory", "practices")),
    ({"memory": {"selection": {"canary": {"enabled": True}}}}, ("memory", "selection")),
    ({"memory": {"policy": {"enabled": True, "mutate_at_birth": True}}}, ("memory", "policy", "mutate_at_birth")),
    ({"experiment": {"hypotheses": [_hyp()]}}, ("experiment", "hypotheses")),
])
def test_a_used_new_field_is_stored_and_hashed(over: dict[str, Any], path: tuple[str, ...]) -> None:
    cfg = _smoke(over)
    node: Any = cfg.canonical_dict()
    for key in path:
        node = node[key]
    assert node
    # the same world with the field back at its default hashes differently
    off = deep_merge(over, {"memory": {"practices": {"enabled": False}, "selection": {"canary": {"enabled": False}},
                                       "policy": {"mutate_at_birth": False}}, "experiment": {"hypotheses": []}})
    assert cfg.hash() != _smoke(off).hash()


# --- events ----------------------------------------------------------------------------------------------
NEW_KINDS = {
    "GADGET_TAMPERED": "gadget_tampered", "GADGET_USE_FAILED": "gadget_use_failed", "SANDBOX_SELF_TEST": "sandbox_self_test",
    "OPERATOR_COMMAND": "operator_command", "TRACER_PLANTED": "tracer_planted", "RUN_ENDED": "run_ended",
    "PRACTICE_PERSISTED": "practice_persisted", "PRACTICE_DROPPED": "practice_dropped", "CANARY_PLANTED": "canary_planted",
    "RECALL_REWARD": "recall_reward", "PRACTICE_ROYALTY": "practice_royalty",
}


def test_the_eleven_new_kinds_exist() -> None:
    assert len(NEW_KINDS) == 11
    for attr, value in NEW_KINDS.items():
        assert getattr(Kind, attr) == value


def test_kind_values_are_unique() -> None:
    values = [v for k, v in vars(Kind).items() if k.isupper()]
    assert len(values) == len(set(values))


# --- database --------------------------------------------------------------------------------------------
def test_a_fresh_database_has_canary_facts(tmp_path: Path) -> None:
    db = Database(tmp_path / "world.db")
    try:
        assert "canary_facts" in db.tables()
        assert db.columns("canary_facts") == ["fact_id", "day", "tick", "channel", "subject", "text", "answer", "options",
                                              "chronicle_item_id"]
        db.execute("INSERT INTO canary_facts(fact_id, day, tick, channel, subject, text, answer, options) VALUES(?,?,?,?,?,?,?,?)",
                   ("c1", 1, 5, "chronicle", "node n4", "A trader at node n4 said the east well runs dry on day 9", "9",
                    json.dumps(["7", "8", "9"])))
        row = db.fetchone("SELECT * FROM canary_facts WHERE fact_id='c1'")
        assert row is not None and row["chronicle_item_id"] is None and json.loads(row["options"]) == ["7", "8", "9"]
    finally:
        db.close()


def test_an_existing_database_gains_canary_facts_on_open(tmp_path: Path) -> None:
    path = tmp_path / "world.db"
    db = Database(path)
    db.execute("DROP TABLE canary_facts")
    db.close()
    db = Database(path)
    try:
        assert "canary_facts" in db.tables()
    finally:
        db.close()


# --- wire models -----------------------------------------------------------------------------------------
class StrictTier(TierPublic):
    model_config = ConfigDict(extra="forbid")


class StrictMemoryLayer(MemoryLayerPublic):
    model_config = ConfigDict(extra="forbid")


class StrictIntelligence(IntelligencePublic):
    model_config = ConfigDict(extra="forbid")


class StrictHelloConfig(HelloConfig):
    """HelloConfig with every key declared: a key public_subset() sends but the model lacks fails here."""

    model_config = ConfigDict(extra="forbid")

    tiers: dict[str, StrictTier]
    memory_layer: StrictMemoryLayer | None = None
    intelligence: StrictIntelligence | None = None


@pytest.mark.parametrize("name", ["demo_memory.yaml", "base.yaml", "exp_ladder_ladder.yaml"])
def test_public_subset_validates_against_a_strict_hello_config(name: str) -> None:
    cfg = load_config(REPO / "configs" / name)
    sub = cfg.public_subset()
    strict = StrictHelloConfig.model_validate(sub)
    assert set(sub) <= set(HelloConfig.model_fields)
    assert strict.memory_layer is not None and strict.intelligence is not None
    assert strict.view_radius == cfg.world.view_radius
    assert strict.intelligence.ladder == list(cfg.intelligence.ladder)
    for tier, t in strict.tiers.items():
        assert t.rank == cfg.rung_of(tier) and t.collapse_temperature == cfg.tiers[tier].collapse_temperature


def test_demo_memory_turns_the_memory_layer_on() -> None:
    hello = HelloConfig.model_validate(load_config(REPO / "configs" / "demo_memory.yaml").public_subset())
    assert hello.memory_layer is not None and hello.memory_layer.enabled


def test_a_minimal_hello_config_from_an_older_server_still_validates() -> None:
    old = {"name": "x", "seed": 1, "days": 1, "ticks_per_day": 24, "tick_seconds": 0.0, "render_delay_ticks": 2,
           "world_size": 60.0, "population_cap": 12, "daily_cap_usd": 3.0, "total_cap_usd": 30.0,
           "tiers": {"scripted": {"color": "#fff", "model": "scripted", "provider": "scripted"}}}
    hello = HelloConfig.model_validate(old)
    assert hello.view_radius is None and hello.memory_layer is None and hello.intelligence is None
    assert hello.tiers["scripted"].rank is None


def test_failed_is_a_wire_status() -> None:
    msg = parse_message({"type": "status", "paused": False, "tick_seconds": 0.0, "status": "failed"})
    assert isinstance(msg, StatusMsg) and msg.status == "failed"
    hello = {"type": "hello", "run_id": "r", "tick": 3, "day": 1, "last_seq": 0, "paused": False, "tick_seconds": 0.0,
             "server_ts_ms": 0.0, "status": "failed", "config": load_config(REPO / "configs" / "demo_memory.yaml").public_subset()}
    parsed = parse_message(hello)
    assert isinstance(parsed, Hello) and parsed.status == "failed"


def test_gadget_item_carries_the_effect_fields() -> None:
    base = {"id": "g1", "name": "lamp", "owner": "a1", "x": 1.0, "y": 2.0, "render": {"shape": "cube", "color": "#fff"}}
    g = GadgetItem.model_validate(base)
    assert (g.effect_kind, g.effect_value, g.purpose, g.verified_without_tests, g.failures, g.created_tick) == (None, None, None, False, 0, None)
    g = GadgetItem.model_validate({**base, "effect_kind": "forage_bonus", "effect_value": 0.2, "purpose": "find food",
                                   "verified_without_tests": True, "failures": 2, "created_tick": 40})
    assert (g.effect_kind, g.effect_value, g.failures, g.created_tick, g.verified_without_tests) == ("forage_bonus", 0.2, 2, 40, True)


# --- exported schemas ------------------------------------------------------------------------------------
def _enum_values(schema: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(schema, dict):
        if isinstance(schema.get("enum"), list):
            out.update(str(v) for v in schema["enum"])
        for v in schema.values():
            out |= _enum_values(v)
    elif isinstance(schema, list):
        for v in schema:
            out |= _enum_values(v)
    return out


@pytest.mark.parametrize("name", ["status", "hello", "snapshot"])
def test_status_and_hello_schemas_contain_failed(name: str) -> None:
    schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    assert "failed" in _enum_values(schema)


def test_hello_and_gadget_schemas_carry_the_new_keys() -> None:
    hello = json.loads((SCHEMAS / "hello.schema.json").read_text())
    defs = hello["$defs"]
    assert {"view_radius", "memory_layer", "intelligence"} <= set(defs["HelloConfig"]["properties"])
    assert {"collapse_temperature", "max_temperature", "rank"} <= set(defs["TierPublic"]["properties"])
    assert set(defs["MemoryLayerPublic"]["required"]) == {"enabled", "policy", "maintenance", "exam"}
    gadgets = json.loads((SCHEMAS / "gadgets.schema.json").read_text())
    props = gadgets["$defs"]["GadgetItem"]["properties"]
    assert {"effect_kind", "effect_value", "purpose", "verified_without_tests", "failures", "created_tick"} <= set(props)


def test_committed_schemas_match_the_models(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    for name in SCHEMA_NAMES:
        assert (tmp_path / f"{name}.schema.json").read_text() == (SCHEMAS / f"{name}.schema.json").read_text(), \
            f"schemas/{name}.schema.json is stale: run `void schema --out schemas`"
