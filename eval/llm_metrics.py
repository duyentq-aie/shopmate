"""Metrics of the LLM components (Phase 5): routing and filter extraction. Pure functions, no services.

Routing (records {id, gold, pred, query_style}; pred None = unusable answer, its own confusion column):
accuracy, per-class precision/recall/F1, macro-F1 over the classes that occur in the gold labels, confusion matrix,
accuracy by query style.

Filter extraction (pairs of expected, extracted dicts): exact match of the whole filter set, and per-field
true/false positives and false negatives. A field is a true positive when expected and extracted have the same value;
a wrong value is a false positive AND a false negative; an extracted field that was not expected is a false positive;
an expected field that was not extracted is a false negative. The expected side is the golden `expected_filters`
without `keyword` (not a filter, R14), exactly as the Phase 3B oracle did.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from app.llm.intents import INTENTS
from app.llm.extractor import EXTRACTOR_FIELDS
from app.retrieval.search import SearchFilters

UNPARSEABLE = "unparseable"


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def routing_metrics(records: list[dict]) -> dict:
    confusion: dict[str, Counter] = defaultdict(Counter)
    by_style: dict[str, list[bool]] = defaultdict(list)
    for r in records:
        confusion[r["gold"]][r["pred"] if r["pred"] is not None else UNPARSEABLE] += 1
        by_style[r["query_style"]].append(r["pred"] == r["gold"])
    per_class = {}
    for intent in INTENTS:
        tp = sum(1 for r in records if r["gold"] == intent and r["pred"] == intent)
        fp = sum(1 for r in records if r["gold"] != intent and r["pred"] == intent)
        fn = sum(1 for r in records if r["gold"] == intent and r["pred"] != intent)
        precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
        f1 = (2 * precision * recall / (precision + recall)) if precision and recall else (0.0 if tp + fn else None)
        per_class[intent] = {"tp": tp, "fp": fp, "fn": fn, "support": tp + fn, "precision": precision, "recall": recall, "f1": f1}
    supported = [c["f1"] for c in per_class.values() if c["support"] > 0]
    correct = sum(1 for r in records if r["pred"] == r["gold"])
    return {
        "n": len(records), "accuracy": _ratio(correct, len(records)),
        "macro_f1": sum(supported) / len(supported) if supported else None, "macro_f1_classes": len(supported),
        "per_class": per_class, "confusion": {g: dict(c) for g, c in sorted(confusion.items())},
        "by_style": {s: {"n": len(v), "accuracy": sum(v) / len(v)} for s, v in sorted(by_style.items())},
        "errors": [r["id"] for r in records if r["pred"] != r["gold"]],
    }


def filter_field_metrics(pairs: list[tuple[dict, dict | None]], exclude_fields: tuple[str, ...] = ()) -> dict:
    """`exclude_fields` leaves fields out of the comparison entirely (exact match and per-field counts), e.g. in_stock
    while the golden label of gs-019 conflicts with the R32 rule."""
    fields_in_play = [f for f in EXTRACTOR_FIELDS if f not in exclude_fields]
    counts = {f: Counter() for f in fields_in_play}
    exact = failures = 0
    for expected, extracted in pairs:
        if extracted is None:
            failures += 1
            extracted = {}
        expected = {k: v for k, v in expected.items() if k not in exclude_fields}
        extracted = {k: v for k, v in extracted.items() if k not in exclude_fields}
        exact += expected == extracted
        for f in fields_in_play:
            want, got = expected.get(f), extracted.get(f)
            if want is not None and got is not None and want == got:
                counts[f]["tp"] += 1
            else:
                counts[f]["fp"] += got is not None
                counts[f]["fn"] += want is not None
    fields = {}
    for f, c in counts.items():
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        fields[f] = {"tp": tp, "fp": fp, "fn": fn, "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn)}
    return {"n": len(pairs), "exact_match": exact, "exact_match_rate": _ratio(exact, len(pairs)), "failures": failures,
            "excluded_fields": list(exclude_fields), "fields": fields}


def normalized_expected(item) -> dict:
    """Golden expected_filters as a plain dict of the fields that are set, without `keyword`."""
    if item.expected_filters is None:
        return {}
    return item.expected_filters.model_dump(exclude_none=True, exclude={"keyword"})


def normalized_extracted(filters: SearchFilters | None) -> dict:
    return {} if filters is None else filters.model_dump(exclude_none=True)
