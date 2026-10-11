# Putting the chat widget on the storefront

The widget (`widget/shopmate-chat.js`, a `<shopmate-chat>` web component) and the chat API that feeds it (`app/chat_api.py`) are part of this repo. The storefront
(`../storefront`, Saleor Paper) is **not modified by anything in this repo**: the change it needs is written as a patch, `docs/storefront-integration.patch`, that you
apply yourself. Nothing here has applied it.

## What the patch does

One file, `src/app/(storefront)/[locale]/layout.tsx` (the root layout of every storefront page): it imports `next/script` and adds, after `<AnalyticsMount />`,

```tsx
{process.env.NEXT_PUBLIC_SHOPMATE_WIDGET_URL && (
    <Script src={process.env.NEXT_PUBLIC_SHOPMATE_WIDGET_URL} data-api-base={process.env.NEXT_PUBLIC_SHOPMATE_API_BASE ?? ""} strategy="afterInteractive" />
)}
```

The widget script sees `data-api-base`, mounts `<shopmate-chat>` on its own and talks to that API. **With the variable unset nothing is rendered**, so the patch is safe to leave
applied. It was checked with `git apply --check`, then applied and reverted, on a scratch copy of the layout file (the real file was only read).

## Apply, use, revert

```bash
# 1. apply (from the shopmate repo; it only touches the one layout file)
git -C ../storefront apply --check ../shopmate/docs/storefront-integration.patch   # dry run
git -C ../storefront apply ../shopmate/docs/storefront-integration.patch

# 2. tell the storefront where the widget is: put these two lines in ../storefront/.env.local (or .env) yourself, then restart `pnpm dev`
#    NEXT_PUBLIC_SHOPMATE_WIDGET_URL=http://127.0.0.1:8766/widget/shopmate-chat.js
#    NEXT_PUBLIC_SHOPMATE_API_BASE=http://127.0.0.1:8766

# 3. start the chat API (in the shopmate repo)
uv run python -m app.chat_api --live        # real agent: Gemini free tier (guarded), Qdrant, read-only Saleor
uv run python -m app.chat_api --fake        # canned answers, nothing else needed
# the demo page without the storefront: http://127.0.0.1:8766/widget/demo

# 4. revert
git -C ../storefront apply -R ../shopmate/docs/storefront-integration.patch
```

## What it assumes (read this before it fails)

| Assumption | Where it comes from | If it is wrong |
|---|---|---|
| The storefront dev server is `http://localhost:3000` | `next dev` default port; `NEXT_PUBLIC_STOREFRONT_URL=http://localhost:3000` in `../storefront/.env.example`. It was not running when this was written, so it was not tried | `SHOPMATE_STOREFRONT_ORIGIN=http://localhost:PORT` for the chat API (the only origin allowed by CORS; any other `Origin` gets 403) |
| A product page is `/{locale}/{channel}/products/{slug}`, here `/en/vn/products/{slug}` | the route folder `src/app/(storefront)/[locale]/[channel]/(main)/products/[slug]`, locales `en pl de fr fi nb ja ko` (no `vi`), channel `vn` | `SHOPMATE_STOREFRONT_PRODUCT_URL=http://localhost:3000/en/vn/products/{slug}` (the product cards link there; the slug of a SKU comes from `data/seed/catalog.vi.json`, the index has none) |
| The browser may call `http://127.0.0.1:8766` from the storefront page | the API binds loopback only | it works only on the machine that runs both; see below for anything else |

## Content-Security-Policy and other things that can block it

The storefront was read for this (`next.config.js`, `src/middleware.ts`): **it sets no `Content-Security-Policy`**, so the script and the calls to `127.0.0.1:8766` are not blocked
by the storefront itself. If you deploy it behind something that adds a CSP (Vercel headers, a reverse proxy, a security plugin), that policy must allow the widget:

```
script-src  ... http://127.0.0.1:8766        # the widget file
connect-src ... http://127.0.0.1:8766        # POST /api/chat and /api/chat/confirm
```

The widget styles itself with a constructable stylesheet inside its shadow root (a `<style>` element only as a fallback), so a strict `style-src` is not a problem for it.

Other limits:
- **Loopback only.** The chat API has no authentication and refuses to bind anything but `127.0.0.1`. A storefront on another machine, or one served over HTTPS from a real host, cannot
  use it as it is: that needs the API behind an HTTPS reverse proxy that authenticates callers and a CORS origin set to the real storefront. Not done, on purpose
  (docs/DECISIONS-phase7.md, D7.16).
- An `https://` storefront page calling `http://127.0.0.1` is allowed by browsers (loopback counts as potentially trustworthy), but a CSP `connect-src` still has to list it.
- The widget keeps only a session id in `sessionStorage`; a reload keeps the agent's memory of the conversation (on the API side, in memory) but not the messages on the screen.
- The chat API is rate limited to 6 messages a minute per session; the Gemini free tier is guarded by the same counters as the evaluation (450 requests a day, shared).

## The look, and the font

The widget is drawn in the ShopMate indigo (`#1F2A5C`) on a cool white, with the product cards' thumbnails drawn by the widget itself (`ShopMateChat.thumbnail`: a silhouette by product kind, the product's own colour, a weave by material), so a card costs no image request. It asks for the font **Be Vietnam Pro** and falls back to the system font when the page does not load it. The Saleor Paper storefront does not load it, and the patch does not add it (that would touch more storefront files): without it the widget looks the same in `system-ui`. To get the font, load it on the page, for example `<link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;500;600;700&display=swap" rel="stylesheet">` (then a CSP needs `style-src https://fonts.googleapis.com` and `font-src https://fonts.gstatic.com`; the demo page does exactly this). The widget's own attributes: `api-base`, `title` (default "Trợ lý ShopMate"), `open` (start with the panel open); methods `open()`, `close()` and `ask(text)`.

## Files

- `widget/shopmate-chat.js`: the component. `widget/demo.html`, `widget/demo.js`, `widget/demo-products.json`: a fake shop page ("Trang demo", ShopMate Fashion) that embeds it; the product list is built from the real catalogue by `scripts/build_demo_products.py`.
- `app/chat_api.py`: the API (`--fake` / `--live`), `tests/test_chat_api.py`: its tests; `tests/test_widget.py`: the thumbnail, the demo products and the design rules (offline).
- `docs/storefront-integration.patch`: the storefront change. This file: how to use it.
