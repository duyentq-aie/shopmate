"""The dev perturbation set (Phase 4, experiment 1): every dev retrieval item with its question
machine-folded, gold unchanged. Built by code from the DEV split only, saved with a hash.

It is a robustness test, not a substitute for real queries: machine-stripped text has no typos and
no abbreviations beyond what the original already had (recorded in the file itself).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.retrieval.folding import has_diacritics
from eval.golden.loader import items_hash, load_split
from eval.perturbation import dev_folded as pert
from tests.test_eval_phase3b import NO_MATCH, _item

REPO = Path(__file__).resolve().parent.parent

ACCENTED = _item("gs-301", "product_search", {"type": "skus", "skus": ["SM-NAM-0001"]},
                 filters={"kind": "Áo thun nam"}, question="Có váy liền nào dưới 400k ko shop?", style="teencode")
PLAIN = _item("gs-302", "policy_question", {"type": "policy_clause", "document": "van-chuyen", "clause": 1},
              question="phi ship nhieu vay shop", style="khong_dau")
ITEMS = [ACCENTED, PLAIN, NO_MATCH]  # NO_MATCH has no gold: it is not part of the set


def test_questions_are_folded_gold_is_kept_and_unanswerable_items_are_left_out():
    built = pert.build_dev_folded(ITEMS)
    assert [i["id"] for i in built["items"]] == ["gs-301", "gs-302"]
    first = built["items"][0]
    assert first["question"] == "co vay lien nao duoi 400k ko shop?"
    assert first["original_question"] == "Có váy liền nào dưới 400k ko shop?"
    assert first["gold_source"] == {"type": "skus", "skus": ["SM-NAM-0001"]}
    assert (first["intent"], first["query_style"]) == ("product_search", "teencode")
    assert built["items"][1]["question"] == "phi ship nhieu vay shop"  # nothing to fold: unchanged
    assert built["n_items"] == 2 and built["n_changed"] == 1


def test_building_is_deterministic_and_the_hash_covers_everything_else():
    a, b = pert.build_dev_folded(ITEMS), pert.build_dev_folded(list(reversed(ITEMS)))
    assert a == b  # input order does not matter
    assert a["sha256"] == pert.content_sha256(a)
    tampered = json.loads(json.dumps(a))
    tampered["items"][0]["question"] += "x"
    assert pert.content_sha256(tampered) != a["sha256"]


def test_the_set_records_where_it_came_from_and_its_limitation():
    built = pert.build_dev_folded(ITEMS)
    assert built["source"]["split"] == "dev" and built["source"]["folding_version"] == 1
    assert built["source"]["items_sha256"] == items_hash([ACCENTED, PLAIN])
    assert "typos" in built["limitation"] and "robustness" in built["limitation"]


def _write(tmp_path, data):
    path = tmp_path / "dev_folded.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_loader_returns_dev_items_with_the_folded_question(tmp_path):
    path = _write(tmp_path, pert.build_dev_folded(ITEMS))
    items, sha = pert.load_dev_folded([ACCENTED, PLAIN, NO_MATCH], path)
    assert [(i.id, i.question) for i in items] == [("gs-301", "co vay lien nao duoi 400k ko shop?"),
                                                   ("gs-302", "phi ship nhieu vay shop")]
    assert items[0].gold_source == ACCENTED.gold_source and items[0].query_style == "teencode"
    assert sha == pert.build_dev_folded(ITEMS)["sha256"]


def test_loader_refuses_an_edited_file(tmp_path):
    data = pert.build_dev_folded(ITEMS)
    data["items"][0]["question"] = "co vay"
    with pytest.raises(pert.PerturbationError, match="hash"):
        pert.load_dev_folded(ITEMS, _write(tmp_path, data))


def test_loader_refuses_a_file_built_from_other_dev_items(tmp_path):
    path = _write(tmp_path, pert.build_dev_folded(ITEMS))
    changed = ACCENTED.model_copy(update={"question": "Có áo thun nào ko shop?"})
    with pytest.raises(pert.PerturbationError, match="source"):
        pert.load_dev_folded([changed, PLAIN, NO_MATCH], path)


def test_a_test_split_item_can_never_enter_the_set():
    test_item = _item("gs-303", "policy_question", {"type": "policy_clause", "document": "doi-tra", "clause": 1},
                      split="test")
    with pytest.raises(ValueError, match="dev"):
        pert.build_dev_folded([ACCENTED, test_item])


# --- the committed file ---------------------------------------------------------------------------


def _dev_retrieval_items():
    return [i for i in load_split("dev") if i.intent in ("product_search", "policy_question")]


def test_committed_set_has_19_product_and_14_policy_items_all_without_diacritics():
    data = json.loads(pert.PERTURBATION_PATH.read_text(encoding="utf-8"))
    by_intent = {k: sum(i["intent"] == k for i in data["items"]) for k in ("product_search", "policy_question")}
    assert by_intent == {"product_search": 19, "policy_question": 14}  # the answerable dev items of RESULTS-phase3b
    assert data["n_items"] == 33 and not any(has_diacritics(i["question"]) for i in data["items"])
    assert data["n_changed"] == sum(i["question"] != i["original_question"] for i in data["items"])


def test_committed_set_is_exactly_what_the_builder_produces_from_the_dev_split_today():
    committed = json.loads(pert.PERTURBATION_PATH.read_text(encoding="utf-8"))
    assert committed == pert.build_dev_folded(_dev_retrieval_items())  # regenerate with `python -m eval.perturbation.dev_folded`


def test_committed_set_loads_against_the_real_dev_items():
    items, _ = pert.load_dev_folded(_dev_retrieval_items())
    assert len(items) == 33 and all(i.split == "dev" for i in items)


@pytest.mark.parametrize("script", ["eval/perturbation/dev_folded.py"])
def test_perturbation_code_cannot_reach_the_test_split(script):
    source = (REPO / script).read_text(encoding="utf-8")
    assert "load_golden" not in source and "final_evaluation" not in source and "test_split" not in source
