# Final evaluation: the single run on the test split (Phase 8)

One run, once, under `docs/PHASE8-PREREG.md`: 33 test items, `gemini-3.5-flash-lite` on the Gemini free tier, commit `6d6b08b69f52`, `deterministic_sha256` `136c1b9f8843b8d2…`. **Every number in this file is generated** from `eval/results/phase8_test/final_evaluation.json` (and the dev column from `eval/results/phase8_rehearsal/final_evaluation.json`) by `scripts/render_results_final.py`; `tests/test_results_final.py` fails if the two differ.

## 1. Results

Two figures per metric, as registered: on all 33 items, and without the one item whose question hash is the one recorded in A0 (32 items; 1 item matched). A proportion is `estimate (successes/n)` with its Wilson 95% interval; an MRR, a latency percentile and the cost are `estimate (n=items)` with a percentile-bootstrap 95% interval (10,000 resamples, seed 20261009). The dev column is optimistic: the prompts and the scoring rules were written with the dev items in view. There is no pass/fail threshold for any metric.

| metric | population | dev (71 items) | test, all items (33) | test, without the excluded item (32) |
|---|---|---|---|---|
| router_accuracy | every item | 1.000 (71/71) [0.949, 1.000] | 1.000 (33/33) [0.896, 1.000] | 1.000 (32/32) [0.893, 1.000] |
| extractor_exact_match | product_search items | 0.958 (23/24) [0.798, 0.993] | 0.600 (6/10) [0.313, 0.832] | 0.600 (6/10) [0.313, 0.832] |
| product_mrr | product items with SKU gold | 1.000 (n=19) [1.000, 1.000] | 1.000 (n=6) [1.000, 1.000] | 1.000 (n=6) [1.000, 1.000] |
| policy_mrr | policy items | 0.839 (n=14) [0.696, 0.964] | 0.852 (n=6) [0.556, 1.000] | 0.852 (n=6) [0.556, 1.000] |
| no_answer_correct | product items with no matching product | 1.000 (5/5) [0.566, 1.000] | 0.750 (3/4) [0.301, 0.954] | 0.750 (3/4) [0.301, 0.954] |
| guardrail_fallback_rate | items with an LLM-written answer (first turn) | 0.026 (1/39) [0.005, 0.132] | 0.111 (2/18) [0.031, 0.328] | 0.111 (2/18) [0.031, 0.328] |
| answer_correct | every item | 0.986 (70/71) [0.924, 0.998] | 0.939 (31/33) [0.804, 0.983] | 0.938 (30/32) [0.799, 0.983] |
| latency_p50_ms | every item (one first turn each) | 2490.4 (n=71) [1648.2, 2859.8] | 2639.4 (n=33) [1246.9, 3051.3] | 2810.0 (n=32) [1573.7, 3251.3] |
| latency_p95_ms | every item (one first turn each) | 4752.2 (n=71) [4135.6, 4861.4] | 4592.5 (n=33) [4124.6, 5371.7] | 4592.5 (n=32) [4124.6, 5371.7] |
| cost_per_1000_usd | every item | 0.9785 (n=71) [0.8272, 1.1307] | 0.9108 (n=33) [0.7109, 1.1153] | 0.9304 (n=32) [0.7276, 1.1338] |
| product_mrr_gold_routing | product items with SKU gold, intent given | 1.000 (n=19) [1.000, 1.000] | 1.000 (n=6) [1.000, 1.000] | 1.000 (n=6) [1.000, 1.000] |

Latency: the test figure is that of one live first turn per item, the dev figure is a replay of recorded latencies, and neither is comparable with the Phase 6 latency, which is over all the turns of the dev run including the multi-turn cases. Cost: the theoretical paid-tier price of the calls (0.3 USD input, 2.5 USD output per million tokens); the free tier cost 0.

## 2. Large dev-test gaps

The rule is fixed in PREREG section 7: a metric has a large gap when its dev point estimate lies outside the registered 95% interval of the test result. It is applied here to both figures.

| metric | dev estimate | test interval, all items | dev outside it | test interval, without the excluded item | dev outside it |
|---|---|---|---|---|---|
| router_accuracy | 1.000 | [0.896, 1.000] | no | [0.893, 1.000] | no |
| extractor_exact_match | 0.958 | [0.313, 0.832] | **yes** | [0.313, 0.832] | **yes** |
| product_mrr | 1.000 | [1.000, 1.000] | no | [1.000, 1.000] | no |
| policy_mrr | 0.839 | [0.556, 1.000] | no | [0.556, 1.000] | no |
| no_answer_correct | 1.000 | [0.301, 0.954] | **yes** | [0.301, 0.954] | **yes** |
| guardrail_fallback_rate | 0.026 | [0.031, 0.328] | **yes** | [0.031, 0.328] | **yes** |
| answer_correct | 0.986 | [0.804, 0.983] | **yes** | [0.799, 0.983] | **yes** |
| latency_p50_ms | 2490.4 | [1246.9, 3051.3] | no | [1573.7, 3251.3] | no |
| latency_p95_ms | 4752.2 | [4124.6, 5371.7] | no | [4124.6, 5371.7] | no |
| cost_per_1000_usd | 0.9785 | [0.7109, 1.1153] | no | [0.7276, 1.1338] | no |
| product_mrr_gold_routing | 1.000 | [1.000, 1.000] | no | [1.000, 1.000] | no |

Metrics with a large dev-test gap, by the rule of PREREG section 7 (the dev point estimate lies outside the registered 95% interval of the test result):

- `extractor_exact_match`: the dev estimate 0.958 lies outside the registered 95% interval of the test result (both figures: [0.313, 0.832]); the dev items were seen while the prompts and the scoring rules were written, so the dev number was optimistic (the test result is the worse one).
- `no_answer_correct`: the dev estimate 1.000 lies outside the registered 95% interval of the test result (both figures: [0.301, 0.954]); the dev items were seen while the prompts and the scoring rules were written, so the dev number was optimistic (the test result is the worse one).
- `guardrail_fallback_rate`: the dev estimate 0.026 lies outside the registered 95% interval of the test result (both figures: [0.031, 0.328]); the dev items were seen while the prompts and the scoring rules were written, so the dev number was optimistic (the test result is the worse one).
- `answer_correct`: the dev estimate 0.986 lies outside the registered 95% interval of the test result (both figures: [0.804, 0.983]); the dev items were seen while the prompts and the scoring rules were written, so the dev number was optimistic (the test result is the worse one).

## 3. Registered secondary analysis: by author and by intent

Descriptive, on all items only (not on the figure without the excluded item), as PREREG section 3 registers. Proportions as successes/n = estimate [Wilson 95%]; the MRRs as a mean with n and no interval. **The samples are small**: a group of fewer than 8 items is marked `(small)` and is not read as a result; no test of a difference between groups is made, and none is claimed.

### By author

| group | items | router_accuracy | answer_correct | extractor_exact_match | no_answer_correct | guardrail_fallback_rate | product_mrr | policy_mrr |
|---|---|---|---|---|---|---|---|---|
| human | 13 | 13/13 = 1.000 [0.772, 1.000] | 12/13 = 0.923 [0.667, 0.986] | 3/4 = 0.750 [0.301, 0.954] | 3/3 = 1.000 [0.439, 1.000] | 1/8 = 0.125 [0.022, 0.471] | 1.000 (n=1) | 0.778 (n=4) |
| agent_draft | 20 | 20/20 = 1.000 [0.839, 1.000] | 19/20 = 0.950 [0.764, 0.991] | 3/6 = 0.500 [0.188, 0.812] | 0/1 = 0.000 [0.000, 0.793] | 1/10 = 0.100 [0.018, 0.404] | 1.000 (n=5) | 1.000 (n=2) |

### By intent

| group | items | router_accuracy | answer_correct | extractor_exact_match | no_answer_correct | guardrail_fallback_rate | product_mrr | policy_mrr |
|---|---|---|---|---|---|---|---|---|
| product_search | 10 | 10/10 = 1.000 [0.722, 1.000] | 9/10 = 0.900 [0.596, 0.982] | 6/10 = 0.600 [0.313, 0.832] | 3/4 = 0.750 [0.301, 0.954] | 1/9 = 0.111 [0.020, 0.435] | 1.000 (n=6) | – |
| policy_question (small) | 6 | 6/6 = 1.000 [0.610, 1.000] | 5/6 = 0.833 [0.436, 0.970] | – | – | 1/6 = 0.167 [0.030, 0.564] | – | 0.852 (n=6) |
| order_status (small) | 6 | 6/6 = 1.000 [0.610, 1.000] | 6/6 = 1.000 [0.610, 1.000] | – | – | 0/3 = 0.000 [0.000, 0.561] | – | – |
| create_ticket (small) | 4 | 4/4 = 1.000 [0.510, 1.000] | 4/4 = 1.000 [0.510, 1.000] | – | – | – | – | – |
| out_of_scope (small) | 3 | 3/3 = 1.000 [0.439, 1.000] | 3/3 = 1.000 [0.439, 1.000] | – | – | – | – | – |
| smalltalk (small) | 4 | 4/4 = 1.000 [0.510, 1.000] | 4/4 = 1.000 [0.510, 1.000] | – | – | – | – | – |

## 4. How the run went

| | |
|---|---|
| result file | `eval/results/phase8_test/final_evaluation.json` (every per-item row, the cross-checks, the attempts), `final_evaluation.md`, `attempts.jsonl` |
| `deterministic_sha256` | `136c1b9f8843b8d201f0fbc6e8fb3f2b2077b7d028032d4b7a9ddc1483408b3e` |
| code measured | commit `6d6b08b69f52446af9bdb64ffbe8f98429b9a0cd`, working tree clean: True |
| freeze | `v1.0-frozen` = commit `d8ec6b3a57ccf64e730fcfa29e34b8d27eb00b48`; the script's own guard (PREREG section 9) accepted HEAD as a descendant of it with no frozen file different |
| dev run checked against | `c9cfdc820e17c59a7df169058f48ac39d6de075c` |
| model | `gemini-3.5-flash-lite`, prompts `answer_v2` `def7398a…`, router `8e08414e…`, extractor `27e2ade2…` |
| test split | 33 items, `items_sha256` `4070dc68164dcf573ee0ed884d685050cd77c87c1b833300920eb9484c8eaf90` (equal to the value registered in PREREG section 1: True) |
| excluded item (A0) | matched 1 item (PREREG section 8 expects exactly one); its id is printed nowhere in the summary and was not looked up |
| attempts | 1 run, 0 refusals; the run ended `complete`, `rerun_allowed` false; no rerun was needed and none was made |
| Gemini requests | 61 live, 45 answered from the cache; PREREG section 5 estimated 60 with a ceiling of 99 |
| wall time | 310.4 s |
| cross-checks | answer score equals the Phase 6 section: True; cost equals the Phase 6 section: True |
| bounds of the agent | every bound held (steps, clarifying questions, searches, failed verifications, confirmation asks, tickets without confirmation): True |
| dev column | `eval/results/phase8_rehearsal/final_evaluation.json`, the rehearsal that PREREG section 2 names as the dev reference (split `dev`, `git_dirty` true: it was run with the tool's own uncommitted files, D8.3); its latencies are replayed, not measured |
| dev latency, typed in PREREG and in that file | PREREG: p50 2495 ms, p95 4785 ms; file: p50 2490.4 ms, p95 4752.2 ms (the replay measures retrieval time again on every run). The large gaps of section 2 are the same with either pair |

Before the run, the first precondition check of this turn stopped because Qdrant was not running (PREREG section 4: the command is not started with a service down). Nothing had been started, no test item was read and no Gemini request was made; after Qdrant was started the checks were repeated from the start and passed, including the two external-state fingerprints (`registered values: equal`). That stop is not an attempt and is not in `attempts.jsonl`. An earlier try of turn 2 stopped at its precondition check because the tag `v1.0-frozen` had been made on the wrong commit; that is D8.6, copied in section 6.

## 5. Discussion

Written after the numbers above and read from the same file. Nothing here is a pass or a fail: PREREG section 2 registers no threshold.

- **Small samples first.** 33 items; the metrics rest on 10 product_search items, 6 policy items, 6 product items with a SKU as gold and 4 `no_answer` items. One item moves a proportion over 4 items by 0.25. Most intervals are wide, and a perfect score does not say that the system is perfect (PREREG section 7).
- **Router.** 33/33 with a Wilson interval [0.896, 1.000]: a result with an interval, not a proof, and the router's dev score was saturated (71/71; PREREG section 10). Misrouted questions: 0.
- **Extractor.** 6/10 exact matches against 23/24 on dev; the dev estimate is outside the test interval (section 2). The output file also holds the registered variant without `in_stock`, the field of the label conflict of `docs/GOLDEN-V2-ISSUES.md` #1, which is not corrected for in the registered figure: 8/10. So 2 of the 4 misses are misses of `in_stock` alone (field table: `in_stock` false negatives 2, false positives 0). The other errors of the field table are `kind` (false positives 1, false negatives 0) and `color` (false positives 1, false negatives 1). This report does not read the questions to say why: the split is not opened again.
- **Retrieval, full pipeline.** `product_mrr` 1.000 (n=6): every product item with a SKU as gold had it at rank 1; `product_mrr_gold_routing` 1.000. `policy_mrr` 0.852 (n=6), the dev estimate inside its interval [0.556, 1.000], which shows how little 6 items say.
- **`no_answer`.** 3/4 against 5/5 on dev. As PREREG section 10 says, this rule counts an empty retrieval after a product route and does not measure how good the offer that follows is.
- **Guardrail and the answer score.** The guardrail replaced the written answer on 2 of 18 answers (0.111; dev 1/39); the rules that fired: `no_sku_cited` 1, `unknown_clause` 1. Whether these were false alarms or caught errors needs the replies, and this report does not read them. `answer_correct` is 31/33; the items scored incorrect are `gs-020` (product_search, agent_draft, check `no_answer`, guardrail fallback true), `gs-115` (policy_question, human, check `gold_clause`, guardrail fallback true). The items scored incorrect are the items on which the guardrail replaced the answer: the same items. This list comes from the `per_item` rows of the result file and is exploratory, not a registered analysis.
- **Latency and cost.** p50 2639.4 ms and p95 4592.5 ms of one live first turn on the free tier (dev: replayed latencies; the dev p50 is inside its test interval and the dev p95 is inside its test interval); 0.911 USD per 1,000 conversations at the theoretical paid-tier price, the free tier costing 0 (the dev estimate is inside its interval).
- **By author and by intent.** The tables of section 3 say where the totals come from. They are descriptions of these items; no difference between rows is a finding about authors or intents (PREREG section 3).
- **What is not claimed.** That the system is as good as the dev numbers (the dev items were seen), that the test split is representative of real customers (everything is synthetic, PREREG section 10), or that a metric is "good" or "bad": there is no threshold. The test split is not opened again until a golden v2 exists.

## 6. Incidents and known limits

PREREG section 10 and decision D8.6, **copied word for word** (`tests/test_results_final.py` compares them with the two files). Section 10 was written before any test number existed; it is not edited in the light of the results.

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

### D8.6 The tag `v1.0-frozen` was first made by mistake, before the freeze commit; it is deleted and made again after CI is green

- **What happened.** On 2026-10-12 at 13:04:54 UTC (20:04 +07) an annotated tag `v1.0-frozen` (tag object `d11c58a32387a33fea22d9817a154c4e0a785fce`, message "Phase 8 freeze: code, prompts, index, external state and pre-registration fixed before the single test-split run") was created on `c9cfdc820e17c59a7df169058f48ac39d6de075c`
  and pushed to `origin`. That commit is the **last commit before Phase 8**: it is not a freeze commit. At the tag, `eval/final_evaluation.py`, `eval/external_state.py` and `docs/DECISIONS-phase8.md` do not exist and `docs/PHASE8-PREREG.md` is the earlier draft (it does not mention `v1.0-frozen`). The freeze commit had not been made, pushed or checked by CI (`origin/main` was at `c9cfdc8`
  and the working tree had 11 uncommitted paths). It was found by the precondition check of turn 2, which stopped there: **the test split was not run**, nothing was read from or written to `eval/results/phase8_test/` (it does not exist), no Gemini request was made that day (the usage file of 2026-10-12 does not exist), and no result of the test split exists anywhere.
- **Decision.** The tag is **deleted (locally and on `origin`) and made again on the freeze commit, after that commit has been pushed and its CI is green**, as `docs/PHASE8-COMMITS.md` describes. The name stays exactly `v1.0-frozen`: `eval/final_evaluation.py` accepts no other name, and `docs/PHASE8-PREREG.md` registers that name.
- **Why this is allowed, and what it is not.** D8.4 says a tag that is deleted and made again is a deviation to report; this is the report. It is a deviation of the **procedure** only: the tag was moved before the test split was opened and before any test number existed, so no choice could depend on a result; the first tag was never used by any run (the script cannot succeed
  on it: the working tree was dirty, and `eval/final_evaluation.py` did not exist at it). After the tag is made again the rule of D8.4 stands: it is not moved a second time, and a second move would be a deviation of another kind, after the commit that the test result depends on is already fixed.
- **Not edited:** `docs/PHASE8-PREREG.md` (the registration is unchanged by this decision; it says that the tag is made on the freeze commit after CI is green, which is what is now done).
- **How it can be checked afterwards.** The old tag object id above is recorded here, so that it is known which tag was replaced. The new one is checked by `git rev-parse v1.0-frozen^{commit}` (the freeze commit, which must be the one whose CI was green) and by the script itself, which resolves the tag and refuses unless HEAD is that commit or differs from it only in files that cannot change a result.
  A run's `final_evaluation.json` records the tag and the commit it resolved to (`provenance.freeze`).
- **The day of the run is the registered one.** The test run is started on the Pacific day **2026-10-13** only: from 14:00 on 13 October to 14:00 on 14 October, Vietnam time, not in the last hour of that day (`docs/PHASE8-PREREG.md` section 5). Not on 2026-10-12, although the counter file of that day is empty: the day was registered in advance and the script does not check it, so this is a procedure that the person who runs it keeps.
- **Undo.** There is nothing to undo in the repository: the entry only records the replacement of the tag. If the old tag were kept instead, the script would refuse every run (see above).
