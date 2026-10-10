"""Analysis of the Phase 6 dev run, from the cache (zero live calls): what the result file does not carry, written to `<prompt>.analysis.json`.

The official result (eval/run_phase6_eval.py) is scored by the pre-registered rules. This analysis replays the same items and cases through the same
harness, offline, and adds three things the owner asked to see next to the accuracy:

1. **The guardrail fallback rate per intent**, a primary quality metric: how often the generated answer was rejected and replaced by the deterministic one,
   against the number of answers the model wrote (and against the number of items). A high rate with a high accuracy means the numbers come from the
   fixed answers, not from the model.
2. **The known gap of the guardrails**: every LLM-written answer that states a size, a colour, a material or a number of days that the tool results do not
   support. This is a REPORTING heuristic, not a guardrail: it reads the text with patterns, it can miss a claim and it can report a harmless one, and the
   reader must look at the sentences it lists.
3. **The error types of the answer-v2 rule** (docs/DECISIONS-phase6.md, A17): E1 fallback by rule code, E2 missing gold SKU although it was retrieved,
   E3 missing gold clause although it was retrieved, each with its item ids.

It also checks itself against the official result (same replies, same fallback flags) and says so. When the code changed after the official run (the
A21 bug fix: a policy answer that says "no information" is no longer rejected, and a fallback quotes only what the rejected text used), the replay
differs from the official result on purpose: `before_after` then gives both tables, per intent, and lists every item whose answer changed.
Usage (Qdrant and Saleor up, cache complete):
    uv run python -m eval.analyze_phase6 --answer-prompt answer_v2
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from app.agent.guardrails import SKU, sentences
from app.agent.protocols import Answer
from app.config import REPO_ROOT
from app.llm.prompts import catalog_vocabulary
from app.retrieval.folding import fold
from eval.golden.loader import load_split
from eval.phase6.cases import load_cases
from eval.phase6_scoring import INTENTS
from eval.run_phase6_eval import build_harness, evaluate_case, evaluate_item
from eval.run_retrieval_eval import canonical_sha256, resolve_results_dir

RESULTS_SUBDIR = "eval/results/phase6"
_DAYS = re.compile(r"([0-9]+)\s*(?:ngày|ngay)\b", re.I)
_SIZE = re.compile(r"(?:size|cỡ|kích cỡ|kích thước)\s*(?:là\s*)?((?:[A-Za-z]{1,3}|[0-9]{2})(?:\s*(?:,|và|hoặc|/)\s*(?:[A-Za-z]{1,3}|[0-9]{2}))*)\b", re.I)
_SIZE_TOKEN = re.compile(r"\b(XXL|XL|XS|S|M|L|[0-9]{2}|Freesize)\b", re.I)
_MATERIAL_INTRO = re.compile(r"(?:chất liệu|làm từ|làm bằng|bằng chất|vải)\s+([^\s,.;:()]+(?:\s+[^\s,.;:()]+)?)", re.I)


def _sizes_in(sentence: str) -> set[str]:
    """Sizes the sentence claims ("size M", "cỡ 38, 39", "kích cỡ S, M và L"), upper-cased."""
    return {token.upper() for match in _SIZE.finditer(sentence) for token in _SIZE_TOKEN.findall(match.group(1))}


def _colour_claims(sentence: str, colours: list[str]) -> set[str]:
    """Colours the sentence claims: "màu X" for any catalog colour, or a multi-word colour anywhere ("xanh rêu", "đỏ đô")."""
    folded = fold(sentence)
    claimed = set()
    for colour in colours:
        key = fold(colour)
        if " " in key and re.search(rf"\b{re.escape(key)}\b", folded) or re.search(rf"\bmau\s+{re.escape(key)}\b", folded):
            claimed.add(colour)
    return {c for c in claimed if not any(c != o and fold(c) in fold(o) for o in claimed)}  # "xanh" inside "xanh rêu" is not a second colour


def _material_claims(sentence: str, materials: list[str]) -> set[str]:
    """Materials the sentence claims: introduced by "chất liệu"/"làm bằng"/..., or a multi-word material anywhere ("da lộn", "cotton pha spandex")."""
    folded = fold(sentence)
    claimed = set()
    for material in materials:
        key = fold(material)
        if " " in key and re.search(rf"\b{re.escape(key)}\b", folded):
            claimed.add(material)
    for match in _MATERIAL_INTRO.finditer(sentence):
        words = fold(match.group(1))
        for material in materials:
            if re.match(rf"{re.escape(fold(material))}\b", words):
                claimed.add(material)
    return {m for m in claimed if not any(m != o and fold(m) in fold(o) for o in claimed)}


def unsupported_claims(kind: str, text: str, evidence: dict, vocabulary) -> list[dict]:
    """What the text claims about size, colour, material or a number of days that the evidence does not support. [] if nothing was found."""
    found: list[dict] = []
    hits = {h["sku"]: h for h in evidence.get("hits", []) if "sku" in h} if kind == "products" else {}
    evidence_days = {int(n) for h in evidence.get("hits", []) for n in _DAYS.findall(h.get("text", ""))}
    for sentence in sentences(text):
        for number in sorted({int(n) for n in _DAYS.findall(sentence)}):
            if number not in evidence_days:
                found.append({"type": "days", "claim": f"{number} ngày", "sentence": sentence})
        if kind != "products":
            continue
        named = [hits[s] for s in SKU.findall(sentence) if s in hits] or list(hits.values())  # the products the sentence is about, or all of them
        supported = {"size": {size.upper() for h in named for size in h["sizes"]}, "color": {h["color"] for h in named}, "material": {h["material"] for h in named}}
        for size in sorted(_sizes_in(sentence) - supported["size"]):
            found.append({"type": "size", "claim": size, "sentence": sentence})
        for colour in sorted(_colour_claims(sentence, list(vocabulary.colors)) - supported["color"]):
            found.append({"type": "color", "claim": colour, "sentence": sentence})
        for material in sorted(_material_claims(sentence, list(vocabulary.materials)) - supported["material"]):
            found.append({"type": "material", "claim": material, "sentence": sentence})
    return found


# --- the replay ------------------------------------------------------------------------------------------------------------------------------


def error_types(items: list[dict]) -> dict:
    """E1, E2, E3 of the answer-v2 rule (A17), with the ids of the items they affect."""
    e1: dict[str, list[str]] = defaultdict(list)
    for item in items:
        for rule in item.get("fallback_rules", []):
            e1[rule].append(item["id"])
    e2 = [i["id"] for i in items if i["intent"] == "product_search" and i["kind"] == "skus" and not i["correct"] and i.get("gold_in_retrieved")]
    e3 = [i["id"] for i in items if i["intent"] == "policy_question" and not i["correct"] and i.get("gold_in_retrieved")]
    return {"E1_fallback_by_rule": {rule: ids for rule, ids in sorted(e1.items())}, "E2_missing_sku_though_retrieved": e2, "E3_missing_citation_though_retrieved": e3}


def _sources(rows: list[dict], answered: dict[str, bool]) -> dict:
    """Of the correct items: how many were answered by the text the model wrote, by the deterministic answer that replaced it, by the fixed reply sent
    because the model said it had no information, or by a fixed reply with no model answer at all."""
    correct = [r for r in rows if r["correct"]]
    fallback = sum(1 for r in correct if answered.get(r["id"]) and r["fallback"])
    declined = sum(1 for r in correct if answered.get(r["id"]) and not r["fallback"] and r.get("declined"))
    model = sum(1 for r in correct if answered.get(r["id"]) and not r["fallback"] and not r.get("declined"))
    return {"model_text": model, "fallback_answer": fallback, "declined_reply": declined, "fixed_reply": len(correct) - model - fallback - declined}


def per_intent_table(items: list[dict], answered: dict[str, bool]) -> dict:
    table = {}
    for intent in INTENTS:
        rows = [i for i in items if i["intent"] == intent]
        made = [i for i in rows if answered.get(i["id"])]
        fallbacks = [i for i in made if i["fallback"]]
        declines = [i for i in made if i.get("declined") and not i["fallback"]]  # the model said "no information": nothing was rejected
        table[intent] = {"items": len(rows), "correct": sum(i["correct"] for i in rows), "accuracy": (sum(i["correct"] for i in rows) / len(rows)) if rows else None,
                         "answers_written_by_the_model": len(made), "fallbacks": len(fallbacks),
                         "fallback_rate_of_answers": (len(fallbacks) / len(made)) if made else None,
                         "fallback_rate_of_items": (len(fallbacks) / len(rows)) if rows else None,
                         "declines": len(declines), "items_with_decline": [i["id"] for i in declines],
                         "correct_by_source": _sources(rows, answered), "items_with_fallback": [i["id"] for i in fallbacks]}
    made_all = [i for i in items if answered.get(i["id"])]
    table["all"] = {"items": len(items), "correct": sum(i["correct"] for i in items), "accuracy": (sum(i["correct"] for i in items) / len(items)) if items else None,
                    "answers_written_by_the_model": len(made_all), "fallbacks": sum(i["fallback"] for i in made_all),
                    "declines": sum(1 for i in made_all if i.get("declined") and not i["fallback"]), "correct_by_source": _sources(items, answered),
                    "fallback_rate_of_answers": (sum(i["fallback"] for i in made_all) / len(made_all)) if made_all else None}
    return table


def before_after(before: list[dict], after: list[dict], answered: dict[str, bool]) -> dict:
    """The same items scored by two versions of the code: the per-intent table of each, and every item whose reply, fallback flag, decline flag or
    score is not the same. `before` is the official result, `after` the replay with the code as it is now."""
    old = {i["id"]: i for i in before}
    changed = []
    for item in after:
        was = old[item["id"]]
        if (was["reply"], was["fallback"], bool(was.get("declined")), was["correct"]) != (item["reply"], item["fallback"], bool(item.get("declined")), item["correct"]):
            changed.append({"id": item["id"], "intent": item["intent"], "reply_changed": was["reply"] != item["reply"], "fallback": [was["fallback"], item["fallback"]],
                            "declined_after": bool(item.get("declined")), "correct": [was["correct"], item["correct"]]})
    table_before, table_after = per_intent_table(before, answered), per_intent_table(after, answered)
    return {"per_intent": {name: {"before": table_before[name], "after": table_after[name]} for name in table_after}, "changed_items": changed}


def replay(args, tmp: Path) -> dict:
    harness, _, _ = build_harness(args, tmp)
    records: list[dict] = []
    compose = harness.answerer.compose

    def recording(kind, question, evidence):
        answer: Answer = compose(kind, question, evidence)
        records.append({"kind": kind, "question": question, "evidence": evidence, "text": answer.text, "fallback": answer.fallback_reason is not None,
                        "declined": answer.declined})
        return answer

    harness.answerer.compose = recording
    vocabulary, items, answered, claims, gold_in = catalog_vocabulary(), [], {}, [], {}
    for item in sorted(load_split("dev"), key=lambda i: i.id):
        before = len(records)
        record = evaluate_item(item, harness)
        made = records[before:]
        answered[item.id] = bool(made)
        source = item.gold_source
        if item.intent == "product_search" and source.type == "skus":
            record["gold_in_retrieved"] = bool(set(source.skus) & set(record["retrieved_skus"]))
        if item.intent == "policy_question":
            record["gold_in_retrieved"] = [source.document, int(source.clause)] in record["retrieved_clauses"]
        items.append(record)
        for r in made:
            if not r["fallback"]:  # a fallback is built from the evidence and cannot state anything the evidence does not
                claims += [{"id": item.id, "kind": r["kind"], **c} for c in unsupported_claims(r["kind"], r["text"], r["evidence"], vocabulary)]
    case_records, case_claims, case_answers, case_fallbacks, case_declines = [], [], 0, 0, 0
    for case in load_cases().cases:
        before = len(records)
        case_records.append(evaluate_case(case, harness))
        for r in records[before:]:
            case_answers += 1
            case_fallbacks += r["fallback"]
            case_declines += r["declined"]
            if not r["fallback"]:
                case_claims += [{"id": case.id, "kind": r["kind"], **c} for c in unsupported_claims(r["kind"], r["text"], r["evidence"], vocabulary)]
    return {"items": items, "answered": answered, "claims": claims, "cases": case_records, "case_claims": case_claims, "case_answers": case_answers,
            "case_fallbacks": case_fallbacks, "case_declines": case_declines,
            "live_calls": harness.generator.inner.live_calls}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--answer-prompt", default="answer_v1")
    parser.add_argument("--results-dir", default=RESULTS_SUBDIR)
    args = parser.parse_args(argv)
    results_dir = resolve_results_dir(args.results_dir)
    official_path = results_dir / f"{args.answer_prompt}.json"
    if not official_path.exists():
        print(f"REFUSED: {official_path} does not exist: run eval/run_phase6_eval.py first", file=sys.stderr)
        return 1
    official = json.loads(official_path.read_text(encoding="utf-8"))
    run_args = SimpleNamespace(live=False, max_live_calls=0, answer_prompt=args.answer_prompt)  # offline: a call would be a cache miss and a stop
    with tempfile.TemporaryDirectory(prefix="shopmate-phase6-analysis-") as tmp:
        outcome = replay(run_args, Path(tmp))
    assert outcome["live_calls"] == 0, "the analysis must make no live call"
    official_items = {i["id"]: i for i in official["deterministic"]["items"]}
    same = all(i["reply"] == official_items[i["id"]]["reply"] and i["fallback"] == official_items[i["id"]]["fallback"] and i["correct"] == official_items[i["id"]]["correct"]
               for i in outcome["items"]) and len(outcome["items"]) == len(official_items)
    changes = before_after([official_items[i["id"]] for i in outcome["items"]], outcome["items"], outcome["answered"])
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    result = {
        "provenance": {"official_result_sha256": official["deterministic_sha256"], "official_result_commit": official["deterministic"]["provenance"]["git_commit"],
                       "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip(),
                       "tracked_files_modified": bool(status), "live_calls": outcome["live_calls"],
                       "this_file_is_a_reporting_analysis": "the claim detector is a heuristic that reads text; it is not a guardrail and not a score"},
        "replay_agrees_with_the_official_result": same,
        "per_intent": per_intent_table(outcome["items"], outcome["answered"]),
        "before_after_the_code_change": {"before": "the official result file, produced by the code of provenance.official_result_commit", "after": "this replay, the code as it is now",
                                         **changes},
        "multiturn_fallbacks": {"answers": outcome["case_answers"], "fallbacks": outcome["case_fallbacks"], "declines": outcome["case_declines"],
                                "claims_unsupported": len(outcome["case_claims"])},
        "unsupported_claims": {"dev_items": outcome["claims"], "multiturn_cases": outcome["case_claims"],
                               "items_affected": sorted({c["id"] for c in outcome["claims"]}), "detector": "size, colour, material and days patterns of eval/analyze_phase6.py"},
        "error_types": error_types(outcome["items"]),
    }
    result["analysis_sha256"] = canonical_sha256({k: v for k, v in result.items() if k != "provenance"})
    out = results_dir / f"{args.answer_prompt}.analysis.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{out.name}: replay agrees with the official result: {same}; live calls {outcome['live_calls']}; sha256={result['analysis_sha256'][:12]}")
    for intent, row in result["per_intent"].items():
        rate = row["fallback_rate_of_answers"]
        print(f"  {intent:16s} accuracy {row.get('accuracy', 0) or 0:.3f}  answers {row['answers_written_by_the_model']:3d}  fallbacks {row['fallbacks']:3d}  "
              f"declines {row['declines']:3d}  fallback rate " + ("n/a" if rate is None else f"{rate:.3f}"))
    print(f"  items whose answer differs from the official result: {[c['id'] for c in changes['changed_items']]}")
    print(f"  answers with an unsupported size/colour/material/days claim: {len(result['unsupported_claims']['items_affected'])} dev items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
