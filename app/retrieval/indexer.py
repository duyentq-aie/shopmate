"""Idempotent indexing of Documents into the Qdrant collection.

- Deterministic point ids (uuid5 of the document key), so re-runs upsert the same points.
- Skip key = (content_sha256, embedding fingerprint): a point is re-embedded when its text or
  payload changes OR when the model revision / embedding settings change. Unchanged documents
  are not re-embedded, and the model is not even loaded when nothing needs embedding.
- Points whose document no longer exists are deleted, so the collection always equals the
  current document set (the manifest relies on that).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from qdrant_client import QdrantClient, models

from app.retrieval.documents import Document
from app.retrieval.embedder import Embedding, EmbeddingConfig
from app.retrieval.store import COLLECTION, DENSE, SPARSE, existing_index_keys

UPSERT_BATCH = 64


class Embedder(Protocol):
    config: EmbeddingConfig

    def encode(self, texts: list[str]) -> list[Embedding]: ...


@dataclass
class IndexStats:
    embedded: int = 0
    skipped: int = 0
    deleted: int = 0


def point_payload(doc: Document, fingerprint: str) -> dict:
    return {**doc.payload, "key": doc.key, "text": doc.text,
            "content_sha256": doc.content_sha256, "embedding_fingerprint": fingerprint}


def to_point(doc: Document, emb: Embedding, fingerprint: str) -> models.PointStruct:
    indices = sorted(emb.sparse)
    return models.PointStruct(
        id=doc.point_id,
        vector={DENSE: emb.dense, SPARSE: models.SparseVector(indices=indices, values=[emb.sparse[i] for i in indices])},
        payload=point_payload(doc, fingerprint),
    )


def index_documents(client: QdrantClient, documents: list[Document], config: EmbeddingConfig,
                    embedder_factory: Callable[[], Embedder], collection: str = COLLECTION) -> IndexStats:
    ids = [d.point_id for d in documents]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate document keys")
    fingerprint = config.fingerprint()
    existing = existing_index_keys(client, collection)
    stats = IndexStats()

    to_embed = [d for d in documents if existing.get(d.point_id) != (d.content_sha256, fingerprint)]
    stats.skipped = len(documents) - len(to_embed)
    if to_embed:
        embedder = embedder_factory()
        if embedder.config.fingerprint() != fingerprint:
            raise ValueError("embedder config does not match the index config")
        embeddings = embedder.encode([d.text for d in to_embed])
        points = [to_point(d, e, fingerprint) for d, e in zip(to_embed, embeddings)]
        for start in range(0, len(points), UPSERT_BATCH):
            client.upsert(collection, points=points[start : start + UPSERT_BATCH], wait=True)
        stats.embedded = len(points)

    stale = sorted(set(existing) - set(ids))
    if stale:
        client.delete(collection, points_selector=models.PointIdsList(points=stale), wait=True)
        stats.deleted = len(stale)
    return stats
