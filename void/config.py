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

from void.economy.catalog import (
    MODEL_PRICES,
    fallback_targets,
    rejects_effort,
    rejects_sampling,
    thinks_by_default,
)

__all__ = ["VoidConfig", "VeinsConfig", "TierConfig", "IntelligenceConfig", "MemoryConfig", "MemoryPolicyConfig", "MaintenanceConfig",
           "ExamConfig", "PracticesConfig", "CanaryConfig", "SelectionArmConfig", "ExperimentConfig", "HypothesisConfig",
           "MaintenanceStrategy", "MaintenanceRight", "load_config", "usd_to_micro", "micro_to_usd", "MICRO"]

MICRO = 1_000_000

# Load-time checks for tiers whose model thinks by default (see VoidConfig.warnings): assumed thinking tokens per
# call by effort level, and the smallest output caps that leave room for thinking plus the answer.
THINKING_ALLOWANCE = {"low": 300, "medium": 800, "high": 1500, "xhigh": 3000, "max": 3000}
MIN_THINKING_DECISION_TOKENS = 1024
MIN_THINKING_MAINTENANCE_TOKENS = 4096
MIN_THINKING_EXAM_TOKENS = 1024


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
    # Store the full rendered prompt and the observation of every call in llm_calls (MEMORY_EVOLUTION §10, WP0).
    # The exam generators and the Parquet export read these; a run without them cannot be audited.
    log_prompts: bool = True


class WeatherConfig(StrictModel):
    baseline: float = 0.0
    nudge_cap_per_agent_day: float = 0.1
    global_nudge_cap_per_tick: float = 0.2
    decay_per_tick: float = 0.10
    yield_sensitivity: float = 0.5
    bounds: tuple[float, float] = (-1.0, 1.0)
    yield_mult_bounds: tuple[float, float] = (0.2, 2.0)


class VeinsConfig(StrictModel):
    """Veins (MEMORY_EVOLUTION §5.2): a node that, for a while, pays several times more per forage.

    Veins open more often at a seeded subset of "vein-prone" nodes, so where veins have appeared is worth
    remembering. A vein shows in an observation only within ``visible_radius`` of its node (tighter than the fog's
    view radius): an agent sees the node from afar but learns of the vein only by coming close. Off by default;
    with ``enabled: false`` the world is exactly what it was before veins existed.
    """

    enabled: bool = False
    prone_nodes: int = 2            # nodes drawn once per run (stream ``vein_prone``) where veins recur
    p_start_prone: float = 0.10     # per tick, the chance a vein opens at a prone node that has none
    p_start_other: float = 0.01     # the same for every other node
    duration_ticks: int = 12        # how long a vein lasts
    yield_mult: float = 4.0         # forage value multiplier while it lasts
    visible_radius: float = 4.0     # an observation shows the vein only within this distance of its node
    refill: bool = True             # a vein opening tops its node up to capacity (and ends any fallow)

    @model_validator(mode="after")
    def _check(self) -> VeinsConfig:
        if self.prone_nodes < 0:
            raise ValueError("world.veins.prone_nodes must be >= 0")
        for name in ("p_start_prone", "p_start_other"):
            if not (0.0 <= getattr(self, name) <= 1.0):
                raise ValueError(f"world.veins.{name} must be within [0, 1]")
        if self.duration_ticks < 1:
            raise ValueError("world.veins.duration_ticks must be >= 1")
        if self.yield_mult < 1.0:
            raise ValueError("world.veins.yield_mult must be >= 1")
        if self.visible_radius <= 0:
            raise ValueError("world.veins.visible_radius must be > 0")
        return self


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
    # Fog (MEMORY_EVOLUTION §5.2): nodes and agents beyond this distance are not listed in the observation;
    # None lists everything (today's behaviour). Node entity stubs are created on first sight, not at birth.
    view_radius: float | None = None
    # Claim feedback: after a checkable false claim the listener learns it was false with this probability.
    claim_feedback_p: float = 0.0
    # Real depletion (MEMORY_EVOLUTION §10.4): a node foraged down to nothing lies fallow for this many ticks (no
    # regrowth, nothing to forage) before it starts to regrow. 0 = it regrows at once, as it always did.
    fallow_ticks: int = 0
    veins: VeinsConfig = VeinsConfig()

    @model_validator(mode="after")
    def _check_fog(self) -> WorldConfig:
        if self.view_radius is not None and self.view_radius <= 0:
            raise ValueError("world.view_radius must be > 0 (or null for no fog)")
        if not (0.0 <= self.claim_feedback_p <= 1.0):
            raise ValueError("world.claim_feedback_p must be within [0, 1]")
        if self.view_radius is not None and self.view_radius < self.talk_radius:
            raise ValueError("world.view_radius must be >= world.talk_radius (an agent must see whom it can talk to)")
        if self.fallow_ticks < 0:
            raise ValueError("world.fallow_ticks must be >= 0")
        if self.veins.enabled:
            if self.view_radius is not None and self.veins.visible_radius >= self.view_radius:
                raise ValueError("world.veins.visible_radius must be < world.view_radius (a vein is seen only up close)")
            if self.veins.prone_nodes > self.resource_nodes:
                raise ValueError("world.veins.prone_nodes must be <= world.resource_nodes")
        return self


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
    # Whether a provider brain is told its character (the six personality traits, motto, lineage) in a
    # "Who you are" section of the user turn. Hidden by default: the deception experiments want behaviour to
    # emerge from the world, not from being told "honesty low". The scripted brain always acts on its traits.
    persona: Literal["hidden", "shown"] = "hidden"
    # Which argument rules a scripted brain uses. "design_7_2": the DESIGN §7.2 table (offspring once the balance
    # reaches 2 x min_reserve + endowment under per-parent names, greed-scaled transfers, nudges toward yield-positive
    # weather, random gadget parameters, sleep when calls burn more than a forage earns). "pre_7_2": the rules the
    # scripted brain had before (offspring above 1.6 x the starting balance, a fixed transfer, nudge and sleep rule,
    # gadget x = 1.0), kept so worlds recorded under them (G0's E0 arms and dependence check) reproduce exactly.
    scripted_rules: Literal["design_7_2", "pre_7_2"] = "design_7_2"


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
    # Kernel wallet that pays for measurement (exam calls) and, later, selection rewards (MEMORY_EVOLUTION §5.8).
    # Seeded once so the money-conservation identity gains one term and still balances.
    research_pool_usd: float = 0.0

    @model_validator(mode="after")
    def _caps(self) -> EconomyConfig:
        if self.total_cap_usd < self.daily_cap_usd:
            raise ValueError("economy.total_cap_usd must be >= economy.daily_cap_usd")
        # a hold below the worst case would let a whole batch of calls through the gate and past the caps
        if self.hold_multiplier < 1.0:
            raise ValueError("economy.hold_multiplier must be >= 1 (a hold must cover the worst case of its call)")
        if self.min_call_reserve_usd < 0:
            raise ValueError("economy.min_call_reserve_usd must be >= 0")
        return self


# Planted strategies with known effects (MEMORY_EVOLUTION §6.2): the measurement pipeline must recover their order.
# ``distill``, ``revert`` and ``retire`` are the published-mechanism baselines of §6.13 row 5 (distillation; revert
# guards; retirement plus cap); void.brain.maintenance_strategies refuses a name it does not implement yet.
MaintenanceStrategy = Literal["noop", "index_builder", "hoarder", "decoy", "summarize", "distill", "revert", "retire"]

# What a maintenance call may do (MEMORY_EVOLUTION §5.3 control ladder, expressed as rights):
#   notes      create new notes under /memories/notes/ (the equal-compute "summarize your day" control, E1 arm b)
#   policy     edit /memories/memory_policy.md (C1 adds this)
#   structure  str_replace / insert / delete / rename on notes and /memories/index/ (C2 adds this)
MaintenanceRight = Literal["notes", "policy", "structure"]


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
    # Scripted tiers only: the planted memory-maintenance strategy for the E0 controls (MEMORY_EVOLUTION §6.2).
    # None means the tier's maintenance call is a no-op. Provider tiers always ask the model.
    maintenance_strategy: MaintenanceStrategy | None = None

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
        if self.maintenance_strategy is not None and self.provider != "scripted":
            raise ValueError("maintenance_strategy applies to scripted tiers only (provider tiers ask the model)")
        if self.provider == "anthropic":
            self._check_anthropic_request_rules()
        return self

    def _check_anthropic_request_rules(self) -> None:
        """Settings the API answers with a 400 (or silently mis-serves) for this tier's model; each would turn
        every call of the tier into an error and its agents into idlers for the whole run."""
        m = self.model
        if self.supports_temperature and rejects_sampling(m):
            raise ValueError(f"tier model {m!r} rejects sampling parameters (a non-default temperature is a 400); "
                             "set supports_temperature: false")
        if self.effort is not None and rejects_effort(m):
            raise ValueError(f"tier model {m!r} does not take an effort level (a 400); set effort: null")
        if self.effort is None and thinks_by_default(m):
            raise ValueError(f"tier model {m!r} thinks by default at its own default effort; name one explicitly "
                             "(effort: low for a per-tick decision)")
        if self.refusal_fallbacks and not fallback_targets(m):
            raise ValueError(f"tier model {m!r} has no documented server-side refusal fallback (and the hold could not "
                             "bound one); set refusal_fallbacks: false")
        listed = MODEL_PRICES.get(m)
        configured = (self.price_in_per_mtok, self.price_out_per_mtok, self.price_cache_read_per_mtok, self.price_cache_write_per_mtok)
        if listed is not None and tuple(float(x) for x in listed) != tuple(float(x) for x in configured):
            raise ValueError(f"tier model {m!r} is billed at {listed} USD/MTok (input, output, cache read, cache write) "
                             f"but the tier configures {configured}; metering, holds and caps would all be wrong")


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


class MemoryPolicyConfig(StrictModel):
    """The agent-owned practices file ``/memories/memory_policy.md`` (MEMORY_EVOLUTION §5.5)."""

    enabled: bool = False
    seed: Literal["blank", "default", "diverse"] = "default"   # S0 / S1 / S2 in the proposal
    max_chars: int = 2000
    max_edits_per_day: int = 5
    show_in_prompt: bool = True       # rendered fenced in the user turn, never in the system prompt
    inherit: bool = True              # a child starts from its parent's policy (source ``inherited``)
    # Kernel mutation at birth (§5.7, source ``mutated``): the kernel rewrites a newborn's policy with a small random
    # change. Built for scripted tiers only (VoidConfig rejects it when a non-scripted tier is rostered).
    mutate_at_birth: bool = False

    @model_validator(mode="after")
    def _check(self) -> MemoryPolicyConfig:
        if self.max_chars < 100:
            raise ValueError("memory.policy.max_chars must be >= 100")
        if self.max_edits_per_day < 0:
            raise ValueError("memory.policy.max_edits_per_day must be >= 0")
        return self


class MaintenanceConfig(StrictModel):
    """The paid nightly maintenance step (MEMORY_EVOLUTION §5.6): one structured call per living agent per day."""

    enabled: bool = False
    mode: Literal["forced", "optional"] = "forced"
    rights: list[MaintenanceRight] = Field(default_factory=lambda: ["notes", "policy", "structure"])
    feedback: Literal["F0", "F1", "F2"] = "F1"   # none / day outcomes / outcomes plus retrieval diagnostics
    payer: Literal["agent", "research_pool"] = "agent"
    max_commands: int = 30
    max_files: int = 200
    max_file_bytes: int = 8000
    max_vault_bytes: int = 200_000
    max_tokens: int = 1024            # output cap of the maintenance call (also bounds its hold)
    # How a scripted (planted) strategy's call is billed. "actual": by the tokens its batch would take, so a
    # strategy that writes more pays more. "flat": every scripted call is billed as if it used the whole output
    # cap, so planted strategies differ only in what they do, not in what they pay (the E0 controls, §6.2).
    # Provider tiers are always billed by actual usage.
    scripted_billing: Literal["actual", "flat"] = "actual"
    notes_shown: int = 12             # today's newest notes listed in the prompt
    note_chars_shown: int = 240

    @model_validator(mode="after")
    def _check(self) -> MaintenanceConfig:
        if self.mode == "optional":
            # maintenance as an action the agent chooses is a later arm; refusing it beats silently running forced
            raise ValueError("memory.maintenance.mode optional is not built (MEMORY_EVOLUTION 5.6 arm, E8); use forced")
        if len(set(self.rights)) != len(self.rights):
            raise ValueError("memory.maintenance.rights has duplicates")
        for name in ("max_commands", "max_files", "max_file_bytes", "max_vault_bytes", "max_tokens", "notes_shown", "note_chars_shown"):
            if getattr(self, name) <= 0:
                raise ValueError(f"memory.maintenance.{name} must be > 0")
        if self.max_file_bytes > self.max_vault_bytes:
            raise ValueError("memory.maintenance.max_file_bytes must be <= max_vault_bytes")
        return self


class ExamConfig(StrictModel):
    """The audit exam (MEMORY_EVOLUTION §5.8, Appendix E): questions about what the agent itself was shown,
    graded by exact match, paid by the research pool, never rewarded. The reward exam is a later arm."""

    enabled: bool = False
    delay_days: int = 2               # ask about facts shown at least this many days earlier
    questions_per_agent: int = 3
    kinds: list[Literal["balance", "neighbour", "node", "heard", "transfer", "claim"]] = Field(
        default_factory=lambda: ["balance", "neighbour", "node"])
    max_tokens: int = 200
    guess_penalty: float = 0.5        # formula scoring: correct +1, abstain 0, wrong -guess_penalty (abstain when unsure)

    @model_validator(mode="after")
    def _check(self) -> ExamConfig:
        if self.delay_days < 1 or self.questions_per_agent < 1 or self.max_tokens <= 0:
            raise ValueError("memory.exam: delay_days and questions_per_agent must be >= 1, max_tokens > 0")
        if not self.kinds or len(set(self.kinds)) != len(self.kinds):
            raise ValueError("memory.exam.kinds must be non-empty and unique")
        if not (0.0 <= self.guess_penalty <= 1.0):
            raise ValueError("memory.exam.guess_penalty must be within [0, 1]")
        return self


class PracticesConfig(StrictModel):
    """Practice transmission between agents (MEMORY_EVOLUTION §5.7): an agent suggests an excerpt of its policy to a
    neighbour with ``share_practice`` (delivered fenced); the kernel measures adoption, never self-report."""

    enabled: bool = False
    excerpt_max_chars: int = 300        # the longest excerpt one suggestion may carry
    adopt_similarity: float = 0.6       # adopted: the receiver's new policy contains the excerpt at this similarity
    persist_days: int = 2               # an adopted practice that survives this many days counts as persisted
    scripted_share_weight: float = 0.3  # scripted tiers: share_practice utility = weight * sociability

    @model_validator(mode="after")
    def _check(self) -> PracticesConfig:
        if not (1 <= self.excerpt_max_chars <= 2000):
            raise ValueError("memory.practices.excerpt_max_chars must be within [1, 2000]")
        if not (0.0 <= self.adopt_similarity <= 1.0):
            raise ValueError("memory.practices.adopt_similarity must be within [0, 1]")
        if self.persist_days < 1:
            raise ValueError("memory.practices.persist_days must be >= 1")
        if self.scripted_share_weight < 0:
            raise ValueError("memory.practices.scripted_share_weight must be >= 0")
        return self


class CanaryConfig(StrictModel):
    """Canary facts (§5.8): facts delivered as ordinary observations and recorded kernel-side (``canary_facts``)."""

    enabled: bool = False
    per_day: int = 1

    @model_validator(mode="after")
    def _check(self) -> CanaryConfig:
        if not (1 <= self.per_day <= 5):
            raise ValueError("memory.selection.canary.per_day must be within [1, 5]")
        return self


class SelectionArmConfig(StrictModel):
    """What the world rewards beyond survival (§5.8, Appendix D). A: nothing extra. B: the reward exam about canary
    facts pays ``recall_reward_usd`` per correct answer. C: the source of an adopted practice that persists earns
    ``practice_royalty_usd``. Both rewards come from the research pool (ledger kinds ``recall_reward`` and
    ``practice_royalty``)."""

    arm: Literal["A", "B", "C"] = "A"
    canary: CanaryConfig = CanaryConfig()
    reward_delay_days: int = 2            # the reward exam asks about canary facts at least this many days old
    reward_questions_per_agent: int = 2
    recall_reward_usd: float = 0.02
    practice_royalty_usd: float = 0.02

    @model_validator(mode="after")
    def _check(self) -> SelectionArmConfig:
        if self.reward_delay_days < 1 or self.reward_questions_per_agent < 1:
            raise ValueError("memory.selection: reward_delay_days and reward_questions_per_agent must be >= 1")
        if self.recall_reward_usd < 0 or self.practice_royalty_usd < 0:
            raise ValueError("memory.selection: recall_reward_usd and practice_royalty_usd must be >= 0")
        return self


class MemoryConfig(StrictModel):
    # The amnesic switch (MEMORY_EVOLUTION §5.2, E1 arm a0): False drops every `remember` and returns no
    # retrieved notes. The self-summary stays (identity, at most self_max_words) so prompts remain comparable.
    enabled: bool = True
    # R1 retrieval (§5.3): notes the agent tags ``index`` get this additive entry-point boost (0 = R0, today).
    index_boost: float = 0.0
    policy: MemoryPolicyConfig = MemoryPolicyConfig()
    maintenance: MaintenanceConfig = MaintenanceConfig()
    exam: ExamConfig = ExamConfig()
    practices: PracticesConfig = PracticesConfig()
    selection: SelectionArmConfig = SelectionArmConfig()
    embedding_dim: int = 256
    entry_k: int = 4
    hops: int = 2
    hop_decay: float = 0.6
    max_retrieved: int = 8   # notes retrieved into each decision prompt (the retrieval-size knob; E0 uses 4)
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


class HypothesisConfig(StrictModel):
    """One pre-registered contrast between two arms (MEMORY_EVOLUTION §6.11, §5.2, §6.2).

    The effect is ``delta = arm_b - arm_a`` of ``outcome`` (an end-of-run outcome path), paired by seed.
    ``superiority`` passes when delta clears ``margin`` in ``direction``; ``non_superiority`` when it is not
    significantly beyond the margin in that direction; ``equivalence`` (two one-sided tests) when it lies within
    +/- margin, so a claim of no effect states its margin. ``margin_unit: sd`` reads the margin in standard deviations
    of the paired differences. Holm correction runs across the hypotheses with ``primary: true``; ``min_power`` also
    requires at least that power at the observed effect size.
    """

    name: str
    arm_a: str
    arm_b: str
    outcome: str
    kind: Literal["superiority", "non_superiority", "equivalence"] = "superiority"
    direction: Literal["greater", "less"] = "greater"
    margin: float = 0.0
    margin_unit: Literal["abs", "sd"] = "abs"
    primary: bool = True
    min_power: float | None = None

    @model_validator(mode="after")
    def _check(self) -> HypothesisConfig:
        if not self.name.strip():
            raise ValueError("experiment.hypotheses: name must be non-empty")
        if self.arm_a == self.arm_b:
            raise ValueError(f"experiment.hypotheses {self.name!r}: arm_a and arm_b must differ")
        if self.margin < 0:
            raise ValueError(f"experiment.hypotheses {self.name!r}: margin must be >= 0")
        if self.kind == "equivalence" and self.margin <= 0:
            raise ValueError(f"experiment.hypotheses {self.name!r}: an equivalence hypothesis needs margin > 0")
        if self.min_power is not None and not (0.0 < self.min_power < 1.0):
            raise ValueError(f"experiment.hypotheses {self.name!r}: min_power must be within (0, 1)")
        return self


class ExperimentConfig(StrictModel):
    name: str | None = None
    arm: str | None = None
    independent_variables: list[str] = Field(default_factory=list)
    primary_outcome: str | None = None
    secondary_outcomes: list[str] = Field(default_factory=list)
    tracer: TracerConfig = TracerConfig()
    hypotheses: list[HypothesisConfig] = Field(default_factory=list)

    @field_validator("hypotheses")
    @classmethod
    def _unique_hypotheses(cls, v: list[HypothesisConfig]) -> list[HypothesisConfig]:
        names = [h.name for h in v]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"experiment.hypotheses has duplicate names: {dupes}")
        return v


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

    @model_validator(mode="after")
    def _memory_layer_consistency(self) -> VoidConfig:
        m = self.memory
        if m.maintenance.enabled and not m.enabled:
            raise ValueError("memory.maintenance.enabled requires memory.enabled (an amnesic agent has nothing to maintain)")
        if "policy" in m.maintenance.rights and m.maintenance.enabled and not m.policy.enabled:
            raise ValueError("memory.maintenance.rights includes 'policy' but memory.policy.enabled is false")
        if m.exam.enabled and not self.run.log_prompts:
            raise ValueError("memory.exam.enabled requires run.log_prompts (questions are generated from what was shown)")
        if m.exam.enabled and "claim" in m.exam.kinds and self.world.claim_feedback_p <= 0:
            raise ValueError("memory.exam.kinds includes 'claim' but world.claim_feedback_p is 0 (nothing to ask about)")
        if m.index_boost < 0:
            raise ValueError("memory.index_boost must be >= 0")
        if m.max_retrieved < 1:
            raise ValueError("memory.max_retrieved must be >= 1 (switch retrieval off with memory.enabled: false)")
        if m.policy.mutate_at_birth:
            # every tier an agent can be born on: the roster and arrivals (a child's tier never crosses a provider)
            live = {a.tier for a in self.agents if a.tier is not None} | {e.arrival.tier for e in self.epochs if e.arrival is not None}
            not_scripted = sorted(t for t in live if t in self.tiers and self.tiers[t].provider != "scripted")
            if not_scripted:
                raise ValueError(f"kernel mutation at birth is built for scripted tiers only "
                                 f"(memory.policy.mutate_at_birth with tiers {not_scripted})")
        sel = m.selection
        if sel.arm == "B" and not (sel.canary.enabled and self.chronicle.enabled):
            raise ValueError("memory.selection.arm B needs memory.selection.canary.enabled and chronicle.enabled "
                             "(the reward exam asks about canary facts delivered as chronicle lines)")
        if sel.arm == "C" and not m.practices.enabled:
            raise ValueError("memory.selection.arm C needs memory.practices.enabled (royalties pay for adopted practices)")
        return self

    def warnings(self) -> list[str]:
        """Load-time sanity warnings (never fatal): economy balance, cap arithmetic, and output caps too small
        for a model that thinks by default (thinking counts toward max_tokens and is billed as output)."""
        out: list[str] = []
        income = self.world.forage_yield_usd * self.world.forage_units_per_tick * self.world.scarcity
        rostered = sorted({a.tier for a in self.agents})
        for name in rostered:
            t = self.tiers[name]
            out_tokens = 150 + self.thinking_allowance(name)
            est = (1500 * t.price_in_per_mtok + out_tokens * t.price_out_per_mtok) / 1_000_000
            if self.economy.world_pricing == "equalized":
                est = (1500 * self.economy.equalized_price_in_per_mtok + out_tokens * self.economy.equalized_price_out_per_mtok) / 1_000_000
            ratio = est / income if income > 0 else float("inf")
            if not (0.3 <= ratio <= 2.0):
                out.append(f"tier {name!r}: estimated call ${est:.4f} vs forage income ${income:.4f}/tick (ratio {ratio:.2f}, want 0.3-2.0)")
        if self.economy.total_cap_usd < self.run.days * self.economy.daily_cap_usd:
            out.append("economy.total_cap_usd < days * daily_cap_usd: the total cap will bind before the last day")
        thinking = [n for n in rostered if self.tiers[n].provider == "anthropic" and thinks_by_default(self.tiers[n].model)]
        for name in thinking:
            t = self.tiers[name]
            if t.max_tokens < MIN_THINKING_DECISION_TOKENS:
                out.append(f"tier {name!r} thinks before it answers: max_tokens {t.max_tokens} < {MIN_THINKING_DECISION_TOKENS} "
                           "leaves little room for the decision JSON")
            if self.memory.maintenance.enabled and self.memory.maintenance.max_tokens < MIN_THINKING_MAINTENANCE_TOKENS:
                out.append(f"tier {name!r} thinks before it answers: memory.maintenance.max_tokens {self.memory.maintenance.max_tokens} "
                           f"< {MIN_THINKING_MAINTENANCE_TOKENS} will cut batches off (nothing applied, still charged)")
            exam_cap = min(t.max_tokens, self.memory.exam.max_tokens)
            if self.memory.exam.enabled and exam_cap < MIN_THINKING_EXAM_TOKENS:
                out.append(f"tier {name!r} thinks before it answers: an exam call may emit {exam_cap} tokens "
                           f"< {MIN_THINKING_EXAM_TOKENS} (min of memory.exam.max_tokens and the tier's max_tokens)")
        return out

    def thinking_allowance(self, tier: str) -> int:
        """Output tokens a call on ``tier`` is assumed to spend thinking, for the load-time cost estimate only
        (`[assumption]`, calibrate from llm_calls.output_tokens after a live run): none for a tier that does not
        think by default."""
        t = self.tiers[tier]
        if t.provider != "anthropic" or not thinks_by_default(t.model):
            return 0
        return THINKING_ALLOWANCE.get(t.effort or "medium", THINKING_ALLOWANCE["medium"])

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

    def canonical_dict(self) -> dict[str, Any]:
        """The resolved config as stored and hashed.

        Fields added after runs already existed (``_LATE_DEFAULTS``: ``world.fallow_ticks``, ``world.veins``,
        ``memory.policy.mutate_at_birth``, ``memory.practices``, ``memory.selection``, ``experiment.hypotheses``,
        ``population.scripted_rules``) are
        left out while they hold their defaults, so a config that does not use them hashes exactly as it did before
        they existed: older runs stay resumable and comparable. Loading the stored YAML restores the defaults, so two
        stored configs are best compared after loading both (``load_config(path).model_dump(mode="json")``): a block
        at its default is absent from one file and present in the other.
        """
        data = self.model_dump(mode="json")
        for path, default in _LATE_DEFAULTS.items():
            *parents, key = path.split(".")
            node: Any = data
            for p in parents:
                node = node.get(p) if isinstance(node, dict) else None
            if isinstance(node, dict) and key in node and node[key] == default:
                del node[key]
        return data

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"))

    def hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.canonical_dict(), sort_keys=True)

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
            "view_radius": self.world.view_radius,
            "memory_layer": {"enabled": self.memory.enabled, "policy": self.memory.policy.enabled,
                             "maintenance": self.memory.maintenance.enabled, "exam": self.memory.exam.enabled},
            "tiers": {k: {"color": t.color, "model": t.model, "provider": t.provider,
                          "collapse_temperature": t.collapse_temperature, "max_temperature": t.max_temperature,
                          "rank": self.rung_of(k)}
                      for k, t in self.tiers.items()},
            "intelligence": {"ladder": list(self.intelligence.ladder)},
        }


# Fields omitted from the canonical form at these values, by dotted path (see VoidConfig.canonical_dict).
_LATE_DEFAULTS: dict[str, Any] = {
    "world.fallow_ticks": 0,
    "world.veins": VeinsConfig().model_dump(mode="json"),
    "memory.policy.mutate_at_birth": False,
    "memory.practices": PracticesConfig().model_dump(mode="json"),
    "memory.selection": SelectionArmConfig().model_dump(mode="json"),
    "experiment.hypotheses": [],
    "population.scripted_rules": "design_7_2",
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
