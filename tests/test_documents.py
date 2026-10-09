"""Document building for the index: products (from runtime query nodes) and policy clauses."""

from __future__ import annotations

import json

import pytest

from app.policies import POLICY_DOCUMENTS, clauses
from app.retrieval.documents import Document, editorjs_to_text, policy_documents, product_document


def _node(**overrides) -> dict:
    node = {
        "name": "Áo thun nam cổ tròn basic đen",
        "description": json.dumps({"blocks": [{"type": "paragraph", "data": {"text": "Mô tả ngắn."}}]}),
        "category": {"slug": "thoi-trang-nam", "name": "Thời trang nam"},
        "attributes": [
            {"attribute": {"slug": "mau-sac"}, "values": [{"name": "Đen"}]},
            {"attribute": {"slug": "kich-co"}, "values": [{"name": "XL"}, {"name": "M"}, {"name": "L"}]},
            {"attribute": {"slug": "chat-lieu"}, "values": [{"name": "Cotton"}]},
        ],
        "variants": [{"sku": "SM-NAM-0001", "quantityAvailable": 0,
                      "pricing": {"price": {"gross": {"amount": 199000.0, "currency": "VND"}}}}],
    }
    node.update(overrides)
    return node


def test_product_document_payload_and_text():
    doc = product_document(_node())
    assert doc.key == "product:SM-NAM-0001"
    assert doc.payload == {
        "doc_type": "product", "sku": "SM-NAM-0001", "name": "Áo thun nam cổ tròn basic đen",
        "category": "thoi-trang-nam", "price_vnd": 199000, "color": "Đen", "material": "Cotton",
        "sizes": ["M", "L", "XL"], "in_stock": False,
    }
    assert "Mô tả ngắn." in doc.text and "Thời trang nam" in doc.text and "Kích cỡ: M, L, XL" in doc.text


def test_point_id_and_hash_are_deterministic_and_order_insensitive():
    a = product_document(_node())
    shuffled = _node(attributes=list(reversed(_node()["attributes"])))
    b = product_document(shuffled)
    assert a.point_id == b.point_id and a.content_sha256 == b.content_sha256
    assert a.point_id == Document("product:SM-NAM-0001", "x", {}).point_id  # id depends on the key only
    changed = product_document(_node(name="Tên khác"))
    assert changed.point_id == a.point_id and changed.content_sha256 != a.content_sha256


def test_product_document_rejects_unexpected_shapes():
    with pytest.raises(ValueError, match="one variant"):
        product_document(_node(variants=[]))
    bad = _node()
    bad["variants"][0]["pricing"]["price"]["gross"]["currency"] = "USD"
    with pytest.raises(ValueError, match="VND"):
        product_document(bad)


def test_editorjs_to_text():
    assert editorjs_to_text(None) == ""
    assert editorjs_to_text('{"blocks": [{"data": {"text": "a"}}, {"data": {"text": "b"}}]}') == "a\nb"
    assert editorjs_to_text("plain") == "plain"


def test_one_policy_document_per_clause():
    docs = policy_documents()
    expected = sum(len(clauses(d)) for d in POLICY_DOCUMENTS)
    assert len(docs) == expected == 19
    assert len({d.point_id for d in docs}) == expected
    doc = next(d for d in docs if d.key == "policy:van-chuyen:2")
    assert doc.payload["clause"] == 2 and doc.payload["file"] == "van-chuyen" and doc.payload["title"] == "Phí vận chuyển"
    assert "30.000đ" in doc.text and "Điều 2. Phí vận chuyển" in doc.text and "Điều 3" not in doc.text
