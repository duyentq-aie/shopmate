"""What the committed manifest says the active index is, for the integration tests.

The manifest (data/index/manifest.json), not a code constant, names the collection that retrieval and
evaluation use. A layout-v1 manifest (Phase 3A/3B) gives the two baseline modes; a layout-v2 manifest
(Phase 4) declares a lexical config and adds the two folded modes. The integration tests are
parametrised from this, so the folded checks appear exactly when the index has the folded vector.
"""

from __future__ import annotations

from app.retrieval.lexical import LexicalConfig
from app.retrieval.manifest import load_manifest

MANIFEST = load_manifest()
ACTIVE = MANIFEST["collection"]
LEXICAL = LexicalConfig() if "lexical" in MANIFEST else None
MODES = ["dense", "hybrid"] + (["dense_sparse_folded", "dense_folded"] if LEXICAL else [])
