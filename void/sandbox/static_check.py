"""Allow-list static checker for gadget code and tests (DESIGN §12 step 3).

This is an anti-footgun filter, not the security boundary (the namespace sandbox is). It
rejects anything outside a small, explicitly enumerated subset of Python: a fixed set of
AST node types, imports only from an allow-list of modules, attribute access on those
modules only through an explicit ``(module, attr)`` table, no dunder or underscore
attributes, no subscripts on module objects, and a denied set of builtin names that also
applies to attribute names (so ``x.exec`` and ``getattr`` are both rejected).
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field

__all__ = ["check", "CheckResult", "ALLOWED_MODULES", "DENIED_NAMES"]

_MATH_PUBLIC = frozenset(n for n in dir(math) if not n.startswith("_"))

ALLOWED_MODULES: dict[str, frozenset[str]] = {
    "math": _MATH_PUBLIC,
    "random": frozenset({"random", "randint", "uniform", "choice", "choices", "shuffle", "seed", "gauss",
                         "sample", "randrange", "triangular", "betavariate", "expovariate", "Random"}),
    "statistics": frozenset({"mean", "median", "mode", "stdev", "pstdev", "variance", "pvariance", "fmean",
                             "geometric_mean", "harmonic_mean", "quantiles", "median_low", "median_high",
                             "multimode", "correlation", "covariance"}),
    "itertools": frozenset({"chain", "product", "permutations", "combinations", "accumulate", "count", "cycle",
                            "repeat", "groupby", "islice", "starmap", "takewhile", "dropwhile", "zip_longest",
                            "tee", "pairwise", "compress", "filterfalse", "combinations_with_replacement"}),
    "functools": frozenset({"reduce", "partial", "lru_cache", "cache", "cmp_to_key", "total_ordering"}),
    "json": frozenset({"dumps", "loads"}),
    "re": frozenset({"compile", "match", "search", "findall", "sub", "fullmatch", "split", "escape", "finditer",
                     "IGNORECASE", "MULTILINE", "DOTALL"}),
    "collections": frozenset({"Counter", "defaultdict", "deque", "namedtuple", "OrderedDict"}),
    "dataclasses": frozenset({"dataclass", "field", "asdict", "replace"}),
    "typing": frozenset({"Any", "Optional", "Union", "List", "Dict", "Tuple", "Set", "Callable", "Iterable",
                         "Iterator", "Sequence", "Mapping", "TypeVar", "Generic", "Protocol", "NamedTuple",
                         "cast", "Literal", "Final"}),
}

DENIED_NAMES = frozenset({
    "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "dir", "getattr", "setattr",
    "delattr", "type", "super", "memoryview", "breakpoint", "input", "help", "exit", "quit", "__builtins__",
    "__loader__", "__spec__", "__file__", "__name__", "__class__", "__subclasses__", "__dict__", "__globals__",
    "__code__", "__closure__", "__mro__", "__bases__", "__base__", "__reduce__", "__reduce_ex__", "__getattribute__",
    "__setattr__", "__delattr__", "__import__", "builtins", "sys", "os", "subprocess", "importlib", "socket",
    "ctypes", "pickle", "marshal", "shutil", "pathlib", "io", "signal", "threading", "multiprocessing",
    "classmethod", "staticmethod", "property", "object", "bytearray", "bytes", "id", "hash", "iter", "next",
    "callable", "isinstance", "issubclass", "hasattr",
})
# `isinstance`/`hasattr`/`callable` are denied because they are common building blocks of
# introspection chains; a gadget that needs them is not a gadget the void needs.

ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Module, ast.FunctionDef, ast.Return, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr, ast.Call,
    ast.Name, ast.Attribute, ast.Constant, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.If, ast.For,
    ast.While, ast.Break, ast.Continue, ast.Pass, ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Subscript,
    ast.Slice, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.comprehension, ast.Lambda,
    ast.arguments, ast.arg, ast.keyword, ast.Load, ast.Store, ast.Del, ast.IfExp, ast.Assert, ast.Raise,
    ast.Try, ast.ExceptHandler, ast.JoinedStr, ast.FormattedValue, ast.Starred, ast.Import, ast.ImportFrom,
    ast.ClassDef, ast.Yield, ast.YieldFrom, ast.Delete,
    # operators
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.LShift, ast.RShift, ast.BitOr,
    ast.BitXor, ast.BitAnd, ast.MatMult, ast.Invert, ast.Not, ast.UAdd, ast.USub, ast.And, ast.Or, ast.Eq,
    ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn,
)


@dataclass
class CheckResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


class _Checker(ast.NodeVisitor):
    def __init__(self, *, allow_gadget: bool = False) -> None:
        self.errors: list[str] = []
        self.module_aliases: dict[str, str] = {}  # local name -> module
        self.imports: list[str] = []
        self.allow_gadget = allow_gadget
        self.gadget_aliases: set[str] = set()

    def err(self, node: ast.AST, msg: str) -> None:
        line = getattr(node, "lineno", "?")
        self.errors.append(f"line {line}: {msg}")

    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, ALLOWED_NODES):
            self.err(node, f"disallowed syntax: {type(node).__name__}")
            return
        super().generic_visit(node)

    # imports --------------------------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "gadget" and self.allow_gadget:
                self.gadget_aliases.add(alias.asname or alias.name)
                self.imports.append("gadget")
                continue
            if alias.name not in ALLOWED_MODULES:
                self.err(node, f"import of {alias.name!r} is not allowed")
                continue
            self.module_aliases[alias.asname or alias.name] = alias.name
            self.imports.append(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or node.module is None:
            self.err(node, "relative imports are not allowed")
            return
        if node.module not in ALLOWED_MODULES:
            self.err(node, f"import from {node.module!r} is not allowed")
            return
        table = ALLOWED_MODULES[node.module]
        for alias in node.names:
            if alias.name == "*":
                self.err(node, "star imports are not allowed")
            elif alias.name not in table:
                self.err(node, f"{node.module}.{alias.name} is not in the allow-list")
            else:
                self.imports.append(f"{node.module}.{alias.name}")
                local = alias.asname or alias.name
                if local in DENIED_NAMES:
                    self.err(node, f"import binds denied name {local!r}")

    # names and attributes --------------------------------------------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        if node.id in DENIED_NAMES or node.id.startswith("__"):
            self.err(node, f"use of denied name {node.id!r}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        attr = node.attr
        if attr.startswith("_"):
            self.err(node, f"underscore attribute {attr!r} is not allowed")
        elif attr in DENIED_NAMES:
            self.err(node, f"denied attribute {attr!r}")
        if isinstance(node.value, ast.Name) and node.value.id in self.module_aliases:
            module = self.module_aliases[node.value.id]
            if attr not in ALLOWED_MODULES[module]:
                self.err(node, f"{module}.{attr} is not in the allow-list")
        elif isinstance(node.value, ast.Name) and node.value.id in self.gadget_aliases:
            pass  # any public attribute of the gadget module is fine in tests
        elif isinstance(node.value, ast.Attribute) and self._is_module_chain(node.value):
            self.err(node, "nested module attribute chains are not allowed")
        self.visit(node.value)

    def _is_module_chain(self, node: ast.AST) -> bool:
        while isinstance(node, ast.Attribute):
            node = node.value
        return isinstance(node, ast.Name) and node.id in self.module_aliases

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.value, ast.Name) and node.value.id in self.module_aliases:
            self.err(node, "subscript on a module object is not allowed")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.decorator_list:
            self.err(node, "class decorators are not allowed")
        for base in node.bases:
            if not (isinstance(base, ast.Name) and base.id == "object") and not (
                isinstance(base, ast.Name) and base.id == "Exception"
            ):
                self.err(node, "class bases other than object/Exception are not allowed")
        if node.keywords:
            self.err(node, "class keywords are not allowed")
        for stmt in node.body:
            self.visit(stmt)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for dec in node.decorator_list:
            ok = (isinstance(dec, ast.Name) and dec.id in {"dataclass", "lru_cache", "cache"}) or (
                isinstance(dec, ast.Attribute) and dec.attr in {"dataclass", "lru_cache", "cache"}
            ) or isinstance(dec, ast.Call)
            if not ok:
                self.err(node, "unsupported decorator")
        if node.name in DENIED_NAMES or node.name.startswith("_"):
            self.err(node, f"function name {node.name!r} is not allowed")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)


def check(source: str, *, max_bytes: int = 8000, allow_gadget: bool = False) -> CheckResult:
    """Check gadget source (``allow_gadget=False``) or its tests (``allow_gadget=True``)."""
    if len(source.encode("utf-8")) > max_bytes:
        return CheckResult(False, [f"source exceeds {max_bytes} bytes"])
    if "\x00" in source:
        return CheckResult(False, ["null byte in source"])
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as e:
        return CheckResult(False, [f"syntax error: {e.msg} (line {e.lineno})"])
    checker = _Checker(allow_gadget=allow_gadget)
    checker.visit(tree)
    return CheckResult(not checker.errors, checker.errors, checker.imports)
