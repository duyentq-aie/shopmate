"""The storefront widget and its demo page as a design (widget/): the fabric-swatch thumbnail, the demo product list, the design system.

The thumbnail function is run under Node with a tiny fake DOM (tests/widget_harness.js) and its output is parsed by a real XML parser, so what is checked is the
drawing, not a regular expression over the source. The browser behaviour (layout, carousel, keyboard, typing indicator) was driven in a real Edge and is shown in
docs/screenshots/phase7/; it is not part of this suite. The Node tests are skipped where Node is not installed.
The chat API itself is tested in tests/test_chat_api.py."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.build_demo_products import CHOSEN, OUT_PATH, build, render

REPO = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent / "widget_harness.js"
WIDGET = REPO / "widget" / "shopmate-chat.js"
DEMO_HTML = (REPO / "widget" / "demo.html").read_text(encoding="utf-8")
DEMO_JS = (REPO / "widget" / "demo.js").read_text(encoding="utf-8")
SOURCE = WIDGET.read_text(encoding="utf-8")
CATALOG = json.loads((REPO / "data" / "seed" / "catalog.vi.json").read_text(encoding="utf-8"))["products"]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node is not installed")
SHAPES = {"jacket", "shirt", "tee", "dress", "pants", "shoe", "bag", "accessory"}
SVG = "{http://www.w3.org/2000/svg}"


def js(expression: str):
    result = subprocess.run([NODE, str(HARNESS), str(WIDGET), expression], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def luminance(hex_color: str) -> float:
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def drawings(products: list[dict]) -> list[dict]:
    """The thumbnail of each product, as XML text, with the rules that made it."""
    data = json.dumps([{"kind": p["kind"], "color": p["color"], "material": p["material"], "name": p["name"]} for p in products], ensure_ascii=False)
    return js(f"({data}).map(p => ({{p, shape: api.shapeOf(p.kind), pattern: api.patternOf(p.material), color: api.colorOf(p.color), xml: xml(api.thumbnail(p))}}))")


# --- the fabric swatch ---------------------------------------------------------------------------------------------------------------------------


@needs_node
def test_all_35_kinds_of_the_catalogue_fall_into_at_most_eight_silhouettes_and_the_expected_ones():
    kinds = sorted({p["kind"] for p in CATALOG})
    assert len(kinds) == 35
    shapes = dict(zip(kinds, js(f"{json.dumps(kinds, ensure_ascii=False)}.map(k => api.shapeOf(k))")))
    assert set(shapes.values()) == SHAPES
    expected = {"Áo khoác nam": "jacket", "Áo khoác nữ": "jacket", "Áo sơ mi nam": "shirt", "Áo polo nam": "shirt", "Áo thun nữ": "tee", "Áo len nữ": "tee", "Áo bra thể thao nữ": "tee",
                "Váy liền nữ": "dress", "Chân váy nữ": "dress", "Quần jean nam": "pants", "Quần short thể thao": "pants", "Quần legging nữ": "pants", "Giày cao gót nữ": "shoe",
                "Dép quai ngang": "shoe", "Sandal": "shoe", "Giày chạy bộ": "shoe", "Túi đeo chéo": "bag", "Balo": "bag", "Ví da nam": "bag", "Ví nữ": "bag", "Mũ": "accessory",
                "Kính mát": "accessory", "Tất": "accessory", "Khăn choàng": "accessory", "Thắt lưng nam": "accessory"}
    assert {k: shapes[k] for k in expected} == expected


@needs_node
def test_every_kind_gets_a_well_formed_svg_with_its_silhouette_and_every_colour_is_painted_as_itself():
    seen = {}
    for p in CATALOG:
        seen.setdefault((p["kind"], p["color"]), p)
    products = list(seen.values())  # every kind and every colour that the catalogue has, in the combinations it has
    for item in drawings(products):
        root = ET.fromstring(item["xml"])  # well-formed or this raises
        assert root.tag == SVG + "svg" and root.get("viewBox") == "0 0 100 100"
        assert root.get("data-shape") == item["shape"] and root.get("data-color") == item["color"] and root.get("role") == "img" and root.get("aria-label")
        body = [path for path in root.iter(SVG + "path") if path.get("fill") == item["color"]]
        assert body, f"nothing is filled with {item['color']} for {item['p']}"  # the silhouette carries the product's own colour
        assert all(path.get("d", "").startswith("M") and path.get("d", "").rstrip().endswith("Z") for path in body)


@needs_node
def test_the_catalogue_colours_map_to_distinct_real_colours_and_an_unknown_one_to_a_neutral_grey():
    colors = sorted({p["color"] for p in CATALOG})
    mapped = dict(zip(colors, js(f"{json.dumps(colors, ensure_ascii=False)}.map(c => api.colorOf(c))")))
    assert len(set(mapped.values())) == len(colors) == 18 and all(re.fullmatch(r"#[0-9A-F]{6}", v) for v in mapped.values())
    assert luminance(mapped["Đen"]) < 0.05 and luminance(mapped["Trắng"]) > 0.8 and luminance(mapped["Kem"]) > 0.7
    red, blue = (int(mapped["Đỏ"][i:i + 2], 16) for i in (1, 5))
    assert red > blue and int(mapped["Xanh dương"][5:7], 16) > int(mapped["Xanh dương"][1:3], 16)  # red is red, blue is blue
    assert js("[api.colorOf('???'), api.colorOf(null), api.colorOf('ĐEN'), api.colorOf('  đen ')]") == ["#C9CDD6", "#C9CDD6", mapped["Đen"], mapped["Đen"]]


@needs_node
def test_the_weave_follows_the_material_ripstop_twill_linen_grain_or_plain():
    materials = sorted({p["material"] for p in CATALOG})
    patterns = dict(zip(materials, js(f"{json.dumps(materials, ensure_ascii=False)}.map(m => api.patternOf(m))")))
    expected = {"Dù": "ripstop", "Denim": "twill", "Linen": "linen", "Da bò": "grain", "Da tổng hợp": "grain", "Da lộn": "grain"}
    assert patterns == {m: expected.get(m, "plain") for m in materials}  # cotton and every other material are plain
    sample = {"Dù": "Áo khoác nữ", "Denim": "Quần jean nam", "Linen": "Váy liền nữ", "Da bò": "Ví da nam", "Cotton": "Áo thun nam"}
    for material, kind in sample.items():
        root = ET.fromstring(drawings([{"kind": kind, "color": "Be", "material": material, "name": "x"}])[0]["xml"])
        weave = list(root.iter(SVG + "pattern"))
        if patterns[material] == "plain":
            assert weave == [] and root.get("data-pattern") == "plain"
        else:
            assert len(weave) == 1 and root.get("data-pattern") == patterns[material]
            assert any(g.get("clip-path", "").startswith("url(#") for g in root.iter(SVG + "g"))  # the weave stays inside the garment
    sizes = {m: (ET.fromstring(drawings([{"kind": k, "color": "Be", "material": m, "name": "x"}])[0]["xml"]).find(f".//{SVG}pattern").get("width")) for m, k in sample.items() if m != "Cotton"}
    assert sizes == {"Dù": "18", "Denim": "4", "Linen": "14", "Da bò": "7"}  # four different weaves, not one pattern four times


@needs_node
def test_two_swatches_on_one_page_never_share_an_id_and_a_decorative_one_is_hidden_from_screen_readers():
    first, second = js("[xml(api.thumbnail({kind: 'Áo khoác nam', color: 'Đen', material: 'Dù'})), xml(api.thumbnail({kind: 'Áo khoác nam', color: 'Đen', material: 'Dù'}, {decorative: true}))]")
    ids = [e.get("id") for text in (first, second) for e in ET.fromstring(text).iter() if e.get("id")]
    assert len(ids) == len(set(ids)) == 4  # a pattern and a clip path each, four different ids
    decorative = ET.fromstring(second)
    assert decorative.get("aria-hidden") == "true" and decorative.get("role") is None and decorative.get("aria-label") is None
    assert ET.fromstring(first).get("aria-label") == "Áo khoác nam Đen"


@needs_node
def test_the_thumbnail_can_be_drawn_without_its_tile_and_survives_a_product_with_nothing_in_it():
    plain = ET.fromstring(js("xml(api.thumbnail({kind: 'Mũ', color: 'Vàng', material: 'Canvas'}, {tile: false}))"))
    assert not [r for r in plain.iter(SVG + "rect")]
    empty = ET.fromstring(js("xml(api.thumbnail({}))"))
    assert empty.get("data-shape") == "accessory" and empty.get("data-color") == "#C9CDD6" and empty.get("aria-label") == "Sản phẩm"
    hostile = ET.fromstring(js("xml(api.thumbnail({kind: '<img src=x onerror=alert(1)>', color: '\"><script>', material: '&', name: '<b>x</b>'}))"))
    assert hostile.get("aria-label") == "<b>x</b>" and not list(hostile.iter(SVG + "script"))  # text stays text


# --- the demo products ---------------------------------------------------------------------------------------------------------------------------


def test_the_demo_product_list_is_what_the_script_builds_from_the_catalogue():
    assert OUT_PATH.read_text(encoding="utf-8") == render(build())


def test_every_demo_product_is_a_real_in_stock_catalogue_product_with_its_catalogue_price():
    by_sku = {p["sku"]: p for p in CATALOG}
    data = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    products = data["products"]
    assert data["synthetic"] is True and 8 <= len(products) <= 12 and len({p["sku"] for p in products}) == len(products) == len(CHOSEN)
    for p in products:
        real = by_sku[p["sku"]]  # a KeyError here is a product that does not exist
        assert (p["name"], p["kind"], p["color"], p["material"], p["slug"], p["category"]) == (real["name"], real["kind"], real["color"], real["material"], real["slug"], real["category"])
        assert p["price_vnd"] == real["price"] and p["in_stock"] is True and real["stock"] > 0
    assert {"SM-NAM-0014", "SM-NAM-0042"} <= {p["sku"] for p in products}  # the two jackets of the offer
    assert len({p["kind"] for p in products}) >= 8


@needs_node
def test_the_demo_products_use_all_eight_silhouettes_and_four_different_weaves():
    products = json.loads(OUT_PATH.read_text(encoding="utf-8"))["products"]
    drawn = drawings(products)
    assert {d["shape"] for d in drawn} == SHAPES
    assert {d["pattern"] for d in drawn} >= {"ripstop", "twill", "linen", "grain", "plain"}
    for d in drawn:
        ET.fromstring(d["xml"])


def test_the_build_script_refuses_a_product_that_is_gone_or_out_of_stock(tmp_path):
    catalog = {"products": [{"sku": sku, "slug": "s", "name": "n", "kind": "k", "category": "c", "color": "Đen", "material": "m", "sizes": ["M"], "price": 1, "stock": 0} for sku in CHOSEN]}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(SystemExit, match="out of stock"):
        build(path)
    path.write_text(json.dumps({"products": []}), encoding="utf-8")
    with pytest.raises(SystemExit, match="not in the catalog"):
        build(path)


# --- the demo page -------------------------------------------------------------------------------------------------------------------------------


def test_the_demo_page_has_the_header_the_hero_the_grid_and_the_footer_of_a_shop_in_vietnamese():
    assert '<html lang="vi">' in DEMO_HTML and "ShopMate<span>Fashion</span>" in DEMO_HTML
    nav = re.search(r"<nav[^>]*>(.*?)</nav>", DEMO_HTML, re.S).group(1)
    assert re.findall(r">([^<]+)</a>", nav) == ["Nam", "Nữ", "Giày dép", "Phụ kiện", "Chính sách"]
    assert "Hỏi trợ lý về size, chất liệu, đổi trả hoặc tra đơn" in DEMO_HTML
    assert 'id="grid"' in DEMO_HTML and "Trang demo — sản phẩm và đơn hàng là dữ liệu hư cấu" in DEMO_HTML
    assert "Thử hỏi" not in DEMO_HTML and "<code" not in DEMO_HTML  # the example questions are chips inside the widget now
    assert "flex-wrap: wrap" in re.search(r"\n  nav \{([^}]*)\}", DEMO_HTML).group(1) and "header.shop .wrap { display: flex; flex-wrap: wrap" in DEMO_HTML  # the nav wraps, it is never cut


def test_the_footer_says_only_what_the_policy_documents_say():
    policies = {name: (REPO / "data" / "policies" / f"{name}.md").read_text(encoding="utf-8") for name in ("doi-tra", "van-chuyen", "bao-hanh")}
    footer = DEMO_HTML[DEMO_HTML.index('<footer'):DEMO_HTML.index("</footer>")]
    facts = [("7 ngày", "doi-tra", "**7 ngày**"), ("30 ngày", "doi-tra", "**30 ngày**"), ("tối đa 1 lần", "doi-tra", "**1 lần**"), ("30.000₫", "van-chuyen", "**30.000đ**"),
             ("500.000₫", "van-chuyen", "**500.000đ**"), ("1–2 ngày", "van-chuyen", "**1–2 ngày**"), ("2–5 ngày", "van-chuyen", "**2–5 ngày**"),
             ("6 tháng", "bao-hanh", "**6 tháng**"), ("12 tháng", "bao-hanh", "**12 tháng**"), ("3 tháng", "bao-hanh", "**3 tháng**")]
    for shown, document, written in facts:
        assert shown in footer and written in policies[document], (shown, document)


def test_the_demo_page_and_its_script_load_nothing_inline_and_put_no_data_in_as_html():
    assert not re.search(r"<script(?![^>]*\bsrc=)", DEMO_HTML)  # the CSP forbids inline script
    assert not re.search(r"\sstyle=", DEMO_HTML)  # and every style is in the one style element
    for forbidden in (r"\.innerHTML", r"\.outerHTML", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new Function"):
        assert not re.search(forbidden, DEMO_JS), forbidden
    assert "window.ShopMateChat.thumbnail" in DEMO_JS and "createElementNS" not in DEMO_JS  # the page does not draw its own swatches: it asks the widget's function
    assert "fetch('/widget/demo-products.json'" in DEMO_JS


# --- the design system -----------------------------------------------------------------------------------------------------------------------------


def test_the_widget_uses_the_agreed_palette_and_only_that():
    for token in ("--indigo:#1F2A5C", "--paper:#F7F8FA", "--ink:#1A1D24", "--tint:#E8EAF4", "--green:#2E7D5B", "--red:#B3261E"):
        assert token in SOURCE, token
    for token in ("--indigo: #1F2A5C", "--paper: #F7F8FA", "--ink: #1A1D24", "--tint: #E8EAF4", "--green: #2E7D5B", "--red: #B3261E"):
        assert token in DEMO_HTML, token
    assert "#faf7f2" not in DEMO_HTML.lower() and "ffbf47" not in SOURCE.lower()  # no cream background, no orange focus ring
    assert ":focus-visible{outline:2px solid var(--indigo)" in SOURCE and ":focus-visible { outline: 2px solid var(--indigo)" in DEMO_HTML


def test_the_lacquer_red_is_only_for_out_of_stock_and_errors():
    for text in (SOURCE.split("\n"), DEMO_HTML.split("\n")):
        for line in text:
            if "var(--red)" in line or "rgba(179" in line:
                assert ".stock.out" in line or ".bot.error" in line, line.strip()
    assert "#B3261E".lower() not in DEMO_JS.lower()


def test_there_is_no_decorative_gradient_and_no_monospace_font_in_what_the_customer_sees():
    for name, text in (("widget", SOURCE), ("demo", DEMO_HTML), ("demo.js", DEMO_JS)):
        assert "gradient" not in text.lower(), name
        assert not re.search(r"monospace|Courier|Consolas|Menlo|\bmono\b", text, re.I), name


def test_the_font_is_be_vietnam_pro_with_the_system_font_as_the_fallback_and_the_widget_loads_nothing_itself():
    assert 'font-family:"Be Vietnam Pro",system-ui' in SOURCE and 'font: 400 16px/1.55 "Be Vietnam Pro", system-ui' in DEMO_HTML
    assert "family=Be+Vietnam+Pro:wght@400;500;600;700" in DEMO_HTML
    assert "googleapis" not in SOURCE and "gstatic" not in SOURCE and "@import" not in SOURCE  # a page that wants the font loads it


def test_radii_follow_the_levels_card_12_button_10_chip_pill_and_the_only_shadow_is_the_floating_panel():
    css = SOURCE
    assert re.search(r"\.card\{[^}]*border-radius:12px", css) and re.search(r"\.btn\{border-radius:10px", css) and re.search(r"\.chip\{[^}]*border-radius:999px", css)
    assert re.search(r"\.item \{[^}]*border-radius: 12px", DEMO_HTML)
    shadows = [line for line in css.split("\n") if "box-shadow" in line]
    assert len(shadows) == 1 and ".panel{" in shadows[0] and "box-shadow" not in DEMO_HTML


def test_the_widget_has_the_parts_of_the_brief():
    for needle in ("'S'", "Thường trả lời trong vài giây", "Thu nhỏ cửa sổ trò chuyện", "this._fab.hidden = open", "MAX_ROWS = 4", "scroll-snap-type:x mandatory", "Xem sản phẩm",
                   "role: 'log'", "e.key === 'Escape'", "function fillText", "/^\\s*-\\s+(.*)$/", "T.retry", "T.wait", "class: 'bubble typing'", "inset:0"):
        assert needle in SOURCE, needle
    for chip in ("Áo khoác nam dưới 500k", "Đổi hàng trong bao nhiêu ngày?", "Kiểm tra đơn hàng", "Giao hàng mất mấy ngày?"):
        assert f"'{chip}'" in SOURCE, chip
    assert SOURCE.count("function thumbnail") == 1 and "thumbnail(p, { decorative: true })" in SOURCE  # the card uses the shared function
    assert re.search(r"@media \(prefers-reduced-motion:no-preference\)\{\.typing i\{animation", SOURCE)  # the dots only move where motion is welcome


def test_every_suggestion_chip_has_a_real_answer_in_the_catalogue_or_the_policies():
    chips = re.findall(r"'([^']+)'", re.search(r"var CHIPS = \[(.*?)\];", SOURCE, re.S).group(1))
    assert chips == ["Áo khoác nam dưới 500k", "Đổi hàng trong bao nhiêu ngày?", "Kiểm tra đơn hàng", "Giao hàng mất mấy ngày?"]
    jackets = {p["sku"] for p in CATALOG if p["kind"] == "Áo khoác nam" and p["price"] <= 500000 and p["stock"] > 0}
    assert jackets == {"SM-NAM-0014", "SM-NAM-0042"}  # chip 1 finds exactly the two jackets of the offer, both in stock
    returns = (REPO / "data" / "policies" / "doi-tra.md").read_text(encoding="utf-8")
    article_2 = returns.split("## Điều 2.")[1].split("## Điều 3.")[0]
    assert "**7 ngày**" in article_2 and "**30 ngày**" in article_2  # chip 2: the return article answers "how many days"
    assert "#" not in chips[2] and not re.search(r"\d", chips[2])  # chip 3 carries no order number: the agent asks for the number and the email (templates.ASK_NUMBER_AND_EMAIL)
    orders = json.loads((REPO / "data" / "seed" / "orders.vi.json").read_text(encoding="utf-8"))["orders"]
    assert orders and all(o["customer"]["email"].endswith("@example.com") for o in orders)  # and there are (synthetic) orders it can then look up
    shipping = (REPO / "data" / "policies" / "van-chuyen.md").read_text(encoding="utf-8").split("## Điều 3.")[1].split("## Điều 4.")[0]
    assert "**1–2 ngày**" in shipping and "**2–5 ngày**" in shipping  # chip 4: the delivery-time article

