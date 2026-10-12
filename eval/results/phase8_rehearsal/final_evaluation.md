# Final evaluation: split dev, 71 items (REHEARSAL: not a result)

commit `c9cfdc820e17` (working tree NOT clean); deterministic_sha256 `daefb4802eadb728`.

Two figures per metric: ALL items, and all items except a stand-in item (a rehearsal of the second figure).

Intervals, as registered: **Wilson 95%** for every proportion (router_accuracy, extractor_exact_match, no_answer_correct, answer_correct, guardrail_fallback_rate); **percentile bootstrap over items** (95%, 10,000 resamples, seed 20261009) for the MRRs, p50 / p95 and the cost.

| metric | population | all items | 95% interval | without the excluded item | 95% interval |
|---|---|---|---|---|---|
| router_accuracy | every item | 1.000 (71/71) | [0.949, 1.000] | 1.000 (70/70) | [0.948, 1.000] |
| extractor_exact_match | product_search items | 0.958 (23/24) | [0.798, 0.993] | 0.957 (22/23) | [0.790, 0.992] |
| product_mrr | product items with SKU gold | 1.000 (n=19) | [1.000, 1.000] | 1.000 (n=18) | [1.000, 1.000] |
| policy_mrr | policy items | 0.839 (n=14) | [0.696, 0.964] | 0.839 (n=14) | [0.696, 0.964] |
| no_answer_correct | product items with no matching product | 1.000 (5/5) | [0.566, 1.000] | 1.000 (5/5) | [0.566, 1.000] |
| guardrail_fallback_rate | items with an LLM-written answer | 0.026 (1/39) | [0.005, 0.132] | 0.026 (1/38) | [0.005, 0.135] |
| latency_p50_ms | every item (one first turn each) | 2490.4 (n=71) | [1648.2, 2859.8] | 2478.2 (n=70) | [1530.5, 2718.4] |
| latency_p95_ms | every item (one first turn each) | 4752.2 (n=71) | [4135.6, 4861.4] | 4752.2 (n=70) | [4135.6, 4808.6] |
| cost_per_1000_usd | every item | 0.9785 (n=71) | [0.8272, 1.1307] | 0.9605 (n=70) | [0.8125, 1.1100] |
| answer_correct | every item | 0.986 (70/71) | [0.924, 0.998] | 0.986 (69/70) | [0.923, 0.997] |
| product_mrr_gold_routing | product items with SKU gold, intent given | 1.000 (n=19) | [1.000, 1.000] | 1.000 (n=18) | [1.000, 1.000] |

## Registered secondary analysis: by author and by intent (descriptive, all items)

Proportions as successes/n = estimate [Wilson 95%]; MRRs as a mean with n. **The samples are small**: a group of fewer than 8 items is marked `(small)` and should not be read as a result; nothing here is a test of a difference between groups, and none is claimed.

### By author

| group | items | router_accuracy | answer_correct | extractor_exact_match | no_answer_correct | guardrail_fallback_rate | product_mrr | policy_mrr |
|---|---|---|---|---|---|---|---|---|
| human | 27 | 27/27 = 1.000 [0.875, 1.000] | 27/27 = 1.000 [0.875, 1.000] | 8/8 = 1.000 [0.676, 1.000] | 4/4 = 1.000 [0.510, 1.000] | 0/12 = 0.000 [0.000, 0.242] | 1.000 (n=4) | 0.750 (n=4) |
| agent_draft | 44 | 44/44 = 1.000 [0.920, 1.000] | 43/44 = 0.977 [0.882, 0.996] | 15/16 = 0.938 [0.717, 0.989] | 1/1 = 1.000 [0.207, 1.000] | 1/27 = 0.037 [0.007, 0.183] | 1.000 (n=15) | 0.875 (n=10) |

### By intent

| group | items | router_accuracy | answer_correct | extractor_exact_match | no_answer_correct | guardrail_fallback_rate | product_mrr | policy_mrr |
|---|---|---|---|---|---|---|---|---|
| product_search | 24 | 24/24 = 1.000 [0.862, 1.000] | 24/24 = 1.000 [0.862, 1.000] | 23/24 = 0.958 [0.798, 0.993] | 5/5 = 1.000 [0.566, 1.000] | 0/21 = 0.000 [0.000, 0.155] | 1.000 (n=19) | – |
| policy_question | 14 | 14/14 = 1.000 [0.785, 1.000] | 13/14 = 0.929 [0.685, 0.987] | – | – | 1/14 = 0.071 [0.013, 0.315] | – | 0.839 (n=14) |
| order_status | 12 | 12/12 = 1.000 [0.758, 1.000] | 12/12 = 1.000 [0.758, 1.000] | – | – | 0/4 = 0.000 [0.000, 0.490] | – | – |
| create_ticket (small) | 7 | 7/7 = 1.000 [0.646, 1.000] | 7/7 = 1.000 [0.646, 1.000] | – | – | – | – | – |
| out_of_scope (small) | 7 | 7/7 = 1.000 [0.646, 1.000] | 7/7 = 1.000 [0.646, 1.000] | – | – | – | – | – |
| smalltalk (small) | 7 | 7/7 = 1.000 [0.646, 1.000] | 7/7 = 1.000 [0.646, 1.000] | – | – | – | – | – |

Live Gemini requests this run: 0; answered from the cache: 231.
No pass/fail threshold exists: these are the numbers, with their intervals.
