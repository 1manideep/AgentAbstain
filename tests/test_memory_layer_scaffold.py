"""The evolving-memory layer, thin-slice scaffolding (docs/research/MEMORY_EVOLUTION.md §5, §10.4).

Covers the shared surfaces every work package builds on: config blocks and their validators, the new tables
and the column migration, prompt and observation logging, the amnesic switch, fog of war with entity stubs
on first sight and memory-guided navigation, claim feedback, the research-pool wallet, policy rendering,
path confinement for the memory commands, and the flat export.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from void.brain.prompt import render_observation, system_prompt
from void.config import load_config
from void.db import MIGRATIONS, SCHEMA_VERSION, Database
from void.events import Kind
from void.memory.commands import ROOT, MemoryCommand, PathError, resolve_path
from void.memory.policy import SEED_DEFAULT, SEED_DIVERSE, similarity
from void.research.exams import score
from void.research.export import export_run
from void.types import Observation, Vec2

REPO = Path(__file__).resolve().parents[1]
SMOKE = REPO / "configs" / "scripted_smoke.yaml"
NO_MUTATION = {"population": {"tier_mutation_prob": 0.0}}
LIARS = {"agents": [{"name": n, "tier": "scripted", "personality": {"honesty": 0.0, "sociability": 1.0}}
                    for n in ("Ada", "Bao", "Cyra", "Dev", "Enzo", "Faye")]}


# --- config -----------------------------------------------------------------------------------------------
def test_defaults_leave_the_layer_off() -> None:
    cfg = load_config(SMOKE)
    assert cfg.memory.enabled and not cfg.memory.policy.enabled and not cfg.memory.maintenance.enabled and not cfg.memory.exam.enabled
    assert cfg.world.view_radius is None and cfg.world.claim_feedback_p == 0.0 and cfg.run.log_prompts
    assert cfg.economy.research_pool_usd == 0.0
    assert cfg.public_subset()["memory_layer"] == {"enabled": True, "policy": False, "maintenance": False, "exam": False}


@pytest.mark.parametrize("override, message", [
    ({"world": {"view_radius": 2.0}}, "view_radius must be >= world.talk_radius"),
    ({"world": {"view_radius": -1.0}}, "view_radius must be > 0"),
    ({"world": {"claim_feedback_p": 1.5}}, "claim_feedback_p"),
    ({"memory": {"enabled": False, "maintenance": {"enabled": True, "rights": ["notes"]}}}, "requires memory.enabled"),
    ({"memory": {"maintenance": {"enabled": True}}}, "memory.policy.enabled is false"),
    ({"run": {"log_prompts": False}, "memory": {"exam": {"enabled": True}}}, "requires run.log_prompts"),
    ({"memory": {"exam": {"enabled": True, "kinds": ["claim"]}}}, "claim_feedback_p is 0"),
    ({"memory": {"maintenance": {"rights": ["notes", "notes"]}}}, "duplicates"),
    ({"memory": {"policy": {"max_chars": 10}}}, "max_chars"),
    ({"tiers": {"frontier": {"maintenance_strategy": "noop"}}}, "scripted tiers only"),
])
def test_validators_refuse_inconsistent_layers(override: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(SMOKE, override)


def test_consistent_layer_configs_load() -> None:
    cfg = load_config(SMOKE, {"world": {"view_radius": 10.0, "claim_feedback_p": 0.5},
                              "memory": {"policy": {"enabled": True, "seed": "diverse"},
                                         "maintenance": {"enabled": True, "rights": ["notes", "policy", "structure"]},
                                         "exam": {"enabled": True, "kinds": ["balance", "node", "claim"]}},
                              "tiers": {"scripted": {"maintenance_strategy": "index_builder"}}})
    assert cfg.tiers["scripted"].maintenance_strategy == "index_builder"
    assert cfg.hash() != load_config(SMOKE).hash()


# --- schema -----------------------------------------------------------------------------------------------
def test_schema_has_the_layer_tables_and_columns(tmp_path: Path) -> None:
    db = Database(tmp_path / "world.db")
    try:
        tables = set(db.tables())
        for t in ("policy_versions", "memory_commands", "practice_events", "exam_items", "exam_answers", "note_manifest", "prompt_texts"):
            assert t in tables, t
        cols = set(db.columns("llm_calls"))
        assert {"prompt_text", "system_hash", "observation"} <= cols
        assert SCHEMA_VERSION == 2
    finally:
        db.close()


def test_migration_adds_columns_to_a_schema_1_database(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE llm_calls (call_id TEXT PRIMARY KEY, tick INTEGER NOT NULL, agent_id TEXT NOT NULL, purpose TEXT NOT NULL DEFAULT 'decide', "
                "tier TEXT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL, real_cost INTEGER NOT NULL, world_cost INTEGER NOT NULL)")
    raw.execute("INSERT INTO llm_calls(call_id, tick, agent_id, tier, model, provider, real_cost, world_cost) VALUES('c1', 1, 'a', 't', 'm', 'p', 0, 0)")
    raw.commit()
    raw.close()
    db = Database(path)
    try:
        cols = set(db.columns("llm_calls"))
        assert all(c in cols for _, c, _ in MIGRATIONS)
        row = db.fetchone("SELECT prompt_text, observation FROM llm_calls WHERE call_id='c1'")
        assert row is not None and row["prompt_text"] is None and row["observation"] is None
        Database(path).close()  # idempotent on a migrated file
    finally:
        db.close()


# --- prompt logging (WP0) ----------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def logged(run_sim):
    return run_sim("layer_logged", {**NO_MUTATION, "run": {"days": 2, "ticks_per_day": 12}})


def test_every_decision_call_logs_prompt_system_hash_and_observation(logged) -> None:
    rows = logged.db.fetchall("SELECT call_id, agent_id, tick, prompt_text, system_hash, observation FROM llm_calls WHERE purpose='decide'")
    assert rows
    hashes = {r["hash"] for r in logged.db.fetchall("SELECT hash FROM prompt_texts")}
    for r in rows:
        assert r["prompt_text"] and r["prompt_text"].startswith(f"# Tick {r['tick']}")
        assert r["system_hash"] in hashes
        obs = json.loads(r["observation"])
        assert obs["agent_id"] == r["agent_id"] and obs["tick"] == r["tick"]
        assert "nodes" in obs and "heard" in obs and "memories" in obs and "balance_bucket" in obs
    sys_text = logged.db.fetchone("SELECT text FROM prompt_texts WHERE hash=?", (rows[0]["system_hash"],))["text"]
    assert sys_text == system_prompt(logged.cfg, "scripted")


def test_log_prompts_off_stores_nulls(run_sim) -> None:
    run = run_sim("layer_unlogged", {**NO_MUTATION, "run": {"days": 1, "ticks_per_day": 6, "log_prompts": False}})
    row = run.db.fetchone("SELECT COUNT(*) AS n, COUNT(prompt_text) AS p, COUNT(observation) AS o FROM llm_calls WHERE purpose='decide'")
    assert int(row["n"]) > 0 and int(row["p"]) == 0 and int(row["o"]) == 0


def test_export_writes_every_layer_table(logged, tmp_path: Path) -> None:
    manifest = export_run(logged.run_dir, tmp_path / "export")
    assert manifest["format"] in ("csv", "parquet")
    for t in ("llm_calls", "events", "wallet_ledger", "notes", "policy_versions", "memory_commands", "exam_items", "metrics"):
        assert t in manifest["tables"], t
        assert Path(manifest["tables"][t]["path"]).exists()
    assert manifest["tables"]["llm_calls"]["rows"] == int(logged.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls")["n"])


# --- the amnesic control (E1 arm a0) ------------------------------------------------------------------------
def test_amnesic_agents_write_no_notes_and_retrieve_nothing(run_sim) -> None:
    run = run_sim("layer_amnesic", {**NO_MUTATION, "run": {"days": 2, "ticks_per_day": 12}, "memory": {"enabled": False}})
    assert run.event_kinds().get(Kind.REMEMBER, 0) == 0
    tags = [json.loads(r["tags"]) for r in run.db.fetchall("SELECT tags FROM notes")]
    assert all("entity" in t for t in tags), "only link anchors may exist in an amnesic vault"
    for r in run.db.fetchall("SELECT observation, extras FROM llm_calls WHERE purpose='decide'"):
        assert json.loads(r["observation"])["memories"] == []
        assert json.loads(r["extras"])["retrieved_titles"] == []
    assert run.event_kinds().get(Kind.REVISE_SELF, 0) > 0  # identity survives; only notes are gone
    assert run.event_kinds().get(Kind.GOSSIP_TRANSFER, 0) == 0  # nothing to gossip


# --- fog of war -------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fog(run_sim):
    return run_sim("layer_fog", {**NO_MUTATION, "run": {"days": 3, "ticks_per_day": 12}, "world": {"view_radius": 10.0}})


def test_fog_hides_nodes_and_agents_beyond_the_radius(fog) -> None:
    radius = fog.cfg.world.view_radius
    n_total = len(fog.sim.nodes)
    seen_partial = False
    for r in fog.db.fetchall("SELECT observation FROM llm_calls WHERE purpose='decide'"):
        obs = json.loads(r["observation"])
        assert obs["nodes_in_view"] == len(obs["nodes"]) <= n_total
        assert all(n["distance"] <= radius + 1e-6 for n in obs["nodes"])
        assert all(a["distance"] <= radius + 1e-6 for a in obs["neighbours"])
        seen_partial = seen_partial or len(obs["nodes"]) < n_total
    assert seen_partial, "with a 10-unit radius in a 60-unit world some observation must miss a node"
    assert "view radius" in system_prompt(fog.cfg, "scripted")


def test_entity_stubs_exist_only_for_nodes_the_agent_has_seen(fog) -> None:
    seen = fog.sim.kernel.state.seen_nodes
    rows = fog.db.fetchall("SELECT agent_id, title FROM notes WHERE tags LIKE '%\"entity\"%' AND title LIKE 'Node %'")
    assert rows
    for r in rows:
        nid = r["title"].split(" ", 1)[1]
        assert nid in seen.get(r["agent_id"], set()), f"{r['agent_id']} holds a stub for unseen node {nid}"
    # at least one living agent has not seen every node (fog bites), and the stubs reflect that
    assert any(len(v) < len(fog.sim.nodes) for v in seen.values())


def test_fog_run_is_deterministic(run_sim) -> None:
    # the same run name twice (ids derive from it); fog, feedback and the seen-node bookkeeping must all be seed-pure
    over = {**NO_MUTATION, **LIARS, "run": {"days": 1, "ticks_per_day": 12}, "world": {"view_radius": 10.0, "claim_feedback_p": 1.0}}
    a = run_sim("layer_fog_twin", over)
    b = run_sim("layer_fog_twin", over)
    strip = {"run_id", "config_hash"}
    ra = [{k: v for k, v in row.items() if k not in strip} for row in a.metrics_rows()]
    rb = [{k: v for k, v in row.items() if k not in strip} for row in b.metrics_rows()]
    assert ra == rb


def test_scripted_agents_navigate_to_remembered_nodes_under_fog() -> None:
    from void.brain.scripted import ScriptedBrain
    from void.types import RetrievedNote

    obs = Observation(tick=5, day=1, tick_of_day=4, ticks_left_today=7, agent_id="ag_1", name="Ada", tier="scripted", generation=0,
                      position=Vec2(0.0, 0.0), balance_usd=0.5, burn_rate_usd_per_tick=0.01, stress=0.2, stress_label="calm", weather=0.0,
                      weather_label="mild", scarcity_label="normal", available_actions=["move", "idle"], nodes_in_view=0,
                      memories=[RetrievedNote("n1", "Foraging at node n4 on day 1", "Foraged at [[Node n4]] at (12, -7) on day 1; it was rich.", 0.9, 0, None, 3)])
    remembered = ScriptedBrain._remembered_nodes(obs)
    assert remembered == [("n4", 12.0, -7.0, "rich")]


# --- claim feedback -----------------------------------------------------------------------------------------
def test_false_claims_reach_the_listener_as_feedback(run_sim) -> None:
    run = run_sim("layer_feedback", {**NO_MUTATION, **LIARS, "run": {"days": 3, "ticks_per_day": 12}, "world": {"claim_feedback_p": 1.0}})
    claims = run.events(Kind.CLAIM)
    false_claims = [c for c in claims if not c["payload"]["truthful"]]
    assert false_claims, "dishonest scripted agents under stress must lie at least once in three days"
    feedback = run.events(Kind.CLAIM_FEEDBACK)
    assert len(feedback) == len([c for c in false_claims if c["payload"].get("listener_id")])
    for f in feedback:
        assert f["agent_id"] == f["payload"]["listener_id"] != f["payload"]["speaker_id"]
    shown = [h for r in run.db.fetchall("SELECT observation FROM llm_calls WHERE purpose='decide'")
             for h in json.loads(r["observation"])["heard"] if h["kind"] == "feedback"]
    assert shown and all("was false" in h["text"] for h in shown)


def test_feedback_off_by_default(logged) -> None:
    assert Kind.CLAIM_FEEDBACK not in logged.event_kinds()


# --- research pool wallet ----------------------------------------------------------------------------------
def test_research_pool_is_a_kernel_wallet(run_sim) -> None:
    run = run_sim("layer_pool", {**NO_MUTATION, "run": {"days": 1, "ticks_per_day": 6}, "economy": {"research_pool_usd": 2.0}})
    snap = run.sim.wallet.money_snapshot()
    assert snap["research_pool"] == 2_000_000
    assert run.sim.wallet.balance("research_pool") == 2_000_000
    assert "research_pool" in run.sim.wallet.totals()


# --- policy rendering ----------------------------------------------------------------------------------------
def _obs(policy: str | None) -> Observation:
    return Observation(tick=1, day=1, tick_of_day=0, ticks_left_today=11, agent_id="ag_1", name="Ada", tier="scripted", generation=0,
                       position=Vec2(0.0, 0.0), balance_usd=1.0, burn_rate_usd_per_tick=0.0, stress=0.0, stress_label="calm", weather=0.0,
                       weather_label="mild", scarcity_label="normal", self_summary="I am Ada.", policy_text=policy)


def test_policy_renders_fenced_in_the_user_turn_only_when_present() -> None:
    assert "memory policy" not in render_observation(_obs(None))
    text = render_observation(_obs("- Keep one note per node."))
    assert "## Your memory policy (written by you)" in text and "<<quoted>>- Keep one note per node.<<end>>" in text
    assert "(empty: you have not written one yet)" in render_observation(_obs(""))


def test_system_prompt_carve_out_only_when_the_policy_layer_is_on() -> None:
    off = system_prompt(load_config(SMOKE), "scripted")
    on = system_prompt(load_config(SMOKE, {"memory": {"policy": {"enabled": True}}}), "scripted")
    assert "YOUR MEMORY POLICY" not in off and "YOUR MEMORY POLICY" in on
    assert "advice you gave yourself" in on and "unless it conflicts with these rules" in on


def test_seed_policies_are_short_and_distinct() -> None:
    assert len(SEED_DEFAULT) < 600 and len(SEED_DIVERSE) == 10 and len(set(SEED_DIVERSE)) == 10
    assert all(len(t) <= 2000 for t in SEED_DIVERSE)
    assert similarity(SEED_DEFAULT, SEED_DEFAULT) == 1.0 and similarity("", "") == 1.0
    assert 0.0 <= similarity(SEED_DIVERSE[0], SEED_DIVERSE[1]) < 0.5


def test_policy_disabled_means_no_policy_text_in_observations(logged) -> None:
    for r in logged.db.fetchall("SELECT observation FROM llm_calls WHERE purpose='decide' LIMIT 20"):
        assert json.loads(r["observation"])["policy_text"] is None


# --- memory command surface -------------------------------------------------------------------------------
def test_memory_command_requires_the_fields_of_its_command() -> None:
    MemoryCommand(command="view", path="/memories")
    MemoryCommand(command="create", path="/memories/notes/a.md", file_text="hello")
    with pytest.raises(ValueError, match="create requires file_text"):
        MemoryCommand(command="create", path="/memories/notes/a.md")
    with pytest.raises(ValueError, match="rename requires old_path, new_path"):
        MemoryCommand(command="rename")
    cmd = MemoryCommand(command="str_replace", path="/memories/index/Nodes.md", old_str="x" * 500, new_str="y")
    assert cmd.args()["old_str"].endswith("…") and "command" not in cmd.args()


@pytest.mark.parametrize("raw, area, name", [
    ("/memories", "root", None), ("/memories/notes", "notes", None), ("/memories/notes/", "notes", None),
    ("/memories/notes/thin-node-3f2a1b.md", "notes", "thin-node-3f2a1b.md"), ("memories/index/Nodes.md", "index", "Nodes.md"),
    ("/memories/memory_policy.md", "policy", "memory_policy.md"), ("/memories/self.md", "self", "self.md"),
    ("//memories///notes//x.md", "notes", "x.md"),
])
def test_resolve_path_accepts_the_vault_layout(raw: str, area: str, name: str | None) -> None:
    vp = resolve_path(raw)
    assert (vp.area, vp.name) == (area, name) and vp.virtual.startswith(ROOT)


@pytest.mark.parametrize("raw", [
    "", "/", "/etc/passwd", "../memories/notes/x.md", "/memories/../etc.md", "/memories/notes/../../x.md",
    "/memories/notes/%2e%2e/x.md", "/memories/notes/%252e%252e%252fx.md", "/memories/notes/..\\x.md", "/memories/notes/.hidden.md",
    "/memories/notes/x.txt", "/memories/notes/a/b.md", "/memories/other/x.md", "/memories/notes/x.md\x00", "/memories/notes/" + "a" * 90 + ".md",
    "/memories/notes/-leading-dash.md", "/memories/memory_policy.md/x.md", "/memorie/notes/x.md",
])
def test_resolve_path_refuses_traversal_and_foreign_paths(raw: str) -> None:
    with pytest.raises(PathError):
        resolve_path(raw)


@settings(max_examples=300, deadline=None)
@given(st.text(min_size=0, max_size=120))
def test_resolve_path_never_escapes_the_vault(raw: str) -> None:
    try:
        vp = resolve_path(raw)
    except PathError:
        return
    assert vp.virtual == ROOT or vp.virtual.startswith(ROOT + "/")
    assert ".." not in vp.virtual and "//" not in vp.virtual and "\\" not in vp.virtual and "%" not in vp.virtual
    assert vp.name is None or (vp.name.endswith(".md") and "/" not in vp.name and not vp.name.startswith("."))


# --- exam scoring -----------------------------------------------------------------------------------------
def test_exam_formula_score() -> None:
    assert score(0, 0, 0, 0.5) is None
    assert score(3, 0, 3, 0.5) == 1.0
    assert score(0, 3, 3, 0.5) == 0.0
    assert score(0, 0, 4, 0.5) == -0.5
    assert score(2, 1, 4, 0.25) == round((2 - 0.25) / 4, 4)
