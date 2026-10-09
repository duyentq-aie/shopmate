"""Qdrant collection layout for ShopMate: one collection, two named vectors.

- "dense":  1024-dim bge-m3 [CLS] vector, cosine distance;
- "sparse": bge-m3 lexical weights (sparse vector, no IDF modifier: the weights are learned).
Payload indexes exist for every field retrieval filters on.

Each point's payload carries `content_sha256` and `embedding_fingerprint`; together they are the
skip key of the indexer, so a change of text, payload, model revision or embedding settings
forces re-embedding.
"""

from __future__ import annotations

from qdrant_client import QdrantClient, models

from app.config import get_setting
from app.retrieval.embedder import EmbeddingConfig

COLLECTION = "shopmate_vn"
DENSE = "dense"
SPARSE = "sparse"
DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"

PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    "doc_type": models.PayloadSchemaType.KEYWORD,
    "sku": models.PayloadSchemaType.KEYWORD,
    "category": models.PayloadSchemaType.KEYWORD,
    "color": models.PayloadSchemaType.KEYWORD,
    "material": models.PayloadSchemaType.KEYWORD,
    "sizes": models.PayloadSchemaType.KEYWORD,
    "price_vnd": models.PayloadSchemaType.INTEGER,
    "in_stock": models.PayloadSchemaType.BOOL,
    "file": models.PayloadSchemaType.KEYWORD,
    "clause": models.PayloadSchemaType.INTEGER,
}


class CollectionMismatch(Exception):
    """The existing collection's vector config differs from the expected one (use --rebuild)."""


def qdrant_url() -> str:
    return get_setting("QDRANT_URL", DEFAULT_QDRANT_URL)


def connect(url: str | None = None) -> QdrantClient:
    return QdrantClient(url=url or qdrant_url(), timeout=30)


def vector_config(config: EmbeddingConfig) -> dict:
    """The vector layout, as recorded in the manifest."""
    return {
        DENSE: {"size": config.dense_dim, "distance": "Cosine"},
        SPARSE: {"modifier": "none"},
    }


def ensure_collection(client: QdrantClient, config: EmbeddingConfig, collection: str = COLLECTION,
                      recreate: bool = False) -> bool:
    """Create the collection if missing (or recreate). Returns True if it was (re)created."""
    exists = client.collection_exists(collection)
    if exists and recreate:
        client.delete_collection(collection)
        exists = False
    if exists:
        params = client.get_collection(collection).config.params
        dense = (params.vectors or {}).get(DENSE)
        if (dense is None or dense.size != config.dense_dim or dense.distance != models.Distance.COSINE
                or SPARSE not in (params.sparse_vectors or {})):
            raise CollectionMismatch(f"collection {collection!r} has a different vector layout; rebuild it")
        return False
    client.create_collection(
        collection,
        vectors_config={DENSE: models.VectorParams(size=config.dense_dim, distance=models.Distance.COSINE)},
        sparse_vectors_config={SPARSE: models.SparseVectorParams()},
    )
    for field, schema in PAYLOAD_INDEXES.items():
        client.create_payload_index(collection, field_name=field, field_schema=schema)
    return True


def existing_index_keys(client: QdrantClient, collection: str = COLLECTION) -> dict[str, tuple[str, str]]:
    """point id -> (content_sha256, embedding_fingerprint) for every point."""
    keys: dict[str, tuple[str, str]] = {}
    offset = None
    while True:
        points, offset = client.scroll(collection, limit=256, offset=offset, with_vectors=False,
                                       with_payload=["content_sha256", "embedding_fingerprint"])
        for point in points:
            payload = point.payload or {}
            keys[str(point.id)] = (payload.get("content_sha256", ""), payload.get("embedding_fingerprint", ""))
        if offset is None:
            return keys


def count_by_doc_type(client: QdrantClient, collection: str = COLLECTION) -> dict[str, int]:
    counts = {}
    for doc_type in ("product", "policy"):
        flt = models.Filter(must=[models.FieldCondition(key="doc_type", match=models.MatchValue(value=doc_type))])
        counts[doc_type] = client.count(collection, count_filter=flt, exact=True).count
    counts["total"] = client.count(collection, exact=True).count
    return counts
