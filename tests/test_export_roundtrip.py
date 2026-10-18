"""The run export round trip (MEMORY_EVOLUTION §7.4, WP0 "export round trip").

A scripted run is exported and read back: every table of ``world.db`` comes back with the same columns and the
same rows in the same Python types, a decide call's full system prompt and user turn are rebuilt from the export
alone, and the data card carries the run's identity, served models and code commit. The CSV dialect keeps NULL and
the empty string apart; the Parquet path runs only where the ``export`` extra is installed.
"""

from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

import void.research.export as ex
from void.brain.prompt import system_prompt
from void.config import VoidConfig
from void.db import SCHEMA_VERSION
from void.research.export import PYARROW_MISSING, export_run, read_export

NO_MUTATION = {"population": {"tier_mutation_prob": 0.0}}
CARD_KEYS = {"schema_version", "run_id", "config_hash", "seed", "status", "served_models", "code_commit", "code_dirty",
             "format", "tables", "created_by"}


def _db_tables(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    finally:
        conn.close()


def _db_rows(path: Path, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.execute(sql)
        return [d[0] for d in cur.description], [tuple(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _db_columns(path: Path, table: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(str(path))
    try:
        return [(str(r[1]), str(r[2] or "")) for r in conn.execute(f'PRAGMA table_info("{table}")')]
    finally:
        conn.close()


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return type(a) is type(b) and a == b


def _assert_rows_equal(got: list[tuple[Any, ...]], want: list[tuple[Any, ...]], where: str) -> None:
    assert len(got) == len(want), f"{where}: {len(got)} rows exported, {len(want)} in world.db"
    for i, (g, w) in enumerate(zip(got, want, strict=True)):
        assert len(g) == len(w), f"{where} row {i}: width"
        for j, (x, y) in enumerate(zip(g, w, strict=True)):
            assert _same(x, y), f"{where} row {i} col {j}: exported {x!r}, world.db {y!r}"


def _assert_round_trip(db: Path, out: Path, card: dict[str, Any]) -> None:
    """Every table of ``db`` (snapshots aside, unless exported) reads back from ``out`` exactly."""
    tables = [t for t in _db_tables(db) if t in card["tables"] or t != "snapshots"]
    for t in tables:
        assert t in card["tables"], t
        hidden = set(card["redacted"].get(t, []))
        decl = [(c, ty) for c, ty in _db_columns(db, t) if c not in hidden]
        entry = card["tables"][t]
        assert [(c["name"], c["type"]) for c in entry["columns"]] == decl, t
        assert (out / entry["path"]).is_file()
        cols, rows = read_export(out, t)
        want_cols, want = _db_rows(db, f'SELECT {", ".join(chr(34) + c + chr(34) for c, _ in decl)} FROM "{t}" ORDER BY rowid')
        assert cols == want_cols, t
        assert entry["rows"] == len(want), t
        _assert_rows_equal(rows, want, t)


# --- a 1-day scripted run with prompts logged ---------------------------------------------------------------------
@pytest.fixture(scope="module")
def run(run_sim):
    return run_sim("export_roundtrip", {**NO_MUTATION, "run": {"days": 1, "ticks_per_day": 12, "log_prompts": True}})


@pytest.fixture(scope="module")
def exported(run, tmp_path_factory) -> tuple[Path, dict[str, Any]]:
    out = tmp_path_factory.mktemp("export_csv")
    return out, export_run(run.run_dir, out, fmt="csv")


def test_every_table_reads_back_with_the_same_rows_and_columns(run, exported) -> None:
    out, card = exported
    assert card["format"] == "csv"
    db = run.run_dir / "world.db"
    present = _db_tables(db)
    assert {"prompt_texts", "run", "llm_calls", "events", "world_kv", "exam_items_answers"} <= set(card["tables"])
    assert set(card["tables"]) == (set(present) - {"snapshots"}) | {"exam_items_answers"}
    assert card["tables"]["prompt_texts"]["rows"] > 0 and card["tables"]["run"]["rows"] == 1
    _assert_round_trip(db, out, card)


def test_the_csv_is_plain_rfc_4180(run, exported) -> None:
    """Any CSV reader gets the header and the row count; strings match the database text exactly."""
    out, card = exported
    db = run.run_dir / "world.db"
    for t in ("llm_calls", "prompt_texts", "run", "events", "notes"):
        with (out / card["tables"][t]["path"]).open(encoding="utf-8", newline="") as fh:
            records = list(csv.reader(fh))
        names = [c["name"] for c in card["tables"][t]["columns"]]
        assert records[0] == names
        _, want = _db_rows(db, f'SELECT {", ".join(chr(34) + n + chr(34) for n in names)} FROM "{t}" ORDER BY rowid')
        assert len(records) - 1 == len(want) == card["tables"][t]["rows"]
        for got, row in zip(records[1:], want, strict=True):
            for g, w in zip(got, row, strict=True):
                if isinstance(w, str):
                    assert g == w
                elif w is None:
                    assert g == ""
    # multi-line text survives (prompts and system prompts span many lines)
    _, texts = read_export(out, "prompt_texts")
    assert all("\n" in text for _, text in texts)


def test_a_decide_call_is_rebuilt_from_the_export_alone(run, exported) -> None:
    out, card = exported
    cols, calls = read_export(out, "llm_calls")
    call = dict(zip(cols, next(c for c in calls if c[cols.index("purpose")] == "decide"), strict=True))
    prompts = dict(read_export(out, "prompt_texts")[1])
    run_cols, run_rows = read_export(out, "run")
    meta = dict(zip(run_cols, run_rows[0], strict=True))
    # the configuration the run used, from the export: it hashes to the card's config hash
    cfg = VoidConfig.model_validate(yaml.safe_load(meta["config_yaml"]))
    assert cfg.hash() == meta["config_hash"] == card["config_hash"] == run.cfg.hash()
    assert meta["seed"] == card["seed"] == run.cfg.run.seed
    # the full system prompt: prompt_texts via system_hash, and it is the prompt that config renders for the tier
    system = prompts[call["system_hash"]]
    assert system == system_prompt(cfg, call["tier"]) == system_prompt(run.cfg, call["tier"])
    # the full user turn, consistent with the observation it rendered
    obs = json.loads(call["observation"])
    user = call["prompt_text"]
    assert user.startswith(f"# Tick {call['tick']} (day {obs['day']},")
    assert f"You are {obs['name']} (id {obs['agent_id']})" in user and obs["agent_id"] == call["agent_id"]
    row = run.db.fetchone("SELECT prompt_text, system_hash FROM llm_calls WHERE call_id=?", (call["call_id"],))
    assert (row["prompt_text"], row["system_hash"]) == (user, call["system_hash"])
    assert "llm_calls.system_hash = prompt_texts.hash" in " ".join(card["joins"])


def test_the_data_card_has_its_keys(run, exported) -> None:
    out, card = exported
    assert json.loads((out / "manifest.json").read_text()) == card
    assert CARD_KEYS <= set(card)
    assert card["created_by"] == "void export" and card["format"] == "csv"
    assert card["schema_version"] == SCHEMA_VERSION
    assert card["run_id"] == run.sim.run_id and card["config_hash"] == run.cfg.hash() and card["seed"] == run.cfg.run.seed
    assert card["status"] == run.db.fetchone("SELECT status FROM run")["status"]
    models = [r["model"] for r in run.db.fetchall("SELECT DISTINCT model FROM llm_calls ORDER BY model")]
    assert card["served_models"] == models and models
    assert card["code_commit"] == run.kv("code_commit") and card["code_dirty"] == run.kv("code_dirty")
    for name, entry in card["tables"].items():
        assert isinstance(entry["rows"], int) and entry["path"] == f"{name}.csv", name
        assert entry["columns"] and all({"name", "type", "storage_classes"} <= set(c) for c in entry["columns"]), name
    assert card["tables"]["exam_items_answers"]["derived"] == "exam_items LEFT JOIN exam_answers USING (item_id)"
    assert card["omitted"] == {"snapshots": "include_snapshots is false"}


def test_the_operator_token_hash_is_never_written(run, exported) -> None:
    out, card = exported
    assert card["redacted"] == {"run": ["operator_token_hash"]}
    secret = run.db.fetchone("SELECT operator_token_hash FROM run")["operator_token_hash"]
    assert secret and all(secret not in p.read_text(encoding="utf-8") for p in out.iterdir())
    assert "operator_token_hash" not in [c["name"] for c in card["tables"]["run"]["columns"]]


def test_snapshots_on_request_and_a_named_subset(run, tmp_path: Path) -> None:
    db = run.run_dir / "world.db"
    card = export_run(run.run_dir, tmp_path / "all", fmt="csv", include_snapshots=True)
    assert "snapshots" in card["tables"] and card["omitted"] == {}
    _assert_round_trip(db, tmp_path / "all", card)
    sub = export_run(run.run_dir, tmp_path / "sub", fmt="csv", tables=["run", "no_such_table", "prompt_texts", "run"])
    assert list(sub["tables"]) == ["run", "prompt_texts"]
    assert sub["omitted"] == {"no_such_table": "not in this database"}
    assert sorted(p.name for p in (tmp_path / "sub").iterdir()) == ["manifest.json", "prompt_texts.csv", "run.csv"]
    assert list(export_run(run.run_dir, tmp_path / "one", fmt="csv", tables="run")["tables"]) == ["run"]  # a bare name


def test_the_default_location_is_the_run_export_dir(run) -> None:
    card = export_run(run.run_dir, fmt="csv", tables=["run"])
    assert (run.run_dir / "export" / "manifest.json").is_file() and card["tables"]["run"]["rows"] == 1


# --- formats ---------------------------------------------------------------------------------------------------------
def test_parquet_without_pyarrow_raises_and_auto_falls_back_to_csv(run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ex, "_pyarrow", lambda: (None, None))
    with pytest.raises(RuntimeError) as err:
        export_run(run.run_dir, tmp_path / "pq", fmt="parquet")
    assert str(err.value) == PYARROW_MISSING == "pyarrow missing: install the export extra"
    assert not (tmp_path / "pq").exists()
    assert export_run(run.run_dir, tmp_path / "auto", fmt="auto", tables=["run"])["format"] == "csv"
    with pytest.raises(ValueError, match="fmt must be one of"):
        export_run(run.run_dir, tmp_path / "x", fmt="xlsx")
    with pytest.raises(FileNotFoundError):
        export_run(tmp_path / "nowhere", fmt="csv")


def test_parquet_round_trip(run, tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    out = tmp_path / "pq"
    card = export_run(run.run_dir, out, fmt="parquet")
    assert card["format"] == "parquet" and card["encoding"] == ex.PARQUET_ENCODING
    assert all(e["path"].endswith(".parquet") and all("parquet_type" in c for c in e["columns"]) for e in card["tables"].values())
    assert card["served_models"] and "prompt_texts" in card["tables"] and "run" in card["tables"]
    _assert_round_trip(run.run_dir / "world.db", out, card)
    assert export_run(run.run_dir, tmp_path / "auto", tables=["run"])["format"] == "parquet"


# --- the derived exam table ------------------------------------------------------------------------------------------
def test_exam_items_answers_is_the_left_join(run_sim, tmp_path: Path) -> None:
    exam = {"enabled": True, "delay_days": 1, "questions_per_agent": 2, "kinds": ["balance", "node"]}
    examined = run_sim("export_exam", {**NO_MUTATION, "run": {"days": 2, "ticks_per_day": 12}, "memory": {"exam": exam}})
    card = export_run(examined.run_dir, tmp_path / "exam", fmt="csv")
    cols, rows = read_export(tmp_path / "exam", "exam_items_answers")
    want_cols, want = _db_rows(examined.run_dir / "world.db",
                               "SELECT i.*, a.call_id, a.answer, a.correct, a.abstained, a.retrieved FROM exam_items i "
                               "LEFT JOIN exam_answers a USING (item_id) ORDER BY i.rowid")
    assert cols == want_cols and len(rows) == card["tables"]["exam_items"]["rows"] > 0
    _assert_rows_equal(rows, want, "exam_items_answers")
    assert any(r[cols.index("correct")] is not None for r in rows)


# --- the dialect on awkward values, without a simulation ---------------------------------------------------------------
AWKWARD: list[tuple[Any, ...]] = [
    (1, "", None, b"\x00\xff", 0.1, "plain"),
    (2, None, "", b"", -0.0, '"quoted", with, commas'),
    (3, 'a "b"\r\nc\nd', "\\N", None, 1e300, "unicode: é中"),
    (4, "123", "  spaced  ", b"x" * 40, float("inf"), "1.5"),
    (5, ",", '"', b"\n", None, "NULL"),
]
MIXED: list[tuple[Any, ...]] = [(1, 7), (2, 2.5), (3, "text"), (4, None), (5, 2 ** 62), (6, -3)]


@pytest.fixture()
def awkward_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "awkward"
    run_dir.mkdir()
    conn = sqlite3.connect(str(run_dir / "world.db"))
    conn.executescript(
        'CREATE TABLE odd (id INTEGER PRIMARY KEY, a TEXT, b TEXT, blob BLOB, r REAL, "order" TEXT);'
        "CREATE TABLE mixed (id INTEGER PRIMARY KEY, v);"
        "CREATE TABLE keyed (k TEXT PRIMARY KEY, n INTEGER) WITHOUT ROWID;"
        "CREATE TABLE run (run_id TEXT PRIMARY KEY, config_hash TEXT, seed INTEGER, schema_version INTEGER, "
        "status TEXT, operator_token_hash TEXT);"
        "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT);"
        'CREATE TABLE "../escape" (x INTEGER);')
    conn.executemany("INSERT INTO odd VALUES (?,?,?,?,?,?)", AWKWARD)
    conn.executemany("INSERT INTO mixed VALUES (?,?)", MIXED)
    conn.executemany("INSERT INTO keyed VALUES (?,?)", [("b", 2), ("a", 1)])
    conn.execute("INSERT INTO run VALUES ('r1', 'h', 7, 2, 'completed', 'deadbeef')")
    conn.execute("INSERT INTO events(kind) VALUES ('x')")
    conn.execute('INSERT INTO "../escape" VALUES (1)')
    conn.commit()
    conn.close()
    return run_dir


def test_awkward_values_round_trip_through_csv(awkward_run: Path) -> None:
    out = awkward_run / "export"
    card = export_run(awkward_run, fmt="csv")
    # no sqlite_sequence, no derived table; a table name never steers where a file lands
    assert set(card["tables"]) == {"odd", "mixed", "keyed", "run", "events", "../escape"}
    assert card["tables"]["../escape"]["path"] == "___escape.csv" and read_export(out, "../escape")[1] == [(1,)]
    assert sorted(p.name for p in awkward_run.iterdir()) == ["export", "world.db"]
    assert card["code_commit"] is None and card["code_dirty"] is None and card["served_models"] == []
    assert (card["run_id"], card["seed"], card["status"], card["schema_version"]) == ("r1", 7, "completed", 2)
    assert "deadbeef" not in (out / "run.csv").read_text()
    _, odd = read_export(out, "odd")
    _assert_rows_equal(odd, AWKWARD, "odd")
    assert odd[0][1] == "" and odd[0][2] is None and odd[1][1] is None and odd[1][2] == ""
    _, mixed = read_export(out, "mixed")
    _assert_rows_equal(mixed, MIXED, "mixed")
    assert {c["name"]: c["storage_classes"] for c in card["tables"]["mixed"]["columns"]}["v"] == ["integer", "null", "real", "text"]
    _, keyed = read_export(out, "keyed")
    assert keyed == [("a", 1), ("b", 2)]
    # stdlib csv agrees on the structure (it cannot tell NULL from "", which is why read_export exists)
    with (out / "odd.csv").open(encoding="utf-8", newline="") as fh:
        records = list(csv.reader(io.StringIO(fh.read(), newline="")))
    assert records[0] == ["id", "a", "b", "blob", "r", "order"] and len(records) == len(AWKWARD) + 1
    assert records[3][1] == 'a "b"\r\nc\nd' and records[1][3] == "00ff"


def test_malformed_csv_is_refused(awkward_run: Path) -> None:
    export_run(awkward_run, fmt="csv", tables=["keyed"])
    path = awkward_run / "export" / "keyed.csv"
    path.write_text('"k","n"\r\n"a"x,1\r\n', encoding="utf-8")
    with pytest.raises(ValueError, match="malformed CSV"):
        read_export(awkward_run / "export", "keyed")
    path.write_text('"k","n"\r\n"a",1,2\r\n', encoding="utf-8")
    with pytest.raises(ValueError, match="fields"):
        read_export(awkward_run / "export", "keyed")


def test_awkward_values_round_trip_through_parquet(awkward_run: Path) -> None:
    pytest.importorskip("pyarrow")
    out = awkward_run / "pq"
    card = export_run(awkward_run, out, fmt="parquet")
    types = {c["name"]: c["parquet_type"] for c in card["tables"]["odd"]["columns"]}
    assert types == {"id": "int64", "a": "string", "b": "string", "blob": "binary", "r": "double", "order": "string"}
    _, odd = read_export(out, "odd")
    _assert_rows_equal(odd, AWKWARD, "odd")
    # a column mixing text with numbers is written as text (the card says so)
    assert {c["name"]: c["parquet_type"] for c in card["tables"]["mixed"]["columns"]}["v"] == "string"
    _, mixed = read_export(out, "mixed")
    assert [v for _, v in mixed] == ["7", "2.5", "text", None, str(2 ** 62), "-3"]
