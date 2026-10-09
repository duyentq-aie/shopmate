# ShopMate

ShopMate is an AI sales assistant for e-commerce: a retrieval-augmented (RAG) agent that answers shopper questions grounded in a store's product catalog and order data. It runs as its own service next to an open-source Saleor stack and talks to Saleor only through its GraphQL API. This repository currently holds the Phase 1 groundwork (a reproducible local Saleor environment and a smoke test); the AI service itself is not built yet.

## Layout

| Path | Purpose |
|---|---|
| `app/` | The ShopMate service (RAG pipeline and agent). Empty until Phase 2. |
| `eval/` | Evaluation datasets and scripts. Empty until later phases. |
| `scripts/` | Operational helpers, starting with `check_saleor.py`. |
| `docs/SETUP.md` | How the local Saleor environment was set up, with versions, ports and fixes. |
| `.env.example` | Configuration template (placeholders only). |

## Quick check

With the local Saleor stack running (see [`docs/SETUP.md`](docs/SETUP.md)):

```bash
SALEOR_STAFF_EMAIL=... SALEOR_STAFF_PASSWORD=... python3 scripts/check_saleor.py
```

## Ownership

ShopMate is my own AI service. It runs alongside Saleor as a separate process and is not a fork or modification of it. All code in this repository is mine.

The Saleor stack used for local development is third-party infrastructure that lives outside this repository (in sibling folders `saleor-platform/` and `storefront/`). **No Saleor code is part of this repo.** It is cloned and run as-is, and each project keeps its own license:

- [saleor-platform](https://github.com/saleor/saleor-platform): BSD-3-Clause
- [Saleor storefront](https://github.com/saleor/storefront): FSL-1.1-ALv2 (Functional Source License 1.1, with an Apache 2.0 future license)
