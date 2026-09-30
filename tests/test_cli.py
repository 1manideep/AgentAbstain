"""`void schema`, `void mock-feed` and `void run` through the argparse entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from void.cli import main
from void.server.protocol import SCHEMA_NAMES, parse_message

ROOT = Path(__file__).resolve().parents[1]
CONFIG = str(ROOT / "configs" / "scripted_smoke.yaml")


def test_schema_writes_nine_valid_schemas(tmp_path: Path) -> None:
    assert main(["schema", "--out", str(tmp_path)]) == 0
    files = sorted(p.name for p in tmp_path.glob("*.schema.json"))
    assert files == sorted(f"{n}.schema.json" for n in SCHEMA_NAMES) and len(files) == 9
    for name in SCHEMA_NAMES:
        schema = json.loads((tmp_path / f"{name}.schema.json").read_text())
        assert schema["$schema"].startswith("https://json-schema.org/")
        assert schema["type"] == "object" and "properties" in schema
        assert schema["properties"]["type"]["const"] == name
        assert "type" in schema["properties"]


def test_mock_feed_records_a_genuine_stream(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "fixtures" / "mock.jsonl"
    assert main(["mock-feed", "--config", CONFIG, "--ticks", "30", "--out", str(out)]) == 0
    lines = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert lines[0]["type"] == "hello" and lines[0]["tick"] == 0
    assert [m["type"] for m in lines[:5]] == ["hello", "roster", "gadgets", "tasks", "chronicle"]
    for m in lines:
        parse_message(m)
    snaps = [m for m in lines if m["type"] == "snapshot"]
    assert len(snaps) >= 30
    ts = [s["ts_ms"] for s in snaps]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)
    gaps = [b - a for a, b in zip(ts, ts[1:], strict=True)]
    assert sum(1 for g in gaps if g >= 20_000) == 1 and all(250 <= g <= 1500 for g in gaps if g < 20_000)
    assert [s["tick"] for s in snaps] == list(range(1, len(snaps) + 1))
    metrics = [m for m in lines if m["type"] == "metrics"]
    assert len(metrics) == len(snaps)
    assert any(m["type"] == "event" for m in lines) and any(m["type"] == "day" for m in lines)
    assert "wrote" in capsys.readouterr().out
    # determinism: the same seed yields the same stream
    out2 = tmp_path / "mock2.jsonl"
    assert main(["mock-feed", "--config", CONFIG, "--ticks", "30", "--out", str(out2)]) == 0
    assert out2.read_text() == out.read_text()


def test_run_one_day_writes_metrics(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["run", "--config", CONFIG, "--days", "1", "--out", str(tmp_path)])
    assert rc == 0
    run_dir = tmp_path / "scripted_smoke"
    metrics = run_dir / "metrics.jsonl"
    assert metrics.is_file() and (run_dir / "world.db").is_file()
    rows = [json.loads(line) for line in metrics.read_text().splitlines() if line.strip()]
    assert sum(1 for r in rows if r.get("kind") == "day") == 1 and sum(1 for r in rows if "tick" in r and r.get("kind") != "day") == 24
    out = capsys.readouterr().out
    assert "day   1" in out and "status=completed" in out
    # a second run into the same directory without --resume is refused (exit 2)
    assert main(["run", "--config", CONFIG, "--days", "1", "--out", str(tmp_path)]) == 2


def test_run_seed_sweep_layout(tmp_path: Path) -> None:
    rc = main(["run", "--config", CONFIG, "--days", "1", "--seeds", "1-2", "--out", str(tmp_path)])
    assert rc == 0
    for seed in (1, 2):
        assert (tmp_path / "scripted_smoke" / "default" / f"seed_{seed}" / "metrics.jsonl").is_file()


def test_inspect(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--config", CONFIG, "--days", "1", "--out", str(tmp_path)]) == 0
    run_dir = str(tmp_path / "scripted_smoke")
    assert main(["inspect", "--run-dir", run_dir, "--tree", "--money", "--events", "5", "--reindex"]) == 0
    out = capsys.readouterr().out
    assert "status=completed" in out and "== lineage ==" in out and "== money ==" in out and "reindexed" in out
    assert main(["inspect", "--run-dir", str(tmp_path / "nope")]) == 1
