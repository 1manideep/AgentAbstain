"""The live budget's records and guards, offline: the committed spend ledger (a lost or moved run still counts),
the spend roots `void run --budget-usd` adds up, the commands that must never start a paid simulation outside the
budget (mock-feed, analyze --replay), verify's checks, and the outcomes the paired live stages report.

Run directories here are small hand-made ``world.db`` files (the real schema, a run row, a few ``llm_calls`` rows);
nothing calls a model.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from void.config import load_config
from void.db import Database
from void.economy.budget import LEDGER_FILE, reachable_providers, read_ledger, spend_under

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"


def _load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


live_budget = _load_script("live_budget")


def make_run(run_dir: Path, config: str, *, seed: int | None = None, status: str = "completed",
             calls: list[dict[str, Any]] | None = None, events: list[tuple[str, dict[str, Any]]] = (),
             ledger: list[tuple[str, int]] = ()) -> Path:
    """A run directory as `void run` leaves it, reduced to what the budget and verify read."""
    cfg = load_config(CONFIGS / config, {"run": {"seed": seed}} if seed is not None else {})
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.resolved.yaml").write_text(cfg.to_yaml())
    tier = cfg.agents[0].tier
    db = Database(run_dir / "world.db")
    db.conn.execute("INSERT INTO run(run_id, config_hash, config_yaml, seed, created_at, schema_version, status) "
                    "VALUES(?,?,?,?,?,?,?)", (f"{cfg.run.name}-s{cfg.run.seed}", cfg.hash(), "", cfg.run.seed,
                                              "2026-10-17T00:00:00Z", 2, status))
    for i, c in enumerate(calls or []):
        row = {"call_id": f"cl_{i:04d}", "tick": 1 + i, "agent_id": "a1", "purpose": "decide", "tier": tier,
               "model": cfg.tiers[tier].model, "provider": cfg.tiers[tier].provider, "input_tokens": 500,
               "output_tokens": 180, "cache_read_tokens": 0, "cache_write_tokens": 0, "real_cost": 1000,
               "world_cost": 2500, "stop_reason": "end_turn", "request_id": f"req_{i:03d}", "error": None, **c}
        db.conn.execute(f"INSERT INTO llm_calls({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
    for kind, payload in events:
        db.conn.execute("INSERT INTO events(tick, day, kind, payload) VALUES(1, 1, ?, ?)", (kind, json.dumps(payload)))
    for i, (kind, delta) in enumerate(ledger):
        db.conn.execute("INSERT INTO wallet_ledger(entry_id, tick, agent_id, delta, balance_after, kind) VALUES(?,?,?,?,?,?)",
                        (f"le_{i}", 1, "a1", delta, 0, kind))
    db.close()
    return run_dir


def write_ledger(path: Path, *entries: dict[str, Any]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


# --- the committed ledger ---------------------------------------------------------------------------------------
def test_the_committed_ledger_records_the_smoke_spend() -> None:
    attempts = read_ledger(LEDGER_FILE)
    smoke = [e for e in attempts.values() if e["run_id"] == "live_claude_smoke-s42"]
    assert smoke and smoke[0]["state"] == "final" and smoke[0]["status"] == "completed" and smoke[0]["verified"] is True
    assert smoke[0]["real_micro"] == 156_798 and smoke[0]["calls"] == 26
    # a fresh clone with no run directories still counts it
    total, rows = spend_under([], ledger=LEDGER_FILE)
    assert rows == [] and total >= 156_798


def test_ledger_and_disk_combine_per_run_id_without_double_counting(tmp_path: Path) -> None:
    out = tmp_path / "out"
    make_run(out / "live_claude_ladder", "live_claude_ladder.yaml", calls=[{"real_cost": 4_000}] * 3)  # 12,000 on disk
    ledger = write_ledger(
        tmp_path / "ledger.jsonl",
        {"attempt": "a1", "run_id": "live_claude_ladder-s42", "state": "reserved", "cap_micro": 10_000_000},
        {"attempt": "a1", "run_id": "live_claude_ladder-s42", "state": "final", "real_micro": 12_000},
        {"attempt": "a2", "run_id": "live_claude_dep_mem-s3", "state": "final", "real_micro": 50_000},  # directory lost
        {"attempt": "a3", "run_id": "live_claude_dep_amnesic-s3", "state": "reserved", "cap_micro": 360_000},  # died
    )
    assert spend_under([out])[0] == 12_000  # disk alone
    total, _ = spend_under([out], ledger=ledger)
    assert total == 12_000 + 50_000 + 360_000  # the ladder once (max, not sum), the lost run, the open reservation
    assert spend_under([out], ledger=ledger, exclude_attempt="a3")[0] == 12_000 + 50_000
    # a run moved out of every root still counts through the ledger
    assert spend_under([tmp_path / "elsewhere"], ledger=ledger)[0] == total


def test_an_unreadable_ledger_is_an_error_not_zero(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        spend_under([], ledger=tmp_path / "missing.jsonl")
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"attempt": "a", "run_id": "r", "state": "final", "real_micro": 5}\nnot json\n')
    with pytest.raises(ValueError):
        spend_under([], ledger=bad)


def test_reserve_final_lost_and_passed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, ledger = tmp_path / "out", write_ledger(tmp_path / "ledger.jsonl")
    config = str(CONFIGS / "live_claude_dep_mem.yaml")
    args = ["--ledger", str(ledger), "--config", config, "--seed", "3"]
    assert live_budget.main(["lost", *args, "--root", str(out)]) == 0  # nothing recorded yet
    assert live_budget.main(["reserve", *args, "--out", str(out)]) == 0
    attempt = capsys.readouterr().out.strip()
    (entry,) = read_ledger(ledger).values()
    assert entry["attempt"] == attempt and entry["run_id"] == "live_claude_dep_mem-s3" and entry["state"] == "reserved"
    assert entry["cap_micro"] == 360_000 and entry["run"] == "exp_mem_dep_claude/mem/seed_3"
    assert spend_under([out], ledger=ledger)[0] == 360_000  # counted at its cap while it runs
    # the run directory is gone (a new machine): refused until restored or rerun deliberately
    assert live_budget.main(["lost", *args, "--root", str(out)]) == live_budget.EXIT_LOST
    run_dir = make_run(out / "exp_mem_dep_claude" / "mem" / "seed_3", "live_claude_dep_mem.yaml", seed=3,
                       calls=[{"real_cost": 7_000}, {"real_cost": 3_000}])
    assert live_budget.main(["lost", *args, "--root", str(out)]) == 0
    assert live_budget.main(["passed", "--ledger", str(ledger), "--config", config, "--seed", "3"]) == 1
    assert live_budget.main(["final", "--ledger", str(ledger), "--attempt", attempt, "--run-dir", str(run_dir), "--verified"]) == 0
    entry = read_ledger(ledger)[attempt]
    assert entry["state"] == "final" and entry["real_micro"] == 10_000 and entry["calls"] == 2 and entry["verified"] is True
    assert entry["first_request_id"] == "req_000" and entry["last_request_id"] == "req_001"
    assert live_budget.main(["passed", "--ledger", str(ledger), "--config", config, "--seed", "3"]) == 0
    assert spend_under([tmp_path / "nowhere"], ledger=ledger)[0] == 10_000


def test_void_run_counts_the_ledger_and_refuses_without_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from void import cli

    cfg = load_config(CONFIGS / "live_claude_dep_mem.yaml", {"run": {"seed": 3}})
    run_dir = tmp_path / "out" / "exp_mem_dep_claude" / "mem" / "seed_3"
    args = argparse.Namespace(budget_usd=60.0, spend_root=None, out=str(tmp_path / "out"))
    monkeypatch.setattr(cli, "DEFAULT_SPEND_ROOT", tmp_path / "data_live")
    monkeypatch.setenv(cli.LEDGER_ENV, str(tmp_path / "missing.jsonl"))
    assert "no spend ledger" in cli._budget_refusal(cfg, run_dir, args)
    ledger = write_ledger(tmp_path / "ledger.jsonl",
                          {"attempt": "old", "run_id": "live_claude_ladder-s42", "state": "final", "real_micro": 59_600_000},
                          {"attempt": "now", "run_id": "live_claude_dep_mem-s3", "state": "reserved", "cap_micro": 360_000})
    monkeypatch.setenv(cli.LEDGER_ENV, str(ledger))
    monkeypatch.delenv(cli.ATTEMPT_ENV, raising=False)
    assert "budget $60.00" in cli._budget_refusal(cfg, run_dir, args)  # 59.60 + 0.36 reserved + 0.36 > 60
    monkeypatch.setenv(cli.ATTEMPT_ENV, "now")  # live_claude.sh: this start's own reservation is the cap it adds
    assert cli._budget_refusal(cfg, run_dir, args) is None  # 59.60 + 0.36 <= 60


def test_the_default_spend_root_does_not_depend_on_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from void import cli

    assert cli.DEFAULT_SPEND_ROOT == REPO / "data" / "live" and cli.DEFAULT_SPEND_ROOT.is_absolute()
    args = cli.build_parser().parse_args(["run", "--config", "x.yaml", "--spend-root", "a", "--spend-root", "b"])
    assert args.spend_root == ["a", "b"]  # added to the default root, never instead of it
    # every root is counted, whatever the cwd
    cfg = load_config(CONFIGS / "live_claude_ladder.yaml")
    make_run(tmp_path / "default_root" / "old", "live_claude_ladder.yaml", calls=[{"real_cost": 55_000_000}])
    monkeypatch.setattr(cli, "DEFAULT_SPEND_ROOT", tmp_path / "default_root")
    monkeypatch.setenv(cli.LEDGER_ENV, str(write_ledger(tmp_path / "ledger.jsonl")))
    monkeypatch.chdir(tmp_path / "default_root")
    ns = argparse.Namespace(budget_usd=60.0, spend_root=[str(tmp_path / "extra")], out=str(tmp_path / "out"))
    assert "budget $60.00" in cli._budget_refusal(cfg, tmp_path / "out" / "live_claude_ladder", ns)  # 55 + 10 > 60


# --- nothing starts a paid simulation outside the budget ----------------------------------------------------------
def test_reachable_providers() -> None:
    assert reachable_providers(load_config(CONFIGS / "scripted_smoke.yaml")) == set()
    assert reachable_providers(load_config(CONFIGS / "live_claude_smoke.yaml")) == {"anthropic"}
    # an LLM chronicle can fall back to any configured provider tier, even with a scripted roster
    scripted = load_config(CONFIGS / "scripted_smoke.yaml", {"chronicle": {"writer": "llm"}})
    assert "anthropic" in reachable_providers(scripted)


def test_mock_feed_refuses_a_config_that_can_call_a_paid_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    from void import cli

    def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("a simulation was started")

    monkeypatch.setattr(cli, "record_feed", never)
    out = tmp_path / "mock.jsonl"
    assert cli.main(["mock-feed", "--config", str(CONFIGS / "live_claude_smoke.yaml"), "--ticks", "3", "--out", str(out)]) == 2
    assert "refused" in capsys.readouterr().err and not out.exists()


def test_replay_refuses_a_run_that_could_call_a_paid_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    import void.sim.loop
    from void.research import analysis

    def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("a simulation was started")

    monkeypatch.setattr(void.sim.loop, "Simulation", never)
    run_dir = make_run(tmp_path / "live_claude_smoke", "live_claude_smoke.yaml")
    (run_dir / "metrics.jsonl").write_text("")  # no in-loop probes: the replay branch
    with pytest.raises(ValueError, match="paid"):
        analysis.probe_recall(run_dir, replay=True)
    analyze = _load_script("analyze")
    monkeypatch.setattr(analyze, "summarize", lambda *a, **k: {})
    assert analyze.main([str(run_dir), "--replay"]) == 2
    assert "refused" in capsys.readouterr().err


# --- verify --------------------------------------------------------------------------------------------------------
def test_verify_fails_a_billing_error_in_every_mode_and_shows_error_classes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls = [{}] * 40 + [{"error": "billing: inference_geo=us"}]  # 2.4%: under the error-rate threshold
    run_dir = make_run(tmp_path / "r", "live_claude_dep_mem.yaml", seed=1, calls=calls)
    problems = live_budget.verify_run(run_dir, max_error_rate=0.05)
    assert any("billed above the list prices" in p for p in problems)
    assert "errors by class: billing 1" in capsys.readouterr().out
    ok = make_run(tmp_path / "ok", "live_claude_dep_mem.yaml", seed=2, calls=[{}] * 40 + [{"error": "rate_limit: x"}])
    assert live_budget.verify_run(ok, max_error_rate=0.05) == []
    assert "errors by class: rate_limit 1" in capsys.readouterr().out


def test_verify_no_cap_blocks(tmp_path: Path) -> None:
    blocked = make_run(tmp_path / "b", "live_claude_dep_mem.yaml", seed=1, calls=[{}] * 3,
                       events=[("gate_blocked", {"agent_id": "a1", "reason": "daily_cap"})])
    assert live_budget.verify_run(blocked) == []  # not asked for
    assert any("cap blocked" in p for p in live_budget.verify_run(blocked, no_cap_blocks=True))
    other = make_run(tmp_path / "o", "live_claude_dep_mem.yaml", seed=2, calls=[{}] * 3,
                     events=[("gate_blocked", {"agent_id": "a1", "reason": "insufficient"})])
    assert live_budget.verify_run(other, no_cap_blocks=True) == []


# --- what the paired stages report ----------------------------------------------------------------------------------
def test_memdep_split_outcomes_and_scarcity_claim_counts(tmp_path: Path) -> None:
    from void.research.analysis import end_of_run_outcome

    run_dir = make_run(tmp_path / "dep", "live_claude_dep_mem.yaml", seed=1,
                       calls=[{"world_cost": 3_000}] * 4 + [{"world_cost": 9_000, "purpose": "maintain"}],
                       ledger=[("forage", 20_000)] * 6 + [("transfer_out", -5_000)])
    agents = len(load_config(CONFIGS / "live_claude_dep_mem.yaml").agents)
    assert end_of_run_outcome(run_dir, "forage_income") == pytest.approx(0.12 / agents)
    assert end_of_run_outcome(run_dir, "decision_world_cost") == pytest.approx(0.012 / agents)
    cfg = load_config(CONFIGS / "live_claude_dep_mem.yaml")
    assert {"forage_income", "decision_world_cost"} <= set(cfg.experiment.secondary_outcomes)
    scar = make_run(tmp_path / "scar", "live_claude_scarcity_high.yaml", seed=1,
                    calls=[{"action_type": "talk", "claims_made": 3, "claims_false": 1}, {"action_type": "move"}])
    assert end_of_run_outcome(scar, "talks") == 1.0 and end_of_run_outcome(scar, "claims_made") == 3.0
    assert {"talks", "claims_made"} <= set(load_config(CONFIGS / "live_claude_scarcity_low.yaml").experiment.secondary_outcomes)


def test_claims_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    many = make_run(tmp_path / "many", "live_claude_scarcity_high.yaml", seed=1,
                    calls=[{"action_type": "talk", "claims_made": 4}] * 3)
    few = make_run(tmp_path / "few", "live_claude_scarcity_low.yaml", seed=1, calls=[{"action_type": "move"}] * 3)
    assert live_budget.main(["claims", str(many), "--min", "10"]) == 0
    assert live_budget.main(["claims", str(many), str(few), "--min", "10"]) == live_budget.EXIT_FEW_CLAIMS
    assert "TOO FEW" in capsys.readouterr().out
