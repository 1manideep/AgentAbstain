"""Press outcomes for exp_press: chronicle reach and fidelity against gossip (void/research/outcomes/press.py).

A hand-built ``world.db`` pins the item matching (best item of the note's origin day, the earliest on a tie, the
run's embedding size), both reaches and both fidelities, with their daily versions, to values worked out by hand.
Scripted runs only: one 2-day run of the ``both`` arm checks the outcomes against the tables, and 2 seeds x 4 arms
of 1-day runs go through ``compare`` with the outcomes the configs declare.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_experiments import CONFIGS, run_arm

from void.config import load_config
from void.db import Database
from void.memory.embed import HashingEmbedder, cosine, pack, unpack
from void.research.analysis import DAILY, computed_outcome_names, connect, end_of_run_outcome, load_metrics
from void.research.compare import compare, render
from void.research.outcomes import load_all
from void.research.outcomes.press import ChronicleMatch, best_item, chronicle_matches, origin_day

DIM = 64   # not the default 256: the matching must use the run's memory.embedding_dim
TPD = 12
OUTCOMES = ("chronicle_reach", "chronicle_fidelity", "gossip_fidelity", "gossip_reach")
SECONDARIES = ["chronicle_fidelity", "gossip_fidelity", "gossip_reach", "gossip_transfers", "notes_by_channel.chronicle"]

HEADLINE_1 = "Day 1: Ada foraged most; two conversations held"
TIED = "Weather ran mild all day over the eastern hills"
NOTE_2 = "Bao foraged most over 8 trips, 60 cents gathered"
ITEM_2_2 = "Bao foraged most: $0.16 over 8 trips ($0.60 gathered in all)."
ITEMS = [  # (day, item_id, text); inserted out of order on purpose
    (1, "d1-10", TIED),
    (1, "d1-1", HEADLINE_1),
    (1, "d1-2", TIED),
    (1, "d1-3", NOTE_2),  # the same text as n2, but n2 read day 2's paper
    (1, "d1-4", "Ada (scripted, generation 0) was born with $1.50."),
    (2, "d2-1", "Day 2: Bao foraged most; a storm broke"),
    (2, "d2-2", ITEM_2_2),
    (2, "d2-3", "Weather ran stormy all day."),
]
# agent: (status, born_tick, died_tick)
AGENTS = {"a1": ("alive", 0, None), "a2": ("alive", 0, None), "a3": ("alive", 0, None), "a4": ("alive", TPD + 5, None),
          "a5": ("alive", 0, None), "d1": ("archived", 0, 2 * TPD + 3)}
# note: (agent, channel, hop, origin, created_tick, archived, text)
NOTES = {
    "n1": ("a1", "chronicle", 0, "chronicle:day_1", TPD + 2, 0, HEADLINE_1),
    "n2": ("a2", "chronicle", 0, "chronicle:day_2", 2 * TPD + 1, 0, NOTE_2),
    "n3": ("a3", "chronicle", 0, "chronicle:day_1", TPD + 4, 0, TIED),
    "n4": ("a1", "chronicle", 0, "chronicle:day_7", 2 * TPD + 2, 0, "A paper nobody kept"),  # no items for day 7
    "n5": ("a3", "chronicle", 0, "chronicle:day_1", TPD + 5, 1, HEADLINE_1),   # deleted by its living owner
    "n6": ("a4", "gossip", 1, "chronicle:day_1", TPD + 6, 0, "Ada foraged most they say"),  # heard the paper
    "n7": ("d1", "chronicle", 0, "chronicle:day_1", TPD + 1, 1, HEADLINE_1),   # archived when d1 died on day 3
    "n8": ("a2", "gossip", 2, "note_x", 4, 0, "Node n3 is rich"),
    "n9": ("a5", "chronicle", 0, "chronicle:day_1", 5, 1, HEADLINE_1),        # deleted: a5 holds nothing
    "n10": ("a5", "observed", 0, None, 6, 0, "I foraged at n2"),
}
# gossip_transfer events: (tick, day, hop, similarity)
TRANSFERS = [(4, 1, 1, 0.9), (TPD + 6, 2, 1, 0.7), (TPD + 7, 2, 2, 0.4)]


@pytest.fixture(autouse=True)
def _registered() -> None:
    load_all()  # copies the registered daily versions into DAILY, as end_of_run_outcome and compare do


def _vec(text: str, dim: int = DIM) -> list[float]:
    """The embedding a note indexed over ``text`` has once read back from its float32 blob."""
    return unpack(pack(HashingEmbedder(dim).embed(text)))


def _sim(note_text: str, item_text: str) -> float:
    return max(0.0, min(1.0, cosine(_vec(note_text), HashingEmbedder(DIM).embed(item_text))))


def _synthetic(run_dir: Path, *, dim: int = DIM) -> Path:
    cfg = load_config(CONFIGS / "base.yaml", {"run": {"days": 3, "ticks_per_day": TPD}, "memory": {"embedding_dim": dim}})
    (run_dir / "config.resolved.yaml").write_text(cfg.to_yaml())
    db = Database(run_dir / "world.db")
    try:
        for aid, (status, born, died) in AGENTS.items():
            db.execute("INSERT INTO agents(agent_id, name, model_tier, personality_seed, memory_path, status, x, y, entropy_budget, "
                       "born_tick, died_tick, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, aid.upper(), "scripted", "{}", "", status, 0, 0, 1, born, died, "t0"))
        for day, item_id, text in ITEMS:
            db.execute("INSERT INTO chronicle_items(day, item_id, kind, event_seq, text) VALUES(?,?,?,?,?)",
                       (day, item_id, "line", None, text))
        for nid, (aid, channel, hop, origin, tick, archived, text) in NOTES.items():
            db.execute("INSERT INTO notes(note_id, agent_id, path, title, created_tick, tags, embedding, channel, hop, "
                       "origin_note_id, archived) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (nid, aid, f"/v/{nid}.md", nid, tick, "[]", pack(HashingEmbedder(DIM).embed(text)), channel, hop,
                        origin, archived))
        for tick, day, hop, sim in TRANSFERS:
            db.execute("INSERT INTO events(tick, day, kind, agent_id, payload) VALUES(?,?,?,?,?)",
                       (tick, day, "gossip_transfer", "a4", json.dumps({"hop": hop, "similarity": sim})))
    finally:
        db.close()
    return run_dir


# --- (1) matching on a hand-built run -----------------------------------------------------------------------------
def test_synthetic_notes_get_the_exact_best_item(tmp_path: Path) -> None:
    run = _synthetic(tmp_path)
    got = {m.note_id: m for m in chronicle_matches(run)}
    assert sorted(got) == ["n1", "n2", "n3", "n4"]  # live chronicle-channel notes only: no deleted, archived or gossip note
    assert got["n1"] == ChronicleMatch("n1", "a1", TPD + 2, 1, "d1-1", pytest.approx(1.0))
    # n2's text is item d1-3 word for word, but n2 read day 2's paper: its best is among day 2's items
    sims_2 = {item_id: _sim(NOTE_2, text) for day, item_id, text in ITEMS if day == 2}
    assert max(sims_2, key=lambda k: sims_2[k]) == "d2-2" and sims_2["d2-2"] < 1.0
    assert got["n2"].day == 2 and got["n2"].item_id == "d2-2" and got["n2"].similarity == pytest.approx(sims_2["d2-2"])
    # d1-2 and d1-10 tie at 1.0: the earlier item of the day wins (by item number, not by text or by insertion)
    assert got["n3"].item_id == "d1-2" and got["n3"].similarity == pytest.approx(1.0)
    assert got["n4"] == ChronicleMatch("n4", "a1", 2 * TPD + 2, 7, None, None)
    # one day at a time: notes created on that day
    assert [m.note_id for m in chronicle_matches(run, day=2)] == ["n1", "n3"]
    assert [m.note_id for m in chronicle_matches(run, day=3)] == ["n2", "n4"] and chronicle_matches(run, day=1) == []


def test_best_item_and_origin_day_helpers() -> None:
    a, b = HashingEmbedder(DIM).embed("alpha beta gamma"), HashingEmbedder(DIM).embed("delta epsilon")
    assert best_item(a, []) is None
    assert best_item(a, [("x", b), ("y", a), ("z", a)]) == ("y", pytest.approx(1.0))
    assert origin_day("chronicle:day_12") == 12
    for bad in (None, "", "chronicle:day_None", "chronicle:day_", "note_1", "tracer"):
        assert origin_day(bad) is None, bad


def test_matching_uses_the_runs_embedding_size(tmp_path: Path) -> None:
    run = _synthetic(tmp_path, dim=32)  # notes indexed at 64 dims, config says 32: refuse rather than mis-measure
    with pytest.raises(ValueError, match="embedding_dim is 32"):
        chronicle_matches(run)


def test_synthetic_outcomes_by_hand(tmp_path: Path) -> None:
    run = _synthetic(tmp_path)
    assert set(OUTCOMES) <= set(computed_outcome_names()) and set(OUTCOMES) <= set(DAILY)
    # living: a1..a5. Chronicle-derived and live: a1 (n1, n4), a2 (n2), a3 (n3), a4 (n6, a gossip copy); a5 deleted n9
    assert end_of_run_outcome(run, "chronicle_reach") == pytest.approx(4 / 5)
    assert end_of_run_outcome(run, "chronicle_reach.chronicle") == pytest.approx(3 / 5)
    assert end_of_run_outcome(run, "chronicle_reach.gossip") == pytest.approx(1 / 5)
    assert end_of_run_outcome(run, "chronicle_reach.inherited") == 0.0
    assert end_of_run_outcome(run, "chronicle_reach.nosuchchannel") is None
    fidelity = (1.0 + _sim(NOTE_2, ITEM_2_2) + 1.0) / 3  # n1, n2, n3; n4 matched nothing
    assert end_of_run_outcome(run, "chronicle_fidelity") == pytest.approx(fidelity)
    assert end_of_run_outcome(run, "chronicle_fidelity.alpha") is None
    # gossip-channel and live: n6 (a4, hop 1), n8 (a2, hop 2)
    assert end_of_run_outcome(run, "gossip_reach") == pytest.approx(2 / 5)
    assert end_of_run_outcome(run, "gossip_reach.1") == pytest.approx(1 / 5)
    assert end_of_run_outcome(run, "gossip_reach.2") == pytest.approx(1 / 5)
    assert end_of_run_outcome(run, "gossip_reach.3") == 0.0
    assert end_of_run_outcome(run, "gossip_reach.one") is None
    assert end_of_run_outcome(run, "gossip_fidelity") == pytest.approx((0.9 + 0.7 + 0.4) / 3)
    assert end_of_run_outcome(run, "gossip_fidelity.1") == pytest.approx(0.8)
    assert end_of_run_outcome(run, "gossip_fidelity.2") == pytest.approx(0.4)
    assert end_of_run_outcome(run, "gossip_fidelity.3") is None and end_of_run_outcome(run, "gossip_fidelity.-1") is None


def test_synthetic_daily_versions_by_hand(tmp_path: Path) -> None:
    run = _synthetic(tmp_path)
    # end of day 1 (tick 12): a1, a2, a3, a5 and d1 alive; no chronicle-derived note yet (a5's n9 was deleted)
    # end of day 2 (tick 24): a4 born; holders a1 (n1), a3 (n3), a4 (n6) and d1 (n7, archived only at its death)
    # end of day 3 (tick 36): d1 dead; the end-of-run state
    assert [DAILY["chronicle_reach"](run, None, d) for d in (1, 2, 3)] == pytest.approx([0.0, 4 / 6, 4 / 5])
    assert [DAILY["chronicle_reach"](run, "gossip", d) for d in (1, 2, 3)] == pytest.approx([0.0, 1 / 6, 1 / 5])
    assert [DAILY["gossip_reach"](run, None, d) for d in (1, 2, 3)] == pytest.approx([1 / 5, 2 / 6, 2 / 5])
    assert DAILY["chronicle_reach"](run, None, 3) == end_of_run_outcome(run, "chronicle_reach")
    # fidelities are over the day's arrivals
    assert DAILY["chronicle_fidelity"](run, None, 1) is None
    assert DAILY["chronicle_fidelity"](run, None, 2) == pytest.approx(1.0)  # n1, n3
    assert DAILY["chronicle_fidelity"](run, None, 3) == pytest.approx(_sim(NOTE_2, ITEM_2_2))  # n2 (n4 unmatched)
    assert [DAILY["gossip_fidelity"](run, None, d) for d in (1, 2)] == pytest.approx([0.9, 0.55])
    assert DAILY["gossip_fidelity"](run, "2", 2) == pytest.approx(0.4) and DAILY["gossip_fidelity"](run, None, 3) is None


def test_reach_is_none_without_living_agents(tmp_path: Path) -> None:
    run = _synthetic(tmp_path)
    conn = sqlite3.connect(str(run / "world.db"))
    conn.execute("UPDATE agents SET status='archived', died_tick=30")
    conn.commit()
    conn.close()
    for name in ("chronicle_reach", "gossip_reach"):
        assert end_of_run_outcome(run, name) is None, name
        assert DAILY[name](run, None, 3) is None and DAILY[name](run, None, 2) is not None, name


# --- (2) a scripted 2-day run of the both arm -------------------------------------------------------------------
@pytest.fixture(scope="module")
def both_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return run_arm(CONFIGS / "exp_press_both.yaml", tmp_path_factory.mktemp("press_both"), (1,), run={"days": 2}) / "seed_1"


def test_scripted_both_arm_reach_and_fidelity(both_run: Path) -> None:
    reach = end_of_run_outcome(both_run, "chronicle_reach")
    fidelity = end_of_run_outcome(both_run, "chronicle_fidelity")
    assert reach is not None and 0.0 < reach <= 1.0
    assert fidelity is not None and 0.0 <= fidelity <= 1.0
    with connect(both_run) as db:
        alive = {r["agent_id"] for r in db.execute("SELECT agent_id FROM agents WHERE status='alive'")}
        holders = {r["agent_id"] for r in db.execute(
            "SELECT agent_id FROM notes WHERE archived=0 AND (channel='chronicle' OR substr(origin_note_id, 1, 10)='chronicle:')")}
        titles = {r["note_id"]: r["title"] for r in db.execute("SELECT note_id, title FROM notes WHERE channel='chronicle'")}
        sims = [json.loads(r["payload"])["similarity"] for r in db.execute("SELECT payload FROM events WHERE kind='gossip_transfer'")]
        gossip_holders = {r["agent_id"] for r in db.execute("SELECT agent_id FROM notes WHERE archived=0 AND channel='gossip'")}
        headlines = {r["day"]: r["item_id"] for r in db.execute("SELECT day, item_id FROM chronicle_items WHERE kind='headline'")}
    assert reach == pytest.approx(len(holders & alive) / len(alive))
    assert end_of_run_outcome(both_run, "gossip_reach") == pytest.approx(len(gossip_holders & alive) / len(alive))
    assert sims and end_of_run_outcome(both_run, "gossip_fidelity") == pytest.approx(sum(sims) / len(sims))
    matches = chronicle_matches(both_run)
    assert matches and all(m.item_id is not None and 0.0 <= (m.similarity or 0.0) <= 1.0 for m in matches)
    assert fidelity == pytest.approx(sum(m.similarity or 0.0 for m in matches) / len(matches))
    # the scripted reader writes "The chronicle said: <headline>": its best item is that day's headline
    for m in matches:
        if titles[m.note_id] == f"Chronicle of day {m.day}":
            assert m.item_id == headlines[m.day], m
    assert DAILY["chronicle_reach"](both_run, None, 2) == pytest.approx(reach)


# --- (3) the four arms through compare --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def press_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("exp_press")
    for arm in ("both", "chronicle", "gossip", "none"):
        run_arm(CONFIGS / f"exp_press_{arm}.yaml", out, (1, 2), run={"days": 1})
    return out


def test_configs_declare_the_press_outcomes() -> None:
    for arm in ("both", "chronicle", "gossip", "none"):
        cfg = load_config(CONFIGS / f"exp_press_{arm}.yaml")
        assert cfg.experiment.name == "exp_press" and cfg.experiment.arm == arm
        assert cfg.experiment.independent_variables == ["chronicle.enabled", "gossip.enabled"]
        assert cfg.experiment.primary_outcome == "chronicle_reach"
        assert cfg.experiment.secondary_outcomes == SECONDARIES
        assert (cfg.chronicle.enabled, cfg.gossip.enabled) == (arm in ("both", "chronicle"), arm in ("both", "gossip"))


def test_compare_accepts_the_four_arms(press_dir: Path) -> None:
    result = compare(press_dir)
    assert result["experiment"] == "exp_press" and result["seeds"] == [1, 2]
    assert sorted(result["arms"]) == ["both", "chronicle", "gossip", "none"]
    assert result["primary"] == "chronicle_reach" and result["secondaries"] == SECONDARIES
    assert len(result["comparisons"]) == 6 and not result["dropped_incomplete"]
    for arm, by_seed in result["values"].items():
        for seed, values in by_seed.items():
            assert set(values) == {"chronicle_reach", *SECONDARIES}, (arm, seed)
            # one day: the first paper is printed as the run ends, so nobody has read it yet
            assert values["chronicle_reach"] == 0.0 and values["chronicle_fidelity"] is None, (arm, seed)
            assert values["gossip_reach"] is not None and 0.0 <= values["gossip_reach"] <= 1.0, (arm, seed)
            if arm in ("chronicle", "none"):
                assert values["gossip_reach"] == 0.0 and values["gossip_fidelity"] is None and values["gossip_transfers"] == 0.0
    for comp in result["comparisons"]:
        assert comp["primary"]["n"] == 2 and comp["primary"]["dropped"] == 0
        assert [s["path"] for s in comp["secondary"]] == SECONDARIES
    # the daily curve of the primary comes from the registered daily version
    assert set(result["curves"]) == {"both", "chronicle", "gossip", "none"}
    assert all(curve == {"1": 0.0} for curve in result["curves"].values())
    assert "chronicle_reach" in render(result)
    _, day_rows = load_metrics(press_dir / "both" / "seed_1")
    assert day_rows  # the last day row the notes_by_channel secondary reads
