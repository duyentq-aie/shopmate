"""Phase 4 additions to the evaluation runner: new modes, perturbation queryset, intents, results dir,
and the index checks for the folded vector. No services.

The most important property: runs of the Phase 3B modes keep exactly the same deterministic shape, so
the baseline can still be reproduced after this change (docs/RESULTS-phase3b.md, "Reproduce").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from qdrant_client import models

from app.retrieval.lexical import LexicalConfig
from app.retrieval.store import DENSE, FOLDED, vectors_digest
from eval import compare_retrieval_runs as cmp
from eval import run_retrieval_eval as run
from eval.golden.loader import load_split
from tests.fake_embedder import FAKE_CONFIG
from tests.test_eval_phase3b import NO_MATCH, POLICY, PRODUCT
from tests.test_retrieval_folded import LEX, _documents, _index, _new_collection

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")

REPO = Path(__file__).resolve().parent.parent
COMMIT = "0" * 40


def _dev_items():
    return [i for i in load_split("dev") if i.intent in run.RETRIEVAL_INTENTS]


# --- Phase 3B runs keep their shape -----------------------------------------------------------------------


def test_phase3b_modes_keep_the_deterministic_shape_of_the_committed_results():
    committed = json.loads((REPO / "eval/results/hybrid-none.json").read_text(encoding="utf-8"))
    old = committed["deterministic"]["provenance"]
    new = run.provenance(COMMIT, _dev_items(), "hybrid", "none")
    assert set(new) == set(old) and new["config"] == old["config"]
    for field in ("embedder_fingerprint", "golden_file_sha256", "dev_split_sha256", "git_dirty"):
        assert new[field] == old[field], field


def test_folded_modes_record_the_lexical_fingerprint_and_only_they_do():
    items = _dev_items()
    for mode in run.FOLDED_MODES:
        assert run.provenance(COMMIT, items, mode, "none")["lexical_fingerprint"] == LexicalConfig().fingerprint()
    for mode in run.PHASE3B_MODES:
        assert "lexical_fingerprint" not in run.provenance(COMMIT, items, mode, "none")


def test_perturbed_runs_record_the_queryset_and_the_perturbation_hash():
    prov = run.provenance(COMMIT, _dev_items(), "hybrid", "none", queryset="dev_folded", perturbation_sha256="abc")
    assert prov["config"]["queryset"] == "dev_folded" and prov["perturbation_sha256"] == "abc"
    default = run.provenance(COMMIT, _dev_items(), "hybrid", "none")
    assert "queryset" not in default["config"] and "perturbation_sha256" not in default


def test_phase3b_comparison_tool_still_compares_only_the_two_phase3b_modes():
    assert cmp.MODES == ("dense", "hybrid") and run.MODES == ("dense", "hybrid", "dense_sparse_folded", "dense_folded")


# --- naming, selection, results dir ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, name",
    [
        (("hybrid", "none"), "hybrid-none.json"),  # a Phase 3B name stays a Phase 3B name
        (("dense_folded", "routing"), "dense_folded-routing.json"),
        (("hybrid", "oracle", ("policy_question",)), "hybrid-oracle.policy_question.json"),
        (("hybrid", "none", run.RETRIEVAL_INTENTS, "dev_folded"), "hybrid-none.dev_folded.json"),
        (("dense_sparse_folded", "oracle", ("policy_question",), "dev_folded"),
         "dense_sparse_folded-oracle.policy_question.dev_folded.json"),
    ],
)
def test_result_names(args, name):
    assert run.result_name(*args) == name


def test_the_full_set_of_intents_in_any_order_is_the_default_run():
    assert run.result_name("hybrid", "none", ("policy_question", "product_search")) == "hybrid-none.json"
    assert run.canonical_intents(["policy_question", "product_search"]) == run.RETRIEVAL_INTENTS
    assert run.canonical_intents(["policy_question"]) == ("policy_question",)


def test_select_items_keeps_only_the_requested_intents():
    items = [PRODUCT, NO_MATCH, POLICY]
    assert run.select_items(items, ("policy_question",)) == [POLICY]
    assert run.select_items(items, run.RETRIEVAL_INTENTS) == items


def test_results_dir_must_stay_inside_eval_results():
    assert run.resolve_results_dir(None) == run.RESULTS_DIR
    assert run.resolve_results_dir("eval/results/phase4-exp1") == run.RESULTS_DIR / "phase4-exp1"
    for outside in ("/tmp/elsewhere", "eval/golden", "eval/results/../golden", "."):
        with pytest.raises(ValueError, match="eval/results"):
            run.resolve_results_dir(outside)


def test_cli_refuses_a_results_dir_outside_eval_results_before_anything_else(capsys):
    assert run.main(["--mode", "dense", "--filters", "none", "--results-dir", "/tmp/elsewhere"]) == 1
    assert "eval/results" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [["--queryset", "test"], ["--intents", "order_status"], ["--mode", "bm25"]])
def test_cli_rejects_unknown_choices(bad):
    argv = ["--mode", "dense", "--filters", "none"]
    with pytest.raises(SystemExit):
        run.main(argv + bad if bad[0] != "--mode" else ["--filters", "none"] + bad)


def test_a_policy_only_run_has_empty_product_aggregates_and_still_prints_a_summary():
    records, latencies = run.run_items([POLICY], lambda q, f, k, m: ["policy:doi-tra:2"], "hybrid", "oracle")
    result = run.build_result(COMMIT, [POLICY], records, latencies, "hybrid", "oracle", 1.0)
    agg = result["deterministic"]["aggregate"]
    assert agg["counts"] == {"product_answerable": 0, "policy": 1, "no_answer": 0}
    assert agg["product_search"]["rr"]["mean"] is None and agg["policy_question"]["rr"]["mean"] == 1.0
    line = run.format_summary("hybrid-oracle.policy_question.json", result)
    assert "product hit@5=n/a" in line and "policy hit@1=1.000" in line


# --- modes need the folded vector --------------------------------------------------------------------------


def test_folded_modes_are_refused_when_the_manifest_declares_no_lexical_config():
    legacy = {"collection": "shopmate_vn", "embedding": {"fingerprint": "x"}}
    v2 = {**legacy, "lexical": {"fingerprint": LEX.fingerprint()}}
    for mode in run.FOLDED_MODES:
        assert "folded" in run.mode_problems(mode, legacy)[0]
        assert run.mode_problems(mode, v2) == []
    for mode in run.PHASE3B_MODES:
        assert run.mode_problems(mode, legacy) == run.mode_problems(mode, v2) == []


# --- the live index must be the one the manifest describes (layout v2) ---------------------------------------


def _v2(docs=None):
    client, docs = _new_collection("t"), docs or _documents()
    _index(client, docs, collection="t")
    manifest = {"collection": "t", "documents": {"product": 40, "policy": 3, "total": 43},
                "embedding": {"fingerprint": FAKE_CONFIG.fingerprint()},
                "lexical": {"fingerprint": LEX.fingerprint()}, "vectors_digest": vectors_digest(client, "t")}
    return client, docs, manifest


def test_a_consistent_v2_index_passes_and_uses_the_manifests_collection():
    client, _, manifest = _v2()
    assert run.index_problems(client, manifest, FAKE_CONFIG, query_lexical=LEX) == []  # no collection argument


def test_a_single_point_with_another_lexical_fingerprint_is_reported():
    client, docs, manifest = _v2()
    client.set_payload("t", payload={"lexical_fingerprint": "stale"}, points=[docs[0].point_id])
    assert run.index_problems(client, manifest, FAKE_CONFIG, query_lexical=LEX) == [
        f"points with another lexical fingerprint than the manifest ({LEX.fingerprint()}): {{'stale': 1}}"]


def test_a_query_side_lexical_config_that_differs_from_the_manifest_is_reported():
    client, _, manifest = _v2()
    problems = run.index_problems(client, manifest, FAKE_CONFIG, query_lexical=LexicalConfig(k1=2.0))
    assert len(problems) == 1 and "query lexical config" in problems[0]


def test_a_changed_dense_or_sparse_vector_is_reported_by_the_digest():
    client, docs, manifest = _v2()
    client.update_vectors("t", points=[models.PointVectors(id=docs[3].point_id, vector={DENSE: [1.0] + [0.0] * 15})])
    problems = run.index_problems(client, manifest, FAKE_CONFIG, query_lexical=LEX)
    assert len(problems) == 1 and "dense/sparse vectors digest" in problems[0]


def test_changing_only_the_folded_vector_is_not_a_digest_problem():
    client, docs, manifest = _v2()
    client.update_vectors("t", points=[models.PointVectors(id=docs[3].point_id, vector={
        FOLDED: models.SparseVector(indices=[1], values=[1.0])})])
    assert run.index_problems(client, manifest, FAKE_CONFIG, query_lexical=LEX) == []


def test_a_manifest_that_declares_a_lexical_config_for_a_collection_without_it_is_reported():
    client = _new_collection("t", lexical=False)
    _index(client, _documents(), collection="t", lexical=None)
    manifest = {"collection": "t", "documents": {"product": 40, "policy": 3, "total": 43},
                "embedding": {"fingerprint": FAKE_CONFIG.fingerprint()}, "lexical": {"fingerprint": LEX.fingerprint()}}
    problems = run.index_problems(client, manifest, FAKE_CONFIG, query_lexical=LEX)
    assert any("no folded vector" in p for p in problems)


def test_a_v2_collection_under_a_v1_manifest_is_reported():
    client, _, manifest = _v2()
    legacy_manifest = {k: v for k, v in manifest.items() if k not in ("lexical", "vectors_digest")}
    problems = run.index_problems(client, legacy_manifest, FAKE_CONFIG)
    assert len(problems) == 1 and "declares no lexical config" in problems[0]
