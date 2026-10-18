"""scripts/live_claude.sh end to end, offline: a stand-in `void` (a shell script that never calls a model), a
temporary OUT and a temporary ledger, LEDGER_PUSH=0 so nothing is committed (or a stand-in `git`, or a scratch
repository with a scratch origin). Covered: the plan stays inside the budget and leaves out runs already done or in
flight; a completed run is verified again before it is skipped; a run the ledger records but whose directory is gone
is refused, with what to do about an attempt that never finished; a run is reserved in the ledger before it starts and
recorded after, also when the script is stopped by a signal, before or while `void run` runs; a rerun closes an
attempt a killed script left open; ledger lines left off origin are pushed; the preconditions (ledger, workspace
spend limit); the scarcity pilot's claims gate.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
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


def script_env(tmp_path: Path, ledger: Path, void: Path | None = None, **env: str) -> dict[str, str]:
    void = void or fake_void(tmp_path)[0]
    full = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "FAKE_"))}
    full.update(PY=sys.executable, VOID=str(void), OUT=str(tmp_path / "out"), REPO_LIVE=str(tmp_path / "repo_live"),
                LEDGER=str(ledger), LEDGER_PUSH="0", ANTHROPIC_API_KEY="offline-placeholder", CONSOLE_SPEND_LIMIT_USD="60",
                STAGES="ladder", PYTHONPATH=str(REPO))
    full.update(env)
    return full


def run_script(tmp_path: Path, ledger: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(SCRIPT)], env=script_env(tmp_path, ledger, **env), capture_output=True, text=True, timeout=240)


def smoke_ledger(tmp_path: Path, *more: dict[str, Any]) -> Path:
    return write_ledger(tmp_path / "ledger.jsonl", SMOKE_LINE, *more)


def ledger_lines(ledger: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in ledger.read_text().splitlines()]


def plan(p: subprocess.CompletedProcess[str]) -> tuple[float, float, dict[str, float]]:
    assert p.returncode == 0, p.stderr
    spent = float(re.search(r"already spent \$([\d.]+)", p.stdout).group(1))
    most = float(re.search(r"can spend at most \$([\d.]+)", p.stdout).group(1))
    return spent, most, {m[0]: float(m[1]) for m in re.findall(r"stage (\w+): caps up to \$([\d.]+)", p.stdout)}


ALL_STAGES = "smoke ladder memdep memdep_veins scarcity"


def test_the_plan_fits_the_budget(tmp_path: Path) -> None:
    # a frozen ledger (the smoke alone), so the arithmetic does not move as live runs add lines to the real one
    spent, most, stages = plan(run_script(tmp_path, smoke_ledger(tmp_path), PLAN_ONLY="1", STAGES=ALL_STAGES))
    assert spent == 0.156798 and stages["smoke"] == 0.0  # the ledger says the smoke passed
    assert stages == {"smoke": 0.0, "ladder": 10.0, "memdep": 11.52, "memdep_veins": 23.04, "scarcity": 14.40}
    assert spent + most <= 60.0


def test_the_committed_ledger_reads_and_plans(tmp_path: Path) -> None:
    # invariants only: every live run adds lines to it
    smoke = [e for e in read_ledger(LEDGER_FILE).values() if e["run_id"] == "live_claude_smoke-s42"]
    assert smoke and smoke[0]["status"] == "completed" and smoke[0]["verified"] is True
    spent, _, stages = plan(run_script(tmp_path, LEDGER_FILE, PLAN_ONLY="1", STAGES=ALL_STAGES))
    assert spent >= 0.156798 and stages["smoke"] == 0.0


def test_the_plan_leaves_out_runs_done_or_in_flight(tmp_path: Path) -> None:
    ladder = {"attempt": "l1", "run_id": "live_claude_ladder-s42", "state": "reserved", "cap_micro": 10_000_000,
              "ts": "2026-10-17T10:00:00Z"}
    # in flight: counted at its cap in the spend, so not again in the plan
    spent, most, stages = plan(run_script(tmp_path, smoke_ledger(tmp_path, ladder), PLAN_ONLY="1", STAGES=ALL_STAGES))
    assert spent == 10.156798 and stages["ladder"] == 0.0 and spent + most <= 60.0
    # done: completed, verified, recorded at its real spend
    make_run(tmp_path / "out" / "live_claude_ladder", "live_claude_ladder.yaml", calls=LADDER_CALLS)
    done = {**ladder, "state": "final", "status": "completed", "verified": True, "real_micro": 3_400_000}
    spent, most, stages = plan(run_script(tmp_path, smoke_ledger(tmp_path, ladder, done), PLAN_ONLY="1", STAGES=ALL_STAGES))
    assert spent == 3.556798 and stages["ladder"] == 0.0 and spent + most <= 60.0
    # a paired run done on disk leaves its cap out of its stage
    make_run(tmp_path / "out" / "exp_mem_dep_claude" / "mem" / "seed_1", "live_claude_dep_mem.yaml", seed=1, calls=[{}] * 3)
    assert plan(run_script(tmp_path, smoke_ledger(tmp_path), PLAN_ONLY="1", STAGES="memdep"))[2]["memdep"] == 11.16


def test_the_plan_counts_again_a_run_that_would_be_redone(tmp_path: Path) -> None:
    failed = {"attempt": "l1", "run_id": "live_claude_ladder-s42", "state": "final", "status": "failed", "verified": False,
              "real_micro": 2_000_000}
    make_run(tmp_path / "out" / "live_claude_ladder", "live_claude_ladder.yaml", status="failed", calls=LADDER_CALLS)
    spent, _, stages = plan(run_script(tmp_path, smoke_ledger(tmp_path, failed), PLAN_ONLY="1"))
    assert spent == 2.156798 and stages["ladder"] == 10.0


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
                                     "cap_micro": 10_000_000, "ts": "2026-10-17T10:00:00Z"})
    p = run_script(tmp_path, ledger)
    assert p.returncode == 1 and "paid for before" in p.stderr and not (tmp_path / "void_called").exists()
    # an attempt that never finished is told apart, with how to close it if it never started
    assert "reserved at 2026-10-17T10:00:00Z and never recorded as finished" in p.stderr
    assert f"final --ledger {ledger} --attempt x --run-dir" in p.stderr


def test_a_rerun_closes_the_attempt_of_a_run_that_finished_after_its_script_died(tmp_path: Path) -> None:
    # the script got TERM before the trap existed (or SIGKILL): `void run` finished alone, no final line was written
    make_run(tmp_path / "out" / "live_claude_ladder", "live_claude_ladder.yaml", calls=LADDER_CALLS,
             created_at="2026-10-17T10:00:03Z")
    ledger = smoke_ledger(tmp_path, {"attempt": "x", "run_id": "live_claude_ladder-s42", "run": "live_claude_ladder",
                                     "state": "reserved", "cap_micro": 10_000_000, "ts": "2026-10-17T10:00:00Z"})
    p = run_script(tmp_path, ledger)
    assert p.returncode == 0 and "already completed and verified" in p.stdout, p.stderr
    final = ledger_lines(ledger)[-1]
    assert final["attempt"] == "x" and final["state"] == "final" and final["real_micro"] == 4_000 and final["verified"] is True
    assert "already spent $0.160798" in run_script(tmp_path, ledger, PLAN_ONLY="1").stdout  # not the $10 cap


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


def hanging_void(tmp_path: Path) -> Path:
    """A stand-in for `void run` that writes a running run directory (copied from $FAKE_TEMPLATE), says so, and then
    waits to be stopped, as a long paid run would."""
    script = tmp_path / "void_hangs"
    script.write_text(f"""#!/bin/sh
mkdir -p "$(dirname "$FAKE_TARGET")" && cp -r "$FAKE_TEMPLATE" "$FAKE_TARGET" && : > "{tmp_path / 'void_started'}"
exec sleep 300
""")
    script.chmod(0o755)
    return script


def wait_for(path: Path, proc: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 120
    while not path.exists():
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.1)


@pytest.mark.parametrize("sig, to_group, code", [(signal.SIGTERM, False, 143), (signal.SIGINT, True, 130)])
def test_a_run_stopped_by_a_signal_is_recorded_and_its_void_stopped(tmp_path: Path, sig: int, to_group: bool,
                                                                     code: int) -> None:
    template = make_run(tmp_path / "template", "live_claude_ladder.yaml", status="running", calls=LADDER_CALLS)
    ledger = smoke_ledger(tmp_path)
    env = script_env(tmp_path, ledger, hanging_void(tmp_path), FAKE_TEMPLATE=str(template),
                     FAKE_TARGET=str(tmp_path / "out" / "live_claude_ladder"))
    proc = subprocess.Popen([str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        wait_for(tmp_path / "void_started", proc)
        if to_group:  # Ctrl-C in a terminal, or a tool timeout: the whole process group
            os.killpg(proc.pid, sig)
        else:  # the script alone: before the trap, `void run` went on as an orphan
            os.kill(proc.pid, sig)
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == code, err
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, 0)  # `void run` was stopped and reaped, not left running as an orphan
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    reserved, final = ledger_lines(ledger)[1:]
    assert reserved["state"] == "reserved" and final["attempt"] == reserved["attempt"] and final["state"] == "final"
    assert final["status"] == "running" and final["real_micro"] == 4_000 and final["verified"] is False
    assert "stopping live_claude_ladder.yaml" in err


def fake_git(tmp_path: Path) -> Path:
    """A stand-in for git on PATH: everything succeeds and does nothing, except that the commit of a reservation hangs
    (the first time) until it is stopped, and every call is logged."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(f"""#!/bin/sh
echo "$*" >> "{tmp_path / 'git_calls'}"
case "$*" in
  *"rev-parse --abbrev-ref HEAD"*) echo "$LEDGER_BRANCH" ;;
  *"diff --quiet"*) exit 1 ;;
  *"rev-parse -q --verify"*) exit 1 ;;
  *"commit"*"live spend: reserve"*)
    if [ ! -e "{tmp_path / 'git_hung'}" ]; then : > "{tmp_path / 'git_hung'}"; exec sleep 300; fi ;;
esac
exit 0
""")
    git.chmod(0o755)
    return bin_dir


def test_a_run_stopped_before_void_starts_is_closed_at_zero(tmp_path: Path) -> None:
    ledger = smoke_ledger(tmp_path)
    env = script_env(tmp_path, ledger, LEDGER_PUSH="1", LEDGER_BRANCH="live-test",
                     PATH=f"{fake_git(tmp_path)}{os.pathsep}{os.environ['PATH']}")
    proc = subprocess.Popen([str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        wait_for(tmp_path / "git_hung", proc)  # Ctrl-C while the reservation is being pushed
        os.killpg(proc.pid, signal.SIGINT)
        _, err = proc.communicate(timeout=120)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert proc.returncode == 130, err
    assert not (tmp_path / "void_called").exists()
    reserved, final = ledger_lines(ledger)[1:]
    assert final["attempt"] == reserved["attempt"] and final["status"] == "not_started" and final["real_micro"] == 0
    assert "live spend: final" in (tmp_path / "git_calls").read_text()  # and it was committed on the way out
    # so the rerun is not refused as a lost paid run, and the never-used cap is not counted
    assert "already spent $0.156798" in run_script(tmp_path, ledger, PLAN_ONLY="1").stdout
    p = run_script(tmp_path, ledger)
    assert "paid for before" not in p.stderr and (tmp_path / "void_called").exists()


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True).stdout


GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def test_ledger_lines_an_interrupted_run_left_off_origin_are_pushed(tmp_path: Path) -> None:
    # a scratch checkout of the scripts with a scratch origin: the script's own git commands, for real
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git("init", "-q", "--bare", "-b", "live", str(origin), cwd=tmp_path)
    (repo / "scripts").mkdir(parents=True)
    for name in ("live_claude.sh", "live_budget.py"):
        shutil.copy2(REPO / "scripts" / name, repo / "scripts" / name)
    (repo / "configs").symlink_to(REPO / "configs")
    ledger = smoke_ledger(tmp_path)
    (repo / "docs" / "research").mkdir(parents=True)
    shutil.copy2(ledger, repo / "docs" / "research" / "live_spend.jsonl")
    ledger = repo / "docs" / "research" / "live_spend.jsonl"
    git("init", "-q", "-b", "live", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("remote", "add", "origin", str(origin), cwd=repo)
    git("push", "-q", "-u", "origin", "live", cwd=repo)
    # an earlier run's final line was committed but its push failed, and a closing line was never committed
    closed = {"attempt": "o", "run_id": "live_claude_dep_mem-s1", "state": "final", "status": "not_started", "real_micro": 0}
    with ledger.open("a") as f:
        f.write(json.dumps({**closed, "state": "reserved", "cap_micro": 360_000}) + "\n")
    git("commit", "-q", "-am", "live spend: reserve o", cwd=repo)
    with ledger.open("a") as f:
        f.write(json.dumps(closed) + "\n")
    make_run(tmp_path / "out" / "live_claude_ladder", "live_claude_ladder.yaml", calls=LADDER_CALLS)  # skipped
    env = {**script_env(tmp_path, ledger, LEDGER_PUSH="1", LEDGER_BRANCH="live"),
           **{k: v for k, v in GIT_ENV.items() if k.startswith("GIT_")}}
    p = subprocess.run([str(repo / "scripts" / "live_claude.sh")], env=env, capture_output=True, text=True, timeout=240)
    assert p.returncode == 0 and "all stages done" in p.stdout, p.stderr
    assert git("rev-parse", "HEAD", cwd=repo) == git("rev-parse", "live", cwd=origin)
    assert git("status", "--porcelain", cwd=repo) == ""
    assert git("show", "live:docs/research/live_spend.jsonl", cwd=origin) == ledger.read_text()


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


def completed_pair_run(run_dir: Path, config: str, seed: int, mean_balance: float) -> Path:
    """A completed paired run as compare and the gates read it: verifying calls, the run row, and a metrics tail at the
    config's last tick that carries its end-of-run mean balance."""
    from void.config import load_config

    make_run(run_dir, config, seed=seed, calls=[{}] * 3)
    cfg = load_config(REPO / "configs" / config, {"run": {"seed": seed}})
    row = {"tick": cfg.run.days * cfg.run.ticks_per_day, "day": cfg.run.days, "mean_balance": mean_balance}
    (run_dir / "metrics.jsonl").write_text(json.dumps(row) + "\n")
    return run_dir


def test_the_gate_reading_is_printed_after_the_dependence_stage_and_never_fails_the_script(tmp_path: Path) -> None:
    out = tmp_path / "out" / "exp_mem_dep_claude"
    for seed, (mem, amnesic) in {1: (1.40, 1.50), 2: (1.38, 1.47)}.items():  # memory loses here: C1 fails
        completed_pair_run(out / "mem" / f"seed_{seed}", "live_claude_dep_mem.yaml", seed, mem)
        completed_pair_run(out / "amnesic" / f"seed_{seed}", "live_claude_dep_amnesic.yaml", seed, amnesic)
    scripted = tmp_path / "no_scripted_runs"
    p = run_script(tmp_path, smoke_ledger(tmp_path), STAGES="memdep", DEP_SEEDS="1 2", GATES_RUNS=str(scripted))
    assert p.returncode == 0, p.stderr
    assert not (tmp_path / "void_called").exists()  # both pairs were done: nothing was started
    start = p.stdout.index(f"=== gates: scripts/gates.py {scripted} --live {tmp_path / 'out'} ===")
    assert p.stdout.index("=== stage memdep done") < start < p.stdout.index("all stages done")
    reading = p.stdout[start:p.stdout.index("=== gates done")]
    assert re.search(r"^C1\[exp_mem_dep_claude\] +FAIL", reading, re.M), reading
    assert re.search(r"^G0 +FAIL", reading, re.M), reading
    assert "=== gates done: exit status 1 (informational" in p.stdout  # gates.py exits 1 and the script goes on


def test_the_gate_reading_follows_the_last_planned_dependence_stage_only(tmp_path: Path) -> None:
    for experiment, prefix, seed in (("exp_mem_dep_claude", "live_claude_dep", 1),
                                     ("exp_mem_dep_veins_claude", "live_claude_dep_veins", 21)):
        for arm in ("mem", "amnesic"):
            completed_pair_run(tmp_path / "out" / experiment / arm / f"seed_{seed}", f"{prefix}_{arm}.yaml", seed, 1.5)
    p = run_script(tmp_path, smoke_ledger(tmp_path), STAGES="memdep memdep_veins", DEP_SEEDS="1", VEIN_SEEDS="21",
                   GATES_RUNS=str(tmp_path / "no_scripted_runs"))
    assert p.returncode == 0, p.stderr
    assert p.stdout.count("=== gates: ") == 1
    assert p.stdout.index("=== stage memdep_veins done") < p.stdout.index("=== gates: ")
    # a plan prints no reading, and neither does a run without a dependence stage
    assert "=== gates" not in run_script(tmp_path, smoke_ledger(tmp_path), PLAN_ONLY="1", STAGES=ALL_STAGES).stdout
    make_run(tmp_path / "out" / "live_claude_ladder", "live_claude_ladder.yaml", calls=LADDER_CALLS)
    p = run_script(tmp_path, smoke_ledger(tmp_path))
    assert p.returncode == 0 and "=== gates" not in p.stdout, p.stderr
