# Phase 5 prompt log (dev split only)

Rules (owner, 2026-10-10): a prompt is changed only for an error type that affects **at least 2 dev items**, never to fix a single item; at most **3 versions per component** in total; every version is logged with its dev scores and the error type it targeted; a new version that does not improve is dropped and the earlier one is kept (and that component stops iterating). Dev scores after iteration are optimistic.

**What counts as an improvement (written before any iteration, so it cannot bend to the results):** router: strictly higher dev accuracy, ties broken by macro-F1; extractor: strictly higher exact-match of the whole filter set WITHOUT the `in_stock` field (the golden label of gs-019 conflicts with the R32 convention, docs/GOLDEN-V2-ISSUES.md #1, so that item must not steer a prompt); in both cases no previously correct error-free category may get worse.

**Disclosure.** The author of v1 had read the dev questions while surveying the labeling conventions (needed to write the intent and price rules). The prompt examples are invented (a test checks that no dev question is copied into a prompt), but v1 is not a blind prompt, and its dev scores are optimistic even with zero iterations.

## Router

| Version | File | sha256 | Dev accuracy | Dev macro-F1 | Targeted error type | Decision |
|---|---|---|---|---|---|---|
| v1 | `prompts/router_v1.md` | `8e08414e3a35fd937b2167438782d16fd79e09f4de4595950f8b316d304fd07c` | 71 / 71 = 1.000 | 1.000 (6 classes) | none (first version) | **kept (chosen)** |

v1 errors on dev: **none** (no unusable answers, every class and every query style at 1.000). No error type exists, so there is nothing to target; v2 and v3 were not written.

Other measurements with v1 (not dev items, not used for decisions): the 8 hand-written probes, 7 of 8 as labeled (docs/RESULTS-phase5.md, section 5).

## Filter extractor

| Version | File | sha256 | Exact match (with `in_stock`) | Exact match (without `in_stock`) | Targeted error type | Decision |
|---|---|---|---|---|---|---|
| v1 | `prompts/filters_v1.md` | `27e2ade227970c84db5ee5ec975c0aad4429fd2de698616d249357431672f74a` | 23 / 24 = 0.958 | 24 / 24 = 1.000 | none (first version) | **kept (chosen)** |

v1 errors on dev: one non-exact item, **gs-019** ("còn hàng k shop", golden `in_stock: true`; the R32 rule gives no `in_stock`). That is one item and a label conflict, not a prompt defect, so by the rule it cannot justify a new version. No value was dropped as out of vocabulary, no unknown field was returned, no answer was unusable.

## Iterations

| Component | Versions written | Iterations (changes made after seeing dev results) | Chosen |
|---|---|---|---|
| router | 1 | **0** | v1 |
| extractor | 1 | **0** | v1 |

Why none: the only dev error of either component is the single gs-019 label conflict, which the rule excludes. This is also a limit: with v1 at the ceiling the dev set **cannot rank prompt versions** (any later version could at best tie), so Phase 8's test split is the only place where a real number will come from.

## Live requests spent (free tier, 500/day, ceiling 450)

Pacific-time day 2026-10-09, counter at the end of Step 2: 109 requests = 3 manual capability probes (survey) + 3 `live_llm` tests + 95 for the v1 dev run (71 router + 24 extractor, no retry, no 429) + 8 for the probes.
