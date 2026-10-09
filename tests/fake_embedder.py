"""Deterministic stand-in for BgeM3Embedder in unit tests: no model, tiny vectors."""

from __future__ import annotations

import hashlib
import math
import re

from app.retrieval.embedder import Embedding, EmbeddingConfig

FAKE_CONFIG = EmbeddingConfig(model_id="fake/bag-of-words", revision="fake-1", dense_dim=16)


def _bucket(word: str, size: int) -> int:
    return int(hashlib.sha256(word.encode()).hexdigest(), 16) % size


class FakeEmbedder:
    def __init__(self, config: EmbeddingConfig = FAKE_CONFIG):
        self.config = config
        self.encoded_texts = 0

    def _one(self, text: str) -> Embedding:
        words = re.findall(r"\w+", text.lower())
        dense = [0.0] * self.config.dense_dim
        sparse: dict[int, float] = {}
        for word in words:
            dense[_bucket(word, self.config.dense_dim)] += 1.0
            token = _bucket(word, 50_000)
            sparse[token] = sparse.get(token, 0.0) + 1.0
        norm = math.sqrt(sum(x * x for x in dense)) or 1.0
        return Embedding(dense=[x / norm for x in dense] if any(dense) else [1.0] + [0.0] * (len(dense) - 1),
                         sparse=sparse)

    def encode(self, texts: list[str]) -> list[Embedding]:
        self.encoded_texts += len(texts)
        return [self._one(t) for t in texts]

    def encode_query(self, text: str) -> Embedding:
        return self._one(text)
