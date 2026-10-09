"""Create the synthetic test orders of data/seed/orders.vi.json in channel `vn`.

Orders are APPEND-ONLY fixtures:
- keyed by `externalReference` (shopmate-seed-order-NNN); missing ones are created,
  existing ones are never recreated or modified (Saleor answers UNIQUE for them);
- there is no reset mode: Saleor cannot delete non-draft orders, and nothing here tries.

Created through `orderBulkCreate` (permission MANAGE_ORDERS_IMPORT only) with
stockUpdatePolicy SKIP, so catalog stock levels are not changed.

Order numbers are assigned by Saleor. They are read back with the READ-ONLY runtime
client (SALEOR_RUNTIME_TOKEN), so the seeder app never needs MANAGE_ORDERS, and
recorded into data/seed/orders.vi.json.

Usage (from the repo root; run seed_channel and seed_catalog first):
    uv run python -m scripts.seed_orders
"""

from __future__ import annotations

import base64
import sys
from dataclasses import dataclass, field

from app.config import api_url, get_setting
from app.orders.schema import ORDERS_PATH, PENDING, OrderFixture, OrderFixtures, load_orders
from app.saleor.readonly_client import ReadOnlySaleorClient, ReadOnlyViolation, SaleorQueryError
from scripts.generate_orders import render
from scripts.seedlib import documents as d
from scripts.seedlib import vn
from scripts.seedlib.client import SeederClient, SeedError, seeder_from_env

LANGUAGE_CODE = "VI"
# orderBulkCreate needs a tax class for every line and for shipping. ShopMate prices are
# tax-inclusive with rate 0, so it references Saleor's existing "No taxes" class (read only;
# creating our own would need MANAGE_TAXES). See docs/DECISIONS-phase2.md, D31.
TAX_CLASS_NAME = "No taxes"


@dataclass
class OrderIds:
    warehouse_id: str
    shipping_method_ids: dict[str, str]  # name -> "ShippingMethod" global id
    tax_class_id: str


@dataclass
class OrderSeedResult:
    created: dict[str, str] = field(default_factory=dict)  # external_reference -> number
    existing: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def shipping_method_global_id(type_id: str) -> str:
    """Zone queries return `ShippingMethodType:<pk>` ids; orderBulkCreate expects `ShippingMethod:<pk>`
    (same primary key, as in Saleor's own tests)."""
    kind, _, pk = base64.b64decode(type_id).decode().partition(":")
    if kind not in ("ShippingMethodType", "ShippingMethod") or not pk:
        raise SeedError(f"unexpected shipping method id {type_id!r}")
    return base64.b64encode(f"ShippingMethod:{pk}".encode()).decode()


def _money(amount: int) -> dict:
    return {"gross": amount, "net": amount}


def lookup_ids(client: SeederClient) -> OrderIds:
    channel = client.request(d.CHANNEL_BY_SLUG, {"slug": vn.CHANNEL_SLUG})["channel"]
    if channel is None:
        raise SeedError(f"channel {vn.CHANNEL_SLUG} does not exist; run scripts.seed_channel first")
    warehouses = {w["slug"]: w["id"] for w in channel["warehouses"]}
    if vn.WAREHOUSE_SLUG not in warehouses:
        raise SeedError(f"warehouse {vn.WAREHOUSE_SLUG} not linked to {vn.CHANNEL_SLUG}; run scripts.seed_channel")
    zones = client.request(d.SHIPPING_ZONES, {"search": vn.SHIPPING_ZONE_NAME})["shippingZones"]["edges"]
    zone = next((z["node"] for z in zones if z["node"]["name"] == vn.SHIPPING_ZONE_NAME), None)
    if zone is None:
        raise SeedError(f"shipping zone {vn.SHIPPING_ZONE_NAME!r} not found; run scripts.seed_channel")
    methods = {m["name"]: shipping_method_global_id(m["id"]) for m in zone["shippingMethods"]}
    tax_classes = [e["node"] for e in client.request(d.TAX_CLASSES)["taxClasses"]["edges"]]
    tax_class = next((t for t in tax_classes if t["name"] == TAX_CLASS_NAME), None)
    if tax_class is None:
        raise SeedError(f"tax class {TAX_CLASS_NAME!r} not found in Saleor")
    return OrderIds(warehouses[vn.WAREHOUSE_SLUG], methods, tax_class["id"])


def _address(order: OrderFixture) -> dict:
    a = order.shipping_address
    return {
        "firstName": order.customer.first_name,
        "lastName": order.customer.last_name,
        "streetAddress1": a.street,
        "cityArea": a.city_area,
        "city": a.city,
        "countryArea": a.country_area,
        "country": a.country,
    }


def order_input(order: OrderFixture, ids: OrderIds) -> dict:
    method_id = ids.shipping_method_ids.get(order.shipping_method)
    if method_id is None:
        raise SeedError(f"{order.external_reference}: shipping method {order.shipping_method!r} not found in Saleor")
    address = _address(order)
    return {
        "externalReference": order.external_reference,
        "channel": vn.CHANNEL_SLUG,
        "createdAt": order.created_at,
        "status": order.status,
        "user": {"email": order.customer.email},
        "billingAddress": address,
        "shippingAddress": address,
        "currency": "VND",
        "languageCode": LANGUAGE_CODE,
        "lines": [
            {
                # Saleor 3.23.40 bug: lookup by variantSku never matches (D32). seed_catalog sets the
                # variant externalReference to the SKU, and that lookup works.
                "variantExternalReference": line.sku,
                "productSku": line.sku,  # kept on the line even if the product is deleted later
                "productName": line.product_name,
                "createdAt": order.created_at,
                "isShippingRequired": True,
                "isGiftCard": False,
                "quantity": line.quantity,
                "totalPrice": _money(line.total),
                "undiscountedTotalPrice": _money(line.total),
                "warehouse": ids.warehouse_id,
                "taxRate": 0,
                "taxClassId": ids.tax_class_id,
            }
            for line in order.lines
        ],
        "deliveryMethod": {
            "shippingMethodId": method_id,
            "shippingMethodName": order.shipping_method,
            "shippingPrice": _money(order.shipping_price),
            "shippingTaxRate": 0,
            "shippingTaxClassId": ids.tax_class_id,
        },
        "fulfillments": [
            {
                "trackingCode": f.tracking_code,
                "lines": [
                    {
                        "variantExternalReference": order.lines[fl.line_index].sku,  # see D32
                        "quantity": fl.quantity,
                        "warehouse": ids.warehouse_id,
                        "orderLineIndex": fl.line_index,
                    }
                    for fl in f.lines
                ],
            }
            for f in order.fulfillments
        ],
    }


def _is_duplicate(errors: list[dict]) -> bool:
    """The order already exists. Other errors on the same row (e.g. a since-deleted
    product) do not matter, because an existing order is never recreated."""
    return any(e.get("code") == "UNIQUE" and e.get("path") == "external_reference" for e in errors)


def create_orders(client: SeederClient, fixtures: OrderFixtures) -> OrderSeedResult:
    ids = lookup_ids(client)
    result = OrderSeedResult()
    inputs = [order_input(o, ids) for o in fixtures.orders]
    payload = client.request(d.ORDER_BULK_CREATE, {"orders": inputs})["orderBulkCreate"]
    for fixture, row in zip(fixtures.orders, payload["results"]):
        errors = row.get("errors") or []
        order = row.get("order")
        if order and not errors:
            result.created[fixture.external_reference] = str(order["number"])
        elif _is_duplicate(errors):
            result.existing.append(fixture.external_reference)
        else:
            result.failed[fixture.external_reference] = str(errors)
    return result


def read_back_numbers(runtime: ReadOnlySaleorClient, fixtures: OrderFixtures) -> tuple[dict[str, str], list[str]]:
    """Read each fixture's order number with the read-only client. Returns (numbers, problems)."""
    numbers: dict[str, str] = {}
    problems: list[str] = []
    for fixture in fixtures.orders:
        order = runtime.execute("OrderByExternalReference", {"externalReference": fixture.external_reference})["order"]
        if order is None:
            problems.append(f"{fixture.external_reference}: not found in Saleor")
            continue
        if order["channel"]["slug"] != vn.CHANNEL_SLUG:
            problems.append(f"{fixture.external_reference}: in channel {order['channel']['slug']}, expected {vn.CHANNEL_SLUG}")
            continue
        numbers[fixture.external_reference] = str(order["number"])
    return numbers, problems


def merge_numbers(fixtures: OrderFixtures, numbers: dict[str, str]) -> tuple[OrderFixtures, list[str]]:
    """Fill pending numbers; a recorded number that differs from Saleor is a conflict, never overwritten."""
    conflicts = []
    updated = []
    for order in fixtures.orders:
        new = numbers.get(order.external_reference)
        if new is None or new == order.number:
            updated.append(order)
        elif order.number == PENDING:
            updated.append(order.model_copy(update={"number": new}))
        else:
            conflicts.append(f"{order.external_reference}: file has #{order.number}, Saleor has #{new}")
            updated.append(order)
    return fixtures.model_copy(update={"orders": updated}), conflicts


def main() -> int:
    fixtures = load_orders()
    with seeder_from_env() as client:
        try:
            result = create_orders(client, fixtures)
        except SeedError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    print(f"orders: {len(result.created)} created, {len(result.existing)} already existed, {len(result.failed)} failed")
    for ref, error in result.failed.items():
        print(f"  FAILED {ref}: {error}", file=sys.stderr)

    numbers = dict(result.created)
    problems: list[str] = []
    runtime_token = get_setting("SALEOR_RUNTIME_TOKEN")
    if runtime_token:
        try:
            with ReadOnlySaleorClient(api_url(), runtime_token) as runtime:
                read, problems = read_back_numbers(runtime, fixtures)
            numbers.update(read)
        except (SaleorQueryError, ReadOnlyViolation) as exc:
            problems = [f"reading order numbers with the runtime token failed: {exc}"]
    else:
        print("WARNING: SALEOR_RUNTIME_TOKEN not set; only numbers of newly created orders are recorded", file=sys.stderr)

    merged, conflicts = merge_numbers(fixtures, numbers)
    if merged != fixtures:
        ORDERS_PATH.write_bytes(render(merged).encode("utf-8"))
        print(f"recorded order numbers in {ORDERS_PATH}")
    pending = sum(1 for o in merged.orders if o.number == PENDING)
    print(f"order numbers: {len(merged.orders) - pending} recorded, {pending} pending")
    for line in problems + conflicts:
        print(f"  PROBLEM {line}", file=sys.stderr)
    return 1 if result.failed or problems or conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
