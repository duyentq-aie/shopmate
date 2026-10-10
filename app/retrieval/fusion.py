"""Client-side Reciprocal Rank Fusion (Phase 4).

Why client-side: Qdrant breaks exact score ties in an arbitrary order that differs for every build and
every `limit`, and its server-side RRF turns that order into ranks. The retriever therefore fetches every
branch whole, orders it itself by (score desc, stable key asc) (app/retrieval/search.py) and fuses here.
No score is ever modified.

The formula is Qdrant's own, measured on the real server (1.19.1, all 38 dev queries, scores equal to
1e-6): score(doc) = sum over the branches that rank it inside the window of 1 / (rank0 + 2), rank0 = 0 for
the best. tests/test_retrieval_integration.py re-checks it against the server on a fixed fixture, so an
upgrade that changes the formula is caught. Scores are exact fractions: mathematically equal sums (e.g.
1/2 + 1/6 and 1/3 + 1/3) are exact ties, to be broken by stable key, not by float rounding.
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction

RRF_OFFSET = 2  # 1 / (k + rank) with k = 1 and 1-based ranks


def rrf_fuse(rankings: Sequence[Sequence[str]], window: int = 50) -> dict[str, Fraction]:
    """Fuse already ordered rankings (best first). Only the first `window` documents of a ranking count."""
    fused: dict[str, Fraction] = {}
    for ranking in rankings:
        seen: set[str] = set()
        for rank0, doc_id in enumerate(ranking[:window]):
            if doc_id in seen:
                raise ValueError(f"document {doc_id!r} appears twice in one ranking")
            seen.add(doc_id)
            fused[doc_id] = fused.get(doc_id, Fraction(0)) + Fraction(1, rank0 + RRF_OFFSET)
    return fused
