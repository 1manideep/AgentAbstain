"""Spread and gate outcomes (void/research/outcomes/spread.py): the censored time to 50% tracer reach, its censoring
indicator, and the mean balance at generation >= G; and the exp_spread / exp_gate configs that declare them.

Synthetic ``metrics.jsonl`` files (no database) pin the censoring: the first tick at half reach when there is one, the
last tick + 1 when there is none, None without the tracer, and the daily versions. A hand-built ``world.db`` pins
``balance_gen_ge`` at the end of the run and per day. Scripted runs only: one day of each of the five configs resolves
every outcome it declares to a number, except ``balance_gen_ge.4``, which is None in a one-day run (no agent is four
generations deep yet); and a ``compare`` over two seeds of the three exp_spread arms keeps every pair.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_experiments import CONFIGS, run_arm

from void.config import load_config
from void.db import Database
from void.research.analysis import (
    DAILY,
    computed_outcome_names,
    connect,
    end_of_run_outcome,
    time_to_50pct_tracer_reach,
)
from void.research.compare import compare
from void.research.outcomes import OUTCOMES, load_all
from void.research.outcomes.spread import DEFAULT_GENERATION, half_reach

REPO = Path(__file__).resolve().parents[1]
NAMES = ("time_to_50pct_tracer_reach_censored", "tracer_reach_censored", "balance_gen_ge")
SPREAD_ARMS = ("gossip", "inherit", "both")
GATE_ARMS = ("on", "off")
SPREAD_PRIMARY = "time_to_50pct_tracer_reach_censored"
SPREAD_SECONDARIES = ["tracer_adopters", "tracer_reach", "tracer_reach_censored", "gossip_transfers"]
GATE_PRIMARY = "gadget_use_failure_rate"
GATE_SECONDARIES = ["mean_balance", "balance_gen_ge.4", "max_generation", "gadgets.verified", "gadgets.rejected"]
# The documented None: no living agent of generation >= 4 after one day (spread.py's module docstring).
MAY_BE_NONE = {"balance_gen_ge.4"}
# gadget_use_failure_rate is None until the first gadget use (void/sim/metrics.py), and in most seeds nobody uses a
# gadget on day 1. Seed 18 is the first seed in 1-20 at which both exp_gate arms have a use within day 1.
SEED = 18
DAY1_TICKS = 12


def _daily(run_dir: Path, path: str, day: int) -> float | None:
    load_all()
    head, _, rest = path.partition(".")
    return DAILY[head](run_dir, rest or None, day)


def test_outcomes_are_registered_with_their_daily_versions() -> None:
    load_all()
    listed = computed_outcome_names()
    for name in NAMES:
        assert name in OUTCOMES and name in listed and name in DAILY, name
        assert OUTCOMES[name].__module__ == "void.research.outcomes.spread"
    assert DEFAULT_GENERATION == 4


# --- censoring, on synthetic metrics rows ------------------------------------------------------------------------------
TPD = 4


def _row(tick: int, population: int, reach: int | None) -> dict[str, Any]:
    row: dict[str, Any] = {"tick": tick, "day": (tick - 1) // TPD + 1, "population": population}
    if reach is not None:
        row["tracer_reach"] = reach
    return row


def _write_metrics(run_dir: Path, rows: list[dict[str, Any]], *, shuffle: bool = True) -> Path:
    """A run directory holding only ``metrics.jsonl``: the tick rows (written in reverse, the reader sorts them) plus
    a day row per day that claims full reach at a far tick, which the outcomes must ignore."""
    run_dir.mkdir(parents=True)
    days = sorted({int(r["day"]) for r in rows})
    day_rows = [{"kind": "day", "day": d, "end_tick": 999, "tracer_reach": 100, "population_end": 100} for d in days]
    ordered = list(reversed(rows)) if shuffle else rows
    (run_dir / "metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [*day_rows, *ordered]))
    return run_dir


# 8 ticks, 2 days: half of six is reached at tick 6 (3 of 6, exactly 0.5) and lost again at tick 8
REACHED = [_row(1, 6, 0), _row(2, 6, 1), _row(3, 0, 0), _row(4, 7, 3), _row(5, 6, 2), _row(6, 6, 3), _row(7, 5, 4),
           _row(8, 6, 2)]
# 8 ticks: never past 2 of 6; tick 3 has nobody alive (0 / 0 is not half)
NEVER = [_row(1, 6, 0), _row(2, 6, 1), _row(3, 0, 0), _row(4, 6, 2), _row(5, 6, 2), _row(6, 7, 3), _row(7, 6, 2),
         _row(8, 6, 2)]
# the tracer disabled: no row carries tracer_reach
NO_TRACER = [_row(t, 6, None) for t in range(1, 9)]


def test_half_reach_on_rows() -> None:
    assert half_reach(REACHED) == (6.0, False)
    assert half_reach(NEVER) == (9.0, True)        # the last tick + 1
    assert half_reach(NEVER[:5]) == (6.0, True)    # censored at whatever tick the rows end
    assert half_reach(NO_TRACER) is None
    assert half_reach([]) is None
    # one row with tracer_reach is enough to say the tracer was on (rows before the plant may lack it)
    assert half_reach([_row(1, 6, None), _row(2, 6, 1)]) == (3.0, True)


def test_a_reached_run_has_the_uncensored_time(tmp_path: Path) -> None:
    run = _write_metrics(tmp_path / "reached", REACHED)
    assert end_of_run_outcome(run, "time_to_50pct_tracer_reach_censored") == 6.0
    assert end_of_run_outcome(run, "tracer_reach_censored") == 0.0
    assert time_to_50pct_tracer_reach(run) == 6.0  # the same reach test as the built-in
    assert end_of_run_outcome(run, "time_to_50pct_tracer_reach") == 6.0
    # daily: censored at the end of day 1 (tick 4 + 1), reached on day 2
    assert [_daily(run, SPREAD_PRIMARY, d) for d in (1, 2)] == [5.0, 6.0]
    assert [_daily(run, "tracer_reach_censored", d) for d in (1, 2)] == [1.0, 0.0]


def test_a_run_that_never_reaches_half_is_censored_at_the_last_tick_plus_one(tmp_path: Path) -> None:
    run = _write_metrics(tmp_path / "never", NEVER)
    assert end_of_run_outcome(run, "time_to_50pct_tracer_reach_censored") == 9.0
    assert end_of_run_outcome(run, "tracer_reach_censored") == 1.0
    assert end_of_run_outcome(run, "time_to_50pct_tracer_reach") is None  # what compare used to drop
    assert [_daily(run, SPREAD_PRIMARY, d) for d in (1, 2)] == [5.0, 9.0]
    assert [_daily(run, "tracer_reach_censored", d) for d in (1, 2)] == [1.0, 1.0]
    assert _daily(run, SPREAD_PRIMARY, 0) is None  # no rows by then


def test_without_the_tracer_both_are_none(tmp_path: Path) -> None:
    run = _write_metrics(tmp_path / "off", NO_TRACER)
    for name in ("time_to_50pct_tracer_reach_censored", "tracer_reach_censored"):
        assert end_of_run_outcome(run, name) is None
        assert _daily(run, name, 2) is None


def test_the_tracer_outcomes_take_no_argument(tmp_path: Path) -> None:
    run = _write_metrics(tmp_path / "reached", REACHED, shuffle=False)
    for path in ("time_to_50pct_tracer_reach_censored.alpha", "tracer_reach_censored.1"):
        with pytest.raises(ValueError, match="takes no argument"):
            end_of_run_outcome(run, path)


# --- balance at generation >= G, on hand-built agents ----------------------------------------------------------------
GEN_TPD = 4
LAST_TICK = 16
# (agent_id, name, parent_id, generation, born_tick, died_tick, status, end balance in micro-dollars)
AGENTS = (
    ("f1", "Fa", None, 0, 0, None, "alive", 1_000_000),
    ("c1", "Fa-1", "f1", 1, 2, None, "alive", 300_000),
    ("g2", "Fa-1-1", "c1", 2, 4, None, "alive", 200_000),
    ("g3", "Fa-1-1-1", "g2", 3, 6, 9, "bankrupt", 0),
    ("g4a", "Fa-1-1-1-1", "g3", 4, 8, None, "alive", 400_000),
    ("g4b", "Fa-1-1-1-2", "g3", 4, 9, 14, "archived", 0),     # alive at the end of day 3, dead by the end
    ("g5", "Fa-1-1-1-1-1", "g4a", 5, 13, None, "alive", 100_000),  # born on day 4
)
# (entry, tick, wallet, delta, kind): consistent with the end balances above
LEDGER = (
    ("l01", 0, "f1", 1_000_000, "starting_balance"),
    ("l02", 2, "c1", 300_000, "endowment_in"),
    ("l03", 4, "g2", 200_000, "endowment_in"),
    ("l04", 6, "g3", 100_000, "endowment_in"), ("l05", 9, "g3", -100_000, "llm_call"),
    ("l06", 8, "g4a", 300_000, "endowment_in"), ("l07", 15, "g4a", 100_000, "forage"),
    ("l08", 9, "g4b", 200_000, "endowment_in"), ("l09", 11, "g4b", -50_000, "llm_call"),
    ("l10", 14, "g4b", -150_000, "estate_out"), ("l11", 14, "spawn_pool", 150_000, "estate_in"),
    ("l12", 13, "g5", 100_000, "endowment_in"),
)


@pytest.fixture
def generations(tmp_path: Path) -> Path:
    cfg = load_config(REPO / "configs" / "base.yaml", {"run": {"days": 4, "ticks_per_day": GEN_TPD}})
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
        db.kv_set("tick", LAST_TICK)
    finally:
        db.close()
    return tmp_path


def test_balance_gen_ge_over_the_living_at_the_end(generations: Path) -> None:
    # generation >= 4 alive at the end: g4a $0.40 and g5 $0.10 (g4b died)
    assert end_of_run_outcome(generations, "balance_gen_ge") == pytest.approx(0.25, abs=1e-12)  # G defaults to 4
    assert end_of_run_outcome(generations, "balance_gen_ge.4") == pytest.approx(0.25, abs=1e-12)
    assert end_of_run_outcome(generations, "balance_gen_ge.5") == pytest.approx(0.10, abs=1e-12)
    assert end_of_run_outcome(generations, "balance_gen_ge.6") is None  # nobody that deep
    # G = 0 is every living agent: (1.00 + 0.30 + 0.20 + 0.40 + 0.10) / 5, the dead bankrupt g3 left out
    assert end_of_run_outcome(generations, "balance_gen_ge.0") == pytest.approx(0.40, abs=1e-12)
    assert end_of_run_outcome(generations, "balance_gen_ge.3") == pytest.approx(0.25, abs=1e-12)
    for bad in ("balance_gen_ge.four", "balance_gen_ge.4.5"):
        with pytest.raises(ValueError, match="generation number"):
            end_of_run_outcome(generations, bad)


def test_balance_gen_ge_per_day(generations: Path) -> None:
    # day 1 (tick 4): nobody of generation 4 is born yet
    assert _daily(generations, "balance_gen_ge.4", 1) is None
    # day 2 (tick 8): g4a, born at tick 8 with $0.30
    assert _daily(generations, "balance_gen_ge.4", 2) == pytest.approx(0.30, abs=1e-12)
    # day 3 (tick 12): g4a $0.30 and g4b $0.15 (alive until tick 14); g5 is not born yet
    assert _daily(generations, "balance_gen_ge.4", 3) == pytest.approx(0.225, abs=1e-12)
    # the last day is the end of the run
    assert _daily(generations, "balance_gen_ge.4", 4) == end_of_run_outcome(generations, "balance_gen_ge.4")
    assert _daily(generations, "balance_gen_ge", 4) == end_of_run_outcome(generations, "balance_gen_ge")


# --- the configs and one scripted day of each ------------------------------------------------------------------------
def test_configs_declare_the_outcomes() -> None:
    for arm in SPREAD_ARMS:
        exp = load_config(CONFIGS / f"exp_spread_{arm}.yaml").experiment
        assert (exp.primary_outcome, exp.secondary_outcomes) == (SPREAD_PRIMARY, SPREAD_SECONDARIES), arm
    for arm in GATE_ARMS:
        exp = load_config(CONFIGS / f"exp_gate_{arm}.yaml").experiment
        assert (exp.primary_outcome, exp.secondary_outcomes) == (GATE_PRIMARY, GATE_SECONDARIES), arm


@pytest.fixture(scope="module")
def one_day(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """One scripted day of every arm: ``spread`` holds the three exp_spread arms at seeds 18 and 19 (for compare),
    ``gate`` the two exp_gate arms at seed 18."""
    spread = tmp_path_factory.mktemp("exp_spread")
    gate = tmp_path_factory.mktemp("exp_gate")
    for arm in SPREAD_ARMS:
        run_arm(CONFIGS / f"exp_spread_{arm}.yaml", spread, (SEED, SEED + 1), run={"days": 1})
    for arm in GATE_ARMS:
        run_arm(CONFIGS / f"exp_gate_{arm}.yaml", gate, (SEED,), run={"days": 1})
    return {"spread": spread, "gate": gate}


def _runs(one_day: dict[str, Path]) -> list[tuple[str, Path]]:
    return ([(f"exp_spread_{a}", one_day["spread"] / a / f"seed_{SEED}") for a in SPREAD_ARMS]
            + [(f"exp_gate_{a}", one_day["gate"] / a / f"seed_{SEED}") for a in GATE_ARMS])


def test_one_day_of_each_config_resolves_every_declared_outcome(one_day: dict[str, Path]) -> None:
    for name, run in _runs(one_day):
        exp = load_config(CONFIGS / f"{name}.yaml").experiment
        for path in [exp.primary_outcome, *exp.secondary_outcomes]:
            value = end_of_run_outcome(run, path)
            if path in MAY_BE_NONE:
                with connect(run) as db:
                    deep = int(db.execute("SELECT COUNT(*) AS n FROM agents WHERE status='alive' AND generation >= ?",
                                          (DEFAULT_GENERATION,)).fetchone()["n"])
                assert (value is None) == (deep == 0), (name, path, value, deep)
                assert value is None, (name, path, "one day is too short for generation 4")
            else:
                assert isinstance(value, float), (name, path, value)


def test_one_day_of_exp_spread_is_censored_or_reached_consistently(one_day: dict[str, Path]) -> None:
    for name, run in _runs(one_day)[:3]:
        t = end_of_run_outcome(run, SPREAD_PRIMARY)
        censored = end_of_run_outcome(run, "tracer_reach_censored")
        raw = end_of_run_outcome(run, "time_to_50pct_tracer_reach")
        if censored == 1.0:
            assert t == DAY1_TICKS + 1 and raw is None, name
        else:
            assert censored == 0.0 and t == raw and t is not None and 1 <= t <= DAY1_TICKS, name
        assert _daily(run, SPREAD_PRIMARY, 1) == t and _daily(run, "tracer_reach_censored", 1) == censored
    for name, run in _runs(one_day)[3:]:  # no tracer in exp_gate
        assert end_of_run_outcome(run, SPREAD_PRIMARY) is None, name


def test_compare_keeps_the_censored_pairs(one_day: dict[str, Path]) -> None:
    result = compare(one_day["spread"])
    assert result["experiment"] == "exp_spread" and result["seeds"] == [SEED, SEED + 1]
    assert result["primary"] == SPREAD_PRIMARY and result["secondaries"] == SPREAD_SECONDARIES
    assert len(result["comparisons"]) == 3 and not result["dropped_incomplete"]
    for comp in result["comparisons"]:
        assert comp["primary"]["n"] == 2 and comp["primary"]["dropped"] == 0
        assert [s["path"] for s in comp["secondary"]] == SPREAD_SECONDARIES
    for arm, by_seed in result["values"].items():
        for seed, values in by_seed.items():
            assert all(isinstance(values[p], float) for p in [SPREAD_PRIMARY, *SPREAD_SECONDARIES]), (arm, seed, values)
    # the daily curve of the primary comes from the registered daily version
    assert set(result["curves"]) == set(SPREAD_ARMS) and all(set(c) == {"1"} for c in result["curves"].values())
