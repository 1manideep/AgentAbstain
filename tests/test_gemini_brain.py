"""GeminiBrain: request shape, usage accounting, finish reasons, error classes, thinking fallback, utility calls.

No network: a fake ``client.aio.models.generate_content`` records the request and returns canned responses.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from google.genai import errors as genai_errors
from test_brains import obs

from void.brain import entropy
from void.brain.base import Sampling
from void.brain.gemini_brain import GeminiBrain
from void.brain.utility_calls import complete_text, make_chronicle_writer, make_paraphraser
from void.config import EntropyConfig, TierConfig, load_config
from void.economy.pricing import hold_micro, real_cost_micro
from void.sim.loop import Simulation


class FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def generate_content(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def fake_client(*responses):
    models = FakeModels(responses)
    return SimpleNamespace(aio=SimpleNamespace(models=models)), models


def resp(text, finish="STOP", *, prompt=1200, cached=0, out=80, thoughts=0, block=None, no_candidates=False):
    usage = SimpleNamespace(prompt_token_count=prompt, cached_content_token_count=cached, candidates_token_count=out,
                            thoughts_token_count=thoughts, total_token_count=prompt + out + thoughts)
    cand = SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text="thinking...", thought=True),
                                                          SimpleNamespace(text=text, thought=False)]),
                           finish_reason=SimpleNamespace(name=finish))
    feedback = SimpleNamespace(block_reason=SimpleNamespace(name=block)) if block else None
    return SimpleNamespace(candidates=[] if no_candidates else [cand], usage_metadata=usage, model_version="gemini-2.5-flash-lite-001",
                           response_id="resp_1", prompt_feedback=feedback)


GOOD = json.dumps({"thought": "north", "memory_ops": [], "action": {"type": "move", "x": 1, "y": 2}})


def brain_for(tier, *responses):
    cfg = load_config("configs/live_gemini_ladder.yaml")
    client, models = fake_client(*responses)
    return GeminiBrain(tier, cfg, client=client), models, cfg


def test_request_shape_usage_and_parsing():
    brain, models, cfg = brain_for("genius", resp(GOOD, prompt=1200, cached=900, out=80, thoughts=120))
    r = asyncio.run(brain.decide(obs(tier="genius"), Sampling(1.9, 0.9, False, api_temperature=1.7)))
    assert r.ok and r.decision.action.type == "move" and r.request_id == "resp_1" and r.model.startswith("gemini-2.5-flash-lite")
    # prompt_token_count includes the cached part; thinking tokens are output
    assert (r.usage.input_tokens, r.usage.cache_read_tokens, r.usage.output_tokens, r.usage.cache_write_tokens) == (300, 900, 200, 0)
    assert r.usage.attempts == [("gemini-2.5-flash-lite-001", r.usage.attempts[0][1])]
    kw = models.calls[0]
    assert kw["model"] == "gemini-3.1-pro" and "Tick 5" in kw["contents"]
    c = kw["config"]
    assert c["max_output_tokens"] == 1536 and c["candidate_count"] == 1 and c["temperature"] == 1.7
    assert c["response_mime_type"] == "application/json" and c["response_json_schema"]["type"] == "object"
    assert c["system_instruction"].startswith("You are an inhabitant of the Void") and "genius" in c["system_instruction"]
    assert c["thinking_config"] == {"thinking_budget": 2048, "include_thoughts": False}
    # served model priced at its own table rate (a fallback-style attempt list), not the genius tier's
    assert real_cost_micro(r.usage, cfg.tiers["genius"], cfg.tiers) == 300 * 0.10 + 900 * 0.01 + 200 * 0.40


def test_thinking_config_is_dropped_after_a_400_and_never_sent_again():
    bad = genai_errors.ClientError(400, {"error": {"code": 400, "message": "thinking_config is not supported for this model", "status": "INVALID_ARGUMENT"}})
    brain, models, _ = brain_for("sharp", bad, resp(GOOD), resp(GOOD))
    s = Sampling(1.0, 0.2, False, api_temperature=0.6)
    assert asyncio.run(brain.decide(obs(tier="sharp"), s)).ok
    assert "thinking_config" in models.calls[0]["config"] and "thinking_config" not in models.calls[1]["config"]
    assert asyncio.run(brain.decide(obs(tier="sharp"), s)).ok and len(models.calls) == 3 and "thinking_config" not in models.calls[2]["config"]


def test_dim_tier_sends_no_temperature_above_its_cap_and_zero_thinking():
    brain, models, _ = brain_for("dim", resp(GOOD))
    asyncio.run(brain.decide(obs(tier="dim"), Sampling(2.0, 1.0, False, api_temperature=2.0)))
    c = models.calls[0]["config"]
    assert c["temperature"] == 2.0 and c["thinking_config"] == {"thinking_budget": 0, "include_thoughts": False} and c["max_output_tokens"] == 320


def test_finish_reasons_refusal_block_and_truncation():
    brain, _, _ = brain_for("average", resp("", finish="SAFETY"), resp("", block="SAFETY", no_candidates=True),
                            resp("", no_candidates=True), resp("{\"thought\": \"cut", finish="MAX_TOKENS"))
    s = Sampling(1.0, 0.2, False, api_temperature=0.5)
    r1 = asyncio.run(brain.decide(obs(tier="average"), s))
    assert r1.decision is None and r1.stop_reason == "refusal" and r1.error == "refusal:safety" and r1.usage.total > 0
    r2 = asyncio.run(brain.decide(obs(tier="average"), s))
    assert r2.error == "refusal:prompt_safety"
    r3 = asyncio.run(brain.decide(obs(tier="average"), s))
    assert r3.error == "refusal:no_candidates"
    r4 = asyncio.run(brain.decide(obs(tier="average"), s))
    assert r4.decision is None and r4.stop_reason == "max_tokens" and r4.error.startswith("parse")


def test_api_errors_map_to_the_shared_error_classes():
    quota = genai_errors.ClientError(429, {"error": {"code": 429, "message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}})
    boom = genai_errors.ServerError(500, {"error": {"code": 500, "message": "internal", "status": "INTERNAL"}})
    brain, _, _ = brain_for("slow", quota, boom, TimeoutError("slow"), RuntimeError("no credentials"))
    s = Sampling(1.0, 0.2, False, api_temperature=0.5)
    errs = [asyncio.run(brain.decide(obs(tier="slow"), s)).error for _ in range(4)]
    assert errs[0].startswith("rate_limit:") and errs[1].startswith("api_500:") and errs[2].startswith("timeout:")
    assert errs[3].startswith("client: RuntimeError")


def test_lazy_client_reports_missing_credentials_as_client_error(monkeypatch):
    cfg = load_config("configs/live_gemini_ladder.yaml")
    brain = GeminiBrain("average", cfg)  # no client, no key in the environment (conftest strips it)
    r = asyncio.run(brain.decide(obs(tier="average"), Sampling(1.0, 0.2, False, api_temperature=0.5)))
    assert r.decision is None and r.error.startswith("client:") and r.usage.total == 0


def test_hold_reserves_thinking_tokens_and_entropy_maps_to_the_provider_range():
    cfg = load_config("configs/live_gemini_ladder.yaml", {"economy": {"world_pricing": "real", "min_call_reserve_usd": 0.0}})
    genius = cfg.tiers["genius"]
    no_thinking = genius.model_copy(update={"thinking_budget": 0})
    assert hold_micro(genius, cfg.economy, 1000) - hold_micro(no_thinking, cfg.economy, 1000) == 2048 * 12  # 2048 tokens at $12/MTok
    eq = load_config("configs/live_gemini_ladder.yaml")  # equalized world price: the hold bounds the larger of the two prices
    assert hold_micro(genius, eq.economy, 1000) >= hold_micro(genius, cfg.economy, 1000)
    t = TierConfig(provider="gemini", model="g", price_in_per_mtok=1, price_out_per_mtok=1, supports_temperature=True, api_temperature_max=2.0,
                   collapse_temperature=1.8, max_temperature=2.0, entropy_budget_max=100.0)
    stressed = entropy.DrainInputs(balance_ratio=0.0, last_action_failed=True, neighbours=5, weather=1.0, ticks_since_forage=None)
    _, s = entropy.step(EntropyConfig(), t, 10.0, stressed, __import__("random").Random(0))
    assert s.stress == 1.0 and abs(s.api_temperature - 2.0) < 1e-9
    calm = entropy.DrainInputs(balance_ratio=1.0, last_action_failed=False, neighbours=0, weather=0.0, ticks_since_forage=0)
    _, s0 = entropy.step(EntropyConfig(), t, 100.0, calm, __import__("random").Random(0))
    assert abs(s0.api_temperature - 2.0 * s0.effective_temperature / 2.0) < 1e-9 and s0.api_temperature < 1.0


def test_utility_calls_run_on_a_gemini_tier(tmp_path):
    cfg = load_config("configs/live_gemini_ladder.yaml", {"run": {"days": 1, "tick_seconds": 0}, "gossip": {"paraphrase": "llm"},
                                                          "chronicle": {"writer": "llm"}, "intelligence": {"ladder": []},
                                                          "agents": [{"name": "Ada", "tier": "average"}, {"name": "Bao", "tier": "scripted"}]})
    client, models = fake_client(resp("Bao said the north node is [[Node n1]] and rich.", out=40),
                                 resp("Day 1: a quiet day\nNothing happened.", out=60))
    sim = Simulation(cfg, tmp_path / "run", gemini_client=client)
    ada = next(a for a in sim.registry.alive() if a.name == "Ada")
    bao = next(a for a in sim.registry.alive() if a.name == "Bao")
    para = make_paraphraser(sim)
    assert para is not None
    out = asyncio.run(para(ada.agent_id, "The north node is [[Node n1]] and rich.", 1))
    assert out and "[[Node n1]]" in out and len(models.calls) == 1
    c = models.calls[0]["config"]
    assert "response_json_schema" not in c and "system_instruction" not in c and c["max_output_tokens"] == 200
    row = sim.db.fetchone("SELECT purpose, provider, model, real_cost FROM llm_calls WHERE agent_id=?", (ada.agent_id,))
    assert row["purpose"] == "gossip" and row["provider"] == "gemini" and row["real_cost"] > 0 and row["model"].startswith("gemini-2.5")
    assert asyncio.run(para(bao.agent_id, "x", 1)) is None and len(models.calls) == 1  # scripted listener: never a paid call
    writer = make_chronicle_writer(sim)
    assert writer is not None and asyncio.run(writer("Day 1: a quiet day\n")) and len(models.calls) == 2
    assert sim.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='chronicle'")["n"] == 1


def test_complete_text_on_gemini_never_raises():
    brain, _, _ = brain_for("average", genai_errors.ServerError(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}}))
    r = asyncio.run(complete_text(brain, "hi"))
    assert r.error.startswith("api_503") and r.raw_text == "" and r.usage.total == 0
