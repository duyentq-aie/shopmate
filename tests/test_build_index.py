"""scripts/build_index.py: building the layout-v2 collection and its manifest (in-memory Qdrant, fake embedder).

The real run needs Saleor and the model (Step 2 of the experiment); these tests cover everything the
build decides on its own: seeding without the model, lineage and digest in the manifest, an idempotent
re-run that leaves the committed manifest untouched, and refusing the frozen Phase 3A/3B collection.
"""

from __future__ import annotations

import json

import pytest
from qdrant_client import QdrantClient

from app.retrieval.indexer import index_documents
from app.retrieval.lexical import LexicalConfig
from app.retrieval.store import COLLECTION, COLLECTION_V2, FOLDED, ensure_collection, vectors_digest
from scripts import build_index as bi
from tests.fake_embedder import FAKE_CONFIG, FakeEmbedder
from tests.test_retrieval_unit import _documents

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")

LEX = LexicalConfig()


def _must_not_load():
    raise AssertionError("the embedding model was loaded")


def _legacy_client():
    client = QdrantClient(":memory:")
    ensure_collection(client, FAKE_CONFIG, collection="old", lexical=False)
    index_documents(client, _documents(), FAKE_CONFIG, embedder_factory=lambda: FakeEmbedder(), collection="old")
    return client


def _build(client, tmp_path, **kwargs):
    options = {"seed_from": None, "recreate": False, "qdrant_version": "1.19.1", "drift_check": "passed",
               "manifest_path": tmp_path / "manifest.json"}
    options.update(kwargs)
    return bi.run_build(client, _documents(), FAKE_CONFIG, LEX, "new", lambda: FakeEmbedder(), **options)


def test_a_seeded_build_copies_the_baseline_vectors_and_records_lineage_and_digests(tmp_path):
    client = _legacy_client()
    source_digest = vectors_digest(client, "old")
    report = bi.run_build(client, _documents(), FAKE_CONFIG, LEX, "new", _must_not_load, seed_from="old",
                          recreate=False, qdrant_version="1.19.1", drift_check="passed",
                          manifest_path=tmp_path / "manifest.json")
    assert (report.stats.copied, report.stats.embedded, report.stats.lexical_computed) == (43, 0, 43)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["collection"] == "new" and manifest["documents"] == {"product": 40, "policy": 3, "total": 43}
    assert manifest["vectors_digest"] == source_digest == vectors_digest(client, "new")
    assert manifest["derived_from"] == {"collection": "old", "vectors_digest": source_digest}
    assert manifest["lexical"]["fingerprint"] == LEX.fingerprint() and manifest["vectors"][FOLDED] == {"modifier": "idf"}


def test_a_build_without_a_seed_embeds_everything_and_has_no_lineage(tmp_path):
    report = _build(QdrantClient(":memory:"), tmp_path)
    assert (report.stats.copied, report.stats.embedded, report.stats.lexical_computed) == (0, 43, 43)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "derived_from" not in manifest and manifest["vectors_digest"]


def test_rerunning_changes_nothing_and_does_not_rewrite_the_manifest(tmp_path):
    client = _legacy_client()
    first = _build(client, tmp_path, seed_from="old")
    text = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    again = bi.run_build(client, _documents(), FAKE_CONFIG, LEX, "new", _must_not_load, seed_from="old", recreate=False,
                         qdrant_version="1.19.1", drift_check="passed", manifest_path=tmp_path / "manifest.json")
    assert first.manifest_written is True and again.manifest_written is False
    assert (again.stats.copied, again.stats.embedded, again.stats.lexical_computed, again.stats.skipped) == (0, 0, 0, 43)
    assert (tmp_path / "manifest.json").read_text(encoding="utf-8") == text  # byte for byte, built_at included


def test_a_rerun_without_the_seed_option_keeps_the_recorded_lineage(tmp_path):
    client = _legacy_client()
    _build(client, tmp_path, seed_from="old")
    _build(client, tmp_path)  # no seed_from this time
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["derived_from"]["collection"] == "old"


def test_a_changed_lexical_config_updates_the_manifest_fingerprint_without_the_model(tmp_path):
    client = _legacy_client()
    _build(client, tmp_path, seed_from="old")
    import dataclasses

    other = dataclasses.replace(LEX, k1=1.5)
    report = bi.run_build(client, _documents(), FAKE_CONFIG, other, "new", _must_not_load, seed_from=None, recreate=False,
                          qdrant_version="1.19.1", drift_check="passed", manifest_path=tmp_path / "manifest.json")
    assert (report.stats.embedded, report.stats.lexical_computed) == (0, 43) and report.manifest_written is True
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["lexical"]["fingerprint"] == other.fingerprint()
    assert manifest["vectors_digest"] == vectors_digest(client, "old")  # the baseline vectors did not move


def test_the_frozen_phase3_collection_is_never_a_build_target(capsys):
    assert COLLECTION == "shopmate_vn" and COLLECTION_V2 != COLLECTION
    assert bi.main(["--collection", COLLECTION]) == 1  # refused before Saleor, Qdrant or the model are touched
    assert "frozen" in capsys.readouterr().err
    assert bi.main(["--collection", COLLECTION, "--rebuild"]) == 1
