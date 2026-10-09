"""Validate eval/golden/golden.vi.json against the schema, the catalog, the policies and the orders.

Checks:
- schema and label values (intent, query style, split, status, author, gold source type);
- every gold SKU exists in data/seed/catalog.vi.json;
- for product_search items with filters, the gold SKU list equals what the filters
  select from the catalog (recomputed here), and a no-match item really matches nothing;
- every policy clause exists in data/policies/;
- every order reference exists in data/seed/orders.vi.json with the expected status;
- anti-IDOR: the email in an order question matches the order owner exactly when
  provided_identity is "matching_email", differs for "mismatched_email", and is
  absent for "none" (the schema already ties expected_behavior to provided_identity);
  order numbers may be "pending" unless --require-order-numbers is given (use it after
  scripts/seed_orders.py has recorded the numbers);
- the test split is 25–35% of items; prints the test-split SHA-256, and compares it
  with --expected-test-hash when given (the frozen hash is recorded after human review).

Exit codes: 0 = valid, 1 = problems found.

Usage (from the repo root):
    uv run python -m eval.golden.validate_golden
    uv run python -m eval.golden.validate_golden --require-order-numbers --expected-test-hash <sha256>
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from app.catalog.schema import Catalog, load_catalog
from app.orders.schema import PENDING, OrderFixtures, load_orders
from app.policies import clauses
from eval.golden.schema import GOLDEN_PATH, GoldenSet, apply_filters, load_golden, test_split_hash

TEST_FRACTION_RANGE = (0.25, 0.35)
EMAIL = re.compile(r"[a-z0-9.]+@example\.com")


def check(golden: GoldenSet, catalog: Catalog, orders: OrderFixtures, require_order_numbers: bool) -> list[str]:
    problems: list[str] = []
    skus = catalog.by_sku()
    fixtures = orders.by_reference()
    clause_cache: dict[str, dict[int, str]] = {}

    for item in golden.items:
        source = item.gold_source
        if source.type == "skus":
            unknown = [s for s in source.skus if s not in skus]
            if unknown:
                problems.append(f"{item.id}: unknown SKUs {unknown}")
        if item.expected_filters is not None:
            computed = apply_filters(catalog, item.expected_filters)
            if source.type == "skus" and computed != source.skus:
                problems.append(f"{item.id}: gold SKUs {source.skus} != filters on catalog {computed}")
            if source.type == "no_answer" and computed:
                problems.append(f"{item.id}: marked no-match but filters select {computed}")
        if source.type == "policy_clause":
            doc_clauses = clause_cache.setdefault(source.document, clauses(source.document))
            if source.clause not in doc_clauses:
                problems.append(f"{item.id}: {source.document} has no Điều {source.clause}")
        if source.type == "order":
            fixture = fixtures.get(source.external_reference)
            if fixture is None:
                problems.append(f"{item.id}: unknown order {source.external_reference}")
                continue
            if fixture.status != source.expected_status:
                problems.append(f"{item.id}: expected_status {source.expected_status} != fixture {fixture.status}")
            emails = set(EMAIL.findall(item.question))
            owner = fixture.customer.email
            identity_ok = {
                "matching_email": owner in emails,
                "mismatched_email": bool(emails) and owner not in emails,
                "none": not emails,
            }[source.provided_identity]
            if not identity_ok:
                problems.append(f"{item.id}: question emails {sorted(emails)} do not fit provided_identity "
                                f"{source.provided_identity} (owner {owner})")
            if source.order_number == PENDING:
                if require_order_numbers:
                    problems.append(f"{item.id}: order number still pending ({source.external_reference})")
            elif source.order_number != fixture.number:
                problems.append(f"{item.id}: order number {source.order_number} != fixture {fixture.number}")

    test_fraction = sum(1 for i in golden.items if i.split == "test") / len(golden.items)
    low, high = TEST_FRACTION_RANGE
    if not low <= test_fraction <= high:
        problems.append(f"test split is {test_fraction:.0%} of items; expected {low:.0%}–{high:.0%}")
    return problems


def summary(golden: GoldenSet) -> list[str]:
    def fmt(counter: Counter) -> str:
        return ", ".join(f"{k}={v}" for k, v in sorted(counter.items()))

    return [
        f"items: {len(golden.items)}",
        f"intent: {fmt(Counter(i.intent for i in golden.items))}",
        f"query_style: {fmt(Counter(i.query_style for i in golden.items))}",
        f"split: {fmt(Counter(i.split for i in golden.items))}",
        f"status: {fmt(Counter(i.status for i in golden.items))}",
        f"author: {fmt(Counter(i.author for i in golden.items))}",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", type=Path, default=GOLDEN_PATH)
    parser.add_argument("--require-order-numbers", action="store_true", help="treat 'pending' order numbers as errors")
    parser.add_argument("--expected-test-hash", help="fail if the test-split SHA-256 differs")
    args = parser.parse_args(argv)

    try:
        golden = load_golden(args.path)
    except ValidationError as exc:
        print(f"INVALID schema in {args.path}:\n{exc}", file=sys.stderr)
        return 1

    problems = check(golden, load_catalog(), load_orders(), args.require_order_numbers)
    digest = test_split_hash(golden)
    if args.expected_test_hash and digest != args.expected_test_hash:
        problems.append(f"test-split hash {digest} != expected {args.expected_test_hash} (test split changed)")

    for line in summary(golden):
        print(line)
    pending = sum(1 for i in golden.items if i.gold_source.type == "order" and i.gold_source.order_number == PENDING)
    if pending:
        print(f"order numbers pending: {pending} items (allowed until scripts/seed_orders.py has run)")
    print(f"test-split sha256: {digest}")
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    if problems:
        print(f"FAIL: {len(problems)} problems")
        return 1
    print("OK: golden set is valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
