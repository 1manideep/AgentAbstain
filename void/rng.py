"""Seeded, named random streams.

Every random draw in the simulation goes through :func:`stream` so that a run is a
pure function of ``(config hash, seed)``. Streams are keyed by a name and any number
of hashable parts (agent id, tick, ...); the same key always yields the same
``random.Random`` state, independent of call order elsewhere in the program.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

__all__ = ["RNG", "derive_seed"]


def derive_seed(root_seed: int, *parts: Any) -> int:
    """Derive a 64-bit seed from a root seed and hashable parts (stable across processes)."""
    h = hashlib.blake2b(digest_size=8)
    h.update(str(root_seed).encode())
    for part in parts:
        h.update(b"\x1f")
        h.update(repr(part).encode())
    return int.from_bytes(h.digest(), "big")


class RNG:
    """Factory of deterministic ``random.Random`` instances keyed by stream name and parts."""

    def __init__(self, root_seed: int) -> None:
        self.root_seed = int(root_seed)

    def stream(self, name: str, *parts: Any) -> random.Random:
        return random.Random(derive_seed(self.root_seed, name, *parts))

    def uniform(self, name: str, *parts: Any) -> float:
        return self.stream(name, *parts).random()

    def child(self, name: str, *parts: Any) -> RNG:
        """A nested RNG whose root is derived from this one (for subsystems with many draws)."""
        return RNG(derive_seed(self.root_seed, name, *parts))
