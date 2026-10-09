"""The folded lexical vector (Phase 4, experiment 1): BM25-style term weights on diacritic-folded syllables.

Stored as a NEW named sparse vector `folded` next to the untouched bge-m3 `dense` and `sparse`
vectors. It is a pure function of a document's text and the LexicalConfig below, so the indexer's
skip rule (hash of the text + a fingerprint) works for it exactly as for the model embeddings.

- document side: per folded token  tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avg_len)),
  dl = number of folded tokens of the document;
- query side: 1.0 for every distinct folded token;
- IDF: applied by Qdrant at query time (the vector is created with modifier=idf), so the score of
  a document is the BM25 sum over the query's tokens. Nothing corpus-dependent is stored.

k1 and b are the textbook defaults and were NOT tuned on the dev split. avg_len is pinned (not
measured per run) so a vector never depends on the other documents; 70.9 is the mean number of
folded tokens of the 269 indexed documents (products 70.7, policies 73.8) when the experiment began.

Token ids: the first 4 bytes of blake2b(token) as an unsigned 32-bit int (Qdrant's sparse index
range). The 489 distinct tokens of the corpus have no collisions; the indexer verifies that for
every build (check_no_collisions) instead of assuming it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from app.retrieval.folding import FOLDING_VERSION, tokenize


class TokenIdCollision(Exception):
    """Two distinct folded tokens hash to the same sparse index."""


@dataclass(frozen=True)
class LexicalConfig:
    folding_version: int = FOLDING_VERSION
    tokenizer: str = "alnum-syllable"
    token_id: str = "blake2b-32"
    weighting: str = "bm25-tf"
    idf: str = "qdrant-modifier"
    k1: float = 1.2
    b: float = 0.75
    avg_len: float = 70.9

    def __post_init__(self) -> None:
        if self.k1 < 0 or not 0.0 <= self.b <= 1.0 or self.avg_len <= 0:
            raise ValueError(f"invalid lexical parameters: k1={self.k1}, b={self.b}, avg_len={self.avg_len}")

    def fingerprint(self) -> str:
        """Stable hash of every setting that affects the stored vectors."""
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def token_id(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest(), "big")


def check_no_collisions(tokens: Iterable[str]) -> None:
    seen: dict[int, str] = {}
    for token in sorted(set(tokens)):
        other = seen.setdefault(token_id(token), token)
        if other != token:
            raise TokenIdCollision(f"tokens {other!r} and {token!r} share sparse index {token_id(token)}")


def doc_vector(text: str, config: LexicalConfig = LexicalConfig()) -> dict[int, float]:
    tokens = tokenize(text)
    if not tokens:
        return {}
    norm = 1 - config.b + config.b * len(tokens) / config.avg_len
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return {token_id(t): tf * (config.k1 + 1) / (tf + config.k1 * norm) for t, tf in counts.items()}


def query_vector(text: str) -> dict[int, float]:
    return {token_id(t): 1.0 for t in set(tokenize(text))}
