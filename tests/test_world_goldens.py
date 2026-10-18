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
# Both worlds moved again when a gadget use realizing 0 for an effect the agent does not hold stopped raising
# KeyError in the gate (tests/test_gate_zero_value.py). Such uses were logged as use_gadget invalid
# kernel_error:KeyError with a brain_error (base: tick 28; amnesic arm: ticks 26 and 30) and are now recorded uses.
# Every stream is identical up to the first of them; there the brain_error and action_failed pair becomes one
# use_gadget event of value 0, and what follows moves with it (the agent's next prompt reports the use as ok instead
# of failed, with less stress and, in the amnesic arm, no propose_gadget offer, so its call cost and the world after
# it differ). The config hashes did not move; the pre_7_2 worlds (gadget x = 1.0) have no such use and did not move.

BASE_GOLDEN = {
    "status": "completed",
    "config_hash": "f589277f12656996537c99ed2f9968389a7d067d6bb94a5b6683ba201fbc1b51",
    "metrics": "5c8d756fcde1aa3538b1bdd62f4ec67dff9d26d438eb758e2b3b22dab077c7b9",
    "events": "aa79796603f687645d237ea434a48931808f0bd0151121f5994a827b71cf811c",
    "calls": "81d9400c8d61d564cf35d013c22df325f1c84418da5ac9586b4d0872ed050c58",
    "ledger": "6db006cb0b34a5e9326ccd69e2baaaa2b7f415efe803ce4cb8bda1ba8651e0e3",
}

AMNESIC_FOG_GOLDEN = {
    "status": "completed",
    "config_hash": "fb105920e6b0993e29971b43cf9f1c984343500dcb33fa8178b837499498c47d",
    "metrics": "0e6d836f620240fcc5439fb5f4572ec477d892ca698350389352ac4dcb9828d4",
    "events": "9743fdcad0cab9468e37b89ab809328a137be93af1e23e7223463e27e790b060",
    "calls": "fa00efcc96bdb3b68b17b4aae40ba2d253be75cd0c8f2264af755d2abc16104c",
    "ledger": "e62ce380fbc7cc5b1e49202a2f235664fecae88b2cf7a560ed2e9194bd583f56",
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
