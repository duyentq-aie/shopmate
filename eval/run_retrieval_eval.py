"""Retrieval evaluation on the DEV split (Phase 3B baseline, Phase 4 experiment 1). Measures; changes nothing.

One run = one (mode, filter setting[, intents][, queryset]):
  mode     dense | hybrid                                  (Phase 3B baseline)
           dense_sparse_folded | dense_folded              (Phase 4: needs the folded vector, layout v2)
  filters  none     no filters at all
           routing  doc_type derived from the intent only (product_search -> product,
                    policy_question -> policy): the value of correct intent routing
           oracle   routing + the item's expected_filters minus `keyword` (DECISIONS R14):
                    the value of perfect filter extraction (an upper bound)

  queryset dev          the dev items as written (default)
           dev_folded   the same items with the question machine-folded (eval/perturbation/dev_folded.json,
                        a robustness test; its hash is checked and recorded)
  intents  product_search and/or policy_question (default both), e.g. oracle for policy_question only

Only product_search and policy_question items are evaluated. Items come from
eval.golden.loader.load_split("dev"); the test split is never read here.
The collection evaluated is the one the committed manifest names (data/index/manifest.json), and
the live index is verified against that manifest first (counts, per-point fingerprints, dense/sparse
digest, lexical fingerprints), so a result can only come from the index it claims.

Refuses to run unless the git working tree is clean (no modified/staged tracked file anywhere,
no untracked file outside eval/results/), so every result references a real commit with
dirty=false. Refuses to overwrite an existing result file without --overwrite.

Output: <results dir>/<mode>-<filters>[.<intents>][.<queryset>].json with a `deterministic` section
(per-item results, aggregates, provenance; hashed into `deterministic_sha256`) and a `measurements`
section (latency, wall time; not hashed). The results dir defaults to eval/results/ and may be any
directory below it (--results-dir eval/results/phase4-exp1), so committed results are never touched.
Provenance of the Phase 3B modes on the default queryset has exactly the Phase 3B shape.

Usage (from the repo root; Qdrant running, index built):
    uv run python -m eval.run_retrieval_eval --mode dense --filters none
    uv run python -m eval.run_retrieval_eval --mode dense_folded --filters none --queryset dev_folded \
        --results-dir eval/results/phase4-exp1
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
from app.retrieval.lexical import LexicalConfig
from app.retrieval.manifest import MANIFEST_PATH, file_sha256, load_manifest
from eval.golden.loader import golden_file_sha256, items_hash, load_split
from eval.golden.schema import GoldenItem
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
PHASE3B_MODES = ("dense", "hybrid")
FOLDED_MODES = ("dense_sparse_folded", "dense_folded")  # Phase 4: need the folded vector (layout v2)
MODES = PHASE3B_MODES + FOLDED_MODES
QUERYSETS = ("dev", "dev_folded")
FILTER_SETTINGS = ("none", "routing", "oracle")
TOP_K = 10
MIN_INTERPRETABLE_N = 8
METRICS_VERSION = "phase3b-v1"
DOC_TYPE_FOR_INTENT = {"product_search": "product", "policy_question": "policy"}


class DirtyWorkingTree(Exception):
    pass


def index_problems(client, manifest: dict, query_config: EmbeddingConfig, collection: str | None = None,
                   query_lexical: LexicalConfig | None = None) -> list[str]:
    """Why the live index cannot be trusted to be the one the manifest describes ([] if it can).

    Point counts alone can match while contents differ (e.g. a re-index with another model revision
    keeps 269 points), so every point's embedding fingerprint must equal the manifest's, and the
    manifest's must equal the embedder used for queries. A layout-v2 manifest adds the same for the
    folded vector (lexical fingerprint per point, query-side config, vector present with the IDF
    modifier) and a digest of the dense/sparse vectors, so the baseline vectors cannot drift
    unnoticed under a configuration that does not even use the folded one.
    """
    from qdrant_client import models

    from app.retrieval.store import (
        COLLECTION,
        FOLDED,
        count_by_doc_type,
        fingerprint_counts,
        lexical_fingerprint_counts,
        vectors_digest,
    )

    collection = collection or manifest.get("collection", COLLECTION)
    problems = []
    counts = count_by_doc_type(client, collection)
    if counts != manifest["documents"]:
        problems.append(f"point counts {counts} != manifest {manifest['documents']}")
    expected = manifest["embedding"]["fingerprint"]
    if expected != query_config.fingerprint():
        problems.append(f"manifest fingerprint {expected} != query embedder {query_config.fingerprint()}")
    wrong = {fp: n for fp, n in fingerprint_counts(client, collection).items() if fp != expected}
    if wrong:
        problems.append(f"points with another embedding fingerprint than the manifest ({expected}): {wrong}")

    sparse_vectors = client.get_collection(collection).config.params.sparse_vectors or {}
    folded = sparse_vectors.get(FOLDED)
    lexical = manifest.get("lexical")
    if lexical:
        expected_lexical = lexical["fingerprint"]
        query_lexical = query_lexical or LexicalConfig()
        if expected_lexical != query_lexical.fingerprint():
            problems.append(f"manifest lexical fingerprint {expected_lexical} != query lexical config "
                            f"{query_lexical.fingerprint()}")
        if folded is None or folded.modifier != models.Modifier.IDF:
            problems.append(f"collection {collection!r} has no folded vector with the IDF modifier, but the "
                            f"manifest declares a lexical config")
        wrong_lexical = {fp: n for fp, n in lexical_fingerprint_counts(client, collection).items()
                         if fp != expected_lexical}
        if wrong_lexical:
            problems.append(f"points with another lexical fingerprint than the manifest ({expected_lexical}): "
                            f"{wrong_lexical}")
    elif folded is not None:
        problems.append(f"collection {collection!r} has a folded vector but the manifest declares no lexical config")
    if "vectors_digest" in manifest:
        live = vectors_digest(client, collection)
        if live != manifest["vectors_digest"]:
            problems.append(f"dense/sparse vectors digest {live[:16]} != manifest {manifest['vectors_digest'][:16]}")
    return problems


def mode_problems(mode: str, manifest: dict) -> list[str]:
    """Why `mode` cannot run against the index the manifest describes."""
    if mode in FOLDED_MODES and not manifest.get("lexical"):
        return [f"mode {mode!r} needs the folded vector, but the manifest declares no lexical config (layout v1)"]
    return []


# --- provenance --------------------------------------------------------------------------


def blocking_paths(porcelain_z: str, allowed_prefixes: tuple[str, ...] = (ALLOWED_UNTRACKED_PREFIX,)) -> list[str]:
    """Paths that make the tree unacceptable, from `git status --porcelain=v1 -z --untracked-files=all`.

    Blocking: any tracked change (modified, staged, deleted, renamed, ...) anywhere, and any
    untracked file outside the allowed prefixes (default eval/results/; the Phase 5 runner also allows
    eval/llm_cache/, where its own cache entries appear). Allowed: untracked files under those prefixes.
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
            if not path.startswith(allowed_prefixes):
                blocking.append(path)
        else:
            blocking.append(path)
    return blocking


def check_clean_tree(repo: Path = REPO_ROOT, allowed_prefixes: tuple[str, ...] = (ALLOWED_UNTRACKED_PREFIX,)) -> str:
    """Return the HEAD commit; raise DirtyWorkingTree listing the blocking paths otherwise."""
    status = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                            cwd=repo, check=True, capture_output=True, text=True).stdout
    blocking = blocking_paths(status, allowed_prefixes)
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


# --- naming and selection -----------------------------------------------------------------------


def resolve_results_dir(arg: str | None) -> Path:
    """The results directory: eval/results/ or any directory below it (the clean-tree check only
    tolerates new files there). Relative paths are taken from the repo root."""
    if arg is None:
        return RESULTS_DIR
    path = (REPO_ROOT / arg).resolve()
    if path != RESULTS_DIR.resolve() and RESULTS_DIR.resolve() not in path.parents:
        raise ValueError(f"--results-dir must be eval/results or a directory below it, got {arg!r}")
    return path


def canonical_intents(intents) -> tuple[str, ...]:
    """The requested intents in the fixed RETRIEVAL_INTENTS order, so the order typed does not matter."""
    return tuple(i for i in RETRIEVAL_INTENTS if i in intents)


def result_name(mode: str, setting: str, intents: tuple[str, ...] = RETRIEVAL_INTENTS, queryset: str = "dev") -> str:
    """<mode>-<filters>[.<intents>][.<queryset>].json; the default run keeps its Phase 3B name."""
    name = f"{mode}-{setting}"
    if canonical_intents(intents) != RETRIEVAL_INTENTS:
        name += "." + "+".join(canonical_intents(intents))
    if queryset != "dev":
        name += f".{queryset}"
    return name + ".json"


def select_items(items: list[GoldenItem], intents) -> list[GoldenItem]:
    return [i for i in items if i.intent in intents]


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


def provenance(commit: str, items: list[GoldenItem], mode: str, setting: str, *,
               intents: tuple[str, ...] = RETRIEVAL_INTENTS, queryset: str = "dev",
               perturbation_sha256: str | None = None) -> dict:
    """`items` are the ORIGINAL dev items evaluated (for a perturbed queryset too: the perturbation
    file's own hash is recorded separately). Fields are only ADDED for the new modes/querysets, so
    the Phase 3B runs keep exactly their Phase 3B shape."""
    prov = {
        "git_commit": commit,
        "git_dirty": False,  # check_clean_tree() refuses to run otherwise
        "index_manifest_sha256": file_sha256(MANIFEST_PATH),
        "embedder_fingerprint": EmbeddingConfig().fingerprint(),
        "golden_file_sha256": golden_file_sha256(),
        "dev_split_sha256": items_hash(items),
        "config": {"mode": mode, "filters": setting, "top_k": TOP_K, "ks": list(KS), "split": "dev",
                   "intents": list(intents), "oracle_drops": ["keyword"],
                   "bootstrap": {"resamples": BOOTSTRAP_RESAMPLES, "seed": BOOTSTRAP_SEED, "confidence": 0.95},
                   "metrics_version": METRICS_VERSION},
    }
    if mode in FOLDED_MODES:
        prov["lexical_fingerprint"] = LexicalConfig().fingerprint()
    if queryset != "dev":
        prov["config"]["queryset"] = queryset
        prov["perturbation_sha256"] = perturbation_sha256
    return prov


def build_result(commit: str, items: list[GoldenItem], records: list[dict], latencies: list[float],
                 mode: str, setting: str, wall_seconds: float, **provenance_options) -> dict:
    deterministic = {"provenance": provenance(commit, items, mode, setting, **provenance_options),
                     "aggregate": aggregate(records), "items": records}
    return {
        "deterministic": deterministic,
        "deterministic_sha256": canonical_sha256(deterministic),
        "measurements": {"latency_ms": {"p50": percentile_ms(latencies, 0.5), "p95": percentile_ms(latencies, 0.95),
                                        "n": len(latencies)},
                         "wall_seconds": round(wall_seconds, 1)},
    }


def format_summary(name: str, result: dict) -> str:
    agg = result["deterministic"]["aggregate"]

    def fmt(cell: dict) -> str:
        return "n/a" if cell["mean"] is None else f"{cell['mean']:.3f}"

    return (f"{name}: {len(result['deterministic']['items'])} dev items; "
            f"product hit@5={fmt(agg['product_search']['hit@5'])} policy hit@1={fmt(agg['policy_question']['hit@1'])}; "
            f"p50={result['measurements']['latency_ms']['p50']} ms; sha256={result['deterministic_sha256'][:12]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--filters", choices=FILTER_SETTINGS, required=True)
    parser.add_argument("--queryset", choices=QUERYSETS, default="dev")
    parser.add_argument("--intents", nargs="+", choices=RETRIEVAL_INTENTS, default=list(RETRIEVAL_INTENTS))
    parser.add_argument("--results-dir", default=None, help="a directory below eval/results (default: eval/results)")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing result file")
    args = parser.parse_args(argv)
    try:
        results_dir = resolve_results_dir(args.results_dir)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    intents = canonical_intents(args.intents)
    out = results_dir / result_name(args.mode, args.filters, intents, args.queryset)
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
    from app.retrieval.store import connect

    manifest = load_manifest()
    problems = mode_problems(args.mode, manifest)
    if problems:
        print("REFUSED: " + "; ".join(problems), file=sys.stderr)
        return 1
    client = connect()
    problems = index_problems(client, manifest, EmbeddingConfig(), query_lexical=LexicalConfig())
    if problems:
        print("REFUSED: the live index does not match data/index/manifest.json:\n  " + "\n  ".join(problems),
              file=sys.stderr)
        return 1
    dev_items = select_items([i for i in load_split("dev") if i.intent in RETRIEVAL_INTENTS], intents)
    items, perturbation_sha256 = dev_items, None
    if args.queryset == "dev_folded":
        from eval.perturbation.dev_folded import load_dev_folded

        perturbed, perturbation_sha256 = load_dev_folded(load_split("dev"))
        items = select_items(perturbed, intents)
    retriever = Retriever(client, BgeM3Embedder(), collection=manifest["collection"])
    retriever.search("khởi động", k=1, mode=args.mode)  # warm-up, not measured

    def search(query, filters, k, mode):
        return [hit.key for hit in retriever.search(query, filters, k=k, mode=mode)]

    started = time.monotonic()
    records, latencies = run_items(items, search, args.mode, args.filters)
    options = {"intents": intents, "queryset": args.queryset, "perturbation_sha256": perturbation_sha256}
    result = build_result(commit, dev_items, records, latencies, args.mode, args.filters, time.monotonic() - started,
                          **options)
    write_result(out, result, args.overwrite)
    print(format_summary(out.name, result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
