"""The decision rule of Phase 4 experiment 1, as code. Written and committed BEFORE any run.

A candidate (dense_sparse_folded or dense_folded) is compared with the baseline `hybrid`, filters=none.
It is adopted only if BOTH hold:
  1. perturbation set (33 answerable dev items, question machine-folded, same gold): MRR improves
     and the paired-bootstrap 95% CI of the per-item difference excludes 0 (ci_low > 0);
  2. original dev set: overall MRR is not worse (point estimate >= baseline, up to float noise) for
     product_search AND for policy_question (answerable items, all query styles).
If both candidates pass, the larger perturbation gain wins; but if the two gains' CIs overlap heavily
(overlap >= HEAVY_OVERLAP of the shorter CI) the simpler/faster configuration wins (dense_folded: two
branches, no bge-m3 sparse). If neither passes the baseline stays and the experiment is recorded as a
negative result. The rule is not changed after results exist; the 6 real khong_dau items and the
machine-detected zero-diacritic items are reported as EXPLORATORY and do not decide anything.

Inputs are the per-item records of eval/results/*.json (`items`), nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence

from eval.retrieval_metrics import mean, paired_bootstrap

RULE_VERSION = "phase4-exp1-v2"
CANDIDATES = ("dense_sparse_folded", "dense_folded")
SIMPLICITY_ORDER = ("dense_folded", "dense_sparse_folded")  # fewer branches, no bge-m3 sparse: simpler and faster
HEAVY_OVERLAP = 0.5  # overlap / length of the shorter CI; this value and above is "heavy"
FLOAT_TOLERANCE = 1e-12  # "not worse" ignores differences below float summation noise
INTENT_OF_GOLD = {"skus": "product_search", "policy_clause": "policy_question"}  # answerable gold types


def rr_by_id(records: Sequence[dict], gold_type: str | None = None) -> dict[str, float]:
    """Reciprocal rank per answerable item (no_answer items have none); optionally one gold type only."""
    return {r["id"]: r["metrics"]["rr"] for r in records
            if r["gold"]["type"] in INTENT_OF_GOLD and (gold_type is None or r["gold"]["type"] == gold_type)}


def _paired(baseline: dict[str, float], candidate: dict[str, float]) -> tuple[list[str], list[float], list[float]]:
    if baseline.keys() != candidate.keys():
        raise ValueError("the two runs cover different items")
    ids = sorted(baseline)
    return ids, [baseline[i] for i in ids], [candidate[i] for i in ids]


def _original_intent(baseline: Sequence[dict], candidate: Sequence[dict], gold_type: str) -> dict:
    _, base, cand = _paired(rr_by_id(baseline, gold_type), rr_by_id(candidate, gold_type))
    base_mrr, cand_mrr = mean(base), mean(cand)
    diff = cand_mrr - base_mrr
    return {"n": len(base), "baseline_mrr": base_mrr, "candidate_mrr": cand_mrr, "diff": diff,
            "not_worse": diff >= -FLOAT_TOLERANCE}


def evaluate_candidate(baseline_perturbed: Sequence[dict], candidate_perturbed: Sequence[dict],
                       baseline_original: Sequence[dict], candidate_original: Sequence[dict]) -> dict:
    _, base, cand = _paired(rr_by_id(baseline_perturbed), rr_by_id(candidate_perturbed))
    boot = paired_bootstrap(base, cand)
    perturbation = {**{k: boot[k] for k in ("n_pairs", "mean_diff", "ci_low", "ci_high", "items_better",
                                            "items_worse", "items_equal")},
                    "passes": boot["ci_low"] is not None and boot["ci_low"] > 0}
    original = {INTENT_OF_GOLD[g]: _original_intent(baseline_original, candidate_original, g) for g in INTENT_OF_GOLD}
    adopt = perturbation["passes"] and all(v["not_worse"] for v in original.values())
    return {"perturbation": perturbation, "original": original, "adopt": adopt}


def overlap_fraction(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Length of the intersection of two intervals divided by the length of the shorter one.

    A zero-length interval counts as fully overlapping (1.0) if it lies inside the other, else 0.0."""
    low, high = max(a[0], b[0]), min(a[1], b[1])
    shorter = min(a[1] - a[0], b[1] - b[0])
    if shorter == 0:  # a single point: inside the other interval or not
        return 1.0 if high >= low else 0.0
    return max(0.0, high - low) / shorter


def choose(verdicts: dict[str, dict]) -> dict:
    """The decision among the candidates, given each one's evaluate_candidate() result."""
    missing = [c for c in CANDIDATES if c not in verdicts]
    if missing:
        raise ValueError(f"both candidates must be judged; missing: {missing}")
    passing = [c for c in CANDIDATES if verdicts[c]["adopt"]]
    out = {"rule_version": RULE_VERSION, "passing": passing, "n_candidates_tested": len(CANDIDATES), "overlap": None,
           "decision": "keep_baseline", "reason": "no candidate satisfied the rule: documented negative result"}
    if len(passing) == 1:
        out.update(decision=passing[0], reason=f"only {passing[0]} satisfied the rule")
    elif len(passing) == 2:
        cis = {c: (verdicts[c]["perturbation"]["ci_low"], verdicts[c]["perturbation"]["ci_high"]) for c in passing}
        out["overlap"] = overlap_fraction(cis[CANDIDATES[0]], cis[CANDIDATES[1]])
        if out["overlap"] >= HEAVY_OVERLAP:
            simpler = next(c for c in SIMPLICITY_ORDER if c in passing)
            out.update(decision=simpler, reason=f"both satisfied the rule and their gain CIs overlap "
                                                f"{out['overlap']:.2f} >= {HEAVY_OVERLAP}: the simpler/faster configuration")
        else:
            larger = max(passing, key=lambda c: verdicts[c]["perturbation"]["mean_diff"])
            out.update(decision=larger, reason="both satisfied the rule; the larger perturbation gain, "
                                               "the gain CIs do not overlap heavily")
    return out
