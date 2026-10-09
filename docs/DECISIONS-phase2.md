# Phase 2 decisions (unattended run, 2026-10-09)

Decisions taken while working without the owner. Rule applied each time: the most conservative option that keeps the agreed design. Each entry: what, options, why.

## Environment

**D1. The Saleor stack was stopped; it was not restarted.**
All seven `saleor-platform-*` containers were `Exited` (about 3 hours before the run; the API answered "connection refused"). Options: start the stack with `docker compose up -d`; work offline. Chosen: offline. Starting containers was not explicitly allowed and changes shared state. Saleor's SDL (`/app/saleor/graphql/schema.graphql`) and a few source files were copied out of the **stopped** API container with `docker cp` (read-only), and every GraphQL document was validated against the SDL with graphql-core. The owner must start the stack before running the seeders.

**D2. Saleor's SDL is committed as a versioned test fixture (revised on the owner's request).**
First decision: not committed, schema tests skipped without `SALEOR_SCHEMA_SDL`. Revised: `tests/fixtures/saleor-schema-3.23.40.graphql`, copied unmodified from the running image (`org.opencontainers.image.version` 3.23.40, sha256 `31353179…a8a31`; byte-identical to the copy taken from the stopped container), with a header naming the source. Saleor's BSD-3-Clause license text sits next to it (`tests/fixtures/SALEOR-LICENSE`), as the license requires. The 28 schema tests now always run, and a missing fixture fails instead of skipping. The README ownership section names this one third-party file.

## Permissions and clients

**D3. The seeder gets `MANAGE_ORDERS_IMPORT`, not `MANAGE_ORDERS`.**
Saleor's `order_bulk_create.py` requires only `MANAGE_ORDERS_IMPORT`. Options: give the seeder `MANAGE_ORDERS` so it can read orders; keep it narrow. Chosen: narrow. Idempotency comes from `orderBulkCreate` itself (a duplicate externalReference returns code `UNIQUE`), and order numbers are read back with the read-only runtime client.

**D4. `report_vn.py` reads the catalog with the seeder token.**
Exact stock (`ProductVariant.stocks`) needs `MANAGE_PRODUCTS`, which the runtime must not have. The script only sends queries, but through the write-capable seeder client, a local-only tool. Orders are read with the runtime read-only client.

**D5. Catalog stock is capped at 50.**
Saleor caps the public `quantityAvailable` at `limit_quantity_per_checkout` (default 50). With stock ≤ 50, what the runtime sees equals the real stock. Options: larger stocks plus a note; cap. Chosen: cap (25 products at 0, the rest 1–50).

**D6. The runtime allowlist has six queries.**
`ChannelBySlug`, `ProductsInChannel`, `VariantBySku`, `OrderByNumber`, `OrderByExternalReference`, `OrdersInChannel`. These are the reads Phase 2 needs (seeding read-back, drift report) plus the obvious product and order lookups. Nothing else is exposed. The order fragment includes `userEmail`, which the future tool layer must not show to an unverified user (identity check is a later-phase concern).

## Orders

**D7. Order fixtures come from a deterministic generator, `scripts/generate_orders.py`.**
Not listed explicitly in the brief, but the golden set needs the fixtures before Saleor exists. Seed 20261010. Numbers start as `pending`; recorded numbers are carried over on regeneration; `--check` ignores numbers.

**D8. Five statuses only: UNCONFIRMED, UNFULFILLED, PARTIALLY_FULFILLED, FULFILLED, CANCELED.**
`orderBulkCreate` validates status against fulfilled quantities (copied into `app/orders/schema.py`). RETURNED, PARTIALLY_RETURNED, EXPIRED and DRAFT were left out: return flows are not modelled by bulk-created fulfillments, and draft/expired orders are not real customer orders. CANCELED and UNCONFIRMED orders carry no fulfillments. Saleor spells it `CANCELED`.

**D9. `stockUpdatePolicy: SKIP`.**
Imported orders do not change stock, so Saleor stock keeps matching `catalog.vi.json` and the drift check stays meaningful. Orders may contain products that are now out of stock (realistic for past orders).

**D10. A row with a `UNIQUE` externalReference error counts as "already exists", even if it has other errors.**
An existing order is never recreated, so other errors on that row (for example a product deleted since) do not matter. A new order that references a missing product fails and is reported.

**D11. Recorded order numbers are never overwritten.**
If `orders.vi.json` has a number and Saleor reports another one for the same externalReference, `seed_orders.py` reports a conflict and exits 1.

**D12. Addresses use i18naddress VN province keys; no postal code, no phone.**
Saleor validates addresses with i18naddress: VN requires street and city, and `countryArea` must be a known key (e.g. "Thành phố Hồ Chí Minh", "Hà Nội"). Phone numbers were omitted because made-up VN mobile numbers could belong to real people. *Not yet executed:* the real validation is verified in step H.

**D13. `productSku` is sent on each order line.**
Otherwise Saleor leaves `productSku` empty for bulk-created lines, and the drift report could not identify a line after its product is deleted.

## Catalog

**D14. Six categories.**
thời trang nam (50), thời trang nữ (50), giày dép (45), túi ví (35), phụ kiện (35), đồ thể thao (35) = 250. SKU format `SM-<NAM|NU|GIAY|TUI|PK|TT>-NNNN`. Names include the color; slugs end with the SKU so they are unique.

**D15. Ownership markers; foreign objects are never reused.**
Attributes and categories carry externalReference `shopmate-attribute-*` / `shopmate-category-*`; the product type and warehouse use `shopmate-` slugs. If an object with the same slug exists without the marker, the seeder stops instead of reusing or modifying it.

**D16. Seeders create or skip; they never update.**
A changed price or stock in the JSON is not pushed to Saleor; `report_vn.py` reports it as drift. Same for shipping-method prices (error, not silent change). Updating is a later decision.

**D17. `--reset-vn` scope.**
Only products with the ShopMate product type, an SKU-shaped externalReference and a listing in `vn`. Interactive confirmation phrase `xoa-san-pham-vn`, or `--confirm` when not interactive. Orders are never touched. Not run.

**D18. Two shipping methods model the free-shipping rule.**
"Giao hàng tiêu chuẩn" 30.000đ with maximum order price 499.999đ, and "Miễn phí vận chuyển" 0đ with minimum 500.000đ. Single source: `app/shop.py`; `tests/test_policies.py` checks that the policy text quotes the same numbers.

**D19. Descriptions are stored as one EditorJS paragraph,** because Saleor's `description` is EditorJS JSON.

## Golden set

**D20. Filters include `kind` and `keyword` in addition to the requested ones.**
Natural questions name a product kind ("áo sơ mi nam") or style ("xếp ly"). Both are applied by code, like color, material, size, price and in_stock.

**D21. Order questions use the placeholder `{order_number}`.**
Numbers do not exist before step H. The schema requires the placeholder exactly when the gold source is an order. `eval/golden/sync_order_numbers.py` fills `order_number` after seeding without touching any other field, and the validator's `--require-order-numbers` turns `pending` into an error.

**D22. One file, `golden.vi.json`, with an `author` field (`agent_draft` | `human`).**
`draft_golden.py` refuses to overwrite it without `--force`, so human edits cannot be lost by accident.

**D23. Split per intent by sha256(id).**
Within each intent, the round(30%) items with the smallest sha256(id) go to test: 41 dev / 19 test (31.7%). The validator accepts 25–35%.

**D24. The test-split hash is not recorded.**
As instructed, it is frozen after the human review and the order-number sync. The validator prints it and can check it with `--expected-test-hash`.

**D25. Two product_search drafts are deliberate no-match questions (gold `no_answer`), and one asks about an out-of-stock product.**
They test that the assistant says "không có" or "hết hàng" instead of inventing a product. For no-match items the validator checks that the filters really select nothing.

## Docs and config

**D26. `.env.example`: the single Phase 1 app-token variable was replaced by `SALEOR_SEEDER_TOKEN` and `SALEOR_RUNTIME_TOKEN`.**
`scripts/check_saleor.py` was later updated (see D30) to read `SALEOR_RUNTIME_TOKEN` from the environment or `.env`; the old variable name is no longer used anywhere.

**D27. Tests use an in-memory fake Saleor (`tests/fake_saleor.py`).**
It proves the scripts' logic (idempotency, scoping, drift detection), not Saleor's behaviour or permissions; that is verified in step H.

## Follow-up session (owner back, 2026-10-09)

**D28. Order lookup must not be IDOR-prone.**
The future `order_status` tool requires the order number AND the customer's email or phone. It returns nothing (no status, items, address or totals) unless both match the same order. Number only: ask for the email or phone. Mismatch, or a question about someone else's order: refuse without revealing anything, including whether the order exists. Not built in Phase 2. `OrderByNumber` in the read-only client is a raw lookup; the tool layer must do the verification and must not expose `OrderByNumber` directly to the model. Encoded in the golden set: `gold_source.order` has `provided_identity` (`matching_email` | `mismatched_email` | `none`) and `expected_behavior` (`answer` | `refuse` | `ask_for_verification`). The schema ties one to the other, and the validator checks that the email in the question matches (or does not match) the fixture's owner. Fixtures have no phone numbers (D12), so drafts use email only.

**D29. Golden set: 4 anti-IDOR drafts appended, existing order items relabelled.**
New items gs-061..gs-064: number + matching email (answer), number + mismatched email (refuse), number only (ask), "show me my friend's order" with the asker's own email (refuse). Ids gs-001..gs-060 are unchanged. Under D28, the 8 original order_status drafts and the 4 order-referencing create_ticket drafts are labelled by what they give: 3 order_status drafts now include the owner's email (answer); the rest give only a number (ask_for_verification). Total 64 drafts: 43 dev / 21 test. Regenerated with `--force`, which is safe only because every item was still an unreviewed agent draft; `--force` now refuses whenever any item is human-authored or reviewed.

**D30. `check_saleor.py` reads the runtime token, scopes counts to one channel, survives a starting API.**
- Token: `SALEOR_RUNTIME_TOKEN` from the environment or `.env` (via `app.config`, like the other scripts); staff email/password only from environment variables, as a fallback.
- The runtime token can modify orders, so every request sent with it first passes `assert_single_query` (the read-only client's parse check). The script's queries are not in the service allowlist: it is a local smoke test, not service code.
- The order count is scoped to `SALEOR_CHANNEL` (addendum 3) instead of all channels. `.env` sets `vn`, so the check fails until `vn` is seeded; use `SALEOR_CHANNEL=default-channel` for the demo channel. The latest-order line no longer prints the customer email.
- Connection-level failures (`URLError`, `ConnectionError` such as `ConnectionResetError` from `getresponse`, `http.client.HTTPException`, `OSError`) become `FAIL: Saleor API not reachable or still starting` with exit 1. `--wait SECONDS` retries with exponential backoff (0.5 s to 5 s).
- The script now needs the uv environment (`uv run python -m scripts.check_saleor`); plain `python3 scripts/check_saleor.py` no longer works.

## Seeding against the real Saleor 3.23.40

**D31. Orders reference Saleor's existing "No taxes" tax class (read only).**
`orderBulkCreate` resolves `shippingTaxClassId` (and the line `taxClassId`) with a helper that raises `NOT_FOUND: Can't return TaxClass instance` when the id is absent, so a tax class is effectively required whenever a shipping method is given. This was the second error of the first H attempt. Options: create a ShopMate tax class (needs `MANAGE_TAXES` for the seeder); reference the demo's "No taxes" class. Chosen: reference it by exact name, read only (the seeder can list tax classes without extra permissions). Prices are tax-inclusive with rate 0. If that class disappears, `seed_orders.py` stops with a clear error.

**D32. Saleor 3.23.40 bug: `orderBulkCreate` cannot resolve variants by `variantSku`.**
In `saleor/graphql/order/bulk_mutations/order_bulk_create.py` the prefetched variants are cached under `ProductVariant.id.<sku>`, but a `variantSku` lookup reads `ProductVariant.sku.<sku>`. Every SKU lookup fails with `ProductVariant instance with sku=... doesn't exist`, even though the variant exists. This was the second H failure. Fix chosen by the owner (option 1): order lines and fulfillment lines use `variantExternalReference`. `seed_catalog.py` sets every variant's externalReference to its SKU, and that cache key is correct. `productSku` is still sent so the line keeps its SKU. The fake Saleor rejects `variantSku` the same way, and a test asserts it is never sent. Re-check when the Saleor image changes.
Also from the first attempt: shipping zones return `ShippingMethodType:<pk>` ids, but `orderBulkCreate` expects `ShippingMethod:<pk>` (same pk, as in Saleor's own tests). `shipping_method_global_id()` converts them.

**D33. Order numbers #24–#83 were consumed by the two rejected H attempts.**
No order was created by either failed run (rejected rows are not saved), but Saleor still advanced its order-number sequence. The 30 fixtures are #84–#113. Numbers are not contiguous, as already seen in Phase 1 (#21, #22).

**D34. An unset `minimumOrderPrice` is stored by Saleor as 0.**
Seen on the second `seed_channel` run, which compared against `None` and stopped with an error. Saleor's state was correct. The comparison now treats an unset minimum as 0, and the fake stores 0 the same way.

**D35. The golden order-number sync is deferred; the golden file is not touched.**
The owner is reviewing `golden.vi.json` and asked that the agent not edit it. `eval/golden/sync_order_numbers.py` (which only fills `gold_source.order_number`) and the strict validator run after the owner has fixed the review labels. `eval/golden/check_query_style.py` is a read-only helper for that review: it suggests `query_style` labels and never writes.
