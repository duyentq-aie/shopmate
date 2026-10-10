# Phase 4 experiment 1: diacritic-insensitive lexical retrieval (2026-10-10)

One change, measured: a new lexical signal that ignores diacritics (the `folded` vector, BM25 on diacritic-folded syllables), compared with the Phase 3B baseline `hybrid`. **Dev split and the dev-derived perturbation set only. The test split was not read.** Design and every decision: `docs/DECISIONS-phase4.md` (R17-R25). The decision rule was written, unit-tested and committed before any run.

## Verdict (pre-registered rule, filters = none)

**Both configurations satisfy the rule. By the rule's tie-break, `dense_folded` is adopted** (dense + folded, the folded branch replaces bge-m3 sparse): the two perturbation gains' 95% intervals overlap 0.59 of the shorter one (≥ 0.5 = "heavily"), so the simpler configuration wins. `dense_folded` also has the larger point gain on both pooled criteria and on most rows below (not on perturbation × product × co_dau, where n = 3), but the rule did not need that.

| | `dense_sparse_folded` (dense + sparse + folded) | `dense_folded` (dense + folded) |
|---|---|---|
| **1. Perturbation set, MRR vs baseline** (33 items, paired bootstrap 95%) | **+0.292** [+0.182, +0.401]; 22 better / 2 worse / 9 equal → CI excludes 0 ✓ | **+0.430** [+0.271, +0.583]; 23 better / 2 worse / 8 equal → CI excludes 0 ✓ |
| **2a. Original dev, product_search MRR** (19 items) | 0.838 → 0.908 (+0.070), not worse ✓ | 0.838 → 0.939 (+0.101), not worse ✓ |
| **2b. Original dev, policy_question MRR** (14 items) | 0.685 → 0.786 (+0.101), not worse ✓ | 0.685 → 0.911 (+0.226), not worse ✓ |
| **Adopt?** | yes | **yes** |

What the evidence supports, and what it does not (details below):
- Removing diacritics from the queries costs the baseline a lot (MRR 0.838 → 0.497 for products, 0.685 → 0.253 for policies on the perturbation set); both folded configurations recover most of it. This is the robust finding.
- On the original dev queries the gains are mostly not distinguishable from noise except `dense_folded` on policy_question MRR (+0.226 [+0.012, +0.464]); the rule asked only that they not be worse.
- Folding is not free: one real false friend ("đến" → "đen") and one neighbouring-clause loss appear among the 9 co_dau items (section 6). At n = 9 neither a harm nor its absence is established.
- `dense_folded` replaces bge-m3 sparse with the folded branch. An exploratory ablation (section 8, added after the verdict, not part of the rule) attributes essentially all of its perturbation gain to adding the folded branch; removing bge-m3 sparse is neutral there.

## Provenance

| Item | Value |
|---|---|
| Git commit (all 16 runs, `git_dirty: false`) | `4c8ee47c62fe138fd9797831b97f2fd713159cf6` |
| Index manifest sha256 | `8bee3a76b734c9d245e81f8a0727d7975e675e02b1b7ba8e0e32956712a7fb38` (`shopmate_vn_v2`, 269 points; Qdrant 1.19.1) |
| bge-m3 dense + sparse digest | `bef623a22aa134b5…`, identical in `shopmate_vn` (Phase 3B) and `shopmate_vn_v2` (copied, never recomputed) |
| Embedder / lexical fingerprints | `53b494c7da7e1203` / `9c2ab3e20f5f1b26` (k1 1.2, b 0.75, avg_len 70.9; not tuned) |
| Golden file / dev split sha256 | `89f0dd2d…` / `62fe8897…` (unchanged from Phase 3B) |
| Perturbation set | `eval/perturbation/dev_folded.json`, sha256 `3be1d846179bbd6b55eb973a14929c3ab6a7f2785cccd11b5bbf9d64c0cda506` |
| Result files | `eval/results/phase4-exp1/`: 15 runs of the experiment, 1 exploratory ablation run (`dense-none.dev_folded.json`, section 8) and `comparison-exp1.json`; the committed Phase 3B files are untouched |

**Baseline reproduction (R20 rule, decided before the runs).** The fused path of every mode was moved client-side (R25), so re-running the baseline was a real test. `hybrid-none` re-run: **`items` (38 of 38) and `aggregate` identical to the committed Phase 3B result**; the provenance differs only in `git_commit` and `index_manifest_sha256` (so `deterministic_sha256` differs, as R20 predicted). Same for `hybrid-routing`, and for the 14 policy items of `hybrid-oracle`. Nothing was adjusted to make them match.

## What was measured

- **Configurations:** `hybrid` (baseline: dense + bge-m3 sparse), `dense_sparse_folded`, `dense_folded`. All fused modes use the same client-side RRF, window of 50 and stable-key tie-breaking (R25); only the branches differ.
- **Query sets:** the 38 dev retrieval items (19 answerable product, 5 no-match product, 14 policy), and the perturbation set (the 19 + 14 answerable items with the question machine-folded, same gold; 24 of the 33 questions changed, 9 had no diacritics to begin with).
- **Filters:** `none` (decides), `routing` (reported), oracle for policy_question only (product oracle is a tautology, Phase 3B).
- **Metrics, bootstrap, definitions:** as in `docs/RESULTS-phase3b.md` (10,000 resamples, seed 20261009, paired differences, 95% percentile intervals; cells with n < 8 are marked \* and should not be interpreted).

## 1. Original dev set, filters = none (MRR, baseline → candidate, paired difference [95% CI])

| Intent × style | n | `dense_sparse_folded` | `dense_folded` |
|---|---|---|---|
| product, all | 19 | 0.838 → 0.908, +0.070 [+0.000, +0.158] | 0.838 → 0.939, +0.101 [−0.044, +0.263] |
| product × co_dau | 3\* | 0.750 → 0.750, 0 | 0.750 → 0.778, +0.028 [−0.667, +0.750] |
| product × khong_dau | 3\* | 0.444 → 0.833, +0.389 [+0.000, +0.667] | 0.444 → 0.833, +0.389 [+0.000, +1.000] |
| product × teencode | 13 | 0.949 → 0.962, +0.013 [+0.000, +0.038] | 0.949 → 1.000, +0.051 [+0.000, +0.154] |
| policy, all | 14 | 0.685 → 0.786, +0.101 [−0.054, +0.262] | 0.685 → 0.911, **+0.226 [+0.012, +0.464]** |
| policy × co_dau | 6\* | 0.917 → 0.917, 0 | 0.917 → 1.000, +0.083 [+0.000, +0.250] |
| policy × khong_dau | 3\* | 0.417 → 0.667, +0.250 [−0.500, +0.750] | 0.417 → 0.833, +0.417 [−0.500, +1.000] |
| policy × teencode | 5\* | 0.567 → 0.700, +0.133 [−0.033, +0.350] | 0.567 → 0.850, +0.283 [−0.033, +0.700] |

hit@1 / hit@5, all items: product 0.789 / 0.947 → 0.842 / 1.000 (`dense_sparse_folded`) and 0.895 / 1.000 (`dense_folded`); policy 0.571 / 0.857 → 0.643 / 1.000 and 0.857 / 1.000.

## 2. Perturbation set (diacritics removed by code), filters = none

| Intent × style (original style label) | n | `dense_sparse_folded` | `dense_folded` |
|---|---|---|---|
| product, all | 19 | 0.497 → 0.797, **+0.300 [+0.166, +0.441]** (+12 / −0) | 0.497 → 0.844, **+0.347 [+0.149, +0.534]** (+12 / −1) |
| product × co_dau | 3\* | 0.458 → 0.833, +0.375 [+0.000, +0.875] | 0.458 → 0.778, +0.319 [−0.667, +0.875] |
| product × khong_dau | 3\* | 0.444 → 0.833 (same questions as dev) | 0.444 → 0.833 |
| product × teencode | 13 | 0.518 → 0.780, **+0.263 [+0.118, +0.430]** | 0.518 → 0.862, **+0.344 [+0.168, +0.531]** |
| policy, all | 14 | 0.253 → 0.533, **+0.280 [+0.089, +0.458]** (+10 / −2) | 0.253 → 0.796, **+0.543 [+0.276, +0.785]** (+11 / −1) |
| policy × co_dau | 6\* | 0.250 → 0.576, +0.326 [+0.160, +0.472] | 0.250 → 0.867, +0.617 [+0.283, +0.917] |
| policy × khong_dau | 3\* | 0.417 → 0.667 (same questions as dev) | 0.417 → 0.833 |
| policy × teencode | 5\* | 0.158 → 0.400, +0.242 [+0.000, +0.575] | 0.158 → 0.689, +0.531 [+0.178, +0.883] |

hit@1 / hit@5, all items: product 0.368 / 0.684 → 0.632 / 0.947 and 0.737 / 1.000; policy 0.143 / 0.429 → 0.286 / 0.857 and 0.714 / 0.929.

**Limitation (also written into the file):** machine-stripped text is not how real users type (no typos, no abbreviations beyond what the original questions had), so this is a robustness test of diacritic handling, not a substitute for real queries. 9 of the 33 questions had no diacritics, so their difference comes from the added branch alone.

## 3. Routing and oracle (reported, not part of the rule)

| Set | Item group | Baseline MRR | `dense_sparse_folded` | `dense_folded` |
|---|---|---|---|---|
| dev, routing | product (19) | 0.838 | 0.908, +0.070 [+0.000, +0.158] | 0.939, +0.101 [−0.044, +0.263] |
| dev, routing | policy (14) | 0.764 | 0.827, +0.063 [−0.077, +0.221] | 0.839, +0.075 [−0.071, +0.236] |
| perturbation, routing | product (19) | 0.502 | 0.815, +0.313 [+0.174, +0.455] | 0.846, +0.345 [+0.145, +0.532] |
| perturbation, routing | policy (14) | 0.412 | 0.651, +0.239 [+0.104, +0.390] | 0.738, +0.326 [+0.162, +0.493] |
| dev, oracle (policy only; = routing for policies) | policy (14) | 0.764 | 0.827, +0.063 [−0.077, +0.221] | 0.839, +0.075 [−0.071, +0.236] |

Routing shrinks the policy gain on the original dev set (`dense_folded` +0.226 → +0.075; the baseline already reaches 0.764 once products cannot crowd out clauses) but not on the perturbation set (+0.326).

## 4. Exploratory subsets (filters = none, original dev; they decide nothing)

| Subset | n | `dense_sparse_folded` | `dense_folded` |
|---|---|---|---|
| The real khong_dau items (gs-002, 014, 018, 032, 113, 119) | 6 | MRR +0.319 [−0.056, +0.625]; +4 / −1 / =1 | MRR +0.403 [−0.056, +0.819]; +4 / −1 / =1 |
| Questions with no diacritic at all, machine-detected (adds gs-104, gs-107, gs-118, labelled teencode) | 9 | MRR +0.259 [+0.009, +0.491]; +6 / −1 / =2 | MRR +0.454 [+0.102, +0.778]; +6 / −1 / =2 |

On the real khong_dau items alone the interval still includes 0 (the reason the rule was amended: n = 6 cannot exclude 0 unless almost everything improves). The direction is the same as on the perturbation set.

## 5. The "phi" / "phi công" false-match check, and every co_dau item whose rank got worse

Dev has 9 answerable co_dau items (3 product, 6 policy). First gold rank, baseline / `dense_sparse_folded` / `dense_folded`:

| Item | filters none | filters routing |
|---|---|---|
| gs-013 (kính mát gọng kim loại) | 4 / 4 / **1** | 4 / 4 / **1** |
| gs-024 (phi ship nhiêu vậy shop) | 2 / 2 / **1** | 1 / **2** / **2** |
| gs-106 (giày sneaker nữ trắng size 37, 300000 đến 700000) | 1 / 1 / **3** | 1 / 1 / **3** |
| gs-023, 027, 029, 031, 109, 120 | 1 / 1 / 1 | 1 / 1 / 1 |

**Every co_dau item that got worse (4 cases, 2 items):**

1. **gs-106, `dense_folded`, filters none and routing, rank 1 → 3. A real folding false friend.** The gold is SM-GIAY-0002 (white slip-on sneaker). The query contains "đến" ("to", in the price range); it folds to `den`, which is the folded form of "đen" (black), so the black sneaker SM-GIAY-0014 ranks first in the folded branch. Branch ranks (dense / bge sparse / folded): gold 2 / 1 / 3, SM-GIAY-0014 4 / 3 / 1, SM-GIAY-0032 (white low-top, 999k) 1 / 2 / 4. In `dense_folded` the RRF scores are 0.700 (0014), 0.700 (0032, an exact tie settled by stable key) and 0.583 (gold), so gold falls to 3. In `dense_sparse_folded` the bge-m3 sparse branch (which keeps "đến" and "đen" apart) holds gold at 1 (1.083 vs 1.033 vs 0.950). Dropping the sparse branch removed that protection.
2. **gs-024, both candidates, filters routing only, rank 1 → 2. Neighbouring-clause confusion, not a false friend.** The query is the typo "phi ship nhiêu vậy shop"; folding lets "phi" match every "phí" clause, and "Phí đổi trả" (doi-tra:4) edges out the gold "Phí vận chuyển" (van-chuyen:2). The matched token is the intended effect of folding; the loss is the Phase 3B pattern 4 (adjacent policy clauses).

**The feared "phi công" case did not materialise.** In Phase 3B gs-024 failed under filters none because the typo "phi" matched "Kính mát **phi công**" *exactly* (the query contains the literal token "phi"; this is the baseline's own false match, not one created by folding). Folding did not make it worse: `dense_folded` ranks the gold first (the sunglasses drop to third), and `dense_sparse_folded` is unchanged (gold 2, sunglasses 1). No co_dau item worsened because of "phi công". The harm that folding did cause is of the same family (two words whose only difference is a diacritic), in a different pair (đến / đen).

With 9 items this is a list of cases, not an estimate of a harm rate: co_dau × product and co_dau × policy are both below 8 items.

## 6. Cost

Latency p50 (one run per configuration, not hashed, single machine): baseline `hybrid` 192 ms, `dense_folded` 207 ms, `dense_sparse_folded` 219 ms. The Phase 3B baseline had 125 ms: client-side fusion (R25) issues one request per branch, each returning the whole collection, plus a count, so **every fused mode is about 65 ms slower than in Phase 3B**. `dense_folded` is the faster of the two folded configurations (two branches, no bge-m3 sparse), by 12 ms at p50, which is within what a single run can resolve.

## 7. Caveats

- **Two configurations were tested against the same baseline** (multiple comparisons): two chances to pass the rule by luck, which the rule does not correct for. Both passes are far from the boundary on the perturbation criterion (lower interval bounds +0.18 and +0.27), but the pass on the original-dev criterion is only "not worse".
- **Ties are broken by stable key** (R25), everywhere in the fused path. Exact score ties are common in the folded branch (53 of 54 hand-written queries had them in the top 50), so part of any measured difference may come from that arbitrary but fixed and reproducible order; a plain BM25 would break them in an arbitrary per-build order.
- **`dense_sparse_folded` mixes two effects:** two lexical branches of three means dense carries 1/3 of the RRF weight instead of 1/2.
- **`dense_folded` mixes two effects:** it adds the folded branch and removes bge-m3 sparse. On the perturbation set the ablation (section 8) separates them: removing sparse is neutral, adding folded carries the gain. On the original dev set no paired ablation was run; Phase 3B already found hybrid slightly worse than dense on policies (hit@1 0.571 vs 0.643), so part of the policy gain there may still come from dropping the sparse branch. Phase 3B's `dense-none` (unchanged `dense` mode, same dense vectors) has MRR 0.736 (product) and 0.750 (policy) on the original dev set against 0.939 and 0.911 for `dense_folded`; that comparison is across commits and untested.
- **Small n** (19 + 14 answerable dev items; 3 to 6 per query-style cell on the cells that matter for the hypothesis). The perturbation set is the only place where an interval excludes 0 with margin.
- **Synthetic catalog,** product names already contain kind and colour, which makes lexical matching easier than on real data.
- **Single run per configuration:** results are reproducible on this index and commit (the determinism integration test repeats every dev query 20 times for every mode), not re-sampled.

## 8. Ablation: `dense` alone (EXPLORATORY: added after the verdict at the owner's request; it does not affect the decision)

Question: is the gain of `dense_folded` due to **adding the folded branch** or to **removing bge-m3 sparse** (it does both)? One extra run, `dense` (unchanged mode) on the perturbation set, filters none. The runner refuses a dirty working tree and this document was uncommitted, so the run was made from a clean clone at the same commit `4c8ee47` and the result file copied here; its provenance matches the 15 other runs field by field (commit, manifest, embedder, dev split, golden file, perturbation hash).

MRR / hit@1 / hit@5 on the perturbation set (33 items):

| Configuration | product (19) | policy (14) |
|---|---|---|
| `hybrid` (baseline: dense + bge-m3 sparse) | 0.497 / 0.368 / 0.684 | 0.253 / 0.143 / 0.429 |
| `dense` (dense only) | 0.435 / 0.316 / 0.684 | 0.304 / 0.143 / 0.429 |
| `dense_sparse_folded` (adds folded to hybrid) | 0.797 / 0.632 / 0.947 | 0.533 / 0.286 / 0.857 |
| `dense_folded` (dense + folded) | 0.844 / 0.737 / 1.000 | 0.796 / 0.714 / 0.929 |

Paired MRR differences, pooled over the 33 items [95% CI] (per intent in brackets):

| Contrast | What it isolates | Pooled | product | policy |
|---|---|---|---|---|
| `dense` − `hybrid` | removing bge-m3 sparse | **−0.014** [−0.105, +0.070]; +8 / −9 / =16 | −0.062 [−0.191, +0.057] | +0.051 [−0.062, +0.158] |
| `dense_folded` − `dense` | adding folded to dense | **+0.444** [+0.305, +0.589]; +22 / −2 / =9 | +0.409 [+0.243, +0.579] | +0.492 [+0.251, +0.731] |
| `dense_sparse_folded` − `hybrid` | adding folded to hybrid | +0.292 [+0.182, +0.401]; +22 / −2 / =9 | +0.300 [+0.166, +0.441] | +0.280 [+0.089, +0.458] |
| `dense_folded` − `hybrid` | both at once (the rule's candidate) | +0.430 [+0.271, +0.583]; +23 / −2 / =8 | +0.347 [+0.149, +0.534] | +0.543 [+0.276, +0.785] |

The first two rows add up to the last one (−0.014 + 0.444 = 0.430).

Reading, within what one run on 33 items allows:
- **On text without diacritics, removing bge-m3 sparse has no detectable effect** (pooled −0.014, interval around 0, 8 items better and 9 worse). Per intent the signs differ (product −0.062, policy +0.051) and neither interval excludes 0, so this is absence of evidence, not evidence of no effect.
- **The gain of `dense_folded` comes from the folded branch**: adding it to dense gives +0.444 with an interval far from 0, and adding it to hybrid gives +0.292 (interval also far from 0). Why adding folded to dense gives more than adding it to hybrid (+0.444 against +0.292) cannot be attributed from these runs: it mixes the RRF weight of dense (1/2 against 1/3) with the presence of the sparse branch.
- **Not shown:** the same split on the original dev queries (no `dense` run there under this commit), and anything about real users' queries; the perturbation set is machine-stripped text.

## 9. What this does not show, and the obvious next measurements

Not done here (out of scope for "one change, measured"): the `dense` ablation on the original dev set (paired); a larger or independent set of real unaccented queries; typo- and abbreviation-aware normalisation; a larger dev set so that the co_dau cells reach n ≥ 8; and routing by diacritic presence, recorded as a future candidate with its overfitting risk in DECISIONS R26.

## 10. How this experiment changed along the way (full log in DECISIONS)

- The first version of the rule (n = 6 real khong_dau items) was replaced by the perturbation criterion before any run (R21).
- Qdrant cannot add a vector to an existing collection (R18); `dense` and `sparse` were copied bit for bit.
- Qdrant's order for exact score ties turned out arbitrary per build and per `limit` (R23). An epsilon tie-break inside the vectors was adopted and **retired the same day, before any evaluation**, because it reordered documents with different scores (property held for 2 of 71 queries); fusion became client-side with stable-key ties and unmodified scores (R25).
- After the verdict, the owner asked for the exploratory `dense` ablation (section 8); it was run from a clean clone at the experiment commit because the working tree was dirty, and the comparison tool gained an `ablation_dense` section that leaves every other section byte-identical (checked).
- After the runs, the comparison tool's comparability check was fixed (a policy-only oracle run legitimately hashes a different item set than a 38-item run). No result file was involved; the tool change is in the working tree next to this document.

## Reproduce

```bash
docker compose up -d qdrant                         # index shopmate_vn_v2 built as in DECISIONS R18 / scripts/build_index.py
for m in hybrid dense_sparse_folded dense_folded; do
  for f in none routing; do for q in dev dev_folded; do
    uv run python -m eval.run_retrieval_eval --mode $m --filters $f --queryset $q --results-dir eval/results/phase4-exp1 --overwrite
  done; done
  uv run python -m eval.run_retrieval_eval --mode $m --filters oracle --intents policy_question --results-dir eval/results/phase4-exp1 --overwrite
done
# exploratory ablation (section 8), from a clean tree at the same commit
uv run python -m eval.run_retrieval_eval --mode dense --filters none --queryset dev_folded --results-dir eval/results/phase4-exp1 --overwrite
uv run python -m eval.compare_exp1 --results-dir eval/results/phase4-exp1 --overwrite
```

`deterministic_sha256` of each file (they include the git commit and manifest hash, so a later commit changes them while per-item results stay identical):

| File | deterministic_sha256 |
|---|---|
| hybrid-none | `642c8deda871e9edd49304e62d7ffd49d1ac74e8a98333f1502d2f1c13a89d11` |
| hybrid-none.dev_folded | `137ca6ed58c1d154b6447db75c34bc4c3f57aefe67e3999f4365e93d37ebd459` |
| hybrid-routing | `568051692ce37edabad6b4687931a773616b19516079b42710727d43081f410b` |
| hybrid-routing.dev_folded | `8066618f114cd8673edd3dadd47fd7cba13d6b0d6f778790e4b2ab6c30c46cc9` |
| hybrid-oracle.policy_question | `ec40cb705692be776b9272f34c874b96a18017824ee52a4b7ef33bc959b7003e` |
| dense_sparse_folded-none | `01f6d56ada9456eaf155d1625eb7d3e55198c5cddbc20dc6fafa01fac3f6b08a` |
| dense_sparse_folded-none.dev_folded | `222d44baf85fec9548bf1d4a400faca3abc43192c0892c90690be3bdbc8936e5` |
| dense_sparse_folded-routing | `0a8cc67323338927feee0d3c4228c2cc8848477535c6f4dd568ab744f14dbb4f` |
| dense_sparse_folded-routing.dev_folded | `af39ae014b03ada9ee26e552ffe090e6a84186403a518a3bb2e67292454285c4` |
| dense_sparse_folded-oracle.policy_question | `8a277a3cf0c4f7225a8f1e044c1e7ace0d888aa95a7ae9dd7b07cc9323a466d8` |
| dense_folded-none | `b71333879c86d7adbbd00834fc3e9c6d167b52b0807876358c24f35441c97b6a` |
| dense_folded-none.dev_folded | `c40d68a203d40955283d36f52804bb505d8f58e20639eb9ff9065cf8ca40a6f9` |
| dense_folded-routing | `fa785dfed58f51233c4e7d4332b538a66e3f6228fdd2107a1e30e74752048645` |
| dense_folded-routing.dev_folded | `e4bc3ef934f3a8a3d7c73d5ed75475a854e9039357b3417ddac750bf0933e152` |
| dense_folded-oracle.policy_question | `3faa01b6ed839582d1767463d5c8ded05ae96b3cc270fa813bc64a69ee2d321b` |
| dense-none.dev_folded (exploratory ablation) | `3cd37d2180432efee12305a0a1effbd34b17b50c2cf5c65975c8f3f0da909af5` |
| comparison-exp1 | `11e0d55dec279eadf3c6c105b02cc58568cf638fd4c87475e32eee980eee17c2` |
