"""The folded lexical vector in the store, indexer and retriever (Phase 4, experiment 1).

In-memory Qdrant (it supports the IDF modifier and partial vector updates, verified) with the fake
embedder: no services, no model. Covers what must stay true for the experiment to mean anything:
the baseline vectors are never touched, the new vector is the only thing computed, every
configuration queries exactly the branches it is defined by, and filters hold on every branch.
"""

from __future__ import annotations

import dataclasses
import random

import pytest
from qdrant_client import QdrantClient, models

from app.retrieval import search as search_module
from app.retrieval.documents import Document
from app.retrieval.embedder import Embedding
from app.retrieval.indexer import SeedMismatch, index_documents
from app.retrieval.lexical import LexicalConfig, TieBreakResolution, TokenIdCollision, doc_vector, query_vector, tie_factor
from app.retrieval.manifest import active_collection, build_manifest
from app.retrieval.search import PREFETCH_LIMIT, Retriever, SearchFilters, fetch_limit, stable_key
from app.retrieval.store import (
    DENSE,
    FOLDED,
    SPARSE,
    CollectionMismatch,
    count_by_doc_type,
    ensure_collection,
    lexical_fingerprint_counts,
    to_sparse_vector,
    vectors_digest,
)
from tests.fake_embedder import FAKE_CONFIG, FakeEmbedder
from tests.test_retrieval_unit import COLORS, KINDS, MATERIALS, SIZE_POOL, _documents, _satisfies

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")

LEX = LexicalConfig()
NEW_MODES = ["dense_sparse_folded", "dense_folded"]
COLLIDING = ("w52890", "w55286")  # a real 32-bit blake2b collision


class ConstantEmbedder:
    """Every text and query gets the same dense vector and no sparse weights: the bge-m3 branches
    cannot discriminate, so any ranking difference comes from the folded branch alone."""

    def __init__(self, config=FAKE_CONFIG):
        self.config = config

    def _one(self) -> Embedding:
        return Embedding(dense=[1.0] + [0.0] * (self.config.dense_dim - 1), sparse={})

    def encode(self, texts):
        return [self._one() for _ in texts]

    def encode_query(self, text):
        return self._one()


class RecordingClient:
    """Real client underneath; remembers the arguments of every query_points call."""

    def __init__(self, client):
        self._client, self.calls = client, []

    def query_points(self, collection, **kwargs):
        self.calls.append(kwargs)
        return self._client.query_points(collection, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _expected_folded(doc, docs, lexical=LEX):
    """The stored folded weights of `doc` among `docs`: pure BM25 scaled by the tie factor of its stable-key rank."""
    ranked = [d.point_id for d in sorted(docs, key=lambda d: stable_key(d.payload, d.key))]
    factor = tie_factor(ranked.index(doc.point_id), len(docs), lexical)
    return {i: w * factor for i, w in doc_vector(doc.text, lexical).items()}


class UpsertRecorder:
    """Real client underneath; remembers the point ids of every upsert, in order."""

    def __init__(self, client):
        self._client, self.ids = client, []

    def upsert(self, collection, points, **kwargs):
        self.ids.extend(str(p.id) for p in points)
        return self._client.upsert(collection, points=points, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _must_not_load():
    raise AssertionError("the embedding model was loaded although nothing needed embedding")


def _new_collection(name="v2", lexical=True):
    client = QdrantClient(":memory:")
    ensure_collection(client, FAKE_CONFIG, collection=name, lexical=lexical)
    return client


def _index(client, docs, collection="v2", lexical=LEX, config=FAKE_CONFIG, embedder=None, **kwargs):
    embedder = embedder or FakeEmbedder(config)
    stats = index_documents(client, docs, config, embedder_factory=lambda: embedder, collection=collection,
                            lexical=lexical, **kwargs)
    return stats, embedder


def _vectors(client, collection, doc):
    return client.retrieve(collection, [doc.point_id], with_vectors=True)[0].vector


def _sparse_dict(vector: models.SparseVector) -> dict[int, float]:
    return dict(zip(vector.indices, vector.values))


# --- layout ------------------------------------------------------------------------------------


def test_folded_vector_is_created_with_the_idf_modifier_next_to_the_baseline_vectors():
    params = _new_collection().get_collection("v2").config.params
    assert set(params.sparse_vectors) == {SPARSE, FOLDED} and DENSE in params.vectors
    assert params.sparse_vectors[FOLDED].modifier == models.Modifier.IDF  # BM25 needs the IDF factor
    assert params.sparse_vectors[SPARSE].modifier in (None, models.Modifier.NONE)  # baseline layout untouched


def test_legacy_layout_has_no_folded_vector():
    params = _new_collection("old", lexical=False).get_collection("old").config.params
    assert set(params.sparse_vectors) == {SPARSE}


def test_a_collection_whose_layout_differs_from_the_requested_one_is_refused():
    with pytest.raises(CollectionMismatch):
        ensure_collection(_new_collection("old", lexical=False), FAKE_CONFIG, collection="old", lexical=True)
    with pytest.raises(CollectionMismatch):
        ensure_collection(_new_collection("v2", lexical=True), FAKE_CONFIG, collection="v2", lexical=False)
    # same layout again is fine and creates nothing
    assert ensure_collection(_new_collection("v2"), FAKE_CONFIG, collection="v2", lexical=True) is False


# --- indexing ----------------------------------------------------------------------------------


def test_first_run_writes_three_vectors_and_the_lexical_fingerprint_second_run_does_nothing():
    client, docs = _new_collection(), _documents()
    stats, _ = _index(client, docs)
    assert (stats.embedded, stats.lexical_computed, stats.skipped, stats.copied) == (43, 43, 0, 0)
    vector = _vectors(client, "v2", docs[0])
    assert set(vector) == {DENSE, SPARSE, FOLDED}
    assert _sparse_dict(vector[FOLDED]) == pytest.approx(_expected_folded(docs[0], docs))  # weights as defined
    assert lexical_fingerprint_counts(client, "v2") == {LEX.fingerprint(): 43}

    again = index_documents(client, docs, FAKE_CONFIG, embedder_factory=_must_not_load, collection="v2", lexical=LEX)
    assert (again.embedded, again.lexical_computed, again.payload_updated, again.skipped) == (0, 0, 0, 43)


def test_changing_the_lexical_config_recomputes_only_the_folded_vector():
    client, docs = _new_collection(), _documents()
    _index(client, docs)
    before = {d.point_id: _vectors(client, "v2", d) for d in docs}
    baseline_digest = vectors_digest(client, "v2")

    other = dataclasses.replace(LEX, k1=1.5)
    stats = index_documents(client, docs, FAKE_CONFIG, embedder_factory=_must_not_load, collection="v2", lexical=other)

    assert (stats.embedded, stats.lexical_computed, stats.skipped) == (0, 43, 0)
    assert vectors_digest(client, "v2") == baseline_digest  # dense + sparse bit-identical
    after = _vectors(client, "v2", docs[0])
    assert _sparse_dict(after[FOLDED]) != _sparse_dict(before[docs[0].point_id][FOLDED])
    assert _sparse_dict(after[FOLDED]) == pytest.approx(_expected_folded(docs[0], docs, other))
    assert lexical_fingerprint_counts(client, "v2") == {other.fingerprint(): 43}


def test_changed_text_recomputes_all_three_vectors_of_that_document_only():
    client, docs = _new_collection(), _documents()
    _index(client, docs)
    changed = [dataclasses.replace(docs[0], text=docs[0].text + " mới")] + docs[1:]
    stats, embedder = _index(client, changed)
    assert (stats.embedded, stats.lexical_computed, stats.skipped) == (1, 1, 42) and embedder.encoded_texts == 1
    assert _sparse_dict(_vectors(client, "v2", docs[0])[FOLDED]) == pytest.approx(_expected_folded(changed[0], changed))


def test_payload_only_change_keeps_the_lexical_fingerprint():
    client, docs = _new_collection(), _documents()
    _index(client, docs)
    changed = [dataclasses.replace(docs[0], payload={**docs[0].payload, "kind": "Áo polo nam"})] + docs[1:]
    stats = index_documents(client, changed, FAKE_CONFIG, embedder_factory=_must_not_load, collection="v2", lexical=LEX)
    assert (stats.embedded, stats.lexical_computed, stats.payload_updated) == (0, 0, 1)
    assert lexical_fingerprint_counts(client, "v2") == {LEX.fingerprint(): 43}


def test_token_id_collision_aborts_before_anything_is_written():
    client = _new_collection()
    docs = _documents()[:3] + [Document("product:SM-NAM-9999", f"áo {COLLIDING[0]} {COLLIDING[1]}",
                                        {"doc_type": "product", "sku": "SM-NAM-9999"})]
    with pytest.raises(TokenIdCollision):
        _index(client, docs)
    assert count_by_doc_type(client, "v2")["total"] == 0


def test_legacy_indexing_is_unchanged_and_writes_no_lexical_data():
    client, docs = _new_collection("old", lexical=False), _documents()
    stats, _ = _index(client, docs, collection="old", lexical=None)
    assert (stats.embedded, stats.lexical_computed) == (43, 0)
    assert set(_vectors(client, "old", docs[0])) == {DENSE, SPARSE}
    assert lexical_fingerprint_counts(client, "old") == {"": 43}


# --- deterministic tie-break: exact ties resolve to the smaller stable key ---------------------------------
# Measured on the real server: Qdrant orders exactly tied scores arbitrarily, differently for every build and
# every `limit`, NOT by insertion order. The folded weights are therefore scaled by the tie factor of the
# document's stable-key rank (app/retrieval/lexical.py).


def test_stored_weights_by_hand_first_rank_is_unscaled_last_rank_is_scaled_by_its_factor():
    client, docs = _new_collection(), _documents()
    _index(client, docs)
    # 43 documents; "policy:..." sorts before "product:...": policy clause 1 is rank 0, the last SKU rank 42
    first = next(d for d in docs if d.key == "policy:van-chuyen:1")
    last = next(d for d in docs if d.key == "product:SM-NAM-0039")
    assert _sparse_dict(_vectors(client, "v2", first)[FOLDED]) == pytest.approx(doc_vector(first.text, LEX))
    scale = 1 - 1e-3 * 42 / 43
    assert _sparse_dict(_vectors(client, "v2", last)[FOLDED]) == pytest.approx(
        {i: w * scale for i, w in doc_vector(last.text, LEX).items()})
    ties = {d.key: client.retrieve("v2", [d.point_id], with_payload=["lexical_tie"])[0].payload["lexical_tie"] for d in docs}
    assert ties["policy:van-chuyen:1"] == "0/43" and ties["product:SM-NAM-0000"] == "3/43" and ties["product:SM-NAM-0039"] == "42/43"


def test_exact_ties_resolve_to_the_smaller_stable_key_in_every_build_whatever_the_input_order():
    twelve = [Document(f"product:SM-NAM-{n:04d}", "áo giống hệt nhau", {"doc_type": "product", "sku": f"SM-NAM-{n:04d}"})
              for n in range(1, 13)]
    query = to_sparse_vector(query_vector("ao giong het nhau"))
    lists = []
    for seed in (1, 2, 3):  # three independent builds from differently shuffled input
        client = _new_collection()
        _index(client, random.Random(seed).sample(twelve, 12), embedder=ConstantEmbedder())
        points = client.query_points("v2", query=query, using=FOLDED, limit=12, with_payload=["key"]).points
        assert len({p.score for p in points}) == 12  # no exact tie is left
        lists.append([p.payload["key"] for p in points])
    assert lists[0] == lists[1] == lists[2] == sorted(lists[0])  # smaller stable key first, in every build


def test_points_are_inserted_in_stable_key_order_with_and_without_a_seed():
    docs = _documents()
    expected = [d.point_id for d in sorted(docs, key=lambda d: stable_key(d.payload, d.key))]
    shuffled = random.Random(5).sample(docs, len(docs))
    fresh = UpsertRecorder(_new_collection())
    _index(fresh, shuffled)
    assert fresh.ids == expected
    client, _ = _seeded()
    seeded = UpsertRecorder(client)
    index_documents(seeded, shuffled, FAKE_CONFIG, embedder_factory=_must_not_load, collection="v2", lexical=LEX,
                    seed_from="old")
    assert seeded.ids == expected


def test_a_corpus_change_that_shifts_ranks_rewrites_only_the_folded_vectors():
    client, docs = _new_collection(), _documents()
    _index(client, docs)
    first = Document("policy:aaa:1", "mới nhất", {"doc_type": "policy", "file": "aaa", "clause": 1})  # sorts before all
    stats, embedder = _index(client, [first, *docs])
    assert (stats.embedded, stats.lexical_computed, stats.skipped) == (1, 44, 0) and embedder.encoded_texts == 1
    assert _sparse_dict(_vectors(client, "v2", docs[0])[FOLDED]) == pytest.approx(_expected_folded(docs[0], [first, *docs]))
    ties = client.retrieve("v2", [docs[0].point_id], with_payload=["lexical_tie"])[0].payload["lexical_tie"]
    assert ties == "4/44"  # preceded by policy:aaa:1 and the three van-chuyen clauses
    removed = index_documents(client, docs, FAKE_CONFIG, embedder_factory=_must_not_load, collection="v2", lexical=LEX)
    assert (removed.embedded, removed.lexical_computed, removed.deleted) == (0, 43, 1)
    assert client.retrieve("v2", [docs[0].point_id], with_payload=["lexical_tie"])[0].payload["lexical_tie"] == "3/43"


def test_a_tie_break_that_float32_could_not_resolve_aborts_before_anything_is_written():
    client = _new_collection()
    with pytest.raises(TieBreakResolution):
        _index(client, _documents(), lexical=LexicalConfig(tie_delta=1e-9))  # 1e-9/43 relative gap
    assert count_by_doc_type(client, "v2")["total"] == 0


def test_legacy_points_carry_no_tie_rank():
    client, docs = _new_collection("old", lexical=False), _documents()
    _index(client, docs, collection="old", lexical=None)
    assert "lexical_tie" not in client.retrieve("old", [docs[0].point_id], with_payload=True)[0].payload


# --- seeding the new collection from the old one (no model) ---------------------------------------


def _seeded(docs=None):
    """An old-layout collection plus an empty v2 collection in the same in-memory Qdrant."""
    client = QdrantClient(":memory:")
    ensure_collection(client, FAKE_CONFIG, collection="old", lexical=False)
    ensure_collection(client, FAKE_CONFIG, collection="v2", lexical=True)
    docs = docs or _documents()
    _index(client, docs, collection="old", lexical=None)
    return client, docs


def test_seeding_copies_dense_and_sparse_bit_for_bit_and_computes_only_the_folded_vector():
    client, docs = _seeded()
    stats = index_documents(client, docs, FAKE_CONFIG, embedder_factory=_must_not_load, collection="v2",
                            lexical=LEX, seed_from="old")
    assert (stats.copied, stats.embedded, stats.lexical_computed, stats.skipped) == (43, 0, 43, 0)
    assert vectors_digest(client, "v2") == vectors_digest(client, "old") == stats.seed_digest
    for doc in docs:
        old, new = _vectors(client, "old", doc), _vectors(client, "v2", doc)
        assert new[DENSE] == old[DENSE]
        assert _sparse_dict(new[SPARSE]) == _sparse_dict(old[SPARSE])
        assert _sparse_dict(new[FOLDED]) == pytest.approx(_expected_folded(doc, docs))
    assert count_by_doc_type(client, "v2") == count_by_doc_type(client, "old")
    assert count_by_doc_type(client, "v2") == {"product": 40, "policy": 3, "total": 43}
    again = index_documents(client, docs, FAKE_CONFIG, embedder_factory=_must_not_load, collection="v2",
                            lexical=LEX, seed_from="old")
    assert (again.copied, again.embedded, again.lexical_computed, again.skipped) == (0, 0, 0, 43)


def test_seeding_does_not_copy_a_point_whose_text_changed():
    client, docs = _seeded()
    changed = [dataclasses.replace(docs[0], text=docs[0].text + " mới")] + docs[1:]
    embedder = FakeEmbedder()
    stats = index_documents(client, changed, FAKE_CONFIG, embedder_factory=lambda: embedder, collection="v2",
                            lexical=LEX, seed_from="old")
    assert (stats.copied, stats.embedded) == (42, 1) and embedder.encoded_texts == 1


def test_seeding_does_not_copy_points_embedded_with_another_model_config():
    client, docs = _seeded()
    other = dataclasses.replace(FAKE_CONFIG, revision="fake-2")
    embedder = FakeEmbedder(other)
    stats = index_documents(client, docs, other, embedder_factory=lambda: embedder, collection="v2",
                            lexical=LEX, seed_from="old")
    assert (stats.copied, stats.embedded) == (0, 43)


def test_seeding_needs_a_lexical_config():
    client, docs = _seeded()
    with pytest.raises(ValueError, match="lexical"):
        index_documents(client, docs, FAKE_CONFIG, embedder_factory=_must_not_load, collection="v2",
                        lexical=None, seed_from="old")


def test_a_corrupted_copy_is_caught_by_the_digest_check():
    client, docs = _seeded()

    class CorruptingClient:
        def __init__(self, inner):
            self._inner = inner

        def upsert(self, collection, points, **kwargs):
            points[0].vector[DENSE] = [points[0].vector[DENSE][0] + 1e-3, *points[0].vector[DENSE][1:]]
            return self._inner.upsert(collection, points=points, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    with pytest.raises(SeedMismatch):
        index_documents(CorruptingClient(client), docs, FAKE_CONFIG, embedder_factory=_must_not_load,
                        collection="v2", lexical=LEX, seed_from="old")


def test_the_vectors_digest_covers_dense_and_sparse_but_not_the_folded_vector():
    client, docs = _seeded()
    index_documents(client, docs, FAKE_CONFIG, embedder_factory=_must_not_load, collection="v2", lexical=LEX,
                    seed_from="old")
    digest = vectors_digest(client, "v2")
    assert vectors_digest(client, "v2", ids=[docs[0].point_id]) != digest  # a subset is a different digest
    index_documents(client, docs, FAKE_CONFIG, embedder_factory=_must_not_load, collection="v2",
                    lexical=dataclasses.replace(LEX, b=0.5))
    assert vectors_digest(client, "v2") == digest  # folded vectors changed, digest did not
    client.update_vectors("v2", points=[models.PointVectors(id=docs[1].point_id, vector={SPARSE: models.SparseVector(
        indices=[1], values=[0.5])})])
    assert vectors_digest(client, "v2") != digest


# --- retrieval ---------------------------------------------------------------------------------------

TUI = Document("product:SM-NAM-0009", "Túi đeo chéo da bò", {"doc_type": "product", "sku": "SM-NAM-0009"})
GIAY = Document("product:SM-NAM-0001", "Giày chạy bộ nhẹ", {"doc_type": "product", "sku": "SM-NAM-0001"})
VI = Document("product:SM-NAM-0002", "Ví da nam màu đen", {"doc_type": "product", "sku": "SM-NAM-0002"})


def _tiny_retriever():
    client = _new_collection()
    embedder = ConstantEmbedder()
    _index(client, [TUI, GIAY, VI], embedder=embedder)
    return Retriever(client, embedder, collection="v2")


@pytest.mark.parametrize("mode", NEW_MODES)
@pytest.mark.parametrize("query", ["tui deo cheo", "Túi đeo chéo", "TUI ĐEO CHEO"])
def test_unaccented_and_accented_queries_find_the_accented_document_through_the_folded_branch(mode, query):
    hits = _tiny_retriever().search(query, k=3, mode=mode)
    assert hits[0].key == "product:SM-NAM-0009"


def test_without_the_folded_branch_the_same_query_does_not_find_it():
    # Control for the test above. The constant embedder cannot discriminate, so dense scores tie
    # exactly and R16 orders by stable key: the "túi" document (SM-NAM-0009) sorts last. (Mode
    # "dense" on purpose: RRF turns a tie into arbitrary distinct ranks, so it has no such order.)
    hits = _tiny_retriever().search("tui deo cheo", k=3, mode="dense")
    assert [h.key for h in hits] == ["product:SM-NAM-0001", "product:SM-NAM-0002", "product:SM-NAM-0009"]


def test_the_lexical_idf_makes_a_rare_token_outweigh_a_common_one():
    docs = [Document(f"product:SM-NAM-{n:04d}", text, {"doc_type": "product", "sku": f"SM-NAM-{n:04d}"})
            for n, text in enumerate(["áo thun nam", "áo sơ mi nam", "áo khoác nam", "kính mát nam", "áo len nam"], 1)]
    client = _new_collection()
    embedder = ConstantEmbedder()
    _index(client, docs, embedder=embedder)
    # "nam" is in every document, "kinh" only in one: the rare token decides
    hits = Retriever(client, embedder, collection="v2").search("ao kinh nam", k=5, mode="dense_folded")
    assert hits[0].key == "product:SM-NAM-0004"


@pytest.mark.parametrize(
    "mode, using",
    [("hybrid", [DENSE, SPARSE]), ("dense_sparse_folded", [DENSE, SPARSE, FOLDED]), ("dense_folded", [DENSE, FOLDED])],
)
def test_each_configuration_queries_exactly_its_branches_and_fuses_with_rrf(mode, using):
    client = _new_collection()
    docs = _documents()
    _index(client, docs)
    recording = RecordingClient(client)
    flt = SearchFilters(doc_type="product", in_stock=True)
    Retriever(recording, FakeEmbedder(), collection="v2").search("áo đen cotton", flt, k=10, mode=mode)
    (call,) = recording.calls
    assert [p.using for p in call["prefetch"]] == using
    assert all(p.limit == PREFETCH_LIMIT and p.filter == flt.to_qdrant() for p in call["prefetch"])
    assert call["query"] == models.FusionQuery(fusion=models.Fusion.RRF)
    assert call["limit"] == fetch_limit(10) and call["query_filter"] == flt.to_qdrant()


def test_dense_mode_still_runs_a_single_dense_query():
    client = _new_collection()
    _index(client, _documents())
    recording = RecordingClient(client)
    Retriever(recording, FakeEmbedder(), collection="v2").search("áo đen", k=5, mode="dense")
    (call,) = recording.calls
    assert call["using"] == DENSE and "prefetch" not in call


@pytest.mark.parametrize("mode", ["hybrid", *NEW_MODES])
def test_a_query_without_tokens_drops_the_lexical_branches_but_still_answers(mode):
    client = _new_collection()
    _index(client, _documents())
    recording = RecordingClient(client)
    hits = Retriever(recording, FakeEmbedder(), collection="v2").search("???", k=5, mode=mode)
    (call,) = recording.calls
    assert [p.using for p in call["prefetch"]] == [DENSE] and len(hits) == 5


@pytest.mark.parametrize("mode", NEW_MODES)
def test_filters_are_enforced_on_every_branch_of_the_new_modes(mode):
    client, docs = _new_collection(), _documents()
    _index(client, docs)
    retriever = Retriever(client, FakeEmbedder(), collection="v2")
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
        hits = retriever.search("ao den cotton", f, k=10, mode=mode)
        expected = [d for d in docs if _satisfies(d.payload, f)]
        assert all(_satisfies(h.payload, f) for h in hits)
        assert len(hits) == min(10, len(expected))


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
def test_the_folded_vector_does_not_change_baseline_results(mode):
    """Dense and hybrid on a collection that also holds the folded vector return exactly what they
    return on the legacy collection: same keys, same scores."""
    client, docs = _seeded()
    index_documents(client, docs, FAKE_CONFIG, embedder_factory=_must_not_load, collection="v2", lexical=LEX,
                    seed_from="old")
    old = Retriever(client, FakeEmbedder(), collection="old")
    new = Retriever(client, FakeEmbedder(), collection="v2")
    for query in ["áo đen cotton", "phí ship", "giay 40", "sản phẩm 7"]:
        for flt in (None, {"doc_type": "policy"}, {"doc_type": "product", "in_stock": True}):
            a, b = old.search(query, flt, k=10, mode=mode), new.search(query, flt, k=10, mode=mode)
            assert [(h.key, h.score) for h in a] == [(h.key, h.score) for h in b], (mode, query, flt)


def test_default_collection_comes_from_the_manifest(monkeypatch):
    client = _new_collection("from_manifest")
    _index(client, _documents(), collection="from_manifest")
    monkeypatch.setattr(search_module, "active_collection", lambda: "from_manifest")
    assert Retriever(client, FakeEmbedder()).collection == "from_manifest"
    assert len(Retriever(client, FakeEmbedder()).search("áo đen", k=3, mode="dense_folded")) == 3


# --- manifest ---------------------------------------------------------------------------------------


def test_legacy_manifest_has_no_lexical_section():
    manifest = build_manifest(FAKE_CONFIG, {"product": 1, "policy": 1, "total": 2}, "1.19.1", "passed")
    assert set(manifest["vectors"]) == {DENSE, SPARSE} and "lexical" not in manifest and "derived_from" not in manifest
    assert "vectors_digest" not in manifest


def test_v2_manifest_records_the_lexical_config_fingerprint_and_lineage():
    lineage = {"collection": "shopmate_vn", "vectors_digest": "abc", "copied": 269}
    manifest = build_manifest(FAKE_CONFIG, {"product": 1, "policy": 1, "total": 2}, "1.19.1", "passed",
                              collection="shopmate_vn_v2", lexical=LEX, derived_from=lineage)
    assert manifest["collection"] == "shopmate_vn_v2"
    assert manifest["vectors"][FOLDED] == {"modifier": "idf"} and manifest["vectors"][SPARSE] == {"modifier": "none"}
    assert manifest["lexical"] == {**dataclasses.asdict(LEX), "fingerprint": LEX.fingerprint()}
    assert manifest["derived_from"] == lineage


def test_active_collection_is_read_from_the_manifest(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"collection": "shopmate_vn_v2"}', encoding="utf-8")
    assert active_collection(path) == "shopmate_vn_v2"
