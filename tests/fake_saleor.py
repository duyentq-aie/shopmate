"""In-memory fake of the small part of Saleor's GraphQL API the seeders use.

It is an httpx transport: the real SeederClient / ReadOnlySaleorClient talk to it
unchanged. Requests are dispatched by operation name. It models only what the
scripts rely on (lookups, creates, UNIQUE external references, pagination); it
does not model permissions or Saleor's full validation, so it proves our logic,
not Saleor's behaviour. That is verified against the real API in step H.
"""

from __future__ import annotations

import base64
import itertools
import json

import httpx
from graphql import OperationDefinitionNode, parse


def _gid(kind: str, n: int) -> str:
    return base64.b64encode(f"{kind}:{n}".encode()).decode()


class FakeSaleor(httpx.BaseTransport):
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._order_numbers = itertools.count(101)
        self.calls: list[str] = []
        self.channels: dict[str, dict] = {}
        self.warehouses: list[dict] = []
        self.zones: list[dict] = []
        self.attributes: list[dict] = []
        self.product_types: list[dict] = []
        self.categories: list[dict] = []
        self.products: list[dict] = []
        self.orders: list[dict] = []
        # Pre-existing demo data that must never be touched.
        self.add_channel("default-channel", "USD", "US")
        self.products.append(self._demo_product())

    # --- helpers -------------------------------------------------------------

    def new_id(self, kind: str) -> str:
        return _gid(kind, next(self._ids))

    def add_channel(self, slug: str, currency: str, country: str) -> dict:
        channel = {"id": self.new_id("Channel"), "slug": slug, "name": slug, "currencyCode": currency,
                   "isActive": True, "defaultCountry": {"code": country}, "warehouses": []}
        self.channels[slug] = channel
        return channel

    def _demo_product(self) -> dict:
        return {"id": self.new_id("Product"), "name": "Demo Juice", "externalReference": None,
                "productType": {"slug": "juice"}, "category": {"slug": "juices"}, "attributes": [],
                "channelListings": [{"channel": {"slug": "default-channel"}, "isPublished": True}],
                "variants": [{"sku": "DEMO-1", "channelListings": [], "stocks": []}]}

    def _channel_by_id(self, channel_id: str) -> dict:
        return next(c for c in self.channels.values() if c["id"] == channel_id)

    def _warehouse_by_id(self, warehouse_id: str) -> dict:
        return next(w for w in self.warehouses if w["id"] == warehouse_id)

    def product_by_ref(self, ref: str) -> dict | None:
        return next((p for p in self.products if p["externalReference"] == ref), None)

    def mutation_calls(self) -> list[str]:
        writes = ("Create", "Update", "Link", "Listing", "Assign", "Delete", "AddWarehouse")
        return [c for c in self.calls if any(w in c for w in writes)]

    @staticmethod
    def _page(items: list[dict], first: int, after: str | None) -> dict:
        start = int(after) if after else 0
        chunk = items[start : start + first]
        end = start + len(chunk)
        return {
            "totalCount": len(items),
            "pageInfo": {"hasNextPage": end < len(items), "endCursor": str(end)},
            "edges": [{"node": n} for n in chunk],
        }

    # --- transport -------------------------------------------------------------

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        op = next(d for d in parse(body["query"]).definitions if isinstance(d, OperationDefinitionNode))
        name = op.name.value
        self.calls.append(name)
        handler = getattr(self, f"op_{name}", None)
        if handler is None:
            return httpx.Response(200, json={"errors": [{"message": f"fake: no handler for {name}"}]})
        return httpx.Response(200, json={"data": handler(body.get("variables") or {})})

    # --- channel / warehouse / shipping ---------------------------------------------

    def op_SeedChannel(self, v):
        c = self.channels.get(v["slug"])
        if c is None:
            return {"channel": None}
        return {"channel": {**c, "warehouses": [{"id": w["id"], "slug": w["slug"]} for w in c["warehouses"]]}}

    def op_ChannelBySlug(self, v):
        c = self.channels.get(v["slug"])
        return {"channel": None if c is None else {k: c[k] for k in ("id", "slug", "name", "currencyCode", "defaultCountry")}}

    def op_SeedWarehouse(self, v):
        return {"warehouses": self._page([w for w in self.warehouses if w["slug"] == v["slug"]], 1, None)}

    def op_SeedWarehouseCreate(self, v):
        w = {"id": self.new_id("Warehouse"), "slug": v["input"]["slug"], "name": v["input"]["name"],
             "shippingZones": {"edges": []}}
        self.warehouses.append(w)
        return {"createWarehouse": {"warehouse": {"id": w["id"], "slug": w["slug"]}, "errors": []}}

    def op_SeedChannelCreate(self, v):
        i = v["input"]
        c = self.add_channel(i["slug"], i["currencyCode"], i["defaultCountry"])
        c["warehouses"] = [self._warehouse_by_id(w) for w in i.get("addWarehouses", [])]
        return {"channelCreate": {"channel": {"id": c["id"], "slug": c["slug"]}, "errors": []}}

    def op_SeedChannelAddWarehouse(self, v):
        c = self._channel_by_id(v["id"])
        c["warehouses"].append(self._warehouse_by_id(v["warehouse"]))
        return {"channelUpdate": {"channel": {"id": c["id"], "slug": c["slug"]}, "errors": []}}

    def _zone_view(self, z: dict) -> dict:
        return {
            "id": z["id"], "name": z["name"], "countries": [{"code": c} for c in z["countries"]],
            "channels": [{"id": cid, "slug": self._channel_by_id(cid)["slug"]} for cid in z["channels"]],
            "warehouses": [{"id": wid, "slug": self._warehouse_by_id(wid)["slug"]} for wid in z["warehouses"]],
            "shippingMethods": z["methods"],
        }

    def op_SeedShippingZones(self, v):
        found = [self._zone_view(z) for z in self.zones if v["search"].lower() in z["name"].lower()]
        return {"shippingZones": self._page(found, 20, None)}

    def op_SeedShippingZoneCreate(self, v):
        i = v["input"]
        z = {"id": self.new_id("ShippingZone"), "name": i["name"], "countries": i["countries"],
             "channels": list(i.get("addChannels", [])), "warehouses": list(i.get("addWarehouses", [])), "methods": []}
        self.zones.append(z)
        return {"shippingZoneCreate": {"shippingZone": {"id": z["id"], "name": z["name"]}, "errors": []}}

    def op_SeedShippingZoneLink(self, v):
        z = next(z for z in self.zones if z["id"] == v["id"])
        z["channels"] += v["input"].get("addChannels", [])
        z["warehouses"] += v["input"].get("addWarehouses", [])
        return {"shippingZoneUpdate": {"shippingZone": {"id": z["id"]}, "errors": []}}

    def op_SeedShippingPriceCreate(self, v):
        i = v["input"]
        z = next(z for z in self.zones if z["id"] == i["shippingZone"])
        m = {"id": self.new_id("ShippingMethodType"), "name": i["name"], "type": i["type"],
             "minimumDeliveryDays": i.get("minimumDeliveryDays"), "maximumDeliveryDays": i.get("maximumDeliveryDays"),
             "channelListings": []}
        z["methods"].append(m)
        return {"shippingPriceCreate": {"shippingMethod": {"id": m["id"], "name": m["name"]}, "errors": []}}

    def op_SeedShippingListing(self, v):
        m = next(m for z in self.zones for m in z["methods"] if m["id"] == v["id"])
        for add in v["input"]["addChannels"]:
            channel = self._channel_by_id(add["channelId"])

            def money(x):
                return None if x is None else {"amount": float(x), "currency": channel["currencyCode"]}

            m["channelListings"].append({
                "channel": {"id": channel["id"], "slug": channel["slug"]},
                "price": money(add.get("price")),
                "minimumOrderPrice": money(add.get("minimumOrderPrice") or 0),  # Saleor stores unset min as 0
                "maximumOrderPrice": money(add.get("maximumOrderPrice")),
            })
        return {"shippingMethodChannelListingUpdate": {"shippingMethod": {"id": m["id"]}, "errors": []}}

    # --- catalog --------------------------------------------------------------

    def op_SeedAttributes(self, v):
        return {"attributes": self._page([a for a in self.attributes if a["slug"] in v["slugs"]], 20, None)}

    def op_SeedAttributeCreate(self, v):
        i = v["input"]
        a = {"id": self.new_id("Attribute"), "slug": i["slug"], "name": i["name"], "inputType": i["inputType"],
             "type": i["type"], "externalReference": i.get("externalReference")}
        self.attributes.append(a)
        return {"attributeCreate": {"attribute": {"id": a["id"], "slug": a["slug"]}, "errors": []}}

    def op_SeedProductType(self, v):
        return {"productTypes": self._page([t for t in self.product_types if t["slug"] == v["slug"]], 1, None)}

    def op_SeedProductTypeCreate(self, v):
        i = v["input"]
        t = {"id": self.new_id("ProductType"), "slug": i["slug"], "name": i["name"], "hasVariants": i["hasVariants"],
             "productAttributes": [{"id": a, "slug": self._attr(a)["slug"]} for a in i["productAttributes"]]}
        self.product_types.append(t)
        return {"productTypeCreate": {"productType": {"id": t["id"], "slug": t["slug"]}, "errors": []}}

    def op_SeedProductAttributeAssign(self, v):
        t = next(t for t in self.product_types if t["id"] == v["productTypeId"])
        t["productAttributes"] += [{"id": o["id"], "slug": self._attr(o["id"])["slug"]} for o in v["operations"]]
        return {"productAttributeAssign": {"productType": {"id": t["id"]}, "errors": []}}

    def _attr(self, attr_id: str) -> dict:
        return next(a for a in self.attributes if a["id"] == attr_id)

    def op_SeedCategories(self, v):
        return {"categories": self._page([c for c in self.categories if c["slug"] in v["slugs"]], 50, None)}

    def op_SeedCategoryCreate(self, v):
        i = v["input"]
        c = {"id": self.new_id("Category"), "slug": i["slug"], "name": i["name"], "externalReference": i.get("externalReference")}
        self.categories.append(c)
        return {"categoryCreate": {"category": {"id": c["id"], "slug": c["slug"]}, "errors": []}}

    def op_SeedProductRefs(self, v):
        type_slug = next(t["slug"] for t in self.product_types if t["id"] == v["productType"])
        items = [{"id": p["id"], "externalReference": p["externalReference"], "channelListings": p["channelListings"]}
                 for p in self.products if p["productType"]["slug"] == type_slug]
        return {"products": self._page(items, v["first"], v.get("after"))}

    def op_SeedProductBulkCreate(self, v):
        results = []
        for i in v["products"]:
            if self.product_by_ref(i["externalReference"]):
                results.append({"product": None, "errors": [{"path": "externalReference", "code": "UNIQUE", "message": "exists"}]})
                continue
            attributes = []
            for value in i["attributes"]:
                names = [value["dropdown"]["value"]] if "dropdown" in value else [m["value"] for m in value["multiselect"]]
                attributes.append({"attribute": {"slug": self._attr(value["id"])["slug"]}, "values": [{"name": n} for n in names]})
            variant = i["variants"][0]
            product = {
                "id": self.new_id("Product"),
                "name": i["name"],
                "externalReference": i["externalReference"],
                "productType": {"slug": next(t["slug"] for t in self.product_types if t["id"] == i["productType"])},
                "category": {"slug": next(c["slug"] for c in self.categories if c["id"] == i["category"])},
                "attributes": attributes,
                "channelListings": [{"channel": {"slug": self._channel_by_id(l["channelId"])["slug"]}, "isPublished": l["isPublished"]}
                                    for l in i["channelListings"]],
                "variants": [{
                    "sku": variant["sku"],
                    "externalReference": variant.get("externalReference"),
                    "channelListings": [{"channel": {"slug": self._channel_by_id(l["channelId"])["slug"]},
                                         "price": {"amount": float(l["price"]), "currency": self._channel_by_id(l["channelId"])["currencyCode"]}}
                                        for l in variant["channelListings"]],
                    "stocks": [{"warehouse": {"slug": self._warehouse_by_id(s["warehouse"])["slug"]}, "quantity": s["quantity"]}
                               for s in variant["stocks"]],
                }],
            }
            self.products.append(product)
            results.append({"product": {"id": product["id"], "externalReference": product["externalReference"]}, "errors": []})
        return {"productBulkCreate": {"count": sum(1 for r in results if r["product"]), "results": results}}

    def op_SeedProductBulkDelete(self, v):
        before = len(self.products)
        self.products = [p for p in self.products if p["id"] not in v["ids"]]
        return {"productBulkDelete": {"count": before - len(self.products), "errors": []}}

    def op_ReportProducts(self, v):
        items = [p for p in self.products if any(l["channel"]["slug"] == v["channel"] for l in p["channelListings"])]
        return {"products": self._page(items, v["first"], v.get("after"))}

    # --- orders ---------------------------------------------------------------------

    def _variant_by_ref(self, ref: str) -> dict | None:
        return next((var for p in self.products for var in p["variants"] if var.get("externalReference") == ref), None)

    def op_SeedTaxClasses(self, v):
        return {"taxClasses": self._page([{"id": _gid("TaxClass", 3), "name": "No taxes"}], 50, None)}

    def op_SeedOrderBulkCreate(self, v):
        """Mirrors the input checks Saleor 3.23.40 applied in the first real run (step H)."""
        results = []
        for i in v["orders"]:
            errors = []
            delivery = i.get("deliveryMethod") or {}
            kind = base64.b64decode(delivery.get("shippingMethodId", "")).decode().partition(":")[0]
            if kind != "ShippingMethod":
                errors.append({"path": "delivery_method.shipping_method_id", "code": "INVALID",
                               "message": f"Expected: ShippingMethod, received: {kind}"})
            if not delivery.get("shippingTaxClassId"):
                errors.append({"path": "delivery_method.shipping_tax_class_id", "code": "NOT_FOUND",
                               "message": "Can't return TaxClass instance."})
            if any(not line.get("taxClassId") for line in i["lines"]):
                errors.append({"path": "lines", "code": "NOT_FOUND", "message": "Can't return TaxClass instance."})
            if any(o["externalReference"] == i["externalReference"] for o in self.orders):
                errors.append({"path": "external_reference", "code": "UNIQUE", "message": "already exists"})
            for n, line in enumerate(i["lines"]):
                if "variantSku" in line:
                    # Saleor 3.23.40 caches variants under the wrong key, so a SKU lookup never matches.
                    errors.append({"path": f"lines.{n}.variant_sku", "code": "NOT_FOUND",
                                   "message": f"ProductVariant instance with sku={line['variantSku']} doesn't exist."})
                elif self._variant_by_ref(line.get("variantExternalReference")) is None:
                    errors.append({"path": f"lines.{n}", "code": "NOT_FOUND", "message": "variant not found"})
            for f in i.get("fulfillments") or []:
                for n, fl in enumerate(f["lines"]):
                    if "variantSku" in fl:
                        errors.append({"path": f"fulfillments.lines.{n}.variant_sku", "code": "NOT_FOUND",
                                       "message": f"ProductVariant instance with sku={fl['variantSku']} doesn't exist."})
            if errors:
                results.append({"order": None, "errors": errors})
                continue
            order = {
                "id": self.new_id("Order"),
                "number": str(next(self._order_numbers)),
                "externalReference": i["externalReference"],
                "status": i["status"],
                "channel": {"slug": i["channel"]},
                "lines": [{"productSku": l["productSku"], "productName": l["productName"], "quantity": l["quantity"],
                           "quantityFulfilled": 0, "variant": {"sku": self._variant_by_ref(l["variantExternalReference"])["sku"]}} for l in i["lines"]],
            }
            self.orders.append(order)
            results.append({"order": {k: order[k] for k in ("id", "number", "externalReference", "status")}, "errors": []})
        return {"orderBulkCreate": {"count": sum(1 for r in results if r["order"]), "results": results}}

    def delete_product_keep_orders(self, ref: str) -> None:
        """Simulate deleting a product: order lines keep productSku but lose the variant."""
        product = self.product_by_ref(ref)
        self.products.remove(product)
        skus = {v["sku"] for v in product["variants"]}
        for order in self.orders:
            for line in order["lines"]:
                if line["variant"] and line["variant"]["sku"] in skus:
                    line["variant"] = None

    def op_OrderByExternalReference(self, v):
        return {"order": next((o for o in self.orders if o["externalReference"] == v["externalReference"]), None)}

    def op_OrdersInChannel(self, v):
        slug = self._channel_by_id(v["channelId"])["slug"]
        return {"orders": self._page([o for o in self.orders if o["channel"]["slug"] == slug], v["first"], v.get("after"))}

    def op_OrderByNumber(self, v):
        found = [o for o in self.orders if o["number"] == v["number"]]
        return {"orders": self._page(found, 1, None)}
