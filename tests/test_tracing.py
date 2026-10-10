"""Request tracing core (app/tracing.py): one JSON line per request in logs/traces.jsonl, exact schema, no secrets.

The exception mapping is tested with the REAL exception classes of the LLM layer, the Saleor read-only client and
qdrant-client, not with stand-ins.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import httpx
import pytest
from qdrant_client.http.exceptions import ResponseHandlingException

from app.llm.errors import (
    ApiError,
    BadResponse,
    CacheCorrupt,
    CacheMiss,
    CallCapExceeded,
    DailyBudgetReached,
    DailyLimitExhausted,
    FreeTierUnavailable,
    LLMError,
    RetriesExhausted,
)
from app.saleor.readonly_client import InvalidVariablesError, ReadOnlyViolation, SaleorQueryError, UnknownOperationError
from app.tracing import (
    ERROR_TYPES,
    STAGES,
    Tracer,
    classify_exception,
    current_trace,
    redact,
    span,
    validate_trace_line,
)

SECRET = "AIzaSyFAKE-SECRET-KEY-0123456789abcdefgh"


class Ticker:
    """A clock that moves 10 ms at every reading, so span timings are exact."""

    def __init__(self, step=0.010, origin=5000.0):
        self.step = step
        self.t = origin - step  # the first reading is `origin`, NOT 0: a span time must be relative to the request start

    def __call__(self):
        self.t += self.step
        return self.t


def _tracer(tmp_path, **kwargs):
    kwargs.setdefault("now", lambda: dt.datetime(2026, 10, 10, 12, 30, 45, 123456, tzinfo=dt.timezone.utc))
    return Tracer(tmp_path / "logs" / "traces.jsonl", **kwargs)


def _lines(tracer):
    return [json.loads(line) for line in tracer.path.read_text(encoding="utf-8").splitlines()]


# --- schema ------------------------------------------------------------------------------------------------------------


def test_a_request_writes_exactly_one_line_with_exactly_the_agreed_keys(tmp_path):
    tracer = _tracer(tmp_path)
    with tracer.request("shop oi co ao khoac nam khong") as trace:
        with span("router", model="gemini-3.5-flash-lite") as sp:
            sp.set_usage(tokens_in=700, tokens_out=8, cache_hit=False)
        trace.intent = "product_search"
    (line,) = _lines(tracer)
    assert set(line) == {"trace_id", "ts", "question", "intent", "status", "spans", "error"}
    assert set(line["spans"][0]) == {"stage", "start_ms", "duration_ms", "model", "tokens_in", "tokens_out", "cache_hit", "error"}
    assert line["question"] == "shop oi co ao khoac nam khong" and line["intent"] == "product_search"
    assert line["status"] == "ok" and line["error"] is None and validate_trace_line(line) == []
    assert re.fullmatch(r"[0-9a-f]{32}", line["trace_id"]) and line["ts"] == "2026-10-10T12:30:45.123Z"  # ISO UTC, milliseconds
    assert line["spans"][0] | {"start_ms": 0, "duration_ms": 0} == {
        "stage": "router", "start_ms": 0, "duration_ms": 0, "model": "gemini-3.5-flash-lite", "tokens_in": 700,
        "tokens_out": 8, "cache_hit": False, "error": None}


def test_the_file_gets_one_line_per_request_across_requests_and_tracer_instances(tmp_path):
    tracer = _tracer(tmp_path)
    for question in ("cau 1", "cau 2"):
        with tracer.request(question):
            pass
    with _tracer(tmp_path).request("cau 3"):  # a new Tracer on the same file appends
        pass
    lines = _lines(tracer)
    assert [line["question"] for line in lines] == ["cau 1", "cau 2", "cau 3"]
    assert len({line["trace_id"] for line in lines}) == 3 and all(validate_trace_line(line) == [] for line in lines)
    assert tracer.path.read_text(encoding="utf-8").count("\n") == 3  # JSON Lines: no blank or partial line


def test_the_log_directory_is_created_and_a_span_outside_a_request_records_nothing(tmp_path):
    tracer = _tracer(tmp_path)
    assert not tracer.path.exists()
    with span("router", model="m") as sp:  # no request is active: a no-op, not an error
        sp.set_usage(tokens_in=1, tokens_out=1, cache_hit=True)
    assert current_trace() is None and not tracer.path.exists()
    with tracer.request("q"):
        assert current_trace() is not None
    assert current_trace() is None and tracer.path.exists()


def test_span_times_are_milliseconds_since_the_start_of_the_request(tmp_path):
    tracer = _tracer(tmp_path, clock=Ticker(0.010))
    with tracer.request("q"):  # reading 1: t0
        with span("router"):  # readings 2 (start) and 3 (end)
            pass
        with span("retrieval"):  # readings 4 and 5
            pass
    a, b = _lines(tracer)[0]["spans"]
    assert (a["start_ms"], a["duration_ms"]) == (10.0, 10.0) and (b["start_ms"], b["duration_ms"]) == (30.0, 10.0)


# --- status ------------------------------------------------------------------------------------------------------------


def test_status_is_ok_without_errors_warn_with_a_span_error_and_error_with_a_request_error(tmp_path):
    tracer = _tracer(tmp_path)
    with tracer.request("ok"):
        with span("router"):
            pass
    with tracer.request("warn"):
        with span("extractor") as sp:
            sp.fail("filter_out_of_vocab", "dropped: kind")
    with tracer.request("error") as trace:
        trace.fail("llm_invalid_output", "not json")
    with tracer.request("error too") as trace:
        with span("router") as sp:
            sp.fail("llm_invalid_output", "x")
        trace.fail("llm_invalid_output", "x")
    statuses = [(line["status"], line["error"] is not None, any(s["error"] for s in line["spans"])) for line in _lines(tracer)]
    assert statuses == [("ok", False, False), ("warn", False, True), ("error", True, False), ("error", True, True)]
    assert all(validate_trace_line(line) == [] for line in _lines(tracer))


def test_an_exception_leaving_a_request_is_traced_and_re_raised_unchanged(tmp_path):
    tracer = _tracer(tmp_path)
    boom = DailyLimitExhausted("daily quota used up")
    with pytest.raises(DailyLimitExhausted) as caught:
        with tracer.request("q"):
            with span("router", model="gemini-3.5-flash-lite"):
                raise boom
    assert caught.value is boom  # tracing never swallows or wraps
    (line,) = _lines(tracer)
    assert line["status"] == "error" and line["error"]["type"] == "llm_429_daily"
    assert line["spans"][0]["error"]["type"] == "llm_429_daily" and "daily quota used up" in line["error"]["message"]
    assert validate_trace_line(line) == []


def test_a_failing_log_write_never_breaks_the_request(tmp_path, capsys):
    path = tmp_path / "logs" / "traces.jsonl"
    path.mkdir(parents=True)  # a directory where the file should be: every write fails
    tracer = Tracer(path)
    with tracer.request("q") as trace:
        trace.intent = "smalltalk"
    assert "trace write failed" in capsys.readouterr().err and path.is_dir()


# --- exception -> error.type ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("exc, stage, expected", [
    # the API's own daily 429 (and only that)
    (DailyLimitExhausted("x"), "router", "llm_429_daily"),
    # our own ceilings, nothing was sent
    (DailyBudgetReached("x"), "extractor", "budget_cap_reached"),
    (CallCapExceeded("x"), "router", "budget_cap_reached"),
    # a 429 that never recovered is an error of its own, never llm_429_minute
    (RetriesExhausted("x", cause="rate_limit"), "router", "llm_rate_limited"),
    # 5xx / network after the retries
    (RetriesExhausted("x", cause="server_error"), "router", "llm_unavailable"),
    (RetriesExhausted("x", cause="network"), "extractor", "llm_unavailable"),
    # 4xx from the API (a limit of 0 is the API refusing this key for this model)
    (ApiError(400, "x"), "router", "llm_request_error"),
    (ApiError(403, "x"), "router", "llm_request_error"),
    (ApiError(404, "x"), "extractor", "llm_request_error"),
    (FreeTierUnavailable("x"), "router", "llm_request_error"),
    (CacheMiss("x"), "router", "llm_cache_miss_offline"),
    (BadResponse("x"), "extractor", "llm_invalid_output"),
    (ResponseHandlingException(httpx.ConnectError("refused")), "retrieval", "qdrant_unreachable"),
    (ResponseHandlingException(httpx.ReadTimeout("slow")), "retrieval", "qdrant_unreachable"),
    (httpx.ConnectError("refused"), "retrieval", "qdrant_unreachable"),
    (httpx.ReadTimeout("slow"), "retrieval", "qdrant_unreachable"),
    (httpx.ReadTimeout("slow"), "order_lookup", "saleor_timeout"),
    (httpx.ReadTimeout("slow"), "ticket_confirm", "saleor_timeout"),
    (httpx.ConnectError("refused"), "order_lookup", "saleor_error"),
    (ReadOnlyViolation("operation type 'mutation' is not allowed"), "order_lookup", "readonly_violation"),
    (UnknownOperationError("not in the allowlist"), "order_lookup", "readonly_violation"),
    (InvalidVariablesError("bad variables"), "ticket_confirm", "readonly_violation"),
    (ValueError("a bug"), "retrieval", "internal_error"),
    (KeyError("a bug"), "router", "internal_error"),
])
def test_exceptions_map_to_their_own_error_type(exc, stage, expected):
    kind, message = classify_exception(exc, stage)
    assert kind == expected and kind in ERROR_TYPES and type(exc).__name__ in message


def _saleor_error(cause):
    error = SaleorQueryError("OrderByNumber: cannot reach Saleor")
    error.__cause__ = cause
    return error


@pytest.mark.parametrize("exc, expected", [
    (_saleor_error(httpx.ReadTimeout("slow")), "saleor_timeout"),
    (_saleor_error(httpx.ConnectTimeout("slow")), "saleor_timeout"),
    (_saleor_error(httpx.PoolTimeout("slow")), "saleor_timeout"),
    (_saleor_error(httpx.ConnectError("refused")), "saleor_error"),  # unreachable is NOT a timeout
    (_saleor_error(httpx.RemoteProtocolError("closed")), "saleor_error"),
    (SaleorQueryError("OrderByNumber: HTTP 500"), "saleor_error"),
    (SaleorQueryError("OrderByNumber: Cannot query field timeout on Order"), "saleor_error"),  # the word is not the cause
])
def test_only_a_real_timeout_is_saleor_timeout_any_other_saleor_failure_is_saleor_error(exc, expected):
    assert classify_exception(exc, "order_lookup")[0] == expected


@pytest.mark.parametrize("exc, expected", [
    (ResponseHandlingException(httpx.ConnectError("refused")), "qdrant_unreachable"),
    (ResponseHandlingException(ValueError("the answer does not parse")), "internal_error"),  # reachable, but not understood
])
def test_a_qdrant_failure_is_unreachable_only_when_the_transport_failed(exc, expected):
    assert classify_exception(exc, "retrieval")[0] == expected


def test_an_api_error_that_is_not_a_4xx_is_not_called_a_request_error():
    assert classify_exception(ApiError(302, "moved"), "router")[0] == "internal_error"
    assert classify_exception(ApiError(429, "x"), "router")[0] == "llm_request_error"  # a 429 that reached ApiError is a 4xx


def test_an_unclassified_llm_failure_is_an_internal_error_not_a_made_up_llm_cause():
    assert classify_exception(LLMError("x"), "router")[0] == "internal_error"
    assert classify_exception(CacheCorrupt("x"), "router")[0] == "internal_error"  # a local file problem, not the model's output
    assert classify_exception(RetriesExhausted("x"), "router")[0] == "internal_error"  # no cause recorded: say nothing about one


def test_no_exception_ever_maps_to_llm_429_minute():
    every = [DailyLimitExhausted("x"), FreeTierUnavailable("x"), DailyBudgetReached("x"), CallCapExceeded("x"), BadResponse("x"),
             CacheMiss("x"), CacheCorrupt("x"), LLMError("x"), ApiError(429, "x"), ApiError(400, "x"),
             *(RetriesExhausted("x", cause=c) for c in ("rate_limit", "server_error", "network", None)),
             ResponseHandlingException(httpx.ConnectError("x")), httpx.ConnectError("x"), httpx.ReadTimeout("x"),
             SaleorQueryError("x"), ReadOnlyViolation("x"), ValueError("x")]
    for exc in every:
        for stage in (*STAGES, None):
            assert classify_exception(exc, stage)[0] != "llm_429_minute", (exc, stage)


def test_llm_429_minute_is_a_recovered_warning_and_cannot_be_set_as_a_failure(tmp_path):
    tracer = _tracer(tmp_path)
    with tracer.request("q") as trace:
        with span("router") as sp:
            with pytest.raises(ValueError):
                sp.fail("llm_429_minute", "x")
        with pytest.raises(ValueError):
            trace.fail("llm_429_minute", "x")


def test_the_validator_rejects_llm_429_minute_as_the_error_of_a_request():
    line = _good_line()
    line["spans"][0]["error"] = {"type": "llm_429_minute", "message": "recovered after 1 retry"}
    line["status"] = "warn"
    assert validate_trace_line(line) == []  # as the warning of a span that then succeeded: valid
    line["error"], line["status"] = {"type": "llm_429_minute", "message": "x"}, "error"
    assert validate_trace_line(line)  # as the reason a request FAILED: never


def test_the_error_vocabulary_is_the_agreed_one_plus_what_a_trace_needs_to_tell_failures_apart():
    assert set(ERROR_TYPES) == {
        # agreed first
        "llm_429_minute", "llm_429_daily", "llm_invalid_output", "filter_out_of_vocab", "qdrant_unreachable", "saleor_timeout",
        "readonly_violation",
        # added by the owner when the nearest-match mapping was rejected
        "llm_unavailable", "llm_request_error", "llm_cache_miss_offline", "budget_cap_reached", "saleor_error",
        # added so that a 429 that never recovered is not called llm_429_minute, and for bugs
        "llm_rate_limited", "internal_error"}
    assert STAGES == ("router", "extractor", "retrieval", "order_lookup", "ticket_confirm", "reply", "answer")


# --- secrets and personal data -------------------------------------------------------------------------------------------


def test_emails_are_masked_in_the_question_and_everywhere_else(tmp_path):
    tracer = _tracer(tmp_path)
    with pytest.raises(ApiError):
        with tracer.request("don 113 cua khachhang02@example.com va Nguyen.Van+tag@mail.co.vn toi dau roi"):
            with span("router") as sp:
                sp.fail("llm_invalid_output", "echo of khachhang02@example.com")
                raise ApiError(400, "bad request for khachhang02@example.com")
    text = tracer.path.read_text(encoding="utf-8")
    assert "@" not in text and "khachhang02" not in text and "example.com" not in text and "Nguyen.Van" not in text
    assert _lines(tracer)[0]["question"] == "don 113 cua [email] va [email] toi dau roi"


def test_keys_and_tokens_never_reach_the_file_even_when_pasted_or_echoed(tmp_path):
    saleor_token = "sltok-0123456789abcdef"
    tracer = _tracer(tmp_path, secrets=[saleor_token])
    question = f"my key is {SECRET} and Bearer abc.DEF-123_xyz and token={saleor_token} and api_key: hunter2hunter2"
    with pytest.raises(ApiError):
        with tracer.request(question):
            with span("router"):
                raise ApiError(403, f"Authorization: Bearer abc.DEF-123_xyz rejected key {SECRET}")
    text = tracer.path.read_text(encoding="utf-8")
    for secret in (SECRET, "abc.DEF-123_xyz", saleor_token, "hunter2hunter2"):
        assert secret not in text, secret
    assert "[secret]" in text and validate_trace_line(_lines(tracer)[0]) == []


def test_configured_secrets_are_read_from_the_environment_without_being_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-only-key-0123456789")
    monkeypatch.setenv("SALEOR_RUNTIME_TOKEN", "env-only-token-abcdef")
    monkeypatch.setenv("SHOP_NAME", "ShopMate")  # not a secret
    tracer = _tracer(tmp_path)
    with tracer.request("dung env-only-key-0123456789 va env-only-token-abcdef cho ShopMate"):
        pass
    assert _lines(tracer)[0]["question"] == "dung [secret] va [secret] cho ShopMate"


@pytest.mark.parametrize("text, expected", [
    ("lien he khachhang03@example.com nhe", "lien he [email] nhe"),
    ("khong co gi", "khong co gi"),
    ("gia 400k, size 37, don 113", "gia 400k, size 37, don 113"),  # ordinary content is untouched
    ("@ShopMate oi", "@ShopMate oi"),  # an @ that is not an email
])
def test_redaction_leaves_ordinary_text_alone(text, expected):
    assert redact(text) == expected


# --- the validator itself ------------------------------------------------------------------------------------------------


def _good_line():
    return {"trace_id": "a" * 32, "ts": "2026-10-10T12:30:45.123Z", "question": "q", "intent": "smalltalk", "status": "ok",
            "spans": [{"stage": "router", "start_ms": 0.0, "duration_ms": 1.5, "model": "m", "tokens_in": 1, "tokens_out": 1,
                       "cache_hit": False, "error": None}], "error": None}


def test_the_validator_accepts_a_good_line_and_rejects_each_kind_of_deviation():
    assert validate_trace_line(_good_line()) == []

    def mutate(fn):
        line = _good_line()
        fn(line)
        return validate_trace_line(line)

    assert mutate(lambda l: l.pop("intent"))
    assert mutate(lambda l: l.update(extra=1))
    assert mutate(lambda l: l["spans"][0].update(extra=1))
    assert mutate(lambda l: l["spans"][0].pop("cache_hit"))
    assert mutate(lambda l: l.update(trace_id="short"))
    assert mutate(lambda l: l.update(ts="2026-10-10 12:30:45"))
    assert mutate(lambda l: l.update(intent="refund"))
    assert mutate(lambda l: l.update(status="fine"))
    assert mutate(lambda l: l["spans"][0].update(stage="guess"))
    assert mutate(lambda l: l["spans"][0].update(tokens_in=-1))
    assert mutate(lambda l: l["spans"][0].update(tokens_in=1.5))
    assert mutate(lambda l: l["spans"][0].update(cache_hit="yes"))
    assert mutate(lambda l: l["spans"][0].update(duration_ms=-1))
    assert mutate(lambda l: l.update(error={"type": "boom", "message": "x"}, status="error"))
    assert mutate(lambda l: l.update(error={"type": "llm_invalid_output"}, status="error"))
    # status must agree with the errors
    assert mutate(lambda l: l.update(status="error"))
    assert mutate(lambda l: l.update(status="warn"))
    assert mutate(lambda l: l["spans"][0].update(error={"type": "llm_invalid_output", "message": "x"}))
    assert mutate(lambda l: l.update(error={"type": "llm_invalid_output", "message": "x"}, status="ok"))
    assert validate_trace_line("not a dict")
