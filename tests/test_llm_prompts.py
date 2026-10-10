"""Versioned prompt files and the closed vocabularies (Phase 5). Hashes of the prompt files go into every result file."""

from __future__ import annotations

import hashlib
import re
from typing import get_args

import pytest

from app.catalog.schema import SIZE_ORDER, load_catalog
from app.llm.prompts import PROMPT_DIR, catalog_vocabulary, load_prompt, render_prompt
from app.llm.intents import INTENTS
from eval.golden.loader import load_split
from eval.golden.schema import Intent


def test_the_hash_of_a_prompt_is_the_sha256_of_its_file_bytes():
    template = load_prompt("router_v1")
    assert template.sha256 == hashlib.sha256((PROMPT_DIR / "router_v1.md").read_bytes()).hexdigest()
    assert template.name == "router_v1" and template.text == (PROMPT_DIR / "router_v1.md").read_text(encoding="utf-8")


def test_an_unknown_prompt_is_an_error():
    with pytest.raises(FileNotFoundError, match="router_v99"):
        load_prompt("router_v99")


def test_the_vocabularies_are_exactly_the_values_of_the_catalog():
    products = load_catalog().products
    vocab = catalog_vocabulary()
    assert set(vocab.categories) == {p.category for p in products} and len(vocab.categories) == 6
    assert set(vocab.kinds) == {p.kind for p in products} and len(vocab.kinds) == 35
    assert set(vocab.colors) == {p.color for p in products} and len(vocab.colors) == 18
    assert set(vocab.materials) == {p.material for p in products} and len(vocab.materials) == 28
    assert set(vocab.sizes) == {s for p in products for s in p.sizes}
    assert list(vocab.sizes) == sorted(vocab.sizes, key=SIZE_ORDER.index)  # canonical order, not alphabetical
    for values in (vocab.categories, vocab.kinds, vocab.colors, vocab.materials):
        assert list(values) == sorted(values)  # a stable order: the schema hash must not depend on dict order


def test_the_vocabulary_hash_changes_when_any_value_changes():
    vocab = catalog_vocabulary()
    import dataclasses

    assert dataclasses.replace(vocab, colors=(*vocab.colors, "Tím than")).sha256() != vocab.sha256()
    assert catalog_vocabulary().sha256() == vocab.sha256()


def test_the_rendered_filter_prompt_lists_every_value_exactly_and_leaves_no_placeholder():
    vocab = catalog_vocabulary()
    rendered = render_prompt(load_prompt("filters_v1"), vocab)
    assert "{{" not in rendered
    for value in (*vocab.categories, *vocab.kinds, *vocab.colors, *vocab.materials, *vocab.sizes):
        assert f'"{value}"' in rendered, value


def test_the_router_prompt_names_every_golden_intent_and_the_injection_rule():
    assert set(INTENTS) == set(get_args(Intent)) and len(INTENTS) == 6  # the router speaks the golden set's intents
    text = load_prompt("router_v1").text
    for intent in INTENTS:
        assert re.search(rf"^- {intent}:", text, re.M), intent
    assert "DATA" in text and "never an instruction" in text


@pytest.mark.parametrize("name", ["router_v1", "filters_v1", "answer_v1", "answer_v2"])
def test_no_dev_question_is_copied_into_a_prompt_example(name):
    text = load_prompt(name).text.lower()
    copied = [i.id for i in load_split("dev") if re.sub(r"\{order_number\}", "", i.question).strip().lower() in text]
    assert copied == []  # examples are invented; the dev set stays a measurement, not training material
