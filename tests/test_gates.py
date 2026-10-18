"""Gates G0 and G1 from runs (``void/research/gates.py``, ``scripts/gates.py``; MEMORY_EVOLUTION 5.2, 6.2, 10.2) and
the 10.4 diagnosis (``scripts/mem_diagnosis.py``).

Five layers:

* the four G0 conditions on fixed per-seed outcomes (:class:`gates.Paired` built by hand): PASS, FAIL and MISSING for
  each, the margin's source, the power requirement, and that the interval is the seeded paired bootstrap compare uses;
  and each condition reading its margin (C2 its power) from a declared hypothesis;
* the script with the loader monkeypatched: one line per condition plus G0, the exit codes 0 / 1 / 2, ``--json``,
  ``--live`` and the argument checks;
* the real loader on 1-day scripted runs of every arm at two seeds: a verdict line per condition without a crash,
  the same numbers as ``compare``, refusal of incomplete and incomparable runs, ``--allow-incomplete``, live mode, and
  the margin sources naming the config files that declare the hypotheses;
* G1 on synthetic E1 run directories (the E1 arm configs, loaded and never run, with planted ``maintain`` commands and
  planted lineage fitness): one passing and one failing (and one missing) case for G1a and for G1b, and ``--g1``
  through the script, exiting 0 only when every condition passes and naming the ones that do not;
* the diagnosis table and the sweep on the 1-day scripted runs.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_experiments import CONFIGS, REPO, run_arm

from void.config import load_config
from void.db import Database
from void.research import gates
from void.research.analysis import end_of_run_outcome
from void.research.compare import compare
from void.research.stats import bootstrap_ci, power_paired

SEEDS = (1, 2)
SHORT_RUN = {"days": 1, "ticks_per_day": 6}
E0_ARMS = ("noop", "index_builder", "hoarder", "decoy")
NOISE = [((i * 7) % 11 - 5) / 5 for i in range(40)]  # a fixed zero-ish pattern in [-1, 1]


def _pairs(effect: float, n: int = 20, spread: float = 0.01, base: float = 1.0) -> list[tuple[float, float]]:
    """``n`` (a, b) pairs whose differences are ``effect`` plus a fixed pattern of size ``spread``."""
    return [(base + 0.01 * i, base + 0.01 * i + effect + spread * NOISE[i]) for i in range(n)]


def _paired(arm_a: str, arm_b: str, outcomes: dict[str, list[tuple[float | None, float | None]]], *,
            experiment: str = "exp", missing: str | None = None, hypotheses: list[dict[str, Any]] | None = None) -> gates.Paired:
    p = gates.Paired(experiment=experiment, exp_dir=f"/runs/{experiment}", arm_a=arm_a, arm_b=arm_b, missing=missing,
                     hypotheses=list(hypotheses or []))
    p.values = {o: {i + 1: ab for i, ab in enumerate(pairs)} for o, pairs in outcomes.items()}
    return p


def _dep(effect: float, **kw: Any) -> gates.Paired:
    return _paired("amnesic", "mem", {"mean_balance": _pairs(effect)}, experiment="exp_mem_dep", **kw)


def _e0(arm: str, effect: float, chars: list[tuple[float | None, float | None]] | None = None, n: int = 20) -> gates.Paired:
    outcomes = {"mean_balance": _pairs(effect, n=n)}
    if chars is not None:
        outcomes["policy_chars"] = chars
    return _paired("noop", arm, outcomes, experiment="exp_mem_e0")


# --- C1: memory dependence ---------------------------------------------------------------------------------------
def test_c1_passes_fails_and_is_missing() -> None:
    ok = gates.condition_dependence(_dep(0.05))
    assert ok["verdict"] == "PASS" and ok["n"] == 20 and ok["ci95"][0] > 0
    assert ok["margin"] == 0.0 and ok["margin_source"] == gates.PROPOSAL_MARGIN_SOURCE and "[assumption]" in ok["margin_source"]
    bad = gates.condition_dependence(_dep(-0.03))
    assert bad["verdict"] == "FAIL" and bad["estimate"] < 0 and "is not above the margin" in bad["reason"]
    noisy = gates.condition_dependence(_paired("amnesic", "mem", {"mean_balance": _pairs(0.002, spread=0.05)}))
    assert noisy["estimate"] > 0 and noisy["ci95"][0] <= 0 and noisy["verdict"] == "FAIL"  # above 0, but the CI is not
    gone = gates.condition_dependence(_dep(0.05, missing="no runs for arm 'mem' under /runs/exp_mem_dep"))
    assert gone["verdict"] == "MISSING" and "no runs" in gone["reason"]
    one = gates.condition_dependence(_paired("amnesic", "mem", {"mean_balance": _pairs(0.05, n=1)}))
    assert one["verdict"] == "MISSING" and "fewer than two" in one["reason"]


def test_c1_reads_the_margin_from_a_declared_hypothesis() -> None:
    hyp = {"name": "dependence", "arm_a": "amnesic", "arm_b": "mem", "outcome": "mean_balance", "kind": "superiority",
           "direction": "greater", "margin": 0.08, "margin_unit": "abs"}
    res = gates.condition_dependence(_dep(0.05, hypotheses=[hyp]))
    assert res["margin"] == 0.08 and res["margin_source"] == "experiment.hypotheses 'dependence'"
    assert res["ci95"][0] > 0 and res["verdict"] == "FAIL"  # clears 0, not the declared margin
    assert gates.condition_dependence(_dep(0.12, hypotheses=[hyp]))["verdict"] == "PASS"
    # the reverse orientation with direction less is the same contrast
    rev = {**hyp, "name": "rev", "arm_a": "mem", "arm_b": "amnesic", "direction": "less", "margin": 0.02}
    assert gates.dependence_margin([rev], "amnesic", "mem") == (0.02, "abs", "experiment.hypotheses 'rev'")
    # other contrasts, outcomes, kinds and directions are not the dependence margin
    others = [{**hyp, "outcome": "exam_score"}, {**hyp, "kind": "equivalence"}, {**hyp, "direction": "less"},
              {**hyp, "arm_b": "other"}]
    assert gates.dependence_margin(others, "amnesic", "mem") == (0.0, "abs", gates.PROPOSAL_MARGIN_SOURCE)
    # a margin in SD units is scaled by the SD of the paired differences
    sd = gates.condition_dependence(_dep(0.05, hypotheses=[{**hyp, "margin": 0.5, "margin_unit": "sd"}]))
    diffs = [b - a for a, b in _pairs(0.05)]
    mean = sum(diffs) / len(diffs)
    expected = 0.5 * (sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)) ** 0.5
    assert sd["margin_unit"] == "sd" and sd["margin_abs"] == pytest.approx(expected) and sd["verdict"] == "PASS"
    assert "SD =" in gates.render({"mode": "scripted", "conditions": [{**sd, "label": "C1"}],
                                   "g0": {"verdict": "PASS", "exit_code": 0, "by_condition": {"C1": "PASS"}}})


# --- C2: positive control with power ---------------------------------------------------------------------------
def test_c2_passes_fails_on_effect_or_power_and_is_missing() -> None:
    ok = gates.condition_index_builder(_e0("index_builder", 0.05))
    assert ok["verdict"] == "PASS" and ok["power"] >= 0.8 and ok["power_method"] == "t"
    assert ok["power"] == pytest.approx(power_paired(ok["d_z"], 20, 0.05, "t"))
    tie = gates.condition_index_builder(_e0("index_builder", 0.0))
    assert tie["verdict"] == "FAIL" and "does not lie above 0" in tie["reason"]
    # every difference positive, so the interval clears 0, but three pairs are far from power 0.8
    weak = gates.condition_index_builder(_paired("noop", "index_builder", {"mean_balance": [(1.0, 1.1), (1.0, 1.12), (1.0, 1.3)]}))
    assert weak["ci95"][0] > 0 and weak["estimate"] > 0 and weak["power"] < 0.8
    assert weak["verdict"] == "FAIL" and "is below 0.8" in weak["reason"]
    # identical differences: no variation, the effect cannot be missed
    exact = gates.condition_index_builder(_paired("noop", "index_builder", {"mean_balance": [(1.0, 1.1), (2.0, 2.1)]}))
    assert exact["d_z"] is None and exact["power"] == 1.0 and exact["verdict"] == "PASS"
    flat = gates.condition_index_builder(_paired("noop", "index_builder", {"mean_balance": [(1.0, 1.0), (2.0, 2.0)]}))
    assert flat["power"] is None and flat["verdict"] == "FAIL" and "undefined" in flat["reason"]
    gone = gates.condition_index_builder(_paired("noop", "index_builder", {}, missing="arm 'index_builder' has no completed runs (2 incomplete)"))
    assert gone["verdict"] == "MISSING" and "no completed runs" in gone["reason"]


# --- C3: negative control ----------------------------------------------------------------------------------------
def test_c3_passes_fails_and_is_missing() -> None:
    assert gates.condition_hoarder(_e0("hoarder", -0.05))["verdict"] == "PASS"
    above = gates.condition_hoarder(_e0("hoarder", 0.05))
    assert above["verdict"] == "FAIL" and "is not below 0" in above["reason"]
    unsure = gates.condition_hoarder(_paired("noop", "hoarder", {"mean_balance": _pairs(-0.002, spread=0.05)}))
    assert unsure["estimate"] < 0 and unsure["ci95"][1] >= 0 and unsure["verdict"] == "FAIL"  # ranked below, not significantly
    assert gates.condition_hoarder(_paired("noop", "hoarder", {}, missing="no runs for arm 'hoarder'"))["verdict"] == "MISSING"


# --- C4: metric check -------------------------------------------------------------------------------------------
def test_c4_passes_fails_on_either_outcome_and_is_missing() -> None:
    longer = _pairs(800.0, spread=20.0, base=350.0)
    same = [(350.0, 350.0)] * 20
    ok = gates.condition_decoy(_e0("decoy", -0.03, longer))
    assert ok["verdict"] == "PASS" and ok["text_metric"]["ci95"][0] > 0
    tie = gates.condition_decoy(_e0("decoy", 0.0, longer))
    assert tie["verdict"] == "PASS"  # not separated on fitness is what the check asks
    richer = gates.condition_decoy(_e0("decoy", 0.05, longer))
    assert richer["verdict"] == "FAIL" and "mean_balance: significantly above" in richer["reason"]
    plain = gates.condition_decoy(_e0("decoy", -0.03, same))
    assert plain["verdict"] == "FAIL" and "policy_chars: not significantly above" in plain["reason"]
    no_text = gates.condition_decoy(_e0("decoy", -0.03, [(None, None)] * 20))
    assert no_text["verdict"] == "MISSING" and "policy_chars" in no_text["reason"] and "20 without a value" in no_text["reason"]
    gone = gates.condition_decoy(_paired("noop", "decoy", {}, missing="refused: incomplete runs: decoy seed 3: run.status 'capped_total'"))
    assert gone["verdict"] == "MISSING" and gone["reason"].startswith("refused")


# --- statistics -------------------------------------------------------------------------------------------------
def test_the_interval_is_the_seeded_paired_bootstrap_and_deterministic() -> None:
    p = _dep(0.01, hypotheses=None)
    first, second = gates.condition_dependence(p), gates.condition_dependence(_dep(0.01))
    assert first["ci95"] == second["ci95"] and first["estimate"] == second["estimate"]
    diffs = [b - a for a, b in p.pairs("mean_balance")]
    assert first["ci95"] == bootstrap_ci(diffs, n_boot=gates.N_BOOT, seed=gates.BOOT_SEED)


def test_overall_and_exit_codes() -> None:
    assert gates.overall(["PASS", "PASS"]) == "PASS"
    assert gates.overall(["PASS", "MISSING"]) == "MISSING"
    assert gates.overall(["MISSING", "FAIL", "PASS"]) == "FAIL"  # a failure is decisive whatever is missing
    assert gates.EXIT_CODES == {"PASS": 0, "FAIL": 1, "MISSING": 2}


# --- the script with the loader monkeypatched ----------------------------------------------------------------------
def _load_script():
    spec = importlib.util.spec_from_file_location("gates_script", REPO / "scripts" / "gates.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LONGER = _pairs(800.0, spread=20.0, base=350.0)
ALL_PASS: dict[tuple[str, str], dict[str, list[tuple[float, float]]]] = {
    ("exp_mem_dep", "mem"): {"mean_balance": _pairs(0.05)},
    ("exp_mem_e0", "index_builder"): {"mean_balance": _pairs(0.05)},
    ("exp_mem_e0", "hoarder"): {"mean_balance": _pairs(-0.05)},
    ("exp_mem_e0", "decoy"): {"mean_balance": _pairs(-0.03), "policy_chars": LONGER},
}


def _fake_loader(table: dict[tuple[str, str], dict[str, list[tuple[float, float]]]], calls: list[tuple[str, str, str]] | None = None):
    def fake(exp_dir: str | Path, arm_a: str, arm_b: str, outcomes: list[str], *, allow_incomplete: bool = False) -> gates.Paired:
        name = Path(exp_dir).name
        if calls is not None:
            calls.append((str(exp_dir), arm_a, arm_b))
        spec = table.get((name, arm_b))
        if spec is None:
            return _paired(arm_a, arm_b, {}, experiment=name, missing=f"no runs for arm {arm_b!r} under {exp_dir}")
        return _paired(arm_a, arm_b, {o: spec[o] for o in outcomes}, experiment=name)
    return fake


@pytest.mark.parametrize("change, code, verdicts", [
    ({}, 0, ["PASS"] * 4),
    ({("exp_mem_dep", "mem"): {"mean_balance": _pairs(-0.03)}}, 1, ["FAIL", "PASS", "PASS", "PASS"]),
    ({("exp_mem_e0", "hoarder"): None}, 2, ["PASS", "PASS", "MISSING", "PASS"]),
    ({("exp_mem_dep", "mem"): {"mean_balance": _pairs(-0.03)}, ("exp_mem_e0", "decoy"): None}, 1, ["FAIL", "PASS", "PASS", "MISSING"]),
])
def test_script_prints_a_line_per_condition_and_exits_by_verdict(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                                                 change: dict, code: int, verdicts: list[str]) -> None:
    table = {k: v for k, v in {**ALL_PASS, **change}.items() if v is not None}
    monkeypatch.setattr(gates, "paired_outcomes", _fake_loader(table))
    assert _load_script().main(["/runs"]) == code
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 5
    for line, cid, verdict in zip(lines, ("C1", "C2", "C3", "C4"), verdicts, strict=False):
        assert line.split()[:2] == [cid, verdict], line
    g0 = {0: "PASS", 1: "FAIL", 2: "MISSING"}[code]
    assert lines[4].startswith(f"G0 {g0}") and lines[4].endswith(f"(exit {code})")


def test_json_output_shape(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(gates, "paired_outcomes", _fake_loader({**ALL_PASS, ("exp_mem_dep", "mem"): {"mean_balance": _pairs(-0.03)}}))
    assert _load_script().main(["/runs", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert set(data) >= {"mode", "runs_root", "live_root", "experiments", "allow_incomplete", "n_boot", "boot_seed",
                         "power_method", "min_power", "primary_pairs", "conditions", "g0"}
    assert data["mode"] == "scripted" and data["n_boot"] == 2000 and data["boot_seed"] == 0
    assert [c["id"] for c in data["conditions"]] == ["C1", "C2", "C3", "C4"]
    for c in data["conditions"]:
        assert {"id", "label", "name", "experiment", "arm_a", "arm_b", "rule", "estimate", "ci95", "n", "verdict",
                "reason", "dropped"} <= set(c)
        assert len(c["ci95"]) == 2 and c["verdict"] in ("PASS", "FAIL", "MISSING") and c["below_primary_pairs"] is False
    c1, c2, _, c4 = data["conditions"]
    assert c1["margin_source"] == gates.PROPOSAL_MARGIN_SOURCE and c2["power"] is not None
    assert c4["text_metric"]["outcome"] == "policy_chars"
    assert data["g0"] == {"verdict": "FAIL", "exit_code": 1,
                          "by_condition": {"C1": "FAIL", "C2": "PASS", "C3": "PASS", "C4": "PASS"}}


def test_live_mode_evaluates_c1_on_each_live_experiment(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, str, str]] = []
    table = {**ALL_PASS, ("exp_mem_dep_claude", "mem"): {"mean_balance": _pairs(0.05)}}
    monkeypatch.setattr(gates, "paired_outcomes", _fake_loader(table, calls))
    assert _load_script().main(["/runs", "--live", "/data/live"]) == 2  # the vein world has not been run
    lines = capsys.readouterr().out.strip().splitlines()
    assert [ln.split()[0] for ln in lines] == ["C1[exp_mem_dep_claude]", "C1[exp_mem_dep_veins_claude]", "C2", "C3", "C4", "G0"]
    assert lines[0].split()[1] == "PASS" and lines[1].split()[1] == "MISSING"
    assert "live dependence" in lines[-1]
    assert calls[0] == (str(Path("/data/live/exp_mem_dep_claude")), "amnesic", "mem")
    assert calls[1] == (str(Path("/data/live/exp_mem_dep_veins_claude")), "amnesic", "mem")
    assert all(c[0] == str(Path("/runs/exp_mem_e0")) and c[1] == "noop" for c in calls[2:])


def test_script_argument_checks(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(gates, "paired_outcomes", _fake_loader({}, calls))
    script = _load_script()
    assert script.main(["/runs", "--experiments", "dep_x, e0_y"]) == 2
    assert [c[0] for c in calls] == [str(Path("/runs/dep_x"))] + [str(Path("/runs/e0_y"))] * 3
    for bad in (["/runs", "--experiments", "only_one"], ["/runs", "--experiments", "a,b", "--live", "/x"], []):
        with pytest.raises(SystemExit) as e:
            script.main(bad)
        assert e.value.code == 2
    with pytest.raises(ValueError):
        gates.evaluate_g0("/runs", experiments=("only_one",))


# --- the real loader on short scripted runs --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def runs_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("gates_runs")
    for arm in ("mem", "amnesic"):
        run_arm(CONFIGS / f"exp_mem_dep_{arm}.yaml", root / "exp_mem_dep", SEEDS, run=SHORT_RUN)
    for arm in E0_ARMS:
        run_arm(CONFIGS / f"exp_mem_e0_{arm}.yaml", root / "exp_mem_e0", SEEDS, run=SHORT_RUN)
    return root


def _set_status(run_dir: Path, status: str) -> None:
    db = sqlite3.connect(str(run_dir / "world.db"))
    try:
        db.execute("UPDATE run SET status=?", (status,))
        db.commit()
    finally:
        db.close()


def test_real_runs_give_a_verdict_line_per_condition(runs_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = _load_script().main([str(runs_root)])
    out = capsys.readouterr()
    lines = out.out.strip().splitlines()
    assert code in (1, 2) and out.err == ""
    assert [ln.split()[0] for ln in lines] == ["C1", "C2", "C3", "C4", "G0"]
    result = gates.evaluate_g0(runs_root)
    assert result["g0"]["exit_code"] == code
    for c, line in zip(result["conditions"], lines, strict=False):
        assert c["n"] == len(SEEDS) and not c["dropped"] and not c["refused"], c
        assert c["verdict"] in ("PASS", "FAIL"), c  # complete runs, two pairs: every condition is decided
        assert c["below_primary_pairs"] and "fewer than the 20 pairs" in line  # decided, and flagged as rough
    # the numbers are compare's: same pairing, same seeded bootstrap
    cmp = compare(runs_root / "exp_mem_dep")
    (pair,) = cmp["comparisons"]
    sign = 1 if (pair["arm_a"], pair["arm_b"]) == ("amnesic", "mem") else -1
    c1 = result["conditions"][0]
    assert c1["estimate"] == pytest.approx(sign * pair["primary"]["delta"])
    if sign == 1:
        assert c1["ci95"] == pair["primary"]["ci95"]
    assert gates.evaluate_g0(runs_root) == result  # deterministic


def test_policy_chars_reads_the_latest_policy(runs_root: Path, tmp_path: Path) -> None:
    run = runs_root / "exp_mem_e0" / "noop" / "seed_1"
    db = sqlite3.connect(str(run / "world.db"))
    try:
        texts = [r[0] for r in db.execute("SELECT p.text FROM policy_versions p WHERE p.version = "
                                          "(SELECT MAX(version) FROM policy_versions q WHERE q.agent_id = p.agent_id)")]
    finally:
        db.close()
    assert texts and gates.policy_chars(run) == pytest.approx(sum(map(len, texts)) / len(texts))
    assert gates.policy_chars(runs_root / "exp_mem_dep" / "mem" / "seed_1") is None  # the policy file is off there
    assert gates.policy_chars(tmp_path) is None  # no database


def test_incomplete_runs_are_refused_unless_allowed(runs_root: Path, tmp_path: Path) -> None:
    root = tmp_path / "runs"
    shutil.copytree(runs_root, root)
    _set_status(root / "exp_mem_dep" / "mem" / "seed_2", "capped_total")
    refused = gates.evaluate_g0(root)["conditions"][0]
    assert refused["verdict"] == "MISSING" and refused["refused"]
    assert "incomplete runs: mem seed 2: run.status 'capped_total'" in refused["reason"] and "--allow-incomplete" in refused["reason"]
    allowed = gates.evaluate_g0(root, allow_incomplete=True)
    c1 = allowed["conditions"][0]
    assert c1["dropped"] == ["mem seed 2: run.status 'capped_total'"] and c1["n"] == 1
    assert c1["verdict"] == "MISSING" and "fewer than two" in c1["reason"]
    assert gates.notes(allowed) == ["C1 dropped mem seed 2: run.status 'capped_total'"]
    # every run of an arm incomplete: no completed runs
    _set_status(root / "exp_mem_dep" / "mem" / "seed_1", "failed")
    gone = gates.evaluate_g0(root, allow_incomplete=True)["conditions"][0]
    assert gone["verdict"] == "MISSING" and "arm 'mem' has no completed runs" in gone["reason"]
    # a seed with a run in one arm only
    shutil.rmtree(root / "exp_mem_e0" / "hoarder" / "seed_2")
    c3 = gates.evaluate_g0(root)["conditions"][2]
    assert c3["verdict"] == "MISSING" and "seed sets differ: noop seed 2: no run in arm 'hoarder'" in c3["reason"]
    c3 = gates.evaluate_g0(root, allow_incomplete=True)["conditions"][2]
    assert c3["dropped"] == ["noop seed 2: no run in arm 'hoarder'"] and c3["n"] == 1


def test_incomparable_runs_are_refused(runs_root: Path, tmp_path: Path) -> None:
    root = tmp_path / "runs"
    shutil.copytree(runs_root, root)
    for cfg_path in sorted((root / "exp_mem_e0" / "decoy").glob("seed_*/config.resolved.yaml")):
        data = yaml.safe_load(cfg_path.read_text())
        data["world"]["view_radius"] = 30.0
        cfg_path.write_text(yaml.safe_dump(data, sort_keys=True))
    conds = gates.evaluate_g0(root)["conditions"]
    assert conds[3]["verdict"] == "MISSING" and conds[3]["refused"] and "world.view_radius" in conds[3]["reason"]
    assert conds[1]["verdict"] != "MISSING" and conds[2]["verdict"] != "MISSING"  # the other E0 pairs are unaffected
    one = root / "exp_mem_e0" / "noop" / "seed_2" / "config.resolved.yaml"
    data = yaml.safe_load(one.read_text())
    data["memory"]["max_retrieved"] = 8
    one.write_text(yaml.safe_dump(data, sort_keys=True))
    c2 = gates.evaluate_g0(root)["conditions"][1]
    assert c2["verdict"] == "MISSING" and "differ outside run.seed" in c2["reason"]


def test_live_mode_reads_the_live_experiments(runs_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    live = tmp_path / "live"
    shutil.copytree(runs_root / "exp_mem_dep", live / "exp_mem_dep_claude")
    code = _load_script().main([str(runs_root), "--live", str(live)])
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("C1[exp_mem_dep_claude] ") and lines[0].split()[1] in ("PASS", "FAIL")
    assert lines[1].startswith("C1[exp_mem_dep_veins_claude] MISSING")
    assert code == (1 if "FAIL" in " ".join(ln.split()[1] for ln in lines[:5]) else 2)


# --- declared hypotheses -----------------------------------------------------------------------------------------
E0_HYPS = [
    {"name": "ib", "arm_a": "noop", "arm_b": "index_builder", "outcome": "mean_balance", "kind": "superiority",
     "direction": "greater", "margin": 0.0, "min_power": 0.8},
    {"name": "hoard", "arm_a": "noop", "arm_b": "hoarder", "outcome": "mean_balance", "kind": "superiority",
     "direction": "less", "margin": 0.0},
    {"name": "decoy_bal", "arm_a": "noop", "arm_b": "decoy", "outcome": "mean_balance", "kind": "non_superiority",
     "direction": "greater", "margin": 0.0},
    {"name": "decoy_text", "arm_a": "noop", "arm_b": "decoy", "outcome": "policy_chars", "kind": "superiority",
     "direction": "greater", "margin": 0.0, "primary": False},
]


def _e0h(arm: str, effect: float, chars: list[tuple[float | None, float | None]] | None = None,
         hyps: list[dict[str, Any]] | None = None) -> gates.Paired:
    p = _e0(arm, effect, chars)
    p.hypotheses = list(E0_HYPS if hyps is None else hyps)
    return p


def test_declared_hypothesis_matches_either_orientation() -> None:
    h = {"name": "x", "arm_a": "noop", "arm_b": "hoarder", "outcome": "mean_balance", "kind": "superiority",
         "direction": "less"}
    assert gates.declared_hypothesis([h], "noop", "hoarder", "mean_balance", "superiority", "less") == h
    rev = {**h, "arm_a": "hoarder", "arm_b": "noop", "direction": "greater"}
    assert gates.declared_hypothesis([rev], "noop", "hoarder", "mean_balance", "superiority", "less") == rev
    assert gates.declared_hypothesis([h], "noop", "hoarder", "mean_balance", "superiority", "greater") is None
    assert gates.declared_hypothesis([h], "noop", "hoarder", "exam_score", "superiority", "less") is None
    assert gates.declared_hypothesis([h], "noop", "hoarder", None, "superiority", "less") == h  # any outcome
    eq = {**h, "kind": "equivalence", "margin": 0.25}
    assert gates.declared_hypothesis([eq], "hoarder", "noop", "mean_balance", "equivalence") == eq


def test_e0_conditions_read_the_declared_margins_and_power() -> None:
    # with margin 0 and min_power 0.8 declared, every verdict is the undeclared one; the source names the hypothesis
    for fn, arm, effect, chars in ((gates.condition_index_builder, "index_builder", 0.05, None),
                                   (gates.condition_hoarder, "hoarder", -0.05, None),
                                   (gates.condition_decoy, "decoy", -0.03, LONGER)):
        plain, declared = fn(_e0(arm, effect, chars)), fn(_e0h(arm, effect, chars))
        assert plain["verdict"] == declared["verdict"] == "PASS"
        assert plain["margin_source"] == gates.PROPOSAL_E0_SOURCE and "[assumption]" in plain["margin_source"]
        assert declared["margin_source"].startswith("experiment.hypotheses '") and declared["hypothesis"] is not None
        assert plain["reason"] == declared["reason"]
    # C2: a declared margin above the effect fails it; a declared min_power above 0.8 is used, one below is not
    ib = dict(E0_HYPS[0])
    over = gates.condition_index_builder(_e0h("index_builder", 0.05, hyps=[{**ib, "margin": 0.06}]))
    assert over["verdict"] == "FAIL" and "is not above the margin 0.0600" in over["reason"]
    weak = _paired("noop", "index_builder", {"mean_balance": [(1.0, 1.1), (1.0, 1.12), (1.0, 1.3), (1.0, 1.15), (1.0, 1.2)]})
    base = gates.condition_index_builder(weak)
    weak.hypotheses = [{**ib, "min_power": 0.99}]
    strict = gates.condition_index_builder(weak)
    assert strict["min_power"] == 0.99 and strict["power"] == base["power"]
    assert base["power"] >= 0.8 and strict["power"] < 0.99 and (base["verdict"], strict["verdict"]) == ("PASS", "FAIL")
    weak.hypotheses = [{**ib, "min_power": 0.5}]
    assert gates.condition_index_builder(weak)["min_power"] == gates.MIN_POWER  # never below what 6.2 fixes
    # C3: the estimate must clear minus the margin
    hoard = gates.condition_hoarder(_e0h("hoarder", -0.05, hyps=[{**E0_HYPS[1], "margin": 0.08}]))
    assert hoard["verdict"] == "FAIL" and "is not below minus the margin 0.0800" in hoard["reason"]
    # C4: a non-superiority margin tolerates a decoy a little above the no-op; a text margin can fail the text metric
    lenient = [{**E0_HYPS[2], "margin": 0.1}, E0_HYPS[3]]
    assert gates.condition_decoy(_e0h("decoy", 0.05, LONGER))["verdict"] == "FAIL"
    assert gates.condition_decoy(_e0h("decoy", 0.05, LONGER, hyps=lenient))["verdict"] == "PASS"
    tall = gates.condition_decoy(_e0h("decoy", -0.03, LONGER, hyps=[E0_HYPS[2], {**E0_HYPS[3], "margin": 900.0}]))
    assert tall["verdict"] == "FAIL" and tall["text_metric"]["margin_abs"] == 900.0
    assert "policy_chars: not significantly above the margin 900.0" in tall["reason"]


def test_declaring_config_follows_extends(tmp_path: Path) -> None:
    hyp = {"name": "dependence", "arm_a": "amnesic", "arm_b": "mem", "outcome": "mean_balance", "kind": "superiority",
           "direction": "greater", "margin": 0.0}
    full = {**hyp, "margin_unit": "abs", "primary": True, "min_power": None}  # as a resolved config stores it
    assert gates.declaring_config("exp_mem_dep_mem", full) == "configs/exp_mem_dep_base.yaml"
    assert gates.declaring_config("live_claude_dep_veins_mem", full) == "configs/exp_mem_dep_base.yaml"  # inherited
    assert gates.declaring_config("exp_mem_dep_mem", {**full, "margin": 0.02}) is None  # declared differently now
    assert gates.declaring_config("exp_mem_e0_noop", full) is None  # E0's list does not hold it
    assert gates.declaring_config("no_such_config", full) is None and gates.declaring_config(None, full) is None
    assert gates.hypothesis_source(full, "exp_mem_dep_amnesic") == \
        "experiment.hypotheses 'dependence' in configs/exp_mem_dep_base.yaml"
    # a chain outside the repository: the nearest file that sets the list decides, and its path is given whole
    (tmp_path / "base.yaml").write_text(yaml.safe_dump({"experiment": {"hypotheses": [hyp]}}))
    (tmp_path / "mid.yaml").write_text(yaml.safe_dump({"extends": "base.yaml", "experiment": {"name": "x"}}))
    (tmp_path / "arm.yaml").write_text(yaml.safe_dump({"extends": "mid.yaml"}))
    assert gates.declaring_config("arm", full, tmp_path) == str((tmp_path / "base.yaml").resolve())
    (tmp_path / "mid.yaml").write_text(yaml.safe_dump({"extends": "base.yaml", "experiment": {"hypotheses": []}}))
    assert gates.declaring_config("arm", full, tmp_path) is None  # mid's empty list replaces base's


def test_real_runs_name_the_config_that_declares_each_margin(runs_root: Path) -> None:
    result = gates.evaluate_g0(runs_root)
    c1, c2, c3, c4 = result["conditions"]
    assert c1["margin_source"] == "experiment.hypotheses 'dependence' in configs/exp_mem_dep_base.yaml"
    assert c2["margin_source"] == "experiment.hypotheses 'index_builder_over_noop' in configs/exp_mem_e0_base.yaml"
    assert c2["min_power"] == 0.8
    assert c3["margin_source"] == "experiment.hypotheses 'hoarder_below_noop' in configs/exp_mem_e0_base.yaml"
    assert c4["margin_source"] == "experiment.hypotheses 'decoy_not_above_noop' in configs/exp_mem_e0_base.yaml"
    assert c4["text_metric"]["margin_source"] == \
        "experiment.hypotheses 'decoy_text_metric_above_noop' in configs/exp_mem_e0_base.yaml"
    assert all(c["margin"] == 0.0 for c in (c1, c2, c3, c4)) and result["g1"] is None
    assert result["exit_code"] == result["g0"]["exit_code"]
    assert "from experiment.hypotheses 'dependence' in configs/exp_mem_dep_base.yaml" in gates.render(result)


# --- G1 on synthetic E1 runs ----------------------------------------------------------------------------------------
E1_TPD = 20                      # one day of 20 ticks: lineage fitness moves in steps of 0.05 agent-days
E1_SEEDS = tuple(range(1, 11))   # the pilot's 10 seeds
_E1_CONFIGS: dict[str, Any] = {}


def _e1_config(arm: str, seed: int):
    if arm not in _E1_CONFIGS:
        _E1_CONFIGS[arm] = load_config(CONFIGS / f"live_mem_e1_{arm}.yaml", {"run": {"days": 1, "ticks_per_day": E1_TPD}})
    cfg = _E1_CONFIGS[arm].model_copy(deep=True)
    cfg.run.seed = seed
    return cfg


def _write_e1_run(run_dir: Path, arm: str, seed: int, *, died_tick: int | None, maintain: list[bool]) -> None:
    """A completed one-day E1 run (no simulation): the arm's resolved config, a run row, one founder born at tick 0
    who dies at ``died_tick`` (lineage fitness ``died_tick / 20``, or 1.0 alive), ``maintain`` commands with those
    outcomes, and tick rows to the last tick."""
    cfg = _e1_config(arm, seed)
    run_dir.mkdir(parents=True)
    text = cfg.to_yaml()
    (run_dir / "config.resolved.yaml").write_text(text)
    last = cfg.run.days * cfg.run.ticks_per_day
    db = Database(run_dir / "world.db")
    try:
        db.execute("INSERT INTO run(run_id, config_hash, config_yaml, seed, created_at, schema_version, status, ended_tick) "
                   "VALUES(?,?,?,?,?,?,?,?)", (f"{arm}-s{seed}", cfg.hash(), text, seed, "2026-01-01T00:00:00", 2,
                                               "completed", last))
        db.execute("INSERT INTO world_kv(key, value) VALUES('tick', ?)", (json.dumps(last),))
        db.execute("INSERT INTO agents(agent_id, name, parent_id, model_tier, balance, personality_seed, memory_path, "
                   "generation, status, x, y, entropy_budget, born_tick, died_tick, created_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("ag1", "Ada", None, "average", 1_000_000, "p", "vault/ag1", 0,
                    "alive" if died_tick is None else "bankrupt", 0.0, 0.0, 100.0, 0, died_tick, "2026-01-01T00:00:00"))
        for i, ok in enumerate(maintain):
            db.execute("INSERT INTO memory_commands(cmd_id, tick, agent_id, purpose, command, args, ok) "
                       "VALUES(?,?,?,?,?,?,?)", (f"m{i}", E1_TPD, "ag1", "maintain", "create", "{}", int(ok)))
    finally:
        db.close()
    rows = [{"tick": t, "day": 1, "mean_balance": 1.0} for t in range(1, last + 1)] + [{"kind": "day", "day": 1, "mean_balance": 1.0}]
    (run_dir / "metrics.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def _e1(root: Path, c_minus_b: list[int], *, c_valid: list[bool] | None = None, seeds: Sequence[int] = E1_SEEDS) -> Path:
    """An E1 experiment directory: b's founder dies at tick 10, c's at ``10 + c_minus_b[i]`` (so c - b on lineage
    fitness is ``c_minus_b[i] / 20``), a0 and a at 10 without maintenance; b's maintenance commands are 19 of 20 valid,
    c's ``c_valid`` (default the same)."""
    good = [True] * 19 + [False]
    for i, seed in enumerate(seeds):
        for arm, died, maintain in (("a0", 10, []), ("a", 10, []), ("b", 10, good),
                                    ("c", 10 + c_minus_b[i], good if c_valid is None else c_valid)):
            _write_e1_run(root / arm / f"seed_{seed}", arm, seed, died_tick=died, maintain=maintain)
    return root


C_OVER_B = [2, 3, 1, 2, 4, 2, 3, 1, 2, 3]       # c - b in ticks: mean 2.3, d_z about 2.4
C_TIES_B = [2, -3, 1, -2, 4, -2, 3, -1, 2, -3]  # mean 0.1, d_z about 0.04


def test_g1a_valid_commands_pass_fail_and_missing(tmp_path: Path) -> None:
    ok = gates.condition_valid_commands(_e1(tmp_path / "ok", C_OVER_B))
    assert ok["verdict"] == "PASS" and ok["experiment"] == "exp_mem_e1"
    assert {a: v["applicable"] for a, v in ok["arms"].items()} == {"a": False, "a0": False, "b": True, "c": True}
    assert ok["arms"]["c"]["mean"] == pytest.approx(0.95) and ok["arms"]["c"]["n"] == 10
    bad = gates.condition_valid_commands(_e1(tmp_path / "bad", C_OVER_B, c_valid=[True, False] * 10))
    assert bad["verdict"] == "FAIL" and "arm 'c' 0.500 is below 0.9" in bad["reason"]
    silent = gates.condition_valid_commands(_e1(tmp_path / "silent", C_OVER_B, c_valid=[]))
    assert silent["verdict"] == "MISSING" and "arm 'c' runs maintenance but no complete run" in silent["reason"]
    assert gates.condition_valid_commands(tmp_path / "nothing")["verdict"] == "MISSING"
    _set_status(tmp_path / "ok" / "c" / "seed_3", "failed")
    refused = gates.condition_valid_commands(tmp_path / "ok")
    assert refused["verdict"] == "MISSING" and refused["refused"] and "c seed 3: run.status 'failed'" in refused["reason"]
    allowed = gates.condition_valid_commands(tmp_path / "ok", allow_incomplete=True)
    assert allowed["verdict"] == "PASS" and allowed["arms"]["c"]["n"] == 9 and allowed["dropped"]


def test_g1b_c_over_b_pass_fail_and_missing(tmp_path: Path) -> None:
    def g1b(root: Path) -> dict[str, Any]:
        return gates.condition_c_over_b(gates.paired_outcomes(root, "b", "c", gates.g1_outcome))

    ok = g1b(_e1(tmp_path / "ok", C_OVER_B))
    diffs = [d / E1_TPD for d in C_OVER_B]
    assert ok["verdict"] == "PASS" and ok["outcome"] == "lineage_fitness" and ok["n"] == 10
    assert ok["estimate"] == pytest.approx(sum(diffs) / 10) and ok["d_z"] >= gates.G1_MIN_DZ
    assert ok["hypothesis"] == "h1_self_edit_over_equal_compute"
    assert ok["outcome_source"] == "experiment.hypotheses 'h1_self_edit_over_equal_compute' in configs/live_mem_e1_base.yaml"
    tie = g1b(_e1(tmp_path / "tie", C_TIES_B))
    assert tie["verdict"] == "FAIL" and tie["d_z"] < gates.G1_MIN_DZ and "is below 0.5 at n=10" in tie["reason"]
    short = g1b(_e1(tmp_path / "short", C_OVER_B, seeds=E1_SEEDS[:5]))
    assert short["verdict"] == "MISSING" and "G1 reads d_z on at least 10" in short["reason"]
    same = g1b(_e1(tmp_path / "same", [2] * 10))  # every difference the same positive number: unbounded d_z
    assert same["d_z"] is None and same["verdict"] == "PASS"


def _g0_passing_then_real(table: dict[tuple[str, str], dict[str, list[tuple[float, float]]]]):
    """The G0 experiments from ``table`` (all passing), every other directory through the real loader."""
    fake, real = _fake_loader(table), gates.paired_outcomes

    def loader(exp_dir, arm_a, arm_b, outcomes, *, allow_incomplete=False):
        if Path(exp_dir).name in ("exp_mem_dep", "exp_mem_e0"):
            return fake(exp_dir, arm_a, arm_b, outcomes, allow_incomplete=allow_incomplete)
        return real(exp_dir, arm_a, arm_b, outcomes, allow_incomplete=allow_incomplete)
    return loader


@pytest.mark.parametrize("c_minus_b, c_valid, code, g1, tail", [
    (C_OVER_B, None, 0, "PASS", "exit 0: every condition passes"),
    (C_TIES_B, None, 1, "FAIL", "exit 1: FAIL G1b"),
    (C_OVER_B, [True, False] * 10, 1, "FAIL", "exit 1: FAIL G1a"),
    (C_OVER_B, [], 2, "MISSING", "exit 2: MISSING G1a"),
])
def test_script_g1_exits_by_every_condition(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
                                            c_minus_b: list[int], c_valid: list[bool] | None, code: int, g1: str, tail: str) -> None:
    e1 = _e1(tmp_path / "exp_mem_e1", c_minus_b, c_valid=c_valid)
    monkeypatch.setattr(gates, "paired_outcomes", _g0_passing_then_real(ALL_PASS))
    assert _load_script().main(["/runs", "--g1", str(e1)]) == code
    lines = capsys.readouterr().out.strip().splitlines()
    assert [ln.split()[0] for ln in lines] == ["C1", "C2", "C3", "C4", "G1a", "G1b", "G0", "G1", "exit"]
    assert lines[6].startswith("G0 PASS: C1 PASS, C2 PASS, C3 PASS, C4 PASS")
    assert lines[7].startswith(f"G1 {g1} (dependence from the scripted runs; 10.4 reads it live): C1 PASS, G1a ")
    assert lines[8] == tail


def test_script_g1_json_and_a_failing_g0(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    e1 = _e1(tmp_path / "exp_mem_e1", C_OVER_B)
    table = {**ALL_PASS, ("exp_mem_dep", "mem"): {"mean_balance": _pairs(-0.03)}}
    monkeypatch.setattr(gates, "paired_outcomes", _g0_passing_then_real(table))
    assert _load_script().main(["/runs", "--g1", str(e1), "--json"]) == 1  # G1a and G1b pass; C1 fails both gates
    data = json.loads(capsys.readouterr().out)
    assert data["g0"]["verdict"] == "FAIL" and data["g1"] == {
        "verdict": "FAIL", "exit_code": 1, "by_condition": {"C1": "FAIL", "G1a": "PASS", "G1b": "PASS"}}
    assert data["exit_code"] == 1 and data["failing"] == ["C1"] and data["missing"] == []
    assert data["experiments"]["e1"] == str(e1) and data["min_valid_rate"] == 0.9 and data["g1_min_d_z"] == 0.5
    assert [c["id"] for c in data["conditions"]] == ["C1", "C2", "C3", "C4", "G1a", "G1b"]


# --- the 10.4 diagnosis ----------------------------------------------------------------------------------------------
def _load_diagnosis():
    spec = importlib.util.spec_from_file_location("mem_diagnosis_script", REPO / "scripts" / "mem_diagnosis.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_diagnosis_table_is_the_paired_difference_of_the_outcomes(runs_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    diag = _load_diagnosis()
    exp = runs_root / "exp_mem_dep"
    assert diag.memory_arms(exp) == ("amnesic", "mem")
    result = diag.diagnose(exp)
    assert (result["arm_without"], result["arm_with"]) == ("amnesic", "mem")
    assert [r["outcome"] for r in result["rows"]] == ["forage_income_usd", "decide_cost_usd",
                                                      "mean_input_tokens_per_decide", "mean_balance"]
    for row in result["rows"]:
        diffs = [end_of_run_outcome(exp / "mem" / f"seed_{s}", row["outcome"])
                 - end_of_run_outcome(exp / "amnesic" / f"seed_{s}", row["outcome"]) for s in SEEDS]
        assert row["n"] == len(SEEDS) and row["delta"] == pytest.approx(sum(diffs) / len(diffs))
        assert row["ci95"] == bootstrap_ci(diffs, n_boot=gates.N_BOOT, seed=gates.BOOT_SEED)
        assert row["higher_with"] == sum(d > 0 for d in diffs)
    by = {r["outcome"]: r for r in result["rows"]}
    tokens = by["mean_input_tokens_per_decide"]
    assert tokens["with"] > tokens["without"] and tokens["higher_with"] == len(SEEDS)  # memory lengthens the prompt
    b = result["break_even"]
    income = by["forage_income_usd"]
    assert b["required_income_rise"] == pytest.approx(by["decide_cost_usd"]["delta"] / income["without"])
    assert b["observed_income_rise"] == pytest.approx(income["delta"] / income["without"])
    text = diag.render(result)
    assert "| Per agent | amnesic | mem | mem minus amnesic [95% CI] |" in text and "Break-even:" in text
    assert diag.main([str(exp), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["sweep"] is None and data["diagnosis"]["rows"][3]["delta"] == pytest.approx(by["mean_balance"]["delta"])
    # naming the arms by hand, and the refusals
    assert diag.diagnose(exp, with_arm="mem", without_arm="amnesic")["rows"] == result["rows"]
    assert diag.main([str(exp), "--with", "mem"]) == 2
    assert diag.main([str(runs_root / "exp_mem_e0")]) == 2  # every E0 arm has memory on
    assert diag.main([str(runs_root / "nothing")]) == 2
    assert "refused:" in capsys.readouterr().err


def test_sweep_settings_and_scripted_only_refusal(tmp_path: Path) -> None:
    diag = _load_diagnosis()
    ofat, full = diag.sweep_settings("ofat"), diag.sweep_settings("full")
    base = {"world.node_capacity": 40.0, "world.node_regen_per_tick": 0.4, "world.view_radius": 12.0}
    assert ofat[0] == base and len(ofat) == 4 and len(full) == 8
    assert [diag.setting_name(s) for s in ofat] == ["cap40_regen0.4_view12", "cap12_regen0.4_view12",
                                                     "cap40_regen0.2_view12", "cap40_regen0.4_view8"]
    assert len({diag.setting_name(s) for s in full}) == 8 and all(s in full for s in ofat)
    with pytest.raises(ValueError):
        diag.sweep_settings("nope")
    # the dependence world's own levels are the base setting's
    dep = load_config(CONFIGS / "exp_mem_dep_mem.yaml")
    assert (dep.world.node_capacity, dep.world.node_regen_per_tick, dep.world.view_radius) == (40.0, 0.4, 12.0)
    with pytest.raises(diag.DiagnosisError, match="not scripted"):
        diag.write_setting_configs([CONFIGS / "live_claude_dep_mem.yaml"], base, tmp_path)


def test_sweep_runs_each_setting_and_tabulates_it(runs_root: Path, tmp_path: Path) -> None:
    diag = _load_diagnosis()
    configs = diag.arm_configs(runs_root / "exp_mem_dep")
    assert sorted(p.name for p in configs) == ["exp_mem_dep_amnesic.yaml", "exp_mem_dep_mem.yaml"]
    settings = diag.sweep_settings("ofat")[:2]
    out = tmp_path / "sweep"
    result = diag.run_sweep(configs, out, seeds="1-2", settings=settings, days=1)
    assert [r["setting"] for r in result["settings"]] == ["cap40_regen0.4_view12", "cap12_regen0.4_view12"]
    for row in result["settings"]:
        assert row["diagnosis"] is not None, row.get("error")
        assert row["diagnosis"]["rows"][3]["n"] == 2
        run = out / row["setting"] / "exp_mem_dep" / "mem" / "seed_1"
        cfg = yaml.safe_load((run / "config.resolved.yaml").read_text())
        assert cfg["world"]["node_capacity"] == row["values"]["world.node_capacity"] and cfg["run"]["days"] == 1
    # the base setting is the dependence world itself: the same resolved config as a direct one-day run
    base_run = out / "cap40_regen0.4_view12" / "exp_mem_dep" / "amnesic" / "seed_2"
    direct = load_config(CONFIGS / "exp_mem_dep_amnesic.yaml", {"run": {"days": 1, "seed": 2, "tick_seconds": 0.0}})
    with sqlite3.connect(str(base_run / "world.db")) as db:
        assert db.execute("SELECT config_hash FROM run").fetchone()[0] == direct.hash()
    text = diag.render_sweep(result)
    assert "| cap12_regen0.4_view12 | 12 | 0.4 | 12 | 2 |" in text
    assert text.splitlines()[-1].startswith("The amnesic arm is ahead on balance in ")
    again = diag.run_sweep(configs, out, seeds="1-2", settings=settings, days=1)  # runs already there are kept
    assert again["settings"][0]["diagnosis"]["rows"] == result["settings"][0]["diagnosis"]["rows"]
