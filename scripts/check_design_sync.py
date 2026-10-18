#!/usr/bin/env python
"""Check that docs/DESIGN.md (and docs/PLAN.md) still match the code: ``python scripts/check_design_sync.py``.

Prints one line per mismatch and exits 1 when there is any; prints nothing and exits 0 when the documents agree with
the code. Every check reads the code as built (pydantic models, the argparse parser, a fresh database, the configs),
never a hand-kept list:

* config (DESIGN section 4): every leaf path of ``VoidConfig``, found by walking the pydantic models, appears in
  backticks in section 4 (``tiers.<name>.model`` for a tier field, ``agents[].name`` for a list item); every
  backticked config path in section 4's key tables names a real key; and a default given there in backticks equals
  the value ``configs/base.yaml`` resolves to.
* layout (section 2): the file tree under ``void/`` equals ``find void -name '*.py'`` without ``__init__.py`` and
  ``__pycache__``, and the ``cli.py`` line lists exactly the subcommands of ``void --help``.
* experiments (section 17): for each ``configs/exp_*.yaml`` that names an arm, the experiment table has a row for its
  experiment listing that arm, with ``run.days``, ``run.ticks_per_day``, the primary outcome, the secondary outcomes
  (in order), the independent variables and the hypothesis names equal to what ``load_config`` resolves; a row or an
  arm without a config is reported too.
* data model (section 3): every table of a fresh ``world.db`` is declared in the schema block, with every column.
* CSP (section 15): the policy quoted there equals ``void.server.auth.CSP``.
* paths: every backticked repository path in DESIGN (``tests/...``, ``scripts/...``, ``void/...``, ``configs/...``,
  ``web/...``, ``docs/...``, ``schemas/...``) exists.
* model identifiers: neither DESIGN nor PLAN carries one (CLAUDE.md); tiers are referred to by rung name.
"""

from __future__ import annotations

import re
import sys
import tempfile
import types
import typing
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402
from pydantic import BaseModel  # noqa: E402

DESIGN = REPO / "docs" / "DESIGN.md"
PLAN = REPO / "docs" / "PLAN.md"
BASE_CONFIG = REPO / "configs" / "base.yaml"

# the acceptance pattern of the CLAUDE.md rule, case-insensitive
MODEL_ID_RE = re.compile(r"(claude|gemini)-[0-9a-z][0-9a-z.-]*", re.IGNORECASE)
_FENCE_RE = re.compile(r"^```[^\n]*\n.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_PATH_PREFIXES = ("tests/", "scripts/", "void/", "configs/", "web/", "docs/", "schemas/")


# --- reading the documents ------------------------------------------------------------------------------------
def section(text: str, number: str) -> str:
    """The body of ``## <number>. ...`` up to the next level-2 heading ('' when there is no such section)."""
    m = re.search(rf"^## {re.escape(number)}\. .*$", text, re.MULTILINE)
    if m is None:
        return ""
    nxt = re.search(r"^## ", text[m.end():], re.MULTILINE)
    return text[m.start(): m.end() + nxt.start()] if nxt else text[m.start():]


def fenced_blocks(text: str) -> list[str]:
    """The contents of the fenced code blocks of ``text``, fences removed."""
    return [b.split("\n", 1)[1].rsplit("```", 1)[0] for b in _FENCE_RE.findall(text)]


def code_spans(text: str) -> list[str]:
    """The inline code spans of ``text`` outside fenced blocks, in order."""
    return _CODE_SPAN_RE.findall(_FENCE_RE.sub("", text))


def table_rows(text: str, first_header: str) -> list[list[str]]:
    """The body rows (cells, stripped) of the markdown tables in ``text`` whose first header cell is ``first_header``."""
    rows: list[list[str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        cells = _cells(lines[i])
        if cells and cells[0] == first_header and i + 1 < len(lines) and set(lines[i + 1].replace("|", "").strip()) <= set("-: "):
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            continue
        i += 1
    return rows


def _cells(line: str) -> list[str]:
    s = line.strip()
    if not s.startswith("|"):
        return []
    return [c.strip() for c in s.strip("|").split("|")]


# --- (a) configuration ----------------------------------------------------------------------------------------
def _models_in(ann: Any) -> list[type[BaseModel]]:
    """The pydantic models inside an annotation (``X``, ``X | None``)."""
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return [ann]
    if typing.get_origin(ann) in (typing.Union, types.UnionType):
        return [m for a in typing.get_args(ann) for m in _models_in(a)]
    return []


def config_leaf_paths() -> list[str]:
    """Every leaf key of ``VoidConfig``: ``a.b.c``; ``tiers.<name>.x`` under a dict of models; ``agents[].x`` in a list."""
    from void.config import VoidConfig

    out: list[str] = []

    def walk(model: type[BaseModel], prefix: str) -> None:
        for name, field in model.model_fields.items():
            leaf(field.annotation, f"{prefix}{name}")

    def leaf(ann: Any, path: str) -> None:
        origin, args = typing.get_origin(ann), typing.get_args(ann)
        sub = _models_in(ann)
        if sub:
            walk(sub[0], f"{path}.")
        elif origin is dict and args and _models_in(args[1]):
            walk(_models_in(args[1])[0], f"{path}.<name>.")
        elif origin is list and args and _models_in(args[0]):
            walk(_models_in(args[0])[0], f"{path}[].")
        else:
            out.append(path)

    walk(VoidConfig, "")
    return out


def _parent_paths(leaves: list[str]) -> set[str]:
    out: set[str] = set()
    for p in leaves:
        parts = p.split(".")
        for i in range(1, len(parts)):
            out.add(".".join(parts[:i]))
            out.add(".".join(parts[:i]).removesuffix("[]"))
    return out


def _base_value(data: Any, path: str) -> tuple[bool, Any]:
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _same(doc: Any, code: Any) -> bool:
    if isinstance(doc, bool) or isinstance(code, bool):
        return doc is code
    if isinstance(doc, (int, float)) and isinstance(code, (int, float)):
        return float(doc) == float(code)
    if isinstance(doc, list) and isinstance(code, (list, tuple)):
        return len(doc) == len(code) and all(_same(a, b) for a, b in zip(doc, code, strict=True))
    if isinstance(doc, dict) and isinstance(code, dict):
        return doc.keys() == code.keys() and all(_same(doc[k], code[k]) for k in doc)
    return bool(doc == code)


def check_config(design: str) -> list[str]:
    from void.config import load_config

    s4 = section(design, "4")
    if not s4:
        return ["DESIGN: section 4 (Configuration) not found"]
    leaves = config_leaf_paths()
    spans = set(code_spans(s4))
    out = [f"DESIGN §4: config key `{p}` is not documented" for p in leaves if p not in spans]
    known = set(leaves) | _parent_paths(leaves)
    base = load_config(BASE_CONFIG).model_dump(mode="json")
    rows = table_rows(s4, "Key")
    if not rows:
        out.append("DESIGN §4: no key table (a table whose first header is 'Key')")
    for cells in rows:
        keys = _CODE_SPAN_RE.findall(cells[0]) if cells else []
        for key in keys:
            if key not in known:
                out.append(f"DESIGN §4: `{key}` is not a config key")
        if len(keys) != 1 or len(cells) < 2 or "<name>" in keys[0] or "[]" in keys[0]:
            continue
        default = _CODE_SPAN_RE.findall(cells[1])
        if not default:
            continue
        found, value = _base_value(base, keys[0])
        if not found:
            continue
        try:
            doc = yaml.safe_load(default[0])
        except yaml.YAMLError:
            out.append(f"DESIGN §4: the default of `{keys[0]}` (`{default[0]}`) is not YAML")
            continue
        if not _same(doc, value):
            out.append(f"DESIGN §4: `{keys[0]}` default `{default[0]}` but configs/base.yaml resolves to {value!r}")
    return out


# --- (b) layout and CLI ---------------------------------------------------------------------------------------
def tree_modules(s2: str) -> tuple[set[str], list[str] | None]:
    """The ``void/`` modules the section 2 tree lists, and the CLI subcommands on its ``cli.py`` line."""
    blocks = fenced_blocks(s2)
    if not blocks:
        return set(), None
    files: set[str] = set()
    cli: list[str] | None = None
    inside = False
    current = ""
    for line in blocks[0].splitlines():
        if not inside:
            inside = line.strip() == "void/"
            continue
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            break
        tokens = line.split()
        if indent <= 2:
            current = tokens[0] if tokens[0].endswith("/") else ""
        m = re.match(r"^\s*cli\.py\s+(.*)$", line)
        if m:
            cli = [t.strip() for t in m.group(1).split("|") if t.strip()]
            files.add("cli.py")
            continue
        files |= {f"{current}{t}" for t in tokens if t.endswith(".py")}
    return files, cli


def code_modules() -> set[str]:
    root = REPO / "void"
    return {p.relative_to(root).as_posix() for p in root.rglob("*.py")
            if p.name != "__init__.py" and "__pycache__" not in p.parts}


def cli_subcommands() -> list[str]:
    import argparse

    from void.cli import build_parser

    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices)
    return []


def check_layout(design: str) -> list[str]:
    s2 = section(design, "2")
    if not s2:
        return ["DESIGN: section 2 (Repository layout) not found"]
    listed, cli = tree_modules(s2)
    actual = code_modules()
    out = [f"DESIGN §2: void/{m} exists but is not in the tree" for m in sorted(actual - listed)]
    out += [f"DESIGN §2: the tree lists void/{m}, which does not exist" for m in sorted(listed - actual)]
    subs = cli_subcommands()
    if cli is None:
        out.append("DESIGN §2: no `cli.py` line listing the subcommands")
    else:
        out += [f"DESIGN §2: subcommand `{s}` (void --help) is not on the cli.py line" for s in subs if s not in cli]
        out += [f"DESIGN §2: the cli.py line lists `{s}`, which void --help does not have" for s in cli if s not in subs]
    return out


# --- (c) experiments ------------------------------------------------------------------------------------------
def check_experiments(design: str) -> list[str]:
    from void.config import load_config

    s17 = section(design, "17")
    if not s17:
        return ["DESIGN: section 17 (Research harness) not found"]
    rows: dict[str, list[str]] = {}
    out: list[str] = []
    for cells in table_rows(s17, "Experiment"):
        names = _CODE_SPAN_RE.findall(cells[0]) if cells else []
        if len(names) != 1 or len(cells) < 8:
            out.append(f"DESIGN §17: malformed experiment row: {' | '.join(cells)}")
            continue
        rows[names[0]] = cells
    if not rows:
        return ["DESIGN §17: no experiment table (a table whose first header is 'Experiment')"]
    seen_arms: dict[str, set[str]] = {}
    for path in sorted((REPO / "configs").glob("exp_*.yaml")):
        cfg = load_config(path)
        exp, arm = cfg.experiment.name, cfg.experiment.arm
        if arm is None:
            continue  # a base file the arms extend
        where = f"DESIGN §17 {exp} (configs/{path.name})"
        cells = rows.get(str(exp))
        if cells is None:
            out.append(f"DESIGN §17: experiment `{exp}` (configs/{path.name}) has no row")
            continue
        seen_arms.setdefault(str(exp), set()).add(arm)
        arms = _CODE_SPAN_RE.findall(cells[1])
        if arm not in arms:
            out.append(f"{where}: arm `{arm}` is not listed")
        want: list[tuple[str, Any, Any]] = [
            ("days", _int(cells[2]), cfg.run.days),
            ("ticks_per_day", _int(cells[3]), cfg.run.ticks_per_day),
            ("primary", _CODE_SPAN_RE.findall(cells[4]), [cfg.experiment.primary_outcome] if cfg.experiment.primary_outcome else []),
            ("secondaries", _CODE_SPAN_RE.findall(cells[5]), list(cfg.experiment.secondary_outcomes)),
            ("independent variables", sorted(_CODE_SPAN_RE.findall(cells[6])), sorted(cfg.experiment.independent_variables)),
            ("hypotheses", sorted(_CODE_SPAN_RE.findall(cells[7])), sorted(h.name for h in cfg.experiment.hypotheses)),
        ]
        for label, doc, code in want:
            if doc != code:
                out.append(f"{where}: {label} {doc!r} but load_config gives {code!r}")
    for exp, cells in rows.items():
        listed = set(_CODE_SPAN_RE.findall(cells[1]))
        for arm in sorted(listed - seen_arms.get(exp, set())):
            out.append(f"DESIGN §17: `{exp}` lists arm `{arm}`, which no configs/exp_*.yaml defines")
    return out


def _int(cell: str) -> int | None:
    try:
        return int(cell.strip().strip("`"))
    except ValueError:
        return None


# --- (d) data model -------------------------------------------------------------------------------------------
def _declaration(block: str, table: str) -> str | None:
    """The text of ``table(...)`` in the schema block, balanced parentheses included."""
    m = re.search(rf"^\s*{re.escape(table)}\(", block, re.MULTILINE)
    if m is None:
        return None
    depth, i = 0, m.end() - 1
    while i < len(block):
        if block[i] == "(":
            depth += 1
        elif block[i] == ")":
            depth -= 1
            if depth == 0:
                return block[m.end(): i]
        i += 1
    return block[m.end():]


def check_data_model(design: str) -> list[str]:
    from void.db import Database

    s3 = section(design, "3")
    blocks = fenced_blocks(s3)
    if not blocks:
        return ["DESIGN §3: no schema block"]
    block = blocks[0]
    out: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "world.db")
        try:
            for table in db.tables():
                if table.startswith("sqlite_"):
                    continue
                decl = _declaration(block, table)
                if decl is None:
                    out.append(f"DESIGN §3: table `{table}` is not declared")
                    continue
                for col in db.columns(table):
                    if not re.search(rf"\b{re.escape(col)}\b", decl):
                        out.append(f"DESIGN §3: column `{table}.{col}` is not declared")
        finally:
            db.close()
    return out


# --- (e) CSP, (f) paths, (g) model identifiers -----------------------------------------------------------------
def check_csp(design: str) -> list[str]:
    from void.server.auth import CSP

    quoted = [s for s in code_spans(section(design, "15")) if s.startswith("default-src")]
    if not quoted:
        return ["DESIGN §15: no CSP quoted (a code span starting with default-src)"]
    return [f"DESIGN §15: CSP `{q}` differs from void.server.auth.CSP `{CSP}`" for q in quoted if q != CSP]


def check_paths(design: str) -> list[str]:
    out: list[str] = []
    for span in dict.fromkeys(code_spans(design)):
        if not span.startswith(_PATH_PREFIXES) or any(c in span for c in "*<>{} "):
            continue
        if not (REPO / span.rstrip("/")).exists():
            out.append(f"DESIGN: `{span}` does not exist")
    return out


def check_model_ids(name: str, text: str) -> list[str]:
    out = []
    for n, line in enumerate(text.splitlines(), start=1):
        for m in MODEL_ID_RE.finditer(line):
            out.append(f"{name}:{n}: model identifier `{m.group(0)}` (refer to tiers by rung name)")
    return out


def check_all() -> list[str]:
    design = DESIGN.read_text(encoding="utf-8")
    plan = PLAN.read_text(encoding="utf-8")
    return (check_config(design) + check_layout(design) + check_experiments(design) + check_data_model(design)
            + check_csp(design) + check_paths(design)
            + check_model_ids("docs/DESIGN.md", design) + check_model_ids("docs/PLAN.md", plan))


def main() -> int:
    problems = check_all()
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
