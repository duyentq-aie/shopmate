"""Cost of a Gemini call as Langfuse is told it: the theoretical paid-tier list price (USD per million tokens), the same table the
evaluation reports use (eval/results/phase8_dev/answer_v2.json, docs/RESULTS-phase5.md). The free tier really charges nothing; the
number is what the same traffic would cost on the paid tier, so a cost chart means something. A cache hit was not billed again."""

from __future__ import annotations

PRICE_INPUT_PER_MILLION = 0.30
PRICE_OUTPUT_PER_MILLION = 2.50
COST_BASIS = "theoretical paid-tier list price; the free tier charges nothing"


def cost_details(tokens_in: int | None, tokens_out: int | None, *, cache_hit: bool | None) -> dict[str, float] | None:
    """{input, output, total} in USD for the tokens of one call, or None when the response had no usageMetadata (never an estimate)."""
    if tokens_in is None and tokens_out is None:
        return None
    if cache_hit:
        return {"input": 0.0, "output": 0.0, "total": 0.0}
    cost_in = (tokens_in or 0) * PRICE_INPUT_PER_MILLION / 1_000_000
    cost_out = (tokens_out or 0) * PRICE_OUTPUT_PER_MILLION / 1_000_000
    return {"input": cost_in, "output": cost_out, "total": cost_in + cost_out}
