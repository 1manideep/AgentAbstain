import asyncio
from types import SimpleNamespace

from void.brain.anthropic_brain import AnthropicBrain
from void.brain.utility_calls import complete_text, make_chronicle_writer, make_paraphraser
from void.config import load_config
from void.sim.loop import Simulation


class FakeMessages:
    def __init__(self, text="Bao said the north node is [[Node n1]] and rich."):
        self.text = text
        self.calls = 0

    async def create(self, **kw):
        self.calls += 1
        u = SimpleNamespace(input_tokens=300, output_tokens=40, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        r = SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)], stop_reason="end_turn", usage=u, model="claude-haiku-4-5")
        r._request_id = "req_x"
        return r


def test_paraphrase_and_chronicle_are_metered_and_gated(tmp_path):
    cfg = load_config("configs/live_two_tier.yaml", {"run": {"days": 1, "tick_seconds": 0}, "gossip": {"paraphrase": "llm"}, "chronicle": {"writer": "llm"},
                                                     "agents": [{"name": "Ada", "tier": "budget"}, {"name": "Bao", "tier": "scripted"}]})
    fake = FakeMessages()
    client = SimpleNamespace(messages=fake, beta=SimpleNamespace(messages=fake))
    sim = Simulation(cfg, tmp_path / "run", client=client)
    ada = next(a for a in sim.registry.alive() if a.name == "Ada")
    bao = next(a for a in sim.registry.alive() if a.name == "Bao")
    para = make_paraphraser(sim)
    assert para is not None
    out = asyncio.run(para(ada.agent_id, "The north node is [[Node n1]] and rich.", 1))
    assert out and "[[Node n1]]" in out and fake.calls == 1
    row = sim.db.fetchone("SELECT purpose, real_cost, world_cost FROM llm_calls WHERE agent_id=?", (ada.agent_id,))
    assert row["purpose"] == "gossip" and row["real_cost"] > 0 and sim.wallet.balance(ada.agent_id) < 1_500_000
    # a scripted listener never triggers a paid call
    assert asyncio.run(para(bao.agent_id, "x", 1)) is None and fake.calls == 1
    # gated out (total cap reached) -> None, no call
    sim.db.kv_set("spend_total", sim.wallet.total_cap)
    assert asyncio.run(para(ada.agent_id, "y", 2)) is None and fake.calls == 1
    sim.db.kv_set("spend_total", 0)
    writer = make_chronicle_writer(sim)
    assert writer is not None
    text = asyncio.run(writer("Day 1: a quiet day\n"))
    assert text and fake.calls == 2
    assert sim.wallet.balance("chronicle") < 500_000
    assert sim.db.fetchone("SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='chronicle'")["n"] == 1


def test_complete_text_handles_errors():
    import anthropic
    import httpx2

    cfg = load_config("configs/live_two_tier.yaml")

    class Boom:
        async def create(self, **kw):
            raise anthropic.InternalServerError("x", response=httpx2.Response(500, request=httpx2.Request("POST", "http://x")), body=None)

    brain = AnthropicBrain("budget", cfg, client=SimpleNamespace(messages=Boom(), beta=SimpleNamespace(messages=Boom())))
    r = asyncio.run(complete_text(brain, "hi"))
    assert r.error and r.error.startswith("api_500") and r.raw_text == ""
