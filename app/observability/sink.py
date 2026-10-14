"""The Langfuse sink: what one traced request becomes in Langfuse (docs/DECISIONS-langfuse.md, L2-L6).

The Tracer (app/tracing.py) calls four hooks, in this order, and catches whatever they raise, so Langfuse can never break a request:

    request_started(trace_id=, session_id=, turn=)       opens the root observation "shopmate-turn"            -> request
    span_started(request, stage=, model=)                opens a child observation, when the step begins       -> span
    span_ended(request, span, index=)                    says which entry of the line's `spans` the child is
    request_ended(request, line, duration_ms=)           sends the data of the finished line and ends everything

Observations are opened when their step begins and ended at the end of the request with their measured durations, because the SDK takes a start
time from the clock and only the end time can be given, and because `annotate_span` still adds to a span after it has finished. The end is the
start this module read from the clock just after the SDK opened the observation, plus the measured duration: an observation therefore never ends
before it starts, and lasts its measured duration plus at most what the SDK needs to open it (a few tenths of a millisecond). Everything sent
comes from the line the Tracer wrote to traces.jsonl (already through `mask`) and goes through `mask` once more here. The prompts and the
completions are not sent: the internal trace does not keep them either.

The client is passed in (the real one comes from app.observability.factory, tests pass a fake), so this module never imports `langfuse`."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

from app.observability.masking import clean_deep, clean_text
from app.observability.pricing import COST_BASIS, cost_details
from app.tracing import SPAN_ONLY, WARNING_ONLY, redact

ROOT_NAME = "shopmate-turn"
GENERATION_STAGES = frozenset({"router", "extractor", "answer"})  # the calls to Gemini (a span with a model); retrieval's model is an embedding
OBSERVATION_TYPES = {"retrieval": "retriever", "order_lookup": "tool", "ticket_confirm": "tool"}
WITHHELD_INTENTS = frozenset({"order_status", "create_ticket"})
WITHHELD_STAGES = frozenset({"order_lookup", "ticket_confirm"})
MS = 1_000_000  # nanoseconds in a millisecond


class _Child:
    def __init__(self, observation: Any, start_ns: int) -> None:
        self.observation, self.start_ns, self.index = observation, start_ns, None


class _Request:
    def __init__(self, root: Any, start_ns: int, attributes: dict[str, Any]) -> None:
        self.root, self.start_ns, self.attributes = root, start_ns, attributes  # the attributes are propagated again to every child
        self.children: list[_Child] = []


class LangfuseSink:
    def __init__(self, client: Any, *, propagate_attributes: Callable[..., Any], secrets: Sequence[str] = (),
                 now_ns: Callable[[], int] = time.time_ns) -> None:
        self.client, self._propagate, self.secrets, self._now_ns = client, propagate_attributes, tuple(secrets), now_ns

    # --- the four hooks -------------------------------------------------------------------------------------------------------------

    def request_started(self, *, trace_id: str, session_id: str | None, turn: int | None) -> _Request:
        attributes: dict[str, Any] = {"trace_name": ROOT_NAME, "metadata": {**({"turn": str(turn)} if turn is not None else {}), "trace_id": trace_id}}
        if session_id is not None:
            attributes = {"session_id": redact(session_id, self.secrets), **attributes}
        with self._propagate(**attributes):
            root = self.client.start_observation(trace_context={"trace_id": self.client.create_trace_id(seed=trace_id)}, name=ROOT_NAME)
        return _Request(root, self._now_ns(), attributes)

    def span_started(self, request: _Request, *, stage: str, model: str | None) -> _Child:
        kind = "generation" if stage in GENERATION_STAGES and model else OBSERVATION_TYPES.get(stage, "span")
        with self._propagate(**request.attributes):
            observation = request.root.start_observation(name=stage, as_type=kind)
        child = _Child(observation, self._now_ns())
        request.children.append(child)
        return child

    def span_ended(self, request: _Request, span: _Child, *, index: int) -> None:
        span.index = index

    def request_ended(self, request: _Request, line: dict | None, *, duration_ms: float) -> None:
        """Send the data of the finished line (None: it could not be built, so nothing is sent but everything is closed) and end the observations."""
        problems: list[Exception] = []
        withheld = line is not None and self._withheld(line)
        child_ends = []
        for child in request.children:
            entry = self._entry(line, child)
            end_ns = child.start_ns + int(entry["duration_ms"] * MS) if entry else child.start_ns
            if entry:
                self._try(problems, child.observation.update, lambda: self._child_data(entry, line, withheld))
            child_ends.append(end_ns)
            self._try(problems, child.observation.end, lambda: {"end_time": end_ns})
        if line is not None:
            self._try(problems, request.root.update, lambda: self._root_data(line, withheld))
        self._try(problems, request.root.end, lambda: {"end_time": max([request.start_ns + int(duration_ms * MS), *child_ends])})
        if problems:  # everything that could be closed is closed; the first failure is the Tracer's to report
            raise problems[0]

    # --- what is sent ---------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _try(problems: list[Exception], call: Callable[..., Any], arguments: Callable[[], dict[str, Any]]) -> None:
        """One failing call must not leave the other observations open: remember the failure and go on."""
        try:
            call(**arguments())
        except Exception as exc:  # noqa: BLE001
            problems.append(exc)

    @staticmethod
    def _entry(line: dict | None, child: _Child) -> dict | None:
        if line is None or child.index is None or child.index >= len(line["spans"]):
            return None
        return line["spans"][child.index]

    @staticmethod
    def _withheld(line: dict) -> bool:
        return line["intent"] in WITHHELD_INTENTS or any(s["stage"] in WITHHELD_STAGES for s in line["spans"])

    def _text(self, value: Any) -> Any:
        return clean_deep(value, self.secrets)

    def _status_message(self, error: dict | None, withheld: bool) -> str | None:
        if error is None:
            return None
        return error["type"] if withheld else clean_text(f"{error['type']}: {error['message']}", self.secrets)

    @staticmethod
    def _span_level(entry: dict, line: dict) -> str | None:
        error = entry["error"]
        if error is None:
            return None
        failed_the_request = line["status"] == "error" and line["error"] is not None and line["error"]["type"] == error["type"]
        return "WARNING" if error["type"] in WARNING_ONLY | SPAN_ONLY or not failed_the_request else "ERROR"

    def _child_data(self, entry: dict, line: dict, withheld: bool) -> dict[str, Any]:
        metadata = {"start_ms": entry["start_ms"], "duration_ms": entry["duration_ms"], "cache_hit": entry["cache_hit"]}
        data: dict[str, Any] = {"output": self._text(entry["summary"]) or None, "level": self._span_level(entry, line),
                                "status_message": self._status_message(entry["error"], withheld)}
        if entry["model"] and entry["stage"] in GENERATION_STAGES:
            tokens_in, tokens_out, hit = entry["tokens_in"], entry["tokens_out"], entry["cache_hit"]
            data["model"] = redact(entry["model"], self.secrets)
            cost = cost_details(tokens_in, tokens_out, cache_hit=hit)
            if cost is not None:
                data["cost_details"] = cost
                metadata["cost_basis"] = COST_BASIS
                if hit:  # not billed again: the tokens of the cached answer are history, not usage
                    metadata.update(cached_tokens_in=tokens_in, cached_tokens_out=tokens_out)
                else:
                    data["usage_details"] = {"input": tokens_in or 0, "output": tokens_out or 0}
        data["metadata"] = metadata
        return data

    def _root_data(self, line: dict, withheld: bool) -> dict[str, Any]:
        question, reply = line["question"], line["reply"]
        if withheld:
            label = line["intent"] or "order"
            question, reply = f"[withheld: {label}, {len(question)} ký tự]", None if reply is None else f"[withheld: {label}, {len(reply)} ký tự]"
        error = line["error"]
        level = {"error": "ERROR", "warn": "WARNING"}.get(line["status"])
        metadata = {"trace_id": line["trace_id"], "turn": line["turn"], "intent": line["intent"], "status": line["status"], "reply_source": line["reply_source"],
                    "reply_truncated": line["reply_truncated"], "schema_version": line["schema_version"]}
        return {"input": self._text(question), "output": self._text(reply), "metadata": self._text(metadata), "level": level,
                "status_message": self._status_message(error, withheld)}
