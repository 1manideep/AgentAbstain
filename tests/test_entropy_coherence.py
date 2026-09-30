import random

from void.brain import coherence, entropy
from void.brain.base import Sampling
from void.brain.decision import Action, Decision
from void.config import EntropyConfig, TierConfig


def tier(**kw):
    base = dict(provider="scripted", model="s", price_in_per_mtok=1, price_out_per_mtok=1,
                collapse_temperature=1.8, max_temperature=2.0, entropy_budget_max=100.0, supports_temperature=True)
    base.update(kw)
    return TierConfig(**base)


def test_coherence_orders_clean_above_garbled():
    clean = "I will move to the north node because the east one is thin and Bao said north is rich today."
    garbled = "node node node node node node node node node node node node node node node"
    assert coherence.score(clean) > 0.9
    assert coherence.score(garbled) < 0.2
    assert coherence.score("") == 0.0
    rng = random.Random(1)
    worse = coherence.degrade(clean, 1.0, rng)
    assert coherence.score(worse) < coherence.score(clean)
    assert coherence.degrade(clean, 0.0, rng) == clean


def test_degeneration_probability_curve():
    cfg = EntropyConfig()
    t = tier()
    assert entropy.degeneration_probability(cfg, t, 1.0) == 0.0
    assert abs(entropy.degeneration_probability(cfg, t, 1.8) - 0.15) < 1e-9
    assert abs(entropy.degeneration_probability(cfg, t, 2.0) - 0.90) < 1e-9
    assert abs(entropy.degeneration_probability(cfg, t, 1.9) - 0.525) < 1e-9


def test_step_drains_and_maps_temperature():
    cfg = EntropyConfig()
    t = tier()
    inp = entropy.DrainInputs(balance_ratio=1.0, last_action_failed=False, neighbours=0, weather=0.0, ticks_since_forage=0)
    b, s = entropy.step(cfg, t, 100.0, inp, random.Random(0))
    assert b == 99.0 and abs(s.stress - 0.01) < 1e-9 and not s.degenerate
    assert s.api_temperature is not None and 0 <= s.api_temperature <= 1
    stressed = entropy.DrainInputs(balance_ratio=0.0, last_action_failed=True, neighbours=5, weather=1.0, ticks_since_forage=None)
    b2, s2 = entropy.step(cfg, t, 10.0, stressed, random.Random(0))
    assert b2 == 0.0 and s2.stress == 1.0 and abs(s2.effective_temperature - 2.0) < 1e-9
    assert entropy.restore(cfg, t, 0.0) == 100.0
    b3, s3 = entropy.step(cfg, tier(supports_temperature=False), 100.0, inp, random.Random(0))
    assert s3.api_temperature is None


def test_corrupt_modes_are_deterministic_and_bounded():
    cfg = EntropyConfig()
    t = tier()
    d = Decision(thought="go north", memory_ops=[], action=Action(type="talk", target="ag_1", text="hello there friend"))
    s = Sampling(effective_temperature=1.95, stress=0.95, degenerate=True)
    kwargs = dict(available=["move", "forage", "talk", "idle"], world_size=60.0,
                  targets={"agents": ["ag_1"]}, previous_action=Action(type="forage"))
    out1, m1 = entropy.corrupt(d, s, t, cfg, random.Random(3), **kwargs)
    out2, m2 = entropy.corrupt(d, s, t, cfg, random.Random(3), **kwargs)
    assert out1 == out2 and m1 == m2
    modes = {entropy.corrupt(d, s, t, cfg, random.Random(i), **kwargs)[1] for i in range(40)}
    assert modes == {"random_action", "perseverate", "garble"}
    for i in range(40):
        out, mode = entropy.corrupt(d, s, t, cfg, random.Random(i), **kwargs)
        assert out.action.type in ("move", "forage", "talk", "idle")
        if out.action.text:
            assert len(out.action.text) <= 400
