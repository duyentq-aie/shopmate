"""Offline integrity check of the committed result files (docs/DECISIONS-phase7.md, D7.20). No Qdrant, no Saleor, no Gemini, no model.

    uv run python -m eval.verify_committed_results            # exit 0: every check passed; 1: something does not match

What it checks, on the files as committed:
1. Every `deterministic_sha256` of eval/results/**.json is the hash of its `deterministic` block, and every `analysis_sha256` is the hash of its analysis
   (so a number edited by hand, or a file regenerated and not committed, is found).
2. eval/results/comparison.json is the paired comparison of the six Phase 3B runs: it is recomputed here from those runs (eval.compare_retrieval_runs, seeded
   bootstrap) and its hash must be the committed one.
3. The Phase 6 analysis agrees with the official result of the same code: the replay in `answer_v2.analysis.json` and `phase6_after_a21/answer_v2.json` give the
   same items, correct counts, fallbacks and declines per intent.

What it does NOT do: it does not re-run `eval.analyze_phase6` itself. That script replays the agent through the live Qdrant index, the bge-m3 model and the read-only
Saleor (the retrieval and the orders are real), so it cannot run in CI. The hashes in (1) and the cross-check in (3) are what can be reproduced without them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from eval.compare_retrieval_runs import build_comparison, load_runs
from eval.run_retrieval_eval import RESULTS_DIR, canonical_sha256

PHASE6_ANALYSIS = RESULTS_DIR / "phase6" / "answer_v2.analysis.json"
PHASE6_OFFICIAL = RESULTS_DIR / "phase6_after_a21" / "answer_v2.json"


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def hash_problems(results_dir: Path = RESULTS_DIR) -> list[str]:
    problems = []
    for path in sorted(results_dir.rglob("*.json")):
        data = _json(path)
        name = path.relative_to(results_dir).as_posix()
        if "deterministic" in data and "deterministic_sha256" in data:
            if canonical_sha256(data["deterministic"]) != data["deterministic_sha256"]:
                problems.append(f"{name}: deterministic_sha256 is not the hash of its `deterministic` block")
        elif "analysis_sha256" in data:
            body = {k: v for k, v in data.items() if k not in ("provenance", "analysis_sha256")}
            if canonical_sha256(body) != data["analysis_sha256"]:
                problems.append(f"{name}: analysis_sha256 is not the hash of its analysis")
        else:
            problems.append(f"{name}: has neither deterministic_sha256 nor analysis_sha256")
    return problems


def comparison_problems(results_dir: Path = RESULTS_DIR) -> list[str]:
    committed = _json(results_dir / "comparison.json")
    try:
        recomputed = build_comparison(load_runs(results_dir))
    except (ValueError, FileNotFoundError, KeyError) as exc:
        return [f"comparison.json: cannot be recomputed from the Phase 3B runs: {exc}"]
    if recomputed["deterministic_sha256"] != committed["deterministic_sha256"] or recomputed["deterministic"] != committed["deterministic"]:
        return ["comparison.json: recomputed from the six Phase 3B runs it differs from the committed file"]
    return []


def phase6_problems(analysis_path: Path = PHASE6_ANALYSIS, official_path: Path = PHASE6_OFFICIAL) -> list[str]:
    analysis, official = _json(analysis_path), _json(official_path)["deterministic"]
    problems = []
    per_intent = analysis["per_intent"]
    for intent, row in official["per_intent"].items():
        mine = per_intent[intent]
        if (mine["items"], mine["correct"]) != (row["n"], row["correct"]):
            problems.append(f"phase6 {intent}: analysis says {mine['correct']}/{mine['items']}, the official result {row['correct']}/{row['n']}")
    guard, extra = official["guardrails"], analysis["multiturn_fallbacks"]
    dev = per_intent["all"]
    for label, mine, theirs in (("answers", dev["answers_written_by_the_model"] + extra["answers"], guard["answers"]),
                                ("fallbacks", dev["fallbacks"] + extra["fallbacks"], guard["fallbacks"]),
                                ("declines", dev["declines"] + extra["declines"], guard["declines"])):
        if mine != theirs:
            problems.append(f"phase6 {label}: analysis {mine}, official result {theirs}")
    for key, field in (("items_with_fallback", "items_with_fallback"), ("items_with_decline", "items_with_decline")):
        listed = sorted(i for intent, row in per_intent.items() if intent != "all" for i in row[field])
        if listed != sorted(guard[key]):
            problems.append(f"phase6 {key}: analysis {listed}, official result {sorted(guard[key])}")
    return problems


def main() -> int:
    checks = (("hashes of every committed result and analysis", hash_problems), ("comparison.json recomputed from the Phase 3B runs", comparison_problems),
              ("Phase 6 analysis agrees with the official result", phase6_problems))
    failed = 0
    for name, check in checks:
        problems = check()
        print(("FAIL " if problems else "ok   ") + name)
        for problem in problems:
            print("       " + problem)
        failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
