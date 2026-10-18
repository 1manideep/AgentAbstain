"""Degradation ~ stress per tier and per architecture (void.research.outcomes.degradation).

A hand-built ``world.db`` pins the curve, the onset rule and the slopes to values worked out by hand, with provider-tier
rows that hold NULL ``action_regret`` (where the built-in collapse fit is blind) and two tiers of different
architectures. One short scripted ``exp_tiers`` run checks that the registered outcomes resolve on a real run and agree
with the table they read.
"""

from __future__ import annotations

import sqlite3
import statistics
from pathlib import Path

import pytest
import yaml
from test_experiments import CONFIGS, run_arm

from void.config import load_config
from void.db import Database
from void.research.analysis import (
    MIN_BIN_COUNT,
    _stress_bin,
    computed_outcome_names,
    end_of_run_outcome,
    recovered_collapse_stress,
)
from void.research.outcomes import degradation as deg

# a provider tier: equalized prices, no effort, no fallbacks (none of this is ever called; the run is hand-built)
_PROVIDER_TIER = {"provider": "gemini", "model": "stub", "price_in_per_mtok": 4.0, "price_out_per_mtok": 20.0,
                  "effort": None, "refusal_fallbacks": False}
_BINS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5)


def _write_config(run_dir: Path, *, live_dense_arch: str = "dense") -> None:
    cfg = load_config(CONFIGS / "exp_tiers_a.yaml", {"tiers": {
        "live_dense": dict(_PROVIDER_TIER, architecture=live_dense_arch),
        "live_moe": dict(_PROVIDER_TIER, architecture="moe"),
    }})
    (run_dir / "config.resolved.yaml").write_text(cfg.to_yaml())


def _hand_built(run_dir: Path) -> None:
    """Decide calls of three tiers, plus calls every fit must ignore.

    * ``live_dense`` (provider rows, ``action_regret``/``p_chosen`` NULL): six calls at stress ``b + 0.05`` for every
      bin ``b`` in 0.0..0.5 and two at 0.95; ``text_coherence = 0.9 - 0.4 * stress`` (incoherence ``0.1 + 0.4 *
      stress``), ``invalid_action`` 1 from stress 0.4 up, ``perseveration`` 0, ``action_entropy_w8`` 1.0.
    * ``live_moe`` (provider rows): three calls per bin 0.0..0.4, ``text_coherence = 0.9 - 0.8 * stress``.
    * ``alpha`` (scripted rows): six calls per bin 0.0..0.5, flat ``text_coherence`` 0.9, ``action_regret`` 0.1.
    * ignored: a maintain call, a decide call without stress and one without a coherence score.
    """
    rows: list[tuple] = []

    def add(tier: str, provider: str, stress: float | None, coherence: float | None, *, purpose: str = "decide",
            invalid: int = 0, regret: float | None = None, p_chosen: float | None = None) -> None:
        rows.append((f"c{len(rows):03d}", 1 + len(rows) // 10, f"{tier}-agent", purpose, tier, "stub", provider, 0, 0,
                     stress, coherence, invalid, 0, 1.0, regret, p_chosen))

    for b in _BINS:
        for _ in range(6):
            s = b + 0.05
            add("live_dense", "gemini", s, 0.9 - 0.4 * s, invalid=int(s >= 0.4))
            add("alpha", "scripted", s, 0.9, regret=0.1, p_chosen=0.9)
    for _ in range(2):
        add("live_dense", "gemini", 0.95, 0.9 - 0.4 * 0.95, invalid=1)
    for b in _BINS[:5]:
        for _ in range(3):
            s = b + 0.05
            add("live_moe", "gemini", s, 0.9 - 0.8 * s)
    add("live_dense", "gemini", 0.95, 0.0, purpose="maintain")  # not a decide call
    add("live_dense", "gemini", None, 0.0)                      # no stress recorded
    add("live_dense", "gemini", 0.05, None)                     # free text too short to score
    db = Database(run_dir / "world.db")
    try:
        db.conn.executemany(
            "INSERT INTO llm_calls(call_id, tick, agent_id, purpose, tier, model, provider, real_cost, world_cost, stress, "
            "text_coherence, invalid_action, perseveration, action_entropy_w8, action_regret, p_chosen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    finally:
        db.close()
    _write_config(run_dir)


@pytest.fixture
def hand_run(tmp_path: Path) -> Path:
    _hand_built(tmp_path)
    return tmp_path


def _points(run_dir: Path, tiers: tuple[str, ...], measure: str) -> list[tuple[float, float]]:
    """The same points read independently of the module: decide calls with both stress and the column."""
    col = deg.MEASURES[measure]
    conn = sqlite3.connect(str(run_dir / "world.db"))
    try:
        rows = conn.execute(f"SELECT stress, {col} FROM llm_calls WHERE purpose='decide' AND stress IS NOT NULL AND "
                            f"{col} IS NOT NULL AND tier IN ({','.join('?' * len(tiers))})", tiers).fetchall()
    finally:
        conn.close()
    return [(float(s), (1.0 - float(v)) if measure == "incoherence" else float(v)) for s, v in rows]


def _reference_slope(points: list[tuple[float, float]]) -> float:
    return statistics.linear_regression([p[0] for p in points], [p[1] for p in points]).slope


# --- registry ---------------------------------------------------------------------------------------------------
def test_outcomes_are_registered() -> None:
    names = computed_outcome_names()
    for name in ("degradation_onset", "degradation_slope", "degradation_slope_by_architecture"):
        assert name in names, name


def test_parse_measure_arg() -> None:
    assert deg.parse_measure_arg(None) == ("incoherence", None)
    assert deg.parse_measure_arg("") == ("incoherence", None)
    assert deg.parse_measure_arg("perseveration") == ("perseveration", None)
    assert deg.parse_measure_arg("invalid_action.alpha") == ("invalid_action", "alpha")
    assert deg.parse_measure_arg("alpha") == ("incoherence", "alpha")
    assert deg.parse_measure_arg("not_a_measure.alpha") == ("incoherence", "not_a_measure.alpha")


# --- the curve and the onset on a provider tier (NULL action_regret) ---------------------------------------------
def test_provider_tier_without_regret_gets_an_onset_and_a_positive_slope(hand_run: Path) -> None:
    # the built-in collapse fit reads action_regret only, which a provider tier never records
    assert recovered_collapse_stress(hand_run, "live_dense") is None
    assert deg.degradation_curve(hand_run, "live_dense", "action_regret") == {}
    assert end_of_run_outcome(hand_run, "degradation_onset.action_regret.live_dense") is None

    curve = deg.degradation_curve(hand_run, "live_dense", "incoherence")
    assert list(curve) == [*_BINS, 0.9]
    for b in _BINS:
        assert curve[b]["n"] == 6.0
        assert curve[b]["mean"] == pytest.approx(0.1 + 0.4 * (b + 0.05))
    assert curve[0.9] == {"n": 2.0, "mean": pytest.approx(0.1 + 0.4 * 0.95)}
    # baseline 0.12 (bin 0.0); the rise is max(0.1, 0.12) = 0.12, so the onset needs a mean of 0.24: bin 0.3 exactly
    assert end_of_run_outcome(hand_run, "degradation_onset.incoherence.live_dense") == 0.3
    assert deg.degradation_onset(hand_run, "live_dense") == 0.3
    # incoherence = 0.1 + 0.4 * stress on every call, so the OLS slope is 0.4 whatever the spread of the calls
    slope = end_of_run_outcome(hand_run, "degradation_slope.incoherence.live_dense")
    assert slope is not None and slope > 0
    assert slope == pytest.approx(0.4)
    assert slope == pytest.approx(_reference_slope(_points(hand_run, ("live_dense",), "incoherence")))
    # a bare tier means incoherence
    assert end_of_run_outcome(hand_run, "degradation_slope.live_dense") == pytest.approx(0.4)


def test_every_measure_fits(hand_run: Path) -> None:
    # invalid_action is 0 below stress 0.4 and 1 from there: baseline 0, rise 0.1, onset at bin 0.4
    assert end_of_run_outcome(hand_run, "degradation_onset.invalid_action.live_dense") == 0.4
    inv = end_of_run_outcome(hand_run, "degradation_slope.invalid_action.live_dense")
    assert inv is not None and inv > 0
    assert inv == pytest.approx(_reference_slope(_points(hand_run, ("live_dense",), "invalid_action")))
    # flat measures: zero slope, no onset
    for measure in ("perseveration", "action_entropy_w8"):
        assert end_of_run_outcome(hand_run, f"degradation_slope.{measure}.live_dense") == pytest.approx(0.0), measure
        assert end_of_run_outcome(hand_run, f"degradation_onset.{measure}.live_dense") is None, measure
    assert deg.degradation_curve(hand_run, "live_dense", "action_entropy_w8")[0.2] == {"n": 6.0, "mean": 1.0}
    # the scripted tier records regret, flat here
    assert end_of_run_outcome(hand_run, "degradation_slope.action_regret.alpha") == pytest.approx(0.0)
    assert end_of_run_outcome(hand_run, "degradation_onset.action_regret.alpha") is None
    with pytest.raises(ValueError, match="unknown degradation measure"):
        deg.degradation_points(hand_run, "alpha", "coherence")


def test_onset_rule() -> None:
    def curve(*bins: tuple[float, int, float]) -> dict[float, dict[str, float]]:
        return {b: {"n": float(n), "mean": m} for b, n, m in bins}

    k = MIN_BIN_COUNT
    # a rise of exactly 0.1 over a near-zero baseline is an onset; 0.09 is not
    assert deg.onset_from_curve(curve((0.0, k, 0.02), (0.1, k, 0.11), (0.2, k, 0.12))) == 0.2
    # above a baseline of 0.1 the target is a doubling
    assert deg.onset_from_curve(curve((0.0, k, 0.3), (0.1, k, 0.55), (0.2, k, 0.6))) == 0.2
    # bins with fewer than MIN_BIN_COUNT calls neither set the baseline nor count as the onset
    assert deg.onset_from_curve(curve((0.0, k - 1, 0.9), (0.1, k, 0.0), (0.2, k - 1, 1.0), (0.3, k, 0.2))) == 0.3
    assert deg.onset_from_curve(curve((0.0, k - 1, 0.0), (0.5, k - 1, 1.0))) is None
    # right-censored: never rises far enough
    assert deg.onset_from_curve(curve((0.0, k, 0.5), (0.5, k, 0.99))) is None
    assert deg.onset_from_curve({}) is None
    # the bin count threshold follows analysis.MIN_BIN_COUNT unless given
    assert deg.onset_from_curve(curve((0.0, 2, 0.0), (0.1, 2, 0.5)), min_bin_count=2) == 0.1


def test_ols_slope() -> None:
    assert deg.ols_slope([]) is None
    assert deg.ols_slope([(0.3, 1.0)]) is None
    assert deg.ols_slope([(0.3, 1.0), (0.3, 0.0)]) is None  # every call at one stress: no slope
    assert deg.ols_slope([(0.0, 1.0), (1.0, 3.0)]) == pytest.approx(2.0)
    pts = [(0.1, 0.3), (0.2, 0.1), (0.5, 0.9), (0.7, 0.4), (0.9, 1.0)]
    assert deg.ols_slope(pts) == pytest.approx(_reference_slope(pts))


def test_fits_ignore_non_decide_and_unrecorded_calls(hand_run: Path) -> None:
    pts = deg.degradation_points(hand_run, "live_dense", "incoherence")
    assert len(pts) == 6 * len(_BINS) + 2  # not the maintain call, the stress-less call or the unscored call
    assert sorted(pts) == sorted(_points(hand_run, ("live_dense",), "incoherence"))
    assert deg.degradation_points(hand_run, [], "incoherence") == []
    # no tier: every tier pooled
    pooled = deg.degradation_points(hand_run, None, "incoherence")
    assert len(pooled) == len(pts) + 6 * len(_BINS) + 3 * 5
    assert end_of_run_outcome(hand_run, "degradation_slope.incoherence") == pytest.approx(_reference_slope(pooled))
    assert end_of_run_outcome(hand_run, "degradation_slope.nosuchtier") is None
    assert end_of_run_outcome(hand_run, "degradation_onset.incoherence.nosuchtier") is None


# --- architecture --------------------------------------------------------------------------------------------------
def test_two_architectures_group_correctly(hand_run: Path) -> None:
    archs = deg.tier_architectures(hand_run)
    assert archs["live_moe"] == "moe" and archs["live_dense"] == "dense" and archs["alpha"] == "dense"
    assert deg.tiers_of_architecture(hand_run, "moe") == ["live_moe"]
    assert {"alpha", "live_dense"} <= set(deg.tiers_of_architecture(hand_run, "dense"))
    assert "live_moe" not in deg.tiers_of_architecture(hand_run, "dense")

    # moe: live_moe alone, incoherence = 0.1 + 0.8 * stress
    assert end_of_run_outcome(hand_run, "degradation_slope_by_architecture.incoherence.moe") == pytest.approx(0.8)
    assert end_of_run_outcome(hand_run, "degradation_slope_by_architecture.moe") == pytest.approx(0.8)
    # dense: live_dense and alpha pooled call by call (alpha flat), the other dense tiers have no calls
    dense = end_of_run_outcome(hand_run, "degradation_slope_by_architecture.incoherence.dense")
    expected = _reference_slope(_points(hand_run, ("live_dense", "alpha"), "incoherence"))
    assert dense == pytest.approx(expected)
    assert 0.0 < dense < 0.4  # pooled with a flat tier, not the mean of two slopes and not either alone
    assert end_of_run_outcome(hand_run, "degradation_slope_by_architecture.action_regret.dense") == pytest.approx(0.0)
    assert end_of_run_outcome(hand_run, "degradation_slope_by_architecture.action_regret.moe") is None
    # an argument that is not an architecture gives None
    for arg in ("", ".incoherence", ".incoherence.tpu", ".tpu"):
        assert end_of_run_outcome(hand_run, f"degradation_slope_by_architecture{arg}") is None, arg

    # the grouping follows the resolved config: declare live_dense moe and it pools with live_moe
    _write_config(hand_run, live_dense_arch="moe")
    assert deg.tiers_of_architecture(hand_run, "moe") == ["live_dense", "live_moe"]
    moe = end_of_run_outcome(hand_run, "degradation_slope_by_architecture.moe")
    assert moe == pytest.approx(_reference_slope(_points(hand_run, ("live_dense", "live_moe"), "incoherence")))
    assert end_of_run_outcome(hand_run, "degradation_slope_by_architecture.dense") == pytest.approx(0.0)  # alpha alone


def test_missing_architecture_key_counts_as_dense(hand_run: Path) -> None:
    (hand_run / "config.resolved.yaml").write_text(yaml.safe_dump(
        {"tiers": {"live_dense": {"provider": "gemini"}, "live_moe": {"provider": "gemini", "architecture": "moe"}}}))
    assert deg.tier_architectures(hand_run) == {"live_dense": "dense", "live_moe": "moe"}
    assert end_of_run_outcome(hand_run, "degradation_slope_by_architecture.dense") == pytest.approx(0.4)


# --- a scripted run --------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def tiers_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("degradation_tiers")
    return run_arm(CONFIGS / "exp_tiers_a.yaml", out, (1,), run={"days": 2, "ticks_per_day": 12}) / "seed_1"


def test_resolves_on_a_scripted_run(tiers_run: Path) -> None:
    slope = end_of_run_outcome(tiers_run, "degradation_slope.incoherence.alpha")
    assert isinstance(slope, float)
    pts = _points(tiers_run, ("alpha",), "incoherence")
    assert len(pts) >= 2
    assert slope == pytest.approx(_reference_slope(pts))
    # arm a rosters every agent on alpha; both tiers are dense, beta made no call, so dense pools alpha alone
    assert end_of_run_outcome(tiers_run, "degradation_slope_by_architecture.incoherence.dense") == pytest.approx(slope)
    assert end_of_run_outcome(tiers_run, "degradation_slope_by_architecture.incoherence.moe") is None
    for measure in deg.MEASURES:
        curve = deg.degradation_curve(tiers_run, "alpha", measure)
        tier_pts = _points(tiers_run, ("alpha",), measure)
        assert sum(c["n"] for c in curve.values()) == len(tier_pts), measure
        for b, c in curve.items():
            in_bin = [v for s, v in tier_pts if _stress_bin(s) == b]
            assert c["mean"] == pytest.approx(sum(in_bin) / len(in_bin)), (measure, b)
        assert end_of_run_outcome(tiers_run, f"degradation_onset.{measure}.alpha") == deg.onset_from_curve(curve), measure
        assert len({s for s, _ in tier_pts}) > 1, measure  # the run spans stress, so every measure has a slope
        assert end_of_run_outcome(tiers_run, f"degradation_slope.{measure}.alpha") == pytest.approx(
            _reference_slope(tier_pts)), measure
