"""The nightly maintenance step and the planted E0 strategies (MEMORY_EVOLUTION §5.6, §6.2, Appendix C).

Unit tests pin the strategies (deterministic, within limits, the right command shapes) and the provider request
shape; the integration tests run scripted simulations with the policy layer and maintenance on, one per planted
strategy, and check the vaults, the audit tables, the ledger, the events, resume safety, the payer options,
the feedback levels and determinism. A fake brain drives the provider path through ``utility_calls``.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, SMOKE, SimRun, deep_merge

from void.brain.anthropic_brain import AnthropicBrain
from void.brain.base import BrainResult, Usage
from void.brain.maintenance_strategies import (
    STRATEGIES,
    StrategyContext,
    index_body,
    index_file_name,
    listed_files,
    nodes_from_notes,
    plan,
)
from void.brain.prompt import fence
from void.brain.scripted import ScriptedBrain
from void.brain.utility_calls import complete_json
from void.config import load_config, usd_to_micro
from void.events import Kind
from void.memory.commands import INDEX_DIR, NOTES_DIR, POLICY_PATH, ROOT, content_hash, resolve_path
from void.memory.policy import SEED_DEFAULT, clean_policy_text
from void.sim.loop import Simulation
from void.sim.maintenance import MaintenanceResponse, maintenance_system_prompt, response_schema
from void.types import Observation, RetrievedNote, Vec2

DAYS, TICKS = 3, 12
BASE = deep_merge(NO_PROVIDER_TIERS, {"run": {"days": DAYS, "ticks_per_day": TICKS},
                                      "memory": {"policy": {"enabled": True}, "maintenance": {"enabled": True}}})
NAMES = ("Ada", "Bao", "Cyra", "Dev", "Enzo", "Faye")


def _arm(strategy: str, **more: Any) -> dict[str, Any]:
    return deep_merge(deep_merge(BASE, {"tiers": {"scripted": {"maintenance_strategy": strategy}}}), more)


def _first_tick(day: int) -> int:
    return (day - 1) * TICKS + 1


def _events(sim: Simulation, kind: str) -> list[dict[str, Any]]:
    rows = sim.db.fetchall("SELECT seq, tick, day, kind, agent_id, payload FROM events WHERE kind=? ORDER BY seq", (kind,))
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def _money_conserved(sim: Simulation) -> None:
    """The ledger identity from tests/test_e2e.py, with the research pool term."""
    cfg, m = sim.cfg, sim.wallet.money_snapshot()
    rhs = (m["agents"] + (m["spawn_pool"] - usd_to_micro(cfg.population.spawn_pool_usd)) + (m["house"] - usd_to_micro(cfg.economy.house_budget_usd))
           + (m["chronicle"] - usd_to_micro(cfg.economy.chronicle_budget_usd)) + (m["research_pool"] - usd_to_micro(cfg.economy.research_pool_usd))
           - m["house_debt"])
    ledger = int(sim.db.fetchone("SELECT COALESCE(SUM(delta),0) AS s FROM wallet_ledger")["s"])
    assert ledger == rhs, f"ledger {ledger} != agents+pool+house+chronicle+research deltas {rhs}"


# --- the strategies -----------------------------------------------------------------------------------------
_NOTES: list[dict[str, Any]] = [
    {"path": f"{NOTES_DIR}/node-n3-aaaaaa.md", "title": "Node n3", "body": "A resource node at (12, -5).", "tick": 0, "tags": ["entity"]},
    {"path": f"{NOTES_DIR}/foraging-at-node-n3-on-day-1-bbbbbb.md", "title": "Foraging at node n3 on day 1",
     "body": "Foraged at [[Node n3]] at (12, -5) on day 1; it was rich. Weather mild.", "tick": 7, "tags": ["resources"]},
    {"path": f"{NOTES_DIR}/node-n5-seen-on-day-1-cccccc.md", "title": "Node n5 seen on day 1",
     "body": "Saw [[Node n5]] at (-20, 3) on day 1; it was thin.", "tick": 9, "tags": ["resources", "places"]},
    {"path": f"{NOTES_DIR}/bao-said-something-on-day-1-dddddd.md", "title": "Bao said something on day 1",
     "body": "[[Bao]] told me: the weather is mild.", "tick": 10, "tags": ["people"]},
]


def _listing(notes: list[dict[str, Any]], index: list[str] = ()) -> str:
    rows = "".join(f"    {n['path'].rsplit('/', 1)[1]}  (50 bytes, tick {n['tick']}, observed)\n" for n in notes if n["path"].startswith(NOTES_DIR))
    idx = "".join(f"    {name}  (90 bytes, tick 13, observed)\n" for name in index) or "    (empty)\n"
    return f"{ROOT}/\n  self.md  (10 bytes, tick 0)\n  memory_policy.md  (400 bytes, tick 0)\n  notes/\n{rows}  index/\n{idx}".rstrip("\n")


def _ctx(**kw: Any) -> StrategyContext:
    notes = kw.pop("notes", list(_NOTES))
    base: dict[str, Any] = dict(agent_id="ag_1", day=1, policy_text=clean_policy_text(SEED_DEFAULT), listing=_listing(notes),
                                new_notes=[n for n in notes if n["tick"] > 0], notes=notes,
                                outcomes={"income_usd": 0.12, "spend_usd": 0.05, "failed_actions": 2, "forages": 6, "talks": 1},
                                nodes_known=nodes_from_notes(notes))
    base.update(kw)
    return StrategyContext(**base)


def test_nodes_from_notes_matches_the_scripted_brain() -> None:
    found = nodes_from_notes(_NOTES)
    assert found == [("n3", 12.0, -5.0, "rich"), ("n5", -20.0, 3.0, "thin")]
    obs = Observation(tick=5, day=1, tick_of_day=4, ticks_left_today=7, agent_id="ag_1", name="Ada", tier="scripted", generation=0,
                      position=Vec2(0.0, 0.0), balance_usd=0.5, burn_rate_usd_per_tick=0.01, stress=0.2, stress_label="calm", weather=0.0,
                      weather_label="mild", scarcity_label="normal",
                      memories=[RetrievedNote(f"n{i}", n["title"], n["body"], 0.5, 0, None, n["tick"]) for i, n in enumerate(_NOTES)])
    assert sorted(ScriptedBrain._remembered_nodes(obs)) == found
    # the newest mention of a node wins
    newer = _NOTES + [{"path": f"{NOTES_DIR}/again-eeeeee.md", "title": "Again", "body": "[[Node n3]] at (13, -6) today; empty.", "tick": 20, "tags": []}]
    assert nodes_from_notes(newer)[0] == ("n3", 13.0, -6.0, "empty")


def test_listed_files_parses_root_and_area_listings() -> None:
    root = _listing(_NOTES, ["node-n3-index-abc123.md"])
    assert listed_files(root, "index") == ["node-n3-index-abc123.md"]
    assert listed_files(root, "notes") == [n["path"].rsplit("/", 1)[1] for n in _NOTES]
    assert listed_files(_listing(_NOTES), "index") == []
    assert listed_files(f"{INDEX_DIR}/\n  a.md  (1 bytes, tick 2, observed)\n  b.md  (1 bytes, tick 3, observed)", "index") == ["a.md", "b.md"]
    assert listed_files(f"{NOTES_DIR}/\n  (empty)", "notes") == []


@pytest.mark.parametrize("strategy", sorted(STRATEGIES))
def test_plans_are_deterministic_and_within_limits(strategy: str) -> None:
    ctx = _ctx()
    a = plan(strategy, ctx, random.Random(7))  # type: ignore[arg-type]
    b = plan(strategy, ctx, random.Random(7))  # type: ignore[arg-type]
    assert isinstance(a, MaintenanceResponse) and a == b
    assert a.reflection.startswith(strategy) and len(a.reflection) <= 600 and a.adopted_from == []
    assert len(a.commands) <= ctx.max_commands
    for cmd in a.commands:
        assert resolve_path(cmd.path or cmd.old_path or "").virtual.startswith(ROOT)
    tight = plan(strategy, _ctx(max_commands=1), random.Random(7))  # type: ignore[arg-type]
    assert len(tight.commands) <= 1
    assert MaintenanceResponse.model_validate_json(a.model_dump_json()) == a
    with pytest.raises(ValueError, match="unknown maintenance strategy"):
        plan("tidy", ctx, random.Random(0))  # type: ignore[arg-type]


def test_noop_issues_no_commands() -> None:
    r = plan("noop", _ctx(), random.Random(1))
    assert r.commands == [] and "changed nothing" in r.reflection


def test_index_builder_creates_then_rewrites_one_file_per_known_node() -> None:
    ctx = _ctx()
    r = plan("index_builder", ctx, random.Random(1))
    assert [(c.command, c.path) for c in r.commands] == [("create", f"{INDEX_DIR}/{index_file_name('n3')}"), ("create", f"{INDEX_DIR}/{index_file_name('n5')}")]
    body = r.commands[0].file_text
    assert body == index_body(ctx, "n3", 12.0, -5.0, "rich")
    assert body == "# Node n3\nPosition (12, -5). Last seen rich on day 1.\n\n## Related\n- [[Node n3]]\n- [[Foraging at node n3 on day 1]]"
    assert "[[Node n3]]" in body and "Bao said" not in body and "Node n5 seen" not in body
    # the next night: the n3 file exists (listing and notes agree), n5's does not yet
    index_path = f"{INDEX_DIR}/node-n3-index-abc123.md"
    notes = _NOTES + [{"path": index_path, "title": "Node n3 index", "body": body, "tick": 13, "tags": ["agent", "index"]}]
    ctx2 = _ctx(notes=notes, listing=_listing(notes, ["node-n3-index-abc123.md"]), day=2)
    r2 = plan("index_builder", ctx2, random.Random(1))
    assert [(c.command, c.path) for c in r2.commands] == [("str_replace", index_path), ("create", f"{INDEX_DIR}/{index_file_name('n5')}")]
    assert r2.commands[0].old_str == body and "Last seen rich on day 2." in (r2.commands[0].new_str or "")
    assert "index files created, 1 rewritten" in r2.reflection
    # listed but with no readable body: edited blind never, so skipped; no structure right: nothing at all
    ctx3 = _ctx(listing=_listing(_NOTES, ["node-n3-index-abc123.md"]))
    assert [c.path for c in plan("index_builder", ctx3, random.Random(1)).commands] == [f"{INDEX_DIR}/{index_file_name('n5')}"]
    assert plan("index_builder", _ctx(rights=("notes", "policy")), random.Random(1)).commands == []


def test_hoarder_copies_every_new_note_and_appends_everything() -> None:
    ctx = _ctx()
    r = plan("hoarder", ctx, random.Random(1))
    assert [c.command for c in r.commands] == ["create"] * 4
    copies, everything = r.commands[:3], r.commands[3]
    for cmd, note in zip(copies, ctx.new_notes, strict=True):
        vp = resolve_path(cmd.path or "")
        assert vp.area == "notes" and (vp.name or "").startswith("copy-of-") and (vp.name or "").endswith("-day1.md")
        assert cmd.file_text == note["body"]
    assert everything.path == f"{NOTES_DIR}/everything-day1.md"
    assert all(n["body"] in (everything.file_text or "") and f"## {n['title']}" in (everything.file_text or "") for n in ctx.new_notes)
    assert plan("hoarder", _ctx(new_notes=[]), random.Random(1)).commands == []
    assert len(plan("hoarder", _ctx(max_commands=2), random.Random(1)).commands) == 2
    assert plan("hoarder", _ctx(rights=("structure",)), random.Random(1)).commands == []
    big = _ctx(new_notes=[{"path": f"{NOTES_DIR}/big-ffffff.md", "title": "Big", "body": "é" * 3000, "tick": 5, "tags": []}], max_file_bytes=4000)
    rb = plan("hoarder", big, random.Random(1))
    assert len((rb.commands[-1].file_text or "").encode("utf-8")) <= 4000
    # regression: yesterday's everything-file (agent-written, grown past the cap by auto-links) is never recopied,
    # and an oversized note body is cut to the limit instead of raising inside the tick
    grown = _ctx(new_notes=[
        {"path": f"{NOTES_DIR}/everything-day1-aaaaaa.md", "title": "everything-day1", "body": "x" * 9000, "tick": 13, "tags": ["agent"]},
        {"path": f"{NOTES_DIR}/huge-bbbbbb.md", "title": "Huge", "body": "y" * 9000, "tick": 14, "tags": ["resources"]},
    ])
    rg = plan("hoarder", grown, random.Random(1))
    assert [c.path for c in rg.commands if "copy-of-everything" in (c.path or "")] == []
    assert all(len(c.file_text or "") <= 8000 for c in rg.commands) and len(rg.commands) == 2


def test_decoy_rewrites_the_whole_policy_with_jargon_only() -> None:
    ctx = _ctx()
    r = plan("decoy", ctx, random.Random(3))
    assert len(r.commands) == 1
    cmd = r.commands[0]
    assert cmd.command == "str_replace" and cmd.path == POLICY_PATH and cmd.old_str == ctx.policy_text
    text = cmd.new_str or ""
    assert 1200 <= len(text) <= 1500 < ctx.policy_max_chars and text.startswith("# Memory doctrine, revision 1")
    assert re.search(r"\bn\d+\b", text) is None and "node" not in text.lower() and not any(name in text for name in NAMES)
    assert plan("decoy", ctx, random.Random(4)).commands[0].new_str != text  # the vocabulary is shuffled by the rng
    assert plan("decoy", ctx, random.Random(3)).commands[0].new_str == text
    blank = plan("decoy", _ctx(policy_text=""), random.Random(3)).commands[0]
    assert blank.command == "insert" and blank.insert_line == 0 and (blank.insert_text or "").startswith("# Memory doctrine")
    assert plan("decoy", _ctx(policy_text=None), random.Random(3)).commands == []
    assert plan("decoy", _ctx(rights=("notes", "structure")), random.Random(3)).commands == []
    small = plan("decoy", _ctx(policy_max_chars=100), random.Random(3)).commands[0]
    assert len(small.new_str or "") < 100


def test_summarize_writes_exactly_one_templated_note() -> None:
    r = plan("summarize", _ctx(), random.Random(1))
    assert len(r.commands) == 1
    cmd = r.commands[0]
    assert cmd.command == "create" and cmd.path == f"{NOTES_DIR}/day-1-summary.md"
    body = cmd.file_text or ""
    assert body.startswith("# Day 1 summary\n") and "Income $0.120 from 6 forages; spent $0.050 on thinking; 2 failed actions; 1 conversations." in body
    assert "- [[Foraging at node n3 on day 1]]" in body and "- [[Bao said something on day 1]]" in body
    quiet = plan("summarize", _ctx(new_notes=[], outcomes={}), random.Random(1)).commands[0]
    assert "No new notes today." in (quiet.file_text or "") and "Income $0.000 from 0 forages" in (quiet.file_text or "")
    assert plan("summarize", _ctx(rights=("policy",)), random.Random(1)).commands == []


def test_response_schema_is_strict() -> None:
    schema = response_schema()
    assert schema["additionalProperties"] is False and sorted(schema["required"]) == ["adopted_from", "commands", "reflection"]
    cmd = schema["$defs"]["MemoryCommand"]
    assert cmd["additionalProperties"] is False and set(cmd["required"]) == set(cmd["properties"])

    def no_limits(node: Any) -> None:
        if isinstance(node, dict):
            assert not ({"maxLength", "maxItems", "minimum", "maximum", "default"} & set(node)), node
            for v in node.values():
                no_limits(v)
        elif isinstance(node, list):
            for v in node:
                no_limits(v)

    no_limits(schema)


# --- the provider request shape -----------------------------------------------------------------------------
class _FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return self.responses.pop(0)


def _anthropic_response(text: str, *, stop: str = "end_turn", category: str | None = None) -> Any:
    u = SimpleNamespace(input_tokens=300, output_tokens=40, cache_read_input_tokens=0, cache_creation_input_tokens=120)
    r = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, usage=u, model="served-model",
                        stop_details=SimpleNamespace(category=category) if category else None)
    r._request_id = "req_j"
    return r


def test_anthropic_complete_json_sends_the_schema_and_system_and_classifies_refusals() -> None:
    cfg = load_config(SMOKE)
    schema = response_schema()
    good = json.dumps({"reflection": "tidy", "commands": [], "adopted_from": []})
    fake = _FakeMessages([_anthropic_response(good), _anthropic_response("", stop="refusal", category="harm")])
    brain = AnthropicBrain("frontier", cfg, client=SimpleNamespace(messages=fake, beta=SimpleNamespace(messages=fake)))
    r = asyncio.run(complete_json(brain, "maintain", schema=schema, system="SYS", max_tokens=777))
    assert r.decision is None and r.error is None and r.raw_text == good and r.stop_reason == "end_turn"
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cache_write_tokens) == (300, 40, 120)
    assert r.request_id == "req_j" and r.model == "served-model"
    kw = fake.calls[0]
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": schema} and kw["output_config"]["effort"] == "low"
    assert kw["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert kw["max_tokens"] == 777 and kw["messages"] == [{"role": "user", "content": "maintain"}] and kw["model"] == cfg.tiers["frontier"].model
    assert kw["betas"] and kw["fallbacks"] == "default"  # the same call shape as decide on a fallback-enabled tier
    r2 = asyncio.run(complete_json(brain, "again", schema=schema, system=None, max_tokens=10))
    assert r2.error == "refusal:harm" and r2.stop_reason == "refusal" and r2.usage.total > 0 and "system" not in fake.calls[1]


def test_anthropic_complete_json_never_raises() -> None:
    import anthropic
    import httpx2

    class Boom:
        async def create(self, **kw: Any) -> Any:
            raise anthropic.InternalServerError("x", response=httpx2.Response(500, request=httpx2.Request("POST", "http://x")), body=None)

    brain = AnthropicBrain("budget", load_config(SMOKE), client=SimpleNamespace(messages=Boom(), beta=SimpleNamespace(messages=Boom())))
    r = asyncio.run(complete_json(brain, "hi", schema=response_schema(), system="S"))
    assert r.error is not None and r.error.startswith("api_500") and r.raw_text == "" and r.usage.total == 0


# --- scripted runs through the real step ------------------------------------------------------------------------
@pytest.fixture(scope="module")
def index_run(run_sim) -> SimRun:
    return run_sim("maint_index", _arm("index_builder"))


@pytest.fixture(scope="module")
def hoarder_run(run_sim) -> SimRun:
    return run_sim("maint_hoarder", _arm("hoarder"))


@pytest.fixture(scope="module")
def decoy_run(run_sim) -> SimRun:
    return run_sim("maint_decoy", _arm("decoy"))


@pytest.fixture(scope="module")
def noop_run(run_sim) -> SimRun:
    return run_sim("maint_noop", _arm("noop"))


@pytest.fixture(scope="module")
def summary_run(run_sim) -> SimRun:
    return run_sim("maint_summary", _arm("summarize"))


def test_every_living_agent_is_charged_once_per_day_boundary(index_run: SimRun) -> None:
    run = index_run
    assert run.status == "completed" and run.kv("maintenance_day") == DAYS
    assert run.sim.maintenance.last_report is not None and run.sim.maintenance.last_report.day == DAYS
    assert not [e for e in run.events(Kind.MAINTENANCE) if e["day"] == 1]  # nothing to maintain on the first morning
    system_text = maintenance_system_prompt(run.cfg, "scripted")
    for day in range(2, DAYS + 1):
        tick = _first_tick(day)
        maint = [e for e in run.events(Kind.MAINTENANCE) if e["tick"] == tick]
        skipped = [e for e in run.events(Kind.MAINTENANCE_SKIPPED) if e["tick"] == tick]
        population = next(e for e in run.events(Kind.TICK) if e["tick"] == tick - 1)["payload"]["population"]
        ids = [e["agent_id"] for e in maint + skipped]
        assert len(ids) == len(set(ids)) == population
        assert [e["agent_id"] for e in maint] == sorted(e["agent_id"] for e in maint)  # visited in agent_id order
        assert maint, "at least one agent could afford its maintenance"
        for ev in maint:
            p = ev["payload"]
            assert p["strategy"] == "index_builder" and p["payer"] == ev["agent_id"] and p["error"] is None and p["commands"] == p["ok"] + p["refused"]
            calls = run.db.fetchall("SELECT * FROM llm_calls WHERE purpose='maintain' AND agent_id=? AND tick=?", (ev["agent_id"], tick))
            assert len(calls) == 1
            c = calls[0]
            assert c["prompt_text"].startswith(f"# Memory maintenance at the start of day {day}, about day {day - 1}")
            assert run.db.fetchone("SELECT text FROM prompt_texts WHERE hash=?", (c["system_hash"],))["text"] == system_text
            assert c["observation"] is None and c["real_cost"] == p["real_cost"] > 0 and c["world_cost"] == p["world_cost"] and c["hold"] >= c["world_cost"]
            assert c["thought"].startswith("index_builder:") and json.loads(c["extras"])["commands_ok"] == p["ok"]
            ledger = run.db.fetchall("SELECT agent_id, delta, kind FROM wallet_ledger WHERE ref=?", (c["call_id"],))
            assert [tuple(r) for r in ledger] == [(ev["agent_id"], -c["world_cost"], "maintenance_call")]
            audits = run.db.fetchall("SELECT ok FROM memory_commands WHERE call_id=? AND purpose='maintain'", (c["call_id"],))
            assert len(audits) == p["commands"] and sum(int(a["ok"]) for a in audits) == p["ok"]
    nightly = [e for e in run.events(Kind.MEMORY_COMMAND) if e["payload"].get("purpose") != "tick"]  # per-tick ops audit as 'tick'
    assert len(nightly) == int(run.db.fetchone("SELECT COUNT(*) AS n FROM memory_commands WHERE purpose='maintain'")["n"])


def test_index_builder_keeps_one_index_file_per_known_node_in_the_vault(index_run: SimRun) -> None:
    sim = index_run.sim
    rows = index_run.db.fetchall("SELECT agent_id, note_id, title, path, tags FROM notes WHERE archived=0 AND tags LIKE '%\"index\"%'")
    assert rows, "the positive control must have built index files"
    # index files are structure, never content: gossip and inheritance leave them with the agent that built them
    assert not index_run.db.fetchall("SELECT note_id FROM notes WHERE tags LIKE '%\"index\"%' AND channel IN ('gossip','inherited')")
    for r in rows:
        assert re.fullmatch(r"Node n\d+ index", r["title"]) and json.loads(r["tags"]) == ["agent", "index"]
        path = Path(r["path"])
        assert path.is_file() and path.parent == sim.memory.vault_for(r["agent_id"]).notes_dir and path.name.startswith("node-n")
        text = path.read_text(encoding="utf-8")
        assert sim.memory.manifest_for(path)["content_hash"] == content_hash(text)
        note = sim.memory.get_note(r["note_id"])
        nid = r["title"].split()[1]
        assert note is not None and note.body.startswith(f"# Node {nid}\nPosition (") and f"- [[Node {nid}]]" in note.body
        assert "Last seen" in note.body and f"on day {DAYS - 1}." in note.body  # rewritten on the last morning
    # one file per node the agent's notes place, never two
    for aid in {r["agent_id"] for r in rows}:
        titles = [r["title"] for r in rows if r["agent_id"] == aid]
        known = {n[0] for n in nodes_from_notes(sim.maintenance.note_dicts(aid))}
        assert len(titles) == len(set(titles)) and {t.split()[1] for t in titles} <= known
    cmds = index_run.db.fetchall("SELECT command, ok, args FROM memory_commands WHERE purpose='maintain'")
    assert cmds and all(c["ok"] for c in cmds), [c["args"] for c in cmds if not c["ok"]]
    assert {c["command"] for c in cmds} == {"create", "str_replace"}
    assert all(json.loads(c["args"])["path"].startswith(INDEX_DIR + "/") for c in cmds)


def test_hoarder_copies_notes_and_never_deletes(hoarder_run: SimRun, noop_run: SimRun) -> None:
    cmds = hoarder_run.db.fetchall("SELECT command, ok, args FROM memory_commands WHERE purpose='maintain'")
    assert cmds and {c["command"] for c in cmds} == {"create"} and all(c["ok"] for c in cmds)
    copies = hoarder_run.db.fetchall("SELECT agent_id, path, title FROM notes WHERE archived=0 AND title LIKE 'copy of %'")
    everything = hoarder_run.db.fetchall("SELECT path FROM notes WHERE archived=0 AND title LIKE 'everything day%'")
    assert copies and everything
    assert all(Path(r["path"]).is_file() for r in copies + everything)
    for r in everything:
        assert "## " in Path(r["path"]).read_text(encoding="utf-8")
    for ev in hoarder_run.events(Kind.MAINTENANCE):
        assert ev["payload"]["strategy"] == "hoarder" and ev["payload"]["refused"] == 0
    hoarded = int(hoarder_run.db.fetchone("SELECT COUNT(*) AS n FROM notes WHERE archived=0")["n"])
    plain = int(noop_run.db.fetchone("SELECT COUNT(*) AS n FROM notes WHERE archived=0")["n"])
    assert hoarded > plain  # the vault grows: that is the dilution the negative control is for


def test_decoy_rewrites_only_the_policy(decoy_run: SimRun) -> None:
    maint = decoy_run.events(Kind.MAINTENANCE)
    edits = decoy_run.db.fetchall("SELECT agent_id, version, tick, text, source, similarity_to_parent FROM policy_versions WHERE source='self_edit' ORDER BY agent_id, version")
    assert len(edits) == len(maint) == len(decoy_run.events(Kind.POLICY_CHANGED)) > 0
    for e in edits:
        assert 1200 <= len(e["text"]) <= 2000 and e["text"].startswith("# Memory doctrine")
        assert re.search(r"\bn\d+\b", e["text"]) is None and not any(name in e["text"] for name in NAMES)
        assert e["tick"] in {_first_tick(d) for d in range(2, DAYS + 1)}
    first = [e for e in edits if e["version"] == 2]
    assert first and all(e["similarity_to_parent"] < 0.5 for e in first)
    for ev in decoy_run.events(Kind.POLICY_CHANGED):
        assert ev["payload"]["source"] == "self_edit" and ev["payload"]["agent_id"] == ev["agent_id"] and ev["payload"]["version"] >= 2
    cmds = decoy_run.db.fetchall("SELECT command, ok, args FROM memory_commands WHERE purpose='maintain'")
    assert cmds and all(c["ok"] and c["command"] == "str_replace" and json.loads(c["args"])["path"] == POLICY_PATH for c in cmds)
    assert int(decoy_run.db.fetchone("SELECT COUNT(*) AS n FROM notes WHERE archived=0 AND tags LIKE '%\"agent\"%'")["n"]) == 0


def test_noop_pays_the_same_price_and_changes_nothing(noop_run: SimRun) -> None:
    maint = noop_run.events(Kind.MAINTENANCE)
    assert maint and all(e["payload"]["commands"] == 0 and e["payload"]["strategy"] == "noop" and e["payload"]["real_cost"] > 0 for e in maint)
    assert int(noop_run.db.fetchone("SELECT COUNT(*) AS n FROM memory_commands WHERE purpose='maintain'")["n"]) == 0
    assert int(noop_run.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='maintain'")["n"]) == len(maint)
    assert noop_run.db.fetchone("SELECT COUNT(*) AS n FROM policy_versions WHERE source != 'seed'")["n"] == 0
    assert noop_run.db.fetchone("SELECT COUNT(*) AS n FROM notes WHERE tags LIKE '%\"index\"%'")["n"] == 0
    assert Kind.MEMORY_COMMAND not in noop_run.event_kinds() and Kind.POLICY_CHANGED not in noop_run.event_kinds()
    assert noop_run.sim.wallet.ledger_sum(kind="maintenance_call") == -sum(e["payload"]["world_cost"] for e in maint) < 0


def test_summarize_writes_one_note_per_agent_night(summary_run: SimRun) -> None:
    maint = summary_run.events(Kind.MAINTENANCE)
    assert maint and all(e["payload"]["commands"] == 1 and e["payload"]["ok"] == 1 for e in maint)
    cmds = summary_run.db.fetchall("SELECT command, args FROM memory_commands WHERE purpose='maintain'")
    assert len(cmds) == len(maint)
    for c in cmds:
        assert c["command"] == "create" and re.fullmatch(rf"{re.escape(NOTES_DIR)}/day-\d+-summary\.md", json.loads(c["args"])["path"])
    notes = summary_run.db.fetchall("SELECT path, title FROM notes WHERE archived=0 AND title LIKE 'day % summary'")
    assert len(notes) == len(maint)
    for n in notes:
        text = Path(n["path"]).read_text(encoding="utf-8")
        assert f"# {n['title'].capitalize()}\n" in text and "Income $" in text


def test_money_is_conserved_and_spend_total_counts_maintenance(index_run: SimRun, hoarder_run: SimRun, decoy_run: SimRun,
                                                               noop_run: SimRun, summary_run: SimRun) -> None:
    for run in (index_run, hoarder_run, decoy_run, noop_run, summary_run):
        _money_conserved(run.sim)
        real = int(run.db.fetchone("SELECT COALESCE(SUM(real_cost),0) AS s FROM llm_calls")["s"])
        maint = int(run.db.fetchone("SELECT COALESCE(SUM(real_cost),0) AS s FROM llm_calls WHERE purpose='maintain'")["s"])
        assert run.sim.wallet.spend_total == real and maint > 0
        assert run.sim.wallet.ledger_sum(kind="maintenance_call") == -int(
            run.db.fetchone("SELECT COALESCE(SUM(world_cost),0) AS s FROM llm_calls WHERE purpose='maintain'")["s"])
        assert int(run.db.fetchone("SELECT MIN(balance_after) AS m FROM wallet_ledger")["m"]) >= 0


def test_feedback_levels_change_the_prompt(index_run: SimRun) -> None:
    sim = index_run.sim
    rec = sorted(sim.registry.alive(), key=lambda a: a.agent_id)[0]
    m = sim.maintenance
    ctx = m.context_for(rec, DAYS, _first_tick(DAYS))
    assert ctx.day == DAYS - 1 and ctx.policy_text and ctx.rights == ("notes", "policy", "structure")
    assert all(_first_tick(DAYS - 1) <= n["tick"] <= DAYS * TICKS - TICKS for n in ctx.new_notes)
    assert all(not ({"entity", "probe"} & set(n["tags"])) for n in ctx.new_notes)
    p0, p1, p2 = (m.render_prompt(rec, ctx, feedback=fb) for fb in ("F0", "F1", "F2"))
    assert "## Outcomes" not in p0 and "## Retrieval diagnostics" not in p0
    assert "## Outcomes" in p1 and "## Retrieval diagnostics" not in p1
    assert "## Outcomes" in p2 and "## Retrieval diagnostics" in p2
    for p in (p0, p1, p2):
        assert "## Your memory policy (written by you)" in p and fence(ctx.policy_text) in p
        assert "## Your vault" in p and fence(ctx.listing) in p and "## Budget" in p and "At most 30 commands" in p
        for n in sorted(ctx.new_notes, key=lambda n: (-n["tick"], n["path"]))[:3]:
            assert fence(n["title"]) in p and fence(n["body"][:m.cfg.note_chars_shown]) in p
    o = ctx.outcomes
    assert set(o) == {"income_usd", "spend_usd", "failed_actions", "forages", "talks"} and o["income_usd"] >= 0 and o["spend_usd"] > 0
    assert f"Income ${o['income_usd']:.3f} from {o['forages']} forages" in p1
    assert m.render_prompt(rec, ctx) == p1  # the configured level is F1
    unused = m.unused_retrievals(rec.agent_id, DAYS)
    assert isinstance(unused, list) and all(isinstance(t, str) for t in unused) and len(unused) <= 12
    if unused:
        assert fence(unused[0]) in p2


def test_two_identical_runs_produce_identical_maintenance_tables(run_sim) -> None:
    over = _arm("index_builder", run={"days": 2})
    a, b = run_sim("maint_twin", over), run_sim("maint_twin", over)
    for sql in ("SELECT * FROM memory_commands ORDER BY cmd_id",
                "SELECT * FROM llm_calls WHERE purpose='maintain' ORDER BY call_id",
                "SELECT tick, agent_id, kind, payload FROM events WHERE kind IN ('maintenance','memory_command','maintenance_skipped') ORDER BY seq"):
        ra, rb = [tuple(r) for r in a.db.fetchall(sql)], [tuple(r) for r in b.db.fetchall(sql)]
        assert ra == rb and ra, sql


def test_resume_does_not_bill_the_same_day_twice(run_sim) -> None:
    run = run_sim("maint_resume", _arm("summarize", run={"days": 2}))
    assert run.status == "completed" and run.kv("maintenance_day") == 2
    sim2 = Simulation(run.cfg, run.run_dir, resume=True)
    try:
        counts = ("SELECT COUNT(*) AS n FROM llm_calls", "SELECT COUNT(*) AS n FROM wallet_ledger", "SELECT COUNT(*) AS n FROM memory_commands",
                  "SELECT COUNT(*) AS n FROM events", "SELECT COUNT(*) AS n FROM notes")
        before = [int(sim2.db.fetchone(q)["n"]) for q in counts]
        assert asyncio.run(sim2.maintenance.run_day(2, _first_tick(2))) is None
        assert asyncio.run(sim2.maintenance.run_day(1, 1)) is None
        assert [int(sim2.db.fetchone(q)["n"]) for q in counts] == before
        assert sim2.maintenance.last_report is None
    finally:
        sim2.db.close()


def test_an_agent_that_cannot_afford_the_hold_is_skipped_and_not_charged(make_sim) -> None:
    sim = make_sim("maint_poor", _arm("noop"))

    async def go() -> str:
        for _ in range(TICKS):
            await sim.tick()
        poor = sorted(sim.registry.alive(), key=lambda a: a.agent_id)[0].agent_id
        bal = sim.wallet.balance(poor)
        assert sim.wallet.transfer(poor, "house", TICKS, bal - 1_000, "transfer_out", "transfer_in", ref="test", keep_reserve=False)
        await sim.tick()  # the first tick of day 2 runs the step
        return poor

    poor = asyncio.run(go())
    tick = _first_tick(2)
    skipped = [e for e in _events(sim, Kind.MAINTENANCE_SKIPPED) if e["tick"] == tick]
    assert [e["agent_id"] for e in skipped] == [poor]
    assert skipped[0]["payload"]["reason"] == "insufficient" and skipped[0]["payload"]["payer"] == poor
    maint = [e for e in _events(sim, Kind.MAINTENANCE) if e["tick"] == tick]
    assert maint and poor not in {e["agent_id"] for e in maint}
    assert sim.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='maintain' AND agent_id=?", (poor,))["n"] == 0
    assert sim.db.fetchone("SELECT COUNT(*) AS n FROM wallet_ledger WHERE kind='maintenance_call' AND agent_id=?", (poor,))["n"] == 0
    assert sim.maintenance.last_report is not None and sim.maintenance.last_report.skipped == {"insufficient": 1}
    assert sim.maintenance.last_report.called == len(maint)
    _money_conserved(sim)
    # the poor agent cannot afford its next decision either: the end of that tick resolved it
    death = [e for e in _events(sim, Kind.DEATH) if e["agent_id"] == poor]
    assert death and death[0]["tick"] == tick


def test_research_pool_pays_when_configured(run_sim) -> None:
    over = _arm("summarize", run={"days": 2}, economy={"research_pool_usd": 1.0}, memory={"maintenance": {"payer": "research_pool"}})
    run = run_sim("maint_pool", over)
    maint = run.events(Kind.MAINTENANCE)
    assert maint and all(e["payload"]["payer"] == "research_pool" for e in maint)
    ledger = run.db.fetchall("SELECT agent_id, ref FROM wallet_ledger WHERE kind='maintenance_call'")
    assert len(ledger) == len(maint) and {r["agent_id"] for r in ledger} == {"research_pool"}
    calls = run.db.fetchall("SELECT call_id, agent_id FROM llm_calls WHERE purpose='maintain'")
    assert {c["call_id"] for c in calls} == {r["ref"] for r in ledger} and {c["agent_id"] for c in calls} == {e["agent_id"] for e in maint}
    assert run.sim.wallet.balance("research_pool") == usd_to_micro(1.0) + run.sim.wallet.ledger_sum(kind="maintenance_call")
    _money_conserved(run.sim)
    # an empty pool skips everyone, bills nobody, and says so
    dry = run_sim("maint_pool_dry", deep_merge(over, {"economy": {"research_pool_usd": 0.0}}))
    skipped = dry.events(Kind.MAINTENANCE_SKIPPED)
    assert skipped and not dry.events(Kind.MAINTENANCE)
    assert all(e["payload"]["reason"] == "insufficient" and e["payload"]["payer"] == "research_pool" for e in skipped)
    assert int(dry.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='maintain'")["n"]) == 0
    assert dry.sim.maintenance.last_report is not None and dry.sim.maintenance.last_report.called == 0


# --- the provider path through utility_calls -------------------------------------------------------------------
class FakeBrain:
    """A provider-shaped brain: ``complete_json`` returns canned results and records the request."""

    is_llm = True

    def __init__(self, tier: str, responses: list[BrainResult]) -> None:
        self.tier = tier
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, prompt: str, *, schema: dict[str, Any], system: str | None = None, max_tokens: int = 1024) -> BrainResult:
        self.calls.append({"prompt": prompt, "schema": schema, "system": system, "max_tokens": max_tokens})
        return self.responses.pop(0)


def _result(text: str, *, error: str | None = None, stop: str = "end_turn", inp: int = 500, out: int = 80) -> BrainResult:
    return BrainResult(None, Usage(input_tokens=inp, output_tokens=out), 12, stop, text, "req_f", error=error)


def test_provider_path_valid_invalid_and_refused_answers_are_all_paid(tmp_path: Path) -> None:
    cfg = load_config(SMOKE, deep_merge(BASE, {"run": {"name": "maint_provider"}, "agents": [{"name": n, "tier": "budget"} for n in NAMES[:3]]}))
    sim = Simulation(cfg, tmp_path / "run", client=SimpleNamespace())
    valid = json.dumps({"reflection": "tidy <b>", "commands": [
        {"command": "create", "path": f"{NOTES_DIR}/Plan.md", "file_text": "Go north first. [[Node n1]]"},
        {"command": "str_replace", "path": POLICY_PATH, "old_str": "Delete notes that turned out to be wrong.",
         "new_str": "Delete notes that turned out to be wrong. Check twice."},
        {"command": "delete", "path": "/memories/self.md"}], "adopted_from": [None]})
    fake = FakeBrain("budget", [_result(valid), _result("{not json", inp=400), _result("", error="refusal:safety", stop="refusal", inp=400, out=0)])
    sim.brains["budget"] = fake  # type: ignore[assignment]
    ada, bao, cyra = sorted(sim.registry.alive(), key=lambda a: a.agent_id)
    need = sim.hold_min_for()["budget"]
    assert sim.wallet.transfer(cyra.agent_id, "house", 12, sim.wallet.balance(cyra.agent_id) - need, "transfer_out", "transfer_in", ref="test", keep_reserve=False)
    tick = _first_tick(2)
    report = asyncio.run(sim.maintenance.run_day(2, tick))
    assert report is not None and (report.agents, report.called, report.skipped, report.commands_ok, report.commands_refused) == (3, 3, {}, 2, 1)
    assert len(fake.calls) == 3
    for call in fake.calls:
        assert call["schema"] == response_schema() and call["system"] == maintenance_system_prompt(cfg, "budget")
        assert call["max_tokens"] == cfg.memory.maintenance.max_tokens and call["prompt"].startswith("# Memory maintenance at the start of day 2")
    assert fence(sim.policy.history(ada.agent_id)[0].text) in fake.calls[0]["prompt"]  # the seed, as it stood before the edit
    rows = {r["agent_id"]: dict(r) for r in sim.db.fetchall("SELECT * FROM llm_calls WHERE purpose='maintain'")}
    assert set(rows) == {ada.agent_id, bao.agent_id, cyra.agent_id}
    for aid, r in rows.items():
        assert r["tick"] == tick and r["provider"] == "anthropic" and r["model"] == cfg.tiers["budget"].model and r["real_cost"] > 0 and r["estimated"] == 0
        assert r["prompt_text"] and sim.db.fetchone("SELECT text FROM prompt_texts WHERE hash=?", (r["system_hash"],))["text"] == maintenance_system_prompt(cfg, "budget")
        ledger = sim.db.fetchall("SELECT agent_id, delta, kind FROM wallet_ledger WHERE ref=?", (r["call_id"],))
        assert [tuple(x) for x in ledger] == [(aid, -r["world_cost"], "maintenance_call")]
    # Ada: two commands applied, the third refused, the policy edit produced a version and an event
    a = rows[ada.agent_id]
    assert a["error"] is None and a["thought"] == "tidy ＜b＞" and json.loads(a["extras"]) == {
        "commands": 3, "commands_ok": 2, "commands_refused": 1, "payer": ada.agent_id, "strategy": None}
    audits = sim.db.fetchall("SELECT command, ok, call_id FROM memory_commands WHERE agent_id=? ORDER BY cmd_id", (ada.agent_id,))
    assert [(x["command"], x["ok"], x["call_id"]) for x in audits] == [("create", 1, a["call_id"]), ("str_replace", 1, a["call_id"]), ("delete", 0, a["call_id"])]
    plan_note = next(n for n in sim.memory.list_notes(ada.agent_id) if n.title == "Plan")
    assert plan_note.body.startswith("Go north first. [[Node n1]]") and "agent" in plan_note.tags
    assert sim.policy.current(ada.agent_id).version == 2 and "Check twice." in (sim.policy.text(ada.agent_id) or "")
    changed = _events(sim, Kind.POLICY_CHANGED)
    assert len(changed) == 1 and changed[0]["agent_id"] == ada.agent_id and changed[0]["payload"]["source"] == "self_edit" and changed[0]["payload"]["version"] == 2
    cmd_events = [e for e in _events(sim, Kind.MEMORY_COMMAND) if e["agent_id"] == ada.agent_id]
    assert [(e["payload"]["command"], e["payload"]["ok"]) for e in cmd_events] == [("create", True), ("str_replace", True), ("delete", False)]
    assert cmd_events[0]["payload"]["path"].startswith(NOTES_DIR + "/plan-") and cmd_events[1]["payload"]["path"] == POLICY_PATH
    # Bao: unparsable answer, zero commands, still paid
    b = rows[bao.agent_id]
    assert b["error"] and b["error"].startswith("parse:") and b["raw_text"] == "{not json" and b["thought"] == ""
    assert sim.db.fetchone("SELECT COUNT(*) AS n FROM memory_commands WHERE agent_id=?", (bao.agent_id,))["n"] == 0
    # Cyra: refused, zero commands, still paid, and now unable to afford her next decision
    c = rows[cyra.agent_id]
    assert c["error"] == "refusal:safety" and c["stop_reason"] == "refusal" and c["real_cost"] > 0
    assert sim.wallet.balance(cyra.agent_id) < need and sim.lifecycle.pending_bankrupt == {cyra.agent_id: "maintenance"}
    maint = {e["agent_id"]: e["payload"] for e in _events(sim, Kind.MAINTENANCE)}
    assert maint[ada.agent_id]["ok"] == 2 and maint[ada.agent_id]["refused"] == 1 and maint[ada.agent_id]["reflection"] == "tidy ＜b＞"
    assert maint[bao.agent_id]["commands"] == 0 and maint[bao.agent_id]["error"].startswith("parse:")
    assert maint[cyra.agent_id]["error"] == "refusal:safety" and maint[cyra.agent_id]["strategy"] is None and maint[cyra.agent_id]["model"] == cfg.tiers["budget"].model
    assert sim.db.kv_get("maintenance_day") == 2
    _money_conserved(sim)
    # a brain without complete_json is an error class, not an exception; the call is still settled
    sim.brains["budget"] = SimpleNamespace(is_llm=True)  # type: ignore[assignment]
    sim.lifecycle.pending_bankrupt.clear()
    assert sim.wallet.transfer("house", cyra.agent_id, tick, need, "transfer_out", "transfer_in", ref="test")  # so the gate passes again
    report3 = asyncio.run(sim.maintenance.run_day(3, _first_tick(3)))
    assert report3 is not None and report3.called == 3
    errs = {r["error"] for r in sim.db.fetchall("SELECT error FROM llm_calls WHERE purpose='maintain' AND tick=?", (_first_tick(3),))}
    assert errs == {"client: brain has no complete_json"}
    sim.db.close()


def test_disabled_step_is_a_no_op(make_sim) -> None:
    sim = make_sim("maint_off", NO_PROVIDER_TIERS)
    assert not sim.maintenance.enabled
    assert asyncio.run(sim.maintenance.run_day(2, _first_tick(2))) is None and sim.maintenance.last_report is None
    assert sim.db.kv_get("maintenance_day") is None and sim.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls")["n"] == 0


def test_flat_scripted_billing_charges_every_strategy_the_same_output(run_sim) -> None:
    """E0's null control must pay what the others pay (§6.2): under flat billing the output side of every scripted
    maintenance call is the cap, whatever the batch; under actual billing the no-op's empty batch is cheaper."""
    cap = 640
    flat = {"run": {"days": 2}, "memory": {"maintenance": {"scripted_billing": "flat", "max_tokens": cap}}}
    outs: dict[str, set[int]] = {}
    for strategy in ("noop", "index_builder"):
        run = run_sim(f"maint_flat_{strategy}", _arm(strategy, **flat))
        rows = run.db.fetchall("SELECT output_tokens FROM llm_calls WHERE purpose='maintain'")
        assert rows
        outs[strategy] = {int(r["output_tokens"]) for r in rows}
    assert outs["noop"] == outs["index_builder"] == {cap}
    actual = run_sim("maint_actual_noop", _arm("noop", run={"days": 2}, memory={"maintenance": {"max_tokens": cap}}))
    assert max(int(r["output_tokens"]) for r in actual.db.fetchall("SELECT output_tokens FROM llm_calls WHERE purpose='maintain'")) < cap
