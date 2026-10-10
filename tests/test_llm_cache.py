"""Disk cache of LLM calls. A re-run of an evaluation must make zero API calls (Phase 5 free-tier guard).

The key covers everything that can change an answer: model id, prompt template hash, schema hash (which holds the
closed vocabularies), the input and the generation config.
"""

from __future__ import annotations

import json

import pytest

from app.llm.cache import CacheKey, DiskCache, canonical_json
from app.llm.errors import CacheCorrupt

BASE = dict(model="gemini-3.5-flash-lite", template_sha256="t" * 64, schema_sha256="s" * 64,
            input_text="shop oi co ao khoac nam khong", generation_config={"temperature": 0, "maxOutputTokens": 256})


def _key(**changes) -> CacheKey:
    return CacheKey(**{**BASE, **changes})


def test_the_digest_is_stable_and_ignores_the_key_order_of_the_generation_config():
    a = _key(generation_config={"temperature": 0, "thinkingConfig": {"thinkingLevel": "minimal"}})
    b = _key(generation_config={"thinkingConfig": {"thinkingLevel": "minimal"}, "temperature": 0})
    assert a.digest() == b.digest() and len(a.digest()) == 64


@pytest.mark.parametrize("change", [
    {"model": "gemini-3.8-flash"},
    {"template_sha256": "u" * 64},
    {"schema_sha256": "v" * 64},
    {"input_text": "shop oi co ao khoac nam khong "},  # one trailing space is another input
    {"generation_config": {"temperature": 0.2, "maxOutputTokens": 256}},
    {"generation_config": {"temperature": 0, "maxOutputTokens": 512}},
])
def test_every_part_of_the_key_changes_the_digest(change):
    assert _key(**change).digest() != _key().digest()


def test_a_stored_entry_is_returned_with_its_raw_response_and_recorded_latency(tmp_path):
    cache = DiskCache(tmp_path)
    assert cache.get("router", _key()) is None
    raw = {"candidates": [{"content": {"parts": [{"text": '{"intent": "product_search"}'}]}}], "usageMetadata": {"promptTokenCount": 49}}
    cache.put("router", _key(), raw, latency_ms=812.5, http_attempts=1)
    entry = cache.get("router", _key())
    assert entry["response"] == raw and entry["latency_ms"] == 812.5 and entry["http_attempts"] == 1
    assert entry["key"]["input"] == BASE["input_text"] and entry["digest"] == _key().digest()
    assert cache.get("filters", _key()) is None  # components do not share entries
    assert cache.get("router", _key(input_text="khac")) is None


def test_entries_are_plain_readable_json_files_and_hold_no_secret(tmp_path):
    cache = DiskCache(tmp_path)
    cache.put("router", _key(), {"candidates": []}, latency_ms=1.0, http_attempts=1)
    (path,) = tmp_path.glob("router/*.json")
    assert path.name == f"{_key().digest()}.json"
    text = path.read_text(encoding="utf-8")
    assert json.loads(text)["digest"] == _key().digest()
    assert "x-goog-api-key" not in text.lower() and "authorization" not in text.lower()


def test_an_existing_entry_is_never_overwritten(tmp_path):
    cache = DiskCache(tmp_path)
    cache.put("router", _key(), {"v": 1}, latency_ms=1.0, http_attempts=1)
    with pytest.raises(FileExistsError):
        cache.put("router", _key(), {"v": 2}, latency_ms=2.0, http_attempts=1)
    assert cache.get("router", _key())["response"] == {"v": 1}


def test_a_tampered_or_misfiled_entry_is_refused_not_trusted(tmp_path):
    cache = DiskCache(tmp_path)
    cache.put("router", _key(), {"v": 1}, latency_ms=1.0, http_attempts=1)
    (path,) = tmp_path.glob("router/*.json")
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["key"]["input"] = "another question"
    path.write_text(json.dumps(entry), encoding="utf-8")
    with pytest.raises(CacheCorrupt):
        cache.get("router", _key())
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(CacheCorrupt):
        cache.get("router", _key())


def test_vietnamese_text_is_stored_as_is_not_escaped(tmp_path):
    cache = DiskCache(tmp_path)
    key = _key(input_text="Có váy liền nào dưới 400k ko shop?")
    cache.put("router", key, {"ok": True}, latency_ms=1.0, http_attempts=1)
    (path,) = tmp_path.glob("router/*.json")
    assert "Có váy liền nào dưới 400k ko shop?" in path.read_text(encoding="utf-8")
    assert canonical_json({"b": 1, "a": "đ"}) == '{"a":"đ","b":1}'


def test_entries_lists_everything_stored_for_a_component(tmp_path):
    cache = DiskCache(tmp_path)
    for n in range(3):
        cache.put("router", _key(input_text=f"q{n}"), {"n": n}, latency_ms=float(n), http_attempts=1)
    assert sorted(e["response"]["n"] for e in cache.entries("router")) == [0, 1, 2] and list(cache.entries("filters")) == []
