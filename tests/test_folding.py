"""Diacritic folding (Phase 4, experiment 1): one function for documents AND queries.

fold: lowercase, Unicode NFD, strip combining marks, đ/Đ -> d. Expected values below are written
by hand from the Vietnamese examples, never computed with the function under test.
"""

from __future__ import annotations

import unicodedata

import pytest

from app.retrieval.folding import fold, has_diacritics, tokenize


@pytest.mark.parametrize(
    "text, folded",
    [
        # catalog / policy phrasing
        ("Túi đeo chéo dưới 400k", "tui deo cheo duoi 400k"),
        ("Quần jean nam màu xanh đậm", "quan jean nam mau xanh dam"),
        ("Điều kiện được đổi trả", "dieu kien duoc doi tra"),
        ("ĐỔI TRẢ", "doi tra"),
        # teencode as typed by shoppers: abbreviations stay, only marks go
        ("Đồ sale 50% thì có đổi trả được ko?", "do sale 50% thi co doi tra duoc ko?"),
        ("shop ơi, đc ko? mn ơi", "shop oi, dc ko? mn oi"),
        ("Có váy liền nào dưới 400k ko shop?", "co vay lien nao duoi 400k ko shop?"),
        # already unaccented text is only lower-cased
        ("Size 32 Freesize", "size 32 freesize"),
        ("tui deo cheo duoi 400k co ko shop", "tui deo cheo duoi 400k co ko shop"),
        # the pair that motivated the experiment (gs-024): phí / phi / phi công all fold alike
        ("phí", "phi"),
        ("phi", "phi"),
        ("Kính mát phi công", "kinh mat phi cong"),
        # non-Vietnamese marks go too
        ("café", "cafe"),
        ("", ""),
    ],
)
def test_fold_by_hand(text, folded):
    assert fold(text) == folded


@pytest.mark.parametrize(
    "accented, base",
    [
        ("àáảãạăằắẳẵặâầấẩẫậ", "a"),
        ("èéẻẽẹêềếểễệ", "e"),
        ("ìíỉĩị", "i"),
        ("òóỏõọôồốổỗộơờớởỡợ", "o"),
        ("ùúủũụưừứửữự", "u"),
        ("ỳýỷỹỵ", "y"),
        ("đ", "d"),
    ],
)
def test_every_vietnamese_letter_folds_to_its_base_letter(accented, base):
    assert fold(accented) == base * len(accented)
    assert fold(accented.upper()) == base * len(accented)


def test_composed_and_decomposed_input_fold_identically():
    text = "Quần jean nam màu xanh đậm, Đổi trả 7 ngày"
    nfc, nfd = unicodedata.normalize("NFC", text), unicodedata.normalize("NFD", text)
    assert nfc != nfd  # the test only means something if the two byte forms differ
    assert fold(nfc) == fold(nfd) == "quan jean nam mau xanh dam, doi tra 7 ngay"


def test_old_and_new_tone_mark_placement_fold_alike():
    assert fold("hòa") == fold("hoà") == "hoa"


def test_output_never_contains_a_combining_mark():
    # "İ".lower() is "i" + U+0307: a character whose lowercase form carries its own mark
    assert fold("İstanbul") == "istanbul"
    assert not any(unicodedata.category(ch) == "Mn" for ch in fold("İ Ǻ ǖ Ṍ ế"))


def test_fold_is_idempotent():
    for text in ["Túi đeo chéo", "Đồ sale 50%", "ko", "ĐƠN HÀNG #12"]:
        assert fold(fold(text)) == fold(text)


@pytest.mark.parametrize(
    "text, tokens",
    [
        ("Túi đeo-chéo, 400k!", ["tui", "deo", "cheo", "400k"]),
        ("Đơn trên 500k có free ship ko shop?", ["don", "tren", "500k", "co", "free", "ship", "ko", "shop"]),
        ("😀 ko", ["ko"]),
        ("", []),
        ("???", []),
    ],
)
def test_tokenize_by_hand(text, tokens):
    assert tokenize(text) == tokens


def test_tokenize_does_not_depend_on_diacritics():
    assert tokenize("Phí vận chuyển") == tokenize("phi van chuyen") == ["phi", "van", "chuyen"]


@pytest.mark.parametrize(
    "text, expected",
    [("tui deo cheo", False), ("Túi", True), ("đc", True), ("ĐƠN", True), ("400k", False), ("", False)],
)
def test_has_diacritics(text, expected):
    assert has_diacritics(text) is expected
