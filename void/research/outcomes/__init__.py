"""Registry of research outcomes defined outside :mod:`void.research.analysis`.

An outcome is a function ``(run_dir, arg) -> float | None`` over one run directory; ``arg`` is whatever followed
the first dot of the outcome path (``"my_outcome.alpha"`` calls it with ``"alpha"``), or None. A submodule of
this package adds one by decorating it::

    from void.research.analysis import _mean, connect
    from void.research.outcomes import register

    def _my_outcome_on_day(run_dir, arg, day):
        ...

    @register("my_outcome", daily=_my_outcome_on_day)
    def my_outcome(run_dir, arg):
        with connect(run_dir) as db:
            ...

Submodules import their helpers (``connect``, ``run_config``, ``load_metrics``, ``resolve_path``, ``_mean``) from
:mod:`void.research.analysis`, which never imports this package at module level, so there is no import cycle.

:func:`void.research.analysis.end_of_run_outcome` calls :func:`load_all` and then resolves a bare name against the
built-in ``COMPUTED`` table first, then against :data:`OUTCOMES`, then against the metrics rows. A ``daily``
function (``(run_dir, arg, day) -> float | None``) is the per-day version that ``compare`` draws its daily curves
from: :func:`load_all` copies every one into ``void.research.analysis.DAILY`` in place, so the comparison picks
new outcomes up without being edited. A built-in name can never be registered (it would be silently shadowed).
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from pathlib import Path

__all__ = ["DAILY_OUTCOMES", "OUTCOMES", "DailyOutcome", "Outcome", "load_all", "register", "unregister"]

Outcome = Callable[[str | Path, str | None], float | None]
DailyOutcome = Callable[[str | Path, str | None, int], float | None]

OUTCOMES: dict[str, Outcome] = {}
DAILY_OUTCOMES: dict[str, DailyOutcome] = {}

_loaded = False


def _builtin_names() -> set[str]:
    from void.research.analysis import COMPUTED  # lazy: analysis imports this package lazily too

    return set(COMPUTED)


def register(name: str, *, daily: DailyOutcome | None = None) -> Callable[[Outcome], Outcome]:
    """Decorator: register ``fn`` as the end-of-run outcome ``name`` (and ``daily`` as its per-day version).

    ``name`` is a bare identifier (no dots), not ``tick`` or ``day``. Registering a name the built-in table already
    holds, or a second function under a name already taken, raises ``ValueError``; the same function again is a no-op.
    """
    if not name.isidentifier() or name in ("tick", "day"):
        raise ValueError(f"outcome name {name!r} must be an identifier (no dots) other than 'tick' and 'day'")

    def deco(fn: Outcome) -> Outcome:
        if name in _builtin_names():
            raise ValueError(f"outcome {name!r} is built into void.research.analysis.COMPUTED")
        if OUTCOMES.get(name, fn) is not fn:
            raise ValueError(f"outcome {name!r} is already registered by {OUTCOMES[name].__module__}")
        OUTCOMES[name] = fn
        if daily is not None:
            DAILY_OUTCOMES[name] = daily
        return fn

    return deco


def unregister(name: str) -> None:
    """Remove ``name`` from the registry and from ``analysis.DAILY`` (for tests and interactive use)."""
    from void.research.analysis import DAILY

    OUTCOMES.pop(name, None)
    daily = DAILY_OUTCOMES.pop(name, None)
    if daily is not None and DAILY.get(name) is daily:
        del DAILY[name]


def load_all() -> dict[str, Outcome]:
    """Import every submodule of this package once (``pkgutil.iter_modules``, sorted, ``_private`` ones skipped),
    then copy the registered daily functions into ``void.research.analysis.DAILY`` in place. Returns
    :data:`OUTCOMES`. Cheap after the first call; an import error propagates (a broken outcome module is loud)."""
    global _loaded
    if not _loaded:
        _loaded = True  # set first: a submodule that resolves an outcome at import time must not recurse
        try:
            for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
                if not info.name.startswith("_"):
                    importlib.import_module(f"{__name__}.{info.name}")
        except BaseException:
            _loaded = False
            raise
    from void.research.analysis import DAILY

    for name, fn in DAILY_OUTCOMES.items():
        DAILY.setdefault(name, fn)
    return OUTCOMES
