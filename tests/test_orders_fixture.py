"""Order fixtures: deterministic, consistent with the catalog, valid for Saleor's orderBulkCreate."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.catalog.schema import load_catalog
from app.orders.schema import PENDING, OrderFixtures, load_orders
from scripts.generate_orders import DEFAULT_SEED, build_orders, render, strip_numbers


def test_file_matches_generator_ignoring_numbers():
    fresh = render(build_orders(load_catalog(), DEFAULT_SEED))
    assert fresh == render(strip_numbers(load_orders()))
    assert fresh == render(build_orders(load_catalog(), DEFAULT_SEED))


def test_recorded_numbers_are_carried_over():
    fixtures = build_orders(load_catalog(), DEFAULT_SEED, {"shopmate-seed-order-002": "42"})
    by_ref = fixtures.by_reference()
    assert by_ref["shopmate-seed-order-002"].number == "42"
    assert by_ref["shopmate-seed-order-001"].number == PENDING


def test_orders_reference_catalog_skus_and_prices():
    catalog = load_catalog().by_sku()
    fixtures = load_orders()
    assert len(fixtures.orders) == 30
    for order in fixtures.orders:
        for line in order.lines:
            assert line.sku in catalog
            assert line.unit_price == catalog[line.sku].price
            assert line.product_name == catalog[line.sku].name


def test_status_variety_and_past_dates():
    fixtures = load_orders()
    assert {o.status for o in fixtures.orders} == {
        "UNCONFIRMED", "UNFULFILLED", "PARTIALLY_FULFILLED", "FULFILLED", "CANCELED"}
    for order in fixtures.orders:
        assert datetime.fromisoformat(order.created_at) < datetime.fromisoformat("2026-10-01T00:00:00+07:00")


def test_schema_rejects_inconsistent_status():
    data = load_orders().model_dump(mode="json")
    fulfilled = next(o for o in data["orders"] if o["status"] == "FULFILLED")
    fulfilled["status"] = "UNFULFILLED"
    with pytest.raises(ValueError, match="does not match fulfillments"):
        OrderFixtures.model_validate(data)
