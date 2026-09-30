"""Sortable, deterministic identifiers.

IDs must not depend on wall-clock time (that would break run reproducibility), so they
are derived from a run-scoped counter plus a short hash. Format: ``<prefix>_<counter:08d><hash6>``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

__all__ = ["IdFactory", "slugify"]


class IdFactory:
    def __init__(self, run_id: str, start: int = 0, persist: Callable[[int], None] | None = None) -> None:
        self.run_id = run_id
        self._count = int(start)
        self._persist = persist

    def new(self, prefix: str) -> str:
        self._count += 1
        if self._persist is not None:
            self._persist(self._count)  # durable with whatever row consumes the id
        digest = hashlib.blake2b(f"{self.run_id}:{prefix}:{self._count}".encode(), digest_size=3).hexdigest()
        return f"{prefix}_{self._count:08d}{digest}"

    @property
    def count(self) -> int:
        """Number of ids handed out so far (persisted in ``world_kv`` as ``id_counter``)."""
        return self._count

    def restore(self, count: int) -> None:
        self._count = int(count)


def slugify(text: str, max_len: int = 48) -> str:
    out: list[str] = []
    prev_dash = False
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")[:max_len].strip("-")
    return slug or "note"
