"""Registry hygiene: every emitted event kind is a ``Kind`` constant, every constant is emitted or reserved, and every
value a ``CHECK (column IN (...))`` constraint of the schema enumerates is written somewhere or reserved.

The emitted kinds are found statically: every ``Event(...)`` built in ``void/**/*.py`` (in a module that imports
``Event`` from ``void.events``) has its kind argument resolved through

* ``Kind.X``;
* a local name the enclosing function binds to ``Kind.X`` (the benefactor's ``public_kind``);
* a parameter of the enclosing function, through the arguments its call sites in the same module pass for it (the
  server's ``_emit_control(sim, Kind.PAUSE, {})``);
* a string literal, or a module-level name bound to one: a *raw* kind, outside the registry.

Anything else is unresolvable and fails the guard, so a new way of emitting has to be taught here first.

The enumerated database values are checked against explicit evidence (a module and a code fragment that writes the
value, which must still be there) plus the SQL literals that write a value directly (``UPDATE t SET c='v'``, an
``INSERT INTO t(...)`` whose ``VALUES`` slot for ``c`` is ``'v'``). A reserved value must have no such write and no
``column="value"`` keyword or parameter default anywhere in ``void/``: once something writes it, the reservation has
to go.

Also here: the gate's ``propose_gadget`` event (the web's tinker gesture listens for it) and the run table's
``windfall_string``, which holds the exact text a windfall delivers under the run's disclosure (DESIGN §9.5).
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from void.agents.models import AgentRecord, PersonalitySeed
from void.agents.registry import AgentRegistry
from void.brain.gadget_templates import TEMPLATES
from void.config import load_config, micro_to_usd, usd_to_micro
from void.db import SCHEMA, Database
from void.economy.benefactor import LEGIBLE_STRING, OPAQUE_STRING
from void.economy.wallet import Wallet
from void.events import EventBus, Kind
from void.ids import IdFactory
from void.rng import RNG
from void.sandbox.gate import GadgetGate
from void.sandbox.registry import GadgetRegistry
from void.sandbox.runner import SandboxResult
from void.sim.loop import Simulation
from void.types import AgentStatus, Vec2

REPO = Path(__file__).resolve().parents[1]
VOID = REPO / "void"

# --- the registry's exceptions -----------------------------------------------------------------------------------------
# Kind constants nothing emits, with the reason.
RESERVED_KINDS: dict[str, str] = {
    Kind.LLM_CALL: "ledger kind only: a decision call is an llm_calls row and a wallet_ledger entry of kind llm_call",
    Kind.BANKRUPT: "death payload carries cause: a bankruptcy is a death event whose cause says why",
}
# Kinds a parallel wave-3 item starts to emit: tolerated whether or not that work is merged yet. Remove the entry
# once it is.
IN_FLIGHT_KINDS: dict[str, str] = {}
# Kinds emitted outside the registry, with why they still are. Each one needs a Kind constant in void/events.py (a
# shared surface) before its emitter can switch to it; the entry must then go.
OUTSIDE_REGISTRY: dict[str, str] = {}

# Enumerated database values written somewhere, with the evidence the SQL scan cannot see: (module, code fragment).
EVIDENCE: dict[tuple[str, str, str], tuple[str, str]] = {
    ("run", "status", "running"): ("void/sim/loop.py", '"running", hashlib.sha256(self.operator_token.encode())'),
    ("run", "status", "completed"): ("void/sim/loop.py", 'self._end_run("completed"'),
    ("run", "status", "capped_total"): ("void/sim/loop.py", 'self._end_run("capped_total"'),
    ("run", "status", "extinct"): ("void/sim/loop.py", 'self._end_run("extinct"'),
    ("agents", "status", "alive"): ("void/agents/lifecycle.py", "status=AgentStatus.ALIVE"),
    ("agents", "status", "bankrupt"): ("void/agents/lifecycle.py", "status=AgentStatus.BANKRUPT"),
    ("agents", "status", "archived"): ("void/agents/lifecycle.py", "status=AgentStatus.ARCHIVED"),
    ("gadgets", "status", "rejected"): ("void/sandbox/gate.py", '"rejected", {}, {}, verification'),
    ("gadgets", "status", "verified"): ("void/sandbox/gate.py", 'sha256(tests), "verified"'),
    ("practice_events", "kind", "suggested"): ("void/culture/practices.py", 'return self.record("suggested", tick'),
    ("practice_events", "kind", "adopted"): ("void/culture/practices.py", 'return self.record("adopted", tick'),
    ("practice_events", "kind", "persisted"): ("void/culture/practices.py", 'return self.record("persisted", tick'),
    ("practice_events", "kind", "dropped"): ("void/culture/practices.py", 'return self.record("dropped", tick'),
    ("policy_versions", "source", "seed"): ("void/memory/policy.py", 'source="seed"'),
    ("policy_versions", "source", "inherited"): ("void/memory/policy.py", 'source="inherited"'),
    ("policy_versions", "source", "self_edit"): ("void/memory/commands.py", 'source="self_edit"'),
    ("policy_versions", "source", "adapted"): ("void/sim/maintenance.py", 'source="adapted", source_agent_id=parent'),
    ("policy_versions", "source", "mutated"): ("void/agents/lifecycle.py", 'source="mutated")'),
    ("policy_versions", "source", "adopted"): ("void/sim/maintenance.py", 'source="adopted", source_agent_id=adopted[0]'),
    ("memory_commands", "purpose", "maintain"): ("void/sim/maintenance.py", 'purpose="maintain", rights='),
    ("memory_commands", "purpose", "tick"): ("void/memory/commands.py", 'tick, "tick", command'),
    **{("memory_commands", "command", c): ("void/memory/commands.py",
                                           'CommandName = Literal["view", "create", "str_replace", "insert", "delete", "rename"]')
       for c in ("view", "create", "str_replace", "insert", "delete", "rename")},
    ("exam_items", "exam", "audit"): ("void/research/exams.py", 'AUDIT = "audit"'),
    ("exam_items", "exam", "reward"): ("void/research/exams.py", 'REWARD = "reward"'),
    ("note_manifest", "written_by", "kernel"): ("void/memory/store.py", 'written_by: str = "kernel"'),
    ("note_manifest", "written_by", "operator"): ("void/server/mcp.py", 'written_by="operator"'),
}
# Enumerated values nothing writes yet, with the reason.
RESERVED_VALUES: dict[tuple[str, str, str], str] = {
    ("memory_commands", "purpose", "recall"): "C3/C4, [proposed]",
    ("memory_commands", "purpose", "kernel"): "C3/C4, [proposed]",
    ("gadgets", "status", "proposed"): "the gate settles a proposal in the tick it is made: its row is inserted verified or rejected",
    ("note_manifest", "written_by", "agent"): "agents change their vault only through memory commands, which the kernel applies "
                                              "and records as written_by kernel",
}
# Values a parallel wave-3 item starts to write (tolerated either way; remove once merged).
IN_FLIGHT_VALUES: dict[tuple[str, str, str], str] = {}


# --- the static scan -----------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Emission:
    where: str
    status: str  # kind | raw | unresolved
    value: str


def _sources() -> list[Path]:
    return sorted(VOID.rglob("*.py"))


def _imports_event(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.ImportFrom) and n.module == "void.events" and any(a.name == "Event" for a in n.names)
               for n in ast.walk(tree))


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _enclosing(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    cur = parents.get(node)
    while cur is not None and not isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
        cur = parents.get(cur)
    return cur


def _kind_arg(call: ast.Call) -> ast.expr | None:
    if len(call.args) > 2:
        return call.args[2]
    return next((k.value for k in call.keywords if k.arg == "kind"), None)


class _Resolver:
    def __init__(self, tree: ast.Module, rel: str) -> None:
        self.tree = tree
        self.rel = rel
        self.parents = _parents(tree)

    def resolve(self, expr: ast.expr, func: ast.FunctionDef | ast.AsyncFunctionDef | None, where: str,
                depth: int = 0) -> list[Emission]:
        if depth > 4:
            return [Emission(where, "unresolved", ast.unparse(expr))]
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id == "Kind":
            value = getattr(Kind, expr.attr, None)
            return [Emission(where, "kind", value) if isinstance(value, str) else Emission(where, "unresolved", ast.unparse(expr))]
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return [Emission(where, "raw", expr.value)]
        if isinstance(expr, ast.IfExp):
            return self.resolve(expr.body, func, where, depth + 1) + self.resolve(expr.orelse, func, where, depth + 1)
        if isinstance(expr, ast.Name):
            if func is not None:
                params = [a.arg for a in (*func.args.posonlyargs, *func.args.args)]
                if expr.id in params or expr.id in [a.arg for a in func.args.kwonlyargs]:
                    return self._from_call_sites(func, expr.id, where, depth)
                bound = [n.value for n in ast.walk(func) if isinstance(n, ast.Assign)
                         and any(isinstance(t, ast.Name) and t.id == expr.id for t in n.targets)]
                if bound:
                    return [e for v in bound for e in self.resolve(v, func, where, depth + 1)]
            bound = [n.value for n in self.tree.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == expr.id for t in n.targets)]
            if bound:
                return [e for v in bound for e in self.resolve(v, None, where, depth + 1)]
        return [Emission(where, "unresolved", ast.unparse(expr))]

    def _from_call_sites(self, func: ast.FunctionDef | ast.AsyncFunctionDef, param: str, where: str,
                         depth: int) -> list[Emission]:
        positional = [a.arg for a in (*func.args.posonlyargs, *func.args.args)]
        out: list[Emission] = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if name != func.name:
                continue
            offset = 1 if isinstance(fn, ast.Attribute) and positional[:1] in (["self"], ["cls"]) else 0
            arg: ast.expr | None = next((k.value for k in node.keywords if k.arg == param), None)
            if arg is None and param in positional:
                i = positional.index(param) - offset
                arg = node.args[i] if 0 <= i < len(node.args) else None
            if arg is not None:
                out += self.resolve(arg, _enclosing(node, self.parents), f"{where} via {self.rel}:{node.lineno}", depth + 1)
        return out or [Emission(where, "unresolved", f"parameter {param} of {func.name} with no call site")]


def emissions() -> list[Emission]:
    out: list[Emission] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not _imports_event(tree):
            continue
        rel = str(path.relative_to(REPO))
        res = _Resolver(tree, rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Event":
                where = f"{rel}:{node.lineno}"
                arg = _kind_arg(node)
                out += ([Emission(where, "unresolved", "no kind argument")] if arg is None
                        else res.resolve(arg, _enclosing(node, res.parents), where))
    return out


def kind_values() -> set[str]:
    return {v for k, v in vars(Kind).items() if k.isupper() and isinstance(v, str)}


# --- enumerated database values --------------------------------------------------------------------------------------------
def check_values() -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    table = None
    for line in SCHEMA.splitlines():
        m = re.match(r"\s*CREATE TABLE IF NOT EXISTS (\w+)", line)
        if m:
            table = m.group(1)
        for cm in re.finditer(r"CHECK\s*\((\w+)\s+IN\s*\(([^)]*)\)\)", line):
            assert table is not None
            out |= {(table, cm.group(1), v) for v in re.findall(r"'([^']*)'", cm.group(2))}
    return out


def _string_constants() -> list[tuple[str, str]]:
    out = []
    for path in _sources():
        if path == VOID / "db.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append((f"{path.relative_to(REPO)}:{node.lineno}", node.value))
    return out


def sql_writes() -> dict[tuple[str, str, str], list[str]]:
    """Values written by an SQL literal: ``UPDATE t SET ... c='v'`` or ``INSERT INTO t(..c..) VALUES(..'v'..)``."""
    found: dict[tuple[str, str, str], list[str]] = {}
    for where, text in _string_constants():
        sql = " ".join(text.split())
        for m in re.finditer(r"\bUPDATE (\w+) SET (.*?)(?: WHERE |$)", sql):
            for col, val in re.findall(r"\b(\w+)\s*=\s*'([^']*)'", m.group(2)):
                found.setdefault((m.group(1), col, val), []).append(where)
        for m in re.finditer(r"\bINSERT (?:OR \w+ )?INTO (\w+)\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)", sql):
            cols = [c.strip() for c in m.group(2).split(",")]
            vals = [v.strip() for v in m.group(3).split(",")]
            for col, val in zip(cols, vals, strict=False):
                if len(val) >= 2 and val[0] == val[-1] == "'":
                    found.setdefault((m.group(1), col, val[1:-1]), []).append(where)
    return found


def keyword_writes() -> dict[tuple[str, str], list[str]]:
    """``column="value"`` as a call keyword or a parameter default anywhere in ``void/`` (db.py aside)."""
    found: dict[tuple[str, str], list[str]] = {}
    for path in _sources():
        if path == VOID / "db.py":
            continue
        rel = str(path.relative_to(REPO))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            pairs: list[tuple[str, ast.expr]] = []
            if isinstance(node, ast.Call):
                pairs = [(k.arg, k.value) for k in node.keywords if k.arg]
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                a = node.args
                positional = [*a.posonlyargs, *a.args]
                pairs = [(p.arg, d) for p, d in zip(positional[len(positional) - len(a.defaults):], a.defaults, strict=True)]
                pairs += [(p.arg, d) for p, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is not None]
            for name, value in pairs:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.setdefault((name, value.value), []).append(f"{rel}:{node.lineno}")
    return found


# --- tests ------------------------------------------------------------------------------------------------------------------
def test_every_emission_resolves_to_a_kind():
    unresolved = [e for e in emissions() if e.status == "unresolved"]
    assert not unresolved, f"event kinds the guard cannot resolve (teach tests/test_event_registry.py): {unresolved}"


def test_every_emitted_kind_is_a_kind_constant():
    found = emissions()
    raw = {e.value for e in found if e.status == "raw"}
    where = {e.value: e.where for e in found if e.status == "raw"}
    assert raw - set(OUTSIDE_REGISTRY) == set(), {k: where[k] for k in raw - set(OUTSIDE_REGISTRY)}
    assert set(OUTSIDE_REGISTRY) - raw == set(), "no longer emitted outside the registry: drop it from OUTSIDE_REGISTRY"
    assert not raw & kind_values(), "a raw string equal to a Kind value: use the constant"
    # the six that used to be raw strings are now constants with emitters
    emitted = {e.value for e in found if e.status == "kind"}
    assert {Kind.GADGET_TAMPERED, Kind.GADGET_USE_FAILED, Kind.SANDBOX_SELF_TEST, Kind.OPERATOR_COMMAND,
            Kind.TRACER_PLANTED, Kind.RUN_ENDED} <= emitted


def test_every_kind_constant_is_emitted_or_reserved():
    emitted = {e.value for e in emissions() if e.status == "kind"}
    silent = kind_values() - emitted - set(IN_FLIGHT_KINDS)
    assert silent == set(RESERVED_KINDS), {
        "emitted but still reserved": sorted(set(RESERVED_KINDS) & emitted),
        "never emitted and not reserved": sorted(silent - set(RESERVED_KINDS)),
    }
    assert all(reason.strip() for reason in (*RESERVED_KINDS.values(), *IN_FLIGHT_KINDS.values(), *OUTSIDE_REGISTRY.values()))
    assert Kind.PROPOSE_GADGET in emitted  # the web's tinker gesture (web/src/scene/humans/behaviour.ts) listens for it


def test_every_enumerated_db_value_is_written_or_reserved():
    values = check_values()
    assert len(values) > 30  # the parse found the CHECK lists
    by_sql = sql_writes()
    written = {v for v in values if v in by_sql} | set(EVIDENCE)
    accounted = written | set(RESERVED_VALUES) | set(IN_FLIGHT_VALUES)
    assert values - accounted == set(), f"enumerated values nothing writes and nothing reserves: {sorted(values - accounted)}"
    assert set(EVIDENCE) | set(RESERVED_VALUES) | set(IN_FLIGHT_VALUES) <= values, "an entry names a value the schema does not enumerate"
    assert not set(EVIDENCE) & set(RESERVED_VALUES)
    assert all(reason.strip() for reason in (*RESERVED_VALUES.values(), *IN_FLIGHT_VALUES.values()))


@pytest.mark.parametrize("key", sorted(EVIDENCE), ids=lambda k: ".".join(k))
def test_evidence_is_still_in_its_module(key):
    module, fragment = EVIDENCE[key]
    assert fragment in (REPO / module).read_text(encoding="utf-8"), f"{key}: {fragment!r} is gone from {module}"


def test_agent_status_evidence_names_the_enumerated_values():
    assert {AgentStatus.ALIVE.value, AgentStatus.BANKRUPT.value, AgentStatus.ARCHIVED.value} == {"alive", "bankrupt", "archived"}


def test_reserved_values_have_no_writer():
    by_sql, by_keyword = sql_writes(), keyword_writes()
    stale = {k: by_sql.get(k, []) + by_keyword.get((k[1], k[2]), []) for k in RESERVED_VALUES}
    assert not {k: v for k, v in stale.items() if v}, "now written: drop the reservation"


# --- the gate's propose_gadget event -------------------------------------------------------------------------------------------
class _FailingTests:
    """A sandbox that is available and fails every proposal at its tests (no subprocess)."""

    isolated = False
    available = True

    async def verify(self, code: str, tests: str, seed: int, *, run_tests: bool = True) -> SandboxResult:
        return SandboxResult(False, "tests", "AssertionError", isolated=False)

    async def run(self, code: str, params: dict[str, float], seed: int) -> SandboxResult:  # pragma: no cover - unused
        return SandboxResult(False, "unavailable", "unused", isolated=False)


def test_a_submitted_proposal_emits_propose_gadget_before_its_verdict(tmp_path):
    cfg = load_config(REPO / "configs" / "scripted_smoke.yaml", {"sandbox": {"proposals_per_agent_per_day": 10}})
    db = Database(tmp_path / "w.db")
    reg = AgentRegistry(db)
    reg.insert(AgentRecord("ag_0", "Ada", "scripted", 2_000_000, PersonalitySeed(), "v", 0, AgentStatus.ALIVE, Vec2(0, 0),
                           0, 100, 0, False, 0))
    reg.insert(AgentRecord("ag_1", "Bo", "scripted", 1, PersonalitySeed(), "v", 0, AgentStatus.ALIVE, Vec2(0, 0), 0, 100, 0, False, 0))
    ids = IdFactory("t")
    bus = EventBus(persist=db.persist_event)
    gate = GadgetGate(cfg, db, GadgetRegistry(db, tmp_path / "gadgets"), reg, _FailingTests(), Wallet(db, cfg, ids), ids, bus, RNG(1))
    by_label = {t.label: t for t in TEMPLATES}
    for i, label in enumerate(("oversized", "static_fail", "valid_beacon")):  # size, static, then the sandbox's tests
        t = by_label[label]
        gate.reset_tick()
        assert not asyncio.run(gate.propose("ag_0", i + 1, 1, t.name, t.purpose, t.code, t.tests, position=(0.0, 0.0))).ok
    t = by_label["valid_beacon"]
    poor = asyncio.run(gate.propose("ag_1", 9, 1, t.name, t.purpose, t.code, t.tests, position=(0.0, 0.0)))
    assert (poor.ok, poor.stage) == (False, "fee")  # not submitted: no event
    rows = db.fetchall("SELECT kind, agent_id, payload, visibility FROM events ORDER BY seq")
    kinds = [r["kind"] for r in rows]
    assert kinds == [Kind.PROPOSE_GADGET, Kind.GADGET_REJECTED] * 3
    fee = micro_to_usd(usd_to_micro(cfg.sandbox.gadget_fee_usd))
    for proposed, verdict in zip(rows[0::2], rows[1::2], strict=True):
        p, v = json.loads(proposed["payload"]), json.loads(verdict["payload"])
        assert set(p) == {"gadget_id", "agent_id", "name", "fee_usd"} and p["fee_usd"] == fee
        assert (p["gadget_id"], p["agent_id"], p["name"]) == (v["gadget_id"], v["owner"], v["name"])
        assert proposed["agent_id"] == "ag_0" and proposed["visibility"] == "public"
    assert [json.loads(r["payload"])["stage"] for r in rows[1::2]] == ["size", "static", "tests"]


# --- the run table's windfall string --------------------------------------------------------------------------------------------
def _windfall_overrides(disclosure: str) -> dict:
    # children stay on the scripted tier (NO_PROVIDER_TIERS); a grant every few ticks on average
    return {"population": {"tier_mutation_prob": 0.0}, "run": {"days": 1},
            "benefactor": {"enabled": True, "mean_interval_ticks": 3, "disclosure": disclosure}}


@pytest.mark.parametrize("disclosure", ["opaque", "legible"])
def test_windfall_string_is_the_delivered_text(run_sim, disclosure):
    """``run.windfall_string`` is the benefactor's template for the disclosure: formatted with a grant's amount it is,
    character for character, the line the recipient hears in its next logged observation."""
    run = run_sim(f"windfall_string_{disclosure}", _windfall_overrides(disclosure))
    template = run.db.fetchone("SELECT windfall_string FROM run")["windfall_string"]
    assert template == (OPAQUE_STRING if disclosure == "opaque" else LEGIBLE_STRING)
    assert template == Simulation.windfall_template(run.cfg)
    grants = run.events(Kind.BENEFACTOR_GRANT)
    assert grants
    heard_kind = "windfall" if disclosure == "opaque" else "transfer"
    delivered = 0
    for g in grants:
        aid, tick, usd = g["agent_id"], g["tick"], g["payload"]["amount_usd"]
        for r in run.db.fetchall("SELECT observation FROM llm_calls WHERE agent_id=? AND tick>=? AND observation IS NOT NULL "
                                 "ORDER BY tick, call_id", (aid, tick)):
            heard = [h for h in json.loads(r["observation"]).get("heard", []) if h["kind"] == heard_kind and h["tick"] == tick]
            if heard:
                assert [h["text"] for h in heard] == [template.format(amount=usd)]
                delivered += 1
                break
    assert delivered >= 1, "no recipient heard a grant in a logged observation"


def test_a_manual_grant_delivers_the_stored_string(make_sim):
    sim = make_sim("windfall_string_manual", _windfall_overrides("opaque"))
    try:
        aid = sorted(a.agent_id for a in sim.registry.alive())[0]
        g = sim.benefactor.grant(aid, usd_to_micro(0.37), 1, 1, manual=True)
        template = sim.db.fetchone("SELECT windfall_string FROM run")["windfall_string"]
        assert g.message.text == template.format(amount=0.37) == "Your balance increased by $0.37. No source is recorded."
    finally:
        sim.db.close()

