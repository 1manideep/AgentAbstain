"""The agent-owned memory policy file ``/memories/memory_policy.md`` (MEMORY_EVOLUTION §5.5) `[scaffold]`.

A short markdown file the agent edits nightly: the unit of evolution. Every change is a row in
``policy_versions`` (agent, version, tick, text, parent version, source, source agent, similarity to the
parent) so lineage, adoption and drift are measurable after the fact. The file on disk is the copy an
operator reads in Obsidian; the database row is authoritative.

Sources (§5.7): ``seed`` at birth of a founder, ``inherited`` when a child copies its parent verbatim,
``self_edit`` for the nightly edit, ``adapted`` for the child's first maintenance edit, ``mutated`` for a
kernel-paid random rewrite, ``adopted`` when the kernel measures that a neighbour's suggestion was taken up.

Security (§5.5, §9.2): the policy is rendered in the *user* turn inside the quote fence under a heading
saying the agent wrote it, never in the system prompt, and the system prompt carries exactly one carve-out:
the agent's own practices file is advice it gave itself, to follow unless it conflicts with the rules.

Implementation status: storage, seeds and limits are WP4; the read path works now so prompts can render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from void.config import MemoryPolicyConfig
from void.db import Database
from void.memory.notes import render_frontmatter, split_frontmatter

__all__ = ["PolicySource", "PolicyVersion", "PolicyStore", "SEED_DEFAULT", "SEED_DIVERSE", "similarity"]

PolicySource = Literal["seed", "self_edit", "inherited", "adapted", "mutated", "adopted"]
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

    def __init__(self, db: Database, cfg: MemoryPolicyConfig, vault_root: Path) -> None:
        self.db = db
        self.cfg = cfg
        self.vault_root = Path(vault_root)

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

    def edits_today(self, agent_id: str, day_start_tick: int) -> int:
        row = self.db.fetchone("SELECT COUNT(*) AS n FROM policy_versions WHERE agent_id=? AND tick>=? AND source='self_edit'",
                               (agent_id, int(day_start_tick)))
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
        """Give a newborn its first policy: the parent's (``inherited``) when there is one and inheritance is
        on, else the configured seed (``seed``). Returns None when the policy layer is disabled.

        WP4 fills in the write path (:meth:`set`); the scaffold keeps this a no-op so births stay unchanged.
        """
        if not self.cfg.enabled:
            return None
        raise NotImplementedError("PolicyStore.init_for is work package WP4 (MEMORY_EVOLUTION §10.1)")

    def set(self, agent_id: str, text: str, tick: int, *, source: PolicySource, source_agent_id: str | None = None) -> PolicyVersion:
        """Record a new version (enforcing ``max_chars``), write the file, return the row. WP4."""
        raise NotImplementedError("PolicyStore.set is work package WP4 (MEMORY_EVOLUTION §10.1)")

    # --- file format -----------------------------------------------------------------------------
    @staticmethod
    def render_file(version: PolicyVersion) -> str:
        front = {"version": version.version, "tick": version.tick, "source": version.source}
        return render_frontmatter(front) + version.text.rstrip("\n") + "\n"

    @staticmethod
    def parse_file(text: str) -> tuple[dict[str, object], str]:
        data, rest = split_frontmatter(text)
        return data, rest[:-1] if rest.endswith("\n") else rest
