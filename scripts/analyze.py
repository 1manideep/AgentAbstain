#!/usr/bin/env python
"""Summarize one run: ``python scripts/analyze.py <run_dir> [--benefactor] [--json] [--replay] [--outcome PATH ...]``.

The summary holds the population, economy and culture counts, decide-call spend (``calls``, ``real_cost_usd``,
``world_cost_usd``), every call's spend (``total_real_cost_usd``, ``calls_by_purpose``), the memory cost endpoints
(``memory_spend_share``, ``cost_per_correct_recall``), the section 10.4 diagnosis rows per agent ever alive
(``forage_income_usd``, ``decide_cost_usd``, ``mean_input_tokens_per_decide``), the audit exam with its
majority baseline (``exam``), the nightly maintenance (``maintenance``) and the policy versions by source.

``--benefactor`` adds the windfall outcomes (lineage penetration, exogenous vocabulary, costly actions
after grants, persistence). ``--replay`` recomputes ``probe_recall`` by re-running the simulation from
the run's resolved config with probes enabled (in a temporary directory) instead of the offline copy.
``--outcome PATH`` (repeatable) adds ``outcomes: {PATH: value}`` resolved exactly as ``compare`` resolves
them; ``--list-outcomes`` prints every computed outcome name (built-in and registered) and exits.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from void.research.analysis import (  # noqa: E402
    computed_outcome_names,
    end_of_run_outcome,
    probe_recall,
    summarize,
)


def render_summary(summary: dict, indent: str = "") -> str:
    lines: list[str] = []
    for key, value in summary.items():
        if isinstance(value, dict) and value:
            lines.append(f"{indent}{key}:")
            lines.append(render_summary(value, indent + "  "))
        else:
            lines.append(f"{indent}{key}: {value}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--benefactor", action="store_true", help="include the benefactor (windfall) outcomes")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--replay", action="store_true", help="recompute probe_recall by replaying the run with probes")
    ap.add_argument("--outcome", action="append", default=[], metavar="PATH",
                    help="also resolve this outcome path (as compare does); repeatable")
    ap.add_argument("--list-outcomes", action="store_true", help="print every computed outcome name and exit")
    args = ap.parse_args(argv)
    if args.list_outcomes:
        print("\n".join(computed_outcome_names()))
        return 0
    if not args.run_dir:
        ap.error("run_dir is required")
    summary = summarize(args.run_dir, benefactor=args.benefactor)
    if args.replay:
        summary["probe_recall"] = probe_recall(args.run_dir, replay=True)
    if args.outcome:
        summary["outcomes"] = {p: end_of_run_outcome(args.run_dir, p) for p in args.outcome}
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        print(render_summary(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
