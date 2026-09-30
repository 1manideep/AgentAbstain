"""Tests for the chronicle: a template newspaper from public events that never prints agent prose."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from void.agents.models import AgentRecord, PersonalitySeed
from void.agents.registry import AgentRegistry
from void.config import AgentSpec, ChronicleConfig, TierConfig, VoidConfig
from void.culture.chronicle import KV_DAY, KV_HEADLINE, KV_REV, Chronicle, ChronicleDoc
from void.db import Database
from void.events import OPERATOR, Event, EventBus, Kind
from void.types import AgentStatus, Vec2

SECRET_TALK = "SECRET_TALK_TEXT the benefactor pays me"
SECRET_NOTE = "SECRET_NOTE_BODY"
SECRET_THOUGHT = "SECRET_THOUGHT"
SECRET_PITCH = "SECRET_PITCH"
LONG_NAME = "Cy <b>bold</b> with a very long name indeed"


def make_cfg(**chronicle: object) -> VoidConfig:
    return VoidConfig(
        tiers={"s": TierConfig(provider="scripted", model="scripted", price_in_per_mtok=0.0, price_out_per_mtok=0.0)},
        agents=[AgentSpec(name="Ana", tier="s")],
        chronicle=ChronicleConfig(**chronicle),  # type: ignore[arg-type]
    )


@dataclass
class World:
    cfg: VoidConfig
    db: Database
    registry: AgentRegistry
    bus: EventBus
    chronicle: Chronicle
    events: list[Event] = field(default_factory=list)

    def emit(self, day: int, kind: str, payload: dict, agent_id: str | None = None, visibility: str = "public") -> Event:
        return self.bus.emit(Event(day * 24, day, kind, payload=payload, agent_id=agent_id, visibility=visibility))

    def kinds(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]


def add_agent(reg: AgentRegistry, agent_id: str, name: str) -> None:
    reg.insert(
        AgentRecord(
            agent_id, name, "s", 1_000_000, PersonalitySeed(), f"vaults/{agent_id}", 0, AgentStatus.ALIVE,
            Vec2(0.0, 0.0), 0.0, 50.0, 0.0, False, 0,
        )
    )


def build(tmp_path: Path, llm_writer=None, **chronicle: object) -> World:
    cfg = make_cfg(**chronicle)
    db = Database(tmp_path / "world.db")
    registry = AgentRegistry(db)
    bus = EventBus(persist=db.persist_event)
    world = World(cfg, db, registry, bus, Chronicle(cfg, db, bus, registry, tmp_path / "chronicle", llm_writer))
    bus.subscribe(world.events.append)
    add_agent(registry, "ag_ana", "Ana")
    add_agent(registry, "ag_bao", "Bao")
    add_agent(registry, "ag_cy", LONG_NAME)
    return world


def emit_day(w: World, day: int = 1) -> None:
    w.emit(day, Kind.BIRTH, {"agent_id": "ag_cy", "name": LONG_NAME, "tier": "budget", "generation": 1,
                             "parent_id": "ag_ana", "kind": "child", "endowment_usd": 0.75})
    w.emit(day, Kind.DEATH, {"agent_id": "ag_bao", "name": "Bao", "tier": "frontier", "generation": 0, "cause": "bankrupt"})
    w.emit(day, Kind.FORAGE, {"agent_id": "ag_ana", "node_id": "n1", "units": 1.0, "usd": 0.02}, agent_id="ag_ana")
    w.emit(day, Kind.FORAGE, {"agent_id": "ag_ana", "node_id": "n1", "units": 1.0, "usd": 0.03}, agent_id="ag_ana")
    w.emit(day, Kind.FORAGE, {"agent_id": "ag_bao", "node_id": "n2", "units": 1.0, "usd": 0.04}, agent_id="ag_bao")
    w.emit(day, Kind.TRANSFER, {"from": "ag_ana", "to": "ag_bao", "amount_usd": 0.10})
    w.emit(day, Kind.WINDFALL, {"agent_id": "ag_ana", "amount_usd": 0.50}, agent_id="ag_ana")
    w.emit(day, Kind.BENEFACTOR_GRANT, {"agent_id": "ag_ana", "amount_usd": 0.50, "source": "benefactor",
                                        "reason": "the benefactor smiled"}, visibility=OPERATOR)
    w.emit(day, Kind.WEATHER, {"value": -0.2, "label": "rain"})
    w.emit(day, Kind.WEATHER, {"value": 0.3, "label": "clear"})
    w.emit(day, Kind.EPOCH, {"kind": "storm", "scarcity": 0.5, "duration_days": 2})
    w.emit(day, Kind.GADGET_VERIFIED, {"gadget_id": "g1", "name": "Rain <catcher>", "owner": "ag_ana", "stage": "tests", "reason": "ok"})
    w.emit(day, Kind.GADGET_VERIFIED, {"gadget_id": "g2", "name": "Windmill", "owner": "ag_bao", "stage": "tests", "reason": "ok"})
    w.emit(day, Kind.GADGET_REJECTED, {"gadget_id": "g3", "name": "Bomb", "owner": "ag_bao", "stage": "static_check",
                                       "reason": "SECRET_REASON import os"})
    w.emit(day, Kind.TASK_POSTED, {"task_id": "t1", "title": "Map the north", "reward_usd": 0.30})
    w.emit(day, Kind.TASK_ASSIGNED, {"task_id": "t1", "agent_id": "ag_ana"})
    w.emit(day, Kind.TASK_COMPLETED, {"task_id": "t1", "agent_id": "ag_ana", "reward_usd": 0.30})
    w.emit(day, Kind.APPLY_TASK, {"task_id": "t1", "agent_id": "ag_ana", "pitch": SECRET_PITCH}, agent_id="ag_ana")
    w.emit(day, Kind.DEGENERATION, {"mode": "garble", "t_eff": 1.9, "t_c": 1.8, "tier": "budget", "generation": 1, "agent_name": "Cy"})
    w.emit(day, Kind.DEGENERATION, {"mode": "perseverate", "t_eff": 1.9, "t_c": 1.8, "tier": "frontier", "generation": 0, "agent_name": "Ana"})
    w.emit(day, Kind.TALK, {"speaker_id": "ag_ana", "listener_id": "ag_bao", "text": SECRET_TALK}, agent_id="ag_ana")
    w.emit(day, Kind.TALK, {"speaker_id": "ag_bao", "listener_id": "ag_ana", "text": SECRET_TALK}, agent_id="ag_bao")
    w.emit(day, Kind.GOSSIP_TRANSFER, {"speaker_id": "ag_ana", "listener_id": "ag_bao", "hop": 1,
                                       "note_title": SECRET_NOTE, "similarity": 0.9}, agent_id="ag_bao")
    w.emit(day, Kind.REMEMBER, {"agent_id": "ag_ana", "title": SECRET_NOTE, "text": SECRET_NOTE}, agent_id="ag_ana")
    w.emit(day, Kind.LLM_CALL, {"agent_id": "ag_ana", "thought": SECRET_THOUGHT}, agent_id="ag_ana")
    # The next day's events must not bleed into this day's paper.
    w.emit(day + 1, Kind.DEATH, {"agent_id": "ag_ana", "name": "Ana", "tier": "frontier", "generation": 0, "cause": "bankrupt"})


def all_text(w: World, doc: ChronicleDoc) -> str:
    rows = w.db.fetchall("SELECT text FROM chronicle_items")
    return "\n".join([doc.headline, doc.markdown, *(i[3] for i in doc.items), *(str(r["text"]) for r in rows)])


# --- writing ------------------------------------------------------------------------------


async def test_write_day_writes_file_items_kv_and_event(tmp_path: Path) -> None:
    w = build(tmp_path)
    emit_day(w, 1)
    doc = await w.chronicle.write_day(1, tick=48)

    path = tmp_path / "chronicle" / "day_001.md"
    assert path.is_file() and path.read_text(encoding="utf-8") == doc.markdown
    assert doc.day == 1
    assert doc.headline.startswith("Day 1: Bao is gone; a storm broke; ")
    assert doc.markdown.splitlines()[0] == "# The Void Chronicle, Day 1"
    assert doc.headline in doc.markdown
    for section in ("Births and deaths", "Fortunes", "Weather and land", "Works", "Board", "Tempers", "Voices"):
        assert f"## {section}" in doc.markdown

    # Every line is an item with a sequential id, mirrored in chronicle_items.
    rows = w.db.fetchall("SELECT item_id, kind, event_seq, text FROM chronicle_items WHERE day=1 ORDER BY rowid")
    assert [(r["item_id"], r["kind"], r["event_seq"], r["text"]) for r in rows] == doc.items
    assert [i[0] for i in doc.items] == [f"d1-{n}" for n in range(1, len(doc.items) + 1)]
    assert doc.items[0] == ("d1-1", "headline", None, doc.headline)
    assert {"death", "birth", "fortune", "weather", "epoch", "work", "board", "temper", "voice"} <= {i[1] for i in doc.items}
    for _, line in zip(doc.items, doc.markdown.splitlines(), strict=False):
        assert "\n" not in line

    death = next(i for i in doc.items if i[1] == "death")
    assert death[3] == "Bao (frontier, generation 0) died: bankrupt."
    assert death[2] == w.kinds(Kind.DEATH)[0].seq
    birth = next(i for i in doc.items if i[1] == "birth")
    assert birth[3].endswith("(budget, generation 1) was born to Ana with $0.75.")
    assert "Ana foraged most: $0.05 over 2 trips ($0.09 gathered in all)." in doc.markdown
    assert "1 transfer moved $0.10 between agents." in doc.markdown
    assert "1 windfall recorded, $0.50 in all." in doc.markdown
    assert "Weather turned from rain to clear (from -0.20 to +0.30)." in doc.markdown
    assert "A storm epoch began (scarcity 0.50, 2 days)." in doc.markdown
    assert "2 gadgets stood: Rain ＜catcher＞ (Ana), Windmill (Bao)." in doc.markdown
    assert "1 gadget failed: Bomb at static_check." in doc.markdown
    assert "Posted: Map the north for $0.30." in doc.markdown
    assert "Ana took Map the north." in doc.markdown
    assert "Ana completed Map the north for $0.30." in doc.markdown
    assert "2 tempers frayed: 1 budget, 1 frontier." in doc.markdown
    assert "2 conversations among 2 voices; 1 rumour changed hands." in doc.markdown
    assert "Ana (frontier" not in doc.markdown  # day 2's death

    assert w.db.kv_get(KV_HEADLINE) == doc.headline
    assert w.db.kv_get(KV_DAY) == 1 and w.db.kv_get(KV_REV) == 1
    ev = w.kinds(Kind.CHRONICLE)
    assert len(ev) == 1 and ev[0].tick == 48 and ev[0].day == 1 and ev[0].seq is not None
    assert ev[0].payload["headline"] == doc.headline and ev[0].payload["item_count"] == len(doc.items)
    assert ev[0].payload["path"] == f"chronicle/{path.name}" and ev[0].payload["day"] == 1

    # Rewriting the same day replaces its items and bumps the revision.
    again = await w.chronicle.write_day(1, tick=49)
    assert again.items == doc.items and again.headline == doc.headline
    assert w.db.kv_get(KV_REV) == 2
    assert w.db.fetchone("SELECT COUNT(*) AS n FROM chronicle_items WHERE day=1")["n"] == len(doc.items)


async def test_chronicle_never_prints_prose_or_the_benefactor(tmp_path: Path) -> None:
    w = build(tmp_path)
    emit_day(w, 1)
    doc = await w.chronicle.write_day(1, tick=48)
    text = all_text(w, doc)
    for secret in ("SECRET", SECRET_TALK, SECRET_NOTE, SECRET_THOUGHT, SECRET_PITCH, "import os"):
        assert secret not in text
    assert "benefactor" not in text.lower()
    assert "windfall" in text  # the windfall itself is public; its source is not


async def test_names_are_sanitized_and_capped(tmp_path: Path) -> None:
    w = build(tmp_path)
    emit_day(w, 1)
    doc = await w.chronicle.write_day(1, tick=48)
    text = all_text(w, doc)
    assert "<b>" not in text and "<catcher>" not in text and "very long name" not in text
    birth = next(i for i in doc.items if i[1] == "birth")[3]
    name = birth.split(" (budget", 1)[0]
    assert len(name) <= 20 and name.startswith("Cy ＜b＞bold")


async def test_headline_latest_and_read(tmp_path: Path) -> None:
    w = build(tmp_path)
    emit_day(w, 1)
    assert w.chronicle.latest() is None and w.chronicle.headline() is None and w.chronicle.read(1) is None
    doc = await w.chronicle.write_day(1, tick=48)
    assert w.chronicle.headline() == doc.headline
    assert w.chronicle.read(1) == doc.markdown and w.chronicle.read(2) is None
    assert w.chronicle.latest() == doc
    quiet = await w.chronicle.write_day(2, tick=72)
    assert w.chronicle.latest() == quiet and w.chronicle.headline() == quiet.headline
    assert w.chronicle.read(1) == doc.markdown


async def test_quiet_day_still_has_a_paper(tmp_path: Path) -> None:
    w = build(tmp_path)
    doc = await w.chronicle.write_day(3, tick=96)
    assert doc.headline == "Day 3: a quiet day"
    assert "Nothing of note was recorded." in doc.markdown
    assert [i[1] for i in doc.items] == ["headline", "quiet"]
    assert (tmp_path / "chronicle" / "day_003.md").is_file()


async def test_disabled_chronicle_does_nothing(tmp_path: Path) -> None:
    w = build(tmp_path, enabled=False)
    emit_day(w, 1)
    doc = await w.chronicle.write_day(1, tick=48)
    assert doc == ChronicleDoc(day=1, headline="", markdown="", items=[])
    assert not (tmp_path / "chronicle").exists()
    assert w.db.fetchone("SELECT COUNT(*) AS n FROM chronicle_items")["n"] == 0
    assert w.db.kv_get(KV_HEADLINE) is None and w.chronicle.latest() is None and w.chronicle.headline() is None
    assert w.kinds(Kind.CHRONICLE) == []


async def test_llm_writer_returning_none_falls_back_to_template(tmp_path: Path) -> None:
    seen: list[str] = []

    async def gated(markdown: str) -> str | None:
        seen.append(markdown)
        return None

    w = build(tmp_path / "llm", gated, writer="llm")
    emit_day(w, 1)
    doc = await w.chronicle.write_day(1, tick=48)
    ref = build(tmp_path / "ref")
    emit_day(ref, 1)
    ref_doc = await ref.chronicle.write_day(1, tick=48)
    assert seen == [ref_doc.markdown]
    assert doc == ref_doc
    assert w.chronicle.read(1) == ref_doc.markdown


async def test_llm_writer_output_replaces_prose_but_keeps_headline_and_items(tmp_path: Path) -> None:
    async def press(markdown: str) -> str | None:
        return "  Rewritten by the press.  "

    w = build(tmp_path, press, writer="llm")
    emit_day(w, 1)
    doc = await w.chronicle.write_day(1, tick=48)
    assert doc.markdown == "Rewritten by the press.\n"
    assert doc.headline.startswith("Day 1: Bao is gone") and len(doc.items) > 1
    assert w.chronicle.read(1) == doc.markdown and w.chronicle.headline() == doc.headline


async def test_template_writer_never_calls_the_llm(tmp_path: Path) -> None:
    called: list[str] = []

    async def press(markdown: str) -> str | None:
        called.append(markdown)
        return "x"

    w = build(tmp_path, press)
    emit_day(w, 1)
    doc = await w.chronicle.write_day(1, tick=48)
    assert called == [] and doc.markdown != "x"


async def test_tolerates_malformed_and_sparse_payloads(tmp_path: Path) -> None:
    w = build(tmp_path)
    w.db.execute(
        "INSERT INTO events(tick, day, kind, agent_id, payload, visibility) VALUES(?,?,?,?,?,?)",
        (24, 1, Kind.DEATH, None, "not json", "public"),
    )
    w.db.execute(
        "INSERT INTO events(tick, day, kind, agent_id, payload, visibility) VALUES(?,?,?,?,?,?)",
        (24, 1, Kind.BIRTH, None, "[1, 2]", "public"),
    )
    for kind in (Kind.FORAGE, Kind.TRANSFER, Kind.WINDFALL, Kind.WEATHER, Kind.EPOCH, Kind.GADGET_VERIFIED,
                 Kind.GADGET_REJECTED, Kind.TASK_POSTED, Kind.TASK_ASSIGNED, Kind.DEGENERATION, Kind.TALK):
        w.emit(1, kind, {})
    w.emit(1, Kind.FORAGE, {"agent_id": "ag_ana", "usd": "lots"})
    w.emit(1, Kind.TASK_COMPLETED, {"task_id": "zzz", "agent_id": "ag_ghost", "reward_usd": None})
    w.emit(1, Kind.BIRTH, {"name": "  ", "generation": "two", "endowment_usd": float("nan")})
    doc = await w.chronicle.write_day(1, tick=48)
    assert len(doc.items) > 2
    assert "someone (unknown tier, generation ?) died." in doc.markdown
    assert "ag_ghost completed zzz for $0.00." in doc.markdown
    assert w.chronicle.latest() == doc
