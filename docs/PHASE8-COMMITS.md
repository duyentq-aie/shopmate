# Phase 8, turn 1: the commit and the tag to make

Nothing was committed, staged, pushed or tagged. One commit, "freeze": it holds the pre-registration, the tool that measures, the official dev run, the rehearsal and the pinned CI. Then the tag `v1.0-frozen`, made **after the push and a green CI**.
**The freeze is the tag.** There is no second commit and no SHA written into any file. **The test split was not run and not consulted** (`docs/PHASE8-PREREG.md`).

## 1. The freeze commit

```bash
git status --porcelain      # exactly the files of the table below, nothing else
uv run pytest -q            # green before the commit (1690 passed, 49 deselected)
git add .github/workflows/ci.yml tests/test_ci.py
git add eval/final_evaluation.py tests/test_final_evaluation.py eval/external_state.py tests/test_external_state.py
git add docs/PHASE8-PREREG.md docs/DECISIONS-phase8.md docs/PHASE8-COMMITS.md
git add eval/results/phase8_dev/answer_v2.json
git add eval/results/phase8_rehearsal/final_evaluation.json eval/results/phase8_rehearsal/final_evaluation.md eval/results/phase8_rehearsal/attempts.jsonl
git commit -m "chore(phase8): freeze: pre-registration, final_evaluation script, official dev run, SHA-pinned CI" \
  -m "docs/PHASE8-PREREG.md registers the metrics (Wilson 95% for the proportions; percentile bootstrap, 10,000 resamples, seed 20261009, for the MRRs, latency and cost), the two figures (all items and without the A0 item), the secondary analysis by author and intent, the fingerprints of the Qdrant collection and the seed orders, the command, the quota, the rerun policy and the disclosures. eval/final_evaluation.py is rehearsed on dev only; the official dev run at c9cfdc8 (70/71, 13/13, 5/5) is in eval/results/phase8_dev. The three actions are pinned by commit SHA."
```

## 2. Push, wait for CI, then the tag (the stale tag is replaced: D8.6)

A tag `v1.0-frozen` already exists, locally and on `origin`, made by mistake on `c9cfdc8` before the freeze commit (tag object `d11c58a3…`; D8.6). Nothing was run on the test split, so it is deleted and made again, once, on the freeze commit; D8.6 is in the freeze commit and records this.

```bash
git push origin main
# wait for the CI run of this commit on GitHub: it must be GREEN. This is the first time the workflow runs with the pinned SHAs (D8.1);
# if it is red, fix it in a new commit, push, wait again: the tag goes on a commit that is green, and only then.
git rev-parse HEAD                                   # note it: the commit that was green
git tag -d v1.0-frozen                               # the stale tag (it points to c9cfdc8), local ...
git push origin --delete v1.0-frozen                 # ... and on origin
git tag -a v1.0-frozen -m "Phase 8 freeze: pre-registration, code and CI as registered in docs/PHASE8-PREREG.md"
git rev-parse v1.0-frozen^{commit}                   # must print the same 40 characters as HEAD above
git push origin v1.0-frozen
```

- The tag name is exactly `v1.0-frozen`; the script accepts no other name, no branch and no bare SHA.
- **Do not move or re-make the tag again** once this one is pushed. The script cannot tell a moved tag from a first one, so doing it would be a deviation to report in `docs/DECISIONS-phase8.md` (the pre-registration is frozen and is not edited). The replacement above is the one exception, recorded in D8.6.
- After the tag, commits that touch only documents other than `docs/PHASE8-PREREG.md`, tests, CI, or `eval/results/` and `eval/llm_cache/` are allowed: the script checks that `app/`, `prompts/`, `data/index`, `data/seed`, `data/policies`, the rest of `eval/` (this includes
  `eval/final_evaluation.py`), `pyproject.toml`, `uv.lock` and `docs/PHASE8-PREREG.md` are the same at HEAD as at the tag, and refuses to open the test split otherwise.

## 3. Turn 2 (not done now)

```bash
uv run python -m eval.final_evaluation --split test --live --max-live-calls 100 \
    --freeze-ref v1.0-frozen --dev-run-sha c9cfdc820e17c59a7df169058f48ac39d6de075c
```

On a clean tree, **on the registered Pacific day, 2026-10-13** (from 14:00 on 13 October to 14:00 on 14 October, Vietnam time, not in the last hour; the script does not check the date, the person who runs it does; D8.6; `docs/PHASE8-PREREG.md` section 5), with Qdrant and Saleor up and in the registered state: `uv run python -m eval.external_state` (read only, writes nothing) must end with `registered values: equal`; the script checks the same before it opens the test split and refuses otherwise. It writes `eval/results/phase8_test/`.

## What is in the freeze commit

| file | why |
|---|---|
| `.github/workflows/ci.yml`, `tests/test_ci.py` | D8.1: the actions pinned by full commit SHA, with tests |
| `eval/final_evaluation.py`, `tests/test_final_evaluation.py` | D8.3, D8.4, D8.5: the one command that measures, its guards (the tag, the dev-run code check, the external-state check), the by-author and by-intent tables |
| `eval/external_state.py`, `tests/test_external_state.py` | D8.5: read-only fingerprints of the Qdrant collection and of the 30 seed orders in Saleor, registered in PREREG section 1 and checked before the test split is opened |
| `docs/PHASE8-PREREG.md` | the pre-registration: freeze = tag `v1.0-frozen`, Wilson 95% for proportions, `answer_correct` primary, the secondary analysis (the catalogue audit section of the earlier commit is kept as it was) |
| `docs/DECISIONS-phase8.md` | D8.1 pins, D8.2 official dev run against the reported numbers, D8.3 the script, its rehearsal and the quota estimate, D8.4 the owner's changes before the freeze, D8.5 the external-state fingerprints, D8.6 the tag made by mistake and replaced, and the registered day of the run |
| `eval/results/phase8_dev/answer_v2.json` | the official dev run at `c9cfdc8` (`deterministic_sha256` `85e54e59…`) |
| `eval/results/phase8_rehearsal/` | the rehearsal of `final_evaluation.py` on dev (0 live requests): not a result; recorded with `git_dirty: true` |
| `docs/PHASE8-COMMITS.md` | this file |
