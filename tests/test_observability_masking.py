"""What leaves for Langfuse goes through the one `mask` of app/tracing.py (emails, keys, Vietnamese phone numbers). Two layers use it: the sink
(every string it sends) and `Langfuse(mask=...)` (the SDK, on input, output and metadata of every observation). Fake secrets are built here."""

from __future__ import annotations

from app.observability.masking import clean_deep, clean_text, sdk_mask
from app.tracing import mask

FAKE_KEY = "fake-" + "x" * 28


def test_the_text_is_masked_exactly_like_a_line_of_traces_jsonl():
    text = f"gọi 0901 234 567 hoặc khachhang02@example.com token=abcdefghijkl key {FAKE_KEY}"
    assert clean_text(text, [FAKE_KEY]) == mask(text, [FAKE_KEY]) == "gọi [sđt] hoặc [email] token=[secret] key [secret]"


def test_a_hex_id_is_left_alone_even_when_its_digits_look_like_a_phone_number():
    """Internal ids are 32 hex characters; one in a hundred has ten digits in a row that the phone pattern would eat."""
    unlucky = "ab" + "0123456789" + "cd" + "e" * 18
    assert mask(unlucky) != unlucky  # the premise: the phone pattern really would corrupt this id
    assert clean_text(unlucky, []) == unlucky
    assert clean_text("0123456789", []) == "[sđt]"  # a bare number is not an id


def test_every_string_of_a_nested_value_is_masked_and_the_rest_is_untouched():
    value = {"a": ["mail khachhang02@example.com", 3, None, True, 1.5], "b": {"c": "0901234567", "n": 7}, "t": ("x@y.vn",)}
    assert clean_deep(value, []) == {"a": ["mail [email]", 3, None, True, 1.5], "b": {"c": "[sđt]", "n": 7}, "t": ["[email]"]}


def test_masking_twice_changes_nothing():
    once = clean_deep({"q": "0901234567 và khachhang02@example.com"}, [FAKE_KEY])
    assert clean_deep(once, [FAKE_KEY]) == once


def test_the_sdk_mask_is_called_with_keyword_data_and_masks_the_same_way():
    masker = sdk_mask([FAKE_KEY])
    assert masker(data={"input": f"khachhang02@example.com {FAKE_KEY}"}) == {"input": "[email] [secret]"}
    assert masker(data="0901234567") == "[sđt]"
    assert masker(data=12) == 12 and masker(data=None) is None
