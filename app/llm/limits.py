"""Client-side limits for the Gemini free tier.

RateLimiter: sliding 60-second window over requests AND (estimated) tokens. It waits instead of sending a request
that would exceed the budget; the budget is set below the API's own limits (the owner's AI Studio numbers for
gemini-3.5-flash-lite: ~15 RPM, ~250K TPM, 500 RPD).

DailyBudget: counts every request sent (a retry is a request too) in a file per Pacific-time day, across runs
and processes, and refuses to send once the ceiling is reached (450 of the 500 requests per day, leaving headroom).
A corrupt counter file stops the run: silently resetting it to 0 would defeat the guard.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

from app.llm.errors import DailyBudgetReached

WINDOW_SECONDS = 60.0
PACIFIC = ZoneInfo("America/Los_Angeles")  # Google resets the daily quota at midnight Pacific time


class RateLimiter:
    def __init__(self, rpm: int = 12, tpm: int = 200_000, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.rpm, self.tpm, self._clock, self._sleep = rpm, tpm, clock, sleep
        self._sent: deque[tuple[float, int]] = deque()  # (time, estimated tokens)

    def acquire(self, tokens: int) -> None:
        if tokens > self.tpm:
            raise ValueError(f"one request of ~{tokens} tokens cannot fit the {self.tpm} tokens/minute budget")
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0][0] >= WINDOW_SECONDS:
                self._sent.popleft()
            if len(self._sent) < self.rpm and sum(t for _, t in self._sent) + tokens <= self.tpm:
                self._sent.append((now, tokens))
                return
            self._sleep(max(0.0, self._sent[0][0] + WINDOW_SECONDS - now) + 0.01)


class DailyBudget:
    def __init__(self, directory: Path, limit: int = 450,
                 now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)) -> None:
        self.directory, self.limit, self._now = Path(directory), limit, now

    def _path(self) -> Path:
        return self.directory / f"usage-{self._now().astimezone(PACIFIC).date().isoformat()}.json"

    def used(self) -> int:
        path = self._path()
        if not path.is_file():
            return 0
        try:
            return int(json.loads(path.read_text(encoding="utf-8"))["count"])
        except (OSError, ValueError, KeyError, TypeError):
            raise DailyBudgetReached(f"the daily request counter {path.name} is unreadable; "
                                     f"fix or delete it deliberately, it is not reset automatically") from None

    def reserve(self) -> int:
        """Count one request BEFORE it is sent. Raises DailyBudgetReached at the ceiling (and counts nothing)."""
        used = self.used()
        if used >= self.limit:
            raise DailyBudgetReached(f"{used} of {self.limit} requests used today (Pacific time): not sending more")
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump({"date": path.stem.removeprefix("usage-"), "count": used + 1, "ceiling": self.limit}, out)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return used + 1
