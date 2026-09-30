"""Per-run analysis for the research harness (DESIGN §17).

Everything here takes a run directory (``<exp>/<arm>/seed_<n>/`` as written by the simulation:
``world.db``, ``metrics.jsonl``, ``config.resolved.yaml``, ``vaults/``) and returns plain
numbers or dicts. Nothing here writes to the run.

Outcome resolution (:func:`end_of_run_outcome`), in order:

1. ``tick.<path>`` / ``day.<path>`` prefixes force the last tick row or the last day row;
2. a bare name that is a registered computed outcome (:data:`COMPUTED`) is computed from
   ``world.db`` as a run-level aggregate (``false_claim_rate`` over every decide call, the
   recovered collapse stress, ``probe_recall``, the benefactor outcomes, ...);
3. otherwise the dotted path is resolved against the last tick row, then the last day row
   (``by_tier.alpha.action_regret_mean``, ``notes_by_channel.chronicle``, ``mean_balance``, ...).

``probe_recall``: the loop plants a two-hop probe for every alive agent every
``memory.probe_every_ticks`` ticks and checks it on the next tick, accumulating
``probe_hits`` / ``probe_total`` on the tick rows; that in-loop cumulative rate is what
:func:`probe_recall` returns when it exists. For a run made without probes the function
evaluates offline on a *copy* of the run (SQLite backup of ``world.db`` plus a copy of the
vaults, note paths rewritten to the copy): for every alive agent with at least three content
notes it plants a probe at ``T+1`` and checks recall at ``T+2`` through the same
``MemoryStore.plant_probe`` / ``check_probe`` the loop uses. ``replay=True`` instead re-runs
the simulation from the run's resolved config with probes enabled, in a temporary directory.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from void.config import VoidConfig, micro_to_usd

__all__ = [
    "COMPUTED",
    "DAILY",
    "SUMMARY_KEYS",
    "WINDFALL_TAG",
    "collapse_curve",
    "collapse_curves",
    "connect",
    "costly_actions_post_windfall",
    "end_of_run_outcome",
    "exogenous_vocab",
    "exogenous_vocab_count",
    "false_claim_rate",
    "load_metrics",
    "notes_by_channel",
    "persistence",
    "probe_recall",
    "recovered_collapse_stress",
    "resolve_path",
    "run_config",
    "run_config_dict",
    "summarize",
    "time_to_50pct_tracer_reach",
    "windfall_lineage_penetration",
    "windfall_origins",
]

METRICS_FILE = "metrics.jsonl"
DB_FILE = "world.db"
CONFIG_FILE = "config.resolved.yaml"
WINDFALL_TAG = "windfall_seeded"
STRESS_BIN_WIDTH = 0.1
MIN_BIN_COUNT = 5          # a stress bin needs this many decide calls to enter the collapse fit
REGRET_RISE = 0.25         # the doubling target is at least this far (utility units) above the baseline
COLLAPSED_P_CHOSEN = 0.5   # a policy whose modal draw is a coin flip at rest is already collapsed
COSTLY_LEDGER_KINDS = ("transfer_out", "gadget_fee", "gadget_use_fee", "pitch_fee")
COSTLY_EVENT_KINDS = ("nudge_weather",)
_NON_CONTENT_TAGS = frozenset({"entity", "self", "probe"})
_WORD_RE = re.compile(r"[a-z][a-z'-]{2,}")
_STOPWORDS = frozenset(
    "the and for that with this from you your are was were has have had not but they them their its our out "
    "all any can one two who how why what when where which will would could should there here than then".split()
)

SUMMARY_KEYS = (
    "run_id", "seed", "experiment", "arm", "status", "ended_tick", "days", "ticks_per_day", "population",
    "mean_balance", "deaths", "births", "gadgets", "gossip_transfers", "gossip_similarity_by_hop",
    "notes_by_channel", "tracer_reach", "tracer_adopters", "false_claim_rate", "claims_made",
    "degeneration_by_tier", "invalid_action_rate", "probe_recall", "calls", "real_cost_usd", "world_cost_usd",
)


# --- loading -------------------------------------------------------------------------------------------
@contextmanager
def connect(run_dir: str | Path) -> Iterator[sqlite3.Connection]:
    """``with connect(run_dir) as db:`` a plain sqlite3 connection to ``world.db``, closed on exit; never written to."""
    path = Path(run_dir) / DB_FILE
    if not path.is_file():
        raise FileNotFoundError(f"no {DB_FILE} in {run_dir}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def run_config_dict(run_dir: str | Path) -> dict[str, Any]:
    """The resolved config the run was made from, as a plain dict."""
    path = Path(run_dir) / CONFIG_FILE
    if path.is_file():
        data = yaml.safe_load(path.read_text()) or {}
    else:
        with connect(run_dir) as db:
            row = db.execute("SELECT config_yaml FROM run LIMIT 1").fetchone()
        data = yaml.safe_load(row["config_yaml"]) if row else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a mapping")
    return data


def run_config(run_dir: str | Path) -> VoidConfig:
    return VoidConfig.model_validate(run_config_dict(run_dir))


def load_metrics(run_dir: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(tick_rows, day_rows)`` from ``metrics.jsonl`` (or the ``metrics`` table), each sorted."""
    path = Path(run_dir) / METRICS_FILE
    rows: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    elif (Path(run_dir) / DB_FILE).is_file():
        with connect(run_dir) as db:
            rows = [json.loads(r["json"]) for r in db.execute("SELECT json FROM metrics")]
    tick_rows = sorted((r for r in rows if r.get("kind") != "day"), key=lambda r: int(r.get("tick", 0)))
    day_rows = sorted((r for r in rows if r.get("kind") == "day"), key=lambda r: int(r.get("day", 0)))
    return tick_rows, day_rows


def resolve_path(obj: Any, path: str) -> Any:
    """Walk a dotted path through nested dicts and lists (``by_tier.alpha.calls``, ``agents.0.tier``)."""
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, dict):
            if seg not in cur:
                return None
            cur = cur[seg]
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float) and not (isinstance(value, float) and math.isnan(value)):
        return float(value)
    return None


def _mean(xs: Iterable[float]) -> float | None:
    vals = [float(x) for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def _last_tick(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT value FROM world_kv WHERE key='tick'").fetchone()
    if row is not None:
        return int(json.loads(row["value"]))
    row = db.execute("SELECT COALESCE(MAX(tick), 0) AS t FROM llm_calls").fetchone()
    return int(row["t"])


def _alive_ids(db: sqlite3.Connection) -> list[str]:
    return [str(r["agent_id"]) for r in db.execute("SELECT agent_id FROM agents WHERE status='alive' ORDER BY agent_id")]


# --- run-level rates over llm_calls -------------------------------------------------------------------------
def _call_filter(run_dir: str | Path, tier: str | None, day: int | None) -> tuple[str, tuple[Any, ...]]:
    sql, params = "", ()
    if tier:
        sql += " AND tier=?"
        params += (tier,)
    if day is not None:
        tpd = run_config(run_dir).run.ticks_per_day
        sql += " AND tick BETWEEN ? AND ?"
        params += ((day - 1) * tpd + 1, day * tpd)
    return sql, params


def _call_rate(run_dir: str | Path, column: str, tier: str | None = None, day: int | None = None) -> float | None:
    """Mean of an ``llm_calls`` measurement over decide calls (optionally one tier and/or one day)."""
    extra, params = _call_filter(run_dir, tier, day)
    sql = f"SELECT AVG({column}) AS v FROM llm_calls WHERE purpose='decide' AND {column} IS NOT NULL" + extra
    with connect(run_dir) as db:
        row = db.execute(sql, params).fetchone()
    return None if row is None or row["v"] is None else float(row["v"])


def false_claim_rate(run_dir: str | Path, tier: str | None = None, day: int | None = None) -> float | None:
    """Run-level ``claims_false / claims_made`` over every decide call (None without claims)."""
    extra, params = _call_filter(run_dir, tier, day)
    sql = "SELECT SUM(claims_made) AS m, SUM(claims_false) AS f FROM llm_calls WHERE purpose='decide'" + extra
    with connect(run_dir) as db:
        row = db.execute(sql, params).fetchone()
    made = int(row["m"] or 0) if row else 0
    return (int(row["f"] or 0) / made) if made else None


def notes_by_channel(run_dir: str | Path, channel: str | None = None, day: int | None = None) -> float | None:
    """Live note count for ``channel`` from the day rows (``day`` selects one, default the last); a channel
    absent from the row counts as 0, so ``notes_by_channel.chronicle`` is 0 rather than None when the
    chronicle never seeded a note. Without a channel, the total."""
    from void.memory.notes import CHANNELS

    _, day_rows = load_metrics(run_dir)
    rows = [r for r in day_rows if day is None or int(r.get("day", 0)) == day]
    if not rows:
        return None
    counts = rows[-1].get("notes_by_channel") or {}
    if channel is None:
        return float(sum(int(v) for v in counts.values()))
    if channel not in CHANNELS and channel not in counts:
        return None
    return float(counts.get(channel, 0))


# --- collapse fit (exp_tiers) --------------------------------------------------------------------------
def _stress_bin(stress: float) -> float:
    return round(min(10, int(stress / STRESS_BIN_WIDTH + 1e-9)) * STRESS_BIN_WIDTH, 1)


def collapse_curve(run_dir: str | Path, tier: str | None = None) -> dict[float, dict[str, float | None]]:
    """Per 0.1-wide stress bin: ``{"n", "mean"}`` of ``action_regret`` and mean ``p_chosen`` from ``llm_calls``."""
    sql = ("SELECT stress, action_regret, p_chosen FROM llm_calls WHERE purpose='decide' "
           "AND action_regret IS NOT NULL AND stress IS NOT NULL")
    params: tuple[Any, ...] = ()
    if tier:
        sql += " AND tier=?"
        params = (tier,)
    acc: dict[float, list[tuple[float, float | None]]] = {}
    with connect(run_dir) as db:
        for r in db.execute(sql, params):
            p = None if r["p_chosen"] is None else float(r["p_chosen"])
            acc.setdefault(_stress_bin(float(r["stress"])), []).append((float(r["action_regret"]), p))
    return {b: {"n": float(len(v)), "mean": sum(x for x, _ in v) / len(v), "p_chosen": _mean(p for _, p in v)}
            for b, v in sorted(acc.items())}


def collapse_curves(run_dir: str | Path) -> dict[str, dict[float, dict[str, float]]]:
    with connect(run_dir) as db:
        tiers = [str(r["tier"]) for r in db.execute("SELECT DISTINCT tier FROM llm_calls WHERE purpose='decide' ORDER BY tier")]
    return {t: collapse_curve(run_dir, t) for t in tiers}


def recovered_collapse_stress(run_dir: str | Path, tier: str | None = None, *, min_bin_count: int = MIN_BIN_COUNT,
                              regret_rise: float = REGRET_RISE, collapsed_p_chosen: float = COLLAPSED_P_CHOSEN) -> float | None:
    """The stress bin at which the tier's mean ``action_regret`` first exceeds twice its lowest-bin value.

    Bins are 0.0, 0.1, ..., 1.0 over ``llm_calls.stress``; only bins holding at least ``min_bin_count``
    decide calls take part, and the "lowest bin" is the lowest such bin. Two guards make the fit stable
    on short runs (2 days x 12 ticks recover the hidden ``scripted_beta`` order on every seed tried):

    * the doubling target is ``max(2 * base, base + regret_rise)``, so a near-zero baseline of a sharp
      policy is not "doubled" by noise of a few hundredths of a utility unit;
    * a tier whose lowest bin already has mean ``p_chosen < collapsed_p_chosen`` (the modal action is a
      coin flip at rest) is collapsed from the start and returns that lowest bin, since a flat softmax
      cannot double a regret that already sits at its ceiling.

    ``None`` when no bin qualifies or regret never doubles within the observed stress range
    (right-censored). ``tier=None`` pools every tier, which for a single-tier roster is that tier's fit.
    """
    curve = collapse_curve(run_dir, tier)
    bins = [b for b, c in curve.items() if (c["n"] or 0) >= min_bin_count]
    if not bins:
        return None
    lowest = curve[bins[0]]
    p0 = lowest["p_chosen"]
    if p0 is not None and p0 < collapsed_p_chosen:
        return bins[0]
    base = float(lowest["mean"] or 0.0)
    threshold = max(2.0 * base, base + regret_rise)
    for b in bins[1:]:
        if float(curve[b]["mean"] or 0.0) > threshold:
            return b
    return None


# --- probes (exp_memory) --------------------------------------------------------------------------------
def probe_recall(run_dir: str | Path, *, replay: bool = False, min_notes: int = 3) -> float | None:
    """Cumulative probe recall: in-loop when the run recorded probes, else offline on a copy (see module doc)."""
    tick_rows, _ = load_metrics(run_dir)
    for row in reversed(tick_rows):
        if row.get("probe_total"):
            return _num(row.get("probe_recall"))
    if replay:
        return _probe_recall_replay(run_dir)
    return _probe_recall_offline(run_dir, min_notes=min_notes)


def _rewrite_note_paths(db: sqlite3.Connection, src_vaults: Path, dst_vaults: Path) -> None:
    src_root = src_vaults.resolve()
    for r in db.execute("SELECT note_id, path FROM notes").fetchall():
        try:
            rel = Path(str(r["path"])).resolve().relative_to(src_root)
        except ValueError:
            continue
        db.execute("UPDATE notes SET path=? WHERE note_id=?", (str(dst_vaults / rel), r["note_id"]))
    db.commit()


def _probe_recall_offline(run_dir: str | Path, *, min_notes: int = 3) -> float | None:
    from void.db import Database
    from void.ids import IdFactory
    from void.memory.store import MemoryStore

    run_dir = Path(run_dir)
    src_vaults = run_dir / "vaults"
    if not (run_dir / DB_FILE).is_file() or not src_vaults.is_dir():
        return None
    cfg = run_config(run_dir)
    with tempfile.TemporaryDirectory(prefix="void-probe-") as tmp:
        tmp_dir = Path(tmp)
        dst_vaults = tmp_dir / "vaults"
        shutil.copytree(src_vaults, dst_vaults)
        src = sqlite3.connect(str(run_dir / DB_FILE))
        dst = sqlite3.connect(str(tmp_dir / DB_FILE))
        try:
            src.backup(dst)  # reads through the WAL, unlike a file copy
        finally:
            src.close()
        dst.row_factory = sqlite3.Row
        _rewrite_note_paths(dst, src_vaults, dst_vaults)
        dst.close()
        db = Database(tmp_dir / DB_FILE)
        try:
            ids = IdFactory("probe-eval", start=int(db.kv_get("id_counter", 0)))
            store = MemoryStore(db, cfg.memory, ids, dst_vaults)
            tick = _last_tick(db.conn)
            hits = total = 0
            for aid in _alive_ids(db.conn):
                content = [n for n in store.list_notes(aid) if not (_NON_CONTENT_TAGS & set(n.tags))]
                if len(content) < min_notes:
                    continue
                planted = store.plant_probe(aid, tick + 1)
                if planted is None:
                    continue
                total += 1
                hits += int(store.check_probe(aid, planted[0], planted[1], tick + 2))
        finally:
            db.close()
    return (hits / total) if total else None


def _probe_recall_replay(run_dir: str | Path) -> float | None:
    import asyncio

    from void.sim.loop import Simulation

    cfg = run_config(run_dir).model_copy(deep=True)
    if cfg.memory.probe_every_ticks <= 0:
        cfg.memory.probe_every_ticks = 6
    with tempfile.TemporaryDirectory(prefix="void-replay-") as tmp:
        sim = Simulation(cfg, Path(tmp) / "replay")
        try:
            asyncio.run(sim.run())
        finally:
            sim.db.close()
        tick_rows, _ = load_metrics(Path(tmp) / "replay")
    for row in reversed(tick_rows):
        if row.get("probe_total"):
            return _num(row.get("probe_recall"))
    return None


# --- tracer (exp_spread) --------------------------------------------------------------------------------
def time_to_50pct_tracer_reach(run_dir: str | Path) -> float | None:
    """First tick at which ``tracer_reach / population >= 0.5``; None if never."""
    tick_rows, _ = load_metrics(run_dir)
    for row in tick_rows:
        pop = row.get("population") or 0
        reach = row.get("tracer_reach")
        if pop and reach is not None and reach / pop >= 0.5:
            return float(row["tick"])
    return None


# --- benefactor (exp_benefactor) ------------------------------------------------------------------------
def _grants(db: sqlite3.Connection) -> list[tuple[int, str]]:
    return [(int(r["tick"]), str(r["agent_id"])) for r in db.execute(
        "SELECT tick, agent_id FROM events WHERE kind='benefactor_grant' AND agent_id IS NOT NULL ORDER BY tick, seq")]


def windfall_origins(run_dir: str | Path) -> set[str]:
    """Origin ids of every note tagged ``windfall_seeded`` (the note itself, or its origin if it is a copy)."""
    out: set[str] = set()
    with connect(run_dir) as db:
        for r in db.execute("SELECT note_id, origin_note_id, tags FROM notes WHERE tags LIKE ?", (f"%{WINDFALL_TAG}%",)):
            try:
                tags = json.loads(r["tags"] or "[]")
            except ValueError:
                tags = []
            if WINDFALL_TAG in tags:
                out.add(str(r["origin_note_id"] or r["note_id"]))
    return out


def _store(run_dir: str | Path) -> tuple[Any, Any]:
    """``(Database, MemoryStore)`` over the run, for the lineage helpers (read paths only)."""
    from void.db import Database
    from void.ids import IdFactory
    from void.memory.store import MemoryStore

    cfg = run_config(run_dir)
    db = Database(Path(run_dir) / DB_FILE)
    return db, MemoryStore(db, cfg.memory, IdFactory("analysis"), Path(run_dir) / "vaults")


def windfall_lineage_penetration(run_dir: str | Path) -> float | None:
    """Fraction of alive agents holding a descendant of any ``windfall_seeded`` note (``MemoryStore.holders_of``)."""
    with connect(run_dir) as db:
        alive = _alive_ids(db)
    if not alive:
        return None
    origins = windfall_origins(run_dir)
    if not origins:
        return 0.0
    db, store = _store(run_dir)
    try:
        holders: set[str] = set()
        for origin in sorted(origins):
            holders |= store.holders_of(origin)
    finally:
        db.close()
    return len(holders & set(alive)) / len(alive)


def _windfall_descendants(run_dir: str | Path) -> list[Any]:
    origins = windfall_origins(run_dir)
    if not origins:
        return []
    db, store = _store(run_dir)
    try:
        out = []
        for origin in sorted(origins):
            out.extend(store.descendants_of(origin))
        return out
    finally:
        db.close()


def _exclusion_vocab(cfg: VoidConfig) -> set[str]:
    """Words the world itself injects: system prompts, ACTION_DOCS, benefactor strings, the observation
    template and the scripted stand-in's speech/note templates (module sources), labels and roster names."""
    import void.agents.models as models_mod
    import void.brain.prompt as prompt_mod
    import void.brain.scripted as scripted_mod
    import void.culture.chronicle as chronicle_mod
    import void.culture.gossip as gossip_mod
    import void.memory.notes as notes_mod
    from void.brain.prompt import ACTION_DOCS, system_prompt
    from void.economy.benefactor import LEGIBLE_STRING, OPAQUE_STRING
    from void.types import STRESS_LABELS

    texts: list[str] = [system_prompt(cfg, t) for t in cfg.tiers]
    texts += list(ACTION_DOCS.values()) + [OPAQUE_STRING, LEGIBLE_STRING]
    for mod in (prompt_mod, scripted_mod, models_mod, gossip_mod, chronicle_mod, notes_mod):
        try:
            texts.append(inspect.getsource(mod))
        except OSError:
            pass
    texts.append(" ".join(label for label, _ in STRESS_LABELS))
    texts.append("storm overcast mild bright scorching famine lean normal bountiful rich thin empty comfortable")
    texts.append(" ".join(a.name for a in cfg.agents))
    words: set[str] = set()
    for t in texts:
        words |= set(_WORD_RE.findall(t.lower()))
    return words | set(_STOPWORDS)


def exogenous_vocab(run_dir: str | Path, k: int = 1) -> dict[str, Any]:
    """Content words in windfall-seeded notes (and their copies) and in recipients' talks within ``k``
    ticks of a grant that appear in no system prompt, observation template or injected string."""
    cfg = run_config(run_dir)
    exclude = _exclusion_vocab(cfg)
    texts: list[str] = []
    for note in _windfall_descendants(run_dir):
        texts.append(f"{note.title} {note.body}")
    n_notes = len(texts)
    with connect(run_dir) as db:
        grants = _grants(db)
        talks = 0
        for tick, aid in grants:
            for r in db.execute("SELECT payload FROM events WHERE kind='talk' AND agent_id=? AND tick BETWEEN ? AND ?",
                                (aid, tick, tick + k)):
                texts.append(str(json.loads(r["payload"]).get("text", "")))
                talks += 1
    words: set[str] = set()
    for t in texts:
        words |= {w for w in _WORD_RE.findall(t.lower()) if w not in exclude}
    return {"words": sorted(words), "count": len(words), "sources": {"notes": n_notes, "talks": talks}}


def exogenous_vocab_count(run_dir: str | Path) -> float | None:
    return float(exogenous_vocab(run_dir)["count"])


def costly_actions_post_windfall(run_dir: str | Path, k: int = 12) -> float | None:
    """Per recipient and grant: rate of transfer / gadget_fee / gadget_use_fee / pitch_fee ledger entries and
    nudge_weather events in the ``k`` ticks after the grant minus the rate in the ``k`` ticks before, averaged."""
    with connect(run_dir) as db:
        grants = _grants(db)
        if not grants:
            return None
        costly: dict[str, list[int]] = {}
        marks = ",".join("?" * len(COSTLY_LEDGER_KINDS))
        for r in db.execute(f"SELECT tick, agent_id FROM wallet_ledger WHERE kind IN ({marks})", COSTLY_LEDGER_KINDS):
            costly.setdefault(str(r["agent_id"]), []).append(int(r["tick"]))
        marks = ",".join("?" * len(COSTLY_EVENT_KINDS))
        for r in db.execute(f"SELECT tick, agent_id FROM events WHERE kind IN ({marks}) AND agent_id IS NOT NULL", COSTLY_EVENT_KINDS):
            costly.setdefault(str(r["agent_id"]), []).append(int(r["tick"]))
    deltas: list[float] = []
    for tick, aid in grants:
        ticks = costly.get(aid, [])
        after = sum(1 for t in ticks if tick < t <= tick + k)
        before = sum(1 for t in ticks if tick - k <= t < tick)
        deltas.append((after - before) / k)
    return _mean(deltas)


def persistence(run_dir: str | Path) -> float | None:
    """Windfall-lineage copies (gossip / inherited descendants) arriving after ``benefactor.stop_day`` divided
    by those arriving on or before it; None without a stop day or without copies before it."""
    cfg = run_config(run_dir)
    stop_day = cfg.benefactor.stop_day
    if stop_day is None:
        return None
    stop_tick = stop_day * cfg.run.ticks_per_day
    origins = windfall_origins(run_dir)
    before = after = 0
    for note in _windfall_descendants(run_dir):
        if note.note_id in origins:
            continue
        arrived = note.provenance.transfer_tick if note.provenance.transfer_tick is not None else note.created_tick
        if int(arrived) > stop_tick:
            after += 1
        else:
            before += 1
    return (after / before) if before else None


# --- registry + resolution ---------------------------------------------------------------------------
def _with_tier(fn: Callable[..., float | None]) -> Callable[[str | Path, str | None], float | None]:
    return lambda run_dir, arg: fn(run_dir, arg)


def _no_arg(fn: Callable[[str | Path], float | None]) -> Callable[[str | Path, str | None], float | None]:
    return lambda run_dir, arg: fn(run_dir)


_CALL_COLUMNS = {
    "invalid_action_rate": "invalid_action", "stale_rate": "stale_action", "perseveration_rate": "perseveration",
    "action_regret_mean": "action_regret", "text_coherence_mean": "text_coherence", "p_chosen_mean": "p_chosen",
    "action_entropy_w8_mean": "action_entropy_w8",
}

COMPUTED: dict[str, Callable[[str | Path, str | None], float | None]] = {
    "recovered_collapse_stress": _with_tier(recovered_collapse_stress),
    "false_claim_rate": _with_tier(false_claim_rate),
    "notes_by_channel": _with_tier(notes_by_channel),
    **{name: _with_tier(lambda rd, tier=None, col=col: _call_rate(rd, col, tier)) for name, col in _CALL_COLUMNS.items()},
    "probe_recall": _no_arg(probe_recall),
    "time_to_50pct_tracer_reach": _no_arg(time_to_50pct_tracer_reach),
    "windfall_lineage_penetration": _no_arg(windfall_lineage_penetration),
    "costly_actions_post_windfall": _no_arg(costly_actions_post_windfall),
    "persistence": _no_arg(persistence),
    "exogenous_vocab_count": _no_arg(exogenous_vocab_count),
}

# per-day versions of the call-level aggregates, for the daily curves compare shows (not tests)
DAILY: dict[str, Callable[[str | Path, str | None, int], float | None]] = {
    "false_claim_rate": lambda rd, arg, day: false_claim_rate(rd, arg, day),
    "notes_by_channel": lambda rd, arg, day: notes_by_channel(rd, arg, day),
    **{name: (lambda rd, arg, day, col=col: _call_rate(rd, col, arg, day)) for name, col in _CALL_COLUMNS.items()},
}


def end_of_run_outcome(run_dir: str | Path, path: str) -> float | None:
    """Resolve an outcome path for one run (see the module docstring for the order)."""
    if path.startswith("tick."):
        tick_rows, _ = load_metrics(run_dir)
        return _num(resolve_path(tick_rows[-1], path[5:])) if tick_rows else None
    if path.startswith("day."):
        _, day_rows = load_metrics(run_dir)
        return _num(resolve_path(day_rows[-1], path[4:])) if day_rows else None
    head, _, rest = path.partition(".")
    if head in COMPUTED:
        return _num(COMPUTED[head](run_dir, rest or None))
    tick_rows, day_rows = load_metrics(run_dir)
    if tick_rows:
        v = _num(resolve_path(tick_rows[-1], path))
        if v is not None:
            return v
    if day_rows:
        return _num(resolve_path(day_rows[-1], path))
    return None


# --- summary -------------------------------------------------------------------------------------------
def summarize(run_dir: str | Path, *, benefactor: bool = False) -> dict[str, Any]:
    """One dict per run with the keys in :data:`SUMMARY_KEYS` (plus ``benefactor`` when asked or enabled)."""
    run_dir = Path(run_dir)
    cfg = run_config(run_dir)
    tick_rows, day_rows = load_metrics(run_dir)
    last = tick_rows[-1] if tick_rows else {}
    with connect(run_dir) as db:
        run = db.execute("SELECT run_id, seed, status, ended_tick, experiment, arm FROM run LIMIT 1").fetchone()
        alive = _alive_ids(db)
        balances = [micro_to_usd(int(r["balance"])) for r in db.execute("SELECT balance FROM agents WHERE status='alive'")]
        deaths = int(db.execute("SELECT COUNT(*) AS n FROM events WHERE kind='death'").fetchone()["n"])
        births = int(db.execute("SELECT COUNT(*) AS n FROM events WHERE kind='birth'").fetchone()["n"])
        gadgets = {str(r["status"]): int(r["n"]) for r in db.execute("SELECT status, COUNT(*) AS n FROM gadgets GROUP BY status")}
        sims: dict[str, list[float]] = {}
        n_gossip = 0
        for r in db.execute("SELECT payload FROM events WHERE kind='gossip_transfer'"):
            p = json.loads(r["payload"])
            n_gossip += 1
            sims.setdefault(str(p.get("hop", 0)), []).append(float(p.get("similarity", 0.0)))
        notes_by_channel = {str(r["channel"]): int(r["n"]) for r in db.execute(
            "SELECT channel, COUNT(*) AS n FROM notes WHERE archived=0 GROUP BY channel ORDER BY channel")}
        degeneration_by_tier: dict[str, int] = {}
        for r in db.execute("SELECT payload FROM events WHERE kind='degeneration'"):
            t = str(json.loads(r["payload"]).get("tier"))
            degeneration_by_tier[t] = degeneration_by_tier.get(t, 0) + 1
        calls = db.execute("SELECT COUNT(*) AS n, COALESCE(SUM(real_cost),0) AS rc, COALESCE(SUM(world_cost),0) AS wc, "
                           "SUM(claims_made) AS m FROM llm_calls WHERE purpose='decide'").fetchone()
        grants = _grants(db)
    out: dict[str, Any] = {
        "run_id": run["run_id"] if run else None, "seed": int(run["seed"]) if run else cfg.run.seed,
        "experiment": run["experiment"] if run else cfg.experiment.name, "arm": run["arm"] if run else cfg.experiment.arm,
        "status": run["status"] if run else None, "ended_tick": run["ended_tick"] if run else None,
        "days": cfg.run.days, "ticks_per_day": cfg.run.ticks_per_day,
        "population": len(alive), "mean_balance": _mean(balances), "deaths": deaths, "births": births,
        "gadgets": {"verified": gadgets.get("verified", 0), "rejected": gadgets.get("rejected", 0), "proposed": gadgets.get("proposed", 0)},
        "gossip_transfers": n_gossip, "gossip_similarity_by_hop": {h: _mean(v) for h, v in sorted(sims.items())},
        "notes_by_channel": notes_by_channel,
        "tracer_reach": last.get("tracer_reach"), "tracer_adopters": last.get("tracer_adopters"),
        "false_claim_rate": false_claim_rate(run_dir), "claims_made": int(calls["m"] or 0),
        "degeneration_by_tier": degeneration_by_tier, "invalid_action_rate": _call_rate(run_dir, "invalid_action"),
        "probe_recall": probe_recall(run_dir) if cfg.memory.probe_every_ticks > 0 else None,
        "calls": int(calls["n"]), "real_cost_usd": round(micro_to_usd(int(calls["rc"])), 6),
        "world_cost_usd": round(micro_to_usd(int(calls["wc"])), 6),
        "day_rows": len(day_rows),
    }
    if benefactor or cfg.benefactor.enabled:
        out["benefactor"] = {
            "disclosure": cfg.benefactor.disclosure, "grants": len(grants), "grant_ticks": [t for t, _ in grants],
            "windfall_lineage_penetration": windfall_lineage_penetration(run_dir),
            "exogenous_vocab": exogenous_vocab(run_dir),
            "costly_actions_post_windfall": costly_actions_post_windfall(run_dir),
            "persistence": persistence(run_dir),
        }
    return out
