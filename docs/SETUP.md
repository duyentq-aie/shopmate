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

Credentials go through the environment only; nothing is written to a file. Exit codes: 0 ok, 1 a check failed, 2 missing credentials. Output after the test order:

```
shop name      : Saleor e-commerce
channel slug   : default-channel  (available: channel-pln, default-channel)
product count  : 29  (channel default-channel)
order count    : 21  (all channels)
latest order   : #23 created 2026-10-09T05:29:52.377076+00:00 by shopmate-test@example.com in default-channel
OK: all checks passed
```

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
