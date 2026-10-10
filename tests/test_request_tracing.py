"""Tracing the request path router -> extractor -> retrieval (app/pipeline.handle_question) with the fake client.

LLM answers come from a scripted stub of the client; the failure paths run through the REAL GeminiClient over a fake
HTTP transport, the REAL Qdrant client pointed at a closed port, and the REAL read-only Saleor client, so the
error.type of each failure comes from the exceptions those layers really raise.
"""

from __future__ import annotations

import json

import httpx
import pytest
from qdrant_client import QdrantClient

from app.llm.cache import DiskCache
from app.llm.client import LiveResponse
from app.llm.errors import CacheMiss, CallCapExceeded, DailyBudgetReached, DailyLimitExhausted, RetriesExhausted
from app.llm.extractor import FilterExtractor
from app.llm.generator import CachedGenerator
from app.llm.intents import INTENTS
from app.llm.prompts import load_prompt
from app.llm.router import GeminiRouter
from app.pipeline import handle_question
from app.retrieval.search import Retriever
from app.saleor.readonly_client import ReadOnlySaleorClient
from app.tracing import Tracer, span, validate_trace_line
from tests.fake_embedder import FakeEmbedder
from tests.test_llm_client import LIMIT_ZERO, PER_DAY, PER_MINUTE, UNKNOWN_429, Harness
from tests.test_retrieval_folded import _documents, _index, _new_collection

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")

NULLS = {"category": None, "kind": None, "color": None, "material": None, "size": None, "price_min": None,
         "price_max": None, "in_stock": None}
EMAIL = "khachhang02@example.com"


def _raw(payload, usage=None):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    raw = {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}], "modelVersion": "gemini-3.5-flash-lite"}
    if usage is not False:
        raw["usageMetadata"] = usage or {"promptTokenCount": 700, "candidatesTokenCount": 8, "totalTokenCount": 708}
    return raw


class ScriptedLLM:
    """Stands in for GeminiClient.generate: answers the router and the extractor from tables keyed by question."""

    def __init__(self, routes, filters=None, router_usage=None, filters_usage=None):
        self.routes, self.filters, self.calls = routes, filters or {}, []
        self.router_usage, self.filters_usage = router_usage, filters_usage

    def generate(self, *, system_instruction, user_text, schema, generation_config):
        self.calls.append(user_text)
        if "intent" in schema["properties"]:
            answer = self.routes[user_text]
            payload = {"intent": answer} if answer in INTENTS else answer  # anything else is returned as raw (unusable) text
            return LiveResponse(_raw(payload, self.router_usage), 640.0, 1)
        return LiveResponse(_raw({**NULLS, **self.filters.get(user_text, {})},
                                 self.filters_usage or {"promptTokenCount": 1900, "candidatesTokenCount": 40, "totalTokenCount": 1940}),
                            900.0, 1)


def _stack(tmp_path, llm, retriever=None, name="a"):
    generator = CachedGenerator(DiskCache(tmp_path / f"cache-{name}"), llm)
    if retriever is None:
        client = _new_collection()
        _index(client, _documents())
        retriever = Retriever(client, FakeEmbedder(), collection="v2")
    tracer = Tracer(tmp_path / "logs" / "traces.jsonl")
    return (GeminiRouter(generator, load_prompt("router_v1")), FilterExtractor(generator, load_prompt("filters_v1")), retriever, tracer)


def _handle(question, stack, **kwargs):
    router, extractor, retriever, tracer = stack
    return handle_question(question, router=router, extractor=extractor, retriever=retriever, tracer=tracer, **kwargs)


def _lines(stack):
    return [json.loads(x) for x in stack[3].path.read_text(encoding="utf-8").splitlines()]


# --- happy paths ----------------------------------------------------------------------------------------------------------

Q_PRODUCT, Q_POLICY, Q_CHAT = "shop oi co ao thun nam mau den khong", "phi ship bao nhieu vay shop", "chao shop"
ROUTES = {Q_PRODUCT: "product_search", Q_POLICY: "policy_question", Q_CHAT: "smalltalk"}
FILTERS = {Q_PRODUCT: {"kind": "Áo thun nam", "color": "Đen"}}


def test_a_product_question_traces_router_extractor_retrieval_in_order_with_real_token_counts(tmp_path):
    stack = _stack(tmp_path, ScriptedLLM(ROUTES, FILTERS))
    result = _handle(Q_PRODUCT, stack)
    (line,) = _lines(stack)
    assert validate_trace_line(line) == []
    assert [s["stage"] for s in line["spans"]] == ["router", "extractor", "retrieval"]
    assert line["intent"] == "product_search" and line["status"] == "ok" and line["error"] is None
    router_span, extractor_span, retrieval_span = line["spans"]
    assert (router_span["model"], router_span["tokens_in"], router_span["tokens_out"], router_span["cache_hit"]) == ("gemini-3.5-flash-lite", 700, 8, False)
    assert (extractor_span["tokens_in"], extractor_span["tokens_out"]) == (1900, 40)  # the usageMetadata numbers, not estimates
    assert retrieval_span["model"] == "fake/bag-of-words" and retrieval_span["tokens_in"] is None and retrieval_span["cache_hit"] is None
    assert result.intent == "product_search" and result.filters["doc_type"] == "product" and result.filters["kind"] == "Áo thun nam"
    assert result.hits and all(h.payload["doc_type"] == "product" for h in result.hits)
    starts = [s["start_ms"] for s in line["spans"]]
    assert starts == sorted(starts) and all(s["duration_ms"] >= 0 for s in line["spans"])


def test_a_policy_question_skips_the_extractor_and_a_greeting_skips_retrieval_too(tmp_path):
    stack = _stack(tmp_path, ScriptedLLM(ROUTES, FILTERS))
    policy, chat = _handle(Q_POLICY, stack), _handle(Q_CHAT, stack)
    first, second = _lines(stack)
    assert [s["stage"] for s in first["spans"]] == ["router", "retrieval"] and first["intent"] == "policy_question"
    assert policy.filters == {"doc_type": "policy"} and all(h.payload["doc_type"] == "policy" for h in policy.hits)
    assert [s["stage"] for s in second["spans"]] == ["router"] and second["intent"] == "smalltalk" and chat.hits == []
    assert first["status"] == second["status"] == "ok"


def test_exactly_one_line_per_request_and_a_repeated_question_is_a_cache_hit(tmp_path):
    stack = _stack(tmp_path, ScriptedLLM(ROUTES, FILTERS))
    for question in (Q_PRODUCT, Q_POLICY, Q_CHAT, Q_PRODUCT):
        _handle(question, stack)
    lines = _lines(stack)
    assert len(lines) == 4 and len({l["trace_id"] for l in lines}) == 4 and all(validate_trace_line(l) == [] for l in lines)
    again = lines[3]["spans"]
    assert [s["cache_hit"] for s in again[:2]] == [True, True] and (again[0]["tokens_in"], again[0]["tokens_out"]) == (700, 8)
    assert again[2]["cache_hit"] is None  # retrieval has no cache


def test_tokens_are_null_when_the_response_has_no_usage_metadata_and_include_thought_tokens_when_it_has(tmp_path):
    no_usage = _stack(tmp_path, ScriptedLLM(ROUTES, router_usage=False), name="n")
    _handle(Q_CHAT, no_usage)
    span_ = _lines(no_usage)[0]["spans"][0]
    assert (span_["tokens_in"], span_["tokens_out"]) == (None, None)  # never filled in with an estimate
    thoughtful = _stack(tmp_path / "t", ScriptedLLM(ROUTES, router_usage={"promptTokenCount": 100, "candidatesTokenCount": 8,
                                                                         "thoughtsTokenCount": 30, "totalTokenCount": 138}))
    _handle(Q_CHAT, thoughtful)
    span_ = _lines(thoughtful)[0]["spans"][0]
    assert (span_["tokens_in"], span_["tokens_out"]) == (100, 38)  # billed output = candidates + thoughts


def test_without_a_tracer_the_request_is_handled_the_same_and_nothing_is_written(tmp_path):
    router, extractor, retriever, tracer = _stack(tmp_path, ScriptedLLM(ROUTES, FILTERS))
    traced = handle_question(Q_PRODUCT, router=router, extractor=extractor, retriever=retriever, tracer=tracer)
    plain = handle_question(Q_PRODUCT, router=router, extractor=extractor, retriever=retriever)
    assert [h.key for h in plain.hits] == [h.key for h in traced.hits] and plain.filters == traced.filters
    assert len(tracer.path.read_text(encoding="utf-8").splitlines()) == 1  # only the traced request wrote


def test_the_components_alone_write_nothing_without_an_open_request(tmp_path):
    router, extractor, retriever, tracer = _stack(tmp_path, ScriptedLLM(ROUTES, FILTERS))
    router.route(Q_PRODUCT), extractor.extract(Q_PRODUCT), retriever.search(Q_PRODUCT, k=3)
    assert not tracer.path.exists()


# --- failure paths: the right error.type ----------------------------------------------------------------------------------------


def test_an_unusable_router_answer_is_llm_invalid_output_and_stops_the_request_without_raising(tmp_path):
    stack = _stack(tmp_path, ScriptedLLM({Q_PRODUCT: "this is not json"}))
    result = _handle(Q_PRODUCT, stack)
    (line,) = _lines(stack)
    assert result.intent is None and result.hits == [] and result.error
    assert line["status"] == "error" and line["error"]["type"] == "llm_invalid_output" and line["intent"] is None
    assert [s["stage"] for s in line["spans"]] == ["router"] and line["spans"][0]["error"]["type"] == "llm_invalid_output"
    assert validate_trace_line(line) == []


def test_a_value_outside_the_vocabulary_is_filter_out_of_vocab_and_the_request_goes_on_as_a_warning(tmp_path):
    stack = _stack(tmp_path, ScriptedLLM(ROUTES, {Q_PRODUCT: {"kind": "Áo gió", "color": "Đen"}}))
    result = _handle(Q_PRODUCT, stack)
    (line,) = _lines(stack)
    assert line["status"] == "warn" and line["error"] is None
    extractor_span = line["spans"][1]
    assert extractor_span["stage"] == "extractor" and extractor_span["error"]["type"] == "filter_out_of_vocab"
    assert "kind" in extractor_span["error"]["message"] and "Áo gió" not in extractor_span["error"]["message"]  # field names only
    assert line["spans"][2]["stage"] == "retrieval" and line["spans"][2]["error"] is None
    assert result.filters == {"doc_type": "product", "color": "Đen"} and validate_trace_line(line) == []


ROUTER_OK = (200, _raw({"intent": "smalltalk"}))
SERVER_ERROR = (503, {"error": {"code": 503, "message": "overloaded"}})


def _llm_stack(tmp_path, script, **kwargs):
    """A real GeminiClient over a scripted transport, wired into the pipeline."""
    harness = Harness(tmp_path / "h", script, **kwargs)
    return harness, _stack(tmp_path, harness.client)


def _fails_with(tmp_path, script, error, question=Q_CHAT, **kwargs):
    """Run one request against the scripted API; the failure must propagate; return (harness, stack, trace line)."""
    harness, stack = _llm_stack(tmp_path, script, **kwargs)
    with pytest.raises(error):
        _handle(question, stack)
    (line,) = _lines(stack)
    assert validate_trace_line(line) == []
    return harness, stack, line


def _types(line):
    return line["error"]["type"], [s["error"]["type"] for s in line["spans"] if s["error"]]


# llm_429_minute: ONLY a per-minute 429 that was retried and then succeeded (a warning, never a failure)


def test_a_per_minute_429_that_was_retried_and_then_succeeded_is_llm_429_minute_as_a_warning(tmp_path):
    harness, stack = _llm_stack(tmp_path, [(429, PER_MINUTE), (429, PER_MINUTE), ROUTER_OK])
    result = _handle(Q_CHAT, stack)
    (line,) = _lines(stack)
    assert result.intent == "smalltalk" and len(harness.requests) == 3
    assert line["status"] == "warn" and line["error"] is None and validate_trace_line(line) == []
    (router_span,) = line["spans"]
    assert router_span["error"]["type"] == "llm_429_minute" and "2" in router_span["error"]["message"]
    assert (router_span["tokens_in"], router_span["tokens_out"], router_span["cache_hit"]) == (700, 8, False)  # the call did succeed
    _handle(Q_CHAT, stack)  # the same question again: a cache hit, no call, no 429 to report
    assert _lines(stack)[1]["status"] == "ok" and _lines(stack)[1]["spans"][0]["error"] is None


@pytest.mark.parametrize("retried", [[SERVER_ERROR], [httpx.ConnectError("down")], [(429, UNKNOWN_429)]])
def test_a_retry_that_was_not_a_recognised_per_minute_429_is_not_labelled_as_one(tmp_path, retried):
    harness, stack = _llm_stack(tmp_path, [*retried, ROUTER_OK])
    _handle(Q_CHAT, stack)
    (line,) = _lines(stack)
    assert len(harness.requests) == 2 and line["status"] == "ok" and line["spans"][0]["error"] is None


def test_a_recovered_429_followed_by_an_unusable_answer_is_traced_as_the_unusable_answer(tmp_path):
    _, stack = _llm_stack(tmp_path, [(429, PER_MINUTE), (200, _raw("not json at all"))])
    _handle(Q_CHAT, stack)
    (line,) = _lines(stack)
    assert _types(line) == ("llm_invalid_output", ["llm_invalid_output"]) and line["status"] == "error"


def test_a_recovered_429_followed_by_a_dead_qdrant_keeps_the_warning_on_the_llm_span_and_fails_on_qdrant(tmp_path):
    harness = Harness(tmp_path / "h", [(429, PER_MINUTE), (200, _raw({"intent": "policy_question"}))])
    dead = Retriever(QdrantClient(url="http://127.0.0.1:9", timeout=1, check_compatibility=False), FakeEmbedder(), collection="v2")
    stack = _stack(tmp_path, harness.client, retriever=dead)
    with pytest.raises(Exception):
        _handle(Q_POLICY, stack)
    (line,) = _lines(stack)
    assert [(s["stage"], (s["error"] or {}).get("type")) for s in line["spans"]] == [("router", "llm_429_minute"), ("retrieval", "qdrant_unreachable")]
    assert line["status"] == "error" and line["error"]["type"] == "qdrant_unreachable" and validate_trace_line(line) == []


# a 429 whose retries are all used up is an ERROR, and is never called llm_429_minute


@pytest.mark.parametrize("script, kwargs", [([(429, PER_MINUTE)] * 5, {"max_attempts": 2}),
                                            ([(429, PER_MINUTE)] * 5, {"max_attempts": 5}),
                                            ([(429, UNKNOWN_429)] * 5, {"max_unknown_429_retries": 1})])
def test_a_429_whose_retries_are_all_exhausted_is_an_error_never_llm_429_minute(tmp_path, script, kwargs):
    harness, stack, line = _fails_with(tmp_path, script, RetriesExhausted, **kwargs)
    assert line["status"] == "error"
    assert _types(line) == ("llm_rate_limited", ["llm_rate_limited"])
    assert "llm_429_minute" not in stack[3].path.read_text(encoding="utf-8")  # nowhere in the written line
    assert len(harness.requests) >= 2 and "429" in line["error"]["message"]


def test_a_per_minute_429_that_recovers_and_a_per_minute_429_that_does_not_end_differently(tmp_path):
    _, recovered = _llm_stack(tmp_path / "a", [(429, PER_MINUTE), ROUTER_OK])
    _handle(Q_CHAT, recovered)
    _, _, failed = _fails_with(tmp_path / "b", [(429, PER_MINUTE)] * 3, RetriesExhausted, max_attempts=2)
    assert (_lines(recovered)[0]["status"], failed["status"]) == ("warn", "error")


# llm_unavailable: 5xx or a network failure after the retries


@pytest.mark.parametrize("script", [[SERVER_ERROR] * 5, [(500, {"error": {}})] * 5, [httpx.ConnectError("down")] * 5,
                                    [httpx.ReadTimeout("slow")] * 5, [(429, PER_MINUTE), SERVER_ERROR, SERVER_ERROR]])
def test_a_5xx_or_a_network_failure_after_the_retries_is_llm_unavailable(tmp_path, script):
    harness, stack, line = _fails_with(tmp_path, script, RetriesExhausted, max_attempts=3)
    assert line["status"] == "error" and _types(line) == ("llm_unavailable", ["llm_unavailable"])
    assert "llm_429_minute" not in stack[3].path.read_text(encoding="utf-8") and len(harness.requests) == 3


# llm_request_error: HTTP 4xx from the API (the real client raises ApiError, or FreeTierUnavailable for a limit of 0)


@pytest.mark.parametrize("script", [[(400, {"error": {"message": "API key not valid", "status": "INVALID_ARGUMENT"}})],
                                    [(403, {"error": {"message": "permission denied"}})], [(404, {"error": {"message": "model not found"}})],
                                    [(429, LIMIT_ZERO)]])
def test_an_http_4xx_from_the_api_is_llm_request_error_and_is_not_retried(tmp_path, script):
    harness, _, line = _fails_with(tmp_path, script, Exception)
    assert _types(line) == ("llm_request_error", ["llm_request_error"]) and len(harness.requests) == 1


# llm_429_daily: Google's real daily 429 only


def test_a_daily_quota_429_from_the_api_is_llm_429_daily(tmp_path):
    harness, _, line = _fails_with(tmp_path, [(429, PER_DAY)] * 2, DailyLimitExhausted)
    assert _types(line) == ("llm_429_daily", ["llm_429_daily"]) and len(harness.requests) == 1


# budget_cap_reached: OUR daily ceiling or per-run cap (nothing, or no further request, was sent)


def test_our_daily_ceiling_is_budget_cap_reached_not_a_429(tmp_path):
    harness, _, line = _fails_with(tmp_path, [ROUTER_OK], DailyBudgetReached, daily_limit=0)
    assert _types(line) == ("budget_cap_reached", ["budget_cap_reached"]) and harness.requests == []


def test_our_per_run_cap_is_budget_cap_reached_not_a_429(tmp_path):
    harness, _, line = _fails_with(tmp_path, [ROUTER_OK], CallCapExceeded, max_live=0)
    assert _types(line) == ("budget_cap_reached", ["budget_cap_reached"]) and harness.requests == []


def test_a_cap_that_stops_a_retry_loop_is_budget_cap_reached_not_llm_429_minute(tmp_path):
    harness, stack, line = _fails_with(tmp_path, [(429, PER_MINUTE), ROUTER_OK], CallCapExceeded, max_live=1)
    assert len(harness.requests) == 1 and _types(line) == ("budget_cap_reached", ["budget_cap_reached"])
    assert "llm_429_minute" not in stack[3].path.read_text(encoding="utf-8")


# llm_cache_miss_offline


def test_a_cache_miss_without_live_calls_is_llm_cache_miss_offline(tmp_path):
    stack = _stack(tmp_path, None)  # a generator with no client: the offline mode of every eval run
    with pytest.raises(CacheMiss):
        _handle(Q_CHAT, stack)
    (line,) = _lines(stack)
    assert _types(line) == ("llm_cache_miss_offline", ["llm_cache_miss_offline"]) and validate_trace_line(line) == []


# qdrant_unreachable


def test_an_unreachable_qdrant_is_qdrant_unreachable_after_the_llm_stages_succeeded(tmp_path):
    dead = Retriever(QdrantClient(url="http://127.0.0.1:9", timeout=1, check_compatibility=False), FakeEmbedder(), collection="v2")
    stack = _stack(tmp_path, ScriptedLLM(ROUTES, FILTERS), retriever=dead)
    with pytest.raises(Exception) as caught:
        _handle(Q_PRODUCT, stack)
    assert type(caught.value).__name__ == "ResponseHandlingException"  # the real qdrant-client failure, re-raised untouched
    (line,) = _lines(stack)
    assert [(s["stage"], bool(s["error"])) for s in line["spans"]] == [("router", False), ("extractor", False), ("retrieval", True)]
    assert line["error"]["type"] == "qdrant_unreachable" and line["spans"][2]["error"]["type"] == "qdrant_unreachable"
    assert line["intent"] == "product_search" and line["status"] == "error" and validate_trace_line(line) == []


# saleor_timeout / saleor_error / readonly_violation: the real read-only client over a fake transport


def _saleor_lookup(tmp_path, handler):
    tracer = Tracer(tmp_path / "logs" / "traces.jsonl")
    with ReadOnlySaleorClient("http://saleor.invalid/graphql/", "t" * 30, transport=httpx.MockTransport(handler)) as saleor:
        with pytest.raises(Exception) as caught:
            with tracer.request("don 113 toi dau roi"):
                with span("order_lookup"):
                    saleor.execute("OrderByNumber", {"number": "113"})
    line = json.loads(tracer.path.read_text(encoding="utf-8"))
    assert type(caught.value).__name__ == "SaleorQueryError" and validate_trace_line(line) == []
    return line


def _raise(exc):
    def handler(request):
        raise type(exc)(str(exc), request=request)
    return handler


def test_a_saleor_timeout_is_saleor_timeout(tmp_path):
    for i, exc in enumerate((httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"))):
        line = _saleor_lookup(tmp_path / str(i), _raise(exc))
        assert _types(line) == ("saleor_timeout", ["saleor_timeout"]) and line["spans"][0]["stage"] == "order_lookup"


@pytest.mark.parametrize("handler", [_raise(httpx.ConnectError("refused")), lambda request: httpx.Response(500, text="oops"),
                                     lambda request: httpx.Response(200, json={"errors": [{"message": "Cannot query field x"}]})])
def test_any_other_saleor_failure_is_saleor_error_not_a_timeout(tmp_path, handler):
    line = _saleor_lookup(tmp_path, handler)
    assert _types(line) == ("saleor_error", ["saleor_error"])


def test_a_read_only_violation_in_the_ticket_confirm_stage(tmp_path):
    tracer = Tracer(tmp_path / "logs" / "traces.jsonl")
    with ReadOnlySaleorClient("http://saleor.invalid/graphql/", "t" * 30, transport=httpx.MockTransport(_raise(httpx.ReadTimeout("x")))) as saleor:
        with pytest.raises(Exception) as refused:
            with tracer.request("huy don 113 giup minh"):
                with span("ticket_confirm"):
                    saleor.execute("CancelOrder", {"id": "x"})  # not in the read-only allowlist
    line = json.loads(tracer.path.read_text(encoding="utf-8"))
    assert type(refused.value).__name__ == "UnknownOperationError"
    assert _types(line) == ("readonly_violation", ["readonly_violation"]) and line["spans"][0]["stage"] == "ticket_confirm"
    assert validate_trace_line(line) == []


# internal_error (not an agreed type: a bug)


def test_an_unexpected_exception_is_traced_as_internal_error_and_re_raised(tmp_path):
    stack = _stack(tmp_path, ScriptedLLM(ROUTES, FILTERS))
    with pytest.raises(ValueError):
        _handle(Q_PRODUCT, stack, k=0)  # the retriever refuses k < 1
    (line,) = _lines(stack)
    assert line["error"]["type"] == "internal_error" and line["spans"][-1]["stage"] == "retrieval" and validate_trace_line(line) == []


# --- secrets and personal data in a real request ------------------------------------------------------------------------------------


def test_no_email_or_secret_reaches_the_file_from_a_real_request_and_failures(tmp_path):
    secret = "AIzaSyFAKE-SECRET-KEY-0123456789abcdefgh"
    question = f"don 113 cua {EMAIL} toi dau roi, key {secret}"
    stack = _stack(tmp_path, ScriptedLLM({question: "garbage with " + EMAIL + " and " + secret}))
    _handle(question, stack)  # unusable answer: the (redacted) request line is written
    failing = _stack(tmp_path / "f", Harness(tmp_path / "h", [(400, {"error": {"message": f"key {secret} and {EMAIL} not valid", "status": "INVALID_ARGUMENT"}})]).client)
    failing[3].path = stack[3].path  # the same log file
    with pytest.raises(Exception):
        _handle(question, failing)
    text = stack[3].path.read_text(encoding="utf-8")
    assert len(text.splitlines()) == 2
    assert EMAIL not in text and "khachhang02" not in text and secret not in text and "example.com" not in text
    assert "[email]" in text and "[secret]" in text
    assert all(validate_trace_line(json.loads(l)) == [] for l in text.splitlines())
