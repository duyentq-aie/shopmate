"""Gemini REST client with the free-tier guards (Phase 5). Plain httpx: no SDK, no new dependency.

Per request, in this order: per-run cap check, daily-budget reservation (counted BEFORE sending, retries
included), client-side rate limit (RPM + TPM, waits), send. Failure handling:
- 429 whose body says the DAILY quota is used, or reports `limit: 0`: stop at once (DailyLimitExhausted /
  FreeTierUnavailable). Never retried: a retry loop must not burn a daily quota.
- 429 per-minute (QuotaFailure id or RetryInfo recognised): exponential backoff with jitter, at least the delay the
  API asked for, bounded by max_attempts.
- 429 not recognised: bounded retries (max_unknown_429_retries), then stop.
- 5xx and network errors: exponential backoff, bounded by max_attempts.
- other 4xx: ApiError at once.
The API key travels in the `x-goog-api-key` header only and is scrubbed from every message; it is never stored in
the cache or in exceptions.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.llm.errors import (
    ApiError,
    BadResponse,
    CallCapExceeded,
    DailyLimitExhausted,
    FreeTierUnavailable,
    RetriesExhausted,
)
from app.llm.limits import DailyBudget, RateLimiter

MODEL_ID = "gemini-3.5-flash-lite"  # pinned exact id (version 3.5-flash-lite-07-2026); never an alias like *-latest
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
CHARS_PER_TOKEN = 3  # conservative for Vietnamese: the estimate only feeds the client-side token limiter


@dataclass(frozen=True)
class LiveResponse:
    raw: dict
    latency_ms: float
    http_attempts: int
    minute_429_retries: int = 0  # recognised per-minute 429s this call retried before it succeeded (for tracing; not cached)


def estimate_tokens(system_instruction: str, user_text: str, schema: dict, max_output_tokens: int) -> int:
    chars = len(system_instruction) + len(user_text) + len(json.dumps(schema, ensure_ascii=False))
    return math.ceil(chars / CHARS_PER_TOKEN) + max_output_tokens


def _seconds(value: str) -> float | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", value or "")
    return float(match.group(1)) if match else None


def classify_429(body) -> tuple[str, float | None]:
    """("zero_limit" | "daily" | "per_minute" | "unknown", retry delay in seconds if the API gave one).

    Only the structured parts of Google's error format are trusted (QuotaFailure quotaId / quotaValue, RetryInfo)
    plus an explicit "limit: 0" in the message; a bare message is not enough to call a 429 per-minute."""
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return "unknown", None
    error = body["error"]
    message = str(error.get("message", "")).lower()
    quota_ids, quota_values, delay = [], [], None
    for detail in error.get("details") or []:
        kind = str(detail.get("@type", ""))
        if kind.endswith("QuotaFailure"):
            for violation in detail.get("violations") or []:
                quota_ids.append(str(violation.get("quotaId", "")).lower())
                quota_values.append(str(violation.get("quotaValue", "")))
        elif kind.endswith("RetryInfo"):
            delay = _seconds(str(detail.get("retryDelay", "")))
    if "limit: 0" in message or "0" in quota_values:
        return "zero_limit", None
    if any("perday" in q for q in quota_ids):
        return "daily", None
    if any("perminute" in q for q in quota_ids):
        return "per_minute", delay
    return "unknown", None


class GeminiClient:
    def __init__(self, api_key: str, *, model: str = MODEL_ID, http: httpx.Client | None = None,
                 limiter: RateLimiter, budget: DailyBudget, max_live_requests: int = 300, max_attempts: int = 5,
                 max_unknown_429_retries: int = 2, base_delay: float = 2.0, max_delay: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None,
                 base_url: str = BASE_URL) -> None:
        if not api_key:
            raise ValueError("an API key is required")
        self._api_key = api_key
        self.model, self.limiter, self.budget = model, limiter, budget
        self.max_live_requests, self.max_attempts = max_live_requests, max_attempts
        self.max_unknown_429_retries, self.base_delay, self.max_delay = max_unknown_429_retries, base_delay, max_delay
        self._http = http or httpx.Client(timeout=60)
        self._sleep, self._rng, self._base_url = sleep, rng or random.Random(), base_url
        self.live_requests = 0  # requests sent by this client (this run)

    def __repr__(self) -> str:
        return f"GeminiClient(model={self.model!r}, live_requests={self.live_requests})"

    def _scrub(self, text: str) -> str:
        return text.replace(self._api_key, "***")

    def _backoff(self, retry_index: int, retry_after: float | None = None) -> float:
        delay = min(self.max_delay, self.base_delay * 2 ** retry_index)
        return max(delay, retry_after or 0.0) + self._rng.uniform(0.0, 1.0)

    def generate(self, *, system_instruction: str, user_text: str, schema: dict, generation_config: dict) -> LiveResponse:
        body = {"systemInstruction": {"parts": [{"text": system_instruction}]},
                "contents": [{"role": "user", "parts": [{"text": user_text}]}],
                "generationConfig": {**generation_config, "responseMimeType": "application/json",
                                     "responseSchema": schema}}
        tokens = estimate_tokens(system_instruction, user_text, schema, int(generation_config.get("maxOutputTokens", 256)))
        url = f"{self._base_url}/models/{self.model}:generateContent"
        attempts = retries = unknown_retries = minute_429s = 0
        while True:
            if self.live_requests >= self.max_live_requests:
                raise CallCapExceeded(f"the per-run cap of {self.max_live_requests} live requests is reached")
            self.budget.reserve()
            self.limiter.acquire(tokens)
            attempts += 1
            self.live_requests += 1
            started = time.perf_counter()
            try:
                response = self._http.post(url, headers={"x-goog-api-key": self._api_key}, json=body)
            except httpx.TransportError as exc:
                reason, wait, cause = f"network error ({type(exc).__name__})", None, "network"
            else:
                latency_ms = (time.perf_counter() - started) * 1000
                status = response.status_code
                if status == 200:
                    try:
                        raw = response.json()
                    except ValueError:
                        raise BadResponse("the API answered 200 with a body that is not JSON") from None
                    return LiveResponse(raw=raw, latency_ms=round(latency_ms, 1), http_attempts=attempts,
                                        minute_429_retries=minute_429s)
                try:
                    error_body = response.json()
                except ValueError:
                    error_body = None
                if status == 429:
                    kind, wait = classify_429(error_body)
                    if kind == "daily":
                        raise DailyLimitExhausted("the API reports the daily quota is exhausted; stopping, not retrying")
                    if kind == "zero_limit":
                        raise FreeTierUnavailable("the API reports a limit of 0 for this model on this key; stopping")
                    if kind == "unknown":
                        unknown_retries += 1
                        if unknown_retries > self.max_unknown_429_retries:
                            raise RetriesExhausted(f"429 not recognised as per-minute, still failing after "
                                                   f"{attempts} attempts; stopping", cause="rate_limit") from None
                    elif kind == "per_minute":
                        minute_429s += 1  # counted when seen; it only matters if a later attempt then succeeds
                    reason, cause = f"429 ({kind})", "rate_limit"
                elif status >= 500:
                    reason, wait, cause = f"server error {status}", None, "server_error"
                else:
                    message = (error_body or {}).get("error", {}).get("message") if isinstance(error_body, dict) else None
                    raise ApiError(status, self._scrub(str(message or response.text[:200])))
            if attempts >= self.max_attempts:
                raise RetriesExhausted(f"{reason} on the last of {attempts} attempts; stopping", cause=cause)
            self._sleep(self._backoff(retries, wait))
            retries += 1
