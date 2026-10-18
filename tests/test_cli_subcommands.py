"""`void compare | analyze | export`, the lazy subcommands, safe re-runs (`--fresh`) and the `run.data_dir` default."""

from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from void.cli import DEFAULT_MOCK_CONFIG, LAZY_COMMANDS, build_parser, main

ROOT = Path(__file__).resolve().parents[1]
SMOKE = str(ROOT / "configs" / "scripted_smoke.yaml")
TINY = {"run": {"days": 1, "ticks_per_day": 6}}  # scripted_smoke cut to one short day; the run name stays
BUILTIN = ("run", "serve", "compare", "analyze", "export", "schema", "mock-feed", "inspect")
EXISTING = "already holds a run: pass --resume to continue it, --fresh to archive it and start over, or --out DIR"


# --- fixtures -------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def experiment(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real 2-arm scripted experiment (exp_mem_dep: mem vs amnesic), two 1-day seeds per arm, run through the CLI."""
    out = tmp_path_factory.mktemp("runs")
    for arm in ("mem", "amnesic"):
        config = str(ROOT / "configs" / f"exp_mem_dep_{arm}.yaml")
        assert main(["run", "--config", config, "--seeds", "1-2", "--days", "1", "--tick-seconds", "0", "--out", str(out)]) == 0
    exp = out / "exp_mem_dep"
    assert sorted(p.name for p in exp.iterdir()) == ["amnesic", "mem"]
    return exp


@pytest.fixture
def tiny(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A config file for :data:`TINY`, outside the test's own ``tmp_path``."""
    path = tmp_path_factory.mktemp("cfg") / "tiny.yaml"
    path.write_text(yaml.safe_dump({"extends": SMOKE, **TINY}))
    return str(path)


@pytest.fixture
def no_port(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the server runner so `void serve` never binds a port; returns the recorded calls."""
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    return calls


def _lazy_modules(monkeypatch: pytest.MonkeyPatch, modules: dict[str, Any]) -> None:
    """Make ``importlib.import_module`` answer the lazy module names from ``modules`` (an exception is raised)."""
    real = importlib.import_module

    def fake(name: str, package: str | None = None) -> Any:
        if name in modules:
            found = modules[name]
            if isinstance(found, BaseException):
                raise found
            return found
        return real(name, package)

    monkeypatch.setattr(importlib, "import_module", fake)


# --- help -------------------------------------------------------------------------------------------------------
def test_top_level_help_lists_every_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in (*BUILTIN, "report", "transplant", "generations", "mcp"):
        assert f"    {name} " in out, name


@pytest.mark.parametrize("name", BUILTIN)
def test_every_subcommand_help_exits_0(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main([name, "--help"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith(f"usage: void {name}")


@pytest.mark.parametrize("name", sorted(LAZY_COMMANDS))
def test_lazy_subcommand_hands_argv_over_verbatim(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def cli_main(argv: list[str]) -> int:
        seen.append(argv)
        return 0 if argv == ["--help"] else 3

    _lazy_modules(monkeypatch, {LAZY_COMMANDS[name][0]: SimpleNamespace(cli_main=cli_main)})
    assert main([name, "--help"]) == 0  # --help belongs to the module, not to argparse
    assert main([name, "x", "--json", "-k", "--", "y"]) == 3
    assert main([name]) == 3
    assert seen == [["--help"], ["x", "--json", "-k", "--", "y"], []]


@pytest.mark.parametrize("name", sorted(LAZY_COMMANDS))
def test_missing_lazy_module_exits_2(name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    module = LAZY_COMMANDS[name][0]
    _lazy_modules(monkeypatch, {module: ModuleNotFoundError(f"No module named {module!r}", name=module)})
    assert main([name, "--help"]) == 2
    assert capsys.readouterr().err.strip() == f"void {name}: not built yet"
    _lazy_modules(monkeypatch, {module: SimpleNamespace()})  # the module exists but has no cli_main yet
    assert main([name, "anything"]) == 2
    assert capsys.readouterr().err.strip() == f"void {name}: not built yet"
    # a module that exists but cannot import one of its own dependencies says which
    _lazy_modules(monkeypatch, {module: ModuleNotFoundError("No module named 'somedep'", name="somedep")})
    assert main([name]) == 2
    err = capsys.readouterr().err
    assert "somedep" in err and "not built yet" not in err


# --- compare ------------------------------------------------------------------------------------------------------
def test_compare_prints_the_primary_line(experiment: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["compare", str(experiment)]) == 0
    out = capsys.readouterr().out
    assert "primary: mean_balance" in out
    assert "amnesic vs mem: n=2" in out
    # --exp NAME resolves to <out>/<NAME> and gives the same report
    assert main(["compare", "--exp", "exp_mem_dep", "--out", str(experiment.parent)]) == 0
    assert capsys.readouterr().out == out
    assert main(["compare", str(experiment), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["primary"] == "mean_balance" and result["arms"] == ["amnesic", "mem"] and result["seeds"] == [1, 2]
    assert result["comparisons"][0]["primary"]["n"] == 2


def test_compare_refusal_exits_2(experiment: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "exp_mem_dep"
    shutil.copytree(experiment, bad)
    for run_dir in sorted((bad / "mem").glob("seed_*")):  # a difference outside the declared independent variable
        path = run_dir / "config.resolved.yaml"
        data = yaml.safe_load(path.read_text())
        data["world"]["scarcity"] = 0.123
        path.write_text(yaml.safe_dump(data, sort_keys=True))
    assert main(["compare", str(bad)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("refused: ") and "world.scarcity" in captured.err
    assert main(["compare", "--exp", "exp_mem_dep", "--out", str(tmp_path)]) == 2
    assert capsys.readouterr().err.startswith("refused: ")
    # neither or both of EXP_DIR and --exp
    assert main(["compare"]) == 2
    assert main(["compare", str(bad), "--exp", "exp_mem_dep"]) == 2
    assert "not both" in capsys.readouterr().err


def test_compare_forwards_allow_incomplete_only_when_supported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    cmp = importlib.import_module("void.research.compare")  # the package re-exports a function of the same name

    calls: list[dict[str, Any]] = []
    result = {"experiment": "x", "arms": ["a", "b"], "seeds": [1], "independent_variables": [], "primary": "p",
              "comparisons": [], "secondaries": [], "warnings": [], "notes": []}

    def old_compare(exp_dir: Any, primary: str | None = None) -> dict[str, Any]:
        calls.append({"primary": primary})
        return result

    def new_compare(exp_dir: Any, primary: str | None = None, *, allow_incomplete: bool = False) -> dict[str, Any]:
        calls.append({"primary": primary, "allow_incomplete": allow_incomplete})
        return result

    monkeypatch.setattr(cmp, "compare", old_compare)
    assert main(["compare", str(tmp_path), "--allow-incomplete", "--primary", "gini"]) == 0
    assert calls[-1] == {"primary": "gini"}
    assert "warning: --allow-incomplete" in capsys.readouterr().err
    monkeypatch.setattr(cmp, "compare", new_compare)
    assert main(["compare", str(tmp_path), "--allow-incomplete"]) == 0
    assert calls[-1] == {"primary": None, "allow_incomplete": True}
    assert main(["compare", str(tmp_path)]) == 0
    assert calls[-1] == {"primary": None, "allow_incomplete": False}  # not forwarded unless asked
    assert "warning" not in capsys.readouterr().err


# --- analyze and export ---------------------------------------------------------------------------------------------
def test_analyze_json_parses(experiment: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = experiment / "mem" / "seed_1"
    assert main(["analyze", str(run_dir), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["run_id"] == "exp_mem_dep_mem-s1" and summary["arm"] == "mem" and summary["status"] == "completed"
    assert "mean_balance" in summary and "notes_by_channel" in summary
    assert main(["analyze", str(run_dir)]) == 0
    text = capsys.readouterr().out
    assert "run_id: exp_mem_dep_mem-s1" in text and "\nmean_balance: " in text
    assert main(["analyze", str(tmp_path / "nope")]) == 1


def _paid_tier(cfg: dict[str, Any], provider: str) -> str:
    """The first tier of ``provider`` the resolved config defines (scripted configs keep the paid tiers around)."""
    return next(name for name, tier in sorted(cfg["tiers"].items()) if tier["provider"] == provider)


def _rostered_tier_on_anthropic(cfg: dict[str, Any]) -> None:
    cfg["tiers"][cfg["agents"][0]["tier"]]["provider"] = "anthropic"


def _agent_on(provider: str) -> Callable[[dict[str, Any]], None]:
    def doctor(cfg: dict[str, Any]) -> None:
        cfg["agents"][0]["tier"] = _paid_tier(cfg, provider)

    return doctor


def _arrival_on_gemini(cfg: dict[str, Any]) -> None:
    cfg.setdefault("epochs", []).append({"day": 1, "kind": "arrival",
                                         "arrival": {"name": "Kai", "tier": _paid_tier(cfg, "gemini")}})


def _chronicle_by_model(cfg: dict[str, Any]) -> None:
    cfg["chronicle"]["writer"] = "llm"  # every agent stays scripted, but the writer falls back to any provider tier


# case -> (how the copied run's resolved config is doctored, the providers the refusal names)
REPLAY_DOCTORINGS: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "rostered tier switched to anthropic": (_rostered_tier_on_anthropic, "anthropic"),
    "agent on an anthropic tier": (_agent_on("anthropic"), "anthropic"),
    "agent on a gemini tier": (_agent_on("gemini"), "gemini"),
    "arrival on a gemini tier": (_arrival_on_gemini, "gemini"),
    "chronicle written by a model": (_chronicle_by_model, "anthropic, gemini"),
}


@pytest.fixture
def no_simulation(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Make building a :class:`void.sim.loop.Simulation` fail loudly; returns the attempted constructions."""
    import void.sim.loop as loop

    built: list[Any] = []

    class Refused:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built.append((args, kwargs))
            raise AssertionError("a Simulation was built for the replay")

    monkeypatch.setattr(loop, "Simulation", Refused)
    return built


def _copy_and_doctor(experiment: Path, tmp_path: Path, doctor: Callable[[dict[str, Any]], None]) -> Path:
    run_dir = tmp_path / "run"
    shutil.copytree(experiment / "mem" / "seed_1", run_dir)
    path = run_dir / "config.resolved.yaml"
    cfg = yaml.safe_load(path.read_text())
    doctor(cfg)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return run_dir


@pytest.mark.parametrize("case", sorted(REPLAY_DOCTORINGS))
def test_analyze_replay_refuses_a_run_that_could_call_a_provider(case: str, experiment: Path, tmp_path: Path,
                                                                 no_simulation: list[Any],
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    """The replay re-runs the run from its resolved config in a temporary directory: a provider tier would spend real
    money that no live-budget root ever sees, so it is refused before anything is built."""
    doctor, providers = REPLAY_DOCTORINGS[case]
    run_dir = _copy_and_doctor(experiment, tmp_path, doctor)
    assert main(["analyze", str(run_dir), "--replay", "--json"]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "Traceback" not in err
    assert err.strip() == (f"refused: void analyze --replay: {run_dir} uses paid tiers ({providers}); replaying would "
                           f"call the provider outside the live budget")
    assert no_simulation == []
    # only the replay is refused: the summary itself reads the files and calls nothing
    assert main(["analyze", str(run_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "exp_mem_dep_mem-s1"
    assert no_simulation == []


def test_analyze_replay_refuses_a_run_whose_config_cannot_be_read(experiment: Path, tmp_path: Path,
                                                                  no_simulation: list[Any],
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = _copy_and_doctor(experiment, tmp_path, lambda cfg: cfg.update(tiers={}))
    assert main(["analyze", str(run_dir), "--replay"]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "Traceback" not in err
    assert err.startswith(f"refused: void analyze --replay: cannot read the config of {run_dir} (ValidationError: ")
    assert err.rstrip().endswith("so cannot tell whether replaying would call a provider")
    assert no_simulation == []


def test_analyze_replay_still_runs_for_a_scripted_run(experiment: Path, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    import void.research.analysis as analysis

    calls: list[tuple[Path, bool]] = []

    def probe_recall(run_dir: Any, *, replay: bool = False, min_notes: int = 3) -> float:
        calls.append((Path(run_dir), replay))
        return 0.25

    monkeypatch.setattr(analysis, "probe_recall", probe_recall)
    run_dir = experiment / "mem" / "seed_1"
    assert main(["analyze", str(run_dir), "--replay", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["probe_recall"] == 0.25
    assert [c for c in calls if c[1]] == [(run_dir, True)]


def test_replay_guard_matches_the_committed_configs() -> None:
    """Every live config (the only ones that roster a paid tier) is refused, and every scripted one may replay."""
    from void.cli import _replay_providers
    from void.config import load_config

    for path in sorted((ROOT / "configs").glob("*.yaml")):
        paid = _replay_providers(load_config(path))
        assert bool(paid) == path.name.startswith("live_"), (path.name, sorted(paid))


def test_export_writes_manifest(experiment: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = tmp_path / "run"
    shutil.copytree(experiment / "mem" / "seed_1", run_dir)
    assert main(["export", str(run_dir)]) == 0
    manifest = json.loads((run_dir / "export" / "manifest.json").read_text())
    assert manifest["format"] in ("csv", "parquet") and manifest["tables"]["events"]["rows"] > 0
    assert "manifest" in capsys.readouterr().out
    out = tmp_path / "flat"
    assert main(["export", str(run_dir), "--out", str(out), "--format", "auto"]) == 0
    assert (out / "manifest.json").is_file()
    # the `void export parquet RUN_DIR` alias: parquet, or exit 2 naming the extra when pyarrow is absent
    rc = main(["export", "parquet", str(run_dir), "--out", str(tmp_path / "pq")])
    if importlib.util.find_spec("pyarrow") is None:
        assert rc == 2 and "install the export extra" in capsys.readouterr().err
    else:
        assert rc == 0 and json.loads((tmp_path / "pq" / "manifest.json").read_text())["format"] == "parquet"
    assert main(["export", "parquet", str(run_dir), "--format", "csv"]) == 2
    assert main(["export", "a", "b", "c"]) == 2
    assert main(["export", str(tmp_path / "nope")]) == 1


def test_export_forwards_fmt_and_maps_missing_pyarrow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    ex = importlib.import_module("void.research.export")

    (tmp_path / "world.db").write_bytes(b"")
    seen: list[str] = []

    def export_run(run_dir: Any, out_dir: Any = None, *, fmt: str = "auto") -> dict[str, Any]:
        seen.append(fmt)
        if fmt == "parquet":
            raise RuntimeError("pyarrow is not installed")
        return {"format": fmt, "tables": {}}

    monkeypatch.setattr(ex, "export_run", export_run)
    assert main(["export", str(tmp_path), "--format", "csv"]) == 0
    assert main(["export", str(tmp_path)]) == 0
    assert main(["export", "parquet", str(tmp_path)]) == 2
    assert seen == ["csv", "auto", "parquet"]
    err = capsys.readouterr().err
    assert "install the export extra" in err and "Traceback" not in err

    def broken(run_dir: Any, out_dir: Any = None, *, fmt: str = "auto") -> dict[str, Any]:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(ex, "export_run", broken)
    with pytest.raises(RuntimeError, match="disk on fire"):  # only the missing-pyarrow error is mapped
        main(["export", str(tmp_path)])


# --- serve and run into an existing run directory ------------------------------------------------------------------
def test_serve_into_an_existing_run_dir_exits_2_without_traceback(tmp_path: Path, tiny: str, no_port: list[dict[str, Any]],
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    serve = ["serve", "--config", tiny, "--out", str(tmp_path), "--static", str(tmp_path / "nostatic")]
    assert main(serve) == 0  # the first serve creates the run (the replaced runner returns at once)
    assert len(no_port) == 1
    capsys.readouterr()
    run_dir = tmp_path / "scripted_smoke"
    assert main(serve) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [f"run dir {run_dir} {EXISTING}"]
    assert len(no_port) == 1
    # --resume reopens it
    assert main([*serve, "--resume"]) == 0
    assert len(no_port) == 2
    # --fresh archives it and starts over
    assert main([*serve, "--fresh"]) == 0
    assert len(no_port) == 3
    assert (tmp_path / "scripted_smoke.prev-1" / "world.db").is_file() and (run_dir / "world.db").is_file()
    assert f"archived {run_dir} -> {tmp_path / 'scripted_smoke.prev-1'}" in capsys.readouterr().err
    # a run made by `void run` is refused the same way
    assert main(["run", "--config", tiny, "--out", str(tmp_path / "r")]) == 0
    capsys.readouterr()
    assert main(["serve", "--config", tiny, "--out", str(tmp_path / "r")]) == 2
    assert capsys.readouterr().err.splitlines() == [f"run dir {tmp_path / 'r' / 'scripted_smoke'} {EXISTING}"]


def test_serve_catches_the_simulation_refusal(tmp_path: Path, tiny: str, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    import void.server.app as app

    def refuse(cfg: Any, run_dir: Path, **kw: Any) -> None:
        raise RuntimeError(f"run scripted_smoke-s42 already exists at {run_dir}; use resume=True or a new name/seed")

    monkeypatch.setattr(app, "serve", refuse)
    args = ["serve", "--config", tiny, "--out", str(tmp_path), "--static", str(tmp_path)]
    assert main(args) == 2
    err = capsys.readouterr().err
    assert err.splitlines()[-1] == f"run dir {tmp_path / 'scripted_smoke'} {EXISTING}" and "Traceback" not in err

    def crash(cfg: Any, run_dir: Path, **kw: Any) -> None:
        raise RuntimeError("something else")

    monkeypatch.setattr(app, "serve", crash)
    with pytest.raises(RuntimeError, match="something else"):
        main(args)


def test_run_fresh_archives_the_old_dir(tmp_path: Path, tiny: str, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["run", "--config", tiny, "--out", str(tmp_path)]
    run_dir = tmp_path / "scripted_smoke"
    assert main(args) == 0
    first = (run_dir / "config.resolved.yaml").read_text()
    capsys.readouterr()
    assert main(args) == 2
    assert f"run dir {run_dir} {EXISTING}" in capsys.readouterr().err.splitlines()
    assert main([*args, "--fresh"]) == 0
    assert (tmp_path / "scripted_smoke.prev-1" / "config.resolved.yaml").read_text() == first
    assert (run_dir / "world.db").is_file()
    assert main([*args, "--fresh"]) == 0  # the first free n
    assert sorted(p.name for p in tmp_path.iterdir()) == ["scripted_smoke", "scripted_smoke.prev-1", "scripted_smoke.prev-2"]
    assert f"archived {run_dir} -> {tmp_path / 'scripted_smoke.prev-2'}" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main([*args, "--fresh", "--resume"])
    assert exc.value.code == 2


def test_data_dir_default_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_port: list[dict[str, Any]],
                                      capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)  # nothing may land in the cwd's data/runs
    elsewhere = tmp_path / "elsewhere"
    config = tmp_path / "cfg.yaml"
    config.write_text(yaml.safe_dump({"extends": SMOKE, "run": {**TINY["run"], "data_dir": str(elsewhere)}}))
    assert main(["run", "--config", str(config), "--days", "1"]) == 0
    assert (elsewhere / "scripted_smoke" / "world.db").is_file()
    assert not (tmp_path / "data").exists()
    capsys.readouterr()
    # serve looks in the same place: the run is already there
    assert main(["serve", "--config", str(config)]) == 2
    assert capsys.readouterr().err.strip() == f"run dir {elsewhere / 'scripted_smoke'} {EXISTING}"
    assert no_port == []


def test_mock_feed_defaults_to_the_committed_fixture_world() -> None:
    args = build_parser().parse_args(["mock-feed"])
    assert args.config == DEFAULT_MOCK_CONFIG == "configs/demo_memory.yaml"
    assert args.out == "web/public/fixtures/mock.jsonl"
