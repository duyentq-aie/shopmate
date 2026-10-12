"""Audit data/seed/catalog.vi.json: product names that contradict the product's own `material` or `color` attribute. READ ONLY.

The catalogue is synthetic and was generated from templates: a name is `kind + style + color` (scripts/generate_catalog.py), and the material is drawn on its
own from the materials of the kind. A style word can therefore name a material the product does not have ("Áo khoác nam jean xanh rêu", material Polyester). The
description is built from the name and the material ("... làm từ chất liệu polyester"), so the contradiction is in the indexed text too.

This script only reads the catalogue file: it writes nothing, opens no index and calls no service (the index and the catalogue are not touched; fixing them
would change the index, see docs/PHASE8-PREREG.md). Run it any time; the numbers only change if the catalogue file changes.

What is checked
  material  a whole word (or two-word phrase) of the name that names a material, against the `material` attribute. Words are matched exactly, with their accents
            (da is leather, but dạ and đa are other words; dù is the fabric, du lịch is travel; tím is purple, "cổ tim" is a neckline). Where the word stands is
            reported as well: in the *kind* ("Quần jean nam": what the garment is called) or in the *style* ("Áo khoác nam jean xanh rêu": a modifier).
            The first seven rules are the ones asked for; the others are the same check for the remaining material words that occur in names.
  color     (a) the name has a colour word that is not the product's `color`; (b) the name does not have the product's own `color`.
            Colour phrases are matched longest first ("xanh navy" before "xanh"), accents exact.
  Not checked, on purpose: "thun" (it names a garment, "áo thun", not only a fabric), and descriptions (they repeat the name and the material).

Usage (from the repo root):
    uv run python -m scripts.audit_catalog_consistency            # the report
    uv run python -m scripts.audit_catalog_consistency --json     # the same as JSON
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.config import REPO_ROOT

CATALOG_PATH = REPO_ROOT / "data" / "seed" / "catalog.vi.json"


@dataclass(frozen=True)
class MaterialRule:
    """`words` is the word or phrase in the name; `accepts` says which `material` values it agrees with; `asked` marks the rules of the original request."""

    label: str
    words: tuple[str, ...]
    accepts: frozenset[str]
    asked: bool = True

    def agrees(self, material: str) -> bool:
        return material in self.accepts


def _prefix(*materials: str) -> frozenset[str]:
    return frozenset(materials)


MATERIAL_RULES = (
    MaterialRule("jean / denim", ("jean",), _prefix("Denim")),
    MaterialRule("denim", ("denim",), _prefix("Denim")),
    MaterialRule("da (leather)", ("da",), _prefix("Da bò", "Da tổng hợp", "Da lộn")),
    MaterialRule("linen", ("linen",), _prefix("Linen")),
    MaterialRule("nỉ (fleece)", ("nỉ",), _prefix("Nỉ")),
    MaterialRule("dù (parachute nylon)", ("dù",), _prefix("Dù")),
    MaterialRule("kaki", ("kaki",), _prefix("Kaki", "Kaki pha spandex")),
    MaterialRule("len (wool knit)", ("len",), _prefix("Len", "Len pha cotton")),
    MaterialRule("lụa (silk)", ("lụa",), _prefix("Lụa"), asked=False),
    MaterialRule("voan (chiffon)", ("voan",), _prefix("Voan"), asked=False),
    MaterialRule("tweed", ("tweed",), _prefix("Tweed"), asked=False),
    MaterialRule("kate", ("kate",), _prefix("Kate"), asked=False),
    MaterialRule("canvas", ("canvas",), _prefix("Vải canvas"), asked=False),
    MaterialRule("lưới (mesh)", ("lưới",), _prefix("Vải lưới"), asked=False),
    MaterialRule("thun lạnh", ("thun", "lạnh"), _prefix("Thun lạnh"), asked=False),
    MaterialRule("cotton", ("cotton",), _prefix("Cotton", "Cotton cá sấu", "Cotton pha spandex", "Len pha cotton"), asked=False),
    MaterialRule("polyester", ("polyester",), _prefix("Polyester", "Polyester pha spandex"), asked=False),
    MaterialRule("bamboo", ("bamboo",), _prefix("Bamboo"), asked=False),
    MaterialRule("acetate", ("acetate",), _prefix("Nhựa acetate"), asked=False),
    MaterialRule("kim loại (metal)", ("kim", "loại"), _prefix("Kim loại"), asked=False),
    MaterialRule("cao su (rubber)", ("cao", "su"), _prefix("Cao su"), asked=False),
)

WORD = re.compile(r"[^\W\d_]+")


def words(text: str) -> list[str]:
    """Lower-case words of `text`, accents kept (NFC), digits and punctuation dropped."""
    return WORD.findall(unicodedata.normalize("NFC", text).lower())


def find_phrase(tokens: list[str], phrase: tuple[str, ...]) -> list[int]:
    n = len(phrase)
    return [i for i in range(len(tokens) - n + 1) if tuple(tokens[i:i + n]) == phrase]


def split_name(product: dict) -> tuple[list[str], list[str], list[str]]:
    """(kind words, style words, trailing colour words) of the name. The generator writes `kind + style + colour`; if a name does not follow that, everything is style."""
    name, kind, color = words(product["name"]), words(product["kind"]), words(product["color"])
    if name[:len(kind)] != kind:
        return [], name, []
    rest = name[len(kind):]
    if color and rest[-len(color):] == color:
        return kind, rest[:-len(color)], color
    return kind, rest, []


def colour_phrases(colors: list[str]) -> list[tuple[str, ...]]:
    """The colour names of the catalogue as word tuples, longest first, so "xanh navy" is read before "xanh" and "đỏ đô" before "đỏ"."""
    return sorted({tuple(words(c)) for c in colors}, key=lambda p: (-len(p), p))


def colours_in(tokens: list[str], phrases: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    found, taken = [], set()
    for phrase in phrases:
        for i in find_phrase(tokens, phrase):
            span = set(range(i, i + len(phrase)))
            if not span & taken:
                taken |= span
                found.append(phrase)
    return found


def audit(products: list[dict]) -> dict:
    """The findings: {"products": n, "carrying": {rule label: n}, "material": {rule label: {"kind": [...], "style": [...]}}, "color": {...}, "flagged": [sku, ...]}.
    `carrying` is how many names have the word at all, so a 0 below is "checked, nothing wrong" and not "the rule never matched". Lists hold dicts with the SKU and why."""
    phrases = colour_phrases(sorted({p["color"] for p in products}))
    material: dict[str, dict[str, list[dict]]] = {r.label: {"kind": [], "style": []} for r in MATERIAL_RULES}
    carrying = {r.label: 0 for r in MATERIAL_RULES}
    color: dict[str, list[dict]] = {"another colour in the name": [], "its own colour is not in the name": []}
    for p in products:
        kind, style, tail = split_name(p)
        for rule in MATERIAL_RULES:
            carrying[rule.label] += bool(find_phrase(words(p["name"]), rule.words))
            for where, tokens in (("kind", kind), ("style", style)):
                if find_phrase(tokens, rule.words) and not rule.agrees(p["material"]):
                    material[rule.label][where].append({"sku": p["sku"], "name": p["name"], "material": p["material"], "color": p["color"]})
        name_tokens = words(p["name"])
        found = colours_in(name_tokens, phrases)
        own = tuple(words(p["color"]))
        others = [" ".join(f) for f in found if f != own]
        if others:
            color["another colour in the name"].append({"sku": p["sku"], "name": p["name"], "color": p["color"], "found": others})
        if own not in found:
            color["its own colour is not in the name"].append({"sku": p["sku"], "name": p["name"], "color": p["color"], "found": [" ".join(f) for f in found]})
    flagged = sorted({f["sku"] for where in material.values() for items in where.values() for f in items} | {f["sku"] for items in color.values() for f in items})
    return {"products": len(products), "carrying": carrying, "material": material, "color": color, "flagged": flagged}


def load_products(path: Path = CATALOG_PATH) -> list[dict]:
    return list(json.loads(path.read_text(encoding="utf-8"))["products"])  # read only


def render(result: dict) -> str:
    out = [f"Catalogue audit (read only): {result['products']} products in {CATALOG_PATH.relative_to(REPO_ROOT)}", ""]
    out.append("A. The name says a material that the `material` attribute does not have")
    total = 0
    for asked in (True, False):
        out.append("   " + ("The rules asked for:" if asked else "The same check for the other material words that occur in names:"))
        for rule in MATERIAL_RULES:
            if rule.asked != asked:
                continue
            found = result["material"][rule.label]
            n = len(found["kind"]) + len(found["style"])
            total += n
            out.append(f"   - {rule.label:<22} {n:>3} of {result['carrying'][rule.label]:>3} names with the word   (in the kind name: {len(found['kind'])}, in the style: {len(found['style'])})   agrees with: {', '.join(sorted(rule.accepts))}")
            for where in ("kind", "style"):
                for f in found[where]:
                    out.append(f"       {f['sku']:<13} [{where}] {f['name']}  -> material {f['material']}")
    out.append(f"   material contradictions: {total} (one product can have more than one)")
    out.append("")
    out.append("B. The name and the `color` attribute")
    for label, items in result["color"].items():
        out.append(f"   - {label}: {len(items)}")
        for f in items:
            out.append(f"       {f['sku']:<13} {f['name']}  color {f['color']}  found {', '.join(f['found']) or '-'}")
    out.append("")
    out.append(f"Products with at least one contradiction: {len(result['flagged'])} of {result['products']}")
    out.append("SKUs: " + ", ".join(result["flagged"]))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="print the findings as JSON")
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH, help="the catalogue file to read (default: the seed catalogue)")
    args = parser.parse_args(argv)
    result = audit(load_products(args.catalog))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
