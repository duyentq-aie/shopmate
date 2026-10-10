"""Answer generation (docs/DECISIONS-phase6.md, A3, A11): the LLM writes the text of a product, policy or order answer from tool results
only, then the code guardrails (app/agent/guardrails.py) judge it. A rejected text is replaced by a deterministic answer built from the same
evidence, and the reason is returned so that the `answer` span can carry `answer_guardrail_fallback`. That replacement never says more than the
rejected text did (A21), and a policy answer that says outright "no information" is sent as the fixed no-information reply, not as a fallback.

`GeminiAnswerer` goes through the same cache and free-tier guards as the router and the extractor (CachedGenerator).
`TemplateAnswerer` makes no LLM call at all: it is what `handle_question` uses when no answerer is given.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.agent import templates as T
from app.agent.guardrails import bounded_fallback, check_answer, citation_footer, fallback_text, is_decline
from app.agent.protocols import Answer
from app.llm.generator import CachedGenerator
from app.llm.prompts import PromptTemplate, render_prompt

ANSWER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "text": {"type": "STRING"},
        "cited_skus": {"type": "ARRAY", "items": {"type": "STRING"}},
        "cited_clauses": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"file": {"type": "STRING"}, "clause": {"type": "INTEGER"}},
                                                      "required": ["file", "clause"]}},
    },
    "required": ["text", "cited_skus", "cited_clauses"],
}
ANSWER_GENERATION_CONFIG = {"temperature": 0, "maxOutputTokens": 700, "thinkingConfig": {"thinkingLevel": "minimal"}}
_HIT_FIELDS = {"products": ("sku", "name", "kind", "color", "material", "sizes", "price_vnd", "in_stock"),
               "policies": ("file", "clause", "title", "text")}


def evidence_for_llm(kind: str, evidence: dict) -> dict:
    """The evidence as the prompt sees it: facts only, no internal keys. Nothing else about the customer is in it."""
    if kind == "order":
        return {"order": evidence["order"]}
    shown = {"hits": [{name: hit[name] for name in _HIT_FIELDS[kind]} for hit in evidence["hits"]]}
    if kind == "products":
        shown["relaxed"] = list(evidence.get("relaxed", []))
    return shown


@dataclass
class AnswerStats:
    """What the evaluation reports about the guardrails."""

    answers: int = 0
    fallbacks: int = 0
    declines: int = 0  # the model said it had no information and the fixed reply was sent: not a rejection, so not a fallback
    reasons: list = field(default_factory=list)  # one entry per fallback: the rule codes that fired


class GeminiAnswerer:
    def __init__(self, generator: CachedGenerator, template: PromptTemplate) -> None:
        self.generator, self.template = generator, template
        self.model = generator.model
        self._system = render_prompt(template)
        self.stats = AnswerStats()

    def compose(self, kind: str, question: str, evidence: dict) -> Answer:
        user_text = json.dumps({"kind": kind, "question": question, "evidence": evidence_for_llm(kind, evidence)}, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":"))
        result = self.generator.generate(component="answer", template_sha256=self.template.sha256, system_instruction=self._system,
                                         user_text=user_text, schema=ANSWER_SCHEMA, generation_config=ANSWER_GENERATION_CONFIG)
        self.stats.answers += 1
        problems, text, skus, clauses = self._read(result)
        if not problems:
            problems = check_answer(kind, text, evidence, question, cited_skus=skus, cited_clauses=clauses, instructions=self._system)
        if problems:
            self.stats.fallbacks += 1
            self.stats.reasons.append(sorted({p.split(":")[0] for p in problems}))
            return Answer(text=bounded_fallback(kind, text, evidence, cited_skus=skus, cited_clauses=clauses), fallback_reason="; ".join(problems)[:300],
                          generation=result)
        if kind == "policies" and is_decline(text, clauses):  # sent as the fixed reply, so that nothing said next to the decline reaches the customer
            self.stats.declines += 1
            return Answer(text=T.NO_POLICY_INFO, generation=result, declined=True)
        if kind == "policies":  # the citation is written by the code, from clauses that were checked against the evidence
            text = text.rstrip() + "\n" + citation_footer(sorted({(c["file"], c["clause"]) for c in clauses}))
        return Answer(text=text.strip(), generation=result)

    @staticmethod
    def _read(result) -> tuple[list[str], str, list, list]:
        if result.text is None:
            return [f"unusable: no answer (finishReason={result.finish_reason})"], "", [], []
        try:
            data = json.loads(result.text)
        except json.JSONDecodeError:
            return ["unusable: the answer is not valid JSON"], "", [], []
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            return ["unusable: the answer has no text"], "", [], []
        skus = data.get("cited_skus") if isinstance(data.get("cited_skus"), list) else []
        clauses = [c for c in (data.get("cited_clauses") or []) if isinstance(c, dict) and isinstance(c.get("file"), str) and isinstance(c.get("clause"), int)]
        return [], data["text"], [s for s in skus if isinstance(s, str)], clauses


class TemplateAnswerer:
    """No LLM: the deterministic answer built from the evidence. The default of `handle_question`."""

    model = None

    def compose(self, kind: str, question: str, evidence: dict) -> Answer:
        return Answer(text=fallback_text(kind, evidence))
