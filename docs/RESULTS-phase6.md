# Phase 6 results: the bounded agent on the dev split

Everything here is **dev only** and **optimistic**: the author has seen the dev items in earlier phases, the prompt and the scoring rules were written with that knowledge, and a second prompt version (section 9) is judged on the same items. The test split was not read and `final_evaluation` was never set. Numbers come from `eval/results/phase6/answer_v1.json` (the official result, `deterministic_sha256` `952cace0ed70…`) and from `eval/results/phase6/answer_v1.analysis.json` (an offline replay that adds the fallback rate, the claim check and the error types; it agrees with the official result item by item).

| | |
|---|---|
| Run | `uv run python -m eval.run_phase6_eval --live`, git commit `53f3c40`, clean tree, 2026-10-10 |
| Model | `gemini-3.5-flash-lite`, temperature 0, thinking level minimal, structured output, free tier |
| Prompts | router_v1, filters_v1 (Phase 5, unchanged) and `answer_v1` (`ae17b6aacba9…`) |
| Scoring | `eval/phase6_scoring.py` (`5d3d873baa8b…`) and the 13 cases `eval/phase6/multiturn_cases.json` (`06136a8ba81b…`), both committed before the run |
| Calls | 74 live requests (the first turn of every dev question reused the 71 router and 24 extractor answers of Phase 5; 45 answers and 29 router/extractor answers for the case messages were new); 97 cache hits. Quota counter after the run: 87 of 450 |
| Cost | free tier, 0 USD |

## 1. The result: accuracy and guardrail fallback rate, side by side

The **fallback rate** is a primary quality metric, not a footnote: when a guardrail rejects the text the model wrote, the customer reads the deterministic answer built from the tool results instead. A high accuracy with a high fallback rate would mean the score comes from the fixed answers, not from the model.

| Intent | Items | Correct | Accuracy (95% Wilson) | Answers written by the model | Fallbacks | **Fallback rate** (of answers; 95% Wilson) | Fallbacks / items | Correct thanks to: model text / fallback answer / fixed reply |
|---|---|---|---|---|---|---|---|---|
| product_search | 24 | 24 | 1.000 (0.862–1.000) | 21 | 4 | **19.0%** (7.7–40.0%) | 16.7% | 17 / 4 / 3 |
| policy_question | 14 | 13 | 0.929 (0.685–0.987) | 14 | 2 | **14.3%** (4.0–39.9%) | 14.3% | 12 / 1 / 0 |
| order_status | 12 | 11 | 0.917 (0.646–0.985) | 4 | 0 | **0.0%** | 0.0% | 4 / 0 / 7 |
| create_ticket | 7 | 7 | 1.000 (0.646–1.000) | 0 | n/a | n/a (no model answer) | n/a | 0 / 0 / 7 |
| out_of_scope | 7 | 7 | 1.000 (0.646–1.000) | 0 | n/a | n/a (no model answer) | n/a | 0 / 0 / 7 |
| smalltalk | 7 | 7 | 1.000 (0.646–1.000) | 0 | n/a | n/a (no model answer) | n/a | 0 / 0 / 7 |
| **all dev items** | **71** | **69** | **0.972 (0.903–0.992)** | **39** | **6** | **15.4%** (7.2–29.7%) | 8.5% | **33 / 5 / 31** |
| scripted multi-turn answers | 13 cases | 13 | 13 of 13 cases passed | 6 | 1 | 16.7% (case mt-01) | n/a | |
| dev items and cases together | | | | 45 | 7 | **15.6%** (7.7–28.8%) | | |

How to read it:
- Of the 69 correct items, **only 33 are credited to text the model wrote**; 5 to the deterministic answer that replaced it, and 31 to fixed replies (questions, refusals, hand-offs, small talk, "nothing found"). Create_ticket, out_of_scope and smalltalk (21 items) never reach the answer step: their 100% measures the router (Phase 5: 71 of 71) and the fixed templates, not answer generation.
- The two intents where the model writes most of the text, product_search and policy_question, lose **about one answer in six** to a fallback (order_status: 0 of 4). The intervals are wide (n = 21 and 14).
- A fallback is not a wrong answer: the five fallback answers that scored correct name a gold SKU or cite the gold clause because they are built from the right tool results. What the rate measures is how often the model's own text could not be trusted by the code.

Pre-registered scoring rules (A4, A15) were applied unchanged. `no_answer` items: 5 of 5 (two had a relaxed suggestion with the dropped constraint named first, three had no result at all).

## 2. What the 7 fallbacks were (every one read by hand)

| Where | Rule that fired | What the model wrote | Verdict |
|---|---|---|---|
| gs-002 | `stock_mismatch` ×2 | one sentence listing 5 shoes, "còn hàng" or "hết hàng" after each, all correct | **false alarm** of the conservative sentence rule (A18.4): a sentence that names several SKUs with different stock and a stock claim is rejected as ambiguous |
| gs-009 | `price_mismatch`, `stock_mismatch` | "ví da nam **dưới 500k**: A (SKU …) giá 429.000đ còn hàng, B …" in one sentence; every price and stock status correct | **false alarm** in substance: the owner's rule (a price in the sentence of a SKU must be that SKU's) was applied as written to the customer's budget "500k", and the list is one ambiguous stock sentence |
| gs-015 | `stock_mismatch` ×2 | one sentence, 3 gym shirts, stock statements correct | **false alarm** (same as gs-002) |
| gs-112 | `stock_mismatch` ×2 | one sentence, 5 sunglasses, stock correct | **false alarm** (same) |
| mt-01 | `stock_mismatch` ×2 | one sentence, 5 shirts, stock correct | **false alarm** (same); the case still passed |
| gs-027 | `unknown_clause` | "…đạt điều kiện tại Điều 3" in an answer about refunds | **false alarm**: "Điều 3" is a cross-reference written inside clause 6 itself, which the model quoted; the guard only knows the clause numbers of the hits. The fallback (clause 6 verbatim) is a correct answer |
| gs-025 | `no_clause_cited` | "Xin lỗi… không có thông tin về chính sách miễn phí vận chuyển cho đơn trên 500k" | the model was **honest**: the three clauses retrieved (doi-tra 2, van-chuyen 5, van-chuyen 1) do not contain the free-shipping rule (van-chuyen 2). The fallback then printed the first retrieved clause, which is irrelevant: the item is **incorrect**, and the fallback made a harmless "I do not know" look like an answer |

**None of the 7 fallbacks stopped a fabricated fact.** On this dev run the guardrails' protection was exercised only by the unit tests (the "áo này 99k đúng không shop?" attack and the other hand-made cases of `tests/test_agent_guardrails.py` and `tests/test_agent_answer.py`, and 44 mutations of the checks), not by a model that lied. Their cost was real: 7 of 45 answers (15.6%) were replaced.

## 3. Unsupported claims about size, colour, material or number of days (the known gap)

The guardrails do not check these four kinds of claim (A18.4). So that the gap is visible, `eval/analyze_phase6.py` reads every answer the model wrote (after the guardrails, so fallbacks are excluded: they are built from the evidence and cannot state more than it says) and reports any size, colour, material or number of days that the tool results of that answer do not support. It is a **heuristic that reads text**: it can miss a claim and report a harmless one, so it is not a guardrail and not a score.

- **Result: 0 claims found** in the 33 dev answers and the 5 multi-turn answers written by the model (38 answers).
- **Checked by hand as well**: every one of the 38 texts was read against its evidence. Colours and materials appear only as the hits' own (for example "chất liệu len" for hits whose material is Len, "màu xám" for a grey hit); sizes appear only where the hits have them ("size 42", "size 41"); every figure with "ngày", "giờ" or "tháng" is in the clause texts (7 ngày, 2–5 ngày, 7–14 ngày làm việc, 24 giờ, 3 ngày, 14 giờ, 12 tháng; "1-2 ngày" differs from the clause's "1–2 ngày" only in the dash). No answer was found that states a size, colour, material or number of days that the tool results do not support.
- What this does **not** show: that the model will never do it (38 answers, one model, temperature 0), nor that the detector sees every phrasing. The by-hand reading was done by the author of the code, not by an independent reader.
- Known weaknesses seen while reading: the answers sometimes repeat the customer's size ("size 42") as if every listed product had it (they do, in these cases, because the retrieval filtered on it); a relaxed answer such as gs-105 is introduced by the fixed notice, so the colour that was dropped is announced by the code.

## 4. Bounds

Every bound of `docs/AGENT-DESIGN.md` section 3 held on every conversation of the run (71 dev first turns, the two simulated confirmation conversations of the one ticket that was proposed on dev (gs-127: one answered "có", one "không"), and 31 case turns):

| Bound | Limit | Largest value seen | Held |
|---|---|---|---|
| steps per turn | 8 | 7 (5 turns: the documented longest path) | yes |
| clarifying questions for one request | 2 | 2 (cases mt-02, mt-04) | yes |
| product searches per turn | 2 (one relaxation) | 2 | yes |
| failed verifications per conversation | 3 | 3 (case mt-05, after which lookup stayed disabled) | yes |
| ticket confirmation asks | 2 | 2 (case mt-08) | yes |
| tickets written without an explicit yes | 0 | 0 (the yes conversations wrote one ticket each, the no conversations none) | yes |

Steps per dev first turn: 3 steps ×14, 4 ×25, 5 ×7, 6 ×20, 7 ×5. No turn was capped.

## 5. The scripted multi-turn cases (13 of 13)

All 13 cases passed turn by turn (31 turns): clarify then answer (mt-01), two questions then a search (mt-02), email asked then order answered (mt-03), two questions then a hand-off (mt-04), three wrong emails then lookup disabled even for the right one (mt-05), ticket yes (mt-06), ticket no (mt-07), unclear twice then no ticket and not stuck (mt-08), one relaxation announced before the suggestions (mt-09), complaint without an order (mt-10), topic change drops the old question (mt-11), phone number is not a verification (mt-12), injection gets a polite refusal and no tool (mt-13). LLM calls per case: 1 to 7. They test what the real router and extractor make of hand-written messages; all 13 held on the first run.

## 6. LLM calls, tokens, latency, cost

- **LLM calls per turn:** at most 3 (router, extractor, answer). Dev first turns: 1 call ×29 items (small talk, out of scope, tickets, and order questions that only ask for the email), 2 calls ×21 (policy, order answers, product searches without a result), 3 calls ×21 (product searches with an answer). Mean over all 102 turns (dev and cases): 1.66.
- **Tokens** (dev first turns): router 71 calls, 892 tokens in and 11 out on average; extractor 24 calls, 1,535 in and 72 out; answer 39 calls, 1,507 in and 115 out.
- **Latency per turn** (a measurement, not part of the hashed result): p50 **1.65 s**, p95 **4.75 s** over 102 turns, being the recorded LLM latencies of the cache plus the retrieval, order lookup and reply time measured in this run.
- **Theoretical paid-tier cost** per 1,000 conversations: **0.918 USD** (router 0.296, extractor 0.216, answer 0.406) at 0.30 USD per million input tokens and 2.50 per million output tokens (https://ai.google.dev/gemini-api/docs/pricing, Gemini 3.5 Flash-Lite, paid tier, Standard, "Last updated 2026-10-09 UTC", read 2026-10-10). A conversation here is one dev item of one turn; the dev mix (34% product searches, 55% of items answered by the model) is not a traffic forecast. Phase 5 priced the router and extractor at 0.51 USD per 1,000; the answer step adds about 0.41. Input tokens dominate; context caching would be the obvious saving on a paid tier, not evaluated. Free-tier cost: 0.

## 7. Error analysis

Two dev items are incorrect.

1. **gs-025** (policy, "đơn trên 500k có free ship ko shop?", gold `van-chuyen` Điều 2). Cause: **retrieval**, the gold clause is not among the 3 clauses given to the answer step (doi-tra 2, van-chuyen 5, van-chuyen 1). The model said it had no information (correct given what it saw); the guardrail then rejected a reply that cites nothing and the **fallback design** printed the first retrieved clause (the return period), irrelevant to the question. Not an answer-text error in the sense of A17 (a single `no_clause_cited` item; E3 is empty because the gold clause was never retrieved). Candidate fix, **not applied** (no tuning beyond the version limit): treat "the model declined" as its own outcome and send the fixed "the shop has no information" reply instead of an irrelevant clause; retrieve more than 3 clauses.
2. **gs-124** (order, "Mình muốn xem tiến độ giao hàng **mã 88**, email là khachhang10@example.com", label: refuse, the email is not the owner's). Observed: the agent asked for the order number again. Cause: the **order-number parser** reads a number after "đơn", "order" or "#", not after "mã", so it saw an email and no order number. Safe (nothing was revealed, nothing was looked up) but not the labelled behaviour. Not fixed: a parser change after seeing dev would be tuning; the gap is recorded for the work before the final evaluation.

Things that are correct but fragile, so that the headline is not over-read:
- 5 of the 6 dev items with a fallback are credited correct because the fallback answer is built from good tool results; the model's own text for them was also factually right.
- Three of the five `no_answer` items have no suggestion at all (nothing matches even after one relaxation); the two with suggestions (gs-105 colour, gs-112 colour) name the dropped constraint first.
- The multi-turn cases are 13 conversations written by the author; passing them shows the bounds hold, not that every conversation works.

## 8. Cache and reproduction

```bash
docker compose up -d qdrant                                  # and Saleor channel vn for the order lookups
uv run python -m eval.run_phase6_eval --live ...             # first run: fetch what the cache lacks (74 requests here)
uv run python -m eval.run_phase6_eval --overwrite ...        # replay: 0 live calls, same deterministic_sha256
uv run python -m eval.analyze_phase6                         # fallback rates, claim check, error types (offline)
```
The answers of the model are in `eval/llm_cache/answer/` (45 entries: 39 dev, 6 cases); the router and extractor answers of the case messages that Phase 5 did not have are 23 new files in `eval/llm_cache/router/` and 6 in `eval/llm_cache/filters/`. The runner stops at a clean point (`<prompt>.progress.json`) when fewer than 4 live requests remain of the day's 450 or a quota error occurs; this run did not need it.

## 9. Pre-registered rule for a second prompt (A17) and its outcome

Error types on the v1 run, counted by the analysis (`E1` by rule, `E2` missing SKU though retrieved, `E3` missing citation though retrieved):

| Error type | Items | Allows a v2? |
|---|---|---|
| E1 `stock_mismatch` | **4 dev items** (gs-002, gs-009, gs-015, gs-112) and the case mt-01 | **yes** (at least 2 dev items) |
| E1 `price_mismatch` | 1 (gs-009) | no |
| E1 `no_clause_cited` | 1 (gs-025) | no |
| E1 `unknown_clause` | 1 (gs-027) | no |
| E2 missing gold SKU although retrieved | 0 | no |
| E3 missing gold clause although retrieved | 0 | no |

So `answer_v2` is **allowed**, and may target only `stock_mismatch`. It is written (`prompts/answer_v2.md`, logged in `docs/PROMPT-LOG-phase6.md`): the only change is one rule and one example asking the model to write each product in a sentence of its own, with exactly one SKU and that product's price and stock status in it. The guardrail is not changed (the guardrail rules are not an error type for a new prompt). Adoption rule: v2 replaces v1 only if no intent's number of correct items is lower, the cases passed are not fewer, and the items with a `stock_mismatch` fallback are strictly fewer than 4.

**Outcome: not run yet.** The v2 run waits for the commit of this result and of the prompt (STOP 2). This section will be completed with the v2 numbers and the decision.

## 10. Limits to keep in mind

- Dev only, 71 items, one run, one model at temperature 0 (cached): the intervals above are wide. Nothing here is a test result.
- The scoring checks that the reply names a gold SKU or cites the gold clause, or that the behaviour matches the label. It does not judge fluency, helpfulness or whether a long answer buries the point.
- The retrieval was not changed (out of scope): the one retrieval-limited failure (gs-025: the gold clause is not in the top 3) is Phase 4 behaviour.
- Price and stock come from the index snapshot, not from live Saleor.
- Orders: the real Saleor with the 30 seeded orders (synthetic data); the failed-verification counter is per conversation (a new conversation resets it, A7).
- Everything is synthetic data on the Gemini free tier, whose inputs may be used by Google (docs/DECISIONS-phase5.md, R27): no real customer data went through the key.

## 11. Disclosure: exposure of one test-split question (A0)

During the Phase 6 survey one test-split question text (no label, no id) was printed by a command run by the author. Its SHA-256, `3b9975a677521f1dda7bac87ffa7098bc3d748f5724545ee185947a1e86d8692`, was recorded from the author's context without reopening the golden file. **The Phase 8 report will give the test score on all test items and on all test items excluding the one whose question hash matches this value.** This run read golden items only through `load_split("dev")` (`tests/test_golden_access.py` fails otherwise) and the result file records `golden_items_read: dev only`.
