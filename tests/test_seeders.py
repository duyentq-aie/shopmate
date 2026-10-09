"""Seeders and the drift report against an in-memory fake Saleor (no network, no tokens)."""

from __future__ import annotations

import pytest

from app.catalog.schema import load_catalog
from app.orders.schema import PENDING
from app.orders.schema import load_orders as _load_recorded_orders
from app.saleor.readonly_client import ReadOnlySaleorClient
from scripts import report_vn, seed_catalog, seed_channel, seed_orders
from scripts.seedlib import vn
from scripts.seedlib.client import SeederClient, SeedError
from scripts.generate_orders import strip_numbers
from tests.fake_saleor import FakeSaleor

URL = "http://saleor.test/graphql/"


def load_orders():
    """Fixtures with numbers reset to pending: the fake Saleor assigns its own numbers,
    independent of the real ones recorded in data/seed/orders.vi.json."""
    return strip_numbers(_load_recorded_orders())


@pytest.fixture
def fake():
    return FakeSaleor()


@pytest.fixture
def seeder(fake):
    with SeederClient(URL, "seeder-token", transport=fake) as client:
        yield client


@pytest.fixture
def runtime(fake):
    with ReadOnlySaleorClient(URL, "runtime-token", transport=fake) as client:
        yield client


@pytest.fixture
def seeded(fake, seeder):
    seed_channel.seed_channel(seeder)
    seed_catalog.seed_catalog(seeder, load_catalog())
    seed_orders.create_orders(seeder, load_orders())
    fake.calls.clear()
    return fake


def _vn_product_count(fake) -> int:
    return sum(1 for p in fake.products if any(l["channel"]["slug"] == "vn" for l in p["channelListings"]))


# --- seed_channel ---------------------------------------------------------------


def test_seed_channel_creates_everything_once(fake, seeder):
    default_before = dict(fake.channels["default-channel"])
    first = seed_channel.seed_channel(seeder)
    assert fake.channels["vn"]["currencyCode"] == "VND"
    assert fake.channels["vn"]["defaultCountry"] == {"code": "VN"}
    assert len(fake.warehouses) == 1 and len(fake.zones) == 1
    assert set(first.shipping_method_ids) == {name for name, *_ in vn.SHIPPING_METHODS}
    listings = {m["name"]: m["channelListings"][0] for m in fake.zones[0]["methods"]}
    assert listings["Giao hàng tiêu chuẩn"]["price"]["amount"] == 30_000
    assert listings["Miễn phí vận chuyển"]["minimumOrderPrice"]["amount"] == 500_000

    fake.calls.clear()
    second = seed_channel.seed_channel(seeder)
    assert second.actions == []
    assert fake.mutation_calls() == []
    assert second.channel_id == first.channel_id
    assert fake.channels["default-channel"] == default_before


def test_seed_channel_refuses_existing_vn_with_wrong_currency(fake, seeder):
    fake.add_channel("vn", "USD", "VN")
    with pytest.raises(SeedError, match="Not modifying"):
        seed_channel.seed_channel(seeder)
    assert fake.mutation_calls() == ["SeedWarehouseCreate"]  # nothing touched the channel


def test_seed_channel_relinks_missing_warehouse(fake, seeder):
    seed_channel.seed_channel(seeder)
    fake.channels["vn"]["warehouses"] = []
    result = seed_channel.seed_channel(seeder)
    assert any("linked warehouse" in a for a in result.actions)
    assert [w["slug"] for w in fake.channels["vn"]["warehouses"]] == [vn.WAREHOUSE_SLUG]


# --- seed_catalog ---------------------------------------------------------------


def test_seed_catalog_is_idempotent(fake, seeder):
    catalog = load_catalog()
    seed_channel.seed_channel(seeder)
    first = seed_catalog.seed_catalog(seeder, catalog)
    assert len(first.created) == len(catalog.products) and not first.failed
    count_after_first = _vn_product_count(fake)

    fake.calls.clear()
    second = seed_catalog.seed_catalog(seeder, catalog)
    assert second.created == [] and len(second.skipped) == len(catalog.products)
    assert fake.mutation_calls() == []
    assert _vn_product_count(fake) == count_after_first == len(catalog.products)


def test_seed_catalog_requires_channel(seeder):
    with pytest.raises(SeedError, match="seed_channel"):
        seed_catalog.seed_catalog(seeder, load_catalog())


def test_seed_catalog_refuses_foreign_attribute(fake, seeder):
    seed_channel.seed_channel(seeder)
    fake.attributes.append({"id": "x", "slug": "mau-sac", "name": "Color", "inputType": "DROPDOWN",
                            "type": "PRODUCT_TYPE", "externalReference": None})
    with pytest.raises(SeedError, match="not ShopMate's"):
        seed_catalog.seed_catalog(seeder, load_catalog())


def test_product_input_uses_multiselect_sizes():
    product = load_catalog().products[0]
    ids = seed_catalog.CatalogIds("ch", "wh", "pt", {"color": "c", "sizes": "s", "material": "m"}, {product.category: "cat"})
    data = seed_catalog.product_input(product, ids)
    sizes = next(a for a in data["attributes"] if a["id"] == "s")
    assert sizes["multiselect"] == [{"value": s} for s in product.sizes]
    assert data["externalReference"] == data["variants"][0]["sku"] == product.sku
    assert data["variants"][0]["stocks"] == [{"warehouse": "wh", "quantity": product.stock}]


def test_reset_vn_deletes_only_shopmate_products_and_no_orders(seeded, seeder):
    orders_before = list(seeded.orders)
    assert seed_catalog.reset_vn(seeder, confirmed=True) == 0
    assert _vn_product_count(seeded) == 0
    assert [p["name"] for p in seeded.products] == ["Demo Juice"]  # demo data untouched
    assert seeded.orders == orders_before  # orders are fixtures, never deleted


def test_reset_vn_refuses_without_confirmation(seeded, seeder, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    count = _vn_product_count(seeded)
    assert seed_catalog.reset_vn(seeder, confirmed=False) == 1
    assert _vn_product_count(seeded) == count
    assert "SeedProductBulkDelete" not in seeded.calls


# --- seed_orders ---------------------------------------------------------------


def test_seed_orders_is_append_only_and_idempotent(fake, seeder, runtime):
    fixtures = load_orders()
    seed_channel.seed_channel(seeder)
    seed_catalog.seed_catalog(seeder, load_catalog())

    first = seed_orders.create_orders(seeder, fixtures)
    assert len(first.created) == 30 and not first.failed
    second = seed_orders.create_orders(seeder, fixtures)
    assert second.created == {} and len(second.existing) == 30 and not second.failed
    assert len(fake.orders) == 30

    numbers, problems = seed_orders.read_back_numbers(runtime, fixtures)
    assert problems == [] and numbers == first.created
    merged, conflicts = seed_orders.merge_numbers(fixtures, numbers)
    assert conflicts == [] and all(o.number != PENDING for o in merged.orders)


def test_existing_order_with_deleted_product_is_still_existing(seeded, seeder):
    fixtures = load_orders()
    seeded.delete_product_keep_orders(fixtures.orders[0].lines[0].sku)
    result = seed_orders.create_orders(seeder, fixtures)
    assert result.failed == {} and len(result.existing) == 30


def test_merge_numbers_never_overwrites_a_recorded_number():
    fixtures = load_orders()
    ref = fixtures.orders[0].external_reference
    recorded = fixtures.model_copy(update={"orders": [fixtures.orders[0].model_copy(update={"number": "7"}), *fixtures.orders[1:]]})
    merged, conflicts = seed_orders.merge_numbers(recorded, {ref: "8"})
    assert merged.by_reference()[ref].number == "7"
    assert conflicts and ref in conflicts[0]


def test_order_input_shape():
    fixture = next(o for o in load_orders().orders if o.status == "PARTIALLY_FULFILLED")
    ids = seed_orders.OrderIds("wh", {fixture.shipping_method: "sm"}, "tc")
    data = seed_orders.order_input(fixture, ids)
    assert data["deliveryMethod"]["shippingTaxClassId"] == "tc"
    assert all(line["taxClassId"] == "tc" for line in data["lines"])
    # Saleor 3.23.40 cannot resolve variantSku (D32): variants go by externalReference (= SKU).
    assert all("variantSku" not in line and line["variantExternalReference"] == line["productSku"] for line in data["lines"])
    assert all("variantSku" not in fl for f in data["fulfillments"] for fl in f["lines"])
    assert data["channel"] == "vn" and data["currency"] == "VND"
    assert data["externalReference"] == fixture.external_reference
    assert data["deliveryMethod"]["shippingMethodId"] == "sm"
    assert data["fulfillments"][0]["lines"][0]["orderLineIndex"] == 0
    assert sum(l["totalPrice"]["gross"] for l in data["lines"]) == fixture.subtotal


# --- report_vn ---------------------------------------------------------------


def test_report_has_no_drift_after_seeding(seeded, seeder, runtime):
    count, products = report_vn.fetch_products(seeder)
    assert count == len(load_catalog().products)
    assert report_vn.diff_catalog(load_catalog(), products).mismatches == []
    order_count, orders = report_vn.fetch_orders(runtime)
    assert order_count == 30
    findings = report_vn.diff_orders(load_orders(), orders)
    assert findings.mismatches == [] and findings.warnings == []


def test_report_detects_catalog_drift(seeded, seeder):
    catalog = load_catalog()
    target = seeded.product_by_ref(catalog.products[0].sku)
    target["variants"][0]["stocks"][0]["quantity"] += 1
    target["name"] = "Đổi tên"
    seeded.products.remove(seeded.product_by_ref(catalog.products[1].sku))
    _, products = report_vn.fetch_products(seeder)
    mismatches = report_vn.diff_catalog(catalog, products).mismatches
    assert any(catalog.products[0].sku in m and "stock" in m and "name" in m for m in mismatches)
    assert any(f"missing product in vn: {catalog.products[1].sku}" == m for m in mismatches)


def test_report_detects_order_drift_and_warns_on_deleted_product(seeded, runtime):
    fixtures = load_orders()
    seeded.orders[0]["status"] = "CANCELED" if fixtures.orders[0].status != "CANCELED" else "UNFULFILLED"
    seeded.delete_product_keep_orders(fixtures.orders[1].lines[0].sku)
    seeded.orders.pop()  # an order disappears
    _, orders = report_vn.fetch_orders(runtime)
    findings = report_vn.diff_orders(fixtures, orders)
    assert any(fixtures.orders[0].external_reference in m and "status" in m for m in findings.mismatches)
    assert any(f"missing order in vn: {fixtures.orders[-1].external_reference}" == m for m in findings.mismatches)
    assert any("no longer exists" in w for w in findings.warnings)
    assert not any(fixtures.orders[1].external_reference in m for m in findings.mismatches)


def test_shipping_method_id_is_converted_to_shipping_method_type():
    import base64
    type_id = base64.b64encode(b"ShippingMethodType:22").decode()
    assert base64.b64decode(seed_orders.shipping_method_global_id(type_id)) == b"ShippingMethod:22"
    with pytest.raises(SeedError):
        seed_orders.shipping_method_global_id(base64.b64encode(b"Product:1").decode())
