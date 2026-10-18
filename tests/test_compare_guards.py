"""The comparison's guards (DESIGN §17, RUNBOOK "compare refuses incomparable runs").

Tiny scripted runs are made once per module, copied into ``tmp_path`` and doctored: the run row's status is
rewritten with sqlite3, ``metrics.jsonl`` (and the ``metrics`` table) are truncated or removed, and resolved
configs are edited. Covered: incomplete runs (``capped_total``, truncated metrics, a crash with no metrics, a line
cut short) are refused, or dropped pair by pair with ``allow_incomplete``; ``exp_ladder`` / ``exp_gate`` refuse tier
mutation; the ``exp_mem`` family must run on equalized prices (``exp_memory`` is not in it); a difference in the exam
or the maintenance step outside the declared variables is refused for an ``exp_mem`` pair; the script's flag.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_experiments import CONFIGS, REPO, run_arm

from void.research.compare import (
    EQUALIZED_EXPERIMENTS,
    IncomparableRuns,
    compare,
    in_family,
    render,
    run_state,
)

MEM_RUN = {"days": 1, "ticks_per_day": 6}
LAST_TICK = 6
SEEDS = (1, 2)


@pytest.fixture(scope="module")
def mem_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("exp_mem_dep")
    for arm in ("mem", "amnesic"):
        run_arm(CONFIGS / f"exp_mem_dep_{arm}.yaml", out, SEEDS, run=MEM_RUN)
    return out


@pytest.fixture(scope="module")
def ladder_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("exp_ladder")
    for arm in ("ladder", "flat"):
        run_arm(CONFIGS / f"exp_ladder_{arm}.yaml", out, (1,), run={"days": 1, "ticks_per_day": 4})
    return out


# --- doctoring helpers -------------------------------------------------------------------------------------------
def _set(data: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    cur = data
    for k in keys[:-1]:
        cur = cur[k]
    cur[keys[-1]] = value


def _edit_configs(arm_dir: Path, sets: dict[str, Any]) -> None:
    """Set dotted config paths in every seed's resolved config under ``arm_dir``."""
    for path in sorted(arm_dir.glob("seed_*/config.resolved.yaml")):
        data = yaml.safe_load(path.read_text())
        for dotted, value in sets.items():
            _set(data, dotted, value)
        path.write_text(yaml.safe_dump(data, sort_keys=True))


def _copy(src: Path, dst: Path, overrides: dict[str, dict[str, Any]] | None = None) -> Path:
    """Copy an experiment directory; ``overrides`` maps an arm to the dotted config paths to set in its runs."""
    shutil.copytree(src, dst)
    for arm, sets in (overrides or {}).items():
        _edit_configs(dst / arm, sets)
    return dst


def _set_status(run_dir: Path, status: str, ended_tick: int | None) -> None:
    db = sqlite3.connect(str(run_dir / "world.db"))
    try:
        db.execute("UPDATE run SET status=?, ended_tick=?, ended_reason=?", (status, ended_tick, status))
        db.commit()
    finally:
        db.close()


def _truncate(run_dir: Path, keep_through: int, *, table: bool = True) -> None:
    """Keep the tick rows up to ``keep_through`` (and the day rows) in ``metrics.jsonl`` and, with ``table``, the table."""
    path = run_dir / "metrics.jsonl"
    if path.is_file():
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        kept = [r for r in rows if r.get("kind") == "day" or int(r["tick"]) <= keep_through]
        path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in kept))
    if table:
        db = sqlite3.connect(str(run_dir / "world.db"))
        try:
            db.execute("DELETE FROM metrics WHERE tick > ?", (keep_through,))
            db.commit()
        finally:
            db.close()


def _cap(run_dir: Path, tick: int) -> None:
    """Make a run look like it hit ``economy.total_cap_usd`` at ``tick``."""
    _set_status(run_dir, "capped_total", tick)
    _truncate(run_dir, tick)


def _load_script():
    spec = importlib.util.spec_from_file_location("compare_script", REPO / "scripts" / "compare.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- complete runs ------------------------------------------------------------------------------------------------
def test_complete_runs_are_compared_and_their_status_reported(mem_dir: Path) -> None:
    result = compare(mem_dir)
    assert result["arms"] == ["amnesic", "mem"] and result["dropped_incomplete"] == []
    assert result["runs"] == {arm: {str(s): {"status": "completed", "last_tick": LAST_TICK} for s in SEEDS} for arm in ("amnesic", "mem")}
    assert result["comparisons"][0]["primary"]["n"] == len(SEEDS)
    assert not any("incomplete" in w for w in result["warnings"])


def test_the_metrics_table_stands_in_for_a_missing_metrics_file(mem_dir: Path, tmp_path: Path) -> None:
    exp = _copy(mem_dir, tmp_path / "exp")
    (exp / "mem" / "seed_1" / "metrics.jsonl").unlink()
    state, why = run_state(exp / "mem" / "seed_1")
    assert state == {"status": "completed", "last_tick": LAST_TICK} and why is None
    assert compare(exp)["comparisons"][0]["primary"]["n"] == len(SEEDS)
    _truncate(exp / "mem" / "seed_1", 2)
    with pytest.raises(IncomparableRuns, match="mem seed 1: last tick row 2 of 6"):
        compare(exp)


# --- incomplete runs are refused ------------------------------------------------------------------------------------
def test_refuses_a_capped_total_run(mem_dir: Path, tmp_path: Path) -> None:
    exp = _copy(mem_dir, tmp_path / "exp")
    _cap(exp / "amnesic" / "seed_2", 5)
    with pytest.raises(IncomparableRuns, match=r"incomplete runs.*amnesic seed 2: run\.status 'capped_total'; last tick row 5 of 6"):
        compare(exp)


def test_refuses_a_capped_total_run_even_when_its_metrics_reach_the_last_tick(mem_dir: Path, tmp_path: Path) -> None:
    exp = _copy(mem_dir, tmp_path / "exp")
    _set_status(exp / "mem" / "seed_1", "capped_total", LAST_TICK)
    with pytest.raises(IncomparableRuns, match=r"mem seed 1: run\.status 'capped_total'"):
        compare(exp)


def test_refuses_a_truncated_run_whose_status_says_completed(mem_dir: Path, tmp_path: Path) -> None:
    exp = _copy(mem_dir, tmp_path / "exp")
    _truncate(exp / "mem" / "seed_1", 3, table=False)
    with pytest.raises(IncomparableRuns, match="mem seed 1: last tick row 3 of 6"):
        compare(exp)


def test_refuses_a_crashed_run_without_metrics(mem_dir: Path, tmp_path: Path) -> None:
    exp = _copy(mem_dir, tmp_path / "exp")
    run = exp / "amnesic" / "seed_1"
    _set_status(run, "running", None)
    (run / "metrics.jsonl").unlink()
    _truncate(run, 0)
    assert run_state(run)[0] == {"status": "running", "last_tick": None}
    with pytest.raises(IncomparableRuns, match=r"amnesic seed 1: run\.status 'running'; no tick rows in the metrics"):
        compare(exp)
    (run / "world.db").unlink()  # a run that died before its database was written
    assert run_state(run) == ({"status": None, "last_tick": None}, "no run row in world.db; no tick rows in the metrics")


def test_refuses_a_metrics_file_with_a_line_cut_short(mem_dir: Path, tmp_path: Path) -> None:
    exp = _copy(mem_dir, tmp_path / "exp")
    path = exp / "mem" / "seed_2" / "metrics.jsonl"
    path.write_text(path.read_text() + '{"tick": 7, "da')
    with pytest.raises(IncomparableRuns, match="mem seed 2: a metrics line does not parse"):
        compare(exp)


# --- allow_incomplete drops the pairs that touch an incomplete run --------------------------------------------------
def test_allow_incomplete_drops_the_pair_and_reports_it(mem_dir: Path, tmp_path: Path) -> None:
    clean = compare(mem_dir)
    exp = _copy(mem_dir, tmp_path / "exp")
    _cap(exp / "amnesic" / "seed_2", 5)
    result = compare(exp, allow_incomplete=True)
    assert result["dropped_incomplete"] == [{"arm": "amnesic", "seed": 2, "status": "capped_total", "last_tick": 5}]
    assert result["runs"]["amnesic"]["2"] == {"status": "capped_total", "last_tick": 5}
    assert result["runs"]["mem"]["2"] == {"status": "completed", "last_tick": LAST_TICK}
    primary = result["comparisons"][0]["primary"]
    assert primary["n"] == 1 and list(primary["per_seed"]) == ["1"] and primary["dropped"] == 0
    assert primary["per_seed"]["1"] == clean["comparisons"][0]["primary"]["per_seed"]["1"]
    assert all(s["n"] == 1 for s in result["comparisons"][0]["secondary"])
    assert "2" not in result["values"]["amnesic"] and "2" in result["values"]["mem"]
    assert result["seeds"] == list(SEEDS)  # the design's seed set; the statistics say what was used
    assert any(w.startswith("dropped incomplete run amnesic seed 2 (run.status 'capped_total'") for w in result["warnings"])
    assert "dropped incomplete run amnesic seed 2" in render(result)


def test_allow_incomplete_keeps_the_pairs_that_do_not_touch_the_run(mem_dir: Path, tmp_path: Path) -> None:
    exp = _copy(mem_dir, tmp_path / "exp")
    shutil.copytree(exp / "mem", exp / "mem2")  # a third arm identical to mem but for its name
    _edit_configs(exp / "mem2", {"experiment.arm": "mem2"})
    _truncate(exp / "amnesic" / "seed_2", 4)
    with pytest.raises(IncomparableRuns, match="amnesic seed 2"):
        compare(exp)
    result = compare(exp, allow_incomplete=True)
    n = {(c["arm_a"], c["arm_b"]): c["primary"]["n"] for c in result["comparisons"]}
    assert n == {("amnesic", "mem"): 1, ("amnesic", "mem2"): 1, ("mem", "mem2"): 2}
    assert result["dropped_incomplete"] == [{"arm": "amnesic", "seed": 2, "status": "completed", "last_tick": 4}]


def test_allow_incomplete_still_refuses_when_no_complete_pair_is_left(mem_dir: Path, tmp_path: Path) -> None:
    exp = _copy(mem_dir, tmp_path / "exp")
    for seed in SEEDS:
        _cap(exp / "mem" / f"seed_{seed}", 3)
    with pytest.raises(IncomparableRuns, match="no complete seed pair left for amnesic vs mem"):
        compare(exp, allow_incomplete=True)


# --- tier mutation ----------------------------------------------------------------------------------------------------
def test_refuses_tier_mutation_under_exp_ladder(ladder_dir: Path, tmp_path: Path) -> None:
    clean = compare(ladder_dir)
    assert clean["experiment"] == "exp_ladder" and clean["dropped_incomplete"] == []
    exp = _copy(ladder_dir, tmp_path / "exp", {arm: {"population.tier_mutation_prob": 0.1} for arm in ("ladder", "flat")})
    with pytest.raises(IncomparableRuns, match=r"exp_ladder: arm 'flat' has population\.tier_mutation_prob=0\.1; 0 is required"):
        compare(exp)


def test_refuses_tier_mutation_under_exp_gate(mem_dir: Path, tmp_path: Path) -> None:
    gate = {"experiment.name": "exp_gate", "economy.world_pricing": "real"}
    exp = _copy(mem_dir, tmp_path / "exp", {arm: {**gate, "population.tier_mutation_prob": 0.1} for arm in ("mem", "amnesic")})
    with pytest.raises(IncomparableRuns, match=r"exp_gate: arm 'amnesic' has population\.tier_mutation_prob=0\.1"):
        compare(exp)
    ok = _copy(mem_dir, tmp_path / "ok", {arm: {**gate, "population.tier_mutation_prob": 0.0} for arm in ("mem", "amnesic")})
    assert compare(ok)["experiment"] == "exp_gate"


# --- equalized pricing for the exp_mem family ---------------------------------------------------------------------------
def test_family_names_do_not_capture_exp_memory() -> None:
    assert in_family("exp_mem_e0", EQUALIZED_EXPERIMENTS) and in_family("exp_mem_dep_claude", EQUALIZED_EXPERIMENTS)
    assert in_family("exp_mem", EQUALIZED_EXPERIMENTS) and in_family("exp_tiers0", EQUALIZED_EXPERIMENTS)
    assert not in_family("exp_memory", EQUALIZED_EXPERIMENTS) and not in_family("exp_gate", EQUALIZED_EXPERIMENTS)


def test_refuses_real_pricing_under_exp_mem(mem_dir: Path, tmp_path: Path) -> None:
    assert compare(mem_dir)["experiment"] == "exp_mem_dep"
    real = {"economy.world_pricing": "real"}
    exp = _copy(mem_dir, tmp_path / "exp", {"mem": real, "amnesic": real})
    with pytest.raises(IncomparableRuns, match=r"exp_mem_dep: arm 'amnesic' has economy\.world_pricing='real'; 'equalized' is required"):
        compare(exp)
    # the DESIGN §17 graph-vs-vector experiment keeps real prices and is not held to the rule
    memory = _copy(mem_dir, tmp_path / "memory", {arm: {**real, "experiment.name": "exp_memory"} for arm in ("mem", "amnesic")})
    assert compare(memory)["experiment"] == "exp_memory"


def test_exp_mem_refuses_exam_and_maintenance_differences_outside_the_variables(mem_dir: Path, tmp_path: Path) -> None:
    exam = _copy(mem_dir, tmp_path / "exam", {"amnesic": {"memory.exam.questions_per_agent": 5}})
    with pytest.raises(IncomparableRuns, match=r"amnesic vs mem: memory\.exam\.questions_per_agent"):
        compare(exam)
    maint = _copy(mem_dir, tmp_path / "maint", {"mem": {"memory.maintenance.max_commands": 12}})
    with pytest.raises(IncomparableRuns, match=r"amnesic vs mem: memory\.maintenance\.max_commands"):
        compare(maint)


# --- the script ---------------------------------------------------------------------------------------------------------
def test_script_refuses_incomplete_runs_unless_allowed(mem_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _load_script()
    exp = _copy(mem_dir, tmp_path / "exp")
    _cap(exp / "mem" / "seed_1", 5)
    assert script.main([str(exp)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("refused: incomplete runs") and "capped_total" in err
    assert script.main([str(exp), "--allow-incomplete", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dropped_incomplete"] == [{"arm": "mem", "seed": 1, "status": "capped_total", "last_tick": 5}]
    assert out["comparisons"][0]["primary"]["n"] == 1
    assert script.main([str(exp), "--allow-incomplete"]) == 0
    assert "dropped incomplete run mem seed 1" in capsys.readouterr().out
