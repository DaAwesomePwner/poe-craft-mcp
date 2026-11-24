"""Regression tests for overlapping callers and failure backoff in CachedHttpClient."""

from __future__ import annotations

import asyncio
import itertools
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

from poe_craft_mcp.config import Config
from poe_craft_mcp.http.cache import ResponseCache
from poe_craft_mcp.http.fetch import CachedHttpClient, FetchError

if TYPE_CHECKING:
    from conftest import FakeClock

URL = "https://poe.ninja/poe1/api/economy/exchange/current/overview"


@respx.mock
async def test_caller_arriving_after_response_stamp_shares_inflight_fetch(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B arrives after A stamped fetched_at but before A finished storing; B must not refetch."""
    ticks = itertools.count(1_700_000_000.0)
    cache = ResponseCache(config.cache_path, clock=lambda: next(ticks))
    client = CachedHttpClient(cache, config.user_agent)
    route = respx.get(URL).respond(200, json={"n": 1}, headers={"ETag": '"v1"'})

    stored = asyncio.Event()
    original_put = cache.put

    async def slow_put(*args: Any, **kwargs: Any) -> None:
        await original_put(*args, **kwargs)
        stored.set()
        await asyncio.sleep(0.05)

    monkeypatch.setattr(cache, "put", slow_put)
    first = asyncio.create_task(client.get_json(URL, min_interval_s=0))
    await stored.wait()
    second = await client.get_json(URL, min_interval_s=0)
    assert (await first).data == second.data == {"n": 1}
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_cancelled_caller_does_not_cancel_shared_fetch(client: CachedHttpClient) -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"n": 1})

    route = respx.get(URL).mock(side_effect=slow)
    first = asyncio.create_task(client.get_json(URL))
    await asyncio.sleep(0)
    second = asyncio.create_task(client.get_json(URL))
    await asyncio.sleep(0.01)
    first.cancel()
    assert (await second).data == {"n": 1}
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_failure_deadline_holds_for_callers_with_shorter_interval(
    client: CachedHttpClient, clock: FakeClock
) -> None:
    route = respx.get(URL).respond(503)
    with pytest.raises(FetchError, match="HTTP 503"):
        await client.get_json(URL, min_interval_s=900)
    clock.advance(61)
    with pytest.raises(FetchError, match="backing off"):
        await client.get_json(URL)
    clock.advance(838)
    with pytest.raises(FetchError, match="backing off"):
        await client.get_json(URL, min_interval_s=0)
    assert route.call_count == 1
    clock.advance(1)
    with pytest.raises(FetchError, match="HTTP 503"):
        await client.get_json(URL)
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_failure_deadline_holds_with_stale_copy(
    client: CachedHttpClient, clock: FakeClock
) -> None:
    route = respx.get(URL)
    route.side_effect = [httpx.Response(200, json={"n": 1}), httpx.Response(503)]
    await client.get_json(URL, min_interval_s=900)
    clock.advance(901)
    assert (await client.get_json(URL, min_interval_s=900)).stale
    clock.advance(61)
    result = await client.get_json(URL)
    assert result.stale and result.data == {"n": 1}
    assert route.call_count == 2
    await client.aclose()


@pytest.fixture
def client(cache: ResponseCache, config: Config) -> CachedHttpClient:
    return CachedHttpClient(cache, config.user_agent)
