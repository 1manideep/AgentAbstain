"""Golden fingerprints: features that are off must leave old worlds exactly as they were.

Two short scripted runs are fingerprinted (metrics.jsonl bytes, the event stream, every decision call with its
prompt and logged observation, and the wallet ledger) and compared with digests recorded on the code *before*
veins, fallow nodes and the scripted brain's vein memory existed (MEMORY_EVOLUTION §10.4, decision 19's
fallback):

* ``base.yaml`` (children kept on the scripted tier so nothing can reach a provider): veins and fallow are off by
  default, so a base run must be byte-identical to before, config hash included.
* the amnesic arm under fog (the smoke world with ``view_radius`` 12 and ``memory.enabled: false``): every change
  to how scripted agents use memory is gated on having memories, so the amnesic arm must not move at all.

Each run happens in its own working directory with a relative run dir, so no absolute path reaches a payload.
If one of these fails after an intended change to an *enabled* mechanism, the change leaked into worlds that do
not use it; re-record a digest only for a deliberate change to the old behaviour, and say so in the commit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO, deep_merge

from void.config import load_config
from void.sim.loop import Simulation


def _digest(rows: list[Any]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(r, sort_keys=True, default=str).encode())
        h.update(b"\n")
    return h.hexdigest()


def fingerprint(config: Path, overrides: dict[str, Any], workdir: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workdir)
    cfg = load_config(config, overrides)
    sim = Simulation(cfg, Path("run"))
    status = asyncio.run(sim.run())
    events = [[r["tick"], r["kind"], r["agent_id"], json.loads(r["payload"]), r["visibility"]]
              for r in sim.db.fetchall("SELECT tick, kind, agent_id, payload, visibility FROM events ORDER BY seq")]
    calls = [dict(r) for r in sim.db.fetchall(
        "SELECT tick, agent_id, purpose, action_type, raw_text, input_tokens, output_tokens, world_cost, prompt_text, observation "
        "FROM llm_calls ORDER BY call_id")]
    ledger = [dict(r) for r in sim.db.fetchall("SELECT * FROM wallet_ledger ORDER BY entry_id")]
    out = {"status": status, "config_hash": cfg.hash(),
           "metrics": hashlib.sha256((Path("run") / "metrics.jsonl").read_bytes()).hexdigest(),
           "events": _digest(events), "calls": _digest(calls), "ledger": _digest(ledger)}
    sim.db.close()
    return out


NO_CHILD_PROVIDERS = {"population": {"tier_mutation_prob": 0.0}, "run": {"days": 2}}

BASE_GOLDEN = {
    "status": "completed",
    "config_hash": "f589277f12656996537c99ed2f9968389a7d067d6bb94a5b6683ba201fbc1b51",
    "metrics": "2a14273e6ff02408e32eb9823ba48f8631f4a0293e4c31295fb3c321e06087db",
    "events": "3f9ddd5211be312b1f17d4af60967f3e0b2a5ed5da5ca61f8511597603993807",
    "calls": "8e3f88341f07b77eff388dbccc83737e01792e6398cdfb6d6eb13b772ae8c5dc",
    "ledger": "69cc1acf2f945851cd6d75ee5c73bf715048ae2366f340d9658728b9b5cd7b37",
}

AMNESIC_FOG_GOLDEN = {
    "status": "completed",
    "config_hash": "fb105920e6b0993e29971b43cf9f1c984343500dcb33fa8178b837499498c47d",
    "metrics": "912052045831afd8c6301e9455eaa8024cbc20e6bb71739da627a0a08849c6b9",
    "events": "75e82ce6cb6f8ee827b7293a303f35a15829efc68c589294df1420c6f3686294",
    "calls": "f266313eec9dc6d017a7b03951e90880d8ad5943d9958f0f1d69ce93233d7da1",
    "ledger": "61b2acd093d556e97364681d40ca452c4b623a9508cab9b1959579be5ff77905",
}


def test_a_base_run_is_byte_identical_to_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    got = fingerprint(REPO / "configs" / "base.yaml", deep_merge(NO_CHILD_PROVIDERS, {"run": {"name": "golden_base"}}),
                      tmp_path / "base", monkeypatch)
    assert got == BASE_GOLDEN


def test_the_amnesic_arm_under_fog_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    over = deep_merge(NO_CHILD_PROVIDERS, {"run": {"name": "golden_amnesic_fog"}, "world": {"view_radius": 12.0},
                                           "memory": {"enabled": False}})
    got = fingerprint(REPO / "configs" / "scripted_smoke.yaml", over, tmp_path / "amnesic", monkeypatch)
    assert got == AMNESIC_FOG_GOLDEN
