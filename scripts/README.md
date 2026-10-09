# scripts/

Operational helpers that are not part of the service itself. Run them from the repo root with `uv run python -m scripts.<name>`. Tokens come from the environment or `.env` (git-ignored); see `docs/SETUP.md`.

| Script | Purpose | Saleor access |
|---|---|---|
| `generate_catalog.py` | Writes the synthetic catalog `data/seed/catalog.vi.json` (fixed seed). `--check` verifies the file is current. | none |
| `generate_orders.py` | Writes the synthetic order fixtures `data/seed/orders.vi.json`, keeping recorded order numbers. `--check` ignores numbers. | none |
| `seed_channel.py` | Creates channel `vn` (VND, VN), warehouse, shipping zone and shipping methods. Idempotent. | seeder token, writes |
| `seed_catalog.py` | Loads the catalog into channel `vn`, keyed by SKU. Idempotent (create or skip). `--reset-vn` deletes ShopMate products in `vn` after confirmation (destructive). | seeder token, writes |
| `seed_orders.py` | Creates the test orders in `vn` (append-only, keyed by externalReference) and records their numbers. | seeder token writes; runtime token reads numbers |
| `report_vn.py` | Counts products and orders in channel `vn` and reports drift from the seed files; exit 1 on drift. | seeder token reads catalog; runtime token reads orders |
| `check_saleor.py` | Smoke test: shop name, product and order counts of one channel (`SALEOR_CHANNEL`). `--wait SECONDS` retries until the API answers. | runtime token (read-only parse check), staff login from env vars as fallback |

`seedlib/` holds the write-capable client and GraphQL documents used only by these local scripts. The service code in `app/` never imports it.
