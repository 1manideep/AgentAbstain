"""The audit exam (MEMORY_EVOLUTION §5.8, Appendix E) `[scaffold]`.

Questions are generated from what the kernel recorded as *shown to that agent* (the ``observation`` JSON
stored with every decision call), asked at least ``memory.exam.delay_days`` later, and graded by exact
match on a closed set of options, so every question is answerable and the grade is mechanical. Abstaining
is allowed and scored at a small penalty so calibration is measurable. The exam is never rewarded and its
question types are hidden from agents; the research pool pays for provider calls.

Generators (Appendix E), each ``(question, truth, options, source_ref)`` from one logged observation:

* ``balance``: "Was your balance empty, thin, comfortable or rich at tick T?"
* ``neighbour``: "Who was nearest to you at tick T?" (names of agents alive at T as options)
* ``node``: "At tick T, what was node N's stock label?" (only nodes that were in view)
* ``heard``: "Who told you about ... on day D?" (from the heard-message log)
* ``transfer``: "Did anyone give you money on day D, and how much?" (ledger)
* ``claim``: "Which of these agents made a false claim about node N?" (needs claim feedback on)

Answering: scripted tiers answer from retrieval alone (``store.retrieve(question)`` then a match of an
option in the retrieved text, else abstain), so the exam score of a scripted agent is a function of its
memory and nothing else; provider tiers get one structured call with the retrieved notes in the prompt.
Implementation is part of the thin slice; the generators above are the contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from void.sim.loop import Simulation

__all__ = ["ExamItem", "ExamAnswer", "ExamReport", "Exams", "score"]

ABSTAIN = "I do not remember"


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


class ExamAnswer(BaseModel):
    """Structured output of one exam call: pick an option or abstain."""

    model_config = {"extra": "forbid"}
    answer: str = Field(max_length=120)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


@dataclass
class ExamReport:
    day: int
    tick: int
    asked: int = 0
    correct: int = 0
    abstained: int = 0
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def accuracy(self) -> float | None:
        return round(self.correct / self.asked, 4) if self.asked else None


def score(correct: int, abstained: int, asked: int, guess_penalty: float) -> float | None:
    """Formula score per question: correct +1, abstained 0, wrong ``-guess_penalty``; mean over ``asked``.

    With penalty g an agent should answer only when its confidence exceeds g / (1 + g), so calibration shows
    up in the abstention pattern rather than being invisible in a plain accuracy.
    """
    if asked <= 0:
        return None
    wrong = asked - correct - abstained
    return round((correct - guess_penalty * wrong) / asked, 4)


class Exams:
    """Generates, asks and grades audit-exam items for one day. No-op while ``memory.exam.enabled`` is false."""

    def __init__(self, sim: Simulation) -> None:
        self.sim = sim
        self.cfg = sim.cfg.memory.exam

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def candidates(self, agent_id: str, *, before_tick: int) -> list[dict[str, Any]]:
        """Logged observations of ``agent_id`` old enough to be asked about (``tick <= before_tick``)."""
        rows = self.sim.db.fetchall(
            "SELECT call_id, tick, observation FROM llm_calls WHERE agent_id=? AND purpose='decide' AND observation IS NOT NULL "
            "AND tick<=? ORDER BY tick", (agent_id, int(before_tick)))
        return [dict(r) for r in rows]

    async def run_day(self, day: int, tick: int) -> ExamReport | None:
        """Ask every living agent ``questions_per_agent`` questions about days at least ``delay_days`` back.

        Returns None when disabled, when no agent has observations old enough, or when ``day`` was already
        examined (``exam_day`` in ``world_kv``). Thin-slice implementation pending.
        """
        if not self.enabled:
            return None
        done = self.sim.db.kv_get("exam_day")
        if done is not None and int(done) >= day:
            return None
        if day <= self.cfg.delay_days:
            return None
        raise NotImplementedError("Exams.run_day is part of the thin slice (MEMORY_EVOLUTION §10.4)")
