"""CachedGenerator: a re-run makes zero API calls; offline mode never touches the network."""

from __future__ import annotations

import pytest

from app.llm.cache import DiskCache
from app.llm.client import LiveResponse
from app.llm.errors import CacheMiss
from app.llm.generator import CachedGenerator

RAW = {"candidates": [{"content": {"parts": [{"text": '{"intent": "product_search"}'}]}, "finishReason": "STOP"}],
       "usageMetadata": {"promptTokenCount": 49, "candidatesTokenCount": 8, "totalTokenCount": 57},
       "modelVersion": "gemini-3.5-flash-lite"}
SCHEMA = {"type": "OBJECT", "properties": {"intent": {"type": "STRING"}}}
CONFIG = {"temperature": 0, "maxOutputTokens": 256}


class StubClient:
    def __init__(self, raw=RAW):
        self.calls, self.raw = [], raw

    def generate(self, *, system_instruction, user_text, schema, generation_config):
        self.calls.append(user_text)
        return LiveResponse(raw=self.raw, latency_ms=812.5, http_attempts=1)


def _generate(gen, text="shop oi co ao khoac nam khong", template="t" * 64, config=CONFIG):
    return gen.generate(component="router", template_sha256=template, system_instruction="Classify.", user_text=text,
                        schema=SCHEMA, generation_config=config)


def test_the_first_call_is_live_and_stored_the_second_is_served_from_the_cache(tmp_path):
    client = StubClient()
    gen = CachedGenerator(DiskCache(tmp_path), client)
    first, second = _generate(gen), _generate(gen)
    assert (first.cached, second.cached) == (False, True) and len(client.calls) == 1
    assert first.text == second.text == '{"intent": "product_search"}'
    assert second.latency_ms == 812.5  # the latency recorded when the call was live
    assert (gen.live_calls, gen.cache_hits) == (1, 1)


def test_a_new_generator_over_the_same_cache_makes_zero_api_calls(tmp_path):
    _generate(CachedGenerator(DiskCache(tmp_path), StubClient()))
    client = StubClient()
    again = _generate(CachedGenerator(DiskCache(tmp_path), client))
    assert again.cached and client.calls == []


def test_offline_mode_refuses_a_miss_without_any_call(tmp_path):
    gen = CachedGenerator(DiskCache(tmp_path), client=None)
    with pytest.raises(CacheMiss, match="router"):
        _generate(gen)
    _generate(CachedGenerator(DiskCache(tmp_path), StubClient()))  # fill it
    assert _generate(gen).cached  # and offline serves a hit


@pytest.mark.parametrize("change", [{"text": "another question"}, {"template": "u" * 64},
                                    {"config": {"temperature": 0.5, "maxOutputTokens": 256}}])
def test_anything_that_changes_the_key_is_a_new_live_call(tmp_path, change):
    client = StubClient()
    gen = CachedGenerator(DiskCache(tmp_path), client)
    _generate(gen)
    _generate(gen, **change)
    assert len(client.calls) == 2


def test_usage_text_and_model_version_are_read_from_the_raw_response(tmp_path):
    result = _generate(CachedGenerator(DiskCache(tmp_path), StubClient()))
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.thought_tokens, result.usage.total_tokens) == (49, 8, 0, 57)
    assert result.model_version == "gemini-3.5-flash-lite" and result.finish_reason == "STOP" and result.http_attempts == 1
    with_thoughts = {**RAW, "usageMetadata": {**RAW["usageMetadata"], "thoughtsTokenCount": 12}}
    assert _generate(CachedGenerator(DiskCache(tmp_path / "b"), StubClient(with_thoughts))).usage.thought_tokens == 12


def test_an_answer_without_text_is_returned_as_such_and_cached_not_raised(tmp_path):
    blocked = {"candidates": [{"finishReason": "SAFETY"}], "usageMetadata": {"promptTokenCount": 10, "totalTokenCount": 10}}
    client = StubClient(blocked)
    gen = CachedGenerator(DiskCache(tmp_path), client)
    first, second = _generate(gen), _generate(gen)
    assert first.text is None and first.finish_reason == "SAFETY" and second.cached and len(client.calls) == 1
    assert _generate(CachedGenerator(DiskCache(tmp_path / "x"), StubClient({}))).text is None  # nothing at all
