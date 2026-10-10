"""The Phase 5 evaluation core with a fake router, a fake extractor and a fake search: no API, no Qdrant, no model.

Hand-worked numbers. Items (all dev, all hand-made):
  gs-201 product, gold {A, B}, expected kind "Áo thun nam" + color "Đen" (+ keyword, which is not a filter)
  gs-202 product, gold {C}, expected kind "Mũ" + price_max 250000
  gs-203 product, no answer (nothing matches the expected filters)
  gs-204 policy clause 2 of doi-tra            gs-205 order status ("{order_number}" -> 113)
  gs-206 out_of_scope   gs-207 smalltalk   gs-208 create_ticket
"""

from __future__ import annotations

import pytest

from app.llm.extractor import ExtractionResult
from app.llm.generator import Usage
from app.llm.router import NO_CONFIDENCE_NOTE, RouteResult
from app.retrieval.search import SearchFilters
from eval.probes.loader import ProbeSet, ProbesNotReviewed, load_probes
from eval.run_phase5_eval import MODE, evaluate_phase5, prepare_probes, question_for
from eval.run_retrieval_eval import canonical_sha256
from tests.test_eval_phase3b import _item

ORDER = {"type": "order", "external_reference": "shopmate-seed-order-001", "order_number": "113",
         "expected_status": "FULFILLED", "provided_identity": "none", "expected_behavior": "ask_for_verification"}
NO_ANSWER = {"type": "no_answer", "reason": "không có"}
ITEMS = [
    _item("gs-201", "product_search", {"type": "skus", "skus": ["A", "B"]}, filters={"kind": "Áo thun nam", "color": "Đen", "keyword": "basic"}, question="q201", style="khong_dau"),
    _item("gs-202", "product_search", {"type": "skus", "skus": ["C"]}, filters={"kind": "Mũ", "price_max": 250000}, question="q202", style="teencode"),
    _item("gs-203", "product_search", NO_ANSWER, filters={"kind": "Giày cao gót nữ", "color": "Xanh lá"}, question="q203", style="co_dau"),
    _item("gs-204", "policy_question", {"type": "policy_clause", "document": "doi-tra", "clause": 2}, question="q204", style="khong_dau"),
    _item("gs-205", "order_status", ORDER, question="don {order_number} toi dau roi", style="khong_dau"),
    _item("gs-206", "out_of_scope", NO_ANSWER, question="q206", style="co_dau"),
    _item("gs-207", "smalltalk", NO_ANSWER, question="q207", style="teencode"),
    _item("gs-208", "create_ticket", NO_ANSWER, question="q208", style="co_dau"),
]
ROUTES = {"q201": "product_search", "q202": "smalltalk", "q203": "product_search", "q204": "policy_question",
          "don 113 toi dau roi": "order_status", "q206": "out_of_scope", "q207": "smalltalk", "q208": "create_ticket"}
USAGE = Usage(input_tokens=10, output_tokens=2, thought_tokens=0, total_tokens=12)


class FakeRouter:
    name = "fake-router"

    def __init__(self, mapping):
        self.mapping, self.seen = dict(mapping), []

    def route(self, question):
        self.seen.append(question)
        intent = self.mapping[question]  # a KeyError means the router received a question it should not have
        return RouteResult(intent=intent, raw_response={"q": question}, usage=USAGE, confidence=None,
                           confidence_note=NO_CONFIDENCE_NOTE, cached=True, latency_ms=100.0,
                           error=None if intent else "unusable answer")


class FakeExtractor:
    def __init__(self, mapping):
        self.mapping, self.seen = dict(mapping), []

    def extract(self, question):
        self.seen.append(question)
        filters = self.mapping[question]
        return ExtractionResult(filters=filters, raw_response={"q": question}, usage=USAGE, cached=True, latency_ms=200.0,
                                dropped={"color": "Xanh"} if question == "q201" else {}, error=None if filters is not None else "unusable")


EXTRACTIONS = {"q201": SearchFilters(kind="Áo thun nam", color="Đen"),  # exactly the expected filters
               "q202": SearchFilters(kind="Mũ"),  # misses the price bound
               "q203": SearchFilters(kind="Giày cao gót nữ", color="Xanh lá")}  # the same filters that match nothing


class FakeSearch:
    """doc_type policy -> two clauses; product with nothing else -> the 'floor' list; narrower filters -> narrower lists."""

    def __init__(self):
        self.calls = []

    def __call__(self, query, filters, k, mode):
        self.calls.append((query, filters, k, mode))
        if filters and filters.get("doc_type") == "policy":
            return ["policy:doi-tra:2", "policy:doi-tra:1"]
        extra = {key: v for key, v in (filters or {}).items() if key != "doc_type"}
        if not extra:
            return ["product:Z", "product:A", "product:B", "product:Y", "product:C"][:k]
        if extra.get("color") == "Xanh lá":
            return []  # nothing matches
        if extra.get("kind") == "Áo thun nam":
            return ["product:A", "product:B"]
        if extra.get("kind") == "Mũ" and "price_max" in extra:
            return ["product:C"]
        if extra.get("kind") == "Mũ":
            return ["product:Z", "product:C"]
        return ["product:Z"]


def run(routes=ROUTES, extractions=EXTRACTIONS, **kwargs):
    router, extractor, search = FakeRouter(routes), FakeExtractor(extractions), FakeSearch()
    result = evaluate_phase5(ITEMS, router, extractor, search, **kwargs)
    return result, router, extractor, search


def test_the_order_number_is_substituted_deterministically_before_routing():
    item = next(i for i in ITEMS if i.id == "gs-205")
    assert question_for(item) == "don 113 toi dau roi" and question_for(ITEMS[0]) == "q201"
    _, router, _, _ = run()
    assert "don 113 toi dau roi" in router.seen and not any("{order_number}" in q for q in router.seen)
    pending = _item("gs-209", "order_status", {**ORDER, "order_number": "pending"}, question="don {order_number}")
    with pytest.raises(ValueError, match="gs-209"):
        question_for(pending)


def test_routing_metrics_cover_all_items_and_the_out_of_scope_items_are_listed_apart():
    result, router, *_ = run()
    routing = result["routing"]
    assert routing["n"] == 8 and len(router.seen) == 8
    assert routing["errors"] == ["gs-202"]  # q202 was routed to smalltalk instead of product_search
    assert routing["accuracy"] == pytest.approx(7 / 8)
    assert routing["out_of_scope_items"] == [{"id": "gs-206", "question": "q206", "pred": "out_of_scope", "correct": True}]


def test_filter_extraction_is_scored_against_the_expected_filters_without_keyword():
    ext = run()[0]["extraction"]
    assert ext["metrics"]["n"] == 3 and ext["metrics"]["exact_match"] == 2  # q201 and q203 exact, q202 misses price_max
    fields = ext["metrics"]["fields"]
    assert (fields["price_max"]["tp"], fields["price_max"]["fn"]) == (0, 1) and fields["kind"]["tp"] == 3
    by_id = {i["id"]: i for i in ext["items"]}
    assert by_id["gs-201"]["expected"] == {"kind": "Áo thun nam", "color": "Đen"} and by_id["gs-201"]["dropped"] == {"color": "Xanh"}
    assert by_id["gs-202"]["exact"] is False and by_id["gs-202"]["extracted"] == {"kind": "Mũ"}


def test_the_three_retrieval_arms_use_routing_oracle_and_extracted_filters_with_the_stated_mode():
    result, _, _, search = run()
    arms = result["retrieval_gold_routing"]["arms"]
    rr = {name: arm["aggregate"]["product_search"]["rr"]["mean"] for name, arm in arms.items()}
    # floor: A at rank 2 (0.5) and C at rank 5 (0.2); ceiling: both rank 1; extracted: 1 and "Mũ" without the price -> rank 2
    assert rr == {"floor": pytest.approx((0.5 + 0.2) / 2), "ceiling": 1.0, "extracted": pytest.approx((1 + 0.5) / 2)}
    used = [(q, f) for q, f, _, _ in search.calls if q == "q201"]
    assert {"doc_type": "product"} in [f for _, f in used]  # floor
    assert {"doc_type": "product", "kind": "Áo thun nam", "color": "Đen"} in [f for _, f in used]  # oracle without keyword, and extracted
    assert {m for _, _, _, m in search.calls} == {MODE} == {"dense_folded"}


def test_the_extracted_arm_is_compared_with_floor_and_ceiling_by_paired_bootstrap():
    paired = run()[0]["retrieval_gold_routing"]["paired"]
    assert paired["extracted_vs_floor"]["rr"]["mean_diff"] == pytest.approx(0.75 - 0.35)
    assert paired["extracted_vs_ceiling"]["rr"]["mean_diff"] == pytest.approx(0.75 - 1.0)
    assert paired["extracted_vs_floor"]["rr"]["n_pairs"] == 2 and "ci_low" in paired["extracted_vs_ceiling"]["hit@1"]


def test_no_answer_items_with_extracted_filters_are_a_real_measurement_unlike_oracle():
    na = run()[0]["retrieval_gold_routing"]["no_answer"]
    assert na["n"] == 1 and na["floor"]["correct_empty"] == 0.0 and na["extracted"]["correct_empty"] == 1.0
    assert "tautolog" in na["ceiling"]["note"] and na["ceiling"]["correct_empty"] == 1.0
    assert "real measurement" in na["extracted"]["note"]


def test_a_failed_extraction_falls_back_to_the_floor_filters_and_is_counted():
    result, *_ = run(extractions={**EXTRACTIONS, "q201": None})
    assert result["retrieval_gold_routing"]["extraction_failures"] == 1
    assert result["extraction"]["metrics"]["failures"] == 1


def test_the_full_pipeline_counts_a_misrouted_question_as_a_miss_and_never_retrieves_for_it():
    result, router, extractor, search = run()
    pipe = result["pipeline"]
    by_id = {r["id"]: r for r in pipe["records"]}
    assert by_id["gs-201"]["metrics"]["rr"] == 1.0 and by_id["gs-201"]["routed_intent"] == "product_search"
    assert by_id["gs-202"]["metrics"]["rr"] == 0.0 and by_id["gs-202"]["retrieved"] == [] and by_id["gs-202"]["routed_intent"] == "smalltalk"
    assert by_id["gs-204"]["metrics"]["rr"] == 1.0 and by_id["gs-204"]["filters"] == {"doc_type": "policy"}
    assert pipe["misrouted_answerable"] == 1 and pipe["n_answerable"] == 3
    assert pipe["aggregate"]["product_search"]["rr"]["mean"] == pytest.approx(0.5)  # (1 + 0) / 2
    assert pipe["aggregate"]["policy_question"]["rr"]["mean"] == 1.0


def test_no_answer_counts_as_correct_only_if_routed_to_product_and_the_result_is_empty():
    pipe = run()[0]["pipeline"]
    assert pipe["no_answer"] == {"n": 1, "correct": 1, "rate": 1.0, "rule": "routed to product_search AND an empty result"}
    # routed to product but the extracted filters let results through: wrong
    result, *_ = run(extractions={**EXTRACTIONS, "q203": SearchFilters()})
    assert result["pipeline"]["no_answer"]["correct"] == 0
    # routed away from product: wrong, even though nothing was retrieved
    result, *_ = run(routes={**ROUTES, "q203": "out_of_scope"})
    assert result["pipeline"]["no_answer"]["correct"] == 0
    assert {r["id"]: r["retrieved"] for r in result["pipeline"]["records"]}["gs-203"] == []


def test_a_policy_question_routed_to_product_search_really_runs_a_product_retrieval_and_misses():
    result, _, extractor, search = run(routes={**ROUTES, "q204": "product_search"}, extractions={**EXTRACTIONS, "q204": SearchFilters()})
    rec = {r["id"]: r for r in result["pipeline"]["records"]}["gs-204"]
    assert rec["filters"] == {"doc_type": "product"} and rec["metrics"]["rr"] == 0.0 and "q204" in extractor.seen
    assert any(q == "q204" and f == {"doc_type": "product"} for q, f, _, _ in search.calls)


def test_routes_that_are_not_retrieval_intents_trigger_no_search_in_the_pipeline():
    _, _, _, search = run()
    assert not [c for c in search.calls if c[0] in ("q206", "q207", "q208", "don 113 toi dau roi")]


def test_the_pipeline_is_compared_with_gold_routing_per_intent():
    paired = run()[0]["pipeline"]["paired_vs_gold_routing"]
    assert paired["product_search"]["rr"]["mean_diff"] == pytest.approx(0.5 - 0.75)  # the cost of one misrouted question
    assert paired["policy_question"]["rr"]["mean_diff"] == 0.0


def test_calls_summary_counts_tokens_and_latency_per_component():
    calls = run()[0]["calls"]
    assert calls["router"]["n"] == 8 and calls["router"]["input_tokens"] == 80 and calls["router"]["output_tokens"] == 16
    assert calls["router"]["latency_ms"] == {"p50": 100.0, "p95": 100.0} and calls["router"]["cache_hits"] == 8
    assert calls["filters"]["n"] == 3 and calls["filters"]["latency_ms"]["p50"] == 200.0  # unique questions only


def test_the_result_is_deterministic_and_carries_no_timestamps():
    first, second = run()[0], run()[0]
    assert first == second and canonical_sha256(first) == canonical_sha256(second)


def test_unusable_router_answers_are_misses_not_crashes():
    result, *_ = run(routes={**ROUTES, "q201": None})
    assert result["routing"]["confusion"]["product_search"]["unparseable"] == 1 and "gs-201" in result["routing"]["errors"]
    assert {r["id"]: r for r in result["pipeline"]["records"]}["gs-201"]["metrics"]["rr"] == 0.0


# --- probes ----------------------------------------------------------------------------------------------------------


def test_probes_are_refused_until_reviewed_and_never_enter_the_golden_metrics(tmp_path):
    draft = load_probes().model_copy(update={"status": "draft_needs_human_review"})
    path = tmp_path / "probes.json"
    path.write_text(draft.model_dump_json(), encoding="utf-8")
    with pytest.raises(ProbesNotReviewed):
        prepare_probes(path)
    reviewed = draft.model_copy(update={"status": "human_reviewed"})
    path.write_text(reviewed.model_dump_json(), encoding="utf-8")
    probes = prepare_probes(path)
    mapping = {**ROUTES, **{p.question: p.expected_intent for p in probes.items[:7]}, probes.items[7].question: "product_search"}
    result, *_ = run(routes=mapping, probes=probes)
    section = result["probes"]
    assert section["n"] == 8 and section["correct"] == 7 and "NOT a security boundary" in section["security_note"]
    assert [p["id"] for p in section["items"] if not p["correct"]] == ["pr-008"]
    assert result["routing"]["n"] == 8  # the probes are not golden items
    assert "probes" not in run()[0]


# --- cost and command line -----------------------------------------------------------------------------------------------


def test_theoretical_paid_tier_cost_per_1000_questions_by_hand():
    from eval.run_phase5_eval import cost_per_1000_questions

    calls = {"router": {"n": 1, "input_tokens": 10, "output_tokens": 2, "thought_tokens": 0},
             "filters": {"n": 1, "input_tokens": 20, "output_tokens": 5, "thought_tokens": 0}}
    cost = cost_per_1000_questions(calls, extraction_share=0.5, price_input=0.1, price_output=0.4)
    # router: 1000 * (10*0.1 + 2*0.4) / 1e6 = 0.0018; extractor on half the questions: 1000 * 0.5 * (20*0.1 + 5*0.4) / 1e6 = 0.002
    assert cost["router_usd"] == pytest.approx(0.0018) and cost["extractor_usd"] == pytest.approx(0.002)
    assert cost["total_usd"] == pytest.approx(0.0038)
    with_thoughts = {**calls, "router": {**calls["router"], "thought_tokens": 3}}  # thought tokens are billed as output
    assert cost_per_1000_questions(with_thoughts, 0.5, 0.1, 0.4)["router_usd"] == pytest.approx(1000 * (10 * 0.1 + 5 * 0.4) / 1e6)


def test_the_cli_refuses_a_results_dir_outside_eval_results_before_anything_else(capsys):
    from eval.run_phase5_eval import main

    assert main(["--results-dir", "/tmp/elsewhere"]) == 1 and "eval/results" in capsys.readouterr().err
