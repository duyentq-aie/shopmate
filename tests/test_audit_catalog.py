"""The read-only catalogue audit (scripts/audit_catalog_consistency.py) and the numbers docs/PHASE8-PREREG.md states from it.

The audit looks for product names that contradict the product's own material or colour. It must read the catalogue and nothing else: no write, no index."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from scripts import audit_catalog_consistency as audit_module
from scripts.audit_catalog_consistency import audit, load_products, main, render

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / "data" / "seed" / "catalog.vi.json"


def product(sku: str, kind: str, style: str, color: str, material: str) -> dict:
    return {"sku": sku, "kind": kind, "name": f"{kind} {style} {color.lower()}".replace("  ", " "), "color": color, "material": material}


def flagged_for(result: dict, label: str) -> dict[str, list[str]]:
    return {where: [f["sku"] for f in items] for where, items in result["material"][label].items()}


# --- the rules, on a small catalogue made for it ---------------------------------------------------------------------------------------------------


def test_a_style_word_that_names_another_material_is_flagged_and_the_matching_one_is_not():
    result = audit([
        product("A", "Áo khoác nam", "jean", "Xanh rêu", "Polyester"),  # a jean jacket that is not denim
        product("B", "Áo khoác nam", "jean", "Đen", "Denim"),  # a jean jacket in denim: fine
        product("C", "Quần short nam", "kaki", "Đen", "Cotton"),
        product("D", "Quần short nam", "kaki", "Be", "Kaki pha spandex"),  # the blend counts as kaki
        product("E", "Áo khoác nam", "nỉ có mũ", "Xám", "Denim"),
    ])
    assert flagged_for(result, "jean / denim") == {"kind": [], "style": ["A"]}
    assert flagged_for(result, "kaki") == {"kind": [], "style": ["C"]}
    assert flagged_for(result, "nỉ (fleece)") == {"kind": [], "style": ["E"]}
    assert result["carrying"]["jean / denim"] == 2 and result["carrying"]["kaki"] == 2
    assert result["flagged"] == ["A", "C", "E"] and result["color"] == {"another colour in the name": [], "its own colour is not in the name": []}


def test_a_word_in_the_kind_name_is_reported_apart_from_a_word_in_the_style():
    result = audit([product("A", "Quần jean nam", "ống đứng", "Đen", "Cotton"), product("B", "Áo khoác nữ", "jean", "Be", "Dù")])
    assert flagged_for(result, "jean / denim") == {"kind": ["A"], "style": ["B"]}


def test_words_are_matched_whole_and_with_their_accents():
    result = audit([
        product("A", "Áo thun nam", "đa năng", "Đen", "Cotton"),  # "đa" is not "da"
        product("B", "Áo thun nam", "dạ", "Đen", "Cotton"),  # "dạ" is not "da"
        product("C", "Balo", "du lịch", "Đen", "Vải canvas"),  # "du lịch" is travel, not "dù"
        product("D", "Ví da nam", "gập đôi", "Đen", "Da bò"),  # the word "da" and leather: fine
        product("E", "Ví da nam", "gập đôi", "Nâu", "Vải canvas"),  # the word "da" and canvas: flagged, in the kind
        product("F", "Áo len nữ", "cổ tròn", "Be", "Len pha cotton"),
        product("G", "Áo thun nam", "cổ tim", "Tím", "Cotton"),  # "tim" (a neckline) is not "tím" (purple); its own colour is in the name
    ])
    assert flagged_for(result, "da (leather)") == {"kind": ["E"], "style": []} and result["carrying"]["da (leather)"] == 2
    assert result["carrying"]["dù (parachute nylon)"] == 0 and result["carrying"]["len (wool knit)"] == 1 and flagged_for(result, "len (wool knit)") == {"kind": [], "style": []}
    assert result["flagged"] == ["E"]


def test_colours_are_matched_longest_first_and_a_wrong_or_missing_colour_is_flagged():
    products = [
        product("A", "Áo khoác nam", "bomber", "Xanh navy", "Polyester"),  # "xanh navy" is one colour, not "xanh" + "navy"
        product("B", "Áo khoác nam", "phối đen", "Xanh rêu", "Polyester"),  # another colour, "đen", in the style
        product("C", "Áo khoác nam", "bomber", "Đỏ đô", "Polyester"),  # "đỏ đô" is read before "đỏ"
        {"sku": "D", "kind": "Mũ", "name": "Mũ lưỡi trai", "color": "Vàng", "material": "Cotton"},  # its own colour is not in the name
        product("Y", "Mũ", "", "Đỏ", "Cotton"),  # "đỏ" is a colour of this catalogue too: it must not be found inside "đỏ đô"
    ]
    result = audit(products + [{"sku": "X", "kind": "Mũ", "name": "Mũ đen", "color": "Đen", "material": "Cotton"}])
    assert [f["sku"] for f in result["color"]["another colour in the name"]] == ["B"] and result["color"]["another colour in the name"][0]["found"] == ["đen"]
    assert [f["sku"] for f in result["color"]["its own colour is not in the name"]] == ["D"]
    assert result["flagged"] == ["B", "D"]


# --- the real catalogue ----------------------------------------------------------------------------------------------------------------------------


def test_the_audit_only_reads_the_catalogue_it_changes_no_file(tmp_path, capsys, monkeypatch):
    before = hashlib.sha256(CATALOG.read_bytes()).hexdigest()
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0 and main(["--json"]) == 0
    assert hashlib.sha256(CATALOG.read_bytes()).hexdigest() == before
    assert list(tmp_path.rglob("*")) == []  # nothing written next to it either
    out = capsys.readouterr().out
    assert "Catalogue audit (read only): 250 products" in out and json.loads(out[out.index("{\n"):])["products"] == 250
    source = Path(audit_module.__file__).read_text(encoding="utf-8")
    assert not re.search(r"write_text|write_bytes|open\([^)]*[\"']w|qdrant|requests|httpx|subprocess", source)  # no write, no index, no network


def test_the_real_catalogue_has_eight_products_whose_style_word_names_a_material_they_do_not_have_and_no_colour_clash():
    result = audit(load_products())
    by_rule = {label: sum(len(v) for v in where.values()) for label, where in result["material"].items()}
    assert {k: v for k, v in by_rule.items() if v} == {"jean / denim": 4, "nỉ (fleece)": 2, "kaki": 2}
    assert result["flagged"] == ["SM-NAM-0014", "SM-NAM-0021", "SM-NAM-0028", "SM-NAM-0041", "SM-NAM-0042", "SM-NAM-0048", "SM-NU-0027", "SM-NU-0046"]
    assert all(not found["kind"] for found in result["material"].values())  # every one of them is a style word, never the kind of the garment
    assert {label: len(items) for label, items in result["color"].items()} == {"another colour in the name": 0, "its own colour is not in the name": 0}
    assert result["carrying"]["da (leather)"] == 7 and result["carrying"]["len (wool knit)"] == 7  # these were looked at, and agree: 0 is a finding, not a rule that never matched
    assert "SKUs: " + ", ".join(result["flagged"]) in render(result)


def test_the_pre_registration_states_the_numbers_the_audit_prints():
    document = REPO / "docs" / "PHASE8-PREREG.md"
    assert document.is_file()
    text = document.read_text(encoding="utf-8")
    result = audit(load_products())
    assert f"Products with at least one contradiction: {len(result['flagged'])} of {result['products']}" in text
    assert "SKUs: " + ", ".join(result["flagged"]) in text
    for label, where in result["material"].items():
        n = sum(len(v) for v in where.values())
        if n:
            assert f"| {label} | {result['carrying'][label]} | {n} |" in text, label
