"""The entry point of a single question: `handle_question` runs ONE turn of the conversation agent (app/agent/, docs/AGENT-DESIGN.md) in a
conversation of its own, with optional request tracing (app/tracing.py).

This replaced the interim router -> extractor -> retrieval sequence of the tracing step (docs/DECISIONS-phase6.md, A12). The signature is the
same except that the answer and the tools are now part of it: without an `answerer` the answers are the deterministic ones of
`TemplateAnswerer` (no LLM call); without a `saleor` client an order lookup is an error, not a refusal; tickets go to `tickets`
(default data/tickets/tickets.jsonl). For a conversation of several turns (clarifying questions, order verification, ticket confirmation)
use `app.agent.Agent` directly and keep the conversation id.

LLM guard errors (daily quota, retries exhausted), a cache miss without --live and service failures propagate unchanged; the trace is written
anyway. With `tracer=None` nothing is traced and nothing is written.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.agent import templates as T
from app.agent.agent import Agent
from app.agent.answer import TemplateAnswerer
from app.agent.tools import RealTools, TicketStore


@dataclass(frozen=True)
class PipelineResult:
    intent: str | None
    filters: dict | None
    hits: list[dict] = field(default_factory=list)  # product or policy dicts, as the agent saw them
    error: str | None = None
    reply: str = ""
    awaiting: str | None = None  # what the next message of this conversation would have to be, if there were one


def handle_question(question: str, *, router, extractor, retriever, tracer=None, answerer=None, saleor=None, tickets: TicketStore | None = None,
                    conversation_id: str | None = None) -> PipelineResult:
    agent = Agent(router=router, extractor=extractor, tools=RealTools(retriever, saleor, tickets), answerer=answerer or TemplateAnswerer(), tracer=tracer)
    turn = agent.turn(conversation_id or uuid.uuid4().hex, question)
    error = "no usable intent" if turn.intent is None and turn.reply == T.TECHNICAL_PROBLEM else None
    return PipelineResult(intent=turn.intent, filters=turn.filters, hits=turn.hits, error=error, reply=turn.reply, awaiting=turn.awaiting)
