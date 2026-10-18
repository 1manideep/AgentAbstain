"""E3a, the discrete-generation protocol (MEMORY_EVOLUTION §6.5, §6.13 row 3): ``void generations``, scripted only.

The protocol's pure pieces (bullets, the oblique rule, the ranking, round-robin parents, the next generation's plan)
are tested on hand-made inputs. The worlds are kept short to keep the suite quick: three generations of two seeds
through the CLI, twice into different directories and once more into the first, give equal JSON; the generation
worlds carry the survivors' vaults and policies with their provenance; and a bullet planted in a generation-0
survivor rises in share under the oblique arm (and does not under the vertical one).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import textwrap
from pathlib import Path

import pytest

from void.memory.policy import SEED_DEFAULT
from void.research.compare import config_diff
from void.research.generations import (
    ARMS,
    GENERATIONS_FILE,
    MANIFEST_FILE,
    ROW_FIELDS,
    SUMMARY_FILE,
    CarriedNote,
    Member,
    append_bullet,
    assign_parents,
    bullet_share,
    bullets,
    carried_origin,
    cli_main,
    founders_plan,
    generation_config,
    most_common_absent_bullet,
    next_plan,
    oblique_policy,
    rank_members,
    run_seed,
)

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "configs" / "exp_mem_e3a.yaml"
MARKER = "Mark the richest node first."
DAYS = 1


def _db(run_dir: Path) -> contextlib.closing[sqlite3.Connection]:
    conn = sqlite3.connect(str(run_dir / "world.db"))
    conn.row_factory = sqlite3.Row
    return contextlib.closing(conn)


def _member(slot: int, balance: int, policy: str | None, status: str = "alive") -> Member:
    return Member(slot=slot, name="ABCDEFGHIJKL"[slot], agent_id=f"ag{slot}", status=status, balance=balance,
                  policy_start=policy, policy_end=policy)


def _json_files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*.json"))}


# --- the pure pieces --------------------------------------------------------------------------------------------
def test_bullets_drop_markers_collapse_space_and_keep_each_once() -> None:
    text = "# Heading\n\nprose line\n- one\n* two\n+ three\n1. four\n2) five\n-   one  \n- ONE\n-\n"
    assert bullets(text) == ["one", "two", "three", "four", "five"]
    assert len(bullets(SEED_DEFAULT)) == 5 and bullets(None) == [] and bullets("") == []


def test_most_common_absent_bullet_counts_then_rank_then_line() -> None:
    survivors = ["- a\n- b", "- c\n- b", "- C"]  # in rank order; "C" is "c"
    assert most_common_absent_bullet("- a", survivors) == "b"           # b and c are held twice; b's holder ranks first
    assert most_common_absent_bullet("- a\n- B", survivors) == "c"      # the parent holds b (case-folded)
    assert most_common_absent_bullet("- a\n- b\n- c", survivors) is None
    assert most_common_absent_bullet(None, ["- x\n- y", "- z"]) == "x"  # a tie at one holder: best rank, then line


def test_oblique_policy_appends_one_bullet_within_the_limit() -> None:
    parent = "# Mine\n\n- a"
    assert oblique_policy(parent, [parent, "- z\n- y"]) == ("# Mine\n\n- a\n- z", "z")
    assert oblique_policy(None, ["- z"]) == ("- z", "z")
    assert oblique_policy(parent, [parent]) == (parent, None)
    assert oblique_policy(parent, ["- zzzz"], max_chars=len(parent) + 3) == (parent, None)
    assert append_bullet("- a\n\n", "b") == "- a\n- b" and append_bullet("", "b") == "- b"


def test_ranking_and_round_robin_parents() -> None:
    members = [_member(0, 5, None), _member(1, 9, None), _member(2, 5, None, status="bankrupt"), _member(3, 5, None),
               _member(4, 0, None, status="bankrupt"), _member(5, 0, None)]
    assert [m.slot for m in rank_members(members)] == [1, 0, 3, 2, 5, 4]  # balance; the living first; roster order
    a, b = members[1], members[0]
    assert assign_parents([5, 1, 3], [a, b]) == [(1, a), (3, b), (5, a)]
    with pytest.raises(ValueError):
        assign_parents([1], [])


def test_next_plan_keeps_survivors_in_their_slots_and_gives_children_a_parent_policy_by_arm() -> None:
    roster = ["Ada", "Bao", "Cyra", "Dev"]
    plan0 = founders_plan(roster, {"Bao": MARKER})
    assert [p.individual for p in plan0] == ["Ada@g0", "Bao@g0", "Cyra@g0", "Dev@g0"] and plan0[1].plant == MARKER
    with pytest.raises(ValueError, match="not in the roster"):
        founders_plan(roster, {"Nobody": MARKER})
    members = [_member(0, 10, "- w"), _member(1, 40, "- x\n- y"), _member(2, 30, "- y\n- z"), _member(3, 20, "- w")]
    note = CarriedNote(note_id="note_1", title="t", body="b", importance=0.5, tags=("places",), hop=0,
                       origin_note_id=None, origin_generation=None, path_agents=())
    notes_of = {"ag1": (note,), "ag2": ()}.__getitem__
    plan1 = next_plan("oblique", 0, plan0, members, run_id="run_g0", notes_of=notes_of)
    assert [p.role for p in plan1] == ["child", "survivor", "survivor", "child"]
    surv = plan1[1]
    assert (surv.individual, surv.policy, surv.policy_source, surv.carried_from, surv.notes) == (
        "Bao@g0", "- x\n- y", "inherited", ("run_g0", "ag1"), (note,))
    # dead slots 0 and 3 in roster order take the survivors ranked 1 (slot 1) and 2 (slot 2)
    c0, c3 = plan1[0], plan1[3]
    assert (c0.individual, c0.parent, c0.parent_slot) == ("Ada@g1", "Bao@g0", 1)
    assert (c0.policy, c0.added_bullet, c0.policy_source) == ("- x\n- y\n- z", "z", "adapted")
    assert (c3.parent_slot, c3.policy, c3.added_bullet) == (2, "- y\n- z\n- x", "x")
    vertical = next_plan("vertical", 0, plan0, members, run_id="run_g0", notes_of=notes_of)
    assert [(p.policy, p.policy_source, p.added_bullet) for p in (vertical[0], vertical[3])] == [
        ("- x\n- y", "inherited", None), ("- y\n- z", "inherited", None)]
    # a child that survives keeps its label and its parent in the next plan
    members1 = [_member(0, 50, c0.policy), _member(1, 1, "- x\n- y"), _member(2, 2, "- y\n- z"), _member(3, 3, c3.policy)]
    plan2 = next_plan("oblique", 1, plan1, members1, run_id="run_g1", notes_of=lambda _aid: ())
    assert (plan2[0].role, plan2[0].individual, plan2[0].parent) == ("survivor", "Ada@g1", "Bao@g0")
    assert (plan2[1].role, plan2[1].individual, plan2[1].parent) == ("child", "Bao@g2", "Ada@g1")
    with pytest.raises(ValueError, match="unknown arm"):
        next_plan("sideways", 0, plan0, members, run_id="r", notes_of=notes_of)


def test_carried_origin_qualifies_in_world_ids_with_their_run() -> None:
    def note(origin: str | None) -> CarriedNote:
        return CarriedNote(note_id="note_9", title="t", body="b", importance=0.5, tags=(), hop=1, origin_note_id=origin,
                           origin_generation=None, path_agents=())

    assert carried_origin(note(None), "run_g3-s1") == "transplant:run_g3-s1:note_9"         # native: the note itself
    assert carried_origin(note("note_2"), "run_g3-s1") == "transplant:run_g3-s1:note_2"     # gossiped: its source note
    assert carried_origin(note("transplant:run_g0-s1:note_5"), "run_g3-s1") == "transplant:run_g0-s1:note_5"  # kept


def test_bullet_share() -> None:
    assert bullet_share(["- a\n- b", "- B", "- c", None], "b") == 0.5
    assert bullet_share([], "b") is None


def test_the_arms_differ_only_in_experiment_arm_and_generations_only_in_run_name() -> None:
    a = generation_config(CONFIG, "oblique", 3, 2)
    b = generation_config(CONFIG, "vertical", 3, 2)
    assert config_diff(a.model_dump(mode="json"), b.model_dump(mode="json")) == ["experiment.arm"]
    g0 = generation_config(CONFIG, "oblique", 3, 0)
    assert config_diff(a.model_dump(mode="json"), g0.model_dump(mode="json")) == ["run.name"]
    assert (a.run.name, a.run.seed, a.run.days, len(a.agents)) == ("exp_mem_e3a_g2", 3, 3, 12)
    assert a.memory.policy.enabled and a.memory.maintenance.enabled and a.memory.exam.enabled
    assert a.memory.policy.seed == "diverse" and not a.population.reproduction_enabled
    assert all(a.tiers[s.tier].provider == "scripted" for s in a.agents) and a.warnings() == []
    assert generation_config(CONFIG, "oblique", 3, 2, days=1).run.days == 1


# --- the command line -----------------------------------------------------------------------------------------
def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "e3a_variant.yaml"
    path.write_text(f"extends: {CONFIG}\n" + textwrap.dedent(body), encoding="utf-8")
    return path


def test_cli_refuses_paid_tiers_and_bad_input_before_running(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    base = ["--generations", "2", "--seeds", "1", "--out", str(out)]
    paid = _config(tmp_path, """
        agents:
          - {name: Ada, tier: budget}
          - {name: Bao, tier: scripted}
        """)
    assert cli_main(["--config", str(paid), *base]) == 2
    assert "refused" in capsys.readouterr().err
    assert cli_main(["--config", str(_config(tmp_path, "experiment: {arm: none}\n")), *base]) == 2
    assert "oblique, vertical" in capsys.readouterr().err
    no_policy = "memory: {policy: {enabled: false}, maintenance: {rights: [notes, structure]}}\n"
    assert cli_main(["--config", str(_config(tmp_path, no_policy)), *base]) == 2
    assert "memory.policy is off" in capsys.readouterr().err
    assert cli_main(["--config", str(CONFIG), *base, "--plant", "Nobody=x"]) == 2
    assert "not in the roster" in capsys.readouterr().err
    assert cli_main(["--config", str(CONFIG), *base, "--plant", "Ada"]) == 2
    assert "NAME=BULLET" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli_main(["--config", str(CONFIG), *base, "--arms", "sideways"])
    assert exc.value.code == 2
    assert not out.exists(), "nothing ran"


def test_void_generations_is_dispatched_to_this_module(capsys: pytest.CaptureFixture[str]) -> None:
    from void.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["generations", "--help"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("usage: void generations")


# --- worlds -----------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cli_runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """Three generations of seeds 1 and 2 (oblique, one-day worlds) into ``a``, again into ``b``, then into ``a``."""
    root = tmp_path_factory.mktemp("e3a")
    args = ["--config", str(CONFIG), "--generations", "3", "--seeds", "1-2", "--days", str(DAYS), "--quiet"]
    rcs = [cli_main([*args, "--out", str(root / "a")])]
    first = _json_files(root / "a")
    rcs.append(cli_main([*args, "--out", str(root / "b")]))
    rcs.append(cli_main([*args, "--out", str(root / "a")]))  # every generation is already there: read back
    return {"root": root, "rcs": rcs, "first": first}


def test_three_generations_of_two_seeds_are_deterministic(cli_runs: dict[str, object]) -> None:
    root = cli_runs["root"]
    assert isinstance(root, Path)
    assert cli_runs["rcs"] == [0, 0, 0]
    a, b = _json_files(root / "a"), _json_files(root / "b")
    assert a == b and a == cli_runs["first"]
    exp = Path("exp_mem_e3a") / "oblique"
    assert str(exp / SUMMARY_FILE) in a
    for seed in (1, 2):
        assert all(str(exp / f"seed_{seed}" / f"gen_{g}" / MANIFEST_FILE) in a for g in range(3))
        rows = json.loads(a[str(exp / f"seed_{seed}" / GENERATIONS_FILE)])
        assert [r["generation"] for r in rows] == [0, 1, 2]
        assert all(set(ROW_FIELDS) <= set(r) for r in rows)
        assert all(r["cohort_mean_balance"] > 0 and 0 <= r["monoculture_index"] <= 1 for r in rows)
        assert rows[0]["policy_similarity_to_gen0"] < 1  # the diverse seed: generation 0 is not one text
    summary = json.loads(a[str(exp / SUMMARY_FILE)])
    assert (summary["arm"], summary["seeds"], summary["complete_seeds"], summary["generations"]) == ("oblique", [1, 2], [1, 2], 3)
    bal = summary["slopes"]["cohort_mean_balance"]
    assert bal["n_seeds"] == 2 and bal["n_obs"] == 6 and bal["slope"] is not None and None not in bal["ci"]
    assert "cohort_audit_accuracy" in summary["slopes"]  # None in one-day worlds: the exam asks two days back
    assert [g["generation"] for g in summary["by_generation"]] == [0, 1, 2]


def test_generation_worlds_carry_vaults_and_policies_with_provenance(cli_runs: dict[str, object]) -> None:
    root = cli_runs["root"]
    assert isinstance(root, Path)
    seed_dir = root / "a" / "exp_mem_e3a" / "oblique" / "seed_1"
    m0 = json.loads((seed_dir / "gen_0" / MANIFEST_FILE).read_text())
    m1 = json.loads((seed_dir / "gen_1" / MANIFEST_FILE).read_text())
    assert (m0["run_id"], m1["run_id"], m1["previous_run_id"]) == ("exp_mem_e3a_g0-s1", "exp_mem_e3a_g1-s1", "exp_mem_e3a_g0-s1")
    assert len(m0["slots"]) == 12 and sum(s["survives"] for s in m0["slots"]) == 6
    ranked0 = sorted(m0["slots"], key=lambda s: s["rank"])
    survivors0 = ranked0[:6]
    assert [s["individual"] for s in survivors0] == m0["survivors"]
    slots1 = {s["slot"]: s for s in m1["slots"]}
    assert {s for s, v in slots1.items() if v["role"] == "survivor"} == {s["slot"] for s in survivors0}
    children = sorted((v for v in slots1.values() if v["role"] == "child"), key=lambda v: v["slot"])
    assert [c["parent"] for c in children] == [s["individual"] for s in survivors0]  # round-robin by rank
    with _db(seed_dir / "gen_0") as db0:
        gen0_ids = {r["note_id"] for r in db0.execute("SELECT note_id FROM notes")}
        lived = {aid: sorted((r["hop"], r["importance"]) for r in db0.execute(
            "SELECT hop, importance, tags FROM notes WHERE agent_id=? AND archived=0", (aid,))
            if not {"entity", "probe"} & set(json.loads(r["tags"])))
            for aid in (s["agent_id"] for s in survivors0)}
    carried = 0
    with _db(seed_dir / "gen_1") as db:
        for s0 in survivors0:
            s1 = slots1[s0["slot"]]
            assert s1["individual"] == s0["individual"] and s1["carried_from"] == {"run_id": m0["run_id"], "agent_id": s0["agent_id"]}
            notes = db.execute("SELECT * FROM notes WHERE agent_id=? AND channel='inherited'", (s1["agent_id"],)).fetchall()
            assert len(notes) == s1["notes_carried"]
            assert sorted((n["hop"], n["importance"]) for n in notes) == lived[s0["agent_id"]]  # every live note, as it was
            carried += len(notes)
            for n in notes:
                prefix = f"transplant:{m0['run_id']}:"
                assert n["origin_note_id"].startswith(prefix) and n["origin_note_id"][len(prefix):] in gen0_ids
                assert n["source_agent_id"] is None and n["transfer_tick"] == 0
                assert "transplanted" in json.loads(n["tags"]) and n["origin_generation"] == 0
            pv = db.execute("SELECT * FROM policy_versions WHERE agent_id=? ORDER BY version DESC LIMIT 1", (s1["agent_id"],)).fetchone()
            assert (pv["source"], pv["source_agent_id"], pv["text"]) == ("inherited", None, s0["policy_end"])
        agent_of = {v["slot"]: v["agent_id"] for v in slots1.values()}
        adapted = 0
        for c in children:
            parent_slot = next(s for s, v in slots1.items() if v["individual"] == c["parent"])
            pv = db.execute("SELECT * FROM policy_versions WHERE agent_id=? ORDER BY version DESC LIMIT 1", (c["agent_id"],)).fetchone()
            assert pv["source_agent_id"] == agent_of[parent_slot] == c["parent_agent_id"]
            parent_text = slots1[parent_slot]["policy_start"]
            if c["added_bullet"] is None:
                assert (pv["source"], pv["text"]) == ("inherited", parent_text)
            else:
                adapted += 1
                assert pv["source"] == "adapted" and pv["text"] == append_bullet(parent_text, c["added_bullet"])
                assert c["added_bullet"] not in bullets(parent_text)
            assert c["policy_start"] == pv["text"] and c["notes_carried"] == 0
        assert adapted >= 1 and carried >= 1
        assert db.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 12  # no birth inside a generation world


@pytest.fixture(scope="module")
def planted(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """MARKER planted in the founder that ranks first in an unplanted generation 0, on the default seed policy (every
    founder holds the same text, so the marker is the one bullet a child can be missing). Two-day worlds with the
    exam one day back, so the audit accuracy is measured."""
    root = tmp_path_factory.mktemp("planted")
    overrides = {"memory": {"policy": {"seed": "default"}, "exam": {"delay_days": 1}}}
    ok, _ = run_seed(CONFIG, "oblique", 1, 1, root / "probe", days=2, overrides=overrides, log=None)
    assert ok
    probe = json.loads((root / "probe" / "exp_mem_e3a" / "oblique" / "seed_1" / "gen_0" / MANIFEST_FILE).read_text())
    top = next(s["name"] for s in probe["slots"] if s["rank"] == 1)
    out: dict[str, object] = {"root": root, "top": top}
    for arm, gens in (("oblique", 3), ("vertical", 2)):
        ok, rows = run_seed(CONFIG, arm, 1, gens, root / "run", days=2, plant={top: MARKER}, overrides=overrides, log=None)
        assert ok
        out[arm] = rows
    return out


def test_a_planted_bullet_in_a_survivor_rises_in_share_under_the_oblique_arm(planted: dict[str, object]) -> None:
    root, top = planted["root"], planted["top"]
    assert isinstance(root, Path)
    for arm in ARMS:
        m0 = json.loads((root / "run" / "exp_mem_e3a" / arm / "seed_1" / "gen_0" / MANIFEST_FILE).read_text())
        bearer = next(s for s in m0["slots"] if s["name"] == top)
        assert bearer["planted_bullet"] == MARKER and MARKER in bullets(bearer["policy_start"])
        assert bearer["survives"], "the planted founder still survives generation 0"
    oblique, vertical = planted["oblique"], planted["vertical"]
    assert isinstance(oblique, list) and isinstance(vertical, list)
    share = [r["planted_share"][MARKER] for r in oblique]
    # generation 1: the bearer plus all six children (the marker is the only survivor bullet a non-bearer lacks)
    assert share[0] == pytest.approx(1 / 12) and share[1] == pytest.approx(7 / 12)
    assert share[2] >= share[1] > share[0]
    # vertical: only the bearer and its own child hold it
    assert [r["planted_share"][MARKER] for r in vertical] == [pytest.approx(1 / 12), pytest.approx(2 / 12)]
    assert all(r["cohort_audit_accuracy"] is not None for r in oblique)
    assert oblique[0] == vertical[0], "generation 0 is the same world in both arms"
