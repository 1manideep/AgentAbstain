"""The audit exam must measure memory (MEMORY_EVOLUTION §5.8, §6.2: "Failing E0 means a bug in the measurement").

In the scripted E0 world the exam used to be degenerate: every node an agent saw was ``rich``, every balance
``comfortable``, and the scripted answerer took the first option word anywhere in its notes, so node notes
answered balance questions (the two vocabularies shared ``rich``, ``thin`` and ``empty``). These checks run the
E0 world itself (``configs/exp_mem_e0_base.yaml`` plus the index-builder arm, four days, written to a temporary
config) and pin the repairs:

* truths vary: at least two of the kinds the E0 world asks have more than one distinct truth, and a kind whose
  candidate observations admit a single truth is flagged and counted rather than passed off as a measurement;
* a memoryless stub that always gives each kind's first option scores no better than the majority baseline;
* the scripted matcher reads only the notes about the question's subject;
* with prompts logged, an exam call's response text is stored whole, however long;
* two runs with one name produce the same exam, row for row.

Scripted brains only; the provider path is driven with a fake brain, so nothing here can reach a model API.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from conftest import NO_PROVIDER_TIERS, deep_merge

from void.brain.base import BrainResult, Usage
from void.config import load_config
from void.events import Kind
from void.research import exams as ex
from void.research.exams import (
    ABSTAIN,
    BALANCE_BUCKETS,
    BALANCE_LABELS,
    NOBODY,
    RAW_TEXT_CHARS,
    SELF_SOURCE,
    STOCK_LABELS,
    ExamReport,
    Exams,
    answer_from_memory,
    match_option,
)
from void.sim.loop import Simulation
from void.types import RetrievedNote

REPO = Path(__file__).resolve().parents[1]
E0_BASE = REPO / "configs" / "exp_mem_e0_base.yaml"
ARM = "index_builder"
DAYS = 4
TPD = 12  # the E0 world's ticks per day


def _e0_config(directory: Path, name: str) -> Path:
    """The E0 world plus one arm's strategy, four days, as a config file that extends the shared base."""
    data = {
        "extends": str(E0_BASE),
        "run": {"name": name, "days": DAYS},
        "population": {"tier_mutation_prob": 0.0},  # children stay scripted: nothing can reach a provider
        "tiers": {"scripted": {"maintenance_strategy": ARM}},
        "experiment": {"arm": ARM},
    }
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _run(config: Path, run_dir: Path) -> Simulation:
    sim = Simulation(load_config(config), run_dir)
    assert asyncio.run(sim.run()) == "completed"
    return sim


def _joined(sim: Simulation) -> list[dict[str, Any]]:
    rows = sim.db.fetchall(
        "SELECT i.item_id, i.agent_id, i.tick_asked, i.tick_about, i.kind, i.question, i.truth, i.options, i.source_ref, "
        "a.call_id, a.answer, a.correct, a.abstained, a.retrieved FROM exam_items i JOIN exam_answers a ON a.item_id=i.item_id "
        "WHERE i.exam='audit' ORDER BY i.tick_asked, i.agent_id, i.item_id")
    return [{**dict(r), "options": json.loads(r["options"]), "retrieved": json.loads(r["retrieved"])} for r in rows]


def _first_option(options: list[str]) -> str:
    """A kind's first option: the first of the closed set before the stream's shuffle (it is sorted first)."""
    return sorted(o for o in options if o != ABSTAIN)[0]


@pytest.fixture(scope="module")
def e0_world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, Simulation]]:
    root = tmp_path_factory.mktemp("exam_validity")
    config = _e0_config(root, "exam_validity_e0")
    sim = _run(config, root / "run")
    yield config, sim
    sim.db.close()


@pytest.fixture(scope="module")
def stub_world(e0_world: tuple[Path, Simulation], tmp_path_factory: pytest.TempPathFactory) -> Iterator[Simulation]:
    """The same world with every scripted answer replaced by the kind's first option: memory plays no part."""
    config, _ = e0_world

    def first(self: Exams, rec: Any, draft: ex.Draft, options: list[str], retrieved: Any) -> tuple[str, str | None]:
        return _first_option(options), None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Exams, "scripted_answer", first)
        sim = _run(config, tmp_path_factory.mktemp("exam_validity_stub") / "run")
    yield sim
    sim.db.close()


# --- truths vary --------------------------------------------------------------------------------------------------
def test_the_e0_world_asks_at_least_two_kinds_with_varied_truths(e0_world: tuple[Path, Simulation]) -> None:
    _, sim = e0_world
    rows = _joined(sim)
    assert rows and set(sim.cfg.memory.exam.kinds) == {"balance", "neighbour", "node"}
    truths: dict[str, Counter[str]] = {}
    for r in rows:
        truths.setdefault(r["kind"], Counter())[r["truth"]] += 1
    varied = sorted(k for k, c in truths.items() if len(c) > 1)
    assert len(varied) >= 2, truths
    assert "balance" in varied, "the balance labels are cut on the dollar balance, so they move in a short world"
    for r in rows:
        if r["kind"] == "balance":
            assert set(r["options"][:-1]) == set(BALANCE_LABELS) and "$" in r["question"]
        if r["kind"] == "node":
            assert set(r["options"][:-1]) == set(STOCK_LABELS)


def test_single_truth_kinds_are_flagged_and_counted(e0_world: tuple[Path, Simulation]) -> None:
    """An item is flagged exactly when the agent's candidate observations admitted one truth for its kind."""
    _, sim = e0_world
    asked = _events(sim, Kind.EXAM_ASKED)
    assert asked and len(asked) == len(_joined(sim))
    delay = sim.cfg.memory.exam.delay_days * TPD
    for e in asked:
        p = e["payload"]
        rec = SimpleNamespace(agent_id=p["agent_id"])
        observed = sim.exams.observations(sim.exams.candidates(p["agent_id"], before_tick=e["tick"] - delay))
        truths = {d.truth for d in sim.exams.drafts(p["kind"], rec, observed)}  # type: ignore[arg-type]
        assert p["truth"] in truths
        assert bool(p.get("single_truth")) == (len(truths) == 1), (p["kind"], p["truth"], truths)
    report = sim.exams.last_report
    assert report is not None
    last = Counter(e["payload"]["kind"] for e in asked if e["tick"] == report.tick and e["payload"].get("single_truth"))
    assert report.single_truth == dict(last) and sum(last.values()) <= report.asked


def test_generation_is_stratified_by_truth_and_counts_single_truth_kinds(make_sim) -> None:
    exam = {"enabled": True, "delay_days": 1, "questions_per_agent": 2, "kinds": ["balance", "node"]}
    sim = make_sim("exam_validity_strata", deep_merge(NO_PROVIDER_TIERS, {"memory": {"exam": exam}}))
    start = sim.cfg.population.starting_balance_usd
    rec = SimpleNamespace(agent_id="ag_x", name="Xan", model_tier="scripted")

    def row(tick: int, balance: float, label: str) -> dict[str, Any]:
        obs = {"tick": tick, "balance_usd": balance, "balance_bucket": "comfortable",
               "nodes": [{"node_id": f"n{tick}", "stock_bucket": label, "distance": 1.0}]}
        return {"call_id": f"cl_{tick}", "tick": tick, "observation": json.dumps(obs)}

    flat = [row(t, start, "rich") for t in range(1, 11)]
    report = ExamReport(day=3, tick=25)
    items = sim.exams.generate(rec, flat, random.Random(1), report)  # type: ignore[arg-type]
    assert sorted(d.kind for d, _ in items) == ["balance", "node"]
    assert all(d.single_truth for d, _ in items) and report.single_truth == {"balance": 1, "node": 1}
    assert {d.truth for d, _ in items} == {"comfortable", "rich"}

    # nine rich sightings and one thin one: an unstratified draw asks about the thin node one time in ten
    skewed = [row(t, start * (0.9 if t % 2 else 1.1), "thin" if t == 7 else "rich") for t in range(1, 11)]
    drawn: dict[str, Counter[str]] = {"balance": Counter(), "node": Counter()}
    for seed in range(300):
        rep = ExamReport(day=3, tick=25)
        for d, options in sim.exams.generate(rec, skewed, random.Random(seed), rep):  # type: ignore[arg-type]
            drawn[d.kind][d.truth] += 1
            assert not d.single_truth and options[-1] == ABSTAIN and d.truth in options
        assert rep.single_truth == {}
    assert 0.35 < drawn["node"]["thin"] / sum(drawn["node"].values()) < 0.65, drawn
    assert set(drawn["balance"]) == {"low", "comfortable"}
    assert 0.35 < drawn["balance"]["low"] / sum(drawn["balance"].values()) < 0.65, drawn
    sim.db.close()


# --- a memoryless answerer cannot beat the majority baseline -----------------------------------------------------
def test_a_constant_answer_scores_at_most_the_majority_baseline(e0_world: tuple[Path, Simulation], stub_world: Simulation) -> None:
    _, sim = e0_world
    rows = _joined(stub_world)
    assert rows
    key = ("agent_id", "tick_asked", "tick_about", "kind", "question", "truth", "options")
    assert [tuple(r[k] for k in key) for r in rows] == [tuple(r[k] for k in key) for r in _joined(sim)], \
        "answers never feed back into the world or the draw"
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
        assert r["abstained"] == 0 and r["answer"] == _first_option(r["options"])
    majority_total = 0
    for kind, items in by_kind.items():
        majority = max(Counter(r["truth"] for r in items).values())  # the majority baseline, computed here
        expected = sum(r["truth"] == _first_option(r["options"]) for r in items)
        correct = sum(r["correct"] for r in items)
        assert correct == expected <= majority, (kind, correct, expected, majority)
        majority_total += majority
    accuracy = sum(r["correct"] for r in rows) / len(rows)
    baseline = majority_total / len(rows)
    assert accuracy <= baseline < 1.0, (accuracy, baseline)
    report = stub_world.exams.last_report
    assert report is not None and report.correct <= report.asked


# --- subject-scoped scripted answers -----------------------------------------------------------------------------
NODE_NOTE = RetrievedNote("nt_1", "Node n3 seen rich on day 1", "Saw [[Node n3]] at (4, 5); it was rich, a comfortable spot.",
                          0.9, 0, None, 3)
MONEY_NOTE = RetrievedNote("nt_2", "Windfall on day 2", "After the storm my balance was low again.", 0.5, 0, None, 20)
TAGS = {"nt_1": ["resources", "places"], "nt_2": ["money", "mystery"]}


def _balance_draft() -> ex.Draft:
    d = ex.question_balance({"tick": 30, "balance_usd": 1.2, "balance_bucket": "comfortable"}, "cl_1", 1.5)
    assert d is not None
    return d


def test_balance_and_stock_vocabularies_are_disjoint() -> None:
    assert not set(BALANCE_LABELS) & set(STOCK_LABELS)
    d = _balance_draft()
    assert d.truth == "low" and tuple(d.options) == BALANCE_LABELS
    assert d.question == ("At tick 30, was your balance broke (under $0.75), low ($0.75 to under $1.50), "
                          "comfortable ($1.50 to under $2.25) or flush ($2.25 or more)?")
    start = 1_500_000
    assert [ex.balance_label(b, start) for b in (0, 749_999, 750_000, 1_499_999, 1_500_000, 2_249_999, 2_250_000)] == \
        ["broke", "broke", "low", "low", "comfortable", "comfortable", "flush"]
    assert ex.question_balance({"tick": 3, "balance_bucket": "thin"}, "cl_1", 1.5) is None, "needs the dollar balance"
    assert ex.question_balance({"tick": 3, "balance_usd": 1.0}, "cl_1", 0.0) is None
    legacy = ex.question_balance({"tick": 3, "balance_bucket": "thin"}, "cl_1")
    assert legacy is not None and tuple(legacy.options) == BALANCE_BUCKETS, "without a start: the kernel's buckets"


def test_scripted_balance_answer_comes_from_a_money_note_not_a_node_note() -> None:
    d = _balance_draft()
    options = ex.finalize_options(d.options, d.truth, random.Random(4))
    # the node note ranks first and names a balance label; only the money note is about the purse
    assert answer_from_memory(d, options, [NODE_NOTE, MONEY_NOTE], TAGS) == ("low", MONEY_NOTE.title)
    assert answer_from_memory(d, options, [NODE_NOTE], TAGS) == (ABSTAIN, None)
    # even with the old overlapping vocabulary the node note answers nothing; unscoped, it answered "rich"
    old = ["rich", "empty", "comfortable", "thin", ABSTAIN]
    assert match_option(f"{NODE_NOTE.title} {NODE_NOTE.body}", old) == "rich"
    assert answer_from_memory(d, old, [NODE_NOTE], TAGS) == (ABSTAIN, None)
    # untagged notes count when their title is about the purse; the self summary comes last
    purse = RetrievedNote("nt_3", "My purse on day 2", "It felt flush.", 0.4, 0, None, 21)
    assert answer_from_memory(d, options, [NODE_NOTE, purse], {}) == ("flush", purse.title)
    assert answer_from_memory(d, options, [NODE_NOTE], TAGS, "I am Ada. My balance is broke.") == ("broke", SELF_SOURCE)
    assert answer_from_memory(d, options, [MONEY_NOTE], TAGS, "My balance is broke.") == ("low", MONEY_NOTE.title)
    self_tagged = RetrievedNote("nt_4", "About me", "Comfortable, mostly.", 0.4, 0, None, 22)
    assert answer_from_memory(d, options, [self_tagged], {"nt_4": ["self"]})[0] == "comfortable"


def test_scripted_node_and_people_answers_stay_on_their_subject() -> None:
    obs = {"tick": 30, "nodes": [{"node_id": "n3", "stock_bucket": "thin"}, {"node_id": "n30", "stock_bucket": "rich"}]}
    n3 = next(d for d in ex.node_questions(obs, "cl_1") if d.subject == "n3")
    other = RetrievedNote("nt_5", "Node n30 seen rich on day 1", "Saw [[Node n30]]; it was rich.", 0.9, 0, None, 3)
    mine = RetrievedNote("nt_6", "Foraging at node n3 on day 1", "Foraged at [[Node n3]]; it was thin.", 0.5, 0, None, 4)
    options = list(STOCK_LABELS) + [ABSTAIN]
    assert answer_from_memory(n3, options, [other, mine]) == ("thin", mine.title)
    assert answer_from_memory(n3, options, [other]) == (ABSTAIN, None), "n30 is not n3"
    near = ex.question_neighbour({"tick": 30, "neighbours": [{"name": "Bao", "distance": 1.0}]}, ["Ada", "Bao"], "cl_1")
    assert near is not None
    motto = RetrievedNote("nt_7", "A saying", "Nobody remembers the quiet ones.", 0.9, 0, None, 3)
    met = RetrievedNote("nt_8", "Bao said something on day 1", "[[Bao]] told me: the north node is rich", 0.4, 0, None, 5)
    opts = [NOBODY, "Ada", "Bao", ABSTAIN]
    assert answer_from_memory(near, opts, [motto]) == (ABSTAIN, None), "a stray 'nobody' names no inhabitant"
    assert answer_from_memory(near, opts, [motto, met]) == ("Bao", met.title)


# --- full response text ----------------------------------------------------------------------------------------------
class LongWinded:
    """A provider-shaped brain whose structured reply carries far more than RAW_TEXT_CHARS of text."""

    is_llm = True
    tier = "frontier"

    def __init__(self) -> None:
        self.raw: list[str] = []

    async def complete_json(self, prompt: str, *, schema: dict[str, Any], system: str, max_tokens: int) -> BrainResult:
        first = re.search(r"^1\. (.+)$", prompt, re.MULTILINE)
        assert first is not None
        raw = json.dumps({"answer": first.group(1), "confidence": 0.6}) + "\n" * (RAW_TEXT_CHARS + 1000)
        self.raw.append(raw)
        return BrainResult(None, Usage(input_tokens=300, output_tokens=40), 2, "end_turn", raw, "req_long", model="fake-model")


def test_exam_response_text_is_stored_whole_when_prompts_are_logged(make_sim, monkeypatch: pytest.MonkeyPatch) -> None:
    over = deep_merge(NO_PROVIDER_TIERS, {
        "run": {"days": 6, "ticks_per_day": TPD}, "economy": {"research_pool_usd": 1.0},
        "memory": {"exam": {"enabled": True, "delay_days": 1, "questions_per_agent": 2, "kinds": ["balance", "neighbour"]}}})
    sim = make_sim("exam_validity_raw", over)
    assert sim.cfg.run.log_prompts and sim.cfg.tiers["frontier"].provider != "scripted"

    async def two_days() -> None:
        await sim.setup()
        for _ in range(2 * TPD):
            await sim.tick()

    asyncio.run(two_days())
    ada = next(a for a in sim.registry.alive() if a.name == "Ada")
    sim.registry.update(ada.agent_id, model_tier="frontier")
    brain = LongWinded()
    sim.brains["frontier"] = brain  # type: ignore[assignment]

    def stored(tick: int) -> list[str]:
        rows = sim.db.fetchall("SELECT raw_text FROM llm_calls WHERE purpose='exam' AND agent_id=? AND tick=? ORDER BY call_id",
                               (ada.agent_id, tick))
        return [str(r["raw_text"]) for r in rows]

    asyncio.run(sim.exams.run_day(3, 2 * TPD + 1))
    whole = stored(2 * TPD + 1)
    assert whole and all(len(t) > RAW_TEXT_CHARS for t in whole)
    assert sorted(whole) == sorted(brain.raw), "logged prompts: the response is stored exactly as returned"
    answers = [r for r in _joined(sim) if r["agent_id"] == ada.agent_id and r["tick_asked"] == 2 * TPD + 1]
    assert answers and all(r["call_id"] and r["abstained"] == 0 for r in answers)

    monkeypatch.setattr(sim.cfg.run, "log_prompts", False)
    asyncio.run(sim.exams.run_day(4, 3 * TPD + 1))
    cut = stored(3 * TPD + 1)
    assert cut and all(len(t) == RAW_TEXT_CHARS for t in cut), "unlogged prompts keep the old cut"
    sim.db.close()


# --- determinism -------------------------------------------------------------------------------------------------------
def _events(sim: Simulation, kind: str) -> list[dict[str, Any]]:
    rows = sim.db.fetchall("SELECT tick, day, kind, agent_id, payload, visibility FROM events WHERE kind=? ORDER BY seq", (kind,))
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def test_twin_runs_produce_the_same_exam(e0_world: tuple[Path, Simulation], tmp_path_factory: pytest.TempPathFactory) -> None:
    config, sim = e0_world
    twin = _run(config, tmp_path_factory.mktemp("exam_validity_twin") / "run")
    try:
        assert _joined(twin) == _joined(sim) and _joined(sim)
        for kind in (Kind.EXAM_ASKED, Kind.EXAM_ANSWERED):
            assert _events(twin, kind) == _events(sim, kind)
        assert twin.exams.last_report == sim.exams.last_report
    finally:
        twin.db.close()
