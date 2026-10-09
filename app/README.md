# app/

The ShopMate service itself. This is where the following will live, starting in Phase 2:

- the catalog ingestion and retrieval (RAG) pipeline,
- the agent and its tools (product search, order lookup),
- the HTTP API that the storefront or other clients call.

The service reads Saleor data only through the GraphQL API, authenticating with a Saleor App token scoped to the minimum permissions it needs (read products, read orders). It never uses a staff account.

Empty for now.
