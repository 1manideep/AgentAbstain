"""Drift test between the hand-written web wire types and the exported schemas (DESIGN §15).

There is no type generator: ``web/src/protocol.ts`` mirrors ``void/server/protocol.py`` by hand and ``void schema``
exports ``schemas/*.schema.json`` from the same models (tests/test_surfaces.py keeps the files current). This test
reads protocol.ts as text and checks the parts a drift would silently break: the ``RunStatus`` union and the other
string unions the schemas pin, and the message ``type`` literals of the ``ServerMsg`` union.

Every message type has an exported schema (``day`` joined ``SCHEMA_NAMES`` with schemas/day.schema.json), so a new
message without one fails here.

The small TypeScript readers here (:func:`ts_union`, :func:`ts_interface_fields`) are also used by
tests/test_server_memory_api.py to hold the memory-layer HTTP responses to their protocol.ts interfaces.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any, get_args

import pytest

from void.server import protocol

REPO = Path(__file__).resolve().parents[1]
PROTOCOL_TS = REPO / "web" / "src" / "protocol.ts"
SCHEMAS = REPO / "schemas"

# Messages that have a wire model but no exported schema yet (see the module docstring).
UNEXPORTED_ALLOWED: frozenset[str] = frozenset()


# --- reading protocol.ts ---------------------------------------------------------------------------------------
@cache
def ts_source() -> str:
    text = PROTOCOL_TS.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # block and doc comments
    return re.sub(r"//[^\n]*", "", text)                # line comments


def _type_alias(name: str) -> str:
    """The right-hand side of ``export type <name> = ...`` (up to the next blank line or declaration)."""
    m = re.search(rf"^export type {name}\s*=\s*(.+?)(?=\n\s*\n|\nexport |\Z)", ts_source(), flags=re.M | re.S)
    assert m is not None, f"protocol.ts has no `export type {name}`"
    return m.group(1)


def ts_union(name: str) -> list[str]:
    """The string literals of ``export type <name> = 'a' | 'b' | ...``, in source order."""
    body = _type_alias(name)
    literals = re.findall(r"'([^']*)'", body)
    assert literals, f"`{name}` is not a union of string literals: {body!r}"
    rest = re.sub(r"'[^']*'", "", body)
    assert set(rest) <= set(" \n\t|"), f"`{name}` mixes literals with other types: {body!r}"
    return literals


def ts_union_members(name: str) -> list[str]:
    """The type names of ``export type <name> = | A | B | ...``."""
    members = [m.strip() for m in _type_alias(name).split("|") if m.strip()]
    for m in members:
        assert re.fullmatch(r"[A-Za-z_]\w*", m), f"`{name}` has a non-name member {m!r}"
    return members


def _block(start: int) -> str:
    """The text between the ``{`` at or after ``start`` and its matching ``}``."""
    src = ts_source()
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    raise AssertionError("unbalanced braces in protocol.ts")


def ts_interface_fields(name: str) -> dict[str, bool]:
    """``{field: required}`` of ``export interface <name>``, including the fields of the interfaces it extends."""
    m = re.search(rf"^export interface {name}\b(?:\s+extends\s+([^{{]+))?\s*\{{", ts_source(), flags=re.M)
    assert m is not None, f"protocol.ts has no `export interface {name}`"
    fields: dict[str, bool] = {}
    for base in (b.strip() for b in (m.group(1) or "").split(",") if b.strip()):
        if re.fullmatch(r"\w+", base) and re.search(rf"^export interface {base}\b", ts_source(), flags=re.M):
            fields.update(ts_interface_fields(base))
    body = _block(m.start())
    depth = 0
    for line in body.split("\n"):
        if depth == 0:
            fm = re.match(r"\s*(\w+)(\?)?\s*:", line)
            if fm:
                fields[fm.group(1)] = fm.group(2) is None
        depth += line.count("{") + line.count("(") + line.count("[") - line.count("}") - line.count(")") - line.count("]")
    return fields


def ts_message_types() -> set[str]:
    """The ``type`` literal of every ``ServerMsg`` member."""
    members = ts_union_members("ServerMsg")
    declared = re.findall(r"^\s+type:\s*'([a-z_]+)'\s*$", ts_source(), flags=re.M)
    # Every member contributes exactly one required ``type: '<literal>'`` field (EventMsg through its EventBase).
    assert len(declared) == len(set(declared)) == len(members), (members, declared)
    return set(declared)


# --- reading schemas/ ------------------------------------------------------------------------------------------
@cache
def schemas() -> dict[str, dict[str, Any]]:
    return {p.name.removesuffix(".schema.json"): json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(SCHEMAS.glob("*.schema.json"))}


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if ref is None:
        return node
    assert ref.startswith("#/$defs/"), ref
    return schema["$defs"][ref.removeprefix("#/$defs/")]


def schema_enum(name: str, *path: str) -> list[str]:
    """The ``enum`` of the property reached by ``path`` in one schema: an optional leading ``$defs`` name, then
    property names (each ``$ref`` on the way is followed)."""
    schema = schemas()[name]
    node: dict[str, Any] = schema
    for i, key in enumerate(path):
        if i == 0 and key in schema.get("$defs", {}):
            node = schema["$defs"][key]
        else:
            node = _resolve(schema, node["properties"][key])
    assert "enum" in node, (name, path, node)
    return list(node["enum"])


# --- tests -----------------------------------------------------------------------------------------------------
def test_schema_files_are_the_exported_message_set() -> None:
    assert set(schemas()) == set(protocol.SCHEMA_NAMES)
    for name, schema in schemas().items():
        assert schema["properties"]["type"]["const"] == name, name


def test_message_type_literals_match_the_schemas() -> None:
    ts_types = ts_message_types()
    schema_types = {s["properties"]["type"]["const"] for s in schemas().values()}
    unexported = set(protocol.MESSAGE_MODELS) - set(protocol.SCHEMA_NAMES)
    assert unexported <= UNEXPORTED_ALLOWED, f"new message types without an exported schema: {unexported - UNEXPORTED_ALLOWED}"
    assert ts_types == set(protocol.MESSAGE_MODELS), "protocol.ts ServerMsg types differ from void/server/protocol.py"
    assert ts_types - unexported == schema_types, "protocol.ts ServerMsg types differ from schemas/*.schema.json"


def test_run_status_union_matches_every_schema() -> None:
    ts = ts_union("RunStatus")
    assert "failed" in ts
    assert sorted(ts) == sorted(get_args(protocol.RunStatus))
    carriers = {name for name, s in schemas().items() if "status" in s["properties"]}
    assert carriers == {"hello", "snapshot", "status"}
    for name in sorted(carriers):
        assert sorted(schema_enum(name, "status")) == sorted(ts), f"schemas/{name}.schema.json status enum"


@pytest.mark.parametrize(("ts_name", "schema", "path"), [
    ("AgentStatus", "roster", ("RosterAgent", "status")),
    ("AgentStatus", "snapshot", ("AgentState", "status")),
    ("Visibility", "event", ("visibility",)),
    ("DegenerationMode", "hello", ("HelloConfig", "degeneration_mode")),
])
def test_other_string_unions_match_the_schemas(ts_name: str, schema: str, path: tuple[str, ...]) -> None:
    assert sorted(ts_union(ts_name)) == sorted(schema_enum(schema, *path))


def test_ts_readers_understand_the_file() -> None:
    """The readers above see what a TypeScript reader would (guards against a silently vacuous drift test)."""
    assert ts_union_members("ServerMsg")[0] == "HelloMsg"
    hello = ts_interface_fields("HelloMsg")
    assert hello["status"] and hello["type"] and "config" in hello
    detail = ts_interface_fields("NoteDetail")  # extends NoteRecord
    assert detail["note_id"] and detail["path_agents"] and detail["tags"] is False
    assert ts_interface_fields("MetricsDaysResponse") == {"days": True}  # inline `{ type?: ... }` is not a field
