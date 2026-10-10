"""Errors of the LLM layer. Everything the free-tier guards raise derives from LLMError.

Guard errors ABORT a run (they are never swallowed into a per-item "failure"): an evaluation that hit a limit
must stop and say so, not quietly continue with fewer answers.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class."""


class ApiError(LLMError):
    """A non-retryable HTTP error from the API (message already scrubbed of the key)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class DailyLimitExhausted(LLMError):
    """The API says the DAILY quota is used up: stop, do not retry in a loop."""


class FreeTierUnavailable(LLMError):
    """The API reports a limit of 0 for this model/key: the free tier does not cover it. Stop."""


class RetriesExhausted(LLMError):
    """Bounded retries (per-minute 429, 5xx, network) did not succeed.

    `cause` is what the LAST attempt failed with: "rate_limit" (a 429 that never recovered), "server_error" (5xx) or
    "network"; None when the raiser did not say. Tracing uses it to name the failure truthfully."""

    CAUSES = ("rate_limit", "server_error", "network")

    def __init__(self, message: str, *, cause: str | None = None) -> None:
        super().__init__(message)
        if cause is not None and cause not in self.CAUSES:
            raise ValueError(f"unknown retry failure cause {cause!r}")
        self.cause = cause


class DailyBudgetReached(LLMError):
    """Our own daily ceiling (below the API's) is reached; no request was sent."""


class CallCapExceeded(LLMError):
    """The per-run cap on live requests is reached; no request was sent."""


class CacheMiss(LLMError):
    """Offline mode and no cached response for this request."""


class CacheCorrupt(LLMError):
    """A cache file is unreadable or does not belong to its key."""


class BadResponse(LLMError):
    """The API answered 200 but the answer has no usable structure (blocked, empty, not JSON)."""
