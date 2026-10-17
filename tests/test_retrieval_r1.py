"""R1 retrieval (MEMORY_EVOLUTION §5.3), fast frontmatter reading and writing, and provider brains built only when reachable.

* R1: with ``memory.index_boost > 0`` a note tagged ``index`` enters retrieval at ``cosine + index_boost``; at 0 the
  ranking, scores and stats are exactly those of the pre-R1 algorithm (a copy of it lives below as the reference).
* ``body_of`` returns what ``split_frontmatter`` returns as the rest, without YAML, for every file the kernel writes.
* ``render_frontmatter`` memoises fragments but writes exactly the pure dumper's bytes (the C dumper does not).
* A scripted-only world never imports a provider SDK.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from void.config import MemoryConfig, load_config
from void.db import Database
from void.ids import IdFactory
from void.memory import notes as notes_mod
from void.memory.embed import cosine, unpack
from void.memory.graph import NoteGraph
from void.memory.notes import (
    Note,
    Provenance,
    body_of,
    render_frontmatter,
    render_note,
    split_frontmatter,
)
from void.memory.store import ENTITY_TAG, INDEX_TAG, MemoryStore
from void.rng import RNG
from void.sim.loop import Simulation
from void.types import RetrievedNote

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"


def make_store(tmp_path: Path, name: str = "run", **cfg: Any) -> MemoryStore:
    root = tmp_path / name
    return MemoryStore(Database(root / "world.db"), MemoryConfig(**cfg), IdFactory(name), root / "vaults")


# --- the pre-R1 algorithm, copied verbatim from MemoryStore.retrieve_with_stats before index_boost existed ---------
def _r0_retrieve(store: MemoryStore, agent_id: str, query: str, tick: int, k: int | None = None) -> tuple[list[RetrievedNote], dict]:
    cfg = store.cfg
    max_depth = max(0, cfg.hops) if cfg.mode == "graph" else 0
    stats: dict[str, object] = {"hop_hist": {h: 0 for h in range(max_depth + 1)}, "mean_age_ticks": 0.0, "channel_mix": {},
                                "entry_ids": []}
    rows = store.db.fetchall(
        "SELECT note_id, agent_id, path, title, created_tick, importance, tags, channel, source_agent_id, hop, origin_note_id, "
        "origin_generation, transfer_tick, path_agents, archived, embedding FROM notes WHERE agent_id=? AND archived=0 ORDER BY note_id",
        (agent_id,),
    )
    limit = int(k) if k is not None else cfg.max_retrieved
    if not rows or limit <= 0:
        return [], stats
    by_id = {str(r["note_id"]): r for r in rows}
    qvec = store.embedder.embed(query)
    sims = {nid: cosine(qvec, unpack(r["embedding"])) for nid, r in by_id.items()}
    ranked = sorted(sims, key=lambda nid: (-sims[nid], nid))
    entry_ids = ranked[: max(0, cfg.entry_k)]
    entry = {nid: max(0.0, sims[nid]) for nid in entry_ids}
    stats["entry_ids"] = list(entry_ids)
    if cfg.mode == "graph":
        walked = NoteGraph(store._light_notes(agent_id, by_id), hub_tags=(ENTITY_TAG,)).walk_with_hops(entry, cfg.hops, cfg.hop_decay)
    else:
        walked = {nid: (score, 0) for nid, score in entry.items()}
    final: dict[str, float] = {}
    for nid, (walk_score, _) in walked.items():
        r = by_id[nid]
        if walk_score <= 0.0 or ENTITY_TAG in set(json.loads(r["tags"])):
            continue
        final[nid] = walk_score * (0.5 + float(r["importance"])) * store._recency_weight(tick, int(r["created_tick"]))
    order = sorted(final, key=lambda nid: (-final[nid], nid))[:limit]
    out: list[RetrievedNote] = []
    hop_hist: dict[int, int] = stats["hop_hist"]  # type: ignore[assignment]
    channel_mix: dict[str, int] = {}
    ages: list[int] = []
    for nid in order:
        r = by_id[nid]
        note = store._load(r)
        out.append(RetrievedNote(note_id=nid, title=note.title, body=note.body, score=final[nid], hop=int(r["hop"]),
                                 source_agent_id=r["source_agent_id"], created_tick=int(r["created_tick"])))
        depth = walked[nid][1]
        hop_hist[depth] = hop_hist.get(depth, 0) + 1
        channel_mix[str(r["channel"])] = channel_mix.get(str(r["channel"]), 0) + 1
        ages.append(max(0, int(tick) - int(r["created_tick"])))
    stats["channel_mix"] = channel_mix
    stats["mean_age_ticks"] = (sum(ages) / len(ages)) if ages else 0.0
    return out, stats


# --- R1: the index boost ------------------------------------------------------------------------------------------
QUERY = "foraged at the eastern node and found food"


def _plain_and_index(store: MemoryStore) -> tuple[Note, Note, float, float]:
    plain = store.remember("ag_1", 1, "Foraged at the eastern node and found food", title="East forage", auto_link=False)
    index = store.remember("ag_1", 1, "Index of nodes: east node stock low, north node rich", title="Node index",
                           tags=[INDEX_TAG], auto_link=False)
    q = store.embedder.embed(QUERY)
    cos_plain = cosine(q, store._embed(plain.title, plain.body))
    cos_index = cosine(q, store._embed(index.title, index.body))
    return plain, index, cos_plain, cos_index


@pytest.mark.parametrize("mode", ["graph", "vector"])
def test_an_index_note_with_lower_cosine_enters_only_when_boosted(tmp_path: Path, mode: str) -> None:
    s = make_store(tmp_path, entry_k=1, mode=mode, auto_link_k=0)
    plain, index, cos_plain, cos_index = _plain_and_index(s)
    assert 0.0 < cos_index < cos_plain  # the setup: the plain note is the better cosine match

    got, stats = s.retrieve_with_stats("ag_1", QUERY, 2, k=1)
    assert stats["entry_ids"] == [plain.note_id] and stats["index_entries"] == 0
    assert [r.note_id for r in got] == [plain.note_id]
    assert index.note_id not in [r.note_id for r in s.retrieve("ag_1", QUERY, 2, k=8)]

    boost = (cos_plain - cos_index) + 0.1  # just enough to put the index note first
    s.cfg = MemoryConfig(entry_k=1, mode=mode, auto_link_k=0, index_boost=boost)
    got, stats = s.retrieve_with_stats("ag_1", QUERY, 2, k=1)
    assert stats["entry_ids"] == [index.note_id] and stats["index_entries"] == 1
    assert [r.note_id for r in got] == [index.note_id]
    # the entry weight is the boosted score; importance 0.5 and age 1 at half-life 96 scale it as for any note
    assert got[0].score == pytest.approx((cos_index + boost) * (0.5 + 0.5) * s._recency_weight(2, 1))

    # a boost too small to close the gap changes nothing
    s.cfg = MemoryConfig(entry_k=1, mode=mode, auto_link_k=0, index_boost=(cos_plain - cos_index) / 2)
    assert s.retrieve_with_stats("ag_1", QUERY, 2, k=1)[1]["entry_ids"] == [plain.note_id]


def test_boosted_entry_weight_is_clamped_at_zero(tmp_path: Path) -> None:
    """An index note whose boosted score is still negative enters (if nothing ranks higher) with weight 0, so it is
    never returned, exactly like a non-positive cosine under R0."""

    class Fixed:
        dim = 8

        def embed(self, text: str) -> list[float]:
            if "index" in text.lower():
                return [-1.0, 0, 0, 0, 0, 0, 0, 0]
            return [1.0, 0, 0, 0, 0, 0, 0, 0] if "query" in text else [0, 1.0, 0, 0, 0, 0, 0, 0]

    root = tmp_path / "fixed"
    s = MemoryStore(Database(root / "world.db"), MemoryConfig(entry_k=2, index_boost=0.25, auto_link_k=0), IdFactory("f"),
                    root / "vaults", embedder=Fixed())
    idx = s.remember("ag_1", 1, "the index", title="Index", tags=[INDEX_TAG], auto_link=False)
    other = s.remember("ag_1", 1, "orthogonal", title="Other", auto_link=False)
    got, stats = s.retrieve_with_stats("ag_1", "query", 1)
    assert stats["entry_ids"] == [other.note_id, idx.note_id] and stats["index_entries"] == 1  # 0.0 > -0.75
    assert got == []  # both entry weights are 0 after clamping


def _random_vault(store: MemoryStore, r: random.Random, n: int) -> list[str]:
    words = ("east north south west node river food water stock rich thin camp trade gadget claim storm bao ada cyra "
             "index plan path lake hill sold bought forage rest").split()
    titles: list[str] = []
    for i in range(n):
        tags = r.choice([[], [], ["resources"], [INDEX_TAG], [INDEX_TAG, "agent"], ["probe"]])
        links = r.sample(titles, k=min(len(titles), r.choice([0, 0, 1, 2])))
        text = " ".join(r.choice(words) for _ in range(r.randint(3, 14)))
        if r.random() < 0.15:
            note = store.ensure_entity("ag_1", f"Node {r.choice(words)}", i, "A resource node.")
        else:
            note = store.remember("ag_1", r.randint(0, 200), text, title=None if r.random() < 0.5 else text[:20], links_to=links,
                                  tags=tags, importance=r.choice([0.1, 0.5, 0.9]))
        titles.append(note.title)
    return words


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("mode", ["graph", "vector"])
def test_boost_zero_is_the_pre_r1_algorithm(tmp_path: Path, seed: int, mode: str) -> None:
    r = random.Random(seed)
    s = make_store(tmp_path, f"r0_{seed}_{mode}", mode=mode, entry_k=r.choice([1, 3, 4]))
    words = _random_vault(s, r, 40)
    for _ in range(25):
        query = " ".join(r.choice(words) for _ in range(r.randint(1, 6)))
        tick, k = r.randint(0, 400), r.choice([None, 1, 4, 8, 50])
        got, stats = s.retrieve_with_stats("ag_1", query, tick, k=k)
        want, want_stats = _r0_retrieve(s, "ag_1", query, tick, k)
        assert got == want  # ids, bodies and float scores, exactly
        assert stats.pop("index_entries") == sum(INDEX_TAG in s.get_note(n).tags for n in stats["entry_ids"])  # type: ignore[union-attr,arg-type]
        assert stats == want_stats
    # a boost with no index note in the vault is also R0
    s2 = make_store(tmp_path, f"noidx_{seed}_{mode}", mode=mode, index_boost=0.7)
    s0 = make_store(tmp_path, f"noidx0_{seed}_{mode}", mode=mode)
    for store in (s2, s0):
        store.remember("ag_1", 3, "east node food rich", title="a")
        store.remember("ag_1", 4, "north camp trade water", title="b", links_to=["a"])
    assert [(x.title, x.score) for x in s2.retrieve("ag_1", "east food", 9)] == \
        [(x.title, x.score) for x in s0.retrieve("ag_1", "east food", 9)]


def test_empty_vault_stats_are_unchanged(tmp_path: Path) -> None:
    s = make_store(tmp_path, index_boost=0.5)
    assert s.retrieve_with_stats("ag_none", QUERY, 3) == ([], {"hop_hist": {0: 0, 1: 0, 2: 0}, "mean_age_ticks": 0.0,
                                                                "channel_mix": {}, "entry_ids": []})


# --- body_of ------------------------------------------------------------------------------------------------------
EDGE_TEXTS = [
    "", "plain body", "\n", "---", "---\n", "---\r\n", "---\n---", "---\n---\n", "---\n---\nbody", "---\n\n---\nbody\n",
    "---\nid: 1\n---\nbody\n---\nmore\n---\n", "---\r\nid: 1\r\n---\r\nbody\r\n", "---\r\r\nid: 1\n---\r\nb", "--- \nid: 1\n---\nx",
    " ---\nid: 1\n---\nx", "x\n---\ny\n---\n", "---\nid: 1\n", "---\nid: 1\n----\nx", "---\nid: 1\n--- \nx\n", "---\nid: 1\n---",
    "---\n[1, 2]\n---\nlist frontmatter", "---\nplain scalar\n---\nb", "---\nid: 1\n---\r",
    "---\nid: 1\n---\n\n", "---\na: '---'\n---\nb", "body\n---\nid: 1\n---\n", "---\nid: [unterminated\nstill: x\n",
]


@pytest.mark.parametrize("text", EDGE_TEXTS)
def test_body_of_edge_cases(text: str) -> None:
    assert body_of(text) == split_frontmatter(text)[1]


@pytest.mark.parametrize("text", ["---\nid: [unterminated\n---\nthe body\n",
                                  "---\nid: 1\n---\r\rX\n---\nthe body\n"])  # YAML reads a lone CR as a line break: two documents
def test_body_of_differs_only_on_a_malformed_block(text: str) -> None:
    assert split_frontmatter(text) == ({}, text)
    assert body_of(text) == "the body\n"


_PIECES = st.sampled_from(["---", "--", "----", "--- ", "\r", "\n", "\r\n", "a", "id: 1", "title: x", ": ", "'", '"', "[", "]", "#",
                           "é", "\U0001f600", " ", "\x85", "\t", " ", "- [[Link]]", "## Related", "yes"])


@settings(max_examples=400, deadline=None)
@given(st.lists(_PIECES, max_size=30).map("".join))
def test_body_of_agrees_with_split_frontmatter_unless_the_yaml_is_malformed(text: str) -> None:
    rest = split_frontmatter(text)[1]
    got = body_of(text)
    if got != rest:  # only allowed when split_frontmatter gave up on a delimited block it could not parse
        assert rest == text and text.endswith(got)
        block = text[: len(text) - len(got)].split("\n")[1:-1]
        with pytest.raises(yaml.YAMLError):
            notes_mod._safe_load("\n".join(block).rstrip("\r"))


_TITLE = st.text(st.characters(blacklist_categories=("Cs",)), max_size=120)


@settings(max_examples=200, deadline=None)
@given(title=_TITLE, body=st.text(st.characters(blacklist_categories=("Cs",)), max_size=200),
       tags=st.lists(_TITLE, max_size=4), path=st.lists(st.text(max_size=12), max_size=3),
       importance=st.floats(allow_nan=False, allow_infinity=False), tick=st.integers(-5, 10**9))
def test_body_of_equals_split_frontmatter_on_rendered_notes(title, body, tags, path, importance, tick) -> None:
    note = Note(note_id="note_x", agent_id="ag_1", title=title, body=body, created_tick=tick, importance=importance, tags=tags,
                provenance=Provenance(source_agent_id="ag_2", hop=1, channel="gossip", path_agents=path))
    text = render_note(note)
    assert body_of(text) == split_frontmatter(text)[1]
    assert render_frontmatter({"title": title, "tags": tags}) == _pure_render({"title": title, "tags": tags})


@pytest.fixture(scope="module")
def r1_world(tmp_path_factory: pytest.TempPathFactory) -> Simulation:
    """Two scripted days of the E0 index-builder arm under R1: kernel notes, index files, backlinks, self files."""
    cfg = load_config(CONFIGS / "exp_mem_e0_index_builder.yaml", {"run": {"days": 2, "ticks_per_day": 12}, "memory": {"index_boost": 0.3}})
    sim = Simulation(cfg, tmp_path_factory.mktemp("r1_world"))
    asyncio.run(sim.run())
    return sim


def test_body_of_equals_split_frontmatter_on_every_file_a_run_writes(r1_world: Simulation) -> None:
    files = sorted(Path(r1_world.run_dir).rglob("*.md"))
    assert len(files) > 50
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert body_of(text) == split_frontmatter(text)[1], path


def test_index_notes_enter_retrieval_in_a_run_under_r1(r1_world: Simulation) -> None:
    mem = r1_world.memory
    agents = [a.agent_id for a in r1_world.registry.alive()]
    with_index = [a for a in agents if any(INDEX_TAG in n.tags for n in mem.list_notes(a))]
    assert with_index, "the index builder keeps index notes"
    for aid in with_index:
        assert mem.retrieve_with_stats(aid, "where is food", r1_world.clock.tick)[1]["index_entries"] >= 1


# --- frontmatter rendering: memoised pure dumper, byte for byte -------------------------------------------------
def _pure_render(mapping: dict[str, Any]) -> str:
    """``render_frontmatter`` as it was: one pure-dumper call per key, no memo."""
    parts = []
    for key, value in mapping.items():
        if isinstance(value, (list, tuple, dict)):
            flow = yaml.safe_dump(list(value) if isinstance(value, tuple) else value, default_flow_style=True, sort_keys=False,
                                  allow_unicode=True, width=1_000_000).strip()
            parts.append(f"{key}: {flow}\n")
        else:
            parts.append(yaml.safe_dump({key: value}, default_flow_style=False, sort_keys=False, allow_unicode=True, width=1_000_000))
    return "---\n" + "".join(parts) + "---\n"


_STRINGS = [
    "", " ", "plain", "The eastern node is thin", "yes: [[odd]] 'title' #1", "colon: inside", "ends with colon:", "a #hash", "#lead",
    "'single'", '"double"', "it's", 'say "hi"', "back\\slash", "tab\there", "line\nbreak", "cr\rhere", "  padded  ", "- dash", "? q",
    "yes", "no", "null", "~", "true", "1.5", "0x1f", "1e3", ".inf", "2026-10-17", "---", "...", "@at", "`tick", "%pct", "!bang", "&amp",
    "*star", "|pipe", ">fold", "{brace}", "[bracket]", "é", "naïve café", "中文标题", "日本語のノート", "Ⅻ", "ﬁ", " nbsp", "​zw",
    "‮rtl", "﻿bom", " ls", " ps", "\x85nel", "emoji \U0001f600", "\U0001d518 fraktur", "🎉🎉", "\x07bell", "\x1besc",
    "\x7fdel", "\x00nul", "T" * 300, "long title " * 120, "ünï " * 400, "x: y " * 200,
]


def _corpus() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, s in enumerate(_STRINGS):
        out.append({"id": f"note_{i:04d}", "title": s, "created_tick": i, "importance": 0.5, "tags": [s] if s else [],
                    "provenance": {"source_agent": s or None, "hop": i % 3, "origin_note": None, "origin_generation": None,
                                   "channel": "gossip", "transfer_tick": None, "path": [s, "ag_1"]}})
    for v in (0.0, -0.0, 1.0, 1, True, False, None, 1e-07, 1e20, 0.1 + 0.2, float("inf"), float("-inf"), float("nan"), -3, 10**15):
        out.append({"importance": v, "created_tick": v, "tags": [], "path": [v], "map": {"k": v}, "tuple": (v, "a")})
    out.append({"tags": [], "path": [], "empty_map": {}, "nested": [[], {}, ["a", ["b"]]], "title": "nested"})
    out.append({"tags": _STRINGS[:20], "title": "many tags"})
    return out


def test_render_frontmatter_is_the_pure_dumper_byte_for_byte() -> None:
    notes_mod._FRAGMENTS.clear()
    corpus = _corpus()
    for mapping in corpus:
        want = _pure_render(mapping)
        assert render_frontmatter(mapping) == want, mapping  # rendered fresh
        assert render_frontmatter(mapping) == want, mapping  # and again from the memo
    # values that compare equal but are written differently never share a memo entry
    assert render_frontmatter({"v": 0.0}) != render_frontmatter({"v": -0.0})
    assert len({render_frontmatter({"v": x}) for x in (1, 1.0, True)}) == 3
    # notes rendered from the memo are byte-identical too, through the parse round trip
    for mapping in corpus[: len(_STRINGS)]:
        note = Note(note_id=mapping["id"], agent_id="ag_1", title=mapping["title"], body="b", created_tick=1, tags=mapping["tags"])
        assert render_note(note) == _pure_render({"id": note.note_id, "title": note.title, "created_tick": 1, "importance": 0.5,
                                                  "tags": note.tags, "provenance": Provenance().to_mapping()}) + "b\n"


def test_render_memo_is_bounded_and_skips_values_it_cannot_freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notes_mod, "_FRAGMENTS_MAX", 4)
    notes_mod._FRAGMENTS.clear()
    for i in range(10):
        render_frontmatter({"n": i})
        assert len(notes_mod._FRAGMENTS) <= 4

    class Str(str):
        pass

    with pytest.raises(yaml.representer.RepresenterError):  # the dumper's own verdict, not a stale memo hit
        render_frontmatter({"title": Str("x")})
    assert ("title", (str, "x")) not in notes_mod._FRAGMENTS


def test_frontmatter_reads_the_same_with_either_loader() -> None:
    """``split_frontmatter`` reads with libyaml when present; on everything the kernel renders it must agree with the
    pure loader (NaN compared by its text, since it never equals itself)."""
    for mapping in _corpus():
        front = render_frontmatter(mapping)
        data, rest = split_frontmatter(front + "body\n")
        pure = yaml.safe_load(front[4:-4])
        assert rest == "body\n" and repr(data) == repr(pure), mapping
    # a lone surrogate (possible in agent text decoded from JSON) is a malformed block, not a crash
    assert split_frontmatter("---\na: \ud800\n---\nb") == ({}, "---\na: \ud800\n---\nb")


# --- provider brains: built only where the run can reach them -------------------------------------------------------
def test_a_scripted_world_imports_no_provider_sdk(tmp_path: Path) -> None:
    """Building the brains of a scripted E0 arm, and then running a few ticks of it, never imports a provider SDK
    (about three seconds of every scripted process before)."""
    code = (
        "import asyncio, sys\n"
        "from void.config import load_config\n"
        "from void.rng import RNG\n"
        "from void.brain.factory import build_brains\n"
        "def sdks():\n"
        "    return sorted(m for m in sys.modules if m == 'anthropic' or m.startswith(('anthropic.', 'google.genai')))\n"
        "cfg = load_config('configs/exp_mem_e0_noop.yaml')\n"
        "brains = build_brains(cfg, RNG(cfg.run.seed), {})\n"
        "assert list(brains) == ['scripted'], list(brains)\n"
        "assert not sdks(), sdks()[:5]\n"
        "from void.sim.loop import Simulation\n"
        "sim = Simulation(cfg, __import__('pathlib').Path(sys.argv[1]))\n"
        "asyncio.run(sim.run(max_ticks=3))\n"
        "assert not sdks(), sdks()[:5]\n"
        "print('ok')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.run([sys.executable, "-c", code, str(tmp_path / "run")], cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=300)
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr[-2000:]


def _cfg(**overrides: Any):
    return load_config(CONFIGS / "base.yaml", overrides)


def test_provider_brains_are_built_for_the_roster_and_on_first_lookup() -> None:
    from void.brain.factory import build_brains

    cfg = _cfg(agents=[{"name": "Ada", "tier": "budget"}, {"name": "Bao", "tier": "scripted"}], population={"tier_mutation_prob": 0.0})
    brains = build_brains(cfg, RNG(1), {}, client=SimpleNamespace())
    assert list(brains) == ["budget", "scripted"]  # config order; frontier and the gemini ladder are not built
    lazy = brains.get("frontier")
    assert lazy is not None and type(lazy).__name__ == "AnthropicBrain" and "frontier" in brains
    assert brains["genius"] is brains.get("genius") and type(brains["genius"]).__name__ == "GeminiBrain"
    assert brains["genius"].semaphore is brains["budget"].semaphore  # one global concurrency limit
    assert brains.get("nope") is None
    with pytest.raises(KeyError):
        brains["nope"]


def test_reachable_tiers_follow_mutation_arrivals_and_writers() -> None:
    from void.brain.factory import build_brains, reachable_tiers

    roster = [{"name": "Ada", "tier": "budget"}]
    assert reachable_tiers(_cfg(agents=roster, population={"tier_mutation_prob": 0.0})) == {"budget"}
    assert reachable_tiers(_cfg(agents=roster)) == {"budget", "frontier"}  # mutation stays within a provider
    ladder = {"ladder": ["genius", "sharp", "average", "slow", "dim"]}
    assert reachable_tiers(_cfg(agents=[{"name": "Ada", "tier": "average"}], intelligence=ladder)) == set(ladder["ladder"])
    assert reachable_tiers(_cfg(agents=[{"name": "Ada", "tier": "average"}], intelligence=ladder,
                                population={"tier_mutation_prob": 0.0})) == {"average"}
    arrival = [{"day": 2, "kind": "arrival", "arrival": {"name": "Neo", "tier": "frontier"}}]
    assert reachable_tiers(_cfg(population={"tier_mutation_prob": 0.0}, epochs=arrival)) == {"scripted", "frontier"}
    assert reachable_tiers(_cfg()) == {"scripted"}  # the scripted roster: no provider tier is reachable
    # an LLM writer may route through any configured provider tier, so every tier is built as before
    cfg = _cfg(chronicle={"writer": "llm"})
    assert list(build_brains(cfg, RNG(1), {}, client=SimpleNamespace())) == list(cfg.tiers)
