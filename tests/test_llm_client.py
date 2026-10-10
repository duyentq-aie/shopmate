"""GeminiClient against a fake HTTP transport: no network, no real sleeping, never the real key.

The 429 bodies below follow Google's documented error format (google.rpc.QuotaFailure / RetryInfo). The real
format has not been observed on this key yet, so anything not recognised is retried a BOUNDED number of times and
then stops; a daily or "limit: 0" quota stops at once. A retry loop is never allowed to burn a daily quota.
"""

from __future__ import annotations

import datetime as dt
import json
import random

import httpx
import pytest

from app.llm.client import GeminiClient, classify_429, estimate_tokens
from app.llm.errors import (
    ApiError,
    CallCapExceeded,
    DailyBudgetReached,
    DailyLimitExhausted,
    FreeTierUnavailable,
    RetriesExhausted,
)
from app.llm.limits import DailyBudget, RateLimiter

SECRET = "AIzaSy-TEST-SECRET-0123456789"
OK = {"candidates": [{"content": {"parts": [{"text": '{"intent": "product_search"}'}]}, "finishReason": "STOP"}],
      "usageMetadata": {"promptTokenCount": 49, "candidatesTokenCount": 13, "totalTokenCount": 62},
      "modelVersion": "gemini-3.5-flash-lite"}


def quota_body(quota_id: str, retry_delay: str | None = None, message: str = "You exceeded your current quota.",
               quota_value: str = "10") -> dict:
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                                "quotaId": quota_id, "quotaValue": quota_value}]}]
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": message, "details": details}}


PER_MINUTE = quota_body("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "3s")
PER_DAY = quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier", quota_value="500")
LIMIT_ZERO = quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier", quota_value="0",
                        message="Quota exceeded for metric: generate_content_free_tier_requests, limit: 0")
UNKNOWN_429 = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Resource has been exhausted."}}


class Harness:
    """A client wired to a scripted transport; records requests, sleeps and the budget."""

    def __init__(self, tmp_path, script, *, max_live=300, daily_limit=450, **client_kwargs):
        self.script, self.requests, self.sleeps = list(script), [], []
        self.limiter_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            status, body = step
            return httpx.Response(status, json=body)

        class SpyLimiter(RateLimiter):
            def acquire(inner, tokens):  # noqa: N805
                self.limiter_calls.append(tokens)

        self.budget = DailyBudget(tmp_path, limit=daily_limit, now=lambda: dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc))
        self.client = GeminiClient(SECRET, http=httpx.Client(transport=httpx.MockTransport(handler)),
                                   limiter=SpyLimiter(), budget=self.budget, max_live_requests=max_live,
                                   sleep=self.sleeps.append, rng=random.Random(0), **client_kwargs)

    def generate(self):
        return self.client.generate(system_instruction="Classify.", user_text="shop oi co ao khoac khong",
                                    schema={"type": "OBJECT", "properties": {"intent": {"type": "STRING"}}},
                                    generation_config={"temperature": 0, "maxOutputTokens": 256})


def test_a_successful_call_returns_the_raw_response_with_the_key_only_in_a_header(tmp_path):
    h = Harness(tmp_path, [(200, OK)])
    live = h.generate()
    assert live.raw == OK and live.http_attempts == 1 and live.latency_ms >= 0
    (request,) = h.requests
    assert request.headers["x-goog-api-key"] == SECRET
    assert SECRET not in str(request.url) and request.url.path.endswith("/models/gemini-3.5-flash-lite:generateContent")
    body = json.loads(request.content)
    assert body["systemInstruction"] == {"parts": [{"text": "Classify."}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "shop oi co ao khoac khong"}]}]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseSchema"]["type"] == "OBJECT"
    assert body["generationConfig"]["temperature"] == 0 and body["generationConfig"]["maxOutputTokens"] == 256
    assert h.budget.used() == 1 and len(h.limiter_calls) == 1 and h.limiter_calls[0] > 0


def test_a_per_minute_429_backs_off_with_growing_jittered_delays_then_succeeds(tmp_path):
    h = Harness(tmp_path, [(429, PER_MINUTE), (429, PER_MINUTE), (200, OK)], base_delay=2.0)
    live = h.generate()
    assert live.http_attempts == 3 and len(h.sleeps) == 2
    assert h.sleeps[0] >= 3.0  # at least the retryDelay the API asked for
    assert h.sleeps[1] > h.sleeps[0] - 1.5 and h.sleeps[1] >= 4.0  # exponential: 2 * 2**1 = 4 s base on the 2nd retry
    assert h.sleeps[0] != h.sleeps[1] and all(s <= 60 + 1.0 for s in h.sleeps)
    assert h.budget.used() == 3  # every attempt is a request against the daily quota


def test_the_backoff_is_deterministic_for_a_seeded_rng(tmp_path):
    runs = []
    for _ in range(2):
        h = Harness(tmp_path / str(len(runs)), [(429, PER_MINUTE), (200, OK)])
        h.generate()
        runs.append(h.sleeps)
    assert runs[0] == runs[1]


@pytest.mark.parametrize("body, error", [(PER_DAY, DailyLimitExhausted), (LIMIT_ZERO, FreeTierUnavailable)])
def test_a_daily_or_zero_limit_stops_at_once_and_never_retries(tmp_path, body, error):
    h = Harness(tmp_path, [(429, body), (200, OK)])
    with pytest.raises(error):
        h.generate()
    assert len(h.requests) == 1 and h.sleeps == [] and len(h.script) == 1  # the 200 behind it was never asked for


def test_a_day_limit_wins_when_the_response_lists_both_a_minute_and_a_day_violation(tmp_path):
    both = quota_body("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "5s")
    both["error"]["details"][0]["violations"].append({"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"})
    assert classify_429(both) == ("daily", None)
    h = Harness(tmp_path, [(429, both), (200, OK)])
    with pytest.raises(DailyLimitExhausted):
        h.generate()
    assert len(h.requests) == 1


def test_an_unrecognised_429_is_retried_a_bounded_number_of_times_then_stops(tmp_path):
    h = Harness(tmp_path, [(429, UNKNOWN_429)] * 10, max_unknown_429_retries=2)
    with pytest.raises(RetriesExhausted):
        h.generate()
    assert len(h.requests) == 3 and len(h.sleeps) == 2  # one try + two retries, then it stops


def test_a_persistent_per_minute_429_gives_up_after_the_attempt_limit(tmp_path):
    h = Harness(tmp_path, [(429, PER_MINUTE)] * 10, max_attempts=4)
    with pytest.raises(RetriesExhausted):
        h.generate()
    assert len(h.requests) == 4


def test_server_errors_are_retried_with_backoff_then_succeed(tmp_path):
    h = Harness(tmp_path, [(503, {"error": {"code": 503, "message": "overloaded"}}), (500, {"error": {}}), (200, OK)])
    assert h.generate().http_attempts == 3 and len(h.sleeps) == 2


def test_a_network_error_is_retried_and_its_message_never_carries_the_key_or_the_url(tmp_path):
    h = Harness(tmp_path, [httpx.ConnectError(f"cannot connect to https://x/?key={SECRET}")] * 10, max_attempts=3)
    with pytest.raises(RetriesExhausted) as caught:
        h.generate()
    assert SECRET not in str(caught.value) and "https://" not in str(caught.value) and len(h.requests) == 3


def test_a_client_error_is_not_retried_and_the_error_message_is_scrubbed_of_the_key(tmp_path):
    h = Harness(tmp_path, [(400, {"error": {"code": 400, "message": f"API key {SECRET} not valid", "status": "INVALID_ARGUMENT"}})])
    with pytest.raises(ApiError) as caught:
        h.generate()
    assert caught.value.status == 400 and SECRET not in str(caught.value) and len(h.requests) == 1
    assert "***" in str(caught.value)


def test_the_per_run_cap_counts_attempts_and_refuses_before_sending(tmp_path):
    h = Harness(tmp_path, [(200, OK)] * 5, max_live=2)
    h.generate()
    h.generate()
    with pytest.raises(CallCapExceeded, match="2"):
        h.generate()
    assert len(h.requests) == 2 and h.budget.used() == 2


def test_the_cap_also_stops_a_retry_loop(tmp_path):
    h = Harness(tmp_path, [(429, PER_MINUTE)] * 10, max_live=2)
    with pytest.raises(CallCapExceeded):
        h.generate()
    assert len(h.requests) == 2


def test_the_daily_ceiling_refuses_before_sending_anything(tmp_path):
    h = Harness(tmp_path, [(200, OK)], daily_limit=0)
    with pytest.raises(DailyBudgetReached):
        h.generate()
    assert h.requests == [] and h.limiter_calls == []


def test_the_token_estimate_grows_with_the_request_and_includes_the_output_allowance():
    small = estimate_tokens("s", "u", {"a": 1}, 256)
    assert estimate_tokens("s" * 3000, "u", {"a": 1}, 256) > small >= 256


def test_the_client_never_shows_its_key(tmp_path):
    h = Harness(tmp_path, [])
    assert SECRET not in repr(h.client) and SECRET not in str(h.client.__dict__.get("model", ""))


@pytest.mark.parametrize("body, expected", [
    (PER_MINUTE, ("per_minute", 3.0)),
    (PER_DAY, ("daily", None)),
    (LIMIT_ZERO, ("zero_limit", None)),
    (UNKNOWN_429, ("unknown", None)),
    (quota_body("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "52.3s"), ("per_minute", 52.3)),
    ({"error": {"message": "Please retry in 41.2s. You exceeded the per minute limit."}}, ("unknown", None)),
    ("not json at all", ("unknown", None)),
])
def test_429_classification(body, expected):
    assert classify_429(body) == expected
