from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from poe_craft_mcp import REPO_URL
from poe_craft_mcp.config import Config
from poe_craft_mcp.http.cache import PriceSnapshot, ResponseCache
from poe_craft_mcp.http.fetch import CachedHttpClient, FetchError

if TYPE_CHECKING:
    from conftest import FakeClock

URL = "https://poe.ninja/poe1/api/economy/leagues"


async def test_get_missing_returns_none(cache: ResponseCache) -> None:
    assert await cache.get(URL) is None


async def test_put_get_and_age(cache: ResponseCache, clock: FakeClock) -> None:
    await cache.put(URL, '{"a": 1}', 200, 'W/"abc"')
    clock.advance(42)
    hit = await cache.get(URL)
    assert hit is not None
    assert hit.json() == {"a": 1}
    assert hit.etag == 'W/"abc"'
    assert hit.status == 200
    assert hit.age_seconds == 42


async def test_put_replaces_existing(cache: ResponseCache) -> None:
    await cache.put(URL, b"1", 200, "e1")
    await cache.put(URL, b"2", 200, None)
    hit = await cache.get(URL)
    assert hit is not None
    assert (hit.body, hit.etag) == (b"2", None)


async def test_touch_resets_age(cache: ResponseCache, clock: FakeClock) -> None:
    assert await cache.touch(URL) is False
    await cache.put(URL, b"{}")
    clock.advance(600)
    assert await cache.touch(URL) is True
    hit = await cache.get(URL)
    assert hit is not None and hit.age_seconds == 0


async def test_snapshots_roundtrip(cache: ResponseCache, clock: FakeClock) -> None:
    start = clock()
    written = await cache.add_snapshots(
        [
            PriceSnapshot("League", "ninja:Currency", "divine", 180.0, 5000),
            PriceSnapshot("League", "ninja:Currency", "divine", 175.0, None, start - 3600),
            PriceSnapshot("League", "ninja:Currency", "exalted", 12.0, 100),
        ]
    )
    assert written == 3
    rows = await cache.get_snapshots("League", "ninja:Currency", "divine")
    assert [r.chaos for r in rows] == [175.0, 180.0]
    assert rows[1].taken_at == start
    recent = await cache.get_snapshots("League", "ninja:Currency", "divine", since=start - 60)
    assert [r.chaos for r in recent] == [180.0]


@pytest.fixture
def client(cache: ResponseCache, config: Config) -> CachedHttpClient:
    return CachedHttpClient(cache, config.user_agent)


@respx.mock
async def test_fresh_fetch_stores_etag_and_sends_user_agent(
    client: CachedHttpClient, cache: ResponseCache
) -> None:
    route = respx.get(URL).respond(200, json={"leagues": []}, headers={"ETag": '"v1"'})
    result = await client.get_json(URL, min_interval_s=900)
    assert result.data == {"leagues": []}
    assert (result.from_cache, result.stale, result.age_seconds) == (False, False, 0.0)
    assert result.source_url == URL
    ua = route.calls.last.request.headers["User-Agent"]
    assert ua.startswith("poe-craft-mcp/") and REPO_URL in ua
    stored = await cache.get(URL)
    assert stored is not None and stored.etag == '"v1"'
    await client.aclose()


@respx.mock
async def test_second_call_within_interval_makes_no_request(
    client: CachedHttpClient, clock: FakeClock
) -> None:
    route = respx.get(URL).respond(200, json={"n": 1}, headers={"ETag": '"v1"'})
    await client.get_json(URL, min_interval_s=900)
    clock.advance(899)
    result = await client.get_json(URL, min_interval_s=900)
    assert route.call_count == 1
    assert result.from_cache and not result.stale
    assert result.age_seconds == 899
    await client.aclose()


@respx.mock
async def test_after_interval_revalidates_with_etag_and_handles_304(
    client: CachedHttpClient, clock: FakeClock
) -> None:
    route = respx.get(URL)
    route.side_effect = [
        httpx.Response(200, json={"n": 1}, headers={"ETag": '"v1"'}),
        httpx.Response(304),
    ]
    await client.get_json(URL, min_interval_s=900)
    clock.advance(901)
    result = await client.get_json(URL, min_interval_s=900)
    assert route.call_count == 2
    assert route.calls.last.request.headers["If-None-Match"] == '"v1"'
    assert result.data == {"n": 1}
    assert result.from_cache and not result.stale and result.age_seconds == 0
    clock.advance(10)
    again = await client.get_json(URL, min_interval_s=900)
    assert route.call_count == 2 and again.age_seconds == 10
    await client.aclose()


@respx.mock
async def test_params_are_part_of_the_cache_key(client: CachedHttpClient) -> None:
    route = respx.get(URL).respond(200, json={})
    first = await client.get_json(URL, params={"league": "A"}, min_interval_s=900)
    await client.get_json(URL, params={"league": "B"}, min_interval_s=900)
    await client.get_json(URL, params={"league": "A"}, min_interval_s=900)
    assert route.call_count == 2
    assert first.source_url == f"{URL}?league=A"
    await client.aclose()


@pytest.mark.parametrize("failure", [httpx.Response(503), httpx.ConnectError("down")])
@respx.mock
async def test_upstream_failure_serves_stale_copy(
    client: CachedHttpClient, clock: FakeClock, failure: object
) -> None:
    route = respx.get(URL)
    route.side_effect = [httpx.Response(200, json={"n": 1}), failure]
    await client.get_json(URL, min_interval_s=60)
    clock.advance(120)
    result = await client.get_json(URL, min_interval_s=60)
    assert result.stale and result.from_cache
    assert result.data == {"n": 1} and result.age_seconds == 120
    await client.aclose()


@respx.mock
async def test_upstream_failure_without_cache_raises(client: CachedHttpClient) -> None:
    respx.get(URL).respond(500)
    with pytest.raises(FetchError) as info:
        await client.get_json(URL)
    assert info.value.status == 500
    await client.aclose()


@respx.mock
async def test_client_error_is_not_masked_by_cache(
    client: CachedHttpClient, clock: FakeClock
) -> None:
    route = respx.get(URL)
    route.side_effect = [httpx.Response(200, json={}), httpx.Response(404)]
    await client.get_json(URL)
    clock.advance(1)
    with pytest.raises(FetchError, match="HTTP 404"):
        await client.get_json(URL)
    await client.aclose()
