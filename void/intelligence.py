"""Capability ladder: place a roster on tiers by a normal distribution (one genius, most not).

``intelligence.ladder`` lists tier names from smartest to dumbest. Rung ``k`` (0-based) owns the
z-score band centred at ``(mid - k) * spacing`` with ``mid = (K - 1) / 2``. Agent ``i`` of ``N``
(after a seeded shuffle) receives the mid-quantile ``z_i = mean + sd * Phi^-1((i + 0.5) / N)``, so
the counts follow the bell *exactly* rather than by luck: five rungs and ten agents give
1 / 2 / 4 / 2 / 1. With ``extremes: always`` the highest-z agent is always on rung 0 and the
lowest-z agent always on the last rung, so a small roster still has its one genius and its one
dunce. The shuffle is keyed by the run seed, so which personality is the genius rotates across
seeds and a paired-by-seed design averages personality out of the intelligence effect.

Pure functions only: this module is imported by ``void.config`` and must not import the rest of
the package.
"""

from __future__ import annotations

import random
from statistics import NormalDist

__all__ = ["z_scores", "rung_for", "assign", "counts"]


def z_scores(n: int, mean: float = 0.0, sd: float = 1.0) -> list[float]:
    """Ascending mid-quantile z-scores for ``n`` draws from N(mean, sd)."""
    nd = NormalDist()
    return [mean + sd * nd.inv_cdf((i + 0.5) / n) for i in range(n)]


def rung_for(z: float, k: int, spacing: float = 1.0) -> int:
    """Index of the rung (0 = smartest) whose band contains ``z``."""
    if k <= 1:
        return 0
    mid = (k - 1) / 2
    for j in range(k - 1):
        if z >= (mid - j - 0.5) * spacing:
            return j
    return k - 1


def assign(names: list[str], ladder: list[str], seed: int, *, mean: float = 0.0, sd: float = 1.0,
           spacing: float = 1.0, shuffle: bool = True, extremes: str = "always") -> dict[str, str]:
    """Map every name to a ladder tier. Deterministic in ``(names, ladder, seed)``."""
    n, k = len(names), len(ladder)
    if n == 0 or k == 0:
        return {}
    order = list(range(n))
    if shuffle:
        random.Random(f"{seed}:intelligence").shuffle(order)  # str seeds hash with sha512: stable across processes
    zs = z_scores(n, mean, sd)  # ascending
    out: dict[str, str] = {}
    for rank, idx in enumerate(order):  # rank 0 is the highest z
        z = zs[n - 1 - rank]
        rung = rung_for(z, k, spacing)
        if extremes == "always" and n >= 2 and k >= 2:
            if rank == 0:
                rung = 0
            elif rank == n - 1:
                rung = k - 1
        out[names[idx]] = ladder[rung]
    return out


def counts(assignment: dict[str, str], ladder: list[str]) -> list[tuple[str, list[str]]]:
    """``[(tier, [names...]), ...]`` in ladder order, names in roster order."""
    return [(tier, [name for name, t in assignment.items() if t == tier]) for tier in ladder]
