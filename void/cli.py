"""Command-line entry point (DESIGN §2): ``void run | serve | compare | analyze | export | inspect | schema | mock-feed``,
plus the lazy subcommands ``report | transplant | generations | mcp``.

* ``run``         headless simulation (one seed or a ``--seeds A-B`` sweep); exit 0 when completed, 2 otherwise.
                  ``--replay-commands RUN_DIR`` applies the operator commands RUN_DIR's run applied (its
                  ``control_commands`` rows) at the ticks it applied them, through the same handler; pause, resume
                  and speed are skipped headless (``void.sim.control.CommandReplay``).
                  A config that puts agents on a paid Anthropic tier needs a live budget (``--budget-usd`` or
                  ``VOID_BUDGET_USD``): each run is refused when the real spend already recorded (the repository's
                  ``data/live``, any ``--spend-root``, ``--out`` and the committed spend ledger) plus the run's
                  ``economy.total_cap_usd`` would exceed it (``void.economy.budget``)
* ``serve``       the simulation behind the HTTP/WebSocket server (DESIGN §15)
* ``compare``     paired comparison of an experiment's arms (``void.research.compare``, DESIGN §17): ``EXP_DIR`` or
                  ``--exp NAME`` (= ``<out>/<NAME>``); an unfair comparison prints ``refused: <reason>`` and exits 2
* ``analyze``     one run's summary (``void.research.analysis``), as ``scripts/analyze.py``; ``--replay`` re-runs the
                  whole run, so it is refused (exit 2) when the run's config could call any provider but ``scripted``
                  (that spend would land in a temporary directory, outside the live budget)
* ``export``      one file per table of a run (``void.research.export``, MEMORY_EVOLUTION §7.4);
                  ``void export parquet RUN_DIR`` is accepted for ``void export RUN_DIR --format parquet``
* ``schema``      export the wire schemas for the frontend
* ``mock-feed``   record a genuine scripted run as the JSONL message stream a socket would receive (default
                  ``configs/demo_memory.yaml``, the world of the committed fixture); refuses a config that could call
                  a paid provider: its calls would land in a deleted temporary directory
* ``inspect``     look inside a run directory (lineage, money, events, reindex the notes index)

Run directories. ``run`` and ``serve`` write ``<out>/<run.name>`` (a sweep: ``<out>/<experiment>/<arm>/seed_<n>``);
``--out`` defaults to the config's ``run.data_dir``. A directory that already holds a run is never started over
silently: ``--resume`` continues it, ``--fresh`` first renames it to ``<dir>.prev-<n>`` (the first free ``n``), and
otherwise the command prints one line saying so and exits 2.

Lazy subcommands. ``report``, ``transplant`` and ``generations`` (modules ``void.research.<name>``) and ``mcp``
(``void.server.mcp``) are imported only when invoked. A module joins the CLI by defining::

    def cli_main(argv: list[str]) -> int

``argv`` is everything after the subcommand name, verbatim (``--help`` included: the module owns its parser
and its help), and the return value is the exit status. While the module does not exist or has no
``cli_main``, the subcommand prints ``void <name>: not built yet`` to stderr and exits 2.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import importlib
import inspect
import json
import math
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from void.config import VoidConfig, load_config, micro_to_usd

__all__ = ["main", "build_parser", "record_feed"]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = "data/runs"       # the default of run.data_dir; where compare --exp looks
DEFAULT_STATIC = "web/dist"
DEFAULT_MOCK_CONFIG = "configs/demo_memory.yaml"   # the world of the committed fixture (RUNBOOK)
DEFAULT_MOCK_OUT = "web/public/fixtures/mock.jsonl"
DEFAULT_SPEND_ROOT = REPO_ROOT / "data" / "live"  # where live runs are written; always counted, whatever the cwd
BUDGET_ENV = "VOID_BUDGET_USD"
EXPORT_FORMATS = ("auto", "csv", "parquet")
EXPORT_USAGE = "void export [parquet] RUN_DIR [--out DIR] [--format auto|csv|parquet]"
EXPORT_EXTRA_HINT = "install the export extra (it provides pyarrow), or pass --format csv"
# subcommands whose module is imported only when invoked: name -> (module, help); see the module docstring
LAZY_COMMANDS: dict[str, tuple[str, str]] = {
    "report": ("void.research.report", "write the report of a run or an experiment"),
    "transplant": ("void.research.transplant", "the practice transplant harness (E5)"),
    "generations": ("void.research.generations", "the generational protocol: runs chained generation by generation"),
    "mcp": ("void.server.mcp", "a Model Context Protocol server over a run"),
}
LEDGER_ENV = "VOID_LEDGER"            # the spend ledger to count (default void.economy.budget.LEDGER_FILE)
ATTEMPT_ENV = "VOID_LEDGER_ATTEMPT"   # set by scripts/live_claude.sh: this start's own reservation in the ledger


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


def _out_dir(args: argparse.Namespace, cfg: VoidConfig) -> Path:
    """``--out`` when given, else the config's ``run.data_dir``."""
    return Path(args.out) if getattr(args, "out", None) else Path(cfg.run.data_dir)


def _holds_run(run_dir: Path) -> bool:
    """True when ``run_dir/world.db`` already records a run (only read; an unreadable file is left to the simulation).

    A plain connection, as ``void.research.analysis.connect``: closing it removes the WAL side files again, which a
    read-only one could not.
    """
    db_path = run_dir / "world.db"
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return False
    try:
        return conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] > 0
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _existing_run_line(run_dir: Path) -> str:
    return (f"run dir {run_dir} already holds a run: pass --resume to continue it, --fresh to archive it and start over, "
            f"or --out DIR")


def _is_existing_run_error(e: BaseException) -> bool:
    """The refusal :class:`void.sim.loop.Simulation` raises when a run directory already holds this run."""
    return isinstance(e, RuntimeError) and "already exists" in str(e)


def _archive_run_dir(run_dir: Path) -> Path | None:
    """``--fresh``: rename an existing ``run_dir`` to ``<run_dir>.prev-<n>`` (first free n); returns the new path."""
    if not run_dir.exists():
        return None
    n = 1
    while (dest := run_dir.with_name(f"{run_dir.name}.prev-{n}")).exists():
        n += 1
    run_dir.rename(dest)
    print(f"archived {run_dir} -> {dest}", file=sys.stderr)
    return dest


def _day_line(sim: Any, day: int, t0: float) -> str:
    row = sim.db.fetchone("SELECT json FROM metrics WHERE tick=?", (-int(day),))
    d = json.loads(row["json"]) if row else {}
    return (f"day {day:>3} | tick {sim.clock.tick:>5} | pop {sim.registry.count_alive():>2} | "
            f"deaths {d.get('deaths', 0)} births {d.get('births', 0)} | gen {d.get('max_generation', 0)} | "
            f"spend ${micro_to_usd(sim.wallet.spend_total):.4f} | {time.perf_counter() - t0:6.1f}s")


def _live_providers(cfg: VoidConfig) -> set[str]:
    from void.economy.budget import BUDGETED_PROVIDERS, reachable_providers

    return reachable_providers(cfg) & set(BUDGETED_PROVIDERS)


def _ledger_path() -> Path:
    import os

    from void.economy.budget import LEDGER_FILE

    env = (os.environ.get(LEDGER_ENV) or "").strip()
    return Path(env) if env else LEDGER_FILE


def _budget_usd(args: argparse.Namespace) -> float | None:
    import os

    raw = getattr(args, "budget_usd", None)
    if raw is None:
        env = (os.environ.get(BUDGET_ENV) or "").strip()
        raw = float(env) if env else None
    return None if raw is None else float(raw)


def _budget_refusal(cfg: VoidConfig, run_dir: Path, args: argparse.Namespace, out: Path, *, fresh: bool = False) -> str | None:
    """Why this run may not start under the live budget, or None when it may (or needs no budget).

    ``fresh``: the existing ``run_dir`` is about to be archived, so this is a new run that may spend its whole cap.
    """
    import os

    from void.economy.budget import run_spend, spend_under

    if not _live_providers(cfg):
        return None
    budget = _budget_usd(args)
    if budget is None:
        return (f"this config puts agents on a paid tier ({', '.join(sorted(_live_providers(cfg)))}); pass --budget-usd "
                f"(or set {BUDGET_ENV}) so the run is counted against the live budget")
    ledger = _ledger_path()
    if not ledger.is_file():
        return (f"no spend ledger at {ledger}: it is the record of real spend that survives a new machine (set {LEDGER_ENV} "
                f"to its path); without it the spend already made cannot be counted")
    roots = [DEFAULT_SPEND_ROOT, *[Path(r) for r in (getattr(args, "spend_root", None) or [])], out]
    try:
        spent_micro, _ = spend_under(roots, ledger=ledger, exclude_attempt=(os.environ.get(ATTEMPT_ENV) or "").strip() or None)
    except ValueError as e:
        return f"the spend ledger cannot be read: {e}"
    need = cfg.economy.total_cap_usd
    if not fresh and (run_dir / "world.db").is_file():  # resuming: only the rest of this run's cap can still be spent
        mine = run_spend(run_dir)
        need = 0.0 if mine.status not in (None, "running") else max(0.0, need - micro_to_usd(mine.real_micro))
    spent = micro_to_usd(spent_micro)
    if spent + need > budget + 1e-9:
        return (f"live budget: ${spent:.4f} already spent under {', '.join(str(r) for r in roots)} and in {ledger} + this "
                f"run's cap ${need:.2f} > budget ${budget:.2f}")
    print(f"live budget: ${spent:.4f} spent + cap ${need:.2f} <= ${budget:.2f}", file=sys.stderr)
    return None


def _summary(sim: Any, status: str, t0: float) -> str:
    counts = {k: int(sim.db.fetchone("SELECT COUNT(*) AS n FROM events WHERE kind=?", (k,))["n"]) for k in ("death", "birth", "degeneration")}
    return (f"run {sim.run_id}: status={status} ticks={sim.clock.tick} days={sim.clock.day} "
            f"population={sim.registry.count_alive()} deaths={counts['death']} births={counts['birth']} "
            f"degenerations={counts['degeneration']} spend_total=${micro_to_usd(sim.wallet.spend_total):.4f} "
            f"elapsed={time.perf_counter() - t0:.1f}s dir={sim.run_dir}")


async def _run_headless(cfg: VoidConfig, run_dir: Path, *, resume: bool, replay: Any = None) -> str:
    from void.sim.loop import Simulation

    sim = Simulation(cfg, run_dir, resume=resume, replay_commands=replay)
    t0 = time.perf_counter()
    print(f"run {sim.run_id}: seed={cfg.run.seed} days={cfg.run.days} ticks_per_day={cfg.run.ticks_per_day} dir={run_dir}", flush=True)

    def on_tick(report: Any) -> None:
        if report.new_day and report.day > 1:
            print(_day_line(sim, report.day - 1, t0), flush=True)

    status = await sim.run(on_tick=on_tick)
    if sim.clock.day >= 1:
        print(_day_line(sim, sim.clock.day, t0), flush=True)
    if sim.replay is not None and sim.replay.remaining():
        print(f"warning: {sim.replay.remaining()} replayed command(s) were due after the run ended", file=sys.stderr)
    print(_summary(sim, status, t0), flush=True)
    sim.db.close()
    gc.collect()  # release the sandbox's subprocess transports while the loop is still open
    return status


def _load_replay(source: str, run_dir: Path, cfg: VoidConfig) -> tuple[Any, str | None]:
    """``--replay-commands``: the source run's commands, or why they cannot be replayed into ``run_dir``."""
    from void.sim.control import CommandReplay

    src = Path(source)
    if not (src / "world.db").is_file():
        return None, f"--replay-commands: {src / 'world.db'} not found"
    try:
        if src.resolve() == run_dir.resolve():
            return None, f"--replay-commands names the run directory itself ({run_dir}); pass --out DIR for the replay"
    except OSError:
        pass
    try:
        replay = CommandReplay.load(src)
    except (OSError, ValueError, sqlite3.Error) as e:
        return None, f"--replay-commands: {e}"
    kinds = sorted({c.kind for c in replay.skipped})
    skipped = f" ({len(replay.skipped)} skipped headless: {', '.join(kinds)})" if kinds else ""
    print(f"replaying {len(replay.commands)} operator command(s) from {src}{skipped}", file=sys.stderr)
    src_hash, src_seed = replay.source.get("config_hash"), replay.source.get("seed")
    if src_hash is not None and (src_hash != cfg.hash() or (src_seed is not None and int(src_seed) != cfg.run.seed)):
        print(f"warning: {src} ran another config or seed (config_hash {str(src_hash)[:12]}, seed {src_seed}); the "
              f"commands are applied at the same ticks, but the world they meet differs", file=sys.stderr)
    return replay, None


# --- subcommands --------------------------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    config = _resolve(args.config)
    sweep = args.seeds is not None
    if sweep and args.replay_commands:
        print("error: --replay-commands replays one run; it cannot be combined with --seeds", file=sys.stderr)
        return 2
    seeds: list[int | None] = list(_parse_seeds(args.seeds)) if sweep else [args.seed]
    statuses: list[str] = []
    for seed in seeds:
        cfg = load_config(config, _run_overrides(seed=seed, days=args.days, tick_seconds=args.tick_seconds))
        out = _out_dir(args, cfg)
        run_dir = _run_dir(out, cfg, sweep=sweep)
        if not args.resume and not args.fresh and _holds_run(run_dir):
            print(_existing_run_line(run_dir), file=sys.stderr)
            statuses.append("exists")
            continue
        for w in cfg.warnings():
            print(f"warning: {w}", file=sys.stderr)
        if cfg.intelligence.ladder:
            print(f"intelligence: {cfg.intelligence_summary()}", file=sys.stderr)
        refusal = _budget_refusal(cfg, run_dir, args, out, fresh=args.fresh)
        if refusal is not None:
            print(f"refused: {refusal}", file=sys.stderr)
            statuses.append("refused")
            break  # later seeds would need at least as much
        replay = None
        if args.replay_commands:  # read before --fresh archives anything
            replay, why = _load_replay(args.replay_commands, run_dir, cfg)
            if why is not None:
                print(f"error: {why}", file=sys.stderr)
                statuses.append("error")
                continue
        if args.fresh:
            _archive_run_dir(run_dir)
        try:
            statuses.append(asyncio.run(_run_headless(cfg, run_dir, resume=args.resume, replay=replay)))
        except RuntimeError as e:
            print(_existing_run_line(run_dir) if _is_existing_run_error(e) else f"error: {e}", file=sys.stderr)
            statuses.append("error")
    return 0 if statuses and all(s == "completed" for s in statuses) else 2


def cmd_serve(args: argparse.Namespace) -> int:
    from void.server.app import serve

    cfg = load_config(_resolve(args.config), _run_overrides(seed=args.seed, tick_seconds=args.tick_seconds))
    out = _out_dir(args, cfg)
    run_dir = _run_dir(out, cfg, sweep=False)
    if not args.resume and not args.fresh and _holds_run(run_dir):
        print(_existing_run_line(run_dir), file=sys.stderr)
        return 2
    for w in cfg.warnings():
        print(f"warning: {w}", file=sys.stderr)
    if cfg.intelligence.ladder:
        print(f"intelligence: {cfg.intelligence_summary()}", file=sys.stderr)
    host = args.host or cfg.server.host
    port = int(args.port or cfg.server.port)
    static = _resolve(args.static)
    if not (static / "index.html").is_file():
        print(f"note: no built frontend at {static} (run `npm run build` in web/); serving the API only", file=sys.stderr)
    refusal = _budget_refusal(cfg, run_dir, args, out, fresh=args.fresh)
    if refusal is not None:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    if args.fresh:
        _archive_run_dir(run_dir)
    try:
        serve(cfg, run_dir, host=host, port=port, resume=args.resume, static_dir=static, paused=args.paused)
    except RuntimeError as e:
        if not _is_existing_run_error(e):
            raise
        print(_existing_run_line(run_dir), file=sys.stderr)
        return 2
    return 0


# --- research subcommands -----------------------------------------------------------------------------------
def cmd_compare(args: argparse.Namespace) -> int:
    from void.research.compare import IncomparableRuns, compare, render

    if (args.exp_dir is None) == (args.exp is None):
        print("error: give an experiment directory (EXP_DIR) or --exp NAME, not both", file=sys.stderr)
        return 2
    exp_dir = Path(args.out) / args.exp if args.exp is not None else Path(args.exp_dir)
    kwargs: dict[str, Any] = {}
    if args.allow_incomplete:
        if "allow_incomplete" in inspect.signature(compare).parameters:
            kwargs["allow_incomplete"] = True
        else:
            print("warning: --allow-incomplete is not supported by this void.research.compare; ignored", file=sys.stderr)
    try:
        result = compare(exp_dir, primary=args.primary, **kwargs)
    except IncomparableRuns as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, default=str) if args.json else render(result))
    return 0


def render_summary(summary: dict[str, Any], indent: str = "") -> str:
    """``key: value`` lines with nested dicts indented: the text form of ``void analyze``."""
    lines: list[str] = []
    for key, value in summary.items():
        if isinstance(value, dict):
            lines.append(f"{indent}{key}:")
            if value:
                lines.append(render_summary(value, indent + "  "))
        else:
            lines.append(f"{indent}{key}: {value}")
    return "\n".join(lines)


def _replay_providers(cfg: VoidConfig) -> set[str]:
    """Every provider other than ``scripted`` that re-running ``cfg`` could call.

    That is the tiers the agents start on and the tiers arrival epochs bring in (children never cross a provider
    boundary: ``void.agents.lifecycle.mutation_candidates``), plus every configured tier when the chronicle is written
    by a model (``void.brain.utility_calls.chronicle_tier`` falls back to the first provider tier configured at all).
    """
    tiers = {a.tier for a in cfg.agents if a.tier}
    tiers |= {e.arrival.tier for e in cfg.epochs if e.arrival is not None}
    if cfg.chronicle.writer == "llm":
        tiers |= set(cfg.tiers)
    return {cfg.tiers[t].provider for t in tiers} - {"scripted"}


def _replay_refusal(run_dir: Path) -> str | None:
    """Why ``void analyze --replay`` may not re-run ``run_dir``, or None when the replay makes no provider call.

    The replay rebuilds a :class:`void.sim.loop.Simulation` from the run's resolved config in a temporary directory,
    so a provider tier would make real calls whose spend is deleted with that directory and never counted against the
    live budget (``void.economy.budget``). Every provider other than ``scripted`` is refused, not only the budgeted
    ones, and so is a config that cannot be read (nothing could tell the replay is free).
    """
    from void.research.analysis import run_config

    try:
        cfg = run_config(run_dir)
    except Exception as e:  # any failure to read the config leaves the replay unchecked: refuse
        first = (str(e).splitlines() or [""])[0]
        return (f"void analyze --replay: cannot read the config of {run_dir} ({type(e).__name__}: {first}), so "
                f"cannot tell whether replaying would call a provider")
    paid = _replay_providers(cfg)
    if paid:
        return (f"void analyze --replay: {run_dir} uses paid tiers ({', '.join(sorted(paid))}); replaying would call "
                f"the provider outside the live budget")
    return None


def cmd_analyze(args: argparse.Namespace) -> int:
    from void.research.analysis import probe_recall, summarize

    run_dir = Path(args.run_dir)
    if not (run_dir / "world.db").is_file():
        print(f"error: {run_dir / 'world.db'} not found", file=sys.stderr)
        return 1
    if args.replay:  # checked before any work: the replay re-runs the whole simulation
        refusal = _replay_refusal(run_dir)
        if refusal is not None:
            print(f"refused: {refusal}", file=sys.stderr)
            return 2
    summary = summarize(run_dir, benefactor=args.benefactor)
    if args.replay:
        summary["probe_recall"] = probe_recall(run_dir, replay=True)
    print(json.dumps(summary, indent=2, sort_keys=True, default=str) if args.json else render_summary(summary))
    return 0


def _have_pyarrow() -> bool:
    import importlib.util

    return importlib.util.find_spec("pyarrow") is not None


def cmd_export(args: argparse.Namespace) -> int:
    # imported at call time: the export_run in place now is the one called (and inspected for fmt)
    from void.research.export import export_run

    target: list[str] = list(args.target)
    if len(target) == 2 and target[0] == "parquet":  # `void export parquet RUN_DIR`
        if args.fmt not in (None, "parquet"):
            print(f"error: `void export parquet` conflicts with --format {args.fmt}", file=sys.stderr)
            return 2
        fmt, run_dir = "parquet", Path(target[1])
    elif len(target) == 1:
        fmt, run_dir = args.fmt or "auto", Path(target[0])
    else:
        print(f"error: usage: {EXPORT_USAGE}", file=sys.stderr)
        return 2
    if not (run_dir / "world.db").is_file():
        print(f"error: {run_dir / 'world.db'} not found", file=sys.stderr)
        return 1
    kwargs: dict[str, Any] = {}
    if "fmt" in inspect.signature(export_run).parameters:
        kwargs["fmt"] = fmt
    elif fmt == "parquet" and not _have_pyarrow():
        print(f"void export: parquet needs pyarrow: {EXPORT_EXTRA_HINT}", file=sys.stderr)
        return 2
    elif fmt == "csv" and _have_pyarrow():
        print("warning: this void.research.export writes parquet whenever pyarrow is importable; --format csv ignored",
              file=sys.stderr)
    out = Path(args.out) if args.out else run_dir / "export"
    try:
        manifest = export_run(run_dir, out, **kwargs)
    except RuntimeError as e:
        if "pyarrow" not in str(e).lower():
            raise
        print(f"void export: {e}: {EXPORT_EXTRA_HINT}", file=sys.stderr)
        return 2
    tables = manifest.get("tables", {})
    for name, info in tables.items():
        print(f"  {name:<16} {int(info.get('rows', 0)):>7} rows  {info.get('path', '')}")
    print(f"exported {len(tables)} tables ({manifest.get('format')}) to {out}; manifest {out / 'manifest.json'}")
    return 0


def cmd_lazy(args: argparse.Namespace) -> int:
    """A lazy subcommand: ``importlib.import_module(module).cli_main(argv)`` (the contract is in the module docstring)."""
    name = args.cmd
    module = LAZY_COMMANDS[name][0]
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as e:
        missing = e.name
        if missing is None or missing == module or module.startswith(missing + "."):
            print(f"void {name}: not built yet", file=sys.stderr)
        else:  # the module exists but one of its own imports does not
            print(f"void {name}: {module} needs the missing module {missing!r}", file=sys.stderr)
        return 2
    cli_main = getattr(mod, "cli_main", None)
    if not callable(cli_main):
        print(f"void {name}: not built yet", file=sys.stderr)
        return 2
    rc = cli_main(list(args.argv))
    return 0 if rc is None else int(rc)


def cmd_schema(args: argparse.Namespace) -> int:
    from void.server.protocol import export_schemas

    for p in export_schemas(Path(args.out)):
        print(p)
    return 0


async def record_feed(cfg: VoidConfig, run_dir: Path, ticks: int, *, seed: int | None = None,
                      api_dump: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a real simulation for ``ticks`` ticks through the hub and return the messages a socket would get.

    When ``api_dump`` is given it is filled with the /api responses (agents, tree, graveyard, gadgets, tasks, and the
    memory reads: policy per agent, memory_summary, practices) so the frontend's fixture mode can answer detail
    requests offline. Vault paths in it are relative to ``run_dir``, so the dump does not depend on where it ran.
    """
    from void.server.api import (
        build_agent_detail,
        build_agent_policy,
        build_graveyard,
        build_memory_summary,
        build_practices,
        build_tree,
    )
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
            agents = {a.agent_id: build_agent_detail(sim, a.agent_id) for a in sim.registry.all()}
            root = Path(run_dir).resolve()
            for detail in agents.values():  # the vault path sits under a temporary run_dir: keep only its relative part
                rec = (detail or {}).get("agent") or {}
                path = rec.get("memory_path")
                if isinstance(path, str) and Path(path).resolve().is_relative_to(root):
                    rec["memory_path"] = Path(path).resolve().relative_to(root).as_posix()
            api_dump.clear()
            api_dump.update({
                "run_id": sim.run_id, "agents": agents,
                "tree": build_tree(sim), "graveyard": build_graveyard(sim),
                "gadgets": hub.gadgets_message(), "tasks": {"type": "tasks", "rev": sim.taskboard.rev, "items": sim.taskboard.snapshot()},
                "policy": {aid: build_agent_policy(sim, aid) for aid in agents},
                "memory_summary": build_memory_summary(sim), "practices": build_practices(sim),
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

    from void.economy.budget import reachable_providers

    ticks = int(args.ticks)
    base = load_config(_resolve(args.config))
    paid = reachable_providers(base)
    if paid:  # a real paid run whose records would be deleted with the temporary directory, outside any budget
        print(f"refused: mock-feed records a scripted fixture; {args.config} can call a paid provider "
              f"({', '.join(sorted(paid))})", file=sys.stderr)
        return 2
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
        for k in ("spawn_pool", "house", "chronicle", "research_pool", "spend_today", "spend_total"):
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
    if args.policy:
        from void.memory.policy import PolicyStore

        cfg = load_config(run_dir / "config.resolved.yaml")
        store = PolicyStore(db, cfg.memory.policy, run_dir / "vaults", ticks_per_day=cfg.run.ticks_per_day)
        row = db.fetchone("SELECT agent_id, name FROM agents WHERE agent_id=? OR name=? ORDER BY born_tick LIMIT 1", (args.policy, args.policy))
        if row is None:
            print(f"error: no agent {args.policy!r}", file=sys.stderr)
            return 1
        print(f"\n== memory policy of {row['name']} [{row['agent_id']}] ==")
        timeline = store.timeline(str(row["agent_id"]))
        if not timeline:
            print("(no policy versions: memory.policy.enabled was false)")
        for v in timeline:
            via = f" from {v['source_agent_id']}" if v.get("source_agent_id") else ""
            sim = v.get("similarity_to_parent")
            sim_s = "" if sim is None else f" sim={float(sim):.2f}"
            print(f"  v{v['version']:<3} tick {v['tick']:<5} {v['source']:<9}{via}{sim_s} {v['chars']} chars "
                  f"+{v['words_added']} -{v['words_removed']} words")
        text = store.text(str(row["agent_id"]))
        if text is not None:
            print("--- current text ---")
            print(text if text.strip() else "(empty)")
    if args.memory_commands:
        print(f"\n== last {args.memory_commands} memory commands ==")
        rows = db.fetchall("SELECT cmd_id, tick, agent_id, purpose, command, args, ok, message FROM memory_commands ORDER BY tick DESC, cmd_id DESC LIMIT ?",
                           (int(args.memory_commands),))
        for r in reversed(rows):
            mark = "ok " if r["ok"] else "REF"
            print(f"t{r['tick']} {r['agent_id']} {r['purpose']:<8} {mark} {r['command']:<11} {r['args'][:90]} :: {(r['message'] or '')[:80]}")
    if args.maintenance:
        print("\n== maintenance per day boundary ==")
        rows = db.fetchall("SELECT day, COUNT(*) AS n, SUM(json_extract(payload,'$.ok')) AS ok, SUM(json_extract(payload,'$.refused')) AS refused, "
                           "SUM(json_extract(payload,'$.world_cost')) AS cost FROM events WHERE kind='maintenance' GROUP BY day ORDER BY day")
        for r in rows:
            print(f"  day {int(r['day']):<3} agents {int(r['n']):<3} commands ok {int(r['ok'] or 0):<4} refused {int(r['refused'] or 0):<4} "
                  f"world cost ${micro_to_usd(int(r['cost'] or 0)):.4f}")
        skipped = db.fetchall("SELECT day, json_extract(payload,'$.reason') AS reason, COUNT(*) AS n FROM events WHERE kind='maintenance_skipped' "
                              "GROUP BY day, reason ORDER BY day")
        for r in skipped:
            print(f"  day {int(r['day']):<3} skipped {int(r['n'])} ({r['reason']})")
        if not rows and not skipped:
            print("(no maintenance events: memory.maintenance.enabled was false)")
    if args.exams:
        print("\n== audit exam ==")
        # a failed item (a paid call that returned no usable answer) is stored as abstained but is not graded
        rows = db.fetchall("SELECT i.agent_id, a.name, COUNT(*) AS asked, SUM(x.correct) AS correct, SUM(x.abstained) AS abstained, "
                           "SUM(x.call_id IS NOT NULL AND x.answer IS NULL) AS failed "
                           "FROM exam_items i JOIN exam_answers x ON x.item_id=i.item_id LEFT JOIN agents a ON a.agent_id=i.agent_id "
                           "GROUP BY i.agent_id ORDER BY a.name")
        for r in rows:
            failed = int(r["failed"] or 0)
            asked, correct, abst = int(r["asked"]) - failed, int(r["correct"] or 0), int(r["abstained"] or 0) - failed
            acc = f"{correct / asked:.2f}" if asked else "n/a"
            print(f"  {(r['name'] or '?'):<12} {r['agent_id']} asked {asked:<3} correct {correct:<3} abstained {abst:<3} "
                  f"failed {failed:<3} accuracy {acc}")
        kinds = db.fetchall("SELECT i.kind, COUNT(*) AS asked, SUM(x.correct) AS correct, SUM(x.abstained) AS abstained FROM exam_items i "
                            "JOIN exam_answers x ON x.item_id=i.item_id GROUP BY i.kind ORDER BY i.kind")
        for r in kinds:
            print(f"  kind {r['kind']:<10} asked {int(r['asked']):<4} correct {int(r['correct'] or 0):<4} abstained {int(r['abstained'] or 0):<4}")
        if not rows:
            print("(no exam rows: memory.exam.enabled was false or no day was old enough)")
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
def _budget_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--budget-usd", type=float, dest="budget_usd", default=None,
                   help=f"live budget in USD across all runs (default ${BUDGET_ENV}); required when agents use a paid Anthropic tier")
    p.add_argument("--spend-root", dest="spend_root", action="append", default=None,
                   help=f"another directory whose runs count against the budget (repeatable; {DEFAULT_SPEND_ROOT}, "
                        f"--out and the spend ledger always count)")


def _run_dir_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", default=None, metavar="DIR", help="parent of the run directory (default: the config's run.data_dir)")
    again = p.add_mutually_exclusive_group()
    again.add_argument("--resume", action="store_true", help="continue the run already in the run directory")
    again.add_argument("--fresh", action="store_true",
                       help="rename an existing run directory to <dir>.prev-<n> (first free n) and start over")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="void", description="The Void: a multi-agent LLM ecology")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="headless run (one seed or a --seeds sweep)")
    r.add_argument("--config", required=True)
    r.add_argument("--seed", type=int)
    r.add_argument("--seeds", help="A-B (inclusive) or A,B,C: run each seed into <out>/<experiment|name>/<arm|default>/seed_<n>/")
    r.add_argument("--days", type=int)
    r.add_argument("--tick-seconds", type=float, dest="tick_seconds")
    r.add_argument("--replay-commands", dest="replay_commands", metavar="RUN_DIR", default=None,
                   help="apply the operator commands RUN_DIR's run applied, each at the tick it was applied there "
                        "(pause, resume and speed are skipped headless); not with --seeds")
    _run_dir_args(r)
    _budget_args(r)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("serve", help="run behind the HTTP/WebSocket server")
    s.add_argument("--config", required=True)
    s.add_argument("--seed", type=int)
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--tick-seconds", type=float, dest="tick_seconds")
    s.add_argument("--static", default=DEFAULT_STATIC, help="built frontend directory (default web/dist)")
    s.add_argument("--paused", action="store_true", help="start paused (step with POST /api/control/step)")
    _run_dir_args(s)
    _budget_args(s)
    s.set_defaults(func=cmd_serve)

    c = sub.add_parser("compare", help="paired comparison of an experiment's arms; refuses unfair comparisons (exit 2)",
                       description="Compare the arms of an experiment directory holding <arm>/seed_<n>/ runs.")
    c.add_argument("exp_dir", nargs="?", metavar="EXP_DIR", help="the experiment directory")
    c.add_argument("--exp", metavar="NAME", help="compare <out>/<NAME> instead of EXP_DIR")
    c.add_argument("--out", default=DEFAULT_OUT, metavar="DIR", help=f"where --exp looks (default {DEFAULT_OUT})")
    c.add_argument("--primary", default=None, help="outcome path overriding experiment.primary_outcome")
    c.add_argument("--json", action="store_true", help="print JSON instead of text")
    c.add_argument("--allow-incomplete", action="store_true", dest="allow_incomplete",
                   help="compare even when some runs did not complete (when the comparison supports it)")
    c.set_defaults(func=cmd_compare)

    a = sub.add_parser("analyze", help="summarize one run (as scripts/analyze.py)")
    a.add_argument("run_dir", metavar="RUN_DIR")
    a.add_argument("--benefactor", action="store_true", help="include the benefactor (windfall) outcomes")
    a.add_argument("--json", action="store_true", help="print JSON instead of text")
    a.add_argument("--replay", action="store_true",
                   help="recompute probe_recall by replaying the run with probes (scripted runs only: a run whose "
                        "config could call a provider is refused, exit 2)")
    a.set_defaults(func=cmd_analyze)

    e = sub.add_parser("export", help="one file per table of a run (`void export parquet RUN_DIR` is an alias)",
                       usage=EXPORT_USAGE.replace("void export", "%(prog)s", 1))
    e.add_argument("target", nargs="+", metavar="RUN_DIR", help="the run directory (after `parquet` for the alias)")
    e.add_argument("--out", default=None, metavar="DIR", help="output directory (default <RUN_DIR>/export)")
    e.add_argument("--format", choices=EXPORT_FORMATS, default=None, dest="fmt",
                   help="auto (parquet when pyarrow is importable, else csv; the default), csv or parquet")
    e.set_defaults(func=cmd_export)

    sc = sub.add_parser("schema", help="export the wire JSON Schemas")
    sc.add_argument("--out", default="schemas")
    sc.set_defaults(func=cmd_schema)

    m = sub.add_parser("mock-feed", help="record a real scripted run as the JSONL message stream a socket would receive")
    m.add_argument("--config", default=DEFAULT_MOCK_CONFIG, help=f"(default {DEFAULT_MOCK_CONFIG}, the committed fixture's world)")
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
    i.add_argument("--policy", metavar="AGENT", help="memory policy timeline and current text of an agent (id or name)")
    i.add_argument("--memory-commands", type=int, metavar="N", dest="memory_commands", help="last N memory commands")
    i.add_argument("--maintenance", action="store_true", help="nightly maintenance summary per day")
    i.add_argument("--exams", action="store_true", help="audit exam results per agent and per question kind")
    i.set_defaults(func=cmd_inspect)

    for name, (module, text) in LAZY_COMMANDS.items():
        # everything after the name goes to the module verbatim, --help included: no option prefix is recognised here
        # (a REMAINDER positional alone would still reject a leading "--flag")
        lz = sub.add_parser(name, help=f"{text} [{module}]", add_help=False, prefix_chars="\0")
        lz.add_argument("argv", nargs=argparse.REMAINDER)
        lz.set_defaults(func=cmd_lazy)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
