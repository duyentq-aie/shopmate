"""The ONLY way evaluation code may read golden items.

`load_split("dev")` returns dev items. `load_split("test")` raises TestSplitLocked unless the
caller passes `final_evaluation=True`, which is reserved for the single, final evaluation run.
Golden-set tooling (validate, merge, sync) keeps using `schema.load_golden`, because it must see
every item; evaluation code must not import `load_golden` (a test checks the eval scripts).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from eval.golden.schema import GOLDEN_PATH, GoldenItem, load_golden


class TestSplitLocked(Exception):
    """The test split was requested without final_evaluation=True."""

    __test__ = False  # not a pytest test class


def load_split(split: Literal["dev", "test"], *, final_evaluation: bool = False,
               path: Path = GOLDEN_PATH) -> list[GoldenItem]:
    if split not in ("dev", "test"):
        raise ValueError(f"unknown split {split!r}")
    if split == "test" and not final_evaluation:
        raise TestSplitLocked("the test split is locked; it is only read by the final evaluation "
                              "(final_evaluation=True)")
    return [item for item in load_golden(path).items if item.split == split]


def items_hash(items: list[GoldenItem]) -> str:
    """SHA-256 of the canonical JSON of the given items (sorted by id); same scheme as test_split_hash."""
    payload = [i.model_dump(mode="json") for i in sorted(items, key=lambda i: i.id)]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
