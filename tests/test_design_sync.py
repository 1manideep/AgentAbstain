"""docs/DESIGN.md and docs/PLAN.md match the code (``scripts/check_design_sync.py``).

The script itself is run as a subprocess and must exit 0 with no output. Each of its checks is then shown to bite:
the shipped DESIGN text is broken in one place (a key row renamed, a default changed, a module or a subcommand
dropped from the tree, an experiment's days changed, a column dropped, the CSP changed) and the check must name the
mismatch, so a check that silently matched nothing could not pass here.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_design_sync.py"


@pytest.fixture(scope="module")
def sync() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_design_sync_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def design() -> str:
    return (REPO / "docs" / "DESIGN.md").read_text(encoding="utf-8")


def _broken(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"the fixture string must occur exactly once: {old!r}"
    return text.replace(old, new)


def test_design_and_plan_match_the_code() -> None:
    proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=REPO, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == ""


def test_leaf_paths_walk_every_model(sync: ModuleType) -> None:
    leaves = sync.config_leaf_paths()
    assert len(leaves) == len(set(leaves))
    for path in ("run.data_dir", "world.veins.refill", "population.persona", "tiers.<name>.model",
                 "tiers.<name>.maintenance_strategy", "agents[].personality", "epochs[].arrival.tier",
                 "memory.selection.canary.per_day", "experiment.hypotheses[].min_power", "server.render_delay_ticks"):
        assert path in leaves, path
    # containers of models are walked, never reported as leaves themselves
    for path in ("world", "world.veins", "tiers", "agents", "epochs", "epochs[].arrival", "memory.selection"):
        assert path not in leaves, path


def test_config_check_catches_an_undocumented_key_and_a_wrong_default(sync: ModuleType, design: str) -> None:
    assert sync.check_config(design) == []
    renamed = _broken(design, "| `world.veins.refill` |", "| `world.veins.refil` |")
    out = sync.check_config(renamed)
    assert any("`world.veins.refill` is not documented" in line for line in out), out
    assert any("`world.veins.refil` is not a config key" in line for line in out), out
    wrong = _broken(design, "| `run.days` | `10` |", "| `run.days` | `11` |")
    out = sync.check_config(wrong)
    assert out == ["DESIGN §4: `run.days` default `11` but configs/base.yaml resolves to 10"], out


def test_layout_check_catches_tree_and_cli_drift(sync: ModuleType, design: str) -> None:
    assert sync.check_layout(design) == []
    missing = _broken(design, " policy.py vault_stats.py\n", " policy.py\n")
    assert sync.check_layout(missing) == ["DESIGN §2: void/memory/vault_stats.py exists but is not in the tree"]
    extra = _broken(design, "  world/      kernel.py", "  world/      state.py kernel.py")
    assert sync.check_layout(extra) == ["DESIGN §2: the tree lists void/world/state.py, which does not exist"]
    no_mcp = _broken(design, "| generations | mcp\n", "| generations\n")
    assert sync.check_layout(no_mcp) == ["DESIGN §2: subcommand `mcp` (void --help) is not on the cli.py line"]


def test_experiment_check_catches_a_changed_row(sync: ModuleType, design: str) -> None:
    assert sync.check_experiments(design) == []
    longer = _broken(design, "| `exp_gate` | `on`, `off` | 30 | 12 |", "| `exp_gate` | `on`, `off` | 31 | 12 |")
    out = sync.check_experiments(longer)
    assert len(out) == 2 and all("exp_gate" in line and "days 31" in line for line in out), out
    no_arm = _broken(design, "| `exp_memory` | `graph`, `vector` |", "| `exp_memory` | `graph` |")
    assert sync.check_experiments(no_arm) == ["DESIGN §17 exp_memory (configs/exp_memory_vector.yaml): arm `vector` is not listed"]
    ghost = _broken(design, "| `exp_memory` | `graph`, `vector` |", "| `exp_memory` | `graph`, `vector`, `hybrid` |")
    assert sync.check_experiments(ghost) == ["DESIGN §17: `exp_memory` lists arm `hybrid`, which no configs/exp_*.yaml defines"]


def test_data_model_check_catches_a_missing_column(sync: ModuleType, design: str) -> None:
    assert sync.check_data_model(design) == []
    out = sync.check_data_model(_broken(design, " experiment, arm,\n    windfall_string)", " experiment, arm)"))
    assert out == ["DESIGN §3: column `run.windfall_string` is not declared"]


def test_csp_paths_and_model_identifiers(sync: ModuleType, design: str) -> None:
    assert sync.check_csp(design) == []
    out = sync.check_csp(_broken(design, "base-uri 'none'; form-action 'none'`.", "base-uri 'none'; form-action 'self'`."))
    assert len(out) == 1 and "differs from void.server.auth.CSP" in out[0], out
    assert sync.check_paths(design) == []
    assert sync.check_paths("see `tests/test_no_such_file.py` and `configs/exp_*.yaml`") == [
        "DESIGN: `tests/test_no_such_file.py` does not exist"]
    # a made-up identifier, joined at runtime so this file's own text holds no identifier-shaped literal (the
    # repository-wide model-id sweep would flag it); the detector still sees the whole string
    fake_id = "-".join(["gemini", "9", "pro", "001"])
    assert sync.check_model_ids("doc", f"a tier on Gemini rungs\nserved by {fake_id} today") == [
        f"doc:2: model identifier `{fake_id}` (refer to tiers by rung name)"]
