"""Render docs/RESULTS-final.md (Phase 8, turn 2) from the committed result files, so that no number in it is typed by hand.

    uv run python -m scripts.render_results_final            # write docs/RESULTS-final.md
    uv run python -m scripts.render_results_final --check    # exit 1 if the committed file is not what the result files give
    uv run python -m scripts.render_results_final --readme   # print the test cells that the README table uses

It reads, and only reads:
  eval/results/phase8_test/final_evaluation.json, attempts.jsonl   the one run on the test split
  eval/results/phase8_rehearsal/final_evaluation.json              the dev column: "the results of the dev run for reference (the same code path)" of
                                                                   docs/PHASE8-PREREG.md section 2
  docs/PHASE8-PREREG.md, docs/DECISIONS-phase8.md                  section 10 and decision D8.6, copied word for word
It never opens the golden file or its loader: the test split is not read again (PREREG section 7). The words of the discussion are written here;
every figure in them is filled in from the files above. tests/test_results_final.py compares the committed document with this output.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from app.config import REPO_ROOT

TEST_DIR = REPO_ROOT / "eval" / "results" / "phase8_test"
TEST_RESULT = TEST_DIR / "final_evaluation.json"
TEST_ATTEMPTS = TEST_DIR / "attempts.jsonl"
DEV_RESULT = REPO_ROOT / "eval" / "results" / "phase8_rehearsal" / "final_evaluation.json"
PREREG = REPO_ROOT / "docs" / "PHASE8-PREREG.md"
DECISIONS = REPO_ROOT / "docs" / "DECISIONS-phase8.md"
OUTPUT = REPO_ROOT / "docs" / "RESULTS-final.md"

FIGURES = ("all", "without_excluded")
# (metric, where it is in the file, the population as PREREG section 2 words it, how it is printed)
METRICS = (
    ("router_accuracy", "metrics", "every item", "prop"),
    ("extractor_exact_match", "metrics", "product_search items", "prop"),
    ("product_mrr", "metrics", "product items with SKU gold", "mrr"),
    ("policy_mrr", "metrics", "policy items", "mrr"),
    ("no_answer_correct", "metrics", "product items with no matching product", "prop"),
    ("guardrail_fallback_rate", "metrics", "items with an LLM-written answer (first turn)", "prop"),
    ("answer_correct", "metrics", "every item", "prop"),
    ("latency_p50_ms", "latency", "every item (one first turn each)", "ms"),
    ("latency_p95_ms", "latency", "every item (one first turn each)", "ms"),
    ("cost_per_1000_usd", "metrics", "every item", "usd"),
    ("product_mrr_gold_routing", "metrics", "product items with SKU gold, intent given", "mrr"),
)
KIND = {name: kind for name, _, _, kind in METRICS}
WHERE = {name: where for name, where, _, _ in METRICS}
LOWER_IS_BETTER = frozenset({"guardrail_fallback_rate", "latency_p50_ms", "latency_p95_ms", "cost_per_1000_usd"})
SECONDARY_COLUMNS = ("router_accuracy", "answer_correct", "extractor_exact_match", "no_answer_correct", "guardrail_fallback_rate")
SECONDARY_MEANS = ("product_mrr", "policy_mrr")
INTENTS = ("product_search", "policy_question", "order_status", "create_ticket", "out_of_scope", "smalltalk")


# --- reading ----------------------------------------------------------------------------------------------------------------


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def entry(doc: dict, figure: str, name: str) -> dict:
    if WHERE[name] == "latency":
        return doc["measurements"]["latency_ms"][figure][name]
    return doc["deterministic"]["metrics"][figure][name]


def attempts(path: Path = TEST_ATTEMPTS) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def prereg_section_10(text: str) -> str:
    start = text.index("## 10. Disclosed incidents and known limits")
    return text[start:].split("\n", 1)[1].strip("\n")


def decision_d86(text: str) -> str:
    match = re.search(r"^### D8\.6 .*?(?=^### D8\.\d|\Z)", text, re.S | re.M)
    if match is None:
        raise ValueError("D8.6 not found in docs/DECISIONS-phase8.md")
    return match.group(0).strip("\n")


def registered(text: str, pattern: str) -> re.Match:
    """A figure of PREREG, read from its text (a missing one is an error, never a guess)."""
    match = re.search(pattern, text)
    if match is None:
        raise ValueError(f"not found in docs/PHASE8-PREREG.md: {pattern}")
    return match


# --- numbers as text ----------------------------------------------------------------------------------------------------------


def digits(kind: str) -> int:
    return {"prop": 3, "mrr": 3, "ms": 1, "usd": 4}[kind]


def value(kind: str, e: dict) -> str:
    est = f"{e['estimate']:.{digits(kind)}f}"
    return f"{est} ({e['successes']}/{e['n']})" if kind == "prop" else f"{est} (n={e['n']})"


def interval(kind: str, e: dict) -> str:
    d = digits(kind)
    return f"[{e['ci_low']:.{d}f}, {e['ci_high']:.{d}f}]"


def cell(kind: str, e: dict) -> str:
    return f"{value(kind, e)} {interval(kind, e)}"


def outside(dev_estimate: float, e: dict) -> bool:
    return dev_estimate < e["ci_low"] or dev_estimate > e["ci_high"]


def proportion(p: dict | None) -> str:
    if not p or not p["n"]:
        return "–"
    return f"{p['successes']}/{p['n']} = {p['estimate']:.3f} [{p['ci_low']:.3f}, {p['ci_high']:.3f}]"


def mean(m: dict | None) -> str:
    return "–" if not m or not m["n"] else f"{m['estimate']:.3f} (n={m['n']})"


def where_is_dev(test: dict, dev: dict, name: str) -> str:
    """'inside' or 'outside' the registered interval of the test result (all items), for the dev point estimate."""
    return "outside" if outside(entry(dev, "all", name)["estimate"], entry(test, "all", name)) else "inside"


# --- the document ---------------------------------------------------------------------------------------------------------------


def results_table(test: dict, dev: dict) -> list[str]:
    n_all = test["deterministic"]["n_items"]
    n_without = n_all - test["deterministic"]["excluded_matched"]
    lines = [f"| metric | population | dev ({dev['deterministic']['n_items']} items) | test, all items ({n_all}) | test, without the excluded item ({n_without}) |",
             "|---|---|---|---|---|"]
    for name, _, population, kind in METRICS:
        dev_cell = cell(kind, entry(dev, "all", name))
        test_cells = [cell(kind, entry(test, figure, name)) for figure in FIGURES]
        lines.append(f"| {name} | {population} | {dev_cell} | {test_cells[0]} | {test_cells[1]} |")
    return lines


def gap_section(test: dict, dev: dict) -> list[str]:
    lines = ["| metric | dev estimate | test interval, all items | dev outside it | test interval, without the excluded item | dev outside it |", "|---|---|---|---|---|---|"]
    listed: list[str] = []
    for name, _, _, kind in METRICS:
        dev_e = entry(dev, "all", name)
        intervals = [interval(kind, entry(test, figure, name)) for figure in FIGURES]
        flags = [outside(dev_e["estimate"], entry(test, figure, name)) for figure in FIGURES]
        lines.append(f"| {name} | {dev_e['estimate']:.{digits(kind)}f} | {intervals[0]} | {'**yes**' if flags[0] else 'no'} | {intervals[1]} | {'**yes**' if flags[1] else 'no'} |")
        if any(flags):
            where_text = "both figures" if all(flags) else ("the figure on all items only" if flags[0] else "the figure without the excluded item only")
            test_e = entry(test, "all" if flags[0] else "without_excluded", name)
            better = (test_e["estimate"] < dev_e["estimate"]) if name in LOWER_IS_BETTER else (test_e["estimate"] > dev_e["estimate"])
            tail = "the test result is the better one here and is reported as it is" if better else "the test result is the worse one"
            listed.append(f"- `{name}`: the dev estimate {dev_e['estimate']:.{digits(kind)}f} lies outside the registered 95% interval of the test result "
                          f"({where_text}: {intervals[0] if flags[0] else intervals[1]}); the dev items were seen while the prompts and the scoring rules were written, "
                          f"so the dev number was optimistic ({tail}).")
    out = lines + [""]
    if listed:
        out += ["Metrics with a large dev-test gap, by the rule of PREREG section 7 (the dev point estimate lies outside the registered 95% interval of the test result):", ""] + listed
    else:
        out += ["No metric has a large dev-test gap by the rule of PREREG section 7."]
    return out


def secondary_table(test: dict, groups: tuple[str, ...], key: str) -> list[str]:
    secondary = test["deterministic"]["secondary"][key]
    header = "| group | items | " + " | ".join(SECONDARY_COLUMNS + SECONDARY_MEANS) + " |"
    lines = [header, "|" + "---|" * (2 + len(SECONDARY_COLUMNS) + len(SECONDARY_MEANS))]
    for group in groups:
        g = secondary[group]
        label = f"{group} (small)" if g["too_small_to_interpret"] else group
        cells = [proportion(g["proportions"].get(c)) for c in SECONDARY_COLUMNS] + [mean(g["means"].get(m)) for m in SECONDARY_MEANS]
        lines.append(f"| {label} | {g['n_items']} | " + " | ".join(cells) + " |")
    return lines


def procedure(test: dict, dev: dict, prereg: str) -> list[str]:
    d, m = test["deterministic"], test["measurements"]
    prov = d["provenance"]
    runs = attempts()
    kinds = [a["kind"] for a in runs]
    last = [a for a in runs if a["kind"] == "run"][-1]
    estimate = registered(prereg, r"\*\*Estimate: (\d+) Gemini requests\*\*").group(1)
    ceiling = registered(prereg, r"the ceiling of the design is (\d+)").group(1)
    bounds_held = all(b["held"] for b in d["phase6"]["bounds"].values())
    dev_prov = dev["deterministic"]["provenance"]
    typed = {name: registered(prereg, rf"\| {name} \(replayed latencies\) \| n=\d+ \| (\d+) \|").group(1) for name in ("latency_p50_ms", "latency_p95_ms")}
    file_dev = {name: entry(dev, "all", name) for name in typed}
    same_list = all(outside(float(typed[n]), entry(test, f, n)) == outside(file_dev[n]["estimate"], entry(test, f, n)) for n in typed for f in FIGURES)
    return [
        "| | |", "|---|---|",
        "| result file | `eval/results/phase8_test/final_evaluation.json` (every per-item row, the cross-checks, the attempts), `final_evaluation.md`, `attempts.jsonl` |",
        f"| `deterministic_sha256` | `{test['deterministic_sha256']}` |",
        f"| code measured | commit `{prov['git_commit']}`, working tree clean: {not prov['git_dirty']} |",
        f"| freeze | `{prov['freeze']['ref']}` = commit `{prov['freeze']['commit']}`; the script's own guard (PREREG section 9) accepted HEAD as a descendant of it with no frozen file different |",
        f"| dev run checked against | `{prov['dev_run_sha']}` |",
        f"| model | `{prov['model_id']}`, prompts `{prov['answer_prompt']['name']}` `{prov['answer_prompt']['sha256'][:8]}…`, router `{prov['router_prompt_sha256'][:8]}…`, extractor `{prov['filters_prompt_sha256'][:8]}…` |",
        f"| test split | {d['n_items']} items, `items_sha256` `{prov['items_sha256']}` (equal to the value registered in PREREG section 1: {prov['items_sha256'] in prereg}) |",
        f"| excluded item (A0) | matched {d['excluded_matched']} item (PREREG section 8 expects exactly one); its id is printed nowhere in the summary and was not looked up |",
        f"| attempts | {kinds.count('run')} run, {kinds.count('refusal')} refusals; the run ended `{last['ended']}`, `rerun_allowed` {str(last['rerun_allowed']).lower()}; no rerun was needed and none was made |",
        f"| Gemini requests | {m['live_calls_this_run']} live, {m['cache_hits_this_run']} answered from the cache; PREREG section 5 estimated {estimate} with a ceiling of {ceiling} |",
        f"| wall time | {m['wall_seconds']} s |",
        f"| cross-checks | answer score equals the Phase 6 section: {d['cross_checks']['answer_correct_matches_phase6']}; cost equals the Phase 6 section: {d['cross_checks']['cost_matches_phase6']} |",
        f"| bounds of the agent | every bound held (steps, clarifying questions, searches, failed verifications, confirmation asks, tickets without confirmation): {bounds_held} |",
        f"| dev column | `eval/results/phase8_rehearsal/final_evaluation.json`, the rehearsal that PREREG section 2 names as the dev reference (split `{dev['deterministic']['split']}`, "
        f"`git_dirty` {str(dev_prov['git_dirty']).lower()}: it was run with the tool's own uncommitted files, D8.3); its latencies are replayed, not measured |",
        f"| dev latency, typed in PREREG and in that file | PREREG: p50 {typed['latency_p50_ms']} ms, p95 {typed['latency_p95_ms']} ms; file: p50 {file_dev['latency_p50_ms']['estimate']} ms, "
        f"p95 {file_dev['latency_p95_ms']['estimate']} ms (the replay measures retrieval time again on every run). The large gaps of section 2 are "
        f"{'the same' if same_list else 'NOT the same'} with either pair |",
    ]


def discussion(test: dict, dev: dict) -> list[str]:
    d = test["deterministic"]
    m_all = {name: entry(test, "all", name) for name, *_ in METRICS}
    m_dev = {name: entry(dev, "all", name) for name, *_ in METRICS}
    ext = d["phase5"]["extraction"]
    ext_all, ext_wo = ext["metrics"], ext["metrics_without_in_stock"]
    fields = ext_all["fields"]
    guard = d["phase6"]["guardrails"]
    misses = [r for r in d["per_item"] if not r["correct"]]
    miss_text = ", ".join(f"`{r['id']}` ({r['intent']}, {r['author']}, check `{r['check']}`, guardrail fallback {str(r['fallback']).lower()})" for r in misses)
    same_items = bool(misses) and all(r["fallback"] for r in misses) and len(misses) == guard["fallbacks"]
    rules = ", ".join(f"`{k}` {v}" for k, v in guard["rules"].items())
    router, answer, no_answer = m_all["router_accuracy"], m_all["answer_correct"], m_all["no_answer_correct"]
    product = m_all["product_mrr"]
    at_rank_1 = "every product item with a SKU as gold had it at rank 1" if product["estimate"] == 1.0 else "the product MRR is below 1"
    return [
        "Written after the numbers above and read from the same file. Nothing here is a pass or a fail: PREREG section 2 registers no threshold.", "",
        f"- **Small samples first.** {d['n_items']} items; the metrics rest on {m_all['extractor_exact_match']['n']} product_search items, {m_all['policy_mrr']['n']} policy items, "
        f"{product['n']} product items with a SKU as gold and {no_answer['n']} `no_answer` items. One item moves a proportion over {no_answer['n']} items by "
        f"{1 / no_answer['n']:.2f}. Most intervals are wide, and a perfect score does not say that the system is perfect (PREREG section 7).",
        f"- **Router.** {router['successes']}/{router['n']} with a Wilson interval {interval('prop', router)}: a result with an interval, not a proof, and the router's dev score was saturated "
        f"({m_dev['router_accuracy']['successes']}/{m_dev['router_accuracy']['n']}; PREREG section 10). Misrouted questions: {router['n'] - router['successes']}.",
        f"- **Extractor.** {ext_all['exact_match']}/{ext_all['n']} exact matches against {m_dev['extractor_exact_match']['successes']}/{m_dev['extractor_exact_match']['n']} on dev; "
        f"the dev estimate is {where_is_dev(test, dev, 'extractor_exact_match')} the test interval (section 2). The output file also holds the registered variant without `in_stock`, the field of the "
        f"label conflict of `docs/GOLDEN-V2-ISSUES.md` #1, which is not corrected for in the registered figure: {ext_wo['exact_match']}/{ext_wo['n']}. So "
        f"{ext_wo['exact_match'] - ext_all['exact_match']} of the {ext_all['n'] - ext_all['exact_match']} misses are misses of `in_stock` alone "
        f"(field table: `in_stock` false negatives {fields['in_stock']['fn']}, false positives {fields['in_stock']['fp']}). The other errors of the field table are `kind` "
        f"(false positives {fields['kind']['fp']}, false negatives {fields['kind']['fn']}) and `color` (false positives {fields['color']['fp']}, false negatives {fields['color']['fn']}). "
        "This report does not read the questions to say why: the split is not opened again.",
        f"- **Retrieval, full pipeline.** `product_mrr` {product['estimate']:.3f} (n={product['n']}): {at_rank_1}; `product_mrr_gold_routing` {m_all['product_mrr_gold_routing']['estimate']:.3f}. "
        f"`policy_mrr` {m_all['policy_mrr']['estimate']:.3f} (n={m_all['policy_mrr']['n']}), the dev estimate {where_is_dev(test, dev, 'policy_mrr')} its interval "
        f"{interval('mrr', m_all['policy_mrr'])}, which shows how little {m_all['policy_mrr']['n']} items say.",
        f"- **`no_answer`.** {no_answer['successes']}/{no_answer['n']} against {m_dev['no_answer_correct']['successes']}/{m_dev['no_answer_correct']['n']} on dev. "
        "As PREREG section 10 says, this rule counts an empty retrieval after a product route and does not measure how good the offer that follows is.",
        f"- **Guardrail and the answer score.** The guardrail replaced the written answer on {guard['fallbacks']} of {guard['answers']} answers "
        f"({m_all['guardrail_fallback_rate']['estimate']:.3f}; dev {m_dev['guardrail_fallback_rate']['successes']}/{m_dev['guardrail_fallback_rate']['n']}); the rules that fired: {rules}. "
        "Whether these were false alarms or caught errors needs the replies, and this report does not read them. "
        f"`answer_correct` is {answer['successes']}/{answer['n']}; the items scored incorrect are {miss_text}. "
        + ("The items scored incorrect are the items on which the guardrail replaced the answer: the same items. " if same_items else "")
        + "This list comes from the `per_item` rows of the result file and is exploratory, not a registered analysis.",
        f"- **Latency and cost.** p50 {m_all['latency_p50_ms']['estimate']:.1f} ms and p95 {m_all['latency_p95_ms']['estimate']:.1f} ms of one live first turn on the free tier "
        f"(dev: replayed latencies; the dev p50 is {where_is_dev(test, dev, 'latency_p50_ms')} its test interval and the dev p95 is {where_is_dev(test, dev, 'latency_p95_ms')} its test interval); "
        f"{m_all['cost_per_1000_usd']['estimate']:.3f} USD per 1,000 conversations at the theoretical paid-tier price, the free tier costing 0 "
        f"(the dev estimate is {where_is_dev(test, dev, 'cost_per_1000_usd')} its interval).",
        "- **By author and by intent.** The tables of section 3 say where the totals come from. They are descriptions of these items; no difference between rows is a finding about authors or intents (PREREG section 3).",
        "- **What is not claimed.** That the system is as good as the dev numbers (the dev items were seen), that the test split is representative of real customers (everything is synthetic, PREREG section 10), "
        "or that a metric is \"good\" or \"bad\": there is no threshold. The test split is not opened again until a golden v2 exists.",
    ]


def render() -> str:
    test, dev = load(TEST_RESULT), load(DEV_RESULT)
    prereg = PREREG.read_text(encoding="utf-8")
    decisions = DECISIONS.read_text(encoding="utf-8")
    d = test["deterministic"]
    n_all, n_without = d["n_items"], d["n_items"] - d["excluded_matched"]
    prices = d["provenance"]["prices_usd_per_million_tokens"]
    small = registered(d["secondary"]["description"], r"fewer than (\d+) items").group(1)
    boot = d["intervals"]["mrr_latency_cost"]
    out: list[str] = [
        "# Final evaluation: the single run on the test split (Phase 8)", "",
        f"One run, once, under `docs/PHASE8-PREREG.md`: {n_all} test items, `{d['provenance']['model_id']}` on the Gemini free tier, commit `{d['provenance']['git_commit'][:12]}`, "
        f"`deterministic_sha256` `{test['deterministic_sha256'][:16]}…`. **Every number in this file is generated** from `eval/results/phase8_test/final_evaluation.json` "
        "(and the dev column from `eval/results/phase8_rehearsal/final_evaluation.json`) by `scripts/render_results_final.py`; `tests/test_results_final.py` fails if the two differ.", "",
        "## 1. Results", "",
        f"Two figures per metric, as registered: on all {n_all} items, and without the one item whose question hash is the one recorded in A0 ({n_without} items; "
        f"{d['excluded_matched']} item matched). A proportion is `estimate (successes/n)` with its Wilson 95% interval; an MRR, a latency percentile and the cost are `estimate (n=items)` "
        f"with a percentile-bootstrap 95% interval ({boot['resamples']:,} resamples, seed {boot['seed']}). "
        "The dev column is optimistic: the prompts and the scoring rules were written with the dev items in view. There is no pass/fail threshold for any metric.", "",
        *results_table(test, dev), "",
        "Latency: the test figure is that of one live first turn per item, the dev figure is a replay of recorded latencies, and neither is comparable with the Phase 6 latency, "
        "which is over all the turns of the dev run including the multi-turn cases. "
        f"Cost: the theoretical paid-tier price of the calls ({prices['input']} USD input, {prices['output']} USD output per million tokens); the free tier cost 0.", "",
        "## 2. Large dev-test gaps", "",
        "The rule is fixed in PREREG section 7: a metric has a large gap when its dev point estimate lies outside the registered 95% interval of the test result. It is applied here to both figures.", "",
        *gap_section(test, dev), "",
        "## 3. Registered secondary analysis: by author and by intent", "",
        "Descriptive, on all items only (not on the figure without the excluded item), as PREREG section 3 registers. Proportions as successes/n = estimate [Wilson 95%]; the MRRs as a mean with n and no interval. "
        f"**The samples are small**: a group of fewer than {small} items is marked `(small)` and is not read as a result; no test of a difference between groups is made, and none is claimed.", "",
        "### By author", "", *secondary_table(test, ("human", "agent_draft"), "by_author"), "",
        "### By intent", "", *secondary_table(test, INTENTS, "by_intent"), "",
        "## 4. How the run went", "", *procedure(test, dev, prereg), "",
        "Before the run, the first precondition check of this turn stopped because Qdrant was not running (PREREG section 4: the command is not started with a service down). "
        "Nothing had been started, no test item was read and no Gemini request was made; after Qdrant was started the checks were repeated from the start and passed, "
        "including the two external-state fingerprints (`registered values: equal`). That stop is not an attempt and is not in `attempts.jsonl`. "
        "An earlier try of turn 2 stopped at its precondition check because the tag `v1.0-frozen` had been made on the wrong commit; that is D8.6, copied in section 6.", "",
        "## 5. Discussion", "", *discussion(test, dev), "",
        "## 6. Incidents and known limits", "",
        "PREREG section 10 and decision D8.6, **copied word for word** (`tests/test_results_final.py` compares them with the two files). Section 10 was written before any test number existed; "
        "it is not edited in the light of the results.", "",
        prereg_section_10(prereg), "",
        decision_d86(decisions), "",
    ]
    return "\n".join(out)


# --- the README cells ------------------------------------------------------------------------------------------------------------


def readme_cells() -> dict[str, str]:
    """The test cells of the README results table, one per row; the README must contain them exactly (tests/test_results_final.py)."""
    test, dev = load(TEST_RESULT), load(DEV_RESULT)

    def both(name: str, template) -> str:
        first, second = (template(entry(test, figure, name)) for figure in FIGURES)
        return f"{first}; without the excluded item {second}"

    def prop(e: dict) -> str:
        return f"{e['successes']} / {e['n']} {interval('prop', e)}"

    def mrr(e: dict) -> str:
        return f"{e['estimate']:.3f} (n={e['n']})"

    def seconds(name: str) -> str:
        return " / ".join(f"{entry(test, figure, name)['estimate'] / 1000:.1f} s" for figure in FIGURES)

    ext_wo = test["deterministic"]["phase5"]["extraction"]["metrics_without_in_stock"]
    dev_g = entry(dev, "all", "guardrail_fallback_rate")
    cells = {
        "router": both("router_accuracy", prop),
        "extraction": both("extractor_exact_match", prop) + f"; {ext_wo['exact_match']} / {ext_wo['n']} without `in_stock`",
        "no_answer": both("no_answer_correct", prop),
        "answers": both("answer_correct", prop),
        "guardrail": both("guardrail_fallback_rate", prop) + f" (first turns of the items; dev on the same basis {dev_g['successes']} / {dev_g['n']})",
        "cost": both("cost_per_1000_usd", lambda e: f"{e['estimate']:.3f} USD {interval('prop', e)}"),
        "latency": f"p50 {seconds('latency_p50_ms')}, p95 {seconds('latency_p95_ms')} (all items / without the excluded item)",
        "pipeline_mrr": f"product {both('product_mrr', mrr)}; policy {both('policy_mrr', mrr)}",
    }
    dev_p, dev_q = entry(dev, "all", "product_mrr"), entry(dev, "all", "policy_mrr")
    cells["dev_pipeline_mrr"] = f"{dev_p['estimate']:.3f} (n={dev_p['n']}) / {dev_q['estimate']:.3f} (n={dev_q['n']})"
    router = entry(test, "all", "router_accuracy")
    cells["router_limitation"] = f"{router['successes']} / {router['n']} (Wilson 95% {interval('prop', router)})"
    return cells


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.render_results_final", description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 if docs/RESULTS-final.md is not what the result files give")
    mode.add_argument("--readme", action="store_true", help="print the test cells of the README table")
    args = parser.parse_args(argv)
    if args.readme:
        for key, text in readme_cells().items():
            print(f"{key}: {text}")
        return 0
    text = render()
    if args.check:
        ok = OUTPUT.is_file() and OUTPUT.read_text(encoding="utf-8") == text
        print("docs/RESULTS-final.md matches the result files" if ok else "docs/RESULTS-final.md differs from what the result files give", file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
