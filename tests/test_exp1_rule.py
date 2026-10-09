"""The decision rule of Phase 4 experiment 1, as code (written and committed before any run).

A candidate configuration (dense_sparse_folded or dense_folded) is adopted only if, filters=none:
  1. on the perturbation set (33 answerable dev items, question machine-folded), MRR vs the baseline
     improves with a paired-bootstrap 95% CI that excludes 0 (ci_low > 0), AND
  2. on the original dev set, overall MRR is not worse (point estimate >= baseline) for BOTH
     product_search and policy_question.
If both candidates pass: the larger perturbation gain wins, unless the two gains' CIs overlap heavily
(overlap >= 0.5 of the shorter CI), then the simpler/faster configuration (dense_folded) wins.
If none passes: keep the baseline; the experiment is a documented negative result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval import exp1_rule as rule

REPO = Path(__file__).resolve().parent.parent


def _records(rr_by_id: dict[str, float], gold_types: dict[str, str] | None = None) -> list[dict]:
    """Per-item run records (the part of eval/results/*.json the rule reads)."""
    out = []
    for item_id, rr in rr_by_id.items():
        gold = (gold_types or {}).get(item_id, "skus")
        out.append({"id": item_id, "gold": {"type": gold}, "metrics": {"rr": rr}})
    return out


# 19 product items (gs-p01..) and 14 policy items (gs-q01..): the shape of the real dev sets
PRODUCT_IDS = [f"gs-p{n:02d}" for n in range(1, 20)]
POLICY_IDS = [f"gs-q{n:02d}" for n in range(1, 15)]
GOLD = {**{i: "skus" for i in PRODUCT_IDS}, **{i: "policy_clause" for i in POLICY_IDS}}


def _run(base_rr: float = 0.5, better: dict[str, float] | None = None) -> list[dict]:
    values = {i: base_rr for i in PRODUCT_IDS + POLICY_IDS}
    values.update(better or {})
    return _records(values, GOLD)


BASELINE = _run()


def _verdict(perturbed_candidate, original_candidate=None):
    return rule.evaluate_candidate(BASELINE, perturbed_candidate, BASELINE, original_candidate or BASELINE)


# --- criterion 1: the perturbation set ----------------------------------------------------------------


def test_a_clear_gain_on_the_perturbation_set_with_unchanged_original_results_is_adopted():
    gained = _run(better={i: 1.0 for i in (PRODUCT_IDS + POLICY_IDS)[:20]})  # +0.5 on 20 of 33 items
    v = _verdict(gained)
    assert v["perturbation"]["n_pairs"] == 33 and v["perturbation"]["mean_diff"] == pytest.approx(0.5 * 20 / 33)
    assert v["perturbation"]["ci_low"] > 0 and v["perturbation"]["passes"] is True
    assert v["adopt"] is True


def test_a_gain_on_only_three_of_33_items_does_not_exclude_zero():
    # (30/33)^33 = 4.3% of resamples contain none of the 3 improved items: the 2.5th percentile is 0
    gained = _run(better={i: 1.0 for i in PRODUCT_IDS[:3]})
    v = _verdict(gained)
    assert v["perturbation"]["mean_diff"] > 0 and v["perturbation"]["ci_low"] <= 0
    assert v["perturbation"]["passes"] is False and v["adopt"] is False


def test_no_change_and_a_loss_on_the_perturbation_set_fail():
    assert _verdict(BASELINE)["adopt"] is False
    worse = _run(better={i: 0.0 for i in PRODUCT_IDS + POLICY_IDS})
    v = _verdict(worse)
    assert v["perturbation"]["mean_diff"] < 0 and v["adopt"] is False


def test_the_perturbation_test_pools_product_and_policy_items():
    only_policy = _run(better={i: 1.0 for i in POLICY_IDS})  # +0.5 on the 14 policy items only
    assert _verdict(only_policy)["perturbation"]["n_pairs"] == 33
    assert _verdict(only_policy)["perturbation"]["mean_diff"] == pytest.approx(0.5 * 14 / 33)


# --- criterion 2: the original dev set ---------------------------------------------------------------


GAINED = _run(better={i: 1.0 for i in (PRODUCT_IDS + POLICY_IDS)[:20]})


def test_a_worse_original_product_mrr_blocks_adoption_even_with_a_perturbation_gain():
    worse = _run(better={PRODUCT_IDS[0]: 0.4})  # one product item 0.5 -> 0.4: mean 0.5 -> 0.4947
    v = _verdict(GAINED, original_candidate=worse)
    assert v["perturbation"]["passes"] and v["original"]["product_search"]["not_worse"] is False
    assert v["original"]["policy_question"]["not_worse"] is True and v["adopt"] is False


def test_a_worse_original_policy_mrr_blocks_adoption_even_with_a_perturbation_gain():
    worse = _run(better={POLICY_IDS[0]: 0.4})
    v = _verdict(GAINED, original_candidate=worse)
    assert v["original"]["policy_question"]["not_worse"] is False and v["adopt"] is False


def test_an_equal_original_mrr_counts_as_not_worse_and_so_does_float_noise():
    equal = _verdict(GAINED, original_candidate=BASELINE)
    assert equal["original"]["product_search"]["diff"] == 0 and equal["adopt"] is True
    noise = _run(better={PRODUCT_IDS[0]: 0.5 - 1e-15})  # below any real difference
    assert _verdict(GAINED, original_candidate=noise)["original"]["product_search"]["not_worse"] is True


def test_a_better_original_mrr_is_fine_and_reported():
    better = _run(better={PRODUCT_IDS[0]: 1.0, POLICY_IDS[0]: 1.0})
    v = _verdict(GAINED, original_candidate=better)
    assert v["original"]["product_search"]["diff"] > 0 and v["original"]["policy_question"]["diff"] > 0 and v["adopt"]


def test_original_mrr_is_computed_per_intent_over_answerable_items_only():
    with_no_answer = BASELINE + [{"id": "gs-n01", "gold": {"type": "no_answer"}, "metrics": {"returned": 10}}]
    v = rule.evaluate_candidate(with_no_answer, GAINED + with_no_answer[-1:], with_no_answer, with_no_answer)
    assert v["perturbation"]["n_pairs"] == 33
    assert v["original"]["product_search"]["baseline_mrr"] == 0.5 and v["original"]["policy_question"]["baseline_mrr"] == 0.5


def test_runs_over_different_items_are_refused():
    with pytest.raises(ValueError, match="different items"):
        rule.evaluate_candidate(BASELINE, BASELINE[:-1], BASELINE, BASELINE)


# --- choosing between the two candidates -----------------------------------------------------------------


def _v(adopt, mean, low, high):
    return {"adopt": adopt, "perturbation": {"mean_diff": mean, "ci_low": low, "ci_high": high}}


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ((0, 1), (0.5, 1.5), 0.5),  # half of the shorter interval
        ((0, 1), (2, 3), 0.0),
        ((0, 1), (1, 2), 0.0),  # touching is not overlapping
        ((0, 4), (1, 2), 1.0),  # the shorter one lies inside the longer one
        ((1, 1), (0, 2), 1.0),  # a zero-length interval inside another
        ((3, 3), (0, 2), 0.0),
    ],
)
def test_overlap_fraction_by_hand(a, b, expected):
    assert rule.overlap_fraction(a, b) == pytest.approx(expected)
    assert rule.overlap_fraction(b, a) == pytest.approx(expected)


def test_no_passing_candidate_keeps_the_baseline():
    out = rule.choose({"dense_sparse_folded": _v(False, 0.1, -0.1, 0.3), "dense_folded": _v(False, 0.0, -0.2, 0.2)})
    assert out["decision"] == "keep_baseline" and out["passing"] == [] and out["n_candidates_tested"] == 2


def test_a_single_passing_candidate_is_chosen():
    out = rule.choose({"dense_sparse_folded": _v(True, 0.2, 0.1, 0.3), "dense_folded": _v(False, 0.3, -0.1, 0.6)})
    assert out["decision"] == "dense_sparse_folded" and out["passing"] == ["dense_sparse_folded"]


def test_two_passing_candidates_with_clearly_different_gains_pick_the_larger_gain():
    a, b = _v(True, 0.50, 0.35, 0.65), _v(True, 0.20, 0.05, 0.30)  # intervals do not overlap
    assert rule.choose({"dense_sparse_folded": a, "dense_folded": b})["decision"] == "dense_sparse_folded"
    assert rule.choose({"dense_sparse_folded": b, "dense_folded": a})["decision"] == "dense_folded"


def test_two_passing_candidates_with_heavily_overlapping_gains_pick_the_simpler_one():
    big, small = _v(True, 0.30, 0.15, 0.45), _v(True, 0.25, 0.10, 0.40)  # overlap 0.25 / 0.30 = 0.83
    out = rule.choose({"dense_sparse_folded": big, "dense_folded": small})
    assert out["decision"] == "dense_folded" and out["overlap"] == pytest.approx(0.25 / 0.30)
    # ... even when the simpler one has the smaller gain, and the other way round it is still the simpler one
    assert rule.choose({"dense_sparse_folded": small, "dense_folded": big})["decision"] == "dense_folded"


def test_an_overlap_of_exactly_one_half_counts_as_heavy():
    a, b = _v(True, 0.9, 0.0, 1.0), _v(True, 0.2, 0.5, 1.5)
    out = rule.choose({"dense_sparse_folded": a, "dense_folded": b})
    assert out["overlap"] == pytest.approx(0.5) and out["decision"] == "dense_folded"


def test_both_candidates_must_be_judged():
    with pytest.raises(ValueError, match="dense_folded"):
        rule.choose({"dense_sparse_folded": _v(True, 0.2, 0.1, 0.3)})


def test_rule_module_cannot_reach_the_test_split():
    source = (REPO / "eval/exp1_rule.py").read_text(encoding="utf-8")
    assert "load_golden" not in source and "final_evaluation" not in source and "test_split" not in source
