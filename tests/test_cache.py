from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from poe_craft_mcp.config import Config
from poe_craft_mcp.http.cache import PriceSnapshot, ResponseCache

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


async def test_concurrent_first_use_initializes_once(config: Config, clock: FakeClock) -> None:
    fresh = ResponseCache(config.data_dir / "fresh" / "cache.db", clock=clock)
    urls = [f"{URL}?n={i}" for i in range(20)]
    await asyncio.gather(*(fresh.get(u) for u in urls), *(fresh.put(u, b"{}") for u in urls))
    hits = await asyncio.gather(*(fresh.get(u) for u in urls))
    assert all(hit is not None for hit in hits)
