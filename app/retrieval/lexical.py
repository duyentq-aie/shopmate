"""The folded lexical vector (Phase 4, experiment 1): BM25-style term weights on diacritic-folded syllables.

Stored as a NEW named sparse vector `folded` next to the untouched bge-m3 `dense` and `sparse`
vectors. It is a function of a document's text, the LexicalConfig below and, only for the
deterministic tie-break (see below), the document's rank among all documents; the indexer stores
both the config fingerprint and that rank per point, so it rewrites exactly the vectors that went stale.

- document side: per folded token  tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avg_len)),
  dl = number of folded tokens of the document;
- query side: 1.0 for every distinct folded token;
- IDF: applied by Qdrant at query time (the vector is created with modifier=idf), so the score of
  a document is the BM25 sum over the query's tokens. IDF is the only corpus statistic BM25 uses,
  and Qdrant keeps it, not us.

k1 and b are the textbook defaults and were NOT tuned on the dev split. avg_len is pinned (not
measured per run) so the BM25 weights never depend on the other documents; 70.9 is the mean number of
folded tokens of the 269 indexed documents (products 70.7, policies 73.8) when the experiment began.

Deterministic tie-break (decided 2026-10-10 after measuring Qdrant): documents of equal length with the
same matching tokens get EXACTLY equal scores, and Qdrant orders exact ties arbitrarily, differently
for every build and every `limit` (not by insertion order; measured on the real server). So the folded
weights of the document ranked r of n by the R16 stable key are scaled by 1 - tie_delta * r / n: an
exact tie now resolves to the smaller stable key, independently of Qdrant. tie_delta = 1e-3 moves a
score by at most 0.1% (a pair is only reordered if its pure BM25 scores differ by less than that),
and the relative gap between neighbouring ranks, tie_delta / n, must stay above float32 noise
(check_tie_resolution; n <= ~1000 at tie_delta = 1e-3). Part of the config, hence of the fingerprint.

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


MIN_TIE_GAP = 1e-6  # smallest relative gap between neighbouring ranks that float32 scoring resolves reliably


class TokenIdCollision(Exception):
    """Two distinct folded tokens hash to the same sparse index."""


class TieBreakResolution(Exception):
    """tie_delta / n is too small for float32 scores to keep neighbouring ranks apart."""


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
    tie_break: str = "stable-key-rank"
    tie_delta: float = 1e-3

    def __post_init__(self) -> None:
        if self.k1 < 0 or not 0.0 <= self.b <= 1.0 or self.avg_len <= 0 or not 0.0 <= self.tie_delta < 1.0:
            raise ValueError(f"invalid lexical parameters: k1={self.k1}, b={self.b}, avg_len={self.avg_len}, "
                             f"tie_delta={self.tie_delta}")

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


def tie_factor(rank: int, n: int, config: LexicalConfig = LexicalConfig()) -> float:
    """Scale applied to the folded weights of the document ranked `rank` (0-based) of `n` by stable key."""
    return 1.0 - config.tie_delta * rank / n


def check_tie_resolution(n: int, config: LexicalConfig = LexicalConfig()) -> None:
    gap = config.tie_delta / n
    if config.tie_delta > 0 and gap < MIN_TIE_GAP:
        raise TieBreakResolution(f"tie_delta {config.tie_delta} over {n} documents leaves a relative gap of {gap:.1e} "
                                 f"between neighbouring ranks, below {MIN_TIE_GAP:.0e}; raise tie_delta")


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
