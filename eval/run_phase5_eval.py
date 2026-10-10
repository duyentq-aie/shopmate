"""Phase 5 evaluation on the DEV split: LLM routing and LLM filter extraction (Gemini free tier), measured.

What is measured (dev only; the test split is never read, `load_split("dev")` locks it):
- routing, all dev items: accuracy, macro-F1, confusion matrix, by query style, the out_of_scope items apart;
- filter extraction, product_search items: exact match of the filter set and per-field precision/recall, against the
  golden expected_filters WITHOUT `keyword` (R14);
- retrieval with dense_folded under GOLD routing: floor (doc_type only) vs LLM-extracted filters vs ceiling (oracle),
  paired bootstrap of extracted against both; no_answer items with extracted filters are a REAL measurement (under
  oracle the empty result is true by construction, Phase 3B);
- the full pipeline LLM route -> doc_type -> (extract filters) -> retrieve: a misrouted question is a miss; a no_answer
  item is correct only if it is routed to product_search AND the result is empty;
- per call latency (p50/p95) and tokens, from the responses recorded in eval/llm_cache (live latency, kept in the cache).

Free-tier guards: LLM calls go through a disk cache (eval/llm_cache/, committed): a re-run makes ZERO API calls. Live
calls need --live; without it a cache miss stops the run. Live runs are rate limited (12 requests/minute), capped per
run (--max-live-calls, default 300) and per day (450, counted across runs), and stop on a daily or limit-0 quota.

Usage (from the repo root; Qdrant running, index built):
    uv run python -m eval.run_phase5_eval                    # offline: needs a complete cache
    uv run python -m eval.run_phase5_eval --live             # fetch what is missing (counts against the daily quota)
    uv run python -m eval.run_phase5_eval --probes           # also route the reviewed adversarial probes
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

from app.config import REPO_ROOT, get_setting
from app.llm.cache import DiskCache
from app.llm.client import MODEL_ID, GeminiClient
from app.llm.errors import LLMError
from app.llm.extractor import EXTRACTOR_GENERATION_CONFIG, FilterExtractor, build_schema
from app.llm.generator import CachedGenerator
from app.llm.limits import DailyBudget, RateLimiter
from app.llm.prompts import PromptTemplate, catalog_vocabulary, load_prompt
from app.llm.router import ROUTER_GENERATION_CONFIG, ROUTER_SCHEMA, GeminiRouter
from eval.golden.loader import golden_file_sha256, items_hash, load_split
from eval.golden.schema import GoldenItem
from eval.llm_metrics import filter_field_metrics, normalized_expected, normalized_extracted, routing_metrics
from eval.probes.loader import PROBES_PATH, ProbeSet, load_probes, require_reviewed
from eval.retrieval_metrics import paired_bootstrap, percentile_ms
from eval.run_retrieval_eval import (
    DirtyWorkingTree,
    aggregate,
    build_filters,
    canonical_sha256,
    check_clean_tree,
    evaluate_item,
    gold_of,
    index_problems,
    mode_problems,
    resolve_results_dir,
    write_result,
)

MODE = "dense_folded"  # Phase 4's adopted retrieval, named explicitly here
TOP_K = 10
PAIRED_METRICS = ("rr", "hit@1", "hit@5", "hit@10")
CACHE_DIR = REPO_ROOT / "eval" / "llm_cache"
USAGE_DIR = REPO_ROOT / "data" / "llm_usage"
ALLOWED_UNTRACKED = ("eval/results/", "eval/llm_cache/")
DAILY_CEILING = 450  # of the 500 requests per day the free tier allows
RPM, TPM = 12, 200_000  # below the 15 RPM / 250K TPM of the free tier
EXTRACTION_NOTE = "real measurement: the filters come from the LLM, so an empty result is not true by construction"
ORACLE_NOTE = "tautology: the oracle filters are the very filters that define 'no match' (Phase 3B)"
NO_ANSWER_RULE = "routed to product_search AND an empty result"


def question_for(item: GoldenItem) -> str:
    """The question as a customer would type it: the {order_number} placeholder is replaced by the real order number
    of the item's gold order (deterministic; refused when the number is not known yet)."""
    if "{order_number}" not in item.question:
        return item.question
    number = getattr(item.gold_source, "order_number", "")
    if not str(number).isdigit():
        raise ValueError(f"{item.id}: cannot substitute {{order_number}}: the gold order number is {number!r}")
    return item.question.replace("{order_number}", str(number))


def _record(item: GoldenItem, filters: dict | None, retrieved: list[str], **extra) -> dict:
    return {"id": item.id, "intent": item.intent, "query_style": item.query_style, "author": item.author,
            "gold": gold_of(item), "filters": filters, "retrieved": retrieved,
            "metrics": evaluate_item(item, retrieved), **extra}


def _answerable(records: list[dict]) -> list[dict]:
    return [r for r in records if r["gold"]["type"] in ("skus", "policy_clause")]


def _paired(base: list[dict], other: list[dict]) -> dict:
    """other minus base, paired by item, for the answerable records."""
    a, b = {r["id"]: r for r in _answerable(base)}, {r["id"]: r for r in _answerable(other)}
    ids = sorted(a)
    return {m: paired_bootstrap([a[i]["metrics"][m] for i in ids], [b[i]["metrics"][m] for i in ids]) for m in PAIRED_METRICS}


def _calls(results: list) -> dict:
    usable = [r for r in results if r.usage is not None]
    latencies = [r.latency_ms / 1000 for r in results]
    return {
        "n": len(results), "cache_hits": sum(r.cached for r in results),
        "input_tokens": sum(r.usage.input_tokens for r in usable), "output_tokens": sum(r.usage.output_tokens for r in usable),
        "thought_tokens": sum(r.usage.thought_tokens for r in usable),
        "latency_ms": {"p50": percentile_ms(latencies, 0.5), "p95": percentile_ms(latencies, 0.95)},
        "model_versions": sorted({v for r in results if (v := r.raw_response.get("modelVersion"))}),
    }


def cost_per_1000_questions(calls: dict, extraction_share: float, price_input: float, price_output: float) -> dict:
    """Theoretical paid-tier cost of the full pipeline per 1,000 questions. `calls` is the `calls` section; prices are
    USD per million tokens; thought tokens are billed as output; `extraction_share` is the fraction of questions that
    reach the extractor (routed to product_search)."""
    def per_call(c: dict) -> float:
        if not c["n"]:
            return 0.0
        return (c["input_tokens"] * price_input + (c["output_tokens"] + c["thought_tokens"]) * price_output) / c["n"] / 1e6

    return {"router_usd": 1000 * per_call(calls["router"]), "extractor_usd": 1000 * extraction_share * per_call(calls["filters"]),
            "total_usd": 1000 * (per_call(calls["router"]) + extraction_share * per_call(calls["filters"]))}


def extraction_share(routing: dict) -> float:
    """Fraction of ALL routed questions that reach the extractor (routed to product_search): the router runs on every
    question, so the cost per 1,000 questions must weigh the extractor by this share."""
    return routing["predicted_counts"].get("product_search", 0) / routing["n"] if routing["n"] else 0.0


def evaluate_phase5(items: list[GoldenItem], router, extractor, search, *, probes: ProbeSet | None = None,
                    mode: str = MODE, k: int = TOP_K) -> dict:
    """`search(query, filters, k, mode) -> list of keys`. Returns the deterministic content of a result (no timestamps)."""
    questions = {item.id: question_for(item) for item in items}
    routes = {item.id: router.route(questions[item.id]) for item in items}

    # --- routing, all items ------------------------------------------------------------------------------------------
    routing = routing_metrics([{"id": i.id, "gold": i.intent, "pred": routes[i.id].intent, "query_style": i.query_style} for i in items])
    routing["unusable_answers"] = sum(routes[i.id].intent is None for i in items)
    predicted: dict[str, int] = {}
    for i in items:
        label = routes[i.id].intent or "unparseable"
        predicted[label] = predicted.get(label, 0) + 1
    routing["predicted_counts"] = predicted
    routing["out_of_scope_items"] = [{"id": i.id, "question": questions[i.id], "pred": routes[i.id].intent,
                                      "correct": routes[i.id].intent == i.intent} for i in items if i.intent == "out_of_scope"]
    routing["confidence"] = sorted({routes[i.id].confidence_note for i in items})

    # --- filter extraction under gold routing ----------------------------------------------------------------------------
    extractions: dict[str, object] = {}

    def extract(question: str):
        if question not in extractions:
            extractions[question] = extractor.extract(question)
        return extractions[question]

    products = [i for i in items if i.intent == "product_search"]
    policies = [i for i in items if i.intent == "policy_question"]
    extraction_items, pairs = [], []
    for item in products:
        result = extract(questions[item.id])
        expected, extracted = normalized_expected(item), (None if result.filters is None else normalized_extracted(result.filters))
        pairs.append((expected, extracted))
        extraction_items.append({"id": item.id, "question": questions[item.id], "expected": expected, "extracted": extracted,
                                 "exact": expected == (extracted or {}), "dropped": result.dropped,
                                 "ignored_fields": result.ignored_fields, "error": result.error})
    # in_stock is also reported apart: golden gs-019 is labeled in_stock True for a question that only asks about
    # availability, which the R32 rule does not filter on (docs/GOLDEN-V2-ISSUES.md #1). The golden set is not edited.
    extraction = {"metrics": filter_field_metrics(pairs),
                  "metrics_without_in_stock": filter_field_metrics(pairs, exclude_fields=("in_stock",)), "items": extraction_items}

    # --- retrieval under gold routing: floor / extracted / ceiling ----------------------------------------------------------
    def extracted_filters(question: str) -> dict:
        filters = extract(question).filters
        return {"doc_type": "product", **normalized_extracted(filters)} if filters is not None else {"doc_type": "product"}

    arms: dict[str, list[dict]] = {"floor": [], "extracted": [], "ceiling": []}
    for item in products:
        question = questions[item.id]
        for name, filters in (("floor", build_filters(item, "routing")), ("extracted", extracted_filters(question)),
                              ("ceiling", build_filters(item, "oracle"))):
            arms[name].append(_record(item, filters, search(question, filters, k, mode)))

    def correct_empty(records: list[dict]) -> float | None:
        values = [r["metrics"]["correct_empty"] for r in records if r["gold"]["type"] == "no_answer"]
        return sum(values) / len(values) if values else None

    n_no_answer = sum(1 for i in products if i.gold_source.type == "no_answer")
    retrieval = {
        "mode": mode, "k": k,
        "arms": {name: {"aggregate": aggregate(records), "records": records} for name, records in arms.items()},
        "paired": {"extracted_vs_floor": _paired(arms["floor"], arms["extracted"]),
                   "extracted_vs_ceiling": _paired(arms["ceiling"], arms["extracted"])},
        "no_answer": {"n": n_no_answer,
                      "floor": {"correct_empty": correct_empty(arms["floor"])},
                      "extracted": {"correct_empty": correct_empty(arms["extracted"]), "note": EXTRACTION_NOTE},
                      "ceiling": {"correct_empty": correct_empty(arms["ceiling"]), "note": ORACLE_NOTE}},
        "extraction_failures": sum(1 for i in products if extract(questions[i.id]).filters is None),
    }

    # --- the full pipeline over the retrieval items ----------------------------------------------------------------------------
    policy_gold = [_record(i, build_filters(i, "routing"), search(questions[i.id], build_filters(i, "routing"), k, mode))
                   for i in policies]
    pipeline_records = []
    for item in [*products, *policies]:
        question, predicted = questions[item.id], routes[item.id].intent
        if predicted == "product_search":
            filters = extracted_filters(question)
        elif predicted == "policy_question":
            filters = {"doc_type": "policy"}
        else:
            filters = None  # no retrieval for this route
        retrieved = search(question, filters, k, mode) if filters is not None else []
        pipeline_records.append(_record(item, filters, retrieved, routed_intent=predicted))
    answerable = _answerable(pipeline_records)
    no_answer_records = [r for r in pipeline_records if r["gold"]["type"] == "no_answer"]
    correct = sum(1 for r in no_answer_records if r["routed_intent"] == "product_search" and not r["retrieved"])
    by_intent = {intent: [r for r in pipeline_records if r["intent"] == intent] for intent in ("product_search", "policy_question")}
    pipeline = {
        "aggregate": aggregate(pipeline_records), "records": pipeline_records, "n_answerable": len(answerable),
        "misrouted_answerable": sum(1 for r in answerable if r["routed_intent"] != r["intent"]),
        "no_answer": {"n": len(no_answer_records), "correct": correct,
                      "rate": correct / len(no_answer_records) if no_answer_records else None, "rule": NO_ANSWER_RULE},
        "paired_vs_gold_routing": {"product_search": _paired(arms["extracted"], by_intent["product_search"]),
                                   "policy_question": _paired(policy_gold, by_intent["policy_question"])},
    }

    # --- calls ---------------------------------------------------------------------------------------------------------------
    calls = {"router": _calls(list(routes.values())), "filters": _calls(list(extractions.values()))}
    result = {"routing": routing, "extraction": extraction, "retrieval_gold_routing": retrieval, "pipeline": pipeline, "calls": calls}

    if probes is not None:
        result["probes"] = probe_section(router, probes)
    return result


def probe_section(router, probes: ProbeSet) -> dict:
    """Route the (reviewed) adversarial probes. They are not golden items and enter no golden metric."""
    probe_results = []
    for probe in probes.items:
        routed = router.route(probe.question)
        probe_results.append({"id": probe.id, "attack": probe.attack, "question": probe.question,
                              "expected": probe.expected_intent, "pred": routed.intent,
                              "correct": routed.intent == probe.expected_intent, "error": routed.error})
    return {"n": len(probe_results), "correct": sum(p["correct"] for p in probe_results),
            "security_note": probes.security_note, "labeling_rule": probes.labeling_rule, "items": probe_results}


def prepare_probes(path: Path = PROBES_PATH) -> ProbeSet:
    probes = load_probes(path)
    require_reviewed(probes)  # raises ProbesNotReviewed while the set is a draft
    return probes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--router-prompt", default="router_v1")
    parser.add_argument("--filters-prompt", default="filters_v1")
    parser.add_argument("--live", action="store_true", help="allow live API calls for cache misses (default: offline)")
    parser.add_argument("--max-live-calls", type=int, default=300, help="hard cap on live requests in this run")
    parser.add_argument("--probes", action="store_true", help="also route the adversarial probes (they must be reviewed)")
    parser.add_argument("--results-dir", default="eval/results/phase5")
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
    out = results_dir / f"{args.router_prompt}+{args.filters_prompt}.json"
    if out.exists() and not args.overwrite:
        print(f"REFUSED: {out} exists; pass --overwrite to replace it", file=sys.stderr)
        return 1
    try:
        commit = check_clean_tree(allowed_prefixes=ALLOWED_UNTRACKED)
    except DirtyWorkingTree as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    try:
        probes = prepare_probes() if args.probes else None
    except Exception as exc:  # ProbesNotReviewed, or an invalid probe file
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    from app.retrieval.embedder import BgeM3Embedder, EmbeddingConfig
    from app.retrieval.lexical import LexicalConfig
    from app.retrieval.manifest import MANIFEST_PATH, file_sha256, load_manifest
    from app.retrieval.search import Retriever
    from app.retrieval.store import connect

    manifest = load_manifest()
    problems = mode_problems(MODE, manifest)
    client = connect()
    problems += index_problems(client, manifest, EmbeddingConfig(), query_lexical=LexicalConfig())
    if problems:
        print("REFUSED: the live index does not match data/index/manifest.json:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1

    llm = None
    if args.live:
        key = get_setting("GEMINI_API_KEY")
        if not key:
            print("REFUSED: --live needs GEMINI_API_KEY in .env or the environment", file=sys.stderr)
            return 1
        llm = GeminiClient(key, limiter=RateLimiter(rpm=RPM, tpm=TPM), budget=DailyBudget(USAGE_DIR, limit=DAILY_CEILING),
                           max_live_requests=args.max_live_calls)
    generator = CachedGenerator(DiskCache(CACHE_DIR), llm)
    router_template, filters_template = load_prompt(args.router_prompt), load_prompt(args.filters_prompt)
    router, extractor = GeminiRouter(generator, router_template), FilterExtractor(generator, filters_template)

    dev = load_split("dev")
    retriever = Retriever(client, BgeM3Embedder(), collection=manifest["collection"])

    def search(query, filters, k, mode):
        return [hit.key for hit in retriever.search(query, filters, k=k, mode=mode)]

    started = time.monotonic()
    try:
        result = evaluate_phase5(dev, router, extractor, search, probes=probes)
    except LLMError as exc:
        print(f"STOPPED: {type(exc).__name__}: {exc}\nEverything fetched so far is in eval/llm_cache/ (re-run to continue).",
              file=sys.stderr)
        return 2

    vocabulary = catalog_vocabulary()
    result["provenance"] = {
        "git_commit": commit, "git_dirty": False, "model_id": MODEL_ID,
        "model_versions": sorted({v for c in result["calls"].values() for v in c["model_versions"]}),
        "router_prompt": {"name": router_template.name, "sha256": router_template.sha256},
        "filters_prompt": {"name": filters_template.name, "sha256": filters_template.sha256},
        "vocabulary_sha256": vocabulary.sha256(),
        "router_schema_sha256": canonical_sha256(ROUTER_SCHEMA), "filters_schema_sha256": canonical_sha256(build_schema(vocabulary)),
        "router_generation_config": ROUTER_GENERATION_CONFIG, "filters_generation_config": EXTRACTOR_GENERATION_CONFIG,
        "retrieval": {"mode": MODE, "k": TOP_K, "index_manifest_sha256": file_sha256(MANIFEST_PATH)},
        "dev_split_sha256": items_hash(dev), "golden_file_sha256": golden_file_sha256(),
        "probes_sha256": file_sha256(PROBES_PATH) if probes else None,
    }
    if args.price_input_per_million is not None and args.price_output_per_million is not None:
        share = extraction_share(result["routing"])
        result["cost_theoretical_paid_tier"] = {
            "prices_usd_per_million_tokens": {"input": args.price_input_per_million, "output": args.price_output_per_million},
            "source": args.price_source, "extraction_share": share,
            "per_1000_questions": cost_per_1000_questions(result["calls"], share, args.price_input_per_million, args.price_output_per_million),
            "free_tier_cost_usd": 0}
    document = {"deterministic": result, "deterministic_sha256": canonical_sha256(result),
                "measurements": {"live_calls_this_run": generator.live_calls, "cache_hits_this_run": generator.cache_hits,
                                 "wall_seconds": round(time.monotonic() - started, 1)}}
    write_result(out, document, args.overwrite)
    routing = result["routing"]
    print(f"{out.name}: routing accuracy {routing['accuracy']:.3f}, macro-F1 {routing['macro_f1']:.3f}; extraction exact "
          f"{result['extraction']['metrics']['exact_match']}/{result['extraction']['metrics']['n']}; live calls {generator.live_calls}, "
          f"cache hits {generator.cache_hits}; sha256={document['deterministic_sha256'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
