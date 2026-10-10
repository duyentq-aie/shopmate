"""The scripted multi-turn cases of the Phase 6 evaluation: loading, placeholders and the check of one turn (pure functions).

The cases are data (multiturn_cases.json); what a turn must satisfy is `Expect`. `check_turn` returns the list of what is wrong, empty when the
turn did what it had to. Nothing here runs the agent: eval/run_phase6_eval.py builds the `facts` of each turn from the conversation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent import templates as T
from app.agent.guardrails import SKU
from app.config import REPO_ROOT

CASES_PATH = REPO_ROOT / "eval" / "phase6" / "multiturn_cases.json"
ORDERS_PATH = REPO_ROOT / "data" / "seed" / "orders.vi.json"
_PLACEHOLDER = re.compile(r"\{(order|email):([0-9]{3})\}")
_TEMPLATE = re.compile(r"^TEMPLATE:([A-Z_]+)$")
_CITATION = re.compile(r"Nguồn:\s*[\w\-]+,\s*Điều\s+[0-9]+")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Expect(_Strict):
    """What must be true after a turn. Absent fields are not checked, except `capped`, which defaults to False (no turn may be capped)."""

    intent: str | None = None
    awaiting: str | None = None  # present with null means "nothing is awaited"
    reply_is: str | None = None  # the name of a fixed reply in app/agent/templates.py
    reply_contains: list[str] = Field(default_factory=list)
    reply_not_contains: list[str] = Field(default_factory=list)
    reply_has_sku: bool | None = None
    reply_cites_policy: bool | None = None
    clarify_count: int | None = None
    relaxed: list[str] | None = None
    tickets_total: int | None = None  # tickets written in this conversation so far
    lookups_total: int | None = None  # order lookups made in this conversation so far
    verify_failures: int | None = None
    capped: bool = False
    no_tools: bool | None = None  # no tool call in this turn

    @model_validator(mode="after")
    def _names_exist(self) -> "Expect":
        if self.reply_is is not None and not (self.reply_is.isupper() and hasattr(T, self.reply_is)):
            raise ValueError(f"reply_is {self.reply_is!r} is not a fixed reply of app/agent/templates.py")
        for text in self.reply_contains + self.reply_not_contains:
            template = _TEMPLATE.match(text)
            if template and not hasattr(T, template.group(1)):
                raise ValueError(f"{text!r} is not a fixed reply of app/agent/templates.py")
        return self


class Turn(_Strict):
    say: str = Field(min_length=1)
    expect: Expect


class Case(_Strict):
    id: str = Field(pattern=r"^mt-[0-9]{2}$")
    title: str
    checks: list[str] = Field(min_length=1)
    turns: list[Turn] = Field(min_length=1)


class CaseFile(_Strict):
    schema_version: int
    description: str
    cases: list[Case]

    @model_validator(mode="after")
    def _unique(self) -> "CaseFile":
        ids = [c.id for c in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("case ids must be unique")
        return self


def load_cases(path: Path = CASES_PATH) -> CaseFile:
    return CaseFile.model_validate_json(path.read_text(encoding="utf-8"))


def load_orders(path: Path = ORDERS_PATH) -> dict[str, dict]:
    """The seed orders by their three-digit suffix: {"005": {"number": "88", "email": "..."}}."""
    return {o["external_reference"].rsplit("-", 1)[-1]: {"number": str(o["number"]), "email": o["customer"]["email"]}
            for o in json.loads(path.read_text(encoding="utf-8"))["orders"]}


def resolve(text: str, orders: dict[str, dict]) -> str:
    """{order:005} -> its number, {email:005} -> the owner's email."""
    def sub(match: re.Match) -> str:
        order = orders[match.group(2)]
        return order["number"] if match.group(1) == "order" else order["email"]

    return _PLACEHOLDER.sub(sub, text)


def _expected_text(name: str, orders: dict[str, dict]) -> str:
    template = _TEMPLATE.match(name)
    return getattr(T, template.group(1)) if template else resolve(name, orders)


def check_turn(expect: Expect, facts: dict, orders: dict[str, dict]) -> list[str]:
    """What is wrong with a turn: `facts` has reply, intent, awaiting, clarify_count, relaxed, capped, tickets_total, lookups_total,
    verify_failures and tool_calls (this turn). [] when the turn did what it had to."""
    problems = []
    reply = facts["reply"]
    if expect.intent is not None and facts["intent"] != expect.intent:
        problems.append(f"intent {facts['intent']!r}, expected {expect.intent!r}")
    if "awaiting" in expect.model_fields_set and facts["awaiting"] != expect.awaiting:
        problems.append(f"awaiting {facts['awaiting']!r}, expected {expect.awaiting!r}")
    if expect.reply_is is not None and reply != getattr(T, expect.reply_is):
        problems.append(f"the reply is not {expect.reply_is}")
    for wanted in expect.reply_contains:
        if _expected_text(wanted, orders) not in reply:
            problems.append(f"the reply does not contain {wanted!r}")
    for unwanted in expect.reply_not_contains:
        if _expected_text(unwanted, orders) in reply:
            problems.append(f"the reply contains {unwanted!r}")
    if expect.reply_has_sku is not None and bool(SKU.search(reply)) != expect.reply_has_sku:
        problems.append(f"reply_has_sku is {bool(SKU.search(reply))}, expected {expect.reply_has_sku}")
    if expect.reply_cites_policy is not None and bool(_CITATION.search(reply)) != expect.reply_cites_policy:
        problems.append(f"reply_cites_policy is {bool(_CITATION.search(reply))}, expected {expect.reply_cites_policy}")
    for name in ("clarify_count", "tickets_total", "lookups_total", "verify_failures"):
        wanted = getattr(expect, name)
        if wanted is not None and facts[name] != wanted:
            problems.append(f"{name} is {facts[name]}, expected {wanted}")
    if expect.relaxed is not None and list(facts["relaxed"]) != expect.relaxed:
        problems.append(f"relaxed is {list(facts['relaxed'])}, expected {expect.relaxed}")
    if bool(facts["capped"]) != expect.capped:
        problems.append(f"capped is {bool(facts['capped'])}, expected {expect.capped}")
    if expect.no_tools is not None and (facts["tool_calls"] == 0) != expect.no_tools:
        problems.append(f"{facts['tool_calls']} tool calls, expected {'none' if expect.no_tools else 'some'}")
    return problems
