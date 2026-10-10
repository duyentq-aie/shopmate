"""Fakes for the agent's collaborators: a scripted router and extractor, a small catalogue, orders and tickets in memory,
and an answerer. Every fake records its calls, so a test can prove what was NOT called as well as what was."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent.protocols import Answer, OrderLookup
from app.retrieval.search import SearchFilters

PRODUCTS = [
    {"key": "product:SM-NAM-0001", "sku": "SM-NAM-0001", "name": "Áo khoác dù nam xanh rêu", "kind": "Áo khoác nam", "color": "Xanh rêu",
     "material": "Dù", "sizes": ["M", "L"], "price_vnd": 459000, "in_stock": True},
    {"key": "product:SM-NAM-0002", "sku": "SM-NAM-0002", "name": "Áo khoác dù nam đen", "kind": "Áo khoác nam", "color": "Đen",
     "material": "Dù", "sizes": ["S", "M", "XL"], "price_vnd": 429000, "in_stock": True},
    {"key": "product:SM-NU-0003", "sku": "SM-NU-0003", "name": "Áo len nữ đỏ", "kind": "Áo len nữ", "color": "Đỏ",
     "material": "Len", "sizes": ["S", "M"], "price_vnd": 349000, "in_stock": True},
    {"key": "product:SM-NU-0004", "sku": "SM-NU-0004", "name": "Áo len nữ kem", "kind": "Áo len nữ", "color": "Kem",
     "material": "Len", "sizes": ["M", "L"], "price_vnd": 369000, "in_stock": False},
    {"key": "product:SM-PK-0005", "sku": "SM-PK-0005", "name": "Mũ lưỡi trai vàng", "kind": "Mũ", "color": "Vàng",
     "material": "Vải canvas", "sizes": ["Freesize"], "price_vnd": 129000, "in_stock": True},
]
POLICIES = [{"key": "policy:doi-tra:2", "file": "doi-tra", "clause": 2, "title": "Thời hạn đổi trả", "text": "Đổi trong vòng 7 ngày."}]
ORDER_88 = {"number": "88", "status": "UNFULFILLED", "created": "2026-09-01T10:00:00+07:00",
            "lines": [{"name": "Áo khoác dù nam đen", "sku": "SM-NAM-0002", "quantity": 1, "quantity_fulfilled": 0}], "fulfillments": []}
ORDER_91 = {**ORDER_88, "number": "91", "status": "FULFILLED"}
ORDERS = {"88": {"email": "khachhang10@example.com", "order": ORDER_88}, "91": {"email": "khachhang02@example.com", "order": ORDER_91}}


class FakeRouter:
    """route(text) -> intent from the table. An unexpected text fails loudly, so a router call that should not happen is a failure."""

    def __init__(self, routes: dict[str, str | None]):
        self.routes, self.calls = routes, []

    def route(self, question: str):
        self.calls.append(question)
        if question not in self.routes:
            raise AssertionError(f"the router was called with a text the test did not expect: {question!r}")
        intent = self.routes[question]
        return SimpleNamespace(intent=intent, error=None if intent else "no usable intent")


class FakeExtractor:
    def __init__(self, filters: dict[str, dict]):
        self.filters, self.calls = filters, []

    def extract(self, question: str):
        self.calls.append(question)
        if question not in self.filters:
            raise AssertionError(f"the extractor was called with a text the test did not expect: {question!r}")
        return SimpleNamespace(filters=SearchFilters(**self.filters[question]), error=None, dropped=[])


class FakeTools:
    def __init__(self, products=PRODUCTS, policies=POLICIES, orders=ORDERS, fail_ticket: Exception | None = None):
        self.products, self.policies, self.orders, self.fail_ticket = list(products), list(policies), orders, fail_ticket
        self.product_searches, self.policy_searches, self.lookups, self.tickets = [], [], [], []

    @staticmethod
    def _matches(product: dict, filters: dict) -> bool:
        return all([
            filters.get("kind") in (None, product["kind"]), filters.get("color") in (None, product["color"]),
            filters.get("material") in (None, product["material"]), filters.get("size") in (None, *product["sizes"]),
            filters.get("price_min") is None or product["price_vnd"] >= filters["price_min"],
            filters.get("price_max") is None or product["price_vnd"] <= filters["price_max"],
            filters.get("in_stock") is None or product["in_stock"] == filters["in_stock"]])

    def search_products(self, query: str, filters: dict) -> list[dict]:
        self.product_searches.append((query, dict(filters)))
        return [p for p in self.products if self._matches(p, filters)]

    def search_policies(self, query: str) -> list[dict]:
        self.policy_searches.append(query)
        return list(self.policies)

    def lookup_order(self, order_number: str, email: str) -> OrderLookup:
        self.lookups.append((order_number, email))
        record = self.orders.get(order_number)
        if record is not None and record["email"] == email.lower():
            return OrderLookup(verified=True, order=dict(record["order"]))
        return OrderLookup(verified=False)  # an unknown number and a wrong email look the same

    def create_ticket(self, *, conversation_id: str, summary: str, order_number: str | None, contact_email: str) -> dict:
        if self.fail_ticket is not None:
            raise self.fail_ticket
        key = (conversation_id, order_number, summary)
        existing = next((t for t in self.tickets if t["key"] == key), None)
        if existing:
            return {"ticket_id": existing["ticket_id"], "created": False}
        ticket = {"key": key, "ticket_id": f"TCK-{len(self.tickets) + 1:04d}", "email": contact_email, "order_number": order_number}
        self.tickets.append(ticket)
        return {"ticket_id": ticket["ticket_id"], "created": True}


class FakeAnswerer:
    """Writes a plain answer from the evidence; can be told to report a guardrail fallback."""

    model = "fake-answerer"

    def __init__(self, fallback_reason: str | None = None):
        self.calls, self.fallback_reason = [], fallback_reason

    def compose(self, kind: str, question: str, evidence: dict) -> Answer:
        self.calls.append((kind, question, evidence))
        if kind == "products":
            text = "Gợi ý: " + ", ".join(f"{h['name']} ({h['sku']}, {h['price_vnd']}đ)" for h in evidence["hits"])
        elif kind == "policies":
            text = "Theo " + ", ".join(f"{h['file']} khoản {h['clause']}" for h in evidence["hits"])
        else:
            text = f"Đơn {evidence['order']['number']} đang ở trạng thái {evidence['order']['status']}."
        return Answer(text=text, fallback_reason=self.fallback_reason)
