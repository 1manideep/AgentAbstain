"""Flat exports of a run for re-analysis outside Python, with a data card (MEMORY_EVOLUTION §7.4, WP0).

``export_run(run_dir, out_dir)`` writes one file per table of ``world.db`` (every table in ``sqlite_master``;
the per-tick ``snapshots`` only with ``include_snapshots=True``) plus a derived ``exam_items_answers`` table
(``exam_items LEFT JOIN exam_answers USING (item_id)``, one row per question). The system prompts are in
``prompt_texts`` (keyed by ``llm_calls.system_hash``), the user turn of every call is ``llm_calls.prompt_text``
and the run metadata (config hash, resolved config YAML, seed, status) is the ``run`` table, so any logged call
can be rebuilt from the export alone. ``run.operator_token_hash`` is left out: it authorises a live run's
control channel and an operator-chosen token could be guessed from its hash; the card lists it under
``redacted``.

``manifest.json`` is the data card: the run's id, config hash, seed, status and database schema version; the
distinct ``llm_calls.model`` strings (``served_models``); the code commit and dirty flag recorded at run
creation (``world_kv``; null when absent); the format; and per table its row count, file and columns (declared
SQLite type and the storage classes actually present).

Formats: ``fmt="csv"``, ``fmt="parquet"`` (needs the ``export`` extra, ``pyarrow``) or ``fmt="auto"`` (Parquet
when ``pyarrow`` is importable, CSV otherwise); the card says which, so a reader never guesses. The CSV dialect
is lossless: RFC 4180, UTF-8, a header row, every string (and every BLOB, as lowercase hex) double-quoted,
numbers unquoted and NULL an empty unquoted field, so NULL and the empty string stay distinct (Python 3.12's
``csv.QUOTE_STRINGS`` convention). ``read_export(out_dir, table)`` reads either format back with the original
Python types. Everything is read-only over ``world.db`` and read inside one transaction, so a live run exports a
consistent snapshot.
"""

from __future__ import annotations

import importlib
import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

__all__ = ["CARD_VERSION", "DERIVED", "FORMATS", "OPTIONAL_TABLES", "PYARROW_MISSING", "REDACTED", "export_run",
           "read_export"]

CARD_VERSION = 1
FORMATS: tuple[str, ...] = ("auto", "csv", "parquet")
PYARROW_MISSING = "pyarrow missing: install the export extra"
# Large per-tick world states that the other tables already cover; exported on request only.
OPTIONAL_TABLES: frozenset[str] = frozenset({"snapshots"})
# Columns never written: (table, column) pairs, listed in the card under "redacted".
REDACTED: dict[str, tuple[str, ...]] = {"run": ("operator_token_hash",)}
# Derived tables: name -> (left table, right table, join column), written as ``left LEFT JOIN right USING (key)``
# with the right table's key column dropped. Built only when both tables exist and no table has the name.
DERIVED: dict[str, tuple[str, str, str]] = {"exam_items_answers": ("exam_items", "exam_answers", "item_id")}
# How the tables join, for the card; a pair is listed when both of its tables were exported.
JOINS: tuple[tuple[str, str, str], ...] = (
    ("llm_calls", "prompt_texts", "llm_calls.system_hash = prompt_texts.hash (the system prompt; "
                                  "llm_calls.prompt_text is the user turn)"),
    ("exam_answers", "exam_items", "exam_answers.item_id = exam_items.item_id (pre-joined as exam_items_answers)"),
    ("exam_answers", "llm_calls", "exam_answers.call_id = llm_calls.call_id"),
    ("memory_commands", "llm_calls", "memory_commands.call_id = llm_calls.call_id"),
    ("note_links", "notes", "note_links.from_note_id = notes.note_id"),
    ("llm_calls", "agents", "llm_calls.agent_id = agents.agent_id"),
)
CSV_ENCODING: dict[str, str] = {
    "dialect": "RFC 4180, UTF-8, CRLF line ends, one header row of column names",
    "strings": "always double-quoted, an inner quote doubled",
    "numbers": "unquoted; integers as digits, reals in Python repr (round-trips exactly)",
    "null": "an empty unquoted field (an empty string is a quoted \"\")",
    "blob": "lowercase hex, double-quoted",
}
PARQUET_ENCODING: dict[str, str] = {
    "types": "integer -> int64, real (or integer mixed with real) -> double, text -> string, blob -> binary; "
             "a column mixing text or blob with other classes is written as string (blob as lowercase hex)",
    "null": "parquet null",
}
_BATCH = 5000


def export_run(run_dir: str | Path, out_dir: str | Path | None = None, *, fmt: str = "auto",
               tables: Iterable[str] | None = None, include_snapshots: bool = False) -> dict[str, Any]:
    """Write the run's tables to ``out_dir`` (default ``<run_dir>/export``) and return the data card.

    ``tables`` limits the export to those names (a table of ``world.db`` or a derived table; a name the
    database lacks is listed under ``omitted``); by default every table is written, ``snapshots`` only with
    ``include_snapshots``. ``fmt="parquet"`` without ``pyarrow`` raises ``RuntimeError(PYARROW_MISSING)``.
    """
    if fmt not in FORMATS:
        raise ValueError(f"fmt must be one of {', '.join(FORMATS)}, not {fmt!r}")
    run_dir = Path(run_dir)
    db_path = run_dir / "world.db"
    if not db_path.is_file():
        raise FileNotFoundError(f"{db_path} not found")
    pa, pq = _pyarrow()
    if fmt == "parquet" and pa is None:
        raise RuntimeError(PYARROW_MISSING)
    used = "parquet" if fmt == "parquet" or (fmt == "auto" and pa is not None) else "csv"
    out = Path(out_dir) if out_dir is not None else run_dir / "export"
    out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, isolation_level=None)
    try:
        conn.execute("BEGIN")  # one read snapshot for the card and every table, even while the run is live
        present = [str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]
        selected, omitted = _select(present, tables, include_snapshots)
        card = _card_header(conn, set(present), used)
        written: dict[str, Any] = {}
        files: set[str] = set()
        for name in selected:
            spec = _source(conn, name)
            path = out / _file_name(name, "parquet" if used == "parquet" else "csv", files)
            if used == "parquet":
                rows, ptypes = _write_parquet(conn, spec, path, pa, pq)
            else:
                rows, ptypes = _write_csv(conn, spec, path), None
            cols = [{"name": c, "type": t, "storage_classes": s} for c, t, s in zip(spec.columns, spec.types, spec.classes,
                                                                                     strict=True)]
            if ptypes is not None:
                for col, pt in zip(cols, ptypes, strict=True):
                    col["parquet_type"] = pt
            entry: dict[str, Any] = {"rows": rows, "path": path.name, "columns": cols}
            if spec.derived:
                entry["derived"] = spec.derived
            if spec.redacted:
                entry["redacted"] = list(spec.redacted)
            written[name] = entry
        conn.execute("COMMIT")
    finally:
        conn.close()

    names = set(written)
    card["tables"] = written
    card["omitted"] = omitted
    card["redacted"] = {t: e["redacted"] for t, e in written.items() if e.get("redacted")}
    card["joins"] = [text for a, b, text in JOINS if a in names and b in names]
    card["encoding"] = PARQUET_ENCODING if used == "parquet" else CSV_ENCODING
    (out / "manifest.json").write_text(json.dumps(card, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return card


def read_export(out_dir: str | Path, table: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    """``(columns, rows)`` of one exported table, values in their original Python types (BLOBs as bytes)."""
    out = Path(out_dir)
    card = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    entry = card["tables"][table]
    path = out / entry["path"]
    names = [c["name"] for c in entry["columns"]]
    blobs = [set(c["storage_classes"]) - {"null"} == {"blob"} for c in entry["columns"]]
    if card["format"] == "parquet":
        pa, pq = _pyarrow()
        if pq is None:
            raise RuntimeError(PYARROW_MISSING)
        data = pq.read_table(path).to_pydict()
        return names, list(zip(*(data[n] for n in names), strict=True))
    with path.open(encoding="utf-8", newline="") as fh:  # no newline translation: a CR inside a string is data
        parsed = _parse_csv(fh.read())
    if not parsed or [v for v, _ in parsed[0]] != names:
        raise ValueError(f"{path}: header does not match the data card")
    rows_out: list[tuple[Any, ...]] = []
    for raw in parsed[1:]:
        if len(raw) != len(names):
            raise ValueError(f"{path}: a row has {len(raw)} fields, the header {len(names)}")
        rows_out.append(tuple(_typed(v, quoted, blob) for (v, quoted), blob in zip(raw, blobs, strict=True)))
    return names, rows_out


# --- selection and the card ---------------------------------------------------------------------------------------
class _Source:
    """One exported table: the SELECT that produces it and its columns."""

    def __init__(self, name: str, sql: str, columns: list[str], types: list[str], classes: list[list[str]],
                 derived: str | None = None, redacted: tuple[str, ...] = ()) -> None:
        self.name, self.sql, self.columns, self.types, self.classes = name, sql, columns, types, classes
        self.derived, self.redacted = derived, redacted


def _select(present: list[str], tables: Iterable[str] | None, include_snapshots: bool) -> tuple[list[str], dict[str, str]]:
    have = set(present)
    derivable = [d for d, (left, right, _) in DERIVED.items() if d not in have and left in have and right in have]
    omitted: dict[str, str] = {}
    chosen: list[str] = []
    if tables is None:
        for t in present:
            if t in OPTIONAL_TABLES and not include_snapshots:
                omitted[t] = "include_snapshots is false"
            else:
                chosen.append(t)
        return sorted(chosen + derivable), omitted
    for t in dict.fromkeys([tables] if isinstance(tables, str) else tables):  # keep order, drop duplicates
        if t in have or t in derivable:
            chosen.append(t)
        elif t in DERIVED:
            omitted[t] = "its source tables are not all in this database"
        else:
            omitted[t] = "not in this database"
    return chosen, omitted


def _card_header(conn: sqlite3.Connection, present: set[str], fmt: str) -> dict[str, Any]:
    run: dict[str, Any] = {}
    if "run" in present:
        cur = conn.execute("SELECT * FROM run ORDER BY rowid LIMIT 1")
        row = cur.fetchone()
        if row is not None:
            run = dict(zip([d[0] for d in cur.description], row, strict=True))
    kv: dict[str, Any] = {}
    if "world_kv" in present:
        for key, value in conn.execute("SELECT key, value FROM world_kv WHERE key IN ('code_commit', 'code_dirty')"):
            try:
                kv[str(key)] = json.loads(value)
            except (TypeError, ValueError):
                kv[str(key)] = value
    served: list[str] = []
    if "llm_calls" in present:
        served = [str(r[0]) for r in conn.execute("SELECT DISTINCT model FROM llm_calls WHERE model IS NOT NULL ORDER BY model")]
    return {
        "card_version": CARD_VERSION,
        "created_by": "void export",
        "schema_version": run.get("schema_version"),
        "run_id": run.get("run_id"),
        "config_hash": run.get("config_hash"),
        "seed": run.get("seed"),
        "status": run.get("status"),
        "experiment": run.get("experiment"),
        "arm": run.get("arm"),
        "created_at": run.get("created_at"),
        "ended_tick": run.get("ended_tick"),
        "ended_reason": run.get("ended_reason"),
        "served_models": served,
        "code_commit": kv.get("code_commit"),
        "code_dirty": kv.get("code_dirty"),
        "format": fmt,
    }


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _file_name(table: str, ext: str, used: set[str]) -> str:
    """``<table>.<ext>``, made safe: a table name is database content and must never leave the export directory."""
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", table) or "table"
    name, i = f"{stem}.{ext}", 1
    while name.lower() in used:
        i += 1
        name = f"{stem}_{i}.{ext}"
    used.add(name.lower())
    return name


def _decl(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    return [(str(r[1]), str(r[2] or "")) for r in conn.execute(f"PRAGMA table_info({_q(table)})")]


def _has_rowid(conn: sqlite3.Connection, table: str) -> bool:
    try:
        conn.execute(f"SELECT rowid FROM {_q(table)} LIMIT 0")
    except sqlite3.OperationalError:
        return False
    return True


def _storage_classes(conn: sqlite3.Connection, sql: str, columns: list[str]) -> list[list[str]]:
    if not columns:
        return []
    aggs = ", ".join(f"group_concat(DISTINCT typeof({_q(c)}))" for c in columns)
    row = conn.execute(f"SELECT {aggs} FROM ({sql})").fetchone()
    return [sorted(str(v).split(",")) if v else [] for v in row]


def _source(conn: sqlite3.Connection, name: str) -> _Source:
    if name in DERIVED and not _table_exists(conn, name):
        left, right, key = DERIVED[name]
        lcols, rcols = _decl(conn, left), [c for c in _decl(conn, right) if c[0] != key]
        cols = [f"l.{_q(c)}" for c, _ in lcols] + [f"r.{_q(c)}" for c, _ in rcols]
        order = " ORDER BY l.rowid" if _has_rowid(conn, left) else ""
        sql = f"SELECT {', '.join(cols)} FROM {_q(left)} AS l LEFT JOIN {_q(right)} AS r USING ({_q(key)}){order}"
        decl = lcols + rcols
        names = [c for c, _ in decl]
        return _Source(name, sql, names, [t for _, t in decl], _storage_classes(conn, sql, names),
                       derived=f"{left} LEFT JOIN {right} USING ({key})")
    hidden = REDACTED.get(name, ())
    decl = [(c, t) for c, t in _decl(conn, name) if c not in hidden]
    names = [c for c, _ in decl]
    order = " ORDER BY rowid" if _has_rowid(conn, name) else ""
    sql = f"SELECT {', '.join(_q(c) for c in names)} FROM {_q(name)}{order}"
    redacted = tuple(c for c, _ in _decl(conn, name) if c in hidden)
    return _Source(name, sql, names, [t for _, t in decl], _storage_classes(conn, sql, names), redacted=redacted)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _batches(conn: sqlite3.Connection, sql: str) -> Iterator[list[tuple[Any, ...]]]:
    cur = conn.execute(sql)
    while True:
        chunk = cur.fetchmany(_BATCH)
        if not chunk:
            return
        yield chunk


# --- CSV ----------------------------------------------------------------------------------------------------------
def _csv_field(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return '"' + v.replace('"', '""') + '"'
    if isinstance(v, (bytes, bytearray, memoryview)):
        return '"' + bytes(v).hex() + '"'
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, float):
        return repr(v)
    return str(v)


def _write_csv(conn: sqlite3.Connection, spec: _Source, path: Path) -> int:
    n = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(",".join(_csv_field(c) for c in spec.columns) + "\r\n")
        for chunk in _batches(conn, spec.sql):
            fh.write("".join(",".join(_csv_field(v) for v in row) + "\r\n" for row in chunk))
            n += len(chunk)
    return n


# a field is a quoted string (inner quotes doubled) or a run of characters that are not a delimiter or a quote
_FIELD = re.compile(r'"([^"]*(?:""[^"]*)*)"|([^,"\r\n]*)')


def _parse_csv(text: str) -> list[list[tuple[str, bool]]]:
    """Records of ``(value, quoted)`` pairs; ``quoted`` separates NULL (unquoted empty) from the empty string."""
    rows: list[list[tuple[str, bool]]] = []
    pos, end = 0, len(text)
    while pos < end:
        row: list[tuple[str, bool]] = []
        while True:
            m = _FIELD.match(text, pos)
            assert m is not None  # the second branch matches the empty string
            quoted = m.group(1) is not None
            row.append((m.group(1).replace('""', '"'), True) if quoted else (m.group(2), False))
            pos = m.end()
            if pos >= end:
                break
            if text[pos] == ",":
                pos += 1
                continue
            if text.startswith("\r\n", pos):
                pos += 2
                break
            if text[pos] == "\n":
                pos += 1
                break
            raise ValueError(f"malformed CSV at offset {pos}")
        rows.append(row)
    return rows


def _typed(value: str, quoted: bool, blob: bool) -> Any:
    if quoted:
        return bytes.fromhex(value) if blob else value
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return float(value)


# --- Parquet ------------------------------------------------------------------------------------------------------
def _pyarrow() -> tuple[Any, Any]:
    try:
        pa = importlib.import_module("pyarrow")
        pq = importlib.import_module("pyarrow.parquet")
    except ImportError:
        return None, None
    return pa, pq


def _affinity(declared: str) -> str:
    """SQLite's column affinity rules (datatype3 §3.1), for the type of a column that holds only NULLs."""
    d = declared.upper()
    if "INT" in d:
        return "integer"
    if any(k in d for k in ("CHAR", "CLOB", "TEXT")):
        return "text"
    if d == "" or "BLOB" in d:
        return "blob"
    return "real"


def _hex_or_str(v: Any) -> Any:
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return repr(v) if isinstance(v, float) else str(v)


def _to_float(v: Any) -> Any:
    return None if v is None else float(v)


def _arrow_column(pa: Any, declared: str, classes: list[str]) -> tuple[Any, Any]:
    """``(arrow type, value converter or None)`` for one column."""
    kinds = set(classes) - {"null"} or {_affinity(declared)}
    if kinds == {"integer"}:
        return pa.int64(), None
    if kinds <= {"integer", "real"}:
        return pa.float64(), _to_float
    if kinds == {"text"}:
        return pa.string(), None
    if kinds == {"blob"}:
        return pa.binary(), None
    return pa.string(), _hex_or_str


def _write_parquet(conn: sqlite3.Connection, spec: _Source, path: Path, pa: Any, pq: Any) -> tuple[int, list[str]]:
    kinds = [_arrow_column(pa, t, c) for t, c in zip(spec.types, spec.classes, strict=True)]
    schema = pa.schema([pa.field(n, k[0]) for n, k in zip(spec.columns, kinds, strict=True)])
    n = 0
    with pq.ParquetWriter(str(path), schema) as writer:
        for chunk in _batches(conn, spec.sql):
            arrays = []
            for i, (atype, conv) in enumerate(kinds):
                values = [r[i] for r in chunk]
                if conv is not None:
                    values = [conv(v) for v in values]
                arrays.append(pa.array(values, type=atype))
            writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
            n += len(chunk)
        if n == 0:
            writer.write_table(schema.empty_table())
    return n, [str(k[0]) for k in kinds]
