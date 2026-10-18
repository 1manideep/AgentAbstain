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

The published-mechanism baselines of §6.13 row 5 (scripted, so they cost what any other arm costs):

* ``distill`` (ALTK-Evolve's distillation): groups of near-duplicate notes (the same normalized title stem, see
  :func:`title_stem`, or cosine >= :data:`DISTILL_COSINE` on the store's hashing embedding of title and body) are
  merged into the group's newest member: the other members are deleted, then that member's body is replaced
  (``str_replace`` of the whole body) by a summary holding, for every node the merged members name, the vault's
  newest fact about it (:func:`format_node_fact`) and, when some members name no node, the newest of those. The
  summary keeps the member's title, path and created tick. The scripted brain's ``node_memory`` dates a note's
  facts by its created tick, so the merge is planned to leave what it reads from the vault unchanged (status,
  position and date per node): every summary agrees with the vault's newest reading; a member is merged only if
  the summary's tick is not later than the newest reading of each node it names (else it stays, rather than
  re-date that node); and notes that hold a node's newest reading while another note of the same tick disagrees
  are left alone (the reader settles such a tie by note id, which a strategy does not see). Index notes, kernel
  anchors and vein notes are never merged (a vein note is a dated event whose repetitions are the evidence that a
  node is vein-prone); a group whose newest member cannot be rewritten verbatim is left alone. The deletes come
  before the edit, so a vault at its file cap needs no free slot; a group too large for the rest of the batch loses
  only its oldest members. Only the structure right is needed.
* ``revert`` (EvolveMem's revert-on-regression): when the day's income (the F1 feedback, ``outcomes``) fell below
  the day before's (``previous_outcomes``), the policy is restored to its previous version's text
  (``previous_policy_text``) with one policy edit; otherwise nothing. Without both fields, or without an earlier
  version, it does nothing: it is a guard, and a world where nothing else edits the policy gives it nothing to undo.
  The nightly step (:meth:`void.sim.maintenance.Maintenance.context_for`) does not fill the two fields yet.
* ``retire`` (Ratchet's retirement plus cap): the oldest notes beyond :data:`RETIRE_CAP` are deleted, oldest first;
  index notes, kernel anchors and ``self`` are never retired or counted.

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
from void.memory.commands import (
    INDEX_DIR,
    MAX_FILE_TEXT,
    NOTES_DIR,
    POLICY_PATH,
    SELF_PATH,
    MemoryCommand,
)
from void.memory.embed import HashingEmbedder
from void.memory.notes import content_body

if TYPE_CHECKING:
    from void.sim.maintenance import MaintenanceResponse

__all__ = ["StrategyContext", "plan", "nodes_from_notes", "listed_files", "index_file_name", "index_body",
           "title_stem", "format_node_fact", "near_duplicate_groups", "DECOY_VOCABULARY", "STRATEGIES",
           "DISTILL_COSINE", "RETIRE_CAP"]

# Copied from void.brain.scripted (``_NODE_POS_RE`` / ``_BUCKET_RE``): "[[Node n3]] at (12, -5) ... rich".
_NODE_POS_RE = re.compile(r"node\s+([A-Za-z0-9_-]+)\]?\]?\s+at\s+\((-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\)", re.IGNORECASE)
_BUCKET_RE = re.compile(r"\b(rich|thin|empty)\b", re.IGNORECASE)
_SECTION_RE = re.compile(r"^(?:/memories/)?(notes|index)/$")
_FACT_DAY_RE = re.compile(r"^\s*on\s+day\s+(\d+)", re.IGNORECASE)   # "... at (12, -5) on day 3; ..."
# What the command layer's cleaning rewrites in an ``old_str`` (control characters, angle brackets, carriage returns):
# a body holding any of these would not be found verbatim, so distill does not rewrite it.
_REWRITTEN_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f<>\r]")
_STEM_TOKEN_RE = re.compile(r"[a-z0-9]+")

MAX_BATCH = 30               # the schema's own cap on ``commands`` (MaintenanceResponse)
MAX_INDEX_LINKS = 40         # related titles listed per index file
MAX_SUMMARY_TITLES = 40      # new-note titles listed in the summary note
MAX_FILE_NAME = 79           # characters before ``.md`` the command layer accepts
DECOY_CHARS = 1500           # target length of the decoy's policy text (under max_chars 2000)
DISTILL_COSINE = 0.9         # the near-duplicate threshold of the near_duplicate_rate outcome (§7.2)
DISTILL_EMBEDDING_DIM = 256  # memory.embedding_dim's default: the space distill compares notes in
MAX_LATEST_CHARS = 300       # characters of a member that names no node a distilled summary keeps
RETIRE_CAP = 40              # notes a retiring vault keeps (index notes and kernel anchors are not counted)
_KERNEL_TAGS = frozenset({"entity", "probe"})
_NOT_INDEXED_TAGS = frozenset({"entity", "probe", "index"})
_NEVER_TOUCHED_TAGS = frozenset({"entity", "probe", "index", "self"})  # never merged, never retired
_STEM_DROP = frozenset({"on", "day", "rich", "thin", "empty"})  # dates and status words, not topics

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

    ``previous_outcomes`` is the ``outcomes`` dict of the day before ``day`` (the feedback the previous night's
    call saw) and ``previous_policy_text`` the text of the policy version before the current one; both are None
    when unknown or when there is none, and only ``revert`` reads them.
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
    previous_outcomes: dict[str, Any] | None = None
    previous_policy_text: str | None = None


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


def _budget(ctx: StrategyContext) -> int:
    return max(0, min(int(ctx.max_commands), MAX_BATCH))


def _touchable(n: dict[str, Any]) -> bool:
    """A note a strategy may merge or retire: not an index note, a kernel anchor or ``self``."""
    path = str(n.get("path", ""))
    return not (_NEVER_TOUCHED_TAGS & set(n.get("tags", ()))) and not path.startswith(INDEX_DIR + "/") and path != SELF_PATH


# --- the shared pieces of the 6.13 baselines -----------------------------------------------------------------
def title_stem(title: str) -> str:
    """A title without its dates, counters and status words: ``Node n3 seen rich on day 3-2`` -> ``node n3 seen``.

    Lowercased word tokens, dropping pure numbers (days, ticks, the vault's ``-2`` suffix of a repeated title) and
    :data:`_STEM_DROP`. Notes about the same thing on different days share a stem; a distilled summary keeps the
    title of the member it was written into, so it is merged again with later notes of its stem.
    """
    return " ".join(t for t in _STEM_TOKEN_RE.findall(title.lower()) if not t.isdigit() and t not in _STEM_DROP)


def _num(v: float) -> str:
    return f"{v + 0.0:g}"  # + 0.0 turns -0.0 into 0.0


def format_node_fact(node_id: str, x: float, y: float, bucket: str, day: int | None) -> str:
    """One node fact in the sighting shape the scripted brain and :func:`nodes_from_notes` both read:
    ``Saw [[Node n3]] at (12, -5) on day 3; it was rich.`` (no day clause when the day is unknown)."""
    when = "" if day is None else f" on day {int(day)}"
    return f"Saw [[Node {node_id}]] at ({_num(x)}, {_num(y)}){when}; it was {bucket}."


Fact = tuple[float, float, str, int | None]  # (x, y, bucket, day) of one node mention


def _first_mentions(n: dict[str, Any]) -> dict[str, Fact]:
    """``node_id -> (x, y, bucket, day)`` for every node one note mentions with a position, read as the scripted
    brain's ``node_memory`` reads a note: title and body, bucket ``thin`` when none follows the position, the first
    mention of a node counting; ``day`` when the mention says ``on day N`` right after the position."""
    text = f"{n.get('title', '')} {n.get('body', '')}"
    out: dict[str, Fact] = {}
    for hit in _NODE_POS_RE.finditer(text):
        if hit.group(1) in out:
            continue
        tail = text[hit.end(): hit.end() + 80]
        b = _BUCKET_RE.search(tail)
        d = _FACT_DAY_RE.match(tail)
        out[hit.group(1)] = (float(hit.group(2)), float(hit.group(3)), b.group(1).lower() if b else "thin",
                             int(d.group(1)) if d else None)
    return out


def _node_facts(notes: list[dict[str, Any]]) -> dict[str, Fact]:
    """``node_id -> (x, y, bucket, day)``: the newest mention per node, notes ordered by created tick (the reader's
    recency), then by path (the reader breaks that tie by note id, which a note dict does not carry; see
    :func:`_newest_ticks` for how distill stays clear of it)."""
    found: dict[str, Fact] = {}
    for n in _by_age(notes):
        found.update(_first_mentions(n))
    return found


def _newest_ticks(notes: list[dict[str, Any]]) -> dict[str, tuple[int, bool]]:
    """``node_id -> (tick, split)``: the created tick of the newest note mentioning the node, and whether the notes of
    that tick disagree about it (position or bucket). On a split the reader's answer turns on note-id order."""
    found: dict[str, tuple[int, set[tuple[float, float, str]]]] = {}
    for n in notes:
        t = int(n.get("tick", 0))
        for nid, (x, y, bucket, _) in _first_mentions(n).items():
            cur = found.get(nid)
            if cur is None or t > cur[0]:
                found[nid] = (t, {(x, y, bucket)})
            elif t == cur[0]:
                cur[1].add((x, y, bucket))
    return {nid: (t, len(seen) > 1) for nid, (t, seen) in found.items()}


def _sparse_unit(text: str, embedder: HashingEmbedder) -> dict[int, float]:
    return {i: v for i, v in enumerate(embedder.embed(text)) if v != 0.0}


def _dot(a: dict[int, float], b: dict[int, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b[i] for i, v in a.items() if i in b)


def near_duplicate_groups(notes: list[dict[str, Any]], *, cosine: float = DISTILL_COSINE,
                          dim: int = DISTILL_EMBEDDING_DIM) -> list[list[dict[str, Any]]]:
    """Groups of two or more notes that are near-duplicates, each oldest first; the groups largest first.

    Two notes are linked when their :func:`title_stem` is the same (and not empty) or when the cosine of the
    store's hashing embedding of ``title + content body`` (:meth:`void.memory.store.MemoryStore._embed`) is at
    least ``cosine``; a group is a connected component of those links. Pure: the order of ``notes`` does not matter.
    """
    items = _by_age(notes)
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    by_stem: dict[str, int] = {}
    for i, n in enumerate(items):
        stem = title_stem(str(n.get("title", "")))
        if stem:
            if stem in by_stem:
                union(by_stem[stem], i)
            else:
                by_stem[stem] = i
    embedder = HashingEmbedder(dim)
    vecs = [_sparse_unit(f"{n.get('title', '')} {content_body(str(n.get('body', '')))}", embedder) for n in items]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if find(i) != find(j) and vecs[i] and vecs[j] and _dot(vecs[i], vecs[j]) >= cosine:
                union(i, j)
    groups: dict[int, list[dict[str, Any]]] = {}
    for i, n in enumerate(items):
        groups.setdefault(find(i), []).append(n)
    out = [g for _, g in sorted(groups.items()) if len(g) >= 2]
    return sorted(out, key=lambda g: (-len(g), int(g[0].get("tick", 0)), str(g[0].get("path", ""))))


def _distilled_body(ctx: StrategyContext, stem: str, members: list[dict[str, Any]], vault_facts: dict[str, Fact]) -> str:
    """The summary of merged ``members`` (oldest first): for every node they name, the vault's newest fact about it
    (``vault_facts``, not the members' own, which may be older than a note outside the group), and, when members
    name no node, the newest of those."""
    named = _node_facts(members)
    facts = {nid: vault_facts.get(nid, named[nid]) for nid in named}
    lines = [f"# Distilled: {stem or 'notes'} (day {ctx.day})", f"Merged {len(members)} notes; the newest fact per node."]
    lines += [f"- {format_node_fact(nid, *facts[nid])}" for nid in sorted(facts)]
    factless = [n for n in members if not _NODE_POS_RE.search(f"{n.get('title', '')} {n.get('body', '')}")]
    if factless:
        latest = " ".join(content_body(str(factless[-1].get("body", ""))).split())[:MAX_LATEST_CHARS]
        if latest:
            lines.append(f"- Latest: {latest}")
    return _cut_bytes("\n".join(lines), min(int(ctx.max_file_bytes), MAX_FILE_TEXT))


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


def _rewritable(body: str) -> bool:
    """Whether ``body`` can be the ``old_str`` of a whole-body ``str_replace``: present, within the command schema's
    cap, and nothing in it the command layer's cleaning would rewrite (else the edit would not find it)."""
    return bool(body.strip()) and len(body) <= MAX_FILE_TEXT and _REWRITTEN_RE.search(body) is None


def _distill(ctx: StrategyContext, rng: random.Random) -> Plan:
    if "structure" not in ctx.rights:
        return "distill: merging needs the structure right, nothing merged", []
    pool = [n for n in ctx.notes if _touchable(n) and "vein" not in set(n.get("tags", ())) and str(n.get("body", "")).strip()]
    groups = near_duplicate_groups(pool)
    # every note the scripted brain can recall (all but the entity anchors retrieval never returns): the newest
    # reading per node, and the tick it dates from
    readable = [n for n in ctx.notes if "entity" not in set(n.get("tags", ()))]
    vault_facts = _node_facts(readable)
    newest = _newest_ticks(readable)

    def pinned(n: dict[str, Any]) -> bool:
        """A note holding a split newest reading: the reader's pick among such notes is by note id, so none is
        merged, rewritten or outdated by a summary on its tick."""
        t = int(n.get("tick", 0))
        return any(newest[nid] == (t, True) for nid in _first_mentions(n))

    def safe(n: dict[str, Any], at: int) -> bool:
        """Whether a summary dated ``at`` may restate ``n``'s nodes: never later than a node's newest reading (that
        would re-date it) and never on the tick of a split one."""
        return all(newest[nid][0] > at or (newest[nid][0] == at and not newest[nid][1]) for nid in _first_mentions(n))

    budget = _budget(ctx)
    cmds: list[MemoryCommand] = []
    merged = deleted = 0
    for group in groups:
        room = budget - len(cmds)
        if room < 2:  # a merge is at least one delete and the edit
            break
        members = [n for n in group if not pinned(n)]
        if len(members) < 2:
            continue
        keeper = members[-1]  # the newest member: its created tick dates the summary for the reader
        old = str(keeper.get("body", ""))
        at = int(keeper.get("tick", 0))
        # a group larger than the batch loses only its oldest members
        sources = [n for n in members[:-1] if safe(n, at)][: room - 1]
        if not sources or not _rewritable(old):
            continue
        body = _distilled_body(ctx, title_stem(str(keeper.get("title", ""))), sources + [keeper], vault_facts)
        # the deletes come first: the edit needs no free file slot, and the sources' bytes are freed before it
        cmds += [MemoryCommand(command="delete", path=str(n["path"])) for n in sources]
        if body != old:
            cmds.append(MemoryCommand(command="str_replace", path=str(keeper["path"]), old_str=old, new_str=body))
        deleted += len(sources)
        merged += 1
    return (f"distill: {len(groups)} groups of near-duplicates among {len(pool)} notes, {merged} merged into their "
            f"newest note, {deleted} sources deleted"), cmds


def _revert(ctx: StrategyContext, rng: random.Random) -> Plan:
    if ctx.policy_text is None:
        return "revert: no policy file in this world", []
    if "policy" not in ctx.rights:
        return "revert: no policy right, nothing restored", []
    today = float(ctx.outcomes.get("income_usd", 0.0))
    before = ctx.previous_outcomes
    if before is None or before.get("income_usd") is None:
        return f"revert: no earlier day to compare day {ctx.day}'s income ${today:.3f} with, kept the policy", []
    yesterday = float(before["income_usd"])
    if today >= yesterday:
        return f"revert: income ${today:.3f} on day {ctx.day} did not fall below ${yesterday:.3f}, kept the policy", []
    old = ctx.previous_policy_text
    if old is None or old == ctx.policy_text:
        return f"revert: income fell from ${yesterday:.3f} to ${today:.3f} but there is no earlier policy to restore", []
    if ctx.policy_text == "":
        cmd = MemoryCommand(command="insert", path=POLICY_PATH, insert_line=0, insert_text=old)
    else:
        cmd = MemoryCommand(command="str_replace", path=POLICY_PATH, old_str=ctx.policy_text, new_str=old)
    return f"revert: income fell from ${yesterday:.3f} to ${today:.3f}, restored the previous policy version", [cmd]


def _retire(ctx: StrategyContext, rng: random.Random) -> Plan:
    if "structure" not in ctx.rights:
        return "retire: no structure right, nothing deleted", []
    pool = [n for n in _by_age(ctx.notes) if _touchable(n)]
    over = len(pool) - RETIRE_CAP
    if over <= 0:
        return f"retire: {len(pool)} notes, within the cap of {RETIRE_CAP}", []
    victims = pool[: min(over, _budget(ctx))]
    cmds = [MemoryCommand(command="delete", path=str(n["path"])) for n in victims]
    return f"retire: {len(pool)} notes over the cap of {RETIRE_CAP}, deleted the {len(victims)} oldest", cmds


STRATEGIES: dict[str, Strategy] = {
    "noop": _noop, "index_builder": _index_builder, "hoarder": _hoarder, "decoy": _decoy, "summarize": _summarize,
    "distill": _distill, "revert": _revert, "retire": _retire,
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
