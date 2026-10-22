"""Per-category practice shares and the replicator-mutator fit for policy lineages (MEMORY_EVOLUTION 7.1, 7.2).

A hand-built ``world.db`` with planted lineages whose transmission kernel and fitness are known (type T1 parents have
two children each, all T1; type T2 parents one each, a quarter of which turn T1) must give back Q and f within the
smoothing's tolerance and exactly by the documented formulas, a cohort fit that beats the no-change baseline, and
category shares that sum to 1. Small worlds pin which births count as transitions, the threshold below which every
outcome is None and the codes file the fit shares with ``cluster_entropy``. A scripted world with births, inherited
policies and the kernel's mutation at birth resolves every new outcome through ``end_of_run_outcome``, the same on a
second run.
"""

from __future__ import annotations

import asyncio
import csv
import math
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, SMOKE, deep_merge

from void.config import load_config
from void.db import Database
from void.research.analysis import DAILY, computed_outcome_names, end_of_run_outcome
from void.research.outcomes import OUTCOMES, load_all
from void.research.outcomes import practices as pr
from void.research.outcomes import transmission_fit as tf
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
A = tf.ALPHA
K = len(pr.CODES)
FLAGS = ("", ".dominant", ".keyword", ".dominant.keyword", ".keyword.dominant")

# Two practice texts the keyword coder puts in T1 and T2 (checked below). T1_TEXT has a T4 bullet too, so the bare
# shares (bullets per agent) and the dominant ones (agents by type) differ.
T1_TEXT = "# Places\n\n- Keep a hub note per node.\n- Rebuild the index nightly.\n- Delete stale notes."
T2_TEXT = "# Short\n\n- Write a summary each night.\n- Merge duplicate notes."
TEXTS = {"T1": T1_TEXT, "T2": T2_TEXT}


def _h(shares: Iterable[float]) -> float:
    return -sum(p * math.log2(p) for p in shares if p > 0)


def _shares(run_dir: Path, flags: str) -> list[float]:
    """``practice_share.<T><flags>`` for T1 to T9, each a number."""
    out = [end_of_run_outcome(run_dir, f"practice_share.{c}{flags}") for c in pr.CODES]
    assert all(s is not None for s in out), flags
    return [float(s) for s in out if s is not None]


# --- building worlds ---------------------------------------------------------------------------------------------
Agent = tuple[str, str | None, int, str]                            # (agent_id, parent_id, generation, status)
Version = tuple[str, int, str, str, str | None]                    # (agent_id, version, text, source, source_agent_id)


def _build(run_dir: Path, agents: Sequence[Agent], versions: Sequence[Version]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg = load_config(REPO / "configs" / "base.yaml", {"run": {"days": 3, "ticks_per_day": 4}})
    (run_dir / "config.resolved.yaml").write_text(cfg.to_yaml())
    db = Database(run_dir / "world.db")
    try:
        for aid, parent, gen, status in agents:
            born = 0 if parent is None else gen * 4
            died = None if status == "alive" else 12
            db.execute("INSERT INTO agents(agent_id, name, parent_id, model_tier, balance, personality_seed, memory_path, "
                       "generation, status, x, y, entropy_budget, born_tick, died_tick, created_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, aid.upper(), parent, "scripted", 0, "{}", "", gen, status, 0, 0, 1, born, died, "t"))
        for aid, version, text, source, src in versions:
            db.execute("INSERT INTO policy_versions(agent_id, version, tick, text, parent_version, source, source_agent_id) "
                       "VALUES(?,?,?,?,?,?,?)", (aid, version, version, text, version - 1 or None, source, src))
        db.kv_set("tick", 12)
    finally:
        db.close()
    return run_dir


class Planter:
    """Plants lineages: founders with a seed text, children that inherit the parent's text (``inherited`` naming the
    parent) and, when their type differs, rewrite it (a ``mutated`` version at birth)."""

    def __init__(self) -> None:
        self.agents: list[Agent] = []
        self.versions: list[Version] = []
        self.type: dict[str, str] = {}
        self.gen: dict[str, int] = {}

    def founder(self, aid: str, kind: str, *, status: str = "bankrupt") -> str:
        self.agents.append((aid, None, 0, status))
        self.versions.append((aid, 1, TEXTS[kind], "seed", None))
        self.type[aid], self.gen[aid] = kind, 0
        return aid

    def child(self, aid: str, parent: str, kind: str, *, status: str = "alive") -> str:
        gen = self.gen[parent] + 1
        self.agents.append((aid, parent, gen, status))
        self.versions.append((aid, 1, TEXTS[self.type[parent]], "inherited", parent))
        if kind != self.type[parent]:
            self.versions.append((aid, 2, TEXTS[kind], "mutated", None))
        self.type[aid], self.gen[aid] = kind, gen
        return aid

    def build(self, run_dir: Path) -> Path:
        return _build(run_dir, self.agents, self.versions)


def _planted() -> Planter:
    """Generation 0: 4 T1 and 16 T2 founders (dead by the end). Every T1 agent of generations 0 and 1 has two T1
    children; every T2 agent one child, and of each four consecutive T2 parents the first one's child is T1.
    Generation 1: 8 + 4 = 12 T1, 12 T2. Generation 2 (no children): 24 + 3 = 27 T1, 9 T2. Generations 1 and 2 live.
    Transitions: T1 -> T1 32, T2 -> T1 7, T2 -> T2 21 (60 in all)."""
    p = Planter()
    cohort = [p.founder(f"g0_a{k}", "T1") for k in range(4)] + [p.founder(f"g0_b{k}", "T2") for k in range(16)]
    for g in (1, 2):
        nxt: list[str] = []
        t2_seen = 0
        for parent in cohort:
            if p.type[parent] == "T1":
                nxt += [p.child(f"g{g}_{parent}_{k}", parent, "T1") for k in range(2)]
            else:
                kind = "T1" if t2_seen % 4 == 0 else "T2"
                t2_seen += 1
                nxt.append(p.child(f"g{g}_{parent}", parent, kind))
        cohort = nxt
    return p


@pytest.fixture
def planted(tmp_path: Path) -> Path:
    return _planted().build(tmp_path / "planted")


# --- the registry ------------------------------------------------------------------------------------------------
def test_outcomes_are_registered_end_of_run_only() -> None:
    load_all()
    listed = computed_outcome_names()
    assert OUTCOMES["practice_share"] is pr.practice_share and OUTCOMES["replicator_mutator"] is tf.replicator_mutator
    for name in ("practice_share", "replicator_mutator"):
        assert name in listed and name not in DAILY, name
    paths = tf.outcome_paths()
    assert len(paths) == len(tf.STATISTICS) + K + K * K and len(set(paths)) == len(paths)
    assert all(p.endswith(".keyword") for p in tf.outcome_paths(keyword=True))


def test_the_planted_texts_code_as_intended() -> None:
    assert [pr.keyword_code(b) for b in pr.bullets(T1_TEXT)] == ["T1", "T1", "T4"]
    assert [pr.keyword_code(b) for b in pr.bullets(T2_TEXT)] == ["T2", "T2"]
    assert pr.dominant_code(pr.code_profile(T1_TEXT, {})) == "T1"
    assert pr.dominant_code(pr.code_profile(T2_TEXT, {})) == "T2"
    assert pr.dominant_code({"T4": 1, "T2": 1}) == "T2"  # a tie goes to the lower code number


# --- the planted world: Q, f and the fit ---------------------------------------------------------------------------
def test_planted_kernel_is_recovered(planted: Path) -> None:
    fit = tf.fit_table(planted)
    assert fit is not None and fit["fitted"] and fit["n_transitions"] == 60
    assert end_of_run_outcome(planted, "replicator_mutator.n_transitions") == 60.0
    assert {j: {i: n for i, n in row.items() if n} for j, row in fit["counts"].items() if any(row.values())} == {
        "T1": {"T1": 32}, "T2": {"T1": 7, "T2": 21}}
    q = {(j, i): end_of_run_outcome(planted, f"replicator_mutator.q.{j}.{i}") for j in pr.CODES for i in pr.CODES}
    # exactly the documented add-alpha estimate, alpha = 1/9 per cell ...
    assert q["T1", "T1"] == pytest.approx((32 + A) / (32 + K * A))
    assert q["T2", "T1"] == pytest.approx((7 + A) / (28 + K * A))
    assert q["T2", "T2"] == pytest.approx((21 + A) / (28 + K * A))
    assert q["T1", "T5"] == pytest.approx(A / (32 + K * A))
    for j in ("T1", "T2"):
        assert sum(q[j, i] for i in pr.CODES) == pytest.approx(1.0)
    # ... which is the planted kernel within the smoothing's pull
    truth = {("T1", "T1"): 1.0, ("T2", "T1"): 0.25, ("T2", "T2"): 0.75}
    for j in ("T1", "T2"):
        for i in pr.CODES:
            assert q[j, i] == pytest.approx(truth.get((j, i), 0.0), abs=0.03), (j, i)
    # rows without a parent of that type are None, not invented
    for j in pr.CODES[2:]:
        assert all(q[j, i] is None for i in pr.CODES), j
        assert fit["q"][j] is None


def test_planted_fitness_and_selection(planted: Path) -> None:
    # pool: generations 0 and 1 (generation 2 is the deepest): 4 + 12 T1 and 16 + 12 T2 agents; 60 children
    mean = 60 / 44
    f = {i: end_of_run_outcome(planted, f"replicator_mutator.f.{i}") for i in pr.CODES}
    assert f["T1"] == pytest.approx((32 / 16) / mean)
    assert f["T2"] == pytest.approx((28 / 28) / mean)
    assert all(f[i] is None for i in pr.CODES[2:])
    assert f["T1"] / f["T2"] == pytest.approx(2.0)                      # T1 parents have twice the children
    assert (16 * f["T1"] + 28 * f["T2"]) / 44 == pytest.approx(1.0)     # the pool's weighted mean is 1
    assert end_of_run_outcome(planted, "replicator_mutator.selection_spread") == pytest.approx(2.0)
    fit = tf.fit_table(planted)
    assert fit is not None and fit["pool"]["T1"] == 16 and fit["pool"]["T2"] == 28
    assert fit["parents"]["T1"] == 16 and fit["parents"]["T2"] == 28


def test_planted_mutation_rate(planted: Path) -> None:
    q11, q22 = (32 + A) / (32 + K * A), (21 + A) / (28 + K * A)
    got = end_of_run_outcome(planted, "replicator_mutator.mutation_rate")
    assert got == pytest.approx(1 - (32 * q11 + 28 * q22) / 60)
    assert got == pytest.approx(7 / 60, abs=0.03)      # the raw share of children that changed type


def _predict(x: dict[str, float], f: dict[str, float], q: dict[str, dict[str, float]]) -> dict[str, float]:
    w = {j: x.get(j, 0.0) * f[j] for j in f}
    total = sum(w.values())
    return {i: sum(w[j] * q[j][i] for j in w) / total for i in pr.CODES}


def _tvd(p: dict[str, float], q: dict[str, float]) -> float:
    return 0.5 * sum(abs(p.get(c, 0.0) - q.get(c, 0.0)) for c in pr.CODES)


def test_planted_cohort_fit_beats_the_no_change_baseline(planted: Path) -> None:
    mean = 60 / 44
    f = {"T1": 2 / mean, "T2": 1 / mean}
    q = {"T1": {i: ((32 if i == "T1" else 0) + A) / (32 + K * A) for i in pr.CODES},
         "T2": {i: ({"T1": 7, "T2": 21}.get(i, 0) + A) / (28 + K * A) for i in pr.CODES}}
    x0, x1, x2 = {"T1": 0.2, "T2": 0.8}, {"T1": 0.5, "T2": 0.5}, {"T1": 27 / 36, "T2": 9 / 36}
    fits = [_tvd(_predict(x0, f, q), x1), _tvd(_predict(x1, f, q), x2)]
    fit_tvd = end_of_run_outcome(planted, "replicator_mutator.fit_tvd")
    baseline = end_of_run_outcome(planted, "replicator_mutator.baseline_tvd")
    assert fit_tvd == pytest.approx(sum(fits) / 2)
    assert baseline == pytest.approx((0.3 + 0.25) / 2)
    assert fit_tvd is not None and baseline is not None and fit_tvd < baseline / 5
    table = tf.fit_table(planted)
    assert table is not None and [c["generation"] for c in table["cohorts"]] == [0, 1]
    assert table["cohorts"][0]["observed"]["T1"] == pytest.approx(0.5)
    assert sum(table["cohorts"][1]["predicted"].values()) == pytest.approx(1.0)


def test_planted_practice_shares_sum_to_one(planted: Path) -> None:
    # living: generations 1 and 2, 39 T1 agents (bullets T1, T1, T4) and 21 T2 agents (T2, T2)
    bare = {c: end_of_run_outcome(planted, f"practice_share.{c}") for c in pr.CODES}
    assert bare["T1"] == pytest.approx(39 / 60 * 2 / 3) and bare["T4"] == pytest.approx(39 / 60 / 3)
    assert bare["T2"] == pytest.approx(21 / 60)
    dominant = {c: end_of_run_outcome(planted, f"practice_share.{c}.dominant") for c in pr.CODES}
    assert dominant["T1"] == pytest.approx(39 / 60) and dominant["T4"] == 0.0
    for flags in FLAGS:
        shares = _shares(planted, flags)
        assert sum(shares) == pytest.approx(1.0), flags
        assert _h(shares) == pytest.approx(end_of_run_outcome(planted, f"cluster_entropy{flags}")), flags


# --- which births are transitions -----------------------------------------------------------------------------------
def test_only_policy_carrying_births_between_typed_agents_count(tmp_path: Path) -> None:
    agents: list[Agent] = [
        ("p", None, 0, "alive"), ("q", None, 0, "alive"), ("blank", None, 0, "alive"), ("nopolicy", None, 0, "alive"),
        ("c_inh", "p", 1, "alive"),        # inherited from p: counts
        ("c_mut", "p", 1, "alive"),        # inherited, then mutated at birth into T2: counts, T1 -> T2
        ("c_adapt", "q", 1, "alive"),      # first version adapted from its parent (an E3a-style seeding): counts
        ("c_seed", "p", 1, "alive"),       # got a seed (inheritance off): not a transition
        ("c_other", "p", 1, "alive"),      # first version copied from q, not from its parent: not a transition
        ("c_blank", "blank", 1, "alive"),  # its parent's policy has no bullet (no type): not a transition
        ("c_none", "nopolicy", 1, "alive"),  # its parent has no policy at all
        ("c_empty", "p", 1, "alive"),      # inherited, then emptied: no type of its own
    ]
    versions: list[Version] = [
        ("p", 1, T1_TEXT, "seed", None), ("q", 1, T2_TEXT, "seed", None), ("blank", 1, "", "seed", None),
        ("c_inh", 1, T1_TEXT, "inherited", "p"),
        ("c_mut", 1, T1_TEXT, "inherited", "p"), ("c_mut", 2, T2_TEXT, "mutated", None),
        ("c_adapt", 1, T2_TEXT + "\n- Delete stale notes.", "adapted", "q"),
        ("c_seed", 1, T1_TEXT, "seed", None),
        ("c_other", 1, T2_TEXT, "inherited", "q"),
        ("c_blank", 1, T2_TEXT, "inherited", "blank"),
        ("c_none", 1, T2_TEXT, "seed", None),
        ("c_empty", 1, T1_TEXT, "inherited", "p"), ("c_empty", 2, "# Nothing", "self_edit", None),
    ]
    run = _build(tmp_path / "filters", agents, versions)
    fit = tf.fit_table(run)
    assert fit is not None
    assert [(t["child_id"], t["parent_type"], t["child_type"]) for t in fit["transitions"]] == [
        ("c_adapt", "T2", "T2"), ("c_inh", "T1", "T1"), ("c_mut", "T1", "T2")]
    assert not fit["fitted"]
    for path in tf.outcome_paths():
        assert end_of_run_outcome(run, path) is None, path


def _chain(n: int) -> Planter:
    """``n`` founders alternating T1 and T2, each with one child of its own type."""
    p = Planter()
    for k in range(n):
        kind = "T1" if k % 2 == 0 else "T2"
        p.child(f"c{k}", p.founder(f"f{k}", kind), kind)
    return p


def test_below_the_threshold_every_outcome_is_none(tmp_path: Path) -> None:
    short = _chain(tf.MIN_TRANSITIONS - 1).build(tmp_path / "short")
    fit = tf.fit_table(short)
    assert fit is not None and fit["n_transitions"] == tf.MIN_TRANSITIONS - 1 and not fit["fitted"]
    for path in tf.outcome_paths() + tf.outcome_paths(keyword=True):
        assert end_of_run_outcome(short, path) is None, path
    enough = _chain(tf.MIN_TRANSITIONS).build(tmp_path / "enough")
    assert end_of_run_outcome(enough, "replicator_mutator.n_transitions") == float(tf.MIN_TRANSITIONS)
    assert end_of_run_outcome(enough, "replicator_mutator.mutation_rate") == pytest.approx(
        1 - (5 + A) / (5 + K * A))
    assert end_of_run_outcome(enough, "replicator_mutator.fit_tvd") is not None
    # every parent has one child: equal fitness, and 5 parents of each type qualify
    assert end_of_run_outcome(enough, "replicator_mutator.selection_spread") == pytest.approx(1.0)


def test_selection_spread_needs_two_types_with_enough_parents(tmp_path: Path) -> None:
    p = Planter()
    for k in range(10):
        p.child(f"c{k}", p.founder(f"f{k}", "T1"), "T1")
    p.child("x0", p.founder("t2_0", "T2"), "T2")
    p.child("x1", p.founder("t2_1", "T2"), "T2")      # two T2 parents: below MIN_PARENTS
    run = p.build(tmp_path / "spread")
    assert end_of_run_outcome(run, "replicator_mutator.n_transitions") == 12.0
    assert end_of_run_outcome(run, "replicator_mutator.selection_spread") is None
    assert end_of_run_outcome(run, "replicator_mutator.f.T2") == pytest.approx(1.0)


def test_a_run_without_births_or_policies_gives_none(tmp_path: Path) -> None:
    p = Planter()
    for k in range(12):
        p.founder(f"f{k}", "T1", status="alive")
    lonely = p.build(tmp_path / "lonely")
    fit = tf.fit_table(lonely)
    assert fit is not None and fit["n_transitions"] == 0 and fit["mutation_rate"] is None
    assert all(v is None for v in fit["f"].values()) and fit["cohorts"] == []
    for path in tf.outcome_paths():
        assert end_of_run_outcome(lonely, path) is None, path
    assert end_of_run_outcome(lonely, "practice_share.T1.dominant") == 1.0
    db = Database(lonely / "world.db")
    try:
        db.execute("DROP TABLE policy_versions")
    finally:
        db.close()
    assert tf.fit_table(lonely) is None
    for path in tf.outcome_paths()[:6] + [f"practice_share.{c}{flags}" for c in pr.CODES for flags in FLAGS]:
        assert end_of_run_outcome(lonely, path) is None, path
    assert end_of_run_outcome(lonely, "cluster_entropy") is None


def test_the_fit_reads_the_codes_file_cluster_entropy_reads(planted: Path) -> None:
    with open(planted / pr.CODES_FILE, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["text", "code"])
        w.writerows([("Write a summary each night.", "T3"), ("merge duplicate notes", "3")])
    # the T2 agents are T3 by the file, T2 by the keyword coder
    assert end_of_run_outcome(planted, "replicator_mutator.q.T3.T3") == pytest.approx((21 + A) / (28 + K * A))
    assert end_of_run_outcome(planted, "replicator_mutator.q.T2.T2") is None
    assert end_of_run_outcome(planted, "replicator_mutator.q.T2.T2.keyword") == pytest.approx((21 + A) / (28 + K * A))
    assert end_of_run_outcome(planted, "practice_share.T3.dominant") == pytest.approx(21 / 60)
    assert end_of_run_outcome(planted, "practice_share.T3.dominant.keyword") == 0.0
    for flags in FLAGS:
        shares = _shares(planted, flags)
        assert sum(shares) == pytest.approx(1.0), flags
        assert _h(shares) == pytest.approx(end_of_run_outcome(planted, f"cluster_entropy{flags}")), flags


@pytest.mark.parametrize("path", [
    "replicator_mutator", "replicator_mutator.fit", "replicator_mutator.keyword", "replicator_mutator.q.T1",
    "replicator_mutator.q.T1.T10", "replicator_mutator.f.T0", "replicator_mutator.f.T1.T2",
    "replicator_mutator.fit_tvd.dominant", "replicator_mutator.keyword.fit_tvd", "replicator_mutator.n_transitions.",
    "practice_share", "practice_share.T10", "practice_share.t1", "practice_share.dominant", "practice_share.T1.mean",
    "practice_share.T1.dominant.", "practice_share.T1.",
])
def test_unknown_arguments_fail_loudly(planted: Path, path: str) -> None:
    with pytest.raises(ValueError):
        end_of_run_outcome(planted, path)


# --- a scripted world ------------------------------------------------------------------------------------------------
# The smoke world with the policy file on: the ten diverse seeds (the six founders get six of them), the kernel's
# mutation at birth, and enough income and room for two generations of children within five days. The shipped worlds
# named for this do not serve: E3a's generation worlds have no births (reproduction off; its children are roster
# agents of the next run) and exp_spread_inherit runs without the policy file.
SCRIPTED: dict[str, Any] = {
    "run": {"days": 5, "ticks_per_day": 12, "name": "replicator_scripted"},
    "world": {"forage_yield_usd": 0.05},
    "population": {"cap": 40},
    "memory": {"policy": {"enabled": True, "seed": "diverse", "mutate_at_birth": True}},
}


def _all_paths() -> list[str]:
    shares = [f"practice_share.{c}{flags}" for c in pr.CODES for flags in FLAGS]
    return shares + tf.outcome_paths() + tf.outcome_paths(keyword=True)


@pytest.fixture(scope="module")
def scripted_runs(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[Path, dict[str, float | None]]]:
    out = []
    for k in range(2):
        run_dir = tmp_path_factory.mktemp(f"replicator_{k}") / "run"
        sim = Simulation(load_config(SMOKE, deep_merge(NO_PROVIDER_TIERS, SCRIPTED)), run_dir)
        asyncio.run(sim.run())
        sim.db.close()
        out.append((run_dir, {path: end_of_run_outcome(run_dir, path) for path in _all_paths()}))
    return out


def test_scripted_world_has_births_with_inherited_policies(scripted_runs: list[tuple[Path, dict[str, Any]]]) -> None:
    run_dir, values = scripted_runs[0]
    fit = tf.fit_table(run_dir)
    assert fit is not None and fit["fitted"]
    assert values["replicator_mutator.n_transitions"] == fit["n_transitions"] >= tf.MIN_TRANSITIONS
    sources = {t["child_id"] for t in fit["transitions"]}
    conn = sqlite3.connect(str(run_dir / "world.db"))
    try:
        inherited = {r[0] for r in conn.execute("SELECT agent_id FROM policy_versions WHERE source='inherited'")}
        mutated = conn.execute("SELECT COUNT(*) FROM policy_versions WHERE source='mutated'").fetchone()[0]
        with_parent = {r[0] for r in conn.execute("SELECT agent_id FROM agents WHERE parent_id IS NOT NULL")}
        deepest = conn.execute("SELECT MAX(generation) FROM agents").fetchone()[0]
    finally:
        conn.close()
    assert sources <= inherited <= with_parent and mutated > 0 and deepest >= 2
    assert len(fit["cohorts"]) >= 1


def test_scripted_world_resolves_every_new_outcome(scripted_runs: list[tuple[Path, dict[str, Any]]]) -> None:
    run_dir, values = scripted_runs[0]
    fit = tf.fit_table(run_dir)
    keyword_fit = tf.fit_table(run_dir, keyword=True)
    assert fit is not None and keyword_fit is not None
    for path, value in values.items():
        assert value is None or (isinstance(value, float) and math.isfinite(value)), path
        if path.startswith("practice_share."):
            assert value is not None, path  # cluster_entropy is defined: the run has living agents with bullets
            continue
        table = keyword_fit if path.endswith(".keyword") else fit
        parts = path.removesuffix(".keyword").split(".")[1:]
        if parts[0] == "q":
            assert (value is None) == (sum(table["counts"][parts[1]].values()) == 0), path
        elif parts[0] == "f":
            assert (value is None) == (table["pool"][parts[1]] == 0), path
        elif parts[0] == "selection_spread":
            enough = [j for j in pr.CODES if table["parents"][j] >= tf.MIN_PARENTS]
            assert (value is None) == (len(enough) < 2), path
        else:
            assert value is not None, path
    for flags in FLAGS:
        shares = _shares(run_dir, flags)
        assert shares == [values[f"practice_share.{c}{flags}"] for c in pr.CODES]
        assert sum(shares) == pytest.approx(1.0), flags
        assert _h(shares) == pytest.approx(end_of_run_outcome(run_dir, f"cluster_entropy{flags}")), flags
    for j in pr.CODES:
        row = [values[f"replicator_mutator.q.{j}.{i}"] for i in pr.CODES]
        if row[0] is not None:
            assert sum(row) == pytest.approx(1.0), j


def test_scripted_world_is_deterministic(scripted_runs: list[tuple[Path, dict[str, Any]]]) -> None:
    (_, first), (_, second) = scripted_runs
    assert first == second
