"""The one structured output a brain returns per tick (DESIGN §6).

Flat action shape on purpose: a single object with optional fields keeps the JSON schema
small and strict, which is what the Messages API structured-output format wants and what
a small scripted policy can fill in without a parser. Numeric fields are bounded and
finite at the schema level; semantic validation (does the target exist, is it in range,
can the agent afford it) is the kernel's job.

``share_practice`` (MEMORY_EVOLUTION §5.7) is the one action type a world offers only when a feature is on
(``memory.practices`` together with ``memory.policy``; :func:`share_practice_enabled`). ``Action`` always accepts
it, but the JSON schema of ``Action.type`` lists only :data:`ACTION_TYPES`, and :func:`strict_json_schema` adds it
on request. The provider schema is config-dependent for that reason: one that always listed it would change the
bytes (and so the priced schema tokens) of every world recorded without practice transmission.
"""

from __future__ import annotations

import copy
import json
import math
import re
from functools import cache
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    ValidationError,
    ValidationInfo,
    WithJsonSchema,
    field_validator,
)
from pydantic_core import PydanticCustomError

if TYPE_CHECKING:
    from void.config import VoidConfig

__all__ = ["ActionType", "ACTION_TYPES", "PracticeActionType", "SHARE_PRACTICE", "MAX_EXCERPT_CHARS", "Claim", "MemoryOp",
           "Action", "Decision", "strict_json_schema", "share_practice_enabled", "NAME_RE", "sanitize_text", "parse_lenient",
           "clamp_to_schema"]

M = TypeVar("M", bound=BaseModel)

ActionType = Literal[
    "move", "forage", "talk", "share_note", "transfer", "read_chronicle", "apply_task",
    "nudge_weather", "propose_gadget", "use_gadget", "create_offspring", "sleep", "idle",
]
ACTION_TYPES: tuple[str, ...] = ActionType.__args__  # type: ignore[attr-defined]
# Offered only when practice transmission is on (see the module docstring), so it is not in ACTION_TYPES.
PracticeActionType = Literal["share_practice"]
SHARE_PRACTICE = "share_practice"
_ActionKind = Literal[ActionType, PracticeActionType]  # flattened: one literal, so a bad type is one error as before

MAX_THOUGHT_CHARS = 600
MAX_TALK_CHARS = 280
MAX_NOTE_CHARS = 600
MAX_TITLE_CHARS = 60
MAX_NAME_CHARS = 20
MAX_CODE_CHARS = 8000
# The longest share_practice excerpt an Action accepts: the upper bound of memory.practices.excerpt_max_chars. The
# kernel clips each excerpt to the configured limit; every other action's `text` keeps MAX_TALK_CHARS.
MAX_EXCERPT_CHARS = 2000
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


PARAMS_SCHEMA = {
    "anyOf": [
        {"type": "array", "items": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "number"}},
                                    "required": ["key", "value"], "additionalProperties": False}},
        {"type": "null"},
    ],
    "description": "gadget parameters as a list of {key, value} pairs (snake_case keys, numeric values)",
}


def _coerce_params(v: Any) -> Any:
    """Accept the API's list of {key, value} pairs or an internal dict; store a dict."""
    if v is None or isinstance(v, dict):
        return v
    if isinstance(v, list):
        out: dict[str, Any] = {}
        for item in v:
            if not isinstance(item, dict) or "key" not in item or "value" not in item:
                raise ValueError("params items must be objects with key and value")
            out[str(item["key"])] = item["value"]
        return out
    raise ValueError("params must be a list of {key, value} pairs")


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


# The JSON schemas of Action.type and Action.text stay what they were before share_practice existed (the base enum,
# MAX_TALK_CHARS): strict_json_schema() adds the action on request, and Action._text_length holds the length rule.
_TYPE_SCHEMA: dict[str, Any] = {"enum": list(ACTION_TYPES), "type": "string"}
_TEXT_SCHEMA: dict[str, Any] = {"anyOf": [{"maxLength": MAX_TALK_CHARS, "type": "string"}, {"type": "null"}]}


class Action(BaseModel):
    model_config = {"extra": "forbid"}
    type: Annotated[_ActionKind, WithJsonSchema(_TYPE_SCHEMA)]
    target: str | None = Field(default=None, max_length=80)
    x: float | None = Field(default=None, ge=-1e4, le=1e4, allow_inf_nan=False)
    y: float | None = Field(default=None, ge=-1e4, le=1e4, allow_inf_nan=False)
    text: Annotated[str | None, WithJsonSchema(_TEXT_SCHEMA)] = None
    note_title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    amount_usd: float | None = Field(default=None, ge=1e-6, le=1000.0, allow_inf_nan=False)
    delta: float | None = Field(default=None, ge=-1.0, le=1.0, allow_inf_nan=False)
    name: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    code: str | None = Field(default=None, max_length=MAX_CODE_CHARS)
    tests: str | None = Field(default=None, max_length=MAX_CODE_CHARS)
    params: Annotated[dict[str, float] | None, BeforeValidator(_coerce_params), WithJsonSchema(PARAMS_SCHEMA)] = None
    claims: list[Claim] = Field(default_factory=list, max_length=4)

    @field_validator("text")
    @classmethod
    def _text_length(cls, v: str | None, info: ValidationInfo) -> str | None:
        """At most MAX_TALK_CHARS, or MAX_EXCERPT_CHARS for a share_practice excerpt. The error is the one a
        ``max_length`` field raises, so lenient parsing and error reports read it as before."""
        if v is None:
            return None
        limit = MAX_EXCERPT_CHARS if info.data.get("type") == SHARE_PRACTICE else MAX_TALK_CHARS
        if len(v) > limit:
            raise PydanticCustomError("string_too_long", "String should have at most {max_length} characters", {"max_length": limit})
        return v

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
        if node.get("type") == "object":
            if "properties" not in node:
                raise ValueError("open-ended object (map) fields are not representable in a strict schema")
            props = {k: walk(v) for k, v in node["properties"].items()}
            node["properties"] = props
            node["required"] = list(props.keys())
            node["additionalProperties"] = False
        for key in ("anyOf", "oneOf", "allOf"):
            if key in node:
                node[key] = [walk(n) for n in node[key]]
        if "items" in node:
            node["items"] = walk(node["items"])
        if "$defs" in node:
            node["$defs"] = {k: walk(v) for k, v in node["$defs"].items()}
        return node

    return walk(copy.deepcopy(schema))


def strict_json_schema(*, share_practice: bool = False) -> dict[str, Any]:
    """The strict decision schema sent to providers. ``share_practice`` adds that action to the enum of
    ``Action.type``; a caller passes :func:`share_practice_enabled` of its config. Without it the schema is the one
    every world without practice transmission was recorded with, byte for byte."""
    schema = _strictify(Decision.model_json_schema())
    if share_practice:
        schema["$defs"]["Action"]["properties"]["type"]["enum"].append(SHARE_PRACTICE)
    return schema


def share_practice_enabled(cfg: VoidConfig) -> bool:
    """Whether a world offers ``share_practice``: practice transmission is on and agents have a policy to excerpt."""
    return bool(cfg.memory.practices.enabled and cfg.memory.policy.enabled)


# --- lenient parsing --------------------------------------------------------------------------------------------
# The strict schema sent to the API cannot carry length, count or range limits (see ``_strictify``), so a model
# that writes a 700-character thought or a 31st command has produced valid JSON for the schema it was given.
# Rejecting the whole answer for that would charge the agent for an idle tick; instead the value is brought
# inside the limits (strings cut, lists sliced, numbers clamped) and validated again. Anything else that fails
# (wrong types, a bad name, truncated JSON) still fails with the original error.


@cache
def _limits_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


def clamp_to_schema(value: Any, schema: dict[str, Any], defs: dict[str, Any] | None = None) -> Any:
    """``value`` with every string cut to its ``maxLength``, every list sliced to its ``maxItems`` and every number
    clamped into ``[minimum, maximum]`` as the (non-strict) JSON schema states them; other values are untouched."""
    defs = schema.get("$defs", {}) if defs is None else defs
    node = schema
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        node = defs.get(ref.split("/")[-1], {})
    if "anyOf" in node:
        want = {dict: "object", list: "array", str: "string", bool: "boolean"}.get(type(value))
        if want is None and isinstance(value, int | float):
            want = "number"
        for branch in node["anyOf"]:
            target = defs.get(branch["$ref"].split("/")[-1], {}) if "$ref" in branch else branch
            kind = target.get("type")
            if kind == want or (want == "number" and kind == "integer") or (want == "object" and "properties" in target):
                return clamp_to_schema(value, branch, defs)
        return value
    if isinstance(value, dict) and isinstance(node.get("properties"), dict):
        props = node["properties"]
        return {k: clamp_to_schema(v, props[k], defs) if k in props else v for k, v in value.items()}
    if isinstance(value, list):
        limit = node.get("maxItems")
        items = value[: int(limit)] if isinstance(limit, int) else value
        sub = node.get("items")
        return [clamp_to_schema(v, sub, defs) for v in items] if isinstance(sub, dict) else items
    if isinstance(value, str):
        limit = node.get("maxLength")
        return value[: int(limit)] if isinstance(limit, int) else value
    if isinstance(value, int | float) and not isinstance(value, bool):
        lo, hi = node.get("minimum"), node.get("maximum")
        if isinstance(lo, int | float) and value < lo:
            return lo
        if isinstance(hi, int | float) and value > hi:
            return hi
    return value


def parse_lenient(model: type[M], text: str) -> M:
    """Validate ``text`` against ``model``; on a failure, retry once with the value clamped to the model's limits.
    Raises the strict attempt's error when the repaired value fails too (or the text is not JSON)."""
    try:
        return model.model_validate_json(text)
    except ValidationError as strict_error:
        try:
            data = json.loads(text)
        except ValueError:
            raise strict_error from None
        try:
            return model.model_validate(clamp_to_schema(data, _limits_schema(model)))
        except ValidationError:
            raise strict_error from None
