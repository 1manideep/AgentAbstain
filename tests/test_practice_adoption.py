"""Kernel-measured practice adoption, persistence and drops; suggestions in the maintenance prompt; adapted and mutated
policy sources; arm-C royalties (MEMORY_EVOLUTION §5.7, §5.8 arm C, Appendix C item 5); and the revert baseline's
inputs in the nightly step.

* the pieces: :func:`excerpt_similarity` (best line window), :func:`mutate_text` (drop, duplicate or move one practice
  line), :meth:`PolicyStore.relabel` / :meth:`PolicyStore.inherited_from`, and the scripted take-up rule of
  :func:`plan` (append the first suggestion from a richer source that the policy does not already hold);
* scripted worlds with practice transmission and maintenance on: a 3-day world adopts (``practice_events`` ``adopted``
  and a policy version whose source is ``adopted``), deterministically; adoptions settle as ``persisted`` (the noop
  keeps them) or ``dropped`` (the decoy rewrites them away) once ``persist_days`` old; arm C pays each persisted
  adoption's source from the research pool and money is conserved; children's policies are ``mutated`` at birth and
  their first own edit is ``adapted``;
* the kernel measures, the answer only reports: a claimed adoption without an edit writes nothing, a silent one counts;
* practices off: no row, no section, and a world with a child's policy edit fingerprinted byte for byte as it was on
  the code before this change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, SMOKE, deep_merge

from void.brain.maintenance_strategies import StrategyContext, plan, take_up
from void.brain.prompt import code_fence, fence
from void.config import MemoryPolicyConfig, load_config, usd_to_micro
from void.culture.practices import PracticeLedger, excerpt_similarity
from void.db import Database
from void.events import OPERATOR, Kind
from void.memory.commands import POLICY_PATH, MemoryCommand
from void.memory.policy import MUTATIONS, SEED_DEFAULT, PolicyStore, clean_policy_text, mutate_text
from void.sim.loop import Simulation
from void.sim.maintenance import MaintenanceResponse
from void.types import AgentStatus

# Practice transmission and maintenance on, the S2 seeds (ten different policies, so an excerpt is news to its
# receiver), a share weight that makes scripted agents suggest within a few days (as tests/test_share_practice.py), and
# the noop strategy: whatever changes a policy here is the take-up rule. The run name seeds where everyone starts;
# under this one the smoke world's agents meet often enough to share.
PRACTICES: dict[str, Any] = deep_merge(NO_PROVIDER_TIERS, {
    "run": {"name": "share_practice", "days": 3},
    "memory": {"policy": {"enabled": True, "seed": "diverse"}, "maintenance": {"enabled": True},
               "practices": {"enabled": True, "scripted_share_weight": 3.0}},
    "tiers": {"scripted": {"maintenance_strategy": "noop"}},
})
# Four days, adoptions settle after one: arm C pays every persisted adoption's source from a funded research pool.
ROYALTIES: dict[str, Any] = deep_merge(PRACTICES, {
    "run": {"days": 4}, "economy": {"research_pool_usd": 1.0},
    "memory": {"practices": {"persist_days": 1}, "selection": {"arm": "C"}},
})
# The decoy rewrites its whole policy every night, so an adoption it keeps tonight is gone the night after: dropped once
# two days old. Children are mutated at birth and their first own edit is adapted.
VARIATION: dict[str, Any] = deep_merge(PRACTICES, {
    "run": {"days": 4}, "memory": {"policy": {"mutate_at_birth": True}, "practices": {"persist_days": 2}},
    "tiers": {"scripted": {"maintenance_strategy": "decoy"}},
})
# Practices off: the smoke world with the policy and the decoy's nightly edits, 12-tick days; under this run name a
# child (born on day 1) edits its inherited policy, so the pin below also covers the adapted label staying off.
OFF: dict[str, Any] = deep_merge(NO_PROVIDER_TIERS, {
    "run": {"name": "adoption_off", "days": 3, "ticks_per_day": 12},
    "memory": {"policy": {"enabled": True}, "maintenance": {"enabled": True}},
    "tiers": {"scripted": {"maintenance_strategy": "decoy"}},
})
# Recorded on the code before this change (the same commit without it, run from a clean export with a relative run
# dir): the OFF world must stay exactly as it was. Re-record only for a deliberate change to that world, and say so.
OFF_GOLDEN = {
    "config_hash": "f1ae648a34b01766d0425744172648c3b157b0f1ab9391fa915263040b509049",
    "metrics": "32f2cea6418a4e8e2b52fc984ee5e2167cefca14f5f0d184188669eb4be780e2",
    "events": "099fc99566576230868887c268731bcc918f279bb5c0e3224df14f675f22ed70",
    "calls": "41ac68cc4e9294a52a6916b197ef903e0911265c640e57298725f269f03e96f5",
    "policy": "598c2c34577f51d805c80f15a0077398b6b43b56332a59c243064d24ed31f70d",
    "ledger": "de64031bf5f873899e433f1bb0a45222af96541206c04972935e8572220109da",
}
SECTION = "## Suggestions received from neighbours"


def _run(overrides: dict[str, Any], workdir: Path) -> Simulation:
    """Run the smoke world under ``overrides`` from ``workdir`` with a relative run dir (no absolute path reaches a
    payload, so runs in different directories compare byte for byte)."""
    workdir.mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(workdir)
        sim = Simulation(load_config(SMOKE, overrides), Path("run"))
        asyncio.run(sim.run())
        sim.metrics_bytes = (Path("run") / "metrics.jsonl").read_bytes()  # type: ignore[attr-defined]
    return sim


def _digest(rows: list[Any]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(r, sort_keys=True, default=str).encode())
        h.update(b"\n")
    return h.hexdigest()


def _events(sim: Simulation, kind: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT tick, kind, agent_id, payload, visibility FROM events"
    rows = sim.db.fetchall(sql + (" WHERE kind=?" if kind else "") + " ORDER BY seq", (kind,) if kind else ())
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def _money_conserved(sim: Simulation) -> None:
    """The ledger identity of tests/test_e2e.py with the research pool term."""
    cfg, m = sim.cfg, sim.wallet.money_snapshot()
    rhs = (m["agents"] + (m["spawn_pool"] - usd_to_micro(cfg.population.spawn_pool_usd)) + (m["house"] - usd_to_micro(cfg.economy.house_budget_usd))
           + (m["chronicle"] - usd_to_micro(cfg.economy.chronicle_budget_usd)) + (m["research_pool"] - usd_to_micro(cfg.economy.research_pool_usd))
           - m["house_debt"])
    ledger = int(sim.db.fetchone("SELECT COALESCE(SUM(delta),0) AS s FROM wallet_ledger")["s"])
    assert ledger == rhs, f"ledger {ledger} != agents+pools deltas {rhs}"


def _day_start(sim: Simulation, tick: int) -> bool:
    return (int(tick) - 1) % int(sim.cfg.run.ticks_per_day) == 0


@pytest.fixture(scope="module")
def adopt(tmp_path_factory: pytest.TempPathFactory) -> Simulation:
    return _run(PRACTICES, tmp_path_factory.mktemp("adopt"))


@pytest.fixture(scope="module")
def royalties(tmp_path_factory: pytest.TempPathFactory) -> Simulation:
    return _run(ROYALTIES, tmp_path_factory.mktemp("royalties"))


@pytest.fixture(scope="module")
def variation(tmp_path_factory: pytest.TempPathFactory) -> Simulation:
    return _run(VARIATION, tmp_path_factory.mktemp("variation"))


# --- the pieces --------------------------------------------------------------------------------------------------------
def test_excerpt_similarity_reads_line_windows() -> None:
    policy = "# Places first\n\n- One note per node.\n- Before moving, read my node notes\n  and go to the richest one.\n"
    assert excerpt_similarity("- One note per node.", policy) == 1.0
    assert excerpt_similarity("One note per NODE", policy) == 1.0                       # tokens, not characters
    assert excerpt_similarity("- Before moving, read my node notes and go to the richest one.", policy) == 1.0  # split
    assert excerpt_similarity("- One note per node.\n- Before moving, read my node notes", policy) == 1.0      # two lines
    assert excerpt_similarity("- Trust agents whose claims were right.", policy) < 0.2
    assert excerpt_similarity("", policy) == excerpt_similarity("- ---", policy) == 0.0  # nothing to hold
    assert excerpt_similarity("- One note per node.", None) == excerpt_similarity("- One note per node.", "") == 0.0


def test_mutate_text_changes_exactly_one_practice_line() -> None:
    text = clean_policy_text(SEED_DEFAULT)
    old = text.split("\n")
    practice = [line for line in old if line.strip() and not line.startswith("#")]
    ops: Counter[str] = Counter()
    for seed in range(60):
        out = mutate_text(text, random.Random(seed))
        assert out is not None and mutate_text(text, random.Random(seed)) == out  # deterministic given the rng
        new_text, op = out
        new = new_text.split("\n")
        ops[op] += 1
        assert [line for line in new if line.startswith("#")] == [line for line in old if line.startswith("#")]
        assert new[0] == old[0]  # the heading keeps its place
        if op == "drop":
            gone = Counter(old) - Counter(new)
            assert len(new) == len(old) - 1 and sum(gone.values()) == 1 and next(iter(gone)) in practice
        elif op == "duplicate":
            extra = Counter(new) - Counter(old)
            assert len(new) == len(old) + 1 and sum(extra.values()) == 1 and next(iter(extra)) in practice
        else:
            assert op == "reorder" and Counter(new) == Counter(old) and new != old
    assert set(ops) == set(MUTATIONS)
    single = "# Mine\n\n- The only practice."
    assert {mutate_text(single, random.Random(s))[1] for s in range(30)} == {"drop", "duplicate"}  # reorder needs two
    assert mutate_text("# Only a heading", random.Random(1)) is None and mutate_text("", random.Random(1)) is None


def test_relabel_and_inherited_from(tmp_path: Path) -> None:
    store = PolicyStore(Database(tmp_path / "w.db"), MemoryPolicyConfig(enabled=True), tmp_path / "vaults", ticks_per_day=12)
    store.init_for("ag_p", 0)
    store.init_for("ag_c", 5, parent_id="ag_p")
    assert store.current("ag_c").source == "inherited" and store.inherited_from("ag_p", 2) is None  # a founder
    store.set("ag_c", "# Mine\n\n- One edit.", 13, source="self_edit")
    assert store.inherited_from("ag_c", 2) == "ag_p"  # its first own edit of what it inherited
    v2 = store.relabel("ag_c", 2, source="adapted", source_agent_id="ag_p")
    assert (v2.source, v2.source_agent_id, v2.text) == ("adapted", "ag_p", "# Mine\n\n- One edit.")
    assert store.current("ag_c") == v2
    header, body = store.parse_file(store.path_for("ag_c").read_text(encoding="utf-8"))
    assert header["source"] == "adapted" and body == v2.text  # the mirror follows the current version
    store.set("ag_c", "# Mine\n\n- Two edits.", 14, source="self_edit")
    assert store.inherited_from("ag_c", 3) is None  # not the first own edit any more
    assert store.edits_today("ag_c", 14) == 2       # a relabelled edit still counts toward the daily budget
    store.relabel("ag_c", 3, source="adopted", source_agent_id="ag_x")
    assert store.edits_today("ag_c", 14) == 2 and store.history("ag_c")[1] == v2  # an older version keeps its row
    with pytest.raises(ValueError):
        store.relabel("ag_c", 1, source="adopted", source_agent_id="ag_x")   # inherited: written by the kernel
    with pytest.raises(ValueError):
        store.relabel("ag_c", 3, source="mutated", source_agent_id=None)     # a kernel source is never given this way
    with pytest.raises(ValueError):
        store.relabel("ag_c", 9, source="adopted", source_agent_id="ag_x")   # no such version
    # a mutated child: kernel versions before its first own edit still make that edit an adaptation
    store.init_for("ag_m", 5, parent_id="ag_p")
    store.set("ag_m", "# How I remember (v1)\n\n- Delete notes that turned out to be wrong.", 5, source="mutated")
    assert store.inherited_from("ag_m", 3) == "ag_p"


def _ctx(**kw: Any) -> StrategyContext:
    base: dict[str, Any] = dict(agent_id="ag_1", day=1, policy_text=clean_policy_text(SEED_DEFAULT), listing="", balance=500_000)
    base.update(kw)
    return StrategyContext(**base)


def _s(event_id: str, source: str, excerpt: str, balance: int, tick: int = 3) -> dict[str, Any]:
    return {"event_id": event_id, "tick": tick, "source_agent_id": source, "source_name": source.upper(), "excerpt": excerpt,
            "source_balance": balance}


SUGGESTIONS = [
    _s("pe_1", "ag_poor", "- Keep a ledger of every forage.", 100_000),                  # not richer
    _s("pe_2", "ag_rich", "- Delete notes that turned out to be wrong.", 900_000),       # already held
    _s("pe_3", "ag_rich", "- Keep a single note called Lessons.", 900_000),             # taken up
    _s("pe_4", "ag_rich2", "- Trust agents whose claims were right.", 950_000),         # only the first is taken
]


def test_the_scripted_take_up_appends_the_first_suggestion_from_a_richer_source() -> None:
    ctx = _ctx(suggestions=SUGGESTIONS)
    r = plan("noop", ctx, random.Random(1))
    lines = ctx.policy_text.split("\n")
    assert r.commands == [MemoryCommand(command="insert", path=POLICY_PATH, insert_line=len(lines),
                                        insert_text="- Keep a single note called Lessons.")]
    assert r.adopted_from == ["ag_rich"] and "took up a practice AG_RICH suggested on tick 3" in r.reflection
    # before a whole-policy rewrite, which then still finds its text; the strategy's own draws are untouched
    d = plan("decoy", ctx, random.Random(7))
    alone = plan("decoy", replace(ctx, suggestions=[]), random.Random(7))
    assert [c.command for c in d.commands] == ["insert", "str_replace"] and d.commands[1:] == alone.commands
    assert d.commands[1].old_str == ctx.policy_text and alone.adopted_from == []
    # nothing to take: no suggestions, no policy right, no policy file, nobody richer, no room
    assert take_up(_ctx()) is None and plan("noop", _ctx(), random.Random(1)).commands == []
    assert take_up(replace(ctx, rights=("notes", "structure"))) is None
    assert take_up(replace(ctx, policy_text=None)) is None
    assert take_up(replace(ctx, balance=10_000_000)) is None
    assert take_up(replace(ctx, policy_max_chars=len(ctx.policy_text) + 10)) is None
    # a blank policy gets the excerpt as its first line
    blank = take_up(replace(ctx, policy_text=""))
    assert blank is not None and blank[1].insert_line == 0


# --- a scripted world adopts ------------------------------------------------------------------------------------------
def test_a_three_day_scripted_world_adopts_and_the_kernel_records_it(adopt: Simulation) -> None:
    sim, threshold = adopt, adopt.cfg.memory.practices.adopt_similarity
    rows = PracticeLedger(sim.db).rows("adopted")
    assert rows, "no scripted adoption in three days"
    events = _events(sim, Kind.PRACTICE_ADOPTED)
    assert len(events) == len(rows)
    adopted_versions = sim.db.fetchall("SELECT * FROM policy_versions WHERE source='adopted' ORDER BY agent_id, version")
    assert adopted_versions
    for r, e in zip(rows, events, strict=True):
        p = e["payload"]
        assert e["visibility"] == OPERATOR and e["tick"] == r["tick"] and e["agent_id"] == r["target_agent_id"]
        assert (p["event_id"], p["source_agent_id"], p["target_agent_id"], p["similarity"]) == (
            r["event_id"], r["source_agent_id"], r["target_agent_id"], r["similarity"])
        assert r["similarity"] >= threshold and _day_start(sim, r["tick"])
        # it was suggested earlier, from that source to that target
        sug = sim.db.fetchone("SELECT * FROM practice_events WHERE event_id=?", (p["suggestion_id"],))
        assert (sug["kind"], sug["source_agent_id"], sug["target_agent_id"], sug["excerpt"]) == (
            "suggested", r["source_agent_id"], r["target_agent_id"], r["excerpt"]) and sug["tick"] < r["tick"]
        # the version the batch wrote holds it, the one before did not
        hist = {v.version: v for v in sim.policy.history(r["target_agent_id"])}
        new = hist[p["version"]]
        assert new.tick == r["tick"] and excerpt_similarity(r["excerpt"], new.text) == r["similarity"]
        assert excerpt_similarity(r["excerpt"], hist[p["version"] - 1].text) < threshold
        assert p["self_reported"] is True  # the scripted take-up names its source in adopted_from
        call = sim.db.fetchone("SELECT extras FROM llm_calls WHERE agent_id=? AND tick=? AND purpose='maintain'",
                               (r["target_agent_id"], r["tick"]))
        extras = json.loads(call["extras"])
        assert extras["adopted"] >= 1 and r["source_agent_id"] in extras["adopted_from"]
    # every adopted version credits the source of an adoption measured in it; its POLICY_CHANGED says so
    by_version = {(r["target_agent_id"], e["payload"]["version"]): r["source_agent_id"] for r, e in zip(rows, events, strict=True)}
    changed = {(e["agent_id"], e["payload"]["version"]): e["payload"] for e in _events(sim, Kind.POLICY_CHANGED)}
    for v in adopted_versions:
        assert by_version[(v["agent_id"], v["version"])] == v["source_agent_id"]
        assert changed[(v["agent_id"], v["version"])]["source"] == "adopted"
        assert changed[(v["agent_id"], v["version"])]["source_agent_id"] == v["source_agent_id"]


def test_the_maintenance_prompt_lists_the_pending_suggestions_fenced_with_their_sources(adopt: Simulation) -> None:
    sim = adopt
    prompts = sim.db.fetchall("SELECT agent_id, tick, prompt_text FROM llm_calls WHERE purpose='maintain' ORDER BY tick, agent_id")
    assert prompts and all(SECTION in p["prompt_text"] for p in prompts)
    for p in prompts:
        text = p["prompt_text"]
        assert text.index(SECTION) < text.index("## Budget")
    shown = 0
    for r in PracticeLedger(sim.db).rows("suggested"):
        nxt = sim.db.fetchone("SELECT prompt_text FROM llm_calls WHERE purpose='maintain' AND agent_id=? AND tick>? ORDER BY tick LIMIT 1",
                              (r["target_agent_id"], r["tick"]))
        if nxt is None:  # the run ended before the target's next maintenance
            continue
        src = sim.registry.get(r["source_agent_id"])
        block = f"- from {src.name} ({src.agent_id}) on tick {r['tick']}:\n{code_fence(fence(r['excerpt']))}"
        assert block in nxt["prompt_text"].split(SECTION, 1)[1], r
        shown += 1
    assert shown >= 1


def test_a_world_with_adoption_is_deterministic(adopt: Simulation, tmp_path: Path) -> None:
    twin = _run(PRACTICES, tmp_path / "twin")
    assert PracticeLedger(twin.db).rows() == PracticeLedger(adopt.db).rows()
    versions = "SELECT * FROM policy_versions ORDER BY agent_id, version"
    assert [dict(r) for r in twin.db.fetchall(versions)] == [dict(r) for r in adopt.db.fetchall(versions)]
    assert _events(twin) == _events(adopt)
    assert twin.metrics_bytes == adopt.metrics_bytes  # type: ignore[attr-defined]


# --- persistence, drops, royalties ------------------------------------------------------------------------------------
def _settled_pairs(sim: Simulation) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Each settlement event with the adopted row it settled."""
    out = []
    for e in _events(sim, Kind.PRACTICE_PERSISTED) + _events(sim, Kind.PRACTICE_DROPPED):
        adopted = sim.db.fetchone("SELECT * FROM practice_events WHERE event_id=?", (e["payload"]["adopted_event_id"],))
        out.append((e, dict(adopted)))
    return out


def _check_settlement(sim: Simulation) -> None:
    span = sim.cfg.memory.practices.persist_days * sim.cfg.run.ticks_per_day
    settled = _settled_pairs(sim)
    ids = [a["event_id"] for _, a in settled]
    assert len(ids) == len(set(ids))  # settled once each
    rows = {r["event_id"]: r for r in PracticeLedger(sim.db).rows()}
    for e, a in settled:
        row = rows[e["payload"]["event_id"]]
        assert a["kind"] == "adopted" and e["tick"] - a["tick"] >= span and _day_start(sim, e["tick"])
        assert (row["kind"], row["source_agent_id"], row["target_agent_id"], row["excerpt"], row["tick"]) == (
            "persisted" if e["kind"] == Kind.PRACTICE_PERSISTED else "dropped", a["source_agent_id"], a["target_agent_id"],
            a["excerpt"], e["tick"])
        assert e["visibility"] == OPERATOR and e["payload"]["similarity"] == row["similarity"]
    # every adoption old enough by the last day start is settled
    last = sim.db.fetchone("SELECT MAX(tick) AS t FROM events WHERE kind='day_start'")["t"]
    due = [r["event_id"] for r in PracticeLedger(sim.db).rows("adopted") if r["tick"] <= last - span]
    assert sorted(due) == sorted(ids)
    assert sim.db.kv_get("practices_settled_through") == last - span


def test_adoptions_kept_for_persist_days_persist_and_arm_c_pays_their_sources(royalties: Simulation) -> None:
    sim = royalties
    _check_settlement(sim)
    threshold = sim.cfg.memory.practices.adopt_similarity
    persisted = PracticeLedger(sim.db).rows("persisted")
    assert persisted, "no adoption persisted"
    for r in persisted:
        assert r["similarity"] >= threshold
    pay = _events(sim, Kind.PRACTICE_ROYALTY)
    assert [e["payload"]["event_id"] for e in pay] == [r["event_id"] for r in persisted]  # one per persisted adoption
    amount = usd_to_micro(sim.cfg.memory.selection.practice_royalty_usd)
    ledger = sim.db.fetchall("SELECT agent_id, delta, kind, ref FROM wallet_ledger WHERE kind='practice_royalty' ORDER BY entry_id")
    paid = [e for e in pay if e["payload"]["paid"]]
    assert paid and len(ledger) == 2 * len(paid)
    for e, (out, into) in zip(paid, zip(ledger[0::2], ledger[1::2], strict=True), strict=True):
        p = e["payload"]
        assert e["visibility"] == OPERATOR and e["agent_id"] == p["source_agent_id"] and p["amount_usd"] > 0
        assert (out["agent_id"], out["delta"], out["ref"]) == ("research_pool", -amount, p["event_id"])
        assert (into["agent_id"], into["delta"], into["ref"]) == (p["source_agent_id"], amount, p["event_id"])
    for e in pay:
        if not e["payload"]["paid"]:
            assert e["payload"]["reason"] == "source_dead"
    assert sim.wallet.ledger_sum(kind="practice_royalty") == 0  # a transfer: the pool's loss is the sources' gain
    assert usd_to_micro(sim.cfg.economy.research_pool_usd) - sim.wallet.balance("research_pool") == amount * len(paid)
    _money_conserved(sim)


def test_arm_a_settles_but_never_pays(adopt: Simulation) -> None:
    assert adopt.cfg.memory.selection.arm == "A"
    assert _events(adopt, Kind.PRACTICE_ROYALTY) == []
    assert adopt.db.fetchone("SELECT COUNT(*) AS n FROM wallet_ledger WHERE kind='practice_royalty'")["n"] == 0


def test_adoptions_rewritten_away_are_dropped(variation: Simulation) -> None:
    sim = variation
    _check_settlement(sim)
    threshold = sim.cfg.memory.practices.adopt_similarity
    dropped = _events(sim, Kind.PRACTICE_DROPPED)
    assert dropped, "the decoy kept every adoption"
    for e in dropped:
        p = e["payload"]
        assert p["reason"] in ("removed", "target_dead") and (p["similarity"] < threshold or p["reason"] == "target_dead")
        if p["reason"] == "removed":  # the receiver's policy no longer holds it
            text = sim.db.fetchone("SELECT text FROM policy_versions WHERE agent_id=? AND tick<=? ORDER BY version DESC LIMIT 1",
                                   (p["target_agent_id"], e["tick"]))["text"]
            excerpt = sim.db.fetchone("SELECT excerpt FROM practice_events WHERE event_id=?", (p["event_id"],))["excerpt"]
            assert excerpt_similarity(excerpt, text) < threshold
    assert _events(sim, Kind.PRACTICE_ROYALTY) == []  # arm A


# --- variation at birth and the child's first edit ---------------------------------------------------------------------
def test_children_are_mutated_at_birth_and_their_first_own_edit_is_adapted(variation: Simulation) -> None:
    sim = variation
    children = sim.db.fetchall("SELECT agent_id, parent_id, born_tick FROM agents WHERE parent_id IS NOT NULL")
    assert children, "no child was born"
    mutated = adapted = 0
    changed = {(e["agent_id"], e["payload"]["version"]): e for e in _events(sim, Kind.POLICY_CHANGED)}
    births = {e["agent_id"]: e for e in _events(sim, Kind.BIRTH)}
    for c in children:
        hist = sim.policy.history(c["agent_id"])
        assert hist[0].source == "inherited" and hist[0].source_agent_id == c["parent_id"]
        if len(hist) > 1 and hist[1].source == "mutated":
            mutated += 1
            v = hist[1]
            assert v.tick == c["born_tick"] and v.source_agent_id is None and v.text != hist[0].text
            e = changed[(c["agent_id"], 2)]
            assert e["payload"]["source"] == "mutated" and e["payload"]["mutation"] in MUTATIONS
            assert e["tick"] == births[c["agent_id"]]["tick"]
            redo = mutate_text(hist[0].text, sim.rng.stream("mutate", c["agent_id"]))
            assert redo is not None and clean_policy_text(redo[0]) == v.text and redo[1] == e["payload"]["mutation"]
        own = [v for v in hist if v.source in ("self_edit", "adapted", "adopted")]
        if own:
            first = own[0]
            if first.source == "adapted":
                adapted += 1
                assert first.source_agent_id == c["parent_id"]
                assert changed[(c["agent_id"], first.version)]["payload"]["source"] == "adapted"
            else:
                assert first.source == "adopted"  # a measured adoption takes precedence over the adaptation label
            assert all(v.source != "adapted" for v in own[1:])
    assert mutated >= 1 and adapted >= 1
    # founders are never mutated or adapted
    founders = [r["agent_id"] for r in sim.db.fetchall("SELECT agent_id FROM agents WHERE parent_id IS NULL")]
    labelled = {r["agent_id"] for r in sim.db.fetchall("SELECT agent_id FROM policy_versions WHERE source IN ('mutated','adapted')")}
    assert founders and not labelled & set(founders)


# --- the kernel measures; the answer only reports ---------------------------------------------------------------------
SMALL: dict[str, Any] = deep_merge(NO_PROVIDER_TIERS, {
    "run": {"days": 4, "ticks_per_day": 12},
    "economy": {"research_pool_usd": 1.0},
    "memory": {"policy": {"enabled": True, "seed": "diverse"}, "maintenance": {"enabled": True},
               "practices": {"enabled": True, "persist_days": 2}, "selection": {"arm": "C"}},
})


def test_a_claimed_adoption_counts_for_nothing_and_a_silent_one_is_measured(make_sim, monkeypatch: pytest.MonkeyPatch) -> None:
    sim = make_sim("adoption_claims", SMALL)
    try:
        a, b, c = sorted(r.agent_id for r in sim.registry.alive())[:3]
        excerpt_b, excerpt_c = "- Write the price of every node I visit.", "- Never trust a rumour twice."
        sim.kernel.practices.suggest(3, a, b, excerpt_b)
        sim.kernel.practices.suggest(4, a, c, excerpt_c)
        responses = {
            b: MaintenanceResponse(reflection="I took a's advice", commands=[], adopted_from=[a]),   # a claim, no edit
            c: MaintenanceResponse(reflection="tidy", commands=[MemoryCommand(
                command="insert", path=POLICY_PATH, insert_line=len((sim.policy.text(c) or "").split("\n")),
                insert_text=excerpt_c)], adopted_from=[]),                                         # an edit, no claim
        }
        real = sim.maintenance._scripted

        def scripted(rec, ctx, prompt, system_tokens, day):
            result, response = real(rec, ctx, prompt, system_tokens, day)
            return result, responses.get(rec.agent_id, response)

        monkeypatch.setattr(sim.maintenance, "_scripted", scripted)
        asyncio.run(sim.maintenance.run_day(2, 13))
        rows = PracticeLedger(sim.db).rows("adopted")
        assert [(r["source_agent_id"], r["target_agent_id"], r["excerpt"], r["similarity"]) for r in rows] == [(a, c, excerpt_c, 1.0)]
        extras = {r["agent_id"]: json.loads(r["extras"]) for r in sim.db.fetchall("SELECT agent_id, extras FROM llm_calls WHERE purpose='maintain'")}
        assert (extras[b]["adopted_from"], extras[b]["adopted"]) == ([a], 0)
        assert (extras[c]["adopted_from"], extras[c]["adopted"]) == ([], 1)
        (ev,) = _events(sim, Kind.PRACTICE_ADOPTED)
        assert ev["payload"]["self_reported"] is False and ev["payload"]["target_agent_id"] == c
        assert sim.policy.current(c).source == "adopted" and sim.policy.current(c).source_agent_id == a
        assert sim.policy.current(b).source == "seed"
        maint = {e["agent_id"]: e["payload"] for e in _events(sim, Kind.MAINTENANCE)}
        assert (maint[b]["adopted_from"], maint[b]["adopted"], maint[c]["adopted"]) == ([a], 0, 1)
        # the prompt each saw listed its suggestion, fenced, with the source; an agent with none says so
        prompt = sim.db.fetchone("SELECT prompt_text FROM llm_calls WHERE purpose='maintain' AND agent_id=?", (b,))["prompt_text"]
        assert f"({a}) on tick 3:\n{code_fence(fence(excerpt_b))}" in prompt
        none = sim.db.fetchone("SELECT prompt_text FROM llm_calls WHERE purpose='maintain' AND agent_id NOT IN (?,?) LIMIT 1", (b, c))
        assert f"{SECTION}\n- (none)" in none["prompt_text"]

        # settlement: due two days on; the receiver has died, so it is dropped, and settling again writes nothing
        assert sim.maintenance.settle_practices(3, 25) == []  # one day old: not due
        sim.registry.update(c, status=AgentStatus.BANKRUPT)
        (dropped,) = sim.maintenance.settle_practices(4, 37)
        assert PracticeLedger(sim.db).rows("dropped")[0]["event_id"] == dropped
        (ev,) = _events(sim, Kind.PRACTICE_DROPPED)
        assert ev["payload"]["reason"] == "target_dead" and ev["payload"]["adopted_tick"] == 13
        assert sim.maintenance.settle_practices(4, 37) == [] and len(PracticeLedger(sim.db).rows("dropped")) == 1
        assert _events(sim, Kind.PRACTICE_ROYALTY) == []  # nothing persisted, nothing paid
    finally:
        sim.db.close()


def test_a_royalty_to_a_dead_source_is_not_paid(make_sim) -> None:
    sim = make_sim("adoption_dead_source", SMALL)
    try:
        a, b = sorted(r.agent_id for r in sim.registry.alive())[:2]
        excerpt = "- Count the coins twice."
        sim.policy.set(b, (sim.policy.text(b) or "") + "\n" + excerpt, 13, source="self_edit")
        PracticeLedger(sim.db).adopt(13, a, b, excerpt, 1.0)
        sim.registry.update(a, status=AgentStatus.BANKRUPT)
        before = sim.wallet.balance("research_pool")
        (persisted,) = sim.maintenance.settle_practices(4, 37)
        (ev,) = _events(sim, Kind.PRACTICE_ROYALTY)
        assert ev["payload"] == {"event_id": persisted, "source_agent_id": a, "target_agent_id": b, "amount_usd": 0.0,
                                 "paid": False, "reason": "source_dead"}
        assert sim.wallet.balance("research_pool") == before
        with sim.db.tx(), pytest.raises(RuntimeError):  # royalties move money outside any transaction
            sim.maintenance._pay_royalty(persisted, {"source_agent_id": a, "target_agent_id": b}, 37, 4)
    finally:
        sim.db.close()


# --- practices off ----------------------------------------------------------------------------------------------------
def test_practices_off_writes_nothing_and_leaves_the_world_byte_identical(tmp_path: Path) -> None:
    sim = _run(OFF, tmp_path / "off")
    db = sim.db
    got = {
        "config_hash": sim.cfg.hash(),
        "metrics": hashlib.sha256(sim.metrics_bytes).hexdigest(),  # type: ignore[attr-defined]
        "events": _digest([[r["tick"], r["kind"], r["agent_id"], json.loads(r["payload"]), r["visibility"]]
                           for r in db.fetchall("SELECT tick, kind, agent_id, payload, visibility FROM events ORDER BY seq")]),
        "calls": _digest([dict(r) for r in db.fetchall(
            "SELECT tick, agent_id, purpose, raw_text, thought, extras, input_tokens, output_tokens, world_cost, prompt_text "
            "FROM llm_calls ORDER BY call_id")]),
        "policy": _digest([dict(r) for r in db.fetchall("SELECT * FROM policy_versions ORDER BY agent_id, version")]),
        "ledger": _digest([dict(r) for r in db.fetchall("SELECT * FROM wallet_ledger ORDER BY entry_id")]),
    }
    assert got == OFF_GOLDEN
    assert PracticeLedger(db).rows() == []
    assert not any(k.startswith("practice_") for k in {r["kind"] for r in db.fetchall("SELECT DISTINCT kind FROM events")})
    assert db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE prompt_text LIKE ?", (f"%{SECTION}%",))["n"] == 0
    # a child edited the policy it inherited, and the edit stays self_edit in a world without the lineage operators
    child_edits = db.fetchall("SELECT p.source FROM policy_versions p JOIN agents a ON a.agent_id=p.agent_id "
                              "WHERE a.parent_id IS NOT NULL AND p.version > 1")
    assert child_edits and {r["source"] for r in child_edits} == {"self_edit"}
    assert db.fetchone("SELECT COUNT(*) AS n FROM policy_versions WHERE source NOT IN ('seed','inherited','self_edit')")["n"] == 0
    assert db.kv_get("practices_settled_through") is None


# --- the revert baseline's inputs ---------------------------------------------------------------------------------------
async def _ticks(sim: Simulation, n: int) -> None:
    for _ in range(n):
        await sim.tick()


def test_revert_restores_the_previous_policy_once_income_falls(make_sim) -> None:
    over = deep_merge(NO_PROVIDER_TIERS, {"run": {"days": 4, "ticks_per_day": 12},
                                          "memory": {"policy": {"enabled": True}, "maintenance": {"enabled": True}},
                                          "tiers": {"scripted": {"maintenance_strategy": "revert"}}})
    sim = make_sim("revert_regress", over)
    try:
        asyncio.run(_ticks(sim, 24))  # days 1 and 2; day 2's maintenance had no earlier day to compare with
        aid = sorted(r.agent_id for r in sim.registry.alive())[0]
        rec = sim.registry.get(aid)
        assert sim.maintenance.context_for(rec, 2, 13).previous_outcomes is None
        seed = sim.policy.text(aid)
        sim.policy.set(aid, "# Edited\n\n- A practice that made things worse.", 24, source="self_edit")
        sim.wallet.credit(aid, 5, usd_to_micro(1.0), "forage")  # day 1 paid far more than day 2 did
        ctx = sim.maintenance.context_for(rec, 3, 25)
        assert ctx.previous_policy_text == seed and ctx.previous_outcomes["income_usd"] > ctx.outcomes["income_usd"]
        assert sim.db.fetchone("SELECT COUNT(*) AS n FROM memory_commands WHERE purpose='maintain'")["n"] == 0
        asyncio.run(_ticks(sim, 1))  # day 3 begins: revert sees the regression and restores the previous version
        cmds = sim.db.fetchall("SELECT agent_id, tick, command, ok, args FROM memory_commands WHERE purpose='maintain'")
        assert [(c["agent_id"], c["tick"], c["command"], c["ok"]) for c in cmds] == [(aid, 25, "str_replace", 1)]
        assert json.loads(cmds[0]["args"])["new_str"] == seed
        assert sim.policy.text(aid) == seed and sim.policy.current(aid).source == "self_edit"
        _money_conserved(sim)
    finally:
        sim.db.close()
