"""Capability ladder: normal-distribution placement of a roster on tiers (void.intelligence, config, mutation)."""

from __future__ import annotations

import pytest

from void.agents.lifecycle import mutation_candidates
from void.config import load_config
from void.intelligence import assign, counts, rung_for, z_scores

NAMES = ["Ada", "Bao", "Cyra", "Dev", "Enzo", "Faye", "Gil", "Hex", "Ivo", "Juno"]
LADDER = ["genius", "sharp", "average", "slow", "dim"]


def hist(a: dict[str, str]) -> list[int]:
    return [len(n) for _, n in counts(a, LADDER)]


def test_ten_on_five_rungs_is_one_two_four_two_one_and_deterministic():
    a = assign(NAMES, LADDER, 42)
    assert hist(a) == [1, 2, 4, 2, 1] and set(a) == set(NAMES)
    assert a == assign(NAMES, LADDER, 42)
    assert hist(assign(NAMES, LADDER, 7)) == [1, 2, 4, 2, 1]
    # the pure bands give the same shape at n=10 (the tails are real, not forced)
    assert hist(assign(NAMES, LADDER, 42, extremes="band")) == [1, 2, 4, 2, 1]


def test_the_genius_rotates_with_the_seed_but_not_with_shuffle_off():
    geniuses = {next(n for n, t in assign(NAMES, LADDER, s).items() if t == "genius") for s in range(12)}
    assert len(geniuses) >= 4
    ordered = assign(NAMES, LADDER, 42, shuffle=False)
    assert ordered["Ada"] == "genius" and ordered["Juno"] == "dim" and [ordered[n] for n in NAMES] == sorted((ordered[n] for n in NAMES), key=LADDER.index)


def test_small_rosters_keep_one_genius_and_one_dunce_unless_asked_for_pure_bands():
    six = NAMES[:6]
    assert hist(assign(six, LADDER, 1)) == [1, 1, 2, 1, 1]
    assert hist(assign(six, LADDER, 1, extremes="band")) == [0, 2, 2, 2, 0]
    assert hist(assign(NAMES[:1], LADDER, 1)) == [0, 0, 1, 0, 0]


def test_mean_shift_pushes_the_roster_down_and_bands_are_symmetric():
    dumb = assign(NAMES, LADDER, 42, mean=-1.0)
    assert hist(dumb)[3] + hist(dumb)[4] > hist(dumb)[0] + hist(dumb)[1] and hist(dumb)[0] == 1
    zs = z_scores(10)
    assert zs == sorted(zs) and abs(zs[0] + zs[-1]) < 1e-12 and abs(zs[-1] - 1.6449) < 1e-3
    assert [rung_for(z, 5) for z in (1.6, 0.6, 0.0, -0.6, -1.6)] == [0, 1, 2, 3, 4] and rung_for(0.0, 1) == 0


def test_config_places_unassigned_agents_and_keeps_explicit_tiers():
    cfg = load_config("configs/demo.yaml")
    by_tier = {t: [a.name for a in cfg.agents if a.tier == t] for t in LADDER}
    assert [len(v) for v in by_tier.values()] == [1, 2, 4, 2, 1]
    assert cfg.intelligence_summary().startswith("genius=1 (")
    assert cfg.rung_of("genius") == 0 and cfg.rung_of("dim") == 4 and cfg.rung_of("scripted") is None
    pub = cfg.public_subset()
    assert pub["tiers"]["genius"]["rank"] == 0 and pub["tiers"]["scripted"]["rank"] is None and pub["intelligence"]["ladder"] == LADDER
    # an explicit tier is an override; the rest still follow the bell over the unassigned names only
    over = load_config("configs/demo.yaml", {"agents": [{"name": "Ada", "tier": "dim"}] + [{"name": n} for n in NAMES[1:]]})
    assert next(a.tier for a in over.agents if a.name == "Ada") == "dim"
    assert [len([a for a in over.agents if a.tier == t]) for t in LADDER] == [1, 2, 3, 2, 2]
    # the resolved roster is in the hash: a different seed places people differently and hashes differently
    other = load_config("configs/demo.yaml", {"run": {"seed": 43}})
    assert other.hash() != cfg.hash() and other.model_dump()["agents"] != cfg.model_dump()["agents"]
    assert all(a.tier is not None for a in cfg.model_copy().agents)


def test_config_rejects_missing_ladder_and_unknown_rungs():
    with pytest.raises(ValueError, match="need intelligence.ladder"):
        load_config("configs/base.yaml", {"agents": [{"name": "Ada"}]})
    with pytest.raises(ValueError, match="unknown tier"):
        load_config("configs/base.yaml", {"intelligence": {"ladder": ["genius", "nope"]}})
    with pytest.raises(ValueError, match="duplicate"):
        load_config("configs/base.yaml", {"intelligence": {"ladder": ["genius", "genius"]}})
    with pytest.raises(ValueError, match="thinking_budget"):
        load_config("configs/base.yaml", {"tiers": {"budget": {"thinking_budget": 5}}})
    with pytest.raises(ValueError, match="api_temperature_max"):
        load_config("configs/base.yaml", {"tiers": {"budget": {"api_temperature_max": 3.0}}})


def test_mutation_moves_one_rung_within_the_provider():
    cfg = load_config("configs/live_gemini_ladder.yaml")
    assert mutation_candidates(cfg, "genius") == ["sharp"]
    assert mutation_candidates(cfg, "average") == ["sharp", "slow"]
    assert mutation_candidates(cfg, "dim") == ["slow"]
    assert mutation_candidates(cfg, "frontier") == ["budget"]  # off the ladder: any other tier of the provider
    mixed = load_config("configs/live_gemini_ladder.yaml", {"intelligence": {"ladder": ["frontier", "sharp", "average", "slow", "dim"]}})
    assert mutation_candidates(mixed, "frontier") == [] and mutation_candidates(mixed, "sharp") == ["average"]
    demo = load_config("configs/demo.yaml")
    assert mutation_candidates(demo, "sharp") == ["genius", "average"] and demo.tiers["sharp"].provider == "scripted"
