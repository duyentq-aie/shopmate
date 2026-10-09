"""Baseline retrieval evaluation on the DEV split (Phase 3B). Measures; changes nothing.

One run = one (mode, filter setting):
  mode     dense | hybrid
  filters  none     no filters at all
           routing  doc_type derived from the intent only (product_search -> product,
                    policy_question -> policy): the value of correct intent routing
           oracle   routing + the item's expected_filters minus `keyword` (DECISIONS R14):
                    the value of perfect filter extraction (an upper bound)

Only product_search and policy_question items are evaluated. Items come from
eval.golden.loader.load_split("dev"); the test split is never read here.

Refuses to run unless the git working tree is clean (no modified/staged tracked file anywhere,
no untracked file outside eval/results/), so every result references a real commit with
dirty=false. Refuses to overwrite an existing result file without --overwrite.

Output: eval/results/<mode>-<filters>.json with a `deterministic` section (per-item results,
aggregates, provenance; hashed into `deterministic_sha256`) and a `measurements` section
(latency, wall time; not hashed).

Usage (from the repo root; Qdrant running, index built):
    uv run python -m eval.run_retrieval_eval --mode dense --filters none
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from app.config import REPO_ROOT
from app.retrieval.embedder import EmbeddingConfig
from app.retrieval.manifest import MANIFEST_PATH, file_sha256, load_manifest
from eval.golden.loader import items_hash, load_split
from eval.golden.schema import GOLDEN_PATH, GoldenItem
from eval.retrieval_metrics import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    KS,
    bootstrap_ci,
    no_answer_item_metrics,
    percentile_ms,
    policy_item_metrics,
    product_item_metrics,
)

RESULTS_DIR = REPO_ROOT / "eval" / "results"
ALLOWED_UNTRACKED_PREFIX = "eval/results/"
RETRIEVAL_INTENTS = ("product_search", "policy_question")
MODES = ("dense", "hybrid")
FILTER_SETTINGS = ("none", "routing", "oracle")
TOP_K = 10
MIN_INTERPRETABLE_N = 8
METRICS_VERSION = "phase3b-v1"
DOC_TYPE_FOR_INTENT = {"product_search": "product", "policy_question": "policy"}


class DirtyWorkingTree(Exception):
    pass


# --- provenance --------------------------------------------------------------------------


def blocking_paths(porcelain_z: str) -> list[str]:
    """Paths that make the tree unacceptable, from `git status --porcelain=v1 -z --untracked-files=all`.

    Blocking: any tracked change (modified, staged, deleted, renamed, ...) anywhere, and any
    untracked file outside eval/results/. Allowed: untracked files under eval/results/.
    """
    entries = porcelain_z.split("\0")
    blocking, i = [], 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":  # rename/copy: the next entry is the original path
            i += 1
        if status == "??":
            if not path.startswith(ALLOWED_UNTRACKED_PREFIX):
                blocking.append(path)
        else:
            blocking.append(path)
    return blocking


def check_clean_tree(repo: Path = REPO_ROOT) -> str:
    """Return the HEAD commit; raise DirtyWorkingTree listing the blocking paths otherwise."""
    status = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                            cwd=repo, check=True, capture_output=True, text=True).stdout
    blocking = blocking_paths(status)
    if blocking:
        raise DirtyWorkingTree("working tree is not clean; commit or remove first:\n  " + "\n  ".join(blocking))
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def write_result(path: Path, result: dict, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} exists; pass --overwrite to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical_sha256(data: dict) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- evaluation ----------------------------------------------------------------------------


def build_filters(item: GoldenItem, setting: str) -> dict | None:
    if setting == "none":
        return None
    filters = {"doc_type": DOC_TYPE_FOR_INTENT[item.intent]}
    if setting == "routing":
        return filters
    if setting != "oracle":
        raise ValueError(f"unknown filter setting {setting!r}")
    if item.expected_filters is not None:
        filters.update(item.expected_filters.model_dump(exclude_none=True, exclude={"keyword"}))
    return filters


def gold_of(item: GoldenItem) -> dict:
    source = item.gold_source
    if source.type == "skus":
        return {"type": "skus", "keys": [f"product:{s}" for s in source.skus]}
    if source.type == "policy_clause":
        return {"type": "policy_clause", "keys": [f"policy:{source.document}:{source.clause}"]}
    return {"type": "no_answer", "keys": []}


def evaluate_item(item: GoldenItem, retrieved: list[str]) -> dict:
    gold = gold_of(item)
    if gold["type"] == "skus":
        return product_item_metrics(retrieved, set(gold["keys"]))
    if gold["type"] == "policy_clause":
        return policy_item_metrics(retrieved, gold["keys"][0])
    return no_answer_item_metrics(retrieved)


def run_items(items: list[GoldenItem], search, mode: str, setting: str) -> tuple[list[dict], list[float]]:
    """search(query, filters, k, mode) -> list of hit keys. Returns (records, latencies in s)."""
    records, latencies = [], []
    for item in sorted(items, key=lambda i: i.id):
        filters = build_filters(item, setting)
        started = time.perf_counter()
        retrieved = search(item.question, filters, TOP_K, mode)
        latencies.append(time.perf_counter() - started)
        records.append({
            "id": item.id, "intent": item.intent, "query_style": item.query_style,
            "author": item.author, "gold": gold_of(item), "filters": filters,
            "retrieved": retrieved, "metrics": evaluate_item(item, retrieved),
        })
    return records, latencies


def _summary(records: list[dict], names: list[str]) -> dict:
    return {name: bootstrap_ci([r["metrics"][name] for r in records]) for name in names}


PRODUCT_METRICS = [f"{m}@{k}" for m in ("hit", "precision", "nrecall") for k in KS] + ["rr"]
POLICY_METRICS = [f"hit@{k}" for k in KS] + ["rr"]
BREAKDOWN_METRICS = {"product_search": ["hit@1", "hit@5", "hit@10", "nrecall@10", "rr"],
                     "policy_question": ["hit@1", "hit@5", "rr"]}


def aggregate(records: list[dict]) -> dict:
    product = [r for r in records if r["gold"]["type"] == "skus"]
    policy = [r for r in records if r["gold"]["type"] == "policy_clause"]
    no_answer = [r for r in records if r["gold"]["type"] == "no_answer"]
    answerable = product + policy

    breakdown = {}
    for intent, group in (("product_search", product), ("policy_question", policy)):
        by_style = defaultdict(list)
        for r in group:
            by_style[r["query_style"]].append(r)
        breakdown[intent] = {
            style: {"n": len(rs), "too_small_to_interpret": len(rs) < MIN_INTERPRETABLE_N,
                    "metrics": {m: bootstrap_ci([r["metrics"][m] for r in rs])["mean"] for m in BREAKDOWN_METRICS[intent]}}
            for style, rs in sorted(by_style.items())
        }
    return {
        "counts": {"product_answerable": len(product), "policy": len(policy), "no_answer": len(no_answer)},
        "product_search": _summary(product, PRODUCT_METRICS),
        "policy_question": _summary(policy, POLICY_METRICS),
        "returned_results": {
            "mean_returned_answerable": bootstrap_ci([r["metrics"]["returned"] for r in answerable])["mean"],
            "answerable_with_zero_results": sum(1 for r in answerable if r["metrics"]["returned"] == 0),
        },
        "breakdown_by_query_style": breakdown,
        "filter_correctness_check": {
            "note": "no_answer items: share of correctly EMPTY result lists. Under oracle filters this is "
                    "true by construction; it checks filter correctness, it is NOT a quality metric.",
            "n": len(no_answer),
            "correct_empty": bootstrap_ci([r["metrics"]["correct_empty"] for r in no_answer])["mean"],
        },
    }


def provenance(commit: str, items: list[GoldenItem], mode: str, setting: str) -> dict:
    return {
        "git_commit": commit,
        "git_dirty": False,  # check_clean_tree() refuses to run otherwise
        "index_manifest_sha256": file_sha256(MANIFEST_PATH),
        "embedder_fingerprint": EmbeddingConfig().fingerprint(),
        "golden_file_sha256": file_sha256(GOLDEN_PATH),
        "dev_split_sha256": items_hash(items),
        "config": {"mode": mode, "filters": setting, "top_k": TOP_K, "ks": list(KS), "split": "dev",
                   "intents": list(RETRIEVAL_INTENTS), "oracle_drops": ["keyword"],
                   "bootstrap": {"resamples": BOOTSTRAP_RESAMPLES, "seed": BOOTSTRAP_SEED, "confidence": 0.95},
                   "metrics_version": METRICS_VERSION},
    }


def build_result(commit: str, items: list[GoldenItem], records: list[dict], latencies: list[float],
                 mode: str, setting: str, wall_seconds: float) -> dict:
    deterministic = {"provenance": provenance(commit, items, mode, setting), "aggregate": aggregate(records),
                     "items": records}
    return {
        "deterministic": deterministic,
        "deterministic_sha256": canonical_sha256(deterministic),
        "measurements": {"latency_ms": {"p50": percentile_ms(latencies, 0.5), "p95": percentile_ms(latencies, 0.95),
                                        "n": len(latencies)},
                         "wall_seconds": round(wall_seconds, 1)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--filters", choices=FILTER_SETTINGS, required=True)
    parser.add_argument("--overwrite", action="store_true", help="replace an existing result file")
    args = parser.parse_args(argv)
    out = RESULTS_DIR / f"{args.mode}-{args.filters}.json"
    if out.exists() and not args.overwrite:
        print(f"REFUSED: {out} exists; pass --overwrite to replace it", file=sys.stderr)
        return 1
    try:
        commit = check_clean_tree()
    except DirtyWorkingTree as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    from app.retrieval.embedder import BgeM3Embedder
    from app.retrieval.search import Retriever
    from app.retrieval.store import connect, count_by_doc_type

    client = connect()
    if count_by_doc_type(client) != load_manifest()["documents"]:
        print("REFUSED: the live index does not match data/index/manifest.json", file=sys.stderr)
        return 1
    items = [i for i in load_split("dev") if i.intent in RETRIEVAL_INTENTS]
    retriever = Retriever(client, BgeM3Embedder())
    retriever.search("khởi động", k=1, mode=args.mode)  # warm-up, not measured

    def search(query, filters, k, mode):
        return [hit.key for hit in retriever.search(query, filters, k=k, mode=mode)]

    started = time.monotonic()
    records, latencies = run_items(items, search, args.mode, args.filters)
    result = build_result(commit, items, records, latencies, args.mode, args.filters, time.monotonic() - started)
    write_result(out, result, args.overwrite)
    agg = result["deterministic"]["aggregate"]
    print(f"{out.name}: {len(records)} dev items; product hit@5={agg['product_search']['hit@5']['mean']:.3f} "
          f"policy hit@1={agg['policy_question']['hit@1']['mean']:.3f}; "
          f"p50={result['measurements']['latency_ms']['p50']} ms; sha256={result['deterministic_sha256'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
