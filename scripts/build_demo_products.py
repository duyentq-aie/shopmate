"""Build widget/demo-products.json: the products on the demo shop page (widget/demo.html), taken from the real seed catalog.

A static list, made once and committed, so the page only reads a file: no server code builds it, nothing is invented. Every entry is a product of
data/seed/catalog.vi.json, in stock, with its own name, kind, colour, material and price. The list is chosen, not sampled: it has the two jackets of the Phase 7.5
smoke test (the ones the jacket offer leads to), a shirt that appeared in a real answer, and products that answer questions of the dev split, so that what the
assistant says about a typed question can be found on the page. It covers all eight silhouettes of the fabric-swatch thumbnail (widget/shopmate-chat.js).

Deterministic: no clock, no randomness, no network.

Usage (from the repo root):
    uv run python -m scripts.build_demo_products            # write the file
    uv run python -m scripts.build_demo_products --check    # exit 1 if the file is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.config import REPO_ROOT

CATALOG_PATH = REPO_ROOT / "data" / "seed" / "catalog.vi.json"
OUT_PATH = REPO_ROOT / "widget" / "demo-products.json"

# sku -> why it is on the page (the dev items are listed by id; the questions are in the golden set, read through load_split("dev") only)
CHOSEN = {
    "SM-NAM-0014": "jacket: one of the two products the jacket offer leads to (docs/DECISIONS-phase7.md, D7.30)",
    "SM-NAM-0042": "jacket: the other one",
    "SM-NAM-0002": "shirt: named in a real answer of the Phase 7.5 smoke test",
    "SM-NAM-0018": "jeans, denim: answers dev item gs-104",
    "SM-NU-0041": "jacket in ripstop (Dù)",
    "SM-NU-0003": "dress in linen: answers dev item gs-004 (a dress under 400k)",
    "SM-NU-0001": "T-shirt in plain cotton",
    "SM-GIAY-0002": "sneaker: answers dev items gs-002 and gs-106",
    "SM-GIAY-0009": "dress shoe: answers dev item gs-016",
    "SM-TUI-0027": "cross-body bag: answers dev item gs-018 (under 400k)",
    "SM-TUI-0009": "leather wallet: answers dev items gs-009 and gs-107",
    "SM-PK-0013": "sunglasses: answers dev item gs-013",
}


def build(catalog_path: Path = CATALOG_PATH) -> dict:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    by_sku = {p["sku"]: p for p in catalog["products"]}
    products = []
    for sku in CHOSEN:
        if sku not in by_sku:
            raise SystemExit(f"{sku} is not in the catalog")
        p = by_sku[sku]
        if int(p["stock"]) <= 0:
            raise SystemExit(f"{sku} is out of stock: the demo page only shows products that can be bought")
        products.append({"sku": p["sku"], "slug": p["slug"], "name": p["name"], "kind": p["kind"], "category": p["category"], "color": p["color"],
                         "material": p["material"], "sizes": list(p["sizes"]), "price_vnd": int(p["price"]), "in_stock": True})
    return {"synthetic": True, "source": "data/seed/catalog.vi.json", "built_by": "scripts/build_demo_products.py", "products": products}


def render(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if widget/demo-products.json is not what this script builds")
    args = parser.parse_args(argv)
    text = render(build())
    if args.check:
        current = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.is_file() else ""
        if current != text:
            print(f"{OUT_PATH.relative_to(REPO_ROOT)} is stale: run `uv run python -m scripts.build_demo_products`", file=sys.stderr)
            return 1
        print(f"{OUT_PATH.relative_to(REPO_ROOT)} is current ({len(CHOSEN)} products)")
        return 0
    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} ({len(CHOSEN)} products)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
