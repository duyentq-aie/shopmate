# Phase 3A decisions: retrieval infrastructure (2026-10-09)

Baseline retrieval only. No evaluation was run; the golden set (`eval/golden/golden.vi.json`, `eval/golden/human_items.toml`) was not opened, read or used in this phase. Smoke queries live in `tests/`.

**R1. Embedding model: BAAI/bge-m3 on CPU, revision `5617a9f61b028005a4858fdac845db406aefb181`.**
Survey on this machine (i5-11400H, 6 torch threads, Saleor running, 4.2–4.3 GiB available): load 3–11 s, about 0.7 GiB RSS at rest, peak about 2.2 GiB while encoding, 2.9 docs/s at batch 32 (mean 107 tokens), about 156 ms per query. It fits next to Saleor (about 2.7 GiB) and Qdrant (about 0.1 GiB) with about 1.7 GiB headroom. The weights were already in the HuggingFace cache; only `sparse_linear.pt` was downloaded at the pinned revision.

**R2. torch from PyTorch's CPU index.**
`[[tool.uv.index]] pytorch-cpu` with `explicit = true`, and `torch` sourced from it. No CUDA or triton packages in `uv.lock`. Versions: torch 2.14.1+cpu, transformers 5.19.0, qdrant-client 1.19.1.

**R3. Hand-written bge-m3 inference instead of FlagEmbedding, guarded by a permanent parity test.**
FlagEmbedding would pull in datasets, accelerate and peft. `app/retrieval/embedder.py` reproduces `BGEM3FlagModel.encode`: dense = L2-normalised [CLS]; sparse = relu(sparse_linear(hidden)), with cls/eos/pad/unk dropped, the max weight kept per token id and weights > 0 only (checked against FlagEmbedding 1.4.2's `_process_token_weights`).
- Reference: `tests/fixtures/bge_m3_reference.json`, produced by FlagEmbedding **1.4.2** (torch 2.14.1+cpu, transformers 5.19.0, Python 3.12.15, use_fp16=False, CPU) with `tests/fixtures/generate_bge_m3_reference.py`, run in a separate bench venv (`~/.cache/shopmate-bench`). FlagEmbedding is not a project dependency.
- 7 texts written for the fixture: with diacritics, without diacritics, teencode, a policy clause, a very short one, mixed Vietnamese/English, and an 854-token text that exercises truncation at 512.
- **Tolerances:** dense cosine ≥ 0.9999; sparse: identical token-id sets and every weight within **1e-4 absolute**. Measured: cosine 1.00000000 for all texts, max weight difference 3.6e-7, smallest reference weight 0.0009 (so 1e-4 cannot hide a dropped token).

**R4. Skip key = content hash + embedding fingerprint.**
Each point stores `content_sha256` (text + payload) and `embedding_fingerprint` (sha256 of model id, revision, dense dim, pooling, normalisation, max_length, sparse activation/pooling/special-token handling, dtype). A point is re-embedded when either changes. A model or config change therefore never leaves stale vectors. Tests: changing the revision re-embeds everything; max_length / sparse / pooling settings change the fingerprint. When nothing changed, the model is not even loaded (re-run: 1.6 s, 0.1 GiB).

**R5. One collection `shopmate_vn`, named vectors `dense` (1024, cosine) and `sparse` (no IDF modifier).**
bge-m3 sparse weights are learned term weights, so Qdrant's IDF modifier is not applied. Payload indexes: doc_type, sku, category, color, material, sizes, price_vnd, in_stock, file, clause. Products and policy clauses share the collection; `doc_type` separates them.

**R6. Documents.**
- Products: read through the read-only runtime client's existing `ProductsInChannel` query; no allowlist change was needed. Text = name, category name, màu sắc, chất liệu, kích cỡ, description (EditorJS → text). Payload: sku, name, category (slug), price_vnd, color, material, sizes (canonical order, so the content hash is stable), in_stock (`quantityAvailable > 0`).
- Policies: one document per "## Điều n." heading, text = document title + clause heading + body. Payload: file, clause, title, document_title.
- Point ids: uuid5 of `product:<sku>` / `policy:<file>:<n>` in a fixed namespace.

**R7. Pre-index drift check reuses `scripts/report_vn.py`.**
`build_index.py` calls `report_vn.fetch_products` + `report_vn.diff_catalog` (seeder token, read only) and refuses to build on any difference, so the manifest's catalog hash describes the indexed data. There is no second comparison implementation.

**R8. Manifest `data/index/manifest.json` (committed).**
It records the collection, embedding config and fingerprint, vector layout, document counts, sha256 of the catalog and of each policy file, the drift-check result, and the Qdrant server/client, torch and transformers versions, plus `built_at`. It is rewritten only when something other than `built_at` changes (or the index changed), so an unchanged re-run leaves the committed file byte-identical. An integration test checks it against the live collection.

**R9. Vanished documents are deleted from the index.**
The indexer deletes points whose document no longer exists, so the collection always equals the current document set and the manifest counts stay true. This is scoped to the ShopMate collection. `--rebuild` drops and recreates only that collection.

**R10. Retrieval baseline.**
`Retriever.search(query, filters, k, mode)`: `dense`, or `hybrid` = dense and sparse prefetch (50 candidates each, `PREFETCH_LIMIT`) fused with RRF inside Qdrant. Filters (`SearchFilters`, unknown fields rejected) are applied inside Qdrant on every prefetch branch and the final query. The golden schema's `kind` and `keyword` filters are not supported by the index payload; that is a later, measured decision. No query rewriting, normalisation, reranker or LLM.

**R11. Test split: `pytest` = unit, `pytest -m integration` = live services.**
`addopts = -m 'not integration'`. Unit tests use a fake embedder and `QdrantClient(":memory:")`. Integration tests need Qdrant, the cached model, the built index and Saleor (read only, for the re-index test). `scripts/test_all.sh` runs both and fails if either fails. The filter property test runs 200 seeded random filter combinations × {dense, hybrid}, checks every hit against the catalog and policy files (not Qdrant's payload), and checks that no matching document is lost (`len(hits) == min(k, matches)`). The first calibration (filters too strict: 107 hits in total) failed its own sanity assert; each field is now on with probability 0.3, and all 200 combinations have matches.

**R12. Qdrant `qdrant/qdrant:v1.19.1` in shopmate's own `docker-compose.yml`.**
The image is pinned (released 2026-09-03; v1.19.2 was 4 days old), digest `sha256:12364fe8…`. The port is bound to 127.0.0.1 only, telemetry is disabled, the memory limit is 1 GiB and storage is in `./data/qdrant` (git-ignored). Saleor's compose file is untouched.
