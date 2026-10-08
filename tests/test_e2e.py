"""End-to-end invariants of a scripted run (DESIGN §19 step 2, §9, §13, §14, §15).

One module-scoped 3-day run of ``configs/scripted_smoke.yaml`` carries most assertions. A second,
deliberately crowded run (smaller world, longer talk radius, a standing storm, a generous benefactor
and an operator-posted task) raises the utility of the rarer actions so action coverage is checked
on the union of both.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from conftest import NO_PROVIDER_TIERS, SimRun, deep_merge

from void.brain.decision import ACTION_TYPES
from void.config import usd_to_micro
from void.memory.notes import CHANNELS, parse_note, render_note, split_frontmatter

DAYS = 3
# Actions the scripted policy may legitimately never pick in three quiet days (task spec).
OPTIONAL = {"create_offspring", "apply_task", "read_chronicle"}
REJECTION_STAGES = {"static", "tests", "spec", "size"}

COVERAGE = deep_merge(NO_PROVIDER_TIERS, {
    "run": {"days": DAYS},
    "world": {"size": 30.0, "talk_radius": 8.0},
    "population": {"cap": 16},
    "agents": [{"name": n, "tier": "scripted"} for n in ("Ada", "Bao", "Cyra", "Dev", "Enzo", "Faye", "Gil", "Hana", "Ivo")],
    "tiers": {"scripted": {"gadget_defect_rate": 0.6, "entropy_budget_max": 40.0}},
    "epochs": [{"day": 1, "kind": "storm", "weather_baseline": -0.6, "duration_days": DAYS}],
    "benefactor": {"enabled": True, "mean_interval_ticks": 4, "amount_usd": [0.5, 1.0]},
})


@pytest.fixture(scope="module")
def smoke(run_sim):
    """The reference run: scripted_smoke.yaml for 3 days, recording the headline at each day start."""
    headline_at_day_start: dict[int, str | None] = {}

    def hook(sim, report):
        if report.new_day:
            headline_at_day_start[report.day] = sim.db.kv_get("chronicle_headline")

    run = run_sim("e2e_smoke", {"run": {"days": DAYS}}, on_tick=hook)
    run.headline_at_day_start = headline_at_day_start  # type: ignore[attr-defined]
    return run


@pytest.fixture(scope="module")
def crowded(run_sim):
    """A crowded run with an operator-driven task lifecycle (post, approve, complete) via the command queue."""
    ops: dict[str, int | str] = {}

    def hook(sim, report):
        if report.tick == 2:
            ops["post_tick"] = report.tick
            sim.enqueue("post_task", {"title": "Sweep the void", "description": "Tidy the ring of nodes.", "reward_usd": 0.30})
        elif "approve_tick" not in ops and report.tick >= 6:
            app = sim.db.fetchone("SELECT application_id, task_id FROM task_applications WHERE status='pending' "
                                  "ORDER BY tick, application_id LIMIT 1")
            if app is not None:
                ops["approve_tick"], ops["task_id"] = report.tick, str(app["task_id"])
                sim.enqueue("approve_task", {"task_id": app["task_id"], "application_id": app["application_id"]})
        elif "approve_tick" in ops and "complete_tick" not in ops and report.tick == int(ops["approve_tick"]) + 3:
            ops["complete_tick"] = report.tick
            sim.enqueue("complete_task", {"task_id": ops["task_id"]})

    run = run_sim("e2e_crowded", COVERAGE, on_tick=hook)
    run.ops = ops  # type: ignore[attr-defined]
    return run


def _ledger_sum(run: SimRun) -> int:
    return int(run.db.fetchone("SELECT COALESCE(SUM(delta),0) AS s FROM wallet_ledger")["s"])


def _money_conserved(run: SimRun) -> None:
    m = run.sim.wallet.money_snapshot()
    initial_pool = usd_to_micro(run.cfg.population.spawn_pool_usd)
    initial_chronicle = usd_to_micro(run.cfg.economy.chronicle_budget_usd)
    initial_house = usd_to_micro(run.cfg.economy.house_budget_usd)
    initial_research = usd_to_micro(run.cfg.economy.research_pool_usd)
    rhs = (m["agents"] + (m["spawn_pool"] - initial_pool) + (m["house"] - initial_house) + (m["chronicle"] - initial_chronicle)
           + (m["research_pool"] - initial_research) - m["house_debt"])
    assert _ledger_sum(run) == rhs, f"ledger {_ledger_sum(run)} != agents+pool+house+chronicle+research deltas {rhs}"


# --- the run itself ---------------------------------------------------------------------------------------------------


def test_run_completes_all_days(smoke: SimRun):
    assert smoke.status == "completed"
    assert smoke.sim.clock.tick == DAYS * smoke.cfg.run.ticks_per_day and smoke.sim.clock.day == DAYS
    assert smoke.db.fetchone("SELECT status, ended_tick FROM run")["status"] == "completed"
    assert smoke.events("run_ended")[-1]["payload"]["reason"] == "completed"


def test_no_negative_balance_and_db_check_holds(smoke: SimRun):
    rows = smoke.db.fetchall("SELECT agent_id, balance FROM agents")
    assert rows and all(int(r["balance"]) >= 0 for r in rows)
    assert int(smoke.db.fetchone("SELECT MIN(balance_after) AS m FROM wallet_ledger")["m"]) >= 0
    with pytest.raises(sqlite3.IntegrityError):
        smoke.db.execute("UPDATE agents SET balance=-1 WHERE agent_id=?", (rows[0]["agent_id"],))
    assert int(smoke.db.fetchone("SELECT balance FROM agents WHERE agent_id=?", (rows[0]["agent_id"],))["balance"]) >= 0


def test_money_is_conserved_across_the_ledger(smoke: SimRun, crowded: SimRun):
    _money_conserved(smoke)
    _money_conserved(crowded)  # windfalls, endowments, task rewards and fees all have ledger rows


def test_spend_total_equals_sum_of_real_cost(smoke: SimRun, crowded: SimRun):
    for run in (smoke, crowded):
        real = int(run.db.fetchone("SELECT COALESCE(SUM(real_cost),0) AS s FROM llm_calls")["s"])
        assert run.sim.wallet.spend_total == real > 0
        assert run.tick_rows()[-1]["spend_total_usd"] == pytest.approx(real / 1e6, abs=1e-6)


def test_population_never_exceeds_cap(smoke: SimRun, crowded: SimRun):
    for run in (smoke, crowded):
        cap = run.cfg.population.cap
        rows = run.tick_rows()
        assert rows and all(0 < r["population"] <= cap for r in rows), [r["population"] for r in rows]
        assert all(int(s["population"]) <= cap for s in _snapshots(run))


def _snapshots(run: SimRun) -> list[dict]:
    return [json.loads(r["json"]) for r in run.db.fetchall("SELECT json FROM snapshots ORDER BY tick")]


def test_every_action_type_is_exercised(smoke: SimRun, crowded: SimRun):
    seen = set(smoke.action_types()) | set(crowded.action_types())
    required = set(ACTION_TYPES) - OPTIONAL
    missing = sorted(required - seen)
    assert not missing, f"action types never chosen in either run: {missing} (seen: {sorted(seen)})"
    # the kernel emitted the matching public event for each of them at least once
    kinds = set(smoke.event_kinds()) | set(crowded.event_kinds())
    assert {"move", "forage", "talk", "share_note", "transfer", "nudge_weather", "use_gadget", "sleep", "idle"} <= kinds
    assert {"gadget_verified", "gadget_rejected"} & kinds


def test_operator_task_lifecycle_through_the_command_queue(crowded: SimRun):
    ops = crowded.ops  # type: ignore[attr-defined]
    cmds = crowded.events("operator_command")
    assert cmds and all(c["visibility"] == "operator" for c in cmds)
    posted = [c for c in cmds if c["payload"]["kind"] == "post_task"]
    assert posted[0]["tick"] == int(ops["post_tick"]) + 1 and posted[0]["payload"]["ok"] is True  # applies at the next tick
    assert "apply_task" in crowded.action_types() and crowded.events("apply_task")
    assert "approve_tick" in ops and "complete_tick" in ops, "no agent applied to the posted task in time"
    completed = crowded.events("task_completed")
    assert completed and completed[0]["payload"]["task_id"] == ops["task_id"] and completed[0]["tick"] == int(ops["complete_tick"]) + 1
    task = crowded.db.fetchone("SELECT status, assigned_agent_id FROM tasks WHERE task_id=?", (ops["task_id"],))
    assert task["status"] == "completed" and task["assigned_agent_id"] == completed[0]["payload"]["agent_id"]
    assert crowded.sim.wallet.ledger_sum(kind="task_reward") == usd_to_micro(0.30)


def test_chronicle_days_exist_and_headline_reaches_next_day(smoke: SimRun):
    chron = smoke.run_dir / "chronicle"
    assert (chron / "day_001.md").is_file() and (chron / "day_002.md").is_file()
    day1 = (chron / "day_001.md").read_text(encoding="utf-8")
    assert day1.startswith("# The Void Chronicle, Day 1")
    heads = smoke.headline_at_day_start  # type: ignore[attr-defined]
    assert heads[1] is None  # nothing to read on the first morning
    assert heads[2] and heads[2].startswith("Day 1:") and heads[3] and heads[3].startswith("Day 2:")
    assert f"**{heads[2]}**" in day1
    assert smoke.kv("chronicle_headline") and int(smoke.kv("chronicle_day")) == DAYS
    written = smoke.events("chronicle")
    assert [ev["payload"]["day"] for ev in written] == list(range(1, DAYS + 1))
    for ev in written:  # each day's paper is written from the next morning's tick (the last one at run end)
        assert ev["payload"]["headline"].startswith(f"Day {ev['payload']['day']}:") and ev["payload"]["rev"] == ev["payload"]["day"]
        assert ev["tick"] == min(ev["payload"]["day"] * smoke.cfg.run.ticks_per_day + 1, DAYS * smoke.cfg.run.ticks_per_day)


def test_gadgets_verified_and_rejected_at_a_gate_stage(smoke: SimRun, crowded: SimRun):
    if not (getattr(smoke.sim.sandbox, "isolated", False) and getattr(crowded.sim.sandbox, "isolated", False)):
        pytest.skip("namespace sandbox unavailable: gadget proposals fail closed")
    gadgets = smoke.sim.gadgets.all() + crowded.sim.gadgets.all()
    verified = [g for g in gadgets if g.status == "verified"]
    rejected = [g for g in gadgets if g.status == "rejected"]
    assert verified and all(g.verification.get("stage") == "verified" and g.x is not None for g in verified)
    assert rejected, "no proposal was rejected in either run"
    assert {g.verification.get("stage") for g in rejected} <= REJECTION_STAGES, [g.verification for g in rejected]
    for run in (smoke, crowded):
        for ev in run.events("gadget_rejected"):
            assert ev["payload"]["stage"] in REJECTION_STAGES
        for ev in run.events("gadget_verified"):
            assert ev["payload"]["stage"] == "verified"


def test_every_vault_file_parses_and_round_trips(smoke: SimRun):
    vaults = smoke.run_dir / "vaults"
    note_files = sorted(vaults.glob("*/notes/*.md"))
    self_files = sorted(vaults.glob("*/self.md"))
    assert note_files and len(self_files) == len(smoke.sim.registry.alive())
    indexed = {r["path"]: r for r in smoke.db.fetchall("SELECT note_id, path, channel FROM notes WHERE archived=0")}
    for path in note_files:
        text = path.read_text(encoding="utf-8")
        note = parse_note(text, agent_id=path.parts[-3], path=str(path))
        assert note.note_id.startswith("note_") and note.title and note.provenance.channel in CHANNELS
        assert render_note(note) == text, f"{path} does not round-trip byte for byte"
        row = indexed.get(str(path))
        assert row is not None and row["note_id"] == note.note_id and row["channel"] == note.provenance.channel
    for path in self_files:
        data, rest = split_frontmatter(path.read_text(encoding="utf-8"))
        assert int(data["version"]) >= 1 and rest.strip()


def test_metrics_jsonl_has_one_row_per_tick_and_per_day(smoke: SimRun):
    rows = smoke.metrics_rows()
    ticks = [r["tick"] for r in rows if r.get("kind") != "day"]
    days = [r["day"] for r in rows if r.get("kind") == "day"]
    assert ticks == list(range(1, DAYS * smoke.cfg.run.ticks_per_day + 1))
    assert days == list(range(1, DAYS + 1))
    for r in rows:
        assert r["run_id"] == smoke.sim.run_id and r["seed"] == smoke.cfg.run.seed and r["config_hash"] == smoke.cfg.hash()
    assert smoke.sim.last_metrics_row["tick"] == ticks[-1]
    stored = smoke.db.fetchall("SELECT tick FROM metrics ORDER BY tick")
    assert [int(r["tick"]) for r in stored] == [-d for d in reversed(days)] + ticks


def test_last_tick_snapshot_has_protocol_keys(smoke: SimRun):
    last = DAYS * smoke.cfg.run.ticks_per_day
    row = smoke.db.fetchone("SELECT json FROM snapshots WHERE tick=?", (last,))
    assert row is not None
    snap = json.loads(row["json"])
    assert {"agents", "nodes", "weather", "epochs", "gadgets_rev"} <= set(snap)
    assert snap["type"] == "snapshot" and snap["tick"] == last and snap["day"] == DAYS
    assert {"active", "scheduled"} <= set(snap["epochs"]) and snap["gadgets_rev"] >= 1
    assert snap == smoke.sim.last_snapshot
    for a in snap["agents"]:
        assert {"id", "x", "y", "stress", "asleep", "status", "balance_usd", "anim", "last_action", "effects"} <= set(a)
    for n in snap["nodes"]:
        assert {"id", "x", "y", "stock", "capacity", "stock_delta"} <= set(n)
