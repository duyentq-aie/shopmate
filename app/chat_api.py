"""Chat API for the storefront widget (docs/DECISIONS-phase7.md, Part C). Loopback only, no authentication.

    uv run python -m app.chat_api --fake        # canned answers, no agent, no network: for the widget demo, tests and CI
    uv run python -m app.chat_api --live        # the real agent: Gemini (free tier, guarded), Qdrant, read-only Saleor
    uv run python -m app.chat_api               # the real agent, offline: LLM answers come from the cache or fail as llm_cache_miss_offline

  POST /api/chat          {session_id, message}  -> {reply, reply_source, trace_id, awaiting_confirmation, awaiting_offer, products}
  POST /api/chat/confirm  {session_id, confirm}  -> the same; resumes the pending "create a ticket?" question (409 if none is pending)
  GET  /api/health        {"ok": true, "mode": "fake" | "live" | "offline"}
  GET  /widget/shopmate-chat.js   the <shopmate-chat> web component
  GET  /widget/demo               a fake shop page (ShopMate Fashion) that embeds it, with /widget/demo.js and /widget/demo-products.json (built by scripts/build_demo_products.py)

Rules of the API (each one has a test):
- session_id is a UUID, message is 1 to 1000 characters, nothing else is accepted (422 with a short Vietnamese message that does not echo the input).
- At most 6 messages per minute per session, in memory (429 with `Retry-After` and a Vietnamese message).
- CORS: only the storefront's origin (default http://localhost:3000, the port `next dev` uses; SHOPMATE_STOREFRONT_ORIGIN changes it) and this API's own
  origin. A request that carries any other `Origin` is refused with 403, not just left without CORS headers.
- The customer never sees a stack trace, an exception text or a trace detail: only a fixed Vietnamese text and the `trace_id` to look the request up
  in the monitor. The agent is built with catch_errors=True, so a failure inside it is traced as before and the customer gets the technical-problem text.
- `products` holds only what the turn already had in its state (name, kind, color, material, price in VND, in stock) plus the storefront link; no Saleor call is made
  for it. kind, color and material are what the widget needs to draw the fabric-swatch thumbnail of a card.
- `awaiting_offer` is true while the agent has asked "drop these constraints to see close matches?" (D7.25): the widget then shows two buttons, which send a plain
  "có" or "không cần" as the next message; nothing else is needed on the server.
  The link comes from the slug in the versioned seed catalog (data/seed/catalog.vi.json, by SKU): the index payload has no slug.
- The ticket rule is unchanged: a ticket is written only after an explicit yes, here the button "Có, tạo yêu cầu" (confirm=true) or a typed yes.
- Binds 127.0.0.1 only (the launcher refuses any other host), and every call into the agent is serialised by one lock (one embedder, one limiter, a demo load).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.responses import FileResponse, JSONResponse, Response

from app.agent import templates as T
from app.config import REPO_ROOT, get_setting
from app.monitor import is_loopback_host
from app.observability.factory import build_sink
from app.retrieval.folding import fold

log = logging.getLogger("shopmate.chat")

DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8766
DEFAULT_STOREFRONT_ORIGIN = "http://localhost:3000"
DEFAULT_PRODUCT_URL = "http://localhost:3000/en/vn/products/{slug}"
RATE_LIMIT, RATE_WINDOW = 6, 60.0
MAX_MESSAGE = 1000
MAX_BODY_BYTES = 16 * 1024
MAX_PRODUCTS = 5
WIDGET_DIR = REPO_ROOT / "widget"
CATALOG_PATH = REPO_ROOT / "data" / "seed" / "catalog.vi.json"
CACHE_DIR = REPO_ROOT / "logs" / "chat_llm_cache"  # git-ignored, apart from the committed evaluation cache
ANSWER_PROMPT = "answer_v2"  # adopted in Phase 6 (docs/DECISIONS-phase6.md, A17)
DEFAULT_MAX_LIVE_CALLS = 30

MSG_RATE_LIMITED = "Bạn nhắn hơi nhanh, đợi một chút rồi gửi lại giúp mình nhé."
MSG_INVALID = "Tin nhắn chưa hợp lệ: cần từ 1 đến 1000 ký tự."
MSG_NOTHING_TO_CONFIRM = "Hiện chưa có yêu cầu nào đang chờ bạn xác nhận."
MSG_FORBIDDEN_ORIGIN = "Trang này không được phép dùng trợ lý ShopMate."
MSG_TOO_BIG = "Tin nhắn quá dài."
MSG_NOT_FOUND = "Không tìm thấy."
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Cross-Origin-Resource-Policy": "cross-origin"}
# the demo page loads one font family (Be Vietnam Pro) from Google Fonts; nothing else leaves the origin
DEMO_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


# --- what goes over the wire ----------------------------------------------------------------------------------------------------


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    message: str = Field(min_length=1, max_length=MAX_MESSAGE)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank")
        return value


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    confirm: bool


class ProductCard(BaseModel):
    name: str
    price_vnd: int
    in_stock: bool
    kind: str | None = None
    color: str | None = None
    material: str | None = None
    slug: str | None = None
    url: str | None = None


class ChatResponse(BaseModel):
    reply: str
    reply_source: str | None
    trace_id: str | None
    awaiting_confirmation: bool
    awaiting_offer: bool = False
    products: list[ProductCard] = []


def error_body(code: str, message: str, **extra) -> dict:
    return {"error": code, "message": message, **extra}


# --- backends ----------------------------------------------------------------------------------------------------------------------


@dataclass
class BackendReply:
    reply: str
    reply_source: str | None = None
    trace_id: str | None = None
    awaiting_confirmation: bool = False
    products: list[dict] = field(default_factory=list)
    awaiting_offer: bool = False


class ChatBackend(Protocol):
    mode: str

    def reply(self, session_id: str, message: str) -> BackendReply: ...

    def confirm(self, session_id: str, confirm: bool) -> BackendReply | None:
        """None when no confirmation is pending for this session."""
        ...


def load_catalog(path: Path = CATALOG_PATH) -> list[dict]:
    """The versioned seed catalog: sku, slug, name, kind, price, stock. Read from disk; Saleor is never asked."""
    try:
        return list(json.loads(path.read_text(encoding="utf-8"))["products"])
    except (OSError, ValueError, KeyError):
        return []


def product_cards(hits: list[dict], reply: str, slugs: Mapping[str, str], url_template: str) -> list[dict]:
    """Cards for the products the reply names (by SKU), from the hits of the turn. Name, price and stock are the hit's own."""
    cards = []
    for hit in hits:
        sku = hit.get("sku")
        if hit.get("key", "").startswith("product:") and sku and sku in reply:
            slug = slugs.get(sku)
            cards.append({"name": str(hit["name"]), "price_vnd": int(hit["price_vnd"]), "in_stock": bool(hit["in_stock"]),
                          "kind": hit.get("kind"), "color": hit.get("color"), "material": hit.get("material"),
                          "slug": slug, "url": url_template.format(slug=slug) if slug else None})
        if len(cards) >= MAX_PRODUCTS:
            break
    return cards


class AgentBackend:
    """The real agent. `agent` is an app.agent.Agent built with catch_errors=True."""

    def __init__(self, agent, *, slugs: Mapping[str, str], url_template: str = DEFAULT_PRODUCT_URL, mode: str = "live") -> None:
        self.agent, self.slugs, self.url_template, self.mode = agent, dict(slugs), url_template, mode
        self._lock = threading.Lock()

    def _wrap(self, result) -> BackendReply:
        return BackendReply(reply=result.reply, reply_source=result.reply_source, trace_id=result.trace_id,
                            awaiting_confirmation=result.awaiting == "ticket_confirmation", awaiting_offer=result.awaiting == "offer_confirmation",
                            products=product_cards(result.hits, result.reply, self.slugs, self.url_template))

    def reply(self, session_id: str, message: str) -> BackendReply:
        with self._lock:
            return self._wrap(self.agent.turn(session_id, message))

    def confirm(self, session_id: str, confirm: bool) -> BackendReply | None:
        with self._lock:
            if not self.agent.is_awaiting_confirmation(session_id):
                return None
            return self._wrap(self.agent.turn(session_id, "có" if confirm else "không"))


class FakeBackend:
    """Canned answers by keyword: no agent, no LLM, no network, no ticket file. For the demo, the tests and CI.
    A complaint ("khiếu nại", "lỗi", "giao nhầm") makes it ask for a confirmation, as the real agent does before it writes a ticket.
    The one jacket question of the live smoke ("áo khoác dù ... size L") gets the offer the real agent made for it, so the widget's offer buttons can be shown."""

    mode = "fake"
    PRODUCT_WORDS = frozenset({"ao", "quan", "giay", "tui", "vay", "mu", "vi", "balo", "dep", "kinh", "that", "lung"})
    STOP = frozenset({"co", "khong", "shop", "ban", "cho", "minh", "xem", "mau", "nao", "dang", "can", "tim", "mua", "gia", "duoi"})
    CREATED = "Mình đã tạo phiếu hỗ trợ TCK-DEMO000001 (đây là bản demo, không có phiếu thật). Nhân viên sẽ liên hệ với bạn qua email."
    POLICY_RETURN = ("Bạn được đổi kích cỡ hoặc màu sắc trong vòng 7 ngày kể từ ngày nhận hàng; sản phẩm lỗi hoặc giao sai thì trong 30 ngày.\n"
                     "Nguồn: doi-tra, Điều 2")
    POLICY_SHIPPING = ("Phí giao hàng tiêu chuẩn là 30.000đ mỗi đơn, miễn phí cho đơn từ 500.000đ. Nội thành TP.HCM giao trong 1–2 ngày làm việc; "
                       "Hà Nội, Đà Nẵng và các tỉnh khác trong 2–5 ngày làm việc.\nNguồn: van-chuyen, Điều 2 và Điều 3")
    POLICY_WARRANTY = ("Giày dép được bảo hành 6 tháng đối với lỗi bong keo, tuột chỉ, gãy đế; túi, ví và thắt lưng da bò 12 tháng; "
                       "kính mát và phụ kiện kim loại 3 tháng.\nNguồn: bao-hanh, Điều 1")
    OFFER = (T.NO_FULL_MATCH + " Điều kiện đang chặn kết quả: chất liệu dù (cùng với kích cỡ L và màu đen). "
             "Hiện có 2 áo khoác nam mức giá tối đa 500.000đ nếu bỏ các điều kiện này — bạn muốn xem không?")
    OFFER_SKUS = ("SM-NAM-0042", "SM-NAM-0014")

    def __init__(self, catalog: list[dict] | None = None, url_template: str = DEFAULT_PRODUCT_URL) -> None:
        self.catalog = catalog if catalog is not None else load_catalog()
        self.url_template = url_template
        self._awaiting: set[str] = set()
        self._offers: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _id() -> str:
        return uuid.uuid4().hex

    def _card(self, p: dict) -> dict:
        return {"name": p["name"], "price_vnd": int(p["price"]), "in_stock": int(p.get("stock", 0)) > 0, "kind": p.get("kind"), "color": p.get("color"),
                "material": p.get("material"), "slug": p["slug"], "url": self.url_template.format(slug=p["slug"])}

    @staticmethod
    def _price_cap(folded: str) -> int | None:
        match = re.search(r"duoi\s*(\d+)\s*(k|tr|trieu|nghin|ngan)?\b", folded)
        if not match:
            return None
        return int(match.group(1)) * {"k": 1_000, "nghin": 1_000, "ngan": 1_000, "tr": 1_000_000, "trieu": 1_000_000}.get(match.group(2) or "", 1)

    def _products(self, tokens: set[str], cap: int | None = None) -> list[dict]:
        wanted = tokens - self.STOP
        pool = [p for p in self.catalog if int(p.get("stock", 0)) > 0 and (cap is None or int(p["price"]) <= cap)]
        scored = [(len(wanted & set(re.findall(r"[a-z0-9]+", fold(f"{p['kind']} {p['name']}")))), i, p) for i, p in enumerate(pool)]
        ranked = sorted(scored, key=lambda t: (-t[0], t[1]))
        best = ranked[0][0] if ranked else 0
        return [self._card(p) for score, _, p in ranked if score > 0 and score == best][:MAX_PRODUCTS]  # only the best matches: "áo khoác nam" never returns a T-shirt

    @staticmethod
    def _lines(cards: list[dict]) -> str:
        return "\n".join(f"- {c['name']}: " + f"{c['price_vnd']:,}".replace(",", ".") + "đ, " + ("còn hàng" if c["in_stock"] else "hết hàng") for c in cards)

    def _offer_answer(self, session_id: str, message: str) -> BackendReply | None:
        """The answer to the offer asked in the last turn (this is the only turn the offer lives, as in the real agent), or None when the message is something else."""
        from app.agent.parsing import is_plain_decline, parse_yes_no

        self._offers.discard(session_id)
        if parse_yes_no(message) == "yes":
            by_sku = {p["sku"]: p for p in self.catalog}
            cards = [self._card(by_sku[sku]) for sku in self.OFFER_SKUS if sku in by_sku]
            return BackendReply(T.offer_label(["size", "color", "material"]) + "\nBên mình gợi ý cho bạn:\n" + self._lines(cards), "llm", self._id(), False, cards)
        if is_plain_decline(message):
            return BackendReply(T.OFFER_DECLINED, "template", self._id())
        return None

    def reply(self, session_id: str, message: str) -> BackendReply:
        folded = fold(message)
        tokens = set(re.findall(r"[a-z0-9]+", folded))
        with self._lock:
            if session_id in self._offers:
                answered = self._offer_answer(session_id, message)
                if answered is not None:
                    return answered
            if session_id in self._awaiting:  # the question is still open: a typed answer is read like the real agent reads it
                if tokens & {"khong", "ko", "thoi", "huy"}:
                    self._awaiting.discard(session_id)
                    return BackendReply(T.TICKET_CANCELLED, "template", self._id())
                if tokens & {"co", "ok", "vang"} or "dong y" in folded:
                    self._awaiting.discard(session_id)
                    return BackendReply(self.CREATED, "template", self._id())
            if re.search(r"\b(khieu nai|loi|hong|phieu|giao nham)\b", folded):
                self._awaiting.add(session_id)
                return BackendReply('Mình sẽ tạo phiếu hỗ trợ với nội dung sau:\nNội dung: "' + " ".join(message.split())[:120] +
                                    '"\nBạn xác nhận tạo phiếu này không? (trả lời "có" để tạo, "không" để hủy)', "clarify", self._id(), True)
            if "khoac" in tokens and "du" in tokens and "size" in tokens:  # the jacket question that has no exact match: the offer
                self._offers.add(session_id)
                return BackendReply(self.OFFER, "template", self._id(), awaiting_offer=True)
        if re.search(r"bao hanh", folded):
            return BackendReply(self.POLICY_WARRANTY, "llm", self._id())
        if re.search(r"van chuyen|phi ship|freeship|giao hang|\bship\b", folded):
            return BackendReply(self.POLICY_SHIPPING, "llm", self._id())
        if re.search(r"doi tra|doi hang|tra hang", folded):
            return BackendReply(self.POLICY_RETURN, "llm", self._id())
        if tokens & {"don", "order"}:
            return BackendReply(T.ASK_NUMBER_AND_EMAIL, "clarify", self._id())
        if tokens & self.PRODUCT_WORDS:
            cards = self._products(tokens, self._price_cap(folded))
            if cards:
                return BackendReply("Mình tìm được vài sản phẩm phù hợp (dữ liệu demo):\n" + self._lines(cards), "llm", self._id(), False, cards)
            return BackendReply(T.NO_RESULTS, "template", self._id())
        if tokens & {"chao", "hello", "hi", "alo", "cam"}:
            return BackendReply(T.SMALLTALK, "template", self._id())
        return BackendReply(T.OUT_OF_SCOPE, "template", self._id())

    def confirm(self, session_id: str, confirm: bool) -> BackendReply | None:
        with self._lock:
            if session_id not in self._awaiting:
                return None
            self._awaiting.discard(session_id)
        return BackendReply(self.CREATED, "template", self._id()) if confirm else BackendReply(T.TICKET_CANCELLED, "template", self._id())


# --- limits -----------------------------------------------------------------------------------------------------------------------


class SessionRateLimiter:
    """At most `limit` messages per `window` seconds per session, in memory. Finished sessions are forgotten."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW, clock: Callable[[], float] = time.monotonic, max_sessions: int = 10_000) -> None:
        self.limit, self.window, self._clock, self.max_sessions = limit, window, clock, max_sessions
        self._seen: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> float | None:
        """None if the message is allowed (and counted); else the seconds to wait."""
        with self._lock:
            now = self._clock()
            if len(self._seen) >= self.max_sessions and key not in self._seen:
                self._seen = {k: q for k, q in self._seen.items() if q and now - q[-1] < self.window}
                if len(self._seen) >= self.max_sessions:
                    return self.window
            queue = self._seen.setdefault(key, deque())
            while queue and now - queue[0] >= self.window:
                queue.popleft()
            if len(queue) >= self.limit:
                return max(1.0, self.window - (now - queue[0]))
            queue.append(now)
            return None


# --- the app ----------------------------------------------------------------------------------------------------------------------


def create_app(backend: ChatBackend, *, storefront_origin: str = DEFAULT_STOREFRONT_ORIGIN, port: int = DEFAULT_PORT, limiter: SessionRateLimiter | None = None,
               widget_dir: Path = WIDGET_DIR) -> FastAPI:
    limiter = limiter or SessionRateLimiter()
    own = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    allowed = {storefront_origin.rstrip("/")} | own
    app = FastAPI(title="ShopMate chat", docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin is not None and origin.rstrip("/") not in allowed:  # a page of another site: not served, whatever CORS would say
            return JSONResponse(error_body("forbidden_origin", MSG_FORBIDDEN_ORIGIN), status_code=403)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse(error_body("too_big", MSG_TOO_BIG), status_code=413)
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(CORSMiddleware, allow_origins=[storefront_origin.rstrip("/")], allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type"],
                       allow_credentials=False, max_age=600)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request: Request, _exc: RequestValidationError):  # nothing of the input or of the schema is echoed
        return JSONResponse(error_body("invalid_request", MSG_INVALID), status_code=422)

    @app.exception_handler(Exception)
    async def broken(_request: Request, exc: Exception):
        log.error("unhandled %s", type(exc).__name__)
        return JSONResponse(error_body("internal", T.TECHNICAL_PROBLEM), status_code=500)

    def respond(call: Callable[[], BackendReply | None], session: str) -> Response:
        wait = limiter.check(session)
        if wait is not None:
            return JSONResponse(error_body("rate_limited", MSG_RATE_LIMITED, retry_after=int(wait + 0.999)), status_code=429, headers={"Retry-After": str(int(wait + 0.999))})
        try:
            result = call()
        except Exception as exc:  # a bug in the glue, not a failure of the agent (the agent traps its own): fixed text, class name in the server log only
            log.error("chat backend failed: %s", type(exc).__name__)
            return JSONResponse(error_body("internal", T.TECHNICAL_PROBLEM), status_code=500)
        if result is None:
            return JSONResponse(error_body("nothing_to_confirm", MSG_NOTHING_TO_CONFIRM), status_code=409)
        body = ChatResponse(reply=result.reply, reply_source=result.reply_source, trace_id=result.trace_id, awaiting_confirmation=result.awaiting_confirmation,
                            awaiting_offer=result.awaiting_offer, products=[ProductCard(**p) for p in result.products])
        return JSONResponse(body.model_dump())

    @app.post("/api/chat")
    def chat(req: ChatRequest) -> Response:
        session = str(req.session_id)
        return respond(lambda: backend.reply(session, req.message), session)

    @app.post("/api/chat/confirm")
    def confirm(req: ConfirmRequest) -> Response:
        session = str(req.session_id)
        return respond(lambda: backend.confirm(session, req.confirm), session)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "mode": backend.mode}

    def static(name: str, media_type: str, csp: str | None = None) -> Response:
        path = widget_dir / name
        if not path.is_file():
            return JSONResponse(error_body("not_found", MSG_NOT_FOUND), status_code=404)
        headers = {"Cache-Control": "no-cache"}
        if csp:
            headers["Content-Security-Policy"] = csp
        return FileResponse(path, media_type=media_type, headers=headers)

    @app.get("/widget/shopmate-chat.js")
    def widget_script() -> Response:
        return static("shopmate-chat.js", "text/javascript; charset=utf-8")

    @app.get("/widget/demo")
    def widget_demo() -> Response:
        return static("demo.html", "text/html; charset=utf-8", DEMO_CSP)

    @app.get("/widget/demo.js")
    def widget_demo_script() -> Response:
        return static("demo.js", "text/javascript; charset=utf-8")

    @app.get("/widget/demo-products.json")
    def widget_demo_products() -> Response:
        return static("demo-products.json", "application/json; charset=utf-8")

    return app


# --- starting it --------------------------------------------------------------------------------------------------------------------


def build_tracer():
    """The tracer of the real agent. Langfuse is switched on here and nowhere else: with LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY set the tracer also
    sends every request to Langfuse (docs/DECISIONS-langfuse.md); without them it is the tracer of before, and `langfuse` is not even imported."""
    from app.tracing import Tracer

    return Tracer(sink=build_sink())


def build_live_backend(*, live: bool, max_live_calls: int, storefront_url_template: str) -> AgentBackend:
    """The real agent. Heavy imports and the key are only touched here, never when the module is imported (tests use FakeBackend)."""
    from app.agent.agent import Agent
    from app.agent.answer import GeminiAnswerer
    from app.agent.tools import RealTools, TicketStore
    from app.config import require_setting, api_url
    from app.llm.cache import DiskCache
    from app.llm.extractor import FilterExtractor
    from app.llm.generator import CachedGenerator
    from app.llm.prompts import load_prompt
    from app.llm.router import GeminiRouter
    from app.saleor.readonly_client import ReadOnlySaleorClient
    from scripts.demo_requests import build_llm, build_retriever  # the same free-tier guards and index check as the demo script

    llm = build_llm(live=live, max_live_calls=max_live_calls)
    retriever = build_retriever()
    generator = CachedGenerator(DiskCache(CACHE_DIR), llm)
    saleor = ReadOnlySaleorClient(api_url(), require_setting("SALEOR_RUNTIME_TOKEN"))
    agent = Agent(router=GeminiRouter(generator, load_prompt("router_v1")), extractor=FilterExtractor(generator, load_prompt("filters_v1")),
                  tools=RealTools(retriever, saleor, TicketStore()), answerer=GeminiAnswerer(generator, load_prompt(ANSWER_PROMPT)), tracer=build_tracer(),
                  catch_errors=True)
    return AgentBackend(agent, slugs={p["sku"]: p["slug"] for p in load_catalog()}, url_template=storefront_url_template, mode="live" if live else "offline")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.chat_api", description="ShopMate chat API for the storefront widget (loopback only).")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"loopback address to bind (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--fake", action="store_true", help="canned answers, no agent (also SHOPMATE_CHAT_FAKE=1)")
    parser.add_argument("--live", action="store_true", help="really call Gemini (free tier, guarded; reads GEMINI_API_KEY)")
    parser.add_argument("--max-live-calls", type=int, default=DEFAULT_MAX_LIVE_CALLS, help="cap on live Gemini requests of this process")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not is_loopback_host(args.host):
        print(f"refusing to start: host {args.host!r} is not a loopback address. The chat API has no authentication, so it binds to 127.0.0.1 only.", file=sys.stderr)
        return 1
    origin = get_setting("SHOPMATE_STOREFRONT_ORIGIN", DEFAULT_STOREFRONT_ORIGIN)
    url_template = get_setting("SHOPMATE_STOREFRONT_PRODUCT_URL", DEFAULT_PRODUCT_URL)
    if args.fake or get_setting("SHOPMATE_CHAT_FAKE") == "1":
        backend: ChatBackend = FakeBackend(url_template=url_template)
    else:
        try:
            backend = build_live_backend(live=args.live, max_live_calls=args.max_live_calls, storefront_url_template=url_template)
        except Exception as exc:  # no key, Qdrant down, index drift: say which, do not start half-working
            print(f"refusing to start the real agent: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    print(f"ShopMate chat API ({backend.mode}): http://{args.host}:{args.port}/widget/demo  (CORS origin {origin}; Ctrl+C to stop)", file=sys.stderr)
    uvicorn.run(create_app(backend, storefront_origin=origin, port=args.port), host=args.host, port=args.port, proxy_headers=False, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
