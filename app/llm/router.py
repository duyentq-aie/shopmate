"""Intent router interface and its Gemini implementation (Phase 5).

`Router.route(question)` returns a RouteResult; callers depend only on this interface, so a Jev router can be added
later without touching them. GeminiRouter uses structured output (an enum of the six intents), temperature 0 and
minimal thinking.

Confidence: gemini-3.5-flash-lite exposes NO token log-probabilities (the API answers 400 "Logprobs is not enabled
for this model", checked 2026-10-10), so there is no calibrated probability and `confidence` stays None. The model
is deliberately NOT asked to rate itself: a self-reported number is not calibrated. (If a future model exposes
log-probabilities, the probability of the chosen intent would be derived from the logprob of the enum value's
first distinguishing token; not implemented, nothing to test it on.)

A message-level failure (blocked, empty, not JSON, intent outside the enum) is returned as an error on the result,
never repaired into a label: the evaluation counts it as a miss.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.llm.generator import CachedGenerator, Usage
from app.llm.intents import INTENTS
from app.llm.prompts import PromptTemplate, render_prompt
from app.tracing import record_intent, span

ROUTER_SCHEMA = {"type": "OBJECT", "properties": {"intent": {"type": "STRING", "enum": list(INTENTS)}},
                 "required": ["intent"]}
ROUTER_GENERATION_CONFIG = {"temperature": 0, "maxOutputTokens": 256, "thinkingConfig": {"thinkingLevel": "minimal"}}
NO_CONFIDENCE_NOTE = ("no calibrated confidence: the model exposes no log-probabilities and was not asked to rate "
                      "itself")


@dataclass(frozen=True)
class RouteResult:
    intent: str | None  # None when the answer was unusable (see `error`)
    raw_response: dict
    usage: Usage | None
    confidence: float | None
    confidence_note: str
    cached: bool
    latency_ms: float
    error: str | None


@runtime_checkable
class Router(Protocol):
    name: str

    def route(self, question: str) -> RouteResult: ...


def parse_intent(text: str | None, finish_reason: str | None) -> tuple[str | None, str | None]:
    """(intent, error)."""
    if text is None:
        return None, f"no answer (finishReason={finish_reason})"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "the answer is not valid JSON"
    if not isinstance(data, dict):
        return None, "the answer is not a JSON object"
    if "intent" not in data:
        return None, "the answer has no `intent` field"
    if not isinstance(data["intent"], str) or data["intent"] not in INTENTS:
        return None, f"the intent {data['intent']!r} is not one of the six"
    return data["intent"], None


class GeminiRouter:
    def __init__(self, generator: CachedGenerator, template: PromptTemplate) -> None:
        self.generator, self.template = generator, template
        self._system = render_prompt(template)
        self.name = f"{generator.model}/{template.name}"

    def route(self, question: str) -> RouteResult:
        with span("router", model=self.generator.model) as recorded:  # a no-op outside a traced request
            result = self.generator.generate(component="router", template_sha256=self.template.sha256,
                                             system_instruction=self._system, user_text=question, schema=ROUTER_SCHEMA,
                                             generation_config=ROUTER_GENERATION_CONFIG)
            intent, error = parse_intent(result.text, result.finish_reason)
            recorded.record_response(result)
            if error:
                recorded.fail("llm_invalid_output", error)
            else:
                record_intent(intent)
                recorded.set_summary(intent=intent)  # no confidence: the model has no log-probabilities (see the module docstring)
            return RouteResult(intent=intent, raw_response=result.raw, usage=result.usage, confidence=None,
                               confidence_note=NO_CONFIDENCE_NOTE, cached=result.cached, latency_ms=result.latency_ms,
                               error=error)
