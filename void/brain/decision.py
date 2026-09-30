"""The one structured output a brain returns per tick.

Flat action shape on purpose: a single object with optional fields keeps the JSON schema
small and strict, which is what the Messages API structured-output format wants and what
a small scripted policy can fill in without a parser. Semantic validation (does the target
exist, is it in range, can the agent afford it) is the kernel's job, not the schema's.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

__all__ = ["ActionType", "ACTION_TYPES", "MemoryOp", "Action", "Decision", "strict_json_schema"]

ActionType = Literal[
    "move", "forage", "talk", "share_note", "transfer", "read_chronicle", "apply_task",
    "nudge_weather", "propose_gadget", "use_gadget", "create_offspring", "sleep", "idle",
]
ACTION_TYPES: tuple[str, ...] = ActionType.__args__  # type: ignore[attr-defined]

MAX_THOUGHT_CHARS = 600
MAX_TALK_CHARS = 400
MAX_NOTE_CHARS = 600
MAX_NAME_CHARS = 24
MAX_CODE_CHARS = 8000


class MemoryOp(BaseModel):
    model_config = {"extra": "forbid"}
    op: Literal["remember", "revise_self"]
    text: str = Field(max_length=MAX_NOTE_CHARS)
    title: str | None = Field(default=None, max_length=80)
    links_to: list[str] = Field(default_factory=list, max_length=6)
    tags: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("links_to", "tags")
    @classmethod
    def _short_strings(cls, v: list[str]) -> list[str]:
        return [s[:60] for s in v]


class Action(BaseModel):
    model_config = {"extra": "forbid"}
    type: ActionType
    target: str | None = Field(default=None, max_length=80)
    x: float | None = None
    y: float | None = None
    text: str | None = Field(default=None, max_length=MAX_TALK_CHARS)
    note_title: str | None = Field(default=None, max_length=80)
    amount_usd: float | None = None
    delta: float | None = None
    name: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    code: str | None = Field(default=None, max_length=MAX_CODE_CHARS)
    tests: str | None = Field(default=None, max_length=MAX_CODE_CHARS)
    params: dict[str, float] | None = None

    @classmethod
    def idle(cls) -> Action:
        return cls(type="idle")


class Decision(BaseModel):
    model_config = {"extra": "forbid"}
    thought: str = Field(max_length=MAX_THOUGHT_CHARS)
    memory_ops: list[MemoryOp] = Field(default_factory=list, max_length=2)
    action: Action

    @classmethod
    def idle(cls, thought: str = "") -> Decision:
        return cls(thought=thought, memory_ops=[], action=Action.idle())


def _strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Make every object strict: all properties required, no extras, optionals nullable.

    Pydantic emits ``default`` and ``maxLength``-style keywords that structured outputs may
    not accept; we strip validation keywords and keep only the structural core.
    """
    drop = {"default", "maxLength", "minLength", "maxItems", "minItems", "title", "pattern", "format",
            "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        node = {k: v for k, v in node.items() if k not in drop}
        if node.get("type") == "object" and "properties" in node:
            props = {k: walk(v) for k, v in node["properties"].items()}
            node["properties"] = props
            node["required"] = list(props.keys())
            node["additionalProperties"] = False
        elif "additionalProperties" in node and isinstance(node["additionalProperties"], dict):
            node["additionalProperties"] = walk(node["additionalProperties"])
        for key in ("anyOf", "oneOf", "allOf"):
            if key in node:
                node[key] = [walk(n) for n in node[key]]
        if "items" in node:
            node["items"] = walk(node["items"])
        if "$defs" in node:
            node["$defs"] = {k: walk(v) for k, v in node["$defs"].items()}
        return node

    return walk(copy.deepcopy(schema))


def strict_json_schema() -> dict[str, Any]:
    return _strictify(Decision.model_json_schema())
