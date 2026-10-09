# scripts/

Operational helpers that are not part of the service itself.

| Script | Purpose |
|---|---|
| `check_saleor.py` | Smoke test for the local Saleor API. Prints the shop name, channel slug, product count and order count; exits non-zero on failure. Standard library only. |

Run it with the Saleor credentials passed through the environment (never written to a file):

```bash
SALEOR_STAFF_EMAIL=... SALEOR_STAFF_PASSWORD=... python3 scripts/check_saleor.py
```

Optional: `SALEOR_API_URL`, `SALEOR_CHANNEL` (default `default-channel`), `--min-products`, `--min-orders`. From Phase 2 on, use `SALEOR_APP_TOKEN` instead of staff credentials.

Later phases will add scripts here, for example catalog seeding and index building.
