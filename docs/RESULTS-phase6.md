# Phase 6 results: the bounded agent on the dev split

Everything here is **dev only** and **optimistic**: the author has seen the dev items in earlier phases, the prompt and the scoring rules were written with that knowledge, and a second prompt version (section 9) is judged on the same items. The test split was not read and `final_evaluation` was never set. Numbers come from `eval/results/phase6/answer_v1.json` (the official result, `deterministic_sha256` `952cace0ed70…`) and from `eval/results/phase6/answer_v1.analysis.json` (an offline replay that adds the fallback rate, the claim check and the error types; it agrees with the official result item by item).

**Reading order.** Sections 1–8 are the **v1** run, as it was reported (not rewritten). Section 9 gives the **v2** run and the decision of the pre-registered rule A17 (v2 adopted). Section 12 is the before/after of the bug-fix change that followed (A21, a code change, not a prompt change). Section 13 says plainly what the guardrails have and have not been shown to do.

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
| gs-025 | `no_clause_cited` | "Xin lỗi… không có thông tin về chính sách miễn phí vận chuyển cho đơn trên 500k" | the model was **honest**: the three clauses retrieved (doi-tra 2, van-chuyen 5, van-chuyen 1) do not contain the free-shipping rule (van-chuyen 2). The fallback then printed the first retrieved clause, which is irrelevant: the item is **incorrect**, and the fallback made a harmless "I do not know" look like an answer (**fixed afterwards, section 12**) |

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

1. **gs-025** (policy, "đơn trên 500k có free ship ko shop?", gold `van-chuyen` Điều 2). Cause: **retrieval**, the gold clause is not among the 3 clauses given to the answer step (doi-tra 2, van-chuyen 5, van-chuyen 1). The model said it had no information (correct given what it saw); the guardrail then rejected a reply that cites nothing and the **fallback design** printed the first retrieved clause (the return period), irrelevant to the question. Not an answer-text error in the sense of A17 (a single `no_clause_cited` item; E3 is empty because the gold clause was never retrieved). Candidate fix, **not applied** (no tuning beyond the version limit): treat "the model declined" as its own outcome and send the fixed "the shop has no information" reply instead of an irrelevant clause; retrieve more than 3 clauses. *Update: the first part was applied after the v2 decision as a separate bug fix (A21, section 12); the retrieval is unchanged.*
2. **gs-124** (order, "Mình muốn xem tiến độ giao hàng **mã 88**, email là khachhang10@example.com", label: refuse, the email is not the owner's). Observed: the agent asked for the order number again. Cause: the **order-number parser** reads a number after "đơn", "order" or "#", not after "mã", so it saw an email and no order number. Safe (nothing was revealed, nothing was looked up) but not the labelled behaviour. Not fixed: a parser change after seeing dev would be tuning; the gap is recorded for the work before the final evaluation. *Update: fixed after the v2 decision as a separate bug fix (A21, section 12).*

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
uv run python -m eval.analyze_phase6 --answer-prompt answer_v2   # the same for v2, plus the before/after of A21
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

**Outcome of the v2 run.** `uv run python -m eval.run_phase6_eval --live --answer-prompt answer_v2`, git commit `3af7dc6`, clean tree, 2026-10-10; official result `eval/results/phase6/answer_v2.json` (`deterministic_sha256` `9db0ce6cd35b…`), replay `answer_v2.analysis.json`. 45 live requests (the 45 answers; the 71 router and 24 extractor answers and the case messages came from the cache, 126 cache hits). Prompt `answer_v2` `def7398ad82b…`. Everything else is as in the table at the top.

| | v1 | v2 |
|---|---|---|
| product_search correct | 24 of 24 | 24 of 24 |
| policy_question correct | 13 of 14 | 13 of 14 |
| order_status correct | 11 of 12 | 11 of 12 |
| create_ticket / out_of_scope / smalltalk correct | 7/7, 7/7, 7/7 | 7/7, 7/7, 7/7 |
| dev items correct | 69 of 71 | 69 of 71 |
| multi-turn cases passed | 13 of 13 | 13 of 13 |
| **fallbacks, product_search** (of answers) | **4 of 21** (19.0%) | **0 of 21** (0.0%; 95% Wilson 0.0–15.5%) |
| **fallbacks, policy_question** | 2 of 14 (14.3%) | 2 of 14 (14.3%; 4.0–39.9%) |
| **fallbacks, order_status** | 0 of 4 | 0 of 4 |
| fallbacks, all dev answers | 6 of 39 (15.4%) | 2 of 39 (**5.1%**; 1.4–16.9%) |
| fallbacks, the 6 case answers | 1 (mt-01) | 0 |
| fallbacks, dev items and cases together | 7 of 45 (15.6%) | **2 of 45 (4.4%**; 1.2–14.8%) |
| dev items with a `stock_mismatch` fallback (the targeted error type) | **4** (gs-002, gs-009, gs-015, gs-112) + mt-01 | **0** + 0 |
| answer calls: input / output tokens (39 dev first turns) | 58,773 / 4,469 | 70,980 / 4,709 (+20.8% input) |
| theoretical paid-tier cost per 1,000 conversations | 0.918 USD | 0.978 USD (+6.5%) |
| latency per turn p50 / p95 (measurement) | 1.65 s / 4.75 s | 1.53 s / 4.87 s |
| bounds | all held | all held (same largest values) |

**Adoption rule (A17, applied exactly as written).** (1) No intent has a lower number of correct items: equal in all six. (2) Cases passed: 13 and 13, not fewer. (3) Items with a `stock_mismatch` fallback: 0, strictly fewer than 4. **All three hold, so `answer_v2` is adopted** (A21). The version limit (2) is reached: there is no v3.

What the v2 run does and does not show:
- The effect is the one that was targeted: every product is now written in a sentence of its own with its price and stock status, and the sentence-level checks no longer see an ambiguous sentence. The five product answers that had been replaced in v1 are now written by the model and are right (prices and stock statuses read against the tool results).
- It is **not** a gain in accuracy: the number of correct items is the same, so the benefit is that more of the correct answers are the model's own text. Of the 69 correct items, 37 are credited to model text (v1: 33), 1 to a fallback answer (v1: 5) and 31 to fixed replies.
- The two remaining fallbacks are the same two as before and both are false alarms (section 13): gs-025 (honest "no information") and gs-027 ("Điều 3" quoted from inside clause 6).
- **Claim check on v2** (the known gap of A18.4: sizes, colours, materials, days). The detector reports 3 sentences (gs-105, gs-112, mt-01: "bên mình không có sản phẩm màu nâu / mẫu kính màu đỏ / áo sơ mi nam màu trắng, nhưng ..."); read by hand, each one **denies** the colour that was not found (the search was relaxed on colour), so none states a colour the tool results do not support. Every product answer was read against its evidence (price, stock, size, colour, material), and every figure with "ngày", "giờ" or "tháng" of the policy and order answers is in the clause texts (7 ngày, 2–5 ngày, 1–2 ngày, 7–14 ngày, 24 giờ, 3 ngày, 14 giờ, 12 tháng); the order answers name the status in the fixed words, the order number, the lines and the tracking number of the order only. No unsupported claim was found. The same limits as in section 3 apply (read by the author, one model, temperature 0).
- Cost of adopting it: +20.8% input tokens on the answer calls.

## 10. Limits to keep in mind

- Dev only, 71 items, one run, one model at temperature 0 (cached): the intervals above are wide. Nothing here is a test result.
- The scoring checks that the reply names a gold SKU or cites the gold clause, or that the behaviour matches the label. It does not judge fluency, helpfulness or whether a long answer buries the point.
- The retrieval was not changed (out of scope): the one retrieval-limited failure (gs-025: the gold clause is not in the top 3) is Phase 4 behaviour.
- Price and stock come from the index snapshot, not from live Saleor.
- Orders: the real Saleor with the 30 seeded orders (synthetic data); the failed-verification counter is per conversation (a new conversation resets it, A7).
- Everything is synthetic data on the Gemini free tier, whose inputs may be used by Google (docs/DECISIONS-phase5.md, R27): no real customer data went through the key.

## 11. Disclosure: exposure of one test-split question (A0)

During the Phase 6 survey one test-split question text (no label, no id) was printed by a command run by the author. Its SHA-256, `3b9975a677521f1dda7bac87ffa7098bc3d748f5724545ee185947a1e86d8692`, was recorded from the author's context without reopening the golden file. **The Phase 8 report will give the test score on all test items and on all test items excluding the one whose question hash matches this value.** This run read golden items only through `load_split("dev")` (`tests/test_golden_access.py` fails otherwise) and the result file records `golden_items_read: dev only`.

## 12. After the A17 decision: the bug-fix change (A21), before and after

A separate change, requested after the v2 decision, **in code and not in the prompt** (nothing in `prompts/`, the scoring, the retrieval or the cache changed). It has three parts (details and the choices taken without asking: `docs/DECISIONS-phase6.md`, A21):

1. **A fallback never asserts more than the text it replaces.** It lists only the products the rejected text named and quotes only the clauses it cited (at most 2); with none, it is a fixed reply that says nothing about any product or clause.
2. **An explicit "no information" policy answer needs no citation.** It is sent as the fixed `NO_POLICY_INFO` reply and counted as a **decline**, not as a fallback; every other rule still applies to it. A test reproduces gs-025 (`tests/test_agent_answer.py`, with the item loaded through `load_split("dev")`, the three clauses that were retrieved and the real model texts of v1 and v2) and fails on the code before the change.
3. **The order-number parser reads "mã 88"** ("mã số 88", "mã đặt hàng 88", ...), and not "88k", "88.000đ", sizes, "mã giảm giá 88", "mã vận đơn 88" (two older defects of the parser found by these tests are fixed too).

**How the numbers below were made.** The official runner refuses a dirty tree, so the after-numbers are the **offline replay of the cache with the code as it is now** (`uv run python -m eval.analyze_phase6 --answer-prompt answer_v2`, **0 live calls**: the analysis asserts it and the quota counter did not move, 135 of 450). The before-numbers are the committed official result `answer_v2.json` (code of commit `3af7dc6`). The replay differs from the official result on exactly two items, listed below; every other reply, fallback flag and score is identical. The analysis was first run with tracked files modified; it was **re-run on the clean tree after the commit** (`c75b566`, `provenance.tracked_files_modified: false`, 0 live calls): the only change in the file is that provenance, and its content hash `analysis_sha256` (`1f9364b0d196…`) is the same.

| Intent | Items | Correct before → after | Answers written by the model | Fallbacks before → after | Declines after | **Fallback rate** before → after |
|---|---|---|---|---|---|---|
| product_search | 24 | 24 → 24 | 21 | 0 → 0 | 0 | **0.0% → 0.0%** |
| policy_question | 14 | 13 → 13 | 14 | 2 → 1 | 1 | **14.3% → 7.1%** (95% Wilson 1.3–31.5%) |
| order_status | 12 | 11 → **12** | 4 | 0 → 0 | 0 | **0.0% → 0.0%** |
| create_ticket | 7 | 7 → 7 | 0 | n/a | 0 | n/a (no model answer) |
| out_of_scope | 7 | 7 → 7 | 0 | n/a | 0 | n/a |
| smalltalk | 7 | 7 → 7 | 0 | n/a | 0 | n/a |
| **all dev items** | **71** | **69 → 70** (0.972 → 0.986; 0.924–0.998) | 39 | **2 → 1** | **1** | **5.1% → 2.6%** (0.5–13.2%) |
| scripted multi-turn answers | 13 cases | 13 → 13 | 6 | 0 → 0 | 0 | 0 of 6 |
| dev items and cases together | | | 45 | 2 → 1 | 1 | 4.4% → **2.2%** (0.4–11.6%) |

Correct items by source, all dev items (model text / fallback answer / declined reply / fixed reply): 37 / 1 / 0 / 31 before, 37 / 1 / 0 / 32 after.

The runner and the analysis now report declines next to fallbacks (`declined` per item, `declines` and `items_with_decline` in the `guardrails` block of a new official result); the committed `answer_v1.json` and `answer_v2.json` predate it and are not rewritten.

**The two items whose answer changed:**
- **gs-025** (policy, "đơn trên 500k có free ship ko shop?"): before, a fallback that printed clause doi-tra 2 (the return period), a clause the model had not used; after, the fixed reply "Mình chưa có thông tin về nội dung này trong chính sách của cửa hàng ... liên hệ nhân viên", not a fallback and no clause quoted. **Still incorrect** (the gold clause van-chuyen 2 was never retrieved: retrieval, not answering). What changed is what the customer is told: an honest "I have no information" instead of an irrelevant quote that looked like an answer. The same holds for v1's text.
- **gs-124** (order, "... mã 88, email là khachhang10@example.com", label `refuse`): before, the agent asked for the order number again (correct behaviour in safety terms, wrong against the label); after, the parser reads "mã 88", the email is not the owner's, the lookup fails and the agent refuses: **now correct**.

**What not to read into it.** The 69 → 70 is the parser gap (section 7) fixed with that dev item in hand: it is **not an independent estimate** of the agent's accuracy, and the same is true of gs-025's new reply. Whether the fixes generalise is for the test split to say (Phase 8). The fallback rate fell from 5.1% to 2.6% only because one false alarm stopped being a fallback; the other (gs-027) is still a fallback.

**A check on real texts of the restriction of product fallbacks.** v2 has no product fallback left, so the restricted product fallback was also replayed on v1, whose four product fallbacks are real texts (scratch directory, offline, 0 live calls, removed afterwards): all four replies are unchanged, because in each the model had named the first three products of the evidence, which is what the old fallback printed. The only differences from the v1 official result are the same two items, gs-025 and gs-124.

**The official result for the new code** (a clean tree, after the commit `c75b566`, offline: 0 live calls, 171 cache hits, quota counter unchanged at 135 of 450): `uv run python -m eval.run_phase6_eval --answer-prompt answer_v2 --results-dir eval/results/phase6_after_a21 --price-input-per-million 0.3 --price-output-per-million 2.5 --price-source "<the source line of the v2 run>"` writes `eval/results/phase6_after_a21/answer_v2.json` (`deterministic_sha256` `ca0c2a48c296…`, git commit `c75b566`, clean tree). `eval/results/phase6/answer_v2.json` stays as the record of the code of `3af7dc6`. Against it, the new file differs in exactly the two items of this section (gs-025 and gs-124) and in the new `declined` / `declines` fields; the 13 case records, the LLM calls and tokens, the steps, the bounds (all held, same largest values) and the theoretical cost (0.978 USD per 1,000) are identical, and the provenance differs only in the commit. Its per-intent correct, fallback and decline counts equal the replay of the analysis above. The first attempt, made without the price arguments, lacked the cost block and was overwritten by this one.

## 13. What the guardrails have, and have not, been shown to do

Stated plainly, so that the fallback numbers are not read as protection:

- **On dev, every guardrail trigger was a false positive.** In the 90 model answers of the two runs (45 of v1, 45 of v2), the guardrails rejected 9 (7 in v1, 2 in v2, the same two as in v1), and **none of the 9 contained a fabricated fact**. Read by hand against the tool results:
  - gs-002, gs-015, gs-112 and the case mt-01 (v1): `stock_mismatch`. One sentence listed several products and gave a correct stock status after each; the rule rejects such a sentence as ambiguous (A18.4). The text was right.
  - gs-009 (v1): `price_mismatch` and `stock_mismatch`. The customer's budget ("dưới 500k") was repeated in the sentence that named SKUs, which the owner's price rule rejects as written; every price and stock status was right.
  - gs-027 (v1 and v2): `unknown_clause`. The model quoted "đạt điều kiện tại Điều 3", a cross-reference written inside clause 6, which the guard (it knows only the clause numbers of the hits) took for an invented clause. The text was right. **Still a fallback after A21.**
  - gs-025 (v1 and v2): `no_clause_cited`. The model honestly said it had no information, which is what the three retrieved clauses supported; the citation rule rejected it. **No longer a fallback after A21** (it is a decline).
- **Recall on real hallucinations is unmeasured**, because the model produced none: no answer of the 90 (the 12 case answers included) was found to state a wrong price, stock status, order status, order number or clause, whether it was rejected or accepted (every answer was read by the author against its tool results; the claim heuristic covers only sizes, colours, materials and days). With zero positives there is no recall to estimate, and the dev run says nothing about how often the model would lie, or how many of its lies the checks would catch.
- **Its protection is so far demonstrated only by the attack tests**: hand-made fabricated answers fed to the checks and to the whole agent (the "áo này 99k đúng không shop?" family and the other cases of `tests/test_agent_guardrails.py` and `tests/test_agent_answer.py`), and mutations of the checks (44 before A21; 25 more on the code of A21: one survived at first, a test was added for it, and all 25 are now killed). The gs-025 test was also run against the behaviour before A21 and fails there with the irrelevant quote of doi-tra Điều 2. A test shows that a check rejects the lie it was written for; it does not show that the lies a model really tells are the ones the tests imagine.
- What the dev run does measure is the **cost**: v1 replaced 7 of 45 answers (15.6%) for nothing, v2 replaced 2 (4.4%), and after A21 1 (2.2%), all of them false alarms. The fallback rate is therefore mostly a measure of how often the conservative rules misfire, not of harm prevented.
- Still not checked by any guardrail (A18.4): sizes, colours, materials and numbers of days in an answer. The claim heuristic found no unsupported one in the answers it read (sections 3 and 9); that is a report, not a protection.
