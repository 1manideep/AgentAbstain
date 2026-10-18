"""The agent-owned memory policy file ``/memories/memory_policy.md`` (MEMORY_EVOLUTION §5.5).

A short markdown file the agent edits nightly: the unit of evolution. Every change is a row in
``policy_versions`` (agent, version, tick, text, parent version, source, source agent, similarity to the
parent) so lineage, adoption and drift are measurable after the fact. The file on disk is the copy an
operator reads in Obsidian; the database row is authoritative.

Sources (§5.7): ``seed`` at birth of a founder, ``inherited`` when a child copies its parent verbatim,
``self_edit`` for the nightly edit, ``adapted`` for the child's first maintenance edit, ``mutated`` for a
kernel-paid random rewrite, ``adopted`` when the kernel measures that a neighbour's suggestion was taken up.

The command layer writes every agent edit as ``self_edit``; the kernel's lineage labels are applied afterwards
through :meth:`PolicyStore.relabel`, which the nightly step calls (``adapted`` when :meth:`PolicyStore.inherited_from`
names a parent, ``adopted`` when it measures a suggestion in the new text). ``self_edit``, ``adapted`` and
``adopted`` are the agent-authored sources (:data:`AGENT_SOURCES`): all three count toward the daily edit budget.
:func:`mutate_text` is the kernel's scripted mutation at birth (``mutated``): drop, duplicate or move one practice
line, drawn from the caller's RNG.

Write path: every version passes through :meth:`PolicyStore.set`, which cleans the text (control characters
and angle brackets neutralised, a spoofed frontmatter block dropped, trailing whitespace removed), enforces
``max_chars`` (an agent-authored version over the limit is refused so the agent is told; a kernel-written one
is cut) and the daily self-edit budget, then inserts the row and mirrors the file in one transaction. A
child's first version is its parent's current text verbatim (``inherited``, ``parent_version`` None because
version numbers are per agent, ``similarity_to_parent`` measured against the parent's text); a founder's is
the configured seed. The store draws no randomness and emits no events: the caller emits ``POLICY_CHANGED``
from :meth:`PolicyStore.event_payload`.

Security (§5.5, §9.2): the policy is rendered in the *user* turn inside the quote fence under a heading
saying the agent wrote it, never in the system prompt, and the system prompt carries exactly one carve-out:
the agent's own practices file is advice it gave itself, to follow unless it conflicts with the rules.
"""

from __future__ import annotations

import dataclasses
import difflib
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from void.brain.decision import sanitize_text
from void.brain.prompt import fence
from void.config import MemoryPolicyConfig
from void.db import Database
from void.memory.notes import render_frontmatter, split_frontmatter

__all__ = ["PolicySource", "PolicyVersion", "PolicyStore", "SEED_DEFAULT", "SEED_DIVERSE", "SOURCES", "KERNEL_SOURCES",
           "AGENT_SOURCES", "MUTATIONS", "FILE_NAME", "similarity", "clean_policy_text", "mutate_text"]

PolicySource = Literal["seed", "self_edit", "inherited", "adapted", "mutated", "adopted"]
SOURCES: tuple[PolicySource, ...] = ("seed", "self_edit", "inherited", "adapted", "mutated", "adopted")
# Versions the kernel writes are cut to ``max_chars``; an agent-authored one over the limit is refused instead,
# so the agent learns the limit rather than silently losing the end of its file.
KERNEL_SOURCES: frozenset[str] = frozenset({"seed", "inherited", "mutated"})
# Versions the agent wrote itself: the command layer records them as ``self_edit`` and the kernel may relabel them
# ``adapted`` or ``adopted`` (its measured lineage); all three count toward ``max_edits_per_day``.
AGENT_SOURCES: frozenset[str] = frozenset({"self_edit", "adapted", "adopted"})
MUTATIONS: tuple[str, ...] = ("drop", "duplicate", "reorder")
FILE_NAME = "memory_policy.md"

SEED_DEFAULT = """# How I remember (v1)

- At the end of each day, write one note per important fact: who, what, where, when.
- Link every note to the node or agent it is about, like [[Node n3]] or [[Ada]].
- Keep one index note per node listing what I know about it. Update it, do not duplicate it.
- If two notes disagree, keep the newer one and write down where each came from.
- Delete notes that turned out to be wrong.
"""

# S2: ten deliberately different hand-written practices, one per founder, to test whether initial diversity
# speeds evolution (§5.5). Kept short; the point is variety of *strategy*, not of wording.
SEED_DIVERSE: tuple[str, ...] = (
    "# Places first\n\n- One note per node: where it is, how rich it was, when I last saw it.\n- Before moving, read my node notes and go to the richest one I have not visited today.\n",
    "# People first\n\n- One note per agent: what they told me and whether it turned out true.\n- Trust agents whose claims were right; ignore the rest.\n",
    "# Daily ledger\n\n- Each night, one note: income, spend, what worked, what failed.\n- Keep the last five; delete older ones.\n",
    "# Rules of thumb\n\n- Keep a single note called Lessons with short rules I have learned.\n- Edit it; never add a second lessons note.\n",
    "# Index everything\n\n- Maintain an index note that links to every other note with one line each.\n- Rebuild it nightly.\n",
    "# Forget fast\n\n- Delete any note older than two days unless it names a node or a person.\n",
    "# Sources\n\n- For every fact I write, record who said it and when.\n- A fact from one source is a rumour; from two it is knowledge.\n",
    "# Routes\n\n- Note the order in which I visited nodes and what each paid.\n- Repeat the best route; change one step when it stops paying.\n",
    "# Warnings only\n\n- Write notes only about dangers: empty nodes, liars, bad weather.\n- Everything else I can see for myself.\n",
    "# Questions\n\n- Each night write down one thing I wish I had known today, and where I could learn it.\n",
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def similarity(a: str, b: str) -> float:
    """Token-set Jaccard similarity (the placeholder measure until a real embedder exists, §5.7)."""
    sa, sb = set(_WORD_RE.findall(a.lower())), set(_WORD_RE.findall(b.lower()))
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb) if (sa | sb) else 0.0


def clean_policy_text(text: str) -> str:
    """Canonical form of a policy text, as stored and rendered.

    The text is agent-authored and shown to models, so control characters are dropped and angle brackets
    neutralised (:func:`sanitize_text`, newlines kept). Every leading frontmatter block that parses to a
    mapping is removed: the file's header (version, tick, source) is kernel-owned and must not be spoofed.
    Line endings are normalised, trailing whitespace dropped from every line and surrounding blank lines
    removed, so the file format round-trips (:meth:`PolicyStore.render_file` appends exactly one newline).
    Idempotent: cleaning a clean text changes nothing.
    """
    out = sanitize_text(text.replace("\r\n", "\n").replace("\r", "\n"))
    out = "\n".join(line.rstrip() for line in out.split("\n")).strip("\n")
    while True:
        data, rest = split_frontmatter(out)
        if not data:
            return out
        out = rest.strip("\n")


def mutate_text(text: str, rng: random.Random) -> tuple[str, str] | None:
    """The kernel's scripted mutation at birth (§5.7 ``mutated``): one small random change to a policy's practice lines
    (non-empty lines that are not markdown headings; a heading names the file, it is no practice).

    The operation is drawn uniformly from those that apply (:data:`MUTATIONS`): ``drop`` removes one practice line,
    ``duplicate`` repeats one right after itself, ``reorder`` moves one to another practice line's place (needs two).
    Returns ``(new_text, operation)``, or None when the text has no practice line or the change leaves it as it was
    (moving a line among identical ones). Deterministic given ``rng``; every other line keeps its place and form.
    """
    lines = text.split("\n")
    units = [i for i, line in enumerate(lines) if line.strip() and not line.strip().startswith("#")]
    if not units:
        return None
    ops = [op for op in MUTATIONS if op != "reorder" or len(units) >= 2]
    op = rng.choice(ops)
    i = rng.choice(units)
    out = list(lines)
    if op == "drop":
        del out[i]
    elif op == "duplicate":
        out.insert(i + 1, lines[i])
    else:  # reorder: the line leaves its place and takes another practice line's, the rest keep their order
        j = rng.choice([u for u in units if u != i])
        line = out.pop(i)
        out.insert(j, line)
    new = "\n".join(out)
    return (new, op) if clean_policy_text(new) != clean_policy_text(text) else None


def _word_diff(old: list[str], new: list[str]) -> tuple[int, int]:
    """``(added, removed)`` word counts between two word lists."""
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


@dataclass(frozen=True)
class PolicyVersion:
    agent_id: str
    version: int
    tick: int
    text: str
    parent_version: int | None
    source: PolicySource
    source_agent_id: str | None
    similarity_to_parent: float | None


class PolicyStore:
    """Read and write policies; one ``policy_versions`` row per change and the file mirrored into the vault."""

    def __init__(self, db: Database, cfg: MemoryPolicyConfig, vault_root: Path, *, ticks_per_day: int = 24) -> None:
        self.db = db
        self.cfg = cfg
        self.vault_root = Path(vault_root)
        self.ticks_per_day = max(1, int(ticks_per_day))

    # --- read ------------------------------------------------------------------------------------
    def path_for(self, agent_id: str) -> Path:
        return self.vault_root / agent_id / FILE_NAME

    def current(self, agent_id: str) -> PolicyVersion | None:
        row = self.db.fetchone(
            "SELECT agent_id, version, tick, text, parent_version, source, source_agent_id, similarity_to_parent "
            "FROM policy_versions WHERE agent_id=? ORDER BY version DESC LIMIT 1", (agent_id,))
        return None if row is None else PolicyVersion(**{k: row[k] for k in row.keys()})

    def text(self, agent_id: str) -> str | None:
        """The current policy text, or None when the agent has none (policy disabled or not yet seeded)."""
        cur = self.current(agent_id)
        return None if cur is None else cur.text

    def history(self, agent_id: str) -> list[PolicyVersion]:
        rows = self.db.fetchall(
            "SELECT agent_id, version, tick, text, parent_version, source, source_agent_id, similarity_to_parent "
            "FROM policy_versions WHERE agent_id=? ORDER BY version", (agent_id,))
        return [PolicyVersion(**{k: r[k] for k in r.keys()}) for r in rows]

    def render_for_prompt(self, agent_id: str) -> str:
        """The current policy fenced for a model (the maintenance prompt), or ``(no policy)`` when there is none."""
        text = self.text(agent_id)
        return fence(text) if text else "(no policy)"

    def timeline(self, agent_id: str) -> list[dict[str, object]]:
        """One dict per version for the CLI: source, lineage, size and a word-level diff against the previous
        version (``words_added`` / ``words_removed``; version 1 is diffed against nothing)."""
        out: list[dict[str, object]] = []
        prev_words: list[str] = []
        for v in self.history(agent_id):
            words = v.text.split()
            added, removed = _word_diff(prev_words, words)
            out.append({
                "version": v.version, "tick": v.tick, "source": v.source, "source_agent_id": v.source_agent_id,
                "parent_version": v.parent_version, "similarity_to_parent": v.similarity_to_parent,
                "chars": len(v.text), "words": len(words), "words_added": added, "words_removed": removed,
            })
            prev_words = words
        return out

    def day_start(self, tick: int) -> int:
        """First tick of the day containing ``tick`` (ticks are 1-based; day d spans (d-1)*T+1 .. d*T)."""
        t = max(1, int(tick))
        return ((t - 1) // self.ticks_per_day) * self.ticks_per_day + 1

    def edits_today(self, agent_id: str, tick: int) -> int:
        """Versions the agent wrote on the day of ``tick`` (:data:`AGENT_SOURCES`: a ``self_edit`` the kernel relabelled
        ``adapted`` or ``adopted`` still counts; the ``max_edits_per_day`` budget counts these)."""
        row = self.db.fetchone("SELECT COUNT(*) AS n FROM policy_versions WHERE agent_id=? AND tick>=? AND tick<=? "
                               "AND source IN ('self_edit','adapted','adopted')",
                               (agent_id, self.day_start(tick), self.day_start(tick) + self.ticks_per_day - 1))
        return int(row["n"]) if row is not None else 0

    def inherited_from(self, agent_id: str, version: int) -> str | None:
        """The parent to credit when ``version`` is the agent's first own edit of the policy it inherited at birth
        (§5.7 ``adapted``): its first version is ``inherited`` and every version before ``version`` was written by the
        kernel (``inherited``, ``mutated``). None otherwise: a founder, a transplant recipient (its first version is
        its seed), or an agent that has edited its policy before."""
        rows = self.db.fetchall("SELECT version, source, source_agent_id FROM policy_versions WHERE agent_id=? AND version<? "
                                "ORDER BY version", (agent_id, int(version)))
        if not rows or rows[0]["source"] != "inherited" or any(r["source"] in AGENT_SOURCES for r in rows):
            return None
        return rows[0]["source_agent_id"]

    # --- write -----------------------------------------------------------------------------------
    def seed_text(self, index: int) -> str:
        """The founder seed for roster position ``index`` under ``cfg.seed``."""
        if self.cfg.seed == "blank":
            return ""
        if self.cfg.seed == "diverse":
            return SEED_DIVERSE[index % len(SEED_DIVERSE)]
        return SEED_DEFAULT

    def init_for(self, agent_id: str, tick: int, *, roster_index: int = 0, parent_id: str | None = None) -> PolicyVersion | None:
        """Give a newborn its first policy: the parent's current text verbatim (``inherited``) when there is one
        and inheritance is on, else the configured seed for ``roster_index`` (``seed``; a blank seed still
        creates version 1 with empty text, so "has a policy" is well defined). Returns None when the policy
        layer is disabled. Idempotent: an agent that already has a version keeps it and gets it back.
        """
        if not self.cfg.enabled:
            return None
        cur = self.current(agent_id)
        if cur is not None:
            return cur
        if parent_id is not None and self.cfg.inherit:
            parent = self.current(parent_id)
            if parent is not None:
                return self.set(agent_id, parent.text, tick, source="inherited", source_agent_id=parent_id)
        return self.set(agent_id, self.seed_text(roster_index), tick, source="seed")

    def set(self, agent_id: str, text: str, tick: int, *, source: PolicySource, source_agent_id: str | None = None) -> PolicyVersion:
        """Record a new version and mirror the file; returns the row.

        The text is cleaned (:func:`clean_policy_text`). Over ``max_chars`` it is cut for a kernel source and
        refused (``ValueError``) for an agent-authored one; a ``self_edit`` beyond ``max_edits_per_day`` is
        refused too. ``similarity_to_parent`` is measured against the agent's previous version, or, for a
        first version copied from ``source_agent_id``, against that agent's current text (1.0 when verbatim).
        The row and the file are written in one transaction, so a failed write leaves no version behind.
        """
        if source not in SOURCES:
            raise ValueError(f"unknown policy source {source!r}")
        clean = clean_policy_text(text)
        if len(clean) > self.cfg.max_chars:
            if source in KERNEL_SOURCES:
                clean = clean[: self.cfg.max_chars].rstrip()
            else:
                raise ValueError(f"policy is {len(clean)} characters; the limit is {self.cfg.max_chars}")
        with self.db.tx():
            if source == "self_edit" and self.edits_today(agent_id, tick) >= self.cfg.max_edits_per_day:
                raise ValueError("daily policy edit budget spent")
            prev = self.current(agent_id)
            origin = self.current(source_agent_id) if prev is None and source_agent_id is not None else None
            if prev is not None:
                sim: float | None = similarity(prev.text, clean)
            elif origin is not None:
                sim = similarity(origin.text, clean)
            else:
                sim = None
            version = PolicyVersion(
                agent_id=agent_id, version=prev.version + 1 if prev is not None else 1, tick=int(tick), text=clean,
                parent_version=prev.version if prev is not None else None, source=source,
                source_agent_id=source_agent_id, similarity_to_parent=sim,
            )
            self.db.execute(
                "INSERT INTO policy_versions(agent_id, version, tick, text, parent_version, source, source_agent_id, similarity_to_parent) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (version.agent_id, version.version, version.tick, version.text, version.parent_version, version.source,
                 version.source_agent_id, version.similarity_to_parent),
            )
            path = self.path_for(agent_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.render_file(version), encoding="utf-8")
        return version

    def relabel(self, agent_id: str, version: int, *, source: PolicySource, source_agent_id: str | None) -> PolicyVersion:
        """Re-attribute a version the agent wrote to the kernel's measured lineage (§5.7): ``adapted`` for a child's first
        own edit of its inherited policy, ``adopted`` when a neighbour's suggestion is measured in it. Only an
        agent-authored version (:data:`AGENT_SOURCES`) may be relabelled, and only to an agent-authored source; text,
        tick, parent and similarity stay as written. The file mirror follows when ``version`` is the current one.
        Raises ``ValueError`` otherwise. Returns the relabelled row.
        """
        if source not in AGENT_SOURCES:
            raise ValueError(f"only an agent-authored source may be given by relabelling, not {source!r}")
        with self.db.tx():
            row = self.db.fetchone(
                "SELECT agent_id, version, tick, text, parent_version, source, source_agent_id, similarity_to_parent "
                "FROM policy_versions WHERE agent_id=? AND version=?", (agent_id, int(version)))
            if row is None:
                raise ValueError(f"{agent_id} has no policy version {version}")
            old = PolicyVersion(**{k: row[k] for k in row.keys()})
            if old.source not in AGENT_SOURCES:
                raise ValueError(f"version {version} of {agent_id} was written by the kernel ({old.source}); it cannot be relabelled")
            new = dataclasses.replace(old, source=source, source_agent_id=source_agent_id)
            self.db.execute("UPDATE policy_versions SET source=?, source_agent_id=? WHERE agent_id=? AND version=?",
                            (new.source, new.source_agent_id, agent_id, int(version)))
            cur = self.current(agent_id)
            if cur is not None and cur.version == new.version:
                self.path_for(agent_id).write_text(self.render_file(new), encoding="utf-8")
        return new

    @staticmethod
    def event_payload(version: PolicyVersion) -> dict[str, object]:
        """What the caller emits as ``POLICY_CHANGED`` for ``version`` (the text itself stays in the table)."""
        return {
            "agent_id": version.agent_id, "version": version.version, "source": version.source,
            "source_agent_id": version.source_agent_id, "similarity_to_parent": version.similarity_to_parent,
            "chars": len(version.text),
        }

    # --- file format -----------------------------------------------------------------------------
    @staticmethod
    def render_file(version: PolicyVersion) -> str:
        """The on-disk mirror: a kernel-owned header (version, tick, source) and the text plus one newline."""
        front = {"version": version.version, "tick": version.tick, "source": version.source}
        return render_frontmatter(front) + version.text.rstrip("\n") + "\n"

    @staticmethod
    def parse_file(text: str) -> tuple[dict[str, object], str]:
        """Inverse of :meth:`render_file`: ``(header fields, policy text)``."""
        data, rest = split_frontmatter(text)
        return data, rest[:-1] if rest.endswith("\n") else rest
