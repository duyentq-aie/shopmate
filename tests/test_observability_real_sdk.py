"""The sink against the REAL langfuse SDK (4.17.0, pinned): the same flow as the fake-client tests, read back from an in-memory OpenTelemetry exporter
instead of the network. This is what keeps tests/fake_langfuse.py honest: if the SDK changes a name or an argument, this file fails and the fake is wrong.
Nothing leaves the process: the SDK is given a span exporter of our own, and CI also blocks public addresses (SHOPMATE_BLOCK_NETWORK)."""

from __future__ import annotations

import datetime as dt
import itertools
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("langfuse")
from langfuse import Langfuse, propagate_attributes  # noqa: E402
from langfuse._client.resource_manager import LangfuseResourceManager  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from app.observability.factory import build_sink  # noqa: E402
from app.tracing import Tracer, record_intent, span  # noqa: E402

SECRET = "test-secret-" + "z" * 12
MODEL = "gemini-3.5-flash-lite"


@pytest.fixture
def langfuse_env():
    """(sink built by the real factory around the real SDK, the exporter that catches what the SDK would have sent)."""
    LangfuseResourceManager.reset()  # the SDK keeps one client per public key for the whole process
    exporter = InMemorySpanExporter()
    env = {"LANGFUSE_PUBLIC_KEY": f"test-public-{uuid.uuid4().hex[:10]}", "LANGFUSE_SECRET_KEY": SECRET, "LANGFUSE_HOST": "http://127.0.0.1:9"}

    def sdk():
        return SimpleNamespace(Langfuse=lambda **kw: Langfuse(**kw, span_exporter=exporter), propagate_attributes=propagate_attributes)

    sink = build_sink(env, load_sdk=sdk)
    assert sink is not None
    yield sink, exporter
    LangfuseResourceManager.reset()


def tracer_for(tmp_path: Path, sink) -> Tracer:
    return Tracer(tmp_path / "traces.jsonl", secrets=[SECRET], sink=sink)


def exported(sink, exporter) -> dict:
    sink.client.flush()
    spans = exporter.get_finished_spans()
    return {s.name: s for s in spans}


def attrs(otel_span) -> dict:
    return dict(otel_span.attributes)


def product_search(tracer: Tracer, question: str = "áo khoác nam xanh rêu") -> None:
    with tracer.request(question, session_id="sess-1", turn=3) as trace:
        with span("router", model=MODEL) as sp:
            sp.set_usage(tokens_in=700, tokens_out=8, cache_hit=False)
            sp.set_summary(intent="product_search")
            record_intent("product_search")
            time.sleep(0.02)
        with span("retrieval", model="bge-m3") as sp:
            sp.set_summary(mode="dense_folded", k=5, hits=[])
        with span("answer", model=MODEL) as sp:
            sp.set_usage(tokens_in=1200, tokens_out=90, cache_hit=False)
        trace.set_reply("Bên mình có Áo khoác dù nam xanh rêu.", "llm")


def test_the_real_sdk_receives_one_root_and_one_child_per_span_in_one_trace(tmp_path, langfuse_env):
    sink, exporter = langfuse_env
    product_search(tracer_for(tmp_path, sink))
    spans = exported(sink, exporter)
    assert set(spans) == {"shopmate-turn", "router", "retrieval", "answer"}
    root = spans["shopmate-turn"]
    (line,) = [json.loads(x) for x in (tmp_path / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len({s.context.trace_id for s in spans.values()}) == 1
    assert format(root.context.trace_id, "032x") == Langfuse.create_trace_id(seed=line["trace_id"])  # the dashboard and Langfuse can be cross-referenced
    assert all(s.parent is not None and s.parent.span_id == root.context.span_id for n, s in spans.items() if n != "shopmate-turn")
    assert attrs(root)["langfuse.internal.as_root"] is True


def test_a_gemini_call_is_a_generation_with_model_usage_and_cost(tmp_path, langfuse_env):
    sink, exporter = langfuse_env
    product_search(tracer_for(tmp_path, sink))
    router = attrs(exported(sink, exporter)["router"])
    assert router["langfuse.observation.type"] == "generation" and router["langfuse.observation.model.name"] == MODEL
    assert json.loads(router["langfuse.observation.usage_details"]) == {"input": 700, "output": 8}
    cost = json.loads(router["langfuse.observation.cost_details"])
    assert cost["total"] == pytest.approx(700 * 0.30 / 1e6 + 8 * 2.50 / 1e6) and cost["input"] > 0 and cost["output"] > 0
    assert attrs(exported(sink, exporter)["retrieval"])["langfuse.observation.type"] == "retriever"


def test_the_session_and_the_turn_are_on_every_observation(tmp_path, langfuse_env):
    sink, exporter = langfuse_env
    product_search(tracer_for(tmp_path, sink))
    for name, otel_span in exported(sink, exporter).items():
        a = attrs(otel_span)
        assert a["session.id"] == "sess-1" and a["langfuse.trace.name"] == "shopmate-turn" and a["langfuse.trace.metadata.turn"] == "3", name


def test_the_measured_durations_are_the_ones_the_observations_have_and_the_root_contains_its_children(tmp_path, langfuse_env):
    sink, exporter = langfuse_env
    product_search(tracer_for(tmp_path, sink))
    spans = exported(sink, exporter)
    (line,) = [json.loads(x) for x in (tmp_path / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
    measured = {s["stage"]: s["duration_ms"] for s in line["spans"]}
    for stage, ms in measured.items():
        observed = (spans[stage].end_time - spans[stage].start_time) / 1e6
        # never shorter than measured (an end can not come before its start), and longer only by what the SDK takes to open the observation
        # (a few tenths of a millisecond; the bound is generous so that a busy machine does not make this flaky)
        assert ms <= observed <= ms + 10, stage
    assert measured["router"] >= 20
    root = spans["shopmate-turn"]
    assert all(root.start_time <= s.start_time and s.end_time <= root.end_time for n, s in spans.items() if n != "shopmate-turn")


def test_a_failed_request_reaches_langfuse_as_an_error_level(tmp_path, langfuse_env):
    sink, exporter = langfuse_env
    tracer = tracer_for(tmp_path, sink)
    with pytest.raises(ZeroDivisionError):
        with tracer.request("câu hỏi"):
            with span("retrieval", model="bge-m3"):
                1 / 0
    spans = exported(sink, exporter)
    for name in ("shopmate-turn", "retrieval"):
        assert attrs(spans[name])["langfuse.observation.level"] == "ERROR", name
        assert "internal_error" in attrs(spans[name])["langfuse.observation.status_message"]


# --- privacy, on what the SDK would actually send ---------------------------------------------------------------------------------------------

PII = ["0901 234 567", "0901234567", "khachhang02@example.com", SECRET, "12 Lê Lợi"]


def every_exported_string(spans: dict) -> str:
    """All the text of all the attributes, one leaf per line. The SDK serialises dicts to JSON with escapes ("đ" is \\u0111), so those are decoded first."""
    leaves: list[str] = []

    def walk(value):
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                decoded = None
            if isinstance(decoded, (dict, list)):
                return walk(decoded)
            leaves.append(value)
        elif isinstance(value, dict):
            for k, v in value.items():
                leaves.append(str(k))
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    for s in spans.values():
        leaves.append(s.name)
        walk(dict(s.attributes))
    return "\n".join(leaves)


def test_no_email_phone_or_key_is_in_anything_the_sdk_would_send(tmp_path, langfuse_env):
    sink, exporter = langfuse_env
    tracer = tracer_for(tmp_path, sink)
    product_search(tracer, f"áo khoác, gọi 0901 234 567 hoặc 0901234567, mail khachhang02@example.com, key {SECRET}")
    with pytest.raises(RuntimeError):
        with tracer.request("lỗi"):
            with span("retrieval", model="bge-m3"):
                raise RuntimeError(f"failed for 0901234567 khachhang02@example.com {SECRET}")
    sent = every_exported_string(exported(sink, exporter))
    assert not any(bad in sent for bad in PII[:4])
    assert "[sđt]" in sent and "[email]" in sent and "[secret]" in sent


def test_the_sdk_level_mask_catches_what_is_given_to_the_client_directly(langfuse_env):
    """The second layer: even an observation that did not come through the sink is masked by the SDK, because the factory installs the mask."""
    sink, exporter = langfuse_env
    sink.client.start_observation(name="direct", input="mail khachhang02@example.com", output={"phone": "0901234567"}, metadata={"k": SECRET}).end()
    sent = every_exported_string(exported(sink, exporter))
    assert not any(bad in sent for bad in PII[1:4]) and "[email]" in sent and "[sđt]" in sent


def test_an_order_turn_sends_its_steps_but_none_of_its_text(tmp_path, langfuse_env):
    sink, exporter = langfuse_env
    tracer = tracer_for(tmp_path, sink)
    with tracer.request("đơn 1234 của khachhang02@example.com, giao 12 Lê Lợi", session_id="sess-9", turn=1) as trace:
        with span("router", model=MODEL):
            record_intent("order_status")
        with span("order_lookup") as sp:
            sp.set_summary(verified=True, found=True, attempts=1)
        trace.set_reply("Đơn 1234 đang giao tới 12 Lê Lợi, Quận 1", "template")
    spans = exported(sink, exporter)
    assert set(spans) == {"shopmate-turn", "router", "order_lookup"}
    sent = every_exported_string(spans)
    assert "Lê Lợi" not in sent and "1234" not in sent and "khachhang02" not in sent
    assert attrs(spans["shopmate-turn"])["langfuse.observation.input"].startswith("[withheld: order_status")
    assert json.loads(attrs(spans["order_lookup"])["langfuse.observation.output"]) == {"verified": True, "found": True, "attempts": 1}
    assert attrs(spans["order_lookup"])["langfuse.observation.type"] == "tool"


def test_a_sdk_that_raises_inside_a_request_does_not_change_its_result(tmp_path, langfuse_env, monkeypatch):
    sink, exporter = langfuse_env
    monkeypatch.setattr(sink.client, "start_observation", lambda **kw: (_ for _ in ()).throw(RuntimeError("langfuse is down")))
    tracer = tracer_for(tmp_path, sink)
    product_search(tracer)
    (line,) = [json.loads(x) for x in (tmp_path / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [s["stage"] for s in line["spans"]] == ["router", "retrieval", "answer"] and line["status"] == "ok"
