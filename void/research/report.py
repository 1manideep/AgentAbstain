"""``void report PATH --out FILE``: one self-contained static HTML report of a run or an experiment
(MEMORY_EVOLUTION 5.10, "a static HTML report can be exported for a paper's supplement").

``PATH`` is detected (:func:`detect`): a directory holding ``world.db`` is a run; a directory whose arms hold runs
(``<arm>/seed_<n>/`` or ``<arm>/`` single runs, as :func:`void.research.compare.discover_runs` finds them) is an
experiment.

A **run report** (:func:`run_report`) holds

* the agents (tier, generation, status, balance) and :func:`void.research.analysis.summarize`, flattened to dotted keys;
* the mean balance and the population at the end of each day (the day's last tick row), as inline SVG charts;
* per agent, the memory-policy timeline (``policy_versions``): one row per version and a word-level diff of each
  version against the agent's previous one (:func:`word_diff_html`: :class:`difflib.SequenceMatcher` over the
  whitespace-separated words, deleted words in ``<del>`` and inserted ones in ``<ins>``, every word escaped). The
  word counts are those of ``void inspect --policy`` (:meth:`void.memory.policy.PolicyStore.timeline`);
* the exams per agent and per question kind (``exam_items`` / ``exam_answers``, one block per exam): graded items
  (an item whose paid call returned no usable answer is not graded, as in :func:`void.research.analysis.exam_counts`),
  correct, abstained, wrong, accuracy and abstention rate, and per kind the majority baseline;
* the maintenance spend per agent (``llm_calls`` with purpose ``maintain``): calls, real and world cost, the share of
  the agent's own call spend (the per-agent :func:`void.research.analysis.memory_spend_share`), memory commands
  accepted and refused, agent-nights skipped and the scripted strategy;
* the vault statistics of the metrics day rows (``vault``, ``vault_by_agent`` and the day row's ``link_density``);
* per agent, the titles of its most recent live notes (``--max-notes``, entity stubs left out).

An **experiment report** (:func:`experiment_report`) holds :func:`void.research.compare.compare`'s result: the runs
and their completeness, one PASS or FAIL row per pre-registered hypothesis (and the same lines as ``void compare``
prints), the paired statistics of the primary outcome and of every secondary (exploratory), per-arm means, the
per-seed values, the daily curves (the primary's from the result; each secondary's drawn the same way) as inline
SVG polylines, warnings and notes, and the full text rendering of ``void compare`` (which carries the 2x2 factorial
and the per-tier difference-in-differences when the design has them). A comparison ``compare`` refuses is refused
here too (``refused: <reason>`` on stderr, exit 2).

The output is one file: inline CSS, no ``<script>``, no external resource or URL, and every piece of text that
comes from a run (agent and model text, note titles, policy texts, names, configuration strings, paths) passes
through :func:`html.escape`. The report carries no timestamp, so the same run gives the same bytes.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from void.config import micro_to_usd
from void.research.analysis import DB_FILE, NOT_AGENT_PAID, connect, load_metrics, run_config, summarize

__all__ = [
    "MAX_NOTES",
    "PALETTE",
    "Html",
    "build_parser",
    "cli_main",
    "detect",
    "esc",
    "experiment_report",
    "run_report",
    "svg_curves",
    "word_diff_html",
]

MAX_NOTES = 40          # most recent live notes listed per agent in a run report
VAULT_COLUMNS = ("notes", "bytes", "mean_note_chars", "link_density", "orphan_rate", "index_coverage")
ENTITY_TAG = "entity"   # kernel link anchors (void.memory.store.ENTITY_TAG), not content
# Okabe-Ito without its yellow (too pale on white), then a grey; a series past the palette is dashed
PALETTE = ("#0072b2", "#d55e00", "#009e73", "#cc79a7", "#e69f00", "#56b4e9", "#888888")
_WORD = re.compile(r"(\S+)(\s*)")
_LEAD = re.compile(r"\s*")

CSS = """
:root{--fg:#1d1f23;--muted:#5d636b;--bg:#ffffff;--panel:#f4f5f7;--line:#d8dbe0;--accent:#0072b2;
--ins-bg:#dcf3e1;--ins-fg:#0b5a1e;--del-bg:#fbe0de;--del-fg:#8a1c1c;--pass:#0b6b2b;--fail:#a1260d}
@media (prefers-color-scheme: dark){:root{--fg:#e5e7ea;--muted:#a1a7af;--bg:#15171a;--panel:#1f2226;--line:#353a40;
--accent:#56b4e9;--ins-bg:#16391f;--ins-fg:#a3e6b4;--del-bg:#40191a;--del-fg:#f4a9a9;--pass:#70d38e;--fail:#ff8d72}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header,nav,main,footer{max-width:1180px;margin:0 auto;padding:0 16px}
header{padding-top:24px}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:19px;margin:32px 0 8px;padding-top:8px;border-top:1px solid var(--line)}
h3{font-size:15px;margin:20px 0 6px}
p{margin:6px 0}
.muted{color:var(--muted)}
nav ol{margin:12px 0 0;padding-left:20px;columns:2;column-gap:32px}
nav a,footer a{color:var(--accent)}
dl.meta{display:grid;grid-template-columns:max-content 1fr;gap:2px 16px;margin:12px 0}
dl.meta dt{color:var(--muted)}
dl.meta dd{margin:0;overflow-wrap:anywhere}
.scroll{overflow-x:auto;margin:6px 0 12px}
table{border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:3px 10px 3px 0;text-align:left;vertical-align:top}
th{font-weight:600;color:var(--muted);white-space:nowrap}
td.num{text-align:right;white-space:nowrap}
tr.total td{font-weight:600}
.text{white-space:pre-wrap;overflow-wrap:anywhere;font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
background:var(--panel);padding:8px 10px;border-radius:4px;margin:4px 0 10px}
ins{background:var(--ins-bg);color:var(--ins-fg);text-decoration:none}
del{background:var(--del-bg);color:var(--del-fg);margin-right:.25em}
details{margin:4px 0}
summary{cursor:pointer}
.pass{color:var(--pass);font-weight:700}
.fail{color:var(--fail);font-weight:700}
.chart{margin:12px 0 20px}
.chart-title{font-weight:600;margin:0 0 4px}
.chart svg{max-width:100%;height:auto;display:block}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:13px}
.swatch{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:6px}
footer{margin-top:40px;padding-bottom:32px;color:var(--muted);font-size:12.5px}
@media print{nav{display:none}h2{break-after:avoid}.text{white-space:pre-wrap}}
"""


# --- escaping and small HTML helpers ---------------------------------------------------------------------------
class Html(str):
    """Markup built here from escaped parts; :func:`esc` passes it through unchanged. Anything else is text."""


def _text(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return "n/a" if not math.isfinite(value) else f"{value:.6g}"
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def esc(value: Any) -> Html:
    """``value`` as safe HTML: :class:`Html` unchanged, anything else formatted (None is ``n/a``, floats to six
    significant digits, containers as JSON) and passed through :func:`html.escape` with quotes."""
    if isinstance(value, Html):
        return value
    return Html(html.escape(_text(value), quote=True))


def _usd(micro: Any) -> str:
    return f"{micro_to_usd(int(micro or 0)):.6f}"


def _rate(num: float, den: float) -> float | None:
    return num / den if den else None


def _is_num(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


_NUMERIC = re.compile(r"[+-]?\d+(\.\d+)?")


def _numeric_cell(value: Any) -> bool:
    """Right-align numbers and numbers already formatted as text (dollar amounts)."""
    return _is_num(value) or (isinstance(value, str) and not isinstance(value, Html) and _NUMERIC.fullmatch(value) is not None)


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]], *, total: Sequence[Any] | None = None) -> Html:
    """A table whose cells are escaped unless already :class:`Html`; numbers are right-aligned."""
    def row(cells: Sequence[Any], cls: str = "") -> str:
        tds = "".join(f'<td class="num">{esc(c)}</td>' if _numeric_cell(c) else f"<td>{esc(c)}</td>" for c in cells)
        return f'<tr class="{cls}">{tds}</tr>' if cls else f"<tr>{tds}</tr>"

    body = [row(r) for r in rows]
    if total is not None:
        body.append(row(total, "total"))
    if not body:
        return Html('<p class="muted">(none)</p>')
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    return Html(f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


def _meta(items: Iterable[tuple[str, Any]]) -> Html:
    return Html('<dl class="meta">' + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in items) + "</dl>")


def _p(text: Any, cls: str = "muted") -> Html:
    return Html(f'<p class="{cls}">{esc(text)}</p>')


def _section(sid: str, title: str, *parts: str) -> tuple[str, str, Html]:
    return sid, title, Html(f'<section id="{esc(sid)}"><h2>{esc(title)}</h2>{"".join(parts)}</section>')


def _page(title: str, subtitle: str, meta: Html, sections: Sequence[tuple[str, str, Html]], footer: str) -> str:
    nav = "".join(f'<li><a href="#{esc(sid)}">{esc(t)}</a></li>' for sid, t, _ in sections)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="generator" content="void report">\n'
        f"<title>{esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
        f"<header><h1>{esc(title)}</h1>{_p(subtitle)}{meta}</header>\n"
        f"<nav><ol>{nav}</ol></nav>\n<main>\n" + "\n".join(body for _, _, body in sections) + "\n</main>\n"
        f"<footer>{esc(footer)}</footer>\n</body>\n</html>\n"
    )


def _flatten(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Nested dicts as ``(dotted.key, value)`` pairs in their own order; an empty dict stays one row."""
    if isinstance(obj, Mapping) and obj:
        out: list[tuple[str, Any]] = []
        for k, v in obj.items():
            out.extend(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return [(prefix, obj)]


# --- word diff ----------------------------------------------------------------------------------------------------
def _words(text: str) -> tuple[str, list[tuple[str, str]]]:
    """``(leading whitespace, [(word, whitespace after it), ...])``: the words are ``text.split()``."""
    lead = _LEAD.match(text)
    return (lead.group(0) if lead else ""), _WORD.findall(text)


def word_diff_html(old: str, new: str) -> tuple[Html, int, int]:
    """``(html, words_added, words_removed)``: ``new`` with the words that changed from ``old`` marked.

    Words are compared as :meth:`void.memory.policy.PolicyStore.timeline` compares them (``SequenceMatcher`` over
    ``split()``, no autojunk), so the counts are the same. Unchanged words keep the new text's spacing; a deleted run
    of words is ``<del>`` (with the old text's spacing inside it), an inserted run ``<ins>``; the whitespace after
    the last word of a run stays outside its tag. Every word and space is escaped."""
    _, a = _words(old)
    lead, b = _words(new)
    out: list[str] = [html.escape(lead)]
    added = removed = 0

    def run(tokens: Sequence[tuple[str, str]]) -> tuple[str, str]:
        text = "".join(w + s for w, s in tokens)
        body = text.rstrip()
        return html.escape(body), html.escape(text[len(body):])

    sm = difflib.SequenceMatcher(a=[w for w, _ in a], b=[w for w, _ in b], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.append(html.escape("".join(w + s for w, s in b[j1:j2])))
            continue
        if i2 > i1:
            removed += i2 - i1
            body, tail = run(a[i1:i2])
            out.append(f"<del>{body}</del>" + ("" if j2 > j1 else tail))
        if j2 > j1:
            added += j2 - j1
            body, tail = run(b[j1:j2])
            out.append(f"<ins>{body}</ins>{tail}")
    return Html("".join(out)), added, removed


# --- charts -------------------------------------------------------------------------------------------------------
def _tick_label(v: float) -> str:
    return f"{v:.4g}"


def svg_curves(curves: Mapping[str, Mapping[Any, Any]], *, label: str, x_label: str = "day") -> Html:
    """An inline SVG line chart: one ``<polyline>`` per series (``{name: {day: value}}``; a day key may be a string,
    a None or non-finite value is skipped), a marker with a ``<title>`` per point, the y range from the data, and an
    HTML legend. No script, no external reference, every name escaped."""
    series: dict[str, list[tuple[float, float]]] = {}
    for name, pts in curves.items():
        clean = sorted((float(d), float(v)) for d, v in pts.items() if _is_num(v) and math.isfinite(float(v)))
        if clean:
            series[str(name)] = clean
    if not series:
        return _p(f"{label}: no daily values")
    xs = sorted({x for pts in series.values() for x, _ in pts})
    ys = [y for pts in series.values() for _, y in pts]
    lo, hi = min(ys), max(ys)
    if hi == lo:
        pad = abs(lo) * 0.05 or 1.0
        lo, hi = lo - pad, hi + pad
    w, h, left, right, top, bottom = 720, 260, 70, 16, 14, 40
    pw, ph = w - left - right, h - top - bottom
    x0, x1 = xs[0], xs[-1]

    def px(x: float) -> float:
        return left + (pw / 2 if x1 == x0 else (x - x0) / (x1 - x0) * pw)

    def py(y: float) -> float:
        return top + (hi - y) / (hi - lo) * ph

    parts = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{esc(label)}">',
             f"<title>{esc(label)}</title>",
             '<g fill="currentColor" font-size="11" font-family="system-ui,sans-serif">']
    for i in range(5):  # horizontal grid and y labels
        yv = lo + (hi - lo) * i / 4
        y = py(yv)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{w - right}" y2="{y:.1f}" stroke="currentColor" stroke-opacity="0.15"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{esc(_tick_label(yv))}</text>')
    step = max(1, math.ceil(len(xs) / 12))
    for x in xs[::step]:
        parts.append(f'<line x1="{px(x):.1f}" y1="{top + ph}" x2="{px(x):.1f}" y2="{top + ph + 4}" stroke="currentColor" stroke-opacity="0.5"/>')
        parts.append(f'<text x="{px(x):.1f}" y="{top + ph + 17}" text-anchor="middle">{esc(_tick_label(x))}</text>')
    parts.append(f'<line x1="{left}" y1="{top + ph}" x2="{w - right}" y2="{top + ph}" stroke="currentColor" stroke-opacity="0.5"/>')
    parts.append(f'<text x="{left + pw / 2:.1f}" y="{h - 4}" text-anchor="middle">{esc(x_label)}</text></g>')
    legend: list[str] = []
    for i, (name, pts) in enumerate(series.items()):
        color = PALETTE[i % len(PALETTE)]
        dash = ' stroke-dasharray="6 3"' if i >= len(PALETTE) else ""
        points = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in pts)
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"{dash}>'
                     f"<title>{esc(name)}</title></polyline>")
        for x, y in pts:
            parts.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="2.8" fill="{color}">'
                         f"<title>{esc(name)}, {esc(x_label)} {esc(_tick_label(x))}: {esc(y)}</title></circle>")
        legend.append(f'<span><span class="swatch" style="background:{color}"></span>{esc(name)}</span>')
    parts.append("</svg>")
    return Html(f'<figure class="chart"><p class="chart-title">{esc(label)}</p>{"".join(parts)}'
                f'<figcaption class="legend">{"".join(legend)}</figcaption></figure>')


# --- the run report -----------------------------------------------------------------------------------------------
def _tables(db: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _day_of(tick: int, ticks_per_day: int) -> int:
    return 0 if tick <= 0 else (int(tick) - 1) // max(1, ticks_per_day) + 1


def _agent_label(agents: Mapping[str, Mapping[str, Any]], agent_id: Any) -> str:
    a = agents.get(str(agent_id))
    return f"{a['name']} [{agent_id}]" if a else str(agent_id)


def _daily_end_values(tick_rows: Sequence[Mapping[str, Any]], key: str) -> dict[int, Any]:
    last: dict[int, Mapping[str, Any]] = {}
    for r in tick_rows:
        last[int(r.get("day", 0))] = r
    return {d: r.get(key) for d, r in sorted(last.items())}


def _policy_section(db: sqlite3.Connection, agents: Mapping[str, Mapping[str, Any]], tpd: int, have: set[str]) -> list[str]:
    if "policy_versions" not in have:
        return [_p("This run has no policy_versions table (made before the memory layer).")]
    rows = db.execute("SELECT agent_id, version, tick, text, parent_version, source, source_agent_id, similarity_to_parent "
                      "FROM policy_versions ORDER BY agent_id, version").fetchall()
    if not rows:
        return [_p("No policy versions: memory.policy.enabled was false.")]
    by_agent: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_agent.setdefault(str(r["agent_id"]), []).append(r)
    order = [aid for aid in agents if aid in by_agent] + sorted(a for a in by_agent if a not in agents)
    out = [_p("One block per agent, in order of birth. Each version after the first is shown as a word-level diff "
              "against the agent's previous version: deleted words struck through, inserted words highlighted.")]
    for aid in order:
        versions = by_agent[aid]
        a = agents.get(aid, {})
        head = f"{_agent_label(agents, aid)}: tier {a.get('model_tier', '?')}, generation {a.get('generation', '?')}, {a.get('status', '?')}"
        table_rows: list[list[Any]] = []
        blocks: list[str] = []
        prev = ""
        for i, v in enumerate(versions):
            text = str(v["text"])
            diff, added, removed = word_diff_html(prev, text)
            via = _agent_label(agents, v["source_agent_id"]) if v["source_agent_id"] else ""
            table_rows.append([int(v["version"]), int(v["tick"]), _day_of(int(v["tick"]), tpd), str(v["source"]), via,
                               v["similarity_to_parent"], len(text), len(text.split()), added, removed])
            title = (f"v{v['version']} at tick {v['tick']} ({v['source']}{', from ' + via if via else ''}): "
                     f"{len(text)} chars, +{added} -{removed} words")
            if i == 0:
                blocks.append(f"<details open><summary>{esc(title)}</summary><div class=\"text\">{esc(text)}</div></details>")
            else:
                blocks.append(f"<details open><summary>{esc(title)}</summary><div class=\"text\">{diff}</div></details>")
            prev = text
        out.append(f"<h3>{esc(head)}</h3>")
        out.append(_table(["version", "tick", "day", "source", "from", "similarity to parent", "chars", "words",
                           "+words", "-words"], table_rows))
        out.extend(blocks)
        if len(versions) > 1:
            out.append(f"<details><summary>current text (v{esc(versions[-1]['version'])})</summary>"
                       f"<div class=\"text\">{esc(str(versions[-1]['text']))}</div></details>")
    return out


def _graded(alias: str = "a") -> str:
    """SQL: the answer measured something (not a paid call that returned no usable answer)."""
    return f"NOT ({alias}.call_id IS NOT NULL AND {alias}.answer IS NULL)"


def _exam_section(db: sqlite3.Connection, agents: Mapping[str, Mapping[str, Any]], have: set[str]) -> list[str]:
    from void.research.exams import normalise

    if not {"exam_items", "exam_answers"} <= have:
        return [_p("This run has no exam tables (made before the memory layer).")]
    exams = [str(r[0]) for r in db.execute("SELECT DISTINCT i.exam FROM exam_items i JOIN exam_answers a ON a.item_id=i.item_id "
                                             "ORDER BY i.exam")]
    if not exams:
        return [_p("No exam answers: memory.exam.enabled was false or no day was old enough.")]
    out = [_p("Graded items exclude an item whose paid call returned no usable answer (counted as failed). "
              "Accuracy is correct / graded and abstention abstained / graded; wrong answers are the rest.")]
    sums = ("COUNT(*) AS items, SUM(a.call_id IS NOT NULL AND a.answer IS NULL) AS failed, "
            f"SUM(CASE WHEN {_graded()} THEN a.correct ELSE 0 END) AS correct, "
            f"SUM(CASE WHEN {_graded()} THEN a.abstained ELSE 0 END) AS abstained")

    def cells(r: sqlite3.Row) -> list[Any]:
        failed = int(r["failed"] or 0)
        graded, correct, abstained = int(r["items"]) - failed, int(r["correct"] or 0), int(r["abstained"] or 0)
        return [graded, correct, abstained, graded - correct - abstained, failed,
                _rate(correct, graded), _rate(abstained, graded)]

    headers = ["graded", "correct", "abstained", "wrong", "failed", "accuracy", "abstention"]
    for exam in exams:
        out.append(f"<h3>{esc(exam)} exam</h3>")
        per_agent = db.execute(f"SELECT i.agent_id AS agent_id, {sums} FROM exam_items i JOIN exam_answers a ON a.item_id=i.item_id "
                               "WHERE i.exam=? GROUP BY i.agent_id", (exam,)).fetchall()
        order = {aid: n for n, aid in enumerate(agents)}
        per_agent = sorted(per_agent, key=lambda r: (order.get(str(r["agent_id"]), len(order)), str(r["agent_id"])))
        total = db.execute(f"SELECT {sums} FROM exam_items i JOIN exam_answers a ON a.item_id=i.item_id WHERE i.exam=?",
                           (exam,)).fetchone()
        out.append("<p>Per agent</p>")
        out.append(_table(["agent", "tier", *headers],
                          [[_agent_label(agents, r["agent_id"]), agents.get(str(r["agent_id"]), {}).get("model_tier", "?"), *cells(r)]
                           for r in per_agent], total=["all agents", "", *cells(total)]))
        truths: dict[str, Counter[str]] = {}
        for r in db.execute(f"SELECT i.kind AS kind, i.truth AS truth FROM exam_items i JOIN exam_answers a ON a.item_id=i.item_id "
                            f"WHERE i.exam=? AND {_graded()}", (exam,)):
            truths.setdefault(str(r["kind"]), Counter())[normalise(str(r["truth"]))] += 1
        per_kind = db.execute(f"SELECT i.kind AS kind, {sums} FROM exam_items i JOIN exam_answers a ON a.item_id=i.item_id "
                              "WHERE i.exam=? GROUP BY i.kind ORDER BY i.kind", (exam,)).fetchall()
        out.append("<p>Per question kind (majority baseline: the accuracy of always giving the kind's commonest truth)</p>")
        out.append(_table(["kind", *headers, "majority baseline"],
                          [[str(r["kind"]), *cells(r),
                            _rate(max(truths[str(r["kind"])].values()), sum(truths[str(r["kind"])].values()))
                            if truths.get(str(r["kind"])) else None] for r in per_kind]))
    return out


def _maintenance_section(db: sqlite3.Connection, agents: Mapping[str, Mapping[str, Any]], have: set[str],
                         payer: str) -> list[str]:
    calls = {str(r["agent_id"]): r for r in db.execute(
        "SELECT agent_id, COUNT(*) AS n, COALESCE(SUM(real_cost),0) AS rc, COALESCE(SUM(world_cost),0) AS wc "
        "FROM llm_calls WHERE purpose='maintain' GROUP BY agent_id")}
    marks = ",".join("?" * len(NOT_AGENT_PAID))
    own = {str(r["agent_id"]): int(r["rc"]) for r in db.execute(
        f"SELECT agent_id, COALESCE(SUM(real_cost),0) AS rc FROM llm_calls WHERE purpose NOT IN ({marks}) GROUP BY agent_id",
        NOT_AGENT_PAID)}
    strategies: dict[str, set[str]] = {}
    for r in db.execute("SELECT agent_id, extras FROM llm_calls WHERE purpose='maintain'"):
        try:
            s = (json.loads(r["extras"]) or {}).get("strategy") if r["extras"] else None
        except (TypeError, ValueError):
            s = None
        if s:
            strategies.setdefault(str(r["agent_id"]), set()).add(str(s))
    commands: dict[str, tuple[int, int]] = {}
    if "memory_commands" in have:
        commands = {str(r["agent_id"]): (int(r["ok"] or 0), int(r["n"]) - int(r["ok"] or 0)) for r in db.execute(
            "SELECT agent_id, COUNT(*) AS n, SUM(ok) AS ok FROM memory_commands WHERE purpose='maintain' GROUP BY agent_id")}
    skipped = {str(r["agent_id"]): int(r["n"]) for r in db.execute(
        "SELECT agent_id, COUNT(*) AS n FROM events WHERE kind='maintenance_skipped' GROUP BY agent_id")}
    ids = list(agents) + sorted({*calls, *commands, *skipped} - set(agents))
    if not calls and not commands and not skipped:
        return [_p("No maintenance calls: memory.maintenance.enabled was false.")]
    rows: list[list[Any]] = []
    tot = {"n": 0, "rc": 0, "wc": 0, "own": 0, "ok": 0, "ref": 0, "skip": 0}
    for aid in ids:
        c = calls.get(aid)
        n, rc, wc = (int(c["n"]), int(c["rc"]), int(c["wc"])) if c else (0, 0, 0)
        ok, ref = commands.get(aid, (0, 0))
        sk = skipped.get(aid, 0)
        rows.append([_agent_label(agents, aid), agents.get(aid, {}).get("model_tier", "?"), ", ".join(sorted(strategies.get(aid, ()))),
                     n, _usd(rc), _usd(wc), _rate(rc, own.get(aid, 0)), ok, ref, sk])
        for k, v in (("n", n), ("rc", rc), ("wc", wc), ("own", own.get(aid, 0)), ("ok", ok), ("ref", ref), ("skip", sk)):
            tot[k] += v
    return [_p(f"Calls with purpose maintain, paid by: {payer}. Share is the maintenance real cost over the real cost of "
               "the agent's own calls (decide, maintain, gossip; not exam or chronicle): memory_spend_share per agent."),
            _table(["agent", "tier", "strategy", "calls", "real cost USD", "world cost USD", "share of own call cost",
                    "commands ok", "commands refused", "nights skipped"], rows,
                   total=["all agents", "", "", tot["n"], _usd(tot["rc"]), _usd(tot["wc"]), _rate(tot["rc"], tot["own"]),
                          tot["ok"], tot["ref"], tot["skip"]])]


def _vault_section(day_rows: Sequence[Mapping[str, Any]], agents: Mapping[str, Mapping[str, Any]]) -> list[str]:
    rows = [r for r in day_rows if isinstance(r.get("vault"), Mapping)]
    if not rows:
        return [_p("No day row carries vault statistics (the run ended before its first day row, or predates them).")]
    out = [_p("From the metrics day rows. Notes are live notes without entity stubs; link density is resolved links per "
              "note; orphan rate the share of notes without a resolved link either way; index coverage the share of "
              "non-index notes within two hops of an index note. The last column is the day row's own link_density "
              "(DESIGN 10: outgoing link rows per live note, averaged over living agents)."),
           "<h3>Population, per day</h3>",
           _table(["day", "living", *VAULT_COLUMNS, "link_density (day row)"],
                  [[int(r.get("day", 0)), r.get("population_end"), *(r["vault"].get(k) for k in VAULT_COLUMNS), r.get("link_density")]
                   for r in rows])]
    days = [int(r.get("day", 0)) for r in rows]
    seen: list[str] = []
    for r in rows:
        for aid in (r.get("vault_by_agent") or {}):
            if aid not in seen:
                seen.append(aid)
    order = [a for a in agents if a in seen] + sorted(a for a in seen if a not in agents)
    if order:
        out.append("<h3>Notes per agent, per day</h3>")
        out.append(_table(["agent", *(f"day {d}" for d in days)],
                          [[_agent_label(agents, aid), *((r.get("vault_by_agent") or {}).get(aid, {}).get("notes") for r in rows)]
                           for aid in order]))
        last = rows[-1].get("vault_by_agent") or {}
        out.append(f"<h3>Per agent, day {esc(days[-1])}</h3>")
        out.append(_table(["agent", *VAULT_COLUMNS],
                          [[_agent_label(agents, aid), *(last[aid].get(k) for k in VAULT_COLUMNS)] for aid in order if aid in last]))
    return out


def _notes_section(db: sqlite3.Connection, agents: Mapping[str, Mapping[str, Any]], max_notes: int) -> list[str]:
    by_agent: dict[str, list[sqlite3.Row]] = {}
    stubs: Counter[str] = Counter()
    for r in db.execute("SELECT agent_id, title, created_tick, channel, tags, hop FROM notes WHERE archived=0 "
                        "ORDER BY agent_id, created_tick, note_id"):
        try:
            tags = json.loads(r["tags"] or "[]")
        except ValueError:
            tags = []
        if isinstance(tags, list) and ENTITY_TAG in tags:
            stubs[str(r["agent_id"])] += 1
            continue
        by_agent.setdefault(str(r["agent_id"]), []).append(r)
    if not by_agent:
        return [_p("No live notes.")]
    out = [_p(f"Live notes without entity stubs, the {max_notes} most recent per agent (oldest first).")]
    order = [a for a in agents if a in by_agent] + sorted(a for a in by_agent if a not in agents)
    for aid in order:
        notes = by_agent[aid]
        shown = notes[-max_notes:] if max_notes > 0 else []
        rows = []
        for r in shown:
            try:
                tags = json.loads(r["tags"] or "[]")
            except ValueError:
                tags = r["tags"]
            rows.append([int(r["created_tick"]), str(r["title"]), str(r["channel"]), int(r["hop"] or 0),
                         ", ".join(map(str, tags)) if isinstance(tags, list) else tags])
        summary = f"{_agent_label(agents, aid)}: {len(notes)} notes ({stubs.get(aid, 0)} entity stubs not listed)"
        if len(shown) < len(notes):
            summary += f", {len(notes) - len(shown)} older notes not listed"
        out.append(f"<details><summary>{esc(summary)}</summary>"
                   f"{_table(['created tick', 'title', 'channel', 'hop', 'tags'], rows)}</details>")
    return out


def run_report(run_dir: str | Path, *, max_notes: int = MAX_NOTES) -> str:
    """The HTML report of one run directory (see the module docstring). Reads the run; writes nothing."""
    run_dir = Path(run_dir)
    cfg = run_config(run_dir)
    tpd = int(cfg.run.ticks_per_day)
    summary = summarize(run_dir)
    tick_rows, day_rows = load_metrics(run_dir)
    with connect(run_dir) as db:
        have = _tables(db)
        run = db.execute("SELECT * FROM run LIMIT 1").fetchone()
        agents: dict[str, dict[str, Any]] = {
            str(r["agent_id"]): dict(r) for r in db.execute(
                "SELECT agent_id, name, model_tier, generation, status, parent_id, born_tick, died_tick, balance "
                "FROM agents ORDER BY born_tick, agent_id")}
        policy = _policy_section(db, agents, tpd, have)
        exams = _exam_section(db, agents, have)
        maintenance = _maintenance_section(db, agents, have, cfg.memory.maintenance.payer)
        notes = _notes_section(db, agents, max_notes)
    agent_rows = [[a["name"], aid, a["model_tier"], a["generation"], a["status"],
                   _agent_label(agents, a["parent_id"]) if a["parent_id"] else "", a["born_tick"], a["died_tick"],
                   f"{micro_to_usd(int(a['balance'])):.6f}"] for aid, a in agents.items()]
    daily = [svg_curves({"mean balance": _daily_end_values(tick_rows, "mean_balance")}, label="mean balance (USD) at the end of each day"),
             svg_curves({"population": _daily_end_values(tick_rows, "population")}, label="living agents at the end of each day")]
    sections = [
        _section("agents", "Agents", _table(["name", "agent id", "tier", "generation", "status", "parent", "born tick",
                                             "died tick", "balance USD"], agent_rows)),
        _section("summary", "Summary", _p("void analyze (void.research.analysis.summarize), nested keys flattened."),
                 _table(["key", "value"], [[k, esc(v)] for k, v in _flatten(summary)])),
        _section("daily", "Daily", *daily),
        _section("policy", "Memory policy timelines", *policy),
        _section("exams", "Exams", *exams),
        _section("maintenance", "Maintenance spend per agent", *maintenance),
        _section("vault", "Vault statistics", *_vault_section(day_rows, agents)),
        _section("notes", "Notes per agent", *notes),
    ]
    run_id = run["run_id"] if run else summary.get("run_id")
    meta = _meta([
        ("run id", run_id), ("experiment", summary.get("experiment")), ("arm", summary.get("arm")),
        ("seed", summary.get("seed")), ("status", run["status"] if run else None),
        ("ended", f"tick {run['ended_tick']} ({run['ended_reason']})" if run else None),
        ("length", f"{cfg.run.days} days x {tpd} ticks"), ("config hash", run["config_hash"] if run else None),
        ("directory", run_dir.name),
    ])
    return _page(f"Run report: {run_id}", f"{cfg.run.name}: one run of The Void", meta, sections,
                 "Written by void report from the run's world.db, metrics.jsonl and config.resolved.yaml. A static file: "
                 "inline styles, no scripts, no external resources; all agent and model text is escaped.")


# --- the experiment report ----------------------------------------------------------------------------------------
def _stat_cells(s: Mapping[str, Any]) -> list[Any]:
    lo, hi = s.get("ci95") or [None, None]
    return [s.get("n"), s.get("dropped", 0), s.get("mean_a"), s.get("mean_b"), s.get("delta"), s.get("d_z"),
            f"[{_text(lo)}, {_text(hi)}]", s.get("sign_test_p"), f"+{s.get('positive', 0)} -{s.get('negative', 0)} ={s.get('ties', 0)}"]


STAT_HEADERS = ["n", "dropped", "mean a", "mean b", "delta (b - a)", "d_z", "CI95", "sign p", "+ - ="]


def _hypotheses_section(result: Mapping[str, Any]) -> list[str]:
    from void.research.compare import render_hypotheses

    hyps = result.get("hypotheses") or []
    if not hyps:
        return [_p("No pre-registered hypotheses (experiment.hypotheses is empty).")]
    rows: list[list[Any]] = []
    for h in hyps:
        verdict = Html('<span class="pass">PASS</span>') if h.get("passed") else Html('<span class="fail">FAIL</span>')
        kind = h["kind"] if h["kind"] == "equivalence" else f"{h['kind']} {h['direction']}"
        margin = f"{_text(h.get('margin'))}{' SD' if h.get('margin_unit') == 'sd' else ''}"
        lo, hi = h.get("ci") or [None, None]
        level = round(100 * float(h.get("ci_level") or 0.95))
        if h["kind"] == "interaction" and h.get("levels"):  # the same contrast wording as compare.render_hypotheses
            contrast = f"({h['arm_b']} - {h['arm_a']}) at each level - at {h['reference']}"
        elif h["kind"] == "interaction":
            contrast = f"({h['arm_d']} - {h['arm_c']}) - ({h['arm_b']} - {h['arm_a']})"
        else:
            contrast = f"{h['arm_b']} - {h['arm_a']}"
        rows.append([verdict, h["name"], kind, margin, "primary" if h.get("primary") else "exploratory",
                     contrast, h["outcome"], h.get("n"), h.get("delta"),
                     f"CI{level} [{_text(lo)}, {_text(hi)}]", h.get("d_z"), h.get("power"), h.get("p"), h.get("p_holm"),
                     h.get("reason")])
    lines = render_hypotheses(dict(result)).splitlines()
    return [_table(["verdict", "hypothesis", "kind", "margin", "role", "contrast", "outcome", "n", "delta", "interval",
                    "d_z", "power", "p", "p Holm", "reason"], rows),
            "<p>As void compare prints them:</p>",
            f'<div class="text">{esc(chr(10).join(lines))}</div>']


def experiment_report(exp_dir: str | Path, *, primary: str | None = None, allow_incomplete: bool = False) -> str:
    """The HTML report of an experiment directory (see the module docstring); raises
    :class:`void.research.compare.IncomparableRuns` when the comparison is refused."""
    from void.research.compare import _daily_curves, compare, discover_runs, render

    exp_dir = Path(exp_dir)
    result = compare(exp_dir, primary=primary, allow_incomplete=allow_incomplete)
    arms: list[str] = list(result["arms"])
    paths = [result["primary"], *result["secondaries"]]
    values: Mapping[str, Mapping[str, Mapping[str, Any]]] = result["values"]

    run_rows = []
    for arm in arms:
        for seed, st in (result.get("runs") or {}).get(arm, {}).items():
            dropped = any(d.get("arm") == arm and str(d.get("seed")) == str(seed) for d in result.get("dropped_incomplete") or [])
            run_rows.append([arm, int(seed), st.get("status"), st.get("last_tick"), "dropped (incomplete)" if dropped else "used"])

    comparisons = result.get("comparisons") or []
    primary_rows = [[c["arm_a"], c["arm_b"], *_stat_cells(c["primary"])] for c in comparisons]
    secondary_rows = [[s["path"], c["arm_a"], c["arm_b"], *_stat_cells(s)] for c in comparisons for s in c.get("secondary") or []]

    mean_rows = []
    for path in paths:
        row: list[Any] = [path]
        for arm in arms:
            vs = [float(v[path]) for v in values.get(arm, {}).values() if _is_num(v.get(path))]
            row.append(f"{_text(sum(vs) / len(vs))} (n={len(vs)})" if vs else "n/a (n=0)")
        mean_rows.append(row)
    seed_rows = [[arm, int(seed), *(row.get(p) for p in paths)] for arm in arms for seed, row in values.get(arm, {}).items()]

    curves = [svg_curves(result.get("curves") or {}, label=f"{result['primary']} per day, mean over seeds (primary)")]
    if result["secondaries"]:
        runs = discover_runs(exp_dir)
        complete = {arm: sorted(int(s) for s in values.get(arm, {})) for arm in arms}
        for path in result["secondaries"]:
            curves.append(svg_curves(_daily_curves(runs, arms, complete, path), label=f"{path} per day, mean over seeds (secondary)"))

    notes = [*(f"warning: {w}" for w in result.get("warnings") or []), *(f"note: {n}" for n in result.get("notes") or [])]
    if result.get("degenerate_pair") is not None:
        notes.append(f"degenerate_pair: {result['degenerate_pair']}")
    sections = [
        _section("runs", "Runs", _table(["arm", "seed", "status", "last tick", "in the statistics"], run_rows),
                 f'<div class="text">{esc(chr(10).join(notes))}</div>' if notes else ""),
        _section("hypotheses", "Pre-registered hypotheses", *_hypotheses_section(result)),
        _section("primary", f"Primary outcome: {result['primary']}",
                 _p("Paired by seed; delta is arm b minus arm a, the CI a paired bootstrap, the p an exact sign test."),
                 _table(["arm a", "arm b", *STAT_HEADERS], primary_rows)),
        _section("secondary", "Secondary outcomes (exploratory)",
                 _table(["outcome", "arm a", "arm b", *STAT_HEADERS], secondary_rows) if secondary_rows
                 else _p("No secondary outcomes.")),
        _section("means", "Per-arm means", _table(["outcome", *arms], mean_rows)),
        _section("curves", "Daily curves", _p("Mean over each arm's complete seeds, per day: shown, not tested."), *curves),
        _section("values", "Per-seed values", _table(["arm", "seed", *paths], seed_rows)),
        _section("text", "Text form (void compare)", f'<div class="text">{esc(render(dict(result)))}</div>'),
    ]
    meta = _meta([
        ("experiment", result["experiment"]), ("directory", exp_dir.name), ("arms", ", ".join(arms)),
        ("seeds", ", ".join(map(str, result["seeds"]))), ("independent variables", ", ".join(result["independent_variables"]) or "(none)"),
        ("primary", result["primary"]), ("secondaries", ", ".join(result["secondaries"]) or "(none)"), ("alpha", result.get("alpha")),
    ])
    n_pass = sum(1 for h in result.get("hypotheses") or [] if h.get("passed"))
    n_hyp = len(result.get("hypotheses") or [])
    return _page(f"Experiment report: {result['experiment']}",
                 f"{len(arms)} arms x {result['n_seeds']} seeds; {n_pass} of {n_hyp} pre-registered hypotheses pass",
                 meta, sections,
                 "Written by void report from void.research.compare. A static file: inline styles, no scripts, "
                 "no external resources; all agent and model text is escaped.")


# --- the command --------------------------------------------------------------------------------------------------
def detect(path: str | Path) -> str | None:
    """``"run"`` for a directory holding ``world.db``, ``"experiment"`` for one whose arms hold runs, else None."""
    from void.research.compare import IncomparableRuns, discover_runs

    path = Path(path)
    if (path / DB_FILE).is_file():
        return "run"
    if not path.is_dir():
        return None
    try:
        return "experiment" if discover_runs(path) else None
    except IncomparableRuns:
        return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="void report",
        description="Write a self-contained static HTML report (inline CSS, no scripts, no external resources) of a run "
                    "directory (it holds world.db) or of an experiment directory (<arm>/seed_<n>/ runs).")
    p.add_argument("path", help="a run directory or an experiment directory")
    p.add_argument("--out", required=True, metavar="FILE", help="the HTML file to write ('-' writes to standard output)")
    p.add_argument("--primary", metavar="OUTCOME", help="experiment: the primary outcome instead of experiment.primary_outcome")
    p.add_argument("--allow-incomplete", action="store_true",
                   help="experiment: leave out the seed pairs an incomplete run touches instead of refusing")
    p.add_argument("--max-notes", type=int, metavar="N", help=f"run: most recent notes listed per agent (default {MAX_NOTES})")
    return p


def cli_main(argv: list[str]) -> int:
    """``void report PATH --out FILE`` (the lazy-subcommand contract of :mod:`void.cli`); returns the exit status."""
    from void.research.compare import IncomparableRuns

    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # --help (0) or a usage error (2): the status, not an exception, is the contract
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
    path = Path(args.path)
    kind = detect(path)
    if kind is None:
        print(f"void report: {path} is neither a run directory (no {DB_FILE}) nor an experiment directory "
              "(no <arm>/seed_<n>/ runs)", file=sys.stderr)
        return 2
    if kind == "run" and (args.primary is not None or args.allow_incomplete):
        print("void report: --primary and --allow-incomplete apply to an experiment directory, not to a run", file=sys.stderr)
        return 2
    if kind == "experiment" and args.max_notes is not None:
        print("void report: --max-notes applies to a run directory, not to an experiment", file=sys.stderr)
        return 2
    if args.max_notes is not None and args.max_notes < 0:
        print("void report: --max-notes must be >= 0", file=sys.stderr)
        return 2
    try:
        if kind == "run":
            text = run_report(path, max_notes=MAX_NOTES if args.max_notes is None else args.max_notes)
        else:
            text = experiment_report(path, primary=args.primary, allow_incomplete=args.allow_incomplete)
    except IncomparableRuns as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if args.out == "-":
        sys.stdout.write(text)
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote the {kind} report of {path} to {out} ({len(text.encode('utf-8'))} bytes)")
    return 0
