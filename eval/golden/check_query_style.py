"""READ-ONLY helper: flag golden items whose query_style label may disagree with the text.

Implements eval/golden/LABELING.md, rules applied in order (first match wins):
1. khong_dau - most words that need a Vietnamese diacritic are written without one
               (even if the question also has abbreviations);
2. teencode  - otherwise, contains a chat abbreviation (ko, k, dc/đc, sp, j, bn, mn, nhìu, e, ...);
3. co_dau    - otherwise.
Not counted as words: loanwords (ship, freeship, size, sale, cute, COD, ...), prices like
400k, the {order_number} placeholder, emails. A few missing diacritics or typos do not
make a question khong_dau.

Approximation for rule 1 (there is no dictionary here): an accented word needed its
diacritic; an unaccented word is counted as "missing a diacritic" unless it is in
CORRECT_WITHOUT_DIACRITICS (common words that are spelled right without marks).
Rule 1 holds when missing > accented. Suggestions only: a human decides every label.

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

# LABELING.md list, plus a few equally unambiguous chat forms.
ABBREVIATIONS = {
    "ko", "k", "dc", "đc", "sp", "j", "bn", "mn", "nhìu", "e",
    "hk", "hok", "nhiu", "vs", "trc", "cx", "nx", "ntn", "r", "z",
}
LOANWORDS = {
    "ship", "freeship", "size", "sale", "cute", "cod", "free", "shop", "check", "email",
    "jean", "gym", "sneaker", "polo", "kaki", "croptop", "oversize", "bucket", "laptop", "inch",
    "logo", "iphone", "ok", "hihi", "s", "m", "l", "xl", "xxl",
}
# Spelled correctly without diacritics, so an unaccented occurrence is not "missing" one.
CORRECT_WITHOUT_DIACRITICS = {
    "nam", "xanh", "thun", "bao", "keo", "bong", "sau", "hai", "ba", "mua", "cho", "nha",
    "em", "anh", "hay",
}  # deliberately small: words like giay/tim/con/la in shop questions usually lack a diacritic
# Placeholder, emails and prices such as "400k" (the "k" there means nghìn, not "không").
_IGNORED = re.compile(r"\{order_number\}|[a-z0-9.]+@example\.com|\b\d+k\b", re.IGNORECASE)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def has_vietnamese_diacritics(text: str) -> bool:
    if "đ" in text.lower():
        return True
    return any(unicodedata.combining(ch) for ch in unicodedata.normalize("NFD", text))


def suggest(question: str) -> tuple[str, str]:
    """Return (suggested label, reason) following LABELING.md's rule order."""
    words = [w.lower() for w in _WORD.findall(_IGNORED.sub(" ", question))]
    abbreviations = sorted({w for w in words if w in ABBREVIATIONS})
    vietnamese = [w for w in words if w not in ABBREVIATIONS and w not in LOANWORDS]
    accented = [w for w in vietnamese if has_vietnamese_diacritics(w)]
    missing = [w for w in vietnamese if not has_vietnamese_diacritics(w) and w not in CORRECT_WITHOUT_DIACRITICS]

    if missing and len(missing) > len(accented):
        return "khong_dau", f"rule 1: {len(missing)} words missing diacritics vs {len(accented)} accented ({', '.join(missing)})"
    if abbreviations:
        return "teencode", f"rule 2: abbreviations {', '.join(abbreviations)}"
    note = f"; few missing diacritics/typos ({', '.join(missing)}) do not count" if missing else ""
    return "co_dau", f"rule 3: diacritics, no abbreviations{note}"


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
