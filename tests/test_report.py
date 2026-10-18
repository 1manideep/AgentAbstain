"""``void report``: the static HTML report of a run or an experiment (MEMORY_EVOLUTION 5.10).

Scripted runs only. One 2-day run of the E0 decoy arm (its maintenance rewrites every policy on the second night,
and the audit exam is asked one day back so day 2 holds exam items) checks the run report: a self-contained file
(no script, no URL), the policy word diffs against ``PolicyStore.timeline``'s counts, the exam, maintenance and
vault tables against the analysis and the day rows. A copy of that run with a planted ``<b>x</b>`` policy line and a
``<script>`` note title checks the escaping. A synthetic two-arm experiment (configs, completed run rows and
metrics rows, no simulation) with one passing and one failing pre-registered hypothesis checks the experiment
report.
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from test_experiments import CONFIGS, run_arm

from void.cli import main
from void.config import VoidConfig, load_config, micro_to_usd
from void.db import Database
from void.memory.policy import PolicyStore
from void.research.analysis import calls_by_purpose, exam_counts, load_metrics, memory_spend_share
from void.research.compare import compare, render_hypotheses
from void.research.report import (
    VAULT_COLUMNS,
    cli_main,
    detect,
    esc,
    experiment_report,
    run_report,
    svg_curves,
    word_diff_html,
)

PLANTED_POLICY = "<b>x</b>"
PLANTED_TITLE = "<script>alert(1)</script>"


# --- fixtures -----------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def memory_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("report_run")
    arm = run_arm(CONFIGS / "exp_mem_e0_decoy.yaml", out, (1,), run={"days": 2, "ticks_per_day": 12},
                  memory={"exam": {"delay_days": 1}})
    return arm / "seed_1"


@pytest.fixture(scope="module")
def planted_run(memory_run: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the run with agent text that must never reach the page as markup: a policy version holding
    ``<b>x</b>`` and a note titled ``<script>...``, written straight into the tables (past the sanitiser)."""
    run = tmp_path_factory.mktemp("report_planted") / "seed_1"
    shutil.copytree(memory_run, run)
    conn = sqlite3.connect(str(run / "world.db"))
    try:
        aid, version, text = conn.execute(
            "SELECT agent_id, version, text FROM policy_versions ORDER BY agent_id, version DESC LIMIT 1").fetchone()
        conn.execute("INSERT INTO policy_versions(agent_id, version, tick, text, parent_version, source, source_agent_id, "
                     "similarity_to_parent) VALUES(?,?,?,?,?,?,?,?)",
                     (aid, version + 1, 24, f"{text}\n- {PLANTED_POLICY} keep this line", version, "self_edit", None, 0.9))
        conn.execute("INSERT INTO notes(note_id, agent_id, path, title, created_tick, embedding) VALUES(?,?,?,?,?,?)",
                     ("nt_planted", aid, f"vaults/{aid}/notes/planted.md", PLANTED_TITLE, 24, b""))
        conn.commit()
    finally:
        conn.close()
    return run


@pytest.fixture(scope="module")
def run_html(memory_run: Path) -> str:
    return run_report(memory_run)


class _Tables(HTMLParser):
    """Every ``<table>`` of a page as rows of cell texts, keyed by the id of the enclosing ``<section>``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section = ""
        self.tables: dict[str, list[list[list[str]]]] = {}
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "section":
            self.section = dict(attrs).get("id") or ""
        elif tag == "table":
            self.tables.setdefault(self.section, []).append([])
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.tables[self.section][-1].append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _tables(page: str) -> dict[str, list[list[list[str]]]]:
    p = _Tables()
    p.feed(page)
    return p.tables


def _assert_self_contained(page: str) -> None:
    low = page.lower()
    assert page.startswith("<!doctype html>") and low.rstrip().endswith("</html>")
    assert "<script" not in low and "http" not in low
    for needle in ("<link", "@import", "url(", " src=", "<iframe", "<object", "<embed", "<img", " on"):
        if needle == " on":  # no inline event handler attribute (onload=, onclick=, ...)
            assert not any(f" on{ev}=" in low for ev in ("load", "click", "error", "mouseover", "focus")), needle
        else:
            assert needle not in low, needle


# --- the run report -----------------------------------------------------------------------------------------------
def test_run_report_cli_writes_one_self_contained_file(planted_run: Path, tmp_path: Path,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "nested" / "r.html"
    assert main(["report", str(planted_run), "--out", str(out)]) == 0
    assert "run report" in capsys.readouterr().out
    page = out.read_text(encoding="utf-8")
    _assert_self_contained(page)
    for sid in ("agents", "summary", "daily", "policy", "exams", "maintenance", "vault", "notes"):
        assert f'<section id="{sid}">' in page, sid
    # the planted policy line and note title arrive as text, never as markup
    assert PLANTED_POLICY not in page and "&lt;b&gt;x&lt;/b&gt;" in page
    assert "<ins>- &lt;b&gt;x&lt;/b&gt; keep this line</ins>" in page  # the appended line, as one inserted run
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<b>" not in page


def test_run_report_is_deterministic_and_stdout_matches(memory_run: Path, run_html: str,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    assert run_report(memory_run) == run_html
    assert cli_main([str(memory_run), "--out", "-"]) == 0
    assert capsys.readouterr().out == run_html
    _assert_self_contained(run_html)


def test_policy_diffs_match_the_policy_store(memory_run: Path, run_html: str) -> None:
    db = Database(memory_run / "world.db")
    try:
        cfg = load_config(memory_run / "config.resolved.yaml")
        store = PolicyStore(db, cfg.memory.policy, memory_run / "vaults", ticks_per_day=cfg.run.ticks_per_day)
        agents = [str(r["agent_id"]) for r in db.fetchall("SELECT agent_id FROM agents ORDER BY agent_id")]
        edited = 0
        for aid in agents:
            history = store.history(aid)
            timeline = store.timeline(aid)
            prev = ""
            for v, t in zip(history, timeline, strict=True):
                diff, added, removed = word_diff_html(prev, v.text)
                assert (added, removed) == (t["words_added"], t["words_removed"])
                assert f"{len(v.text)} chars, +{added} -{removed} words" in run_html
                if v.version > 1:
                    edited += 1
                    assert str(diff) in run_html  # the version's diff is on the page as computed
                prev = v.text
    finally:
        db.close()
    assert edited >= 1  # the decoy rewrote at least one policy on the second night
    assert "<ins>" in run_html and "<del>" in run_html


def test_exam_tables_agree_with_the_analysis(memory_run: Path, run_html: str) -> None:
    counts = exam_counts(memory_run)
    assert counts is not None and counts[0] > 0  # day 2 asked about day 1
    asked, correct, abstained = counts
    tables = _tables(run_html)["exams"]
    per_agent, per_kind = tables[0], tables[1]
    assert per_agent[0][:6] == ["agent", "tier", "graded", "correct", "abstained", "wrong"]
    total = per_agent[-1]
    assert total[0] == "all agents"
    assert [int(x) for x in total[2:5]] == [asked, correct, abstained]
    assert sum(int(r[2]) for r in per_agent[1:-1]) == asked
    assert per_kind[0][-1] == "majority baseline"
    assert sum(int(r[1]) for r in per_kind[1:]) == asked
    assert sum(int(r[2]) for r in per_kind[1:]) == correct
    assert sum(int(r[3]) for r in per_kind[1:]) == abstained
    for r in per_kind[1:]:
        graded, right, abst = int(r[1]), int(r[2]), int(r[3])
        assert float(r[6]) == pytest.approx(right / graded, abs=1e-5)
        assert float(r[7]) == pytest.approx(abst / graded, abs=1e-5)


def test_maintenance_and_vault_tables_agree_with_the_run(memory_run: Path, run_html: str) -> None:
    tables = _tables(run_html)
    maint = tables["maintenance"][0]
    head, total = maint[0], maint[-1]
    assert head[3:7] == ["calls", "real cost USD", "world cost USD", "share of own call cost"]
    spent = calls_by_purpose(memory_run)["maintain"]
    assert int(total[3]) == spent["calls"] > 0
    assert total[4] == f"{spent['real_cost_usd']:.6f}" and total[5] == f"{spent['world_cost_usd']:.6f}"
    assert float(total[6]) == pytest.approx(memory_spend_share(memory_run), rel=1e-5)
    assert all(r[2] == "decoy" for r in maint[1:-1] if int(r[3]) > 0)
    _, day_rows = load_metrics(memory_run)
    population = tables["vault"][0]
    assert population[0] == ["day", "living", *VAULT_COLUMNS, "link_density (day row)"]
    assert [int(r[0]) for r in population[1:]] == [int(d["day"]) for d in day_rows] == [1, 2]
    for r, d in zip(population[1:], day_rows, strict=True):
        assert int(r[2]) == d["vault"]["notes"] and int(r[3]) == d["vault"]["bytes"]
    per_day = tables["vault"][1]
    assert per_day[0] == ["agent", "day 1", "day 2"]
    for col, d in enumerate(day_rows, start=1):
        assert sum(int(r[col]) for r in per_day[1:]) == d["vault"]["notes"]
    # every balance on the agents table is the one in the database
    with sqlite3.connect(str(memory_run / "world.db")) as conn:
        balances = {name: micro_to_usd(int(b)) for name, b in conn.execute("SELECT name, balance FROM agents")}
    for r in tables["agents"][0][1:]:
        assert float(r[-1]) == pytest.approx(balances[r[0]], abs=1e-6)


def test_notes_are_capped_per_agent(memory_run: Path) -> None:
    page = run_report(memory_run, max_notes=1)
    notes = _tables(page)["notes"]
    assert notes and all(len(t) == 2 for t in notes)  # header plus the single most recent note
    assert "older notes not listed" in page


# --- pieces ---------------------------------------------------------------------------------------------------------
def test_word_diff_marks_changed_words_and_escapes_them() -> None:
    assert word_diff_html("a b c", "a x c") == ("a <del>b</del><ins>x</ins> c", 1, 1)
    assert word_diff_html("a b c", "a c") == ("a <del>b</del> c", 0, 1)
    assert word_diff_html("a c", "a b c") == ("a <ins>b</ins> c", 1, 0)
    assert word_diff_html("", "  one two") == ("  <ins>one two</ins>", 2, 0)
    assert word_diff_html("# T\n- one\n- keep\n", "# T\n- two\n- keep\n") == ("# T\n- <del>one</del><ins>two</ins>\n- keep\n", 1, 1)
    diff, added, removed = word_diff_html("x", "x <i>y</i> & 'z'")
    assert diff == "x <ins>&lt;i&gt;y&lt;/i&gt; &amp; &#x27;z&#x27;</ins>" and (added, removed) == (3, 0)
    assert word_diff_html("same text", "same text") == ("same text", 0, 0)


def test_esc_formats_and_escapes() -> None:
    assert esc(None) == "n/a" and esc(True) == "true" and esc(3) == "3" and esc(float("nan")) == "n/a"
    assert esc(0.123456789) == "0.123457"
    assert esc('<a href="x">') == "&lt;a href=&quot;x&quot;&gt;"
    assert esc({"k": "<v>"}) == "{&quot;k&quot;: &quot;&lt;v&gt;&quot;}"
    assert esc(esc("<x>")) == "&lt;x&gt;"  # escaped once, not twice


def test_svg_curves_draws_one_polyline_per_series() -> None:
    svg = svg_curves({"arm<a>": {"1": 1.0, "2": 2.0, "3": None}, "b": {1: 1.5, 2: float("nan"), 3: 0.5}}, label="m & n")
    assert svg.count("<polyline") == 2 and svg.count("<circle") == 4
    assert "arm&lt;a&gt;" in svg and "arm<a>" not in svg and "m &amp; n" in svg
    assert "http" not in svg and "<script" not in svg
    # y spans the data: the highest point at the top of the plot, the lowest at the bottom
    first = svg.split('<polyline points="', 1)[1].split('"', 1)[0].split()
    assert [float(p.split(",")[1]) for p in first] == [pytest.approx(14 + 206 * 2 / 3, abs=0.1), pytest.approx(14.0)]
    flat = svg_curves({"only": {"4": 2.0}}, label="flat")  # one point, no range: no division by zero
    assert flat.count("<polyline") == 1 and flat.count("<circle") == 1
    assert "no daily values" in svg_curves({"a": {"1": None}}, label="empty")


# --- the experiment report ------------------------------------------------------------------------------------------
SEEDS = (1, 2, 3, 4)
UP = (0.9, 1.1, 1.0, 1.2)
HYPOTHESES = [
    {"name": "up_beats_base", "arm_a": "base", "arm_b": "up", "outcome": "mean_balance", "kind": "superiority",
     "margin": 0.2},
    {"name": "flat <script>x</script>", "arm_a": "base", "arm_b": "up", "outcome": "mean_balance",
     "kind": "equivalence", "margin": 0.05, "primary": False},
]


def _synthetic_run(run_dir: Path, cfg: VoidConfig, balance_by_day: dict[int, float]) -> None:
    """A completed run: resolved config, a ``completed`` run row and tick rows carrying ``mean_balance``."""
    run_dir.mkdir(parents=True)
    text = cfg.to_yaml()
    (run_dir / "config.resolved.yaml").write_text(text)
    last = cfg.run.days * cfg.run.ticks_per_day
    db = Database(run_dir / "world.db")
    try:
        db.execute("INSERT INTO run(run_id, config_hash, config_yaml, seed, created_at, schema_version, status, ended_tick) "
                   "VALUES(?,?,?,?,?,?,?,?)", (f"r{cfg.run.name}{cfg.run.seed}", "synthetic", text, cfg.run.seed,
                                               "2026-01-01T00:00:00", 2, "completed", last))
    finally:
        db.close()
    rows: list[dict[str, Any]] = []
    for t in range(1, last + 1):
        day = (t - 1) // cfg.run.ticks_per_day + 1
        rows.append({"tick": t, "day": day, "mean_balance": balance_by_day[day], "population": 3})
    rows += [{"kind": "day", "day": d} for d in range(1, cfg.run.days + 1)]
    (run_dir / "metrics.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


@pytest.fixture(scope="module")
def experiment(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("report_exp") / "exp_synth"
    for i, arm in enumerate(("base", "up")):
        base = load_config(CONFIGS / "base.yaml", {
            "run": {"name": f"exp_synth_{arm}", "days": 2, "ticks_per_day": 2},
            "world": {"scarcity": round(1.0 - 0.1 * i, 2)},
            "entropy": {"degeneration": {"mode": "observe"}},
            "experiment": {"name": "exp_synth", "arm": arm, "independent_variables": ["world.scarcity"],
                           "primary_outcome": "mean_balance", "secondary_outcomes": ["deaths"], "hypotheses": HYPOTHESES},
        })
        for j, seed in enumerate(SEEDS):
            cfg = base.model_copy(deep=True)
            cfg.run.seed = seed
            value = 2.0 + 0.1 * seed + 0.05 * math.sin(seed) + (UP[j] if arm == "up" else 0.0)
            _synthetic_run(root / arm / f"seed_{seed}", cfg, {1: value - 0.3, 2: value})
    return root


def test_experiment_report_has_pass_and_fail_lines_and_curves(experiment: Path, tmp_path: Path) -> None:
    out = tmp_path / "exp.html"
    assert cli_main([str(experiment), "--out", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    _assert_self_contained(page)
    result = compare(experiment)
    verdicts = {h["name"]: h["passed"] for h in result["hypotheses"]}
    assert verdicts == {"up_beats_base": True, "flat <script>x</script>": False}
    hyp = _tables(page)["hypotheses"][0]
    assert [r[0] for r in hyp[1:]] == ["PASS", "FAIL"]
    assert [r[1] for r in hyp[1:]] == ["up_beats_base", "flat <script>x</script>"]  # as text, after unescaping
    assert '<span class="pass">PASS</span>' in page and '<span class="fail">FAIL</span>' in page
    # the same PASS / FAIL lines void compare prints, escaped
    for line in render_hypotheses(result).splitlines()[1:]:
        assert line.strip().startswith(("PASS", "FAIL"))
        assert esc(line.strip()) in page
    assert "flat &lt;script&gt;x&lt;/script&gt;" in page
    # primary and secondary statistics
    tables = _tables(page)
    primary = tables["primary"][0]
    assert primary[1][:3] == ["base", "up", str(len(SEEDS))]
    assert float(primary[1][6]) == pytest.approx(sum(UP) / len(UP), abs=1e-5)
    assert tables["secondary"][0][1][0] == "deaths"
    # the daily curves: the primary per arm, as polylines with one point per day
    curves = page.split('<section id="curves">', 1)[1].split("</section>", 1)[0]
    assert curves.count("<polyline") >= 2 and "mean_balance per day" in curves
    assert result["curves"]["up"]["2"] - result["curves"]["up"]["1"] == pytest.approx(0.3)
    assert experiment_report(experiment) == page  # deterministic


def test_detect_and_command_errors(memory_run: Path, experiment: Path, tmp_path: Path,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    assert detect(memory_run) == "run" and detect(experiment) == "experiment"
    assert detect(tmp_path) is None and detect(tmp_path / "missing") is None
    assert cli_main([str(tmp_path), "--out", str(tmp_path / "x.html")]) == 2
    assert "neither a run directory" in capsys.readouterr().err
    assert not (tmp_path / "x.html").exists()
    assert cli_main(["--help"]) == 0 and "usage: void report" in capsys.readouterr().out
    assert cli_main([str(memory_run)]) == 2  # --out is required
    capsys.readouterr()
    assert cli_main([str(memory_run), "--out", "-", "--primary", "deaths"]) == 2
    assert "experiment directory" in capsys.readouterr().err
    assert cli_main([str(experiment), "--out", "-", "--max-notes", "3"]) == 2
    assert "run directory" in capsys.readouterr().err
    one_arm = tmp_path / "one_arm"
    shutil.copytree(experiment / "base", one_arm / "base")
    assert cli_main([str(one_arm), "--out", str(tmp_path / "one.html")]) == 2
    assert capsys.readouterr().err.startswith("refused: ")
