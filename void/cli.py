"""Command-line entry point: ``void run | serve | schema | mock-feed | inspect`` (DESIGN §2).

* ``run``       headless simulation (one seed or a ``--seeds A-B`` sweep); exit 0 when completed, 2 otherwise
* ``serve``     the simulation behind the HTTP/WebSocket server (DESIGN §15)
* ``schema``    export the wire schemas for the frontend
* ``mock-feed`` record a genuine scripted run as the JSONL message stream a socket would receive
* ``inspect``   look inside a run directory (lineage, money, events, reindex the notes index)
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from void.config import VoidConfig, load_config, micro_to_usd

__all__ = ["main", "build_parser", "record_feed"]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = "data/runs"
DEFAULT_STATIC = "web/dist"
DEFAULT_MOCK_CONFIG = "configs/scripted_smoke.yaml"
DEFAULT_MOCK_OUT = "web/public/fixtures/mock.jsonl"


# --- helpers ------------------------------------------------------------------------------------------------
def _resolve(path: str | Path) -> Path:
    """A path relative to the cwd, falling back to the repository root (so ``configs/x.yaml`` works anywhere)."""
    p = Path(path)
    if p.exists() or p.is_absolute():
        return p
    alt = REPO_ROOT / p
    return alt if alt.exists() else p


def _run_overrides(seed: int | None = None, days: int | None = None, tick_seconds: float | None = None,
                   name: str | None = None) -> dict[str, Any]:
    run: dict[str, Any] = {}
    if seed is not None:
        run["seed"] = int(seed)
    if days is not None:
        run["days"] = int(days)
    if tick_seconds is not None:
        run["tick_seconds"] = float(tick_seconds)
    if name is not None:
        run["name"] = name
    return {"run": run} if run else {}


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


def _run_dir(out: Path, cfg: VoidConfig, *, sweep: bool) -> Path:
    if sweep:
        return out / (cfg.experiment.name or cfg.run.name) / (cfg.experiment.arm or "default") / f"seed_{cfg.run.seed}"
    return out / cfg.run.name


def _day_line(sim: Any, day: int, t0: float) -> str:
    row = sim.db.fetchone("SELECT json FROM metrics WHERE tick=?", (-int(day),))
    d = json.loads(row["json"]) if row else {}
    return (f"day {day:>3} | tick {sim.clock.tick:>5} | pop {sim.registry.count_alive():>2} | "
            f"deaths {d.get('deaths', 0)} births {d.get('births', 0)} | gen {d.get('max_generation', 0)} | "
            f"spend ${micro_to_usd(sim.wallet.spend_total):.4f} | {time.perf_counter() - t0:6.1f}s")


def _summary(sim: Any, status: str, t0: float) -> str:
    counts = {k: int(sim.db.fetchone("SELECT COUNT(*) AS n FROM events WHERE kind=?", (k,))["n"]) for k in ("death", "birth", "degeneration")}
    return (f"run {sim.run_id}: status={status} ticks={sim.clock.tick} days={sim.clock.day} "
            f"population={sim.registry.count_alive()} deaths={counts['death']} births={counts['birth']} "
            f"degenerations={counts['degeneration']} spend_total=${micro_to_usd(sim.wallet.spend_total):.4f} "
            f"elapsed={time.perf_counter() - t0:.1f}s dir={sim.run_dir}")


async def _run_headless(cfg: VoidConfig, run_dir: Path, *, resume: bool) -> str:
    from void.sim.loop import Simulation

    sim = Simulation(cfg, run_dir, resume=resume)
    t0 = time.perf_counter()
    print(f"run {sim.run_id}: seed={cfg.run.seed} days={cfg.run.days} ticks_per_day={cfg.run.ticks_per_day} dir={run_dir}", flush=True)

    def on_tick(report: Any) -> None:
        if report.new_day and report.day > 1:
            print(_day_line(sim, report.day - 1, t0), flush=True)

    status = await sim.run(on_tick=on_tick)
    if sim.clock.day >= 1:
        print(_day_line(sim, sim.clock.day, t0), flush=True)
    print(_summary(sim, status, t0), flush=True)
    sim.db.close()
    gc.collect()  # release the sandbox's subprocess transports while the loop is still open
    return status


# --- subcommands --------------------------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    config = _resolve(args.config)
    out = Path(args.out)
    sweep = args.seeds is not None
    seeds: list[int | None] = list(_parse_seeds(args.seeds)) if sweep else [args.seed]
    statuses: list[str] = []
    for seed in seeds:
        cfg = load_config(config, _run_overrides(seed=seed, days=args.days, tick_seconds=args.tick_seconds))
        for w in cfg.warnings():
            print(f"warning: {w}", file=sys.stderr)
        if cfg.intelligence.ladder:
            print(f"intelligence: {cfg.intelligence_summary()}", file=sys.stderr)
        run_dir = _run_dir(out, cfg, sweep=sweep)
        try:
            statuses.append(asyncio.run(_run_headless(cfg, run_dir, resume=args.resume)))
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            statuses.append("error")
    return 0 if statuses and all(s == "completed" for s in statuses) else 2


def cmd_serve(args: argparse.Namespace) -> int:
    from void.server.app import serve

    cfg = load_config(_resolve(args.config), _run_overrides(seed=args.seed, tick_seconds=args.tick_seconds))
    for w in cfg.warnings():
        print(f"warning: {w}", file=sys.stderr)
    if cfg.intelligence.ladder:
        print(f"intelligence: {cfg.intelligence_summary()}", file=sys.stderr)
    host = args.host or cfg.server.host
    port = int(args.port or cfg.server.port)
    static = _resolve(args.static)
    if not (static / "index.html").is_file():
        print(f"note: no built frontend at {static} (run `npm run build` in web/); serving the API only", file=sys.stderr)
    run_dir = _run_dir(Path(args.out), cfg, sweep=False)
    serve(cfg, run_dir, host=host, port=port, resume=args.resume, static_dir=static, paused=args.paused)
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    from void.server.protocol import export_schemas

    for p in export_schemas(Path(args.out)):
        print(p)
    return 0


async def record_feed(cfg: VoidConfig, run_dir: Path, ticks: int, *, seed: int | None = None,
                      api_dump: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a real simulation for ``ticks`` ticks through the hub and return the messages a socket would get.

    When ``api_dump`` is given it is filled with the /api responses (agents, tree, graveyard, gadgets, tasks)
    so the frontend's fixture mode can answer detail requests offline.
    """
    from void.server.api import build_agent_detail, build_graveyard, build_tree
    from void.server.protocol import parse_message
    from void.server.ws import Hub, Subscriber, SyntheticClock
    from void.sim.loop import Simulation

    sim = Simulation(cfg, run_dir)
    clock = SyntheticClock(cfg.run.seed if seed is None else seed, gap_tick=max(1, ticks // 2))
    hub = Hub(sim, clock=clock)
    sub = Subscriber(queue_size=1 << 20)  # the recorder never overflows
    hub.start()
    hub.subscribe(sub)
    await sim.setup()
    out: list[dict[str, Any]] = list(hub.preamble())
    try:
        for _ in range(int(ticks)):
            if sim.status != "running":
                break
            await sim.tick()
            await hub.flush()
            out.extend(sub.drain())
        if sim.status != "running":
            await sim.finish()
            await hub.on_run_end()
        else:
            sim.metrics.close()
        await hub.flush()
        out.extend(sub.drain())
        if api_dump is not None:
            api_dump.clear()
            api_dump.update({
                "run_id": sim.run_id,
                "agents": {a.agent_id: build_agent_detail(sim, a.agent_id) for a in sim.registry.all()},
                "tree": build_tree(sim), "graveyard": build_graveyard(sim),
                "gadgets": hub.gadgets_message(), "tasks": {"type": "tasks", "rev": sim.taskboard.rev, "items": sim.taskboard.snapshot()},
            })
    finally:
        await hub.stop()
        sim.db.close()
        gc.collect()  # release the sandbox's subprocess transports while the loop is still open
    for m in out:
        parse_message(m)  # the fixture is schema-valid by construction
    return out


def cmd_mock_feed(args: argparse.Namespace) -> int:
    import tempfile

    ticks = int(args.ticks)
    base = load_config(_resolve(args.config))
    days = max(base.run.days, math.ceil(ticks / base.run.ticks_per_day))
    cfg = load_config(_resolve(args.config), _run_overrides(seed=args.seed, days=days, tick_seconds=0.0, name="mock_feed"))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    api_dump: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="void-mock-feed-") as tmp:
        messages = asyncio.run(record_feed(cfg, Path(tmp) / "run", ticks, api_dump=api_dump))
    with out.open("w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m, separators=(",", ":"), sort_keys=True) + "\n")
    api_path = out.with_name("api.json")
    api_path.write_text(json.dumps(api_dump, separators=(",", ":"), sort_keys=True, default=str), encoding="utf-8")
    print(f"wrote {api_path} ({len(api_dump.get('agents', {}))} agents)")
    kinds: dict[str, int] = {}
    for m in messages:
        kinds[m["type"]] = kinds.get(m["type"], 0) + 1
    print(f"wrote {out} ({len(messages)} messages: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())) + ")")
    return 0


def _print_tree(rows: list[dict[str, Any]]) -> None:
    children: dict[str | None, list[dict[str, Any]]] = {}
    for r in rows:
        children.setdefault(r["parent_id"], []).append(r)

    def walk(parent: str | None, depth: int) -> None:
        for r in children.get(parent, []):
            died = f" died@{r['died_tick']}" if r["died_tick"] is not None else ""
            print(f"{'  ' * depth}{r['name']} [{r['agent_id']}] {r['model_tier']} gen{r['generation']} {r['status']} "
                  f"born@{r['born_tick']}{died} ${micro_to_usd(int(r['balance'] or 0)):.4f}")
            walk(r["agent_id"], depth + 1)

    walk(None, 0)
    ids = {r["agent_id"] for r in rows}
    for r in rows:  # orphans whose parent is not in the table
        if r["parent_id"] is not None and r["parent_id"] not in ids:
            print(f"(orphan) {r['name']} [{r['agent_id']}] parent {r['parent_id']} missing")


def cmd_inspect(args: argparse.Namespace) -> int:
    from void.db import Database

    run_dir = Path(args.run_dir)
    db_path = run_dir / "world.db"
    if not db_path.is_file():
        print(f"error: {db_path} not found", file=sys.stderr)
        return 1
    db = Database(db_path)
    run = db.fetchone("SELECT * FROM run")
    if run is None:
        print("error: empty run table", file=sys.stderr)
        return 1
    tick, day = db.kv_get("tick", 0), db.kv_get("day", 0)
    alive = db.fetchone("SELECT COUNT(*) AS n FROM agents WHERE status='alive'")["n"]
    archived = db.fetchone("SELECT COUNT(*) AS n FROM agents WHERE status='archived'")["n"]
    n_events = db.fetchone("SELECT COUNT(*) AS n, COALESCE(MAX(seq),0) AS s FROM events")
    n_snaps = db.fetchone("SELECT COUNT(*) AS n FROM snapshots")["n"]
    print(f"run {run['run_id']}: status={run['status']} seed={run['seed']} created={run['created_at']} "
          f"ended_tick={run['ended_tick']} reason={run['ended_reason']} experiment={run['experiment']} arm={run['arm']}")
    print(f"tick={tick} day={day} alive={alive} archived={archived} events={n_events['n']} (last seq {n_events['s']}) snapshots={n_snaps}")
    if args.tree:
        print("\n== lineage ==")
        _print_tree([dict(r) for r in db.fetchall("SELECT * FROM agents ORDER BY born_tick, agent_id")])
    if args.money:
        print("\n== money ==")
        for k in ("spawn_pool", "house", "chronicle", "spend_today", "spend_total"):
            print(f"{k:>12}: ${micro_to_usd(int(db.kv_get(k, 0) or 0)):.4f}")
        total = int(db.fetchone("SELECT COALESCE(SUM(balance),0) AS s FROM agents")["s"])
        print(f"{'agents':>12}: ${micro_to_usd(total):.4f}")
        for r in db.fetchall("SELECT agent_id, name, status, balance FROM agents ORDER BY balance DESC, agent_id"):
            print(f"  {r['name']:<12} {r['agent_id']} {r['status']:<9} ${micro_to_usd(int(r['balance'])):.4f}")
        print("ledger by kind:")
        for r in db.fetchall("SELECT kind, COUNT(*) AS n, SUM(delta) AS d FROM wallet_ledger GROUP BY kind ORDER BY kind"):
            print(f"  {r['kind']:<16} n={int(r['n']):>6} net=${micro_to_usd(int(r['d'] or 0)):.4f}")
    if args.events:
        print(f"\n== last {args.events} events ==")
        rows = db.fetchall("SELECT seq, tick, day, kind, agent_id, visibility, payload FROM events ORDER BY seq DESC LIMIT ?", (int(args.events),))
        for r in reversed(rows):
            print(f"#{r['seq']} t{r['tick']} d{r['day']} {r['kind']} {r['agent_id'] or '-'} [{r['visibility']}] {r['payload']}")
    if args.reindex:
        from void.ids import IdFactory
        from void.memory.store import MemoryStore

        cfg = load_config(run_dir / "config.resolved.yaml")
        store = MemoryStore(db, cfg.memory, IdFactory(str(run["run_id"]), start=int(db.kv_get("id_counter", 0))), run_dir / "vaults")
        total = 0
        for r in db.fetchall("SELECT agent_id, status FROM agents ORDER BY agent_id"):
            archived_agent = r["status"] == "archived"
            n = store.reindex(r["agent_id"], root=(run_dir / "graveyard") if archived_agent else None, archived=archived_agent)
            total += n
            print(f"reindexed {r['agent_id']}: {n} notes")
        print(f"reindexed {total} notes")
    db.close()
    return 0


# --- parser -------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="void", description="The Void: a multi-agent LLM ecology")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="headless run (one seed or a --seeds sweep)")
    r.add_argument("--config", required=True)
    r.add_argument("--seed", type=int)
    r.add_argument("--seeds", help="A-B (inclusive) or A,B,C: run each seed into <out>/<experiment|name>/<arm|default>/seed_<n>/")
    r.add_argument("--days", type=int)
    r.add_argument("--out", default=DEFAULT_OUT)
    r.add_argument("--tick-seconds", type=float, dest="tick_seconds")
    r.add_argument("--resume", action="store_true")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("serve", help="run behind the HTTP/WebSocket server")
    s.add_argument("--config", required=True)
    s.add_argument("--seed", type=int)
    s.add_argument("--out", default=DEFAULT_OUT)
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--resume", action="store_true")
    s.add_argument("--tick-seconds", type=float, dest="tick_seconds")
    s.add_argument("--static", default=DEFAULT_STATIC, help="built frontend directory (default web/dist)")
    s.add_argument("--paused", action="store_true", help="start paused (step with POST /api/control/step)")
    s.set_defaults(func=cmd_serve)

    sc = sub.add_parser("schema", help="export the wire JSON Schemas")
    sc.add_argument("--out", default="schemas")
    sc.set_defaults(func=cmd_schema)

    m = sub.add_parser("mock-feed", help="record a real scripted run as the JSONL message stream a socket would receive")
    m.add_argument("--config", default=DEFAULT_MOCK_CONFIG)
    m.add_argument("--ticks", type=int, default=240)
    m.add_argument("--seed", type=int)
    m.add_argument("--out", default=DEFAULT_MOCK_OUT)
    m.set_defaults(func=cmd_mock_feed)

    i = sub.add_parser("inspect", help="look inside a run directory")
    i.add_argument("--run-dir", required=True, dest="run_dir")
    i.add_argument("--tree", action="store_true")
    i.add_argument("--money", action="store_true")
    i.add_argument("--events", type=int, metavar="N")
    i.add_argument("--reindex", action="store_true")
    i.set_defaults(func=cmd_inspect)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
