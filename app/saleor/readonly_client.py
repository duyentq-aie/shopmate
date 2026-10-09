"""Read-only Saleor GraphQL client for the ShopMate runtime.

Why this exists: Saleor has no read-only permission for orders. Reading orders
needs MANAGE_ORDERS, which also allows cancelling, fulfilling and editing them
(verified against PermissionEnum of Saleor 3.23; see docs/SETUP.md). So the
`shopmate-runtime` token CAN write, and read-only must be enforced here, before
a request leaves the process.

Two layers:

1. PRIMARY GUARD: the allowlist. Callers (including any future LLM tool) only
   choose an operation NAME from `ALLOWED_OPERATIONS` and pass variables, which
   are validated by that operation's pydantic model. There is no API that
   accepts GraphQL text from a caller, so a caller cannot write a mutation.
2. SECOND LAYER: every document is parsed with graphql-core when the module is
   imported and again right before sending, and anything that is not exactly
   one `query` operation with the expected name is rejected. This catches a
   mistake in the allowlist itself (e.g. someone adds a mutation to it).

Nothing in this module may be changed to accept caller-supplied GraphQL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import httpx
from graphql import (
    FragmentDefinitionNode,
    GraphQLSyntaxError,
    OperationDefinitionNode,
    OperationType,
    parse,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_TIMEOUT_SECONDS = 15.0


class ReadOnlyViolation(Exception):
    """A request was refused before it left the process."""


class UnknownOperationError(ReadOnlyViolation):
    """The operation name is not in the allowlist."""


class InvalidVariablesError(ReadOnlyViolation):
    """The variables do not match the operation's pydantic model."""


class SaleorQueryError(Exception):
    """Saleor answered with an HTTP error or GraphQL errors."""


# --- variable models -------------------------------------------------------

_SLUG = r"^[a-z0-9-]+$"


class _Vars(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class ChannelBySlugVars(_Vars):
    slug: str = Field(pattern=_SLUG, max_length=100)


class ProductsInChannelVars(_Vars):
    channel: str = Field(pattern=_SLUG, max_length=100)
    first: int = Field(default=100, ge=1, le=100)
    after: str | None = Field(default=None, max_length=200)


class VariantBySkuVars(_Vars):
    sku: str = Field(pattern=r"^[A-Za-z0-9-]+$", max_length=64)
    channel: str = Field(pattern=_SLUG, max_length=100)


class OrderByNumberVars(_Vars):
    number: str = Field(pattern=r"^\d{1,10}$")


class OrderByExternalReferenceVars(_Vars):
    external_reference: str = Field(alias="externalReference", pattern=r"^[A-Za-z0-9_.-]+$", max_length=100)


class OrdersInChannelVars(_Vars):
    channel_id: str = Field(alias="channelId", max_length=200)
    first: int = Field(default=100, ge=1, le=100)
    after: str | None = Field(default=None, max_length=200)


# --- documents -------------------------------------------------------------

_ORDER_FIELDS = """
fragment ShopMateOrder on Order {
  id
  number
  externalReference
  status
  created
  userEmail
  channel { slug }
  shippingMethodName
  subtotal { gross { amount currency } }
  shippingPrice { gross { amount currency } }
  total { gross { amount currency } }
  lines { productSku productName quantity quantityFulfilled variant { sku } }
  fulfillments { status trackingNumber created }
}
"""

_PRODUCT_FIELDS = """
fragment ShopMateProduct on Product {
  id
  name
  slug
  externalReference
  description
  category { slug name }
  attributes { attribute { slug } values { name } }
  isAvailableForPurchase
  variants {
    sku
    quantityAvailable
    pricing { price { gross { amount currency } } }
  }
}
"""


@dataclass(frozen=True)
class Operation:
    name: str
    document: str
    variables: type[_Vars]


def _op(name: str, document: str, variables: type[_Vars]) -> tuple[str, Operation]:
    return name, Operation(name, document, variables)


_ALLOWLIST: dict[str, Operation] = dict(
    [
        _op("ChannelBySlug", """
query ChannelBySlug($slug: String!) {
  channel(slug: $slug) { id slug name currencyCode defaultCountry { code } }
}
""", ChannelBySlugVars),
        _op("ProductsInChannel", """
query ProductsInChannel($channel: String!, $first: Int!, $after: String) {
  products(channel: $channel, first: $first, after: $after, sortBy: {field: NAME, direction: ASC}) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges { node { ...ShopMateProduct } }
  }
}
""" + _PRODUCT_FIELDS, ProductsInChannelVars),
        _op("VariantBySku", """
query VariantBySku($sku: String!, $channel: String!) {
  productVariant(sku: $sku, channel: $channel) {
    sku
    quantityAvailable
    pricing { price { gross { amount currency } } }
    product { ...ShopMateProduct }
  }
}
""" + _PRODUCT_FIELDS, VariantBySkuVars),
        _op("OrderByNumber", """
query OrderByNumber($number: String!) {
  orders(first: 1, filter: {numbers: [$number]}) {
    edges { node { ...ShopMateOrder } }
  }
}
""" + _ORDER_FIELDS, OrderByNumberVars),
        _op("OrderByExternalReference", """
query OrderByExternalReference($externalReference: String!) {
  order(externalReference: $externalReference) { ...ShopMateOrder }
}
""" + _ORDER_FIELDS, OrderByExternalReferenceVars),
        _op("OrdersInChannel", """
query OrdersInChannel($channelId: ID!, $first: Int!, $after: String) {
  orders(first: $first, after: $after, filter: {channels: [$channelId]}) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges { node { ...ShopMateOrder } }
  }
}
""" + _ORDER_FIELDS, OrdersInChannelVars),
    ]
)


def assert_single_query(document: str, expected_name: str) -> None:
    """Second layer: refuse anything but exactly one named `query` operation (plus fragments)."""
    try:
        ast = parse(document)
    except GraphQLSyntaxError as exc:
        raise ReadOnlyViolation(f"{expected_name}: document does not parse: {exc.message}") from exc

    operations = []
    for definition in ast.definitions:
        if isinstance(definition, OperationDefinitionNode):
            operations.append(definition)
        elif not isinstance(definition, FragmentDefinitionNode):
            raise ReadOnlyViolation(f"{expected_name}: unexpected definition {definition.kind}")
    if len(operations) != 1:
        raise ReadOnlyViolation(f"{expected_name}: expected exactly one operation, found {len(operations)}")
    operation = operations[0]
    if operation.operation is not OperationType.QUERY:
        raise ReadOnlyViolation(f"{expected_name}: operation type {operation.operation.value!r} is not allowed")
    if operation.name is None or operation.name.value != expected_name:
        raise ReadOnlyViolation(f"{expected_name}: operation name does not match the allowlist entry")


for _name, _operation in _ALLOWLIST.items():  # fail at import if the allowlist itself is wrong
    if _name != _operation.name:
        raise ReadOnlyViolation(f"allowlist key {_name!r} != operation name {_operation.name!r}")
    assert_single_query(_operation.document, _operation.name)

ALLOWED_OPERATIONS: Mapping[str, Operation] = MappingProxyType(_ALLOWLIST)


class ReadOnlySaleorClient:
    """Sends only allowlisted queries. The only entry point is `execute(name, variables)`."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._http = httpx.Client(
            transport=transport,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        self._url = url

    def __enter__(self) -> "ReadOnlySaleorClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def execute(self, operation_name: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Validate, re-check and send one allowlisted query; return its `data`."""
        operation = _ALLOWLIST.get(operation_name)
        if operation is None:
            raise UnknownOperationError(f"operation {operation_name!r} is not in the read-only allowlist")
        try:
            validated = operation.variables.model_validate(dict(variables or {}))
        except ValidationError as exc:
            raise InvalidVariablesError(f"{operation_name}: {exc}") from exc
        assert_single_query(operation.document, operation.name)

        payload = {
            "query": operation.document,
            "operationName": operation.name,
            "variables": validated.model_dump(mode="json", by_alias=True, exclude_none=True),
        }
        try:
            response = self._http.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise SaleorQueryError(f"{operation_name}: cannot reach Saleor: {exc}") from exc
        if response.status_code != 200:
            raise SaleorQueryError(f"{operation_name}: HTTP {response.status_code}")
        body = response.json()
        if body.get("errors"):
            messages = "; ".join(str(e.get("message")) for e in body["errors"])
            raise SaleorQueryError(f"{operation_name}: {messages}")
        return body.get("data") or {}
