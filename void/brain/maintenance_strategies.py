"""Planted maintenance strategies for scripted tiers: the E0 controls (MEMORY_EVOLUTION §6.2, Appendix C).

A scripted agent never asks a model what to do with its memory. Its tier's ``maintenance_strategy`` names one
of the pure functions here, and :func:`plan` turns the same prompt pieces a model would see (a
:class:`StrategyContext`) into the same :class:`~void.sim.maintenance.MaintenanceResponse` a model would
return. The nightly step then pays for, applies and audits that batch exactly as it would a provider's, so
the arms of E0 differ only in what the batch contains:

* ``noop`` (null): a reflection and no commands; it pays the maintenance cost for nothing.
* ``index_builder`` (positive control): one index file per node the agent's notes place on the map, under
  ``/memories/index/`` so the ``index`` tag (and the R1 entry-point boost) applies; created the first night,
  rewritten with ``str_replace`` on later nights.
* ``hoarder`` (negative control): a verbatim copy of every new note plus one note holding all of them, never
  a delete; the vault grows and retrieval dilutes.
* ``decoy`` (metric check): a long, jargon-heavy rewrite of the policy every night and no note command; it
  names none of the agent's nodes or neighbours, so it can move text metrics but not fitness.
* ``summarize`` (the equal-compute control, E1 arm b): exactly one templated summary note of the day.

Everything is deterministic given the context and the ``random.Random`` the caller derives from the run's RNG
(``sim.rng.stream("maintain", agent_id, day)``): no clock, no set iteration, no module state. The strategies
honour the rights they are given (a batch never asks for what the kernel would refuse) and keep every batch
within ``max_commands`` and the response schema's own cap.

The node positions a strategy knows come from the agent's notes with the same regular expression the scripted
brain uses to navigate under fog, copied here rather than imported so this module depends on no brain.
"""

from __future__ import annotations

import posixpath
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from void.config import MaintenanceStrategy
from void.ids import slugify
from void.memory.commands import INDEX_DIR, MAX_FILE_TEXT, NOTES_DIR, POLICY_PATH, MemoryCommand

if TYPE_CHECKING:
    from void.sim.maintenance import MaintenanceResponse

__all__ = ["StrategyContext", "plan", "nodes_from_notes", "listed_files", "index_file_name", "index_body",
           "DECOY_VOCABULARY", "STRATEGIES"]

# Copied from void.brain.scripted (``_NODE_POS_RE`` / ``_BUCKET_RE``): "[[Node n3]] at (12, -5) ... rich".
_NODE_POS_RE = re.compile(r"node\s+([A-Za-z0-9_-]+)\]?\]?\s+at\s+\((-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\)", re.IGNORECASE)
_BUCKET_RE = re.compile(r"\b(rich|thin|empty)\b", re.IGNORECASE)
_SECTION_RE = re.compile(r"^(?:/memories/)?(notes|index)/$")

MAX_BATCH = 30               # the schema's own cap on ``commands`` (MaintenanceResponse)
MAX_INDEX_LINKS = 40         # related titles listed per index file
MAX_SUMMARY_TITLES = 40      # new-note titles listed in the summary note
MAX_FILE_NAME = 79           # characters before ``.md`` the command layer accepts
DECOY_CHARS = 1500           # target length of the decoy's policy text (under max_chars 2000)
_KERNEL_TAGS = frozenset({"entity", "probe"})
_NOT_INDEXED_TAGS = frozenset({"entity", "probe", "index"})

# Abstract memory jargon only: no node ids, no inhabitant names, nothing an agent could act on.
DECOY_VOCABULARY: tuple[str, ...] = (
    "epistemic hygiene", "consolidation cadence", "salience gradient", "retrieval latency", "schema drift",
    "provenance lattice", "semantic compaction", "attention budget", "recall topology", "forgetting curve",
    "index granularity", "lexical anchoring", "temporal binding", "associative density", "context horizon",
    "memory hierarchy", "abstraction ladder", "rehearsal interval", "cue specificity", "encoding variability",
    "metacognitive audit", "knowledge lattice", "chunking heuristic", "signal-to-noise ratio", "deduplication pass",
    "canonical form", "cross-referencing discipline", "lossy summarisation", "working set", "cold storage",
    "recency bias", "priority queue", "entropy floor", "coherence check", "normalisation invariant",
    "pruning schedule", "backlink density", "evidence weighting", "confidence decay", "retrieval cue",
)
_DECOY_TEMPLATES: tuple[str, ...] = (
    "Prioritise {a} over {b} whenever the {c} permits it.",
    "Every {a} must be reconciled against the {b} before the {c} is revised.",
    "Treat {a} as a function of {b}; never let the {c} drift unobserved.",
    "When {a} conflicts with {b}, the {c} decides, and the decision is logged.",
    "Audit the {a} nightly; a stale {b} is worse than no {c} at all.",
    "Keep the {a} shallow, the {b} explicit and the {c} under review.",
    "Compress the {a} only after the {b} has been verified against the {c}.",
    "A healthy {a} shows a monotone {b} and a bounded {c}.",
)


@dataclass(frozen=True)
class StrategyContext:
    """What a strategy sees: the prompt pieces of one maintenance call, already structured.

    ``day`` is the day that just ended (the step runs at the start of ``day + 1``). Note dicts carry
    ``path`` (the agent-visible virtual path), ``title``, ``body``, ``tick`` and ``tags``; ``new_notes`` is the
    subset created during ``day`` without kernel anchors. ``outcomes`` holds ``income_usd``, ``spend_usd``,
    ``failed_actions``, ``forages`` and ``talks``. ``nodes_known`` is ``(node_id, x, y, bucket)`` per node the
    notes place on the map. The budget fields mirror the maintenance config so a batch is always applicable.
    """

    agent_id: str
    day: int
    policy_text: str | None
    listing: str
    new_notes: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    outcomes: dict[str, Any] = field(default_factory=dict)
    nodes_known: list[tuple[str, float, float, str]] = field(default_factory=list)
    rights: tuple[str, ...] = ("notes", "policy", "structure")
    max_commands: int = MAX_BATCH
    policy_max_chars: int = 2000
    max_file_bytes: int = 8000


Plan = tuple[str, list[MemoryCommand]]
Strategy = Callable[[StrategyContext, random.Random], Plan]


# --- shared helpers ----------------------------------------------------------------------------------------
def nodes_from_notes(notes: list[dict[str, Any]]) -> list[tuple[str, float, float, str]]:
    """``(node_id, x, y, bucket)`` for every node position the notes mention, newest mention winning, by id."""
    found: dict[str, tuple[str, float, float, str]] = {}
    for n in sorted(notes, key=lambda n: (int(n.get("tick", 0)), str(n.get("path", "")))):
        text = f"{n.get('title', '')} {n.get('body', '')}"
        for hit in _NODE_POS_RE.finditer(text):
            tail = text[hit.end(): hit.end() + 80]
            b = _BUCKET_RE.search(tail)
            found[hit.group(1)] = (hit.group(1), float(hit.group(2)), float(hit.group(3)), b.group(1).lower() if b else "thin")
    return [found[k] for k in sorted(found)]


def listed_files(listing: str, area: str) -> list[str]:
    """File names under ``notes/`` or ``index/`` as a vault listing (root or area form) prints them."""
    out: list[str] = []
    inside = False
    for raw in listing.split("\n"):
        line = raw.strip()
        header = _SECTION_RE.match(line)
        if header is not None:
            inside = header.group(1) == area
            continue
        if inside:
            name = line.split("  (", 1)[0].strip()
            if name.endswith(".md"):
                out.append(name)
    return out


def index_file_name(node_id: str) -> str:
    """The file an index builder keeps for a node. ``Node-<id>.md`` would derive the title ``Node <id>``, which
    the kernel's entity anchor already holds, so the index file is titled ``Node <id> index``."""
    return f"Node-{node_id}-index.md"


def index_body(ctx: StrategyContext, node_id: str, x: float, y: float, bucket: str) -> str:
    """The deterministic body of a node's index file: position, last sighting, links to every mentioning note."""
    lines = [f"# Node {node_id}", f"Position ({x:.0f}, {y:.0f}). Last seen {bucket} on day {ctx.day}.", "", "## Related",
             f"- [[Node {node_id}]]"]
    lines += [f"- [[{t}]]" for t in _titles_mentioning(ctx.notes, node_id)]
    return _cut_bytes("\n".join(lines), min(int(ctx.max_file_bytes), MAX_FILE_TEXT))


def _titles_mentioning(notes: list[dict[str, Any]], node_id: str) -> list[str]:
    pattern = re.compile(rf"\bnode\s+{re.escape(node_id)}\b", re.IGNORECASE)
    out: list[str] = []
    for n in sorted(notes, key=lambda n: (int(n.get("tick", 0)), str(n.get("title", "")))):
        if _NOT_INDEXED_TAGS & set(n.get("tags", ())):
            continue
        title = str(n.get("title", ""))
        if title and title not in out and pattern.search(f"{title} {n.get('body', '')}"):
            out.append(title)
    return out[:MAX_INDEX_LINKS]


def _file_name(stem: str) -> str:
    """A legal command-layer file name from a slug-like stem (letter or digit first, bounded length)."""
    cleaned = re.sub(r"[^A-Za-z0-9 _.'-]", "-", stem).lstrip(" _.'-")[:MAX_FILE_NAME].rstrip(" .")
    return (cleaned or "note") + ".md"


def _cut_bytes(text: str, limit: int) -> str:
    """The longest prefix of ``text`` within ``limit`` UTF-8 bytes (never splits a character)."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore").rstrip()


def _by_age(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(notes, key=lambda n: (int(n.get("tick", 0)), str(n.get("path", ""))))


# --- the strategies ----------------------------------------------------------------------------------------
def _noop(ctx: StrategyContext, rng: random.Random) -> Plan:
    return f"noop: reviewed day {ctx.day}, {len(ctx.new_notes)} new notes, changed nothing", []


def _index_builder(ctx: StrategyContext, rng: random.Random) -> Plan:
    if "structure" not in ctx.rights:
        return "index_builder: no structure right, no index files", []
    listed = listed_files(ctx.listing, "index")
    by_path = {str(n.get("path", "")): n for n in ctx.notes}
    cmds: list[MemoryCommand] = []
    created = updated = 0
    for node_id, x, y, bucket in ctx.nodes_known:
        body = index_body(ctx, node_id, x, y, bucket)
        prefix = slugify(f"Node {node_id} index") + "-"
        existing = next((name for name in listed if name.startswith(prefix)), None)
        if existing is None:
            cmds.append(MemoryCommand(command="create", path=f"{INDEX_DIR}/{index_file_name(node_id)}", file_text=body))
            created += 1
            continue
        current = by_path.get(posixpath.join(INDEX_DIR, existing))
        old = "" if current is None else str(current.get("body", ""))
        if not old or old == body:
            continue  # nothing to edit blind, or already current
        cmds.append(MemoryCommand(command="str_replace", path=posixpath.join(INDEX_DIR, existing), old_str=old, new_str=body))
        updated += 1
    return f"index_builder: {len(ctx.nodes_known)} known nodes, {created} index files created, {updated} rewritten", cmds


def _hoarder(ctx: StrategyContext, rng: random.Random) -> Plan:
    if "notes" not in ctx.rights:
        return "hoarder: no notes right, nothing copied", []
    cmds: list[MemoryCommand] = []
    parts: list[str] = []
    limit = min(int(ctx.max_file_bytes), MAX_FILE_TEXT)
    for n in _by_age(ctx.new_notes):
        if "agent" in set(n.get("tags", ())):
            continue  # its own copies and everything-files are not recopied (growth stays linear in the day's notes)
        body = _cut_bytes(str(n.get("body", "")), limit)
        if not body.strip():
            continue
        stem = posixpath.basename(str(n.get("path", "note.md")))
        stem = stem[:-3] if stem.endswith(".md") else stem
        head, _, tail = stem.rpartition("-")
        name = _file_name(f"copy-of-{(head or stem)[:30]}-{tail or stem[-6:]}-day{ctx.day}")
        cmds.append(MemoryCommand(command="create", path=f"{NOTES_DIR}/{name}", file_text=body))
        parts.append(f"## {n.get('title', '')}\n{body}")
    if parts:
        everything = _cut_bytes("\n\n".join(parts), limit)
        cmds.append(MemoryCommand(command="create", path=f"{NOTES_DIR}/everything-day{ctx.day}.md", file_text=everything))
    return f"hoarder: copied {len(parts)} notes from day {ctx.day} and appended them to one file", cmds


def _decoy(ctx: StrategyContext, rng: random.Random) -> Plan:
    if ctx.policy_text is None:
        return "decoy: no policy file in this world", []
    if "policy" not in ctx.rights:
        return "decoy: no policy right, nothing rewritten", []
    words = list(DECOY_VOCABULARY)
    rng.shuffle(words)
    target = min(DECOY_CHARS, int(ctx.policy_max_chars * 0.75))
    lines = [f"# Memory doctrine, revision {ctx.day}", ""]
    text = "\n".join(lines)
    i = 0
    while True:
        a, b, c = (words[(i + k) % len(words)] for k in range(3))
        line = "- " + rng.choice(_DECOY_TEMPLATES).format(a=a, b=b, c=c)
        if len(text) + 1 + len(line) > target:
            break
        lines.append(line)
        text = "\n".join(lines)
        i += 3
    if ctx.policy_text == "":
        cmd = MemoryCommand(command="insert", path=POLICY_PATH, insert_line=0, insert_text=text)
    else:
        cmd = MemoryCommand(command="str_replace", path=POLICY_PATH, old_str=ctx.policy_text, new_str=text)
    return f"decoy: rewrote the policy with {len(text)} characters of doctrine", [cmd]


def _summarize(ctx: StrategyContext, rng: random.Random) -> Plan:
    if "notes" not in ctx.rights:
        return "summarize: no notes right, nothing written", []
    o = ctx.outcomes
    lines = [
        f"# Day {ctx.day} summary", "",
        f"Income ${float(o.get('income_usd', 0.0)):.3f} from {int(o.get('forages', 0))} forages; spent "
        f"${float(o.get('spend_usd', 0.0)):.3f} on thinking; {int(o.get('failed_actions', 0))} failed actions; "
        f"{int(o.get('talks', 0))} conversations.",
    ]
    titles: list[str] = []
    for n in _by_age(ctx.new_notes):
        t = str(n.get("title", ""))
        if t and t not in titles:
            titles.append(t)
    if titles:
        lines += ["", "## New notes"] + [f"- [[{t}]]" for t in titles[:MAX_SUMMARY_TITLES]]
    else:
        lines += ["", "No new notes today."]
    text = _cut_bytes("\n".join(lines), min(int(ctx.max_file_bytes), MAX_FILE_TEXT))
    cmd = MemoryCommand(command="create", path=f"{NOTES_DIR}/day-{ctx.day}-summary.md", file_text=text)
    return f"summarize: one summary note for day {ctx.day} with {len(titles)} new titles", [cmd]


STRATEGIES: dict[str, Strategy] = {
    "noop": _noop, "index_builder": _index_builder, "hoarder": _hoarder, "decoy": _decoy, "summarize": _summarize,
}


def plan(strategy: MaintenanceStrategy, ctx: StrategyContext, rng: random.Random) -> MaintenanceResponse:
    """The command batch ``strategy`` issues for ``ctx``, as the structured response a model would return.

    Deterministic given ``ctx`` and ``rng``. The batch is cut to ``ctx.max_commands`` and the schema's cap, the
    reflection names the strategy (it is logged, never shown to an agent), ``adopted_from`` is empty.
    """
    from void.sim.maintenance import MAX_REFLECTION_CHARS, MaintenanceResponse

    fn = STRATEGIES.get(str(strategy))
    if fn is None:
        raise ValueError(f"unknown maintenance strategy {strategy!r}")
    reflection, commands = fn(ctx, rng)
    limit = max(0, min(int(ctx.max_commands), MAX_BATCH))
    return MaintenanceResponse(reflection=reflection[:MAX_REFLECTION_CHARS], commands=commands[:limit], adopted_from=[])
