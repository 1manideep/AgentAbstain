"""Two processes, same config and seed: byte-identical metrics.jsonl and identical event sequences (DESIGN §14.2).

Each run happens in its own subprocess with its own working directory and a *relative* run dir, so
absolute temp paths never leak into the compared payloads and Python's per-process hash
randomisation is left on (a run must not depend on it).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO, SMOKE

DAYS = 2

RUNNER = '''
import asyncio, json, sys
from pathlib import Path
from void.config import load_config
from void.sim.loop import Simulation

cfg = load_config(sys.argv[1], {"run": {"days": int(sys.argv[2])}})
sim = Simulation(cfg, Path("run"))
status = asyncio.run(sim.run())
rows = sim.db.fetchall("SELECT tick, kind, agent_id, payload FROM events ORDER BY seq")
with open("events.jsonl", "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps([r["tick"], r["kind"], r["agent_id"], json.loads(r["payload"])], sort_keys=True) + "\\n")
print(status)
'''


def _spawn(workdir: Path, script: Path) -> subprocess.Popen:
    workdir.mkdir()
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.Popen([sys.executable, str(script), str(SMOKE), str(DAYS)], cwd=workdir, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


@pytest.fixture(scope="module")
def twin_runs(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("determinism")
    script = root / "runner.py"
    script.write_text(RUNNER, encoding="utf-8")
    procs = [_spawn(root / name, script) for name in ("a", "b")]
    outputs = [p.communicate(timeout=240)[0] for p in procs]
    for p, out in zip(procs, outputs, strict=True):
        assert p.returncode == 0, out[-2000:]
        assert out.strip().splitlines()[-1] == "completed", out[-2000:]
    return root / "a", root / "b"


def test_metrics_jsonl_is_byte_identical(twin_runs):
    a, b = twin_runs
    ma, mb = (a / "run" / "metrics.jsonl").read_bytes(), (b / "run" / "metrics.jsonl").read_bytes()
    assert ma == mb
    rows = [json.loads(line) for line in ma.decode().splitlines() if line.strip()]
    assert len(rows) == DAYS * 24 + DAYS and len({r["config_hash"] for r in rows}) == 1


def test_event_sequences_are_identical(twin_runs):
    a, b = twin_runs
    ea = (a / "events.jsonl").read_text(encoding="utf-8").splitlines()
    eb = (b / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(ea) == len(eb) > 100
    first_diff = next((i for i, (x, y) in enumerate(zip(ea, eb, strict=True)) if x != y), None)
    assert first_diff is None, f"event #{first_diff} differs:\n{ea[first_diff]}\n{eb[first_diff]}"
    kinds = {json.loads(line)[1] for line in ea}
    assert {"tick", "forage", "move", "chronicle", "run_ended"} <= kinds
