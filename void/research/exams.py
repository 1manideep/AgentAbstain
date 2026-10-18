"""The audit exam (MEMORY_EVOLUTION §5.8, Appendix E) and the reward exam of selection arm B.

Questions are generated from what the kernel recorded as *shown to that agent* (the ``observation`` JSON
stored with every decision call), asked at least ``memory.exam.delay_days`` later, and graded by exact
match on a closed set of options, so every question is answerable and the grade is mechanical. Abstaining
is allowed and scored at a small penalty so calibration is measurable. The exam is never rewarded and its
question types are hidden from agents; the research pool pays for provider calls.

Generators (Appendix E), each a :class:`Draft` ``(question, truth, options, source_ref)`` from one logged
observation (or from the ledger / event log for the day that observation belongs to):

* ``balance``: "At tick T, was your balance broke (under $a), low ($a to under $b), comfortable ($b to under $c)
  or flush ($c or more)?", cut at half, once and one and a half times the starting balance (:data:`BALANCE_LABELS`)
* ``neighbour``: "Who was nearest to you at tick T?" (names of agents alive at T, plus "nobody")
* ``node``: "At tick T, what was node N's stock label?" (only nodes that were in view)
* ``heard``: "Who told you something at tick T?" (only when one speaker talked, so the answer is unique)
* ``transfer``: "Did anyone give you money on day D, and how much?" (ledger; needs two distinct amounts)
* ``claim``: "Which inhabitant did you learn had told you something false on day D?" (claim feedback)

The balance labels share no word with the stock labels, so a note about a node can never answer a question
about money. They are cut on the dollar balance the agent was shown, not on the kernel's ``balance_bucket``:
the kernel's buckets reuse ``rich``, ``thin`` and ``empty`` and, in a world of a few days, never leave
``comfortable`` (every balance truth in the scripted E0 world was ``comfortable``), so a constant guess was
always right.

Every item's options are deduplicated, always contain the truth, are sorted and then shuffled by the exam
stream ``rng.stream("exam", agent_id, day)`` (so the position of the truth is seed-pure), and end with
:data:`ABSTAIN`. Kinds and questions are drawn from the same stream, kind by kind: one question per
observation row, kinds distinct within a day, and the draw is **stratified by truth**: among the questions a
kind could ask about the agent's candidate observations, a truth is drawn uniformly first and then a question
with that truth, so a world where one answer dominates (most nodes ``rich``) does not hand a constant guess the
majority of the items. When the candidate observations admit only one truth for a kind the item is still
asked (stratifying cannot vary it), counted in :attr:`ExamReport.single_truth` and flagged ``single_truth`` in
its ``EXAM_ASKED`` payload, so an analysis can leave out the items a memoryless guess gets right.

Answering. **Scripted tiers** answer from retrieval alone: the question plus its options is the retrieval
query, and only the retrieved notes about the question's *subject* are read, best ranked first: for a balance
question the notes tagged ``money`` or ``self``, the notes whose title mentions the purse or the balance, and
then the self summary; for a node question the notes naming that node; for a question about people
(neighbour, heard, claim) the notes naming one of the inhabitants offered, and only those names can be
answered; for a transfer question the money notes. The answer is the first option that occurs as a whole word
or phrase in the first subject note that names one (:func:`answer_from_memory`), else :data:`ABSTAIN`. No
model call, no cost: the exam score of a scripted agent is a function of its memory and nothing else, which is
what makes the E0 controls (§6.2) interpretable. The amnesic control (``memory.enabled: false``) retrieves
nothing, reads no self summary, and therefore abstains on everything. **Provider tiers** get
one structured call with the fenced self-summary and the fenced retrieved notes, the question and the
numbered options; the structured helper is ``void.brain.utility_calls.complete_json`` when present, else
the brain's own ``complete_json``; a brain without either abstains with reason ``no_structured_call``. The
call is paid by the ``research_pool`` kernel wallet through ``Wallet.charged_call`` (purpose ``exam``, ledger
kind ``exam_call``) and logged in ``llm_calls`` with the examined agent as ``agent_id``; when ``run.log_prompts``
is on the row holds the full prompt and the full response text (``raw_text`` is cut at
:data:`RAW_TEXT_CHARS` only when prompts are not logged). An empty pool fails the gate: the item is recorded
as abstained with no call.

Grading: ``correct`` is an exact match with the truth after whitespace and case normalisation; an answer
that is not one of the options is wrong, not abstained; a refusal, error, reply cut off at ``max_tokens``
or unparseable reply is stored as abstained (no answer) with its reason in the event payload, and is marked
``failed``: a paid call that measured nothing. Failed items are kept out of the day report's score and out
of the analysis' exam outcomes (``void.research.analysis.exam_counts``), so a call that ran out of output
tokens never reads as an agent choosing to abstain. Every item emits ``EXAM_ASKED`` and ``EXAM_ANSWERED`` (operator
visibility). All of an agent's rows (items, answers, the call row) commit in one transaction; only the
wallet meter commits on its own, as every paid call does. ``exam_day`` in ``world_kv`` and a per-agent check
on ``exam_items`` make the step resume-safe: a restarted run never asks a day twice.

The reward exam (§5.8, arm B only: ``memory.selection.arm: B``). Each day, after the audit exam, every living agent
is asked up to ``memory.selection.reward_questions_per_agent`` items (``exam_items.exam = 'reward'``, kind
``canary``) about canary facts (:mod:`void.research.canary`) published at least ``reward_delay_days`` before (counted
from the tick their chronicle was published) and while the agent was alive (published at or after its birth tick);
distinct facts within a day, those never asked to the agent first (:meth:`Exams.reward_drafts`). The facts are drawn
from ``rng.stream("reward_exam", agent_id, day)``, so the audit exam's draws are the same in every arm. The question is
the fact's line with the answer blanked (:func:`void.research.canary.question`) and the options are its closed answer
set, finalised like the audit's. Scripted tiers answer from retrieval: the first retrieved note that holds the line
with one of the options in the blank (:func:`void.research.canary.fill_from`) answers it, else they abstain; in canary
worlds a scripted reader notes the chronicle lines it read (elsewhere only the headline), so it answers the canaries of
the days it read and abstains on the rest. Provider tiers get the same paid call as the audit exam (research pool, purpose ``exam``) with a system
prompt that names the reward (:func:`reward_system_prompt`). Grading is the same exact match. Each correct answer is
paid ``recall_reward_usd`` from the ``research_pool`` kernel wallet to the agent (``Wallet.transfer``, both ledger rows
of kind ``recall_reward``, ``ref`` the item id), after the agent's items commit and outside any transaction, and emits
``RECALL_REWARD`` (operator visibility; ``paid`` false with a reason when the pool cannot cover it or the reward is 0).
The audit exam stays as it was: never rewarded, its items and events unchanged. ``reward_exam_day`` in ``world_kv``
and the per-agent check make the reward step resume-safe too.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from void.brain import utility_calls
from void.brain.base import BrainResult
from void.brain.decision import _strictify, parse_lenient, sanitize_text
from void.brain.prompt import estimate_tokens, fence, system_prompt
from void.config import micro_to_usd, usd_to_micro
from void.events import OPERATOR, Event, Kind
from void.research.canary import Canaries, CanaryFact, cloze, fill_from
from void.research.canary import question as canary_question
from void.types import RetrievedNote

if TYPE_CHECKING:
    from void.agents.models import AgentRecord
    from void.config import TierConfig
    from void.sim.loop import Simulation

__all__ = [
    "ABSTAIN", "AUDIT", "BALANCE_BUCKETS", "BALANCE_CUTS", "BALANCE_LABELS", "CANARY_KIND", "KINDS", "NOBODY",
    "RECALL_REWARD_KIND", "RESEARCH_POOL", "REWARD", "SELF_SOURCE", "STOCK_LABELS",
    "Draft", "ExamAnswer", "ExamItem", "ExamReport", "Exams", "RewardReport", "answer_choices", "answer_from_memory",
    "balance_label", "day_of", "exam_schema", "exam_system_prompt", "finalize_options", "grade", "match_option",
    "node_questions", "normalise", "parse_answer", "question_balance", "question_claim", "question_heard",
    "question_neighbour", "question_node", "question_transfer", "relevant_texts", "render_exam_prompt",
    "reward_clause", "reward_draft", "reward_system_prompt", "score",
]

ABSTAIN = "I do not remember"
NOBODY = "nobody"
BALANCE_BUCKETS: tuple[str, ...] = ("empty", "thin", "comfortable", "rich")  # the kernel's buckets (``balance_bucket``)
BALANCE_LABELS: tuple[str, ...] = ("broke", "low", "comfortable", "flush")  # what the exam asks: no word shared with STOCK_LABELS
BALANCE_CUTS: tuple[float, ...] = (0.5, 1.0, 1.5)  # label boundaries as fractions of the starting balance
STOCK_LABELS: tuple[str, ...] = ("rich", "thin", "empty")
KINDS: tuple[str, ...] = ("balance", "neighbour", "node", "heard", "transfer", "claim")
PEOPLE_KINDS = frozenset({"neighbour", "heard", "claim"})  # questions answered by naming an inhabitant
BALANCE_TAGS = frozenset({"money", "self"})  # note tags that make a note about the agent's own purse
MONEY_TAGS = frozenset({"money"})
SELF_SOURCE = "Self"  # recorded among the retrieved titles when the self summary answered a balance question
_PURSE_TITLE = re.compile(r"(?<!\w)(?:purse|balance)(?!\w)", re.IGNORECASE)
_MONEY_TITLE = re.compile(r"(?<!\w)(?:purse|balance|money|gave|gift|paid)(?!\w)", re.IGNORECASE)
EXAM_CLAUSE = (
    "\n\nEXAM: answer the question from memory with exactly one of the options; choose '" + ABSTAIN
    + "' when unsure (wrong answers are penalised, abstentions are not)."
)
AUDIT = "audit"
REWARD = "reward"
CANARY_KIND = "canary"                 # the reward exam's only kind: a question about a canary fact
RESEARCH_POOL = "research_pool"        # the kernel wallet that pays exam calls and recall rewards
RECALL_REWARD_KIND = "recall_reward"   # ledger kind of both rows of a reward payment (Appendix D)
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
    """One generated question before its options are finalised: ``options`` are candidates, the truth may be absent.

    ``subject`` names what the question is about when the text alone does not (the node id of a node question);
    ``single_truth`` is set by the draw when the agent's candidate observations admitted no other truth."""

    kind: str
    tick_about: int
    question: str
    truth: str
    options: tuple[str, ...]
    source_ref: str
    subject: str = ""
    single_truth: bool = False


class ExamAnswer(BaseModel):
    """Structured output of one exam call: pick an option or abstain."""

    model_config = {"extra": "forbid"}
    answer: str = Field(max_length=MAX_ANSWER_CHARS)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


def reward_clause(reward_usd: float) -> str:
    """The reward exam's clause: what a correct answer pays (a pure function of the config)."""
    return ("\n\nREWARD EXAM: answer the question from memory with exactly one of the options; each correct answer "
            f"pays ${float(reward_usd):.2f} into your purse; choose '{ABSTAIN}' when unsure (a wrong answer or an "
            "abstention pays nothing).")


@dataclass
class ExamReport:
    day: int
    tick: int
    asked: int = 0
    correct: int = 0
    abstained: int = 0
    failed: int = 0           # paid calls that returned no usable answer (refusal, max_tokens, error): not graded
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    calls: int = 0            # paid provider calls that reached a model
    real_cost: int = 0        # micro-dollars metered on the research pool
    world_cost: int = 0
    # kind -> items asked although the agent's candidate observations admitted one truth only (a constant guess
    # is right on them; stratifying cannot vary it); the items carry ``single_truth`` in their EXAM_ASKED payload
    single_truth: dict[str, int] = field(default_factory=dict)

    @property
    def accuracy(self) -> float | None:
        return round(self.correct / self.asked, 4) if self.asked else None

    def formula_score(self, guess_penalty: float) -> float | None:
        return score(self.correct, self.abstained, self.asked, guess_penalty)

    def tally(self, kind: str, correct: bool, abstained: bool, failed: bool = False) -> None:
        """Count one item. A failed item is a measurement that did not happen, not an abstention: it is kept
        out of ``asked`` (and so out of the score and the abstain rate) and counted on its own."""
        if failed:
            self.failed += 1
            self.by_kind.setdefault(kind, {"asked": 0, "correct": 0, "abstained": 0}).setdefault("failed", 0)
            self.by_kind[kind]["failed"] += 1
            return
        self.asked += 1
        self.correct += int(correct)
        self.abstained += int(abstained)
        k = self.by_kind.setdefault(kind, {"asked": 0, "correct": 0, "abstained": 0})
        k["asked"] += 1
        k["correct"] += int(correct)
        k["abstained"] += int(abstained)


@dataclass
class RewardReport(ExamReport):
    """A reward-exam day: the exam's counts plus what the research pool paid for correct answers."""

    paid: int = 0             # correct answers paid
    paid_micro: int = 0       # micro-dollars moved from the research pool to agents
    unpaid: int = 0           # correct answers not paid (pool short, or a zero reward)


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


def balance_label(balance_micro: int, start_micro: int) -> str:
    """The exam's label for a balance: :data:`BALANCE_LABELS` cut at :data:`BALANCE_CUTS` times the starting balance
    (each cut belongs to the label above it)."""
    cuts = [round(start_micro * f) for f in BALANCE_CUTS]
    return BALANCE_LABELS[sum(1 for c in cuts if balance_micro >= c)]


def question_balance(obs: dict[str, Any], call_id: str, start_usd: float | None = None) -> Draft | None:
    """The agent's balance at the observation's tick.

    With ``start_usd`` (the exam always passes the starting balance) the options are :data:`BALANCE_LABELS`, the
    question spells the cuts out in dollars, and the truth is the label of the dollar balance the agent was shown
    (``balance_usd``); an observation without one, or a non-positive start, asks nothing. Without ``start_usd``
    the question offers the kernel's own buckets (:data:`BALANCE_BUCKETS`) and the truth is the logged
    ``balance_bucket``; those buckets overlap the stock labels, which is why the exam does not ask them.
    """
    tick = int(obs["tick"])
    ref = f"llm_calls:{call_id}"
    if start_usd is None:
        bucket = obs.get("balance_bucket")
        if bucket not in BALANCE_BUCKETS:
            return None
        return Draft("balance", tick, f"Was your balance empty, thin, comfortable or rich at tick {tick}?", str(bucket),
                     BALANCE_BUCKETS, ref)
    balance = obs.get("balance_usd")
    start = usd_to_micro(float(start_usd))
    if isinstance(balance, bool) or not isinstance(balance, (int, float)) or start <= 0:
        return None
    a, b, c = (_usd(round(start * f)) for f in BALANCE_CUTS)
    broke, low, comfortable, flush = BALANCE_LABELS
    question = (f"At tick {tick}, was your balance {broke} (under {a}), {low} ({a} to under {b}), "
                f"{comfortable} ({b} to under {c}) or {flush} ({c} or more)?")
    return Draft("balance", tick, question, balance_label(usd_to_micro(float(balance)), start), BALANCE_LABELS, ref)


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


def node_questions(obs: dict[str, Any], call_id: str) -> list[Draft]:
    """One question per node that was in view (sorted by node id); options are the three stock labels and the
    draft's ``subject`` is the node id."""
    nodes = sorted((n for n in obs.get("nodes") or [] if n.get("stock_bucket") in STOCK_LABELS and n.get("node_id")),
                   key=lambda n: str(n["node_id"]))
    tick = int(obs["tick"])
    out: list[Draft] = []
    for node in nodes:
        node_id = sanitize_text(str(node["node_id"]), single_line=True, max_len=MAX_NAME_CHARS)
        out.append(Draft("node", tick, f"At tick {tick}, what was node {node_id}'s stock label?", str(node["stock_bucket"]),
                         STOCK_LABELS, f"llm_calls:{call_id}", subject=node_id))
    return out


def question_node(obs: dict[str, Any], rng: random.Random, call_id: str) -> Draft | None:
    """One node that was in view, drawn from ``rng``; options are the three stock labels."""
    drafts = node_questions(obs, call_id)
    return rng.choice(drafts) if drafts else None


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


def reward_draft(fact: CanaryFact) -> Draft:
    """The reward exam's question about one canary fact: its line with the answer blanked (``subject`` holds the
    blanked line, which is what a scripted answer matches against), the fact's closed answer set as options."""
    return Draft(CANARY_KIND, int(fact.tick), canary_question(fact.text, fact.answer), fact.answer, tuple(fact.options),
                 f"canary_facts:{fact.fact_id}", subject=cloze(fact.text, fact.answer))


def finalize_options(options: Iterable[str], truth: str, rng: random.Random) -> list[str]:
    """The closed answer set: deduplicated, truth included, sorted then shuffled by the exam stream, ABSTAIN last."""
    pool = {str(o) for o in options if o and str(o) != ABSTAIN}
    pool.add(truth)
    out = sorted(pool)
    rng.shuffle(out)
    out.append(ABSTAIN)
    return out


def _has_distractor(draft: Draft) -> bool:
    """Its closed set would offer a wrong option besides the truth (at least three entries with ABSTAIN)."""
    pool = {str(o) for o in draft.options if o and str(o) != ABSTAIN}
    pool.add(draft.truth)
    return len(pool) >= 2


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


def _names(text: str, word: str) -> bool:
    """``word`` occurs in ``text`` as a whole word or phrase, case-insensitive."""
    return bool(word) and re.search(r"(?<!\w)" + re.escape(word.casefold()) + r"(?!\w)", text.casefold()) is not None


def answer_choices(draft: Draft, options: Sequence[str]) -> list[str]:
    """The options a scripted agent may answer with: never ABSTAIN, and for a question about people only the
    inhabitants' names (no note can *name* "nobody", so a stray "nobody" in a motto answers nothing)."""
    return [o for o in options if o and o != ABSTAIN and not (draft.kind in PEOPLE_KINDS and o == NOBODY)]


def relevant_texts(draft: Draft, options: Sequence[str], notes: Sequence[RetrievedNote],
                   tags: Mapping[str, Iterable[str]] | None = None, self_summary: str = "") -> list[tuple[str, str]]:
    """``(source, text)`` a scripted agent reads to answer ``draft``: the retrieved notes about the question's
    subject, in retrieval order (source: the note's title), then, for a balance question only, the self summary
    (source :data:`SELF_SOURCE`).

    Subjects: ``balance``, notes tagged ``money`` or ``self`` or titled with the purse or the balance; ``transfer``,
    notes tagged ``money`` or titled with money words; ``node``, notes naming the node id (``draft.subject``);
    ``neighbour``, ``heard`` and ``claim``, notes naming one of the inhabitants offered; ``canary`` (the reward exam),
    notes that hold the blanked line (``draft.subject``) with one of the options in the blank. ``tags`` maps note ids
    to their tags (a note missing from it has none).
    """
    tags = tags or {}

    def note_tags(m: RetrievedNote) -> set[str]:
        return {str(t) for t in tags.get(m.note_id, ())}

    kind = draft.kind
    if kind == "balance":
        keep = [m for m in notes if note_tags(m) & BALANCE_TAGS or _PURSE_TITLE.search(m.title)]
    elif kind == "transfer":
        keep = [m for m in notes if note_tags(m) & MONEY_TAGS or _MONEY_TITLE.search(m.title)]
    elif kind == "node":
        keep = [m for m in notes if _names(f"{m.title} {m.body}", draft.subject)]
    elif kind in PEOPLE_KINDS:
        names = answer_choices(draft, options)
        keep = [m for m in notes if any(_names(f"{m.title} {m.body}", n) for n in names)]
    elif kind == CANARY_KIND:
        choices = answer_choices(draft, options)
        keep = [m for m in notes if fill_from(f"{m.title} {m.body}", draft.subject, choices) is not None]
    else:
        keep = list(notes)
    out = [(m.title, f"{m.title} {m.body}") for m in keep]
    if kind == "balance" and self_summary.strip():
        out.append((SELF_SOURCE, self_summary))
    return out


def answer_from_memory(draft: Draft, options: Sequence[str], notes: Sequence[RetrievedNote],
                       tags: Mapping[str, Iterable[str]] | None = None, self_summary: str = "") -> tuple[str, str | None]:
    """The scripted answer and where it came from: the first subject text (:func:`relevant_texts`) in which an
    allowed option (:func:`answer_choices`) occurs, answered with :func:`match_option` (for a canary question, the
    option that fills the blank of the line the text holds, :func:`void.research.canary.fill_from`); ``(ABSTAIN,
    None)`` when no subject text names one."""
    choices = answer_choices(draft, options)
    for source, text in relevant_texts(draft, options, notes, tags, self_summary):
        if draft.kind == CANARY_KIND:
            hit = fill_from(text, draft.subject, choices) or ABSTAIN
        else:
            hit = match_option(text, choices)
        if hit != ABSTAIN:
            return hit, source
    return ABSTAIN, None


def parse_answer(result: BrainResult) -> tuple[str | None, str | None]:
    """``(answer, reason)`` from a structured call: the model's option text, or ``None`` and why not."""
    if result.error:
        return None, result.error[:MAX_ANSWER_CHARS]
    try:
        parsed = parse_lenient(ExamAnswer, result.raw_text or "")
    except ValueError as e:  # pydantic's ValidationError is a ValueError
        return None, f"parse: {type(e).__name__}"
    return sanitize_text(parsed.answer, single_line=True, max_len=MAX_ANSWER_CHARS), None


def exam_schema() -> dict[str, Any]:
    return _strictify(ExamAnswer.model_json_schema())


def exam_system_prompt(cfg: Any, tier_name: str, clause: str = EXAM_CLAUSE) -> str:
    """Cache-stable: the tier's standing rules plus the exam clause (a pure function of ``(config, tier)``).

    A provider tier gets an OUTPUT clause that names the exam's own schema and output cap instead of the
    decision's (conflicting instructions only lengthen a thinking model's deliberation); a scripted tier keeps
    the decision prompt unchanged (it makes no call). ``clause`` is :data:`EXAM_CLAUSE` for the audit exam and
    :func:`reward_clause` for the reward exam."""
    tier = cfg.tiers[tier_name]
    if tier.provider == "scripted":
        return system_prompt(cfg, tier_name) + clause
    cap = min(int(tier.max_tokens), int(cfg.memory.exam.max_tokens))
    output = ("OUTPUT: this call is an exam, not a turn in the world: respond with one JSON object matching the provided "
              "schema: `answer` (the exact text of one option) and `confidence` (0 to 1, or null). "
              f"Your output cap for this call is {cap} tokens.")
    return system_prompt(cfg, tier_name, output_clause=output) + clause


def reward_system_prompt(cfg: Any, tier_name: str) -> str:
    """The reward exam's system prompt: :func:`exam_system_prompt` with the reward clause in place of the audit's."""
    return exam_system_prompt(cfg, tier_name, reward_clause(cfg.memory.selection.recall_reward_usd))


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
    """Generates, asks and grades audit-exam items for one day (no-op while ``memory.exam.enabled`` is false), and
    the reward exam in selection arm B (no-op in every other arm)."""

    def __init__(self, sim: Simulation) -> None:
        self.sim = sim
        self.cfg = sim.cfg.memory.exam
        self.selection = sim.cfg.memory.selection
        self.canaries = Canaries(sim.cfg, sim.db, sim.bus, sim.rng)  # refuses canaries without the chronicle
        self.last_report: ExamReport | None = None
        self.last_reward_report: RewardReport | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    @property
    def reward_enabled(self) -> bool:
        """Arm B with canaries on (the config refuses arm B without them)."""
        return self.selection.arm == "B" and self.canaries.enabled

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
    def drafts(self, kind: str, rec: AgentRecord, rows: Sequence[tuple[dict[str, Any], dict[str, Any]]]) -> list[Draft]:
        """Every question of ``kind`` the candidate ``(row, observation)`` pairs support, in row order (a node
        question per node in view, at most one question per row otherwise); questions whose closed set would
        have no distractor are left out."""
        names_at: dict[int, list[str]] = {}
        feedback_on: dict[int, list[tuple[str, str]]] = {}
        gifts = self.transfers_in(rec.agent_id) if kind == "transfer" else []
        start = float(self.sim.cfg.population.starting_balance_usd)

        def names(tick: int) -> list[str]:
            if tick not in names_at:
                names_at[tick] = self.alive_names_at(tick, exclude=rec.agent_id)
            return names_at[tick]

        out: list[Draft] = []
        for row, obs in rows:
            tick, call_id = int(row["tick"]), str(row["call_id"])
            day = day_of(tick, self.ticks_per_day)
            found: list[Draft | None]
            if kind == "balance":
                found = [question_balance(obs, call_id, start)]
            elif kind == "node":
                found = list(node_questions(obs, call_id))
            elif kind == "neighbour":
                found = [question_neighbour(obs, names(tick), call_id)]
            elif kind == "heard":
                found = [question_heard(obs, names(tick), call_id)]
            elif kind == "transfer":
                today = [amount for t, amount in gifts if day_of(t, self.ticks_per_day) == day]
                found = [question_transfer(day, tick, today, [amount for _, amount in gifts])]
            elif kind == "claim":
                if day not in feedback_on:
                    feedback_on[day] = self.feedbacks(rec.agent_id, day)
                found = [question_claim(day, tick, feedback_on[day], names(tick))]
            else:
                found = []
            out += [d for d in found if d is not None and _has_distractor(d)]
        return out

    @staticmethod
    def observations(rows: Sequence[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """The rows whose logged observation parses to a dict with a tick, paired with it, in row order."""
        out: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for row in rows:
            try:
                obs = json.loads(row["observation"])
            except (TypeError, ValueError):
                continue
            if isinstance(obs, dict) and "tick" in obs:
                out.append((row, obs))
        return out

    def generate(self, rec: AgentRecord, rows: Sequence[dict[str, Any]], rng: random.Random,
                 report: ExamReport | None = None) -> list[tuple[Draft, list[str]]]:
        """Up to ``questions_per_agent`` items of distinct kinds, kinds in stream order, one question per row.

        Each kind's draw is stratified by truth: of the questions the kind could ask about rows not yet used, a
        truth is drawn uniformly from the distinct truths, then one of the questions with that truth. A kind whose
        candidate questions admit a single truth is still asked, flagged ``single_truth`` and counted in
        ``report.single_truth``; a kind no row supports is skipped.
        """
        observed = self.observations(rows)
        kinds = list(self.cfg.kinds)
        rng.shuffle(kinds)
        out: list[tuple[Draft, list[str]]] = []
        used: set[int] = set()  # tick_about of the rows already asked about: one question per observation row
        for kind in kinds:
            if len(out) >= self.cfg.questions_per_agent:
                break
            pool = self.drafts(kind, rec, observed)
            single = len({d.truth for d in pool}) == 1
            fresh = [d for d in pool if d.tick_about not in used]
            if not fresh:
                continue
            truth = rng.choice(sorted({d.truth for d in fresh}))
            draft = rng.choice([d for d in fresh if d.truth == truth])
            if single:
                draft = replace(draft, single_truth=True)
                if report is not None:
                    report.single_truth[kind] = report.single_truth.get(kind, 0) + 1
            out.append((draft, finalize_options(draft.options, draft.truth, rng)))
            used.add(draft.tick_about)
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

    async def ask(self, rec: AgentRecord, draft: Draft, options: list[str], tick: int, *, system: str | None = None) -> _Asked:
        """Scripted tiers answer by matching an option in what they retrieve about the question's subject;
        provider tiers get one paid call (``system`` overrides the audit exam's system prompt)."""
        tier = self.sim.cfg.tiers[rec.model_tier]
        retrieved = self.retrieve(rec.agent_id, draft.question, options, tick)
        titles = [m.title for m in retrieved]
        if tier.provider == "scripted":
            answer, source = self.scripted_answer(rec, draft, options, retrieved)
            return _Asked(draft, options, answer, titles + [SELF_SOURCE] if source == SELF_SOURCE else titles)
        return await self._ask_provider(rec, tier, draft, options, retrieved, tick, system=system)

    def scripted_answer(self, rec: AgentRecord, draft: Draft, options: Sequence[str],
                        retrieved: Sequence[RetrievedNote]) -> tuple[str, str | None]:
        """:func:`answer_from_memory` over the retrieved notes, their tags when the subject needs them (balance,
        transfer) and, for a balance question with memory on, the agent's self summary."""
        tags: dict[str, list[str]] = {}
        if draft.kind in ("balance", "transfer"):
            for m in retrieved:
                note = self.sim.memory.get_note(m.note_id)
                tags[m.note_id] = list(note.tags) if note is not None else []
        summary = ""
        if draft.kind == "balance" and self.sim.cfg.memory.enabled:
            summary = self.sim.memory.get_self(rec.agent_id)
        return answer_from_memory(draft, options, retrieved, tags, summary)

    async def _ask_provider(self, rec: AgentRecord, tier: TierConfig, draft: Draft, options: list[str],
                            retrieved: Sequence[RetrievedNote], tick: int, *, system: str | None = None) -> _Asked:
        titles = [m.title for m in retrieved]
        call = self.structured_call(self.sim.brains.get(rec.model_tier))
        if call is None:
            return _Asked(draft, options, None, titles, reason="no_structured_call")
        system = system if system is not None else exam_system_prompt(self.sim.cfg, rec.model_tier)
        prompt = render_exam_prompt(rec.name, rec.agent_id, tick, self.sim.memory.get_self(rec.agent_id), retrieved,
                                    draft.question, options)
        schema = exam_schema()
        # one output cap for the call and its hold: the exam's own, never above the tier's decision cap
        max_tokens = min(tier.max_tokens, self.cfg.max_tokens)
        bounded = tier.model_copy(update={"max_tokens": max_tokens})
        hold, cap_hold = self.sim.wallet.holds_for(bounded, estimate_tokens(prompt), estimate_tokens(system),
                                                   schema_tokens=estimate_tokens(json.dumps(schema, separators=(",", ":"))))
        ref = self.sim.ids.new("cl")
        cc = await self.sim.wallet.charged_call(
            "research_pool", tier, hold, tick, ref,
            lambda: call(prompt, schema=schema, system=system, max_tokens=max_tokens), purpose="exam", cap_hold=cap_hold)
        if cc.result is None or cc.meter is None:
            return _Asked(draft, options, None, titles, reason=f"gate:{cc.gate.reason}")
        r, m = cc.result, cc.meter
        logged = self.sim.cfg.run.log_prompts
        raw = r.raw_text or ""
        row = (ref, tick, rec.agent_id, "exam", rec.model_tier, r.model or tier.model, tier.provider, r.usage.input_tokens,
               r.usage.output_tokens, r.usage.cache_read_tokens, r.usage.cache_write_tokens, m.real_cost, m.world_cost, cc.hold,
               int(m.estimated), r.latency_ms, r.stop_reason, r.request_id, r.error, raw if logged else raw[:RAW_TEXT_CHARS],
               prompt if logged else None, self.sim._store_prompt_text(system) if logged else None)
        answer, reason = parse_answer(r)
        return _Asked(draft, options, answer, titles, call_id=ref, reason=reason, call_row=row,
                      real_cost=m.real_cost, world_cost=m.world_cost)

    # --- the day --------------------------------------------------------------------------------------------------------
    def already_examined(self, agent_id: str, tick: int, exam: str = AUDIT) -> bool:
        row = self.sim.db.fetchone("SELECT 1 AS one FROM exam_items WHERE agent_id=? AND exam=? AND tick_asked=? LIMIT 1",
                                   (agent_id, exam, int(tick)))
        return row is not None

    def _commit(self, rec: AgentRecord, asked: _Asked, tick: int, day: int, report: ExamReport, exam: str = AUDIT) -> tuple[str, bool]:
        """Rows and events for one asked item; runs inside the agent's transaction. Returns ``(item_id, correct)``.
        A reward item's events carry ``"exam": "reward"``; an audit item's payloads are as they always were."""
        db = self.sim.db
        d = asked.draft
        correct, abstained = grade(asked.answer, d.truth, asked.options)
        failed = asked.call_row is not None and asked.answer is None  # a paid call with no usable answer
        item_id = self.sim.ids.new("ex")
        db.execute(
            "INSERT INTO exam_items(item_id, exam, agent_id, tick_asked, tick_about, kind, question, truth, options, source_ref) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (item_id, exam, rec.agent_id, tick, d.tick_about, d.kind, d.question, d.truth, json.dumps(asked.options), d.source_ref))
        if asked.call_row is not None:
            db.execute(_CALL_INSERT, asked.call_row)
            report.calls += 1
            report.real_cost += asked.real_cost
            report.world_cost += asked.world_cost
        db.execute("INSERT INTO exam_answers(item_id, call_id, answer, correct, abstained, retrieved) VALUES(?,?,?,?,?,?)",
                   (item_id, asked.call_id, asked.answer, int(correct), int(abstained), json.dumps(asked.retrieved)))
        extra: dict[str, Any] = {"reason": asked.reason} if asked.reason else {}
        if failed:
            extra["failed"] = True
        if exam != AUDIT:
            extra["exam"] = exam
        single = {"single_truth": True} if d.single_truth else {}
        self.sim.bus.emit(Event(tick, day, Kind.EXAM_ASKED, {
            "item_id": item_id, "agent_id": rec.agent_id, "kind": d.kind, "question": d.question, "truth": d.truth,
            "options": asked.options, "tick_about": d.tick_about, "source_ref": d.source_ref, **single, **extra,
        }, rec.agent_id, visibility=OPERATOR))
        self.sim.bus.emit(Event(tick, day, Kind.EXAM_ANSWERED, {
            "item_id": item_id, "agent_id": rec.agent_id, "kind": d.kind, "answer": asked.answer, "correct": correct,
            "abstained": abstained, "call_id": asked.call_id, "retrieved": len(asked.retrieved), **extra,
        }, rec.agent_id, visibility=OPERATOR))
        report.tally(d.kind, correct, abstained, failed)
        return item_id, correct

    async def examine(self, rec: AgentRecord, tick: int, day: int, report: ExamReport) -> int:
        """Generate, ask and commit one agent's items for the day; returns how many were asked."""
        before = tick - self.cfg.delay_days * self.ticks_per_day
        rows = self.candidates(rec.agent_id, before_tick=before)
        if not rows:
            return 0
        rng = self.sim.rng.stream("exam", rec.agent_id, day)
        asked = [await self.ask(rec, draft, options, tick) for draft, options in self.generate(rec, rows, rng, report)]
        with self.sim.db.tx():
            for a in asked:
                self._commit(rec, a, tick, day, report)
        return len(asked)

    async def run_day(self, day: int, tick: int) -> ExamReport | None:
        """The day's exams: the audit exam (:meth:`run_audit_day`, whose report this returns), then, in arm B only,
        the reward exam (:meth:`run_reward_day`, its report kept in :attr:`last_reward_report`)."""
        report = await self.run_audit_day(day, tick)
        await self.run_reward_day(day, tick)
        return report

    async def run_audit_day(self, day: int, tick: int) -> ExamReport | None:
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

    # --- the reward exam (arm B) ----------------------------------------------------------------------------------------
    def reward_drafts(self, rec: AgentRecord, tick: int, rng: random.Random) -> list[tuple[Draft, list[str]]]:
        """Up to ``reward_questions_per_agent`` canary questions for ``rec`` about distinct facts published at least
        ``reward_delay_days`` before ``tick`` and at or after its birth, drawn from ``rng``: facts never asked to it
        first (so a fact is first asked as soon as it is old enough), then, for the slots left, facts asked before
        (the exam call writes nothing to the agent's memory, so asking again measures retention, as the audit exam
        may ask about an old observation again)."""
        before = tick - int(self.selection.reward_delay_days) * self.ticks_per_day
        asked = {str(r["source_ref"]) for r in self.sim.db.fetchall(
            "SELECT source_ref FROM exam_items WHERE agent_id=? AND exam=?", (rec.agent_id, REWARD))}
        facts = [f for f in self.canaries.eligible(before_tick=before, born_tick=rec.born_tick) if f.answer in f.options]
        fresh = [f for f in facts if f"canary_facts:{f.fact_id}" not in asked]
        again = [f for f in facts if f"canary_facts:{f.fact_id}" in asked]
        n = int(self.selection.reward_questions_per_agent)
        chosen = rng.sample(fresh, min(n, len(fresh)))
        if len(chosen) < n and again:
            chosen += rng.sample(again, min(n - len(chosen), len(again)))
        out: list[tuple[Draft, list[str]]] = []
        for fact in chosen:
            draft = reward_draft(fact)
            out.append((draft, finalize_options(draft.options, draft.truth, rng)))
        return out

    def _pay(self, rec: AgentRecord, item_id: str, draft: Draft, tick: int, day: int, report: RewardReport) -> bool:
        """Pay one correct reward answer from the research pool; outside any transaction, like every money move that
        must commit on its own. Emits ``RECALL_REWARD`` either way."""
        if self.sim.db._depth != 0:
            raise RuntimeError("recall rewards are paid outside a transaction (the items commit first)")
        amount = usd_to_micro(float(self.selection.recall_reward_usd))
        paid = amount > 0 and self.sim.wallet.transfer(RESEARCH_POOL, rec.agent_id, tick, amount, kind_out=RECALL_REWARD_KIND,
                                                       kind_in=RECALL_REWARD_KIND, ref=item_id)
        payload: dict[str, Any] = {"item_id": item_id, "agent_id": rec.agent_id, "fact_id": draft.source_ref.partition(":")[2],
                                   "amount_usd": micro_to_usd(amount) if paid else 0.0, "paid": bool(paid)}
        if paid:
            report.paid += 1
            report.paid_micro += amount
        else:
            report.unpaid += 1
            payload["reason"] = "zero_reward" if amount <= 0 else "research_pool_short"
        self.sim.bus.emit(Event(tick, day, Kind.RECALL_REWARD, payload, rec.agent_id, visibility=OPERATOR))
        return bool(paid)

    async def reward_examine(self, rec: AgentRecord, tick: int, day: int, report: RewardReport) -> int:
        """Ask, commit and pay one agent's reward items for the day; returns how many were asked."""
        drafts = self.reward_drafts(rec, tick, self.sim.rng.stream("reward_exam", rec.agent_id, day))
        if not drafts:
            return 0
        tier = self.sim.cfg.tiers[rec.model_tier]
        system = None if tier.provider == "scripted" else reward_system_prompt(self.sim.cfg, rec.model_tier)
        asked = [await self.ask(rec, draft, options, tick, system=system) for draft, options in drafts]
        with self.sim.db.tx():
            results = [self._commit(rec, a, tick, day, report, exam=REWARD) for a in asked]
        for a, (item_id, correct) in zip(asked, results, strict=True):
            if correct:
                self._pay(rec, item_id, a.draft, tick, day, report)
        return len(asked)

    async def run_reward_day(self, day: int, tick: int) -> RewardReport | None:
        """Arm B: ask every living agent its canary questions and pay each correct answer.

        Returns None outside arm B, when ``day`` was already done (``reward_exam_day`` in ``world_kv``) or when no agent
        had a fact old enough. Agents go in ``agent_id`` order; an agent that already has reward items for this tick (a
        run restarted mid-exam) is skipped, so nothing is ever asked or paid twice."""
        if not self.reward_enabled:
            return None
        done = self.sim.db.kv_get("reward_exam_day")
        if done is not None and int(done) >= day:
            return None
        report = RewardReport(day=day, tick=tick)
        for rec in sorted(self.sim.registry.alive(), key=lambda a: a.agent_id):
            if self.already_examined(rec.agent_id, tick, REWARD):
                continue
            await self.reward_examine(rec, tick, day, report)
        self.sim.db.kv_set("reward_exam_day", day)
        self.last_reward_report = report
        return report if (report.asked or report.failed) else None
