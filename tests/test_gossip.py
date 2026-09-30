"""Tests for gossip: the scripted paraphrase and note transfer with provenance."""

from __future__ import annotations

import dataclasses
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from void.agents.models import AgentRecord, PersonalitySeed
from void.agents.registry import AgentRegistry
from void.config import AgentSpec, GossipConfig, TierConfig, VoidConfig
from void.culture.gossip import GOSSIP_PARAPHRASE_DOWNGRADED, Gossip, paraphrase_scripted
from void.db import Database
from void.events import Event, EventBus, Kind
from void.ids import IdFactory
from void.memory.embed import HashingEmbedder
from void.memory.notes import Note, Provenance
from void.memory.store import MemoryStore
from void.rng import RNG
from void.types import AgentStatus, Vec2

PROV_FIELDS = {f.name for f in dataclasses.fields(Provenance)}
SAMPLE = "The [[Node north]] is rich in the morning; [[Bao]] said the east node was thin and the weather cold."
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


def make_cfg(**gossip: object) -> VoidConfig:
    return VoidConfig(
        tiers={"s": TierConfig(provider="scripted", model="scripted", price_in_per_mtok=0.0, price_out_per_mtok=0.0)},
        agents=[AgentSpec(name="Ana", tier="s")],
        gossip=GossipConfig(**gossip),  # type: ignore[arg-type]
    )


@dataclass
class World:
    cfg: VoidConfig
    db: Database
    registry: AgentRegistry
    memory: MemoryStore
    rng: RNG
    bus: EventBus
    gossip: Gossip
    events: list[Event] = field(default_factory=list)

    def kinds(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]

    def notes(self, agent_id: str) -> list[Note]:
        """Live notes minus entity stubs the store may create for wikilinks."""
        return [n for n in self.memory.list_notes(agent_id) if "entity" not in n.tags]


def add_agent(
    reg: AgentRegistry, agent_id: str, name: str, generation: int = 0, status: AgentStatus = AgentStatus.ALIVE
) -> None:
    reg.insert(
        AgentRecord(
            agent_id, name, "s", 1_000_000, PersonalitySeed(), f"vaults/{agent_id}", generation, status,
            Vec2(0.0, 0.0), 0.0, 50.0, 0.0, False, 0,
        )
    )


def build(tmp_path: Path, paraphraser=None, *, seed: int = 42, **gossip: object) -> World:
    cfg = make_cfg(**gossip)
    db = Database(tmp_path / "world.db")
    registry = AgentRegistry(db)
    memory = MemoryStore(db, cfg.memory, IdFactory("run"), tmp_path / "vaults", HashingEmbedder(64))
    bus = EventBus(persist=db.persist_event)
    rng = RNG(seed)
    world = World(cfg, db, registry, memory, rng, bus, Gossip(cfg, memory, registry, rng, bus, paraphraser))
    bus.subscribe(world.events.append)
    add_agent(registry, "ag_ana", "Ana", generation=0)
    add_agent(registry, "ag_bao", "Bao", generation=1)
    add_agent(registry, "ag_cy", "Cy", generation=2)
    add_agent(registry, "ag_dead", "Dov", status=AgentStatus.ARCHIVED)
    return world


# --- paraphrase_scripted ------------------------------------------------------------------


def test_paraphrase_zero_rate_is_identity() -> None:
    for seed in range(5):
        assert paraphrase_scripted(SAMPLE, 0.0, random.Random(seed)) == SAMPLE
    assert paraphrase_scripted("", 0.5, random.Random(1)) == ""


@pytest.mark.parametrize("rate", [0.15, 0.5, 1.0])
def test_paraphrase_is_deterministic_and_preserves_wikilinks(rate: float) -> None:
    outputs: list[str] = []
    for seed in range(30):
        a = paraphrase_scripted(SAMPLE, rate, random.Random(seed))
        b = paraphrase_scripted(SAMPLE, rate, random.Random(seed))
        assert a == b
        assert _WIKILINK_RE.findall(a) == ["Node north", "Bao"]
        assert 0.7 * len(SAMPLE) <= len(a) <= 1.3 * len(SAMPLE)
        outputs.append(a)
    assert any(o != SAMPLE for o in outputs)
    # different seeds give different rumours (it is a random perturbation, not a fixed rewrite)
    assert len(set(outputs)) > 1


def test_paraphrase_skips_stop_words_and_honours_length_bound_on_short_text() -> None:
    assert paraphrase_scripted("the of and", 1.0, random.Random(1)) == "the of and"
    assert paraphrase_scripted("[[Only a link]]", 1.0, random.Random(1)) == "[[Only a link]]"
    for seed in range(10):
        out = paraphrase_scripted("rich", 1.0, random.Random(seed))
        assert 0.7 * 4 <= len(out) <= 1.3 * 4


def test_paraphrase_synonyms_keep_case_and_punctuation() -> None:
    changed = {paraphrase_scripted("Rich.", 1.0, random.Random(seed)) for seed in range(20)}
    assert all(o.endswith(".") and o[0].isupper() for o in changed)


# --- Gossip -------------------------------------------------------------------------------


async def test_talk_share_copies_note_with_provenance_and_does_not_duplicate(tmp_path: Path) -> None:
    w = build(tmp_path, share_probability_on_talk=1.0)
    src = w.memory.remember(
        "ag_ana", 3, "The north node is rich in the morning, go there first.",
        title="North node", importance=0.9, tags=["resources"],
    )
    w.memory.remember("ag_ana", 3, "I am Ana, a careful forager.", title="Ana", importance=1.0, tags=["self"])

    w.gossip.queue("ag_ana", "ag_bao", 5)
    assert w.gossip.pending == 1
    payloads = await w.gossip.flush(5, 0)
    assert w.gossip.pending == 0
    assert len(payloads) == 1
    p = payloads[0]

    copies = w.notes("ag_bao")
    assert len(copies) == 1
    copy = copies[0]
    assert copy.title == "North node" and copy.note_id == p["new_note_id"]
    assert copy.body != src.body  # paraphrased
    assert "gossip" in copy.tags and "resources" in copy.tags
    prov = copy.provenance
    assert prov.hop == 1 and prov.source_agent_id == "ag_ana"
    assert prov.origin_note_id == src.note_id and prov.origin_generation == 0
    if "channel" in PROV_FIELDS:
        assert prov.channel == "gossip"
    if "transfer_tick" in PROV_FIELDS:
        assert prov.transfer_tick == 5
    if "path_agents" in PROV_FIELDS:
        assert list(prov.path_agents) == ["ag_ana"]

    events = w.kinds(Kind.GOSSIP_TRANSFER)
    assert len(events) == 1
    ev = events[0]
    assert ev.agent_id == "ag_bao" and ev.seq is not None and ev.tick == 5 and ev.payload == p
    assert p["speaker_id"] == "ag_ana" and p["listener_id"] == "ag_bao" and p["hop"] == 1
    assert p["origin_note_id"] == src.note_id and p["origin_agent_id"] == "ag_ana"
    assert p["origin_generation"] == 0 and p["note_title"] == "North node"
    assert 0.0 <= p["similarity"] <= 1.0

    # A second share of the same origin is skipped: Bao already holds it.
    w.gossip.queue("ag_ana", "ag_bao", 6)
    assert await w.gossip.flush(6, 0) == []
    assert len(w.notes("ag_bao")) == 1 and len(w.kinds(Kind.GOSSIP_TRANSFER)) == 1


async def test_share_probability_zero_never_queues(tmp_path: Path) -> None:
    w = build(tmp_path, share_probability_on_talk=0.0)
    w.memory.remember("ag_ana", 1, "The node is rich.", importance=0.8)
    w.gossip.queue("ag_ana", "ag_bao", 2)
    assert w.gossip.pending == 0
    assert await w.gossip.flush(2, 0) == []


def test_share_draw_is_deterministic_per_seed(tmp_path: Path) -> None:
    a = build(tmp_path / "a", share_probability_on_talk=0.5)
    b = build(tmp_path / "b", share_probability_on_talk=0.5)
    c = build(tmp_path / "c", seed=7, share_probability_on_talk=0.5)
    pairs = [("ag_ana", "ag_bao"), ("ag_bao", "ag_cy"), ("ag_cy", "ag_ana")]
    counts = []
    for world in (a, b, c):
        seen = []
        for tick in range(40):
            for speaker, listener in pairs:
                before = world.gossip.pending
                world.gossip.queue(speaker, listener, tick)
                seen.append(world.gossip.pending > before)
        counts.append(seen)
    assert counts[0] == counts[1]
    assert 0 < sum(counts[0]) < len(counts[0])
    assert counts[0] != counts[2]


async def test_share_to_dead_or_unknown_agent_is_skipped(tmp_path: Path) -> None:
    w = build(tmp_path, share_probability_on_talk=1.0)
    w.memory.remember("ag_ana", 1, "The west node is barren.", importance=0.8)
    w.gossip.queue("ag_ana", "ag_dead", 2)
    w.gossip.queue("ag_ana", "ag_nobody", 2)
    w.gossip.queue("ag_nobody", "ag_bao", 2)
    assert w.gossip.pending == 3
    assert await w.gossip.flush(2, 0) == []
    assert w.notes("ag_dead") == [] and w.notes("ag_bao") == []
    assert w.kinds(Kind.GOSSIP_TRANSFER) == []


async def test_forced_share_with_title_picks_that_note(tmp_path: Path) -> None:
    w = build(tmp_path, share_probability_on_talk=0.0)
    w.memory.remember("ag_ana", 1, "The north node is rich.", title="North node", importance=0.9)
    w.memory.remember("ag_ana", 1, "Storms come from the west at dusk.", title="Weather lore", importance=0.3)

    w.gossip.queue("ag_ana", "ag_bao", 2, note_title="  weather LORE ", forced=True)
    payloads = await w.gossip.flush(2, 0)
    assert [p["note_title"] for p in payloads] == ["Weather lore"]
    assert [n.title for n in w.notes("ag_bao")] == ["Weather lore"]

    # An unknown title falls back to the most important shareable note.
    w.gossip.queue("ag_ana", "ag_cy", 3, note_title="no such note", forced=True)
    payloads = await w.gossip.flush(3, 0)
    assert [p["note_title"] for p in payloads] == ["North node"]


async def test_second_hop_keeps_origin_and_extends_path(tmp_path: Path) -> None:
    w = build(tmp_path, share_probability_on_talk=1.0, mutation_rate=0.0)
    src = w.memory.remember("ag_ana", 1, "Keep to the east node.", title="East", importance=0.9)
    w.gossip.queue("ag_ana", "ag_bao", 2)
    assert len(await w.gossip.flush(2, 0)) == 1
    w.gossip.queue("ag_bao", "ag_cy", 3)
    payloads = await w.gossip.flush(3, 0)
    assert len(payloads) == 1
    p = payloads[0]
    assert p["hop"] == 2 and p["origin_note_id"] == src.note_id
    assert p["origin_agent_id"] == "ag_ana" and p["origin_generation"] == 0
    assert p["similarity"] == pytest.approx(1.0)  # no mutation: identical to the origin

    cy = w.notes("ag_cy")[0]
    assert cy.provenance.hop == 2 and cy.provenance.source_agent_id == "ag_bao"
    assert cy.provenance.origin_note_id == src.note_id
    if "path_agents" in PROV_FIELDS:
        assert list(cy.provenance.path_agents) == ["ag_ana", "ag_bao"]

    # Nobody can gossip Ana's own note back to her, and Bao has nothing else to tell Cy.
    w.gossip.queue("ag_bao", "ag_ana", 4, forced=True)
    w.gossip.queue("ag_cy", "ag_ana", 4, forced=True)
    w.gossip.queue("ag_bao", "ag_cy", 4, forced=True)
    assert await w.gossip.flush(4, 0) == []


async def test_self_and_entity_notes_are_never_shared(tmp_path: Path) -> None:
    w = build(tmp_path, share_probability_on_talk=1.0)
    w.memory.remember("ag_ana", 1, "I am Ana.", title="Ana", importance=1.0, tags=["self"])
    w.memory.remember("ag_ana", 1, "Node n1", title="Node n1", importance=1.0, tags=["entity"])
    w.gossip.queue("ag_ana", "ag_bao", 2)
    w.gossip.queue("ag_ana", "ag_cy", 2, note_title="Ana", forced=True)
    assert await w.gossip.flush(2, 0) == []
    assert w.notes("ag_bao") == [] and w.notes("ag_cy") == []


async def test_llm_paraphraser_returning_none_downgrades_to_scripted(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []

    async def gated(listener_id: str, text: str, tick: int) -> str | None:
        calls.append((listener_id, tick))
        return None

    w = build(tmp_path, gated, share_probability_on_talk=1.0, paraphrase="llm")
    src = w.memory.remember("ag_ana", 1, "The north node is rich in the morning.", importance=0.9)
    w.gossip.queue("ag_ana", "ag_bao", 2)
    payloads = await w.gossip.flush(2, 0)
    assert len(payloads) == 1 and calls == [("ag_bao", 2)]
    down = w.kinds(GOSSIP_PARAPHRASE_DOWNGRADED)
    assert len(down) == 1 and down[0].payload["reason"] == "gated" and down[0].agent_id == "ag_bao"
    assert w.notes("ag_bao")[0].body != src.body  # scripted fallback still perturbs


async def test_llm_paraphraser_text_is_used_within_per_tick_budget(tmp_path: Path) -> None:
    async def writer(listener_id: str, text: str, tick: int) -> str | None:
        return f"Rumour for {listener_id}: {text}"

    w = build(tmp_path, writer, share_probability_on_talk=1.0, paraphrase="llm", max_llm_paraphrases_per_tick=1)
    w.memory.remember("ag_ana", 1, "The west node is barren.", importance=0.9)
    w.gossip.queue("ag_ana", "ag_bao", 2)
    w.gossip.queue("ag_ana", "ag_cy", 2)
    assert len(await w.gossip.flush(2, 0)) == 2
    assert w.notes("ag_bao")[0].body.startswith("Rumour for ag_bao:")
    assert not w.notes("ag_cy")[0].body.startswith("Rumour")
    assert [e.payload["reason"] for e in w.kinds(GOSSIP_PARAPHRASE_DOWNGRADED)] == ["tick_budget"]
    # The budget is per flush.
    w.gossip.queue("ag_bao", "ag_cy", 3, forced=True)
    w.gossip.queue("ag_cy", "ag_bao", 3, forced=True)
    payloads = await w.gossip.flush(3, 0)
    assert [p["listener_id"] for p in payloads] == []  # both already hold Ana's origin


async def test_gossip_disabled_is_a_noop(tmp_path: Path) -> None:
    w = build(tmp_path, enabled=False, share_probability_on_talk=1.0)
    w.memory.remember("ag_ana", 1, "The west node is barren.", importance=0.9)
    w.gossip.queue("ag_ana", "ag_bao", 2, forced=True)
    assert w.gossip.pending == 0
    assert await w.gossip.flush(2, 0) == []
    assert w.notes("ag_bao") == [] and w.events == []


async def test_flush_is_deterministic_across_runs(tmp_path: Path) -> None:
    results = []
    for name in ("a", "b"):
        w = build(tmp_path / name, share_probability_on_talk=1.0, mutation_rate=0.5)
        w.memory.remember("ag_ana", 1, SAMPLE, title="Lore", importance=0.9)
        w.memory.remember("ag_bao", 1, "Storms come from the west at dusk and the cash runs thin.", importance=0.7)
        w.gossip.queue("ag_ana", "ag_bao", 2)
        w.gossip.queue("ag_bao", "ag_cy", 2)
        payloads = await w.gossip.flush(2, 0)
        results.append((payloads, [n.body for n in w.notes("ag_bao")], [n.body for n in w.notes("ag_cy")]))
    assert results[0] == results[1]
    assert len(results[0][0]) == 2
