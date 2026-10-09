"""Copy recorded order numbers from data/seed/orders.vi.json into the golden set.

Only `gold_source.order_number` of order items changes ("pending" -> number);
every other field, including human edits, is preserved. A golden item whose
recorded number differs from the fixture is reported and left unchanged.

Run after scripts/seed_orders.py and BEFORE freezing the test-split hash
(changing order numbers changes the hash).

Usage (from the repo root):
    uv run python -m eval.golden.sync_order_numbers
"""

from __future__ import annotations

import sys

from app.orders.schema import PENDING, OrderFixtures, load_orders
from eval.golden.draft_golden import render
from eval.golden.schema import GOLDEN_PATH, GoldenSet, load_golden


def sync(golden: GoldenSet, orders: OrderFixtures) -> tuple[GoldenSet, int, list[str]]:
    fixtures = orders.by_reference()
    items, changed, conflicts = [], 0, []
    for item in golden.items:
        source = item.gold_source
        fixture = fixtures.get(source.external_reference) if source.type == "order" else None
        if fixture is None or fixture.number == PENDING or source.order_number == fixture.number:
            items.append(item)
        elif source.order_number == PENDING:
            items.append(item.model_copy(update={"gold_source": source.model_copy(update={"order_number": fixture.number})}))
            changed += 1
        else:
            conflicts.append(f"{item.id}: golden has #{source.order_number}, fixture has #{fixture.number}")
            items.append(item)
    return golden.model_copy(update={"items": items}), changed, conflicts


def main() -> int:
    golden, changed, conflicts = sync(load_golden(), load_orders())
    if changed:
        GOLDEN_PATH.write_bytes(render(golden).encode("utf-8"))
    print(f"order numbers synced: {changed} items updated")
    for conflict in conflicts:
        print(f"CONFLICT: {conflict}", file=sys.stderr)
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
