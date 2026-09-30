"""The unexplained benefactor (DESIGN §9.5): seeded windfalls, opaque vs legible disclosure, no leaks.

Two one-day runs share every seed, so the grant schedule is the same in both arms (common random
numbers); only the disclosure differs. The chronicle is built from public events only, so the word
"benefactor" must never reach it.
"""

from __future__ import annotations

import json

import pytest
from conftest import NO_PROVIDER_TIERS, SimRun, deep_merge

from void.config import usd_to_micro
from void.economy.benefactor import LEGIBLE_STRING, OPAQUE_STRING


def _config(disclosure: str) -> dict:
    return deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 1},
                                          "benefactor": {"enabled": True, "mean_interval_ticks": 3, "disclosure": disclosure}})


@pytest.fixture(scope="module")
def opaque(run_sim) -> SimRun:
    return run_sim("benefactor_opaque", _config("opaque"))


@pytest.fixture(scope="module")
def legible(run_sim) -> SimRun:
    return run_sim("benefactor_legible", _config("legible"))


def _grants(run: SimRun) -> list[dict]:
    return run.events("benefactor_grant")


def _chronicle_text(run: SimRun) -> str:
    files = sorted((run.run_dir / "chronicle").glob("day_*.md"))
    assert files
    return "\n".join(p.read_text(encoding="utf-8") for p in files)


def test_opaque_windfalls_have_ledger_rows_and_no_source(opaque: SimRun):
    assert opaque.status == "completed"
    rows = opaque.db.fetchall("SELECT agent_id, tick, delta, kind, ref, payload FROM wallet_ledger WHERE kind='windfall' ORDER BY entry_id")
    assert len(rows) >= 1 and all(int(r["delta"]) > 0 and r["ref"] == "benefactor" for r in rows)
    lo, hi = opaque.cfg.benefactor.amount_usd
    assert all(usd_to_micro(lo) <= int(r["delta"]) <= usd_to_micro(hi) for r in rows)
    public = opaque.events("windfall")
    assert len(public) == len(rows) == len(_grants(opaque))
    for ev, row in zip(public, rows, strict=True):
        assert ev["visibility"] == "public" and ev["tick"] == int(row["tick"]) and ev["agent_id"] == row["agent_id"]
        assert set(ev["payload"]) == {"agent_id", "amount_usd"}  # no source, no reason, no disclosure
        assert ev["payload"]["amount_usd"] == int(row["delta"]) / 1e6
        assert "benefactor" not in json.dumps(ev["payload"]).lower()
    assert opaque.db.fetchone("SELECT windfall_string FROM run")["windfall_string"] == "opaque"
    assert "No source is recorded" in OPAQUE_STRING


def test_benefactor_grant_events_are_operator_only(opaque: SimRun, legible: SimRun):
    for run in (opaque, legible):
        grants = _grants(run)
        assert grants and all(g["visibility"] == "operator" for g in grants)
        for g in grants:
            assert g["payload"]["disclosure"] == run.cfg.benefactor.disclosure and g["payload"]["manual"] is False
            assert g["payload"]["targeting"] == "random" and g["payload"]["amount_usd"] > 0
        assert not run.events("benefactor_grant", visibility="public")


def test_recipient_note_in_the_same_or_next_tick_is_tagged_windfall_seeded(opaque: SimRun):
    seeded = 0
    for g in _grants(opaque):
        aid, tick = g["agent_id"], g["tick"]
        notes = opaque.db.fetchall("SELECT tags, title, channel FROM notes WHERE agent_id=? AND created_tick IN (?, ?) "
                                   "AND NOT (tags LIKE '%\"entity\"%') ORDER BY created_tick, note_id", (aid, tick, tick + 1))
        for n in notes:
            assert "windfall_seeded" in json.loads(n["tags"]), (g, dict(n))
            seeded += 1
    assert seeded >= 1, "no recipient wrote a note within a tick of a grant"
    # the tag is kernel-side: it appears in the vault file too, and nowhere on unrelated notes
    tagged = opaque.db.fetchall("SELECT agent_id, created_tick, path FROM notes WHERE tags LIKE '%windfall_seeded%'")
    grant_ticks = {(g["agent_id"], g["tick"]) for g in _grants(opaque)}
    for n in tagged:
        assert (n["agent_id"], int(n["created_tick"])) in grant_ticks or (n["agent_id"], int(n["created_tick"]) - 1) in grant_ticks
        assert "windfall_seeded" in open(n["path"], encoding="utf-8").read()


def test_opaque_chronicle_never_mentions_the_benefactor(opaque: SimRun):
    text = _chronicle_text(opaque)
    assert "benefactor" not in text.lower()
    assert "windfall" in text.lower()  # the public fact (money appeared) is reported, its source is not


def test_legible_arm_uses_task_reward_and_task_completed(legible: SimRun):
    assert legible.status == "completed"
    rows = legible.db.fetchall("SELECT kind, delta FROM wallet_ledger WHERE ref='benefactor' ORDER BY entry_id")
    assert rows and {r["kind"] for r in rows} == {"task_reward"}
    assert not legible.db.fetchall("SELECT 1 FROM wallet_ledger WHERE kind='windfall'") and not legible.events("windfall")
    public = legible.events("task_completed")
    assert len(public) == len(rows) == len(_grants(legible))
    for ev in public:
        assert ev["visibility"] == "public" and set(ev["payload"]) == {"task_id", "agent_id", "reward_usd"}
    assert legible.db.fetchone("SELECT windfall_string FROM run")["windfall_string"] == "legible"
    assert LEGIBLE_STRING.startswith("Reward for services rendered")


@pytest.mark.xfail(reason="bug: legible benefactor grants use task_id 'benefactor', which the chronicle prints as the task title")
def test_legible_chronicle_never_mentions_the_benefactor(legible: SimRun):
    assert "benefactor" not in _chronicle_text(legible).lower()


def test_grant_schedule_is_identical_across_disclosure_arms(opaque: SimRun, legible: SimRun):
    def schedule(run: SimRun) -> list[tuple[int, str, float]]:
        return [(g["tick"], g["agent_id"], g["payload"]["amount_usd"]) for g in _grants(run)]

    assert schedule(opaque) == schedule(legible) and len(schedule(opaque)) >= 2
    ticks = [t for t, _, _ in schedule(opaque)]
    assert ticks == sorted(ticks) and len(set(ticks)) == len(ticks)
    assert opaque.cfg.hash() != legible.cfg.hash() and opaque.cfg.run.seed == legible.cfg.run.seed
