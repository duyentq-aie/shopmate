# ShopMate

[![CI](https://github.com/duyentq-aie/shopmate/actions/workflows/ci.yml/badge.svg)](https://github.com/duyentq-aie/shopmate/actions/workflows/ci.yml)

**A bounded, measured AI sales assistant for a Vietnamese online shop: a LangGraph agent that answers product, policy and order questions in Vietnamese from retrieved facts, checks its own answers with code before the customer sees them, and records every step of every request in a trace you can replay in a dashboard.** It runs next to an open-source Saleor stack and only ever reads from it.

| The dashboard: the journey of one real request (live traces of a run on synthetic data) | The chat widget on a demo shop page |
|---|---|
| ![Dashboard, tab Request flow: intent routing, filter extraction, retrieval, grounded answer, guardrail check, reply](docs/screenshots/phase7/dashboard-live-product-desktop.png) | ![Chat widget with product cards](docs/screenshots/phase7/widget-products-desktop.png) |

More screenshots (sample data, desktop and phone): [`docs/screenshots/phase7/`](docs/screenshots/phase7/).

## What is in it

- **Retrieval you can defend.** bge-m3 dense vectors plus a diacritic-folded lexical branch in Qdrant, fused client-side with deterministic tie-breaking. Vietnamese shoppers often type without accents; the folded branch was adopted by a decision rule written *before* the numbers ([results](docs/RESULTS-phase4-exp1.md)).
- **An agent with bounds on every loop.** Intent router and filter extractor (Gemini Flash-Lite, free tier, structured output), read-only Saleor tools (order lookup needs the order number *and* the owner's email), a ticket written only after an explicit yes. Two clarifying questions, one relaxed search (and then, if still empty, an offer the customer must accept: "no match; X blocks; N products without it; want to see them?"), three failed verifications, eight steps per turn, never more ([design](docs/AGENT-DESIGN.md)).
- **Grounded answers with code guardrails.** The LLM writes the text from tool results; code then checks every price, stock claim, order status, SKU and clause against those results and replaces a rejected text with a fixed one ([results and an honest account of what that proves](docs/RESULTS-phase6.md)).
- **Traces and a monitor.** One JSON line per request (question, router decision, filters, hits, tool results, guardrail verdict, the reply the customer got, masked for emails, phone numbers and keys); a loopback-only dashboard draws the journey of a request, the flow of all requests and the error board ([decisions](docs/DECISIONS-phase7.md)).
- **A storefront widget and API** (`<shopmate-chat>`, FastAPI, rate limited, CORS-locked, loopback only) with a patch for the Saleor storefront that you apply yourself ([how](docs/STOREFRONT-INTEGRATION.md)).
- **CI that needs no secret and no network** (unit tests, integrity of the committed results, gitleaks over the history).

## Architecture

```mermaid
flowchart LR
    W["Storefront widget<br/>&lt;shopmate-chat&gt;"] -->|"POST /api/chat"| API["Chat API<br/>FastAPI · loopback"]
    API --> A
    subgraph A["LangGraph agent (bounded)"]
        direction TB
        R["Router<br/>6 intents · Gemini"] --> E["Filter extractor<br/>closed vocabulary"]
        R --> POL["Policy search"]
        E --> RET["Hybrid retrieval<br/>dense_folded · bge-m3 + folded BM25<br/>Qdrant"]
        POL --> RET
        R --> TOOLS["Read-only Saleor tools<br/>order lookup · email must match"]
        RET --> ANS["Answer (LLM)"]
        TOOLS --> ANS
        ANS --> G["Code guardrails<br/>price · stock · SKU · clause · PII"]
        G --> OUT["Reply"]
        TOOLS --> TK["Ticket<br/>only after an explicit yes"]
    end
    A -->|"one JSON line per request"| TR[("logs/traces.jsonl")]
    TR --> M["Monitor<br/>127.0.0.1:8765"]
```

## Results (dev split only)

Everything below was measured on the **dev split** (71 questions, the author has seen them). **Held-out test split: evaluated once, in Phase 8.** No test number is claimed here.

| What | Dev result | Where |
|---|---|---|
| Intent router, 6 intents | 71 / 71 correct, macro-F1 1.000 (saturated, see limitations) | [phase 5](docs/RESULTS-phase5.md) |
| Filter extraction, exact match | 23 / 24 (24 / 24 without `in_stock`; the miss is a label conflict) | [phase 5](docs/RESULTS-phase5.md) |
| Retrieval MRR, product / policy, `dense_folded` vs baseline `hybrid` | 0.939 vs 0.838 / 0.911 vs 0.685 (policy gain +0.226, 95% CI [+0.012, +0.464]) | [phase 4](docs/RESULTS-phase4-exp1.md) |
| Retrieval on queries typed without accents (33 items) | MRR +0.430, 95% CI [+0.271, +0.583] | [phase 4](docs/RESULTS-phase4-exp1.md) |
| Questions with no answer in the catalog, handled with an empty result | 5 / 5 | [phase 5](docs/RESULTS-phase5.md) |
| Grounded answers, correct items | 70 / 71 (69 / 71 before one parser fix made with that item in view: not an independent estimate) | [phase 6](docs/RESULTS-phase6.md) |
| Scripted multi-turn conversations | 13 / 13 | [phase 6](docs/RESULTS-phase6.md) |
| Answers replaced by the guardrail's fixed text | 1 of 45 (2.2%; every trigger on dev was a false alarm) | [phase 6](docs/RESULTS-phase6.md) |
| Prompt-injection probes (not part of the golden set) | 7 / 8 as labeled | [phase 5](docs/RESULTS-phase5.md) |
| Cost on a paid tier (theoretical; the free tier cost 0) | 0.978 USD per 1,000 conversations | [phase 6](docs/RESULTS-phase6.md) |
| Latency per turn (measurement) | p50 1.5 s, p95 4.9 s | [phase 6](docs/RESULTS-phase6.md) |

## Honest limitations

- **The router's dev score is saturated.** 71 / 71 says the prompt is good enough for these questions, not how it generalises; the prompt was written after reading the dev items. The real number is the test split's.
- **Guardrail recall is unmeasured.** The model never lied on dev, so there is no recall to estimate: every trigger was a false alarm, and the protection is shown only by hand-made attack tests and mutation tests of the checks ([section 13 of the results](docs/RESULTS-phase6.md)). Sizes, colours, materials and numbers of days in an answer are not checked at all.
- **The golden set v1 has `agent_draft` items**: 44 of the 71 dev items were first drafted by an agent (all are marked human-reviewed), and a few label conflicts are listed in [`docs/GOLDEN-V2-ISSUES.md`](docs/GOLDEN-V2-ISSUES.md).
- **Free-tier latency and limits.** Gemini free tier, 450 requests a day by our own ceiling, rate limited client-side; a turn takes 1 to 5 seconds, with no streaming.
- **Synthetic data only**: a fictional shop, a 250-product synthetic catalog, 30 seeded orders. Nothing here says how it behaves on a real catalog or real customers.
- **Local only**: the chat API and the monitor have no authentication and refuse to bind anything but `127.0.0.1`. A real deployment needs HTTPS and authentication in front of them.

## Quickstart

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker. Python 3.12 is managed by uv.

```bash
uv sync                                    # project-local venv with the locked dependencies (CPU torch included, ~270 MB)
uv run pytest -q                           # unit tests: no services, no network, no key
uv run python -m app.chat_api --fake       # chat widget demo with canned answers: http://127.0.0.1:8766/widget/demo
```

The real stack needs Qdrant and the Saleor stack (sibling folders, see [`docs/SETUP.md`](docs/SETUP.md)):

```bash
docker compose up -d qdrant                                  # Qdrant, in this repo
uv run python -m scripts.check_saleor --wait 60              # Saleor channel `vn` answers
uv run python -m app.chat_api --live                         # the real agent on 127.0.0.1:8766 (Gemini free tier, guarded)
uv run python -m app.monitor                                 # the dashboard on http://127.0.0.1:8765/monitor/
uv run python -m scripts.demo_requests                       # ten hand-written questions through the pipeline (offline from a cache; --live to fetch)
uv run pytest -m integration                                 # tests that need Qdrant, the bge-m3 model and Saleor
uv run python -m eval.run_phase6_eval --answer-prompt answer_v2   # dev evaluation, offline from eval/llm_cache/ (--live fetches what is missing)
uv run python -m eval.verify_committed_results               # the committed results still hash and recompute
```

Configuration is read from the environment, then from `.env` (git-ignored; copy `.env.example`). The names:

`SALEOR_API_URL`, `SALEOR_CHANNEL`, `SALEOR_STAFF_EMAIL`, `SALEOR_STAFF_PASSWORD`, `SALEOR_SEEDER_TOKEN`, `SALEOR_RUNTIME_TOKEN`, `GEMINI_API_KEY`, `QDRANT_URL`; optional: `MONITOR_HOST`, `MONITOR_PORT`, `SHOPMATE_STOREFRONT_ORIGIN`, `SHOPMATE_STOREFRONT_PRODUCT_URL`, `SHOPMATE_CHAT_FAKE`.

Nothing calls the Gemini API unless you pass `--live`, and every live call is counted against a daily ceiling shared with the evaluation.

## Layout

| Path | Purpose |
|---|---|
| `app/agent/` | The bounded conversation agent: LangGraph graph, parsers, fixed replies, tools, answers and guardrails. Design: [`docs/AGENT-DESIGN.md`](docs/AGENT-DESIGN.md). |
| `app/retrieval/`, `data/index/manifest.json` | bge-m3 embeddings, Qdrant index, dense / hybrid / folded retrieval, and the lineage of the index the evaluations run against. |
| `app/llm/`, `prompts/` | Gemini client with free-tier guards (cache, rate limit, daily ceiling), router, filter extractor, versioned prompts. |
| `app/tracing.py`, `app/monitor.py`, `dashboard/` | Trace schema v2, the loopback monitor and its single-file dashboard. |
| `app/chat_api.py`, `widget/` | The chat API and the `<shopmate-chat>` web component and its demo page. |
| `app/saleor/` | The read-only Saleor client (allowlisted operations only). |
| `eval/` | Golden set (test split locked), runners, metrics, committed results and the offline integrity check. |
| `scripts/` | Generators and seeders for the synthetic data, the demo script, Saleor smoke test. |
| `docs/` | Decisions (`DECISIONS-*.md`, one file per phase), results (`RESULTS-*.md`), setup, storefront integration, screenshots. |
| `tests/` | `uv run pytest`: unit tests. `-m integration`: live Qdrant, model and Saleor. |
| `.github/workflows/ci.yml` | Unit tests, results integrity, gitleaks. |

## Data: all synthetic

**All catalog, order and customer data in this repository is synthetic.** It is generated by code with a fixed random seed (`scripts/generate_catalog.py`, `scripts/generate_orders.py`), without any LLM, and describes a fictional shop, "ShopMate Fashion". Customer emails use the reserved `example.com` domain; addresses are made up; the policy documents are fictional. One product is one variant is one SKU; size is a multiselect of available sizes and there is no per-size stock.

## Ownership

ShopMate is my own AI service. It runs alongside Saleor as a separate process and is not a fork or modification of it. All code in this repository is mine. The Saleor stack used for local development is third-party infrastructure that lives outside this repository (sibling folders `saleor-platform/` and `storefront/`); no Saleor code is part of this repo except Saleor's GraphQL schema (SDL), kept unmodified with its BSD-3-Clause license as a test fixture in `tests/fixtures/`. [saleor-platform](https://github.com/saleor/saleor-platform) is BSD-3-Clause; the [Saleor storefront](https://github.com/saleor/storefront) is FSL-1.1-ALv2.
