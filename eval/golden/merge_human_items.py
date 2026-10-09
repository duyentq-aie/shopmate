"""Merge human-written items from eval/golden/human_items.toml into eval/golden/golden.vi.json.

The human writes content (question, intent, query_style, source fields); this script
computes the gold labels:
- product_search: gold SKUs = apply_filters(catalog, expected_filters); no match -> no_answer;
- policy_question: policy file + clause, checked against data/policies/;
- order items: order number and status from data/seed/orders.vi.json; provided_identity from
  the email the question gives (compared with the order's owner), which fixes the expected
  behavior (answer / ask_for_verification / refuse, see LABELING.md, D28 and D37). v1 is
  email-only: a question that gives only a phone expects ask_for_verification;
- author = human, status = human_reviewed.

Safety:
- the agent-drafted items (author agent_draft, gs-001 ... gs-064) are never modified, except
  their `split` (re-split below); any other change aborts before writing;
- human items are replaced from the TOML on every run (idempotent); a human item that is in
  the golden file but missing from the TOML aborts the merge (nothing is deleted silently);
- the merged set must pass the strict validator before anything is written.

Split: stratified by intent x query_style over ALL items (allowed because no system has run
on the test split yet). Within each stratum, items are ranked by sha256(id) and the first
floor(0.3 * n + 0.5) go to test. The cell table is printed for information only; the
checks are on the MARGINS of the test split (MIN_TEST_PER_STYLE, MIN_TEST_PER_INTENT).
The test-split hash is printed but NOT frozen.

Usage (from the repo root):
    uv run python -m eval.golden.merge_human_items --dry-run
    uv run python -m eval.golden.merge_human_items
"""

from __future__ import annotations

import argparse
import hashlib
import math
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.catalog.schema import Catalog, load_catalog
from app.orders.schema import OrderFixtures, load_orders
from eval.golden import validate_golden
from eval.golden.draft_golden import render
from eval.golden.schema import (
    GOLDEN_PATH,
    IDENTITY_BEHAVIOR,
    GoldenItem,
    GoldenSet,
    Intent,
    ProductFilters,
    QueryStyle,
    apply_filters,
    load_golden,
    test_split_hash,
)

HUMAN_ITEMS_PATH = GOLDEN_PATH.parent / "human_items.toml"
FROZEN_TEST_SPLIT = GOLDEN_PATH.parent / "test_split.sha256"  # present once the test split is frozen
FIRST_HUMAN_ID = 101
TEST_FRACTION = 0.3
# Marginal checks on the test split (warnings; the cells themselves are not checked).
MIN_TEST_PER_STYLE = 8
MIN_TEST_PER_INTENT = {"product_search": 6, "policy_question": 6}
MIN_TEST_OTHER_INTENT = 2
NO_MATCH_REASON = "Không có sản phẩm nào khớp bộ lọc."


class MergeError(Exception):
    """The input or the merge result is not acceptable; nothing was written."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PolicyRef(_Strict):
    file: str
    clause: int = Field(ge=1)

    @property
    def document(self) -> str:
        return self.file.removesuffix(".md")


class OrderRef(_Strict):
    external_reference: str = Field(pattern=r"^shopmate-seed-order-\d{3}$")
    email: str | None = None
    phone: str | None = None


class HumanItem(_Strict):
    id: str = Field(pattern=r"^gs-\d{3}$")
    question: str = Field(min_length=2)
    intent: Intent
    query_style: QueryStyle
    notes: str | None = None
    expected_filters: ProductFilters | None = None
    policy: PolicyRef | None = None
    order: OrderRef | None = None
    no_answer: str | None = None

    @model_validator(mode="after")
    def _one_matching_source(self) -> "HumanItem":
        if int(self.id[3:]) < FIRST_HUMAN_ID:
            raise ValueError(f"{self.id}: human items start at gs-{FIRST_HUMAN_ID}")
        given = [k for k in ("expected_filters", "policy", "order", "no_answer") if getattr(self, k) is not None]
        allowed = {
            "product_search": {"expected_filters"},
            "policy_question": {"policy"},
            "order_status": {"order"},
            "create_ticket": {"order", "no_answer"},
            "out_of_scope": {"no_answer"},
            "smalltalk": {"no_answer"},
        }[self.intent]
        if len(given) != 1 or given[0] not in allowed:
            raise ValueError(f"{self.id}: intent {self.intent} needs exactly one of {sorted(allowed)}, got {given}")
        if self.order:
            for value in (self.order.email, self.order.phone):
                if value and value not in self.question:
                    raise ValueError(f"{self.id}: {value!r} is given in `order` but does not appear in the question")
        return self


def load_human_items(path: Path = HUMAN_ITEMS_PATH) -> list[HumanItem]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown = set(data) - {"items"}
    if unknown:
        raise MergeError(f"{path}: unexpected top-level keys {sorted(unknown)}")
    try:
        items = [HumanItem.model_validate(raw) for raw in data.get("items", [])]
    except ValidationError as exc:
        raise MergeError(f"{path}: {exc}") from exc
    ids = [i.id for i in items]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise MergeError(f"{path}: duplicate ids {dupes}")
    return items


def _identity(order: OrderRef, owner_email: str) -> str:
    """v1 verification is email-only (D37): a phone never matches and never refuses; it means
    'ask for the email', exactly like giving no identity at all."""
    if order.email:
        return "matching_email" if order.email.lower() == owner_email.lower() else "mismatched_email"
    if order.phone:
        return "phone_only"
    return "none"


def to_golden_item(item: HumanItem, catalog: Catalog, orders: OrderFixtures) -> dict:
    if item.expected_filters is not None:
        skus = apply_filters(catalog, item.expected_filters)
        source = {"type": "skus", "skus": skus} if skus else {"type": "no_answer", "reason": NO_MATCH_REASON}
    elif item.policy is not None:
        source = {"type": "policy_clause", "document": item.policy.document, "clause": item.policy.clause}
    elif item.order is not None:
        fixture = orders.by_reference().get(item.order.external_reference)
        if fixture is None:
            raise MergeError(f"{item.id}: unknown order {item.order.external_reference}")
        identity = _identity(item.order, fixture.customer.email)
        source = {
            "type": "order",
            "external_reference": fixture.external_reference,
            "order_number": fixture.number,
            "expected_status": fixture.status,
            "provided_identity": identity,
            "expected_behavior": IDENTITY_BEHAVIOR[identity],
        }
    else:
        source = {"type": "no_answer", "reason": item.no_answer}
    golden = {
        "id": item.id,
        "question": item.question,
        "intent": item.intent,
        "query_style": item.query_style,
        "gold_source": source,
        "split": "dev",  # reassigned by stratified_split
        "status": "human_reviewed",
        "author": "human",
    }
    if item.expected_filters is not None:
        golden["expected_filters"] = item.expected_filters.model_dump(exclude_none=True)
    if item.notes:
        golden["notes"] = item.notes
    return golden


def stratified_split(items: list[dict]) -> dict[tuple[str, str], tuple[int, int]]:
    """Assign `split` in place; return {(intent, query_style): (n_dev, n_test)}."""
    strata: dict[tuple[str, str], list[dict]] = {}
    for item in items:
        strata.setdefault((item["intent"], item["query_style"]), []).append(item)
    counts = {}
    for key, group in sorted(strata.items()):
        ranked = sorted(group, key=lambda i: hashlib.sha256(i["id"].encode()).hexdigest())
        n_test = math.floor(TEST_FRACTION * len(group) + 0.5)  # half-up, no banker's rounding
        for rank, item in enumerate(ranked):
            item["split"] = "test" if rank < n_test else "dev"
        counts[key] = (len(group) - n_test, n_test)
    return counts


def merge(golden: GoldenSet, human: list[HumanItem], catalog: Catalog, orders: OrderFixtures):
    """Return (merged GoldenSet, per-stratum counts). Raises MergeError; writes nothing."""
    existing = golden.model_dump(mode="json", exclude_none=True)["items"]
    agent = [i for i in existing if i["author"] == "agent_draft"]
    previous_human = {i["id"] for i in existing if i["author"] == "human"}

    agent_ids = {i["id"] for i in agent}
    clash = sorted(agent_ids & {h.id for h in human})
    if clash:
        raise MergeError(f"refusing to overwrite agent-drafted items {clash}")
    dropped = sorted(previous_human - {h.id for h in human})
    if dropped:
        raise MergeError(f"human items {dropped} are in the golden file but not in the TOML; "
                         f"nothing is deleted automatically")

    items = [dict(i) for i in agent] + [to_golden_item(h, catalog, orders) for h in human]
    items.sort(key=lambda i: i["id"])
    counts = stratified_split(items)
    merged = GoldenSet.model_validate({"description": golden.description, "items": items})

    # The agent-drafted items must be unchanged apart from `split`.
    after = {i.id: i.model_dump(mode="json", exclude_none=True) for i in merged.items}
    for before in agent:
        a, b = dict(after[before["id"]]), dict(before)
        a.pop("split"), b.pop("split")
        if a != b:
            raise MergeError(f"internal error: {before['id']} would change beyond its split")

    problems = validate_golden.check(merged, catalog, orders, require_order_numbers=True)
    if problems:
        raise MergeError("merged set fails validation:\n  " + "\n  ".join(problems))
    return merged, counts


def marginal_checks(merged: GoldenSet) -> tuple[list[str], list[str]]:
    """Return (table lines, warnings) for test-split counts per query_style and per intent."""
    lines, warnings = [], []
    for field, values, minimum in (
        ("query_style", ("co_dau", "khong_dau", "teencode"), lambda v: MIN_TEST_PER_STYLE),
        ("intent", ("product_search", "policy_question", "order_status", "create_ticket", "out_of_scope", "smalltalk"),
         lambda v: MIN_TEST_PER_INTENT.get(v, MIN_TEST_OTHER_INTENT)),
    ):
        lines.append(f"{field:18} {'n':>3} {'dev':>4} {'test':>5} {'min':>4}")
        for value in values:
            group = [i for i in merged.items if getattr(i, field) == value]
            n_test = sum(1 for i in group if i.split == "test")
            need = minimum(value)
            ok = n_test >= need
            lines.append(f"  {value:16} {len(group):>3} {len(group) - n_test:>4} {n_test:>5} {need:>4}  {'ok' if ok else 'LOW'}")
            if not ok:
                warnings.append(f"{field}={value}: {n_test} test items, need >= {need}")
    return lines, warnings


def report(merged: GoldenSet, counts: dict[tuple[str, str], tuple[int, int]]) -> list[str]:
    lines = ["cells (intent x query_style), for information only:",
             f"  {'intent':16} {'query_style':10} {'n':>3} {'dev':>4} {'test':>5}"]
    for (intent, style), (n_dev, n_test) in counts.items():
        lines.append(f"  {intent:16} {style:10} {n_dev + n_test:>3} {n_dev:>4} {n_test:>5}")
    margin_lines, warnings = marginal_checks(merged)
    lines += ["", "marginal checks on the test split:"] + margin_lines
    split = Counter(i.split for i in merged.items)
    authors = Counter(i.author for i in merged.items)
    lines.append(f"total: {len(merged.items)} items (agent_draft {authors['agent_draft']}, human {authors['human']}); "
                 f"dev {split['dev']}, test {split['test']} ({split['test'] / len(merged.items):.0%})")
    lines += [f"WARNING: {w}" for w in warnings]
    lines.append(f"test-split sha256 (NOT frozen): {test_split_hash(merged)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=HUMAN_ITEMS_PATH)
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH)
    parser.add_argument("--dry-run", action="store_true", help="validate and print counts; write nothing")
    args = parser.parse_args(argv)

    try:
        human = load_human_items(args.input)
        merged, counts = merge(load_golden(args.golden), human, load_catalog(), load_orders())
    except MergeError as exc:
        print(f"REFUSED (nothing written): {exc}", file=sys.stderr)
        return 1

    for line in report(merged, counts):
        print(line)
    if args.dry_run:
        print(f"dry run: {args.golden} not modified")
        return 0
    frozen = args.golden.with_name(FROZEN_TEST_SPLIT.name)
    if frozen.is_file() and test_split_hash(merged) != frozen.read_text(encoding="utf-8").strip():
        print(f"REFUSED (nothing written): the test split is frozen ({frozen}) and this merge would change it. "
              f"Re-freezing is a deliberate decision; see docs/SETUP.md.", file=sys.stderr)
        return 1
    args.golden.write_bytes(render(merged).encode("utf-8"))
    print(f"merged {len(human)} human items into {args.golden}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
