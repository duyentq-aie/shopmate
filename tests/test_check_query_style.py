"""check_query_style: heuristics, and it never writes the golden file."""

from __future__ import annotations

import json

import pytest

from eval.golden.check_query_style import main, suggest


@pytest.mark.parametrize(
    "question, expected",
    [
        # rule 3: diacritics, no abbreviations
        ("Shop có áo sơ mi nam màu xanh dương không?", "co_dau"),
        ("giày bong keo thì bảo hành bao lâu shop", "co_dau"),
        ("phi ship nhiêu vậy shop", "co_dau"),  # "ship" is a loanword; one typo ("phi") is not khong_dau
        ("Mình cần váy liền dưới 400k", "co_dau"),  # price "400k" is not the abbreviation "k"
        ("Đơn {order_number}, email khachhang10@example.com", "co_dau"),
        # rule 2: diacritics + a chat abbreviation
        ("Shop ơi, có sơ mi nam màu xanh dương ko?", "teencode"),
        ("dc kiểm hàng trc khi nhận k shop", "teencode"),
        ("đơn trên 500k có free ship hk ạ", "teencode"),
        ("shop check giúp e đơn {order_number} với ạ", "teencode"),  # "e" (em)
        ("ao thun nam XXL còn hàng ko shop", "teencode"),  # one missing diacritic does not make it khong_dau
        # rule 1 is checked first: mostly without diacritics, even with abbreviations
        ("tim giay sneaker nu size 37", "khong_dau"),
        ("tui deo cheo duoi 400k co ko shop", "khong_dau"),
        ("cam on shop nhìu nha", "khong_dau"),
        ("kiem tra don {order_number} giup minh, email khachhang01@example.com", "khong_dau"),
    ],
)
def test_suggestions(question, expected):
    assert suggest(question)[0] == expected


def test_never_modifies_the_file(tmp_path, capsys):
    path = tmp_path / "golden.json"
    items = [
        {"id": "gs-001", "question": "Shop ơi có áo ko?", "query_style": "co_dau"},
        {"id": "gs-002", "question": "tim giay nu", "query_style": "khong_dau"},
    ]
    path.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    assert main(["--path", str(path)]) == 0
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    out = capsys.readouterr().out
    assert "gs-001  label=co_dau  suggested=teencode" in out and "gs-002" not in out
    assert "1 of 2 items flagged" in out
