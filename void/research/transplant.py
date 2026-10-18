"""The practice transplant harness, E5 (docs/research/MEMORY_EVOLUTION.md §6.7, WP9): ``void transplant``.

A donor's final memory (its live notes and its current memory policy) is given to fresh recipients in a standard
evaluation world (``configs/exp_mem_e5_eval.yaml``), once per condition and seed, and the runs land where
``compare`` reads them::

    void transplant --donor-run runs/exp_mem_e0/index_builder/seed_1 --seeds 1-20 --out runs
    python scripts/compare.py runs/exp_mem_e5

Conditions (each one an arm; :data:`CONDITIONS`):

* ``none``      nothing is transplanted (the recipients start exactly as founders of the evaluation world do);
* ``policy``    the donor's current policy only;
* ``vault``     the donor's live notes only;
* ``both``      notes and policy: the full transplant;
* ``shuffled``  the full transplant with the policy's lines shuffled (:func:`shuffle_policy`);
* ``inverted``  the full transplant with each imperative bullet of the policy negated (:func:`invert_policy`).

The two faithfulness controls (§6.7) keep the vault, so each differs from ``both`` in the policy's *content* alone: the
shuffled policy has the same lines, length and structure in another order, the inverted one the same practices
turned into their opposites.

Layout and fairness. Runs are ordinary run directories at ``OUT/<experiment.name>/<condition>/seed_<n>/``
(``exp_mem_e5`` for the shipped config). The condition is written into ``experiment.arm`` and into ``run.name``
(``<run.name>_<condition>``), the two paths compare always allows, so the arms' resolved configs differ only there.
The transplant itself is recorded in the run: the notes and policy rows carry it, ``world_kv['transplant']`` holds
the manifest (donor run, donors, recipients, what each received) and ``transplant.json`` mirrors it beside
``world.db``. A directory that already holds a run is never overwritten: a completed run with the same config and
the same transplant is skipped and counts as done, anything else is reported and makes the exit status 2.

Seeding. :class:`void.sim.loop.Simulation` is built for the evaluation config; before the first tick, recipient ``i``
of the first ``--recipients`` roster agents receives from donor ``i mod len(donors)``:

* each live note of the donor, written through :meth:`MemoryStore.remember` with the donor's title, body (verbatim,
  never cut), importance and tags plus the tag ``transplanted``, on channel ``inherited``, with provenance
  ``origin_note_id = 'transplant:<donor_run_id>:<donor_note_id>'``, ``transfer_tick`` the seeding tick, the donor
  note's ``hop`` (as inheritance keeps it), ``origin_generation`` the donor note's (else the donor's generation) and
  ``source_agent_id`` the donor's id when that agent exists in this run, else NULL (the usual case: agent ids are
  scoped to their run). ``path_agents`` keeps only ids of this run. The kernel's own anchors and instruments are not
  vault content and are not copied (:data:`SKIPPED_TAGS`): entity stubs (the recipient makes its own on first sight,
  with its own world's coordinates) and probe markers;
* the donor's current policy (or its shuffled or inverted form) as a new ``policy_versions`` row with source
  ``inherited`` through :meth:`PolicyStore.set`, announced with ``POLICY_CHANGED`` like any other version.

A note keeps its text, so what it says is about the donor's world: the evaluation world places its nodes by its own
seed (on the same ring, so a donor's positions are close but exact only at the donor's seed), and a day a note
names ("Last seen rich on day 6") is a day of the donor's clock, which a recipient's scripted brain reads as newer
than anything it sees in a shorter evaluation world.

The run then goes through the same loop as ``void run`` (``Simulation.run``). Only scripted configs are accepted:
a config that could reach a paid provider is refused, because these runs are not counted against a live budget.

Donors (``--donors``): ``richest`` is the living donor with the highest final balance (ties: agent id); ``all`` is
every living donor in roster order (birth tick, then id); ``names:A,B`` names them in that order (a living agent of
that name first, else the latest-born one). A donor with no policy cannot serve a condition that transplants one.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from void.config import VoidConfig, load_config, micro_to_usd
from void.memory.notes import Provenance, body_of
from void.rng import RNG

__all__ = [
    "CONDITIONS",
    "DEFAULT_CONDITIONS",
    "DEFAULT_CONFIG",
    "MANIFEST_FILE",
    "SKIPPED_TAGS",
    "TRANSPLANT_TAG",
    "Donor",
    "DonorNote",
    "cli_main",
    "condition_config",
    "invert_line",
    "invert_policy",
    "load_donors",
    "origin_note_id",
    "policy_variant",
    "run_condition",
    "seed_transplant",
    "shuffle_policy",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = "configs/exp_mem_e5_eval.yaml"
CONDITIONS: tuple[str, ...] = ("none", "policy", "vault", "both", "shuffled", "inverted")
DEFAULT_CONDITIONS = ",".join(CONDITIONS)
TRANSPLANT_TAG = "transplanted"
CHANNEL = "inherited"
MANIFEST_FILE = "transplant.json"
KV_KEY = "transplant"
SKIPPED_TAGS: tuple[str, ...] = ("entity", "probe")
# condition -> (copy the vault, policy variant or None)
_PLAN: dict[str, tuple[bool, str | None]] = {
    "none": (False, None),
    "policy": (False, "faithful"),
    "vault": (True, None),
    "both": (True, "faithful"),
    "shuffled": (True, "shuffled"),
    "inverted": (True, "inverted"),
}


# --- the policy controls ------------------------------------------------------------------------------------------
def shuffle_policy(text: str, seed: int) -> str:
    """The policy with its content lines permuted by ``rng.stream('transplant', seed)`` (deterministic per seed).

    Headings and blank lines keep their places and every other line moves among the remaining places, so the result
    has the same lines, the same length and the same structure in another order. When at least two of the lines
    differ the permutation is redrawn from the same stream until the text changes (a shuffle that leaves the policy
    as it was would not be a control).
    """
    lines = text.split("\n")
    movable = [i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")]
    content = [lines[i] for i in movable]
    rng = RNG(seed).stream("transplant", seed)
    order = list(content)
    rng.shuffle(order)
    if len(set(content)) > 1:
        tries = 0
        while order == content and tries < 64:
            rng.shuffle(order)
            tries += 1
        if order == content:  # pragma: no cover - 64 identity draws in a row
            order = order[1:] + order[:1]
    out = list(lines)
    for i, line in zip(movable, order, strict=True):
        out[i] = line
    return "\n".join(out)


_BULLET_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)(.*)$")
_CLAUSE_RE = re.compile(r"(?<=[.!?;])(\s+)")
_WORD_RE = re.compile(r"[A-Za-z'’]+")
_DO_NOT_RE = re.compile(r"(?:do\s+not|don['’]t)\s+", re.IGNORECASE)
_LATER_MARKER_RE = re.compile(r",(\s+)(do\s+not|don['’]t|never|always)(\s+)", re.IGNORECASE)
# a clause that opens with one of these and has a comma is "<condition>, <imperative>": the imperative is negated
OPENERS = frozenset({"if", "when", "whenever", "before", "after", "at", "each", "every", "for", "once", "unless", "while",
                     "until", "on", "in", "as", "during", "between"})
# a clause whose first word is one of these is not an imperative and is kept as it is
NOT_IMPERATIVE = OPENERS | frozenset({
    "a", "an", "the", "this", "that", "these", "those", "my", "your", "our", "their", "his", "her", "its", "it", "i",
    "you", "we", "they", "he", "she", "there", "here", "one", "two", "three", "some", "any", "all", "most", "no",
    "everything", "nothing", "anything", "something", "everyone", "nobody", "from", "to", "by", "with", "of", "about",
    "than", "and", "but", "or", "so", "because", "then", "also", "only", "not", "what", "which", "who", "where", "why",
    "how",
})


def _with_case(word: str, upper: bool) -> str:
    return word[:1].upper() + word[1:] if upper else word


def _negate_lead(clause: str) -> str:
    """The clause with its leading imperative negated (see :func:`invert_line`)."""
    m = _WORD_RE.match(clause)
    if m is None:
        return clause
    word = m.group(0)
    lower, upper = word.lower(), word[:1].isupper()
    if lower == "never":
        return _with_case("always", upper) + clause[m.end():]
    if lower == "always":
        return _with_case("never", upper) + clause[m.end():]
    dn = _DO_NOT_RE.match(clause)
    if dn is not None:
        rest = clause[dn.end():]
        return _with_case(rest, upper) if rest else rest
    if lower in NOT_IMPERATIVE:
        return clause
    verb = word if any(c.isupper() for c in word[1:]) else word[:1].lower() + word[1:]
    return _with_case("do not", upper) + " " + verb + clause[m.end():]


def _flip_later(match: re.Match[str]) -> str:
    marker = match.group(2).lower()
    if marker == "never":
        return f",{match.group(1)}always{match.group(3)}"
    if marker == "always":
        return f",{match.group(1)}never{match.group(3)}"
    return f",{match.group(1)}"  # "do not" / "don't" dropped


def _invert_clause(clause: str) -> str:
    m = _WORD_RE.match(clause)
    if m is None:
        return clause
    head, rest = "", clause
    if m.group(0).lower() in OPENERS and ", " in clause:
        cut = clause.index(", ") + 2
        head, rest = clause[:cut], clause[cut:]
    comma = rest.find(",")
    if comma >= 0:
        rest = rest[:comma] + _LATER_MARKER_RE.sub(_flip_later, rest[comma:])
    return head + _negate_lead(rest)


def invert_line(line: str) -> str:
    """One policy line with each imperative of a bullet negated; any other line is returned unchanged.

    The rule, deterministic and applied clause by clause (a bullet is split after ``.``, ``!``, ``?`` and ``;``):

    1. a clause that opens with a condition word (:data:`OPENERS`: if, when, before, at, each, for, ...) and has a
       comma keeps everything up to the first comma; the rest is the imperative;
    2. in the imperative, later comma clauses that start with ``do not`` / ``don't`` lose it, and ``never`` and
       ``always`` there trade places ("Update it, do not duplicate it." becomes "Do not update it, duplicate it.");
    3. the imperative's first word decides: ``Never Y`` becomes ``Always Y`` and ``Always Y`` becomes ``Never Y``;
       ``Do not Y`` and ``Don't Y`` become ``Y``; a word that cannot start an imperative (an article, a pronoun, a
       number, a preposition, a conjunction: :data:`NOT_IMPERATIVE`) leaves the clause as it is; any other word
       is taken for a verb and the clause becomes ``Do not <verb> ...`` ("Keep X" becomes "Do not keep X").

    Capitalisation of the first letter is kept. Headings, blank lines and prose lines are not bullets and stay.
    """
    m = _BULLET_RE.match(line)
    if m is None:
        return line
    prefix, body = m.group(1), m.group(2)
    parts = _CLAUSE_RE.split(body)  # clause, whitespace, clause, ...
    return prefix + "".join(p if i % 2 else _invert_clause(p) for i, p in enumerate(parts))


def invert_policy(text: str) -> str:
    """The policy with every imperative bullet negated by :func:`invert_line` (deterministic; no randomness)."""
    return "\n".join(invert_line(line) for line in text.split("\n"))


def policy_variant(variant: str, text: str, seed: int) -> str:
    """The text a condition transplants: ``faithful`` (as is), ``shuffled`` or ``inverted``."""
    if variant == "faithful":
        return text
    if variant == "shuffled":
        return shuffle_policy(text, seed)
    if variant == "inverted":
        return invert_policy(text)
    raise ValueError(f"unknown policy variant {variant!r}")


# --- donors -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class DonorNote:
    note_id: str
    title: str
    body: str
    importance: float
    tags: tuple[str, ...]
    created_tick: int
    hop: int
    origin_generation: int | None
    path_agents: tuple[str, ...]


@dataclass(frozen=True)
class Donor:
    run_id: str
    agent_id: str
    name: str
    status: str
    balance_usd: float
    generation: int
    policy: str | None
    notes: tuple[DonorNote, ...] = field(default=())


def origin_note_id(donor_run_id: str, donor_note_id: str) -> str:
    """The provenance origin of a transplanted note: ``transplant:<donor_run_id>:<donor_note_id>``."""
    return f"transplant:{donor_run_id}:{donor_note_id}"


def _json_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _note_file(run_dir: Path, agent_id: str, stored: str) -> Path:
    """Where a donor note's file is now: the stored path when it exists, else the same file under the run's vaults
    (a run written with a relative ``--out`` stores paths relative to the directory it was started from)."""
    p = Path(stored)
    parts = p.parts
    for k in range(len(parts) - 1, 0, -1):
        if parts[k] == agent_id and parts[k - 1] == "vaults":
            candidate = run_dir / "vaults" / agent_id / Path(*parts[k + 1:])
            if candidate.is_file():
                return candidate
            break
    if p.is_file():
        return p
    raise FileNotFoundError(f"donor note file {stored} is neither at that path nor under {run_dir / 'vaults' / agent_id}")


def _read_body(path: Path) -> str:
    rest = body_of(path.read_text(encoding="utf-8"))
    return rest[:-1] if rest.endswith("\n") else rest  # as MemoryStore reads a note


def _connect(run_dir: Path) -> sqlite3.Connection:
    db_path = run_dir / "world.db"
    if not db_path.is_file():
        raise FileNotFoundError(f"no world.db in donor run {run_dir}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _select(rows: list[sqlite3.Row], spec: str) -> list[sqlite3.Row]:
    alive = [r for r in rows if r["status"] == "alive"]
    if spec == "richest":
        if not alive:
            raise ValueError("no donor is alive at the end of the donor run")
        return [sorted(alive, key=lambda r: (-int(r["balance"]), str(r["agent_id"])))[0]]
    if spec == "all":
        if not alive:
            raise ValueError("no donor is alive at the end of the donor run")
        return sorted(alive, key=lambda r: (int(r["born_tick"]), str(r["agent_id"])))
    if spec.startswith("names:"):
        names = [n.strip() for n in spec[len("names:"):].split(",") if n.strip()]
        if not names:
            raise ValueError("--donors names: needs at least one name")
        out: list[sqlite3.Row] = []
        for name in names:
            hits = [r for r in rows if str(r["name"]) == name]
            if not hits:
                raise ValueError(f"no agent named {name!r} in the donor run (agents: {', '.join(sorted({str(r['name']) for r in rows}))})")
            hits.sort(key=lambda r: (r["status"] == "alive", int(r["born_tick"]), str(r["agent_id"])))
            out.append(hits[-1])
        return out
    raise ValueError(f"--donors must be richest, all or names:A,B (got {spec!r})")


def load_donors(run_dir: str | Path, spec: str = "richest") -> list[Donor]:
    """The donors ``spec`` names in the run at ``run_dir``, each with its current policy and its live notes.

    The donor run is only read. Notes are in creation order; those tagged :data:`SKIPPED_TAGS` are left out.
    """
    run_dir = Path(run_dir)
    conn = _connect(run_dir)
    try:
        row = conn.execute("SELECT run_id FROM run LIMIT 1").fetchone()
        if row is None:
            raise ValueError(f"donor run {run_dir} records no run")
        run_id = str(row["run_id"])
        agents = conn.execute("SELECT agent_id, name, status, balance, generation, born_tick FROM agents ORDER BY agent_id").fetchall()
        donors: list[Donor] = []
        for a in _select(agents, spec):
            aid = str(a["agent_id"])
            try:
                prow = conn.execute("SELECT text FROM policy_versions WHERE agent_id=? ORDER BY version DESC LIMIT 1", (aid,)).fetchone()
            except sqlite3.OperationalError:  # a run from before the policy layer
                prow = None
            notes: list[DonorNote] = []
            for n in conn.execute(
                "SELECT note_id, path, title, importance, tags, created_tick, hop, origin_generation, path_agents FROM notes "
                "WHERE agent_id=? AND archived=0 ORDER BY created_tick, note_id", (aid,)
            ):
                tags = tuple(_json_list(n["tags"]))
                if set(tags) & set(SKIPPED_TAGS):
                    continue
                notes.append(DonorNote(
                    note_id=str(n["note_id"]), title=str(n["title"]), body=_read_body(_note_file(run_dir, aid, str(n["path"]))),
                    importance=float(n["importance"]), tags=tags, created_tick=int(n["created_tick"]), hop=int(n["hop"]),
                    origin_generation=None if n["origin_generation"] is None else int(n["origin_generation"]),
                    path_agents=tuple(_json_list(n["path_agents"])),
                ))
            donors.append(Donor(run_id=run_id, agent_id=aid, name=str(a["name"]), status=str(a["status"]),
                                balance_usd=micro_to_usd(int(a["balance"])), generation=int(a["generation"]),
                                policy=None if prow is None else str(prow["text"]), notes=tuple(notes)))
        return donors
    finally:
        conn.close()


# --- seeding ------------------------------------------------------------------------------------------------------
def _recipients(sim: Any, n: int) -> list[Any]:
    """The first ``n`` roster agents of the evaluation world, as founded (``cfg.agents`` order)."""
    roster = [spec.name for spec in sim.cfg.agents]
    if not 1 <= n <= len(roster):
        raise ValueError(f"--recipients must be between 1 and the roster size {len(roster)} (got {n})")
    founders = {a.name: a for a in sim.registry.all() if a.born_tick == 0 and a.parent_id is None}
    out = []
    for name in roster[:n]:
        if name not in founders:
            raise ValueError(f"roster agent {name!r} was not founded in the evaluation world")
        out.append(founders[name])
    return out


def seed_transplant(sim: Any, donors: list[Donor], condition: str, *, recipients: int, seed: int) -> dict[str, Any]:
    """Give the first ``recipients`` roster agents of ``sim`` (not yet ticked) what ``condition`` transplants.

    Recipient ``i`` receives from ``donors[i % len(donors)]``. Returns the manifest, which is also stored in
    ``world_kv['transplant']`` and written to ``<run_dir>/transplant.json``.
    """
    from void.events import Event, Kind

    if condition not in _PLAN:
        raise ValueError(f"unknown condition {condition!r}; expected one of {', '.join(CONDITIONS)}")
    if not donors:
        raise ValueError("no donors")
    copy_vault, variant = _PLAN[condition]
    pairs = [(rec, donors[i % len(donors)]) for i, rec in enumerate(_recipients(sim, recipients))]
    if variant is not None:
        if not sim.cfg.memory.policy.enabled:
            raise ValueError(f"condition {condition!r} transplants a policy but memory.policy is off in the evaluation config")
        missing = sorted({d.name for _, d in pairs if d.policy is None})
        if missing:
            raise ValueError(f"condition {condition!r} transplants a policy but donor(s) {', '.join(missing)} have none")
    tick = int(sim.clock.tick)
    day = max(1, int(sim.clock.day))
    rows: list[dict[str, Any]] = []
    for rec, donor in pairs:
        here = sim.registry.get(donor.agent_id) is not None
        source = donor.agent_id if here else None
        copied = 0
        if copy_vault:
            for note in donor.notes:
                path = [a for a in note.path_agents if sim.registry.get(a) is not None] + ([donor.agent_id] if here else [])
                prov = Provenance(
                    source_agent_id=source, hop=note.hop, origin_note_id=origin_note_id(donor.run_id, note.note_id),
                    origin_generation=note.origin_generation if note.origin_generation is not None else donor.generation,
                    channel=CHANNEL, transfer_tick=tick, path_agents=path,
                )
                sim.memory.remember(rec.agent_id, tick, note.body, title=note.title, tags=[*note.tags, TRANSPLANT_TAG],
                                    importance=note.importance, provenance=prov, max_chars=len(note.body))
                copied += 1
        version = None
        text_hash = None
        if variant is not None:
            text = policy_variant(variant, donor.policy or "", seed)
            pv = sim.policy.set(rec.agent_id, text, tick, source="inherited", source_agent_id=source)
            sim.bus.emit(Event(tick, day, Kind.POLICY_CHANGED, sim.policy.event_payload(pv), rec.agent_id))
            version = pv.version
            text_hash = hashlib.sha256(pv.text.encode("utf-8")).hexdigest()[:16]
        rows.append({"agent_id": rec.agent_id, "name": rec.name, "donor_agent_id": donor.agent_id, "donor_name": donor.name,
                     "source_agent_id": source, "notes_copied": copied, "policy_variant": variant,
                     "policy_version": version, "policy_sha256": text_hash})
    manifest = {
        "condition": condition, "seed": int(seed), "tick": tick, "donor_run_id": donors[0].run_id,
        "donors": [{"agent_id": d.agent_id, "name": d.name, "status": d.status, "balance_usd": d.balance_usd,
                    "notes": len(d.notes), "has_policy": d.policy is not None} for d in donors],
        "recipients": rows, "channel": CHANNEL, "tag": TRANSPLANT_TAG, "skipped_tags": list(SKIPPED_TAGS),
    }
    sim.db.kv_set(KV_KEY, manifest)
    (Path(sim.run_dir) / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


# --- running ------------------------------------------------------------------------------------------------------
def _resolve(path: str | Path) -> Path:
    """A path relative to the cwd, falling back to the repository root (so ``configs/x.yaml`` works anywhere)."""
    p = Path(path)
    if p.exists() or p.is_absolute():
        return p
    alt = REPO_ROOT / p
    return alt if alt.exists() else p


def condition_config(config: str | Path, condition: str, seed: int, *, days: int | None = None) -> VoidConfig:
    """The evaluation config for one condition and seed: ``experiment.arm`` = condition, ``run.name`` =
    ``<run.name>_<condition>``, ``run.seed`` = seed (and ``run.days`` when given)."""
    path = _resolve(config)
    base = load_config(path)
    run: dict[str, Any] = {"seed": int(seed), "name": f"{base.run.name}_{condition}"}
    if days is not None:
        run["days"] = int(days)
    return load_config(path, {"run": run, "experiment": {"arm": condition}})


def run_dir_for(out: Path, cfg: VoidConfig) -> Path:
    return out / str(cfg.experiment.name or cfg.run.name) / str(cfg.experiment.arm) / f"seed_{cfg.run.seed}"


def _signature(donors: list[Donor], recipients: int) -> dict[str, Any]:
    return {"donor_run_id": donors[0].run_id, "donors": [d.agent_id for d in donors], "recipients": recipients}


def _existing(run_dir: Path, cfg: VoidConfig, signature: dict[str, Any]) -> str | None:
    """None when ``run_dir`` holds no run; ``same`` for a completed run of this config and transplant; else why not."""
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
    mine = {"donor_run_id": manifest.get("donor_run_id"), "donors": [d.get("agent_id") for d in manifest.get("donors", [])],
            "recipients": len(manifest.get("recipients", []))}
    if mine != signature:
        return "a run with another transplant"
    if row["status"] != "completed":
        return f"an unfinished run (status {row['status']})"
    return "same"


def _day_line(sim: Any, day: int, t0: float) -> str:
    return (f"day {day:>3} | tick {sim.clock.tick:>5} | pop {sim.registry.count_alive():>2} | "
            f"spend ${micro_to_usd(sim.wallet.spend_total):.4f} | {time.perf_counter() - t0:6.1f}s")


async def run_condition(cfg: VoidConfig, run_dir: Path, donors: list[Donor], condition: str, *, recipients: int,
                        quiet: bool = False) -> str:
    """Build the simulation, seed the transplant before the first tick, run it to the end; returns the status."""
    from void.sim.loop import Simulation

    sim = Simulation(cfg, run_dir)
    try:
        manifest = seed_transplant(sim, donors, condition, recipients=recipients, seed=cfg.run.seed)
        t0 = time.perf_counter()
        if not quiet:
            got = ", ".join(f"{r['name']}<-{r['donor_name']}({r['notes_copied']} notes"
                            f"{', policy ' + r['policy_variant'] if r['policy_variant'] else ''})" for r in manifest["recipients"])
            print(f"run {sim.run_id}: condition={condition} seed={cfg.run.seed} days={cfg.run.days} dir={run_dir}", flush=True)
            print(f"  transplant: {got if condition != 'none' else 'nothing'}", flush=True)

        def on_tick(report: Any) -> None:
            if not quiet and report.new_day and report.day > 1:
                print("  " + _day_line(sim, report.day - 1, t0), flush=True)

        status = await sim.run(on_tick=on_tick)
        if not quiet:
            if sim.clock.day >= 1:
                print("  " + _day_line(sim, sim.clock.day, t0), flush=True)
            print(f"  status={status} ticks={sim.clock.tick} population={sim.registry.count_alive()} "
                  f"elapsed={time.perf_counter() - t0:.1f}s", flush=True)
    finally:
        sim.db.close()
        gc.collect()  # release the sandbox's subprocess transports while the loop is still open
    return status


def _parse_seeds(spec: str) -> list[int]:
    """``A-B`` (inclusive), ``A,B,C`` or a single seed."""
    seeds: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:
            lo, hi = part.split("-", 1)
            a, b = int(lo), int(hi)
            if b < a:
                raise argparse.ArgumentTypeError(f"bad seed range {part!r}")
            seeds.extend(range(a, b + 1))
        else:
            seeds.append(int(part))
    if not seeds:
        raise argparse.ArgumentTypeError("no seeds given")
    return seeds


def _parse_conditions(spec: str) -> list[str]:
    out = [c.strip() for c in spec.split(",") if c.strip()]
    bad = [c for c in out if c not in _PLAN]
    if bad or not out:
        raise argparse.ArgumentTypeError(f"conditions must be among {', '.join(CONDITIONS)} (got {spec!r})")
    return list(dict.fromkeys(out))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="void transplant",
        description="E5: transplant a donor's memory policy and vault into fresh recipients, one run per condition and seed "
                    "(MEMORY_EVOLUTION 6.7). Runs land in OUT/<experiment>/<condition>/seed_<n>/ for compare.")
    p.add_argument("--donor-run", required=True, help="the donor's run directory (holds world.db and vaults/)")
    p.add_argument("--donors", default="richest", help="richest | all | names:A,B (default richest)")
    p.add_argument("--conditions", type=_parse_conditions, default=_parse_conditions(DEFAULT_CONDITIONS),
                   help=f"comma-separated, among {', '.join(CONDITIONS)} (default all six)")
    p.add_argument("--config", default=DEFAULT_CONFIG, help=f"the evaluation world (default {DEFAULT_CONFIG})")
    p.add_argument("--recipients", type=int, default=3, help="the first N roster agents receive (default 3)")
    p.add_argument("--seeds", type=_parse_seeds, required=True, help="A-B (inclusive), A,B,C or one seed")
    p.add_argument("--out", default=None, help="output root (default the config's run.data_dir)")
    p.add_argument("--days", type=int, default=None, help="override run.days (smoke runs; default the config's)")
    p.add_argument("--quiet", action="store_true", help="one line per run instead of per day")
    return p


def cli_main(argv: list[str]) -> int:
    """``void transplant ...``: 0 when every run completed (or was already complete), 2 otherwise."""
    from void.economy.budget import reachable_providers

    args = build_parser().parse_args(argv)
    try:
        cfg0 = condition_config(args.config, args.conditions[0], args.seeds[0], days=args.days)
    except (OSError, ValueError) as e:
        print(f"error: config {args.config}: {e}", file=sys.stderr)
        return 2
    paid = reachable_providers(cfg0)
    if paid:
        print(f"refused: the evaluation config reaches a paid provider ({', '.join(sorted(paid))}); the transplant harness "
              f"runs scripted worlds only and is not counted against a live budget", file=sys.stderr)
        return 2
    for w in cfg0.warnings():
        print(f"warning: {w}", file=sys.stderr)
    try:
        donors = load_donors(_resolve(args.donor_run), args.donors)
        if not 1 <= args.recipients <= len(cfg0.agents):
            raise ValueError(f"--recipients must be between 1 and the roster size {len(cfg0.agents)} (got {args.recipients})")
        need_policy = [c for c in args.conditions if _PLAN[c][1] is not None]
        lacking = sorted({d.name for d in donors[: args.recipients] if d.policy is None})
        if need_policy and lacking:
            raise ValueError(f"condition(s) {', '.join(need_policy)} transplant a policy but donor(s) {', '.join(lacking)} have none")
        if need_policy and not cfg0.memory.policy.enabled:
            raise ValueError(f"condition(s) {', '.join(need_policy)} transplant a policy but memory.policy is off in {args.config}")
    except (OSError, ValueError, sqlite3.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if len(donors) > args.recipients:
        print(f"warning: {len(donors)} donors but {args.recipients} recipients; donors after the first {args.recipients} "
              f"receive no recipient", file=sys.stderr)
    out = Path(args.out) if args.out else Path(cfg0.run.data_dir)
    print("donors: " + ", ".join(f"{d.name} ({d.agent_id}, {d.status}, ${d.balance_usd:.4f}, {len(d.notes)} notes, "
                                  f"{'policy' if d.policy is not None else 'no policy'})" for d in donors)
          + f" from {donors[0].run_id}", flush=True)
    signature = _signature(donors, args.recipients)
    statuses: dict[tuple[str, int], str] = {}
    for condition in args.conditions:
        for seed in args.seeds:
            cfg = condition_config(args.config, condition, seed, days=args.days)
            run_dir = run_dir_for(out, cfg)
            state = _existing(run_dir, cfg, signature)
            if state == "same":
                print(f"run dir {run_dir} already holds this completed run: skipped", flush=True)
                statuses[(condition, seed)] = "completed"
                continue
            if state is not None:
                print(f"run dir {run_dir} already holds {state}: remove it or pass another --out", file=sys.stderr)
                statuses[(condition, seed)] = "exists"
                continue
            try:
                status = asyncio.run(run_condition(cfg, run_dir, donors, condition, recipients=args.recipients, quiet=args.quiet))
            except (RuntimeError, ValueError) as e:
                print(f"error: {condition} seed {seed}: {e}", file=sys.stderr)
                status = "error"
            if args.quiet:
                print(f"{condition} seed {seed}: {status} ({run_dir})", flush=True)
            statuses[(condition, seed)] = status
    done = sum(1 for s in statuses.values() if s == "completed")
    exp_dir = out / str(cfg0.experiment.name or cfg0.run.name)
    print(f"transplant: {done}/{len(statuses)} runs completed under {exp_dir}; compare with: python scripts/compare.py {exp_dir}",
          flush=True)
    return 0 if statuses and done == len(statuses) else 2
