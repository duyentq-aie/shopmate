"""The Phase 6 evaluation runner (eval/run_phase6_eval.py) with fake components: scoring end to end, the simulated confirmations, the bounds, the clean
stop when a quota ends, the replay from the cache, and the guards of the command line. Hand-made items stand in for golden items (the real dev
items are read only by the runner itself, through load_split("dev")); nothing here reaches Gemini, Saleor or the real tickets file."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import templates as T
from app.agent.agent import Agent, TurnResult
from app.agent.answer import GeminiAnswerer
from app.agent.tools import TICKETS_PATH, TicketStore
from app.llm.cache import DiskCache
from app.llm.client import LiveResponse
from app.llm.errors import DailyLimitExhausted
from app.llm.generator import CachedGenerator
from app.llm.limits import DailyBudget
from app.llm.prompts import load_prompt
from app.tracing import Tracer
from eval.phase6.cases import Case, Expect, Turn, load_orders
from eval.run_phase6_eval import Harness, InstrumentedTools, RecordingGenerator, evaluate_phase6, main
from tests.fake_agent import FakeExtractor, FakeRouter, FakeTools
from tests.test_retrieval_folded import _documents  # noqa: F401  (keeps the fixture module imported like its siblings)

REPO = Path(__file__).resolve().parent.parent
OWNER = "khachhang10@example.com"


class StoreBackedTools(FakeTools):
    """The fake tools, with tickets written to a real TicketStore so that the runner can count them per conversation."""

    def __init__(self, store: TicketStore, **kwargs):
        super().__init__(**kwargs)
        self.store = store

    def create_ticket(self, *, conversation_id, summary, order_number, contact_email):
        return self.store.create(conversation_id=conversation_id, summary=summary, order_number=order_number, contact_email=contact_email)


class GroundedLLM:
    """The model behind the answer step: writes a grounded answer from the evidence it is given (or a wrong one, when told to)."""

    def __init__(self, wrong: bool = False, error: Exception | None = None):
        self.calls, self.wrong, self.error = [], wrong, error

    def generate(self, *, system_instruction, user_text, schema, generation_config):
        if self.error is not None:
            raise self.error
        asked = json.loads(user_text)
        self.calls.append(asked)
        evidence = asked["evidence"]
        if asked["kind"] == "products":
            hit = evidence["hits"][0]
            price = 99000 if self.wrong else hit["price_vnd"]
            answer = {"text": f"{hit['name']} (SKU {hit['sku']}) giá {price:,}đ.".replace(",", "."), "cited_skus": [hit["sku"]], "cited_clauses": []}
        elif asked["kind"] == "policies":
            hit = evidence["hits"][0]
            answer = {"text": "Mình đã tìm được quy định liên quan.", "cited_skus": [], "cited_clauses": [{"file": hit["file"], "clause": hit["clause"]}]}
        else:
            answer = {"text": f"Đơn #{evidence['order']['number']} hiện đang được xử lý, chưa giao.", "cited_skus": [], "cited_clauses": []}
        raw = {"candidates": [{"content": {"parts": [{"text": json.dumps(answer, ensure_ascii=False)}]}, "finishReason": "STOP"}],
               "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 50, "totalTokenCount": 1250}, "modelVersion": "gemini-3.5-flash-lite"}
        return LiveResponse(raw=raw, latency_ms=800.0, http_attempts=1)


def item(item_id, intent, question, source):
    return SimpleNamespace(id=item_id, intent=intent, question=question, gold_source=source)


SKUS = SimpleNamespace(type="skus", skus=["SM-NAM-0001"])
NO_ANSWER = SimpleNamespace(type="no_answer")
POLICY = SimpleNamespace(type="policy_clause", document="doi-tra", clause=2)


def order(behaviour):
    return SimpleNamespace(type="order", expected_behavior=behaviour, order_number="88")


ITEMS = [
    item("gs-901", "product_search", "áo khoác nam xanh rêu còn không shop", SKUS),
    item("gs-902", "product_search", "áo len nữ màu xanh dương size XL có không shop", NO_ANSWER),
    item("gs-903", "policy_question", "đổi size trong bao lâu vậy shop", POLICY),
    item("gs-904", "order_status", "đơn {order_number} của mình đang ở đâu, email " + OWNER, order("answer")),
    item("gs-905", "order_status", "đơn {order_number} của mình đang ở đâu rồi", order("ask_for_verification")),
    item("gs-906", "order_status", "đơn {order_number} của mình, email sai@example.com", order("refuse")),
    item("gs-907", "create_ticket", "khiếu nại đơn {order_number} giao thiếu hàng, email " + OWNER, order("answer")),
    item("gs-908", "create_ticket", "nhân viên tư vấn thái độ chưa tốt, mình muốn phản ánh", NO_ANSWER),
    item("gs-909", "smalltalk", "chào shop nhé", NO_ANSWER),
    item("gs-910", "out_of_scope", "thời tiết hôm nay thế nào", NO_ANSWER),
]
ROUTES = {"áo khoác nam xanh rêu còn không shop": "product_search", "áo len nữ màu xanh dương size XL có không shop": "product_search",
          "đổi size trong bao lâu vậy shop": "policy_question", f"đơn 88 của mình đang ở đâu, email {OWNER}": "order_status",
          "đơn 88 của mình đang ở đâu rồi": "order_status", "đơn 88 của mình, email sai@example.com": "order_status",
          f"khiếu nại đơn 88 giao thiếu hàng, email {OWNER}": "create_ticket", "nhân viên tư vấn thái độ chưa tốt, mình muốn phản ánh": "create_ticket",
          "chào shop nhé": "smalltalk", "thời tiết hôm nay thế nào": "out_of_scope"}
FILTERS = {"áo khoác nam xanh rêu còn không shop": {"kind": "Áo khoác nam", "color": "Xanh rêu"},
           "áo len nữ màu xanh dương size XL có không shop": {"kind": "Áo len nữ", "color": "Xanh dương", "size": "XL"}}


def make(tmp_path, *, llm="default", routes=None, budget=None, live=True, name="a"):
    store = TicketStore(tmp_path / f"tickets-{name}.jsonl")
    tools = InstrumentedTools(StoreBackedTools(store))
    client = (GroundedLLM() if llm == "default" else llm) if live else None
    generator = RecordingGenerator(CachedGenerator(DiskCache(tmp_path / "cache"), client))
    answerer = GeminiAnswerer(generator, load_prompt("answer_v1"))
    tracer = Tracer(tmp_path / f"traces-{name}.jsonl")
    agent = Agent(router=FakeRouter(routes or ROUTES), extractor=FakeExtractor(FILTERS), tools=tools, answerer=answerer, tracer=tracer)
    fixtures = {str(o["number"]): o for o in json.loads((REPO / "data" / "seed" / "orders.vi.json").read_text(encoding="utf-8"))["orders"]}
    return Harness(agent, tools, generator, answerer, store, tracer, load_orders(), budget, fixtures), client, store


# --- scoring end to end ------------------------------------------------------------------------------------------------------------------------


def test_a_working_agent_scores_every_intent_correct_and_every_bound_holds(tmp_path):
    h, client, _ = make(tmp_path)
    outcome = evaluate_phase6(ITEMS, [], h)
    d = outcome["deterministic"]
    assert outcome["complete"] is True and outcome["stopped"] is None
    assert [(r["id"], r["correct"]) for r in d["items"]] == [(i.id, True) for i in sorted(ITEMS, key=lambda i: i.id)], [r for r in d["items"] if not r["correct"]]
    assert d["per_intent"]["all"] == {"n": 10, "correct": 10, "accuracy": 1.0}
    assert d["per_intent"]["product_search"]["by_kind"]["no_answer"]["correct"] == 1  # gs-902: nothing matches, one relaxation, announced
    assert all(b["held"] for b in d["bounds"].values()) and d["bounds"]["steps_per_turn"]["max_seen"] <= 7
    assert d["guardrails"]["fallbacks"] == 0 and d["guardrails"]["answers"] == len(client.calls) == 3  # a product, a policy and an order answer


def test_the_proposed_ticket_is_confirmed_and_declined_in_conversations_of_its_own_and_only_the_yes_writes(tmp_path):
    h, _, store = make(tmp_path)
    result = evaluate_phase6([ITEMS[6]], [], h)["deterministic"]["items"][0]
    assert result["correct"] is True and result["awaiting"] == "ticket_confirmation"
    assert h.tickets_of("dev-gs-907") == 0 and h.tickets_of("dev-gs-907-yes") == 1 and h.tickets_of("dev-gs-907-no") == 0
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 1


def test_the_simulated_tickets_never_touch_the_real_tickets_file(tmp_path):
    before = TICKETS_PATH.read_bytes() if TICKETS_PATH.exists() else None
    h, _, _ = make(tmp_path)
    evaluate_phase6([ITEMS[6]], [], h)
    assert (TICKETS_PATH.read_bytes() if TICKETS_PATH.exists() else None) == before


def test_an_agent_that_answers_wrongly_is_scored_wrongly_and_the_reason_is_recorded(tmp_path):
    routes = {**ROUTES, "áo khoác nam xanh rêu còn không shop": "smalltalk", "đổi size trong bao lâu vậy shop": "out_of_scope"}
    h, _, _ = make(tmp_path, routes=routes)
    items = {r["id"]: r for r in evaluate_phase6(ITEMS, [], h)["deterministic"]["items"]}
    assert items["gs-901"]["correct"] is False and "no gold SKU" in items["gs-901"]["reason"] and items["gs-901"]["routed_to"] == "smalltalk"
    assert items["gs-903"]["correct"] is False and "does not cite" in items["gs-903"]["reason"]
    assert items["gs-909"]["correct"] is True  # the others are unaffected


def test_a_fabricated_answer_is_replaced_and_counted_with_its_rule(tmp_path):
    h, _, _ = make(tmp_path, llm=GroundedLLM(wrong=True))
    d = evaluate_phase6([ITEMS[0]], [], h)["deterministic"]
    assert d["guardrails"] == {"answers": 1, "fallbacks": 1, "rules": {"price_mismatch": 1}, "items_with_fallback": ["gs-901"]}
    assert d["items"][0]["correct"] is True and "99" not in d["items"][0]["reply"] and d["items"][0]["fallback"] is True  # the deterministic answer still names the SKU
    assert d["items"][0]["fallback_rules"] == ["price_mismatch"] and d["items"][0]["retrieved_skus"] == ["SM-NAM-0001"]  # what the error analysis needs


def test_a_ticket_written_by_a_turn_that_was_not_an_explicit_yes_is_a_violated_bound(tmp_path):
    h, _, _ = make(tmp_path)

    class Rogue:
        def turn(self, conversation_id, message):
            with h.tracer.request(message):  # one trace line per turn, like the real agent
                h.tools.create_ticket(conversation_id=conversation_id, summary="s", order_number=None, contact_email=OWNER)  # written on the very first turn
            return TurnResult(reply="ok", intent="create_ticket", awaiting=None)

        def snapshot(self, conversation_id):
            return {}

    h.agent = Rogue()
    h.turn("c1", "tạo phiếu giúp mình", role="dev")
    assert h.seen["tickets_without_confirmation"] == 1


# --- the multi-turn cases ----------------------------------------------------------------------------------------------------------------------


def test_the_cases_are_run_turn_by_turn_and_a_missed_expectation_is_reported_with_its_problem(tmp_path):
    routes = {**ROUTES, "đơn 88 là gì": "order_status"}
    good = Case(id="mt-90", title="verify then answer", checks=["verify_answer"], turns=[
        Turn(say="đơn 88 là gì", expect=Expect(awaiting="email", reply_is="ASK_EMAIL", lookups_total=0)),
        Turn(say=OWNER, expect=Expect(awaiting=None, lookups_total=1, reply_contains=["88"]))])
    bad = Case(id="mt-91", title="an expectation that is wrong", checks=["x"], turns=[Turn(say="đơn 88 là gì", expect=Expect(awaiting=None, tickets_total=1))])
    h, _, _ = make(tmp_path, routes=routes)
    d = evaluate_phase6([], [good, bad], h)["deterministic"]
    assert d["multiturn"]["passed"] == 1 and d["multiturn"]["total"] == 2
    ok, ko = d["multiturn"]["cases"]
    assert ok["passed"] is True and [t["passed"] for t in ok["turns"]] == [True, True]
    assert ko["passed"] is False and any("awaiting" in p for p in ko["turns"][0]["problems"]) and any("tickets_total" in p for p in ko["turns"][0]["problems"])


def test_a_turn_is_checked_for_the_step_cap_too(tmp_path):
    routes = {**ROUTES, "đơn 88 là gì": "order_status"}
    case = Case(id="mt-92", title="x", checks=["x"], turns=[Turn(say="đơn 88 là gì", expect=Expect(awaiting="email"))])
    h, _, _ = make(tmp_path, routes=routes)
    assert evaluate_phase6([], [case], h)["deterministic"]["multiturn"]["cases"][0]["turns"][0]["problems"] == []
    assert h.seen["steps_per_turn"] == 4  # intake, route, check_slots, reply


# --- stopping at a clean point -------------------------------------------------------------------------------------------------------------------


def test_a_quota_error_stops_the_run_before_the_item_and_keeps_what_is_done(tmp_path):
    cache_dir = tmp_path / "cache"
    h, _, _ = make(tmp_path)
    evaluate_phase6(ITEMS[:2], [], h)  # gs-901 and gs-902 are in the cache now
    h2, _, _ = make(tmp_path, llm=GroundedLLM(error=DailyLimitExhausted("the daily quota is used up")), name="b")
    outcome = evaluate_phase6(ITEMS[:4], [], h2)  # gs-903 needs an answer that is not cached
    assert outcome["complete"] is False
    assert outcome["stopped"]["before"] == "gs-903" and "DailyLimitExhausted" in outcome["stopped"]["reason"]
    assert [r["id"] for r in outcome["deterministic"]["items"]] == ["gs-901", "gs-902"]


def test_a_nearly_empty_daily_budget_stops_before_starting_an_item_that_may_not_finish(tmp_path):
    budget = DailyBudget(tmp_path / "usage", limit=450)
    for _ in range(448):
        budget.reserve()
    h, _, _ = make(tmp_path, budget=budget)
    outcome = evaluate_phase6(ITEMS, [], h)
    assert outcome["complete"] is False and outcome["deterministic"]["items"] == []
    assert outcome["stopped"]["before"] == "gs-901" and "2 requests left" in outcome["stopped"]["reason"]


def test_without_live_calls_a_cache_miss_is_a_clean_stop_too(tmp_path):
    h, _, _ = make(tmp_path, live=False)
    outcome = evaluate_phase6(ITEMS, [], h)
    assert outcome["complete"] is False and outcome["stopped"]["before"] == "gs-901" and "CacheMiss" in outcome["stopped"]["reason"]


def test_a_budget_with_room_does_not_stop_a_run(tmp_path):
    budget = DailyBudget(tmp_path / "usage", limit=450)
    h, _, _ = make(tmp_path, budget=budget)
    assert evaluate_phase6(ITEMS[:3], [], h)["complete"] is True


# --- replay ----------------------------------------------------------------------------------------------------------------------------------------


def test_a_replay_from_the_cache_makes_no_live_call_and_reproduces_the_result_exactly(tmp_path):
    first, client, _ = make(tmp_path)
    a = evaluate_phase6(ITEMS, [], first)
    assert first.generator.inner.live_calls == len(client.calls) > 0
    replay, _, _ = make(tmp_path, live=False, name="b")  # no client at all: a call would be a CacheMiss
    b = evaluate_phase6(ITEMS, [], replay)
    assert replay.generator.inner.live_calls == 0 and b["complete"] is True
    assert json.dumps(a["deterministic"], sort_keys=True, ensure_ascii=False) == json.dumps(b["deterministic"], sort_keys=True, ensure_ascii=False)


def test_the_deterministic_part_carries_no_timestamp_or_path_and_the_latency_is_a_measurement(tmp_path):
    h, _, _ = make(tmp_path)
    outcome = evaluate_phase6(ITEMS[:3], [], h, prices=(0.30, 2.50), price_source="test")
    text = json.dumps(outcome["deterministic"], ensure_ascii=False)
    assert str(tmp_path) not in text and "T2026" not in text and "latency" not in text
    assert set(outcome["measurements"]["latency_ms_per_turn"]) >= {"p50", "p95", "n"}
    cost = outcome["deterministic"]["cost_theoretical_paid_tier"]
    assert cost["free_tier_cost_usd"] == 0 and cost["per_1000_conversations_usd"] > 0


def test_llm_calls_per_turn_never_exceed_three(tmp_path):
    h, _, _ = make(tmp_path)
    d = evaluate_phase6(ITEMS, [], h)["deterministic"]
    assert d["calls"]["llm_calls_per_turn"]["max"] <= 3 and d["calls"]["by_component_dev_first_turns"]["answer"]["n"] == 3


# --- the command line and the source ------------------------------------------------------------------------------------------------------------


def test_the_cli_refuses_a_results_dir_outside_eval_results_before_anything_else(capsys):
    assert main(["--results-dir", "/tmp/elsewhere"]) == 1 and "eval/results" in capsys.readouterr().err


def test_the_cli_refuses_to_overwrite_a_result_and_a_dirty_tree(monkeypatch, capsys):
    from eval.run_retrieval_eval import DirtyWorkingTree

    out = REPO / "eval" / "results" / "phase6" / "answer_v_exists.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("{}", encoding="utf-8")
    try:
        assert main(["--answer-prompt", "answer_v_exists"]) == 1 and "exists" in capsys.readouterr().err
    finally:
        out.unlink()

    def dirty(**kwargs):
        raise DirtyWorkingTree("working tree is not clean")

    monkeypatch.setattr("eval.run_phase6_eval.check_clean_tree", dirty)
    assert main(["--answer-prompt", "answer_v1", "--results-dir", "eval/results/phase6-scratch"]) == 1 and "not clean" in capsys.readouterr().err


def test_the_runner_reads_golden_items_only_through_the_dev_loader_and_never_names_the_final_evaluation_flag():
    source = (REPO / "eval" / "run_phase6_eval.py").read_text(encoding="utf-8")
    assert source.count("load_split(") == 2 and 'load_split("dev")' in source  # the import and the one call
    for word in ("load_golden", "final_evaluation", "test_split", "GOLDEN_PATH"):
        assert word not in source


def test_the_scoring_module_and_the_cases_are_not_modified_by_the_runner():
    source = (REPO / "eval" / "run_phase6_eval.py").read_text(encoding="utf-8")
    assert "write_text" not in source.replace("write_result", "")  # the runner writes its results only through write_result


def test_a_temporary_directory_holds_the_traces_and_tickets_of_a_real_run():
    source = (REPO / "eval" / "run_phase6_eval.py").read_text(encoding="utf-8")
    assert "tempfile.TemporaryDirectory" in source and 'tmp_dir / "tickets.jsonl"' in source and "TICKETS_PATH" not in source
