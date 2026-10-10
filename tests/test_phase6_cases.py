"""The scripted multi-turn cases (eval/phase6/multiturn_cases.json) and the check of a turn (eval/phase6/cases.py). The cases were written before
any evaluation run; these tests keep them well formed, outside the golden set, and covering every bound of docs/AGENT-DESIGN.md."""

from __future__ import annotations

import difflib
import re

import pytest
from pydantic import ValidationError

from app.agent import templates as T
from app.retrieval.folding import fold
from eval.golden.loader import load_split
from eval.phase6.cases import Case, Expect, Turn, check_turn, load_cases, load_orders, resolve
from eval.probes.loader import load_probes
from scripts.demo_requests import QUESTIONS as DEMO_QUESTIONS

ORDERS = load_orders()
CASES = load_cases()
FACTS = dict(reply="", intent=None, awaiting=None, clarify_count=0, relaxed=[], capped=False, tickets_total=0, lookups_total=0, verify_failures=0, tool_calls=0)


def facts(**changes):
    return {**FACTS, **changes}


def messages():
    return [resolve(turn.say, ORDERS) for case in CASES.cases for turn in case.turns]


# --- the file -------------------------------------------------------------------------------------------------------------------------------


def test_there_are_about_a_dozen_cases_with_unique_ids_and_every_turn_says_what_it_expects():
    assert 12 <= len(CASES.cases) <= 14 and [c.id for c in CASES.cases] == [f"mt-{i:02d}" for i in range(1, len(CASES.cases) + 1)]
    assert all(t.expect.model_fields_set for c in CASES.cases for t in c.turns)  # no turn is left unchecked


def test_every_bound_and_every_dangerous_path_is_covered_by_at_least_one_case():
    covered = {check for case in CASES.cases for check in case.checks}
    assert covered >= {"clarify", "clarify_limit", "order_slots_handoff", "verify_answer", "verify_lock", "relax", "ticket_yes", "ticket_no",
                       "ticket_unclear", "general_ticket", "topic_change", "phone_only", "injection"}


def test_the_placeholders_all_resolve_to_a_seed_order_and_nothing_is_left_unresolved():
    for text in messages():
        assert "{" not in text and "}" not in text, text
    assert resolve("{order:005} {email:005}", ORDERS) == f"{ORDERS['005']['number']} {ORDERS['005']['email']}"
    with pytest.raises(KeyError):
        resolve("{order:999}", ORDERS)


def test_no_message_is_a_dev_question_a_probe_a_demo_question_or_close_to_one():
    """Checked against everything this test may read. The test split is never read; for it the guarantee is that the cases were written by hand."""
    known = [i.question.replace("{order_number}", "88") for i in load_split("dev")] + [p.question for p in load_probes().items] + [q.text for q in DEMO_QUESTIONS]
    for message in messages():
        if re.fullmatch(r"[^\s@]+@[^\s@]+", message):
            continue  # an email address alone is a value for a slot, not a question: it is inside any dev question that has the same address
        for other in known:
            a, b = fold(message), fold(other)
            assert a != b and (len(a) < 12 or a not in b) and b not in a, (message, other)  # "có" or "không" is a protocol word, in any sentence
            # the wording is compared, not the email addresses and numbers (the seed customers are shared by design)
            a, b = (re.sub(r"[^\s@]+@[^\s@]+|[0-9]+", "#", x) for x in (a, b))
            assert difflib.SequenceMatcher(None, a, b).ratio() < 0.75, (message, other)


def test_the_cases_contain_no_real_looking_personal_data():
    for text in messages():
        digits = "".join(ch for ch in text if ch.isdigit())
        assert "0912345678" in text or len(digits) < 8, text  # the one phone number is the made-up one of the phone-only case
    emails = {w for t in messages() for w in t.split() if "@" in w}
    assert all(e.rstrip(",.").endswith("@example.com") for e in emails)


# --- the schema -------------------------------------------------------------------------------------------------------------------------------


def test_an_unknown_expectation_or_template_name_is_refused():
    with pytest.raises(ValidationError):
        Expect(no_such_check=1)
    with pytest.raises(ValidationError):
        Expect(reply_is="NO_SUCH_REPLY")
    with pytest.raises(ValidationError):
        Expect(reply_contains=["TEMPLATE:NO_SUCH_REPLY"])
    with pytest.raises(ValidationError):
        Case(id="case-1", title="x", checks=["x"], turns=[Turn(say="a", expect=Expect())])
    assert Expect(reply_is="ASK_EMAIL", reply_contains=["TEMPLATE:NO_FULL_MATCH", "{order:005}"]).reply_is == "ASK_EMAIL"


# --- check_turn ----------------------------------------------------------------------------------------------------------------------------------


def test_a_turn_that_did_what_it_had_to_has_no_problem():
    expect = Expect(intent="order_status", awaiting="email", reply_is="ASK_EMAIL", lookups_total=0, no_tools=True)
    assert check_turn(expect, facts(intent="order_status", awaiting="email", reply=T.ASK_EMAIL), ORDERS) == []


@pytest.mark.parametrize("expect, changes, fragment", [
    (Expect(intent="smalltalk"), dict(intent="order_status"), "intent"),
    (Expect(awaiting="email"), dict(awaiting=None), "awaiting"),
    (Expect(awaiting=None), dict(awaiting="email"), "awaiting"),  # an explicit null means: nothing is awaited
    (Expect(reply_is="ASK_EMAIL"), dict(reply=T.HANDOFF), "ASK_EMAIL"),
    (Expect(reply_contains=["TEMPLATE:NO_FULL_MATCH"]), dict(reply="khớp rồi"), "NO_FULL_MATCH"),
    (Expect(reply_contains=["{order:005}"]), dict(reply="đơn khác"), "{order:005}"),
    (Expect(reply_not_contains=["{email:005}"]), dict(reply=f"gửi {ORDERS['005']['email']}"), "contains"),
    (Expect(reply_not_contains=["TEMPLATE:ASK_PRODUCT_TYPE"]), dict(reply=T.ASK_PRODUCT_TYPE), "contains"),
    (Expect(reply_has_sku=True), dict(reply="không có mã"), "reply_has_sku"),
    (Expect(reply_has_sku=False), dict(reply="SM-NAM-0001"), "reply_has_sku"),
    (Expect(reply_cites_policy=True), dict(reply="đổi trong 7 ngày"), "reply_cites_policy"),
    (Expect(clarify_count=2), dict(clarify_count=1), "clarify_count"),
    (Expect(tickets_total=1), dict(tickets_total=0), "tickets_total"),
    (Expect(lookups_total=3), dict(lookups_total=4), "lookups_total"),
    (Expect(verify_failures=3), dict(verify_failures=2), "verify_failures"),
    (Expect(relaxed=["size"]), dict(relaxed=[]), "relaxed"),
    (Expect(no_tools=True), dict(tool_calls=2), "tool calls"),
    (Expect(no_tools=False), dict(tool_calls=0), "tool calls"),
    (Expect(), dict(capped=True), "capped"),  # no turn may be capped unless the case says so
])
def test_each_expectation_is_checked_and_a_miss_is_reported(expect, changes, fragment):
    problems = check_turn(expect, facts(**changes), ORDERS)
    assert problems and any(fragment in p for p in problems), problems


def test_the_citation_of_a_policy_is_recognised():
    ok = facts(reply="Phí là 30.000đ.\nNguồn: van-chuyen, Điều 2")
    assert check_turn(Expect(reply_cites_policy=True), ok, ORDERS) == []
    assert check_turn(Expect(reply_cites_policy=True), facts(reply="Nguồn: không rõ"), ORDERS)


def test_a_capped_turn_is_fine_only_when_the_case_expects_it():
    assert check_turn(Expect(capped=True), facts(capped=True), ORDERS) == []
