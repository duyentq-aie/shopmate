"""Index manifest (data/index/manifest.json, committed): the lineage of every future eval number.

Records the embedding model and revision, vector layout, document counts, source file hashes
and Qdrant versions. Rewritten only when something other than its timestamps would change,
so re-running an unchanged build leaves the committed file untouched.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from app.catalog.schema import CATALOG_PATH
from app.config import REPO_ROOT
from app.policies import POLICY_DOCUMENTS, policy_path
from app.retrieval.embedder import EmbeddingConfig
from app.retrieval.store import COLLECTION, vector_config

MANIFEST_PATH = REPO_ROOT / "data" / "index" / "manifest.json"
TIMESTAMP_FIELDS = ("built_at",)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(config: EmbeddingConfig, counts: dict[str, int], qdrant_server_version: str,
                   drift_check: str, collection: str = COLLECTION) -> dict:
    return {
        "collection": collection,
        "embedding": {**asdict(config), "fingerprint": config.fingerprint()},
        "vectors": vector_config(config),
        "documents": counts,
        "sources": {
            "catalog": {"path": str(CATALOG_PATH.relative_to(REPO_ROOT)), "sha256": file_sha256(CATALOG_PATH)},
            "policies": {
                name: {"path": str(policy_path(name).relative_to(REPO_ROOT)), "sha256": file_sha256(policy_path(name))}
                for name in POLICY_DOCUMENTS
            },
            "products_from": "Saleor channel vn via read-only runtime client (ProductsInChannel)",
            "drift_check": drift_check,
        },
        "versions": {
            "qdrant_server": qdrant_server_version,
            "qdrant_client": version("qdrant-client"),
            "torch": version("torch"),
            "transformers": version("transformers"),
        },
        "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def _without_timestamps(manifest: dict) -> dict:
    return {k: v for k, v in manifest.items() if k not in TIMESTAMP_FIELDS}


def write_manifest(manifest: dict, path: Path = MANIFEST_PATH, force: bool = False) -> bool:
    """Write unless only timestamps differ from the existing file. Returns True if written."""
    if path.is_file() and not force:
        current = json.loads(path.read_text(encoding="utf-8"))
        if _without_timestamps(current) == _without_timestamps(manifest):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
