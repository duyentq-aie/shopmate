"""Paired comparisons between the Phase 3B result files (reads eval/results/*.json only).

- dense vs hybrid, per filter setting (difference = hybrid - dense);
- none -> routing (value of correct routing) and routing -> oracle (value of correct filter
  extraction), per mode (difference = later - earlier).
All runs must share the git commit, index manifest, embedder fingerprint and dev-split hash.
Writes eval/results/comparison.json (deterministic; refuses to overwrite without --overwrite).

Usage:
    uv run python -m eval.compare_retrieval_runs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eval.retrieval_metrics import paired_bootstrap
from eval.run_retrieval_eval import FILTER_SETTINGS, PHASE3B_MODES as MODES, RESULTS_DIR, canonical_sha256, write_result

COMPARED = {"product_search": ["hit@1", "hit@5", "hit@10", "nrecall@10", "precision@5", "rr"],
            "policy_question": ["hit@1", "hit@5", "rr"]}
SHARED_PROVENANCE = ("git_commit", "git_dirty", "index_manifest_sha256", "embedder_fingerprint", "dev_split_sha256",
                     "golden_file_sha256")


def load_runs(results_dir: Path = RESULTS_DIR) -> dict[tuple[str, str], dict]:
    runs = {}
    for mode in MODES:
        for setting in FILTER_SETTINGS:
            path = results_dir / f"{mode}-{setting}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            if canonical_sha256(data["deterministic"]) != data["deterministic_sha256"]:
                raise ValueError(f"{path}: deterministic_sha256 does not match its content")
            runs[(mode, setting)] = data["deterministic"]
    first = next(iter(runs.values()))["provenance"]
    for key, run in runs.items():
        for field in SHARED_PROVENANCE:
            if run["provenance"][field] != first[field]:
                raise ValueError(f"run {key} has a different {field}; runs are not comparable")
    if first["git_dirty"]:
        raise ValueError("runs were made from a dirty tree")
    return runs


def _per_item(run: dict, intent_gold: str, metric: str) -> dict[str, float | None]:
    return {r["id"]: r["metrics"].get(metric) for r in run["items"] if r["gold"]["type"] == intent_gold}


def compare(a: dict, b: dict) -> dict:
    """Paired differences b - a for every compared metric."""
    out = {}
    for intent, gold_type in (("product_search", "skus"), ("policy_question", "policy_clause")):
        out[intent] = {}
        for metric in COMPARED[intent]:
            xa, xb = _per_item(a, gold_type, metric), _per_item(b, gold_type, metric)
            if xa.keys() != xb.keys():
                raise ValueError("runs cover different items")
            ids = sorted(xa)
            out[intent][metric] = paired_bootstrap([xa[i] for i in ids], [xb[i] for i in ids])
    return out


def build_comparison(runs: dict[tuple[str, str], dict]) -> dict:
    result = {
        "provenance": {k: next(iter(runs.values()))["provenance"][k] for k in SHARED_PROVENANCE},
        "hybrid_minus_dense": {s: compare(runs[("dense", s)], runs[("hybrid", s)]) for s in FILTER_SETTINGS},
        "routing_minus_none": {m: compare(runs[(m, "none")], runs[(m, "routing")]) for m in MODES},
        "oracle_minus_routing": {m: compare(runs[(m, "routing")], runs[(m, "oracle")]) for m in MODES},
    }
    return {"deterministic": result, "deterministic_sha256": canonical_sha256(result)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        comparison = build_comparison(load_runs())
        write_result(RESULTS_DIR / "comparison.json", comparison, args.overwrite)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"comparison.json written, sha256={comparison['deterministic_sha256'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
