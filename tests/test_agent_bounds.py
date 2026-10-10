"""The agent's loops and exits (docs/AGENT-DESIGN.md section 3): clarify at most 2 times, relax filters once, disable order lookup
after 3 failed verifications, write a ticket only after an explicit yes, stop a turn at the step cap. Everything runs with fake
LLM components and fake tools that record their calls, so what must NOT happen is proved as well as what must."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from langgraph.errors import GraphRecursionError

from app.agent import templates as T
from app.agent.agent import Agent
from app.tracing import Tracer, validate_trace_line
from tests.fake_agent import FakeAnswerer, FakeExtractor, FakeRouter, FakeTools

REPO = Path(__file__).resolve().parent.parent
EMAIL_88, EMAIL_91 = "khachhang10@example.com", "khachhang02@example.com"


def make_agent(tmp_path, routes, filters=None, tools=None, answerer=None, max_steps=8, trace=True):
    router, extractor = FakeRouter(routes), FakeExtractor(filters or {})
    tools, answerer = tools or FakeTools(), answerer or FakeAnswerer()
    tracer = Tracer(tmp_path / "logs" / "traces.jsonl") if trace else None
    agent = Agent(router=router, extractor=extractor, tools=tools, answerer=answerer, tracer=tracer, max_steps=max_steps)
    return agent, router, extractor, tools, answerer, tracer


def trace_lines(tracer):
    return [json.loads(x) for x in tracer.path.read_text(encoding="utf-8").splitlines()]


# --- the clarify loop: at most 2 questions, then answer with what is known or hand off --------------------------------------------------

VAGUE = "cho mình xem đồ đi shop"


def test_a_vague_product_request_is_clarified_twice_then_searched_with_what_is_known_never_a_third_question(tmp_path):
    routes = {VAGUE: "product_search", "màu đỏ thôi": "product_search", "gì cũng được": "product_search"}
    filters = {VAGUE: {}, f"{VAGUE} màu đỏ thôi": {"color": "Đỏ"}, f"{VAGUE} màu đỏ thôi gì cũng được": {"color": "Đỏ"}}
    agent, router, extractor, tools, answerer, _ = make_agent(tmp_path, routes, filters)
    first = agent.turn("c1", VAGUE)
    second = agent.turn("c1", "màu đỏ thôi")
    assert (first.reply, first.awaiting, first.clarify_count) == (T.ASK_PRODUCT_TYPE, "product_type", 1)
    assert (second.reply, second.awaiting, second.clarify_count) == (T.ASK_PRODUCT_TYPE, "product_type", 2)
    assert tools.product_searches == []  # no search while the request is vague
    third = agent.turn("c1", "gì cũng được")
    assert third.awaiting is None and third.reply != T.ASK_PRODUCT_TYPE  # no third question
    assert tools.product_searches == [(f"{VAGUE} màu đỏ thôi gì cũng được", {"color": "Đỏ"})]  # what is known: the colour
    assert [h["sku"] for h in third.hits] == ["SM-NU-0003"] and "SM-NU-0003" in third.reply
    assert len(router.calls) == 3 and extractor.calls[-1] == f"{VAGUE} màu đỏ thôi gì cũng được"


def test_a_clarifying_answer_is_merged_with_the_request_and_the_search_goes_ahead(tmp_path):
    routes = {VAGUE: "product_search", "áo khoác nam xanh rêu": "product_search"}
    filters = {VAGUE: {}, f"{VAGUE} áo khoác nam xanh rêu": {"kind": "Áo khoác nam", "color": "Xanh rêu"}}
    agent, _, extractor, tools, _, _ = make_agent(tmp_path, routes, filters)
    agent.turn("c1", VAGUE)
    result = agent.turn("c1", "áo khoác nam xanh rêu")
    assert extractor.calls == [VAGUE, f"{VAGUE} áo khoác nam xanh rêu"]  # the extractor sees the whole request
    assert [h["sku"] for h in result.hits] == ["SM-NAM-0001"] and result.awaiting is None and result.clarify_count == 0
    assert len(tools.product_searches) == 1


ASK_ORDER = "kiểm tra giúp mình đơn hàng với"


def test_missing_order_slots_are_asked_for_twice_then_handed_off_never_a_third_question(tmp_path):
    routes = {ASK_ORDER: "order_status", "không biết": "order_status", "chịu luôn": "order_status"}
    agent, router, _, tools, answerer, _ = make_agent(tmp_path, routes)
    first, second, third = agent.turn("c1", ASK_ORDER), agent.turn("c1", "không biết"), agent.turn("c1", "chịu luôn")
    assert (first.reply, first.awaiting) == (T.ASK_NUMBER_AND_EMAIL, "order_number_and_email")
    assert second.reply == T.ASK_NUMBER_AND_EMAIL and second.clarify_count == 2
    assert third.reply == T.HANDOFF and third.awaiting is None  # a polite hand-off, not a third question
    assert tools.lookups == [] and answerer.calls == []
    assert agent.snapshot("c1").get("pending") is None  # nothing left waiting


def test_an_answer_that_gives_the_awaited_slot_skips_the_router_and_the_flow_completes(tmp_path):
    routes = {ASK_ORDER: "order_status"}
    agent, router, _, tools, answerer, _ = make_agent(tmp_path, routes)
    agent.turn("c1", ASK_ORDER)
    second = agent.turn("c1", "88")  # a bare number is the order number now, because the agent just asked for it
    assert second.reply == T.ASK_EMAIL and second.clarify_count == 2
    third = agent.turn("c1", EMAIL_88)
    assert tools.lookups == [("88", EMAIL_88)] and "88" in third.reply and third.awaiting is None
    assert router.calls == [ASK_ORDER]  # the two follow-ups were read by rules, not by the router
    assert [kind for kind, *_ in answerer.calls] == ["order"]


def test_a_reply_without_the_awaited_slot_is_a_new_message_and_a_new_topic_drops_the_old_question(tmp_path):
    routes = {ASK_ORDER: "order_status", "đổi size trong mấy ngày vậy shop": "policy_question"}
    agent, router, *_ = make_agent(tmp_path, routes)
    agent.turn("c1", ASK_ORDER)
    changed = agent.turn("c1", "đổi size trong mấy ngày vậy shop")
    assert router.calls[-1] == "đổi size trong mấy ngày vậy shop" and changed.intent == "policy_question"
    assert changed.awaiting is None and agent.snapshot("c1").get("pending") is None and changed.clarify_count == 0


# --- filter relaxation: once, in a fixed order, announced -----------------------------------------------------------------------------

SWEATER_XL = "áo len nữ màu đỏ size XL còn không shop"


def test_a_search_with_no_result_is_relaxed_once_dropping_size_and_the_reply_says_so_before_any_suggestion(tmp_path):
    filters = {SWEATER_XL: {"kind": "Áo len nữ", "color": "Đỏ", "size": "XL"}}
    agent, _, _, tools, _, _ = make_agent(tmp_path, {SWEATER_XL: "product_search"}, filters)
    result = agent.turn("c1", SWEATER_XL)
    assert [f for _, f in tools.product_searches] == [{"kind": "Áo len nữ", "color": "Đỏ", "size": "XL"}, {"kind": "Áo len nữ", "color": "Đỏ"}]
    assert result.relaxed == ["size"] and [h["sku"] for h in result.hits] == ["SM-NU-0003"]
    assert result.reply.startswith(T.NO_FULL_MATCH) and T.relaxed_note("size") in result.reply
    assert result.reply.index(T.NO_FULL_MATCH) < result.reply.index(T.relaxed_note("size")) < result.reply.index("SM-NU-0003")


def test_when_the_relaxed_search_is_empty_too_the_agent_stops_and_never_searches_a_third_time(tmp_path):
    question = "áo len nữ màu xanh dương size XL có không shop"
    filters = {question: {"kind": "Áo len nữ", "color": "Xanh dương", "size": "XL"}}
    agent, _, _, tools, answerer, _ = make_agent(tmp_path, {question: "product_search"}, filters)
    result = agent.turn("c1", question)
    assert len(tools.product_searches) == 2 and result.hits == [] and answerer.calls == []
    assert result.reply == T.no_results_relaxed("size") and T.NO_FULL_MATCH in result.reply


@pytest.mark.parametrize("filters", [{"kind": "Áo len nữ", "price_max": 100000}, {"kind": "Mũ", "in_stock": False, "price_min": 900000},
                                     {"category": "thoi-trang-nu", "price_max": 1000}])
def test_with_nothing_relaxable_there_is_one_search_and_a_plain_nothing_found(tmp_path, filters):
    question = "tìm giúp mình món đồ này"
    agent, _, _, tools, _, _ = make_agent(tmp_path, {question: "product_search"}, {question: filters})
    result = agent.turn("c1", question)
    assert len(tools.product_searches) == 1 and result.relaxed == [] and result.reply == T.NO_RESULTS
    assert T.NO_FULL_MATCH in result.reply and "bỏ điều kiện" not in result.reply


def test_a_search_that_finds_something_is_never_relaxed_and_never_says_nothing_matches(tmp_path):
    question = "áo khoác nam xanh rêu size L"
    agent, _, _, tools, _, _ = make_agent(tmp_path, {question: "product_search"}, {question: {"kind": "Áo khoác nam", "color": "Xanh rêu", "size": "L"}})
    result = agent.turn("c1", question)
    assert len(tools.product_searches) == 1 and result.relaxed == [] and T.NO_FULL_MATCH not in result.reply and "SM-NAM-0001" in result.reply


@pytest.mark.parametrize("filters, dropped", [
    ({"kind": "Mũ", "color": "Tím", "size": "XXL", "in_stock": True, "price_max": 50000}, "size"),
    ({"kind": "Mũ", "color": "Tím", "price_min": 10, "price_max": 50000}, "color"),
    ({"kind": "Áo len nữ", "material": "Lụa", "in_stock": True}, "material"),
])
def test_the_protected_constraints_survive_the_relaxed_search(tmp_path, filters, dropped):
    question = "tìm giúp mình món đồ khác"
    agent, _, _, tools, _, _ = make_agent(tmp_path, {question: "product_search"}, {question: filters})
    agent.turn("c1", question)
    first, second = (f for _, f in tools.product_searches)
    assert dropped in first and dropped not in second
    for protected in ("kind", "price_min", "price_max", "in_stock"):
        assert second.get(protected) == first.get(protected)


# --- order verification: both or nothing, and nothing leaks --------------------------------------------------------------------------

def test_a_number_alone_asks_for_the_email_and_looks_nothing_up(tmp_path):
    agent, _, _, tools, answerer, _ = make_agent(tmp_path, {"đơn 88 của mình tới đâu rồi": "order_status"})
    result = agent.turn("c1", "đơn 88 của mình tới đâu rồi")
    assert result.reply == T.ASK_EMAIL and tools.lookups == [] and answerer.calls == [] and "UNFULFILLED" not in result.reply


def test_a_phone_alone_asks_for_the_email_and_the_phone_is_not_kept(tmp_path):
    text = "đơn 88, số điện thoại mình là 0912345678"
    agent, *_, tools, answerer, _ = make_agent(tmp_path, {text: "order_status"})
    result = agent.turn("c1", text)
    assert result.reply == T.ASK_EMAIL_FOR_PHONE and tools.lookups == []
    slots = agent.snapshot("c1")["slots"]
    assert slots.get("phone_seen") is True and "0912345678" not in json.dumps(slots)  # the fact is kept, the number is not


def test_an_unknown_number_and_a_wrong_email_get_the_same_refusal_and_the_same_number_of_lookups(tmp_path):
    routes = {"đơn 88, email sai@example.com": "order_status", "đơn 999, email sai@example.com": "order_status"}
    agent, _, _, tools, answerer, _ = make_agent(tmp_path, routes)
    wrong_email = agent.turn("a", "đơn 88, email sai@example.com")  # an order that exists, someone else's email
    unknown = agent.turn("b", "đơn 999, email sai@example.com")  # an order that does not exist
    assert wrong_email.reply == unknown.reply == T.REFUSE_VERIFICATION
    assert len(tools.lookups) == 2 and answerer.calls == []
    for word in ("tồn tại", "không có đơn", "không thấy đơn", "UNFULFILLED", "Áo khoác"):
        assert word not in wrong_email.reply


def test_someone_elses_order_with_my_own_email_is_refused_like_any_mismatch(tmp_path):
    text = f"xem giúp mình đơn 88 của bạn mình, email mình là {EMAIL_91}"
    agent, _, _, tools, answerer, _ = make_agent(tmp_path, {text: "order_status"})
    result = agent.turn("c1", text)
    assert result.reply == T.REFUSE_VERIFICATION and tools.lookups == [("88", EMAIL_91)] and answerer.calls == []


def test_the_email_is_matched_without_regard_to_case(tmp_path):
    text = "đơn 88, mail KhachHang10@Example.COM nha"
    agent, _, _, tools, answerer, _ = make_agent(tmp_path, {text: "order_status"})
    agent.turn("c1", text)
    assert tools.lookups == [("88", EMAIL_88)] and len(answerer.calls) == 1


def test_three_failed_verifications_disable_order_lookup_for_the_conversation_even_for_correct_credentials(tmp_path):
    messages = [f"đơn 88, email sai{i}@example.com" for i in (1, 2, 3)] + [f"đơn 88, email {EMAIL_88}", f"đơn 91, email {EMAIL_91}"]
    agent, _, _, tools, answerer, _ = make_agent(tmp_path, {m: "order_status" for m in messages})
    results = [agent.turn("c1", m) for m in messages]
    assert [r.reply for r in results[:2]] == [T.REFUSE_VERIFICATION] * 2 and results[2].reply == T.REFUSE_VERIFICATION_LOCKED
    assert results[3].reply == results[4].reply == T.LOOKUP_DISABLED
    assert len(tools.lookups) == 3 and answerer.calls == []  # the tool is not called again, whoever asks and whatever they type
    assert agent.snapshot("c1")["verify_failures"] == 3 and agent.snapshot("c1")["lookup_disabled"] is True


def test_the_counter_belongs_to_one_conversation_another_conversation_starts_again(tmp_path):
    """The known gap (A7): opening a new conversation resets the counter. A real deployment needs a limit per order number or caller."""
    bad = [f"đơn 88, email sai{i}@example.com" for i in (1, 2, 3)]
    good = f"đơn 88, email {EMAIL_88}"
    agent, _, _, tools, answerer, _ = make_agent(tmp_path, {m: "order_status" for m in [*bad, good]})
    for m in bad:
        agent.turn("a", m)
    assert agent.turn("a", good).reply == T.LOOKUP_DISABLED
    assert "88" in agent.turn("b", good).reply and len(answerer.calls) == 1  # the gap, stated and tested


def test_what_one_conversation_verified_or_was_told_never_reaches_another(tmp_path):
    text, plain = f"đơn 88, email {EMAIL_88}", "kiểm tra đơn 88 giúp mình"
    agent, _, _, tools, answerer, _ = make_agent(tmp_path, {text: "order_status", plain: "order_status"})
    agent.turn("a", text)
    result = agent.turn("b", plain)  # b gives no email: a's email must not be used for b
    assert result.reply == T.ASK_EMAIL and len(tools.lookups) == 1 and len(answerer.calls) == 1


def test_the_answerer_receives_the_order_without_an_address_phone_or_email(tmp_path):
    text = f"đơn 88, email {EMAIL_88}"
    agent, *_, answerer, _ = make_agent(tmp_path, {text: "order_status"})
    agent.turn("c1", text)
    (_, question, evidence), = answerer.calls
    assert set(evidence) == {"order"} and set(evidence["order"]) <= {"number", "status", "created", "lines", "fulfillments"}
    assert EMAIL_88 not in json.dumps(evidence) and EMAIL_88 not in json.dumps(agent.snapshot("c1").get("order") or {})


def test_the_order_number_of_a_finished_request_is_not_reused_for_the_next_one_but_the_email_is(tmp_path):
    first, second = f"đơn 88, email {EMAIL_88}", "còn đơn khác của mình thì sao"
    agent, _, _, tools, _, _ = make_agent(tmp_path, {first: "order_status", second: "order_status"})
    agent.turn("c1", first)
    result = agent.turn("c1", second)
    assert result.reply == T.ASK_ORDER_NUMBER and len(tools.lookups) == 1  # which order? the old number is not assumed
    assert agent.snapshot("c1")["slots"] == {"email": EMAIL_88}  # the customer's email stays, so it is not asked for again


# --- tickets: the only write, only after an explicit yes ---------------------------------------------------------------------------------

COMPLAINT = f"mình muốn khiếu nại đơn 88 giao thiếu hàng, email {EMAIL_88}"


def test_a_ticket_is_proposed_then_written_exactly_once_after_an_explicit_yes(tmp_path):
    agent, router, _, tools, answerer, _ = make_agent(tmp_path, {COMPLAINT: "create_ticket"})
    proposal = agent.turn("c1", COMPLAINT)
    assert proposal.awaiting == "ticket_confirmation" and tools.tickets == []  # nothing written yet
    assert "k***@example.com" in proposal.reply and EMAIL_88 not in proposal.reply and "#88" in proposal.reply
    done = agent.turn("c1", "có, tạo đi")
    assert len(tools.tickets) == 1 and tools.tickets[0]["order_number"] == "88" and tools.tickets[0]["email"] == EMAIL_88
    assert "TCK-0001" in done.reply and done.awaiting is None
    assert router.calls == [COMPLAINT]  # the confirmation was read by the rules, not routed
    assert answerer.calls == []  # the LLM wrote nothing in a ticket flow


@pytest.mark.parametrize("reply", ["không", "thôi hủy đi", "ko tạo nha"])
def test_a_no_writes_nothing_and_ends_the_flow(tmp_path, reply):
    agent, *_, tools, _, _ = make_agent(tmp_path, {COMPLAINT: "create_ticket"})
    agent.turn("c1", COMPLAINT)
    result = agent.turn("c1", reply)
    assert result.reply == T.TICKET_CANCELLED and tools.tickets == [] and result.awaiting is None


def test_an_unclear_answer_is_asked_once_more_then_the_ticket_is_cancelled_without_a_write(tmp_path):
    routes = {COMPLAINT: "create_ticket", "xin chào shop": "smalltalk"}
    agent, router, _, tools, *_ = make_agent(tmp_path, routes)
    agent.turn("c1", COMPLAINT)
    again = agent.turn("c1", "để mình nghĩ đã")
    assert again.reply == T.TICKET_REASK and again.awaiting == "ticket_confirmation"
    cancelled = agent.turn("c1", "hmm chắc vậy")
    assert cancelled.reply == T.TICKET_NOT_CONFIRMED and cancelled.awaiting is None and tools.tickets == []
    assert agent.turn("c1", "xin chào shop").intent == "smalltalk"  # the conversation is not stuck waiting
    assert router.calls == [COMPLAINT, "xin chào shop"]


def test_an_unclear_answer_followed_by_a_yes_writes_one_ticket(tmp_path):
    agent, *_, tools, _, _ = make_agent(tmp_path, {COMPLAINT: "create_ticket"})
    agent.turn("c1", COMPLAINT)
    assert agent.turn("c1", "để mình nghĩ đã").awaiting == "ticket_confirmation"
    assert "TCK-0001" in agent.turn("c1", "ok tạo đi").reply and len(tools.tickets) == 1


def test_a_conversation_that_is_abandoned_at_the_confirmation_writes_nothing(tmp_path):
    agent, *_, tools, _, _ = make_agent(tmp_path, {COMPLAINT: "create_ticket"})
    result = agent.turn("c1", COMPLAINT)
    assert result.awaiting == "ticket_confirmation" and tools.tickets == []  # and no further turn ever comes


def test_a_yes_after_the_ticket_exists_is_a_new_message_not_a_second_confirmation(tmp_path):
    routes = {COMPLAINT: "create_ticket", "có": "smalltalk"}
    agent, router, _, tools, *_ = make_agent(tmp_path, routes)
    agent.turn("c1", COMPLAINT)
    agent.turn("c1", "có")  # confirms
    after = agent.turn("c1", "có")  # a new message now: it goes to the router
    assert len(tools.tickets) == 1 and router.calls == [COMPLAINT, "có"] and after.intent == "smalltalk"


def test_a_ticket_about_an_order_needs_the_owners_email_a_wrong_one_is_refused_and_nothing_is_proposed(tmp_path):
    text = "tạo giúp mình phiếu khiếu nại đơn 88, email khachhang99@example.com"
    agent, *_, tools, _, _ = make_agent(tmp_path, {text: "create_ticket"})
    result = agent.turn("c1", text)
    assert result.reply == T.REFUSE_VERIFICATION and result.awaiting is None and tools.tickets == []


def test_a_ticket_about_an_order_without_an_email_asks_for_it_first(tmp_path):
    text = "mình muốn khiếu nại đơn 88 giao sai màu"
    agent, *_, tools, _, _ = make_agent(tmp_path, {text: "create_ticket"})
    result = agent.turn("c1", text)
    assert result.reply == T.ASK_EMAIL and tools.tickets == [] and tools.lookups == []


def test_a_ticket_without_an_order_asks_for_a_contact_email_then_proposes_then_writes(tmp_path):
    text = "nhân viên hôm qua phục vụ chưa tốt, mình muốn phản ánh"
    agent, *_, tools, _, _ = make_agent(tmp_path, {text: "create_ticket"})
    asked = agent.turn("c1", text)
    assert asked.reply == T.ASK_CONTACT_EMAIL and asked.awaiting == "email"
    proposal = agent.turn("c1", "mail mình là khachhang03@example.com")
    assert proposal.awaiting == "ticket_confirmation" and tools.lookups == [] and "#" not in proposal.reply
    agent.turn("c1", "đồng ý")
    assert len(tools.tickets) == 1 and tools.tickets[0]["order_number"] is None and tools.tickets[0]["email"] == "khachhang03@example.com"


def test_a_ticket_that_cannot_be_written_is_reported_and_traced_never_claimed(tmp_path):
    tools = FakeTools(fail_ticket=OSError("disk full"))
    agent, *_, tracer = make_agent(tmp_path, {COMPLAINT: "create_ticket"}, tools=tools)
    agent.turn("c1", COMPLAINT)
    result = agent.turn("c1", "có")
    assert result.reply == T.TICKET_FAILED and "TCK" not in result.reply
    last = trace_lines(tracer)[-1]
    assert last["status"] == "error" and last["error"]["type"] == "ticket_write_failed" and validate_trace_line(last) == []


def test_a_cap_in_the_middle_of_a_question_leaves_nothing_waiting(tmp_path):
    agent, *_ = make_agent(tmp_path, {ASK_ORDER: "order_status"}, max_steps=3)
    result = agent.turn("c1", ASK_ORDER)  # intake, route, check_slots set a question; the reply step that would send it is refused
    assert result.capped is True and result.reply == T.STEP_CAP
    state = agent.snapshot("c1")
    assert state["pending"] is None and state["clarify_count"] == 0 and state.get("awaiting") is None


# --- the step cap ------------------------------------------------------------------------------------------------------------------------


def test_the_longest_legitimate_path_is_seven_steps_and_fits_under_the_cap_of_eight(tmp_path):
    filters = {SWEATER_XL: {"kind": "Áo len nữ", "color": "Đỏ", "size": "XL"}}
    agent, *_ = make_agent(tmp_path, {SWEATER_XL: "product_search"}, filters)
    result = agent.turn("c1", SWEATER_XL)  # intake, route, extract, check_product, search, relax, compose
    assert result.steps == 7 and result.capped is False and result.reply.startswith(T.NO_FULL_MATCH)


def test_every_other_path_is_not_longer(tmp_path):
    routes = {"chào shop": "smalltalk", "hôm nay trời đẹp nhỉ": "out_of_scope", "đổi size bao lâu vậy": "policy_question",
              f"đơn 88, email {EMAIL_88}": "order_status", COMPLAINT: "create_ticket"}
    agent, *_ = make_agent(tmp_path, routes)
    steps = {m: agent.turn("c", m).steps for m in routes}
    assert steps == {"chào shop": 3, "hôm nay trời đẹp nhỉ": 3, "đổi size bao lâu vậy": 4, f"đơn 88, email {EMAIL_88}": 5, COMPLAINT: 6}
    assert agent.turn("c", "có").steps == 3  # await_confirmation, write_ticket, reply: a resume turn starts counting again from 0


def test_a_turn_that_reaches_the_cap_stops_with_a_safe_reply_and_the_request_error(tmp_path):
    filters = {SWEATER_XL: {"kind": "Áo len nữ", "color": "Đỏ", "size": "XL"}}
    agent, _, _, tools, answerer, tracer = make_agent(tmp_path, {SWEATER_XL: "product_search", "chào shop": "smalltalk"}, filters, max_steps=3)
    result = agent.turn("c1", SWEATER_XL)
    assert result.capped is True and result.reply == T.STEP_CAP and result.steps == 4  # the 4th node was refused
    assert tools.product_searches == [] and answerer.calls == []
    (line,) = trace_lines(tracer)
    assert line["status"] == "error" and line["error"]["type"] == "agent_step_cap_reached" and validate_trace_line(line) == []
    assert "used more than 3 steps" in line["error"]["message"]  # the step counter stopped it, not LangGraph's own limit behind it
    assert [(s["stage"], s["error"]["type"]) for s in line["spans"]] == [("reply", "agent_step_cap_reached")]  # on a span too: the monitor lists spans
    assert agent.snapshot("c1")["pending"] is None and agent.snapshot("c1")["clarify_count"] == 0  # nothing is left half done
    assert agent.turn("c1", "chào shop").capped is False  # the next turn is a fresh start, the conversation is not stuck


def test_a_graph_that_loops_is_stopped_by_the_cap_before_langgraph_has_to(tmp_path):
    from langgraph.graph import END, START, StateGraph

    from app.agent.graph import guarded, next_after
    from app.agent.state import AgentState

    runs = []

    def spin(state):
        runs.append(1)
        return {"goto": "spin"}  # a node that always asks to run itself again

    builder = StateGraph(AgentState)
    builder.add_node("spin", guarded("spin", spin, max_steps=8))
    builder.add_edge(START, "spin")
    builder.add_conditional_edges("spin", next_after, {"spin": "spin", END: END})
    out = builder.compile().invoke({"steps": 0}, {"recursion_limit": 30})  # LangGraph's own limit is far away
    assert out["capped"] is True and out["steps"] == 9 and len(runs) == 8 and out["reply"] == T.STEP_CAP


def test_langgraphs_recursion_limit_is_the_backstop_and_is_reported_the_same_way(tmp_path, monkeypatch):
    agent, _, _, _, _, tracer = make_agent(tmp_path, {"chào shop": "smalltalk"})
    assert agent.recursion_limit == 10  # 8 steps + the start step LangGraph counts + the one refused step: the counter must fire first

    def runaway(*args, **kwargs):
        raise GraphRecursionError("Recursion limit of 9 reached")

    monkeypatch.setattr(agent.graph, "invoke", runaway)
    result = agent.turn("c1", "chào shop")
    assert result.capped is True and result.reply == T.STEP_CAP
    (line,) = trace_lines(tracer)
    assert line["error"]["type"] == "agent_step_cap_reached" and "LangGraph recursion limit" in line["error"]["message"]
    assert [s["error"]["type"] for s in line["spans"]] == ["agent_step_cap_reached"]


def test_a_real_recursion_error_leaves_no_stuck_thread_the_next_turn_starts_from_the_beginning(tmp_path):
    filters = {SWEATER_XL: {"kind": "Áo len nữ", "color": "Đỏ", "size": "XL"}}
    agent, _, _, _, _, tracer = make_agent(tmp_path, {SWEATER_XL: "product_search", "chào shop": "smalltalk"}, filters)
    agent.recursion_limit = 3  # LangGraph's own limit, far below the step cap: the backstop fires on the 3rd node for real
    capped = agent.turn("c1", SWEATER_XL)
    assert capped.capped is True and capped.reply == T.STEP_CAP
    assert any(task.name for task in agent.graph.get_state(agent._config("c1")).tasks)  # the thread was left with a node still to run
    agent.recursion_limit = 10
    result = agent.turn("c1", "chào shop")  # a new message: it must not "resume" the stuck node
    assert result.reply == T.SMALLTALK and result.capped is False
    assert [l["error"] and l["error"]["type"] for l in trace_lines(tracer)] == ["agent_step_cap_reached", None]


# --- one trace line per turn, honest spans ---------------------------------------------------------------------------------------------


def test_every_turn_is_one_valid_trace_line_and_a_pause_for_confirmation_is_not_an_error(tmp_path):
    routes = {"chào shop": "smalltalk", "đổi size bao lâu vậy": "policy_question", COMPLAINT: "create_ticket"}
    agent, *_, tracer = make_agent(tmp_path, routes)
    for message in ("chào shop", "đổi size bao lâu vậy", COMPLAINT, "có"):
        agent.turn("c1", message)
    lines = trace_lines(tracer)
    assert len(lines) == 4 and all(validate_trace_line(l) == [] for l in lines) and all(l["status"] == "ok" for l in lines)
    assert [[s["stage"] for s in l["spans"]] for l in lines] == [["reply"], ["answer"], ["ticket_confirm"], ["ticket_confirm", "reply"]]
    assert all(s["error"] is None for l in lines for s in l["spans"])  # GraphInterrupt is a pause, not a failure
    assert lines[3]["intent"] == "create_ticket"  # the resumed turn still says what the conversation is about


def test_a_guardrail_fallback_is_a_warning_on_the_answer_span_and_the_request_is_warn(tmp_path):
    agent, *_, tracer = make_agent(tmp_path, {"đổi size bao lâu vậy": "policy_question"}, answerer=FakeAnswerer(fallback_reason="a price not in the tool results"))
    result = agent.turn("c1", "đổi size bao lâu vậy")
    (line,) = trace_lines(tracer)
    assert line["status"] == "warn" and line["error"] is None and line["spans"][0]["error"]["type"] == "answer_guardrail_fallback"
    assert result.reply  # the customer still gets an answer


def test_an_unusable_router_answer_is_a_fixed_apology_and_a_traced_error_not_a_crash(tmp_path):
    agent, *_, tracer = make_agent(tmp_path, {"???": None})
    result = agent.turn("c1", "???")
    assert result.reply == T.TECHNICAL_PROBLEM and result.intent is None
    (line,) = trace_lines(tracer)
    assert line["status"] == "error" and line["error"]["type"] == "llm_invalid_output"


def test_no_turn_uses_more_than_three_llm_calls(tmp_path):
    filters = {SWEATER_XL: {"kind": "Áo len nữ", "color": "Đỏ", "size": "XL"}}
    routes = {SWEATER_XL: "product_search", "đổi size bao lâu vậy": "policy_question", f"đơn 88, email {EMAIL_88}": "order_status"}
    agent, router, extractor, tools, answerer, _ = make_agent(tmp_path, routes, filters)
    for message in routes:
        before = len(router.calls) + len(extractor.calls) + len(answerer.calls)
        agent.turn("c1", message)
        assert len(router.calls) + len(extractor.calls) + len(answerer.calls) - before <= 3


def test_small_talk_and_out_of_scope_use_no_tool_and_no_answerer_and_reply_politely(tmp_path):
    agent, router, extractor, tools, answerer, _ = make_agent(tmp_path, {"chào shop": "smalltalk", "tỷ giá hôm nay": "out_of_scope"})
    greeting, other = agent.turn("c1", "chào shop"), agent.turn("c1", "tỷ giá hôm nay")
    assert greeting.reply == T.SMALLTALK and other.reply == T.OUT_OF_SCOPE
    assert not (tools.product_searches or tools.policy_searches or tools.lookups or tools.tickets) and answerer.calls == [] and extractor.calls == []


# --- what the code itself must not be --------------------------------------------------------------------------------------------------


def test_the_agent_is_synchronous_so_the_cancellation_addendum_does_not_apply():
    """docs/AGENT-DESIGN.md section 9: no async def and no await in app/agent. If that ever changes, asyncio.CancelledError must be
    recorded as request_interrupted before the change ships."""
    for path in sorted((REPO / "app" / "agent").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not [n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith))], path.name


def test_the_interrupt_is_in_a_node_without_a_span_and_without_a_side_effect_before_it():
    source = (REPO / "app" / "agent" / "graph.py").read_text(encoding="utf-8")
    node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == "await_confirmation")
    calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
    names = [getattr(c.func, "id", getattr(c.func, "attr", "")) for c in calls]
    assert "interrupt" in names and "span" not in names  # GraphInterrupt would be recorded as an error inside a span
    assert not any(n in names for n in ("create_ticket", "lookup_order", "search_products", "compose"))  # the node runs again on resume


def test_langsmith_tracing_is_off_and_nothing_in_the_environment_turns_it_on(monkeypatch):
    from langsmith.utils import tracing_is_enabled

    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert tracing_is_enabled() is False
