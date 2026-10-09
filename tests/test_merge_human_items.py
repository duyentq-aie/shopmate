"""merge_human_items: computed gold labels, safety rails, stratified split. Never touches the real files."""

from __future__ import annotations

import re
import shutil
import tomllib

import pytest

from app.catalog.schema import load_catalog
from app.orders.schema import load_orders
from eval.golden import merge_human_items as m
from eval.golden.schema import GOLDEN_PATH, apply_filters, load_golden

TEMPLATE = m.HUMAN_ITEMS_PATH


@pytest.fixture
def golden_copy(tmp_path):
    path = tmp_path / "golden.vi.json"
    shutil.copy(GOLDEN_PATH, path)
    return path


def _template_examples() -> str:
    """The commented examples of the template, uncommented."""
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines()
    start = next(n for n, line in enumerate(lines) if "Example 1" in line)
    return "\n".join(re.sub(r"^# ?", "", line) for line in lines[start:] if not line.startswith("# ----"))


def _owner(ref: str) -> str:
    return load_orders().by_reference()[ref].customer.email


def _run(tmp_path, golden_copy, toml_text: str, *extra: str) -> int:
    toml = tmp_path / "human.toml"
    toml.write_text(toml_text, encoding="utf-8")
    return m.main(["--input", str(toml), "--golden", str(golden_copy), *extra])


def test_template_has_no_active_items_and_its_examples_merge(tmp_path, golden_copy):
    assert tomllib.loads(TEMPLATE.read_text(encoding="utf-8")) == {}
    examples = _template_examples()
    assert len(tomllib.loads(examples)["items"]) == 3
    assert _run(tmp_path, golden_copy, examples) == 0
    merged = {i.id: i for i in load_golden(golden_copy).items}
    assert merged["gs-101"].gold_source.skus == ["SM-GIAY-0019", "SM-GIAY-0043"]
    assert merged["gs-102"].gold_source.document == "doi-tra" and merged["gs-102"].gold_source.clause == 2
    order = merged["gs-103"].gold_source
    assert (order.provided_identity, order.expected_behavior, order.order_number) == (
        "matching_email", "answer", load_orders().by_reference()["shopmate-seed-order-020"].number)
    assert {merged[i].author for i in ("gs-101", "gs-102", "gs-103")} == {"human"}
    assert {merged[i].status for i in ("gs-101", "gs-102", "gs-103")} == {"human_reviewed"}


def test_gold_skus_come_from_the_catalog_and_no_match_becomes_no_answer(tmp_path, golden_copy):
    toml = '''
[[items]]
id = "gs-110"
question = "phụ kiện nào dưới 150k còn hàng"
intent = "product_search"
query_style = "co_dau"
[items.expected_filters]
category = "phu-kien"
price_max = 150000
in_stock = true

[[items]]
id = "gs-111"
question = "có giày cao gót xanh lá ko"
intent = "product_search"
query_style = "teencode"
[items.expected_filters]
kind = "Giày cao gót nữ"
color = "Xanh lá"
'''
    assert _run(tmp_path, golden_copy, toml) == 0
    merged = {i.id: i for i in load_golden(golden_copy).items}
    expected = apply_filters(load_catalog(), merged["gs-110"].expected_filters)
    assert merged["gs-110"].gold_source.skus == expected and len(expected) > 3
    assert merged["gs-111"].gold_source.type == "no_answer"


@pytest.mark.parametrize(
    "order_fields, question_suffix, identity, behavior",
    [
        ("", "", "none", "ask_for_verification"),
        (', email = "khachhang99@example.com"', " email khachhang99@example.com", "mismatched_email", "refuse"),
        # v1 is email-only (D37): a phone alone means "ask for the email", never refuse
        (', phone = "0901234567"', " sdt 0901234567", "phone_only", "ask_for_verification"),
        (', email = "khachhang99@example.com", phone = "0901234567"',
         " email khachhang99@example.com sdt 0901234567", "mismatched_email", "refuse"),
    ],
)
def test_order_identity_decides_behavior(tmp_path, golden_copy, order_fields, question_suffix, identity, behavior):
    toml = f'''
[[items]]
id = "gs-120"
question = "đơn {{order_number}} sao rồi{question_suffix}"
intent = "order_status"
query_style = "co_dau"
order = {{ external_reference = "shopmate-seed-order-001"{order_fields} }}
'''
    assert _run(tmp_path, golden_copy, toml) == 0
    source = next(i for i in load_golden(golden_copy).items if i.id == "gs-120").gold_source
    assert (source.provided_identity, source.expected_behavior) == (identity, behavior)


@pytest.mark.parametrize(
    "toml, message",
    [
        ('[[items]]\nid = "gs-030"\nquestion = "x y"\nintent = "smalltalk"\nquery_style = "co_dau"\nno_answer = "r"\n',
         "start at gs-101"),
        ('[[items]]\nid = "gs-130"\nquestion = "chào"\nintent = "smalltalk"\nquery_style = "co_dau"\n'
         'policy = { file = "doi-tra", clause = 1 }\n', "needs exactly one of"),
        ('[[items]]\nid = "gs-131"\nquestion = "đơn {order_number}"\nintent = "order_status"\nquery_style = "co_dau"\n'
         'order = { external_reference = "shopmate-seed-order-001", email = "khachhang01@example.com" }\n',
         "does not appear in the question"),
        ('[[items]]\nid = "gs-132"\nquestion = "đổi trả sao"\nintent = "policy_question"\nquery_style = "co_dau"\n'
         'policy = { file = "doi-tra", clause = 99 }\n', "Điều 99"),
        ('[[items]]\nid = "gs-133"\nquestion = "a b"\nintent = "smalltalk"\nquery_style = "co_dau"\nno_answer = "r"\n'
         '[[items]]\nid = "gs-133"\nquestion = "c d"\nintent = "smalltalk"\nquery_style = "co_dau"\nno_answer = "r"\n',
         "duplicate ids"),
    ],
)
def test_bad_input_is_refused_and_nothing_is_written(tmp_path, golden_copy, capsys, toml, message):
    before = golden_copy.read_bytes()
    assert _run(tmp_path, golden_copy, toml) == 1
    assert message in capsys.readouterr().err
    assert golden_copy.read_bytes() == before


def test_agent_items_only_change_split_and_merge_is_idempotent(tmp_path, golden_copy):
    before = {i.id: i.model_dump(exclude={"split"}) for i in load_golden(golden_copy).items}
    examples = _template_examples()
    assert _run(tmp_path, golden_copy, examples) == 0
    first = golden_copy.read_bytes()
    after = {i.id: i.model_dump(exclude={"split"}) for i in load_golden(golden_copy).items}
    assert all(after[k] == v for k, v in before.items())
    assert _run(tmp_path, golden_copy, examples) == 0
    assert golden_copy.read_bytes() == first


def test_human_item_removed_from_toml_is_not_deleted_silently(tmp_path, golden_copy, capsys):
    assert _run(tmp_path, golden_copy, _template_examples()) == 0
    merged = golden_copy.read_bytes()
    assert _run(tmp_path, golden_copy, "") == 1
    assert "not in the TOML" in capsys.readouterr().err
    assert golden_copy.read_bytes() == merged


def test_dry_run_writes_nothing(tmp_path, golden_copy):
    before = golden_copy.read_bytes()
    assert _run(tmp_path, golden_copy, _template_examples(), "--dry-run") == 0
    assert golden_copy.read_bytes() == before


def test_stratified_split_is_deterministic_and_about_30_percent():
    items = [{"id": f"gs-{n:03d}", "intent": "smalltalk", "query_style": "co_dau"} for n in range(1, 21)]
    items += [{"id": f"gs-{n:03d}", "intent": "out_of_scope", "query_style": "teencode"} for n in range(101, 104)]
    counts = m.stratified_split(items)
    assert counts[("smalltalk", "co_dau")] == (14, 6)
    assert counts[("out_of_scope", "teencode")] == (2, 1)  # floor(0.9 + 0.5) = 1
    first = [i["split"] for i in items]
    m.stratified_split(items)
    assert [i["split"] for i in items] == first


def test_marginal_checks_replace_cell_warnings(tmp_path, golden_copy, capsys):
    assert _run(tmp_path, golden_copy, _template_examples(), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "for information only" in out and "marginal checks on the test split" in out
    assert "WARNING: stratum" not in out and "NOT frozen" in out


def test_marginal_thresholds():
    golden = load_golden()
    lines, warnings = m.marginal_checks(golden)
    rows = {line.split()[0]: line.split() for line in lines if line.startswith("  ")}
    assert rows["product_search"][4] == "6" and rows["policy_question"][4] == "6"
    assert rows["smalltalk"][4] == "2" and rows["teencode"][4] == "8"
    for value, row in rows.items():
        assert (row[5] == "LOW") == any(w.split(":")[0].endswith("=" + value) for w in warnings)


def test_no_item_may_expect_a_phone_to_match():
    from typing import get_args

    from eval.golden.schema import ProvidedIdentity
    assert not any("phone" in v and v != "phone_only" for v in get_args(ProvidedIdentity))
