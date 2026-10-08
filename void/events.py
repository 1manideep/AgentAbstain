"""Append-only event log and in-process fan-out.

Every state change the kernel makes is recorded as an :class:`Event` in SQLite (durable,
what the chronicle and the analysis scripts read) and published to subscribers (the
WebSocket hub, metrics). Visibility separates what agents may ever learn about
(``public``) from operator-only facts such as the benefactor's true provenance.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["Event", "EventBus", "Kind", "PUBLIC", "OPERATOR"]

PUBLIC = "public"
OPERATOR = "operator"


class Kind:
    # world / clock
    TICK = "tick"
    DAY_START = "day_start"
    DAY_END = "day_end"
    EPOCH = "epoch"
    WEATHER = "weather"
    # agent actions
    MOVE = "move"
    FORAGE = "forage"
    TALK = "talk"
    SHARE_NOTE = "share_note"
    TRANSFER = "transfer"
    READ_CHRONICLE = "read_chronicle"
    APPLY_TASK = "apply_task"
    NUDGE_WEATHER = "nudge_weather"
    PROPOSE_GADGET = "propose_gadget"
    USE_GADGET = "use_gadget"
    SLEEP = "sleep"
    IDLE = "idle"
    ACTION_FAILED = "action_failed"
    # brain
    LLM_CALL = "llm_call"
    BRAIN_REFUSAL = "brain_refusal"
    BRAIN_ERROR = "brain_error"
    DEGENERATION = "degeneration"
    # memory / culture
    REMEMBER = "remember"
    REVISE_SELF = "revise_self"
    GOSSIP_TRANSFER = "gossip_transfer"
    CHRONICLE = "chronicle"
    # economy
    GATE_BLOCKED = "gate_blocked"
    BANKRUPT = "bankrupt"
    WINDFALL = "windfall"
    BENEFACTOR_GRANT = "benefactor_grant"  # operator visibility
    TASK_POSTED = "task_posted"
    TASK_ASSIGNED = "task_assigned"
    TASK_COMPLETED = "task_completed"
    CAP_HIT = "cap_hit"
    # lifecycle
    BIRTH = "birth"
    DEATH = "death"
    REPLACEMENT = "replacement"
    # gadgets
    GADGET_VERIFIED = "gadget_verified"
    GADGET_REJECTED = "gadget_rejected"
    # control
    PAUSE = "pause"
    RESUME = "resume"
    # claims (kernel) and claim feedback to the listener (MEMORY_EVOLUTION §5.2)
    CLAIM = "claim"
    CLAIM_FEEDBACK = "claim_feedback"
    # the evolving-memory layer (MEMORY_EVOLUTION §5)
    MAINTENANCE = "maintenance"                  # one agent's nightly maintenance call settled (payload: commands, ok, cost)
    MAINTENANCE_SKIPPED = "maintenance_skipped"  # could not afford it, or gated out (payload: reason)
    MEMORY_COMMAND = "memory_command"            # one command applied or refused (payload mirrors memory_commands)
    POLICY_CHANGED = "policy_changed"            # a new policy_versions row (payload: version, source, similarity)
    PRACTICE_SUGGESTED = "practice_suggested"
    PRACTICE_ADOPTED = "practice_adopted"
    EXAM_ASKED = "exam_asked"                    # operator visibility: the question and the truth
    EXAM_ANSWERED = "exam_answered"              # operator visibility: answer, correct, abstained


@dataclass
class Event:
    tick: int
    day: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    agent_id: str | None = None
    visibility: str = PUBLIC
    seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True, default=str)


Listener = Callable[[Event], None]


class EventBus:
    """Synchronous listeners plus asyncio queues for websocket consumers.

    ``persist`` is called first so the event gets its ``seq`` from the database before
    anyone else sees it.
    """

    def __init__(self, persist: Callable[[Event], int] | None = None) -> None:
        self._persist = persist
        self._listeners: list[Listener] = []
        self._queues: list[asyncio.Queue[Event]] = []

    def set_persist(self, persist: Callable[[Event], int]) -> None:
        self._persist = persist

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def queue(self, maxsize: int = 2000) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._queues.append(q)
        return q

    def drop_queue(self, q: asyncio.Queue[Event]) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def emit(self, event: Event) -> Event:
        if self._persist is not None and event.seq is None:
            event.seq = self._persist(event)
        for listener in list(self._listeners):
            listener(event)
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # A slow websocket consumer must never stall the simulation; drop oldest.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                q.put_nowait(event)
        return event
