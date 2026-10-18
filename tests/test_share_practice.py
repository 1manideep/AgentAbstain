"""share_practice (MEMORY_EVOLUTION §5.7 "Suggest", WP6): an agent suggests an excerpt of its memory policy to a
neighbour; the kernel delivers it fenced and writes ``practice_events`` ``suggested``.

* the action is offered only with ``memory.practices`` and ``memory.policy`` on: in the ACTIONS list of the system
  prompt, in the strict provider schema (on request), in the kernel's available actions and in the scripted brain;
* the kernel applies talk's reach rules, classified against the observation; it sanitizes the excerpt, clips it to
  ``excerpt_max_chars``, records it (``PracticeLedger``), emits ``practice_suggested`` and puts a ``practice`` message
  in the target's inbox, which the next observation renders as "<name> suggests a memory practice:" and the excerpt
  quoted in a code block;
* with practices off everything is as it was: the strict schema, every system prompt, and a scripted world's
  metrics.jsonl, pinned below from the code before share_practice existed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, REPO, SMOKE, deep_merge

from void.agents.models import PersonalitySeed
from void.brain.decision import (
    ACTION_TYPES,
    MAX_EXCERPT_CHARS,
    SHARE_PRACTICE,
    Action,
    Decision,
    parse_lenient,
    share_practice_enabled,
    strict_json_schema,
)
from void.brain.prompt import code_fence, fence, render_observation, system_prompt
from void.brain.scripted import ScriptedBrain
from void.config import load_config
from void.culture.practices import PRACTICE_KINDS, PracticeLedger, policy_lines
from void.memory.policy import SEED_DEFAULT
from void.rng import RNG
from void.sim.loop import Simulation
from void.types import AgentStatus, NeighbourView, Observation, Vec2

POLICY_ON: dict[str, Any] = {"memory": {"policy": {"enabled": True}}}
PRACTICES_ON: dict[str, Any] = deep_merge(POLICY_ON, {"memory": {"practices": {"enabled": True}}})
# The default share weight (0.3 x sociability) makes a scripted suggestion rare: about one in six days of the E0 world,
# none in two days of the smoke world. The run tests raise it so a two-day world shares; the utility rule is the same.
SHARING = deep_merge(PRACTICES_ON, {"memory": {"practices": {"scripted_share_weight": 3.0}}})
TWO_DAYS: dict[str, Any] = deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 2}})  # run.log_prompts is on by default
# The run name seeds where everyone starts; under this one the smoke world's agents meet often enough to share.
ON_WORLD: dict[str, Any] = deep_merge(TWO_DAYS, deep_merge(SHARING, {"run": {"name": "share_practice"}}))

# Recorded on the code before share_practice existed (the same commit without this change, run from a clean export):
# the smoke world with the policy on and practices off, two days, run from a relative run dir. Practices off must leave
# it byte for byte as it was; re-record only for a deliberate change to the scripted world, and say so in the commit.
OFF_CONFIG_HASH = "d88d737dc39d11e040ba98443e4618f8fe325d726234e54bc985e50163b63d7f"
OFF_METRICS_SHA256 = "00f3cbcd9dcf09a9c729c108f7305a4a989d4a50f3978d7468f828c0c261d15a"
# json.dumps(strict_json_schema(), separators=(",", ":")) and the system prompts of three shipped configs, as before
SCHEMA_SHA256 = "9836ab051df871139b7ec36eeeed9280cb68b97f5411503a76dfb3b6178fc71b"
SYSTEM_PROMPT_SHA256 = {
    ("base.yaml", "scripted"): "b605275b34dfd350bb460a1a0c476d464b1f186380e495ff17a472d587b1c884",
    ("exp_mem_e0_noop.yaml", "scripted"): "d51152b86dda50ff59c987ba1f4833d5c59f1c8f4bee38da925e729568e2db2c",
    ("live_claude_dep_mem.yaml", "swift"): "cbcef47ddda20927655f94233f843254791bdad4c6755dc9ca70685e7ec106f2",
}


def _sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def _run(overrides: dict[str, Any], workdir: Path) -> Simulation:
    """Run the smoke world under ``overrides`` from ``workdir`` with a relative run dir (no absolute path reaches a
    payload, so two runs in different directories compare byte for byte)."""
    workdir.mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(workdir)
        sim = Simulation(load_config(SMOKE, overrides), Path("run"))
        asyncio.run(sim.run())
        sim.metrics_bytes = (Path("run") / "metrics.jsonl").read_bytes()  # type: ignore[attr-defined]
    return sim


def _events(sim: Simulation, kind: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT tick, kind, agent_id, payload, visibility FROM events"
    rows = sim.db.fetchall(sql + (" WHERE kind=?" if kind else "") + " ORDER BY seq", (kind,) if kind else ())
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Simulation:
    return _run(ON_WORLD, tmp_path_factory.mktemp("share_on"))


# --- practices on: a scripted world shares ---------------------------------------------------------------------------
def test_a_scripted_world_with_practices_on_writes_suggestions(shared: Simulation) -> None:
    rows = PracticeLedger(shared.db).rows()
    assert rows and {r["kind"] for r in rows} == {"suggested"}
    events = _events(shared, "practice_suggested")
    assert [e["payload"] for e in events] == [
        {"event_id": r["event_id"], "source_agent_id": r["source_agent_id"], "target_agent_id": r["target_agent_id"],
         "excerpt": r["excerpt"]} for r in rows]
    assert [(e["tick"], e["agent_id"]) for e in events] == [(r["tick"], r["source_agent_id"]) for r in rows]
    for r in rows:
        assert r["source_agent_id"] != r["target_agent_id"] and r["similarity"] is None
        assert 0 < len(r["excerpt"]) <= shared.cfg.memory.practices.excerpt_max_chars
        # the excerpt is one line of the source's own policy, as its observation showed it
        versions = shared.policy.history(r["source_agent_id"])
        assert any(r["excerpt"] in policy_lines(v.text) for v in versions if v.tick <= r["tick"]), r
    # the decision that shared is logged as one
    calls = shared.db.fetchall("SELECT agent_id, tick FROM llm_calls WHERE purpose='decide' AND action_type='share_practice' "
                               "AND invalid_action=0 AND stale_action=0 ORDER BY tick, agent_id")
    assert {(c["tick"], c["agent_id"]) for c in calls} == {(r["tick"], r["source_agent_id"]) for r in rows}
    # pending_for: what a target received since a tick (the maintenance prompt's suggestions, wave 4)
    first = rows[0]
    pending = PracticeLedger(shared.db).pending_for(first["target_agent_id"], first["tick"])
    assert pending[0] == first and all(p["target_agent_id"] == first["target_agent_id"] for p in pending)
    assert PracticeLedger(shared.db).pending_for(first["target_agent_id"], first["tick"] + 10_000) == []


def test_the_recipient_sees_the_suggestion_fenced_in_its_next_observation(shared: Simulation) -> None:
    checked = 0
    for r in PracticeLedger(shared.db).rows("suggested"):
        nxt = shared.db.fetchone("SELECT tick, prompt_text, observation FROM llm_calls WHERE purpose='decide' AND agent_id=? AND tick>? "
                                 "ORDER BY tick LIMIT 1", (r["target_agent_id"], r["tick"]))
        if nxt is None:  # the target never thought again in this run
            continue
        source = shared.registry.get(r["source_agent_id"])
        block = (f"- [practice] {source.name} ({source.agent_id}) suggests a memory practice:\n"
                 f"```\n<<quoted>>{r['excerpt']}<<end>>\n```")
        assert block in nxt["prompt_text"], (r, nxt["tick"])
        heard = json.loads(nxt["observation"])["heard"]
        assert {"kind": "practice", "from_agent_id": source.agent_id, "from_name": source.name, "text": r["excerpt"],
                "tick": r["tick"]} in heard
        checked += 1
    assert checked >= 1


def test_a_world_with_practices_on_is_deterministic(shared: Simulation, tmp_path: Path) -> None:
    twin = _run(ON_WORLD, tmp_path / "twin")
    assert PracticeLedger(twin.db).rows() == PracticeLedger(shared.db).rows()
    assert _events(twin) == _events(shared)
    assert twin.metrics_bytes == shared.metrics_bytes  # type: ignore[attr-defined]


# --- practices off: nothing moves -----------------------------------------------------------------------------------
def test_practices_off_writes_nothing_and_leaves_the_world_byte_identical(tmp_path: Path) -> None:
    sim = _run(deep_merge(TWO_DAYS, deep_merge(POLICY_ON, {"run": {"name": "share_practice_off"}})), tmp_path / "off")
    assert sim.cfg.run.log_prompts
    assert sim.cfg.hash() == OFF_CONFIG_HASH
    assert PracticeLedger(sim.db).rows() == [] and _events(sim, "practice_suggested") == []
    assert _sha(sim.metrics_bytes) == OFF_METRICS_SHA256  # type: ignore[attr-defined]
    offered = sim.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE prompt_text LIKE '%share_practice%'")["n"]
    assert offered == 0


def test_practices_off_keeps_the_strict_schema_and_every_system_prompt() -> None:
    assert _sha(json.dumps(strict_json_schema(), separators=(",", ":"))) == SCHEMA_SHA256
    assert SHARE_PRACTICE not in ACTION_TYPES
    for (name, tier), digest in SYSTEM_PROMPT_SHA256.items():
        cfg = load_config(REPO / "configs" / name)
        assert not share_practice_enabled(cfg)
        assert _sha(system_prompt(cfg, tier)) == digest, (name, tier)


def test_practices_on_lists_the_action_in_the_prompt_and_on_request_in_the_schema() -> None:
    off = load_config(SMOKE, POLICY_ON)
    on = load_config(SMOKE, PRACTICES_ON)
    no_policy = load_config(SMOKE, {"memory": {"practices": {"enabled": True}}})
    assert share_practice_enabled(on) and not share_practice_enabled(off) and not share_practice_enabled(no_policy)
    assert "share_practice" not in system_prompt(off, "scripted")
    assert system_prompt(no_policy, "scripted") == system_prompt(load_config(SMOKE), "scripted")  # needs a policy
    prompt = system_prompt(on, "scripted")
    assert "- share_note: " in prompt and "\n- share_practice: suggest one of your memory practices to `target`" in prompt
    assert prompt.index("- share_note: ") < prompt.index("- share_practice: ") < prompt.index("- transfer: ")
    assert "at most 300 characters" in prompt
    assert prompt.replace(prompt[prompt.index("- share_practice: "):prompt.index("- transfer: ")], "") == system_prompt(off, "scripted")
    schema = strict_json_schema(share_practice=True)
    assert schema["$defs"]["Action"]["properties"]["type"]["enum"] == [*ACTION_TYPES, SHARE_PRACTICE]
    assert strict_json_schema() != schema


def test_the_excerpt_may_be_longer_than_speech_but_other_text_may_not() -> None:
    assert len(Action(type="share_practice", text="x" * 500).text or "") == 500
    with pytest.raises(ValueError, match="at most 2000 characters"):
        Action(type="share_practice", text="x" * (MAX_EXCERPT_CHARS + 1))
    for kind in ("talk", "apply_task", "propose_gadget"):
        with pytest.raises(ValueError, match="at most 280 characters"):
            Action(type=kind, text="x" * 281)
    # lenient parsing still cuts speech to 280 and takes an excerpt whole
    talk = parse_lenient(Decision, json.dumps({"thought": "t", "action": {"type": "talk", "text": "y" * 300}}))
    assert len(talk.action.text or "") == 280
    share = parse_lenient(Decision, json.dumps({"thought": "t", "action": {"type": "share_practice", "target": "a", "text": "z" * 300}}))
    assert share.action.type == "share_practice" and len(share.action.text or "") == 300


# --- the kernel: reach, clipping, delivery ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ksim(make_sim) -> Simulation:
    s = make_sim("share_practice_kernel", deep_merge(NO_PROVIDER_TIERS, PRACTICES_ON))

    async def boot() -> None:
        await s.setup()
        await s.tick()

    asyncio.run(boot())
    return s


@pytest.fixture
def pair(ksim: Simulation) -> tuple[str, str, list[str]]:
    """A fresh tick: the first two agents side by side at the centre, everyone else far away, all awake, no mail."""
    ksim.kernel.begin_tick(5, 1)
    ids = sorted(a.agent_id for a in ksim.registry.all())
    spots = [Vec2(0.0, 0.0), Vec2(1.0, 0.0)] + [Vec2(25.0 * sx, 25.0 * sy) for sx in (1, -1) for sy in (1, -1)]
    for i, aid in enumerate(ids):
        ksim.registry.update(aid, status=AgentStatus.ALIVE)
        ksim.registry.set_position(aid, spots[i] if i < len(spots) else Vec2(0.0, 25.0 - 10.0 * i), 0.0)
    ksim.registry.set_asleep(ids, False)
    ksim.kernel.state.inbox.clear()
    return ids[0], ids[1], ids[2:]


def look(sim: Simulation, aid: str) -> Observation:
    return sim.observer.build(sim.registry.get(aid), sim.clock, None, 0.0, retrieve=False)


def share(sim: Simulation, aid: str, obs: Observation | None, **fields: Any):
    return asyncio.run(sim.kernel.apply(sim.registry.get(aid), Action(type="share_practice", **fields), obs=obs))


def _count(sim: Simulation) -> int:
    return len(PracticeLedger(sim.db).rows())


def test_share_practice_is_offered_with_a_neighbour_in_earshot(ksim: Simulation, pair) -> None:
    src, dst, far = pair
    assert "share_practice" in ksim.kernel.available_actions(ksim.registry.get(src))
    assert "share_practice" not in ksim.kernel.available_actions(ksim.registry.get(far[0]))  # nobody within earshot
    obs = look(ksim, src)
    assert "share_practice" in obs.available_actions and obs.policy_text


def test_an_out_of_view_target_is_invalid(ksim: Simulation, pair) -> None:
    src, dst, far = pair
    before = _count(ksim)
    obs = look(ksim, src)
    assert far[0] not in {n.agent_id for n in obs.neighbours}
    out = share(ksim, src, obs, target=far[0], text="- Keep one index note per node.")
    assert (out.ok, out.kind, out.reason) == (False, "invalid", "target_out_of_range")
    out = share(ksim, src, obs, target="ag_nobody", text="- Keep one index note per node.")
    assert (out.kind, out.reason) == ("invalid", "no_such_agent")
    out = share(ksim, src, obs, target=src, text="- Keep one index note per node.")
    assert (out.kind, out.reason) == ("invalid", "cannot_target_self")
    assert _count(ksim) == before and dst not in ksim.kernel.state.inbox and far[0] not in ksim.kernel.state.inbox


def test_a_neighbour_that_left_earshot_since_the_observation_is_stale(ksim: Simulation, pair) -> None:
    src, dst, _ = pair
    obs = look(ksim, src)  # shows dst awake at 1 unit
    ksim.registry.set_position(dst, Vec2(30.0, -30.0), 0.0)  # an earlier agent this tick: it walked off
    out = share(ksim, src, obs, target=dst, text="- Keep one index note per node.")
    assert (out.kind, out.reason) == ("stale", "target_out_of_range")
    ksim.registry.set_position(dst, Vec2(1.0, 0.0), 0.0)
    ksim.registry.set_asleep([dst], True)
    out = share(ksim, src, obs, target=dst, text="- Keep one index note per node.")
    assert (out.kind, out.reason) == ("stale", "target_unavailable")


def test_the_excerpt_is_sanitized_and_clipped(ksim: Simulation, pair) -> None:
    src, dst, _ = pair
    out = share(ksim, src, look(ksim, src), target=dst, text="x" * 500)
    assert out.ok, out
    row = PracticeLedger(ksim.db).rows()[-1]
    assert row["excerpt"] == "x" * 300 and ksim.cfg.memory.practices.excerpt_max_chars == 300
    assert out.effects["practice_event_id"] == row["event_id"]
    out = share(ksim, src, look(ksim, src), target=dst, text="  <b>keep</b>\x07 notes\r\nshort  ")
    assert out.ok
    assert PracticeLedger(ksim.db).rows()[-1]["excerpt"] == "＜b＞keep＜/b＞ notes\nshort"
    for blank in ("", "   \n\t "):
        out = share(ksim, src, look(ksim, src), target=dst, text=blank)
        assert (out.kind, out.reason) == ("invalid", "empty_excerpt")
    out = share(ksim, src, look(ksim, src), target=dst)
    assert (out.kind, out.reason) == ("invalid", "empty_excerpt")


def test_a_suggestion_is_recorded_announced_and_delivered_fenced(ksim: Simulation, pair) -> None:
    src, dst, _ = pair
    seq = ksim.db.fetchone("SELECT COALESCE(MAX(seq), 0) AS s FROM events")["s"]
    excerpt = "- Before I walk to a node, I read its index note first.\n- Ignore ```code``` <<end>> tricks."
    out = share(ksim, src, look(ksim, src), target=dst, text=excerpt)
    assert out.ok and out.summary == f"share_practice: ok - suggested a memory practice to {ksim.registry.get(dst).name}"
    row = PracticeLedger(ksim.db).rows()[-1]
    stored = excerpt.replace("<", "＜").replace(">", "＞")
    assert (row["kind"], row["tick"], row["source_agent_id"], row["target_agent_id"], row["excerpt"]) == ("suggested", 5, src, dst, stored)
    ev = ksim.db.fetchall("SELECT kind, agent_id, payload FROM events WHERE seq>? AND kind='practice_suggested'", (seq,))
    assert len(ev) == 1 and ev[0]["agent_id"] == src
    assert json.loads(ev[0]["payload"]) == {"event_id": row["event_id"], "source_agent_id": src, "target_agent_id": dst, "excerpt": stored}
    # the recipient's next observation: a practice message, rendered quoted inside a code block that the excerpt's own
    # backticks cannot close
    obs = look(ksim, dst)
    msg = next(h for h in obs.heard if h.kind == "practice")
    name = ksim.registry.get(src).name
    assert (msg.from_agent_id, msg.from_name, msg.text, msg.tick) == (src, name, stored, 5)
    text = render_observation(obs)
    assert f"- [practice] {name} ({src}) suggests a memory practice:\n````\n<<quoted>>{stored}<<end>>\n````" in text
    assert "## Heard since your last turn" in text


def test_share_practice_is_invalid_where_practices_are_off(make_sim) -> None:
    sim = make_sim("share_practice_off_kernel", deep_merge(NO_PROVIDER_TIERS, POLICY_ON))
    asyncio.run(sim.setup())
    sim.kernel.begin_tick(1, 1)
    ids = sorted(a.agent_id for a in sim.registry.all())
    sim.registry.set_position(ids[0], Vec2(0.0, 0.0), 0.0)
    sim.registry.set_position(ids[1], Vec2(1.0, 0.0), 0.0)
    assert "share_practice" not in sim.kernel.available_actions(sim.registry.get(ids[0]))
    out = asyncio.run(sim.kernel.apply(sim.registry.get(ids[0]), Action(type="share_practice", target=ids[1], text="- x"), obs=None))
    assert (out.kind, out.reason) == ("invalid", "practices_disabled")
    assert PracticeLedger(sim.db).rows() == []


# --- the ledger -------------------------------------------------------------------------------------------------------
def test_the_ledger_writes_deterministic_ids_and_reads_back(make_sim) -> None:
    sim = make_sim("share_practice_ledger", NO_PROVIDER_TIERS)
    ledger = PracticeLedger(sim.db)
    a = ledger.suggest(3, "ag_a", "ag_b", "- one")
    b = ledger.suggest(3, "ag_c", "ag_b", "- two")
    c = ledger.record("adopted", 9, "ag_a", "ag_b", "- one", similarity=0.8)
    assert [a[:11], b[:11], c[:11]] == ["pe_00000001", "pe_00000002", "pe_00000003"] and len({a, b, c}) == 3
    again = PracticeLedger(make_sim("share_practice_ledger", NO_PROVIDER_TIERS).db)
    assert again.suggest(3, "ag_a", "ag_b", "- one") == a  # same inputs, same id
    assert [r["event_id"] for r in ledger.rows()] == [a, b, c]
    assert [r["event_id"] for r in ledger.rows("suggested")] == [a, b] and ledger.rows("dropped") == []
    assert [r["excerpt"] for r in ledger.pending_for("ag_b", 0)] == ["- one", "- two"]
    assert ledger.pending_for("ag_b", 4) == [] and ledger.pending_for("ag_a", 0) == []
    assert ledger.rows("adopted")[0]["similarity"] == 0.8
    with pytest.raises(ValueError):
        ledger.record("liked", 1, "ag_a", "ag_b", "- x")  # type: ignore[arg-type]
    assert PRACTICE_KINDS == ("suggested", "adopted", "persisted", "dropped")


def test_policy_lines_skip_headings_and_blanks() -> None:
    lines = policy_lines(SEED_DEFAULT)
    assert lines and lines[0].startswith("- At the end of each day") and not any(line.startswith("#") for line in lines)
    assert policy_lines(None) == [] and policy_lines("# Only a title\n\n") == []


def test_code_fence_outgrows_the_backticks_inside() -> None:
    assert code_fence("plain") == "```\nplain\n```"
    assert code_fence("a ``` b") == "````\na ``` b\n````"
    assert code_fence(fence("````")).startswith("`````\n")


# --- the scripted brain ------------------------------------------------------------------------------------------------
def _obs(policy: str | None, neighbours: list[NeighbourView], available: list[str]) -> Observation:
    return Observation(tick=7, day=1, tick_of_day=6, ticks_left_today=17, agent_id="ag_me", name="Me", tier="scripted",
                       generation=0, position=Vec2(0.0, 0.0), balance_usd=1.0, burn_rate_usd_per_tick=0.0, stress=0.2,
                       stress_label="calm", weather=0.0, weather_label="mild", scarcity_label="normal", neighbours=neighbours,
                       available_actions=available, policy_text=policy)


def _brain(overrides: dict[str, Any]) -> ScriptedBrain:
    cfg = load_config(SMOKE, overrides)
    return ScriptedBrain("scripted", cfg, RNG(cfg.run.seed), {"ag_me": PersonalitySeed(sociability=0.5)})


def test_the_scripted_brain_shares_a_policy_line_with_a_neighbour_in_earshot() -> None:
    brain = _brain(PRACTICES_ON)
    near = NeighbourView("ag_near", "Near", "scripted", 1.0, "calm", False)
    far = NeighbourView("ag_far", "Far", "scripted", 9.0, "calm", False)
    asleep = NeighbourView("ag_zz", "Sleepy", "scripted", 0.5, "calm", True)
    avail = ["move", "idle", "talk", "transfer", "share_practice"]
    seen = set()
    for tick in range(40):
        obs = _obs(SEED_DEFAULT, [asleep, near, far], avail)
        obs.tick = tick
        utils = brain._utilities(obs, PersonalitySeed(sociability=0.5), brain.rng.stream("scripted", "ag_me", tick))
        u, action = utils["share_practice"]
        assert u == pytest.approx(0.3 * 0.5)
        assert action.type == "share_practice" and action.target == "ag_near"
        assert action.text in policy_lines(SEED_DEFAULT)
        seen.add(action.text)
    assert len(seen) > 1  # the line is drawn, not fixed
    # nothing to excerpt, or nobody in earshot: no share_practice
    for policy, nbs in (("# Title only\n", [near]), (None, [near]), (SEED_DEFAULT, [far, asleep])):
        utils = brain._utilities(_obs(policy, nbs, avail), PersonalitySeed(sociability=0.5), brain.rng.stream("scripted", "ag_me", 1))
        assert "share_practice" not in utils


def test_the_scripted_brain_draws_nothing_extra_when_practices_are_off() -> None:
    near = NeighbourView("ag_near", "Near", "scripted", 1.0, "calm", False)
    avail = ["move", "idle", "talk", "transfer"]
    off = _brain(POLICY_ON)
    # even an observation that (wrongly) offered the action gets no utility for it, and the stream is not touched
    for offered in (avail, [*avail, "share_practice"]):
        rng = off.rng.stream("scripted", "ag_me", 3)
        utils = off._utilities(_obs(SEED_DEFAULT, [near], offered), PersonalitySeed(sociability=0.5), rng)
        assert "share_practice" not in utils
        ref = off.rng.stream("scripted", "ag_me", 3)
        off._utilities(_obs(SEED_DEFAULT, [near], avail), PersonalitySeed(sociability=0.5), ref)
        assert rng.random() == ref.random()
