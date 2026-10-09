# Local environment setup (Phase 1)

How the local Saleor stack that ShopMate runs next to was set up, on 2026-10-09. Everything here was run and verified, except where a line says "not tested".

## Workspace layout

```
~/shopmate-workspace/        (inside WSL, ext4; never on /mnt/c)
├── saleor-platform/   upstream clone, run only (BSD-3-Clause)
├── storefront/        upstream clone, run only (FSL-1.1-ALv2)
└── shopmate/          this repo, the only place my code goes
```

The only file created or edited inside the upstream clones is `storefront/.env` (git-ignored). Installing and running the storefront also produces git-ignored build output (`node_modules/`, `.next/`, `src/gql/`) and sets `core.hooksPath` in `storefront/.git/config` (see "Problems and fixes"). `git status` is clean in both clones after setup.

## Services and ports

| Service | URL / port | Notes |
|---|---|---|
| Saleor API | http://localhost:8000/graphql/ | image `ghcr.io/saleor/saleor:3.23` |
| Dashboard | http://localhost:9000 | image `saleor-dashboard:3.23` |
| Storefront | http://localhost:3000 | Next.js dev server, redirects to `/en/default-channel` |
| Mailpit (test email) | http://localhost:8025 (SMTP 1025) | v1.31.4 |
| Jaeger (tracing) | http://localhost:16686 (OTLP 4317, 4318) | |
| PostgreSQL 15 | localhost:5432 | upstream default credentials, local only |
| Valkey 8.1 (Redis-compatible) | localhost:6379 | |

Ports 3000, 8000, 9000, 8025, 5432, 6379, 16686, 4317, 4318 and 1025 were all free before starting (checked from WSL and from Windows).

## Versions

| Tool | Version |
|---|---|
| OS | Ubuntu 26.04 on WSL2 (kernel 6.6.87.2) |
| Docker Desktop / Compose | Docker 29.4.0, Compose v5.1.1 |
| Memory Docker sees | 7.64 GiB (upstream asks for at least 5 GB) |
| Git | 2.53.0 |
| Node used for the storefront | 24.21.0 (from `.nvmrc`), via nvm 0.40.8; npm 11.19.0 |
| Node installed system-wide (apt) | 22.22.1, left untouched |
| corepack / pnpm | 0.36.0 (bundled with Node 24) / 10.28.1 (pinned by `packageManager`) |
| Python | 3.14.4 (system `python3`); there is no `python` command and no `pip3` |
| saleor-platform | commit `ab6315b` (2026-04-16, "Update Saleor and Saleor Dashboard images to 3.23") |
| storefront | commit `b73bdce3` (2026-10-01, Next.js 16.3.8) |

## Channels and seed data

- Channel slug used everywhere: **`default-channel`** (name "Channel-USD", USD). The second channel is `channel-pln` (PLN).
- Each channel has 29 products.
- `populatedb` also creates **20 orders** (#1 to #20). So "order count >= 1" is true on a fresh stack and proves nothing about a new order. The baseline is 20.
- Superuser: `admin@example.com`. The password is the default documented in the saleor-platform README (step 4 of "How to run it?"). Local development only.

## Commands that worked

### A. Saleor platform

```bash
mkdir -p ~/shopmate-workspace && cd ~/shopmate-workspace
git clone https://github.com/saleor/saleor-platform.git
cd saleor-platform
docker compose run --rm api python3 manage.py migrate
docker compose run --rm api python3 manage.py populatedb --createsuperuser
docker compose up -d
```

The first run pulls all images, which takes a few minutes. `docker compose run` also starts `db`, `cache` and `jaeger` in the background.

### B. Verify the API

```bash
curl -s -H 'Content-Type: application/json' \
  -d '{"query":"{ shop { name } }"}' http://localhost:8000/graphql/
```

Returns `{"data":{"shop":{"name":"Saleor e-commerce"}, ...}}`. Channels and order counts need authentication; use `scripts/check_saleor.py` (below). The Dashboard at :9000 answers HTTP 200 and is configured with `API_URL=http://localhost:8000/graphql/`.

### C. Storefront

```bash
cd ~/shopmate-workspace
git clone https://github.com/saleor/storefront.git

# Node 24 (the storefront requires it) via nvm, cloned at a pinned tag.
# This does NOT edit ~/.bashrc, so nvm must be sourced explicitly (see "Daily use").
git clone --depth 1 --branch v0.40.8 https://github.com/nvm-sh/nvm.git ~/.nvm
cd storefront
source ~/.nvm/nvm.sh && nvm install        # reads .nvmrc -> v24.21.0

# pnpm through corepack, shims in a user directory (no sudo; ~/.local/bin was already on PATH)
corepack enable --install-directory ~/.local/bin

cp .env.example .env                        # then edit the values below
pnpm install --frozen-lockfile
pnpm dev
```

Active (non-comment) lines in `storefront/.env`:

```
NEXT_PUBLIC_SALEOR_API_URL=http://localhost:8000/graphql/
NEXT_PUBLIC_STOREFRONT_URL=http://localhost:3000
NEXT_PUBLIC_DEFAULT_CHANNEL=default-channel
CONTENT_PROVIDER=saleor
ALLOW_DUMMY_PAYMENT=true
NEXT_PUBLIC_ALLOW_DUMMY_PAYMENT=true
```

`http://localhost:3000` redirects to `/en/default-channel` and lists products.

### D. Smoke test

```bash
cd ~/shopmate-workspace/shopmate
SALEOR_STAFF_EMAIL=admin@example.com SALEOR_STAFF_PASSWORD=<local dev password> \
  python3 scripts/check_saleor.py
```

Credentials go through the environment only; nothing is written to a file. Exit codes: 0 ok, 1 a check failed, 2 missing credentials. Output after the test order (Phase 1 version of the script; see "Smoke test since Phase 2" below for the current command):

```
shop name      : Saleor e-commerce
channel slug   : default-channel  (available: channel-pln, default-channel)
product count  : 29  (channel default-channel)
order count    : 21  (all channels)
latest order   : #23 created 2026-10-09T05:29:52.377076+00:00 by shopmate-test@example.com in default-channel
OK: all checks passed
```

### Smoke test since Phase 2

The script now runs inside the uv environment, reads `SALEOR_RUNTIME_TOKEN` from the environment or `.env`, and falls back to `SALEOR_STAFF_EMAIL`/`SALEOR_STAFF_PASSWORD` from **environment variables only**. All counts are scoped to `SALEOR_CHANNEL` (`.env` sets `vn`; use `SALEOR_CHANNEL=default-channel` for the demo channel). Requests sent with the runtime token pass the same read-only parse check as the service client.

```bash
cd ~/shopmate-workspace/shopmate
uv run python -m scripts.check_saleor --wait 60
```

**"Container started" is not "API ready".** Right after `docker compose up -d` the API container is up but Saleor may still be booting: connections are refused or reset mid-response (`ConnectionResetError` raised from `getresponse`). The script reports this as `FAIL: Saleor API not reachable or still starting (...)` with exit 1. `--wait SECONDS` retries `{ shop { name } }` with exponential backoff (0.5 s doubling to 5 s) until the API answers or the time is up.

## Daily use

```bash
# Saleor stack
cd ~/shopmate-workspace/saleor-platform
docker compose up -d        # start
docker compose stop         # stop, keeps the data

# Storefront (every new terminal needs nvm sourced, because ~/.bashrc was not edited)
cd ~/shopmate-workspace/storefront
source ~/.nvm/nvm.sh && nvm use
pnpm dev
```

Never run `docker compose down --volumes` or `docker compose rm` casually: they delete the database volume (all data, the superuser and the test order).

## Test order

The test order was **not** placed through the storefront checkout, because Dummy Payment does not work with this backend (see below). It was created in the Dashboard instead:

1. Dashboard, Orders, Create order, channel `Channel-USD`.
2. Add a product, set the customer email `shopmate-test@example.com`, add a US shipping and billing address, choose a shipping method.
3. **Finalize** (a draft order is not counted as an order until it is finalized).

Result: order **#23**, status UNFULFILLED, origin DRAFT, channel `default-channel`, payment status NOT_CHARGED. Order numbers #21 and #22 were consumed by earlier drafts that no longer exist, so numbers are not contiguous. Total order count went from 20 to 21.

## Problems and fixes

| # | Problem | Cause | Fix |
|---|---|---|---|
| 1 | `docker` not found in WSL | `/usr/bin/docker` was a dangling symlink; Docker Desktop was not running and WSL integration was off | Start Docker Desktop; Settings, Resources, WSL integration, enable Ubuntu |
| 2 | `channels` query returns `PermissionDenied` | Channels and orders need `AUTHENTICATED_APP` or `AUTHENTICATED_STAFF_USER`; only `shop.name` and channel products are public | Authenticate (`tokenCreate`, or an app token); `check_saleor.py` does this |
| 3 | Storefront needs Node 24, system has 22.22.1 | `.nvmrc` = 24, `engines.node` = 24.x | nvm cloned at tag v0.40.8 into `~/.nvm`, no `.bashrc` edit. The first `nvm install` printed nothing and installed nothing (cause unknown); running it again worked |
| 4 | pnpm missing | not installed | `corepack enable --install-directory ~/.local/bin` using Node 24's corepack. This also creates `yarn` and `yarnpkg` shims there. The old apt corepack 0.24.0 was deliberately not used (not tested) |
| 5 | `pnpm install` warns "Ignored build scripts" (`@parcel/watcher`, `@swc/core`, `esbuild`, `unrs-resolver`) | pnpm 10 blocks dependency build scripts by default | Left as is. `pnpm approve-builds` would edit `pnpm-workspace.yaml`. The dev server works without them |
| 6 | `pnpm install` changes `storefront/.git/config` | the `prepare` script runs `husky install`, which sets `core.hooksPath=.husky` | Accepted. It is not a tracked file; nothing is committed in `storefront/` |
| 7 | `pnpm dev` writes files | `predev` runs graphql-codegen into `src/gql/` | None needed. `src/gql/` is git-ignored |
| 8 | **Checkout: "Payment failed. App with provided identifier not found."** | See "Dummy Payment" below | Order created from the Dashboard instead |
| 9 | Product thumbnails are broken in the checkout summary | Next's image optimizer refuses to fetch from `localhost:8000` ("resolved to private IP ... `images.dangerouslyAllowLocalIP`") | Cosmetic, not fixed. Fixing means editing `next.config.js`, which is off limits |
| 10 | Dashboard login rejected for about a minute | Saleor suspends login after a failed attempt ("Logging has been suspended till ...") | Wait for the time in the message, then retry. Avoid scripting wrong-password tests |
| 11 | Address form says "This value is not valid for the address" | Postal code must match the state/region (for example `10001` with `NY`) | Pick a consistent set |

### Dummy Payment

The storefront pays through the Transactions API (`transactionInitialize`) with gateway id `mirumee.payments.dummy`. That call only works with an installed Saleor **App**. This backend has no apps installed (`apps.totalCount = 0`). `mirumee.payments.dummy` is a legacy plugin: it shows up in `shop.availablePaymentGateways`, but `transactionInitialize` cannot use it. `ALLOW_DUMMY_PAYMENT` only opens the storefront-side gate; it does not create the app.

To make checkout work end to end, the Dummy Payment App (id `saleor.io.dummy-payment-app`) has to be installed in Saleor. Findings from the docs, **not tested**:

- The original repo `saleor/dummy-payment-app` is archived; the app now lives in the `saleor/apps` monorepo, folder `apps/dummy-payment-app`.
- Install is Dashboard, Apps, Install external app, with the app's `/api/manifest` URL.
- A hosted app cannot reach a Saleor on `localhost`, so it would have to run locally. Saleor's outgoing-request filter (`HTTP_IP_FILTER_ENABLED=True` in `saleor-platform/common.env`) blocks private addresses; the upstream comment says to set it to `False` for local app development. Do that with a compose override file kept outside `saleor-platform/`, not by editing upstream files.
- The app's dev server also defaults to port 3000, which the storefront uses.

## Fragile points

- **Dummy Payment end to end does not work** (above). Phase 1's "order placed through the storefront" criterion was replaced by a Dashboard order.
- **Moving upstream.** The Saleor images use the floating tag `3.23`, and the storefront was cloned from `main`. Record commits (above) if you need to reproduce this exactly.
- **nvm is not hooked into `~/.bashrc`.** A fresh terminal has Node 22; the storefront needs the explicit `source ~/.nvm/nvm.sh && nvm use`.
- **The dev server is a background process.** It stops when its shell or session ends.
- **Default credentials and exposed ports.** PostgreSQL (`saleor`/`saleor`) and Valkey are published on 0.0.0.0, and `SECRET_KEY=changeme` comes from upstream `backend.env`. Fine for local development; do not expose this machine's ports to a network.
- **Order count is not a regression signal** while seed data exists (20 orders). Compare against a known baseline or look at the latest order instead.

## Known risks for Phase 2

- **Python version.** The system Python is 3.14.4. AI libraries such as PyTorch and sentence-transformers may not support 3.14 yet. Plan: install `uv` and use a project-local Python 3.12 (no sudo). Not done in Phase 1.
- **`python` command.** Only `python3` exists. Phase 1 scripts use `python3` and the standard library only, so no pip is needed. A project-local virtual environment from `uv` will provide `python`.
- **Authentication for the service.** From Phase 2 on, the ShopMate service must authenticate with a **Saleor App token scoped to the minimum permissions it needs (read products, read orders), never the admin account.** `SALEOR_STAFF_EMAIL` and `SALEOR_STAFF_PASSWORD` exist for Phase 1 only. Caveat, to verify when creating the app: Saleor has no read-only permission scopes (reading orders needs `MANAGE_ORDERS`, which also allows writes), so "read-only" has to be enforced in ShopMate's own tool layer by exposing only read queries.

---

# Phase 2: data foundation

Status on 2026-10-09 (evening): both apps exist with the permissions below (checked with `query { app { name permissions { code } } }`), and channel `vn`, the catalog and the test orders are seeded and verified. The "not yet executed" notes below are kept for history; the results are in "Seeding results".

## Python environment

| Item | Value |
|---|---|
| uv | 0.12.22, already in `~/.local/bin` (no sudo) |
| Python | 3.12.15, installed by `uv python install 3.12` into `~/.local/share/uv/python`; pinned by `.python-version` and `requires-python = ">=3.12,<3.13"` |
| Runtime dependencies | `httpx`, `pydantic`, `graphql-core` (locked in `uv.lock`) |
| Dev dependency group | `pytest` |

```bash
cd ~/shopmate-workspace/shopmate
uv sync
uv run python --version   # Python 3.12.15
uv run pytest
```

The project is not an installable package (`[tool.uv] package = false`); entry points run from the repo root as modules, e.g. `uv run python -m scripts.seed_channel`.

## Saleor apps and permissions (least privilege)

Verified against `PermissionEnum` of Saleor 3.23 (24 values, read by introspection): **Saleor has no read-only permission for orders.** The only order permissions are `MANAGE_ORDERS` (read *and* write: cancel, fulfill, refund, edit drafts) and `MANAGE_ORDERS_IMPORT` (`orderBulkCreate` only). Reading orders therefore requires a permission that can also modify them. This is a real trade-off: read-only for the runtime is enforced in ShopMate's own client (next section), not by Saleor.

Create both apps in the Dashboard: Extensions (called Apps in older dashboards), Add extension, Provide details manually. Then Create token on the app page and paste it into `shopmate/.env`. Never commit it.

| App | `.env` variable | Permissions | Why |
|---|---|---|---|
| `shopmate-seeder` (local only) | `SALEOR_SEEDER_TOKEN` | `MANAGE_CHANNELS` | create channel `vn`, link its warehouse |
| | | `MANAGE_PRODUCTS` | warehouse, categories, products, stock; read exact stock for the drift report |
| | | `MANAGE_PRODUCT_TYPES_AND_ATTRIBUTES` | attributes màu sắc / kích cỡ / chất liệu, the product type |
| | | `MANAGE_SHIPPING` | shipping zone VN and shipping methods |
| | | `MANAGE_ORDERS_IMPORT` | create test orders with `orderBulkCreate` (narrower than `MANAGE_ORDERS`; source: `order_bulk_create.py` requires only this) |
| `shopmate-runtime` (the AI service) | `SALEOR_RUNTIME_TOKEN` | `MANAGE_ORDERS` only | order lookup. Products are read with public channel-scoped queries (published products only), so the runtime gets **no** catalog permission and cannot modify catalog data |

Consequences of the split:

- The seeder cannot read orders (no `MANAGE_ORDERS`). Order seeding is idempotent through `orderBulkCreate` itself (a duplicate `externalReference` is rejected with code `UNIQUE`), and order numbers are read back with the read-only runtime client.
- The runtime cannot see exact stock: `stocks` needs `MANAGE_PRODUCTS`, and the public `quantityAvailable` is capped at 50 per checkout (`limit_quantity_per_checkout`). The synthetic catalog keeps stock ≤ 50 so `quantityAvailable` equals the real stock. `report_vn.py` reads stock with the seeder token.
- *Not yet executed:* that these permission sets are sufficient is derived from Saleor's source and schema. If a call fails with `PermissionDenied`, add only the permission named in the error and record it here.

## Read-only runtime client

`app/saleor/readonly_client.py` is the only Saleor client the service may use.

1. **Primary guard: the allowlist.** Callers, including any future LLM tool, choose an operation **name** from `ALLOWED_OPERATIONS` and pass variables that are validated by that operation's pydantic model (extra keys forbidden). There is no method that accepts GraphQL text, so a caller can never supply a mutation. Allowed operations: `ChannelBySlug`, `ProductsInChannel`, `VariantBySku`, `OrderByNumber`, `OrderByExternalReference`, `OrdersInChannel`.
2. **Second layer: parsing.** Every allowlisted document is parsed with graphql-core at import and again just before sending. Anything that is not exactly one named `query` operation (plus fragments) is rejected: mutations, subscriptions, multi-operation documents, anonymous or misnamed queries, unparseable text.

`tests/test_readonly_client.py` proves that an allowlisted query is sent, and that a mutation, a subscription, a multi-operation document, an unknown operation name, raw GraphQL passed as a name and invalid variables are all rejected **before any network call** (fake transport records zero requests).

## Seeding channel `vn` (*written, not yet executed*)

Run in this order, from `shopmate/`, after both tokens are in `.env`:

```bash
uv run python -m scripts.seed_channel   # channel vn (VND, VN), warehouse shopmate-vn-hcm, zone "ShopMate – Việt Nam", 2 shipping methods
uv run python -m scripts.seed_catalog   # 250 products from data/seed/catalog.vi.json
uv run python -m scripts.seed_orders    # 30 orders from data/seed/orders.vi.json, records order numbers
uv run python -m scripts.report_vn      # counts in channel vn + drift check, exit 0 = no drift
```

Every seeder is idempotent and can be re-run. What each one owns, and how it recognises it:

| Object | Key | Re-run behaviour |
|---|---|---|
| Channel `vn` | slug `vn` | skipped if it exists; **error** (never changed) if currency/country differ |
| Warehouse | slug `shopmate-vn-hcm` | skipped; re-linked to `vn` if unlinked |
| Shipping zone | name `ShopMate – Việt Nam` | skipped; re-linked to channel/warehouse if needed |
| Shipping methods | name in that zone | skipped; **error** if the `vn` price differs (no silent price change) |
| Attributes `mau-sac`, `kich-co` (MULTISELECT), `chat-lieu` | slug + externalReference `shopmate-attribute-*` | skipped; error if an attribute with that slug is not ShopMate's |
| Product type `shopmate-thoi-trang` | slug | skipped; missing attributes assigned |
| Categories | slug + externalReference `shopmate-category-*` | skipped; error if the slug belongs to something else |
| Products | `externalReference` = SKU | created if missing, **skipped (not updated)** if present |
| Orders | `externalReference` = `shopmate-seed-order-NNN` | created if missing, never recreated or modified |

`default-channel`, `channel-pln` and the demo data are never read for writing or changed.

Shipping configuration (single source `app/shop.py`, quoted in `data/policies/van-chuyen.md`, checked by `tests/test_policies.py`): "Giao hàng tiêu chuẩn" 30.000đ for orders below 500.000đ (maximum order price 499.999đ), "Miễn phí vận chuyển" 0đ from 500.000đ, delivery 1–5 days.

Orders are created with `stockUpdatePolicy: SKIP`, so they do not change catalog stock, and with `taxRate 0` and explicit prices.

### Seeding results (Saleor 3.23.40)

| Step | Run 1 | Run 2 (idempotency) |
|---|---|---|
| `seed_channel` | warehouse, channel `vn` (VND, VN), zone, 2 shipping methods created | nothing to do (after the D34 fix) |
| `seed_catalog` | 250 created | 0 created, 250 already existed |
| `report_vn --catalog-only` | products in channel vn: 250 | products in channel vn: 250 |
| `seed_orders` | 30 created, numbers #84–#113 recorded (third attempt, see D31–D33) | 0 created, 30 already existed |
| `report_vn` | products in vn: 250, orders in vn: 30, no drift | |

Counts per channel with the runtime token (`check_saleor.py`): `vn` 250 products / 30 orders; `default-channel` 29 / 8 (latest still #23); `channel-pln` 29 / 13. The demo channels together still hold the 21 orders of Phase 1.

Saleor behaviours found while seeding (details in DECISIONS D31–D34):

- `orderBulkCreate` cannot look up variants by `variantSku` (a cache-key bug in 3.23.40). The seeder uses `variantExternalReference`, which `seed_catalog.py` sets to the SKU.
- `orderBulkCreate` needs `ShippingMethod:<pk>` ids (zones return `ShippingMethodType:<pk>`) and a tax class for shipping and for each line. The seeder references the existing "No taxes" class, read only.
- Rejected bulk-create rows still consume order numbers.
- An unset `minimumOrderPrice` reads back as 0.

### Drift report

`scripts/report_vn.py` scopes every count to channel `vn` (demo data makes global counts meaningless) and compares Saleor with the seed files:

- catalog: missing and extra products; per SKU the name, category, price (VND), stock, màu sắc, chất liệu, kích cỡ, published in `vn`;
- orders, matched by externalReference (never by count or number): missing orders, status, recorded number, line SKUs and quantities. A line whose product was deleted is a **warning**, not a failure.

Exit codes: 0 no drift, 1 drift, 2 configuration error. `--catalog-only` skips the order part.

### Reset (destructive)

```bash
uv run python -m scripts.seed_catalog --reset-vn
```

Deletes only products that have the ShopMate product type, an SKU-shaped externalReference and a listing in `vn`. It asks for the confirmation phrase `xoa-san-pham-vn` (or `--confirm xoa-san-pham-vn` when not interactive). **Orders are never deleted:** they are append-only fixtures, and Saleor cannot delete non-draft orders through the API anyway. Ask before running this.

## Product images

Not seeded. Products have no media. Fine for the API and the assistant; storefront product pages show placeholders. Not planned before a later phase.

## Data files

| File | Produced by | Notes |
|---|---|---|
| `data/seed/catalog.vi.json` | `scripts/generate_catalog.py`, seed 20261009 | 250 products, 6 categories, 25 with stock 0, prices 69.000đ–1.869.000đ. Byte-identical on every run (`tests/test_catalog_determinism.py`, also across `PYTHONHASHSEED` values) |
| `data/seed/orders.vi.json` | `scripts/generate_orders.py`, seed 20261010; numbers by `scripts/seed_orders.py` | 30 orders: 10 FULFILLED, 9 UNFULFILLED, 4 PARTIALLY_FULFILLED, 4 CANCELED, 3 UNCONFIRMED; 20 customers `khachhangNN@example.com`; numbers `pending` until seeded |
| `data/policies/*.md` | written by hand | đổi trả (7 clauses), vận chuyển (6), bảo hành (6), headed `## Điều N. …` |
| `eval/golden/golden.vi.json` | `eval/golden/draft_golden.py` (refuses to overwrite) | 64 agent drafts, see below |

## Golden set

Schema: `eval/golden/schema.py`. Item fields: `id`, `question`, `intent` (product_search, policy_question, order_status, create_ticket, out_of_scope, smalltalk), `query_style` (co_dau, khong_dau, teencode), `expected_filters` (product_search only: category, kind, keyword, color, material, size, price_min, price_max, in_stock), `gold_source` (`skus` | `policy_clause` | `order` | `no_answer`), `split` (dev/test), `status`, `author`, `notes`.

- Gold SKU lists of filtered product searches are computed from the catalog by `apply_filters`; the validator recomputes them, so labels cannot drift from the data.
- Order questions use the literal placeholder `{order_number}`; the gold source names the fixture by external reference. `order_number` stays `pending` until step H.
- **Anti-IDOR (DECISIONS D28, D37):** order data may only be revealed for order number + the owner's email (v1 is email-only; a phone alone → ask for the email). Order gold sources carry `provided_identity` and `expected_behavior`: `matching_email` → `answer`, `none` → `ask_for_verification`, `mismatched_email` → `refuse`. The validator checks the email in the question against the fixture's owner.
- 64 agent drafts, all `status: draft_needs_human_review`, `author: agent_draft`: 22 product_search (2 deliberate no-match, 1 out-of-stock product), 12 policy_question, 12 order_status (gs-061..064 are the verify-or-refuse cases), 6 create_ticket, 6 out_of_scope, 6 smalltalk; 33 có dấu, 17 không dấu, 14 teencode; 43 dev, 21 test (per intent, the ~30% with the smallest sha256(id)).

Human-written items (DECISIONS D36): write content in `eval/golden/human_items.toml` (syntax and 3 tested examples in `eval/golden/human_items.example.toml`; ids from gs-101), then:

```bash
uv run python -m eval.golden.merge_human_items --dry-run   # validate, print per-stratum counts, write nothing
uv run python -m eval.golden.merge_human_items             # compute gold labels, merge, re-split all items
```

The script computes gold SKUs, order numbers and expected order behavior, sets `author: human` / `status: human_reviewed`, never changes gs-001…gs-064 except their `split`, and re-splits all items stratified by intent × query_style (~70/30). It prints the cell table for information and warns when a margin of the test split is low: each query_style ≥ 8 test items, product_search and policy_question ≥ 6, other intents ≥ 2 (D37). Order verification is email-only in v1: a question giving only a phone expects `ask_for_verification`. The TOML is the source of truth for human items: edit them there, not in `golden.vi.json`.

Review helper (read only, never writes): `uv run python -m eval.golden.check_query_style` flags items whose `query_style` label likely disagrees with the text (abbreviation tokens such as ko, k, dc, đc, sp, j, ship → teencode; no diacritics → khong_dau). The owner decides every label.

Review labels: reviewed items get `status: "human_reviewed"`. The 64 drafts keep `author: "agent_draft"`; `author: "human"` is only for items the owner writes.

```bash
uv run python -m eval.golden.validate_golden                          # now: pending order numbers allowed
uv run python -m eval.golden.sync_order_numbers                       # after seed_orders.py
uv run python -m eval.golden.validate_golden --require-order-numbers  # after the sync
```

**Test split: FROZEN on 2026-10-09.**

```
4070dc68164dcf573ee0ed884d685050cd77c87c1b833300920eb9484c8eaf90
```

Recorded in `eval/golden/test_split.sha256`. The final set has 104 items (64 `agent_draft` + 40 `human`, all `human_reviewed`), 71 dev / 33 test. Test-split margins: co_dau 12, khong_dau 9, teencode 12; product_search 10, policy_question 6, order_status 6, create_ticket 4, out_of_scope 3, smalltalk 4 (all thresholds met). Guards:

- `tests/test_frozen_test_split.py` fails if any test item changes, moves in or out of the test split, or the set is re-split;
- `merge_human_items.py` refuses to write a merge that would change the frozen split (dry runs still work);
- `uv run python -m eval.golden.validate_golden --require-order-numbers --expected-test-hash 4070dc68164dcf573ee0ed884d685050cd77c87c1b833300920eb9484c8eaf90`.

Changing the test split after this point creates a new evaluation baseline. Do it only deliberately: record why here, then replace the hash in `eval/golden/test_split.sha256`.

## Offline checks against Saleor's schema

Every runtime and seeder GraphQL document is validated against Saleor's SDL by `tests/test_documents_against_schema.py`, with no live Saleor. The SDL is a versioned fixture: `tests/fixtures/saleor-schema-3.23.40.graphql`, unmodified from image `ghcr.io/saleor/saleor:3.23` (version label 3.23.40), with Saleor's BSD-3-Clause license in `tests/fixtures/SALEOR-LICENSE`. The tests never skip; a missing fixture fails. When the Saleor image changes, refresh the fixture and rename it after the new version:

```bash
docker inspect saleor-platform-api-1 --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
docker cp saleor-platform-api-1:/app/saleor/graphql/schema.graphql /tmp/saleor.graphql
SALEOR_SCHEMA_SDL=/tmp/saleor.graphql uv run pytest tests/test_documents_against_schema.py   # try before replacing
```

Schema validation does not check permissions or Saleor's business rules.

---

# Phase 3A: retrieval infrastructure

Decisions: `docs/DECISIONS-phase3a.md`. Baseline only; no evaluation has been run.

## Qdrant

```bash
cd ~/shopmate-workspace/shopmate
docker compose up -d qdrant        # shopmate's own compose file; never Saleor's
curl -s http://127.0.0.1:6333/readyz
```

Image `qdrant/qdrant:v1.19.1` (pinned), REST on `127.0.0.1:6333` only, storage `./data/qdrant` (git-ignored), memory limit 1 GiB. `docker compose stop qdrant` keeps the data; never `down --volumes` casually.

## Embedding model

`BAAI/bge-m3` at revision `5617a9f61b028005a4858fdac845db406aefb181`, run on CPU with torch from PyTorch's CPU index. Filterable payload: doc_type, sku, category, kind, color, material, sizes, price_vnd, in_stock, file, clause. `keyword` is not a filter (DECISIONS R14). The first use downloads missing files into `~/.cache/huggingface` (about 2.2 GB); later loads read the cache only.

## Build the index

```bash
uv run python -m scripts.build_index            # drift check, documents, embed changed ones, manifest
uv run python -m scripts.build_index --rebuild  # drop and recreate the shopmate_vn collection
```

Needs Saleor running (seeder token for the drift check, runtime token for products) and Qdrant. Results on 2026-10-09:

| Run | Embedded | Skipped | Time | Peak RSS |
|---|---|---|---|---|
| first | 269 (250 products + 19 policy clauses) | 0 | 101.8 s (96.0 s embedding) | 2.16 GiB |
| second | 0 | 269 | 1.6 s (model not loaded) | 0.10 GiB |

The manifest (`data/index/manifest.json`, committed) was byte-identical after the second run.

## Tests

```bash
uv run pytest                  # unit tests: fast, no services (158)
uv run pytest -m integration   # live: Qdrant + cached model + built index + Saleor (9, about 30 s, about 2.1 GiB)
./scripts/test_all.sh          # both; fails if either fails
```

Phase 3A counts as done only when both are green.

`test_all.sh` exits non-zero and prints `FAIL` for whichever suite fails. This was verified on 2026-10-09 by injecting one failing test from outside the repo, without editing any file: `PYTEST_ADDOPTS="tests /path/outside/forced_test_dir" ./scripts/test_all.sh`. With a failing unit test it printed `unit tests: FAIL (1)`, `integration tests: PASS`, exit 1. With a failing `@pytest.mark.integration` test it printed `unit tests: PASS`, `integration tests: FAIL (1)`, exit 1. The real suites still ran in full (159 + 9). The temporary tests were deleted afterwards; a clean run exits 0. The integration suite covers: parity with FlagEmbedding (tolerances in DECISIONS R3), manifest = live index, re-index unchanged, smoke queries in both modes, and the filter property test (200 random filter combinations × dense/hybrid, checked against the catalog file).

### Bench venv (FlagEmbedding reference), kept outside the project

`~/.cache/shopmate-bench/venv` holds FlagEmbedding, which is deliberately not a project dependency. It is only needed to regenerate `tests/fixtures/bge_m3_reference.json` (for example after changing `MODEL_REVISION` in `app/retrieval/embedder.py`). Keep it. If it is lost, rebuild it with the versions the committed fixture was made with (also recorded inside the fixture under `versions`):

```bash
B=~/.cache/shopmate-bench          # on disk; NOT /tmp (tmpfs, uses RAM)
mkdir -p $B && cd $B
uv venv --python 3.12 venv
VIRTUAL_ENV=$B/venv uv pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.14.1"
VIRTUAL_ENV=$B/venv uv pip install "FlagEmbedding==1.4.2" "transformers==5.19.0" huggingface_hub psutil
$B/venv/bin/python -c "import torch, FlagEmbedding; print(torch.__version__)"   # expect 2.14.1+cpu
```

Install torch from the CPU index **first**: otherwise the resolver pulls the CUDA build and triton (several GB).

Regenerate the fixture from the repo root, then run the parity test:

```bash
cd ~/shopmate-workspace/shopmate
~/.cache/shopmate-bench/venv/bin/python tests/fixtures/generate_bge_m3_reference.py
uv run pytest -m integration tests/test_embedder_parity.py
```

The generator pins the model revision itself (it must equal `MODEL_REVISION`), uses `use_fp16=False` on CPU, and writes the library versions into the fixture. If you change the FlagEmbedding version, record it in `docs/DECISIONS-phase3a.md` (R3) together with the measured parity.

## Memory on this machine (2026-10-09, WSL 7.6 GiB)

| Component | RAM |
|---|---|
| Saleor stack (7 containers) | about 2.0–2.7 GiB (the worker varies 1.4–2.0 GiB) |
| Qdrant (269 points) | about 80–92 MiB |
| bge-m3 during indexing / integration tests | peak 2.16–2.19 GiB, about 0.7 GiB at rest after load |
| Available with everything idle | about 4.2 GiB |

`/tmp` in this WSL is a tmpfs, so files written there use RAM. Do not put large venvs or caches there.
