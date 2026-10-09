"""Generate the synthetic Vietnamese catalog: data/seed/catalog.vi.json.

Deterministic: a fixed seed and `random.Random`, no LLM calls, no clock, no set
iteration. The same seed always produces byte-identical output (see
tests/test_catalog_determinism.py). ALL DATA IS SYNTHETIC.

Usage (from the repo root):
    uv run python -m scripts.generate_catalog            # write the file
    uv run python -m scripts.generate_catalog --check    # exit 1 if the file is stale
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.catalog.schema import CATALOG_PATH, Catalog, Category, Product, nfc

DEFAULT_SEED = 20261009
OUT_OF_STOCK_COUNT = 25  # ~10% of 250
MAX_STOCK = 50  # Saleor caps `quantityAvailable` at 50 per checkout; keep stock within it

LETTER = ("S", "M", "L", "XL", "XXL")
WAIST = ("28", "29", "30", "31", "32", "33", "34")
SHOE_MEN = ("39", "40", "41", "42", "43", "44")
SHOE_WOMEN = ("35", "36", "37", "38", "39")
SHOE_UNISEX = ("36", "37", "38", "39", "40", "41", "42", "43")
FREE = ("Freesize",)


@dataclass(frozen=True)
class Kind:
    name: str
    styles: tuple[str, ...]
    price: tuple[int, int]  # VND, inclusive
    sizes: tuple[str, ...]
    colors: tuple[str, ...]
    materials: tuple[str, ...]


@dataclass(frozen=True)
class CategorySpec:
    slug: str
    name: str
    sku_prefix: str
    count: int
    kinds: tuple[Kind, ...]
    usage: tuple[str, ...]
    care: tuple[str, ...]


CLOTHES_CARE = (
    "Nên giặt ở nhiệt độ dưới 30 độ C và lộn trái khi phơi để giữ màu.",
    "Có thể giặt máy ở chế độ nhẹ, không dùng chất tẩy mạnh.",
    "Ủi ở nhiệt độ trung bình, tránh phơi trực tiếp dưới nắng gắt.",
)
LEATHER_CARE = (
    "Lau sạch bằng khăn mềm hơi ẩm, tránh ngâm nước.",
    "Bảo quản nơi khô thoáng, nhét giấy vào bên trong để giữ form.",
    "Dùng xi hoặc kem dưỡng da định kỳ để sản phẩm bền đẹp.",
)

CATEGORIES: tuple[CategorySpec, ...] = (
    CategorySpec(
        "thoi-trang-nam", "Thời trang nam", "NAM", 50,
        (
            Kind("Áo thun nam", ("cổ tròn basic", "cổ tim", "oversize", "in chữ", "kẻ ngang"),
                 (149_000, 299_000), LETTER,
                 ("Đen", "Trắng", "Xám", "Xanh navy", "Be", "Xanh rêu"),
                 ("Cotton", "Thun lạnh", "Cotton pha spandex")),
            Kind("Áo sơ mi nam", ("tay dài công sở", "tay ngắn", "kẻ sọc", "oxford", "cổ trụ"),
                 (249_000, 499_000), LETTER,
                 ("Trắng", "Xanh dương", "Xanh navy", "Đen", "Hồng", "Kem"),
                 ("Cotton", "Linen", "Kate", "Bamboo")),
            Kind("Áo polo nam", ("basic", "phối viền", "thể thao"),
                 (199_000, 399_000), LETTER,
                 ("Trắng", "Đen", "Xanh navy", "Xám", "Đỏ đô"),
                 ("Cotton cá sấu", "Thun lạnh", "Cotton")),
            Kind("Quần jean nam", ("slim fit", "ống đứng", "rách gối", "baggy"),
                 (349_000, 599_000), WAIST,
                 ("Xanh nhạt", "Xanh đậm", "Đen", "Xám"),
                 ("Denim",)),
            Kind("Quần kaki nam", ("slim fit", "ống đứng", "túi hộp"),
                 (299_000, 499_000), WAIST,
                 ("Be", "Đen", "Xanh navy", "Xám", "Nâu"),
                 ("Kaki", "Kaki pha spandex")),
            Kind("Quần short nam", ("kaki", "thể thao", "đi biển"),
                 (149_000, 299_000), LETTER,
                 ("Be", "Đen", "Xanh navy", "Xám", "Xanh rêu"),
                 ("Kaki", "Polyester", "Cotton")),
            Kind("Áo khoác nam", ("bomber", "gió hai lớp", "jean", "nỉ có mũ"),
                 (399_000, 899_000), LETTER,
                 ("Đen", "Xanh navy", "Xám", "Be", "Xanh rêu"),
                 ("Polyester", "Dù", "Denim", "Nỉ")),
        ),
        (
            "Phù hợp đi làm, đi chơi hay dạo phố cuối tuần.",
            "Thiết kế đơn giản, dễ phối với quần jean hoặc quần kaki.",
            "Form dáng chuẩn người Việt, mặc thoải mái cả ngày.",
        ),
        CLOTHES_CARE,
    ),
    CategorySpec(
        "thoi-trang-nu", "Thời trang nữ", "NU", 50,
        (
            Kind("Áo thun nữ", ("cổ tròn basic", "croptop", "oversize", "in hình"),
                 (129_000, 279_000), LETTER,
                 ("Trắng", "Đen", "Hồng", "Be", "Vàng", "Xanh dương"),
                 ("Cotton", "Thun lạnh", "Cotton pha spandex")),
            Kind("Áo sơ mi nữ", ("công sở", "tay bồng", "cổ nơ", "dáng rộng"),
                 (229_000, 459_000), LETTER,
                 ("Trắng", "Kem", "Xanh dương", "Hồng", "Đen"),
                 ("Lụa", "Voan", "Cotton", "Linen")),
            Kind("Váy liền nữ", ("hoa nhí", "công sở", "maxi đi biển", "babydoll"),
                 (299_000, 699_000), LETTER,
                 ("Đen", "Trắng", "Hồng", "Xanh dương", "Vàng", "Đỏ"),
                 ("Voan", "Lụa", "Cotton", "Linen")),
            Kind("Chân váy nữ", ("chữ A", "xếp ly", "bút chì", "jean"),
                 (199_000, 399_000), LETTER,
                 ("Đen", "Be", "Trắng", "Xanh navy", "Hồng"),
                 ("Kaki", "Voan", "Denim", "Tweed")),
            Kind("Quần jean nữ", ("ống loe", "skinny", "ống suông", "baggy"),
                 (329_000, 559_000), WAIST[:5],
                 ("Xanh nhạt", "Xanh đậm", "Đen", "Trắng"),
                 ("Denim",)),
            Kind("Áo khoác nữ", ("cardigan", "blazer", "jean", "gió chống nắng"),
                 (299_000, 799_000), LETTER,
                 ("Đen", "Be", "Trắng", "Hồng", "Xám"),
                 ("Len", "Polyester", "Denim", "Dù")),
            Kind("Áo len nữ", ("cổ lọ", "dệt kim", "cổ tim"),
                 (279_000, 499_000), LETTER,
                 ("Kem", "Be", "Nâu", "Xám", "Hồng"),
                 ("Len", "Len pha cotton")),
        ),
        (
            "Phù hợp đi làm, đi tiệc nhẹ hoặc dạo phố.",
            "Thiết kế nữ tính, tôn dáng, dễ phối cùng nhiều phụ kiện.",
            "Kiểu dáng trẻ trung, phù hợp nhiều vóc dáng.",
        ),
        CLOTHES_CARE,
    ),
    CategorySpec(
        "giay-dep", "Giày dép", "GIAY", 45,
        (
            Kind("Giày sneaker nam", ("cổ thấp", "cổ cao", "đế độn"),
                 (499_000, 1_290_000), SHOE_MEN,
                 ("Trắng", "Đen", "Xám", "Xanh navy"),
                 ("Da tổng hợp", "Vải canvas", "Da bò")),
            Kind("Giày sneaker nữ", ("cổ thấp", "đế độn", "slip-on"),
                 (459_000, 1_190_000), SHOE_WOMEN,
                 ("Trắng", "Hồng", "Be", "Đen"),
                 ("Da tổng hợp", "Vải canvas")),
            Kind("Giày tây nam", ("oxford", "derby", "loafer", "monk strap", "chelsea boot"),
                 (790_000, 1_890_000), SHOE_MEN,
                 ("Đen", "Nâu"),
                 ("Da bò", "Da tổng hợp")),
            Kind("Giày cao gót nữ", ("mũi nhọn 7cm", "đế vuông 5cm", "quai mảnh 9cm"),
                 (390_000, 890_000), SHOE_WOMEN,
                 ("Đen", "Be", "Đỏ", "Trắng"),
                 ("Da tổng hợp", "Da bò", "Da lộn")),
            Kind("Sandal", ("quai chéo", "đế bánh mì", "quai hậu"),
                 (199_000, 499_000), SHOE_UNISEX,
                 ("Đen", "Nâu", "Be", "Trắng"),
                 ("Da tổng hợp", "Cao su", "Dù")),
            Kind("Dép quai ngang", ("basic", "đế êm", "in logo"),
                 (99_000, 249_000), SHOE_UNISEX,
                 ("Đen", "Trắng", "Xám", "Xanh navy"),
                 ("Cao su", "EVA")),
        ),
        (
            "Đế êm, chống trơn trượt, đi cả ngày không mỏi chân.",
            "Kiểu dáng thời trang, dễ phối với nhiều trang phục.",
            "Phù hợp đi học, đi làm và đi chơi.",
        ),
        LEATHER_CARE,
    ),
    CategorySpec(
        "tui-vi", "Túi ví", "TUI", 35,
        (
            Kind("Túi xách nữ", ("công sở", "tote", "mini", "kẹp nách"),
                 (399_000, 1_290_000), FREE,
                 ("Đen", "Nâu", "Be", "Trắng", "Hồng"),
                 ("Da tổng hợp", "Da bò", "Vải canvas")),
            Kind("Túi đeo chéo", ("mini", "hộp", "bao tử"),
                 (249_000, 699_000), FREE,
                 ("Đen", "Nâu", "Xám", "Be"),
                 ("Da tổng hợp", "Vải canvas", "Dù")),
            Kind("Balo", ("laptop 15.6 inch", "du lịch", "mini"),
                 (349_000, 990_000), FREE,
                 ("Đen", "Xám", "Xanh navy", "Be"),
                 ("Polyester", "Dù", "Vải canvas")),
            Kind("Ví da nam", ("gập đôi", "dáng đứng", "mỏng", "dáng ngang có khóa", "kẹp tiền"),
                 (249_000, 699_000), FREE,
                 ("Đen", "Nâu"),
                 ("Da bò", "Da tổng hợp")),
            Kind("Ví nữ", ("cầm tay", "mini gập ba", "dài có khóa kéo"),
                 (199_000, 599_000), FREE,
                 ("Hồng", "Đen", "Be", "Đỏ", "Trắng"),
                 ("Da tổng hợp", "Da bò")),
        ),
        (
            "Nhiều ngăn tiện lợi, đựng vừa điện thoại, ví và đồ cá nhân.",
            "Khóa kéo chắc chắn, đường may tỉ mỉ.",
            "Thiết kế gọn nhẹ, phù hợp đi làm và đi chơi.",
        ),
        LEATHER_CARE,
    ),
    CategorySpec(
        "phu-kien", "Phụ kiện", "PK", 35,
        (
            Kind("Thắt lưng nam", ("khóa tự động", "khóa kim", "bản nhỏ", "khóa trơn", "bản to"),
                 (199_000, 599_000), FREE,
                 ("Đen", "Nâu"),
                 ("Da bò", "Da tổng hợp")),
            Kind("Mũ", ("lưỡi trai basic", "lưỡi trai thêu chữ", "bucket", "nồi beret"),
                 (99_000, 249_000), FREE,
                 ("Đen", "Trắng", "Be", "Xanh navy", "Hồng"),
                 ("Cotton", "Kaki", "Len", "Polyester")),
            Kind("Kính mát", ("gọng vuông", "gọng tròn", "phi công"),
                 (199_000, 590_000), FREE,
                 ("Đen", "Nâu", "Vàng", "Bạc"),
                 ("Nhựa acetate", "Kim loại")),
            Kind("Khăn choàng", ("họa tiết", "trơn", "caro"),
                 (149_000, 399_000), FREE,
                 ("Kem", "Đỏ", "Xám", "Be", "Xanh navy"),
                 ("Len", "Lụa", "Cotton")),
            Kind("Tất", ("cổ ngắn set 5 đôi", "cổ cao set 3 đôi", "lười set 5 đôi"),
                 (59_000, 149_000), FREE,
                 ("Trắng", "Đen", "Xám", "Be"),
                 ("Cotton", "Cotton pha spandex")),
        ),
        (
            "Điểm nhấn nhỏ giúp bộ trang phục thêm nổi bật.",
            "Thiết kế tối giản, hợp với nhiều phong cách.",
            "Món quà tặng ý nghĩa cho bạn bè và người thân.",
        ),
        (
            "Bảo quản nơi khô ráo, tránh va đập mạnh.",
            "Vệ sinh nhẹ nhàng bằng khăn mềm sau khi sử dụng.",
        ),
    ),
    CategorySpec(
        "do-the-thao", "Đồ thể thao", "TT", 35,
        (
            Kind("Áo thể thao nam", ("chạy bộ", "tập gym", "ba lỗ"),
                 (149_000, 349_000), LETTER,
                 ("Đen", "Xám", "Xanh dương", "Đỏ", "Trắng"),
                 ("Polyester", "Thun lạnh", "Vải lưới")),
            Kind("Áo bra thể thao nữ", ("nâng đỡ vừa", "nâng đỡ cao", "dây chéo lưng"),
                 (179_000, 399_000), LETTER[:4],
                 ("Đen", "Hồng", "Trắng", "Xám", "Tím"),
                 ("Polyester", "Thun lạnh")),
            Kind("Quần legging nữ", ("lưng cao", "lửng", "có túi"),
                 (199_000, 399_000), LETTER,
                 ("Đen", "Xám", "Xanh navy", "Tím"),
                 ("Thun lạnh", "Polyester pha spandex")),
            Kind("Quần short thể thao", ("hai lớp", "chạy bộ", "tập gym"),
                 (129_000, 299_000), LETTER,
                 ("Đen", "Xám", "Xanh navy", "Xanh dương"),
                 ("Polyester", "Vải lưới")),
            Kind("Giày chạy bộ", ("đệm êm", "siêu nhẹ", "chống trượt"),
                 (690_000, 1_890_000), SHOE_UNISEX,
                 ("Đen", "Trắng", "Xám", "Xanh dương", "Cam"),
                 ("Vải lưới", "Da tổng hợp")),
        ),
        (
            "Co giãn tốt, thoáng khí, phù hợp tập gym, chạy bộ và yoga.",
            "Khô nhanh, thấm hút mồ hôi, vận động thoải mái.",
            "Thiết kế năng động cho người yêu thể thao.",
        ),
        CLOTHES_CARE,
    ),
)

MATERIAL_NOTES = {
    "Cotton": "mềm mại, thấm hút mồ hôi tốt",
    "Thun lạnh": "mát, ít nhăn, co giãn nhẹ",
    "Cotton pha spandex": "co giãn bốn chiều, giữ form tốt",
    "Linen": "nhẹ, thoáng mát cho ngày hè",
    "Kate": "đứng form, ít nhăn, dễ ủi",
    "Bamboo": "kháng khuẩn tự nhiên, mềm mịn",
    "Cotton cá sấu": "dày dặn, bề mặt vải thoáng khí",
    "Denim": "bền chắc, càng mặc càng đẹp",
    "Kaki": "dày dặn, đứng dáng",
    "Kaki pha spandex": "đứng dáng mà vẫn co giãn nhẹ",
    "Polyester": "nhẹ, nhanh khô, bền màu",
    "Polyester pha spandex": "ôm vừa vặn, co giãn tốt",
    "Dù": "chống nước nhẹ, cản gió",
    "Nỉ": "ấm áp, mềm mại",
    "Lụa": "mềm rủ, bóng nhẹ sang trọng",
    "Voan": "mỏng nhẹ, bay bổng",
    "Tweed": "dệt sợi đan xen, sang trọng",
    "Len": "giữ ấm tốt, mềm tay",
    "Len pha cotton": "ấm vừa phải, không gây ngứa",
    "Da tổng hợp": "bền, dễ lau chùi",
    "Da bò": "da thật, bền và lên màu đẹp theo thời gian",
    "Da lộn": "bề mặt mịn, sang trọng",
    "Vải canvas": "dày, bền, chịu lực tốt",
    "Cao su": "chống trơn trượt, chịu nước",
    "EVA": "siêu nhẹ, êm chân",
    "Nhựa acetate": "nhẹ, bền màu, ôm mặt",
    "Kim loại": "mảnh, chắc chắn, không gỉ",
    "Vải lưới": "thoáng khí tối đa",
}


def _ascii_slug(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
    return "-".join(part for part in out.split("-") if part)


def _material_phrase(material: str) -> str:
    return material if material.isupper() else material.lower()


def _pick_sizes(rng: random.Random, pool: tuple[str, ...]) -> list[str]:
    if len(pool) == 1:
        return list(pool)
    length = rng.randint(2, len(pool))
    start = rng.randint(0, len(pool) - length)
    return list(pool[start : start + length])


def _pick_price(rng: random.Random, low: int, high: int) -> int:
    """Shop-style prices ending in 9.000 (e.g. 249.000đ), within [low, high]."""
    tens = rng.randint((low + 1_000 + 9_999) // 10_000, (high + 1_000) // 10_000)
    return tens * 10_000 - 1_000


def build_catalog(seed: int = DEFAULT_SEED) -> Catalog:
    rng = random.Random(seed)
    categories: list[Category] = []
    products: list[dict] = []
    used_names: set[str] = set()

    for spec in CATEGORIES:
        categories.append(Category(slug=spec.slug, name=nfc(spec.name), sku_prefix=spec.sku_prefix))
        for index in range(spec.count):
            kind = spec.kinds[index % len(spec.kinds)]
            for _attempt in range(100):
                style = rng.choice(kind.styles)
                color = rng.choice(kind.colors)
                name = nfc(f"{kind.name} {style} {color.lower()}")
                if name not in used_names:
                    break
            else:  # pragma: no cover - the spec has enough combinations
                raise RuntimeError(f"cannot find a unique name for {kind.name}")
            used_names.add(name)

            material = rng.choice(kind.materials)
            sizes = _pick_sizes(rng, kind.sizes)
            sku = f"SM-{spec.sku_prefix}-{index + 1:04d}"
            size_text = "Freesize" if sizes == ["Freesize"] else ", ".join(sizes)
            description = " ".join(
                (
                    f"{name} làm từ chất liệu {_material_phrase(material)}, {MATERIAL_NOTES[material]}.",
                    rng.choice(spec.usage),
                    f"Kích cỡ có sẵn: {size_text}.",
                    rng.choice(spec.care),
                )
            )
            products.append(
                {
                    "sku": sku,
                    "slug": f"{_ascii_slug(name)}-{sku.lower()}",
                    "name": name,
                    "category": spec.slug,
                    "kind": nfc(kind.name),
                    "description": nfc(description),
                    "color": nfc(color),
                    "material": nfc(material),
                    "sizes": sizes,
                    "price": _pick_price(rng, *kind.price),
                    "stock": rng.randint(1, MAX_STOCK),
                }
            )

    products.sort(key=lambda p: p["sku"])
    for i in sorted(rng.sample(range(len(products)), OUT_OF_STOCK_COUNT)):
        products[i]["stock"] = 0

    return Catalog(seed=seed, categories=categories, products=[Product(**p) for p in products])


def render(catalog: Catalog) -> str:
    """Canonical serialization: fixed key order, UTF-8 text, trailing newline."""
    return json.dumps(catalog.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=CATALOG_PATH)
    parser.add_argument("--check", action="store_true", help="do not write; exit 1 if --out differs")
    args = parser.parse_args(argv)

    text = render(build_catalog(args.seed))
    if args.check:
        current = args.out.read_bytes() if args.out.is_file() else b""
        if current != text.encode("utf-8"):
            print(f"STALE: {args.out} differs from generator output (seed {args.seed})", file=sys.stderr)
            return 1
        print(f"OK: {args.out} matches generator output (seed {args.seed})")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(text.encode("utf-8"))
    catalog = Catalog.model_validate_json(text)
    out_of_stock = sum(1 for p in catalog.products if p.stock == 0)
    print(f"wrote {args.out}: {len(catalog.products)} products, {len(catalog.categories)} categories, "
          f"{out_of_stock} out of stock (seed {args.seed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
