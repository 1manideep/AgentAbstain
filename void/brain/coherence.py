"""Output coherence: a cheap, model-agnostic degeneration measure.

Applied to the raw text a brain produced (the JSON decision plus any free text). Three
signals, combined by geometric mean so any one collapsing drags the score down:

* distinct-2-gram ratio (repetition collapse drives it toward 0),
* 1 - longest-run fraction (a single token repeated over half the output scores ~0),
* length sanity (near-empty or absurdly long outputs are penalised).

It is also the *inverse* operator used to garble text when the entropy model decides an
agent degenerates this tick, so what we induce is what we measure.
"""

from __future__ import annotations

import math
import random
import re

__all__ = ["score", "degrade", "tokens"]

_WORD = re.compile(r"[A-Za-z0-9_']+")


def tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _distinct_bigram_ratio(toks: list[str]) -> float:
    if len(toks) < 2:
        return 1.0
    bigrams = list(zip(toks, toks[1:], strict=False))
    return len(set(bigrams)) / len(bigrams)


def _longest_run_fraction(toks: list[str]) -> float:
    if not toks:
        return 0.0
    best = run = 1
    for a, b in zip(toks, toks[1:], strict=False):
        run = run + 1 if a == b else 1
        best = max(best, run)
    return best / len(toks)


def _length_sanity(n: int, lo: int = 3, hi: int = 800) -> float:
    if n <= 0:
        return 0.0
    if n < lo:
        return n / lo
    if n > hi:
        return max(0.0, 1.0 - (n - hi) / hi)
    return 1.0


def score(text: str) -> float:
    toks = tokens(text)
    a = _distinct_bigram_ratio(toks)
    b = 1.0 - _longest_run_fraction(toks)
    c = _length_sanity(len(toks))
    if min(a, b, c) <= 0.0:
        return 0.0
    return float(math.exp((math.log(a) + math.log(b) + math.log(c)) / 3.0))


def degrade(text: str, severity: float, rng: random.Random) -> str:
    """Garble text the way a too-hot sampler does: repeats, stutters, shuffled tails.

    ``severity`` in [0, 1]; 0 returns the text unchanged.
    """
    severity = max(0.0, min(1.0, severity))
    if severity == 0.0 or not text.strip():
        return text
    words = text.split()
    if not words:
        return text
    out: list[str] = []
    for w in words:
        out.append(w)
        r = rng.random()
        if r < severity * 0.5:
            out.extend([w] * (1 + int(rng.random() * 3 * severity)))  # stutter
        elif r < severity * 0.7:
            continue
    cut = max(1, int(len(out) * (1.0 - 0.5 * severity)))
    head, tail = out[:cut], out[cut:]
    rng.shuffle(tail)
    return " ".join(head + tail)
