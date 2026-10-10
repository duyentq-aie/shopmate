"""The agent's LangGraph state machine (docs/AGENT-DESIGN.md).

The LLM is used only at fuzzy steps: `route`, `extract`, `compose`. Everything else is rules and tools. Every loop has a bound:
clarify 2, relax 1, failed verifications 3, confirmation asks 2, steps per turn 8. The per-turn graph has no cycle except the
bounded self-loop of `await_confirmation`. `NODE_TARGETS` is the single list of nodes and edges: the graph is built from it, a
node that asks to go anywhere else fails, and a test compares it with the table of docs/AGENT-DESIGN.md.

Nothing here is asynchronous (docs/DECISIONS-phase6.md, A13). `interrupt()` lives in a node of its own with no span and no side
effect before it: the node runs again from its first line when the customer answers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent import templates as T
from app.agent.parsing import mask_contacts, mask_email, parse_emails, parse_order_number, parse_phone, parse_yes_no
from app.agent.protocols import Answerer, AgentTools
from app.agent.relax import relax
from app.agent.state import AgentState
from app.tracing import current_trace, record_intent, span

MAX_STEPS = 8  # graph steps per turn
MAX_CLARIFY = 2  # clarifying questions for one request
MAX_VERIFY_FAILURES = 3  # failed order verifications per conversation
MAX_CONFIRM_ASKS = 2  # times the agent asks whether to create the ticket
SUMMARY_TEXT_LIMIT = 300

NODE_TARGETS: dict[str, tuple[str, ...]] = {
    "intake": ("route", "check_slots"),
    "route": ("extract", "search_policies", "check_slots", "reply"),
    "extract": ("check_product",),
    "check_product": ("search_products", "reply"),
    "search_products": ("relax_search", "compose"),
    "relax_search": ("compose",),
    "search_policies": ("compose",),
    "check_slots": ("lookup_order", "propose_ticket", "reply"),
    "lookup_order": ("compose", "propose_ticket", "reply"),
    "propose_ticket": ("await_confirmation",),
    "await_confirmation": ("await_confirmation", "write_ticket", "reply"),
    "write_ticket": ("reply",),
    "compose": (),  # ends the turn
    "reply": (),  # ends the turn
}


@dataclass(frozen=True)
class Deps:
    router: object  # route(text) -> object with .intent and .error
    extractor: object  # extract(text) -> object with .filters (SearchFilters | None)
    tools: AgentTools
    answerer: Answerer


def next_after(state: AgentState) -> str:
    """The conditional edge of every node: the node's own choice, or the end if the step cap was hit."""
    return END if state.get("capped") else state.get("goto", END)


def _say(kind: str, **args) -> dict:
    """Ask the `reply` node to render a fixed reply."""
    return {"reply_kind": kind, "reply_args": args, "goto": "reply"}


def _finished(state: AgentState) -> dict:
    """What is forgotten when a request ends: the question being waited for, the counters, the order number, the ticket draft.
    The customer's email stays (they gave it to us in this conversation); the failed-verification counter is never reset."""
    email = (state.get("slots") or {}).get("email")
    return {"pending": None, "clarify_count": 0, "slots": {"email": email} if email else {}, "query": None, "ticket": None,
            "ticket_text": None, "ticket_prompt": None, "confirm_attempts": 0, "awaiting": None}


def guarded(name: str, fn: Callable[[AgentState], dict], max_steps: int = MAX_STEPS) -> Callable[[AgentState], dict]:
    """Wrap a node: count the step, refuse it past the cap, and check that it only goes where NODE_TARGETS says."""

    def run(state: AgentState) -> dict:
        steps = state.get("steps", 0) + 1
        if steps > max_steps:
            return {"steps": steps, "capped": True, "reply": T.STEP_CAP, "goto": END, **_finished(state)}
        update = fn(state) or {}
        targets = NODE_TARGETS.get(name)
        if targets is not None and update.get("goto") is not None and update["goto"] not in targets:
            raise AssertionError(f"node {name} asked to go to {update['goto']!r}; its declared targets are {targets}")
        return {**update, "steps": steps}

    run.__name__ = name
    return run


def build_graph(deps: Deps, *, max_steps: int = MAX_STEPS, checkpointer=None):
    tools, answerer = deps.tools, deps.answerer

    # --- reading the customer's message -------------------------------------------------------------------------------------

    def intake(state: AgentState) -> dict:
        message, pending = state["message"], state.get("pending")
        need = (pending or {}).get("need", [])
        slots = dict(state.get("slots") or {})
        emails, phone = parse_emails(message), parse_phone(message)
        number = parse_order_number(message, bare_ok="order_number" in need)
        if len(emails) == 1:  # two different addresses are ambiguous: keep what we had and ask again
            slots["email"] = emails[0]
        if number:
            slots["order_number"] = number
        if phone:
            slots["phone_seen"] = True  # the number itself is not kept
        answers_the_question = bool(pending and pending["intent"] in ("order_status", "create_ticket")
                                    and (("email" in need and (len(emails) == 1 or phone)) or ("order_number" in need and number)))
        if answers_the_question:
            return {"slots": slots, "intent": pending["intent"], "goto": "check_slots"}
        return {"slots": slots, "goto": "route"}

    def route(state: AgentState) -> dict:
        result = deps.router.route(state["message"])
        if result.intent is None:
            trace = current_trace()
            if trace is not None:
                trace.fail("llm_invalid_output", result.error or "the router returned no intent")
            return {"intent": None, **_say("technical_problem")}
        intent, pending = result.intent, state.get("pending")
        record_intent(intent)
        update: dict = {"intent": intent}
        if pending and pending["intent"] != intent:  # a new topic: the old question is dropped
            update.update(pending=None, clarify_count=0, query=None)
            pending = None
        if intent == "product_search":
            continuing = bool(pending and state.get("query"))
            update.update(query=f"{state['query']} {state['message']}" if continuing else state["message"], goto="extract")
        elif intent == "policy_question":
            update.update(query=state["message"], goto="search_policies")
        elif intent in ("order_status", "create_ticket"):
            update["goto"] = "check_slots"
            if intent == "create_ticket" and not pending:
                update["ticket_text"] = state["message"]
        else:  # smalltalk, out_of_scope
            update.update(_say(intent))
        return update

    # --- products ---------------------------------------------------------------------------------------------------------------

    def extract(state: AgentState) -> dict:
        result = deps.extractor.extract(state["query"])
        if result.filters is None:  # no usable extraction: search with the text alone, do not quiz the customer about it
            return {"filters": {}, "extract_failed": True, "goto": "check_product"}
        filters = result.filters.model_dump(exclude_none=True)
        filters.pop("doc_type", None)
        return {"filters": filters, "goto": "check_product"}

    def check_product(state: AgentState) -> dict:
        filters = state.get("filters") or {}
        if state.get("extract_failed") or filters.get("kind") or filters.get("category"):
            return {"goto": "search_products"}
        count = state.get("clarify_count", 0)
        if count >= MAX_CLARIFY:  # asked twice already: answer with what is known
            return {"goto": "search_products"}
        return {"clarify_count": count + 1, "pending": {"intent": "product_search", "need": ["product_type"]}, **_say("ask_product_type")}

    def search_products(state: AgentState) -> dict:
        filters = state.get("filters") or {}
        hits = tools.search_products(state["query"], filters)
        if not hits and not state.get("relaxed_once") and relax(filters) is not None:
            return {"hits": hits, "goto": "relax_search"}
        return {"hits": hits, "goto": "compose"}

    def relax_search(state: AgentState) -> dict:
        relaxed_filters, dropped = relax(state.get("filters") or {})
        hits = tools.search_products(state["query"], relaxed_filters)
        return {"hits": hits, "relaxed": [dropped], "relaxed_once": True, "goto": "compose"}

    def search_policies(state: AgentState) -> dict:
        return {"hits": tools.search_policies(state["query"]), "goto": "compose"}

    # --- orders and tickets -------------------------------------------------------------------------------------------------------

    def check_slots(state: AgentState) -> dict:
        intent, slots = state["intent"], state.get("slots") or {}
        number, email = slots.get("order_number"), slots.get("email")
        needs_lookup = intent == "order_status" or (intent == "create_ticket" and number is not None)
        if needs_lookup and state.get("lookup_disabled"):
            return _say("lookup_disabled")
        need = [name for name, missing in (("order_number", needs_lookup and number is None), ("email", email is None)) if missing]
        if need:
            count = state.get("clarify_count", 0)
            if count >= MAX_CLARIFY:  # asked twice already: hand off, never a third question
                return _say("handoff")
            if need == ["order_number", "email"]:
                kind = "ask_number_and_email"
            elif need == ["order_number"]:
                kind = "ask_order_number"
            elif slots.get("phone_seen"):
                kind = "ask_email_for_phone"
            else:
                kind = "ask_email" if needs_lookup else "ask_contact_email"
            return {"clarify_count": count + 1, "pending": {"intent": intent, "need": need}, **_say(kind)}
        return {"goto": "lookup_order" if needs_lookup else "propose_ticket"}

    def lookup_order(state: AgentState) -> dict:
        slots = state["slots"]
        result = tools.lookup_order(slots["order_number"], slots["email"])
        if result.verified:
            return {"order": result.order, "goto": "compose" if state["intent"] == "order_status" else "propose_ticket"}
        failures = state.get("verify_failures", 0) + 1
        update = {"verify_failures": failures}  # the reply node forgets the order number: the next try needs a new one
        if failures >= MAX_VERIFY_FAILURES:
            return {**update, "lookup_disabled": True, **_say("refuse_verification_locked")}
        return {**update, **_say("refuse_verification")}

    def propose_ticket(state: AgentState) -> dict:
        slots = state.get("slots") or {}
        order_number = slots.get("order_number") if state.get("order") else None  # only an order that was verified is named
        with span("ticket_confirm"):
            text = mask_contacts(" ".join((state.get("ticket_text") or state["message"]).split()))[:SUMMARY_TEXT_LIMIT]
            lines = [f'Nội dung: "{text}"']
            if order_number:
                lines.append(f"Đơn hàng: #{order_number}")
            lines.append(f"Email liên hệ: {mask_email(slots['email'])}")
            summary = "\n".join(lines)
        return {"ticket": {"summary": summary, "order_number": order_number, "email": slots["email"]}, "ticket_prompt": T.ticket_proposal(summary),
                "confirm_attempts": 0, "goto": "await_confirmation"}

    def await_confirmation(state: AgentState) -> dict:
        attempts = state.get("confirm_attempts", 0)
        prompt = state["ticket_prompt"] if attempts == 0 else T.TICKET_REASK
        answer = interrupt({"kind": "ticket_confirmation", "prompt": prompt, "attempt": attempts})
        decision = parse_yes_no(str(answer))
        attempts += 1
        if decision == "yes":
            return {"confirm_attempts": attempts, "goto": "write_ticket"}
        if decision == "no":
            return _say("ticket_cancelled")
        if attempts >= MAX_CONFIRM_ASKS:  # not clearly a yes twice: no ticket
            return _say("ticket_not_confirmed")
        return {"confirm_attempts": attempts, "goto": "await_confirmation"}

    def write_ticket(state: AgentState) -> dict:
        ticket = state["ticket"]
        with span("ticket_confirm") as recorded:
            try:
                result = tools.create_ticket(conversation_id=state["conversation_id"], summary=ticket["summary"],
                                             order_number=ticket["order_number"], contact_email=ticket["email"])
            except Exception as exc:  # the customer said yes and nothing was written: say so, and trace it
                message = f"{type(exc).__name__}: {exc}"
                recorded.fail("ticket_write_failed", message)
                trace = current_trace()
                if trace is not None:
                    trace.fail("ticket_write_failed", message)
                return _say("ticket_failed")
        return {"ticket_result": result, **_say("ticket_created", ticket_id=result["ticket_id"])}

    # --- the answer and the fixed replies ---------------------------------------------------------------------------------------

    def compose(state: AgentState) -> dict:
        intent = state["intent"]
        kind = {"product_search": "products", "policy_question": "policies", "order_status": "order"}[intent]
        hits, order, relaxed = state.get("hits") or [], state.get("order"), state.get("relaxed") or []
        evidence = {"order": order} if kind == "order" else {"hits": hits}
        if kind == "products":
            evidence["relaxed"] = relaxed
        if not (order if kind == "order" else hits):  # nothing to ground an answer on: say so, without the LLM
            text = (T.no_results_relaxed(relaxed[0]) if relaxed else T.NO_RESULTS) if kind == "products" else T.NO_POLICY_INFO
            with span("reply"):
                pass
            return {"reply": text, **_finished(state)}
        with span("answer", model=getattr(answerer, "model", None)) as recorded:
            answer = answerer.compose(kind, state.get("query") or state["message"], evidence)
            if answer.generation is not None:
                recorded.record_response(answer.generation)
            if answer.fallback_reason:
                recorded.fail("answer_guardrail_fallback", answer.fallback_reason)
        text = answer.text
        if kind == "products" and relaxed:  # the notice is code, not the LLM: it comes before any suggestion
            text = f"{T.NO_FULL_MATCH} {T.relaxed_note(relaxed[0])}\n{text}"
        return {"reply": text, **_finished(state)}

    def reply(state: AgentState) -> dict:
        kind = state["reply_kind"]
        with span("reply"):
            text = T.render(kind, **(state.get("reply_args") or {}))
        if kind in T.AWAITING:  # a question: the request is not over
            return {"reply": text, "awaiting": T.AWAITING[kind]}
        return {"reply": text, **_finished(state)}

    nodes = {"intake": intake, "route": route, "extract": extract, "check_product": check_product, "search_products": search_products,
             "relax_search": relax_search, "search_policies": search_policies, "check_slots": check_slots, "lookup_order": lookup_order,
             "propose_ticket": propose_ticket, "await_confirmation": await_confirmation, "write_ticket": write_ticket,
             "compose": compose, "reply": reply}
    assert set(nodes) == set(NODE_TARGETS)
    builder = StateGraph(AgentState)
    for name, fn in nodes.items():
        builder.add_node(name, guarded(name, fn, max_steps))
    builder.add_edge(START, "intake")
    for name, targets in NODE_TARGETS.items():
        if targets:
            builder.add_conditional_edges(name, next_after, {**{target: target for target in targets}, END: END})
        else:
            builder.add_edge(name, END)
    return builder.compile(checkpointer=checkpointer)
