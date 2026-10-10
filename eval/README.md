# eval/

Evaluation for ShopMate.

- `golden/` – the golden question set (`golden.vi.json`), its schema, the script that wrote the agent drafts, the validator and the order-number sync. See `docs/SETUP.md`, "Golden set". Evaluation code reads it only through `golden/loader.py`; the test split stays locked.
- `run_retrieval_eval.py`, `retrieval_metrics.py` – the retrieval runner (dev split only; refuses a dirty working tree and an index that does not match `data/index/manifest.json`) and its metrics and bootstrap statistics. Modes: `dense`, `hybrid` (Phase 3B) and `dense_sparse_folded`, `dense_folded` (Phase 4). `compare_retrieval_runs.py` compares the Phase 3B runs.
- `perturbation/` – `dev_folded.json`, the dev retrieval items with the question machine-folded (Phase 4 experiment 1), and the code that builds and verifies it (`python -m eval.perturbation.dev_folded [--check]`).
- `llm_metrics.py`, `run_phase5_eval.py` – Phase 5: routing and filter-extraction metrics, and the runner (LLM router + filter extractor measured on dev; offline by default, `--live` to fetch missing answers). `llm_cache/` holds the committed raw LLM answers (a re-run needs no API key); `probes/` the hand-written adversarial probes (outside the golden set, gated by human review).
- `exp1_rule.py` – the pre-registered decision rule of Phase 4 experiment 1, as code. `compare_exp1.py` applies it to the result files and adds the paired tables, the reproduction check and the co_dau analysis.
- `results/` – committed result files (`eval/results/*.json` Phase 3B, `eval/results/phase4-exp1/` Phase 4).

Decisions and results: `docs/DECISIONS-phase4.md`, `docs/RESULTS-phase3b.md`.
