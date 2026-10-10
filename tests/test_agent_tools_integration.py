"""The real tools against the real services (needs Saleor channel vn, Qdrant with the index, the cached bge-m3 model): the 30 seed orders through
the read-only client, the anti-IDOR rule for every one of them, and retrieval over the real index. Read only: nothing is written anywhere."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent import templates as T
from app.agent.agent import Agent
from app.agent.answer import TemplateAnswerer
from app.agent.protocols import OrderLookup
from app.agent.tools import RealTools, TicketStore
from app.config import api_url, require_setting
from app.retrieval.manifest import load_manifest
from app.retrieval.search import Retriever
from app.retrieval.store import connect
from app.saleor.readonly_client import ReadOnlySaleorClient
from tests.fake_agent import FakeExtractor, FakeRouter

pytestmark = pytest.mark.integration
ORDERS = json.loads((Path(__file__).resolve().parent.parent / "data" / "seed" / "orders.vi.json").read_text(encoding="utf-8"))["orders"]


@pytest.fixture(scope="module")
def saleor():
    with ReadOnlySaleorClient(api_url(), require_setting("SALEOR_RUNTIME_TOKEN")) as client:
        yield client


@pytest.fixture(scope="module")
def tools(saleor, embedder, tmp_path_factory):
    retriever = Retriever(connect(), embedder, collection=load_manifest()["collection"])
    return RealTools(retriever, saleor, TicketStore(tmp_path_factory.mktemp("tickets") / "t.jsonl"))


def test_every_seed_order_is_revealed_to_its_owner_and_to_nobody_else(tools):
    emails = sorted({o["customer"]["email"] for o in ORDERS})
    for order in ORDERS:
        number, owner = str(order["number"]), order["customer"]["email"]
        verified = tools.lookup_order(number, owner)
        assert verified.verified and verified.order["number"] == number and verified.order["status"] == order["status"], number
        assert set(verified.order) == {"number", "status", "created", "lines", "fulfillments"} and owner not in json.dumps(verified.order)
        assert sorted(line["sku"] for line in verified.order["lines"]) == sorted(line["sku"] for line in order["lines"])
        stranger = next(e for e in emails if e != owner)
        assert tools.lookup_order(number, stranger) == OrderLookup(False), number  # someone else's email
        assert tools.lookup_order(number, "sai@example.com") == OrderLookup(False), number
    assert tools.lookup_order("999999", ORDERS[0]["customer"]["email"]) == OrderLookup(False)  # an order that does not exist: the same answer


def test_an_order_of_another_channel_is_never_revealed(tools, saleor):
    """The test order #23 of the storefront (default-channel) exists in Saleor, but it is not the shop's: its owner's email must not open it."""
    data = saleor.execute("OrderByNumber", {"number": "23"}).get("orders") or {}
    if not data.get("edges"):
        pytest.skip("order #23 is not in this Saleor")
    node = data["edges"][0]["node"]
    assert (node.get("channel") or {}).get("slug") != "vn" and node.get("userEmail")
    assert tools.lookup_order("23", node["userEmail"]) == OrderLookup(False)


def test_the_retrieval_tools_return_the_shape_the_agent_expects_from_the_real_index(tools):
    products = tools.search_products("áo khoác nam màu đen", {})
    assert products and all(set(p) == {"key", "sku", "name", "kind", "color", "material", "sizes", "price_vnd", "in_stock"} for p in products)
    assert all(p["key"] == f"product:{p['sku']}" and isinstance(p["price_vnd"], int) and isinstance(p["in_stock"], bool) for p in products)
    policies = tools.search_policies("đổi trả trong bao nhiêu ngày")
    assert policies and all(p["key"].startswith("policy:") and isinstance(p["clause"], int) and p["text"] for p in policies)
    assert {p["file"] for p in policies} <= {"doi-tra", "van-chuyen", "bao-hanh"}


def test_the_agent_with_the_real_tools_answers_an_order_question_and_refuses_a_stranger(tools):
    owner = ORDERS[4]["customer"]["email"]  # shopmate-seed-order-005, number 88
    mine, theirs = f"đơn {ORDERS[4]['number']}, email {owner}", f"đơn {ORDERS[4]['number']}, email khachhang99@example.com"
    agent = Agent(router=FakeRouter({mine: "order_status", theirs: "order_status"}), extractor=FakeExtractor({}), tools=tools, answerer=TemplateAnswerer())
    answer = agent.turn("a", mine)
    assert f"Đơn #{ORDERS[4]['number']}" in answer.reply and "chưa giao" in answer.reply and owner not in answer.reply
    assert agent.turn("b", theirs).reply == T.REFUSE_VERIFICATION
