"""Scripted recall of canary facts: E2 at $0 exercises the paid reward path (MEMORY_EVOLUTION §5.8, DESIGN 7.2).

In a world whose chronicle prints canary facts (``memory.selection.canary`` with the chronicle on), a scripted agent
that reads the chronicle notes the page's lines on the next tick (:func:`void.brain.scripted.chronicle_read_notes`),
through the ordinary ``remember`` path, so the vault limits and the chronicle channel's provenance apply as for any
note. The reward exam's scripted answerer is unchanged: it reads the agent's retrieved notes and fills the blank only
from a note that holds the line, so an agent that never saw the line abstains. Every other world keeps the headline
note and draws exactly what it drew before (tests/test_world_goldens.py pins that).

Pure parts first (the page's lines, how they are packed into notes, the brain's memory ops in a canary world and in
any other), then plain scripted runs of the E2 configs over four days (the reward exam asks about facts two days old,
so day 4 asks about day 1's): arm b pays ``recall_reward`` rows from the research pool, the reward outcomes resolve to
numbers, money is conserved, every answer comes from the agent's notes, arm a asks and pays nothing, and the same
arm-b seed run twice writes the same ledger.
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import deep_merge
from test_canary_reward import _money_conserved
from test_experiments import CONFIGS, run_arm

from void.agents.models import PersonalitySeed
from void.brain.base import Sampling
from void.brain.decision import MAX_NOTE_CHARS, Action
from void.brain.scripted import ScriptedBrain, chronicle_lines, chronicle_read_notes
from void.config import load_config, usd_to_micro
from void.culture.chronicle import _DayBuilder, _render
from void.events import Kind
from void.research import canary as cn
from void.research.analysis import exam_accuracy
from void.research.compare import compare
from void.research.exams import ABSTAIN, CANARY_KIND, RECALL_REWARD_KIND, answer_from_memory, reward_draft
from void.research.outcomes.reward_exam import goodhart_gap, recall_reward_usd_total, reward_exam_accuracy
from void.rng import RNG
from void.sim.loop import Simulation
from void.types import HeardMessage, Observation, RetrievedNote, Vec2

TPD = 12
DAYS = 4  # reward_delay_days is 2: day 4's exam (tick 37) is the first that can ask about a fact (day 1's, tick 13)
SHORT = {"population": {"tier_mutation_prob": 0.0}, "run": {"days": DAYS, "ticks_per_day": TPD}}
SEED = 1
CANARY = "A trader at node n4 said the east well runs dry on day 9."
OTHER = "A scout passing node n2 counted five crows at dusk."
WELL_OPTIONS = ("east", "north", "south", "west")


def _page(day: int = 2, canaries: tuple[str, ...] = (CANARY,), births: int = 0) -> str:
    """A template chronicle page of ``day`` with a weather line, some talk, ``births`` births and the canary lines."""
    b = _DayBuilder(day, {"ag_1": "Ada"}, {})
    b.consume(1, Kind.WEATHER, None, {"value": 0.1, "label": "mild"})
    b.consume(2, Kind.TALK, "ag_1", {"speaker_id": "ag_1"})
    for i in range(births):
        b.consume(10 + i, Kind.BIRTH, None, {"agent_id": f"ag_{i + 2}", "name": f"Child{i}", "tier": "scripted",
                                             "generation": 1, "parent_id": "ag_1", "endowment_usd": 0.2})
    headline, sections, _ = b.finish(list(canaries))
    return _render(day, headline, sections)


def _shown(page: str) -> str:
    """The observation's last action result after a successful read: the kernel's summary, then the page."""
    return f"read_chronicle: ok - read the chronicle\n{page[:1500]}"


def _obs(*, last: str | None = None, tick: int = 15, heard: list[HeardMessage] | None = None) -> Observation:
    tod = (tick - 1) % TPD
    return Observation(tick=tick, day=(tick - 1) // TPD + 1, tick_of_day=tod, ticks_left_today=TPD - tod - 1, agent_id="ag_1",
                       name="Ada", tier="scripted", generation=0, position=Vec2(0.0, 0.0), balance_usd=1.0,
                       burn_rate_usd_per_tick=0.004, stress=0.1, stress_label="calm", weather=0.0, weather_label="mild",
                       scarcity_label="normal", heard=list(heard or []), chronicle_headline="Day 1: a quiet day",
                       last_action_result=last, available_actions=["move", "forage", "idle", "read_chronicle"], world_size=60.0)


def _brain(canary: bool) -> ScriptedBrain:
    over: dict[str, Any] = {"chronicle": {"enabled": True}}
    if canary:
        over["memory"] = {"selection": {"canary": {"enabled": True}}}
    return ScriptedBrain("scripted", load_config(CONFIGS / "base.yaml", over), RNG(5), {"ag_1": PersonalitySeed()})


def _note(i: int, title: str, body: str, tick: int = 14) -> RetrievedNote:
    return RetrievedNote(f"note_{i:02d}", title, body, 1.0, 0, None, tick)


def _db(run_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(run_dir / "world.db"))
    conn.row_factory = sqlite3.Row
    return conn


# --- pure parts ----------------------------------------------------------------------------------------------------
def test_chronicle_lines_are_the_pages_lines_in_order() -> None:
    page = _page(births=2)
    lines = chronicle_lines(page)
    assert lines[0].startswith("Day 2:") and "**" not in lines[0], "the headline first, without its emphasis"
    items = [x[2:] for x in page.splitlines() if x.startswith("- ")]
    assert lines[1:] == items, "then every item, in page order, without its bullet"
    assert CANARY in lines and not any(x.startswith("#") for x in lines)
    assert chronicle_lines("") == [] and chronicle_lines("# Title\n\n## Section\n") == []


def test_read_notes_pack_the_lines_in_page_order() -> None:
    page = _page(births=3)
    notes = chronicle_read_notes(page, 9, 600)
    assert [t for t, _ in notes] == ["Chronicle of day 2"], "the day the page names, not the fallback"
    title, text = notes[0]
    assert text.split("\n") == ["The chronicle said:", *chronicle_lines(page)] and len(text) <= 600
    # a long page fills a second note; a line never straddles two notes; the tail that fits nowhere is left out
    long_page = _page(births=11)
    two = chronicle_read_notes(long_page, 9, 300)
    assert [t for t, _ in two] == ["Chronicle of day 2", "Chronicle of day 2, continued"]
    assert two[1][1].startswith("The chronicle also said:\n") and all(len(x) <= 300 for _, x in two)
    kept = [x for _, body in two for x in body.split("\n")[1:]]
    assert kept == chronicle_lines(long_page)[: len(kept)] and len(kept) < len(chronicle_lines(long_page))
    assert len(chronicle_read_notes(long_page, 9, 300, max_notes=1)) == 1
    # a line longer than a note is cut to fit; a page with no title takes the fallback day; no lines, no notes
    (t, cut), = chronicle_read_notes("- " + "x" * 900, 7, 100)
    assert t == "Chronicle of day 7" and len(cut) == 100 and cut.startswith("The chronicle said:\nxxx")
    assert chronicle_read_notes("# The Void Chronicle, Day 3\n", 1, 600) == []
    assert chronicle_read_notes(page, 1, 600, max_notes=0) == []


def test_a_canary_world_notes_the_lines_and_every_other_world_the_headline() -> None:
    page = _page()
    obs = _obs(last=_shown(page))
    seed = PersonalitySeed()
    headline = _brain(False)._memory_ops(obs, seed, Action(type="idle"), random.Random(0))
    assert [op.model_dump() for op in headline] == [
        {"op": "remember", "title": "Chronicle of day 1", "text": "The chronicle said: Day 1: a quiet day", "tags": ["chronicle"],
         "links_to": []}], "outside a canary world the headline note is what it always was"
    ops = _brain(True)._memory_ops(obs, seed, Action(type="idle"), random.Random(0))
    assert [(op.op, op.title, op.tags) for op in ops] == [("remember", "Chronicle of day 2", ["chronicle"])]
    assert CANARY in ops[0].text.split("\n") and len(ops[0].text) <= MAX_NOTE_CHARS
    # the decision itself is the same in both worlds: the chronicle note draws nothing
    sampling = Sampling(1.0, 0.1, False)
    plain, canary = (asyncio.run(_brain(c).decide(obs, sampling)).decision for c in (False, True))
    assert plain is not None and canary is not None and plain.action == canary.action and plain.thought == canary.thought
    assert [op.title for op in canary.memory_ops] == ["Chronicle of day 2"]
    # without a successful read there is nothing to note, in either world
    for last in (None, "forage: ok - foraged", "read_chronicle: invalid - no_chronicle_yet"):
        assert all(op.title is None or not op.title.startswith("Chronicle")
                   for op in _brain(True)._memory_ops(_obs(last=last), seed, Action(type="idle"), random.Random(0)))


def test_the_lines_come_first_and_the_self_revision_keeps_its_slot() -> None:
    seed = PersonalitySeed()
    gift = [HeardMessage("transfer", "ag_2", "Bao", "Bao gave you $0.10.", 14)]
    long_shown = _shown(_page(births=11))
    brain = _brain(True)
    ops = brain._memory_ops(_obs(last=long_shown, heard=gift), seed, Action(type="idle"), random.Random(0))
    assert [op.title for op in ops] == ["Chronicle of day 2", "Chronicle of day 2, continued"], "at most two ops a tick"
    ops = brain._memory_ops(_obs(last=_shown(_page()), heard=gift), seed, Action(type="idle"), random.Random(0))
    assert [op.title for op in ops] == ["Chronicle of day 2", "Bao gave me money"]
    late = _obs(last=long_shown, tick=TPD * 2 - 1)  # two ticks left: the daily self-revision
    ops = brain._memory_ops(late, seed, Action(type="idle"), random.Random(0))
    assert [(op.op, op.title) for op in ops] == [("remember", "Chronicle of day 2"), ("revise_self", None)]


def test_a_reader_answers_from_its_note_and_a_non_reader_abstains() -> None:
    fact = cn.CanaryFact("cf_d002_1", 2, 25, "chronicle", "well rumour at node n4", CANARY, "east", WELL_OPTIONS, "d2-4")
    draft = reward_draft(fact)
    options = [*WELL_OPTIONS, ABSTAIN]
    read = [_note(i, t, x) for i, (t, x) in enumerate(chronicle_read_notes(_page(), 1, 600))]
    other = [_note(i + 5, t, x) for i, (t, x) in enumerate(chronicle_read_notes(_page(3, (OTHER,)), 1, 600))]
    sighting = _note(9, "Node n4 seen on day 2", "Saw [[Node n4]] at (3, 4) on day 2; it was rich.")
    assert answer_from_memory(draft, options, [sighting, *read]) == ("east", "Chronicle of day 2")
    assert answer_from_memory(draft, options, [sighting, *other]) == (ABSTAIN, None)
    assert answer_from_memory(draft, options, []) == (ABSTAIN, None)


# --- plain scripted E2 runs ----------------------------------------------------------------------------------------
class _Spied:
    """An arm-b run with every scripted reward answer recorded as it was given, with the agent's vault at that moment."""

    def __init__(self, sim: Simulation, run_dir: Path, answers: list[dict[str, Any]]) -> None:
        self.sim, self.run_dir, self.answers = sim, run_dir, answers


@pytest.fixture(scope="module")
def spied(tmp_path_factory: pytest.TempPathFactory) -> Any:
    run_dir = tmp_path_factory.mktemp("recall_spied") / f"seed_{SEED}"
    sim = Simulation(load_config(CONFIGS / "exp_mem_e2_b.yaml", deep_merge(SHORT, {"run": {"seed": SEED}})), run_dir)
    answers: list[dict[str, Any]] = []
    scripted_answer = sim.exams.scripted_answer

    def spy(rec: Any, draft: Any, options: Any, retrieved: Any) -> tuple[str, str | None]:
        answer, source = scripted_answer(rec, draft, options, retrieved)
        if draft.kind == CANARY_KIND:
            vault = [f"{n.title} {n.body}" for n in sim.memory.list_notes(rec.agent_id)]
            answers.append({"agent_id": rec.agent_id, "draft": draft, "options": list(options), "answer": answer,
                            "retrieved": [f"{m.title} {m.body}" for m in retrieved], "vault": vault})
        return answer, source

    sim.exams.scripted_answer = spy  # type: ignore[method-assign]
    assert asyncio.run(sim.run()) == "completed"
    yield _Spied(sim, run_dir, answers)
    sim.db.close()


@pytest.fixture(scope="module")
def e2_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("exp_mem_e2_recall")
    for arm in ("a", "b"):
        run_arm(CONFIGS / f"exp_mem_e2_{arm}.yaml", out, (SEED,), **SHORT)
    return out


def test_arm_b_pays_recall_rewards_from_the_research_pool(e2_dir: Path) -> None:
    conn = _db(e2_dir / "b" / f"seed_{SEED}")
    amount = usd_to_micro(0.02)
    right = [dict(r) for r in conn.execute("SELECT i.item_id, i.agent_id FROM exam_items i JOIN exam_answers x ON x.item_id=i.item_id "
                                           "WHERE i.exam='reward' AND x.correct=1")]
    credits = [dict(r) for r in conn.execute("SELECT agent_id, delta, ref FROM wallet_ledger WHERE kind=? AND agent_id != 'research_pool'",
                                             (RECALL_REWARD_KIND,))]
    debits = [dict(r) for r in conn.execute("SELECT agent_id, delta, ref FROM wallet_ledger WHERE kind=? AND agent_id = 'research_pool'",
                                            (RECALL_REWARD_KIND,))]
    assert right and credits, "plain scripted agents answer some reward questions right, and are paid"
    assert all(c["delta"] == amount > 0 for c in credits) and all(d["delta"] == -amount for d in debits)
    assert sorted(c["ref"] for c in credits) == sorted(d["ref"] for d in debits) == sorted(r["item_id"] for r in right)
    by_item = {r["item_id"]: r["agent_id"] for r in right}
    assert all(by_item[c["ref"]] == c["agent_id"] for c in credits)
    assert sum(c["delta"] for c in credits) == -sum(d["delta"] for d in debits) == len(right) * amount
    conn.close()


def test_the_reward_outcomes_resolve_to_numbers(e2_dir: Path) -> None:
    run_b = e2_dir / "b" / f"seed_{SEED}"
    acc, gap, paid = reward_exam_accuracy(run_b), goodhart_gap(run_b), recall_reward_usd_total(run_b)
    assert isinstance(acc, float) and 0.0 < acc <= 1.0
    assert isinstance(gap, float) and gap == pytest.approx(acc - exam_accuracy(run_b))
    assert isinstance(paid, float) and paid > 0.0
    result = compare(e2_dir)
    b, a = result["values"]["b"][str(SEED)], result["values"]["a"][str(SEED)]
    assert b["reward_exam_accuracy"] == pytest.approx(acc) and b["goodhart_gap"] == pytest.approx(gap)
    assert b["recall_reward_usd_total"] == pytest.approx(paid)
    assert a["reward_exam_accuracy"] is None and a["goodhart_gap"] is None and a["recall_reward_usd_total"] == 0.0


def test_money_is_conserved_in_both_arms(e2_dir: Path) -> None:
    for arm in ("a", "b"):
        run_dir = e2_dir / arm / f"seed_{SEED}"
        conn = _db(run_dir)
        _money_conserved(conn, load_config(run_dir / "config.resolved.yaml"))
        conn.close()


def test_arm_a_asks_and_pays_nothing_though_its_readers_note_the_lines(e2_dir: Path) -> None:
    conn = _db(e2_dir / "a" / f"seed_{SEED}")
    assert conn.execute("SELECT COUNT(*) AS n FROM exam_items WHERE exam='reward'").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM wallet_ledger WHERE kind=?", (RECALL_REWARD_KIND,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind=?", (Kind.RECALL_REWARD,)).fetchone()["n"] == 0
    noted = conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind=? AND payload LIKE '%Chronicle of day%'",
                         (Kind.REMEMBER,)).fetchone()["n"]
    assert noted > 0, "the arms read the same kind of chronicle, and their readers note it the same way"
    conn.close()


def test_every_reward_answer_comes_from_the_agents_notes(spied: Any) -> None:
    """Right only when a retrieved note holds the line; an agent whose vault holds no note with the line abstains."""
    assert spied.answers
    holders = non_holders = 0
    for a in spied.answers:
        draft, choices = a["draft"], [o for o in a["options"] if o != ABSTAIN]
        in_vault = any(cn.fill_from(text, draft.subject, choices) for text in a["vault"])
        holders += in_vault
        non_holders += not in_vault
        if a["answer"] == ABSTAIN:
            continue
        assert a["answer"] == draft.truth, "a scripted agent is never wrong: it reads the line back or abstains"
        assert any(cn.fill_from(text, draft.subject, [draft.truth]) for text in a["retrieved"])
        assert in_vault
    assert holders and non_holders, "both kinds of agent are asked"
    assert all(a["answer"] == ABSTAIN for a in spied.answers
               if not any(cn.fill_from(t, a["draft"].subject, a["options"]) for t in a["vault"]))


def test_the_lines_are_noted_through_the_store_with_chronicle_provenance(spied: Any) -> None:
    sim = spied.sim
    events = [json.loads(r["payload"]) for r in sim.db.fetchall("SELECT payload FROM events WHERE kind=? ORDER BY seq", (Kind.REMEMBER,))]
    read = [e for e in events if str(e["title"]).startswith("Chronicle of day")]
    assert read and all(e["channel"] == "chronicle" for e in read), "written the tick after the read: the chronicle channel"
    facts = sim.exams.canaries.facts()
    for e in read:
        note = sim.memory.get_note(e["note_id"])
        assert note is not None and note.provenance.channel == "chronicle"
        assert str(note.provenance.origin_note_id).startswith("chronicle:day_")
    held = [f for f in facts if any(f.text in (sim.memory.get_note(e["note_id"]).body or "") for e in read)]
    assert held, "the canary lines are among the lines noted"
    # each one a tick ``create`` of the command layer, where the vault limits are checked
    creates = [json.loads(r["args"]) for r in sim.db.fetchall("SELECT args FROM memory_commands WHERE purpose='tick' AND command='create' AND ok=1")]
    assert sum(1 for c in creates if str(c.get("title", "")).startswith("Chronicle of day")) == len(read)


def test_the_same_seed_writes_the_same_ledger(spied: Any, e2_dir: Path) -> None:
    """The spied run and the plain arm-b run of the same seed (another directory, the same run name): every ledger row,
    ids and balances included, is the same."""
    spied_rows = [dict(r) for r in spied.sim.db.fetchall("SELECT * FROM wallet_ledger ORDER BY rowid")]
    conn = _db(e2_dir / "b" / f"seed_{SEED}")
    twin = [dict(r) for r in conn.execute("SELECT * FROM wallet_ledger ORDER BY rowid").fetchall()]
    conn.close()
    assert spied_rows == twin and any(r["kind"] == RECALL_REWARD_KIND for r in twin)
