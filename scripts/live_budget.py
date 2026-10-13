#!/usr/bin/env python
"""The live budget guard and run checks behind scripts/live_claude.sh. Reads run directories only; never calls a model.

  live_budget.py spent  --root DIR [--root DIR ...] [--plain]     real Anthropic spend already recorded (USD)
  live_budget.py check  --root DIR --budget USD (--config YAML | --cap USD)
                                                                  exit 0 when spent + the run's cap <= budget, else 3
  live_budget.py cap    --config YAML                              the run's economy.total_cap_usd (USD)
  live_budget.py rundir --config YAML --out DIR [--seed N]        the run directory `void run` would write
  live_budget.py status RUN_DIR                                   the run's status, or `absent`
  live_budget.py verify RUN_DIR [--strict] [--max-error-rate F] [--expect-purposes a,b]
                                                                  exit 0 when the run is usable, else 4
  live_budget.py report --root DIR                                one line per run

Spend is read from each run's own records (void.economy.budget): the real cost of every call on an Anthropic tier,
or the wallet's spend total when that is larger, plus any hold still reserved when a process died. Scripted runs
cost nothing real and are never counted.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from void.config import load_config, micro_to_usd  # noqa: E402
from void.economy.budget import DB_FILE, run_spend, spend_under  # noqa: E402

EXIT_REFUSED = 3
EXIT_UNUSABLE = 4


def _overrides(seed: int | None) -> dict[str, Any]:
    return {"run": {"seed": int(seed)}} if seed is not None else {}


def cmd_spent(args: argparse.Namespace) -> int:
    total, rows = spend_under(args.root)
    if args.plain:
        print(f"{micro_to_usd(total):.6f}")
        return 0
    for r in rows:
        if r.real_micro:
            print(f"  {r.run_dir}: ${micro_to_usd(r.real_micro):.4f} ({r.calls} calls, status {r.status})")
    print(f"spent ${micro_to_usd(total):.4f} across {len(rows)} run(s) under {', '.join(args.root)}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    if args.cap is None and args.config is None:
        print("check: pass --config or --cap", file=sys.stderr)
        return 2
    cap = float(args.cap) if args.cap is not None else load_config(args.config, _overrides(args.seed)).economy.total_cap_usd
    total, _ = spend_under(args.root)
    spent = micro_to_usd(total)
    if spent + cap > float(args.budget) + 1e-9:
        print(f"refused: ${spent:.4f} already spent + this run's cap ${cap:.2f} > budget ${float(args.budget):.2f}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"ok: ${spent:.4f} spent + cap ${cap:.2f} <= budget ${float(args.budget):.2f}")
    return 0


def cmd_cap(args: argparse.Namespace) -> int:
    print(f"{load_config(args.config).economy.total_cap_usd:.2f}")
    return 0


def cmd_rundir(args: argparse.Namespace) -> int:
    from void.cli import _run_dir

    cfg = load_config(args.config, _overrides(args.seed))
    print(_run_dir(Path(args.out), cfg, sweep=args.seed is not None))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    if not (run_dir / DB_FILE).is_file():
        print("absent")
    else:
        print(run_spend(run_dir).status or "unknown")
    return 0


def _rows(run_dir: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{run_dir / DB_FILE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM llm_calls WHERE provider='anthropic' ORDER BY tick, call_id").fetchall()
    finally:
        conn.close()


def _events(run_dir: Path, kinds: tuple[str, ...]) -> int:
    conn = sqlite3.connect(f"file:{run_dir / DB_FILE}?mode=ro", uri=True)
    try:
        marks = ",".join("?" for _ in kinds)
        return int(conn.execute(f"SELECT COUNT(*) FROM events WHERE kind IN ({marks})", kinds).fetchone()[0])
    finally:
        conn.close()


def verify_run(run_dir: Path, *, strict: bool = False, max_error_rate: float = 0.05,
               expect_purposes: tuple[str, ...] = ()) -> list[str]:
    """Problems that make a finished live run unusable (empty when it is fine); prints a per-purpose summary."""
    from void.brain.base import Usage
    from void.economy.catalog import same_model, thinks_by_default
    from void.economy.pricing import real_cost_micro

    problems: list[str] = []
    if not (run_dir / DB_FILE).is_file():
        return [f"{run_dir}: no {DB_FILE}"]
    spend = run_spend(run_dir)
    if spend.status != "completed":
        problems.append(f"status is {spend.status!r}, not 'completed'")
    cfg = load_config(run_dir / "config.resolved.yaml")
    rows = _rows(run_dir)
    if not rows:
        problems.append("no Anthropic calls were made")
        return problems
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault((str(r["purpose"]), str(r["tier"])), []).append(r)
    print(f"{run_dir}: {len(rows)} calls, ${micro_to_usd(spend.real_micro):.4f}, status {spend.status}")
    for (purpose, tier), rs in sorted(groups.items()):
        n = len(rs)
        print(f"  {purpose:<9} {tier:<9} calls {n:<4} errors {sum(r['error'] is not None for r in rs):<3} "
              f"in {sum(r['input_tokens'] for r in rs) / n:7.0f} out {sum(r['output_tokens'] for r in rs) / n:6.0f} "
              f"cache_read {sum(r['cache_read_tokens'] > 0 for r in rs):<4} ${micro_to_usd(sum(r['real_cost'] for r in rs)):.4f}")
    errors = [r for r in rows if r["error"] is not None]
    rate = len(errors) / len(rows)
    for purpose in expect_purposes:
        if not any(p == purpose for p, _ in groups):
            problems.append(f"no {purpose!r} call was made")
    if strict:
        for r in errors[:5]:
            problems.append(f"call {r['call_id']} ({r['purpose']}, {r['tier']}) failed: {str(r['error'])[:160]}")
        if len(errors) > 5:
            problems.append(f"... {len(errors) - 5} more failed calls")
        bad_stop = [r for r in rows if r["stop_reason"] != "end_turn"]
        if bad_stop:
            problems.append(f"{len(bad_stop)} call(s) did not end with end_turn: "
                            + ", ".join(sorted({str(r['stop_reason']) for r in bad_stop})))
        if any(int(r["estimated"]) for r in rows):
            problems.append("some calls were charged an estimate (usage unknown: timeout or dropped connection)")
        for r in rows:
            tier = cfg.tiers.get(str(r["tier"]))
            if tier is None:
                problems.append(f"call {r['call_id']} names unknown tier {r['tier']!r}")
                continue
            if not same_model(tier.model, str(r["model"])):
                problems.append(f"call {r['call_id']} was served by {r['model']} (a fallback?), not the tier's model")
                continue
            usage = Usage(input_tokens=int(r["input_tokens"]), output_tokens=int(r["output_tokens"]),
                          cache_read_tokens=int(r["cache_read_tokens"]), cache_write_tokens=int(r["cache_write_tokens"]))
            expected = real_cost_micro(usage, tier)
            if expected != int(r["real_cost"]):
                problems.append(f"call {r['call_id']}: metered {r['real_cost']} micro-$ but tokens x list prices = {expected}")
        for (purpose, tier_name), rs in groups.items():
            tier = cfg.tiers.get(tier_name)
            if purpose == "decide" and len(rs) >= 2 and tier is not None and thinks_by_default(tier.model) \
                    and not any(r["cache_read_tokens"] > 0 for r in rs[1:]):
                problems.append(f"tier {tier_name!r}: no decide call after the first read the prompt cache")
        if _events(run_dir, ("brain_error", "brain_refusal")):
            problems.append("brain_error or brain_refusal events were emitted")
    elif rate > max_error_rate:
        problems.append(f"{len(errors)} of {len(rows)} calls failed ({rate:.1%} > {max_error_rate:.0%}): "
                        + ", ".join(sorted({str(r['error']).split(':')[0] for r in errors})))
    return problems


def cmd_verify(args: argparse.Namespace) -> int:
    purposes = tuple(p for p in (args.expect_purposes or "").split(",") if p)
    problems = verify_run(Path(args.run_dir), strict=args.strict, max_error_rate=args.max_error_rate, expect_purposes=purposes)
    for p in problems:
        print(f"  PROBLEM: {p}", file=sys.stderr)
    if problems:
        print(f"verify: {args.run_dir} is not usable ({len(problems)} problem(s))", file=sys.stderr)
        return EXIT_UNUSABLE
    print(f"verify: {args.run_dir} ok")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    total, rows = spend_under(args.root)
    for r in rows:
        print(f"{str(r.run_dir):<70} {str(r.status):<13} calls {r.calls:<5} errors {r.errors:<4} ${micro_to_usd(r.real_micro):.4f}")
    print(f"total ${micro_to_usd(total):.4f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("spent")
    p.add_argument("--root", action="append", required=True)
    p.add_argument("--plain", action="store_true")
    p.set_defaults(func=cmd_spent)
    p = sub.add_parser("check")
    p.add_argument("--root", action="append", required=True)
    p.add_argument("--budget", type=float, required=True)
    p.add_argument("--config")
    p.add_argument("--seed", type=int)
    p.add_argument("--cap", type=float)
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("cap")
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_cap)
    p = sub.add_parser("rundir")
    p.add_argument("--config", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int)
    p.set_defaults(func=cmd_rundir)
    p = sub.add_parser("status")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("verify")
    p.add_argument("run_dir")
    p.add_argument("--strict", action="store_true", help="no failed call, every stop end_turn, no fallback, exact metering, cache reads")
    p.add_argument("--max-error-rate", type=float, default=0.05, dest="max_error_rate")
    p.add_argument("--expect-purposes", dest="expect_purposes", help="comma-separated call purposes that must appear")
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("report")
    p.add_argument("--root", action="append", required=True)
    p.set_defaults(func=cmd_report)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
