"""Generate the synthetic test-order fixtures: data/seed/orders.vi.json.

Deterministic for a given seed and catalog. Order numbers are assigned by Saleor,
so they are not generated: every order starts as "pending", and numbers that
scripts/seed_orders.py already recorded in the existing file are carried over.
ALL DATA IS SYNTHETIC (customers use @example.com addresses).

Usage (from the repo root):
    uv run python -m scripts.generate_orders            # write the file
    uv run python -m scripts.generate_orders --check    # exit 1 if stale (numbers ignored)
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import shop
from app.catalog.schema import Catalog, load_catalog
from app.orders.schema import ORDERS_PATH, PENDING, OrderFixtures, load_orders

DEFAULT_SEED = 20261010
ICT = timezone(timedelta(hours=7))
FIRST_DAY = datetime(2026, 8, 1, 8, 0, tzinfo=ICT)
LAST_DAY = datetime(2026, 9, 30, 21, 0, tzinfo=ICT)  # must stay in the past at seeding time

STATUS_PLAN = (
    ["FULFILLED"] * 10
    + ["UNFULFILLED"] * 9
    + ["PARTIALLY_FULFILLED"] * 4
    + ["CANCELED"] * 4
    + ["UNCONFIRMED"] * 3
)

LAST_NAMES = ("Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Vũ", "Đặng", "Bùi", "Đỗ", "Ngô")
FIRST_NAMES = ("An", "Bình", "Châu", "Dũng", "Giang", "Hà", "Hùng", "Lan", "Linh", "Minh",
               "Nam", "Ngọc", "Phúc", "Quân", "Thảo", "Trang", "Tuấn", "Vy")
# (city_area, city, country_area). country_area values are i18naddress VN keys.
AREAS = (
    ("Quận 1", "Hồ Chí Minh", "Thành phố Hồ Chí Minh"),
    ("Quận 3", "Hồ Chí Minh", "Thành phố Hồ Chí Minh"),
    ("Quận Bình Thạnh", "Hồ Chí Minh", "Thành phố Hồ Chí Minh"),
    ("Thành phố Thủ Đức", "Hồ Chí Minh", "Thành phố Hồ Chí Minh"),
    ("Quận Cầu Giấy", "Hà Nội", "Hà Nội"),
    ("Quận Đống Đa", "Hà Nội", "Hà Nội"),
    ("Quận Hải Châu", "Đà Nẵng", "Đà Nẵng"),
    ("Quận Ninh Kiều", "Cần Thơ", "Cần Thơ"),
    ("Quận Lê Chân", "Hải Phòng", "Hải Phòng"),
)
STREETS = ("Nguyễn Trãi", "Lê Lợi", "Trần Hưng Đạo", "Hai Bà Trưng", "Lý Thường Kiệt",
           "Nguyễn Huệ", "Phan Đình Phùng", "Võ Văn Tần", "Điện Biên Phủ")
CUSTOMER_COUNT = 20  # fewer customers than orders, so some customers have several orders


def _customers(rng: random.Random) -> list[dict]:
    customers = []
    for n in range(1, CUSTOMER_COUNT + 1):
        city_area, city, country_area = rng.choice(AREAS)
        customers.append(
            {
                "customer": {
                    "first_name": rng.choice(FIRST_NAMES),
                    "last_name": rng.choice(LAST_NAMES),
                    "email": f"khachhang{n:02d}@example.com",
                },
                "shipping_address": {
                    "street": f"{rng.randint(1, 299)} {rng.choice(STREETS)}",
                    "city_area": city_area,
                    "city": city,
                    "country_area": country_area,
                    "country": "VN",
                },
            }
        )
    return customers


def build_orders(catalog: Catalog, seed: int = DEFAULT_SEED, numbers: dict[str, str] | None = None) -> OrderFixtures:
    rng = random.Random(seed)
    numbers = numbers or {}
    customers = _customers(rng)
    statuses = list(STATUS_PLAN)
    rng.shuffle(statuses)

    span = int((LAST_DAY - FIRST_DAY).total_seconds())
    offsets = sorted(rng.randrange(span) for _ in statuses)

    orders = []
    for index, (status, offset) in enumerate(zip(statuses, offsets), start=1):
        ref = f"shopmate-seed-order-{index:03d}"
        line_count = rng.randint(2, 3) if status == "PARTIALLY_FULFILLED" else rng.randint(1, 3)
        products = rng.sample(catalog.products, line_count)
        lines = [
            {"sku": p.sku, "product_name": p.name, "quantity": rng.choice((1, 1, 1, 2, 2, 3)), "unit_price": p.price}
            for p in products
        ]
        subtotal = sum(line["quantity"] * line["unit_price"] for line in lines)

        fulfillments = []
        tracking = f"SMVN{rng.randint(10**8, 10**9 - 1)}"
        if status == "FULFILLED":
            fulfillments = [{"tracking_code": tracking,
                             "lines": [{"line_index": i, "quantity": line["quantity"]} for i, line in enumerate(lines)]}]
        elif status == "PARTIALLY_FULFILLED":
            fulfillments = [{"tracking_code": tracking, "lines": [{"line_index": 0, "quantity": lines[0]["quantity"]}]}]

        created = FIRST_DAY + timedelta(seconds=offset)
        orders.append(
            {
                "external_reference": ref,
                "number": numbers.get(ref, PENDING),
                "status": status,
                "created_at": created.replace(microsecond=0).isoformat(),
                **rng.choice(customers),
                "lines": lines,
                "fulfillments": fulfillments,
                "shipping_method": shop.shipping_method_for(subtotal),
                "shipping_price": shop.shipping_fee_for(subtotal),
                "subtotal": subtotal,
                "total": subtotal + shop.shipping_fee_for(subtotal),
            }
        )
    return OrderFixtures.model_validate({"seed": seed, "orders": orders})


def render(fixtures: OrderFixtures) -> str:
    return json.dumps(fixtures.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


def existing_numbers(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return {o.external_reference: o.number for o in load_orders(path).orders if o.number != PENDING}


def strip_numbers(fixtures: OrderFixtures) -> OrderFixtures:
    return fixtures.model_copy(update={"orders": [o.model_copy(update={"number": PENDING}) for o in fixtures.orders]})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=ORDERS_PATH)
    parser.add_argument("--check", action="store_true", help="exit 1 if --out differs (order numbers ignored)")
    args = parser.parse_args(argv)

    catalog = load_catalog()
    if args.check:
        fresh = render(build_orders(catalog, args.seed))
        current = render(strip_numbers(load_orders(args.out))) if args.out.is_file() else ""
        if fresh != current:
            print(f"STALE: {args.out} differs from generator output (seed {args.seed})", file=sys.stderr)
            return 1
        print(f"OK: {args.out} matches generator output (seed {args.seed}, numbers ignored)")
        return 0

    fixtures = build_orders(catalog, args.seed, existing_numbers(args.out))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(render(fixtures).encode("utf-8"))
    pending = sum(1 for o in fixtures.orders if o.number == PENDING)
    print(f"wrote {args.out}: {len(fixtures.orders)} orders, {pending} with number pending (seed {args.seed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
