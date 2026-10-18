#!/usr/bin/env python
"""Gates G0 and G1 from runs: ``python scripts/gates.py RUNS_ROOT [--experiments DEP,E0 | --live DATA_LIVE] [--g1 EXP_DIR] [--json] [--allow-incomplete]``.

Prints one line per condition (C1 memory dependence, C2 index builder over no-op with power, C3 hoarder below no-op,
C4 decoy not above no-op on balance but above it on policy length) and an overall G0 line; the rules are in
``void/research/gates.py``. ``RUNS_ROOT`` holds ``<experiment>/<arm>/seed_<n>/`` runs, as ``void run --seeds`` writes
them; ``--experiments`` names the dependence and E0 experiments (default ``exp_mem_dep,exp_mem_e0``). With ``--live``,
C1 is evaluated instead on each live paired experiment of ``scripts/live_claude.sh`` under DATA_LIVE
(``exp_mem_dep_claude`` and ``exp_mem_dep_veins_claude``), with the same statistics; C2 to C4 still come from
RUNS_ROOT. ``--allow-incomplete`` drops the seed pairs that touch an incomplete run instead of refusing them; the
dropped runs are listed on stderr. Each condition reads its margin (and C2 its power) from the hypotheses the runs'
configs declare (``experiment.hypotheses``) and names the config file that declares them.

``--g1 EXP_DIR`` adds gate G1 (MEMORY_EVOLUTION 10.2) on the E1 experiment directory (``<arm>/seed_<n>/`` with arms
``a0``, ``a``, ``b``, ``c``): G1a, ``maintenance_valid_command_rate`` at least 0.9 in every arm that runs maintenance,
and G1b, ``c - b`` with ``d_z`` at least 0.5 on at least 10 pairs; the G1 line combines them with the C1 line(s), and a
last line gives the exit status and names every condition that failed or is missing.

Exit status, over every condition printed: 0 when every condition passes, 1 when any fails, 2 when none fails and any
is missing (an arm without completed runs, too few pairs, or runs refused as incomplete or not comparable).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from void.research.gates import DEPENDENCE, E0, evaluate_g0, notes, render  # noqa: E402


def _experiments(text: str) -> tuple[str, str]:
    names = [part.strip() for part in text.split(",") if part.strip()]
    if len(names) != 2:
        raise argparse.ArgumentTypeError("give two experiment names, the dependence experiment then E0: DEP,E0")
    return names[0], names[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs_root", help="directory holding <experiment>/<arm>/seed_<n>/ runs")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--experiments", type=_experiments, default=(DEPENDENCE, E0), metavar="DEP,E0",
                       help=f"the dependence and E0 experiment directories under RUNS_ROOT (default {DEPENDENCE},{E0})")
    which.add_argument("--live", metavar="DATA_LIVE", default=None,
                       help="evaluate C1 on the live paired experiments under DATA_LIVE instead")
    ap.add_argument("--g1", metavar="EXP_DIR", default=None,
                    help="also evaluate gate G1 on the E1 experiment directory EXP_DIR (arms a0, a, b, c)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="drop the seed pairs that touch an incomplete run instead of refusing the condition")
    args = ap.parse_args(argv)
    result = evaluate_g0(args.runs_root, experiments=args.experiments, live_root=args.live,
                         allow_incomplete=args.allow_incomplete, g1_dir=args.g1)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        for note in notes(result):
            print(f"note: {note}", file=sys.stderr)
        print(render(result))
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
