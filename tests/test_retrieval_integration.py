"""Against the REAL index: running Qdrant, cached bge-m3, Saleor channel vn (read-only).

Run with `uv run pytest -m integration` (or scripts/test_all.sh). Smoke queries are written for
these tests; the golden set is never used here.

The collection under test and the modes come from the committed manifest (tests/active_index.py):
layout v1 gives dense and hybrid; layout v2 (Phase 4) also checks the folded vector and the two folded
modes, and that the baseline vectors are still the ones copied from the earlier collection.
"""

from __future__ import annotations

import functools
import random

import pytest

from app.catalog.schema import load_catalog
from app.config import api_url, require_setting
from app.policies import POLICY_DOCUMENTS, clauses
from app.retrieval.documents import fetch_product_documents, policy_documents
from app.retrieval.embedder import EmbeddingConfig
from app.retrieval.indexer import index_documents
from app.retrieval.manifest import file_sha256, load_manifest
from app.retrieval.search import Retriever, SearchFilters
from app.retrieval.store import (
    DENSE,
    FOLDED,
    SPARSE,
    connect,
    count_by_doc_type,
    lexical_fingerprint_counts,
    vectors_digest,
)
from app.saleor.readonly_client import ReadOnlySaleorClient
from app.catalog.schema import CATALOG_PATH
from app.policies import policy_path
from qdrant_client import models
from tests.active_index import ACTIVE, LEXICAL, MODES

pytestmark = pytest.mark.integration

FILTER_ON_PROBABILITY = 0.2  # 156/200 combinations match (1056 hits, 10 with a kind filter); the 44 empty ones test "no false hits". Every kind is also covered by test_kind_filter_for_every_catalog_kind.

SMOKE_QUERIES = [
    "áo thun nam màu đen",
    "giay chay bo size 42",
    "túi xách da bò cho nữ",
    "shop ơi có váy đi biển ko",
    "phí vận chuyển bao nhiêu",
    "doi tra trong may ngay",
]


@pytest.fixture(scope="module")
def client():
    c = connect()
    assert c.collection_exists(ACTIVE), f"index {ACTIVE} missing: run `uv run python -m scripts.build_index`"
    return c


class _MemoEmbedder:
    """Embeds each distinct query once, so the property test stays fast."""

    def __init__(self, embedder):
        self.config = embedder.config
        self._embedder = embedder
        self.encode_query = functools.cache(embedder.encode_query)


@pytest.fixture(scope="module")
def retriever(client, embedder):
    return Retriever(client, _MemoEmbedder(embedder), collection=ACTIVE)


def test_manifest_matches_the_actual_index(client):
    manifest = load_manifest()
    config = EmbeddingConfig()
    assert manifest["collection"] == ACTIVE
    assert manifest["embedding"]["fingerprint"] == config.fingerprint()
    assert manifest["embedding"]["revision"] == config.revision
    assert manifest["documents"] == count_by_doc_type(client, ACTIVE)
    assert "kind" in manifest["filterable_payload"] and "keyword" not in manifest["filterable_payload"]
    assert "kind" in (client.get_collection(ACTIVE).payload_schema or {})
    assert manifest["documents"]["product"] == len(load_catalog().products)
    assert manifest["documents"]["policy"] == sum(len(clauses(d)) for d in POLICY_DOCUMENTS)
    params = client.get_collection(ACTIVE).config.params
    assert params.vectors[DENSE].size == manifest["vectors"][DENSE]["size"]
    assert SPARSE in params.sparse_vectors
    assert manifest["sources"]["catalog"]["sha256"] == file_sha256(CATALOG_PATH)
    for name in POLICY_DOCUMENTS:
        assert manifest["sources"]["policies"][name]["sha256"] == file_sha256(policy_path(name))
    fingerprints = set()
    offset = None
    while True:
        points, offset = client.scroll(ACTIVE, limit=256, offset=offset, with_payload=["embedding_fingerprint"])
        fingerprints |= {p.payload["embedding_fingerprint"] for p in points}
        if offset is None:
            break
    assert fingerprints == {config.fingerprint()}


@pytest.mark.skipif(LEXICAL is None, reason="the committed manifest is layout v1: there is no folded vector yet")
def test_v2_manifest_matches_the_folded_vector_and_the_baseline_vectors(client):
    manifest = load_manifest()
    assert manifest["lexical"]["fingerprint"] == LEXICAL.fingerprint()
    assert manifest["vectors"][FOLDED] == {"modifier": "idf"}
    folded = client.get_collection(ACTIVE).config.params.sparse_vectors[FOLDED]
    assert folded.modifier == models.Modifier.IDF
    assert lexical_fingerprint_counts(client, ACTIVE) == {LEXICAL.fingerprint(): manifest["documents"]["total"]}
    assert vectors_digest(client, ACTIVE) == manifest["vectors_digest"]
    source = manifest["derived_from"]["collection"]
    if client.collection_exists(source):  # the frozen Phase 3A/3B collection: baseline vectors must be untouched
        assert vectors_digest(client, source) == manifest["derived_from"]["vectors_digest"] == manifest["vectors_digest"]


def test_reindexing_leaves_point_counts_unchanged(client):
    kinds = {p.sku: p.kind for p in load_catalog().products}
    with ReadOnlySaleorClient(api_url(), require_setting("SALEOR_RUNTIME_TOKEN")) as runtime:
        documents = fetch_product_documents(runtime, kinds) + policy_documents()
    before = count_by_doc_type(client, ACTIVE)

    def must_not_load():
        raise AssertionError("re-index tried to load the model: some document changed")

    stats = index_documents(client, documents, EmbeddingConfig(), embedder_factory=must_not_load, collection=ACTIVE,
                            lexical=LEXICAL)
    assert (stats.embedded, stats.payload_updated, stats.deleted, stats.skipped) == (0, 0, 0, len(documents))
    assert count_by_doc_type(client, ACTIVE) == before


@pytest.mark.parametrize("mode", MODES)
def test_smoke_queries_return_results(retriever, mode):
    for query in SMOKE_QUERIES:
        hits = retriever.search(query, k=5, mode=mode)
        assert len(hits) == 5, query
        assert all(h.key.startswith(("product:", "policy:")) for h in hits)
    policy_hits = retriever.search("phí vận chuyển bao nhiêu", {"doc_type": "policy"}, k=3, mode=mode)
    assert policy_hits and all(h.payload["doc_type"] == "policy" for h in policy_hits)


def _catalog_view() -> dict[str, dict]:
    return {p.sku: {"doc_type": "product", "category": p.category, "kind": p.kind, "color": p.color, "material": p.material,
                    "sizes": p.sizes, "price_vnd": p.price, "in_stock": p.stock > 0} for p in load_catalog().products}


def _policy_view() -> dict[tuple[str, int], dict]:
    return {(d, n): {"doc_type": "policy", "file": d, "clause": n} for d in POLICY_DOCUMENTS for n in clauses(d)}


def _truth(hit) -> dict:
    """The facts about a hit taken from the SOURCE FILES (catalog / policies), not from Qdrant's payload."""
    if hit.key.startswith("product:"):
        return _catalog_view()[hit.key.removeprefix("product:")]
    _, file, clause = hit.key.split(":")
    return _policy_view()[(file, int(clause))]


def _satisfies(truth: dict, f: SearchFilters) -> bool:
    return all([
        f.doc_type is None or truth["doc_type"] == f.doc_type,
        f.category is None or truth.get("category") == f.category,
        f.kind is None or truth.get("kind") == f.kind,
        f.color is None or truth.get("color") == f.color,
        f.material is None or truth.get("material") == f.material,
        f.size is None or f.size in truth.get("sizes", []),
        f.price_min is None or truth.get("price_vnd", -1) >= f.price_min,
        f.price_max is None or truth.get("price_vnd", 10**12) <= f.price_max,
        f.in_stock is None or truth.get("in_stock") == f.in_stock,
        f.file is None or truth.get("file") == f.file,
        f.clause is None or truth.get("clause") == f.clause,
    ])


def _random_filters(rng: random.Random) -> SearchFilters:
    catalog = load_catalog()
    if rng.random() < 0.15:
        file = rng.choice(POLICY_DOCUMENTS)
        return SearchFilters(doc_type="policy", file=file,
                             clause=rng.choice([None, *clauses(file)]))
    def pick(values):  # each filter field is on with FILTER_ON_PROBABILITY, so most combinations still match
        return rng.choice(values) if rng.random() < FILTER_ON_PROBABILITY else None

    low = pick([100_000, 200_000, 300_000, 500_000])
    high = pick([300_000, 500_000, 800_000, 1_500_000])
    if low is not None and high is not None and low > high:
        low, high = high, low
    return SearchFilters(
        doc_type=pick(["product"]),
        category=pick(sorted({p.category for p in catalog.products})),
        kind=pick(sorted({p.kind for p in catalog.products})),
        color=pick(sorted({p.color for p in catalog.products})),
        material=pick(sorted({p.material for p in catalog.products})),
        size=pick(["S", "M", "XL", "30", "38", "41", "Freesize"]),
        price_min=low,
        price_max=high,
        in_stock=pick([True, False]),
    )


@pytest.mark.parametrize("mode", MODES)
def test_every_result_satisfies_random_filters(retriever, mode):
    rng = random.Random(20261009)
    checked = non_empty = kind_non_empty = 0
    for i in range(200):
        f = _random_filters(rng)
        query = SMOKE_QUERIES[i % len(SMOKE_QUERIES)]
        hits = retriever.search(query, f, k=10, mode=mode)
        for hit in hits:
            assert _satisfies(_truth(hit), f), (mode, query, f, hit.key)
        expected = sum(_satisfies(t, f) for t in [*_catalog_view().values(), *_policy_view().values()])
        assert len(hits) == min(10, expected), (mode, f, len(hits), expected)  # nothing matching is lost
        checked += len(hits)
        non_empty += bool(hits)
        kind_non_empty += bool(hits) and f.kind is not None
    # The property is only meaningful if most combinations actually return results.
    assert non_empty >= 100 and checked >= 1000 and kind_non_empty >= 10, (non_empty, checked, kind_non_empty)


@pytest.mark.parametrize("mode", MODES)
def test_kind_filter_for_every_catalog_kind(retriever, mode):
    """Every kind in the catalog: all hits have that kind (per the catalog file) and none is lost."""
    catalog = _catalog_view()
    kinds = sorted({v["kind"] for v in catalog.values()})
    assert len(kinds) >= 30
    for i, kind in enumerate(kinds):
        f = SearchFilters(kind=kind)
        hits = retriever.search(SMOKE_QUERIES[i % len(SMOKE_QUERIES)], f, k=10, mode=mode)
        assert all(_truth(h).get("kind") == kind for h in hits), (mode, kind)
        assert len(hits) == min(10, sum(v["kind"] == kind for v in catalog.values())), (mode, kind)
