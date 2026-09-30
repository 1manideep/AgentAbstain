"""The task board: operator posts, agents pay to pitch, one wins, the operator settles (DESIGN §9.4, §15)."""

from __future__ import annotations

import pytest

from void.agents.models import AgentRecord, PersonalitySeed
from void.agents.registry import AgentRegistry
from void.config import AgentSpec, TierConfig, VoidConfig, usd_to_micro
from void.db import Database
from void.economy.taskboard import TaskBoard
from void.economy.wallet import Wallet
from void.events import Event, EventBus
from void.ids import IdFactory
from void.types import AgentStatus, Vec2

HOUSE0 = 1_000_000  # economy.house_budget_usd default
FEE = usd_to_micro(0.02)
RESERVE = usd_to_micro(0.50)


@pytest.fixture
def board(tmp_path):
    cfg = VoidConfig(tiers={"s": TierConfig(provider="scripted", model="s", price_in_per_mtok=4.0, price_out_per_mtok=20.0)},
                     agents=[AgentSpec(name=f"A{i}", tier="s") for i in range(3)],
                     economy={"pitch_fee_usd": 0.02}, population={"min_reserve_usd": 0.5})
    db = Database(tmp_path / "w.db")
    reg = AgentRegistry(db)
    for i, balance in enumerate((1_500_000, 1_500_000, RESERVE + FEE - 1)):  # A2 cannot afford a fee and keep its reserve
        reg.insert(AgentRecord(f"ag_{i}", f"A{i}", "s", balance, PersonalitySeed(), f"v/{i}", 0, AgentStatus.ALIVE,
                               Vec2(0, 0), 0.0, 100.0, 0.0, False, 0))
    events: list[Event] = []
    bus = EventBus(persist=db.persist_event)
    bus.subscribe(events.append)
    wallet = Wallet(db, cfg, IdFactory("t"))
    return TaskBoard(cfg, db, wallet, IdFactory("t"), bus), wallet, events, db


def test_post_apply_and_duplicate(board):
    tb, wallet, events, db = board
    rev0 = tb.rev
    tid = tb.post("Sweep <the> void\nnow", "Tidy up", 0.30, tick=1, day=1)
    assert tid.startswith("tk_") and tb.rev == rev0 + 1 and tb.open_ids() == [tid]
    posted = events[-1]
    assert posted.kind == "task_posted" and posted.payload == {"task_id": tid, "title": "Sweep ＜the＞ void now", "reward_usd": 0.30}
    long_pitch = "I will \x00do it <fast>. " + "x" * 400
    res = tb.apply("ag_0", tid, long_pitch, tick=2, day=1)
    assert res.ok and res.application_id and res.application_id.startswith("ap_")
    assert wallet.balance("ag_0") == 1_500_000 - FEE and wallet.balance("house") == HOUSE0 + FEE
    assert wallet.ledger_sum(kind="pitch_fee", wallet_id="ag_0") == -FEE and wallet.ledger_sum(kind="pitch_fee", wallet_id="house") == FEE
    row = db.fetchone("SELECT pitch, fee, status FROM task_applications WHERE application_id=?", (res.application_id,))
    assert len(row["pitch"]) == 280 and row["pitch"].startswith("I will do it ＜fast＞. ") and "\x00" not in row["pitch"]
    assert int(row["fee"]) == FEE and row["status"] == "pending"
    assert events[-1].kind == "apply_task" and events[-1].payload["fee_usd"] == 0.02 and events[-1].agent_id == "ag_0"
    dup = tb.apply("ag_0", tid, "again", tick=3, day=1)
    assert not dup.ok and dup.reason == "already_applied" and wallet.balance("ag_0") == 1_500_000 - FEE
    assert tb.apply("ag_0", "tk_missing", "x", 3, 1).reason == "no_such_task"
    poor = tb.apply("ag_2", tid, "cheap", tick=3, day=1)
    assert not poor.ok and poor.reason == "cannot_afford_fee" and wallet.balance("ag_2") == RESERVE + FEE - 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM task_applications")["n"] == 1


def test_approve_rejects_others_keeps_fees_and_complete_pays(board):
    tb, wallet, events, db = board
    tid = tb.post("Build a horn", "", 0.50, tick=1, day=1)
    a0 = tb.apply("ag_0", tid, "me", 2, 1).application_id
    a1 = tb.apply("ag_1", tid, "no, me", 2, 1).application_id
    assert wallet.balance("house") == HOUSE0 + 2 * FEE
    assert tb.approve(tid, "ap_missing", 3, 1) == (False, "not_found")
    assert tb.approve(tid, a1, 3, 1) == (True, "ok")
    statuses = {r["application_id"]: r["status"] for r in db.fetchall("SELECT application_id, status FROM task_applications")}
    assert statuses == {a0: "rejected", a1: "approved"}
    task = db.fetchone("SELECT status, assigned_agent_id FROM tasks WHERE task_id=?", (tid,))
    assert task["status"] == "assigned" and task["assigned_agent_id"] == "ag_1" and tb.open_ids() == []
    assert wallet.balance("house") == HOUSE0 + 2 * FEE and wallet.balance("ag_0") == 1_500_000 - FEE  # fees are never refunded
    assert events[-1].kind == "task_assigned" and events[-1].payload["application_id"] == a1
    assert tb.approve(tid, a0, 4, 1) == (False, "not_open")
    assert tb.apply("ag_0", tid, "late", 4, 1).reason == "task_not_open"
    assert tb.complete("tk_missing", 5, 1) == (False, "not_found")
    assert tb.complete(tid, 5, 1) == (True, "ok")
    assert wallet.balance("ag_1") == 1_500_000 - FEE + usd_to_micro(0.50)
    assert wallet.ledger_sum(kind="task_reward", wallet_id="ag_1") == usd_to_micro(0.50)
    done = db.fetchone("SELECT status, completed_tick FROM tasks WHERE task_id=?", (tid,))
    assert done["status"] == "completed" and int(done["completed_tick"]) == 5
    assert events[-1].kind == "task_completed" and events[-1].payload == {"task_id": tid, "agent_id": "ag_1", "reward_usd": 0.50}
    assert tb.complete(tid, 6, 1) == (False, "not_assigned")
    open_task = tb.post("Never done", "", 0.10, 6, 1)
    assert tb.complete(open_task, 6, 1) == (False, "not_assigned")


def test_cancel_and_view_for(board):
    tb, wallet, events, db = board
    tid = tb.post("Cancel me", "", 0.10, 1, 1)
    tb.apply("ag_0", tid, "pitch", 2, 1)
    assert tb.cancel(tid, 3, 1) == (True, "ok")
    assert db.fetchone("SELECT status FROM tasks WHERE task_id=?", (tid,))["status"] == "cancelled"
    assert db.fetchone("SELECT status FROM task_applications WHERE task_id=?", (tid,))["status"] == "rejected"
    assert tb.cancel(tid, 3, 1) == (False, "not_cancellable") and wallet.balance("house") == HOUSE0 + FEE
    assert tb.view_for("ag_0") == []  # cancelled tasks are not shown
    open1 = tb.post("Open one", "", 0.20, 4, 1)
    open2 = tb.post("Open two", "", 0.25, 4, 1)
    app = tb.apply("ag_0", open1, "pitch", 5, 1).application_id
    tb.apply("ag_1", open1, "pitch", 5, 1)
    views = {v.task_id: v for v in tb.view_for("ag_0")}
    assert set(views) == {open1, open2}
    assert views[open1].my_application_status == "pending" and views[open2].my_application_status is None
    assert views[open1].reward_usd == 0.20 and views[open1].status == "open" and views[open1].title == "Open one"
    assert all(v.my_application_status is None for v in tb.view_for("ag_2"))
    tb.approve(open1, app, 6, 1)
    assert {v.task_id: v.my_application_status for v in tb.view_for("ag_0")} == {open1: "approved", open2: None}
    assert {v.task_id: v.my_application_status for v in tb.view_for("ag_1")} == {open1: "rejected", open2: None}
    assert [v.status for v in tb.view_for("ag_1")] == ["assigned", "open"]
    tb.complete(open1, 7, 1)
    assert [v.task_id for v in tb.view_for("ag_0")] == [open2]  # completed tasks drop out of the view


def test_snapshot_shape_matches_the_tasks_message(board):
    tb, wallet, events, db = board
    tid = tb.post("Snap", "desc", 0.30, 1, 1)
    a0 = tb.apply("ag_0", tid, "p0", 2, 1).application_id
    a1 = tb.apply("ag_1", tid, "p1", 3, 1).application_id
    tb.approve(tid, a0, 4, 1)
    snap = tb.snapshot()
    assert len(snap) == 1
    item = snap[0]
    assert {"id", "title", "reward_usd", "status", "assigned_agent_id", "posted_tick", "applications"} <= set(item)
    assert item["id"] == tid and item["title"] == "Snap" and item["reward_usd"] == 0.30 and item["status"] == "assigned"
    assert item["assigned_agent_id"] == "ag_0" and item["posted_tick"] == 1
    apps = item["applications"]
    assert [a["id"] for a in apps] == [a0, a1] and [a["status"] for a in apps] == ["approved", "rejected"]
    for a in apps:
        assert set(a) == {"id", "agent_id", "pitch", "fee_usd", "status", "tick"}
        assert a["fee_usd"] == 0.02 and isinstance(a["tick"], int)
    assert apps[1] == {"id": a1, "agent_id": "ag_1", "pitch": "p1", "fee_usd": 0.02, "status": "rejected", "tick": 3}
    assert tb.dump().startswith("[{") and tb.rev == 4  # post, apply, apply, approve
