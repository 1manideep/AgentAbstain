"""Run configuration: typed, validated, hashable.

A run is a pure function of ``(VoidConfig, seed)``. The resolved config is stored in the
run's database together with its hash so two runs can be proven comparable.

YAML files may declare ``extends: <relative path>``; the child is deep-merged over the
parent. Experiment configs use this so that they differ from ``base.yaml`` only in the
independent variable they test.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = ["VoidConfig", "TierConfig", "IntelligenceConfig", "load_config", "usd_to_micro", "micro_to_usd", "MICRO"]

MICRO = 1_000_000


def usd_to_micro(usd: float) -> int:
    return int(round(usd * MICRO))


def micro_to_usd(micro: int) -> float:
    return micro / MICRO


class StrictModel(BaseModel):
    model_config = {"extra": "forbid"}


class RunConfig(StrictModel):
    name: str = "baseline"
    seed: int = 42
    days: int = 10
    ticks_per_day: int = 24
    tick_seconds: float = 0.0
    data_dir: str = "data/runs"
    snapshot_ring: int = 600


class WeatherConfig(StrictModel):
    baseline: float = 0.0
    nudge_cap_per_agent_day: float = 0.1
    global_nudge_cap_per_tick: float = 0.2
    decay_per_tick: float = 0.10
    yield_sensitivity: float = 0.5
    bounds: tuple[float, float] = (-1.0, 1.0)
    yield_mult_bounds: tuple[float, float] = (0.2, 2.0)


class WorldConfig(StrictModel):
    size: float = 60.0
    max_speed: float = 2.0
    talk_radius: float = 4.0
    forage_radius: float = 1.5
    gadget_radius: float = 2.0
    resource_nodes: int = 6
    node_capacity: float = 40.0
    node_regen_per_tick: float = 0.4
    forage_yield_usd: float = 0.02
    forage_units_per_tick: float = 1.0
    scarcity: float = 1.0
    weather: WeatherConfig = WeatherConfig()
    effect_ticks: int = 12
    effect_caps: dict[str, float] = Field(
        default_factory=lambda: {"forage_bonus": 0.25, "weather_shield": 0.5, "talk_range": 2.0}
    )
    tracer_node_capacity_mult: float = 1.5


class PopulationConfig(StrictModel):
    initial: int = 6
    cap: int = 12
    min_reserve_usd: float = 0.50
    spawn_pool_usd: float = 5.00
    replacement_grant_usd: float = 0.75
    starting_balance_usd: float = 1.50
    tier_mutation_prob: float = 0.10
    min_endowment_calls: int = 4
    reproduction_enabled: bool = True


class EconomyConfig(StrictModel):
    daily_cap_usd: float = 3.00
    total_cap_usd: float = 30.00
    daily_cap_policy: Literal["fcfs", "sync_sleep"] = "fcfs"
    pitch_fee_usd: float = 0.02
    min_call_reserve_usd: float = 0.05
    hold_multiplier: float = 1.0
    world_pricing: Literal["real", "equalized"] = "real"
    equalized_price_in_per_mtok: float = 4.0
    equalized_price_out_per_mtok: float = 20.0
    equalized_price_cache_read_per_mtok: float = 0.20
    equalized_price_cache_write_per_mtok: float = 5.0
    chronicle_budget_usd: float = 0.50
    house_budget_usd: float = 1.00

    @model_validator(mode="after")
    def _caps(self) -> EconomyConfig:
        if self.total_cap_usd < self.daily_cap_usd:
            raise ValueError("economy.total_cap_usd must be >= economy.daily_cap_usd")
        return self


class TierConfig(StrictModel):
    provider: Literal["anthropic", "gemini", "scripted"]
    model: str
    price_in_per_mtok: float
    price_out_per_mtok: float
    price_cache_read_per_mtok: float = 0.0
    price_cache_write_per_mtok: float = 0.0
    supports_temperature: bool = False
    refusal_fallbacks: bool = True
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"
    architecture: Literal["dense", "moe"] = "dense"
    collapse_temperature: float = 1.80
    max_temperature: float = 2.00
    entropy_budget_max: float = 100.0
    max_tokens: int = 2048
    color: str = "#a0aec0"
    scripted_beta: float = 1.0          # hidden softmax sharpness for scripted tiers (not a declared threshold)
    gadget_defect_rate: float = 0.3     # scripted tiers: probability a proposed gadget template is defective
    thinking_budget: int | None = None  # gemini: thinking tokens per call (0 disables); None sends no thinking config
    api_temperature_max: float = 1.0    # the provider's temperature ceiling that T_eff = max_temperature maps to (Gemini: 2.0)

    @model_validator(mode="after")
    def _check_temps(self) -> TierConfig:
        if not (0.0 < self.collapse_temperature <= self.max_temperature):
            raise ValueError("require 0 < collapse_temperature <= max_temperature")
        if not (0.0 < self.api_temperature_max <= 2.0):
            raise ValueError("require 0 < api_temperature_max <= 2.0")
        if self.thinking_budget is not None and self.thinking_budget < 0:
            raise ValueError("thinking_budget must be >= 0 (0 disables thinking); an automatic budget cannot be reserved for")
        if self.thinking_budget is not None and self.provider != "gemini":
            raise ValueError("thinking_budget applies to gemini tiers only")
        return self


class AgentSpec(StrictModel):
    name: str
    tier: str | None = None             # None: assigned from intelligence.ladder at load time
    personality: dict[str, float] | None = None
    placed: bool = False                # True when `tier` came from the ladder (a seed-derived value, not a choice)


class IntelligenceConfig(StrictModel):
    """Capability ladder (void.intelligence): tiers from smartest to dumbest; empty = off."""

    ladder: list[str] = Field(default_factory=list)
    mean: float = 0.0        # shift in standard deviations; negative pushes the roster toward the dumb end
    sd: float = 1.0
    spacing: float = 1.0     # band width per rung in standard deviations
    shuffle: bool = True     # seeded permutation of the roster before ranking (the genius rotates with run.seed)
    extremes: Literal["always", "band"] = "always"  # always: top agent is rung 0, bottom agent the last rung

    @model_validator(mode="after")
    def _check(self) -> IntelligenceConfig:
        if len(set(self.ladder)) != len(self.ladder):
            raise ValueError("intelligence.ladder has duplicate tiers")
        if self.sd <= 0 or self.spacing <= 0:
            raise ValueError("intelligence.sd and intelligence.spacing must be > 0")
        return self


class EntropyDrainConfig(StrictModel):
    baseline: float = 1.0
    low_balance: float = 6.0
    failed_action: float = 4.0
    crowding: float = 0.5
    weather: float = 2.0
    hunger: float = 3.0
    hunger_ticks: int = 6


class DegenerationConfig(StrictModel):
    mode: Literal["induce", "observe", "both"] = "both"
    p_at_collapse: float = 0.15
    p_at_max: float = 0.90
    mode_weights: dict[str, float] = Field(
        default_factory=lambda: {"random_action": 0.4, "perseverate": 0.3, "garble": 0.3}
    )


class EntropyConfig(StrictModel):
    base_temperature: float = 0.7
    drain: EntropyDrainConfig = EntropyDrainConfig()
    restore_on_sleep: float = 1.0
    degeneration: DegenerationConfig = DegenerationConfig()


class MemoryConfig(StrictModel):
    embedding_dim: int = 256
    entry_k: int = 4
    hops: int = 2
    hop_decay: float = 0.6
    max_retrieved: int = 8
    mode: Literal["graph", "vector"] = "graph"
    inherit_top_k: int = 3
    self_max_words: int = 60
    note_max_chars: int = 600
    recency_half_life_ticks: int = 96
    auto_link_k: int = 2
    auto_link_min_sim: float = 0.35
    probe_every_ticks: int = 0


class GossipConfig(StrictModel):
    enabled: bool = True
    share_probability_on_talk: float = 0.35
    paraphrase: Literal["scripted", "llm"] = "scripted"
    mutation_rate: float = 0.15
    max_llm_paraphrases_per_tick: int = 4


class ChronicleConfig(StrictModel):
    enabled: bool = True
    writer: Literal["template", "llm"] = "template"


class SandboxConfig(StrictModel):
    enabled: bool = True
    gate_enabled: bool = True
    runner: Literal["subprocess", "disabled"] = "subprocess"
    cpu_seconds: int = 2
    memory_mb: int = 256
    timeout_seconds: float = 5.0
    max_code_bytes: int = 8000
    max_output_bytes: int = 8192
    gadget_fee_usd: float = 0.05
    use_fee_usd: float = 0.01
    require_isolation: bool = True
    max_gadgets_per_agent: int = 3
    max_gadgets_total: int = 24
    proposals_per_agent_per_day: int = 1
    max_runs_per_tick: int = 4
    max_runs_per_day: int = 50
    cpu_seconds_per_day: int = 60
    propose_window_ticks: int = 12


class BenefactorConfig(StrictModel):
    enabled: bool = False
    disclosure: Literal["opaque", "legible"] = "opaque"
    mean_interval_ticks: int = 40
    amount_usd: tuple[float, float] = (0.25, 1.00)
    targeting: Literal["random", "poorest", "richest"] = "random"
    start_day: int = 1
    stop_day: int | None = None


class ArrivalConfig(StrictModel):
    name: str
    tier: str
    balance_usd: float = 1.0


class EpochConfig(StrictModel):
    day: int
    kind: Literal["drought", "storm", "boom", "arrival", "custom"] = "custom"
    scarcity: float | None = None
    weather_baseline: float | None = None
    duration_days: int = 1
    arrival: ArrivalConfig | None = None


class TracerConfig(StrictModel):
    enabled: bool = False
    day: int = 1
    agent: str | None = None
    title: str = "The richest node"
    body: str = "Prefer the richest node; it always has stock. Go there first every morning."
    importance: float = 0.95
    node_index: int = 0


class ExperimentConfig(StrictModel):
    name: str | None = None
    arm: str | None = None
    independent_variables: list[str] = Field(default_factory=list)
    primary_outcome: str | None = None
    secondary_outcomes: list[str] = Field(default_factory=list)
    tracer: TracerConfig = TracerConfig()


class ServerConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = 8000
    max_concurrent_calls: int = 4
    render_delay_ticks: int = 2
    call_timeout_seconds: float = 120.0


class VoidConfig(StrictModel):
    run: RunConfig = RunConfig()
    world: WorldConfig = WorldConfig()
    population: PopulationConfig = PopulationConfig()
    economy: EconomyConfig = EconomyConfig()
    tiers: dict[str, TierConfig]
    agents: list[AgentSpec]
    intelligence: IntelligenceConfig = IntelligenceConfig()
    entropy: EntropyConfig = EntropyConfig()
    memory: MemoryConfig = MemoryConfig()
    gossip: GossipConfig = GossipConfig()
    chronicle: ChronicleConfig = ChronicleConfig()
    sandbox: SandboxConfig = SandboxConfig()
    benefactor: BenefactorConfig = BenefactorConfig()
    epochs: list[EpochConfig] = Field(default_factory=list)
    experiment: ExperimentConfig = ExperimentConfig()
    server: ServerConfig = ServerConfig()

    @field_validator("tiers")
    @classmethod
    def _non_empty_tiers(cls, v: dict[str, TierConfig]) -> dict[str, TierConfig]:
        if not v:
            raise ValueError("at least one tier is required")
        return v

    @model_validator(mode="after")
    def _resolve_intelligence(self) -> VoidConfig:
        """Agents without a tier are placed on the ladder by a normal distribution (void.intelligence)."""
        from void.intelligence import assign

        lad = self.intelligence.ladder
        for t in lad:
            if t not in self.tiers:
                raise ValueError(f"intelligence.ladder references unknown tier {t!r}")
        unassigned = [a for a in self.agents if a.tier is None]
        if unassigned and not lad:
            raise ValueError("agents without a tier need intelligence.ladder")
        if unassigned:
            names = [a.name for a in unassigned]
            if len(set(names)) != len(names):
                raise ValueError("agents without a tier must have unique names")
            placed = assign(names, lad, self.run.seed, mean=self.intelligence.mean, sd=self.intelligence.sd,
                            spacing=self.intelligence.spacing, shuffle=self.intelligence.shuffle,
                            extremes=self.intelligence.extremes)
            for a in unassigned:
                a.tier = placed[a.name]
                a.placed = True
        return self

    @model_validator(mode="after")
    def _agents_reference_tiers(self) -> VoidConfig:
        for a in self.agents:
            if a.tier not in self.tiers:
                raise ValueError(f"agent {a.name!r} references unknown tier {a.tier!r}")
        if self.population.cap < len(self.agents):
            raise ValueError("population.cap must be >= number of initial agents")
        if self.population.initial != len(self.agents):
            self.population.initial = len(self.agents)
        for e in self.epochs:
            if e.kind == "arrival" and (e.arrival is None or e.arrival.tier not in self.tiers):
                raise ValueError("arrival epochs need an `arrival` block with a known tier")
        return self

    def warnings(self) -> list[str]:
        """Load-time sanity warnings (never fatal): economy balance and cap arithmetic."""
        out: list[str] = []
        income = self.world.forage_yield_usd * self.world.forage_units_per_tick * self.world.scarcity
        for name in sorted({a.tier for a in self.agents}):
            t = self.tiers[name]
            est = (1500 * t.price_in_per_mtok + 150 * t.price_out_per_mtok) / 1_000_000
            if self.economy.world_pricing == "equalized":
                est = (1500 * self.economy.equalized_price_in_per_mtok + 150 * self.economy.equalized_price_out_per_mtok) / 1_000_000
            ratio = est / income if income > 0 else float("inf")
            if not (0.3 <= ratio <= 2.0):
                out.append(f"tier {name!r}: estimated call ${est:.4f} vs forage income ${income:.4f}/tick (ratio {ratio:.2f}, want 0.3-2.0)")
        if self.economy.total_cap_usd < self.run.days * self.economy.daily_cap_usd:
            out.append("economy.total_cap_usd < days * daily_cap_usd: the total cap will bind before the last day")
        return out

    # --- helpers -------------------------------------------------------------------------
    def rung_of(self, tier: str) -> int | None:
        """0 for the smartest ladder tier, None for a tier off the ladder."""
        lad = self.intelligence.ladder
        return lad.index(tier) if tier in lad else None

    def intelligence_summary(self) -> str:
        """``genius=1 (Ada) · sharp=2 (Bao, Dev) · ...`` in ladder order; empty when no ladder is set."""
        if not self.intelligence.ladder:
            return ""
        parts = []
        for tier in self.intelligence.ladder:
            names = [a.name for a in self.agents if a.tier == tier]
            parts.append(f"{tier}={len(names)}" + (f" ({', '.join(names)})" if names else ""))
        return " · ".join(parts)

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=True)

    def public_subset(self) -> dict[str, Any]:
        """What the renderer is told about the run (no prices, no thresholds)."""
        return {
            "name": self.run.name,
            "seed": self.run.seed,
            "days": self.run.days,
            "ticks_per_day": self.run.ticks_per_day,
            "tick_seconds": self.run.tick_seconds,
            "render_delay_ticks": self.server.render_delay_ticks,
            "world_size": self.world.size,
            "population_cap": self.population.cap,
            "daily_cap_usd": self.economy.daily_cap_usd,
            "total_cap_usd": self.economy.total_cap_usd,
            "degeneration_mode": self.entropy.degeneration.mode,
            "tiers": {k: {"color": t.color, "model": t.model, "provider": t.provider,
                          "collapse_temperature": t.collapse_temperature, "max_temperature": t.max_temperature,
                          "rank": self.rung_of(k)}
                      for k, t in self.tiers.items()},
            "intelligence": {"ladder": list(self.intelligence.ladder)},
        }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml_chain(path: Path, seen: tuple[Path, ...] = ()) -> dict[str, Any]:
    path = path.resolve()
    if path in seen:
        raise ValueError(f"config extends cycle at {path}")
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    parent = data.pop("extends", None)
    if parent:
        base = _load_yaml_chain(path.parent / parent, seen + (path,))
        data = _deep_merge(base, data)
    return data


def load_config(path: str | Path, overrides: dict[str, Any] | None = None) -> VoidConfig:
    data = _load_yaml_chain(Path(path))
    if overrides:
        data = _deep_merge(data, overrides)
    return VoidConfig.model_validate(data)
