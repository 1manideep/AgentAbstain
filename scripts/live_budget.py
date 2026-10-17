#!/usr/bin/env python
"""The live budget guard and run checks behind scripts/live_claude.sh. Reads run directories only; never calls a model.

  live_budget.py spent  --root DIR [--root DIR ...] [--ledger FILE] [--plain]
                                                                  real Anthropic spend already recorded (USD)
  live_budget.py check  --root DIR [--ledger FILE] --budget USD (--config YAML [--seed N] | --cap USD)
                                                                  exit 0 when spent + the run's cap <= budget, else 3
  live_budget.py cap    --config YAML                              the run's economy.total_cap_usd (USD)
  live_budget.py rundir --config YAML --out DIR [--seed N]        the run directory `void run` would write
  live_budget.py status RUN_DIR                                   the run's status, or `absent`
  live_budget.py verify RUN_DIR [--strict] [--max-error-rate F] [--expect-purposes a,b] [--no-cap-blocks]
                                                                  exit 0 when the run is usable, else 4
  live_budget.py report --root DIR [--ledger FILE]                one line per run
  live_budget.py reserve --ledger FILE --config YAML [--seed N] --out DIR
                                                                  append a `reserved` line (the run's full cap) and
                                                                  print its attempt id
  live_budget.py final  --ledger FILE --attempt ID --run-dir DIR [--verified]
                                                                  append the attempt's `final` line (its real spend)
  live_budget.py lost   --ledger FILE --config YAML [--seed N] --root DIR
                                                                  exit 5 when the ledger records spend for this run
                                                                  but no directory of it exists under the roots
  live_budget.py passed --ledger FILE --config YAML [--seed N]    exit 0 when the ledger records a completed,
                                                                  verified attempt of this run, else 1
  live_budget.py claims RUN_DIR [RUN_DIR ...] --min N             talks and listed claims per run; exit 6 when a
                                                                  run listed fewer than N claims

Spend is read from each run's own records (void.economy.budget): the real cost of every call on an Anthropic tier,
or the wallet's spend total when that is larger, plus any hold still reserved when a process died. Scripted runs
cost nothing real and are never counted. With ``--ledger`` the committed spend ledger is added: per run id the
larger of the ledger and the directories on disk, so a lost or moved run directory still counts.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from void.config import VoidConfig, load_config, micro_to_usd, usd_to_micro  # noqa: E402
from void.economy.budget import DB_FILE, find_runs, read_ledger, run_spend, spend_under  # noqa: E402

EXIT_REFUSED = 3
EXIT_UNUSABLE = 4
EXIT_LOST = 5
EXIT_FEW_CLAIMS = 6
BILLING_ERROR = "billing"  # AnthropicBrain's error class for a response billed above the list prices


def _overrides(seed: int | None) -> dict[str, Any]:
    return {"run": {"seed": int(seed)}} if seed is not None else {}


def _run_id(cfg: VoidConfig) -> str:
    return f"{cfg.run.name}-s{cfg.run.seed}"  # as void.sim.loop.Simulation names a run


def _spend(args: argparse.Namespace) -> tuple[int, list[Any]]:
    return spend_under(args.root, ledger=getattr(args, "ledger", None))


def cmd_spent(args: argparse.Namespace) -> int:
    total, rows = _spend(args)
    if args.plain:
        print(f"{micro_to_usd(total):.6f}")
        return 0
    for r in rows:
        if r.real_micro:
            print(f"  {r.run_dir}: ${micro_to_usd(r.real_micro):.4f} ({r.calls} calls, status {r.status})")
    where = ", ".join(args.root) + (f" and the ledger {args.ledger}" if args.ledger else "")
    print(f"spent ${micro_to_usd(total):.4f} across {len(rows)} run(s) under {where}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    if args.cap is None and args.config is None:
        print("check: pass --config or --cap", file=sys.stderr)
        return 2
    cap = float(args.cap) if args.cap is not None else load_config(args.config, _overrides(args.seed)).economy.total_cap_usd
    total, _ = _spend(args)
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


def _cap_blocks(run_dir: Path) -> int:
    """Calls a real-dollar cap blocked (the agent slept or idled instead), and total-cap hits."""
    conn = sqlite3.connect(f"file:{run_dir / DB_FILE}?mode=ro", uri=True)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='cap_hit' OR (kind='gate_blocked' AND "
            "json_extract(payload, '$.reason') IN ('daily_cap', 'total_cap'))").fetchone()[0])
    finally:
        conn.close()


def _error_class(error: Any) -> str:
    return str(error).split(":")[0]


def verify_run(run_dir: Path, *, strict: bool = False, max_error_rate: float = 0.05,
               expect_purposes: tuple[str, ...] = (), no_cap_blocks: bool = False) -> list[str]:
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
    if errors:  # always shown, so a run that passes with a few failures still says which kind they were
        classes: dict[str, int] = {}
        for r in errors:
            classes[_error_class(r["error"])] = classes.get(_error_class(r["error"]), 0) + 1
        print("  errors by class: " + ", ".join(f"{k} {v}" for k, v in sorted(classes.items())))
    billed_above_list = [r for r in errors if _error_class(r["error"]) == BILLING_ERROR]
    if billed_above_list:  # every mode: the meter priced these calls at list prices the provider did not charge
        problems.append(f"{len(billed_above_list)} call(s) were billed above the list prices the meter uses: "
                        + str(billed_above_list[0]["error"])[:200])
    for purpose in expect_purposes:
        if not any(p == purpose for p, _ in groups):
            problems.append(f"no {purpose!r} call was made")
    if no_cap_blocks:
        blocked = _cap_blocks(run_dir)
        if blocked:
            problems.append(f"a real-dollar cap blocked {blocked} call(s): the arms would no longer be treated alike")
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
                        + ", ".join(sorted({_error_class(r['error']) for r in errors})))
    return problems


def cmd_verify(args: argparse.Namespace) -> int:
    purposes = tuple(p for p in (args.expect_purposes or "").split(",") if p)
    problems = verify_run(Path(args.run_dir), strict=args.strict, max_error_rate=args.max_error_rate,
                          expect_purposes=purposes, no_cap_blocks=args.no_cap_blocks)
    for p in problems:
        print(f"  PROBLEM: {p}", file=sys.stderr)
    if problems:
        print(f"verify: {args.run_dir} is not usable ({len(problems)} problem(s))", file=sys.stderr)
        return EXIT_UNUSABLE
    print(f"verify: {args.run_dir} ok")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    total, rows = _spend(args)
    for r in rows:
        print(f"{str(r.run_dir):<70} {str(r.status):<13} calls {r.calls:<5} errors {r.errors:<4} ${micro_to_usd(r.real_micro):.4f}")
    if args.ledger:
        on_disk = {r.run_id for r in rows}
        for attempt, e in sorted(read_ledger(args.ledger).items()):
            if e["run_id"] not in on_disk:
                micro = int(e.get("real_micro") or 0) if e["state"] == "final" else int(e.get("cap_micro") or 0)
                print(f"{'(ledger only) ' + str(e['run_id']):<70} {str(e.get('status', e['state'])):<13} "
                      f"calls {int(e.get('calls') or 0):<5} attempt {attempt} ${micro_to_usd(micro):.4f}")
    print(f"total ${micro_to_usd(total):.4f}")
    return 0


# --- the committed spend ledger -----------------------------------------------------------------------------
def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append(ledger: Path, entry: dict[str, Any]) -> None:
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def cmd_reserve(args: argparse.Namespace) -> int:
    from void.cli import _run_dir

    ledger = Path(args.ledger)
    attempts = read_ledger(ledger)  # also refuses a missing or malformed ledger before anything is written
    cfg = load_config(args.config, _overrides(args.seed))
    run_id = _run_id(cfg)
    attempt = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{run_id}"
    n = 1
    while attempt in attempts:
        n += 1
        attempt = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{run_id}-{n}"
    run_dir = _run_dir(Path(args.out), cfg, sweep=args.seed is not None)
    try:
        rel = str(run_dir.resolve().relative_to(Path(args.out).resolve()))
    except ValueError:
        rel = str(run_dir)
    _append(ledger, {"attempt": attempt, "run_id": run_id, "run": rel, "config": Path(args.config).name, "state": "reserved",
                     "cap_micro": usd_to_micro(cfg.economy.total_cap_usd), "ts": _now()})
    print(attempt)
    return 0


def cmd_final(args: argparse.Namespace) -> int:
    ledger = Path(args.ledger)
    attempts = read_ledger(ledger)
    if args.attempt not in attempts:
        print(f"final: no attempt {args.attempt!r} in {ledger}", file=sys.stderr)
        return 2
    run_dir = Path(args.run_dir)
    entry: dict[str, Any] = {"attempt": args.attempt, "run_id": attempts[args.attempt]["run_id"], "state": "final", "ts": _now(),
                             "verified": bool(args.verified)}
    if not (run_dir / DB_FILE).is_file():  # refused or failed before the run wrote anything: nothing was spent
        entry.update(status="not_started", real_micro=0, calls=0, errors=0)
    else:
        spend = run_spend(run_dir)
        conn = sqlite3.connect(f"file:{run_dir / DB_FILE}?mode=ro", uri=True)
        try:
            ids = [r[0] for r in conn.execute("SELECT request_id FROM llm_calls WHERE provider='anthropic' AND request_id IS NOT NULL "
                                              "ORDER BY tick, call_id")]
            row = conn.execute("SELECT created_at FROM run LIMIT 1").fetchone()
        finally:
            conn.close()
        entry.update(status=spend.status, real_micro=spend.real_micro, calls=spend.calls, errors=spend.errors,
                     first_request_id=ids[0] if ids else None, last_request_id=ids[-1] if ids else None,
                     created_at=row[0] if row else None)
        if spend.run_id and spend.run_id != entry["run_id"]:
            print(f"final: {run_dir} holds run {spend.run_id}, not {entry['run_id']}", file=sys.stderr)
            return 2
    _append(ledger, entry)
    print(f"ledger: {args.attempt} final, status {entry['status']}, ${micro_to_usd(int(entry['real_micro'])):.4f}")
    return 0


def _paid_attempts(ledger: str, run_id: str) -> list[dict[str, Any]]:
    out = []
    for e in read_ledger(ledger).values():
        if e["run_id"] == run_id and (e["state"] == "reserved" or int(e.get("real_micro") or 0) > 0):
            out.append(e)
    return out


def cmd_lost(args: argparse.Namespace) -> int:
    run_id = _run_id(load_config(args.config, _overrides(args.seed)))
    paid = _paid_attempts(args.ledger, run_id)
    if not paid:
        return 0
    if any(run_spend(d).run_id == run_id for d in find_runs(args.root)):
        return 0
    print(f"lost: the ledger records {len(paid)} paid attempt(s) of {run_id} ({', '.join(str(e['attempt']) for e in paid)}) "
          f"but no directory of it exists under {', '.join(args.root)}", file=sys.stderr)
    return EXIT_LOST


def cmd_passed(args: argparse.Namespace) -> int:
    run_id = _run_id(load_config(args.config, _overrides(args.seed)))
    for e in read_ledger(args.ledger).values():
        if e["run_id"] == run_id and e["state"] == "final" and e.get("status") == "completed" and e.get("verified") is True:
            return 0
    return 1


def cmd_claims(args: argparse.Namespace) -> int:
    short = 0
    for d in args.run_dirs:
        conn = sqlite3.connect(f"file:{Path(d) / DB_FILE}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(action_type='talk'), 0), COALESCE(SUM(claims_made), 0), "
                               "COALESCE(SUM(claims_false), 0) FROM llm_calls WHERE purpose='decide'").fetchone()
        finally:
            conn.close()
        decisions, talks, made, false = (int(x) for x in row)
        ok = made >= args.min
        short += not ok
        print(f"  {d}: {decisions} decisions, {talks} talks, {made} claims listed ({false} false){'' if ok else '  TOO FEW'}")
    if short:
        print(f"claims: {short} run(s) listed fewer than {args.min} claims; false_claim_rate would rest on too few claims",
              file=sys.stderr)
        return EXIT_FEW_CLAIMS
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("spent")
    p.add_argument("--root", action="append", required=True)
    p.add_argument("--ledger")
    p.add_argument("--plain", action="store_true")
    p.set_defaults(func=cmd_spent)
    p = sub.add_parser("check")
    p.add_argument("--root", action="append", required=True)
    p.add_argument("--ledger")
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
    p.add_argument("--no-cap-blocks", action="store_true", dest="no_cap_blocks",
                   help="unusable when a daily or total real-dollar cap blocked any call (paired stages)")
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("report")
    p.add_argument("--root", action="append", required=True)
    p.add_argument("--ledger")
    p.set_defaults(func=cmd_report)
    p = sub.add_parser("reserve")
    p.add_argument("--ledger", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--seed", type=int)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_reserve)
    p = sub.add_parser("final")
    p.add_argument("--ledger", required=True)
    p.add_argument("--attempt", required=True)
    p.add_argument("--run-dir", required=True, dest="run_dir")
    p.add_argument("--verified", action="store_true")
    p.set_defaults(func=cmd_final)
    p = sub.add_parser("lost")
    p.add_argument("--ledger", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--seed", type=int)
    p.add_argument("--root", action="append", required=True)
    p.set_defaults(func=cmd_lost)
    p = sub.add_parser("passed")
    p.add_argument("--ledger", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--seed", type=int)
    p.set_defaults(func=cmd_passed)
    p = sub.add_parser("claims")
    p.add_argument("run_dirs", nargs="+")
    p.add_argument("--min", type=int, required=True)
    p.set_defaults(func=cmd_claims)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
