"""Baseline retrieval over the ShopMate collection.

search(query, filters, k, mode):
- mode "dense":  nearest neighbours on the bge-m3 dense vector;
- mode "hybrid": dense and sparse candidates (PREFETCH_LIMIT each) fused with Reciprocal Rank
  Fusion inside Qdrant (Query API, FusionQuery RRF).
Filters are translated to a Qdrant filter and applied INSIDE Qdrant, on every prefetch branch and
on the final query, so a result can never violate them.

Deliberately a baseline: no query rewriting, no Vietnamese normalisation, no reranker, no LLM.
Those come later as measured experiments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from qdrant_client import QdrantClient, models

from app.retrieval.embedder import Embedding
from app.retrieval.store import COLLECTION, DENSE, SPARSE

Mode = Literal["dense", "hybrid"]
PREFETCH_LIMIT = 50  # candidates per branch before RRF; part of the baseline definition
HIDDEN_PAYLOAD = ("text", "content_sha256", "embedding_fingerprint")


class SearchFilters(BaseModel):
    """Structured filters. All set fields must hold (AND)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_type: Literal["product", "policy"] | None = None
    category: str | None = None
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

        for key in ("doc_type", "category", "color", "material", "in_stock", "file", "clause"):
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

    def __init__(self, client: QdrantClient, embedder, collection: str = COLLECTION) -> None:
        self.client = client
        self.embedder = embedder
        self.collection = collection

    def search(self, query: str, filters: SearchFilters | dict | None = None, k: int = 10,
               mode: Mode = "dense") -> list[Hit]:
        if k < 1:
            raise ValueError("k must be >= 1")
        if isinstance(filters, dict):
            filters = SearchFilters.model_validate(filters)
        flt = filters.to_qdrant() if filters else None
        embedding: Embedding = self.embedder.encode_query(query)

        if mode == "dense":
            result = self.client.query_points(self.collection, query=embedding.dense, using=DENSE,
                                              query_filter=flt, limit=k, with_payload=True)
        elif mode == "hybrid":
            prefetch = [models.Prefetch(query=embedding.dense, using=DENSE, filter=flt, limit=PREFETCH_LIMIT)]
            if embedding.sparse:
                indices = sorted(embedding.sparse)
                sparse = models.SparseVector(indices=indices, values=[embedding.sparse[i] for i in indices])
                prefetch.append(models.Prefetch(query=sparse, using=SPARSE, filter=flt, limit=PREFETCH_LIMIT))
            result = self.client.query_points(self.collection, prefetch=prefetch,
                                              query=models.FusionQuery(fusion=models.Fusion.RRF),
                                              query_filter=flt, limit=k, with_payload=True)
        else:
            raise ValueError(f"unknown mode {mode!r}")

        hits = []
        for point in result.points:
            payload = dict(point.payload or {})
            text = payload.get("text", "")
            for field in HIDDEN_PAYLOAD:
                payload.pop(field, None)
            hits.append(Hit(point_id=str(point.id), score=point.score, key=payload.pop("key", ""), text=text,
                            payload=payload))
        return hits
