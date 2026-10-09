"""Diacritic folding for the folded lexical signal (Phase 4, experiment 1).

fold(text): lowercase, Unicode NFD, strip combining marks, đ -> d. The SAME function is applied to
documents (when the folded vector is built) and to queries, and to the perturbation set; changing it
means bumping FOLDING_VERSION, which changes the lexical fingerprint and forces a re-index.

Deliberately NOT done: teencode expansion ("ko", "dc", "mn" stay as typed), typo repair, word
segmentation. Folding merges words that differ only by diacritics ("phí" and "phi công" both contain
the token "phi"); measuring what that costs is the point of the experiment.
"""

from __future__ import annotations

import re
import unicodedata

FOLDING_VERSION = 1
_TOKEN = re.compile(r"[a-z0-9]+")


def fold(text: str) -> str:
    # Lowercase FIRST: some capitals lowercase into letter + combining mark ("İ" -> "i" + U+0307),
    # and the mark must still be stripped below. đ has no decomposition, so it is mapped explicitly.
    decomposed = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def tokenize(text: str) -> list[str]:
    """Folded alphanumeric runs: Vietnamese syllables, numbers and things like "400k"."""
    return _TOKEN.findall(fold(text))


def has_diacritics(text: str) -> bool:
    """True if the text contains any Vietnamese-style diacritic (a combining mark or đ/Đ)."""
    return any(unicodedata.category(ch) == "Mn" or ch in "đĐ" for ch in unicodedata.normalize("NFD", text))
