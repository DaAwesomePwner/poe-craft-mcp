# ruff: noqa: F811, F401  (fixtures are re-used from test_ninja)
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp import Client
from test_ninja import INTERVAL, NinjaApi, api, ctx, with_league

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.server import build_server

if TYPE_CHECKING:
    from conftest import FakeClock


@asynccontextmanager
async def session(ctx: AppContext) -> AsyncIterator[Client]:
    async with Client(build_server(ctx.config, ctx)) as client:
        yield client


async def call(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    return result.structured_content or {}


async def test_tools_are_listed_and_read_only(
    ctx: AppContext,
) -> None:
    async with session(ctx) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("get_prices", "get_base_price", "get_league_status"):
            assert tools[name].annotations is not None
            assert tools[name].annotations.read_only_hint is True


async def test_get_prices_rows_sources_and_not_found(
    ctx: AppContext,
) -> None:
    async with session(ctx) as client:
        data = await call(
            client, "get_prices", category="Currency", names=["Divine Orb", "exalted", "Nope Orb"]
        )
        assert [r["id"] for r in data["rows"]] == ["divine", "exalted"]
        assert data["not_found"] == ["Nope Orb"]
        assert data["league"] == "Covens" and data["category"] == "Currency"
        assert data["data_age_seconds"] == 0.0
        league_source, source = data["sources"]
        assert league_source["name"] == "poe.ninja leagues" and league_source["stale"] is False
        assert source["name"] == "poe.ninja Currency" and source["url"].endswith("type=Currency")
        assert source["age_seconds"] == 0.0 and source["stale"] is False
        row = data["rows"][0]
        assert {"name", "id", "chaos", "divine", "volume_or_listings", "sparkline_7d"} <= row.keys()


async def test_get_prices_without_names_is_sorted_by_value(
    ctx: AppContext,
) -> None:
    async with session(ctx) as client:
        data = await call(client, "get_prices", category="Essence")
        chaos = [r["chaos"] for r in data["rows"]]
        assert chaos == sorted(chaos, reverse=True) and data["total_rows"] == 6
        assert data["truncated"] is False


async def test_get_prices_second_call_is_cached_and_reports_age(
    ctx: AppContext,
    api: NinjaApi,
    clock: FakeClock,
) -> None:
    async with session(ctx) as client:
        await call(client, "get_prices", category="Currency")
        count = api.call_count
        clock.advance(600)
        data = await call(client, "get_prices", category="Currency", names=["chaos"])
        assert api.call_count == count
        assert data["data_age_seconds"] == 600.0
        assert [src["age_seconds"] for src in data["sources"]] == [600.0, 600.0]


async def test_get_prices_empty_type_returns_hint(
    ctx: AppContext,
) -> None:
    async with session(ctx) as client:
        data = await call(client, "get_prices", category="Scarab")
        assert data["rows"] == [] and "no rows" in data["hint"]


async def test_get_prices_unknown_category_is_an_error_listing_types(
    ctx: AppContext,
) -> None:
    async with session(ctx) as client:
        result = await client.call_tool("get_prices", {"category": "Bananas"})
        assert result.is_error
        text = str(result.content)
        assert "Bananas" in text and "Currency" in text and "BaseType" in text


async def test_get_base_price_hit_and_miss(
    ctx: AppContext,
) -> None:
    async with session(ctx) as client:
        hit = await call(client, "get_base_price", base="Astral Plate", ilvl_min=84)
        assert hit["row"]["id"] == "astral-plate-84" and hit["row"]["chaos"] == 2.0
        assert "hint" not in hit
        assert [src["name"] for src in hit["sources"]] == [
            "poe.ninja leagues",
            "poe.ninja BaseType",
        ]
        assert hit["data_age_seconds"] == 0.0

        shaper = await call(
            client, "get_base_price", base="Astral Plate", ilvl_min=86, influence="Shaper"
        )
        assert shaper["row"]["id"] == "astral-plate-86-shaper"

        miss = await call(client, "get_base_price", base="Nonexistent Base")
        assert miss["row"] is None and "trade query" in miss["hint"]
        assert miss["sources"]


async def test_league_status_fields_and_caching(
    ctx: AppContext,
    api: NinjaApi,
    clock: FakeClock,
) -> None:
    async with session(ctx) as client:
        first = await call(client, "get_league_status")
        assert first["league"] == "Covens" and first["league_name"] == "Covens"
        assert first["chaos_per_divine"] == 195.0
        assert first["datasets"] == {"Currency": 0.0}
        assert first["poesessid_set"] is None and first["trade_rate_limit"] is None
        assert first["pending"] == ["poesessid_set", "trade_rate_limit"]
        assert {s["name"] for s in first["sources"]} == {"poe.ninja leagues", "poe.ninja Currency"}
        assert first["data_age_seconds"] == 0.0
        assert api.call_count == 2

        clock.advance(INTERVAL - 1)
        second = await call(client, "get_league_status")
        assert api.call_count == 2
        assert second["datasets"] == {"Currency": INTERVAL - 1}
        assert second["data_age_seconds"] == INTERVAL - 1

        clock.advance(2)
        third = await call(client, "get_league_status")
        assert api.call_count == 4
        assert all(call.headers.get("If-None-Match") for call in api.calls[2:])
        assert third["datasets"] == {"Currency": 0.0} and third["data_age_seconds"] == 0.0


async def test_league_status_lists_every_cached_dataset(
    ctx: AppContext,
) -> None:
    async with session(ctx) as client:
        await call(client, "get_prices", category="Essence")
        await call(client, "get_base_price", base="Astral Plate")
        status = await call(client, "get_league_status")
        assert set(status["datasets"]) == {"Currency", "Essence", "BaseType"}


async def test_stale_league_selection_is_visible_in_every_tool(
    ctx: AppContext, api: NinjaApi, clock: FakeClock
) -> None:
    async with session(ctx) as client:
        await call(client, "get_prices", category="Currency", names=["chaos"])
        clock.advance(INTERVAL + 1)
        api.fail("leagues", 503)  # a rollover is not visible until the lookup works again
        prices = await call(client, "get_prices", category="Currency", names=["chaos"])
        base = await call(client, "get_base_price", base="Astral Plate")
        status = await call(client, "get_league_status")
    for data in (prices, base, status):
        leagues = next(src for src in data["sources"] if src["name"] == "poe.ninja leagues")
        assert leagues["stale"] is True and leagues["age_seconds"] == INTERVAL + 1


async def test_status_with_explicit_league_and_missing_leagues_endpoint_asks_once(
    tmp_path: Path, clock: FakeClock, api: NinjaApi
) -> None:
    api.fail("leagues", 404)
    ctx = await with_league(tmp_path, clock, "Standard")
    try:
        async with session(ctx) as client:
            status = await call(client, "get_league_status")
            assert status["league"] == "Standard" and status["chaos_per_divine"] == 195.0
            assert api.hits("leagues") == 1
            await call(client, "get_prices", category="Currency", names=["chaos"])
            assert api.hits("leagues") == 1
            clock.advance(INTERVAL + 1)
            await call(client, "get_league_status")
            assert api.hits("leagues") == 2
    finally:
        await ctx.aclose()


async def test_get_prices_records_the_rows_it_returns(ctx: AppContext) -> None:
    async with session(ctx) as client:
        data = await call(
            client, "get_prices", category="BaseType", names=["imperial-bow-86-hunter"]
        )
    assert data["rows"][0]["volume_or_listings"] == 11
    snaps = await ctx.cache.get_snapshots("Covens", "ninja:BaseType", "imperial-bow-86-hunter")
    assert [s.chaos for s in snaps] == [2.0]
