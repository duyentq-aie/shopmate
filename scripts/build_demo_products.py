"""Build widget/demo-products.json: the products on the demo shop page (widget/demo.html), taken from the real seed catalog, with their photos.

A static list, made once and committed, so the page only reads a file: no server code builds it, nothing is invented. Every entry is a product of
data/seed/catalog.vi.json, in stock, with its own name, kind, colour, material and price. The list is chosen, not sampled, by three rules:

1. the product has a licensed photo of the right kind and colour (widget/img/products/<SKU>.webp, credits in widget/img/products/CREDITS.md); a product
   without a good photo is left out, not shown with a wrong one;
2. it is not one of the products that scripts/audit_catalog_consistency.py flags (a name that contradicts the material): the demo shows no product whose
   own name is wrong, so the two jackets of the old list are gone;
3. the suggestion chip of the widget finds only products of this list (tests/test_widget.py): "Giày tây nam màu nâu dưới 1 triệu" finds the two brown
   dress shoes.

The photos are put in the file as `data:` URIs. That is deliberate: the chat API serves a fixed list of widget files (app/chat_api.py) and no folder, so a file
under widget/img/ has no URL of its own; the one JSON file it does serve carries the pictures, and the demo page's CSP allows `data:` images. The images stay
as ordinary files in widget/img/ (the source of truth, each at most 120 KB); this script refuses a bigger one.

Deterministic: no clock, no randomness, no network.

Usage (from the repo root):
    uv run python -m scripts.build_demo_products            # write the file
    uv run python -m scripts.build_demo_products --check    # exit 1 if the file is stale
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from app.config import REPO_ROOT
from scripts.audit_catalog_consistency import audit

CATALOG_PATH = REPO_ROOT / "data" / "seed" / "catalog.vi.json"
OUT_PATH = REPO_ROOT / "widget" / "demo-products.json"
IMAGE_DIR = REPO_ROOT / "widget" / "img" / "products"
HERO_PATH = REPO_ROOT / "widget" / "img" / "hero.webp"
MAX_IMAGE_BYTES = 120 * 1024
HERO = {"alt": "Váy, móc áo gỗ và kính mát trên nền sáng", "credit": "Ảnh: Atlantic Ambience, Pexels"}

# sku -> why it is on the page
CHOSEN = {
    "SM-NAM-0004": "jeans, black, ripped knee: the one pair of black jeans of the photo",
    "SM-NAM-0002": "shirt, black, long sleeve",
    "SM-NU-0029": "T-shirt, white",
    "SM-NU-0018": "A-line skirt, white",
    "SM-GIAY-0032": "low-top sneaker, white",
    "SM-GIAY-0009": "dress shoe (derby), brown: found by the suggestion chip",
    "SM-GIAY-0015": "dress shoe (loafer), brown: found by the suggestion chip",
    "SM-TUI-0026": "handbag (tote), brown",
    "SM-TUI-0032": "cross-body bag (box), brown",
    "SM-TUI-0019": "leather wallet, brown",
    "SM-PK-0001": "belt, black",
}


def data_uri(path: Path) -> str:
    return "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build(catalog_path: Path = CATALOG_PATH, image_dir: Path = IMAGE_DIR, hero_path: Path = HERO_PATH) -> dict:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    by_sku = {p["sku"]: p for p in catalog["products"]}
    flagged = set(audit(catalog["products"])["flagged"])
    products = []
    for sku in CHOSEN:
        if sku not in by_sku:
            raise SystemExit(f"{sku} is not in the catalog")
        p = by_sku[sku]
        if int(p["stock"]) <= 0:
            raise SystemExit(f"{sku} is out of stock: the demo page only shows products that can be bought")
        if sku in flagged:
            raise SystemExit(f"{sku} is flagged by scripts/audit_catalog_consistency.py: its name contradicts its material or colour")
        image = image_dir / f"{sku}.webp"
        if not image.is_file():
            raise SystemExit(f"{sku} has no photo ({image.relative_to(REPO_ROOT) if image.is_relative_to(REPO_ROOT) else image}): a product without one is left out")
        if image.stat().st_size > MAX_IMAGE_BYTES:
            raise SystemExit(f"{sku}: the photo is {image.stat().st_size} bytes, more than {MAX_IMAGE_BYTES}")
        products.append({"sku": p["sku"], "slug": p["slug"], "name": p["name"], "kind": p["kind"], "category": p["category"], "color": p["color"],
                         "material": p["material"], "sizes": list(p["sizes"]), "price_vnd": int(p["price"]), "in_stock": True, "image": data_uri(image)})
    if hero_path.stat().st_size > MAX_IMAGE_BYTES:
        raise SystemExit(f"the hero picture is {hero_path.stat().st_size} bytes, more than {MAX_IMAGE_BYTES}")
    return {"synthetic": True, "source": "data/seed/catalog.vi.json", "built_by": "scripts/build_demo_products.py",
            "images": "widget/img/products/CREDITS.md", "hero": {**HERO, "image": data_uri(hero_path)}, "products": products}


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
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} ({len(CHOSEN)} products, {len(text) // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
