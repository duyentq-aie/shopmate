"""Baseline retrieval over the ShopMate collection.

search(query, filters, k, mode): `mode=None` runs the configured default, app.config.DEFAULT_RETRIEVAL_MODE
(dense_folded, adopted in Phase 4); the evaluation runners always name their mode.
- mode "dense":  nearest neighbours on the bge-m3 dense vector;
- mode "hybrid": dense and sparse candidates fused with Reciprocal Rank Fusion;
- Phase 4 experiment 1 (layout-v2 collection only), the same fusion with the diacritic-folded
  lexical vector (`folded`) as an extra or replacement branch:
    "dense_sparse_folded": dense + sparse + folded;
    "dense_folded":        dense + folded (the folded branch replaces bge-m3 sparse).
  Every fused mode uses the same fusion, window and ordering, so branches are the only thing that
  differs. A branch with no query weights (no tokens) is dropped.

Fusion is CLIENT-SIDE (decided 2026-10-10, DECISIONS R25). Qdrant breaks exact score ties in an
arbitrary order that differs for every build and every `limit`, and its server-side RRF turns that
order into ranks. So each branch is fetched WHOLE (limit = collection size, so no tie can sit on a
cut-off), ordered by (score desc, stable key asc), cut to the first PREFETCH_LIMIT and fused with
app/retrieval/fusion.rrf_fuse, which reproduces Qdrant's formula exactly. Scores are never modified.
Fetching the whole collection is valid only at this scale (a few hundred points); the scalable
version fetches N and extends the fetch while the N-th and (N+1)-th scores tie.
Filters are translated to a Qdrant filter and applied INSIDE Qdrant, on every prefetch branch and
on the final query, so a result can never violate them. There is deliberately no `keyword`
filter: free-text name matching is the retrieval model's job, not a hard filter (DECISIONS R14).

Deterministic order (Phase 3A bug fix, DECISIONS R16): RRF scores depend only on ranks, so ties
are common, and Qdrant returns tied points in an arbitrary order that changes between calls. The
retriever therefore over-fetches `fetch_limit(k)` fused results, sorts them client-side by
(score desc, stable key asc) and truncates to k. Over-fetching matters: sorting only the top k
cannot fix WHICH of two tied points made it into the list. The stable key is the SKU for products
and "file:clause" for policies. This is a fix, not a ranking choice; smarter tie-breaking (e.g.
preferring the higher dense score) is a Phase 4 candidate.

Deliberately a baseline: no query rewriting, no reranker, no LLM. Vietnamese normalisation exists
only as the folded branch above, where it is the thing being measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from qdrant_client import QdrantClient, models

import app.config as config
from app.retrieval.embedder import Embedding
from app.retrieval.fusion import rrf_fuse
from app.retrieval.lexical import LexicalConfig, query_vector
from app.retrieval.manifest import active_collection
from app.retrieval.store import DENSE, FOLDED, SPARSE, to_sparse_vector
from app.tracing import span

Mode = Literal["dense", "hybrid", "dense_sparse_folded", "dense_folded"]
FUSED_BRANCHES: dict[str, tuple[str, ...]] = {
    "hybrid": (DENSE, SPARSE),
    "dense_sparse_folded": (DENSE, SPARSE, FOLDED),
    "dense_folded": (DENSE, FOLDED),
}
PREFETCH_LIMIT = 50  # window per branch before RRF (documents ranked deeper add nothing); part of the baseline definition
MIN_FETCH = 30  # fused results fetched before the deterministic sort


def fetch_limit(k: int) -> int:
    return max(3 * k, MIN_FETCH)


def stable_key(payload: dict, fallback: str) -> str:
    """Tie-break key: SKU for products, "file:clause" for policies (clause zero-padded so 2 < 10)."""
    if payload.get("doc_type") == "product" and payload.get("sku"):
        return f"product:{payload['sku']}"
    if payload.get("doc_type") == "policy" and payload.get("file") is not None:
        return f"policy:{payload['file']}:{int(payload.get('clause', 0)):04d}"
    return fallback
HIDDEN_PAYLOAD = ("text", "text_sha256", "payload_sha256", "embedding_fingerprint")


class SearchFilters(BaseModel):
    """Structured filters. All set fields must hold (AND)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_type: Literal["product", "policy"] | None = None
    category: str | None = None
    kind: str | None = None  # catalog product kind, e.g. "Áo sơ mi nam"
    color: str | None = None
    material: str | None = None
    size: str | None = None  # the product's available sizes include this one
    price_min: int | None = Field(default=None, ge=0)
    price_max: int | None = Field(default=None, ge=0)
    in_stock: bool | None = None
    file: str | None = None  # policy document
    clause: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _price_range(self) -> "SearchFilters":
        if self.price_min is not None and self.price_max is not None and self.price_min > self.price_max:
            raise ValueError("price_min > price_max")
        return self

    def to_qdrant(self) -> models.Filter | None:
        must: list[models.Condition] = []

        def match(key: str, value) -> None:
            must.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))

        for key in ("doc_type", "category", "kind", "color", "material", "in_stock", "file", "clause"):
            value = getattr(self, key)
            if value is not None:
                match(key, value)
        if self.size is not None:
            match("sizes", self.size)  # matches if any element of the list equals it
        if self.price_min is not None or self.price_max is not None:
            must.append(models.FieldCondition(key="price_vnd", range=models.Range(gte=self.price_min, lte=self.price_max)))
        return models.Filter(must=must) if must else None


@dataclass(frozen=True)
class Hit:
    point_id: str
    score: float
    key: str
    text: str
    payload: dict


TRACE_HITS = 5  # hits named in a trace summary
TRACE_TITLE = 80


def trace_hits(hits: list[Hit]) -> list[dict]:
    """The hits as a trace summary names them: id, kind, a short title and the score. No text, so no policy clause ever reaches a trace."""
    named = []
    for hit in hits[:TRACE_HITS]:
        kind = hit.payload.get("doc_type") or ("policy" if hit.key.startswith("policy:") else "product")
        title = hit.payload.get("name") if kind == "product" else hit.payload.get("title")
        named.append({"id": hit.key or hit.point_id, "kind": kind, "title": str(title or "")[:TRACE_TITLE], "score": round(hit.score, 5)})
    return named


class Retriever:
    """Holds the Qdrant client and the (loaded once) embedder."""

    def __init__(self, client: QdrantClient, embedder, collection: str | None = None,
                 lexical: LexicalConfig | None = None) -> None:
        self.client = client
        self.embedder = embedder
        self.collection = collection or active_collection()
        self.lexical = lexical or LexicalConfig()  # query side: only the folding is needed (weights are binary)

    def _weights(self, branch: str, query: str, embedding: Embedding) -> dict[int, float] | list[float]:
        if branch == DENSE:
            return embedding.dense
        return embedding.sparse if branch == SPARSE else query_vector(query)

    def branch_ranking(self, branch: str, query: str, flt: models.Filter | None, embedding: Embedding,
                       size: int) -> list:
        """The WHOLE ranking of one branch (`size` = collection size), ordered by (score desc, stable key asc).
        Empty if the query has no weights for this branch. Scores are Qdrant's own, never modified."""
        weights = self._weights(branch, query, embedding)
        if not weights or size == 0:
            return []
        vector = weights if branch == DENSE else to_sparse_vector(weights)
        points = self.client.query_points(self.collection, query=vector, using=branch, query_filter=flt, limit=size,
                                          with_payload=True).points
        return sorted(points, key=lambda p: (-p.score, stable_key(p.payload or {}, str(p.id))))

    def ranking(self, query: str, branch: str, filters: SearchFilters | dict | None = None) -> list:
        """The whole ordered ranking of ONE branch ("dense", "sparse" or "folded") for a query: what search()
        fuses (before the window cut). For inspection and tests."""
        if isinstance(filters, dict):
            filters = SearchFilters.model_validate(filters)
        return self.branch_ranking(branch, query, filters.to_qdrant() if filters else None,
                                   self.embedder.encode_query(query), self.client.count(self.collection, exact=True).count)

    def count(self, filters: SearchFilters | dict | None = None) -> int:
        """How many documents match `filters` (exact, applied inside Qdrant like in a search). No embedding, no ranking, no LLM."""
        if isinstance(filters, dict):
            filters = SearchFilters.model_validate(filters)
        flt = filters.to_qdrant() if filters else None
        return self.client.count(self.collection, count_filter=flt, exact=True).count

    def search(self, query: str, filters: SearchFilters | dict | None = None, k: int = 10,
               mode: Mode | None = None) -> list[Hit]:
        """`mode=None` means the configured default (app.config.DEFAULT_RETRIEVAL_MODE)."""
        model_id = getattr(getattr(self.embedder, "config", None), "model_id", None)
        with span("retrieval", model=model_id) as recorded:  # a no-op outside a traced request
            hits = self._search(query, filters, k, mode)
            recorded.set_summary(mode=mode or config.DEFAULT_RETRIEVAL_MODE, k=k, hits=trace_hits(hits))
            return hits

    def _search(self, query: str, filters: SearchFilters | dict | None, k: int, mode: Mode | None) -> list[Hit]:
        if k < 1:
            raise ValueError("k must be >= 1")
        mode = mode or config.DEFAULT_RETRIEVAL_MODE  # looked up at call time: one value, one place
        if mode != "dense" and mode not in FUSED_BRANCHES:
            raise ValueError(f"unknown mode {mode!r}")
        if isinstance(filters, dict):
            filters = SearchFilters.model_validate(filters)
        flt = filters.to_qdrant() if filters else None
        embedding: Embedding = self.embedder.encode_query(query)

        if mode == "dense":
            result = self.client.query_points(self.collection, query=embedding.dense, using=DENSE,
                                              query_filter=flt, limit=fetch_limit(k), with_payload=True)
            ordered = sorted(result.points,
                             key=lambda p: (-p.score, stable_key(p.payload or {}, str(p.id))))[:k]
            scored = [(point, point.score) for point in ordered]
        else:
            size = self.client.count(self.collection, exact=True).count
            rankings = [self.branch_ranking(branch, query, flt, embedding, size) for branch in FUSED_BRANCHES[mode]]
            points = {str(p.id): p for ranking in rankings for p in ranking}
            fused = rrf_fuse([[str(p.id) for p in ranking] for ranking in rankings], window=PREFETCH_LIMIT)
            best = sorted(fused, key=lambda i: (-fused[i], stable_key(points[i].payload or {}, i)))[:k]
            scored = [(points[i], float(fused[i])) for i in best]

        hits = []
        for point, score in scored:
            payload = dict(point.payload or {})
            text = payload.get("text", "")
            for field in HIDDEN_PAYLOAD:
                payload.pop(field, None)
            hits.append(Hit(point_id=str(point.id), score=score, key=payload.pop("key", ""), text=text,
                            payload=payload))
        return hits
