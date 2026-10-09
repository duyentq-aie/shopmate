"""Retrieval metrics and bootstrap statistics (pure functions, no services).

Definitions (also written into docs/RESULTS-phase3b.md):

product_search, answerable (gold = set of SKUs G, retrieved list R, top-k = R[:k]):
- hit@k                                 = 1 if G ∩ top-k is non-empty, else 0
- precision@k (over returned results)   = |G ∩ top-k| / min(k, |R|); undefined (excluded and
                                          counted) when |R| = 0
- normalized recall@k                   = |G ∩ top-k| / min(k, |G|)
- reciprocal rank                       = 1 / rank of the first gold result, 0 if none in R
policy_question (one gold clause c):
- hit@k = 1 if c in top-k; reciprocal rank as above (MRR = mean)
no_answer (product_search with no matching product):
- correct_empty = 1 if |R| = 0. Under oracle filters this is true by construction (Qdrant applies
  exactly the filters that defined "no match"), so it is a FILTER CORRECTNESS CHECK, not quality.

Statistics: percentile bootstrap over items (10,000 resamples, fixed seed), 95% intervals.
Paired bootstrap for differences between two runs on the same items.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

KS = (1, 5, 10)
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261009
CONFIDENCE = 0.95


def first_rank(retrieved: Sequence[str], gold: set[str]) -> int | None:
    for rank, key in enumerate(retrieved, start=1):
        if key in gold:
            return rank
    return None


def product_item_metrics(retrieved: Sequence[str], gold: set[str]) -> dict:
    if not gold:
        raise ValueError("answerable items need a non-empty gold set")
    rank = first_rank(retrieved, gold)
    metrics: dict = {"first_gold_rank": rank, "rr": 1.0 / rank if rank else 0.0, "returned": len(retrieved)}
    for k in KS:
        top = retrieved[:k]
        found = len(gold.intersection(top))
        metrics[f"hit@{k}"] = 1.0 if found else 0.0
        metrics[f"precision@{k}"] = found / min(k, len(retrieved)) if retrieved else None
        metrics[f"nrecall@{k}"] = found / min(k, len(gold))
    return metrics


def policy_item_metrics(retrieved: Sequence[str], gold: str) -> dict:
    rank = first_rank(retrieved, {gold})
    metrics: dict = {"first_gold_rank": rank, "rr": 1.0 / rank if rank else 0.0, "returned": len(retrieved)}
    for k in KS:
        metrics[f"hit@{k}"] = 1.0 if rank is not None and rank <= k else 0.0
    return metrics


def no_answer_item_metrics(retrieved: Sequence[str]) -> dict:
    return {"returned": len(retrieved), "correct_empty": 1.0 if not retrieved else 0.0}


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(sorted_values: list[float], q: float) -> float:
    idx = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def bootstrap_ci(values: Sequence[float | None], resamples: int = BOOTSTRAP_RESAMPLES,
                 seed: int = BOOTSTRAP_SEED) -> dict:
    """Mean and percentile CI; None values (undefined metric) are excluded and counted."""
    defined = [v for v in values if v is not None]
    result = {"n": len(defined), "excluded": len(values) - len(defined), "mean": mean(defined),
              "ci_low": None, "ci_high": None}
    if not defined:
        return result
    rng = random.Random(seed)
    n = len(defined)
    means = sorted(sum(defined[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    alpha = (1 - CONFIDENCE) / 2
    result["ci_low"], result["ci_high"] = _percentile(means, alpha), _percentile(means, 1 - alpha)
    return result


def paired_bootstrap(a: Sequence[float | None], b: Sequence[float | None], resamples: int = BOOTSTRAP_RESAMPLES,
                     seed: int = BOOTSTRAP_SEED) -> dict:
    """Mean of per-item differences (b - a) with a CI; pairs with a None on either side are dropped."""
    if len(a) != len(b):
        raise ValueError("paired samples must have the same length")
    diffs = [y - x for x, y in zip(a, b) if x is not None and y is not None]
    ci = bootstrap_ci(diffs, resamples, seed)
    distinguishable = ci["n"] > 0 and (ci["ci_low"] > 0 or ci["ci_high"] < 0)
    return {"n_pairs": ci["n"], "mean_diff": ci["mean"], "ci_low": ci["ci_low"], "ci_high": ci["ci_high"],
            "distinguishable_from_noise": distinguishable,
            "items_better": sum(d > 0 for d in diffs), "items_worse": sum(d < 0 for d in diffs),
            "items_equal": sum(d == 0 for d in diffs)}


def percentile_ms(values_seconds: Sequence[float], q: float) -> float | None:
    if not values_seconds:
        return None
    return round(_percentile(sorted(values_seconds), q) * 1000, 1)
