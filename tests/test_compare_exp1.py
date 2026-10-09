"""Phase 4 experiment 1 comparison tool: baseline reproduction check, paired tables, the rule applied to
real result records, exploratory subsets, and the co_dau "false friend" analysis. Reads result files only.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from eval import compare_exp1 as cx
from eval.run_retrieval_eval import canonical_sha256

REPO = Path(__file__).resolve().parent.parent
COMMITTED = json.loads((REPO / "eval/results/hybrid-none.json").read_text(encoding="utf-8"))


def _rehash(run: dict) -> dict:
    run["deterministic_sha256"] = canonical_sha256(run["deterministic"])
    return run


# --- baseline reproduction -------------------------------------------------------------------------------


def test_an_identical_rerun_reproduces_the_hash_exactly():
    out = cx.reproduction_check(COMMITTED, copy.deepcopy(COMMITTED))
    assert out["ok"] and out["exact_hash"] and out["provenance_differences"] == []


def test_a_rerun_at_a_later_commit_with_a_new_manifest_reproduces_items_and_aggregate():
    later = copy.deepcopy(COMMITTED)
    later["deterministic"]["provenance"]["git_commit"] = "f" * 40
    later["deterministic"]["provenance"]["index_manifest_sha256"] = "e" * 64
    out = cx.reproduction_check(COMMITTED, _rehash(later))
    assert out["ok"] and not out["exact_hash"]
    assert out["provenance_differences"] == ["git_commit", "index_manifest_sha256"] and out["problems"] == []


def test_a_changed_item_result_is_not_a_reproduction():
    other = copy.deepcopy(COMMITTED)
    other["deterministic"]["items"][0]["retrieved"].reverse()
    out = cx.reproduction_check(COMMITTED, _rehash(other))
    assert not out["ok"] and any("items" in p for p in out["problems"])


def test_a_changed_aggregate_is_not_a_reproduction():
    other = copy.deepcopy(COMMITTED)
    other["deterministic"]["aggregate"]["policy_question"]["rr"]["mean"] += 0.01
    assert not cx.reproduction_check(COMMITTED, _rehash(other))["ok"]


@pytest.mark.parametrize("field", ["embedder_fingerprint", "dev_split_sha256", "golden_file_sha256"])
def test_any_other_provenance_difference_is_not_a_reproduction(field):
    other = copy.deepcopy(COMMITTED)
    other["deterministic"]["provenance"][field] = "x" * 16
    out = cx.reproduction_check(COMMITTED, _rehash(other))
    assert not out["ok"] and field in " ".join(out["problems"])


def test_a_changed_run_config_or_extra_provenance_fields_are_not_a_reproduction():
    other = copy.deepcopy(COMMITTED)
    other["deterministic"]["provenance"]["config"]["top_k"] = 5
    assert not cx.reproduction_check(COMMITTED, _rehash(other))["ok"]
    extra = copy.deepcopy(COMMITTED)
    extra["deterministic"]["provenance"]["lexical_fingerprint"] = "abc"
    assert not cx.reproduction_check(COMMITTED, _rehash(extra))["ok"]


def test_a_result_whose_hash_does_not_match_its_content_is_refused():
    broken = copy.deepcopy(COMMITTED)
    broken["deterministic"]["items"][0]["metrics"]["rr"] = 0.123
    with pytest.raises(ValueError, match="deterministic_sha256"):
        cx.check_integrity(broken, "hybrid-none.json")


# --- paired tables ------------------------------------------------------------------------------------------


def _rec(item_id, gold="skus", style="co_dau", rr=0.5, hit1=0.0, retrieved=None, rank=None):
    return {"id": item_id, "intent": "product_search" if gold == "skus" else "policy_question", "query_style": style,
            "gold": {"type": gold, "keys": ["product:G"] if gold == "skus" else ["policy:doi-tra:1"]},
            "retrieved": retrieved or [], "metrics": {"rr": rr, "hit@1": hit1, "hit@5": 1.0, "first_gold_rank": rank}}


def test_paired_table_groups_by_intent_and_query_style_and_flags_tiny_cells():
    base = [_rec(f"gs-{n:03d}", style="khong_dau" if n < 3 else "teencode", rr=0.5) for n in range(1, 12)]
    cand = [_rec(r["id"], style=r["query_style"], rr=1.0 if r["query_style"] == "khong_dau" else 0.5) for r in base]
    table = cx.paired_table(base, cand, ["rr"])
    overall = table["product_search"]["all"]
    assert overall["n"] == 11 and overall["rr"]["mean_diff"] == pytest.approx(2 * 0.5 / 11)  # 2 of 11 items +0.5
    assert overall["rr"]["baseline_mean"] == pytest.approx(0.5) and overall["rr"]["candidate_mean"] == pytest.approx(6.5 / 11)
    khong_dau = table["product_search"]["khong_dau"]
    assert khong_dau["n"] == 2 and khong_dau["too_small_to_interpret"] is True
    assert khong_dau["rr"]["mean_diff"] == pytest.approx(0.5)
    teen = table["product_search"]["teencode"]
    assert teen["n"] == 9 and teen["too_small_to_interpret"] is False and teen["rr"]["mean_diff"] == 0
    assert "policy_question" not in table or table["policy_question"] == {}


# --- exploratory subsets ----------------------------------------------------------------------------------------


def test_exploratory_subsets_are_khong_dau_items_and_zero_diacritic_questions_and_are_labelled():
    base = [_rec("gs-001", style="khong_dau", rr=0.5), _rec("gs-002", style="teencode", rr=0.5),
            _rec("gs-003", style="co_dau", rr=0.5), _rec("gs-004", gold="policy_clause", style="khong_dau", rr=0.5)]
    cand = [_rec(r["id"], gold=r["gold"]["type"], style=r["query_style"], rr=1.0) for r in base]
    questions = {"gs-001": "tui deo cheo", "gs-002": "co ko shop", "gs-003": "Túi đeo chéo", "gs-004": "phi ship"}
    out = cx.exploratory(base, cand, questions)
    assert out["status"].startswith("EXPLORATORY") and "does not decide" in out["status"]
    assert out["real_khong_dau"]["n_pairs"] == 2 and out["real_khong_dau"]["mean_diff"] == pytest.approx(0.5)
    assert out["zero_diacritic_questions"]["n_pairs"] == 3  # gs-001, gs-002, gs-004 have no diacritics; gs-003 does


# --- co_dau items whose rank got worse ---------------------------------------------------------------------------------


TEXTS = {
    "policy:van-chuyen:1": "Vận chuyển\nĐiều 1. Phí vận chuyển\nPhí vận chuyển nội thành là 30.000đ.",
    "product:SM-KINH-0001": "Kính mát phi công\nDanh mục: Phụ kiện\nMàu sắc: Đen",
    "product:SM-VI-0001": "Ví da nam\nMàu sắc: Nâu\nVí đựng thẻ và tiền",
}


def _gold_policy(item_id, style, rank, retrieved):
    return {"id": item_id, "intent": "policy_question", "query_style": style,
            "gold": {"type": "policy_clause", "keys": ["policy:van-chuyen:1"]}, "retrieved": retrieved,
            "metrics": {"first_gold_rank": rank, "rr": 1 / rank if rank else 0.0}}


def test_worsened_co_dau_items_are_listed_with_their_new_displacers_and_the_fold_only_matches():
    gold = "policy:van-chuyen:1"
    base = [
        _gold_policy("gs-024", "co_dau", 1, [gold, "product:SM-VI-0001"]),  # worse: 1 -> 2
        _gold_policy("gs-025", "co_dau", 3, ["product:SM-VI-0001", "product:SM-KINH-0001", gold]),  # better: 3 -> 1
        _gold_policy("gs-026", "co_dau", 2, ["product:SM-VI-0001", gold]),  # same
        _gold_policy("gs-027", "co_dau", 1, [gold]),  # lost: 1 -> not found
        _gold_policy("gs-028", "khong_dau", 1, [gold]),  # worse but not co_dau: not listed
    ]
    cand = [
        _gold_policy("gs-024", "co_dau", 2, ["product:SM-KINH-0001", gold]),
        _gold_policy("gs-025", "co_dau", 1, [gold, "product:SM-VI-0001"]),
        _gold_policy("gs-026", "co_dau", 2, ["product:SM-VI-0001", gold]),
        _gold_policy("gs-027", "co_dau", None, ["product:SM-KINH-0001"]),
        _gold_policy("gs-028", "khong_dau", 3, ["product:SM-VI-0001", "product:SM-KINH-0001", gold]),
    ]
    questions = {"gs-024": "phí vận chuyển", "gs-027": "phí ship"}
    worse = cx.worsened_co_dau(base, cand, questions, TEXTS)
    assert [w["id"] for w in worse] == ["gs-024", "gs-027"]
    first = worse[0]
    assert (first["baseline_rank"], first["candidate_rank"], first["question"]) == (1, 2, "phí vận chuyển")
    (displacer,) = first["new_displacers"]
    assert displacer["key"] == "product:SM-KINH-0001"
    assert displacer["exact_matches"] == [] and displacer["fold_only_matches"] == {"phí": ["phi"]}
    assert displacer["false_friend"] is True
    lost = worse[1]
    assert (lost["baseline_rank"], lost["candidate_rank"]) == (1, None)
    assert lost["new_displacers"][0]["fold_only_matches"] == {"phí": ["phi"]}


def test_exact_and_fold_only_matches_are_told_apart():
    # "kính" and "mát" occur in the document as typed; "phí" only as "phi"
    assert cx.fold_matches("kính mát phí", TEXTS["product:SM-KINH-0001"]) == {
        "exact_matches": ["kính", "mát"], "fold_only_matches": {"phí": ["phi"]}, "false_friend": True}
    # the document has both spellings: the query word is matched exactly, so nothing is fold-only
    assert cx.fold_matches("phí", "Phí và phi công") == {"exact_matches": ["phí"], "fold_only_matches": {},
                                                          "false_friend": False}
    # an unaccented query word meeting an accented document word is fold-only too
    assert cx.fold_matches("tui", "Túi đeo chéo") == {"exact_matches": [], "fold_only_matches": {"tui": ["túi"]},
                                                       "false_friend": True}
    assert cx.fold_matches("xe", "Túi đeo chéo")["false_friend"] is False


def test_a_displacer_that_already_outranked_gold_in_the_baseline_is_not_new():
    gold = "policy:van-chuyen:1"
    base = [_gold_policy("gs-024", "co_dau", 3, ["product:SM-VI-0001", "product:SM-KINH-0001", gold])]
    cand = [_gold_policy("gs-024", "co_dau", 4, ["product:SM-VI-0001", "product:SM-KINH-0001", "x", gold])]
    (item,) = cx.worsened_co_dau(base, cand, {"gs-024": "phí vận chuyển"}, {**TEXTS, "x": "Áo thun"})
    assert [d["key"] for d in item["new_displacers"]] == ["x"] and item["new_displacers"][0]["false_friend"] is False


def test_nothing_is_listed_when_no_co_dau_item_got_worse():
    gold = "policy:van-chuyen:1"
    run = [_gold_policy("gs-024", "co_dau", 1, [gold])]
    assert cx.worsened_co_dau(run, run, {"gs-024": "phí"}, TEXTS) == []


# --- the whole report, on real result records -------------------------------------------------------------------------


def _fake_phase4_runs():
    """Stand-ins built from the committed Phase 3B files: enough to exercise every code path on real records.
    (dense-none plays dense_folded, hybrid-routing plays dense_sparse_folded; the numbers mean nothing.)"""
    def load(name):
        return json.loads((REPO / "eval/results" / f"{name}.json").read_text(encoding="utf-8"))

    runs = {}
    for setting in ("none", "routing"):
        for queryset in ("dev", "dev_folded"):
            runs[("hybrid", setting, queryset)] = load(f"hybrid-{setting}")
            runs[("dense_folded", setting, queryset)] = load(f"dense-{setting}")
            runs[("dense_sparse_folded", setting, queryset)] = load("hybrid-routing" if setting == "none" else "hybrid-none")
    return runs


def test_report_applies_the_rule_and_returns_every_section():
    runs = _fake_phase4_runs()
    questions = {r["id"]: "câu hỏi có dấu" for r in COMMITTED["deterministic"]["items"]}
    report = cx.build_report(runs, COMMITTED, questions, texts={})
    assert set(report) >= {"rule_version", "reproduction", "dev", "perturbation", "rule", "exploratory",
                           "co_dau_worsened", "caveats"}
    assert report["reproduction"]["ok"] and report["reproduction"]["exact_hash"]
    assert set(report["rule"]["candidates"]) == {"dense_sparse_folded", "dense_folded"}
    assert report["rule"]["choice"]["decision"] in {"keep_baseline", "dense_sparse_folded", "dense_folded"}
    assert report["rule"]["choice"]["n_candidates_tested"] == 2
    assert set(report["dev"]["none"]["dense_folded"]) == {"by_intent_and_style"}
    assert set(report["co_dau_worsened"]) == {"dense_sparse_folded", "dense_folded"}
    assert any("multiple" in c for c in report["caveats"])


def test_report_refuses_runs_that_are_not_comparable():
    runs = _fake_phase4_runs()
    other = copy.deepcopy(runs[("dense_folded", "none", "dev")])
    other["deterministic"]["provenance"]["dev_split_sha256"] = "0" * 64
    runs[("dense_folded", "none", "dev")] = _rehash(other)
    with pytest.raises(ValueError, match="dev_split_sha256"):
        cx.build_report(runs, COMMITTED, {}, texts={})


def test_compare_tool_cannot_reach_the_test_split():
    source = (REPO / "eval/compare_exp1.py").read_text(encoding="utf-8")
    assert "load_golden" not in source and "final_evaluation" not in source and "test_split" not in source
