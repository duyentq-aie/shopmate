"""The chat API and the widget files (app/chat_api.py, widget/): input rules, rate limit, CORS, the confirmation flow, no stack traces.

Offline: the API runs over FakeBackend (no agent, no LLM, no network, no ticket file), or over the real Agent with fake components. No
fake secret is taken from the environment: the one used here is built in the test."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.agent import Agent
from app.chat_api import DEFAULT_PORT, MAX_BODY_BYTES, AgentBackend, FakeBackend, SessionRateLimiter, create_app, main, parse_args, product_cards
from app.agent import templates as T
from tests.fake_agent import FakeAnswerer, FakeExtractor, FakeRouter, FakeTools

REPO = Path(__file__).resolve().parent.parent
STOREFRONT = "http://localhost:3000"
OTHER = "http://evil.example"
FAKE_KEY = "fake-" + "x" * 28


def sid() -> str:
    return str(uuid.uuid4())


def client(backend=None, **kwargs) -> TestClient:
    return TestClient(create_app(backend or FakeBackend(), storefront_origin=STOREFRONT, **kwargs))


def say(c: TestClient, session: str, message: str, **kwargs):
    return c.post("/api/chat", json={"session_id": session, "message": message}, **kwargs)


# --- the contract ---------------------------------------------------------------------------------------------------------------------


def test_a_chat_answer_has_exactly_the_agreed_fields():
    body = say(client(), sid(), "áo khoác nam").json()
    assert set(body) == {"reply", "reply_source", "trace_id", "awaiting_confirmation", "products"}
    assert body["awaiting_confirmation"] is False and body["reply_source"] == "llm" and body["trace_id"]
    assert 1 <= len(body["products"]) <= 3
    for product in body["products"]:
        assert set(product) == {"name", "price_vnd", "in_stock", "slug", "url"} and isinstance(product["price_vnd"], int)
        assert product["url"] == f"http://localhost:3000/en/vn/products/{product['slug']}"


def test_a_message_without_products_has_an_empty_list():
    body = say(client(), sid(), "chào shop").json()
    assert body["products"] == [] and body["reply_source"] == "template"


def test_health_says_the_mode_and_nothing_else():
    assert client().get("/api/health").json() == {"ok": True, "mode": "fake"}


# --- input validation -------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    {}, {"message": "xin chào"}, {"session_id": sid()}, {"session_id": "not-a-uuid", "message": "xin chào"}, {"session_id": 12, "message": "xin chào"},
    {"session_id": sid(), "message": ""}, {"session_id": sid(), "message": "   \n  "}, {"session_id": sid(), "message": "a" * 1001},
    {"session_id": sid(), "message": 5}, {"session_id": sid(), "message": "xin chào", "extra": 1}, {"session_id": sid(), "message": ["x"]}, [], "text", None])
def test_invalid_input_is_a_422_with_a_short_vietnamese_message_that_echoes_nothing(body):
    response = client().post("/api/chat", json=body)
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request", "message": "Tin nhắn chưa hợp lệ: cần từ 1 đến 1000 ký tự."}


def test_the_limits_of_the_message_are_inclusive():
    c = client()
    assert say(c, sid(), "a").status_code == 200 and say(c, sid(), "a" * 1000).status_code == 200
    assert say(c, sid(), "a" * 1001).status_code == 422


def test_a_session_id_must_be_a_uuid_in_any_of_its_spellings_and_is_normalised():
    c, raw = client(), uuid.uuid4()
    for spelling in (str(raw), raw.hex, str(raw).upper()):
        assert say(c, spelling, "chào").status_code == 200
    for bad in ("1234", "../../etc/passwd", "<script>", str(raw) + "x", ""):
        assert say(c, bad, "chào").status_code == 422


def test_a_non_json_body_and_a_wrong_content_type_are_refused():
    c = client()
    assert c.post("/api/chat", content=b"{not json", headers={"content-type": "application/json"}).status_code == 422
    assert c.post("/api/chat", content="session_id=x", headers={"content-type": "text/plain"}).status_code == 422
    confirm = c.post("/api/chat/confirm", json={"session_id": sid(), "confirm": "maybe"})
    assert confirm.status_code == 422


def test_a_huge_body_is_refused_before_it_is_read():
    response = client().post("/api/chat", content=b"x" * (MAX_BODY_BYTES + 1), headers={"content-type": "application/json"})
    assert response.status_code == 413 and response.json()["error"] == "too_big"


def test_only_the_documented_routes_exist():
    c = client()
    for path in ("/docs", "/redoc", "/openapi.json", "/", "/api", "/api/chat/../health/x"):
        assert c.get(path).status_code in (404, 405), path
    assert c.get("/api/chat").status_code == 405 and c.delete("/api/chat").status_code == 405


# --- rate limit ---------------------------------------------------------------------------------------------------------------------------


def test_the_seventh_message_of_a_session_in_a_minute_is_a_429_in_vietnamese_with_retry_after():
    c, mine, other = client(), sid(), sid()
    assert [say(c, mine, "chào").status_code for _ in range(6)] == [200] * 6
    blocked = say(c, mine, "chào")
    assert blocked.status_code == 429 and blocked.json()["error"] == "rate_limited" and "nhanh" in blocked.json()["message"]
    assert 1 <= int(blocked.headers["retry-after"]) <= 60 and blocked.json()["retry_after"] == int(blocked.headers["retry-after"])
    assert say(c, other, "chào").status_code == 200  # another session is not affected


def test_the_confirm_endpoint_counts_against_the_same_limit():
    c, mine = client(), sid()
    for _ in range(6):
        c.post("/api/chat/confirm", json={"session_id": mine, "confirm": True})
    assert c.post("/api/chat/confirm", json={"session_id": mine, "confirm": True}).status_code == 429


def test_the_limiter_frees_the_session_after_the_window_and_forgets_idle_sessions():
    now = [0.0]
    limiter = SessionRateLimiter(limit=2, window=60, clock=lambda: now[0], max_sessions=3)
    assert limiter.check("a") is None and limiter.check("a") is None and limiter.check("a") == 60
    now[0] = 59.5
    assert limiter.check("a") == pytest.approx(1.0, abs=1.01)  # the first message leaves the window in half a second
    now[0] = 61
    assert limiter.check("a") is None
    now[0] = 500  # every earlier session is idle now: the new ones fit, the table does not grow past its cap
    for key in ("e", "f", "g"):
        assert limiter.check(key) is None  # 'a' was idle and is dropped to make room
    assert "a" not in limiter._seen and len(limiter._seen) == 3


def test_a_full_table_of_live_sessions_refuses_a_new_one_instead_of_growing():
    limiter = SessionRateLimiter(limit=5, window=60, clock=lambda: 0.0, max_sessions=2)
    assert limiter.check("a") is None and limiter.check("b") is None
    assert limiter.check("c") == 60 and limiter.check("a") is None


# --- CORS ---------------------------------------------------------------------------------------------------------------------------------


def test_the_storefront_origin_gets_cors_headers_and_a_preflight_is_answered():
    c = client()
    ok = say(c, sid(), "chào", headers={"Origin": STOREFRONT})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == STOREFRONT and "access-control-allow-credentials" not in ok.headers
    pre = c.options("/api/chat", headers={"Origin": STOREFRONT, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == STOREFRONT and "POST" in pre.headers["access-control-allow-methods"]


def test_another_origin_is_refused_whatever_it_asks():
    c = client()
    for origin in (OTHER, "http://localhost:3001", "https://localhost:3000", "null", "http://localhost:3000.evil.example"):
        response = say(c, sid(), "chào", headers={"Origin": origin})
        assert response.status_code == 403 and "access-control-allow-origin" not in response.headers, origin
        assert response.json()["error"] == "forbidden_origin"
    pre = c.options("/api/chat", headers={"Origin": OTHER, "Access-Control-Request-Method": "POST"})
    assert pre.status_code in (400, 403) and "access-control-allow-origin" not in pre.headers
    assert c.get("/widget/shopmate-chat.js", headers={"Origin": OTHER}).status_code == 403


def test_the_api_own_origin_may_call_it_and_a_call_without_an_origin_is_fine():
    c = client(port=DEFAULT_PORT)
    for origin in (f"http://127.0.0.1:{DEFAULT_PORT}", f"http://localhost:{DEFAULT_PORT}"):
        assert say(c, sid(), "chào", headers={"Origin": origin}).status_code == 200
    assert say(c, sid(), "chào").status_code == 200


def test_the_storefront_origin_can_be_set_and_nothing_else_changes():
    c = TestClient(create_app(FakeBackend(), storefront_origin="http://localhost:3100/"))
    assert say(c, sid(), "chào", headers={"Origin": "http://localhost:3100"}).status_code == 200
    assert say(c, sid(), "chào", headers={"Origin": STOREFRONT}).status_code == 403


# --- the confirmation flow ----------------------------------------------------------------------------------------------------------------


def test_a_complaint_asks_for_a_confirmation_and_the_yes_button_creates_nothing_before_it_is_pressed():
    c, mine = client(), sid()
    asked = say(c, mine, "giao nhầm màu rồi shop").json()
    assert asked["awaiting_confirmation"] is True and asked["reply_source"] == "clarify" and "xác nhận" in asked["reply"]
    done = c.post("/api/chat/confirm", json={"session_id": mine, "confirm": True}).json()
    assert done["awaiting_confirmation"] is False and "TCK-" in done["reply"]
    assert c.post("/api/chat/confirm", json={"session_id": mine, "confirm": True}).status_code == 409  # nothing is pending any more


def test_the_no_button_cancels_and_a_confirm_without_a_question_is_a_409():
    c, mine = client(), sid()
    assert c.post("/api/chat/confirm", json={"session_id": mine, "confirm": True}).json() == {
        "error": "nothing_to_confirm", "message": "Hiện chưa có yêu cầu nào đang chờ bạn xác nhận."}
    say(c, mine, "sản phẩm bị lỗi, tôi muốn khiếu nại")
    cancelled = c.post("/api/chat/confirm", json={"session_id": mine, "confirm": False}).json()
    assert cancelled["reply"] == T.TICKET_CANCELLED and cancelled["awaiting_confirmation"] is False


def test_a_typed_answer_to_the_open_question_works_like_the_button_and_another_session_cannot_confirm_it():
    c, mine, other = client(), sid(), sid()
    say(c, mine, "giao nhầm màu rồi shop")
    assert c.post("/api/chat/confirm", json={"session_id": other, "confirm": True}).status_code == 409
    assert "TCK-" in say(c, mine, "có").json()["reply"]


def test_the_fake_backend_never_writes_a_ticket_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c, mine = client(), sid()
    say(c, mine, "giao nhầm màu rồi shop")
    c.post("/api/chat/confirm", json={"session_id": mine, "confirm": True})
    assert list(tmp_path.rglob("*")) == []


def test_with_the_real_agent_the_buttons_resume_the_interrupt_and_write_exactly_one_ticket(tmp_path):
    complaint = "giao nhầm màu rồi shop"
    tools = FakeTools()
    agent = Agent(router=FakeRouter({complaint: "create_ticket"}), extractor=FakeExtractor({}), tools=tools, answerer=FakeAnswerer(), catch_errors=True)
    c, mine = client(AgentBackend(agent, slugs={})), sid()
    assert say(c, mine, complaint).json()["awaiting_confirmation"] is False  # the agent asks for an email first
    asked = say(c, mine, "khachhang02@example.com").json()
    assert asked["awaiting_confirmation"] is True and tools.tickets == []  # nothing written while the question is open
    answered = c.post("/api/chat/confirm", json={"session_id": mine, "confirm": True}).json()
    assert "TCK-" in answered["reply"] and len(tools.tickets) == 1
    assert c.post("/api/chat/confirm", json={"session_id": mine, "confirm": True}).status_code == 409 and len(tools.tickets) == 1


def test_with_the_real_agent_the_no_button_writes_nothing(tmp_path):
    complaint = "giao nhầm màu rồi shop"
    tools = FakeTools()
    agent = Agent(router=FakeRouter({complaint: "create_ticket"}), extractor=FakeExtractor({}), tools=tools, answerer=FakeAnswerer(), catch_errors=True)
    c, mine = client(AgentBackend(agent, slugs={})), sid()
    say(c, mine, complaint); say(c, mine, "khachhang02@example.com")
    body = c.post("/api/chat/confirm", json={"session_id": mine, "confirm": False}).json()
    assert body["reply"] == T.TICKET_CANCELLED and tools.tickets == []


# --- products ------------------------------------------------------------------------------------------------------------------------------


def test_product_cards_come_from_the_hits_the_reply_names_and_carry_the_storefront_link():
    hits = [{"key": "product:SM-NAM-0002", "sku": "SM-NAM-0002", "name": "Áo khoác dù nam đen", "price_vnd": 429000, "in_stock": True},
            {"key": "product:SM-NAM-0001", "sku": "SM-NAM-0001", "name": "Áo khoác dù nam xanh rêu", "price_vnd": 459000, "in_stock": False},
            {"key": "policy:doi-tra:2", "sku": "SM-X", "name": "not a product", "price_vnd": 1, "in_stock": True}]
    cards = product_cards(hits, "Mình gợi ý SM-NAM-0002 (429.000đ).", {"SM-NAM-0002": "ao-khoac-du-nam-den"}, "https://shop.test/p/{slug}")
    assert cards == [{"name": "Áo khoác dù nam đen", "price_vnd": 429000, "in_stock": True, "slug": "ao-khoac-du-nam-den", "url": "https://shop.test/p/ao-khoac-du-nam-den"}]
    unknown = product_cards(hits[:1], "SM-NAM-0002", {}, "https://shop.test/p/{slug}")
    assert unknown[0]["slug"] is None and unknown[0]["url"] is None  # no slug known: a card without a link, never a made-up one


def test_the_real_agent_answer_shows_cards_only_for_the_skus_it_names():
    routes, filters = {"áo khoác nam": "product_search"}, {"áo khoác nam": {"kind": "Áo khoác nam"}}
    agent = Agent(router=FakeRouter(routes), extractor=FakeExtractor(filters), tools=FakeTools(), answerer=FakeAnswerer(), catch_errors=True)
    body = say(client(AgentBackend(agent, slugs={"SM-NAM-0002": "ao-khoac-du-nam-den"})), sid(), "áo khoác nam").json()
    names = {p["name"] for p in body["products"]}
    assert names == {"Áo khoác dù nam xanh rêu", "Áo khoác dù nam đen"} and all(p["price_vnd"] > 0 for p in body["products"])
    assert {p["name"]: p["url"] for p in body["products"]}["Áo khoác dù nam đen"].endswith("/products/ao-khoac-du-nam-den")


# --- nothing internal reaches the customer ---------------------------------------------------------------------------------------------------


class Exploding:
    mode = "fake"

    def reply(self, session_id, message):
        raise RuntimeError("secret internals: " + FAKE_KEY + " /home/someone/app/chat_api.py line 42")

    def confirm(self, session_id, confirm):
        raise ValueError("also internal")


def test_a_bug_in_the_backend_is_a_500_with_the_fixed_text_and_no_stack_trace_or_exception_text():
    c = TestClient(create_app(Exploding(), storefront_origin=STOREFRONT), raise_server_exceptions=False)
    for response in (say(c, sid(), "chào"), c.post("/api/chat/confirm", json={"session_id": sid(), "confirm": True})):
        assert response.status_code == 500
        assert response.json() == {"error": "internal", "message": T.TECHNICAL_PROBLEM}
        for leaked in ("Traceback", "RuntimeError", FAKE_KEY, "/home/", "chat_api", "internals", "ValueError"):
            assert leaked not in response.text


def test_a_failure_inside_the_agent_is_the_technical_problem_text_with_the_trace_id_and_no_exception_text(tmp_path):
    from app.llm.errors import DailyLimitExhausted
    from app.tracing import Tracer

    class Dying(FakeRouter):
        def route(self, question):
            raise DailyLimitExhausted("429 RESOURCE_EXHAUSTED " + FAKE_KEY)

    tracer = Tracer(tmp_path / "logs" / "traces.jsonl", secrets=[FAKE_KEY])
    agent = Agent(router=Dying({}), extractor=FakeExtractor({}), tools=FakeTools(), answerer=FakeAnswerer(), tracer=tracer, catch_errors=True)
    response = say(client(AgentBackend(agent, slugs={})), sid(), "chào")
    body = response.json()
    assert response.status_code == 200 and body["reply"] == T.TECHNICAL_PROBLEM and body["reply_source"] == "error_template"
    line = json.loads(tracer.path.read_text(encoding="utf-8"))
    assert body["trace_id"] == line["trace_id"] and line["error"]["type"] == "llm_429_daily"  # the trace has the cause, the customer only its id
    for leaked in ("DailyLimit", "RESOURCE_EXHAUSTED", FAKE_KEY, "Traceback"):
        assert leaked not in response.text


def test_the_validation_error_does_not_echo_the_input():
    secret_like = "hello " + FAKE_KEY
    response = client().post("/api/chat", json={"session_id": "nope", "message": secret_like})
    assert response.status_code == 422 and FAKE_KEY not in response.text and "nope" not in response.text


def test_api_responses_are_not_cached_and_every_response_is_nosniff():
    c = client()
    response = say(c, sid(), "chào")
    assert response.headers["cache-control"] == "no-store" and response.headers["x-content-type-options"] == "nosniff"
    assert c.get("/widget/shopmate-chat.js").headers["x-content-type-options"] == "nosniff"


# --- the widget files -----------------------------------------------------------------------------------------------------------------------


def test_the_widget_script_is_served_as_javascript_and_is_the_file_in_the_repo():
    response = client().get("/widget/shopmate-chat.js")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/javascript")
    assert response.content == (REPO / "widget" / "shopmate-chat.js").read_bytes()
    assert "customElements.define('shopmate-chat'" in response.text


def test_the_demo_page_is_html_with_a_strict_csp_and_says_it_is_a_demo():
    response = client().get("/widget/demo")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    assert "script-src 'self'" in response.headers["content-security-policy"] and "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "Trang demo" in response.text and '<script src="/widget/shopmate-chat.js">' in response.text
    assert "<script>" not in response.text  # no inline script: the CSP would block it


def test_a_missing_widget_file_is_a_404_that_names_no_path(tmp_path):
    c = TestClient(create_app(FakeBackend(), widget_dir=tmp_path))
    response = c.get("/widget/shopmate-chat.js")
    assert response.status_code == 404 and str(tmp_path) not in response.text


def test_the_widget_never_turns_server_text_into_html():
    source = (REPO / "widget" / "shopmate-chat.js").read_text(encoding="utf-8")
    for forbidden in (r"\.innerHTML", r"\.outerHTML", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new Function"):
        assert not re.search(forbidden, source), forbidden
    assert source.count("window.sessionStorage") == 2 and re.search(r"try \{\s*var id = window\.sessionStorage", source)  # storage is only touched inside try/catch


def test_the_widget_has_the_behaviour_of_the_brief():
    source = (REPO / "widget" / "shopmate-chat.js").read_text(encoding="utf-8")
    for needle in ("attachShadow", "aria-live", "'api-base'", "ShopMate đang trả lời…", "Có, tạo yêu cầu", "'Không'", "/api/chat/confirm", "e.shiftKey", "e.isComposing",
                   "Intl.NumberFormat('vi-VN')", "'₫'", "noopener noreferrer", "this._send.disabled = this._pending"):
        assert needle in source, needle


# --- starting it -------------------------------------------------------------------------------------------------------------------------------


def test_the_launcher_binds_loopback_only_on_8766_by_default():
    args = parse_args([])
    assert (args.host, args.port, args.fake, args.live) == ("127.0.0.1", 8766, False, False)
    assert DEFAULT_PORT == 8766


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com", ""])
def test_a_host_that_is_not_loopback_is_refused_before_anything_starts(host, capsys):
    assert main(["--host", host, "--fake"]) == 1
    assert "not a loopback address" in capsys.readouterr().err


def test_fake_mode_starts_uvicorn_on_the_given_loopback_address(monkeypatch):
    started = {}
    monkeypatch.setattr("app.chat_api.uvicorn.run", lambda app, **kw: started.update(kw))
    assert main(["--fake", "--port", "8799"]) == 0
    assert started["host"] == "127.0.0.1" and started["port"] == 8799 and started["proxy_headers"] is False


def test_importing_the_module_loads_no_model_and_reads_no_key():
    import subprocess
    import sys

    code = "import sys, app.chat_api; bad = [m for m in ('torch', 'transformers', 'scripts.demo_requests', 'app.retrieval.embedder') if m in sys.modules]; print(bad)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, env={"PATH": "", "PYTHONPATH": str(REPO)}, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout


def test_the_fake_environment_variable_selects_the_fake_backend_without_any_flag(monkeypatch):
    seen = {}
    monkeypatch.setenv("SHOPMATE_CHAT_FAKE", "1")
    monkeypatch.setattr("app.chat_api.uvicorn.run", lambda app, **kw: seen.update(app=app))
    monkeypatch.setattr("app.chat_api.build_live_backend", lambda **kw: pytest.fail("the real agent must not be built in fake mode"))
    assert main([]) == 0
    assert TestClient(seen["app"]).get("/api/health").json() == {"ok": True, "mode": "fake"}
