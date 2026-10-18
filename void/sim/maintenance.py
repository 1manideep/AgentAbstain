"""The nightly memory-maintenance step (MEMORY_EVOLUTION §5.6, Appendix C).

At the day boundary, before the first tick of the new day, every living agent (in ``agent_id`` order,
concurrency bounded by the provider semaphore) gets one paid, structured call: it sees its policy, a
listing of its vault, the day's new notes and a summary of the day's outcomes, and answers with a batch of
:class:`~void.memory.commands.MemoryCommand` which the kernel validates and applies.

Rules the implementation keeps:

* **One call, not a tool loop.** Cheaper, provider-agnostic, deterministic for scripted tiers. The user turn is
  rendered by :meth:`Maintenance.render_prompt` with every agent-authored piece (policy, listing, note titles
  and bodies, retrieved titles) inside :func:`~void.brain.prompt.fence`; the system prompt is the cache-stable
  :func:`maintenance_system_prompt`. Feedback levels: ``F0`` shows no outcomes, ``F1`` the day's income,
  spend, failures, forages and talks (from the ledger and the event log), ``F2`` also the titles that were
  retrieved during the day but never mentioned in a thought or an utterance.
* **The agent pays** (``memory.maintenance.payer: agent``) through ``Wallet.gate`` / ``Wallet.meter`` with
  purpose ``maintain`` (ledger kind ``maintenance_call``); the hold is the one a decision call would carry
  with the maintenance output cap. An agent that cannot afford the hold is skipped and a
  ``MAINTENANCE_SKIPPED`` event says so. Poverty degrading maintenance is a feature of the design, not a bug.
  With ``payer: research_pool`` the kernel's research wallet is gated and metered instead. An agent whose
  balance after metering no longer covers its next decision call is flagged ``pending_bankrupt`` with cause
  ``maintenance``; the end of the first tick resolves it, as for any other cause.
* **Scripted tiers never call a provider.** Their ``maintenance_strategy`` (``noop``, ``index_builder``,
  ``hoarder``, ``decoy``, ``summarize``; :mod:`void.brain.maintenance_strategies`) produces the command batch
  deterministically from the run's RNG stream ``("maintain", agent_id, day)``, and synthetic usage (prompt and
  response tokens as the scripted brain estimates them, the system prompt as a cache write on the agent's
  first maintenance call and a cache read afterwards) is metered exactly like a decision call, so the cost
  side is identical across arms. Provider tiers get one :func:`~void.brain.utility_calls.complete_json` call
  constrained by the strict :class:`MaintenanceResponse` schema; a refusal or an unparsable answer means zero
  commands, but the call is still paid.
* **Every call is logged** in ``llm_calls`` with purpose ``maintain``, the full prompt, the full response text and
  the system hash (``run.log_prompts``; the system text is content-addressed in ``prompt_texts``; without
  ``log_prompts`` the response is cut at 4000 characters), the reflection as
  ``thought`` and the batch counts in ``extras``; every command in ``memory_commands`` (through
  :class:`~void.memory.commands.MemoryCommands`) with one ``MEMORY_COMMAND`` event each, a ``POLICY_CHANGED``
  event when a command produced a new policy version, and the day's result in one ``MAINTENANCE`` event.
* **Transactions.** Per agent: gate, build, call, meter (outside any transaction, the wallet insists), then one
  ``db.tx()`` for the commands, the call row and the events.
* **Resume-safe**: the step records ``maintenance_day`` in ``world_kv`` so a restarted run never bills twice.

``mode: optional`` (maintenance as an action the agent chooses) is out of scope for the thin slice and behaves
like ``forced``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from void.brain.base import BrainResult, Usage
from void.brain.decision import _strictify, parse_lenient, sanitize_text
from void.brain.maintenance_strategies import StrategyContext, nodes_from_notes, plan
from void.brain.prompt import estimate_tokens, fence
from void.config import micro_to_usd
from void.events import Event, Kind
from void.memory.commands import POLICY_PATH, MemoryCommand, virtual_path
from void.memory.store import ENTITY_TAG, PROBE_TAG

if TYPE_CHECKING:
    from void.agents.models import AgentRecord
    from void.sim.loop import Simulation

__all__ = ["MaintenanceResponse", "MaintenanceReport", "Maintenance", "maintenance_system_prompt", "response_schema"]

MAX_REFLECTION_CHARS = 600
MAX_UNUSED_TITLES = 12       # retrieval diagnostics shown at F2
_UNKNOWN_USAGE = ("timeout", "connection", "exception")  # error classes whose usage is unknown: the hold is charged


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

    tier = cfg.tiers[tier_name]
    if tier.provider == "scripted":  # unchanged: a planted strategy's cost is a function of this prompt's length
        return (
            system_prompt(cfg, tier_name)
            + "\n\nMAINTENANCE: once a day you may reorganize your own memory with the memory commands "
            "(view, create, str_replace, insert, delete, rename) over /memories. Your memory policy is advice you "
            "gave yourself; follow it unless it conflicts with these rules. You pay for this call from your own balance. "
            "Respond with one JSON object: `reflection` (private), `commands` (a batch applied in order) and "
            "`adopted_from` (ids of neighbours whose advice you took, or empty)."
        )
    # a provider tier: the OUTPUT clause names this call's schema and cap, not the decision's, and the reflection
    # is a short statement of what changed and why (not a request to reproduce internal reasoning)
    output = ("OUTPUT: this call is your nightly memory maintenance, not a turn in the world: respond with one JSON "
              "object matching the provided schema: `reflection` (one or two sentences on what you change and why), "
              f"`commands` (at most {int(cfg.memory.maintenance.max_commands)}, applied in order) and `adopted_from` "
              "(ids of neighbours whose advice you took, or empty). "
              f"Your output cap for this call is {int(cfg.memory.maintenance.max_tokens)} tokens.")
    return (
        system_prompt(cfg, tier_name, output_clause=output)
        + "\n\nMAINTENANCE: once a day you may reorganize your own memory with the memory commands "
        "(view, create, str_replace, insert, delete, rename) over /memories. Your memory policy is advice you "
        "gave yourself; follow it unless it conflicts with these rules. You pay for this call from your own balance."
    )


def response_schema() -> dict[str, Any]:
    """The strict JSON schema of :class:`MaintenanceResponse` as the providers' structured output wants it."""
    return _strictify(MaintenanceResponse.model_json_schema())


class Maintenance:
    """Runs the step for one day. Constructed by the simulation; a no-op while ``memory.maintenance.enabled`` is false."""

    def __init__(self, sim: Simulation) -> None:
        self.sim = sim
        self.cfg = sim.cfg.memory.maintenance
        self.last_report: MaintenanceReport | None = None
        self._schema: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    @property
    def schema(self) -> dict[str, Any]:
        if self._schema is None:
            self._schema = response_schema()
        return self._schema

    # --- the prompt pieces ---------------------------------------------------------------------------------
    def day_range(self, day: int) -> tuple[int, int]:
        """First and last tick of the day that ended when ``day`` began (ticks are 1-based)."""
        t = int(self.sim.cfg.run.ticks_per_day)
        return (day - 2) * t + 1, (day - 1) * t

    def note_dicts(self, agent_id: str) -> list[dict[str, Any]]:
        """Every live note of the agent as the strategies and the prompt see it (virtual path, body, tick, tags)."""
        return [{"path": virtual_path(n), "title": n.title, "body": n.body, "tick": int(n.created_tick), "tags": list(n.tags)}
                for n in self.sim.memory.list_notes(agent_id)]

    def outcomes_for(self, agent_id: str, day: int) -> dict[str, Any]:
        """The day's outcomes from the ledger and the event log: forage income, thinking spend, failures, forages, talks."""
        lo, hi = self.day_range(day)
        db = self.sim.db
        income = db.fetchone("SELECT COALESCE(SUM(delta),0) AS s FROM wallet_ledger WHERE agent_id=? AND kind='forage' "
                             "AND tick BETWEEN ? AND ?", (agent_id, lo, hi))
        spend = db.fetchone("SELECT COALESCE(SUM(delta),0) AS s FROM wallet_ledger WHERE agent_id=? "
                            "AND kind IN ('llm_call','gossip_call') AND tick BETWEEN ? AND ?", (agent_id, lo, hi))

        def count(kind: str) -> int:
            row = db.fetchone("SELECT COUNT(*) AS n FROM events WHERE kind=? AND agent_id=? AND tick BETWEEN ? AND ?",
                              (kind, agent_id, lo, hi))
            return int(row["n"]) if row is not None else 0

        return {
            "income_usd": round(micro_to_usd(int(income["s"]) if income is not None else 0), 6),
            "spend_usd": round(micro_to_usd(-int(spend["s"]) if spend is not None else 0), 6),
            "failed_actions": count(Kind.ACTION_FAILED), "forages": count(Kind.FORAGE), "talks": count(Kind.TALK),
        }

    def unused_retrievals(self, agent_id: str, day: int) -> list[str]:
        """Titles retrieved for the agent during the day that no thought or utterance of that day mentioned (F2)."""
        lo, hi = self.day_range(day)
        rows = self.sim.db.fetchall("SELECT extras, thought, raw_text FROM llm_calls WHERE agent_id=? AND purpose='decide' "
                                    "AND tick BETWEEN ? AND ? ORDER BY tick, call_id", (agent_id, lo, hi))
        retrieved: list[str] = []
        haystack: list[str] = []
        for r in rows:
            try:
                extras = json.loads(r["extras"] or "{}")
            except (TypeError, ValueError):
                extras = {}
            for t in extras.get("retrieved_titles", []) or []:
                if str(t) not in retrieved:
                    retrieved.append(str(t))
            haystack.append(f"{r['thought'] or ''} {r['raw_text'] or ''}".lower())
        hay = " ".join(haystack)
        return [t for t in retrieved if t.lower() not in hay][:MAX_UNUSED_TITLES]

    def context_for(self, rec: AgentRecord, day: int, tick: int) -> StrategyContext:
        """The structured prompt pieces for ``rec`` at the start of ``day`` (about ``day - 1``)."""
        aid = rec.agent_id
        notes = self.note_dicts(aid)
        lo, hi = self.day_range(day)
        new = [n for n in notes if lo <= n["tick"] <= hi and not ({ENTITY_TAG, PROBE_TAG} & set(n["tags"]))]
        return StrategyContext(
            agent_id=aid, day=day - 1, policy_text=self.sim.policy.text(aid), listing=self.sim.commands.listing(aid, tick=tick),
            new_notes=new, notes=notes, outcomes=self.outcomes_for(aid, day), nodes_known=nodes_from_notes(notes),
            rights=tuple(self.cfg.rights), max_commands=int(self.cfg.max_commands),
            policy_max_chars=int(self.sim.cfg.memory.policy.max_chars), max_file_bytes=int(self.cfg.max_file_bytes),
        )

    def render_prompt(self, rec: AgentRecord, ctx: StrategyContext, *, feedback: str | None = None) -> str:
        """The user turn of the maintenance call (Appendix C). Agent-authored text is fenced; ``feedback``
        defaults to the configured level."""
        fb = feedback or self.cfg.feedback
        d = ctx.day
        bal = micro_to_usd(self.sim.wallet.balance(rec.agent_id))
        lines = [
            f"# Memory maintenance at the start of day {d + 1}, about day {d}",
            f"You are {rec.name} (id {rec.agent_id}), generation {rec.generation}, tier {rec.model_tier}. Balance ${bal:.3f}.",
            "", "## Your memory policy (written by you)",
        ]
        if ctx.policy_text is None:
            lines.append("(no policy file in this world)")
        elif not ctx.policy_text.strip():
            lines.append("(empty: you have not written one yet)")
        else:
            lines.append(fence(ctx.policy_text))
        lines += ["", "## Your vault (sizes in bytes, tick of the last change)", fence(ctx.listing)]
        newest = sorted(ctx.new_notes, key=lambda n: (-int(n["tick"]), str(n["path"])))[: self.cfg.notes_shown]
        lines += ["", f"## New notes from day {d} ({len(newest)} of {len(ctx.new_notes)} shown)"]
        if newest:
            for n in newest:
                body = str(n["body"])[: self.cfg.note_chars_shown]
                lines.append(f"- {n['path']} titled {fence(str(n['title']))}: {fence(body)}")
        else:
            lines.append("- (none)")
        if fb in ("F1", "F2"):
            o = ctx.outcomes
            lines += ["", f"## Outcomes of day {d}",
                      f"Income ${float(o.get('income_usd', 0.0)):.3f} from {int(o.get('forages', 0))} forages; spent "
                      f"${float(o.get('spend_usd', 0.0)):.3f} on thinking; {int(o.get('failed_actions', 0))} failed actions; "
                      f"{int(o.get('talks', 0))} conversations."]
        if fb == "F2":
            unused = self.unused_retrievals(rec.agent_id, d + 1)
            lines += ["", "## Retrieval diagnostics",
                      "Notes that came to mind during the day but were never acted on: "
                      + (", ".join(fence(t) for t in unused) if unused else "(none)")]
        rights = ", ".join(ctx.rights) if ctx.rights else "none"
        lines += ["", "## Budget",
                  f"At most {ctx.max_commands} commands this call; the policy may hold at most {ctx.policy_max_chars} characters. "
                  f"Your rights: {rights} (notes: create under /memories/notes/; policy: edit /memories/memory_policy.md; "
                  f"structure: str_replace, insert, delete, rename, and files under /memories/index/)."]
        return "\n".join(lines)

    # --- the call -------------------------------------------------------------------------------------------
    def _scripted(self, rec: AgentRecord, ctx: StrategyContext, prompt: str, system_tokens: int, day: int,
                  ) -> tuple[BrainResult, MaintenanceResponse]:
        """A planted strategy's batch with synthetic usage, metered like a scripted decision."""
        tier = self.sim.cfg.tiers[rec.model_tier]
        try:
            response = plan(tier.maintenance_strategy or "noop", ctx, self.sim.rng.stream("maintain", rec.agent_id, day))
        except ValueError as e:  # a planted strategy built an invalid command: the call is paid, nothing is applied
            response = MaintenanceResponse(reflection=f"strategy error: {type(e).__name__}"[:MAX_REFLECTION_CHARS], commands=[])
        raw = json.dumps(response.model_dump(exclude_none=True), separators=(",", ":"))
        first = self.sim.db.fetchone("SELECT 1 AS x FROM llm_calls WHERE agent_id=? AND purpose='maintain' LIMIT 1",
                                     (rec.agent_id,)) is None
        out_tokens = int(self.cfg.max_tokens) if self.cfg.scripted_billing == "flat" else estimate_tokens(raw)
        usage = Usage(input_tokens=estimate_tokens(prompt), output_tokens=out_tokens,
                      cache_read_tokens=0 if first else system_tokens, cache_write_tokens=system_tokens if first else 0)
        return BrainResult(None, usage, 0, "end_turn", raw, model=tier.model), response

    async def _provider(self, rec: AgentRecord, prompt: str, system: str) -> tuple[BrainResult, MaintenanceResponse | None]:
        """One structured provider call; a refusal, an error or an unparsable answer yields no response."""
        from void.brain.utility_calls import complete_json

        brain = self.sim.brains.get(rec.model_tier)
        if brain is None or not hasattr(brain, "complete_json"):
            return BrainResult(None, Usage(), 0, "error", "", error="client: brain has no complete_json"), None
        try:
            result = await complete_json(brain, prompt, schema=self.schema, system=system, max_tokens=int(self.cfg.max_tokens))
        except Exception as e:  # a brain must not raise, but the wallet must still settle
            result = BrainResult(None, Usage(), 0, "error", "", error=f"exception: {type(e).__name__}: {e}"[:300])
        if result.error is not None:
            return result, None
        try:
            return result, parse_lenient(MaintenanceResponse, result.raw_text)
        except ValueError as e:  # pydantic ValidationError or a JSON error
            return dataclasses.replace(result, error=f"parse: {type(e).__name__}"), None

    def _store_prompt_text(self, text: str) -> str:
        """Content-address the system prompt in ``prompt_texts`` (as the loop does); returns its hash."""
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        self.sim.db.execute("INSERT OR IGNORE INTO prompt_texts(hash, text) VALUES(?,?)", (h, text))
        return h

    async def _maintain(self, rec: AgentRecord, day: int, tick: int, report: MaintenanceReport) -> None:
        sim, cfg = self.sim, self.cfg
        aid, tier_name = rec.agent_id, rec.model_tier
        tier = sim.cfg.tiers[tier_name]
        system = maintenance_system_prompt(sim.cfg, tier_name)
        system_tokens = estimate_tokens(system)
        ctx = self.context_for(rec, day, tick)
        prompt = self.render_prompt(rec, ctx)
        # the hold of a decision call on this tier, with the maintenance output cap instead of the tier's
        hold, cap_hold = sim.wallet.holds_for(tier.model_copy(update={"max_tokens": int(cfg.max_tokens)}), estimate_tokens(prompt),
                                              system_tokens, schema_tokens=estimate_tokens(json.dumps(self.schema, separators=(",", ":"))))
        payer = aid if cfg.payer == "agent" else "research_pool"
        gate = sim.wallet.gate(payer, hold, cap_hold=cap_hold)
        if not gate.ok:
            report.skipped[gate.reason] = report.skipped.get(gate.reason, 0) + 1
            sim.bus.emit(Event(tick, day, Kind.MAINTENANCE_SKIPPED, {"agent_id": aid, "tier": tier_name, "reason": gate.reason, "payer": payer,
                                                                   "hold_usd": micro_to_usd(hold), "balance_usd": micro_to_usd(gate.balance)}, aid))
            return
        scripted = tier.provider == "scripted"
        strategy = (tier.maintenance_strategy or "noop") if scripted else None
        if scripted:
            result, response = self._scripted(rec, ctx, prompt, system_tokens, day)
        else:
            result, response = await self._provider(rec, prompt, system)
        call_id = sim.ids.new("cl")
        unknown = result.error is not None and result.error.split(":")[0] in _UNKNOWN_USAGE
        meter = sim.wallet.meter(payer, tier, None if unknown else result.usage, tick, call_id, hold, purpose="maintain",
                                 cap_hold=cap_hold)
        report.called += 1
        report.real_cost += meter.real_cost
        report.world_cost += meter.world_cost
        commands = list(response.commands)[: int(cfg.max_commands)] if response is not None else []
        reflection = sanitize_text(response.reflection if response is not None else "", max_len=MAX_REFLECTION_CHARS)
        n_ok = n_refused = 0
        with sim.db.tx():
            for cmd in commands:
                res = sim.commands.apply(aid, cmd, tick, purpose="maintain", rights=list(cfg.rights), call_id=call_id)
                n_ok += int(res.ok)
                n_refused += int(not res.ok)
                path = res.effects.get("path") if res.ok else None
                sim.bus.emit(Event(tick, day, Kind.MEMORY_COMMAND, {
                    "agent_id": aid, "command": cmd.command, "ok": res.ok, "message": res.message[:120],
                    "path": path or sanitize_text(cmd.path or cmd.old_path or "", single_line=True, max_len=200)}, aid))
                if res.ok and res.effects.get("path") == POLICY_PATH:
                    cur = sim.policy.current(aid)
                    if cur is not None:
                        sim.bus.emit(Event(tick, day, Kind.POLICY_CHANGED, sim.policy.event_payload(cur), aid))
            logged = bool(sim.cfg.run.log_prompts)
            raw = result.raw_text or ""  # whole when prompts are logged (full response text per call, §7.4)
            extras = {"strategy": strategy, "payer": payer, "commands": len(commands), "commands_ok": n_ok, "commands_refused": n_refused}
            sim.db.execute(
                "INSERT INTO llm_calls(call_id, tick, agent_id, purpose, tier, model, provider, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, real_cost, world_cost, hold, estimated, latency_ms, stop_reason, request_id, error, raw_text, thought, extras, "
                "prompt_text, system_hash, observation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (call_id, tick, aid, "maintain", tier_name, result.model or tier.model, tier.provider, result.usage.input_tokens,
                 result.usage.output_tokens, result.usage.cache_read_tokens, result.usage.cache_write_tokens, meter.real_cost, meter.world_cost,
                 hold, int(meter.estimated), result.latency_ms, result.stop_reason, result.request_id, result.error, raw if logged else raw[:4000],
                 reflection, json.dumps(extras, sort_keys=True), prompt if logged else None, self._store_prompt_text(system) if logged else None, None),
            )
            sim.bus.emit(Event(tick, day, Kind.MAINTENANCE, {
                "agent_id": aid, "tier": tier_name, "strategy": strategy, "model": result.model or tier.model, "payer": payer,
                "commands": len(commands), "ok": n_ok, "refused": n_refused, "real_cost": meter.real_cost, "world_cost": meter.world_cost,
                "estimated": meter.estimated, "error": result.error, "reflection": reflection[:200]}, aid))
        report.commands_ok += n_ok
        report.commands_refused += n_refused
        if payer == aid and (meter.overrun or meter.balance_after < sim.hold_min_for().get(tier_name, 0)):
            sim.lifecycle.pending_bankrupt[aid] = "maintenance"  # cannot afford its next decision: resolved at the end of the tick

    async def run_day(self, day: int, tick: int) -> MaintenanceReport | None:
        """Maintain every living agent's memory at the start of ``day`` (about the day that just ended).

        Returns None when disabled, on the first day, or when ``day`` was already maintained (resume). Agents are
        visited in ``agent_id`` order; each is gated, called, metered and committed on its own (see the module
        docstring), and ``maintenance_day`` is recorded once the whole day is done.
        """
        if not self.enabled or day <= 1:
            return None
        done = self.sim.db.kv_get("maintenance_day")
        if done is not None and int(done) >= day:
            return None
        report = MaintenanceReport(day=day, tick=tick)
        for rec in sorted(self.sim.registry.alive(), key=lambda a: a.agent_id):
            report.agents += 1
            await self._maintain(rec, day, tick, report)
        self.sim.db.kv_set("maintenance_day", day)
        self.last_report = report
        return report
