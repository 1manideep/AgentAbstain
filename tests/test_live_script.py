"""scripts/live_claude.sh end to end, offline: a stand-in `void` (a shell script that never calls a model), a
temporary OUT and a temporary ledger, LEDGER_PUSH=0 so nothing is committed. Covered: the plan stays inside the
budget; a completed run is verified again before it is skipped; a run the ledger records but whose directory is gone
is refused; a run is reserved in the ledger before it starts and recorded after; the preconditions (ledger, workspace
spend limit); the scarcity pilot's claims gate.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_live_budget import make_run, write_ledger

from void.economy.budget import LEDGER_FILE, read_ledger

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "live_claude.sh"
SMOKE_LINE = next(e for e in read_ledger(LEDGER_FILE).values() if e["run_id"] == "live_claude_smoke-s42")
LADDER_CALLS = [{"purpose": p} for p in ("decide", "decide", "maintain", "exam")]


def fake_void(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in for `void run`: records that it was called (and the ledger attempt it was handed), then copies
    $FAKE_TEMPLATE to $FAKE_TARGET when both are set, else fails."""
    marker = tmp_path / "void_called"
    script = tmp_path / "void"
    script.write_text(f"""#!/bin/sh
echo "$VOID_LEDGER_ATTEMPT" >> "{marker}"
[ -n "$VOID_LEDGER_ATTEMPT" ] && [ -n "$FAKE_TEMPLATE" ] || exit 2
mkdir -p "$(dirname "$FAKE_TARGET")" && cp -r "$FAKE_TEMPLATE" "$FAKE_TARGET"
""")
    script.chmod(0o755)
    return script, marker


def run_script(tmp_path: Path, ledger: Path, **env: str) -> subprocess.CompletedProcess[str]:
    void, _ = fake_void(tmp_path)
    full = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "FAKE_"))}
    full.update(PY=sys.executable, VOID=str(void), OUT=str(tmp_path / "out"), REPO_LIVE=str(tmp_path / "repo_live"),
                LEDGER=str(ledger), LEDGER_PUSH="0", ANTHROPIC_API_KEY="offline-placeholder", CONSOLE_SPEND_LIMIT_USD="60",
                STAGES="ladder")
    full.update(env)
    return subprocess.run([str(SCRIPT)], env=full, capture_output=True, text=True, timeout=240)


def smoke_ledger(tmp_path: Path, *more: dict[str, Any]) -> Path:
    return write_ledger(tmp_path / "ledger.jsonl", SMOKE_LINE, *more)


def test_the_plan_fits_the_budget_with_the_committed_ledger(tmp_path: Path) -> None:
    p = run_script(tmp_path, LEDGER_FILE, PLAN_ONLY="1", STAGES="smoke ladder memdep memdep_veins scarcity")
    assert p.returncode == 0, p.stderr
    spent = float(re.search(r"already spent \$([\d.]+)", p.stdout).group(1))
    most = float(re.search(r"can spend at most \$([\d.]+)", p.stdout).group(1))
    assert spent >= 0.156798 and "stage smoke: caps up to $0.00" in p.stdout  # the ledger says the smoke passed
    assert spent + most <= 60.0


def test_a_completed_run_that_does_not_verify_is_not_skipped(tmp_path: Path) -> None:
    make_run(tmp_path / "out" / "live_claude_ladder", "live_claude_ladder.yaml",
             calls=LADDER_CALLS + [{"error": "rate_limit: slow down"}] * 2)
    p = run_script(tmp_path, smoke_ledger(tmp_path))
    assert p.returncode == 1 and "completed but does not verify" in p.stderr and "_rejected" in p.stderr
    assert not (tmp_path / "void_called").exists()


def test_a_completed_run_that_verifies_is_skipped(tmp_path: Path) -> None:
    make_run(tmp_path / "out" / "live_claude_ladder", "live_claude_ladder.yaml", calls=LADDER_CALLS)
    p = run_script(tmp_path, smoke_ledger(tmp_path))
    assert p.returncode == 0, p.stderr
    assert "already completed and verified" in p.stdout and not (tmp_path / "void_called").exists()


def test_a_run_the_ledger_records_but_whose_directory_is_gone_is_refused(tmp_path: Path) -> None:
    ledger = smoke_ledger(tmp_path, {"attempt": "x", "run_id": "live_claude_ladder-s42", "state": "reserved",
                                     "cap_micro": 10_000_000})
    p = run_script(tmp_path, ledger)
    assert p.returncode == 1 and "paid for before" in p.stderr and not (tmp_path / "void_called").exists()


def test_a_run_is_reserved_before_it_starts_and_recorded_after(tmp_path: Path) -> None:
    template = make_run(tmp_path / "template", "live_claude_ladder.yaml", calls=LADDER_CALLS)
    ledger = smoke_ledger(tmp_path)
    p = run_script(tmp_path, ledger, FAKE_TEMPLATE=str(template), FAKE_TARGET=str(tmp_path / "out" / "live_claude_ladder"))
    assert p.returncode == 0, p.stderr
    attempt = (tmp_path / "void_called").read_text().strip()
    lines = [json.loads(line) for line in ledger.read_text().splitlines()]
    reserved, final = lines[1], lines[2]
    assert reserved["attempt"] == final["attempt"] == attempt and reserved["state"] == "reserved"
    assert reserved["run_id"] == "live_claude_ladder-s42" and reserved["cap_micro"] == 10_000_000
    assert final["state"] == "final" and final["status"] == "completed" and final["verified"] is True
    assert final["real_micro"] == 4_000 and final["calls"] == 4
    assert "LEDGER_PUSH=0" in p.stderr


def test_a_run_that_fails_is_still_recorded(tmp_path: Path) -> None:
    ledger = smoke_ledger(tmp_path)
    p = run_script(tmp_path, ledger)  # the stand-in fails before writing anything
    assert p.returncode == 1 and "run did not complete" in p.stderr
    final = [json.loads(line) for line in ledger.read_text().splitlines()][-1]
    assert final["state"] == "final" and final["status"] == "not_started" and final["real_micro"] == 0


def test_nothing_starts_when_the_reservation_cannot_be_pushed(tmp_path: Path) -> None:
    ledger = smoke_ledger(tmp_path)
    # LEDGER_PUSH on, but the checkout is not on the ledger's branch: git is never asked to commit or push
    p = run_script(tmp_path, ledger, LEDGER_PUSH="1", LEDGER_BRANCH="no-such-branch-for-tests")
    assert p.returncode == 1 and "nothing was started" in p.stderr and not (tmp_path / "void_called").exists()
    reserved, final = [json.loads(line) for line in ledger.read_text().splitlines()][1:]
    assert reserved["state"] == "reserved" and final["status"] == "not_started" and final["real_micro"] == 0
    # so the next attempt is not mistaken for a lost paid run, and its cap is not counted twice
    p = run_script(tmp_path, ledger, PLAN_ONLY="1")
    assert p.returncode == 0 and "already spent $0.156798" in p.stdout
    p = run_script(tmp_path, ledger)
    assert "paid for before" not in p.stderr and (tmp_path / "void_called").exists()


@pytest.mark.parametrize("env, message", [
    ({"CONSOLE_SPEND_LIMIT_USD": ""}, "CONSOLE_SPEND_LIMIT_USD"),
    ({"CONSOLE_SPEND_LIMIT_USD": "75"}, "at most BUDGET_USD"),
])
def test_a_paid_run_needs_a_stated_workspace_spend_limit(tmp_path: Path, env: dict[str, str], message: str) -> None:
    p = run_script(tmp_path, smoke_ledger(tmp_path), **env)
    assert p.returncode == 1 and message in p.stderr and not (tmp_path / "void_called").exists()


def test_no_ledger_no_plan(tmp_path: Path) -> None:
    p = run_script(tmp_path, tmp_path / "missing.jsonl", PLAN_ONLY="1")
    assert p.returncode == 1 and "no spend ledger" in p.stderr


def test_the_scarcity_pilot_stops_the_stage_when_runs_list_too_few_claims(tmp_path: Path) -> None:
    out = tmp_path / "out" / "exp_scarcity_claude"
    for arm in ("high", "low"):
        make_run(out / arm / "seed_1", f"live_claude_scarcity_{arm}.yaml", seed=1, calls=[{"action_type": "move"}] * 3)
    p = run_script(tmp_path, smoke_ledger(tmp_path), STAGES="scarcity", SCARCITY_SEEDS="1 2 3", SCARCITY_PILOT="1",
                   MIN_CLAIMS="10")
    assert p.returncode == 1 and "scarcity pilot" in p.stderr
    assert not (tmp_path / "void_called").exists()  # seeds 2 and 3 never started
