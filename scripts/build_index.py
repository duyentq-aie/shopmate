"""Build (or update) the Qdrant index for channel `vn`: products + policy clauses.

Steps:
1. Drift check: Saleor channel vn must match data/seed/catalog.vi.json. Reuses
   scripts/report_vn.py's logic (fetch_products + diff_catalog, seeder token, read only).
   Any difference stops the build, so the catalog hash in the manifest means something.
2. Documents: products through the read-only runtime client, policies from data/policies/.
3. Index: idempotent upsert with deterministic ids; documents whose content and embedding
   fingerprint are unchanged are skipped (the model is not loaded if nothing changed);
   points of vanished documents are deleted.
4. Manifest: data/index/manifest.json, rewritten only when something besides timestamps changed.

Usage (from the repo root; Qdrant running: `docker compose up -d qdrant`):
    uv run python -m scripts.build_index
    uv run python -m scripts.build_index --rebuild   # drop and recreate the ShopMate collection
"""

from __future__ import annotations

import argparse
import resource
import sys
import time

from app.catalog.schema import load_catalog
from app.config import api_url, require_setting
from app.retrieval.documents import fetch_product_documents, policy_documents
from app.retrieval.embedder import BgeM3Embedder, EmbeddingConfig
from app.retrieval.indexer import index_documents
from app.retrieval.manifest import build_manifest, write_manifest
from app.retrieval.store import COLLECTION, connect, count_by_doc_type, ensure_collection
from app.saleor.readonly_client import ReadOnlySaleorClient
from scripts import report_vn
from scripts.seedlib.client import seeder_from_env


def drift_check() -> list[str]:
    with seeder_from_env() as seeder:
        _, nodes = report_vn.fetch_products(seeder)
    return report_vn.diff_catalog(load_catalog(), nodes).mismatches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rebuild", action="store_true", help=f"drop and recreate collection {COLLECTION}")
    args = parser.parse_args(argv)
    started = time.monotonic()

    mismatches = drift_check()
    if mismatches:
        print(f"REFUSED: Saleor channel vn differs from the catalog file ({len(mismatches)} differences):", file=sys.stderr)
        for line in mismatches[:20]:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("drift check: Saleor channel vn matches data/seed/catalog.vi.json")

    kinds = {p.sku: p.kind for p in load_catalog().products}  # kind is a catalog field, not in Saleor
    with ReadOnlySaleorClient(api_url(), require_setting("SALEOR_RUNTIME_TOKEN")) as runtime:
        products = fetch_product_documents(runtime, kinds)
    policies = policy_documents()
    documents = products + policies
    print(f"documents: {len(products)} products, {len(policies)} policy clauses")

    config = EmbeddingConfig()
    client = connect()
    created = ensure_collection(client, config, recreate=args.rebuild)
    if created:
        print(f"collection {COLLECTION} {'recreated' if args.rebuild else 'created'}")

    embed_started = time.monotonic()
    stats = index_documents(client, documents, config, embedder_factory=lambda: BgeM3Embedder(config))
    embed_seconds = time.monotonic() - embed_started
    print(f"index: {stats.embedded} embedded, {stats.payload_updated} payload-only updates, "
          f"{stats.skipped} unchanged (skipped), {stats.deleted} deleted "
          f"in {embed_seconds:.1f}s")

    counts = count_by_doc_type(client)
    expected = {"product": len(products), "policy": len(policies), "total": len(documents)}
    if counts != expected:
        print(f"ERROR: collection counts {counts} != documents {expected}", file=sys.stderr)
        return 1
    manifest = build_manifest(config, counts, client.info().version, drift_check="passed")
    changed = bool(stats.embedded or stats.payload_updated or stats.deleted or created)
    written = write_manifest(manifest, force=changed)
    print(f"manifest: {'written' if written else 'unchanged'} ({counts['total']} points: "
          f"{counts['product']} products, {counts['policy']} policy clauses)")
    peak_gib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20
    print(f"done in {time.monotonic() - started:.1f}s, peak RSS {peak_gib:.2f} GiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
