"""Retrieval must be deterministic (Phase 3A bug fix, DECISIONS R16).

Integration: every dev retrieval query, 20 times per filter setting and mode (the folded modes too
once the active index has the folded vector), against the REAL Qdrant, must give identical ordered result lists. The query embedding is computed once per query
(embeddings were verified deterministic), so this isolates Qdrant + the client-side tie-break.
"""

from __future__ import annotations

import functools

import pytest

from eval.golden.loader import load_split
from eval.run_retrieval_eval import FILTER_SETTINGS, RETRIEVAL_INTENTS, TOP_K, build_filters
from tests.active_index import ACTIVE, MODES

REPEATS = 20


@pytest.mark.integration
@pytest.mark.parametrize("setting", FILTER_SETTINGS)
@pytest.mark.parametrize("mode", MODES)
def test_every_dev_query_gives_identical_results_20_times(embedder, mode, setting):
    from app.retrieval.search import Retriever
    from app.retrieval.store import connect

    class Memo:
        def __init__(self):
            self.config = embedder.config
            self.encode_query = functools.cache(embedder.encode_query)

    retriever = Retriever(connect(), Memo(), collection=ACTIVE)
    items = [i for i in load_split("dev") if i.intent in RETRIEVAL_INTENTS]
    assert len(items) >= 30
    unstable = []
    for item in items:
        filters = build_filters(item, setting)
        runs = {tuple(h.key for h in retriever.search(item.question, filters, k=TOP_K, mode=mode))
                for _ in range(REPEATS)}
        if len(runs) != 1:
            unstable.append((item.id, len(runs)))
    assert unstable == [], f"non-deterministic results ({mode}, {setting}): {unstable}"
