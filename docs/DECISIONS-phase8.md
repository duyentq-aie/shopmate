# Phase 8 decisions: freezing and pre-registering the final evaluation (2026-10-12)

Turn 1 of Phase 8. The test split was **not** run and was not consulted: nothing in this turn opened it (`eval/final_evaluation.py` can, only under the guards of D8.3, and was run on dev only).
What is registered in advance is in `docs/PHASE8-PREREG.md`; this file says what was done to get there and why.

### D8.1 The GitHub actions are pinned by full commit SHA

- **Context.** D7.23 pinned the three actions to a major tag, because the workflow had never run, and said to pin by SHA once the commits were re-checked ("a tag can move"). A pre-registration that
  freezes the code freezes what runs it, so the pins are made now.
- **Checked again, through the public GitHub API on 2026-10-12** (`git/ref/tags/<tag>` and `commits/<tag>`; a commit is the object a tag must resolve to, and `setup-uv@v7` is an annotated tag, whose tag object
  `94527f2e458b27549849d47d273a16bec83a01e9` was followed to its commit). The three commits are **the same as the ones D7.23 recorded on 2026-10-11** (nothing moved), and each answers `commits/<sha>` with HTTP 200 in its own repository:
  `actions/checkout@v7` = `3d3c42e5aac5ba805825da76410c181273ba90b1` (committed 2026-07-17), `astral-sh/setup-uv@v7` = `37802adc94f370d6bfd71619e3f0bf239e1f3b78` (2026-03-16),
  `gitleaks/gitleaks-action@v3` = `e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e` (2026-05-30).
- **Choice.** `.github/workflows/ci.yml` has `uses: <action>@<40 hex> # <tag>` in all six places (the comment is the tag the SHA was resolved from, so a bump is a deliberate act). `tests/test_ci.py` now requires
  a 40-hex SHA with that comment, the exact three pins above, and that `docs/DECISIONS-phase8.md` names them (this paragraph); a tag, a branch or a short SHA fails.
- **Not done:** the workflow has still never run on GitHub (it was never pushed in this turn); the first run will show whether a pinned commit behaves like the tag did. Dependabot can bump the pins.
- **Undo.** Put the tags back (`@v7`, `@v7`, `@v3`) and relax the two tests; nothing else depends on it.

### D8.2 The official dev run on the current HEAD, made again, from the cache

- **Command.** `uv run python -m eval.run_phase6_eval --answer-prompt answer_v2 --results-dir eval/results/phase8_dev --price-input-per-million 0.30 --price-output-per-million 2.50 --price-source "<the Phase 6 string>"`:
  the official runner (not a replay: it builds the agent, the retriever and the read-only Saleor client, and refuses a dirty tree). Offline first, as ordered: **0 live Gemini requests, 171 answers from `eval/llm_cache/`, nothing missing.**
- **Where.** HEAD `c9cfdc820e17c59a7df169058f48ac39d6de075c`. The runner refuses a working tree that differs from HEAD, and this turn had two uncommitted edits of its own (the SHA pins of D8.1). They were taken out for the run by
  reverse-applying a patch saved beforehand, and put back afterwards (the saved patch and the working-tree diff are byte-identical): the file records `git_commit` `c9cfdc82…` and `git_dirty: false`, which is true.
- **Result file.** `eval/results/phase8_dev/answer_v2.json`, `deterministic_sha256` `85e54e592895df2914ce52c790c006e9e273a056cad2c51dea6bb15a14310a73`.
- **Against the numbers reported so far (`eval/results/phase6_after_a21/answer_v2.json`, code of `c75b566`): the same, except for what is listed.**

| | committed (A21) | this run |
|---|---|---|
| dev items correct | 70 / 71 | **70 / 71** (the one miss is gs-025: the reply does not cite van-chuyen Điều 2) |
| product_search / policy / order / ticket / out_of_scope / smalltalk | 24/24, 13/14, 12/12, 7/7, 7/7, 7/7 | identical |
| product `no_answer` | 5 / 5 | **5 / 5** |
| multi-turn cases | 13 / 13 | **13 / 13**, every case identical |
| guardrails | 45 answers, 1 fallback (gs-027), 1 decline (gs-025) | identical |
| calls, tokens, bounds, steps, theoretical cost per 1,000 | 0.97847 USD | identical (0.97847 USD) |
| `deterministic_sha256` | `ca0c2a48…` | `85e54e59…`: differs, for the two reasons below |

  1. **`provenance.git_commit`**: `c75b566…` -> `c9cfdc8…` (the only provenance field that differs: prompts, schemas, vocabulary, index manifest, dev-split and cases hashes are the same).
  2. **Three items changed their `reply` and `awaiting`, all three `no_answer` items, all three still correct (the scoring needs the fixed "no match" sentence and no SKU, D7.28):** since Phase 7.5 an empty product search
     ends with an offer the customer must accept instead of "nothing found". gs-021: "…Điều kiện đang chặn kết quả: màu đỏ. Hiện có 2 áo len nữ kích cỡ XXL nếu bỏ điều kiện này — bạn muốn xem không?";
     gs-108: "…chất liệu tweed (cùng với màu kem). Hiện có 7 áo khoác nữ…"; gs-110: "…chất liệu vải canvas (cùng với kích cỡ Freesize và màu vàng). Hiện có 7 mũ mức giá từ 100.000đ đến 250.000đ…".
     `awaiting` is `offer_confirmation` instead of `null`. No router, extractor or answer call differs.
  - The measured (non-deterministic) latency p95 over the 102 turns of the run is 4805 ms against 4779 ms: retrieval time measured now.
- **Why the dev run is at `c9cfdc8` and not at the freeze commit.** A result cannot be committed in the commit it was obtained at. What the pre-registration pins instead is the **code**: `eval/final_evaluation.py` refuses to open
  the test split unless nothing that can change a result (`app/`, `prompts/`, `data/index`, `data/seed`, `data/policies`, `eval/` except its results, its cache and `final_evaluation.py`, `pyproject.toml`, `uv.lock`) differs between this
  commit and HEAD (D8.3). The freeze commit changes none of them.
- **Undo.** Delete `eval/results/phase8_dev/`.

### D8.3 `eval/final_evaluation.py`: the one command that measures, and its rehearsal on dev

There was no script for "the final evaluation": the flag `load_split("test", final_evaluation=True)` existed and nothing used it. This turn wrote it, and ran it on **dev** (`--rehearsal`). Three of the choices first made here were
changed by the owner before the freeze (D8.4): `answer_correct` is a primary metric, the interval of a proportion is Wilson, and the freeze is a tag. The text below is the script as it stands after D8.4.

- **What it measures** (definitions in `docs/PHASE8-PREREG.md`): router accuracy, extractor exact match, product MRR, policy MRR, no_answer, guardrail fallback rate, p50 and p95 latency, cost per 1,000 conversations, every one
  with a 95% interval, for **all items** and for **all items except the one whose question hash is A0's**: Wilson for the proportions (router accuracy, extractor exact match, no_answer, answer_correct, fallback rate), percentile bootstrap
  (10,000 resamples, seed 20261009, the settings of Phases 3B to 6) for the MRRs, the latency percentiles and the cost. It reuses the two evaluations that already exist
  (`evaluate_phase5` for the router, the extractor and the full retrieval pipeline; `evaluate_phase6` for the agent, the guardrail, tokens and latency) on one set of cached calls, so the numbers are the earlier ones by construction;
  two cross-checks in the output (the answer score and the cost agree with the Phase 6 section) say when the script, not the system, is wrong.
- **Choices taken without asking (the first two were reversed by the owner, D8.4; the rest stand):**
  1. *`answer_correct` was added as a ninth metric, marked as an addition that could be struck out.* **Now a primary metric** (D8.4).
  2. *A Wilson interval was printed beside the bootstrap interval of every yes/no metric.* **Now the registered interval of a proportion, with no bootstrap number beside it** (D8.4). The reason stands: a percentile bootstrap collapses to a point
     when every item succeeded (71 of 71 gives [1.000, 1.000]; 33 of 33 would too); the Wilson interval for 33 of 33 is [0.896, 1.000].
  3. *Latency is that of the first turn of each item* (recorded LLM latencies plus retrieval, lookup and reply time), n = the number of items; the Phase 6 figure (p50 1.5 s, p95 4.9 s) is over 102 turns including the multi-turn cases, so the two are not comparable.
  4. *The scripted multi-turn cases are not part of it*: they are not golden items.
  5. *No pass/fail anywhere.* The only descriptive rule is in the PREREG ("a large dev-test gap").
- **Guards of the test split** (all in code, all tested without opening the split): `--split test` exists only here; a clean tree; no result file yet (no overwrite flag for the test split); at most three attempts in
  `eval/results/phase8_test/attempts.jsonl` and none after a failure that is not an infrastructure error; `--freeze-ref v1.0-frozen` (the tag exists and HEAD is its commit, or descends from it with no frozen file different: D8.4) and
  `--dev-run-sha` (no result-relevant file differs). The excluded item is matched by hashing every `question` the way A0 did (UTF-8, placeholder literal, no normalisation); **its id is printed nowhere and stored in no summary**;
  if nothing matches, the report says so and the second figure equals the first. A test (`tests/test_final_evaluation.py`) fails if any other module passes `final_evaluation=` to the loader.
- **The rehearsal** (`eval/results/phase8_rehearsal/`, `--split dev --rehearsal --rehearse-exclusion`; the first dev item stands in for the excluded one): ran end to end from the cache, **0 live requests** (231 cache hits), wrote
  `final_evaluation.json`, `final_evaluation.md` and `attempts.jsonl`, both figures, all intervals and, since D8.4, the by-author and by-intent tables; the two cross-checks hold. It ran on HEAD `c9cfdc8` **with the working tree not clean**
  (this script, its tests, the CI pins), recorded in the file as `git_dirty: true` and the list of paths; its output says "REHEARSAL: not a result" and is not the dev reference of D8.2. It was run again after D8.4 (the files in the folder are those of the second run).
- **What a 33-item test run costs in Gemini requests.** The composition of the test split is documented (`docs/SETUP.md`: 10 product_search, 6 policy_question, 6 order_status, 4 create_ticket, 3 out_of_scope, 4 smalltalk); the cost per intent is the
  mean of the dev run of D8.2 (every call of a first turn): product_search 2.88, policy 2.00, order_status 1.33, ticket 1.00, out_of_scope 1.00, smalltalk 1.00. **Estimate: 60 requests** (59.8); at the largest per-item count seen on dev for each intent, 65; the
  absolute ceiling of the design, 3 per item, is 99 (the cap `--max-live-calls` defaults to 100). The router and extractor calls of the Phase 5 half and of the agent half are the same requests, so the cache makes them one.
- **Undo.** Delete `eval/final_evaluation.py` and `tests/test_final_evaluation.py`; nothing else imports them.

### D8.4 The owner's changes before the freeze: primary metric, interval, tag, secondary analysis

Made on the owner's instructions after reading the first version of the pre-registration; no Gemini request, the test split not run, nothing committed.

1. **`answer_correct` stays in the list of primary metrics** (it is the same score as every dev report: 70/71). The words "an addition" and "can be struck out" are gone from `docs/PHASE8-PREREG.md`.
2. **The registered interval of a proportion is Wilson 95%** (z = 1.959964) for `router_accuracy`, `extractor_exact_match`, `no_answer_correct`, `answer_correct` and `guardrail_fallback_rate`. **The percentile bootstrap** (95%, 10,000 resamples, seed 20261009)
   is kept for `product_mrr`, `policy_mrr`, `product_mrr_gold_routing`, the latency p50 and p95, and the cost per 1,000. `eval/final_evaluation.py` carries the kind of each interval in the output (`"interval": "wilson"` or `"bootstrap"`), prints no bootstrap number for a
   proportion, and `tests/test_final_evaluation.py` checks the map from metric to interval kind, textbook Wilson values (for example 33 of 33 is [0.896, 1.000]) and that a proportion never gets a bootstrap interval. The Wilson bounds are rounded to 12 decimals
   (a bound of 0.9999999999999999 is a rounding artefact, not a number to report). The rule of the "large dev-test gap" uses the registered interval of the metric (the dev point estimate outside it); the old clause about a single point is gone.
3. **The freeze is the tag `v1.0-frozen`**, not a SHA written into the file by a second commit. `eval/final_evaluation.py` takes `--freeze-ref v1.0-frozen` (any other name is refused: not a branch, not a SHA), resolves `refs/tags/v1.0-frozen^{commit}` (an annotated tag is followed to its commit),
   and refuses unless **HEAD is that commit, or descends from it with no difference in a frozen path**. The frozen paths are the existing list (`app/`, `prompts/`, `data/index`, `data/seed`, `data/policies`, `eval/` except `eval/results/` and `eval/llm_cache/`, `pyproject.toml`, `uv.lock`)
   **plus `docs/PHASE8-PREREG.md`** (the registration itself must not change between the tag and the run). Note that this is stricter than the first version in one respect and different in another: `eval/final_evaluation.py` is now frozen as well (it is under `eval/`; the dev-run
   check still excludes it, because it is newer than the dev run by construction), and the notes `docs/PHASE8-COMMITS.md` and the other documents may change after the tag, because they do not affect a result. The sequence is in `docs/PHASE8-COMMITS.md`: freeze commit, push, CI green, tag, push the tag. There is no second commit,
   and no placeholder for a SHA in the pre-registration. **The tag has to be made on the commit whose CI was green**, and must not be moved afterwards: the script cannot tell a moved tag from a first one, so a tag that is deleted and made again is a deviation to report. Because the pre-registration is itself frozen, a deviation is recorded in this file (a new decision) and in the write-up of the results, never by editing `docs/PHASE8-PREREG.md` after the tag
   (the script would refuse to run). If that is not wanted, take `docs/PHASE8-PREREG.md` out of `FROZEN_PATHS` in `eval/final_evaluation.py` and the sentence in the PREREG that says so.
4. **A registered secondary analysis**: the results on the test split by author (`human` / `agent_draft`) and by intent, descriptive only, with Wilson 95% for the proportions and a mean (with n, no interval) for the two MRRs. It is **for all items only**, not for the second figure: the difference between
   the two tables would show to which author and intent the excluded item belongs, and nobody looks that up (A0). Groups with fewer than 8 items (`MIN_INTERPRETABLE_N`, the threshold of Phase 3B) are marked `(small)` in the table, and the registered text says that no difference between groups is tested or claimed.
   The test split's split by author (20 `agent_draft`, 13 `human`) is **a subtraction** (64 items in all, 44 of them in dev), not a count: the split was not read. The rehearsal on dev prints both tables (27 `human` / 44 `agent_draft`; by intent 24 / 14 / 12 / 7 / 7 / 7).
5. **Not changed:** the two figures (all items; without the A0 item), the absence of pass/fail thresholds, the guards (clean tree, no overwrite, at most three attempts, none after a failure that is not infrastructure), the quota estimate (60 requests, ceiling 99), the planned day (2026-10-13 Pacific).
- **Undo.** Items 1 to 4 are separate changes in the script and its tests; the PREREG states each. Putting the SHA back instead of the tag means undoing item 3 in `frozen_problems` and `main`.

### D8.5 The state outside the repository is fingerprinted and checked before the test split is opened

- **Context.** The freeze tag pins files. It cannot pin what is in a running service: a re-index (or a manual edit) of the Qdrant collection, or a seed order that is cancelled, fulfilled or edited in Saleor (the runtime token has MANAGE_ORDERS; read-only is enforced by the client, D7), would change what the test run measures
  under an unchanged commit. Added before the freeze, on the owner's instruction; no Gemini request, the test split not run, nothing committed, nothing written to Qdrant or Saleor.
- **What.** `eval/external_state.py` (tests: `tests/test_external_state.py`, fake clients only).
  - **Qdrant `shopmate_vn_v2`:** point count (exact), vector layout (dense size and distance, sparse modifiers: the form of the manifest), and `payloads_sha256`, the sha256 of the lines `<point id> <sha256 of the canonical JSON of the payload>`, sorted by id. Obtained by `get_collection`, `count` and `scroll` without vectors; nothing else is called (a test reads the source for that).
  - **Saleor seed orders #84 to #113:** one allowlisted `OrderByNumber` query each through `ReadOnlySaleorClient` with the runtime token (read by `require_setting`, never printed). Per order: number, status, total, number of lines, sha256 of the lower-cased customer email; `rows_sha256` is the sha256 of the 30 lines sorted by number.
    **The raw email is dropped inside `order_row`**: it is in no fingerprint, message, file or test output (tests check this).
- **Values, read 2026-10-12** (registered in `docs/PHASE8-PREREG.md` section 1, rows "the index" and "the order data", and in `REGISTERED_STATE`; a test makes the two agree):
  `points` 269, `payloads_sha256` `f2a7b8c2b7bd9021a526e2900f176cc6c4a7ec7472c10bf83334d55706cd292b`; `found` 30, `rows_sha256` `33df74d4c896aa0c999e36a8b6f95ddcf74b429ce0f49363513cb5a7a25f4d18`.
  **Against the manifest:** 269 points = `documents.total`, 250 product + 19 policy, collection and vector layout equal (no difference, so the work did not stop). Two reads in a row gave identical values. The 30 orders equal field by field the rows that `data/seed/orders.vi.json` implies.
- **The guard** (`eval/final_evaluation.py`, `--split test` only): after the git guards and the attempts, **before** `OPENING THE TEST SPLIT` and before `load_split`, it recomputes both fingerprints (read only) and compares them with `REGISTERED_STATE`. If one differs, or a service cannot be read, it refuses (exit 1), prints the registered and the current value of each part
  that differs (for the orders also which order differs from the seed file and in which field, never a value of an email) and appends a `kind: "refusal"` entry (`guard`, `ended: external_state_differs`, `opened_test_split: false`, `differences`) to `attempts.jsonl`. PREREG section 9 lists it.
- **Choices taken without asking (change them before the freeze if wrong):**
  1. *A refusal is not an attempt.* It opened nothing, so `attempts_problems` counts only `kind: "run"` entries (and judges the last run); five refusals do not use up the three attempts and do not block the next run. A service that is down is a refusal too, not a pass.
  2. *A separate module, frozen with the code.* `eval/` is a frozen path, so the module cannot change between the tag and the run; like `final_evaluation.py` it is excluded from the dev-run comparison because it is newer than the dev run and cannot change a result.
  3. *The email is hashed with plain sha256 of the lower-cased, trimmed address.* The addresses are fictional (`@example.com`) and guessable: the hash is there so that no address is written anywhere and a change is detected, not as protection.
  4. *The registered values are constants in code and text in PREREG.* The code is frozen by the tag as the PREREG is; a test fails if they disagree, and a third test compares them with the manifest.
  5. *The dev rehearsal does not use the guard* (the dev split needs none); it is run again at the end of this turn only so that its recorded dirty-file list is that of the final tree.
- **Limits** (also in PREREG section 10): the Qdrant vectors are not read (the manifest digest and the Phase 6 runner's check cover them); orders outside #84 to #113 are not registered (the agent looks up whatever number a question names, and a number that does not exist should stay absent); the Saleor catalogue is not read at run time and not
  fingerprinted; the fingerprints were taken after the official dev run of D8.2, so they say what the state is now (the dev run itself checked the index against the manifest and found no difference).
- **Undo.** Delete `eval/external_state.py`, `tests/test_external_state.py`, the call in `main`, the exclusion in `CODE_EXCLUDED` and the two PREREG rows; `attempts_problems` can keep ignoring non-`run` entries.

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

