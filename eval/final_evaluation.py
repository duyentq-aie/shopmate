"""Phase 8: the final evaluation, pre-registered in docs/PHASE8-PREREG.md. One command measures the whole system on a split and reports it twice.

    uv run python -m eval.final_evaluation --split dev --rehearsal                 # the rehearsal on dev: the same code path, nothing is opened that was not open
    uv run python -m eval.final_evaluation --split test --live --freeze-ref v1.0-frozen --dev-run-sha SHA   # the real thing, once (turn 2 of Phase 8)

What it does (one pass, one set of cached LLM calls):
- Phase 5 core on the split: the router on every item, the filter extractor on every product item, and the full pipeline LLM route -> doc_type -> extracted filters ->
  retrieval (a misrouted question is a miss; a no_answer item is correct only if it is routed to product_search AND the result is empty);
- Phase 6 core on the same items: the agent end to end, one first turn per item, scored by eval/phase6_scoring.py, with the guardrail, the tokens and the latency of each turn.
  The scripted multi-turn cases are not golden items and are not part of this run.
- the metrics below, each with its registered interval, for ALL items and for all items EXCEPT the one whose question hash is pre-registered (A0, docs/DECISIONS-phase6.md);
- a registered secondary analysis: the proportions by author (human / agent_draft) and by intent, descriptive, on all items.

The metrics (per item, then a proportion, a mean or a percentile over items):
  router_accuracy          the router named the gold intent                                      (every item)                           proportion
  extractor_exact_match    the extracted filters equal the expected ones, without `keyword`      (product_search items)                 proportion
  product_mrr              reciprocal rank of the first gold SKU, full pipeline                  (product items with SKU gold)         mean
  policy_mrr               reciprocal rank of the gold clause, full pipeline                     (policy items)                         mean
  no_answer_correct        routed to product_search and nothing returned                         (product items with no matching product)  proportion
  guardrail_fallback_rate  the guardrail replaced the written answer with its fixed text          (items for which the LLM wrote an answer) proportion
  answer_correct           the Phase 6 score of the item (a primary metric: every earlier dev report has it)  (every item)            proportion
  latency_ms p50 / p95     of one first turn: recorded LLM latencies plus retrieval, lookup and reply time                          percentile
  cost_per_1000_usd        theoretical paid-tier cost of 1,000 such conversations (the free tier costs 0)                           mean x 1000
  product_mrr_gold_routing the same as product_mrr with the intent given, so it can be compared with the Phase 5 number (secondary)  mean
Intervals, as registered: a proportion (router_accuracy, extractor_exact_match, no_answer_correct, answer_correct, guardrail_fallback_rate) has the Wilson score interval, 95%.
The MRRs, the latency percentiles and the cost have a percentile bootstrap over items: 95%, 10,000 resamples, seed 20261009 (eval/retrieval_metrics.py).

Guards (the test split is opened only here, only with `--split test`, and only under all of these):
- a clean working tree (nothing but new files under eval/results/ and eval/llm_cache/), a result file that does not exist yet (no overwrite exists for the test split),
  at most 3 attempts in eval/results/phase8_test/attempts.jsonl and none after a failure that is not an infrastructure error (docs/PHASE8-PREREG.md, "Rerun policy");
- `--freeze-ref v1.0-frozen`: the tag resolves to a commit and HEAD is that commit, or descends from it with nothing changed that can change a result or the pre-registration
  (app/, prompts/, data/, eval/ except its results and cache, pyproject.toml, uv.lock, docs/PHASE8-PREREG.md);
  `--dev-run-sha`: nothing that can change a result differs between the commit of the official dev run and HEAD;
- the state outside the repository is the registered one (eval/external_state.py, docs/PHASE8-PREREG.md section 1): the fingerprint of the Qdrant collection and the one of the seed orders in Saleor
  are recomputed, read only, and must equal the registered values; otherwise it refuses, prints which part differs and writes a "refusal" entry (not an attempt) to attempts.jsonl.
The item whose question hash is pre-registered is matched here, by hashing every question; its id is never printed or stored in the summary.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from app.config import REPO_ROOT
from app.llm.errors import LLMError
from app.llm.extractor import EXTRACTOR_GENERATION_CONFIG, FilterExtractor, build_schema
from app.llm.prompts import catalog_vocabulary, load_prompt
from app.llm.client import MODEL_ID
from app.llm.router import ROUTER_GENERATION_CONFIG, ROUTER_SCHEMA, GeminiRouter
from app.tracing import classify_exception
from eval import external_state
from eval.golden.loader import golden_file_sha256, items_hash, load_split
from eval.retrieval_metrics import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, CONFIDENCE, _percentile, bootstrap_ci
from eval.run_phase5_eval import ALLOWED_UNTRACKED, MODE, TOP_K, evaluate_phase5, question_for
from eval.run_phase6_eval import _sha, _version, build_harness, evaluate_phase6
from eval.run_retrieval_eval import MIN_INTERPRETABLE_N, DirtyWorkingTree, blocking_paths, canonical_sha256, check_clean_tree, resolve_results_dir, write_result

# --- what is pre-registered ----------------------------------------------------------------------------------------------------------------------------

EXCLUDED_TEST_QUESTION_SHA256 = "3b9975a677521f1dda7bac87ffa7098bc3d748f5724545ee185947a1e86d8692"  # A0, docs/DECISIONS-phase6.md: SHA-256 of the exact UTF-8 `question` text
PRICES = (0.30, 2.50)  # USD per million tokens, input and output: the prices of every earlier report
PRICE_SOURCE = ("https://ai.google.dev/gemini-api/docs/pricing, Gemini 3.5 Flash-Lite, paid tier, Standard; 'Last updated 2026-10-09 UTC'; read 2026-10-10")
ANSWER_PROMPT = "answer_v2"
RESULT_NAME = "final_evaluation"
MAX_TEST_ATTEMPTS = 3  # the first run and at most two reruns
RERUN_ELIGIBLE = frozenset({"llm_429_daily", "llm_rate_limited", "llm_unavailable", "budget_cap_reached", "qdrant_unreachable", "saleor_timeout", "saleor_error",
                            "request_interrupted"})  # infrastructure only: the run produced no result because a service or the quota was not there
# What can change a result: the code, the prompts, the data the index and the answers are made from, the dependencies and the evaluation code itself.
CODE_PATHS = ("app", "prompts", "data/index", "data/seed", "data/policies", "eval", "pyproject.toml", "uv.lock")
CODE_EXCLUDED = (":(exclude)eval/results", ":(exclude)eval/llm_cache", ":(exclude)eval/final_evaluation.py", ":(exclude)eval/external_state.py")  # the dev run is older than the script and the state fingerprints: neither can change a result; both are frozen by the tag
FREEZE_TAG = "v1.0-frozen"  # the freeze is this tag (created after the freeze commit's CI is green); nothing else can stand for it
FROZEN_PATHS = (*CODE_PATHS, "docs/PHASE8-PREREG.md")  # between the tag and HEAD nothing here may differ: not the code, not the script, not the registration
FROZEN_EXCLUDED = (":(exclude)eval/results", ":(exclude)eval/llm_cache")
INTENT_ORDER = ("product_search", "policy_question", "order_status", "create_ticket", "out_of_scope", "smalltalk")
AUTHOR_ORDER = ("human", "agent_draft")
SECONDARY_PROPORTIONS = ("router_accuracy", "answer_correct", "extractor_exact_match", "no_answer_correct", "guardrail_fallback_rate")
SECONDARY_MEANS = ("product_mrr", "policy_mrr")

METRICS = (  # name, kind, population of items, label
    ("router_accuracy", "proportion", "every item"),
    ("extractor_exact_match", "proportion", "product_search items"),
    ("product_mrr", "mean", "product items with SKU gold"),
    ("policy_mrr", "mean", "policy items"),
    ("no_answer_correct", "proportion", "product items with no matching product"),
    ("guardrail_fallback_rate", "proportion", "items with an LLM-written answer"),
    ("latency_p50_ms", "p50", "every item (one first turn each)"),
    ("latency_p95_ms", "p95", "every item (one first turn each)"),
    ("cost_per_1000_usd", "cost", "every item"),
    ("answer_correct", "proportion", "every item"),
    ("product_mrr_gold_routing", "mean", "product items with SKU gold, intent given"),
)


# --- statistics ----------------------------------------------------------------------------------------------------------------------------------------


def percentile_ci(values: list[float], q: float, resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED) -> dict:
    """The q-percentile of `values` (same index rule as eval.retrieval_metrics.percentile_ms) with a percentile-bootstrap interval over the items."""
    if not values:
        return {"n": 0, "estimate": None, "ci_low": None, "ci_high": None}
    rng, n = random.Random(seed), len(values)
    stats = sorted(_percentile(sorted(values[rng.randrange(n)] for _ in range(n)), q) for _ in range(resamples))
    alpha = (1 - CONFIDENCE) / 2
    return {"n": n, "estimate": _percentile(sorted(values), q), "ci_low": _percentile(stats, alpha), "ci_high": _percentile(stats, 1 - alpha)}


def wilson(successes: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    """The Wilson score interval of a proportion (95% by default): the registered interval of every yes/no metric. A percentile bootstrap would collapse to a point
    when every item succeeded (71 of 71 gives [1, 1]), which says nothing about how little 71 items can show; this one does not."""
    if n == 0:
        return None
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return round(max(0.0, centre - half), 12), round(min(1.0, centre + half), 12)


def summarise(kind: str, values: list[float]) -> dict:
    """One metric over the items that have it. kind: "proportion" (Wilson), "mean" and "cost" (percentile bootstrap, cost x 1000), "p50" / "p95" (percentile bootstrap)."""
    if kind in ("p50", "p95"):
        out = percentile_ci(values, 0.5 if kind == "p50" else 0.95)
        return {**{key: (round(v, 1) if isinstance(v, float) else v) for key, v in out.items()}, "interval": "bootstrap"}
    if kind == "proportion":
        successes = int(round(sum(values)))
        interval = wilson(successes, len(values))
        return {"n": len(values), "successes": successes, "estimate": successes / len(values) if values else None,
                "ci_low": interval[0] if interval else None, "ci_high": interval[1] if interval else None, "interval": "wilson"}
    ci = bootstrap_ci(values)
    scale = 1000 if kind == "cost" else 1
    return {"n": ci["n"], "estimate": None if ci["mean"] is None else ci["mean"] * scale,
            "ci_low": None if ci["ci_low"] is None else ci["ci_low"] * scale, "ci_high": None if ci["ci_high"] is None else ci["ci_high"] * scale, "interval": "bootstrap"}


def populations(values: dict[str, dict[str, float]], excluded: set[str]) -> dict:
    """The metrics for all items and for all items except `excluded` (ids). Both come from the same per-item values and the same interval settings."""
    result = {}
    for name, ids in (("all", set()), ("without_excluded", excluded)):
        result[name] = {metric: summarise(kind, [v for item_id, v in sorted(values[metric].items()) if item_id not in ids and v is not None])
                        for metric, kind, _ in METRICS}
    return result


def groups_of(items) -> dict[str, dict[str, list[str]]]:
    """The two registered groupings of the items: by author (human / agent_draft) and by intent, as lists of item ids."""
    by_author = {a: [i.id for i in items if i.author == a] for a in AUTHOR_ORDER}
    by_author.update({a: [i.id for i in items if i.author == a] for a in sorted({i.author for i in items} - set(AUTHOR_ORDER))})
    by_intent = {k: [i.id for i in items if i.intent == k] for k in INTENT_ORDER}
    return {"by_author": {k: v for k, v in by_author.items() if v}, "by_intent": {k: v for k, v in by_intent.items() if v}}


def breakdown(values: dict[str, dict[str, float]], groups: dict[str, list[str]]) -> dict:
    """The registered secondary analysis, for ALL items (never for the population without the excluded item: the difference between the two tables would say which group
    that item belongs to). Descriptive: the proportions with the Wilson interval, the MRRs as a mean, and a flag on every group too small to read."""
    out = {}
    for group, ids in groups.items():
        chosen = set(ids)

        def of(metric: str) -> list[float]:
            return [v for item_id, v in sorted(values[metric].items()) if item_id in chosen and v is not None]

        means = {metric: {"n": len(of(metric)), "estimate": (sum(of(metric)) / len(of(metric))) if of(metric) else None} for metric in SECONDARY_MEANS}
        out[group] = {"n_items": len(chosen), "too_small_to_interpret": len(chosen) < MIN_INTERPRETABLE_N,
                      "proportions": {metric: summarise("proportion", of(metric)) for metric in SECONDARY_PROPORTIONS}, "means": means}
    return out


# --- per-item values from the two evaluations ------------------------------------------------------------------------------------------------------------


def question_sha256(item) -> str:
    """The fingerprint of A0: the exact UTF-8 `question` as stored (the {order_number} placeholder literal, no normalisation)."""
    return hashlib.sha256(item.question.encode("utf-8")).hexdigest()


def per_item_values(items, r5: dict, r6: dict, turn_records: list[dict], prices: tuple[float, float]) -> tuple[dict[str, dict[str, float]], list[dict]]:
    """-> ({metric: {item id: value}}, the per-item table). Latency and cost come from the first turn of each item."""
    values: dict[str, dict[str, float]] = {name: {} for name, _, _ in METRICS}
    wrong_route = set(r5["routing"]["errors"])
    for item in items:
        values["router_accuracy"][item.id] = 0.0 if item.id in wrong_route else 1.0
    for entry in r5["extraction"]["items"]:
        values["extractor_exact_match"][entry["id"]] = 1.0 if entry["exact"] else 0.0
    for record in r5["pipeline"]["records"]:
        kind = record["gold"]["type"]
        if kind == "skus":
            values["product_mrr"][record["id"]] = record["metrics"]["rr"]
        elif kind == "policy_clause":
            values["policy_mrr"][record["id"]] = record["metrics"]["rr"]
        elif kind == "no_answer":
            values["no_answer_correct"][record["id"]] = 1.0 if record["routed_intent"] == "product_search" and not record["retrieved"] else 0.0
    for record in r5["retrieval_gold_routing"]["arms"]["extracted"]["records"]:
        if record["gold"]["type"] == "skus":
            values["product_mrr_gold_routing"][record["id"]] = record["metrics"]["rr"]
    by_conversation = {r["conversation"]: r for r in turn_records if r["role"] == "dev"}
    author = {item.id: item.author for item in items}
    table = []
    for result in r6["deterministic"]["items"]:
        turn = by_conversation[f"dev-{result['id']}"]
        answers = sum(1 for call in turn["calls"] if call["component"] == "answer")
        cost = sum((c["input"] * prices[0] + (c["output"] + c["thought"]) * prices[1]) / 1e6 for c in turn["calls"])
        latency = sum(c["latency_ms"] for c in turn["calls"]) + turn["non_llm_ms"]
        values["answer_correct"][result["id"]] = 1.0 if result["correct"] else 0.0
        if answers:
            values["guardrail_fallback_rate"][result["id"]] = 1.0 if result["fallback"] else 0.0
        values["latency_p50_ms"][result["id"]] = latency
        values["latency_p95_ms"][result["id"]] = latency
        values["cost_per_1000_usd"][result["id"]] = cost
        table.append({"id": result["id"], "intent": result["intent"], "author": author[result["id"]], "routed_to": result["routed_to"], "correct": result["correct"], "check": result["check"],
                      "fallback": result["fallback"], "declined": result["declined"], "answers_written": answers, "llm_calls": result["llm_calls"], "cost_usd": cost})
    return values, table


def cross_checks(summary_all: dict, r6: dict) -> dict:
    """What the two evaluations must agree on; a False here means the script, not the system, is wrong."""
    deterministic = r6["deterministic"]
    out = {"answer_correct_matches_phase6": summary_all["answer_correct"]["successes"] == deterministic["per_intent"]["all"]["correct"]}
    paid = deterministic.get("cost_theoretical_paid_tier")
    if paid is not None and summary_all["cost_per_1000_usd"]["estimate"] is not None:
        out["cost_matches_phase6"] = abs(summary_all["cost_per_1000_usd"]["estimate"] - paid["per_1000_conversations_usd"]) < 1e-9
    return out


# --- the guards --------------------------------------------------------------------------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def resolve_freeze_tag(repo: Path, ref: str) -> str | None:
    """The commit a tag points to (an annotated tag is followed); None if `ref` is not a tag of this repository. A branch name or a bare SHA is not a freeze."""
    name = ref if ref.startswith("refs/tags/") else f"refs/tags/{ref}"
    found = _git(repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
    return found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else None


def frozen_problems(repo: Path, freeze_ref: str | None, dev_run_sha: str | None) -> list[str]:
    """Why the tree is not the frozen one; [] when it is. The freeze is a tag: HEAD is its commit, or descends from it with nothing changed that can change a result or the registration."""
    if not freeze_ref or not dev_run_sha:
        return ["--freeze-ref and --dev-run-sha are required"]
    problems = []
    freeze = resolve_freeze_tag(repo, freeze_ref)
    if freeze is None:
        return [f"--freeze-ref {freeze_ref} is not a tag of this repository (the tag is made after the freeze commit's CI is green: docs/PHASE8-COMMITS.md)"]
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if head != freeze:
        if _git(repo, "merge-base", "--is-ancestor", freeze, head).returncode != 0:
            problems.append(f"HEAD {head[:12]} is not the commit of {freeze_ref} ({freeze[:12]}) and does not descend from it")
        else:
            changed = _git(repo, "diff", "--name-only", freeze, head, "--", *FROZEN_PATHS, *FROZEN_EXCLUDED).stdout.split()
            if changed:
                problems.append(f"files that can change a result, or the pre-registration, differ between {freeze_ref} and HEAD: " + ", ".join(changed[:8]))
    dev = _git(repo, "rev-parse", "--verify", f"{dev_run_sha}^{{commit}}")
    if dev.returncode != 0:
        problems.append(f"--dev-run-sha {dev_run_sha} is not a commit of this repository")
    else:
        changed = _git(repo, "diff", "--name-only", dev.stdout.strip(), "HEAD", "--", *CODE_PATHS, *CODE_EXCLUDED).stdout.split()
        if changed:
            problems.append("files that can change a result differ between the official dev run and HEAD: " + ", ".join(changed[:8]))
    return problems


def attempts_problems(results_dir: Path) -> list[str]:
    path = results_dir / "attempts.jsonl"
    if not path.is_file():
        return []
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    attempts = [e for e in entries if e.get("kind", "run") == "run"]  # a refusal of a guard ("refusal") opened nothing: it is not an attempt
    problems = []
    if len(attempts) >= MAX_TEST_ATTEMPTS:
        problems.append(f"{len(attempts)} attempts are already recorded; the policy allows the first run and at most two reruns")
    if attempts and not attempts[-1].get("rerun_allowed", False):
        problems.append(f"the last attempt ended with {attempts[-1].get('ended')!r}, which is not an infrastructure error: it is a finding, not a reason to run again")
    return problems


def log_attempt(results_dir: Path, **fields) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    with (results_dir / "attempts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **fields}, ensure_ascii=False) + "\n")


# --- the run ---------------------------------------------------------------------------------------------------------------------------------------------------


def evaluate_final(items, harness, router, extractor, search, prices: tuple[float, float] = PRICES, price_source: str = PRICE_SOURCE) -> dict:
    """Run both evaluations on `items`; -> {"complete", "stopped", "r5", "r6", "values", "table"}. Raises LLMError for what stops the Phase 5 core (a quota, a cache miss)."""
    r5 = evaluate_phase5(items, router, extractor, search)
    r6 = evaluate_phase6(items, [], harness, prices=prices, price_source=price_source)
    if not r6["complete"]:
        return {"complete": False, "stopped": r6["stopped"], "r5": r5, "r6": r6}
    values, table = per_item_values(items, r5, r6, harness.turn_records, prices)
    return {"complete": True, "stopped": None, "r5": r5, "r6": r6, "values": values, "table": table}


def build_document(items, outcome: dict, excluded: set[str], provenance: dict, extra_measurements: dict) -> dict:
    r5, r6 = outcome["r5"], outcome["r6"]
    summary = populations(outcome["values"], excluded)
    latency = {pop: {name: summary[pop][name] for name in ("latency_p50_ms", "latency_p95_ms")} for pop in summary}
    deterministic = {
        "kind": "final_evaluation", "split": provenance["split"], "n_items": len(items), "excluded_matched": len(excluded),
        "intervals": {"proportions": {"interval": "wilson", "confidence": CONFIDENCE}, "mrr_latency_cost": {"interval": "percentile bootstrap", "confidence": CONFIDENCE,
                                                                                                          "resamples": BOOTSTRAP_RESAMPLES, "seed": BOOTSTRAP_SEED}},
        "metrics": {pop: {m: v for m, v in metrics.items() if not m.startswith("latency_")} for pop, metrics in summary.items()},
        "secondary": {"description": "registered, descriptive, all items only; proportions with the Wilson 95% interval, MRRs as means; groups of fewer than "
                                     f"{MIN_INTERPRETABLE_N} items are flagged too small to interpret",
                      **{name: breakdown(outcome["values"], groups) for name, groups in groups_of(items).items()}},
        "per_item": outcome["table"], "cross_checks": cross_checks(summary["all"], r6),
        "phase5": {"routing": {k: r5["routing"][k] for k in ("n", "accuracy", "macro_f1", "errors", "confusion")},
                   "extraction": {"metrics": r5["extraction"]["metrics"], "metrics_without_in_stock": r5["extraction"]["metrics_without_in_stock"]},
                   "no_answer": r5["pipeline"]["no_answer"], "pipeline_counts": r5["pipeline"]["aggregate"]["counts"]},
        "phase6": {"per_intent": r6["deterministic"]["per_intent"], "guardrails": r6["deterministic"]["guardrails"], "bounds": r6["deterministic"]["bounds"],
                   "calls": r6["deterministic"]["calls"], "cost_theoretical_paid_tier": r6["deterministic"].get("cost_theoretical_paid_tier")},
        "provenance": provenance,
    }
    measurements = {"latency_ms": latency, **extra_measurements}
    return {"deterministic": deterministic, "deterministic_sha256": canonical_sha256(deterministic), "measurements": measurements}


def _proportion_cell(entry: dict) -> str:
    if not entry["n"]:
        return "–"
    return f"{entry['successes']}/{entry['n']} = {entry['estimate']:.3f} [{entry['ci_low']:.3f}, {entry['ci_high']:.3f}]"


def render_report(document: dict) -> str:
    d, m = document["deterministic"], document["measurements"]
    prov, boot = d["provenance"], d["intervals"]["mrr_latency_cost"]
    lines = [f"# Final evaluation: split {d['split']}, {d['n_items']} items" + (" (REHEARSAL: not a result)" if prov.get("rehearsal") else ""), "",
             f"commit `{prov['git_commit'][:12]}`{' (working tree NOT clean)' if prov.get('git_dirty') else ''}; deterministic_sha256 `{document['deterministic_sha256'][:16]}`.", "",
             (f"Two figures per metric: ALL items, and all items except the one whose question hash is pre-registered (matched {d['excluded_matched']} of 1)." if not prov.get("rehearsal")
              else "Two figures per metric: ALL items, and all items except a stand-in item (a rehearsal of the second figure)."), "",
             "Intervals, as registered: **Wilson 95%** for every proportion (router_accuracy, extractor_exact_match, no_answer_correct, answer_correct, guardrail_fallback_rate); "
             f"**percentile bootstrap over items** ({int(boot['confidence'] * 100)}%, {boot['resamples']:,} resamples, seed {boot['seed']}) for the MRRs, p50 / p95 and the cost.", "",
             "| metric | population | all items | 95% interval | without the excluded item | 95% interval |", "|---|---|---|---|---|---|"]

    def cell(entry: dict, kind: str) -> tuple[str, str]:
        if entry["estimate"] is None:
            return "n/a", ""
        digits = 1 if kind in ("p50", "p95") else 4 if kind == "cost" else 3
        extra = f" ({entry['successes']}/{entry['n']})" if kind == "proportion" else f" (n={entry['n']})"
        return f"{entry['estimate']:.{digits}f}{extra}", f"[{entry['ci_low']:.{digits}f}, {entry['ci_high']:.{digits}f}]"

    for name, kind, label in METRICS:
        pair = []
        for pop in ("all", "without_excluded"):
            pair.extend(cell(m["latency_ms"][pop][name] if name.startswith("latency_") else d["metrics"][pop][name], kind))
        lines.append(f"| {name} | {label} | " + " | ".join(pair) + " |")
    lines += ["", "## Registered secondary analysis: by author and by intent (descriptive, all items)", "",
              f"Proportions as successes/n = estimate [Wilson 95%]; MRRs as a mean with n. **The samples are small**: a group of fewer than {MIN_INTERPRETABLE_N} items is marked `(small)` and "
              "should not be read as a result; nothing here is a test of a difference between groups, and none is claimed.", ""]
    for title, key in (("By author", "by_author"), ("By intent", "by_intent")):
        lines += [f"### {title}", "", "| group | items | " + " | ".join(SECONDARY_PROPORTIONS) + " | " + " | ".join(SECONDARY_MEANS) + " |",
                  "|---|---|" + "---|" * (len(SECONDARY_PROPORTIONS) + len(SECONDARY_MEANS))]
        for group, entry in d["secondary"][key].items():
            cells = [_proportion_cell(entry["proportions"][metric]) for metric in SECONDARY_PROPORTIONS]
            cells += [f"{entry['means'][metric]['estimate']:.3f} (n={entry['means'][metric]['n']})" if entry["means"][metric]["n"] else "–" for metric in SECONDARY_MEANS]
            lines.append(f"| {group}{' (small)' if entry['too_small_to_interpret'] else ''} | {entry['n_items']} | " + " | ".join(cells) + " |")
        lines.append("")
    lines += [f"Live Gemini requests this run: {m.get('live_calls_this_run')}; answered from the cache: {m.get('cache_hits_this_run')}.",
              "No pass/fail threshold exists: these are the numbers, with their intervals."]
    return "\n".join(lines) + "\n"


def provenance(args, commit: str, dirty: list[str], items, rehearsal: bool, freeze: dict, dev_run_sha: str | None, answer_prompt) -> dict:
    vocabulary = catalog_vocabulary()
    return {
        "split": args.split, "rehearsal": rehearsal, "git_commit": commit, "git_dirty": bool(dirty), "dirty_paths": dirty[:20], "freeze": freeze, "dev_run_sha": dev_run_sha,
        "model_id": MODEL_ID, "items_sha256": items_hash(items), "golden_file_sha256": golden_file_sha256(), "n_items": len(items),
        "answer_prompt": {"name": answer_prompt.name, "sha256": answer_prompt.sha256}, "router_prompt_sha256": load_prompt("router_v1").sha256,
        "filters_prompt_sha256": load_prompt("filters_v1").sha256, "router_schema_sha256": canonical_sha256(ROUTER_SCHEMA),
        "filters_schema_sha256": canonical_sha256(build_schema(vocabulary)), "vocabulary_sha256": vocabulary.sha256(),
        "generation_configs": {"router": ROUTER_GENERATION_CONFIG, "filters": EXTRACTOR_GENERATION_CONFIG},
        "retrieval": {"mode": MODE, "k": TOP_K, "index_manifest_sha256": _sha(REPO_ROOT / "data" / "index" / "manifest.json")},
        "langgraph": _version("langgraph"), "prices_usd_per_million_tokens": {"input": PRICES[0], "output": PRICES[1]}, "price_source": PRICE_SOURCE,
        "final_evaluation_flag": args.split == "test",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--rehearsal", action="store_true", help="dev only: the dry run; a dirty tree is allowed (and recorded), the output says it is not a result")
    parser.add_argument("--rehearse-exclusion", action="store_true", help="dev only: take the first dev item as the excluded one, to exercise the second figure")
    parser.add_argument("--live", action="store_true", help="allow live Gemini calls for cache misses (default: offline, a miss stops the run)")
    parser.add_argument("--max-live-calls", type=int, default=100, help="hard cap on live requests in this run")
    parser.add_argument("--results-dir", default=None)
    parser.add_argument("--overwrite", action="store_true", help="dev only")
    parser.add_argument("--freeze-ref", default=None, help=f"the tag of the freeze, {FREEZE_TAG} (required for the test split)")
    parser.add_argument("--dev-run-sha", default=None, help="the commit of the official dev run (required for the test split)")
    args = parser.parse_args(argv)
    test = args.split == "test"
    if test and (args.rehearsal or args.rehearse_exclusion or args.overwrite):
        print("REFUSED: the test split has no rehearsal and no overwrite", file=sys.stderr)
        return 1
    try:
        results_dir = resolve_results_dir(args.results_dir or ("eval/results/phase8_test" if test else "eval/results/phase8_rehearsal"))
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    out = results_dir / f"{RESULT_NAME}.json"
    if out.exists() and not (args.overwrite and not test):
        print(f"REFUSED: {out} exists" + ("; the test split is run once" if test else "; pass --overwrite to replace it"), file=sys.stderr)
        return 1
    dirty: list[str] = []
    try:
        commit = check_clean_tree(allowed_prefixes=ALLOWED_UNTRACKED)
    except DirtyWorkingTree as exc:
        if test or not args.rehearsal:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        status = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=REPO_ROOT, capture_output=True, text=True).stdout
        dirty = list(blocking_paths(status, ALLOWED_UNTRACKED))
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    freeze: dict = {}
    if test:
        problems = [] if args.freeze_ref == FREEZE_TAG else [f"--freeze-ref must be {FREEZE_TAG}: the pre-registration names that tag and no other"]
        problems += frozen_problems(REPO_ROOT, args.freeze_ref, args.dev_run_sha) + attempts_problems(results_dir)
        if problems:
            print("REFUSED:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 1
        differences = external_state.check()  # the Qdrant collection and the seed orders as they are now, read only, against the values registered in PREREG section 1
        if differences:
            log_attempt(results_dir, kind="refusal", guard="external_state", ended="external_state_differs", opened_test_split=False, differences=differences)
            print("REFUSED: the state outside the repository is not the registered one (docs/PHASE8-PREREG.md section 1); the test split was not opened:\n  " + "\n  ".join(differences), file=sys.stderr)
            return 1
        freeze = {"ref": args.freeze_ref, "commit": resolve_freeze_tag(REPO_ROOT, args.freeze_ref)}
        print(f"OPENING THE TEST SPLIT (final_evaluation=True): once, under docs/PHASE8-PREREG.md, freeze {args.freeze_ref} = {freeze['commit'][:12]}", file=sys.stderr)
    items = sorted(load_split(args.split, final_evaluation=test), key=lambda i: i.id)
    hashes = {EXCLUDED_TEST_QUESTION_SHA256} if test else set()
    excluded = {i.id for i in items if question_sha256(i) in hashes}
    if args.rehearse_exclusion and not test:
        excluded = {items[0].id}
        print(f"rehearsal: {items[0].id} stands in for the excluded item", file=sys.stderr)
    answer_prompt = load_prompt(ANSWER_PROMPT)
    started, attempt = time.monotonic(), {"kind": "run", "split": args.split, "live": args.live, "git_commit": commit, "rehearsal": args.rehearsal}
    with tempfile.TemporaryDirectory(prefix="shopmate-final-") as tmp:
        try:
            harness, manifest, _ = build_harness(SimpleNamespace(live=args.live, max_live_calls=args.max_live_calls, answer_prompt=ANSWER_PROMPT), Path(tmp))
        except SystemExit as exc:
            print(exc, file=sys.stderr)
            return 1
        generator = harness.generator
        router = GeminiRouter(generator, load_prompt("router_v1"))
        extractor = FilterExtractor(generator, load_prompt("filters_v1"))
        retriever = harness.tools.inner.retriever

        def search(query, filters, k, mode):
            return [hit.key for hit in retriever.search(query, filters, k=k, mode=mode)]

        try:
            outcome = evaluate_final(items, harness, router, extractor, search)
        except LLMError as exc:
            ended, _ = classify_exception(exc)
            log_attempt(results_dir, **attempt, ended=ended, rerun_allowed=ended in RERUN_ELIGIBLE, live_calls=generator.inner.live_calls)
            print(f"STOPPED: {type(exc).__name__}: {exc}\nEverything fetched so far is in eval/llm_cache/; " + ("a rerun continues from it." if ended in RERUN_ELIGIBLE else "this is not an infrastructure error."), file=sys.stderr)
            return 2
        except Exception as exc:  # a service was not there, or a bug: say which kind, keep the cache
            ended, _ = classify_exception(exc)
            log_attempt(results_dir, **attempt, ended=ended, rerun_allowed=ended in RERUN_ELIGIBLE, live_calls=generator.inner.live_calls)
            print(f"FAILED: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
            return 3
        if not outcome["complete"]:
            reason = outcome["stopped"]["reason"]
            eligible = reason.split(":")[0] in ("DailyLimitExhausted", "DailyBudgetReached", "CallCapExceeded", "RetriesExhausted")
            log_attempt(results_dir, **attempt, ended=reason[:80], rerun_allowed=eligible, live_calls=generator.inner.live_calls)
            print(f"STOPPED before {outcome['stopped']['before']}: {reason}", file=sys.stderr)
            return 2
        document = build_document(items, outcome, excluded, provenance(args, commit, dirty, items, args.rehearsal, freeze, args.dev_run_sha, answer_prompt),
                                  {"live_calls_this_run": generator.inner.live_calls, "cache_hits_this_run": generator.inner.cache_hits,
                                   "wall_seconds": round(time.monotonic() - started, 1)})
    write_result(out, document, args.overwrite and not test)
    report = out.with_suffix(".md")
    report.write_text(render_report(document), encoding="utf-8")
    log_attempt(results_dir, **attempt, ended="complete", rerun_allowed=False, live_calls=document["measurements"]["live_calls_this_run"], sha256=document["deterministic_sha256"])
    summary = document["deterministic"]["metrics"]["all"]
    print(f"{out.name} ({args.split}{', rehearsal' if args.rehearsal else ''}): " + ", ".join(
        f"{name} {summary[name]['estimate']:.3f}" for name in ("router_accuracy", "extractor_exact_match", "product_mrr", "policy_mrr", "no_answer_correct", "answer_correct"))
        + f"; live calls {document['measurements']['live_calls_this_run']}, cache hits {document['measurements']['cache_hits_this_run']}; sha256={document['deterministic_sha256'][:12]}")
    print(f"report: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
