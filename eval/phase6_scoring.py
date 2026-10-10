"""Scoring rules of the Phase 6 evaluation on the dev split, written as code BEFORE any evaluation run (docs/DECISIONS-phase6.md, A4, A15).

Everything here is a pure function of what one conversation did (`Observation`) and of the golden label of the item. No LLM judges anything: a
rule that cannot be checked in code is not a rule of this evaluation.

Per intent (golden labels):
- product_search with gold SKUs: correct iff the final reply names at least one gold SKU (a SKU the reply shows, not one that was merely retrieved).
- product_search with no_answer (A4, approved by the owner): correct iff the reply explicitly says that nothing matches all the constraints (the
  fixed sentence NO_FULL_MATCH, and the first search really found nothing) AND every relaxed suggestion states which constraint was dropped (the
  fixed note comes before the first suggested SKU), AND only size, colour or material was relaxed (never kind, price or in_stock).
- policy_question: correct iff the reply cites the gold file and clause (the line "Nguồn: <file>, Điều <n>").
- order_status: the observed behaviour (answer, ask_for_verification, refuse) equals the label, and a reply that is not an answer shows nothing of
  the order.
- create_ticket: same behaviour check; and no ticket is ever written without a confirmation turn: for an `answer` label, a "yes" conversation
  writes exactly one ticket and a "no" conversation none; for every other label no conversation writes any. A ticket request that names no order
  (golden source no_answer) is correct iff the agent asks for a contact email and writes and looks up nothing.
- out_of_scope and smalltalk: correct iff no tool and no answer call was made and the reply is one of the two fixed polite replies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agent import templates as T
from app.agent.graph import MAX_CLARIFY, MAX_CONFIRM_ASKS, MAX_STEPS, MAX_VERIFY_FAILURES
from app.agent.guardrails import SKU
from app.agent.relax import RELAX_ORDER

_CITATION = re.compile(r"Nguồn:\s*([^\n]+)")
_PAIR = re.compile(r"([\w\-]+),\s*Điều\s+([0-9]+)")
REFUSALS = (T.REFUSE_VERIFICATION, T.REFUSE_VERIFICATION_LOCKED, T.LOOKUP_DISABLED)
ASKING = {"email", "order_number", "order_number_and_email"}
POLITE = (T.SMALLTALK, T.OUT_OF_SCOPE)
INTENTS = ("product_search", "policy_question", "order_status", "create_ticket", "out_of_scope", "smalltalk")


@dataclass
class Observation:
    """What the first turn of a conversation did. Built by the runner from the turn result and the instrumented tools."""

    reply: str
    intent: str | None = None
    awaiting: str | None = None
    relaxed: list = field(default_factory=list)
    first_search_empty: bool | None = None  # None: no product search was made
    product_searches: int = 0
    policy_searches: int = 0
    lookups: int = 0
    lookup_verified: bool = False
    tickets_written: int = 0
    answer_calls: int = 0  # answers written by the LLM (fresh or from the cache)
    fallback: bool = False
    order_facts: tuple = ()  # strings that must not appear in a reply that is not an answer: the order number's status words, SKUs, ...
    # the two simulated confirmations of a ticket that was proposed (create_ticket items)
    tickets_after_yes: int | None = None
    tickets_after_no: int | None = None

    @property
    def tool_calls(self) -> int:
        return self.product_searches + self.policy_searches + self.lookups + self.tickets_written


def reply_skus(reply: str) -> set[str]:
    return set(SKU.findall(reply))


def cited_clauses(reply: str) -> set[tuple[str, int]]:
    """The (file, clause) pairs of the citation lines of a reply."""
    return {(file, int(clause)) for line in _CITATION.findall(reply) for file, clause in _PAIR.findall(line)}


def observed_behaviour(obs: Observation, intent: str) -> str:
    """"refuse", "ask_for_verification", "answer" or "other": what the first turn did, in the vocabulary of the golden labels."""
    if obs.reply in REFUSALS:
        return "refuse"
    if obs.awaiting in ASKING:
        return "ask_for_verification"
    if intent == "order_status" and obs.lookup_verified and obs.awaiting is None and obs.answer_calls + int(obs.fallback) >= 1:
        return "answer"
    if intent == "create_ticket" and obs.lookup_verified and obs.awaiting == "ticket_confirmation":
        return "answer"
    return "other"


def _result(correct: bool, check: str, reason: str = "") -> dict:
    return {"correct": bool(correct), "check": check, "reason": reason}


def score_no_answer(obs: Observation) -> dict:
    """A4. Correct iff the reply says nothing matches all constraints and names what every suggestion left out."""
    if obs.first_search_empty is not True:
        return _result(False, "no_answer", "the first search already found products: the agent claimed a match the golden set says does not exist")
    if T.NO_FULL_MATCH not in obs.reply:
        return _result(False, "no_answer", "the reply does not say that nothing matches all the constraints")
    if any(name not in RELAX_ORDER for name in obs.relaxed):
        return _result(False, "no_answer", f"a protected constraint was relaxed: {obs.relaxed}")
    suggested = SKU.search(obs.reply)
    if suggested:  # suggestions exist: each relaxed constraint must be named before the first suggestion
        if not obs.relaxed:
            return _result(False, "no_answer", "the reply suggests products although nothing was relaxed")
        for name in obs.relaxed:
            note = T.relaxed_note(name)
            if note not in obs.reply or obs.reply.index(note) > suggested.start():
                return _result(False, "no_answer", f"the suggestions do not state before they begin that {name} was dropped")
    return _result(True, "no_answer")


def score_products(gold_skus: set[str], obs: Observation) -> dict:
    shown = reply_skus(obs.reply)
    if shown & gold_skus:
        return _result(True, "gold_sku")
    return _result(False, "gold_sku", "the reply names no gold SKU" + ("" if shown else " (it names no SKU at all)"))


def score_policy(gold_file: str, gold_clause: int, obs: Observation) -> dict:
    if (gold_file, gold_clause) in cited_clauses(obs.reply):
        return _result(True, "gold_clause")
    return _result(False, "gold_clause", f"the reply does not cite {gold_file} Điều {gold_clause}")


def _leaks(obs: Observation) -> list[str]:
    return [fact for fact in obs.order_facts if fact and fact in obs.reply]


def score_order(expected: str, obs: Observation) -> dict:
    behaviour = observed_behaviour(obs, "order_status")
    if behaviour != expected:
        return _result(False, "behaviour", f"expected {expected}, observed {behaviour}")
    if expected != "answer" and (leaked := _leaks(obs)):
        return _result(False, "behaviour", f"a reply that is not an answer shows order data: {leaked}")
    if expected == "ask_for_verification" and obs.lookups:
        return _result(False, "behaviour", "the order was looked up before the customer was verified")
    return _result(True, "behaviour")


def score_ticket(expected: str | None, obs: Observation) -> dict:
    """expected: the golden behaviour of an item with an order; None for a ticket request that names no order."""
    if obs.tickets_written:
        return _result(False, "ticket", "a ticket was written during the first turn, before any confirmation")
    if expected is None:
        ok = obs.awaiting == "email" and obs.lookups == 0
        return _result(ok, "ticket", "" if ok else "no contact email was asked for, or an order was looked up")
    behaviour = observed_behaviour(obs, "create_ticket")
    if behaviour != expected:
        return _result(False, "ticket", f"expected {expected}, observed {behaviour}")
    if expected == "answer":
        if (obs.tickets_after_yes, obs.tickets_after_no) != (1, 0):
            return _result(False, "ticket", f"after yes {obs.tickets_after_yes} tickets and after no {obs.tickets_after_no}, expected 1 and 0")
        return _result(True, "ticket")
    if (obs.tickets_after_yes or 0) or (obs.tickets_after_no or 0):
        return _result(False, "ticket", "a ticket was written in a conversation that was not verified")
    if expected == "refuse" and (leaked := _leaks(obs)):
        return _result(False, "ticket", f"a refusal shows order data: {leaked}")
    return _result(True, "ticket")


def score_polite(obs: Observation) -> dict:
    if obs.tool_calls or obs.answer_calls:
        return _result(False, "polite", "a tool or the answer step was used")
    if obs.reply not in POLITE:
        return _result(False, "polite", "the reply is not one of the two fixed polite replies")
    return _result(True, "polite")


def score_item(item, obs: Observation) -> dict:
    """Score one golden item (any object with .intent and .gold_source) from its observation."""
    source = item.gold_source
    if item.intent == "product_search":
        return score_no_answer(obs) if source.type == "no_answer" else score_products(set(source.skus), obs)
    if item.intent == "policy_question":
        return score_policy(source.document, int(source.clause), obs)
    if item.intent == "order_status":
        return score_order(source.expected_behavior, obs)
    if item.intent == "create_ticket":
        return score_ticket(None if source.type == "no_answer" else source.expected_behavior, obs)
    return score_polite(obs)


def aggregate(records: list[dict]) -> dict:
    """Per intent: n, correct, accuracy; product_search also split into gold-SKU and no_answer items. Records: {intent, kind, correct}."""
    def block(rows: list[dict]) -> dict:
        n, ok = len(rows), sum(r["correct"] for r in rows)
        return {"n": n, "correct": ok, "accuracy": ok / n if n else None}

    result = {intent: block([r for r in records if r["intent"] == intent]) for intent in INTENTS}
    result["product_search"]["by_kind"] = {kind: block([r for r in records if r["intent"] == "product_search" and r["kind"] == kind])
                                           for kind in ("skus", "no_answer")}
    result["all"] = block(records)
    return result


# --- the bounds, reported from what the conversations did -----------------------------------------------------------------------------------


BOUNDS = {"steps_per_turn": MAX_STEPS, "clarifying_questions": MAX_CLARIFY, "product_searches_per_turn": 2, "failed_verifications": MAX_VERIFY_FAILURES,
          "confirmation_asks": MAX_CONFIRM_ASKS, "tickets_without_confirmation": 0}


def bounds_report(observed: dict) -> dict:
    """observed: the largest value seen for each bound (and the number of tickets written without a yes). -> {bound: {limit, max_seen, held}}."""
    return {name: {"limit": limit, "max_seen": observed.get(name, 0), "held": observed.get(name, 0) <= limit} for name, limit in BOUNDS.items()}
