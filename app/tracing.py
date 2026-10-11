"""Request tracing: ONE JSON line per request in logs/traces.jsonl (git-ignored).

    with tracer.request(question) as trace:          # one line is written when the block ends, whatever happens
        with span("router", model=...) as sp:        # a timed step; no-op outside a request
            ...

Schema of a line (exactly these keys). Version 2 (what is written now; docs/DECISIONS-phase7.md D7.1):
  schema_version 2, trace_id, ts (ISO UTC, ms), session_id | null (the agent's thread id), turn | null (1, 2, ... in that
  conversation), question (what the customer typed, through `mask`: emails, keys and phone numbers hidden), intent | null, status ok|warn|error,
  reply | null (what the customer received, through the same `mask`, cut to 800 characters after masking),
  reply_truncated (bool), reply_source llm|template|guardrail_fallback|clarify|error_template | null,
  spans[] {stage, start_ms, duration_ms, model, tokens_in, tokens_out, cache_hit, error {type, message} | null,
           summary {...}},
  error {type, message} | null.
Version 1 (before Phase 7) had none of schema_version, session_id, turn, reply, reply_truncated, reply_source and no span
summary; such lines stay valid for `validate_trace_line` and are still read by the dashboard.
A span's `summary` is a small dict of what is needed to understand the decision of that step, never a prompt or a policy text:
  router {intent}; extractor {filters, relaxed?}; retrieval {mode, k, hits [{id, kind, title, score}] (at most 5)};
  order_lookup {verified, found, attempts}; ticket_confirm {state: asked|confirmed|declined|written};
  answer {prompt_version, guardrail {checked, violations, fallback_used} | null}; reply {template}.
All the summaries of a line together are at most 4 KB serialised: hits are cut first (D7.2).
status: error if the request has an error, warn if only spans have one (the request went on), else ok.
start_ms and duration_ms are milliseconds from the start of the request.

Tracing never changes behaviour: spans are no-ops outside a request, exceptions are recorded and re-raised
untouched (BaseException too: Ctrl+C and sys.exit() end a request, so they are recorded as errors, never as a success;
only GeneratorExit, which is how Python closes a generator and not a failure, passes through unrecorded), and a failing
log write is reported on stderr and otherwise ignored.

Tokens are the numbers of the Gemini `usageMetadata` of the response, never estimates (null when the response has
none); tokens_out is candidates + thought tokens, the billed output. A cache hit carries the recorded metadata of the
answer that was cached, with cache_hit true: it was not billed again.

Nothing secret is written: emails become "[email]"; API keys, tokens and passwords (the values configured in the
environment or .env, key-like strings, Bearer tokens, `key=value` assignments) become "[secret]", in the question,
the error messages and every other string of the line. Phone numbers (Vietnamese format) become "[sđt]" in everything the
customer typed or was told: the question, the reply and the span summaries, all through the one function `mask`
(docs/DECISIONS-phase7.md, D7.8). Ids, model names and error messages get `redact` only: a hex id could contain digits that look like a phone number.

error.type is one of ERROR_TYPES. A trace never names a cause that did not happen, so every distinct failure has its
own type and nothing is mapped to the nearest one (details and the real cause are in `message`):
  llm_429_minute         WARNING only: a per-minute 429 was retried and the call then succeeded; never an error
  llm_429_daily          the API's own daily-quota 429
  llm_rate_limited       a 429 (per-minute or unrecognised) on every attempt: retries exhausted
  llm_unavailable        5xx or network failure after the retries were exhausted
  llm_request_error      HTTP 4xx from the API (including a limit of 0 on this key for this model)
  llm_cache_miss_offline cache miss while running without --live
  budget_cap_reached     OUR daily ceiling or per-run call cap (the request was not sent)
  llm_invalid_output     the LLM answered but gave nothing usable (not JSON, outside the enum, blocked)
  filter_out_of_vocab    the extractor dropped a value (outside the closed vocabularies, or an invalid price)
  qdrant_unreachable     the transport to Qdrant failed (connection refused, timeout)
  saleor_timeout         a real timeout talking to Saleor
  saleor_error           any other SaleorQueryError (unreachable, HTTP status, GraphQL error)
  readonly_violation     the read-only Saleor client refused a request
  request_interrupted    KeyboardInterrupt or SystemExit ended the request before it finished (it is re-raised, never swallowed)
  answer_guardrail_fallback  WARNING of one span: a guardrail rejected the generated answer and a fixed template was sent
  agent_step_cap_reached the agent's turn hit its step cap and was ended with a safe reply
  ticket_write_failed    the customer confirmed a ticket but it could not be written
  internal_error         any other exception: a bug (without a type of its own it would be untraceable)
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from qdrant_client.http.exceptions import ResponseHandlingException

from app.config import DEFAULT_ENV_FILE, REPO_ROOT, read_env_file
from app.llm.errors import (
    ApiError,
    BadResponse,
    CacheMiss,
    CallCapExceeded,
    DailyBudgetReached,
    DailyLimitExhausted,
    FreeTierUnavailable,
    LLMError,
    RetriesExhausted,
)
from app.llm.intents import INTENTS

TRACE_PATH = REPO_ROOT / "logs" / "traces.jsonl"
SCHEMA_VERSION = 2
REPLY_SOURCES = ("llm", "template", "guardrail_fallback", "clarify", "error_template")
REPLY_LIMIT = 800  # characters of the reply kept in a line
SUMMARY_LIMIT = 4096  # bytes of all the span summaries of one line together, serialised
MAX_HITS = 5
STAGES = ("router", "extractor", "retrieval", "order_lookup", "ticket_confirm", "reply", "answer")
ERROR_TYPES = ("llm_429_minute", "llm_429_daily", "llm_rate_limited", "llm_unavailable", "llm_request_error",
               "llm_cache_miss_offline", "budget_cap_reached", "llm_invalid_output", "filter_out_of_vocab", "qdrant_unreachable",
               "saleor_timeout", "saleor_error", "readonly_violation", "request_interrupted", "answer_guardrail_fallback",
               "agent_step_cap_reached", "ticket_write_failed", "internal_error")
WARNING_ONLY = frozenset({"llm_429_minute"})  # a recovered condition: valid on a span, never the reason a request failed
SPAN_ONLY = frozenset({"filter_out_of_vocab", "answer_guardrail_fallback"})  # warnings of one span; a request never fails with them
STATUSES = ("ok", "warn", "error")
MESSAGE_LIMIT = 300

# --- masking -------------------------------------------------------------------------------------------------------------

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),  # Google API keys
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]+=*"),  # Authorization header values
    re.compile(r"(?i)\b((?:api[_-]?key|token|secret|password)\s*[=:]\s*)\S+"),  # name=value, name: value
)
# the same pattern as app.agent.parsing.PHONE (a test keeps them equal); not imported: the agent imports this module
PHONE = re.compile(r"(?<![\d@])(?:\+?84|0)(?:[\s.\-]?\d){9}(?!\d)")
PHONE_MASK = "[sđt]"
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.I)
MIN_SECRET_LENGTH = 8


def known_secrets(env_file: Path = DEFAULT_ENV_FILE) -> list[str]:
    """Values of the configured settings whose NAME says they are secrets (environment and .env)."""
    values = {**read_env_file(env_file), **os.environ}
    return sorted({v for k, v in values.items() if SECRET_NAME.search(k) and len(v) >= MIN_SECRET_LENGTH}, key=len, reverse=True)


def redact(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    for secret in sorted((s for s in secrets if len(s) >= MIN_SECRET_LENGTH), key=len, reverse=True):
        text = text.replace(secret, "[secret]")
    text = SECRET_PATTERNS[0].sub("[secret]", text)
    text = SECRET_PATTERNS[1].sub("Bearer [secret]", text)
    text = SECRET_PATTERNS[2].sub(r"\1[secret]", text)
    return EMAIL.sub("[email]", text)


def mask(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """THE mask of a line's customer text (the question, the reply, every string of a summary): `redact`, then Vietnamese phone numbers -> "[sđt]".
    Emails go first, so a phone number inside an address is part of "[email]". Order numbers and amounts have fewer than ten digits and are left alone."""
    return PHONE.sub(PHONE_MASK, redact(text, secrets))


# --- exceptions -> error.type ----------------------------------------------------------------------------------------------


def classify_exception(exc: BaseException, stage: str | None = None) -> tuple[str, str]:
    """(error.type, message) for an exception raised in `stage`. The message is the class name and the text, cut short.

    Only a type that names what really happened is returned; an exception that fits none is `internal_error`.
    Never `llm_429_minute`: that is the warning of a call that recovered, and an exception means it did not."""
    message = f"{type(exc).__name__}: {str(exc)[:MESSAGE_LIMIT]}"
    if isinstance(exc, KeyboardInterrupt):
        return "request_interrupted", "KeyboardInterrupt: the request was interrupted before it finished"
    if isinstance(exc, SystemExit):
        return "request_interrupted", f"SystemExit: the process was asked to exit (code {str(exc.code)[:100]!r}) before the request finished"
    if isinstance(exc, DailyLimitExhausted):
        return "llm_429_daily", message
    if isinstance(exc, (DailyBudgetReached, CallCapExceeded)):
        return "budget_cap_reached", message
    if isinstance(exc, FreeTierUnavailable):  # the API's 4xx: this key has no quota for this model
        return "llm_request_error", message
    if isinstance(exc, RetriesExhausted):
        if exc.cause == "rate_limit":
            return "llm_rate_limited", message
        if exc.cause in ("server_error", "network"):
            return "llm_unavailable", message
        return "internal_error", message  # no cause recorded: claim none
    if isinstance(exc, ApiError):
        return ("llm_request_error" if 400 <= exc.status < 500 else "internal_error"), message
    if isinstance(exc, CacheMiss):
        return "llm_cache_miss_offline", message
    if isinstance(exc, BadResponse):
        return "llm_invalid_output", message
    from app.agent.tools import TicketWriteError  # lazy: tracing must not import the agent at load time
    from app.saleor.readonly_client import ReadOnlyViolation, SaleorQueryError  # lazy: retrieval does not need Saleor

    if isinstance(exc, TicketWriteError):
        return "ticket_write_failed", message

    if isinstance(exc, ReadOnlyViolation):
        return "readonly_violation", message
    if isinstance(exc, SaleorQueryError):  # the client wraps the httpx error it got as __cause__
        return ("saleor_timeout" if isinstance(exc.__cause__, httpx.TimeoutException) else "saleor_error"), message
    if isinstance(exc, ResponseHandlingException):  # qdrant-client wraps what it could not get or understand
        return ("qdrant_unreachable" if isinstance(exc.source, httpx.TransportError) else "internal_error"), message
    if isinstance(exc, httpx.TransportError):  # raw transport errors: which service depends on the stage
        if stage == "retrieval":
            return "qdrant_unreachable", message
        if stage in ("order_lookup", "ticket_confirm"):
            return ("saleor_timeout" if isinstance(exc, httpx.TimeoutException) else "saleor_error"), message
    return "internal_error", message


# --- spans and traces ---------------------------------------------------------------------------------------------------


def _check_failure_type(error_type: str, *, request: bool = False) -> None:
    """request=True: the failure of the whole request (Trace.fail); False: the error or warning of one span."""
    if error_type not in ERROR_TYPES:
        raise ValueError(f"unknown error type {error_type!r}")
    if error_type in WARNING_ONLY:
        raise ValueError(f"{error_type!r} is the warning of a call that recovered, not a failure")
    if request and error_type in SPAN_ONLY:
        raise ValueError(f"{error_type!r} is a warning of one span, not the failure of a request")


class SpanRecorder:
    """What the code inside a span can say about it."""

    def __init__(self, stage: str, model: str | None) -> None:
        self.stage, self.model = stage, model
        self.tokens_in: int | None = None
        self.tokens_out: int | None = None
        self.cache_hit: bool | None = None
        self.error: dict | None = None
        self.recovered: dict | None = None  # a warning: something went wrong and was overcome; yields to a real error
        self.summary: dict = {}  # what is needed to understand this step's decision (see the module docstring)

    def set_summary(self, **fields) -> None:
        self.summary.update(fields)

    def set_usage(self, *, tokens_in: int | None = None, tokens_out: int | None = None, cache_hit: bool | None = None) -> None:
        self.tokens_in, self.tokens_out, self.cache_hit = tokens_in, tokens_out, cache_hit

    def record_response(self, result) -> None:
        """Tokens and cache flag of a GenResult. Tokens come from the response's usageMetadata, and only if it has one."""
        if (result.raw or {}).get("usageMetadata"):
            self.tokens_in = result.usage.input_tokens
            self.tokens_out = result.usage.output_tokens + result.usage.thought_tokens
        self.cache_hit = result.cached
        if result.minute_429_retries > 0:  # this call hit a per-minute 429, retried, and then got its answer
            self.recovered = {"type": "llm_429_minute", "message": f"per-minute 429 retried {result.minute_429_retries} "
                                                                    "time(s); the call then succeeded"}

    def fail(self, error_type: str, message: str) -> None:
        _check_failure_type(error_type)
        self.error = {"type": error_type, "message": message[:MESSAGE_LIMIT]}


class Trace:
    def __init__(self, question: str, ts: dt.datetime, trace_id: str, clock: Callable[[], float], *,
                 session_id: str | None = None, turn: int | None = None) -> None:
        self.question, self.ts, self.trace_id = question, ts, trace_id
        self.session_id, self.turn = session_id, turn
        self.intent: str | None = None
        self.reply: str | None = None  # what the customer received (raw here; the Tracer masks and cuts it when it writes the line)
        self.reply_source: str | None = None
        self.error: dict | None = None
        self.spans: list[dict] = []
        self._clock, self._t0 = clock, clock()
        self._escaping: tuple[BaseException, dict] | None = None  # the exception a span saw, and how it classified it

    def fail(self, error_type: str, message: str) -> None:
        _check_failure_type(error_type, request=True)
        self.error = {"type": error_type, "message": message[:MESSAGE_LIMIT]}

    def set_reply(self, text: str | None, source: str | None) -> None:
        """What the customer was given and where it came from (one of REPLY_SOURCES); None for both if the customer saw nothing."""
        if (text is None) != (source is None):
            raise ValueError("reply and reply_source are both set or both None")
        if source is not None and source not in REPLY_SOURCES:
            raise ValueError(f"unknown reply source {source!r}")
        self.reply, self.reply_source = text, source

    def annotate(self, stage: str, **fields) -> None:
        """Add fields to the summary of the most recent span of `stage` (the step is over, a later step learned more about it)."""
        for recorded in reversed(self.spans):
            if recorded["stage"] == stage:
                recorded["summary"].update(fields)
                return

    def record_exception(self, exc: BaseException) -> None:
        """Make the request an error because of `exc` (unless it already failed): the type the span that saw it chose, or its own."""
        if self.error is None:
            seen = self._escaping
            if seen is not None and seen[0] is exc:
                self.error = dict(seen[1])  # the span that raised already classified it
            else:
                kind, message = classify_exception(exc)
                self.error = {"type": kind, "message": message}

    @property
    def status(self) -> str:
        if self.error is not None:
            return "error"
        return "warn" if any(s["error"] for s in self.spans) else "ok"


_CURRENT: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("shopmate_trace", default=None)


def current_trace() -> Trace | None:
    return _CURRENT.get()


def record_intent(intent: str | None) -> None:
    trace = current_trace()
    if trace is not None and intent in INTENTS:
        trace.intent = intent


def annotate_span(stage: str, **fields) -> None:
    """Add to the summary of the latest span of `stage` of the current request; nothing outside a request or without such a span."""
    trace = current_trace()
    if trace is not None:
        trace.annotate(stage, **fields)


@contextmanager
def span(stage: str, model: str | None = None) -> Iterator[SpanRecorder]:
    """A timed step of the current request. Outside a request it does nothing but yield a recorder."""
    recorder = SpanRecorder(stage, model)
    trace = current_trace()
    if trace is None:
        yield recorder
        return
    start = trace._clock()
    try:
        yield recorder
    except GeneratorExit:  # Python closing a generator is not a failure of this step: let it through, record nothing
        raise
    except BaseException as exc:  # incl. KeyboardInterrupt and SystemExit: the step did not finish, and the exception goes on
        if recorder.error is None:
            kind, message = classify_exception(exc, stage)
            recorder.error = {"type": kind, "message": message}
        trace._escaping = (exc, recorder.error)
        raise
    finally:
        end = trace._clock()
        trace.spans.append({"stage": stage, "start_ms": round((start - trace._t0) * 1000, 1),
                            "duration_ms": round((end - start) * 1000, 1), "model": recorder.model,
                            "tokens_in": recorder.tokens_in, "tokens_out": recorder.tokens_out,
                            "cache_hit": recorder.cache_hit, "error": recorder.error or recorder.recovered,
                            "summary": recorder.summary})


def summary_bytes(summary) -> int:
    return len(json.dumps(summary, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def summaries_size(spans: list[dict]) -> int:
    """Bytes of all the span summaries of a line together, as they are serialised."""
    return sum(summary_bytes(s.get("summary", {})) for s in spans)


def fit_summaries(spans: list[dict], limit: int = SUMMARY_LIMIT) -> None:
    """Make the summaries of a line together at most `limit` bytes, in place. The hits go first (last hit of the span with the most hits,
    one at a time, and the span says `hits_truncated`); if that is not enough, the biggest summaries are replaced by {"truncated": true}."""
    while summaries_size(spans) > limit:
        with_hits = [s for s in spans if s["summary"].get("hits")]
        if not with_hits:
            break
        fattest = max(with_hits, key=lambda s: len(s["summary"]["hits"]))
        fattest["summary"]["hits"].pop()
        fattest["summary"]["hits_truncated"] = True
    while summaries_size(spans) > limit:
        biggest = max((s for s in spans if s["summary"] != {"truncated": True}), key=lambda s: summary_bytes(s["summary"]), default=None)
        if biggest is None:
            break
        biggest["summary"] = {"truncated": True}


class Tracer:
    def __init__(self, path: Path = TRACE_PATH, *, secrets: list[str] | None = None,
                 clock: Callable[[], float] = time.perf_counter,
                 now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
                 new_id: Callable[[], str] = lambda: uuid.uuid4().hex) -> None:
        self.path = Path(path)
        self.secrets = known_secrets() + list(secrets or [])
        self._clock, self._now, self._new_id = clock, now, new_id
        self._lock = threading.Lock()

    @contextmanager
    def request(self, question: str, *, session_id: str | None = None, turn: int | None = None) -> Iterator[Trace]:
        trace = Trace(question, self._now(), self._new_id(), self._clock, session_id=session_id, turn=turn)
        token = _CURRENT.set(trace)
        try:
            yield trace
        except GeneratorExit:  # the generator that holds this request open is being closed: not a failure
            raise
        except BaseException as exc:  # incl. KeyboardInterrupt and SystemExit: the line says error, and the exception goes on
            trace.record_exception(exc)
            raise
        finally:
            _CURRENT.reset(token)
            self._write(trace)

    def _clean(self, text: str | None) -> str | None:
        """`redact` only: for ids, model names and error messages (see `mask` for the customer's own text)."""
        return None if text is None else redact(text, self.secrets)

    def _mask(self, text: str | None) -> str | None:
        return None if text is None else mask(text, self.secrets)

    def _clean_reply(self, text: str | None) -> tuple[str | None, bool]:
        """(the reply as it is written, whether it was cut): masked first, so a secret is never cut in half and left readable."""
        if text is None:
            return None, False
        masked = mask(text, self.secrets)
        return (masked[:REPLY_LIMIT], True) if len(masked) > REPLY_LIMIT else (masked, False)

    def _clean_deep(self, value):
        """A summary with every string through `mask`, like the question and the reply."""
        if isinstance(value, str):
            return mask(value, self.secrets)
        if isinstance(value, dict):
            return {str(k): self._clean_deep(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._clean_deep(v) for v in value]
        return value

    def _error(self, error: dict | None) -> dict | None:
        return None if error is None else {"type": error["type"], "message": self._clean(error["message"])}

    def line(self, trace: Trace) -> dict:
        reply, truncated = self._clean_reply(trace.reply)
        spans = [{**s, "model": self._clean(s["model"]), "error": self._error(s["error"]), "summary": self._clean_deep(s["summary"])}
                 for s in trace.spans]
        fit_summaries(spans)
        return {
            "schema_version": SCHEMA_VERSION,
            "trace_id": trace.trace_id,
            "ts": trace.ts.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "session_id": self._clean(trace.session_id), "turn": trace.turn,
            "question": self._mask(trace.question), "intent": trace.intent, "status": trace.status,
            "reply": reply, "reply_truncated": truncated, "reply_source": trace.reply_source,
            "spans": spans,
            "error": self._error(trace.error),
        }

    def _write(self, trace: Trace) -> None:
        try:
            text = json.dumps(self.line(trace), ensure_ascii=False, separators=(",", ":"))
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as out:
                    out.write(text + "\n")
        except Exception as exc:  # tracing must never break a request
            print(f"trace write failed: {type(exc).__name__}", file=sys.stderr)


# --- validation of a line ------------------------------------------------------------------------------------------------

_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_TOP = {"trace_id", "ts", "question", "intent", "status", "spans", "error"}  # version 1: the lines written before Phase 7
_SPAN = {"stage", "start_ms", "duration_ms", "model", "tokens_in", "tokens_out", "cache_hit", "error"}
_TOP_V2 = _TOP | {"schema_version", "session_id", "turn", "reply", "reply_truncated", "reply_source"}
_SPAN_V2 = _SPAN | {"summary"}


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _is_count(value) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _error_problems(error, where: str) -> list[str]:
    if error is None:
        return []
    if not isinstance(error, dict) or set(error) != {"type", "message"}:
        return [f"{where}: must be null or exactly {{type, message}}"]
    problems = []
    if error["type"] not in ERROR_TYPES:
        problems.append(f"{where}.type {error['type']!r} is not an error type")
    if not isinstance(error["message"], str):
        problems.append(f"{where}.message is not a string")
    return problems


def _v2_problems(line: dict) -> list[str]:
    problems = []
    if line["schema_version"] != SCHEMA_VERSION or isinstance(line["schema_version"], bool):
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    if line["session_id"] is not None and not isinstance(line["session_id"], str):
        problems.append("session_id must be a string or null")
    turn = line["turn"]
    if turn is not None and not (isinstance(turn, int) and not isinstance(turn, bool) and turn >= 1):
        problems.append("turn must be a positive integer or null")
    reply, source = line["reply"], line["reply_source"]
    if reply is not None and not isinstance(reply, str):
        problems.append("reply must be a string or null")
    elif isinstance(reply, str) and len(reply) > REPLY_LIMIT:
        problems.append(f"reply is longer than {REPLY_LIMIT} characters")
    if source is not None and source not in REPLY_SOURCES:
        problems.append(f"reply_source {source!r} is not one of {list(REPLY_SOURCES)}")
    if (reply is None) != (source is None):
        problems.append("reply and reply_source must be both set or both null")
    if not isinstance(line["reply_truncated"], bool):
        problems.append("reply_truncated must be a boolean")
    elif line["reply_truncated"] and not (isinstance(reply, str) and len(reply) == REPLY_LIMIT):
        problems.append(f"reply_truncated is true but the reply is not {REPLY_LIMIT} characters long")
    return problems


def validate_trace_line(line) -> list[str]:
    """What is wrong with a parsed trace line ([] if it matches the schema exactly). A line without `schema_version` is
    a version 1 line (before Phase 7) and is checked against the version 1 schema."""
    if not isinstance(line, dict):
        return ["the line is not a JSON object"]
    problems = []
    v2 = "schema_version" in line
    top, span_keys = (_TOP_V2, _SPAN_V2) if v2 else (_TOP, _SPAN)
    if set(line) != top:
        problems.append(f"keys differ: missing {sorted(top - set(line))}, extra {sorted(set(line) - top)}")
        return problems
    if v2:
        problems += _v2_problems(line)
    if not (isinstance(line["trace_id"], str) and re.fullmatch(r"[0-9a-f]{32}", line["trace_id"])):
        problems.append("trace_id must be 32 hex characters")
    if not (isinstance(line["ts"], str) and _TS.fullmatch(line["ts"])):
        problems.append("ts must be ISO UTC with milliseconds, like 2026-10-10T12:30:45.123Z")
    if not isinstance(line["question"], str):
        problems.append("question must be a string")
    if line["intent"] is not None and line["intent"] not in INTENTS:
        problems.append("intent must be one of the six intents or null")
    if line["status"] not in STATUSES:
        problems.append("status must be ok, warn or error")
    problems += _error_problems(line["error"], "error")
    if isinstance(line["error"], dict) and line["error"].get("type") in WARNING_ONLY | SPAN_ONLY:
        problems.append(f"error.type {line['error']['type']!r} is a warning of a span; it cannot fail a request")
    if not isinstance(line["spans"], list):
        return problems + ["spans must be a list"]
    span_errors = False
    for i, s in enumerate(line["spans"]):
        where = f"spans[{i}]"
        if not isinstance(s, dict) or set(s) != span_keys:
            problems.append(f"{where}: keys differ from the span schema")
            continue
        if v2 and not isinstance(s["summary"], dict):
            problems.append(f"{where}.summary must be an object")
        if s["stage"] not in STAGES:
            problems.append(f"{where}.stage {s['stage']!r} is not a stage")
        if not (_is_number(s["start_ms"]) and _is_number(s["duration_ms"])):
            problems.append(f"{where}: start_ms and duration_ms must be non-negative numbers")
        if s["model"] is not None and not isinstance(s["model"], str):
            problems.append(f"{where}.model must be a string or null")
        if not (_is_count(s["tokens_in"]) and _is_count(s["tokens_out"])):
            problems.append(f"{where}: tokens must be non-negative integers or null")
        if s["cache_hit"] is not None and not isinstance(s["cache_hit"], bool):
            problems.append(f"{where}.cache_hit must be a boolean or null")
        problems += _error_problems(s["error"], f"{where}.error")
        span_errors = span_errors or s["error"] is not None
    if v2 and not problems and summaries_size(line["spans"]) > SUMMARY_LIMIT:
        problems.append(f"the span summaries together are longer than {SUMMARY_LIMIT} bytes")
    if not problems:  # the status must agree with the errors
        expected = "error" if line["error"] is not None else ("warn" if span_errors else "ok")
        if line["status"] != expected:
            problems.append(f"status {line['status']!r} does not match the errors (expected {expected!r})")
    return problems
