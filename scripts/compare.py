#!/usr/bin/env python
"""Compare the arms of an experiment: ``python scripts/compare.py <exp_dir> [--primary path] [--allow-incomplete] [--json]``.

``<exp_dir>`` holds ``<arm>/seed_<n>/`` run directories. Exits 2 with the reason when the runs are not
comparable (configs differ outside the declared independent variables, seed sets differ, degeneration
mode is not ``observe``, pricing is not equalized or tiers mutate where the experiment forbids it, a run did
not complete, ...). ``--allow-incomplete`` drops every seed pair that touches an incomplete run (status other
than ``completed``, or metrics that stop before the last tick) and lists the dropped runs instead of refusing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from void.research.compare import IncomparableRuns, compare, render  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("exp_dir")
    ap.add_argument("--primary", default=None, help="outcome path overriding experiment.primary_outcome")
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="drop the seed pairs that touch an incomplete run (capped, extinct, crashed, truncated) instead of refusing")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args(argv)
    try:
        result = compare(args.exp_dir, primary=args.primary, allow_incomplete=args.allow_incomplete)
    except IncomparableRuns as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
