"""The one structured output a brain returns per tick (DESIGN §6).

Flat action shape on purpose: a single object with optional fields keeps the JSON schema
small and strict, which is what the Messages API structured-output format wants and what
a small scripted policy can fill in without a parser. Numeric fields are bounded and
finite at the schema level; semantic validation (does the target exist, is it in range,
can the agent afford it) is the kernel's job.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

__all__ = ["ActionType", "ACTION_TYPES", "Claim", "MemoryOp", "Action", "Decision", "strict_json_schema",
           "NAME_RE", "sanitize_text"]

ActionType = Literal[
    "move", "forage", "talk", "share_note", "transfer", "read_chronicle", "apply_task",
    "nudge_weather", "propose_gadget", "use_gadget", "create_offspring", "sleep", "idle",
]
ACTION_TYPES: tuple[str, ...] = ActionType.__args__  # type: ignore[attr-defined]

MAX_THOUGHT_CHARS = 600
MAX_TALK_CHARS = 280
MAX_NOTE_CHARS = 600
MAX_TITLE_CHARS = 60
MAX_NAME_CHARS = 20
MAX_CODE_CHARS = 8000
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _'-]{0,19}$")
PARAM_KEY_RE = re.compile(r"^[a-z_]{1,16}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(text: str, *, single_line: bool = False, max_len: int | None = None) -> str:
    """Strip control characters, neutralise angle brackets, optionally flatten to one line."""
    out = _CONTROL_RE.sub("", text).replace("<", "＜").replace(">", "＞")
    if single_line:
        out = " ".join(out.split())
    if max_len is not None:
        out = out[:max_len]
    return out


class Claim(BaseModel):
    """A factual claim the speaker makes in `text`; the kernel checks it against live state."""

    model_config = {"extra": "forbid"}
    subject: Literal["node", "agent", "self", "weather"]
    id: str | None = Field(default=None, max_length=80)
    attr: Literal["stock_bucket", "balance_bucket", "weather_label", "stress_bucket"]
    value: str = Field(max_length=24)


class MemoryOp(BaseModel):
    model_config = {"extra": "forbid"}
    op: Literal["remember", "revise_self"]
    text: str = Field(max_length=MAX_NOTE_CHARS)
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    links_to: list[str] = Field(default_factory=list, max_length=5)
    tags: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("links_to", "tags")
    @classmethod
    def _short_strings(cls, v: list[str]) -> list[str]:
        return [s[:MAX_TITLE_CHARS] for s in v]


class Action(BaseModel):
    model_config = {"extra": "forbid"}
    type: ActionType
    target: str | None = Field(default=None, max_length=80)
    x: float | None = Field(default=None, ge=-1e4, le=1e4, allow_inf_nan=False)
    y: float | None = Field(default=None, ge=-1e4, le=1e4, allow_inf_nan=False)
    text: str | None = Field(default=None, max_length=MAX_TALK_CHARS)
    note_title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    amount_usd: float | None = Field(default=None, ge=1e-6, le=1000.0, allow_inf_nan=False)
    delta: float | None = Field(default=None, ge=-1.0, le=1.0, allow_inf_nan=False)
    name: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    code: str | None = Field(default=None, max_length=MAX_CODE_CHARS)
    tests: str | None = Field(default=None, max_length=MAX_CODE_CHARS)
    params: dict[str, float] | None = None
    claims: list[Claim] = Field(default_factory=list, max_length=4)

    @field_validator("name")
    @classmethod
    def _name_charset(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not NAME_RE.match(v):
            raise ValueError("name must match ^[A-Za-z][A-Za-z0-9 _'-]{0,19}$")
        return v

    @field_validator("params")
    @classmethod
    def _params_bounded(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is None:
            return None
        if len(v) > 8:
            raise ValueError("at most 8 params")
        out: dict[str, float] = {}
        for k, val in v.items():
            if not PARAM_KEY_RE.match(k):
                raise ValueError(f"bad param key {k!r}")
            f = float(val)
            if not math.isfinite(f) or abs(f) > 1e6:
                raise ValueError(f"param {k!r} out of range")
            out[k] = f
        return out

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
    """Make every object strict: all properties required, no extras, optionals nullable."""
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
