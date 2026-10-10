"""Phase 4 experiment 1: compare dense_sparse_folded and dense_folded with the baseline `hybrid`.

Reads result files only (eval/results/phase4-exp1/ by default) plus, for the false-friend analysis, the
text of the indexed documents. Produces comparison-exp1.json with:
- reproduction: the baseline re-run against the committed Phase 3B hybrid-none (exact hash when the
  commit and manifest are the same; otherwise items + aggregate identical and ONLY git_commit and
  index_manifest_sha256 differing in the provenance);
- dev / perturbation: paired bootstrap of each candidate against the baseline per intent and query
  style, filters none and routing;
- rule: the pre-registered decision rule (eval/exp1_rule.py) applied to filters=none, and its choice;
- exploratory: the 6 real khong_dau items and the machine-detected zero-diacritic items (they decide
  nothing);
- co_dau_worsened: every co_dau item whose first-gold rank got worse, with the documents that newly
  outrank gold and which query words matched them ONLY through folding (the "phi" / "phi công" kind);
- oracle_policy: oracle filters for policy_question only (product oracle is a tautology, Phase 3B).

Usage (from the repo root; Qdrant running, for the document texts):
    uv run python -m eval.compare_exp1 --results-dir eval/results/phase4-exp1
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections.abc import Sequence
from pathlib import Path

from app.retrieval.folding import fold, has_diacritics, tokenize
from eval import exp1_rule
from eval.exp1_rule import CANDIDATES, INTENT_OF_GOLD
from eval.retrieval_metrics import mean, paired_bootstrap
from eval.run_retrieval_eval import (
    MIN_INTERPRETABLE_N,
    RESULTS_DIR,
    RETRIEVAL_INTENTS,
    canonical_sha256,
    resolve_results_dir,
    result_name,
    write_result,
)

BASELINE = "hybrid"
SETTINGS = ("none", "routing")
QUERYSETS = ("dev", "dev_folded")
TABLE_METRICS = ["rr", "hit@1", "hit@5"]
COMMITTED_BASELINE = RESULTS_DIR / "hybrid-none.json"
ALLOWED_REPRODUCTION_DIFFERENCES = {"git_commit", "index_manifest_sha256"}
SHARED_PROVENANCE = ("git_commit", "git_dirty", "index_manifest_sha256", "embedder_fingerprint", "dev_split_sha256",
                     "golden_file_sha256")
EXPLORATORY_STATUS = ("EXPLORATORY: reported for information, not part of the pre-registered decision rule; "
                      "it does not decide anything")
CAVEATS = [
    "multiple comparisons: two candidate configurations were tested against the same baseline, which gives two "
    "chances to pass the rule by luck; the rule does not correct for it",
    "the perturbation set is machine-stripped text: no typos and no abbreviations beyond the original questions; "
    "it is a robustness test, not a substitute for real queries",
    "dev sets are small (19 answerable product items, 14 policy items); most intervals are wide and query-style "
    "cells with fewer than 8 items are marked too_small_to_interpret",
    "exact score ties inside a branch (frequent in the folded branch: equal-length documents with the same matching "
    "tokens) are broken by stable key, client-side (DECISIONS R25): an arbitrary but fixed rule, so part of any "
    "measured difference may come from that arbitrary order",
    "dense_sparse_folded has two lexical branches of three, so dense carries 1/3 of the RRF weight instead of "
    "1/2 as in the baseline and in dense_folded: its effect mixes 'a new signal' with 'less dense weight'",
]


# --- reading and checking runs --------------------------------------------------------------------------------


def check_integrity(run: dict, name: str) -> dict:
    if canonical_sha256(run["deterministic"]) != run["deterministic_sha256"]:
        raise ValueError(f"{name}: deterministic_sha256 does not match its content")
    return run


def reproduction_check(committed: dict, new: dict) -> dict:
    """Does `new` reproduce the committed baseline run? (see the module docstring for what that means)"""
    check_integrity(committed, "committed baseline")
    check_integrity(new, "re-run baseline")
    old_d, new_d = committed["deterministic"], new["deterministic"]
    problems = []
    if old_d["items"] != new_d["items"]:
        problems.append("items differ")
    if old_d["aggregate"] != new_d["aggregate"]:
        problems.append("aggregate differs")
    old_p, new_p = old_d["provenance"], new_d["provenance"]
    if set(old_p) != set(new_p):
        problems.append(f"provenance fields differ: {sorted(set(old_p) ^ set(new_p))}")
    differences = sorted(k for k in set(old_p) & set(new_p) if old_p[k] != new_p[k])
    unexpected = [k for k in differences if k not in ALLOWED_REPRODUCTION_DIFFERENCES]
    if unexpected:
        problems.append(f"provenance field(s) {unexpected} differ (only {sorted(ALLOWED_REPRODUCTION_DIFFERENCES)} may)")
    return {"ok": not problems, "exact_hash": committed["deterministic_sha256"] == new["deterministic_sha256"],
            "committed_sha256": committed["deterministic_sha256"], "rerun_sha256": new["deterministic_sha256"],
            "provenance_differences": differences, "problems": problems}


def _check_comparable(runs: dict) -> None:
    keys = sorted(runs)
    first = runs[keys[0]]["deterministic"]["provenance"]
    for key in keys:
        prov = runs[key]["deterministic"]["provenance"]
        for field in SHARED_PROVENANCE:
            if prov[field] != first[field]:
                raise ValueError(f"run {key} has a different {field}; runs are not comparable")
    for field, wanted in (("lexical_fingerprint", lambda k: k[0] in CANDIDATES),
                          ("perturbation_sha256", lambda k: k[2] == "dev_folded")):
        values = {runs[k]["deterministic"]["provenance"].get(field) for k in keys if wanted(k)}
        if len(values) > 1:
            raise ValueError(f"runs disagree on {field}: {sorted(map(str, values))}")


# --- tables -----------------------------------------------------------------------------------------------------


def _answerable(records: Sequence[dict]) -> dict[str, dict]:
    return {r["id"]: r for r in records if r["gold"]["type"] in INTENT_OF_GOLD}


def _cell(base: list[dict], cand: list[dict], metrics: list[str]) -> dict:
    cell = {"n": len(base), "too_small_to_interpret": len(base) < MIN_INTERPRETABLE_N}
    for metric in metrics:
        a, b = [r["metrics"][metric] for r in base], [r["metrics"][metric] for r in cand]
        cell[metric] = {**paired_bootstrap(a, b), "baseline_mean": mean(a), "candidate_mean": mean(b)}
    return cell


def paired_table(base_records: Sequence[dict], cand_records: Sequence[dict], metrics: list[str] = TABLE_METRICS) -> dict:
    """Candidate minus baseline, paired by item, per intent and per query style (and 'all' styles)."""
    base, cand = _answerable(base_records), _answerable(cand_records)
    if base.keys() != cand.keys():
        raise ValueError("the two runs cover different items")
    table: dict[str, dict] = {}
    for gold_type, intent in INTENT_OF_GOLD.items():
        ids = sorted(i for i in base if base[i]["gold"]["type"] == gold_type)
        if not ids:
            continue
        cells = {"all": _cell([base[i] for i in ids], [cand[i] for i in ids], metrics)}
        for style in sorted({base[i]["query_style"] for i in ids}):
            sub = [i for i in ids if base[i]["query_style"] == style]
            cells[style] = _cell([base[i] for i in sub], [cand[i] for i in sub], metrics)
        table[intent] = cells
    return table


def _pooled_rr(base_records: Sequence[dict], cand_records: Sequence[dict], ids: list[str]) -> dict:
    base, cand = exp1_rule.rr_by_id(base_records), exp1_rule.rr_by_id(cand_records)
    out = paired_bootstrap([base[i] for i in ids], [cand[i] for i in ids])
    return {**out, "ids": ids}


def exploratory(base_records: Sequence[dict], cand_records: Sequence[dict], questions: dict[str, str]) -> dict:
    """The two subsets that do not decide anything: the real khong_dau items and the items whose question
    has no diacritic at all (machine-detected), both on the original dev set."""
    answerable = _answerable(base_records)
    khong_dau = sorted(i for i, r in answerable.items() if r["query_style"] == "khong_dau")
    zero = sorted(i for i in answerable if not has_diacritics(questions[i]))
    return {"status": EXPLORATORY_STATUS, "real_khong_dau": _pooled_rr(base_records, cand_records, khong_dau),
            "zero_diacritic_questions": _pooled_rr(base_records, cand_records, zero)}


# --- co_dau items that got worse -------------------------------------------------------------------------------------


def fold_matches(question: str, text: str) -> dict:
    """How the query's words meet a document: exactly, or ONLY through folding (query "phí" vs document "phi")."""
    q_words = list(dict.fromkeys(re.findall(r"\w+", unicodedata.normalize("NFC", question).lower())))
    doc_words = set(re.findall(r"\w+", unicodedata.normalize("NFC", text).lower()))
    doc_tokens = set(tokenize(text))
    exact, fold_only = [], {}
    for word in q_words:
        if fold(word) not in doc_tokens:
            continue
        if word in doc_words:
            exact.append(word)
        else:
            fold_only[word] = sorted(w for w in doc_words if fold(w) == fold(word))
    return {"exact_matches": sorted(exact), "fold_only_matches": fold_only, "false_friend": bool(fold_only)}


def worsened_co_dau(base_records: Sequence[dict], cand_records: Sequence[dict], questions: dict[str, str],
                    texts: dict[str, str]) -> list[dict]:
    """Every co_dau item whose first gold rank got worse (not found = worst), with the documents that now
    outrank gold and did not in the baseline."""
    base, cand = _answerable(base_records), _answerable(cand_records)
    out = []
    for item_id in sorted(base):
        b, c = base[item_id], cand[item_id]
        if b["query_style"] != "co_dau":
            continue
        rank_b, rank_c = b["metrics"]["first_gold_rank"], c["metrics"]["first_gold_rank"]
        if rank_b is None or (rank_c is not None and rank_c <= rank_b):
            continue
        ahead_before = set(b["retrieved"][: rank_b - 1])
        ahead_now = c["retrieved"] if rank_c is None else c["retrieved"][: rank_c - 1]
        question = questions.get(item_id, "")
        displacers = []
        for key in ahead_now:
            if key in ahead_before:
                continue
            text = texts.get(key)
            displacers.append({"key": key, "text_missing": text is None, **fold_matches(question, text or "")})
        out.append({"id": item_id, "question": question, "baseline_rank": rank_b, "candidate_rank": rank_c,
                    "baseline_top10": b["retrieved"], "candidate_top10": c["retrieved"], "new_displacers": displacers})
    return out


# --- the report ------------------------------------------------------------------------------------------------------


def _records(runs: dict, mode: str, setting: str, queryset: str) -> list[dict]:
    return runs[(mode, setting, queryset)]["deterministic"]["items"]


def build_report(runs: dict, committed_baseline: dict, questions: dict[str, str], texts: dict[str, str]) -> dict:
    """`runs` is keyed by (mode, filters, queryset): hybrid and both candidates for filters none/routing on
    dev and dev_folded, plus optional (mode, 'oracle', 'dev') policy-only runs."""
    for key, run in runs.items():
        check_integrity(run, str(key))
    _check_comparable(runs)
    report: dict = {"rule_version": exp1_rule.RULE_VERSION,
                    "provenance": {k: runs[(BASELINE, "none", "dev")]["deterministic"]["provenance"][k]
                                   for k in SHARED_PROVENANCE},
                    "reproduction": reproduction_check(committed_baseline, runs[(BASELINE, "none", "dev")])}
    for section, queryset in (("dev", "dev"), ("perturbation", "dev_folded")):
        report[section] = {
            setting: {c: {"by_intent_and_style": paired_table(_records(runs, BASELINE, setting, queryset),
                                                               _records(runs, c, setting, queryset))}
                      for c in CANDIDATES}
            for setting in SETTINGS}
    verdicts = {c: exp1_rule.evaluate_candidate(_records(runs, BASELINE, "none", "dev_folded"),
                                                _records(runs, c, "none", "dev_folded"),
                                                _records(runs, BASELINE, "none", "dev"), _records(runs, c, "none", "dev"))
                for c in CANDIDATES}
    report["rule"] = {"candidates": verdicts, "choice": exp1_rule.choose(verdicts)}
    base_dev = _records(runs, BASELINE, "none", "dev")
    report["exploratory"] = {c: exploratory(base_dev, _records(runs, c, "none", "dev"), questions) for c in CANDIDATES}
    for name, setting in (("co_dau_worsened", "none"), ("co_dau_worsened_routing", "routing")):
        base_records = _records(runs, BASELINE, setting, "dev")
        report[name] = {c: worsened_co_dau(base_records, _records(runs, c, setting, "dev"), questions, texts)
                        for c in CANDIDATES}
    oracle = {}
    if (BASELINE, "oracle", "dev") in runs:
        base_oracle = _records(runs, BASELINE, "oracle", "dev")
        oracle = {c: {"by_intent_and_style": paired_table(base_oracle, _records(runs, c, "oracle", "dev"))}
                  for c in CANDIDATES if (c, "oracle", "dev") in runs}
    report["oracle_policy"] = oracle
    report["caveats"] = CAVEATS
    return report


# --- command line ----------------------------------------------------------------------------------------------------


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_runs(results_dir: Path) -> dict:
    runs = {}
    for mode in (BASELINE, *CANDIDATES):
        for setting in SETTINGS:
            for queryset in QUERYSETS:
                runs[(mode, setting, queryset)] = _read(results_dir / result_name(mode, setting, RETRIEVAL_INTENTS, queryset))
        oracle = results_dir / result_name(mode, "oracle", ("policy_question",))
        if oracle.is_file():
            runs[(mode, "oracle", "dev")] = _read(oracle)
    return runs


def load_texts() -> dict[str, str]:
    """key -> indexed text, from the active collection's payload."""
    from app.retrieval.manifest import active_collection
    from app.retrieval.store import connect

    client, texts, offset = connect(), {}, None
    while True:
        points, offset = client.scroll(active_collection(), limit=256, offset=offset, with_vectors=False,
                                       with_payload=["key", "text"])
        texts.update({p.payload["key"]: p.payload["text"] for p in points})
        if offset is None:
            return texts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results-dir", default="eval/results/phase4-exp1")
    parser.add_argument("--committed-baseline", default=str(COMMITTED_BASELINE))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        from eval.perturbation.dev_folded import PERTURBATION_PATH

        results_dir = resolve_results_dir(args.results_dir)
        questions = {e["id"]: e["original_question"] for e in _read(PERTURBATION_PATH)["items"]}
        report = build_report(load_runs(results_dir), _read(Path(args.committed_baseline)), questions, load_texts())
        write_result(results_dir / "comparison-exp1.json",
                     {"deterministic": report, "deterministic_sha256": canonical_sha256(report)}, args.overwrite)
    except (ValueError, FileNotFoundError, FileExistsError, KeyError) as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    choice = report["rule"]["choice"]
    print(f"comparison-exp1.json written; reproduction ok={report['reproduction']['ok']} "
          f"exact_hash={report['reproduction']['exact_hash']}; decision={choice['decision']} ({choice['reason']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
