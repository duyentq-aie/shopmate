"""The dashboard page (dashboard/index.html), checked without a browser: its structure as text, and its pure data code under Node.

Phase 7 redesign (docs/DECISIONS-phase7.md D7.10-D7.13): four tabs, a journey per request, a sample that carries the v2 fields. Trace data is the
customer's own text, so the page must create nodes and never parse HTML. The browser behaviour (keyboard, replay, tabs, filters) was driven
in a real Chromium during Phase 7; it is not part of this suite. The Node tests are skipped where Node is not installed."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.agent import guardrails, templates
from app.llm.intents import INTENTS
from app.tracing import ERROR_TYPES, REPLY_SOURCES, STAGES, Tracer, validate_trace_line

REPO = Path(__file__).resolve().parent.parent
PAGE = REPO / "dashboard" / "index.html"
HARNESS = Path(__file__).resolve().parent / "dashboard_harness.js"
HTML = PAGE.read_text(encoding="utf-8")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node is not installed")
NOW = "Date.parse('2026-10-11T08:00:00Z')"


def js(expression: str):
    result = subprocess.run([NODE, str(HARNESS), str(PAGE), expression], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _script() -> str:
    return re.search(r"<script>(.*)</script>", HTML, re.S).group(1)


# --- structure ---------------------------------------------------------------------------------------------------------------


def test_the_page_has_exactly_the_four_tabs_with_flow_first_and_selected():
    tabs = re.findall(r'<button class="tab" role="tab" id="tab-(\w+)" data-tab="\w+" aria-controls="panel-\w+" aria-selected="(\w+)"[^>]*>([^<]+)', HTML)
    assert [(name, label) for name, _, label in tabs] == [("flow", "Luồng xử lý"), ("overview", "Tổng quan"), ("errors", "Lỗi"), ("eval", "Đánh giá")]
    assert [selected for _, selected, _ in tabs] == ["true", "false", "false", "false"]
    assert 'role="tablist"' in HTML
    for name, *_ in tabs:
        assert f'id="panel-{name}" role="tabpanel" aria-labelledby="tab-{name}"' in HTML


def test_the_flow_tab_has_the_list_the_filters_the_journey_and_the_replay_button():
    for needle in ('id="rows" role="listbox"', 'id="fQuery"', 'id="fStatus"', 'id="fIntent"', 'id="journey"', "'Phát lại'"):
        assert needle in HTML, needle
    for status in ('value="ok"', 'value="warn"', 'value="fallback"', 'value="error"'):
        assert status in HTML


def test_the_stage_names_of_the_journey_are_the_agreed_vietnamese_ones():
    for label in ("Phân loại ý định", "Trích bộ lọc", "Tìm kiếm", "Tra cứu đơn", "Xác nhận tạo ticket", "Viết câu trả lời", "Kiểm tra guardrail", "Khách hỏi", "ShopMate trả lời"):
        assert label in HTML, label
    assert "Câu trả lời dự phòng của guardrail" in HTML and "Trace cũ (v1) — không có nội dung câu trả lời" in HTML


def test_live_mode_polls_every_5_seconds_and_the_three_banners_exist():
    assert re.search(r"fetch\('api/traces\?limit=5000'", HTML) and "setInterval(pollLive, 5000)" in HTML
    for banner in ("TRỰC TIẾP.", "MẤT KẾT NỐI.", "DỮ LIỆU MẪU."):
        assert banner in HTML, banner


def test_no_trace_data_is_ever_parsed_as_html():
    script = _script()
    for forbidden in (r"\.innerHTML", r"\.outerHTML", r"insertAdjacentHTML", r"document\.write", r"createContextualFragment", r"DOMParser"):
        assert not re.search(forbidden, script), f"the page uses {forbidden}"
    assert not re.search(r"innerHTML\s*=", HTML)  # not even for static text: nothing to audit
    assert "createTextNode" in script and "replaceChildren" in script  # text goes in as text nodes


def test_local_storage_is_only_touched_inside_try_catch():
    script = _script()
    assert script.count("localStorage") == 2
    for match in re.finditer(r"localStorage", script):
        before = script[max(0, match.start() - 60):match.start()]
        assert "try {" in before, before


def test_prefers_reduced_motion_is_respected_by_the_replay():
    script = _script()
    assert "prefers-reduced-motion: reduce" in script and "if (reduceMotion())" in script
    assert "@media (prefers-reduced-motion:no-preference)" in HTML  # transitions only where motion is welcome


def test_every_interactive_element_has_a_focus_ring_and_the_list_has_arrow_key_navigation():
    assert ":focus-visible{outline:2px solid var(--accent)" in HTML
    assert "'ArrowDown'" in HTML and "'ArrowUp'" in HTML and 'role: \'option\'' in HTML.replace('role: "option"', "role: 'option'")


def test_the_page_names_the_mobile_breakpoint_of_the_brief():
    assert "@media (max-width:759px)" in HTML  # < 760px: list and journey are stacked
    assert "scrollIntoView" in HTML


# --- the page knows what the code can write ------------------------------------------------------------------------------------


def _object_keys(name: str) -> set[str]:
    block = re.search(rf"const {name} = \{{(.*?)\n\}};", HTML, re.S).group(1)
    return set(re.findall(r"^\s+([a-z_0-9]+):\s", block, re.M))


def test_the_page_knows_every_stage_and_every_reply_source_of_the_tracer():
    stage_block = re.search(r"const STAGE = \{(.*?)\n\};", HTML, re.S).group(1)
    assert set(re.findall(r"^\s+([a-z_]+):\s+\{label:", stage_block, re.M)) == set(STAGES)
    source_block = re.search(r"const SOURCE = \{(.*?)\n\};", HTML, re.S).group(1)
    assert set(re.findall(r"^\s+([a-z_]+):\s+'", source_block, re.M)) == set(REPLY_SOURCES)


def test_the_page_knows_every_intent_and_every_fixed_reply_and_every_guardrail_rule():
    intent_block = re.search(r"const INTENT = \{(.*?)\};", HTML, re.S).group(1)
    assert set(re.findall(r"([a-z_]+):\s*'", intent_block)) == set(INTENTS) | {"unknown"}
    template_block = re.search(r"const TEMPLATE = \{(.*?)\n\};", HTML, re.S).group(1)
    known = set(re.findall(r"\b([a-z_]+):\s*'", template_block))
    assert set(templates._FIXED) | {"ticket_created", "no_results_relaxed", "offer_invite"} <= known
    rule_block = re.search(r"const RULE = \{(.*?)\n\};", HTML, re.S).group(1)
    rules = set(re.findall(r"\b([a-z_]+):\s*'", rule_block))
    written = set(re.findall(r'"([a-z_]+): ', Path(guardrails.__file__).read_text(encoding="utf-8"))) | set(re.findall(r'f"([a-z_]+): ', Path(guardrails.__file__).read_text(encoding="utf-8")))
    assert written and written <= rules, sorted(written - rules)


def test_the_journey_can_show_the_counts_of_an_offer():
    for text in ("Đếm để gợi ý hàng gần đúng", "Ràng buộc chặn:", "Theo lời mời khách đã đồng ý", "Tìm kiếm theo lời mời", "sm.mode === 'count'"):
        assert text in HTML, text


def test_every_error_type_has_a_short_vietnamese_explanation_for_the_journey():
    block = re.search(r"const ERR = \{(.*?)\n\};", HTML, re.S).group(1)
    shorts = re.findall(r"^\s+([a-z0-9_]+):\s*\{label: '[^']*', sev: '\w+', short: '([^']+)'", block, re.M)
    assert {name for name, _ in shorts} == set(ERROR_TYPES)
    assert all(len(text) <= 120 for _, text in shorts)  # short means short


# --- the pure code under Node ---------------------------------------------------------------------------------------------------


@needs_node
def test_the_sample_is_deterministic_for_a_given_clock():
    first, second = js(f"api.makeSample({NOW})"), js(f"api.makeSample({NOW})")
    assert first == second and len(first) > 150
    other = js(f"api.makeSample({NOW} + 86400000)")
    assert [r["question"] for r in other][:5] != [r["question"] for r in first][:5] or other != first  # a different clock gives a different day


@needs_node
def test_every_line_of_the_sample_is_a_valid_trace_line_of_the_real_schema():
    sample = js(f"api.makeSample({NOW})")
    problems = {r["trace_id"]: validate_trace_line(r) for r in sample if validate_trace_line(r)}
    assert problems == {}, list(problems.items())[:3]
    assert len({r["trace_id"] for r in sample}) == len(sample)


@needs_node
def test_the_sample_tells_every_story_the_dashboard_has_to_draw():
    sample = js(f"api.makeSample({NOW})")
    v2 = [r for r in sample if "schema_version" in r]
    assert set(REPLY_SOURCES) <= {r["reply_source"] for r in v2}  # every way a reply can be made is in the sample
    sessions: dict[str, list] = {}
    for r in v2:
        sessions.setdefault(r["session_id"], []).append(r["turn"])
    assert any(len(turns) >= 3 for turns in sessions.values()) and any(len(turns) == 2 for turns in sessions.values())
    assert all(sorted(turns) == list(range(1, len(turns) + 1)) for turns in sessions.values())  # turns 1, 2, 3 without holes
    assert any(r["reply_source"] == "guardrail_fallback" and any(s["stage"] == "answer" and s["summary"]["guardrail"]["fallback_used"] for s in r["spans"]) for r in v2)
    assert any(r["status"] == "error" and r["intent"] is not None and r["spans"] and r["spans"][-1]["error"] for r in v2)  # an error half way through
    assert any(r["status"] == "error" and r["reply"] and r["reply_source"] == "error_template" for r in v2)
    assert any(s["stage"] == "extractor" and s["summary"].get("relaxed") for r in v2 for s in r["spans"])  # a relaxed search
    assert any(r["reply_truncated"] and len(r["reply"]) == 800 for r in v2)
    assert any("schema_version" not in r and all("summary" not in s for s in r["spans"]) for r in sample)  # old v1 lines are mixed in
    assert all(json.dumps([s["summary"] for s in r["spans"]], ensure_ascii=False).encode().__len__() <= 4096 for r in v2)
    assert any(r["session_id"] and r["turn"] == 3 and r["intent"] == "create_ticket" and r["spans"][0]["summary"].get("state") == "written" for r in v2)
    assert any(s["stage"] == "order_lookup" and s["summary"].get("attempts") == 2 for r in v2 for s in r["spans"])
    # the offer after an empty search (Phase 7.5): the counts, the question, and both answers to it
    counts = [(r, s) for r in v2 for s in r["spans"] if s["stage"] == "retrieval" and s["summary"].get("mode") == "count"]
    assert counts and all(s["summary"]["blocking_constraint"] in ("size", "color", "material", "price") and s["summary"]["offer_count"] > 0 and s["summary"]["hits"] == [] for _, s in counts)
    assert all(r["reply_source"] == "template" and "Hiện có" in r["reply"] and r["spans"][-1]["summary"] == {"template": "offer_invite"} and "SM-" not in r["reply"] for r, _ in counts)
    follow = [x for r, _ in counts for x in v2 if x["session_id"] == r["session_id"] and x["turn"] == 2]
    assert any(x["reply"].startswith("Không khớp hoàn toàn: khác") and x["spans"][0]["summary"].get("offer_drops") for x in follow)  # accepted: the label and the search that follows the offer
    assert any(x["question"] in ("không", "thôi khỏi", "không cần đâu") and x["intent"] == "product_search" and [s["stage"] for s in x["spans"]] == ["reply"]
               and x["spans"][0]["summary"] == {"template": "offer_declined"} and x["reply"].startswith("Dạ, mình giữ nguyên") for x in follow)  # declined: a fixed reply, no router
    single = [s for _, s in counts if len(s["summary"]["offer_drops"]) == 1]
    together = [s for _, s in counts if len(s["summary"]["offer_drops"]) > 1]
    assert single and together  # both kinds of offer are in the sample: one constraint alone, and several together
    assert all([p["dropped"] for p in s["summary"]["probes"]][-1] == s["summary"]["offer_drops"] for _, s in counts)  # the last count made is the one that gave the offer


@needs_node
def test_the_sample_contains_no_email_phone_or_secret():
    sample = js(f"api.makeSample({NOW})")
    text = json.dumps([{k: v for k, v in r.items() if k not in ("trace_id", "session_id")} for r in sample], ensure_ascii=False)  # ids are hex, not phone numbers
    assert not re.search(r"[\w.]+@[\w-]+\.\w+", text.replace("l***@example.com", ""))
    assert not re.search(r"(?<![\d@])(?:\+?84|0)(?:[\s.\-]?\d){9}(?!\d)", text)
    assert "AIza" not in text


@needs_node
def test_the_page_reads_a_v1_line_and_a_v2_line_written_by_the_real_tracer(tmp_path):
    tracer = Tracer(tmp_path / "t.jsonl", secrets=[])
    with tracer.request("chao shop", session_id="s-1", turn=2) as trace:
        trace.set_reply("Chào bạn!", "template")
    v2_line = (tmp_path / "t.jsonl").read_text(encoding="utf-8").strip()
    v1_line = json.dumps({"trace_id": "b" * 32, "ts": "2026-10-10T12:30:45.123Z", "question": "cu", "intent": "smalltalk", "status": "ok",
                          "spans": [{"stage": "router", "start_ms": 0, "duration_ms": 5, "model": None, "tokens_in": None, "tokens_out": None,
                                     "cache_hit": None, "error": None}], "error": None})
    text = json.dumps(v1_line + "\n" + v2_line + "\nnot json at all\n")
    parsed = js(f"(function(){{ const r = api.parseFile({text}); return {{bad: r.bad, traces: r.traces.map(t => ({{v: t.version, reply: t.reply, src: t.reply_source, sid: t.session_id, turn: t.turn, q: t.question, n: t.spans.length, summary: t.spans.map(s => s.summary)}}))}}; }})()")
    assert parsed["bad"] == 1 and len(parsed["traces"]) == 2
    old, new = sorted(parsed["traces"], key=lambda t: t["v"])
    assert (old["v"], old["reply"], old["src"], old["sid"], old["turn"], old["summary"]) == (1, None, None, None, None, [None])
    assert (new["v"], new["reply"], new["src"], new["sid"], new["turn"], new["q"]) == (2, "Chào bạn!", "template", "s-1", 2, "chao shop")


@needs_node
def test_a_hostile_trace_line_cannot_break_the_reader():
    hostile = {"trace_id": "c" * 32, "ts": "2026-10-10T12:30:45.123Z", "question": "<img src=x onerror=alert(1)>", "intent": "<b>x</b>", "status": "ok",
               "spans": [{"stage": "retrieval", "summary": {"hits": "not a list"}}, "nope", {"stage": 5}], "error": None, "reply": 12}
    parsed = js(f"(function(){{ const t = api.parseFile({json.dumps(json.dumps(hostile))}).traces[0]; return {{q: t.question, intent: t.intent, outcome: api.outcomeOf(t), n: t.spans.length, reply: t.reply, i: api.intentOf(t)}}; }})()")
    assert parsed == {"q": "<img src=x onerror=alert(1)>", "intent": "<b>x</b>", "outcome": "ok", "n": 1, "reply": None, "i": "unknown"}


@needs_node
def test_outcome_and_the_flow_graph_count_every_request_once():
    graph = js(f"(function(){{ const t = api.makeSample({NOW}).map(api.normalize); return {{g: api.flowGraph(t), n: t.length, outcomes: t.map(api.outcomeOf)}}; }})()")
    g = graph["g"]
    assert g["total"] == graph["n"] == sum(g["byOutcome"].values()) == sum(v["total"] for v in g["byIntent"].values())
    for intent in g["byIntent"].values():
        assert intent["total"] == intent["ok"] + intent["warn"] + intent["fallback"] + intent["error"]
    assert set(graph["outcomes"]) == {"ok", "warn", "fallback", "error"}


@needs_node
def test_a_guardrail_fallback_is_its_own_outcome_and_an_error_beats_it():
    cases = {"fb": ("warn", "guardrail_fallback", None), "warn": ("warn", "llm", None), "err": ("error", "guardrail_fallback", {"type": "qdrant_unreachable", "message": "x"}), "ok": ("ok", "llm", None)}
    out = js("(function(){ const base = {trace_id: 'a'.repeat(32), ts: '2026-10-10T12:30:45.123Z', question: 'q', intent: 'product_search', spans: [], error: null}; "
             f"const cases = {json.dumps(cases)}; const r = {{}}; for (const k of Object.keys(cases)) {{ const [status, src, error] = cases[k]; r[k] = api.outcomeOf(api.normalize(Object.assign({{}}, base, {{schema_version: 2, status, reply_source: src, error}}))); }} return r; }})()")
    assert out == {"fb": "fallback", "warn": "warn", "err": "error", "ok": "ok"}


@needs_node
def test_the_replay_schedule_follows_the_real_durations_inside_the_target_time():
    plan = js("api.replaySchedule([100, 900, 0, 1000], 3000)")
    assert plan["starts"] == sorted(plan["starts"]) and plan["starts"][0] == 0 and abs(plan["end"] - 3000) <= 2
    assert plan["starts"][2] - plan["starts"][1] > 5 * (plan["starts"][1] - plan["starts"][0])  # 900 ms of real time takes 9 times as long as 100 ms
    assert js("api.replaySchedule([1, 1, 1], 300)")["starts"] == [0, 100, 200]
