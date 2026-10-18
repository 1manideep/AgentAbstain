"""Backend follow-ups from the wave reviews, one check per change that moves behaviour.

* Provider brains offer ``share_practice`` exactly in worlds that offer it, and the hold's ``schema_tokens`` counts the
  schema they send; with practices off both are what they were (the request bodies are pinned in
  tests/test_live_requests.py).
* ``gossip_paraphrase_downgraded`` is a registered event kind (the registry guard in tests/test_event_registry.py).
* Utility calls store the whole response text when prompts are logged, like decide and maintain.
* Gate C4 reads ``policy_chars`` from the outcome registry (living agents' current policies).
* ``void mcp`` is described as a server over a vault directory.
* The run name seeds the id-keyed random streams, so transplant conditions at one seed differ by more than the
  transplant (void/research/transplant.py says so).

Scripted brains and offline fakes only: nothing here can reach a model API.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import NO_PROVIDER_TIERS, SMOKE, deep_merge
from test_brains import obs
from test_gemini_brain import fake_client, resp
from test_outcomes_memory_structure import World

from void.brain.base import Sampling
from void.brain.decision import SHARE_PRACTICE, share_practice_enabled, strict_json_schema
from void.brain.gemini_brain import GeminiBrain
from void.brain.prompt import estimate_tokens
from void.brain.utility_calls import make_chronicle_writer, make_paraphraser
from void.cli import LAZY_COMMANDS, main
from void.config import load_config
from void.culture import gossip
from void.events import Kind
from void.research import gates
from void.research.outcomes import memory_structure as ms
from void.sim.loop import Simulation

PRACTICES_ON: dict[str, Any] = {"memory": {"policy": {"enabled": True}, "practices": {"enabled": True}}}
GEMINI = "configs/live_gemini_ladder.yaml"


def _enum(schema: dict[str, Any]) -> list[str]:
    return schema["$defs"]["Action"]["properties"]["type"]["enum"]


def _schema_tokens(share_practice: bool) -> int:
    return estimate_tokens(json.dumps(strict_json_schema(share_practice=share_practice), separators=(",", ":")))


# --- (1) share_practice for provider brains -----------------------------------------------------------------------
def test_the_gemini_brain_offers_share_practice_only_where_the_world_does() -> None:
    share = {"thought": "teach", "memory_ops": [], "action": {"type": SHARE_PRACTICE, "target": "Bao", "text": "Forage early."}}
    for overrides, offered in (({}, False), (PRACTICES_ON, True)):
        cfg = load_config(GEMINI, overrides)
        assert share_practice_enabled(cfg) is offered
        client, models = fake_client(resp(json.dumps(share)))
        brain = GeminiBrain("average", cfg, client=client)
        r = asyncio.run(brain.decide(obs(tier="average"), Sampling(1.0, 0.2, False, api_temperature=0.5)))
        schema = models.calls[0]["config"]["response_json_schema"]
        assert schema == strict_json_schema(share_practice=offered)
        assert (SHARE_PRACTICE in _enum(schema)) is offered
        assert r.ok and r.decision.action.type == SHARE_PRACTICE  # the kernel, not the parser, decides validity


def test_schema_tokens_count_the_schema_the_provider_brains_send(tmp_path: Path) -> None:
    off = Simulation(load_config(SMOKE, deep_merge(NO_PROVIDER_TIERS, {"run": {"name": "schema_off"}})), tmp_path / "off")
    on = Simulation(load_config(SMOKE, deep_merge(NO_PROVIDER_TIERS, deep_merge(PRACTICES_ON, {"run": {"name": "schema_on"}}))),
                    tmp_path / "on")
    try:
        assert off.schema_tokens == _schema_tokens(False) == 587  # what every practices-off world was recorded with
        assert on.schema_tokens == _schema_tokens(True) > off.schema_tokens
    finally:
        off.db.close()
        on.db.close()


# --- (2) the downgrade event is a Kind ------------------------------------------------------------------------------
def test_the_paraphrase_downgrade_is_a_registered_kind() -> None:
    assert Kind.GOSSIP_PARAPHRASE_DOWNGRADED == "gossip_paraphrase_downgraded"
    assert gossip.GOSSIP_PARAPHRASE_DOWNGRADED == Kind.GOSSIP_PARAPHRASE_DOWNGRADED  # the old name still imports


# --- (3) utility calls keep the whole response when prompts are logged ----------------------------------------------
class LongReply:
    def __init__(self, text: str) -> None:
        self.text = text

    async def create(self, **_: Any) -> Any:
        u = SimpleNamespace(input_tokens=300, output_tokens=1800, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        r = SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)], stop_reason="end_turn", usage=u,
                            model="offline-model")
        r._request_id = "req_long"
        return r


@pytest.mark.parametrize("log_prompts", [True, False])
def test_utility_calls_store_the_whole_reply_when_prompts_are_logged(tmp_path: Path, log_prompts: bool) -> None:
    column = "Day 1: a quiet day\n" + "The north node stayed rich and nobody came. " * 120  # about 5400 characters
    assert len(column) > 4000
    cfg = load_config("configs/live_two_tier.yaml", {
        "run": {"days": 1, "tick_seconds": 0, "log_prompts": log_prompts}, "gossip": {"paraphrase": "llm"},
        "chronicle": {"writer": "llm"}, "agents": [{"name": "Ada", "tier": "budget"}, {"name": "Bao", "tier": "scripted"}]})
    fake = LongReply(column)
    sim = Simulation(cfg, tmp_path / "run", client=SimpleNamespace(messages=fake, beta=SimpleNamespace(messages=fake)))
    try:
        writer, para = make_chronicle_writer(sim), make_paraphraser(sim)
        assert writer is not None and para is not None
        assert asyncio.run(writer("Day 1: a quiet day\n")) == column.strip()
        ada = next(a for a in sim.registry.alive() if a.name == "Ada")
        assert asyncio.run(para(ada.agent_id, "The north node is [[Node n1]] and rich.", 1))
        rows = sim.db.fetchall("SELECT purpose, raw_text FROM llm_calls WHERE purpose IN ('chronicle', 'gossip') ORDER BY purpose")
        assert [r["purpose"] for r in rows] == ["chronicle", "gossip"]
        for r in rows:
            assert r["raw_text"] == (column if log_prompts else column[:4000])
    finally:
        sim.db.close()


# --- (4) gate C4 reads the registered policy_chars ------------------------------------------------------------------
def test_the_gate_reads_policy_chars_from_the_outcome_registry(tmp_path: Path) -> None:
    run, empty = tmp_path / "run", tmp_path / "empty"
    run.mkdir()
    w = World(run)
    w.agent("ag_a")
    w.agent("ag_b")
    w.agent("ag_c", status="bankrupt")
    w.policy("ag_a", "seed text", "four")
    w.policy("ag_b", "twelve chars")
    w.policy("ag_c", "a dead agent's long policy is not counted")
    w.close()
    assert gates.policy_chars(run) == ms.policy_chars(run) == pytest.approx((4 + 12) / 2)  # the living, current versions
    empty.mkdir()
    w = World(empty)
    w.agent("ag_a")
    w.close()
    assert gates.policy_chars(empty) is None  # the policy file was off: no versions
    assert gates.policy_chars(tmp_path / "nothing") is None  # no database
    assert gates.OUTCOMES[gates.TEXT_METRIC] is gates.policy_chars


# --- (6) the mcp help ----------------------------------------------------------------------------------------------
def test_the_mcp_command_is_described_as_a_server_over_a_vault(capsys: pytest.CaptureFixture[str]) -> None:
    assert LAZY_COMMANDS["mcp"] == ("void.server.mcp", "a Model Context Protocol server over a vault directory")
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "a Model Context Protocol server over a vault directory" in capsys.readouterr().out


# --- (7) what the run name seeds -----------------------------------------------------------------------------------
def test_two_run_names_at_one_seed_share_the_seed_draws_but_not_the_id_keyed_streams(tmp_path: Path) -> None:
    sims = [Simulation(load_config(SMOKE, deep_merge(NO_PROVIDER_TIERS, {"run": {"name": f"e5_eval_{c}", "seed": 7}})),
                       tmp_path / c) for c in ("none", "both")]
    try:
        a, b = (sorted(s.registry.alive(), key=lambda r: r.agent_id) for s in sims)  # roster order: the id's counter
        # drawn from the seed alone: node placement, founders' names, positions and personalities
        assert {k: (n.x, n.y) for k, n in sims[0].nodes.items()} == {k: (n.x, n.y) for k, n in sims[1].nodes.items()}
        assert [(r.name, r.pos.x, r.pos.y) for r in a] == [(r.name, r.pos.x, r.pos.y) for r in b]
        assert [sims[0].seed_of[r.agent_id] for r in a] == [sims[1].seed_of[r.agent_id] for r in b]
        # the ids carry the run name, and the scripted brain's stream is keyed on them
        assert not {r.agent_id for r in a} & {r.agent_id for r in b}
        draws = [[s.rng.stream("scripted", r.agent_id, 1).random() for r in rs] for s, rs in zip(sims, (a, b), strict=True)]
        assert all(x != y for x, y in zip(*draws, strict=True))
    finally:
        for s in sims:
            s.db.close()
