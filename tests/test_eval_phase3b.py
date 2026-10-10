"""Phase 3B evaluation tooling: test-split lock, clean-tree check, metrics, aggregation. No services."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from eval import compare_retrieval_runs as cmp
from eval import run_retrieval_eval as run
from eval.golden.draft_golden import render
from eval.golden.loader import TestSplitLocked, load_split
from eval.golden.schema import GoldenItem, GoldenSet
from eval.retrieval_metrics import (
    bootstrap_ci,
    no_answer_item_metrics,
    paired_bootstrap,
    policy_item_metrics,
    product_item_metrics,
)

REPO = Path(__file__).resolve().parent.parent


def _item(id_, intent, gold, split="dev", filters=None, style="co_dau", question="câu hỏi"):
    data = {"id": id_, "question": question, "intent": intent, "query_style": style, "gold_source": gold,
            "split": split, "status": "human_reviewed", "author": "human"}
    if filters is not None:
        data["expected_filters"] = filters
    return GoldenItem.model_validate(data)


PRODUCT = _item("gs-201", "product_search", {"type": "skus", "skus": ["SM-NAM-0001", "SM-NAM-0002"]},
                filters={"kind": "Áo thun nam", "keyword": "basic", "color": "Đen"}, style="teencode")
NO_MATCH = _item("gs-202", "product_search", {"type": "no_answer", "reason": "không có"},
                 filters={"kind": "Giày cao gót nữ", "color": "Xanh lá"})
POLICY = _item("gs-203", "policy_question", {"type": "policy_clause", "document": "doi-tra", "clause": 2},
               style="khong_dau")


# --- the test split is locked ------------------------------------------------------------------


def test_test_split_is_locked_without_the_final_flag():
    with pytest.raises(TestSplitLocked):
        load_split("test")
    dev = load_split("dev")
    assert dev and all(i.split == "dev" for i in dev)


def test_final_flag_unlocks_on_a_synthetic_set(tmp_path):
    # Proves the flag works WITHOUT touching the real test split.
    path = tmp_path / "golden.json"
    smalltalk = {"type": "no_answer", "reason": "chào"}
    items = [_item("gs-001", "smalltalk", smalltalk, "dev"), _item("gs-002", "smalltalk", smalltalk, "test")]
    path.write_text(render(GoldenSet(description="synthetic", items=items)), encoding="utf-8")
    with pytest.raises(TestSplitLocked):
        load_split("test", path=path)
    assert [i.id for i in load_split("test", final_evaluation=True, path=path)] == ["gs-002"]


@pytest.mark.parametrize("script", ["eval/run_retrieval_eval.py", "eval/compare_retrieval_runs.py"])
def test_eval_scripts_cannot_reach_the_test_split(script):
    source = (REPO / script).read_text(encoding="utf-8")
    assert "load_golden" not in source  # only the guarded loader
    assert "final_evaluation" not in source  # never sets the flag
    assert "test_split" not in source


def test_runner_cli_has_no_split_option():
    with pytest.raises(SystemExit):
        run.main(["--mode", "dense", "--filters", "none", "--split", "test"])


# --- clean working tree ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "porcelain, blocking",
    [
        ("", []),
        (" M app/x.py\0", ["app/x.py"]),  # modified tracked
        ("M  app/x.py\0", ["app/x.py"]),  # staged
        ("D  old.py\0", ["old.py"]),  # deleted
        ("R  new.py\0old.py\0", ["new.py"]),  # rename: second path consumed
        ("?? notes.txt\0", ["notes.txt"]),  # untracked outside eval/results
        ("?? eval/results/dense-none.json\0", []),  # fresh result: allowed
        (" M eval/results/dense-none.json\0", ["eval/results/dense-none.json"]),  # committed result changed
    ],
)
def test_blocking_paths(porcelain, blocking):
    assert run.blocking_paths(porcelain) == blocking


def test_extra_allowed_prefixes_let_new_files_under_them_through_but_nothing_else():
    allowed = ("eval/results/", "eval/llm_cache/")
    assert run.blocking_paths("?? eval/llm_cache/router/abc.json\0", allowed) == []
    assert run.blocking_paths("?? eval/llm_cache/router/abc.json\0") == ["eval/llm_cache/router/abc.json"]  # default unchanged
    assert run.blocking_paths("?? notes.txt\0", allowed) == ["notes.txt"]
    assert run.blocking_paths(" M eval/llm_cache/router/old.json\0", allowed) == ["eval/llm_cache/router/old.json"]  # a committed entry changed


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "code.py").write_text("x = 1\n")
    _git(tmp_path, "add", "code.py")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_fresh_result_file_does_not_block(repo):
    (repo / "eval" / "results").mkdir(parents=True)
    (repo / "eval" / "results" / "dense-none.json").write_text("{}")
    assert len(run.check_clean_tree(repo)) == 40  # returns the commit


def test_modified_tracked_file_blocks(repo):
    (repo / "code.py").write_text("x = 2\n")
    with pytest.raises(run.DirtyWorkingTree, match="code.py"):
        run.check_clean_tree(repo)


def test_untracked_file_outside_results_blocks(repo):
    (repo / "scratch.txt").write_text("tmp")
    with pytest.raises(run.DirtyWorkingTree, match="scratch.txt"):
        run.check_clean_tree(repo)


def test_existing_result_is_not_overwritten(tmp_path):
    path = tmp_path / "r.json"
    run.write_result(path, {"a": 1}, overwrite=False)
    with pytest.raises(FileExistsError):
        run.write_result(path, {"a": 2}, overwrite=False)
    run.write_result(path, {"a": 2}, overwrite=True)
    assert json.loads(path.read_text()) == {"a": 2}


# --- filters --------------------------------------------------------------------------------------


def test_filter_settings():
    assert run.build_filters(PRODUCT, "none") is None
    assert run.build_filters(PRODUCT, "routing") == {"doc_type": "product"}
    assert run.build_filters(PRODUCT, "oracle") == {"doc_type": "product", "kind": "Áo thun nam", "color": "Đen"}
    assert run.build_filters(POLICY, "routing") == run.build_filters(POLICY, "oracle") == {"doc_type": "policy"}


# --- metrics --------------------------------------------------------------------------------------


def test_product_metrics_by_hand():
    gold = {"A", "B", "C"}
    m = product_item_metrics(["X", "A", "Y", "B", "Z", "Q"], gold)
    assert m["first_gold_rank"] == 2 and m["rr"] == 0.5
    assert (m["hit@1"], m["hit@5"], m["hit@10"]) == (0.0, 1.0, 1.0)
    assert m["precision@5"] == 2 / 5 and m["precision@10"] == 2 / 6  # over returned results (6)
    assert m["nrecall@5"] == 2 / 3 and m["nrecall@1"] == 0.0
    few = product_item_metrics(["A"], gold)
    assert few["precision@10"] == 1.0 and few["nrecall@10"] == 1 / 3
    empty = product_item_metrics([], gold)
    assert empty["precision@5"] is None and empty["hit@10"] == 0.0 and empty["rr"] == 0.0


def test_policy_and_no_answer_metrics():
    m = policy_item_metrics(["p:1", "p:2", "p:3"], "p:3")
    assert (m["hit@1"], m["hit@5"], m["rr"]) == (0.0, 1.0, 1 / 3)
    assert no_answer_item_metrics([])["correct_empty"] == 1.0
    assert no_answer_item_metrics(["x"])["correct_empty"] == 0.0


def test_bootstrap_is_deterministic_and_excludes_undefined():
    values = [1.0, 0.0, 1.0, 1.0, None, 0.0]
    a, b = bootstrap_ci(values), bootstrap_ci(values)
    assert a == b and a["n"] == 5 and a["excluded"] == 1 and a["mean"] == 0.6
    assert a["ci_low"] <= a["mean"] <= a["ci_high"]


def test_paired_bootstrap():
    same = paired_bootstrap([1.0, 0.0, 1.0], [1.0, 0.0, 1.0])
    assert same["mean_diff"] == 0 and not same["distinguishable_from_noise"] and same["items_equal"] == 3
    better = paired_bootstrap([0.0] * 30, [1.0] * 30)
    assert better["distinguishable_from_noise"] and better["items_better"] == 30


# --- end to end with a fake search ---------------------------------------------------------------


def _fake_search(query, filters, k, mode):
    if filters and filters.get("color") == "Xanh lá":
        return []  # the no-match item under oracle filters
    if filters and filters.get("doc_type") == "policy":
        return ["policy:doi-tra:1", "policy:doi-tra:2"]
    return ["product:SM-NAM-0009", "product:SM-NAM-0002", "product:SM-NAM-0001"][:k]


def test_run_aggregate_and_deterministic_hash():
    items = [PRODUCT, NO_MATCH, POLICY]
    records, latencies = run.run_items(items, _fake_search, "dense", "oracle")
    first = run.build_result("abc", items, records, latencies, "dense", "oracle", 1.0)
    agg = first["deterministic"]["aggregate"]
    assert agg["counts"] == {"product_answerable": 1, "policy": 1, "no_answer": 1}
    assert agg["product_search"]["hit@1"]["mean"] == 0.0 and agg["product_search"]["rr"]["mean"] == 0.5
    assert agg["policy_question"]["rr"]["mean"] == 0.5
    assert agg["filter_correctness_check"]["correct_empty"] == 1.0
    assert agg["breakdown_by_query_style"]["product_search"]["teencode"]["too_small_to_interpret"] is True
    records2, latencies2 = run.run_items(items, _fake_search, "dense", "oracle")
    second = run.build_result("abc", items, records2, [x * 3 for x in latencies2], "dense", "oracle", 9.0)
    assert first["deterministic_sha256"] == second["deterministic_sha256"]  # latency is not hashed


def test_answerable_with_zero_results_is_counted():
    records, latencies = run.run_items([PRODUCT], lambda q, f, k, m: [], "dense", "oracle")
    agg = run.aggregate(records)
    assert agg["returned_results"]["answerable_with_zero_results"] == 1
    assert agg["product_search"]["precision@5"]["excluded"] == 1


def test_comparison_of_two_fake_runs():
    items = [PRODUCT, POLICY]
    a, _ = run.run_items(items, lambda q, f, k, m: ["product:X"], "dense", "none")
    b, _ = run.run_items(items, _fake_search, "hybrid", "none")
    diff = cmp.compare({"items": a}, {"items": b})
    assert diff["product_search"]["hit@5"]["mean_diff"] == 1.0
    assert diff["product_search"]["hit@5"]["n_pairs"] == 1


# --- the live index must be the one the manifest describes --------------------------------------


@pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")
def test_index_check_catches_a_same_count_index_with_other_fingerprints():
    import dataclasses

    from qdrant_client import QdrantClient

    from app.retrieval.indexer import index_documents
    from app.retrieval.store import ensure_collection
    from tests.fake_embedder import FAKE_CONFIG, FakeEmbedder
    from tests.test_retrieval_unit import _documents

    client = QdrantClient(":memory:")
    ensure_collection(client, FAKE_CONFIG, collection="t")
    docs = _documents()
    index_documents(client, docs, FAKE_CONFIG, embedder_factory=lambda: FakeEmbedder(), collection="t")
    manifest = {"documents": {"product": 40, "policy": 3, "total": 43},
                "embedding": {"fingerprint": FAKE_CONFIG.fingerprint()}}
    assert run.index_problems(client, manifest, FAKE_CONFIG, collection="t") == []

    # Same 43 points, re-embedded with another model revision: counts still match.
    other = dataclasses.replace(FAKE_CONFIG, revision="fake-2")
    index_documents(client, docs, other, embedder_factory=lambda: FakeEmbedder(other), collection="t")
    problems = run.index_problems(client, manifest, FAKE_CONFIG, collection="t")
    assert len(problems) == 1 and "another embedding fingerprint" in problems[0] and "43" in problems[0]

    # Manifest updated but the query embedder is still the old config.
    manifest_other = {**manifest, "embedding": {"fingerprint": other.fingerprint()}}
    problems = run.index_problems(client, manifest_other, FAKE_CONFIG, collection="t")
    assert len(problems) == 1 and "query embedder" in problems[0]


@pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")
def test_index_check_catches_a_single_stale_point():
    from qdrant_client import QdrantClient

    from app.retrieval.indexer import index_documents
    from app.retrieval.store import ensure_collection
    from tests.fake_embedder import FAKE_CONFIG, FakeEmbedder
    from tests.test_retrieval_unit import _documents

    client = QdrantClient(":memory:")
    ensure_collection(client, FAKE_CONFIG, collection="t")
    docs = _documents()
    index_documents(client, docs, FAKE_CONFIG, embedder_factory=lambda: FakeEmbedder(), collection="t")
    client.set_payload("t", payload={"embedding_fingerprint": "stale"}, points=[docs[0].point_id])
    manifest = {"documents": {"product": 40, "policy": 3, "total": 43},
                "embedding": {"fingerprint": FAKE_CONFIG.fingerprint()}}
    assert run.index_problems(client, manifest, FAKE_CONFIG, collection="t") == [
        f"points with another embedding fingerprint than the manifest ({FAKE_CONFIG.fingerprint()}): {{'stale': 1}}"]
