"""The request path router -> (extractor) -> retrieval, with optional request tracing (app/tracing.py).

Same routing rules as the Phase 5 evaluation pipeline (eval/run_phase5_eval.py), as a function a service can call:
- product_search: extract filters, retrieve with doc_type product + the extracted filters (an unusable extraction
  falls back to doc_type only);
- policy_question: retrieve with doc_type policy;
- any other intent: no retrieval (the later phases handle orders, tickets and replies);
- an unusable router answer: no intent, no retrieval, the request is traced as an error.
LLM guard errors (daily quota, retries exhausted) and retrieval failures propagate unchanged; the trace is written
anyway. With `tracer=None` nothing is traced and nothing is written.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field

from app.retrieval.search import Hit


@dataclass(frozen=True)
class PipelineResult:
    intent: str | None
    filters: dict | None
    hits: list[Hit] = field(default_factory=list)
    error: str | None = None


# INTERIM ENTRY POINT until the Phase 6 agent loop. This fixed router -> extractor -> retrieval sequence exists only so that
# a request has one place to be traced end to end; the agent loop will replace it (and own the order_lookup, ticket_confirm,
# reply and answer stages). Do not grow it into the agent: tracing (app/tracing.py) is what carries over, this function is not.
def handle_question(question: str, *, router, extractor, retriever, tracer=None, k: int = 10, mode=None) -> PipelineResult:
    with (tracer.request(question) if tracer is not None else nullcontext(None)) as trace:
        route = router.route(question)
        if route.intent is None:
            if trace is not None:
                trace.fail("llm_invalid_output", route.error or "the router returned no intent")
            return PipelineResult(intent=None, filters=None, hits=[], error=route.error)
        filters: dict | None = None
        if route.intent == "product_search":
            extraction = extractor.extract(question)
            extracted = extraction.filters.model_dump(exclude_none=True) if extraction.filters is not None else {}
            filters = {"doc_type": "product", **extracted}
        elif route.intent == "policy_question":
            filters = {"doc_type": "policy"}
        hits = retriever.search(question, filters, k=k, mode=mode) if filters is not None else []
        return PipelineResult(intent=route.intent, filters=filters, hits=hits)
