"""Load data/seed/catalog.vi.json into Saleor channel `vn`.

Idempotent: products are keyed by `externalReference` = SKU. Products that already
exist are skipped (NOT updated: use scripts/report_vn.py to detect drift).
Attributes, the product type and categories are created once and recognised by
their ShopMate slugs / external references; objects that exist but are not
ShopMate's are never reused or modified.

Product images are not seeded (see docs/SETUP.md).

Usage (from the repo root, needs SALEOR_SEEDER_TOKEN; run seed_channel first):
    uv run python -m scripts.seed_catalog
    uv run python -m scripts.seed_catalog --reset-vn   # DESTRUCTIVE, asks for confirmation
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field

from app.catalog.schema import ATTRIBUTES, SKU_PATTERN, Catalog, Product, load_catalog
from scripts.seedlib import documents as d
from scripts.seedlib import vn
from scripts.seedlib.client import SeederClient, SeedError, seeder_from_env

BATCH_SIZE = 25
RESET_CONFIRMATION = "xoa-san-pham-vn"


@dataclass
class CatalogIds:
    channel_id: str
    warehouse_id: str
    product_type_id: str
    attribute_ids: dict[str, str]  # catalog field -> attribute id
    category_ids: dict[str, str]  # category slug -> id


@dataclass
class SeedResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)


def editorjs(text: str) -> str:
    """Saleor stores descriptions as EditorJS JSON."""
    return json.dumps({"blocks": [{"type": "paragraph", "data": {"text": text}}], "version": "2.24.3"}, ensure_ascii=False)


def lookup_channel_and_warehouse(client: SeederClient) -> tuple[str, str]:
    channel = client.request(d.CHANNEL_BY_SLUG, {"slug": vn.CHANNEL_SLUG})["channel"]
    if channel is None:
        raise SeedError(f"channel {vn.CHANNEL_SLUG} does not exist; run scripts.seed_channel first")
    warehouses = {w["slug"]: w["id"] for w in channel["warehouses"]}
    if vn.WAREHOUSE_SLUG not in warehouses:
        raise SeedError(f"warehouse {vn.WAREHOUSE_SLUG} is not linked to channel {vn.CHANNEL_SLUG}; run scripts.seed_channel")
    return channel["id"], warehouses[vn.WAREHOUSE_SLUG]


def ensure_attributes(client: SeederClient, actions: list[str]) -> dict[str, str]:
    slugs = [slug for slug, _, _ in ATTRIBUTES.values()]
    edges = client.request(d.ATTRIBUTES_BY_SLUG, {"slugs": slugs})["attributes"]["edges"]
    found = {e["node"]["slug"]: e["node"] for e in edges}
    ids: dict[str, str] = {}
    for catalog_field, (slug, name, input_type) in ATTRIBUTES.items():
        ref = f"{vn.ATTRIBUTE_REF_PREFIX}{slug}"
        node = found.get(slug)
        if node is not None:
            if node.get("externalReference") != ref:
                raise SeedError(f"attribute {slug!r} exists but is not ShopMate's (externalReference {node.get('externalReference')!r})")
            if node["inputType"] != input_type:
                raise SeedError(f"attribute {slug!r} has input type {node['inputType']}, expected {input_type}")
            ids[catalog_field] = node["id"]
            continue
        payload = client.mutate(
            d.ATTRIBUTE_CREATE,
            {
                "input": {
                    "name": name,
                    "slug": slug,
                    "type": "PRODUCT_TYPE",
                    "inputType": input_type,
                    "valueRequired": True,
                    "visibleInStorefront": True,
                    "filterableInStorefront": True,
                    "filterableInDashboard": True,
                    "externalReference": ref,
                }
            },
            "attributeCreate",
        )
        ids[catalog_field] = payload["attribute"]["id"]
        actions.append(f"created attribute {slug} ({input_type})")
    return ids


def ensure_product_type(client: SeederClient, attribute_ids: dict[str, str], actions: list[str]) -> str:
    edges = client.request(d.PRODUCT_TYPE_BY_SLUG, {"slug": vn.PRODUCT_TYPE_SLUG})["productTypes"]["edges"]
    if not edges:
        payload = client.mutate(
            d.PRODUCT_TYPE_CREATE,
            {
                "input": {
                    "name": vn.PRODUCT_TYPE_NAME,
                    "slug": vn.PRODUCT_TYPE_SLUG,
                    "kind": "NORMAL",
                    "hasVariants": False,
                    "isShippingRequired": True,
                    "productAttributes": list(attribute_ids.values()),
                }
            },
            "productTypeCreate",
        )
        actions.append(f"created product type {vn.PRODUCT_TYPE_SLUG}")
        return payload["productType"]["id"]

    node = edges[0]["node"]
    if node["hasVariants"]:
        raise SeedError(f"product type {vn.PRODUCT_TYPE_SLUG} has variants enabled; expected a simple product type")
    assigned = {a["id"] for a in node["productAttributes"]}
    missing = [i for i in attribute_ids.values() if i not in assigned]
    if missing:
        client.mutate(
            d.PRODUCT_ATTRIBUTE_ASSIGN,
            {"productTypeId": node["id"], "operations": [{"id": i, "type": "PRODUCT"} for i in missing]},
            "productAttributeAssign",
        )
        actions.append(f"assigned {len(missing)} attributes to {vn.PRODUCT_TYPE_SLUG}")
    return node["id"]


def ensure_categories(client: SeederClient, catalog: Catalog, actions: list[str]) -> dict[str, str]:
    slugs = [c.slug for c in catalog.categories]
    edges = client.request(d.CATEGORIES_BY_SLUG, {"slugs": slugs})["categories"]["edges"]
    found = {e["node"]["slug"]: e["node"] for e in edges}
    ids: dict[str, str] = {}
    for category in catalog.categories:
        ref = f"{vn.CATEGORY_REF_PREFIX}{category.slug}"
        node = found.get(category.slug)
        if node is not None:
            if node.get("externalReference") != ref:
                raise SeedError(f"category {category.slug!r} exists but is not ShopMate's")
            ids[category.slug] = node["id"]
            continue
        payload = client.mutate(
            d.CATEGORY_CREATE,
            {"input": {"name": category.name, "slug": category.slug, "externalReference": ref}},
            "categoryCreate",
        )
        ids[category.slug] = payload["category"]["id"]
        actions.append(f"created category {category.slug}")
    return ids


def existing_products(client: SeederClient, product_type_id: str) -> dict[str, dict]:
    """All products of the ShopMate product type, keyed by externalReference."""
    found: dict[str, dict] = {}
    after = None
    while True:
        conn = client.request(d.PRODUCT_REFS_BY_TYPE, {"productType": product_type_id, "first": 100, "after": after})["products"]
        for edge in conn["edges"]:
            node = edge["node"]
            if node["externalReference"]:
                found[node["externalReference"]] = node
        if not conn["pageInfo"]["hasNextPage"]:
            return found
        after = conn["pageInfo"]["endCursor"]


def product_input(product: Product, ids: CatalogIds) -> dict:
    attrs = ids.attribute_ids
    return {
        "name": product.name,
        "slug": product.slug,
        "externalReference": product.sku,
        "productType": ids.product_type_id,
        "category": ids.category_ids[product.category],
        "description": editorjs(product.description),
        "attributes": [
            {"id": attrs["color"], "dropdown": {"value": product.color}},
            {"id": attrs["sizes"], "multiselect": [{"value": s} for s in product.sizes]},
            {"id": attrs["material"], "dropdown": {"value": product.material}},
        ],
        "channelListings": [
            {"channelId": ids.channel_id, "isPublished": True, "visibleInListings": True, "isAvailableForPurchase": True}
        ],
        "variants": [
            {
                "sku": product.sku,
                "name": product.sku,
                "externalReference": product.sku,
                "trackInventory": True,
                "attributes": [],
                "stocks": [{"warehouse": ids.warehouse_id, "quantity": product.stock}],
                "channelListings": [{"channelId": ids.channel_id, "price": product.price}],
            }
        ],
    }


def prepare(client: SeederClient, catalog: Catalog, actions: list[str]) -> CatalogIds:
    channel_id, warehouse_id = lookup_channel_and_warehouse(client)
    attribute_ids = ensure_attributes(client, actions)
    product_type_id = ensure_product_type(client, attribute_ids, actions)
    category_ids = ensure_categories(client, catalog, actions)
    return CatalogIds(channel_id, warehouse_id, product_type_id, attribute_ids, category_ids)


def seed_catalog(client: SeederClient, catalog: Catalog) -> SeedResult:
    result = SeedResult()
    ids = prepare(client, catalog, result.actions)
    existing = existing_products(client, ids.product_type_id)
    missing: list[Product] = []
    for product in catalog.products:
        if product.sku in existing:
            result.skipped.append(product.sku)
        else:
            missing.append(product)

    for start in range(0, len(missing), BATCH_SIZE):
        batch = missing[start : start + BATCH_SIZE]
        payload = client.request(d.PRODUCT_BULK_CREATE, {"products": [product_input(p, ids) for p in batch]})["productBulkCreate"]
        for product, row in zip(batch, payload["results"]):
            if row.get("product") and not row.get("errors"):
                result.created.append(product.sku)
            else:
                result.failed[product.sku] = str(row.get("errors"))
    return result


def products_to_reset(client: SeederClient, product_type_id: str) -> list[tuple[str, str]]:
    """(id, sku) of ShopMate catalog products listed in channel vn. Nothing else is ever selected."""
    pattern = re.compile(SKU_PATTERN)
    selected = []
    for ref, node in sorted(existing_products(client, product_type_id).items()):
        in_vn = any(l["channel"]["slug"] == vn.CHANNEL_SLUG for l in node["channelListings"] or [])
        if in_vn and pattern.match(ref):
            selected.append((node["id"], ref))
    return selected


def reset_vn(client: SeederClient, confirmed: bool) -> int:
    """Delete ShopMate products in channel vn. Orders are fixtures and are never touched."""
    edges = client.request(d.PRODUCT_TYPE_BY_SLUG, {"slug": vn.PRODUCT_TYPE_SLUG})["productTypes"]["edges"]
    if not edges:
        print("nothing to reset: ShopMate product type does not exist")
        return 0
    targets = products_to_reset(client, edges[0]["node"]["id"])
    print(f"--reset-vn will DELETE {len(targets)} ShopMate products in channel {vn.CHANNEL_SLUG} "
          f"(orders are not touched; demo data and default-channel are not touched).")
    if not targets:
        return 0
    if not confirmed:
        if not sys.stdin.isatty():
            print(f"refusing: not interactive. Re-run with --confirm {RESET_CONFIRMATION}", file=sys.stderr)
            return 1
        if input(f"Type {RESET_CONFIRMATION!r} to continue: ").strip() != RESET_CONFIRMATION:
            print("aborted, nothing deleted")
            return 1
    deleted = 0
    for start in range(0, len(targets), 50):
        ids = [i for i, _ in targets[start : start + 50]]
        deleted += client.mutate(d.PRODUCT_BULK_DELETE, {"ids": ids}, "productBulkDelete")["count"]
    print(f"deleted {deleted} products from channel {vn.CHANNEL_SLUG}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset-vn", action="store_true", help="DESTRUCTIVE: delete ShopMate products in channel vn")
    parser.add_argument("--confirm", default="", help=f"non-interactive confirmation for --reset-vn ({RESET_CONFIRMATION})")
    args = parser.parse_args(argv)

    with seeder_from_env() as client:
        try:
            if args.reset_vn:
                return reset_vn(client, confirmed=args.confirm == RESET_CONFIRMATION)
            result = seed_catalog(client, load_catalog())
        except SeedError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    for line in result.actions:
        print(line)
    print(f"products: {len(result.created)} created, {len(result.skipped)} already existed, {len(result.failed)} failed")
    for sku, error in result.failed.items():
        print(f"  FAILED {sku}: {error}", file=sys.stderr)
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
