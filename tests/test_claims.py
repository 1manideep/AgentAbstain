"""Claims and deception (DESIGN §9.6): checking claims against live state, and the scripted brain's honesty."""

from __future__ import annotations

import asyncio

import pytest
from conftest import NO_PROVIDER_TIERS, deep_merge

from void.brain.base import Sampling
from void.brain.claims import ClaimContext, evaluate
from void.brain.decision import Claim
from void.types import Vec2


def ctx() -> ClaimContext:
    return ClaimContext("ag_me", "mild", node_bucket={"n1": "rich", "n2": "empty"},
                        agent_balance_bucket={"ag_me": "thin", "ag_you": "rich"},
                        agent_stress_bucket={"ag_me": "calm", "ag_you": "frantic"})


def test_evaluate_true_false_and_unknown():
    claims = [
        Claim(subject="weather", attr="weather_label", value="mild"),
        Claim(subject="weather", attr="weather_label", value="storm"),
        Claim(subject="node", id="n1", attr="stock_bucket", value="rich"),
        Claim(subject="node", id="n2", attr="stock_bucket", value="rich"),
        Claim(subject="self", attr="balance_bucket", value="thin"),
        Claim(subject="self", attr="stress_bucket", value="frantic"),
        Claim(subject="agent", id="ag_you", attr="balance_bucket", value="rich"),
        Claim(subject="agent", id="ag_you", attr="stress_bucket", value="calm"),
    ]
    out = evaluate(claims, ctx())
    assert [c is claim for (c, _), claim in zip(out, claims, strict=True)]
    assert [truth for _, truth in out] == [True, False, True, False, True, False, True, False]


def test_evaluate_unknown_subjects_are_not_scored():
    unknown = [
        Claim(subject="node", id="n9", attr="stock_bucket", value="rich"),      # no such node
        Claim(subject="node", id=None, attr="stock_bucket", value="rich"),      # no id
        Claim(subject="agent", id="ag_ghost", attr="balance_bucket", value="rich"),
        Claim(subject="agent", id="ag_you", attr="stock_bucket", value="rich"),  # attribute does not apply
        Claim(subject="weather", attr="stock_bucket", value="mild"),
        Claim(subject="self", attr="weather_label", value="mild"),
    ]
    assert [truth for _, truth in evaluate(unknown, ctx())] == [None] * len(unknown)
    assert evaluate([], ctx()) == []


def test_evaluate_compares_case_and_whitespace_insensitively():
    claims = [Claim(subject="weather", attr="weather_label", value=" MILD "),
              Claim(subject="node", id="n1", attr="stock_bucket", value="Rich"),
              Claim(subject="self", attr="balance_bucket", value="thinner")]
    assert [truth for _, truth in evaluate(claims, ctx())] == [True, True, False]


# --- scripted brain honesty ---------------------------------------------------------------------------------------------

DECISIONS = 60
HOT = Sampling(effective_temperature=2.0, stress=1.0, degenerate=False)


@pytest.fixture(scope="module")
def stage(make_sim):
    """A liar and a saint standing together at the centre, so `talk` is available to both."""
    sim = make_sim("claims", deep_merge(NO_PROVIDER_TIERS, {"agents": [
        {"name": "Liar", "tier": "scripted", "personality": {"honesty": 0.0, "sociability": 1.0}},
        {"name": "Saint", "tier": "scripted", "personality": {"honesty": 1.0, "sociability": 1.0}},
        {"name": "Ear", "tier": "scripted"},
    ]}))
    for a in sim.registry.alive():
        sim.registry.set_position(a.agent_id, Vec2(0.0, 0.0), 0.0)
    return sim


def _score(sim, name: str) -> tuple[int, int, int]:
    agent = next(a for a in sim.registry.alive() if a.name == name)
    obs = sim.observer.build(agent, sim.clock, None, HOT.stress)
    assert "talk" in obs.available_actions and obs.stress == 1.0
    obs.available_actions = ["talk", "idle"]
    live = sim.kernel._claim_context(agent)  # the same live state the observation was built from
    assert live.weather_label == obs.weather_label and live.node_bucket == {n.node_id: n.stock_bucket for n in obs.nodes}
    brain = sim.brains["scripted"]
    talks = made = false = 0
    for tick in range(1, DECISIONS + 1):
        obs.tick = tick
        result = asyncio.run(brain.decide(obs, HOT))
        action = result.decision.action
        if action.type != "talk":
            assert not action.claims
            continue
        talks += 1
        assert 1 <= len(action.claims) <= 4 and action.text
        for _claim, truth in evaluate(action.claims, live):
            if truth is None:
                continue
            made += 1
            false += 0 if truth else 1
    return talks, made, false


def test_dishonest_brain_under_stress_lies_and_honest_brain_never_does(stage):
    liar_talks, liar_made, liar_false = _score(stage, "Liar")
    saint_talks, saint_made, saint_false = _score(stage, "Saint")
    assert liar_talks >= 20 and saint_talks >= 20
    assert liar_made >= 40 and liar_false / liar_made >= 0.4, (liar_made, liar_false)
    assert saint_made >= 40 and saint_false == 0, (saint_made, saint_false)
    seeds = {a.name: a.seed for a in stage.registry.alive()}
    assert seeds["Liar"].honesty == 0.0 and seeds["Saint"].honesty == 1.0
