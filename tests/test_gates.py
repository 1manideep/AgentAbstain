"""Gate G0 from runs (``void/research/gates.py``, ``scripts/gates.py``; MEMORY_EVOLUTION 5.2, 6.2, 10.2).

Three layers:

* the four conditions on fixed per-seed outcomes (:class:`gates.Paired` built by hand): PASS, FAIL and MISSING for
  each, the margin's source, the power requirement, and that the interval is the seeded paired bootstrap compare uses;
* the script with the loader monkeypatched: one line per condition plus G0, the exit codes 0 / 1 / 2, ``--json``,
  ``--live`` and the argument checks;
* the real loader on 1-day scripted runs of every arm at two seeds: a verdict line per condition without a crash,
  the same numbers as ``compare``, refusal of incomplete and incomparable runs, ``--allow-incomplete``, and live mode.
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

from void.research import gates
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
