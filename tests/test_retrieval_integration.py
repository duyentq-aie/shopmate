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
from app.retrieval.folding import tokenize
from app.retrieval.fusion import rrf_fuse
from app.retrieval.lexical import doc_vector, query_vector
from app.retrieval.manifest import file_sha256, load_manifest
from app.retrieval.search import PREFETCH_LIMIT, Retriever, SearchFilters, stable_key
from app.retrieval.store import (
    DENSE,
    FOLDED,
    SPARSE,
    connect,
    count_by_doc_type,
    lexical_fingerprint_counts,
    to_sparse_vector,
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


# Hand-written, unaccented and accented, none taken from the golden set.
TIE_QUERIES = ["ao thun nam", "quan jean nam", "giay the thao nu", "vi da nam", "tui xach nu", "balo laptop",
               "dong ho nam", "kinh mat", "that lung da", "mu luoi trai", "doi tra hang", "phi van chuyen",
               "bao hanh san pham", "ao khoac", "dam cong so", "vay lien", "ao so mi nam", "quan short", "giay sandal",
               "dep quai ngang", "ao len", "size m", "size 40", "mau den", "mau trang", "chat lieu cotton", "free ship",
               "hoan tien", "áo thun nam", "giày thể thao nữ", "túi xách nữ", "phí vận chuyển", "bảo hành sản phẩm",
               "đồng hồ nam", "quần short"]


@pytest.mark.skipif(LEXICAL is None, reason="the committed manifest is layout v1: there is no folded vector yet")
def test_the_live_index_passes_every_check_the_evaluation_runner_makes(client):
    from eval.run_retrieval_eval import index_problems

    assert index_problems(client, load_manifest(), EmbeddingConfig(), query_lexical=LEXICAL) == []


@pytest.mark.skipif(LEXICAL is None, reason="the committed manifest is layout v1: there is no folded vector yet")
def test_stored_folded_vectors_are_exactly_the_bm25_weights_of_their_text(client):
    """Scores are never modified: the folded vector of every point is pure BM25 of its stored text."""
    offset, checked = None, 0
    while True:
        points, offset = client.scroll(ACTIVE, limit=64, offset=offset, with_vectors=[FOLDED], with_payload=["text"])
        for point in points:
            stored = dict(zip(point.vector[FOLDED].indices, point.vector[FOLDED].values))
            expected = doc_vector(point.payload["text"], LEXICAL)
            assert stored.keys() == expected.keys() and all(
                stored[i] == pytest.approx(expected[i], rel=1e-6) for i in expected), point.id
            checked += 1
        if offset is None:
            break
    assert checked == load_manifest()["documents"]["total"]


def _dev_and_perturbation_questions() -> list[str]:
    from eval.golden.loader import load_split
    from eval.perturbation.dev_folded import load_dev_folded

    dev = load_split("dev")  # questions only: no metric is computed here
    return [i.question for i in dev if i.intent in ("product_search", "policy_question")] + \
        [i.question for i in load_dev_folded(dev)[0]]


def _ranking_invariants(retriever, client, query, branch):
    from qdrant_client import models as qm

    ranking = retriever.ranking(query, branch)
    keys = [stable_key(p.payload, str(p.id)) for p in ranking]
    for a, b, ka, kb in zip(ranking, ranking[1:], keys, keys[1:]):
        # different scores keep Qdrant's order (higher first); EXACT ties are ordered by ascending stable key
        assert a.score > b.score or (a.score == b.score and ka < kb), (query, branch, ka, kb, a.score, b.score)
    size = client.count(ACTIVE, exact=True).count
    embedding = retriever.embedder.encode_query(query)
    raw_query = {"dense": embedding.dense, "sparse": qm.SparseVector(indices=sorted(embedding.sparse),
                                                                    values=[embedding.sparse[i] for i in sorted(embedding.sparse)]),
                 "folded": to_sparse_vector(query_vector(query))}[branch]
    raw = client.query_points(ACTIVE, query=raw_query, using=branch, limit=size).points
    # nothing dropped, no score modified
    assert {str(p.id): p.score for p in ranking} == {str(p.id): p.score for p in raw}, (query, branch)
    return ranking


@pytest.mark.parametrize("branch", ["dense", "sparse"] + (["folded"] if LEXICAL else []))
def test_every_branch_ranking_orders_ties_by_stable_key_and_never_reorders_different_scores(retriever, client, branch):
    sizes = [len(_ranking_invariants(retriever, client, query, branch)) for query in TIE_QUERIES]
    # an empty ranking is legitimate (a query with no matching token), but most queries must match something
    assert sum(n > 0 for n in sizes) >= 20 and sum(n >= 2 for n in sizes) >= 20, sizes


@pytest.mark.skipif(LEXICAL is None, reason="the committed manifest is layout v1: there is no folded vector yet")
def test_folded_ranking_holds_the_invariants_for_every_dev_and_perturbation_query_and_is_complete(retriever, client):
    texts, offset = {}, None
    while True:
        points, offset = client.scroll(ACTIVE, limit=256, with_payload=["text"], offset=offset)
        texts.update({str(p.id): set(tokenize(p.payload["text"])) for p in points})
        if offset is None:
            break
    questions = _dev_and_perturbation_questions()
    assert len(questions) == 38 + 33
    for question in questions:
        ranking = _ranking_invariants(retriever, client, question, "folded")
        wanted = {i for i, tokens in texts.items() if tokens & set(tokenize(question))}
        assert {str(p.id) for p in ranking} == wanted, question  # every document sharing a token, and nothing else


@pytest.mark.skipif(LEXICAL is None, reason="the committed manifest is layout v1: there is no folded vector yet")
def test_ties_in_the_folded_branch_are_common_so_the_stable_key_rule_is_exercised(retriever, client):
    tied = sum(len({p.score for p in r}) < len(r) for r in (retriever.ranking(q, "folded") for q in TIE_QUERIES))
    assert tied >= 20, tied  # measured 53/54 hand-written queries: exact ties are the rule, not the exception


def test_client_side_rrf_equals_the_servers_fusion_on_a_fixed_fixture():
    """The formula is Qdrant's own (1 / (rank0 + 2) per branch, window 50). If an upgrade of the server changes
    it, this fails. Fixture: 60 points; dense ranks them 1..60, sparse ranks them 60..1; both windows cut at 50."""
    import math
    import uuid

    from qdrant_client import models as qm

    name = f"zz_rrf_fixture_{uuid.uuid4().hex[:8]}"
    c = connect()
    c.create_collection(name, vectors_config={"dense": qm.VectorParams(size=2, distance=qm.Distance.COSINE)},
                        sparse_vectors_config={"sparse": qm.SparseVectorParams()})
    try:
        c.upsert(name, wait=True, points=[
            qm.PointStruct(id=i, vector={"dense": [math.cos(math.radians(i)), math.sin(math.radians(i))],
                                         "sparse": qm.SparseVector(indices=[0], values=[i / 10])}) for i in range(1, 61)])
        dense_query, sparse_query = [1.0, 0.0], qm.SparseVector(indices=[0], values=[1.0])
        server = c.query_points(
            name, prefetch=[qm.Prefetch(query=dense_query, using="dense", limit=PREFETCH_LIMIT),
                            qm.Prefetch(query=sparse_query, using="sparse", limit=PREFETCH_LIMIT)],
            query=qm.FusionQuery(fusion=qm.Fusion.RRF), limit=60).points
        # by hand: doc i is dense rank0 i-1 (inside the window if i <= 50) and sparse rank0 60-i (inside if i >= 11)
        by_hand = {i: (1 / (i + 1) if i <= 50 else 0.0) + (1 / (62 - i) if i >= 11 else 0.0) for i in range(1, 61)}
        assert len(server) == 60 and all(abs(p.score - by_hand[p.id]) < 1e-6 for p in server)
        rankings = [[str(i) for i in range(1, 61)], [str(i) for i in range(60, 0, -1)]]
        fused = rrf_fuse(rankings, window=PREFETCH_LIMIT)
        assert {int(i): float(v) for i, v in fused.items()} == pytest.approx(by_hand)
        assert all(abs(float(fused[str(p.id)]) - p.score) < 1e-6 for p in server)
    finally:
        c.delete_collection(name)


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
