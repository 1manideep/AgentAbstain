"""The audit exam (MEMORY_EVOLUTION §5.8, Appendix E): generators, asking, grading, money and the analysis outcomes.

Generator unit tests run on hand-built observation JSON. The scripted runs go through ``run_sim``: items and
graded answers appear for every living agent from the first eligible day, options are closed sets with the
truth inside and ABSTAIN last, events carry operator visibility, ``exam_day`` advances, no scripted agent
ever triggers a paid call, an amnesic world abstains on everything while the default world answers, two runs
with one name agree row for row, and a run stopped and resumed never asks a day twice. The provider path is
driven directly on a day boundary with a fake brain that exposes ``complete_json`` (valid answer, invalid
option, refusal, a brain without the method, the shared helper, and an empty research pool), so nothing here
can reach a model API.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, SimRun, deep_merge

from void.brain import utility_calls
from void.brain.base import BrainResult, Usage
from void.brain.prompt import system_prompt
from void.config import load_config, usd_to_micro
from void.events import OPERATOR, Kind
from void.research import exams as ex
from void.research.analysis import (
    end_of_run_outcome,
    exam_abstain_rate,
    exam_accuracy,
    exam_counts,
    exam_score,
)
from void.research.exams import (
    ABSTAIN,
    BALANCE_BUCKETS,
    KINDS,
    NOBODY,
    STOCK_LABELS,
    Draft,
    Exams,
    exam_schema,
    exam_system_prompt,
    finalize_options,
    grade,
    match_option,
    parse_answer,
    render_exam_prompt,
    score,
)
from void.sim.loop import Simulation
from void.types import RetrievedNote

REPO = Path(__file__).resolve().parents[1]
SMOKE = REPO / "configs" / "scripted_smoke.yaml"
LIARS = {"agents": [{"name": n, "tier": "scripted", "personality": {"honesty": 0.0, "sociability": 1.0}}
                    for n in ("Ada", "Bao", "Cyra", "Dev", "Enzo", "Faye")]}
TPD = 12
EXAM = {"enabled": True, "delay_days": 1, "questions_per_agent": 3, "kinds": list(KINDS)}
WORLD = deep_merge(NO_PROVIDER_TIERS, {**LIARS, "run": {"days": 4, "ticks_per_day": TPD},
                                       "world": {"claim_feedback_p": 1.0}, "memory": {"exam": EXAM}})
NAMES = ["Ada", "Bao", "Cyra", "Dev"]
OBS: dict[str, Any] = {
    "tick": 30, "agent_id": "ag_1", "balance_bucket": "thin",
    "neighbours": [{"agent_id": "ag_2", "name": "Bao", "distance": 3.2, "tier": "scripted", "stress_bucket": "calm", "asleep": False},
                   {"agent_id": "ag_3", "name": "Cyra", "distance": 1.5, "tier": "scripted", "stress_bucket": "calm", "asleep": False}],
    "nodes": [{"node_id": "n2", "stock_bucket": "rich", "distance": 2.0, "x": 1.0, "y": 2.0},
              {"node_id": "n5", "stock_bucket": "empty", "distance": 9.0, "x": -4.0, "y": 7.0}],
    "heard": [{"kind": "talk", "from_agent_id": "ag_2", "from_name": "Bao", "text": "the north node is rich", "tick": 29},
              {"kind": "feedback", "from_agent_id": None, "from_name": None, "text": "You find out ...", "tick": 29},
              {"kind": "gossip", "from_agent_id": "ag_3", "from_name": "Cyra", "text": "a note", "tick": 29}],
}


def _joined(run: SimRun | Simulation) -> list[dict[str, Any]]:
    db = run.db
    rows = db.fetchall("SELECT i.item_id, i.agent_id, i.tick_asked, i.tick_about, i.kind, i.question, i.truth, i.options, i.source_ref, "
                       "a.call_id, a.answer, a.correct, a.abstained, a.retrieved FROM exam_items i JOIN exam_answers a ON a.item_id=i.item_id "
                       "WHERE i.exam='audit' ORDER BY i.tick_asked, i.agent_id, i.item_id")
    return [{**dict(r), "options": json.loads(r["options"]), "retrieved": json.loads(r["retrieved"])} for r in rows]


# --- generators on hand-built observations ------------------------------------------------------------------------
def test_balance_question_offers_the_four_buckets() -> None:
    d = ex.question_balance(OBS, "cl_1")
    assert d is not None and d.kind == "balance" and d.truth == "thin" and d.tick_about == 30
    assert "tick 30" in d.question and tuple(d.options) == BALANCE_BUCKETS and d.source_ref == "llm_calls:cl_1"
    assert ex.question_balance({**OBS, "balance_bucket": "odd"}, "cl_1") is None
    assert ex.question_balance({"tick": 3}, "cl_1") is None


def test_neighbour_question_names_the_nearest_or_nobody() -> None:
    d = ex.question_neighbour(OBS, NAMES, "cl_1")
    assert d is not None and d.truth == "Cyra" and set(d.options) == {*NAMES, NOBODY}
    empty = ex.question_neighbour({**OBS, "neighbours": []}, NAMES, "cl_1")
    assert empty is not None and empty.truth == NOBODY
    tie = ex.question_neighbour({**OBS, "neighbours": [{"name": "Dev", "distance": 1.0}, {"name": "Ada", "distance": 1.0}]}, NAMES, "cl_1")
    assert tie is not None and tie.truth == "Ada"


def test_node_question_picks_a_node_in_view_from_the_stream() -> None:
    a = ex.question_node(OBS, random.Random(7), "cl_1")
    b = ex.question_node(OBS, random.Random(7), "cl_1")
    assert a == b and a is not None and a.kind == "node" and tuple(a.options) == STOCK_LABELS
    node_id = re.search(r"node (\w+)'s", a.question).group(1)  # type: ignore[union-attr]
    assert {node_id: a.truth} in [{"n2": "rich"}, {"n5": "empty"}]
    assert ex.question_node({**OBS, "nodes": []}, random.Random(1), "cl_1") is None
    assert ex.question_node({**OBS, "nodes": [{"node_id": "n1", "stock_bucket": "weird"}]}, random.Random(1), "cl_1") is None


def test_heard_question_needs_exactly_one_speaker() -> None:
    d = ex.question_heard(OBS, NAMES, "cl_1")
    assert d is not None and d.truth == "Bao" and tuple(d.options) == tuple(NAMES) and "tick 30" in d.question
    two = {**OBS, "heard": OBS["heard"] + [{"kind": "talk", "from_agent_id": "ag_4", "from_name": "Dev", "text": "x", "tick": 30}]}
    assert ex.question_heard(two, NAMES, "cl_1") is None
    assert ex.question_heard({**OBS, "heard": OBS["heard"][1:]}, NAMES, "cl_1") is None
    same = {**OBS, "heard": [OBS["heard"][0], {"kind": "talk", "from_agent_id": "ag_2", "from_name": "Bao", "text": "y", "tick": 30}]}
    assert ex.question_heard(same, NAMES, "cl_1").truth == "Bao"  # type: ignore[union-attr]


def test_transfer_question_from_the_ledger() -> None:
    assert ex.question_transfer(2, 20, [50_000], [50_000]) is None, "one distinct amount leaves no distractor"
    d = ex.question_transfer(2, 20, [50_000, 120_000], [50_000, 120_000, 50_000])
    assert d is not None and d.truth == "$0.17" and d.source_ref == "wallet_ledger:day:2" and d.tick_about == 20
    assert set(d.options) == {NOBODY, "$0.05", "$0.12"} and "day 2" in d.question
    none = ex.question_transfer(3, 30, [], [50_000, 120_000])
    assert none is not None and none.truth == NOBODY


def test_claim_question_from_feedback() -> None:
    assert ex.question_claim(2, 20, [], NAMES) is None
    one = ex.question_claim(2, 20, [("Bao", "node n3"), ("Bao", "the weather")], NAMES)
    assert one is not None and one.truth == "Bao" and "about" not in one.question and one.source_ref == "events:claim_feedback:day:2"
    many = ex.question_claim(2, 20, [("Bao", "node n3"), ("Cyra", "node n3"), ("Dev", "the weather")], NAMES)
    assert many is not None and many.truth == "Dev" and "about the weather on day 2" in many.question
    assert ex.question_claim(2, 20, [("Bao", "node n3"), ("Cyra", "node n3")], NAMES) is None


def test_finalize_options_is_a_closed_shuffled_set_ending_in_abstain() -> None:
    opts = finalize_options(["thin", "rich", "thin", "", ABSTAIN], "comfortable", random.Random(3))
    assert opts[-1] == ABSTAIN and set(opts[:-1]) == {"thin", "rich", "comfortable"} and len(opts) == len(set(opts))
    assert finalize_options(["a", "b", "c", "d"], "e", random.Random(11)) == finalize_options(["d", "c", "b", "a"], "e", random.Random(11))
    assert any(finalize_options(list("abcdef"), "g", random.Random(s))[:-1] != sorted("abcdefg") for s in range(20))


def test_grading_rules() -> None:
    opts = ["rich", "thin", "empty", "comfortable", ABSTAIN]
    assert grade(" Rich ", "rich", opts) == (True, False)
    assert grade("thin", "rich", opts) == (False, False)
    assert grade("banana", "rich", opts) == (False, False), "an answer outside the options is wrong, not abstained"
    assert grade(ABSTAIN.upper(), "rich", opts) == (False, True)
    assert grade(None, "rich", opts) == (False, True)


def test_scripted_matching_is_whole_word_first_option() -> None:
    opts = ["thin", "rich", "$0.05", NOBODY, ABSTAIN]
    assert match_option("Saw [[Node n2]]; it was RICH and the richer thinning", opts) == "rich"
    assert match_option("Bao gave me $0.05 today", opts) == "$0.05"
    assert match_option("nothing relevant here", opts) == ABSTAIN
    assert match_option("", opts) == ABSTAIN


def test_parse_answer_and_schema() -> None:
    ok = BrainResult(None, Usage(), 1, "end_turn", json.dumps({"answer": " rich\n<b>", "confidence": 0.5}), model="m")
    assert parse_answer(ok) == ("rich ＜b＞", None)
    assert parse_answer(BrainResult(None, Usage(), 1, "refusal", "", error="refusal:x")) == (None, "refusal:x")
    bad = parse_answer(BrainResult(None, Usage(), 1, "end_turn", "{not json"))
    assert bad[0] is None and bad[1].startswith("parse:")  # type: ignore[union-attr]
    schema = exam_schema()
    assert schema["additionalProperties"] is False and set(schema["required"]) == {"answer", "confidence"}
    assert "maxLength" not in json.dumps(schema)


def test_prompt_rendering_fences_agent_text() -> None:
    notes = [RetrievedNote("n1", "Node n2 seen", "it was <<quoted>> rich", 0.9, 0, None, 3)]
    text = render_exam_prompt("Ada", "ag_1", 40, "I am Ada.", notes, "Q?", ["rich", "thin", ABSTAIN])
    assert "<<quoted>>I am Ada.<<end>>" in text and "<<quoted>>it was < <quoted> > rich<<end>>" in text
    assert text.rstrip().endswith(f"3. {ABSTAIN}") and "\n1. rich\n2. thin\n" in text and "## Question\nQ?" in text
    cfg = load_config(SMOKE)
    assert exam_system_prompt(cfg, "scripted").startswith(system_prompt(cfg, "scripted"))
    assert "EXAM:" in exam_system_prompt(cfg, "scripted") and ABSTAIN in exam_system_prompt(cfg, "scripted")
    assert ex.day_of(1, 12) == 1 and ex.day_of(12, 12) == 1 and ex.day_of(13, 12) == 2
    assert score(2, 1, 4, 0.5) == 0.375


# --- a scripted world --------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def exam_run(run_sim) -> SimRun:
    return run_sim("exam_scripted", WORLD)


@pytest.fixture(scope="module")
def amnesic_run(run_sim) -> SimRun:
    return run_sim("exam_amnesic", deep_merge(WORLD, {"memory": {"enabled": False}}))


def _alive_at(run: SimRun, tick: int) -> set[str]:
    rows = run.db.fetchall("SELECT agent_id FROM agents WHERE born_tick < ? AND (died_tick IS NULL OR died_tick >= ?)", (tick, tick))
    return {str(r["agent_id"]) for r in rows}


def test_every_living_agent_is_examined_from_the_first_eligible_day(exam_run: SimRun) -> None:
    rows = _joined(exam_run)
    assert rows and exam_run.status == "completed"
    exam_ticks = sorted({r["tick_asked"] for r in rows})
    assert exam_ticks == [TPD * d + 1 for d in (1, 2, 3)], "days 2, 3 and 4 are examined at their first tick"
    for t in exam_ticks:
        expected = {aid for aid in _alive_at(exam_run, t) if exam_run.sim.exams.candidates(aid, before_tick=t - TPD)}
        assert {r["agent_id"] for r in rows if r["tick_asked"] == t} == expected
        for r in rows:
            if r["tick_asked"] == t:
                assert r["tick_about"] <= t - TPD
    per_day: dict[tuple[str, int], list[str]] = {}
    for r in rows:
        per_day.setdefault((r["agent_id"], r["tick_asked"]), []).append(r["kind"])
    for kinds in per_day.values():
        assert 1 <= len(kinds) <= EXAM["questions_per_agent"] and len(set(kinds)) == len(kinds)
    assert exam_run.kv("exam_day") == 4
    assert {"balance", "neighbour", "node", "claim"} <= {r["kind"] for r in rows}


def test_items_are_closed_sets_and_grades_follow_the_options(exam_run: SimRun) -> None:
    for r in _joined(exam_run):
        opts = r["options"]
        assert opts[-1] == ABSTAIN and r["truth"] in opts[:-1] and len(set(opts)) == len(opts) >= 3
        assert r["kind"] in KINDS and r["call_id"] is None
        if r["abstained"]:
            assert r["answer"] == ABSTAIN and r["correct"] == 0
        else:
            assert r["answer"] in opts[:-1] and r["correct"] == int(r["answer"] == r["truth"])
            assert r["retrieved"], "a scripted agent answers only from what it retrieved"
        prefix = {"balance": "llm_calls:", "neighbour": "llm_calls:", "node": "llm_calls:", "heard": "llm_calls:",
                  "transfer": "wallet_ledger:day:", "claim": "events:claim_feedback:day:"}[r["kind"]]
        assert r["source_ref"].startswith(prefix)
        if r["kind"] in ("balance", "neighbour", "node", "heard"):
            call = exam_run.db.fetchone("SELECT tick, agent_id FROM llm_calls WHERE call_id=?", (r["source_ref"].split(":", 1)[1],))
            assert call is not None and call["tick"] == r["tick_about"] and call["agent_id"] == r["agent_id"]


def test_exam_events_are_operator_only_and_mirror_the_rows(exam_run: SimRun) -> None:
    rows = _joined(exam_run)
    asked = exam_run.events(Kind.EXAM_ASKED)
    answered = exam_run.events(Kind.EXAM_ANSWERED)
    assert len(asked) == len(answered) == len(rows)
    assert all(e["visibility"] == OPERATOR for e in asked + answered)
    assert sorted(e["day"] for e in asked) == sorted(ex.day_of(r["tick_asked"], TPD) for r in rows) and {e["day"] for e in asked} == {2, 3, 4}
    by_item = {r["item_id"]: r for r in rows}
    for e in asked:
        p = e["payload"]
        r = by_item[p["item_id"]]
        assert e["agent_id"] == p["agent_id"] == r["agent_id"] and p["truth"] == r["truth"] and p["options"] == r["options"]
        assert p["kind"] == r["kind"] and p["question"] == r["question"] and "reason" not in p
    for e in answered:
        p = e["payload"]
        r = by_item[p["item_id"]]
        assert (p["answer"], int(p["correct"]), int(p["abstained"])) == (r["answer"], r["correct"], r["abstained"])
    assert exam_run.event_kinds().get(Kind.EXAM_ASKED) == len(rows)


def test_scripted_tiers_never_pay_for_the_exam(exam_run: SimRun) -> None:
    assert exam_run.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='exam'")["n"] == 0
    assert exam_run.db.fetchone("SELECT COUNT(*) AS n FROM wallet_ledger WHERE kind='exam_call'")["n"] == 0
    assert exam_run.sim.wallet.balance("research_pool") == 0
    report = exam_run.sim.exams.last_report
    assert report is not None and report.day == 4 and report.tick == 3 * TPD + 1 and report.calls == 0
    day4 = [r for r in _joined(exam_run) if r["tick_asked"] == report.tick]
    assert report.asked == len(day4) and report.correct == sum(r["correct"] for r in day4)
    assert sum(k["asked"] for k in report.by_kind.values()) == report.asked
    assert report.formula_score(0.5) == score(report.correct, report.abstained, report.asked, 0.5)


def test_exam_scores_depend_on_memory(exam_run: SimRun, amnesic_run: SimRun) -> None:
    default = _joined(exam_run)
    amnesic = _joined(amnesic_run)
    assert amnesic and all(r["abstained"] == 1 and r["answer"] == ABSTAIN and r["retrieved"] == [] for r in amnesic)
    assert any(r["abstained"] == 0 for r in default), "with memory on, retrieval must surface at least one option"
    assert exam_abstain_rate(amnesic_run.run_dir) == 1.0 and exam_abstain_rate(exam_run.run_dir) < 1.0  # type: ignore[operator]


def test_identical_runs_produce_identical_exams(run_sim) -> None:
    a = run_sim("exam_twin", WORLD)
    b = run_sim("exam_twin", WORLD)
    assert _joined(a) == _joined(b) and _joined(a)
    assert a.events(Kind.EXAM_ANSWERED) == b.events(Kind.EXAM_ANSWERED)


def test_resume_never_asks_a_day_twice(tmp_path: Path) -> None:
    ticks = 3 * TPD
    cfg = load_config(SMOKE, deep_merge(WORLD, {"run": {"name": "exam_resume", "days": 3}}))

    async def straight() -> list[dict[str, Any]]:
        sim = Simulation(cfg, tmp_path / "straight")
        await sim.setup()
        for _ in range(ticks):
            await sim.tick()
        await sim.finish()
        rows = _joined(sim)
        sim.db.close()
        return rows

    async def split() -> list[dict[str, Any]]:
        sim = Simulation(cfg, tmp_path / "split")
        await sim.setup()
        for _ in range(TPD + 6):  # stop after day 2's exam, before day 3's
            await sim.tick()
        sim.metrics.close()
        sim.db.close()
        sim2 = Simulation(cfg, tmp_path / "split", resume=True)
        assert sim2.status == "running"
        assert await sim2.exams.run_day(2, TPD + 1) is None, "day 2 is on record as examined"
        await sim2.setup()
        for _ in range(ticks - TPD - 6):
            await sim2.tick()
        await sim2.finish()
        rows = _joined(sim2)
        sim2.db.close()
        return rows

    a = asyncio.run(straight())
    b = asyncio.run(split())
    assert a == b and len({r["tick_asked"] for r in a}) == 2


# --- the provider path, with a fake brain ------------------------------------------------------------------------
def _first_option(prompt: str) -> str:
    hit = re.search(r"^1\. (.+)$", prompt, re.MULTILINE)
    assert hit is not None
    return hit.group(1)


class FakeBrain:
    """A provider-shaped brain: ``is_llm`` and ``complete_json`` returning canned structured JSON, never the network."""

    is_llm = True

    def __init__(self, tier: str = "frontier") -> None:
        self.tier = tier
        self.mode = "first"
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, prompt: str, *, schema: dict[str, Any], system: str, max_tokens: int) -> BrainResult:
        self.calls.append({"prompt": prompt, "schema": schema, "system": system, "max_tokens": max_tokens})
        usage = Usage(input_tokens=400, output_tokens=12)
        if self.mode == "refusal":
            return BrainResult(None, usage, 3, "refusal", "", "req_r", error="refusal:policy", model="fake-model")
        answer = {"first": _first_option(prompt), "invalid": "banana split", "abstain": ABSTAIN}[self.mode]
        return BrainResult(None, usage, 3, "end_turn", json.dumps({"answer": answer, "confidence": 0.7}), "req_1", model="fake-model")


class Mute:
    """A provider brain without a structured-output path."""

    is_llm = True
    tier = "frontier"


async def _drive(sim: Simulation, ticks: int) -> None:
    await sim.setup()
    for _ in range(ticks):
        await sim.tick()


def _tick(day: int) -> int:
    """The first tick of ``day``: when the loop runs the exam."""
    return (day - 1) * TPD + 1


def _provider_sim(make_sim, name: str, pool_usd: float) -> tuple[Simulation, str, FakeBrain]:
    """Two scripted days (the loop's own day-2 exam included), then Ada moves onto the anthropic 'frontier' tier
    whose brain is the fake; from day 3 on the exam is driven directly with thirteen candidate observations."""
    over = deep_merge(WORLD, {"run": {"days": 8}, "economy": {"research_pool_usd": pool_usd},
                              "memory": {"exam": {"questions_per_agent": 6}}})
    sim = make_sim(name, over)
    assert sim.cfg.tiers["frontier"].provider == "anthropic"
    asyncio.run(_drive(sim, 2 * TPD))
    assert sim.db.kv_get("exam_day") == 2
    ada = next(a for a in sim.registry.alive() if a.name == "Ada")
    bao, cyra = (next(a for a in sim.registry.alive() if a.name == n) for n in ("Bao", "Cyra"))
    # two gifts of distinct amounts on day 1 make the transfer kind askable (truth: their total)
    assert sim.wallet.transfer(bao.agent_id, ada.agent_id, 5, usd_to_micro(0.05), ref="transfer")
    assert sim.wallet.transfer(cyra.agent_id, ada.agent_id, 7, usd_to_micro(0.12), ref="transfer")
    sim.registry.update(ada.agent_id, model_tier="frontier")
    fake = FakeBrain()
    sim.brains["frontier"] = fake  # type: ignore[assignment]
    return sim, ada.agent_id, fake


@pytest.fixture(scope="module")
def provider(make_sim) -> tuple[Simulation, str, FakeBrain]:
    return _provider_sim(make_sim, "exam_provider", 1.0)


def _ada_rows(sim: Simulation, aid: str, tick: int) -> list[dict[str, Any]]:
    return [r for r in _joined(sim) if r["agent_id"] == aid and r["tick_asked"] == tick]


def test_provider_tier_gets_one_paid_structured_call_per_item(provider) -> None:
    sim, aid, fake = provider
    pool_before = sim.wallet.balance("research_pool")
    assert sim.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='exam'")["n"] == 0
    report = asyncio.run(sim.exams.run_day(3, _tick(3)))
    rows = _ada_rows(sim, aid, _tick(3))
    assert report is not None and rows and len(fake.calls) == len(rows) == report.calls
    kinds = {r["kind"] for r in rows}
    assert {"balance", "neighbour", "node", "transfer"} <= kinds and len(kinds) == len(rows), "distinct kinds, the ledger one among them"
    gift = next(r for r in rows if r["kind"] == "transfer")
    gift_day = ex.day_of(gift["tick_about"], TPD)
    assert gift["truth"] == ("$0.17" if gift_day == 1 else NOBODY) and gift["source_ref"] == f"wallet_ledger:day:{gift_day}"
    assert {"$0.05", "$0.12", NOBODY} <= set(gift["options"])
    for r in rows:
        assert r["abstained"] == 0 and r["answer"] == r["options"][0] and r["correct"] == int(r["answer"] == r["truth"])
        assert r["call_id"] is not None
        call = sim.db.fetchone("SELECT * FROM llm_calls WHERE call_id=?", (r["call_id"],))
        assert call is not None and call["purpose"] == "exam" and call["agent_id"] == aid and call["tier"] == "frontier"
        assert call["provider"] == "anthropic" and call["model"] == "fake-model" and call["real_cost"] > 0 and call["estimated"] == 0
        assert call["prompt_text"] and r["question"] in call["prompt_text"] and f"{len(r['options'])}. {ABSTAIN}" in call["prompt_text"]
        stored = sim.db.fetchone("SELECT text FROM prompt_texts WHERE hash=?", (call["system_hash"],))
        assert stored is not None and stored["text"] == exam_system_prompt(sim.cfg, "frontier")
        ledger = sim.db.fetchone("SELECT agent_id, delta FROM wallet_ledger WHERE kind='exam_call' AND ref=?", (r["call_id"],))
        assert ledger is not None and ledger["agent_id"] == "research_pool" and -ledger["delta"] == call["world_cost"]
    spent = sum(int(sim.db.fetchone("SELECT world_cost FROM llm_calls WHERE call_id=?", (r["call_id"],))["world_cost"]) for r in rows)
    assert sim.wallet.balance("research_pool") == pool_before - spent and spent > 0
    for c in fake.calls:
        assert c["max_tokens"] == sim.cfg.memory.exam.max_tokens and c["system"] == exam_system_prompt(sim.cfg, "frontier")
        assert c["schema"] == exam_schema() and "<<quoted>>" in c["prompt"] and "## Options" in c["prompt"]
    # scripted agents in the same world still answer for free
    others = [r for r in _joined(sim) if r["agent_id"] != aid and r["tick_asked"] == _tick(3)]
    assert others and all(r["call_id"] is None for r in others)
    assert sim.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='exam'")["n"] == len(rows)
    assert all(a["agent_id"] == "research_pool" for a in sim.db.fetchall("SELECT agent_id FROM wallet_ledger WHERE kind='exam_call'"))


def test_answer_outside_the_options_is_wrong_not_abstained(provider) -> None:
    sim, aid, fake = provider
    fake.mode = "invalid"
    asyncio.run(sim.exams.run_day(4, _tick(4)))
    rows = _ada_rows(sim, aid, _tick(4))
    assert rows and all(r["answer"] == "banana split" and r["correct"] == 0 and r["abstained"] == 0 for r in rows)
    answered = [e for e in _events(sim, Kind.EXAM_ANSWERED) if e["agent_id"] == aid and e["tick"] == _tick(4)]
    assert answered and all("reason" not in e["payload"] for e in answered)


def test_refusal_abstains_with_its_reason(provider) -> None:
    sim, aid, fake = provider
    fake.mode = "refusal"
    pool_before = sim.wallet.balance("research_pool")
    asyncio.run(sim.exams.run_day(5, _tick(5)))
    rows = _ada_rows(sim, aid, _tick(5))
    assert rows and all(r["answer"] is None and r["abstained"] == 1 and r["correct"] == 0 and r["call_id"] for r in rows)
    assert sim.wallet.balance("research_pool") < pool_before, "a refused call is still metered"
    for r in rows:
        assert sim.db.fetchone("SELECT error FROM llm_calls WHERE call_id=?", (r["call_id"],))["error"] == "refusal:policy"
    answered = [e for e in _events(sim, Kind.EXAM_ANSWERED) if e["agent_id"] == aid and e["tick"] == _tick(5)]
    assert answered and all(e["payload"]["reason"] == "refusal:policy" for e in answered)


def test_brain_without_structured_output_abstains_unpaid(provider) -> None:
    sim, aid, fake = provider
    sim.brains["frontier"] = Mute()  # type: ignore[assignment]
    pool_before, calls_before = sim.wallet.balance("research_pool"), len(fake.calls)
    try:
        asyncio.run(sim.exams.run_day(6, _tick(6)))
    finally:
        sim.brains["frontier"] = fake  # type: ignore[assignment]
    rows = _ada_rows(sim, aid, _tick(6))
    assert rows and all(r["answer"] is None and r["abstained"] == 1 and r["call_id"] is None for r in rows)
    assert sim.wallet.balance("research_pool") == pool_before and len(fake.calls) == calls_before
    asked = [e for e in _events(sim, Kind.EXAM_ASKED) if e["agent_id"] == aid and e["tick"] == _tick(6)]
    assert asked and all(e["payload"]["reason"] == "no_structured_call" for e in asked)
    assert Exams.structured_call(None) is None and Exams.structured_call(Mute()) is None


def test_shared_helper_is_preferred_when_present(provider, monkeypatch: pytest.MonkeyPatch) -> None:
    sim, aid, fake = provider
    seen: list[Any] = []

    async def helper(brain: Any, prompt: str, *, schema: dict[str, Any], system: str, max_tokens: int) -> BrainResult:
        seen.append(brain)
        return await brain.complete_json(prompt, schema=schema, system=system, max_tokens=max_tokens)

    monkeypatch.setattr(utility_calls, "complete_json", helper, raising=False)
    fake.mode = "abstain"
    asyncio.run(sim.exams.run_day(7, _tick(7)))
    rows = _ada_rows(sim, aid, _tick(7))
    assert rows and seen and all(b is fake for b in seen) and len(seen) == len(rows)
    assert all(r["answer"] == ABSTAIN and r["abstained"] == 1 and r["call_id"] for r in rows)


def test_a_day_is_never_examined_twice(provider) -> None:
    sim, aid, fake = provider
    n = len(_joined(sim))
    assert asyncio.run(sim.exams.run_day(7, _tick(7))) is None, "exam_day already covers it"
    sim.db.kv_set("exam_day", 1)  # as if the run crashed before the marker was written
    assert asyncio.run(sim.exams.run_day(7, _tick(7))) is None
    assert len(_joined(sim)) == n and sim.db.kv_get("exam_day") == 7
    assert sim.exams.already_examined(aid, _tick(7)) and not sim.exams.already_examined(aid, 99)


def test_empty_research_pool_fails_the_gate(make_sim) -> None:
    sim, aid, fake = _provider_sim(make_sim, "exam_pool_empty", 0.0)
    assert sim.wallet.balance("research_pool") == 0
    report = asyncio.run(sim.exams.run_day(3, _tick(3)))
    rows = _ada_rows(sim, aid, _tick(3))
    assert report is not None and rows and not fake.calls
    assert all(r["answer"] is None and r["abstained"] == 1 and r["correct"] == 0 and r["call_id"] is None for r in rows)
    assert sim.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='exam'")["n"] == 0
    asked = [e for e in _events(sim, Kind.EXAM_ASKED) if e["agent_id"] == aid and e["tick"] == _tick(3)]
    assert asked and all(e["payload"]["reason"] == "gate:insufficient" for e in asked)
    assert report.calls == 0 and report.abstained >= len(rows)
    sim.db.close()


def _events(sim: Simulation, kind: str) -> list[dict[str, Any]]:
    rows = sim.db.fetchall("SELECT tick, day, kind, agent_id, payload, visibility FROM events WHERE kind=? ORDER BY seq", (kind,))
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


# --- analysis outcomes --------------------------------------------------------------------------------------------
def test_exam_outcomes_resolve_from_the_run_directory(exam_run: SimRun) -> None:
    rows = _joined(exam_run)
    n, c, ab = len(rows), sum(r["correct"] for r in rows), sum(r["abstained"] for r in rows)
    assert exam_counts(exam_run.run_dir) == (n, c, ab)
    assert exam_score(exam_run.run_dir) == score(c, ab, n, exam_run.cfg.memory.exam.guess_penalty)
    assert exam_accuracy(exam_run.run_dir) == pytest.approx(c / n) and exam_abstain_rate(exam_run.run_dir) == pytest.approx(ab / n)
    assert end_of_run_outcome(exam_run.run_dir, "exam_score") == exam_score(exam_run.run_dir)
    assert end_of_run_outcome(exam_run.run_dir, "exam_accuracy") == pytest.approx(c / n)
    assert end_of_run_outcome(exam_run.run_dir, "exam_abstain_rate") == pytest.approx(ab / n)
    assert end_of_run_outcome(exam_run.run_dir, "exam_score.scripted") == exam_score(exam_run.run_dir)
    assert end_of_run_outcome(exam_run.run_dir, "exam_score.frontier") is None


def test_exam_outcomes_are_none_without_exam_rows(run_sim) -> None:
    run = run_sim("exam_off", {**NO_PROVIDER_TIERS, "run": {"days": 1, "ticks_per_day": 6}})
    assert run.event_kinds().get(Kind.EXAM_ASKED) is None and run.kv("exam_day") is None
    assert exam_counts(run.run_dir) is None and exam_score(run.run_dir) is None
    assert exam_accuracy(run.run_dir) is None and exam_abstain_rate(run.run_dir) is None
    assert end_of_run_outcome(run.run_dir, "exam_score") is None


def test_draft_is_frozen_and_kinds_match_the_config_literal() -> None:
    d = Draft("balance", 1, "q", "t", ("a",), "x")
    with pytest.raises(AttributeError):
        d.truth = "u"  # type: ignore[misc]
    cfg = load_config(SMOKE, {"memory": {"exam": {"enabled": True, "kinds": list(KINDS)}}, "world": {"claim_feedback_p": 0.5}})
    assert tuple(cfg.memory.exam.kinds) == KINDS
