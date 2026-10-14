"""Cost of a Gemini call as Langfuse is told it (docs/DECISIONS-langfuse.md): the theoretical paid-tier price, the same one the evaluation
reports use. The price table is pinned to the committed dev report, so there is one source and no second number to drift."""

from __future__ import annotations

import json
from pathlib import Path

from app.observability.pricing import COST_BASIS, PRICE_INPUT_PER_MILLION, PRICE_OUTPUT_PER_MILLION, cost_details

REPO = Path(__file__).resolve().parent.parent


def test_the_prices_are_the_ones_of_the_committed_dev_report():
    report = json.loads((REPO / "eval" / "results" / "phase8_dev" / "answer_v2.json").read_text(encoding="utf-8"))
    prices = report["deterministic"]["cost_theoretical_paid_tier"]["prices_usd_per_million_tokens"]
    assert (PRICE_INPUT_PER_MILLION, PRICE_OUTPUT_PER_MILLION) == (prices["input"], prices["output"])


def test_cost_is_input_and_output_tokens_times_their_price_per_million():
    cost = cost_details(1_000_000, 1_000_000, cache_hit=False)
    assert cost == {"input": 0.30, "output": 2.50, "total": 2.80}
    small = cost_details(700, 8, cache_hit=False)
    assert small["input"] == 700 * 0.30 / 1_000_000 and small["output"] == 8 * 2.50 / 1_000_000
    assert small["total"] == small["input"] + small["output"]


def test_a_cache_hit_was_not_billed_so_it_costs_nothing_whatever_its_recorded_tokens():
    assert cost_details(700, 8, cache_hit=True) == {"input": 0.0, "output": 0.0, "total": 0.0}


def test_a_response_without_usage_metadata_has_no_cost_rather_than_an_estimated_one():
    assert cost_details(None, None, cache_hit=False) is None
    assert cost_details(None, None, cache_hit=None) is None


def test_one_missing_side_counts_as_zero_tokens_but_the_other_side_is_still_priced():
    assert cost_details(1000, None, cache_hit=False) == {"input": 1000 * 0.30 / 1_000_000, "output": 0.0, "total": 1000 * 0.30 / 1_000_000}


def test_the_basis_says_this_is_a_list_price_and_not_what_the_free_tier_charged():
    assert "paid" in COST_BASIS and "free" in COST_BASIS
