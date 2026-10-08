"""Flat exports of a run for analysis outside Python (MEMORY_EVOLUTION §7.4) `[scaffold]`.

``export_run(run_dir, out_dir)`` writes one file per table that analysis needs: ``llm_calls`` (with the
prompt text and observation JSON), ``events``, ``wallet_ledger``, ``notes``, ``note_links``,
``policy_versions``, ``memory_commands``, ``exam_items`` joined with ``exam_answers``, and the metrics
rows. Parquet when ``pyarrow`` is importable, CSV otherwise; the format is reported in the manifest so a
reader never guesses. Everything is read-only over ``world.db``.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

__all__ = ["TABLES", "export_run"]

TABLES: tuple[str, ...] = (
    "llm_calls", "events", "wallet_ledger", "notes", "note_links", "self_versions", "policy_versions",
    "memory_commands", "practice_events", "exam_items", "exam_answers", "note_manifest", "metrics", "agents",
)


def _rows(conn: sqlite3.Connection, table: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    return cols, [tuple(r) for r in cur.fetchall()]


def export_run(run_dir: str | Path, out_dir: str | Path | None = None, *, tables: tuple[str, ...] = TABLES) -> dict[str, Any]:
    """Write every table in ``tables`` to ``out_dir`` (default ``<run_dir>/export``); returns the manifest."""
    run_dir = Path(run_dir)
    out = Path(out_dir) if out_dir is not None else run_dir / "export"
    out.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{run_dir / 'world.db'}?mode=ro", uri=True)
    try:
        import pyarrow as pa  # type: ignore[import-not-found]
        import pyarrow.parquet as pq  # type: ignore[import-not-found]
        fmt = "parquet"
    except Exception:  # pragma: no cover - depends on the environment
        pa = pq = None
        fmt = "csv"
    manifest: dict[str, Any] = {"run_dir": str(run_dir), "format": fmt, "tables": {}}
    try:
        for table in tables:
            try:
                cols, rows = _rows(conn, table)
            except sqlite3.OperationalError:
                continue
            if fmt == "parquet":
                path = out / f"{table}.parquet"
                data = {c: [_cell(r[i]) for r in rows] for i, c in enumerate(cols)}
                pq.write_table(pa.table(data), path)  # type: ignore[union-attr]
            else:
                path = out / f"{table}.csv"
                with path.open("w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow(cols)
                    for r in rows:
                        w.writerow([_cell(v) for v in r])
            manifest["tables"][table] = {"rows": len(rows), "path": str(path)}
    finally:
        conn.close()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def _cell(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return v
