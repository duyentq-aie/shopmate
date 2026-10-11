"""The conversation agent: one `turn()` per customer message, one traced request per turn.

A turn is a run of the graph from START, except while a ticket waits for its confirmation: then the customer's message is the
resume value of the pending `interrupt()`. (A new input on a paused thread would only pause it again, so the choice is made here,
from the thread's pending interrupts, never from the message.) Conversations live in memory (InMemorySaver): a restart forgets
them, which is safe, because no confirmation means no ticket. Everything is synchronous.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field, replace

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from app.agent import templates as T
from app.agent.graph import MAX_STEPS, Deps, build_graph
from app.agent.protocols import Answerer, AgentTools
from app.tracing import Tracer, record_intent, span

# the per-turn part of the state, reset at the start of every turn (the conversation part is kept by the thread)
_PER_TURN = {"steps": 0, "capped": False, "goto": "", "reply": None, "reply_source": None, "reply_kind": None, "reply_args": {}, "filters": {}, "hits": [],
             "relaxed": [], "relaxed_once": False, "offer_dropped": [], "extract_failed": False, "order": None, "ticket_result": None, "awaiting": None}


@dataclass(frozen=True)
class TurnResult:
    reply: str
    intent: str | None
    awaiting: str | None  # what the next message should be: product_type, order_number, email, order_number_and_email, ticket_confirmation, offer_confirmation
    hits: list = field(default_factory=list)
    filters: dict | None = None
    relaxed: list = field(default_factory=list)
    steps: int = 0
    capped: bool = False
    clarify_count: int = 0
    reply_source: str | None = None  # llm | template | guardrail_fallback | clarify | error_template (what the trace records)
    trace_id: str | None = None  # the line this turn wrote in the trace file (None without a tracer)
    failed: bool = False  # the turn ended in an exception that `catch_errors` turned into the technical-problem reply


class Agent:
    def __init__(self, *, router, extractor, tools: AgentTools, answerer: Answerer, tracer: Tracer | None = None,
                 max_steps: int = MAX_STEPS, checkpointer=None, catch_errors: bool = False) -> None:
        self.max_steps, self.tracer = max_steps, tracer
        # False: an exception ends the turn and propagates (the trace is written anyway). True (the chat API): an Exception is traced as
        # an error and the customer gets the technical-problem reply; KeyboardInterrupt and SystemExit still propagate.
        self.catch_errors = catch_errors
        self._turns: dict[str, int] = {}  # turns so far per conversation: kept like the conversations themselves, in memory
        # LangGraph counts the start step and refuses to run a node past the limit. The step counter in the state must be what stops a turn
        # (it sends the safe reply and traces it), so LangGraph's limit leaves room for the one step the counter refuses: 8 + start + 1.
        self.recursion_limit = max_steps + 2
        self.graph = build_graph(Deps(router, extractor, tools, answerer), max_steps=max_steps, checkpointer=checkpointer or InMemorySaver())

    def _config(self, conversation_id: str) -> dict:
        return {"configurable": {"thread_id": conversation_id}, "recursion_limit": self.recursion_limit}

    def snapshot(self, conversation_id: str) -> dict:
        """The state of a conversation (for tests and debugging). Contains what the customer typed: never log it."""
        return dict(self.graph.get_state(self._config(conversation_id)).values)

    @staticmethod
    def _record_cap(trace, message: str) -> None:
        """On the request AND on a span (the safe reply that was sent), because the monitor lists errors by span."""
        with span("reply") as recorded:
            recorded.fail("agent_step_cap_reached", message)
        trace.fail("agent_step_cap_reached", message)

    def is_awaiting_confirmation(self, conversation_id: str) -> bool:
        """True while a ticket waits for the customer's yes or no (the thread is paused at the interrupt)."""
        return any(task.interrupts for task in self.graph.get_state(self._config(conversation_id)).tasks)

    def turn(self, conversation_id: str, message: str) -> TurnResult:
        number = self._turns[conversation_id] = self._turns.get(conversation_id, 0) + 1
        with (self.tracer.request(message, session_id=conversation_id, turn=number) if self.tracer is not None else nullcontext(None)) as trace:
            try:
                result = self._run(trace, conversation_id, message)
            except Exception as exc:
                if not self.catch_errors:
                    raise
                if trace is not None:
                    trace.record_exception(exc)
                result = TurnResult(reply=T.TECHNICAL_PROBLEM, intent=trace.intent if trace is not None else None, awaiting=None,
                                    reply_source="error_template", failed=True)
            if trace is not None:
                trace.set_reply(result.reply, result.reply_source)
                result = replace(result, trace_id=trace.trace_id)
            return result

    def _run(self, trace, conversation_id: str, message: str) -> TurnResult:
        config = self._config(conversation_id)
        state = self.graph.get_state(config)
        paused = any(task.interrupts for task in state.tasks)
        try:
            if paused:
                record_intent(state.values.get("intent"))
                out = self.graph.invoke(Command(resume=message, update=dict(_PER_TURN)), config)
            else:
                out = self.graph.invoke({**_PER_TURN, "conversation_id": conversation_id, "message": message}, config)
        except GraphRecursionError as exc:  # the backstop: the step counter should have stopped the turn first
            if trace is not None:
                self._record_cap(trace, f"LangGraph recursion limit reached: {str(exc)[:120]}")
            return TurnResult(reply=T.STEP_CAP, intent=self.snapshot(conversation_id).get("intent"), awaiting=None, capped=True,
                              steps=self.max_steps + 1, reply_source="error_template")
        values = self.graph.get_state(config).values
        interrupts = out.get("__interrupt__")
        if interrupts:  # the ticket waits for its confirmation: the customer sees the question of the interrupt
            reply, awaiting, steps, source = interrupts[0].value["prompt"], "ticket_confirmation", values.get("steps", 0) + 1, "clarify"
        else:
            reply, awaiting, steps = values.get("reply") or T.TECHNICAL_PROBLEM, values.get("awaiting"), values.get("steps", 0)
            source = values.get("reply_source") if values.get("reply") else "error_template"
            if values.get("capped"):
                source = "error_template"  # the step cap sends the safe reply, whichever node was running
        if values.get("capped") and trace is not None:
            self._record_cap(trace, f"the turn used more than {self.max_steps} steps")
        return TurnResult(reply=reply, intent=values.get("intent"), awaiting=awaiting, hits=list(values.get("hits") or []),
                          filters=values.get("filters"), relaxed=list(values.get("relaxed") or []), steps=steps,
                          capped=bool(values.get("capped")), clarify_count=values.get("clarify_count", 0), reply_source=source or "template")
