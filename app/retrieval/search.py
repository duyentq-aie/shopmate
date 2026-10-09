"""Baseline retrieval over the ShopMate collection.

search(query, filters, k, mode):
- mode "dense":  nearest neighbours on the bge-m3 dense vector;
- mode "hybrid": dense and sparse candidates (PREFETCH_LIMIT each) fused with Reciprocal Rank
  Fusion inside Qdrant (Query API, FusionQuery RRF);
- Phase 4 experiment 1 (layout-v2 collection only), the same fusion with the diacritic-folded
  lexical vector (`folded`) as an extra or replacement branch:
    "dense_sparse_folded": dense + sparse + folded;
    "dense_folded":        dense + folded (the folded branch replaces bge-m3 sparse).
  Every fused mode uses the same RRF settings, PREFETCH_LIMIT and R16 ordering, so branches are the
  only thing that differs. A branch with no query weights (no tokens) is dropped.
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

from app.retrieval.embedder import Embedding
from app.retrieval.lexical import LexicalConfig, query_vector
from app.retrieval.manifest import active_collection
from app.retrieval.store import DENSE, FOLDED, SPARSE, to_sparse_vector

Mode = Literal["dense", "hybrid", "dense_sparse_folded", "dense_folded"]
FUSED_BRANCHES: dict[str, tuple[str, ...]] = {
    "hybrid": (DENSE, SPARSE),
    "dense_sparse_folded": (DENSE, SPARSE, FOLDED),
    "dense_folded": (DENSE, FOLDED),
}
PREFETCH_LIMIT = 50  # candidates per branch before RRF; part of the baseline definition
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

    def search(self, query: str, filters: SearchFilters | dict | None = None, k: int = 10,
               mode: Mode = "dense") -> list[Hit]:
        if k < 1:
            raise ValueError("k must be >= 1")
        if mode != "dense" and mode not in FUSED_BRANCHES:
            raise ValueError(f"unknown mode {mode!r}")
        if isinstance(filters, dict):
            filters = SearchFilters.model_validate(filters)
        flt = filters.to_qdrant() if filters else None
        embedding: Embedding = self.embedder.encode_query(query)

        if mode == "dense":
            result = self.client.query_points(self.collection, query=embedding.dense, using=DENSE,
                                              query_filter=flt, limit=fetch_limit(k), with_payload=True)
        else:
            prefetch = []
            for branch in FUSED_BRANCHES[mode]:
                weights = self._weights(branch, query, embedding)
                if not weights:
                    continue  # no sparse weights / no tokens: this branch cannot say anything
                vector = weights if branch == DENSE else to_sparse_vector(weights)
                prefetch.append(models.Prefetch(query=vector, using=branch, filter=flt, limit=PREFETCH_LIMIT))
            result = self.client.query_points(self.collection, prefetch=prefetch,
                                              query=models.FusionQuery(fusion=models.Fusion.RRF),
                                              query_filter=flt, limit=fetch_limit(k), with_payload=True)

        ordered = sorted(result.points,
                         key=lambda p: (-p.score, stable_key(p.payload or {}, str(p.id))))[:k]
        hits = []
        for point in ordered:
            payload = dict(point.payload or {})
            text = payload.get("text", "")
            for field in HIDDEN_PAYLOAD:
                payload.pop(field, None)
            hits.append(Hit(point_id=str(point.id), score=point.score, key=payload.pop("key", ""), text=text,
                            payload=payload))
        return hits
