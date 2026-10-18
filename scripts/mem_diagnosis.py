#!/usr/bin/env python
"""The memory-dependence diagnosis of MEMORY_EVOLUTION 10.4, from runs: ``python scripts/mem_diagnosis.py EXP_DIR
[--json] [--with ARM --without ARM] [--allow-incomplete] [--sweep OUT [--sweep-seeds 1-10] [--sweep-grid ofat|full]
[--sweep-days N] [--jobs N]]``.

``EXP_DIR`` holds ``<arm>/seed_<n>/`` runs of a memory-on arm and an amnesic arm (``void run --seeds`` writes them, e.g.
``runs/exp_mem_dep``); the arms are told apart by ``memory.enabled`` in their resolved configs unless ``--with`` and
``--without`` name them. The runs are paired by seed and checked exactly as ``scripts/gates.py`` checks them (refused
when incomplete or when the arms differ outside their declared variables; ``--allow-incomplete`` drops the seed pairs
that touch an incomplete run).

The table (markdown, or JSON with ``--json``) has one row per quantity of the 10.4 diagnosis, per agent ever alive:
foraging income (``forage_income_usd``), decision-call cost (``decide_cost_usd``), input tokens per decision
(``mean_input_tokens_per_decide``), and the net, the end balance (``mean_balance``). Each row gives both arms' means,
the paired difference with memory minus without, its 95% paired bootstrap interval and ``d_z`` (2000 resamples,
``random.Random(0)``, the statistics ``compare`` and ``gates`` use), the change relative to the amnesic arm, and the
seeds in which the memory arm is higher. Below it, the break-even: memory's extra decision cost divided by the amnesic
arm's foraging income is the rise in foraging income memory needs to pay for itself, set against the rise it gets
(the income difference over the same income, with its interval scaled the same way).

``--sweep OUT`` also runs the free sweep of 10.4 ("node capacity (12 to 40), regrowth (0.2 to 0.4) and view radius (8
to 12)") on scripted brains: each setting is a temporary config per arm that extends the arm's config file
(``configs/<run.name>.yaml`` of EXP_DIR's runs, or ``--sweep-configs``) and sets ``world.node_capacity``,
``world.node_regen_per_tick`` and ``world.view_radius``; ``void run --seeds`` writes its runs to
``OUT/<setting>/<experiment>/<arm>/seed_<n>/`` (a run already there is kept, not redone). 10.4 counts four settings
and does not list them, so the default grid ``ofat`` is the dependence world (capacity 40, regrowth 0.4, view radius
12) and the three settings that move one factor to its other level [assumption]; ``full`` runs all eight
combinations. ``--sweep-days`` shortens the runs (for tests). The sweep refuses any config whose roster is not
scripted-only, so it can never spend money. It prints one row per setting: the balance difference with its interval,
the income change and the break-even, and how many settings the amnesic arm wins on balance.

Exit status: 0 when the table (and the sweep, when asked) was produced, 2 when the runs were refused or missing.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import sys
import tempfile
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from void.config import load_config  # noqa: E402
from void.research import gates  # noqa: E402
from void.research.analysis import resolve_path, run_config_dict  # noqa: E402
from void.research.compare import IncomparableRuns, discover_runs, paired_stats  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
N_BOOT, BOOT_SEED = gates.N_BOOT, gates.BOOT_SEED
# (outcome, label, decimals): the rows of the 10.4 diagnosis table, then the net
ROWS: tuple[tuple[str, str, int], ...] = (
    ("forage_income_usd", "Foraging income (USD)", 4),
    ("decide_cost_usd", "Decision-call cost (USD)", 4),
    ("mean_input_tokens_per_decide", "Input tokens per decision", 0),
    ("mean_balance", "End balance, the net (USD)", 4),
)
INCOME, COST = "forage_income_usd", "decide_cost_usd"
# the sweep's factors, each with the dependence world's level first and the other level of 10.4 second
SWEEP_FACTORS: tuple[tuple[str, str, tuple[float, float]], ...] = (
    ("world.node_capacity", "capacity", (40.0, 12.0)),
    ("world.node_regen_per_tick", "regrowth", (0.4, 0.2)),
    ("world.view_radius", "view radius", (12.0, 8.0)),
)
GRIDS = ("ofat", "full")


class DiagnosisError(RuntimeError):
    """The runs cannot be diagnosed (missing, refused or incomplete); the message says why."""


# --- the diagnosis table -----------------------------------------------------------------------------------------
def memory_arms(exp_dir: str | Path) -> tuple[str, str]:
    """``(without, with)``: the arm whose runs have ``memory.enabled`` false and the one with it true."""
    try:
        found = discover_runs(exp_dir)
    except IncomparableRuns as e:
        raise DiagnosisError(str(e)) from e
    by_memory: dict[bool, list[str]] = {True: [], False: []}
    for arm, by_seed in found.items():
        cfg = run_config_dict(by_seed[min(by_seed)])
        enabled = resolve_path(cfg, "memory.enabled")
        by_memory[True if enabled is None else bool(enabled)].append(arm)
    if len(by_memory[False]) != 1 or len(by_memory[True]) != 1:
        raise DiagnosisError(f"{exp_dir}: need one arm with memory.enabled false and one with it true, found "
                             f"without {by_memory[False]} and with {by_memory[True]}; name them with --with and --without")
    return by_memory[False][0], by_memory[True][0]


def _ratio(num: float | None, den: float | None) -> float | None:
    return None if num is None or not den else num / den


def diagnose(exp_dir: str | Path, *, with_arm: str | None = None, without_arm: str | None = None,
             allow_incomplete: bool = False) -> dict[str, Any]:
    """The 10.4 diagnosis of ``exp_dir`` (module docstring) as a dict; raises :class:`DiagnosisError`."""
    exp_dir = Path(exp_dir)
    if (with_arm is None) != (without_arm is None):
        raise DiagnosisError("name both arms (--with and --without) or neither")
    if with_arm is None or without_arm is None:
        without_arm, with_arm = memory_arms(exp_dir)
    paired = gates.paired_outcomes(exp_dir, without_arm, with_arm, [o for o, _, _ in ROWS], allow_incomplete=allow_incomplete)
    if paired.missing:
        raise DiagnosisError(paired.missing)
    rows: list[dict[str, Any]] = []
    for outcome, label, nd in ROWS:
        s = paired_stats(paired.pairs(outcome), n_boot=N_BOOT, seed=BOOT_SEED)
        rows.append({"outcome": outcome, "label": label, "decimals": nd, "n": s["n"],
                     "without_value": paired.n_without_value(outcome), "without": s["mean_a"], "with": s["mean_b"],
                     "delta": s["delta"], "ci95": list(s["ci95"]), "d_z": s["d_z"],
                     "relative": _ratio(s["delta"], s["mean_a"]), "higher_with": s["positive"],
                     "lower_with": s["negative"], "ties": s["ties"]})
    by = {r["outcome"]: r for r in rows}
    income, cost = by[INCOME], by[COST]
    base = income["without"]
    lo, hi = income["ci95"]
    break_even = {
        "decide_cost_delta": cost["delta"], "forage_income_without": base,
        "required_income_rise": _ratio(cost["delta"], base),
        "observed_income_rise": _ratio(income["delta"], base),
        "observed_income_rise_ci95": [_ratio(lo, base), _ratio(hi, base)],
        "rule": "required = decide-cost delta / forage income without memory; observed = forage income delta / the same",
    }
    return {"experiment": paired.experiment, "exp_dir": str(exp_dir), "arm_without": without_arm, "arm_with": with_arm,
            "n_boot": N_BOOT, "boot_seed": BOOT_SEED, "dropped": paired.dropped, "rows": rows, "break_even": break_even}


def _num(v: float | None, nd: int, sign: bool = False) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"


def _pct(v: float | None, sign: bool = False) -> str:
    if v is None:
        return "n/a"
    return f"{100 * v:+.1f}%" if sign else f"{100 * v:.1f}%"


def render(result: dict[str, Any]) -> str:
    """The diagnosis as a markdown table plus the break-even line."""
    w, wo = result["arm_with"], result["arm_without"]
    n = max((r["n"] for r in result["rows"]), default=0)
    lines = [f"Memory-dependence diagnosis, {result['experiment']} ({result['exp_dir']}): {w} minus {wo}, per agent ever "
             f"alive, {n} paired seeds; 95% paired bootstrap intervals ({result['n_boot']} resamples, seed "
             f"{result['boot_seed']}).", "",
             f"| Per agent | {wo} | {w} | {w} minus {wo} [95% CI] | d_z | Change | {w} higher in |",
             "|---|---|---|---|---|---|---|"]
    for r in result["rows"]:
        nd = r["decimals"]
        lo, hi = r["ci95"]
        lines.append(f"| {r['label']} | {_num(r['without'], nd)} | {_num(r['with'], nd)} | "
                     f"{_num(r['delta'], nd, True)} [{_num(lo, nd)}, {_num(hi, nd)}] | {_num(r['d_z'], 2)} | "
                     f"{_pct(r['relative'], True)} | {r['higher_with']} of {r['n']} seeds |")
    b = result["break_even"]
    lo, hi = b["observed_income_rise_ci95"]
    lines += ["", f"Break-even: memory adds {_num(b['decide_cost_delta'], 4, True)} USD of decision cost per agent, so "
                  f"foraging income ({_num(b['forage_income_without'], 4)} USD without memory) must rise by "
                  f"{_pct(b['required_income_rise'])} to pay for it; it rises by {_pct(b['observed_income_rise'], True)} "
                  f"[{_pct(lo, True)}, {_pct(hi, True)}]."]
    if result["dropped"]:
        lines.append(f"Dropped as incomplete or unpaired: {'; '.join(result['dropped'])}.")
    return "\n".join(lines)


# --- the sweep ---------------------------------------------------------------------------------------------------
def sweep_settings(grid: str = "ofat") -> list[dict[str, float]]:
    """The settings of the sweep, each ``{dotted path: value}`` over :data:`SWEEP_FACTORS`: ``ofat`` the dependence
    world and one factor moved at a time (four settings), ``full`` every combination (eight)."""
    if grid not in GRIDS:
        raise ValueError(f"grid must be one of {GRIDS}")
    base = {path: levels[0] for path, _, levels in SWEEP_FACTORS}
    if grid == "ofat":
        return [base] + [{**base, path: levels[1]} for path, _, levels in SWEEP_FACTORS]
    return [dict(zip([p for p, _, _ in SWEEP_FACTORS], combo, strict=True))
            for combo in itertools.product(*(levels for _, _, levels in SWEEP_FACTORS))]


def setting_name(setting: dict[str, float]) -> str:
    """``cap40_regen0.4_view12`` style: one token per factor, in :data:`SWEEP_FACTORS` order."""
    short = {"world.node_capacity": "cap", "world.node_regen_per_tick": "regen", "world.view_radius": "view"}
    return "_".join(f"{short[p]}{setting[p]:g}" for p, _, _ in SWEEP_FACTORS)


def _nest(dotted: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, value in dotted.items():
        *parents, key = path.split(".")
        node = out
        for p in parents:
            node = node.setdefault(p, {})
        node[key] = value
    return out


def arm_configs(exp_dir: str | Path, configs_dir: str | Path | None = None) -> list[Path]:
    """The config file of each arm under ``exp_dir``: ``<configs_dir>/<run.name>.yaml`` of its first run."""
    root = Path(configs_dir) if configs_dir is not None else REPO / "configs"
    try:
        found = discover_runs(exp_dir)
    except IncomparableRuns as e:
        raise DiagnosisError(str(e)) from e
    out: list[Path] = []
    for arm, by_seed in found.items():
        name = resolve_path(run_config_dict(by_seed[min(by_seed)]), "run.name")
        path = root / f"{name}.yaml"
        if not path.is_file():
            raise DiagnosisError(f"arm {arm!r}: no config file {path} for run.name {name!r}; give --sweep-configs")
        out.append(path)
    return out


def write_setting_configs(arm_configs: Sequence[str | Path], setting: dict[str, float], tmp_dir: str | Path,
                          days: int | None = None) -> list[Path]:
    """One temporary config per arm under ``tmp_dir``: ``extends`` the arm's config file and sets the setting's world
    values (and ``run.days`` when given). Refuses (``DiagnosisError``) a config whose roster is not scripted-only."""
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for cfg_path in arm_configs:
        cfg_path = Path(cfg_path).resolve()
        over = _nest(setting)
        if days is not None:
            over["run"] = {"days": int(days)}
        path = tmp_dir / f"{cfg_path.stem}__{setting_name(setting)}.yaml"
        path.write_text(yaml.safe_dump({"extends": str(cfg_path), **over}, sort_keys=True))
        cfg = load_config(path)
        paid = sorted({a.tier for a in cfg.agents if cfg.tiers[a.tier].provider != "scripted"})
        if paid:
            raise DiagnosisError(f"{cfg_path}: tiers {paid} are not scripted; the sweep runs scripted brains only")
        out.append(path)
    return out


def _run_config(job: tuple[str, str, str, str]) -> int:
    """``void run --config CONFIG --seeds SEEDS --tick-seconds 0 --out OUT``, its output to LOG; the exit status."""
    from void.cli import main as cli_main

    config, seeds, out, log = job
    with open(log, "a", encoding="utf-8") as fh, contextlib.redirect_stdout(fh), contextlib.redirect_stderr(fh):
        return cli_main(["run", "--config", config, "--seeds", seeds, "--tick-seconds", "0", "--out", out])


def run_sweep(arm_config_paths: Sequence[str | Path], out: str | Path, *, seeds: str = "1-10", grid: str = "ofat",
              settings: Sequence[dict[str, float]] | None = None, days: int | None = None, jobs: int = 1,
              allow_incomplete: bool = False) -> dict[str, Any]:
    """Run the sweep (module docstring) into ``out`` and diagnose every setting."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    chosen = list(settings) if settings is not None else sweep_settings(grid)
    experiment: str | None = None
    with tempfile.TemporaryDirectory(prefix="mem_sweep_") as tmp:
        jobs_list: list[tuple[str, str, str, str]] = []
        for setting in chosen:
            name = setting_name(setting)
            configs = write_setting_configs(arm_config_paths, setting, Path(tmp) / name, days)
            names = {load_config(c).experiment.name for c in configs}
            if len(names) != 1 or None in names:
                raise DiagnosisError(f"the sweep's arm configs must share one experiment.name, found {sorted(map(str, names))}")
            experiment = str(names.pop())
            for cfg in configs:
                jobs_list.append((str(cfg), seeds, str(out / name), str(out / name / f"{Path(cfg).stem}.log")))
                (out / name).mkdir(parents=True, exist_ok=True)
        if jobs > 1:
            with ProcessPoolExecutor(max_workers=jobs) as pool:
                list(pool.map(_run_config, jobs_list))
        else:
            for job in jobs_list:
                _run_config(job)
    rows: list[dict[str, Any]] = []
    for setting in chosen:
        name = setting_name(setting)
        exp_dir = out / name / str(experiment)
        row: dict[str, Any] = {"setting": name, "values": dict(setting), "exp_dir": str(exp_dir)}
        try:
            row["diagnosis"] = diagnose(exp_dir, allow_incomplete=allow_incomplete)
        except DiagnosisError as e:
            row["diagnosis"], row["error"] = None, str(e)
        rows.append(row)
    return {"out": str(out), "seeds": seeds, "grid": grid if settings is None else "custom", "days": days,
            "factors": [{"path": p, "label": label, "levels": list(levels)} for p, label, levels in SWEEP_FACTORS],
            "settings": rows}


def render_sweep(result: dict[str, Any]) -> str:
    """One markdown row per setting, then how many settings the amnesic arm wins on balance."""
    days = "" if result["days"] is None else f", {result['days']} day(s)"
    lines = [f"Sweep ({result['grid']}, seeds {result['seeds']}{days}) under {result['out']}:", "",
             "| Setting | Node capacity | Regrowth | View radius | n | Balance, memory minus amnesic [95% CI] | "
             "Ahead on balance | Income change | Break-even income rise |",
             "|---|---|---|---|---|---|---|---|---|"]
    wins = decided = 0
    for row in result["settings"]:
        v = row["values"]
        head = (f"| {row['setting']} | {v['world.node_capacity']:g} | {v['world.node_regen_per_tick']:g} | "
                f"{v['world.view_radius']:g} |")
        d = row["diagnosis"]
        if d is None:
            lines.append(f"{head} 0 | not evaluated: {row['error']} | | | |")
            continue
        bal = next(r for r in d["rows"] if r["outcome"] == "mean_balance")
        lo, hi = bal["ci95"]
        ahead = "n/a" if bal["delta"] is None else (d["arm_with"] if bal["delta"] > 0 else d["arm_without"] if bal["delta"] < 0 else "tie")
        if bal["delta"] is not None:
            decided += 1
            wins += bal["delta"] < 0
        b = d["break_even"]
        lines.append(f"{head} {bal['n']} | {_num(bal['delta'], 4, True)} [{_num(lo, 4)}, {_num(hi, 4)}] | {ahead} | "
                     f"{_pct(b['observed_income_rise'], True)} | {_pct(b['required_income_rise'])} |")
    lines += ["", f"The amnesic arm is ahead on balance in {wins} of {decided} settings."]
    return "\n".join(lines)


# --- command line ------------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("exp_dir", help="experiment directory holding a memory-on arm and an amnesic arm (<arm>/seed_<n>/)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    ap.add_argument("--with", dest="with_arm", default=None, help="the memory-on arm (default: by memory.enabled)")
    ap.add_argument("--without", dest="without_arm", default=None, help="the amnesic arm (default: by memory.enabled)")
    ap.add_argument("--allow-incomplete", action="store_true", help="drop the seed pairs that touch an incomplete run")
    ap.add_argument("--sweep", metavar="OUT", default=None, help="also run the 10.4 sweep into OUT and tabulate it")
    ap.add_argument("--sweep-configs", default=None, metavar="A.yaml,B.yaml",
                    help="the arm config files the sweep extends (default: configs/<run.name>.yaml of EXP_DIR's arms)")
    ap.add_argument("--sweep-seeds", default="1-10", help="seeds of the sweep (default 1-10)")
    ap.add_argument("--sweep-grid", choices=GRIDS, default="ofat", help="ofat (four settings, default) or full (eight)")
    ap.add_argument("--sweep-days", type=int, default=None, help="override run.days in the sweep (for tests)")
    ap.add_argument("--jobs", type=int, default=1, help="parallel sweep runs (default 1)")
    args = ap.parse_args(argv)
    try:
        result = diagnose(args.exp_dir, with_arm=args.with_arm, without_arm=args.without_arm,
                          allow_incomplete=args.allow_incomplete)
        sweep = None
        if args.sweep:
            configs = ([Path(p.strip()) for p in args.sweep_configs.split(",") if p.strip()] if args.sweep_configs
                       else arm_configs(args.exp_dir))
            sweep = run_sweep(configs, args.sweep, seeds=args.sweep_seeds, grid=args.sweep_grid, days=args.sweep_days,
                              jobs=max(1, args.jobs), allow_incomplete=args.allow_incomplete)
    except DiagnosisError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"diagnosis": result, "sweep": sweep}, indent=2, sort_keys=True, default=str))
    else:
        print(render(result))
        if sweep is not None:
            print()
            print(render_sweep(sweep))
    if sweep is not None and any(r["diagnosis"] is None for r in sweep["settings"]):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
