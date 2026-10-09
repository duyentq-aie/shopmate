"""Schema of the golden evaluation set (eval/golden/golden.vi.json).

Each item: id, question, intent, query style, expected filters (product_search only),
the gold answer source, split (dev/test) and review status.

Gold answer source types:
- skus:          the exact SKUs a correct product answer is about. When `expected_filters`
                 is set, the list MUST equal `apply_filters(catalog, filters)`; the
                 validator recomputes it, so labels cannot drift from the catalog.
- policy_clause: a document in data/policies/ and a clause number (Điều N).
- order:         a fixture order (external_reference) and its expected status. Its
                 `order_number` stays "pending" until scripts/seed_orders.py has run.
                 Questions refer to it with the literal placeholder `{order_number}`,
                 which the eval harness substitutes.
                 Anti-IDOR rule (docs/DECISIONS-phase2.md, D28): an order is revealed only
                 when the question gives the order number AND the customer's matching
                 email. `provided_identity` says what the question gives, and
                 `expected_behavior` must follow from it (IDENTITY_BEHAVIOR).
- no_answer:     there is no grounded answer (out of scope, smalltalk, nothing matches).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.catalog.schema import Catalog
from app.config import REPO_ROOT

GOLDEN_PATH = REPO_ROOT / "eval" / "golden" / "golden.vi.json"
ORDER_PLACEHOLDER = "{order_number}"

Intent = Literal["product_search", "policy_question", "order_status", "create_ticket", "out_of_scope", "smalltalk"]
QueryStyle = Literal["co_dau", "khong_dau", "teencode"]  # có dấu / không dấu / viết tắt-teencode
Split = Literal["dev", "test"]
ReviewStatus = Literal["draft_needs_human_review", "human_reviewed"]
Author = Literal["agent_draft", "human"]
PolicyDocument = Literal["doi-tra", "van-chuyen", "bao-hanh"]
OrderStatus = Literal["UNCONFIRMED", "UNFULFILLED", "PARTIALLY_FULFILLED", "FULFILLED", "CANCELED"]
# v1 verification is EMAIL ONLY (D37). Orders carry no phone numbers and are append-only, so a
# phone could never match; a phone-only question is treated like "no identity": ask for the email.
# Phones are still recognised in questions (validate_golden.PHONE), but no item expects one to match.
ProvidedIdentity = Literal["none", "matching_email", "mismatched_email", "phone_only"]
ExpectedBehavior = Literal["answer", "ask_for_verification", "refuse"]
IDENTITY_BEHAVIOR: dict[str, str] = {
    "matching_email": "answer",  # number + the owner's email: the only case that reveals anything
    "none": "ask_for_verification",  # number only: ask for the email, reveal nothing yet
    "phone_only": "ask_for_verification",  # number + a phone: v1 cannot verify phones, ask for the email
    "mismatched_email": "refuse",  # number + someone else's email: refuse, reveal nothing
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductFilters(_Strict):
    """Structured filters a correct product search should apply. All set fields must hold (AND)."""

    category: str | None = None  # category slug
    kind: str | None = None  # e.g. "Áo sơ mi nam"
    keyword: str | None = None  # case-insensitive substring of the product name
    color: str | None = None  # màu sắc, exact catalog value
    material: str | None = None  # chất liệu, exact catalog value
    size: str | None = None  # kích cỡ: available sizes include this one
    price_min: int | None = Field(default=None, ge=0)
    price_max: int | None = Field(default=None, ge=0)
    in_stock: bool | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> "ProductFilters":
        if not self.model_dump(exclude_none=True):
            raise ValueError("expected_filters must set at least one filter")
        return self


class SkusSource(_Strict):
    type: Literal["skus"] = "skus"
    skus: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _sorted_unique(self) -> "SkusSource":
        if self.skus != sorted(set(self.skus)):
            raise ValueError("skus must be sorted and unique")
        return self


class PolicyClauseSource(_Strict):
    type: Literal["policy_clause"] = "policy_clause"
    document: PolicyDocument
    clause: int = Field(ge=1)


class OrderSource(_Strict):
    type: Literal["order"] = "order"
    external_reference: str = Field(pattern=r"^shopmate-seed-order-\d{3}$")
    order_number: str = Field(pattern=r"^(pending|\d+)$")
    expected_status: OrderStatus  # what a verified answer says; must NOT be revealed otherwise
    provided_identity: ProvidedIdentity
    expected_behavior: ExpectedBehavior

    @model_validator(mode="after")
    def _verify_or_refuse(self) -> "OrderSource":
        wanted = IDENTITY_BEHAVIOR[self.provided_identity]
        if self.expected_behavior != wanted:
            raise ValueError(f"provided_identity {self.provided_identity} requires expected_behavior {wanted}")
        return self


class NoAnswerSource(_Strict):
    type: Literal["no_answer"] = "no_answer"
    reason: str = Field(min_length=3)


GoldSource = Annotated[SkusSource | PolicyClauseSource | OrderSource | NoAnswerSource, Field(discriminator="type")]

ALLOWED_SOURCES: dict[str, set[str]] = {
    "product_search": {"skus", "no_answer"},
    "policy_question": {"policy_clause"},
    "order_status": {"order"},
    "create_ticket": {"order", "no_answer"},
    "out_of_scope": {"no_answer"},
    "smalltalk": {"no_answer"},
}


class GoldenItem(_Strict):
    id: str = Field(pattern=r"^gs-\d{3}$")
    question: str = Field(min_length=2)
    intent: Intent
    query_style: QueryStyle
    expected_filters: ProductFilters | None = None
    gold_source: GoldSource
    split: Split
    status: ReviewStatus
    author: Author
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "GoldenItem":
        if self.gold_source.type not in ALLOWED_SOURCES[self.intent]:
            raise ValueError(f"{self.id}: intent {self.intent} cannot have gold source {self.gold_source.type}")
        if self.expected_filters is not None and self.intent != "product_search":
            raise ValueError(f"{self.id}: expected_filters only apply to product_search")
        if self.intent == "product_search" and self.gold_source.type == "no_answer" and self.expected_filters is None:
            raise ValueError(f"{self.id}: a no-match product_search needs expected_filters to prove it matches nothing")
        has_placeholder = ORDER_PLACEHOLDER in self.question
        if has_placeholder != (self.gold_source.type == "order"):
            raise ValueError(f"{self.id}: {ORDER_PLACEHOLDER} must appear exactly when the gold source is an order")
        return self


class GoldenSet(_Strict):
    schema_version: Literal[1] = 1
    description: str
    items: list[GoldenItem]

    @model_validator(mode="after")
    def _unique_sorted(self) -> "GoldenSet":
        ids = [i.id for i in self.items]
        if ids != sorted(set(ids)):
            raise ValueError("item ids must be unique and sorted")
        return self


def apply_filters(catalog: Catalog, filters: ProductFilters) -> list[str]:
    """The SKUs matching every set filter, sorted. The single definition of 'gold' for filtered searches."""
    keyword = filters.keyword.casefold() if filters.keyword else None
    matches = []
    for p in catalog.products:
        if filters.category is not None and p.category != filters.category:
            continue
        if filters.kind is not None and p.kind != filters.kind:
            continue
        if keyword is not None and keyword not in p.name.casefold():
            continue
        if filters.color is not None and p.color != filters.color:
            continue
        if filters.material is not None and p.material != filters.material:
            continue
        if filters.size is not None and filters.size not in p.sizes:
            continue
        if filters.price_min is not None and p.price < filters.price_min:
            continue
        if filters.price_max is not None and p.price > filters.price_max:
            continue
        if filters.in_stock is not None and p.in_stock != filters.in_stock:
            continue
        matches.append(p.sku)
    return sorted(matches)


def test_split_hash(golden: GoldenSet) -> str:
    """SHA-256 of the canonical JSON of the test-split items, sorted by id."""
    items = [i.model_dump(mode="json") for i in sorted(golden.items, key=lambda i: i.id) if i.split == "test"]
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


test_split_hash.__test__ = False  # not a pytest test despite the name


def load_golden(path: Path = GOLDEN_PATH) -> GoldenSet:
    return GoldenSet.model_validate_json(path.read_text(encoding="utf-8"))
