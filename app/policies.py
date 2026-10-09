"""Access to the policy documents in data/policies/ and their numbered clauses."""

from __future__ import annotations

import re
from pathlib import Path

from app.config import REPO_ROOT

POLICY_DIR = REPO_ROOT / "data" / "policies"
POLICY_DOCUMENTS = ("doi-tra", "van-chuyen", "bao-hanh")

_CLAUSE_HEADING = re.compile(r"^## Điều (\d+)\. (.+)$", re.MULTILINE)


def policy_path(document: str) -> Path:
    if document not in POLICY_DOCUMENTS:
        raise ValueError(f"unknown policy document {document!r}")
    return POLICY_DIR / f"{document}.md"


def clauses(document: str) -> dict[int, str]:
    """Map clause number -> clause title, e.g. {1: 'Phạm vi áp dụng', ...}."""
    text = policy_path(document).read_text(encoding="utf-8")
    return {int(number): title.strip() for number, title in _CLAUSE_HEADING.findall(text)}
