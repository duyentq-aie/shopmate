# Phase 8 pre-registration

**Written before any test-split number exists.** The test split has not been run, and nothing in this file or in the turns that wrote it opened it (`eval/final_evaluation.py` can open it, once, under the guards of section 9; it was run
on **dev** only). Everything below is registered *before* the run so that no choice can depend on what the run shows. This file is frozen with the tag: the script refuses to run if it differs between the tag and HEAD, so a deviation is never announced by editing it; it is written in
`docs/DECISIONS-phase8.md` (a new decision) and in the write-up of the results, which says which numbers it touches.

**Freeze = the tag `v1.0-frozen`.** It is made on the freeze commit after that commit has been pushed and its CI is green (`docs/PHASE8-COMMITS.md`), and it is the only name the script accepts (`--freeze-ref v1.0-frozen`). This file contains no SHA: the tag
*is* the freeze, and the script resolves it to a commit.

## 1. What is frozen, and against what

| | |
|---|---|
| the freeze | the tag `v1.0-frozen`. `eval/final_evaluation.py` refuses to open the test split unless the tag exists and HEAD is its commit, or descends from it with **nothing different** in `app/`, `prompts/`, `data/index`, `data/seed`, `data/policies`, `eval/` (except `eval/results/` and `eval/llm_cache/`, which this very run writes), `pyproject.toml`, `uv.lock` or this file. Documents other than this one, tests and CI may differ |
| the code that is measured | `c9cfdc820e17c59a7df169058f48ac39d6de075c` is the commit of the official dev run (`docs/DECISIONS-phase8.md` D8.2); `--dev-run-sha` makes the script check that nothing that can change a result differs between it and HEAD (the script `eval/final_evaluation.py` is newer than that run and is covered by the check above) |
| the model | `gemini-3.5-flash-lite` (Gemini free tier), generation settings and prompts as in the dev run: router `router_v1` `8e08414e…`, extractor `filters_v1` `27e2ade2…`, answers `answer_v2` `def7398a…` (full hashes in `eval/results/phase8_dev/answer_v2.json`) |
| the index | collection `shopmate_vn_v2`, mode `dense_folded` (`k` 5 products, 3 policy clauses in the agent; 10 in the retrieval metrics), `data/index/manifest.json` sha256 `8bee3a76b734c9d245e81f8a0727d7975e675e02b1b7ba8e0e32956712a7fb38`; the catalogue is **not** corrected before the run (section 10). **Fingerprint of the live collection** (`eval/external_state.py`, read only, taken 2026-10-12): **269 points** (250 product + 19 policy: equal to the manifest), vectors `dense` 1024 Cosine, `sparse` without modifier, `folded` with the IDF modifier (equal to the manifest), and `payloads_sha256` = `f2a7b8c2b7bd9021a526e2900f176cc6c4a7ec7472c10bf83334d55706cd292b`, the sha256 of the lines `<point id> <sha256 of the canonical JSON of its payload>`, one per point, sorted by id (the vectors are not read: the manifest holds the digest of the vectors of the build, and the Phase 6 runner checks the live index against it) |
| the order data | the 30 seed orders **#84 to #113** in Saleor (`docs/SETUP.md`), read one by one through the read-only client (allowlisted `OrderByNumber`, runtime token), taken 2026-10-12: **30 orders found**, `rows_sha256` = `33df74d4c896aa0c999e36a8b6f95ddcf74b429ce0f49363513cb5a7a25f4d18`, the sha256 of the lines `<number>\|<status>\|<total>\|<number of lines>\|<sha256 of the lower-cased customer email>`, one per order, sorted by number. Field by field they equal what `data/seed/orders.vi.json` implies. The raw email is written nowhere |
| the golden set | the test split is the one recorded in `eval/golden/test_split.sha256` (`4070dc68164dcf573ee0ed884d685050cd77c87c1b833300920eb9484c8eaf90`), 33 items: 10 product_search, 6 policy_question, 6 order_status, 4 create_ticket, 3 out_of_scope, 4 smalltalk (`docs/SETUP.md`); `tests/test_frozen_test_split.py` fails if it changed |
| the dev reference | `eval/results/phase8_dev/answer_v2.json`, `deterministic_sha256` `85e54e592895df2914ce52c790c006e9e273a056cad2c51dea6bb15a14310a73`: dev 70/71, multi-turn 13/13, product `no_answer` 5/5, identical to the numbers reported so far except three replies that are now offers (D8.2) |
| the tool | `eval/final_evaluation.py`, rehearsed on dev: `eval/results/phase8_rehearsal/` (D8.3, D8.4) |
| the CI | the three actions pinned by commit SHA (D8.1) |

The dev run is at `c9cfdc8`, not at the freeze commit, because a result cannot be committed in the commit it was obtained at; the pinned thing is the code, and the script checks that it is unchanged.

## 2. What is reported, and with which interval

One run measures everything on the 33 test items, once. Per item, then a proportion, a mean or a percentile over items. **The interval depends on the kind of metric, and is registered here:**

- **A proportion** (`router_accuracy`, `extractor_exact_match`, `no_answer_correct`, `answer_correct`, `guardrail_fallback_rate`): the **Wilson score interval, 95%** (z = 1.959964). It is the registered interval; no bootstrap number is printed for a proportion.
  A percentile bootstrap collapses to a point when every item succeeded (71 of 71 gives [1.000, 1.000]) and says nothing about how little 71 items can show; the Wilson interval for 33 of 33 is [0.896, 1.000].
- **The MRRs, the latency percentiles and the cost:** a **percentile bootstrap over items, 95%, 10,000 resamples, seed 20261009** (`eval/retrieval_metrics.py`, the settings of Phases 3B to 6; one seed for every metric and both figures).
  The interval of an MRR collapses to a point as well when every item has the gold at rank 1; the number of items is printed beside it.

| metric | per item | items | interval | what a miss is |
|---|---|---|---|---|
| `router_accuracy` | 1 if the router (`router_v1`) names the gold intent | all 33 | Wilson | a wrong intent, or an unusable answer |
| `extractor_exact_match` | 1 if the extracted filters equal the expected filters without `keyword` (`eval/llm_metrics.py`) | the product_search items | Wilson | any difference in any field. The label conflict of gs-019-type items (`in_stock`, `docs/GOLDEN-V2-ISSUES.md` #1) is **not** corrected for; the result without `in_stock` is in the output file as well (`phase5.extraction`) |
| `product_mrr` | reciprocal rank (top 10) of the first gold SKU in the **full pipeline**: LLM route -> doc_type -> LLM-extracted filters -> `dense_folded` retrieval | product items with SKU gold | bootstrap | a misrouted question has rank 0 |
| `policy_mrr` | reciprocal rank of the gold clause in the full pipeline | the policy items | bootstrap | a misrouted question has rank 0 |
| `no_answer_correct` | 1 if the question is routed to product_search **and** the result is empty | product items whose gold is "no matching product" | Wilson | routed elsewhere, or a result that is not empty |
| `guardrail_fallback_rate` | 1 if the guardrail replaced the written answer with its fixed text | items for which the LLM wrote an answer in the first turn | Wilson | a replaced answer (a decline is not a fallback) |
| `answer_correct` | the Phase 6 score of the item (`eval/phase6_scoring.py`, written before any run) | all 33 | Wilson | a reply that the scoring rules reject. **A primary metric**, like the others: every earlier report has it (70/71 on dev) |
| `latency_p50_ms`, `latency_p95_ms` | the first turn of the item: recorded LLM latencies plus retrieval, lookup and reply time | all 33 | bootstrap | not a hit-or-miss metric. The value at index round(q x (n-1)) of the sorted latencies (`eval/retrieval_metrics.py`, the rule of every earlier latency figure). **Not comparable with the earlier 1.5 s / 4.9 s**, which are over 102 turns of the dev run including the multi-turn cases |
| `cost_per_1000_usd` | USD of one item's calls at 0.30 (input) and 2.50 (output, thought tokens included) per million tokens, x 1000 | all 33 | bootstrap | the theoretical paid-tier cost (prices of `docs/RESULTS-phase6.md`); the free tier costs 0 |
| `product_mrr_gold_routing` | as `product_mrr` with the intent given (secondary) | product items with SKU gold | bootstrap | for comparison with the Phase 5 number |

**Two figures per metric:** (a) all 33 items; (b) all items except the one whose question hash is the one recorded in A0 (`3b9975a677521f1dda7bac87ffa7098bc3d748f5724545ee185947a1e86d8692`; 32 items if it matches, section 8). The script matches
by hashing every test `question` exactly as A0 did (UTF-8 of the text as stored, the `{order_number}` placeholder literal, no normalisation). Nobody looks up the id of that item before the report; the script prints it nowhere and stores it in no summary.
If no item matches, the report says so and the second figure is the first.

**There is no pass/fail threshold, for any metric.** The numbers are reported with their intervals and nothing else is concluded from them in advance. The results of the dev run for reference (the same code path, `eval/results/phase8_rehearsal/`;
dev numbers are **optimistic**: the prompts and the scoring rules were written with the dev items in view):

| metric | dev (71 items) | estimate | 95% interval |
|---|---|---|---|
| router_accuracy | 71/71 | 1.000 | Wilson [0.949, 1.000] |
| extractor_exact_match | 23/24 | 0.958 | Wilson [0.798, 0.993] |
| product_mrr | n=19 | 1.000 | bootstrap [1.000, 1.000] |
| policy_mrr | n=14 | 0.839 | bootstrap [0.696, 0.964] |
| no_answer_correct | 5/5 | 1.000 | Wilson [0.566, 1.000] |
| guardrail_fallback_rate | 1/39 | 0.026 | Wilson [0.005, 0.132] |
| answer_correct | 70/71 | 0.986 | Wilson [0.924, 0.998] |
| product_mrr_gold_routing | n=19 | 1.000 | bootstrap [1.000, 1.000] |
| latency_p50_ms (replayed latencies) | n=71 | 2495 | bootstrap [1648, 2895] |
| latency_p95_ms (replayed latencies) | n=71 | 4785 | bootstrap [4230, 4863] |
| cost_per_1000_usd | n=71 | 0.9785 | bootstrap [0.8272, 1.1307] |

## 3. Registered secondary analysis: by author and by intent

The same run also reports, **on the test split**, the proportions by **author** (`human` / `agent_draft`) and by **intent** (product_search, policy_question, order_status, create_ticket, out_of_scope, smalltalk), as two tables (`secondary` in `final_evaluation.json`, and the end of `final_evaluation.md`).
It is **descriptive**, registered here so that it is not a search after the fact:

- per group: the number of items; `router_accuracy`, `answer_correct`, `extractor_exact_match`, `no_answer_correct` and `guardrail_fallback_rate` as successes/n with the **Wilson 95% interval**; `product_mrr` and `policy_mrr` as a mean with n, **without an interval**
  (a bootstrap over a handful of items would be a number without meaning);
- **all items only**: not for the second figure of section 2. The difference between two such tables would show which author and which intent the excluded item has, and its identity is not looked up;
- **the samples are small, and the report says so in the table:** by intent the test split has 10 / 6 / 6 / 4 / 3 / 4 items (`docs/SETUP.md`), by author 20 `agent_draft` and 13 `human` (64 minus the 44 of dev; the split was not read to count them). A group of fewer than 8 items (`MIN_INTERPRETABLE_N`, the threshold of Phase 3B)
  is marked `(small)`: policy_question, order_status, create_ticket, out_of_scope and smalltalk will be. A metric that applies to a few items only (for example `no_answer_correct`, `extractor_exact_match` outside product_search) has an interval that is almost the whole of [0, 1];
- **no test of a difference between groups is made, and none is claimed.** A difference between author or intent rows is not a finding about authors or intents; it is a description of 33 items. The table is there to show where an overall number comes from, and what a reader should
  not conclude from it.

The same two tables are printed for dev by the rehearsal (`eval/results/phase8_rehearsal/final_evaluation.md`), where the groups are larger (27 `human`, 44 `agent_draft`; 24 / 14 / 12 / 7 / 7 / 7 by intent).

## 4. The command, exactly

Preconditions, checked by hand and then by the script: `git status --porcelain` empty; the tag `v1.0-frozen` exists and `git rev-parse HEAD` is its commit (or a later one that changed none of the frozen files); Qdrant (`http://127.0.0.1:6333/healthz`) and Saleor
(`uv run python -m scripts.check_saleor --wait 60`) answer and `uv run python -m eval.external_state` ends with `registered values: equal` (it writes nothing); the file of the Pacific day in `data/llm_usage/` shows at most 350 of 450 requests used; `uv run pytest -q` is green (it includes the frozen test-split hash).

```bash
uv run python -m eval.final_evaluation --split test --live --max-live-calls 100 \
    --freeze-ref v1.0-frozen --dev-run-sha c9cfdc820e17c59a7df169058f48ac39d6de075c
```

It writes `eval/results/phase8_test/final_evaluation.json` (with `deterministic_sha256`, the provenance, the tag and the commit it resolved to, and every per-item row), `final_evaluation.md` (both figures and the secondary tables) and `attempts.jsonl`. It prints
`OPENING THE TEST SPLIT` first. The result file is committed as it is, and every number in the report is read from it.

## 5. Quota and day

- **Estimate: 60 Gemini requests** (59.8 = 10 x 2.88 product_search + 6 x 2.00 policy + 6 x 1.33 order_status + 4 x 1.00 ticket + 3 x 1.00 out_of_scope + 4 x 1.00 smalltalk, the mean calls per first turn of each intent on dev, D8.3); 65 at the largest
  count seen on dev for each intent; the ceiling of the design is 99 (3 per item). The router and extractor requests of the two halves of the script are the same requests, so the cache makes them one. At 12 requests a minute (the client limit) a run takes about
  5 to 8 minutes; the first run is the only one that spends quota, a rerun replays what it already has.
- **Day:** a Pacific day (the counter resets at 00:00 Pacific, 14:00 Vietnam time until 1 November). Planned: **2026-10-13** (14:00 on 13 October to 14:00 on 14 October, Vietnam time), or any later day whose counter file has room for 100 more requests (450 is our ceiling of the 500 the free tier allows).
  The run is not started in the last hour of a Pacific day, so that a quota reset cannot split it.

## 6. Rerun policy

- **A run is repeated only if it failed for infrastructure**, i.e. it produced no result because a service or the quota was not there. The error types (`app/tracing.py`) that count: `llm_429_daily`, `llm_rate_limited`, `llm_unavailable`, `budget_cap_reached`,
  `qdrant_unreachable`, `saleor_timeout`, `saleor_error`, `request_interrupted`. The script records how every attempt ended in `eval/results/phase8_test/attempts.jsonl` and sets `rerun_allowed` from this list (`RERUN_ELIGIBLE` in `eval/final_evaluation.py`).
- **Only the items that failed are run again, by construction:** every answered LLM request is in `eval/llm_cache/` and a rerun replays it (answers, latency, tokens: the same), so only what was never answered is asked again. A response that came back but was unusable
  (`llm_invalid_output`) is cached like any other and is a **result**, not a failure.
- **At most two reruns**: three attempts in all; the script refuses a fourth.
- **Never because of the result.** Not after a number is low, not after a wrong answer, not to "see if it is stable". A failure that is not on the list (`llm_request_error`, `llm_cache_miss_offline`, `internal_error`, an exception of our own code, any wrong reply) is a finding:
  it is reported, and the script refuses a rerun after it. There is no way to overwrite a finished result file.
- A change of code, prompt, data or configuration between attempts is refused by the script (section 1); a change that it cannot see is a deviation, recorded as said at the top of this file (the decision record and the write-up of the results), and the write-up says which numbers it touches.

## 7. Commitments

- **Every result is published**, whatever it is: the whole `final_evaluation.json` (every per-item row, the cross-checks, the attempts), both figures, all intervals, the secondary tables, and a report that gives them before any discussion. No metric of section 2 is dropped, added or redefined
  after the run; an analysis that was not registered is labelled as exploratory.
- **A large dev-test gap is reported as overfitting to dev.** "Large" is fixed now, descriptively and not as a threshold of success: a metric has a large gap when its **dev point estimate lies outside the registered 95% interval of the test result** (Wilson for a proportion, bootstrap for an MRR, a latency
  percentile or the cost). The report lists those metrics and says in one sentence for each: the dev items were seen while the prompts and the scoring rules were written, so the dev number was optimistic. A test result better than dev is reported as it is.
- **Small samples are said to be small.** 33 items, of which at most 10 product_search items, 6 policy items and a handful of `no_answer` items: most intervals are wide, a perfect score does not say "the system is perfect", and the breakdowns of section 3 are not read as results.
- **The test split is not opened again** after this run, for any purpose, until a golden v2 exists.

## 8. Matching the excluded item

The script hashes every `question` of the test split (section 2) and reports how many matched (expected: exactly 1 of 33). If 1 matched, the second figure is on 32 items; if 0, the report says "no item matched" and the second figure equals the first; more than 1 would be reported as it is.
A mismatch would mean the stored text is not the text that was hashed in the Phase 6 survey: that is a fact to report, not something to look into by reading the test split.

## 9. The guards of `eval/final_evaluation.py`

The test split is opened only by `--split test`, only with `final_evaluation=True`, and only if: the working tree is clean (new files under `eval/results/` and `eval/llm_cache/` allowed); `eval/results/phase8_test/final_evaluation.json` does not exist; the attempts file permits another attempt (section 6);
`--freeze-ref` is exactly `v1.0-frozen`, that tag exists, and HEAD is its commit or descends from it with no frozen file different (section 1); no result-relevant file differs between `--dev-run-sha` and HEAD; **the state outside the repository is the registered one:** `eval/external_state.py` recomputes, read only, the fingerprint of the Qdrant collection and the one of the 30 seed orders (section 1) and the script refuses if
either differs from the registered value, or if a service cannot be read. It prints the registered and the current value of each part that differs (and, for the orders, which order differs from the seed file in which field, never an email) and writes a `refusal` entry to `attempts.jsonl`; a refusal
opened nothing, so it is not one of the three attempts. There is no rehearsal and no overwrite for the test split.
`tests/test_final_evaluation.py` checks each of these without opening the split, and that no other module passes `final_evaluation=` to the loader.

## 10. Disclosed incidents and known limits

### A0: one test question text was seen in Phase 6

During the Phase 6 survey a `grep -n` over the raw golden file printed the text of one test-split question (its text only; no id, no label). It was not used in any prompt, test, example or document, and the golden file was not reopened to find the item.
Its SHA-256 (section 2) was recorded from the author's context. Pre-registered then, and kept here: the test score is reported on all items **and** on all items except the one that matches (`docs/DECISIONS-phase6.md` A0, `docs/RESULTS-phase6.md` section 11).
The record of that day says it was not used in any prompt, test, example or document. Whether a choice made afterwards was influenced by having seen it can be neither excluded nor checked from the repository; that is why the second figure exists.

### D7.24: the golden file was read directly twice in Phase 7

While wording a limitation in the README, two commands read `eval/golden/golden.vi.json` directly (one `grep -o` counting `agent_draft`, one `json.load` that printed the key names of the first item and the count of items per `(author, status, split)`), instead of through `load_split("dev")`.
What was printed: counts and key names only; no test question, label or id was printed, read into a file or used (`docs/DECISIONS-phase7.md` D7.24). The frozen hash and `tests/test_golden_access.py` were untouched and pass.

### The golden set has items written by an agent

104 items: 64 `agent_draft` (all human-reviewed) and 40 written by the owner (`docs/SETUP.md`); 44 of the 71 dev items are `agent_draft` (counted through `load_split("dev")`), so by subtraction **20 of the 33 test items** are (the test split was not read for this number: it is 64 minus 44).
Items drafted by an agent can differ from what real customers type (phrasing, difficulty, how often a question is ambiguous) in ways nobody measured; the registered secondary analysis (section 3) reports the results by author for that reason, descriptively. Label conflicts known from dev (`docs/GOLDEN-V2-ISSUES.md`): `in_stock` labelled for a question that only asks about availability (#1); `query_style` teencode vs khong_dau (#2);
the intent of a request about someone else's order (#3). The test split may have the same conflicts; it cannot be checked, and the golden set is **not** edited.

### The router's dev score is saturated

71 of 71 on dev says that the prompt is good enough for those questions, not how it generalises: the prompt was written after reading the dev items. A router that is wrong on a test question is the result; a router that is right on all 33 is a result with a Wilson interval of [0.896, 1.000], not a proof.

### The `no_answer` rule does not measure how good an offer is

Since Phase 7.5 an empty product search ends with an offer ("nothing matches; this constraint blocks; N products without it; do you want to see them?"). The `no_answer_correct` metric counts a question as right if it is routed to product_search and the **retrieval** returned nothing, and the Phase 6 score of such an item
needs the fixed "no match" sentence and no SKU (`eval/phase6_scoring.py`, written before the offer existed, `docs/DECISIONS-phase7.md` D7.28). So a poor, wrong or useless offer scores as well as a good one; whether the offer is the *right* one (which constraint, how many products) is not measured by any number in this report.

### Other limits that bound what the numbers say

- Everything is **synthetic**: a fictional shop, a 250-product catalogue, 30 seeded orders, questions typed by the author and an agent. Nothing says how it behaves on a real catalogue or real customers.
- **Guardrail recall is unmeasured**: the model never lied on dev, so the fallback rate counts false alarms and corrections of a weaker kind, not caught lies; sizes, colours, materials and numbers of days in an answer are not checked at all (`docs/RESULTS-phase6.md`).
- **Latency and the free tier:** the test run is the only live run of the 33 items, on the free tier at the moment it happens; the dev latency in this file is a replay of recorded latencies. Theoretical cost is a formula, not an invoice.
- **One run, one model version:** `gemini-3.5-flash-lite` can change under the same name; the model versions the API reports are recorded in the file.
- **State that is not fingerprinted:** the Qdrant vectors (covered by the manifest's digest and the Phase 6 runner's check, not by the payload list), and any Saleor order outside #84 to #113: the agent looks up whatever order number a question names (`OrderByNumber`, its only Saleor call at run time; products and stock come from the Qdrant payloads), so an order
  created or removed outside that range would not be seen. The Saleor catalogue is not read at run time and is not fingerprinted. The two fingerprints were taken after the official dev run of the same day, so they say what the state is now, not that it was the same at the dev run: the dev run itself checked the index against the manifest (counts, embedding fingerprints, vector digest) and found no difference.
- **The index** was built once from the catalogue and the policies; retrieval is not randomised, but the LLM calls are cached after the first answer, so the numbers of a rerun are those of the first run.

### The synthetic catalogue has products whose name contradicts their material

**What it is.** `data/seed/catalog.vi.json` is generated from templates (`scripts/generate_catalog.py`): a product name is `kind + style + colour`, and the material is drawn
separately, from the materials of the kind. A style word can therefore name a material the product does not have: "Áo khoác nam **jean** xanh rêu" is made of Polyester. The description
is built from the name and the material ("… làm từ chất liệu polyester"), so the contradiction is also inside the text that is indexed. The data is synthetic; this is a limit of how it
was made, not a measurement.

**How it was checked.** `uv run python -m scripts.audit_catalog_consistency` (tests: `tests/test_audit_catalog.py`). It only reads the catalogue file: it writes nothing and opens no index
or service. A whole word of the name that names a material is compared with the `material` attribute (jean and denim: Denim; da: Da bò, Da tổng hợp or Da lộn; linen; nỉ; dù; kaki, also
Kaki pha spandex; len, also Len pha cotton; and the other material words, none of which occurs in a name). Words are matched exactly, accents included (da is leather, dạ and đa are other words;
dù is the fabric, "du lịch" is travel; tím is purple, "cổ tim" is a neckline). The name is also checked against the `color` attribute, longest colour first ("xanh navy" before "xanh").
"thun" is not checked (it names a garment as well as a fabric), and neither are the descriptions.

**Result: 250 products read.**

| rule | names with the word | contradictions |
|---|---|---|
| jean / denim | 18 | 4 |
| nỉ (fleece) | 2 | 2 |
| kaki | 10 | 2 |

Products with at least one contradiction: 8 of 250
SKUs: SM-NAM-0014, SM-NAM-0021, SM-NAM-0028, SM-NAM-0041, SM-NAM-0042, SM-NAM-0048, SM-NU-0027, SM-NU-0046

| SKU | name | material |
|---|---|---|
| SM-NAM-0014 | Áo khoác nam jean xanh rêu | Polyester |
| SM-NAM-0021 | Áo khoác nam nỉ có mũ xám | Denim |
| SM-NAM-0028 | Áo khoác nam jean xanh navy | Dù |
| SM-NAM-0041 | Quần short nam kaki xanh rêu | Cotton |
| SM-NAM-0042 | Áo khoác nam nỉ có mũ xanh navy | Polyester |
| SM-NAM-0048 | Quần short nam kaki đen | Polyester |
| SM-NU-0027 | Áo khoác nữ jean be | Dù |
| SM-NU-0046 | Chân váy nữ jean trắng | Tweed |

- All eight are a **style** word of the name, none is in the kind of the garment: every "Quần jean nam / nữ" is Denim, every "Ví da" is leather, every "Áo len nữ" is wool or a wool blend.
- **Looked at and found consistent (0 contradictions):** "da" (7 names), "len" (7 names). **Not checkable, the word is in no name:** denim, linen, dù, lụa, voan, tweed, kate, canvas, lưới, thun lạnh,
  cotton, polyester, bamboo, acetate, kim loại, cao su. A zero in the second group means the rule has nothing to read, not that it was verified.
- **Colour:** 0 products whose name has another colour, 0 whose name lacks its own colour. This is by construction (the generator writes the colour attribute into the name), so it is not independent evidence.
- 8 of 250 is 3.2% of the catalogue. Two of the eight, SM-NAM-0014 and SM-NAM-0042, are the two jackets of the offer shown by the widget demo (D7.30, D7.36); the demo draws them with a plain weave
  because it follows the `material` attribute.

**What it means for the results.** A question that filters on material, or an answer that quotes it, can touch one of these eight products and then rests on a record that disagrees with itself.
Checked on **dev** only, through `load_split("dev")`: none of the eight is among the products selected by the expected filters of the 24 dev product items. The **test split was not consulted**
(it is locked), so whether its items are exposed is not known and cannot be known without opening it; at most 8 of the 250 products are involved. Both figures of Phase 8 are reported on the catalogue
as it is.

**Decision: the catalogue is not corrected before Phase 8.** Correcting a name or a material changes the indexed text, therefore the embeddings, the lexical fingerprint and
`data/index/manifest.json`, and with them the index that the dev results, the frozen commit and the hashes of the committed results are tied to. It is left to after Phase 8, as one change with a re-index
and a re-run on dev, and the audit script is the check that it worked (it must then print 0 of 250).
