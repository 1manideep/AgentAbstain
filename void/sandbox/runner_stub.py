"""Executed *inside* the sandbox. Loads a gadget with curated builtins and writes result.json.

argv: <seed> <mode: verify|run> <params-json>
Files in /work: gadget.py, tests.py (verify only). Output: /work/result.json, out.txt, err.txt.
"""

import json
import random
import sys
import traceback
import types

ALLOWED = ("math", "random", "statistics", "itertools", "functools", "json", "re", "collections", "dataclasses", "typing")
MAX_OUT = 8192


def main() -> None:
    seed = int(sys.argv[1])
    mode = sys.argv[2]
    params = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    result_fh = open("/work/result.json", "w")  # opened before any gadget code runs
    out_fh = open("/work/out.txt", "w")
    err_fh = open("/work/err.txt", "w")
    sys.stdout = out_fh
    sys.stderr = err_fh
    random.seed(seed)
    real_modules = {name: __import__(name) for name in ALLOWED}
    real_import = __import__
    gadget_mod = types.ModuleType("gadget")

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level != 0:
            raise ImportError("relative imports are not allowed")
        if name == "gadget":
            return gadget_mod
        if name in real_modules:
            return real_modules[name]
        raise ImportError(f"import of {name!r} is not allowed in the void")

    import builtins as _b

    safe_builtins = {
        k: getattr(_b, k)
        for k in (
            "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "frozenset", "int",
            "len", "list", "map", "max", "min", "pow", "print", "range", "repr", "reversed", "round", "set",
            "slice", "sorted", "str", "sum", "tuple", "zip", "True", "False", "None", "Exception", "ValueError",
            "TypeError", "KeyError", "IndexError", "ZeroDivisionError", "AssertionError", "StopIteration",
            "RuntimeError", "ArithmeticError", "LookupError", "NotImplementedError", "isinstance",
        )
        if hasattr(_b, k)
    }
    safe_builtins["__import__"] = safe_import
    safe_builtins["__build_class__"] = _b.__build_class__

    def finish(payload):
        out_fh.flush()
        err_fh.flush()
        json.dump(payload, result_fh)
        result_fh.close()

    try:
        with open("/work/gadget.py") as f:
            code = f.read()
        ns = {"__builtins__": safe_builtins, "__name__": "gadget"}
        exec(compile(code, "gadget.py", "exec"), ns)  # noqa: S102 - this is the sandbox's purpose
        for k, v in ns.items():
            if not k.startswith("__"):
                setattr(gadget_mod, k, v)
        if mode == "verify":
            describe = ns.get("describe")
            run = ns.get("run")
            if not callable(describe) or not callable(run):
                finish({"ok": False, "stage": "contract", "error": "gadget must define describe() and run(params)"})
                return
            desc = describe()
            with open("/work/tests.py") as f:
                tests = f.read()
            tns = {"__builtins__": safe_builtins, "__name__": "tests"}
            try:
                exec(compile(tests, "tests.py", "exec"), tns)  # noqa: S102
            except AssertionError as e:
                finish({"ok": False, "stage": "tests", "error": f"assertion failed: {e}", "describe": desc})
                return
            except Exception as e:  # noqa: BLE001
                finish({"ok": False, "stage": "tests", "error": f"{type(e).__name__}: {e}", "describe": desc})
                return
            finish({"ok": True, "stage": "verified", "describe": desc})
        else:
            run = ns.get("run")
            if not callable(run):
                finish({"ok": False, "stage": "contract", "error": "no run()"})
                return
            value = run(dict(params))
            finish({"ok": True, "stage": "run", "run": value})
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()[-800:]
        finish({"ok": False, "stage": "exception", "error": f"{type(e).__name__}: {e}", "trace": tb})
    finally:
        del real_import


if __name__ == "__main__":
    main()
