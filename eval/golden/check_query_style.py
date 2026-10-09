"""READ-ONLY helper: flag golden items whose query_style label may disagree with the text.

Heuristics (suggestions only; a human makes the final call on every item):
- contains teencode / abbreviation tokens (ko, k, dc, đc, sp, j, ship, ...) -> teencode
- otherwise, no Vietnamese diacritics at all                                 -> khong_dau
- otherwise                                                                    -> co_dau
The {order_number} placeholder, email addresses and prices like "400k" are ignored.

The file is read as raw JSON (it does not need to pass the schema) and is never written.

Usage (from the repo root):
    uv run python -m eval.golden.check_query_style [--path FILE] [--all]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

from eval.golden.schema import GOLDEN_PATH

TEENCODE_TOKENS = {
    # the owner's examples
    "ko", "k", "dc", "đc", "sp", "j", "ship",
    # other common abbreviations in Vietnamese chat
    "hk", "hok", "khg", "kh", "z", "vs", "bn", "trc", "r", "e", "a", "ib", "ok", "oke", "h",
    "mn", "ad", "nhiu", "nhìu", "lun", "thik", "cx", "nx", "ntn", "sz",
}
# Placeholder, emails and prices such as "400k" (the "k" there means nghìn, not "không").
_IGNORED = re.compile(r"\{order_number\}|[a-z0-9.]+@example\.com|\b\d+k\b", re.IGNORECASE)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def has_vietnamese_diacritics(text: str) -> bool:
    if "đ" in text.lower():
        return True
    return any(unicodedata.combining(ch) for ch in unicodedata.normalize("NFD", text))


def suggest(question: str) -> tuple[str, str]:
    """Return (suggested label, reason)."""
    text = _IGNORED.sub(" ", question)
    tokens = [t.lower() for t in _WORD.findall(text)]
    hits = sorted({t for t in tokens if t in TEENCODE_TOKENS})
    if hits:
        return "teencode", f"abbreviation tokens: {', '.join(hits)}"
    if tokens and not has_vietnamese_diacritics(text):
        return "khong_dau", "no Vietnamese diacritics"
    return "co_dau", "has diacritics, no abbreviation tokens"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", type=Path, default=GOLDEN_PATH)
    parser.add_argument("--all", action="store_true", help="print every item, not only disagreements")
    args = parser.parse_args(argv)

    items = json.loads(args.path.read_text(encoding="utf-8")).get("items", [])
    flagged = 0
    for item in items:
        label = item.get("query_style")
        suggested, reason = suggest(item.get("question", ""))
        if suggested != label:
            flagged += 1
        if suggested != label or args.all:
            mark = "FLAG" if suggested != label else "ok  "
            print(f"{mark} {item.get('id')}  label={label}  suggested={suggested}  ({reason})")
            print(f"       {item.get('question')}")
    print(f"{flagged} of {len(items)} items flagged (suggestions only; nothing was written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
