#!/usr/bin/env python
"""Regenerate every figure in docs/research/MEMORY_EVOLUTION.md.

    .venv/bin/void run --config configs/demo.yaml --seeds 1-4,42 --days 14 --tick-seconds 0 --out runs/figs
    .venv/bin/python docs/research/figures/make_figures.py --runs runs/figs/demo/default [--only power,cost]

Requires matplotlib, numpy, scipy and networkx (``uv pip install -e '.[figures]'``).

Figure kinds, stated on every image so nobody mistakes one for the other:

* REAL     computed from run databases or from the repo's own price table and prompt sizes
* COMPUTED analytic (statistical power); exact given its stated assumptions
* SCHEMATIC design diagrams of a proposal; nothing here is built yet unless a box says "exists"
* HYPOTHETICAL curves drawn to show what each hypothesis predicts; NOT data

Colours are the dataviz reference palette (first slots validated adjacent, light surface);
lines are direct-labelled because aqua and yellow sit below 3:1 contrast on the surface.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402,F401
import numpy as np  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#8a8983"
GRID = "#e6e5e0"
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, VIOLET = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#4a3aa7"
NEUTRAL_FILL, NEUTRAL_EDGE = "#f0efec", "#b9b8b2"
BLUE_TINT, ORANGE_TINT = "#e3eefb", "#fbe6dc"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10, "text.color": INK, "axes.edgecolor": NEUTRAL_EDGE,
    "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2, "axes.facecolor": SURFACE,
    "figure.facecolor": SURFACE, "savefig.facecolor": SURFACE, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True, "lines.solid_capstyle": "round",
})
DPI = 170


def save(fig, name: str) -> None:
    out = HERE / name
    fig.savefig(out, dpi=DPI)
    plt.close(fig)
    print("wrote", out.relative_to(REPO))


def tag(fig, kind: str, note: str = "") -> None:
    colors = {"REAL": AQUA, "COMPUTED": BLUE, "SCHEMATIC": MUTED, "HYPOTHETICAL": ORANGE}
    fig.text(0.012, 0.012, f"{kind}", fontsize=8.5, fontweight="bold", color=colors[kind], ha="left", va="bottom")
    if note:
        fig.text(0.012 + 0.012 * (len(kind) + 1) * 0.62, 0.012, note, fontsize=8.5, color=INK2, ha="left", va="bottom")


# --------------------------------------------------------------------------------------------- drawing helpers
def canvas(w: float, h: float, title: str, subtitle: str = ""):
    fig = plt.figure(figsize=(w, h), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, w * 10)
    ax.set_ylim(0, h * 10)
    ax.axis("off")
    ax.grid(False)
    ax.text(2, h * 10 - 3.2, title, fontsize=15, fontweight="bold", va="top")
    if subtitle:
        ax.text(2, h * 10 - 8.2, subtitle, fontsize=9.6, color=INK2, va="top")
    return fig, ax


def box(ax, x, y, w, h, title, body="", fc=NEUTRAL_FILL, ec=NEUTRAL_EDGE, tsize=10, bsize=8.4, lw=1.2, dashed=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=1.4", fc=fc, ec=ec, lw=lw,
                                ls="--" if dashed else "-"))
    ax.text(x + 1.6, y + h - 1.7, title, fontsize=tsize, fontweight="bold", va="top", ha="left", color=INK)
    if body:
        ax.text(x + 1.6, y + h - 1.7 - tsize * 0.22 - 0.9, body, fontsize=bsize, va="top", ha="left", color=INK2, linespacing=1.35)


def arrow(ax, p0, p1, label="", color=INK2, lw=1.4, rad=0.0, lx=None, ly=None, style="-|>", ls="-"):
    ax.annotate("", xy=p1, xytext=p0, arrowprops=dict(arrowstyle=style, color=color, lw=lw, shrinkA=0, shrinkB=0,
                                                     connectionstyle=f"arc3,rad={rad}", ls=ls))
    if label:
        mx = (p0[0] + p1[0]) / 2 if lx is None else lx
        my = (p0[1] + p1[1]) / 2 if ly is None else ly
        ax.text(mx, my, label, fontsize=8, color=INK2, ha="center", va="center",
                bbox=dict(fc=SURFACE, ec="none", pad=1.2))


def legend_boxes(ax, x, y, items):
    for i, (label, fc, ec) in enumerate(items):
        ax.add_patch(FancyBboxPatch((x + i * 38, y), 4.2, 2.6, boxstyle="round,pad=0,rounding_size=0.6", fc=fc, ec=ec, lw=1.2))
        ax.text(x + i * 38 + 5.6, y + 1.3, label, fontsize=8.6, va="center", color=INK2)


# ------------------------------------------------------------------------------------------------ SCHEMATICS
def fig_architecture():
    fig, ax = canvas(13, 7.6, "Where the evolving-memory layer sits",
                     "The existing kernel stays the referee. New pieces add a confined memory API, an agent-owned policy file and a nightly maintenance step.")
    new = dict(fc=BLUE_TINT, ec=BLUE)
    cw, ch, gap = 21.2, 15.4, 2.4
    xs = [33, 33 + cw + gap, 33 + 2 * (cw + gap)]
    rows = [47, 28.5, 10]
    # left column
    box(ax, 2, 34, 24, 26, "Agent brain", "Any model behind an API.\nOne decision per tick.\n\nReads: observation, self\nsummary, retrieved notes,\nits own memory policy.\nWrites: one action plus\nmemory commands.", fc=SURFACE, ec=INK2, bsize=8.2)
    box(ax, 2, 9, 24, 16, "MCP wrapper", "The same six commands\nserved over MCP, so any\nharness or model can use\nthe framework.", **new, bsize=8.2)
    # kernel zone
    ax.add_patch(FancyBboxPatch((31, 6.5), 76, 59, boxstyle="round,pad=0,rounding_size=1.8", fc="#f7f6f3", ec=NEUTRAL_EDGE, lw=1.2, ls="--"))
    ax.text(33, 64.2, "Kernel (the referee)", fontsize=10.5, fontweight="bold", va="top")
    box(ax, xs[0], rows[0], cw, ch, "Memory policy file", "Short markdown the agent\nowns; enters its prompt\nevery tick. Size-capped,\ndiffed daily.", **new, bsize=8)
    box(ax, xs[1], rows[0], cw, ch, "Nightly maintenance", "One extra call per agent\nper day, paid from its own\nwallet: memory work\ncompetes with foraging.", **new, bsize=8)
    box(ax, xs[2], rows[0], cw, ch, "Memory metrics", "Policy drift, adoption,\nvault structure, recall,\ncost per useful recall.", **new, bsize=8)
    box(ax, xs[0], rows[1], cw, ch, "Memory commands", "view, create, str_replace,\ninsert, delete, rename.\nConfined to own vault,\nquota'd, audited.", **new, bsize=8)
    box(ax, xs[1], rows[1], cw, ch, "Vault store + index", "Markdown notes, SQLite\nindex, auto-linking, hop\nretrieval, provenance.\nExists today.", bsize=8)
    box(ax, xs[2], rows[1], cw, ch, "Gossip", "Note copies with drift and\nprovenance. Exists today.\nNew: share_practice moves\npolicy excerpts.", bsize=8)
    box(ax, xs[0], rows[2], cw, ch, "Wallet + ledger", "Every call and fee is a\nledger row. Thinking is\npaid by the agent.\nExists today.", bsize=8)
    box(ax, xs[1], rows[2], cw, ch, "Probes + tracers", "Planted facts measure\nrecall and spread.\nExists today.", bsize=8)
    box(ax, xs[2], rows[2], cw, ch, "Chronicle + graveyard", "Daily public record, dead\nagents' vaults archived.\nExists today.", bsize=8)
    # right column
    box(ax, 110, 42, 19, 19, "Observability", "Vault opens in\nObsidian. Policy diffs\nper day. Adoption and\nstructure charts in the\ncontrol room.", **new, bsize=8)
    # arrows
    arrow(ax, (33, 56), (26, 56), "", lw=1.4)                              # policy enters the prompt
    ax.text(28.6, 58.4, "policy", fontsize=7.6, color=INK2, ha="center")
    arrow(ax, (26, 38), (33, 36.2), "", lw=1.4)                            # memory ops
    ax.text(28.6, 41.2, "memory\nops", fontsize=7.6, color=INK2, ha="center", va="center", linespacing=1.15)
    arrow(ax, (xs[1], 55), (xs[0] + cw, 55), "", lw=1.4)                   # maintenance edits policy
    arrow(ax, (xs[0] + cw, 36.2), (xs[1], 36.2), "", lw=1.4)               # commands -> vault
    arrow(ax, (xs[2] + cw, 55), (110, 52), "", lw=1.4)                     # metrics -> observability
    arrow(ax, (26, 17), (xs[0] + 4, rows[1]), "", lw=1.4, rad=-0.2)        # MCP wraps commands
    ax.text(28.4, 22.2, "wraps", fontsize=7.6, color=INK2, ha="center", bbox=dict(fc="#f7f6f3", ec="none", pad=1))
    legend_boxes(ax, 2, 3.6, [("exists in the repo today", NEUTRAL_FILL, NEUTRAL_EDGE), ("proposed", BLUE_TINT, BLUE)])
    tag(fig, "SCHEMATIC", "proposal diagram; blue boxes are not built yet")
    save(fig, "fig1_architecture.png")


def fig_trust():
    fig, ax = canvas(13, 7.2, "Trust boundary: what an agent may write and what the kernel owns",
                     "Agents get real control of how they remember, but never of the evidence the experiment is measured with.")
    ax.add_patch(FancyBboxPatch((2, 15), 46, 43, boxstyle="round,pad=0,rounding_size=1.6", fc=ORANGE_TINT, ec=ORANGE, lw=1.3))
    ax.add_patch(FancyBboxPatch((82, 15), 46, 43, boxstyle="round,pad=0,rounding_size=1.6", fc=BLUE_TINT, ec=BLUE, lw=1.3))
    ax.text(4, 56.4, "Agent-writable", fontsize=11.5, fontweight="bold", va="top")
    ax.text(84, 56.4, "Kernel-owned", fontsize=11.5, fontweight="bold", va="top")
    left = ["Note titles, bodies, links and tags", "Its own folders and index notes", "memory_policy.md (size-capped, diffed)", "What to merge, rename, keep or delete\nin its own vault"]
    right = ["Provenance: channel, source agent,\nhop count, path of agents", "Importance, timestamps, note ids", "Probes, tracers, ledger, balances", "Quotas, rate limits, audit log of\nevery command"]
    for i, (a, b) in enumerate(zip(left, right, strict=True)):
        y = 46.4 - i * 8.9
        ax.add_patch(FancyBboxPatch((4.2, y - 3.6), 41.6, 7.8, boxstyle="round,pad=0,rounding_size=1", fc=SURFACE, ec=ORANGE, lw=0.9))
        ax.text(6, y + 0.2, a, fontsize=9, va="center", color=INK)
        ax.add_patch(FancyBboxPatch((84.2, y - 3.6), 41.6, 7.8, boxstyle="round,pad=0,rounding_size=1", fc=SURFACE, ec=BLUE, lw=0.9))
        ax.text(86, y + 0.2, b, fontsize=9, va="center", color=INK)
    box(ax, 55, 26, 20, 26, "Validator", "Resolves the path,\nchecks it stays inside\nthe agent's vault,\nenforces quotas and\nsize caps, then writes\nthe file and the\ndatabase row.", fc=SURFACE, ec=INK2, bsize=8.4)
    arrow(ax, (48, 41), (55, 41), "command", lx=51.5, ly=44.5)
    arrow(ax, (75, 41), (82, 41), "metadata", lx=78.5, ly=44.5)
    ax.add_patch(FancyBboxPatch((2, 3.4), 126, 9.4, boxstyle="round,pad=0,rounding_size=1.4", fc=NEUTRAL_FILL, ec=NEUTRAL_EDGE, lw=1.2))
    ax.text(4, 11.8, "Reindex rule", fontsize=10.5, fontweight="bold", va="top")
    ax.text(4, 8.6, "Metadata found in a file is untrusted. The database row is authoritative, and a file that disagrees with it is flagged, not believed.\n"
            "Today reindex rebuilds the index from file frontmatter. That must change before agents can write files, or an agent could forge where a note came from.",
            fontsize=8.8, color=INK2, va="top", linespacing=1.4)
    tag(fig, "SCHEMATIC", "proposal diagram")
    save(fig, "fig2_trust_boundary.png")


def fig_loop():
    fig, ax = canvas(12.6, 8.8, "Three ways a memory practice changes, and one way it is judged",
                     "The unit of evolution is a short text file. Everything else in the world stays the same.")
    cx, cy = 63, 41
    ax.add_patch(plt.Circle((cx, cy), 12.5, fc=BLUE_TINT, ec=BLUE, lw=1.6))
    ax.text(cx, cy + 3, "Memory policy", fontsize=11.5, fontweight="bold", ha="center")
    ax.text(cx, cy - 1.6, "a short markdown file\nthe agent owns", fontsize=8.6, color=INK2, ha="center", linespacing=1.35)
    box(ax, 40, 63, 46, 11, "1. Self-revision  (days)", "Nightly, the agent edits its vault and its policy.\nWithin one lifetime: learning, in the Lamarckian sense.", fc=SURFACE, ec=ORANGE, bsize=8.3)
    box(ax, 82, 31.5, 42, 19, "2. Social transmission  (days)", "A neighbour may share a policy excerpt.\nThe receiver adopts, adapts or ignores it.\nEvery adoption is logged with its source,\nso practices have lineages.", fc=SURFACE, ec=ORANGE, bsize=8.3)
    box(ax, 40, 8, 46, 11, "3. Inheritance + mutation  (generations)", "A child starts from the parent's policy, rewritten with\nsmall random changes. Darwinian variation.", fc=SURFACE, ec=ORANGE, bsize=8.3)
    box(ax, 2, 31.5, 42, 19, "Selection", "The survival economy does the judging:\nevery thought is paid for, bankrupt agents\ndie, wealthy agents reproduce and are\nimitated. Nobody scores the policy directly.", fc=SURFACE, ec=INK2, bsize=8.3)
    arrow(ax, (cx, 53.5), (cx, 63), "", color=ORANGE, lw=1.6, style="<|-|>")
    arrow(ax, (75.5, cy), (82, cy), "", color=ORANGE, lw=1.6, style="<|-|>")
    arrow(ax, (cx, 28.5), (cx, 19), "", color=ORANGE, lw=1.6, style="<|-|>")
    arrow(ax, (44, cy), (50.5, cy), "", color=INK2, lw=1.6, style="<|-|>")
    box(ax, 2, 6, 34, 14, "Fitness signals to compare", "A  survival only\nB  measured recall on planted facts\nC  adoption of your practice by others", fc=NEUTRAL_FILL, ec=NEUTRAL_EDGE, bsize=8.6)
    arrow(ax, (19, 20), (19, 31.5), "", color=INK2)
    tag(fig, "SCHEMATIC", "proposal diagram")
    save(fig, "fig3_evolution_operators.png")


# ------------------------------------------------------------------------------------------ COMPUTED / REAL
def power_t(n: int, d: float, alpha: float = 0.05) -> float:
    from scipy import stats
    df = n - 1
    tcrit = stats.t.ppf(1 - alpha / 2, df)
    nc = d * math.sqrt(n)
    upper = stats.nct.sf(tcrit, df, nc)
    if not math.isfinite(upper):  # scipy's noncentral t returns NaN at some large noncentralities; power there is 1 to machine precision
        upper = 1.0
    return float(min(1.0, upper + stats.nct.cdf(-tcrit, df, nc)))


def power_sign(n: int, d: float, alpha: float = 0.05) -> float:
    from scipy import stats
    c = -1
    for k in range(n + 1):
        if 2 * stats.binom.cdf(k, n, 0.5) <= alpha:
            c = k
        else:
            break
    if c < 0:
        return 0.0
    p = float(stats.norm.cdf(d))
    return float(stats.binom.cdf(c, n, p) + stats.binom.sf(n - c - 1, n, p))


def stay_above(ns, y, level=0.8):
    """First n after which the curve stays at or above `level` (None if it never does within the range)."""
    for i in range(len(ns)):
        if (y[i:] >= level).all():
            return int(ns[i])
    return None


def fig_power():
    ns = np.arange(3, 41)
    effects = [(0.5, BLUE, "d = 0.5  medium"), (0.8, ORANGE, "d = 0.8  large"), (1.2, AQUA, "d = 1.2"), (1.6, YELLOW, "d = 1.6")]
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.9), dpi=DPI, sharey=True)
    fig.subplots_adjust(left=0.06, right=0.975, top=0.80, bottom=0.21, wspace=0.07)
    fig.text(0.06, 0.94, "How many paired seeds does one comparison need?", fontsize=15, fontweight="bold", va="top")
    fig.text(0.06, 0.885, "Power to detect a difference between two arms, by standardized paired effect size d, at alpha 0.05, two-sided. Dots mark the seeds needed to stay above 80%.", fontsize=9.4, color=INK2, va="top")
    need = {}
    for ax, fn, title in ((axes[0], power_t, "Paired t-test"), (axes[1], power_sign, "Exact sign test  (one of the three statistics compare.py reports)")):
        for d, col, lab in effects:
            y = np.array([fn(int(n), d) for n in ns])
            ax.plot(ns, y, color=col, lw=2.2, label=lab)
            n80 = stay_above(ns, y)
            need[f"{title.split('  ')[0]}|d={d}"] = n80
            if n80 is not None:
                yy = y[list(ns).index(n80)]
                ax.plot([n80], [yy], marker="o", ms=7, mfc=col, mec=SURFACE, mew=1.6, zorder=5)
                ax.text(n80 + 0.5, yy - 0.055, f"{n80}", fontsize=9, color=INK, va="top", fontweight="bold")
            else:
                ax.text(39.6, y[-1] + 0.05, "> 40", fontsize=9, color=INK, va="bottom", ha="right", fontweight="bold")
        ax.axhline(0.8, color=INK2, lw=1, ls=(0, (4, 3)))
        ax.text(39.7, 0.785, "80% power", fontsize=8.4, color=INK2, va="top", ha="right")
        ax.set_title(title, fontsize=10.5, loc="left", color=INK, pad=8)
        ax.set_xlim(3, 40)
        ax.set_ylim(0, 1.02)
        ax.set_xlabel("paired seeds per arm")
    axes[0].set_ylabel("power")
    axes[1].tick_params(labelleft=False)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4, frameon=False, fontsize=9.4, labelcolor=INK2, bbox_to_anchor=(0.5, 0.075))
    tag(fig, "COMPUTED", "noncentral t and exact binomial; per-seed differences assumed normal with sd 1")
    save(fig, "fig4_power_analysis.png")
    return need


def tier_cost(cfg, tier: str, maint_per_day: float, days=14, agents=10, ticks=24, dec=(555, 753, 200), mnt=(6000, 0, 900)):
    from void.brain.base import Usage
    from void.economy.pricing import real_cost_micro
    t = cfg.tiers[tier]
    tb = float(t.thinking_budget or 0)
    d = Usage(input_tokens=dec[0], cache_read_tokens=dec[1], output_tokens=int(dec[2] + 0.35 * tb))
    m = Usage(input_tokens=mnt[0], cache_read_tokens=mnt[1], output_tokens=int(mnt[2] + 0.6 * tb))
    per_agent_day = ticks * real_cost_micro(d, t, cfg.tiers) + maint_per_day * real_cost_micro(m, t, cfg.tiers)
    return per_agent_day * days * agents / 1e6, ticks * real_cost_micro(d, t, cfg.tiers) * days * agents / 1e6


def fig_cost():
    from void.config import load_config
    cfg = load_config(REPO / "configs" / "base.yaml")
    tiers = [("average", "Gemini 3.1 Flash-Lite", BLUE), ("sharp", "Gemini 3.7 Flash", ORANGE), ("budget", "Claude Haiku 4.5", AQUA), ("frontier", "Claude Opus 5.5", YELLOW)]
    xs = [0, 1, 2, 4]
    fig, ax = plt.subplots(figsize=(11.5, 6.2), dpi=DPI)
    fig.subplots_adjust(left=0.085, right=0.79, top=0.80, bottom=0.20)
    fig.text(0.085, 0.94, "What a 14-day, 10-agent run costs, and what nightly maintenance adds", fontsize=15, fontweight="bold", va="top")
    fig.text(0.085, 0.885, "Real prompt sizes from the repo's renderer and the tier prices in configs/base.yaml. Output lengths and thinking use are assumptions (see footnote).", fontsize=9.4, color=INK2, va="top")
    table = {}
    for tier, label, col in tiers:
        ys = [tier_cost(cfg, tier, x)[0] for x in xs]
        table[tier] = ys
        ax.plot(xs, ys, color=col, lw=2.2, marker="o", ms=7, mfc=col, mec=SURFACE, mew=1.6, label=label)
        ax.text(4.12, ys[-1], f"{label}   ${ys[-1]:.2f}" if ys[-1] < 10 else f"{label}   ${ys[-1]:.0f}", color=INK, fontsize=9, va="center")
    ax.set_yscale("log")
    ax.set_yticks([1, 2, 5, 10, 20, 50])
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:g}"))
    ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_ylim(1.2, 62)
    ax.set_xticks(xs)
    ax.set_xlim(-0.15, 4.15)
    ax.set_xlabel("maintenance calls per agent per day")
    ax.set_ylabel("cost of one run (USD, log scale)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.15), ncol=4, frameon=False, fontsize=9, labelcolor=INK2)
    tag(fig, "REAL", "decision call 555 fresh + 753 cached input, 200 output tokens; maintenance 6,000 input, 900 output; thinking 35% / 60% of budget")
    save(fig, "fig5_cost_model.png")
    return table


def load_days(run_dir: Path):
    rows = []
    for line in (run_dir / "metrics.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r.get("kind") == "day":
            rows.append(r)
    return sorted(rows, key=lambda r: int(r["day"]))


def fig_generations(runs: Path):
    seeds = sorted(p for p in runs.glob("seed_*") if (p / "metrics.jsonl").exists())
    if not seeds:
        print("skip generations: no runs under", runs)
        return {}
    series_gen, series_birth, days = [], [], None
    for sd in seeds:
        d = load_days(sd)
        days = [int(r["day"]) for r in d]
        series_gen.append([int(r.get("max_generation", 0)) for r in d])
        series_birth.append([int(r.get("births", 0)) for r in d])
    g = np.array(series_gen, dtype=float)
    b = np.array(series_birth, dtype=float)
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.4), dpi=DPI)
    fig.subplots_adjust(left=0.06, right=0.975, top=0.80, bottom=0.17, wspace=0.16)
    fig.text(0.06, 0.94, "How many generations does a 14-day run actually reach?", fontsize=15, fontweight="bold", va="top")
    fig.text(0.06, 0.885, f"{len(seeds)} scripted-brain runs of configs/demo.yaml (10 agents, drought day 4, arrival day 8, boom day 10). Thin lines are seeds, bold is the median.", fontsize=9.4, color=INK2, va="top")
    ax = axes[0]
    for row in g:
        ax.plot(days, row, color=BLUE, lw=1, alpha=0.35)
    ax.plot(days, np.median(g, axis=0), color=BLUE, lw=2.6)
    ax.set_title("Deepest generation alive or dead so far", fontsize=10.5, loc="left", pad=8)
    ax.set_xlabel("simulated day")
    ax.set_ylabel("generation")
    ax.set_ylim(0, max(4, g.max() + 0.5))
    ax = axes[1]
    for row in b:
        ax.plot(days, row, color=ORANGE, lw=1, alpha=0.35)
    ax.plot(days, np.median(b, axis=0), color=ORANGE, lw=2.6)
    for x, lab in ((4, "drought"), (8, "arrival"), (10, "boom")):
        ax.axvline(x, color=GRID, lw=1.4, zorder=0)
        ax.text(x + 0.1, b.max() * 0.96 if b.max() else 1, lab, fontsize=8.4, color=INK2, va="top")
    ax.text(1.35, b.max() * 0.93 if b.max() else 1, "day 1 counts\nthe ten founders", fontsize=8.2, color=INK2, va="top")
    ax.set_title("Births per day", fontsize=10.5, loc="left", pad=8)
    ax.set_xlabel("simulated day")
    ax.set_ylabel("births")
    tag(fig, "REAL", "scripted brains, so this bounds turnover under the current economy, not what live models would do")
    save(fig, "fig6_generation_depth.png")
    return {"median_final_generation": float(np.median(g[:, -1])), "max_final_generation": float(g[:, -1].max()), "runs": len(seeds)}


def fig_vault(run: Path):
    import networkx as nx
    db = sqlite3.connect(run / "world.db")
    db.row_factory = sqlite3.Row
    names = {r["agent_id"]: r["name"] for r in db.execute("SELECT agent_id, name FROM agents")}
    best, best_key = None, (-1, -1)
    for r in db.execute("SELECT agent_id, COUNT(*) n, COUNT(DISTINCT channel) c FROM notes WHERE archived=0 GROUP BY agent_id"):
        key = (int(r["c"]), int(r["n"]))
        if key > best_key:
            best, best_key = r["agent_id"], key
    notes = list(db.execute("SELECT note_id, title, channel, tags FROM notes WHERE agent_id=? AND archived=0", (best,)))
    by_title = {n["title"]: n["note_id"] for n in notes}
    G = nx.Graph()
    for n in notes:
        G.add_node(n["note_id"], title=n["title"], channel=n["channel"], entity="entity" in json.loads(n["tags"]))
    for r in db.execute("SELECT l.from_note_id f, l.to_title t FROM note_links l JOIN notes n ON n.note_id=l.from_note_id WHERE n.agent_id=?", (best,)):
        if r["t"] in by_title and r["f"] in G and by_title[r["t"]] != r["f"]:
            G.add_edge(r["f"], by_title[r["t"]])
    total_notes, total_edges = len(G), G.number_of_edges()
    isolated = [n for n in G if G.degree(n) == 0]
    G.remove_nodes_from(isolated)
    pos = nx.kamada_kawai_layout(G)
    palette = {"observed": BLUE, "gossip": ORANGE, "inherited": AQUA, "chronicle": YELLOW}
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 6.4), dpi=DPI, gridspec_kw=dict(width_ratios=[1.2, 1]))
    fig.subplots_adjust(left=0.03, right=0.975, top=0.80, bottom=0.14, wspace=0.10)
    fig.text(0.03, 0.94, "What a vault looks like today, from a real run", fontsize=15, fontweight="bold", va="top")
    fig.text(0.03, 0.885, f"Left: {names.get(best, best)}'s notes as a graph, dots are notes and lines are wikilinks ({len(isolated)} unlinked notes not drawn). Right: notes held by all agents, by how they arrived.", fontsize=9.2, color=INK2, va="top")
    ax = axes[0]
    ax.axis("off")
    ax.grid(False)
    nx.draw_networkx_edges(G, pos, ax=ax, edge_color=NEUTRAL_EDGE, width=0.8, alpha=0.8)
    for ch, col in palette.items():
        nodes = [n for n, d in G.nodes(data=True) if d["channel"] == ch and not d["entity"]]
        if nodes:
            nx.draw_networkx_nodes(G, pos, nodelist=nodes, node_color=col, node_size=[46 + 12 * G.degree(n) for n in nodes], ax=ax, edgecolors=SURFACE, linewidths=1.2, label=ch)
    ents = [n for n, d in G.nodes(data=True) if d["entity"]]
    if ents:
        nx.draw_networkx_nodes(G, pos, nodelist=ents, node_color=NEUTRAL_FILL, node_shape="s", node_size=[90 + 16 * G.degree(n) for n in ents], ax=ax, edgecolors=INK2, linewidths=1.3, label="entity stub (hub)")
    for n in sorted(ents, key=lambda n: -G.degree(n))[:7]:
        x, y = pos[n]
        ax.text(x, y - 0.075, G.nodes[n]["title"][:22], fontsize=7.8, ha="center", va="top", color=INK, bbox=dict(fc=SURFACE, ec="none", pad=0.7, alpha=0.9))
    ax.legend(loc="lower left", frameon=False, fontsize=8.4, labelcolor=INK2, markerscale=0.7)
    ax = axes[1]
    days = load_days(run)
    chans = [c for c in palette if any((r.get("notes_by_channel") or {}).get(c, 0) for r in days)]
    x = [int(r["day"]) for r in days]
    bottom = np.zeros(len(days))
    for ch in chans:
        vals = np.array([float((r.get("notes_by_channel") or {}).get(ch, 0)) for r in days])
        ax.bar(x, vals, bottom=bottom, color=palette[ch], width=0.72, label=ch, edgecolor=SURFACE, linewidth=1.5)
        bottom += vals
    ax.set_xlabel("simulated day")
    ax.set_ylabel("notes held (end of day)")
    ax.set_ylim(0, bottom.max() * 1.12)
    ax.legend(frameon=False, fontsize=8.4, labelcolor=INK2, loc="upper left")
    tag(fig, "REAL", "scripted brains, seed 42; live models write richer notes, but the structure and provenance fields are the same")
    save(fig, "fig7_vault_today.png")
    return {"agent": names.get(best, best), "notes": total_notes, "edges": total_edges, "unlinked": len(isolated), "channels": sorted({d['channel'] for _, d in G.nodes(data=True)})}


def fig_hypotheses():
    fig, axes = plt.subplots(1, 3, figsize=(14.5, 5.3), dpi=DPI)
    fig.subplots_adjust(left=0.05, right=0.985, top=0.79, bottom=0.17, wspace=0.16)
    fig.text(0.05, 0.94, "What each hypothesis predicts, and what would falsify it", fontsize=15, fontweight="bold", va="top")
    fig.text(0.05, 0.885, "Curves are drawn by hand to show the predicted shape. They are not data and carry no numbers.", fontsize=9.6, color=INK2, va="top")
    g = np.linspace(0, 10, 200)
    lx = 10.3
    ax = axes[0]
    ax.plot(g, 0.18 + 0.72 / (1 + np.exp(-(g - 4.6) * 0.85)), color=BLUE, lw=2.4)
    ax.plot(g, 0.18 + 0.36 / (1 + np.exp(-(g - 5.2) * 0.75)), color=ORANGE, lw=2.4)
    ax.plot(g, 0.18 + 0.10 * (1 - np.exp(-g / 2.5)), color=AQUA, lw=2.4)
    ax.plot(g, 0.18 + 0.02 * np.sin(g * 1.7), color=YELLOW, lw=2.4)
    ax.text(lx, 0.88, "transmission\n+ selection", fontsize=8.6, va="center")
    ax.text(lx, 0.55, "inheritance\nonly", fontsize=8.6, va="center")
    ax.text(lx, 0.31, "selection only,\nno self-edit", fontsize=8.6, va="center")
    ax.text(lx, 0.13, "no selection", fontsize=8.6, va="center")
    ax.set_title("H3  Practices ratchet up", fontsize=10.5, loc="left", pad=8)
    ax.set_xlabel("generation")
    ax.set_ylabel("lineage fitness (relative)")
    ax.set_xlim(0, 14.4)
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.set_xticks([0, 5, 10])
    ax = axes[1]
    ax.plot(g, 0.15 + 0.80 * (1 - np.exp(-g / 2.2)), color=BLUE, lw=2.4)
    ax.plot(g, 0.30 + 0.28 * (1 - np.exp(-g / 1.6)) - 0.30 * (1 / (1 + np.exp(-(g - 6.4) * 1.1))), color=ORANGE, lw=2.4)
    ax.plot(g, 0.30 + 0.30 * (1 - np.exp(-g / 4.5)), color=AQUA, lw=2.4)
    ax.text(lx, 0.95, "score on the\nplanted probes", fontsize=8.6, va="center")
    ax.text(lx, 0.30, "held-out score,\nproxy reward", fontsize=8.6, va="center")
    ax.text(lx, 0.62, "held-out score,\nsurvival only", fontsize=8.6, va="center")
    ax.set_title("H2  A proxy reward is gamed", fontsize=10.5, loc="left", pad=8)
    ax.set_xlabel("generation")
    ax.set_ylabel("score")
    ax.set_xlim(0, 14.4)
    ax.set_ylim(0, 1.05)
    ax.set_yticks([])
    ax.set_xticks([0, 5, 10])
    ax = axes[2]
    r = np.linspace(0, 4, 100)
    ax.plot(r, 0.15 + 0.7 * r / 4, color=BLUE, lw=2.4)
    ax.plot(r, 0.85 - 0.7 * r / 4, color=ORANGE, lw=2.4)
    ax.plot(r, 0.25 + 0.6 * np.sin(np.pi * r / 4), color=AQUA, lw=2.4)
    ax.axhline(0, color=NEUTRAL_EDGE, lw=1)
    ax.text(4.15, 0.85, "amplifier", fontsize=8.6, va="center")
    ax.text(4.15, 0.15, "equalizer", fontsize=8.6, va="center")
    ax.text(4.15, 0.27, "mid-tier\npeak", fontsize=8.6, va="center")
    ax.set_title("H4  Who benefits from self-directed memory", fontsize=10.5, loc="left", pad=8)
    ax.set_xlabel("capability rung")
    ax.set_ylabel("gain over default scaffold")
    ax.set_xlim(-0.1, 5.6)
    ax.set_ylim(-0.05, 1.05)
    ax.set_yticks([])
    ax.set_xticks([0, 2, 4])
    ax.set_xticklabels(["dim", "average", "genius"])
    fig.text(0.5, 0.5, "HYPOTHETICAL", fontsize=58, color=INK, alpha=0.04, ha="center", va="center", rotation=12, fontweight="bold")
    tag(fig, "HYPOTHETICAL", "predicted shapes only")
    save(fig, "fig8_hypothesis_patterns.png")


def fig_roadmap():
    fig, ax = plt.subplots(figsize=(13, 6.6), dpi=DPI)
    fig.subplots_adjust(left=0.29, right=0.975, top=0.80, bottom=0.15)
    fig.text(0.03, 0.94, "Roadmap: 26 weeks from today's repo to a submitted flagship paper", fontsize=15, fontweight="bold", va="top")
    fig.text(0.03, 0.885, "Assumes Claude implements and the owner spends about 15 to 20 hours a week on decisions, review and analysis. Bars are estimates, not commitments.", fontsize=9.6, color=INK2, va="top")
    tasks = [
        ("Prompt logging + Parquet export", 0, 2, BLUE), ("Trust model: DB-authoritative provenance", 1, 3, BLUE),
        ("Memory commands, quotas, audit log", 2, 5, BLUE), ("Policy file + nightly maintenance", 3, 6, BLUE),
        ("Metrics, probes, transplant harness", 5, 8, BLUE), ("Pilots on live models (Flash-Lite)", 8, 11, ORANGE),
        ("Main experiments E1 to E5", 11, 19, ORANGE), ("Transfer: external benchmarks + open weights", 17, 22, AQUA),
        ("Analysis, write-up, release", 20, 26, VIOLET),
    ]
    for i, (name, a, b, col) in enumerate(tasks):
        y = len(tasks) - 1 - i
        ax.barh(y, b - a, left=a, height=0.56, color=col, edgecolor=SURFACE, linewidth=1.5)
        ax.text(-0.5, y, name, ha="right", va="center", fontsize=9, color=INK)
    ax.set_yticks([])
    ax.set_xlim(0, 26)
    ax.set_xlabel("week")
    ax.set_xticks(range(0, 27, 2))
    ax.grid(axis="y", visible=False)
    for w, lab in ((8, "go / no-go on a live signal"), (19, "freeze designs, stop building")):
        ax.axvline(w, color=INK2, lw=1, ls=(0, (4, 3)))
        ax.text(w + 0.2, len(tasks) - 0.35, lab, fontsize=8.4, color=INK2, va="bottom")
    ax.set_ylim(-0.6, len(tasks) + 0.2)
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=BLUE, label="build"), Patch(color=ORANGE, label="run"), Patch(color=AQUA, label="generalize"), Patch(color=VIOLET, label="write")],
              frameon=False, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.11), fontsize=8.8, labelcolor=INK2)
    tag(fig, "SCHEMATIC", "planning estimate")
    save(fig, "fig9_roadmap.png")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, default=None, help="directory holding seed_*/ run directories (for the REAL figures)")
    ap.add_argument("--only", default="", help="comma list: arch,trust,loop,power,cost,gens,vault,hyp,road")
    args = ap.parse_args()
    only = set(filter(None, args.only.split(",")))
    def want(key: str) -> bool:
        return not only or key in only

    facts_path = HERE / "facts.json"
    facts: dict[str, object] = json.loads(facts_path.read_text()) if only and facts_path.exists() else {}
    steps = (
        ("arch", fig_architecture), ("trust", fig_trust), ("loop", fig_loop), ("hyp", fig_hypotheses), ("road", fig_roadmap),
    )
    for key, fn in steps:
        if want(key):
            fn()
    if want("power"):
        facts["power_seeds_for_80pct"] = fig_power()
    if want("cost"):
        facts["cost_table_usd"] = {k: [round(v, 3) for v in vs] for k, vs in fig_cost().items()}
    if args.runs is not None:
        if want("gens"):
            facts["generations"] = fig_generations(args.runs)
        if want("vault"):
            facts["vault"] = fig_vault(args.runs / "seed_42")
    facts_path.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n")
    print(json.dumps(facts, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
