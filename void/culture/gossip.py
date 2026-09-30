"""Gossip: horizontal transfer of notes between agents that talk (DESIGN §10, §11.1).

When one agent talks to another the kernel calls :meth:`Gossip.queue`; at the end of the
tick :meth:`Gossip.flush` copies one note from the speaker into the listener's vault with
a paraphrased body and full provenance (``channel=gossip``, ``hop + 1``, origin note and
generation, transfer tick, path of agents). A ``gossip_transfer`` event records the
embedding similarity of the copy to the origin so drift per hop can be measured.

Paraphrasing is scripted by default (:func:`paraphrase_scripted`, a deterministic
telephone-game perturbation) and may be routed through an injected LLM ``paraphraser``;
when that returns ``None`` (gated out by the wallet or budget) the scripted path is used
and a ``gossip_paraphrase_downgraded`` event is emitted.

Every random draw comes from ``rng.stream("gossip", ...)`` and the queue is processed in
insertion order, so a run's gossip is a pure function of ``(config, seed)``.
"""

from __future__ import annotations

import dataclasses
import math
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from void.agents.registry import AgentRegistry
from void.config import VoidConfig
from void.events import Event, EventBus, Kind
from void.memory.notes import Note, Provenance, normalise_title
from void.memory.store import MemoryStore
from void.rng import RNG

__all__ = [
    "Gossip",
    "paraphrase_scripted",
    "make_provenance",
    "GOSSIP_PARAPHRASE_DOWNGRADED",
    "SYNONYMS",
    "STOP_WORDS",
    "EXCLUDED_TAGS",
]

GOSSIP_PARAPHRASE_DOWNGRADED = "gossip_paraphrase_downgraded"
GOSSIP_TAG = "gossip"
EXCLUDED_TAGS = frozenset({"self", "entity"})
LENGTH_TOLERANCE = 0.30

Paraphraser = Callable[[str, str, int], Awaitable[str | None]]

# --- scripted paraphrase ------------------------------------------------------------------

STOP_WORDS = frozenset(
    """
    a an the and or but if of to in on at by for with from as is are was were be been being am
    it its this that these those i you he she we they me him her us them my your his our their
    not no so do does did have has had will would can could should may might there here then
    than very just also too only what which who whom when where why how all any some more most
    other another into over under up down out about because while until yet still even each
    every both either neither own same such
    """.split()
)

_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("rich", "plentiful", "abundant"),
    ("thin", "scarce", "meagre"),
    ("empty", "barren"),
    ("north", "south", "east", "west"),
    ("northern", "southern", "eastern", "western"),
    ("storm", "gale", "squall"),
    ("storms", "gales", "squalls"),
    ("rain", "drizzle", "downpour"),
    ("sunny", "clear", "bright"),
    ("cloudy", "grey", "overcast"),
    ("calm", "placid", "mild"),
    ("cold", "chilly", "bitter"),
    ("hot", "warm", "sweltering"),
    ("wind", "breeze", "gust"),
    ("weather", "sky", "climate"),
    ("money", "cash", "coin"),
    ("dollar", "buck"),
    ("dollars", "bucks"),
    ("wealth", "fortune", "riches"),
    ("poor", "broke", "penniless"),
    ("cheap", "affordable"),
    ("expensive", "costly", "dear"),
    ("price", "cost", "fee"),
    ("balance", "purse", "funds"),
    ("good", "fine", "decent"),
    ("bad", "awful", "dreadful"),
    ("great", "excellent", "superb"),
    ("big", "large", "huge"),
    ("small", "little", "tiny"),
    ("fast", "quick", "swift"),
    ("slow", "sluggish"),
    ("near", "close", "nearby"),
    ("far", "distant", "remote"),
    ("always", "often", "usually"),
    ("never", "rarely", "seldom"),
    ("morning", "dawn", "daybreak"),
    ("evening", "dusk", "nightfall"),
    ("night", "dark"),
    ("found", "discovered", "spotted"),
    ("said", "claimed", "told"),
    ("says", "claims", "tells"),
    ("go", "head", "travel"),
    ("went", "headed", "travelled"),
    ("safe", "secure"),
    ("danger", "risk", "peril"),
    ("busy", "crowded", "packed"),
    ("quiet", "silent", "deserted"),
    ("first", "earliest"),
    ("last", "final"),
    ("full", "stocked", "loaded"),
    ("many", "plenty", "lots"),
    ("few", "scant"),
)

SYNONYMS: dict[str, tuple[str, ...]] = {}
for _group in _SYNONYM_GROUPS:
    for _word in _group:
        SYNONYMS.setdefault(_word, tuple(w for w in _group if w != _word))

# A wikilink is one atomic token; whitespace runs are kept so the output keeps its shape.
_TOKEN_RE = re.compile(r"\[\[[^\[\]]+?\]\]|\s+|[^\s\[]+|\[")


def _split_word(tok: str) -> tuple[str, str, str]:
    """``(prefix punctuation, core, suffix punctuation)`` of a token."""
    i, j = 0, len(tok)
    while i < j and not tok[i].isalnum():
        i += 1
    while j > i and not tok[j - 1].isalnum():
        j -= 1
    return tok[:i], tok[i:j], tok[j:]


def _is_wikilink(tok: str) -> bool:
    return len(tok) > 4 and tok.startswith("[[") and tok.endswith("]]")


def _is_word(tok: str) -> bool:
    return bool(tok) and not tok.isspace() and not _is_wikilink(tok) and _split_word(tok)[1] != ""


def _is_content(tok: str) -> bool:
    core = _split_word(tok)[1]
    return any(c.isalpha() for c in core) and core.lower() not in STOP_WORDS


def _match_case(new: str, like: str) -> str:
    if len(like) > 1 and like.isupper():
        return new.upper()
    if like[:1].isupper():
        return new[:1].upper() + new[1:]
    if like.islower():
        return new.lower()
    return new


def _neighbour_word(tokens: list[str], i: int) -> int | None:
    """Index of the adjacent word token (right first, then left); wikilinks block the search."""
    for step in (1, -1):
        j = i + step
        while 0 <= j < len(tokens):
            tok = tokens[j]
            if tok == "" or tok.isspace():
                j += step
                continue
            if _is_word(tok):
                return j
            break
    return None


def paraphrase_scripted(text: str, mutation_rate: float, rng: random.Random) -> str:
    """Deterministically perturb a fraction ``mutation_rate`` of the content words of ``text``.

    Stop words and ``[[wikilinks]]`` are never touched. For each chosen word the ``rng``
    picks an operation: swap it for a built-in synonym, drop it, or swap it with its
    neighbouring word (punctuation stays in place, case follows the slot). Operations that
    would push the length outside ±30% of the input are skipped, so the bound always
    holds. ``mutation_rate <= 0`` returns ``text`` unchanged.
    """
    if mutation_rate <= 0.0 or not text:
        return text
    tokens = _TOKEN_RE.findall(text)
    content = [i for i, t in enumerate(tokens) if _is_word(t) and _is_content(t)]
    if not content:
        return text
    k = min(len(content), math.ceil(min(1.0, mutation_rate) * len(content)))
    targets = sorted(rng.sample(content, k))
    total = len(text)
    lo, hi = math.floor(total * (1 - LENGTH_TOLERANCE)), math.ceil(total * (1 + LENGTH_TOLERANCE))
    length = total

    for i in targets:
        tok = tokens[i]
        if not _is_word(tok):  # dropped or displaced by an earlier operation
            continue
        prefix, core, suffix = _split_word(tok)
        options = SYNONYMS.get(core.lower())
        weighted = (["synonym", "synonym"] if options else []) + ["drop", "swap"]
        first = rng.choice(weighted)
        order = [first] + [op for op in ("synonym", "swap", "drop") if op != first]
        for op in order:
            if op == "synonym":
                if not options:
                    continue
                new_tok = prefix + _match_case(rng.choice(options), core) + suffix
                new_len = length - len(tok) + len(new_tok)
                if lo <= new_len <= hi:
                    tokens[i] = new_tok
                    length = new_len
                    break
            elif op == "swap":
                j = _neighbour_word(tokens, i)
                if j is None:
                    continue
                p2, c2, s2 = _split_word(tokens[j])
                tokens[i] = prefix + _match_case(c2, core) + suffix
                tokens[j] = p2 + _match_case(core, c2) + s2
                break
            else:  # drop the word and one adjacent whitespace run
                ws: int | None = None
                if i + 1 < len(tokens) and tokens[i + 1].isspace():
                    ws = i + 1
                elif i > 0 and tokens[i - 1].isspace():
                    ws = i - 1
                removed = len(tok) + (len(tokens[ws]) if ws is not None else 0)
                if lo <= length - removed <= hi:
                    tokens[i] = ""
                    if ws is not None:
                        tokens[ws] = ""
                    length -= removed
                    break
    return "".join(tokens)


# --- provenance helper --------------------------------------------------------------------

_PROVENANCE_FIELDS = frozenset(f.name for f in dataclasses.fields(Provenance))


def make_provenance(**fields: Any) -> Provenance:
    """A :class:`Provenance` from whichever of ``fields`` the dataclass currently defines.

    ``channel``, ``transfer_tick`` and ``path_agents`` (DESIGN §10) are passed through when
    the memory layer defines them and dropped otherwise.
    """
    return Provenance(**{k: v for k, v in fields.items() if k in _PROVENANCE_FIELDS})


def _path_agents(prov: Provenance) -> list[str]:
    path = getattr(prov, "path_agents", None) or getattr(prov, "path", None) or []
    return [str(a) for a in path]


# --- gossip -------------------------------------------------------------------------------


@dataclass
class _Pending:
    speaker_id: str
    listener_id: str
    tick: int
    note_title: str | None
    forced: bool


class Gossip:
    """Queues note shares during a tick and performs them at :meth:`flush`."""

    def __init__(
        self,
        cfg: VoidConfig,
        memory: MemoryStore,
        registry: AgentRegistry,
        rng: RNG,
        bus: EventBus,
        paraphraser: Paraphraser | None = None,
    ) -> None:
        self.cfg = cfg
        self.memory = memory
        self.registry = registry
        self.rng = rng
        self.bus = bus
        self.paraphraser = paraphraser
        self._queue: list[_Pending] = []

    @property
    def pending(self) -> int:
        return len(self._queue)

    # --- kernel entry points ------------------------------------------------------------
    def queue(
        self,
        speaker_id: str,
        listener_id: str,
        tick: int,
        *,
        note_title: str | None = None,
        forced: bool = False,
    ) -> None:
        """Register a share. ``forced`` (``share_note``) always queues; a ``talk`` queues with
        probability ``gossip.share_probability_on_talk``."""
        if not self.cfg.gossip.enabled or speaker_id == listener_id:
            return
        if not forced:
            p = self.cfg.gossip.share_probability_on_talk
            if p <= 0.0 or self.rng.stream("gossip", tick, speaker_id, listener_id).random() >= p:
                return
        title = (note_title or "").strip() or None
        self._queue.append(_Pending(speaker_id, listener_id, int(tick), title, forced))

    async def flush(self, tick: int, day: int) -> list[dict[str, Any]]:
        """Perform every queued share in order; returns the ``gossip_transfer`` payloads."""
        pending, self._queue = self._queue, []
        if not self.cfg.gossip.enabled or not pending:
            return []
        llm_calls = 0
        out: list[dict[str, Any]] = []
        for item in pending:
            listener = self.registry.get(item.listener_id)
            if listener is None or not listener.alive:
                continue
            speaker = self.registry.get(item.speaker_id)
            if speaker is None:
                continue
            note = self._pick_note(item, self._held_origins(item.listener_id))
            if note is None:
                continue

            origin_id = note.provenance.origin_note_id or note.note_id
            origin = note if origin_id == note.note_id else self.memory.get_note(origin_id)
            origin_generation = note.provenance.origin_generation
            if origin_generation is None:
                origin_generation = int(speaker.generation)

            new_body, used_llm = await self._paraphrase(item, note, tick, day, llm_calls)
            llm_calls += used_llm

            hop = int(note.provenance.hop) + 1
            provenance = make_provenance(
                channel=GOSSIP_TAG,
                source_agent_id=item.speaker_id,
                hop=hop,
                origin_note_id=origin_id,
                origin_generation=origin_generation,
                transfer_tick=int(tick),
                path_agents=_path_agents(note.provenance) + [item.speaker_id],
            )
            copy = self.memory.copy_note(
                note,
                item.listener_id,
                tick,
                body=new_body,
                provenance=provenance,
                tags=list(note.tags) + [GOSSIP_TAG],
            )
            reference = origin.body if origin is not None else note.body
            similarity = round(max(0.0, min(1.0, self.memory.similarity(reference, copy.body))), 4)
            payload: dict[str, Any] = {
                "speaker_id": item.speaker_id,
                "listener_id": item.listener_id,
                "hop": hop,
                "origin_note_id": origin_id,
                "origin_agent_id": origin.agent_id if origin is not None else item.speaker_id,
                "origin_generation": origin_generation,
                "note_title": copy.title,
                "similarity": similarity,
                "new_note_id": copy.note_id,
            }
            self.bus.emit(Event(tick, day, Kind.GOSSIP_TRANSFER, payload=payload, agent_id=item.listener_id))
            out.append(payload)
        return out

    # --- internals ----------------------------------------------------------------------
    def _held_origins(self, agent_id: str) -> set[str]:
        """Origins already present in ``agent_id``'s live notes (own ids count as origins)."""
        rows = self.memory.db.fetchall(
            "SELECT note_id, origin_note_id FROM notes WHERE agent_id=? AND archived=0", (agent_id,)
        )
        held: set[str] = set()
        for r in rows:
            held.add(str(r["note_id"]))
            if r["origin_note_id"]:
                held.add(str(r["origin_note_id"]))
        return held

    def _pick_note(self, item: _Pending, held: set[str]) -> Note | None:
        """The named note if found, else the speaker's best shareable note the listener lacks."""
        candidates = [
            n
            for n in self.memory.list_notes(item.speaker_id)
            if not (set(n.tags) & EXCLUDED_TAGS)
            and n.provenance.source_agent_id != item.listener_id
            and item.listener_id not in _path_agents(n.provenance)
        ]
        candidates.sort(key=lambda n: (-float(n.importance), -int(n.created_tick), n.note_id))

        def shareable(n: Note) -> bool:
            return (n.provenance.origin_note_id or n.note_id) not in held

        if item.note_title is not None:
            key = normalise_title(item.note_title)
            named = [n for n in candidates if normalise_title(n.title) == key]
            if named:
                return named[0] if shareable(named[0]) else None
        for n in candidates:
            if shareable(n):
                return n
        return None

    async def _paraphrase(
        self, item: _Pending, note: Note, tick: int, day: int, llm_calls: int
    ) -> tuple[str, int]:
        """``(new body, llm calls used)``; falls back to the scripted path when the LLM is unavailable."""
        gcfg = self.cfg.gossip
        if gcfg.paraphrase == "llm" and self.paraphraser is not None:
            if llm_calls < gcfg.max_llm_paraphrases_per_tick:
                result = await self.paraphraser(item.listener_id, note.body, tick)
                if result is not None and result.strip():
                    return result.strip(), 1
                self._downgrade(item, note, tick, day, "gated")
                return self._scripted(item, note, tick), 1
            self._downgrade(item, note, tick, day, "tick_budget")
        return self._scripted(item, note, tick), 0

    def _scripted(self, item: _Pending, note: Note, tick: int) -> str:
        stream = self.rng.stream("gossip", "paraphrase", tick, item.speaker_id, item.listener_id, note.note_id)
        return paraphrase_scripted(note.body, self.cfg.gossip.mutation_rate, stream)

    def _downgrade(self, item: _Pending, note: Note, tick: int, day: int, reason: str) -> None:
        self.bus.emit(
            Event(
                tick,
                day,
                GOSSIP_PARAPHRASE_DOWNGRADED,
                payload={
                    "speaker_id": item.speaker_id,
                    "listener_id": item.listener_id,
                    "note_id": note.note_id,
                    "reason": reason,
                },
                agent_id=item.listener_id,
            )
        )
