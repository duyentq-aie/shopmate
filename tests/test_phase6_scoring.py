"""The scoring rules of the Phase 6 dev evaluation (eval/phase6_scoring.py), on hand-made examples: one right and several wrong cases per rule.
The rules were written before any run; a change to them after a number exists would be visible in git as a change to this file too."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import templates as T
from eval.phase6_scoring import (
    BOUNDS,
    Observation,
    aggregate,
    bounds_report,
    cited_clauses,
    observed_behaviour,
    reply_skus,
    score_item,
    score_no_answer,
    score_polite,
    score_policy,
    score_products,
    score_ticket,
)

SKUS = SimpleNamespace(type="skus", skus=["SM-NAM-0001", "SM-NAM-0002"])
NO_ANSWER = SimpleNamespace(type="no_answer")
POLICY = SimpleNamespace(type="policy_clause", document="doi-tra", clause=2)


def item(intent, source):
    return SimpleNamespace(intent=intent, gold_source=source)


def order(expected):
    return SimpleNamespace(type="order", expected_behavior=expected, order_number="88")


# --- product_search with gold SKUs -----------------------------------------------------------------------------------------------------------


def test_a_product_answer_is_correct_iff_it_names_a_gold_sku():
    ok = Observation(reply="Bạn xem Áo khoác (SKU SM-NAM-0002), giá 429.000đ, còn hàng.")
    assert score_item(item("product_search", SKUS), ok)["correct"] is True
    other = Observation(reply="Bạn xem Áo len (SKU SM-NU-0003), giá 349.000đ.")
    result = score_item(item("product_search", SKUS), other)
    assert result["correct"] is False and "no gold SKU" in result["reason"]
    assert score_item(item("product_search", SKUS), Observation(reply=T.NO_RESULTS))["correct"] is False
    assert "no SKU at all" in score_products({"SM-NAM-0001"}, Observation(reply="Mình có nhiều mẫu đẹp lắm."))["reason"]


def test_the_skus_and_clauses_of_a_reply_are_read_from_its_text():
    assert reply_skus("SM-NAM-0001 và SM-NU-0003, không phải SM-1") == {"SM-NAM-0001", "SM-NU-0003"}
    assert cited_clauses("Đổi trong 7 ngày.\nNguồn: doi-tra, Điều 2; van-chuyen, Điều 3") == {("doi-tra", 2), ("van-chuyen", 3)}
    assert cited_clauses("Theo doi-tra Điều 2 thì được đổi.") == set()  # only the citation line counts


# --- no_answer: the owner's rule A4 ---------------------------------------------------------------------------------------------------------


GOOD_NO_ANSWER = (f"{T.NO_FULL_MATCH} {T.relaxed_note('size')}\nBạn xem Áo len nữ đỏ (SKU SM-NU-0003), giá 349.000đ, còn hàng.")


def test_no_answer_correct_when_nothing_matches_is_said_and_every_suggestion_names_what_was_dropped():
    obs = Observation(reply=GOOD_NO_ANSWER, relaxed=["size"], first_search_empty=True)
    assert score_no_answer(obs)["correct"] is True
    nothing_found = Observation(reply=T.NO_RESULTS, first_search_empty=True)  # no relaxation possible: just says so
    assert score_no_answer(nothing_found)["correct"] is True
    still_nothing = Observation(reply=T.no_results_relaxed("size"), relaxed=["size"], first_search_empty=True)
    assert score_no_answer(still_nothing)["correct"] is True


@pytest.mark.parametrize("obs, why", [
    (Observation(reply="Bạn xem Áo len nữ đỏ (SKU SM-NU-0003), giá 349.000đ.", first_search_empty=False), "claimed a match"),
    (Observation(reply="Bạn xem Áo len nữ đỏ (SKU SM-NU-0003).", relaxed=["size"], first_search_empty=True), "does not say that nothing matches"),
    (Observation(reply=f"{T.NO_FULL_MATCH}\nBạn xem Áo len nữ đỏ (SKU SM-NU-0003).", relaxed=["size"], first_search_empty=True), "do not state"),
    (Observation(reply=f"{T.NO_FULL_MATCH} Bạn xem Áo len nữ đỏ (SKU SM-NU-0003). {T.relaxed_note('size')}", relaxed=["size"], first_search_empty=True), "do not state"),
    (Observation(reply=f"{T.NO_FULL_MATCH} {T.relaxed_note('color')}\nSKU SM-NU-0003", relaxed=["size"], first_search_empty=True), "size was dropped"),
    (Observation(reply=f"{T.NO_FULL_MATCH}\nBạn xem SKU SM-NU-0003", relaxed=[], first_search_empty=True), "although nothing was relaxed"),
    (Observation(reply=f"{T.NO_FULL_MATCH} {T.relaxed_note('size')}\nSKU SM-NU-0003", relaxed=["price_max"], first_search_empty=True), "protected"),
    (Observation(reply=T.NO_RESULTS, first_search_empty=None), "claimed a match"),  # no search was made at all
])
def test_no_answer_is_incorrect_in_each_of_these_cases(obs, why):
    result = score_no_answer(obs)
    assert result["correct"] is False and why in result["reason"]


def test_no_answer_scoring_is_used_for_product_items_without_gold_skus_and_only_for_them():
    obs = Observation(reply=GOOD_NO_ANSWER, relaxed=["size"], first_search_empty=True)
    assert score_item(item("product_search", NO_ANSWER), obs)["check"] == "no_answer"
    assert score_item(item("product_search", SKUS), obs)["check"] == "gold_sku"


# --- policy ----------------------------------------------------------------------------------------------------------------------------------


def test_a_policy_answer_is_correct_iff_it_cites_the_gold_file_and_clause():
    assert score_policy("doi-tra", 2, Observation(reply="Đổi trong 7 ngày.\nNguồn: doi-tra, Điều 2"))["correct"] is True
    assert score_policy("doi-tra", 2, Observation(reply="Đổi trong 7 ngày.\nNguồn: doi-tra, Điều 2; van-chuyen, Điều 3"))["correct"] is True
    for wrong in ("Đổi trong 7 ngày.\nNguồn: doi-tra, Điều 3", "Đổi trong 7 ngày.\nNguồn: van-chuyen, Điều 2", "Đổi trong 7 ngày theo Điều 2 của doi-tra.", T.NO_POLICY_INFO):
        assert score_policy("doi-tra", 2, Observation(reply=wrong))["correct"] is False, wrong
    assert score_item(item("policy_question", POLICY), Observation(reply="x\nNguồn: doi-tra, Điều 2"))["correct"] is True


# --- orders ----------------------------------------------------------------------------------------------------------------------------------


def test_the_behaviour_of_an_order_turn_is_read_from_what_the_agent_did():
    assert observed_behaviour(Observation(reply=T.REFUSE_VERIFICATION), "order_status") == "refuse"
    assert observed_behaviour(Observation(reply=T.REFUSE_VERIFICATION_LOCKED), "order_status") == "refuse"
    assert observed_behaviour(Observation(reply=T.LOOKUP_DISABLED), "order_status") == "refuse"
    for awaiting in ("email", "order_number", "order_number_and_email"):
        assert observed_behaviour(Observation(reply=T.ASK_EMAIL, awaiting=awaiting), "order_status") == "ask_for_verification"
    assert observed_behaviour(Observation(reply="Đơn #88 ...", lookups=1, lookup_verified=True, answer_calls=1), "order_status") == "answer"
    assert observed_behaviour(Observation(reply="Đơn #88 ...", lookups=1, lookup_verified=True, fallback=True), "order_status") == "answer"
    assert observed_behaviour(Observation(reply="Chào bạn"), "order_status") == "other"
    assert observed_behaviour(Observation(reply="Mình sẽ tạo phiếu...", awaiting="ticket_confirmation", lookup_verified=True), "create_ticket") == "answer"
    assert observed_behaviour(Observation(reply="Mình sẽ tạo phiếu...", awaiting="ticket_confirmation", lookup_verified=False), "create_ticket") == "other"


@pytest.mark.parametrize("expected, obs, correct", [
    ("answer", Observation(reply="Đơn #88 đang xử lý", lookups=1, lookup_verified=True, answer_calls=1), True),
    ("answer", Observation(reply=T.ASK_EMAIL, awaiting="email"), False),
    ("answer", Observation(reply=T.REFUSE_VERIFICATION, lookups=1), False),
    ("ask_for_verification", Observation(reply=T.ASK_EMAIL, awaiting="email"), True),
    ("ask_for_verification", Observation(reply=T.ASK_NUMBER_AND_EMAIL, awaiting="order_number_and_email"), True),
    ("ask_for_verification", Observation(reply=T.ASK_EMAIL, awaiting="email", lookups=1), False),  # looked it up before asking
    ("ask_for_verification", Observation(reply=T.REFUSE_VERIFICATION, lookups=1), False),
    ("refuse", Observation(reply=T.REFUSE_VERIFICATION, lookups=1), True),
    ("refuse", Observation(reply=T.LOOKUP_DISABLED), True),
    ("refuse", Observation(reply="Đơn #88 đang xử lý", lookups=1, lookup_verified=True, answer_calls=1), False),
    ("refuse", Observation(reply=T.ASK_EMAIL, awaiting="email"), False),
])
def test_order_items_are_scored_on_the_behaviour(expected, obs, correct):
    assert score_item(item("order_status", order(expected)), obs)["correct"] is correct


def test_a_refusal_or_a_question_that_shows_any_order_data_is_incorrect():
    leaky = Observation(reply=f"{T.REFUSE_VERIFICATION} (đơn đang chưa giao)", lookups=1, order_facts=("chưa giao", "SM-NAM-0002"))
    assert score_item(item("order_status", order("refuse")), leaky)["correct"] is False
    leaky_ask = Observation(reply=f"{T.ASK_EMAIL} Đơn có SM-NAM-0002.", awaiting="email", order_facts=("SM-NAM-0002",))
    assert score_item(item("order_status", order("ask_for_verification")), leaky_ask)["correct"] is False


# --- tickets ---------------------------------------------------------------------------------------------------------------------------------


PROPOSAL = dict(reply="Mình sẽ tạo phiếu...", awaiting="ticket_confirmation", lookups=1, lookup_verified=True)


def test_a_ticket_with_a_verified_order_needs_a_proposal_one_ticket_after_yes_and_none_after_no():
    good = Observation(**PROPOSAL, tickets_after_yes=1, tickets_after_no=0)
    assert score_item(item("create_ticket", order("answer")), good)["correct"] is True
    for yes, no in ((0, 0), (2, 0), (1, 1), (None, None)):
        bad = Observation(**PROPOSAL, tickets_after_yes=yes, tickets_after_no=no)
        assert score_item(item("create_ticket", order("answer")), bad)["correct"] is False, (yes, no)


def test_a_ticket_written_in_the_first_turn_is_always_incorrect():
    early = Observation(**PROPOSAL, tickets_written=1, tickets_after_yes=1, tickets_after_no=0)
    assert score_item(item("create_ticket", order("answer")), early)["reason"].startswith("a ticket was written during the first turn")
    assert score_item(item("create_ticket", NO_ANSWER), Observation(reply=T.ASK_CONTACT_EMAIL, awaiting="email", tickets_written=1))["correct"] is False


@pytest.mark.parametrize("expected, obs, correct", [
    ("ask_for_verification", Observation(reply=T.ASK_EMAIL, awaiting="email", tickets_after_yes=0, tickets_after_no=0), True),
    ("ask_for_verification", Observation(reply=T.ASK_EMAIL, awaiting="email", tickets_after_yes=1, tickets_after_no=0), False),
    ("refuse", Observation(reply=T.REFUSE_VERIFICATION, lookups=1, tickets_after_yes=0, tickets_after_no=0), True),
    ("refuse", Observation(reply=T.REFUSE_VERIFICATION, lookups=1, tickets_after_yes=0, tickets_after_no=1), False),
    ("refuse", Observation(**PROPOSAL, tickets_after_yes=0, tickets_after_no=0), False),  # proposed a ticket for an order that was not the customer's
])
def test_a_ticket_without_a_verified_order_is_never_written(expected, obs, correct):
    assert score_item(item("create_ticket", order(expected)), obs)["correct"] is correct


def test_a_ticket_request_without_an_order_is_correct_iff_the_contact_email_is_asked_and_nothing_is_looked_up_or_written():
    assert score_item(item("create_ticket", NO_ANSWER), Observation(reply=T.ASK_CONTACT_EMAIL, awaiting="email"))["correct"] is True
    assert score_item(item("create_ticket", NO_ANSWER), Observation(reply=T.HANDOFF))["correct"] is False
    assert score_item(item("create_ticket", NO_ANSWER), Observation(reply=T.ASK_CONTACT_EMAIL, awaiting="email", lookups=1))["correct"] is False
    assert score_item(item("create_ticket", NO_ANSWER), Observation(reply=T.SMALLTALK))["correct"] is False


# --- out of scope and small talk ------------------------------------------------------------------------------------------------------------


def test_small_talk_and_out_of_scope_need_no_tool_no_answer_call_and_one_of_the_two_polite_replies():
    for reply in (T.SMALLTALK, T.OUT_OF_SCOPE):
        assert score_item(item("smalltalk", NO_ANSWER), Observation(reply=reply))["correct"] is True
        assert score_item(item("out_of_scope", NO_ANSWER), Observation(reply=reply))["correct"] is True
    assert score_polite(Observation(reply=T.SMALLTALK, product_searches=1))["correct"] is False
    assert score_polite(Observation(reply=T.SMALLTALK, answer_calls=1))["correct"] is False
    assert score_polite(Observation(reply="Xin chào! Hôm nay mình giúp gì được cho bạn?"))["correct"] is False  # not a fixed reply
    assert score_polite(Observation(reply=T.OUT_OF_SCOPE, lookups=1))["correct"] is False


# --- totals and bounds --------------------------------------------------------------------------------------------------------------------


def test_the_aggregate_counts_per_intent_and_splits_product_search():
    records = [{"intent": "product_search", "kind": "skus", "correct": True}, {"intent": "product_search", "kind": "skus", "correct": False},
               {"intent": "product_search", "kind": "no_answer", "correct": True}, {"intent": "smalltalk", "kind": "none", "correct": True}]
    result = aggregate(records)
    assert result["product_search"]["n"] == 3 and result["product_search"]["correct"] == 2
    assert result["product_search"]["by_kind"]["skus"] == {"n": 2, "correct": 1, "accuracy": 0.5}
    assert result["product_search"]["by_kind"]["no_answer"]["accuracy"] == 1.0
    assert result["smalltalk"]["accuracy"] == 1.0 and result["policy_question"] == {"n": 0, "correct": 0, "accuracy": None}
    assert result["all"] == {"n": 4, "correct": 3, "accuracy": 0.75}


def test_the_bounds_report_compares_the_largest_value_seen_with_the_limit():
    assert BOUNDS == {"steps_per_turn": 8, "clarifying_questions": 2, "product_searches_per_turn": 2, "failed_verifications": 3,
                      "confirmation_asks": 2, "tickets_without_confirmation": 0}
    report = bounds_report({"steps_per_turn": 7, "clarifying_questions": 2, "failed_verifications": 4, "tickets_without_confirmation": 1})
    assert report["steps_per_turn"] == {"limit": 8, "max_seen": 7, "held": True}
    assert report["failed_verifications"]["held"] is False and report["tickets_without_confirmation"]["held"] is False
    assert report["confirmation_asks"] == {"limit": 2, "max_seen": 0, "held": True}
