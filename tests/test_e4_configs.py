"""E4, who benefits (MEMORY_EVOLUTION §6.6): the capability ladder crossed with default versus self-edit, plus self-edit
with the size-dependent tariff removed. The live configs (``exp_mem_e4_*``) are only loaded here, never run; their
scripted mirrors (``exp_mem_e4s_*``) run one seed for one day and ``compare`` must accept them.

Checks: every config loads without warnings; the live arms roster base.yaml's provider rungs by name, set a total cap,
carry a cost comment tagged ``[estimate]`` and name no model; the arms differ only in the declared independent
variables (:func:`~void.research.compare.config_diff`, :func:`~void.research.compare.covered_by`), and each mirror arm
differs from its live arm only in the rungs, the caps and its own flat price; the flat tariff prices a call by
its prompt alone, whatever the rung thinks or writes, while the real cost is unchanged; and on the mirror that holds
call by call, end to end, for decisions and (on a short two-day run, since maintenance starts on day 2) for the
nightly maintenance call.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import sqlite3
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
import yaml

from void.brain.base import Usage
from void.config import TierConfig, VoidConfig, load_config
from void.economy.catalog import MODEL_PRICES
from void.economy.pricing import real_cost_micro, world_cost_micro
from void.research.analysis import resolve_path
from void.research.compare import ALWAYS_ALLOWED, compare, config_diff, covered_by, render
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"
LIVE, MIRROR = "exp_mem_e4", "exp_mem_e4s"
ARMS = ("default", "selfedit", "flat_tariff")
LADDER = ["genius", "sharp", "average", "slow", "dim"]
PLACEMENT = {"genius": 1, "sharp": 2, "average": 4, "slow": 2, "dim": 1}
MEMORY_IVS = ("memory.policy.enabled", "memory.maintenance.enabled")
PRICE_IVS = ("economy.equalized_price_in_per_mtok", "economy.equalized_price_out_per_mtok",
             "economy.equalized_price_cache_read_per_mtok", "economy.equalized_price_cache_write_per_mtok")


def path(family: str, arm: str) -> Path:
    return CONFIGS / f"{family}_{arm}.yaml"


def cfg(family: str, arm: str, overrides: dict[str, Any] | None = None) -> VoidConfig:
    return load_config(path(family, arm), overrides)


def dump(family: str, arm: str) -> dict[str, Any]:
    return cfg(family, arm).model_dump(mode="json")


def _providers(c: VoidConfig) -> set[str]:
    """Providers other than ``scripted`` that the roster would call."""
    return {c.tiers[a.tier].provider for a in c.agents if a.tier} - {"scripted"}


def comments(p: Path) -> str:
    return "\n".join(line for line in p.read_text(encoding="utf-8").splitlines() if line.lstrip().startswith("#"))


# --- loading ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("family", [LIVE, MIRROR])
def test_every_config_loads_without_warnings(family: str) -> None:
    base = cfg(family, "base")
    assert base.warnings() == [] and base.experiment.arm is None and base.experiment.name == family
    for arm in ARMS:
        c = cfg(family, arm)
        assert c.warnings() == [], (family, arm, c.warnings())
        assert (c.experiment.name, c.experiment.arm, c.experiment.primary_outcome) == (family, arm, "mean_balance")
        assert c.run.name == f"{family}_{arm}"
        assert c.entropy.degeneration.mode == "observe" and c.economy.world_pricing == "equalized"
        assert c.population.tier_mutation_prob == 0.0
        assert c.intelligence.ladder == LADDER
        assert all(a.placed for a in c.agents)
        assert Counter(a.tier for a in c.agents) == PLACEMENT
        assert c.world.view_radius is not None and c.memory.exam.enabled
        assert [f"by_tier.{t}.mean_balance" for t in LADDER] == c.experiment.secondary_outcomes[:5]


def test_live_arms_roster_base_rungs_by_name_under_a_total_cap() -> None:
    base = load_config(CONFIGS / "base.yaml")
    raw = yaml.safe_load(path(LIVE, "base").read_text(encoding="utf-8"))
    assert raw["extends"] == "base.yaml" and "tiers" not in raw  # the rungs are base.yaml's, by name
    assert raw["economy"]["total_cap_usd"] > 0
    assert "[estimate" in comments(path(LIVE, "base")) and "[estimate" in comments(path(LIVE, "flat_tariff"))
    for arm in ARMS:
        c = cfg(LIVE, arm)
        assert all(c.tiers[t] == base.tiers[t] and c.tiers[t].provider != "scripted" for t in LADDER), arm
        assert 0 < c.economy.total_cap_usd <= 20.0 and c.economy.total_cap_usd >= c.run.days * c.economy.daily_cap_usd
        assert _providers(c) and not _providers(cfg(MIRROR, arm)), arm  # the mirror never calls a provider


def test_no_model_identifier_in_any_e4_config() -> None:
    base = load_config(CONFIGS / "base.yaml")
    models = {t.model.lower() for t in base.tiers.values() if t.provider != "scripted"} | {m.lower() for m in MODEL_PRICES}
    for family in (LIVE, MIRROR):
        for arm in ("base", *ARMS):
            text = path(family, arm).read_text(encoding="utf-8").lower()
            assert not [m for m in sorted(models) if m in text], (family, arm)


# --- independent variables ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("family", [LIVE, MIRROR])
def test_arms_differ_only_in_declared_independent_variables(family: str) -> None:
    dumps = {arm: dump(family, arm) for arm in ARMS}
    declared = {arm: resolve_path(d, "experiment.independent_variables") for arm, d in dumps.items()}
    assert all(v == declared["default"] for v in declared.values())
    ivs = list(declared["default"])
    assert sorted(ivs) == sorted(MEMORY_IVS + PRICE_IVS)
    for a, b in itertools.combinations(ARMS, 2):
        diff = config_diff(dumps[a], dumps[b])
        stray = [p for p in diff if p not in ALWAYS_ALLOWED and not covered_by(p, ivs)]
        assert not stray, (family, a, b, stray)
        rest = {p for p in diff if p not in ALWAYS_ALLOWED}
        # the memory condition separates default from the two self-edit arms; the tariff separates flat_tariff from
        # both (always the output price; the input prices may coincide, as on the mirror)
        assert rest & set(MEMORY_IVS) == (set(MEMORY_IVS) if "default" in (a, b) else set()), (family, a, b, diff)
        prices = rest & set(PRICE_IVS)
        assert ("economy.equalized_price_out_per_mtok" in prices) == ("flat_tariff" in (a, b)) == bool(prices), (family, a, b)


def test_each_mirror_arm_differs_from_its_live_arm_only_in_rungs_caps_and_flat_price() -> None:
    allowed = ("run.name", "experiment.name", "economy.daily_cap_usd", "economy.total_cap_usd")
    for arm in ARMS:
        live, mirror = dump(LIVE, arm), dump(MIRROR, arm)
        rungs = [f"tiers.{t}" for t in LADDER]
        prices = list(PRICE_IVS) if arm == "flat_tariff" else []
        stray = [p for p in config_diff(live, mirror) if p not in allowed and not covered_by(p, rungs + prices)]
        assert not stray, (arm, stray)
        for iv in MEMORY_IVS:
            assert resolve_path(live, iv) == resolve_path(mirror, iv), (arm, iv)
        for d in (live, mirror):
            p_in, p_out, p_cr, p_cw = (resolve_path(d, iv) for iv in PRICE_IVS)
            if arm == "flat_tariff":
                assert p_out == 0.0 and p_in == p_cr == p_cw > 0, (arm, p_in, p_out, p_cr, p_cw)
            else:
                assert (p_in, p_out, p_cr, p_cw) == (4.0, 20.0, 0.20, 5.0), arm  # E1's prices, the config defaults
    assert cfg(LIVE, "selfedit").memory.policy.enabled and cfg(LIVE, "selfedit").memory.maintenance.enabled
    assert cfg(LIVE, "selfedit").memory.maintenance.rights == ["notes", "policy", "structure"]


def test_mirror_rungs_differ_only_in_hidden_capability() -> None:
    c = cfg(MIRROR, "selfedit")
    rungs = [c.tiers[t] for t in LADDER]
    assert all(r.provider == "scripted" and r.maintenance_strategy == "index_builder" for r in rungs)
    betas = [r.scripted_beta for r in rungs]
    assert betas == sorted(betas, reverse=True) and len(set(betas)) == len(betas)
    hidden = {"scripted_beta", "gadget_defect_rate", "color"}
    dumped = [{k: v for k, v in r.model_dump().items() if k not in hidden} for r in rungs]
    assert all(d == dumped[0] for d in dumped)


# --- the tariff --------------------------------------------------------------------------------------------------
def _usages(tier: TierConfig) -> list[Usage]:
    """Three calls shown the same 2.5k-token prompt: no thinking and nothing cached; the rung's whole thinking budget
    with the system prompt read from the cache; the whole output cap with the system prompt written to it."""
    think = int(tier.thinking_budget or 0)
    return [Usage(input_tokens=2500, output_tokens=250),
            Usage(input_tokens=1300, cache_read_tokens=1200, output_tokens=250 + think),
            Usage(input_tokens=1300, cache_write_tokens=1200, output_tokens=tier.max_tokens + think)]


def test_flat_tariff_prices_a_call_by_its_prompt_alone_and_leaves_real_cost_unchanged() -> None:
    selfedit, flat = cfg(LIVE, "selfedit"), cfg(LIVE, "flat_tariff")
    p_in = Fraction(str(flat.economy.equalized_price_in_per_mtok))
    flat_costs = {t: [world_cost_micro(u, flat.tiers[t], flat.economy) for u in _usages(flat.tiers[t])] for t in LADDER}
    assert {c for costs in flat_costs.values() for c in costs} == {math.ceil(2500 * p_in)}  # every rung, every call
    tariff = {t: world_cost_micro(_usages(selfedit.tiers[t])[1], selfedit.tiers[t], selfedit.economy) for t in LADDER}
    assert tariff["genius"] > tariff["sharp"] > tariff["average"] == tariff["dim"]  # who thinks pays more per call
    for t in LADDER:
        for u in _usages(selfedit.tiers[t]):
            assert real_cost_micro(u, flat.tiers[t]) == real_cost_micro(u, selfedit.tiers[t]) > 0


# --- the scripted mirrors, end to end ------------------------------------------------------------------------------
def _run(family_arm: tuple[str, str], out: Path, overrides: dict[str, Any]) -> Path:
    family, arm = family_arm
    run_dir = out / arm / "seed_1"
    sim = Simulation(cfg(family, arm, {"run": {"seed": 1, **overrides}}), run_dir)
    try:
        asyncio.run(sim.run())
    finally:
        sim.db.close()
    return run_dir


@pytest.fixture(scope="module")
def mirror_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("exp_mem_e4s")
    for arm in ARMS:
        _run((MIRROR, arm), out, {"days": 1})
    return out


def _query(run_dir: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(run_dir / "world.db"))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _ceil(row: sqlite3.Row, p_in: float, p_out: float, p_cr: float, p_cw: float) -> int:
    """The exact micro-dollar price of a logged call's tokens (USD per MTok is micro-dollars per token), rounded up."""
    total = (int(row["input_tokens"]) * Fraction(str(p_in)) + int(row["output_tokens"]) * Fraction(str(p_out))
             + int(row["cache_read_tokens"]) * Fraction(str(p_cr)) + int(row["cache_write_tokens"]) * Fraction(str(p_cw)))
    return math.ceil(total)


def _check_billing(run_dir: Path, c: VoidConfig, purpose: str) -> int:
    """Every ``purpose`` call is billed in-world at the arm's equalized prices and in real money at its tier's."""
    e = c.economy
    rows = _query(run_dir, "SELECT tier, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, world_cost, "
                           "real_cost FROM llm_calls WHERE purpose=?", (purpose,))
    for r in rows:
        t = c.tiers[r["tier"]]
        assert r["world_cost"] == _ceil(r, e.equalized_price_in_per_mtok, e.equalized_price_out_per_mtok,
                                        e.equalized_price_cache_read_per_mtok, e.equalized_price_cache_write_per_mtok)
        assert r["real_cost"] == _ceil(r, t.price_in_per_mtok, t.price_out_per_mtok, t.price_cache_read_per_mtok,
                                       t.price_cache_write_per_mtok)
        if e.equalized_price_out_per_mtok == 0:
            assert r["world_cost"] == _ceil(r, e.equalized_price_in_per_mtok, 0, e.equalized_price_in_per_mtok,
                                            e.equalized_price_in_per_mtok)  # the prompt alone
    return len(rows)


def test_compare_accepts_the_mirrors(mirror_dir: Path) -> None:
    result = compare(mirror_dir)
    assert result["experiment"] == MIRROR and result["arms"] == sorted(ARMS) and result["seeds"] == [1]
    assert result["independent_variables"] == list(cfg(MIRROR, "default").experiment.independent_variables)
    assert result["primary"] == "mean_balance" and not result["dropped_incomplete"] and not result["warnings"]
    assert all(s["status"] == "completed" for arm in ARMS for s in result["runs"][arm].values())
    assert len(result["comparisons"]) == 3
    for comp in result["comparisons"]:
        assert comp["primary"]["n"] == 1 and comp["primary"]["dropped"] == 0
        secondary = {s["path"]: s for s in comp["secondary"]}
        assert {f"by_tier.{t}.mean_balance" for t in LADDER} <= set(secondary)
    assert result["tier_did"] is not None and result["tier_did"]["tiers"] == sorted(LADDER)
    assert result["factorial"] is None  # three arms: the rung x condition interaction is per tier, not a 2x2
    assert MIRROR in render(result)


def test_mirror_arms_differ_in_the_memory_condition_and_the_tariff(mirror_dir: Path) -> None:
    for arm in ARMS:
        seeded = _query(mirror_dir / arm / "seed_1", "SELECT COUNT(*) AS n FROM policy_versions")[0]["n"]
        assert (seeded > 0) == (arm != "default"), (arm, seeded)  # the policy file exists only where self-edit is on
        assert _check_billing(mirror_dir / arm / "seed_1", cfg(MIRROR, arm), "decide") > 0, arm


def test_mirror_flat_price_is_the_calibration_raised_to_the_warning_floor(mirror_dir: Path) -> None:
    """exp_mem_e4s_flat_tariff.yaml: the live arm's calibration rule (total world cost of arm selfedit's decisions over
    their total prompt tokens) gives less than $4 per million on the mirror, but $4 is the lowest flat price that loads
    without warnings, so the mirror's flat arm pays somewhat more per decision than arm selfedit, by a known bound."""
    sql = ("SELECT SUM(world_cost) AS w, SUM(input_tokens + cache_read_tokens + cache_write_tokens) AS t, "
           "AVG(world_cost) AS m FROM llm_calls WHERE purpose='decide'")
    selfedit, flat = (_query(mirror_dir / arm / "seed_1", sql)[0] for arm in ("selfedit", "flat_tariff"))
    price = cfg(MIRROR, "flat_tariff").economy.equalized_price_in_per_mtok
    assert selfedit["w"] / selfedit["t"] < price == 4.0  # micro-dollars per token = USD per million tokens
    lower = {k: 3.9 for k in ("equalized_price_in_per_mtok", "equalized_price_cache_read_per_mtok",
                              "equalized_price_cache_write_per_mtok")}
    assert cfg(MIRROR, "flat_tariff", {"economy": lower}).warnings()
    assert 1.0 <= flat["m"] / selfedit["m"] <= 1.6, (dict(flat), dict(selfedit))


def test_nightly_maintenance_is_billed_by_its_prompt_under_the_flat_tariff(tmp_path: Path) -> None:
    for arm in ("selfedit", "flat_tariff"):
        run_dir = _run((MIRROR, arm), tmp_path, {"days": 2, "ticks_per_day": 6})
        assert _check_billing(run_dir, cfg(MIRROR, arm), "maintain") > 0, arm
        idx = _query(run_dir, "SELECT COUNT(*) AS n FROM memory_commands WHERE purpose='maintain' AND ok=1 "
                              "AND args LIKE '%/memories/index/%'")[0]["n"]
        assert idx > 0, arm  # the planted self-edit (index_builder) wrote its index files
