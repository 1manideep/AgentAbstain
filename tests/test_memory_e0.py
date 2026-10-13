"""E0, the controls before any money is spent (MEMORY_EVOLUTION §6.2), end to end on scripted brains.

Four arms that differ only in the planted maintenance strategy run under fog with claim feedback, seeded
policies, paid maintenance and the audit exam. This module checks the *plumbing* the real 20-seed run relies
on: every arm pays for maintenance, each strategy leaves its signature in ``memory_commands`` and the vaults,
exams are asked and graded, money is conserved, and ``compare`` accepts the arms and reports the outcomes.
The pass conditions on effect sizes (index builder over no-op with power 0.8, decoy not separable from
no-op on fitness, hoarder below no-op) belong to the 20-seed run, not to a two-seed smoke test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_experiments import CONFIGS, run_arm

from void.config import usd_to_micro
from void.research.analysis import end_of_run_outcome, run_config
from void.research.compare import compare, render

ARMS = ("noop", "index_builder", "hoarder", "decoy")
SEEDS = (1, 2)
RUN = {"days": 4, "ticks_per_day": 12}


@pytest.fixture(scope="module")
def e0_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("exp_mem_e0")
    for arm in ARMS:
        run_arm(CONFIGS / f"exp_mem_e0_{arm}.yaml", out, SEEDS, run=RUN)
    return out


def _db(path: Path):
    import sqlite3

    conn = sqlite3.connect(str(path / "world.db"))
    conn.row_factory = sqlite3.Row
    return conn


def test_every_arm_pays_for_maintenance_once_per_agent_per_day(e0_dir: Path) -> None:
    for arm in ARMS:
        for seed in SEEDS:
            db = _db(e0_dir / arm / f"seed_{seed}")
            days = [int(r["d"]) for r in db.execute("SELECT DISTINCT (tick - 1) / 12 + 1 AS d FROM llm_calls WHERE purpose='maintain' ORDER BY d")]
            assert days and min(days) == 2, (arm, seed, days)  # nothing to maintain before day 2
            per_day = db.execute("SELECT tick, COUNT(*) AS n FROM llm_calls WHERE purpose='maintain' GROUP BY tick").fetchall()
            alive_at = {int(r["tick"]): int(r["n"]) for r in per_day}
            for tick, n in alive_at.items():
                alive = db.execute("SELECT COUNT(*) AS n FROM agents WHERE born_tick < ? AND (died_tick IS NULL OR died_tick >= ?)", (tick, tick)).fetchone()["n"]
                skipped = db.execute("SELECT COUNT(*) AS n FROM events WHERE kind='maintenance_skipped' AND tick=?", (tick,)).fetchone()["n"]
                assert n + skipped == alive, (arm, seed, tick, n, skipped, alive)
            ledger = db.execute("SELECT COALESCE(SUM(-delta),0) AS s FROM wallet_ledger WHERE kind='maintenance_call'").fetchone()["s"]
            calls = db.execute("SELECT COALESCE(SUM(world_cost),0) AS s FROM llm_calls WHERE purpose='maintain'").fetchone()["s"]
            assert ledger == calls, (arm, seed)
            for r in db.execute("SELECT prompt_text, system_hash FROM llm_calls WHERE purpose='maintain'"):
                assert r["prompt_text"] and r["system_hash"]


def test_each_strategy_leaves_its_signature(e0_dir: Path) -> None:
    for seed in SEEDS:
        cmds = {}
        for arm in ARMS:
            db = _db(e0_dir / arm / f"seed_{seed}")
            rows = db.execute("SELECT command, args, ok FROM memory_commands WHERE purpose='maintain'").fetchall()
            cmds[arm] = [(r["command"], json.loads(r["args"]), bool(r["ok"])) for r in rows]
        assert cmds["noop"] == []
        idx = [c for c in cmds["index_builder"] if (c[1].get("path") or "").startswith("/memories/index/")]
        assert idx and any(c[2] for c in idx), "index builder writes index files"
        copies = [c for c in cmds["hoarder"] if c[0] == "create" and "copy-of" in (c[1].get("path") or "")]
        assert copies, "hoarder copies notes"
        pol = [c for c in cmds["decoy"] if (c[1].get("path") or "") == "/memories/memory_policy.md"]
        assert pol and all(c[0] == "str_replace" for c in pol), "decoy rewrites its policy"
        db = _db(e0_dir / "decoy" / f"seed_{seed}")
        versions = db.execute("SELECT agent_id, version, source, similarity_to_parent, LENGTH(text) AS n FROM policy_versions "
                              "WHERE source='self_edit' ORDER BY agent_id, version").fetchall()
        assert versions and all(800 <= int(v["n"]) <= 2000 for v in versions)
        # the first rewrite leaves the seed policy behind; later nights draw on the same jargon, so they resemble each other
        firsts = {}
        for v in versions:
            firsts.setdefault(v["agent_id"], v)
        assert all(float(v["similarity_to_parent"]) < 0.3 for v in firsts.values())
        db = _db(e0_dir / "index_builder" / f"seed_{seed}")
        n_index = db.execute("SELECT COUNT(*) AS n FROM notes WHERE archived=0 AND tags LIKE '%\"index\"%'").fetchone()["n"]
        assert n_index > 0


def test_exams_are_asked_and_graded_in_every_arm(e0_dir: Path) -> None:
    for arm in ARMS:
        for seed in SEEDS:
            db = _db(e0_dir / arm / f"seed_{seed}")
            items = db.execute("SELECT COUNT(*) AS n FROM exam_items").fetchone()["n"]
            answers = db.execute("SELECT COUNT(*) AS n, SUM(correct) AS c, SUM(abstained) AS a FROM exam_answers").fetchone()
            assert items > 0 and int(answers["n"]) == items, (arm, seed)
            assert db.execute("SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='exam'").fetchone()["n"] == 0  # scripted: no paid call
            assert end_of_run_outcome(e0_dir / arm / f"seed_{seed}", "exam_score") is not None


def test_money_is_conserved_with_maintenance_and_the_research_pool(e0_dir: Path) -> None:
    for arm in ARMS:
        for seed in SEEDS:
            run_dir = e0_dir / arm / f"seed_{seed}"
            cfg = run_config(run_dir)
            db = _db(run_dir)
            kv = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT key, value FROM world_kv")}
            agents = db.execute("SELECT COALESCE(SUM(balance),0) AS s FROM agents").fetchone()["s"]
            ledger = db.execute("SELECT COALESCE(SUM(delta),0) AS s FROM wallet_ledger").fetchone()["s"]
            rhs = (agents + (kv["spawn_pool"] - usd_to_micro(cfg.population.spawn_pool_usd))
                   + (kv["house"] - usd_to_micro(cfg.economy.house_budget_usd)) + (kv["chronicle"] - usd_to_micro(cfg.economy.chronicle_budget_usd))
                   + (kv["research_pool"] - usd_to_micro(cfg.economy.research_pool_usd)) - kv.get("house_debt", 0))
            assert ledger == rhs, (arm, seed, ledger, rhs)


def test_compare_accepts_the_arms_and_reports_memory_outcomes(e0_dir: Path) -> None:
    result = compare(e0_dir)
    assert result["primary"] == "mean_balance" and set(result["arms"]) == set(ARMS) and result["n_seeds"] == len(SEEDS)
    assert "exam_score" in result["secondaries"]
    for arm in ARMS:
        for seed in SEEDS:
            vals = result["values"][arm][str(seed)]
            assert vals["mean_balance"] is not None and vals["exam_score"] is not None, (arm, seed, vals)
    text = render(result)
    assert "index_builder" in text and "noop" in text and "exam_score" in text
    assert len(result["comparisons"]) == 6  # every pair of four arms
