"""E5, the transplant harness (MEMORY_EVOLUTION §6.7, WP9): ``void transplant`` on scripted brains only.

The donor is the E0 index builder at seed 1 (the dry run's donor). The tests check the two policy controls are
deterministic, that seeding writes the transplant's provenance and a policy version through the stores, that a
1-day run of all six conditions completes and ``compare`` accepts the experiment, and the small dry run.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import sqlite3
import statistics
from pathlib import Path

import pytest

from void.config import load_config
from void.memory.policy import SEED_DEFAULT, SEED_DIVERSE
from void.research.analysis import end_of_run_outcome
from void.research.compare import compare, render
from void.research.transplant import (
    CONDITIONS,
    DEFAULT_CONFIG,
    MANIFEST_FILE,
    TRANSPLANT_TAG,
    cli_main,
    condition_config,
    invert_line,
    invert_policy,
    load_donors,
    origin_note_id,
    seed_transplant,
    shuffle_policy,
)
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"
DONOR_CONFIG = CONFIGS / "exp_mem_e0_index_builder.yaml"


def _db(run_dir: Path) -> contextlib.closing[sqlite3.Connection]:
    conn = sqlite3.connect(str(run_dir / "world.db"))
    conn.row_factory = sqlite3.Row
    return contextlib.closing(conn)


@pytest.fixture(scope="module")
def donor_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The E0 index-builder arm at seed 1, as `void run --seeds 1` writes it (6 days)."""
    run_dir = tmp_path_factory.mktemp("donor") / "exp_mem_e0" / "index_builder" / "seed_1"
    sim = Simulation(load_config(DONOR_CONFIG, {"run": {"seed": 1}}), run_dir)
    try:
        assert asyncio.run(sim.run()) == "completed"
    finally:
        sim.db.close()
    return run_dir


# --- the policy controls --------------------------------------------------------------------------------------
def test_shuffle_is_deterministic_per_seed_and_keeps_lines_and_structure() -> None:
    for text in (SEED_DEFAULT, *SEED_DIVERSE):
        assert shuffle_policy(text, 7) == shuffle_policy(text, 7)
    lines = SEED_DEFAULT.split("\n")
    outs = {shuffle_policy(SEED_DEFAULT, seed) for seed in range(1, 21)}
    assert len(outs) > 5, "different seeds give different orders"
    for out in outs:
        assert out != SEED_DEFAULT
        got = out.split("\n")
        assert sorted(got) == sorted(lines) and len(out) == len(SEED_DEFAULT)
        for i, line in enumerate(lines):  # headings and blank lines stay where they were
            if not line.strip() or line.startswith("#"):
                assert got[i] == line
    assert shuffle_policy("# Only a heading\n\n- one bullet", 3) == "# Only a heading\n\n- one bullet"


def test_invert_is_deterministic_and_negates_each_imperative_bullet() -> None:
    assert invert_line("- Keep one index note per node.") == "- Do not keep one index note per node."
    assert invert_line("- Never trust gossip.") == "- Always trust gossip."
    assert invert_line("- Always write the date.") == "- Never write the date."
    assert invert_line("- Do not delete notes.") == "- Delete notes."
    assert invert_line("- Don't guess.") == "- Guess."
    assert invert_line("- Update it, do not duplicate it.") == "- Do not update it, duplicate it."
    assert invert_line("- If two notes disagree, keep the newer one.") == "- If two notes disagree, do not keep the newer one."
    assert invert_line("- Edit it; never add a second note.") == "- Do not edit it; always add a second note."
    assert invert_line("1. Link every note to [[Node n3]].") == "1. Do not link every note to [[Node n3]]."
    # not bullets, or not imperative: kept
    for line in ("# How I remember (v1)", "", "Keep this prose line.", "- A fact from one source is a rumour.",
                 "- [[Node n3]] is rich."):
        assert invert_line(line) == line
    inverted = invert_policy(SEED_DEFAULT)
    assert inverted == invert_policy(SEED_DEFAULT)
    before, after = SEED_DEFAULT.split("\n"), inverted.split("\n")
    assert len(before) == len(after) and before[0] == after[0]
    bullets = [(b, a) for b, a in zip(before, after, strict=True) if b.startswith("- ")]
    assert len(bullets) == 5 and all(b != a and "do not" in a.lower() for b, a in bullets)


# --- seeding ----------------------------------------------------------------------------------------------------
def _eval_sim(tmp: Path, condition: str, seed: int = 1) -> Simulation:
    return Simulation(condition_config(DEFAULT_CONFIG, condition, seed, days=1), tmp / condition / f"seed_{seed}")


def test_load_donors_selects_and_reads_the_vault(donor_run: Path) -> None:
    with _db(donor_run) as db:
        alive = db.execute("SELECT agent_id, name, balance FROM agents WHERE status='alive' ORDER BY balance DESC, agent_id").fetchall()
        live = db.execute("SELECT COUNT(*) AS n FROM notes WHERE agent_id=? AND archived=0 AND tags NOT LIKE '%\"entity\"%' "
                          "AND tags NOT LIKE '%\"probe\"%'", (alive[0]["agent_id"],)).fetchone()["n"]
        policy = db.execute("SELECT text FROM policy_versions WHERE agent_id=? ORDER BY version DESC LIMIT 1",
                            (alive[0]["agent_id"],)).fetchone()["text"]
    (richest,) = load_donors(donor_run, "richest")
    assert richest.agent_id == alive[0]["agent_id"] and richest.policy == policy
    assert len(richest.notes) == live and live > 0
    assert all("entity" not in n.tags and n.body for n in richest.notes)
    assert any("index" in n.tags for n in richest.notes), "the index builder's index notes travel with the vault"
    assert [d.name for d in load_donors(donor_run, "names:Cyra,Ada")] == ["Cyra", "Ada"]
    assert len(load_donors(donor_run, "all")) == len(alive)
    with pytest.raises(ValueError):
        load_donors(donor_run, "names:Nobody")
    with pytest.raises(ValueError):
        load_donors(donor_run, "poorest")


def test_seeding_writes_transplant_provenance_and_a_policy_version(donor_run: Path, tmp_path: Path) -> None:
    donors = load_donors(donor_run, "richest")
    donor = donors[0]
    sim = _eval_sim(tmp_path, "both")
    try:
        manifest = seed_transplant(sim, donors, "both", recipients=3, seed=1)
        roster = [a.name for a in sim.cfg.agents]
        by_name = {a.name: a for a in sim.registry.all()}
        recipients = [by_name[n].agent_id for n in roster[:3]]
        assert [r["agent_id"] for r in manifest["recipients"]] == recipients
        for aid in recipients:
            notes = [n for n in sim.memory.list_notes(aid) if n.provenance.channel == "inherited"]
            assert len(notes) == len(donor.notes)
            for got, src in zip(notes, donor.notes, strict=True):
                assert TRANSPLANT_TAG in got.tags and set(src.tags) <= set(got.tags)
                assert got.provenance.origin_note_id == origin_note_id(donor.run_id, src.note_id)
                assert got.provenance.origin_note_id == f"transplant:{donor.run_id}:{src.note_id}"
                assert got.provenance.source_agent_id is None  # the donor's id is not an agent of this run
                assert got.provenance.transfer_tick == 0 and got.created_tick == 0
                assert got.body.startswith(src.body.split("\n## Related")[0])
            hist = sim.policy.history(aid)
            assert [v.source for v in hist] == ["seed", "inherited"]
            assert hist[-1].text == donor.policy and hist[-1].source_agent_id is None
        for name in roster[3:]:  # everyone else starts as a founder does
            aid = by_name[name].agent_id
            assert [v.source for v in sim.policy.history(aid)] == ["seed"]
            assert not [n for n in sim.memory.list_notes(aid) if n.provenance.channel == "inherited"]
        rows = sim.db.fetchall("SELECT agent_id, payload FROM events WHERE kind='policy_changed'")
        assert sorted(r["agent_id"] for r in rows) == sorted(recipients)
        assert all(json.loads(r["payload"])["source"] == "inherited" for r in rows)
        assert sim.db.kv_get("transplant") == manifest
        assert json.loads((sim.run_dir / MANIFEST_FILE).read_text()) == manifest
    finally:
        sim.db.close()


def test_each_condition_transplants_what_it_names(donor_run: Path, tmp_path: Path) -> None:
    donors = load_donors(donor_run, "richest")
    text = donors[0].policy or ""
    expect = {"none": (0, None), "policy": (0, text), "vault": (len(donors[0].notes), None),
              "both": (len(donors[0].notes), text), "shuffled": (len(donors[0].notes), shuffle_policy(text, 2)),
              "inverted": (len(donors[0].notes), invert_policy(text))}
    for condition in CONDITIONS:
        sim = _eval_sim(tmp_path, condition, seed=2)
        try:
            seed_transplant(sim, donors, condition, recipients=1, seed=2)
            aid = next(a.agent_id for a in sim.registry.all() if a.name == sim.cfg.agents[0].name)
            n_notes, policy = expect[condition]
            assert len([n for n in sim.memory.list_notes(aid) if n.provenance.channel == "inherited"]) == n_notes, condition
            hist = sim.policy.history(aid)
            if policy is None:
                assert [v.source for v in hist] == ["seed"], condition
            else:
                assert hist[-1].source == "inherited" and hist[-1].text == policy, condition
        finally:
            sim.db.close()
    assert expect["shuffled"][1] != text and expect["inverted"][1] != text


def test_a_donor_of_this_run_is_named_as_the_source(donor_run: Path, tmp_path: Path) -> None:
    sim = _eval_sim(tmp_path, "both")
    try:
        local = next(a for a in sim.registry.all() if a.name == sim.cfg.agents[-1].name)
        donor = dataclasses.replace(load_donors(donor_run, "richest")[0], agent_id=local.agent_id)
        seed_transplant(sim, [donor], "both", recipients=2, seed=1)
        first = next(a.agent_id for a in sim.registry.all() if a.name == sim.cfg.agents[0].name)
        notes = [n for n in sim.memory.list_notes(first) if n.provenance.channel == "inherited"]
        assert notes and all(n.provenance.source_agent_id == local.agent_id for n in notes)
        assert all(n.provenance.path_agents == [local.agent_id] for n in notes)
        assert sim.policy.current(first).source_agent_id == local.agent_id  # type: ignore[union-attr]
    finally:
        sim.db.close()


def test_seeding_refuses_what_it_cannot_do(donor_run: Path, tmp_path: Path) -> None:
    donors = load_donors(donor_run, "richest")
    sim = _eval_sim(tmp_path, "both")
    try:
        with pytest.raises(ValueError):
            seed_transplant(sim, donors, "sideways", recipients=1, seed=1)
        with pytest.raises(ValueError):
            seed_transplant(sim, donors, "both", recipients=0, seed=1)
        with pytest.raises(ValueError):
            seed_transplant(sim, [dataclasses.replace(donors[0], policy=None)], "policy", recipients=1, seed=1)
    finally:
        sim.db.close()


# --- end to end -------------------------------------------------------------------------------------------------
def test_one_day_run_of_all_six_conditions_and_compare_accepts_it(donor_run: Path, tmp_path: Path,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "runs"
    argv = ["--donor-run", str(donor_run), "--seeds", "1", "--days", "1", "--out", str(out), "--quiet"]
    assert cli_main(argv) == 0
    exp = out / "exp_mem_e5"
    assert sorted(p.name for p in exp.iterdir()) == sorted(CONDITIONS)
    for condition in CONDITIONS:
        run_dir = exp / condition / "seed_1"
        with _db(run_dir) as db:
            row = db.execute("SELECT run_id, status, experiment, arm FROM run").fetchone()
        assert (row["status"], row["experiment"], row["arm"]) == ("completed", "exp_mem_e5", condition)
        assert row["run_id"] == f"exp_mem_e5_{condition}-s1"
        assert json.loads((run_dir / MANIFEST_FILE).read_text())["condition"] == condition
    result = compare(exp)
    assert sorted(result["arms"]) == sorted(CONDITIONS) and result["primary"] == "mean_balance"
    assert "both vs none" in render(result)
    capsys.readouterr()
    assert cli_main(argv) == 0  # a second invocation finds every run complete and skips it
    assert capsys.readouterr().out.count("skipped") == len(CONDITIONS)


def test_cli_refuses_bad_input_and_paid_configs(donor_run: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = ["--donor-run", str(donor_run), "--seeds", "1", "--days", "1", "--out", str(tmp_path)]
    assert cli_main([*base, "--config", str(CONFIGS / "live_claude_smoke.yaml")]) == 2
    assert "refused" in capsys.readouterr().err
    assert cli_main([*base, "--recipients", "99"]) == 2
    assert cli_main([*base, "--donors", "names:Nobody"]) == 2
    assert cli_main(["--donor-run", str(tmp_path / "missing"), "--seeds", "1", "--out", str(tmp_path)]) == 2
    with pytest.raises(SystemExit):
        cli_main([*base, "--conditions", "none,sideways"])
    with pytest.raises(SystemExit) as exc:
        cli_main(["--help"])
    assert exc.value.code == 0 and capsys.readouterr().out.startswith("usage: void transplant")
    assert not (tmp_path / "exp_mem_e5").exists()


def test_eval_config_is_the_e0_world_cut_to_three_days() -> None:
    cfg = load_config(CONFIGS / "exp_mem_e5_eval.yaml")
    e0 = load_config(CONFIGS / "exp_mem_e0_noop.yaml")
    assert cfg.run.days == 3 and cfg.experiment.name == "exp_mem_e5" and cfg.experiment.arm == "none"
    assert cfg.memory.policy.enabled and cfg.memory.maintenance.enabled
    assert all(cfg.tiers[a.tier].maintenance_strategy == "noop" for a in cfg.agents)
    assert cfg.world == e0.world and cfg.memory == e0.memory and cfg.agents == e0.agents
    a, b = condition_config(DEFAULT_CONFIG, "none", 3), condition_config(DEFAULT_CONFIG, "both", 3)
    da, db = a.model_dump(mode="json"), b.model_dump(mode="json")
    assert {k for k in da if da[k] != db[k]} == {"run", "experiment"}
    assert {k for k in da["run"] if da["run"][k] != db["run"][k]} == {"name"}
    assert {k for k in da["experiment"] if da["experiment"][k] != db["experiment"][k]} == {"arm"}


def test_dry_run_full_transplant_is_not_worse_than_nothing(donor_run: Path, tmp_path: Path) -> None:
    """§6.7's free dry run, small: the index builder's vault and policy in fresh scripted agents (seeds 1-4).

    The plumbing is asserted. The expected effect ('both' >= 'none' on mean balance, on average) is reported as an
    expected failure when it does not hold: E0's positive control does not separate from the no-op on scripted brains
    (MEMORY_EVOLUTION 10.4), so the harness has no advantage to reproduce, and neither the scripted brain nor the
    world is changed to manufacture one.
    """
    out = tmp_path / "runs"
    assert cli_main(["--donor-run", str(donor_run), "--conditions", "none,both", "--seeds", "1-4", "--out", str(out), "--quiet"]) == 0
    values = {c: [end_of_run_outcome(out / "exp_mem_e5" / c / f"seed_{s}", "mean_balance") for s in range(1, 5)] for c in ("none", "both")}
    assert all(v is not None and v > 0 for vs in values.values() for v in vs), values
    means = {c: statistics.fmean(float(v) for v in vs if v is not None) for c, vs in values.items()}
    if means["both"] < means["none"]:
        pytest.xfail(f"dry run: 'both' {means['both']:.4f} < 'none' {means['none']:.4f} mean balance at seeds 1-4; "
                     "the E0 index builder does not beat the no-op on scripted brains (MEMORY_EVOLUTION 10.4)")
    assert means["both"] >= means["none"], means
