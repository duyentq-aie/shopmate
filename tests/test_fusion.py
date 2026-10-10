"""Client-side Reciprocal Rank Fusion (Phase 4). The formula is Qdrant's own, verified against the server:
score(doc) = sum over the branches that rank it within the window of 1 / (rank0 + 2), rank0 = 0 for the best.
(tests/test_retrieval_integration.py re-checks it against the real server on a fixed fixture.)
Values are exact fractions so that mathematically equal sums are exact ties, not float noise.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.retrieval.fusion import rrf_fuse


def test_one_branch_scores_are_one_over_rank_plus_two():
    assert rrf_fuse([["a", "b", "c"]]) == {"a": Fraction(1, 2), "b": Fraction(1, 3), "c": Fraction(1, 4)}


def test_two_branches_add_up_per_document():
    fused = rrf_fuse([["a", "b", "c"], ["c", "a"]])
    assert fused == {"a": Fraction(1, 2) + Fraction(1, 3),  # rank 0 and rank 1
                     "b": Fraction(1, 3),  # only in the first branch
                     "c": Fraction(1, 4) + Fraction(1, 2)}  # rank 2 and rank 0
    assert fused["a"] == Fraction(5, 6) and fused["c"] == Fraction(3, 4)


def test_documents_beyond_the_window_get_nothing_from_that_branch():
    ranking = [f"d{n}" for n in range(60)]
    fused = rrf_fuse([ranking], window=50)
    assert len(fused) == 50 and fused["d49"] == Fraction(1, 51) and "d50" not in fused
    assert rrf_fuse([ranking], window=3) == {"d0": Fraction(1, 2), "d1": Fraction(1, 3), "d2": Fraction(1, 4)}


def test_mathematically_equal_sums_are_exactly_equal():
    # p: rank 0 and rank 4 -> 1/2 + 1/6 ; q: rank 1 and rank 1 -> 1/3 + 1/3 ; both are exactly 2/3
    first = ["p", "q", "f1", "f2", "f3"]
    second = ["g0", "q", "g2", "g3", "p"]
    fused = rrf_fuse([first, second])
    assert fused["p"] == fused["q"] == Fraction(2, 3)


def test_an_empty_branch_changes_nothing():
    assert rrf_fuse([["a", "b"], []]) == rrf_fuse([["a", "b"]])
    assert rrf_fuse([]) == {}


def test_a_document_listed_twice_in_one_branch_is_an_error():
    with pytest.raises(ValueError, match="twice"):
        rrf_fuse([["a", "b", "a"]])

