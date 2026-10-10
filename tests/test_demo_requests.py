"""scripts/demo_requests.py: ten hand-written questions through app.pipeline.handle_question, traced.

Everything here runs with the REAL GeminiClient over a fake HTTP transport (no network: a guard fails any test that
tries to reach a host other than loopback), the real limiter class (spied, so nothing sleeps), a real DailyBudget in a
temp dir, the real disk cache, the real router/extractor/pipeline/tracer and an in-memory Qdrant with the fake
embedder. Nothing here, and nothing the author of this script did, calls the live API.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import re
from pathlib import Path

import httpx
import pytest

from app.llm.client import GeminiClient
from app.llm.intents import INTENTS
from app.llm.limits import DailyBudget, RateLimiter
from app.retrieval.folding import fold, has_diacritics
from app.retrieval.search import Retriever
from app.tracing import TRACE_PATH, Tracer, validate_trace_line
from eval.golden.loader import load_split
from eval.probes.loader import load_probes
from eval.run_phase5_eval import DAILY_CEILING, RPM, TPM, USAGE_DIR
from scripts import demo_requests as demo
from scripts.demo_requests import QUESTIONS, Refused, assemble, build_llm, main, parse_args, validate_new_lines
from tests.fake_embedder import FakeEmbedder
from tests.test_llm_client import PER_MINUTE
from tests.test_request_tracing import NULLS, _raw
from tests.test_retrieval_folded import _documents, _index, _new_collection

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")
KEY = "AIzaSyDEMO-FAKE-KEY-0123456789abcdef"
TODAY = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
FILTERS = {  # what the fake model extracts for the product questions
    QUESTIONS[0].text: {"kind": "Áo khoác nam", "color": "Xanh rêu", "size": "L"},
    QUESTIONS[1].text: {"kind": "Áo sơ mi nữ", "color": "Trắng", "material": "Linen"},
    QUESTIONS[2].text: {"kind": "Túi xách nữ", "price_max": 600000},
}


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    """A test that builds a client without a fake transport would reach Google: fail it instead."""
    real = httpx.HTTPTransport.handle_request

    def guard(self, request):
        if request.url.host not in ("127.0.0.1", "localhost"):
            raise AssertionError(f"a test tried to reach {request.url.host}")
        return real(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guard)


class SpyLimiter(RateLimiter):
    def __init__(self, record):
        super().__init__(rpm=RPM, tpm=TPM)
        self.record = record

    def acquire(self, tokens):
        self.record.append(tokens)  # never waits


class Rig:
    """A fake Gemini behind the real client, plus the files a run uses, all under tmp_path."""

    def __init__(self, tmp_path: Path, *, used_today: int = 0, prelude=(), interrupt_on: int | None = None):
        self.interrupt_on = interrupt_on  # the number of the HTTP request during which Ctrl+C is pressed
        self.tmp, self.requests, self.sleeps, self.acquired = tmp_path, [], [], []
        self.prelude = list(prelude)  # answers given first, e.g. [(429, PER_MINUTE)]
        self.usage_dir, self.cache_dir, self.traces = tmp_path / "usage", tmp_path / "cache", tmp_path / "logs" / "traces.jsonl"
        self.budget = DailyBudget(self.usage_dir, limit=DAILY_CEILING, now=lambda: TODAY)
        if used_today:
            self.usage_dir.mkdir(parents=True)
            self.budget._path().write_text(json.dumps({"date": "2026-10-10", "count": used_today, "ceiling": DAILY_CEILING}), encoding="utf-8")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.interrupt_on == len(self.requests):
            raise KeyboardInterrupt
        if self.prelude:
            status, body = self.prelude.pop(0)
            return httpx.Response(status, json=body)
        body = json.loads(request.content)
        question = body["contents"][0]["parts"][0]["text"]
        if "intent" in body["generationConfig"]["responseSchema"]["properties"]:
            intent = next(q.expected_intent for q in QUESTIONS if q.text == question)
            return httpx.Response(200, json=_raw({"intent": intent}))
        return httpx.Response(200, json=_raw({**NULLS, **FILTERS.get(question, {})},
                                             {"promptTokenCount": 1900, "candidatesTokenCount": 40, "totalTokenCount": 1940}))

    def llm(self, live, max_live_calls=40, **kwargs):
        return build_llm(live=live, max_live_calls=max_live_calls, key=KEY, http=httpx.Client(transport=httpx.MockTransport(self.handler)),
                         limiter=SpyLimiter(self.acquired), budget=self.budget, sleep=self.sleeps.append, **kwargs)

    def build(self, args):
        client = _new_collection()
        _index(client, _documents())
        retriever = Retriever(client, FakeEmbedder(), collection="v2")
        return assemble(llm=self.llm(args.live, args.max_live_calls), budget=self.budget, cache_dir=self.cache_dir,
                        retriever=retriever, tracer=Tracer(args.traces))

    def run(self, *argv):
        return main([*argv, "--traces", str(self.traces)], build=self.build)

    def lines(self):
        return [json.loads(x) for x in self.traces.read_text(encoding="utf-8").splitlines()] if self.traces.exists() else []


def _summary(text: str) -> dict:
    """The numbers the summary prints, as ints, and the traces path."""
    return {"live": int(re.search(r"live calls used: (\d+)", text).group(1)),
            "quota": tuple(int(x) for x in re.search(r"quota counter today[^:]*: (\d+) of (\d+)", text).groups()),
            "traces": re.search(r"traces file: (\S+)", text).group(1)}


# --- the questions ----------------------------------------------------------------------------------------------------------


def test_there_are_ten_distinct_questions_with_the_agreed_mix():
    assert len(QUESTIONS) == 10 and len({fold(q.text) for q in QUESTIONS}) == 10
    assert {q.expected_intent for q in QUESTIONS} == set(INTENTS)  # all six intents are exercised
    assert [q.kind for q in QUESTIONS].count("injection") == 1
    assert [q.expected_intent for q in QUESTIONS if q.kind == "injection"] == ["out_of_scope"]
    assert any(has_diacritics(q.text) for q in QUESTIONS) and any(not has_diacritics(q.text) for q in QUESTIONS)
    assert all(has_diacritics(q.text) == (q.style == "diacritics") for q in QUESTIONS)  # the label says what the text is
    assert sum(not has_diacritics(q.text) for q in QUESTIONS) >= 3


def test_the_questions_carry_no_email_secret_or_real_looking_personal_data():
    for q in QUESTIONS:
        assert "@" not in q.text and "http" not in q.text.lower() and not re.search(r"\d{9,}", q.text), q.text
        assert 15 <= len(q.text) <= 220, q.text


def test_no_question_is_taken_from_the_dev_set_or_the_probes():
    """Checked against everything this test may read. The test split is never read (load_split("dev") locks it), so
    for it the guarantee is only that the questions were written by hand and are not near any dev or probe question."""
    known = [i.question for i in load_split("dev")] + [p.question for p in load_probes().items]
    assert len(known) == 71 + 8
    for q in QUESTIONS:
        for other in known:
            a, b = fold(q.text), fold(other)
            assert a != b and a not in b and b not in a, (q.text, other)
            assert difflib.SequenceMatcher(None, a, b).ratio() < 0.75, (q.text, other)


def test_the_script_does_not_touch_the_golden_set():
    source = Path(demo.__file__).read_text(encoding="utf-8")
    assert "eval.golden" not in source and "load_split" not in source


# --- the arguments and the llm ------------------------------------------------------------------------------------------------


def test_defaults_are_offline_with_a_small_live_cap_and_the_standard_trace_file():
    args = parse_args([])
    assert args.live is False and args.max_live_calls == 40 and args.traces == TRACE_PATH
    assert parse_args(["--live", "--max-live-calls", "5"]).max_live_calls == 5


def test_without_live_there_is_no_client_and_the_key_is_never_read(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the API key was read without --live")

    monkeypatch.setattr(demo, "get_setting", forbidden)
    assert build_llm(live=False, max_live_calls=40) is None


def test_live_without_a_key_is_refused(monkeypatch):
    monkeypatch.setattr(demo, "get_setting", lambda name, *a, **k: None)
    with pytest.raises(Refused, match="GEMINI_API_KEY"):
        build_llm(live=True, max_live_calls=40)


def test_the_live_client_uses_the_standard_limiter_ceiling_and_cap_of_the_phase_5_runner(monkeypatch):
    monkeypatch.setattr(demo, "get_setting", lambda name, *a, **k: KEY)
    client = build_llm(live=True, max_live_calls=7, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert isinstance(client, GeminiClient) and client.max_live_requests == 7
    assert (client.limiter.rpm, client.limiter.tpm) == (RPM, TPM) == (12, 200_000)
    assert client.budget.limit == DAILY_CEILING == 450 and client.budget.directory == USAGE_DIR


def test_the_demo_cache_is_apart_from_the_committed_evaluation_cache_and_git_ignored():
    from eval.run_phase5_eval import CACHE_DIR

    assert demo.DEMO_CACHE_DIR != CACHE_DIR and not demo.DEMO_CACHE_DIR.is_relative_to(CACHE_DIR)
    assert demo.DEMO_CACHE_DIR.is_relative_to(TRACE_PATH.parent) and TRACE_PATH.parent.name == "logs"  # under logs/, which .gitignore lists
    assert "logs/" in (demo.REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


# --- a run ----------------------------------------------------------------------------------------------------------------------


def test_a_live_run_sends_each_question_through_the_pipeline_and_writes_one_valid_trace_line_each(tmp_path, capsys):
    rig = Rig(tmp_path, used_today=100)
    assert rig.run("--live") == 0
    shown = capsys.readouterr()
    lines = rig.lines()
    assert len(lines) == 10 and all(validate_trace_line(l) == [] for l in lines) and len({l["trace_id"] for l in lines}) == 10
    assert [l["question"] for l in lines] == [q.text for q in QUESTIONS]
    assert [l["intent"] for l in lines] == [q.expected_intent for q in QUESTIONS]
    stages = {q.text: [s["stage"] for s in l["spans"]] for q, l in zip(QUESTIONS, lines)}
    for q in QUESTIONS:
        expected = {"product_search": ["router", "extractor", "retrieval"], "policy_question": ["router", "retrieval"]}.get(q.expected_intent, ["router"])
        assert stages[q.text] == expected, q.text
    assert all(l["status"] == "ok" for l in lines) and all(s["cache_hit"] is False for l in lines for s in l["spans"] if s["stage"] != "retrieval")
    # 10 router calls + 3 extractor calls: every one went through the limiter and was counted against the day
    assert len(rig.requests) == 13 and len(rig.acquired) == 13 and rig.budget.used() == 113
    summary = _summary(shown.out)
    assert summary["live"] == 13 and summary["quota"] == (113, 450) and summary["traces"] == str(rig.traces)
    assert "10 new lines" in shown.out and "all valid" in shown.out
    assert KEY not in shown.out and KEY not in shown.err and KEY not in rig.traces.read_text(encoding="utf-8")


def test_each_question_is_shown_with_what_it_was_routed_to_and_what_came_back(tmp_path, capsys):
    rig = Rig(tmp_path)
    rig.run("--live")
    shown = capsys.readouterr().out
    for q in QUESTIONS:
        assert q.text in shown
    assert shown.count("intent=product_search") == 3 and "filters=" in shown and "hits=" in shown
    hit_lines = re.findall(r"hits=(\d+)( · .*)?\n", shown)
    assert len(hit_lines) == 5  # 3 product + 2 policy questions retrieve; the other five intents do not
    for count, keys in hit_lines:  # the number of hits is the number of keys listed (at most the top 3)
        assert int(count) == len([k for k in keys.removeprefix(" · ").split(", ") if k]) and 0 <= int(count) <= 3
    assert any(int(count) >= 1 for count, _ in hit_lines)  # and the small fake index does return some
    assert shown.count("no retrieval for this intent") == 5


def test_without_live_the_cache_is_used_and_nothing_is_sent_or_counted(tmp_path, capsys):
    rig = Rig(tmp_path, used_today=100)
    assert rig.run("--live") == 0
    capsys.readouterr()
    sent, counted = len(rig.requests), rig.budget.used()
    assert rig.run() == 0  # the second run is offline: every answer comes from the cache
    shown = capsys.readouterr().out
    assert len(rig.requests) == sent and rig.budget.used() == counted == 113 and len(rig.acquired) == 13
    assert _summary(shown)["live"] == 0 and _summary(shown)["quota"] == (113, 450)
    lines = rig.lines()
    assert len(lines) == 20 and all(validate_trace_line(l) == [] for l in lines)
    again = lines[10:]
    assert all(s["cache_hit"] is True for l in again for s in l["spans"] if s["stage"] in ("router", "extractor"))
    assert "10 new lines" in shown  # only the lines of THIS run are counted and checked


def test_without_live_a_cache_miss_stops_the_run_at_that_question_with_no_call(tmp_path, capsys, monkeypatch):
    rig = Rig(tmp_path, used_today=7)
    monkeypatch.setattr(demo, "get_setting", lambda *a, **k: (_ for _ in ()).throw(AssertionError("key read")))
    assert rig.run() == 2
    shown = capsys.readouterr()
    assert rig.requests == [] and rig.acquired == [] and rig.budget.used() == 7
    assert "STOPPED" in shown.err and "question 1" in shown.err and "--live" in shown.err
    (line,) = rig.lines()  # the request that missed is traced; the other nine were never started
    assert line["status"] == "error" and line["error"]["type"] == "llm_cache_miss_offline" and validate_trace_line(line) == []
    summary = _summary(shown.out)
    assert summary["live"] == 0 and summary["quota"] == (7, 450) and "1 new line" in shown.out
    assert "questions run: 0 of 10 (stopped at question 1)" in shown.out


def test_a_cache_that_covers_only_part_of_the_questions_stops_at_the_first_missing_one(tmp_path, capsys):
    rig = Rig(tmp_path)
    assert rig.run("--live") == 0
    capsys.readouterr()
    for path in sorted((rig.cache_dir / "router").glob("*.json")):  # lose the cached router answer of question 4
        if QUESTIONS[3].text in path.read_text(encoding="utf-8"):
            path.unlink()
    rig.traces.unlink()
    assert rig.run() == 2
    shown = capsys.readouterr()
    assert "question 4" in shown.err
    lines = rig.lines()
    assert len(lines) == 4 and [l["status"] for l in lines] == ["ok", "ok", "ok", "error"]


def test_the_daily_ceiling_stops_the_run_and_nothing_beyond_it_is_sent(tmp_path, capsys):
    rig = Rig(tmp_path, used_today=449)  # one request left today
    assert rig.run("--live") == 2
    shown = capsys.readouterr()
    assert len(rig.requests) == 1 and rig.budget.used() == 450 and len(rig.acquired) == 1
    assert "STOPPED" in shown.err and "DailyBudgetReached" in shown.err
    last = rig.lines()[-1]
    assert last["error"]["type"] == "budget_cap_reached" and validate_trace_line(last) == []
    assert _summary(shown.out)["quota"] == (450, 450) and _summary(shown.out)["live"] == 1


def test_the_per_run_cap_stops_the_run(tmp_path, capsys):
    rig = Rig(tmp_path)
    assert rig.run("--live", "--max-live-calls", "4") == 2
    shown = capsys.readouterr()
    assert len(rig.requests) == 4 and "CallCapExceeded" in shown.err
    assert rig.lines()[-1]["error"]["type"] == "budget_cap_reached" and _summary(shown.out)["live"] == 4


def test_a_retried_per_minute_429_counts_as_a_live_call_and_the_run_goes_on(tmp_path, capsys):
    rig = Rig(tmp_path, prelude=[(429, PER_MINUTE)])
    assert rig.run("--live") == 0
    shown = capsys.readouterr().out
    assert len(rig.requests) == 14 and len(rig.acquired) == 14 and rig.budget.used() == 14 and rig.sleeps  # the retry waited and was counted
    assert _summary(shown)["live"] == 14
    first = rig.lines()[0]
    assert first["status"] == "warn" and first["spans"][0]["error"]["type"] == "llm_429_minute"


def test_ctrl_c_during_a_question_stops_the_run_and_the_interrupted_request_is_traced_as_such(tmp_path, capsys):
    rig = Rig(tmp_path, interrupt_on=5)  # request 5 is the router call of question 3
    assert rig.run("--live") == 2
    shown = capsys.readouterr()
    assert "STOPPED at question 3 of 10: interrupted" in shown.err and "questions run: 2 of 10 (stopped at question 3)" in shown.out
    lines = rig.lines()
    assert len(lines) == 3 and [l["status"] for l in lines] == ["ok", "ok", "error"]
    assert lines[2]["error"]["type"] == "request_interrupted" and lines[2]["spans"][0]["error"]["type"] == "request_interrupted"
    assert all(validate_trace_line(l) == [] for l in lines) and "all valid" in shown.out
    assert _summary(shown.out)["live"] == 5


def test_an_api_error_that_echoes_the_key_never_reaches_the_screen_or_the_file(tmp_path, capsys):
    rig = Rig(tmp_path, prelude=[(400, {"error": {"message": f"API key {KEY} not valid", "status": "INVALID_ARGUMENT"}})])
    assert rig.run("--live") == 2
    shown = capsys.readouterr()
    assert KEY not in shown.out and KEY not in shown.err and KEY not in rig.traces.read_text(encoding="utf-8")
    assert rig.lines()[0]["error"]["type"] == "llm_request_error"


def test_live_without_a_key_is_refused_before_anything_is_written(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(demo, "get_setting", lambda name, *a, **k: None)
    traces = tmp_path / "logs" / "traces.jsonl"

    def build(args):
        return assemble(llm=build_llm(live=args.live, max_live_calls=args.max_live_calls), budget=DailyBudget(tmp_path / "u"),
                        cache_dir=tmp_path / "c", retriever=None, tracer=Tracer(args.traces))

    assert main(["--live", "--traces", str(traces)], build=build) == 1
    assert "REFUSED" in capsys.readouterr().err and not traces.exists()


# --- checking the lines it wrote ---------------------------------------------------------------------------------------------


def test_validate_new_lines_checks_only_what_was_appended_after_the_offset(tmp_path):
    rig = Rig(tmp_path)
    rig.run("--live")
    path = rig.traces
    good = path.read_text(encoding="utf-8").splitlines()[0]
    broken = json.loads(good)
    broken.pop("status")
    old = path.stat().st_size
    with open(path, "a", encoding="utf-8") as out:
        out.write("this is not json\n" + json.dumps(broken) + "\n" + good + "\n")
    count, problems = validate_new_lines(path, old)
    assert count == 3 and len(problems) == 2
    assert any("not JSON" in p and "line 1" in p for p in problems) and any("line 2" in p for p in problems)
    assert validate_new_lines(path, 0)[0] == 13  # 10 from the run + 3 appended
    assert validate_new_lines(tmp_path / "missing.jsonl", 0) == (0, [])
    with open(path, "a", encoding="utf-8") as out:
        out.write('{"half": "a line that was cut')  # a writer died in the middle of a line
    count, problems = validate_new_lines(path, old)
    assert count == 4 and any("incomplete" in p and "line 4" in p for p in problems)


def test_an_invalid_line_written_by_the_tracer_fails_the_run_with_its_own_exit_code(tmp_path, capsys):
    rig = Rig(tmp_path)

    class BrokenTracer(Tracer):
        def line(self, trace):
            line = super().line(trace)
            line.pop("status")
            return line

    def build(args):
        stack = rig.build(args)
        stack.tracer = BrokenTracer(args.traces)
        return stack

    assert main(["--live", "--traces", str(rig.traces)], build=build) == 3
    shown = capsys.readouterr()
    assert "INVALID" in shown.out + shown.err and "status" in shown.out + shown.err


def test_lines_already_in_the_file_are_neither_counted_nor_judged(tmp_path, capsys):
    rig = Rig(tmp_path)
    rig.traces.parent.mkdir(parents=True)
    rig.traces.write_text("an old garbage line\n", encoding="utf-8")
    assert rig.run("--live") == 0
    assert "10 new lines" in capsys.readouterr().out and len(rig.traces.read_text(encoding="utf-8").splitlines()) == 11


def test_the_default_target_is_the_standard_trace_file():
    assert parse_args([]).traces == TRACE_PATH and TRACE_PATH.name == "traces.jsonl" and TRACE_PATH.parent.name == "logs"


# --- the real stack (needs Qdrant and the cached bge-m3 model) ---------------------------------------------------------------------


@pytest.mark.integration
def test_the_ten_questions_run_through_the_real_index_and_model_with_a_scripted_llm(tmp_path, embedder, capsys):
    from app.retrieval.manifest import load_manifest
    from app.retrieval.store import connect

    rig = Rig(tmp_path)
    manifest = load_manifest()

    def build(args):
        retriever = Retriever(connect(), embedder, collection=manifest["collection"])
        return assemble(llm=rig.llm(args.live, args.max_live_calls), budget=rig.budget, cache_dir=rig.cache_dir, retriever=retriever,
                        tracer=Tracer(args.traces))

    assert main(["--live", "--traces", str(rig.traces)], build=build) == 0
    lines = rig.lines()
    assert len(lines) == 10 and all(validate_trace_line(l) == [] for l in lines)
    retrieval_models = {s["model"] for l in lines for s in l["spans"] if s["stage"] == "retrieval"}
    assert retrieval_models == {"BAAI/bge-m3"}
    assert "hits=" in capsys.readouterr().out
