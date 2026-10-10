"""The rules that read a customer's follow-up message (app/agent/parsing.py) and the filter relaxation rule
(app/agent/relax.py). No LLM reads a follow-up: what is not understood by these rules is simply not a yes, not an
email, not an order number (docs/DECISIONS-phase6.md, A2, A4, A6)."""

from __future__ import annotations

import pytest

from app.agent.parsing import mask_contacts, mask_email, parse_emails, parse_order_number, parse_phone, parse_yes_no
from app.agent.relax import NEVER_RELAXED, RELAX_ORDER, relax

# --- email -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("email của mình là khachhang02@example.com", ["khachhang02@example.com"]),
    ("Khachhang02@Example.COM", ["khachhang02@example.com"]),  # case does not matter
    ("  mail dat hang: khachhang10@example.com. cam on shop", ["khachhang10@example.com"]),  # trailing full stop
    ("(khachhang10@example.com)", ["khachhang10@example.com"]),
    ("a.b+c@sub.example.vn", ["a.b+c@sub.example.vn"]),
    ("khachhang01@example.com và khachhang02@example.com", ["khachhang01@example.com", "khachhang02@example.com"]),
    ("khachhang01@example.com khachhang01@EXAMPLE.com", ["khachhang01@example.com"]),  # the same address twice is one
    ("không có email nha", []),
    ("khachhang@", []),
    ("@example.com", []),
    ("đơn 88", []),
])
def test_emails_are_found_lowercased_and_distinct(text, expected):
    assert parse_emails(text) == expected


def test_masking_keeps_the_domain_and_the_first_letter_only():
    assert mask_email("khachhang02@example.com") == "k***@example.com"
    assert mask_email("a@example.com") == "a***@example.com"
    assert "khachhang" not in mask_email("khachhang02@example.com")


@pytest.mark.parametrize("text, expected", [
    ("khiếu nại đơn 88, email khachhang10@example.com nhé", "khiếu nại đơn 88, email k***@example.com nhé"),
    ("mail a@x.vn hoặc b@y.com", "mail a***@x.vn hoặc b***@y.com"),
    ("gọi mình 0912345678 nha", "gọi mình [số điện thoại] nha"),
    ("sđt +84 912 345 678, email Khach@Example.com", "sđt [số điện thoại], email K***@Example.com"),
    ("không có thông tin cá nhân, đơn 88 giá 399k", "không có thông tin cá nhân, đơn 88 giá 399k"),
])
def test_contacts_in_a_quoted_text_are_masked_so_a_summary_never_shows_them(text, expected):
    assert mask_contacts(text) == expected


# --- order number --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("Shop kiểm tra giúp mình đơn 88 với", "88"),
    ("đơn #88 của mình", "88"),
    ("kiem tra don 84 giup minh", "84"),
    ("mã đơn: 2057", "2057"),
    ("Mã đơn hàng số 112", "112"),
    ("order 91 đâu rồi", "91"),
    ("đơn hàng 88, email khachhang10@example.com", "88"),
    ("#7", "7"),
    ("DON 88", "88"),
    ("đơn số 88 nha", "88"),
])
def test_an_order_number_is_read_when_the_message_says_it_is_one(text, expected):
    assert parse_order_number(text) == expected


@pytest.mark.parametrize("text", [
    "áo này 399k được không", "giá dưới 600k", "size 38 còn không", "giảm 50% nhé", "gọi 0912345678 giúp mình",
    "khachhang10@example.com", "đơn của mình đâu rồi", "mình đặt 2 cái áo", "ship trong 3 ngày", "đơn 12345678901",  # a phone-length number
    "", "88",  # a bare number is only an order number when the agent just asked for one
])
def test_anything_else_with_digits_is_not_an_order_number(text):
    assert parse_order_number(text) is None


def test_a_bare_number_counts_only_when_the_agent_is_waiting_for_an_order_number():
    assert parse_order_number("88", bare_ok=True) == "88"
    assert parse_order_number(" #88 ", bare_ok=True) == "88"
    assert parse_order_number("88 cái áo", bare_ok=True) is None  # not a bare number
    assert parse_order_number("0912345678", bare_ok=True) is None  # a phone number is never an order number
    assert parse_order_number("khachhang10@example.com", bare_ok=True) is None


def test_the_digits_of_an_email_are_not_an_order_number():
    assert parse_order_number("don khachhang10@example.com") is None
    assert parse_order_number("đơn 88 email khachhang10@example.com") == "88"


# --- phone -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, found", [
    ("số điện thoại của mình là 0912345678", True), ("0912 345 678", True), ("+84 912 345 678", True), ("09.1234.5678", True),
    ("84912345678", True), ("đơn 88", False), ("giá 399000", False), ("khachhang0912345678@example.com", False), ("", False),
])
def test_a_phone_number_is_recognised_so_that_the_agent_asks_for_the_email_instead(text, found):
    assert (parse_phone(text) is not None) == found


# --- yes / no ------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["có", "Có", "có ạ", "có, tạo đi", "đồng ý", "dong y", "ok", "OK tạo đi shop", "oke", "yes", "y", "vâng",
                                  "xác nhận", "xac nhan", "tạo đi", "tao di", "ừ tạo đi", "được, tạo nhé"])
def test_a_clear_yes_is_yes(text):
    assert parse_yes_no(text) == "yes"


@pytest.mark.parametrize("text", ["không", "Không", "ko", "k", "no", "thôi", "hủy", "huỷ", "huy di", "không tạo", "ko tạo nha", "đừng tạo", "khỏi",
                                  "thôi không cần", "không, cảm ơn"])
def test_a_clear_no_is_no(text):
    assert parse_yes_no(text) == "no"


@pytest.mark.parametrize("text", ["", "   ", "hmm", "để mình nghĩ đã", "giá bao nhiêu vậy", "mấy ngày thì có", "shop mở cửa mấy giờ",
                                  "có thể cho mình hỏi thêm không", "có hay không", "tạo", "ok nhưng đổi sang email khác", "chắc vậy", "😀",
                                  "đồng ý nhưng không chắc", "yes no"])
def test_anything_else_is_unclear_and_unclear_is_never_a_yes(text):
    assert parse_yes_no(text) in ("unclear", "no")
    assert parse_yes_no(text) != "yes"


def test_mixed_answers_resolve_to_no_never_to_yes():
    for text in ("có thể cho mình hỏi thêm không", "có hay không", "yes no", "đồng ý nhưng không chắc"):
        assert parse_yes_no(text) == "no"
    for text in ("hmm", "để mình nghĩ đã", "tạo", "ok nhưng đổi sang email khác", "chắc vậy"):
        assert parse_yes_no(text) == "unclear"


def test_a_yes_buried_in_a_long_message_is_not_a_yes_but_a_long_run_of_pure_affirmations_is():
    assert parse_yes_no("có " + "lan man " * 12) == "unclear"
    assert parse_yes_no("ok tạo đi shop nhé nha ạ giúp mình luôn nhé shop ơi") == "yes"  # every word is an affirmation or a courtesy


# --- relaxation --------------------------------------------------------------------------------------------------------------


def test_the_order_is_size_then_color_then_material_and_kind_price_and_stock_are_never_touched():
    assert RELAX_ORDER == ("size", "color", "material")
    assert NEVER_RELAXED >= {"kind", "category", "price_min", "price_max", "in_stock", "doc_type"}


@pytest.mark.parametrize("filters, expected_dropped, expected_filters", [
    ({"kind": "Áo len nữ", "color": "Đỏ", "size": "XXL"}, "size", {"kind": "Áo len nữ", "color": "Đỏ"}),
    ({"kind": "Áo khoác nữ", "color": "Kem", "material": "Tweed"}, "color", {"kind": "Áo khoác nữ", "material": "Tweed"}),
    ({"kind": "Kính mát", "material": "Nhựa acetate", "price_max": 500000}, "material", {"kind": "Kính mát", "price_max": 500000}),
    ({"kind": "Mũ", "color": "Vàng", "material": "Vải canvas", "size": "Freesize", "price_min": 100000, "price_max": 250000},
     "size", {"kind": "Mũ", "color": "Vàng", "material": "Vải canvas", "price_min": 100000, "price_max": 250000}),
])
def test_one_constraint_is_dropped_the_first_present_in_the_fixed_order(filters, expected_dropped, expected_filters):
    assert relax(filters) == (expected_filters, expected_dropped)


@pytest.mark.parametrize("filters", [
    {}, {"kind": "Áo thun nam"}, {"category": "thoi-trang-nam"}, {"kind": "Túi đeo chéo", "in_stock": True},
    {"kind": "Mũ", "price_max": 250000, "price_min": 100000}, {"doc_type": "product", "kind": "Mũ"},
])
def test_nothing_to_relax_means_no_second_search(filters):
    assert relax(filters) is None


def test_relaxation_never_changes_the_input_and_never_drops_a_protected_field():
    original = {"kind": "Áo len nữ", "color": "Đỏ", "size": "XXL", "price_max": 400000, "in_stock": True}
    snapshot = dict(original)
    new, dropped = relax(original)
    assert original == snapshot and dropped not in NEVER_RELAXED
    assert {"kind", "price_max", "in_stock"} <= set(new)
    # applied repeatedly it would walk through size, color, material only: the agent applies it ONCE, but the rule itself is safe
    seen, current = [], original
    while (step := relax(current)) is not None:
        current, name = step
        seen.append(name)
    assert seen == ["size", "color"] and current == {"kind": "Áo len nữ", "price_max": 400000, "in_stock": True}
