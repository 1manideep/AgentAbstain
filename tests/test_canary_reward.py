"""Canary facts in the chronicle and the reward exam of selection arm B (MEMORY_EVOLUTION §5.8, Appendix D, WP7).

Pure parts first: the canary generator is a pure function of its stream (unique clozes, closed answer sets, lines the
sanitizer leaves alone), a canary line is printed like any other chronicle line, scripted recall needs the line itself,
and canaries refuse to run without the chronicle. Then scripted runs of the E2 configs:

* ``e2_dir``: arms a and b at seeds 1 and 2 over three days, with ``reward_delay_days`` 1 so a three-day run reaches
  the reward exam. Canaries are recorded, linked to their chronicle items and the same in both arms; arm a asks no
  reward item; arm b asks some and (scripted brains keep the headline, not the lines) pays nothing; money is
  conserved; ``compare`` accepts the experiment and reports the reward outcomes.
* ``paid``: arm b over four days with a hook that writes each canary line into half the agents' notes as soon as it
  is printed, so some reward answers are right. Each correct answer is paid exactly ``recall_reward_usd`` from the
  research pool (ledger rows of kind ``recall_reward`` with the item as ``ref``), wrong and abstained ones are not,
  the audit exam pays nothing, money is conserved and a day is never examined twice.
* A provider tier answers the reward exam through one paid structured call with the reward system prompt, charged to
  the research pool, with a fake brain (nothing here can reach a model API).
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import deep_merge
from test_experiments import CONFIGS, run_arm

from void.brain.base import BrainResult, Usage
from void.brain.decision import sanitize_text
from void.config import load_config, micro_to_usd, usd_to_micro
from void.culture.chronicle import CANARY_ITEM_KIND, CANARY_SECTION, _DayBuilder, _render
from void.db import Database
from void.events import OPERATOR, Kind
from void.research import canary as cn
from void.research.analysis import end_of_run_outcome, exam_accuracy
from void.research.compare import compare, render
from void.research.exams import (
    ABSTAIN,
    CANARY_KIND,
    RECALL_REWARD_KIND,
    answer_from_memory,
    exam_system_prompt,
    reward_draft,
    reward_system_prompt,
)
from void.research.outcomes.reward_exam import goodhart_gap, recall_reward_usd_total, reward_exam_accuracy
from void.sim.loop import Simulation
from void.types import RetrievedNote

TPD = 12
SEEDS = (1, 2)
NO_MUTATION = {"population": {"tier_mutation_prob": 0.0}}
SHORT = deep_merge(NO_MUTATION, {"run": {"days": 3, "ticks_per_day": TPD}, "memory": {"selection": {"reward_delay_days": 1}}})
NODES = ["n1", "n2", "n3", "n4", "n5", "n6"]


def _tick(day: int) -> int:
    """The first tick of ``day``: when the loop writes the chronicle of the day before and runs the exams."""
    return (day - 1) * TPD + 1


def _db(run_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(run_dir / "world.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _money_conserved(conn: Any, cfg: Any) -> None:
    """The ledger identity of tests/test_e2e.py and tests/test_wallet.py, research pool term included."""
    kv = {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM world_kv").fetchall()}
    agents = int(conn.execute("SELECT COALESCE(SUM(balance),0) AS s FROM agents").fetchone()["s"])
    ledger = int(conn.execute("SELECT COALESCE(SUM(delta),0) AS s FROM wallet_ledger").fetchone()["s"])
    rhs = (agents + (kv["spawn_pool"] - usd_to_micro(cfg.population.spawn_pool_usd))
           + (kv["house"] - usd_to_micro(cfg.economy.house_budget_usd))
           + (kv["chronicle"] - usd_to_micro(cfg.economy.chronicle_budget_usd))
           + (kv["research_pool"] - usd_to_micro(cfg.economy.research_pool_usd)) - kv.get("house_debt", 0))
    assert ledger == rhs, f"ledger {ledger} != agents+pool+house+chronicle+research deltas {rhs}"


# --- pure parts ----------------------------------------------------------------------------------------------------
def test_generation_is_a_pure_function_of_the_stream() -> None:
    a = cn.generate(random.Random(7), 3, 5, NODES)
    b = cn.generate(random.Random(7), 3, 5, NODES)
    assert a == b and len(a) == 5
    assert cn.generate(random.Random(8), 3, 5, NODES) != a
    seen: set[str] = set()
    for day in range(1, 40):
        for t, text, subject, answer in cn.generate(random.Random(day), day, 5, NODES, seen):
            blanked = cn.cloze(text, answer)
            assert blanked not in seen, "every question of a run has one answer"
            seen.add(blanked)
            assert answer in t.answers and cn.BLANK in blanked and answer not in blanked.split()
            assert sanitize_text(text, single_line=True) == text and text.isascii() and text.endswith(".")
            assert not any(re.search(rf"(?<!\w){o}(?!\w)", text, re.I) for o in t.answers if o != answer)
            assert re.search(r"node n\d", text) and subject and "canary" not in text.lower()
    # a cloze already taken is drawn again, and ``taken`` is not modified
    taken = {cn.cloze(text, answer) for _, text, _, answer in a}
    before = set(taken)
    again = cn.generate(random.Random(7), 3, 5, NODES, taken)
    assert taken == before and not ({cn.cloze(x, ans) for _, x, _, ans in again} & taken)


def test_cloze_question_and_exact_recall() -> None:
    text, answer = "A trader at node n4 said the east well runs dry on day 9.", "east"
    blanked = cn.cloze(text, answer)
    assert blanked == "A trader at node n4 said the ___ well runs dry on day 9."
    assert cn.question(text, answer) == 'Which word fills the blank? "A trader at node n4 said the ___ well runs dry on day 9."'
    options = ["north", "south", "east", "west"]
    assert cn.fill_from("The chronicle said: a trader at node n4 said the EAST well runs dry on day 9", blanked, options) == "east"
    assert cn.fill_from("Node n4 is rich; the east road is long.", blanked, options) is None  # names the node, not the line
    assert cn.fill_from("A trader at node n4 said the east well runs dry on day 90.", blanked, options) is None
    with pytest.raises(ValueError):
        cn.cloze(text, "north")

    fact = cn.CanaryFact("cf_d002_1", 2, 25, "chronicle", "well rumour at node n4", text, answer, tuple(sorted(options)), "d2-4")
    draft = reward_draft(fact)
    assert draft.kind == CANARY_KIND and draft.truth == "east" and draft.subject == blanked
    assert draft.source_ref == "canary_facts:cf_d002_1" and draft.tick_about == 25
    hit = RetrievedNote("nt_1", "Chronicle line", f"The chronicle said: {text}", 1.0, 0, None, 10)
    near = RetrievedNote("nt_2", "Node n4 seen on day 2", "Saw node n4 at (3, 4); the east side was rich.", 1.0, 0, None, 11)
    opts = [*options, ABSTAIN]
    assert answer_from_memory(draft, opts, [near, hit]) == ("east", "Chronicle line")
    assert answer_from_memory(draft, opts, [near]) == (ABSTAIN, None)
    assert answer_from_memory(draft, opts, []) == (ABSTAIN, None)


def test_a_canary_line_is_printed_like_any_other_line() -> None:
    """Adding canaries to a day changes its page by exactly the canary lines, in "Weather and land", as ``- <line>``."""
    events = [(1, Kind.WEATHER, None, {"value": 0.1, "label": "mild"}), (2, Kind.TALK, "ag_1", {"speaker_id": "ag_1"}),
              (3, Kind.FORAGE, "ag_1", {"agent_id": "ag_1", "usd": 0.04})]

    def build(canaries: list[str]) -> tuple[_DayBuilder, str, list[Any]]:
        b = _DayBuilder(2, {"ag_1": "Ada"}, {})
        for seq, kind, aid, p in events:
            b.consume(seq, kind, aid, p)
        headline, sections, items = b.finish(canaries)
        return b, _render(2, headline, sections), items

    lines = ["A scout passing node n2 counted five crows at dusk.", "A trader at node n4 said the east well runs dry on day 9."]
    _, plain, plain_items = build([])
    b, page, items = build(lines)
    assert [x for x in page.splitlines() if x not in {f"- {t}" for t in lines}] == plain.splitlines()
    section = page.split(f"## {CANARY_SECTION}\n\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert section == ["- Weather ran mild all day (from +0.10 to +0.10).", *(f"- {t}" for t in lines)]
    by_id = {i[0]: i for i in items}
    weather = next(i for i in items if i[3].startswith("Weather ran"))
    for item_id, text in zip(b.canary_item_ids, lines, strict=True):
        _, kind, seq, stored = by_id[item_id]
        assert stored == text and kind == weather[1] == CANARY_ITEM_KIND and seq is None is weather[2]
    assert len(items) == len(plain_items) + len(lines)
    assert all(t not in page.split("\n", 3)[2] for t in lines), "never in the headline"


def test_canaries_need_the_chronicle(tmp_path: Path) -> None:
    cfg = load_config(CONFIGS / "exp_mem_e2_a.yaml", deep_merge(SHORT, {"chronicle": {"enabled": False}}))
    with pytest.raises(ValueError, match="chronicle.enabled"):
        cn.validate(cfg)
    with pytest.raises(ValueError, match="chronicle.enabled"):
        Simulation(cfg, tmp_path / "run")
    cn.validate(load_config(CONFIGS / "exp_mem_e2_a.yaml", {"memory": {"selection": {"canary": {"enabled": False}}},
                                                           "chronicle": {"enabled": False}}))


def test_eligible_facts_respect_the_delay_and_the_birth_tick(tmp_path: Path) -> None:
    cfg = load_config(CONFIGS / "exp_mem_e2_b.yaml")
    db = Database(tmp_path / "w.db")
    store = cn.Canaries(cfg, db)
    for day, tick in ((1, 13), (2, 25), (3, 37)):
        facts, fresh = store.plan(day, tick)
        assert fresh and len(facts) == cfg.memory.selection.canary.per_day
        with db.tx():
            store.record(facts, [f"d{day}-9"], fresh=True)
    again, fresh = store.plan(2, 99)
    assert not fresh and [f.tick for f in again] == [25] and again[0].chronicle_item_id == "d2-9"
    assert [f.tick for f in store.eligible(before_tick=30, born_tick=0)] == [13, 25]
    assert [f.tick for f in store.eligible(before_tick=30, born_tick=20)] == [25]
    assert store.eligible(before_tick=12, born_tick=0) == []
    db.close()


# --- the E2 experiment, scripted ----------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def e2_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("exp_mem_e2")
    for arm in ("a", "b"):
        run_arm(CONFIGS / f"exp_mem_e2_{arm}.yaml", out, SEEDS, **SHORT)
    return out


def test_canary_facts_are_recorded_linked_and_announced(e2_dir: Path) -> None:
    for arm in ("a", "b"):
        for seed in SEEDS:
            run_dir = e2_dir / arm / f"seed_{seed}"
            conn = _db(run_dir)
            facts = _rows(conn, "SELECT * FROM canary_facts ORDER BY day")
            assert [f["day"] for f in facts] == [1, 2, 3], (arm, seed)
            planted = _rows(conn, "SELECT tick, day, payload, visibility FROM events WHERE kind=? ORDER BY seq", (Kind.CANARY_PLANTED,))
            assert len(planted) == len(facts) and all(e["visibility"] == OPERATOR for e in planted)
            for f, e in zip(facts, planted, strict=True):
                payload = json.loads(e["payload"])
                assert payload["fact_id"] == f["fact_id"] and payload["answer"] == f["answer"] and e["tick"] == f["tick"]
                assert f["channel"] == "chronicle" and f["answer"] in json.loads(f["options"])
                item = conn.execute("SELECT * FROM chronicle_items WHERE day=? AND item_id=?", (f["day"], f["chronicle_item_id"])).fetchone()
                assert item is not None and item["text"] == f["text"]
                page = (run_dir / "chronicle" / f"day_{f['day']:03d}.md").read_text()
                assert f"\n- {f['text']}\n" in page
            # published with the next day's first tick, the last day's at the end of the run
            assert [f["tick"] for f in facts] == [_tick(2), _tick(3), 3 * TPD]
            conn.close()


def test_canaries_are_deterministic_and_the_same_in_both_arms(e2_dir: Path, tmp_path: Path) -> None:
    # The arms print the same facts. Where a fact lands in the day's item list (chronicle_item_id) is not compared across
    # arms: it follows the items before it, and those depend on what the agents did (agent ids, and so the scripted
    # brain's streams, derive from the run name; scripted agents reproduce, DESIGN §7.2, so the day's births differ).
    # The twin run below pins the item ids and the pages within one arm.
    fact = "day, tick, channel, subject, text, answer, options"
    for seed in SEEDS:
        a, b = (_rows(_db(e2_dir / arm / f"seed_{seed}"), f"SELECT {fact} FROM canary_facts ORDER BY day") for arm in ("a", "b"))
        assert a == b, "the arms read the same canaries"
    other = {tuple(r["text"] for r in _rows(_db(e2_dir / "a" / f"seed_{s}"), "SELECT text FROM canary_facts ORDER BY day")) for s in SEEDS}
    assert len(other) == 2, "a different seed prints different canaries"
    twin = run_arm(CONFIGS / "exp_mem_e2_b.yaml", tmp_path, (1,), **SHORT) / "seed_1"
    full = "fact_id, chronicle_item_id, " + fact
    assert _rows(_db(twin), f"SELECT {full} FROM canary_facts ORDER BY day") == _rows(_db(e2_dir / "b" / "seed_1"), f"SELECT {full} FROM canary_facts ORDER BY day")
    for day in (1, 2, 3):
        assert (twin / "chronicle" / f"day_{day:03d}.md").read_text() == (e2_dir / "b" / "seed_1" / "chronicle" / f"day_{day:03d}.md").read_text()


def test_canary_items_are_indistinguishable_from_other_items(e2_dir: Path) -> None:
    for arm in ("a", "b"):
        run_dir = e2_dir / arm / "seed_1"
        conn = _db(run_dir)
        canary_ids = {(r["day"], r["chronicle_item_id"]) for r in conn.execute("SELECT day, chronicle_item_id FROM canary_facts")}
        items = _rows(conn, "SELECT day, item_id, kind, event_seq, text FROM chronicle_items")
        ordinary = [i for i in items if (i["day"], i["item_id"]) not in canary_ids]
        canaries = [i for i in items if (i["day"], i["item_id"]) in canary_ids]
        assert len(canaries) == len(canary_ids) == 3
        weather_lines = [i for i in ordinary if i["kind"] == CANARY_ITEM_KIND]
        assert weather_lines and all(i["event_seq"] is None for i in weather_lines)
        for c in canaries:
            assert c["kind"] in {i["kind"] for i in ordinary} and c["event_seq"] is None
            assert re.fullmatch(r"d\d+-\d+", c["item_id"]) and sanitize_text(c["text"], single_line=True) == c["text"]
            assert c["text"].isascii() and c["text"].endswith(".") and c["text"][0].isupper()
            page = (run_dir / "chronicle" / f"day_{c['day']:03d}.md").read_text()
            assert not re.search(r"canary|reward|exam|quiz", page, re.I)
            assert c["text"] not in page.split("\n", 3)[2], "never in the headline"
            section = page.split(f"## {CANARY_SECTION}\n\n", 1)[1].split("\n\n", 1)[0].splitlines()
            assert f"- {c['text']}" in section and all(line.startswith("- ") for line in section)
        conn.close()


def test_arm_a_asks_no_reward_item_and_arm_b_does(e2_dir: Path) -> None:
    for seed in SEEDS:
        a, b = (_db(e2_dir / arm / f"seed_{seed}") for arm in ("a", "b"))
        assert a.execute("SELECT COUNT(*) AS n FROM exam_items WHERE exam='reward'").fetchone()["n"] == 0
        assert a.execute("SELECT COUNT(*) AS n FROM events WHERE kind=?", (Kind.RECALL_REWARD,)).fetchone()["n"] == 0
        assert a.execute("SELECT COUNT(*) AS n FROM wallet_ledger WHERE kind=?", (RECALL_REWARD_KIND,)).fetchone()["n"] == 0
        assert a.execute("SELECT COUNT(*) AS n FROM exam_items WHERE exam='audit'").fetchone()["n"] > 0
        rows = _rows(b, "SELECT i.*, x.answer, x.correct, x.abstained FROM exam_items i JOIN exam_answers x ON x.item_id=i.item_id "
                        "WHERE i.exam='reward' ORDER BY i.tick_asked, i.agent_id")
        alive = b.execute("SELECT COUNT(*) AS n FROM agents WHERE born_tick <= ? AND (died_tick IS NULL OR died_tick >= ?)",
                          (_tick(2), _tick(3))).fetchone()["n"]
        # day 3, one fact a day, delay one day: the day-1 canary (published at tick 13) is asked to every agent once
        assert len(rows) == alive and {r["tick_asked"] for r in rows} == {_tick(3)}
        fact = b.execute("SELECT * FROM canary_facts WHERE day=1").fetchone()
        for r in rows:
            assert r["kind"] == CANARY_KIND and r["truth"] == fact["answer"] and r["source_ref"] == f"canary_facts:{fact['fact_id']}"
            assert r["question"] == cn.question(fact["text"], fact["answer"]) and r["tick_about"] == fact["tick"]
            opts = json.loads(r["options"])
            assert opts[-1] == ABSTAIN and sorted(opts[:-1]) == json.loads(fact["options"])
            # scripted brains keep the headline, not the line: they abstain, and nothing is paid
            assert r["answer"] == ABSTAIN and r["abstained"] == 1 and r["correct"] == 0
        assert b.execute("SELECT COUNT(*) AS n FROM wallet_ledger WHERE kind=?", (RECALL_REWARD_KIND,)).fetchone()["n"] == 0
        asked = _rows(b, "SELECT payload FROM events WHERE kind=? ORDER BY seq", (Kind.EXAM_ASKED,))
        tagged = [json.loads(e["payload"]) for e in asked if json.loads(e["payload"]).get("exam") == "reward"]
        assert len(tagged) == len(rows) and all(p["kind"] == CANARY_KIND for p in tagged)
        assert all("exam" not in json.loads(e["payload"]) for e in asked if json.loads(e["payload"])["kind"] != CANARY_KIND)
        a.close()
        b.close()


def test_money_is_conserved_in_both_arms(e2_dir: Path) -> None:
    for arm in ("a", "b"):
        for seed in SEEDS:
            run_dir = e2_dir / arm / f"seed_{seed}"
            conn = _db(run_dir)
            _money_conserved(conn, load_config(run_dir / "config.resolved.yaml"))
            conn.close()


def test_compare_accepts_e2_and_reports_the_reward_outcomes(e2_dir: Path) -> None:
    result = compare(e2_dir)
    assert result["primary"] == "exam_accuracy" and set(result["arms"]) == {"a", "b"} and result["n_seeds"] == len(SEEDS)
    assert result["independent_variables"] == ["memory.selection.arm"]
    for name in ("reward_exam_accuracy", "goodhart_gap", "recall_reward_usd_total"):
        assert name in result["secondaries"]
    for seed in SEEDS:
        a, b = result["values"]["a"][str(seed)], result["values"]["b"][str(seed)]
        assert a["reward_exam_accuracy"] is None and a["goodhart_gap"] is None and a["recall_reward_usd_total"] == 0.0
        assert b["reward_exam_accuracy"] == 0.0 and b["recall_reward_usd_total"] == 0.0
        assert b["goodhart_gap"] == pytest.approx(0.0 - exam_accuracy(e2_dir / "b" / f"seed_{seed}"))
    text = render(result)
    assert "reward_exam_accuracy" in text and "goodhart_gap" in text and "recall_reward_usd_total" in text


# --- payments ---------------------------------------------------------------------------------------------------------
class _Paid:
    def __init__(self, sim: Simulation, run_dir: Path, planted: dict[str, set[str]], origins: dict[str, list[str]]) -> None:
        self.sim, self.run_dir, self.planted, self.origins = sim, run_dir, planted, origins

    def had_copy(self, fact_id: str, agent_id: str, tick: int) -> bool:
        """Whether a copy of a note the hook wrote for ``fact_id`` had reached ``agent_id`` by ``tick``: passed on by
        gossip, or inherited at birth (a child copies its parent's top notes, DESIGN §13)."""
        return any(n.agent_id == agent_id and (n.provenance.transfer_tick or n.created_tick) <= tick
                   for origin in self.origins[fact_id] for n in self.sim.memory.descendants_of(origin))


@pytest.fixture(scope="module")
def paid(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Arm b over four days; each canary line goes into the notes of every other agent (by id) when it is printed."""
    run_dir = tmp_path_factory.mktemp("e2_paid") / "run"
    cfg = load_config(CONFIGS / "exp_mem_e2_b.yaml", deep_merge(SHORT, {"run": {"days": 4, "seed": 3}}))
    sim = Simulation(cfg, run_dir)
    planted: dict[str, set[str]] = {}
    origins: dict[str, list[str]] = {}

    def hook(report: Any) -> None:
        for fact in sim.exams.canaries.facts():
            if fact.fact_id in planted:
                continue
            holders = sorted(a.agent_id for a in sim.registry.alive())[::2]
            origins[fact.fact_id] = [
                sim.memory.remember(aid, report.tick, f"The chronicle said: {fact.text}", title=f"Chronicle line of day {fact.day}",
                                    tags=["chronicle"], importance=0.9).note_id
                for aid in holders]
            planted[fact.fact_id] = set(holders)

    status = asyncio.run(sim.run(on_tick=hook))
    assert status == "completed"
    yield _Paid(sim, run_dir, planted, origins)
    sim.db.close()


def test_arm_b_pays_exactly_the_reward_per_correct_answer(paid: Any) -> None:
    sim = paid.sim
    amount = usd_to_micro(sim.cfg.memory.selection.recall_reward_usd)
    rows = _rows(sim.db.conn, "SELECT i.item_id, i.agent_id, i.tick_asked, i.source_ref, i.truth, x.answer, x.correct, x.abstained "
                              "FROM exam_items i JOIN exam_answers x ON x.item_id=i.item_id WHERE i.exam='reward'")
    correct = [r for r in rows if r["correct"]]
    assert correct and len(correct) < len(rows), "some reward answers are right, some are not"
    for r in rows:
        fact_id = r["source_ref"].split(":", 1)[1]
        if r["agent_id"] in paid.planted[fact_id]:  # the line itself is in the agent's notes: answered right
            assert r["correct"] and r["answer"] == r["truth"]
        elif r["correct"]:  # or a copy reached the agent before it was asked (with twelve agents, gossip and births do)
            assert r["answer"] == r["truth"] and paid.had_copy(fact_id, r["agent_id"], r["tick_asked"])
        else:  # scripted recall needs the line: without it the agent abstains
            assert r["abstained"] == 1
    credits = _rows(sim.db.conn, "SELECT agent_id, delta, ref, payload FROM wallet_ledger WHERE kind=? AND agent_id != 'research_pool'",
                    (RECALL_REWARD_KIND,))
    debits = _rows(sim.db.conn, "SELECT agent_id, delta, ref FROM wallet_ledger WHERE kind=? AND agent_id = 'research_pool'",
                   (RECALL_REWARD_KIND,))
    assert sorted(c["ref"] for c in credits) == sorted(r["item_id"] for r in correct) == sorted(d["ref"] for d in debits)
    assert all(c["delta"] == amount for c in credits) and all(d["delta"] == -amount for d in debits)
    assert sum(c["delta"] for c in credits) == len(correct) * amount == -sum(d["delta"] for d in debits)
    by_item = {r["item_id"]: r["agent_id"] for r in correct}
    assert all(by_item[c["ref"]] == c["agent_id"] and json.loads(c["payload"]) == {"from": "research_pool"} for c in credits)
    # scripted agents make no exam call, so the pool moved by the rewards and nothing else
    assert sim.wallet.balance("research_pool") == usd_to_micro(sim.cfg.economy.research_pool_usd) - len(correct) * amount
    events = [json.loads(r["payload"]) for r in sim.db.fetchall("SELECT payload FROM events WHERE kind=? AND visibility=?",
                                                                 (Kind.RECALL_REWARD, OPERATOR))]
    assert sorted(e["item_id"] for e in events) == sorted(by_item) and all(e["paid"] and e["amount_usd"] == 0.02 for e in events)
    assert sim.db.fetchone("SELECT COUNT(*) AS n FROM events WHERE kind=?", (Kind.RECALL_REWARD,))["n"] == len(correct)


def test_new_facts_are_asked_first_then_old_ones_fill_the_day(paid: Any) -> None:
    """Delay one day, two questions a day, one canary a day: day 3 can ask only the day-1 fact; day 4 asks the day-2 fact
    (never asked) and fills its second slot with the day-1 fact again."""
    sim = paid.sim
    fact_of_day = {int(r["day"]): f"canary_facts:{r['fact_id']}" for r in sim.db.fetchall("SELECT day, fact_id FROM canary_facts")}
    by_agent: dict[str, dict[int, list[str]]] = {}
    for r in sim.db.fetchall("SELECT agent_id, tick_asked, source_ref FROM exam_items WHERE exam='reward' ORDER BY item_id"):
        by_agent.setdefault(r["agent_id"], {}).setdefault(int(r["tick_asked"]), []).append(r["source_ref"])
    assert by_agent
    for days in by_agent.values():
        assert days[_tick(3)] == [fact_of_day[1]]
        assert sorted(days[_tick(4)]) == sorted([fact_of_day[1], fact_of_day[2]])


def test_the_audit_exam_pays_nothing(paid: Any) -> None:
    sim = paid.sim
    audit = {r["item_id"] for r in sim.db.fetchall("SELECT item_id FROM exam_items WHERE exam='audit'")}
    right = sim.db.fetchone("SELECT COUNT(*) AS n FROM exam_answers x JOIN exam_items i ON i.item_id=x.item_id "
                            "WHERE i.exam='audit' AND x.correct=1")["n"]
    assert audit and right > 0, "the audit exam has right answers, and none of them is paid"
    refs = {r["ref"] for r in sim.db.fetchall("SELECT ref FROM wallet_ledger WHERE ref IS NOT NULL")}
    assert not (audit & refs)
    pool_kinds = {r["kind"] for r in sim.db.fetchall("SELECT DISTINCT kind FROM wallet_ledger WHERE agent_id='research_pool'")}
    assert pool_kinds <= {RECALL_REWARD_KIND}


def test_money_is_conserved_with_rewards(paid: Any) -> None:
    assert paid.sim.db.fetchone("SELECT COUNT(*) AS n FROM wallet_ledger WHERE kind=?", (RECALL_REWARD_KIND,))["n"] > 0
    _money_conserved(paid.sim.db.conn, paid.sim.cfg)


def test_reward_outcomes_resolve_from_the_run(paid: Any) -> None:
    sim, run_dir = paid.sim, paid.run_dir
    rows = sim.db.fetchall("SELECT x.correct FROM exam_items i JOIN exam_answers x ON x.item_id=i.item_id WHERE i.exam='reward'")
    acc = sum(int(r["correct"]) for r in rows) / len(rows)
    assert reward_exam_accuracy(run_dir) == pytest.approx(acc) == end_of_run_outcome(run_dir, "reward_exam_accuracy")
    assert end_of_run_outcome(run_dir, "reward_exam_accuracy.scripted") == pytest.approx(acc)
    assert end_of_run_outcome(run_dir, "reward_exam_accuracy.frontier") is None
    assert goodhart_gap(run_dir) == pytest.approx(acc - exam_accuracy(run_dir))
    paid_usd = micro_to_usd(sum(int(r["delta"]) for r in sim.db.fetchall(
        "SELECT delta FROM wallet_ledger WHERE kind=? AND agent_id != 'research_pool'", (RECALL_REWARD_KIND,))))
    assert recall_reward_usd_total(run_dir) == pytest.approx(paid_usd) and paid_usd > 0
    from void.research.analysis import DAILY
    from void.research.outcomes import load_all

    load_all()
    by_day = [DAILY["recall_reward_usd_total"](run_dir, None, d) for d in (1, 2, 3, 4)]
    assert by_day[0] == by_day[1] == 0.0 and sum(by_day) == pytest.approx(paid_usd)
    assert DAILY["reward_exam_accuracy"](run_dir, None, 1) is None and DAILY["reward_exam_accuracy"](run_dir, None, 3) is not None


def test_a_reward_day_is_never_examined_twice(paid: Any) -> None:
    sim = paid.sim
    before = sim.db.fetchone("SELECT COUNT(*) AS n FROM exam_items WHERE exam='reward'")["n"]
    pool = sim.wallet.balance("research_pool")
    assert sim.db.kv_get("reward_exam_day") == 4
    assert asyncio.run(sim.exams.run_reward_day(4, _tick(4))) is None
    sim.db.kv_set("reward_exam_day", 3)  # as if the process died after day 4's items committed
    asyncio.run(sim.exams.run_reward_day(4, _tick(4)))
    assert sim.db.fetchone("SELECT COUNT(*) AS n FROM exam_items WHERE exam='reward'")["n"] == before
    assert sim.wallet.balance("research_pool") == pool


# --- a provider tier, with a fake brain --------------------------------------------------------------------------------
class TruthfulBrain:
    """A provider-shaped brain whose ``complete_json`` answers each reward question with its truth (or abstains)."""

    is_llm = True

    def __init__(self, truths: Callable[[str], str | None]) -> None:
        self.truths = truths
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, prompt: str, *, schema: dict[str, Any], system: str, max_tokens: int) -> BrainResult:
        self.calls.append({"prompt": prompt, "system": system})
        answer = self.truths(prompt) or ABSTAIN
        return BrainResult(None, Usage(input_tokens=300, output_tokens=10), 3, "end_turn",
                           json.dumps({"answer": answer, "confidence": 0.9}), "req_1", model="fake-model")


def test_a_provider_tier_gets_one_paid_call_per_reward_item(tmp_path: Path) -> None:
    """Two scripted days, then Ada moves onto the anthropic 'frontier' tier whose brain is a fake; day 3's reward exam is
    driven directly: one paid structured call with the reward system prompt, charged to the research pool, then paid."""
    sim = Simulation(load_config(CONFIGS / "exp_mem_e2_b.yaml", deep_merge(SHORT, {"run": {"days": 6}})), tmp_path / "run")

    async def drive(ticks: int) -> None:
        await sim.setup()
        for _ in range(ticks):
            await sim.tick()

    asyncio.run(drive(2 * TPD))  # days 1 and 2; the day-1 canary is printed at tick 13
    ada = next(a for a in sim.registry.alive() if a.name == "Ada")
    sim.registry.update(ada.agent_id, model_tier="frontier")
    assert sim.cfg.tiers["frontier"].provider == "anthropic"
    facts = {cn.question(f.text, f.answer): f.answer for f in sim.exams.canaries.facts()}
    fake = TruthfulBrain(lambda prompt: next((a for q, a in facts.items() if q in prompt), None))
    sim.brains["frontier"] = fake  # type: ignore[assignment]
    pool = sim.wallet.balance("research_pool")
    report = asyncio.run(sim.exams.run_reward_day(3, _tick(3)))
    assert report is not None and report.calls == len(fake.calls) == 1 and report.correct == report.paid == 1
    row = sim.db.fetchone("SELECT i.item_id, x.call_id, x.answer, x.correct FROM exam_items i JOIN exam_answers x ON x.item_id=i.item_id "
                          "WHERE i.exam='reward' AND i.agent_id=?", (ada.agent_id,))
    assert row["correct"] == 1 and row["call_id"] is not None
    call = sim.db.fetchone("SELECT * FROM llm_calls WHERE call_id=?", (row["call_id"],))
    assert call["purpose"] == "exam" and call["agent_id"] == ada.agent_id and call["world_cost"] > 0
    assert fake.calls[0]["system"] == reward_system_prompt(sim.cfg, "frontier") != exam_system_prompt(sim.cfg, "frontier")
    assert "pays $0.02 into your purse" in fake.calls[0]["system"]
    stored = sim.db.fetchone("SELECT text FROM prompt_texts WHERE hash=?", (call["system_hash"],))
    assert stored is not None and stored["text"] == reward_system_prompt(sim.cfg, "frontier")
    amount = usd_to_micro(sim.cfg.memory.selection.recall_reward_usd)
    assert sim.wallet.balance("research_pool") == pool - int(call["world_cost"]) - amount
    reward = sim.db.fetchone("SELECT agent_id, delta FROM wallet_ledger WHERE kind=? AND ref=? AND agent_id != 'research_pool'",
                             (RECALL_REWARD_KIND, row["item_id"]))
    assert reward["agent_id"] == ada.agent_id and reward["delta"] == amount
    sim.db.close()
