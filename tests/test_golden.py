"""Golden set: schema, code-computed labels, validator and order-number sync."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.catalog.schema import load_catalog
from app.orders.schema import load_orders
from eval.golden import draft_golden, validate_golden
from eval.golden.draft_golden import build_drafts, render
from eval.golden.schema import (
    IDENTITY_BEHAVIOR,
    GoldenItem,
    GoldenSet,
    ProductFilters,
    apply_filters,
    load_golden,
    test_split_hash,
)
from eval.golden.sync_order_numbers import sync


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


@pytest.fixture(scope="module")
def orders():
    return load_orders()


def _with_pending_numbers(golden: GoldenSet) -> GoldenSet:
    """The golden set as it was before sync_order_numbers ran, independent of the file's state."""
    data = golden.model_dump(mode="json")
    for item in data["items"]:
        if item["gold_source"]["type"] == "order":
            item["gold_source"]["order_number"] = "pending"
    return GoldenSet.model_validate(data)


def _replace_item(golden: GoldenSet, item_id: str, **changes) -> GoldenSet:
    data = golden.model_dump(mode="json")
    for item in data["items"]:
        if item["id"] == item_id:
            item.update(changes)
    return GoldenSet.model_validate(data)


def test_committed_golden_set_passes_validator(catalog, orders):
    golden = load_golden()
    assert validate_golden.check(golden, catalog, orders, require_order_numbers=False) == []
    assert validate_golden.main([]) == 0


def test_strict_mode_rejects_pending_order_numbers(catalog, orders):
    golden = _with_pending_numbers(load_golden())
    problems = validate_golden.check(golden, catalog, orders, require_order_numbers=True)
    assert problems and all("pending" in p for p in problems)


def test_drafts_are_deterministic_and_all_need_review(catalog, orders):
    first = build_drafts(catalog, orders)
    assert render(first) == render(build_drafts(catalog, orders))
    assert len(first.items) == 64
    assert {i.status for i in first.items} == {"draft_needs_human_review"}
    assert {i.author for i in first.items} == {"agent_draft"}
    test_share = sum(i.split == "test" for i in first.items) / len(first.items)
    assert 0.25 <= test_share <= 0.35


def test_gold_skus_are_computed_from_filters(catalog):
    golden = load_golden()
    for item in golden.items:
        if item.expected_filters and item.gold_source.type == "skus":
            assert item.gold_source.skus == apply_filters(catalog, item.expected_filters)


def test_validator_catches_drifted_sku_labels(catalog, orders):
    golden = load_golden()
    item = next(i for i in golden.items if i.gold_source.type == "skus" and len(i.gold_source.skus) > 1)
    tampered = _replace_item(golden, item.id, gold_source={"type": "skus", "skus": item.gold_source.skus[:1]})
    problems = validate_golden.check(tampered, catalog, orders, require_order_numbers=False)
    assert any(item.id in p and "filters" in p for p in problems)


def test_validator_catches_unknown_sku_bad_clause_and_wrong_status(catalog, orders):
    golden = load_golden()
    sku_item = next(i for i in golden.items if i.gold_source.type == "skus" and i.expected_filters)
    policy_item = next(i for i in golden.items if i.gold_source.type == "policy_clause")
    order_item = next(i for i in golden.items if i.gold_source.type == "order")
    wrong_status = "CANCELED" if order_item.gold_source.expected_status != "CANCELED" else "FULFILLED"
    tampered = _replace_item(golden, sku_item.id, gold_source={"type": "skus", "skus": ["SM-NAM-9999"]})
    tampered = _replace_item(tampered, policy_item.id,
                             gold_source={"type": "policy_clause", "document": "doi-tra", "clause": 99})
    tampered = _replace_item(tampered, order_item.id,
                             gold_source={**order_item.gold_source.model_dump(), "expected_status": wrong_status})
    problems = "\n".join(validate_golden.check(tampered, catalog, orders, require_order_numbers=False))
    assert "SM-NAM-9999" in problems and "Điều 99" in problems and "expected_status" in problems


@pytest.mark.parametrize(
    "changes",
    [
        {"intent": "shopping"},
        {"query_style": "english"},
        {"split": "train"},
        {"status": "approved"},
        {"gold_source": {"type": "skus", "skus": ["SM-NAM-0001"]}},  # smalltalk cannot have SKUs
    ],
)
def test_schema_rejects_bad_labels(changes):
    base = next(i for i in load_golden().items if i.intent == "smalltalk").model_dump(mode="json")
    with pytest.raises(ValidationError):
        GoldenItem.model_validate({**base, **changes})


def test_order_placeholder_rule():
    base = next(i for i in load_golden().items if i.intent == "order_status").model_dump(mode="json")
    with pytest.raises(ValidationError, match="order_number"):
        GoldenItem.model_validate({**base, "question": "Đơn của mình đâu?"})


def test_filters_must_not_be_empty():
    with pytest.raises(ValidationError):
        ProductFilters()


def test_test_split_hash_tracks_only_test_items():
    golden = load_golden()
    digest = test_split_hash(golden)
    dev_item = next(i for i in golden.items if i.split == "dev")
    test_item = next(i for i in golden.items if i.split == "test")
    assert test_split_hash(_replace_item(golden, dev_item.id, notes="edited dev item")) == digest
    assert test_split_hash(_replace_item(golden, test_item.id, notes="edited test item")) != digest


def test_sync_fills_pending_numbers_and_keeps_human_edits(orders):
    golden = _with_pending_numbers(load_golden())
    order_item = next(i for i in golden.items if i.gold_source.type == "order")
    edited = _replace_item(golden, order_item.id, notes="human note", status="human_reviewed")
    numbered = orders.model_copy(update={"orders": [o.model_copy(update={"number": str(500 + n)})
                                                    for n, o in enumerate(orders.orders)]})
    synced, changed, conflicts = sync(edited, numbered)
    assert conflicts == [] and changed == sum(1 for i in golden.items if i.gold_source.type == "order")
    item = next(i for i in synced.items if i.id == order_item.id)
    assert item.notes == "human note" and item.status == "human_reviewed"
    assert item.gold_source.order_number == numbered.by_reference()[item.gold_source.external_reference].number


# --- anti-IDOR order drafts (D28) ---------------------------------------------------


def test_order_drafts_cover_verify_or_refuse():
    sources = [i.gold_source for i in load_golden().items if i.gold_source.type == "order"]
    behaviors = {(s.provided_identity, s.expected_behavior) for s in sources}
    assert {("matching_email", "answer"), ("none", "ask_for_verification"), ("mismatched_email", "refuse")} <= behaviors
    assert behaviors <= {*IDENTITY_BEHAVIOR.items()}
    ids = {i.id for i in load_golden().items}
    assert {"gs-061", "gs-062", "gs-063", "gs-064"} <= ids


def test_schema_rejects_reveal_without_matching_identity():
    base = next(i for i in load_golden().items
                if i.gold_source.type == "order" and i.gold_source.provided_identity == "none").model_dump(mode="json")
    for behavior in ("answer", "refuse"):
        with pytest.raises(ValidationError, match="requires expected_behavior"):
            GoldenItem.model_validate({**base, "gold_source": {**base["gold_source"], "expected_behavior": behavior}})


def test_validator_checks_email_against_order_owner(catalog, orders):
    golden = load_golden()
    owner_of = {o.external_reference: o.customer.email for o in orders.orders}
    matching = next(i for i in golden.items
                    if i.gold_source.type == "order" and i.gold_source.provided_identity == "matching_email")
    owner = owner_of[matching.gold_source.external_reference]
    stranger = next(e for e in sorted(set(owner_of.values())) if e != owner)
    tampered = _replace_item(golden, matching.id, question=matching.question.replace(owner, stranger))
    problems = validate_golden.check(tampered, catalog, orders, require_order_numbers=False)
    assert any(matching.id in p and "provided_identity" in p for p in problems)

    ask = next(i for i in golden.items if i.gold_source.type == "order" and i.gold_source.provided_identity == "none")
    tampered = _replace_item(golden, ask.id, question=ask.question + f" email {owner}")
    problems = validate_golden.check(tampered, catalog, orders, require_order_numbers=False)
    assert any(ask.id in p for p in problems)


def test_force_never_overwrites_reviewed_items(tmp_path):
    out = tmp_path / "golden.json"
    data = load_golden().model_dump(mode="json", exclude_none=True)
    data["items"][0]["status"] = "human_reviewed"
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    before = out.read_bytes()
    assert draft_golden.main(["--out", str(out), "--force"]) == 1
    assert out.read_bytes() == before


def test_committed_golden_set_passes_strict_mode_once_numbers_are_recorded(catalog, orders):
    golden = load_golden()
    if all(i.gold_source.type != "order" or i.gold_source.order_number != "pending" for i in golden.items):
        assert validate_golden.check(golden, catalog, orders, require_order_numbers=True) == []
    else:
        assert any(o.number == "pending" for o in orders.orders), "orders are seeded; run sync_order_numbers"
