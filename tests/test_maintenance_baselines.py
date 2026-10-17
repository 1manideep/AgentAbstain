"""MEMORY_EVOLUTION 6.13 row 5: the scripted maintenance baselines distill, revert and retire, as E0 arms.

Unit tests pin each strategy's batch on a hand-built :class:`StrategyContext` and apply the batches to a real
vault and policy store; two three-day scripted runs check that distillation leaves a lower near-duplicate rate
than the no-op; and a seven-arm ``exp_mem_e0`` directory at two seeds and two days goes through ``compare``.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_experiments import CONFIGS, run_arm

from void.brain.maintenance_strategies import (
    DISTILL_COSINE,
    RETIRE_CAP,
    StrategyContext,
    format_node_fact,
    near_duplicate_groups,
    nodes_from_notes,
    plan,
    title_stem,
)
from void.brain.scripted import node_memory
from void.config import MaintenanceConfig, MemoryConfig, MemoryPolicyConfig, load_config
from void.db import Database
from void.ids import IdFactory
from void.memory.commands import INDEX_DIR, NOTES_DIR, POLICY_PATH, MemoryCommands, virtual_path
from void.memory.embed import HashingEmbedder, cosine
from void.memory.policy import SEED_DEFAULT, PolicyStore, clean_policy_text
from void.memory.store import MemoryStore
from void.research.analysis import end_of_run_outcome
from void.research.compare import compare, render
from void.research.outcomes.memory_structure import NEAR_DUPLICATE_COSINE
from void.sim.loop import Simulation
from void.types import RetrievedNote

AGENT = "ag_1"
ALL_RIGHTS = ["notes", "policy", "structure"]
E0_ARMS = ("noop", "index_builder", "hoarder", "decoy", "distill", "revert", "retire")
NEW_ARMS = ("distill", "revert", "retire")
SEED_TEXT = clean_policy_text(SEED_DEFAULT)
EDITED = "# Places first\n\n- One note per node: where it is, how rich it was, when I last saw it."


def _note(name: str, title: str, body: str, tick: int, tags: list[str], area: str = NOTES_DIR) -> dict[str, Any]:
    return {"path": f"{area}/{name}.md", "title": title, "body": body, "tick": tick, "tags": tags}


PLACES = ["resources", "places"]
NOTES: list[dict[str, Any]] = [
    _note("node-n3-aaaaaa", "Node n3", "A resource node.", 0, ["entity"]),
    _note("node-n3-seen-on-day-1-bbbbbb", "Node n3 seen on day 1", "Saw [[Node n3]] at (12, -5) on day 1; it was rich.", 3, PLACES),
    _note("vein-at-node-n3-on-day-1-cccccc", "Vein at node n3 on day 1",
          "Saw a vein at [[Node n3]] at (12, -5) on day 1, tick 4; it pays x4 for 8 more ticks.", 4, PLACES + ["vein"]),
    _note("node-n3-seen-on-day-1-2-dddddd", "Node n3 seen on day 1-2", "Saw [[Node n3]] at (12, -5) on day 1; it was rich.", 5, PLACES),
    _note("node-n5-seen-on-day-1-eeeeee", "Node n5 seen on day 1", "Saw [[Node n5]] at (-20, 3) on day 1; it was thin.", 9, PLACES),
    _note("bao-said-something-on-day-1-ffffff", "Bao said something on day 1", "[[Bao]] told me: the weather is mild.", 10, ["people"]),
    _note("node-n3-index-gggggg", "Node n3 index", "# Node n3\nPosition (12, -5). Last seen rich on day 1.", 13, ["agent", "index"], INDEX_DIR),
    _note("node-n3-seen-empty-on-day-2-hhhhhh", "Node n3 seen empty on day 2", "Saw [[Node n3]] at (12, -5) on day 2; it was empty.", 14, PLACES),
    _note("bao-said-something-on-day-2-iiiiii", "Bao said something on day 2", "[[Bao]] told me: the market is quiet today.", 15, ["people"]),
    _note("vein-at-node-n3-on-day-2-jjjjjj", "Vein at node n3 on day 2",
          "Saw a vein at [[Node n3]] at (12, -5) on day 2, tick 16; it pays x4 for 8 more ticks.", 16, PLACES + ["vein"]),
]


def _ctx(**kw: Any) -> StrategyContext:
    notes = kw.pop("notes", list(NOTES))
    base: dict[str, Any] = dict(agent_id=AGENT, day=2, policy_text=SEED_TEXT, listing="", notes=notes,
                                new_notes=[n for n in notes if n["tick"] > 12],
                                outcomes={"income_usd": 0.10, "spend_usd": 0.05, "failed_actions": 0, "forages": 4, "talks": 1},
                                nodes_known=nodes_from_notes(notes))
    base.update(kw)
    return StrategyContext(**base)


def _sig(ctx: StrategyContext, strategy: str) -> list[tuple[str, str | None]]:
    return [(c.command, c.path) for c in plan(strategy, ctx, random.Random(3)).commands]  # type: ignore[arg-type]


def _path(name: str) -> str:
    return next(n["path"] for n in NOTES if n["path"].endswith(f"-{name}.md"))


# --- the shared pieces --------------------------------------------------------------------------------------------
def test_title_stems_drop_dates_counters_and_status_words() -> None:
    assert title_stem("Node n3 seen on day 1") == title_stem("Node n3 seen on day 1-2") == "node n3 seen"
    assert title_stem("Node n3 seen empty on day 2") == "node n3 seen"  # a correction note joins its sightings
    assert title_stem("node n3 seen distilled day 4") == "node n3 seen"  # a summary joins its later sources
    assert title_stem("Foraging at node n3 on day 2") == "foraging at node n3" != title_stem("Node n3 seen on day 2")
    assert title_stem("Bao said something on day 1") == "bao said something"
    assert title_stem("Day 3") == ""


def test_the_distill_threshold_is_the_outcome_threshold() -> None:
    assert DISTILL_COSINE == NEAR_DUPLICATE_COSINE


def test_node_facts_are_read_back_by_both_parsers() -> None:
    fact = format_node_fact("n3", 12.0, -5.0, "empty", 2)
    assert fact == "Saw [[Node n3]] at (12, -5) on day 2; it was empty."
    assert format_node_fact("n9", 0.5, -0.0, "thin", None) == "Saw [[Node n9]] at (0.5, 0); it was thin."
    body = "\n".join(["# Distilled", f"- {fact}", f"- {format_node_fact('n5', -20.0, 3.0, 'rich', 1)}"])
    assert nodes_from_notes([{"path": "p", "title": "t", "body": body, "tick": 30}]) == [("n3", 12.0, -5.0, "empty"), ("n5", -20.0, 3.0, "rich")]
    mem = node_memory([RetrievedNote("x", "t", body, 0.5, 0, None, 30)], 12)
    assert (mem["n3"].bucket, mem["n3"].x, mem["n3"].y, mem["n5"].bucket) == ("empty", 12.0, -5.0, "rich")


def test_near_duplicate_groups_link_by_stem_or_cosine_and_ignore_input_order() -> None:
    long = "Foraged at [[Node n7]] at (1, 2) on day 1; it was rich. The weather was mild, the queue short and the path clear."
    a = _note("a", "Foraging at node n7 on day 1", long, 3, PLACES)
    b = _note("b", "Foraging near node n7 on day 1", long, 4, PLACES)  # another stem, nearly the same words
    c = _note("c", "Ada gave me money", "[[Ada]] transferred money to me on day 1. [[Ada]] is generous.", 5, ["money"])
    emb = HashingEmbedder(256)
    assert cosine(emb.embed(f"{a['title']} {long}"), emb.embed(f"{b['title']} {long}")) >= DISTILL_COSINE
    assert title_stem(a["title"]) != title_stem(b["title"])
    assert near_duplicate_groups([c, b, a]) == [[a, b]]
    pool = [n for n in NOTES if "entity" not in n["tags"] and "index" not in n["tags"] and "vein" not in n["tags"]]
    groups = near_duplicate_groups(pool)
    shuffled = list(pool)
    random.Random(5).shuffle(shuffled)
    assert near_duplicate_groups(shuffled) == groups
    assert [[n["title"] for n in g] for g in groups] == [
        ["Node n3 seen on day 1", "Node n3 seen on day 1-2", "Node n3 seen empty on day 2"],  # largest first, oldest first
        ["Bao said something on day 1", "Bao said something on day 2"],
    ]


# --- distill -----------------------------------------------------------------------------------------------------
def test_distill_merges_each_group_into_a_summary_then_deletes_its_sources() -> None:
    ctx = _ctx()
    r = plan("distill", ctx, random.Random(3))
    assert [(c.command, c.path) for c in r.commands] == [
        ("create", f"{NOTES_DIR}/node-n3-seen-distilled-day-2.md"),
        ("delete", _path("bbbbbb")), ("delete", _path("dddddd")), ("delete", _path("hhhhhh")),
        ("create", f"{NOTES_DIR}/bao-said-something-distilled-day-2.md"),
        ("delete", _path("ffffff")), ("delete", _path("iiiiii")),
    ]
    n3, bao = r.commands[0].file_text, r.commands[4].file_text
    assert n3 == ("# Distilled: node n3 seen (day 2)\nMerged 3 notes; the newest fact per node.\n"
                  "- Saw [[Node n3]] at (12, -5) on day 2; it was empty.")
    assert bao == ("# Distilled: bao said something (day 2)\nMerged 2 notes; the newest fact per node.\n"
                   "- Latest: [[Bao]] told me: the market is quiet today.")
    assert r.reflection.startswith("distill: 2 groups") and "5 sources deleted" in r.reflection
    # never an entity stub, an index note or a vein note; the lone n5 sighting has no duplicate and stays
    touched = {c.path for c in r.commands}
    for keep in ("aaaaaa", "cccccc", "eeeeee", "gggggg", "jjjjjj"):
        assert _path(keep) not in touched


def test_distill_keeps_each_batch_within_its_budget_and_rights() -> None:
    assert _sig(_ctx(max_commands=4), "distill") == [
        ("create", f"{NOTES_DIR}/node-n3-seen-distilled-day-2.md"),
        ("delete", _path("bbbbbb")), ("delete", _path("dddddd")), ("delete", _path("hhhhhh"))]
    # a group larger than the room left: the summary still holds every member's facts, the oldest sources go first
    tight = plan("distill", _ctx(max_commands=3), random.Random(3))
    assert [(c.command, c.path) for c in tight.commands] == [
        ("create", f"{NOTES_DIR}/node-n3-seen-distilled-day-2.md"), ("delete", _path("bbbbbb")), ("delete", _path("dddddd"))]
    assert "on day 2; it was empty." in (tight.commands[0].file_text or "")
    assert _sig(_ctx(max_commands=1), "distill") == []
    assert _sig(_ctx(rights=("notes", "policy")), "distill") == []
    assert _sig(_ctx(rights=("structure",)), "distill") == []


def test_distill_never_reuses_a_title_the_vault_holds() -> None:
    held = _note("node-n3-seen-distilled-day-2-kkkkkk", "node n3 seen distilled day 2", "Older summary.", 1, ["agent"])
    notes = [n for n in NOTES if n["tags"] != ["people"]] + [held]
    sig = _sig(_ctx(notes=notes), "distill")
    assert sig[0] == ("create", f"{NOTES_DIR}/node-n3-seen-distilled-day-2-2.md")
    assert ("delete", held["path"]) in sig  # the older summary shares the stem and is merged too


# --- revert ------------------------------------------------------------------------------------------------------
def _rctx(today: float, yesterday: float | None, **kw: Any) -> StrategyContext:
    base: dict[str, Any] = dict(policy_text=EDITED, previous_policy_text=SEED_TEXT,
                                outcomes={"income_usd": today, "spend_usd": 0.02, "failed_actions": 0, "forages": 3, "talks": 0},
                                previous_outcomes=None if yesterday is None else {"income_usd": yesterday})
    base.update(kw)
    return _ctx(**base)


def test_revert_restores_the_previous_policy_only_when_income_fell() -> None:
    r = plan("revert", _rctx(0.05, 0.10), random.Random(3))
    assert [(c.command, c.path, c.old_str, c.new_str) for c in r.commands] == [("str_replace", POLICY_PATH, EDITED, SEED_TEXT)]
    assert r.reflection.startswith("revert: income fell from $0.100 to $0.050")
    assert _sig(_rctx(0.10, 0.10), "revert") == [] and _sig(_rctx(0.20, 0.10), "revert") == []
    assert _sig(_rctx(0.05, None), "revert") == []                                   # no day before to compare with
    assert _sig(_rctx(0.05, 0.10, previous_policy_text=None), "revert") == []        # nothing earlier to restore
    assert _sig(_rctx(0.05, 0.10, previous_policy_text=EDITED), "revert") == []      # the previous text is the current
    assert _sig(_rctx(0.05, 0.10, rights=("notes", "structure")), "revert") == []
    assert _sig(_rctx(0.05, 0.10, policy_text=None), "revert") == []                 # no policy file in this world
    blank = plan("revert", _rctx(0.05, 0.10, policy_text=""), random.Random(3)).commands
    assert [(c.command, c.path, c.insert_line, c.insert_text) for c in blank] == [("insert", POLICY_PATH, 0, SEED_TEXT)]


# --- retire ------------------------------------------------------------------------------------------------------
def _crowd(n: int) -> list[dict[str, Any]]:
    notes = [_note(f"note-{i:02d}-xxxxxx", f"Note {i:02d}", f"Fact number {i}.", 100 - i, ["people"]) for i in range(n)]
    return notes + [
        _note("node-n3-aaaaaa", "Node n3", "A resource node.", 0, ["entity"]),
        _note("node-n3-index-gggggg", "Node n3 index", "# Node n3", 1, ["agent", "index"], INDEX_DIR),
        _note("self-yyyyyy", "Self", "I am Ada.", 2, ["self"]),
    ]


def test_retire_deletes_the_oldest_notes_beyond_the_cap() -> None:
    assert RETIRE_CAP == 40
    notes = _crowd(RETIRE_CAP + 5)
    oldest = sorted((n for n in notes if n["title"].startswith("Note")), key=lambda n: (n["tick"], n["path"]))[:5]
    r = plan("retire", _ctx(notes=notes), random.Random(3))
    assert [(c.command, c.path) for c in r.commands] == [("delete", n["path"]) for n in oldest]
    assert r.reflection == "retire: 45 notes over the cap of 40, deleted the 5 oldest"
    assert _sig(_ctx(notes=notes, max_commands=3), "retire") == [("delete", n["path"]) for n in oldest[:3]]
    assert _sig(_ctx(notes=_crowd(RETIRE_CAP)), "retire") == []        # index, entity and self notes are not counted
    assert _sig(_ctx(notes=notes, rights=("notes", "policy")), "retire") == []


# --- the batches on a real vault and policy store ----------------------------------------------------------------
@pytest.fixture
def vault(tmp_path: Path) -> Iterator[SimpleNamespace]:
    db = Database(tmp_path / "world.db")
    ids = IdFactory("run")
    store = MemoryStore(db, MemoryConfig(), ids, tmp_path / "vaults")
    policy = PolicyStore(db, MemoryPolicyConfig(enabled=True, seed="default"), tmp_path / "vaults", ticks_per_day=12)
    commands = MemoryCommands(db, store, MaintenanceConfig(), ids, policy=policy)
    try:
        yield SimpleNamespace(db=db, store=store, policy=policy, commands=commands)
    finally:
        db.close()


def _note_dicts(v: SimpleNamespace) -> list[dict[str, Any]]:
    return [{"path": virtual_path(n), "title": n.title, "body": n.body, "tick": int(n.created_tick), "tags": list(n.tags)}
            for n in v.store.list_notes(AGENT)]


def _apply_all(v: SimpleNamespace, ctx: StrategyContext, strategy: str, tick: int) -> None:
    response = plan(strategy, ctx, random.Random(3))  # type: ignore[arg-type]
    assert response.commands
    for cmd in response.commands:
        res = v.commands.apply(AGENT, cmd, tick, purpose="maintain", rights=ALL_RIGHTS)
        assert res.ok, (cmd.command, cmd.path, res.message)


def test_distill_batches_apply_cleanly_and_keep_the_newest_facts(vault: SimpleNamespace) -> None:
    for n in NOTES:
        if "entity" not in n["tags"]:
            vault.store.remember(AGENT, n["tick"], n["body"], title=n["title"], tags=n["tags"], auto_link=False)
    before = _note_dicts(vault)
    _apply_all(vault, _ctx(notes=before, listing=vault.commands.listing(AGENT)), "distill", 25)
    after = _note_dicts(vault)
    assert len(after) == len(before) - 5 + 2
    assert sorted(n["title"] for n in after if "agent" in n["tags"] and "index" not in n["tags"]) == [
        "bao said something distilled day 2", "node n3 seen distilled day 2"]
    plain = [n for n in after if "index" not in n["tags"]]
    assert dict((nid, b) for nid, _, _, b in nodes_from_notes(plain)) == {"n3": "empty", "n5": "thin"}
    assert near_duplicate_groups([n for n in after if not {"index", "vein"} & set(n["tags"])]) == []
    # the next night: a fresh sighting joins the summary's stem, and the two are merged again
    vault.store.remember(AGENT, 30, "Saw [[Node n3]] at (12, -5) on day 3; it was rich.", title="Node n3 seen on day 3", tags=PLACES)
    _apply_all(vault, _ctx(day=3, notes=_note_dicts(vault)), "distill", 37)
    titles = [n["title"] for n in _note_dicts(vault)]
    assert "node n3 seen distilled day 3" in titles and "node n3 seen distilled day 2" not in titles
    assert dict((nid, b) for nid, _, _, b in nodes_from_notes(_note_dicts(vault)))["n3"] == "rich"


def test_revert_batches_restore_the_previous_version(vault: SimpleNamespace) -> None:
    vault.policy.init_for(AGENT, 0)
    edit = plan("decoy", _ctx(policy_text=vault.policy.text(AGENT)), random.Random(1)).commands[0]
    assert vault.commands.apply(AGENT, edit, 13, purpose="maintain", rights=ALL_RIGHTS).ok
    history = vault.policy.history(AGENT)
    assert len(history) == 2 and history[-1].text != SEED_TEXT
    ctx = _rctx(0.02, 0.09, policy_text=vault.policy.text(AGENT), previous_policy_text=history[-2].text)
    _apply_all(vault, ctx, "revert", 25)
    history = vault.policy.history(AGENT)
    assert [v.source for v in history] == ["seed", "self_edit", "self_edit"] and history[-1].text == SEED_TEXT


def test_retire_batches_apply_and_leave_the_cap(vault: SimpleNamespace) -> None:
    for i in range(RETIRE_CAP + 3):
        vault.store.remember(AGENT, i + 1, f"Fact number {i} about the weather.", title=f"Fact {i:02d}", tags=["weather"], auto_link=False)
    vault.store.remember(AGENT, 0, "# Node n3", title="Node n3 index", tags=["agent", "index"], auto_link=False)
    _apply_all(vault, _ctx(notes=_note_dicts(vault)), "retire", 60)
    left = _note_dicts(vault)
    assert len([n for n in left if "index" not in n["tags"]]) == RETIRE_CAP
    assert "Node n3 index" in {n["title"] for n in left} and "Fact 00" not in {n["title"] for n in left}


# --- the E0 arms ---------------------------------------------------------------------------------------------------
def test_the_new_arm_configs_differ_from_the_noop_only_in_the_strategy() -> None:
    def strip(arm: str) -> dict[str, Any]:
        data = load_config(CONFIGS / f"exp_mem_e0_{arm}.yaml").model_dump()
        assert data["experiment"]["arm"] == arm and data["tiers"]["scripted"]["maintenance_strategy"] == arm
        data["run"]["name"] = data["experiment"]["arm"] = data["tiers"]["scripted"]["maintenance_strategy"] = None
        return data

    noop = strip("noop")
    for arm in NEW_ARMS:
        assert strip(arm) == noop, arm
    assert load_config(CONFIGS / "exp_mem_e0_distill.yaml").memory.maintenance.scripted_billing == "flat"


def _run(config: Path, out: Path, **run: Any) -> Path:
    sim = Simulation(load_config(config, {"run": run}), out)
    try:
        asyncio.run(sim.run())
    finally:
        sim.db.close()
    return out


@pytest.fixture(scope="module")
def three_days(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("e0_distill")
    return {arm: _run(CONFIGS / f"exp_mem_e0_{arm}.yaml", out / arm, days=3, seed=42) for arm in ("noop", "distill")}


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path / "world.db"))
    conn.row_factory = sqlite3.Row
    return conn


def test_distill_leaves_fewer_near_duplicates_than_the_noop(three_days: dict[str, Path]) -> None:
    noop = end_of_run_outcome(three_days["noop"], "near_duplicate_rate")
    distill = end_of_run_outcome(three_days["distill"], "near_duplicate_rate")
    assert noop is not None and distill is not None and distill < noop, (distill, noop)
    assert end_of_run_outcome(three_days["distill"], "vault_notes") < end_of_run_outcome(three_days["noop"], "vault_notes")
    db = _db(three_days["distill"])
    rows = db.execute("SELECT command, ok FROM memory_commands WHERE purpose='maintain'").fetchall()
    db.close()
    assert rows and all(r["ok"] for r in rows) and {r["command"] for r in rows} == {"create", "delete"}
    for arm, run_dir in three_days.items():  # flat billing: every arm pays the full output cap, whatever its batch
        db = _db(run_dir)
        out = {int(r["output_tokens"]) for r in db.execute("SELECT output_tokens FROM llm_calls WHERE purpose='maintain'")}
        db.close()
        assert out == {768}, arm


@pytest.fixture(scope="module")
def e0_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("exp_mem_e0")
    for arm in E0_ARMS:
        run_arm(CONFIGS / f"exp_mem_e0_{arm}.yaml", out, (1, 2), run={"days": 2, "ticks_per_day": 12})
    return out


def test_compare_accepts_seven_e0_arms(e0_dir: Path) -> None:
    result = compare(e0_dir)
    assert result["primary"] == "mean_balance" and set(result["arms"]) == set(E0_ARMS) and result["n_seeds"] == 2
    assert result["dropped_incomplete"] == []
    assert len(result["comparisons"]) == 21  # every pair of seven arms
    for arm in E0_ARMS:
        for seed in ("1", "2"):
            assert result["values"][arm][seed]["mean_balance"] is not None, (arm, seed)
    text = render(result)
    assert all(arm in text for arm in E0_ARMS)


def test_new_arms_pay_every_night_and_leave_their_signature(e0_dir: Path) -> None:
    for seed in (1, 2):
        cmds: dict[str, list[tuple[str, bool]]] = {}
        for arm in E0_ARMS:
            db = _db(e0_dir / arm / f"seed_{seed}")
            cmds[arm] = [(r["command"], bool(r["ok"])) for r in db.execute(
                "SELECT command, ok FROM memory_commands WHERE purpose='maintain' ORDER BY rowid")]
            calls = db.execute("SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='maintain'").fetchone()["n"]
            versions = db.execute("SELECT MAX(version) AS v FROM policy_versions").fetchone()["v"]
            db.close()
            assert calls > 0, (arm, seed)
            if arm == "revert":
                assert versions == 1  # nothing else edits the policy in E0, so there is never an earlier version to restore
        assert cmds["distill"] and all(ok for _, ok in cmds["distill"]) and {c for c, _ in cmds["distill"]} == {"create", "delete"}
        assert cmds["revert"] == [] and cmds["retire"] == []  # two days leave every vault under the cap
