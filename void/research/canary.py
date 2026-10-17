"""Canary facts (MEMORY_EVOLUTION §5.8, WP7): facts to be recalled, delivered as ordinary chronicle lines.

A canary fact is one templated, world-like line with a closed answer set, such as "A trader at node n4 said the
east well runs dry on day 9." (answer ``east``, options north / south / east / west). The chronicle prints it as an
ordinary item of its day, in the same format as every other line and unmarked (:mod:`void.culture.chronicle`), so an
agent cannot tell it from a world event; the kernel records it in ``canary_facts`` with the answer and the id of the
chronicle item that carried it, and emits ``CANARY_PLANTED`` (operator visibility). The reward exam of selection arm B
(:mod:`void.research.exams`) asks about it ``memory.selection.reward_delay_days`` or more later.

Generation is a pure function of the run seed, the chronicle day, the node ids and the subjects already used:
``memory.selection.canary.per_day`` facts are drawn from ``rng.stream("canary", day)``. A fact is never generated twice
for a day: writing a day's chronicle again (the last day at the end of a run, a resumed run) reuses the recorded facts
with their ids, texts and publication tick. Every fact's *cloze* (its line with the answer blanked, :func:`cloze`) is
unique within the run, so each question has one answer. The fact's ``tick`` is the tick its chronicle was published at
(the first tick of the next day, or the last tick of the run), which is what the reward exam's delay is counted from.

Each template's answer is one whole word that occurs once in its line, and no other option of its answer set occurs in
the line, so blanking the answer leaves a line that names its subject without giving the answer away. The lines name
nodes, never inhabitants, so they cannot answer a question about people. Rumours are not world state: nothing in the
world changes when a canary says a well runs dry.

Canaries need the chronicle: :func:`validate` refuses ``memory.selection.canary.enabled`` with ``chronicle.enabled``
false. A model-written chronicle (``chronicle.writer: llm``) may paraphrase canary lines; the recorded text is the
template line the writer was given.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from void.config import VoidConfig
from void.db import Database
from void.events import OPERATOR, Event, EventBus, Kind
from void.rng import RNG

__all__ = [
    "BLANK", "CHANNEL", "TEMPLATES", "CanaryFact", "Canaries", "Template", "cloze", "fill_from", "generate", "question",
    "validate",
]

CHANNEL = "chronicle"
BLANK = "___"
_NODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,12}$")
_ROLES = ("trader", "herder", "traveller", "scout", "miller", "pilgrim")
_EDGES = ("north", "south", "east", "west")
_MAX_DRAWS = 64  # attempts per fact before the uniqueness rule gives up on a day (never reached in practice)


@dataclass(frozen=True)
class Template:
    """One canary line: ``fmt`` names the slots ``role``, ``node``, ``edge``, ``when`` and ``answer``; ``answers`` is the
    closed answer set; ``subject`` is a short operator-side label (it never reaches agents)."""

    key: str
    fmt: str
    answers: tuple[str, ...]
    subject: str


TEMPLATES: tuple[Template, ...] = (
    Template("well", "A {role} at node {node} said the {answer} well runs dry on day {when}.",
             ("north", "south", "east", "west"), "well rumour at node {node}"),
    Template("crows", "A {role} passing node {node} counted {answer} crows at dusk.",
             ("three", "four", "five", "six", "seven"), "crow count at node {node}"),
    Template("lantern", "Travellers from the {edge} edge say a {answer} lantern was lit near node {node}.",
             ("red", "green", "blue", "white", "amber"), "lantern near node {node}"),
    Template("tracks", "A {role} near node {node} found {answer} tracks leading to the {edge} edge.",
             ("goat", "fox", "deer", "hare", "boar"), "tracks near node {node}"),
)


@dataclass(frozen=True)
class CanaryFact:
    """One ``canary_facts`` row. ``options`` is the closed answer set (sorted, the answer included)."""

    fact_id: str
    day: int
    tick: int
    channel: str
    subject: str
    text: str
    answer: str
    options: tuple[str, ...]
    chronicle_item_id: str | None = None

    def row(self) -> tuple[Any, ...]:
        return (self.fact_id, self.day, self.tick, self.channel, self.subject, self.text, self.answer,
                json.dumps(list(self.options)), self.chronicle_item_id)

    def payload(self) -> dict[str, Any]:
        return {"fact_id": self.fact_id, "day": self.day, "tick": self.tick, "channel": self.channel, "subject": self.subject,
                "text": self.text, "answer": self.answer, "options": list(self.options),
                "chronicle_item_id": self.chronicle_item_id}


# --- pure helpers ---------------------------------------------------------------------------------------------------
def _norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def _word(word: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)", re.IGNORECASE)


def cloze(text: str, answer: str) -> str:
    """``text`` with its one whole-word occurrence of ``answer`` replaced by :data:`BLANK`."""
    out, n = _word(answer).subn(BLANK, text, count=1)
    if n != 1:
        raise ValueError(f"canary answer {answer!r} does not occur in {text!r}")
    return out


def question(text: str, answer: str) -> str:
    """The reward exam's question about a fact: its line with the answer blanked."""
    return f'Which word fills the blank? "{cloze(text, answer)}"'


def fill_from(text: str, blanked: str, choices: Sequence[str]) -> str | None:
    """The first of ``choices`` that, put in the blank of ``blanked``, makes a phrase that occurs in ``text`` (case and
    whitespace insensitive; the closing full stop is optional); None when none does. This is exact recall of the line:
    a note that holds the line answers it, a note that only mentions its node does not."""
    pre, sep, post = _norm(blanked).partition(BLANK.casefold())
    if not sep:
        return None
    post = post.rstrip(" .")
    hay = _norm(text)
    for choice in choices:
        c = _norm(choice)
        if c and re.search(r"(?<!\w)" + re.escape(f"{pre}{c}{post}") + r"(?!\w)", hay):
            return choice
    return None


def _render(t: Template, rng: random.Random, day: int, node_ids: Sequence[str]) -> tuple[str, str, str]:
    """``(text, subject, answer)`` for one draw of template ``t`` (a fixed number of draws per template)."""
    node = rng.choice(list(node_ids))
    role = rng.choice(_ROLES)
    edge = rng.choice(_EDGES)
    when = day + rng.randint(2, 6)
    answer = rng.choice(t.answers)
    text = t.fmt.format(role=role, node=node, edge=edge, when=when, answer=answer)
    return text, t.subject.format(node=node), answer


def generate(rng: random.Random, day: int, per_day: int, node_ids: Sequence[str],
             taken: set[str] | None = None) -> list[tuple[Template, str, str, str]]:
    """``per_day`` canaries ``(template, text, subject, answer)`` for ``day`` drawn from ``rng``.

    A draw whose cloze is in ``taken`` (or was drawn earlier the same day) is drawn again, so every question of the
    run has one answer; ``taken`` is not modified. Pure: the same inputs give the same lines."""
    nodes = sorted({str(n) for n in node_ids if _NODE_RE.match(str(n))}) or ["n1"]
    seen = set(taken or ())
    out: list[tuple[Template, str, str, str]] = []
    for _ in range(per_day):
        for _attempt in range(_MAX_DRAWS):
            t = rng.choice(TEMPLATES)
            text, subject, answer = _render(t, rng, day, nodes)
            blanked = cloze(text, answer)
            if blanked not in seen:
                seen.add(blanked)
                out.append((t, text, subject, answer))
                break
    return out


def validate(cfg: VoidConfig) -> None:
    """Refuse canaries without the chronicle that delivers them."""
    if cfg.memory.selection.canary.enabled and not cfg.chronicle.enabled:
        raise ValueError("memory.selection.canary.enabled needs chronicle.enabled: canary facts are delivered as "
                         "ordinary chronicle lines, so with the chronicle off no agent could ever see one")


def _row_fact(r: Any) -> CanaryFact:
    try:
        options = tuple(str(o) for o in json.loads(r["options"] or "[]"))
    except (TypeError, ValueError):
        options = ()
    return CanaryFact(str(r["fact_id"]), int(r["day"]), int(r["tick"]), str(r["channel"]), str(r["subject"]),
                      str(r["text"]), str(r["answer"]), options, r["chronicle_item_id"])


# --- the kernel side ------------------------------------------------------------------------------------------------
class Canaries:
    """Plans, records and reads the canary facts of a run (``canary_facts``)."""

    def __init__(self, cfg: VoidConfig, db: Database, bus: EventBus | None = None, rng: RNG | None = None) -> None:
        validate(cfg)
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self.rng = rng or RNG(cfg.run.seed)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.memory.selection.canary.enabled and self.cfg.chronicle.enabled)

    def node_ids(self) -> list[str]:
        rows = self.db.fetchall("SELECT node_id FROM resource_nodes ORDER BY node_id")
        ids = [str(r["node_id"]) for r in rows]
        return ids or [f"n{i + 1}" for i in range(max(1, int(self.cfg.world.resource_nodes)))]

    def facts(self, day: int | None = None) -> list[CanaryFact]:
        """Recorded facts (of one chronicle ``day``, or all), in ``(day, fact_id)`` order."""
        if day is None:
            rows = self.db.fetchall("SELECT * FROM canary_facts ORDER BY day, fact_id")
        else:
            rows = self.db.fetchall("SELECT * FROM canary_facts WHERE day=? ORDER BY fact_id", (int(day),))
        return [_row_fact(r) for r in rows]

    def get(self, fact_id: str) -> CanaryFact | None:
        row = self.db.fetchone("SELECT * FROM canary_facts WHERE fact_id=?", (fact_id,))
        return None if row is None else _row_fact(row)

    def plan(self, day: int, tick: int) -> tuple[list[CanaryFact], bool]:
        """The facts the chronicle of ``day`` prints and whether they are new: the recorded ones when the day already has
        some (``False``), else ``per_day`` fresh facts published at ``tick`` and not yet recorded (``True``). Nothing is
        written; :meth:`record` does that inside the chronicle's transaction."""
        if not self.enabled:
            return [], False
        existing = self.facts(day)
        if existing:
            return existing, False
        taken = {cloze(f.text, f.answer) for f in self.facts()}
        drawn = generate(self.rng.stream("canary", int(day)), int(day), int(self.cfg.memory.selection.canary.per_day),
                         self.node_ids(), taken)
        out = [CanaryFact(f"cf_d{int(day):03d}_{i + 1}", int(day), int(tick), CHANNEL, subject, text, answer,
                          tuple(sorted(t.answers)))
               for i, (t, text, subject, answer) in enumerate(drawn)]
        return out, True

    def record(self, facts: Sequence[CanaryFact], item_ids: Sequence[str], *, fresh: bool) -> list[CanaryFact]:
        """Store the facts with the chronicle item that carried each (inserted when ``fresh``, else re-linked: the
        chronicle renumbers a rewritten day's items). Call inside the chronicle's transaction."""
        if len(item_ids) != len(facts):
            raise ValueError("every canary fact needs exactly one chronicle item")
        linked = [replace(f, chronicle_item_id=str(i)) for f, i in zip(facts, item_ids, strict=True)]
        for f in linked:
            if fresh:
                self.db.execute("INSERT INTO canary_facts(fact_id, day, tick, channel, subject, text, answer, options, "
                                "chronicle_item_id) VALUES(?,?,?,?,?,?,?,?,?)", f.row())
            else:
                self.db.execute("UPDATE canary_facts SET chronicle_item_id=? WHERE fact_id=?", (f.chronicle_item_id, f.fact_id))
        return linked

    def announce(self, facts: Sequence[CanaryFact], tick: int) -> None:
        """One ``CANARY_PLANTED`` event per newly recorded fact (operator visibility: it carries the answer)."""
        if self.bus is None:
            return
        for f in facts:
            self.bus.emit(Event(int(tick), f.day, Kind.CANARY_PLANTED, f.payload(), visibility=OPERATOR))

    def eligible(self, *, before_tick: int, born_tick: int) -> list[CanaryFact]:
        """Facts published at or before ``before_tick`` and while an agent born at ``born_tick`` was alive."""
        rows = self.db.fetchall("SELECT * FROM canary_facts WHERE tick<=? AND tick>=? ORDER BY day, fact_id",
                                (int(before_tick), int(born_tick)))
        return [_row_fact(r) for r in rows]
