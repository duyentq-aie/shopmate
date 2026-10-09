"""The hand-written bge-m3 embedder must reproduce FlagEmbedding's outputs (reference fixture).

Tolerances (docs/DECISIONS-phase3a.md): dense cosine >= 0.9999; sparse: identical token ids and
every weight within 1e-4 absolute. Measured on 2026-10-09: cosine 1.00000000, max weight diff 3.6e-7.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.retrieval.embedder import MODEL_REVISION, EmbeddingConfig

REFERENCE = Path(__file__).parent / "fixtures" / "bge_m3_reference.json"
DENSE_MIN_COSINE = 0.9999
SPARSE_MAX_ABS_DIFF = 1e-4


def _reference() -> dict:
    return json.loads(REFERENCE.read_text(encoding="utf-8"))


def test_reference_fixture_matches_pinned_model():
    ref = _reference()
    assert ref["revision"] == MODEL_REVISION
    assert ref["max_length"] == EmbeddingConfig().max_length
    assert ref["versions"]["FlagEmbedding"]
    styles = {i["name"] for i in ref["items"]}
    assert {"co_dau", "khong_dau", "teencode", "long_truncated"} <= styles
    assert next(i for i in ref["items"] if i["name"] == "long_truncated")["untruncated_tokens"] > 512


@pytest.mark.integration
def test_embedder_matches_flagembedding_reference(embedder):
    ref = _reference()
    outputs = embedder.encode([item["text"] for item in ref["items"]])
    for item, out in zip(ref["items"], outputs):
        a, b = item["dense"], out.dense
        cosine = sum(x * y for x, y in zip(a, b)) / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
        assert cosine >= DENSE_MIN_COSINE, (item["name"], cosine)
        expected = {int(k): v for k, v in item["sparse"].items()}
        assert set(out.sparse) == set(expected), item["name"]
        worst = max(abs(expected[k] - out.sparse[k]) for k in expected)
        assert worst <= SPARSE_MAX_ABS_DIFF, (item["name"], worst)
