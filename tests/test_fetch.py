from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from poe_craft_mcp import REPO_URL
from poe_craft_mcp.config import Config
from poe_craft_mcp.http.cache import ResponseCache
from poe_craft_mcp.http.fetch import CachedHttpClient, FetchError, cache_key

if TYPE_CHECKING:
    from conftest import FakeClock

URL = "https://poe.ninja/poe1/api/economy/leagues"


def test_cache_key_merges_and_sorts_query() -> None:
    assert cache_key(f"{URL}?type=Currency&league=A") == f"{URL}?league=A&type=Currency"
    assert cache_key(f"{URL}?league=A", {"type": "Fossil"}) == f"{URL}?league=A&type=Fossil"
    assert cache_key(f"{URL}?league=A", {"league": "B"}) == f"{URL}?league=B"
    assert cache_key(URL, {}) == URL


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


@respx.mock
async def test_query_string_in_url_is_preserved(client: CachedHttpClient) -> None:
    route = respx.get(URL).respond(200, json={})
    a = await client.get_json(f"{URL}?league=A", min_interval_s=900)
    b = await client.get_json(f"{URL}?league=B", params={"type": "Currency"}, min_interval_s=900)
    assert route.call_count == 2
    sent = [str(call.request.url) for call in route.calls]
    assert sent == [f"{URL}?league=A", f"{URL}?league=B&type=Currency"]
    assert (a.source_url, b.source_url) == tuple(sent)
    await client.aclose()


@respx.mock
async def test_concurrent_callers_share_one_request(client: CachedHttpClient) -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"n": 1})

    route = respx.get(URL).mock(side_effect=slow)
    results = await asyncio.gather(*(client.get_json(URL) for _ in range(5)))
    assert route.call_count == 1
    assert all(r.data == {"n": 1} and not r.stale for r in results)
    await client.aclose()


@respx.mock
async def test_failure_backs_off_and_serves_stale(
    client: CachedHttpClient, clock: FakeClock
) -> None:
    route = respx.get(URL)
    route.side_effect = [
        httpx.Response(200, json={"n": 1}),
        httpx.Response(503),
        httpx.Response(200, json={"n": 2}),
    ]
    await client.get_json(URL, min_interval_s=60)
    clock.advance(120)
    assert (await client.get_json(URL, min_interval_s=60)).stale
    clock.advance(59)
    again = await client.get_json(URL, min_interval_s=60)
    assert again.stale and again.data == {"n": 1}
    assert route.call_count == 2
    clock.advance(1)
    recovered = await client.get_json(URL, min_interval_s=60)
    assert route.call_count == 3
    assert recovered.data == {"n": 2} and not recovered.stale
    await client.aclose()


@respx.mock
async def test_failure_without_cache_backs_off_too(
    client: CachedHttpClient, clock: FakeClock
) -> None:
    route = respx.get(URL).respond(502)
    with pytest.raises(FetchError):
        await client.get_json(URL)
    clock.advance(30)
    with pytest.raises(FetchError, match="backing off"):
        await client.get_json(URL)
    assert route.call_count == 1
    clock.advance(30)
    with pytest.raises(FetchError, match="HTTP 502"):
        await client.get_json(URL)
    assert route.call_count == 2
    await client.aclose()
