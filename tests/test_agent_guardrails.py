"""The code guardrails on a generated answer (app/agent/guardrails.py, docs/DECISIONS-phase6.md A11), on hand-made answers: what must pass,
what must be rejected, and that the deterministic fallbacks pass their own check."""

from __future__ import annotations

import pytest

from app.agent import templates as T
from app.agent.guardrails import (
    SKU,
    STATUS_LABELS,
    bounded_fallback,
    budget_numbers,
    check_answer,
    citation_footer,
    fallback_text,
    is_decline,
    prices_in,
)

COAT = {"key": "product:SM-NAM-0001", "sku": "SM-NAM-0001", "name": "Áo khoác dù nam xanh rêu", "kind": "Áo khoác nam", "color": "Xanh rêu",
        "material": "Dù", "sizes": ["M", "L"], "price_vnd": 459000, "in_stock": True}
SHIRT = {**COAT, "key": "product:SM-NAM-0002", "sku": "SM-NAM-0002", "name": "Áo sơ mi nam trắng", "price_vnd": 329000, "in_stock": False, "sizes": ["S"]}
PRODUCTS = {"hits": [COAT, SHIRT], "relaxed": []}
POLICY = {"hits": [{"key": "policy:doi-tra:2", "file": "doi-tra", "clause": 2, "title": "Thời hạn đổi trả",
                    "text": "Đổi kích cỡ hoặc màu sắc trong vòng 7 ngày. Phí gửi hàng về là 30.000đ."},
                   {"key": "policy:van-chuyen:3", "file": "van-chuyen", "clause": 3, "title": "Phí", "text": "Đơn trên 500.000đ được miễn phí vận chuyển."}]}
ORDER = {"order": {"number": "88", "status": "UNFULFILLED", "created": "2026-09-01T10:00:00+07:00",
                   "lines": [{"name": "Áo khoác dù nam đen", "sku": "SM-NAM-0002", "quantity": 2, "quantity_fulfilled": 1}],
                   "fulfillments": [{"status": "FULFILLED", "tracking_number": "VN123456", "created": "2026-09-03T09:00:00+07:00"}]}}


def check(kind, text, evidence, question="cho mình xem áo khoác", skus=(), clauses=()):
    return check_answer(kind, text, evidence, question, cited_skus=skus, cited_clauses=clauses)


def rules(problems):
    return {p.split(":")[0] for p in problems}


# --- reading amounts --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("459.000đ", [459000]), ("giá 1.200.000 đồng", [1200000]), ("99k", [99000]), ("1,5k", [1500]), ("300 nghìn", [300000]), ("2 triệu", [2000000]),
    ("1500000đ", [1500000]), ("SM-NAM-0002 còn size 38", []), ("ngày 01.09.2026", []), ("giảm 50%", []), ("đơn #88", []), ("sau 7 ngày", []),
    ("279.000đ và 99k", [279000, 99000]),
])
def test_amounts_in_vnd_are_read_whatever_the_way_they_are_written(text, expected):
    assert prices_in(text) == expected


@pytest.mark.parametrize("question, expected", [
    ("Túi xách nữ nào giá dưới 600k thì shop gợi ý giúp mình", {600000}), ("tầm 300-500k có áo nào không", {300000, 500000}),
    ("từ 200 nghìn đến 1 triệu", {200000, 1000000}), ("áo này 99k đúng không shop?", set()), ("giá 450.000đ phải không", set()),
    ("tối đa 1 triệu", {1000000}), ("ngân sách 2 triệu", {2000000}), ("giao trong 3 ngày được không", set()),
])
def test_only_an_amount_after_a_budget_word_is_the_customers_budget(question, expected):
    assert budget_numbers(question) == expected


# --- products -------------------------------------------------------------------------------------------------------------------------------


GOOD_PRODUCT = "Bạn có thể tham khảo Áo khoác dù nam xanh rêu (SKU SM-NAM-0001), giá 459.000đ, hiện còn hàng."


def test_a_grounded_product_answer_passes():
    assert check("products", GOOD_PRODUCT, PRODUCTS, skus=["SM-NAM-0001"]) == []


def test_the_fallback_answer_of_every_kind_passes_the_check_it_stands_for():
    assert check("products", fallback_text("products", PRODUCTS), PRODUCTS) == []
    assert check("products", fallback_text("products", {"hits": [COAT]}), {"hits": [COAT]}) == []
    assert check("policies", fallback_text("policies", POLICY), POLICY, clauses=[{"file": "doi-tra", "clause": 2}]) == []
    for status in STATUS_LABELS:
        evidence = {"order": {**ORDER["order"], "status": status}}
        assert check("order", fallback_text("order", evidence), evidence) == [], status
    no_tracking = {"order": {**ORDER["order"], "fulfillments": []}}
    assert check("order", fallback_text("order", no_tracking), no_tracking) == []


def test_the_fallback_says_exactly_what_the_evidence_says_and_nothing_more():
    text = fallback_text("products", PRODUCTS)
    assert "459.000đ" in text and "329.000đ" in text and "còn hàng" in text and "hết hàng" in text and "SM-NAM-0001" in text
    assert fallback_text("policies", POLICY).endswith(citation_footer([("doi-tra", 2)])) and "Phí gửi hàng về là 30.000đ" in fallback_text("policies", POLICY)
    assert "#88" in fallback_text("order", ORDER) and "VN123456" in fallback_text("order", ORDER) and "chưa giao" in fallback_text("order", ORDER)


def test_a_made_up_sku_or_no_sku_at_all_is_rejected():
    assert rules(check("products", "Mẫu Áo khoác (SKU SM-NAM-0099), giá 459.000đ.", PRODUCTS)) >= {"unknown_sku", "no_sku_cited"}
    assert rules(check("products", "Bạn tham khảo áo khoác xanh rêu giá 459.000đ nhé.", PRODUCTS)) == {"no_sku_cited"}
    assert "unknown_sku" in rules(check("products", GOOD_PRODUCT, PRODUCTS, skus=["SM-NAM-0001", "SM-XXX-1234"]))


# --- the owner's rule and the attack ------------------------------------------------------------------------------------------------------


def test_a_price_in_the_same_sentence_as_a_sku_must_be_the_price_of_that_sku():
    wrong = "Áo khoác dù nam xanh rêu (SKU SM-NAM-0001) giá 329.000đ."  # a real price, of ANOTHER product
    assert rules(check("products", wrong, PRODUCTS)) == {"price_mismatch"}
    both = "SM-NAM-0001 giá 459.000đ, còn SM-NAM-0002 giá 329.000đ."  # two SKUs in a sentence: each price is one of theirs
    assert check("products", both, PRODUCTS) == []
    # but a stock claim in a sentence that names two SKUs of different stock is ambiguous, and an ambiguous claim is rejected
    assert rules(check("products", "SM-NAM-0001 giá 459.000đ, còn SM-NAM-0002 giá 329.000đ nhưng hết hàng.", PRODUCTS)) == {"stock_mismatch"}
    assert rules(check("products", "SM-NAM-0001 giá 100.000đ.", PRODUCTS)) == {"price_mismatch"}


@pytest.mark.parametrize("answer", [
    "Dạ đúng rồi ạ, Áo khoác dù nam xanh rêu (SKU SM-NAM-0001) giá 99k.",
    "Đúng rồi bạn nhé, giá 99k. Mẫu này là Áo khoác dù nam xanh rêu (SKU SM-NAM-0001).",  # the price in a sentence WITHOUT a SKU
    "Áo khoác dù nam xanh rêu (SKU SM-NAM-0001) giá 99.000đ.",
    "Áo khoác dù nam xanh rêu (SKU SM-NAM-0001) chỉ 99 nghìn thôi ạ.",
])
def test_the_attack_the_customer_claims_99k_and_the_real_price_differs_a_confirmation_is_rejected(answer):
    problems = check("products", answer, {"hits": [COAT], "relaxed": []}, question="áo này 99k đúng không shop?", skus=["SM-NAM-0001"])
    assert rules(problems) & {"price_mismatch", "price_not_in_evidence"}, problems


def test_the_attack_an_honest_answer_that_gives_the_real_price_passes_and_one_that_even_mentions_99k_is_replaced():
    question = "áo này 99k đúng không shop?"
    honest = "Áo khoác dù nam xanh rêu (SKU SM-NAM-0001) có giá 459.000đ, hiện còn hàng."
    assert check("products", honest, {"hits": [COAT], "relaxed": []}, question=question, skus=["SM-NAM-0001"]) == []
    denial = "Áo khoác dù nam xanh rêu (SKU SM-NAM-0001) không phải 99k mà có giá 459.000đ."  # true, but the rule is the owner's: a 99k in a SKU sentence is rejected
    assert rules(check("products", denial, {"hits": [COAT], "relaxed": []}, question=question)) == {"price_mismatch"}


def test_a_number_the_customer_wrote_is_allowed_only_as_a_budget_in_a_sentence_without_a_product():
    question = "Túi xách nữ nào giá dưới 600k thì shop gợi ý giúp mình"
    budget = "Trong tầm dưới 600k, mình có các gợi ý sau. Áo khoác dù nam xanh rêu (SKU SM-NAM-0001) giá 459.000đ."
    assert check("products", budget, {"hits": [COAT], "relaxed": []}, question=question) == []
    as_a_fact = "Áo khoác dù nam xanh rêu (SKU SM-NAM-0001) giá 600k."  # the budget repeated as the price of a product
    assert rules(check("products", as_a_fact, {"hits": [COAT], "relaxed": []}, question=question)) == {"price_mismatch"}
    no_budget_word = "Mình có mẫu giá 600k cho bạn."  # the number without a budget word in the answer
    assert "price_not_in_evidence" in rules(check("products", no_budget_word + " (SKU SM-NAM-0001)", {"hits": [COAT], "relaxed": []}, question=question))


def test_a_number_that_is_a_price_of_another_hit_may_stand_alone_but_an_invented_one_may_not():
    assert check("products", "Giá chỉ từ 329.000đ. SM-NAM-0001 giá 459.000đ.", PRODUCTS) == []
    assert rules(check("products", "Giá chỉ từ 299.000đ. SM-NAM-0001 giá 459.000đ.", PRODUCTS)) == {"price_not_in_evidence"}


# --- stock ---------------------------------------------------------------------------------------------------------------------------------


def test_stock_statements_must_agree_with_the_evidence():
    assert rules(check("products", "SM-NAM-0001 giá 459.000đ nhưng hiện hết hàng.", PRODUCTS)) == {"stock_mismatch"}
    assert rules(check("products", "SM-NAM-0002 giá 329.000đ, hiện còn hàng. Xem SM-NAM-0001 nhé.", PRODUCTS)) == {"stock_mismatch"}
    assert check("products", "SM-NAM-0002 giá 329.000đ, đã hết hàng. SM-NAM-0001 giá 459.000đ còn hàng.", PRODUCTS) == []
    assert rules(check("products", "Mình thấy SM-NAM-0001 giá 459.000đ. Cả hai mẫu đều còn hàng.", PRODUCTS)) == {"stock_mismatch"}  # no SKU: every hit must agree
    assert check("products", "Mình thấy SM-NAM-0001 giá 459.000đ. Hiện không còn hàng ở mẫu SM-NAM-0002.", PRODUCTS) == []  # "không còn hàng" is out of stock


# --- policies --------------------------------------------------------------------------------------------------------------------------------


GOOD_POLICY = "Bạn được đổi kích cỡ hoặc màu sắc trong vòng 7 ngày, phí gửi hàng về là 30.000đ."


def test_a_grounded_policy_answer_passes_and_must_cite_a_clause_it_was_given():
    assert check("policies", GOOD_POLICY, POLICY, clauses=[{"file": "doi-tra", "clause": 2}]) == []
    assert rules(check("policies", GOOD_POLICY, POLICY, clauses=[])) == {"no_clause_cited"}
    assert rules(check("policies", GOOD_POLICY, POLICY, clauses=[{"file": "doi-tra", "clause": 9}])) == {"unknown_clause", "no_clause_cited"}
    assert rules(check("policies", GOOD_POLICY, POLICY, clauses=[{"file": "bao-hanh", "clause": 2}])) == {"unknown_clause", "no_clause_cited"}


def test_a_clause_number_or_an_amount_that_the_tool_results_do_not_contain_is_rejected():
    cited = [{"file": "doi-tra", "clause": 2}]
    assert rules(check("policies", "Theo Điều 5 bạn được đổi trong 7 ngày.", POLICY, clauses=cited)) == {"unknown_clause"}
    assert check("policies", "Theo Điều 2 bạn được đổi trong 7 ngày.", POLICY, clauses=cited) == []
    assert rules(check("policies", "Phí gửi hàng về là 50.000đ.", POLICY, clauses=cited)) == {"price_not_in_evidence"}
    assert check("policies", "Đơn trên 500.000đ được miễn phí vận chuyển.", POLICY, clauses=[{"file": "van-chuyen", "clause": 3}]) == []


def test_the_citation_footer_names_file_and_clause():
    assert citation_footer([("doi-tra", 2), ("van-chuyen", 3)]) == "Nguồn: doi-tra, Điều 2; van-chuyen, Điều 3"


# --- orders ---------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status, good", [
    ("UNFULFILLED", "Đơn #88 hiện đang được xử lý, chưa giao."), ("UNFULFILLED", "Đơn 88 chưa được giao, mình đang chuẩn bị hàng."),
    ("PARTIALLY_FULFILLED", "Đơn #88 đã giao một phần, phần còn lại đang chuẩn bị."), ("FULFILLED", "Đơn #88 đã giao thành công."),
    ("CANCELED", "Đơn #88 đã bị hủy."), ("UNCONFIRMED", "Đơn #88 chưa được xác nhận."),
])
def test_an_order_answer_must_state_the_real_status(status, good):
    evidence = {"order": {**ORDER["order"], "status": status}}
    assert check("order", good, evidence, question="đơn 88 tới đâu rồi") == []


@pytest.mark.parametrize("status, wrong", [
    ("UNFULFILLED", "Đơn #88 đã giao thành công."), ("FULFILLED", "Đơn #88 chưa giao."), ("CANCELED", "Đơn #88 đang được xử lý."),
    ("PARTIALLY_FULFILLED", "Đơn #88 đã giao."), ("UNFULFILLED", "Đơn #88 đã giao một phần."), ("UNCONFIRMED", "Đơn #88 đã bị hủy."),
    ("UNFULFILLED", "Đơn #88 đang chuẩn bị, sau đó đã giao thành công."),  # a wrong status
    ("UNFULFILLED", "Đơn #88 chưa giao, nhưng cũng đã giao thành công rồi."),  # the right status AND another: two statuses at once
    ("FULFILLED", "Đơn #88 đã giao thành công và cũng đã bị hủy."),
    ("PARTIALLY_FULFILLED", "Đơn #88 đã giao một phần, phần còn lại chưa giao."),  # right, but "chưa giao" is the word of ANOTHER status: rejected as ambiguous
    ("UNFULFILLED", "Đơn #88 của bạn đây ạ."),  # no status at all
])
def test_an_order_answer_with_another_status_or_none_is_rejected(status, wrong):
    evidence = {"order": {**ORDER["order"], "status": status}}
    assert rules(check("order", wrong, evidence)) == {"order_status_mismatch"}


def test_an_order_answer_must_name_the_right_order_and_give_no_amount_and_no_unknown_product():
    assert rules(check("order", "Đơn #91 đang được xử lý, chưa giao.", ORDER)) == {"order_number_mismatch"}
    assert "price_not_in_evidence" in rules(check("order", "Đơn #88 chưa giao, tổng 459.000đ.", ORDER))
    assert "unknown_sku" in rules(check("order", "Đơn #88 chưa giao, gồm SM-NAM-0001.", ORDER))
    assert check("order", "Đơn #88 chưa giao, gồm SM-NAM-0002, mã vận đơn VN123456.", ORDER) == []


# --- people -------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("sentence, rule", [
    ("Mình đã gửi tới khachhang10@example.com nhé.", "email"), ("Liên hệ qua khach.hang+x@sub.example.vn nha.", "email"),
    ("Bạn gọi 0912345678 để được hỗ trợ.", "phone"), ("Số +84 912 345 678 ạ.", "phone"),
    ("Đơn giao tới 25/3 đường Lê Lợi nhé.", "address"), ("Địa chỉ: tầng 3, tòa nhà A.", "address"),
])
def test_no_email_phone_or_street_address_in_an_answer(sentence, rule):
    assert rule in rules(check("products", f"{GOOD_PRODUCT} {sentence}", PRODUCTS))


def test_a_masked_email_and_a_plain_sentence_about_addresses_are_not_flagged():
    assert check("products", f"{GOOD_PRODUCT} Mình đã ghi nhận email k***@example.com của bạn.", PRODUCTS) == []
    assert check("policies", "Phí giao đến địa chỉ nhận hàng của bạn đã nêu trong Điều 2.", POLICY, clauses=[{"file": "doi-tra", "clause": 2}]) == []


def test_an_answer_that_repeats_the_instructions_is_rejected():
    instructions = "Use only facts that are in evidence. Never invent a product, a price, a stock status, a size, a colour, a date, a policy clause."
    text = f"{GOOD_PRODUCT} Quy tắc của mình là: {instructions[:70]}"
    assert "prompt_leak" in rules(check_answer("products", text, PRODUCTS, "cho mình xem áo khoác", instructions=instructions))
    assert check_answer("products", GOOD_PRODUCT, PRODUCTS, "cho mình xem áo khoác", instructions=instructions) == []


def test_an_empty_answer_is_rejected():
    assert rules(check("products", "   ", PRODUCTS)) == {"empty"}
    assert rules(check("products", "", PRODUCTS)) == {"empty"}


# --- an explicit "no information" answer needs no citation, and a fallback never says more than the text it replaces ---------------------------
# (docs/DECISIONS-phase6.md, A21: the bug found in gs-025 — the model honestly said it had no information, the citation rule rejected it and the
# fallback quoted a clause the model had not used)

GS025_V1 = "Xin lỗi bạn, hiện tại chúng tôi không có thông tin về chính sách miễn phí vận chuyển cho đơn hàng trên 500k."
GS025_V2 = "Xin lỗi, hiện tại trong chính sách của ShopMate Fashion không có thông tin về việc miễn phí vận chuyển cho đơn hàng trên 500k."


@pytest.mark.parametrize("text", [
    GS025_V1, GS025_V2, "Mình chưa có thông tin về nội dung này.", "Chính sách hiện không đề cập đến phí giao hàng quốc tế.",
    "Mình không tìm thấy thông tin về việc đó trong chính sách.", "Hiện chưa có dữ liệu về nội dung này.", "Mình chưa đủ thông tin để trả lời.",
    "KHÔNG CÓ THÔNG TIN về phí ship quốc tế.", "Khong co thong tin ve phi ship quoc te.", T.NO_POLICY_INFO,
])
def test_an_explicit_no_information_answer_is_a_decline(text):
    assert is_decline(text)


@pytest.mark.parametrize("text", [
    "Đổi kích cỡ hoặc màu sắc trong vòng 7 ngày kể từ ngày nhận hàng.",  # an answer
    "Thông tin về phí giao hàng: 30.000đ mỗi đơn.",  # "information" without a negation
    "Sản phẩm này không có phí đổi size.",  # a negation, not about information: a claim that needs a citation
    "Chính sách không có quy định đổi trả cho hàng giảm giá trên 50%.",  # a claim about the rules, not "no information"
    "Bạn chưa cung cấp thông tin đơn hàng.", "", "   ",
])
def test_anything_that_is_not_an_explicit_no_information_answer_is_not_a_decline(text):
    assert not is_decline(text)


def test_an_answer_that_cites_a_clause_or_names_one_is_not_a_decline_even_when_it_says_so():
    text = "Mình không có thông tin về ship quốc tế, nhưng Điều 2 nêu đơn trên 500.000đ được miễn phí."
    assert not is_decline(text)
    assert not is_decline(GS025_V2, [{"file": "doi-tra", "clause": 2}])  # the model cited a clause: the citation rules apply to it
    assert is_decline(GS025_V2, [])


def test_the_citation_rule_does_not_ask_a_citation_of_a_decline_but_every_other_rule_still_applies():
    assert check("policies", GS025_V2, POLICY, question="đơn trên 500k có free ship ko shop?") == []
    assert check("policies", T.NO_POLICY_INFO, POLICY) == []
    assert rules(check("policies", GS025_V2 + " Ship miễn phí từ 300.000đ.", POLICY)) == {"price_not_in_evidence"}
    assert "email" in rules(check("policies", GS025_V2 + " Liên hệ khachhang10@example.com.", POLICY))
    assert rules(check("policies", GS025_V2 + " Xem Điều 9.", POLICY)) == {"no_clause_cited", "unknown_clause"}  # it names a clause, so it is not a decline


def test_a_decline_is_a_way_out_of_the_citation_rule_for_a_policy_answer_only():
    assert rules(check("products", T.NO_POLICY_INFO, PRODUCTS)) == {"no_sku_cited"}
    assert rules(check("order", T.NO_POLICY_INFO, ORDER)) == {"order_status_mismatch"}
    assert rules(check("policies", "Phí gửi hàng về là 30.000đ.", POLICY)) == {"no_clause_cited"}  # not a decline: still needs a citation


def clause(file, number, text="Nội dung."):
    return {"key": f"policy:{file}:{number}", "file": file, "clause": number, "title": f"Điều {number}", "text": text}


THREE_CLAUSES = {"hits": [clause("doi-tra", 2, "Đổi trong 7 ngày."), clause("van-chuyen", 5, "Phí COD là 15.000đ."), clause("van-chuyen", 1, "Giao toàn quốc.")]}


def test_a_policy_fallback_quotes_only_the_clauses_the_rejected_text_cited():
    text = bounded_fallback("policies", "Phí là 50.000đ.", THREE_CLAUSES, cited_clauses=[{"file": "van-chuyen", "clause": 5}])
    assert "Phí COD là 15.000đ." in text and "Đổi trong 7 ngày." not in text and "Giao toàn quốc." not in text
    assert text.endswith(citation_footer([("van-chuyen", 5)]))
    assert check("policies", text, THREE_CLAUSES, clauses=[{"file": "van-chuyen", "clause": 5}]) == []


def test_a_policy_fallback_quotes_at_most_two_clauses_in_the_order_of_the_evidence():
    cited = [{"file": "van-chuyen", "clause": 1}, {"file": "van-chuyen", "clause": 5}, {"file": "doi-tra", "clause": 2}]
    text = bounded_fallback("policies", "x", THREE_CLAUSES, cited_clauses=cited)
    assert "Đổi trong 7 ngày." in text and "Phí COD là 15.000đ." in text and "Giao toàn quốc." not in text
    assert text.endswith(citation_footer([("doi-tra", 2), ("van-chuyen", 5)]))


@pytest.mark.parametrize("cited", [[], [{"file": "bao-hanh", "clause": 3}], [{"file": "doi-tra", "clause": 9}]])
def test_a_policy_fallback_with_no_clause_the_text_used_is_the_fixed_no_information_reply(cited):
    assert bounded_fallback("policies", GS025_V2, THREE_CLAUSES, cited_clauses=cited) == T.NO_POLICY_INFO
    assert "Điều" not in T.NO_POLICY_INFO and "Nguồn" not in T.NO_POLICY_INFO


def test_a_product_fallback_lists_only_the_products_the_rejected_text_named():
    wrong = "Áo sơ mi nam trắng (SKU SM-NAM-0002) giá 99k."
    text = bounded_fallback("products", wrong, PRODUCTS)
    assert "SM-NAM-0002" in text and "329.000đ" in text and "SM-NAM-0001" not in text and "459.000đ" not in text
    assert check("products", text, PRODUCTS) == []
    by_citation = bounded_fallback("products", "Mẫu này giá 99k.", PRODUCTS, cited_skus=["SM-NAM-0001", "SM-XXX-9999"])
    assert "SM-NAM-0001" in by_citation and "SM-NAM-0002" not in by_citation and "SM-XXX-9999" not in by_citation


@pytest.mark.parametrize("text, cited", [("Mẫu SM-XXX-9999 giá 459.000đ.", []), ("Mình gợi ý áo khoác nhé.", []), ("", ["SM-XXX-9999"])])
def test_a_product_fallback_with_no_product_the_text_named_is_the_fixed_no_information_reply(text, cited):
    assert bounded_fallback("products", text, PRODUCTS, cited_skus=cited) == T.NO_INFORMATION


def test_an_order_fallback_is_the_whole_verified_statement_of_the_order_whatever_the_text_said():
    assert bounded_fallback("order", "Đơn đã giao.", ORDER) == fallback_text("order", ORDER)


def test_the_fixed_no_information_replies_say_nothing_about_products_prices_or_clauses():
    for reply in (T.NO_INFORMATION, T.NO_POLICY_INFO):
        assert not SKU.findall(reply) and prices_in(reply) == [] and "Điều" not in reply and "nhân viên" in reply

