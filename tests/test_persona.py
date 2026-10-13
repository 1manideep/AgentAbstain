"""The persona layer: an agent's character reaches its brain only when asked for, and the roster carries traits."""

from __future__ import annotations

import json

import pytest

from void.agents.models import TRAITS, PersonalitySeed, trait_level
from void.brain.prompt import render_observation
from void.config import load_config
from void.server.protocol import parse_message

NO_MUTATION = {"population": {"tier_mutation_prob": 0.0}}


def test_trait_levels_and_rounding() -> None:
    assert (trait_level(0.1), trait_level(0.5), trait_level(0.9)) == ("low", "moderate", "high")
    seed = PersonalitySeed(greed=0.123456, honesty=0.98765)
    assert seed.traits()["greed"] == 0.12 and seed.traits()["honesty"] == 0.99 and set(seed.traits()) == set(TRAITS)


def test_persona_hidden_by_default(run_sim) -> None:
    run = run_sim("persona_hidden", {**NO_MUTATION, "run": {"days": 1, "ticks_per_day": 6}})
    assert run.cfg.population.persona == "hidden"
    for r in run.db.fetchall("SELECT prompt_text, observation FROM llm_calls WHERE purpose='decide'"):
        assert "## Who you are" not in r["prompt_text"]
        assert json.loads(r["observation"])["persona"] is None


def test_persona_shown_renders_temperament_lineage_and_fenced_motto(run_sim) -> None:
    run = run_sim("persona_shown", {**NO_MUTATION, "run": {"days": 1, "ticks_per_day": 6}, "population": {"persona": "shown"},
                                    "agents": [{"name": n, "tier": "scripted", "personality": {"greed": 0.9, "honesty": 0.1}} for n in ("Ada", "Bao")]})
    rows = run.db.fetchall("SELECT agent_id, prompt_text, observation FROM llm_calls WHERE purpose='decide'")
    assert rows
    for r in rows:
        text = r["prompt_text"]
        assert "## Who you are" in text and "greed high" in text and "honesty low" in text and "a founder of this world" in text
        persona = json.loads(r["observation"])["persona"]
        assert persona["traits"]["greed"] == 0.9 and persona["parent_name"] is None
        assert f"Your motto: <<quoted>>{persona['motto']}<<end>>" in text
        # the section sits before Self so the identity precedes the agent's own summary
        assert text.index("## Who you are") < text.index("## Self")


def test_child_persona_names_its_parent(make_sim) -> None:
    sim = make_sim("persona_child", {**NO_MUTATION, "population": {"persona": "shown"}})
    parent = sim.registry.alive()[0]
    from void.config import usd_to_micro

    sim.wallet.credit(parent.agent_id, 1, usd_to_micro(5.0), "test_grant")
    parent = sim.registry.get(parent.agent_id)
    outcome = sim.lifecycle.create_offspring(parent, "Kid", usd_to_micro(1.0), 1, 1, hold_min=1000)
    assert outcome.ok, outcome.reason
    child = sim.registry.get(str(outcome.effects["child_id"]))
    obs = sim.observer.build(child, sim.clock, None, 0.0)
    assert obs.persona is not None and obs.persona.generation == 1 and obs.persona.parent_name == parent.name
    assert f"generation 1, child of {parent.name}" in render_observation(obs)


def test_roster_carries_traits_and_motto(make_sim) -> None:
    sim = make_sim("persona_roster", NO_MUTATION)
    roster = sim.roster()
    assert roster and all(set(a["traits"]) == set(TRAITS) for a in roster)
    assert all(0.0 <= v <= 1.0 for a in roster for v in a["traits"].values())
    assert all(isinstance(a["motto"], str) and a["motto"] for a in roster)
    msg = parse_message({"type": "roster", "agents": roster})
    assert msg.agents[0].traits == roster[0]["traits"]


@pytest.mark.parametrize("value", ["hidden", "shown"])
def test_persona_config_values(value: str) -> None:
    cfg = load_config("configs/scripted_smoke.yaml", {"population": {"persona": value}})
    assert cfg.population.persona == value
