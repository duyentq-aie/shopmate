"""The Langfuse sink (docs/DECISIONS-langfuse.md, L2-L6): a request becomes one root observation with one child per span of the internal trace.
Everything runs against the fake client of tests/fake_langfuse.py; tests/test_observability_real_sdk.py runs the same flow on the real SDK."""

from __future__ import annotations

import pytest

from app.observability.pricing import COST_BASIS
from app.observability.sink import LangfuseSink
from tests.fake_langfuse import FakeClient

FAKE_KEY = "fake-" + "x" * 28
TRACE_ID = "a" * 32
MODEL = "gemini-3.5-flash-lite"
START_NS = 1_700_000_000_000_000_000


def make_sink(client: FakeClient | None = None, **kwargs) -> tuple[LangfuseSink, FakeClient]:
    client = client or FakeClient()
    clock = iter(range(START_NS, START_NS + 10**12, 1_000_000))  # every reading of the clock is one millisecond later
    return LangfuseSink(client, propagate_attributes=client.propagate_attributes, now_ns=lambda: next(clock), **kwargs), client


def span_line(stage: str, *, model=None, start_ms=0.0, duration_ms=10.0, tokens_in=None, tokens_out=None, cache_hit=None, error=None, summary=None) -> dict:
    return {"stage": stage, "start_ms": start_ms, "duration_ms": duration_ms, "model": model, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cache_hit": cache_hit, "error": error, "summary": summary or {}}


def make_line(spans: list[dict], **overrides) -> dict:
    line = {"schema_version": 2, "trace_id": TRACE_ID, "ts": "2026-10-13T01:02:03.456Z", "session_id": "sess-1", "turn": 2, "question": "áo khoác nam xanh rêu",
            "intent": "product_search", "status": "ok", "reply": "Bên mình có Áo khoác dù nam xanh rêu.", "reply_truncated": False, "reply_source": "llm",
            "spans": spans, "error": None}
    return {**line, **overrides}


def run(sink: LangfuseSink, line: dict, *, duration_ms: float = 100.0) -> None:
    """The four hook calls of one request, in the order the Tracer makes them."""
    request = sink.request_started(trace_id=line["trace_id"], session_id=line["session_id"], turn=line["turn"])
    handles = [sink.span_started(request, stage=s["stage"], model=s["model"]) for s in line["spans"]]
    for index, handle in enumerate(handles):
        sink.span_ended(request, handle, index=index)
    sink.request_ended(request, line, duration_ms=duration_ms)


SPANS = [span_line("router", model=MODEL, start_ms=1, duration_ms=20, tokens_in=700, tokens_out=8, cache_hit=False, summary={"intent": "product_search"}),
         span_line("extractor", model=MODEL, start_ms=22, duration_ms=30, tokens_in=900, tokens_out=40, cache_hit=False, summary={"filters": {"kind": "Áo khoác nam"}}),
         span_line("retrieval", model="bge-m3", start_ms=53, duration_ms=15, summary={"mode": "dense_folded", "k": 5, "hits": [{"id": "SM-NAM-0001", "kind": "product", "title": "Áo khoác dù nam xanh rêu", "score": 0.8}]}),
         span_line("answer", model=MODEL, start_ms=70, duration_ms=25, tokens_in=1200, tokens_out=90, cache_hit=False, summary={"prompt_version": "answer_v2", "guardrail": None})]


# --- shape ------------------------------------------------------------------------------------------------------------------------------


def test_a_request_is_one_root_observation_with_the_internal_trace_id_behind_its_langfuse_id():
    sink, client = make_sink()
    run(sink, make_line(SPANS))
    root = client.root
    assert root.name == "shopmate-turn"
    assert root.kwargs["trace_context"] == {"trace_id": FakeClient.create_trace_id(seed=TRACE_ID)}
    assert root.propagated == [{"session_id": "sess-1", "trace_name": "shopmate-turn", "metadata": {"turn": "2", "trace_id": TRACE_ID}}]


def test_there_is_one_child_per_internal_span_with_the_same_name_in_the_same_order_and_every_observation_is_ended_children_first():
    sink, client = make_sink()
    run(sink, make_line(SPANS))
    assert [c.name for c in client.children] == [s["stage"] for s in SPANS]
    assert all(c.parent is client.root for c in client.children)
    assert all(o.ended for o in client.observations)
    assert client.root.end_order == len(client.observations)  # the root is the last to end


def test_the_stages_map_to_the_observation_types_that_say_what_they_are():
    spans = [span_line("router", model=MODEL), span_line("extractor", model=MODEL), span_line("retrieval", model="bge-m3"), span_line("order_lookup"),
             span_line("ticket_confirm"), span_line("reply"), span_line("answer", model=MODEL)]
    sink, client = make_sink()
    run(sink, make_line(spans))
    assert {c.name: c.as_type for c in client.children} == {"router": "generation", "extractor": "generation", "retrieval": "retriever",
                                                           "order_lookup": "tool", "ticket_confirm": "tool", "reply": "span", "answer": "generation"}


def test_an_answer_span_without_a_model_is_a_template_reply_not_a_generation():
    sink, client = make_sink()
    run(sink, make_line([span_line("answer", model=None)]))
    assert client.children[0].as_type == "span"


def test_children_are_created_inside_the_propagation_scope_so_they_carry_the_session():
    sink, client = make_sink()
    run(sink, make_line(SPANS))
    assert all(c.propagated and c.propagated[0]["session_id"] == "sess-1" for c in client.children)


def test_a_request_without_a_session_propagates_no_session_id():
    sink, client = make_sink()
    run(sink, make_line([], session_id=None, turn=None))
    assert "session_id" not in client.root.propagated[0] and client.root.propagated[0]["metadata"] == {"trace_id": TRACE_ID}


# --- what is sent -----------------------------------------------------------------------------------------------------------------------


def test_the_root_carries_the_question_the_reply_and_what_the_turn_was_about():
    sink, client = make_sink()
    run(sink, make_line(SPANS))
    root = client.root.merged
    assert root["input"] == "áo khoác nam xanh rêu" and root["output"] == "Bên mình có Áo khoác dù nam xanh rêu."
    assert root["metadata"] == {"trace_id": TRACE_ID, "turn": 2, "intent": "product_search", "status": "ok", "reply_source": "llm", "reply_truncated": False,
                                "schema_version": 2}
    assert "level" not in root


def test_a_generation_has_model_usage_cost_and_the_summary_as_its_output_but_never_a_prompt():
    sink, client = make_sink()
    run(sink, make_line(SPANS))
    router = client.children[0].merged
    assert router["model"] == MODEL and router["usage_details"] == {"input": 700, "output": 8}
    assert router["cost_details"]["total"] == pytest.approx(700 * 0.30 / 1e6 + 8 * 2.50 / 1e6)
    assert router["output"] == {"intent": "product_search"} and "input" not in router
    assert router["metadata"]["cost_basis"] == COST_BASIS and router["metadata"]["cache_hit"] is False
    assert router["metadata"]["duration_ms"] == 20 and router["metadata"]["start_ms"] == 1


def test_a_cache_hit_is_free_and_reports_no_usage_but_keeps_the_recorded_tokens_in_its_metadata():
    sink, client = make_sink()
    run(sink, make_line([span_line("answer", model=MODEL, tokens_in=1200, tokens_out=90, cache_hit=True)]))
    answer = client.children[0].merged
    assert answer["cost_details"] == {"input": 0.0, "output": 0.0, "total": 0.0} and "usage_details" not in answer
    assert answer["metadata"]["cache_hit"] is True and answer["metadata"]["cached_tokens_in"] == 1200 and answer["metadata"]["cached_tokens_out"] == 90


def test_a_response_without_usage_metadata_has_neither_usage_nor_cost():
    sink, client = make_sink()
    run(sink, make_line([span_line("router", model=MODEL, cache_hit=False)]))
    router = client.children[0].merged
    assert "usage_details" not in router and "cost_details" not in router


def test_the_spans_that_are_not_calls_to_gemini_have_no_usage_or_cost():
    sink, client = make_sink()
    run(sink, make_line(SPANS))
    for child in client.children:
        if child.as_type != "generation":
            assert "usage_details" not in child.merged and "cost_details" not in child.merged


def test_the_end_of_a_span_is_its_start_plus_its_measured_duration():
    sink, client = make_sink()
    request = sink.request_started(trace_id=TRACE_ID, session_id=None, turn=None)  # reads the clock: START_NS
    handle = sink.span_started(request, stage="router", model=MODEL)  # START_NS + 1 ms
    sink.span_ended(request, handle, index=0)  # START_NS + 2 ms
    sink.request_ended(request, make_line([span_line("router", model=MODEL, duration_ms=20.0)], session_id=None, turn=None), duration_ms=50.0)
    assert client.children[0].end_time == START_NS + 1_000_000 + 20_000_000
    assert client.root.end_time == START_NS + 50_000_000  # the measured duration of the request, and never earlier than a child's end


# --- levels -----------------------------------------------------------------------------------------------------------------------------


def test_a_span_that_failed_the_request_is_an_error_and_a_recovered_one_is_a_warning():
    boom = {"type": "qdrant_unreachable", "message": "ConnectError: refused"}
    slow = {"type": "llm_429_minute", "message": "per-minute 429 retried 1 time(s); the call then succeeded"}
    spans = [span_line("router", model=MODEL, error=slow), span_line("retrieval", model="bge-m3", error=boom)]
    sink, client = make_sink()
    run(sink, make_line(spans, status="error", error=boom, intent=None, reply=None, reply_source=None))
    router, retrieval = (c.merged for c in client.children)
    assert router["level"] == "WARNING" and router["status_message"].startswith("llm_429_minute")
    assert retrieval["level"] == "ERROR" and retrieval["status_message"] == "qdrant_unreachable: ConnectError: refused"
    assert client.root.merged["level"] == "ERROR" and client.root.merged["status_message"] == "qdrant_unreachable: ConnectError: refused"


def test_a_warning_request_is_a_warning_and_a_guardrail_fallback_is_only_a_warning_of_its_span():
    fallback = {"type": "answer_guardrail_fallback", "message": "a fixed template was sent"}
    sink, client = make_sink()
    run(sink, make_line([span_line("answer", model=MODEL, error=fallback)], status="warn", reply_source="guardrail_fallback"))
    assert client.children[0].merged["level"] == "WARNING" and client.root.merged["level"] == "WARNING"


# --- privacy ----------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("intent", ["order_status", "create_ticket"])
def test_the_text_of_an_order_or_ticket_turn_is_withheld_but_the_steps_are_still_sent(intent):
    spans = [span_line("router", model=MODEL, summary={"intent": intent}), span_line("order_lookup", summary={"verified": True, "found": True, "attempts": 1})]
    line = make_line(spans, intent=intent, question="đơn 1234 của khachhang02@example.com", reply="Đơn 1234 đang giao tới 12 Lê Lợi, Quận 1")
    sink, client = make_sink()
    run(sink, line)
    root = client.root.merged
    assert root["input"] == f"[withheld: {intent}, {len(line['question'])} ký tự]" and root["output"] == f"[withheld: {intent}, {len(line['reply'])} ký tự]"
    assert not any("Lê Lợi" in s or "khachhang02" in s for o in client.observations for s in o.every_string())
    assert [c.name for c in client.children] == ["router", "order_lookup"]
    assert client.children[1].merged["output"] == {"verified": True, "found": True, "attempts": 1}


def test_a_turn_that_only_has_an_order_span_is_withheld_whatever_its_intent_says():
    """A follow-up turn can reach the order tool with no intent recorded; the span is what gives it away."""
    line = make_line([span_line("order_lookup", summary={"verified": False, "found": False})], intent=None, question="0901234567 [email]", reply="Không xác minh được")
    sink, client = make_sink()
    run(sink, line)
    assert client.root.merged["input"].startswith("[withheld:")


def test_a_withheld_request_sends_the_error_type_but_not_its_message():
    err = {"type": "saleor_error", "message": "SaleorQueryError: order 1234 of khachhang02@example.com"}
    line = make_line([span_line("order_lookup", error=err)], intent="order_status", status="error", error=err, reply=None, reply_source=None)
    sink, client = make_sink()
    run(sink, line)
    assert client.root.merged["status_message"] == "saleor_error" and client.children[0].merged["status_message"] == "saleor_error"


def test_a_product_search_turn_is_not_withheld():
    sink, client = make_sink()
    run(sink, make_line(SPANS))
    assert client.root.merged["input"] == "áo khoác nam xanh rêu"


def test_whatever_the_line_holds_every_string_sent_is_masked_again_by_the_sink():
    """The Tracer already masked the line; this is the second layer, for a string it did not mask (an error message gets `redact` only)."""
    err = {"type": "internal_error", "message": f"ValueError: call 0901 234 567 or khachhang02@example.com with {FAKE_KEY}"}
    line = make_line([span_line("retrieval", model="bge-m3", error=err)], status="error", error=err, reply=None, reply_source=None)
    sink, client = make_sink(secrets=[FAKE_KEY])
    run(sink, line)
    sent = [s for o in client.observations for s in o.every_string()]
    assert not any(bad in s for s in sent for bad in ("0901 234 567", "khachhang02@example.com", FAKE_KEY))
    assert any("[sđt]" in s and "[email]" in s and "[secret]" in s for s in sent)


def test_the_internal_trace_id_is_never_corrupted_by_the_mask():
    unlucky = "ab" + "0123456789" + "cd" + "e" * 18
    sink, client = make_sink()
    run(sink, make_line(SPANS, trace_id=unlucky))
    assert client.root.merged["metadata"]["trace_id"] == unlucky and client.root.propagated[0]["metadata"]["trace_id"] == unlucky


# --- Langfuse failing -------------------------------------------------------------------------------------------------------------------


def test_a_line_that_could_not_be_built_still_closes_everything_without_sending_any_text():
    sink, client = make_sink()
    request = sink.request_started(trace_id=TRACE_ID, session_id="sess-1", turn=1)
    sink.span_ended(request, sink.span_started(request, stage="router", model=MODEL), index=0)
    sink.request_ended(request, None, duration_ms=5.0)
    assert all(o.ended for o in client.observations) and len(client.observations) == 2
    assert all("input" not in o.merged and "output" not in o.merged for o in client.observations)


def test_a_span_with_no_matching_line_entry_is_closed_without_data():
    sink, client = make_sink()
    request = sink.request_started(trace_id=TRACE_ID, session_id=None, turn=None)
    sink.span_started(request, stage="router", model=MODEL)  # span_ended never came and the line has no span
    sink.request_ended(request, make_line([], session_id=None, turn=None), duration_ms=5.0)
    assert all(o.ended for o in client.observations)


def test_when_updating_fails_everything_is_still_ended_and_the_failure_is_reported_to_the_caller():
    sink, client = make_sink(FakeClient(fail={"update"}))
    with pytest.raises(RuntimeError, match="update failed"):
        run(sink, make_line(SPANS))
    assert len(client.observations) == 1 + len(SPANS) and all(o.ended for o in client.observations)
