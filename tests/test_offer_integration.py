"""The offer after an empty search, against the REAL index (Qdrant with the Phase 4 collection, and for the acceptance the cached bge-m3 model): the two product
questions of the Phase 7 live smoke that found nothing. The counts are checked against the seed catalog read from disk (the index is a snapshot of it), so the
test does not trust the code it tests. No LLM, no Saleor: the router, the extractor and the answerer are fakes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent import templates as T
from app.agent.agent import Agent
from app.agent.tools import RealTools, TicketStore
from app.retrieval.manifest import load_manifest
from app.retrieval.search import Retriever
from app.retrieval.store import connect
from tests.fake_agent import FakeAnswerer, FakeExtractor, FakeRouter

pytestmark = pytest.mark.integration
CATALOG = json.loads((Path(__file__).resolve().parent.parent / "data" / "seed" / "catalog.vi.json").read_text(encoding="utf-8"))["products"]

# the two questions of the live smoke, with the filters the extractor really returned for them (logs of that run, docs/DECISIONS-phase7.md D7.19)
JACKET = "Mình cần áo khoác dù nam màu đen dưới 500k, còn size L không shop?"
JACKET_FILTERS = {"kind": "Áo khoác nam", "color": "Đen", "material": "Dù", "size": "L", "price_max": 500000}
SHIRT = "Shop có áo sơ mi nam màu trắng chất liệu linen không?"
SHIRT_FILTERS = {"kind": "Áo sơ mi nam", "color": "Trắng", "material": "Linen"}


def in_catalogue(filters: dict) -> list[dict]:
    """The same constraints applied to the catalogue file, by hand."""
    return [p for p in CATALOG if p["kind"] == filters["kind"] and filters.get("color") in (None, p["color"]) and filters.get("material") in (None, p["material"])
            and filters.get("size") in (None, *p["sizes"]) and p["price"] <= filters.get("price_max", 10**12)]


@pytest.fixture(scope="module")
def counting_tools(tmp_path_factory):
    """Counting needs no embedding: the retriever is built without a model."""
    return RealTools(Retriever(connect(), None, collection=load_manifest()["collection"]), None, TicketStore(tmp_path_factory.mktemp("t") / "t.jsonl"))


def test_the_live_index_and_the_catalogue_agree_on_every_step_of_both_questions(counting_tools):
    for filters in (JACKET_FILTERS, SHIRT_FILTERS):
        current = dict(filters)
        for name in ("size", "color", "material", "price_max"):
            if name not in current:
                continue
            del current[name]
            assert counting_tools.retriever.count({**current, "doc_type": "product"}) == len(in_catalogue(current)), (filters, current)


def test_the_jacket_question_is_blocked_by_the_material_after_size_and_colour_and_two_jackets_under_500k_exist(counting_tools):
    offer = counting_tools.offer_for(JACKET_FILTERS)
    expected = in_catalogue({"kind": "Áo khoác nam", "price_max": 500000})
    assert (offer.blocking, offer.dropped, offer.count) == ("material", ("size", "color", "material"), len(expected)) and offer.count == 2
    assert offer.filters == {"kind": "Áo khoác nam", "price_max": 500000} and sorted(p["sku"] for p in expected) == ["SM-NAM-0014", "SM-NAM-0042"]
    # dropping any of the first steps alone is not enough: that is why the offer goes on to the material
    assert in_catalogue({**JACKET_FILTERS, "size": None}) == [] and in_catalogue({k: v for k, v in JACKET_FILTERS.items() if k not in ("size", "color")}) == []


def test_the_shirt_question_is_blocked_by_the_material_after_the_colour_and_seven_men_s_shirts_exist(counting_tools):
    offer = counting_tools.offer_for(SHIRT_FILTERS)
    assert (offer.blocking, offer.dropped, offer.count) == ("material", ("color", "material"), len(in_catalogue({"kind": "Áo sơ mi nam"}))) and offer.count == 7
    assert offer.filters == {"kind": "Áo sơ mi nam"}
    assert in_catalogue({"kind": "Áo sơ mi nam", "material": "Linen"}) == [] and in_catalogue({"kind": "Áo sơ mi nam", "color": "Trắng"}) == []  # neither alone was enough


@pytest.fixture
def agent_and_tools(embedder, tmp_path):
    tools = RealTools(Retriever(connect(), embedder, collection=load_manifest()["collection"]), None, TicketStore(tmp_path / "t.jsonl"))
    agent = Agent(router=FakeRouter({JACKET: "product_search", SHIRT: "product_search"}), extractor=FakeExtractor({JACKET: JACKET_FILTERS, SHIRT: SHIRT_FILTERS}), tools=tools,
                  answerer=FakeAnswerer())  # the filters are the ones the real extractor returned
    return agent, tools


def test_the_real_agent_offers_then_searches_again_only_after_a_yes_for_the_jacket(agent_and_tools):
    agent, tools = agent_and_tools
    offered = agent.turn("jacket", JACKET)
    assert offered.reply == ("Mình không tìm thấy sản phẩm nào khớp tất cả yêu cầu của bạn. Điều kiện đang chặn kết quả: chất liệu dù (cùng với kích cỡ L và màu đen). "
                             "Hiện có 2 áo khoác nam mức giá tối đa 500.000đ nếu bỏ các điều kiện này — bạn muốn xem không?")
    assert offered.hits == [] and offered.awaiting == "offer_confirmation" and offered.steps == 7 and "SM-NAM" not in offered.reply
    accepted = agent.turn("jacket", "có")
    assert sorted(h["sku"] for h in accepted.hits) == ["SM-NAM-0014", "SM-NAM-0042"] and all(h["price_vnd"] <= 500000 for h in accepted.hits)
    assert accepted.reply.startswith("Không khớp hoàn toàn: khác kích cỡ, màu sắc và chất liệu.\n") and accepted.steps == 3


def test_the_real_agent_offers_the_shirts_and_a_no_searches_nothing(agent_and_tools):
    agent, _ = agent_and_tools
    offered = agent.turn("shirt", SHIRT)
    assert "Điều kiện đang chặn kết quả: chất liệu linen (cùng với màu trắng). Hiện có 7 áo sơ mi nam nếu bỏ các điều kiện này — bạn muốn xem không?" in offered.reply
    assert T.NO_FULL_MATCH in offered.reply and offered.hits == [] and offered.reply_source == "template"
