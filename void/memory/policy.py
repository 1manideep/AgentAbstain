"""The agent-owned memory policy file ``/memories/memory_policy.md`` (MEMORY_EVOLUTION §5.5).

A short markdown file the agent edits nightly: the unit of evolution. Every change is a row in
``policy_versions`` (agent, version, tick, text, parent version, source, source agent, similarity to the
parent) so lineage, adoption and drift are measurable after the fact. The file on disk is the copy an
operator reads in Obsidian; the database row is authoritative.

Sources (§5.7): ``seed`` at birth of a founder, ``inherited`` when a child copies its parent verbatim,
``self_edit`` for the nightly edit, ``adapted`` for the child's first maintenance edit, ``mutated`` for a
kernel-paid random rewrite, ``adopted`` when the kernel measures that a neighbour's suggestion was taken up.

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

import difflib
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
           "FILE_NAME", "similarity", "clean_policy_text"]

PolicySource = Literal["seed", "self_edit", "inherited", "adapted", "mutated", "adopted"]
SOURCES: tuple[PolicySource, ...] = ("seed", "self_edit", "inherited", "adapted", "mutated", "adopted")
# Versions the kernel writes are cut to ``max_chars``; an agent-authored one over the limit is refused instead,
# so the agent learns the limit rather than silently losing the end of its file.
KERNEL_SOURCES: frozenset[str] = frozenset({"seed", "inherited", "mutated"})
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
        """Self-edits recorded on the day of ``tick`` (the ``max_edits_per_day`` budget counts these)."""
        row = self.db.fetchone("SELECT COUNT(*) AS n FROM policy_versions WHERE agent_id=? AND tick>=? AND tick<=? AND source='self_edit'",
                               (agent_id, self.day_start(tick), self.day_start(tick) + self.ticks_per_day - 1))
        return int(row["n"]) if row is not None else 0

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
