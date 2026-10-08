"""The nightly memory-maintenance step (MEMORY_EVOLUTION §5.6, Appendix C) `[scaffold]`.

At the day boundary, before the first tick of the new day, every living agent (in ``agent_id`` order,
concurrency bounded by the provider semaphore) gets one paid, structured call: it sees its policy, a
listing of its vault, the day's new notes and a summary of the day's outcomes, and answers with a batch of
:class:`~void.memory.commands.MemoryCommand` which the kernel validates and applies.

Rules the implementation (WP5) must keep:

* **One call, not a tool loop.** Cheaper, provider-agnostic, deterministic for scripted tiers.
* **The agent pays** (``memory.maintenance.payer: agent``) through ``Wallet.charged_call`` with purpose
  ``maintain``; an agent that cannot afford the hold is skipped and a ``MAINTENANCE_SKIPPED`` event says so.
  Poverty degrading maintenance is a feature of the design, not a bug.
* **Scripted tiers never call a provider.** Their ``maintenance_strategy`` (``noop``, ``index_builder``,
  ``hoarder``, ``decoy``, ``summarize``) produces the command batch deterministically from the run's RNG,
  and synthetic usage is metered exactly like a decision call so the cost side is identical across arms.
* **Every call is logged** in ``llm_calls`` with purpose ``maintain`` and the full prompt (``run.log_prompts``),
  every command in ``memory_commands``, and the day's result in one ``MAINTENANCE`` event.
* **Resume-safe**: the step records ``maintenance_day`` in ``world_kv`` so a restarted run never bills twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from void.memory.commands import MemoryCommand

if TYPE_CHECKING:
    from void.sim.loop import Simulation

__all__ = ["MaintenanceResponse", "MaintenanceReport", "Maintenance", "maintenance_system_prompt"]

MAX_REFLECTION_CHARS = 600


class MaintenanceResponse(BaseModel):
    """The strict structured output of one maintenance call (Appendix C)."""

    model_config = {"extra": "forbid"}
    reflection: str = Field(max_length=MAX_REFLECTION_CHARS)
    commands: list[MemoryCommand] = Field(default_factory=list, max_length=30)
    adopted_from: list[str | None] = Field(default_factory=list, max_length=4)  # self-reported, never used for scoring


@dataclass
class MaintenanceReport:
    day: int
    tick: int
    agents: int = 0
    called: int = 0
    skipped: dict[str, int] = field(default_factory=dict)     # reason -> count
    commands_ok: int = 0
    commands_refused: int = 0
    real_cost: int = 0
    world_cost: int = 0


def maintenance_system_prompt(cfg: Any, tier_name: str) -> str:
    """Cache-stable system prompt for the maintenance call: the world's standing rules plus the carve-out.

    Kept as a pure function of ``(config, tier)`` like :func:`void.brain.prompt.system_prompt`.
    """
    from void.brain.prompt import system_prompt

    return (
        system_prompt(cfg, tier_name)
        + "\n\nMAINTENANCE: once a day you may reorganize your own memory with the memory commands "
        "(view, create, str_replace, insert, delete, rename) over /memories. Your memory policy is advice you "
        "gave yourself; follow it unless it conflicts with these rules. You pay for this call from your own balance. "
        "Respond with one JSON object: `reflection` (private), `commands` (a batch applied in order) and "
        "`adopted_from` (ids of neighbours whose advice you took, or empty)."
    )


class Maintenance:
    """Runs the step for one day. Constructed by the simulation; a no-op while ``memory.maintenance.enabled`` is false."""

    def __init__(self, sim: Simulation) -> None:
        self.sim = sim
        self.cfg = sim.cfg.memory.maintenance

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    async def run_day(self, day: int, tick: int) -> MaintenanceReport | None:
        """Maintain every living agent's memory at the start of ``day`` (about the day that just ended).

        Returns None when disabled or when ``day`` was already maintained (resume). WP5 implements the body:
        build the prompt (policy, listing, new notes, outcomes at the configured feedback level), route the
        call through the wallet, parse :class:`MaintenanceResponse`, apply the commands through
        :class:`~void.memory.commands.MemoryCommands` with the configured rights, log and emit.
        """
        if not self.enabled or day <= 1:
            return None
        done = self.sim.db.kv_get("maintenance_day")
        if done is not None and int(done) >= day:
            return None
        raise NotImplementedError("Maintenance.run_day is work package WP5 (MEMORY_EVOLUTION §10.1)")
