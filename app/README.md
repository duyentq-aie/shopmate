# app/

The ShopMate service. Phase 2 contains only its data foundation:

- `catalog/schema.py`, `orders/schema.py` – pydantic schemas of the seed files in `data/seed/`;
- `policies.py` – access to the numbered clauses of `data/policies/`;
- `shop.py` – shop constants (currency, shipping fee, free-shipping threshold) shared by the seeders, the policies and tests;
- `saleor/readonly_client.py` – the only Saleor client the service may use: allowlisted, parsed, read-only queries (see `docs/SETUP.md`);
- `config.py` – settings from the environment or `.env`.

Retrieval, the agent and the HTTP API come in later phases. The service reads Saleor only through the read-only client with the `shopmate-runtime` app token; it never uses a staff account or the seeder token.
