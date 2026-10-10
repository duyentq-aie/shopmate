"""Idempotent indexing of Documents into the Qdrant collection.

- Deterministic point ids (uuid5 of the document key), so re-runs upsert the same points.
- Re-embedding key = (text_sha256, embedding fingerprint): a point is re-embedded when its text
  changes OR when the model revision / embedding settings change. A payload-only change
  (payload_sha256) overwrites the payload without re-embedding. The model is not even loaded
  when nothing needs embedding.
- Points whose document no longer exists are deleted, so the collection always equals the
  current document set (the manifest relies on that).

Documents are inserted in R16 stable-key order (sku; file:clause zero-padded), whatever order the caller
passes: the layout is explicit and reproducible. (Measured: Qdrant does NOT break exact score ties by
insertion order, so this order is not what makes ties deterministic; the tie factor below is.)

Layout v2 (Phase 4, `lexical` given): every point also carries the `folded` vector: BM25 weights of its
text, scaled by the tie factor of its stable-key rank (app/retrieval/lexical.py). It is keyed by
`lexical_fingerprint` (the config) and `lexical_tie` ("rank/n"). A change of the lexical config, or a
change of the document set that moves a rank, rewrites ONLY the folded vector and those two payload
fields (no model, dense/sparse untouched).

`seed_from` fills a new v2 collection from an existing v1 collection: for every document whose
(text hash, embedding fingerprint) matches the source point, dense and sparse are COPIED, not
recomputed, and the folded vector is computed. A digest over the copied dense/sparse vectors is
compared between source and target; any difference raises SeedMismatch.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from qdrant_client import QdrantClient, models

from app.retrieval.documents import Document
from app.retrieval.embedder import Embedding, EmbeddingConfig
from app.retrieval.folding import tokenize
from app.retrieval.lexical import LexicalConfig, check_no_collisions, check_tie_resolution, doc_vector, tie_factor
from app.retrieval.search import stable_key
from app.retrieval.store import (
    COLLECTION,
    DENSE,
    FOLDED,
    SPARSE,
    existing_index_keys,
    existing_lexical_state,
    to_sparse_vector,
    vectors_digest,
)

UPSERT_BATCH = 64


class SeedMismatch(Exception):
    """Dense/sparse vectors of a seeded collection differ from the source collection's."""


class Embedder(Protocol):
    config: EmbeddingConfig

    def encode(self, texts: list[str]) -> list[Embedding]: ...


@dataclass
class IndexStats:
    embedded: int = 0
    payload_updated: int = 0
    skipped: int = 0
    deleted: int = 0
    lexical_computed: int = 0  # folded vectors written (with an embedding, a copy or on their own)
    copied: int = 0  # points whose dense + sparse vectors were copied from the seed collection
    seed_digest: str | None = None  # digest of the copied dense/sparse vectors (equal on both sides)


Ranks = dict[str, tuple[int, int]]  # point id -> (rank by stable key, number of documents)


def stable_key_ranks(documents: list[Document]) -> Ranks:
    ordered = sorted(documents, key=lambda d: stable_key(d.payload, d.key))
    return {d.point_id: (rank, len(ordered)) for rank, d in enumerate(ordered)}


def tie_label(rank_of: tuple[int, int], lexical: LexicalConfig) -> str:
    """The `lexical_tie` payload value: "rank/n" while the tie-break is on, "" when tie_delta is 0."""
    return f"{rank_of[0]}/{rank_of[1]}" if lexical.tie_delta > 0 else ""


def point_payload(doc: Document, fingerprint: str, lexical_fingerprint: str | None = None,
                  lexical_tie: str | None = None) -> dict:
    payload = {**doc.payload, "key": doc.key, "text": doc.text, "text_sha256": doc.text_sha256,
               "payload_sha256": doc.payload_sha256, "embedding_fingerprint": fingerprint}
    if lexical_fingerprint is not None:
        payload["lexical_fingerprint"] = lexical_fingerprint
        payload["lexical_tie"] = lexical_tie or ""
    return payload


def folded_vector(doc: Document, lexical: LexicalConfig, rank_of: tuple[int, int]) -> models.SparseVector:
    factor = tie_factor(rank_of[0], rank_of[1], lexical)
    return to_sparse_vector({token: weight * factor for token, weight in doc_vector(doc.text, lexical).items()})


def to_point(doc: Document, emb: Embedding, fingerprint: str, lexical: LexicalConfig | None = None,
             rank_of: tuple[int, int] | None = None) -> models.PointStruct:
    vector = {DENSE: emb.dense, SPARSE: to_sparse_vector(emb.sparse)}
    if lexical is not None:
        vector[FOLDED] = folded_vector(doc, lexical, rank_of)
    return models.PointStruct(
        id=doc.point_id,
        vector=vector,
        payload=point_payload(doc, fingerprint, lexical.fingerprint() if lexical else None,
                              tie_label(rank_of, lexical) if lexical else None),
    )


def lexical_tie_mismatches(client: QdrantClient, collection: str = COLLECTION) -> int:
    """Number of points whose stored `lexical_tie` is not "rank/n" of their stable key among the points now in
    the collection: their folded vector was scaled for another document set (or another order)."""
    payloads, offset = [], None
    while True:
        points, offset = client.scroll(collection, limit=256, offset=offset, with_vectors=False, with_payload=True)
        payloads.extend((str(p.id), p.payload or {}) for p in points)
        if offset is None:
            break
    ordered = sorted(payloads, key=lambda item: stable_key(item[1], item[1].get("key", item[0])))
    return sum(payload.get("lexical_tie", "") != f"{rank}/{len(ordered)}" for rank, (_id, payload) in enumerate(ordered))


def seed_points(client: QdrantClient, source: str, target: str, documents: list[Document], config: EmbeddingConfig,
                lexical: LexicalConfig, ranks: Ranks) -> tuple[int, str | None]:
    """Copy dense + sparse of every up-to-date source point into `target` (missing there) and add the
    folded vector. Returns (points copied, digest of what was copied)."""
    fingerprint = config.fingerprint()
    source_keys = existing_index_keys(client, source)
    target_keys = existing_index_keys(client, target)
    todo = [d for d in documents
            if d.point_id not in target_keys and source_keys.get(d.point_id, ("", "", ""))[:2] == (d.text_sha256, fingerprint)]
    for start in range(0, len(todo), UPSERT_BATCH):
        batch = todo[start : start + UPSERT_BATCH]
        vectors = {str(r.id): r.vector for r in client.retrieve(source, ids=[d.point_id for d in batch],
                                                                with_vectors=[DENSE, SPARSE], with_payload=False)}
        points = [
            models.PointStruct(
                id=d.point_id,
                vector={DENSE: vectors[d.point_id][DENSE],
                        SPARSE: models.SparseVector(indices=vectors[d.point_id][SPARSE].indices,
                                                    values=vectors[d.point_id][SPARSE].values),
                        FOLDED: folded_vector(d, lexical, ranks[d.point_id])},
                payload=point_payload(d, fingerprint, lexical.fingerprint(), tie_label(ranks[d.point_id], lexical)),
            )
            for d in batch
        ]
        client.upsert(target, points=points, wait=True)
    if not todo:
        return 0, None
    ids = [d.point_id for d in todo]
    source_digest, target_digest = vectors_digest(client, source, ids), vectors_digest(client, target, ids)
    if source_digest != target_digest:
        raise SeedMismatch(f"dense/sparse digest of {len(ids)} copied points differs: "
                           f"{source!r} {source_digest[:16]} != {target!r} {target_digest[:16]}")
    return len(todo), target_digest


def index_documents(client: QdrantClient, documents: list[Document], config: EmbeddingConfig,
                    embedder_factory: Callable[[], Embedder], collection: str = COLLECTION,
                    lexical: LexicalConfig | None = None, seed_from: str | None = None) -> IndexStats:
    documents = sorted(documents, key=lambda d: stable_key(d.payload, d.key))  # explicit insertion order
    ids = [d.point_id for d in documents]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate document keys")
    if seed_from is not None and lexical is None:
        raise ValueError("seeding a collection creates the folded vector: a lexical config is required")
    ranks = stable_key_ranks(documents)
    if lexical is not None:  # before any write: either failure would corrupt the folded vectors silently
        check_no_collisions(token for d in documents for token in tokenize(d.text))
        check_tie_resolution(len(documents), lexical)
    fingerprint = config.fingerprint()
    lexical_fingerprint = lexical.fingerprint() if lexical else None
    stats = IndexStats()
    if seed_from is not None:
        stats.copied, stats.seed_digest = seed_points(client, seed_from, collection, documents, config, lexical, ranks)
        stats.lexical_computed += stats.copied
    existing = existing_index_keys(client, collection)
    existing_lexical = existing_lexical_state(client, collection) if lexical else {}

    to_embed = [d for d in documents if existing.get(d.point_id, ("", "", ""))[:2] != (d.text_sha256, fingerprint)]
    embed_ids = {d.point_id for d in to_embed}
    to_lexical = [d for d in documents
                  if lexical and d.point_id not in embed_ids
                  and existing_lexical.get(d.point_id) != (lexical_fingerprint, tie_label(ranks[d.point_id], lexical))]
    to_update = [d for d in documents
                 if d.point_id not in embed_ids and existing[d.point_id][2] != d.payload_sha256]
    for start in range(0, len(to_lexical), UPSERT_BATCH):
        batch = to_lexical[start : start + UPSERT_BATCH]
        client.update_vectors(collection, wait=True,
                              points=[models.PointVectors(id=d.point_id,
                                                          vector={FOLDED: folded_vector(d, lexical, ranks[d.point_id])})
                                      for d in batch])
    for doc in to_lexical:
        client.set_payload(collection, payload={"lexical_fingerprint": lexical_fingerprint,
                                                "lexical_tie": tie_label(ranks[doc.point_id], lexical)},
                           points=[doc.point_id], wait=True)
    stats.lexical_computed += len(to_lexical)
    for doc in to_update:
        client.overwrite_payload(
            collection, points=[doc.point_id], wait=True,
            payload=point_payload(doc, fingerprint, lexical_fingerprint,
                                  tie_label(ranks[doc.point_id], lexical) if lexical else None))
    stats.payload_updated = len(to_update)
    touched = embed_ids | {d.point_id for d in to_lexical} | {d.point_id for d in to_update}
    stats.skipped = len(documents) - stats.copied - len(touched)
    if to_embed:
        embedder = embedder_factory()
        if embedder.config.fingerprint() != fingerprint:
            raise ValueError("embedder config does not match the index config")
        embeddings = embedder.encode([d.text for d in to_embed])
        points = [to_point(d, e, fingerprint, lexical, ranks[d.point_id]) for d, e in zip(to_embed, embeddings)]
        for start in range(0, len(points), UPSERT_BATCH):
            client.upsert(collection, points=points[start : start + UPSERT_BATCH], wait=True)
        stats.embedded = len(points)
        if lexical:
            stats.lexical_computed += len(points)

    stale = sorted(set(existing) - set(ids))
    if stale:
        client.delete(collection, points_selector=models.PointIdsList(points=stale), wait=True)
        stats.deleted = len(stale)
    return stats
