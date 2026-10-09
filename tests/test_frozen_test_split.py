"""The test split is FROZEN (2026-10-09): 33 items, recorded in eval/golden/test_split.sha256.

The hash covers every field of every test-split item (canonical JSON, see
eval/golden/schema.test_split_hash). Any change to a test item, moving an item into or out of
the test split, or re-splitting fails this test. Changing it on purpose is a new evaluation
baseline: record why in docs/SETUP.md and re-freeze deliberately.
"""

from __future__ import annotations

from pathlib import Path

from eval.golden.schema import GOLDEN_PATH, load_golden, test_split_hash

FROZEN = Path(GOLDEN_PATH).with_name("test_split.sha256")


def test_test_split_is_unchanged():
    recorded = FROZEN.read_text(encoding="utf-8").strip()
    assert len(recorded) == 64, "eval/golden/test_split.sha256 must hold one SHA-256 hex digest"
    golden = load_golden()
    assert test_split_hash(golden) == recorded, (
        "the frozen test split changed; see tests/test_frozen_test_split.py before re-freezing"
    )
    assert sum(1 for i in golden.items if i.split == "test") == 33
