# Final evaluation: split test, 33 items

commit `6d6b08b69f52`; deterministic_sha256 `136c1b9f8843b8d2`.

Two figures per metric: ALL items, and all items except the one whose question hash is pre-registered (matched 1 of 1).

Intervals, as registered: **Wilson 95%** for every proportion (router_accuracy, extractor_exact_match, no_answer_correct, answer_correct, guardrail_fallback_rate); **percentile bootstrap over items** (95%, 10,000 resamples, seed 20261009) for the MRRs, p50 / p95 and the cost.

| metric | population | all items | 95% interval | without the excluded item | 95% interval |
|---|---|---|---|---|---|
| router_accuracy | every item | 1.000 (33/33) | [0.896, 1.000] | 1.000 (32/32) | [0.893, 1.000] |
| extractor_exact_match | product_search items | 0.600 (6/10) | [0.313, 0.832] | 0.600 (6/10) | [0.313, 0.832] |
| product_mrr | product items with SKU gold | 1.000 (n=6) | [1.000, 1.000] | 1.000 (n=6) | [1.000, 1.000] |
| policy_mrr | policy items | 0.852 (n=6) | [0.556, 1.000] | 0.852 (n=6) | [0.556, 1.000] |
| no_answer_correct | product items with no matching product | 0.750 (3/4) | [0.301, 0.954] | 0.750 (3/4) | [0.301, 0.954] |
| guardrail_fallback_rate | items with an LLM-written answer | 0.111 (2/18) | [0.031, 0.328] | 0.111 (2/18) | [0.031, 0.328] |
| latency_p50_ms | every item (one first turn each) | 2639.4 (n=33) | [1246.9, 3051.3] | 2810.0 (n=32) | [1573.7, 3251.3] |
| latency_p95_ms | every item (one first turn each) | 4592.5 (n=33) | [4124.6, 5371.7] | 4592.5 (n=32) | [4124.6, 5371.7] |
| cost_per_1000_usd | every item | 0.9108 (n=33) | [0.7109, 1.1153] | 0.9304 (n=32) | [0.7276, 1.1338] |
| answer_correct | every item | 0.939 (31/33) | [0.804, 0.983] | 0.938 (30/32) | [0.799, 0.983] |
| product_mrr_gold_routing | product items with SKU gold, intent given | 1.000 (n=6) | [1.000, 1.000] | 1.000 (n=6) | [1.000, 1.000] |

## Registered secondary analysis: by author and by intent (descriptive, all items)

Proportions as successes/n = estimate [Wilson 95%]; MRRs as a mean with n. **The samples are small**: a group of fewer than 8 items is marked `(small)` and should not be read as a result; nothing here is a test of a difference between groups, and none is claimed.

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

Live Gemini requests this run: 61; answered from the cache: 45.
No pass/fail threshold exists: these are the numbers, with their intervals.
