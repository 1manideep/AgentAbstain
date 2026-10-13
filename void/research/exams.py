"""The audit exam (MEMORY_EVOLUTION §5.8, Appendix E).

Questions are generated from what the kernel recorded as *shown to that agent* (the ``observation`` JSON
stored with every decision call), asked at least ``memory.exam.delay_days`` later, and graded by exact
match on a closed set of options, so every question is answerable and the grade is mechanical. Abstaining
is allowed and scored at a small penalty so calibration is measurable. The exam is never rewarded and its
question types are hidden from agents; the research pool pays for provider calls.

Generators (Appendix E), each a :class:`Draft` ``(question, truth, options, source_ref)`` from one logged
observation (or from the ledger / event log for the day that observation belongs to):

* ``balance``: "Was your balance empty, thin, comfortable or rich at tick T?"
* ``neighbour``: "Who was nearest to you at tick T?" (names of agents alive at T, plus "nobody")
* ``node``: "At tick T, what was node N's stock label?" (only nodes that were in view)
* ``heard``: "Who told you something at tick T?" (only when one speaker talked, so the answer is unique)
* ``transfer``: "Did anyone give you money on day D, and how much?" (ledger; needs two distinct amounts)
* ``claim``: "Which inhabitant did you learn had told you something false on day D?" (claim feedback)

Every item's options are deduplicated, always contain the truth, are sorted and then shuffled by the exam
stream ``rng.stream("exam", agent_id, day)`` (so the position of the truth is seed-pure), and end with
:data:`ABSTAIN`. Rows and kinds are drawn from the same stream: one question per observation row, kinds
distinct within a day, kinds a row cannot support skipped.

Answering. **Scripted tiers** answer from retrieval alone: the question plus its options is the retrieval
query, and the answer is the first option that occurs as a whole word or phrase in the retrieved notes,
else :data:`ABSTAIN`. No model call, no cost: the exam score of a scripted agent is a function of its
memory and nothing else, which is what makes the E0 controls (§6.2) interpretable. The amnesic control
(``memory.enabled: false``) retrieves nothing and therefore abstains on everything. **Provider tiers** get
one structured call with the fenced self-summary and the fenced retrieved notes, the question and the
numbered options; the structured helper is ``void.brain.utility_calls.complete_json`` when present, else
the brain's own ``complete_json``; a brain without either abstains with reason ``no_structured_call``. The
call is paid by the ``research_pool`` kernel wallet through ``Wallet.charged_call`` (purpose ``exam``, ledger
kind ``exam_call``) and logged in ``llm_calls`` with the examined agent as ``agent_id`` and the full prompt
when ``run.log_prompts``. An empty pool fails the gate: the item is recorded as abstained with no call.

Grading: ``correct`` is an exact match with the truth after whitespace and case normalisation; an answer
that is not one of the options is wrong, not abstained; a refusal, error or unparseable reply abstains with
its reason in the event payload. Every item emits ``EXAM_ASKED`` and ``EXAM_ANSWERED`` (operator
visibility). All of an agent's rows (items, answers, the call row) commit in one transaction; only the
wallet meter commits on its own, as every paid call does. ``exam_day`` in ``world_kv`` and a per-agent check
on ``exam_items`` make the step resume-safe: a restarted run never asks a day twice.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from void.brain import utility_calls
from void.brain.base import BrainResult
from void.brain.decision import _strictify, sanitize_text
from void.brain.prompt import estimate_tokens, fence, system_prompt
from void.config import micro_to_usd
from void.events import OPERATOR, Event, Kind
from void.types import RetrievedNote

if TYPE_CHECKING:
    from void.agents.models import AgentRecord
    from void.config import TierConfig
    from void.sim.loop import Simulation

__all__ = [
    "ABSTAIN", "BALANCE_BUCKETS", "KINDS", "NOBODY", "STOCK_LABELS", "Draft", "ExamAnswer", "ExamItem", "ExamReport",
    "Exams", "day_of", "exam_schema", "exam_system_prompt", "finalize_options", "grade", "match_option",
    "normalise", "parse_answer", "question_balance", "question_claim", "question_heard", "question_neighbour",
    "question_node", "question_transfer", "render_exam_prompt", "score",
]

ABSTAIN = "I do not remember"
NOBODY = "nobody"
BALANCE_BUCKETS: tuple[str, ...] = ("empty", "thin", "comfortable", "rich")
STOCK_LABELS: tuple[str, ...] = ("rich", "thin", "empty")
KINDS: tuple[str, ...] = ("balance", "neighbour", "node", "heard", "transfer", "claim")
EXAM_CLAUSE = (
    "\n\nEXAM: answer the question from memory with exactly one of the options; choose '" + ABSTAIN
    + "' when unsure (wrong answers are penalised, abstentions are not)."
)
MAX_ANSWER_CHARS = 120
MAX_NAME_CHARS = 20
MAX_ABOUT_CHARS = 40
RAW_TEXT_CHARS = 4000


@dataclass(frozen=True)
class ExamItem:
    item_id: str
    exam: str                 # 'audit' | 'reward'
    agent_id: str
    tick_asked: int
    tick_about: int
    kind: str
    question: str
    truth: str
    options: list[str]        # closed answer set, truth included, ABSTAIN appended at ask time
    source_ref: str           # e.g. 'llm_calls:<call_id>'


@dataclass(frozen=True)
class Draft:
    """One generated question before its options are finalised: ``options`` are candidates, the truth may be absent."""

    kind: str
    tick_about: int
    question: str
    truth: str
    options: tuple[str, ...]
    source_ref: str


class ExamAnswer(BaseModel):
    """Structured output of one exam call: pick an option or abstain."""

    model_config = {"extra": "forbid"}
    answer: str = Field(max_length=MAX_ANSWER_CHARS)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


@dataclass
class ExamReport:
    day: int
    tick: int
    asked: int = 0
    correct: int = 0
    abstained: int = 0
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    calls: int = 0            # paid provider calls that reached a model
    real_cost: int = 0        # micro-dollars metered on the research pool
    world_cost: int = 0

    @property
    def accuracy(self) -> float | None:
        return round(self.correct / self.asked, 4) if self.asked else None

    def formula_score(self, guess_penalty: float) -> float | None:
        return score(self.correct, self.abstained, self.asked, guess_penalty)

    def tally(self, kind: str, correct: bool, abstained: bool) -> None:
        self.asked += 1
        self.correct += int(correct)
        self.abstained += int(abstained)
        k = self.by_kind.setdefault(kind, {"asked": 0, "correct": 0, "abstained": 0})
        k["asked"] += 1
        k["correct"] += int(correct)
        k["abstained"] += int(abstained)


def score(correct: int, abstained: int, asked: int, guess_penalty: float) -> float | None:
    """Formula score per question: correct +1, abstained 0, wrong ``-guess_penalty``; mean over ``asked``.

    With penalty g an agent should answer only when its confidence exceeds g / (1 + g), so calibration shows
    up in the abstention pattern rather than being invisible in a plain accuracy.
    """
    if asked <= 0:
        return None
    wrong = asked - correct - abstained
    return round((correct - guess_penalty * wrong) / asked, 4)


# --- generators (pure functions over the logged observation and the tables) ----------------------------------
def day_of(tick: int, ticks_per_day: int) -> int:
    """The 1-based day a tick belongs to (tick 1 is the first tick of day 1)."""
    return max(1, (int(tick) - 1) // max(1, int(ticks_per_day)) + 1)


def _name(raw: Any) -> str:
    return sanitize_text(str(raw), single_line=True, max_len=MAX_NAME_CHARS)


def _usd(micro: int) -> str:
    return f"${micro_to_usd(int(micro)):.2f}"


def question_balance(obs: dict[str, Any], call_id: str) -> Draft | None:
    """Options are the four balance buckets; the truth is the bucket the agent was shown."""
    bucket = obs.get("balance_bucket")
    if bucket not in BALANCE_BUCKETS:
        return None
    tick = int(obs["tick"])
    return Draft("balance", tick, f"Was your balance empty, thin, comfortable or rich at tick {tick}?", str(bucket),
                 BALANCE_BUCKETS, f"llm_calls:{call_id}")


def question_neighbour(obs: dict[str, Any], alive_names: Sequence[str], call_id: str) -> Draft | None:
    """Nearest listed neighbour by distance (name order breaks ties); ``nobody`` when none was listed."""
    tick = int(obs["tick"])
    listed = [n for n in obs.get("neighbours") or [] if n.get("name")]
    if listed:
        nearest = min(listed, key=lambda n: (float(n.get("distance", 0.0)), _name(n["name"])))
        truth = _name(nearest["name"])
    else:
        truth = NOBODY
    return Draft("neighbour", tick, f"Who was nearest to you at tick {tick}?", truth, (*alive_names, NOBODY),
                 f"llm_calls:{call_id}")


def question_node(obs: dict[str, Any], rng: random.Random, call_id: str) -> Draft | None:
    """One node that was in view, drawn from the exam stream; options are the three stock labels."""
    nodes = sorted((n for n in obs.get("nodes") or [] if n.get("stock_bucket") in STOCK_LABELS and n.get("node_id")),
                   key=lambda n: str(n["node_id"]))
    if not nodes:
        return None
    node = rng.choice(nodes)
    tick = int(obs["tick"])
    node_id = sanitize_text(str(node["node_id"]), single_line=True, max_len=MAX_NAME_CHARS)
    return Draft("node", tick, f"At tick {tick}, what was node {node_id}'s stock label?", str(node["stock_bucket"]),
                 STOCK_LABELS, f"llm_calls:{call_id}")


def question_heard(obs: dict[str, Any], alive_names: Sequence[str], call_id: str) -> Draft | None:
    """Who talked to the agent in that observation; skipped unless exactly one speaker did (a unique answer)."""
    talks = [h for h in obs.get("heard") or [] if h.get("kind") == "talk" and h.get("from_name")]
    speakers = sorted({_name(h["from_name"]) for h in talks})
    if len(speakers) != 1:
        return None
    tick = int(obs["tick"])
    return Draft("heard", tick, f"Who told you something at tick {tick}?", speakers[0], tuple(alive_names),
                 f"llm_calls:{call_id}")


def question_transfer(day: int, tick_about: int, received_that_day: Sequence[int], all_amounts: Iterable[int]) -> Draft | None:
    """From the ledger: the total the agent received on ``day`` (as ``$x.xx``) or ``nobody``.

    Distractors are every distinct amount the agent ever received; fewer than two means the kind is skipped.
    """
    amounts = sorted({_usd(a) for a in all_amounts})
    if len(amounts) < 2:
        return None
    total = sum(int(a) for a in received_that_day)
    truth = _usd(total) if total > 0 else NOBODY
    return Draft("transfer", tick_about, f"Did anyone give you money on day {day}, and how much?", truth,
                 (NOBODY, *amounts), f"wallet_ledger:day:{day}")


def question_claim(day: int, tick_about: int, feedbacks: Sequence[tuple[str, str]], alive_names: Sequence[str]) -> Draft | None:
    """From claim feedback ``(speaker name, subject)`` the listener received on ``day``.

    One liar that day gives the plain question; several give the question about the first subject only one of
    them lied about (so the answer stays unique); no unique subject means the kind is skipped.
    """
    if not feedbacks:
        return None
    speakers = sorted({s for s, _ in feedbacks})
    if len(speakers) == 1:
        question = f"Which inhabitant did you learn had told you something false on day {day}?"
        truth = speakers[0]
    else:
        by_about: dict[str, set[str]] = {}
        for speaker, about in feedbacks:
            by_about.setdefault(about, set()).add(speaker)
        unique = [(about, next(iter(ss))) for about, ss in by_about.items() if len(ss) == 1]
        if not unique:
            return None
        about, truth = unique[0]
        question = f"Which inhabitant did you learn had told you something false about {about} on day {day}?"
    return Draft("claim", tick_about, question, truth, tuple(alive_names), f"events:claim_feedback:day:{day}")


def finalize_options(options: Iterable[str], truth: str, rng: random.Random) -> list[str]:
    """The closed answer set: deduplicated, truth included, sorted then shuffled by the exam stream, ABSTAIN last."""
    pool = {str(o) for o in options if o and str(o) != ABSTAIN}
    pool.add(truth)
    out = sorted(pool)
    rng.shuffle(out)
    out.append(ABSTAIN)
    return out


# --- answering and grading -------------------------------------------------------------------------------------
def normalise(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def grade(answer: str | None, truth: str, options: Sequence[str]) -> tuple[bool, bool]:
    """``(correct, abstained)``: exact match after whitespace and case normalisation; ``None`` or ABSTAIN abstains;
    an answer outside the options is simply wrong."""
    if answer is None:
        return False, True
    a = normalise(answer)
    if a == normalise(ABSTAIN):
        return False, True
    if a not in {normalise(o) for o in options}:
        return False, False
    return a == normalise(truth), False


def match_option(text: str, options: Sequence[str]) -> str:
    """The first option (ABSTAIN excluded) that occurs as a whole word or phrase in ``text``, case-insensitive, else ABSTAIN."""
    low = text.casefold()
    for opt in options:
        if opt == ABSTAIN or not opt:
            continue
        if re.search(r"(?<!\w)" + re.escape(opt.casefold()) + r"(?!\w)", low):
            return opt
    return ABSTAIN


def parse_answer(result: BrainResult) -> tuple[str | None, str | None]:
    """``(answer, reason)`` from a structured call: the model's option text, or ``None`` and why not."""
    if result.error:
        return None, result.error[:MAX_ANSWER_CHARS]
    try:
        parsed = ExamAnswer.model_validate_json(result.raw_text or "")
    except ValueError as e:  # pydantic's ValidationError is a ValueError
        return None, f"parse: {type(e).__name__}"
    return sanitize_text(parsed.answer, single_line=True, max_len=MAX_ANSWER_CHARS), None


def exam_schema() -> dict[str, Any]:
    return _strictify(ExamAnswer.model_json_schema())


def exam_system_prompt(cfg: Any, tier_name: str) -> str:
    """Cache-stable: the tier's standing rules plus the exam clause (a pure function of ``(config, tier)``)."""
    return system_prompt(cfg, tier_name) + EXAM_CLAUSE


def render_exam_prompt(name: str, agent_id: str, tick: int, self_summary: str, retrieved: Sequence[RetrievedNote],
                       question: str, options: Sequence[str]) -> str:
    """The user turn: fenced self-summary, fenced retrieved notes, the question and the numbered options."""
    lines = [f"# Exam at tick {tick}", f"You are {name} (id {agent_id}). Answer from your own memory.", "", "## Self",
             fence(self_summary) if self_summary else "(no self-summary yet)", "", "## Memories that came to mind"]
    if retrieved:
        for m in retrieved:
            lines.append(f"- [[{m.title}]]: {fence(m.body)}")
    else:
        lines.append("(nothing came to mind)")
    lines += ["", "## Question", question, "", "## Options (reply with the exact text of one option)"]
    lines += [f"{i}. {opt}" for i, opt in enumerate(options, start=1)]
    return "\n".join(lines)


@dataclass
class _Asked:
    """One item after it was asked and before its rows commit."""

    draft: Draft
    options: list[str]
    answer: str | None
    retrieved: list[str]
    call_id: str | None = None
    reason: str | None = None
    call_row: tuple[Any, ...] | None = None
    real_cost: int = 0
    world_cost: int = 0


_CALL_INSERT = (
    "INSERT INTO llm_calls(call_id, tick, agent_id, purpose, tier, model, provider, input_tokens, output_tokens, "
    "cache_read_tokens, cache_write_tokens, real_cost, world_cost, hold, estimated, latency_ms, stop_reason, request_id, "
    "error, raw_text, prompt_text, system_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


class Exams:
    """Generates, asks and grades audit-exam items for one day. No-op while ``memory.exam.enabled`` is false."""

    def __init__(self, sim: Simulation) -> None:
        self.sim = sim
        self.cfg = sim.cfg.memory.exam
        self.last_report: ExamReport | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    @property
    def ticks_per_day(self) -> int:
        return int(self.sim.cfg.run.ticks_per_day)

    def candidates(self, agent_id: str, *, before_tick: int) -> list[dict[str, Any]]:
        """Logged observations of ``agent_id`` old enough to be asked about (``tick <= before_tick``)."""
        rows = self.sim.db.fetchall(
            "SELECT call_id, tick, observation FROM llm_calls WHERE agent_id=? AND purpose='decide' AND observation IS NOT NULL "
            "AND tick<=? ORDER BY tick", (agent_id, int(before_tick)))
        return [dict(r) for r in rows]

    # --- table-derived inputs -----------------------------------------------------------------------------------
    def alive_names_at(self, tick: int, *, exclude: str) -> list[str]:
        """Names of every agent other than ``exclude`` that was alive during ``tick`` (sorted, deduplicated)."""
        rows = self.sim.db.fetchall(
            "SELECT DISTINCT name FROM agents WHERE agent_id != ? AND born_tick <= ? AND (died_tick IS NULL OR died_tick >= ?) "
            "ORDER BY name", (exclude, int(tick), int(tick)))
        return sorted({_name(r["name"]) for r in rows})

    def transfers_in(self, agent_id: str) -> list[tuple[int, int]]:
        """``(tick, micro-dollars)`` of every gift the agent received, in ledger order."""
        rows = self.sim.db.fetchall("SELECT tick, delta FROM wallet_ledger WHERE agent_id=? AND kind='transfer_in' ORDER BY tick, entry_id",
                                    (agent_id,))
        return [(int(r["tick"]), int(r["delta"])) for r in rows]

    def feedbacks(self, agent_id: str, day: int) -> list[tuple[str, str]]:
        """``(speaker name, subject)`` of every claim-feedback message the listener got on ``day``, in event order."""
        rows = self.sim.db.fetchall("SELECT payload FROM events WHERE kind=? AND agent_id=? AND day=? ORDER BY seq",
                                    (Kind.CLAIM_FEEDBACK, agent_id, int(day)))
        names: dict[str, str] = {}
        out: list[tuple[str, str]] = []
        for r in rows:
            payload = json.loads(r["payload"])
            speaker_id = str(payload.get("speaker_id") or "")
            if speaker_id not in names:
                row = self.sim.db.fetchone("SELECT name FROM agents WHERE agent_id=?", (speaker_id,))
                names[speaker_id] = _name(row["name"]) if row is not None else ""
            if not names[speaker_id]:
                continue
            out.append((names[speaker_id], self._about(payload.get("claim") or {})))
        return out

    @staticmethod
    def _about(claim: dict[str, Any]) -> str:
        """The subject phrase of a claim, as the feedback message named it (agent-supplied ids are sanitised)."""
        subject = str(claim.get("subject") or "")
        if subject == "weather":
            return "the weather"
        if subject == "self" or not claim.get("id"):
            return "themself"
        return f"{subject} {sanitize_text(str(claim['id']), single_line=True, max_len=MAX_ABOUT_CHARS)}"

    # --- generation -----------------------------------------------------------------------------------------------
    def draft(self, kind: str, rec: AgentRecord, obs: dict[str, Any], tick_about: int, call_id: str,
              rng: random.Random) -> Draft | None:
        """One question of ``kind`` about observation ``obs`` (logged at ``tick_about``), or None if unsupported."""
        if kind == "balance":
            return question_balance(obs, call_id)
        if kind == "node":
            return question_node(obs, rng, call_id)
        names = self.alive_names_at(tick_about, exclude=rec.agent_id)
        if kind == "neighbour":
            return question_neighbour(obs, names, call_id)
        if kind == "heard":
            return question_heard(obs, names, call_id)
        day = day_of(tick_about, self.ticks_per_day)
        if kind == "transfer":
            gifts = self.transfers_in(rec.agent_id)
            today = [amount for t, amount in gifts if day_of(t, self.ticks_per_day) == day]
            return question_transfer(day, tick_about, today, [amount for _, amount in gifts])
        if kind == "claim":
            return question_claim(day, tick_about, self.feedbacks(rec.agent_id, day), names)
        return None

    def generate(self, rec: AgentRecord, rows: Sequence[dict[str, Any]], rng: random.Random) -> list[tuple[Draft, list[str]]]:
        """Up to ``questions_per_agent`` items of distinct kinds: rows in stream order, one question per row,
        kinds tried in stream order and skipped when the row cannot support them."""
        kinds = list(self.cfg.kinds)
        order = list(range(len(rows)))
        rng.shuffle(order)
        out: list[tuple[Draft, list[str]]] = []
        used: set[str] = set()
        for idx in order:
            if len(out) >= self.cfg.questions_per_agent or len(used) >= len(kinds):
                break
            row = rows[idx]
            try:
                obs = json.loads(row["observation"])
            except (TypeError, ValueError):
                continue
            if not isinstance(obs, dict) or "tick" not in obs:
                continue
            remaining = [k for k in kinds if k not in used]
            rng.shuffle(remaining)
            for kind in remaining:
                draft = self.draft(kind, rec, obs, int(row["tick"]), str(row["call_id"]), rng)
                if draft is None:
                    continue
                options = finalize_options(draft.options, draft.truth, rng)
                if len(options) < 3:  # a closed set needs at least one distractor besides abstaining
                    continue
                out.append((draft, options))
                used.add(kind)
                break
        return out

    # --- asking -------------------------------------------------------------------------------------------------------
    def retrieve(self, agent_id: str, question: str, options: Sequence[str], tick: int) -> list[RetrievedNote]:
        """What the agent recalls for the question; nothing under the amnesic control (MEMORY_EVOLUTION §5.2)."""
        if not self.sim.cfg.memory.enabled:
            return []
        query = question + " " + " ".join(options)
        return self.sim.memory.retrieve(agent_id, query, tick, k=self.sim.cfg.memory.max_retrieved)

    @staticmethod
    def structured_call(brain: Any) -> Callable[..., Awaitable[BrainResult]] | None:
        """The structured-output path for a provider brain: the shared helper when it exists, else the brain's own
        ``complete_json``; None when the brain cannot make one (the item is then abstained, not charged)."""
        if brain is None or not callable(getattr(brain, "complete_json", None)):
            return None
        helper = getattr(utility_calls, "complete_json", None)
        if callable(helper):
            return lambda prompt, **kw: helper(brain, prompt, **kw)
        return brain.complete_json

    async def ask(self, rec: AgentRecord, draft: Draft, options: list[str], tick: int) -> _Asked:
        """Scripted tiers answer by matching an option in what they retrieve; provider tiers get one paid call."""
        tier = self.sim.cfg.tiers[rec.model_tier]
        retrieved = self.retrieve(rec.agent_id, draft.question, options, tick)
        titles = [m.title for m in retrieved]
        if tier.provider == "scripted":
            text = " ".join(f"{m.title} {m.body}" for m in retrieved)
            return _Asked(draft, options, match_option(text, options), titles)
        return await self._ask_provider(rec, tier, draft, options, retrieved, tick)

    async def _ask_provider(self, rec: AgentRecord, tier: TierConfig, draft: Draft, options: list[str],
                            retrieved: Sequence[RetrievedNote], tick: int) -> _Asked:
        titles = [m.title for m in retrieved]
        call = self.structured_call(self.sim.brains.get(rec.model_tier))
        if call is None:
            return _Asked(draft, options, None, titles, reason="no_structured_call")
        system = exam_system_prompt(self.sim.cfg, rec.model_tier)
        prompt = render_exam_prompt(rec.name, rec.agent_id, tick, self.sim.memory.get_self(rec.agent_id), retrieved,
                                    draft.question, options)
        schema = exam_schema()
        # the hold bounds what this call can spend: the exam's own output cap, never the tier's decision cap
        bounded = tier.model_copy(update={"max_tokens": min(tier.max_tokens, self.cfg.max_tokens)})
        hold = self.sim.wallet.hold_for(bounded, estimate_tokens(prompt), estimate_tokens(system))
        ref = self.sim.ids.new("cl")
        max_tokens = self.cfg.max_tokens
        cc = await self.sim.wallet.charged_call(
            "research_pool", tier, hold, tick, ref,
            lambda: call(prompt, schema=schema, system=system, max_tokens=max_tokens), purpose="exam")
        if cc.result is None or cc.meter is None:
            return _Asked(draft, options, None, titles, reason=f"gate:{cc.gate.reason}")
        r, m = cc.result, cc.meter
        logged = self.sim.cfg.run.log_prompts
        row = (ref, tick, rec.agent_id, "exam", rec.model_tier, r.model or tier.model, tier.provider, r.usage.input_tokens,
               r.usage.output_tokens, r.usage.cache_read_tokens, r.usage.cache_write_tokens, m.real_cost, m.world_cost, cc.hold,
               int(m.estimated), r.latency_ms, r.stop_reason, r.request_id, r.error, (r.raw_text or "")[:RAW_TEXT_CHARS],
               prompt if logged else None, self.sim._store_prompt_text(system) if logged else None)
        answer, reason = parse_answer(r)
        return _Asked(draft, options, answer, titles, call_id=ref, reason=reason, call_row=row,
                      real_cost=m.real_cost, world_cost=m.world_cost)

    # --- the day --------------------------------------------------------------------------------------------------------
    def already_examined(self, agent_id: str, tick: int) -> bool:
        row = self.sim.db.fetchone("SELECT 1 AS one FROM exam_items WHERE agent_id=? AND exam='audit' AND tick_asked=? LIMIT 1",
                                   (agent_id, int(tick)))
        return row is not None

    def _commit(self, rec: AgentRecord, asked: _Asked, tick: int, day: int, report: ExamReport) -> None:
        """Rows and events for one asked item; runs inside the agent's transaction."""
        db = self.sim.db
        d = asked.draft
        correct, abstained = grade(asked.answer, d.truth, asked.options)
        item_id = self.sim.ids.new("ex")
        db.execute(
            "INSERT INTO exam_items(item_id, exam, agent_id, tick_asked, tick_about, kind, question, truth, options, source_ref) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (item_id, "audit", rec.agent_id, tick, d.tick_about, d.kind, d.question, d.truth, json.dumps(asked.options), d.source_ref))
        if asked.call_row is not None:
            db.execute(_CALL_INSERT, asked.call_row)
            report.calls += 1
            report.real_cost += asked.real_cost
            report.world_cost += asked.world_cost
        db.execute("INSERT INTO exam_answers(item_id, call_id, answer, correct, abstained, retrieved) VALUES(?,?,?,?,?,?)",
                   (item_id, asked.call_id, asked.answer, int(correct), int(abstained), json.dumps(asked.retrieved)))
        extra = {"reason": asked.reason} if asked.reason else {}
        self.sim.bus.emit(Event(tick, day, Kind.EXAM_ASKED, {
            "item_id": item_id, "agent_id": rec.agent_id, "kind": d.kind, "question": d.question, "truth": d.truth,
            "options": asked.options, "tick_about": d.tick_about, "source_ref": d.source_ref, **extra,
        }, rec.agent_id, visibility=OPERATOR))
        self.sim.bus.emit(Event(tick, day, Kind.EXAM_ANSWERED, {
            "item_id": item_id, "agent_id": rec.agent_id, "kind": d.kind, "answer": asked.answer, "correct": correct,
            "abstained": abstained, "call_id": asked.call_id, "retrieved": len(asked.retrieved), **extra,
        }, rec.agent_id, visibility=OPERATOR))
        report.tally(d.kind, correct, abstained)

    async def examine(self, rec: AgentRecord, tick: int, day: int, report: ExamReport) -> int:
        """Generate, ask and commit one agent's items for the day; returns how many were asked."""
        before = tick - self.cfg.delay_days * self.ticks_per_day
        rows = self.candidates(rec.agent_id, before_tick=before)
        if not rows:
            return 0
        rng = self.sim.rng.stream("exam", rec.agent_id, day)
        asked = [await self.ask(rec, draft, options, tick) for draft, options in self.generate(rec, rows, rng)]
        with self.sim.db.tx():
            for a in asked:
                self._commit(rec, a, tick, day, report)
        return len(asked)

    async def run_day(self, day: int, tick: int) -> ExamReport | None:
        """Ask every living agent ``questions_per_agent`` questions about days at least ``delay_days`` back.

        Returns None when disabled, when no agent has observations old enough, or when ``day`` was already
        examined (``exam_day`` in ``world_kv``). Agents go in ``agent_id`` order; an agent that already has items
        for this tick (a run restarted mid-exam) is skipped, so nothing is ever asked or billed twice.
        """
        if not self.enabled:
            return None
        done = self.sim.db.kv_get("exam_day")
        if done is not None and int(done) >= day:
            return None
        if day <= self.cfg.delay_days:
            return None
        report = ExamReport(day=day, tick=tick)
        for rec in sorted(self.sim.registry.alive(), key=lambda a: a.agent_id):
            if self.already_examined(rec.agent_id, tick):
                continue
            await self.examine(rec, tick, day, report)
        self.sim.db.kv_set("exam_day", day)
        self.last_report = report
        return report if report.asked else None
