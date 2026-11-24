# ruff: noqa: F811, F401  (fixtures are re-used from test_ninja)
from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from test_ninja import NinjaApi, api, ctx
from test_trade import SAVED_URL, SEARCH, FakeSleep, blob, recorded

from poe_craft_mcp import secrets
from poe_craft_mcp.clients.ninja import get_ninja
from poe_craft_mcp.clients.trade import TradeClient
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.http.ratelimit import RateLimiter
from poe_craft_mcp.log import register_secret
from poe_craft_mcp.services.trade_pricing import PricingUnavailable, parse_indexed, price_outcome

if TYPE_CHECKING:
    from conftest import FakeClock

TRADE = "https://www.pathofexile.com"
FAKE_SECRET = "fake-poesessid-0123456789abcdef"


@pytest.fixture
def no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets, "_from_keyring", lambda: None)


@pytest.fixture
async def trade(ctx: AppContext, clock: FakeClock) -> TradeClient:
    limiter = RateLimiter(clock=clock, sleep=FakeSleep(clock))
    client = TradeClient(ctx.config, ctx.cache, limiter=limiter, poesessid=lambda: None)
    ctx.services["trade"] = client
    return client


def fetch_body(prices: list[tuple[float, str]]) -> httpx.Response:
    result = [
        {
            "id": f"id{i}",
            "listing": {
                "indexed": "2026-11-23T00:00:00Z",
                "price": {"amount": amount, "currency": currency},
                "account": {"online": {"league": "Covens"}},
            },
            "item": {},
        }
        for i, (amount, currency) in enumerate(prices)
    ]
    return httpx.Response(200, json={"result": result})


def mock_trade(
    api: NinjaApi, league: str = "Covens", fetch: httpx.Response | None = None
) -> tuple[respx.Route, respx.Route]:
    search = api.router.post(f"{TRADE}/api/trade/search/{league}").mock(
        return_value=recorded("search")
    )
    fetched = api.router.get(url__startswith=f"{TRADE}/api/trade/fetch/").mock(
        return_value=fetch or recorded("fetch")
    )
    return search, fetched


async def test_happy_path_saved_url(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, clock: FakeClock
) -> None:
    clock.now = datetime(2026, 11, 24, 1, 0, tzinfo=UTC).timestamp()
    search, _ = mock_trade(api)
    price = await price_outcome(ctx, trade_url=SAVED_URL, sample=10)
    assert search.call_count == 1  # the URL's league (Settlers) was swapped for Covens
    assert (price.p10, price.p25, price.median) == (5.0, 5.0, 5.0)
    assert (price.listing_count, price.raw_count, price.dropped_outliers) == (10, 10, 0)
    assert price.currency_mix == {"chaos": 10} and price.dropped_unknown_currency == {}
    assert price.cheapest_listing_age_seconds == 24 * 60 + 10  # newest 5c listing 00:35:50
    assert (price.search_id, price.total, price.sample, price.league) == (
        SEARCH["id"],
        356,
        10,
        "Covens",
    )
    assert price.stale is False and price.age_seconds == 0.0 and price.chaos_per_divine == 195.0
    names = [s["name"] for s in price.sources]
    assert names == ["poe.ninja leagues", "poe.ninja Currency", "pathofexile.com trade"]
    trade_src = price.sources[2]
    assert trade_src["url"] == f"{TRADE}/trade/search/Covens/{SEARCH['id']}"
    assert trade_src["search_id"] == SEARCH["id"] and trade_src["stale"] is False


async def test_prices_are_converted_through_rates(
    ctx: AppContext, api: NinjaApi, trade: TradeClient
) -> None:
    prices = [(1, "divine"), (200, "chaos"), (230, "exalted"), (3, "nonsense")]
    mock_trade(api, fetch=fetch_body(prices))
    price = await price_outcome(ctx, query_json={"query": {"type": "Simple Robe"}}, sample=4)
    assert price.median == 200.0 and price.p10 == pytest.approx(196.0)
    assert price.currency_mix == {"divine": 1, "chaos": 1, "exalted": 1}
    assert price.dropped_unknown_currency == {"nonsense": 1}
    assert (price.listing_count, price.raw_count) == (3, 4)
    assert price.sources[2]["url"].startswith(f"{TRADE}/trade/search/Covens/")


async def test_query_json_posts_online_price_sorted_search(
    ctx: AppContext, api: NinjaApi, trade: TradeClient
) -> None:
    search, _ = mock_trade(api)
    await price_outcome(ctx, query_json={"query": {"type": "Simple Robe"}}, sample=10)
    body = json.loads(search.calls.last.request.content)
    assert body["query"]["status"] == {"option": "online"} and body["sort"] == {"price": "asc"}


async def test_stale_listings_propagate(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, clock: FakeClock
) -> None:
    search, _ = mock_trade(api)
    await price_outcome(ctx, trade_url=SAVED_URL, sample=10)
    clock.advance(25 * 60)
    trade.limiter.paused_until = clock() + 120
    price = await price_outcome(ctx, trade_url=SAVED_URL, sample=10)
    assert search.call_count == 1
    assert price.stale is True and price.age_seconds == 1500 and price.median == 5.0
    assert price.sources[2]["stale"] is True and price.sources[2]["from_cache"] is True


async def test_rate_limited_without_cache_is_pricing_unavailable(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, clock: FakeClock
) -> None:
    search, _ = mock_trade(api)
    trade.limiter.paused_until = clock() + 120
    with pytest.raises(PricingUnavailable) as err:
        await price_outcome(ctx, trade_url=SAVED_URL)
    assert err.value.retry_after == pytest.approx(120) and search.call_count == 0
    assert "rate limited" in err.value.reason


async def test_trade_error_never_quotes_the_cookie(
    ctx: AppContext, api: NinjaApi, trade: TradeClient
) -> None:
    trade._poesessid = lambda: FAKE_SECRET
    register_secret(FAKE_SECRET)
    api.router.post(f"{TRADE}/api/trade/search/Covens").respond(
        400, json={"error": {"message": f"bad query {FAKE_SECRET}"}}
    )
    with pytest.raises(PricingUnavailable) as err:
        await price_outcome(ctx, query_json={"type": "Simple Robe"})
    assert err.value.retry_after is None and err.value.reason.startswith("trade site:")
    assert FAKE_SECRET not in str(err.value) + repr(err.value) + err.value.reason


async def test_ninja_failure_is_unavailable_and_skips_trade(
    ctx: AppContext, api: NinjaApi, trade: TradeClient
) -> None:
    search, _ = mock_trade(api)
    api.fail("Currency", 503)
    with pytest.raises(PricingUnavailable, match="currency rates"):
        await price_outcome(ctx, trade_url=SAVED_URL)
    assert search.call_count == 0


async def test_explicit_league_and_sample_clamp(
    ctx: AppContext, api: NinjaApi, trade: TradeClient
) -> None:
    search, fetch = mock_trade(api, league="Standard")
    price = await price_outcome(ctx, trade_url=SAVED_URL, sample=500, league="Standard")
    assert search.call_count == 1 and fetch.call_count == 3  # 25 results, batches of 10
    assert price.sample == 100 and price.league == "Standard"
    assert [s["name"] for s in price.sources] == ["poe.ninja Currency", "pathofexile.com trade"]
    assert api.hits("leagues") == 0


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"trade_url": SAVED_URL, "query_json": {"type": "x"}}, {"trade_url": "https://x/y"}],
)
async def test_input_validation(ctx: AppContext, api: NinjaApi, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        await price_outcome(ctx, **kwargs)
    assert api.call_count == 0 and "trade" not in ctx.services


def test_parse_indexed() -> None:
    assert parse_indexed("2026-11-22T09:42:11Z") == datetime(2026, 11, 22, 9, 42, 11, tzinfo=UTC)
    assert parse_indexed("2026-11-22T09:42:11").tzinfo is UTC  # type: ignore[union-attr]
    assert parse_indexed("garbage") is None and parse_indexed(None) is None


SECURABLE = {"query": {"type": "Simple Robe", "status": {"option": "securable"}}}


@pytest.mark.parametrize(
    "target",
    [
        {"query_json": SECURABLE},
        {"trade_url": f"{TRADE}/trade/search/Settlers/{blob(json.dumps(SECURABLE).encode())}"},
    ],
)
async def test_instant_buyout_status_in_the_query_is_kept(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, target: dict[str, Any]
) -> None:
    search, _ = mock_trade(api)
    price = await price_outcome(ctx, sample=10, **target)
    body = json.loads(search.calls.last.request.content)
    assert body["query"]["status"] == {"option": "securable"}
    assert price.sources[-1]["instant_buyout"] is True


async def test_rates_and_divine_come_from_one_currency_snapshot(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ninja = get_ninja(ctx)
    real = ninja.dataset
    reads: list[Any] = []

    async def shifting(category: str, league: Any = None, **kwargs: Any) -> Any:
        ds = await real(category, league, **kwargs)
        if ds.type != "Currency":
            return ds
        reads.append(ds)
        if len(reads) == 1:
            return ds
        rows = [{**r, "chaos": 200.0} if r["id"] == "divine" else r for r in ds.rows]
        return dataclasses.replace(ds, rows=rows)  # a refresh landed between two reads

    monkeypatch.setattr(ninja, "dataset", shifting)
    mock_trade(api, fetch=fetch_body([(1, "divine")]))
    price = await price_outcome(ctx, query_json={"type": "Simple Robe"}, sample=1)
    assert len(reads) == 1
    assert price.median == price.chaos_per_divine == 195.0


async def test_cold_opaque_saved_url_shares_one_wait_budget(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, clock: FakeClock
) -> None:
    body = {"id": "Ab12Cd", "query": {"type": "Simple Robe"}}
    headers = {  # after this request the next search must wait 10 s
        "x-rate-limit-policy": "trade-search-request-limit",
        "x-rate-limit-rules": "Ip",
        "x-rate-limit-ip": "2:10:60",
        "x-rate-limit-ip-state": "2:10:0",
    }
    saved = api.router.get(f"{TRADE}/api/trade/search/Settlers/Ab12Cd").respond(
        200, json=body, headers=headers
    )
    search, _ = mock_trade(api)
    await get_ninja(ctx).dataset("Currency")
    trade.limiter.paused_until = clock() + 15  # resolving waits 15 s
    start = clock()
    with pytest.raises(PricingUnavailable) as err:
        await price_outcome(ctx, trade_url=f"{TRADE}/trade/search/Settlers/Ab12Cd")
    assert saved.call_count == 1 and search.call_count == 0
    assert clock() - start <= 20 and err.value.retry_after is not None
