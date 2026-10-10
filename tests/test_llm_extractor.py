"""FilterExtractor (Phase 5): question -> SearchFilters, closed vocabularies, nothing invented.

keyword is never extracted (R14). in_stock is True only when the customer restricts results to available items.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from app.llm.cache import DiskCache
from app.llm.client import LiveResponse
from app.llm.extractor import EXTRACTOR_FIELDS, EXTRACTOR_GENERATION_CONFIG, FilterExtractor, build_schema
from app.llm.generator import CachedGenerator
from app.llm.prompts import catalog_vocabulary, load_prompt, render_prompt
from app.retrieval.search import SearchFilters

NULLS = {f: None for f in EXTRACTOR_FIELDS}


def _raw(payload, finish="STOP"):
    text = payload if isinstance(payload, str) or payload is None else json.dumps(payload, ensure_ascii=False)
    candidate = {"finishReason": finish}
    if text is not None:
        candidate["content"] = {"parts": [{"text": text}]}
    return {"candidates": [candidate], "modelVersion": "gemini-3.5-flash-lite",
            "usageMetadata": {"promptTokenCount": 1900, "candidatesTokenCount": 40, "totalTokenCount": 1940}}


class StubClient:
    def __init__(self, raw):
        self.raw, self.requests = raw, []

    def generate(self, *, system_instruction, user_text, schema, generation_config):
        self.requests.append({"system": system_instruction, "user": user_text, "schema": schema})
        return LiveResponse(raw=self.raw, latency_ms=900.0, http_attempts=1)


def _extractor(tmp_path, payload, vocabulary=None):
    client = StubClient(_raw(payload))
    return FilterExtractor(CachedGenerator(DiskCache(tmp_path), client), load_prompt("filters_v1"), vocabulary), client


def test_the_schema_has_closed_enums_for_the_vocabulary_fields_nullable_and_no_keyword():
    vocab = catalog_vocabulary()
    schema = build_schema(vocab)
    props = schema["properties"]
    assert set(props) == set(EXTRACTOR_FIELDS) == {"category", "kind", "color", "material", "size", "price_min", "price_max", "in_stock"}
    assert "keyword" not in props and schema["required"] == list(EXTRACTOR_FIELDS)  # every field is answered, null if absent
    for field, values in (("category", vocab.categories), ("kind", vocab.kinds), ("color", vocab.colors),
                          ("material", vocab.materials), ("size", vocab.sizes)):
        assert props[field] == {"type": "STRING", "enum": list(values), "nullable": True}
    assert props["price_min"] == {"type": "INTEGER", "nullable": True} and props["in_stock"] == {"type": "BOOLEAN", "nullable": True}
    assert EXTRACTOR_GENERATION_CONFIG["temperature"] == 0 and EXTRACTOR_GENERATION_CONFIG["thinkingConfig"] == {"thinkingLevel": "minimal"}


def test_a_complete_answer_becomes_search_filters_with_exactly_the_extracted_fields(tmp_path):
    payload = {**NULLS, "kind": "Giày sneaker nữ", "color": "Trắng", "size": "37", "price_min": 300000, "price_max": 700000}
    result = _extractor(tmp_path, payload)[0].extract("giay sneaker nu trang size 37 tu 300k den 700k")
    assert result.error is None and result.dropped == {}
    assert result.filters == SearchFilters(kind="Giày sneaker nữ", color="Trắng", size="37", price_min=300000, price_max=700000)
    assert result.filters.doc_type is None and result.filters.file is None  # the router decides doc_type, not the extractor
    assert (result.usage.input_tokens, result.latency_ms, result.cached) == (1900, 900.0, False)


def test_an_all_null_answer_is_an_empty_filter_set(tmp_path):
    result = _extractor(tmp_path, NULLS)[0].extract("co gi hot khong")
    assert result.filters == SearchFilters() and result.filters.to_qdrant() is None and result.error is None


@pytest.mark.parametrize("field, value", [("kind", "Áo gió"), ("color", "Xanh"), ("color", "đen"), ("material", "Lụa tơ tằm"),
                                          ("size", "XXXL"), ("category", "thoi-trang-tre-em")])
def test_a_value_outside_the_vocabulary_becomes_null_and_is_recorded_never_invented(tmp_path, field, value):
    result = _extractor(tmp_path, {**NULLS, field: value})[0].extract("x")
    assert getattr(result.filters, field) is None and result.dropped == {field: value} and result.error is None


@pytest.mark.parametrize("field, value", [("price_max", "400k"), ("price_max", 399.5), ("price_min", -1), ("price_max", True),
                                          ("price_min", [1])])
def test_a_price_that_is_not_a_non_negative_whole_number_is_dropped(tmp_path, field, value):
    result = _extractor(tmp_path, {**NULLS, field: value})[0].extract("x")
    assert getattr(result.filters, field) is None and field in result.dropped


def test_a_whole_number_written_as_a_float_is_accepted_as_that_integer(tmp_path):
    result = _extractor(tmp_path, {**NULLS, "price_max": 500000.0})[0].extract("x")
    assert result.filters.price_max == 500000 and isinstance(result.filters.price_max, int) and result.dropped == {}


def test_an_inconsistent_price_range_drops_both_bounds(tmp_path):
    result = _extractor(tmp_path, {**NULLS, "price_min": 700000, "price_max": 300000})[0].extract("x")
    assert result.filters.price_min is None and result.filters.price_max is None
    assert result.dropped == {"price_min": 700000, "price_max": 300000}


def test_in_stock_true_is_kept_and_false_or_non_boolean_is_dropped(tmp_path):
    assert _extractor(tmp_path / "a", {**NULLS, "in_stock": True})[0].extract("x").filters.in_stock is True
    for n, value in enumerate((False, "true", 1)):
        result = _extractor(tmp_path / f"b{n}", {**NULLS, "in_stock": value})[0].extract("x")
        assert result.filters.in_stock is None and result.dropped == {"in_stock": value}


def test_keyword_and_any_other_unknown_field_are_ignored_and_recorded(tmp_path):
    result = _extractor(tmp_path, {**NULLS, "kind": "Mũ", "keyword": "bucket", "brand": "X"})[0].extract("mu bucket")
    assert result.filters == SearchFilters(kind="Mũ") and result.ignored_fields == ["brand", "keyword"]


@pytest.mark.parametrize("payload, finish", [("oops", "STOP"), (None, "SAFETY"), ('["kind"]', "STOP"), ({"kind": "Mũ"}, "STOP")])
def test_an_unusable_answer_is_an_error_with_no_filters(tmp_path, payload, finish):
    result = FilterExtractor(CachedGenerator(DiskCache(tmp_path), StubClient(_raw(payload, finish))), load_prompt("filters_v1")).extract("x")
    assert result.filters is None and result.error  # a missing field means the model did not follow the schema


def test_the_vocabulary_is_in_the_system_instruction_and_the_question_only_in_the_user_turn(tmp_path):
    extractor, client = _extractor(tmp_path, NULLS)
    extractor.extract("tim ao so mi nam")
    (request,) = client.requests
    assert request["user"] == "tim ao so mi nam" and "tim ao so mi nam" not in request["system"]
    assert request["system"] == render_prompt(load_prompt("filters_v1"), catalog_vocabulary()) and '"Áo sơ mi nam"' in request["system"]


def test_a_different_vocabulary_is_a_different_cache_key_so_a_catalog_change_never_serves_stale_answers(tmp_path):
    vocab = catalog_vocabulary()
    other = dataclasses.replace(vocab, colors=(*vocab.colors, "Tím than"))
    client = StubClient(_raw(NULLS))
    generator = CachedGenerator(DiskCache(tmp_path), client)
    FilterExtractor(generator, load_prompt("filters_v1"), vocab).extract("x")
    FilterExtractor(generator, load_prompt("filters_v1"), vocab).extract("x")
    FilterExtractor(generator, load_prompt("filters_v1"), other).extract("x")
    assert len(client.requests) == 2  # same vocabulary: cache hit; changed vocabulary: new call
