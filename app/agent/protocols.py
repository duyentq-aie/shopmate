"""What the agent needs from the outside: tools, an answerer, and the results they return.

The router and the extractor are the Phase 5 components (`route(text)` -> intent, `extract(text)` -> filters) and are used
as they are. Hits, orders and tickets travel as plain dicts so that the conversation state stays serialisable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OrderLookup:
    """Result of `lookup_order`. `verified` is True only when the order number AND the owner's email matched the same
    order. There is deliberately no second field that says why it failed: an unknown number and a wrong email must be
    indistinguishable to everything downstream (D28)."""

    verified: bool
    order: dict | None = None  # the minimal order: number, status, created, lines, fulfillments; never an address, phone or email


@dataclass(frozen=True)
class Answer:
    """The text of an answer. `fallback_reason` is set when a guardrail discarded the generated text and `text` is the fixed template."""

    text: str
    fallback_reason: str | None = None


class AgentTools(Protocol):
    def search_products(self, query: str, filters: dict) -> list[dict]: ...

    def search_policies(self, query: str) -> list[dict]: ...

    def lookup_order(self, order_number: str, email: str) -> OrderLookup: ...

    def create_ticket(self, *, conversation_id: str, summary: str, order_number: str | None, contact_email: str) -> dict:
        """-> {"ticket_id": str, "created": bool}. Raises when the ticket cannot be written."""
        ...


class Answerer(Protocol):
    def compose(self, kind: str, question: str, evidence: dict) -> Answer:
        """kind: "products" | "policies" | "order". evidence: {"hits": [...], "relaxed": [...]} | {"hits": [...]} | {"order": {...}}."""
        ...
