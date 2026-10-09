"""Pydantic schema for the synthetic catalog in data/seed/catalog.vi.json.

Design (agreed in Phase 2): one product = one variant = one SKU.
- `color` (màu sắc) and `material` (chất liệu) are single-value dropdowns.
- `sizes` (kích cỡ) is a multiselect of AVAILABLE sizes. A size filter means
  "products whose available sizes include X". There is no per-size stock: `stock`
  is one number for the whole SKU. This is a deliberate simplification.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import REPO_ROOT

CATALOG_PATH = REPO_ROOT / "data" / "seed" / "catalog.vi.json"

# Canonical size order. `sizes` lists must follow it, so filters and diffs are stable.
SIZE_ORDER: tuple[str, ...] = (
    "S", "M", "L", "XL", "XXL",
    "28", "29", "30", "31", "32", "33", "34",
    "35", "36", "37", "38", "39", "40", "41", "42", "43", "44",
    "Freesize",
)

# Saleor attribute slugs and Vietnamese display names.
ATTRIBUTES = {
    "color": ("mau-sac", "Màu sắc", "DROPDOWN"),
    "sizes": ("kich-co", "Kích cỡ", "MULTISELECT"),
    "material": ("chat-lieu", "Chất liệu", "DROPDOWN"),
}

SKU_PATTERN = r"^SM-[A-Z]{2,4}-\d{4}$"


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Category(_Strict):
    slug: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    sku_prefix: str = Field(pattern=r"^[A-Z]{2,4}$")


class Product(_Strict):
    sku: str = Field(pattern=SKU_PATTERN)
    slug: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=3)
    category: str
    kind: str = Field(description="Product kind, e.g. 'Áo sơ mi nam'. Also part of the name.")
    description: str = Field(min_length=20)
    color: str
    material: str
    sizes: list[str] = Field(min_length=1)
    price: int = Field(gt=0, description="VND, rounded to 1,000")
    stock: int = Field(ge=0)

    @field_validator("name", "kind", "description", "color", "material")
    @classmethod
    def _must_be_nfc(cls, value: str) -> str:
        if value != nfc(value):
            raise ValueError("text must be Unicode NFC")
        return value

    @field_validator("price")
    @classmethod
    def _rounded_price(cls, value: int) -> int:
        if value % 1000:
            raise ValueError("price must be a multiple of 1,000 VND")
        return value

    @field_validator("sizes")
    @classmethod
    def _canonical_sizes(cls, value: list[str]) -> list[str]:
        unknown = [s for s in value if s not in SIZE_ORDER]
        if unknown:
            raise ValueError(f"unknown sizes {unknown}")
        if value != sorted(set(value), key=SIZE_ORDER.index):
            raise ValueError("sizes must be unique and in canonical order")
        return value

    @property
    def in_stock(self) -> bool:
        return self.stock > 0


class Catalog(_Strict):
    schema_version: Literal[1] = 1
    synthetic: Literal[True] = True
    seed: int
    channel: Literal["vn"] = "vn"
    currency: Literal["VND"] = "VND"
    categories: list[Category]
    products: list[Product]

    @model_validator(mode="after")
    def _consistent(self) -> "Catalog":
        cats = {c.slug: c for c in self.categories}
        if len(cats) != len(self.categories):
            raise ValueError("duplicate category slug")
        for field in ("sku", "slug", "name"):
            values = [getattr(p, field) for p in self.products]
            dupes = {v for v in values if values.count(v) > 1}
            if dupes:
                raise ValueError(f"duplicate product {field}: {sorted(dupes)[:5]}")
        for p in self.products:
            cat = cats.get(p.category)
            if cat is None:
                raise ValueError(f"{p.sku}: unknown category {p.category}")
            if not p.sku.startswith(f"SM-{cat.sku_prefix}-"):
                raise ValueError(f"{p.sku}: SKU prefix does not match category {p.category}")
        if [p.sku for p in self.products] != sorted(p.sku for p in self.products):
            raise ValueError("products must be sorted by SKU")
        return self

    def by_sku(self) -> dict[str, Product]:
        return {p.sku: p for p in self.products}


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    return Catalog.model_validate_json(path.read_text(encoding="utf-8"))
