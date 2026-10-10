"""CachedGenerator: the only way components talk to the model. Cache first; a miss is a live call (or, offline,
a CacheMiss). Every 200 answer is cached, usable or not: a blocked or empty answer is a recorded fact of the run,
and re-running must not spend quota to ask again. The component decides what an unusable answer means."""

from __future__ import annotations

from dataclasses import dataclass

from app.llm.cache import CacheKey, DiskCache, canonical_json, sha256_text
from app.llm.client import MODEL_ID, GeminiClient
from app.llm.errors import CacheMiss


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    thought_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class GenResult:
    raw: dict
    text: str | None
    finish_reason: str | None
    usage: Usage
    latency_ms: float
    cached: bool
    digest: str
    http_attempts: int
    model_version: str | None


def parse_usage(raw: dict) -> Usage:
    meta = raw.get("usageMetadata") or {}
    return Usage(input_tokens=int(meta.get("promptTokenCount", 0)), output_tokens=int(meta.get("candidatesTokenCount", 0)),
                 thought_tokens=int(meta.get("thoughtsTokenCount", 0)), total_tokens=int(meta.get("totalTokenCount", 0)))


def parse_answer(raw: dict) -> tuple[str | None, str | None]:
    """(text, finishReason) of the first candidate; text is None when there is none (blocked, empty)."""
    candidates = raw.get("candidates") or []
    if not candidates:
        return None, None
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    texts = [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
    return ("".join(texts) if texts else None), candidate.get("finishReason")


class CachedGenerator:
    def __init__(self, cache: DiskCache, client: GeminiClient | None, model: str = MODEL_ID) -> None:
        self.cache, self.client, self.model = cache, client, model
        self.live_calls = self.cache_hits = 0

    def generate(self, *, component: str, template_sha256: str, system_instruction: str, user_text: str, schema: dict,
                 generation_config: dict) -> GenResult:
        key = CacheKey(model=self.model, template_sha256=template_sha256,
                       schema_sha256=sha256_text(canonical_json(schema)), input_text=user_text,
                       generation_config=generation_config)
        entry = self.cache.get(component, key)
        cached = entry is not None
        if entry is None:
            if self.client is None:
                raise CacheMiss(f"no cached {component} response for this request and live calls are off "
                                f"(digest {key.digest()[:12]}); run with --live to fetch it")
            live = self.client.generate(system_instruction=system_instruction, user_text=user_text, schema=schema,
                                        generation_config=generation_config)
            entry = self.cache.put(component, key, live.raw, latency_ms=live.latency_ms, http_attempts=live.http_attempts)
            self.live_calls += 1
        else:
            self.cache_hits += 1
        raw = entry["response"]
        text, finish = parse_answer(raw)
        return GenResult(raw=raw, text=text, finish_reason=finish, usage=parse_usage(raw), latency_ms=entry["latency_ms"],
                         cached=cached, digest=entry["digest"], http_attempts=entry["http_attempts"],
                         model_version=raw.get("modelVersion"))
