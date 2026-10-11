"""Trace schema v2 (docs/DECISIONS-phase7.md D7.1-D7.4): the line says what the customer asked, what each step decided and what the
customer received. Everything here is offline: fake components, a scripted LLM, an in-memory index and a fake Saleor transport.
Fake secrets are built in the test ("fake-" + "x" * 28), never taken from the environment."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import templates as T
from app.agent.agent import Agent
from app.agent.parsing import PHONE as AGENT_PHONE
from app.agent.protocols import Answer
from app.agent.tools import RealTools, TicketStore
from app.llm.errors import DailyLimitExhausted
from app.monitor import create_app
from app.retrieval.search import trace_hits
from app.saleor.readonly_client import ReadOnlySaleorClient
from app.tracing import (PHONE, REPLY_LIMIT, REPLY_SOURCES, SUMMARY_LIMIT, Tracer, annotate_span, fit_summaries, mask, span, summaries_size,
                         validate_trace_line)
from tests.fake_agent import FakeAnswerer, FakeExtractor, FakeRouter, FakeTools
from tests.test_agent_bounds import make_agent, trace_lines
from tests.test_agent_tools import NODE, OWNER, FakeSaleor
from tests.test_request_tracing import FILTERS, Q_POLICY, Q_PRODUCT, ROUTES, ScriptedLLM, _handle, _lines, _stack
from tests.test_retrieval_unit import _documents

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")
REPO = Path(__file__).resolve().parent.parent
FAKE_KEY = "fake-" + "x" * 28
V1_LINE = {"trace_id": "a" * 32, "ts": "2026-10-10T12:30:45.123Z", "question": "chao shop", "intent": "smalltalk", "status": "ok",
           "spans": [{"stage": "router", "start_ms": 0, "duration_ms": 12.5, "model": "gemini-3.5-flash-lite", "tokens_in": 700, "tokens_out": 8,
                      "cache_hit": False, "error": None}], "error": None}


class Generated:
    """What the answer span reads of a GenResult: no usageMetadata, so no tokens; `cached` is the cache flag."""

    raw = {}
    cached = False
    minute_429_retries = 0


class LLMAnswerer:
    """An answerer that behaves like the Gemini one: the text comes with a generation and a guardrail report."""

    model = "fake-llm"

    def __init__(self, *, fallback_reason=None, violations=(), checked=3, declined=False, prompt_version="answer_v2"):
        self.kwargs = dict(fallback_reason=fallback_reason, violations=tuple(violations), checked=checked, declined=declined, prompt_version=prompt_version)

    def compose(self, kind, question, evidence):
        return Answer(text="Áo khoác dù nam đen SM-NAM-0002 giá 429.000đ.", generation=Generated(), **self.kwargs)


def _tracer(tmp_path):
    return Tracer(tmp_path / "logs" / "traces.jsonl", secrets=[FAKE_KEY])


def _one_line(tracer):
    (line,) = [json.loads(x) for x in tracer.path.read_text(encoding="utf-8").splitlines()]
    return line


# --- one mask for what the customer typed and what the customer was told (D7.8) -------------------------------------------------------------

PHONES = ["0901234567", "0901 234 567", "090.123.4567", "090-123-4567", "+84 901 234 567", "84901234567", "+84901234567"]


@pytest.mark.parametrize("phone", PHONES)
def test_a_phone_number_in_the_question_is_masked_in_every_vietnamese_format(tmp_path, phone):
    tracer = _tracer(tmp_path)
    with tracer.request(f"gọi {phone} giúp mình, hoặc mail khachhang02@example.com, token=abcdefghijkl"):
        pass
    line = _one_line(tracer)
    assert line["question"] == "gọi [sđt] giúp mình, hoặc mail [email], token=[secret]"
    assert not any(digit_run in tracer.path.read_text(encoding="utf-8") for digit_run in ("901234567", "901 234 567", "123.4567", "123-4567"))
    assert validate_trace_line(line) == []


def test_the_question_the_reply_and_the_summaries_go_through_the_same_function(tmp_path):
    """The three places must give what `mask` gives, for the same text: if one of them drifted (a second regex, a missing step) this would show."""
    texts = ["gọi 0912 345 678 nhé", "Liên hệ khachhang02@example.com hoặc +84 987654321", f"khóa {FAKE_KEY} và 0901234567", "không có gì để che", "0123456789012"]
    tracer = _tracer(tmp_path)
    for text in texts:
        with tracer.request(text) as trace:
            with span("retrieval") as sp:
                sp.set_summary(hits=[{"id": "x", "kind": "product", "title": text, "score": 1}], note=text)
            trace.set_reply(text, "template")
    for text, raw in zip(texts, tracer.path.read_text(encoding="utf-8").splitlines()):
        line = json.loads(raw)
        expected = mask(text, [FAKE_KEY])
        assert line["question"] == line["reply"] == line["spans"][0]["summary"]["note"] == line["spans"][0]["summary"]["hits"][0]["title"] == expected, text


def test_amounts_and_order_numbers_are_not_phone_numbers(tmp_path):
    tracer = _tracer(tmp_path)
    text = "đơn #2057 giá 459.000đ, tổng 1500000đ, size 42, mã 8800123"
    with tracer.request(text) as trace:
        trace.set_reply(text, "template")
    line = _one_line(tracer)
    assert line["question"] == line["reply"] == text


def test_ids_models_and_error_messages_are_not_given_the_phone_mask(tmp_path):
    """A hex id can contain ten digits in a row; masking it would corrupt the thing used to look a request up."""
    tracer = _tracer(tmp_path)
    session = "a0123456789b-1111-4222-8333-444455556666"
    with tracer.request("chào", session_id=session, turn=1) as trace:
        with span("router", model="gemini-0123456789") as sp:
            sp.fail("llm_invalid_output", "the answer 0123456789 is not JSON")
        trace.fail("llm_invalid_output", "the answer 0123456789 is not JSON")
    line = _one_line(tracer)
    assert line["session_id"] == session and line["spans"][0]["model"] == "gemini-0123456789" and re.fullmatch(r"[0-9a-f]{32}", line["trace_id"])
    assert line["error"]["message"] == "the answer 0123456789 is not JSON"  # only the customer's own text gets the mask


def test_tracing_has_exactly_one_place_that_hides_phone_numbers():
    source = (REPO / "app" / "tracing.py").read_text(encoding="utf-8")
    assert source.count("PHONE.sub(") == 1  # inside `mask`


# --- the reply: masked, cut, sourced ---------------------------------------------------------------------------------------------------


def test_the_reply_is_masked_like_the_question_and_phone_numbers_too(tmp_path):
    tracer = _tracer(tmp_path)
    text = f"Liên hệ khachhang02@example.com hoặc 0901 234 567 / +84 912345678, khóa {FAKE_KEY}, token=abcdefghijkl giá 459.000đ"
    with tracer.request("xin chào") as trace:
        trace.set_reply(text, "template")
    line = _one_line(tracer)
    assert line["reply"] == "Liên hệ [email] hoặc [sđt] / [sđt], khóa [secret], token=[secret] giá 459.000đ"
    for leaked in ("khachhang02", "0901", "912345678", FAKE_KEY, "abcdefghijkl"):
        assert leaked not in tracer.path.read_text(encoding="utf-8")
    assert line["reply_source"] == "template" and line["reply_truncated"] is False and validate_trace_line(line) == []


def test_the_reply_is_cut_to_800_characters_after_masking_and_says_so(tmp_path):
    tracer = _tracer(tmp_path)
    secret_at_the_edge = "a" * (REPLY_LIMIT - 10) + f" {FAKE_KEY} cuối"
    with tracer.request("q") as trace:
        trace.set_reply(secret_at_the_edge, "llm")
    with tracer.request("q2") as trace:
        trace.set_reply("b" * REPLY_LIMIT, "llm")  # exactly the limit: nothing is cut
    cut, whole = [json.loads(x) for x in tracer.path.read_text(encoding="utf-8").splitlines()]
    assert len(cut["reply"]) == REPLY_LIMIT == 800 and cut["reply_truncated"] is True
    assert "fake-" not in cut["reply"] and "[secret]" in cut["reply"]  # masked first: no half secret is left at the cut
    assert len(whole["reply"]) == REPLY_LIMIT and whole["reply_truncated"] is False
    assert validate_trace_line(cut) == [] and validate_trace_line(whole) == []


def test_a_reply_and_its_source_are_set_together_and_only_from_the_known_sources(tmp_path):
    tracer = _tracer(tmp_path)
    with tracer.request("q") as trace:
        with pytest.raises(ValueError):
            trace.set_reply("x", None)
        with pytest.raises(ValueError):
            trace.set_reply(None, "llm")
        with pytest.raises(ValueError):
            trace.set_reply("x", "made_up")
    assert REPLY_SOURCES == ("llm", "template", "guardrail_fallback", "clarify", "error_template")


class DyingRouter(FakeRouter):
    def route(self, question):
        raise DailyLimitExhausted("429 RESOURCE_EXHAUSTED daily")


def test_a_failure_before_any_reply_leaves_the_reply_null_and_the_error_type_unchanged(tmp_path):
    tracer = _tracer(tmp_path)
    agent = Agent(router=DyingRouter({}), extractor=FakeExtractor({}), tools=FakeTools(), answerer=FakeAnswerer(), tracer=tracer)
    with pytest.raises(DailyLimitExhausted):
        agent.turn("c1", "chào")
    line = _one_line(tracer)
    assert line["error"]["type"] == "llm_429_daily" and line["status"] == "error"  # the error taxonomy is untouched
    assert line["reply"] is None and line["reply_source"] is None  # the customer saw nothing: the exception reached the caller
    assert validate_trace_line(line) == []


def test_with_catch_errors_the_customer_gets_the_technical_problem_reply_and_the_trace_says_so(tmp_path):
    tracer = _tracer(tmp_path)
    agent = Agent(router=DyingRouter({}), extractor=FakeExtractor({}), tools=FakeTools(), answerer=FakeAnswerer(), tracer=tracer, catch_errors=True)
    result = agent.turn("c1", "chào")
    assert result.reply == T.TECHNICAL_PROBLEM and result.failed and result.reply_source == "error_template" and result.trace_id
    line = _one_line(tracer)
    assert (line["error"]["type"], line["status"]) == ("llm_429_daily", "error")
    assert (line["reply"], line["reply_source"], line["trace_id"]) == (T.TECHNICAL_PROBLEM, "error_template", result.trace_id)
    assert validate_trace_line(line) == []


def test_catch_errors_never_swallows_an_interrupt(tmp_path):
    tracer = _tracer(tmp_path)

    class Interrupted(FakeRouter):
        def route(self, question):
            raise KeyboardInterrupt

    agent = Agent(router=Interrupted({}), extractor=FakeExtractor({}), tools=FakeTools(), answerer=FakeAnswerer(), tracer=tracer, catch_errors=True)
    with pytest.raises(KeyboardInterrupt):
        agent.turn("c1", "chào")
    line = _one_line(tracer)
    assert line["error"]["type"] == "request_interrupted" and line["reply"] is None


# --- every reply_source is produced by a real path --------------------------------------------------------------------------------------


def test_reply_source_llm_when_the_llm_text_passed_the_guardrails(tmp_path):
    agent, *_, tracer = make_agent(tmp_path, {Q_PRODUCT: "product_search"}, {Q_PRODUCT: {"kind": "Áo khoác nam"}}, answerer=LLMAnswerer())
    result = agent.turn("c1", Q_PRODUCT)
    line = trace_lines(tracer)[0]
    assert line["reply_source"] == result.reply_source == "llm" and line["reply"] == result.reply
    answer = next(s for s in line["spans"] if s["stage"] == "answer")
    assert answer["summary"] == {"prompt_version": "answer_v2", "guardrail": {"checked": 3, "violations": [], "fallback_used": False}}


def test_reply_source_template_for_fixed_replies_and_for_the_answer_without_an_llm(tmp_path):
    agent, *_, tracer = make_agent(tmp_path, {"chào": "smalltalk", Q_PRODUCT: "product_search"}, {Q_PRODUCT: {"kind": "Áo khoác nam"}})
    agent.turn("c1", "chào")
    agent.turn("c1", Q_PRODUCT)  # FakeAnswerer: text without a generation = the deterministic answer, no LLM
    first, second = trace_lines(tracer)
    assert (first["reply_source"], first["spans"][-1]["summary"]) == ("template", {"template": "smalltalk"})
    assert second["reply_source"] == "template"
    assert next(s for s in second["spans"] if s["stage"] == "answer")["summary"] == {"prompt_version": None, "guardrail": None}  # no guardrail ran


def test_reply_source_template_when_there_is_nothing_to_ground_an_answer_on(tmp_path):
    agent, *_, tracer = make_agent(tmp_path, {Q_PRODUCT: "product_search"}, {Q_PRODUCT: {"kind": "Mũ", "price_max": 1000}})
    result = agent.turn("c1", Q_PRODUCT)
    line = trace_lines(tracer)[0]
    assert result.hits == [] and line["reply_source"] == "template" and line["reply"].startswith(T.NO_FULL_MATCH)
    assert line["spans"][-1]["stage"] == "reply" and line["spans"][-1]["summary"]["template"] in ("no_results", "no_results_relaxed")


def test_reply_source_guardrail_fallback_with_the_rules_that_fired(tmp_path):
    answerer = LLMAnswerer(fallback_reason="price_mismatch: 99000 is not the price of SM-NAM-0002", violations=("price_mismatch", "stock_mismatch"), checked=2)
    agent, *_, tracer = make_agent(tmp_path, {Q_PRODUCT: "product_search"}, {Q_PRODUCT: {"kind": "Áo khoác nam"}}, answerer=answerer)
    agent.turn("c1", Q_PRODUCT)
    line = trace_lines(tracer)[0]
    answer = next(s for s in line["spans"] if s["stage"] == "answer")
    assert line["reply_source"] == "guardrail_fallback" and line["status"] == "warn"  # the old warning is still there
    assert answer["error"]["type"] == "answer_guardrail_fallback"
    assert answer["summary"]["guardrail"] == {"checked": 2, "violations": ["price_mismatch", "stock_mismatch"], "fallback_used": True}


def test_reply_source_template_when_the_model_declined_policy_questions(tmp_path):
    agent, *_, tracer = make_agent(tmp_path, {"hỏi gì đó": "policy_question"}, answerer=LLMAnswerer(declined=True, checked=1))
    agent.turn("c1", "hỏi gì đó")
    line = trace_lines(tracer)[0]
    assert line["reply_source"] == "template" and line["status"] == "ok"  # nothing was rejected: the fixed no-information reply


def test_reply_source_clarify_for_questions_to_the_customer_including_the_ticket_confirmation(tmp_path):
    complaint = "giao nhầm màu rồi shop"
    agent, *_, tracer = make_agent(tmp_path, {"cho xem đồ": "product_search", complaint: "create_ticket"}, {"cho xem đồ": {}})
    agent.turn("c1", "cho xem đồ")
    agent.turn("c2", complaint)
    agent.turn("c2", "khachhang02@example.com")  # the contact email: now the ticket is proposed
    first, second, third = trace_lines(tracer)
    assert first["reply_source"] == "clarify" and first["reply"] == T.ASK_PRODUCT_TYPE
    assert second["reply_source"] == "clarify" and second["reply"] == T.ASK_CONTACT_EMAIL
    assert third["reply_source"] == "clarify" and "Bạn xác nhận tạo phiếu" in third["reply"]
    assert third["spans"][-1]["summary"] == {"state": "asked"}


def test_reply_source_error_template_for_the_technical_problem_the_step_cap_and_a_failed_ticket(tmp_path):
    agent, *_, tracer = make_agent(tmp_path, {"???": None})
    unusable = agent.turn("c1", "???")
    capped_agent, *_, capped_tracer = make_agent(tmp_path / "cap", {"chào": "smalltalk"}, max_steps=1)
    capped = capped_agent.turn("c1", "chào")
    failing = FakeTools(fail_ticket=OSError("disk full"))
    ticket_agent, *_, ticket_tracer = make_agent(tmp_path / "tk", {"giao nhầm màu rồi shop": "create_ticket"}, tools=failing)
    for message in ("giao nhầm màu rồi shop", "khachhang02@example.com", "có"):
        failed = ticket_agent.turn("c1", message)
    assert (unusable.reply, capped.reply, failed.reply) == (T.TECHNICAL_PROBLEM, T.STEP_CAP, T.TICKET_FAILED)
    assert trace_lines(tracer)[0]["reply_source"] == "error_template"
    assert trace_lines(capped_tracer)[0]["reply_source"] == "error_template"
    last = trace_lines(ticket_tracer)[-1]
    assert last["reply_source"] == "error_template" and last["error"]["type"] == "ticket_write_failed"


def test_every_reply_source_is_exercised_by_this_file():
    source = Path(__file__).read_text(encoding="utf-8")
    for name in REPLY_SOURCES:
        assert f'"{name}"' in source, name


# --- session and turn ---------------------------------------------------------------------------------------------------------------------


def test_session_id_and_turn_count_the_turns_of_each_conversation(tmp_path):
    routes = {"chào": "smalltalk", "cảm ơn": "smalltalk", "hi": "smalltalk"}
    agent, *_, tracer = make_agent(tmp_path, routes)
    for conversation, message in (("c1", "chào"), ("c2", "hi"), ("c1", "cảm ơn")):
        agent.turn(conversation, message)
    lines = trace_lines(tracer)
    assert [(l["session_id"], l["turn"]) for l in lines] == [("c1", 1), ("c2", 1), ("c1", 2)]
    assert all(validate_trace_line(l) == [] for l in lines)


def test_a_turn_that_resumes_a_confirmation_is_the_next_turn_of_the_same_session(tmp_path):
    agent, *_, tracer = make_agent(tmp_path, {"giao nhầm màu rồi shop": "create_ticket"})
    for message in ("giao nhầm màu rồi shop", "khachhang02@example.com", "có"):
        agent.turn("c9", message)
    lines = trace_lines(tracer)
    assert [(l["session_id"], l["turn"]) for l in lines] == [("c9", 1), ("c9", 2), ("c9", 3)]
    stages = [(s["stage"], s["summary"]) for s in lines[2]["spans"]]
    assert stages == [("ticket_confirm", {"state": "written"}), ("reply", {"template": "ticket_created"})]
    assert lines[2]["reply_source"] == "template" and "TCK-" in lines[2]["reply"]


def test_a_turn_result_carries_the_id_of_its_trace_line(tmp_path):
    agent, *_, tracer = make_agent(tmp_path, {"chào": "smalltalk"})
    result = agent.turn("c1", "chào")
    assert result.trace_id == trace_lines(tracer)[0]["trace_id"]
    untraced, *_ = make_agent(tmp_path / "x", {"chào": "smalltalk"}, trace=False)
    assert untraced.turn("c1", "chào").trace_id is None


# --- the summaries -------------------------------------------------------------------------------------------------------------------------


def test_router_extractor_retrieval_and_reply_summaries_of_a_product_question(tmp_path):
    stack = _stack(tmp_path, ScriptedLLM(ROUTES, FILTERS))
    _handle(Q_PRODUCT, stack)
    line = _lines(stack)[0]
    router, extractor, retrieval, answer = (s["summary"] for s in line["spans"])
    assert router == {"intent": "product_search"}
    assert extractor == {"filters": {"kind": "Áo thun nam", "color": "Đen"}}  # normalised: None fields are left out, no doc_type
    assert retrieval["mode"] == "dense_folded" and retrieval["k"] == 5 and 1 <= len(retrieval["hits"]) <= 5
    for hit in retrieval["hits"]:
        assert set(hit) == {"id", "kind", "title", "score"} and hit["kind"] == "product" and hit["id"].startswith("product:") and len(hit["title"]) <= 80
    assert answer == {"prompt_version": None, "guardrail": None}  # the template answerer: no prompt, no guardrail
    assert line["reply_source"] == "template" and line["session_id"] and line["turn"] == 1 and validate_trace_line(line) == []


def test_a_policy_hit_names_the_clause_by_title_and_never_carries_its_text(tmp_path):
    stack = _stack(tmp_path, ScriptedLLM(ROUTES, FILTERS))
    _handle(Q_POLICY, stack)
    line = _lines(stack)[0]
    retrieval = next(s for s in line["spans"] if s["stage"] == "retrieval")["summary"]
    assert retrieval["k"] == 3 and retrieval["hits"] and all(h["kind"] == "policy" and h["id"].startswith("policy:") for h in retrieval["hits"])
    assert all(h["title"].startswith("t") for h in retrieval["hits"])  # the title of the clause
    summaries = json.dumps([s["summary"] for s in line["spans"]], ensure_ascii=False)
    assert all(d.text not in summaries for d in _documents() if d.payload["doc_type"] == "policy")  # no clause text in any summary


class SpanningExtractor(FakeExtractor):
    """Like FakeExtractor, but opens the `extractor` span the real one opens, with the summary the real one sets."""

    def extract(self, question):
        with span("extractor") as recorded:
            result = super().extract(question)
            recorded.set_summary(filters=result.filters.model_dump(exclude_none=True, exclude={"doc_type"}))
            return result


def test_the_extractor_summary_names_the_filter_the_relaxed_search_left_out(tmp_path):
    filters = {Q_PRODUCT: {"kind": "Mũ", "color": "Đỏ"}}  # there is a yellow hat in the fake catalogue and no red one
    tracer = _tracer(tmp_path)
    agent = Agent(router=FakeRouter({Q_PRODUCT: "product_search"}), extractor=SpanningExtractor(filters), tools=FakeTools(),
                  answerer=FakeAnswerer(), tracer=tracer)
    result = agent.turn("c1", Q_PRODUCT)
    assert result.relaxed == ["color"] and [h["sku"] for h in result.hits] == ["SM-PK-0005"]
    extractor = next(s for s in _one_line(tracer)["spans"] if s["stage"] == "extractor")
    assert extractor["summary"] == {"filters": {"kind": "Mũ", "color": "Đỏ"}, "relaxed": ["color"]}


def test_annotate_span_adds_to_the_latest_span_of_that_stage_and_is_a_no_op_otherwise(tmp_path):
    annotate_span("extractor", relaxed=["color"])  # outside a request
    tracer = _tracer(tmp_path)
    with tracer.request("q"):
        with span("extractor") as first:
            first.set_summary(filters={"kind": "Mũ"})
        with span("extractor") as second:
            second.set_summary(filters={"kind": "Áo"})
        annotate_span("extractor", relaxed=["color"])
        annotate_span("order_lookup", attempts=2)  # no such span
    line = _one_line(tracer)
    assert [s["summary"] for s in line["spans"]] == [{"filters": {"kind": "Mũ"}}, {"filters": {"kind": "Áo"}, "relaxed": ["color"]}]


def _lookup_summary(line):
    return next(s for s in line["spans"] if s["stage"] == "order_lookup")["summary"]


def test_the_order_lookup_summary_says_verified_found_and_attempts_and_nothing_personal(tmp_path):
    tools = RealTools(None, ReadOnlySaleorClient("http://saleor.test/graphql/", "t" * 30, transport=FakeSaleor()), TicketStore(tmp_path / "t.jsonl"))
    wrong, right = "đơn 88 của khachhang99@example.com", f"đơn 88 của {OWNER}"
    agent, *_, tracer = make_agent(tmp_path, {wrong: "order_status", right: "order_status"}, tools=tools, answerer=LLMAnswerer())
    agent.turn("c1", wrong)
    agent.turn("c1", right)  # attempts counts the verifications of the conversation, the failed one included
    first, second = trace_lines(tracer)
    assert _lookup_summary(first) == {"verified": False, "found": True, "attempts": 1}  # `found` is for the operator; the reply cannot tell
    assert first["reply"] == T.REFUSE_VERIFICATION and first["reply_source"] == "template"
    assert _lookup_summary(second) == {"verified": True, "found": True, "attempts": 2}
    text = tracer.path.read_text(encoding="utf-8")
    assert "khachhang" not in text and NODE["channel"]["slug"] not in json.dumps([_lookup_summary(first), _lookup_summary(second)])  # no email anywhere


def test_the_summary_of_an_unknown_number_is_found_false(tmp_path):
    tools = RealTools(None, ReadOnlySaleorClient("http://saleor.test/graphql/", "t" * 30, transport=FakeSaleor({})), TicketStore(tmp_path / "t.jsonl"))
    message = f"đơn 77 của {OWNER}"
    agent, *_, tracer = make_agent(tmp_path, {message: "order_status"}, tools=tools, answerer=LLMAnswerer())
    agent.turn("c1", message)
    line = trace_lines(tracer)[0]
    assert _lookup_summary(line) == {"verified": False, "found": False, "attempts": 1} and line["reply"] == T.REFUSE_VERIFICATION


def test_ticket_confirm_summary_is_asked_then_written(tmp_path):
    agent, *_, tracer = make_agent(tmp_path, {"giao nhầm màu rồi shop": "create_ticket"})
    for message in ("giao nhầm màu rồi shop", "khachhang02@example.com", "có"):
        agent.turn("c1", message)
    lines = trace_lines(tracer)
    assert [s["summary"] for s in lines[1]["spans"] if s["stage"] == "ticket_confirm"] == [{"state": "asked"}]
    assert [s["summary"] for s in lines[2]["spans"] if s["stage"] == "ticket_confirm"] == [{"state": "written"}]


def test_a_declined_ticket_is_seen_in_the_reply_template(tmp_path):
    agent, *_, tracer = make_agent(tmp_path, {"giao nhầm màu rồi shop": "create_ticket"})
    for message in ("giao nhầm màu rồi shop", "khachhang02@example.com", "không"):
        agent.turn("c1", message)
    last = trace_lines(tracer)[-1]
    assert last["spans"][-1]["summary"] == {"template": "ticket_cancelled"} and last["reply"] == T.TICKET_CANCELLED


def test_summary_strings_are_masked_too(tmp_path):
    tracer = _tracer(tmp_path)
    with tracer.request("q"):
        with span("retrieval") as sp:
            sp.set_summary(hits=[{"id": "product:SM-1", "kind": "product", "title": f"Liên hệ khachhang02@example.com {FAKE_KEY} 0901234567", "score": 0.5}])
    text = tracer.path.read_text(encoding="utf-8")
    for leaked in ("khachhang02", FAKE_KEY, "0901234567"):
        assert leaked not in text
    assert "[email]" in text and "[secret]" in text and "[sđt]" in text


# --- the 4 KB limit ------------------------------------------------------------------------------------------------------------------------


def _fat_hits(n):
    return [{"id": f"product:SM-NAM-{i:04d}", "kind": "product", "title": "x" * 80, "score": 0.01 + i / 1000} for i in range(n)]


def test_all_the_summaries_of_a_line_stay_within_4_kb_and_the_hits_are_cut_first(tmp_path):
    tracer = _tracer(tmp_path)
    with tracer.request("q"):
        for _ in range(6):  # six retrievals of five fat hits each
            with span("retrieval") as sp:
                sp.set_summary(mode="dense_folded", k=5, hits=_fat_hits(5))
        with span("router") as sp:
            sp.set_summary(intent="product_search")
    line = _one_line(tracer)
    assert summaries_size(line["spans"]) <= SUMMARY_LIMIT == 4096
    assert line["spans"][-1]["summary"] == {"intent": "product_search"}  # the small summaries are kept whole
    assert any(s["summary"].get("hits_truncated") for s in line["spans"][:6])
    assert all(s["summary"]["mode"] == "dense_folded" for s in line["spans"][:6])  # what decided is kept, only hits go
    assert validate_trace_line(line) == []


def test_when_cutting_the_hits_is_not_enough_the_biggest_summary_is_replaced_by_a_marker():
    spans = [{"summary": {"filters": {"kind": "y" * 5000}}}, {"summary": {"intent": "product_search"}}]
    fit_summaries(spans)
    assert spans[0]["summary"] == {"truncated": True} and spans[1]["summary"] == {"intent": "product_search"}
    assert summaries_size(spans) <= SUMMARY_LIMIT


def test_a_line_with_summaries_over_the_limit_is_invalid():
    line = {**V1_LINE, "schema_version": 2, "session_id": None, "turn": None, "reply": None, "reply_truncated": False, "reply_source": None}
    line["spans"] = [{**V1_LINE["spans"][0], "summary": {"filters": {"kind": "y" * 5000}}}]
    assert any("4096" in p for p in validate_trace_line(line))


def test_trace_hits_name_at_most_five_hits_with_short_titles():
    hits = [SimpleNamespace(key=f"product:SM-{i}", point_id=str(i), score=0.123456789, payload={"doc_type": "product", "name": "N" * 200}) for i in range(8)]
    named = trace_hits(hits)
    assert len(named) == 5 and all(len(h["title"]) == 80 and h["score"] == 0.12346 for h in named)


# --- v1 lines and the validator -----------------------------------------------------------------------------------------------------------------


def test_a_version_1_line_is_still_valid_and_a_mixed_file_is_served_whole_by_the_monitor(tmp_path):
    from fastapi.testclient import TestClient

    assert validate_trace_line(V1_LINE) == []
    tracer = _tracer(tmp_path)
    with tracer.request("v2") as trace:
        trace.set_reply("xin chào", "template")
    path = tmp_path / "logs" / "traces.jsonl"
    path.write_text(json.dumps(V1_LINE, ensure_ascii=False) + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    body = TestClient(create_app(traces_path=path), client=("127.0.0.1", 50000)).get("/monitor/api/traces?limit=10").text
    served = [json.loads(x) for x in body.splitlines()]
    assert [("schema_version" in l, validate_trace_line(l)) for l in served] == [(False, []), (True, [])]


@pytest.mark.parametrize("change,fragment", [
    ({"schema_version": 3}, "schema_version"), ({"turn": 0}, "turn"), ({"reply": 5}, "reply"), ({"reply_source": "magic"}, "reply_source"),
    ({"reply": None}, "both set or both null"), ({"reply_truncated": "yes"}, "reply_truncated"), ({"reply": "x" * 801}, "longer than 800"),
    ({"reply_truncated": True}, "reply_truncated is true"), ({"session_id": 12}, "session_id")])
def test_the_validator_rejects_each_deviation_of_version_2(change, fragment):
    line = {**V1_LINE, "schema_version": 2, "session_id": "s", "turn": 1, "reply": "xin chào", "reply_truncated": False, "reply_source": "template",
            "spans": [{**V1_LINE["spans"][0], "summary": {"intent": "smalltalk"}}]}
    assert validate_trace_line(line) == []
    problems = validate_trace_line({**line, **change})
    assert problems and any(fragment in p for p in problems), problems


def test_a_version_2_line_must_have_every_new_key_and_a_summary_object_on_each_span():
    line = {**V1_LINE, "schema_version": 2}
    assert validate_trace_line(line)  # the other new keys are missing
    full = {**line, "session_id": None, "turn": None, "reply": None, "reply_truncated": False, "reply_source": None}
    assert validate_trace_line(full)  # the spans have no summary
    full["spans"] = [{**V1_LINE["spans"][0], "summary": []}]
    assert any("summary must be an object" in p for p in validate_trace_line(full))


def test_the_phone_mask_pattern_is_the_agents_phone_pattern():
    assert PHONE.pattern == AGENT_PHONE.pattern  # tracing cannot import the agent, so the two are kept equal by this test


def test_logs_are_git_ignored():
    result = subprocess.run(["git", "check-ignore", "logs/traces.jsonl"], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == "logs/traces.jsonl"


def test_the_stages_did_not_change():
    from app.tracing import STAGES

    assert STAGES == ("router", "extractor", "retrieval", "order_lookup", "ticket_confirm", "reply", "answer")  # D7.4: the guardrail is not a stage


def test_a_follow_up_turn_that_skips_the_router_still_records_the_intent(tmp_path):
    agent, router, *_, tracer = make_agent(tmp_path, {"đơn 88 đâu rồi": "order_status"})  # the router raises on any other text
    agent.turn("c1", "đơn 88 đâu rồi")
    agent.turn("c1", "khachhang10@example.com")  # the answer to "which email?": goes straight to the lookup
    first, second = trace_lines(tracer)
    assert router.calls == ["đơn 88 đâu rồi"] and "router" not in [s["stage"] for s in second["spans"]]
    assert (first["intent"], second["intent"]) == ("order_status", "order_status")
    assert [s["stage"] for s in second["spans"]] == ["answer"]  # the fake tools open no order_lookup span; the real ones do
