"""LIVE checks against the real Gemini API (free tier). NOT part of scripts/test_all.sh and not run by default
(`uv run pytest -m live_llm`). Each test spends 1 request of the daily quota (500/day, 450 allowed by our ceiling),
counted by the same DailyBudget as the evaluation. Hand-written questions only; the responses go to a temporary cache.
"""

from __future__ import annotations

import pytest

from app.config import get_setting
from app.llm.cache import DiskCache
from app.llm.client import MODEL_ID, GeminiClient
from app.llm.errors import ApiError
from app.llm.extractor import FilterExtractor
from app.llm.generator import CachedGenerator
from app.llm.intents import INTENTS
from app.llm.limits import DailyBudget, RateLimiter
from app.llm.prompts import load_prompt
from app.llm.router import ROUTER_SCHEMA, GeminiRouter
from eval.run_phase5_eval import DAILY_CEILING, RPM, TPM, USAGE_DIR

pytestmark = pytest.mark.live_llm


@pytest.fixture(scope="module")
def client():
    key = get_setting("GEMINI_API_KEY")
    if not key:
        pytest.skip("GEMINI_API_KEY is not set")
    return GeminiClient(key, limiter=RateLimiter(rpm=RPM, tpm=TPM), budget=DailyBudget(USAGE_DIR, limit=DAILY_CEILING), max_live_requests=10)


def test_the_router_schema_and_prompt_are_accepted_by_the_api(client, tmp_path):
    router = GeminiRouter(CachedGenerator(DiskCache(tmp_path), client), load_prompt("router_v1"))
    result = router.route("shop oi co ao khoac nam mau den khong")
    assert result.error is None and result.intent in INTENTS and result.usage.input_tokens > 0


def test_the_filter_schema_with_closed_nullable_enums_is_accepted_by_the_api(client, tmp_path):
    extractor = FilterExtractor(CachedGenerator(DiskCache(tmp_path), client), load_prompt("filters_v1"))
    result = extractor.extract("cho minh xem giay sandal nu mau kem duoi 450k")
    assert result.error is None and result.filters is not None


def test_the_model_still_exposes_no_log_probabilities(client):
    """Documents the finding behind 'no calibrated confidence'. If this ever passes a request, implement a probability."""
    with pytest.raises(ApiError, match="(?i)logprobs"):
        client.generate(system_instruction="Classify.", user_text="chao shop", schema=ROUTER_SCHEMA,
                        generation_config={"temperature": 0, "maxOutputTokens": 64, "responseLogprobs": True, "logprobs": 3})
    assert client.model == MODEL_ID == "gemini-3.5-flash-lite"
