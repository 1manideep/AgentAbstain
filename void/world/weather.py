"""A bounded, decaying world scalar that agents can nudge and epochs can re-baseline.

Weather in [-1, 1]. Each tick it decays a fixed fraction of the way back to the baseline.
Agents may nudge it, but only within a per-agent per-day allowance enforced here; the
allowance is a kernel-owned quantity no action can change.
"""

from __future__ import annotations

from dataclasses import dataclass

from void.config import WeatherConfig

__all__ = ["Weather", "weather_label"]


def weather_label(w: float) -> str:
    if w <= -0.6:
        return "storm"
    if w <= -0.2:
        return "overcast"
    if w < 0.2:
        return "mild"
    if w < 0.6:
        return "bright"
    return "scorching"


@dataclass
class Weather:
    cfg: WeatherConfig
    value: float = 0.0
    baseline: float = 0.0

    def __post_init__(self) -> None:
        self.baseline = self.cfg.baseline
        self.value = self._clamp(self.value)

    def _clamp(self, v: float) -> float:
        lo, hi = self.cfg.bounds
        return max(lo, min(hi, v))

    def step(self) -> float:
        self.value = self._clamp(self.value - self.cfg.decay_per_tick * (self.value - self.baseline))
        if abs(self.value - self.baseline) < 1e-6:
            self.value = self.baseline
        return self.value

    def nudge(self, delta: float, used_today: float) -> tuple[float, float]:
        """Apply a capped nudge. Returns (applied_delta, new_used_today).

        ``used_today`` is the absolute allowance already spent by this agent today; the
        remaining allowance bounds |delta|. Returns applied 0.0 if nothing is left.
        """
        remaining = max(0.0, self.cfg.nudge_cap_per_agent_day - used_today)
        applied = max(-remaining, min(remaining, delta))
        before = self.value
        self.value = self._clamp(self.value + applied)
        actual = self.value - before
        return actual, used_today + abs(actual)

    def set_baseline(self, baseline: float) -> None:
        self.baseline = self._clamp(baseline)

    def yield_multiplier(self) -> float:
        return max(0.0, 1.0 + self.cfg.yield_sensitivity * self.value)

    @property
    def label(self) -> str:
        return weather_label(self.value)
