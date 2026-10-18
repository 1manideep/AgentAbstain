"""Transmission outcomes (MEMORY_EVOLUTION 7.1) and the practice-coding tooling of 7.3: the codebook, the blind sampler,
the keyword coder and Cohen's kappa.

A hand-built ``world.db`` (five agents, a wallet ledger, policy versions with seeds, self-edits, an inheritance and an
adoption, and practice events of every kind) pins every outcome, end of run and per day, to values worked out by hand
in the comments. The sampler runs over a synthetic tree of runs whose paths name arms and experiments, and must not
leak them. The codebook in docs/research is parsed and held to the code and to the 7.3 table. A short scripted world
with practices on checks the outcomes on a real run.
"""

from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
import math
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, SMOKE, deep_merge

from void.config import load_config
from void.db import Database
from void.memory.policy import SEED_DEFAULT
from void.research.analysis import DAILY, computed_outcome_names, end_of_run_outcome
from void.research.outcomes import OUTCOMES, load_all
from void.research.outcomes import practices as pr
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
CODEBOOK = REPO / "docs" / "research" / "codebook.md"
PLAN = REPO / "docs" / "research" / "MEMORY_EVOLUTION.md"
TPD = 4
LAST_TICK = 20

EVENT_NAMES = ("adoption_rate", "adoption_latency_ticks", "persistence_rate", "first_adoption_source_richer_share")
POLICY_NAMES = ("novelty_share", "lineage_depth", "cluster_entropy")
NAMES = EVENT_NAMES + POLICY_NAMES


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("code_practices_script", REPO / "scripts" / "code_practices.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _h(*shares: float) -> float:
    return -sum(p * math.log2(p) for p in shares if p > 0)


def _read(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --- the registry --------------------------------------------------------------------------------------------------
def test_outcomes_are_registered_with_their_daily_versions() -> None:
    load_all()
    listed = computed_outcome_names()
    for name in NAMES:
        assert name in OUTCOMES and name in listed, name
        assert OUTCOMES[name] is getattr(pr, name), name
    for name in EVENT_NAMES:
        assert name in DAILY, name
    for name in POLICY_NAMES:
        assert name not in DAILY, name


# --- a hand-built world ----------------------------------------------------------------------------------------------
# (agent_id, parent_id, generation, born_tick, died_tick, status, end balance in micro-dollars)
AGENTS = (
    ("a", None, 0, 0, None, "alive", 1_000_000),
    ("b", None, 0, 0, None, "alive", 200_000),
    ("c", "a", 1, 8, None, "alive", 700_000),
    ("d", None, 0, 0, 12, "bankrupt", 0),
    ("e", None, 0, 0, None, "alive", 500_000),   # alive without a policy: in no policy figure
)
# (tick, agent, delta): consistent with the end balances, so the balance at any tick is known
LEDGER = (
    (0, "a", 1_000_000),
    (0, "b", 600_000), (6, "b", -400_000),                    # b has 200k from tick 6 on
    (8, "c", 300_000), (13, "c", 400_000),                    # c has 300k at ticks 8-12, 700k at the end
    (0, "d", 400_000), (10, "d", -350_000), (12, "d", -50_000),
    (0, "e", 500_000),
)
A1 = "# A\n\n- Keep a hub note per node.\n- Delete stale notes."
A2 = A1 + "\n- Repeat the best route."
A3 = "# A\n\n- Keep a hub note per node.\n- Delete stale notes, always.\n- Repeat the best route.\n- Write a summary each night."
B1 = "# B\n\n- Trust nobody.\n- Write a summary each night."
B2 = "# B\n\n- Trust nobody.\n- Keep a hub note per node.\n- Tag every note."
C2 = ("# C\n\n- Keep a hub note per node.\n- Unpaired practice.\n- Answer questions first.\n- Repeat the best route.\n"
      "- Ask before trusting.")
D1 = "# D\n\n- Ask before trusting."
# (agent, version, tick, text, source, source_agent_id)
POLICIES = (
    ("a", 1, 0, A1, "seed", None),
    ("a", 2, 4, A2, "self_edit", None),
    ("a", 3, 7, A3, "adopted", "b"),       # the row names b; event ad2 records the same adoption
    ("b", 1, 0, B1, "seed", None),
    ("b", 2, 6, B2, "self_edit", None),    # a maintenance edit the kernel measured as an adoption (event ad1)
    ("c", 1, 8, A3, "inherited", "a"),     # a's current text at c's birth, verbatim
    ("c", 2, 12, C2, "self_edit", None),
    ("d", 1, 0, D1, "seed", None),
)
# (event_id, tick, kind, source, target, excerpt)
EVENTS = (
    ("s1", 2, "suggested", "a", "b", "- Keep a hub note per node."),
    ("s2", 3, "suggested", "d", "b", "- Ask before trusting."),
    ("s3", 4, "suggested", "a", "b", "- Merge duplicate notes nightly."),
    ("s4", 5, "suggested", "b", "a", "- Delete stale notes."),
    ("ad1", 6, "adopted", "a", "b", "Keep a hub note per node"),          # pairs with s1 (same excerpt), not s3
    ("ad2", 7, "adopted", "b", "a", "- Delete stale notes, always."),     # no same excerpt: the latest b->a, s4
    ("s5", 9, "suggested", "c", "d", "- Trust nobody."),
    ("ad3", 10, "adopted", "c", "d", "Trust nobody."),                    # pairs with s5
    ("ad4", 11, "adopted", "e", "c", "- Unpaired practice."),             # no suggestion e->c: unpaired
    ("pe1", 14, "persisted", "a", "b", "Keep a hub note per node"),
    ("dr1", 15, "dropped", "b", "a", "- Delete stale notes, always."),
)


@pytest.fixture
def world(tmp_path: Path) -> Path:
    cfg = load_config(REPO / "configs" / "base.yaml", {"run": {"days": 5, "ticks_per_day": TPD}})
    (tmp_path / "config.resolved.yaml").write_text(cfg.to_yaml())
    db = Database(tmp_path / "world.db")
    try:
        for aid, parent, gen, born, died, status, balance in AGENTS:
            db.execute("INSERT INTO agents(agent_id, name, parent_id, model_tier, balance, personality_seed, memory_path, "
                       "generation, status, x, y, entropy_budget, born_tick, died_tick, created_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, aid.upper(), parent, "scripted", balance, "{}", "", gen, status, 0, 0, 1, born, died, "t"))
        for i, (tick, aid, delta) in enumerate(LEDGER):
            db.execute("INSERT INTO wallet_ledger(entry_id, tick, agent_id, delta, balance_after, kind) VALUES(?,?,?,?,?,?)",
                       (f"l{i:02d}", tick, aid, delta, 0, "test"))
        for aid, version, tick, text, source, src in POLICIES:
            db.execute("INSERT INTO policy_versions(agent_id, version, tick, text, parent_version, source, source_agent_id) "
                       "VALUES(?,?,?,?,?,?,?)", (aid, version, tick, text, version - 1 or None, source, src))
        for event_id, tick, kind, source, target, excerpt in EVENTS:
            db.execute("INSERT INTO practice_events(event_id, tick, kind, source_agent_id, target_agent_id, excerpt) "
                       "VALUES(?,?,?,?,?,?)", (event_id, tick, kind, source, target, excerpt))
        db.kv_set("tick", LAST_TICK)
    finally:
        db.close()
    return tmp_path


def test_practice_event_rates_are_exact(world: Path) -> None:
    got = {name: end_of_run_outcome(world, name) for name in EVENT_NAMES}
    assert got["adoption_rate"] == pytest.approx(4 / 5)                 # ad1-ad4 over s1-s5
    assert got["adoption_latency_ticks"] == pytest.approx((4 + 2 + 1) / 3)  # ad1-s1, ad2-s4, ad3-s5; ad4 unpaired
    assert got["persistence_rate"] == pytest.approx(1 / 4)              # pe1 over ad1-ad4
    assert end_of_run_outcome(world, "persistence_rate.resolved") == pytest.approx(1 / 2)  # pe1 over pe1 + dr1
    # first adoptions: b from a at 6 (1M > 200k, after b's tick-6 spend: richer), a from b at 7 (no),
    # d from c at 10 (300k > 50k after d's tick-10 loss; it had 400k before), c from e at 11 (500k > 300k; c ends
    # with 700k, so end balances would say no)
    assert got["first_adoption_source_richer_share"] == pytest.approx(3 / 4)


def test_adoptions_pair_with_the_suggestion_they_took_up(world: Path) -> None:
    with pr.connect(world) as db:
        events = pr._read_events(db, None)
    assert events is not None
    pairs = [(a.event_id, None if s is None else s.event_id) for a, s in pr.pair_adoptions(events)]
    assert pairs == [("ad1", "s1"), ("ad2", "s4"), ("ad3", "s5"), ("ad4", None)]


def test_first_adoptions_table(world: Path) -> None:
    rows = {r["agent_id"]: r for r in pr.first_adoptions(world)}
    assert sorted(rows) == ["a", "b", "c", "d"]
    assert rows["b"] == {"agent_id": "b", "tick": 6, "source_agent_id": "a", "adopter_balance_usd": 0.2,
                         "source_balance_usd": 1.0, "richer": True}
    assert (rows["a"]["richer"], rows["c"]["adopter_balance_usd"], rows["d"]["adopter_balance_usd"]) == (False, 0.3, 0.05)


def test_daily_versions_count_the_rows_written_by_each_day(world: Path) -> None:
    load_all()

    def daily(name: str, day: int, arg: str | None = None) -> float | None:
        return DAILY[name](world, arg, day)

    # day 2 ends at tick 8: s1-s4, ad1, ad2
    assert daily("adoption_rate", 2) == pytest.approx(2 / 4)
    assert daily("adoption_latency_ticks", 2) == pytest.approx((4 + 2) / 2)
    assert daily("persistence_rate", 2) == 0.0
    assert daily("persistence_rate", 2, "resolved") is None
    assert daily("first_adoption_source_richer_share", 2) == pytest.approx(1 / 2)
    # day 3 ends at tick 12: everything but the fates
    assert daily("adoption_rate", 3) == pytest.approx(4 / 5)
    assert daily("first_adoption_source_richer_share", 3) == pytest.approx(3 / 4)
    assert daily("persistence_rate", 3) == 0.0
    # day 4 ends at tick 16: the fates are in; the last day equals the end-of-run value
    for name in EVENT_NAMES:
        assert daily(name, 5) == end_of_run_outcome(world, name), name
    assert daily("persistence_rate", 4, "resolved") == pytest.approx(1 / 2)
    assert daily("adoption_rate", 0) is None  # nothing suggested by tick 0


def test_lineage_depth_is_the_longest_chain_of_hops(world: Path) -> None:
    # b2 adopted from a1 (event ad1 pairs with s1 at tick 2, when a was at v1): 1. a3 adopted from b at tick 7 (the
    # row: b2, depth 1) so 2; the event ad2 pairs with s4 at tick 5 (b1, depth 0) and adds only 1. c1 inherited a3
    # at tick 8: 3; c2 is a self-edit of it: 3. Living with a policy: a, b, c (d is dead, e has none).
    assert end_of_run_outcome(world, "lineage_depth") == pytest.approx((2 + 1 + 3) / 3)


def test_novelty_share_counts_bullets_no_ancestor_or_seed_holds(world: Path) -> None:
    # a3: hub (seed), "delete stale notes always" (the excerpt a adopted), "repeat the best route" (a wrote it in a2:
    #     its own earlier version does not count, so novel), "write a summary each night" (b's seed) -> 1 of 4
    # b2: "trust nobody" (seed), hub (seed), "tag every note" (in no ancestor: a1 is the text behind the hop) -> 1 of 3
    # c2: hub (seed), "unpaired practice" (the excerpt of ad4), "answer questions first" (novel), "repeat the best
    #     route" (in the inherited a3), "ask before trusting" (d's seed: every seed counts, not only the lineage's)
    #     -> 1 of 5
    assert end_of_run_outcome(world, "novelty_share") == pytest.approx(3 / 12)


def test_cluster_entropy_with_the_keyword_coder(world: Path) -> None:
    # keyword codes: a3 = T1 hub, T4 delete/stale, T7 repeat/best/route, T2 summary; b2 = T8 trust, T1, T3 tag;
    # c2 = T1, T9 (no rule), T6 answer/question, T7, T8 trusting
    assert [pr.keyword_code(b) for b in pr.bullets(A3)] == ["T1", "T4", "T7", "T2"]
    assert [pr.keyword_code(b) for b in pr.bullets(B2)] == ["T8", "T1", "T3"]
    assert [pr.keyword_code(b) for b in pr.bullets(C2)] == ["T1", "T9", "T6", "T7", "T8"]
    mix = {
        "T1": (1 / 4 + 1 / 3 + 1 / 5) / 3, "T2": (1 / 4) / 3, "T3": (1 / 3) / 3, "T4": (1 / 4) / 3, "T6": (1 / 5) / 3,
        "T7": (1 / 4 + 1 / 5) / 3, "T8": (1 / 3 + 1 / 5) / 3, "T9": (1 / 5) / 3,
    }
    assert sum(mix.values()) == pytest.approx(1.0)
    assert end_of_run_outcome(world, "cluster_entropy") == pytest.approx(_h(*mix.values()))
    # every agent's profile ties, and a tie goes to the lower code: all three lean T1
    assert end_of_run_outcome(world, "cluster_entropy.dominant") == 0.0


def test_cluster_entropy_reads_practice_codes_csv(world: Path) -> None:
    rows = [("- Trust nobody.", "T8"), ("Tag every note", "8"), ("repeat the best route", "T7"),
            ("Repeat the best route!", "t7"), ("repeat the best route", "T2"), ("Keep a hub note per node", "T1"),
            ("unrelated line", "T5"), ("uncoded", "")]
    with open(world / pr.CODES_FILE, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["item_id", "text", "code"])
        w.writerows([(f"i{i}", t, c) for i, (t, c) in enumerate(rows)])
    table = pr.read_codes(world / pr.CODES_FILE)
    assert table["repeat the best route"] == "T7"  # two votes against one
    assert "uncoded" not in table
    # a3 = T1 (file), T4 (keyword: not in the file), T7 (file), T2 (keyword); b2 = T8, T1, T8 (file);
    # c2 = T1 (file), T9, T6 (keyword), T7 (file), T8 (keyword)
    a, b, c = {"T1": 1, "T4": 1, "T7": 1, "T2": 1}, {"T8": 2, "T1": 1}, {"T1": 1, "T9": 1, "T6": 1, "T7": 1, "T8": 1}
    mix = {code: sum(p.get(code, 0) / sum(p.values()) for p in (a, b, c)) / 3 for code in pr.CODES}
    assert end_of_run_outcome(world, "cluster_entropy") == pytest.approx(_h(*mix.values()))
    # dominant: a -> T1 (tie), b -> T8, c -> T1 (tie): clusters of 2 and 1
    assert end_of_run_outcome(world, "cluster_entropy.dominant") == pytest.approx(_h(2 / 3, 1 / 3))
    # .keyword ignores the file
    assert end_of_run_outcome(world, "cluster_entropy.keyword.dominant") == 0.0
    assert end_of_run_outcome(world, "cluster_entropy.dominant.keyword") == 0.0
    # a code outside the codebook is an error, not a new category
    with open(world / pr.CODES_FILE, "a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow(["i99", "Trust nobody", "T10"])
    with pytest.raises(ValueError, match="T10"):
        end_of_run_outcome(world, "cluster_entropy")


@pytest.mark.parametrize("path", ["adoption_rate.x", "adoption_latency_ticks.1", "persistence_rate.fast",
                                  "first_adoption_source_richer_share.x", "novelty_share.x", "lineage_depth.2",
                                  "cluster_entropy.mean", "cluster_entropy.dominant."])
def test_unknown_arguments_fail_loudly(world: Path, path: str) -> None:
    with pytest.raises(ValueError):
        end_of_run_outcome(world, path)


def test_a_run_without_the_memory_tables_gives_none(world: Path) -> None:
    db = Database(world / "world.db")
    try:
        db.execute("DROP TABLE practice_events")
        db.execute("DROP TABLE policy_versions")
    finally:
        db.close()
    for name in NAMES:
        assert end_of_run_outcome(world, name) is None, name
    assert pr.first_adoptions(world) == []


def test_without_suggestions_or_adoptions_the_rates_are_none(world: Path) -> None:
    db = Database(world / "world.db")
    try:
        db.execute("DELETE FROM practice_events")
    finally:
        db.close()
    for name in EVENT_NAMES:
        assert end_of_run_outcome(world, name) is None, name
    # without the events the policy rows still carry the adoption a3 and the inheritance c1
    assert end_of_run_outcome(world, "lineage_depth") == pytest.approx((1 + 0 + 2) / 3)


# --- normalization and the keyword coder -----------------------------------------------------------------------------
def test_bullets_are_normalized_practice_lines() -> None:
    assert pr.normalize_line("- Delete notes that turned out to be wrong.") == pr.normalize_line(
        "* delete notes  that turned out to be WRONG")
    assert pr.normalize_line("  1. Link [[Node n3]]!") == "1 link node n3"
    assert pr.bullets("# Title\n\n- One.\n- one\n---\n\n- Two") == ["one", "two"]
    assert pr.bullets(None) == [] and pr.bullets("") == []


def test_keyword_coder_rules() -> None:
    assert pr.keyword_code("Delete notes that turned out to be wrong.") == "T4"
    assert pr.keyword_code("Keep the old backlinks.") == "T1"           # its own rule, not 'link' inside a word
    assert pr.keyword_scores("Keep the old backlinks.") == {"T1": 1}
    assert pr.keyword_code("Trust agents whose claims were right.") == "T8"  # T8 1, T3 1: a tie goes to T8
    assert pr.keyword_code("For every fact I write, record who said it.") == "T5"
    assert pr.keyword_code("Keep notes short.") == pr.OTHER == "T9"
    assert pr.keyword_code("") == "T9"
    for code in pr.TIE_ORDER:
        assert code in pr.KEYWORDS
    assert sorted(pr.TIE_ORDER) == sorted(pr.KEYWORDS) and pr.OTHER not in pr.KEYWORDS
    # a pure function: the seed policy codes the same every time
    assert [pr.keyword_code(b) for b in pr.bullets(SEED_DEFAULT)] == ["T3", "T1", "T1", "T5", "T4"]


def test_normalize_code() -> None:
    assert [pr.normalize_code(x) for x in (" t3 ", "3", "T9", "", None, "T10", "x")] == ["T3", "T3", "T9", "", "", "T10", "X"]


# --- kappa -------------------------------------------------------------------------------------------------------
def test_kappa_is_one_for_perfect_agreement() -> None:
    labels = ["T1", "T2", "T3", "T1", "T9", "T4"]
    assert pr.cohens_kappa(labels, list(labels)) == 1.0
    assert pr.cohens_kappa(["T1"] * 5, ["T1"] * 5) == 1.0  # one label throughout: 0/0, perfect agreement


def test_kappa_matches_the_textbook_two_by_two_example() -> None:
    # 50 items: both yes 20, A yes B no 5, A no B yes 10, both no 15. p_o = 35/50 = 0.7;
    # p_e = (25/50)(30/50) + (25/50)(20/50) = 0.3 + 0.2 = 0.5; kappa = (0.7 - 0.5) / (1 - 0.5) = 0.4
    a = ["yes"] * 20 + ["yes"] * 5 + ["no"] * 10 + ["no"] * 15
    b = ["yes"] * 20 + ["no"] * 5 + ["yes"] * 10 + ["no"] * 15
    got = pr.agreement(a, b)
    assert (got["n"], got["observed"], got["expected"]) == (50, pytest.approx(0.7), pytest.approx(0.5))
    assert got["kappa"] == pytest.approx(0.4)
    assert got["confusion"] == {("no", "no"): 15, ("no", "yes"): 10, ("yes", "no"): 5, ("yes", "yes"): 20}
    assert pr.cohens_kappa(b, a) == pytest.approx(0.4)  # symmetric
    assert pr.cohens_kappa(["x", "y"], ["y", "x"]) == pytest.approx(-1.0)  # systematic disagreement
    with pytest.raises(ValueError):
        pr.cohens_kappa([], [])
    with pytest.raises(ValueError):
        pr.cohens_kappa(["x"], ["x", "y"])


def test_kappa_command_joins_on_item_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = ["yes"] * 25 + ["no"] * 25
    b = ["yes"] * 20 + ["no"] * 5 + ["yes"] * 10 + ["no"] * 15
    ids = [f"it_{i:03d}" for i in range(50)]
    pa, pb = tmp_path / "a.csv", tmp_path / "b.csv"
    pr.write_csv(pa, ("item_id", "code"), [{"item_id": i, "code": x} for i, x in zip(ids, a, strict=True)]
                 + [{"item_id": "it_only_a", "code": "yes"}])
    pr.write_csv(pb, ("item_id", "text", "code"), [{"item_id": i, "text": "t", "code": x}
                                                   for i, x in reversed(list(zip(ids, b, strict=True)))]
                 + [{"item_id": "it_blank", "code": ""}])
    script = _script()
    assert script.main(["kappa", str(pa), str(pb), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["n"] == 50 and out["kappa"] == pytest.approx(0.4) and out["coded_in_one_file_only"] == 1
    assert script.main(["kappa", str(pa), str(pb)]) == 0
    assert "kappa: 0.4000" in capsys.readouterr().out
    empty = tmp_path / "empty.csv"
    pr.write_csv(empty, ("item_id", "code"), [])
    assert script.main(["kappa", str(pa), str(empty)]) == 2


# --- the blind sampler -------------------------------------------------------------------------------------------
LAYOUT = {  # run path -> {agent: [policy texts by version]}
    "exp_secretname/arm_alpha/seed_1": {"ag_x1": [SEED_DEFAULT, "- Merge notes nightly.\n- Trust nobody."],
                                        "ag_x2": [SEED_DEFAULT]},
    "exp_secretname/arm_alpha/seed_2": {"ag_x3": [SEED_DEFAULT, "- Keep a hub note per node."]},
    "exp_secretname/arm_bravo/seed_1": {"ag_y1": [SEED_DEFAULT, "- Tag every note.\n- Repeat the best route.",
                                                  "- Tag every note."],
                                        "ag_y2": ["", "# only a heading"]},          # no practice line: not drawn
    "exp_secretname/arm_bravo/seed_2": {"ag_y3": [SEED_DEFAULT, "- Keep a hub note per node.", "- Ask first."]},
}
# the seed (in every run), merge+trust, hub (in two runs), tag+repeat, tag, ask; ag_y2's versions have no practice line
DISTINCT_TEXTS = 6
ALL_VERSIONS = 11


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    for rel, agents in LAYOUT.items():
        run_dir = root / rel
        run_dir.mkdir(parents=True)
        db = Database(run_dir / "world.db")
        try:
            for aid, texts in agents.items():
                for v, text in enumerate(texts, start=1):
                    db.execute("INSERT INTO policy_versions(agent_id, version, tick, text, parent_version, source) "
                               "VALUES(?,?,?,?,?,?)", (aid, v, 10 * v, text, v - 1 or None, "seed" if v == 1 else "self_edit"))
        finally:
            db.close()
    old = root / "old_run"  # a run made before the memory layer: skipped
    old.mkdir()
    db = Database(old / "world.db")
    try:
        db.execute("DROP TABLE policy_versions")
    finally:
        db.close()
    return root


def test_the_sample_is_blind(runs_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "sample.csv"
    assert _script().main(["sample", str(runs_root), "--n", "4", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    header = text.splitlines()[0].split(",")
    assert tuple(header) == pr.SAMPLE_COLUMNS == ("item_id", "policy_id", "line_no", "text", "code")
    forbidden = {"arm", "experiment", "exp", "run", "run_dir", "seed", "agent", "agent_id", "source", "tick", "version"}
    assert not forbidden & set(header)
    for leak in ("secretname", "arm_alpha", "arm_bravo", "alpha", "bravo", "seed_1", "seed_2", "ag_x", "ag_y", "runs"):
        assert leak not in text, leak
    rows = _read(out)
    assert len({r["policy_id"] for r in rows}) == 4
    assert all(re.fullmatch(r"it_[0-9a-f]{12}", r["item_id"]) for r in rows)
    assert all(re.fullmatch(r"pv_[0-9a-f]{12}", r["policy_id"]) for r in rows)
    assert len({r["item_id"] for r in rows}) == len(rows)
    assert all(r["code"] == "" for r in rows)
    # each version's lines together and in order
    seen: list[str] = []
    for r in rows:
        if not seen or seen[-1] != r["policy_id"]:
            assert r["policy_id"] not in seen
            seen.append(r["policy_id"])
    for pid in seen:
        assert [int(r["line_no"]) for r in rows if r["policy_id"] == pid] == list(
            range(1, sum(r["policy_id"] == pid for r in rows) + 1))


def test_the_key_maps_every_item_back_to_its_run(runs_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "s.csv"
    assert _script().main(["sample", str(runs_root), "--n", "100", "--out", str(out)]) == 0
    key = {r["item_id"]: r for r in _read(tmp_path / "s.key.csv")}
    rows = _read(out)
    assert set(key) == {r["item_id"] for r in rows}
    assert tuple(_read(tmp_path / "s.key.csv")[0]) == pr.KEY_COLUMNS
    for r in rows:
        k = key[r["item_id"]]
        assert k["policy_id"] == r["policy_id"] and k["line_no"] == r["line_no"]
        texts = LAYOUT[k["run"]][k["agent_id"]]
        lines = [ln for ln in pr.policy_lines(texts[int(k["version"]) - 1]) if pr.normalize_line(ln)]
        assert lines[int(r["line_no"]) - 1] == r["text"]
        assert int(k["tick"]) == 10 * int(k["version"])
    # by default each distinct text once: the seed text of five agents, the hub text of two, are one version each
    assert len({r["policy_id"] for r in rows}) == DISTINCT_TEXTS


def test_sampler_is_deterministic_and_seeded(runs_root: Path, tmp_path: Path) -> None:
    script = _script()
    paths = {}
    for name, seed in (("one", "0"), ("two", "0"), ("three", "7")):
        paths[name] = tmp_path / f"{name}.csv"
        assert script.main(["sample", str(runs_root), "--n", "3", "--out", str(paths[name]), "--seed", seed]) == 0
    assert paths["one"].read_bytes() == paths["two"].read_bytes()
    assert paths["one"].read_bytes() != paths["three"].read_bytes()
    assert pr.sample_policy_lines(runs_root, n=3, seed=0) == pr.sample_policy_lines(runs_root, n=3, seed=0)
    # another salt, other ids, the same draw
    salted = pr.sample_policy_lines(runs_root, n=3, seed=0, salt="private")
    plain = pr.sample_policy_lines(runs_root, n=3, seed=0)
    assert [s.text for s in salted] == [s.text for s in plain]
    assert not {s.item_id for s in salted} & {s.item_id for s in plain}
    # --all-versions keeps the duplicates (the seed text in every run)
    every = pr.sample_policy_lines(runs_root, n=100, seed=0, distinct=False)
    assert len({(s.run, s.agent_id, s.version) for s in every}) == ALL_VERSIONS
    with pytest.raises(ValueError):
        pr.sample_policy_lines(runs_root, n=0)


def test_sample_with_nothing_to_draw(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert _script().main(["sample", str(tmp_path / "empty"), "--out", str(tmp_path / "x.csv")]) == 2


def test_code_command_fills_the_code_column_with_the_keyword_coder(runs_root: Path, tmp_path: Path) -> None:
    script = _script()
    sample, codes = tmp_path / "sample.csv", tmp_path / "codes.csv"
    assert script.main(["sample", str(runs_root), "--n", "100", "--out", str(sample)]) == 0
    assert script.main(["code", str(sample), "--coder", "keyword", "--out", str(codes)]) == 0
    before, after = _read(sample), _read(codes)
    assert [{k: v for k, v in r.items() if k != "code"} for r in after] == [
        {k: v for k, v in r.items() if k != "code"} for r in before]
    assert [r["code"] for r in after] == [pr.keyword_code(r["text"]) for r in before]
    assert {r["code"] for r in after} <= set(pr.CODES)
    # the keyword coder agrees with itself
    assert script.main(["kappa", str(codes), str(codes)]) == 0
    with pytest.raises(SystemExit):
        script.main(["code", str(sample), "--coder", "llm", "--out", str(codes)])  # not built: paid


# --- the codebook --------------------------------------------------------------------------------------------------
def _plan_codes() -> dict[str, str]:
    text = PLAN.read_text(encoding="utf-8")
    section = text.split("### 7.3 Coding the practices", 1)[1].split("\n### ", 1)[0]
    return dict(re.findall(r"^\| (T\d) \| (.+?) \|$", section, flags=re.M))


def _codebook_sections() -> dict[str, tuple[str, str]]:
    text = CODEBOOK.read_text(encoding="utf-8")
    parts = re.split(r"^### (T\d) (.+)$", text, flags=re.M)
    out: dict[str, tuple[str, str]] = {}
    for i in range(1, len(parts) - 2, 3):
        body = parts[i + 2].split("\n## ", 1)[0]
        out[parts[i]] = (parts[i + 1].strip(), body)
    return out


def test_codebook_categories_are_exactly_the_plan_s() -> None:
    plan = _plan_codes()
    assert list(plan) == [f"T{i}" for i in range(1, 10)]
    assert plan == pr.CODE_NAMES
    assert {code: name for code, (name, _) in _codebook_sections().items()} == pr.CODE_NAMES


def test_codebook_keywords_and_examples_match_the_coder() -> None:
    sections = _codebook_sections()
    for code, (_, body) in sections.items():
        line = next(ln for ln in body.splitlines() if ln.startswith("Keywords:"))
        assert tuple(re.findall(r"`([^`]+)`", line)) == pr.KEYWORDS.get(code, ()), code
        examples = re.findall(r"^- `(.+)`$", body.split("Keywords:")[0], flags=re.M)
        assert examples, code
        for example in examples:
            assert pr.keyword_code(example) == code, (code, example)
    text = CODEBOOK.read_text(encoding="utf-8")
    assert ", ".join(pr.TIE_ORDER) in text
    hard = re.findall(r"^\| `(.+?)`(?: \(.+?\))? \| (T\d) \| (T\d) \|$", text, flags=re.M)
    assert len(hard) >= 5
    for line, human, keyword in hard:
        assert pr.keyword_code(line) == keyword, line
        assert human != keyword, line  # a hard case is one where the two differ
    assert "[assumption]" in text


# --- a scripted world --------------------------------------------------------------------------------------------
def test_a_scripted_world_with_practices_on(tmp_path: Path) -> None:
    sharing: dict[str, Any] = {"memory": {"policy": {"enabled": True},
                                          "practices": {"enabled": True, "scripted_share_weight": 3.0}},
                               "run": {"days": 2, "name": "share_practice"}}
    sim = Simulation(load_config(SMOKE, deep_merge(NO_PROVIDER_TIERS, sharing)), tmp_path / "run")
    asyncio.run(sim.run())
    run_dir = tmp_path / "run"
    suggested = sim.db.fetchone("SELECT COUNT(*) AS n FROM practice_events WHERE kind='suggested'")["n"]
    assert suggested > 0
    # nothing measures adoption yet: the rate is 0 and the outcomes that need an adoption have none
    assert end_of_run_outcome(run_dir, "adoption_rate") == 0.0
    for name in ("adoption_latency_ticks", "persistence_rate", "first_adoption_source_richer_share"):
        assert end_of_run_outcome(run_dir, name) is None, name
    # no maintenance edits the policies: every living policy is the seed or an inherited copy of it
    alive = [r["agent_id"] for r in sim.db.fetchall("SELECT agent_id FROM agents WHERE status='alive'")]
    current = [sim.policy.text(aid) for aid in alive]
    assert current and all(t == SEED_DEFAULT.strip() for t in current)
    assert end_of_run_outcome(run_dir, "novelty_share") == 0.0
    shares = {"T1": 2 / 5, "T3": 1 / 5, "T4": 1 / 5, "T5": 1 / 5}  # SEED_DEFAULT's five lines, as keyword-coded above
    assert end_of_run_outcome(run_dir, "cluster_entropy") == pytest.approx(_h(*shares.values()))
    assert end_of_run_outcome(run_dir, "cluster_entropy.dominant") == 0.0
    # with inheritance as the only hop, a policy's depth is its agent's generation
    generations = [r["generation"] for r in sim.db.fetchall("SELECT generation FROM agents WHERE status='alive'")]
    assert any(generations)  # the world had children: the check is not 0 == 0
    assert end_of_run_outcome(run_dir, "lineage_depth") == pytest.approx(sum(generations) / len(generations))
