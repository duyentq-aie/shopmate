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


_DOC_TITLE = re.compile(r"^# (.+)$", re.MULTILINE)


def document_title(document: str) -> str:
    match = _DOC_TITLE.search(policy_path(document).read_text(encoding="utf-8"))
    return match.group(1).strip() if match else document


def clause_bodies(document: str) -> dict[int, tuple[str, str]]:
    """Map clause number -> (title, body text up to the next clause heading)."""
    text = policy_path(document).read_text(encoding="utf-8")
    matches = list(_CLAUSE_HEADING.finditer(text))
    result = {}
    for match, nxt in zip(matches, [*matches[1:], None]):
        body = text[match.end() : nxt.start() if nxt else len(text)].strip()
        result[int(match.group(1))] = (match.group(2).strip(), body)
    return result
