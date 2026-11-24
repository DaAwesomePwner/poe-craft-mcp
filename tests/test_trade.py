from __future__ import annotations

import base64
import gzip
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

from poe_craft_mcp.clients.trade import NotCached, RateLimited, TradeClient, TradeError, get_trade
from poe_craft_mcp.clients.trade_models import decode_search_id, parse_saved_url, query_hash
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
    assert sent["query"]["status"] == {"option": "securable"}  # the saved URL says online
    assert sent["sort"] == {"price": "asc"}
    assert result["total"] == 356 and result["search_id"] == SEARCH["id"]
    assert result["dropped_not_instant_buyout"] == 0


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
    listings, not_buyout = await trade.fetch(ids, "Q1")
    assert fetch.call_count == 3
    batches = [call.request.url.path.rsplit("/", 1)[1].split(",") for call in fetch.calls]
    assert [len(b) for b in batches] == [10, 10, 5]
    assert all(call.request.url.params["query"] == "Q1" for call in fetch.calls)
    assert all(c.request.headers["User-Agent"] == config.user_agent for c in fetch.calls)
    assert all("Cookie" not in call.request.headers for call in fetch.calls)
    assert (len(listings), not_buyout) == (30, 0)
    first = listings[0]
    assert set(first) == {"id", "amount", "currency", "indexed", "stack_size"}
    assert (first["amount"], first["currency"]) == (5.0, "chaos")
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


@pytest.mark.parametrize("status", [None, "online", "any", "securable"])
@pytest.mark.parametrize("as_url", [False, True])
@respx.mock
async def test_every_search_is_forced_to_instant_buyout(
    trade: TradeClient, status: str | None, as_url: bool
) -> None:
    search, _ = mock_search_and_fetch()
    query: dict[str, Any] = {"type": "Simple Robe"}
    if status is not None:
        query["status"] = {"option": status}
    target: str | dict[str, Any] = {"query": query}
    if as_url:
        target = f"{BASE}/trade/search/Settlers/{blob(json.dumps(target).encode())}"
    await trade.listings_for("Covens", target, sample=10)
    sent = json.loads(search.calls.last.request.content)
    assert sent["query"] == {"type": "Simple Robe", "status": {"option": "securable"}}


@respx.mock
async def test_cache_key_follows_the_forced_status(
    trade: TradeClient, cache: ResponseCache
) -> None:
    search, fetch = mock_search_and_fetch()
    query = {"type": "Simple Robe", "status": {"option": "online"}}
    legacy = query_hash("search", query, True, False, 10)  # the old online-only key
    stale_whispers = {"listings": [{"amount": 1.0}], "total": 1, "search_id": "old"}
    await cache.put(f"trade://listings/Covens/{legacy}", json.dumps(stale_whispers))
    first = await trade.listings_for("Covens", {"query": query}, sample=10)
    assert (first["from_cache"], first["search_id"], search.call_count) == (False, SEARCH["id"], 1)
    # Queries differing only in status send the same securable search, so they share a key.
    for status in ("any", "securable"):
        again = {"query": {**query, "status": {"option": status}}}
        assert (await trade.listings_for("Covens", again, sample=10))["from_cache"] is True
    assert (search.call_count, fetch.call_count) == (1, 1)


@respx.mock
async def test_listings_without_the_buyout_fee_are_dropped_and_counted(
    trade: TradeClient,
) -> None:
    body = fixture("fetch")["body"]
    for entry in body["result"][:3]:
        del entry["listing"]["fee"]
        entry["listing"]["whisper"] = "@someone Hi, I would like to buy your item"
    body["result"][3]["listing"]["fee"] = None
    respx.post(f"{BASE}/api/trade/search/Covens").mock(return_value=recorded("search"))
    respx.get(url__startswith=f"{BASE}/api/trade/fetch/").respond(200, json=body)
    result = await trade.listings_for("Covens", SAVED_URL, sample=10)
    assert (len(result["listings"]), result["dropped_not_instant_buyout"]) == (6, 4)
    kept = {listing["id"] for listing in result["listings"]}
    assert kept == {entry["id"] for entry in body["result"][4:]}
    cached = await trade.listings_for("Covens", SAVED_URL, sample=10)
    assert (cached["from_cache"], cached["dropped_not_instant_buyout"]) == (True, 4)


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
            logger.setLevel(logging.NOTSET)
            logger.propagate = True
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


def make_client(config: Config, cache: ResponseCache, clock: FakeClock) -> TradeClient:
    limiter = RateLimiter(clock=clock, sleep=FakeSleep(clock))
    return TradeClient(config, cache, limiter=limiter, poesessid=lambda: SECRET)


def blob(payload: bytes) -> str:
    return base64.urlsafe_b64encode(gzip.compress(payload)).decode().rstrip("=")


@pytest.mark.parametrize("base_url", ["http://www.pathofexile.com", "https://trade.example.test"])
@respx.mock
async def test_cookie_only_sent_to_official_https_origin(
    base_url: str, tmp_path: Path, cache: ResponseCache, clock: FakeClock
) -> None:
    config = Config(data_dir=tmp_path / "data", trade_base_url=base_url)
    trade = make_client(config, cache, clock)
    route = respx.post(f"{base_url}/api/trade/search/Covens").mock(return_value=recorded("search"))
    await trade.search("Covens", {"type": "Simple Robe"})
    assert "Cookie" not in route.calls.last.request.headers
    await trade.aclose()


@respx.mock
async def test_redirects_are_not_followed(
    config: Config, cache: ResponseCache, clock: FakeClock
) -> None:
    trade = make_client(config, cache, clock)
    respx.post(f"{BASE}/api/trade/search/Covens").respond(
        302, headers={"Location": "https://elsewhere.example.test/steal"}
    )
    elsewhere = respx.route(host="elsewhere.example.test").respond(200, json={})
    with pytest.raises(TradeError, match="redirect refused"):
        await trade.search("Covens", {"type": "Simple Robe"})
    assert not elsewhere.called
    await trade.aclose()


@respx.mock
async def test_echoed_cookie_is_never_cached_or_returned(
    config: Config, cache: ResponseCache, clock: FakeClock
) -> None:
    trade = make_client(config, cache, clock)
    route = respx.post(f"{BASE}/api/trade/search/Covens")
    route.respond(200, json={"id": f"x{SECRET}", "result": [], "total": 0})
    with pytest.raises(TradeError) as err:
        await trade.listings_for("Covens", SAVED_URL, sample=10)
    assert SECRET not in str(err.value) + repr(err.value)
    route.respond(200, json={"id": "clean", "result": [], "total": 0})
    result = await trade.listings_for("Covens", SAVED_URL, sample=10)
    assert (result["from_cache"], result["search_id"]) == (False, "clean")
    await trade.aclose()


def test_decompression_bomb_and_oversized_ids_rejected() -> None:
    bomb = blob(b'{"a":"' + b"x" * 5_000_000 + b'"}')
    assert len(bomb) < 8192
    with pytest.raises(TradeError, match="too large"):
        decode_search_id(bomb)
    with pytest.raises(TradeError):
        parse_saved_url(f"{BASE}/trade/search/Covens/H4sI{'A' * 9000}")
    with pytest.raises(TradeError):
        decode_search_id("H4sI" + "A" * 9000)
    with pytest.raises(TradeError):
        decode_search_id(blob(b"[1, 2]"))


@respx.mock
async def test_one_deadline_covers_all_fetch_batches(trade: TradeClient, clock: FakeClock) -> None:
    respx.post(f"{BASE}/api/trade/search/Covens").mock(return_value=recorded("search"))
    slow = {
        "X-Rate-Limit-Policy": "trade-fetch-request-limit",
        "X-Rate-Limit-Rules": "Ip",
        "X-Rate-Limit-Ip": "2:12:60",
        "X-Rate-Limit-Ip-State": "1:12:0",
    }
    fetch = respx.get(url__startswith=f"{BASE}/api/trade/fetch/").respond(
        200, json=fixture("fetch")["body"], headers=slow
    )
    start = clock()
    with pytest.raises(RateLimited):
        await trade.listings_for("Covens", SAVED_URL, sample=25)
    assert fetch.call_count == 2
    assert clock() - start <= 20


@respx.mock
async def test_saved_descending_sort_is_forced_ascending(trade: TradeClient) -> None:
    search, _ = mock_search_and_fetch()
    body = {"id": "Ab12Cd", "query": {"type": "Simple Robe"}, "sort": {"price": "desc"}}
    respx.get(f"{BASE}/api/trade/search/Covens/Ab12Cd").respond(200, json=body)
    await trade.listings_for("Covens", f"{BASE}/trade/search/Covens/Ab12Cd", sample=10)
    assert json.loads(search.calls.last.request.content)["sort"] == {"price": "asc"}
    await trade.listings_for("Covens", {**body, "query": {"type": "Silk Robe"}}, sample=10)
    assert json.loads(search.calls.last.request.content)["sort"] == {"price": "asc"}


@respx.mock
async def test_exchange_urls_are_refused_without_a_request(trade: TradeClient) -> None:
    query = {"status": {"option": "securable"}, "have": ["chaos"], "want": ["divine"]}
    decodable = f"{BASE}/trade/exchange/Covens/{blob(json.dumps(query).encode())}"
    opaque = f"{BASE}/trade/exchange/Covens/Ab12Cd"
    anything = respx.route().respond(500)
    for url in (decodable, opaque):
        with pytest.raises(ValueError, match="exchange listings are whisper-based"):
            await trade.listings_for("Covens", url, sample=3)
        with pytest.raises(ValueError, match="use an item search"):
            await trade.listings_for("Covens", url, sample=3, cache_only=True)
    assert not anything.called


@pytest.mark.parametrize(
    ("base_url", "sends_cookie"),
    [
        ("https://www.pathofexile.com:8443", False),
        ("https://www.pathofexile.com:0", False),
        ("https://www.pathofexile.com:443", True),
    ],
)
@respx.mock
async def test_cookie_requires_default_https_port(
    base_url: str, sends_cookie: bool, tmp_path: Path, cache: ResponseCache, clock: FakeClock
) -> None:
    config = Config(data_dir=tmp_path / "data", trade_base_url=base_url)
    trade = make_client(config, cache, clock)
    route = respx.post(url__regex=r".*/api/trade/search/Covens$").mock(
        return_value=recorded("search")
    )
    await trade.search("Covens", {"type": "Simple Robe"})
    assert ("Cookie" in route.calls.last.request.headers) is sends_cookie
    await trade.aclose()


@respx.mock
async def test_json_escaped_cookie_echo_is_rejected_before_caching(
    config: Config, cache: ResponseCache, clock: FakeClock
) -> None:
    trade = make_client(config, cache, clock)
    escaped = "".join(f"\\u{ord(c):04x}" for c in SECRET)
    assert SECRET not in escaped
    route = respx.post(f"{BASE}/api/trade/search/Covens")
    route.respond(
        200,
        content=f'{{"id": "x{escaped}", "result": [], "total": 0}}'.encode(),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(TradeError, match="echoed") as err:
        await trade.listings_for("Covens", SAVED_URL, sample=10)
    assert SECRET not in str(err.value) + repr(err.value)
    route.respond(200, json={"id": "clean", "result": [], "total": 0})
    result = await trade.listings_for("Covens", SAVED_URL, sample=10)
    assert (result["from_cache"], result["search_id"]) == (False, "clean")
    await trade.aclose()


@respx.mock
async def test_cache_only_serves_any_cached_copy_and_never_requests(
    trade: TradeClient, clock: FakeClock
) -> None:
    search, fetch = mock_search_and_fetch()
    opaque = respx.get(f"{BASE}/api/trade/search/Covens/Ab12Cd")
    with pytest.raises(NotCached):
        await trade.listings_for("Covens", SAVED_URL, sample=10, cache_only=True)
    with pytest.raises(NotCached):
        await trade.resolve_saved_url(f"{BASE}/trade/search/Covens/Ab12Cd", cache_only=True)
    assert (search.call_count, fetch.call_count, opaque.call_count) == (0, 0, 0)
    await trade.listings_for("Covens", SAVED_URL, sample=10)
    fresh = await trade.listings_for("Covens", SAVED_URL, sample=10, cache_only=True)
    clock.advance(25 * 60)
    old = await trade.listings_for("Covens", SAVED_URL, sample=10, cache_only=True)
    assert (fresh["stale"], old["stale"], old["age_seconds"]) == (False, True, 1500)
    assert (search.call_count, fetch.call_count) == (1, 1)
