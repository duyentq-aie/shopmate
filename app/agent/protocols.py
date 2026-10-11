"""What the agent needs from the outside: tools, an answerer, and the results they return.

The router and the extractor are the Phase 5 components (`route(text)` -> intent, `extract(text)` -> filters) and are used
as they are. Hits, orders and tickets travel as plain dicts so that the conversation state stays serialisable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.agent.relax import Offer


@dataclass(frozen=True)
class OrderLookup:
    """Result of `lookup_order`. `verified` is True only when the order number AND the owner's email matched the same
    order. There is deliberately no second field that says why it failed: an unknown number and a wrong email must be
    indistinguishable to everything downstream (D28)."""

    verified: bool
    order: dict | None = None  # the minimal order: number, status, created, lines, fulfillments; never an address, phone or email


@dataclass(frozen=True)
class Answer:
    """The text of an answer. `fallback_reason` is set when a guardrail discarded the generated text and `text` is the fixed template.
    `declined` is set when the model itself said it has no information and `text` is the fixed no-information reply: nothing was rejected."""

    text: str
    fallback_reason: str | None = None
    generation: object | None = None  # the GenResult of the LLM call, if there was one: the answer span records its tokens from it
    declined: bool = False
    prompt_version: str | None = None  # for the trace: the prompt template that wrote the text (None without an LLM)
    checked: int | None = None  # for the trace: sentences the code guardrails went through (None: no guardrail ran)
    violations: tuple[str, ...] = ()  # for the trace: the rule names that fired (the part of each problem before the colon)


class AgentTools(Protocol):
    def search_products(self, query: str, filters: dict) -> list[dict]: ...

    def search_policies(self, query: str) -> list[dict]: ...

    def offer_for(self, filters: dict) -> Offer | None:
        """After an empty product search: the offer of docs/DECISIONS-phase7.md D7.25 (which constraints to drop for there to be products, and how many), found by
        COUNTING, never by searching; None if no step finds anything. Never drops `kind`."""
        ...

    def lookup_order(self, order_number: str, email: str) -> OrderLookup: ...

    def create_ticket(self, *, conversation_id: str, summary: str, order_number: str | None, contact_email: str) -> dict:
        """-> {"ticket_id": str, "created": bool}. Raises when the ticket cannot be written."""
        ...


class Answerer(Protocol):
    def compose(self, kind: str, question: str, evidence: dict) -> Answer:
        """kind: "products" | "policies" | "order". evidence: {"hits": [...], "relaxed": [...]} | {"hits": [...]} | {"order": {...}}."""
        ...
