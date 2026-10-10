"""The real tools (app/agent/tools.py): order lookup with every IDOR case through the REAL read-only Saleor client over a fake transport,
tickets on a real file, retrieval over an in-memory index. Nothing here reaches Saleor, Gemini or the real tickets file."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import httpx
import pytest

from app.agent import templates as T
from app.agent.agent import Agent
from app.agent.protocols import OrderLookup
from app.agent.tools import POLICY_K, PRODUCT_K, TICKETS_PATH, RealTools, TicketStore, TicketWriteError
from app.retrieval.search import Retriever
from app.saleor.readonly_client import ALLOWED_OPERATIONS, ReadOnlySaleorClient
from app.tracing import Tracer, classify_exception, validate_trace_line
from tests.fake_agent import FakeAnswerer, FakeExtractor, FakeRouter
from tests.fake_embedder import FakeEmbedder
from tests.test_retrieval_folded import _documents, _index, _new_collection

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")
REPO = Path(__file__).resolve().parent.parent
OWNER = "khachhang10@example.com"
NODE = {"id": "T3JkZXI6MQ==", "number": "88", "externalReference": "shopmate-seed-order-005", "status": "UNFULFILLED", "created": "2026-09-01T10:00:00+07:00",
        "userEmail": OWNER, "channel": {"slug": "vn"}, "shippingMethodName": "Giao nhanh", "subtotal": {"gross": {"amount": 1, "currency": "VND"}},
        "shippingPrice": {"gross": {"amount": 0, "currency": "VND"}}, "total": {"gross": {"amount": 1, "currency": "VND"}},
        "lines": [{"productSku": "SM-NAM-0002", "productName": "Áo khoác dù nam đen", "quantity": 2, "quantityFulfilled": 1, "variant": {"sku": "SM-NAM-0002"}}],
        "fulfillments": [{"status": "FULFILLED", "trackingNumber": "VN123456", "created": "2026-09-03T09:00:00+07:00"}]}


class FakeSaleor(httpx.BaseTransport):
    """Answers OrderByNumber from a table of nodes; records every request."""

    def __init__(self, nodes: dict[str, dict] | None = None, status: int = 200, error: Exception | None = None):
        self.nodes = {"88": NODE} if nodes is None else nodes
        self.requests, self.status, self.error = [], status, error

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if self.error is not None:
            raise self.error
        if self.status != 200:
            return httpx.Response(self.status, text="boom")
        number = self.requests[-1]["variables"]["number"]
        node = self.nodes.get(number)
        return httpx.Response(200, json={"data": {"orders": {"edges": [{"node": node}] if node else []}}})


def tools_with(transport: FakeSaleor, tmp_path, retriever=None) -> RealTools:
    return RealTools(retriever, ReadOnlySaleorClient("http://saleor.test/graphql/", "t" * 30, transport=transport), TicketStore(tmp_path / "t.jsonl"))


# --- lookup_order: both or nothing ----------------------------------------------------------------------------------------------------


def test_the_owner_with_the_right_number_gets_the_minimal_order(tmp_path):
    result = tools_with(FakeSaleor(), tmp_path).lookup_order("88", OWNER)
    assert result.verified and set(result.order) == {"number", "status", "created", "lines", "fulfillments"}
    assert result.order["number"] == "88" and result.order["status"] == "UNFULFILLED"
    assert result.order["lines"] == [{"name": "Áo khoác dù nam đen", "sku": "SM-NAM-0002", "quantity": 2, "quantity_fulfilled": 1}]
    assert result.order["fulfillments"] == [{"status": "FULFILLED", "tracking_number": "VN123456", "created": "2026-09-03T09:00:00+07:00"}]
    text = json.dumps(result.order)
    for forbidden in (OWNER, "khachhang", "externalReference", "shopmate-seed", "Giao nhanh", "amount"):
        assert forbidden not in text  # no email, no internal reference, no totals


@pytest.mark.parametrize("email", ["sai@example.com", "khachhang01@example.com", "khachhang10@example.org", "xkhachhang10@example.com",
                                   "khachhang10@example.comx", "", "khachhang10", "@example.com", "a@b"])
def test_a_wrong_email_is_not_verified_and_looks_exactly_like_an_unknown_number(tmp_path, email):
    tools = tools_with(FakeSaleor(), tmp_path)
    wrong_email = tools.lookup_order("88", email)
    unknown_number = tools.lookup_order("999", email)
    assert wrong_email == unknown_number == OrderLookup(verified=False, order=None)


def test_a_right_email_with_the_wrong_number_or_another_owners_order_is_not_verified(tmp_path):
    other = {**NODE, "number": "91", "userEmail": "khachhang02@example.com"}
    tools = tools_with(FakeSaleor({"88": NODE, "91": other}), tmp_path)
    assert tools.lookup_order("91", OWNER) == OrderLookup(False)  # the owner of 88 asks for the order of someone else
    assert tools.lookup_order("88", "khachhang02@example.com") == OrderLookup(False)
    assert tools.lookup_order("91", "khachhang02@example.com").verified is True  # and the real owner of 91 is served


@pytest.mark.parametrize("given", ["KHACHHANG10@EXAMPLE.COM", "  khachhang10@example.com  ", "KhachHang10@Example.com"])
def test_the_email_comparison_ignores_case_and_surrounding_blanks(tmp_path, given):
    assert tools_with(FakeSaleor(), tmp_path).lookup_order("88", given).verified is True


@pytest.mark.parametrize("stored", ["KhachHang10@Example.com", "KHACHHANG10@EXAMPLE.COM", " khachhang10@example.com "])
def test_the_owners_email_is_compared_without_regard_to_the_case_it_is_stored_in(tmp_path, stored):
    transport = FakeSaleor({"88": {**NODE, "userEmail": stored}})
    assert tools_with(transport, tmp_path).lookup_order("88", OWNER).verified is True


def test_an_order_of_another_channel_is_never_revealed_even_to_its_owner(tmp_path):
    foreign = {**NODE, "channel": {"slug": "default-channel"}}
    assert tools_with(FakeSaleor({"88": foreign}), tmp_path).lookup_order("88", OWNER) == OrderLookup(False)


@pytest.mark.parametrize("node", [{**NODE, "userEmail": None}, {**NODE, "userEmail": ""}, {**NODE, "number": "89"}, {**NODE, "channel": None}])
def test_an_order_without_an_owner_email_or_that_does_not_match_the_asked_number_is_not_verified(tmp_path, node):
    assert tools_with(FakeSaleor({"88": node}), tmp_path).lookup_order("88", OWNER) == OrderLookup(False)
    assert tools_with(FakeSaleor({"88": node}), tmp_path).lookup_order("88", "") == OrderLookup(False)  # an empty email never matches an empty owner


@pytest.mark.parametrize("number", ["", "abc", "88 OR 1=1", "88; DROP", "88\n", "-1", "8.8", "123456789", "0x58", "٨٨", "88 ", " 88", "#88", "ｖ88"])
def test_a_value_that_cannot_be_an_order_number_is_not_even_sent_to_saleor(tmp_path, number):
    transport = FakeSaleor()
    assert tools_with(transport, tmp_path).lookup_order(number, OWNER) == OrderLookup(False)
    assert transport.requests == []


def test_a_bad_email_shape_is_not_sent_either(tmp_path):
    transport = FakeSaleor()
    for email in ("", "not an email", "a b@c.dd", "@@x.yy"):
        assert tools_with(transport, tmp_path).lookup_order("88", email) == OrderLookup(False)
    assert transport.requests == []


def test_one_lookup_is_one_allowlisted_read_only_query_that_selects_no_address_or_phone(tmp_path):
    transport = FakeSaleor()
    tools_with(transport, tmp_path).lookup_order("88", OWNER)
    (request,) = transport.requests
    assert request["operationName"] == "OrderByNumber" and request["variables"] == {"number": "88"}
    assert request["query"].lstrip().startswith("query ") and "mutation" not in request["query"].lower()
    for field in ("address", "Address", "phone", "Phone"):
        assert field not in request["query"]  # the document cannot return what it does not ask for
    assert "OrderByNumber" in ALLOWED_OPERATIONS


def test_a_saleor_failure_is_an_error_not_a_failed_verification(tmp_path):
    for transport in (FakeSaleor(status=500), FakeSaleor(error=httpx.ConnectError("down")), FakeSaleor(error=httpx.ReadTimeout("slow"))):
        with pytest.raises(Exception) as caught:
            tools_with(transport, tmp_path).lookup_order("88", OWNER)
        assert type(caught.value).__name__ == "SaleorQueryError"


def test_without_a_saleor_client_a_lookup_is_an_error_not_a_refusal(tmp_path):
    with pytest.raises(RuntimeError, match="not configured"):
        RealTools(None).lookup_order("88", OWNER)


def test_the_comparison_is_constant_time_and_the_tool_has_no_write_path_to_saleor():
    source = (REPO / "app" / "agent" / "tools.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source
    for word in ("mutation", "seedlib", "SEEDER", "orderUpdate", "orderCancel"):
        assert word not in source


def test_the_order_lookup_span_is_clean_on_a_refusal_and_carries_the_real_error_on_an_outage(tmp_path):
    tracer = Tracer(tmp_path / "traces.jsonl")
    tools = tools_with(FakeSaleor(), tmp_path)
    with tracer.request("q"):
        assert tools.lookup_order("88", "sai@example.com").verified is False
    with pytest.raises(Exception):
        with tracer.request("q"):
            tools_with(FakeSaleor(error=httpx.ReadTimeout("slow")), tmp_path).lookup_order("88", OWNER)
    refused, outage = [json.loads(x) for x in tracer.path.read_text(encoding="utf-8").splitlines()]
    assert [(s["stage"], s["error"]) for s in refused["spans"]] == [("order_lookup", None)] and refused["status"] == "ok"
    assert [(s["stage"], s["error"]["type"]) for s in outage["spans"]] == [("order_lookup", "saleor_timeout")] and outage["error"]["type"] == "saleor_timeout"
    assert validate_trace_line(refused) == [] and validate_trace_line(outage) == []


# --- tickets ---------------------------------------------------------------------------------------------------------------------------


def test_a_ticket_is_one_json_line_with_its_idempotency_key(tmp_path):
    store = TicketStore(tmp_path / "data" / "tickets" / "tickets.jsonl")
    result = store.create(conversation_id="c1", summary="Nội dung: giao thiếu hàng", order_number="88", contact_email=OWNER)
    assert result["created"] is True and result["ticket_id"].startswith("TCK-")
    (line,) = store.path.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["ticket_id"] == result["ticket_id"] and record["order_number"] == "88" and record["contact_email"] == OWNER
    assert record["conversation_id"] == "c1" and record["status"] == "open" and record["idempotency_key"] == TicketStore.key("c1", "88", record["summary"])


def test_the_same_ticket_is_written_once_even_if_confirmed_twice(tmp_path):
    store = TicketStore(tmp_path / "t.jsonl")
    args = dict(conversation_id="c1", summary="s", order_number=None, contact_email=OWNER)
    first, second = store.create(**args), store.create(**args)
    assert first["ticket_id"] == second["ticket_id"] and (first["created"], second["created"]) == (True, False)
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 1


def test_another_conversation_or_another_order_or_summary_is_another_ticket(tmp_path):
    store = TicketStore(tmp_path / "t.jsonl")
    ids = {store.create(conversation_id=c, summary=s, order_number=o, contact_email=OWNER)["ticket_id"]
           for c, s, o in (("c1", "s", None), ("c2", "s", None), ("c1", "s", "88"), ("c1", "other", None))}
    assert len(ids) == 4 and len(store.path.read_text(encoding="utf-8").splitlines()) == 4


def test_concurrent_confirmations_of_one_ticket_write_one_line(tmp_path):
    store = TicketStore(tmp_path / "t.jsonl")
    threads = [threading.Thread(target=lambda: store.create(conversation_id="c1", summary="s", order_number=None, contact_email=OWNER)) for _ in range(8)]
    [t.start() for t in threads], [t.join() for t in threads]
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 1


def test_a_ticket_that_cannot_be_written_raises_a_ticket_write_error_and_is_traced_as_such(tmp_path):
    blocked = tmp_path / "t.jsonl"
    blocked.mkdir()  # a directory where the file should be
    with pytest.raises(TicketWriteError):
        TicketStore(blocked).create(conversation_id="c1", summary="s", order_number=None, contact_email=OWNER)
    kind, message = classify_exception(TicketWriteError("OSError: disk full"), "ticket_confirm")
    assert kind == "ticket_write_failed" and "TicketWriteError" in message


def test_the_tickets_file_is_git_ignored_and_the_default_path_is_the_agreed_one():
    assert TICKETS_PATH == REPO / "data" / "tickets" / "tickets.jsonl"
    ignored = subprocess.run(["git", "check-ignore", "-q", "data/tickets/tickets.jsonl"], cwd=REPO).returncode == 0
    assert ignored, "data/tickets/ must be git-ignored"


def test_only_the_write_ticket_node_ever_calls_create_ticket():
    import ast

    class Callers(ast.NodeVisitor):
        def __init__(self):
            self.stack, self.found = [], []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            if getattr(node.func, "attr", "") == "create_ticket":
                self.found.append(self.stack[-1] if self.stack else "<module>")  # the innermost function that makes the call
            self.generic_visit(node)

    for path in sorted((REPO / "app").rglob("*.py")):
        callers = Callers()
        callers.visit(ast.parse(path.read_text(encoding="utf-8")))
        if path.name == "tools.py":
            continue  # RealTools.create_ticket hands the call to the store
        assert all(name == "write_ticket" for name in callers.found) and (not callers.found or path.name == "graph.py"), (path, callers.found)


# --- retrieval tools -------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def retriever():
    client = _new_collection()
    _index(client, _documents())
    return Retriever(client, FakeEmbedder(), collection="v2")


def test_product_search_returns_only_products_as_plain_dicts_within_the_limit(tmp_path, retriever):
    hits = RealTools(retriever, tickets=TicketStore(tmp_path / "t.jsonl")).search_products("áo", {})
    assert hits and len(hits) <= PRODUCT_K
    for hit in hits:
        assert set(hit) == {"key", "sku", "name", "kind", "color", "material", "sizes", "price_vnd", "in_stock"}
        assert hit["key"] == f"product:{hit['sku']}" and isinstance(hit["price_vnd"], int) and isinstance(hit["sizes"], list)
        json.dumps(hit)  # serialisable: it goes into the conversation state


def test_a_policy_question_never_returns_a_product_and_a_product_question_never_returns_a_policy(tmp_path, retriever):
    tools = RealTools(retriever, tickets=TicketStore(tmp_path / "t.jsonl"))
    policies = tools.search_policies("áo thun nam màu đen giá bao nhiêu")
    products = tools.search_products("đổi trả trong bao nhiêu ngày", {})
    assert policies and all(set(p) == {"key", "file", "clause", "title", "text"} and p["key"].startswith("policy:") for p in policies)
    assert len(policies) <= POLICY_K and products and all(p["key"].startswith("product:") for p in products)


def test_the_extracted_filters_are_applied_and_a_doc_type_in_them_cannot_override_the_tool(tmp_path, retriever):
    tools = RealTools(retriever, tickets=TicketStore(tmp_path / "t.jsonl"))
    everything = tools.search_products("áo", {})
    kind = everything[0]["kind"]
    only = tools.search_products("áo", {"kind": kind})
    assert only and {h["kind"] for h in only} == {kind}
    assert all(h["key"].startswith("product:") for h in tools.search_products("áo", {"doc_type": "policy"}))  # the tool decides the doc type


def test_each_retrieval_tool_opens_a_retrieval_span(tmp_path, retriever):
    tracer = Tracer(tmp_path / "traces.jsonl")
    tools = RealTools(retriever, tickets=TicketStore(tmp_path / "t.jsonl"))
    with tracer.request("q"):
        tools.search_products("áo", {})
        tools.search_policies("đổi trả")
    (line,) = [json.loads(x) for x in tracer.path.read_text(encoding="utf-8").splitlines()]
    assert [s["stage"] for s in line["spans"]] == ["retrieval", "retrieval"] and validate_trace_line(line) == []


# --- the tools inside the agent --------------------------------------------------------------------------------------------------------------


def test_the_agent_with_the_real_tools_verifies_an_order_and_writes_a_ticket_only_after_a_yes(tmp_path, retriever):
    complaint = f"mình muốn khiếu nại đơn 88 giao thiếu hàng, email {OWNER}"
    transport = FakeSaleor()
    tools = tools_with(transport, tmp_path, retriever)
    agent = Agent(router=FakeRouter({complaint: "create_ticket", f"đơn 88, email {OWNER}": "order_status", "đơn 88, email sai@example.com": "order_status"}),
                  extractor=FakeExtractor({}), tools=tools, answerer=FakeAnswerer(), tracer=Tracer(tmp_path / "traces.jsonl"))
    wrong = agent.turn("a", "đơn 88, email sai@example.com")
    assert wrong.reply == T.REFUSE_VERIFICATION and wrong.hits == []
    status = agent.turn("b", f"đơn 88, email {OWNER}")
    assert "88" in status.reply and "UNFULFILLED" in status.reply
    proposal = agent.turn("c", complaint)
    assert proposal.awaiting == "ticket_confirmation" and not (tmp_path / "t.jsonl").exists()  # nothing on disk before the yes
    done = agent.turn("c", "có")
    assert "TCK-" in done.reply and len((tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert all(r["operationName"] == "OrderByNumber" for r in transport.requests)  # reads only, and only the allowlisted query
    lines = [json.loads(x) for x in (tmp_path / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(validate_trace_line(l) == [] for l in lines) and "order_lookup" in {s["stage"] for l in lines for s in l["spans"]}
