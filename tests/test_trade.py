from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

from poe_craft_mcp.clients.trade import RateLimited, TradeClient, TradeError, get_trade
from poe_craft_mcp.clients.trade_models import decode_search_id, parse_saved_url
from poe_craft_mcp.config import Config
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.http.cache import ResponseCache
from poe_craft_mcp.http.ratelimit import RateLimiter
from poe_craft_mcp.log import add_file_log

if TYPE_CHECKING:
    from conftest import FakeClock

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "trade"
BASE = "https://www.pathofexile.com"
SECRET = "c0ffee-POESESSID-d34db33f"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def recorded(name: str) -> httpx.Response:
    data = fixture(name)
    headers = {k: v for k, v in data["headers"].items() if k.lower() != "content-type"}
    return httpx.Response(data["status"], json=data["body"], headers=headers)


SEARCH = fixture("search")["body"]
SAVED_URL = f"{BASE}/trade/search/Settlers/{SEARCH['id']}"


class FakeSleep:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    async def __call__(self, seconds: float) -> None:
        self.clock.advance(seconds)


@pytest.fixture
async def trade(
    config: Config, cache: ResponseCache, clock: FakeClock
) -> AsyncIterator[TradeClient]:
    limiter = RateLimiter(clock=clock, sleep=FakeSleep(clock))
    client = TradeClient(config, cache, limiter=limiter, poesessid=lambda: None)
    yield client
    await client.aclose()


def mock_search_and_fetch(league: str = "Covens") -> tuple[respx.Route, respx.Route]:
    search = respx.post(f"{BASE}/api/trade/search/{league}").mock(return_value=recorded("search"))
    fetch = respx.get(url__startswith=f"{BASE}/api/trade/fetch/").mock(
        return_value=recorded("fetch")
    )
    return search, fetch


def test_parse_saved_url_variants() -> None:
    saved = parse_saved_url(f"{BASE}/trade/exchange/Hardcore%20Covens/abc123/")
    assert (saved.kind, saved.url_league, saved.search_id) == (
        "exchange",
        "Hardcore Covens",
        "abc123",
    )
    with pytest.raises(TradeError):
        parse_saved_url(f"{BASE}/trade/about")
    assert decode_search_id("abc123") is None


@respx.mock
async def test_saved_url_decodes_locally_and_swaps_league(trade: TradeClient) -> None:
    resolved = await trade.resolve_saved_url(SAVED_URL)
    assert resolved.kind == "search"
    assert resolved.query["name"] == "Tabula Rasa"
    search, _ = mock_search_and_fetch("Covens")
    result = await trade.listings_for("Covens", SAVED_URL, sample=10)
    sent = json.loads(search.calls.last.request.content)
    assert sent["query"]["name"] == "Tabula Rasa"
    assert sent["query"]["status"] == {"option": "online"}
    assert sent["sort"] == {"price": "asc"}
    assert result["total"] == 356 and result["search_id"] == SEARCH["id"]


@respx.mock
async def test_opaque_saved_id_is_fetched_once_then_cached(trade: TradeClient) -> None:
    body = {"id": "Ab12Cd", "query": {"type": "Simple Robe"}, "sort": {"price": "desc"}}
    route = respx.get(f"{BASE}/api/trade/search/Hardcore%20Covens/Ab12Cd").respond(200, json=body)
    url = f"{BASE}/trade/search/Hardcore%20Covens/Ab12Cd"
    first = await trade.resolve_saved_url(url)
    second = await trade.resolve_saved_url(url)
    assert first == second
    assert (first.query, first.sort) == ({"type": "Simple Robe"}, {"price": "desc"})
    assert route.call_count == 1


@respx.mock
async def test_missing_saved_search_raises_trade_error(trade: TradeClient) -> None:
    respx.get(f"{BASE}/api/trade/search/Covens/Ab12Cd").mock(
        return_value=recorded("saved_search_404")
    )
    with pytest.raises(TradeError) as err:
        await trade.resolve_saved_url(f"{BASE}/trade/search/Covens/Ab12Cd")
    assert err.value.status == 404 and "Resource not found" in str(err.value)


@respx.mock
async def test_fetch_batches_of_ten_and_normalizes(trade: TradeClient, config: Config) -> None:
    _, fetch = mock_search_and_fetch()
    ids = [f"id{i}" for i in range(25)]
    listings = await trade.fetch(ids, "Q1")
    assert fetch.call_count == 3
    batches = [call.request.url.path.rsplit("/", 1)[1].split(",") for call in fetch.calls]
    assert [len(b) for b in batches] == [10, 10, 5]
    assert all(call.request.url.params["query"] == "Q1" for call in fetch.calls)
    assert all(c.request.headers["User-Agent"] == config.user_agent for c in fetch.calls)
    assert all("Cookie" not in call.request.headers for call in fetch.calls)
    assert len(listings) == 30
    first = listings[0]
    assert set(first) == {"id", "amount", "currency", "indexed", "account_online", "stack_size"}
    assert (first["amount"], first["currency"], first["account_online"]) == (5.0, "chaos", True)
    assert "account0" not in json.dumps(listings)


@respx.mock
async def test_cache_hit_within_trade_cache_min(trade: TradeClient, clock: FakeClock) -> None:
    search, fetch = mock_search_and_fetch()
    first = await trade.listings_for("Covens", SAVED_URL, sample=10)
    assert (first["from_cache"], first["stale"], len(first["listings"])) == (False, False, 10)
    clock.advance(19 * 60)
    second = await trade.listings_for("Covens", SAVED_URL, sample=10)
    assert (search.call_count, fetch.call_count) == (1, 1)
    assert (second["from_cache"], second["stale"], second["age_seconds"]) == (True, False, 1140)
    assert second["listings"] == first["listings"]
    clock.advance(2 * 60)
    await trade.listings_for("Covens", SAVED_URL, sample=10)
    assert search.call_count == 2


@respx.mock
async def test_rate_limited_falls_back_to_stale_cache(trade: TradeClient, clock: FakeClock) -> None:
    search, _ = mock_search_and_fetch()
    await trade.listings_for("Covens", SAVED_URL, sample=10)
    clock.advance(25 * 60)
    trade.limiter.paused_until = clock() + 120
    result = await trade.listings_for("Covens", SAVED_URL, sample=10)
    assert (result["stale"], result["from_cache"], result["age_seconds"]) == (True, True, 1500)
    assert search.call_count == 1
    with pytest.raises(RateLimited):
        await trade.listings_for("Standard", SAVED_URL, sample=10)


@respx.mock
async def test_exchange_offers_priced_per_unit(trade: TradeClient) -> None:
    route = respx.post(f"{BASE}/api/trade/exchange/Covens").mock(return_value=recorded("exchange"))
    result = await trade.exchange("Covens", want=["divine"], have=["chaos"])
    body = json.loads(route.calls.last.request.content)
    assert body["query"] == {"status": {"option": "online"}, "want": ["divine"], "have": ["chaos"]}
    assert result["total"] == 9 and len(result["listings"]) == 9
    amounts = [o["amount"] for o in result["listings"]]
    assert amounts == sorted(amounts) and result["listings"][0]["currency"] == "chaos"
    assert all(o["stack_size"] for o in result["listings"])


@respx.mock
async def test_instant_buyout_sets_securable_status(trade: TradeClient) -> None:
    search, _ = mock_search_and_fetch()
    await trade.listings_for("Covens", {"query": {"type": "Simple Robe"}}, instant_buyout=True)
    assert json.loads(search.calls.last.request.content)["query"]["status"] == {
        "option": "securable"
    }


@respx.mock
async def test_poesessid_sent_as_cookie_but_never_leaks(
    config: Config, cache: ResponseCache, clock: FakeClock, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    monkeypatch.setattr("keyring.get_password", lambda *_: None)
    monkeypatch.setenv("POESESSID", SECRET)
    caplog.set_level(logging.DEBUG)
    file_logger = add_file_log("poe_craft_mcp", tmp_path / "all.log")
    ctx = AppContext.create(config, clock=clock)
    trade = get_trade(ctx)
    assert get_trade(ctx) is trade
    trade.limiter.sleep = FakeSleep(clock)
    search, fetch = mock_search_and_fetch()
    errors: list[str] = []
    try:
        result = await trade.listings_for("Covens", SAVED_URL, sample=10)
        logging.getLogger("poe_craft_mcp.clients.trade").warning("cookie=%s", SECRET)
        respx.post(f"{BASE}/api/trade/search/Standard").respond(
            500, json={"error": {"message": f"bad session {SECRET}"}}
        )
        with pytest.raises(TradeError) as err:
            await trade.listings_for("Standard", SAVED_URL, sample=10)
        errors.append(str(err.value) + repr(err.value))
        respx.post(f"{BASE}/api/trade/search/Hardcore").respond(429, headers={"Retry-After": "60"})
        with pytest.raises(RateLimited) as limited:
            await trade.listings_for("Hardcore", SAVED_URL, sample=10)
        errors.append(str(limited.value) + repr(limited.value))
        state = json.dumps(trade.limiter.state())
    finally:
        await ctx.aclose()
        for logger in (file_logger, logging.getLogger("poe_craft_mcp.ratelimit")):
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
    for call in [*search.calls, *fetch.calls]:
        assert call.request.headers["Cookie"] == f"POESESSID={SECRET}"
        assert call.request.headers["User-Agent"] == config.user_agent
    assert "cookie=***" in caplog.text
    file_text = (tmp_path / "all.log").read_text(encoding="utf-8")
    assert "cookie=***" in file_text
    leaks = [caplog.text, file_text, *errors, state, json.dumps(result)]
    leaks += [r.getMessage() for r in caplog.records]
    assert all(SECRET not in text for text in leaks)
    assert (config.logs_dir / "ratelimit.log").exists()
    assert SECRET not in (config.logs_dir / "ratelimit.log").read_text(encoding="utf-8")
