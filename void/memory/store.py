"""MemoryStore: the facade the simulation uses for agent memory.

It ties together the on-disk vault (source of truth, :mod:`void.memory.vault`), the
``notes`` / ``note_links`` / ``self_versions`` index tables, the deterministic embedder and
the wikilink graph walk. Every write goes through one ``db.tx()`` so a crash never leaves
the index and the vault disagreeing for longer than a ``reindex``.

Retrieval (DESIGN §10): cosine top-``entry_k`` of the agent's own non-archived notes are the
entry points; in ``graph`` mode a BFS follows wikilinks for ``hops`` hops with ``hop_decay``;
the final rank is ``walk_score * (0.5 + importance) * recency_weight``. Only the querying
agent's embeddings are ever loaded. Entity stubs are link anchors: they are walked through
but never returned.

Auto-linking (A-MEM): a new note is linked both ways to the ``auto_link_k`` most similar
existing notes above ``auto_link_min_sim`` via ``## Related`` sections. Embeddings are taken
over a note's *content* (title + body without that section), so adding a backlink to a file
never changes its embedding and ``reindex`` reproduces the index byte for byte.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from sqlite3 import Row

from void.config import MemoryConfig
from void.db import Database
from void.ids import IdFactory
from void.memory.embed import Embedder, HashingEmbedder, cosine, pack, unpack
from void.memory.graph import NoteGraph
from void.memory.notes import (
    CHANNELS,
    DEFAULT_CHANNEL,
    DEFAULT_TITLE,
    Note,
    Provenance,
    add_related_links,
    content_body,
    normalise_title,
    parse_note,
    parse_wikilinks,
    strip_wikilinks,
)
from void.memory.vault import Vault
from void.types import RetrievedNote

__all__ = [
    "MemoryStore",
    "MIN_RECENCY_WEIGHT",
    "TITLE_WORDS",
    "TITLE_MAX_CHARS",
    "ENTITY_TAG",
    "SELF_TAG",
    "PROBE_TAG",
    "TRACER_TAGS",
    "TRACER_ORIGIN",
]

MIN_RECENCY_WEIGHT = 0.05
TITLE_WORDS = 6
TITLE_MAX_CHARS = 80

ENTITY_TAG = "entity"
SELF_TAG = "self"
PROBE_TAG = "probe"
TRACER_TAGS: tuple[str, ...] = ("strategy", "tracer")
TRACER_ORIGIN = "tracer"

_AUTO_LINK_EXCLUDED = frozenset({ENTITY_TAG, SELF_TAG})
_PROBE_ANCHOR_EXCLUDED = frozenset({ENTITY_TAG, SELF_TAG, PROBE_TAG})

_NOTE_COLUMNS = (
    "note_id, agent_id, path, title, created_tick, importance, tags, channel, "
    "source_agent_id, hop, origin_note_id, origin_generation, transfer_tick, path_agents, archived"
)


class MemoryStore:
    def __init__(
        self,
        db: Database,
        cfg: MemoryConfig,
        ids: IdFactory,
        vault_root: Path,
        embedder: Embedder | None = None,
    ) -> None:
        self.db = db
        self.cfg = cfg
        self.ids = ids
        self.vault_root = Path(vault_root)
        self.vault_root.mkdir(parents=True, exist_ok=True)
        self.embedder: Embedder = embedder if embedder is not None else HashingEmbedder(cfg.embedding_dim)

    # --- vault access -----------------------------------------------------------------
    def vault_for(self, agent_id: str) -> Vault:
        return Vault(self.vault_root, agent_id)

    # --- writing ----------------------------------------------------------------------
    def remember(
        self,
        agent_id: str,
        tick: int,
        text: str,
        *,
        title: str | None = None,
        links_to: Sequence[str] = (),
        tags: Sequence[str] = (),
        importance: float = 0.5,
        provenance: Provenance | None = None,
        channel: str = DEFAULT_CHANNEL,
        auto_link: bool = True,
    ) -> Note:
        """Write a new note to the agent's vault, index it and auto-link it. One transaction.

        ``provenance`` wins over ``channel`` when both are given. Titles are unique per vault:
        a taken title gets ``-2``, ``-3``, ... so ``[[title]]`` links resolve unambiguously.
        Notes tagged ``entity`` or ``self`` are never auto-linked (they are anchors).
        """
        prov = provenance if provenance is not None else Provenance(channel=channel)
        if prov.channel not in CHANNELS:
            raise ValueError(f"unknown memory channel {prov.channel!r}; expected one of {CHANNELS}")
        body, _ = add_related_links(text[: self.cfg.note_max_chars], links_to)
        clean_tags = _dedupe([t.strip() for t in tags])
        vault = self.vault_for(agent_id)
        with self.db.tx():
            note = Note(
                note_id=self.ids.new("note"),
                agent_id=agent_id,
                title=self._unique_title(agent_id, (title or "").strip() or self._derive_title(text)),
                body=body,
                created_tick=int(tick),
                importance=float(importance),
                tags=clean_tags,
                provenance=prov,
            )
            embedding = self._embed(note.title, note.body)
            related: list[Note] = []
            if auto_link and not (_AUTO_LINK_EXCLUDED & set(clean_tags)):
                related = self._auto_link_candidates(agent_id, embedding)
                note.body, _ = add_related_links(note.body, [r.title for r in related])
            note.links = parse_wikilinks(note.body)
            path = vault.write_note(note)
            try:
                self._index_note(note, embedding)
                for target in related:
                    self._add_backlink(target, note.title)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        return note

    def copy_note(
        self,
        note: Note,
        to_agent_id: str,
        tick: int,
        *,
        body: str | None = None,
        provenance: Provenance,
        importance: float | None = None,
        tags: Sequence[str] | None = None,
    ) -> Note:
        """Write a copy of ``note`` into ``to_agent_id``'s vault with the given provenance.

        Used by gossip (paraphrased ``body``, ``channel=gossip``, ``hop + 1``) and inheritance
        (``channel=inherited``). The provenance (channel, transfer_tick, path, hop, origin) is
        stored verbatim; the caller decides it. The source note is untouched.
        """
        return self.remember(
            to_agent_id,
            tick,
            note.body if body is None else body,
            title=note.title,
            tags=note.tags if tags is None else tags,
            importance=note.importance if importance is None else importance,
            provenance=provenance,
        )

    def ensure_entity(self, agent_id: str, title: str, tick: int, body: str) -> Note:
        """The agent's note titled ``title`` (case-insensitive), creating an ``entity`` stub if absent.

        Entity stubs are link anchors: never auto-linked, never returned by retrieval, but
        ``[[title]]`` links to them resolve and they are walked through.
        """
        existing = self._find_by_title(agent_id, title)
        if existing is not None:
            return existing
        return self.remember(
            agent_id, tick, body, title=" ".join(title.split()), tags=[ENTITY_TAG], auto_link=False
        )

    def plant_tracer(self, agent_id: str, tick: int, title: str, body: str, importance: float = 0.95) -> Note:
        """Seed a strategy note (DESIGN §11.5): channel ``tracer``, origin ``'tracer'``."""
        prov = Provenance(channel="tracer", origin_note_id=TRACER_ORIGIN)
        return self.remember(
            agent_id, tick, body, title=title, tags=list(TRACER_TAGS), importance=importance, provenance=prov
        )

    def revise_self(self, agent_id: str, tick: int, text: str) -> tuple[int, str]:
        """Replace the agent's self summary (capped at ``self_max_words``); returns (version, summary)."""
        summary = " ".join(text.split()[: self.cfg.self_max_words])
        with self.db.tx():
            row = self.db.fetchone(
                "SELECT MAX(version) AS v FROM self_versions WHERE agent_id=?", (agent_id,)
            )
            version = int(row["v"] or 0) + 1 if row is not None else 1
            self.vault_for(agent_id).write_self(summary, version, tick)
            self.db.execute(
                "INSERT INTO self_versions(agent_id, version, tick, summary) VALUES(?,?,?,?)",
                (agent_id, version, int(tick), summary),
            )
        return version, summary

    # --- self node --------------------------------------------------------------------
    def get_self(self, agent_id: str) -> str:
        row = self.db.fetchone(
            "SELECT summary FROM self_versions WHERE agent_id=? ORDER BY version DESC LIMIT 1", (agent_id,)
        )
        if row is not None:
            return str(row["summary"])
        return self.vault_for(agent_id).read_self()[0]

    def self_history(self, agent_id: str) -> list[dict[str, object]]:
        rows = self.db.fetchall(
            "SELECT version, tick, summary FROM self_versions WHERE agent_id=? ORDER BY version", (agent_id,)
        )
        return [
            {"version": int(r["version"]), "tick": int(r["tick"]), "summary": str(r["summary"])} for r in rows
        ]

    # --- reading ----------------------------------------------------------------------
    def get_note(self, note_id: str) -> Note | None:
        row = self.db.fetchone(f"SELECT {_NOTE_COLUMNS} FROM notes WHERE note_id=?", (note_id,))
        return None if row is None else self._load(row)

    def list_notes(self, agent_id: str, include_archived: bool = False) -> list[Note]:
        sql = f"SELECT {_NOTE_COLUMNS} FROM notes WHERE agent_id=?"
        if not include_archived:
            sql += " AND archived=0"
        rows = self.db.fetchall(sql + " ORDER BY created_tick, note_id", (agent_id,))
        return [self._load(r) for r in rows]

    def top_notes(self, agent_id: str, k: int, *, exclude_tags: tuple[str, ...] = ()) -> list[Note]:
        """The agent's ``k`` most important live notes (ties: newest first, then id), optionally skipping tagged stubs."""
        where = " AND ".join(["agent_id=?", "archived=0"] + ["tags NOT LIKE ?" for _ in exclude_tags])
        params: list[object] = [agent_id] + [f'%"{t}"%' for t in exclude_tags] + [int(k)]
        rows = self.db.fetchall(
            f"SELECT {_NOTE_COLUMNS} FROM notes WHERE {where} ORDER BY importance DESC, created_tick DESC, note_id LIMIT ?",
            tuple(params),
        )
        return [self._load(r) for r in rows]

    def retrieve(self, agent_id: str, query: str, tick: int, *, k: int | None = None) -> list[RetrievedNote]:
        """Rank the agent's live notes against ``query`` (DESIGN §10); top ``k`` as RetrievedNote."""
        return self.retrieve_with_stats(agent_id, query, tick, k=k)[0]

    def retrieve_with_stats(
        self, agent_id: str, query: str, tick: int, *, k: int | None = None
    ) -> tuple[list[RetrievedNote], dict[str, object]]:
        """:meth:`retrieve` plus per-call stats: ``hop_hist``, ``mean_age_ticks``, ``channel_mix``, ``entry_ids``.

        Entity stubs and zero-score candidates are never returned; ``hop_hist`` covers depths
        ``0..hops`` (all zero in ``vector`` mode beyond depth 0).
        """
        max_depth = max(0, self.cfg.hops) if self.cfg.mode == "graph" else 0
        stats: dict[str, object] = {
            "hop_hist": {h: 0 for h in range(max_depth + 1)},
            "mean_age_ticks": 0.0,
            "channel_mix": {},
            "entry_ids": [],
        }
        rows = self.db.fetchall(
            f"SELECT {_NOTE_COLUMNS}, embedding FROM notes WHERE agent_id=? AND archived=0 ORDER BY note_id",
            (agent_id,),
        )
        limit = int(k) if k is not None else self.cfg.max_retrieved
        if not rows or limit <= 0:
            return [], stats
        by_id: dict[str, Row] = {str(r["note_id"]): r for r in rows}

        qvec = self.embedder.embed(query)
        sims = {nid: cosine(qvec, unpack(r["embedding"])) for nid, r in by_id.items()}
        ranked = sorted(sims, key=lambda nid: (-sims[nid], nid))
        entry_ids = ranked[: max(0, self.cfg.entry_k)]
        entry = {nid: max(0.0, sims[nid]) for nid in entry_ids}
        stats["entry_ids"] = list(entry_ids)

        if self.cfg.mode == "graph":
            graph = NoteGraph(self._light_notes(agent_id, by_id), hub_tags=(ENTITY_TAG,))
            walked = graph.walk_with_hops(entry, self.cfg.hops, self.cfg.hop_decay)
        else:
            walked = {nid: (score, 0) for nid, score in entry.items()}

        final: dict[str, float] = {}
        for nid, (walk_score, _) in walked.items():
            r = by_id[nid]
            if walk_score <= 0.0 or ENTITY_TAG in _row_tags(r):
                continue
            final[nid] = (
                walk_score
                * (0.5 + float(r["importance"]))
                * self._recency_weight(tick, int(r["created_tick"]))
            )
        order = sorted(final, key=lambda nid: (-final[nid], nid))[:limit]

        out: list[RetrievedNote] = []
        hop_hist: dict[int, int] = stats["hop_hist"]  # type: ignore[assignment]
        channel_mix: dict[str, int] = {}
        ages: list[int] = []
        for nid in order:
            r = by_id[nid]
            note = self._load(r)
            out.append(
                RetrievedNote(
                    note_id=nid,
                    title=note.title,
                    body=note.body,
                    score=final[nid],
                    hop=int(r["hop"]),
                    source_agent_id=r["source_agent_id"],
                    created_tick=int(r["created_tick"]),
                )
            )
            depth = walked[nid][1]
            hop_hist[depth] = hop_hist.get(depth, 0) + 1
            channel = str(r["channel"])
            channel_mix[channel] = channel_mix.get(channel, 0) + 1
            ages.append(max(0, int(tick) - int(r["created_tick"])))
        stats["channel_mix"] = channel_mix
        stats["mean_age_ticks"] = (sum(ages) / len(ages)) if ages else 0.0
        return out, stats

    def similarity(self, text_a: str, text_b: str) -> float:
        """Embedding cosine between two texts (gossip drift metric)."""
        return cosine(self.embedder.embed(text_a), self.embedder.embed(text_b))

    # --- probes (DESIGN §10) ----------------------------------------------------------
    def plant_probe(self, agent_id: str, tick: int) -> tuple[str, str] | None:
        """Plant a probe two hops from the agent's most recent note; returns ``(probe_note_id, query)``.

        Anchor A (newest non-entity, non-probe note) <-> bridge B <-> probe P, linked both ways
        through the same backlink writer auto-linking uses (a nonsense probe cannot be relied on
        to auto-link by similarity). P shares no content words with A, so only a graph walk from
        A's title can recall it. ``None`` if the agent has no eligible note.
        """
        rows = self.db.fetchall(
            f"SELECT {_NOTE_COLUMNS} FROM notes WHERE agent_id=? AND archived=0 "
            "ORDER BY created_tick DESC, note_id DESC",
            (agent_id,),
        )
        anchor_row = next((r for r in rows if not (_PROBE_ANCHOR_EXCLUDED & _row_tags(r))), None)
        if anchor_row is None:
            return None
        anchor = self._load(anchor_row)
        with self.db.tx():
            bridge = self.remember(
                agent_id,
                tick,
                f"Bridge marker {tick}: a stepping stone.",
                title=f"Bridge {tick}",
                links_to=[anchor.title],
                tags=[PROBE_TAG],
                auto_link=False,
            )
            self._add_backlink(anchor, bridge.title)
            probe = self.remember(
                agent_id,
                tick,
                f"Probe marker {tick}: zxq vlt qmp.",
                title=f"Probe {tick}",
                links_to=[bridge.title],
                tags=[PROBE_TAG],
                auto_link=False,
            )
            self._add_backlink(bridge, probe.title)
        return probe.note_id, anchor.title

    def check_probe(
        self, agent_id: str, probe_note_id: str, query: str, tick: int, k: int | None = None
    ) -> bool:
        """Whether the planted probe is among ``retrieve(agent_id, query, tick, k=k)``."""
        return any(r.note_id == probe_note_id for r in self.retrieve(agent_id, query, tick, k=k))

    # --- tracer lineage (DESIGN §11.5) ------------------------------------------------
    def holders_of(self, origin_note_id: str, include_archived: bool = False) -> set[str]:
        """Agents holding the note ``origin_note_id`` itself or any copy descending from it."""
        sql = "SELECT DISTINCT agent_id FROM notes WHERE (origin_note_id=? OR note_id=?)"
        if not include_archived:
            sql += " AND archived=0"
        return {str(r["agent_id"]) for r in self.db.fetchall(sql, (origin_note_id, origin_note_id))}

    def descendants_of(self, origin_note_id: str) -> list[Note]:
        """The origin note and every copy descending from it (archived included), oldest first."""
        rows = self.db.fetchall(
            f"SELECT {_NOTE_COLUMNS} FROM notes WHERE origin_note_id=? OR note_id=? "
            "ORDER BY COALESCE(transfer_tick, created_tick), created_tick, note_id",
            (origin_note_id, origin_note_id),
        )
        return [self._load(r) for r in rows]

    def first_arrival(self, agent_id: str, origin_note_id: str) -> tuple[int, str, int] | None:
        """``(tick, channel, hop)`` of the earliest descendant of ``origin_note_id`` in the agent's vault."""
        row = self.db.fetchone(
            "SELECT COALESCE(transfer_tick, created_tick) AS arrived, channel, hop FROM notes "
            "WHERE agent_id=? AND (origin_note_id=? OR note_id=?) "
            "ORDER BY arrived, created_tick, note_id LIMIT 1",
            (agent_id, origin_note_id, origin_note_id),
        )
        if row is None:
            return None
        return int(row["arrived"]), str(row["channel"]), int(row["hop"])

    # --- metrics ----------------------------------------------------------------------
    def notes_by_channel(self, agent_ids: list[str]) -> dict[str, int]:
        """Live note counts per channel over ``agent_ids`` (every channel present, zero if none)."""
        counts = {c: 0 for c in CHANNELS}
        if not agent_ids:
            return counts
        marks = ",".join("?" * len(agent_ids))
        rows = self.db.fetchall(
            f"SELECT channel, COUNT(*) AS n FROM notes WHERE archived=0 AND agent_id IN ({marks}) GROUP BY channel",
            tuple(agent_ids),
        )
        for r in rows:
            counts[str(r["channel"])] = counts.get(str(r["channel"]), 0) + int(r["n"])
        return counts

    def link_density(self, agent_id: str) -> float:
        """Outgoing links per live note of the agent (0.0 without notes)."""
        n = self.db.fetchone("SELECT COUNT(*) AS n FROM notes WHERE agent_id=? AND archived=0", (agent_id,))
        total = int(n["n"]) if n is not None else 0
        if total == 0:
            return 0.0
        links = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM note_links WHERE from_note_id IN "
            "(SELECT note_id FROM notes WHERE agent_id=? AND archived=0)",
            (agent_id,),
        )
        return (int(links["n"]) if links is not None else 0) / total

    # --- lifecycle --------------------------------------------------------------------
    def archive_agent(self, agent_id: str, graveyard_root: Path) -> Path:
        """Mark every note archived and move the vault into the graveyard; returns its new dir."""
        vault = self.vault_for(agent_id)
        old_dir = vault.dir
        with self.db.tx():
            new_dir = vault.move_to(Path(graveyard_root))
            rows = self.db.fetchall("SELECT note_id, path FROM notes WHERE agent_id=?", (agent_id,))
            for r in rows:
                old_path = Path(str(r["path"]))
                try:
                    new_path = new_dir / old_path.relative_to(old_dir)
                except ValueError:
                    new_path = old_path  # not under the vault: leave the index pointing where it was
                self.db.execute(
                    "UPDATE notes SET archived=1, path=? WHERE note_id=?", (str(new_path), str(r["note_id"]))
                )
        return new_dir

    def reindex(self, agent_id: str, *, root: Path | None = None, archived: bool = False) -> int:
        """Rebuild the ``notes`` and ``note_links`` rows for ``agent_id`` from its vault files.

        ``root`` overrides the vault root (e.g. the graveyard); ``archived`` sets the flag on
        the rebuilt rows. Returns the number of notes indexed.
        """
        vault = Vault(root, agent_id) if root is not None else self.vault_for(agent_id)
        count = 0
        with self.db.tx():
            self.db.execute(
                "DELETE FROM note_links WHERE from_note_id IN (SELECT note_id FROM notes WHERE agent_id=?)",
                (agent_id,),
            )
            self.db.execute("DELETE FROM notes WHERE agent_id=?", (agent_id,))
            for note in vault.iter_notes():
                note.archived = archived
                self._index_note(note)
                count += 1
        return count

    # --- internals --------------------------------------------------------------------
    def _embed(self, title: str, body: str) -> list[float]:
        """Embedding over a note's content: title plus body without its ``## Related`` section."""
        return self.embedder.embed(f"{title} {content_body(body)}")

    def _index_note(self, note: Note, embedding: list[float] | None = None) -> None:
        """Insert the ``notes`` row and its ``note_links`` rows (caller holds the transaction)."""
        if not note.path:
            raise ValueError("note must be written to the vault before indexing")
        p = note.provenance
        if p.channel not in CHANNELS:
            raise ValueError(f"unknown memory channel {p.channel!r}; expected one of {CHANNELS}")
        vec = embedding if embedding is not None else self._embed(note.title, note.body)
        self.db.execute(
            "INSERT INTO notes(note_id, agent_id, path, title, created_tick, importance, tags, embedding, "
            "channel, source_agent_id, hop, origin_note_id, origin_generation, transfer_tick, path_agents, "
            "archived) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                note.note_id,
                note.agent_id,
                note.path,
                note.title,
                int(note.created_tick),
                float(note.importance),
                json.dumps(list(note.tags)),
                pack(vec),
                p.channel,
                p.source_agent_id,
                int(p.hop),
                p.origin_note_id,
                p.origin_generation,
                p.transfer_tick,
                json.dumps([str(a) for a in p.path_agents]),
                1 if note.archived else 0,
            ),
        )
        for title in note.links:
            self.db.execute(
                "INSERT OR IGNORE INTO note_links(from_note_id, to_title) VALUES(?,?)", (note.note_id, title)
            )

    def _auto_link_candidates(self, agent_id: str, embedding: list[float]) -> list[Note]:
        """Top ``auto_link_k`` live, non-anchor notes of the agent with cosine >= ``auto_link_min_sim``."""
        if self.cfg.auto_link_k <= 0:
            return []
        rows = self.db.fetchall(
            f"SELECT {_NOTE_COLUMNS}, embedding FROM notes WHERE agent_id=? AND archived=0 ORDER BY note_id",
            (agent_id,),
        )
        scored: list[tuple[float, str, Row]] = []
        for r in rows:
            if _AUTO_LINK_EXCLUDED & _row_tags(r):
                continue
            sim = cosine(embedding, unpack(r["embedding"]))
            if sim >= self.cfg.auto_link_min_sim:
                scored.append((-sim, str(r["note_id"]), r))
        scored.sort(key=lambda t: (t[0], t[1]))
        return [self._load(r) for _, _, r in scored[: self.cfg.auto_link_k]]

    def _add_backlink(self, target: Note, title: str) -> bool:
        """Add ``[[title]]`` under ``target``'s ``## Related`` section and index the link; False if present."""
        new_body, added = add_related_links(target.body, [title])
        if not added:
            return False
        target.body = new_body
        target.links = parse_wikilinks(new_body)
        self.vault_for(target.agent_id).write_note(target)
        self.db.execute(
            "INSERT OR IGNORE INTO note_links(from_note_id, to_title) VALUES(?,?)", (target.note_id, added[0])
        )
        return True

    def _unique_title(self, agent_id: str, title: str) -> str:
        rows = self.db.fetchall("SELECT title FROM notes WHERE agent_id=? AND archived=0", (agent_id,))
        taken = {normalise_title(str(r["title"])) for r in rows}
        if normalise_title(title) not in taken:
            return title
        n = 2
        while normalise_title(f"{title}-{n}") in taken:
            n += 1
        return f"{title}-{n}"

    def _find_by_title(self, agent_id: str, title: str) -> Note | None:
        key = normalise_title(title)
        rows = self.db.fetchall(
            f"SELECT {_NOTE_COLUMNS} FROM notes WHERE agent_id=? AND archived=0 ORDER BY created_tick, note_id",
            (agent_id,),
        )
        for r in rows:
            if normalise_title(str(r["title"])) == key:
                return self._load(r)
        return None

    def _load(self, row: Row) -> Note:
        """A full Note from an index row: the file supplies the content, the row the flags."""
        path = Path(str(row["path"]))
        note = parse_note(path.read_text(encoding="utf-8"), agent_id=str(row["agent_id"]), path=str(path))
        note.archived = bool(row["archived"])
        return note

    def _light_notes(self, agent_id: str, by_id: dict[str, Row]) -> list[Note]:
        """Body-less Notes carrying titles, tags and links from the index (no file reads)."""
        link_rows = self.db.fetchall(
            "SELECT from_note_id, to_title FROM note_links WHERE from_note_id IN "
            "(SELECT note_id FROM notes WHERE agent_id=? AND archived=0) ORDER BY rowid",
            (agent_id,),
        )
        links: dict[str, list[str]] = {nid: [] for nid in by_id}
        for lr in link_rows:
            links.setdefault(str(lr["from_note_id"]), []).append(str(lr["to_title"]))
        return [
            Note(
                note_id=nid,
                agent_id=agent_id,
                title=str(r["title"]),
                body="",
                created_tick=int(r["created_tick"]),
                importance=float(r["importance"]),
                tags=sorted(_row_tags(r)),
                links=links.get(nid, []),
            )
            for nid, r in by_id.items()
        ]

    def _recency_weight(self, tick: int, created_tick: int) -> float:
        half_life = max(1, int(self.cfg.recency_half_life_ticks))
        age = max(0, int(tick) - int(created_tick))
        return max(MIN_RECENCY_WEIGHT, 0.5 ** (age / half_life))

    @staticmethod
    def _derive_title(text: str) -> str:
        words = strip_wikilinks(content_body(text)).split()
        title = " ".join(words[:TITLE_WORDS])[:TITLE_MAX_CHARS].strip()
        return title or DEFAULT_TITLE


def _row_tags(row: Row) -> set[str]:
    try:
        tags = json.loads(row["tags"] or "[]")
    except (TypeError, ValueError):
        return set()
    return {str(t) for t in tags} if isinstance(tags, list) else set()


def _dedupe(items: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
