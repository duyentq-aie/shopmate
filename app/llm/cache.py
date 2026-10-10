"""Disk cache of LLM calls (Phase 5 free-tier guard): every call is stored; a re-run makes zero API calls.

Key = sha256 of the canonical JSON of {model id, prompt template hash, schema hash, input, generation config}.
The schema hash covers the closed vocabularies (they are enum values of the schema), so a catalog change is a
new key. One file per call, `<root>/<component>/<digest>.json`, holding the key parts, the RAW response, the
latency measured when the call was live and the number of HTTP attempts. Entries are committed under
eval/llm_cache/ so anyone can reproduce an evaluation without an API key. They hold no secret: the key travels in
a request header that is never stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.llm.errors import CacheCorrupt


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CacheKey:
    model: str
    template_sha256: str
    schema_sha256: str
    input_text: str
    generation_config: Mapping

    def as_dict(self) -> dict:
        return {"model": self.model, "template_sha256": self.template_sha256, "schema_sha256": self.schema_sha256,
                "input": self.input_text, "generation_config": dict(self.generation_config)}

    def digest(self) -> str:
        return sha256_text(canonical_json(self.as_dict()))


class DiskCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, component: str, key: CacheKey) -> Path:
        return self.root / component / f"{key.digest()}.json"

    def get(self, component: str, key: CacheKey) -> dict | None:
        path = self._path(component, key)
        if not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheCorrupt(f"{path.name}: unreadable cache entry ({type(exc).__name__})") from None
        if entry.get("digest") != key.digest() or entry.get("key") != json.loads(canonical_json(key.as_dict())):
            raise CacheCorrupt(f"{path.name}: the entry does not belong to its key")
        return entry

    def put(self, component: str, key: CacheKey, response: dict, *, latency_ms: float, http_attempts: int) -> dict:
        path = self._path(component, key)
        if path.exists():
            raise FileExistsError(f"{path} exists: cache entries are never overwritten")
        entry = {"digest": key.digest(), "key": key.as_dict(), "response": response, "latency_ms": latency_ms,
                 "http_attempts": http_attempts,
                 "recorded_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(json.dumps(entry, ensure_ascii=False, indent=1) + "\n")
            os.replace(tmp, path)  # atomic: a crash never leaves a half-written entry under the final name
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return entry

    def entries(self, component: str) -> Iterator[dict]:
        for path in sorted((self.root / component).glob("*.json")):
            yield json.loads(path.read_text(encoding="utf-8"))
