"""Routing and filter-extraction metrics (Phase 5), checked against hand-worked examples."""

from __future__ import annotations

import pytest

from app.retrieval.search import SearchFilters
from eval.golden.loader import load_split
from eval.llm_metrics import filter_field_metrics, normalized_expected, normalized_extracted, routing_metrics


def _r(i, gold, pred, style="co_dau"):
    return {"id": f"gs-{i:03d}", "gold": gold, "pred": pred, "query_style": style}


ROUTES = [_r(1, "product_search", "product_search", "khong_dau"), _r(2, "product_search", "policy_question", "khong_dau"),
          _r(3, "policy_question", "policy_question"), _r(4, "policy_question", "policy_question"),
          _r(5, "out_of_scope", "out_of_scope", "teencode"), _r(6, "smalltalk", None, "teencode")]


def test_routing_accuracy_and_the_confusion_matrix_by_hand():
    m = routing_metrics(ROUTES)
    assert m["n"] == 6 and m["accuracy"] == pytest.approx(4 / 6)
    assert m["confusion"]["product_search"] == {"product_search": 1, "policy_question": 1}
    assert m["confusion"]["policy_question"] == {"policy_question": 2}
    assert m["confusion"]["smalltalk"] == {"unparseable": 1}  # an unusable answer is its own column, never a guessed label
    assert m["errors"] == ["gs-002", "gs-006"]


def test_per_class_precision_recall_f1_and_macro_f1_by_hand():
    m = routing_metrics(ROUTES)
    ps, pq, oos, st = (m["per_class"][c] for c in ("product_search", "policy_question", "out_of_scope", "smalltalk"))
    assert (ps["precision"], ps["recall"], ps["f1"]) == (1.0, 0.5, pytest.approx(2 / 3))
    assert (pq["precision"], pq["recall"], pq["f1"]) == (pytest.approx(2 / 3), 1.0, pytest.approx(0.8))
    assert (oos["precision"], oos["recall"], oos["f1"]) == (1.0, 1.0, 1.0)
    assert (st["precision"], st["recall"], st["f1"], st["support"]) == (None, 0.0, 0.0, 1)  # nothing predicted: precision undefined
    # macro-F1 averages the classes that occur in the gold labels (4 of 6 here): (2/3 + 0.8 + 1 + 0) / 4
    assert m["macro_f1"] == pytest.approx((2 / 3 + 0.8 + 1 + 0) / 4)
    assert m["per_class"]["order_status"]["support"] == 0 and m["macro_f1_classes"] == 4


def test_accuracy_by_query_style():
    styles = routing_metrics(ROUTES)["by_style"]
    assert styles["khong_dau"] == {"n": 2, "accuracy": 0.5} and styles["co_dau"] == {"n": 2, "accuracy": 1.0}
    assert styles["teencode"] == {"n": 2, "accuracy": 0.5}


EXPECTED_PAIRS = [
    ({"kind": "A", "color": "X"}, {"kind": "A", "color": "X"}),
    ({"kind": "A", "size": "37"}, {"kind": "A", "size": "38"}),
    ({"price_max": 400000}, {}),
    ({"kind": "B"}, {"kind": "B", "color": "Y"}),
]


def test_exact_match_and_per_field_counts_by_hand():
    m = filter_field_metrics(EXPECTED_PAIRS)
    assert m["n"] == 4 and m["exact_match"] == 1 and m["exact_match_rate"] == 0.25 and m["failures"] == 0
    f = m["fields"]
    assert (f["kind"]["tp"], f["kind"]["fp"], f["kind"]["fn"]) == (3, 0, 0) and f["kind"]["precision"] == f["kind"]["recall"] == 1.0
    assert (f["color"]["tp"], f["color"]["fp"], f["color"]["fn"]) == (1, 1, 0) and (f["color"]["precision"], f["color"]["recall"]) == (0.5, 1.0)
    assert (f["size"]["tp"], f["size"]["fp"], f["size"]["fn"]) == (0, 1, 1)  # a wrong value is both a false positive and a miss
    assert (f["size"]["precision"], f["size"]["recall"]) == (0.0, 0.0)
    assert (f["price_max"]["tp"], f["price_max"]["fp"], f["price_max"]["fn"]) == (0, 0, 1)
    assert f["price_max"]["precision"] is None and f["price_max"]["recall"] == 0.0  # nothing predicted: precision undefined
    assert f["material"]["precision"] is None and f["material"]["recall"] is None  # field never expected nor predicted


def test_a_failed_extraction_counts_as_no_filters_and_is_counted_apart():
    m = filter_field_metrics([({"kind": "A"}, None), ({}, {})])
    assert m["failures"] == 1 and m["exact_match"] == 1 and m["fields"]["kind"]["fn"] == 1


def test_a_question_with_no_expected_filter_and_none_extracted_is_an_exact_match():
    assert filter_field_metrics([({}, {})])["exact_match"] == 1


def test_keyword_is_removed_from_the_expected_filters_like_in_the_phase_3b_oracle():
    by_id = {i.id: i for i in load_split("dev")}
    assert by_id["gs-015"].expected_filters.keyword == "tập gym"  # the golden label has a keyword...
    assert normalized_expected(by_id["gs-015"]) == {"kind": "Áo thể thao nam"}  # ...which is not a filter (R14)
    assert normalized_expected(by_id["gs-106"]) == {"kind": "Giày sneaker nữ", "color": "Trắng", "size": "37",
                                                    "price_min": 300000, "price_max": 700000}
    assert normalized_expected(by_id["gs-003"]) == {"kind": "Quần jean nam", "color": "Đen"}  # in_stock absent: not False


def test_extracted_filters_are_normalised_to_the_fields_that_are_set():
    assert normalized_extracted(SearchFilters(kind="Mũ", price_max=250000)) == {"kind": "Mũ", "price_max": 250000}
    assert normalized_extracted(None) == {} and normalized_extracted(SearchFilters()) == {}


def test_a_field_can_be_left_out_of_the_comparison_entirely():
    # the golden label says in_stock True, the extractor (by the R32 rule) says nothing: a miss WITH the field, an exact match WITHOUT it
    pairs = [({"kind": "A", "in_stock": True}, {"kind": "A"}), ({"kind": "B"}, {"kind": "B"})]
    with_field = filter_field_metrics(pairs)
    without = filter_field_metrics(pairs, exclude_fields=("in_stock",))
    assert with_field["exact_match"] == 1 and with_field["fields"]["in_stock"]["fn"] == 1
    assert without["exact_match"] == 2 and without["exact_match_rate"] == 1.0 and "in_stock" not in without["fields"]
    assert without["fields"]["kind"]["tp"] == 2 and without["excluded_fields"] == ["in_stock"]
    assert with_field["excluded_fields"] == []
