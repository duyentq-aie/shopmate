"""Build (or update) the Qdrant index for channel `vn`: products + policy clauses.

Layout v2 (Phase 4): collection `shopmate_vn_v2` holds three named vectors per point: bge-m3
`dense` and `sparse`, plus the diacritic-folded lexical vector `folded`. Qdrant cannot add a vector
to an existing collection, so v2 is a new collection; the Phase 3A/3B collection `shopmate_vn` is
frozen (this script refuses to target it) so every Phase 3B configuration stays reproducible.

Steps:
1. Drift check: Saleor channel vn must match data/seed/catalog.vi.json. Reuses
   scripts/report_vn.py's logic (fetch_products + diff_catalog, seeder token, read only).
   Any difference stops the build, so the catalog hash in the manifest means something.
2. Documents: products through the read-only runtime client, policies from data/policies/.
3. Index: idempotent upsert with deterministic ids; documents whose content and fingerprints
   are unchanged are skipped (the model is not loaded if nothing needs embedding); with
   --seed-from, dense and sparse vectors are COPIED from the earlier collection (verified by a
   digest) and only the folded vector is computed; points of vanished documents are deleted.
4. Manifest: data/index/manifest.json, rewritten only when something besides timestamps changed.
   It names the collection, records the lexical config, the digest of the dense/sparse vectors
   and the lineage (`derived_from`).

Usage (from the repo root; Qdrant running: `docker compose up -d qdrant`):
    uv run python -m scripts.build_index --seed-from shopmate_vn   # first v2 build: no model run
    uv run python -m scripts.build_index                           # later: update shopmate_vn_v2
    uv run python -m scripts.build_index --rebuild                 # drop and recreate shopmate_vn_v2
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.catalog.schema import load_catalog
from app.config import api_url, require_setting
from app.retrieval.documents import Document, fetch_product_documents, policy_documents
from app.retrieval.embedder import BgeM3Embedder, EmbeddingConfig
from app.retrieval.indexer import IndexStats, index_documents
from app.retrieval.lexical import LexicalConfig
from app.retrieval.manifest import MANIFEST_PATH, build_manifest, write_manifest
from app.retrieval.store import (
    COLLECTION,
    COLLECTION_V2,
    connect,
    count_by_doc_type,
    ensure_collection,
    vectors_digest,
)
from app.saleor.readonly_client import ReadOnlySaleorClient
from scripts import report_vn
from scripts.seedlib.client import seeder_from_env

FROZEN_COLLECTIONS = (COLLECTION,)  # the Phase 3A/3B index: never a build target


class IndexCountMismatch(Exception):
    """After indexing, the collection does not hold exactly the documents."""


@dataclass
class BuildReport:
    stats: IndexStats
    counts: dict[str, int]
    manifest: dict
    manifest_written: bool
    created: bool


def drift_check() -> list[str]:
    with seeder_from_env() as seeder:
        _, nodes = report_vn.fetch_products(seeder)
    return report_vn.diff_catalog(load_catalog(), nodes).mismatches


def run_build(client, documents: list[Document], config: EmbeddingConfig, lexical: LexicalConfig, collection: str,
              embedder_factory: Callable, *, seed_from: str | None, recreate: bool, qdrant_version: str,
              drift_check: str, manifest_path: Path = MANIFEST_PATH) -> BuildReport:
    """Create/update the layout-v2 collection from `documents` and write its manifest."""
    created = ensure_collection(client, config, collection, recreate=recreate, lexical=True)
    stats = index_documents(client, documents, config, embedder_factory, collection=collection, lexical=lexical,
                            seed_from=seed_from)
    counts = count_by_doc_type(client, collection)
    products = sum(d.payload.get("doc_type") == "product" for d in documents)
    expected = {"product": products, "policy": len(documents) - products, "total": len(documents)}
    if counts != expected:
        raise IndexCountMismatch(f"collection counts {counts} != documents {expected}")
    derived_from = None
    if seed_from is not None:
        derived_from = {"collection": seed_from, "vectors_digest": vectors_digest(client, seed_from)}
    elif manifest_path.is_file():  # keep the recorded lineage when this run did not seed
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("collection") == collection:
            derived_from = previous.get("derived_from")
    manifest = build_manifest(config, counts, qdrant_version, drift_check, collection=collection, lexical=lexical,
                              derived_from=derived_from, vectors_digest=vectors_digest(client, collection))
    changed = bool(stats.embedded or stats.payload_updated or stats.deleted or stats.lexical_computed or stats.copied
                   or created)
    return BuildReport(stats, counts, manifest, write_manifest(manifest, manifest_path, force=changed), created)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--collection", default=COLLECTION_V2, help=f"target collection (default {COLLECTION_V2})")
    parser.add_argument("--seed-from", default=None, metavar="COLLECTION",
                        help="copy dense and sparse vectors from this earlier collection instead of embedding")
    parser.add_argument("--rebuild", action="store_true", help="drop and recreate the target collection")
    args = parser.parse_args(argv)
    if args.collection in FROZEN_COLLECTIONS:
        print(f"REFUSED: {args.collection} is the frozen Phase 3A/3B index; build {COLLECTION_V2} instead",
              file=sys.stderr)
        return 1
    if args.seed_from == args.collection:
        print("REFUSED: --seed-from must differ from the target collection", file=sys.stderr)
        return 1
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

    config, lexical = EmbeddingConfig(), LexicalConfig()
    client = connect()
    model_loads = []

    def embedder_factory():
        model_loads.append(1)
        return BgeM3Embedder(config)

    report = run_build(client, documents, config, lexical, args.collection, embedder_factory, seed_from=args.seed_from,
                       recreate=args.rebuild, qdrant_version=client.info().version, drift_check="passed")
    stats = report.stats
    if report.created:
        print(f"collection {args.collection} {'recreated' if args.rebuild else 'created'}")
    print(f"index: {stats.copied} dense+sparse copied from {args.seed_from}, {stats.embedded} embedded by bge-m3, "
          f"{stats.lexical_computed} folded vectors computed, {stats.payload_updated} payload-only updates, "
          f"{stats.skipped} unchanged (skipped), {stats.deleted} deleted; bge-m3 model loaded: "
          f"{'yes' if model_loads else 'no'}")
    if stats.seed_digest:
        print(f"seed check: dense+sparse digest of the {stats.copied} copied points is identical in "
              f"{args.seed_from} and {args.collection} ({stats.seed_digest[:16]}…)")
    counts = report.counts
    print(f"manifest: {'written' if report.manifest_written else 'unchanged'} ({counts['total']} points: "
          f"{counts['product']} products, {counts['policy']} policy clauses; lexical fingerprint "
          f"{lexical.fingerprint()}; vectors digest {report.manifest['vectors_digest'][:16]}…)")
    peak_gib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20
    print(f"done in {time.monotonic() - started:.1f}s, peak RSS {peak_gib:.2f} GiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
