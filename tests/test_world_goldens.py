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
random gadget parameters, sleep when calls burn more than a forage earns): both worlds now have births. Run under
``population.scripted_rules: pre_7_2`` they still reproduce the digests from before (tests/test_scripted_selection.py).
Re-recorded again when the kernel began classifying failed actions as invalid or stale against the agent's
observation (see the note above BASE_GOLDEN).
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
# The amnesic arm's "calls" digest also moved when decision responses stopped being cut at 4000 characters under
# run.log_prompts (full response text per call, MEMORY_EVOLUTION §7.4); cutting every raw_text back to 4000
# characters gives the digest recorded before that change exactly. The base world has no response over 4000
# characters, so its "calls" digest held.
# Every digest moved, deliberately, when the kernel began classifying a failed action against the observation the
# agent decided on (DESIGN §5, §7.4): stale only if the observation showed the action valid, invalid (failed_action
# drain, invalid_action_rate) if it already showed it could not work. First difference in the base world: a
# use_gadget refused for its fee at tick 35 by an agent whose observed balance could not cover it above the reserve
# (stale -> invalid); in the amnesic fog world: at tick 2 a talk to an inhabitant in view at 9.23 units, beyond the
# 4-unit earshot (stale -> invalid). Kernel.apply without an observation keeps the old kinds, and with the loop's
# observation dropped both worlds give the digests recorded before exactly (base metrics 8b7a0f7a..., events
# 7b86641d..., calls 8cbe69f9..., ledger 7270da62...; amnesic fog metrics aceec850..., events 4c285707..., calls
# e5a71250..., ledger 53cef135...).

BASE_GOLDEN = {
    "status": "completed",
    "config_hash": "f589277f12656996537c99ed2f9968389a7d067d6bb94a5b6683ba201fbc1b51",
    "metrics": "cec0964c193d53516c44d98fb3f4384c05d43bcbe02668a87c9c52994e4fd911",
    "events": "704f63314cf34e28d075cfeb54f24adb9388ac5a87108e72020c6721e0cd9e7b",
    "calls": "2ee87969e26d3ddfd3f604db69824e36c6080aa667ae0f94abfa4112e5fdb133",
    "ledger": "50facf0e1aa21271f3f2fe89524385baeadc6d5d37fb7203af1ab1c2d7f74aa6",
}

AMNESIC_FOG_GOLDEN = {
    "status": "completed",
    "config_hash": "fb105920e6b0993e29971b43cf9f1c984343500dcb33fa8178b837499498c47d",
    "metrics": "c438467e7549a2d2b6298018164f70da672daef2fb90beed329c1163cb90fb05",
    "events": "35ca27dbe83803f3915a3b4d7edbdcbe0087b6df76af55c92d82730e99d03c6c",
    "calls": "494b379794d2328fa4eba495063cd5c77010b57b6874a575776190a4f4a75973",
    "ledger": "ac3153bdd45825b70fd85f577de947d9ae36b7347a48667663d1f1c312389137",
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
