"""Embedding providers.

- `hashing`: deterministic signed feature-hashing of words and word bigrams. Lexical, NOT semantic;
  it exists so local/demo installs work offline and is labelled `hashing-v1` wherever stored.
- `openai_compatible`: any /v1/embeddings-compatible endpoint (OpenAI, Azure, vLLM, TEI, Bedrock proxy).
"""

from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

import httpx

from app.core.config import get_settings
from app.core.errors import ServiceUnavailable

_TOKEN = re.compile(r"[a-z0-9₹]+(?:[.\-][a-z0-9]+)*")


class Embedder(Protocol):
    model_name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    model_name = "hashing-v1"

    def __init__(self, dim: int):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        toks = _TOKEN.findall(text.lower())
        feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]
        v = [0.0] * self.dim
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_cached_hash_vec(t, self.dim) for t in texts]


@lru_cache(maxsize=20000)
def _cached_hash_vec(text: str, dim: int) -> list[float]:
    return HashingEmbedder(dim)._vec(text)


class OpenAICompatibleEmbedder:
    def __init__(self) -> None:
        s = get_settings()
        if not s.embedding_api_url or not s.embedding_model:
            raise ServiceUnavailable("Embedding API URL/model not configured.")
        self.url = s.embedding_api_url.rstrip("/") + "/embeddings"
        self.model_name = s.embedding_model
        self.dim = s.embedding_dim
        self.headers = {"Authorization": f"Bearer {s.embedding_api_key.get_secret_value()}"} if s.embedding_api_key else {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        with httpx.Client(timeout=60) as client:
            for i in range(0, len(texts), 64):
                batch = texts[i : i + 64]
                r = client.post(self.url, json={"model": self.model_name, "input": batch, "dimensions": self.dim}, headers=self.headers)
                r.raise_for_status()
                out.extend(d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"]))
        return out


@lru_cache
def get_embedder() -> Embedder:
    s = get_settings()
    if s.embedding_provider == "openai_compatible":
        return OpenAICompatibleEmbedder()
    return HashingEmbedder(s.embedding_dim)
