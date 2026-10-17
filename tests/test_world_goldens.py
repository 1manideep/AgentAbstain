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

Re-recorded once, deliberately, when the scripted brain started to follow DESIGN §7.2 (offspring from
2 x min_reserve + endowment under per-parent names, greed-scaled transfers, nudges toward yield-positive weather,
random gadget parameters, sleep when calls burn more than a forage earns): both worlds now have births.
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

# The metrics digests were re-recorded when metrics rows gained keys (tick gadget fields, the full by_tier /
# by_generation fields with stress_bins, day link_density / vault / vault_by_agent). That change was checked to be
# purely additive: removing the new keys from the new rows gives back the old rows exactly; nothing else moved.
# The DESIGN §7.2 scripted brain then moved every digest (births in both worlds); its metrics digests were recorded
# on top of the added keys, and stripping those keys from these rows gives back the §7.2 rows recorded without them.

BASE_GOLDEN = {
    "status": "completed",
    "config_hash": "f589277f12656996537c99ed2f9968389a7d067d6bb94a5b6683ba201fbc1b51",
    "metrics": "8b7a0f7a23744324bff96edc96f5c2f3ef08d9ad412ae786aaeaf722f86bbbd8",
    "events": "7b86641db05416f9e6777345858843f861e952465fea8623ecb28a5081fc4038",
    "calls": "8cbe69f9a6789fb9a9f14a703bdd5ae15dc5af98636f2868b5013580e92660e8",
    "ledger": "7270da6251493fa8fef512b3eb91b864d03c3f873a1c0eebe3e7c2791c29cd86",
}

AMNESIC_FOG_GOLDEN = {
    "status": "completed",
    "config_hash": "fb105920e6b0993e29971b43cf9f1c984343500dcb33fa8178b837499498c47d",
    "metrics": "aceec8505aae9bb3c3ebb685df68af7f53c882f738faa09d3e9dd799ca08871e",
    "events": "4c2857075a953610723d500bca16e048f78b2d80dc6c370cc63582aaed0a11fb",
    "calls": "e5a71250b2af8c467a1e7c9cce6b400851cb464f6c49016b2108cc29809ff954",
    "ledger": "53cef13500522cca21b3096354dcf1f1f0a30da269d27fa5488a11e0f8ea61f6",
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
