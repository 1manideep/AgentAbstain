"""Chronicle: the Void's daily newspaper (DESIGN §11.2).

At the end of each day :meth:`Chronicle.write_day` reads the *public* events of the day
that ended and prints a template newspaper of sanitized names and numbers. Agent prose
(talk text, note bodies, pitches, thoughts) never reaches the page: the aggregator reads
only the payload keys listed in DESIGN §15, and every name, title and label passes through
:func:`sanitize_text` with a length cap. Operator-visibility events, including the
benefactor's grants (§9.5), are never read, so the chronicle cannot reveal them.

Each line gets an item id ``d<day>-<n>`` stored in ``chronicle_items``; the headline goes to
``world_kv`` so it can appear in every observation the next day. An optional LLM writer may
rewrite the template prose; when it returns ``None`` the template stands.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from void.agents.registry import AgentRegistry
from void.brain.decision import sanitize_text
from void.config import VoidConfig
from void.db import Database
from void.events import PUBLIC, Event, EventBus, Kind

__all__ = ["Chronicle", "ChronicleDoc", "KV_HEADLINE", "KV_DAY", "KV_REV", "TITLE"]

KV_HEADLINE = "chronicle_headline"
KV_DAY = "chronicle_day"
KV_REV = "chronicle_rev"
TITLE = "The Void Chronicle"
NAME_LEN = 20
LABEL_LEN = 40

LlmWriter = Callable[[str], Awaitable[str | None]]
Item = tuple[str, str, int | None, str]

_SECTIONS = ("Births and deaths", "Fortunes", "Weather and land", "Works", "Board", "Tempers", "Voices", "Notes")
_NUMBER_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
_EPOCH_PHRASES = {
    "drought": "a drought set in",
    "storm": "a storm broke",
    "boom": "a boom began",
    "arrival": "a stranger arrived",
}
_ITEM_NUM_RE = re.compile(r"-(\d+)$")


@dataclass
class ChronicleDoc:
    day: int
    headline: str
    markdown: str
    items: list[Item] = field(default_factory=list)


# --- sanitizing helpers -------------------------------------------------------------------


def _clean(value: Any, max_len: int = NAME_LEN, default: str = "someone") -> str:
    if value is None:
        return default
    return sanitize_text(str(value), single_line=True, max_len=max_len).strip() or default


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default  # NaN guard


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usd(value: Any) -> str:
    return f"${_num(value):.2f}"


def _count(n: int, singular: str, plural: str | None = None) -> str:
    word = singular if n == 1 else (plural or singular + "s")
    return f"{n} {word}"


def _count_words(n: int, singular: str, plural: str | None = None) -> str:
    word = singular if n == 1 else (plural or singular + "s")
    number = _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)
    return f"{number} {word}"


def _payload(row: Any) -> dict[str, Any]:
    try:
        data = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --- day aggregation ----------------------------------------------------------------------


class _DayBuilder:
    """Consumes the day's public events and produces headline, sections and items."""

    def __init__(self, day: int, names: dict[str, str], task_titles: dict[str, str]) -> None:
        self.day = day
        self.names = names
        self.task_titles = task_titles
        self.deaths: list[tuple[int, dict[str, Any]]] = []
        self.births: list[tuple[int, dict[str, Any]]] = []
        self.forage_usd: Counter[str] = Counter()
        self.forage_trips: Counter[str] = Counter()
        self.transfer_total = 0.0
        self.transfer_count = 0
        self.windfall_total = 0.0
        self.windfall_count = 0
        self.weather: list[tuple[float, str]] = []
        self.epochs: list[tuple[int, dict[str, Any]]] = []
        self.verified: list[tuple[int, dict[str, Any]]] = []
        self.rejected: list[tuple[int, dict[str, Any]]] = []
        self.posted: list[tuple[int, dict[str, Any]]] = []
        self.assigned: list[tuple[int, dict[str, Any]]] = []
        self.completed: list[tuple[int, dict[str, Any]]] = []
        self.degen_total = 0
        self.degen_by_tier: Counter[str] = Counter()
        self.talks = 0
        self.speakers: set[str] = set()
        self.gossip = 0
        self._items: list[Item] = []
        self._sections: dict[str, list[str]] = {s: [] for s in _SECTIONS}

    # --- input -----------------------------------------------------------------------
    def consume(self, seq: int, kind: str, agent_id: str | None, p: dict[str, Any]) -> None:
        if kind == Kind.DEATH:
            self.deaths.append((seq, p))
        elif kind == Kind.BIRTH:
            self.births.append((seq, p))
        elif kind == Kind.FORAGE:
            who = str(p.get("agent_id") or agent_id or "")
            self.forage_usd[who] += _num(p.get("usd"))
            self.forage_trips[who] += 1
        elif kind == Kind.TRANSFER:
            self.transfer_total += _num(p.get("amount_usd"))
            self.transfer_count += 1
        elif kind == Kind.WINDFALL:
            self.windfall_total += _num(p.get("amount_usd"))
            self.windfall_count += 1
        elif kind == Kind.WEATHER:
            self.weather.append((_num(p.get("value")), _clean(p.get("label"), default="unsettled")))
        elif kind == Kind.EPOCH:
            self.epochs.append((seq, p))
        elif kind == Kind.GADGET_VERIFIED:
            self.verified.append((seq, p))
        elif kind == Kind.GADGET_REJECTED:
            self.rejected.append((seq, p))
        elif kind == Kind.TASK_POSTED:
            self.posted.append((seq, p))
            if p.get("task_id") and p.get("title"):
                self.task_titles.setdefault(str(p["task_id"]), _clean(p["title"], LABEL_LEN, default="a task"))
        elif kind == Kind.TASK_ASSIGNED:
            self.assigned.append((seq, p))
        elif kind == Kind.TASK_COMPLETED:
            self.completed.append((seq, p))
        elif kind == Kind.DEGENERATION:
            self.degen_total += 1
            self.degen_by_tier[_clean(p.get("tier"), default="unknown")] += 1
        elif kind == Kind.TALK:
            self.talks += 1
            speaker = p.get("speaker_id") or agent_id
            if speaker:
                self.speakers.add(str(speaker))
        elif kind == Kind.GOSSIP_TRANSFER:
            self.gossip += 1
        # every other kind (remember, llm_call, claim, ...) is ignored: it may carry prose

    # --- output ----------------------------------------------------------------------
    def finish(self) -> tuple[str, dict[str, list[str]], list[Item]]:
        headline = self._headline()
        self._add_item("headline", headline)
        self._births_and_deaths()
        self._fortunes()
        self._weather_and_land()
        self._works()
        self._board()
        self._tempers()
        self._voices()
        if len(self._items) == 1:
            self._line("Notes", "quiet", "Nothing of note was recorded.")
        sections = {name: lines for name, lines in self._sections.items() if lines}
        return headline, sections, self._items

    def _add_item(self, kind: str, text: str, seq: int | None = None) -> str:
        item_id = f"d{self.day}-{len(self._items) + 1}"
        self._items.append((item_id, kind, seq, text))
        return item_id

    def _line(self, section: str, kind: str, text: str, seq: int | None = None) -> None:
        self._add_item(kind, text, seq)
        self._sections[section].append(text)

    def name(self, agent_id: Any, given: Any = None) -> str:
        if given:
            return _clean(given)
        key = str(agent_id) if agent_id is not None else ""
        return self.names.get(key) or _clean(key or None)

    def task_title(self, p: dict[str, Any]) -> str:
        task_id = str(p.get("task_id") or "")
        if p.get("title"):
            return _clean(p.get("title"), LABEL_LEN, default="a task")
        return self.task_titles.get(task_id) or _clean(task_id or None, LABEL_LEN, default="a task")

    def _top_forager(self) -> tuple[str, float, int] | None:
        if not self.forage_usd:
            return None
        who = max(self.forage_usd, key=lambda a: (self.forage_usd[a], self.forage_trips[a], a))
        return who, self.forage_usd[who], self.forage_trips[who]

    # --- headline --------------------------------------------------------------------
    def _headline(self) -> str:
        facts: list[str] = []
        if self.deaths:
            names = [self.name(p.get("agent_id"), p.get("name")) for _, p in self.deaths]
            if len(names) == 1:
                facts.append(f"{names[0]} is gone")
            elif len(names) == 2:
                facts.append(f"{names[0]} and {names[1]} are gone")
            else:
                facts.append(f"{_count_words(len(names), 'agent')} are gone")
        for _, p in self.epochs[:1]:
            kind = _clean(p.get("kind"), default="custom")
            facts.append(_EPOCH_PHRASES.get(kind, "the world shifted"))
        if self.births:
            names = [self.name(p.get("agent_id"), p.get("name")) for _, p in self.births]
            if len(names) == 1:
                verb = "arrived" if self.births[0][1].get("kind") == "arrival" else "was born"
                facts.append(f"{names[0]} {verb}")
            else:
                facts.append(f"{_count_words(len(names), 'agent')} were born")
        if self.verified:
            facts.append(f"{_count_words(len(self.verified), 'gadget')} stood")
        if self.completed:
            facts.append(f"{self.name(self.completed[0][1].get('agent_id'))} finished a task")
        top = self._top_forager()
        if top is not None:
            facts.append(f"{self.name(top[0])} foraged most")
        if self.degen_total:
            facts.append(f"{_count_words(self.degen_total, 'temper')} frayed")
        if self.talks:
            facts.append(f"{_count_words(self.talks, 'conversation')} held")
        body = "; ".join(facts[:3]) if facts else "a quiet day"
        return f"Day {self.day}: {body}"

    # --- sections --------------------------------------------------------------------
    def _births_and_deaths(self) -> None:
        for seq, p in self.deaths:
            who = self.name(p.get("agent_id"), p.get("name"))
            tier = _clean(p.get("tier"), default="unknown tier")
            gen = _int(p.get("generation"))
            cause = _clean(p.get("cause"), LABEL_LEN, default="")
            text = f"{who} ({tier}, generation {gen if gen is not None else '?'}) died"
            text += f": {cause}." if cause else "."
            self._line("Births and deaths", "death", text, seq)
        for seq, p in self.births:
            who = self.name(p.get("agent_id"), p.get("name"))
            tier = _clean(p.get("tier"), default="unknown tier")
            gen = _int(p.get("generation"))
            kind = str(p.get("kind") or "")
            if kind == "arrival":
                verb = "arrived from beyond the edge"
            elif kind == "replacement":
                verb = "was spawned to fill an empty place"
            else:
                verb = "was born"
            text = f"{who} ({tier}, generation {gen if gen is not None else '?'}) {verb}"
            if p.get("parent_id"):
                text += f" to {self.name(p.get('parent_id'))}"
            if p.get("endowment_usd") is not None:
                text += f" with {_usd(p.get('endowment_usd'))}"
            self._line("Births and deaths", "birth", text + ".", seq)

    def _fortunes(self) -> None:
        top = self._top_forager()
        if top is not None:
            who, usd, trips = top
            total = sum(self.forage_usd.values())
            self._line(
                "Fortunes",
                "fortune",
                f"{self.name(who)} foraged most: {_usd(usd)} over {_count(trips, 'trip')} "
                f"({_usd(total)} gathered in all).",
            )
        if self.transfer_count:
            self._line(
                "Fortunes",
                "fortune",
                f"{_count(self.transfer_count, 'transfer')} moved {_usd(self.transfer_total)} between agents.",
            )
        if self.windfall_count:
            self._line(
                "Fortunes",
                "fortune",
                f"{_count(self.windfall_count, 'windfall')} recorded, {_usd(self.windfall_total)} in all.",
            )

    def _weather_and_land(self) -> None:
        if self.weather:
            values = [v for v, _ in self.weather]
            first, last = self.weather[0][1], self.weather[-1][1]
            span = f"(from {min(values):+.2f} to {max(values):+.2f})"
            if first == last:
                text = f"Weather ran {last} all day {span}."
            else:
                text = f"Weather turned from {first} to {last} {span}."
            self._line("Weather and land", "weather", text)
        for seq, p in self.epochs:
            kind = _clean(p.get("kind"), default="custom")
            details: list[str] = []
            if p.get("scarcity") is not None:
                details.append(f"scarcity {_num(p.get('scarcity')):.2f}")
            if p.get("weather_baseline") is not None:
                details.append(f"weather {_num(p.get('weather_baseline')):+.2f}")
            days = _int(p.get("duration_days"))
            if days is not None:
                details.append(_count(days, "day"))
            text = f"A {kind} epoch began" + (f" ({', '.join(details)})" if details else "") + "."
            self._line("Weather and land", "epoch", text, seq)

    def _works(self) -> None:
        if self.verified:
            parts = [f"{_clean(p.get('name'), default='a gadget')} ({self.name(p.get('owner'))})" for _, p in self.verified]
            self._line("Works", "work", f"{_count(len(parts), 'gadget')} stood: {', '.join(parts)}.")
        if self.rejected:
            parts = [
                f"{_clean(p.get('name'), default='a gadget')} at {_clean(p.get('stage'), default='review')}"
                for _, p in self.rejected
            ]
            self._line("Works", "work", f"{_count(len(parts), 'gadget')} failed: {', '.join(parts)}.")

    def _board(self) -> None:
        for seq, p in self.posted:
            self._line("Board", "board", f"Posted: {self.task_title(p)} for {_usd(p.get('reward_usd'))}.", seq)
        for seq, p in self.assigned:
            self._line("Board", "board", f"{self.name(p.get('agent_id'))} took {self.task_title(p)}.", seq)
        for seq, p in self.completed:
            self._line(
                "Board",
                "board",
                f"{self.name(p.get('agent_id'))} completed {self.task_title(p)} for {_usd(p.get('reward_usd'))}.",
                seq,
            )

    def _tempers(self) -> None:
        if not self.degen_total:
            return
        by_tier = ", ".join(f"{n} {tier}" for tier, n in sorted(self.degen_by_tier.items()))
        self._line("Tempers", "temper", f"{_count(self.degen_total, 'temper')} frayed: {by_tier}.")

    def _voices(self) -> None:
        if not (self.talks or self.gossip):
            return
        text = f"{_count(self.talks, 'conversation')} among {_count(len(self.speakers), 'voice')}"
        text += f"; {_count(self.gossip, 'rumour')} changed hands."
        self._line("Voices", "voice", text)


def _render(day: int, headline: str, sections: dict[str, list[str]]) -> str:
    out = [f"# {TITLE}, Day {day}", "", f"**{headline}**", ""]
    for name in _SECTIONS:
        lines = sections.get(name)
        if not lines:
            continue
        out.append(f"## {name}")
        out.append("")
        out.extend(f"- {line}" for line in lines)
        out.append("")
    return "\n".join(out)


# --- chronicle ----------------------------------------------------------------------------


class Chronicle:
    def __init__(
        self,
        cfg: VoidConfig,
        db: Database,
        bus: EventBus,
        registry: AgentRegistry,
        chronicle_dir: Path,
        llm_writer: LlmWriter | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self.registry = registry
        self.chronicle_dir = Path(chronicle_dir)
        self.llm_writer = llm_writer

    def path_for(self, day: int) -> Path:
        return self.chronicle_dir / f"day_{int(day):03d}.md"

    async def write_day(self, day: int, tick: int) -> ChronicleDoc:
        """Write the newspaper for ``day`` from its public events; no-op when disabled."""
        if not self.cfg.chronicle.enabled:
            return ChronicleDoc(day=day, headline="", markdown="", items=[])
        rows = self.db.fetchall(
            "SELECT seq, kind, agent_id, payload FROM events WHERE day=? AND visibility=? ORDER BY seq",
            (int(day), PUBLIC),
        )
        builder = _DayBuilder(day, self._names(), self._task_titles())
        for r in rows:
            builder.consume(int(r["seq"]), str(r["kind"]), r["agent_id"], _payload(r))
        headline, sections, items = builder.finish()
        markdown = _render(day, headline, sections)

        if self.cfg.chronicle.writer == "llm" and self.llm_writer is not None:
            rewritten = await self.llm_writer(markdown)
            if rewritten is not None and rewritten.strip():
                markdown = rewritten.strip() + "\n"

        path = self.path_for(day)
        self.chronicle_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        with self.db.tx():
            self.db.execute("DELETE FROM chronicle_items WHERE day=?", (int(day),))
            for item_id, kind, seq, text in items:
                self.db.execute(
                    "INSERT INTO chronicle_items(day, item_id, kind, event_seq, text) VALUES(?,?,?,?,?)",
                    (int(day), item_id, kind, seq, text),
                )
            rev = (_int(self.db.kv_get(KV_REV, 0)) or 0) + 1
            self.db.kv_set(KV_HEADLINE, headline)
            self.db.kv_set(KV_DAY, int(day))
            self.db.kv_set(KV_REV, rev)
        self.bus.emit(
            Event(
                tick,
                day,
                Kind.CHRONICLE,
                payload={"day": int(day), "headline": headline, "item_count": len(items), "path": f"chronicle/{path.name}", "rev": rev},
            )
        )
        return ChronicleDoc(day=day, headline=headline, markdown=markdown, items=items)

    # --- reading ---------------------------------------------------------------------
    def latest(self) -> ChronicleDoc | None:
        day = _int(self.db.kv_get(KV_DAY))
        if day is None:
            return None
        markdown = self.read(day)
        if markdown is None:
            return None
        return ChronicleDoc(
            day=day,
            headline=str(self.db.kv_get(KV_HEADLINE, "") or ""),
            markdown=markdown,
            items=self.items(day),
        )

    def read(self, day: int) -> str | None:
        path = self.path_for(day)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def headline(self) -> str | None:
        value = self.db.kv_get(KV_HEADLINE)
        return None if value is None else str(value)

    def items(self, day: int) -> list[Item]:
        rows = self.db.fetchall(
            "SELECT item_id, kind, event_seq, text FROM chronicle_items WHERE day=?", (int(day),)
        )

        def order(row: Any) -> int:
            m = _ITEM_NUM_RE.search(str(row["item_id"]))
            return int(m.group(1)) if m else 0

        return [
            (str(r["item_id"]), str(r["kind"]), None if r["event_seq"] is None else int(r["event_seq"]), str(r["text"]))
            for r in sorted(rows, key=order)
        ]

    # --- internals -------------------------------------------------------------------
    def _names(self) -> dict[str, str]:
        return {a.agent_id: _clean(a.name) for a in self.registry.all()}

    def _task_titles(self) -> dict[str, str]:
        rows = self.db.fetchall("SELECT task_id, title FROM tasks")
        return {str(r["task_id"]): _clean(r["title"], LABEL_LEN, default="a task") for r in rows}
