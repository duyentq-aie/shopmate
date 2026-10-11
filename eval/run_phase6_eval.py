"""Phase 6 evaluation on the DEV split: the conversation agent, end to end (docs/DECISIONS-phase6.md, docs/AGENT-DESIGN.md section 10).

What is measured (dev only: `load_split("dev")` is the only way golden items are read; the test split is never opened):
- every dev item as the first turn of a conversation of its own, scored by the rules of eval/phase6_scoring.py (written before any run);
- the tickets: for every proposal, a conversation that answers "có" and one that answers "không" are simulated, in a temporary ticket file;
- the scripted multi-turn cases of eval/phase6/multiturn_cases.json (outside the golden set), checked turn by turn;
- every bound of the agent (steps, clarifying questions, searches, failed verifications, confirmation asks, tickets without a yes);
- the guardrails (how often the fallback answer replaced the generated one, and which rule fired; how often the model said it had no information:
  a decline, not a fallback), LLM calls and tokens per turn, latency per turn,
  the theoretical paid-tier cost per 1,000 conversations.

Free-tier guards: LLM calls go through the disk cache (eval/llm_cache/, committed): a re-run makes ZERO API calls, and the router and extractor
answers of the Phase 5 evaluation are reused where the keys match (same prompt, same text). Live calls need --live; without it a cache miss
stops the run. Live runs are rate limited, capped per run and per day (450, counted across runs). When the daily budget cannot cover the next
item, or a quota error stops the run, it STOPS AT A CLEAN POINT: it writes `<prompt>.progress.json` (what is done and why it stopped) and exits 2;
run the same command again after the reset (00:00 Pacific, 14:00 in Vietnam until 1 November) and everything finished replays from the cache.

Usage (from the repo root; Qdrant and Saleor running, the index built):
    uv run python -m eval.run_phase6_eval                  # offline: needs a complete cache
    uv run python -m eval.run_phase6_eval --live           # fetch what is missing (counts against the daily quota)
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from app.agent import templates as T
from app.agent.agent import Agent
from app.agent.answer import ANSWER_GENERATION_CONFIG, ANSWER_SCHEMA, GeminiAnswerer
from app.agent.graph import MAX_STEPS
from app.agent.parsing import parse_yes_no
from app.agent.tools import PRODUCT_K, POLICY_K, RealTools, TicketStore
from app.config import REPO_ROOT, get_setting
from app.llm.cache import DiskCache
from app.llm.client import MODEL_ID, GeminiClient
from app.llm.errors import LLMError
from app.llm.extractor import EXTRACTOR_GENERATION_CONFIG, FilterExtractor, build_schema
from app.llm.generator import CachedGenerator
from app.llm.limits import DailyBudget, RateLimiter
from app.llm.prompts import catalog_vocabulary, load_prompt
from app.llm.router import ROUTER_GENERATION_CONFIG, ROUTER_SCHEMA, GeminiRouter
from app.tracing import Tracer, validate_trace_line
from eval.golden.loader import golden_file_sha256, items_hash, load_split
from eval.phase6 import cases as case_module
from eval.phase6.cases import check_turn, load_cases, load_orders
from eval.phase6_scoring import BOUNDS, Observation, aggregate, bounds_report, score_item
from eval.retrieval_metrics import percentile_ms
from eval.run_phase5_eval import ALLOWED_UNTRACKED, CACHE_DIR, DAILY_CEILING, RPM, TPM, USAGE_DIR, question_for
from eval.run_retrieval_eval import (
    DirtyWorkingTree,
    canonical_sha256,
    check_clean_tree,
    index_problems,
    mode_problems,
    resolve_results_dir,
    write_result,
)

RESULTS_SUBDIR = "eval/results/phase6"
CONFIRM_YES, CONFIRM_NO = "có", "không"
MIN_HEADROOM = 4  # live requests that must be left before a new item is started: router, extractor, answer and one retry
ORDER_STATUS_WORDS = ("chưa giao", "đã giao", "đã bị hủy", "chưa được xác nhận")
NON_LLM_STAGES = ("retrieval", "order_lookup", "ticket_confirm", "reply")


# --- instrumentation: what the tools and the model did, turn by turn ----------------------------------------------------------------------------


class InstrumentedTools:
    """Wraps the tools and counts what the agent asks of them (the counters only grow; callers take differences)."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.product_hits: list[int] = []  # the number of hits of each product search, in order
        self.policy_searches = 0
        self.lookups: list[bool] = []  # verified or not, in order
        self.tickets: list[bool] = []  # created (True) or already there (False), in order

    def search_products(self, query, filters):
        hits = self.inner.search_products(query, filters)
        self.product_hits.append(len(hits))
        return hits

    def search_policies(self, query):
        self.policy_searches += 1
        return self.inner.search_policies(query)

    def offer_for(self, filters):  # counts only (Qdrant count, no search): not a product search, so not part of what the scoring observes
        return self.inner.offer_for(filters)

    def lookup_order(self, order_number, email):
        result = self.inner.lookup_order(order_number, email)
        self.lookups.append(result.verified)
        return result

    def create_ticket(self, **kwargs):
        result = self.inner.create_ticket(**kwargs)
        self.tickets.append(result["created"])
        return result

    def mark(self) -> tuple:
        return len(self.product_hits), self.policy_searches, len(self.lookups), len(self.tickets)


class RecordingGenerator:
    """The generator the components use, recording every call: which component, whether it came from the cache, the tokens, the latency."""

    def __init__(self, inner: CachedGenerator) -> None:
        self.inner, self.log = inner, []

    @property
    def model(self) -> str:
        return self.inner.model

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def generate(self, *, component, **kwargs):
        result = self.inner.generate(component=component, **kwargs)
        self.log.append({"component": component, "cached": result.cached, "input": result.usage.input_tokens, "output": result.usage.output_tokens,
                         "thought": result.usage.thought_tokens, "latency_ms": result.latency_ms})
        return result


class Harness:
    """Everything one evaluation needs, so that it can be built from real parts (main) or from fakes (tests)."""

    def __init__(self, agent: Agent, tools: InstrumentedTools, generator: RecordingGenerator, answerer, tickets: TicketStore, tracer: Tracer,
                 orders: dict[str, dict], budget: DailyBudget | None = None, fixtures: dict[str, dict] | None = None) -> None:
        self.agent, self.tools, self.generator, self.answerer, self.tickets, self.tracer = agent, tools, generator, answerer, tickets, tracer
        self.orders, self.budget, self.fixtures = orders, budget, fixtures or {}
        self.trace_offset = 0
        self.seen = {"steps_per_turn": 0, "clarifying_questions": 0, "product_searches_per_turn": 0, "failed_verifications": 0,
                     "confirmation_asks": 0, "tickets_without_confirmation": 0}
        self._asks: Counter = Counter()
        self._awaiting: dict[str, str | None] = {}
        self.turn_records: list[dict] = []  # one entry per turn, for the call and latency statistics

    def tickets_of(self, conversation_id: str) -> int:
        if not self.tickets.path.exists():
            return 0
        return sum(1 for line in self.tickets.path.read_text(encoding="utf-8").splitlines()
                   if line.strip() and json.loads(line)["conversation_id"] == conversation_id)

    def _new_trace_line(self) -> dict:
        data = self.tracer.path.read_bytes()[self.trace_offset:]
        self.trace_offset += len(data)
        (line,) = [json.loads(x) for x in data.decode("utf-8").splitlines()]  # exactly one line per turn
        return line

    def turn(self, conversation_id: str, message: str, *, role: str) -> dict:
        marks, log_before, fallbacks_before = self.tools.mark(), len(self.generator.log), self.answerer.stats.fallbacks
        reasons_before, declines_before = len(self.answerer.stats.reasons), self.answerer.stats.declines
        result = self.agent.turn(conversation_id, message)
        after = self.tools.mark()
        delta = {"product_searches": after[0] - marks[0], "policy_searches": after[1] - marks[1], "lookups": after[2] - marks[2], "tickets": after[3] - marks[3]}
        first_search_empty = (self.tools.product_hits[marks[0]] == 0) if delta["product_searches"] else None
        verified = any(self.tools.lookups[marks[2]:after[2]])
        calls = self.generator.log[log_before:]
        line = self._new_trace_line()
        problems = validate_trace_line(line)
        assert not problems, f"the trace line of a turn is invalid: {problems}"
        verify_failures = self.agent.snapshot(conversation_id).get("verify_failures", 0)
        if result.awaiting == "ticket_confirmation":
            self._asks[conversation_id] += 1
        if delta["tickets"] and not (parse_yes_no(message) == "yes" and self._awaiting.get(conversation_id) == "ticket_confirmation"):
            self.seen["tickets_without_confirmation"] += delta["tickets"]  # a ticket written by a turn that was not an explicit yes to a proposal
        self._awaiting[conversation_id] = result.awaiting
        for name, value in (("steps_per_turn", result.steps), ("clarifying_questions", result.clarify_count), ("product_searches_per_turn", delta["product_searches"]),
                            ("failed_verifications", verify_failures), ("confirmation_asks", self._asks[conversation_id])):
            self.seen[name] = max(self.seen[name], value)
        record = {"role": role, "conversation": conversation_id, "llm_calls": len(calls), "calls": calls, "steps": result.steps,
                  "non_llm_ms": sum(s["duration_ms"] for s in line["spans"] if s["stage"] in NON_LLM_STAGES)}
        self.turn_records.append(record)
        return {"result": result, "delta": delta, "first_search_empty": first_search_empty, "verified": verified, "verify_failures": verify_failures,
                "fallback": self.answerer.stats.fallbacks > fallbacks_before, "declined": self.answerer.stats.declines > declines_before,
                "answer_calls": sum(1 for c in calls if c["component"] == "answer"), "record": record,
                "fallback_rules": sorted({rule for rules in self.answerer.stats.reasons[reasons_before:] for rule in rules})}


# --- the dev items ---------------------------------------------------------------------------------------------------------------------------------


def _item_kind(item) -> str:
    return "no_answer" if item.gold_source.type == "no_answer" else {"product_search": "skus"}.get(item.intent, "none")


def _order_facts(item, fixtures: dict[str, dict]) -> tuple:
    number = str(getattr(item.gold_source, "order_number", ""))
    order = fixtures.get(number)
    if order is None:
        return ()
    return tuple(line["sku"] for line in order["lines"]) + tuple(line["product_name"] for line in order["lines"]) + ORDER_STATUS_WORDS


def evaluate_item(item, h: Harness) -> dict:
    cid, message = f"dev-{item.id}", question_for(item)
    first = h.turn(cid, message, role="dev")
    result, delta = first["result"], first["delta"]
    obs = Observation(reply=result.reply, intent=result.intent, awaiting=result.awaiting, relaxed=list(result.relaxed), first_search_empty=first["first_search_empty"],
                      product_searches=delta["product_searches"], policy_searches=delta["policy_searches"], lookups=delta["lookups"],
                      lookup_verified=first["verified"], tickets_written=delta["tickets"], answer_calls=first["answer_calls"], fallback=first["fallback"],
                      order_facts=_order_facts(item, h.fixtures) if item.intent in ("order_status", "create_ticket") else ())
    if result.awaiting == "ticket_confirmation":  # the proposal is answered both ways, in conversations of their own
        for answer, name in ((CONFIRM_YES, "yes"), (CONFIRM_NO, "no")):
            branch = f"{cid}-{name}"
            h.turn(branch, message, role="simulation")
            h.turn(branch, answer, role="simulation")
            setattr(obs, f"tickets_after_{name}", h.tickets_of(branch))
    score = score_item(item, obs)
    return {"id": item.id, "intent": item.intent, "kind": _item_kind(item), "correct": score["correct"], "check": score["check"], "reason": score["reason"],
            "routed_to": result.intent, "awaiting": result.awaiting, "fallback": first["fallback"], "fallback_rules": first["fallback_rules"],
            "declined": first["declined"],
            "retrieved_skus": [h["sku"] for h in result.hits if "sku" in h], "retrieved_clauses": [[h["file"], h["clause"]] for h in result.hits if "clause" in h],
            "llm_calls": first["record"]["llm_calls"], "steps": result.steps, "reply": result.reply}


# --- the multi-turn cases --------------------------------------------------------------------------------------------------------------------------


def evaluate_case(case, h: Harness) -> dict:
    cid, lookups_total, turns = case.id, 0, []
    for index, turn in enumerate(case.turns, start=1):
        record = h.turn(cid, case_module.resolve(turn.say, h.orders), role="case")
        result, delta = record["result"], record["delta"]
        lookups_total += delta["lookups"]
        facts = {"reply": result.reply, "intent": result.intent, "awaiting": result.awaiting, "clarify_count": result.clarify_count, "relaxed": result.relaxed,
                 "capped": result.capped, "tickets_total": h.tickets_of(cid), "lookups_total": lookups_total, "verify_failures": record["verify_failures"],
                 "tool_calls": sum(delta.values())}
        problems = check_turn(turn.expect, facts, h.orders)
        turns.append({"turn": index, "passed": not problems, "problems": problems, "reply": result.reply, "llm_calls": record["record"]["llm_calls"]})
    return {"id": case.id, "title": case.title, "checks": case.checks, "passed": all(t["passed"] for t in turns), "turns": turns}


# --- the whole evaluation -------------------------------------------------------------------------------------------------------------------------


def _call_summary(entries: list[dict]) -> dict:
    """Calls and tokens (what a replay reproduces exactly). Whether a call came from the cache is NOT here: it differs between a live run and a replay."""
    return {"n": len(entries), "input_tokens": sum(c["input"] for c in entries), "output_tokens": sum(c["output"] for c in entries),
            "thought_tokens": sum(c["thought"] for c in entries)}


def _per_call_cost(calls: dict, price_input: float, price_output: float) -> float:
    if not calls["n"]:
        return 0.0
    return (calls["input_tokens"] * price_input + (calls["output_tokens"] + calls["thought_tokens"]) * price_output) / calls["n"] / 1e6


def evaluate_phase6(items, cases, h: Harness, *, prices: tuple[float, float] | None = None, price_source: str | None = None) -> dict:
    """-> {"complete": bool, "stopped": {...} | None, "deterministic": {...}, "measurements": {...}}. `deterministic` holds everything that a replay
    from the cache must reproduce exactly; the latency (partly measured now) is in `measurements`."""
    done_items, done_cases, stopped = [], [], None
    live = h.generator.inner.client is not None
    work = [("item", item) for item in sorted(items, key=lambda i: i.id)] + [("case", case) for case in cases]
    for kind, unit in work:
        if live and h.budget is not None and h.budget.limit - h.budget.used() < MIN_HEADROOM:  # a clean point: do not start what may not finish
            stopped = {"before": unit.id, "reason": f"the daily budget has {h.budget.limit - h.budget.used()} requests left, and the next unit may need {MIN_HEADROOM}"}
            break
        try:
            (done_items if kind == "item" else done_cases).append(evaluate_item(unit, h) if kind == "item" else evaluate_case(unit, h))
        except LLMError as exc:  # a quota, a cap, an exhausted retry or a cache miss: stop here and keep what is done
            stopped = {"before": unit.id, "reason": f"{type(exc).__name__}: {exc}"[:300]}
            break
    dev_turns = [r for r in h.turn_records if r["role"] == "dev"]
    all_turns = [r for r in h.turn_records if r["role"] in ("dev", "case")]
    by_component = {name: _call_summary([c for r in dev_turns for c in r["calls"] if c["component"] == name]) for name in ("router", "filters", "answer")}
    deterministic = {
        "per_intent": aggregate([{"intent": r["intent"], "kind": r["kind"], "correct": r["correct"]} for r in done_items]),
        "items": done_items,
        "multiturn": {"passed": sum(c["passed"] for c in done_cases), "total": len(done_cases), "cases": done_cases},
        "bounds": bounds_report(h.seen),
        "guardrails": {"answers": h.answerer.stats.answers, "fallbacks": h.answerer.stats.fallbacks, "declines": h.answerer.stats.declines,
                       "rules": dict(Counter(rule for rules in h.answerer.stats.reasons for rule in rules)),
                       "items_with_fallback": [r["id"] for r in done_items if r["fallback"]],
                       "items_with_decline": [r["id"] for r in done_items if r["declined"]]},
        "calls": {"dev_turns": len(dev_turns), "llm_calls_per_turn": {"mean": (sum(r["llm_calls"] for r in all_turns) / len(all_turns)) if all_turns else None,
                                                                      "max": max((r["llm_calls"] for r in all_turns), default=0)},
                  "by_component_dev_first_turns": by_component},
        "steps": {"max_steps": MAX_STEPS, "max_seen": max((r["steps"] for r in h.turn_records), default=0)},
    }
    if prices is not None and dev_turns:
        per_turn = sum(_per_call_cost(by_component[c], *prices) * by_component[c]["n"] for c in by_component) / len(dev_turns)
        deterministic["cost_theoretical_paid_tier"] = {"prices_usd_per_million_tokens": {"input": prices[0], "output": prices[1]}, "source": price_source,
                                                       "per_1000_conversations_usd": 1000 * per_turn, "free_tier_cost_usd": 0,
                                                       "note": "a conversation here is one dev item (one turn); the dev mix is not a traffic forecast"}
    latencies = [(sum(c["latency_ms"] for c in r["calls"]) + r["non_llm_ms"]) / 1000 for r in all_turns]
    measurements = {"calls_from_the_cache": {name: sum(c["cached"] for r in dev_turns for c in r["calls"] if c["component"] == name) for name in by_component},
                    "latency_ms_per_turn": {"p50": percentile_ms(latencies, 0.5), "p95": percentile_ms(latencies, 0.95), "n": len(latencies),
                                            "note": "recorded LLM latencies (kept in the cache) plus the retrieval, order lookup and reply time measured in this run"}}
    return {"complete": stopped is None, "stopped": stopped, "deterministic": deterministic, "measurements": measurements}


# --- the command line ------------------------------------------------------------------------------------------------------------------------------


def build_harness(args, tmp_dir: Path):
    from app.retrieval.embedder import BgeM3Embedder, EmbeddingConfig
    from app.retrieval.lexical import LexicalConfig
    from app.retrieval.manifest import load_manifest
    from app.retrieval.search import Retriever
    from app.retrieval.store import connect
    from app.saleor.readonly_client import ReadOnlySaleorClient
    from app.config import api_url, require_setting
    from app.config import DEFAULT_RETRIEVAL_MODE

    manifest = load_manifest()
    problems = mode_problems(DEFAULT_RETRIEVAL_MODE, manifest)
    client = connect()
    problems += index_problems(client, manifest, EmbeddingConfig(), query_lexical=LexicalConfig())
    if problems:
        raise SystemExit("REFUSED: the live index does not match data/index/manifest.json:\n  " + "\n  ".join(problems))
    saleor = ReadOnlySaleorClient(api_url(), require_setting("SALEOR_RUNTIME_TOKEN"))
    saleor.execute("ChannelBySlug", {"slug": "vn"})  # reachable and the token works, before anything is spent
    llm, budget = None, DailyBudget(USAGE_DIR, limit=DAILY_CEILING)
    if args.live:
        key = get_setting("GEMINI_API_KEY")
        if not key:
            raise SystemExit("REFUSED: --live needs GEMINI_API_KEY in .env or the environment")
        llm = GeminiClient(key, limiter=RateLimiter(rpm=RPM, tpm=TPM), budget=budget, max_live_requests=args.max_live_calls)
    generator = RecordingGenerator(CachedGenerator(DiskCache(CACHE_DIR), llm))
    tickets = TicketStore(tmp_dir / "tickets.jsonl")
    tools = InstrumentedTools(RealTools(Retriever(client, BgeM3Embedder(), collection=manifest["collection"]), saleor, tickets))
    answerer = GeminiAnswerer(generator, load_prompt(args.answer_prompt))
    tracer = Tracer(tmp_dir / "traces.jsonl")
    agent = Agent(router=GeminiRouter(generator, load_prompt("router_v1")), extractor=FilterExtractor(generator, load_prompt("filters_v1")), tools=tools,
                  answerer=answerer, tracer=tracer)
    fixtures = {str(o["number"]): o for o in json.loads(case_module.ORDERS_PATH.read_text(encoding="utf-8"))["orders"]}
    return Harness(agent, tools, generator, answerer, tickets, tracer, load_orders(), budget, fixtures), manifest, saleor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--answer-prompt", default="answer_v1")
    parser.add_argument("--live", action="store_true", help="allow live API calls for cache misses (default: offline)")
    parser.add_argument("--max-live-calls", type=int, default=300, help="hard cap on live requests in this run")
    parser.add_argument("--results-dir", default=RESULTS_SUBDIR)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--price-input-per-million", type=float, default=None, help="theoretical paid-tier USD per 1M input tokens")
    parser.add_argument("--price-output-per-million", type=float, default=None, help="theoretical paid-tier USD per 1M output tokens")
    parser.add_argument("--price-source", default=None, help="where and when the prices were read")
    args = parser.parse_args(argv)
    try:
        results_dir = resolve_results_dir(args.results_dir)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    out = results_dir / f"{args.answer_prompt}.json"
    if out.exists() and not args.overwrite:
        print(f"REFUSED: {out} exists; pass --overwrite to replace it", file=sys.stderr)
        return 1
    try:
        commit = check_clean_tree(allowed_prefixes=ALLOWED_UNTRACKED)
    except DirtyWorkingTree as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    answer_template = load_prompt(args.answer_prompt)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="shopmate-phase6-") as tmp:
        try:
            harness, manifest, _ = build_harness(args, Path(tmp))
        except SystemExit as exc:
            print(exc, file=sys.stderr)
            return 1
        dev = load_split("dev")
        cases = load_cases().cases
        prices = (args.price_input_per_million, args.price_output_per_million) if args.price_input_per_million is not None and args.price_output_per_million is not None else None
        outcome = evaluate_phase6(dev, cases, harness, prices=prices, price_source=args.price_source)
        generator = harness.generator
    if not outcome["complete"]:
        progress = out.with_name(f"{args.answer_prompt}.progress.json")
        write_result(progress, {"complete": False, "stopped": outcome["stopped"], "done_items": [i["id"] for i in outcome["deterministic"]["items"]],
                                "done_cases": [c["id"] for c in outcome["deterministic"]["multiturn"]["cases"]],
                                "how_to_resume": "run the same command again after the quota reset (00:00 Pacific; 14:00 in Vietnam until 1 November); "
                                                 "everything finished is replayed from eval/llm_cache"}, overwrite=True)
        print(f"STOPPED before {outcome['stopped']['before']}: {outcome['stopped']['reason']}\nprogress: {progress}", file=sys.stderr)
        return 2
    vocabulary = catalog_vocabulary()
    deterministic = outcome["deterministic"]
    deterministic["provenance"] = {
        "git_commit": commit, "git_dirty": False, "model_id": MODEL_ID, "golden_items_read": "dev only",
        "answer_prompt": {"name": answer_template.name, "sha256": answer_template.sha256},
        "router_prompt_sha256": load_prompt("router_v1").sha256, "filters_prompt_sha256": load_prompt("filters_v1").sha256,
        "answer_schema_sha256": canonical_sha256(ANSWER_SCHEMA), "router_schema_sha256": canonical_sha256(ROUTER_SCHEMA),
        "filters_schema_sha256": canonical_sha256(build_schema(vocabulary)), "vocabulary_sha256": vocabulary.sha256(),
        "generation_configs": {"router": ROUTER_GENERATION_CONFIG, "filters": EXTRACTOR_GENERATION_CONFIG, "answer": ANSWER_GENERATION_CONFIG},
        "retrieval": {"mode": "dense_folded", "product_k": PRODUCT_K, "policy_k": POLICY_K, "index_manifest_sha256": _sha(REPO_ROOT / "data" / "index" / "manifest.json")},
        "agent": {"max_steps": MAX_STEPS, "bounds": BOUNDS, "langgraph": _version("langgraph")},
        "dev_split_sha256": items_hash(dev), "golden_file_sha256": golden_file_sha256(), "cases_sha256": _sha(case_module.CASES_PATH),
        "scoring_module_sha256": _sha(REPO_ROOT / "eval" / "phase6_scoring.py"),
    }
    document = {"deterministic": deterministic, "deterministic_sha256": canonical_sha256(deterministic),
                "measurements": {**outcome["measurements"], "live_calls_this_run": generator.inner.live_calls, "cache_hits_this_run": generator.inner.cache_hits,
                                 "wall_seconds": round(time.monotonic() - started, 1)}}
    write_result(out, document, args.overwrite)
    summary = deterministic["per_intent"]
    print(f"{out.name}: " + ", ".join(f"{k} {summary[k]['correct']}/{summary[k]['n']}" for k in ("product_search", "policy_question", "order_status", "create_ticket", "out_of_scope", "smalltalk"))
          + f"; multi-turn {deterministic['multiturn']['passed']}/{deterministic['multiturn']['total']}; fallbacks {deterministic['guardrails']['fallbacks']}; "
          f"live calls {generator.inner.live_calls}, cache hits {generator.inner.cache_hits}; sha256={document['deterministic_sha256'][:12]}")
    return 0


def _sha(path: Path) -> str:
    from app.retrieval.manifest import file_sha256

    return file_sha256(path)


def _version(package: str) -> str:
    from importlib.metadata import version

    return version(package)


if __name__ == "__main__":
    sys.exit(main())
