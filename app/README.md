# app/

The ShopMate service. It reads Saleor only through the read-only client with the `shopmate-runtime` app token; it never uses a staff account or the seeder token.

- `agent/` – the bounded conversation agent (LangGraph): graph, parsers, fixed replies, tools, answers and code guardrails (`docs/AGENT-DESIGN.md`);
- `llm/` – Gemini client with free-tier guards, router, filter extractor, prompts, cache;
- `retrieval/` – bge-m3 embeddings, the Qdrant index, dense / hybrid / diacritic-folded search;
- `tracing.py`, `monitor.py` – request tracing (schema v2, `docs/DECISIONS-phase7.md`) and the loopback monitor that serves `dashboard/index.html`;
- `chat_api.py` – the chat API for the storefront widget (`widget/`); `--fake` for canned answers, `--live` for the real agent;
- `pipeline.py` – one turn of the agent with optional tracing;
- `catalog/schema.py`, `orders/schema.py` – pydantic schemas of the seed files in `data/seed/`;
- `policies.py` – access to the numbered clauses of `data/policies/`;
- `shop.py` – shop constants (currency, shipping fee, free-shipping threshold) shared by the seeders, the policies and tests;
- `saleor/readonly_client.py` – the only Saleor client the service may use: allowlisted, parsed, read-only queries (`docs/SETUP.md`);
- `config.py` – settings from the environment or `.env`.
