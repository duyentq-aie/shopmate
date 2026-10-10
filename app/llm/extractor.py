"""FilterExtractor (Phase 5): one customer question -> SearchFilters, with CLOSED vocabularies from the catalog.

The vocabularies are both in the prompt and the enum values of the response schema (nullable). Whatever the model
returns is still validated here: a value outside the vocabulary becomes null and is recorded in `dropped`, never
invented, never snapped to a "nearest" value. Price semantics (documented in prompts/filters_v1.md, taken from the
golden labels and eval/golden/schema.apply_filters): bounds are inclusive, "dưới X" -> price_max, "trên X" ->
price_min, "từ A đến B" -> both, k = thousand. in_stock is True only when the customer RESTRICTS results to available
items; "còn ko?" asks about availability, and filtering would hide the very item asked about (DECISIONS R32).
`keyword` is not a filter (R14) and has no field; if the model adds one it is ignored and recorded.
doc_type is the router's job: this extractor leaves it unset.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.llm.generator import CachedGenerator, Usage
from app.llm.prompts import PromptTemplate, Vocabulary, catalog_vocabulary, render_prompt
from app.retrieval.search import SearchFilters
from app.tracing import span

EXTRACTOR_FIELDS = ("category", "kind", "color", "material", "size", "price_min", "price_max", "in_stock")
EXTRACTOR_GENERATION_CONFIG = {"temperature": 0, "maxOutputTokens": 512, "thinkingConfig": {"thinkingLevel": "minimal"}}
_ENUM_FIELDS = (("category", "categories"), ("kind", "kinds"), ("color", "colors"), ("material", "materials"), ("size", "sizes"))


def build_schema(vocabulary: Vocabulary) -> dict:
    props: dict[str, dict] = {name: {"type": "STRING", "enum": list(getattr(vocabulary, plural)), "nullable": True}
                              for name, plural in _ENUM_FIELDS}
    props["price_min"] = {"type": "INTEGER", "nullable": True}
    props["price_max"] = {"type": "INTEGER", "nullable": True}
    props["in_stock"] = {"type": "BOOLEAN", "nullable": True}
    return {"type": "OBJECT", "properties": {name: props[name] for name in EXTRACTOR_FIELDS},
            "required": list(EXTRACTOR_FIELDS), "propertyOrdering": list(EXTRACTOR_FIELDS)}


@dataclass(frozen=True)
class ExtractionResult:
    filters: SearchFilters | None  # None only when the answer was unusable (see `error`)
    raw_response: dict
    usage: Usage | None
    cached: bool
    latency_ms: float
    dropped: dict = field(default_factory=dict)  # field -> the value that was rejected (and turned into null)
    ignored_fields: list = field(default_factory=list)  # fields the schema does not have (keyword, ...)
    error: str | None = None


def _whole_number(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value >= 0 else None
    return None


def clean(data: dict, vocabulary: Vocabulary) -> tuple[SearchFilters, dict, list]:
    """The validated filters, what was dropped, and the unknown fields that were ignored."""
    kept: dict = {}
    dropped: dict = {}
    for name, plural in _ENUM_FIELDS:
        value = data.get(name)
        if value is None:
            continue
        if isinstance(value, str) and value in getattr(vocabulary, plural):
            kept[name] = value
        else:
            dropped[name] = value
    prices = {}
    for name in ("price_min", "price_max"):
        value = data.get(name)
        if value is None:
            continue
        number = _whole_number(value)
        if number is None:
            dropped[name] = value
        else:
            prices[name] = number
    if "price_min" in prices and "price_max" in prices and prices["price_min"] > prices["price_max"]:
        dropped.update(price_min=prices.pop("price_min"), price_max=prices.pop("price_max"))
    kept.update(prices)
    in_stock = data.get("in_stock")
    if in_stock is True:
        kept["in_stock"] = True
    elif in_stock is not None:
        dropped["in_stock"] = in_stock  # False (or anything else) is not a request the system supports
    ignored = sorted(set(data) - set(EXTRACTOR_FIELDS))
    return SearchFilters(**kept), dropped, ignored


class FilterExtractor:
    def __init__(self, generator: CachedGenerator, template: PromptTemplate, vocabulary: Vocabulary | None = None) -> None:
        self.generator, self.template = generator, template
        self.vocabulary = vocabulary or catalog_vocabulary()
        self._system = render_prompt(template, self.vocabulary)
        self.schema = build_schema(self.vocabulary)
        self.name = f"{generator.model}/{template.name}"

    def extract(self, question: str) -> ExtractionResult:
        with span("extractor", model=self.generator.model) as recorded:  # a no-op outside a traced request
            result = self.generator.generate(component="filters", template_sha256=self.template.sha256,
                                             system_instruction=self._system, user_text=question, schema=self.schema,
                                             generation_config=EXTRACTOR_GENERATION_CONFIG)
            recorded.record_response(result)
            extraction = self._interpret(result)
            if extraction.error:
                recorded.fail("llm_invalid_output", extraction.error)
            elif extraction.dropped:  # field names only: the dropped values are model output, not needed in a trace
                recorded.fail("filter_out_of_vocab", "dropped fields: " + ", ".join(sorted(extraction.dropped)))
            return extraction

    def _interpret(self, result) -> ExtractionResult:
        common = dict(raw_response=result.raw, usage=result.usage, cached=result.cached, latency_ms=result.latency_ms)
        if result.text is None:
            return ExtractionResult(filters=None, error=f"no answer (finishReason={result.finish_reason})", **common)
        try:
            data = json.loads(result.text)
        except json.JSONDecodeError:
            return ExtractionResult(filters=None, error="the answer is not valid JSON", **common)
        if not isinstance(data, dict):
            return ExtractionResult(filters=None, error="the answer is not a JSON object", **common)
        missing = [name for name in EXTRACTOR_FIELDS if name not in data]
        if missing:
            return ExtractionResult(filters=None, error=f"the answer lacks required fields {missing}", **common)
        filters, dropped, ignored = clean(data, self.vocabulary)
        return ExtractionResult(filters=filters, dropped=dropped, ignored_fields=ignored, **common)
