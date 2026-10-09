"""Report counts for channel `vn` and detect drift from the seed files.

All counts are scoped to channel `vn` (the demo data in default-channel makes
global counts meaningless). Drift checks:
- catalog (data/seed/catalog.vi.json) vs Saleor: missing / extra products, and per
  SKU the name, category, price (VND), stock, màu sắc, chất liệu, kích cỡ, published;
- orders (data/seed/orders.vi.json) vs Saleor, matched by externalReference (never by
  count or number): missing orders, status, recorded number, line SKUs and quantities.
  An order line whose product was deleted is reported as a warning, not a failure.

Reads only. Catalog data is read with SALEOR_SEEDER_TOKEN (exact stock needs
MANAGE_PRODUCTS; the runtime token cannot see it). Orders are read with the
read-only runtime client (SALEOR_RUNTIME_TOKEN).

Exit codes: 0 = no drift, 1 = drift found, 2 = configuration error.

Usage (from the repo root):
    uv run python -m scripts.report_vn
    uv run python -m scripts.report_vn --catalog-only
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field

from app.catalog.schema import ATTRIBUTES, Catalog, load_catalog
from app.config import ConfigError, api_url, require_setting
from app.orders.schema import PENDING, OrderFixtures, load_orders
from app.saleor.readonly_client import ReadOnlySaleorClient, ReadOnlyViolation, SaleorQueryError
from scripts.seedlib import documents as d
from scripts.seedlib import vn
from scripts.seedlib.client import SeederClient, SeedError, seeder_from_env

ATTR_SLUG = {catalog_field: slug for catalog_field, (slug, _, _) in ATTRIBUTES.items()}


@dataclass
class Findings:
    mismatches: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def fetch_products(client: SeederClient) -> tuple[int, list[dict]]:
    nodes: list[dict] = []
    after = None
    total = 0
    while True:
        conn = client.request(d.PRODUCTS_IN_CHANNEL_FULL, {"channel": vn.CHANNEL_SLUG, "first": 100, "after": after})["products"]
        total = conn["totalCount"]
        nodes.extend(e["node"] for e in conn["edges"])
        if not conn["pageInfo"]["hasNextPage"]:
            return total, nodes
        after = conn["pageInfo"]["endCursor"]


def _values(node: dict, slug: str) -> list[str]:
    for selected in node["attributes"]:
        if selected["attribute"]["slug"] == slug:
            return [v["name"] for v in selected["values"]]
    return []


def diff_catalog(catalog: Catalog, nodes: list[dict]) -> Findings:
    found = Findings()
    expected = catalog.by_sku()
    seen: set[str] = set()
    for node in nodes:
        ref = node.get("externalReference")
        if ref not in expected:
            found.mismatches.append(f"extra product in {vn.CHANNEL_SLUG}: {node['name']!r} (externalReference {ref!r})")
            continue
        seen.add(ref)
        p = expected[ref]
        diffs = []
        variants = node["variants"] or []
        if len(variants) != 1 or variants[0]["sku"] != p.sku:
            diffs.append(f"variants {[v['sku'] for v in variants]} != [{p.sku}]")
        if node["name"] != p.name:
            diffs.append(f"name {node['name']!r} != {p.name!r}")
        if (node.get("category") or {}).get("slug") != p.category:
            diffs.append(f"category {(node.get('category') or {}).get('slug')!r} != {p.category!r}")
        listing = next((l for l in node["channelListings"] or [] if l["channel"]["slug"] == vn.CHANNEL_SLUG), None)
        if not listing or not listing["isPublished"]:
            diffs.append("not published in vn")
        if variants:
            v = variants[0]
            price = next((l["price"] for l in v["channelListings"] or [] if l["channel"]["slug"] == vn.CHANNEL_SLUG), None)
            if price is None or price["currency"] != "VND" or float(price["amount"]) != p.price:
                diffs.append(f"price {price} != {p.price} VND")
            stock = sum(s["quantity"] for s in v["stocks"] or [] if s["warehouse"]["slug"] == vn.WAREHOUSE_SLUG)
            if stock != p.stock:
                diffs.append(f"stock {stock} != {p.stock}")
        if _values(node, ATTR_SLUG["color"]) != [p.color]:
            diffs.append(f"màu sắc {_values(node, ATTR_SLUG['color'])} != [{p.color!r}]")
        if _values(node, ATTR_SLUG["material"]) != [p.material]:
            diffs.append(f"chất liệu {_values(node, ATTR_SLUG['material'])} != [{p.material!r}]")
        if set(_values(node, ATTR_SLUG["sizes"])) != set(p.sizes):
            diffs.append(f"kích cỡ {sorted(_values(node, ATTR_SLUG['sizes']))} != {p.sizes}")
        if diffs:
            found.mismatches.append(f"{p.sku}: " + "; ".join(diffs))
    for sku in sorted(set(expected) - seen):
        found.mismatches.append(f"missing product in {vn.CHANNEL_SLUG}: {sku}")
    return found


def fetch_orders(runtime: ReadOnlySaleorClient) -> tuple[int, list[dict]]:
    channel = runtime.execute("ChannelBySlug", {"slug": vn.CHANNEL_SLUG})["channel"]
    if channel is None:
        raise SeedError(f"channel {vn.CHANNEL_SLUG} does not exist")
    nodes: list[dict] = []
    after = None
    while True:
        conn = runtime.execute("OrdersInChannel", {"channelId": channel["id"], "first": 100, "after": after})["orders"]
        nodes.extend(e["node"] for e in conn["edges"])
        if not conn["pageInfo"]["hasNextPage"]:
            return conn["totalCount"], nodes
        after = conn["pageInfo"]["endCursor"]


def diff_orders(fixtures: OrderFixtures, nodes: list[dict]) -> Findings:
    found = Findings()
    by_ref = {n["externalReference"]: n for n in nodes if n.get("externalReference")}
    for fixture in fixtures.orders:
        ref = fixture.external_reference
        node = by_ref.get(ref)
        if node is None:
            found.mismatches.append(f"missing order in {vn.CHANNEL_SLUG}: {ref}")
            continue
        diffs = []
        if node["status"] != fixture.status:
            diffs.append(f"status {node['status']} != {fixture.status}")
        if fixture.number != PENDING and str(node["number"]) != fixture.number:
            diffs.append(f"number #{node['number']} != recorded #{fixture.number}")
        actual = Counter((line["productSku"], line["quantity"]) for line in node["lines"])
        wanted = Counter((line.sku, line.quantity) for line in fixture.lines)
        if actual != wanted:
            diffs.append(f"lines {sorted(actual.items())} != {sorted(wanted.items())}")
        for line in node["lines"]:
            if line.get("variant") is None:
                found.warnings.append(f"{ref} (#{node['number']}): product {line['productSku']} no longer exists in Saleor")
        if diffs:
            found.mismatches.append(f"{ref} (#{node['number']}): " + "; ".join(diffs))
    known = {o.external_reference for o in fixtures.orders}
    for node in nodes:
        if node.get("externalReference") not in known:
            found.warnings.append(f"order #{node['number']} in {vn.CHANNEL_SLUG} is not a ShopMate fixture")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog-only", action="store_true", help="skip the order checks (no runtime token needed)")
    args = parser.parse_args(argv)

    catalog = load_catalog()
    findings = Findings()
    try:
        with seeder_from_env() as client:
            product_count, products = fetch_products(client)
        catalog_findings = diff_catalog(catalog, products)
        print(f"products in channel {vn.CHANNEL_SLUG}: {product_count} (catalog file: {len(catalog.products)})")
        findings.mismatches += catalog_findings.mismatches

        if not args.catalog_only:
            try:
                token = require_setting("SALEOR_RUNTIME_TOKEN")
            except ConfigError as exc:
                print(f"CONFIG ERROR: {exc} (or use --catalog-only)", file=sys.stderr)
                return 2
            fixtures = load_orders()
            with ReadOnlySaleorClient(api_url(), token) as runtime:
                order_count, orders = fetch_orders(runtime)
            order_findings = diff_orders(fixtures, orders)
            print(f"orders in channel {vn.CHANNEL_SLUG}: {order_count} (fixture file: {len(fixtures.orders)})")
            findings.mismatches += order_findings.mismatches
            findings.warnings += order_findings.warnings
    except (SeedError, SaleorQueryError, ReadOnlyViolation) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for warning in findings.warnings:
        print(f"WARNING: {warning}")
    for mismatch in findings.mismatches:
        print(f"DRIFT: {mismatch}")
    if findings.mismatches:
        print(f"FAIL: {len(findings.mismatches)} differences between Saleor channel {vn.CHANNEL_SLUG} and the seed files")
        return 1
    print(f"OK: Saleor channel {vn.CHANNEL_SLUG} matches the seed files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
