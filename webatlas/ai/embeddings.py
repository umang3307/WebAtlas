"""Embeddings: a model-backed embedder when one is configured, a built-in offline one otherwise.

The offline embedder is a hashing bag-of-words (the "hashing trick"): deterministic, dependency-
free, and good enough for keyword-ish semantic search over a site's documents. It means WebAtlas's
document search works with no model, no network and no API key. When a real embedding model is
reachable it's used instead, for better quality.

Vectors are stored as packed float32 bytes; similarity is cosine, in pure Python (fine for the
thousands of chunks one site produces).
"""

from __future__ import annotations

import hashlib
import math
import re
from array import array

from webatlas.ai.client import LLMClient, LLMError

OFFLINE_DIM = 512
_TOKEN = re.compile(r"[a-z0-9]{2,}")

# Common words carry little signal and, shared across every document, add noise to cosine
# similarity. Dropping them sharpens offline keyword retrieval.
_STOP = frozenset((    # noqa: SIM905 — a prose list reads better than a 100-item literal
    "the a an and or but if then of to in on at for with by from as is are was were be been being "
    "this that these those it its they them their there here we our you your i he she his her "
    "how what when where which who whom why do does did done has have had will would shall should "
    "can could may might must not no yes so than too very much many more most some any all each "
    "about over under out up down off again further once such own same other also"
).split())


_SUFFIXES = ("ingly", "edly", "ing", "ies", "ied", "ment", "ness", "tion", "sion",
             "ed", "es", "ly", "s")


def _stem(tok: str) -> str:
    """A crude suffix stripper so 'roads'/'road' and 'allocated'/'allocates' land together.
    Not linguistically correct — just enough to lift offline keyword recall."""
    if len(tok) <= 4:
        return tok
    for suf in _SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def _stable_hash(token: str) -> int:
    # Independent of PYTHONHASHSEED, so vectors made in one process match those made in another
    # (indexing and querying, across restarts).
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")


def hashing_embed(text: str, dim: int = OFFLINE_DIM) -> list[float]:
    """A normalized hashing bag-of-words vector. No model needed, deterministic across runs."""
    vec = [0.0] * dim
    for tok in _tokens(text):
        vec[_stable_hash(tok) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    return max(-1.0, min(1.0, dot))   # both sides are stored normalized


def pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def unpack(blob: bytes) -> list[float]:
    v = array("f")
    v.frombytes(blob)
    return list(v)


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


class Embedder:
    """Uses the LLM client's embedding endpoint when it works; otherwise the offline embedder.
    Falls back per batch, so a model that goes away mid-run doesn't break indexing."""

    def __init__(self, client: LLMClient | None = None, force_offline: bool = False):
        self.client = client
        self.force_offline = force_offline or client is None
        self.using_model = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.force_offline:
            try:
                vectors = self.client.embed(texts)
                if vectors and len(vectors) == len(texts):
                    self.using_model = True
                    return [_normalize(v) for v in vectors]
            except LLMError:
                pass   # fall through to offline
        self.using_model = False
        return [hashing_embed(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
