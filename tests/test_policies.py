"""Policy documents must be well-formed and agree with the shop constants Saleor is seeded with."""

from __future__ import annotations

import unicodedata

import pytest

from app import shop
from app.policies import POLICY_DOCUMENTS, clauses, policy_path


@pytest.mark.parametrize("document", POLICY_DOCUMENTS)
def test_clauses_are_numbered_consecutively(document):
    numbers = list(clauses(document))
    assert numbers == list(range(1, len(numbers) + 1))
    assert len(numbers) >= 5


@pytest.mark.parametrize("document", POLICY_DOCUMENTS)
def test_documents_are_nfc_and_marked_fictional(document):
    text = policy_path(document).read_text(encoding="utf-8")
    assert text == unicodedata.normalize("NFC", text)
    assert "hư cấu" in text


def test_shipping_policy_matches_seeded_shipping_config():
    text = policy_path("van-chuyen").read_text(encoding="utf-8")
    assert f"**{shop.format_vnd(shop.STANDARD_SHIPPING_FEE)}**" in text
    assert f"**{shop.format_vnd(shop.FREE_SHIPPING_THRESHOLD)}**" in text
    assert f"**{shop.DELIVERY_DAYS_MIN}–2 ngày**" in text
    assert f"**2–{shop.DELIVERY_DAYS_MAX} ngày**" in text


def test_shipping_fee_rule():
    assert shop.shipping_fee_for(shop.FREE_SHIPPING_THRESHOLD - 1_000) == shop.STANDARD_SHIPPING_FEE
    assert shop.shipping_fee_for(shop.FREE_SHIPPING_THRESHOLD) == 0
