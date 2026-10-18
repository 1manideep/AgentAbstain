"""Lineage outcomes: lineage fitness F_L, net worth, cohort-by-generation fitness and exam accuracy, max generation.

A hand-built ``world.db`` (three generations, a dead founder with living children, a parentless newcomer, estates and an
audit exam) pins every outcome, end of run and per day, to values worked out by hand. Two scripted runs check the same
outcomes on real worlds: two days in which nobody dies (a founder alive throughout scores exactly the run's days), and
three scarce days with bankruptcies, replacements and exams, checked against the metrics rows, the death events and
the wallet ledger. Every value is resolved through ``end_of_run_outcome`` (or the ``DAILY`` table ``compare`` reads).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from conftest import NO_PROVIDER_TIERS, SimRun, deep_merge

from void.config import load_config, micro_to_usd
from void.db import Database
from void.research.analysis import DAILY, computed_outcome_names, end_of_run_outcome, exam_accuracy, summarize
from void.research.outcomes import OUTCOMES, load_all
from void.research.outcomes.lineage import cohort_table, lineage_table

REPO = Path(__file__).resolve().parents[1]
NAMES = ("lineage_fitness", "net_worth", "cohort_fitness", "cohort_exam_accuracy", "max_generation")
WITH_DAILY = ("lineage_fitness", "net_worth", "cohort_fitness", "max_generation")
TPD = 4
LAST_TICK = 20


def _conn(run_dir: Path) -> closing[sqlite3.Connection]:
    conn = sqlite3.connect(str(run_dir / "world.db"))
    conn.row_factory = sqlite3.Row
    return closing(conn)


def _daily(run_dir: Path, path: str, day: int) -> float | None:
    load_all()
    head, _, rest = path.partition(".")
    return DAILY[head](run_dir, rest or None, day)


# --- the registry ------------------------------------------------------------------------------------------------
def test_outcomes_are_registered_with_their_daily_versions() -> None:
    load_all()
    listed = computed_outcome_names()
    for name in NAMES:
        assert name in OUTCOMES and name in listed, name
        assert OUTCOMES[name].__module__ == "void.research.outcomes.lineage"
    for name in WITH_DAILY:
        assert name in DAILY, name
    assert "cohort_exam_accuracy" not in DAILY


# --- a hand-built world --------------------------------------------------------------------------------------------
# (agent_id, name, parent_id, generation, born_tick, died_tick, status, end balance in micro-dollars)
AGENTS = (
    ("f1", "Fa", None, 0, 0, 8, "archived", 0),             # a founder that died with living children
    ("f2", "Fb", None, 0, 0, None, "alive", 1_000_000),
    ("f3", "Nova", None, 0, 12, None, "alive", 500_000),    # a parentless newcomer is its own founder
    ("c1", "Fa-1", "f1", 1, 4, None, "alive", 400_000),
    ("c2", "Fa-2", "f1", 1, 8, 16, "archived", 0),
    ("g1", "Fa-1-1", "c1", 2, 10, None, "alive", 250_000),
    ("g2", "Fa-2-1", "c2", 2, 14, None, "alive", 50_000),
    ("d1", "Fb-1", "f2", 1, 6, 10, "bankrupt", 0),          # spent down to nothing: no estate
)
# (entry, tick, wallet, delta, kind): consistent with the end balances above, so balances at any tick are known
LEDGER = (
    ("l01", 0, "f1", 300_000, "starting_balance"), ("l02", 8, "f1", -300_000, "estate_out"),
    ("l03", 8, "spawn_pool", 300_000, "estate_in"),
    ("l04", 0, "f2", 1_000_000, "starting_balance"),
    ("l05", 12, "f3", 500_000, "spawn_grant"),
    ("l06", 4, "c1", 400_000, "spawn_grant"),
    ("l07", 8, "c2", 150_000, "spawn_grant"), ("l08", 12, "c2", -50_000, "llm_call"),
    ("l09", 16, "c2", -100_000, "estate_out"), ("l10", 16, "spawn_pool", 100_000, "estate_in"),
    ("l11", 10, "g1", 200_000, "spawn_grant"), ("l12", 18, "g1", 50_000, "forage"),
    ("l13", 14, "g2", 50_000, "spawn_grant"),
    ("l14", 6, "d1", 50_000, "endowment_in"), ("l15", 9, "d1", -50_000, "llm_call"),
)
# (item, exam, agent, answer row (call_id, answer, correct, abstained) or None)
EXAM = (
    ("e1", "audit", "f2", (None, "rich", 1, 0)),
    ("e2", "audit", "f2", (None, "thin", 0, 0)),
    ("e3", "audit", "c1", (None, "rich", 1, 0)),
    ("e4", "audit", "c1", (None, "rich", 1, 0)),
    ("e5", "audit", "d1", (None, "I do not remember", 0, 1)),
    ("e6", "audit", "c1", ("cl_failed", None, 0, 1)),   # a paid call with no usable answer: not graded
    ("e7", "reward", "c1", (None, "rich", 1, 0)),       # not the audit exam
    ("e8", "audit", "g1", (None, "rich", 1, 0)),
    ("e9", "audit", "g2", None),                        # never answered
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    cfg = load_config(REPO / "configs" / "base.yaml", {"run": {"days": 5, "ticks_per_day": TPD}})
    (tmp_path / "config.resolved.yaml").write_text(cfg.to_yaml())
    db = Database(tmp_path / "world.db")
    try:
        for aid, name, parent, gen, born, died, status, balance in AGENTS:
            db.execute("INSERT INTO agents(agent_id, name, parent_id, model_tier, balance, personality_seed, memory_path, generation, "
                       "status, x, y, entropy_budget, born_tick, died_tick, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, name, parent, "scripted", balance, "{}", "", gen, status, 0, 0, 1, born, died, f"tick:{born}"))
        for entry, tick, wallet, delta, kind in LEDGER:
            db.execute("INSERT INTO wallet_ledger(entry_id, tick, agent_id, delta, balance_after, kind) VALUES(?,?,?,?,?,?)",
                       (entry, tick, wallet, delta, 0, kind))
        for item, exam, aid, answer in EXAM:
            db.execute("INSERT INTO exam_items(item_id, exam, agent_id, tick_asked, tick_about, kind, question, truth, options, "
                       "source_ref) VALUES(?,?,?,?,?,?,?,?,?,?)", (item, exam, aid, 16, 4, "node", "q?", "rich", "[]", "ref"))
            if answer is not None:
                db.execute("INSERT INTO exam_answers(item_id, call_id, answer, correct, abstained) VALUES(?,?,?,?,?)", (item, *answer))
        db.kv_set("tick", LAST_TICK)
    finally:
        db.close()
    return tmp_path


def test_lineage_fitness_and_net_worth_match_the_hand_built_tree(tree: Path) -> None:
    # agent-days at tick 20, 4 ticks a day: f1 8/4=2, c1 16/4=4, c2 8/4=2, g1 10/4=2.5, g2 6/4=1.5 (lineage 12);
    # f2 20/4=5, d1 4/4=1 (lineage 6); the newcomer f3 8/4=2
    assert end_of_run_outcome(tree, "lineage_fitness") == pytest.approx((12 + 6 + 2) / 3, abs=1e-12)
    # f1's lineage: living c1 0.40 + g1 0.25 + g2 0.05, estates f1 0.30 + c2 0.10; f2 1.00 (d1 left nothing); f3 0.50
    assert end_of_run_outcome(tree, "net_worth") == pytest.approx((1.10 + 1.00 + 0.50) / 3, abs=1e-12)
    # one founder's lineage, by name or by id
    assert end_of_run_outcome(tree, "lineage_fitness.founder.Fa") == pytest.approx(12.0, abs=1e-12)
    assert end_of_run_outcome(tree, "lineage_fitness.founder.f1") == pytest.approx(12.0, abs=1e-12)
    assert end_of_run_outcome(tree, "lineage_fitness.founder.Nova") == pytest.approx(2.0, abs=1e-12)
    assert end_of_run_outcome(tree, "net_worth.founder.Fa") == pytest.approx(1.10, abs=1e-12)
    assert end_of_run_outcome(tree, "net_worth.founder.Fb") == pytest.approx(1.00, abs=1e-12)
    assert end_of_run_outcome(tree, "lineage_fitness.founder.Fa-1") is None  # a child is not a founder
    assert end_of_run_outcome(tree, "net_worth.founder.nobody") is None
    for bad in ("lineage_fitness.tier.alpha", "lineage_fitness.founder", "net_worth.Fa"):
        with pytest.raises(ValueError, match="founder"):
            end_of_run_outcome(tree, bad)
    table = lineage_table(tree)
    assert {k: (v["name"], v["members"], v["living"]) for k, v in table.items()} == {
        "f1": ("Fa", 5, 3), "f2": ("Fb", 2, 1), "f3": ("Nova", 1, 1)}
    assert [round(v["agent_days"], 9) for v in table.values()] == [12.0, 6.0, 2.0]
    assert [round(v["net_worth_usd"], 9) for v in table.values()] == [1.10, 1.00, 0.50]


def test_cohort_outcomes_and_max_generation_match_the_hand_built_tree(tree: Path) -> None:
    assert end_of_run_outcome(tree, "cohort_fitness.0") == pytest.approx((2 + 5 + 2) / 3, abs=1e-12)
    assert end_of_run_outcome(tree, "cohort_fitness.1") == pytest.approx((4 + 2 + 1) / 3, abs=1e-12)
    assert end_of_run_outcome(tree, "cohort_fitness.2") == pytest.approx((2.5 + 1.5) / 2, abs=1e-12)
    assert end_of_run_outcome(tree, "cohort_fitness") == pytest.approx(20 / 8, abs=1e-12)  # every agent
    assert end_of_run_outcome(tree, "cohort_fitness.3") is None
    # graded audit items: f2 1 of 2; c1 2 of 2 and d1 0 of 1 (the failed call and the reward item are out); g1 1 of 1
    assert end_of_run_outcome(tree, "cohort_exam_accuracy.0") == pytest.approx(1 / 2)
    assert end_of_run_outcome(tree, "cohort_exam_accuracy.1") == pytest.approx(2 / 3)
    assert end_of_run_outcome(tree, "cohort_exam_accuracy.2") == 1.0
    assert end_of_run_outcome(tree, "cohort_exam_accuracy.3") is None
    assert end_of_run_outcome(tree, "cohort_exam_accuracy") == pytest.approx(4 / 6) == exam_accuracy(tree)
    assert end_of_run_outcome(tree, "max_generation") == 2.0
    for bad in ("cohort_fitness.alpha", "cohort_exam_accuracy.one"):
        with pytest.raises(ValueError, match="generation"):
            end_of_run_outcome(tree, bad)
    table = cohort_table(tree)
    assert {g: row["agents"] for g, row in table.items()} == {0: 3, 1: 3, 2: 2}
    assert [round(row["fitness"], 9) for row in table.values()] == [3.0, round(7 / 3, 9), 2.0]
    assert [row["exam_accuracy"] for row in table.values()] == pytest.approx([1 / 2, 2 / 3, 1.0])


def test_daily_values_stand_at_the_end_of_each_day(tree: Path) -> None:
    # end of day 2 (tick 8): f3, g1, g2 not yet born; f1 just died (estate 0.30), c2 holds 0.15, d1 still alive with 0.05
    assert _daily(tree, "lineage_fitness", 2) == pytest.approx((2 + 1 + 0 + 2 + 0.5) / 2, abs=1e-12)
    assert _daily(tree, "net_worth", 2) == pytest.approx(((0.40 + 0.15 + 0.30) + (1.00 + 0.05)) / 2, abs=1e-12)
    assert _daily(tree, "max_generation", 2) == 1.0
    assert _daily(tree, "cohort_fitness.1", 2) == pytest.approx((1 + 0 + 0.5) / 3, abs=1e-12)
    # end of day 3 (tick 12): f3 born this tick (a founder with no days yet), c2 holds 0.10, g1 0.20 before its forage
    assert _daily(tree, "lineage_fitness", 3) == pytest.approx(((2 + 2 + 1 + 0.5) + (3 + 1) + 0) / 3, abs=1e-12)
    assert _daily(tree, "net_worth", 3) == pytest.approx(((0.40 + 0.10 + 0.20 + 0.30) + 1.00 + 0.50) / 3, abs=1e-12)
    assert _daily(tree, "max_generation", 3) == 2.0
    living = {d: sum(v["living"] for v in lineage_table(tree, d).values()) for d in (1, 2, 3, 4, 5)}
    # tick 4: f1 f2 c1; 8: f2 c1 c2 d1 (f1 died at 8); 12: f2 f3 c1 c2 g1 (d1 died at 10); 16 and 20: f2 f3 c1 g1 g2
    assert living == {1: 3, 2: 4, 3: 5, 4: 5, 5: 5}
    # the last day, and any day past the run's end, is the end-of-run value
    for path in ("lineage_fitness", "net_worth", "cohort_fitness.2", "max_generation", "lineage_fitness.founder.Fa"):
        for day in (5, 9):
            assert _daily(tree, path, day) == pytest.approx(end_of_run_outcome(tree, path), abs=1e-12), (path, day)


def test_a_world_without_agents_has_no_lineage_outcomes(tmp_path: Path) -> None:
    Database(tmp_path / "world.db").close()
    for name in NAMES:
        assert end_of_run_outcome(tmp_path, name) is None, name
    assert lineage_table(tmp_path) == {} and cohort_table(tmp_path) == {}


# --- two scripted days in which nobody dies --------------------------------------------------------------------
@pytest.fixture(scope="module")
def two_days(run_sim) -> SimRun:
    return run_sim("lineage_two_days", deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 2}}))


def test_two_scripted_days_without_deaths(two_days: SimRun) -> None:
    run_dir, tpd = two_days.run_dir, two_days.cfg.run.ticks_per_day
    assert two_days.status == "completed"
    assert end_of_run_outcome(run_dir, "deaths") == 0.0
    last = 2 * tpd
    with _conn(run_dir) as conn:
        rows = conn.execute("SELECT parent_id, born_tick, balance FROM agents").fetchall()
    founders = sum(1 for r in rows if r["parent_id"] is None)
    f_l = end_of_run_outcome(run_dir, "lineage_fitness")
    assert isinstance(f_l, float)
    if len(rows) == founders:  # nobody was born: every founder lived both days
        assert abs(f_l - 2.0) < 1e-9
        assert abs(_daily(run_dir, "lineage_fitness", 1) - 1.0) < 1e-9
    else:
        assert abs(f_l - sum((last - r["born_tick"]) / tpd for r in rows) / founders) < 1e-9
    worth = end_of_run_outcome(run_dir, "net_worth")
    assert isinstance(worth, float)
    assert worth == pytest.approx(sum(micro_to_usd(r["balance"]) for r in rows) / founders, abs=1e-12)  # no estates
    if len(rows) == founders:
        assert worth == pytest.approx(summarize(run_dir)["mean_balance"], abs=1e-12)
    assert end_of_run_outcome(run_dir, "cohort_fitness.0") == pytest.approx(2.0, abs=1e-9)
    assert end_of_run_outcome(run_dir, "max_generation") == two_days.day_rows()[-1]["max_generation"]


# --- three scarce scripted days: deaths, replacements and exams ---------------------------------------------------
SCARCE = deep_merge(NO_PROVIDER_TIERS, {
    "run": {"days": 3},
    "population": {"starting_balance_usd": 0.12, "spawn_pool_usd": 6.0, "replacement_grant_usd": 0.5, "min_reserve_usd": 0.05},
    "world": {"forage_yield_usd": 0.002},
    "memory": {"exam": {"enabled": True, "delay_days": 1}},
    "economy": {"research_pool_usd": 1.0},
})


@pytest.fixture(scope="module")
def scarce(run_sim) -> SimRun:
    return run_sim("lineage_scarce", SCARCE)


def _agents(run_dir: Path) -> list[sqlite3.Row]:
    with _conn(run_dir) as conn:
        return conn.execute("SELECT agent_id, parent_id, generation, born_tick, died_tick, balance FROM agents").fetchall()


def test_scarce_world_lineages_partition_the_agents(scarce: SimRun) -> None:
    run_dir, tpd, last = scarce.run_dir, scarce.cfg.run.ticks_per_day, int(scarce.kv("tick"))
    deaths = scarce.events("death")
    assert deaths and any(b["payload"]["parent_id"] for b in scarce.events("birth"))  # the world has lineages to follow
    rows = _agents(run_dir)
    founders = [r for r in rows if r["parent_id"] is None]
    assert all(r["generation"] == 0 for r in founders)
    table = lineage_table(run_dir)
    assert set(table) == {r["agent_id"] for r in founders}
    assert sum(v["members"] for v in table.values()) == len(rows)
    # F_L times the founders is every agent-day the run held
    days = [((r["died_tick"] if r["died_tick"] is not None else last) - r["born_tick"]) / tpd for r in rows]
    assert end_of_run_outcome(run_dir, "lineage_fitness") * len(founders) == pytest.approx(sum(days), abs=1e-9)
    assert sum(v["agent_days"] for v in table.values()) == pytest.approx(sum(days), abs=1e-9)
    # net worth times the founders is the living balances plus every estate the death events announced
    living = sum(micro_to_usd(r["balance"]) for r in rows if r["died_tick"] is None)
    estates = sum(float(d["payload"]["estate_usd"]) for d in deaths)
    assert estates > 0
    assert end_of_run_outcome(run_dir, "net_worth") * len(founders) == pytest.approx(living + estates, abs=1e-9)
    # cohorts weigh back to the pooled mean; the deepest generation is the metrics row's
    cohorts = cohort_table(run_dir)
    assert sum(c["agents"] * c["fitness"] for c in cohorts.values()) == pytest.approx(sum(days), abs=1e-9)
    for g, c in cohorts.items():
        assert end_of_run_outcome(run_dir, f"cohort_fitness.{g}") == pytest.approx(c["fitness"], abs=1e-12)
    assert end_of_run_outcome(run_dir, "max_generation") == max(cohorts) == scarce.day_rows()[-1]["max_generation"]


def test_scarce_world_daily_values_match_the_metrics_and_the_ledger(scarce: SimRun) -> None:
    run_dir, tpd = scarce.run_dir, scarce.cfg.run.ticks_per_day
    rows = _agents(run_dir)
    last_row_of_day = {int(r["day"]): r for r in scarce.tick_rows()}
    day_rows = {int(r["day"]): r for r in scarce.day_rows()}
    deaths = scarce.events("death")
    with _conn(run_dir) as conn:
        for day in (1, 2, 3):
            cut = day * tpd
            alive = [r for r in rows if r["born_tick"] <= cut and (r["died_tick"] is None or r["died_tick"] > cut)]
            founders = sum(1 for r in rows if r["born_tick"] <= cut and r["parent_id"] is None)
            # balance at the cutoff from the ledger's own running balance: the last entry booked by then
            held = 0
            for r in alive:
                entry = conn.execute("SELECT balance_after FROM wallet_ledger WHERE agent_id=? AND tick<=? ORDER BY tick DESC, rowid DESC "
                                     "LIMIT 1", (r["agent_id"], cut)).fetchone()
                held += int(entry["balance_after"]) if entry is not None else 0
            estate = -int(conn.execute("SELECT COALESCE(SUM(delta),0) AS s FROM wallet_ledger WHERE kind='estate_out' AND tick<=?",
                                       (cut,)).fetchone()["s"])
            assert _daily(run_dir, "net_worth", day) == pytest.approx(micro_to_usd(held + estate) / founders, abs=1e-12), day
            # the living at the cutoff are the metrics row's population, holding its mean balance
            tick_row = last_row_of_day[day]
            table = lineage_table(run_dir, day)
            assert sum(v["living"] for v in table.values()) == tick_row["population"] == len(alive), day
            announced = sum(float(d["payload"]["estate_usd"]) for d in deaths if d["tick"] <= cut)
            assert sum(v["net_worth_usd"] for v in table.values()) == pytest.approx(
                tick_row["mean_balance"] * tick_row["population"] + announced, abs=1e-3), day
            assert _daily(run_dir, "max_generation", day) == day_rows[day]["max_generation"], day
    for path in ("lineage_fitness", "net_worth", "cohort_fitness.0", "cohort_fitness.1", "max_generation"):
        assert _daily(run_dir, path, 3) == pytest.approx(end_of_run_outcome(run_dir, path), abs=1e-12), path


def test_scarce_world_cohort_exam_accuracy(scarce: SimRun) -> None:
    run_dir = scarce.run_dir
    with _conn(run_dir) as conn:
        graded = conn.execute(
            "SELECT g.generation AS gen, COUNT(*) AS n, SUM(a.correct) AS c FROM exam_answers a "
            "JOIN exam_items i ON i.item_id = a.item_id JOIN agents g ON g.agent_id = i.agent_id "
            "WHERE i.exam='audit' AND NOT (a.call_id IS NOT NULL AND a.answer IS NULL) GROUP BY g.generation").fetchall()
    assert graded, "the scarce world asks an audit exam"
    for r in graded:
        assert end_of_run_outcome(run_dir, f"cohort_exam_accuracy.{r['gen']}") == pytest.approx(r["c"] / r["n"]), r["gen"]
    pooled = end_of_run_outcome(run_dir, "cohort_exam_accuracy")
    assert pooled == pytest.approx(sum(r["c"] for r in graded) / sum(r["n"] for r in graded)) == exam_accuracy(run_dir)
    absent = max(r["gen"] for r in graded) + 1
    if end_of_run_outcome(run_dir, "max_generation") < absent:
        assert end_of_run_outcome(run_dir, f"cohort_exam_accuracy.{absent}") is None
