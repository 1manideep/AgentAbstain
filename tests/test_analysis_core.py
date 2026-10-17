"""Analysis core: the outcome registry, run-total counts, spend by purpose, the 10.4 diagnosis and the exam baseline.

Scripted runs only. One 3-day run of the E0 index-builder arm (maintenance, gossip, exams, gadgets) checks the
counts and the spend against the tables directly; a hand-built ``world.db`` pins the exam baseline, the memory
spend share, the cost per correct recall and the per-agent diagnosis to values worked out by hand.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_experiments import CONFIGS, REPO, run_arm

import void.research.analysis as analysis
from void.config import load_config, micro_to_usd
from void.db import Database
from void.research import outcomes
from void.research.analysis import (
    COUNTED_EVENTS,
    DAILY,
    calls_by_purpose,
    computed_outcome_names,
    cost_per_correct_recall,
    decide_cost_usd,
    end_of_run_outcome,
    exam_accuracy,
    exam_accuracy_over_baseline,
    exam_counts,
    exam_majority_baseline,
    forage_income_usd,
    load_metrics,
    memory_spend_share,
    summarize,
)


@pytest.fixture(scope="module")
def e0_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("analysis_core")
    # seed 2: three days hold exams, gossip and verified gadgets but no rejected one (``gadgets.rejected`` is 0)
    return run_arm(CONFIGS / "exp_mem_e0_index_builder.yaml", out, (2,), run={"days": 3}) / "seed_2"


def _db(run_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(run_dir / "world.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


# --- (1) the registry ------------------------------------------------------------------------------------------
def test_registered_outcome_resolves_and_is_listed(e0_run: Path) -> None:
    seen: list[str | None] = []

    def per_day(run_dir: str | Path, arg: str | None, day: int) -> float:
        return float(day)

    @outcomes.register("core_probe_outcome", daily=per_day)
    def probe(run_dir: str | Path, arg: str | None) -> float:
        seen.append(arg)
        return 41.5 if arg is None else float(len(arg))

    try:
        assert end_of_run_outcome(e0_run, "core_probe_outcome") == 41.5
        assert end_of_run_outcome(e0_run, "core_probe_outcome.abc") == 3.0
        assert seen == [None, "abc"]
        assert "core_probe_outcome" in computed_outcome_names()
        # load_all() put the daily version into the very dict compare draws its daily curves from
        compare_mod = importlib.import_module("void.research.compare")  # the package re-exports a function named compare
        assert DAILY["core_probe_outcome"] is per_day and compare_mod.DAILY is DAILY
        outcomes.register("core_probe_outcome")(probe)  # the same function again is a no-op
        with pytest.raises(ValueError, match="already registered"):
            outcomes.register("core_probe_outcome")(lambda rd, arg: 0.0)
        with pytest.raises(ValueError, match="COMPUTED"):
            outcomes.register("deaths")(probe)  # built-ins resolve first, so they cannot be shadowed
        for bad in ("a.b", "", "tick", "day"):
            with pytest.raises(ValueError):
                outcomes.register(bad)
    finally:
        outcomes.unregister("core_probe_outcome")
    assert "core_probe_outcome" not in computed_outcome_names() and "core_probe_outcome" not in DAILY
    assert end_of_run_outcome(e0_run, "core_probe_outcome") is None


def test_load_all_imports_every_submodule_once(e0_run: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extra = tmp_path / "extra_outcomes"
    extra.mkdir()
    (extra / "core_probe_mod.py").write_text(
        "from void.research.analysis import connect\n"
        "from void.research.outcomes import register\n\n\n"
        "@register('core_probe_agents', daily=lambda run_dir, arg, day: None)\n"
        "def agents(run_dir, arg):\n"
        "    with connect(run_dir) as db:\n"
        "        return float(db.execute('SELECT COUNT(*) AS n FROM agents').fetchone()['n'])\n")
    (extra / "_skipped_mod.py").write_text("raise RuntimeError('private submodules are not imported')\n")
    monkeypatch.setattr(outcomes, "__path__", [*outcomes.__path__, str(extra)])
    monkeypatch.setattr(outcomes, "_loaded", False)
    importlib.invalidate_caches()
    name = "void.research.outcomes.core_probe_mod"
    try:
        registered = outcomes.load_all()
        mod = sys.modules[name]
        assert "core_probe_agents" in registered and "core_probe_agents" in DAILY
        monkeypatch.setattr(outcomes, "_loaded", False)
        outcomes.load_all()  # a re-executed module would register a new function and raise
        assert sys.modules[name] is mod
        with _db(e0_run) as conn:
            assert end_of_run_outcome(e0_run, "core_probe_agents") == _one(conn, "SELECT COUNT(*) FROM agents")
        assert "core_probe_agents" in computed_outcome_names()
    finally:
        outcomes.unregister("core_probe_agents")
        sys.modules.pop(name, None)


# --- (2) run-total counts --------------------------------------------------------------------------------------
def test_counts_are_run_totals_from_the_tables(e0_run: Path) -> None:
    conn = _db(e0_run)
    tick_rows, day_rows = load_metrics(e0_run)
    days = [int(r["day"]) for r in day_rows]
    for name, kind in COUNTED_EVENTS.items():
        n = _one(conn, "SELECT COUNT(*) FROM events WHERE kind=?", (kind,))
        assert end_of_run_outcome(e0_run, name) == n, name
        assert sum(DAILY[name](e0_run, None, d) for d in days) == n, name  # each day's own count
        assert end_of_run_outcome(e0_run, f"tick.{name}") == tick_rows[-1][name]  # the last tick row, as before
    gossip = _one(conn, "SELECT COUNT(*) FROM events WHERE kind='gossip_transfer'")
    assert gossip > tick_rows[-1]["gossip_transfers"], "the run must tell a run total from the last tick's count"
    births = _one(conn, "SELECT COUNT(*) FROM events WHERE kind='birth'")
    assert end_of_run_outcome(e0_run, "births.scripted") == births and end_of_run_outcome(e0_run, "births.nosuchtier") == 0.0
    for status in ("proposed", "verified", "rejected", "tampered"):
        n = _one(conn, "SELECT COUNT(*) FROM gadgets WHERE status=?", (status,))
        assert end_of_run_outcome(e0_run, f"gadgets.{status}") == n, status
    assert end_of_run_outcome(e0_run, "gadgets.rejected") == 0.0  # no gadget was rejected: 0, not None
    assert end_of_run_outcome(e0_run, "gadgets") == _one(conn, "SELECT COUNT(*) FROM gadgets") > 0


# --- (3) spend, exam, maintenance and policy in the summary -----------------------------------------------------
def test_summary_counts_every_call_and_the_memory_layer(e0_run: Path) -> None:
    s = summarize(e0_run)
    conn = _db(e0_run)
    by = s["calls_by_purpose"]
    assert {"decide", "maintain"} <= set(by)
    assert by == calls_by_purpose(e0_run)
    total = _one(conn, "SELECT SUM(real_cost) FROM llm_calls")
    assert sum(v["real_cost_usd"] for v in by.values()) == pytest.approx(micro_to_usd(total), abs=1e-6)
    assert s["total_real_cost_usd"] == pytest.approx(micro_to_usd(total), abs=1e-6)
    assert sum(v["calls"] for v in by.values()) == _one(conn, "SELECT COUNT(*) FROM llm_calls")
    for purpose, v in by.items():
        world = _one(conn, "SELECT SUM(world_cost) FROM llm_calls WHERE purpose=?", (purpose,))
        assert v["world_cost_usd"] == pytest.approx(micro_to_usd(world), abs=1e-6)
    # the old keys stay decide-only, and maintenance is what they missed
    assert (s["calls"], s["real_cost_usd"]) == (by["decide"]["calls"], by["decide"]["real_cost_usd"])
    assert s["total_real_cost_usd"] == pytest.approx(by["decide"]["real_cost_usd"] + by["maintain"]["real_cost_usd"], abs=1e-6)
    paid = sum(v["real_cost_usd"] for p, v in by.items() if p not in ("exam", "chronicle"))
    assert s["memory_spend_share"] == pytest.approx(by["maintain"]["real_cost_usd"] / paid)
    assert end_of_run_outcome(e0_run, "memory_spend_share") == pytest.approx(s["memory_spend_share"])

    counts = exam_counts(e0_run)
    assert counts is not None, "a 3-day E0 run examines day 1"
    asked, correct, abstained = counts
    exam = s["exam"]
    assert (exam["asked"], exam["correct"], exam["abstained"]) == counts
    assert exam["accuracy"] == pytest.approx(correct / asked) and exam["score"] is not None
    assert exam["accuracy_over_baseline"] == pytest.approx(exam["accuracy"] - exam["majority_baseline"])
    assert 0.0 < exam["majority_baseline"] <= 1.0
    assert s["cost_per_correct_recall"] == pytest.approx(by["maintain"]["real_cost_usd"] / correct)

    maint = s["maintenance"]
    assert maint["commands_ok"] == _one(conn, "SELECT COUNT(*) FROM memory_commands WHERE purpose='maintain' AND ok=1") > 0
    assert maint["commands_refused"] == _one(conn, "SELECT COUNT(*) FROM memory_commands WHERE purpose='maintain' AND ok=0")
    assert maint["skipped_days"] == _one(conn, "SELECT COUNT(*) FROM events WHERE kind='maintenance_skipped'")
    assert s["policy_versions"] == {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM policy_versions GROUP BY source")}
    assert s["policy_versions"].get("seed") == _one(conn, "SELECT COUNT(*) FROM agents")

    n_agents = _one(conn, "SELECT COUNT(*) FROM agents")
    forage = _one(conn, "SELECT SUM(delta) FROM wallet_ledger WHERE kind='forage'")
    assert s["forage_income_usd"] == pytest.approx(micro_to_usd(forage) / n_agents) and forage > 0
    assert s["decide_cost_usd"] == pytest.approx(by["decide"]["real_cost_usd"] / n_agents)
    tokens = conn.execute("SELECT AVG(input_tokens) FROM llm_calls WHERE purpose='decide'").fetchone()[0]
    assert s["mean_input_tokens_per_decide"] == pytest.approx(tokens)
    for name in ("forage_income_usd", "decide_cost_usd", "mean_input_tokens_per_decide"):
        assert end_of_run_outcome(e0_run, name) == pytest.approx(s[name]), name


def test_analyze_script_prints_the_new_keys(e0_run: Path) -> None:
    out = subprocess.run([sys.executable, str(REPO / "scripts" / "analyze.py"), str(e0_run), "--json",
                          "--outcome", "gadgets.rejected", "--outcome", "gossip_transfers"],
                         capture_output=True, text=True, check=True, cwd=REPO)
    data = json.loads(out.stdout)
    assert {"decide", "maintain"} <= set(data["calls_by_purpose"])
    for key in analysis.SUMMARY_KEYS:
        assert key in data, key
    assert data["outcomes"]["gadgets.rejected"] == 0.0 and data["outcomes"]["gossip_transfers"] > 0
    listed = subprocess.run([sys.executable, str(REPO / "scripts" / "analyze.py"), "--list-outcomes"],
                            capture_output=True, text=True, check=True, cwd=REPO).stdout.split()
    assert listed == computed_outcome_names() and "memory_spend_share" in listed
    text = subprocess.run([sys.executable, str(REPO / "scripts" / "analyze.py"), str(e0_run)],
                          capture_output=True, text=True, check=True, cwd=REPO).stdout
    for key in ("calls_by_purpose:", "  maintain:", "memory_spend_share:", "exam:", "  majority_baseline:", "maintenance:"):
        assert key in text, key


# --- (4)/(5) a hand-built world.db ------------------------------------------------------------------------------
def _hand_built(run_dir: Path) -> None:
    """Three agents (a1 and a2 alpha, b1 beta and bankrupt), an audit exam, calls of every purpose and a ledger."""
    db = Database(run_dir / "world.db")
    try:
        for aid, tier, status in (("a1", "alpha", "alive"), ("a2", "alpha", "alive"), ("b1", "beta", "bankrupt")):
            db.execute("INSERT INTO agents(agent_id, name, model_tier, personality_seed, memory_path, status, x, y, entropy_budget, "
                       "born_tick, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (aid, aid.upper(), tier, "{}", "", status, 0, 0, 1, 0, "t0"))
        # (item, exam, agent, kind, truth, answer row: (call_id, answer, correct, abstained) or None)
        items = [
            ("i1", "audit", "a1", "balance", "comfortable", (None, "comfortable", 1, 0)),
            ("i2", "audit", "a1", "balance", "comfortable", (None, "rich", 0, 0)),
            ("i3", "audit", "a1", "balance", " Comfortable", (None, "comfortable", 1, 0)),  # normalised like grading
            ("i4", "audit", "a1", "balance", "broke", (None, "comfortable", 0, 0)),
            ("i5", "audit", "a1", "node", "rich", (None, "rich", 1, 0)),
            ("i6", "audit", "a1", "node", "thin", (None, "I do not remember", 0, 1)),
            ("i7", "audit", "a2", "node", "thin", (None, "thin", 1, 0)),
            ("i8", "audit", "a2", "neighbour", "Ada", (None, "Bao", 0, 0)),
            ("i9", "audit", "b1", "neighbour", "Bao", (None, "Bao", 1, 0)),
            ("x1", "audit", "b1", "balance", "broke", ("cl_failed", None, 0, 1)),  # a failed paid call: not graded
            ("x2", "reward", "a1", "balance", "broke", (None, "broke", 1, 0)),     # not the audit exam
            ("x3", "audit", "a2", "node", "empty", None),                          # never answered
        ]
        for item_id, exam, aid, kind, truth, answer in items:
            db.execute("INSERT INTO exam_items(item_id, exam, agent_id, tick_asked, tick_about, kind, question, truth, options, source_ref) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)", (item_id, exam, aid, 30, 5, kind, "q?", truth, "[]", "ref"))
            if answer is not None:
                db.execute("INSERT INTO exam_answers(item_id, call_id, answer, correct, abstained) VALUES(?,?,?,?,?)", (item_id, *answer))
        # (call, agent, purpose, tier, real_cost micro, input tokens)
        calls = [
            ("c1", "a1", "decide", "alpha", 100_000, 200), ("c2", "a2", "decide", "alpha", 50_000, 400),
            ("c3", "b1", "decide", "beta", 30_000, 600), ("c4", "b1", "decide", "beta", 20_000, None),
            ("c5", "a1", "maintain", "alpha", 40_000, 900), ("c6", "b1", "maintain", "beta", 10_000, 900),
            ("c7", "a2", "gossip", "alpha", 5_000, 50), ("c8", "a1", "exam", "alpha", 70_000, 300),
            ("c9", "chronicle", "chronicle", "alpha", 90_000, 700),
        ]
        for call_id, aid, purpose, tier, real, tokens in calls:
            db.execute("INSERT INTO llm_calls(call_id, tick, agent_id, purpose, tier, model, provider, input_tokens, real_cost, world_cost) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)", (call_id, 3, aid, purpose, tier, "stub", "scripted", tokens, real, 2 * real))
        ledger = [("l1", "a1", 200_000, "forage"), ("l2", "a2", 100_000, "forage"), ("l3", "b1", 60_000, "forage"),
                  ("l4", "a1", 500_000, "transfer_in"), ("l5", "a2", -100_000, "llm_call")]
        for entry_id, aid, delta, kind in ledger:
            db.execute("INSERT INTO wallet_ledger(entry_id, tick, agent_id, delta, balance_after, kind) VALUES(?,?,?,?,?,?)",
                       (entry_id, 3, aid, delta, 0, kind))
    finally:
        db.close()


def test_exam_baseline_matches_a_hand_built_exam(tmp_path: Path) -> None:
    _hand_built(tmp_path)
    # graded: i1..i9. Commonest truths: balance 'comfortable' 3 of 4, node 'thin' 2 of 3, neighbour 1 of 2 -> 6 of 9
    assert exam_counts(tmp_path) == (9, 5, 1)
    assert exam_majority_baseline(tmp_path) == pytest.approx(6 / 9)
    assert exam_accuracy(tmp_path) == pytest.approx(5 / 9)
    assert exam_accuracy_over_baseline(tmp_path) == pytest.approx(5 / 9 - 6 / 9)
    assert end_of_run_outcome(tmp_path, "exam_majority_baseline") == pytest.approx(6 / 9)
    assert end_of_run_outcome(tmp_path, "exam_accuracy_over_baseline") == pytest.approx(-1 / 9)
    # alpha: i1..i8, baseline 3 + 2 + 1 of 8, 4 correct; beta: i9 alone
    assert exam_majority_baseline(tmp_path, "alpha") == pytest.approx(6 / 8)
    assert end_of_run_outcome(tmp_path, "exam_accuracy_over_baseline.alpha") == pytest.approx(4 / 8 - 6 / 8)
    assert exam_majority_baseline(tmp_path, "beta") == 1.0 and exam_accuracy_over_baseline(tmp_path, "beta") == 0.0


def test_spend_and_diagnosis_match_a_hand_built_run(tmp_path: Path) -> None:
    _hand_built(tmp_path)
    by = calls_by_purpose(tmp_path)
    assert by == {
        "chronicle": {"calls": 1, "real_cost_usd": 0.09, "world_cost_usd": 0.18},
        "decide": {"calls": 4, "real_cost_usd": 0.2, "world_cost_usd": 0.4},
        "exam": {"calls": 1, "real_cost_usd": 0.07, "world_cost_usd": 0.14},
        "gossip": {"calls": 1, "real_cost_usd": 0.005, "world_cost_usd": 0.01},
        "maintain": {"calls": 2, "real_cost_usd": 0.05, "world_cost_usd": 0.1},
    }
    # agent-paid: decide 200k + maintain 50k + gossip 5k; the exam (research pool) and the chronicle writer are out
    assert memory_spend_share(tmp_path) == pytest.approx(50 / 255)
    assert end_of_run_outcome(tmp_path, "memory_spend_share.alpha") == pytest.approx(40 / 195)
    assert cost_per_correct_recall(tmp_path) == pytest.approx(0.05 / 5)
    assert end_of_run_outcome(tmp_path, "cost_per_correct_recall.alpha") == pytest.approx(0.04 / 4)
    # per agent ever alive: three agents, the bankrupt one included; forage credits only
    assert forage_income_usd(tmp_path) == pytest.approx(0.36 / 3)
    assert end_of_run_outcome(tmp_path, "forage_income_usd.alpha") == pytest.approx(0.30 / 2)
    assert decide_cost_usd(tmp_path) == pytest.approx(0.20 / 3)
    assert end_of_run_outcome(tmp_path, "decide_cost_usd.beta") == pytest.approx(0.05)
    assert end_of_run_outcome(tmp_path, "mean_input_tokens_per_decide") == pytest.approx(400.0)  # NULL tokens skipped
    assert end_of_run_outcome(tmp_path, "mean_input_tokens_per_decide.alpha") == pytest.approx(300.0)


def test_spend_and_exam_endpoints_are_none_without_data(tmp_path: Path) -> None:
    Database(tmp_path / "world.db").close()
    for name in ("memory_spend_share", "cost_per_correct_recall", "forage_income_usd", "decide_cost_usd",
                 "mean_input_tokens_per_decide", "exam_majority_baseline", "exam_accuracy_over_baseline"):
        assert end_of_run_outcome(tmp_path, name) is None, name
    for name in ("deaths", "births", "gossip_transfers", "gadgets", "gadgets.rejected"):
        assert end_of_run_outcome(tmp_path, name) == 0.0, name
    assert calls_by_purpose(tmp_path) == {}


def test_a_run_made_before_the_memory_layer_still_summarizes(tmp_path: Path) -> None:
    _hand_built(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "world.db"))
    for table in ("exam_answers", "exam_items", "memory_commands", "policy_versions"):  # older runs never had them
        conn.execute(f"DROP TABLE {table}")
    conn.commit()
    conn.close()
    (tmp_path / "config.resolved.yaml").write_text(load_config(CONFIGS / "base.yaml").to_yaml())
    assert exam_counts(tmp_path) is None and exam_majority_baseline(tmp_path) is None
    assert end_of_run_outcome(tmp_path, "cost_per_correct_recall") is None
    assert end_of_run_outcome(tmp_path, "memory_spend_share") == pytest.approx(50 / 255)
    s = summarize(tmp_path)
    assert s["exam"] == {"asked": 0, "correct": 0, "abstained": 0, "score": None, "accuracy": None,
                         "majority_baseline": None, "accuracy_over_baseline": None}
    assert s["maintenance"] == {"commands_ok": 0, "commands_refused": 0, "skipped_days": 0} and s["policy_versions"] == {}
    assert s["total_real_cost_usd"] == pytest.approx(0.415) and s["real_cost_usd"] == pytest.approx(0.2)
