"""Reservations keep ``spend_today`` under the daily cap on every tick (DESIGN §9.1, §9.3, §19 step 4).

Six agents compete for a daily cap of 1.5 holds. Under ``fcfs`` at most one call fits per tick and
the losers are put to sleep for the day; under ``sync_sleep`` the whole population sleeps as soon as
one more round of calls would not fit.
"""

from __future__ import annotations

import pytest
from conftest import NO_PROVIDER_TIERS, SimRun, deep_merge

from void.config import micro_to_usd

TIER = "scripted"


@pytest.fixture(scope="module")
def hold(make_sim) -> int:
    """One worst-case hold for a 1200-token prompt, computed the way the loop computes it."""
    probe = make_sim("gate_probe")
    return probe.wallet.hold_for(probe.cfg.tiers[TIER], 1200, probe.system_tokens[TIER])


def _config(hold: int, policy: str) -> dict:
    return deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 1},
                                          "economy": {"daily_cap_usd": micro_to_usd(hold) * 1.5, "daily_cap_policy": policy}})


@pytest.fixture(scope="module")
def fcfs(run_sim, hold) -> SimRun:
    return run_sim("gate_fcfs", _config(hold, "fcfs"))


@pytest.fixture(scope="module")
def sync(run_sim, hold) -> SimRun:
    return run_sim("gate_sync", _config(hold, "sync_sleep"))


def _assert_cap_never_exceeded(run: SimRun, hold: int) -> None:
    cap = run.cfg.economy.daily_cap_usd
    rows = run.tick_rows()
    assert len(rows) == run.cfg.run.ticks_per_day and len(run.sim.registry.alive()) == 6
    assert all(r["spend_today_usd"] <= cap + 1e-9 for r in rows), [r["spend_today_usd"] for r in rows]
    assert all(r["calls"] <= int(cap * 1e6 // hold) for r in rows)  # never more than floor(cap / hold) calls per tick
    # every call was gated with its hold reserved: no call was ever admitted past the cap
    for c in run.db.fetchall("SELECT tick, hold, real_cost FROM llm_calls ORDER BY tick"):
        assert int(c["hold"]) >= int(c["real_cost"]) and int(c["hold"]) <= cap * 1e6


def test_fcfs_spend_today_never_exceeds_cap(fcfs: SimRun, hold: int):
    assert fcfs.status == "completed"
    _assert_cap_never_exceeded(fcfs, hold)
    assert fcfs.sim.wallet.spend_total > 0  # somebody did get to call


def test_fcfs_daily_cap_blocks_are_followed_by_forced_sleep(fcfs: SimRun):
    events = fcfs.events()
    by_seq = {e["seq"]: e for e in events}
    blocked = [e for e in events if e["kind"] == "gate_blocked" and e["payload"]["reason"] == "daily_cap"]
    assert blocked, "no agent was ever blocked by the daily cap"
    for e in blocked:
        nxt = by_seq[e["seq"] + 1]
        assert nxt["kind"] == "sleep" and nxt["agent_id"] == e["agent_id"] and nxt["tick"] == e["tick"]
        assert nxt["payload"] == {"agent_id": e["agent_id"], "forced": "daily_cap"}
        # a blocked agent stays asleep: no call for it later that day
        later = fcfs.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE agent_id=? AND tick>=?", (e["agent_id"], e["tick"]))
        assert int(later["n"]) == 0
    # the very first tick: one call admitted, five blocked (cap = 1.5 holds)
    first = [e for e in blocked if e["tick"] == 1]
    assert len(first) == 5 and fcfs.tick_rows()[0]["calls"] == 1 and fcfs.tick_rows()[0]["gate_blocked"] == 5


def test_sync_sleep_puts_everyone_to_sleep_in_the_same_tick(sync: SimRun, hold: int):
    assert sync.status == "completed"
    _assert_cap_never_exceeded(sync, hold)
    forced = [e for e in sync.events("sleep") if e["payload"].get("forced") == "daily_cap"]
    assert forced, "sync_sleep never forced a sleep"
    by_tick: dict[int, set[str]] = {}
    for e in forced:
        by_tick.setdefault(e["tick"], set()).add(e["agent_id"])
    alive = {a.agent_id for a in sync.sim.registry.alive()}
    assert any(agents == alive for agents in by_tick.values()), by_tick
    assert not sync.events("gate_blocked")  # nobody is gated individually under sync_sleep
