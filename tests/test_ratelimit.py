from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from poe_craft_mcp.http.ratelimit import RateLimited, RateLimiter, parse_triples
from poe_craft_mcp.log import add_file_log

if TYPE_CHECKING:
    from conftest import FakeClock

# Recorded from www.pathofexile.com (see tests/fixtures/trade/search.json).
SEARCH_HEADERS = {
    "X-Rate-Limit-Policy": "trade-search-request-limit",
    "X-Rate-Limit-Rules": "Ip",
    "X-Rate-Limit-Ip": "5:10:60,15:60:300,30:300:1800,600:21600:3600",
    "X-Rate-Limit-Ip-State": "1:10:0,1:60:0,2:300:0,151:21600:0",
}


class FakeSleep:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(round(seconds, 6))
        self.clock.advance(seconds)
        await asyncio.sleep(0)


@pytest.fixture
def sleep(clock: FakeClock) -> FakeSleep:
    return FakeSleep(clock)


@pytest.fixture
def limiter(clock: FakeClock, sleep: FakeSleep) -> RateLimiter:
    return RateLimiter(clock=clock, sleep=sleep)


def reply(status: int = 200, headers: dict[str, str] | None = None):
    async def send() -> httpx.Response:
        return httpx.Response(status, headers=headers or {})

    return send


def test_parse_triples_multiple_periods_and_garbage() -> None:
    assert parse_triples("5:10:60,15:60:300, 30:300:1800") == [
        (5, 10, 60),
        (15, 60, 300),
        (30, 300, 1800),
    ]
    assert parse_triples("") == []
    assert parse_triples("1:2,x:1:1,3:4:5") == [(3, 4, 5)]


async def test_headers_build_policy_with_multiple_rules(limiter: RateLimiter) -> None:
    headers = {
        **SEARCH_HEADERS,
        "X-Rate-Limit-Rules": "Ip,Account",
        "X-Rate-Limit-Account": "3:5:60",
        "X-Rate-Limit-Account-State": "2:5:0",
    }
    await limiter.run("search", reply(200, headers))
    state = limiter.state()
    assert state["kinds"]["search"] == "trade-search-request-limit"
    policy = state["policies"]["trade-search-request-limit"]
    assert [r["period"] for r in policy["rules"]["Ip"]] == [10, 60, 300, 21600]
    assert [r["hits"] for r in policy["rules"]["Ip"]] == [1, 1, 2, 151]
    assert policy["rules"]["Account"] == [
        {"max_hits": 3, "period": 5, "restriction": 60, "hits": 2}
    ]
    json.dumps(state)


async def test_waits_until_one_hit_spare(
    limiter: RateLimiter, sleep: FakeSleep, clock: FakeClock
) -> None:
    headers = {"X-Rate-Limit-Rules": "Ip", "X-Rate-Limit-Ip": "3:10:60"}
    await limiter.run("search", reply(200, {**headers, "X-Rate-Limit-Ip-State": "1:10:0"}))
    clock.advance(1)
    await limiter.run("search", reply(200, {**headers, "X-Rate-Limit-Ip-State": "2:10:0"}))
    assert sleep.calls == []
    # 2 hits in the window and max 3: the third would use the spare, so wait for the oldest.
    assert limiter.wait_for("search") == pytest.approx(9.0)
    await limiter.run("search", reply(200, {**headers, "X-Rate-Limit-Ip-State": "2:10:0"}))
    assert sleep.calls == [9.0]


async def test_server_state_counts_hits_we_did_not_send(limiter: RateLimiter) -> None:
    headers = {"X-Rate-Limit-Rules": "Ip", "X-Rate-Limit-Ip": "5:10:60"}
    await limiter.run("fetch", reply(200, {**headers, "X-Rate-Limit-Ip-State": "4:10:0"}))
    assert limiter.wait_for("fetch") == pytest.approx(10.0)


async def test_one_request_per_two_seconds_before_first_rules(
    limiter: RateLimiter, sleep: FakeSleep
) -> None:
    for _ in range(3):
        await limiter.run("search", reply())
    assert sleep.calls == [2.0, 2.0]
    # exchange shares search's default policy until a response names its own.
    await limiter.run("exchange", reply())
    assert sleep.calls == [2.0, 2.0, 2.0]
    await limiter.run("fetch", reply())
    assert sleep.calls == [2.0, 2.0, 2.0]


async def test_429_pauses_whole_queue_for_retry_after_plus_ten_percent(
    limiter: RateLimiter, sleep: FakeSleep, clock: FakeClock
) -> None:
    start = clock()
    with pytest.raises(RateLimited) as err:
        await limiter.run("search", reply(429, {**SEARCH_HEADERS, "Retry-After": "10"}))
    assert err.value.retry_after == pytest.approx(11.0)
    assert limiter.state()["paused_until"] == pytest.approx(start + 11.0)
    await limiter.run("fetch", reply())
    assert sleep.calls == [pytest.approx(11.0)]
    assert limiter.state()["paused_until"] is None


async def test_wait_over_twenty_seconds_raises_without_sending(
    limiter: RateLimiter, sleep: FakeSleep
) -> None:
    with pytest.raises(RateLimited):
        await limiter.run("search", reply(429, {"Retry-After": "30"}))
    sent = []

    async def send() -> httpx.Response:
        sent.append(1)
        return httpx.Response(200)

    with pytest.raises(RateLimited) as err:
        await limiter.run("fetch", send)
    assert err.value.retry_after == pytest.approx(33.0)
    assert (sent, sleep.calls) == ([], [])


async def test_active_restriction_in_state_blocks_policy(limiter: RateLimiter) -> None:
    headers = {
        "X-Rate-Limit-Rules": "Ip",
        "X-Rate-Limit-Ip": "5:10:60",
        "X-Rate-Limit-Ip-State": "6:10:60",
    }
    await limiter.run("search", reply(200, headers))
    with pytest.raises(RateLimited):
        await limiter.run("search", reply())


async def test_only_one_request_in_flight(limiter: RateLimiter) -> None:
    active = peak = 0

    async def send() -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(
            200, headers={"X-Rate-Limit-Rules": "Ip", "X-Rate-Limit-Ip": "99:1:1"}
        )

    await asyncio.gather(*(limiter.run("fetch", send) for _ in range(5)))
    assert peak == 1


async def test_waiters_get_rate_limited_when_a_429_lands(limiter: RateLimiter) -> None:
    release = asyncio.Event()
    sent: list[str] = []

    async def first() -> httpx.Response:
        sent.append("first")
        await release.wait()
        return httpx.Response(429, headers={"Retry-After": "5"})

    async def second() -> httpx.Response:
        sent.append("second")
        return httpx.Response(200)

    a = asyncio.create_task(limiter.run("search", first))
    await asyncio.sleep(0)
    b = asyncio.create_task(limiter.run("fetch", second))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(a, b, return_exceptions=True)
    assert all(isinstance(r, RateLimited) for r in results)
    assert sent == ["first"]


async def test_state_updates_are_logged_to_file(limiter: RateLimiter, tmp_path: Path) -> None:
    path = tmp_path / "logs" / "ratelimit.log"
    logger = add_file_log("poe_craft_mcp.ratelimit", path)
    logger.setLevel(logging.INFO)
    try:
        await limiter.run("search", reply(200, SEARCH_HEADERS))
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(logging.NOTSET)
    text = path.read_text(encoding="utf-8")
    assert "trade-search-request-limit" in text and "151:21600:0" in text
