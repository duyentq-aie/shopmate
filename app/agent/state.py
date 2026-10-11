"""The state of one conversation (a LangGraph thread). Plain, serialisable values only."""

from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict, total=False):
    # --- given at the start of every turn (see Agent.turn) ---------------------------------------------------------------
    conversation_id: str
    message: str  # the customer's message of this turn
    steps: int  # graph steps used in this turn
    capped: bool  # the turn hit the step cap
    goto: str  # the node a node asks to run next
    reply: str | None  # the final text of the turn
    reply_source: str | None  # where the reply came from, for the trace: llm | template | guardrail_fallback | clarify | error_template
    reply_kind: str | None  # a fixed template to render (app/agent/templates.py) and its arguments
    reply_args: dict
    filters: dict
    hits: list
    relaxed: list  # the constraint dropped by the one relaxed search, if any
    relaxed_once: bool
    offer_dropped: list  # the constraints of an accepted offer that this turn's search left out (names; empty on every other turn)
    extract_failed: bool
    order: dict | None  # the verified order of this turn (minimal)
    ticket_result: dict | None
    awaiting: str | None  # what the agent waits for after this turn
    # --- kept for the whole conversation ---------------------------------------------------------------------------------
    intent: str | None
    pending: dict | None  # {"intent": ..., "need": [...]}: the question the agent asked and what it needs back
    clarify_count: int  # clarifying questions asked for the current request (bound: 2)
    slots: dict  # order_number, email, phone_seen (a phone number itself is never stored)
    query: str | None  # the product request so far (the clarifying answers are appended)
    ticket_text: str | None  # what the customer wants the ticket to say
    ticket: dict | None  # the proposed ticket: summary, order_number, email
    ticket_prompt: str | None
    confirm_attempts: int  # confirmation questions asked (bound: 2)
    verify_failures: int  # failed order verifications in this conversation (bound: 3)
    lookup_disabled: bool
    offer: dict | None  # an offer to search again without some constraints (query, filters, dropped, count): valid for the next message only
