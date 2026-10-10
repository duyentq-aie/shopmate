"""Client-side limits for the Gemini free tier: requests/minute, tokens/minute, and the daily ceiling that is
tracked across runs (Pacific-time day, like Google's quota reset). Fake clock and sleep: nothing really waits.
"""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from app.llm.errors import DailyBudgetReached
from app.llm.limits import DailyBudget, RateLimiter


class FakeTime:
    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _limiter(t, rpm=12, tpm=200_000):
    return RateLimiter(rpm=rpm, tpm=tpm, clock=t.clock, sleep=t.sleep)


def test_requests_inside_the_per_minute_budget_never_wait():
    t = FakeTime()
    limiter = _limiter(t)
    for _ in range(12):
        limiter.acquire(100)
    assert t.sleeps == []


def test_the_request_over_the_budget_waits_until_the_oldest_one_leaves_the_window():
    t = FakeTime()
    limiter = _limiter(t)
    for n in range(12):
        t.now = 1000.0 + n  # one request per second: the first leaves the 60 s window at 1060
        limiter.acquire(100)
    t.now = 1011.5
    limiter.acquire(100)  # the 13th must wait for the first (sent at 1000.0) to age out
    assert t.sleeps and t.now >= 1060.0 and t.now < 1062.0
    assert sum(t.sleeps) == pytest.approx(t.now - 1011.5)


def test_the_window_slides_it_does_not_reset_on_the_minute():
    t = FakeTime()
    limiter = _limiter(t, rpm=2)
    limiter.acquire(1)  # at 1000
    t.now = 1030.0
    limiter.acquire(1)  # at 1030
    t.now = 1059.0
    limiter.acquire(1)  # a third inside any 60 s window containing 1000 and 1030: waits until 1060
    assert t.now >= 1060.0 and t.now < 1061.0


def test_the_token_budget_limits_too():
    t = FakeTime()
    limiter = _limiter(t, rpm=100, tpm=1000)
    limiter.acquire(600)
    limiter.acquire(300)
    assert t.sleeps == []
    limiter.acquire(300)  # 1200 tokens inside a minute: waits for the 600-token request to age out
    assert t.now >= 1060.0


def test_a_single_request_larger_than_the_token_budget_is_refused_not_waited_for():
    with pytest.raises(ValueError, match="tokens"):
        _limiter(FakeTime(), tpm=1000).acquire(1001)


PT = ZoneInfo("America/Los_Angeles")


def _budget(tmp_path, when, limit=450):
    return DailyBudget(tmp_path, limit=limit, now=lambda: when)


def test_every_reservation_is_counted_and_survives_a_new_process(tmp_path):
    when = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
    budget = _budget(tmp_path, when)
    assert budget.used() == 0
    assert [budget.reserve() for _ in range(3)] == [1, 2, 3]
    assert _budget(tmp_path, when).used() == 3  # a fresh object reads the same file
    (path,) = tmp_path.glob("usage-*.json")
    assert json.loads(path.read_text(encoding="utf-8"))["count"] == 3


def test_the_ceiling_stops_before_the_request_is_sent(tmp_path):
    when = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
    budget = _budget(tmp_path, when, limit=3)
    for _ in range(3):
        budget.reserve()
    with pytest.raises(DailyBudgetReached, match="3"):
        budget.reserve()
    assert budget.used() == 3  # the refused reservation is not counted


def test_the_day_is_the_pacific_time_day_not_the_utc_day(tmp_path):
    before = dt.datetime(2026, 10, 10, 6, 59, tzinfo=dt.timezone.utc)  # 23:59 PDT on 2026-10-09
    after = dt.datetime(2026, 10, 10, 7, 1, tzinfo=dt.timezone.utc)  # 00:01 PDT on 2026-10-10
    assert before.astimezone(PT).date() == dt.date(2026, 10, 9) and after.astimezone(PT).date() == dt.date(2026, 10, 10)
    _budget(tmp_path, before).reserve()
    assert _budget(tmp_path, before).used() == 1 and _budget(tmp_path, after).used() == 0  # a new day starts at 0
    assert (tmp_path / "usage-2026-10-09.json").is_file()


def test_a_corrupt_counter_stops_the_run_instead_of_resetting_to_zero(tmp_path):
    when = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
    (tmp_path / "usage-2026-10-10.json").write_text("{ broken", encoding="utf-8")
    with pytest.raises(DailyBudgetReached, match="counter"):
        _budget(tmp_path, when).reserve()
