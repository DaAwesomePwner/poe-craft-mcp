from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from poe_craft_mcp.http.ratelimit import (
    RateLimited,
    RateLimiter,
    parse_triples,
    retry_after_seconds,
    setup_state_log,
)

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


@pytest.fixture
def state_logger(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "logs" / "ratelimit.log"
    logger = setup_state_log(path)
    yield path
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


async def test_state_logged_to_file_at_any_level_without_stderr_echo(
    limiter: RateLimiter, state_logger: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    await limiter.run("search", reply(200, SEARCH_HEADERS))
    text = state_logger.read_text(encoding="utf-8")
    assert "trade-search-request-limit" in text and "151:21600:0" in text
    assert "151:21600:0" not in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12", 12.0),
        ("1.5", 1.5),
        ("NaN", 60.0),
        ("inf", 60.0),
        ("-5", 60.0),
        ("garbage", 60.0),
        (None, 60.0),
        ("99999", 99999.0),
        ("9" * 400, 60.0),
        ("Tue, 14 Nov 2023 22:13:50 GMT", 30.0),
    ],
)
def test_retry_after_parsing_is_strict(value: str | None, expected: float) -> None:
    now = 1_700_000_000.0  # 2023-11-14T22:13:20Z
    headers = httpx.Headers({} if value is None else {"Retry-After": value})
    assert retry_after_seconds(headers, now) == pytest.approx(expected)


async def test_nan_retry_after_still_pauses(limiter: RateLimiter, clock: FakeClock) -> None:
    with pytest.raises(RateLimited) as err:
        await limiter.run("search", reply(429, {"Retry-After": "NaN"}))
    assert err.value.retry_after == pytest.approx(66.0)
    assert limiter.state()["paused_until"] == pytest.approx(clock() + 66.0)


async def test_lock_wait_counts_against_deadline(limiter: RateLimiter, clock: FakeClock) -> None:
    release = asyncio.Event()

    async def slow() -> httpx.Response:
        await release.wait()
        return httpx.Response(200)

    holder = asyncio.create_task(limiter.run("search", slow))
    await asyncio.sleep(0)
    with pytest.raises(RateLimited, match="queue busy"):
        await limiter.run("fetch", reply(), deadline=clock() + 0.05)
    release.set()
    await holder


async def test_unlearned_kind_honours_restriction_seen_elsewhere(limiter: RateLimiter) -> None:
    restricted = {
        **SEARCH_HEADERS,
        "X-Rate-Limit-Ip-State": "6:10:60,1:60:0,2:300:0,151:21600:0",
    }
    await limiter.run("search", reply(200, restricted))
    with pytest.raises(RateLimited):
        await limiter.run("exchange", reply())
    # search waits out its own restriction.
    assert limiter.wait_for("search") == pytest.approx(60.0)


async def test_long_retry_after_is_honoured_unclamped(
    limiter: RateLimiter, clock: FakeClock
) -> None:
    start = clock()
    with pytest.raises(RateLimited) as err:
        await limiter.run("search", reply(429, {"Retry-After": "7200"}))
    assert err.value.retry_after == pytest.approx(7920.0)
    clock.advance(3961)
    with pytest.raises(RateLimited):
        await limiter.run("fetch", reply())
    assert limiter.state()["paused_until"] == pytest.approx(start + 7920.0)


async def test_unlearned_sibling_keeps_one_hit_spare(
    limiter: RateLimiter, sleep: FakeSleep
) -> None:
    near_full = {
        "X-Rate-Limit-Policy": "trade-search-request-limit",
        "X-Rate-Limit-Rules": "Ip",
        "X-Rate-Limit-Ip": "5:10:60",
        "X-Rate-Limit-Ip-State": "4:10:0",
    }
    await limiter.run("search", reply(200, near_full))
    assert limiter.wait_for("exchange") == pytest.approx(10.0)
    await limiter.run("exchange", reply())
    assert sleep.calls == [10.0]


async def test_fetch_never_inherits_search_restriction(limiter: RateLimiter) -> None:
    restricted = {
        **SEARCH_HEADERS,
        "X-Rate-Limit-Ip-State": "6:10:60,1:60:0,2:300:0,151:21600:0",
    }
    await limiter.run("search", reply(200, restricted))
    assert limiter.wait_for("fetch") == 0.0
    assert limiter.wait_for("search") == pytest.approx(60.0)
    fetch_full = {
        "X-Rate-Limit-Policy": "trade-fetch-request-limit",
        "X-Rate-Limit-Rules": "Ip",
        "X-Rate-Limit-Ip": "12:4:120",
        "X-Rate-Limit-Ip-State": "13:4:120",
    }
    await limiter.run("fetch", reply(200, fetch_full))
    # A fetch restriction never gates exchange (search's sibling) either.
    assert limiter.wait_for("exchange") == pytest.approx(60.0)
    assert limiter.wait_for("fetch") == pytest.approx(120.0)
