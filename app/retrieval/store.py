"""Qdrant collection layout for ShopMate: named vectors in one collection.

- "dense":  1024-dim bge-m3 [CLS] vector, cosine distance;
- "sparse": bge-m3 lexical weights (sparse vector, no IDF modifier: the weights are learned);
- "folded": (layout v2 only, Phase 4) BM25-style weights on diacritic-folded syllables, created
  with Qdrant's IDF modifier. Qdrant cannot add a named vector to an existing collection, so v2 is
  a NEW collection (COLLECTION_V2); the Phase 3A/3B collection COLLECTION is kept as it is.
Payload indexes exist for every field retrieval filters on.

Each point's payload carries `text_sha256` and `embedding_fingerprint` (the re-embedding key:
a change of text, model revision or embedding settings forces re-embedding) and `payload_sha256`
(a payload-only change is written without re-embedding). Layout v2 points also carry
`lexical_fingerprint` (a change of the lexical config recomputes only the folded vector).
"""

from __future__ import annotations

import hashlib
import struct
from array import array

from qdrant_client import QdrantClient, models

from app.config import get_setting
from app.retrieval.embedder import EmbeddingConfig

COLLECTION = "shopmate_vn"  # layout v1 (Phase 3A/3B): dense + sparse. Kept for reproducibility.
COLLECTION_V2 = "shopmate_vn_v2"  # layout v2 (Phase 4): dense + sparse + folded
DENSE = "dense"
SPARSE = "sparse"
FOLDED = "folded"
DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"

PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    "doc_type": models.PayloadSchemaType.KEYWORD,
    "sku": models.PayloadSchemaType.KEYWORD,
    "category": models.PayloadSchemaType.KEYWORD,
    "kind": models.PayloadSchemaType.KEYWORD,
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


def vector_config(config: EmbeddingConfig, lexical: bool = False) -> dict:
    """The vector layout, as recorded in the manifest."""
    layout = {
        DENSE: {"size": config.dense_dim, "distance": "Cosine"},
        SPARSE: {"modifier": "none"},
    }
    if lexical:
        layout[FOLDED] = {"modifier": "idf"}
    return layout


def to_sparse_vector(weights: dict[int, float]) -> models.SparseVector:
    indices = sorted(weights)
    return models.SparseVector(indices=indices, values=[weights[i] for i in indices])


def ensure_collection(client: QdrantClient, config: EmbeddingConfig, collection: str = COLLECTION,
                      recreate: bool = False, lexical: bool = False) -> bool:
    """Create the collection if missing (or recreate). Returns True if it was (re)created.

    `lexical` selects layout v2 (adds the `folded` vector). An existing collection must have exactly
    the requested layout: a v1 collection is never silently used as v2 or the other way round."""
    exists = client.collection_exists(collection)
    if exists and recreate:
        client.delete_collection(collection)
        exists = False
    if exists:
        params = client.get_collection(collection).config.params
        dense = (params.vectors or {}).get(DENSE)
        sparse_vectors = params.sparse_vectors or {}
        folded = sparse_vectors.get(FOLDED)
        if (dense is None or dense.size != config.dense_dim or dense.distance != models.Distance.COSINE
                or SPARSE not in sparse_vectors or (FOLDED in sparse_vectors) != lexical
                or (lexical and folded.modifier != models.Modifier.IDF)):
            raise CollectionMismatch(f"collection {collection!r} has a different vector layout; rebuild it")
        present = set((client.get_collection(collection).payload_schema or {}).keys())
        for field, schema in PAYLOAD_INDEXES.items():
            if field not in present:  # e.g. a filter field added later
                client.create_payload_index(collection, field_name=field, field_schema=schema)
        return False
    client.create_collection(
        collection,
        vectors_config={DENSE: models.VectorParams(size=config.dense_dim, distance=models.Distance.COSINE)},
        sparse_vectors_config={SPARSE: models.SparseVectorParams(),
                               **({FOLDED: models.SparseVectorParams(modifier=models.Modifier.IDF)} if lexical else {})},
    )
    for field, schema in PAYLOAD_INDEXES.items():
        client.create_payload_index(collection, field_name=field, field_schema=schema)
    return True


def existing_index_keys(client: QdrantClient, collection: str = COLLECTION) -> dict[str, tuple[str, str, str]]:
    """point id -> (text_sha256, embedding_fingerprint, payload_sha256) for every point."""
    keys: dict[str, tuple[str, str]] = {}
    offset = None
    while True:
        points, offset = client.scroll(collection, limit=256, offset=offset, with_vectors=False,
                                       with_payload=["text_sha256", "embedding_fingerprint", "payload_sha256"])
        for point in points:
            payload = point.payload or {}
            keys[str(point.id)] = (payload.get("text_sha256", ""), payload.get("embedding_fingerprint", ""),
                                   payload.get("payload_sha256", ""))
        if offset is None:
            return keys


def fingerprint_counts(client: QdrantClient, collection: str = COLLECTION) -> dict[str, int]:
    """embedding_fingerprint -> number of points carrying it (a missing fingerprint counts as "")."""
    counts: dict[str, int] = {}
    for _text_sha, fingerprint, _payload_sha in existing_index_keys(client, collection).values():
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
    return counts


def count_by_doc_type(client: QdrantClient, collection: str = COLLECTION) -> dict[str, int]:
    counts = {}
    for doc_type in ("product", "policy"):
        flt = models.Filter(must=[models.FieldCondition(key="doc_type", match=models.MatchValue(value=doc_type))])
        counts[doc_type] = client.count(collection, count_filter=flt, exact=True).count
    counts["total"] = client.count(collection, exact=True).count
    return counts


def existing_lexical_fingerprints(client: QdrantClient, collection: str = COLLECTION) -> dict[str, str]:
    """point id -> lexical_fingerprint ("" when the point has none)."""
    out: dict[str, str] = {}
    offset = None
    while True:
        points, offset = client.scroll(collection, limit=256, offset=offset, with_vectors=False,
                                       with_payload=["lexical_fingerprint"])
        for point in points:
            out[str(point.id)] = (point.payload or {}).get("lexical_fingerprint", "")
        if offset is None:
            return out


def lexical_fingerprint_counts(client: QdrantClient, collection: str = COLLECTION) -> dict[str, int]:
    """lexical_fingerprint -> number of points carrying it (a missing fingerprint counts as "")."""
    counts: dict[str, int] = {}
    for fingerprint in existing_lexical_fingerprints(client, collection).values():
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
    return counts


def vectors_digest(client: QdrantClient, collection: str = COLLECTION, ids: list[str] | None = None) -> str:
    """SHA-256 over the bge-m3 `dense` and `sparse` vectors (point id, float32 bytes), in point-id order.

    The folded vector is deliberately not part of it: this digest is the proof that the baseline
    vectors of two collections (or of one collection before and after an update) are bit-identical.
    `ids` restricts it to a subset (the same subset must be hashed on both sides)."""
    points = []
    if ids is None:
        offset = None
        while True:
            batch, offset = client.scroll(collection, limit=128, offset=offset, with_vectors=[DENSE, SPARSE],
                                          with_payload=False)
            points.extend(batch)
            if offset is None:
                break
    else:
        for start in range(0, len(ids), 128):
            points.extend(client.retrieve(collection, ids=ids[start : start + 128], with_vectors=[DENSE, SPARSE],
                                          with_payload=False))
    digest = hashlib.sha256()
    for point in sorted(points, key=lambda p: str(p.id)):
        digest.update(str(point.id).encode())
        digest.update(array("f", point.vector[DENSE]).tobytes())
        sparse = point.vector[SPARSE]
        for index, value in sorted(zip(sparse.indices, sparse.values)):
            digest.update(struct.pack("<If", index, value))
    return digest.hexdigest()
