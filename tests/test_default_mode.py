"""The default retrieval mode comes from ONE config value (app.config.DEFAULT_RETRIEVAL_MODE).

Phase 4 adopted `dense_folded` (docs/RESULTS-phase4-exp1.md); until now the code still defaulted to `dense`.
The evaluation runners of Phase 3B and 4 pass the mode explicitly, so their results do not depend on it.
"""

from __future__ import annotations

import pytest
from qdrant_client import QdrantClient

import app.config as config
from app.retrieval.search import FUSED_BRANCHES, Retriever
from app.retrieval.store import DENSE, FOLDED, ensure_collection
from tests.fake_embedder import FAKE_CONFIG, FakeEmbedder
from tests.test_retrieval_folded import RecordingClient, _documents, _index

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")


def _recorded_retriever():
    client = QdrantClient(":memory:")
    ensure_collection(client, FAKE_CONFIG, collection="v2", lexical=True)
    _index(client, _documents())
    recording = RecordingClient(client)
    return Retriever(recording, FakeEmbedder(), collection="v2"), recording


def test_search_without_a_mode_runs_the_configured_default():
    retriever, recording = _recorded_retriever()
    hits = retriever.search("áo đen", k=3)
    assert [c["using"] for c in recording.calls] == [DENSE, FOLDED] and len(hits) == 3  # dense_folded's two branches


def test_an_explicit_mode_still_wins_over_the_default():
    retriever, recording = _recorded_retriever()
    retriever.search("áo đen", k=3, mode="dense")
    assert [c["using"] for c in recording.calls] == [DENSE]


def test_the_default_is_read_from_the_single_config_value(monkeypatch):
    retriever, recording = _recorded_retriever()
    monkeypatch.setattr(config, "DEFAULT_RETRIEVAL_MODE", "dense")
    retriever.search("áo đen", k=3)
    assert [c["using"] for c in recording.calls] == [DENSE]


def test_an_invalid_configured_default_is_refused(monkeypatch):
    retriever, _ = _recorded_retriever()
    monkeypatch.setattr(config, "DEFAULT_RETRIEVAL_MODE", "bm25")
    with pytest.raises(ValueError, match="unknown mode"):
        retriever.search("áo đen", k=3)


def test_the_configured_default_is_dense_folded_as_decided_in_phase_4():
    # pins the Phase 4 decision (docs/RESULTS-phase4-exp1.md): changing it needs a new decision, not a drive-by edit
    assert config.DEFAULT_RETRIEVAL_MODE == "dense_folded"
    assert config.DEFAULT_RETRIEVAL_MODE in FUSED_BRANCHES or config.DEFAULT_RETRIEVAL_MODE == "dense"
