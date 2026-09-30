"""Deterministic, dependency-free text embeddings.

Feature hashing of lowercased word unigrams and bigrams into a fixed-size signed
vector, L2-normalised. This is deliberately simple: it is stable across machines,
needs no model download and no network, and is good enough to pick *entry points*
into the memory graph. Real semantic search belongs behind the :class:`Embedder`
protocol (a Voyage or local model can be dropped in without touching retrieval).
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from typing import Protocol

__all__ = ["Embedder", "HashingEmbedder", "cosine", "pack", "unpack", "tokenize"]

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_STOP = frozenset(
    "a an the and or but if of to in on at by for with from as is are was were be been being "
    "it its this that these those i you he she we they me him her us them my your his our their "
    "not no so do does did have has had will would can could should may might".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP]


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    def __init__(self, dim: int = 256) -> None:
        if dim < 8:
            raise ValueError("embedding dim must be >= 8")
        self.dim = dim

    def _bucket(self, feature: str) -> tuple[int, float]:
        h = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "big") % self.dim
        sign = 1.0 if h[4] & 1 else -1.0
        return idx, sign

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        toks = tokenize(text)
        feats = list(toks) + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]
        for f in feats:
            idx, sign = self._bucket(f)
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("dimension mismatch")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))
