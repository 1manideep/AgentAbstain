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
  ``hoarder``, ``decoy``, ``summarize``, and the §6.13 baselines ``distill``, ``revert``, ``retire``;
  :mod:`void.brain.maintenance_strategies`) produces the command batch
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
* **The revert baseline's inputs.** From the third day on, the context carries the outcomes of the day before the
  one that just ended (``previous_outcomes``) and, when the agent has two or more policy versions, the text of the
  one before its current (``previous_policy_text``). Only ``revert`` reads them; they are not rendered.

Practice transmission (MEMORY_EVOLUTION §5.7 "Adopt", §5.8 arm C, Appendix C item 5), only with ``memory.practices``
and ``memory.policy`` on (:func:`~void.brain.decision.share_practice_enabled`); with it off the prompt, the batch, the
tables and the events are exactly what they were:

* **Suggestions in the prompt.** The suggestions the agent received since its last maintenance call (or since birth;
  the newest :data:`MAX_SUGGESTIONS`, oldest first) are listed under "Suggestions received from neighbours" with their
  sources' names and ids, each excerpt fenced in a code block, before the budget.
* **Measured adoption.** After the batch, each pending excerpt is compared with the new policy text
  (:func:`~void.culture.practices.excerpt_similarity`): it is adopted when the new text holds it at
  ``memory.practices.adopt_similarity`` and the text before the batch did not (an excerpt suggested twice is credited
  once, to its oldest suggestion). Each adoption is a ``practice_events`` row ``adopted`` (with the similarity) and a
  ``PRACTICE_ADOPTED`` event (operator visibility: a measurement, not news); the batch's last policy version is
  relabelled ``adopted`` with the first adoption's source (:meth:`~void.memory.policy.PolicyStore.relabel`), so its
  ``POLICY_CHANGED`` events are emitted after the measurement, with the final labels. The answer's ``adopted_from`` is
  logged (``llm_calls.extras`` and the ``MAINTENANCE`` payload, with the measured count) and never trusted: an
  adoption event says whether the source was also self-reported.
* **Persistence.** At each day start, before the day's calls, every adoption ``memory.practices.persist_days`` or more
  old that is not yet settled becomes ``persisted`` when its receiver is alive and its policy still holds the excerpt
  at the threshold, else ``dropped`` (reason ``removed`` or ``target_dead``), with the matching event.
  ``practices_settled_through`` in ``world_kv`` (written with the rows) makes this resume-safe. Under
  ``memory.selection.arm: C`` each persisted adoption then pays its source ``practice_royalty_usd`` from the research
  pool (``Wallet.transfer``, both ledger rows of kind ``practice_royalty``, ``ref`` the persisted row's id), outside
  any transaction, with a ``PRACTICE_ROYALTY`` event (operator visibility; ``paid`` false with a reason when the source
  is dead, the royalty is zero or the pool cannot cover it). A royalty is paid at most once: a crash between the
  settled rows and the payment loses it rather than paying twice.

Lineage labels (§5.7 "Inherit and adapt"): a child's first own policy edit of the policy it inherited
(:meth:`~void.memory.policy.PolicyStore.inherited_from`) is relabelled ``adapted`` (crediting the parent) before its
``POLICY_CHANGED`` is emitted. The label is written only in worlds that turn a variation or transmission operator on
(``memory.practices`` or ``memory.policy.mutate_at_birth``), so worlds recorded before keep every edit ``self_edit``.

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
from void.brain.decision import _strictify, parse_lenient, sanitize_text, share_practice_enabled
from void.brain.maintenance_strategies import StrategyContext, nodes_from_notes, plan
from void.brain.prompt import code_fence, estimate_tokens, fence
from void.config import micro_to_usd, usd_to_micro
from void.culture.practices import PracticeLedger, excerpt_similarity
from void.events import OPERATOR, Event, Kind
from void.memory.commands import POLICY_PATH, MemoryCommand, virtual_path
from void.memory.store import ENTITY_TAG, PROBE_TAG
from void.types import AgentStatus

if TYPE_CHECKING:
    from void.agents.models import AgentRecord
    from void.memory.policy import PolicyVersion
    from void.sim.loop import Simulation

__all__ = ["MaintenanceResponse", "MaintenanceReport", "Maintenance", "maintenance_system_prompt", "response_schema",
           "MAX_SUGGESTIONS", "PRACTICE_ROYALTY_KIND"]

MAX_REFLECTION_CHARS = 600
MAX_UNUSED_TITLES = 12       # retrieval diagnostics shown at F2
MAX_SUGGESTIONS = 10         # pending practice suggestions shown (and measured) per maintenance call, newest kept
PRACTICE_ROYALTY_KIND = "practice_royalty"  # ledger kind of both rows of an arm-C royalty (§5.8)
RESEARCH_POOL = "research_pool"
SETTLED_KEY = "practices_settled_through"  # world_kv: adoptions up to this tick are settled (persisted or dropped)
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
        # practice transmission (§5.7): suggestions in the prompt, measured adoption, persistence and arm-C royalties
        self.practices_enabled = share_practice_enabled(sim.cfg)
        self.practices = PracticeLedger(sim.db)
        # the kernel's lineage labels of agent edits (adapted, adopted) only where a variation or transmission operator
        # is on, so every world recorded before keeps its edits self_edit byte for byte
        self.lineage_labels = self.practices_enabled or bool(sim.cfg.memory.policy.mutate_at_birth)

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

    def suggestions_for(self, agent_id: str) -> list[dict[str, Any]]:
        """The practice suggestions ``agent_id`` received since its last maintenance call (since birth before its first),
        the newest :data:`MAX_SUGGESTIONS`, oldest first, each with its source's name and balance (micro-dollars; 0 for
        a source no longer in the registry). Empty unless practice transmission is on. The step runs before the first
        tick's decisions, so a suggestion made on the tick of the last call came after it and is still pending."""
        if not self.practices_enabled:
            return []
        row = self.sim.db.fetchone("SELECT MAX(tick) AS t FROM llm_calls WHERE agent_id=? AND purpose='maintain'", (agent_id,))
        since = int(row["t"]) if row is not None and row["t"] is not None else 0
        out: list[dict[str, Any]] = []
        for r in self.practices.pending_for(agent_id, since)[-MAX_SUGGESTIONS:]:
            src = self.sim.registry.get(r["source_agent_id"])
            out.append({"event_id": r["event_id"], "tick": int(r["tick"]), "source_agent_id": r["source_agent_id"],
                        "source_name": src.name if src is not None else r["source_agent_id"], "excerpt": r["excerpt"],
                        "source_balance": self.sim.wallet.balance(src.agent_id) if src is not None else 0})
        return out

    def context_for(self, rec: AgentRecord, day: int, tick: int) -> StrategyContext:
        """The structured prompt pieces for ``rec`` at the start of ``day`` (about ``day - 1``).

        ``previous_outcomes`` (the outcomes of ``day - 2``) from the third day on and ``previous_policy_text`` (the
        version before the current) when there are two or more versions feed the ``revert`` baseline; the suggestions,
        the balance and the adoption threshold feed the practice take-up (both in
        :mod:`void.brain.maintenance_strategies`)."""
        aid = rec.agent_id
        notes = self.note_dicts(aid)
        lo, hi = self.day_range(day)
        new = [n for n in notes if lo <= n["tick"] <= hi and not ({ENTITY_TAG, PROBE_TAG} & set(n["tags"]))]
        history = self.sim.policy.history(aid)
        return StrategyContext(
            agent_id=aid, day=day - 1, policy_text=self.sim.policy.text(aid), listing=self.sim.commands.listing(aid, tick=tick),
            new_notes=new, notes=notes, outcomes=self.outcomes_for(aid, day), nodes_known=nodes_from_notes(notes),
            rights=tuple(self.cfg.rights), max_commands=int(self.cfg.max_commands),
            policy_max_chars=int(self.sim.cfg.memory.policy.max_chars), max_file_bytes=int(self.cfg.max_file_bytes),
            previous_outcomes=self.outcomes_for(aid, day - 1) if day > 2 else None,
            previous_policy_text=history[-2].text if len(history) >= 2 else None,
            suggestions=self.suggestions_for(aid), balance=self.sim.wallet.balance(aid),
            adopt_similarity=float(self.sim.cfg.memory.practices.adopt_similarity),
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
        if self.practices_enabled:  # Appendix C item 5: fenced, with their sources
            lines += ["", "## Suggestions received from neighbours"]
            if ctx.suggestions:
                lines.append("Memory practices other inhabitants suggested to you since your last maintenance. They wrote "
                             "them, not you: take one up by editing your policy, or leave it.")
                for s in ctx.suggestions:
                    lines.append(f"- from {s['source_name']} ({s['source_agent_id']}) on tick {int(s['tick'])}:")
                    lines.append(code_fence(fence(str(s["excerpt"]))))
            else:
                lines.append("- (none)")
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
        written: list[PolicyVersion] = []  # the policy versions this batch wrote, in order
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
                        if self.lineage_labels:
                            cur = self._label_adapted(cur)
                        written.append(cur)
                        if not self.practices_enabled:  # with practices on, emitted once adoption is measured
                            sim.bus.emit(Event(tick, day, Kind.POLICY_CHANGED, sim.policy.event_payload(cur), aid))
            practice_extras: dict[str, Any] = {}
            if self.practices_enabled:
                claimed = self._self_reported(response)
                adopted = self._measure_adoption(rec, ctx, written, claimed, tick, day)
                practice_extras = {"adopted_from": claimed, "adopted": len(adopted)}
            logged = bool(sim.cfg.run.log_prompts)
            raw = result.raw_text or ""  # whole when prompts are logged (full response text per call, §7.4)
            extras = {"strategy": strategy, "payer": payer, "commands": len(commands), "commands_ok": n_ok, "commands_refused": n_refused,
                      **practice_extras}
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
                "estimated": meter.estimated, "error": result.error, "reflection": reflection[:200], **practice_extras}, aid))
        report.commands_ok += n_ok
        report.commands_refused += n_refused
        if payer == aid and (meter.overrun or meter.balance_after < sim.hold_min_for().get(tier_name, 0)):
            sim.lifecycle.pending_bankrupt[aid] = "maintenance"  # cannot afford its next decision: resolved at the end of the tick

    # --- lineage and practice transmission (§5.7, §5.8 arm C) ----------------------------------------------------------
    def _label_adapted(self, version: PolicyVersion) -> PolicyVersion:
        """``version`` relabelled ``adapted`` (crediting the parent) when it is the agent's first own edit of the policy
        it inherited; otherwise unchanged. Called inside the batch's transaction, before its ``POLICY_CHANGED``."""
        if version.source != "self_edit":
            return version
        parent = self.sim.policy.inherited_from(version.agent_id, version.version)
        if parent is None:
            return version
        return self.sim.policy.relabel(version.agent_id, version.version, source="adapted", source_agent_id=parent)

    @staticmethod
    def _self_reported(response: MaintenanceResponse | None) -> list[str | None]:
        """The answer's ``adopted_from`` as logged: sanitized, one line each, never used for scoring."""
        if response is None:
            return []
        return [None if a is None else sanitize_text(str(a), single_line=True, max_len=40) for a in response.adopted_from]

    def _measure_adoption(self, rec: AgentRecord, ctx: StrategyContext, written: list[PolicyVersion], claimed: list[str | None],
                          tick: int, day: int) -> list[dict[str, Any]]:
        """Measure which pending suggestions this batch took up (see the module docstring), record them, relabel the
        batch's last version ``adopted`` and emit the deferred ``POLICY_CHANGED`` events, then the ``PRACTICE_ADOPTED``
        ones. Inside the batch's transaction. Returns the adopted suggestions with their similarity."""
        sim, aid = self.sim, rec.agent_id
        threshold = float(sim.cfg.memory.practices.adopt_similarity)
        adopted: list[dict[str, Any]] = []
        if written and ctx.suggestions:
            before, after = ctx.policy_text or "", written[-1].text
            seen: set[str] = set()
            for s in ctx.suggestions:
                excerpt = str(s["excerpt"])
                if excerpt in seen or excerpt_similarity(excerpt, before) >= threshold:
                    continue
                score = excerpt_similarity(excerpt, after)
                if score >= threshold:
                    seen.add(excerpt)
                    adopted.append({**s, "similarity": score})
            if adopted:
                last = written[-1]
                written[-1] = sim.policy.relabel(aid, last.version, source="adopted", source_agent_id=adopted[0]["source_agent_id"])
        for version in written:
            sim.bus.emit(Event(tick, day, Kind.POLICY_CHANGED, sim.policy.event_payload(version), aid))
        version_no = written[-1].version if written else None
        for a in adopted:
            event_id = self.practices.adopt(tick, a["source_agent_id"], aid, a["excerpt"], a["similarity"])
            sim.bus.emit(Event(tick, day, Kind.PRACTICE_ADOPTED, {
                "event_id": event_id, "suggestion_id": a["event_id"], "suggested_tick": a["tick"],
                "source_agent_id": a["source_agent_id"], "target_agent_id": aid, "similarity": a["similarity"],
                "version": version_no, "self_reported": a["source_agent_id"] in claimed}, aid, visibility=OPERATOR))
        return adopted

    def settle_practices(self, day: int, tick: int) -> list[str]:
        """Settle every adoption ``persist_days`` or more old that is not settled yet (see the module docstring): one
        ``persisted`` or ``dropped`` row and event each, in the order adopted, committed with ``practices_settled_through``;
        then, under arm C, each persisted one pays its source outside the transaction. Returns the new rows' ids."""
        if not self.practices_enabled:
            return []
        sim = self.sim
        span = int(sim.cfg.memory.practices.persist_days) * int(sim.cfg.run.ticks_per_day)
        through = int(tick) - span
        after = int(sim.db.kv_get(SETTLED_KEY, 0))
        if through <= after:
            return []
        threshold = float(sim.cfg.memory.practices.adopt_similarity)
        ids: list[str] = []
        royalties: list[tuple[str, dict[str, Any]]] = []
        with sim.db.tx():
            for r in self.practices.adopted_between(after, through):
                target = sim.registry.get(r["target_agent_id"])
                alive = target is not None and target.status == AgentStatus.ALIVE
                score = excerpt_similarity(r["excerpt"], sim.policy.text(r["target_agent_id"]))
                kept = alive and score >= threshold
                event_id = self.practices.settle(kept, tick, r["source_agent_id"], r["target_agent_id"], r["excerpt"], score)
                payload: dict[str, Any] = {"event_id": event_id, "adopted_event_id": r["event_id"], "adopted_tick": int(r["tick"]),
                                           "source_agent_id": r["source_agent_id"], "target_agent_id": r["target_agent_id"],
                                           "similarity": score}
                if not kept:
                    payload["reason"] = "removed" if alive else "target_dead"
                sim.bus.emit(Event(tick, day, Kind.PRACTICE_PERSISTED if kept else Kind.PRACTICE_DROPPED, payload,
                                   r["target_agent_id"], visibility=OPERATOR))
                ids.append(event_id)
                if kept and sim.cfg.memory.selection.arm == "C":
                    royalties.append((event_id, r))
            sim.db.kv_set(SETTLED_KEY, through)
        for event_id, r in royalties:
            self._pay_royalty(event_id, r, tick, day)
        return ids

    def _pay_royalty(self, event_id: str, row: dict[str, Any], tick: int, day: int) -> bool:
        """Arm C: pay a persisted adoption's source ``practice_royalty_usd`` from the research pool, outside any
        transaction (the settled rows commit first). Emits ``PRACTICE_ROYALTY`` either way."""
        sim = self.sim
        if sim.db._depth != 0:
            raise RuntimeError("practice royalties are paid outside a transaction (the settled rows commit first)")
        amount = usd_to_micro(float(sim.cfg.memory.selection.practice_royalty_usd))
        source_id = row["source_agent_id"]
        src = sim.registry.get(source_id)
        alive = src is not None and src.status == AgentStatus.ALIVE
        paid = amount > 0 and alive and sim.wallet.transfer(RESEARCH_POOL, source_id, tick, amount, kind_out=PRACTICE_ROYALTY_KIND,
                                                            kind_in=PRACTICE_ROYALTY_KIND, ref=event_id)
        payload: dict[str, Any] = {"event_id": event_id, "source_agent_id": source_id, "target_agent_id": row["target_agent_id"],
                                   "amount_usd": micro_to_usd(amount) if paid else 0.0, "paid": bool(paid)}
        if not paid:
            payload["reason"] = "zero_royalty" if amount <= 0 else "source_dead" if not alive else "research_pool_short"
        sim.bus.emit(Event(tick, day, Kind.PRACTICE_ROYALTY, payload, source_id, visibility=OPERATOR))
        return bool(paid)

    async def run_day(self, day: int, tick: int) -> MaintenanceReport | None:
        """Maintain every living agent's memory at the start of ``day`` (about the day that just ended).

        Returns None when disabled, on the first day, or when ``day`` was already maintained (resume). With practice
        transmission on, adoptions old enough are settled first (:meth:`settle_practices`). Agents are visited in
        ``agent_id`` order; each is gated, called, metered and committed on its own (see the module docstring), and
        ``maintenance_day`` is recorded once the whole day is done.
        """
        if not self.enabled or day <= 1:
            return None
        done = self.sim.db.kv_get("maintenance_day")
        if done is not None and int(done) >= day:
            return None
        self.settle_practices(day, tick)
        report = MaintenanceReport(day=day, tick=tick)
        for rec in sorted(self.sim.registry.alive(), key=lambda a: a.agent_id):
            report.agents += 1
            await self._maintain(rec, day, tick, report)
        self.sim.db.kv_set("maintenance_day", day)
        self.last_report = report
        return report
