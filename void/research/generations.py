"""E3a, the discrete-generation protocol (docs/research/MEMORY_EVOLUTION.md §6.5, §6.13 row 3): ``void generations``.

The Vallinder-Hughes generational protocol with the memory policy as the heritable text. Twelve agents live in a
3-day evaluation world (``configs/exp_mem_e3a.yaml``); the top half by end balance survive; the dead are replaced by
children, each of which gets a survivor parent's policy; the next generation starts in a fresh world::

    void generations --config configs/exp_mem_e3a.yaml --generations 10 --seeds 1-10 --out runs
    void generations --config configs/exp_mem_e3a.yaml --arms oblique,vertical --generations 10 --seeds 1-10 --out runs

Layout. Generation ``g`` of seed ``n`` is an ordinary run directory at ``OUT/<experiment>/<arm>/seed_<n>/gen_<g>/``
(``exp_mem_e3a`` for the shipped config). Beside it, ``gen_<g>/generation.json`` holds what the generation was seeded
with and how it ranked, ``seed_<n>/generations.json`` the cohort-by-generation rows (:data:`ROW_FIELDS`) and
``<arm>/summary.json`` the slopes on generation across seeds (:func:`summarize`).

The protocol, per seed:

1. Generation 0 is the configured world: every roster agent is a founder with the configured seed policy (``--plant
   NAME=BULLET`` appends a bullet to a founder's, the way an experiment plants a practice).
2. Generation ``g`` runs as ``void run`` would (``Simulation.run``) for ``run.days`` days.
3. The roster agents are ranked by end balance (highest first; at equal balance the living before the dead, then
   roster order). The top ``N // 2`` survive.
4. Generation ``g + 1`` is the same world again (same config and seed; ``run.name`` ``<run.name>_g<g+1>``), and before
   its first tick every roster slot is seeded:

   * a survivor keeps its slot and gets back its own live notes and its current policy (transplant seeding, below);
   * each dead agent's slot holds a child. The children, in roster order of their slots, take the survivors as
     parents round-robin by rank (child ``k`` gets the survivor ranked ``k mod N//2``). A child gets no notes, only a
     policy, by the arm (``experiment.arm``, :data:`ARMS`):

     - ``oblique``: the parent's policy plus the survivors' most common bullet the parent's policy lacks
       (:func:`oblique_policy`), as a version with source ``adapted`` and ``source_agent_id`` the parent. When every
       survivor bullet is already in the parent's policy (or the bullet would take it past ``memory.policy.max_chars``)
       nothing is added and the copy is recorded as what it is, ``inherited``.
     - ``vertical``: a verbatim copy of the parent's policy, source ``inherited``, ``source_agent_id`` the parent.

Slots. The roster is the same in every generation: a slot's name, personality and spawn place come from the config
and the seed, so they belong to the world and not to the individual. What moves between generations is the text
(policy) and, for a survivor, its vault. Individuals are labelled ``<slot name>@g<generation born>`` in the manifests
(``Ada@g0`` is the founder in Ada's slot; ``Bao@g2`` a child born into Bao's slot in generation 2).

Transplant seeding (the helpers of :mod:`void.research.transplant`, with this protocol's mapping of who receives what).
A survivor's live notes (kernel anchors and probe markers excepted, :data:`~void.research.transplant.SKIPPED_TAGS`)
are written through :meth:`MemoryStore.remember` with their title, body (verbatim, not cut, not re-linked), importance
and tags plus ``transplanted``, on channel ``inherited``, with provenance ``origin_note_id``
``transplant:<previous run id>:<id>`` where ``id`` is the note's in-world origin (a gossiped note's points to the
speaker's note) or else its own id, and a note carried before keeps the run-qualified origin it already has
(:func:`carried_origin`); ``hop`` as it was (carrying does not hand a note on), ``origin_generation``
the note's, else the survivor's generation of birth, ``transfer_tick`` the seeding tick, ``source_agent_id`` NULL (the
survivor's previous life is in another run) and ``path_agents`` only ids of this run. Its policy comes back as a new
version with source ``inherited`` and no source agent. Every seeded version is announced with ``POLICY_CHANGED``.
The seeding manifest is stored in ``world_kv['generation']``.

Rows (``generations.json``, one per generation, :data:`ROW_FIELDS`): ``cohort_mean_balance`` is the mean end balance
in USD of the generation's roster agents, dead ones included at theirs; ``cohort_audit_accuracy`` is
:func:`~void.research.outcomes.lineage.cohort_exam_accuracy` and ``cohort_fitness`` (mean agent-days)
:func:`~void.research.outcomes.lineage.cohort_fitness`, both over every agent of the generation world (the cohort is
the generation's population, survivors and children alike); ``monoculture_index`` is
:func:`~void.research.outcomes.memory_structure.monoculture_index` (living agents' current policies);
``policy_similarity_to_gen0`` is the mean :func:`~void.memory.policy.similarity` between each agent's policy at the
end of the generation and each generation-0 agent's policy at the start of generation 0 (token Jaccard, the
placeholder measure until a real embedder exists). With ``--plant``, ``planted_share`` gives each planted bullet's
share of the generation's agents holding it at the start of the generation.

Summary (``summary.json``, per arm): :func:`void.research.stats.slope_by_seed` of every row metric on generation (a
separate intercept per seed, the within estimator, a cluster bootstrap by seed; H3, H9), and the per-generation
means across seeds. It covers the seeds of the invocation that wrote it.

Determinism and resuming. Every generation is a pure function of the config, the seed, the arm's rule and the
generation before it; at a given seed and generation the arms share ``run.name`` and so every random stream, and
generation 0 is the same world in both. No output file holds a time or an absolute path, so re-running gives equal
JSON. A generation directory that already holds a run is never overwritten: a finished run (``completed``, or
``extinct``) of the same config and the same seeding is skipped and read back, anything else is reported, ends that
seed's chain and makes the exit status 2.

Scripted only. A config that could reach a paid provider is refused: these runs are not counted against a live
budget. On scripted brains the policy does not steer behaviour: the text reaches a balance only through the price of
reading it (it is part of every observation, so a longer policy costs more per decision), and selection acts mostly
on what the slots are (personality, spawn place) and on the survivors' vaults. The numbers are a harness baseline.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from void.config import VoidConfig, load_config, micro_to_usd
from void.memory.notes import Provenance
from void.memory.policy import clean_policy_text, similarity
from void.research import stats
from void.research.outcomes.lineage import cohort_exam_accuracy, cohort_fitness
from void.research.outcomes.memory_structure import monoculture_index
from void.research.transplant import (
    CHANNEL,
    SKIPPED_TAGS,
    TRANSPLANT_TAG,
    _json_list,
    _note_file,
    _parse_seeds,
    _read_body,
    _resolve,
    origin_note_id,
)

__all__ = [
    "ARMS",
    "DEFAULT_CONFIG",
    "GENERATIONS_FILE",
    "MANIFEST_FILE",
    "ROW_FIELDS",
    "SUMMARY_FILE",
    "CarriedNote",
    "Member",
    "SlotPlan",
    "append_bullet",
    "assign_parents",
    "bullet_key",
    "bullet_share",
    "bullets",
    "carried_origin",
    "cli_main",
    "founders_plan",
    "generation_config",
    "load_notes",
    "most_common_absent_bullet",
    "next_plan",
    "oblique_policy",
    "rank_members",
    "read_members",
    "run_seed",
    "seed_generation",
    "summarize",
]

DEFAULT_CONFIG = "configs/exp_mem_e3a.yaml"
ARMS: tuple[str, ...] = ("oblique", "vertical")
GENERATIONS_FILE = "generations.json"
SUMMARY_FILE = "summary.json"
MANIFEST_FILE = "generation.json"
KV_KEY = "generation"
QUALIFIED_ORIGIN = origin_note_id("", "").rstrip(":") + ":"  # "transplant:", how a run-qualified origin starts
FINISHED: tuple[str, ...] = ("completed", "extinct")  # a generation world that ended either way has a ranking
ROW_FIELDS: tuple[str, ...] = ("generation", "cohort_mean_balance", "cohort_audit_accuracy", "cohort_fitness",
                               "monoculture_index", "policy_similarity_to_gen0")
METRICS: tuple[str, ...] = ROW_FIELDS[1:]


# --- bullets and the child's policy -------------------------------------------------------------------------------
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(\S.*?)\s*$")


def bullet_key(text: str) -> str:
    """The identity of a bullet: its text with whitespace collapsed, case-folded."""
    return " ".join(text.split()).casefold()


def bullets(text: str | None) -> list[str]:
    """The bullets of a policy in order (the list marker dropped, whitespace collapsed), each once by :func:`bullet_key`."""
    out: list[str] = []
    seen: set[str] = set()
    for line in (text or "").split("\n"):
        m = _BULLET_RE.match(line)
        if m is None:
            continue
        body = " ".join(m.group(1).split())
        key = bullet_key(body)
        if key not in seen:
            seen.add(key)
            out.append(body)
    return out


def append_bullet(text: str | None, bullet: str) -> str:
    """``text`` with ``- <bullet>`` as a new last line."""
    base = (text or "").rstrip("\n")
    return f"{base}\n- {bullet}" if base else f"- {bullet}"


def most_common_absent_bullet(parent: str | None, survivors: Sequence[str | None]) -> str | None:
    """The bullet held by the most survivor policies (``survivors`` in rank order) that ``parent`` does not hold.

    Ties go to the bullet of the best-ranked survivor holding it, then to the earlier line of that survivor's policy.
    Returns the bullet's text as that survivor wrote it, or None when every survivor bullet is already the parent's.
    """
    present = {bullet_key(b) for b in bullets(parent)}
    counts: dict[str, int] = {}
    first: dict[str, tuple[int, int, str]] = {}
    for rank, text in enumerate(survivors):
        for line, b in enumerate(bullets(text)):
            key = bullet_key(b)
            counts[key] = counts.get(key, 0) + 1
            first.setdefault(key, (rank, line, b))
    absent = [k for k in counts if k not in present]
    if not absent:
        return None
    best = min(absent, key=lambda k: (-counts[k], first[k][0], first[k][1]))
    return first[best][2]


def oblique_policy(parent: str | None, survivors: Sequence[str | None], *, max_chars: int | None = None) -> tuple[str, str | None]:
    """The oblique child's policy: ``(text, added bullet)``; the parent's text and None when nothing is added (no
    survivor bullet is absent from it, or adding one would take the cleaned text past ``max_chars``)."""
    bullet = most_common_absent_bullet(parent, survivors)
    if bullet is None:
        return parent or "", None
    text = append_bullet(parent, bullet)
    if max_chars is not None and len(clean_policy_text(text)) > max_chars:
        return parent or "", None
    return text, bullet


# --- ranking and the plan of the next generation ------------------------------------------------------------------
@dataclass(frozen=True)
class Member:
    """A roster agent of a finished generation world."""

    slot: int
    name: str
    agent_id: str
    status: str
    balance: int                 # micro-dollars at the end of the run
    policy_start: str | None     # the policy it started the generation with (after seeding)
    policy_end: str | None       # its current policy at the end


def rank_members(members: Iterable[Member]) -> list[Member]:
    """Highest end balance first; at equal balance the living before the dead, then roster order."""
    return sorted(members, key=lambda m: (-m.balance, m.status != "alive", m.slot))


def assign_parents(dead_slots: Iterable[int], survivors: Sequence[Member]) -> list[tuple[int, Member]]:
    """``(child slot, parent)`` pairs: the dead slots in roster order take ``survivors`` (in rank order) round-robin."""
    if not survivors:
        raise ValueError("no survivors to be parents")
    return [(slot, survivors[k % len(survivors)]) for k, slot in enumerate(sorted(dead_slots))]


@dataclass(frozen=True)
class CarriedNote:
    note_id: str
    title: str
    body: str
    importance: float
    tags: tuple[str, ...]
    hop: int
    origin_note_id: str | None
    origin_generation: int | None
    path_agents: tuple[str, ...]


@dataclass(frozen=True)
class SlotPlan:
    """What one roster slot holds when a generation starts."""

    slot: int
    name: str
    role: str                                 # founder | survivor | child
    individual: str                           # "<slot name>@g<generation born>"
    born: int
    parent: str | None = None                 # the parent's individual (a child, and a survivor that was one)
    parent_slot: int | None = None            # a child's parent's slot in this generation
    policy: str | None = None                 # the text seeded before the first tick; None keeps the config's seed
    policy_source: str | None = None          # inherited | adapted (survivors and children)
    added_bullet: str | None = None           # the oblique bullet a child got
    plant: str | None = None                  # a founder's planted bullet (generation 0)
    carried_from: tuple[str, str] | None = None  # a survivor's (run id, agent id) in the generation before
    notes: tuple[CarriedNote, ...] = ()


def founders_plan(roster: Sequence[str], plant: Mapping[str, str] | None = None) -> list[SlotPlan]:
    """Generation 0: every slot a founder with the configured seed policy, plus a planted bullet where ``plant`` names one."""
    plant = dict(plant or {})
    unknown = sorted(set(plant) - set(roster))
    if unknown:
        raise ValueError(f"--plant names {', '.join(unknown)}, not in the roster ({', '.join(roster)})")
    return [SlotPlan(slot=i, name=name, role="founder", individual=f"{name}@g0", born=0, plant=plant.get(name))
            for i, name in enumerate(roster)]


def next_plan(arm: str, generation: int, plan: Sequence[SlotPlan], members: Sequence[Member], *, run_id: str,
              notes_of: Callable[[str], tuple[CarriedNote, ...]], max_chars: int | None = None) -> list[SlotPlan]:
    """The plan of generation ``generation + 1`` from generation ``generation``'s plan and its finished roster.

    ``notes_of(agent_id)`` returns a survivor's live notes in the finished run (:func:`load_notes`).
    """
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {', '.join(ARMS)}")
    ranked = rank_members(members)
    keep = len(ranked) // 2
    if keep < 1:
        raise ValueError("a generation needs at least two agents for half of them to survive")
    survivors = ranked[:keep]
    texts = [m.policy_end for m in survivors]
    by_slot = {p.slot: p for p in plan}
    out: dict[int, SlotPlan] = {}
    for m in survivors:
        prev = by_slot[m.slot]
        out[m.slot] = SlotPlan(slot=m.slot, name=prev.name, role="survivor", individual=prev.individual, born=prev.born,
                               parent=prev.parent, policy=m.policy_end or "", policy_source="inherited",
                               carried_from=(run_id, m.agent_id), notes=notes_of(m.agent_id))
    born = generation + 1
    for slot, parent in assign_parents((m.slot for m in ranked[keep:]), survivors):
        if arm == "oblique":
            text, added = oblique_policy(parent.policy_end, texts, max_chars=max_chars)
        else:
            text, added = parent.policy_end or "", None
        name = by_slot[slot].name
        out[slot] = SlotPlan(slot=slot, name=name, role="child", individual=f"{name}@g{born}", born=born,
                             parent=out[parent.slot].individual, parent_slot=parent.slot, policy=text,
                             policy_source="adapted" if added is not None else "inherited", added_bullet=added)
    return [out[i] for i in sorted(out)]


def plan_signature(plan: Sequence[SlotPlan]) -> str:
    """A hash of everything a plan seeds (a finished generation is reused only under the same signature)."""
    blob = json.dumps([asdict(p) for p in plan], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --- seeding a generation world ------------------------------------------------------------------------------------
def carried_origin(note: CarriedNote, run_id: str) -> str:
    """The origin a carried note is recorded with: an origin already qualified by a run (``transplant:...``, a note
    carried before) is kept; any other is a note id of ``run_id``, the run the note is carried out of (the note's own
    id, or the in-world origin a gossiped or inherited note points to) and is qualified with it."""
    origin = note.origin_note_id
    if origin is not None and origin.startswith(QUALIFIED_ORIGIN):
        return origin
    return origin_note_id(run_id, origin or note.note_id)


def seed_generation(sim: Any, plan: Sequence[SlotPlan], *, generation: int, arm: str,
                    previous_run_id: str | None) -> dict[str, Any]:
    """Seed the roster of ``sim`` (built, not yet ticked) as ``plan`` says; returns the seeding manifest, which is
    also stored in ``world_kv['generation']``."""
    from void.events import Event, Kind

    if not sim.cfg.memory.policy.enabled:
        raise ValueError("the generational protocol inherits memory policies, but memory.policy is off in this config")
    roster = [spec.name for spec in sim.cfg.agents]
    if [p.name for p in plan] != roster or [p.slot for p in plan] != list(range(len(roster))):
        raise ValueError("the generation plan does not match the config's roster")
    founders = {a.name: a for a in sim.registry.all() if a.born_tick == 0 and a.parent_id is None}
    missing = [name for name in roster if name not in founders]
    if missing:
        raise ValueError(f"roster agent(s) {', '.join(missing)} were not founded in the generation world")
    agent_of = {p.slot: founders[p.name].agent_id for p in plan}
    tick = int(sim.clock.tick)
    day = max(1, int(sim.clock.day))
    rows: dict[int, dict[str, Any]] = {}
    # survivors (and planted founders) first, so a child's parent already holds the policy the child copies
    for p in sorted(plan, key=lambda p: (p.role == "child", p.slot)):
        aid = agent_of[p.slot]
        for note in p.notes:
            assert p.carried_from is not None
            prov = Provenance(
                source_agent_id=None, hop=note.hop, origin_note_id=carried_origin(note, p.carried_from[0]),
                origin_generation=note.origin_generation if note.origin_generation is not None else p.born,
                channel=CHANNEL, transfer_tick=tick,
                path_agents=[a for a in note.path_agents if sim.registry.get(a) is not None],
            )
            sim.memory.remember(aid, tick, note.body, title=note.title, tags=[*note.tags, TRANSPLANT_TAG],
                                importance=note.importance, provenance=prov, auto_link=False, max_chars=len(note.body))
        text: str | None = None
        source: str | None = None
        source_agent: str | None = None
        if p.plant is not None:
            text, source = append_bullet(sim.policy.text(aid), p.plant), "seed"
        elif p.policy is not None:
            text, source = p.policy, p.policy_source or "inherited"
            source_agent = agent_of[p.parent_slot] if p.role == "child" and p.parent_slot is not None else None
        version = None
        if text is not None:
            pv = sim.policy.set(aid, text, tick, source=source, source_agent_id=source_agent)
            sim.bus.emit(Event(tick, day, Kind.POLICY_CHANGED, sim.policy.event_payload(pv), aid))
            version = pv.version
        rows[p.slot] = {
            "slot": p.slot, "name": p.name, "agent_id": aid, "role": p.role, "individual": p.individual, "born": p.born,
            "parent": p.parent, "parent_agent_id": source_agent, "policy_source": source, "policy_version": version,
            "added_bullet": p.added_bullet, "planted_bullet": p.plant, "notes_carried": len(p.notes),
            "carried_from": None if p.carried_from is None else {"run_id": p.carried_from[0], "agent_id": p.carried_from[1]},
        }
    manifest = {
        "generation": int(generation), "arm": arm, "seed": int(sim.cfg.run.seed), "run_id": sim.run_id, "tick": tick,
        "previous_run_id": previous_run_id, "signature": plan_signature(plan), "channel": CHANNEL, "tag": TRANSPLANT_TAG,
        "slots": [rows[i] for i in sorted(rows)],
    }
    sim.db.kv_set(KV_KEY, manifest)
    return manifest


# --- reading a finished generation ---------------------------------------------------------------------------------
def _connect(run_dir: Path) -> sqlite3.Connection:
    db_path = run_dir / "world.db"
    if not db_path.is_file():
        raise FileNotFoundError(f"no world.db in {run_dir}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def read_members(run_dir: str | Path, roster: Sequence[str], seed_tick: int = 0) -> list[Member]:
    """The roster founders of a finished generation world in roster order, with their end balance and the policy they
    started with (their latest version at ``seed_tick``) and ended with."""
    conn = _connect(Path(run_dir))
    try:
        rows = {str(r["name"]): r for r in conn.execute(
            "SELECT agent_id, name, status, balance FROM agents WHERE born_tick=0 AND parent_id IS NULL ORDER BY agent_id")}
        out: list[Member] = []
        for slot, name in enumerate(roster):
            r = rows.get(name)
            if r is None:
                raise ValueError(f"roster agent {name!r} is not in {run_dir}")
            aid = str(r["agent_id"])
            start = conn.execute("SELECT text FROM policy_versions WHERE agent_id=? AND tick<=? ORDER BY version DESC LIMIT 1",
                                 (aid, int(seed_tick))).fetchone()
            end = conn.execute("SELECT text FROM policy_versions WHERE agent_id=? ORDER BY version DESC LIMIT 1", (aid,)).fetchone()
            out.append(Member(slot=slot, name=name, agent_id=aid, status=str(r["status"]), balance=int(r["balance"]),
                              policy_start=None if start is None else str(start["text"]),
                              policy_end=None if end is None else str(end["text"])))
        return out
    finally:
        conn.close()


def load_notes(run_dir: str | Path, agent_id: str) -> tuple[CarriedNote, ...]:
    """An agent's live notes in a finished run, in creation order; kernel anchors and probe markers are left out."""
    run_dir = Path(run_dir)
    conn = _connect(run_dir)
    try:
        out: list[CarriedNote] = []
        for n in conn.execute(
            "SELECT note_id, path, title, importance, tags, hop, origin_note_id, origin_generation, path_agents FROM notes "
            "WHERE agent_id=? AND archived=0 ORDER BY created_tick, note_id", (agent_id,)
        ):
            tags = tuple(_json_list(n["tags"]))
            if set(tags) & set(SKIPPED_TAGS):
                continue
            out.append(CarriedNote(
                note_id=str(n["note_id"]), title=str(n["title"]), body=_read_body(_note_file(run_dir, agent_id, str(n["path"]))),
                importance=float(n["importance"]), tags=tags, hop=int(n["hop"]),
                origin_note_id=None if n["origin_note_id"] is None else str(n["origin_note_id"]),
                origin_generation=None if n["origin_generation"] is None else int(n["origin_generation"]),
                path_agents=tuple(_json_list(n["path_agents"])),
            ))
        return tuple(out)
    finally:
        conn.close()


def _run_row(run_dir: Path) -> tuple[str, str, dict[str, Any] | None]:
    """``(run id, status, seeding manifest)`` of a generation run."""
    conn = _connect(run_dir)
    try:
        row = conn.execute("SELECT run_id, status FROM run LIMIT 1").fetchone()
        kv = conn.execute("SELECT value FROM world_kv WHERE key=?", (KV_KEY,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"{run_dir} records no run")
    return str(row["run_id"]), str(row["status"]), None if kv is None else json.loads(kv["value"])


def _mean(values: Iterable[float]) -> float | None:
    vals = list(values)
    return sum(vals) / len(vals) if vals else None


def bullet_share(policies: Sequence[str | None], bullet: str) -> float | None:
    """The share of ``policies`` holding ``bullet`` (by :func:`bullet_key`); None for no policies."""
    if not policies:
        return None
    key = bullet_key(bullet)
    return sum(1 for p in policies if key in {bullet_key(b) for b in bullets(p)}) / len(policies)


def generation_row(generation: int, run_dir: Path, members: Sequence[Member], gen0_start: Sequence[str | None],
                   plants: Sequence[str] = ()) -> dict[str, Any]:
    """One ``generations.json`` row (see the module docstring)."""
    row: dict[str, Any] = {
        "generation": int(generation),
        "cohort_mean_balance": _mean(micro_to_usd(m.balance) for m in members),
        "cohort_audit_accuracy": cohort_exam_accuracy(run_dir),
        "cohort_fitness": cohort_fitness(run_dir),
        "monoculture_index": monoculture_index(run_dir),
        "policy_similarity_to_gen0": _mean(similarity(m.policy_end or "", s or "") for m in members for s in gen0_start),
    }
    if plants:
        row["planted_share"] = {b: bullet_share([m.policy_start for m in members], b) for b in plants}
    return row


# --- running ---------------------------------------------------------------------------------------------------
def generation_config(config: str | Path, arm: str, seed: int, generation: int, *, days: int | None = None,
                      overrides: Mapping[str, Any] | None = None) -> VoidConfig:
    """The config of one generation world: ``experiment.arm`` = arm, ``run.seed`` = seed, ``run.name`` =
    ``<run.name>_g<generation>`` (``run.days`` when given; ``overrides`` first, these on top)."""
    path = _resolve(config)
    merged: dict[str, Any] = json.loads(json.dumps(dict(overrides or {})))
    base = load_config(path, merged)
    run = dict(merged.get("run") or {})
    run.update({"seed": int(seed), "name": f"{base.run.name}_g{int(generation)}"})
    if days is not None:
        run["days"] = int(days)
    merged["run"] = run
    merged["experiment"] = {**dict(merged.get("experiment") or {}), "arm": arm}
    return load_config(path, merged)


def seed_dir_for(out: Path, cfg: VoidConfig) -> Path:
    return out / str(cfg.experiment.name or cfg.run.name) / str(cfg.experiment.arm) / f"seed_{cfg.run.seed}"


def _existing(run_dir: Path, cfg: VoidConfig, signature: str) -> str | None:
    """None when ``run_dir`` holds no run; ``same`` for a finished run of this config and seeding; else why not."""
    db_path = run_dir / "world.db"
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return "an unreadable world.db"
    try:
        row = conn.execute("SELECT status, config_hash FROM run LIMIT 1").fetchone()
        if row is None:
            return None
        kv = conn.execute("SELECT value FROM world_kv WHERE key=?", (KV_KEY,)).fetchone()
    except sqlite3.Error:
        return "an unreadable world.db"
    finally:
        conn.close()
    if row["config_hash"] != cfg.hash():
        return "a run of another config"
    manifest = json.loads(kv["value"]) if kv is not None else {}
    if manifest.get("signature") != signature:
        return "a run with another seeding"
    if row["status"] not in FINISHED:
        return f"an unfinished run (status {row['status']})"
    return "same"


async def run_generation(cfg: VoidConfig, run_dir: Path, plan: Sequence[SlotPlan], *, generation: int, arm: str,
                         previous_run_id: str | None) -> str:
    """Build the generation world, seed it before the first tick, run it to the end; returns the run status."""
    from void.sim.loop import Simulation

    sim = Simulation(cfg, run_dir)
    try:
        seed_generation(sim, plan, generation=generation, arm=arm, previous_run_id=previous_run_id)
        status = await sim.run()
    finally:
        sim.db.close()
        gc.collect()  # release the sandbox's subprocess transports while the loop is still open
    return status


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fmt(v: float | None, spec: str) -> str:
    return "n/a" if v is None else format(v, spec)


def run_seed(config: str | Path, arm: str, seed: int, generations: int, out: str | Path, *, days: int | None = None,
             plant: Mapping[str, str] | None = None, overrides: Mapping[str, Any] | None = None,
             log: Callable[[str], None] | None = print) -> tuple[bool, list[dict[str, Any]]]:
    """Run ``generations`` generations of one seed and arm; writes every ``gen_<g>/generation.json`` and
    ``generations.json``. Returns ``(every generation finished, rows)``; a generation that cannot run ends the chain."""
    if generations < 1:
        raise ValueError("--generations must be at least 1")
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {', '.join(ARMS)}")
    say = log or (lambda _line: None)
    cfg0 = generation_config(config, arm, seed, 0, days=days, overrides=overrides)
    roster = [spec.name for spec in cfg0.agents]
    plants = list(dict(plant or {}).values())
    seed_dir = seed_dir_for(Path(out), cfg0)
    plan = founders_plan(roster, plant)
    rows: list[dict[str, Any]] = []
    gen0_start: list[str | None] = []
    previous_run_id: str | None = None
    ok = True
    for g in range(generations):
        cfg = cfg0 if g == 0 else generation_config(config, arm, seed, g, days=days, overrides=overrides)
        run_dir = seed_dir / f"gen_{g}"
        signature = plan_signature(plan)
        state = _existing(run_dir, cfg, signature)
        t0 = time.perf_counter()
        if state is None:
            try:
                status = asyncio.run(run_generation(cfg, run_dir, plan, generation=g, arm=arm, previous_run_id=previous_run_id))
            except (RuntimeError, ValueError, OSError) as e:
                say(f"error: {arm} seed {seed} generation {g}: {e}")
                ok = False
                break
            if status not in FINISHED:
                say(f"error: {arm} seed {seed} generation {g} ended {status} ({run_dir})")
                ok = False
                break
        elif state == "same":
            say(f"{run_dir} already holds this finished generation: read back")
        else:
            say(f"error: {run_dir} already holds {state}: remove it or pass another --out")
            ok = False
            break
        run_id, status, seeding = _run_row(run_dir)
        seed_tick = int((seeding or {}).get("tick", 0))
        members = read_members(run_dir, roster, seed_tick)
        if g == 0:
            gen0_start = [m.policy_start for m in members]
        row = generation_row(g, run_dir, members, gen0_start, plants)
        rows.append(row)
        ranked = rank_members(members)
        keep = len(ranked) // 2
        rank_of = {m.slot: i + 1 for i, m in enumerate(ranked)}
        by_slot = {p.slot: p for p in plan}
        slots = {s["slot"]: s for s in (seeding or {}).get("slots", [])}
        _write_json(run_dir / MANIFEST_FILE, {
            **(seeding or {}), "status": status,
            "slots": [{**slots.get(m.slot, {}), "individual": by_slot[m.slot].individual, "status": m.status,
                       "balance_usd": micro_to_usd(m.balance), "rank": rank_of[m.slot], "survives": rank_of[m.slot] <= keep,
                       "policy_start": m.policy_start, "policy_end": m.policy_end} for m in members],
            "survivors": [by_slot[m.slot].individual for m in ranked[:keep]],
            "row": row,
        })
        say(f"{cfg.experiment.name}/{arm} seed {seed} gen {g}: {status} | mean ${_fmt(row['cohort_mean_balance'], '.4f')} | "
            f"audit {_fmt(row['cohort_audit_accuracy'], '.3f')} | M {_fmt(row['monoculture_index'], '.3f')} | "
            f"sim0 {_fmt(row['policy_similarity_to_gen0'], '.3f')} | survivors "
            f"{', '.join(m.name for m in ranked[:keep])} | {time.perf_counter() - t0:.1f}s")
        if g + 1 < generations:
            plan = next_plan(arm, g, plan, members, run_id=run_id, notes_of=lambda aid, d=run_dir: load_notes(d, aid),
                             max_chars=int(cfg.memory.policy.max_chars))
            previous_run_id = run_id
    _write_json(seed_dir / GENERATIONS_FILE, rows)
    return ok and len(rows) == generations, rows


def summarize(arm_dir: str | Path, seeds: Sequence[int], generations: int, *, n_boot: int = stats.N_BOOT) -> dict[str, Any]:
    """The arm's summary over ``seeds``: slopes on generation of every row metric (:func:`stats.slope_by_seed`) and the
    per-generation means across seeds. Seeds without ``generations.json`` are listed as missing."""
    arm_dir = Path(arm_dir)
    per_seed: dict[int, list[dict[str, Any]]] = {}
    for s in seeds:
        path = arm_dir / f"seed_{s}" / GENERATIONS_FILE
        if path.is_file():
            per_seed[int(s)] = [r for r in json.loads(path.read_text(encoding="utf-8")) if int(r["generation"]) < generations]
    slopes = {metric: stats.slope_by_seed(((s, r["generation"], r.get(metric)) for s, rows in per_seed.items() for r in rows),
                                          n_boot=n_boot)
              for metric in METRICS}
    by_generation: list[dict[str, Any]] = []
    for g in range(generations):
        entry: dict[str, Any] = {"generation": g}
        for metric in METRICS:
            vals = [r[metric] for rows in per_seed.values() for r in rows if r["generation"] == g and r.get(metric) is not None]
            entry[metric] = _mean(vals)
            entry[f"{metric}_n"] = len(vals)
        by_generation.append(entry)
    return {
        "experiment": arm_dir.parent.name, "arm": arm_dir.name, "generations": int(generations),
        "seeds": [int(s) for s in seeds],
        "complete_seeds": [s for s, rows in per_seed.items() if len(rows) == generations],
        "missing_seeds": [int(s) for s in seeds if int(s) not in per_seed],
        "slopes": slopes, "by_generation": by_generation,
    }


# --- the command line --------------------------------------------------------------------------------------------
def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected at least 1, got {value}")
    return value


def _parse_arms(spec: str) -> list[str]:
    out = [a.strip() for a in spec.split(",") if a.strip()]
    bad = [a for a in out if a not in ARMS]
    if bad or not out:
        raise argparse.ArgumentTypeError(f"arms must be among {', '.join(ARMS)} (got {spec!r})")
    return list(dict.fromkeys(out))


def _parse_plants(items: Sequence[str]) -> dict[str, str]:
    plants: dict[str, str] = {}
    for item in items:
        name, sep, bullet = item.partition("=")
        name, bullet = name.strip(), " ".join(bullet.split())
        if not sep or not name or not bullet:
            raise ValueError(f"--plant takes NAME=BULLET (got {item!r})")
        if name in plants:
            raise ValueError(f"--plant names {name} twice")
        plants[name] = bullet
    return plants


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="void generations",
        description="E3a: the discrete-generation protocol (MEMORY_EVOLUTION 6.5). Each generation is a world of the "
                    "config's roster; the top half by end balance survive with their vaults and policies, and each "
                    "dead slot holds a child with a survivor's policy (oblique: plus the survivors' most common "
                    "bullet it lacks; vertical: a verbatim copy). Runs land in OUT/<experiment>/<arm>/seed_<n>/gen_<g>/.")
    p.add_argument("--config", default=DEFAULT_CONFIG, help=f"the generation world (default {DEFAULT_CONFIG})")
    p.add_argument("--generations", type=_positive_int, default=10, help="generations per seed (default 10)")
    p.add_argument("--seeds", type=_parse_seeds, required=True, help="A-B (inclusive), A,B,C or one seed")
    p.add_argument("--arms", type=_parse_arms, default=None,
                   help=f"comma-separated among {', '.join(ARMS)} (default the config's experiment.arm)")
    p.add_argument("--out", default=None, help="output root (default the config's run.data_dir)")
    p.add_argument("--days", type=_positive_int, default=None, help="override run.days (smoke runs; default the config's)")
    p.add_argument("--plant", action="append", default=[], metavar="NAME=BULLET",
                   help="append BULLET to founder NAME's policy in generation 0 (repeatable)")
    p.add_argument("--quiet", action="store_true", help="one line per seed instead of one per generation")
    return p


def cli_main(argv: list[str]) -> int:
    """``void generations ...``: 0 when every seed finished every generation (or already had), 2 otherwise."""
    from void.economy.budget import reachable_providers

    args = build_parser().parse_args(argv)
    try:
        base = load_config(_resolve(args.config))
        arms = args.arms or [str(base.experiment.arm)]
        if arms[0] not in ARMS:
            raise ValueError(f"experiment.arm is {base.experiment.arm!r}; the generational arms are {', '.join(ARMS)} "
                             f"(set it in the config or pass --arms)")
        cfg0 = generation_config(args.config, arms[0], args.seeds[0], 0, days=args.days)
    except (OSError, ValueError) as e:
        print(f"error: config {args.config}: {e}", file=sys.stderr)
        return 2
    paid = reachable_providers(cfg0)
    if paid:
        print(f"refused: the generation world reaches a paid provider ({', '.join(sorted(paid))}); `void generations` "
              f"runs scripted tiers only and is not counted against a live budget", file=sys.stderr)
        return 2
    try:
        if not cfg0.memory.policy.enabled:
            raise ValueError(f"memory.policy is off in {args.config}; the generational protocol inherits policies")
        if len(cfg0.agents) < 2:
            raise ValueError(f"the roster of {args.config} has {len(cfg0.agents)} agent(s); half must survive, so it needs two")
        plants = _parse_plants(args.plant)
        founders_plan([spec.name for spec in cfg0.agents], plants)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for w in cfg0.warnings():
        print(f"warning: {w}", file=sys.stderr)
    out = Path(args.out) if args.out else Path(cfg0.run.data_dir)
    exp_dir = out / str(cfg0.experiment.name or cfg0.run.name)
    done = total = 0

    def log(line: str) -> None:
        if line.startswith("error:"):
            print(line, file=sys.stderr, flush=True)
        elif not args.quiet:
            print(line, flush=True)

    for arm in arms:
        for seed in args.seeds:
            total += 1
            finished, rows = run_seed(args.config, arm, seed, args.generations, out, days=args.days, plant=plants, log=log)
            done += int(finished)
            if args.quiet:
                last = rows[-1] if rows else {}
                print(f"{arm} seed {seed}: {len(rows)}/{args.generations} generations"
                      f"{'' if finished else ' (incomplete)'}; last mean ${_fmt(last.get('cohort_mean_balance'), '.4f')}",
                      flush=True)
        summary = summarize(exp_dir / arm, args.seeds, args.generations)
        _write_json(exp_dir / arm / SUMMARY_FILE, summary)
        for metric in ("cohort_mean_balance", "cohort_audit_accuracy"):
            s = summary["slopes"][metric]
            lo, hi = s["ci"]
            print(f"{arm}: slope of {metric} on generation {_fmt(s['slope'], '+.5f')} "
                  f"[{_fmt(lo, '+.5f')}, {_fmt(hi, '+.5f')}] over {s['n_seeds']} seed(s)", flush=True)
    print(f"generations: {done}/{total} seed chains finished under {exp_dir}", flush=True)
    return 0 if total and done == total else 2
