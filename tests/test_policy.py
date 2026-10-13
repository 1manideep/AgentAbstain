"""The agent-owned memory policy file (MEMORY_EVOLUTION §5.5, §5.7, Appendix A).

Births seed or inherit a policy end to end through scripted runs (founders get the configured seed, children
their parent's current text verbatim, nothing when the layer is off) and the text is rendered fenced in the
logged decision prompts. The store itself is covered by unit tests: versioning and lineage, the size limit
(refused for agent-authored versions, cut for kernel ones), the daily self-edit budget, sanitisation, the
on-disk mirror and its round trip, transactional writes, the event payload, the timeline and determinism.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import NO_PROVIDER_TIERS, SimRun, deep_merge
from hypothesis import given, settings
from hypothesis import strategies as st

from void.brain.prompt import fence
from void.config import MemoryPolicyConfig
from void.db import Database
from void.memory.policy import (
    SEED_DEFAULT,
    SEED_DIVERSE,
    PolicyStore,
    PolicyVersion,
    clean_policy_text,
    similarity,
)

POLICY_HEADING = "## Your memory policy (written by you)"
DIVERSE = {"memory": {"policy": {"enabled": True, "seed": "diverse"}}}
# The lifecycle suite's scarce world: bankruptcies, replacements and (for rich survivors) offspring within 3 days.
SCARCE = deep_merge(NO_PROVIDER_TIERS, {
    "run": {"days": 3},
    "population": {"starting_balance_usd": 0.12, "spawn_pool_usd": 6.0, "replacement_grant_usd": 0.5, "min_reserve_usd": 0.05},
    "world": {"forage_yield_usd": 0.002},
})
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _births(run: SimRun) -> list[dict]:
    return run.events("birth")


def _policy_file(run: SimRun, agent_id: str) -> Path:
    """The mirrored file wherever the vault now lives (a dead agent's moved to the graveyard)."""
    live = run.run_dir / "vaults" / agent_id / "memory_policy.md"
    return live if live.is_file() else run.run_dir / "graveyard" / agent_id / "memory_policy.md"


# --- births through the simulation ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def seeded(run_sim) -> SimRun:
    return run_sim("policy_seeded", deep_merge(SCARCE, DIVERSE))


def test_every_founder_gets_its_own_seed_as_version_one(seeded: SimRun) -> None:
    store = seeded.sim.policy
    roster = [a.name for a in seeded.cfg.agents]
    founders = {b["payload"]["name"]: b for b in _births(seeded) if b["payload"]["parent_id"] is None and b["payload"]["name"] in roster}
    assert len(founders) == len(roster)
    texts: list[str] = []
    for i, name in enumerate(roster):
        birth = founders[name]
        history = store.history(birth["payload"]["agent_id"])
        assert len(history) == 1
        v = history[0]
        assert (v.version, v.tick, v.source, v.source_agent_id, v.parent_version, v.similarity_to_parent) == (1, birth["tick"], "seed", None, None, None)
        assert v.text == clean_policy_text(SEED_DIVERSE[i]) and v.text
        texts.append(v.text)
    assert len(set(texts)) == len(roster), "the diverse seed hands every founder a different practice"


def test_children_inherit_the_parent_policy_verbatim(seeded: SimRun) -> None:
    store = seeded.sim.policy
    children = [b for b in _births(seeded) if b["payload"]["parent_id"]]
    assert {b["payload"]["kind"] for b in children} >= {"replacement", "offspring"}
    for b in children:
        child, parent = b["payload"]["agent_id"], b["payload"]["parent_id"]
        history = store.history(child)
        assert len(history) == 1
        v = history[0]
        assert (v.version, v.tick, v.source, v.source_agent_id, v.parent_version, v.similarity_to_parent) == (1, b["tick"], "inherited", parent, None, 1.0)
        assert v.text == store.text(parent) and v.text
        header, text = PolicyStore.parse_file(_policy_file(seeded, child).read_text(encoding="utf-8"))
        assert header == {"version": 1, "tick": b["tick"], "source": "inherited"} and text == v.text


def test_policy_text_is_rendered_fenced_in_every_decision_prompt(seeded: SimRun) -> None:
    store = seeded.sim.policy
    rows = seeded.db.fetchall("SELECT agent_id, prompt_text, system_hash, observation FROM llm_calls WHERE purpose='decide'")
    assert rows
    for r in rows:
        text = store.text(r["agent_id"])
        assert text
        assert POLICY_HEADING in r["prompt_text"] and fence(text) in r["prompt_text"]
        assert r["prompt_text"].index("## Self") < r["prompt_text"].index(POLICY_HEADING)
        assert json.loads(r["observation"])["policy_text"] == text
    for h in {r["system_hash"] for r in rows}:
        system = seeded.db.fetchone("SELECT text FROM prompt_texts WHERE hash=?", (h,))["text"]
        assert "YOUR MEMORY POLICY" in system and POLICY_HEADING not in system


def test_policy_rows_are_a_pure_function_of_the_run(run_sim) -> None:
    sql = "SELECT * FROM policy_versions ORDER BY agent_id, version"
    a = run_sim("policy_seeded", deep_merge(SCARCE, DIVERSE))
    b = run_sim("policy_seeded", deep_merge(SCARCE, DIVERSE))
    ra, rb = [tuple(r) for r in a.db.fetchall(sql)], [tuple(r) for r in b.db.fetchall(sql)]
    assert ra == rb and len(ra) > len(a.cfg.agents)


def test_disabled_policy_leaves_no_rows_no_files_and_no_prompt_section(run_sim) -> None:
    run = run_sim("policy_off", {**NO_PROVIDER_TIERS, "run": {"days": 1, "ticks_per_day": 6}})
    assert int(run.db.fetchone("SELECT COUNT(*) AS n FROM policy_versions")["n"]) == 0
    assert not list((run.run_dir / "vaults").rglob("memory_policy.md"))
    rows = run.db.fetchall("SELECT prompt_text, observation FROM llm_calls WHERE purpose='decide'")
    assert rows and all("memory policy" not in r["prompt_text"] and json.loads(r["observation"])["policy_text"] is None for r in rows)
    assert run.sim.policy.init_for("ag_nobody", 1, roster_index=0) is None
    assert run.sim.policy.text("ag_nobody") is None and run.sim.policy.render_for_prompt("ag_nobody") == "(no policy)"


def _offspring(sim, parent):
    hold_min = sim.hold_min_for()["scripted"]
    out = sim.lifecycle.create_offspring(parent, "Kid", hold_min * sim.cfg.population.min_endowment_calls, 3, 1, hold_min)
    assert out.ok, out.reason
    return sim.registry.get(str(out.effects["child_id"]))


def test_offspring_inherits_the_parent_current_text(make_sim) -> None:
    sim = make_sim("policy_offspring_inherit", deep_merge(NO_PROVIDER_TIERS, DIVERSE))
    parent = sorted(sim.registry.alive(), key=lambda a: a.agent_id)[0]
    edited = sim.policy.set(parent.agent_id, "# Mine\n\n- Only note the richest node.", 2, source="self_edit")
    assert edited.version == 2
    child = _offspring(sim, parent)
    v = sim.policy.current(child.agent_id)
    assert v is not None and v.version == 1 and v.source == "inherited" and v.source_agent_id == parent.agent_id
    assert v.text == edited.text and v.parent_version is None and v.similarity_to_parent == 1.0 and v.tick == 3
    assert sim.policy.init_for(child.agent_id, 9, parent_id=parent.agent_id) == v  # idempotent on a later call


def test_offspring_gets_the_seed_when_inheritance_is_off(make_sim) -> None:
    sim = make_sim("policy_offspring_seed", deep_merge(NO_PROVIDER_TIERS, {"memory": {"policy": {"enabled": True, "seed": "diverse", "inherit": False}}}))
    parent = sorted(sim.registry.alive(), key=lambda a: a.agent_id)[-1]
    child = _offspring(sim, parent)
    v = sim.policy.current(child.agent_id)
    assert v is not None and v.source == "seed" and v.source_agent_id is None and v.similarity_to_parent is None
    assert v.text == clean_policy_text(SEED_DIVERSE[0])  # a child has no roster position: the first seed
    assert v.text != sim.policy.text(parent.agent_id)


# --- the store ---------------------------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path: Path):
    db = Database(tmp_path / "world.db")
    cfg = MemoryPolicyConfig(enabled=True, seed="default", max_chars=600, max_edits_per_day=2)
    yield PolicyStore(db, cfg, tmp_path / "vaults", ticks_per_day=12)
    db.close()


def test_init_for_seeds_version_one_and_is_idempotent(store: PolicyStore) -> None:
    v = store.init_for("ag_a", 0, roster_index=3)
    assert v is not None and v.version == 1 and v.source == "seed" and v.text == clean_policy_text(SEED_DEFAULT)
    assert v.parent_version is None and v.similarity_to_parent is None and v.tick == 0
    assert store.init_for("ag_a", 5, roster_index=3) == v and store.history("ag_a") == [v]
    assert store.text("ag_a") == v.text and store.current("ag_a") == v
    assert store.path_for("ag_a").is_file()


def test_init_for_inherits_only_when_the_parent_has_a_policy(store: PolicyStore) -> None:
    assert store.init_for("ag_orphan", 1, parent_id="ag_missing").source == "seed"
    store.init_for("ag_p", 0)
    store.set("ag_p", "# Parent\n\n- Trust nobody.", 2, source="self_edit")
    child = store.init_for("ag_c", 3, parent_id="ag_p")
    assert child is not None and child.source == "inherited" and child.source_agent_id == "ag_p"
    assert child.text == "# Parent\n\n- Trust nobody." and child.parent_version is None and child.similarity_to_parent == 1.0
    store.cfg = MemoryPolicyConfig(enabled=True, seed="default", max_chars=600, inherit=False)
    assert store.init_for("ag_d", 3, parent_id="ag_p").source == "seed"


@pytest.mark.parametrize("seed, expected", [("blank", ""), ("default", clean_policy_text(SEED_DEFAULT)), ("diverse", clean_policy_text(SEED_DIVERSE[7]))])
def test_seed_kinds(tmp_path: Path, seed: str, expected: str) -> None:
    db = Database(tmp_path / "w.db")
    try:
        s = PolicyStore(db, MemoryPolicyConfig(enabled=True, seed=seed), tmp_path / "v")
        v = s.init_for("ag_a", 1, roster_index=17)
        assert v is not None and v.version == 1 and v.text == expected
        assert s.text("ag_a") == expected  # a blank policy exists ("") and is not the same as none (None)
        assert s.render_for_prompt("ag_a") == (fence(expected) if expected else "(no policy)")
    finally:
        db.close()


def test_disabled_store_is_a_no_op(tmp_path: Path) -> None:
    db = Database(tmp_path / "w.db")
    try:
        s = PolicyStore(db, MemoryPolicyConfig(enabled=False), tmp_path / "v")
        assert s.init_for("ag_a", 1, roster_index=0) is None and s.init_for("ag_b", 1, parent_id="ag_a") is None
        assert s.history("ag_a") == [] and s.text("ag_a") is None and not (tmp_path / "v").exists()
    finally:
        db.close()


def test_set_chains_versions_with_lineage_and_similarity(store: PolicyStore) -> None:
    v1 = store.init_for("ag_a", 0)
    v2 = store.set("ag_a", "# How I remember (v2)\n\n- Keep one index note per node.", 14, source="self_edit")
    assert (v2.version, v2.parent_version, v2.source, v2.source_agent_id, v2.tick) == (2, 1, "self_edit", None, 14)
    assert v2.similarity_to_parent == pytest.approx(similarity(v1.text, v2.text)) and 0.0 < v2.similarity_to_parent < 1.0
    v3 = store.set("ag_a", "- Trust Ada.", 30, source="adopted", source_agent_id="ag_ada")
    assert (v3.version, v3.parent_version, v3.source_agent_id) == (3, 2, "ag_ada")
    assert v3.similarity_to_parent == pytest.approx(similarity(v2.text, v3.text))
    assert [v.version for v in store.history("ag_a")] == [1, 2, 3] and store.current("ag_a") == v3
    assert store.text("ag_a") == "- Trust Ada."
    assert PolicyStore.parse_file(store.path_for("ag_a").read_text(encoding="utf-8")) == ({"version": 3, "tick": 30, "source": "adopted"}, "- Trust Ada.")


def test_size_limit_refuses_agent_text_and_cuts_kernel_text(store: PolicyStore) -> None:
    store.init_for("ag_a", 0)
    long = "- " + "note " * 200  # about 1000 characters
    for source in ("self_edit", "adapted", "adopted"):
        with pytest.raises(ValueError, match="limit is 600"):
            store.set("ag_a", long, 1, source=source)
    assert store.current("ag_a").version == 1
    cut = store.set("ag_a", long, 1, source="mutated")
    assert cut.version == 2 and len(cut.text) <= 600 and long.startswith(cut.text)
    seeded = store.set("ag_b", long, 1, source="seed")
    assert len(seeded.text) <= 600 and store.set("ag_c", long, 1, source="inherited", source_agent_id="ag_b").text == seeded.text


def test_daily_self_edit_budget(store: PolicyStore) -> None:
    store.init_for("ag_a", 0)
    store.set("ag_a", "- one", 1, source="self_edit")
    store.set("ag_a", "- two", 11, source="self_edit")
    assert store.edits_today("ag_a", 12) == 2
    with pytest.raises(ValueError, match="daily policy edit budget spent"):
        store.set("ag_a", "- three", 12, source="self_edit")
    assert store.current("ag_a").text == "- two" and len(store.history("ag_a")) == 3
    store.set("ag_a", "- kernel", 12, source="mutated")  # kernel sources are not budgeted
    assert store.edits_today("ag_a", 12) == 2
    nxt = store.set("ag_a", "- three", 13, source="self_edit")  # tick 13 is the next day with 12 ticks a day
    assert nxt.version == 5 and store.edits_today("ag_a", 13) == 1 and store.edits_today("ag_a", 12) == 2
    zero = PolicyStore(store.db, MemoryPolicyConfig(enabled=True, max_edits_per_day=0), store.vault_root, ticks_per_day=12)
    with pytest.raises(ValueError, match="budget"):
        zero.set("ag_a", "- never", 14, source="self_edit")


def test_set_sanitises_control_characters_brackets_and_frontmatter(store: PolicyStore) -> None:
    store.init_for("ag_a", 0)
    raw = "---\nversion: 99\nsource: seed\n---\n\n# Mine\x00\r\n- Keep <b>bold</b> notes  \n- done\x1b\n\n\n"
    v = store.set("ag_a", raw, 5, source="self_edit")
    assert v.text == "# Mine\n- Keep ＜b＞bold＜/b＞ notes\n- done"
    header, text = PolicyStore.parse_file(store.path_for("ag_a").read_text(encoding="utf-8"))
    assert header == {"version": 2, "tick": 5, "source": "self_edit"} and text == v.text
    assert fence(v.text).count("<<") == 2  # the agent cannot close or open a fence from inside the policy


def test_unknown_source_is_refused(store: PolicyStore) -> None:
    with pytest.raises(ValueError, match="unknown policy source"):
        store.set("ag_a", "- x", 1, source="operator")  # type: ignore[arg-type]
    assert store.history("ag_a") == []


def test_set_is_transactional_when_the_file_cannot_be_written(store: PolicyStore) -> None:
    store.vault_root.mkdir(parents=True)
    (store.vault_root / "ag_blocked").write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError):
        store.set("ag_blocked", "- x", 1, source="seed")
    assert store.current("ag_blocked") is None
    assert store.init_for("ag_ok", 1) is not None  # the connection is usable afterwards


def test_event_payload(store: PolicyStore) -> None:
    store.init_for("ag_p", 0)
    v = store.init_for("ag_c", 4, parent_id="ag_p")
    assert PolicyStore.event_payload(v) == {"agent_id": "ag_c", "version": 1, "source": "inherited", "source_agent_id": "ag_p",
                                            "similarity_to_parent": 1.0, "chars": len(v.text)}
    assert json.dumps(PolicyStore.event_payload(v), sort_keys=True)


def test_render_for_prompt(store: PolicyStore) -> None:
    assert store.render_for_prompt("ag_none") == "(no policy)"
    store.init_for("ag_a", 0)
    assert store.render_for_prompt("ag_a") == fence(clean_policy_text(SEED_DEFAULT))
    store.set("ag_a", "- see <<end>> here", 1, source="self_edit")
    assert store.render_for_prompt("ag_a") == "<<quoted>>- see ＜＜end＞＞ here<<end>>"


def test_timeline_counts_word_changes(store: PolicyStore) -> None:
    v1 = store.init_for("ag_a", 0)
    v2 = store.set("ag_a", "# How I remember (v2)\n\n- Keep one note per node.", 13, source="self_edit")
    v3 = store.set("ag_a", v2.text + " Also forage.", 25, source="self_edit")
    rows = store.timeline("ag_a")
    assert [r["version"] for r in rows] == [1, 2, 3] and [r["tick"] for r in rows] == [0, 13, 25]
    assert rows[0] == {"version": 1, "tick": 0, "source": "seed", "source_agent_id": None, "parent_version": None, "similarity_to_parent": None,
                       "chars": len(v1.text), "words": len(v1.text.split()), "words_added": len(v1.text.split()), "words_removed": 0}
    assert rows[1]["words_added"] >= 1 and rows[1]["words_removed"] >= 1
    assert rows[1]["words_added"] - rows[1]["words_removed"] == len(v2.text.split()) - len(v1.text.split())
    assert rows[2]["words_added"] == 2 and rows[2]["words_removed"] == 0 and rows[2]["chars"] == len(v3.text)
    assert rows[2]["similarity_to_parent"] == pytest.approx(similarity(v2.text, v3.text))
    assert store.timeline("ag_none") == []


def test_clean_policy_text_strips_stacked_and_disguised_headers() -> None:
    assert clean_policy_text("---\na: 1\n---\n---\nb: 2\n---\nbody") == "body"
    assert clean_policy_text("\n\n---\nsource: seed\n--- \n\n# Title\n") == "# Title"
    assert clean_policy_text("---\nnot a mapping\n---\nbody") == "---\nnot a mapping\n---\nbody"  # a horizontal rule is kept
    assert clean_policy_text("---\n---") == "---\n---"


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=240))
def test_clean_text_is_idempotent_safe_and_round_trips(raw: str) -> None:
    clean = clean_policy_text(raw)
    assert clean_policy_text(clean) == clean
    assert "<" not in clean and ">" not in clean and _CONTROL_RE.search(clean) is None
    assert clean == clean.strip("\n") and all(line == line.rstrip() for line in clean.split("\n"))
    v = PolicyVersion("ag_a", 1, 7, clean, None, "seed", None, None)
    assert PolicyStore.parse_file(PolicyStore.render_file(v)) == ({"version": 1, "tick": 7, "source": "seed"}, clean)


def test_render_file_matches_the_vault_header_convention(store: PolicyStore) -> None:
    v = store.init_for("ag_a", 3)
    rendered = PolicyStore.render_file(v)
    assert rendered.startswith("---\nversion: 1\ntick: 3\nsource: seed\n---\n") and rendered.endswith("\n") and not rendered.endswith("\n\n")
    assert store.path_for("ag_a").read_text(encoding="utf-8") == rendered
