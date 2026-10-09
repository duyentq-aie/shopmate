# Phase 3B results: baseline retrieval, dev split only

This is the "before" number for every later retrieval change. It measures the Phase 3A retrieval exactly as built, plus one bug fix (deterministic tie-breaking, DECISIONS R16), with no tuning. **Dev split only. The test split was not read** (`load_split` locks it, and no eval script sets `final_evaluation`).

## Provenance

| Item | Value |
|---|---|
| Git commit (all 6 runs, `git_dirty: false`) | `d8b4a6171d6e0c09b09735508ef3f5d4da892e4d` |
| Index manifest sha256 | `004844ff62c6b2cbb14fd5a96da3c49187d723c21ed675d1bcfe2feb6ec0397c` (269 points, all with the manifest's fingerprint) |
| Embedder fingerprint | `53b494c7da7e1203` (bge-m3 @ `5617a9f…`) |
| Golden file sha256 | `89f0dd2deff438b09e7849c0f6c2bed022a347ce12e3ec97b93514bf9631bd81` |
| Dev split sha256 | `62fe8897140b08601306f468b40a71512693f8543d96442deb3dca479b0e8ed6` |
| Result files | `eval/results/{dense,hybrid}-{none,routing,oracle}.json`, `eval/results/comparison.json` (sha256 `450870d8…`) |
| Determinism | `hybrid-none` and `hybrid-oracle` each re-run twice with `--overwrite`: `deterministic_sha256` identical every time (`0198d72d…`, `a522253c…`) |

The baseline is defined **after** R16. Six earlier runs (commit `757072e`) were discarded because hybrid results were not deterministic.

## What was measured

- **Items:** dev split, retrieval intents only. 19 answerable `product_search` items, 5 `product_search` no-match items (gold `no_answer`), 14 `policy_question` items; 38 queries in total.
- **Modes:** `dense` (bge-m3 dense vector) and `hybrid` (dense + sparse, 50 candidates each, RRF in Qdrant). Both use the R16 over-fetch and stable sort. k = 10 is retrieved once, and the metrics are computed for k = 1, 5, 10.
- **Filter settings:**
  - `none`: no filters at all.
  - `routing`: `doc_type` from the intent only (product_search → product, policy_question → policy). This measures the value of correct intent routing.
  - `oracle`: routing + the item's `expected_filters` **minus `keyword`** (DECISIONS R14). This measures the value of perfect filter extraction. Policy items have no expected filters, so for them oracle = routing.

## Metric definitions

For an item, G is its gold set, R the returned list (at most 10) and top-k = R[:k].

| Metric | Applies to | Definition |
|---|---|---|
| hit@k | product, policy | 1 if G ∩ top-k ≠ ∅, else 0 |
| **precision@k (over returned results)** | product | \|G ∩ top-k\| / min(k, \|R\|). Undefined when \|R\| = 0; such items are excluded and counted (0 in every run) |
| normalized recall@k | product | \|G ∩ top-k\| / min(k, \|G\|). Plain recall@k is misleading when \|G\| > k |
| MRR | product, policy | mean of 1 / rank of the first gold result (0 if none in R) |
| correct-empty | no-match items | 1 if \|R\| = 0. **Filter correctness check, not a quality metric** (see below) |
| latency p50 / p95 | per run | wall time of one `Retriever.search` call (query embedding + Qdrant + sort), warm model, 38 queries |

Every 95% interval is a percentile bootstrap over items (10,000 resamples, seed 20261009). Differences between runs use a **paired** bootstrap on per-item differences. A difference counts as distinguishable from noise only if its interval excludes 0. A breakdown cell with fewer than 8 items is marked *too small* and should not be interpreted.

## Headline results

### product_search (19 answerable items)

| Run | hit@1 | hit@5 | hit@10 | precision@5 (over returned) | nrecall@10 | MRR |
|---|---|---|---|---|---|---|
| dense-none | 0.632 [0.421, 0.842] | 0.895 [0.737, 1.000] | 0.947 [0.842, 1.000] | 0.347 [0.242, 0.474] | 0.916 [0.789, 1.000] | 0.736 [0.572, 0.889] |
| dense-routing | 0.632 [0.421, 0.842] | 0.895 [0.737, 1.000] | 0.947 [0.842, 1.000] | 0.347 [0.242, 0.474] | 0.916 [0.789, 1.000] | 0.736 [0.572, 0.889] |
| dense-oracle ⚠ | 1.000 | 1.000 | 1.000 | 0.937 [0.832, 1.000] | 1.000 | 1.000 |
| hybrid-none | 0.789 [0.579, 0.947] | 0.947 [0.842, 1.000] | 0.947 [0.842, 1.000] | 0.389 [0.274, 0.526] | 0.947 [0.842, 1.000] | 0.838 [0.684, 0.965] |
| hybrid-routing | 0.789 [0.579, 0.947] | 0.947 [0.842, 1.000] | 0.947 [0.842, 1.000] | 0.389 [0.274, 0.526] | 0.947 [0.842, 1.000] | 0.838 [0.684, 0.965] |
| hybrid-oracle ⚠ | 1.000 | 1.000 | 1.000 | 0.937 [0.832, 1.000] | 1.000 | 1.000 |

⚠ **Oracle product numbers are near-tautological; do not read them as ranking quality.** The gold SKU lists were computed by `apply_filters` from the very filters that oracle passes to Qdrant, so the filtered candidate set *is* the gold set. Answerable product items return only **2.84 results on average under oracle** (10.0 under none/routing), and 17 of 19 return exactly their gold set. Ranking is exercised only by the 2 items whose gold also depends on `keyword` (which oracle drops): gs-015 (7 candidates, 3 gold, precision@5 0.6) and gs-017 (7 candidates, 1 gold, precision@5 0.2). Both still rank a gold item first. What oracle does show is the ceiling: with perfect filters, product retrieval is solved on this catalog, so **the open problem is filter extraction and semantic ranking without filters**.

### policy_question (14 items)

| Run | hit@1 | hit@5 | hit@10 | MRR |
|---|---|---|---|---|
| dense-none | 0.643 [0.357, 0.857] | 0.857 [0.643, 1.000] | 0.857 [0.643, 1.000] | 0.750 [0.536, 0.929] |
| dense-routing = dense-oracle | 0.714 [0.500, 0.929] | 0.857 [0.643, 1.000] | 1.000 [1.000, 1.000] | 0.803 [0.616, 0.964] |
| hybrid-none | 0.571 [0.286, 0.786] | 0.857 [0.643, 1.000] | 0.857 [0.643, 1.000] | 0.685 [0.470, 0.881] |
| hybrid-routing = hybrid-oracle | 0.643 [0.357, 0.857] | 0.929 [0.786, 1.000] | 1.000 [1.000, 1.000] | 0.764 [0.586, 0.929] |

### Returned results and latency

| Run | mean returned (answerable) | answerable with 0 results | latency p50 | latency p95 |
|---|---|---|---|---|
| dense-none | 10.0 | 0 | 127.3 ms | 138.5 ms |
| dense-routing | 10.0 | 0 | 122.1 ms | 158.7 ms |
| dense-oracle | 5.88 (product 2.84, policy 10.0) | 0 | 144.1 ms | 188.0 ms |
| hybrid-none | 10.0 | 0 | 124.9 ms | 145.5 ms |
| hybrid-routing | 10.0 | 0 | 132.0 ms | 145.9 ms |
| hybrid-oracle | 5.88 (product 2.84, policy 10.0) | 0 | 139.6 ms | 176.0 ms |

Latency is dominated by embedding the query on CPU (about 120 ms). Dense and hybrid are indistinguishable at this resolution (38 queries per run, single machine, not hashed). Latency is not part of the deterministic result.

## Value of routing and of filter extraction (paired differences, dev)

Each cell is the mean per-item difference [95% CI]; "+b/−w" counts the items that got better or worse; **bold = distinguishable from noise**.

| Metric | dense: none → routing | dense: routing → oracle | hybrid: none → routing | hybrid: routing → oracle |
|---|---|---|---|---|
| product hit@1 | +0.000 (0 changed) | **+0.368 [+0.158, +0.579]** +7/−0 | +0.000 (0 changed) | **+0.211 [+0.053, +0.421]** +4/−0 |
| product hit@5 | +0.000 | +0.105 [+0.000, +0.263] +2/−0 | +0.000 | +0.053 [+0.000, +0.158] +1/−0 |
| product nrecall@10 | +0.000 | +0.084 [+0.000, +0.211] +3/−0 | +0.000 | +0.053 [+0.000, +0.158] +1/−0 |
| product precision@5 | +0.000 | **+0.589 [+0.442, +0.726]** +16/−0 | +0.000 | **+0.547 [+0.389, +0.695]** +15/−0 |
| product MRR | +0.000 | **+0.264 [+0.111, +0.428]** +7/−0 | +0.000 | **+0.162 [+0.035, +0.316]** +4/−0 |
| policy hit@1 | +0.071 [+0.000, +0.214] +1/−0 | (oracle = routing) | +0.071 [−0.143, +0.286] +2/−1 | (oracle = routing) |
| policy hit@10 | 0.857 → 1.000 (2 items recovered) | — | 0.857 → 1.000 (2 items recovered) | — |
| policy MRR | +0.053 [+0.000, +0.132] +3/−0 | — | +0.080 [−0.057, +0.229] +4/−1 | — |

Reading:
- **Routing** changes nothing for product queries: their gold products were never displaced by policy clauses inside the top 10. For policy queries, routing removes the products that crowd out clauses: 2 policy items with no gold clause in the top 10 under `none` find it under routing (hit@10 0.857 → 1.000). gs-113 reaches rank 7 (dense) / 5 (hybrid), gs-118 rank 10 / 6, and gs-032 moves to rank 1 in both modes. The hit@1 and MRR gains are small, and none is distinguishable at n = 14.
- **Filter extraction** (routing → oracle) is the large, distinguishable step for products in both modes (hit@1, precision@5, MRR). It is an upper bound inflated by construction (see ⚠ above). It says that good filters would fix most product errors; it does not say how good real extracted filters will be.

## Dense vs hybrid (paired, hybrid − dense)

| Setting | product hit@1 | product MRR | product precision@5 | policy hit@1 | policy MRR |
|---|---|---|---|---|---|
| none | +0.158 [+0.000, +0.316] +3/−0 | **+0.102 [+0.019, +0.202]** +5/−0 | +0.042 [+0.000, +0.095] +3/−0 | −0.071 [−0.286, +0.143] +1/−2 | −0.065 [−0.190, +0.060] +1/−4 |
| routing | +0.158 [+0.000, +0.316] +3/−0 | **+0.102 [+0.019, +0.202]** +5/−0 | +0.042 [+0.000, +0.095] +3/−0 | −0.071 [−0.214, +0.000] +0/−1 | −0.039 [−0.121, +0.014] +2/−2 |
| oracle | 0 (identical) | 0 | 0 | −0.071 [−0.214, +0.000] +0/−1 | −0.039 [−0.121, +0.014] +2/−2 |

**Plainly: at this sample size, dense and hybrid are mostly not distinguishable.** The only interval that excludes 0 is product MRR without attribute filters: +0.10, with 5 items better and none worse. Its lower bound (+0.019) is close to 0, and n = 19. For policies hybrid looks slightly worse (4 of 14 items lower in MRR under none), but every interval includes 0. Hybrid is a reasonable default for products and an open question for policies. **Neither claim should be considered established until a larger dev set or the final test evaluation.**

## Breakdown by query_style (cells with n < 8 are too small to interpret)

| Intent × style | n | dense-none hit@1 / hit@5 / MRR | hybrid-none hit@1 / hit@5 / MRR | interpretable? |
|---|---|---|---|---|
| product × co_dau | 3 | 0.333 / 1.000 / 0.583 | 0.667 / 1.000 / 0.750 | **too small** |
| product × khong_dau | 3 | 0.000 / 0.333 / 0.159 | 0.333 / 0.667 / 0.444 | **too small** |
| product × teencode | 13 | 0.846 / 1.000 / 0.904 | 0.923 / 1.000 / 0.949 | yes |
| policy × co_dau | 6 | 1.000 / 1.000 / 1.000 | 0.833 / 1.000 / 0.917 | **too small** |
| policy × khong_dau | 3 | 0.000 / 0.667 / 0.333 | 0.333 / 0.667 / 0.417 | **too small** |
| policy × teencode | 5 | 0.600 / 0.800 / 0.700 | 0.400 / 0.800 / 0.567 | **too small** |

Only product × teencode reaches 8 items. Most of its queries carry diacritics plus chat abbreviations (per LABELING.md), and they do well. The khong_dau cells are tiny but consistently the weakest in both intents and both modes; that is a direction to measure, not a finding. The full per-run breakdowns (routing, oracle) are in the result JSON files.

## Filter correctness check (not a quality metric)

The 5 no-match product items (gs-021, gs-105, gs-108, gs-110, gs-112) return an empty list in **5/5 under oracle** in both modes. This holds **by construction**: Qdrant applies exactly the filters that define "no match" (the Phase 3A property test proves Qdrant filtering equals `apply_filters`). It confirms the filter plumbing; it says nothing about quality. Under `none` and `routing` these items return 10 results (0/5 empty). Semantic retrieval never says "nothing matches"; that has to come from filters or a later answer step.

## Error analysis (dev, filter setting `none`, 10 worst per mode)

"Worst" = highest rank of the first gold result (not found = worst), then lowest normalized recall@10. Causes are a reading of the retrieved lists, not fixes.

### dense-none

| Item | Style | First gold rank | Cause (one line) |
|---|---|---|---|
| gs-018 "tui deo cheo duoi 400k co ko shop" | khong_dau | not in top 10 | **No diacritics**: "tui deo cheo" never matches "Túi đeo chéo"; the top results are a COD policy clause and slippers. The price constraint is ignored by semantics |
| gs-113 "phi van chuyen giao hang ben shop tinh the nao vay" | khong_dau | not in top 10 | **No diacritics + wrong doc type**: wallets and bags beat the shipping-fee clause; routing recovers it |
| gs-118 "lam the nao de gui yeu cau doi tra do vay mn" | teencode, no diacritics | not in top 10 | **No diacritics + wrong doc type**: slippers and backpacks instead of the return-procedure clause |
| gs-014 "quan kaki nam mau be size 32 con ko" | khong_dau | 7 | **Right kind, wrong attribute**: other khaki trousers (grey, black) outrank the beige one; colour without diacritics ("be") carries little weight |
| gs-013 "Cho mình xem kính mát gọng kim loại với" | co_dau | 4 | **Attribute ignored**: acetate-frame sunglasses rank above the only metal one; "kim loại" is in the text but the name says "phi công" |
| gs-104 "co quan jean nam denim mau xanh dam size 31 k shop" | teencode, no diacritics | 4 | **Near-colour confusion**: "xanh nhạt" and other "xanh đậm" jeans (without size 31) rank first; sizes are not used semantically |
| gs-002 "tim sneaker nu size 37 shop oi" | khong_dau | 3 | **Gender confusion**: men's sneakers outrank women's; "nu" (nữ) without diacritics is weak |
| gs-004 "Có váy liền nào dưới 400k ko shop?" | teencode | 2 | **Numeric constraint ignored**: a 669k dress ranks first; "dưới 400k" is not something embeddings enforce |
| gs-025 "đơn trên 500k có free ship ko shop?" | teencode | 2 | **Neighbouring clause**: the COD clause outranks the shipping-fee clause (both mention đơn/giao hàng) |
| gs-032 "do da bi moc vi de am thi co duoc bao hanh ko shop" | khong_dau | 2 | **Wrong doc type + no diacritics**: a leather wallet ("da") ranks above the warranty-exclusion clause |

### hybrid-none

| Item | Style | First gold rank | Cause (one line) |
|---|---|---|---|
| gs-018 | khong_dau | not in top 10 | **No diacritics**: as in dense; sparse adds dress shoes; neither branch maps "tui deo cheo" to the bags |
| gs-113 | khong_dau | not in top 10 | **No diacritics + wrong doc type**: jackets, wallets and bags; sparse tokens of unaccented words match nothing useful |
| gs-118 | teencode, no diacritics | not in top 10 | **No diacritics + wrong doc type**: T-shirts and slippers, as in dense |
| gs-013 | co_dau | 4 | **Attribute ignored**: acetate-frame sunglasses outrank the metal one, as in dense |
| gs-032 | khong_dau | 4 | **Wrong doc type + no diacritics**: a shipping-scope clause and a leather wallet come first |
| gs-014 | khong_dau | 3 | **Right kind, wrong attribute**: improves from 7 (dense) to 3, still behind grey and black khakis |
| gs-025 | teencode | 3 | **Neighbouring clause**: COD and order-tracking clauses outrank the shipping-fee clause |
| gs-104 | teencode, no diacritics | 3 | **Near-colour confusion**: "xanh nhạt" jeans first; improves from 4 to 3 |
| gs-024 "phi ship nhiêu vậy shop" | co_dau (typo "phi") | 2 | **Lexical false friend**: the typo "phi" (for phí) sparse-matches "Kính mát **phi công**", which ranks first |
| gs-026 "Đồ sale 50% thì có đổi trả được ko?" | teencode | 2 | **Neighbouring clause**: "Điều kiện được đổi trả" outranks "Sản phẩm không áp dụng đổi trả" |

### Patterns (for the next phase to measure, one change at a time)

1. **Missing diacritics is the dominant failure.** The 3 queries that fail completely in both modes (gs-018, gs-113, gs-118) are written without diacritics, as are 4 more of the 10 worst. bge-m3's dense and sparse representations of unaccented Vietnamese are far from the accented catalog text. Candidates: diacritic restoration or normalization of queries and/or documents, or indexing an unaccented copy of the text.
2. **Wrong document type.** Under `none`, products crowd out policy clauses (gs-113, gs-118, gs-032) and occasionally the reverse (gs-018). Routing alone recovers 2 policy items at k = 10, so an intent router is cheap and useful. The gain is not yet distinguishable at hit@1.
3. **Attributes and numbers are not enforced by semantics.** Colour (gs-014, gs-104), material (gs-013), gender (gs-002), size and price limits (gs-004, gs-018) are weakly encoded or ignored. Oracle filters fix all of these, so **filter extraction** is the highest-value product change, bounded by the ⚠ caveat.
4. **Neighbouring policy clauses.** Fee vs COD (gs-025), conditions vs exclusions (gs-026). Candidates: richer clause text or titles, or a reranker.
5. **Sparse false friends from typos.** "phi" → "phi công" (gs-024): lexical matching amplifies typos that dense would tolerate. Candidate: typo-aware normalization; also measure sparse weighting in fusion.
6. **Ties.** R16 makes ties deterministic by stable key. Preferring the higher dense score among RRF ties is a Phase 4 candidate (DECISIONS R16), not done.

Nothing above was changed in this phase.

## Limits of this baseline

- **Small n.** 19 answerable product and 14 policy items. Most intervals are ±0.15–0.25, and only product × teencode has n ≥ 8 among the style cells.
- **Oracle is an optimistic ceiling by construction** for products (see ⚠). Phase 4 should report LLM-extracted filters against both `routing` (floor) and `oracle` (ceiling), and should prefer metrics that still measure ranking when filters are imperfect (MRR, hit@1, precision@k over returned).
- **Synthetic catalog.** Product names contain kind, style and colour, which makes dense matching easier than on real catalogs.
- **Dev only.** The frozen test split (`4070dc68…`) is untouched and is for the final evaluation only.

## Reproduce

```bash
git checkout d8b4a61            # or a later commit; results record their own commit
docker compose up -d qdrant     # index built as in docs/SETUP.md (Phase 3A)
for m in dense hybrid; do for f in none routing oracle; do
  uv run python -m eval.run_retrieval_eval --mode $m --filters $f --overwrite
done; done
uv run python -m eval.compare_retrieval_runs --overwrite
```

The `deterministic_sha256` of each file must match:

| File | deterministic_sha256 |
|---|---|
| dense-none | `141245be47c997a6883f950b65464b537689b582d9cf6760d4e4d08f40ee5c77` |
| dense-routing | `991c6701f24c3e9d50cbc1cf75c260d1d9e876b0e04d385477689d37d0648660` |
| dense-oracle | `89e7f8eef049e313bed74c6c594d8fd96702e557849b18afa5c04a73db59ddbc` |
| hybrid-none | `0198d72d71ac96d8f3ebe7559ae790b6c5c3c25ecde69cfcb28165634bf8249a` |
| hybrid-routing | `951851ce9420292eadaee7d4a4c6bb6bcbae53c8aababbc5c5d4940e1b75fded` |
| hybrid-oracle | `a522253cc28b834e6e2e93a9afdaec5bc34100f79d7b8da927844f07bc44cdea` |
| comparison | `450870d894cf8729df937b9b938501084da6a0af9d4fab1a493c92d855852f85` |

These hashes include the git commit in their provenance, so re-running from a later commit gives different hashes but must give identical per-item results and aggregates.
