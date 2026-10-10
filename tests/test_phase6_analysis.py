"""The reporting analysis of the Phase 6 run (eval/analyze_phase6.py): the detector of claims about size, colour, material and days that the tool results do
not support (the known gap of the guardrails), the fallback rate per intent and the error types of the answer-v2 rule, on hand-made examples. The detector is a
heuristic that reads text: these tests say what it finds and what it deliberately does not."""

from __future__ import annotations

import pytest

from app.llm.prompts import catalog_vocabulary
from eval.analyze_phase6 import error_types, per_intent_table, unsupported_claims

VOCAB = catalog_vocabulary()
COAT = {"sku": "SM-NAM-0001", "name": "Áo khoác dù nam", "color": "Xanh rêu", "material": "Dù", "sizes": ["M", "L"], "price_vnd": 459000, "in_stock": True}
SHOES = {"sku": "SM-GIAY-0002", "name": "Giày sneaker nam", "color": "Đen", "material": "Da lộn", "sizes": ["40", "41", "42"], "price_vnd": 899000, "in_stock": True}
PRODUCTS = {"hits": [COAT, SHOES], "relaxed": []}
POLICY = {"hits": [{"file": "doi-tra", "clause": 2, "title": "Thời hạn", "text": "Đổi kích cỡ hoặc màu sắc trong vòng 7 ngày kể từ ngày nhận hàng."}]}


def claims(kind, text, evidence):
    return [(c["type"], c["claim"]) for c in unsupported_claims(kind, text, evidence, VOCAB)]


@pytest.mark.parametrize("text", [
    "Áo khoác dù nam (SKU SM-NAM-0001) giá 459.000đ, có size M và L.",
    "SM-GIAY-0002 có cỡ 40, 41 và 42.", "Áo khoác (SKU SM-NAM-0001) màu xanh rêu, chất liệu dù.", "Giày (SKU SM-GIAY-0002) màu đen, da lộn.",
    "Cả hai mẫu đều có size M.", "Mình có hai mẫu đẹp cho bạn.",
])
def test_claims_that_the_tool_results_support_are_not_reported(text):
    assert claims("products", text, PRODUCTS) == []


@pytest.mark.parametrize("text, expected", [
    ("Áo khoác dù nam (SKU SM-NAM-0001) có size XL.", [("size", "XL")]),
    ("SM-NAM-0001 có size M, L và XXL.", [("size", "XXL")]),
    ("SM-GIAY-0002 có cỡ 38, 40.", [("size", "38")]),
    ("Áo khoác dù nam (SKU SM-NAM-0001) có màu đen.", [("color", "Đen")]),
    ("SM-GIAY-0002 màu xanh rêu rất đẹp.", [("color", "Xanh rêu")]),
    ("SM-NAM-0001 làm bằng cotton, chất liệu thoáng.", [("material", "Cotton")]),
    ("SM-GIAY-0002 làm từ da bò thật.", [("material", "Da bò")]),
    ("SM-NAM-0001 có size M và màu đen.", [("color", "Đen")]),
])
def test_a_size_colour_or_material_the_hit_does_not_have_is_reported(text, expected):
    assert claims("products", text, PRODUCTS) == expected


def test_a_sentence_is_judged_against_the_products_it_names_and_otherwise_against_all_hits():
    assert claims("products", "SM-GIAY-0002 có size M.", PRODUCTS) == [("size", "M")]  # M belongs to the coat, not to the shoes
    assert claims("products", "Mẫu này có size M.", PRODUCTS) == []  # no SKU in the sentence: any hit supports it
    assert claims("products", "SM-NAM-0001 màu đen.", PRODUCTS) == [("color", "Đen")]


def test_a_multi_word_colour_is_one_colour_not_two():
    assert claims("products", "SM-NAM-0001 màu xanh rêu.", PRODUCTS) == []
    assert claims("products", "SM-GIAY-0002 màu xanh dương.", PRODUCTS) == [("color", "Xanh dương")]


def test_a_colour_that_contains_another_colours_name_is_one_claim_not_two():
    dark_red = {**COAT, "sku": "SM-NU-0009", "color": "Đỏ đô"}
    assert claims("products", "SM-NU-0009 có màu đỏ đô rất đẹp.", {"hits": [dark_red]}) == []  # "đỏ" inside "đỏ đô" is not a second colour
    assert claims("products", "SM-NAM-0001 có màu đỏ đô.", {"hits": [COAT]}) == [("color", "Đỏ đô")]


def test_days_must_be_in_the_clause_texts_and_a_product_answer_has_no_days_to_give():
    assert claims("policies", "Bạn được đổi trong vòng 7 ngày.", POLICY) == []
    assert claims("policies", "Bạn được đổi trong vòng 30 ngày.", POLICY) == [("days", "30 ngày")]
    assert claims("policies", "Đổi trong 7 ngày hoặc 14 ngày.", POLICY) == [("days", "14 ngày")]
    assert claims("products", "SM-NAM-0001 giao trong 3 ngày.", PRODUCTS) == [("days", "3 ngày")]
    assert claims("order", "Đơn #88 đặt cách đây 5 ngày.", {"order": {"number": "88"}}) == [("days", "5 ngày")]


def test_the_reported_claim_carries_its_sentence_so_that_a_reader_can_judge_it():
    (found,) = unsupported_claims("products", "Mình thấy hai mẫu. SM-NAM-0001 có size XL.", PRODUCTS, VOCAB)
    assert found == {"type": "size", "claim": "XL", "sentence": "SM-NAM-0001 có size XL."}


# --- the table and the error types ----------------------------------------------------------------------------------------------------------


def record(item_id, intent, correct, fallback=False, kind="none", **extra):
    return {"id": item_id, "intent": intent, "kind": kind, "correct": correct, "fallback": fallback, "fallback_rules": extra.pop("rules", []),
            "retrieved_skus": [], "retrieved_clauses": [], "check": "x", **extra}


def test_the_fallback_rate_is_given_per_intent_against_the_answers_the_model_wrote_and_against_the_items():
    items = [record("a", "product_search", True, kind="skus"), record("b", "product_search", False, True, kind="skus", rules=["price_mismatch"]),
             record("c", "product_search", True, kind="no_answer"), record("d", "policy_question", True), record("e", "smalltalk", True)]
    table = per_intent_table(items, {"a": True, "b": True, "c": False, "d": True, "e": False})
    assert table["product_search"] == {"items": 3, "correct": 2, "accuracy": 2 / 3, "answers_written_by_the_model": 2, "fallbacks": 1, "fallback_rate_of_answers": 0.5,
                                       "fallback_rate_of_items": 1 / 3, "correct_by_source": {"model_text": 1, "fallback_answer": 0, "fixed_reply": 1},
                                       "items_with_fallback": ["b"]}
    assert table["policy_question"]["fallback_rate_of_answers"] == 0.0
    assert table["smalltalk"]["answers_written_by_the_model"] == 0 and table["smalltalk"]["fallback_rate_of_answers"] is None  # no model answer: no rate
    assert table["all"]["answers_written_by_the_model"] == 3 and table["all"]["fallbacks"] == 1 and table["all"]["fallback_rate_of_answers"] == pytest.approx(1 / 3)
    assert table["all"]["accuracy"] == pytest.approx(4 / 5)


def test_a_correct_item_is_credited_to_the_model_text_to_the_fallback_answer_or_to_a_fixed_reply():
    items = [record("a", "product_search", True, kind="skus"), record("b", "product_search", True, True, kind="skus", rules=["stock_mismatch"]),
             record("c", "product_search", False, True, kind="skus", rules=["stock_mismatch"]), record("d", "product_search", True, kind="no_answer"),
             record("e", "smalltalk", True)]
    table = per_intent_table(items, {"a": True, "b": True, "c": True, "d": False, "e": False})
    assert table["product_search"]["correct_by_source"] == {"model_text": 1, "fallback_answer": 1, "fixed_reply": 1}
    assert table["smalltalk"]["correct_by_source"] == {"model_text": 0, "fallback_answer": 0, "fixed_reply": 1}
    assert table["all"]["correct_by_source"] == {"model_text": 1, "fallback_answer": 1, "fixed_reply": 2}


def test_the_error_types_of_the_answer_v2_rule_are_counted_from_the_records():
    items = [record("a", "product_search", False, True, kind="skus", rules=["price_mismatch", "stock_mismatch"], gold_in_retrieved=True),
             record("b", "product_search", False, False, kind="skus", gold_in_retrieved=True), record("c", "product_search", False, False, kind="skus", gold_in_retrieved=False),
             record("d", "policy_question", False, False, gold_in_retrieved=True), record("e", "policy_question", False, False, gold_in_retrieved=False),
             record("f", "product_search", False, True, kind="skus", rules=["price_mismatch"], gold_in_retrieved=False)]
    types = error_types(items)
    assert types["E1_fallback_by_rule"] == {"price_mismatch": ["a", "f"], "stock_mismatch": ["a"]}
    assert types["E2_missing_sku_though_retrieved"] == ["a", "b"]  # c and f: the gold SKU was never retrieved, so the answer text is not to blame
    assert types["E3_missing_citation_though_retrieved"] == ["d"]
