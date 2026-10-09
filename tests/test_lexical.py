"""Folded lexical vector: BM25-style weights on diacritic-folded syllables (Phase 4, experiment 1).

Document side: tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avg_len)) per token, where dl is the
document's folded token count and avg_len is PINNED in the config (not measured per run).
Query side: 1.0 per distinct token. The IDF factor is applied by Qdrant (modifier=idf), not here.
Expected numbers are worked out by hand in the comments.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.retrieval.folding import FOLDING_VERSION
from app.retrieval.lexical import LexicalConfig, TokenIdCollision, check_no_collisions, doc_vector, query_vector, token_id

# avg_len 4 makes a 4-token document "average length": the length normaliser is 1 - b + b * 4/4 = 1.
CFG = LexicalConfig(k1=1.2, b=0.75, avg_len=4.0)
# A real 32-bit blake2b collision between two distinct tokens (found by brute force).
COLLIDING = ("w52890", "w55286")


def test_token_ids_are_stable_u32_values():
    # These ids are stored in the index; changing the scheme must go through the fingerprint.
    assert token_id("tui") == 1147751854
    assert token_id("ao") == 2676719799
    assert all(0 <= token_id(t) < 2**32 for t in ("tui", "ao", "400k", "w1"))


def test_document_weights_by_hand():
    # "ao ao thun tay": dl = 4 -> normaliser 1. ao: tf 2 -> 2*2.2/(2+1.2) = 1.375; thun, tay: 2.2/2.2 = 1.0
    vector = doc_vector("ao ao thun tay", CFG)
    assert vector == pytest.approx({token_id("ao"): 1.375, token_id("thun"): 1.0, token_id("tay"): 1.0})


def test_longer_documents_get_lower_weights_and_shorter_ones_higher():
    long_doc = doc_vector("ao a b c d e f g", CFG)  # dl = 8: normaliser 0.25 + 0.75*2 = 1.75 -> 2.2/(1+1.2*1.75)
    short_doc = doc_vector("ao tay", CFG)  # dl = 2: normaliser 0.25 + 0.75*0.5 = 0.625 -> 2.2/(1+1.2*0.625)
    assert long_doc[token_id("ao")] == pytest.approx(2.2 / 3.1)
    assert short_doc[token_id("ao")] == pytest.approx(2.2 / 1.75)


def test_term_frequency_saturates_without_length_normalisation():
    no_norm = LexicalConfig(k1=1.2, b=0.0, avg_len=4.0)
    weights = [doc_vector(" ".join(["ao"] * n), no_norm)[token_id("ao")] for n in (1, 2, 3, 50)]
    # tf*2.2/(tf+1.2): 1.0, 1.375, 6.6/4.2, 110/51.2 -- rises, but never reaches k1 + 1 = 2.2
    assert weights == pytest.approx([1.0, 1.375, 6.6 / 4.2, 110 / 51.2])
    assert weights == sorted(weights) and weights[-1] < 2.2


def test_documents_and_queries_ignore_diacritics_and_case():
    plain = doc_vector("tui deo cheo", CFG)
    assert doc_vector("Túi đeo chéo", CFG) == doc_vector("TÚI ĐEO CHÉO", CFG) == plain
    assert query_vector("Túi ĐEO chéo") == query_vector("tui deo cheo")


def test_query_vector_is_binary_over_distinct_tokens():
    assert query_vector("Áo ao thun ao") == {token_id("ao"): 1.0, token_id("thun"): 1.0}


@pytest.mark.parametrize("text", ["", "???", "😀"])
def test_text_without_tokens_gives_an_empty_vector(text):
    assert doc_vector(text, CFG) == {} and query_vector(text) == {}


def test_document_and_query_share_token_ids_so_a_dot_product_is_meaningful():
    # unaccented query token meets the accented document token on the same index
    assert set(query_vector("tui")) <= set(doc_vector("Túi đeo chéo", CFG))


def test_default_config_tracks_the_folding_version():
    assert LexicalConfig().folding_version == FOLDING_VERSION


def test_fingerprint_is_stable_and_covers_every_setting():
    base = LexicalConfig()
    assert base.fingerprint() == LexicalConfig().fingerprint() and len(base.fingerprint()) == 16
    for change in ({"k1": 1.5}, {"b": 0.5}, {"avg_len": 80.0}, {"folding_version": 99}, {"tokenizer": "other"},
                   {"token_id": "other"}):
        assert dataclasses.replace(base, **change).fingerprint() != base.fingerprint(), change


@pytest.mark.parametrize("bad", [{"k1": -0.1}, {"b": 1.1}, {"b": -0.1}, {"avg_len": 0.0}])
def test_invalid_parameters_are_rejected(bad):
    with pytest.raises(ValueError):
        LexicalConfig(**bad)


def test_collision_between_distinct_tokens_is_detected():
    assert token_id(COLLIDING[0]) == token_id(COLLIDING[1])
    with pytest.raises(TokenIdCollision, match="w52890.*w55286"):
        check_no_collisions(["tui", *COLLIDING, "ao"])
    check_no_collisions(["tui", "ao", "tui"])  # repeats of one token are not collisions
