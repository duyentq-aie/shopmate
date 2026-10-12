"""The four hook points of the Tracer (docs/DECISIONS-langfuse.md, L2 and L5): Langfuse is a second output of the same trace, opt-in per Tracer,
and it can neither change what traces.jsonl says nor break a request. Real Tracer, real sink, fake Langfuse client."""

from __future__ import annotations

import datetime as dt
import itertools
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from app.observability.sink import LangfuseSink
from app.tracing import Tracer, annotate_span, record_intent, span
from tests.fake_langfuse import FakeClient

REPO = Path(__file__).resolve().parent.parent
FAKE_KEY = "fake-" + "x" * 28
QUESTION = "áo khoác nam xanh rêu, gọi 0901 234 567 hoặc khachhang02@example.com"


def make_tracer(path: Path, sink=None) -> Tracer:
    ticks = itertools.count(0, 5)  # the clock moves 5 ms per reading, so only the differences inside a request matter
    return Tracer(path, secrets=[FAKE_KEY], clock=lambda: next(ticks) / 1000, now=lambda: dt.datetime(2026, 10, 13, 1, 2, 3, tzinfo=dt.timezone.utc),
                  new_id=lambda: "b" * 32, sink=sink)


def make_sink(client: FakeClient) -> LangfuseSink:
    return LangfuseSink(client, propagate_attributes=client.propagate_attributes, secrets=[FAKE_KEY])


def one_request(tracer: Tracer, question: str = QUESTION) -> None:
    with tracer.request(question, session_id="sess-1", turn=1) as trace:
        with span("router", model="gemini-3.5-flash-lite") as sp:
            sp.set_usage(tokens_in=700, tokens_out=8, cache_hit=False)
            sp.set_summary(intent="product_search")
            record_intent("product_search")
        with span("extractor", model="gemini-3.5-flash-lite") as sp:
            sp.set_summary(filters={"kind": "Áo khoác nam"})
        with span("retrieval", model="bge-m3") as sp:
            sp.set_summary(mode="dense_folded", k=5, hits=[])
        annotate_span("extractor", relaxed=["color"])  # a later step learns more about a step that is over
        with span("reply") as sp:
            sp.set_summary(template="found")
        trace.set_reply("Bên mình có Áo khoác dù nam xanh rêu.", "llm")


# --- the second output changes nothing about the first ---------------------------------------------------------------------------------------


def test_traces_jsonl_is_byte_for_byte_the_same_with_and_without_a_sink(tmp_path):
    without, with_sink = tmp_path / "a" / "traces.jsonl", tmp_path / "b" / "traces.jsonl"
    one_request(make_tracer(without))
    one_request(make_tracer(with_sink, make_sink(FakeClient())))
    assert with_sink.read_bytes() == without.read_bytes() and without.stat().st_size > 0


def test_a_tracer_has_no_sink_unless_it_is_given_one():
    assert Tracer().sink is None


def test_importing_the_tracer_never_imports_langfuse():
    code = "import sys, app.tracing; assert not any(m == 'langfuse' or m.startswith('langfuse.') for m in sys.modules), 'langfuse was imported'"
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


# --- what the sink gets -----------------------------------------------------------------------------------------------------------------------


def test_the_observations_match_the_spans_of_the_internal_trace_one_to_one(tmp_path):
    client = FakeClient()
    tracer = make_tracer(tmp_path / "traces.jsonl", make_sink(client))
    one_request(tracer)
    (line,) = [json.loads(x) for x in tracer.path.read_text(encoding="utf-8").splitlines()]
    assert [c.name for c in client.children] == [s["stage"] for s in line["spans"]] == ["router", "extractor", "retrieval", "reply"]
    assert [c.as_type for c in client.children] == ["generation", "generation", "retriever", "span"]
    assert client.root.kwargs["trace_context"]["trace_id"] == FakeClient.create_trace_id(seed=line["trace_id"])
    assert client.root.propagated[0]["session_id"] == "sess-1" and all(o.ended for o in client.observations)


def test_what_the_sink_sends_is_the_masked_line_not_what_the_customer_typed(tmp_path):
    client = FakeClient()
    one_request(make_tracer(tmp_path / "traces.jsonl", make_sink(client)))
    assert client.root.merged["input"] == "áo khoác nam xanh rêu, gọi [sđt] hoặc [email]"
    assert not any("0901" in s or "khachhang02" in s for o in client.observations for s in o.every_string())


def test_a_summary_added_after_its_span_ended_reaches_langfuse(tmp_path):
    client = FakeClient()
    one_request(make_tracer(tmp_path / "traces.jsonl", make_sink(client)))
    extractor = client.children[1].merged
    assert extractor["output"] == {"filters": {"kind": "Áo khoác nam"}, "relaxed": ["color"]}


def test_a_request_that_fails_is_sent_as_an_error_and_the_exception_still_propagates(tmp_path):
    client = FakeClient()
    tracer = make_tracer(tmp_path / "traces.jsonl", make_sink(client))
    with pytest.raises(ZeroDivisionError):
        with tracer.request("câu hỏi") as trace:
            with span("retrieval", model="bge-m3"):
                1 / 0
    assert client.root.merged["level"] == "ERROR" and client.children[0].merged["level"] == "ERROR" and all(o.ended for o in client.observations)


def test_a_keyboard_interrupt_ends_the_observations_too(tmp_path):
    client = FakeClient()
    tracer = make_tracer(tmp_path / "traces.jsonl", make_sink(client))
    with pytest.raises(KeyboardInterrupt):
        with tracer.request("câu hỏi"):
            with span("router", model="m"):
                raise KeyboardInterrupt
    assert all(o.ended for o in client.observations) and client.root.merged["status_message"].startswith("request_interrupted")


# --- Langfuse failing -------------------------------------------------------------------------------------------------------------------------


class ExplodingSink:
    """Fails at every hook with the customer's text in the message, to prove the log says no more than the class of the failure."""

    def __getattr__(self, hook):
        def explode(*args, **kwargs):
            raise RuntimeError(f"{hook} failed for 0901234567 {kwargs or args}")

        return explode


def test_a_sink_that_fails_at_every_hook_changes_nothing_about_the_request_or_its_trace_line(tmp_path, caplog):
    healthy, broken = tmp_path / "a" / "traces.jsonl", tmp_path / "b" / "traces.jsonl"
    one_request(make_tracer(healthy))
    with caplog.at_level(logging.WARNING, logger="shopmate.observability"):
        one_request(make_tracer(broken, ExplodingSink()))
    assert broken.read_bytes() == healthy.read_bytes()
    assert "RuntimeError" in caplog.text and "0901234567" not in caplog.text and "áo khoác" not in caplog.text
    assert {r.levelno for r in caplog.records} == {logging.WARNING}


@pytest.mark.parametrize("failing", [{"start_observation"}, {"update"}, {"end"}, {"create_trace_id"}])
def test_a_client_that_fails_never_reaches_the_request_or_the_trace_file(tmp_path, failing):
    healthy, broken = tmp_path / "a" / "traces.jsonl", tmp_path / "b" / "traces.jsonl"
    one_request(make_tracer(healthy))
    one_request(make_tracer(broken, make_sink(FakeClient(fail=failing))))
    assert broken.read_bytes() == healthy.read_bytes()


def test_a_request_whose_line_cannot_be_built_still_closes_its_observations_and_sends_no_text(tmp_path, capsys):
    client = FakeClient()
    tracer = make_tracer(tmp_path / "traces.jsonl", make_sink(client))
    tracer.line = lambda trace: (_ for _ in ()).throw(ValueError("cannot build"))
    one_request(tracer)
    assert "trace write failed" in capsys.readouterr().err and not tracer.path.exists()
    assert all(o.ended for o in client.observations) and all("input" not in o.merged for o in client.observations)
