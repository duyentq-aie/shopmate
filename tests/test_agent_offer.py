"""The offer after an empty product search (docs/DECISIONS-phase7.md, D7.25 to D7.29): count which constraints block, ASK, and search again only after an explicit yes.

Offline: fake components for the unit tests, an in-memory Qdrant for the trace test. The real index is checked in tests/test_offer_integration.py."""

from __future__ import annotations

import json

import pytest

from app.agent import templates as T
from app.agent.agent import Agent
from app.agent.relax import OFFER_ORDER, drop, plan_offer
from app.agent.tools import RealTools, TicketStore
from app.retrieval.documents import Document
from app.retrieval.search import Retriever
from app.tracing import Tracer, validate_trace_line
from tests.fake_agent import FakeAnswerer, FakeExtractor, FakeRouter, FakeTools
from tests.fake_embedder import FakeEmbedder
from tests.test_agent_bounds import make_agent, trace_lines
from tests.test_retrieval_folded import _index, _new_collection

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")

# the catalogue of tests/fake_agent.py: sweaters (đỏ S,M 349k in stock; kem M,L 369k out of stock), jackets (xanh rêu M,L 459k; đen S,M,XL 429k), one hat (vàng, canvas, freesize)
BLUE_SWEATER_XL = "áo len nữ màu xanh dương size XL có không shop"
BLUE_SWEATER = {"kind": "Áo len nữ", "color": "Xanh dương", "size": "XL"}
CHEAP_SWEATER = "áo len nữ dưới 100k có không shop"
RED_WOOL_HAT = "mũ len màu đỏ size M"


def agent_with(tmp_path, routes, filters, **kwargs):
    tools = FakeTools(offers=True, **kwargs)
    return (*make_agent(tmp_path, routes, filters, tools=tools), tools)


# --- the planner: pure ----------------------------------------------------------------------------------------------------------------------


def counts_from(table):
    """A count function over the set of constraint names still present (kind is always there): {frozenset(): n, ...}."""
    return lambda filters: table.get(frozenset(k for k in filters if k != "kind"), 0)


def test_when_no_single_constraint_is_enough_they_are_dropped_cumulatively_in_the_order_size_color_material_price():
    filters = {"kind": "Áo khoác nam", "color": "Đen", "material": "Dù", "size": "L", "price_max": 500000}
    seen = []
    count = counts_from({frozenset({"price_max"}): 2, frozenset(): 9})
    offer, probes = plan_offer(filters, lambda f: seen.append(sorted(f)) or count(f))
    assert (offer.blocking, offer.dropped, offer.count) == ("material", ("size", "color", "material"), 2)
    assert offer.filters == {"kind": "Áo khoác nam", "price_max": 500000}  # the price is still there: it was not needed
    assert [p["dropped"] for p in probes] == [["size"], ["color"], ["material"], ["price"], ["size", "color"], ["size", "color", "material"]]  # in the order they were tried
    assert [p["count"] for p in probes] == [0, 0, 0, 0, 0, 2] and len(seen) == 6  # it stopped at the first cumulative step with products
    assert offer.values == {"size": {"size": "L"}, "color": {"color": "Đen"}, "material": {"material": "Dù"}}


def test_a_single_constraint_that_is_enough_is_offered_alone_and_the_others_are_kept():
    """Dropping the material alone gives a product: the offer names the material only and keeps the size and the colour (the cumulative way would have dropped all three)."""
    filters = {"kind": "Áo len nữ", "size": "L", "color": "Đen", "material": "Lụa"}
    count = counts_from({frozenset({"size", "color"}): 1, frozenset(): 9})  # kind + size + colour exists; kind alone has 9; nothing else has anything
    offer, probes = plan_offer(filters, count)
    assert (offer.blocking, offer.dropped, offer.count) == ("material", ("material",), 1)
    assert offer.filters == {"kind": "Áo len nữ", "size": "L", "color": "Đen"} and offer.values == {"material": {"material": "Lụa"}}
    assert [p["dropped"] for p in probes] == [["size"], ["color"], ["material"]] and [p["count"] for p in probes] == [0, 0, 1]  # no cumulative step was tried


def test_the_first_single_constraint_in_the_order_wins_when_several_are_enough():
    filters = {"kind": "Mũ", "size": "M", "color": "Đỏ", "material": "Len"}
    table = {frozenset({"size", "material"}): 3, frozenset({"size", "color"}): 5}  # dropping the colour alone gives 3, dropping the material alone gives 5; the size alone gives 0
    offer, probes = plan_offer(filters, counts_from(table))
    assert (offer.dropped, offer.count) == (("color",), 3) and offer.filters == {"kind": "Mũ", "size": "M", "material": "Len"}
    assert [p["dropped"] for p in probes] == [["size"], ["color"]]  # it stopped at the first one: the material was never counted


def test_at_most_seven_counts_four_alone_and_three_together_in_this_order():
    filters = {"kind": "Mũ", "size": "M", "color": "Đỏ", "material": "Len", "price_min": 5, "price_max": 9}
    offer, probes = plan_offer(filters, lambda f: 0)
    assert offer is None and len(probes) == 7
    assert [p["dropped"] for p in probes] == [["size"], ["color"], ["material"], ["price"], ["size", "color"], ["size", "color", "material"], ["size", "color", "material", "price"]]
    assert [len(plan_offer({"kind": "Mũ", **{k: v for k, v in filters.items() if k in keep}}, lambda f: 0)[1]) for keep in ({"price_max"}, {"size", "color"}, {"size", "color", "material"})] == [1, 3, 5]  # 2n - 1 counts for n constraints


def test_price_comes_last_and_is_only_offered_when_everything_else_was_not_enough():
    filters = {"kind": "Mũ", "color": "Đỏ", "price_min": 10000, "price_max": 50000}
    offer, probes = plan_offer(filters, counts_from({frozenset(): 4}))
    assert (offer.blocking, offer.dropped, offer.count) == ("price", ("color", "price"), 4)
    assert offer.values["price"] == {"price_min": 10000, "price_max": 50000} and offer.filters == {"kind": "Mũ"}  # both ends of the price go together
    assert [p["dropped"] for p in probes] == [["color"], ["price"], ["color", "price"]]


def test_with_nothing_to_drop_or_nothing_found_even_then_there_is_no_offer_and_kind_is_never_dropped():
    assert plan_offer({"kind": "Mũ"}, lambda f: pytest.fail("nothing to count"))[0] is None
    assert plan_offer({"kind": "Mũ", "in_stock": True, "category": "phu-kien"}, lambda f: pytest.fail("in_stock and category are not offered away"))[0] is None
    seen = []
    offer, probes = plan_offer({"kind": "Quần jean", "color": "Đen", "size": "30", "price_max": 1}, lambda f: seen.append(dict(f)) or 0)
    assert offer is None and len(probes) == 5  # size, color and price alone, then size and color, then all three
    assert all(f.get("kind") == "Quần jean" for f in seen)  # never counted without the kind


@pytest.mark.parametrize("name", ["kind", "category", "in_stock", "doc_type", "price_max", "anything"])
def test_only_the_four_named_constraints_can_be_dropped(name):
    assert OFFER_ORDER == ("size", "color", "material", "price")
    with pytest.raises(ValueError):
        drop({"kind": "Mũ", "price_max": 1}, name)


# --- the reply -------------------------------------------------------------------------------------------------------------------------------


def test_after_the_relaxation_found_nothing_the_reply_names_the_blocking_constraint_and_invites_without_listing_a_product(tmp_path):
    agent, _, _, _, answerer, tracer, tools = agent_with(tmp_path, {BLUE_SWEATER_XL: "product_search"}, {BLUE_SWEATER_XL: BLUE_SWEATER})
    result = agent.turn("c1", BLUE_SWEATER_XL)
    assert result.hits == [] and result.relaxed == ["size"] and result.awaiting == "offer_confirmation" and answerer.calls == []  # no LLM answer, nothing listed
    assert result.reply == ("Mình không tìm thấy sản phẩm nào khớp tất cả yêu cầu của bạn. Điều kiện đang chặn kết quả: màu xanh dương (cùng với kích cỡ XL). "
                            "Hiện có 2 áo len nữ nếu bỏ các điều kiện này — bạn muốn xem không?")
    assert T.NO_FULL_MATCH in result.reply and "SM-" not in result.reply and result.reply_source == "template" and result.steps == 7
    assert len(tools.product_searches) == 2  # the search and the one relaxation: the offer searched nothing
    assert tools.counts and all(f.get("kind") == "Áo len nữ" for f in tools.counts)  # kind was in every count


def test_a_price_that_blocks_is_offered_not_dropped(tmp_path):
    agent, _, _, _, _, _, tools = agent_with(tmp_path, {CHEAP_SWEATER: "product_search"}, {CHEAP_SWEATER: {"kind": "Áo len nữ", "price_max": 100000}})
    result = agent.turn("c1", CHEAP_SWEATER)
    assert result.reply == ("Mình không tìm thấy sản phẩm nào khớp tất cả yêu cầu của bạn. Điều kiện đang chặn kết quả: mức giá tối đa 100.000đ. "
                            "Hiện có 2 áo len nữ nếu bỏ điều kiện này — bạn muốn xem không?")
    assert len(tools.product_searches) == 1 and tools.product_searches[0][1] == {"kind": "Áo len nữ", "price_max": 100000}  # the price was never dropped by itself
    assert result.relaxed == [] and result.awaiting == "offer_confirmation"


def test_the_offer_names_every_dropped_constraint_with_the_customers_own_value(tmp_path):
    agent, *_ = agent_with(tmp_path, {RED_WOOL_HAT: "product_search"}, {RED_WOOL_HAT: {"kind": "Mũ", "color": "Đỏ", "material": "Len", "size": "M"}})
    reply = agent.turn("c1", RED_WOOL_HAT).reply
    assert "Điều kiện đang chặn kết quả: chất liệu len (cùng với kích cỡ M và màu đỏ). Hiện có 1 mũ nếu bỏ các điều kiện này" in reply


def test_when_dropping_everything_but_the_kind_finds_nothing_the_plain_reply_stays(tmp_path):
    question = "quần jean đen cho mình xem"
    agent, _, _, _, _, _, tools = agent_with(tmp_path, {question: "product_search"}, {question: {"kind": "Quần jean", "color": "Đen"}})
    result = agent.turn("c1", question)
    assert result.reply == T.no_results_relaxed("color") and result.awaiting is None and result.reply_source == "template"
    assert tools.counts == [{"kind": "Quần jean"}]  # kind was never dropped, not even to look


def test_with_offers_off_nothing_changes(tmp_path):
    agent, *_ = make_agent(tmp_path, {BLUE_SWEATER_XL: "product_search"}, {BLUE_SWEATER_XL: BLUE_SWEATER}, tools=FakeTools())
    result = agent.turn("c1", BLUE_SWEATER_XL)
    assert result.reply == T.no_results_relaxed("size") and result.awaiting is None


# --- the customer's answer ------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("yes", ["có", "Có nhé", "ok", "vâng", "đồng ý"])
def test_an_explicit_yes_searches_again_without_exactly_the_offered_constraints_and_answers_with_the_label(tmp_path, yes):
    agent, router, extractor, _, answerer, _, tools = agent_with(tmp_path, {BLUE_SWEATER_XL: "product_search"}, {BLUE_SWEATER_XL: BLUE_SWEATER})
    agent.turn("c1", BLUE_SWEATER_XL)
    result = agent.turn("c1", yes)
    assert router.calls == [BLUE_SWEATER_XL] and extractor.calls == [BLUE_SWEATER_XL]  # neither was asked again: no LLM before the answer
    assert tools.product_searches[-1] == (BLUE_SWEATER_XL, {"kind": "Áo len nữ"})  # the original request, without size and color: kind stayed
    assert [h["sku"] for h in result.hits] == ["SM-NU-0003", "SM-NU-0004"] and result.steps == 3 and result.intent == "product_search"
    assert result.reply.startswith("Không khớp hoàn toàn: khác kích cỡ và màu sắc.\n") and "SM-NU-0003" in result.reply
    assert result.relaxed == ["size", "color"] and result.awaiting is None and len(answerer.calls) == 1
    kind, question, evidence = answerer.calls[0]
    assert question == BLUE_SWEATER_XL and evidence["relaxed"] == ["size", "color"]  # the model is told what was dropped; the label itself is code


def test_a_price_is_dropped_only_after_the_customer_says_yes(tmp_path):
    agent, _, _, _, _, _, tools = agent_with(tmp_path, {CHEAP_SWEATER: "product_search"}, {CHEAP_SWEATER: {"kind": "Áo len nữ", "price_max": 100000}})
    agent.turn("c1", CHEAP_SWEATER)
    assert all("price_max" in f for _, f in tools.product_searches)
    result = agent.turn("c1", "có")
    assert tools.product_searches[-1][1] == {"kind": "Áo len nữ"} and result.reply.startswith("Không khớp hoàn toàn: khác mức giá.\n")


@pytest.mark.parametrize("answer", ["không", "Không!", "thôi", "thôi khỏi", "không cần đâu", "không, cảm ơn bạn nhé", "ko"])
def test_a_plain_refusal_gets_a_fixed_reply_without_the_router_the_llm_or_a_search_and_the_offer_is_gone(tmp_path, answer):
    agent, router, extractor, _, answerer, tracer, tools = agent_with(tmp_path, {BLUE_SWEATER_XL: "product_search"}, {BLUE_SWEATER_XL: BLUE_SWEATER})  # the router raises on any other text
    agent.turn("c1", BLUE_SWEATER_XL)
    searches, counts = len(tools.product_searches), len(tools.counts)
    result = agent.turn("c1", answer)
    assert router.calls == [BLUE_SWEATER_XL] and extractor.calls == [BLUE_SWEATER_XL] and answerer.calls == []  # 0 LLM calls for the refusal
    assert len(tools.product_searches) == searches and len(tools.counts) == counts  # nothing was searched or counted
    assert result.reply == T.OFFER_DECLINED and result.reply_source == "template" and result.steps == 2 and result.awaiting is None and result.intent == "product_search"
    assert agent.snapshot("c1")["offer"] is None
    line = trace_lines(tracer)[-1]
    assert [s["stage"] for s in line["spans"]] == ["reply"] and line["spans"][0]["summary"] == {"template": "offer_declined"} and line["intent"] == "product_search"
    assert line["reply_source"] == "template" and line["status"] == "ok" and validate_trace_line(line) == []


@pytest.mark.parametrize("message", ["không, cho mình xem áo khoác", "thôi, mình tìm áo thun nhé", "không phải áo len, áo khoác", "không có size L thì thôi"])
def test_a_refusal_that_asks_for_something_else_goes_to_the_router_like_any_new_message(tmp_path, message):
    routes = {BLUE_SWEATER_XL: "product_search", message: "smalltalk", "có": "smalltalk"}
    agent, router, _, _, answerer, _, tools = agent_with(tmp_path, routes, {BLUE_SWEATER_XL: BLUE_SWEATER})
    agent.turn("c1", BLUE_SWEATER_XL)
    searches = len(tools.product_searches)
    result = agent.turn("c1", message)
    assert router.calls == [BLUE_SWEATER_XL, message] and result.reply == T.SMALLTALK and len(tools.product_searches) == searches  # the offer was not accepted by it either
    assert agent.snapshot("c1")["offer"] is None
    again = agent.turn("c1", "có")  # too late: the offer was spent by the message before
    assert router.calls[-1] == "có" and len(tools.product_searches) == searches and again.reply == T.SMALLTALK


def test_a_bare_no_without_an_offer_is_an_ordinary_message_for_the_router(tmp_path):
    agent, router, *_ = agent_with(tmp_path, {"không": "smalltalk"}, {})
    assert agent.turn("c1", "không").reply == T.SMALLTALK and router.calls == ["không"]  # the fixed refusal exists only while an offer is waiting


def test_the_synthetic_catalogue_where_dropping_the_material_alone_is_enough_keeps_the_size_and_the_colour_all_the_way(tmp_path):
    """Dropping the material alone gives a black size-L silk-free sweater; the cumulative way would have offered to drop size and colour as well."""
    sweater = {"key": "product:SM-X-0001", "sku": "SM-X-0001", "name": "Áo len nữ đen thử", "kind": "Áo len nữ", "color": "Đen", "material": "Len", "sizes": ["L"],
               "price_vnd": 300000, "in_stock": True}
    question = "áo len nữ đen lụa size L"
    filters = {question: {"kind": "Áo len nữ", "color": "Đen", "material": "Lụa", "size": "L"}}
    agent, _, _, _, _, tracer, tools = agent_with(tmp_path, {question: "product_search"}, filters, products=[sweater])
    offered = agent.turn("c1", question)
    assert offered.reply == ("Mình không tìm thấy sản phẩm nào khớp tất cả yêu cầu của bạn. Điều kiện đang chặn kết quả: chất liệu lụa. "
                             "Hiện có 1 áo len nữ kích cỡ L màu đen nếu bỏ điều kiện này — bạn muốn xem không?")
    assert [c for c in tools.counts] == [{"kind": "Áo len nữ", "color": "Đen", "material": "Lụa"}, {"kind": "Áo len nữ", "material": "Lụa", "size": "L"},
                                         {"kind": "Áo len nữ", "color": "Đen", "size": "L"}]  # size alone, colour alone, material alone: it stopped there
    accepted = agent.turn("c1", "có")
    assert tools.product_searches[-1][1] == {"kind": "Áo len nữ", "color": "Đen", "size": "L"}  # size and colour are still in the search
    assert [h["sku"] for h in accepted.hits] == ["SM-X-0001"] and accepted.relaxed == ["material"]
    assert accepted.reply.startswith("Không khớp hoàn toàn: khác chất liệu.\n")


def test_another_topic_is_a_new_turn_and_it_spends_the_offer(tmp_path):
    other = "đổi size bao lâu vậy"
    routes = {BLUE_SWEATER_XL: "product_search", other: "policy_question", "có": "smalltalk"}
    agent, router, _, _, _, _, tools = agent_with(tmp_path, routes, {BLUE_SWEATER_XL: BLUE_SWEATER})
    agent.turn("c1", BLUE_SWEATER_XL)
    policy = agent.turn("c1", other)
    assert policy.intent == "policy_question" and "SM-" not in policy.reply
    searches = len(tools.product_searches)
    assert agent.turn("c1", "có").reply == T.SMALLTALK and len(tools.product_searches) == searches  # the yes comes one message too late


@pytest.mark.parametrize("message", ["có, nhưng cho mình xem áo khoác", "có ai không", "có hàng không shop"])
def test_a_yes_with_something_else_in_it_is_not_an_acceptance(tmp_path, message):
    routes = {BLUE_SWEATER_XL: "product_search", message: "smalltalk"}
    agent, router, _, _, _, _, tools = agent_with(tmp_path, routes, {BLUE_SWEATER_XL: BLUE_SWEATER})
    agent.turn("c1", BLUE_SWEATER_XL)
    searches = len(tools.product_searches)
    agent.turn("c1", message)
    assert router.calls[-1] == message and len(tools.product_searches) == searches  # the same parser as the ticket: only a plain yes counts


def test_an_offer_belongs_to_its_conversation(tmp_path):
    routes = {BLUE_SWEATER_XL: "product_search", "có": "smalltalk"}
    agent, router, _, _, _, _, tools = agent_with(tmp_path, routes, {BLUE_SWEATER_XL: BLUE_SWEATER})
    agent.turn("mine", BLUE_SWEATER_XL)
    other = agent.turn("someone else", "có")  # a yes in another conversation accepts nothing
    assert router.calls[-1] == "có" and other.reply == T.SMALLTALK and len(tools.product_searches) == 2


def test_an_empty_catalogue_gives_no_offer(tmp_path):
    agent, _, _, _, _, _, tools = agent_with(tmp_path, {BLUE_SWEATER_XL: "product_search"}, {BLUE_SWEATER_XL: BLUE_SWEATER}, products=[])
    result = agent.turn("c1", BLUE_SWEATER_XL)
    assert tools.counts and result.awaiting is None and agent.snapshot("c1").get("offer") is None and result.reply == T.no_results_relaxed("size")


def test_an_accepted_offer_whose_search_is_empty_says_so_plainly_and_makes_no_new_offer(tmp_path):
    agent, _, _, _, _, _, tools = agent_with(tmp_path, {BLUE_SWEATER_XL: "product_search"}, {BLUE_SWEATER_XL: BLUE_SWEATER})
    agent.turn("c1", BLUE_SWEATER_XL)
    tools.products = []  # the stock vanished between the offer and the yes
    result = agent.turn("c1", "có")
    assert result.hits == [] and result.awaiting is None and T.NO_FULL_MATCH in result.reply and agent.snapshot("c1")["offer"] is None
    assert len(tools.offer_requests) == 1  # offer_for ran for the first message only


def test_the_step_cap_and_the_other_bounds_are_untouched(tmp_path):
    agent, *_ = agent_with(tmp_path, {BLUE_SWEATER_XL: "product_search"}, {BLUE_SWEATER_XL: BLUE_SWEATER})
    first = agent.turn("c1", BLUE_SWEATER_XL)
    assert first.steps == 7 and first.capped is False  # intake, route, extract, check_product, search, relax, suggest: still the longest path
    tight, *_ = make_agent(tmp_path / "tight", {BLUE_SWEATER_XL: "product_search"}, {BLUE_SWEATER_XL: BLUE_SWEATER}, tools=FakeTools(offers=True), max_steps=6)
    assert tight.turn("c", BLUE_SWEATER_XL).capped  # the seventh step (suggest) is refused like any other past the cap


def test_a_ticket_flow_after_an_offer_is_the_same_as_always(tmp_path):
    complaint = "giao nhầm màu rồi shop"
    routes = {BLUE_SWEATER_XL: "product_search", complaint: "create_ticket"}
    agent, _, _, _, _, _, tools = agent_with(tmp_path, routes, {BLUE_SWEATER_XL: BLUE_SWEATER})
    agent.turn("c1", BLUE_SWEATER_XL)
    for message in (complaint, "khachhang02@example.com"):
        agent.turn("c1", message)
    assert tools.tickets == []  # nothing is written before the explicit yes
    assert "TCK-" in agent.turn("c1", "có").reply and len(tools.tickets) == 1


# --- what the trace says -------------------------------------------------------------------------------------------------------------------


def _real_tools(tmp_path):
    def doc(sku, name, kind, color, material, sizes, price):
        return Document(f"product:{sku}", f"{name} {color} {material}", {"doc_type": "product", "sku": sku, "name": name, "category": "thoi-trang-nu", "kind": kind, "price_vnd": price,
                                                                         "color": color, "material": material, "sizes": sizes, "in_stock": True})

    docs = [doc("SM-NU-0001", "Áo len nữ đỏ", "Áo len nữ", "Đỏ", "Len", ["S", "M"], 349000), doc("SM-NU-0002", "Áo len nữ kem", "Áo len nữ", "Kem", "Len", ["M", "L"], 369000),
            doc("SM-NU-0003", "Áo len nữ đen", "Áo len nữ", "Đen", "Len pha cotton", ["S"], 399000), doc("SM-PK-0004", "Mũ lưỡi trai vàng", "Mũ", "Vàng", "Vải canvas", ["Freesize"], 129000)]
    client = _new_collection()
    _index(client, docs)
    return RealTools(Retriever(client, FakeEmbedder(), collection="v2"), None, TicketStore(tmp_path / "t.jsonl")), client


def test_the_trace_of_an_offer_and_of_its_acceptance(tmp_path):
    tools, _ = _real_tools(tmp_path)
    tracer = Tracer(tmp_path / "logs" / "traces.jsonl", secrets=[])
    question = "áo len nữ màu xanh dương size XL"
    agent = Agent(router=FakeRouter({question: "product_search"}), extractor=FakeExtractor({question: BLUE_SWEATER}), tools=tools, answerer=FakeAnswerer(), tracer=tracer)
    offered, accepted = agent.turn("c1", question), agent.turn("c1", "có")
    first, second = trace_lines(tracer)
    assert all(validate_trace_line(line) == [] for line in (first, second))
    stages = [s["stage"] for s in first["spans"]]
    assert stages == ["retrieval", "retrieval", "retrieval", "reply"]  # the search, the relaxed search, the counts; no router or extractor span (fakes), no answer
    count = first["spans"][2]["summary"]
    assert count["mode"] == "count" and count["blocking_constraint"] == "color" and count["offer_count"] == 3 and count["offer_drops"] == ["size", "color"]
    assert count["probes"] == [{"dropped": ["size"], "count": 0}, {"dropped": ["color"], "count": 0}, {"dropped": ["size", "color"], "count": 3}] and count["hits"] == []  # in the order tried
    assert first["spans"][3]["summary"] == {"template": "offer_invite"} and first["reply_source"] == "template" and first["status"] == "ok" and first["reply"] == offered.reply
    assert first["spans"][0]["summary"]["hits"] == [] and first["spans"][1]["summary"]["hits"] == []
    # the yes: no router, no extractor, a search without size and color, and the trace says so
    assert [s["stage"] for s in second["spans"]] == ["retrieval", "answer"] and second["intent"] == "product_search" and second["turn"] == 2
    assert second["spans"][0]["summary"]["offer_drops"] == ["size", "color"] and [h["kind"] for h in second["spans"][0]["summary"]["hits"]] == ["product"] * 3
    assert second["reply"].startswith("Không khớp hoàn toàn: khác kích cỡ và màu sắc.") and accepted.reply == second["reply"]


def test_the_real_count_is_exact_and_agrees_with_the_real_search(tmp_path):
    tools, _ = _real_tools(tmp_path)
    assert tools.retriever.count({"doc_type": "product", "kind": "Áo len nữ"}) == 3
    assert tools.retriever.count({"doc_type": "product", "kind": "Áo len nữ", "color": "Đen", "size": "M"}) == 0
    assert tools.retriever.count({"doc_type": "product", "kind": "Áo len nữ", "price_max": 360000}) == 1
    assert tools.retriever.count({"doc_type": "product", "kind": "Mũ"}) == 1
    for filters in ({"kind": "Áo len nữ"}, {"kind": "Áo len nữ", "color": "Đen"}, {"kind": "Mũ", "color": "Đỏ"}):  # a count above 0 is a search with hits and the other way round
        assert (tools.retriever.count({**filters, "doc_type": "product"}) > 0) == bool(tools.search_products("áo", filters))
    offer = tools.offer_for({"kind": "Áo len nữ", "color": "Xanh dương", "size": "XL"})
    assert (offer.blocking, offer.dropped, offer.count) == ("color", ("size", "color"), 3)
    assert tools.offer_for({"kind": "Áo len nữ"}) is None and tools.offer_for({"kind": "Quần", "color": "Đen"}) is None


def test_a_count_that_fails_is_a_qdrant_error_of_the_retrieval_stage_and_the_reply_is_not_made_up(tmp_path):
    tools, _ = _real_tools(tmp_path)
    tracer = Tracer(tmp_path / "logs" / "traces.jsonl", secrets=[])
    question = "áo len nữ màu xanh dương size XL"
    agent = Agent(router=FakeRouter({question: "product_search"}), extractor=FakeExtractor({question: BLUE_SWEATER}), tools=tools, answerer=FakeAnswerer(), tracer=tracer,
                  catch_errors=True)

    def broken(*args, **kwargs):
        raise RuntimeError("the counts went wrong")

    tools.retriever.count = broken
    result = agent.turn("c1", question)
    line = json.loads((tmp_path / "logs" / "traces.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert result.reply == T.TECHNICAL_PROBLEM and result.reply_source == "error_template" and line["status"] == "error"
    assert line["spans"][-1]["stage"] == "retrieval" and line["spans"][-1]["error"]["type"] == "internal_error"  # a bug, said as a bug
