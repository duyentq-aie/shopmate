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
Each point stores `text_sha256` (originally `content_sha256` of text + payload, split in R15) and `embedding_fingerprint` (sha256 of model id, revision, dense dim, pooling, normalisation, max_length, sparse activation/pooling/special-token handling, dtype). A point is re-embedded when either changes. A model or config change therefore never leaves stale vectors. Tests: changing the revision re-embeds everything; max_length / sparse / pooling settings change the fingerprint. When nothing changed, the model is not even loaded (re-run: 1.6 s, 0.1 GiB).

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
`Retriever.search(query, filters, k, mode)`: `dense`, or `hybrid` = dense and sparse prefetch (50 candidates each, `PREFETCH_LIMIT`) fused with RRF inside Qdrant. Filters (`SearchFilters`, unknown fields rejected) are applied inside Qdrant on every prefetch branch and the final query. The golden schema's `kind` filter was added afterwards (R13); `keyword` is deliberately not a filter (R14). No query rewriting, normalisation, reranker or LLM.

**R11. Test split: `pytest` = unit, `pytest -m integration` = live services.**
`addopts = -m 'not integration'`. Unit tests use a fake embedder and `QdrantClient(":memory:")`. Integration tests need Qdrant, the cached model, the built index and Saleor (read only, for the re-index test). `scripts/test_all.sh` runs both and fails if either fails. The filter property test runs 200 seeded random filter combinations × {dense, hybrid}, checks every hit against the catalog and policy files (not Qdrant's payload), and checks that no matching document is lost (`len(hits) == min(k, matches)`). The first calibration (filters too strict: 107 hits in total) failed its own sanity assert. **Correction:** an earlier note here said that with p = 0.3 "all 200 combinations have matches". That figure came from a buggy diagnostic (it drew new random filters for every document), and it was wrong. After `kind` was added (R13), p = 0.3 gave 132/200 non-empty combinations and 809 hits, failing the sanity assert again. The test now uses `FILTER_ON_PROBABILITY = 0.2`: 156/200 combinations match, 1056 hits, 10 non-empty combinations with a kind filter. The 44 empty ones check that nothing false is returned. Sanity asserts: ≥ 100 non-empty combinations, ≥ 1000 hits, ≥ 10 non-empty with kind. A separate test checks every catalog kind in both modes.

**R12. Qdrant `qdrant/qdrant:v1.19.1` in shopmate's own `docker-compose.yml`.**
The image is pinned (released 2026-09-03; v1.19.2 was 4 days old), digest `sha256:12364fe8…`. The port is bound to 127.0.0.1 only, telemetry is disabled, the memory limit is 1 GiB and storage is in `./data/qdrant` (git-ignored). Saleor's compose file is untouched.

## Follow-up before closing 3A (owner's decisions)

**R13. `kind` is a product payload field and a filter.**
`kind` (e.g. "Áo sơ mi nam", 35 values) is not stored in Saleor, so it is sourced from `data/seed/catalog.vi.json` and joined by SKU. This is safe because `build_index.py` runs only after the Saleor-vs-catalog drift check passes, and a Saleor product missing from the catalog stops the build. It has a keyword payload index, is filterable like color (`SearchFilters.kind`) and is listed in the manifest's `filterable_payload`. Tests: unit filter test with kinds, the random-filter property test, and `test_kind_filter_for_every_catalog_kind` (every kind × dense/hybrid, checked against the catalog file). The index was rebuilt: 269 points re-embedded once (88 s) because of R15's new hash fields; the next run embedded nothing and left the manifest unchanged.

**R14. `keyword` is NOT a filter.**
The golden schema's `keyword` (a substring of the product name, e.g. "xếp ly") describes what a customer typed, not a structured constraint. Turning it into a hard filter would make retrieval look perfect on exactly the cases it should earn. It stays a semantic concern for the retrieval model (dense / sparse). `SearchFilters` rejects `keyword` like any unknown field. Gold SKU lists are unchanged by this: the golden tooling still computes them by code from the catalog with `apply_filters` (keyword included), independent of what the retriever supports.

**R15. Separate text and payload hashes.**
Re-embedding key = `text_sha256` + embedding fingerprint (model, revision and settings; R4 unchanged in spirit). A payload-only change (`payload_sha256`, e.g. adding `kind`) is written with `overwrite_payload` without loading the model. This was needed so payload changes such as R13 do not re-embed 269 documents. The first build after this change re-embedded everything once, because the old points had no `text_sha256`. `IndexStats` reports `payload_updated`. Missing payload indexes are added to an existing collection by `ensure_collection`. Test: a payload-only change updates the payload, does not load the model, and the next run is a no-op.
