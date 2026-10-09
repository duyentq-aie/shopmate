"""Indexer, store, manifest and search logic on an in-memory Qdrant with a fake embedder (no services)."""

from __future__ import annotations

import dataclasses
import json
import random

import pytest
from qdrant_client import QdrantClient

from app.retrieval.documents import Document
from app.retrieval.indexer import index_documents
from app.retrieval.manifest import write_manifest
from app.retrieval.search import Retriever, SearchFilters
from app.retrieval.store import CollectionMismatch, count_by_doc_type, ensure_collection
from tests.fake_embedder import FAKE_CONFIG, FakeEmbedder

# The in-memory Qdrant ignores payload indexes and says so; the server (integration tests) uses them.
pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")

COLORS, MATERIALS, CATEGORIES = ["Đen", "Trắng", "Xám"], ["Cotton", "Da bò"], ["thoi-trang-nam", "giay-dep"]
KINDS = ["Áo thun nam", "Giày sneaker nam", "Ví da nam"]
SIZE_POOL = ["S", "M", "L", "39", "40", "41"]


def _documents(n: int = 40) -> list[Document]:
    rng = random.Random(7)
    docs = []
    for i in range(n):
        sku = f"SM-NAM-{i:04d}"
        payload = {"doc_type": "product", "sku": sku, "name": f"sản phẩm {i}", "category": rng.choice(CATEGORIES),
                   "kind": rng.choice(KINDS), "price_vnd": rng.randrange(100, 1000) * 1000, "color": rng.choice(COLORS),
                   "material": rng.choice(MATERIALS), "sizes": sorted(rng.sample(SIZE_POOL, 2)), "in_stock": rng.random() > 0.2}
        docs.append(Document(f"product:{sku}", f"áo {payload['color']} {payload['material']} số {i}", payload))
    for clause in (1, 2, 3):
        docs.append(Document(f"policy:van-chuyen:{clause}", f"vận chuyển điều {clause} phí ship",
                             {"doc_type": "policy", "file": "van-chuyen", "clause": clause, "title": f"t{clause}"}))
    return docs


@pytest.fixture
def client():
    c = QdrantClient(":memory:")
    ensure_collection(c, FAKE_CONFIG, collection="test")
    return c


def _index(client, docs, config=FAKE_CONFIG):
    embedder = FakeEmbedder(config)
    stats = index_documents(client, docs, config, embedder_factory=lambda: embedder, collection="test")
    return stats, embedder


def test_first_run_embeds_all_second_run_embeds_nothing(client):
    docs = _documents()
    stats, _ = _index(client, docs)
    assert (stats.embedded, stats.skipped, stats.deleted) == (len(docs), 0, 0)

    def must_not_load():
        raise AssertionError("model loaded although nothing changed")

    again = index_documents(client, docs, FAKE_CONFIG, embedder_factory=must_not_load, collection="test")
    assert (again.embedded, again.skipped, again.deleted) == (0, len(docs), 0)
    assert count_by_doc_type(client, "test") == {"product": 40, "policy": 3, "total": 43}


def test_changed_content_reembeds_only_that_document(client):
    docs = _documents()
    _index(client, docs)
    changed = [dataclasses.replace(docs[0], text=docs[0].text + " mới")] + docs[1:]
    stats, embedder = _index(client, changed)
    assert (stats.embedded, stats.skipped) == (1, len(docs) - 1) and embedder.encoded_texts == 1


def test_model_revision_change_forces_reembedding_of_everything(client):
    docs = _documents()
    _index(client, docs)
    new_config = dataclasses.replace(FAKE_CONFIG, revision="fake-2")
    assert new_config.fingerprint() != FAKE_CONFIG.fingerprint()
    stats, _ = _index(client, docs, new_config)
    assert (stats.embedded, stats.skipped) == (len(docs), 0)


@pytest.mark.parametrize("change", [{"max_length": 256}, {"sparse_drop_special_tokens": False}, {"dense_pooling": "mean"}])
def test_embedding_settings_are_part_of_the_skip_key(change):
    assert dataclasses.replace(FAKE_CONFIG, **change).fingerprint() != FAKE_CONFIG.fingerprint()


def test_payload_only_change_updates_payload_without_loading_the_model(client):
    docs = _documents()
    _index(client, docs)
    changed = [dataclasses.replace(docs[0], payload={**docs[0].payload, "kind": "Áo polo nam"})] + docs[1:]

    def must_not_load():
        raise AssertionError("model loaded for a payload-only change")

    stats = index_documents(client, changed, FAKE_CONFIG, embedder_factory=must_not_load, collection="test")
    assert (stats.embedded, stats.payload_updated, stats.skipped) == (0, 1, len(docs) - 1)
    point = client.retrieve("test", [docs[0].point_id], with_payload=True)[0]
    assert point.payload["kind"] == "Áo polo nam" and point.payload["payload_sha256"] == changed[0].payload_sha256
    again = index_documents(client, changed, FAKE_CONFIG, embedder_factory=must_not_load, collection="test")
    assert (again.payload_updated, again.skipped) == (0, len(docs))


def test_vanished_documents_are_deleted(client):
    docs = _documents()
    _index(client, docs)
    stats, _ = _index(client, docs[5:])
    assert stats.deleted == 5 and count_by_doc_type(client, "test")["total"] == len(docs) - 5


def test_point_ids_are_the_deterministic_document_ids(client):
    docs = _documents()
    _index(client, docs)
    stored = {str(p.id) for p in client.scroll("test", limit=100)[0]}
    assert stored == {d.point_id for d in docs}


def test_existing_collection_with_other_layout_is_refused(client):
    with pytest.raises(CollectionMismatch):
        ensure_collection(client, dataclasses.replace(FAKE_CONFIG, dense_dim=32), collection="test")


def test_manifest_is_not_rewritten_for_timestamp_only_changes(tmp_path):
    path = tmp_path / "manifest.json"
    base = {"collection": "c", "documents": {"total": 1}, "built_at": "2026-01-01T00:00:00+00:00"}
    assert write_manifest(base, path) is True
    assert write_manifest({**base, "built_at": "2026-02-02T00:00:00+00:00"}, path) is False
    assert json.loads(path.read_text())["built_at"] == "2026-01-01T00:00:00+00:00"
    assert write_manifest({**base, "documents": {"total": 2}}, path) is True
    assert write_manifest({**base, "built_at": "2026-03-03T00:00:00+00:00"}, path, force=True) is True


def _satisfies(payload: dict, f: SearchFilters) -> bool:
    checks = [
        f.doc_type is None or payload.get("doc_type") == f.doc_type,
        f.category is None or payload.get("category") == f.category,
        f.kind is None or payload.get("kind") == f.kind,
        f.color is None or payload.get("color") == f.color,
        f.material is None or payload.get("material") == f.material,
        f.size is None or f.size in payload.get("sizes", []),
        f.price_min is None or payload.get("price_vnd", -1) >= f.price_min,
        f.price_max is None or payload.get("price_vnd", 10**12) <= f.price_max,
        f.in_stock is None or payload.get("in_stock") == f.in_stock,
        f.file is None or payload.get("file") == f.file,
        f.clause is None or payload.get("clause") == f.clause,
    ]
    return all(checks)


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
def test_filters_are_enforced_inside_qdrant(client, mode):
    docs = _documents()
    _index(client, docs)
    retriever = Retriever(client, FakeEmbedder(), collection="test")
    rng = random.Random(11)
    for _ in range(60):
        f = SearchFilters(
            doc_type="product",
            kind=rng.choice([None, *KINDS]),
            color=rng.choice([None, *COLORS]),
            material=rng.choice([None, *MATERIALS]),
            size=rng.choice([None, *SIZE_POOL]),
            price_max=rng.choice([None, 400_000, 700_000]),
            in_stock=rng.choice([None, True, False]),
        )
        hits = retriever.search("áo đen cotton", f, k=10, mode=mode)
        expected = [d for d in docs if _satisfies(d.payload, f)]
        assert all(_satisfies(h.payload, f) for h in hits)
        assert len(hits) == min(10, len(expected))  # nothing matching is lost either


def test_policy_filters_and_hidden_payload(client):
    _index(client, _documents())
    hits = Retriever(client, FakeEmbedder(), collection="test").search("phí ship", {"doc_type": "policy", "clause": 2}, k=5)
    assert [h.key for h in hits] == ["policy:van-chuyen:2"]
    assert not {"text_sha256", "payload_sha256", "embedding_fingerprint"} & set(hits[0].payload) and hits[0].text


def test_search_input_validation(client):
    retriever = Retriever(client, FakeEmbedder(), collection="test")
    with pytest.raises(ValueError):
        retriever.search("x", k=0)
    with pytest.raises(ValueError):
        retriever.search("x", mode="bm25")
    with pytest.raises(ValueError):
        SearchFilters(price_min=500, price_max=100)
    with pytest.raises(ValueError):
        SearchFilters(keyword="xếp ly")  # not a filter (DECISIONS R14); unknown fields are rejected, not ignored
    assert SearchFilters().to_qdrant() is None
