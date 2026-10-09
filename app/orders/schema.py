"""Pydantic schema for the synthetic test orders in data/seed/orders.vi.json.

Orders are append-only fixtures: they are created once in Saleor channel `vn`
(scripts/seed_orders.py, keyed by `external_reference`) and never deleted.
`number` is the Saleor order number, "pending" until seed_orders.py has read it back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import REPO_ROOT

ORDERS_PATH = REPO_ROOT / "data" / "seed" / "orders.vi.json"
PENDING = "pending"
EXTERNAL_REFERENCE_PATTERN = r"^shopmate-seed-order-\d{3}$"

OrderStatus = Literal["UNCONFIRMED", "UNFULFILLED", "PARTIALLY_FULFILLED", "FULFILLED", "CANCELED"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Customer(_Strict):
    first_name: str
    last_name: str
    email: str = Field(pattern=r"^[a-z0-9.]+@example\.com$")


class Address(_Strict):
    street: str
    city_area: str
    city: str
    country_area: str  # must be a Saleor/i18naddress VN province key, e.g. "Hà Nội"
    country: Literal["VN"] = "VN"


class OrderLine(_Strict):
    sku: str
    product_name: str
    quantity: int = Field(ge=1)
    unit_price: int = Field(gt=0)

    @property
    def total(self) -> int:
        return self.quantity * self.unit_price


class FulfillmentLine(_Strict):
    line_index: int = Field(ge=0)
    quantity: int = Field(ge=1)


class Fulfillment(_Strict):
    tracking_code: str
    lines: list[FulfillmentLine] = Field(min_length=1)


class OrderFixture(_Strict):
    external_reference: str = Field(pattern=EXTERNAL_REFERENCE_PATTERN)
    number: str = Field(pattern=r"^(pending|\d+)$")
    status: OrderStatus
    created_at: str  # ISO 8601 with offset; must be in the past when seeding
    customer: Customer
    shipping_address: Address
    lines: list[OrderLine] = Field(min_length=1)
    fulfillments: list[Fulfillment] = []
    shipping_method: str
    shipping_price: int = Field(ge=0)
    subtotal: int
    total: int

    @model_validator(mode="after")
    def _consistent(self) -> "OrderFixture":
        if self.subtotal != sum(line.total for line in self.lines):
            raise ValueError(f"{self.external_reference}: subtotal mismatch")
        if self.total != self.subtotal + self.shipping_price:
            raise ValueError(f"{self.external_reference}: total mismatch")
        fulfilled = [0] * len(self.lines)
        for fulfillment in self.fulfillments:
            for fl in fulfillment.lines:
                if fl.line_index >= len(self.lines):
                    raise ValueError(f"{self.external_reference}: bad line_index {fl.line_index}")
                fulfilled[fl.line_index] += fl.quantity
        if any(f > line.quantity for f, line in zip(fulfilled, self.lines)):
            raise ValueError(f"{self.external_reference}: fulfilled more than ordered")
        ordered, shipped = sum(line.quantity for line in self.lines), sum(fulfilled)
        # Mirrors Saleor's orderBulkCreate status validation; CANCELED and UNCONFIRMED
        # are kept without fulfillments (the conservative case).
        expected = {
            "FULFILLED": shipped == ordered,
            "PARTIALLY_FULFILLED": 0 < shipped < ordered,
            "UNFULFILLED": shipped == 0,
            "UNCONFIRMED": shipped == 0,
            "CANCELED": shipped == 0,
        }[self.status]
        if not expected:
            raise ValueError(f"{self.external_reference}: status {self.status} does not match fulfillments")
        return self


class OrderFixtures(_Strict):
    schema_version: Literal[1] = 1
    synthetic: Literal[True] = True
    seed: int
    channel: Literal["vn"] = "vn"
    currency: Literal["VND"] = "VND"
    orders: list[OrderFixture]

    @model_validator(mode="after")
    def _unique(self) -> "OrderFixtures":
        refs = [o.external_reference for o in self.orders]
        if len(set(refs)) != len(refs) or refs != sorted(refs):
            raise ValueError("external_reference values must be unique and sorted")
        numbers = [o.number for o in self.orders if o.number != PENDING]
        if len(set(numbers)) != len(numbers):
            raise ValueError("duplicate order numbers")
        return self

    def by_reference(self) -> dict[str, OrderFixture]:
        return {o.external_reference: o for o in self.orders}


def load_orders(path: Path = ORDERS_PATH) -> OrderFixtures:
    return OrderFixtures.model_validate_json(path.read_text(encoding="utf-8"))
