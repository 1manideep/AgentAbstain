#!/usr/bin/env python
"""Summarize one run: ``python scripts/analyze.py <run_dir> [--benefactor] [--json] [--replay]``.

``--benefactor`` adds the windfall outcomes (lineage penetration, exogenous vocabulary, costly actions
after grants, persistence). ``--replay`` recomputes ``probe_recall`` by re-running the simulation from
the run's resolved config with probes enabled (in a temporary directory) instead of the offline copy.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from void.research.analysis import probe_recall, summarize  # noqa: E402


def render_summary(summary: dict, indent: str = "") -> str:
    lines: list[str] = []
    for key, value in summary.items():
        if isinstance(value, dict):
            lines.append(f"{indent}{key}:")
            lines.append(render_summary(value, indent + "  "))
        else:
            lines.append(f"{indent}{key}: {value}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir")
    ap.add_argument("--benefactor", action="store_true", help="include the benefactor (windfall) outcomes")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--replay", action="store_true", help="recompute probe_recall by replaying the run with probes")
    args = ap.parse_args(argv)
    summary = summarize(args.run_dir, benefactor=args.benefactor)
    if args.replay:
        summary["probe_recall"] = probe_recall(args.run_dir, replay=True)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        print(render_summary(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
