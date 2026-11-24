# ruff: noqa: F811, F401  (fixtures are re-used from test_ninja and test_trade_pricing)
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from test_ninja import NinjaApi, api, ctx
from test_tools_prices import call, session
from test_trade import SAVED_URL, SEARCH, FakeSleep
from test_trade_pricing import mock_trade, no_keyring, trade

from poe_craft_mcp.clients.trade import TradeClient
from poe_craft_mcp.config import Config
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.http.ratelimit import RateLimiter

if TYPE_CHECKING:
    from conftest import FakeClock


async def test_price_trade_query_is_listed_read_only(ctx: AppContext) -> None:
    async with session(ctx) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    annotations = tools["price_trade_query"].annotations
    assert annotations is not None and annotations.read_only_hint is True


async def test_price_trade_query_envelope(
    ctx: AppContext, api: NinjaApi, trade: TradeClient
) -> None:
    mock_trade(api)
    async with session(ctx) as client:
        data = await call(client, "price_trade_query", trade_url=SAVED_URL, sample=10)
    assert (data["p10"], data["p25"], data["median"]) == (5.0, 5.0, 5.0)
    assert data["divine"] == {"p10": 0.026, "p25": 0.026, "median": 0.026}
    assert data["chaos_per_divine"] == 195.0
    assert (data["listing_count"], data["raw_count"], data["dropped_outliers"]) == (10, 10, 0)
    assert data["dropped_unknown_currency"] == {} and data["currency_mix"] == {"chaos": 10}
    assert (data["total"], data["search_id"], data["stale"]) == (356, SEARCH["id"], False)
    assert data["sample"] == 10 and data["league"] == "Covens"
    assert "cheapest_listing_age_seconds" in data
    assert data["data_age_seconds"] == 0.0
    assert [s["name"] for s in data["sources"]] == [
        "poe.ninja leagues",
        "poe.ninja Currency",
        "pathofexile.com trade",
    ]


async def test_price_trade_query_marks_stale(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, clock: FakeClock
) -> None:
    mock_trade(api)
    async with session(ctx) as client:
        await call(client, "price_trade_query", trade_url=SAVED_URL, sample=10)
        clock.advance(25 * 60)
        trade.limiter.paused_until = clock() + 120
        data = await call(client, "price_trade_query", trade_url=SAVED_URL, sample=10)
    assert data["stale"] is True and data["sources"][2]["stale"] is True
    assert data["data_age_seconds"] == 1500


async def test_price_trade_query_rate_limited_is_a_clear_error(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, clock: FakeClock
) -> None:
    mock_trade(api)
    trade.limiter.paused_until = clock() + 90
    async with session(ctx) as client:
        result = await client.call_tool("price_trade_query", {"trade_url": SAVED_URL})
    assert result.is_error
    text = str(result.content)
    assert "Price unavailable" in text and "Try again in about 90s" in text
    assert "Traceback" not in text


async def test_price_trade_query_needs_exactly_one_input(ctx: AppContext) -> None:
    async with session(ctx) as client:
        neither = await client.call_tool("price_trade_query", {})
        both = await client.call_tool(
            "price_trade_query", {"trade_url": SAVED_URL, "query_json": {"type": "x"}}
        )
    for result in (neither, both):
        assert result.is_error and "exactly one" in str(result.content)


async def test_status_reports_limiter_only_after_trade_use(
    ctx: AppContext, api: NinjaApi, clock: FakeClock, no_keyring: None
) -> None:
    async with session(ctx) as client:
        before = await call(client, "get_league_status")
        assert before["trade_rate_limit"] == {"note": "no trade calls yet in this session"}
        assert "trade" not in ctx.services

        limiter = RateLimiter(clock=clock, sleep=FakeSleep(clock))
        ctx.services["trade"] = TradeClient(
            ctx.config, ctx.cache, limiter=limiter, poesessid=lambda: None
        )
        mock_trade(api)
        await call(client, "price_trade_query", query_json={"type": "Simple Robe"}, sample=5)
        after = await call(client, "get_league_status")
    limits = after["trade_rate_limit"]
    assert set(limits) == {"paused_until", "kinds", "policies"}
    assert "trade-search-request-limit" in limits["policies"]


async def test_omitted_sample_uses_configured_trade_sample(
    tmp_path: Path, api: NinjaApi, clock: FakeClock
) -> None:
    ctx = AppContext.create(Config(data_dir=tmp_path / "data", trade_sample=10), clock=clock)
    limiter = RateLimiter(clock=clock, sleep=FakeSleep(clock))
    ctx.services["trade"] = TradeClient(
        ctx.config, ctx.cache, limiter=limiter, poesessid=lambda: None
    )
    _, fetch = mock_trade(api)
    try:
        async with session(ctx) as client:
            data = await call(client, "price_trade_query", trade_url=SAVED_URL)
    finally:
        await ctx.aclose()
    assert data["sample"] == 10 and fetch.call_count == 1
